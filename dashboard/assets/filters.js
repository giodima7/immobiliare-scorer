// Lume dashboard — rental filters beyond the basic inputs: must-haves (with
// yes / not-yet-known counts), condition, discount and contract filters, and
// the active-filter chips under the filter bar.
// Plain script (no bundler); shares globals with the other assets/*.js files.
// Declarations only: code that runs at start-up lives in init.js, which loads
// last, so any function here may call any other.

// ── Must-haves ────────────────────────────────────────────────────────────────
// value(l) → true / false / null (not known yet). Coverage grows as the
// scanners re-read search pages and listing pages (listing_details.py), so
// "include not-yet-known" lets a strict filter stay usable meanwhile.
// "lift" is the long-standing #rElevator checkbox (map, drawer and saved
// filters already read it); the others live in rentMust.
// Parquet, a liveable living room, a real bedroom (2026-10-31): what the
// photo reading saw (photo_features, photo_tags, photo_flags), the parquet
// also from the description (text_flags, scoring.stamp_text_flags).
function _mustParquet(l) {
  const floors = l.photo_features?.floors;
  if (floors === 'parquet' || (l.text_flags || []).includes('parquet')) return true;
  return floors ? false : null;
}
function _mustLiving(l) {
  const tags = l.photo_tags || [], sep = l.photo_features?.separate_living;
  if (tags.includes('small_living') || tags.includes('student_layout') || sep === false) return false;
  return tags.includes('big_living') || sep === true ? true : null;
}
function _mustBedroom(l) {
  if ((l.photo_flags || []).includes('no_bedroom') || l.text_says_studio || l.bedrooms === 0) return false;
  return l.bedrooms >= 1 ? true : null;
}
// A casa di ringhiera — the description (text_flags) or the photo note says so.
function isRinghiera(l) {
  return (l.text_flags || []).includes('ringhiera')
      || /\b(?:di|a) ringhiera|ringhiera milanese|ballatoio/i.test(l.photo_note_it || '');
}

const RENT_MUSTS = [
  { key: 'parquet',   icon: 'list-details',    value: _mustParquet },
  { key: 'living',    icon: 'maximize',        value: _mustLiving },
  { key: 'bedroom',   icon: 'bed',             value: _mustBedroom },
  { key: 'furnished', icon: 'armchair',        value: l => l.furnished ?? null },
  { key: 'balcony',   icon: 'plant',           value: l => l.has_balcony ?? null },
  { key: 'parking',   icon: 'car',             value: l => l.has_parking ?? null },
  { key: 'lift',      icon: 'elevator',        value: l => l.elevator ?? null },
  { key: 'heating',   icon: 'flame',           value: l => l.heating_type && l.heating_type !== 'unknown' ? l.heating_type === 'autonomous' : null },
  { key: 'baths',     icon: 'bath',            value: l => l.bathrooms != null ? l.bathrooms >= 2 : null },
  { key: 'renovated', icon: 'sparkles',        value: l => { const g = conditionGroup(l.condition); return g === 'unknown' ? null : g === 'premium'; } },
  { key: 'safe',      icon: 'alert-triangle',  value: l => !l.price_suspect },
  { key: 'no_ringhiera', icon: 'building',     value: l => !isRinghiera(l) },
];
// Exclusions, shown as checkboxes under the tiles rather than as tiles.
const MUST_CHECKS = ['safe', 'no_ringhiera'];
// For sale: no "furnished", no price-suspect flag.
const SALE_MUST_KEYS = ['parquet', 'living', 'bedroom', 'balcony', 'parking', 'lift', 'heating', 'baths', 'renovated', 'no_ringhiera'];
const RENT_MUST_BY_KEY = Object.fromEntries(RENT_MUSTS.map(m => [m.key, m]));

let rentMust = new Set();          // active must-have keys, except 'lift' (#rElevator)
let rentIncludeUnknown = false;    // must-haves also pass listings where it's not known yet

function rentMustActive() {
  const keys = [...rentMust];
  if (document.getElementById('rElevator')?.checked) keys.push('lift');
  return keys;
}

function toggleRentMust(key) {
  if (key === 'lift') {
    const el = document.getElementById('rElevator');
    if (el) el.checked = !el.checked;
  } else if (rentMust.has(key)) {
    rentMust.delete(key);
  } else {
    rentMust.add(key);
  }
  renderRentals();
  saveFilters();
}

// Same grouping as comps_engine.condition_group (Python).
function conditionGroup(raw) {
  // Immobiliare's sale pages say "da_ristrutturare": read as "renovated" until
  // 2026-10-31 (132 sales).
  const c = String(raw || '').toLowerCase().replace(/_/g, ' ').trim();
  if (!c || c.includes('idealista')) return 'unknown';
  if ((c.includes('da ') && c.includes('ristruttur')) || c.includes('fatiscent')) return 'needs_work';
  if (['ristruttur', 'ottim', 'nuovo', 'costruz'].some(k => c.includes(k))) return 'premium';
  if (['buon', 'abitabil', 'normale'].some(k => c.includes(k))) return 'standard';
  return 'unknown';
}

// True when the listing passes the must-haves and the condition / discount /
// contract filters in f (called from applyRentFilters).
function passesExtraFilters(l, f) {
  for (const k of f.must || []) {
    if (k === 'lift') continue;                       // applyRentFilters' elevator check
    const v = RENT_MUST_BY_KEY[k]?.value(l);
    if (v === true || (v == null && f.includeUnknown)) continue;
    return false;
  }
  if (f.condition) {
    const g = conditionGroup(l.condition);
    if (f.condition === 'premium' && g !== 'premium' && !(g === 'unknown' && f.includeUnknown)) return false;
    if (f.condition === 'not_needs_work' && g === 'needs_work') return false;
  }
  if (f.minDiscount && !(l.comps_delta_pct != null && l.comps_delta_pct <= -f.minDiscount)) return false;
  if (f.contract && l.contract_type !== f.contract
      && !(l.contract_type == null && f.includeUnknown)) return false;
  return true;
}

// Yes / not-yet-known counts per must-have over what the other filters keep.
function mustCounts(base, musts = RENT_MUSTS) {
  const counts = {};
  for (const m of musts) {
    let yes = 0, unknown = 0;
    for (const l of base) {
      const v = m.value(l);
      if (v === true) yes++;
      else if (v == null) unknown++;
    }
    counts[m.key] = { yes, unknown };
  }
  return counts;
}
function rentMustCounts(f) {
  return mustCounts(applyRentFilters(ALL_RENT, { ...f, must: [], elevator: false }));
}

// Must-have tiles: name on top, "5.768 · +5.798 not known yet" below.
// 'safe' (hide suspicious prices) is an exclusion, so it's a separate
// checkbox (.hide-suspect), not a tile.
function renderMustGrid(container, counts, active = new Set(rentMustActive()), musts = RENT_MUSTS) {
  if (!container) return;
  container.innerHTML = musts.filter(m => !MUST_CHECKS.includes(m.key)).map(m => {
    const c = counts[m.key] || { yes: 0, unknown: 0 };
    const unk = c.unknown > 0 ? ` · ${escapeHtml(t('must.unknown_n', { n: fmt(c.unknown) }))}` : '';
    return `<button type="button" class="must-chip${active.has(m.key) ? ' on' : ''}" data-must="${m.key}"
              aria-pressed="${active.has(m.key)}">
              <span class="must-label">${icon(m.icon)}${escapeHtml(t('must.' + m.key))}</span>
              <span class="must-count"><strong>${fmt(c.yes)}</strong>${unk}</span>
            </button>`;
  }).join('');
  if (musts === RENT_MUSTS) {
    document.querySelectorAll('.hide-suspect').forEach(cb => { cb.checked = active.has('safe'); });
    document.querySelectorAll('.hide-ringhiera:not(#sHideRinghiera)').forEach(cb => { cb.checked = active.has('no_ringhiera'); });
  }
}

// ── Active-filter chips ───────────────────────────────────────────────────────
// One removable chip per active filter, so nothing filters out of sight.
function rentActiveFilterChips(f) {
  const chips = [];
  const add = (key, label) => chips.push({ key, label });
  if (f.myZones) add('myZones', t('mz.label'));
  if (f.areas && f.areas.length) add('areas', f.areas.length > 2 ? `${f.areas.slice(0, 2).join(', ')} +${f.areas.length - 2}` : f.areas.join(', '));
  if (f.maxRent)   add('maxRent', t('chip.max_rent', { n: fmt(f.maxRent) }));
  if (f.minRooms)  add('minRooms', t('chip.min_rooms', { n: f.minRooms }));
  if (f.minSqm)    add('minSqm', t('chip.min_sqm', { n: f.minSqm }));
  if (f.gemFilter) add('gemFilter', t({ hidden: 'gem.hidden', good: 'gem.good', fair: 'gem.fair' }[f.gemFilter] || 'gem.good'));
  for (const k of f.must || []) add('must:' + k, t('must.' + k));
  if (f.includeUnknown) add('includeUnknown', t('chip.include_unknown'));
  if (f.condition)   add('condition', t('cond.' + f.condition));
  if (f.minDiscount) add('minDiscount', t('chip.min_discount', { n: f.minDiscount }));
  if (f.contract)    add('contract', f.contract);
  if (f.maxMetro)    add('maxMetro', t('chip.max_metro', { n: fmt(f.maxMetro) }));
  if (f.minFloor > -999) add('minFloor', t('chip.min_floor', { n: f.minFloor }));
  if (f.source)      add('source', f.source === 'private' ? t('filter.private') : siteName(f.source));
  if (f.minEnergy)   add('minEnergy', t('chip.min_energy', { v: f.minEnergy }));
  if (f.minPhoto)    add('minPhoto', t('chip.min_photo', { n: f.minPhoto }));
  if (f.minScore)    add('minScore', t('chip.min_score', { n: f.minScore }));
  if (f.fListed)     add('fListed', f.fListed === 'today' ? t('chip.listed_today') : t('chip.listed_days', { n: f.fListed }));
  if (f.idQuery)     add('idQuery', 'ID ' + f.idQuery);
  if (document.getElementById('rShowStale')?.checked) add('showStale', t('filter.show_expired'));
  return chips;
}

function renderRentFilterExtras(f, shown) {
  ['rSortSelect', 'dSort'].forEach(id => {
    const sel = document.getElementById(id);
    if (sel && typeof rentSortKey !== 'undefined' && sel.value !== rentSortKey) sel.value = rentSortKey;
  });
  document.querySelectorAll('.seg .seg-btn').forEach(b => b.setAttribute('aria-pressed', String(b.classList.contains('active'))));
  const mz = document.getElementById('rMyZones');
  if (mz) { mz.setAttribute('aria-pressed', String(!!f.myZones)); mz.classList.toggle('on', !!f.myZones); }
  const dmz = document.getElementById('dMyZones'); if (dmz) dmz.checked = !!f.myZones;
  const counts = rentMustCounts(f);
  renderMustGrid(document.getElementById('rMustGrid'), counts);
  renderMustGrid(document.getElementById('dMustGrid'), counts);
  const nMust = rentMustActive().length;
  const mustBadge = document.getElementById('rMustBadge');
  if (mustBadge) { mustBadge.textContent = nMust || ''; mustBadge.hidden = !nMust; }
  document.getElementById('rMustBtn')?.classList.toggle('on', nMust > 0);

  const chips = rentActiveFilterChips(f);
  const moreKeys = ['condition', 'minDiscount', 'contract', 'maxMetro', 'minFloor', 'source',
                    'minEnergy', 'minScore', 'fListed', 'idQuery', 'showStale'];
  const nMore = chips.filter(c => moreKeys.includes(c.key)).length;
  const moreBadge = document.getElementById('rMoreBadge');
  if (moreBadge) { moreBadge.textContent = nMore || ''; moreBadge.hidden = !nMore; }
  document.getElementById('rMoreBtn')?.classList.toggle('on', nMore > 0);

  // Phone header: how many filters are on.
  const mBadge = document.getElementById('mobileFilterBadge');
  if (mBadge) { mBadge.textContent = chips.length || ''; mBadge.hidden = !chips.length; }

  // The phone drawer's "Show N results" (must-haves apply immediately).
  const showBtn = document.getElementById('drawerShowBtn');
  if (showBtn && document.getElementById('mobileFilterDrawer')?.classList.contains('is-open')) {
    showBtn.textContent = t('drawer.show', { n: fmt(shown) });
  }

  const box = document.getElementById('rActiveChips');
  if (!box) return;
  // Daily e-mail alerts run from the local dashboard only.
  const alertBtn = (typeof IS_LIVE !== 'undefined' && IS_LIVE)
    ? `<button type="button" class="btn btn-ghost btn-sm" data-save-alert>${icon('bell')}<span>${escapeHtml(t('filter.save_alert'))}</span></button>` : '';
  box.innerHTML = chips.map(c =>
    `<button type="button" class="fchip" data-clear="${escapeHtml(c.key)}" aria-label="${escapeHtml(t('chip.remove') + ': ' + c.label)}">
       ${escapeHtml(c.label)}${icon('x')}
     </button>`).join('')
    + (chips.length ? `<button type="button" class="btn btn-ghost btn-sm" data-clear="all">${escapeHtml(t('filter.clear_filters'))}</button>` : '')
    // No count before the first page is in ("0 flats" while loading).
    + (typeof _rentLoading !== 'undefined' && _rentLoading && !ALL_RENT.length ? ''
      : `<span class="fchips-count"><strong>${fmt(shown)}</strong> ${escapeHtml(t(shown === 1 ? 'chip.flat' : 'chip.flats'))}`
        + (typeof _rentLoadingMore !== 'undefined' && _rentLoadingMore ? ` ${escapeHtml(t('chip.so_far'))}` : '')
        + `</span>`)
    + `<button type="button" class="btn btn-ghost btn-sm" data-ss-menu="rent">${icon('bookmark')}<span>${escapeHtml(t('ss.button'))}${(n => n ? ' · ' + n : '')(savedSearches('rent').length)}</span></button>`
    + alertBtn;
}

// Remove one filter (chip ×) — or all of them.
function clearRentFilter(key) {
  const el = id => document.getElementById(id);
  if (key === 'all') { el('rBtnReset')?.click(); return; }
  if (key.startsWith('must:')) { toggleRentMust(key.slice(5)); return; }
  switch (key) {
    case 'areas':
      if (typeof filterAreaMsd !== 'undefined' && filterAreaMsd) filterAreaMsd.clear();
      rentSelectedAreas = [];
      break;
    case 'myZones':  el('rMyZones')?.setAttribute('aria-pressed', 'false'); break;
    case 'maxRent':  el('rMaxRent').value = ''; break;
    case 'minRooms': el('rMinRooms').value = ''; break;
    case 'minSqm':   el('rMinSqm').value = ''; break;
    case 'gemFilter':
      document.querySelectorAll('#rGemFilter .gem-tab, #dGemFilter .gem-tab').forEach(b => b.classList.toggle('active', b.dataset.gem === ''));
      break;
    case 'includeUnknown': rentIncludeUnknown = false; if (el('rIncludeUnknown')) el('rIncludeUnknown').checked = false; break;
    case 'condition':   el('rCondition').value = ''; break;
    case 'minDiscount': el('rMinDiscount').value = ''; break;
    case 'contract':    el('rContract').value = ''; break;
    case 'maxMetro':    el('rMaxMetro').value = ''; break;
    case 'minFloor':    el('rMinFloor').value = ''; break;
    case 'source':
      document.querySelectorAll('#rSourceFilter .sort-tab').forEach(b => b.classList.toggle('active', (b.dataset.rsource || '') === ''));
      break;
    case 'minEnergy':   el('rMinEnergy').value = ''; break;
    case 'minPhoto':    el('rMinPhoto').value = ''; break;
    case 'minScore':    syncScoreFromSlider(0); el('rScore').value = 0; break;
    case 'fListed':     el('rListed').value = ''; break;
    case 'idQuery':     el('rIdSearch').value = ''; break;
    case 'showStale':   el('rShowStale').checked = false; if (IS_LIVE) loadRentals(); break;
  }
  renderRentals();
  saveFilters();
}

// The filter bar's Must-haves / More filters panels: one open at a time.
function toggleRentPanel(which) {
  const ids = { must: 'rMustPanel', more: 'rMorePanel' };
  for (const [k, id] of Object.entries(ids)) {
    const p = document.getElementById(id);
    if (!p) continue;
    const open = k === which ? p.hidden : false;
    p.hidden = !open;
    document.getElementById(k === 'must' ? 'rMustBtn' : 'rMoreBtn')?.setAttribute('aria-expanded', String(open));
  }
}

// ── My zones (dashboard/focus_areas.json) ─────────────────────────────────────
// The zones the user drew street by street — circles and polygons, the same
// file that makes the scanners and the photo reading go there first. One
// switch in Rentals, For sale (and so the Map) and For you; by position only,
// not by the site's neighbourhood names (2026-10-31: "Cadore up to Piazzale
// Libia, not as far as Lodi TIBB" is no neighbourhood).
let MY_ZONES = null;               // [{name, center, radius_m, polygon}] once loaded

async function loadMyZones() {
  if (MY_ZONES) return MY_ZONES;
  try {
    const r = await fetch('focus_areas.json', { cache: 'no-cache' });
    MY_ZONES = r.ok ? ((await r.json()).areas || []) : [];
  } catch { MY_ZONES = []; }
  return MY_ZONES;
}

function _inPolygon(lat, lng, poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [y1, x1] = poly[i], [y2, x2] = poly[j];
    if ((y1 > lat) !== (y2 > lat) && lng < (x2 - x1) * (lat - y1) / (y2 - y1) + x1) inside = !inside;
  }
  return inside;
}

// In one of the zones. Before the file has loaded, everything passes.
function inMyZones(l) {
  if (!MY_ZONES) return true;
  const lat = +l.latitude, lng = +l.longitude;
  if (!lat || !lng) return false;
  return MY_ZONES.some(z => (z.polygon && _inPolygon(lat, lng, z.polygon))
    || (z.center && z.radius_m && _km([lat, lng], z.center) * 1000 <= z.radius_m));
}

// ── Saved searches ────────────────────────────────────────────────────────────
// A name and the filters of Rentals or For sale, kept like the saved
// listings (this browser, the local copy, the account — so the iPhone and
// the Mac share them). {id, name, kind: 'rent'|'sale', f, at}.
function savedSearches(kind) {
  return (Storage.get('savedSearches', []) || []).filter(s => !kind || s.kind === kind);
}

function saveSearch(kind, name) {
  const f = kind === 'sale'
    ? (({ down, rate, years, ...rest }) => rest)(saleFilters())      // the mortgage terms are settings
    : rentFilterState();
  const all = Storage.get('savedSearches', []) || [];
  const s = { id: 'ss_' + Math.random().toString(36).slice(2, 10), name: name.slice(0, 60), kind, f, at: new Date().toISOString() };
  Storage.set('savedSearches', [s, ...all]);
  return s;
}

function deleteSearch(id) {
  Storage.set('savedSearches', (Storage.get('savedSearches', []) || []).filter(s => s.id !== id));
}

function applySearch(id) {
  const s = savedSearches().find(x => x.id === id);
  if (!s) return;
  if (s.kind === 'sale') {
    const cur = saleFilters();
    Storage.set('saleFilters', { ...SALE_DEFAULTS, ...s.f, down: cur.down, rate: cur.rate, years: cur.years });
    if (typeof _saleAreaMsd !== 'undefined' && _saleAreaMsd) _saleAreaMsd.setSelected(s.f.areas || []);
    renderSales();
  } else {
    document.getElementById('rBtnReset')?.click();                  // start clean, then the saved state
    rentMust = new Set(); rentIncludeUnknown = false;
    applyRentFilterState(s.f);
    renderRentals();
    saveFilters();
  }
  showToast(t('ss.applied', { name: s.name }));
}

// The menu under "Saved searches": apply, delete, save the current filters.
function openSavedSearchMenu(btn, kind) {
  closeAvatarMenu();
  const esc = escapeHtml, rect = btn.getBoundingClientRect();
  const list = savedSearches(kind);
  const menu = document.createElement('div');
  menu.className = 'avatar-menu ss-menu';
  menu.id = 'active-avatar-menu';
  menu.setAttribute('role', 'menu');
  menu.innerHTML = `<div class="avatar-menu-header">${esc(t('ss.title'))}</div>
    ${list.length ? list.map(s => `<div class="ss-row">
        <button type="button" class="avatar-menu-item" role="menuitem" data-ss-apply="${esc(s.id)}">${icon('bookmark')} <span class="ss-name">${esc(s.name)}</span></button>
        <button type="button" class="btn btn-ghost btn-sm btn-icon" data-ss-delete="${esc(s.id)}" aria-label="${esc(t('ss.delete', { name: s.name }))}" title="${esc(t('ss.delete', { name: s.name }))}">${icon('trash')}</button>
      </div>`).join('') : `<p class="ss-empty">${esc(t('ss.none'))}</p>`}
    <form class="ss-new" autocomplete="off">
      <input type="text" class="input" maxlength="60" placeholder="${esc(t('ss.name_ph'))}" aria-label="${esc(t('ss.name_ph'))}">
      <button type="submit" class="btn btn-primary btn-sm">${esc(t('ss.save'))}</button>
    </form>`;
  document.body.appendChild(menu);
  const w = Math.min(300, window.innerWidth - 16);
  menu.style.width = `${w}px`;
  menu.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - w - 8))}px`;
  menu.style.top = `${rect.bottom + 6}px`;
  menu.addEventListener('click', e => {
    const ap = e.target.closest('[data-ss-apply]'), del = e.target.closest('[data-ss-delete]');
    if (ap) { closeAvatarMenu(); applySearch(ap.dataset.ssApply); }
    if (del) {
      const s = savedSearches().find(x => x.id === del.dataset.ssDelete);
      deleteSearch(del.dataset.ssDelete);
      closeAvatarMenu();
      if (s) showActionToast(t('ss.deleted', { name: s.name }), t('fav.undo'),
        () => Storage.set('savedSearches', [s, ...(Storage.get('savedSearches', []) || [])]));
    }
  });
  menu.querySelector('form').addEventListener('submit', e => {
    e.preventDefault();
    const name = e.target.querySelector('input').value.trim();
    if (!name) { e.target.querySelector('input').focus(); return; }
    saveSearch(kind, name);
    closeAvatarMenu();
    showToast(t('ss.saved', { name }));
  });
  menu.addEventListener('keydown', e => { if (e.key === 'Escape') { closeAvatarMenu(); btn.focus(); } });
  _avatarMenuOutside = (e) => { if (!menu.contains(e.target) && e.target !== btn) closeAvatarMenu(); };
  setTimeout(() => document.addEventListener('click', _avatarMenuOutside, true), 0);
}
