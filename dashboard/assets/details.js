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
  lmLoadMapSnip(l);
  _lmEnsureFull(l).then(() => {
    if (_lmListing !== l || box.hidden) return;
    _lmLoadPhotos(l);
    const reasons = document.getElementById('lmReasons');
    if (reasons) reasons.innerHTML = reasonsBulletsHtml(l);
    const comps = document.getElementById('lmComps');
    if (comps && (l.comps_n || 0) > 0) _loadCompsList(l, comps);
  });
}

// Where the flat is, as a map snippet in Details (2026-10-28, the user: "a
// snippet of the flat's location"): OpenStreetMap's standard tiles in both
// themes — the Map tab's grey canvas left the street names unreadable ("I
// can't read the streets or tell where it is"); these name streets, parks
// and metro stops. Fixed (no drag or zoom), a pin — or a circle as wide as
// the guess when the place is approximate (geo_precision) — and Google Maps
// on a tap.
const _LM_APPROX_M = { zone: 600, metro: 350, street: 250, near: 200 };
let _lmMap = null;

function lmMapSnipHtml(l) {
  if (l.latitude == null || l.longitude == null) return '';
  const url = `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(`${l.latitude},${l.longitude}`)}`;
  return `<div class="lm-mapsnip" id="lmMapSnip" data-href="${escapeHtml(url)}" role="link" tabindex="0"
               aria-label="${escapeHtml(t('lm.open_maps'))}"></div>
    <a class="lm-maplink" href="${escapeHtml(url)}" target="_blank" rel="noopener">${icon('map-pin')}<span>${escapeHtml(t('lm.open_maps'))}</span>${icon('external-link')}</a>`;
}

async function lmLoadMapSnip(l) {
  const el = document.getElementById('lmMapSnip');
  if (!el) return;
  try { await ensureLeaflet(); } catch { el.remove(); return; }
  if (!el.isConnected || _lmListing !== l) return;
  _lmMap?.remove();
  const at = [Number(l.latitude), Number(l.longitude)];
  const approx = _LM_APPROX_M[l.geo_precision];
  const zoom = approx >= 500 ? 14 : 15;
  const m = L.map(el, { zoomControl: false, dragging: false, scrollWheelZoom: false, touchZoom: false,
                        doubleClickZoom: false, boxZoom: false, keyboard: false, tap: false })
    .setView(at, zoom);
  m.attributionControl.setPrefix(false);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
              { maxZoom: 19, attribution: '© OpenStreetMap' }).addTo(m);
  if (approx) L.circle(at, { radius: approx, color: '#2A7A5A', weight: 1.5, fillOpacity: 0.12 }).addTo(m);
  else L.circleMarker(at, { radius: 8, color: '#fff', weight: 2.5, fillColor: '#2A7A5A', fillOpacity: 1 }).addTo(m);
  _lmMap = m;
  // The sheet may still be sliding in or laying out: redraw at the real size.
  const fit = () => { if (_lmMap === m) { m.invalidateSize(); m.setView(at, zoom, { animate: false }); } };
  if ('ResizeObserver' in window) new ResizeObserver(fit).observe(el);
  else setTimeout(fit, 60);
}

document.addEventListener('click', e => {
  const snip = e.target.closest?.('#lmMapSnip');
  if (snip && !e.target.closest('.leaflet-control-attribution')) window.open(snip.dataset.href, '_blank', 'noopener');
});
document.addEventListener('keydown', e => {
  if (e.key === 'Enter' && e.target.id === 'lmMapSnip') window.open(e.target.dataset.href, '_blank', 'noopener');
});

function closeListingModal() {
  const box = document.getElementById('listingModal');
  if (!box || box.hidden) return;
  box.hidden = true;
  _lmListing = null;
  _lmMap?.remove();
  _lmMap = null;
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

// Rows are loaded without the photo list (online it's a sixth of the
// payload, locally a slim load drops it): fetch it for one listing, once.
async function listingPhotos(l) {
  if (l._photos) return l._photos;
  let photos = Array.isArray(l.photos) ? l.photos : null;
  try {
    const sale = /sale/.test(l.source || '');      // sales carry their photos locally
    if (!photos && IS_LIVE && !sale) {
      const r = await fetch(`/rentals/extras?ids=${encodeURIComponent(String(l.id))}`);
      if (r.ok) photos = ((await r.json())[0] || {}).photos || null;
    } else if (!photos && SupabaseClient.configured) {
      const r = await SupabaseClient.fetchByIds([l.id], 'id,photos');
      photos = (r[0] && r[0].photos) || null;
    }
  } catch { /* fall back to the thumbnail */ }
  photos = (photos || []).filter(Boolean);
  if (!photos.length && l.thumbnail) photos = [l.thumbnail];
  l._photos = photos;
  return photos;
}

async function _lmLoadPhotos(l) {
  const photos = await listingPhotos(l);
  const box = document.getElementById('lmPhotos');
  if (!box || !box.isConnected) return;
  // A click opens the viewer at that photo; "+N" leads to the rest on the site.
  box.innerHTML = photos.length
    ? photos.map((u, i) => `<button type="button" class="lm-photo" data-pv-open="${i}" aria-label="${escapeHtml(t('pv.open', { i: i + 1, n: photos.length }))}">
        <img src="${_safeUrl(u)}" alt="" loading="lazy"></button>`).join('')
      + (l.photo_count > photos.length ? `<a class="lm-photo lm-photo-more" href="${_safeUrl(l.url)}" target="_blank" rel="noopener">${escapeHtml(t('lm.more_photos', { n: l.photo_count - photos.length }))}</a>` : '')
    : `<div class="lm-photo lm-photo-empty">${icon('photo')}</div>`;
}

// ── Photos: paging on the card, full-screen viewer from Details ──────────────
// The user, 2026-10-25: "can I scroll through the pictures… right now I can
// just click on one and see that one only in a new page".
// Card photos (2026-10-28, "the photo scrolling before opening the listing
// doesn't work well"): the card's .card-photo-track starts with the thumbnail
// and is filled with the rest of the gallery — prefetched for the cards about
// to scroll into view, one request for many — so a swipe is a native scroll
// that follows the finger and snaps photo by photo. Arrows scroll the strip.
function _cardListing(id) {
  return ALL_RENT.find(x => String(x.id) === id)
      || (typeof ALL_SALE !== 'undefined' && ALL_SALE.find(x => String(x.id) === id))
      || (typeof _savedRows !== 'undefined' && _savedRows.find(x => String(x.id) === id));
}

function cardPhotoFill(box) {
  if (!box) return Promise.resolve();
  if (box._fill) return box._fill;
  box._fill = (async () => {
    const l = _cardListing(box.dataset.photoId);
    const track = box.querySelector('.card-photo-track');
    if (!l || !track) return;
    const photos = await listingPhotos(l);
    if (photos.length < 2 || !box.isConnected || track.children.length > 1) return;
    // The thumbnail is the first photo, small: the strip goes on from the second.
    track.insertAdjacentHTML('beforeend', photos.slice(1).map(u =>
      `<img class="card-thumb" src="${_safeUrl(u)}" alt="" loading="lazy" decoding="async" draggable="false">`).join(''));
    box.dataset.n = photos.length;
    _cardPhotoCount(box);
  })();
  return box._fill;
}

function _cardPhotoCount(box) {
  const track = box.querySelector('.card-photo-track');
  const count = box.querySelector('.card-photo-count');
  const n = Number(box.dataset.n) || 0;
  if (!track || !count || n < 2) return;
  const i = Math.round(track.scrollLeft / Math.max(1, track.clientWidth));
  count.textContent = `${Math.min(i, n - 1) + 1}/${n}`;
  count.hidden = false;
}

async function cardPhotoStep(box, step) {
  if (!box) return;
  await cardPhotoFill(box);
  const track = box.querySelector('.card-photo-track');
  if (!track || track.children.length < 2) return;
  const w = track.clientWidth, n = track.children.length;
  const i = Math.round(track.scrollLeft / Math.max(1, w));
  const to = (i + step + n) % n;            // round the end, like the viewer
  track.scrollTo({ left: to * w, behavior: Math.abs(to - i) > 1 ? 'auto' : 'smooth' });
}

// Prefetch the galleries of the cards about to be seen: one request for all
// of them (local /rentals/extras?ids=…, else Supabase), so the first swipe
// already has something to scroll to.
const _photoPrefetchIds = new Set();
let _photoPrefetchTimer = 0;
async function _prefetchCardPhotos() {
  const ids = [..._photoPrefetchIds];
  _photoPrefetchIds.clear();
  const ls = ids.map(_cardListing).filter(l => l && !l._photos && !Array.isArray(l.photos));
  if (!ls.length) return;
  try {
    let rows = [];
    const sale = l => /sale/.test(l.source || '');
    const rent = ls.filter(l => !sale(l));
    if (IS_LIVE && rent.length) {
      const r = await fetch(`/rentals/extras?ids=${encodeURIComponent(rent.map(l => l.id).join(','))}`);
      if (r.ok) rows = await r.json();
    } else if (!IS_LIVE && SupabaseClient.configured) {
      rows = await SupabaseClient.fetchByIds(ls.map(l => l.id), 'id,photos');
    }
    const by = new Map(rows.map(r => [String(r.id), r.photos]));
    for (const l of ls) {
      const p = by.get(String(l.id));
      if (Array.isArray(p)) l.photos = p;          // listingPhotos() takes it from here
    }
  } catch { /* each card falls back to its own fetch on touch */ }
  document.querySelectorAll('.card-photo[data-photo-id]').forEach(box => {
    if (!box._fill && ids.includes(box.dataset.photoId) && box.isConnected) cardPhotoFill(box);
  });
}
const _photoObserver = 'IntersectionObserver' in window ? new IntersectionObserver(entries => {
  for (const en of entries) {
    if (!en.isIntersecting) continue;
    _photoObserver.unobserve(en.target);
    if (en.target.querySelector('.card-photo-nav')) _photoPrefetchIds.add(en.target.dataset.photoId);
  }
  if (_photoPrefetchIds.size) {
    clearTimeout(_photoPrefetchTimer);
    _photoPrefetchTimer = setTimeout(_prefetchCardPhotos, 250);
  }
}, { rootMargin: '300px 0px' }) : null;
function observeCardPhotos(root = document) {
  if (!_photoObserver) return;
  root.querySelectorAll('.card-photo[data-photo-id]:not([data-observed])').forEach(box => {
    box.dataset.observed = '1';
    _photoObserver.observe(box);
  });
}
// The counter follows the strip; a touch or a hover fills a card not prefetched yet.
document.addEventListener('scroll', e => {
  const box = e.target.closest?.('.card-photo');
  if (box) _cardPhotoCount(box);
}, true);
document.addEventListener('touchstart', e => {
  const box = e.target.closest?.('.card-photo');
  if (box) cardPhotoFill(box);
}, { passive: true });
document.addEventListener('mouseover', e => {
  const box = e.target.closest?.('.card-photo');
  if (box && !box._fill) cardPhotoFill(box);
});
// Cards come and go (filters, infinite scroll): watch for new ones.
new MutationObserver(() => observeCardPhotos()).observe(document.documentElement, { childList: true, subtree: true });

let _pv = null;          // { el, photos, i, lastFocus }

function openPhotoViewer(photos, start = 0) {
  if (!photos.length) return;
  let el = document.getElementById('photoViewer');
  if (!el) {
    el = document.createElement('div');
    el.id = 'photoViewer';
    el.className = 'pv';
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-modal', 'true');
    el.innerHTML = `
      <button type="button" class="pv-btn pv-close" data-pv-close aria-label="${escapeHtml(t('pv.close'))}">${icon('x')}</button>
      <button type="button" class="pv-btn pv-prev" data-pv-step="-1" aria-label="${escapeHtml(t('pv.prev'))}">${icon('chevron-left')}</button>
      <img class="pv-img" alt="">
      <button type="button" class="pv-btn pv-next" data-pv-step="1" aria-label="${escapeHtml(t('pv.next'))}">${icon('chevron-right')}</button>
      <div class="pv-count" aria-live="polite"></div>`;
    document.body.appendChild(el);
  }
  _pv = { el, photos, i: start, lastFocus: document.activeElement };
  el.hidden = false;
  document.body.classList.add('pv-open');
  _pvShow(0);
  el.querySelector('.pv-close').focus();
}

function _pvShow(step) {
  if (!_pv) return;
  const n = _pv.photos.length;
  _pv.i = (_pv.i + step + n) % n;
  _pv.el.querySelector('.pv-img').src = _pv.photos[_pv.i];
  _pv.el.querySelector('.pv-count').textContent = `${_pv.i + 1} / ${n}`;
  _pv.el.querySelectorAll('.pv-prev, .pv-next').forEach(b => { b.hidden = n < 2; });
  // The next one is likely wanted: start loading it.
  if (n > 1) new Image().src = _pv.photos[(_pv.i + 1) % n];
}

function closePhotoViewer() {
  if (!_pv) return;
  _pv.el.hidden = true;
  document.body.classList.remove('pv-open');
  _pv.lastFocus?.focus?.();
  _pv = null;
}

document.addEventListener('click', e => {
  const t0 = e.target.closest?.('[data-photo-step], [data-pv-open], [data-pv-step], [data-pv-close], .pv');
  if (!t0) return;
  if (t0.dataset.photoStep) {
    e.preventDefault();
    cardPhotoStep(t0.closest('.card-photo'), Number(t0.dataset.photoStep));
  } else if (t0.dataset.pvOpen != null && _lmListing) {
    listingPhotos(_lmListing).then(p => openPhotoViewer(p, Number(t0.dataset.pvOpen)));
  } else if (t0.dataset.pvStep) {
    _pvShow(Number(t0.dataset.pvStep));
  } else if (t0.hasAttribute('data-pv-close') || e.target === t0) {
    closePhotoViewer();          // the close button, or the dark area around the photo
  }
});

// Keys while the viewer is open: arrows page, Escape closes it (not Details
// underneath), Tab stays inside. Capture phase, ahead of Details' handler.
window.addEventListener('keydown', e => {
  if (!_pv) return;
  if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') _pvShow(e.key === 'ArrowLeft' ? -1 : 1);
  else if (e.key === 'Escape') closePhotoViewer();
  else if (e.key === 'Tab') {
    const btns = [..._pv.el.querySelectorAll('.pv-btn:not([hidden])')];
    const at = btns.indexOf(document.activeElement);
    btns[(at + (e.shiftKey ? -1 : 1) + btns.length) % btns.length]?.focus();
  } else return;
  e.preventDefault();
  e.stopImmediatePropagation();
}, true);

// A horizontal swipe pages in the viewer. On a card the strip scrolls
// natively; a swipe that came before its photos did is replayed.
let _swipe = null;
document.addEventListener('touchstart', e => {
  const box = e.target.closest?.('.card-photo, .pv');
  if (box && e.touches.length === 1) {
    const track = box.querySelector('.card-photo-track');
    _swipe = { box, x: e.touches[0].clientX, y: e.touches[0].clientY, n: track ? track.children.length : 0 };
  }
}, { passive: true });
document.addEventListener('touchend', e => {
  if (!_swipe) return;
  const { box, x, y, n } = _swipe;
  _swipe = null;
  const dx = e.changedTouches[0].clientX - x, dy = e.changedTouches[0].clientY - y;
  if (Math.abs(dx) < 40 || Math.abs(dx) < Math.abs(dy) * 1.5) return;
  const step = dx < 0 ? 1 : -1;
  if (box.classList.contains('pv')) { _pvShow(step); return; }
  if (n < 2) cardPhotoStep(box, step);       // the strip had nothing to scroll yet
}, { passive: true });

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

// What photo_eval.py saw in the photos (scoring stamps photo_* fields).
function _lmPhotoVerdict(l) {
  if (!l.photo_deal) return '';
  const cls = { confirms: 'good', doubt: 'warn', contradicts: 'bad' }[l.photo_deal] || '';
  const note = (currentLang === 'it' ? l.photo_note_it : l.photo_note_en) || l.photo_note_it || '';
  const sc = (k, v) => v != null ? `${t(k)} ${v}/5` : null;
  const scores = [sc('photo.condition', l.photo_condition), sc('photo.kitchen', l.photo_kitchen),
                  sc('photo.bathroom', l.photo_bathroom), sc('photo.light', l.photo_light)].filter(Boolean).join(' · ');
  const flags = (l.photo_flags || []).map(f => t('photo.flag.' + f)).join(', ');
  // The user's own verdict (photo_feedback) wins; Sonnet's stays below it.
  const grade = l.photo_grade != null ? ' · ' + t('photo.grade', { v: fmtNum(l.photo_grade, l.photo_grade % 1 ? 1 : 0) }) : '';
  const head = (l.photo_by_user ? `${t('pfb.yours')}: ${t('pfb.' + l.photo_deal)}` : `Sonnet: ${t('photo.' + l.photo_deal)}`) + grade;
  const sub = l.photo_by_user ? (l.photo_user_note || '') : note;
  const sonnet = l.photo_by_user && l.photo_sonnet_deal
    ? t('pfb.sonnet_said', { v: t('photo.' + l.photo_sonnet_deal) }) + (note ? ' — ' + note : '') : '';
  return `<div class="lm-verdict lm-photo-verdict ${cls}">
      <div class="lm-verdict-head">${icon('photo')} ${escapeHtml(head)}</div>
      ${sub ? `<div class="lm-verdict-sub">${escapeHtml(sub)}</div>` : ''}
      <div class="lm-photo-meta">${sonnet ? escapeHtml(sonnet) + '<br>' : ''}${escapeHtml([scores, flags].filter(Boolean).join(' · '))}${scores || flags ? '<br>' : ''}${escapeHtml(t('photo.read', { date: _lmDate(l.photo_read_at) || '–', conf: t('photo.conf.' + (l.photo_confidence || 'low')) }))}</div>
    </div>`;
}

// What Sonnet saw that the listing doesn't say (rubric v5, 2026-10-25): its
// features and up to 8 tags from a fixed list — what an agent or a surveyor
// would point out. +1 a plus, −1 a minus.
const PHOTO_TAGS = {
  big_living: 1, big_bedroom: 1, big_kitchen: 1, night_zone_apart: 1, storage: 1, high_ceilings: 1,
  small_living: -1, small_bedroom: -1, small_bathroom: -1, walk_through_rooms: -1, long_corridor: -1,
  cramped: -1, low_ceilings: -1,
  bright: 1, big_windows: 1, open_view: 1, quiet_courtyard: 1, dual_aspect: 1,
  dark: -1, small_windows: -1, facing_wall: -1, busy_street: -1,
  new_bathroom: 1, new_kitchen: 1, recently_renovated: 1, new_windows: 1, armoured_door: 1,
  air_conditioning: 1, bathroom_window: 1,
  dated_bathroom: -1, dated_kitchen: -1, old_windows: -1, old_radiators: -1, exposed_wiring: -1,
  windowless_bathroom: -1, worn_floors: -1,
  damp: -1, cracks: -1, plan_mismatch: -1,
  modern_furniture: 1, period_features: 1, dated_furniture: -1, mismatched_furniture: -1,
  smart_entrance: 1, good_facade: 1, shabby_building: -1,
};

function _lmSeenHtml(l) {
  const tags = (l.photo_tags || []).filter(k => k in PHOTO_TAGS)
    .sort((a, b) => PHOTO_TAGS[b] - PHOTO_TAGS[a]);
  const f = l.photo_features || {};
  const val = (k, v) => v === true ? t('pfeat.' + k) : v === false ? t('pfeat.no_' + k)
                      : typeof v === 'number' ? t('pfeat.' + k + '_n', { n: v }) : t('pfeat.' + k + '.' + v);
  const feats = Object.entries(f).filter(([, v]) => v != null && v !== '').map(([k, v]) => val(k, v));
  if (!tags.length && !feats.length) return '';
  return `<div class="lm-seen">
      <div class="lm-seen-head">${icon('eye')} ${escapeHtml(t('pfeat.title'))}</div>
      ${tags.length ? `<div class="lm-seen-tags">${tags.map(k =>
        `<span class="fy-chip ${PHOTO_TAGS[k] < 0 ? 'warn' : 'good'}">${escapeHtml(t('ptag.' + k))}</span>`).join('')}</div>` : ''}
      ${feats.length ? `<div class="lm-seen-feats">${escapeHtml(feats.join(' · '))}</div>` : ''}
    </div>`;
}

// "What do you think of the photos?" — the user's verdict on what Sonnet
// read (api.py /api/photo-feedback): it applies to this listing at once
// and teaches the next readings. Local dashboard only.
const _PFB_DEALS = ['confirms', 'doubt', 'contradicts'];
function _lmPhotoFeedbackHtml(l) {
  if (!l.photo_deal || typeof IS_LIVE === 'undefined' || !IS_LIVE) return '';
  const esc = escapeHtml, cur = l.photo_by_user ? l.photo_deal : null;
  const btn = d => `<button type="button" class="seg-btn${cur === d ? ' active' : ''}" aria-pressed="${cur === d}" data-pfb="${d}" onclick="_pfbPick(this)">${esc(t('pfb.' + d))}</button>`;
  return `<div class="pfb" data-pfb-id="${esc(String(l.id))}">
      <div class="pfb-q">${esc(t('pfb.q'))}</div>
      <div class="seg pfb-seg" role="group" aria-label="${esc(t('pfb.q'))}">${_PFB_DEALS.map(btn).join('')}</div>
      <label class="pfb-grade">${esc(t('pfb.grade'))}
        <select class="input pfb-grade-sel">
          <option value="">${esc(t('pfb.grade_none'))}</option>
          ${Array.from({ length: 19 }, (_, i) => 1 + i / 2).map(g => `<option value="${g}"${l.photo_by_user && l.photo_grade === g ? ' selected' : ''}>${fmtNum(g, g % 1 ? 1 : 0)}</option>`).join('')}
        </select> / 10</label>
      <textarea class="input pfb-note" rows="2" maxlength="500" placeholder="${esc(t('pfb.note_ph'))}">${esc(l.photo_user_note || '')}</textarea>
      <div class="pfb-row">
        <button type="button" class="btn btn-secondary btn-sm" onclick="_pfbSave(this)">${esc(t('pfb.save'))}</button>
        ${cur ? `<button type="button" class="btn btn-ghost btn-sm" onclick="_pfbSave(this, true)">${esc(t('pfb.clear'))}</button>` : ''}
        <span class="pfb-status" aria-live="polite"></span>
      </div>
    </div>`;
}

function _pfbPick(b) {
  b.closest('.pfb').querySelectorAll('[data-pfb]').forEach(x => {
    const on = x === b && !x.classList.contains('active');
    x.classList.toggle('active', on);
    x.setAttribute('aria-pressed', String(on));
  });
}

async function _pfbSave(b, clear = false) {
  const box = b.closest('.pfb'), id = box.dataset.pfbId, status = box.querySelector('.pfb-status');
  const deal = clear ? null : (box.querySelector('[data-pfb].active')?.dataset.pfb || null);
  const note = clear ? '' : box.querySelector('.pfb-note').value.trim();
  const grade = clear ? null : (box.querySelector('.pfb-grade-sel')?.value || null);
  if (!clear && !deal) { status.textContent = t('pfb.pick'); return; }
  status.textContent = '…';
  try {
    const r = await fetch('/api/photo-feedback', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                   body: JSON.stringify({ id, deal, note, grade }) }).then(x => x.json());
    if (!r || r.error) throw new Error(r && r.error);
    const l = [typeof _lmListing !== 'undefined' ? _lmListing : null,
               ...(typeof ALL_RENT !== 'undefined' ? ALL_RENT : []),
               ...(typeof ALL_SALE !== 'undefined' ? ALL_SALE : [])].find(x => x && String(x.id) === id);
    if (l) {
      Object.assign(l, r.listing);
      const v = box.parentElement.querySelector('.lm-photo-verdict');
      if (v) v.outerHTML = _lmPhotoVerdict(l);
      const card = document.querySelector(`#rentGrid .card[data-listing-id="${CSS.escape(id)}"]`);
      if (card && typeof renderRentalCard === 'function') card.replaceWith(renderRentalCard(l));
    }
    if (clear) { box.querySelectorAll('[data-pfb]').forEach(x => { x.classList.remove('active'); x.setAttribute('aria-pressed', 'false'); }); box.querySelector('.pfb-note').value = ''; }
    status.textContent = t(clear ? 'pfb.cleared' : 'pfb.saved');
  } catch {
    status.textContent = t('pfb.error');
  }
}

// Rent, condo fees, heating billed apart and their total, as the listing
// states them; then what moving in takes (deposit + first month, agency
// fee aside). With no fees stated: says so, no total.
function _lmCostTable(l) {
  const esc = escapeHtml, c = monthlyCost(l);
  const row = (k, v, cls = '') => `<tr class="${cls}"><td>${esc(k)}</td><td>${esc(v)}</td></tr>`;
  const cap = s => s.charAt(0).toUpperCase() + s.slice(1);
  let rows = row(cap(t('cost.rent')), `€${fmt(c.rent)}`);
  rows += row(cap(t('cost.condo')), c.included ? t('cost.condo_in_rent') : c.condo ? `€${fmt(c.condo)}`
              : c.suspect ? t('cost.suspect_row', { v: fmt(c.statedCondo) }) : t('cost.not_stated'),
              c.condo || c.included ? '' : 'is-muted');
  if (c.heating) rows += row(`${cap(t('cost.heating'))} ${t('cost.heating_year', { v: fmt(c.heating * 12) })}`, `€${fmt(c.heating)}`);
  if (c.known) rows += row(t('cost.total'), `€${fmt(c.total)}`, 'is-total');
  const dep = l.deposit_months ? l.deposit_months * c.rent : (Number(l.deposit_eur) || 0);
  if (dep) {
    const label = l.deposit_months ? t('cost.entry', { n: l.deposit_months }) : t('cost.entry_eur');
    rows += row(`${label} · ${t('cost.no_agency')}`, `€${fmt(dep + c.total)}`, 'is-entry');
  }
  const note = !c.known ? `<div class="lm-muted">${esc(c.suspect ? t('cost.suspect_long', { v: fmt(c.statedCondo) }) : t(c.pageRead ? 'cost.ask_long' : 'cost.pending_long'))}</div>` : '';
  return `<table class="lm-costs">${rows}</table>${note}`;
}

// Links to the other copies of the same flat (_dedupListings → also_on).
function _lmAlsoOn(l) {
  const seen = new Set();
  const links = (l.also_on || []).filter(a => a.url && !seen.has(a.url) && seen.add(a.url)).map(a =>
    `<a href="${_safeUrl(a.url)}" target="_blank" rel="noopener">${escapeHtml(siteName(a.source))}${a.price && a.price !== (l.rent_mo ?? l.price) ? ' €' + fmt(a.price) : ''}${a.private && !isPrivate(l) ? ' (' + escapeHtml(t('card.private').toLowerCase()) + ')' : ''}</a>`);
  return links.length ? ' · ' + escapeHtml(t('lm.also_on')) + ' ' + links.join(', ') : '';
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
  const site = siteName(l.source);
  const subito = l.source === 'subito', casait = l.source === 'casait';
  const provenance = subito ? t('lm.subito_read')
    : l.detail_fetched_at ? t('lm.page_read', { date: _lmDate(l.detail_fetched_at) })
    : casait ? t('lm.casait_read') : t('lm.page_not_read');
  // Subito / Casa.it listings without the site's own pin are placed from their text.
  const approx = { street: 'lm.geo_street', metro: 'lm.geo_metro', zone: 'lm.geo_zone' }[l.geo_precision];
  const unplaced = (subito || casait) && l.latitude == null;
  const fav = isRentFav(l.id);
  const id = esc(String(l.id));
  const method = compsMethodTip(l);

  return `
  <div class="lm-head">
    <div class="lm-title">
      <h2 id="lmTitle">${esc([formatAddress(l.neighbourhood), l.address ? formatAddress(l.address) : ''].filter(Boolean).join(' · ')
                              || (l.title || '').trim() || '–')}</h2>
      <div class="lm-sub">${esc(site)}${isPrivate(l) ? ' · ' + esc(t('card.private')) : ''}${_lmAlsoOn(l)} · ID ${id}${l.first_seen_date ? ' · ' + esc(t('lm.first_seen', { date: _lmDate(l.first_seen_date) })) : ''}</div>
      ${_longOnlineText(l) ? `<div class="lm-cap lm-online">${icon('clock')} ${esc(_longOnlineText(l))} — ${esc(t('card.online_ask'))}</div>` : ''}
    </div>
    <button type="button" class="btn btn-secondary btn-icon lm-close" aria-label="${esc(t('lm.close'))}">${icon('x')}</button>
  </div>

  <div class="lm-scroll">
    <div class="lm-photos" id="lmPhotos"></div>

    <div class="lm-verdict ${v.cls}">
      <div class="lm-verdict-head">${esc(v.head)}</div>
      ${v.sub ? `<div class="lm-verdict-sub">${esc(v.sub)}</div>` : ''}
    </div>
    ${_lmPhotoVerdict(l)}
    ${_lmSeenHtml(l)}
    ${_lmPhotoFeedbackHtml(l)}

    <div class="lm-money">
      <div><span class="lm-rent">€${fmt(monthlyCost(l).total)}</span><span class="lm-muted">${esc(t(monthlyCost(l).known ? 'cost.all_in' : 'cost.rent_only'))}</span></div>
      ${_lmCostTable(l)}
      ${l.ask_psqm ? `<div class="lm-muted">€${fmt1(l.ask_psqm)}/m²${l.comps_median ? ' · ' + esc(t('lm.nearby_median', { v: fmt1(l.comps_median) })) : ''}</div>` : ''}
      ${history ? `<div class="lm-history">${icon('trending-down')}${esc(history)}</div>` : ''}
    </div>

    <h3 class="lm-section-title">${esc(t('lm.facts'))}</h3>
    <div class="lm-facts">
      ${known.map(([k, val]) => `<div class="lm-fact"><div class="lm-fact-k">${esc(k)}</div><div class="lm-fact-v">${esc(String(val))}</div></div>`).join('')}
    </div>
    ${unknown.length ? `<div class="lm-unknown-line">${esc(t('lm.not_known_list', { list: unknown.join(', ') }))}</div>` : ''}
    <div class="lm-note">${icon(l.detail_fetched_at || subito ? 'check' : 'clock')}${esc(provenance)}${(l.twin_filled || []).length
        ? ' · ' + esc(t('card.twin', { site: l.source === 'idealista' ? 'Immobiliare.it' : 'Idealista' })) : ''}</div>

    <div class="lm-two">
      <section>
        <h3 class="lm-section-title">${esc(t('lm.location'))}</h3>
        ${lmMapSnipHtml(l)}
        ${approx || unplaced ? `<div class="lm-muted">${esc(t(unplaced ? 'lm.geo_none' : approx))}</div>` : ''}
        <ul class="lm-near">
          ${near('train', metroLine, l.geo_precision === 'zone' ? t('lm.from_zone_centre') : l.geo_precision === 'metro' ? t('lm.metro_named') : l.metro_walk_min != null ? t('lm.walk', { n: l.metro_walk_min }) : dist(l.metro_nearest_dist_m))}
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
