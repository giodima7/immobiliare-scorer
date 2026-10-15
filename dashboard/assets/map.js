// Lume dashboard — Map tab.
// Plain script (no bundler); shares globals with the other assets/*.js files.
// Declarations only: code that runs at start-up lives in init.js, which loads
// last, so any function here may call any other.

// ── Map tab (item 9) ──────────────────────────────────────────────────────────
// PERF targets (DevTools mobile emulation, iPhone 14, 4× CPU throttle):
//   - Tab tap → map visible:           < 1.5s
//   - Pan gesture end → markers settled: < 500ms
//   - DOM nodes (marker-related):       < 500

const _isMobile = window.innerWidth <= 768;
let _mapInitialised = false;
let _map            = null;
let _clusterGroup   = null;
let _mapRenderer    = null;
let _mapDataType    = 'rentals';  // 'rentals' | 'sales'
let _mapToggleBound = false;
let _mapAllListings = [];         // geo-valid dataset for viewport rendering
let _mapRenderedIds = new Set();  // IDs already added to cluster

// ── Debounce helper ──────────────────────────────────────────────────────────
function _mapDebounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// ── Score colour ─────────────────────────────────────────────────────────────
function _mapScoreColor(score) {
  if (score >= 70) return '#16A34A';
  if (score >= 45) return '#D97706';
  return '#DC2626';
}

// ── Marker factory ────────────────────────────────────────────────────────────
function _createMapMarker(l, lat, lng) {
  if (_isMobile) {
    // Canvas circleMarker — all markers share one <canvas> DOM node
    const score  = l.score_total ?? 0;
    const isGem  = l.hidden_gem;
    return L.circleMarker([lat, lng], {
      renderer:    _mapRenderer,
      radius:      isGem ? 9 : 7,
      fillColor:   isGem ? '#15803D' : _mapScoreColor(score),
      fillOpacity: 0.9,
      color:       'white',
      weight:      isGem ? 2.5 : 1.5,
    });
  }
  // Desktop: divIcon with score bubble (unchanged behaviour)
  const score    = l.score_total ?? 0;
  const color    = _mapScoreColor(score);
  const isGem    = l.hidden_gem;
  const isGoodV  = l.good_value && !l.hidden_gem;
  const iconHtml = isGem
    ? `<div style="width:34px;height:34px;background:var(--green-ink);color:#fff;font-size: 14px;display:flex;align-items:center;justify-content:center;box-shadow:0 2px 8px rgba(0,0,0,0.35);border:2px solid rgba(255,255,255,0.9);border-radius:4px;transform:rotate(45deg)"><span style="transform:rotate(-45deg)">✦</span></div>`
    : isGoodV
    ? `<div style="width:34px;height:34px;background:var(--blue-ink);color:#fff;font-weight:800;font-size:13px;display:flex;align-items:center;justify-content:center;box-shadow:0 2px 8px rgba(0,0,0,0.30);border:2px solid rgba(255,255,255,0.9);border-radius:50%">↑</div>`
    : `<div style="width:34px;height:34px;border-radius:50%;background:${color};color:#fff;font-weight:800;font-size:12px;display:flex;align-items:center;justify-content:center;box-shadow:0 2px 8px rgba(0,0,0,0.30);border:2px solid rgba(255,255,255,0.85)">${score}</div>`;
  return L.marker([lat, lng], {
    icon: L.divIcon({
      className:   '',
      html:        iconHtml,
      iconSize:    [34, 34],
      iconAnchor:  [17, 17],
      popupAnchor: [0, -20],
    })
  });
}

// ── Popup factory ─────────────────────────────────────────────────────────────
function _createMapPopup(l, isRentalArg) {
  // isRentalArg: optional override. The main Map tab uses the global
  // _mapDataType, but the detail-page mini-map needs to render mixed
  // marker types (the listing as a sale, plus rental comps) so it passes
  // the type explicitly per marker.
  const score    = l.score_total ?? 0;
  const isRental = (typeof isRentalArg === 'boolean')
    ? isRentalArg
    : (_mapDataType === 'rentals');

  // Full popup — same on all screen sizes
  const sc       = scoreClass(score);
  const nb       = formatAddress(l.neighbourhood || l.address || '–');
  const thumb    = l.thumbnail ? l.thumbnail.replace('/xxs-c.', '/xs-c.') : null;
  const imgHtml  = thumb
    ? `<img class="map-popup-img" src="${_safeUrl(thumb)}" onerror="this.style.display='none'" alt="">`
    : '';
  const priceStr = isRental
    ? (l.rent_mo != null ? `€${fmt(l.rent_mo)}/mo` : '–')
    : (l.price   != null ? `€${fmt(l.price)}`       : '–');
  let compsHtml = '';
  if (isRental && l.comps_delta_pct != null) {
    const dp   = l.comps_delta_pct;
    const cls  = dp <= -5 ? 'below' : dp >= 5 ? 'above' : 'at';
    const sign = dp > 0 ? '+' : '';
    const n    = l.comps_n || 0;
    compsHtml = `<div class="map-popup-comps ${cls}">${sign}${dp.toFixed(1)}% vs ${n} comps</div>`;
  } else if (!isRental && l.comps_sale_delta_pct != null) {
    const dp   = l.comps_sale_delta_pct;
    const cls  = dp <= -5 ? 'below' : dp >= 5 ? 'above' : 'at';
    const sign = dp > 0 ? '+' : '';
    const n    = l.comps_sale_n || 0;
    compsHtml = `<div class="map-popup-comps ${cls}">${sign}${dp.toFixed(1)}% vs ${n} sale comps</div>`;
  } else if (!isRental && l.vs_omi_label) {
    const cls = (l.vs_omi_pct ?? 0) <= -5 ? 'below' : (l.vs_omi_pct ?? 0) >= 5 ? 'above' : 'at';
    compsHtml = `<div class="map-popup-comps ${cls}">${escapeHtml(l.vs_omi_label)}</div>`;
  }
  const rows = [];
  if (l.rooms)                rows.push(['bed', `${l.rooms} rooms`]);
  if (l.sqm)                  rows.push(['ruler-2', `${fmt(l.sqm)} m²${l.ask_psqm ? ' · €' + l.ask_psqm.toFixed(1) + '/m²' + (isRental ? '/mo' : '') : ''}`]);
  const floorLabel = l.floor === 'T' ? 'Ground' : l.floor === 'R' ? 'Rialzato' : l.floor ? `Floor ${l.floor}` : null;
  if (floorLabel)             rows.push(['building', floorLabel]);
  if (l.metro_nearest_dist_m) rows.push(['train', `${l.metro_nearest_dist_m}m metro`]);
  if (l.condition)            rows.push(['home', l.condition]);
  if (l.energy_class)         rows.push(['bolt', `Class ${l.energy_class}`]);
  if (l.elevator === true)    rows.push(['elevator', 'Elevator']);
  if (l.has_balcony === true) rows.push(['plant', 'Balcony']);
  const gridHtml = rows.length
    ? `<div class="map-popup-grid">${rows.map(([ic, txt]) => `<span>${icon(ic)} ${escapeHtml(txt)}</span>`).join('')}</div>`
    : '';
  // Popup sub-line: prefer the cleaned-up street address. Drop trailing
  // city name + postcode that Immobiliare/Idealista append, since the
  // city is implied by the map and the postcode is dead weight.
  // NEVER fall back to omi_descr here — that field is an ALL-CAPS OMI
  // zone label ("NAVIGLI, CORSO GENOVA") and reads as garbage when
  // shown as if it were an address.
  const rawAddr = String(l.address || '').trim();
  const subAddr = rawAddr
    .replace(/,?\s*(?:milano|roma|napoli|la\s*maddalena)\s*$/i, '')
    .replace(/,?\s*\d{5}\s*$/i, '')
    .trim();
  const zoneSub = (subAddr && subAddr.toLowerCase() !== String(l.neighbourhood || '').toLowerCase())
    ? formatAddress(subAddr)
    : '';
  return `
    ${imgHtml}
    <div class="map-popup-body">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
        <div class="map-popup-score ${sc}">${score}</div>
        <div style="min-width:0">
          <div class="map-popup-title">${escapeHtml(nb)}</div>
          ${zoneSub ? `<div class="map-popup-sub">${escapeHtml(zoneSub)}</div>` : ''}
        </div>
      </div>
      <div class="map-popup-price">${priceStr}</div>
      ${compsHtml}
      ${gridHtml}
      <a class="map-popup-link" href="${_safeUrl(l.url)}" target="_blank" rel="noopener">→ View listing</a>
    </div>`;
}

// ── Viewport-based marker rendering ──────────────────────────────────────────
const _MOBILE_MARKER_CAP = 400; // PERF: cap prevents > ~500 marker DOM nodes on mobile

function _renderVisibleMarkers() {
  if (!_map || !_clusterGroup) return;
  const bounds = _map.getBounds().pad(0.2); // 20% buffer beyond viewport

  let visible = _mapAllListings.filter(l => {
    const lat = l.latitude ?? l.lat;
    const lng = l.longitude ?? l.lng ?? l.lon;
    return bounds.contains([lat, lng]);
  });

  // Cap to top-scored listings on mobile to keep DOM lean
  let capped = false;
  if (_isMobile && visible.length > _MOBILE_MARKER_CAP) {
    visible = visible.slice().sort((a, b) => (b.score_total ?? 0) - (a.score_total ?? 0)).slice(0, _MOBILE_MARKER_CAP);
    capped = true; // PERF: if this fires often, consider reducing _MOBILE_MARKER_CAP
  }

  visible.forEach(l => {
    const id = l.id ?? l.url;
    if (_mapRenderedIds.has(id)) return;
    const lat = l.latitude ?? l.lat;
    const lng = l.longitude ?? l.lng ?? l.lon;
    const marker = _createMapMarker(l, lat, lng);

    // Store the listing ID as a primitive on the marker, then generate
    // the popup lazily by looking the listing up in _mapAllListings at
    // popup-open time. This is immune to closure bugs (the captured value
    // is a string, not an object reference) and stays correct even if
    // _mapAllListings is replaced/filtered between marker creation and
    // popup display. Always coerce to string when comparing so the lookup
    // works for both numeric (Immobiliare) and prefixed-string (Idealista)
    // listing IDs.
    marker.listingId = String(id);
    marker.bindPopup(() => {
      const found = _mapAllListings.find(x => String(x.id ?? x.url) === marker.listingId);
      return found
        ? _createMapPopup(found)
        : '<div style="padding:12px;color:var(--text-secondary);font-size:12px">Listing no longer in view</div>';
    }, { maxWidth: 300, minWidth: 260 });

    _clusterGroup.addLayer(marker);
    _mapRenderedIds.add(id);
  });

  const capEl = document.getElementById('mapCapNotice');
  if (capEl) capEl.style.display = capped ? '' : 'none';
}

function _getMapRentFilters() {
  return {
    areas:     mapSelectedAreas,
    fascia:    document.getElementById('mRentFascia')?.value || '',
    omiZone:   '',
    maxRent:   parseInt(document.getElementById('mRentMaxRent')?.value)   || 0,
    minSqm:    parseInt(document.getElementById('mRentMinSqm')?.value)    || 0,
    minRooms:  parseInt(document.getElementById('mRentMinRooms')?.value)  || 0,
    minScore:  parseInt(document.getElementById('mRentScore')?.value)     || 0,
    source:    '',
    maxMetro:  parseInt(document.getElementById('mRentMaxMetro')?.value)  || 0,
    minFloor:  parseInt(document.getElementById('mRentMinFloor')?.value)  || -999,
    elevator:  document.getElementById('mRentElevator')?.checked || false,
    minEnergy: document.getElementById('mRentMinEnergy')?.value || '',
    fListed:   document.getElementById('mRentListed')?.value || '',
  };
}

function _getMapData() {
  return applyRentFilters(ALL_RENT, _getMapRentFilters());
}

function _mapInit() {
  if (_mapInitialised) return;
  _mapInitialised = true;

  _mapRenderer = _isMobile ? L.canvas({ padding: 0.5 }) : L.svg();
  const [_mapLat0, _mapLng0, _mapZ0] = (typeof getMapDefaultView === 'function')
    ? getMapDefaultView() : [45.4642, 9.1900, 12];
  _map = L.map('mapContainer').setView([_mapLat0, _mapLng0], _mapZ0);

  // Lighter tile options on mobile: load only when panning stops, minimal tile buffer
  const tileOpts = {
    attribution: '© <a href="https://carto.com/">CARTO</a> · © OpenStreetMap contributors',
    maxZoom: 19,
    ..._isMobile ? { updateWhenIdle: true, keepBuffer: 1 } : {},
  };
  L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', tileOpts).addTo(_map);

  // Cluster group replaces L.layerGroup — collapses dense areas into single DOM nodes
  _clusterGroup = L.markerClusterGroup({
    maxClusterRadius:    50,
    spiderfyOnMaxZoom:   true,
    showCoverageOnHover: false,
    zoomToBoundsOnClick: true,
    iconCreateFunction: function(cluster) {
      const count = cluster.getChildCount();
      const size  = count < 10 ? 36 : count < 50 ? 42 : 48;
      return L.divIcon({
        html: `<div style="width:${size}px;height:${size}px;border-radius:50%;background:var(--green-ink);color:white;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:600;box-shadow:0 2px 8px rgba(0,0,0,0.25)">${count}</div>`,
        className: '',
        iconSize:   [size, size],
        iconAnchor: [size / 2, size / 2],
      });
    },
  });
  _map.addLayer(_clusterGroup);

  // Legend control (rentals-only — hidden on mobile via CSS; no OMI polygons on mobile)
  if (!_isMobile) {
    const _legend = L.control({ position: 'bottomleft' });
    _legend.onAdd = function() {
      const d = L.DomUtil.create('div');
      d.id = 'mapLegend';
      d.style.cssText = 'background:rgba(255,255,255,0.95);border:1px solid #e5e7eb;border-radius:8px;padding:8px 12px;font-size:11px;line-height:1.9;pointer-events:none;display:none';
      d.innerHTML = `
        <div style="display:flex;align-items:center;gap:7px"><div style="width:14px;height:14px;border-radius:50%;background:#6B7280"></div> Score</div>
        <div style="display:flex;align-items:center;gap:7px"><div style="width:14px;height:14px;background:var(--green-ink);transform:rotate(45deg);border-radius:2px;flex-shrink:0"></div> Hidden Gem</div>
        <div style="display:flex;align-items:center;gap:7px"><div style="width:14px;height:14px;border-radius:50%;background:var(--blue-ink);flex-shrink:0"></div> Great Value</div>`;
      return d;
    };
    _legend.addTo(_map);
  }

  // Viewport rendering: debounced on mobile to absorb momentum-scroll flurry
  const onMove = _isMobile
    ? _mapDebounce(_renderVisibleMarkers, 150)
    : _renderVisibleMarkers;
  _map.on('moveend', onMove);
  _map.on('zoomend', onMove);

  // Event bindings — only attach once
  if (!_mapToggleBound) {
    _mapToggleBound = true;

    const _mr = () => _mapRender();

    // Rental filter bindings
    ['mRentFascia','mRentMaxRent','mRentMinSqm','mRentMinRooms',
     'mRentMaxMetro','mRentMinFloor','mRentElevator','mRentMinEnergy','mRentListed'].forEach(id => {
      document.getElementById(id)?.addEventListener('change', _mr);
    });
    [['mRentScore', 'mRentScoreVal']].forEach(([sid, vid]) => {
      const sl = document.getElementById(sid);
      const lb = document.getElementById(vid);
      if (sl) sl.addEventListener('input', () => { if (lb) lb.textContent = sl.value; _mr(); });
    });
    document.getElementById('mRentReset')?.addEventListener('click', () => {
      ['mRentFascia','mRentMaxRent','mRentMinSqm','mRentMinRooms',
       'mRentMaxMetro','mRentMinFloor','mRentMinEnergy','mRentListed'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
      });
      const sc = document.getElementById('mRentScore');
      const lb = document.getElementById('mRentScoreVal');
      if (sc) { sc.value = 0; if (lb) lb.textContent = '0'; }
      const ev = document.getElementById('mRentElevator');
      if (ev) ev.checked = false;
      mapSelectedAreas = [];
      mapAreaMsd.clear();
      _mr();
    });
  }
}

// Per-city tight bboxes for map filtering. Matches enrich_geo._CITY_BBOXES
// exactly so the dashboard and the geocoder agree on what counts as
// "inside the city". The old map filter used a loose 44–47°/8–11° box
// that covered most of Lombardy + Piedmont, so listings whose
// coordinates drifted to Bollate / Sesto / Cornaredo / Corsico etc.
// got plotted on the Milan map.
const _CITY_MAP_BBOXES = {
  milano:       [45.38,  9.02, 45.56,  9.32],
  roma:         [41.75, 12.30, 42.00, 12.70],
  napoli:       [40.78, 14.14, 40.92, 14.38],
  la_maddalena: [41.17,  9.35, 41.25,  9.47],
};

function _coordsInsideCity(lat, lng, city) {
  const bb = _CITY_MAP_BBOXES[city] || _CITY_MAP_BBOXES.milano;
  return lat >= bb[0] && lat <= bb[2] && lng >= bb[1] && lng <= bb[3];
}

function _mapRender() {
  if (!_map) return;

  // Refresh dataset and reset viewport rendering state.
  // Two filter passes:
  //   1. Coordinates must be inside the active city's bbox — kills
  //      the suburb pins (Sesto / Bollate / Corsico etc.) the old
  //      44–47° loose filter let through.
  //   2. Listings whose OMI lookup landed via 'centroid' fallback
  //      (instead of a real polygon match) are also dropped — that's
  //      where geocoder-drifted suburb addresses end up even when
  //      their coords technically fall inside the loose bbox.
  const _city = (typeof currentCity === 'string' && currentCity) || 'milano';
  _mapAllListings = _getMapData().filter(l => {
    const lat = l.latitude ?? l.lat;
    const lng = l.longitude ?? l.lng ?? l.lon;
    if (!lat || !lng) return false;
    if (!_coordsInsideCity(lat, lng, _city)) return false;
    // omi_source values that count as "really in the city":
    //   'polygon'                  — direct hit on an OMI zone polygon
    //   'nominatim+polygon'        — geocoded then polygon-validated
    // 'centroid' / 'nominatim+centroid' / 'failed' / 'no_coordinates'
    // all indicate the address didn't actually polygon-match — drop
    // them. Listings without any omi_source (older rows) keep working
    // because the bbox check above handles the obvious suburbs.
    const src = (l.omi_source || '').toLowerCase();
    if (src.includes('centroid') || src === 'failed' || src === 'no_coordinates') {
      return false;
    }
    return true;
  });
  _mapRenderedIds.clear();
  _clusterGroup.clearLayers();

  // Diagnostic: open DevTools console to verify the right city's data
  // is on screen. Switching cities should print a different count here
  // — if it doesn't, the data pipeline forgot to re-filter by city.
  console.log(`[map] rendering ${_mapAllListings.length} ${_mapDataType} markers for city=${_city}`);

  // Render markers currently in viewport (moveend will add more as user pans)
  _renderVisibleMarkers();

  document.getElementById('mapListingCount').textContent =
    `${_mapAllListings.length} ${_mapAllListings.length === 1 ? 'listing' : 'listings'}`;
  const _legendEl = document.getElementById('mapLegend');
  if (_legendEl) _legendEl.style.display = _mapDataType === 'rentals' ? '' : 'none';

  // Fit map to full dataset bounds
  setTimeout(() => {
    _map.invalidateSize();
    if (_mapAllListings.length) {
      const coords = _mapAllListings.map(l => [l.latitude ?? l.lat, l.longitude ?? l.lng ?? l.lon]);
      _map.fitBounds(coords, { padding: [24, 24], maxZoom: 14 });
    } else {
      const [_dLat, _dLng, _dZ] = (typeof getMapDefaultView === 'function')
        ? getMapDefaultView() : [45.4642, 9.1900, 12];
      _map.setView([_dLat, _dLng], _dZ);
    }
  }, 50);
}
