// Lume dashboard — rentals: state, helpers and the card renderer.
// Plain script (no bundler); shares globals with the other assets/*.js files.
// Declarations only: code that runs at start-up lives in init.js, which loads
// last, so any function here may call any other.

// ── Rentals — global state ────────────────────────────────────────────────────
let ALL_RENT          = [];
let rentFasciaRanges  = {};
let rentSortKey       = 'score_total';

// ── Rentals — helpers ─────────────────────────────────────────────────────────
function computeRentFasciaRanges(data) {
  const r = {};
  // Official OMI fascia letters; also keep A for any legacy keyword-matched listings
  for (const f of ['A', 'B', 'C', 'D', 'E', 'R']) {
    const ps = data.filter(l => l.omi_fascia === f && l.ask_psqm > 0).map(l => l.ask_psqm);
    if (ps.length) r[f] = { min: Math.min(...ps), max: Math.max(...ps) };
  }
  return r;
}

function _populateOmiZoneFilter(data) {
  const sel = document.getElementById('rOmiZone');
  if (!sel) return;
  const current = sel.value;
  // Collect unique zones sorted by fascia then code
  const zones = [...new Set(data.map(l => l.omi_zona).filter(Boolean))].sort((a, b) => {
    const fa = a[0], fb = b[0];
    if (fa !== fb) return fa.localeCompare(fb);
    return a.localeCompare(b);
  });
  // Rebuild options (keep first "All zones" option)
  while (sel.options.length > 1) sel.remove(1);
  zones.forEach(code => {
    const sample = data.find(l => l.omi_zona === code);
    const descr  = sample?.omi_descr ? ` — ${sample.omi_descr}` : '';
    const opt = document.createElement('option');
    opt.value = code;
    opt.textContent = `${code}${descr}`;
    sel.appendChild(opt);
  });
  // Restore selection if still valid
  if (current && zones.includes(current)) sel.value = current;
}

function getRentBarPct(l) {
  const r = rentFasciaRanges[l.omi_fascia];
  if (!r || r.max <= r.min) return 50;
  return Math.max(0, Math.min(100,
    Math.round((l.ask_psqm - r.min) / (r.max - r.min) * 100)));
}

// Energy class ranking for display/filtering
const ENERGY_RANK = { G:1, F:2, E:3, D:4, C:5, B:6, A1:7, A2:8, A3:9, A4:10, A:7 };
const ENERGY_MIN_RANK = { C: 5, B: 6, A: 7 };

// ── New listing badge helpers ──────────────────────────────────────────────────
// "New" badge disappears 3 days after the listing was first seen by our scanner.
function isNew(l) {
  if (!l.first_seen_date) return false;
  const cutoff = new Date();
  cutoff.setDate(cutoff.getDate() - 3);
  return new Date(l.first_seen_date) >= cutoff && !_listedEarlier(l);
}

// Ids grow over time on both sites, so a listing whose id is below what we
// already knew 3 days ago was published before that: first seen late (a
// zone read further, a scan gap), not new. 2026-10-17: ~3.5k of ~4.6k "new"
// listings were these. The bar is the 99th percentile, not the max: a few
// rows carry an older first_seen_date than their id (same flat on both
// sites keeps the earliest date) and one of them would lift the max to
// today's ids. Same rule for the digest: fetch_rentals._old_id_bar.
let _idBar = null, _idBarFor = null;
function _idNum(l) { const m = String(l.id ?? '').match(/\d+/); return m ? Number(m[0]) : null; }
function _listedEarlier(l) {
  // Subito says when the ad went up: trust that over the id.
  if (l.source === 'subito' && l.published_date) {
    const d = new Date(); d.setDate(d.getDate() - 3);
    return String(l.published_date).slice(0, 10) < d.toISOString().slice(0, 10);
  }
  const pool = typeof ALL_RENT !== 'undefined' ? ALL_RENT : null;
  if (!pool || !pool.length) return false;
  if (_idBarFor !== pool) {
    const d = new Date(); d.setDate(d.getDate() - 3);
    const cut = d.toISOString().slice(0, 10);
    const ids = {};
    for (const x of pool) {
      if (!x.first_seen_date || x.first_seen_date > cut) continue;
      const n = _idNum(x), src = x.source || 'immobiliare';
      if (n != null) (ids[src] = ids[src] || []).push(n);
    }
    _idBar = {};
    for (const [src, a] of Object.entries(ids)) {
      a.sort((p, q) => p - q);
      _idBar[src] = a[Math.floor(0.99 * (a.length - 1))];
    }
    _idBarFor = pool;
  }
  const n = _idNum(l), bar = _idBar[l.source || 'immobiliare'];
  return n != null && bar != null && n < bar;
}

// Whole days since the scanner first saw the listing (local calendar days).
function _daysSinceFirstSeen(l) {
  const fsd = l && l.first_seen_date;
  if (!fsd) return null;
  const d = new Date(fsd);
  if (isNaN(d.getTime())) return null;
  const today = new Date(); today.setHours(0, 0, 0, 0);
  return Math.round((today - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 86400000);
}

// href only for http(s) links — scraped data must not inject javascript: URLs.
function _safeUrl(u) {
  return /^https?:\/\//i.test(String(u || '')) ? escapeHtml(u) : '#';
}

// Metres between two lat/lon points.
function _distM(a1, o1, a2, o2) {
  const R = 6371000, r = Math.PI / 180;
  const dA = (a2 - a1) * r, dO = (o2 - o1) * r;
  const h = Math.sin(dA / 2) ** 2 + Math.cos(a1 * r) * Math.cos(a2 * r) * Math.sin(dO / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

// Rows behind a listing's benchmark (comps_ids). Locally the rows come from
// the loaded grid or /rentals/by-ids (archived comps too); online from
// Supabase, comps_ids included.
async function _compRows(l) {
  let ids = Array.isArray(l.comps_ids) ? l.comps_ids : null;
  if (!ids && !IS_LIVE && SupabaseClient.configured) {
    const r = await SupabaseClient.fetchByIds([l.id], 'id,comps_ids');
    ids = (r[0] && r[0].comps_ids) || [];
  }
  ids = (ids || []).map(String);
  const byId = new Map(ALL_RENT.map(x => [String(x.id), x]));
  let rows = ids.map(i => byId.get(i)).filter(Boolean);
  const missing = ids.filter(i => !byId.has(i));
  if (missing.length) {
    if (IS_LIVE) {
      const r = await fetch(`/rentals/by-ids?ids=${encodeURIComponent(missing.join(','))}`);
      if (r.ok) rows = rows.concat(await r.json());
    } else {
      rows = rows.concat(await SupabaseClient.fetchByIds(missing,
        'id,price,sqm,rooms,ask_psqm,condition,source,url,latitude,longitude,is_stale,neighbourhood'));
    }
  }
  return { ids, rows };
}

// The comparable flats table in Details: this flat first, then the comps
// nearest first. Ten rows, the rest behind "Show all".
async function _loadCompsList(l, box) {
  const msg = key => `<div class="comps-list-msg">${escapeHtml(t(key))}</div>`;
  box.innerHTML = msg('card.comps_loading');
  try {
    const { ids, rows } = await _compRows(l);
    if (!ids.length) { box.innerHTML = msg('card.comps_none'); return; }
    const withD = rows.map(x => ({ x, d: (l.latitude != null && x.latitude != null)
      ? _distM(l.latitude, l.longitude, x.latitude, x.longitude) : null }))
      .sort((a, b) => (a.d ?? 1e9) - (b.d ?? 1e9));
    const row = (x, d, self) => {
      const rent = x.rent_mo ?? x.price;
      const psqm = x.ask_psqm ?? (rent && x.sqm ? rent / x.sqm : null);
      const cond = x.condition ? escapeHtml(String(x.condition).split(' / ')[0].replace(' (Idealista)', '')) : '–';
      const link = self ? `<strong>${escapeHtml(t('comps.this_flat'))}</strong>`
        : `<a href="${_safeUrl(x.url)}" target="_blank" rel="noopener">${cond}${icon('external-link')}</a>`;
      return `<tr class="${self ? 'is-self' : ''}${x.is_stale ? ' is-archived' : ''}">
        <td>${d != null ? fmt(Math.round(d)) + ' m' : '–'}</td>
        <td>${psqm != null ? '€' + fmt1(psqm) : '–'}</td>
        <td>${rent != null ? '€' + fmt(rent) : '–'}</td>
        <td>${x.sqm != null ? fmt(x.sqm) : '–'}</td>
        <td>${link}${x.is_stale ? ` <span class="comps-archived">${escapeHtml(t('card.comps_archived'))}</span>` : ''}</td></tr>`;
    };
    const hiddenMsg = withD.length < ids.length
      ? `<div class="comps-list-msg">${escapeHtml(t('card.comps_hidden', { n: ids.length - withD.length }))}</div>` : '';
    const render = all => {
      const shown = all ? withD : withD.slice(0, 10);
      box.innerHTML = `<div class="table-wrap"><table class="comps-table"><thead><tr>
          <th>${escapeHtml(t('comps.col_dist'))}</th><th>€/m²</th><th>${escapeHtml(t('comps.col_rent'))}</th><th>m²</th><th>${escapeHtml(t('comps.col_cond'))}</th>
        </tr></thead><tbody>${row(l, null, true)}${shown.map(({ x, d }) => row(x, d, false)).join('')}</tbody></table></div>`
        + (!all && withD.length > 10 ? `<button type="button" class="btn btn-ghost btn-sm" data-comps-all>${escapeHtml(t('comps.show_all', { n: withD.length }))}</button>` : '')
        + hiddenMsg;
      box.querySelector('[data-comps-all]')?.addEventListener('click', () => render(true));
    };
    render(false);
  } catch (e) {
    console.warn('comps list:', e);
    box.innerHTML = msg('card.comps_none');
  }
}

// Score reasons (explain.py) as bullets — Details view.
// Sentiment governs the icon: positive / negative / neutral.
const _reasonIconMap = { positive: 'check', negative: 'alert-triangle', neutral: 'info-circle' };

function reasonsBulletsHtml(l) {
  // The price reason repeats the verdict ("x% below similar flats").
  const reasons = (Array.isArray(l && l.score_reasons) ? l.score_reasons : [])
    .filter(r => !(String(r.component || '').toLowerCase() === 'price' && l.comps_delta_pct != null));
  if (!reasons.length) return '';
  return `<ul class="callout-reasons">${reasons.map(r => {
    const sentiment = (_reasonIconMap[r.sentiment]) ? r.sentiment : 'negative';
    const component = (r.component || '').toString();
    const cKey      = component.toLowerCase();
    const compLabel = component ? t('reason.component.' + cKey) : '';
    return `
      <li class="callout-reason callout-reason-${sentiment}">
        <span class="reason-icon">${icon(_reasonIconMap[sentiment])}</span>
        ${component
          ? `<span class="reason-component reason-component-${escapeHtml(cKey)}">${escapeHtml(compLabel)}</span>`
          : ''}
        <span class="reason-text">${escapeHtml(reasonTextLocalised(r))}</span>
      </li>`;
  }).join('')}</ul>`;
}

// Translate a score-reason emitted by Python's explain.py.
//   - {i18n_key, i18n_vars} → t() lookup with placeholder substitution
//   - i18n_vars._items (used by reason.property.strengths and friends) is
//     a list of {key, vars} pairs that get individually translated and
//     joined with ", ".
//   - Old rows lacking i18n_key fall back to the English r.text verbatim.
function reasonTextLocalised(r) {
  if (!r) return '';
  if (r.i18n_key) {
    const vars = Object.assign({}, r.i18n_vars || {});
    const sub  = vars._items;
    if (Array.isArray(sub) && sub.length) {
      vars.items = sub.map(s => t(s.key, s.vars || {})).join(', ');
      vars.items = vars.items.charAt(0).toUpperCase() + vars.items.slice(1);
    }
    delete vars._items;
    return t(r.i18n_key, vars);
  }
  return r.text || '';
}

// How the benchmark was built — shown in Details.
function compsMethodTip(l) {
  const src    = l.comps_source || '';
  const n      = l.comps_n || 0;
  const radius = l.comps_radius_m ? fmt(Math.round(l.comps_radius_m)) : null;
  const key = {
    knn_stratified: 'comps.m_knn_same', knn: l.comps_adjusted ? 'comps.m_knn_adj' : 'comps.m_knn_mixed',
    zone_stratified: 'comps.m_zone_same', zone_any_condition: 'comps.m_zone_adj',
    radius_800m_same_fascia: 'comps.m_radius', fascia_fallback: 'comps.m_fascia',
    omi_zone: 'comps.m_omi', omi_only: 'comps.m_omi',
  }[src];
  return key ? t(key, { n, m: radius || '–', zone: l.omi_zona || '?' }) : '';
}

// Card banners — archived listing, or a discount too large to trust.
function staleBannerHtml(l) {
  if (!l.is_stale) return '';
  const days = l.days_since_seen;
  return `<div class="stale-banner">${icon('alert-triangle')} ${escapeHtml(days != null ? t('card.stale_days', { n: days }) : t('card.stale'))}</div>`;
}

function extremeUnderpricingBannerHtml(l) {
  const flag = l.extreme_underpricing || l._extreme_underpricing;
  if (!flag) return '';
  return `<div class="extreme-underpricing-banner">${icon('alert-triangle')} ${escapeHtml(t('card.extreme'))}</div>`;
}

// ── Rentals — card renderer ───────────────────────────────────────────────────
// Card anatomy: photo · neighbourhood + score · street · source · price with
// the discount vs nearby flats · rooms, size, floor · metro and freshness ·
// [Details] [♡] … [Open]. The why-sentence, comps bar, €/m² and the ID live
// in Details.
function favBtnInner() {
  return icon('heart');
}

function _specsParts(l) {
  const parts = [];
  if (l.rooms != null) parts.push(l.rooms === 1 ? t('card.studio') : t('card.rooms', { n: l.rooms }));
  // Bedrooms when the listing says (Immobiliare 99 %, Idealista from its page).
  if (l.bedrooms != null && l.rooms !== 1) parts.push(t(l.bedrooms === 1 ? 'card.bedroom' : 'card.bedrooms', { n: l.bedrooms }));
  if (l.sqm) parts.push(`${fmt(l.sqm)} m²`);
  const fl = _floorLabel(l);
  if (fl) parts.push(fl + (l.elevator === true ? ' · ' + t('lm.lift') : l.elevator === false && (l.floor_n ?? 0) > 1 ? ' · ' + t('lm.no_lift') : ''));
  else if (l.elevator === true) parts.push(t('lm.lift'));
  if (l.bathrooms >= 2) parts.push(t('card.baths', { n: l.bathrooms }));
  return parts;
}

// The specs line of a card, short enough for one line: the lift is an icon.
function _specsHtml(l) {
  const esc = escapeHtml, parts = [];
  if (l.rooms != null) parts.push(esc(l.rooms === 1 ? t('card.studio') : t('card.rooms', { n: l.rooms })));
  if (l.bedrooms != null && l.rooms !== 1) parts.push(esc(t(l.bedrooms === 1 ? 'card.bedroom' : 'card.bedrooms', { n: l.bedrooms })));
  if (l.sqm) parts.push(`${fmt(l.sqm)} m²`);
  const fl = _floorLabel(l);
  const lift = l.elevator === true ? `<span class="spec-lift" title="${esc(t('lm.lift'))}" aria-label="${esc(t('lm.lift'))}">${icon('elevator')}</span>`
             : l.elevator === false && (l.floor_n ?? 0) > 1 ? ' ' + esc(t('lm.no_lift')) : '';
  if (fl) parts.push(esc(fl) + lift);
  else if (l.elevator === true) parts.push(lift);
  if (l.bathrooms >= 2) parts.push(esc(t('card.baths', { n: l.bathrooms })));
  return parts.map(p => `<span>${p}</span>`).join('');
}

// ── Card rows (2026-10-30 redesign) ─────────────────────────────────────────
// From a product-design review against Zillow, Rightmove, Idealista and
// Immobiliare (the user: "I don't see the floor here, or it takes me ages"):
// fixed slots in scan order — price, key facts, place, money, reasons,
// actions — words over pictograms, one verdict number (the ring), and a
// fact we show is never silently dropped: unknown reads as a muted word
// with a dashed circle (the user didn't want a "?").
function _unkHtml(word) {
  return `<span class="fact-unk" title="${escapeHtml(t('unk.tip'))}">${icon('circle-dashed')}${escapeHtml(word)}</span>`;
}

// "3° piano, ascensore" / "4° piano, senza ascensore" / "Piano terra" — the
// floor slot is always there; the lift matters from the 2nd floor up.
function _floorPhraseHtml(l) {
  const esc = escapeHtml, fn = l.floor_n;
  const label = _floorLabel(l);
  if (fn == null && !label) return _unkHtml(t('fact.floor'));
  const tone = fn != null && fn <= -2 ? 'tone-bad' : fn === -1 ? 'tone-warn' : '';
  let html = tone ? `<span class="${tone}">${esc(label)}</span>` : esc(label);
  if (fn != null && fn >= 2) {
    if (l.elevator === true) html += esc(', ' + t('lm.lift'));
    else if (l.elevator === false) html += `, <span class="${fn >= 4 ? 'tone-bad' : 'tone-warn'}">${esc(t('lm.no_lift'))}</span>`;
    else html += ', ' + _unkHtml(t('lm.lift'));
  } else if (fn === 1 && l.elevator === true) html += esc(', ' + t('lm.lift'));
  return html;
}

// rooms · m² · floor[, lift] · (2+ baths) · (bedrooms of a 3+ room flat)
function _factsHtml(l) {
  const esc = escapeHtml, parts = [];
  if (l.rooms != null) parts.push(esc(l.rooms === 1 ? t('card.studio') : t('card.rooms', { n: l.rooms })));
  if (l.sqm) parts.push(`${fmt(l.sqm)} m²`);
  parts.push(_floorPhraseHtml(l));
  if (l.bathrooms >= 2) parts.push(esc(t('card.baths', { n: l.bathrooms })));
  if ((l.rooms || 0) >= 3 && l.bedrooms != null) parts.push(esc(t(l.bedrooms === 1 ? 'card.bedroom' : 'card.bedrooms', { n: l.bedrooms })));
  return `<div class="card-facts">${parts.map(p => `<span>${p}</span>`).join('<i aria-hidden="true">·</i>')}</div>`;
}

// "18% below nearby" as words with a tone, not a pill.
function _vsNearbyHtml(dp, tip = '') {
  if (dp == null) return '';
  const tone = dp <= -5 ? 'tone-good' : dp >= 5 ? 'tone-bad' : '';
  const txt = Math.abs(dp) < 1 ? t('card.in_line')
            : t(dp < 0 ? 'card.below_nearby' : 'card.above_nearby', { pct: fmt(Math.round(Math.abs(dp))) });
  return `<span class="vs-nearby ${tone}"${tip ? ` title="${escapeHtml(tip)}"` : ''}>${escapeHtml(txt)}</span>`;
}

// Zone · street — the card's heading; a tap anywhere on the card opens Details.
function _placeHtml(zone, street, attr, suffix = '') {
  const showStreet = street && street.toLowerCase() !== String(zone).toLowerCase();
  return `<h2 class="card-place"><button type="button" class="card-title-btn" ${attr}><span class="place-zone">${escapeHtml(zone)}</span>${
    showStreet ? `<span class="place-street"> · ${escapeHtml(street)}</span>` : ''}</button>${suffix}</h2>`;
}

// Milan's metro line colours (ATM), for the badge in the footer.
const _MLINE = { M1: '#E3242B', M2: '#00A651', M3: '#F7B500', M4: '#0072BC', M5: '#97509F' };
// The lines of each station. metro_nearest_line can't be trusted (the POI
// file tags Tre Torri, Isola, Lodi, Porta Romana, Missori… all "M1").
const _MLINE_STOPS = {
  M1: 'sesto 1 maggio|sesto rondo|sesto marelli|villa san giovanni|precotto|gorla|turro|rovereto|pasteur|loreto|lima|porta venezia|palestro|san babila|duomo|cordusio|cairoli|cadorna|conciliazione|pagano|buonarroti|amendola|lotto|qt8|lampugnano|uruguay|bonola|san leonardo|molino dorino|pero|rho fiera|wagner|de angeli|gambara|bande nere|primaticcio|inganni|bisceglie',
  M2: 'assago milanofiori nord|assago milanofiori forum|abbiategrasso|famagosta|romolo|porta genova|sant agostino|sant ambrogio|cadorna|lanza|moscova|garibaldi|gioia|centrale|caiazzo|loreto|piola|lambrate|udine|cimiano|crescenzago|cascina gobba|vimodrone|cologno sud|cologno centro|cologno nord|cascina burrona|cernusco sul naviglio|cascina antonietta|gorgonzola|villa fiorita|cassina de pecchi|bussero|villa pompea|gessate|cassina de pecchi',
  M3: 'san donato|rogoredo|porto di mare|corvetto|brenta|lodi|porta romana|crocetta|missori|duomo|montenapoleone|turati|repubblica|centrale|sondrio|zara|maciachini|dergano|affori centro|affori fn|comasina',
  M4: 'san cristoforo|segneri|gelsomini|frattini|tolstoj|washington bolivar|bolivar|california|coni zugna|sant ambrogio|de amicis|vetra|santa sofia|sforza policlinico|san babila|tricolore|dateo|susa|argonne|forlanini|stazione forlanini|repetti|linate',
  M5: 'bignami|ponale|bicocca|ca granda|istria|marche|zara|isola|garibaldi|monumentale|cenisio|gerusalemme|domodossola|tre torri|portello|lotto|segesta|san siro ippodromo|san siro stadio',
};
const _stopKey = s => String(s || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase()
  .replace(/^(?:milano|mm\d?|metro \w+)\s+/, '').replace(/\bp\.?ta\b/g, 'porta')
  .replace(/\bs\.\s*(?=[aeiou])/g, 'sant ').replace(/\bs\.\s*/g, 'san ')
  .replace(/[°º]/g, ' ').replace(/[^a-z0-9]+/g, ' ').replace(/\b(m[1-5]|mm|fn|fs|tibb|t i b b)\b$/g, '').trim();
const _STOP_LINES = (() => {
  const m = new Map();
  for (const [line, list] of Object.entries(_MLINE_STOPS))
    for (const st of list.split('|')) m.set(st, [...(m.get(st) || []), line]);
  return m;
})();
function _stationLines(name) {
  const k = _stopKey(name);
  if (_STOP_LINES.has(k)) return _STOP_LINES.get(k);
  // "Garibaldi FS" → garibaldi fs; "Lodi T.I.B.B." → lodi; "Sesto 1° Maggio FS"…
  for (const [st, lines] of _STOP_LINES) if (k === st || k.startsWith(st + ' ') || st.startsWith(k + ' ')) return lines;
  return [];
}

// "[M1] 8 min · Lodi TIBB": the minutes before the name so an ellipsis
// never eats them.
function _metroFootHtml(l) {
  if (l.geo_precision === 'zone') return '';
  let walk = l.metro_walk_min != null ? l.metro_walk_min : null;
  if (walk == null && l.metro_nearest_dist_m != null) walk = Math.max(1, Math.round(l.metro_nearest_dist_m / 70));
  if (walk == null || !l.metro_nearest_name) return '';
  const badge = _stationLines(l.metro_nearest_name).slice(0, 2)
    .map(line => `<b class="mline" style="--ml:${_MLINE[line]}"${line === 'M3' ? ' data-dark' : ''}>${line}</b>`).join('');
  const what = l.geo_precision === 'metro' ? t('card.near_stop', { name: l.metro_nearest_name })
             : `${t('card.min', { n: walk })} · ${l.metro_nearest_name}`;
  return `<span class="foot-metro">${badge}<span>${escapeHtml(what)}</span></span>`;
}

// Bottom row: where and how fresh on the left, ♡ and Open on the right.
function _cardFootHtml({ metaHtml = '', favHtml = '', openHtml = '' }) {
  return `<div class="card-foot"><div class="foot-meta">${metaHtml}</div><div class="foot-act">${favHtml}${openHtml}</div></div>`;
}

function _dealDeltaHtml(l) {
  if (l.price_suspect) {
    return `<span class="deal-delta tone-warn" title="${escapeHtml(t('card.suspect'))}">${icon('alert-triangle')}${escapeHtml(t('card.verify'))}</span>`;
  }
  const dp = l.comps_delta_pct;
  if (dp == null) return '';
  const tone = dp <= -5 ? 'good' : dp >= 5 ? 'bad' : 'at';
  const tip = t('card.delta_tip', { n: l.comps_n || 0, m: l.comps_radius_m ? fmt(Math.round(l.comps_radius_m)) : '–' });
  const label = Math.abs(dp) < 1 ? t('card.in_line') : t('card.vs_nearby', { pct: fmtPct(dp) });
  return `<span class="deal-delta tone-${tone}" title="${escapeHtml(tip)}">${escapeHtml(label)}</span>`;
}

// The photos' verdict on the card: only the clear ones (doubts live in Details).
// Under the price: what the total is made of (a bar sized by share, the
// parts, a warning when extras pass 15 % of the rent), "fees included", or —
// when the listing states no fees — that the number is the rent only.
// One quiet line under the price: what the all-in total is made of, or why
// it's rent only. The bar and the "+x% on the rent" tag live in Details
// (2026-10-19 card redesign: the card had grown to 13 items in 6 rows).
function _costLineHtml(c) {
  const line = (txt, cls = '', title = '') =>
    `<div class="cost-line${cls ? ' ' + cls : ''}"${title ? ` title="${escapeHtml(title)}"` : ''}>${escapeHtml(txt)}</div>`;
  if (c.included && !c.heating) return line(t('cost.included'), 'is-ok');
  if (!c.known) {
    if (c.suspect) return line(t('cost.suspect'), 'is-warn', t('cost.suspect_long', { v: fmt(c.statedCondo) }));
    return line(t(c.pageRead ? 'cost.ask' : 'cost.pending'), '', t(c.pageRead ? 'cost.ask_long' : 'cost.pending_long'));
  }
  const parts = [`${t('cost.rent')} €${fmt(c.rent)}`];
  if (c.condo) parts.push(`${t('cost.condo')} €${fmt(c.condo)}`);
  if (c.heating) parts.push(`${t('cost.heating')} €${fmt(c.heating)}`);
  const note = c.included ? t('cost.included') : (!c.condo ? t('cost.condo_unknown') : '');
  return line(parts.join(' + ') + (note ? ' · ' + note : ''), c.extraPct >= 25 ? 'is-heavy' : '',
              c.extraPct >= 15 ? t('cost.extra_pct', { pct: c.extraPct }) : '');
}

// What the photos showed (photo_eval): a chip for every verdict, "doubt"
// included — it is the most common one (694 of 1,269 on 2026-10-18), and
// without a chip a read listing looked unread.
function _photoChipHtml(l) {
  const v = { confirms: ['tone-good', 'card.photo_ok'], contradicts: ['tone-bad', 'card.photo_bad'],
              doubt: ['tone-at', 'card.photo_doubt'] }[l.photo_deal];
  if (!v) return '';
  const note = (currentLang === 'it' ? l.photo_note_it : l.photo_note_en) || '';
  const yours = l.photo_by_user ? ' · ' + t('card.photo_you') : '';
  // The 1–10 grade (rubric v2) says more than the verdict; older readings
  // have only the verdict.
  const g = l.photo_grade;
  const tone = g == null ? v[0] : g >= 7 ? 'tone-good' : g <= 4.5 ? 'tone-bad' : 'tone-at';
  const text = g != null ? t('card.photo_grade', { v: fmtNum(g, g % 1 ? 1 : 0) })
             : t(l.photo_by_user ? 'pfb.' + l.photo_deal : v[1]);
  return `<span class="deal-delta ${tone}" title="${escapeHtml(l.photo_by_user ? (l.photo_user_note || '') : note)}">${icon('photo')}${escapeHtml(text + yours)}</span>`;
}

function _metroMeta(l) {
  let walk = l.metro_walk_min != null ? l.metro_walk_min : null;
  if (walk == null && l.metro_nearest_dist_m != null) walk = Math.max(1, Math.round(l.metro_nearest_dist_m / 70));
  if (walk == null) return '';
  const name = l.metro_nearest_name ? `${l.metro_nearest_name}${l.metro_nearest_line ? ' ' + l.metro_nearest_line : ''} · ` : '';
  // Placed at the centre of the zone its text names: no walk to show.
  if (l.geo_precision === 'zone') return '';
  // Placed at the stop the listing names: the walk is unknown, not 0 min.
  if (l.geo_precision === 'metro' && l.metro_nearest_name)
    return `<span>${icon('train')}${escapeHtml(t('card.metro_named', { name: name.replace(/ · $/, '') }))}</span>`;
  // Neutral: a green "≤ 5 min" and a blue "new" read as random colours
  // without a legend (the user, 2026-10-19); the photo carries "New".
  return `<span>${icon('train')}${escapeHtml(name + t('card.walk_min', { n: walk }))}</span>`;
}

// Days since the site published it (days_on_market as of the last scan).
function daysOnline(l) {
  if (l.published_date) {
    const d = Math.floor((Date.now() - new Date(l.published_date)) / 86400000);
    if (Number.isFinite(d) && d >= 0) return d;
  }
  return l.days_on_market ?? null;
}

// Online for over two months: shown instead of "seen N days ago" — a flat
// still listed after months is often already let (2026-10-25, Immobiliare
// 125803325: −20 %, 8.5/10 photos, online since January).
const LONG_ONLINE_DAYS = 60;
function _longOnlineText(l) {
  const d = daysOnline(l);
  if (d == null || d <= LONG_ONLINE_DAYS) return null;
  return t('card.online_months', { n: fmt(Math.floor(d / 30)) });
}

// Freshness for the footer: words, the long-online caution in amber.
function _freshFootHtml(l, longDays = LONG_ONLINE_DAYS) {
  const d = daysOnline(l);
  if (d != null && d > longDays) {
    const since = l.published_date ? fmtDate(new Date(l.published_date)) : '';
    return `<span class="foot-fresh tone-warn" title="${escapeHtml(t('card.online_tip', { date: since }))}">${escapeHtml(t('card.online_months', { n: fmt(Math.floor(d / 30)) }))}</span>`;
  }
  const days = _daysSinceFirstSeen(l);
  if (days == null) return '';
  const earlier = days <= 3 && _listedEarlier(l);
  const txt = earlier ? t(days <= 0 ? 'card.found_today_old' : 'card.found_days_old', { n: fmt(days) })
            : days <= 0 ? t('card.seen_today') : t('card.seen_days', { n: fmt(days) });
  return `<span class="foot-fresh" title="${escapeHtml(fmtDate(l.first_seen_date))}">${escapeHtml(txt)}</span>`;
}

function _freshMeta(l) {
  const long = _longOnlineText(l);
  if (long) {
    const since = l.published_date ? fmtDate(new Date(l.published_date)) : '';
    return `<span class="meta-warn" title="${escapeHtml(t('card.online_tip', { date: since }))}">${icon('clock')}${escapeHtml(long)}</span>`;
  }
  const days = _daysSinceFirstSeen(l);
  if (days == null) return '';
  const earlier = days <= 3 && _listedEarlier(l);
  const txt = earlier ? t(days <= 0 ? 'card.found_today_old' : 'card.found_days_old', { n: fmt(days) })
            : days <= 0 ? t('card.seen_today') : t('card.seen_days', { n: fmt(days) });
  return `<span title="${escapeHtml(fmtDate(l.first_seen_date))}">${icon('clock')}${escapeHtml(txt)}</span>`;
}

// Score ring markup. animate=false (re-renders) paints the final state.
function scoreRingHtml(l, size = 44, opts = {}) {
  const score = opts.value ?? l.score_total ?? 0;
  const sc    = scoreClass(score);
  const r     = size / 2 - 3;
  const C     = 2 * Math.PI * r;
  const off   = C * (1 - Math.max(0, Math.min(100, score)) / 100);
  const wP  = scoreSettings.w_price    ?? 0.45;
  const wPr = scoreSettings.w_property ?? 0.25;
  const wL  = scoreSettings.w_location ?? 0.20;
  const wPn = scoreSettings.w_penalty  ?? 0.10;
  // The score's breakdown tooltip — not on a For-you fit, which isn't the score.
  const parts = opts.value != null ? '' : `
         data-sc-total="${l.score_total ?? ''}" data-sc-price="${l.score_price ?? ''}"
         data-sc-prop="${l.score_property ?? l.score_physical ?? ''}" data-sc-loc="${l.score_location ?? l.geo_score ?? ''}"
         data-sc-pen="${l.score_penalty ?? ''}" data-sc-rent="${l.score_rent ?? ''}"
         data-w-price="${wP}" data-w-prop="${wPr}" data-w-loc="${wL}" data-w-pen="${wPn}"
         data-sc-cap="${escapeHtml(l.score_cap_reason || '')}"`;
  return `<div class="score-ring-wrap sc-${sc}" role="img" aria-label="${escapeHtml((opts.caption ? opts.caption + ' ' : '') + score)}"
         data-score-target="${score}"${parts}>
      <svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" style="display:block" aria-hidden="true">
        <circle class="ring-track" cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke-width="3.5"/>
        <circle class="score-ring-fill" cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke-width="3.5"
                stroke-linecap="round" stroke-dasharray="${C.toFixed(2)}" stroke-dashoffset="${C.toFixed(2)}"
                transform="rotate(-90 ${size / 2} ${size / 2})" data-final-offset="${off.toFixed(2)}"/>
      </svg>
      <div class="score-ring-text">0</div>${opts.caption ? `<div class="score-ring-cap">${escapeHtml(opts.caption)}</div>` : ''}
    </div>`;
}

// "A good flat at a fair price" (2026-10-25): the deal labels reward a
// discount, and the cheapest listings often have the worst interiors; this
// one finds the flats priced like their neighbours that are worth it. Price
// within ±5 % of nearby flats, photos 7/10 or better (6 with two strong
// pluses — light, a terrace, a renovation…), a good location, no serious
// defect. 182 rentals on the day.
const _FAIR_STRONG = ['bright', 'open_view', 'big_living', 'high_ceilings', 'recently_renovated', 'new_bathroom',
                      'new_kitchen', 'dual_aspect', 'big_windows', 'quiet_courtyard'];
const _FAIR_BAD = ['damp', 'cracks', 'plan_mismatch', 'windowless_bathroom', 'dark', 'facing_wall', 'shabby_building'];
function isFairPick(l) {
  const d = l.comps_delta_pct, g = l.photo_grade, tags = l.photo_tags || [];
  if (d == null || d < -5 || d > 5 || typeof g !== 'number') return false;
  if (l.hidden_gem || l.good_value || l.price_suspect || l.is_room_let || l.is_stale || l.excluded
      || (l.floor_n ?? 0) < 0 || l.photo_deal === 'contradicts' || tags.some(k => _FAIR_BAD.includes(k))) return false;
  const strong = tags.filter(k => _FAIR_STRONG.includes(k)).length + (l.photo_features?.terrace ? 1 : 0);
  return (g >= 7 || (g >= 6 && strong >= 2)) && (l.score_location ?? 0) >= 70;
}

// What the monthly total is made of, then how it compares with nearby —
// one line under the price ("rent €1,300 + condo €100 · 18% below nearby").
function _rentMoneyHtml(c, l) {
  let txt, cls = '', title = '';
  if (c.included && !c.heating) { txt = t('cost.included'); cls = 'is-ok'; }
  else if (!c.known) {
    if (c.suspect) { txt = t('cost.suspect'); cls = 'tone-warn'; title = t('cost.suspect_long', { v: fmt(c.statedCondo) }); }
    else { txt = t(c.pageRead ? 'cost.ask' : 'cost.pending'); title = t(c.pageRead ? 'cost.ask_long' : 'cost.pending_long'); }
  } else {
    const parts = [`${t('cost.rent')} €${fmt(c.rent)}`];
    if (c.condo) parts.push(`${t('cost.condo')} €${fmt(c.condo)}`);
    if (c.heating) parts.push(`${t('cost.heating')} €${fmt(c.heating)}`);
    const note = c.included ? t('cost.included') : (!c.condo ? t('cost.condo_unknown') : '');
    txt = parts.join(' + ') + (note ? ' · ' + note : '');
    if (c.extraPct >= 25) cls = 'tone-warn';
    if (c.extraPct >= 15) title = t('cost.extra_pct', { pct: c.extraPct });
  }
  const vs = l.price_suspect
    ? `<span class="vs-nearby tone-warn" title="${escapeHtml(t('card.suspect'))}">${escapeHtml(t('card.verify'))}</span>`
    : _vsNearbyHtml(l.comps_delta_pct, t('card.delta_tip', { n: l.comps_n || 0, m: l.comps_radius_m ? fmt(Math.round(l.comps_radius_m)) : '–' }));
  return `<div class="card-money"><span class="${cls}"${title ? ` title="${escapeHtml(title)}"` : ''}>${escapeHtml(txt)}</span>${vs ? '<i aria-hidden="true">·</i>' + vs : ''}</div>`;
}

// A rent lowered since we first saw it: a badge on the photo.
function _priceCutBadge(l) {
  const was = l.previous_price, now = l.rent_mo ?? l.price;
  if (!was || !now || was <= now) return '';
  const pct = Math.round((was - now) / was * 100);
  return pct >= 1 ? `<span class="card-photo-new-badge badge-cut" title="${escapeHtml(priceHistoryText(l) || '')}">${escapeHtml(t('badge.price_cut', { pct }))}</span>` : '';
}

// opts.fit: For you shows its fit in the ring instead of the score.
function renderRentalCard(l, opts = {}) {
  const id   = escapeHtml(String(l.id));
  const site = siteName(l.source);
  const street = l.address ? formatAddress(String(l.address).replace(/,?\s*milano\s*$/i, '')) : '';
  // A listing without a neighbourhood (Subito, placed from its text or not
  // at all) is titled by its street, else by its own title.
  const nb   = formatAddress(l.neighbourhood) || street || (l.title || '').trim() || '–';

  const gemChip = l.hidden_gem
    ? `<span class="gem-photo-chip chip-hidden">${icon('diamond')}<span>${escapeHtml(t('gem.hidden'))}</span></span>`
    : l.good_value
    ? `<span class="gem-photo-chip chip-good">${icon('coin')}<span>${escapeHtml(t('gem.good'))}</span></span>`
    : isFairPick(l)
    ? `<span class="gem-photo-chip chip-fair">${icon('sparkles')}<span>${escapeHtml(t('gem.fair'))}</span></span>`
    : '';
  // One badge top-right: a price cut says more than "New".
  const topRight = _priceCutBadge(l) || (isNew(l) ? `<span class="card-photo-new-badge">${escapeHtml(t('badge.new'))}</span>` : '');
  const photoChip = _photoChipHtml(l);
  // A swipe or the arrows page through the photos on the card itself
  // (details.js, cardPhotoFill / cardPhotoStep); a tap on the photo opens
  // Details. A div, not a button: iOS won't scroll a strip inside a button.
  const nav = l.thumbnail && (l.photo_count || 0) !== 1
    ? `<button type="button" class="card-photo-nav prev" data-photo-step="-1" aria-label="${escapeHtml(t('pv.prev'))}" tabindex="-1">${icon('chevron-left')}</button>
       <button type="button" class="card-photo-nav next" data-photo-step="1" aria-label="${escapeHtml(t('pv.next'))}" tabindex="-1">${icon('chevron-right')}</button>
       <span class="card-photo-count" hidden></span>`
    : '';
  const photo = (l.thumbnail || gemChip || topRight)
    ? `<div class="card-photo" data-photo-id="${id}"><div class="card-photo-wrap" data-open-details="${id}" aria-label="${escapeHtml(t('card.details') + ': ' + nb)}">${l.thumbnail ? `<div class="card-photo-track"><img class="card-thumb" src="${_safeUrl(l.thumbnail)}" alt="" loading="lazy" draggable="false"></div>` : ''}${gemChip}${topRight}${l.thumbnail && photoChip ? `<span class="card-photo-grade">${photoChip}</span>` : ''}</div>${nav}</div>`
    : '';
  // The site is the Open button's label; "+1" when the flat is on another site too.
  const others = alsoOnSites(l) ? alsoOnSites(l).split(', ').length : 0;
  // "Immobiliare", "Subito" — but "Casa" alone reads as a word: kept whole.
  const openLabel = (site === 'Casa.it' ? site : site.replace('.it', '')) + (others ? ` +${others}` : '');

  const cost = monthlyCost(l);
  const fav = isRentFav(l.id);
  const priv = isPrivate(l) ? t('card.private') : privateCopySite(l) ? t('card.private_on', { site: privateCopySite(l) }) : '';

  const card = document.createElement('article');
  card.className = 'card' + (l.hidden_gem ? ' is-gem' : (l.good_value ? ' is-goodvalue' : ''));
  card.setAttribute('data-listing-id', String(l.id));
  card.dataset.cardOpen = 'rent';
  card.innerHTML = `
    ${photo}
    <div class="card-body">
      <div class="card-top">
        <div class="card-top-main">
          <div class="card-price">
            <span class="price-main">${l.rent_mo != null ? '€' + fmt(cost.total) : '–'}</span>
            <span class="price-unit">${escapeHtml(t(cost.known ? 'cost.all_in' : 'cost.rent_only'))}</span>
            ${l.thumbnail ? '' : photoChip}
          </div>
          ${_rentMoneyHtml(cost, l)}
        </div>
        ${opts.fit != null ? scoreRingHtml(l, 40, { value: opts.fit, caption: t('ring.fit') }) : scoreRingHtml(l, 40, { caption: t('ring.score') })}
      </div>
      ${_factsHtml(l)}
      ${_placeHtml(nb, street, `data-open-details="${id}"`, priv ? `<span class="place-private">${escapeHtml(priv)}</span>` : '')}
    </div>
    ${_cardFootHtml({
      metaHtml: _metroFootHtml(l) + _freshFootHtml(l),
      favHtml: `<button type="button" class="btn btn-secondary btn-icon btn-fav${fav ? ' is-fav' : ''}" data-rent-id="${id}"
              aria-pressed="${fav}" aria-label="${escapeHtml(t(fav ? 'card.unsave' : 'card.save'))}" title="${escapeHtml(t(fav ? 'card.unsave' : 'card.save'))}">${favBtnInner()}</button>`,
      openHtml: `<a class="btn btn-secondary btn-open" href="${_safeUrl(l.url)}" target="_blank" rel="noopener"
         data-listing-id="${id}" title="${escapeHtml(t('lm.open_on', { site }) + (others ? ' · ' + t('card.also_on', { sites: alsoOnSites(l) }) : ''))}"
         onclick="_promptArchiveListing(this.dataset.listingId, 'rental', this)">${escapeHtml(openLabel)}${icon('external-link')}</a>`,
    })}
    ${extremeUnderpricingBannerHtml(l)}
    ${staleBannerHtml(l)}
  `;
  return card;
}

// Every favourite toggle (cards, Details) ends here so all copies agree.
function setFavUi(id, on) {
  document.querySelectorAll(`.btn-fav[data-rent-id="${CSS.escape(String(id))}"]`).forEach(b => {
    b.classList.toggle('is-fav', on);
    b.setAttribute('aria-pressed', String(on));
    b.title = t(on ? 'card.unsave' : 'card.save');
    b.setAttribute('aria-label', b.title);
  });
  const lm = document.querySelector(`[data-lm-fav="${CSS.escape(String(id))}"]`);
  if (lm) {
    lm.classList.toggle('on', on);
    lm.classList.toggle('is-fav', on);       // the icon-only ♡ of the 2026-10-30 sheet
    lm.setAttribute('aria-pressed', String(on));
    lm.setAttribute('aria-label', t(on ? 'card.unsave' : 'card.save'));
    const sp = lm.querySelector('span'); if (sp) sp.textContent = t(on ? 'lm.saved' : 'lm.save');
  }
}
