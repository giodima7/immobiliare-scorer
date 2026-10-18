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

  const sourceLbl = listing ? siteName(listing.source) : '';
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
    showToast(t('toast.link_copied'));
  } catch {
    prompt(t('toast.copy_link'), url);
  }
}

function updateBookmarkIcon() { /* bookmark buttons retired — Details shows collections inline */ }
function refreshAllBookmarkIcons() {}

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
    <div class="avatar-menu-header">${escapeHtml(t('menu.signed_in'))} <strong>${escapeHtml(userName)}</strong></div>
    <button class="avatar-menu-item" onclick="closeAvatarMenu(); switchTab('saved');">
      ${icon('heart')} ${escapeHtml(t('tab.saved'))} <span class="badge">${fmt(getRentFavs().length)} · ${fmt(collectionsCount)}</span>
    </button>
    <button class="avatar-menu-item" onclick="closeAvatarMenu(); openAlertsManager();">
      ${icon('bell')} ${escapeHtml(t('menu.my_alerts') || 'My alerts')}
      ${alertsTotal > 0
        ? `<span class="badge${alertsActive > 0 ? ' badge-alert' : ''}">${alertsTotal}</span>`
        : ''}
    </button>
    <button class="avatar-menu-item" onclick="closeAvatarMenu(); Auth.signOut();">
      ${icon('arrow-up-right')} ${escapeHtml(t('header.sign_out') || 'Sign out')}
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
          <h2 id="alertsManagerTitle">${icon('bell')} ${escapeHtml(t('menu.my_alerts') || 'My alerts')}</h2>
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
  if (body) body.innerHTML = `<p style="color:var(--text-secondary);font-size:13px;margin:8px 0">${escapeHtml(t('loading.listings') || 'Loading…')}</p>`;
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

// ── Saved view: favourites + collections ──────────────────────────────────────
// Favourites not in the loaded grid (archived since) are fetched by id.
let _savedRows = [];

async function _savedListings(ids) {
  const byId = new Map(ALL_RENT.map(x => [String(x.id), x]));
  for (const r of _savedRows) if (!byId.has(String(r.id))) byId.set(String(r.id), r);
  const missing = ids.filter(i => !byId.has(i));
  if (missing.length) {
    let rows = [];
    try {
      if (IS_LIVE) {
        const r = await fetch(`/rentals/by-ids?ids=${encodeURIComponent(missing.join(','))}`);
        if (r.ok) rows = await r.json();
      } else if (SupabaseClient.configured) {
        rows = await SupabaseClient.fetchByIds(missing, SupabaseClient._RENT_COLUMNS);
      }
    } catch { /* show what we have */ }
    for (const r of rows) { _savedRows.push(r); byId.set(String(r.id), r); }
  }
  return ids.map(i => byId.get(i)).filter(Boolean);
}

async function renderSaved() {
  const grid = document.getElementById('savedGrid');
  if (!grid) return;
  const ids = getRentFavs().map(String);
  const count = document.getElementById('savedFavCount');
  if (count) count.textContent = ids.length ? t(ids.length === 1 ? 'saved.n_one' : 'saved.n', { n: fmt(ids.length) }) : '';
  if (!ids.length) {
    grid.innerHTML = `<div class="empty-block" style="grid-column:1/-1">${escapeHtml(t('saved.empty'))}</div>`;
  } else {
    if (!grid.children.length) grid.innerHTML = `<div class="empty-block" style="grid-column:1/-1">${escapeHtml(t('loading.listings'))}</div>`;
    const rows = await _savedListings(ids);
    grid.innerHTML = '';
    const frag = document.createDocumentFragment();
    rows.forEach(l => { const c = renderRentalCard(l); c.classList.add('no-anim'); frag.appendChild(c); });
    grid.appendChild(frag);
    _observeScoreRings(grid, false);
    if (rows.length < ids.length) {
      grid.insertAdjacentHTML('beforeend', `<div class="empty-block" style="grid-column:1/-1">${escapeHtml(t('saved.gone', { n: ids.length - rows.length }))}</div>`);
    }
  }
  renderSavedCollections();
}

function renderSavedCollections() {
  const list = document.getElementById('savedCollections');
  if (!list) return;
  const all = Collections.getAll();
  if (!all.length) {
    list.innerHTML = `<div class="empty-block">${escapeHtml(t('col.empty'))}</div>`;
    return;
  }
  list.innerHTML = all.map(col => `
    <div class="col-card" data-col-id="${escapeHtml(col.id)}">
      <div class="col-card-main">
        <div class="col-card-name">${escapeHtml(col.name)}</div>
        <div class="col-card-meta">${escapeHtml(t(col.listing_ids.length === 1 ? 'col.meta_one' : 'col.meta', { n: fmt(col.listing_ids.length), when: formatRelativeTime(col.updated_at) }))}</div>
      </div>
      <div class="col-card-actions">
        <button type="button" class="btn btn-secondary btn-sm" data-col-action="view">${icon('eye')}<span>${escapeHtml(t('col.view'))}</span></button>
        <button type="button" class="btn btn-ghost btn-sm btn-icon" data-col-action="share" aria-label="${escapeHtml(t('col.share'))}" title="${escapeHtml(t('col.share'))}">${icon('share')}</button>
        <button type="button" class="btn btn-ghost btn-sm btn-icon" data-col-action="rename" aria-label="${escapeHtml(t('col.rename'))}" title="${escapeHtml(t('col.rename'))}">${icon('pencil')}</button>
        <button type="button" class="btn btn-ghost btn-sm btn-icon btn-danger-text" data-col-action="delete" aria-label="${escapeHtml(t('col.delete'))}" title="${escapeHtml(t('col.delete'))}">${icon('trash')}</button>
      </div>
    </div>`).join('');
}

// Rename in place: the name turns into an input; Enter saves, Escape cancels.
function _renameCollectionInline(card) {
  const id = card.dataset.colId;
  const col = Collections.getAll().find(c => c.id === id);
  if (!col) return;
  const nameEl = card.querySelector('.col-card-name');
  nameEl.innerHTML = `<input type="text" class="input" maxlength="60" value="${escapeHtml(col.name)}" aria-label="${escapeHtml(t('col.rename'))}">`;
  const input = nameEl.querySelector('input');
  input.focus(); input.select();
  const done = save => {
    if (save && input.value.trim() && input.value.trim() !== col.name) Collections.rename(id, input.value.trim());
    renderSavedCollections();
  };
  input.addEventListener('keydown', e => { if (e.key === 'Enter') done(true); if (e.key === 'Escape') done(false); });
  input.addEventListener('blur', () => done(true));
}

async function shareCollection(collectionId) {
  const url = Collections.buildShareUrl(collectionId);
  if (!url) { showToast(t('col.empty_share')); return; }
  const col = Collections.getAll().find(c => c.id === collectionId);
  if (navigator.share && /Mobi|Android|iPhone/i.test(navigator.userAgent)) {
    try { await navigator.share({ title: `Lume — ${col.name}`, url, text: 'Lume' }); return; }
    catch (e) {}
  }
  try {
    await navigator.clipboard.writeText(url);
    showToast(t('toast.link_copied'));
  } catch {
    prompt(t('toast.copy_link'), url);
  }
}

// Show a collection's flats in the Rentals list (same as opening its link).
function viewCollection(collectionId) {
  const col = Collections.getAll().find(c => c.id === collectionId);
  if (!col || !col.listing_ids.length) { showToast(t('col.empty_share')); return; }
  window.sharedCollection = { name: col.name, listing_ids: col.listing_ids.map(String), own: true };
  switchTab('rentals');
  renderRentals();
  window.scrollTo({ top: 0 });
}

function deleteCollectionConfirm(collectionId) {
  const col = Collections.getAll().find(c => c.id === collectionId);
  if (!col) return;
  if (confirm(t('col.delete_confirm', { name: col.name }))) {
    Collections.delete(collectionId);
    renderSavedCollections();
    showToast(t('col.deleted'));
  }
}

// ── Shared-collection viewer (?c=...) ────────────────────────────────────────
function handleSharedCollectionParam() {
  const params = new URLSearchParams(location.search);
  const encoded = params.get('c');
  if (!encoded) return false;
  const decoded = Collections.decodeFromUrl(encoded);
  if (!decoded) {
    showToast(t('col.bad_link'));
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
  const missing = sc.listing_ids.length - visibleCount;
  const missingNote = missing > 0
    ? `<div class="banner-sub">${escapeHtml(t('col.missing', { n: missing }))}</div>`
    : '';
  const banner = document.createElement('div');
  banner.className = 'shared-collection-banner';
  banner.innerHTML = `
    <div class="banner-icon">${icon('folder')}</div>
    <div class="banner-text">
      <div class="banner-title">${escapeHtml(t(sc.own ? 'col.viewing' : 'col.shared'))} <strong>${escapeHtml(sc.name)}</strong></div>
      <div class="banner-sub">${escapeHtml(t(visibleCount === 1 ? 'saved.n_one' : 'saved.n', { n: fmt(visibleCount) }))}</div>
      ${missingNote}
    </div>
    <button type="button" class="btn btn-secondary" onclick="exitSharedCollection()">${escapeHtml(t('col.show_all'))}</button>`;

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
