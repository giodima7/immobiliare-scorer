// Lume dashboard — the listing Details view: everything Lume knows about a
// flat (verdict, price history, all facts, location, score, comparable
// flats) over the page, so it can be judged before opening the portal.
// Plain script (no bundler); shares globals with the other assets/*.js files.
// Declarations only: code that runs at start-up lives in init.js, which loads
// last, so any function here may call any other.

let _lmLastFocus = null;

function openListingModal(id) {
  const l = ALL_RENT.find(x => String(x.id) === String(id));
  const box = document.getElementById('listingModal');
  if (!l || !box) return;
  _lmLastFocus = document.activeElement;
  document.getElementById('lmBody').innerHTML = listingModalHtml(l);
  box.hidden = false;
  document.body.classList.add('lm-open');
  box.querySelector('.lm-close')?.focus();
  _lmLoadPhotos(l);
  const comps = document.getElementById('lmComps');
  if (comps && (l.comps_n || 0) > 0) _loadCompsList(l, comps);
}

function closeListingModal() {
  const box = document.getElementById('listingModal');
  if (!box || box.hidden) return;
  box.hidden = true;
  document.body.classList.remove('lm-open');
  document.getElementById('lmBody').innerHTML = '';
  if (_lmLastFocus && _lmLastFocus.isConnected) _lmLastFocus.focus();
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
  box.innerHTML = photos.length
    ? photos.slice(0, 8).map(u => `<a href="${_safeUrl(u)}" target="_blank" rel="noopener" class="lm-photo">
        <img src="${_safeUrl(u)}" alt="" loading="lazy"></a>`).join('')
      + (l.photo_count > photos.length ? `<a class="lm-photo lm-photo-more" href="${_safeUrl(l.url)}" target="_blank" rel="noopener">+${l.photo_count - Math.min(8, photos.length)}</a>` : '')
    : `<div class="lm-photo lm-photo-empty">${icon('photo')}</div>`;
}

function _lmDate(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleDateString(currentLang === 'it' ? 'it-IT' : 'en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
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

function listingModalHtml(l) {
  const esc = escapeHtml;
  const yesNo = v => v === true ? t('lm.yes') : v === false ? t('lm.no') : null;
  const heat  = { autonomous: t('lm.heat_own'), centralised: t('lm.heat_central'), none: t('lm.heat_none') };
  const fact = (label, value, cls = '') => `<div class="lm-fact${cls ? ' ' + cls : ''}">
      <div class="lm-fact-k">${esc(label)}</div>
      <div class="lm-fact-v${value == null || value === '' ? ' lm-unknown' : ''}">${value == null || value === '' ? esc(t('lm.not_known')) : esc(String(value))}</div>
    </div>`;
  const floor = (() => {
    const lbl = _floorLabel(l);
    if (!lbl) return null;
    return lbl + (l.elevator === true ? ' · ' + t('lm.lift') : l.elevator === false ? ' · ' + t('lm.no_lift') : '');
  })();
  const fees = l.spese_condominiali ? Number(l.spese_condominiali) : null;
  const label = l.hidden_gem ? t('gem.hidden') : l.good_value ? t('gem.good') : null;
  const dp = l.comps_delta_pct;
  const verdictCls = l.price_suspect ? 'lm-verdict warn' : (label || (dp != null && dp < 0)) ? 'lm-verdict good' : 'lm-verdict';
  const verdictHead = l.price_suspect ? t('card.suspect')
    : dp == null ? t('lm.no_comps')
    : (label ? label + ' · ' : '') + (dp < 0 ? t('lm.below', { pct: Math.abs(dp).toFixed(1) }) : t('lm.above', { pct: dp.toFixed(1) }));
  const verdictSub = [
    l.comps_n ? t('lm.comps_n', { n: l.comps_n, m: l.comps_radius_m ? fmt(Math.round(l.comps_radius_m)) : '–' }) : null,
    l.comps_p40 != null && l.comps_p60 != null ? t('lm.middle', { a: Number(l.comps_p40).toFixed(1), b: Number(l.comps_p60).toFixed(1) }) : null,
    l.price_z != null ? t('card.z_val', { z: Math.abs(l.price_z).toFixed(1), gv: Math.abs(scoreSettings.rent_gv_z_max ?? -1.1).toFixed(1) }) : null,
  ].filter(Boolean).join(' · ');
  const history = priceHistoryText(l);
  const w = { price: scoreSettings.w_price ?? 0.45, prop: scoreSettings.w_property ?? 0.25,
              loc: scoreSettings.w_location ?? 0.20, pen: scoreSettings.w_penalty ?? 0.10 };
  const bar = (k, v, wt) => `<div class="lm-bar"><span class="lm-bar-k">${esc(t(k))} <span class="lm-muted">${Math.round(wt * 100)}%</span></span>
      <span class="lm-bar-track"><span class="lm-bar-fill" style="width:${Math.max(0, Math.min(100, v ?? 50))}%"></span></span>
      <span class="lm-bar-v">${v ?? '–'}</span></div>`;
  const near = (ic, label, val) => val == null ? '' : `<li>${icon(ic)}<span>${esc(label)}</span><span class="lm-muted">${esc(val)}</span></li>`;
  const metroLine = l.metro_nearest_name
    ? `${l.metro_nearest_name}${l.metro_nearest_line ? ' ' + l.metro_nearest_line : ''}` : t('lm.metro');
  const site = l.source === 'idealista' ? 'Idealista' : 'Immobiliare.it';
  const provenance = l.detail_fetched_at
    ? t('lm.page_read', { date: _lmDate(l.detail_fetched_at) })
    : t('lm.page_not_read');

  return `
  <div class="lm-head">
    <div class="lm-title">
      <h2 id="lmTitle">${esc(formatAddress(l.neighbourhood) || '–')}${l.address ? ' · ' + esc(formatAddress(l.address)) : ''}</h2>
      <div class="lm-sub">${esc(site)} · ID ${esc(String(l.id))}${l.first_seen_date ? ' · ' + esc(t('lm.first_seen', { date: _lmDate(l.first_seen_date) })) : ''}${l.photo_count ? ' · ' + esc(t('lm.photos', { n: l.photo_count })) : ''}</div>
    </div>
    <button type="button" class="lm-close" aria-label="${esc(t('lm.close'))}">${icon('x')}</button>
  </div>
  <div class="lm-photos" id="lmPhotos"></div>

  <div class="${verdictCls}">
    <div class="lm-verdict-head">${esc(verdictHead)}</div>
    ${verdictSub ? `<div class="lm-verdict-sub">${esc(verdictSub)}</div>` : ''}
  </div>

  <div class="lm-money">
    <div><span class="lm-rent">€${fmt(l.rent_mo)}</span><span class="lm-muted">/mo</span></div>
    ${fees ? `<div class="lm-muted">+ €${fmt(fees)} ${esc(t('lm.fees'))} = <strong>€${fmt((Number(l.rent_mo) || 0) + fees)}</strong> ${esc(t('lm.all_in'))}</div>` : ''}
    ${l.ask_psqm ? `<div class="lm-muted">€${Number(l.ask_psqm).toFixed(1)}/m²${l.comps_median ? ' · ' + esc(t('lm.nearby_median', { v: Number(l.comps_median).toFixed(1) })) : ''}</div>` : ''}
    ${history ? `<div class="lm-history">${icon('trending-down')}${esc(history)}</div>` : ''}
  </div>

  <div class="lm-facts">
    ${fact(t('lm.size'), l.sqm ? `${l.sqm} m²` : null)}
    ${fact(t('lm.rooms'), l.rooms != null ? `${l.rooms}${l.bedrooms ? ' · ' + t('lm.bedrooms', { n: l.bedrooms }) : ''}` : null)}
    ${fact(t('lm.bathrooms'), l.bathrooms)}
    ${fact(t('lm.floor'), floor)}
    ${fact(t('lm.condition'), l.condition ? String(l.condition).replace(' (Idealista)', '') : null)}
    ${fact(t('lm.heating'), l.heating_type ? (heat[l.heating_type] ?? null) : null)}
    ${fact(t('must.balcony'), yesNo(l.has_balcony))}
    ${fact(t('must.parking'), yesNo(l.has_parking))}
    ${fact(t('must.furnished'), yesNo(l.furnished))}
    ${fact(t('lm.energy'), l.energy_class)}
    ${fact(t('lm.year'), l.year_built)}
    ${fact(t('lm.exposure'), l.is_external === true ? t('lm.external') : l.is_external === false ? t('lm.internal') : null)}
    ${fact(t('lm.contract'), l.contract_type)}
    ${fact(t('lm.availability'), l.availability)}
    ${fact(t('lm.ac'), l.air_conditioning ? t('lm.yes') : null)}
    ${fact(t('lm.fees_k'), fees ? `€${fmt(fees)}/mo` : null)}
  </div>
  <div class="lm-note">${icon(l.detail_fetched_at ? 'check' : 'refresh')}${esc(provenance)}${(l.twin_filled || []).length
      ? ' · ' + esc(t('card.twin', { site: l.source === 'idealista' ? 'Immobiliare.it' : 'Idealista' })) : ''}</div>

  <div class="lm-two">
    <section>
      <h3>${esc(t('lm.location'))}</h3>
      <ul class="lm-near">
        ${near('train', metroLine, l.metro_walk_min != null ? t('lm.walk', { n: l.metro_walk_min }) : l.metro_nearest_dist_m != null ? `${fmt(Math.round(l.metro_nearest_dist_m))} m` : null)}
        ${near('bus', t('lm.tram'), l.tram_nearest_dist_m != null ? `${fmt(Math.round(l.tram_nearest_dist_m))} m` : null)}
        ${near('shopping-cart', t('lm.supermarket'), l.supermarket_nearest_dist_m != null ? `${fmt(Math.round(l.supermarket_nearest_dist_m))} m` : null)}
        ${near('trees', t('lm.park'), l.park_nearest_dist_m != null ? `${fmt(Math.round(l.park_nearest_dist_m))} m` : null)}
        ${near('school', t('lm.university'), l.university_nearest_dist_m != null ? `${fmt(Math.round(l.university_nearest_dist_m))} m` : null)}
      </ul>
    </section>
    <section>
      <h3>${esc(t('lm.score', { n: l.score_total ?? '–' }))}</h3>
      ${bar('lm.price', l.score_price, w.price)}
      ${bar('lm.location', l.score_location ?? l.geo_score, w.loc)}
      ${bar('lm.property', l.score_property, w.prop)}
      ${bar('lm.red_flags', l.score_penalty, w.pen)}
      ${l.score_cap_reason ? `<div class="lm-cap">${esc(t('card.cap', { reason: l.score_cap_reason }))}</div>` : ''}
      ${reasonsBulletsHtml(l)}
    </section>
  </div>

  ${(l.comps_n || 0) > 0 ? `<section>
    <h3>${esc(t('lm.compared_with'))}</h3>
    <div class="lm-comps" id="lmComps"></div>
  </section>` : ''}

  <div class="lm-actions">
    <button type="button" class="fb-btn${isRentFav(l.id) ? ' on' : ''}" data-lm-fav="${esc(String(l.id))}">${icon('heart')}<span>${esc(isRentFav(l.id) ? t('lm.saved') : t('lm.save'))}</span></button>
    <button type="button" class="fb-btn" data-listing-id="${esc(String(l.id))}" onclick="openCollectionMenu(this.dataset.listingId, event)">${icon('bookmark')}<span>${esc(t('lm.collection'))}</span></button>
    <button type="button" class="fb-btn" data-listing-id="${esc(String(l.id))}" onclick="shareListing(this.dataset.listingId, event)">${icon('share')}<span>${esc(t('lm.share'))}</span></button>
    <a class="fb-btn lm-open" href="${_safeUrl(l.url)}" target="_blank" rel="noopener"
       data-listing-id="${esc(String(l.id))}" onclick="_promptArchiveListing(this.dataset.listingId, 'rental', this)">${icon('external-link')}<span>${esc(t('lm.open_on', { site }))}</span></a>
  </div>`;
}
