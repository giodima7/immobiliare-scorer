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
    n:    fmt(parseInt(m[1].replace(/[^\d]/g, ''), 10)),
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

// Locale-aware numbers — Italian "1.234,5", English "1,234.5". Grouping
// always on: it-IT alone leaves 4-digit numbers ungrouped ("5768" next to
// "11.480").
const _nfCache = {};
function _nf(opts) {
  const key = currentLang + JSON.stringify(opts || {});
  return _nfCache[key] || (_nfCache[key] = new Intl.NumberFormat(
    currentLang === 'it' ? 'it-IT' : 'en-GB', { useGrouping: 'always', ...(opts || {}) }));
}
function fmtNum(n, decimals) {
  if (n == null || n === '' || Number.isNaN(Number(n))) return '–';
  const d = decimals == null ? 0 : decimals;
  return _nf({ minimumFractionDigits: d, maximumFractionDigits: d }).format(Number(n));
}
// One decimal ("15,3" / "15.3").
const fmt1 = n => fmtNum(n, 1);
// Signed percentage with a real minus sign: "−27%", "+4%".
function fmtPct(n, decimals = 0) {
  if (n == null || Number.isNaN(Number(n))) return '–';
  const v = Number(n);
  return (v < 0 ? '−' : v > 0 ? '+' : '') + fmtNum(Math.abs(v), decimals) + '%';
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
    ? `<span style="font-weight:700">${v}</span> <span style="color:var(--text-secondary);font-size: 12px">×${w} = ${Math.round(Number(v)*w)}</span>`
    : `<span style="color:var(--text-secondary)">– (neutral 50×${w} = ${Math.round(50*w)})</span>`;
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
    ${d.scCap ? `<div class="gt-sep"></div><div style="font-size: 12px;color:var(--coral-ink)">${escapeHtml(t('card.cap', { reason: d.scCap }))}</div>` : ''}
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
// The one place the score thresholds live — cards, map, Details and Stats
// all colour by it (CSS: .sc-high / .sc-mid / .sc-low).
const scoreClass = s => s >= 70 ? 'high' : s >= 45 ? 'mid' : 'low';
const fmt = n => (n == null || n === '' || Number.isNaN(Number(n))) ? '–' : _nf().format(Number(n));

// The site a listing comes from ("Open on …", source chips, sharing).
function siteName(source) {
  const s = String(source || '');
  if (s.startsWith('idealista')) return 'Idealista';
  if (s === 'subito') return 'Subito.it';
  if (s === 'casait') return 'Casa.it';
  return 'Immobiliare.it';
}

// A private landlord — no agency fee. "user" is Immobiliare's word for a
// private advertiser (fetch_casait uses it too); Subito is read from private
// ads only; Idealista says it on the listing page (listing_details).
function isPrivate(l) {
  return !!l && (l.agency_type === 'user' || l.source === 'subito');
}

// The site where the same flat is up from a private landlord, when this
// copy is an agency's (an owner who also hired an agency): the way round
// the fee. '' when none.
function privateCopySite(l) {
  if (!l || isPrivate(l)) return '';
  const a = (l.also_on || []).find(x => x.private);
  return a ? siteName(a.source) : '';
}

// The other sites / agencies the same flat is listed on (widgets.js
// _dedupListings → also_on), as "Idealista, Subito.it".
function alsoOnSites(l) {
  const names = [...new Set((l.also_on || []).map(a => siteName(a.source)))];
  return names.filter(n => n !== siteName(l.source)).join(', ');
}

// What a listing really costs a month: rent + condo fees + heating billed
// apart, as the listing page states them (listing_details.py) — never an
// estimate. known=false → only the rent is known ("rent only").
// Condo fees above half the rent are almost always a yearly figure typed
// as monthly (€1.200 on a €1.200 rent, 2026-10-18): left out of the total,
// shown as "to check".
function monthlyCost(l) {
  const rent     = Number(l.rent_mo ?? l.price) || 0;
  const included = l.condo_included === true;
  const stated   = included ? 0 : (Number(l.spese_condominiali) || 0);
  const suspect  = stated > 0 && rent > 0 && stated > rent * 0.5;
  const condo    = suspect ? 0 : stated;
  // Heating above half the rent is a placeholder, not a cost (an agency
  // typed €2,147,483,647 a year): left out like an implausible condo fee.
  const heatRaw  = Number(l.heating_mo) || 0;
  const heating  = heatRaw > 0 && rent > 0 && heatRaw <= rent * 0.5 ? heatRaw : 0;
  const known    = included || condo > 0 || heating > 0;
  const total    = rent + condo + heating;
  return { rent, condo, heating, included, known, total, suspect, statedCondo: stated,
           extraPct: rent ? Math.round((total - rent) / rent * 100) : 0,
           // Subito's search result is the whole listing: nothing left to read.
           pageRead: !!l.detail_fetched_at || l.source === 'subito' };
}

// ── Floor display helpers ─────────────────────────────────────────────────────
function _floorLabel(l) {
  const fn = l.floor_n;
  if (fn == null) return l.floor_label || null;
  if (fn <= -2) return t('floor.basement');
  if (fn === -1) return t('floor.semi');
  if (fn === 0) return t('floor.ground');
  // Legacy: old data had R→-1 incorrectly; detect via raw floor field
  if (fn === 1 && l.floor === 'R') return t('floor.raised');
  return t('floor.n', { n: fn });
}

function avg(arr, key) {
  const vals = arr.map(l => l[key]).filter(v => v != null && !isNaN(v));
  return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
}

// ── Favourites ────────────────────────────────────────────────────────────────
function getFavourites()     { return Storage.get('favourites', []); }

// Header "Saved" count — rental favourites (the sales list is retired).
function updateFavBadge() {
  const n     = getRentFavs().length;
  const badge = document.getElementById('favBadge');
  if (!badge) return;
  badge.textContent = fmt(n);
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
  updateFavBadge();
  if (typeof activeTab !== 'undefined' && activeTab === 'saved' && typeof renderSaved === 'function') renderSaved();
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


  // Scanner panels (querySelectorAll — there are two .scanner-panel elements).
  // The .email-panel was removed in the UI consolidation.
  document.querySelectorAll('.scanner-panel').forEach(p => p.style.display = 'none');

  // Rentals-tab action controls
  hide('btnApplyOmi', 'applyOmiStatus', 'btnEnrichGeo', 'btnDownloadPois', 'geoCoverage', 'cacheRow',
       'btnDqRefresh', 'btnDqReenrichF', 'btnDqExport');

  // Public mode: hide Settings tab only
  hide('tabSettings');
  // Highlight the tab that is open (a restored one — For you, Map — too;
  // this used to light Rentals whatever was showing).
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  const curTab = document.querySelector(`.tab-btn[data-tab="${typeof activeTab !== 'undefined' ? activeTab : 'rentals'}"]`)
              || document.getElementById('tabRentals');
  if (curTab) curTab.classList.add('active');

  // The Snapshot pill and the CI pill expose self-hosted-runner state —
  // useful when working locally against the scanner, meaningless to a
  // visitor on immobiliare-scorer.pages.dev. Show both only on localhost.
  const onLocalhost = ['localhost', '127.0.0.1', '0.0.0.0'].includes(location.hostname);
  setStatusPill(false);
  const pill = document.getElementById('statusPill');
  if (pill) pill.hidden = !onLocalhost;

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
  _liveKnown = true;   // loadRentals() may run now (see rentals.js)
  if (!IS_LIVE) {
    applyStaticMode();
  } else {
    setStatusPill(true);
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
      if (typeof loadPhotoStatus === 'function') loadPhotoStatus();
      if (typeof loadSaleAreasCard === 'function') loadSaleAreasCard();
    }
    if (activeTab === 'settings' || activeTab === 'rentals') startScannerPoll();
    const _staleGroup = document.getElementById('rShowStaleGroup');
    if (_staleGroup) _staleGroup.hidden = false;
    // Real scoring weights for the score breakdown (not just when Settings
    // is opened) + the local copy of saved favourites/collections.
    if (typeof _loadScoreSettings === 'function') _loadScoreSettings();
    if (typeof window._loadPoisStatus === 'function') window._loadPoisStatus();
    await _syncUserDataFromServer();
    _loadLocalAlertIdentity();
  }

  // The cities config (display name, map centre) and the listings load in
  // parallel — the baked-in fallback covers Milan until the config lands.
  const citiesReady = loadAvailableCities().catch(() => {}).then(() => {
    updateCityToggleUI();
    updateFiltersForCurrentCity();
  });
  // Load initial data now that IS_LIVE is settled — avoids a race where a
  // standalone loadRentals() call fires before init() and picks the wrong
  // path (Supabase REST in deployed mode vs Flask /rentals locally).
  await loadRentals();
  await citiesReady;
  loadCacheStats();
}

// Header data-source dot: the label lives in its tooltip.
function setStatusPill(live) {
  const pill = document.getElementById('statusPill');
  if (!pill) return;
  pill.classList.toggle('is-live', !!live);
  const label = live ? t('status.live') : t('status.snapshot');
  pill.title = label;
  pill.setAttribute('aria-label', label);
  const lbl = document.getElementById('statusPillLabel');
  if (lbl) lbl.textContent = label;
}

// ── Heavy libraries, loaded on first use ──────────────────────────────────────
// Leaflet (Map tab) and Chart.js (Stats, Tools) used to load with the page.
function _loadAsset(tag, attrs) {
  return new Promise((resolve, reject) => {
    const el = document.createElement(tag);
    Object.assign(el, attrs);
    el.crossOrigin = 'anonymous';
    el.onload = () => resolve();
    el.onerror = () => reject(new Error('could not load ' + (attrs.src || attrs.href)));
    document.head.appendChild(el);
  });
}
let _leafletPromise = null;
function ensureLeaflet() {
  if (window.L && L.markerClusterGroup) return Promise.resolve();
  if (!_leafletPromise) {
    const css = (href, integrity) => _loadAsset('link', { rel: 'stylesheet', href, integrity });
    _leafletPromise = Promise.all([
      css('https://unpkg.com/leaflet@1.9.4/dist/leaflet.css', 'sha384-sHL9NAb7lN7rfvG5lfHpm643Xkcjzp4jFvuavGOndn6pjVqS6ny56CAt3nsEVT4H'),
      css('https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css', 'sha384-pmjIAcz2bAn0xukfxADbZIb3t8oRT9Sv0rvO+BR5Csr6Dhqq+nZs59P0pPKQJkEV'),
      css('https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css', 'sha384-wgw+aLYNQ7dlhK47ZPK7FRACiq7ROZwgFNg0m04avm4CaXS+Z9Y7nMu8yNjBKYC+'),
      _loadAsset('script', { src: 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js', integrity: 'sha384-cxOPjt7s7Iz04uaHJceBmS+qpjv2JkIHNVcuOrM+YHwZOmJGBXI00mdUXEq65HTH' })
        .then(() => _loadAsset('script', { src: 'https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js', integrity: 'sha384-eXVCORTRlv4FUUgS/xmOyr66XBVraen8ATNLMESp92FKXLAMiKkerixTiBvXriZr' })),
    ]).catch(e => { _leafletPromise = null; throw e; });
  }
  return _leafletPromise;
}
let _chartPromise = null;
function ensureChart() {
  if (window.Chart) { applyChartTheme(); return Promise.resolve(); }
  if (!_chartPromise) {
    _chartPromise = _loadAsset('script', { src: 'https://cdn.jsdelivr.net/npm/chart.js@4.4.2/dist/chart.umd.min.js', integrity: 'sha384-e6cc9LaIG7xZ3XD5B+jtr1NhTWPQGQdRCh6xiZ+ZFUtWCpg4ycv3Sh+SkZoopvUY' })
      .then(applyChartTheme)
      .catch(e => { _chartPromise = null; throw e; });
  }
  return _chartPromise;
}
// Chart.js reads plain colours: hand it the current theme's tokens.
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function applyChartTheme() {
  if (!window.Chart) return;
  Chart.defaults.font.family = cssVar('--font') || 'Inter, sans-serif';
  Chart.defaults.font.size = 12;
  Chart.defaults.color = cssVar('--text-secondary');
  Chart.defaults.borderColor = cssVar('--border-light');
}
function isDarkTheme() {
  const th = document.documentElement.dataset.theme;
  return th === 'dark' || (!th && window.matchMedia('(prefers-color-scheme: dark)').matches);
}
// After a theme change: re-colour what paints with plain colours.
function onThemeChanged() {
  applyChartTheme();
  if (typeof insightsLoaded !== 'undefined' && insightsLoaded && typeof computeInsights === 'function' && activeTab === 'insights') computeInsights();
  else if (typeof insightsLoaded !== 'undefined') insightsLoaded = false;
  if (typeof _mapApplyTheme === 'function') _mapApplyTheme();
}

// ── ID search: lives inside the rentals filter bar ─

// ── Tab switching ─────────────────────────────────────────────────────────────
let activeTab = 'rentals';

const TAB_STORAGE_KEY = 'active_tab';

function switchTab(tab, opts) {
  const persist = !opts || opts.persist !== false;
  activeTab = tab;
  document.body.dataset.tab = tab;
  ['foryouView','rentalsView','salesView','mapView','savedView','insightsView','settingsView'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.hidden = id !== tab + 'View';
  });
  document.querySelectorAll('.tab-btn').forEach(b => {
    const on = b.dataset.tab === tab;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', String(on));
  });
  if (typeof toggleRentPanel === 'function') toggleRentPanel(null);
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
  if (tab === 'foryou') {
    stopScannerPoll();
    renderForYou();
    // Both kinds of listing feed it; their loaders render it again as each arrives.
    if (!ALL_RENT.length) loadRentals();
    if (!ALL_SALE.length) loadSales();
  } else if (tab === 'rentals') {
    startScannerPoll();
    if (!ALL_RENT.length) loadRentals();
  } else if (tab === 'sales') {
    stopScannerPoll();
    if (!ALL_SALE.length) loadSales(); else renderSales();
  } else if (tab === 'map') {
    stopScannerPoll();
    ensureLeaflet().then(() => {
      if (activeTab !== 'map') return;
      _mapInit();
      _sizeMapView();
      if (_map) _map.invalidateSize();  // Leaflet needs this when container was hidden
      if (!ALL_RENT.length) loadRentals().then(() => _mapRender());
      else _mapRender();
    }).catch(e => showToast(t('error.lib', { what: 'Leaflet' }) + ' ' + e.message));
  } else if (tab === 'saved') {
    stopScannerPoll();
    if (typeof renderSaved === 'function') renderSaved();
  } else if (tab === 'insights') {
    stopScannerPoll();
    ensureChart().then(() => { if (activeTab === 'insights') showInsightsTab(); })
      .catch(e => showToast(t('error.lib', { what: 'Chart.js' }) + ' ' + e.message));
  } else if (tab === 'settings') {
    // Keep the scanner poll alive on Settings — the multi-city Enrichment
    // panel reads its progress from /scanner-status. Polling here is also
    // useful for the (less-frequently-touched) scan log.
    startScannerPoll();
    _settingsLoad();
    if (typeof loadSaleAreasCard === 'function') loadSaleAreasCard();
    if (typeof loadPhotoStatus === 'function') loadPhotoStatus();
    _loadScoreSettings();
    ensureChart().catch(() => {}).finally(() => _dqLoad());
    if (typeof initCollapsibleCards === 'function') initCollapsibleCards();
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

    const validTabs = ['foryou', 'rentals', 'sales', 'map', 'saved', 'insights', 'settings'];
    // ?tab=saved — the old /favourites page redirects here.
    const asked = params.get('tab');
    if (asked && validTabs.includes(asked)) {
      const url = new URL(location); url.searchParams.delete('tab');
      history.replaceState({}, '', url);
      switchTab(asked);
      return;
    }
    const saved = Storage.get(TAB_STORAGE_KEY, 'rentals');
    // Avoid a redundant re-render when the saved tab matches the default.
    if (validTabs.includes(saved) && saved !== 'rentals') {
      switchTab(saved, { persist: false });
    }
  } catch (_) { /* localStorage unavailable — fall back to default rentals tab */ }
}

// ── Theme ─────────────────────────────────────────────────────────────────────
// System (default) → light → dark. The choice sits on <html data-theme> and
// in localStorage (read before first paint by the inline script in index.html).
const _THEMES = ['system', 'light', 'dark'];

function currentTheme() {
  return document.documentElement.dataset.theme || 'system';
}

function setTheme(mode) {
  if (mode === 'system') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = mode;
  try {
    if (mode === 'system') localStorage.removeItem('lume_theme');
    else localStorage.setItem('lume_theme', mode);
  } catch (_) {}
  renderThemeToggle();
  onThemeChanged();
}

function cycleTheme() {
  setTheme(_THEMES[(_THEMES.indexOf(currentTheme()) + 1) % _THEMES.length]);
}

function renderThemeToggle() {
  const btn = document.getElementById('themeToggle');
  if (!btn) return;
  const mode = currentTheme();
  btn.innerHTML = icon({ system: 'circle-half-2', light: 'sun', dark: 'moon' }[mode]);
  btn.title = t('theme.' + mode);
  btn.setAttribute('aria-label', t('theme.' + mode));
}
