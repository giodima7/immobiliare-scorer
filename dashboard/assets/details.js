// Lume dashboard — the listing Details view: everything Lume knows about a
// flat (verdict, price history, all facts, location, score, comparable
// flats) over the page, so it can be judged before opening the portal.
// Header and actions stay put; only the middle scrolls. Focus stays inside
// while it's open and returns to where it came from.
// Plain script (no bundler); shares globals with the other assets/*.js files.
// Declarations only: code that runs at start-up lives in init.js, which loads
// last, so any function here may call any other.

let _lmLastFocus = null;
let _lmListing = null;

function openListingModal(id) {
  const l = ALL_RENT.find(x => String(x.id) === String(id))
         || (typeof _savedRows !== 'undefined' && _savedRows.find(x => String(x.id) === String(id)));
  const box = document.getElementById('listingModal');
  if (!l || !box) return;
  _lmListing = l;
  if (box.hidden) _lmLastFocus = document.activeElement;
  document.getElementById('lmBody').innerHTML = listingModalHtml(l);
  box.hidden = false;
  document.body.classList.add('lm-open');
  box.querySelector('.lm-close')?.focus();
  _lmEnsureFull(l).then(() => {
    if (_lmListing !== l || box.hidden) return;
    _lmLoadPhotos(l);
    const reasons = document.getElementById('lmReasons');
    if (reasons) reasons.innerHTML = reasonsBulletsHtml(l);
    const comps = document.getElementById('lmComps');
    if (comps && (l.comps_n || 0) > 0) _loadCompsList(l, comps);
  });
}

function closeListingModal() {
  const box = document.getElementById('listingModal');
  if (!box || box.hidden) return;
  box.hidden = true;
  _lmListing = null;
  document.body.classList.remove('lm-open');
  document.getElementById('lmBody').innerHTML = '';
  if (_lmLastFocus && _lmLastFocus.isConnected) _lmLastFocus.focus();
}

// Tab / Shift+Tab cycle inside the dialog.
function _lmTrapFocus(e) {
  if (e.key !== 'Tab') return;
  const box = document.getElementById('listingModal');
  if (!box || box.hidden) return;
  const items = [...box.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), select, [tabindex]:not([tabindex="-1"])')]
    .filter(el => el.offsetParent !== null);
  if (!items.length) return;
  const first = items[0], last = items[items.length - 1];
  if (!box.contains(document.activeElement)) { e.preventDefault(); first.focus(); return; }
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

// The local grid is loaded without photos, comparable ids and score
// reasons (half the payload); fetch them for this one listing.
async function _lmEnsureFull(l) {
  if (!IS_LIVE || l._full) return;
  try {
    const r = await fetch(`/rentals/extras?ids=${encodeURIComponent(String(l.id))}`);
    if (r.ok) {
      const row = (await r.json())[0];
      if (row) for (const [k, v] of Object.entries(row)) if (l[k] === undefined) l[k] = v;
    }
  } catch { /* the view still works without them */ }
  l._full = true;
}

// Online rows are loaded without the photo list (it's a sixth of the
// payload); fetch it for this one listing.
async function _lmLoadPhotos(l) {
  let photos = Array.isArray(l.photos) ? l.photos : null;
  if (!photos && !IS_LIVE && SupabaseClient.configured) {
    const r = await SupabaseClient.fetchByIds([l.id], 'id,photos');
    photos = (r[0] && r[0].photos) || [];
  }
  photos = (photos || []).filter(Boolean);
  if (!photos.length && l.thumbnail) photos = [l.thumbnail];
  const box = document.getElementById('lmPhotos');
  if (!box || !box.isConnected) return;
  // Photos are a mouse convenience; keyboard users get the "+N" link.
  box.innerHTML = photos.length
    ? photos.slice(0, 8).map(u => `<a href="${_safeUrl(u)}" target="_blank" rel="noopener" class="lm-photo" tabindex="-1">
        <img src="${_safeUrl(u)}" alt="" loading="lazy"></a>`).join('')
      + (l.photo_count > photos.length ? `<a class="lm-photo lm-photo-more" href="${_safeUrl(l.url)}" target="_blank" rel="noopener">${escapeHtml(t('lm.more_photos', { n: l.photo_count - Math.min(8, photos.length) }))}</a>` : '')
    : `<div class="lm-photo lm-photo-empty">${icon('photo')}</div>`;
}

function _lmDate(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return isNaN(d) ? iso : fmtDate(d);
}

// "Was €X" — a price drop on this listing, or the same flat listed before.
function priceHistoryText(l) {
  if (l.previous_price && l.rent_mo && l.previous_price > l.rent_mo) {
    return t('lm.price_dropped', { was: fmt(l.previous_price), date: _lmDate(l.price_changed_date) || '' });
  }
  if (l.relisted_from_rent) {
    return t('lm.relisted', { was: fmt(l.relisted_from_rent), date: _lmDate(l.relisted_from_date) || '' });
  }
  return null;
}

function _lmVerdict(l) {
  const dp = l.comps_delta_pct;
  const label = l.hidden_gem ? t('gem.hidden') : l.good_value ? t('gem.good') : null;
  const cls = l.price_suspect ? 'warn' : l.hidden_gem ? 'gem' : (label || (dp != null && dp <= -3)) ? 'good'
            : (dp != null && dp >= 5) ? 'bad' : '';
  const head = l.price_suspect ? t('card.suspect')
    : dp == null ? t('lm.no_comps')
    : (label ? label + ' · ' : '') + (Math.abs(dp) < 1 ? t('card.in_line')
      : dp < 0 ? t('lm.below', { pct: fmtNum(Math.abs(dp), 1) }) : t('lm.above', { pct: fmtNum(dp, 1) }));
  const gv = scoreSettings.rent_gv_z_max ?? -1.1;
  const conf = { high: t('conf.high'), medium: t('conf.medium'), low: t('conf.thin') }[(l.comps_conf_label || '').toLowerCase()];
  const sub = [
    l.comps_n ? t('lm.comps_n', { n: l.comps_n, m: l.comps_radius_m ? fmt(Math.round(l.comps_radius_m)) : '–' }) : null,
    l.comps_p40 != null && l.comps_p60 != null ? t('lm.middle', { a: fmt1(l.comps_p40), b: fmt1(l.comps_p60) }) : null,
    dp != null && dp < 0 && l.price_z != null ? t(l.price_z <= gv ? 'lm.strong_discount' : 'lm.normal_discount') : null,
    conf,
  ].filter(Boolean).join(' · ');
  return { cls, head, sub };
}

function listingModalHtml(l) {
  const esc = escapeHtml;
  const yesNo = v => v === true ? t('lm.yes') : v === false ? t('lm.no') : null;
  const heat  = { autonomous: t('lm.heat_own'), centralised: t('lm.heat_central'), none: t('lm.heat_none') };
  const floor = (() => {
    const lbl = _floorLabel(l);
    if (!lbl) return null;
    return lbl + (l.elevator === true ? ' · ' + t('lm.lift') : l.elevator === false ? ' · ' + t('lm.no_lift') : '');
  })();
  const fees = l.spese_condominiali ? Number(l.spese_condominiali) : null;
  const facts = [
    [t('lm.size'), l.sqm ? `${fmt(l.sqm)} m²` : null],
    [t('lm.rooms'), l.rooms != null ? `${l.rooms}${l.bedrooms ? ' · ' + t('lm.bedrooms', { n: l.bedrooms }) : ''}` : null],
    [t('lm.bathrooms'), l.bathrooms],
    [t('lm.floor'), floor],
    [t('lm.condition'), l.condition ? String(l.condition).replace(' (Idealista)', '') : null],
    [t('lm.heating'), l.heating_type ? (heat[l.heating_type] ?? null) : null],
    [t('must.balcony'), yesNo(l.has_balcony)],
    [t('must.parking'), yesNo(l.has_parking)],
    [t('must.furnished'), yesNo(l.furnished)],
    [t('lm.energy'), l.energy_class],
    [t('lm.year'), l.year_built],
    [t('lm.exposure'), l.is_external === true ? t('lm.external') : l.is_external === false ? t('lm.internal') : null],
    [t('lm.contract'), l.contract_type],
    [t('lm.availability'), l.availability],
    [t('lm.ac'), l.air_conditioning ? t('lm.yes') : null],
    [t('lm.fees_k'), fees ? `€${fmt(fees)}${t('card.per_month')}` : null],
  ];
  const known   = facts.filter(([, v]) => v != null && v !== '');
  const unknown = facts.filter(([, v]) => v == null || v === '').map(([k]) => k);
  const v = _lmVerdict(l);
  const history = priceHistoryText(l);
  const w = { price: scoreSettings.w_price ?? 0.45, prop: scoreSettings.w_property ?? 0.25,
              loc: scoreSettings.w_location ?? 0.20, pen: scoreSettings.w_penalty ?? 0.10 };
  const bar = (k, val, wt) => `<div class="lm-bar sc-${scoreClass(val ?? 50)}"><span class="lm-bar-k">${esc(t(k))} <span class="lm-muted">${Math.round(wt * 100)}%</span></span>
      <span class="lm-bar-track"><span class="lm-bar-fill" style="width:${Math.max(0, Math.min(100, val ?? 50))}%"></span></span>
      <span class="lm-bar-v">${val ?? '–'}</span></div>`;
  const near = (ic, label, val) => val == null ? '' : `<li>${icon(ic)}<span>${esc(label)}</span><span class="lm-muted">${esc(val)}</span></li>`;
  const dist = m => m != null ? `${fmt(Math.round(m))} m` : null;
  const metroLine = l.metro_nearest_name
    ? `${l.metro_nearest_name}${l.metro_nearest_line ? ' ' + l.metro_nearest_line : ''}` : t('lm.metro');
  const site = l.source === 'idealista' ? 'Idealista' : 'Immobiliare.it';
  const provenance = l.detail_fetched_at ? t('lm.page_read', { date: _lmDate(l.detail_fetched_at) }) : t('lm.page_not_read');
  const fav = isRentFav(l.id);
  const id = esc(String(l.id));
  const method = compsMethodTip(l);

  return `
  <div class="lm-head">
    <div class="lm-title">
      <h2 id="lmTitle">${esc(formatAddress(l.neighbourhood) || '–')}${l.address ? ' · ' + esc(formatAddress(l.address)) : ''}</h2>
      <div class="lm-sub">${esc(site)} · ID ${id}${l.first_seen_date ? ' · ' + esc(t('lm.first_seen', { date: _lmDate(l.first_seen_date) })) : ''}</div>
    </div>
    <button type="button" class="btn btn-secondary btn-icon lm-close" aria-label="${esc(t('lm.close'))}">${icon('x')}</button>
  </div>

  <div class="lm-scroll">
    <div class="lm-photos" id="lmPhotos"></div>

    <div class="lm-verdict ${v.cls}">
      <div class="lm-verdict-head">${esc(v.head)}</div>
      ${v.sub ? `<div class="lm-verdict-sub">${esc(v.sub)}</div>` : ''}
    </div>

    <div class="lm-money">
      <div><span class="lm-rent">€${fmt(l.rent_mo)}</span><span class="lm-muted">${esc(t('card.per_month'))}</span></div>
      ${fees ? `<div class="lm-muted">+ €${fmt(fees)} ${esc(t('lm.fees'))} = <strong>€${fmt((Number(l.rent_mo) || 0) + fees)}</strong> ${esc(t('lm.all_in'))}</div>` : ''}
      ${l.ask_psqm ? `<div class="lm-muted">€${fmt1(l.ask_psqm)}/m²${l.comps_median ? ' · ' + esc(t('lm.nearby_median', { v: fmt1(l.comps_median) })) : ''}</div>` : ''}
      ${history ? `<div class="lm-history">${icon('trending-down')}${esc(history)}</div>` : ''}
    </div>

    <h3 class="lm-section-title">${esc(t('lm.facts'))}</h3>
    <div class="lm-facts">
      ${known.map(([k, val]) => `<div class="lm-fact"><div class="lm-fact-k">${esc(k)}</div><div class="lm-fact-v">${esc(String(val))}</div></div>`).join('')}
    </div>
    ${unknown.length ? `<div class="lm-unknown-line">${esc(t('lm.not_known_list', { list: unknown.join(', ') }))}</div>` : ''}
    <div class="lm-note">${icon(l.detail_fetched_at ? 'check' : 'clock')}${esc(provenance)}${(l.twin_filled || []).length
        ? ' · ' + esc(t('card.twin', { site: l.source === 'idealista' ? 'Immobiliare.it' : 'Idealista' })) : ''}</div>

    <div class="lm-two">
      <section>
        <h3 class="lm-section-title">${esc(t('lm.location'))}</h3>
        <ul class="lm-near">
          ${near('train', metroLine, l.metro_walk_min != null ? t('lm.walk', { n: l.metro_walk_min }) : dist(l.metro_nearest_dist_m))}
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
        <div id="lmReasons">${reasonsBulletsHtml(l)}</div>
      </section>
    </div>

    ${(l.comps_n || 0) > 0 ? `<section>
      <h3 class="lm-section-title">${esc(t('lm.compared_with'))}</h3>
      ${method ? `<p class="lm-note">${icon('info-circle')}${esc(method)}</p>` : ''}
      <div class="lm-comps" id="lmComps"></div>
    </section>` : ''}

    <div class="lm-collections" id="lmCollections" hidden></div>
  </div>

  <div class="lm-actions">
    <button type="button" class="btn btn-secondary${fav ? ' on' : ''}" data-lm-fav="${id}" aria-pressed="${fav}">${icon('heart')}<span>${esc(t(fav ? 'lm.saved' : 'lm.save'))}</span></button>
    <button type="button" class="btn btn-secondary" data-lm-collections="${id}" aria-expanded="false" aria-controls="lmCollections">${icon('folder')}<span>${esc(t('lm.collection'))}</span></button>
    <button type="button" class="btn btn-ghost" data-listing-id="${id}" onclick="shareListing(this.dataset.listingId, event)">${icon('share')}<span>${esc(t('lm.share'))}</span></button>
    <a class="btn btn-primary lm-open" href="${_safeUrl(l.url)}" target="_blank" rel="noopener"
       data-listing-id="${id}" onclick="_promptArchiveListing(this.dataset.listingId, 'rental', this)"><span>${esc(t('lm.open_on', { site }))}</span>${icon('external-link')}</a>
  </div>`;
}

// "Collection" in Details: tick the collections this flat belongs to, or
// make a new one — inline, no prompt().
function renderLmCollections(listingId) {
  const box = document.getElementById('lmCollections');
  if (!box) return;
  const cols = Collections.getAll();
  box.innerHTML = `
    <h3 class="lm-section-title" style="margin-top:0">${escapeHtml(t('lm.collection'))}</h3>
    ${cols.length ? cols.map(c => `
      <label class="lm-col-row">
        <input type="checkbox" data-col-toggle="${escapeHtml(c.id)}" ${Collections.isInCollection(c.id, listingId) ? 'checked' : ''}>
        <span>${escapeHtml(c.name)}</span>
        <span class="col-count">${fmt(c.listing_ids.length)}</span>
      </label>`).join('') : `<p class="lm-unknown-line">${escapeHtml(t('col.none_yet'))}</p>`}
    <form class="lm-col-new" data-col-new="${escapeHtml(String(listingId))}" autocomplete="off">
      <input type="text" class="input" maxlength="60" placeholder="${escapeHtml(t('col.new_placeholder'))}" aria-label="${escapeHtml(t('col.new_placeholder'))}">
      <button type="submit" class="btn btn-secondary">${icon('plus')}<span>${escapeHtml(t('col.create'))}</span></button>
    </form>`;
}
