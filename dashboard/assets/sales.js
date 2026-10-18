// ── Sales (compravendita) ────────────────────────────────────────────────────
// Listings for sale in the areas the user picked (sales_scan.py →
// milano_sales_latest.json locally, listing_type "sale" on Supabase). Same
// layout as Rentals: the filter bar (#saleFilterBar, built like #filterBar),
// removable chips with the count, cards, and Details in #listingModal. Each
// card: price and €/m² against nearby sales (comps_sale_delta_pct), the rent
// the flat would fetch and the gross yield (estimated from the rentals we
// hold), and the mortgage payment for the down payment / rate / years set in
// "More filters". Which areas are read: Tools → Areas for sale.

let ALL_SALE = [];
let _saleLoading = false;
let _saleLoadPromise = null;
let _saleAreaMsd = null;
const _SALE_AREA_ITEMS = [];       // neighbourhoods of the loaded sales, most listings first
const SALE_SINCE = '2026-10-18';   // first sales sweep: nothing is "new" that day

const SALE_DEFAULTS = { areas: [], maxPrice: 0, minRooms: 0, minSqm: 0, deal: '', sort: 'score',
                        down: 20, rate: 3.5, years: 25, condition: '', listed: '', source: '' };

function saleFilters() {
  const f = Object.assign({}, SALE_DEFAULTS, Storage.get('saleFilters', {}) || {});
  if (!Array.isArray(f.areas)) f.areas = [];
  delete f.area;                    // the old "my areas" switch
  return f;
}
function _setSaleFilters(patch) {
  Storage.set('saleFilters', Object.assign(saleFilters(), patch));
  renderSales();
}

// Monthly payment of a fixed-rate loan for (price − down payment).
function mortgagePayment(price, f) {
  const loan = price * (1 - (Number(f.down) || 0) / 100);
  const n = (Number(f.years) || 25) * 12, r = (Number(f.rate) || 0) / 100 / 12;
  if (!loan || !n) return 0;
  return r ? loan * r / (1 - Math.pow(1 + r, -n)) : loan / n;
}
const _round10 = v => Math.round(v / 10) * 10;

function loadSales() {
  if (!_saleLoadPromise) _saleLoadPromise = _loadSalesNow().finally(() => { _saleLoadPromise = null; });
  return _saleLoadPromise;
}

async function _loadSalesNow() {
  _saleLoading = true;
  renderSales();
  let raw = null;
  if (typeof IS_LIVE !== 'undefined' && IS_LIVE) {
    try {
      const resp = await fetch('/sales?city=milano');
      if (resp.ok) raw = await resp.json();
    } catch { /* fall through to Supabase */ }
  }
  if ((!raw || !raw.length) && typeof SupabaseClient !== 'undefined' && SupabaseClient.configured) {
    raw = await SupabaseClient.fetchAll('sale');
  }
  // Same flat on both sites → one card (the dashboard's own dedup, by price).
  ALL_SALE = _dedupListings((raw || []).filter(l => !l.is_stale && !l.excluded && !l.is_auction
                                                  && !l.is_nuda_proprieta && l.price), false);
  const n = {};
  for (const l of ALL_SALE) if (l.neighbourhood) n[l.neighbourhood] = (n[l.neighbourhood] || 0) + 1;
  _SALE_AREA_ITEMS.splice(0, _SALE_AREA_ITEMS.length,
    ...Object.entries(n).sort((a, b) => b[1] - a[1]).map(([k, c]) => ({ key: k, label: `${k} · ${fmt(c)}` })));
  _saleLoading = false;
  renderSales();
}

// ── Filter bar ───────────────────────────────────────────────────────────────
// Wired on the first render (the markup is static in index.html).
function _initSaleBar() {
  if (_saleAreaMsd || !document.getElementById('saleAreaWrap')) return;
  const el = id => document.getElementById(id);
  _saleAreaMsd = buildMultiSelect({
    wrapId: 'saleAreaWrap', triggerId: 'saleAreaTrigger', panelId: 'saleAreaPanel',
    searchId: 'saleAreaSearch', listId: 'saleAreaList', clearId: 'saleAreaClear', doneId: 'saleAreaDone',
    items: _SALE_AREA_ITEMS, onChange: areas => _setSaleFilters({ areas }),
  });
  let timer = null;
  const num = (id, key) => el(id)?.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => _setSaleFilters({ [key]: Number(el(id).value) || 0 }), 300);
  });
  num('sMaxPrice', 'maxPrice'); num('sMinRooms', 'minRooms'); num('sMinSqm', 'minSqm');
  num('sDown', 'down'); num('sRate', 'rate'); num('sYears', 'years');
  el('sSortSelect')?.addEventListener('change', e => _setSaleFilters({ sort: e.target.value }));
  el('sCondition')?.addEventListener('change', e => _setSaleFilters({ condition: e.target.value }));
  el('sListed')?.addEventListener('change', e => _setSaleFilters({ listed: e.target.value }));
  document.querySelectorAll('#sDealFilter [data-sdeal]').forEach(b =>
    b.addEventListener('click', () => _setSaleFilters({ deal: b.dataset.sdeal })));
  document.querySelectorAll('#sSourceFilter [data-ssource]').forEach(b =>
    b.addEventListener('click', () => _setSaleFilters({ source: b.dataset.ssource })));
  el('sMoreBtn')?.addEventListener('click', e => { e.stopPropagation(); _toggleSaleMore(); });
  el('sMoreDone')?.addEventListener('click', () => _toggleSaleMore(false));
  el('sBtnReset')?.addEventListener('click', () => _clearSaleFilter('all'));
  el('sActiveChips')?.addEventListener('click', e => {
    const b = e.target.closest('[data-sclear]');
    if (b) _clearSaleFilter(b.dataset.sclear);
  });
  document.addEventListener('click', e => {
    if (!e.target.closest('#saleFilterBar') && !e.target.closest('.msd-panel')) _toggleSaleMore(false);
  });
}

function _toggleSaleMore(open) {
  const p = document.getElementById('sMorePanel');
  if (!p) return;
  const show = open === undefined ? p.hidden : !!open;
  p.hidden = !show;
  document.getElementById('sMoreBtn')?.setAttribute('aria-expanded', String(show));
}

// Inputs, segments and badges from the saved filters (a reload, a chip ×).
function _syncSaleBar(f) {
  // Not while typing in it; an unset filter (0) shows its placeholder.
  const set = (id, v) => { const e = document.getElementById(id); if (e && document.activeElement !== e) e.value = v ?? ''; };
  set('sMaxPrice', f.maxPrice || ''); set('sMinRooms', f.minRooms || ''); set('sMinSqm', f.minSqm || '');
  set('sDown', f.down); set('sRate', f.rate); set('sYears', f.years);
  set('sSortSelect', f.sort); set('sCondition', f.condition); set('sListed', f.listed);
  const seg = (sel, attr, cur) => document.querySelectorAll(sel).forEach(b => {
    const on = (b.dataset[attr] || '') === (cur || '');
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', String(on));
  });
  seg('#sDealFilter [data-sdeal]', 'sdeal', f.deal);
  seg('#sSourceFilter [data-ssource]', 'ssource', f.source);
  if (_saleAreaMsd) _saleAreaMsd.setSelected(f.areas);
  const nMore = _saleChips(f).filter(c => ['condition', 'listed', 'source'].includes(c.key)).length;
  const badge = document.getElementById('sMoreBadge');
  if (badge) { badge.textContent = nMore || ''; badge.hidden = !nMore; }
  document.getElementById('sMoreBtn')?.classList.toggle('on', nMore > 0);
}

function _saleChips(f) {
  const c = [], add = (key, label) => c.push({ key, label });
  if (f.areas.length) add('areas', f.areas.length > 2 ? `${f.areas.slice(0, 2).join(', ')} +${f.areas.length - 2}` : f.areas.join(', '));
  if (f.maxPrice)  add('maxPrice', t('chip.max_rent', { n: fmt(f.maxPrice) }));
  if (f.minRooms)  add('minRooms', t('chip.min_rooms', { n: f.minRooms }));
  if (f.minSqm)    add('minSqm', t('chip.min_sqm', { n: f.minSqm }));
  if (f.deal)      add('deal', t(f.deal === 'below' ? 'sale.deal_below' : 'sale.deal_yield'));
  if (f.condition) add('condition', t('cond.' + f.condition));
  if (f.listed)    add('listed', t('chip.listed_days', { n: f.listed }));
  if (f.source)    add('source', siteName(f.source));
  return c;
}

function _renderSaleChips(f, shown) {
  const box = document.getElementById('sActiveChips');
  if (!box) return;
  const chips = _saleChips(f), esc = escapeHtml;
  box.innerHTML = chips.map(c =>
    `<button type="button" class="fchip" data-sclear="${esc(c.key)}" aria-label="${esc(t('chip.remove') + ': ' + c.label)}">${esc(c.label)}${icon('x')}</button>`).join('')
    + (chips.length ? `<button type="button" class="btn btn-ghost btn-sm" data-sclear="all">${esc(t('filter.clear_filters'))}</button>` : '')
    + `<span class="fchips-count"><strong>${fmt(shown)}</strong> ${esc(t(shown === 1 ? 'sale.flat' : 'sale.flats'))}</span>`;
}

function _clearSaleFilter(key) {
  if (key === 'all') {
    const f = saleFilters();   // the mortgage inputs and the sort are settings, not filters
    Storage.set('saleFilters', Object.assign({}, SALE_DEFAULTS, { sort: f.sort, down: f.down, rate: f.rate, years: f.years }));
    _saleAreaMsd?.clear();
    renderSales();
    return;
  }
  _setSaleFilters({ [key]: Array.isArray(SALE_DEFAULTS[key]) ? [] : SALE_DEFAULTS[key] });
}

function _saleMatches(l, f) {
  if (f.areas.length) {
    // Both sites' names: "porta romana" ⊂ "porta romana - medaglie d'oro".
    const nb = (l.neighbourhood || '').toLowerCase();
    if (!nb || !f.areas.some(a => { const al = a.toLowerCase(); return nb.includes(al) || al.includes(nb); })) return false;
  }
  if (f.maxPrice && l.price > f.maxPrice) return false;
  if (f.minRooms && (l.rooms ?? 0) < f.minRooms) return false;
  if (f.minSqm && (l.sqm ?? 0) < f.minSqm) return false;
  if (f.deal === 'below' && !(l.comps_sale_delta_pct != null && l.comps_sale_delta_pct <= -10)) return false;
  if (f.deal === 'yield' && !((l.estimated_yield_pct ?? 0) >= 5)) return false;
  if (f.condition) {
    const g = conditionGroup(l.condition);
    if (f.condition === 'premium' ? g !== 'premium' : g === 'needs_work') return false;
  }
  if (f.listed) {
    const d = new Date(); d.setDate(d.getDate() - Number(f.listed));
    if (!l.first_seen_date || l.first_seen_date < d.toISOString().slice(0, 10)) return false;
  }
  if (f.source && (l.source || 'sale') !== f.source) return false;
  return true;
}

function _saleSorted(rows, f) {
  const key = {
    score: l => -(l.score_total ?? 0),
    price: l => l.price ?? Infinity,
    psqm: l => (l.ask_psqm ?? (l.sqm ? l.price / l.sqm : Infinity)),
    yield: l => -(l.estimated_yield_pct ?? -Infinity),
    delta: l => l.comps_sale_delta_pct ?? Infinity,
    newest: l => -new Date(l.first_seen_date || 0).getTime(),
  }[f.sort] || (l => -(l.score_total ?? 0));
  return rows.slice().sort((a, b) => key(a) - key(b));
}

function renderSales() {
  const grid = document.getElementById('saleGrid');
  if (!grid) return;
  _initSaleBar();
  const f = saleFilters();
  _syncSaleBar(f);
  const esc = escapeHtml;
  if (_saleLoading && !ALL_SALE.length) {
    _renderSaleChips(f, 0);
    grid.innerHTML = `<div class="empty-state">${esc(t('sale.loading'))}</div>`;
    return;
  }
  const rows = _saleSorted(ALL_SALE.filter(l => _saleMatches(l, f)), f);
  _renderSaleChips(f, rows.length);
  if (!rows.length) {
    grid.innerHTML = `<div class="empty-state">${esc(t(ALL_SALE.length ? 'sale.none' : 'sale.empty'))}
      ${ALL_SALE.length && _saleChips(f).length ? `<button type="button" class="btn btn-secondary btn-sm" data-sclear-grid>${esc(t('filter.clear_filters'))}</button>` : ''}</div>`;
    grid.querySelector('[data-sclear-grid]')?.addEventListener('click', () => _clearSaleFilter('all'));
    return;
  }
  grid.innerHTML = '';
  _saleRows = rows;
  _saleShown = 0;
  _saleAppendBatch(f);
}

// Infinite scroll, as Rentals: 60 cards, then 60 more as the end nears.
let _saleRows = [], _saleShown = 0, _saleObserver = null;
function _saleAppendBatch(f = saleFilters()) {
  const grid = document.getElementById('saleGrid');
  if (!grid) return;
  const batch = _saleRows.slice(_saleShown, _saleShown + 60);
  document.getElementById('saleSentinel')?.remove();
  if (!batch.length) return;
  grid.insertAdjacentHTML('beforeend', batch.map(l => saleCardHtml(l, f)).join(''));
  _saleShown += batch.length;
  if (typeof _observeScoreRings === 'function') _observeScoreRings(grid, false);   // final state, no count-up
  if (_saleObserver) { _saleObserver.disconnect(); _saleObserver = null; }
  const status = document.getElementById('saleScrollStatus');
  if (status) status.textContent = _saleShown < _saleRows.length
    ? t('scroll.of', { a: fmt(_saleShown), b: fmt(_saleRows.length) }) : '';
  if (_saleShown >= _saleRows.length) return;
  const s = document.createElement('div');
  s.id = 'saleSentinel';
  s.style.cssText = 'height:1px;pointer-events:none;grid-column:1/-1';
  grid.appendChild(s);
  _saleObserver = new IntersectionObserver(entries => { if (entries[0].isIntersecting) _saleAppendBatch(); },
                                           { rootMargin: '0px 0px 1200px 0px', threshold: 0 });
  _saleObserver.observe(s);
}

// ── Card ─────────────────────────────────────────────────────────────────────
function _saleTitle(l) {
  return formatAddress(l.neighbourhood) || (l.address ? formatAddress(l.address) : '') || (l.title || '').trim() || '–';
}

function _saleDeltaHtml(l) {
  const dp = l.comps_sale_delta_pct;
  if (dp == null) return '';
  const tone = dp <= -5 ? 'good' : dp >= 5 ? 'bad' : 'at';
  return `<span class="deal-delta tone-${tone}" title="${escapeHtml(t('sale.delta_tip', { n: l.comps_sale_n || 0 }))}">${escapeHtml(Math.abs(dp) < 1 ? t('card.in_line') : t('sale.vs_nearby', { pct: fmtPct(dp) }))}</span>`;
}

function _saleSeenMeta(l) {
  // Everything was first seen on the first sweep: no "new today" for those.
  if (!l.first_seen_date || l.first_seen_date <= SALE_SINCE) return '';
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const d = new Date(l.first_seen_date); d.setHours(0, 0, 0, 0);
  const days = Math.round((today - d) / 86400000);
  return `<span title="${escapeHtml(fmtDate(l.first_seen_date))}">${icon('clock')}${escapeHtml(days <= 0 ? t('card.seen_today') : t('card.seen_days', { n: fmt(days) }))}</span>`;
}

function saleCardHtml(l, f) {
  const esc = escapeHtml;
  const psqm = l.ask_psqm || (l.sqm ? Math.round(l.price / l.sqm) : null);
  const pay = mortgagePayment(l.price, f);
  const condo = Number(l.spese_condominiali || l.condominium_fees) || 0;
  // The three numbers that matter for buying: payment, rent it would fetch, yield.
  const figs = [
    pay ? `<span title="${esc(t('sale.pay_tip', { down: fmt(f.down), rate: fmt1(f.rate), years: fmt(f.years) }))}">${icon('calendar')}${esc(t('sale.card_pay', { v: fmt(_round10(pay)) }))}</span>` : '',
    l.estimated_rent_mo ? `<span title="${esc(t('sale.rent_tip', { n: l.estimated_rent_n_comps || 0 }))}">${icon('key')}${esc(t('sale.card_rent', { v: fmt(l.estimated_rent_mo), y: fmt1(l.estimated_yield_pct ?? 0) }))}</span>` : '',
    condo ? `<span>${icon('building')}${esc(t('sale.card_condo', { v: fmt(condo) }))}</span>` : '',
  ].filter(Boolean).join('');
  const isNew = l.first_seen_date && l.first_seen_date > SALE_SINCE
    && (Date.now() - new Date(l.first_seen_date)) / 86400000 <= 3;
  const site = siteName(l.source);
  const id = esc(String(l.id));
  const title = _saleTitle(l);
  const street = l.address ? formatAddress(String(l.address).replace(/,?\s*milano\s*$/i, '')) : '';
  const meta = [typeof _metroMeta === 'function' ? _metroMeta(l) : '', _saleSeenMeta(l)].filter(Boolean).join('');
  return `<article class="card sale-card" data-listing-id="${id}">
    ${l.thumbnail ? `<button type="button" class="card-photo-wrap" data-open-sale="${id}" aria-label="${esc(t('card.details') + ': ' + title)}"><img class="card-thumb" src="${_safeUrl(l.thumbnail)}" alt="" loading="lazy">${isNew ? `<span class="card-photo-new-badge">${esc(t('badge.new'))}</span>` : ''}</button>` : ''}
    <div class="card-body">
      <div class="card-head">
        <div class="card-title-wrap">
          <h2 class="card-title"><button type="button" class="card-title-btn" data-open-sale="${id}">${esc(title)}</button></h2>
          <div class="card-sub">${esc([street && street.toLowerCase() !== title.toLowerCase() ? street : '', site, alsoOnSites(l) ? t('card.also_on', { sites: alsoOnSites(l) }) : ''].filter(Boolean).join(' · '))}</div>
        </div>
        ${scoreRingHtml(l)}
      </div>
      <div class="card-price">
        <span class="price-main">€${fmt(l.price)}</span>
        ${psqm ? `<span class="price-unit">€${fmt(Math.round(psqm))}/m²</span>` : ''}
        ${_saleDeltaHtml(l)}
      </div>
      ${figs ? `<div class="sale-figs">${figs}</div>` : ''}
      <div class="card-specs">${_specsParts(l).map(p => `<span>${esc(p)}</span>`).join('')}</div>
      ${meta ? `<div class="card-meta">${meta}</div>` : ''}
    </div>
    <div class="card-footer">
      <button type="button" class="btn btn-secondary" data-open-sale="${id}">${esc(t('card.details'))}</button>
      <a class="btn btn-primary btn-open" href="${_safeUrl(l.url)}" target="_blank" rel="noopener">${esc(t('card.open'))}${icon('external-link')}</a>
    </div>
  </article>`;
}

// ── Details (the rentals' #listingModal, with sale content) ─────────────────
function openSaleModal(id) {
  const l = ALL_SALE.find(x => String(x.id) === String(id));
  const box = document.getElementById('listingModal');
  if (!l || !box) return;
  _lmListing = l;
  if (box.hidden) _lmLastFocus = document.activeElement;
  document.getElementById('lmBody').innerHTML = saleModalHtml(l);
  box.hidden = false;
  document.body.classList.add('lm-open');
  box.querySelector('.lm-close')?.focus();
  _lmLoadPhotos(l);
}

document.addEventListener('click', e => {
  const o = e.target.closest && e.target.closest('[data-open-sale]');
  if (o) { e.preventDefault(); openSaleModal(o.dataset.openSale); return; }
  // "Change down payment, rate, years" in Details → the bar's More filters.
  if (e.target.closest && e.target.closest('[data-sale-mortgage]')) {
    document.querySelector('#listingModal .lm-close')?.click();
    _toggleSaleMore(true);
    document.getElementById('sDown')?.focus();
  }
});

function _saleVerdict(l) {
  const dp = l.comps_sale_delta_pct;
  if (dp == null) return { cls: '', head: t('sale.no_comps'), sub: '' };
  const conf = { high: t('conf.high'), medium: t('conf.medium'), low: t('conf.thin') }[(l.comps_sale_conf_label || '').toLowerCase()];
  return {
    cls: dp <= -5 ? 'good' : dp >= 5 ? 'bad' : '',
    head: Math.abs(dp) < 1 ? t('card.in_line') : t(dp < 0 ? 'sale.below' : 'sale.above', { pct: fmtNum(Math.abs(dp), 1) }),
    sub: [t('sale.delta_tip', { n: l.comps_sale_n || 0 }),
          l.comps_sale_p40 != null && l.comps_sale_p60 != null
            ? t('sale.middle', { a: fmt(Math.round(l.comps_sale_p40)), b: fmt(Math.round(l.comps_sale_p60)) }) : null,
          conf].filter(Boolean).join(' · '),
  };
}

// Where the asking €/m² sits: nearby sales (median, middle half) and the
// official OMI range, on one scale.
function _saleScaleHtml(l, psqm) {
  const esc = escapeHtml;
  const med = l.comps_sale_median, lo = l.omi_compr_min, hi = l.omi_compr_max;
  const vals = [psqm, med, lo, hi].filter(v => v != null && v > 0);
  if (!psqm || vals.length < 2) return '';
  const min = Math.min(...vals) * 0.92, max = Math.max(...vals) * 1.06;
  const pos = v => Math.max(0, Math.min(100, (v - min) / (max - min) * 100));
  const dp = l.comps_sale_delta_pct;
  const tone = dp == null ? '' : dp <= -5 ? 'good' : dp >= 5 ? 'bad' : 'at';
  return `<div class="sale-scale" role="img" aria-label="${esc(t('sale.scale_aria', { v: fmt(Math.round(psqm)) }))}">
    <div class="sale-scale-track">
      ${lo && hi ? `<span class="sale-scale-omi" style="left:${pos(lo)}%;width:${pos(hi) - pos(lo)}%"></span>` : ''}
      ${med ? `<span class="sale-scale-median" style="left:${pos(med)}%"></span>` : ''}
      <span class="sale-scale-this tone-${tone}" style="left:${pos(psqm)}%"></span>
    </div>
    <div class="sale-scale-legend">
      <span><i class="k-this tone-${tone}"></i>${esc(t('sale.scale_this', { v: fmt(Math.round(psqm)) }))}</span>
      ${med ? `<span><i class="k-median"></i>${esc(t('sale.scale_median', { v: fmt(Math.round(med)) }))}</span>` : ''}
      ${lo && hi ? `<span><i class="k-omi"></i>${esc(t('sale.scale_omi', { a: fmt(Math.round(lo)), b: fmt(Math.round(hi)) }))}</span>` : ''}
    </div>
  </div>`;
}

function saleModalHtml(l) {
  const esc = escapeHtml, f = saleFilters();
  const yesNo = v => v === true ? t('lm.yes') : v === false ? t('lm.no') : null;
  const heat = { autonomous: t('lm.heat_own'), centralised: t('lm.heat_central'), none: t('lm.heat_none') };
  const psqm = l.ask_psqm || (l.sqm ? Math.round(l.price / l.sqm) : null);
  const site = siteName(l.source);
  const id = esc(String(l.id));
  const v = _saleVerdict(l);
  const down = l.price * (Number(f.down) || 0) / 100, pay = _round10(mortgagePayment(l.price, f));
  const condo = Number(l.spese_condominiali || l.condominium_fees) || 0;
  const rent = l.estimated_rent_mo, y = l.estimated_yield_pct;
  const tile = (k, val, sub, cls = '') => `<div class="sale-tile ${cls}"><div class="sale-tile-k">${esc(k)}</div>
      <div class="sale-tile-v">${val}</div>${sub ? `<div class="sale-tile-s">${esc(sub)}</div>` : ''}</div>`;
  const perMonth = n => `€${fmt(n)}<span>/${esc(t('sale.month'))}</span>`;
  const tiles = [
    tile(t('sale.t_payment'), perMonth(pay), t('sale.t_payment_sub', { down: fmt(Math.round(down)), p: fmt(f.down), rate: fmt1(f.rate), years: fmt(f.years) })),
    rent ? tile(t('sale.est_rent'), perMonth(rent), t('sale.t_rent_sub', { n: fmt(l.estimated_rent_n_comps || 0) }))
         : tile(t('sale.est_rent'), '–', t('sale.no_rent'), 'is-muted'),
    y ? tile(t('sale.gross_yield'), `${fmt1(y)}%`, condo && rent ? t('sale.t_net', { v: fmt1((rent - condo) * 12 / l.price * 100) }) : t('sale.t_gross_sub'),
             y >= 5 ? 'is-good' : '')
      : tile(t('sale.gross_yield'), '–', '', 'is-muted'),
    condo ? tile(t('sale.condo_row'), perMonth(condo), l.heating_mo ? t('sale.t_heating', { v: fmt(l.heating_mo) }) : '')
          : tile(t('sale.condo_row'), esc(t('cost.not_stated')), t('sale.t_condo_ask'), 'is-muted'),
  ].join('');
  // Buying vs renting a similar flat, month by month (both estimates).
  const compare = rent && pay ? `<div class="sale-compare">${icon('home')}<span>${esc(t(condo ? 'sale.compare' : 'sale.compare_nocondo', {
      buy: fmt(pay + condo), rent: fmt(rent) }))}</span></div>` : '';
  const floor = (() => {
    const lbl = _floorLabel(l);
    if (!lbl) return null;
    return lbl + (l.elevator === true ? ' · ' + t('lm.lift') : l.elevator === false ? ' · ' + t('lm.no_lift') : '');
  })();
  const facts = [
    [t('lm.size'), l.sqm ? `${fmt(l.sqm)} m²` : null],
    [t('lm.rooms'), l.rooms != null ? `${l.rooms}${l.bedrooms ? ' · ' + t('lm.bedrooms', { n: l.bedrooms }) : ''}` : null],
    [t('lm.bathrooms'), l.bathrooms],
    [t('lm.floor'), floor],
    [t('lm.condition'), l.condition ? String(l.condition).replace(' (Idealista)', '') : null],
    [t('lm.heating'), l.heating_type ? (heat[l.heating_type] ?? null) : null],
    [t('must.balcony'), yesNo(l.has_balcony)],
    [t('must.parking'), yesNo(l.has_parking)],
    [t('lm.energy'), l.energy_class],
    [t('lm.year'), l.year_built],
    [t('lm.exposure'), l.is_external === true ? t('lm.external') : l.is_external === false ? t('lm.internal') : null],
  ];
  const known   = facts.filter(([, val]) => val != null && val !== '');
  const unknown = facts.filter(([, val]) => val == null || val === '').map(([k]) => k);
  const w = { price: scoreSettings.w_price ?? 0.45, prop: scoreSettings.w_property ?? 0.25,
              loc: scoreSettings.w_location ?? 0.20, pen: scoreSettings.w_penalty ?? 0.10 };
  const bar = (k, val, wt) => `<div class="lm-bar sc-${scoreClass(val ?? 50)}"><span class="lm-bar-k">${esc(t(k))} <span class="lm-muted">${Math.round(wt * 100)}%</span></span>
      <span class="lm-bar-track"><span class="lm-bar-fill" style="width:${Math.max(0, Math.min(100, val ?? 50))}%"></span></span>
      <span class="lm-bar-v">${val ?? '–'}</span></div>`;
  const dist = m => m != null ? `${fmt(Math.round(m))} m` : null;
  const near = (ic, label, val) => val == null ? '' : `<li>${icon(ic)}<span>${esc(label)}</span><span class="lm-muted">${esc(val)}</span></li>`;
  const history = priceHistoryText(l);
  return `
  <div class="lm-head">
    <div class="lm-title">
      <h2 id="lmTitle">${esc([formatAddress(l.neighbourhood), l.address ? formatAddress(l.address) : ''].filter(Boolean).join(' · ') || _saleTitle(l))}</h2>
      <div class="lm-sub">${esc(site)}${typeof _lmAlsoOn === 'function' ? _lmAlsoOn(l) : ''} · ID ${id}${l.first_seen_date ? ' · ' + esc(t('lm.first_seen', { date: _lmDate(l.first_seen_date) })) : ''}</div>
    </div>
    <button type="button" class="btn btn-secondary btn-icon lm-close" aria-label="${esc(t('lm.close'))}">${icon('x')}</button>
  </div>
  <div class="lm-scroll">
    <div class="lm-photos" id="lmPhotos"></div>
    <div class="lm-verdict ${v.cls}">
      <div class="lm-verdict-head">${esc(v.head)}</div>
      ${v.sub ? `<div class="lm-verdict-sub">${esc(v.sub)}</div>` : ''}
    </div>
    ${typeof _lmPhotoVerdict === 'function' ? _lmPhotoVerdict(l) + _lmPhotoFeedbackHtml(l) : ''}

    <div class="lm-money">
      <span class="lm-rent">€${fmt(l.price)}</span>
      ${psqm ? `<span class="lm-muted">€${fmt(Math.round(psqm))}/m²</span>` : ''}
    </div>
    ${history ? `<div class="lm-history">${icon('trending-down')}${esc(history)}</div>` : ''}
    ${_saleScaleHtml(l, psqm)}

    <h3 class="lm-section-title">${esc(t('sale.numbers'))}</h3>
    <div class="sale-tiles">${tiles}</div>
    ${compare}
    <p class="lm-note">${icon('info-circle')}<span>${esc(t('sale.estimate_note'))} · <button type="button" class="btn-link" data-sale-mortgage>${esc(t('sale.change_mortgage'))}</button></span></p>

    <h3 class="lm-section-title">${esc(t('lm.facts'))}</h3>
    <div class="lm-facts">${known.map(([k, val]) => `<div class="lm-fact"><div class="lm-fact-k">${esc(k)}</div><div class="lm-fact-v">${esc(String(val))}</div></div>`).join('')}</div>
    ${unknown.length ? `<div class="lm-unknown-line">${esc(t('lm.not_known_list', { list: unknown.join(', ') }))}</div>` : ''}

    <div class="lm-two">
      <section>
        <h3 class="lm-section-title">${esc(t('lm.location'))}</h3>
        <ul class="lm-near">
          ${near('train', l.metro_nearest_name ? `${l.metro_nearest_name}${l.metro_nearest_line ? ' ' + l.metro_nearest_line : ''}` : t('lm.metro'), l.metro_walk_min != null ? t('lm.walk', { n: l.metro_walk_min }) : dist(l.metro_nearest_dist_m))}
          ${near('bus', t('lm.tram'), dist(l.tram_nearest_dist_m))}
          ${near('shopping-cart', t('lm.supermarket'), dist(l.supermarket_nearest_dist_m))}
          ${near('trees', t('lm.park'), dist(l.park_nearest_dist_m))}
          ${near('school', t('lm.university'), dist(l.university_nearest_dist_m))}
        </ul>
      </section>
      <section>
        <h3 class="lm-section-title">${esc(t('lm.score', { n: l.score_total ?? '–' }))}</h3>
        ${bar('lm.price', l.score_price, w.price)}
        ${bar('lm.location', l.score_location ?? l.geo_score, w.loc)}
        ${bar('lm.property', l.score_property, w.prop)}
        ${bar('lm.red_flags', l.score_penalty, w.pen)}
        ${l.score_cap_reason ? `<div class="lm-cap">${esc(t('card.cap', { reason: l.score_cap_reason }))}</div>` : ''}
        <div>${typeof reasonsBulletsHtml === 'function' ? reasonsBulletsHtml(l) : ''}</div>
      </section>
    </div>
  </div>
  <div class="lm-actions">
    <a class="btn btn-primary lm-open" href="${_safeUrl(l.url)}" target="_blank" rel="noopener"><span>${esc(t('lm.open_on', { site }))}</span>${icon('external-link')}</a>
  </div>`;
}

// ── Tools → Areas for sale ───────────────────────────────────────────────────
// Which macro-areas the scanners read for sale and the price / rooms limits
// (api.py /api/sale-areas → sales_scan.set_sale_areas; both daemons sweep
// the new set at their next pause). Local dashboard only.
let _saleCfg = null;

async function loadSaleAreasCard() {
  const card = document.getElementById('saleAreasCard');
  if (!card) return;
  card.hidden = !(typeof IS_LIVE !== 'undefined' && IS_LIVE);
  if (card.hidden) return;
  try { _saleCfg = await fetch('/api/sale-areas').then(r => r.json()); } catch { _saleCfg = null; }
  if (!_saleCfg) return;
  document.getElementById('saleCfgPrice').value = _saleCfg.max_price || '';
  document.getElementById('saleCfgRooms').value = _saleCfg.min_rooms || '';
  _renderSaleAreasGrid();
  if (card.dataset.wired) return;
  card.dataset.wired = '1';
  document.getElementById('saleAreasGrid').addEventListener('click', e => {
    const c = e.target.closest('.scan-area-card');
    if (!c) return;
    e.preventDefault();
    const sel = new Set(_saleCfg.selected || []);
    sel.has(c.dataset.slug) ? sel.delete(c.dataset.slug) : sel.add(c.dataset.slug);
    _saleCfg.selected = [...sel];
    _renderSaleAreasGrid();
  });
  document.getElementById('btnSaleAreasAll').addEventListener('click', () => {
    _saleCfg.selected = _saleCfg.areas.map(a => a.slug); _renderSaleAreasGrid();
  });
  document.getElementById('btnSaleAreasNone').addEventListener('click', () => {
    _saleCfg.selected = []; _renderSaleAreasGrid();
  });
  document.getElementById('btnSaleAreasSave').addEventListener('click', async () => {
    const status = document.getElementById('saleAreasStatus');
    try {
      _saleCfg = await fetch('/api/sale-areas', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ immobiliare: _saleCfg.selected,
                               max_price: Number(document.getElementById('saleCfgPrice').value) || null,
                               min_rooms: Number(document.getElementById('saleCfgRooms').value) || null }),
      }).then(r => r.json());
      status.textContent = t('sale.scan_saved');
      _renderSaleAreasGrid();
    } catch {
      status.textContent = t('sale.scan_error');
    }
  });
}

function _renderSaleAreasGrid() {
  const grid = document.getElementById('saleAreasGrid');
  if (!grid || !_saleCfg) return;
  const sel = new Set(_saleCfg.selected || []);
  grid.innerHTML = (_saleCfg.areas || []).map(a => {
    const on = sel.has(a.slug);
    return `<label class="scan-area-card${on ? ' scan-area-card-on' : ''}" data-slug="${escapeHtml(a.slug)}">
        <input type="checkbox"${on ? ' checked' : ''}>
        <div class="scan-area-card-content"><div class="scan-area-name">${escapeHtml(a.name)}</div></div>
        <div class="scan-area-toggle"><div class="toggle-track${on ? ' toggle-on' : ''}"><div class="toggle-thumb"></div></div></div>
      </label>`;
  }).join('');
  const count = document.getElementById('saleAreasCount');
  if (count) count.textContent = t('sale.areas_count', { n: sel.size, total: (_saleCfg.areas || []).length });
}
