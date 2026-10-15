// Lume dashboard — Stats tab.
// Plain script (no bundler); shares globals with the other assets/*.js files.
// Declarations only: code that runs at start-up lives in init.js, which loads
// last, so any function here may call any other.

// ── Insights tab ─────────────────────────────────────────────────────────────
let insightsLoaded = false;
let insightsMode   = 'rentals';
const _insightsCharts = {};
let _insightsAgencyData = [];
let _insightsZoneData   = [];
let _zoneSortKey = 'count', _zoneSortAsc = false;
let _agencySortKey = 'count', _agencySortAsc = false;

function _destroyChart(id) {
  if (_insightsCharts[id]) { _insightsCharts[id].destroy(); delete _insightsCharts[id]; }
}

function _metricCard(label, value, accent) {
  const border = accent ? `border-left:3px solid ${accent};padding-left:13px` : '';
  const valColor = accent ? `color:${accent}` : 'color:var(--text-primary)';
  return `<div style="background:var(--bg-card);border:1px solid var(--border-light);border-radius:10px;padding:14px 16px;${border}">
    <div style="font-size:11px;color:var(--text-secondary);font-weight:600;text-transform:uppercase;letter-spacing:0.5px">${label}</div>
    <div style="font-size: 24px;font-weight:800;margin-top:4px;${valColor}">${value}</div>
  </div>`;
}

function showInsightsTab() {
  if (insightsLoaded) return;
  const listings = ALL_RENT;
  if (!listings.length) {
    loadRentals().then(() => { computeInsights(); insightsLoaded = true; });
  } else {
    computeInsights();
    insightsLoaded = true;
  }
}

function computeInsights() {
  const listings = ALL_RENT;
  _renderMarketOverview(listings);
  _renderNewActivity(listings);
  _renderZoneBreakdown(listings);
  _renderAgencyLeaderboard(listings);
}

function _renderMarketOverview(listings) {
  const priceKey = insightsMode === 'rentals' ? 'rent_mo' : 'price';
  const scores   = listings.map(l => l.score_total).filter(v => v != null);
  const prices   = listings.map(l => l[priceKey]).filter(v => v != null);
  const avgScore = scores.length ? Math.round(scores.reduce((a,b)=>a+b,0)/scores.length) : null;
  const avgPrice = prices.length ? Math.round(prices.reduce((a,b)=>a+b,0)/prices.length) : null;
  const hidden   = listings.filter(l => l.hidden_gem).length;

  const el = document.getElementById('insightsMetricCards');
  const scoreAccent = avgScore == null ? null : avgScore >= 70 ? '#16a34a' : avgScore >= 50 ? '#d97706' : '#dc2626';
  if (el) el.innerHTML = [
    _metricCard('Total listings', fmt(listings.length), '#3B82F6'),
    _metricCard('Avg score', avgScore != null ? avgScore + '/100' : '–', scoreAccent),
    _metricCard(insightsMode === 'rentals' ? 'Avg rent /mo' : 'Avg price', avgPrice != null ? '€' + fmt(avgPrice) : '–', '#6366F1'),
    _metricCard('Hidden Gems', fmt(hidden), '#7C3AED'),
  ].join('');

  _destroyChart('scoreChart');
  const scoreCtx = document.getElementById('insightsScoreChart');
  if (!scoreCtx || !scores.length) return;
  const buckets = Array(10).fill(0);
  scores.forEach(s => { const b = Math.min(9, Math.floor(s / 10)); buckets[b]++; });
  _insightsCharts['scoreChart'] = new Chart(scoreCtx, {
    type: 'bar',
    data: {
      labels: ['0–9','10–19','20–29','30–39','40–49','50–59','60–69','70–79','80–89','90–100'],
      datasets: [{ data: buckets, backgroundColor: 'rgba(45,106,79,0.7)', borderRadius: 4 }]
    },
    options: {
      responsive: true, maintainAspectRatio: true,
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => { const pct = scores.length ? (c.parsed.y / scores.length * 100).toFixed(1) : 0; return `${c.parsed.y} listings (${pct}%)`; } } } },
      scales: { x: { ticks: { font: { size: 11 } } }, y: { beginAtZero: true, ticks: { font: { size: 11 } } } }
    }
  });
}

function _renderNewActivity(listings) {
  const now   = new Date();
  const mkAgo = d => { const t = new Date(now); t.setDate(t.getDate() - d); return t; };
  const ago7 = mkAgo(7), ago14 = mkAgo(14), ago30 = mkAgo(30);
  const n7  = listings.filter(l => l.first_seen_date && new Date(l.first_seen_date) >= ago7).length;
  const n14 = listings.filter(l => l.first_seen_date && new Date(l.first_seen_date) >= ago14).length;
  const n30 = listings.filter(l => l.first_seen_date && new Date(l.first_seen_date) >= ago30).length;

  const el = document.getElementById('insightsNewCounters');
  if (el) el.innerHTML = [
    _metricCard('Last 7 days', fmt(n7)),
    _metricCard('Last 14 days', fmt(n14)),
    _metricCard('Last 30 days', fmt(n30)),
  ].join('');

  _destroyChart('newChart');
  const newCtx = document.getElementById('insightsNewChart');
  if (!newCtx) return;
  const days30 = [], dayCounts = {};
  for (let i = 29; i >= 0; i--) {
    const d = new Date(now); d.setDate(d.getDate() - i);
    const key = _localDateStr(d);
    days30.push(key); dayCounts[key] = 0;
  }
  listings.forEach(l => { if (l.first_seen_date && dayCounts.hasOwnProperty(l.first_seen_date)) dayCounts[l.first_seen_date]++; });
  _insightsCharts['newChart'] = new Chart(newCtx, {
    type: 'bar',
    data: {
      labels: days30.map(d => d.slice(5)),
      datasets: [{ data: days30.map(d => dayCounts[d]), backgroundColor: 'rgba(59,130,246,0.7)', borderRadius: 3 }]
    },
    options: {
      responsive: true, maintainAspectRatio: true,
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => c.parsed.y + ' new' } } },
      scales: { x: { ticks: { font: { size: 10 }, maxRotation: 45 } }, y: { beginAtZero: true, ticks: { font: { size: 11 } } } }
    }
  });
}

function _scoreColor(s) {
  if (s == null) return '';
  return s >= 70 ? 'color:#16a34a;font-weight:600' : s >= 50 ? 'color:#d97706;font-weight:600' : 'color:#dc2626;font-weight:600';
}
function _agencyTypeBadge(type) {
  if (!type || type === '–') return '<span style="color:var(--text-secondary)">–</span>';
  const t = type.toLowerCase();
  const s = t.includes('agenc') ? 'background:#DBEAFE;color:#1E40AF' : t.includes('privat') ? 'background:#D1FAE5;color:#065F46' : 'background:#F3F4F6;color:#6B7280';
  return `<span style="font-size: 11px;font-weight:600;padding:2px 7px;border-radius:10px;${s}">${type}</span>`;
}
function _updateSortIndicators(headerId, sortKey, sortAsc, colKeys) {
  const tr = document.getElementById(headerId);
  if (!tr) return;
  tr.querySelectorAll('th span').forEach((span, i) => {
    span.textContent = colKeys[i] === sortKey ? (sortAsc ? ' ▲' : ' ▼') : ' ↕';
  });
}

function _renderZoneBreakdown(listings) {
  const zones = {};
  listings.forEach(l => {
    const key = l.neighbourhood || '_unknown';
    if (!zones[key]) zones[key] = { name: key, count: 0, scores: [], prices: [], gems: 0, good: 0 };
    zones[key].count++;
    if (l.score_total != null) zones[key].scores.push(l.score_total);
    const price = l.rent_mo ?? l.price;
    if (price != null) zones[key].prices.push(price);
    if (l.hidden_gem) zones[key].gems++;
    if (l.good_value && !l.hidden_gem) zones[key].good++;
  });
  _insightsZoneData = Object.values(zones).map(z => ({
    ...z,
    avg_score: z.scores.length ? Math.round(z.scores.reduce((a,b)=>a+b,0)/z.scores.length) : null,
    avg_price: z.prices.length ? Math.round(z.prices.reduce((a,b)=>a+b,0)/z.prices.length) : null,
  }));
  _renderZoneTable();
}

function _renderZoneTable() {
  const colKeys = ['name','count','avg_score','avg_price','gems','good'];
  _updateSortIndicators('insightsZoneHead', _zoneSortKey, _zoneSortAsc, colKeys);
  const data = [..._insightsZoneData].sort((a, b) => {
    const av = a[_zoneSortKey] ?? (_zoneSortKey === 'name' ? '' : -Infinity);
    const bv = b[_zoneSortKey] ?? (_zoneSortKey === 'name' ? '' : -Infinity);
    if (typeof av === 'string') return _zoneSortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
    return _zoneSortAsc ? av - bv : bv - av;
  });
  const rows = data.map(z => {
    const gemStyle  = z.gems > 0 ? 'color:#7C3AED;font-weight:700' : 'color:var(--text-secondary)';
    const goodStyle = z.good > 0 ? 'color:#2563EB;font-weight:700' : 'color:var(--text-secondary)';
    return `<tr>
      <td>${z.name === '_unknown' ? '–' : z.name}</td>
      <td>${fmt(z.count)}</td>
      <td style="${_scoreColor(z.avg_score)}">${z.avg_score != null ? z.avg_score : '–'}</td>
      <td>${z.avg_price != null ? '€' + fmt(z.avg_price) : '–'}</td>
      <td style="${gemStyle}">${z.gems || 0}</td>
      <td style="${goodStyle}">${z.good || 0}</td>
    </tr>`;
  });
  const tbody = document.getElementById('insightsZoneBody');
  if (tbody) tbody.innerHTML = rows.join('') || '<tr><td colspan="6" style="color:var(--text-secondary);text-align:center;padding:16px">No zone data yet</td></tr>';
}

function _sortZone(key) {
  if (_zoneSortKey === key) _zoneSortAsc = !_zoneSortAsc;
  else { _zoneSortKey = key; _zoneSortAsc = false; }
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
    const price = l.rent_mo ?? l.price;
    if (price != null) map[key].prices.push(price);
  }
  return Object.values(map)
    .filter(a => a.count >= 2)
    .map(a => ({
      ...a,
      avg_score: a.scores.length ? Math.round(a.scores.reduce((x,y)=>x+y,0)/a.scores.length) : null,
      avg_price: a.prices.length ? Math.round(a.prices.reduce((x,y)=>x+y,0)/a.prices.length) : null,
    }));
}

function _renderAgencyLeaderboard(listings) {
  const hasAgencyData = listings.some(l => l.agency_id || l.agency_name);
  _insightsAgencyData = hasAgencyData ? computeAgencyStats(listings) : [];
  _insightsAgencyMissingData = !hasAgencyData;
  _renderAgencyTable();
}

let _insightsAgencyMissingData = false;
function _renderAgencyTable() {
  const tbody = document.getElementById('insightsAgencyBody');
  if (!tbody) return;
  if (_insightsAgencyMissingData) {
    tbody.innerHTML = `<tr><td colspan="5" style="color:var(--text-secondary);text-align:center;padding:20px;line-height:1.6">
      Agency data not yet available.<br>
      <span style="font-size:11px">It will populate automatically after the next scan.</span>
    </td></tr>`;
    return;
  }
  const colKeys = ['name','type','count','avg_score','avg_price'];
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
  // The agency name is now a Lume-internal link: clicking switches to the
  // matching listings tab and pins the filter. The external agency URL
  // (from the source site) moves to a small "↗" icon next to the name.
  const rows = data.map(a => {
    const name = a.name || a.id || '–';
    const safe = String(name).replace(/'/g, "\\'");
    const ext  = a.url
      ? ` <a href="${a.url}" target="_blank" rel="noopener" title="Open agency on ${insightsMode === 'sales' ? 'sales' : 'rentals'} source site" style="color:var(--text-secondary);text-decoration:none;margin-left:6px">↗</a>`
      : '';
    return `<tr>
      <td>
        <button type="button" class="agency-link" onclick="pinAgency('${safe}', '${insightsMode === 'sales' ? 'sales' : 'rentals'}')">${name}</button>${ext}
      </td>
      <td>${_agencyTypeBadge(a.type || '')}</td>
      <td>${fmt(a.count)}</td>
      <td style="${_scoreColor(a.avg_score)}">${a.avg_score != null ? a.avg_score : '–'}</td>
      <td>${a.avg_price != null ? '€' + fmt(a.avg_price) : '–'}</td>
    </tr>`;
  });
  tbody.innerHTML = rows.join('') || '<tr><td colspan="5" style="color:var(--text-secondary);text-align:center;padding:16px">No agencies with 2+ listings</td></tr>';
}

// Agency-pin: clicking an agency name in the Stats leaderboard switches
// to the Rentals tab and hard-restricts the grid to that agency.
// Backed by window._pinnedAgencyRent, honoured by applyRentFilters.
function pinAgency(name, mode) {
  if (!name) return;
  window._pinnedAgencyRent = name;
  if (typeof switchTab === 'function') switchTab('rentals');
  if (typeof renderRentals === 'function') renderRentals();
  _refreshAgencyBanners();
}

function clearPinnedAgency(mode) {
  window._pinnedAgencyRent = null;
  if (typeof renderRentals === 'function') renderRentals();
  _refreshAgencyBanners();
}

function _refreshAgencyBanners() {
  const pairs = [
    ['rAgencyBanner', 'rAgencyBannerName', window._pinnedAgencyRent],
  ];
  for (const [bannerId, nameId, val] of pairs) {
    const b = document.getElementById(bannerId);
    if (!b) continue;
    if (val) {
      const n = document.getElementById(nameId);
      if (n) n.textContent = val;
      b.style.display = 'flex';
    } else {
      b.style.display = 'none';
    }
  }
}

function _sortAgency(key) {
  if (_agencySortKey === key) _agencySortAsc = !_agencySortAsc;
  else { _agencySortKey = key; _agencySortAsc = false; }
  _renderAgencyTable();
}
