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
                        down: 20, rate: 3.5, years: 25, condition: '', listed: '', source: '',
                        minFloor: '', maxMetro: '', minEnergy: '', minPhoto: '' };

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
  if (typeof activeTab !== 'undefined' && activeTab === 'foryou' && typeof renderForYou === 'function') renderForYou();
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
  el('sMinFloor')?.addEventListener('change', e => _setSaleFilters({ minFloor: e.target.value }));
  el('sMaxMetro')?.addEventListener('change', e => _setSaleFilters({ maxMetro: e.target.value }));
  el('sMinEnergy')?.addEventListener('change', e => _setSaleFilters({ minEnergy: e.target.value }));
  el('sMinPhoto')?.addEventListener('change', e => _setSaleFilters({ minPhoto: e.target.value }));
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
  set('sMinFloor', f.minFloor); set('sMaxMetro', f.maxMetro); set('sMinEnergy', f.minEnergy); set('sMinPhoto', f.minPhoto);
  const seg = (sel, attr, cur) => document.querySelectorAll(sel).forEach(b => {
    const on = (b.dataset[attr] || '') === (cur || '');
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', String(on));
  });
  seg('#sDealFilter [data-sdeal]', 'sdeal', f.deal);
  seg('#sSourceFilter [data-ssource]', 'ssource', f.source);
  if (_saleAreaMsd) _saleAreaMsd.setSelected(f.areas);
  const nMore = _saleChips(f).filter(c => ['condition', 'listed', 'source', 'minFloor', 'maxMetro', 'minEnergy'].includes(c.key)).length;
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
  if (f.minFloor !== '') add('minFloor', t('chip.min_floor', { n: f.minFloor }));
  if (f.maxMetro)  add('maxMetro', t('chip.max_metro', { n: fmt(Number(f.maxMetro)) }));
  if (f.minEnergy) add('minEnergy', t('chip.min_energy', { v: f.minEnergy }));
  if (f.minPhoto)  add('minPhoto', t('chip.min_photo', { n: f.minPhoto }));
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
  // As Rentals: an unknown floor or metro distance doesn't pass; an unknown
  // energy class does (partial data).
  if (f.minFloor !== '' && (l.floor_n ?? -999) < Number(f.minFloor)) return false;
  if (f.maxMetro && (l.metro_nearest_dist_m == null || l.metro_nearest_dist_m > Number(f.maxMetro))) return false;
  if (f.minEnergy && l.energy_class && (ENERGY_RANK[l.energy_class] || 0) < (ENERGY_MIN_RANK[f.minEnergy] || 0)) return false;
  if (f.minPhoto && !(l.photo_grade != null && l.photo_grade >= Number(f.minPhoto))) return false;   // not graded yet: out
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
    photo: l => -(l.photo_grade ?? -1),
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

// What owning costs a month next to renting it, in one line. Condo fees
// unknown (64 % of sales) are shown as unknown, not taken as €0 — the old
// "Owning: €X/mo with fees" claimed fees it didn't have.
function _ownLineHtml(l, f) {
  const pay = mortgagePayment(l.price, f);
  if (!pay) return '';
  const esc = escapeHtml;
  const condo = Number(l.spese_condominiali || l.condominium_fees) || 0;
  const known = condo > 0 || l.condo_included === true;
  const own = _round10(pay + condo);
  const rent = l.estimated_rent_mo;
  let html = `<b>€${fmt(own)}</b>${known ? '' : ' + ' + _unkHtml(t('own.condo'))} ${esc(t('own.to_own'))}`;
  if (rent && known) {
    const gap = own - rent;
    html += '<i aria-hidden="true">·</i>' + (Math.abs(gap) < 50 ? `<span>${esc(t('own.same'))}</span>`
      : `<span class="${gap > 0 ? 'tone-warn' : 'tone-good'}">${esc(t(gap > 0 ? 'own.more' : 'own.less', { v: fmt(_round10(Math.abs(gap))) }))}</span>`);
  } else if (rent) {
    html += `<i aria-hidden="true">·</i><span title="${esc(t('sale.rent_tip', { n: l.estimated_rent_n_comps || 0 }))}">${esc(t('own.rent', { v: fmt(_round10(rent)) }))}</span>`;
  }
  return `<div class="card-own" title="${esc(t('sale.pay_tip', { down: fmt(f.down), rate: fmt1(f.rate), years: fmt(f.years) }))}">${html}</div>`;
}

// Freshness on a sale card: online for over six months in amber (27 % of
// dated sales), else "seen N days ago" — but not for the first sweep's rows.
const LONG_ONLINE_SALE_DAYS = 180;
function _saleFreshHtml(l) {
  const d = daysOnline(l);
  if (d != null && d > LONG_ONLINE_SALE_DAYS) return _freshFootHtml(l, LONG_ONLINE_SALE_DAYS);
  if (!l.first_seen_date || l.first_seen_date <= SALE_SINCE) return '';
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const day = new Date(l.first_seen_date); day.setHours(0, 0, 0, 0);
  const days = Math.round((today - day) / 86400000);
  return `<span class="foot-fresh" title="${escapeHtml(fmtDate(l.first_seen_date))}">${escapeHtml(days <= 0 ? t('card.seen_today') : t('card.seen_days', { n: fmt(days) }))}</span>`;
}

// opts.fit: For you shows its fit in the ring instead of the score.
function saleCardHtml(l, f, opts = {}) {
  const esc = escapeHtml;
  const psqm = l.ask_psqm || (l.sqm ? Math.round(l.price / l.sqm) : null);
  const isNew = l.first_seen_date && l.first_seen_date > SALE_SINCE
    && (Date.now() - new Date(l.first_seen_date)) / 86400000 <= 3;
  const site = siteName(l.source);
  const id = esc(String(l.id));
  const title = _saleTitle(l);
  const street = l.address ? formatAddress(String(l.address).replace(/,?\s*milano\s*$/i, '')) : '';
  const others = alsoOnSites(l) ? alsoOnSites(l).split(', ').length : 0;
  const openLabel = (site === 'Casa.it' ? site : site.replace('.it', '')) + (others ? ` +${others}` : '');
  const topRight = _priceCutBadge(l) || (isNew ? `<span class="card-photo-new-badge">${esc(t('badge.new'))}</span>` : '');
  const vs = _vsNearbyHtml(l.comps_sale_delta_pct, t('sale.delta_tip', { n: l.comps_sale_n || 0 }));
  return `<article class="card sale-card" data-listing-id="${id}" data-card-open="sale">
    ${l.thumbnail ? `<div class="card-photo" data-photo-id="${id}"><div class="card-photo-wrap" data-open-sale="${id}" aria-label="${esc(t('card.details') + ': ' + title)}"><div class="card-photo-track"><img class="card-thumb" src="${_safeUrl(l.thumbnail)}" alt="" loading="lazy" draggable="false"></div>${topRight}</div>${(l.photo_count || 0) !== 1 ? `
       <button type="button" class="card-photo-nav prev" data-photo-step="-1" aria-label="${esc(t('pv.prev'))}" tabindex="-1">${icon('chevron-left')}</button>
       <button type="button" class="card-photo-nav next" data-photo-step="1" aria-label="${esc(t('pv.next'))}" tabindex="-1">${icon('chevron-right')}</button>
       <span class="card-photo-count" hidden></span>` : ''}</div>` : ''}
    <div class="card-body">
      <div class="card-top">
        <div class="card-top-main">
          <div class="card-price"><span class="price-main">€${fmt(l.price)}</span></div>
          ${psqm ? `<div class="card-money"><span>€${fmt(Math.round(psqm))}/m²</span>${vs ? '<i aria-hidden="true">·</i>' + vs : ''}</div>` : ''}
        </div>
        ${opts.fit != null ? scoreRingHtml(l, 40, { value: opts.fit, caption: t('ring.fit') }) : scoreRingHtml(l, 40, { caption: t('ring.score') })}
      </div>
      ${_factsHtml(l)}
      ${_placeHtml(title, street, `data-open-sale="${id}"`)}
      ${_ownLineHtml(l, f)}
    </div>
    ${_cardFootHtml({
      metaHtml: (typeof _metroFootHtml === 'function' ? _metroFootHtml(l) : '') + _saleFreshHtml(l),
      favHtml: (() => { const fav = isSaleFav(l.id); return `<button type="button" class="btn btn-secondary btn-icon btn-fav${fav ? ' is-fav' : ''}" data-sale-id="${id}"
              aria-pressed="${fav}" aria-label="${esc(t(fav ? 'card.unsave' : 'card.save'))}" title="${esc(t(fav ? 'card.unsave' : 'card.save'))}">${favBtnInner()}</button>`; })(),
      openHtml: `<a class="btn btn-secondary btn-open" href="${_safeUrl(l.url)}" target="_blank" rel="noopener"
         title="${esc(t('lm.open_on', { site }) + (others ? ' · ' + t('card.also_on', { sites: alsoOnSites(l) }) : ''))}">${esc(openLabel)}${icon('external-link')}</a>`,
    })}
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
  box.classList.remove('is-past-sum');
  _lmLoadPhotos(l);
  lmLoadMapSnip(l);
  _lmAfterRender(l, 'sale');
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



function saleModalHtml(l) {
  return lmSheetHtml(l, 'sale');
}

// Owning vs renting: one table in the rentals' cost-table style — payment
// (with its terms), condo fees, the monthly total, renting a similar flat,
// and the gap; the yield as one quiet line under it.
function _saleOwnTableHtml(l, f) {
  const esc = escapeHtml;
  const pay = _round10(mortgagePayment(l.price, f));
  if (!pay) return '';
  const down = l.price * (Number(f.down) || 0) / 100;
  const condo = Number(l.spese_condominiali || l.condominium_fees) || 0;
  const known = condo > 0 || l.condo_included === true;
  const heat = Number(l.heating_mo) || 0;
  const own = _round10(pay + condo + heat);
  const rent = l.estimated_rent_mo;
  const row = (k, v, cls = '') => `<tr class="${cls}"><td>${k}</td><td>${v}</td></tr>`;
  let rows = row(esc(t('sale.t_payment')), `€${fmt(pay)}`);
  rows += `<tr class="is-sub"><td colspan="2">${esc(t('sale.t_payment_sub', { down: fmt(Math.round(down)), p: fmt(f.down), rate: fmt1(f.rate), years: fmt(f.years) }))} · <button type="button" class="btn-link" data-sale-mortgage>${esc(t('sale.change'))}</button></td></tr>`;
  rows += row(esc(t('sale.condo_row')), known ? `€${fmt(condo)}` : `<span class="lm-muted">${esc(t('cost.not_stated'))}</span>`, known ? '' : 'is-muted');
  if (heat) rows += row(esc(t('cost.heating')), `€${fmt(heat)}`);
  rows += row(esc(t('sale.to_own_row')), known ? `€${fmt(own)}` : `€${fmt(own)} + ${_unkHtml(t('own.condo'))}`, 'is-total');
  rows += row(esc(t('sale.rent_similar')), rent ? `~€${fmt(_round10(rent))}` : `<span class="lm-muted">${esc(t('sale.no_rent'))}</span>`);
  if (rent) rows += `<tr class="is-sub"><td colspan="2">${esc(t('sale.t_rent_sub', { n: fmt(l.estimated_rent_n_comps || 0) }))}</td></tr>`;
  if (rent) {
    const gap = own - rent;
    const txt = !known ? `<span class="lm-muted">${esc(t('sale.no_compare_condo'))}</span>`
      : Math.abs(gap) < 50 ? esc(t('own.same'))
      : `<span class="tone-${gap > 0 ? 'warn' : 'good'}">${esc(t(gap > 0 ? 'own.more' : 'own.less', { v: fmt(_round10(Math.abs(gap))) }))}</span>`;
    rows += `<tr class="is-gap"><td colspan="2">${txt}</td></tr>`;
  }
  const y = l.estimated_yield_pct;
  const yieldLine = y ? `<div class="lm-yield">${esc(t('sale.yield_line', { g: fmt1(y) }))}${known && rent ? esc(t('sale.yield_net', { n: fmt1((rent - condo) * 12 / l.price * 100) })) : ''}</div>` : '';
  return `<section class="lm-sec" id="lmOwn"><h3 class="lm-sec-title">${esc(t('sale.own_vs_rent'))}</h3>
    <table class="lm-costs lm-own">${rows}</table>${yieldLine}<div class="lm-fine">${esc(t('sale.estimate_note'))}</div></section>`;
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
