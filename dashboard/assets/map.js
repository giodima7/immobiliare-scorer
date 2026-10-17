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

// ── Marker colours: the same score classes as the cards (scoreClass) ─────────
// Canvas markers (phones) need plain colours, read from the theme tokens.
function _mapColor(l) {
  if (l.hidden_gem) return cssVar('--gem');
  if (l.good_value) return cssVar('--green-ink');
  return cssVar({ high: '--green-deep', mid: '--amber', low: '--coral' }[scoreClass(l.score_total ?? 0)]);
}

// ── Marker factory ────────────────────────────────────────────────────────────
function _createMapMarker(l, lat, lng) {
  if (_isMobile) {
    // Canvas circleMarker — all markers share one <canvas> DOM node
    const isGem  = l.hidden_gem;
    return L.circleMarker([lat, lng], {
      renderer:    _mapRenderer,
      radius:      isGem ? 9 : 7,
      fillColor:   _mapColor(l),
      fillOpacity: 0.95,
      color:       '#fff',
      weight:      isGem ? 2.5 : 1.5,
    });
  }
  // Desktop: diamond = Hidden Gem, coin = Great Value, number = score.
  const score = l.score_total ?? 0;
  const html = l.hidden_gem
    ? `<div class="map-marker mk-gem"><span>${icon('diamond')}</span></div>`
    : l.good_value
    ? `<div class="map-marker mk-good">${icon('coin')}</div>`
    : `<div class="map-marker sc-${scoreClass(score)}">${score}</div>`;
  return L.marker([lat, lng], {
    title: `${formatAddress(l.neighbourhood || '')} · €${fmt(monthlyCost(l).total)}`,
    icon: L.divIcon({ className: '', html, iconSize: [32, 32], iconAnchor: [16, 16], popupAnchor: [0, -18] }),
  });
}

// ── Popup factory ─────────────────────────────────────────────────────────────
function _createMapPopup(l) {
  const score = l.score_total ?? 0;
  const nb    = formatAddress(l.neighbourhood || l.address || '–');
  const thumb = l.thumbnail ? l.thumbnail.replace('/xxs-c.', '/xs-c.') : null;
  const img   = thumb ? `<img class="map-popup-img" src="${_safeUrl(thumb)}" onerror="this.style.display='none'" alt="">` : '';
  const dp    = l.comps_delta_pct;
  const comps = dp != null
    ? `<div class="map-popup-comps ${dp <= -5 ? 'below' : dp >= 5 ? 'above' : 'at'}">${escapeHtml(t('card.vs_nearby', { pct: fmtPct(dp) }))}</div>` : '';
  const specs = _specsParts(l);
  const rawAddr = String(l.address || '').trim()
    .replace(/,?\s*milano\s*$/i, '').replace(/,?\s*\d{5}\s*$/i, '').trim();
  const sub = rawAddr && rawAddr.toLowerCase() !== String(l.neighbourhood || '').toLowerCase() ? formatAddress(rawAddr) : '';
  return `
    ${img}
    <div class="map-popup-body">
      <div class="map-popup-head">
        <div class="map-popup-score sc-${scoreClass(score)}">${score}</div>
        <div style="min-width:0">
          <div class="map-popup-title">${escapeHtml(nb)}</div>
          ${sub ? `<div class="map-popup-sub">${escapeHtml(sub)}</div>` : ''}
        </div>
      </div>
      <div class="map-popup-price">€${fmt(monthlyCost(l).total)}<span class="price-unit"> ${escapeHtml(t(monthlyCost(l).known ? 'cost.all_in' : 'cost.rent_only'))}</span></div>
      ${comps}
      ${specs.length ? `<div class="map-popup-grid">${specs.map(x => `<span>${escapeHtml(x)}</span>`).join('')}</div>` : ''}
      <div class="map-popup-actions">
        <button type="button" class="btn btn-secondary btn-sm" data-open-details="${escapeHtml(String(l.id))}">${escapeHtml(t('card.details'))}</button>
        <a class="btn btn-primary btn-sm map-popup-link" href="${_safeUrl(l.url)}" target="_blank" rel="noopener">${escapeHtml(t('card.open'))}</a>
      </div>
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
        : `<div style="padding:12px;color:var(--text-secondary)">${escapeHtml(t('map.gone'))}</div>`;
    }, { maxWidth: 300, minWidth: 260 });

    _clusterGroup.addLayer(marker);
    _mapRenderedIds.add(id);
  });

  const capEl = document.getElementById('mapCapNotice');
  if (capEl) capEl.hidden = !capped;
}

// The map shows exactly what the Rentals list shows (same filter bar).
function _getMapData() {
  return applyRentFilters(ALL_RENT, getRentFilters());
}

// The map fills the window below the filter bar and toolbar.
function _sizeMapView() {
  const box = document.getElementById('mapContainer');
  if (!box || box.offsetParent === null) return;
  const top = box.getBoundingClientRect().top;
  box.style.height = Math.max(360, window.innerHeight - top - 16) + 'px';
  if (_map) _map.invalidateSize();
}

// Dark theme: invert the light map tiles (no second tile set needed).
function _mapApplyTheme() {
  document.getElementById('mapContainer')?.classList.toggle('map-dark', isDarkTheme());
  if (_map && _isMobile) _mapRender();
}

function _mapInit() {
  if (_mapInitialised) return;
  _mapInitialised = true;

  _mapRenderer = _isMobile ? L.canvas({ padding: 0.5 }) : L.svg();
  const [_mapLat0, _mapLng0, _mapZ0] = (typeof getMapDefaultView === 'function')
    ? getMapDefaultView() : [45.4642, 9.1900, 12];
  _map = L.map('mapContainer').setView([_mapLat0, _mapLng0], _mapZ0);

  // OpenStreetMap's standard tiles (CARTO's basemaps now need an API key).
  // Lighter options on mobile: load only when panning stops, minimal buffer.
  const tileOpts = {
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    maxZoom: 19,
    ..._isMobile ? { updateWhenIdle: true, keepBuffer: 1 } : {},
  };
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', tileOpts).addTo(_map);
  _mapApplyTheme();

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
        html: `<div class="map-cluster" style="width:${size}px;height:${size}px">${fmt(count)}</div>`,
        className: '',
        iconSize:   [size, size],
        iconAnchor: [size / 2, size / 2],
      });
    },
  });
  _map.addLayer(_clusterGroup);

  // Viewport rendering: debounced on mobile to absorb momentum-scroll flurry
  const onMove = _isMobile
    ? _mapDebounce(_renderVisibleMarkers, 150)
    : _renderVisibleMarkers;
  _map.on('moveend', onMove);
  _map.on('zoomend', onMove);

  window.addEventListener('resize', _mapDebounce(_sizeMapView, 150));
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

  // Render markers currently in viewport (moveend will add more as user pans)
  _renderVisibleMarkers();

  document.getElementById('mapListingCount').textContent =
    t(_mapAllListings.length === 1 ? 'map.count_one' : 'map.count', { n: fmt(_mapAllListings.length) });
  _sizeMapView();

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
