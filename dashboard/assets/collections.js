// Lume dashboard — collections, share menu, avatar menu, alerts modal, shared-collection and deep-link views.
// Plain script (no bundler); shares globals with the other assets/*.js files.
// Declarations only: code that runs at start-up lives in init.js, which loads
// last, so any function here may call any other.

// ── Collections module (per-user, localStorage-backed) ───────────────────────
const Collections = {
  getAll() { return Storage.get('collections', []) || []; },

  create(name) {
    const all = this.getAll();
    const col = {
      id: 'col_' + Math.random().toString(36).slice(2, 10),
      name: (name || 'Untitled').slice(0, 60),
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      listing_ids: [],
    };
    all.unshift(col);
    Storage.set('collections', all);
    return col;
  },

  addListing(collectionId, listingId) {
    const all = this.getAll();
    const col = all.find(c => c.id === collectionId);
    if (!col) return false;
    if (col.listing_ids.includes(String(listingId))) return false;
    if (col.listing_ids.length >= 100) return 'full';
    col.listing_ids.push(String(listingId));
    col.updated_at = new Date().toISOString();
    Storage.set('collections', all);
    return true;
  },

  removeListing(collectionId, listingId) {
    const all = this.getAll();
    const col = all.find(c => c.id === collectionId);
    if (!col) return false;
    col.listing_ids = col.listing_ids.filter(id => String(id) !== String(listingId));
    col.updated_at = new Date().toISOString();
    Storage.set('collections', all);
    return true;
  },

  delete(collectionId) {
    Storage.set('collections', this.getAll().filter(c => c.id !== collectionId));
  },

  rename(collectionId, newName) {
    const all = this.getAll();
    const col = all.find(c => c.id === collectionId);
    if (!col) return false;
    col.name = String(newName).slice(0, 60);
    col.updated_at = new Date().toISOString();
    Storage.set('collections', all);
    return true;
  },

  isInCollection(collectionId, listingId) {
    const col = this.getAll().find(c => c.id === collectionId);
    return col ? col.listing_ids.includes(String(listingId)) : false;
  },

  isInAnyCollection(listingId) {
    return this.getAll().some(c => c.listing_ids.includes(String(listingId)));
  },

  encodeForShare(collectionId) {
    const col = this.getAll().find(c => c.id === collectionId);
    if (!col || !col.listing_ids.length) return null;
    const payload = JSON.stringify({ n: col.name, i: col.listing_ids });
    return btoa(unescape(encodeURIComponent(payload)))
      .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  },

  decodeFromUrl(encoded) {
    try {
      let b64 = encoded.replace(/-/g, '+').replace(/_/g, '/');
      while (b64.length % 4) b64 += '=';
      const data = JSON.parse(decodeURIComponent(escape(atob(b64))));
      if (!data.n || !Array.isArray(data.i)) return null;
      return { name: String(data.n).slice(0, 60), listing_ids: data.i.map(String) };
    } catch (e) { return null; }
  },

  buildShareUrl(collectionId) {
    const encoded = this.encodeForShare(collectionId);
    return encoded ? `${location.origin}${location.pathname}?c=${encoded}` : null;
  },
};

// ── Share / collection menu — used by rental cards ───────────────────────────
async function shareListing(listingId, ev) {
  if (ev) ev.stopPropagation();
  // Look up the listing in the lexically-scoped ALL_RENT — it's defined as
  // `let` in this script, not attached to `window`.
  const idStr = String(listingId);
  const all   = (typeof ALL_RENT !== 'undefined' ? ALL_RENT : []);
  const listing = all.find(l => String(l.id) === idStr);

  // Prefer the original source URL (Immobiliare or Idealista); fall back to
  // the Lume deep-link only when the listing has no source URL stored.
  const sourceUrl = listing && listing.url;
  const url = sourceUrl
    || `${location.origin}${location.pathname}?l=${encodeURIComponent(listingId)}`;

  const sourceLbl = listing
    ? (listing.source && String(listing.source).startsWith('idealista')
       ? 'Idealista'
       : 'Immobiliare.it')
    : '';
  const title = listing
    ? `${listing.neighbourhood || 'Milan listing'} — Lume score ${listing.score_total ?? '?'}`
    : 'Lume listing';
  const text = listing
    ? `Lume score ${listing.score_total ?? '?'}/100${sourceLbl ? ' · ' + sourceLbl : ''}`
    : 'Shared via Lume';

  if (navigator.share && /Mobi|Android|iPhone/i.test(navigator.userAgent)) {
    try { await navigator.share({ title, url, text }); return; }
    catch (e) { /* user cancelled — fall through to clipboard */ }
  }
  try {
    await navigator.clipboard.writeText(url);
    showToast(sourceLbl ? `${sourceLbl} link copied` : 'Link copied to clipboard');
  } catch {
    prompt('Copy this link:', url);
  }
}

let _outsideClickHandler = null;
function closeCollectionMenu() {
  const m = document.getElementById('active-collection-menu');
  if (m) m.remove();
  if (_outsideClickHandler) {
    document.removeEventListener('click', _outsideClickHandler, true);
    _outsideClickHandler = null;
  }
}

function openCollectionMenu(listingId, ev) {
  if (ev) ev.stopPropagation();
  // Sign-in required to save to a collection. Opens Clerk modal if guest.
  if (typeof Auth !== 'undefined'
      && !Auth.requireSignIn('Sign in to save listings to collections')) {
    return;
  }
  closeCollectionMenu();

  const collections = Collections.getAll();
  const trigger = (ev && ev.currentTarget) || document.activeElement;
  const rect = trigger.getBoundingClientRect();

  const menu = document.createElement('div');
  menu.className = 'collection-menu';
  menu.id = 'active-collection-menu';

  const items = [];
  if (!collections.length) {
    items.push(`<div class="collection-empty">No collections yet</div>`);
  } else {
    collections.forEach(col => {
      const inCol = Collections.isInCollection(col.id, listingId);
      items.push(`
        <button class="collection-item${inCol ? ' checked' : ''}"
                onclick="toggleListingInCollection('${col.id}', '${escapeHtml(String(listingId))}', this)">
          <span class="collection-check">${inCol ? '✓' : ''}</span>
          <span class="collection-name">${escapeHtml(col.name)}</span>
          <span class="collection-count">${col.listing_ids.length}</span>
        </button>`);
    });
  }
  items.push(`
    <button class="collection-item collection-create"
            onclick="createCollectionFromMenu('${escapeHtml(String(listingId))}')">
      <span class="collection-check">+</span>
      <span class="collection-name">Create new collection</span>
    </button>`);

  menu.innerHTML = items.join('');
  document.body.appendChild(menu);

  // Position above or below the trigger depending on viewport space
  const menuHeight = Math.min(collections.length * 44 + 60, 320);
  const fitsBelow = (window.innerHeight - rect.bottom) > menuHeight + 10;
  menu.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - 268))}px`;
  menu.style.top  = fitsBelow
    ? `${rect.bottom + 6}px`
    : `${rect.top - menuHeight - 6}px`;

  _outsideClickHandler = (e) => {
    if (!menu.contains(e.target)) closeCollectionMenu();
  };
  setTimeout(() => document.addEventListener('click', _outsideClickHandler, true), 0);
}

function toggleListingInCollection(collectionId, listingId, btnEl) {
  const wasIn = Collections.isInCollection(collectionId, listingId);
  if (wasIn) {
    Collections.removeListing(collectionId, listingId);
    btnEl.classList.remove('checked');
    btnEl.querySelector('.collection-check').textContent = '';
    showToast('Removed from collection');
  } else {
    const result = Collections.addListing(collectionId, listingId);
    if (result === 'full') {
      showToast('Collections are capped at 100 listings to keep share links short.');
      return;
    }
    btnEl.classList.add('checked');
    btnEl.querySelector('.collection-check').textContent = '✓';
    showToast('Added to collection');
  }
  updateBookmarkIcon(listingId);
  const newCount = Collections.getAll().find(c => c.id === collectionId)?.listing_ids.length || 0;
  btnEl.querySelector('.collection-count').textContent = newCount;
}

function createCollectionFromMenu(listingId) {
  const name = prompt('Collection name:', '');
  if (!name || !name.trim()) return;
  const col = Collections.create(name.trim());
  Collections.addListing(col.id, listingId);
  updateBookmarkIcon(listingId);
  closeCollectionMenu();
  showToast(`Created "${col.name}" and added listing`);
}

function updateBookmarkIcon(listingId) {
  const isInAny = Collections.isInAnyCollection(listingId);
  document.querySelectorAll(`.btn-bookmark[data-listing-id="${CSS.escape(String(listingId))}"]`)
    .forEach(btn => btn.classList.toggle('active', isInAny));
}

function refreshAllBookmarkIcons() {
  document.querySelectorAll('.btn-bookmark[data-listing-id]').forEach(btn => {
    const id = btn.getAttribute('data-listing-id');
    btn.classList.toggle('active', Collections.isInAnyCollection(id));
  });
}

// ── Avatar dropdown menu ──────────────────────────────────────────────────────
let _avatarMenuOutside = null;
function closeAvatarMenu() {
  const m = document.getElementById('active-avatar-menu');
  if (m) m.remove();
  if (_avatarMenuOutside) {
    document.removeEventListener('click', _avatarMenuOutside, true);
    _avatarMenuOutside = null;
  }
}

function showUserMenu(ev) {
  if (ev) ev.stopPropagation();
  closeAvatarMenu();

  const avatar = document.getElementById('user-avatar');
  const rect = avatar.getBoundingClientRect();
  const collectionsCount = Collections.getAll().length;
  const userName = (typeof Auth !== 'undefined' && Auth.userName) ? Auth.userName() : 'User';
  // Alert counts come from the cached _digestActiveCount maintained by
  // _refreshDigestAlertBadge(). Falls back to 0 before the first refresh.
  const alertsActive = (typeof window._digestActiveCount === 'number') ? window._digestActiveCount : 0;
  const alertsTotal  = (typeof window._digestTotalCount  === 'number') ? window._digestTotalCount  : 0;

  const menu = document.createElement('div');
  menu.className = 'avatar-menu';
  menu.id = 'active-avatar-menu';
  menu.innerHTML = `
    <div class="avatar-menu-header">Signed in as <strong>${escapeHtml(userName)}</strong></div>
    <button class="avatar-menu-item" onclick="closeAvatarMenu(); openCollectionsDrawer();">
      📁 My collections <span class="badge">${collectionsCount}</span>
    </button>
    <button class="avatar-menu-item" onclick="closeAvatarMenu(); openAlertsManager();">
      🔔 ${escapeHtml(t('menu.my_alerts') || 'My alerts')}
      ${alertsTotal > 0
        ? `<span class="badge${alertsActive > 0 ? ' badge-alert' : ''}">${alertsTotal}</span>`
        : ''}
    </button>
    <button class="avatar-menu-item" onclick="closeAvatarMenu(); Auth.signOut();">
      🚪 ${escapeHtml(t('header.sign_out') || 'Sign out')}
    </button>`;
  document.body.appendChild(menu);

  menu.style.right = `${Math.max(8, window.innerWidth - rect.right)}px`;
  menu.style.top   = `${rect.bottom + 8}px`;

  _avatarMenuOutside = (e) => {
    if (!menu.contains(e.target) && e.target !== avatar) closeAvatarMenu();
  };
  setTimeout(() => document.addEventListener('click', _avatarMenuOutside, true), 0);
}

// ── My alerts modal (avatar-menu entry) ───────────────────────────────────
// Reuses the Settings-tab list renderer. Opens as a centered drawer the
// user can dismiss via Esc, backdrop click, or the close button.
async function openAlertsManager() {
  if (!IS_LIVE) { showToast(t('alerts.local_only')); return; }
  const user = _alertUser();
  if (!user) {
    showToast(t('alerts.local_only'));
    return;
  }
  let back = document.getElementById('alertsManagerBackdrop');
  if (!back) {
    back = document.createElement('div');
    back.id = 'alertsManagerBackdrop';
    back.className = 'save-alert-backdrop';     // reuse modal styling
    back.onclick = (ev) => closeAlertsManager(ev);
    back.innerHTML = `
      <div class="save-alert-modal" onclick="event.stopPropagation()" role="dialog" aria-modal="true">
        <div class="save-alert-header">
          <h2 id="alertsManagerTitle">🔔 ${escapeHtml(t('menu.my_alerts') || 'My alerts')}</h2>
          <button type="button" class="save-alert-close" onclick="closeAlertsManager()" aria-label="Close">×</button>
        </div>
        <div id="alertsManagerBody"></div>
      </div>`;
    document.body.appendChild(back);
  }
  // Always render with a fresh fetch — pause/delete actions invalidate
  // the cached list, and re-opening the modal is the user's natural
  // refresh signal.
  const body = document.getElementById('alertsManagerBody');
  if (body) body.innerHTML = `<p style="color:var(--muted);font-size:13px;margin:8px 0">${escapeHtml(t('loading.listings') || 'Loading…')}</p>`;
  back.hidden = false;
  document.addEventListener('keydown', _alertsManagerEscHandler);

  const html = await renderDigestSettingsSection();
  if (body) body.innerHTML = html;
  // Re-translate any newly-injected data-i18n nodes (Settings list uses
  // the same tags as the Settings tab itself).
  body?.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = t(el.dataset.i18n); });
}

function closeAlertsManager(ev) {
  if (ev && ev.target && ev.currentTarget && ev.target !== ev.currentTarget) return;
  const back = document.getElementById('alertsManagerBackdrop');
  if (back) back.hidden = true;
  document.removeEventListener('keydown', _alertsManagerEscHandler);
}

function _alertsManagerEscHandler(ev) {
  if (ev.key === 'Escape') closeAlertsManager();
}

// ── Avatar badge: fetch the user's alerts and dot the avatar when any
// are active. Called from Auth._onLogin (after sign-in) and again after
// any save/pause/delete via the existing renderDigestSettingsSection
// refresh callers — we hook in there to update the cache.
async function _refreshDigestAlertBadge() {
  const user = _alertUser();
  const dot = document.getElementById('avatar-alert-dot');
  if (!user) {
    window._digestActiveCount = 0;
    window._digestTotalCount  = 0;
    if (dot) dot.style.display = 'none';
    return;
  }
  try {
    const rows = await _fetchDigestFilters(user.id);
    window._digestTotalCount  = rows.length;
    window._digestActiveCount = rows.filter(r => r.active).length;
    if (dot) {
      dot.style.display = (window._digestActiveCount > 0) ? 'block' : 'none';
    }
  } catch (_) {
    // Silent — the badge is informational. A failed fetch just leaves
    // the avatar dotless until the next refresh.
  }
}

// ── Collections drawer ────────────────────────────────────────────────────────
function openCollectionsDrawer() {
  let backdrop = document.getElementById('collections-drawer-backdrop');
  let drawer   = document.getElementById('collections-drawer');
  if (!drawer) {
    backdrop = document.createElement('div');
    backdrop.id = 'collections-drawer-backdrop';
    backdrop.className = 'collections-drawer-backdrop';
    backdrop.onclick = closeCollectionsDrawer;
    document.body.appendChild(backdrop);

    drawer = document.createElement('aside');
    drawer.id = 'collections-drawer';
    drawer.className = 'collections-drawer';
    drawer.innerHTML = `
      <div class="drawer-header">
        <h2>My collections</h2>
        <button class="drawer-close" onclick="closeCollectionsDrawer()" aria-label="Close">×</button>
      </div>
      <div class="drawer-body" id="collections-list"></div>
      <div class="drawer-footer">
        <button class="btn-create-collection" onclick="createCollectionFromDrawer()">
          + Create new collection
        </button>
      </div>`;
    document.body.appendChild(drawer);
  }
  renderCollectionsList();
  requestAnimationFrame(() => {
    backdrop.classList.add('open');
    drawer.classList.add('open');
  });
  document.addEventListener('keydown', _drawerEscHandler);
}

function closeCollectionsDrawer() {
  const backdrop = document.getElementById('collections-drawer-backdrop');
  const drawer   = document.getElementById('collections-drawer');
  if (backdrop) backdrop.classList.remove('open');
  if (drawer) drawer.classList.remove('open');
  document.removeEventListener('keydown', _drawerEscHandler);
  setTimeout(() => {
    backdrop?.remove();
    drawer?.remove();
  }, 280);
}
function _drawerEscHandler(e) { if (e.key === 'Escape') closeCollectionsDrawer(); }

function renderCollectionsList() {
  const list = document.getElementById('collections-list');
  if (!list) return;
  const all = Collections.getAll();
  if (!all.length) {
    list.innerHTML = `
      <div class="collections-empty">
        <div class="empty-icon">📁</div>
        <div class="empty-title">No collections yet</div>
        <div class="empty-text">Save listings into collections to organise them and share with friends.</div>
      </div>`;
    return;
  }
  list.innerHTML = all.map(col => `
    <div class="collection-card" data-id="${col.id}">
      <div class="collection-card-main">
        <div class="collection-card-name" onclick="renameCollectionInline('${col.id}', this)">${escapeHtml(col.name)}</div>
        <div class="collection-card-meta">
          ${col.listing_ids.length} listing${col.listing_ids.length === 1 ? '' : 's'}
          · updated ${formatRelativeTime(col.updated_at)}
        </div>
      </div>
      <div class="collection-card-actions">
        <button onclick="shareCollection('${col.id}')" title="Share collection" aria-label="Share">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/>
            <line x1="8.6" y1="13.5" x2="15.4" y2="17.5"/><line x1="15.4" y1="6.5" x2="8.6" y2="10.5"/>
          </svg>
        </button>
        <button onclick="viewCollection('${col.id}')" title="View collection" aria-label="View">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>
          </svg>
        </button>
        <button class="danger" onclick="deleteCollectionConfirm('${col.id}')" title="Delete collection" aria-label="Delete">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/>
          </svg>
        </button>
      </div>
    </div>`).join('');
}

async function shareCollection(collectionId) {
  const url = Collections.buildShareUrl(collectionId);
  if (!url) { showToast('Collection is empty — add listings first'); return; }
  const col = Collections.getAll().find(c => c.id === collectionId);
  if (navigator.share && /Mobi|Android|iPhone/i.test(navigator.userAgent)) {
    try { await navigator.share({ title: `Lume — ${col.name}`, url, text: 'Lume collection' }); return; }
    catch (e) {}
  }
  try {
    await navigator.clipboard.writeText(url);
    showToast('Share link copied to clipboard');
  } catch {
    prompt('Copy this link:', url);
  }
}

function viewCollection(collectionId) {
  const url = Collections.buildShareUrl(collectionId);
  if (!url) { showToast('Collection is empty'); return; }
  closeCollectionsDrawer();
  location.href = url;
}

function deleteCollectionConfirm(collectionId) {
  const col = Collections.getAll().find(c => c.id === collectionId);
  if (!col) return;
  if (confirm(`Delete "${col.name}"? This cannot be undone.`)) {
    Collections.delete(collectionId);
    renderCollectionsList();
    refreshAllBookmarkIcons();
    showToast('Collection deleted');
  }
}

function createCollectionFromDrawer() {
  const name = prompt('Collection name:', '');
  if (!name || !name.trim()) return;
  Collections.create(name.trim());
  renderCollectionsList();
  showToast('Collection created');
}

function renameCollectionInline(collectionId, el) {
  const current = el.textContent;
  const newName = prompt('Rename collection:', current);
  if (newName && newName.trim() && newName !== current) {
    Collections.rename(collectionId, newName.trim());
    renderCollectionsList();
  }
}

// ── Shared-collection viewer (?c=...) ────────────────────────────────────────
function handleSharedCollectionParam() {
  const params = new URLSearchParams(location.search);
  const encoded = params.get('c');
  if (!encoded) return false;
  const decoded = Collections.decodeFromUrl(encoded);
  if (!decoded) {
    showToast('Shared collection link is invalid');
    return false;
  }
  window.sharedCollection = decoded;
  return true;
}

function exitSharedCollection() {
  window.sharedCollection = null;
  const url = new URL(location);
  url.searchParams.delete('c');
  history.replaceState({}, '', url);
  document.querySelector('.shared-collection-banner')?.remove();
  // Re-render whatever tab is active so the dataset un-filters
  if (typeof renderRentals === 'function') renderRentals();
}

function renderSharedCollectionBanner(visibleCount, totalRequested) {
  // Place the banner above the active grid
  document.querySelectorAll('.shared-collection-banner').forEach(b => b.remove());
  if (!window.sharedCollection) return;
  const sc = window.sharedCollection;
  const isLoggedOut = !(typeof Auth !== 'undefined' && Auth.user);
  const cta = isLoggedOut
    ? `· <a href="#" onclick="Auth.openSignIn();return false;">Sign up to see all of Milan with Lume scoring →</a>`
    : '';
  const missing = sc.listing_ids.length - totalRequested;
  const missingNote = missing > 0
    ? `<div class="banner-sub" style="color:var(--amber)">${missing} listing${missing===1?'':'s'} in this collection ${missing===1?'is':'are'} no longer available.</div>`
    : '';
  const banner = document.createElement('div');
  banner.className = 'shared-collection-banner';
  banner.innerHTML = `
    <div class="banner-icon">📁</div>
    <div class="banner-text">
      <div class="banner-title">Shared collection: <strong>${escapeHtml(sc.name)}</strong></div>
      <div class="banner-sub">${visibleCount} listing${visibleCount===1?'':'s'} ${cta}</div>
      ${missingNote}
    </div>
    <button class="banner-clear" onclick="exitSharedCollection()">Show all listings</button>`;

  // Insert above whichever grid is currently visible
  const target = document.querySelector('#rentalsView:not([hidden]) .grid');
  if (target && target.parentNode) {
    target.parentNode.insertBefore(banner, target);
  } else {
    document.body.prepend(banner);
  }
}

// ── ?l= deep-link to a single listing ────────────────────────────────────────
function handleSharedListingParam() {
  const params = new URLSearchParams(location.search);
  const id = params.get('l');
  if (!id) return;
  // Defer until the cards have rendered
  const tryHighlight = (attemptsLeft = 20) => {
    const card = document.querySelector(`.card[data-listing-id="${CSS.escape(id)}"]`);
    if (card) {
      card.scrollIntoView({ behavior: 'smooth', block: 'center' });
      card.classList.add('shared-highlight');
      setTimeout(() => card.classList.remove('shared-highlight'), 3500);
    } else if (attemptsLeft > 0) {
      setTimeout(() => tryHighlight(attemptsLeft - 1), 250);
    }
  };
  setTimeout(tryHighlight, 400);
}
