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
const RENT_MUSTS = [
  { key: 'furnished', icon: 'armchair',        value: l => l.furnished ?? null },
  { key: 'balcony',   icon: 'plant',           value: l => l.has_balcony ?? null },
  { key: 'parking',   icon: 'car',             value: l => l.has_parking ?? null },
  { key: 'lift',      icon: 'elevator',        value: l => l.elevator ?? null },
  { key: 'heating',   icon: 'flame',           value: l => l.heating_type && l.heating_type !== 'unknown' ? l.heating_type === 'autonomous' : null },
  { key: 'baths',     icon: 'bath',            value: l => l.bathrooms != null ? l.bathrooms >= 2 : null },
  { key: 'renovated', icon: 'sparkles',        value: l => { const g = conditionGroup(l.condition); return g === 'unknown' ? null : g === 'premium'; } },
  { key: 'safe',      icon: 'alert-triangle',  value: l => !l.price_suspect },
];
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
  const c = String(raw || '').toLowerCase().trim();
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
function rentMustCounts(f) {
  const base = applyRentFilters(ALL_RENT, { ...f, must: [], elevator: false });
  const counts = {};
  for (const m of RENT_MUSTS) {
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

// Must-have tiles: name on top, "5.768 · +5.798 not known yet" below.
// 'safe' (hide suspicious prices) is an exclusion, so it's a separate
// checkbox (.hide-suspect), not a tile.
function renderMustGrid(container, counts) {
  if (!container) return;
  const active = new Set(rentMustActive());
  container.innerHTML = RENT_MUSTS.filter(m => m.key !== 'safe').map(m => {
    const c = counts[m.key] || { yes: 0, unknown: 0 };
    const unk = c.unknown > 0 ? ` · ${escapeHtml(t('must.unknown_n', { n: fmt(c.unknown) }))}` : '';
    return `<button type="button" class="must-chip${active.has(m.key) ? ' on' : ''}" data-must="${m.key}"
              aria-pressed="${active.has(m.key)}">
              <span class="must-label">${icon(m.icon)}${escapeHtml(t('must.' + m.key))}</span>
              <span class="must-count"><strong>${fmt(c.yes)}</strong>${unk}</span>
            </button>`;
  }).join('');
  document.querySelectorAll('.hide-suspect').forEach(cb => { cb.checked = active.has('safe'); });
}

// ── Active-filter chips ───────────────────────────────────────────────────────
// One removable chip per active filter, so nothing filters out of sight.
function rentActiveFilterChips(f) {
  const chips = [];
  const add = (key, label) => chips.push({ key, label });
  if (f.areas && f.areas.length) add('areas', f.areas.length > 2 ? `${f.areas.slice(0, 2).join(', ')} +${f.areas.length - 2}` : f.areas.join(', '));
  if (f.maxRent)   add('maxRent', t('chip.max_rent', { n: fmt(f.maxRent) }));
  if (f.minRooms)  add('minRooms', t('chip.min_rooms', { n: f.minRooms }));
  if (f.minSqm)    add('minSqm', t('chip.min_sqm', { n: f.minSqm }));
  if (f.gemFilter) add('gemFilter', f.gemFilter === 'hidden' ? t('gem.hidden') : t('gem.good'));
  for (const k of f.must || []) add('must:' + k, t('must.' + k));
  if (f.includeUnknown) add('includeUnknown', t('chip.include_unknown'));
  if (f.condition)   add('condition', t('cond.' + f.condition));
  if (f.minDiscount) add('minDiscount', t('chip.min_discount', { n: f.minDiscount }));
  if (f.contract)    add('contract', f.contract);
  if (f.fascia)      add('fascia', t('fascia.' + f.fascia));
  if (f.maxMetro)    add('maxMetro', t('chip.max_metro', { n: fmt(f.maxMetro) }));
  if (f.minFloor > -999) add('minFloor', t('chip.min_floor', { n: f.minFloor }));
  if (f.source)      add('source', f.source === 'idealista' ? 'Idealista' : 'Immobiliare.it');
  if (f.minEnergy)   add('minEnergy', t('chip.min_energy', { v: f.minEnergy }));
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
  const counts = rentMustCounts(f);
  renderMustGrid(document.getElementById('rMustGrid'), counts);
  renderMustGrid(document.getElementById('dMustGrid'), counts);
  const nMust = rentMustActive().length;
  const mustBadge = document.getElementById('rMustBadge');
  if (mustBadge) { mustBadge.textContent = nMust || ''; mustBadge.hidden = !nMust; }
  document.getElementById('rMustBtn')?.classList.toggle('on', nMust > 0);

  const chips = rentActiveFilterChips(f);
  const moreKeys = ['condition', 'minDiscount', 'contract', 'fascia', 'maxMetro', 'minFloor', 'source',
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
    + `<span class="fchips-count"><strong>${fmt(shown)}</strong> ${escapeHtml(t(shown === 1 ? 'chip.flat' : 'chip.flats'))}</span>`
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
    case 'fascia':      el('rFascia').value = ''; break;
    case 'maxMetro':    el('rMaxMetro').value = ''; break;
    case 'minFloor':    el('rMinFloor').value = ''; break;
    case 'source':
      document.querySelectorAll('#rSourceFilter .sort-tab').forEach(b => b.classList.toggle('active', (b.dataset.rsource || '') === ''));
      break;
    case 'minEnergy':   el('rMinEnergy').value = ''; break;
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
