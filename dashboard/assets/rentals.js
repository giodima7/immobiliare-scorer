// Lume dashboard — rentals: filtering, rendering, progressive loading, data loading.
// Plain script (no bundler); shares globals with the other assets/*.js files.
// Declarations only: code that runs at start-up lives in init.js, which loads
// last, so any function here may call any other.

// ── Rentals — filter & render ─────────────────────────────────────────────────
// Lume listing-ID search — one input per tab, both sit inside their
// own filter bar. Trimmed + lower-cased once here so the
// applyRent/Sale filters can do a cheap substring match.
function getIdQuery(inputId) {
  return (document.getElementById(inputId)?.value || '').trim().toLowerCase();
}

function getRentFilters() {
  return {
    idQuery:   getIdQuery('rIdSearch'),
    areas:     rentSelectedAreas,
    fascia:    document.getElementById('rFascia').value,
    // OMI Zone filter removed from the bar — Area + Fascia cover the same
    // intent and work across cities. Field kept as '' so applyRentFilters'
    // `if (f.omiZone && …)` branch becomes an effective no-op without
    // editing every filter-call-site.
    omiZone:   '',
    maxRent:   parseInt(document.getElementById('rMaxRent').value)   || 0,
    minSqm:    parseInt(document.getElementById('rMinSqm').value)    || 0,
    minRooms:  parseInt(document.getElementById('rMinRooms').value)  || 0,
    minScore:  parseInt(document.getElementById('rScore').value)     || 0,
    source:    document.querySelector('#rSourceFilter .sort-tab.active')?.dataset.rsource || '',
    maxMetro:  parseInt(document.getElementById('rMaxMetro').value)  || 0,
    minFloor:  parseInt(document.getElementById('rMinFloor').value)  || -999,
    elevator:  document.getElementById('rElevator').checked,
    minEnergy: document.getElementById('rMinEnergy').value,
    gemFilter: document.querySelector('#rGemFilter .gem-tab.active')?.dataset.gem || '',
    fListed:   document.getElementById('rListed')?.value || '',
    // filters.js: must-haves (lift = elevator above), condition, discount, contract
    must:           rentMustActive(),
    includeUnknown: rentIncludeUnknown,
    condition:      document.getElementById('rCondition')?.value || '',
    minDiscount:    parseInt(document.getElementById('rMinDiscount')?.value) || 0,
    contract:       document.getElementById('rContract')?.value || '',
  };
}

function applyRentFilters(data, f) {
  // Shared-collection mode: hard-restrict to the IDs in the URL before any
  // other filter runs. The user can still narrow further with the filter bar.
  if (window.sharedCollection) {
    const ids = new Set(window.sharedCollection.listing_ids.map(String));
    data = data.filter(l => ids.has(String(l.id)));
  }
  // Agency-pin filter: when the user clicks an agency name in the Stats
  // leaderboard, we hard-restrict the grid to that agency's listings. The
  // pin is cleared via the banner's ✕ button.
  if (window._pinnedAgencyRent) {
    const want = window._pinnedAgencyRent.toLowerCase();
    data = data.filter(l => (l.agency_name || '').trim().toLowerCase() === want);
  }
  // Hide stale (off-market) listings by default. The "Show expired" toggle
  // in the filter bar inverts this for debugging/historical review.
  const showStale = document.getElementById('rShowStale')?.checked || false;
  if (!showStale) data = data.filter(l => !l.is_stale);
  return data.filter(l => {
    // Header ID search short-circuits the rest of the filters — when
    // it's active the user is looking for one specific listing, not
    // narrowing by other criteria.
    if (f.idQuery) {
      if (!String(l.id ?? '').toLowerCase().includes(f.idQuery)) return false;
    }
    // Fake / bait / misrepresented listings (score 0) — the sales filter
    // already hides them; rentals showed 231 of them in the grid.
    if (l.excluded) return false;
    if (f.areas.length) {
      const nb = (l.neighbourhood || '').toLowerCase();
      // Bidirectional substring match so both sources work:
      //   • nb.includes(al): "navigli - darsena" matches filter "navigli" ✓
      //   • al.includes(nb): "porta romana" matches filter "porta romana - medaglie d'oro" ✓
      if (!f.areas.some(a => { const al = a.toLowerCase(); return nb.includes(al) || al.includes(nb); })) return false;
    }
    if (f.fascia   && l.omi_fascia !== f.fascia)               return false;
    if (f.omiZone  && l.omi_zona  !== f.omiZone)              return false;
    if (f.maxRent  && monthlyCost(l).total > f.maxRent)       return false;   // all-in when known
    if (f.minSqm   && l.sqm < f.minSqm)                       return false;
    if (f.minRooms && (l.rooms ?? 0) < f.minRooms)            return false;
    if (f.minScore && (l.score_total ?? 0) < f.minScore)      return false;
    if (f.source   && (l.source || 'immobiliare') !== f.source) return false;
    // New enriched filters
    if (f.maxMetro > 0) {
      // When a metro distance filter is active, listings with NO geo data are
      // excluded — we can't confirm they meet the requirement.
      if (l.metro_nearest_dist_m == null) return false;
      if (l.metro_nearest_dist_m > f.maxMetro) return false;
    }
    if (f.minFloor > -999) {
      if ((l.floor_n ?? -999) < f.minFloor) return false;
    }
    if (f.elevator && !(l.elevator === true || (f.includeUnknown && l.elevator == null))) return false;
    if (!passesExtraFilters(l, f)) return false;
    if (f.gemFilter === 'hidden' && !l.hidden_gem) return false;
    if (f.gemFilter === 'good'   && !(l.good_value === true && l.hidden_gem !== true)) return false;
    if (f.fListed) {
      if (!l.first_seen_date) return false;
      if (f.fListed === 'today') {
        // Match listings whose first_seen_date is today's calendar date —
        // local date: the UTC one missed the 00:29 scan until 02:00.
        const todayStr = _localDateStr(new Date());
        if (String(l.first_seen_date).slice(0, 10) !== todayStr) return false;
      } else {
        const days = parseInt(f.fListed);
        const cutoff = new Date(); cutoff.setDate(cutoff.getDate() - days);
        if (new Date(l.first_seen_date) < cutoff) return false;
      }
    }
    if (f.minEnergy) {
      const rank    = ENERGY_RANK[l.energy_class] || 0;
      const minRank = ENERGY_MIN_RANK[f.minEnergy] || 0;
      // Only filter out if energy data is present AND rank is too low
      if (l.energy_class && rank < minRank) return false;
    }
    return true;
  });
}

// ── Progressive card loading ──────────────────────────────────────────────────
// Initial render: 60 cards painted synchronously (so first paint is fast).
// Subsequent batches: 60 more cards each time the scroll sentinel comes
// within 1,200 px of the viewport's bottom edge, so cards arrive before the
// user reaches the end. Filter changes reset the cursor and re-arm.
const RENT_PAGE_INITIAL    = 60;
const RENT_PAGE_SUBSEQUENT = 60;
const SCROLL_ROOT_MARGIN   = '0px 0px 1200px 0px';

let _rentFilteredData  = [];
let _rentRenderedCount = 0;
let _rentObserver      = null;

function _updateRentScrollStatus() {
  const el = document.getElementById('rentScrollStatus');
  if (!el) return;
  const total = _rentFilteredData.length;
  const shown = Math.min(_rentRenderedCount, total);
  if (total === 0) {
    el.innerHTML = '';
  } else if (shown >= total) {
    el.textContent = total > RENT_PAGE_INITIAL ? t('scroll.all', { n: fmt(total) }) : '';
  } else {
    el.innerHTML =
      `<span class="loading-dots">${escapeHtml(t('scroll.more'))}</span>` +
      `<span class="scroll-meta">${escapeHtml(t('scroll.of', { a: fmt(shown), b: fmt(total) }))}</span>`;
  }
}

function _rentAppendBatch() {
  const grid = document.getElementById('rentGrid');
  if (!grid) return;
  const pageSize = _rentRenderedCount === 0 ? RENT_PAGE_INITIAL : RENT_PAGE_SUBSEQUENT;
  const batch    = _rentFilteredData.slice(_rentRenderedCount, _rentRenderedCount + pageSize);
  if (!batch.length) return;

  // Remove sentinel before appending so cards land above it
  const oldSentinel = document.getElementById('rentSentinel');
  if (oldSentinel) oldSentinel.remove();

  // Entrance animation and score count-up: first paint of the page only.
  const animate = _rentRenderedCount === 0 && !_ringsAnimatedOnce;
  const frag = document.createDocumentFragment();
  batch.forEach(l => {
    const c = renderRentalCard(l);
    if (!animate) c.classList.add('no-anim');
    frag.appendChild(c);
  });
  grid.appendChild(frag);
  _rentRenderedCount += batch.length;
  _observeScoreRings(grid, animate);
  if (animate && ALL_RENT.length) _ringsAnimatedOnce = true;
  _updateRentScrollStatus();

  if (_rentRenderedCount < _rentFilteredData.length) {
    const s = document.createElement('div');
    s.id = 'rentSentinel';
    s.style.cssText = 'height:1px;pointer-events:none;grid-column:1/-1';
    grid.appendChild(s);

    if (_rentObserver) _rentObserver.disconnect();
    _rentObserver = new IntersectionObserver(
      entries => { if (entries[0].isIntersecting) _rentAppendBatch(); },
      { rootMargin: SCROLL_ROOT_MARGIN, threshold: 0 }
    );
    _rentObserver.observe(s);
  } else {
    if (_rentObserver) { _rentObserver.disconnect(); _rentObserver = null; }
  }
}

function renderRentals() {
  if (typeof _refreshAgencyBanners === 'function') _refreshAgencyBanners();
  const f   = getRentFilters();
  // Saved filters may still hold the removed "vs OMI" sort.
  if (!['score_total', 'comps_delta_pct', 'first_seen_date', 'rent_mo'].includes(rentSortKey)) {
    rentSortKey = 'score_total';
  }
  const asc = (rentSortKey === 'rent_mo' || rentSortKey === 'comps_delta_pct');

  // If the user is scrolled past the top of the results, bring them back so
  // the updated list starts in view instead of empty space below it.
  const chipsRow = document.getElementById('rActiveChips');
  if (chipsRow && ALL_RENT.length > 0 && activeTab === 'rentals') {
    const top = chipsRow.getBoundingClientRect().top + window.scrollY
              - (document.getElementById('filterBar')?.offsetHeight || 0) - 120;
    if (window.scrollY > top + 200) window.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
  }

  _rentFilteredData = applyRentFilters(ALL_RENT, f);
  const _sortVal = l => {
    const v = rentSortKey === 'rent_mo' ? (l.rent_mo != null ? monthlyCost(l).total : null) : l[rentSortKey];
    if (v == null) return asc ? Infinity : -Infinity;
    return rentSortKey === 'first_seen_date' ? Date.parse(v) || 0 : v;
  };
  _rentFilteredData.sort((a, b) => {
    // Discount sort: "verify first" discounts (beyond the label floor) go
    // after the believable ones instead of filling the first rows.
    if (rentSortKey === 'comps_delta_pct' && !!a.price_suspect !== !!b.price_suspect) {
      return a.price_suspect ? 1 : -1;
    }
    const av = _sortVal(a), bv = _sortVal(b);
    return (asc ? av - bv : bv - av) || ((b.score_total || 0) - (a.score_total || 0));
  });
  _rentRenderedCount = 0;

  if (_rentObserver) { _rentObserver.disconnect(); _rentObserver = null; }

  const grid = document.getElementById('rentGrid');
  grid.innerHTML = '';

  if (!_rentFilteredData.length) {
    // Three states:
    //   (a) Filters narrowed an existing dataset to zero → loosen-filters copy
    //   (b) Initial fetch in flight (_rentLoading) → branded loading shimmer
    //   (c) Fetch completed but dataset is genuinely empty → scan-now copy
    if (ALL_RENT.length) {
      grid.innerHTML = _emptyStateHtml(f);
    } else if (typeof _rentLoading !== 'undefined' && _rentLoading) {
      grid.innerHTML = _renderLumeLoading();
    } else {
      grid.innerHTML = `<div class="empty"><h3>${escapeHtml(t('empty.no_data'))}</h3><p>${escapeHtml(t('empty.no_data_hint'))}</p></div>`;
    }
    _updateRentScrollStatus();
  } else {
    _rentAppendBatch();
  }

  // Update gem filter button counts (based on full ALL_RENT, independent of other filters)
  if (ALL_RENT.length > 0) {
    // Counted over what the grid can show (excluded/fake rows never are).
    const shown   = ALL_RENT.filter(l => !l.excluded);
    const nHidden = shown.filter(l => l.hidden_gem).length;
    const nGood   = shown.filter(l => l.good_value && !l.hidden_gem).length;
    // Deal-type buttons: label + count (the count hides on narrower bars).
    ['rGemFilter', 'dGemFilter'].forEach(gid => document.querySelectorAll(`#${gid} .gem-tab`).forEach(btn => {
      const gem = btn.dataset.gem;
      const label = gem === 'hidden' ? t('gem.hidden') : gem === 'good' ? t('gem.good') : t('gem.all');
      const n = gem === 'hidden' ? nHidden : gem === 'good' ? nGood : null;
      btn.innerHTML = `${escapeHtml(label)}${n != null ? ` <span class="pill-count">${fmt(n)}</span>` : ''}`;
      btn.removeAttribute('data-i18n');
    }));
  }

  // Counts reflect the full filtered set, not just rendered cards
  renderRentFilterExtras(f, _rentFilteredData.length);
  _updateMaintenanceCounts();
  if (activeTab === 'map' && _mapDataType === 'rentals') _mapRender();

  // Shared-collection banner — render above the grid when ?c=… is active
  if (window.sharedCollection) {
    renderSharedCollectionBanner(_rentFilteredData.length, window.sharedCollection.listing_ids.length);
  }
  // ?l= deep-link highlight (single-listing share)
  handleSharedListingParam();
}

// ── Rentals — data loading ────────────────────────────────────────────────────
// Flag the loading window so renderRentals can paint a branded
// loading state instead of the "Lume hasn't scanned yet" empty
// message (which previously appeared during the initial fetch and
// implied the data was missing when it was just in flight).
let _rentLoading = false;

// Branded loading-state markup. Renders the same green-glow lume mark
// from the login screen, plus 3 shimmering skeleton cards so the user
// can see the shape of the grid before the data arrives. The pulse +
// shimmer animations are defined in CSS so they keep running even
// while the JS main thread is busy parsing the listings JSON.
function _renderLumeLoading() {
  const t1 = (typeof t === 'function')
    ? (t('loading.listings') || 'Lume is reading the listings…')
    : 'Lume is reading the listings…';
  return `
    <div class="lume-loading" role="status" aria-live="polite">
      <div class="lume-loading-mark">
        <svg width="56" height="56" viewBox="0 0 28 28" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
          <defs>
            <radialGradient id="lume-loading-glow" cx="50%" cy="50%" r="50%">
              <stop offset="40%" stop-color="#2A7A5A"/>
              <stop offset="100%" stop-color="#2A7A5A" stop-opacity="0.15"/>
            </radialGradient>
          </defs>
          <circle class="lume-loading-halo" cx="14" cy="14" r="12" fill="url(#lume-loading-glow)"/>
          <circle cx="14" cy="14" r="6"  fill="#2A7A5A"/>
        </svg>
      </div>
      <div class="lume-loading-text">${escapeHtml(t1)}</div>
      <div class="lume-loading-skeletons">
        <!-- 6 cards = 2 full rows × 3 columns on desktop, so the grid
             silhouette reads as "real listings", not a single
             centred placeholder. Mobile collapses to 1 column = 6
             stacked skeletons which is still a clear loading cue. -->
        <div class="lume-skel-card"><div class="lume-skel-photo"></div><div class="lume-skel-line w-80"></div><div class="lume-skel-line w-40"></div></div>
        <div class="lume-skel-card"><div class="lume-skel-photo"></div><div class="lume-skel-line w-80"></div><div class="lume-skel-line w-40"></div></div>
        <div class="lume-skel-card"><div class="lume-skel-photo"></div><div class="lume-skel-line w-80"></div><div class="lume-skel-line w-40"></div></div>
        <div class="lume-skel-card"><div class="lume-skel-photo"></div><div class="lume-skel-line w-80"></div><div class="lume-skel-line w-40"></div></div>
        <div class="lume-skel-card"><div class="lume-skel-photo"></div><div class="lume-skel-line w-80"></div><div class="lume-skel-line w-40"></div></div>
        <div class="lume-skel-card"><div class="lume-skel-photo"></div><div class="lume-skel-line w-80"></div><div class="lume-skel-line w-40"></div></div>
      </div>
    </div>`;
}

// Archived listings are only loadable from the local API (Supabase serves
// live rows only), so "Show expired" exists only there.
function _rentWithStale() {
  return IS_LIVE && !!document.getElementById('rShowStale')?.checked;
}

// Callers fire early (a restored Stats/Map tab, tab switches) before the
// page knows whether the local API is up; loading then would take the
// Supabase path even on the local dashboard. Until init sets _liveKnown,
// calls wait; concurrent calls share one load.
let _liveKnown = false;
let _rentLoadPromise = null;

function loadRentals() {
  if (!_liveKnown) return Promise.resolve();      // init calls it once it knows
  if (!_rentLoadPromise) {
    _rentLoadPromise = _loadRentalsNow().finally(() => { _rentLoadPromise = null; });
  }
  return _rentLoadPromise;
}

async function _loadRentalsNow() {
  // Three paths, tried in priority order:
  //   1. Local dev (IS_LIVE) — Flask /rentals serves the freshest scanner
  //      output from dashboard/rentals_latest.json. Best for testing a
  //      just-run scan before sync. Skipped if Flask returns [] (the
  //      local JSON was never written, or got cleaned up after sync).
  //   2. Supabase REST — sole source on the deployed dashboard, and the
  //      local fallback when Flask has nothing to serve.
  //   3. Empty grid + diagnostic meta line — Supabase isn't configured.
  _rentLoading = true;
  // Paint the loading state immediately so the user sees the brand
  // animation rather than the empty placeholder for the few seconds
  // it takes Supabase to respond.
  if (typeof renderRentals === 'function' && !ALL_RENT.length) renderRentals();
  const _supabaseReady = typeof SupabaseClient !== 'undefined' && SupabaseClient.configured;
  let raw = null;
  if (IS_LIVE) {
    try {
      // Per-city: Flask's /rentals serves {city}_rentals_latest.json. Without
      // this query param it would always return the legacy Milan-only file
      // and the dashboard would show Milan listings when the user picked
      // Roma/Napoli/etc.
      // visible=1: the file also keeps ~18k archived rows the grid never
      // shows; the server drops them (and gzips) — ~4 MB instead of ~90.
      // "Show expired" (local only) asks for the archived rows too.
      const _city = (typeof currentCity === 'string' && currentCity) || 'milano';
      // slim=1: photos, comparable ids and score reasons come per listing
      // when its Details opens (/rentals/extras) — less than half the bytes.
      const resp = await fetch(`/rentals?city=${encodeURIComponent(_city)}&slim=1${_rentWithStale() ? '' : '&visible=1'}`);
      if (resp.ok) raw = await resp.json();
    } catch { /* fall through to Supabase */ }
  }
  let _partial = false;
  if ((!raw || !raw.length) && _supabaseReady) {
    // Paint the first 1,000 (best-scored) as soon as they arrive.
    raw = await SupabaseClient.fetchAll('rental', {
      onFirstPage: rows => {
        if (ALL_RENT.length) return;
        ALL_RENT = _dedupListings(rows.filter(l => !l.is_stale), true);
        _rentLoading = false;
        renderRentals();
      },
    });
    _partial = SupabaseClient.lastLoadPartial;
    if (!raw) {
      const meta = document.getElementById('meta');
      if (meta) meta.textContent = t('error.supabase');
      raw = [];
    }
  } else if (!raw) {
    const meta = document.getElementById('meta');
    if (meta) meta.textContent = t('error.supabase_unconfigured');
    raw = [];
  }
  // Archived rows out BEFORE merging duplicates — otherwise an archived copy
  // could win a duplicate group and hide the live listing.
  ALL_RENT = _dedupListings(_rentWithStale() ? raw : raw.filter(l => !l.is_stale), true);
  rentFasciaRanges = computeRentFasciaRanges(ALL_RENT);
  if (typeof onDataLoaded === 'function') onDataLoaded();
  else _populateOmiZoneFilter(ALL_RENT);
  // Clear the loading flag BEFORE renderRentals so the empty-state
  // message switches from "loading…" animation back to the real
  // "Nothing matches yet" / "Lume hasn't scanned yet" copy.
  _rentLoading = false;
  renderRentals();
  _updateUnmatchedBadge();
  // Replace the "Lume is reading the listings…" placeholder with the real
  // count. Skipped on the failure paths above — those already wrote a
  // diagnostic into #meta and we don't want to clobber it. fmtDate() picks
  // the locale ("15 mag 2026" in Italian, "15 May 2026" in English).
  _rentLoadPartial = _partial;
  renderHeaderMeta();
  // A tab restored on reload (Stats, Map) rendered before the data was in.
  if (typeof activeTab !== 'undefined') {
    if (activeTab === 'insights' && typeof showInsightsTab === 'function') showInsightsTab();
    if (activeTab === 'map' && typeof _mapRender === 'function') _mapRender();
  }
}

let _rentLoadPartial = false;

// Tools → Data enrichment: zone-by-name and location-data coverage.
function _updateMaintenanceCounts() {
  const fb = document.getElementById('rFallbackCount');
  if (fb) fb.textContent = fmt(ALL_RENT.filter(l => l.omi_fallback).length);
  const geoCovEl = document.getElementById('geoCoverageCount');
  if (!geoCovEl) return;
  const geoTotal  = ALL_RENT.length;
  const geoHaveIt = ALL_RENT.filter(l => l.metro_nearest_dist_m != null).length;
  geoCovEl.textContent = `${fmt(geoHaveIt)} / ${fmt(geoTotal)}`;
}

// Empty state: name the filter that removes the most results, with a
// one-click way to drop it ("0 with 'Up to €1' — Remove: 812 flats").
function _emptyStateHtml(f) {
  const chips = rentActiveFilterChips(f);
  let best = null;
  for (const c of chips) {
    const g = _filtersWithout(f, c.key);
    if (!g) continue;
    const n = applyRentFilters(ALL_RENT, g).length;
    if (!best || n > best.n) best = { ...c, n };
  }
  const tip = best && best.n > 0
    ? `<p>${escapeHtml(t('empty.blocker', { f: best.label }))}</p>
       <div class="empty-actions">
         <button type="button" class="btn btn-primary" data-clear-empty="${escapeHtml(best.key)}">${escapeHtml(t('empty.remove', { n: fmt(best.n) }))}</button>
         <button type="button" class="btn btn-ghost" data-clear-empty="all">${escapeHtml(t('filter.clear_filters'))}</button>
       </div>`
    : `<p>${escapeHtml(t('empty.loosen'))}</p>`;
  return `<div class="empty"><h3>${escapeHtml(t('empty.no_results'))}</h3>${tip}</div>`;
}

// A copy of the filter state f without one filter (chip key) — null when
// that filter isn't part of f.
function _filtersWithout(f, key) {
  const g = { ...f, must: [...(f.must || [])] };
  if (key.startsWith('must:')) {
    const k = key.slice(5);
    g.must = g.must.filter(x => x !== k);
    if (k === 'lift') g.elevator = false;
    return g;
  }
  const blank = { areas: [], maxRent: 0, minRooms: 0, minSqm: 0, gemFilter: '', includeUnknown: false,
                  condition: '', minDiscount: 0, contract: '', fascia: '', maxMetro: 0, minFloor: -999,
                  source: '', minEnergy: '', minScore: 0, fListed: '', idQuery: '' };
  if (!(key in blank)) return null;
  g[key] = blank[key];
  return g;
}

// Header line "N listings in Milan · date". Its data-i18n (the loading
// text) is dropped once data is in, so a later translation pass — the
// start-up one, or a language switch — doesn't put "reading…" back.
function renderHeaderMeta() {
  const meta = document.getElementById('meta');
  if (!meta || !ALL_RENT.length) return;
  meta.removeAttribute('data-i18n');
  meta.textContent = t('header.listings_count', {
    n:    fmt(ALL_RENT.filter(l => !l.excluded).length),
    city: _cityDisplayName(_currentCityConfig()),
    date: fmtDate(new Date()),
  }) + (_rentLoadPartial ? ' · ' + t('error.partial') : '');
}

function _updateUnmatchedBadge() {
  // Count listings using keyword fallback instead of polygon match
  const n = ALL_RENT.filter(l => l.omi_fallback).length;
  const badge = document.getElementById('unmatchedBadge');
  if (!badge) return;
  if (n > 0) {
    badge.textContent = n;
    badge.style.display = '';
  } else {
    badge.style.display = 'none';
  }
}
