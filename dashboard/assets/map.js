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
let _mapDataType    = Storage.get('mapType', 'rentals') === 'sales' ? 'sales' : 'rentals';
let _mapView        = Storage.get('mapView', 'pins') === 'prices' ? 'prices' : 'pins';
let _priceLayer     = null;           // the price grid (Prices view)
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
    title: `${formatAddress(l.neighbourhood || '')} · €${fmt(_mapDataType === 'sales' ? l.price : monthlyCost(l).total)}`,
    icon: L.divIcon({ className: '', html, iconSize: [32, 32], iconAnchor: [16, 16], popupAnchor: [0, -18] }),
  });
}

// ── Popup factory ─────────────────────────────────────────────────────────────
function _createMapPopup(l) {
  const score = l.score_total ?? 0;
  const sale  = _mapDataType === 'sales';
  const nb    = formatAddress(l.neighbourhood || l.address || '–');
  const thumb = l.thumbnail ? l.thumbnail.replace('/xxs-c.', '/xs-c.') : null;
  const img   = thumb ? `<img class="map-popup-img" src="${_safeUrl(thumb)}" onerror="this.style.display='none'" alt="">` : '';
  // The card's line: a suspect price reads "verify", never a green discount.
  const delta = sale ? _saleDeltaHtml(l) : _dealDeltaHtml(l);
  const comps = delta ? `<div class="map-popup-comps">${delta}</div>` : '';
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
      <div class="map-popup-price">${sale
        ? `€${fmt(l.price)}${l.sqm ? `<span class="price-unit"> · €${fmt(Math.round(l.price / l.sqm))}/m²</span>` : ''}`
        : `€${fmt(monthlyCost(l).total)}<span class="price-unit"> ${escapeHtml(t(monthlyCost(l).known ? 'cost.all_in' : 'cost.rent_only'))}</span>`}</div>
      ${comps}
      ${specs.length ? `<div class="map-popup-grid">${specs.map(x => `<span>${escapeHtml(x)}</span>`).join('')}</div>` : ''}
      <div class="map-popup-actions">
        <button type="button" class="btn btn-secondary btn-sm" ${sale ? 'data-open-sale' : 'data-open-details'}="${escapeHtml(String(l.id))}">${escapeHtml(t('card.details'))}</button>
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

// The map shows exactly what the Rentals list shows (same filter bar) — or,
// switched to sales, what For sale shows.
function _getMapData() {
  if (_mapDataType === 'sales') {
    const f = saleFilters();
    return ALL_SALE.filter(l => _saleMatches(l, f));
  }
  return applyRentFilters(ALL_RENT, getRentFilters());
}

// Rentals or sales: render, loading them first when the tab hasn't yet.
// Sales render through renderSales, which syncs their bar and redraws the map.
function _mapShow() {
  document.querySelectorAll('[data-map-type]').forEach(b => {
    const on = b.dataset.mapType === _mapDataType;
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', String(on));
  });
  _mapPlaceSaleBar();
  if (_mapDataType === 'sales') { if (ALL_SALE.length) renderSales(); else loadSales(); return; }
  if (ALL_RENT.length) _mapRender();
  else loadRentals().then(() => { if (activeTab === 'map') _mapRender(); });
}

// Sales on the map: For sale's own filter bar and chips move above it (their
// controls keep their listeners) and go back when the map shows rentals or
// another tab opens; the Rentals bar hides meanwhile (lume.css, data-map).
const _mapSaleHomes = {};
function _mapPlaceSaleBar() {
  const toolbar = document.querySelector('#mapView .map-toolbar');
  if (!toolbar) return;
  const onMap = activeTab === 'map' && _mapDataType === 'sales';
  document.body.dataset.map = _mapDataType;
  for (const id of ['saleFilterBar', 'sActiveChips']) {
    const el = document.getElementById(id);
    if (!el) continue;
    if (onMap && el.parentElement !== toolbar.parentElement) {
      if (!_mapSaleHomes[id]) { _mapSaleHomes[id] = document.createComment(id); el.before(_mapSaleHomes[id]); }
      toolbar.before(el);
    } else if (!onMap && _mapSaleHomes[id] && el.parentElement === toolbar.parentElement) {
      _mapSaleHomes[id].after(el);
    }
  }
}

// The map fills the window below the filter bar and toolbar.
function _sizeMapView() {
  const box = document.getElementById('mapContainer');
  if (!box || box.offsetParent === null) return;
  const top = box.getBoundingClientRect().top;
  box.style.height = Math.max(360, window.innerHeight - top - 16) + 'px';
  if (_map) _map.invalidateSize();
}

// Esri's grey canvas basemap (raster, no key): Light Gray in the light theme,
// Dark Gray in the dark one, street names on a second layer above. Until
// 2026-10-24 OpenStreetMap's standard tiles, colour-inverted in the dark
// theme — busy and garish. Not CARTO (answers "API key required") nor
// OpenFreeMap's vector tiles (MapLibre froze the whole page).
let _tileLayer = null, _labelLayer = null;
const _ESRI = (name, kind) =>
  `https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_${name}_Gray_${kind}/MapServer/tile/{z}/{y}/{x}`;

function _mapApplyTheme() {
  const name = isDarkTheme() ? 'Dark' : 'Light';
  _tileLayer?.setUrl(_ESRI(name, 'Base'));
  _labelLayer?.setUrl(_ESRI(name, 'Reference'));
  if (_map && (_isMobile || _mapView === 'prices')) _mapRender();
}

function _mapInit() {
  if (_mapInitialised) return;
  _mapInitialised = true;

  _mapRenderer = _isMobile ? L.canvas({ padding: 0.5 }) : L.svg();
  const [_mapLat0, _mapLng0, _mapZ0] = (typeof getMapDefaultView === 'function')
    ? getMapDefaultView() : [45.4642, 9.1900, 12];
  _map = L.map('mapContainer').setView([_mapLat0, _mapLng0], _mapZ0);

  // Lighter options on mobile: load only when panning stops, minimal buffer.
  // The canvas tiles stop at zoom 16; Leaflet scales them up beyond.
  const tileOpts = {
    maxNativeZoom: 16, maxZoom: 19,
    ..._isMobile ? { updateWhenIdle: true, keepBuffer: 1 } : {},
  };
  const name = isDarkTheme() ? 'Dark' : 'Light';
  _tileLayer = L.tileLayer(_ESRI(name, 'Base'), {
    ...tileOpts,
    attribution: 'Tiles © <a href="https://www.esri.com">Esri</a> — Esri, HERE, Garmin, © OpenStreetMap contributors',
  }).addTo(_map);
  // Street names in a pane of their own: above the price grid in the Prices
  // view (its cells hid them), under the pins otherwise.
  _map.createPane('mapLabels').style.pointerEvents = 'none';
  _labelLayer = L.tileLayer(_ESRI(name, 'Reference'), { ...tileOpts, pane: 'mapLabels' }).addTo(_map);

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
  const onPriceMove = _mapDebounce(() => { if (_mapView === 'prices') _mapRenderPrices(); }, 120);
  _map.on('moveend', () => { if (_mapView === 'pins') onMove(); else onPriceMove(); });
  _map.on('zoomend', () => { if (_mapView === 'pins') onMove(); });
  _map.on('click', e => {
    if (_mapView !== 'prices') return;
    L.popup({ maxWidth: 240 }).setLatLng(e.latlng).setContent(_mapPriceAt(e.latlng)).openOn(_map);
  });
  document.querySelectorAll('[data-map-view]').forEach(b => b.addEventListener('click', () => {
    if (b.dataset.mapView === _mapView) return;
    _mapView = b.dataset.mapView;
    Storage.set('mapView', _mapView);
    _map.closePopup();
    _mapRender();
  }));

  window.addEventListener('resize', _mapDebounce(_sizeMapView, 150));
  document.querySelectorAll('[data-map-type]').forEach(b => b.addEventListener('click', () => {
    if (b.dataset.mapType === _mapDataType) return;
    _mapDataType = b.dataset.mapType;
    Storage.set('mapType', _mapDataType);
    _map.closePopup();
    _mapShow();
  }));
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
  document.querySelectorAll('[data-map-view]').forEach(b => {
    const on = b.dataset.mapView === _mapView;
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', String(on));
  });
  document.getElementById('mapView')?.setAttribute('data-view', _mapView);
  const labels = _map.getPane('mapLabels');
  if (labels) labels.style.zIndex = _mapView === 'prices' ? 450 : 250;
  const capEl = document.getElementById('mapCapNotice');
  if (_mapView === 'prices') {
    if (capEl) capEl.hidden = true;
    _mapRenderPrices();
    _sizeMapView();
    return;                          // the grid keeps the view where it is
  }
  _priceLayer?.clearLayers();

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

// ── Prices view: a smooth price surface (2026-10-31) ─────────────────────────
// Every live listing of the kind shown, not the filtered list: a price cap in
// the filter bar would paint its own ceiling. Rentals: all-in rent per m² a
// month; sales: price per m². Each point of the view gets the average of the
// listings around it, weighted by distance (a Gaussian, ~120 m wide close up,
// ~450 m zoomed out) — the user found 250–500 m squares too coarse to read a
// street and hiding the map. Where fewer than ~3 listings weigh in, nothing is
// drawn. One blue ramp (dataviz sequential), darkest = dearest, flipped in the
// dark theme; colours spread over the quantiles of what is drawn in view, so
// streets a few euros apart still differ close up — the legend says the range
// in view. Tap the map for a point's price.
const _PRICE_RAMP = ['#cde2fb', '#9ec5f4', '#6da7ec', '#3987e5', '#256abf', '#184f95', '#0d366b'];
const _PRICE_STEP = 4;                 // screen px per surface pixel (smoothed when scaled)
let _pricePoints = null, _priceAnchors = null, _priceKey = '';

function _priceSource() {
  const _city = (typeof currentCity === 'string' && currentCity) || 'milano';
  const rows = _mapDataType === 'sales' ? ALL_SALE : ALL_RENT;
  return rows.filter(l => !l.is_stale && !l.excluded && l.latitude && l.longitude
                       && l.geo_precision !== 'metro' && l.geo_precision !== 'zone'
                       && _coordsInsideCity(l.latitude, l.longitude, _city));
}

function _pricePerSqm(l) {
  if (!l.sqm || l.sqm < 20 || l.sqm > 300) return null;
  const p = _mapDataType === 'sales' ? l.price : monthlyCost(l).total;
  return p > 0 ? p / l.sqm : null;
}

// "€24,1" a month or "€6.780"; and what 60 m² comes to at that rate.
function _priceLabel(v) {
  return _mapDataType === 'sales' ? `€${fmt(Math.round(v / 10) * 10)}` : `€${fmt(Math.round(v * 10) / 10, { minimumFractionDigits: 1, maximumFractionDigits: 1 })}`;
}
function _price60(v) {
  return _mapDataType === 'sales' ? `€${fmt(Math.round(v * 60 / 1000))}k` : `€${fmt(Math.round(v * 60 / 10) * 10)}`;
}

// The listings as {lat, lng, v}, outliers clipped to the 2nd–98th percentile;
// the colour anchors at the city's quantiles. Rebuilt when the data changes.
function _pricePrep() {
  const src = _priceSource(), key = `${_mapDataType}:${src.length}:${isDarkTheme()}`;
  if (key === _priceKey && _pricePoints) return;
  const pts = [];
  for (const l of src) {
    const v = _pricePerSqm(l);
    if (v != null) pts.push({ lat: l.latitude, lng: l.longitude, v });
  }
  const vs = pts.map(p => p.v).sort((a, b) => a - b);
  const q = f => vs[Math.min(vs.length - 1, Math.floor(f * vs.length))];
  const lo = q(0.02), hi = q(0.98);
  for (const p of pts) p.v = Math.min(hi, Math.max(lo, p.v));
  _pricePoints = pts;
  _priceKey = key;
}

// Colour anchors at the quantiles of the values drawn in view.
function _priceAnchorsOf(vals) {
  const vs = Float32Array.from(vals).sort();
  const q = f => vs[Math.min(vs.length - 1, Math.floor(f * vs.length))];
  return vs.length ? [0.02, 0.15, 0.3, 0.5, 0.7, 0.85, 0.98].map(q) : [];
}

function _priceRamp() { return isDarkTheme() ? _PRICE_RAMP.slice().reverse() : _PRICE_RAMP; }
const _hexRgb = h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16));

// €/m² → [r, g, b] along the ramp, piecewise-linear between the anchors.
function _priceColor(v, rgb) {
  const a = _priceAnchors;
  if (v <= a[0]) return rgb[0];
  for (let i = 1; i < a.length; i++) {
    if (v <= a[i]) {
      const t = (v - a[i - 1]) / ((a[i] - a[i - 1]) || 1);
      return rgb[i - 1].map((c, k) => c + (rgb[i][k] - c) * t);
    }
  }
  return rgb[rgb.length - 1];
}

function _priceSigmaM(z) { return z <= 12 ? 450 : z === 13 ? 300 : z === 14 ? 200 : z === 15 ? 140 : 110; }

// Points into a raster of the view (plus a margin), both sums blurred by a
// separable Gaussian: value = Σw·v / Σw, density = Σw (≈ listings nearby).
function _mapRenderPrices() {
  if (!_map) return;
  if (!_priceLayer) _priceLayer = L.layerGroup().addTo(_map);
  _pricePrep();
  const size = _map.getSize(), z = _map.getZoom();
  const mPerPx = 156543.03 * Math.cos(_map.getCenter().lat * Math.PI / 180) / Math.pow(2, z);
  const sig = _priceSigmaM(z) / (mPerPx * _PRICE_STEP);           // in surface pixels
  const R = Math.ceil(3 * sig), W = Math.ceil(size.x / _PRICE_STEP) + 2 * R, H = Math.ceil(size.y / _PRICE_STEP) + 2 * R;
  const sum = new Float32Array(W * H), cnt = new Float32Array(W * H);
  for (const p of _pricePoints) {
    const c = _map.latLngToContainerPoint([p.lat, p.lng]);
    const x = Math.round(c.x / _PRICE_STEP) + R, y = Math.round(c.y / _PRICE_STEP) + R;
    if (x < 0 || y < 0 || x >= W || y >= H) continue;
    sum[y * W + x] += p.v; cnt[y * W + x] += 1;
  }
  const k = [];
  for (let i = -R; i <= R; i++) k.push(Math.exp(-(i * i) / (2 * sig * sig)));
  const blur = (src, tmp) => {
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
      let s = 0;
      for (let i = -R; i <= R; i++) { const xx = x + i; if (xx >= 0 && xx < W) s += src[y * W + xx] * k[i + R]; }
      tmp[y * W + x] = s;
    }
    for (let x = 0; x < W; x++) for (let y = 0; y < H; y++) {
      let s = 0;
      for (let i = -R; i <= R; i++) { const yy = y + i; if (yy >= 0 && yy < H) s += tmp[yy * W + x] * k[i + R]; }
      src[y * W + x] = s;
    }
  };
  const tmp = new Float32Array(W * H);
  blur(sum, tmp); blur(cnt, tmp);
  const w = W - 2 * R, h = H - 2 * R;
  const canvas = document.createElement('canvas');
  canvas.width = w; canvas.height = h;
  const ctx = canvas.getContext('2d'), img = ctx.createImageData(w, h), rgb = _priceRamp().map(_hexRgb);
  const drawn = [];
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const i = (y + R) * W + (x + R);
    if (cnt[i] >= 2.5) drawn.push(sum[i] / cnt[i]);
  }
  _priceAnchors = _priceAnchorsOf(drawn);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const i = (y + R) * W + (x + R), n = cnt[i];
    if (n < 2.5) continue;                                         // too few listings near: blank
    const [r, g, b] = _priceColor(sum[i] / n, rgb), o = (y * w + x) * 4;
    img.data[o] = r; img.data[o + 1] = g; img.data[o + 2] = b;
    img.data[o + 3] = Math.round(255 * 0.55 * Math.min(1, (n - 2.5) / 3.5 + 0.35));
  }
  ctx.putImageData(img, 0, 0);
  const nw = _map.containerPointToLatLng([0, 0]), se = _map.containerPointToLatLng([w * _PRICE_STEP, h * _PRICE_STEP]);
  _priceLayer.clearLayers();
  _priceLayer.addLayer(L.imageOverlay(canvas.toDataURL(), [nw, se], { className: 'map-price-surface', interactive: false }));
  _renderPriceLegend();
  document.getElementById('mapListingCount').textContent = t('map.price_count', { n: fmt(_pricePoints.length) });
}

// Tap / click: the price at that point and how many listings within 300 m.
function _mapPriceAt(latlng) {
  _pricePrep();
  const sigM = Math.max(_priceSigmaM(_map.getZoom()), 150);
  let s = 0, n = 0, near = 0;
  for (const p of _pricePoints) {
    const d = _map.distance(latlng, [p.lat, p.lng]);
    if (d > 3 * sigM) continue;
    const w = Math.exp(-(d * d) / (2 * sigM * sigM));
    s += w * p.v; n += w;
    if (d <= 300) near++;
  }
  if (n < 2.5) return `<div class="map-price-pop">${escapeHtml(t('map.price_few'))}</div>`;
  const v = s / n;
  return `<div class="map-price-pop"><strong>${escapeHtml(_priceLabel(v))}</strong> ${escapeHtml(t(_mapDataType === 'sales' ? 'map.price_unit_sale' : 'map.price_unit_rent'))}`
    + `<br>${escapeHtml(t('map.price_60', { v: _price60(v) }))}`
    + `<br><span class="map-price-n">${escapeHtml(t('map.price_near', { n: fmt(near) }))}</span></div>`;
}

// A continuous bar with the city's low, middle and high; what 60 m² costs.
function _renderPriceLegend() {
  const box = document.getElementById('mapPriceLegend');
  if (!box) return;
  const a = _priceAnchors;
  if (!a || !a.length) { box.innerHTML = ''; return; }
  const esc = escapeHtml, unit = t(_mapDataType === 'sales' ? 'map.price_unit_sale' : 'map.price_unit_rent');
  box.innerHTML = `<span class="mpl-unit">${esc(unit)}</span>
    <span class="mpl-bar" style="background:linear-gradient(90deg, ${_priceRamp().join(', ')})"></span>
    <span class="mpl-ends"><span>${esc(_priceLabel(a[0]))}</span><span>${esc(_priceLabel(a[a.length - 1]))}</span></span>
    <span class="mpl-60">${esc(t('map.price_60_range', { lo: _price60(a[0]), hi: _price60(a[a.length - 1]) }))} · ${esc(t('map.price_tap'))}</span>`;
}
