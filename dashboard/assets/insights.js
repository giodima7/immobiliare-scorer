// Lume dashboard — Stats tab.
// Plain script (no bundler); shares globals with the other assets/*.js files.
// Declarations only: code that runs at start-up lives in init.js, which loads
// last, so any function here may call any other.

// ── Stats tab ───────────────────────────────────────────────────────────────
// Colours come from the theme tokens (cssVar), score colours from
// scoreClass() — the same 70 / 45 thresholds as the cards and the map.
let insightsLoaded = false;
const _insightsCharts = {};
let _insightsAgencyData = [];
let _insightsZoneData   = [];
let _zoneSortKey = 'count', _zoneSortAsc = false;
let _agencySortKey = 'count', _agencySortAsc = false;
let _zoneShowAll = false, _agencyShowAll = false;
const _STATS_TOP = 20;

function _destroyChart(id) {
  if (_insightsCharts[id]) { _insightsCharts[id].destroy(); delete _insightsCharts[id]; }
}

function _kpi(label, value, cls = '') {
  return `<div class="kpi"><div class="kpi-label">${escapeHtml(label)}</div>
    <div class="kpi-value ${cls}">${value}</div></div>`;
}

function showInsightsTab() {
  if (insightsLoaded) return;
  if (!ALL_RENT.length) {
    loadRentals().then(() => { computeInsights(); insightsLoaded = true; });
  } else {
    computeInsights();
    insightsLoaded = true;
  }
}

// What the grid can show — excluded (fake / bait) rows never are.
function _statsListings() {
  return ALL_RENT.filter(l => !l.excluded);
}

function computeInsights() {
  if (!window.Chart) return;
  const listings = _statsListings();
  _renderMarketOverview(listings);
  _renderNewActivity(listings);
  _renderZoneBreakdown(listings);
  _renderAgencyLeaderboard(listings);
}

function _barChart(id, canvasId, labels, data, color, tooltipLabel) {
  _destroyChart(id);
  const ctx = document.getElementById(canvasId);
  if (!ctx) return;
  _insightsCharts[id] = new Chart(ctx, {
    type: 'bar',
    data: { labels, datasets: [{ data, backgroundColor: color, borderRadius: 4, maxBarThickness: 40 }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: tooltipLabel } } },
      scales: { x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 10 } },
                y: { beginAtZero: true, ticks: { precision: 0, maxTicksLimit: 5 } } },
    },
  });
}

function _renderMarketOverview(listings) {
  const scores   = listings.map(l => l.score_total).filter(v => v != null);
  const rents    = listings.map(l => l.rent_mo).filter(v => v != null);
  const avgScore = scores.length ? Math.round(scores.reduce((a, b) => a + b, 0) / scores.length) : null;
  const avgRent  = rents.length ? Math.round(rents.reduce((a, b) => a + b, 0) / rents.length) : null;
  const hidden   = listings.filter(l => l.hidden_gem).length;
  const good     = listings.filter(l => l.good_value && !l.hidden_gem).length;

  const el = document.getElementById('insightsMetricCards');
  if (el) el.innerHTML = [
    _kpi(t('stats.kpi_listings'), fmt(listings.length)),
    _kpi(t('stats.kpi_rent'), avgRent != null ? '€' + fmt(avgRent) : '–'),
    _kpi(t('stats.kpi_score'), avgScore != null ? fmt(avgScore) : '–'),
    _kpi(t('gem.good'), fmt(good), 'good-text'),
    _kpi(t('gem.hidden'), fmt(hidden), 'gem-text'),
  ].join('');

  if (!scores.length) return;
  const buckets = Array(10).fill(0);
  scores.forEach(s => { buckets[Math.min(9, Math.floor(s / 10))]++; });
  const colors = buckets.map((_, i) => cssVar({ high: '--green-deep', mid: '--amber', low: '--coral' }[scoreClass(i * 10 + 5)]));
  _barChart('scoreChart', 'insightsScoreChart',
    ['0–9', '10–19', '20–29', '30–39', '40–49', '50–59', '60–69', '70–79', '80–89', '90–100'],
    buckets, colors,
    c => t('stats.tip_listings', { n: fmt(c.parsed.y), pct: fmtNum(c.parsed.y / scores.length * 100, 1) }));
}

function _renderNewActivity(listings) {
  const now = new Date();
  const days30 = [], dayCounts = {};
  for (let i = 29; i >= 0; i--) {
    const d = new Date(now); d.setDate(d.getDate() - i);
    const key = _localDateStr(d);
    days30.push(key); dayCounts[key] = 0;
  }
  listings.forEach(l => { const k = String(l.first_seen_date || '').slice(0, 10); if (k in dayCounts) dayCounts[k]++; });
  _barChart('newChart', 'insightsNewChart',
    days30.map(d => fmtDate(d, { day: 'numeric', month: 'short' })),
    days30.map(d => dayCounts[d]), cssVar('--green-deep'),
    c => t('stats.tip_new', { n: fmt(c.parsed.y) }));
}

function _updateSortIndicators(headerId, sortKey, sortAsc, colKeys) {
  const tr = document.getElementById(headerId);
  if (!tr) return;
  tr.querySelectorAll('.sort-ind').forEach((span, i) => {
    span.textContent = colKeys[i] === sortKey ? (sortAsc ? '▲' : '▼') : '';
  });
  tr.querySelectorAll('th').forEach((th, i) => th.setAttribute('aria-sort',
    colKeys[i] === sortKey ? (sortAsc ? 'ascending' : 'descending') : 'none'));
}

function _moreButton(id, shown, total, showAll, onToggle) {
  const btn = document.getElementById(id);
  if (!btn) return;
  btn.hidden = total <= _STATS_TOP;
  btn.textContent = showAll ? t('stats.show_top', { n: _STATS_TOP }) : t('stats.show_all', { n: fmt(total) });
  btn.onclick = onToggle;
}

function _renderZoneBreakdown(listings) {
  const zones = {};
  listings.forEach(l => {
    const key = l.neighbourhood || '_unknown';
    if (!zones[key]) zones[key] = { name: key, count: 0, scores: [], prices: [], gems: 0, good: 0 };
    zones[key].count++;
    if (l.score_total != null) zones[key].scores.push(l.score_total);
    if (l.rent_mo != null) zones[key].prices.push(l.rent_mo);
    if (l.hidden_gem) zones[key].gems++;
    if (l.good_value && !l.hidden_gem) zones[key].good++;
  });
  _insightsZoneData = Object.values(zones).map(z => ({
    ...z,
    avg_score: z.scores.length ? Math.round(z.scores.reduce((a, b) => a + b, 0) / z.scores.length) : null,
    avg_price: z.prices.length ? Math.round(z.prices.reduce((a, b) => a + b, 0) / z.prices.length) : null,
  }));
  _renderZoneTable();
}

function _renderZoneTable() {
  const colKeys = ['name', 'count', 'avg_score', 'avg_price', 'gems', 'good'];
  _updateSortIndicators('insightsZoneHead', _zoneSortKey, _zoneSortAsc, colKeys);
  const data = [..._insightsZoneData].sort((a, b) => {
    const av = a[_zoneSortKey] ?? (_zoneSortKey === 'name' ? '' : -Infinity);
    const bv = b[_zoneSortKey] ?? (_zoneSortKey === 'name' ? '' : -Infinity);
    if (typeof av === 'string') return _zoneSortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
    return _zoneSortAsc ? av - bv : bv - av;
  });
  const shown = _zoneShowAll ? data : data.slice(0, _STATS_TOP);
  const rows = shown.map(z => `<tr class="zone-row" data-zone="${escapeHtml(z.name)}" tabindex="0">
      <td>${z.name === '_unknown' ? '–' : escapeHtml(formatAddress(z.name))}</td>
      <td class="num">${fmt(z.count)}</td>
      <td class="num"><span class="score-text sc-${scoreClass(z.avg_score ?? 0)}">${z.avg_score ?? '–'}</span></td>
      <td class="num">${z.avg_price != null ? '€' + fmt(z.avg_price) : '–'}</td>
      <td class="num${z.gems ? ' gem-text' : ''}">${fmt(z.gems)}</td>
      <td class="num${z.good ? ' good-text' : ''}">${fmt(z.good)}</td>
    </tr>`);
  const tbody = document.getElementById('insightsZoneBody');
  if (tbody) tbody.innerHTML = rows.join('') || `<tr><td colspan="6" class="section-hint">${escapeHtml(t('stats.no_data'))}</td></tr>`;
  _moreButton('insightsZoneMore', shown.length, data.length, _zoneShowAll, () => { _zoneShowAll = !_zoneShowAll; _renderZoneTable(); });
}

// A neighbourhood row → the Rentals list filtered to it.
function openZoneInRentals(name) {
  if (!name || name === '_unknown') return;
  rentSelectedAreas = [name];
  if (typeof filterAreaMsd !== 'undefined' && filterAreaMsd) filterAreaMsd.setSelected([name]);
  switchTab('rentals');
  renderRentals();
  saveFilters();
  window.scrollTo({ top: 0 });
}

function _sortZone(key) {
  if (_zoneSortKey === key) _zoneSortAsc = !_zoneSortAsc;
  else { _zoneSortKey = key; _zoneSortAsc = key === 'name'; }
  _renderZoneTable();
}

function computeAgencyStats(listings) {
  const map = {};
  for (const l of listings) {
    if (!l.agency_id && !l.agency_name) continue;   // skip listings with no agency data yet
    const key = (l.agency_name || '').trim().toLowerCase() || l.agency_id || '_unknown';
    if (!map[key]) map[key] = { id: l.agency_id || '', name: l.agency_name || '', type: l.agency_type || '', url: l.agency_url || '', count: 0, scores: [], prices: [] };
    map[key].count++;
    if (l.score_total != null) map[key].scores.push(l.score_total);
    if (l.rent_mo != null) map[key].prices.push(l.rent_mo);
  }
  return Object.values(map)
    .filter(a => a.count >= 2)
    .map(a => ({
      ...a,
      avg_score: a.scores.length ? Math.round(a.scores.reduce((x, y) => x + y, 0) / a.scores.length) : null,
      avg_price: a.prices.length ? Math.round(a.prices.reduce((x, y) => x + y, 0) / a.prices.length) : null,
    }));
}

let _insightsAgencyMissingData = false;
function _renderAgencyLeaderboard(listings) {
  const hasAgencyData = listings.some(l => l.agency_id || l.agency_name);
  _insightsAgencyData = hasAgencyData ? computeAgencyStats(listings) : [];
  _insightsAgencyMissingData = !hasAgencyData;
  _renderAgencyTable();
}

function _renderAgencyTable() {
  const tbody = document.getElementById('insightsAgencyBody');
  if (!tbody) return;
  if (_insightsAgencyMissingData) {
    tbody.innerHTML = `<tr><td colspan="5" class="section-hint">${escapeHtml(t('stats.no_agency'))}</td></tr>`;
    return;
  }
  const colKeys = ['name', 'type', 'count', 'avg_score', 'avg_price'];
  _updateSortIndicators('insightsAgencyHead', _agencySortKey, _agencySortAsc, colKeys);
  const q = (document.getElementById('insightsAgencySearch')?.value || '').toLowerCase();
  let data = _insightsAgencyData.filter(a => !q || (a.name || a.id).toLowerCase().includes(q));
  data = [...data].sort((a, b) => {
    const isStr = _agencySortKey === 'name' || _agencySortKey === 'type';
    const av = a[_agencySortKey] ?? (isStr ? '' : -Infinity);
    const bv = b[_agencySortKey] ?? (isStr ? '' : -Infinity);
    if (isStr) return _agencySortAsc ? String(av).localeCompare(String(bv)) : String(bv).localeCompare(String(av));
    return _agencySortAsc ? av - bv : bv - av;
  });
  const shown = (_agencyShowAll || q) ? data : data.slice(0, _STATS_TOP);
  // The agency name filters the Rentals list; ↗ opens it on the portal.
  const rows = shown.map(a => {
    const name = a.name || a.id || '–';
    const ext = /^https?:\/\//.test(a.url || '')
      ? ` <a href="${_safeUrl(a.url)}" target="_blank" rel="noopener" class="btn btn-ghost btn-sm btn-icon" aria-label="${escapeHtml(t('stats.agency_site'))}" title="${escapeHtml(t('stats.agency_site'))}">${icon('external-link')}</a>`
      : '';
    return `<tr>
      <td><button type="button" class="agency-link" data-pin-agency="${escapeHtml(name)}">${escapeHtml(name)}</button>${ext}</td>
      <td>${a.type ? `<span class="type-badge">${escapeHtml(a.type)}</span>` : '–'}</td>
      <td class="num">${fmt(a.count)}</td>
      <td class="num"><span class="score-text sc-${scoreClass(a.avg_score ?? 0)}">${a.avg_score ?? '–'}</span></td>
      <td class="num">${a.avg_price != null ? '€' + fmt(a.avg_price) : '–'}</td>
    </tr>`;
  });
  tbody.innerHTML = rows.join('') || `<tr><td colspan="5" class="section-hint">${escapeHtml(t('stats.no_agency_match'))}</td></tr>`;
  _moreButton('insightsAgencyMore', shown.length, q ? 0 : data.length, _agencyShowAll,
    () => { _agencyShowAll = !_agencyShowAll; _renderAgencyTable(); });
}

// Agency-pin: clicking an agency name in the Stats leaderboard switches
// to the Rentals tab and hard-restricts the grid to that agency.
// Backed by window._pinnedAgencyRent, honoured by applyRentFilters.
function pinAgency(name) {
  if (!name) return;
  window._pinnedAgencyRent = name;
  if (typeof switchTab === 'function') switchTab('rentals');
  if (typeof renderRentals === 'function') renderRentals();
  _refreshAgencyBanners();
  window.scrollTo({ top: 0 });
}

function clearPinnedAgency() {
  window._pinnedAgencyRent = null;
  if (typeof renderRentals === 'function') renderRentals();
  _refreshAgencyBanners();
}

function _refreshAgencyBanners() {
  const b = document.getElementById('rAgencyBanner');
  if (!b) return;
  const val = window._pinnedAgencyRent;
  if (val) {
    const n = document.getElementById('rAgencyBannerName');
    if (n) n.textContent = val;
    b.style.display = 'flex';
  } else {
    b.style.display = 'none';
  }
}

function _sortAgency(key) {
  if (_agencySortKey === key) _agencySortAsc = !_agencySortAsc;
  else { _agencySortKey = key; _agencySortAsc = key === 'name' || key === 'type'; }
  _renderAgencyTable();
}
