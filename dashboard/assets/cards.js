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
  return new Date(l.first_seen_date) >= cutoff;
}
function newBadgeHtml(l) {
  return isNew(l) ? '<span class="new-badge">New</span>' : '';
}

// Format first_seen_date as "May 2, 2026 (2 days ago)" for the data-points panel.
function firstSeenLabel(l) {
  const fsd = l && l.first_seen_date;
  if (!fsd) return null;
  const d = new Date(fsd);
  if (isNaN(d.getTime())) return fsd;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const dd = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const days = Math.round((today - dd) / 86400000);
  const fmt = d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  const rel = days === 0 ? 'today'
            : days === 1 ? 'yesterday'
            : days < 0   ? `in ${Math.abs(days)}d`
            : `${days} days ago`;
  return `${fmt} (${rel})`;
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

// The comparable flats behind a listing's benchmark (comps_ids), nearest
// first. Locally the rows come from the loaded grid or /rentals/by-ids
// (archived comps too); online from Supabase, comps_ids included.
async function _loadCompsList(l, box) {
  const msg = key => `<div class="comps-list-msg">${escapeHtml(t(key))}</div>`;
  box.innerHTML = msg('card.comps_loading');
  try {
    let ids = Array.isArray(l.comps_ids) ? l.comps_ids : null;
    if (!ids && !IS_LIVE && SupabaseClient.configured) {
      const r = await SupabaseClient.fetchByIds([l.id], 'id,comps_ids');
      ids = (r[0] && r[0].comps_ids) || [];
    }
    ids = (ids || []).map(String);
    if (!ids.length) { box.innerHTML = msg('card.comps_none'); return; }
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
    const withD = rows.map(x => ({ x, d: (l.latitude != null && x.latitude != null)
      ? _distM(l.latitude, l.longitude, x.latitude, x.longitude) : null }))
      .sort((a, b) => (a.d ?? 1e9) - (b.d ?? 1e9));
    box.innerHTML = `<table class="comps-table"><thead><tr>
        <th>m</th><th>€/m²</th><th>€/mo</th><th>m²</th><th></th></tr></thead><tbody>
      ${withD.map(({ x, d }) => {
        const rent = x.rent_mo ?? x.price;
        const psqm = x.ask_psqm ?? (rent && x.sqm ? rent / x.sqm : null);
        const cond = x.condition ? escapeHtml(String(x.condition).split(' / ')[0]) : '';
        return `<tr class="${x.is_stale ? 'is-archived' : ''}">
          <td>${d != null ? Math.round(d) : '–'}</td>
          <td>${psqm != null ? Number(psqm).toFixed(1) : '–'}</td>
          <td>${rent != null ? fmt(rent) : '–'}</td>
          <td>${x.sqm ?? '–'}</td>
          <td><a href="${_safeUrl(x.url)}" target="_blank" rel="noopener">${cond || '↗'}</a>${x.is_stale
            ? ` <span class="comps-archived">${escapeHtml(t('card.comps_archived'))}</span>` : ''}</td></tr>`;
      }).join('')}</tbody></table>${withD.length < ids.length
        ? `<div class="comps-list-msg">${escapeHtml(t('card.comps_hidden', { n: ids.length - withD.length }))}</div>` : ''}`;
  } catch (e) {
    console.warn('comps list:', e);
    box.innerHTML = msg('card.comps_none');
  }
}

// Up to 3 explanatory bullets beneath the why-sentence callout.
// Each bullet carries a component tag — Price / Property / Location / Penalty —
// matching the rows in the score-breakdown panel. Sentiment governs the icon:
// ✓ positive (strength), ⚠ negative (weakness), • neutral (informational).
// Reads `listing.score_reasons` (populated by explain.py at score time).
const _reasonIconMap = { positive: '✓', negative: '⚠', neutral: '•' };

function reasonsBulletsHtml(l) {
  // The price reason repeats the sentence above it ("x% below similar
  // flats") — and on a "verify first" discount its ✓ would contradict it.
  const reasons = (Array.isArray(l && l.score_reasons) ? l.score_reasons : [])
    .filter(r => !(String(r.component || '').toLowerCase() === 'price' && l.comps_delta_pct != null));
  if (!reasons.length) return '';
  return `<ul class="callout-reasons">${reasons.map(r => {
    const sentiment = (_reasonIconMap[r.sentiment]) ? r.sentiment : 'negative';
    const icon      = _reasonIconMap[sentiment];
    const component = (r.component || '').toString();
    const cKey      = component.toLowerCase();
    // Translate the component badge ("PRICE" → "PREZZO" in Italian) and
    // the body text. reasonText() prefers r.i18n_key/i18n_vars when present,
    // falling back to the English r.text for older synced rows.
    const compKey   = 'reason.component.' + cKey;
    const compLabel = component ? t(compKey) : '';
    return `
      <li class="callout-reason callout-reason-${sentiment}">
        <span class="reason-icon">${icon}</span>
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
//     joined with ", " — gives Italian users localised sub-fragments
//     like "recentemente ristrutturato, piano 5 con ascensore".
//   - Old rows lacking i18n_key fall back to the English r.text verbatim.
function reasonTextLocalised(r) {
  if (!r) return '';
  if (r.i18n_key) {
    const vars = Object.assign({}, r.i18n_vars || {});
    const sub  = vars._items;
    if (Array.isArray(sub) && sub.length) {
      vars.items = sub.map(s => t(s.key, s.vars || {})).join(', ');
      // Capitalise the first character of the joined fragments (matches
      // the Python side's `text[0].upper() + text[1:]` formatting).
      vars.items = vars.items.charAt(0).toUpperCase() + vars.items.slice(1);
    }
    delete vars._items;
    return t(r.i18n_key, vars);
  }
  return r.text || '';
}

// Combined specs pill: 🛏 N · 🛁 N · Xm²
// Falls back gracefully when fields are missing.
function specsPillHtml(l) {
  const parts = [];
  if (l.bedrooms !== null && l.bedrooms !== undefined) {
    const label = l.bedrooms === 0 ? 'studio' : `${l.bedrooms}`;
    parts.push(`🛏 ${label}`);
  }
  if (l.bathrooms) parts.push(`🛁 ${l.bathrooms}`);
  if (l.sqm)        parts.push(`${l.sqm}m²`);
  if (parts.length === 0) return '';
  return `<span class="specs-pill">${parts.join(' · ')}</span>`;
}

// ── Card layout helpers (Fixes 1, 2, 4) ───────────────────────────────────────

// 3 horizontally-scrollable stat pills (metro · floor · comps delta)
function statPillsHtml(l, isRental) {
  const pills = [];

  // Pill 1 — Metro: 🚇 Cadorna · 6 min M1
  // Always show walking time, not distance. When metro_walk_min is missing
  // (≈18 % of sale listings — geo enrichment didn't compute it), derive it
  // from metro_nearest_dist_m using a 70 m/min walking pace, which matches
  // the rest of our routed-time data within ±1 min.
  let walkMin = (l.metro_walk_min != null) ? l.metro_walk_min : null;
  if (walkMin == null && l.metro_nearest_dist_m != null) {
    walkMin = Math.max(1, Math.round(l.metro_nearest_dist_m / 70));
  }
  if (walkMin != null) {
    const color = walkMin <= 5  ? 'var(--green-deep)'
                : walkMin <= 12 ? 'var(--amber)'
                : 'var(--text-secondary)';
    const name = l.metro_nearest_name ? `${escapeHtml(l.metro_nearest_name)} · ` : '';
    const line = l.metro_nearest_line ? ` ${escapeHtml(l.metro_nearest_line)}` : '';
    const distHint = (l.metro_walk_min == null && l.metro_nearest_dist_m != null)
      ? ` (~${Math.round(l.metro_nearest_dist_m)}m)` : '';
    const tip  = l.metro_nearest_name
      ? `${l.metro_nearest_name}${l.metro_nearest_line ? ' (' + l.metro_nearest_line + ')' : ''} · ${walkMin} min walk${distHint}`
      : `${walkMin} min walk to nearest metro${distHint}`;
    pills.push(`<span class="stat-pill" style="color:${color}" title="${escapeHtml(tip)}">🚇 ${name}${walkMin} min${line}</span>`);
  } else {
    pills.push(`<span class="stat-pill" style="color:var(--text-muted)">🚇 —</span>`);
  }

  // Pill 2 — Floor + lift, falling back to rooms / lift when floor is unknown.
  // Idealista's scraper occasionally misses floor entirely (~6 % of sales);
  // showing an empty `🏢 —` reads as a bug. Fall back to whatever's useful.
  const floorLbl = (typeof _floorLabel === 'function' ? _floorLabel(l) : null)
                || (l.floor_n != null ? `${l.floor_n}° piano` : null);
  if (floorLbl) {
    const fn = l.floor_n;
    const fcol = fn != null && fn < 0   ? 'var(--coral)'
              : fn === 0                 ? 'var(--amber)'
              : 'var(--text-primary)';
    const lift = l.elevator === true ? ' 🛗' : '';
    pills.push(`<span class="stat-pill" style="color:${fcol}">🏢 ${escapeHtml(floorLbl)}${lift}</span>`);
  } else if (l.elevator === true || l.elevator === false) {
    // Floor unknown but lift state known — still useful
    const liftLabel = l.elevator === true ? '🛗 With lift' : '🚫 No lift';
    pills.push(`<span class="stat-pill" style="color:var(--text-secondary)">${liftLabel}</span>`);
  } else if (l.rooms != null) {
    // Last fallback: rooms count
    const roomsLabel = l.rooms === 1 ? '1 room' : `${l.rooms} rooms`;
    pills.push(`<span class="stat-pill" style="color:var(--text-secondary)">🛏 ${roomsLabel}</span>`);
  }
  // If floor + lift + rooms are all unknown, simply omit the middle pill
  // — the row stays clean rather than displaying a placeholder.

  // Pill 3 — how fresh the listing is (the discount is already on the
  // comps row and in the sentence below; good flats go in days).
  const days = _daysSinceFirstSeen(l);
  if (days != null) {
    const txt = days <= 0 ? t('card.seen_today') : t('card.seen_days', { n: days });
    const col = days <= 3 ? 'var(--green-deep)' : days > 30 ? 'var(--text-muted)' : 'var(--text-secondary)';
    pills.push(`<span class="stat-pill" style="color:${col}" title="${escapeHtml(firstSeenLabel(l) || '')}">🕒 ${escapeHtml(txt)}</span>`);
  }

  return `<div class="stat-pills">${pills.join('')}</div>`;
}

// Short comps context: "50 comps · B21" or "133 comps · B13 · adj."
function compsContextStr(l, isRental) {
  const n   = isRental ? (l.comps_n || 0) : (l.comps_sale_n || 0);
  const z   = l.omi_zona || '';
  const adj = isRental ? !!l.comps_adjusted : !!l.comps_sale_adjusted;
  const rad = isRental ? l.comps_radius_m : null;
  const parts = [`${n} comps`];
  if (rad)    parts.push(`≤${Math.round(rad)} m`);
  else if (z) parts.push(escapeHtml(z));
  if (adj) parts.push('adj.');
  return parts.join(' · ');
}

// Short label for the detail panel's "Comps source" row.
const _COMPS_SOURCE_SHORT = {
  knn_stratified:          'nearest flats, same condition',
  knn:                     'nearest flats, condition adjusted',
  zone_stratified:         'same zone + same condition',
  zone_any_condition:      'same zone + condition adjusted',
  radius_800m_same_fascia: '≤800 m + same fascia',
  fascia_fallback:         'same fascia (citywide)',
  omi_zone:                'OMI zone fallback',
  omi_only:                'OMI reference only',
};

// Methodology tooltip — full description on hover (kept for ⓘ icon)
function compsMethodTip(l, isRental) {
  const src    = (isRental ? l.comps_source : l.comps_sale_source) || '';
  const cgroup = isRental ? l.comps_condition_group : l.comps_sale_condition_group;
  const adj    = isRental ? !!l.comps_adjusted : !!l.comps_sale_adjusted;
  const zona   = l.omi_zona || '?';
  const fascia = l.omi_fascia || '?';
  const nComps = (isRental ? l.comps_n : l.comps_sale_n) || 0;
  const radius = isRental ? l.comps_radius_m : l.comps_sale_radius_m;
  const within = radius ? ` within ${radius} m` : '';
  if (src === 'knn_stratified')
    return `The ${nComps} nearest flats${within} in the same condition group (${cgroup}) and OMI fascia ${fascia} — €/m² scaled to this flat's size, recently seen listings weighted more.`;
  if (src === 'knn')
    return adj
      ? `The ${nComps} nearest flats${within} in OMI fascia ${fascia}, prices adjusted to ${cgroup} condition and scaled to this flat's size.`
      : `The ${nComps} nearest flats${within} in OMI fascia ${fascia}, mixed conditions (this flat's condition is unknown — no adjustment), scaled to its size.`;
  if (src === 'zone_stratified' && cgroup && cgroup !== 'unknown')
    return `Comps from same OMI zone (${zona}) and same condition group (${cgroup}). Most reliable.`;
  if (src === 'zone_any_condition')
    return adj
      ? `Comps include listings of different renovation states; prices adjusted to approximate ${cgroup || 'this'} condition before computing the median.`
      : `Comps from OMI zone ${zona} with mixed conditions (target condition unknown — no adjustment).`;
  if (src === 'radius_800m_same_fascia')
    return `Zone too sparse — falling back to 800m radius within OMI fascia ${fascia}.`;
  if (src === 'fascia_fallback')
    return `No nearby comps — using full OMI fascia ${fascia} as benchmark.`;
  if (src.startsWith('comps_')) return `Comps within ${src.replace('comps_','')}m radius.`;
  if (src === 'omi_zone')       return `OMI zone benchmark (no per-listing comps).`;
  if (src === 'omi_only')       return `No live comps — priced against OMI reference.`;
  return '';
}

// Single-line comps row: [N comps · ZONE]  [━━━●━━]  [-19.9%]
function compsRowHtml(l, isRental) {
  const dp = isRental ? l.comps_delta_pct : l.comps_sale_delta_pct;
  if (dp == null) return '';
  const dpN  = Number(dp);
  const pos  = Math.max(0, Math.min(100, Math.round((dpN + 20) / 40 * 100)));
  const col  = dpN <= -5 ? 'var(--green-deep)'
            : dpN >=  5 ? 'var(--coral)'
            : 'var(--amber)';
  const sign = dpN > 0 ? '+' : '';
  const ctxText = compsContextStr(l, isRental);
  const tipText = compsMethodTip(l, isRental);
  const tip     = escapeHtml(tipText);
  // Hover shows the title; a tap/click opens the same text under the row
  // (title tooltips never appear on touch screens).
  return `<div class="comps-row">
    <span class="comps-context" title="${tip}" role="button" tabindex="0" data-comps-tip>${ctxText} <span class="comps-context-info">ⓘ</span></span>
    <div class="comps-track-wrap" style="color:${col}">
      <div class="comps-track-gradient"></div>
      <div class="comps-track-dot" style="left:${pos}%"></div>
    </div>
    <span class="comps-delta-label" style="color:${col}">${sign}${dpN.toFixed(1)}%</span>
  </div>${tipText ? `<div class="comps-tip-text" hidden>${tip}</div>` : ''}`;
}

// Listing-€/m² vs area-median pill — sits next to the listing's own
// €/m² in the price row so the reader can see at a glance how this
// listing's asking-€/m² compares to what the surrounding
// neighbourhood is fetching. Renders whenever we have the listing's
// €/m² AND a comps anchor (median OR delta) so the pill is either
// present on every card or absent for the same reason (no comps)
// — never conditional on direction (below/above market).
//
// Layout: "€18.7 vs €24/m²/mo"
//   - Left number is the listing's own €/m², colour-coded by delta:
//       ≤ -5 %  → green (below market)
//       ≥ +5 %  → red   (above market)
//       between → amber (at market)
//   - Right number is the area median (preferred from comps_median
//     when present, else derived from ask_psqm + delta — exact
//     mathematical inverse).
function areaPsqmPillHtml(l, isRental) {
  const askPsqm = Number((isRental ? (l.ask_psqm_rent ?? l.ask_psqm) : l.ask_psqm) || 0);
  if (!(askPsqm > 0)) return '';

  const unit    = isRental ? '/m²/mo' : '/m²';
  const askStr  = `€${askPsqm.toFixed(1)}`;
  const delta   = isRental ? l.comps_delta_pct : l.comps_sale_delta_pct;

  // Resolve the area median: prefer the actual comps median, fall back
  // to deriving it from ask_psqm + delta (exact mathematical inverse).
  let med = isRental ? l.comps_median : l.comps_sale_median;
  med = Number(med);
  if (!Number.isFinite(med) || med <= 0) {
    if (delta == null) {
      // No comps anchor at all — render the listing's €/m² alone so
      // the price row still shows it (matches the pre-pill UI).
      return `<span class="price-area-pill" title="Listing €/m²">
                <span class="price-area-ask tone-neutral">${askStr}${unit}</span>
              </span>`;
    }
    med = askPsqm / (1 + delta / 100);
  }

  const medStr  = `€${Math.round(med)}${unit}`;
  const tone    = (delta == null) ? 'neutral'
                : delta <= -5     ? 'good'
                : delta >=  5     ? 'bad'
                                  : 'at';
  const nComps  = isRental ? (l.comps_n || 0) : (l.comps_sale_n || 0);
  const title   = nComps
    ? `Listing €/m² vs area median from ${nComps} comparables`
    : 'Listing €/m² vs area median for similar nearby listings';
  return `<span class="price-area-pill" title="${title}">
            <span class="price-area-ask tone-${tone}">${askStr}</span>
            <span class="price-area-vs">vs</span>${medStr}
          </span>`;
}

// Stale-banner footer for cards — only renders when is_stale is set and
// the "Show expired" toggle has been flipped on (otherwise stale listings
// are filtered out entirely by applyRentFilters).
function staleBannerHtml(l) {
  if (!l.is_stale) return '';
  const days = l.days_since_seen;
  const daysStr = (days != null) ? `${days} days` : 'a while';
  return `<div class="stale-banner">⚠ Off-market — not seen by the scanner in ${daysStr}</div>`;
}

// Banner shown on cards that crossed the extreme-underpricing threshold
// (>60 % below comps). The row is NOT excluded (genuine distressed sales
// exist), but the score is capped at 55 and gem badges stripped — this
// banner surfaces the suspicion so the user verifies before acting on it.
// Reads .extreme_underpricing (DB column from migration 010). The
// legacy _extreme_underpricing key from scoring.py's apply_… helper
// also works for listings that were scored locally without going
// through Supabase.
function extremeUnderpricingBannerHtml(l) {
  const flag = l.extreme_underpricing || l._extreme_underpricing;
  if (!flag) return '';
  const dp = (l.extreme_underpricing_delta != null)
    ? l.extreme_underpricing_delta
    : (l._extreme_underpricing_delta != null
        ? l._extreme_underpricing_delta
        : (l.comps_sale_delta_pct != null
            ? l.comps_sale_delta_pct
            : l.comps_delta_pct));
  const pctStr = (dp != null && !isNaN(dp))
    ? `${Math.abs(dp).toFixed(0)}% below comps — `
    : '';
  return `<div class="extreme-underpricing-banner">⚠ ${pctStr}verify this listing — price unusually far below market</div>`;
}

// ── Rentals — card renderer ───────────────────────────────────────────────────
function renderRentalCard(l) {
  const sc       = scoreClass(l.score_total);
  const barPct   = getRentBarPct(l);
  const fasciaColour = barPct <= 33 ? 'var(--green)' : barPct <= 66 ? 'var(--yellow)' : 'var(--red)';

  const omiMid    = l.omi_loc_mid ?? l.omi_rent_mid;

  // ── Score formula legend ──────────────────────────────────────────────────
  const wP  = scoreSettings.w_price    ?? 0.45;
  const wPr = scoreSettings.w_property ?? 0.25;
  const wL  = scoreSettings.w_location ?? 0.20;
  const wPn = scoreSettings.w_penalty  ?? 0.10;
  const _yesNo = v => v === true ? 'Yes ✓' : v === false ? 'No' : null;
  const _heat  = { autonomous: 'Autonomous', centralised: 'Central', none: 'None', unknown: null };

  // ── Score breakdown tooltip ──────────────────────────────────────────────
  // (Score breakdown shown via #globalTooltip on score-badge-wrap hover — see tooltip handler above)

  // ── Expandable details panel ─────────────────────────────────────────────
  // val is escaped here — scraped text (condition, heating, floor, metro …)
  // goes straight into innerHTML.
  const _dRow = (lbl, val, cls) => {
    const dv = (val != null && val !== '') ? escapeHtml(String(val)) : '–';
    const cs = cls ? ' ' + cls : '';
    return `<div class="dg-row"><span class="dg-lbl">${lbl}</span><span class="dg-val${cs}">${dv}</span></div>`;
  };
  const detailsHtml = `
    <button class="btn-details-toggle" data-details-toggle>${escapeHtml(t('card.how_lume_scored'))} <span class="details-caret">▾</span></button>
    <div class="card-details" data-details-panel>
      <div class="details-section-title">🏠 Physical</div>
      <div class="details-grid">
        ${_dRow('Rooms', l.rooms != null ? l.rooms + ' room' + (l.rooms !== 1 ? 's' : '') : null, l.rooms == null ? 'muted' : '')}
        ${_dRow('Bedrooms', l.bedrooms != null ? (l.bedrooms === 0 ? 'Studio' : l.bedrooms + ' bed' + (l.bedrooms !== 1 ? 's' : '')) : null, l.bedrooms == null ? 'muted' : '')}
        ${(() => { const lbl = _floorLabel(l); const fn = l.floor_n; const cls = fn == null ? 'muted' : fn < 0 ? 'bad' : fn >= 3 ? 'good' : ''; return _dRow('Floor', lbl, cls); })()}
        ${_dRow('Elevator', l.elevator === true ? 'Yes ✓' : l.elevator === false ? 'No' : null, l.elevator === true ? 'good' : l.elevator == null ? 'muted' : '')}
        ${_dRow('External', l.is_external === true ? 'Yes ✓' : l.is_external === false ? 'No' : null, l.is_external === true ? 'good' : l.is_external == null ? 'muted' : '')}
        ${_dRow('Energy', l.energy_class, l.energy_class ? (ENERGY_RANK[l.energy_class] >= 5 ? 'good' : '') : 'muted')}
        ${_dRow('Year built', l.year_built, l.year_built ? '' : 'muted')}
        ${_dRow('Bathrooms', l.bathrooms != null ? l.bathrooms : null, l.bathrooms >= 2 ? 'good' : l.bathrooms == null ? 'muted' : '')}
        ${_dRow('Balcony', l.has_balcony === true ? 'Yes ✓' : l.has_balcony === false ? 'No' : null, l.has_balcony === true ? 'good' : l.has_balcony == null ? 'muted' : '')}
        ${_dRow('Parking', l.has_parking === true ? 'Yes ✓' : l.has_parking === false ? 'No' : null, l.has_parking === true ? 'good' : l.has_parking == null ? 'muted' : '')}
        ${_dRow('Condition', l.condition, l.condition ? '' : 'muted')}
        ${_dRow('Heating', l.heating_type ? (_heat[l.heating_type] ?? l.heating_type) : null, l.heating_type ? '' : 'muted')}
        ${_dRow('Furnished', _yesNo(l.furnished), l.furnished == null ? 'muted' : '')}
        ${l.air_conditioning ? _dRow('Air conditioning', 'Yes ✓', 'good') : ''}
        ${l.contract_type ? _dRow('Contract', l.contract_type) : ''}
        ${l.availability ? _dRow('Availability', l.availability) : ''}
        ${l.spese_condominiali ? _dRow('Condo fees', '€' + fmt(l.spese_condominiali) + '/mo') : ''}
        ${_dRow('Photos', l.photo_count != null ? l.photo_count + ' photos' : null, l.photo_count >= 8 ? 'good' : l.photo_count > 0 ? '' : 'muted')}
        ${_dRow('Days listed', l.days_on_market != null ? l.days_on_market + 'd' : null, l.days_on_market != null && l.days_on_market > 60 ? 'bad' : l.days_on_market == null ? 'muted' : '')}
        ${_dRow('First seen', firstSeenLabel(l), l.first_seen_date ? '' : 'muted')}
      </div>
      ${(l.twin_filled || []).length ? `<div class="details-note">${escapeHtml(t('card.twin', {
          site: l.source === 'idealista' ? 'Immobiliare.it' : 'Idealista' }))}</div>` : ''}
      <div class="details-section-title" style="margin-top:6px">🌍 Geospatial</div>
      <div class="details-grid">
        ${_dRow('Metro station', l.metro_nearest_name, l.metro_nearest_name ? '' : 'muted')}
        ${_dRow('Metro line', l.metro_nearest_line, l.metro_nearest_line ? '' : 'muted')}
        ${_dRow('Metro dist', l.metro_nearest_dist_m != null ? l.metro_nearest_dist_m + 'm' : null, l.metro_nearest_dist_m != null ? (l.metro_nearest_dist_m <= 500 ? 'good' : l.metro_nearest_dist_m > 1500 ? 'bad' : '') : 'muted')}
        ${_dRow('Walk time', l.metro_walk_min != null ? l.metro_walk_min + ' min' : null, l.metro_walk_min == null ? 'muted' : '')}
        ${_dRow('Tram stop', l.tram_nearest_dist_m != null ? l.tram_nearest_dist_m + 'm' : null, l.tram_nearest_dist_m != null ? (l.tram_nearest_dist_m <= 300 ? 'good' : '') : 'muted')}
        ${_dRow('Supermarket', l.supermarket_nearest_dist_m != null ? l.supermarket_nearest_dist_m + 'm' : null, l.supermarket_nearest_dist_m != null ? (l.supermarket_nearest_dist_m <= 400 ? 'good' : '') : 'muted')}
        ${_dRow('Park', l.park_nearest_dist_m != null ? l.park_nearest_dist_m + 'm' : null, l.park_nearest_dist_m != null ? (l.park_nearest_dist_m <= 500 ? 'good' : '') : 'muted')}
        ${_dRow('University', l.university_nearest_dist_m != null ? l.university_nearest_dist_m + 'm' : null, l.university_nearest_dist_m == null ? 'muted' : '')}
      </div>
      <div class="details-section-title" style="margin-top:6px">📊 Score breakdown</div>
      <div class="details-grid">
        ${_dRow(`Price vs comps ×${wP}`, l.score_price != null ? l.score_price + ' → ' + Math.round(l.score_price * wP) : '50 (neutral)', l.score_price == null ? 'muted' : '')}
        ${_dRow(`Property ×${wPr}`, l.score_property != null ? l.score_property + ' → ' + Math.round(l.score_property * wPr) : null, l.score_property == null ? 'muted' : '')}
        ${_dRow(`Location ×${wL}`, l.score_location != null ? l.score_location + ' → ' + Math.round(l.score_location * wL) : (l.geo_score != null ? l.geo_score + ' → ' + Math.round(l.geo_score * wL) : '50 (neutral)'), (l.score_location == null && l.geo_score == null) ? 'muted' : '')}
        ${_dRow(`Penalty ×${wPn}`, l.score_penalty != null ? l.score_penalty + ' → ' + Math.round(l.score_penalty * wPn) : '100 (no penalty)', l.score_penalty == null ? 'muted' : '')}
        ${_dRow('Comps median', l.comps_median != null ? '€' + l.comps_median + '/m²/mo (' + (l.comps_n || 0) + ' comps'
            + (l.comps_p40 != null && l.comps_p60 != null ? `, middle €${Number(l.comps_p40).toFixed(1)}–${Number(l.comps_p60).toFixed(1)}` : '')
            + ')' : null, l.comps_median == null ? 'muted' : '')}
        ${_dRow('Comps source', l.comps_source ? (_COMPS_SOURCE_SHORT[l.comps_source] || l.comps_source)
            + (l.comps_radius_m ? ` · ≤${Math.round(l.comps_radius_m)} m` : '') : null, l.comps_source ? '' : 'muted')}
        ${l.price_z != null ? _dRow(t('card.z'), t('card.z_val', {
            z: Math.abs(l.price_z).toFixed(1), gv: Math.abs(scoreSettings.rent_gv_z_max ?? -1.1).toFixed(1) }),
            l.price_z <= (scoreSettings.rent_gv_z_max ?? -1.1) ? 'good' : '') : ''}
        ${l.score_cap_reason ? _dRow('Cap', t('card.cap', { reason: l.score_cap_reason }), 'bad') : ''}
        ${_dRow('Total', l.score_total != null ? l.score_total + ' / 100' : null)}
      </div>
      ${(l.comps_n || 0) > 0 ? `<button class="btn-comps-list" data-comps-list>${escapeHtml(t('card.comps_show', { n: l.comps_n }))}</button>
      <div class="comps-list" data-comps-box hidden></div>` : ''}
    </div>`;

  // Lume's price alert (only shown when listing is priced above similar flats nearby)
  const compsSugHtml = (() => {
    const dp  = l.comps_delta_pct;
    const med = l.comps_median;
    const sqm = l.sqm;
    const rentMo = l.rent_mo;
    if (dp == null || dp <= 0 || !med || !sqm) return '';
    const compsSugRent = Math.round(med * sqm / 25) * 25;
    const diff = rentMo - compsSugRent;
    if (diff <= 0) return '';
    return `<div class="suggested-rent-row sug-warn">
      <span class="sug-label">${escapeHtml(t('comps.suggest'))}</span>
      <span class="sug-val" style="color:var(--yellow);font-weight:700">€${fmt(compsSugRent)}/mo</span>
      <span class="sug-label">${escapeHtml(t('comps.diff_rent', {diff: fmt(diff), n: l.comps_n || 0}))}</span>
    </div>`;
  })();

  // (The OMI-benchmark "OMI suggests:" row used to render here as a
  // second opinion alongside compsSugHtml. Removed — comps reflect the
  // actual local market and the OMI number was confusing duplication.
  // The OMI mid is still surfaced in the score-breakdown tooltip and
  // the detail page for users who want the official benchmark.)

  // ── Variant A: Calm Hierarchy ─────────────────────────────────────────────
  const score     = l.score_total ?? 0;
  const ringColor = sc === 'high' ? '#2A7A5A' : sc === 'mid' ? '#E8922A' : '#E05C4B';
  const C         = 2 * Math.PI * 23;   // r=23 for size=52
  const ringOffset = C * (1 - Math.max(0, Math.min(100, score)) / 100);

  // SVG score ring (animated count-up via IntersectionObserver — see globals)
  const scoreRingHtml = `
    <div class="score-ring-wrap"
         data-score-target="${score}"
         data-score-color="${ringColor}"
         data-sc-total="${l.score_total ?? ''}"
         data-sc-price="${l.score_price ?? ''}"
         data-sc-prop="${l.score_property ?? l.score_physical ?? ''}"
         data-sc-loc="${l.score_location ?? l.geo_score ?? ''}"
         data-sc-pen="${l.score_penalty ?? ''}"
         data-sc-geo="${l.geo_score ?? ''}"
         data-sc-phys="${l.score_physical ?? ''}"
         data-sc-rent="${l.score_rent ?? ''}"
         data-sc-value="${l.score_value ?? ''}"
         data-sc-fascia="${l.score_fascia ?? ''}"
         data-w-price="${wP}"
         data-w-prop="${wPr}"
         data-w-loc="${wL}"
         data-w-pen="${wPn}"
         data-sc-cap="${escapeHtml(l.score_cap_reason || '')}">
      <svg width="52" height="52" viewBox="0 0 52 52" style="display:block">
        <circle cx="26" cy="26" r="23" fill="none" stroke="#EDEAE4" stroke-width="3.5"/>
        <circle class="score-ring-fill" cx="26" cy="26" r="23" fill="none"
                stroke="${ringColor}" stroke-width="3.5"
                stroke-linecap="round"
                stroke-dasharray="${C.toFixed(2)}"
                stroke-dashoffset="${C.toFixed(2)}"
                transform="rotate(-90 26 26)"
                data-final-offset="${ringOffset.toFixed(2)}"/>
      </svg>
      <div class="score-ring-text"
           style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
                  font-weight:800;font-size:17px;letter-spacing:-0.3px;color:${ringColor};font-family:var(--font)">0</div>
    </div>`;

  // Gem chip on photo (replaces header badge)
  const gemPhotoChip = l.hidden_gem
    ? `<div class="gem-photo-chip chip-hidden"><span style="font-size:13px">💎</span><span>${escapeHtml(t('badge.hidden_gem').replace(/^✦\s*/, ''))}</span></div>`
    : l.good_value
    ? `<div class="gem-photo-chip chip-good"><span style="font-size:13px">💰</span><span>${escapeHtml(t('badge.great_value').replace(/^💰\s*/, ''))}</span></div>`
    : '';

  // Photo section
  // New badge as photo overlay (Fix 3)
  const newOverlayHtml = isNew(l)
    ? `<div class="card-photo-new-badge">${escapeHtml(t('badge.new'))}</div>`
    : '';
  const photoSection = (l.thumbnail || gemPhotoChip || newOverlayHtml)
    ? `<div class="card-photo-wrap">${l.thumbnail ? `<img class="card-thumb" src="${_safeUrl(l.thumbnail)}" alt="" loading="lazy">` : ''}${gemPhotoChip}${newOverlayHtml}</div>`
    : '';

  // Single-line comps row (Fixes 1 + 4)
  const compsBarHtml = compsRowHtml(l, true);

  // Single "why" sentence — Lume voice, neighbours framing.
  // STRINGS keys: callout.{hidden_gem,great_value,below_market,at_market,above_market}
  // and conf.{high,medium,low,thin} carry the en/it phrasing.
  const whySentence = (() => {
    const dp  = l.comps_delta_pct;
    const n   = l.comps_n || 0;
    const med = l.comps_median;
    const confRaw = (l.comps_conf_label || '').toLowerCase();
    const confL = confRaw === 'high'   ? t('conf.high')
                : confRaw === 'medium' ? t('conf.medium')
                : confRaw === 'low'    ? t('conf.thin')
                : '';
    if (dp == null && !omiMid) return null;
    if (l.price_suspect) return escapeHtml(t('card.suspect'));
    if (dp != null && med != null) {
      const abs   = Math.abs(dp).toFixed(1);
      const psqm  = `€${med}/m²/mo`;
      // {psqm} and {conf} are interpolated separately by the callout
      // templates ("...({psqm} · {conf})"). Pre-concatenating the
      // confidence into psqm here used to double-print it, e.g.
      // "(€31.5/m²/mo · Lume is fairly sure · Lume is fairly sure)".
      const vars  = { pct: abs, psqm, conf: confL, n };
      // Wrap the badge name in <strong> in the templated output.
      // The translated headline contains placeholders, not literal HTML, so
      // we substitute the variable, then bold the leading badge phrase.
      if (l.hidden_gem) {
        const s = t('callout.hidden_gem', vars);
        return s.replace(/^([^—]+)—/, '<strong>$1</strong>—');
      }
      if (l.good_value) {
        const s = t('callout.great_value', vars);
        return s.replace(/^([^—]+)—/, '<strong>$1</strong>—');
      }
      if (dp <= -3)      return t('callout.below_market', vars);
      if (dp >= 5)       return t('callout.above_market', { ...vars, pct: dp.toFixed(1) });
      return t('callout.at_market', vars);
    }
    return omiMid
      ? `No local flats to compare — Lume benchmarked against OMI ${l.omi_zona || 'zone'} (€${omiMid}/m²/mo).`
      : null;
  })();
  const whyClass  = l.price_suspect ? 'why-sentence why-neutral why-suspect'
                  : (l.hidden_gem || l.good_value || (l.comps_delta_pct != null && l.comps_delta_pct < 0))
                    ? 'why-sentence' : 'why-sentence why-neutral';

  // Condo fees + the all-in monthly cost, when the listing states them.
  const speseNote = l.spese_condominiali
    ? `<span style="font-size:11px;color:var(--muted)"> + €${fmt(l.spese_condominiali)} fees = €${fmt((Number(l.rent_mo) || 0) + Number(l.spese_condominiali))} all-in</span>`
    : '';

  const sourceBadge = l.source === 'idealista'
    ? '<span class="source-badge idealista">Idealista</span>'
    : '<span class="source-badge immobiliare">Immobiliare.it</span>';
  const fascia = l.omi_fascia || '?';

  const card = document.createElement('div');
  card.className = 'card' + (l.hidden_gem ? ' is-gem' : (l.good_value ? ' is-goodvalue' : ''));
  card.setAttribute('data-listing-id', String(l.id));
  card.innerHTML = `
    ${photoSection}

    <div class="card-header-a">
      ${scoreRingHtml}
      <div class="card-header-text">
        <div class="card-neighbourhood-row">
          <span class="card-neighbourhood">${escapeHtml(formatAddress(l.neighbourhood) || '–')}</span>
          <span class="id-pill" data-listing-id="${escapeHtml(String(l.id))}"
                title="Lume listing ID — click to search">${escapeHtml(String(l.id))}</span>
        </div>
        <div class="card-address-row">
          <span class="card-address">${escapeHtml(l.address ? formatAddress(l.address) : (formatAddress(l.neighbourhood) || '–'))}</span>
          ${sourceBadge}
        </div>
      </div>
    </div>

    <div class="price-row-a">
      <span class="price-main-a">${l.rent_mo != null ? '€' + fmt(l.rent_mo) : '–'}</span>
      <span style="font-size:13px;color:var(--muted);font-weight:500">${l.rent_mo != null ? '/mo' : ''}</span>
      ${speseNote}
      ${specsPillHtml(l)}
      ${areaPsqmPillHtml(l, true)}
    </div>
    ${compsSugHtml}
    ${compsBarHtml}

    ${statPillsHtml(l, true)}

    ${whySentence ? `<div class="${whyClass}">${whySentence}${reasonsBulletsHtml(l)}</div>` : ''}

    <div class="card-footer-a">
      <button class="btn-bookmark${Collections.isInAnyCollection(l.id) ? ' active' : ''}"
              data-listing-id="${escapeHtml(String(l.id))}"
              style="margin-left:auto"
              onclick="openCollectionMenu(this.dataset.listingId, event)"
              title="Save to collection" aria-label="Save to collection">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>
      </button>
      <button class="btn-share"
              data-listing-id="${escapeHtml(String(l.id))}"
              onclick="shareListing(this.dataset.listingId, event)"
              title="Share listing" aria-label="Share listing">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.6" y1="13.5" x2="15.4" y2="17.5"/><line x1="15.4" y1="6.5" x2="8.6" y2="10.5"/></svg>
      </button>
      <button class="btn-fav${isRentFav(l.id) ? ' is-fav' : ''}"
              data-rent-id="${escapeHtml(String(l.id))}"
              title="${isRentFav(l.id) ? 'Remove from favourites' : 'Add to favourites'}">
        ${isRentFav(l.id) ? '♥' : '♡'} Fav
      </button>
      <a class="link-btn" href="${_safeUrl(l.url)}" target="_blank" rel="noopener"
         data-listing-id="${escapeHtml(String(l.id))}"
         onclick="_promptArchiveListing(this.dataset.listingId, 'rental', this)">${escapeHtml(t('card.view'))}</a>
    </div>
    ${detailsHtml}
    ${extremeUnderpricingBannerHtml(l)}
    ${staleBannerHtml(l)}
  `;

  card.querySelector('[data-rent-id]').addEventListener('click', e => {
    const id  = e.currentTarget.dataset.rentId;
    const now = toggleRentFav(id);
    e.currentTarget.innerHTML = (now ? '♥' : '♡') + ' Fav';
    e.currentTarget.classList.toggle('is-fav', now);
    e.currentTarget.title = now ? 'Remove from favourites' : 'Add to favourites';
  });

  // Details toggle
  const _toggleBtn   = card.querySelector('[data-details-toggle]');
  const _detailPanel = card.querySelector('[data-details-panel]');
  if (_toggleBtn && _detailPanel) {
    _toggleBtn.addEventListener('click', () => {
      const open = _toggleBtn.classList.toggle('open');
      _detailPanel.classList.toggle('open', open);
      _toggleBtn.childNodes[0].textContent = (open ? t('card.data_hide') : t('card.how_lume_scored')) + ' ';
    });
  }
  const _compsBtn = card.querySelector('[data-comps-list]');
  const _compsBox = card.querySelector('[data-comps-box]');
  if (_compsBtn && _compsBox) {
    _compsBtn.addEventListener('click', () => {
      _compsBox.hidden = !_compsBox.hidden;
      if (!_compsBox.hidden && !_compsBox.dataset.loaded) {
        _compsBox.dataset.loaded = '1';
        _loadCompsList(l, _compsBox);
      }
    });
  }

  return card;
}
