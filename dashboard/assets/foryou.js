// ── For you ───────────────────────────────────────────────────────────────────
// The best listings for what the user can afford (2026-10-25): their net pay,
// cash and the share of pay they'd put into a home turn into three budgets —
// a bilocale alone, a flat with two bedrooms split in two, and the price they
// could buy at — and each budget gets the best listings in their zones.
// Pay and cash stay in this browser (Storage → localStorage), never online.
// Estimates, not financial advice.

const FY_DEFAULTS = { income: 0, cash: 0, share: 33, reserve: 0, costs: 8,
                      areas: [], mode: 'rent', noStudio: true, floor: '' };
const FY_PAGE = 6;                       // cards per list, "show more" adds as many

let _fyMsd = null;
const _FY_AREA_ITEMS = [];
const FY_NEAR_PAGE = 3;                  // "near your zones": a shorter list below
const _FY_SHOWN0 = { solo: FY_PAGE, duo: FY_PAGE, buy: FY_PAGE,
                     soloNear: FY_NEAR_PAGE, duoNear: FY_NEAR_PAGE, buyNear: FY_NEAR_PAGE };
const _fyShown = { ..._FY_SHOWN0 };

function fyProfile() {
  return Object.assign({}, FY_DEFAULTS, Storage.get('forYou', {}));
}

// The mortgage terms are the sales tab's (down %, rate, years): one set of
// numbers for every payment shown on the dashboard.
function _fyLoan() {
  const f = typeof saleFilters === 'function' ? saleFilters() : {};
  return { down: Number(f.down) || 20, rate: Number(f.rate) || 3.5, years: Number(f.years) || 25 };
}

// The three budgets. Buying: the lower of what the cash covers (down payment
// plus purchase costs, keeping the reserve) and what a payment within the
// share of pay borrows.
function fyBudgets(p = fyProfile(), loan = _fyLoan()) {
  const solo = p.income * p.share / 100;
  const n = loan.years * 12, r = loan.rate / 1200;
  const maxLoan = r ? solo * (1 - Math.pow(1 + r, -n)) / r : solo * n;
  const byPayment = maxLoan / (1 - loan.down / 100);
  const byCash = Math.max(0, p.cash - p.reserve) / ((loan.down + p.costs) / 100);
  const buy = Math.floor(Math.min(byPayment, byCash) / 1000) * 1000;
  return { solo, duo: solo * 2, buy, limit: byCash < byPayment ? 'cash' : 'payment',
           payment: buy ? mortgagePayment(buy, loan) : 0 };
}

// ── Fit: how well a listing suits this person, not just whether it passes ──
// fit = 35 % the deal (Lume's score) + 25 % how liveable it is for the
// scenario + 15 % room in the budget + 15 % how trustworthy the ad is +
// 10 % how close to the chosen zones. Each part 0–100. The card says why.
const FY_W = { deal: 0.35, live: 0.25, budget: 0.15, trust: 0.15, zone: 0.10 };
const FY_OVER = 1.05;          // up to 5 % over budget still shown, flagged
const FY_NEAR_KM = 1.0;        // outside the zones but this close: shown, flagged
const FY_PER_ZONE = 2;         // at most this many from one neighbourhood in the first page

const _clamp01 = v => Math.max(0, Math.min(1, v));
const _lerp = (v, a, b) => _clamp01((v - a) / (b - a));      // 0 at a, 1 at b

function _km(a, b) {
  const dy = (a[0] - b[0]) * 111.2, dx = (a[1] - b[1]) * 111.2 * Math.cos(a[0] * Math.PI / 180);
  return Math.hypot(dx, dy);
}

// Centre of each chosen zone: the median pin of its listings.
function _fyZoneCentres(areas) {
  const med = a => { const s = [...a].sort((x, y) => x - y); return s[s.length >> 1]; };
  const out = [];
  for (const area of areas) {
    const al = area.toLowerCase();
    const pts = [...(ALL_RENT || []), ...(ALL_SALE || [])].filter(l => l.latitude && l.longitude
      && (l.neighbourhood || '').toLowerCase().includes(al));
    if (pts.length) out.push({ name: area, c: [med(pts.map(l => +l.latitude)), med(pts.map(l => +l.longitude))] });
  }
  return out;
}

// 1 inside a chosen zone, down to 0.3 at FY_NEAR_KM from its centre's edge; null = too far.
function _fyZone(l, p, centres) {
  if (!p.areas.length) return { fit: 1 };
  const nb = (l.neighbourhood || '').toLowerCase();
  if (nb && p.areas.some(a => { const al = a.toLowerCase(); return nb.includes(al) || al.includes(nb); })) return { fit: 1 };
  if (!l.latitude || !l.longitude || !centres.length) return null;
  const near = centres.map(z => ({ z, km: _km([+l.latitude, +l.longitude], z.c) })).sort((a, b) => a.km - b.km)[0];
  // A neighbourhood is ~0.6 km across: count from there.
  const out = near.km - 0.6;
  if (out > FY_NEAR_KM) return null;
  return { fit: 0.3 + 0.7 * (1 - _clamp01(out / FY_NEAR_KM)), km: near.km, zone: near.z.name };
}

// Never proposed: a price too low to trust, a room let, a basement, no
// bedroom, photos that contradict the deal (a grade of 4 or less, or the
// user's own "not convincing").
function _fyLiveable(l) {
  return !l.is_stale && !l.excluded && !l._excluded && !l.price_suspect && !l.extreme_underpricing
         && !l._extreme_underpricing && !l.is_room_let && !(l.floor_n != null && l.floor_n < 0)
         && !(l.photo_flags || []).includes('no_bedroom')
         && l.photo_deal !== 'contradicts' && !(typeof l.photo_grade === 'number' && l.photo_grade <= 4);
}

const _grade = l => (typeof l.photo_grade === 'number' ? l.photo_grade : null);

// Ground floor or piano rialzato: floor 0, or "R" / "Piano rialzato" (often
// stored as floor 1).
function _fyLowFloor(l) {
  return (l.floor_n != null && l.floor_n <= 0)
      || /rialzat/i.test(`${l.floor_label || ''} ${l.floor || ''}`) || String(l.floor || '').trim().toUpperCase() === 'R';
}

// The user's "leave out" choices. A floor not stated passes, flagged.
function _fyPrefsOk(l, p, why) {
  if (p.noStudio && ((l.rooms || 0) < 2 || l.text_says_studio)) return false;
  if (p.floor) {
    if (l.floor_n == null && !l.floor) { why?.push({ bad: true, w: 1, t: t_('fy.w_floor_unknown') }); return true; }
    if (_fyLowFloor(l)) return false;
    if (p.floor === '2' && (l.floor_n || 0) < 2) return false;
  }
  return true;
}

// How much to trust the ad: fresh, seen lately, photos read.
function _fyTrust(l, why) {
  const d = typeof daysOnline === 'function' ? daysOnline(l) : l.days_on_market;
  let t = d == null ? 0.7 : d <= 30 ? 1 : d <= 60 ? 0.85 : d <= 120 ? 0.55 : 0.3;
  if (d != null && d > 60) why.push({ bad: true, w: 2, t: t_('fy.w_online', { n: fmt(Math.floor(d / 30)) }) });
  const seen = l.last_seen_date ? (Date.now() - new Date(l.last_seen_date)) / 86400000 : null;
  if (seen != null && seen > 4) t *= 0.7;
  if (_grade(l) == null) t *= 0.85;
  if (l.first_seen_date && (Date.now() - new Date(l.first_seen_date)) / 86400000 <= 3) why.push({ w: 1, t: t_('fy.w_new') });
  return t;
}

function _fyCommonLive(l, why) {
  const g = _grade(l);
  const parts = [];
  if (g != null) {
    parts.push(_lerp(g, 4, 9));
    if (g >= 7.5) why.push({ w: 3, t: t_('fy.w_photos', { g: String(g).replace('.', ',') }) });
  }
  const walk = l.metro_walk_min;
  if (walk != null) {
    parts.push(walk <= 6 ? 1 : walk <= 12 ? 0.7 : walk <= 18 ? 0.4 : 0.2);
    if (walk <= 6) why.push({ w: 2, t: walk <= 1 ? t_('fy.w_metro_door') : t_('fy.w_metro', { n: walk }) });
  }
  if ((l.floor_n || 0) >= 3 && l.elevator === false) {
    parts.push(0.3); why.push({ bad: true, w: 2, t: t_('fy.w_nolift', { n: l.floor_n }) });
  }
  if (l.has_balcony) { parts.push(0.9); why.push({ w: 1, t: t_('fy.w_balcony') }); }
  // What Sonnet saw (details.js PHOTO_TAGS): pluses and minuses an agent
  // would point out — light, room sizes, old windows, a windowless bathroom…
  const tags = (l.photo_tags || []).filter(k => typeof PHOTO_TAGS !== 'undefined' && k in PHOTO_TAGS);
  if (tags.length) {
    const net = tags.reduce((s, k) => s + PHOTO_TAGS[k], 0);
    parts.push(_clamp01(0.55 + 0.12 * net));
    for (const k of tags) why.push({ bad: PHOTO_TAGS[k] < 0, w: _FY_TAG_W[k] || 1, t: t_('ptag.' + k) });
  }
  return parts;
}
// The tags that weigh most in a choice to live there.
const _FY_TAG_W = { bright: 3, dark: 3, damp: 3, windowless_bathroom: 2, big_living: 2, quiet_courtyard: 2,
                    busy_street: 2, new_bathroom: 2, recently_renovated: 2, walk_through_rooms: 2, cramped: 2,
                    plan_mismatch: 3, cracks: 3, old_windows: 2, small_bathroom: 2 };

const _avg = a => a.length ? a.reduce((x, y) => x + y, 0) / a.length : 0.6;

function _fyLiveSolo(l, why) {
  const parts = _fyCommonLive(l, why);
  const sqm = l.sqm || 0;
  if (sqm) {
    parts.push(sqm < 35 ? 0.2 : sqm < 42 ? 0.6 : sqm <= 70 ? 1 : 0.85);
    if (sqm >= 50) why.push({ w: 2, t: t_('fy.w_sqm', { n: sqm }) });
  }
  if ((l.bedrooms || 0) >= 1) parts.push(1);
  return _avg(parts);
}

function _fyLiveDuo(l, why) {
  const parts = _fyCommonLive(l, why);
  const each = (l.sqm || 0) / 2;
  if (each) {
    parts.push(each < 25 ? 0.2 : each < 32 ? 0.6 : 1);
    if (each >= 35) why.push({ w: 2, t: t_('fy.w_each_sqm', { n: Math.round(each) }) });
  }
  const f = l.photo_features || {};
  const baths = l.bathrooms ?? f.bathrooms;
  if ((baths || 0) >= 2) { parts.push(1); why.push({ w: 3, t: t_('fy.w_baths') }); }
  else parts.push(0.5);
  // A living room besides the two bedrooms — the photos tell when the site doesn't.
  if (f.separate_living != null) parts.push(f.separate_living ? 1 : 0.3);
  else if (l.rooms && l.bedrooms) parts.push(l.rooms > l.bedrooms ? 1 : 0.4);
  return _avg(parts);
}

function _fyLiveBuy(l, p, loan, why) {
  const parts = _fyCommonLive(l, why);
  const condo = Number(l.spese_condominiali || l.condominium_fees) || 0;
  const own = mortgagePayment(l.price, loan) + condo;
  if (l.estimated_rent_mo) {
    const gap = l.estimated_rent_mo - own;           // > 0: owning costs less than renting it
    parts.push(_lerp(gap / l.estimated_rent_mo, -0.3, 0.25));
    why.push(gap >= 0 ? { w: 3, t: t_('fy.w_own_less', { v: fmt(Math.round(gap / 10) * 10) }) }
                      : { bad: true, w: 2, t: t_('fy.w_own_more', { v: fmt(Math.round(-gap / 10) * 10) }) });
  }
  const ec = (l.energy_class || '').toUpperCase()[0];
  if (ec) {
    parts.push('AB'.includes(ec) ? 1 : 'CD'.includes(ec) ? 0.75 : 'E'.includes(ec) ? 0.5 : 0.25);
    if ('FG'.includes(ec)) why.push({ bad: true, w: 2, t: t_('fy.w_energy', { c: ec }) });
  }
  if (/ristruttur|rinnov/i.test(l.condition || '') && !/da ristruttur/i.test(l.condition || '')) parts.push(1);
  if (/da ristruttur/i.test(l.condition || '')) { parts.push(0.3); why.push({ bad: true, w: 2, t: t_('fy.w_works') }); }
  // Cash left after the down payment and the purchase costs.
  const left = p.cash - l.price * (loan.down + p.costs) / 100;
  parts.push(_lerp(left, 0, 6 * own));
  if (left >= 6 * own) why.push({ w: 1, t: t_('fy.w_cash_left', { v: fmt(Math.round(left / 1000) * 1000) }) });
  return _avg(parts);
}

const t_ = (k, v) => t(k, v);

function _fyScore(l, kind, p, b, loan, centres) {
  const zone = _fyZone(l, p, centres);
  if (!zone) return null;
  const cost = kind === 'buy' ? l.price : monthlyCost(l).total;
  const budget = kind === 'buy' ? b.buy : kind === 'duo' ? b.duo : b.solo;
  if (!cost || !budget || cost > budget * FY_OVER) return null;
  const why = [];
  if (!_fyPrefsOk(l, p, why)) return null;
  const ratio = cost / budget;
  const room = ratio <= 0.85 ? 1 : ratio <= 1 ? 1 - (ratio - 0.85) / 0.15 * 0.5 : 0.2;
  const under = budget - cost;
  if (ratio > 1) why.push({ bad: true, w: 3, t: t_(kind === 'buy' ? 'fy.w_over_buy' : 'fy.w_over', { v: fmt(Math.round(-under / (kind === 'buy' ? 1000 : 10)) * (kind === 'buy' ? 1000 : 10)) }) });
  else if (kind !== 'buy' && under >= 80) why.push({ w: 2, t: t_('fy.w_under', { v: fmt(Math.round(under / 10) * 10) }) });
  if (zone.km != null) why.push({ bad: true, w: 2, t: t_('fy.w_near', { km: zone.km.toFixed(1).replace('.', ','), z: zone.zone }) });
  const live = kind === 'buy' ? _fyLiveBuy(l, p, loan, why) : kind === 'duo' ? _fyLiveDuo(l, why) : _fyLiveSolo(l, why);
  const trust = _fyTrust(l, why);
  const fit = 100 * (FY_W.deal * (l.score_total || 0) / 100 + FY_W.live * live + FY_W.budget * room
                     + FY_W.trust * trust + FY_W.zone * zone.fit);
  return { l, fit: Math.round(fit), why, near: zone.km != null };
}

// Best first, with at most FY_PER_ZONE from one neighbourhood on the first page.
function _fyOrder(scored) {
  scored.sort((a, b) => b.fit - a.fit);
  const first = [], rest = [], per = {};
  for (const s of scored) {
    const nb = s.l.neighbourhood || '';
    if (first.length < FY_PAGE && (per[nb] || 0) < FY_PER_ZONE) { first.push(s); per[nb] = (per[nb] || 0) + 1; }
    else rest.push(s);
  }
  return [...first, ...rest];
}

function fyLists(p = fyProfile()) {
  const b = fyBudgets(p), loan = _fyLoan(), centres = _fyZoneCentres(p.areas);
  // Inside the chosen zones, and — kept apart, never mixed in — up to
  // FY_NEAR_KM outside them (the user: "you don't respect my request if you
  // put in listings from other zones").
  const run = (rows, kind, ok) => {
    const all = rows.filter(l => ok(l) && _fyLiveable(l))
      .map(l => _fyScore(l, kind, p, b, loan, centres)).filter(Boolean);
    return [_fyOrder(all.filter(s => !s.near)), _fyOrder(all.filter(s => s.near))];
  };
  const rents = (ALL_RENT || []).filter(l => l.rent_mo);
  const [solo, soloNear] = run(rents, 'solo', l => (l.rooms || 0) >= 2 && (l.bedrooms == null || l.bedrooms >= 1));
  // Two bedrooms the site or the photos confirm — not "3 rooms, so 2
  // bedrooms" (8 % of trilocali have one: id_37182070).
  const [duo, duoNear] = run(rents, 'duo', l => (l.bedrooms ?? 0) >= 2);
  const [buy, buyNear] = run((ALL_SALE || []).filter(l => l.price), 'buy', () => true);
  return { b, solo, soloNear, duo, duoNear, buy, buyNear };
}

// "Why this one": the best three reasons and the main caveat.
function _fyWhyHtml(s) {
  const good = s.why.filter(w => !w.bad).sort((a, b) => b.w - a.w).slice(0, 3);
  const bad = s.why.filter(w => w.bad).sort((a, b) => b.w - a.w).slice(0, 1);
  return `<div class="fy-why"><span class="fy-fit" title="${escapeHtml(t('fy.fit_tip'))}">${escapeHtml(t('fy.fit', { n: s.fit }))}</span>${
    good.map(w => `<span class="fy-chip">${escapeHtml(w.t)}</span>`).join('')}${
    bad.map(w => `<span class="fy-chip warn">${escapeHtml(w.t)}</span>`).join('')}</div>`;
}

// ── Render ────────────────────────────────────────────────────────────────────
function renderForYou() {
  const view = document.getElementById('foryouView');
  if (!view) return;
  _fyInitForm();
  const p = fyProfile();
  view.querySelectorAll('[data-fy-mode]').forEach(btn => {
    const on = btn.dataset.fyMode === p.mode;
    btn.classList.toggle('active', on);
    btn.setAttribute('aria-pressed', String(on));
  });
  document.getElementById('fyRent').hidden = p.mode !== 'rent';
  document.getElementById('fyBuy').hidden = p.mode !== 'buy';

  const set = (id, html) => { const el = document.getElementById(id); if (el) el.innerHTML = html; };
  if (!p.income) {
    ['fyOutSolo', 'fyOutDuo', 'fyOutBuy'].forEach(id => set(id, '–'));
    ['fyOutDuoSub', 'fyOutBuySub'].forEach(id => set(id, ''));
    ['fyGridSolo', 'fyGridDuo', 'fyGridBuy'].forEach(id =>
      set(id, `<div class="empty-state">${escapeHtml(t('fy.enter_pay'))}</div>`));
    return;
  }
  const { b, solo, soloNear, duo, duoNear, buy, buyNear } = fyLists(p);
  const mo = `<span class="fy-unit">${escapeHtml(t('fy.per_month'))}</span>`;
  set('fyOutSolo', '€' + fmt(Math.round(b.solo)) + mo);
  set('fyOutDuo', '€' + fmt(Math.round(b.duo)) + mo);
  set('fyOutDuoSub', escapeHtml(t('fy.each', { v: fmt(Math.round(b.solo)) })));
  set('fyOutBuy', b.buy ? '€' + fmt(b.buy) : '–');
  set('fyOutBuySub', b.buy ? escapeHtml(t('fy.buy_sub', { v: fmt(Math.round(b.payment / 10) * 10),
                                                          limit: t(b.limit === 'cash' ? 'fy.limit_cash' : 'fy.limit_pay') })) : '');
  set('fyHeadSolo', escapeHtml(t('fy.head_solo', { v: fmt(Math.round(b.solo)), n: fmt(solo.length) })));
  set('fyHeadDuo', escapeHtml(t('fy.head_duo', { v: fmt(Math.round(b.duo)), n: fmt(duo.length) })));
  set('fyHeadBuy', escapeHtml(t('fy.head_buy', { v: fmt(b.buy), n: fmt(buy.length) })));

  _fyRentGrid('fyGridSolo', solo, 'solo', null);
  _fyNear('fyGridSolo', soloNear, 'soloNear', (id, l, k) => _fyRentGrid(id, l, k, null));
  _fyRentGrid('fyGridDuo', duo, 'duo', 2);
  _fyNear('fyGridDuo', duoNear, 'duoNear', (id, l, k) => _fyRentGrid(id, l, k, 2));
  _fyBuyGrid('fyGridBuy', buy, 'buy');
  _fyNear('fyGridBuy', buyNear, 'buyNear', _fyBuyGrid);
}

// "Near your zones": its own block under a list, only when there are any.
function _fyNear(gridId, list, key, render) {
  const grid = document.getElementById(gridId);
  if (!grid) return;
  let box = document.getElementById(gridId + 'Near');
  if (!box) {
    box = document.createElement('div');
    box.id = gridId + 'Near';
    box.className = 'fy-near';
    grid.after(box);
  }
  box.hidden = !list.length;
  if (!list.length) { box.innerHTML = ''; return; }
  box.innerHTML = `<h4 class="fy-near-head">${icon('map-pin')}<span>${escapeHtml(t('fy.near_head', { n: fmt(list.length) }))}</span></h4>
    <p class="fy-sub">${escapeHtml(t('fy.near_sub'))}</p><div class="grid" id="${gridId}NearGrid"></div>`;
  render(gridId + 'NearGrid', list, key);
}

function _fyMore(grid, list, key) {
  if (list.length <= _fyShown[key]) return;
  grid.insertAdjacentHTML('beforeend', `<div class="fy-more"><button type="button" class="btn btn-secondary btn-sm" data-fy-more="${key}">${
    escapeHtml(t('fy.more', { n: fmt(list.length - _fyShown[key]) }))}</button></div>`);
}

// list: [{ l, fit, why }] from fyLists.
function _fyRentGrid(id, list, key, split) {
  const grid = document.getElementById(id);
  if (!grid) return;
  grid.innerHTML = '';
  if (!list.length) { grid.innerHTML = `<div class="empty-state">${escapeHtml(t(key.endsWith('Near') ? 'fy.none' : 'fy.none_zones'))}</div>`; return; }
  for (const s of list.slice(0, _fyShown[key])) {
    const card = renderRentalCard(s.l);
    const price = card.querySelector('.card-price');
    if (split) {
      price?.insertAdjacentHTML('beforeend',
        `<span class="fy-each">${escapeHtml(t('fy.each', { v: fmt(Math.round(monthlyCost(s.l).total / split)) }))}</span>`);
    }
    price?.insertAdjacentHTML('afterend', _fyWhyHtml(s));
    grid.appendChild(card);
  }
  if (typeof _observeScoreRings === 'function') _observeScoreRings(grid, false);
  _fyMore(grid, list, key);
}

function _fyBuyGrid(id, list, key) {
  const grid = document.getElementById(id);
  if (!grid) return;
  if (!list.length) { grid.innerHTML = `<div class="empty-state">${escapeHtml(t(key.endsWith('Near') ? 'fy.none' : 'fy.none_zones'))}</div>`; return; }
  const loan = _fyLoan();
  const shown = list.slice(0, _fyShown[key]);
  grid.innerHTML = shown.map(s => saleCardHtml(s.l, loan)).join('');
  // Rent or buy: what owning costs a month next to what renting it would.
  grid.querySelectorAll('.card').forEach((card, i) => {
    const s = shown[i], l = s.l;
    const own = mortgagePayment(l.price, loan) + (Number(l.spese_condominiali || l.condominium_fees) || 0);
    const rent = l.estimated_rent_mo;
    card.querySelector('.card-price')?.insertAdjacentHTML('afterend',
      `<div class="fy-own">${escapeHtml(t('fy.own', { v: fmt(Math.round(own / 10) * 10) }))}${
        rent ? ` · ${escapeHtml(t('fy.vs_rent', { v: fmt(Math.round(rent / 10) * 10) }))}` : ''}</div>${_fyWhyHtml(s)}`);
  });
  if (typeof _observeScoreRings === 'function') _observeScoreRings(grid, false);
  _fyMore(grid, list, key);
}

// ── Form ──────────────────────────────────────────────────────────────────────
const _FY_FIELDS = { fyIncome: 'income', fyCash: 'cash', fyShare: 'share', fyReserve: 'reserve', fyCosts: 'costs' };
const _FY_LOAN_FIELDS = { fyDown: 'down', fyRate: 'rate', fyYears: 'years' };

function _fyInitPrefs(p) {
  const studio = document.getElementById('fyNoStudio'), floor = document.getElementById('fyFloor');
  if (studio) studio.checked = !!p.noStudio;
  if (floor) floor.value = p.floor || '';
  if (studio && !studio.dataset.wired) {
    studio.dataset.wired = '1';
    studio.addEventListener('change', () => _fySave({ noStudio: studio.checked }));
    floor?.addEventListener('change', () => _fySave({ floor: floor.value }));
  }
}

function _fySave(patch) {
  Storage.set('forYou', Object.assign(fyProfile(), patch));
  Object.assign(_fyShown, _FY_SHOWN0);
  renderForYou();
}

let _fyTimer = null;
function _fyInitForm() {
  const p = fyProfile(), loan = _fyLoan();
  _fyInitPrefs(p);
  for (const [id, key] of Object.entries(_FY_FIELDS)) {
    const el = document.getElementById(id);
    if (el && document.activeElement !== el) el.value = p[key] || '';     // empty, not 0, until set
  }
  for (const [id, key] of Object.entries(_FY_LOAN_FIELDS)) {
    const el = document.getElementById(id);
    if (el && document.activeElement !== el) el.value = loan[key];
  }
  // Zones: the neighbourhoods of the loaded rentals and sales, most listings first.
  const counts = {};
  for (const l of [...(ALL_RENT || []), ...(ALL_SALE || [])]) {
    if (l.neighbourhood) counts[l.neighbourhood] = (counts[l.neighbourhood] || 0) + 1;
  }
  // Filled in place: the picker reads this array each time it opens, so
  // neighbourhoods of sales loaded later show up too.
  _FY_AREA_ITEMS.splice(0, _FY_AREA_ITEMS.length,
    ...Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([k, c]) => ({ key: k, label: `${k} · ${fmt(c)}` })));
  if (!_fyMsd && document.getElementById('fyAreaWrap')) {
    _fyMsd = buildMultiSelect({
      wrapId: 'fyAreaWrap', triggerId: 'fyAreaTrigger', panelId: 'fyAreaPanel',
      searchId: 'fyAreaSearch', listId: 'fyAreaList', clearId: 'fyAreaClear', doneId: 'fyAreaDone',
      items: _FY_AREA_ITEMS, onChange: areas => _fySave({ areas }),
    });
    _fyMsd.setSelected(p.areas);
    for (const id of [...Object.keys(_FY_FIELDS), ...Object.keys(_FY_LOAN_FIELDS)]) {
      document.getElementById(id)?.addEventListener('input', () => {
        clearTimeout(_fyTimer);
        _fyTimer = setTimeout(() => {
          const num = id => parseFloat(document.getElementById(id)?.value) || 0;
          const patch = {};
          for (const [fid, key] of Object.entries(_FY_FIELDS)) patch[key] = num(fid);
          if (!patch.share) patch.share = FY_DEFAULTS.share;
          Storage.set('saleFilters', Object.assign(saleFilters(),
            { down: num('fyDown') || 20, rate: num('fyRate') || 3.5, years: num('fyYears') || 25 }));
          _fySave(patch);
        }, 300);
      });
    }
  }
}

document.addEventListener('click', e => {
  const mode = e.target.closest?.('[data-fy-mode]');
  if (mode) { _fySave({ mode: mode.dataset.fyMode }); return; }
  const more = e.target.closest?.('[data-fy-more]');
  if (more) { _fyShown[more.dataset.fyMore] += FY_PAGE; renderForYou(); }
});
