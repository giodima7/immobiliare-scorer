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
  _map.on('moveend', () => { if (_mapView === 'pins') onMove(); });
  _map.on('zoomend', () => { if (_mapView === 'pins') onMove(); else _mapRenderPrices(); });
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

// ── Prices view: a grid of median €/m² (2026-10-31) ──────────────────────────
// Every live listing of the kind shown, not the filtered list: a price cap in
// the filter bar would paint its own ceiling. A cell needs 5 listings; cells
// are 500 m wide zoomed out, 350 m, then 250 m (density measured that day:
// 350 m cells with 5+ listings hold 94 % of the rentals). Rentals: all-in
// rent per m² a month; sales: price per m². Colours: one blue ramp in seven
// quantile classes (dataviz sequential), darkest = dearest; flipped in the
// dark theme so the dear cells stay the ones that stand out.
const _PRICE_RAMP = ['#cde2fb', '#9ec5f4', '#6da7ec', '#3987e5', '#256abf', '#184f95', '#0d366b'];
const _PRICE_MIN_N = 5;

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

function _median(a) {
  const s = a.slice().sort((x, y) => x - y), m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

// "€24,1" a month or "€6.780"; and what 60 m² comes to at that rate.
function _priceLabel(v) {
  return _mapDataType === 'sales' ? `€${fmt(Math.round(v / 10) * 10)}` : `€${fmt(Math.round(v * 10) / 10, { minimumFractionDigits: 1, maximumFractionDigits: 1 })}`;
}
function _price60(v) {
  return _mapDataType === 'sales' ? `€${fmt(Math.round(v * 60 / 1000))}k` : `€${fmt(Math.round(v * 60 / 10) * 10)}`;
}

function _mapRenderPrices() {
  if (!_map) return;
  if (!_priceLayer) _priceLayer = L.layerGroup().addTo(_map);
  _priceLayer.clearLayers();
  const z = _map.getZoom();
  const cell = z <= 12 ? 500 : z <= 13 ? 350 : 250;
  const dLat = cell / 111000, dLng = cell / (111000 * Math.cos(45.46 * Math.PI / 180));
  const cells = new Map();
  let n = 0;
  for (const l of _priceSource()) {
    const v = _pricePerSqm(l);
    if (v == null) continue;
    n++;
    const k = `${Math.floor(l.latitude / dLat)}:${Math.floor(l.longitude / dLng)}`;
    if (!cells.has(k)) cells.set(k, []);
    cells.get(k).push(v);
  }
  const shown = [...cells].filter(([, v]) => v.length >= _PRICE_MIN_N)
                          .map(([k, v]) => ({ k, med: _median(v), n: v.length }));
  // Seven classes at the quantiles of the cells' medians.
  const meds = shown.map(c => c.med).sort((a, b) => a - b);
  const breaks = [1, 2, 3, 4, 5, 6].map(i => meds[Math.min(meds.length - 1, Math.floor(i * meds.length / 7))]);
  const ramp = isDarkTheme() ? _PRICE_RAMP.slice().reverse() : _PRICE_RAMP;
  const cls = v => breaks.filter(b => v >= b).length;
  const unit = t(_mapDataType === 'sales' ? 'map.price_unit_sale' : 'map.price_unit_rent');
  for (const c of shown) {
    const [i, j] = c.k.split(':').map(Number);
    const rect = L.rectangle([[i * dLat, j * dLng], [(i + 1) * dLat, (j + 1) * dLng]], {
      renderer: _mapRenderer, stroke: true, weight: 1, color: isDarkTheme() ? '#1b1b19' : '#ffffff',
      fillColor: ramp[cls(c.med)], fillOpacity: 0.5,
    });
    rect.bindTooltip(`<strong>${escapeHtml(_priceLabel(c.med))}</strong> ${escapeHtml(unit)}<br>`
      + `${escapeHtml(t('map.price_60', { v: _price60(c.med) }))}<br>`
      + `<span class="map-price-n">${escapeHtml(t('map.price_n', { n: fmt(c.n) }))}</span>`, { sticky: true, direction: 'top' });
    _priceLayer.addLayer(rect);
  }
  _renderPriceLegend(meds, breaks, ramp, unit);
  document.getElementById('mapListingCount').textContent = t('map.price_count', { n: fmt(n), c: fmt(shown.length) });
}

// Seven swatches with their lower bounds; the ends say what 60 m² costs.
function _renderPriceLegend(meds, breaks, ramp, unit) {
  const box = document.getElementById('mapPriceLegend');
  if (!box) return;
  if (!meds.length) { box.innerHTML = ''; return; }
  const esc = escapeHtml;
  const lows = [meds[0], ...breaks];
  box.innerHTML = `<span class="mpl-unit">${esc(unit)}</span>
    <span class="mpl-ramp">${ramp.map((c, i) => `<span class="mpl-step" style="background:${c}" title="${esc(_priceLabel(lows[i]))}+"></span>`).join('')}</span>
    <span class="mpl-ends"><span>${esc(_priceLabel(meds[0]))}</span><span>${esc(_priceLabel(meds[meds.length - 1]))}</span></span>
    <span class="mpl-60">${esc(t('map.price_60_range', { lo: _price60(meds[0]), hi: _price60(meds[meds.length - 1]) }))}</span>`;
}
