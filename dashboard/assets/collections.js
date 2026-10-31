// Lume dashboard — collections, share menu, avatar menu, alerts modal, shared-collection and deep-link views.
// Plain script (no bundler); shares globals with the other assets/*.js files.
// Declarations only: code that runs at start-up lives in init.js, which loads
// last, so any function here may call any other.

// ── Collections module (per-user, localStorage-backed) ───────────────────────
const Collections = {
  getAll() { return Storage.get('collections', []) || []; },

  // A collection holds rentals or sales: kind 'sale' on sale ones only, so the
  // rental collections saved before 2026-10-31 stay byte-identical (sync).
  ofKind(kind) { return this.getAll().filter(c => (c.kind || 'rent') === kind); },

  create(name, kind = 'rent') {
    const all = this.getAll();
    const col = {
      id: 'col_' + Math.random().toString(36).slice(2, 10),
      name: (name || 'Untitled').slice(0, 60),
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      listing_ids: [],
    };
    if (kind === 'sale') col.kind = 'sale';
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

  // The whole list in one write (choosing several flats at once).
  setListings(collectionId, ids) {
    const all = this.getAll();
    const col = all.find(c => c.id === collectionId);
    if (!col) return false;
    col.listing_ids = [...new Set(ids.map(String))].slice(0, 100);
    col.updated_at = new Date().toISOString();
    Storage.set('collections', all);
    return true;
  },

  // A flat no longer saved leaves every collection of its kind. Returns the
  // collections it was in, for the toast and its undo (restore).
  removeEverywhere(listingId, kind) {
    const all = this.getAll(), id = String(listingId), touched = [];
    for (const c of all) {
      if ((c.kind || 'rent') !== kind || !c.listing_ids.includes(id)) continue;
      c.listing_ids = c.listing_ids.filter(x => x !== id);
      c.updated_at = new Date().toISOString();
      touched.push({ id: c.id, name: c.name });
    }
    if (touched.length) Storage.set('collections', all);
    return touched;
  },

  restore(listingId, collectionIds) {
    const all = this.getAll(), id = String(listingId);
    for (const c of all) {
      if (collectionIds.includes(c.id) && !c.listing_ids.includes(id)) {
        c.listing_ids.push(id);
        c.updated_at = new Date().toISOString();
      }
    }
    Storage.set('collections', all);
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
    const payload = JSON.stringify({ n: col.name, i: col.listing_ids, ...(col.kind === 'sale' ? { k: 's' } : {}) });
    return btoa(unescape(encodeURIComponent(payload)))
      .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  },

  decodeFromUrl(encoded) {
    try {
      let b64 = encoded.replace(/-/g, '+').replace(/_/g, '/');
      while (b64.length % 4) b64 += '=';
      const data = JSON.parse(decodeURIComponent(escape(atob(b64))));
      if (!data.n || !Array.isArray(data.i)) return null;
      return { name: String(data.n).slice(0, 60), listing_ids: data.i.map(String),
               kind: data.k === 's' ? 'sale' : 'rent' };
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
  const all   = [...(typeof ALL_RENT !== 'undefined' ? ALL_RENT : []), ...(typeof ALL_SALE !== 'undefined' ? ALL_SALE : [])];
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
      ${icon('heart')} ${escapeHtml(t('tab.saved'))} <span class="badge">${fmt(getRentFavs().length + getSaleFavs().length)}</span>
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

// ── Saved view ────────────────────────────────────────────────────────────────
// One page (2026-10-31): Rentals | For sale, then that kind's collections as
// chips over one grid. A collection is a named subset of the saved flats of
// one kind: ticking one saves the flat, un-saving takes it out of every
// collection of its kind, deleting a collection keeps the hearts.
let _savedRows = [];            // saved rentals fetched by id (gone from the grid)
let _savedPick = null;          // { colId, ids: Set } while choosing flats for a collection
let _savedSeq = 0;              // the latest render wins (sales may still be loading)
let _savedGone = [];            // ids of the shown list that resolve to nothing

// Which side and which chip were last shown — this browser only.
function _savedView() {
  const v = Storage.get('savedView', null) || {};
  return { kind: v.kind === 'sale' ? 'sale' : 'rent', col: { rent: '', sale: '', ...(v.col || {}) } };
}
function _setSavedView(kind, colId) {
  const v = _savedView();
  if (kind) v.kind = kind;
  if (colId !== undefined) v.col[v.kind] = colId;
  Storage.set('savedView', v);
}

function _favKey(kind) { return kind === 'sale' ? 'saleFavourites' : 'rentFavourites'; }
function _favIds(kind) { return (Storage.get(_favKey(kind), []) || []).map(String); }

// Save flats (a collection ticked on an unsaved flat), one write.
function _addFavs(ids, kind) {
  const have = _favIds(kind), add = ids.map(String).filter(i => !have.includes(i));
  if (!add.length) return;
  Storage.set(_favKey(kind), [...have, ...add]);
  add.forEach(i => kind === 'sale' ? setSaleFavUi(i, true) : setFavUi(i, true));
  updateFavBadge();
}

// Collections saved before they implied the heart: their flats become saved
// (once per browser, when there are collections to look at).
function _savedMigrate() {
  if (Storage.get('savedColMigrated', false) || !Collections.getAll().length) return;
  _addFavs(Collections.ofKind('rent').flatMap(c => c.listing_ids), 'rent');
  Storage.set('savedColMigrated', true);
}

// Saved ids → cards, each card with the saved ids it stands for: a copy the
// dashboard folded into its twin (dedup, also_on) is the twin's card.
async function _savedListings(ids, kind) {
  let byId;
  if (kind === 'sale') {
    if (!ALL_SALE.length && typeof loadSales === 'function') await loadSales();
    byId = new Map(ALL_SALE.map(l => [String(l.id), l]));
    for (const l of ALL_SALE) for (const a of l.also_on || []) if (a.id != null && !byId.has(String(a.id))) byId.set(String(a.id), l);
  } else {
    byId = new Map(ALL_RENT.map(x => [String(x.id), x]));
    for (const l of ALL_RENT) for (const a of l.also_on || []) if (a.id != null && !byId.has(String(a.id))) byId.set(String(a.id), l);
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
  }
  const cards = new Map(), gone = [];
  for (const i of ids) {
    const l = byId.get(i);
    if (!l) { gone.push(i); continue; }
    if (!cards.has(l)) cards.set(l, []);
    cards.get(l).push(i);
  }
  // Off-market flats last (they keep their own banner).
  const out = [...cards].map(([l, ids]) => ({ l, ids }));
  return { cards: [...out.filter(c => !c.l.is_stale), ...out.filter(c => c.l.is_stale)], gone };
}

function _savedCardEl(l, kind) {
  if (kind === 'sale') {
    const tmp = document.createElement('div');
    tmp.innerHTML = saleCardHtml(l, typeof saleFilters === 'function' ? saleFilters() : {});
    const el = tmp.firstElementChild;
    el.classList.add('no-anim');
    return el;
  }
  const el = renderRentalCard(l);
  el.classList.add('no-anim');
  return el;
}

async function renderSaved() {
  const grid = document.getElementById('savedGrid');
  if (!grid) return;
  const esc = escapeHtml, block = (html, cls = '') => `<div class="empty-block${cls}" style="grid-column:1/-1">${html}</div>`;
  const seq = ++_savedSeq;
  _savedMigrate();
  const v = _savedView();
  let pickCol = _savedPick && Collections.getAll().find(c => c.id === _savedPick.colId);
  if (_savedPick && !pickCol) _savedPickEnd();
  const kind = pickCol ? (pickCol.kind || 'rent') : v.kind;
  const cols = Collections.ofKind(kind);
  const col = pickCol ? null : cols.find(c => c.id === v.col[kind]) || null;
  const favs = _favIds(kind);
  document.getElementById('savedView')?.classList.toggle('is-picking', !!pickCol);
  _renderSavedKind(kind);
  _renderSavedChips(cols, col);
  _renderSavedColbar(col, favs.length);
  _renderSavedPickBars(pickCol);
  // The local API answers first: before that, saved rentals would be looked
  // up online and the page said "2 saved listings are no longer online".
  if (!_liveKnown) { grid.innerHTML = block(esc(t('loading.listings'))); return; }

  let ids;
  if (pickCol) {                                   // every saved flat, members ticked
    const members = pickCol.listing_ids.map(String);
    ids = [...new Set([...favs.slice().reverse(), ...members.slice().reverse()])];
  } else ids = (col ? col.listing_ids.map(String) : favs).slice().reverse();   // newest first

  if (!ids.length) {
    grid.innerHTML = col
      ? block(`${esc(t('col.empty_here', { name: col.name }))} ${esc(t('col.empty_hint'))}`
              + (favs.length ? `<div><button type="button" class="btn btn-primary btn-sm" data-col-pick>${icon('check')}<span>${esc(t('col.pick'))}</span></button></div>` : ''), ' saved-empty')
      : block(`${esc(t(kind === 'sale' ? 'saved.empty_sale' : 'saved.empty_rent'))}
              <div><button type="button" class="btn btn-secondary btn-sm" data-saved-go="${kind}">${esc(t(kind === 'sale' ? 'saved.go_sale' : 'saved.go_rent'))}</button></div>`, ' saved-empty');
    return;
  }
  if (!grid.querySelector('.card')) grid.innerHTML = block(esc(t('loading.listings')));
  const { cards, gone } = await _savedListings(ids, kind);
  if (seq !== _savedSeq) return;                  // a newer render took over
  _savedGone = gone;
  grid.innerHTML = '';
  const frag = document.createDocumentFragment();
  for (const { l, ids: own } of cards) {
    const el = _savedCardEl(l, kind);
    el.dataset.savedIds = own.join(',');
    if (pickCol) {
      const on = own.some(i => _savedPick.ids.has(i));
      el.classList.toggle('is-picked', on);
      el.setAttribute('role', 'checkbox');
      el.setAttribute('aria-checked', String(on));
      el.tabIndex = 0;
      el.insertAdjacentHTML('afterbegin', `<span class="pick-check" aria-hidden="true">${icon('check')}</span>`);
    }
    frag.appendChild(el);
  }
  grid.appendChild(frag);
  if (gone.length && !pickCol) {
    grid.insertAdjacentHTML('beforeend', `<div class="empty-block saved-gone" style="grid-column:1/-1">
      <span>${esc(t(gone.length === 1 ? 'saved.gone_one' : 'saved.gone', { n: fmt(gone.length) }))}</span>
      <button type="button" class="btn btn-secondary btn-sm" data-saved-gone>${esc(t('saved.gone_remove'))}</button></div>`);
  }
  _observeScoreRings(grid, false);
  if (pickCol) _savedPickCount();
}

function _renderSavedKind(kind) {
  const box = document.getElementById('savedKind');
  if (!box) return;
  box.setAttribute('aria-label', t('saved.kind'));
  box.innerHTML = ['rent', 'sale'].map(k => `<button type="button" class="seg-btn${k === kind ? ' active' : ''}" data-saved-kind="${k}" aria-pressed="${k === kind}">${escapeHtml(t(k === 'sale' ? 'tab.sales' : 'tab.rentals'))} <span class="pill-count">${fmt(_favIds(k).length)}</span></button>`).join('');
}

function _renderSavedChips(cols, col) {
  const box = document.getElementById('savedChips');
  if (!box) return;
  const esc = escapeHtml, x = box.scrollLeft;
  const chip = (id, name, n, on) => `<button type="button" class="col-chip" role="radio" aria-checked="${on}" data-saved-col="${esc(id)}"><span class="col-chip-name">${esc(name)}</span>${n != null ? `<span class="pill-count">${fmt(n)}</span>` : ''}</button>`;
  box.setAttribute('aria-label', t('saved.collections'));
  box.innerHTML = chip('', t('saved.all'), null, !col)
    + cols.map(c => chip(c.id, c.name, c.listing_ids.length, !!col && c.id === col.id)).join('')
    + `<button type="button" class="col-chip col-chip-new" data-col-new-chip>${icon('plus')}<span>${esc(t('col.new_chip'))}</span></button>`;
  box.scrollLeft = x;
}

function _renderSavedColbar(col, nFavs) {
  const bar = document.getElementById('savedColbar');
  if (!bar) return;
  bar.hidden = !col;
  if (!col) { bar.innerHTML = ''; delete bar.dataset.colId; return; }
  const esc = escapeHtml, n = col.listing_ids.length;
  bar.dataset.colId = col.id;
  bar.innerHTML = `<div class="saved-colbar-main">
      <div class="saved-colbar-name">${esc(col.name)}</div>
      <div class="saved-colbar-meta">${esc(t(n === 1 ? 'col.meta_one' : 'col.meta', { n: fmt(n), when: formatRelativeTime(col.updated_at) }))}</div>
    </div>
    <button type="button" class="btn btn-secondary btn-sm" data-col-pick ${nFavs || n ? '' : 'disabled'}>${icon('check')}<span>${esc(t('col.pick'))}</span></button>
    <button type="button" class="btn btn-ghost btn-sm btn-icon" data-col-more aria-haspopup="menu" aria-label="${esc(t('col.more'))}" title="${esc(t('col.more'))}">${icon('dots-vertical')}</button>`;
}

// ⋯ on the collection bar: Copy link, Rename, Delete (the avatar menu's look).
function _savedColMenu(btn) {
  const id = document.getElementById('savedColbar')?.dataset.colId;
  if (!id) return;
  closeAvatarMenu();
  const esc = escapeHtml, rect = btn.getBoundingClientRect();
  const menu = document.createElement('div');
  menu.className = 'avatar-menu';
  menu.id = 'active-avatar-menu';
  menu.setAttribute('role', 'menu');
  menu.innerHTML = `
    <button type="button" class="avatar-menu-item" role="menuitem" data-col-menu="share">${icon('share')} ${esc(t('col.share'))}</button>
    <button type="button" class="avatar-menu-item" role="menuitem" data-col-menu="rename">${icon('pencil')} ${esc(t('col.rename'))}</button>
    <button type="button" class="avatar-menu-item btn-danger-text" role="menuitem" data-col-menu="delete">${icon('trash')} ${esc(t('col.delete'))}</button>`;
  document.body.appendChild(menu);
  menu.style.right = `${Math.max(8, window.innerWidth - rect.right)}px`;
  menu.style.top = `${rect.bottom + 6}px`;
  menu.addEventListener('click', e => {
    const item = e.target.closest('[data-col-menu]');
    if (!item) return;
    closeAvatarMenu();
    ({ share: () => shareCollection(id), rename: () => _renameCollectionInline(document.getElementById('savedColbar')),
       delete: () => deleteCollectionConfirm(id) })[item.dataset.colMenu]();
  });
  menu.addEventListener('keydown', e => { if (e.key === 'Escape') { closeAvatarMenu(); btn.focus(); } });
  menu.querySelector('button')?.focus();
  _avatarMenuOutside = (e) => { if (!menu.contains(e.target) && e.target !== btn) closeAvatarMenu(); };
  setTimeout(() => document.addEventListener('click', _avatarMenuOutside, true), 0);
}

// Rename in place: the bar's name turns into an input; Enter saves, Escape cancels.
function _renameCollectionInline(bar) {
  const id = bar?.dataset.colId;
  const col = Collections.getAll().find(c => c.id === id);
  if (!col) return;
  const nameEl = bar.querySelector('.saved-colbar-name');
  nameEl.innerHTML = `<input type="text" class="input" maxlength="60" value="${escapeHtml(col.name)}" aria-label="${escapeHtml(t('col.rename'))}">`;
  const input = nameEl.querySelector('input');
  input.focus(); input.select();
  let done = false;
  const finish = save => {
    if (done) return;
    done = true;
    if (save && input.value.trim() && input.value.trim() !== col.name) Collections.rename(id, input.value.trim());
    renderSaved();
  };
  input.addEventListener('keydown', e => { if (e.key === 'Enter') finish(true); if (e.key === 'Escape') finish(false); });
  input.addEventListener('blur', () => finish(true));
}

// "+ New collection": a name, then straight to choosing its flats.
function _savedNewCollection(name) {
  const kind = _savedView().kind;
  const col = Collections.create(name, kind);
  _setSavedView(kind, col.id);
  showToast(t('col.created', { name: col.name }));
  if (_favIds(kind).length) _savedPickStart(col.id); else renderSaved();
}

// ── Choosing flats for a collection ──
function _savedPickStart(colId) {
  const col = Collections.getAll().find(c => c.id === colId);
  if (!col) return;
  _savedPick = { colId, ids: new Set(col.listing_ids.map(String)) };
  document.getElementById('savedGrid').innerHTML = '';
  renderSaved();
  window.scrollTo({ top: 0 });
}

function _savedPickEnd() {
  _savedPick = null;
  document.getElementById('savedView')?.classList.remove('is-picking');
  _renderSavedPickBars(null);
}

function _renderSavedPickBars(col) {
  const head = document.getElementById('savedPickHead'), bar = document.getElementById('savedPickBar');
  if (!head || !bar) return;
  head.hidden = bar.hidden = !col;
  if (!col) return;
  const esc = escapeHtml;
  head.innerHTML = `<h2 class="section-title">${esc(t('col.pick_for', { name: col.name }))}</h2>
    <p class="section-hint">${esc(t('col.pick_hint'))}</p>`;
  bar.innerHTML = `<span class="saved-pick-n" id="savedPickN"></span>
    <button type="button" class="btn btn-secondary" data-pick-cancel>${esc(t('alert_modal.cancel'))}</button>
    <button type="button" class="btn btn-primary" data-pick-save>${esc(t('col.pick_save'))}</button>`;
  _savedPickCount();
}

function _savedPickCount() {
  const el = document.getElementById('savedPickN');
  if (!el || !_savedPick) return;
  const n = document.querySelectorAll('#savedGrid .card.is-picked').length;
  el.textContent = t(n === 1 ? 'col.pick_one' : 'col.pick_n', { n: fmt(n) });
}

function _savedPickToggle(card) {
  if (!_savedPick) return;
  const own = (card.dataset.savedIds || card.dataset.listingId || '').split(',').filter(Boolean);
  const on = !card.classList.contains('is-picked');
  if (on) {
    if (_savedPick.ids.size >= 100) { showToast(t('col.full')); return; }
    _savedPick.ids.add(own[0]);
  } else own.forEach(i => _savedPick.ids.delete(i));
  card.classList.toggle('is-picked', on);
  card.setAttribute('aria-checked', String(on));
  _savedPickCount();
}

function _savedPickSave() {
  const col = _savedPick && Collections.getAll().find(c => c.id === _savedPick.colId);
  if (!col) { _savedPickEnd(); renderSaved(); return; }
  const sel = _savedPick.ids, kind = col.kind || 'rent';
  const keep = col.listing_ids.map(String).filter(i => sel.has(i));
  Collections.setListings(col.id, [...keep, ...[...sel].filter(i => !keep.includes(i))]);
  _addFavs([...sel], kind);
  _savedPickEnd();
  showToast(t('col.pick_saved', { name: col.name }));
  renderSaved();
}

// "Remove them": the saved flats no longer online leave Saved (or, on a
// collection chip, that collection) — with an undo.
function _savedRemoveGone() {
  const gone = _savedGone.slice();
  if (!gone.length) return;
  const v = _savedView(), kind = v.kind, colId = v.col[kind];
  const favsBefore = _favIds(kind), colsBefore = JSON.parse(JSON.stringify(Collections.getAll()));
  const col = colId && Collections.getAll().find(c => c.id === colId);
  if (col) Collections.setListings(col.id, col.listing_ids.filter(i => !gone.includes(String(i))));
  else {
    Storage.set(_favKey(kind), favsBefore.filter(i => !gone.includes(i)));
    gone.forEach(i => Collections.removeEverywhere(i, kind));
    updateFavBadge();
  }
  renderSaved();
  showActionToast(t('saved.gone_removed', { n: fmt(gone.length) }), t('fav.undo'), () => {
    Storage.set(_favKey(kind), favsBefore);
    Storage.set('collections', colsBefore);
    updateFavBadge();
    if (activeTab === 'saved') renderSaved();
  });
}

// Un-saving from a card or Details: say which collections the flat left, and undo.
function _savedUnsaveToast(id, kind, touched) {
  if (!touched.length && activeTab !== 'saved') return;
  const msg = touched.length > 1 ? t('fav.removed_cols', { n: fmt(touched.length) })
    : touched.length ? t('fav.removed_col_one', { name: touched[0].name }) : t('fav.removed');
  showActionToast(msg, t('fav.undo'), () => {
    _addFavs([id], kind);
    if (touched.length) Collections.restore(id, touched.map(c => c.id));
    if (activeTab === 'saved') renderSaved();
  });
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

function deleteCollectionConfirm(collectionId) {
  const col = Collections.getAll().find(c => c.id === collectionId);
  if (!col) return;
  if (confirm(t('col.delete_confirm', { name: col.name }))) {
    Collections.delete(collectionId);
    _setSavedView(null, '');
    renderSaved();
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
  // A sale collection's link opens For sale, filtered (_saleMatches).
  window.sharedCollection = { ...decoded, _ids: new Set(decoded.listing_ids) };
  if (decoded.kind === 'sale') setTimeout(() => switchTab('sales'), 0);
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
  if (ALL_SALE.length && typeof renderSales === 'function') renderSales();
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
      <div class="banner-title">${escapeHtml(t('col.shared'))} <strong>${escapeHtml(sc.name)}</strong></div>
      <div class="banner-sub">${escapeHtml(t(visibleCount === 1 ? 'saved.n_one' : 'saved.n', { n: fmt(visibleCount) }))}</div>
      ${missingNote}
    </div>
    <button type="button" class="btn btn-secondary" onclick="exitSharedCollection()">${escapeHtml(t('col.show_all'))}</button>`;

  // Insert above whichever grid is currently visible
  const target = document.querySelector('#rentalsView:not([hidden]) .grid, #salesView:not([hidden]) .grid');
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
