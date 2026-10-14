// Lume dashboard — app shell: city config, shared state, tooltip, formatting helpers, favourites, local-API detection, start-up, sticky filter bar, ID search, tabs.
// Plain script (no bundler); shares globals with the other assets/*.js files.
// Declarations only: code that runs at start-up lives in init.js, which loads
// last, so any function here may call any other.

// ─── City ──────────────────────────────────────────────────────────────────
// Milan only. The multi-city picker is parked in full.html. currentCity
// stays 'milano' and drives the city= filter on every Supabase listings
// fetch + zone-filter rebuild + metro-filter visibility.
let currentCity = 'milano';

// Loaded async at page-init from the cities Supabase table. Fallback list
// covers the seeded 4 cities so the dropdown still works when Supabase is
// unreachable (the cities table is small + public-read; failures are rare).
let availableCities = [
  { code: 'milano',       comune_code: 'F205', display_name_en: 'Milan',         display_name_it: 'Milano',       has_metro: true,  center_lat: 45.4642, center_lng: 9.1900,  default_zoom: 13 },
  { code: 'roma',         comune_code: 'H501', display_name_en: 'Rome',          display_name_it: 'Roma',         has_metro: true,  center_lat: 41.9028, center_lng: 12.4964, default_zoom: 12 },
  { code: 'napoli',       comune_code: 'F839', display_name_en: 'Naples',        display_name_it: 'Napoli',       has_metro: true,  center_lat: 40.8518, center_lng: 14.2681, default_zoom: 13 },
  { code: 'la_maddalena', comune_code: 'E425', display_name_en: 'La Maddalena',  display_name_it: 'La Maddalena', has_metro: false, center_lat: 41.2133, center_lng: 9.4063,  default_zoom: 14 },
];

async function loadAvailableCities() {
  if (typeof SUPABASE_URL === 'undefined' || typeof SUPABASE_ANON_KEY === 'undefined') return;
  try {
    const resp = await fetch(
      `${SUPABASE_URL}/rest/v1/cities?active=eq.true&order=display_name_en`,
      { headers: { 'apikey': SUPABASE_ANON_KEY, 'Authorization': `Bearer ${SUPABASE_ANON_KEY}` } }
    );
    if (resp.ok) {
      const rows = await resp.json();
      if (Array.isArray(rows) && rows.length) availableCities = rows;
    }
  } catch (_) { /* keep fallback */ }
}

function _currentCityConfig() {
  return availableCities.find(c => c.code === currentCity) || availableCities[0];
}

function _cityDisplayName(cfg) {
  if (!cfg) return '';
  return currentLang === 'it' ? (cfg.display_name_it || cfg.code) : (cfg.display_name_en || cfg.code);
}

function updateCityToggleUI() {
  updateMetaCity();
}

// Refresh the meta line's city slot. Standalone so language toggles +
// city changes + post-load count updates can all reuse it. The actual
// listings count is owned by loadRentals' meta-writer; this helper
// keeps the city name fresh without overwriting the count when one
// is already there.
function updateMetaCity() {
  const meta = document.getElementById('meta');
  if (!meta) return;
  const cityName = _cityDisplayName(_currentCityConfig());
  // Only rewrite when the existing text already includes a count
  // (header.listings_count was applied). The "loading…" placeholder
  // and Supabase-error states are left alone.
  const m = meta.textContent.match(/^(\d[\d.,\s]*)\s+(listings|annunci)\b/);
  if (!m) return;
  meta.textContent = t('header.listings_count', {
    n:    parseInt(m[1].replace(/[^\d]/g, ''), 10),
    city: cityName,
    date: fmtDate(new Date()),
  });
}

// Hide controls that don't apply to the active city. Currently:
//   • cities with has_metro === false (e.g. La Maddalena) hide both the
//     metro filter group and its preceding separator, and reset the value
//     so an inherited "≤500m" doesn't silently filter out every listing.
function updateFiltersForCurrentCity() {
  const cfg     = _currentCityConfig();
  const noMetro = !!(cfg && cfg.has_metro === false);
  ['rFilterMetroSection', 'rFilterMetroSep'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = noMetro ? 'none' : '';
  });
  if (noMetro) {
    ['rMaxMetro'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.value = '';
    });
  }
}

// ── Per-city filter rebuilds ────────────────────────────────────────────────
// Zone + area dropdowns are derived from the loaded listings, so they have
// to be rebuilt every time the city changes (or finishes loading). The
// heavy lifting already lives in _populateOmiZoneFilter / buildMultiSelect
// — these wrappers are the single entry-point onDataLoaded() calls.
function rebuildZoneFilter() {
  if (typeof _populateOmiZoneFilter === 'function' && Array.isArray(ALL_RENT)) {
    _populateOmiZoneFilter(ALL_RENT);
  }
}

function rebuildAreaFilter() {
  // Area MSDs lazy-build on first use; rebuilding here keeps their
  // option lists scoped to the active city. The MSDs read from ALL_RENT
  // on their next render call — loadRentals() calls renderRentals() right
  // after onDataLoaded(), so we deliberately do NOT trigger another render
  // here. (An earlier version did, which fired renderRentals BEFORE
  // loadRentals had finished its post-data setup and left the grid
  // blank when the inner call threw.)
}

// Single entry-point called after every dataset load (loadRentals) so
// per-city UI stays in sync without each caller having to remember every
// step.
function onDataLoaded() {
  rebuildZoneFilter();
  rebuildAreaFilter();
  updateFiltersForCurrentCity();
}

// Map default view for the active city. Returns [lat, lng, zoom] using
// values from the cities Supabase row when available, otherwise the
// fallback constants baked into availableCities. Used by _mapInit() for
// the very first paint and again whenever the map needs to "reset" with
// an empty dataset.
function getMapDefaultView() {
  const cfg = _currentCityConfig() || {};
  const lat  = Number.isFinite(cfg.center_lat) ? cfg.center_lat : 45.4642;
  const lng  = Number.isFinite(cfg.center_lng) ? cfg.center_lng : 9.1900;
  const zoom = Number.isFinite(cfg.default_zoom) ? cfg.default_zoom : 12;
  return [lat, lng, zoom];
}

// Locale-aware date — Italian: "15 mag 2026", English: "15 May 2026".
function fmtDate(dateLike, opts) {
  if (!dateLike) return '';
  const d = (dateLike instanceof Date) ? dateLike : new Date(dateLike);
  if (Number.isNaN(d.getTime())) return '';
  const locale = currentLang === 'it' ? 'it-IT' : 'en-GB';
  return d.toLocaleDateString(locale, opts || { day: 'numeric', month: 'short', year: 'numeric' });
}

// Locale-aware number — Italian: "1.234,56", English: "1,234.56".
function fmtNum(n, decimals) {
  if (n == null || Number.isNaN(Number(n))) return '–';
  const locale = currentLang === 'it' ? 'it-IT' : 'en-GB';
  const d = decimals == null ? 0 : decimals;
  return Number(n).toLocaleString(locale, {
    minimumFractionDigits: d, maximumFractionDigits: d,
  });
}

// ── Global state ──────────────────────────────────────────────────────────────
let scoreSettings = {   // loaded from /api/scoring-settings, used for badge data-attrs
  // Same as scoring.py's _DEFAULT_SETTINGS; replaced by the real values from
  // /api/scoring-settings at startup when the local API is up.
  w_price: 0.45, w_property: 0.25, w_location: 0.20, w_penalty: 0.10,
  penalty_dom_stale: 60, penalty_dom_warn: 30,
  radii: [500, 800, 1200], min_comps: 5,
};

// ── Global tooltip (item 4 + 5: fixed-position, no clipping) ─────────────────
const _gtEl = document.getElementById('globalTooltip');
let _gtHideTimer = null;
let _gtState = { anchorEl: null, mode: 'anchor', x: 0, y: 0 };

function _gtShow(html, anchorEl, mode = 'anchor', point = null) {
  clearTimeout(_gtHideTimer);
  _gtEl.innerHTML = html;
  _gtEl.style.display = 'block';
  _gtState = {
    anchorEl,
    mode,
    x: point?.clientX ?? 0,
    y: point?.clientY ?? 0,
  };
  _gtPosition();
}
function _gtHide() {
  _gtHideTimer = setTimeout(() => { _gtEl.style.display = 'none'; }, 80);
}
function _gtPosition(point = null) {
  if (_gtEl.style.display === 'none') return;
  const tw  = _gtEl.offsetWidth  || 220;
  const th  = _gtEl.offsetHeight || 140;
  const vw  = window.innerWidth;
  const vh  = window.innerHeight;
  let top;
  let left;

  if (_gtState.mode === 'cursor' && (_gtState.anchorEl || point)) {
    const p = point || { clientX: _gtState.x, clientY: _gtState.y };
    _gtState.x = p.clientX;
    _gtState.y = p.clientY;
    top = p.clientY + 18;
    left = p.clientX + 18;
    if (left + tw > vw - 8) left = p.clientX - tw - 18;
    if (top + th > vh - 8) top = p.clientY - th - 18;
  } else if (_gtState.anchorEl) {
    const r = _gtState.anchorEl.getBoundingClientRect();
    top  = r.bottom + 8;
    if (top + th > vh - 8) top = r.top - th - 8;
    left = r.left + r.width / 2 - tw / 2;
  } else {
    return;
  }

  top = Math.max(8, Math.min(top, vh - th - 8));
  left = Math.max(8, Math.min(left, vw - tw - 8));
  _gtEl.style.top  = top  + 'px';
  _gtEl.style.left = left + 'px';
}

// Build the Score-breakdown HTML from a wrapper element's data-* attrs.
// Shared between hover (mouseenter) and click (pin) paths.
function _scoreBreakdownHtml(bdWrap) {
  const d = bdWrap.dataset;
  const _fmtSub = (v, w) => v !== '' && v != null
    ? `<span style="font-weight:700">${v}</span> <span style="color:var(--muted);font-size:10px">×${w} = ${Math.round(Number(v)*w)}</span>`
    : `<span style="color:var(--muted)">– (neutral 50×${w} = ${Math.round(50*w)})</span>`;
  const wP  = Number(d.wPrice || 0.45);
  const wPr = Number(d.wProp  || 0.25);
  const wL  = Number(d.wLoc   || 0.20);
  const wPn = Number(d.wPen   || 0.10);
  return `
    <div class="gt-title">Score breakdown</div>
    <div class="sb-row"><span class="sb-label">Price vs comps (×${wP})</span>${_fmtSub(d.scPrice,wP)}</div>
    <div class="sb-row"><span class="sb-label">Property quality (×${wPr})</span>${_fmtSub(d.scProp,wPr)}</div>
    <div class="sb-row"><span class="sb-label">Location / geo (×${wL})</span>${_fmtSub(d.scLoc,wL)}</div>
    <div class="sb-row"><span class="sb-label">Penalty score (×${wPn})</span>${_fmtSub(d.scPen,wPn)}</div>
    ${d.scCap ? `<div class="gt-sep"></div><div style="font-size:10px;color:var(--coral)">${escapeHtml(t('card.cap', { reason: d.scCap }))}</div>` : ''}
    <div class="gt-sep"></div>
    <div class="sb-row"><span class="sb-label" style="font-weight:700">Total</span><span style="font-weight:800;font-size:14px">${d.scTotal ?? '–'} / 100</span></div>`;
}

// "Pinned" state: when set, the tooltip stays open until the user clicks
// outside the score badge or the same badge again. Pinning is what makes
// the tooltip work on touch devices where there's no hover.
let _gtPinnedAnchor = null;

// Document-level delegated handlers fire for EVERY event in the page, not
// just our card markup. e.target can be the document itself (text-node
// event after a Range selection), an SVG element on Safari/Firefox where
// `closest` historically wasn't on SVGElement, or any other non-Element
// target. Guarding once here keeps every delegation site safe without
// scattering `typeof` checks through the call sites.
const _safeClosest = (target, selector) =>
  (target && typeof target.closest === 'function') ? target.closest(selector) : null;

// ── Item 3: Address formatting ────────────────────────────────────────────────
const _ADDR_LOWER = new Set(['via','corso','piazza','viale','largo','vicolo',
  'strada','borgo','contrada','lungarno','lungotevere','salita','rampa']);

function formatAddress(str) {
  if (!str) return str;
  return str.trim().split(/\s+/).map((word, i) => {
    const base = word.toLowerCase().replace(/['']/g, "'");
    if (i > 0 && _ADDR_LOWER.has(base)) return base;
    return base
      .split(/([-'])/)
      .map(part => /^[a-zàèéìòù]/i.test(part)
        ? part.charAt(0).toUpperCase() + part.slice(1)
        : part)
      .join('');
  }).join(' ');
}

// ── Helpers ───────────────────────────────────────────────────────────────────
const scoreClass = s => s >= 70 ? 'high' : s >= 45 ? 'mid' : 'low';
const fmt = n => n != null ? Number(n).toLocaleString('it-IT') : '–';

// ── Floor display helpers ─────────────────────────────────────────────────────
function _floorLabel(l) {
  if (l.floor_label) return l.floor_label;
  const fn = l.floor_n;
  if (fn == null) return null;
  if (fn <= -2) return 'Interrato';
  if (fn === -1) return 'Seminterrato';
  if (fn === 0) return 'Piano terra';
  // Legacy: old data had R→-1 incorrectly; detect via raw floor field
  if (fn === 1 && l.floor === 'R') return 'Piano rialzato';
  return `${fn}° piano`;
}

function conditionEmoji(cond) {
  if (!cond) return '';
  const c = cond.toLowerCase();
  if (c.includes('da ') && c.includes('ristruttur')) return '🔨';
  if (c.includes('ottim') || c.includes('ristruttur') || c.includes('eccellent')) return '🌟';
  if (c.includes('nuovo') || c.includes('nuova') || c.includes('costruz')) return '✨';
  if (c.includes('buon') || c.includes('abitabil')) return '✅';
  return '🏠';
}

function avg(arr, key) {
  const vals = arr.map(l => l[key]).filter(v => v != null && !isNaN(v));
  return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
}

// ── Favourites ────────────────────────────────────────────────────────────────
function getFavourites()     { return Storage.get('favourites', []); }

function updateFavBadge() {
  const n     = getFavourites().length;
  const badge = document.getElementById('favBadge');
  if (!badge) return;
  badge.textContent = n;
  badge.classList.toggle('visible', n > 0);
}

// ── Rental favourites (separate key from sales) ───────────────────────────────
function getRentFavs()       { return Storage.get('rentFavourites', []); }
function saveRentFavs(ids)   { Storage.set('rentFavourites', ids); }
function isRentFav(id)       { return getRentFavs().includes(String(id)); }

function toggleRentFav(id) {
  // No sign-in gate: locally Clerk never loads, so saving was impossible.
  // Favourites live in this browser (and, locally, in user_data.json).
  const ids = getRentFavs();
  const i   = ids.indexOf(String(id));
  if (i === -1) ids.push(String(id)); else ids.splice(i, 1);
  saveRentFavs(ids);
  return i === -1;
}

// ── Live-API vs static-mode detection ────────────────────────────────────────
let IS_LIVE = false;   // true when running via api.py locally

async function applyStaticMode() {
  // ── Hide all data-pulling / server-only controls ──────────────────────────
  const hide = (...ids) => ids.forEach(id => {
    const el = typeof id === 'string' ? document.getElementById(id) : id;
    if (el) el.style.display = 'none';
  });

  // Header: local-server badge (Run-Lume + Zones buttons removed entirely).
  document.querySelectorAll('.server-note').forEach(el => el.style.display = 'none');

  // Scanner panels (querySelectorAll — there are two .scanner-panel elements).
  // The .email-panel was removed in the UI consolidation.
  document.querySelectorAll('.scanner-panel').forEach(p => p.style.display = 'none');

  // Rentals-tab action controls
  hide('btnApplyOmi', 'applyOmiStatus', 'btnEnrichGeo', 'btnDownloadPois', 'geoCoverage', 'cacheRow',
       'btnDqRefresh', 'btnDqReenrichF', 'btnDqExport');

  // Public mode: hide Settings tab only
  hide('tabSettings');
  // Ensure rentals tab is active on initial load
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  const rentTab = document.getElementById('tabRentals');
  if (rentTab) rentTab.classList.add('active');

  // The Snapshot pill and the CI pill expose self-hosted-runner state —
  // useful when working locally against the scanner, meaningless to a
  // visitor on immobiliare-scorer.pages.dev. Show both only on localhost.
  const onLocalhost = ['localhost', '127.0.0.1', '0.0.0.0'].includes(location.hostname);
  if (onLocalhost) {
    const pill = document.getElementById('statusPill');
    if (pill) pill.style.display = '';
  } else {
    const pill   = document.getElementById('statusPill');
    if (pill)   pill.style.display   = 'none';
  }

  // Last-scan time used to come from the JSON snapshot's Last-Modified
  // header. Supabase doesn't expose a per-table mtime, but renderRentals()
  // sets the meta line to "{N} listings · {today}" once data lands, which
  // covers the same UX need (a freshness signal in the header).
}

// ── Init ──────────────────────────────────────────────────────────────────────
function _updateStickyTops() {
  const header  = document.querySelector('header');
  const tabBar  = document.querySelector('.tab-bar');
  const headerH = header ? header.offsetHeight : 0;
  const tabH    = tabBar ? tabBar.offsetHeight : 0;
  if (tabBar) tabBar.style.top = headerH + 'px';

  // Only ONE sticky bar to position: the filter bar itself.
  document.querySelectorAll('.filters').forEach(f => {
    f.style.top = (headerH + tabH) + 'px';
  });
}

// ── Filter bar sticky shadow — sentinel-based detection ───────────────────────
// Insert a 1px sentinel just before each .filters element. When the sentinel
// scrolls out of view, the filter bar is "stuck" and gains the .is-stuck class,
// which paints a stronger bottom shadow and removes its rounded outer margin.
function _wireFilterBarStickyShadow() {
  if (!('IntersectionObserver' in window)) return;
  document.querySelectorAll('.filters').forEach(filterBar => {
    if (filterBar.dataset.stuckObserved === '1') return;
    filterBar.dataset.stuckObserved = '1';

    let sentinel = filterBar.previousElementSibling;
    if (!sentinel || !sentinel.classList.contains('filters-sentinel')) {
      sentinel = document.createElement('div');
      sentinel.className = 'filters-sentinel';
      filterBar.parentNode.insertBefore(sentinel, filterBar);
    }

    const obs = new IntersectionObserver(
      ([entry]) => filterBar.classList.toggle('is-stuck', !entry.isIntersecting),
      { threshold: 1.0 }
    );
    obs.observe(sentinel);
  });
}

async function init() {
  // Log the build marker so iOS Safari stale-cache debugging becomes
  // a one-glance check in DevTools instead of guessing whether a
  // deploy actually landed.
  const _buildMeta = document.querySelector('meta[name="lume-build"]');
  if (_buildMeta) console.log(`[lume] build: ${_buildMeta.content}`);
  const isHttp = location.protocol === 'http:' || location.protocol === 'https:';
  if (!isHttp) {
    document.getElementById('meta').textContent = 'needs HTTP server';
    return;
  }

  // Detect live-API vs static (Cloudflare Pages) mode.
  // Primary signal: hostname. Only localhost/127.0.0.1 gets the live API
  // (api.py listens on 8000). On Cloudflare Pages we can't trust /api-ping
  // alone — Pages serves unknown URLs via SPA fallback as 200, defeating the
  // probe. Hostname is the bulletproof signal; /api-ping is kept as a
  // belt-and-suspenders check for unusual local setups (e.g. a LAN IP).
  const _isLocalHost = ['localhost', '127.0.0.1', '0.0.0.0'].includes(location.hostname);
  if (!_isLocalHost) {
    IS_LIVE = false;
  } else {
    try {
      const ping = await fetch('/api-ping');
      IS_LIVE = ping.ok;
    } catch {
      IS_LIVE = false;
    }
  }
  if (!IS_LIVE) {
    applyStaticMode();
  } else {
    const pill = document.getElementById('statusPill');
    if (pill) {
      pill.classList.add('is-live');
      pill.style.display = '';
      const lbl = document.getElementById('statusPillLabel');
      if (lbl) lbl.textContent = 'Live';
    }
    // Late-binding fix: if the user already clicked Settings BEFORE the
    // /api-ping above resolved, switchTab('settings') ran with IS_LIVE
    // still false and left #scannerCard hidden. Re-run the IS_LIVE-gated
    // side-effects now that the flag is finally true. Without this the
    // user has to switch away and back for the Scanner card to appear.
    if (activeTab === 'settings') {
      const _scannerCard = document.getElementById('scannerCard');
      if (_scannerCard) {
        _scannerCard.hidden = false;
        if (typeof _renderCityScanGrid === 'function') _renderCityScanGrid();
      }
    }
    if (activeTab === 'settings' || activeTab === 'rentals') startScannerPoll();
    const _staleGroup = document.getElementById('rShowStaleGroup');
    if (_staleGroup) _staleGroup.hidden = false;
    // Real scoring weights for the score breakdown (not just when Settings
    // is opened) + the local copy of saved favourites/collections.
    if (typeof _loadScoreSettings === 'function') _loadScoreSettings();
    await _syncUserDataFromServer();
    _loadLocalAlertIdentity();
  }

  // Load the cities config first so currentCity → display name + map
  // centre + has_metro are accurate before any listings render. The
  // fallback availableCities baked into the script already covers the
  // four seeded cities so a failure here is a no-op.
  try { await loadAvailableCities(); } catch (_) { /* keep fallback */ }
  updateCityToggleUI();
  updateFiltersForCurrentCity();

  // Load initial data now that IS_LIVE is settled — avoids a race where a
  // standalone loadRentals() call fires before init() and picks the wrong
  // path (Supabase REST in deployed mode vs Flask /rentals locally).
  await loadRentals();
  loadCacheStats();
}

// ── ID search: lives inside the rentals filter bar ─

// ── Tab switching ─────────────────────────────────────────────────────────────
let activeTab = 'rentals';

const TAB_STORAGE_KEY = 'active_tab';

function switchTab(tab, opts) {
  const persist = !opts || opts.persist !== false;
  activeTab = tab;
  ['rentalsView','mapView','insightsView','settingsView'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    const show = id === tab + 'View';
    el.hidden = !show;
    el.style.display = show ? '' : 'none';
  });
  document.querySelectorAll('.tab-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.tab === tab));
  // Recalc after the tab swap so the filter bar lands at the right top.
  if (typeof _updateStickyTops === 'function') {
    requestAnimationFrame(_updateStickyTops);
  }
  // Persist the active tab so reloading the dashboard keeps the user where
  // they were. Skip persistence for detail-view navigations — those use the
  // URL itself, not the tab state.
  if (persist) {
    try { Storage.set(TAB_STORAGE_KEY, tab); } catch (_) {}
  }
  if (tab === 'rentals') {
    startScannerPoll();
    if (!ALL_RENT.length) loadRentals();
    // First-paint of the filter bar happens with display:none on the
    // rentalsView container — getBoundingClientRect returns zeros and
    // the pill-thumbs land at 0/0. Recompute now that the bar is laid
    // out so the thumbs snap to the correct positions immediately.
    if (typeof _updateAllPillThumbs === 'function') {
      requestAnimationFrame(_updateAllPillThumbs);
    }
  } else if (tab === 'map') {
    stopScannerPoll();
    _mapInit();
    if (_map) _map.invalidateSize();  // Leaflet needs this when container was hidden
    if (_mapDataType === 'rentals' && !ALL_RENT.length) loadRentals().then(() => _mapRender());
    else _mapRender();
  } else if (tab === 'insights') {
    stopScannerPoll();
    showInsightsTab();
  } else if (tab === 'settings') {
    // Keep the scanner poll alive on Settings — the multi-city Enrichment
    // panel reads its progress from /scanner-status. Polling here is also
    // useful for the (less-frequently-touched) scan log.
    startScannerPoll();
    _settingsLoad();
    _loadScoreSettings();
    _dqLoad();
    // Scanner card is localhost-only (the Cloudflare deployment doesn't
    // have api.py running). Re-render the city grid each time we open
    // Settings so it picks up any localStorage edits made elsewhere.
    const _scannerCard = document.getElementById('scannerCard');
    if (_scannerCard) {
      _scannerCard.hidden = !IS_LIVE;
      if (IS_LIVE) {
        if (typeof _renderCityScanGrid === 'function') _renderCityScanGrid();
      }
    }
    // Daily-alert section reads from Supabase digest_filters. Async so
    // the rest of the Settings panel paints immediately; the wrap div
    // gets filled in once the network round-trip completes.
    const _digestWrap = document.getElementById('digestSettingsWrap');
    if (_digestWrap && typeof renderDigestSettingsSection === 'function') {
      renderDigestSettingsSection().then(html => { _digestWrap.innerHTML = html; });
    }
  } else {
    stopScannerPoll();
  }
}

// Restore the active tab on page load. Called from showApp() once the DOM
// is ready. Skipped when the URL targets a shared collection — that has its
// own banner that takes priority.
function restoreActiveTab() {
  try {
    const params = new URLSearchParams(location.search);
    if (params.get('c'))                  return;

    const saved = Storage.get(TAB_STORAGE_KEY, 'rentals');
    const validTabs = ['rentals', 'map', 'insights', 'settings'];
    // Avoid a redundant re-render when the saved tab matches the default.
    if (validTabs.includes(saved) && saved !== 'rentals') {
      switchTab(saved, { persist: false });
    }
  } catch (_) { /* localStorage unavailable — fall back to default rentals tab */ }
}
