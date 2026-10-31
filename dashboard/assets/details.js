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
  box.classList.remove('is-past-sum');
  box.hidden = false;
  document.body.classList.add('lm-open');
  box.querySelector('.lm-close')?.focus();
  lmLoadMapSnip(l);
  _lmAfterRender(l, 'rent');
  _lmEnsureFull(l).then(() => {
    if (_lmListing !== l || box.hidden) return;
    _lmLoadPhotos(l);
    const reasons = document.getElementById('lmReasons');
    if (reasons) reasons.innerHTML = reasonsBulletsHtml(l);
    // The description arrives with the rest: add it if it wasn't there.
    if (l.description && !document.getElementById('lmDesc')) {
      document.querySelector('#listingModal .lm-know')?.insertAdjacentHTML('afterend', _lmDescHtml(l));
      _lmAfterRender(l, 'rent');
    }
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
  // A tap opens the viewer at that photo; floor plans come after the photos;
  // "+N" leads to the rest on the site.
  const plans = (l.floorplans || []).filter(Boolean);
  if (!photos.length && !plans.length) return;
  l._photos = [...photos, ...plans];
  box.innerHTML = photos.map((u, i) => `<button type="button" class="lm-hero-slide" data-pv-open="${i}" aria-label="${escapeHtml(t('pv.open', { i: i + 1, n: photos.length }))}">
        <img src="${_safeUrl(u)}" alt=""${i ? ' loading="lazy"' : ''}></button>`).join('')
    + plans.map((u, j) => `<button type="button" class="lm-hero-slide is-plan" data-pv-open="${photos.length + j}" aria-label="${escapeHtml(t('lm.plan'))}">
        <img src="${_safeUrl(u)}" alt="" loading="lazy"><span class="lm-plan-tag">${escapeHtml(t('lm.plan'))}</span></button>`).join('')
    + (l.photo_count > photos.length ? `<a class="lm-hero-slide lm-hero-more" href="${_safeUrl(l.url)}" target="_blank" rel="noopener">${escapeHtml(t('lm.more_photos', { n: l.photo_count - photos.length }))} ↗</a>` : '');
  const count = document.querySelector('#lmHero .lm-hero-count'), n = photos.length + plans.length;
  const upd = () => { if (!count || n < 2) return; const i = Math.round(box.scrollLeft / Math.max(1, box.clientWidth)); count.textContent = i < photos.length ? `${i + 1}/${photos.length}` : t('lm.plan'); count.hidden = false; };
  box.addEventListener('scroll', upd, { passive: true });
  upd();
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
  const now = l.rent_mo ?? l.price;
  if (l.previous_price && now && l.previous_price > now) {
    return t('lm.price_dropped', { was: fmt(l.previous_price), date: _lmDate(l.price_changed_date) || '' });
  }
  if (l.relisted_from_rent) {
    return t('lm.relisted', { was: fmt(l.relisted_from_rent), date: _lmDate(l.relisted_from_date) || '' });
  }
  return null;
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
      const know = document.querySelector('#listingModal .lm-know');
      if (know) know.outerHTML = _lmKnowHtml(l, l.rent_mo != null ? 'rent' : 'sale');
      const st = document.getElementById('lmPhotoState');
      if (st) st.textContent = ' · ' + t('lm.your_verdict', { v: l.photo_by_user ? t('pfb.' + l.photo_deal) : t('pfb.none') });
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

// ── Details sheet, shared by rentals and sales (2026-10-30 redesign) ─────────
// From a product-design review against Rightmove, Zillow, Redfin, Funda and
// Airbnb: Details starts where the card ended — the card's own rows, larger —
// then one "What to know" box, the listing's own words, the money, the
// price vs nearby, the flat, the location; reference material (score maths,
// comparables, photo-reading details) folded. Each thing said once.

// Summary: price + ring, money line, price history, facts, owning (sale), metro.
function _lmSummaryHtml(l, kind) {
  const esc = escapeHtml;
  let top, money, own = '';
  if (kind === 'sale') {
    const psqm = l.ask_psqm || (l.sqm ? Math.round(l.price / l.sqm) : null);
    const vs = _vsNearbyHtml(l.comps_sale_delta_pct, t('sale.delta_tip', { n: l.comps_sale_n || 0 }));
    top = `<span class="lm-sum-price">€${fmt(l.price)}</span>`;
    money = psqm ? `<div class="card-money"><span>€${fmt(Math.round(psqm))}/m²</span>${vs ? '<i aria-hidden="true">·</i>' + vs : ''}</div>` : '';
    own = _ownLineHtml(l, saleFilters());
  } else {
    const c = monthlyCost(l);
    top = `<span class="lm-sum-price">€${fmt(c.total)}</span><span class="lm-sum-unit">${esc(t(c.known ? 'cost.all_in' : 'cost.rent_only'))}</span>`;
    money = _rentMoneyHtml(c, l);
  }
  const hist = priceHistoryText(l);
  return `<div class="lm-sum" id="lmSum">
    <div class="lm-sum-top">
      <div class="lm-sum-main"><div>${top}</div>${money}</div>
      <button type="button" class="lm-ring-btn" data-lm-goto="lmScoreFold" aria-label="${esc(t('lm.why_score', { n: l.score_total ?? '–' }))}">${scoreRingHtml(l, 48, { value: l.score_total ?? 0, caption: t('ring.score') })}</button>
    </div>
    ${hist ? `<div class="lm-sum-hist">${esc(hist)}</div>` : ''}
    ${_factsHtml(l)}
    ${own}
    ${_metroFootHtml(l) ? `<div class="lm-sum-metro">${_metroFootHtml(l)}</div>` : ''}
  </div>`;
}

// The sticky header's short form once the summary has scrolled away.
function _lmMiniText(l, kind) {
  const c = kind === 'sale' ? null : monthlyCost(l);
  const price = kind === 'sale' ? `€${fmt(l.price)}` : `€${fmt(c.total)}${t('lm.per_mo')}`;
  const bits = [l.rooms != null ? (l.rooms === 1 ? t('card.studio') : t('card.rooms', { n: l.rooms })) : null,
                l.sqm ? `${fmt(l.sqm)} m²` : null, _floorLabel(l)].filter(Boolean);
  return `<b>${escapeHtml(price)}</b><span> · ${escapeHtml(bits.join(' · '))}</span>`;
}

// "What to know": the verdict in words and its basis, the photos, the
// pluses and minuses, a private landlord, at most two cautions — replaces
// the price-verdict, photo-verdict and "Seen in the photos" boxes.
function _lmKnowHtml(l, kind) {
  const esc = escapeHtml, rows = [];
  const row = (lead, tone, rest, id = '') =>
    `<div class="lm-know-row"${id ? ` id="${id}"` : ''}><b class="lm-know-lead tone-${tone}">${esc(lead)}</b>${rest ? ' ' + rest : ''}</div>`;
  // 1. Price.
  const dp = kind === 'sale' ? l.comps_sale_delta_pct : l.comps_delta_pct;
  const n = kind === 'sale' ? l.comps_sale_n : l.comps_n;
  const m = kind === 'sale' ? l.comps_sale_radius_m : l.comps_radius_m;
  const confKey = String((kind === 'sale' ? l.comps_sale_conf_label : l.comps_conf_label) || '').toLowerCase();
  let lead, tone;
  if (kind !== 'sale' && l.price_suspect) { lead = t('know.check_price'); tone = 'warn'; }
  else if (l.hidden_gem) { lead = t('gem.hidden'); tone = 'good'; }
  else if (l.good_value) { lead = t('gem.good'); tone = 'good'; }
  else if (dp == null) { lead = t('know.no_comp'); tone = 'muted'; }
  else if (dp <= -5) { lead = t('know.good_price'); tone = 'good'; }
  else if (dp >= 5) { lead = t('know.above'); tone = 'bad'; }
  else { lead = t('know.like_nearby'); tone = 'muted'; }
  let basis;
  if (kind !== 'sale' && l.price_suspect) basis = t('card.suspect');
  else if (dp == null) basis = t(kind === 'sale' ? 'sale.no_comps' : 'lm.no_comps');
  else {
    basis = m ? t(kind === 'sale' ? 'know.basis_sale_m' : 'know.basis', { n: fmt(n || 0), m: fmt(Math.round(m)) })
              : t(kind === 'sale' ? 'know.basis_sale' : 'know.basis_nom', { n: fmt(n || 0) });
    const gv = scoreSettings.rent_gv_z_max ?? -1.1;
    if (kind !== 'sale' && dp < 0 && l.price_z != null) basis += '; ' + t(l.price_z <= gv ? 'lm.strong_discount' : 'lm.normal_discount');
    if (confKey === 'medium') basis += '; ' + t('know.fair_comp');
    else if (confKey === 'low') basis += '; ' + t('know.rough');
  }
  rows.push(row(lead, tone, esc(basis)));
  // 2. Photos.
  if (l.photo_deal) {
    const g = l.photo_grade;
    const ptone = g != null ? (g >= 7 ? 'good' : g <= 4.5 ? 'bad' : 'warn') : ({ confirms: 'good', doubt: 'warn', contradicts: 'bad' }[l.photo_deal] || 'muted');
    const plead = l.photo_by_user ? t('know.your_photos', { v: t('pfb.' + l.photo_deal) })
                : g != null ? t('know.photos_g', { g: fmtNum(g, g % 1 ? 1 : 0) }) : t('know.photos');
    const note = l.photo_by_user ? (l.photo_user_note || '') : ((currentLang === 'it' ? l.photo_note_it : l.photo_note_en) || l.photo_note_it || '');
    rows.push(row(plead, ptone, `<span class="lm-know-clamp">${esc(t('photo.' + l.photo_deal) + (note ? ' — ' + note : ''))}</span>`, 'lmKnowPhotos'));
  }
  // 3. Pluses and minuses from the photos.
  const tags = (l.photo_tags || []).filter(k => k in PHOTO_TAGS);
  const plus = tags.filter(k => PHOTO_TAGS[k] > 0).slice(0, 6), minus = tags.filter(k => PHOTO_TAGS[k] < 0).slice(0, 4);
  if (plus.length || minus.length) {
    rows.push(`<div class="lm-know-row">${plus.length ? `<b class="lm-know-lead tone-good">${esc(t('know.pluses'))}</b> ${esc(plus.map(k => t('ptag.' + k)).join(', '))}` : ''}${
      plus.length && minus.length ? ' <i class="lm-dot">·</i> ' : ''}${minus.length ? `<b class="lm-know-lead tone-warn">${esc(t('know.minuses'))}</b> ${esc(minus.map(k => t('ptag.' + k)).join(', '))}` : ''}</div>`);
  }
  // 4. A private landlord.
  if (kind !== 'sale' && (isPrivate(l) || privateCopySite(l))) {
    rows.push(row(t('know.private'), 'good', esc(isPrivate(l) ? t('know.no_fee') : t('card.private_on', { site: privateCopySite(l) }))));
  }
  // 5. Cautions, at most two.
  const cautions = [];
  const d = daysOnline(l);
  if (d != null && d > (kind === 'sale' ? 180 : LONG_ONLINE_DAYS))
    cautions.push(row(t('card.online_months', { n: fmt(Math.floor(d / 30)) }), 'warn', esc(t('card.online_ask'))));
  const approx = { street: 'lm.geo_street', metro: 'lm.geo_metro', zone: 'lm.geo_zone' }[l.geo_precision];
  if (l.latitude == null && (l.source === 'subito' || l.source === 'casait')) cautions.push(row(t('know.no_pos'), 'warn', esc(t('lm.geo_none'))));
  else if (approx) cautions.push(row(t('know.approx'), 'warn', esc(t(approx).split(/ — |: /)[0].replace(/^[^:]*:\s*/, ''))));
  if (l.score_cap_reason) cautions.push(row(t('know.capped'), 'warn', esc(l.score_cap_reason)));
  if (l.is_stale) cautions.push(row(t('know.stale'), 'bad', ''));
  rows.push(...cautions.slice(0, 2));
  return `<div class="lm-know">${rows.join('')}</div>`;
}

// The listing's own words: ** stripped, shouted paragraphs sentence-cased.
function _lmDescClean(s) {
  return String(s || '').replace(/\*\*|__/g, '').split('\n').map(x => x.trim()).join('\n')
    .replace(/\n{3,}/g, '\n\n').split('\n\n').map(p => {
      const letters = p.replace(/[^A-Za-zÀ-ÿ]/g, '');
      if (letters.length < 20 || letters.replace(/[^A-ZÀ-Þ]/g, '').length < 0.8 * letters.length) return p;
      return p.toLowerCase().replace(/(^|[.!?]\s+)([a-zà-ÿ])/g, (m, a, b) => a + b.toUpperCase());
    }).join('\n\n').trim();
}

function _lmDescHtml(l) {
  const text = _lmDescClean(l.description);
  if (!text) return '';
  const snippet = /^idealista/.test(l.source || '') && text.length <= 330;
  return `<section class="lm-sec lm-desc" id="lmDesc">
    <h3 class="lm-sec-title">${escapeHtml(t('lm.from_listing'))}</h3>
    <div class="lm-desc-text" lang="it">${escapeHtml(text)}${snippet ? '…' : ''}</div>
    ${snippet ? `<a class="btn-link lm-desc-more" href="${_safeUrl(l.url)}" target="_blank" rel="noopener">${escapeHtml(t('lm.read_on', { site: siteName(l.source) }))} ↗</a>`
              : `<button type="button" class="btn-link lm-desc-toggle" data-lm-desc hidden>${escapeHtml(t('lm.read_all'))}</button>`}
  </section>`;
}

// A clean condition / energy class, or null for junk ("Bilocale", "8").
function _cleanCondition(v) {
  const s = String(v || '').toLowerCase();
  if (!s) return null;
  if (/da ristruttur/.test(s)) return { v: t('cond.to_renovate'), tone: 'warn' };
  if (/ottim|ristruttur/.test(s)) return { v: t('cond.renovated') };
  if (/nuov|costruz/.test(s)) return { v: t('cond.new') };
  if (/buon|abitabil|normale/.test(s)) return { v: t('cond.good') };
  return null;
}
function _cleanEnergy(v) {
  const s = String(v || '').trim().toUpperCase();
  return /^(A[1-4]|A\+{0,4}|[A-G])$/.test(s) ? s : null;
}

// The flat: label–value rows the summary doesn't already show.
function _lmFactsListHtml(l, kind) {
  const esc = escapeHtml;
  const yesNo = v => v === true ? t('lm.yes') : v === false ? t('lm.no') : null;
  const heat = { autonomous: t('lm.heat_own'), centralised: t('lm.heat_central'), none: t('lm.heat_none') };
  const cond = _cleanCondition(l.condition), en = _cleanEnergy(l.energy_class);
  const rows = [
    (l.rooms || 0) <= 2 && l.bedrooms != null ? [t('lm.bedrooms_k'), l.bedrooms] : null,
    l.bathrooms === 1 ? [t('lm.bathrooms'), 1] : null,
    [t('lm.condition'), cond?.v, cond?.tone],
    kind === 'sale' ? null : [t('must.furnished'), yesNo(l.furnished)],
    [t('lm.heating'), l.heating_type ? heat[l.heating_type] ?? null : null],
    [t('lm.energy'), en, en === 'G' ? 'warn' : ''],
    [t('must.balcony'), yesNo(l.has_balcony)],
    [t('must.parking'), yesNo(l.has_parking)],
    l.air_conditioning ? [t('lm.ac'), t('lm.yes')] : null,
    [t('lm.exposure'), l.is_external === true ? t('lm.external') : l.is_external === false ? t('lm.internal') : null],
    [t('lm.year'), l.year_built],
    kind === 'sale' ? null : [t('lm.contract'), l.contract_type],
    [t('lm.availability'), /^libero$/i.test(l.availability || '') ? t('lm.free_now') : l.availability],
  ].filter(Boolean);
  const known = rows.filter(([, v]) => v != null && v !== '');
  const watch = [t('lm.bathrooms'), t('lm.condition'), t('lm.heating'), t('lm.energy'), t('must.balcony'), t('must.parking'), t('lm.year')]
    .concat(kind === 'sale' ? [] : [t('must.furnished')]);
  const unknown = watch.filter(k => !known.some(([kk]) => kk === k) && !(k === t('lm.bathrooms') && l.bathrooms >= 2));
  const read = !!l.detail_fetched_at || l.source === 'subito';
  return `<section class="lm-sec" id="lmFlat">
    <h3 class="lm-sec-title">${esc(t('lm.the_flat'))}</h3>
    ${known.length ? `<dl class="lm-dl">${known.map(([k, v, tone]) => `<div class="lm-dl-row"><dt>${esc(k)}</dt><dd${tone ? ` class="tone-${tone}"` : ''}>${esc(String(v))}</dd></div>`).join('')}</dl>` : ''}
    ${unknown.length ? `<div class="lm-dl-unk">${icon('circle-dashed')}<span>${esc(t(read ? 'lm.not_stated_list' : 'lm.not_read_list', { list: unknown.join(', ').toLowerCase() }))}</span></div>` : ''}
    ${(l.twin_filled || []).length ? `<div class="lm-fine">${esc(t('card.twin', { site: l.source === 'idealista' ? 'Immobiliare.it' : 'Idealista' }))}</div>` : ''}
  </section>`;
}

// Price vs nearby: one gauge for both sheets (2026-10-30; a number line with
// a band, a tick and a dashed OMI line read as noise). The verdict comes
// first, then a five-step bar centred on the nearby median, much lower …
// much higher, with this flat's pin on it; the steps break at ±5 % (the
// card's tone) and ±15 %, the outer ones run to ±40 %. OMI is one line of
// text under it, since it is a cross-check, not what the score uses.
const _LM_STEPS = ['lm.much_lower', 'lm.lower', 'lm.in_line', 'lm.higher', 'lm.much_higher'];
function _lmGaugePos(dp) {
  const seg = (lo, hi, at) => at + Math.max(0, Math.min(1, (dp - lo) / (hi - lo))) * 20;
  return dp < -15 ? seg(-40, -15, 0) : dp < -5 ? seg(-15, -5, 20) : dp < 5 ? seg(-5, 5, 40)
       : dp < 15 ? seg(5, 15, 60) : seg(15, 40, 80);
}

function _lmScaleHtml(l, kind) {
  const esc = escapeHtml, sale = kind === 'sale';
  // What the score compares: a sale's asking €/m²; a rental's rent plus condo
  // fees (capped at 20 % of the rent, as scoring.compute_effective_psqm).
  const condo = sale ? 0 : Math.min(+(l.spese_condominiali || l.condominium_fees || 0), (l.rent_mo || 0) * 0.2);
  const psqm = sale ? (l.ask_psqm || (l.sqm ? l.price / l.sqm : null))
             : (l.rent_mo && l.sqm ? (l.rent_mo + condo) / l.sqm : l.ask_psqm);
  const med = sale ? l.comps_sale_median : l.comps_median;
  const lo = sale ? l.omi_compr_min : l.omi_loc_min, hi = sale ? l.omi_compr_max : l.omi_loc_max;
  const f = v => sale ? fmt(Math.round(v)) : fmt1(v);
  if (!psqm) return '';
  let dp = sale ? l.comps_sale_delta_pct : l.comps_delta_pct;
  if (dp == null && med) dp = (psqm / med - 1) * 100;
  // OMI rents leave condo fees out, so a rental is held against them by rent alone.
  const own = sale ? psqm : (l.ask_psqm || psqm);
  const omi = lo && hi ? `<p class="lm-gauge-omi">${esc(t('lm.omi_line', { a: f(lo), b: f(hi) }))}${
    esc(t(own < lo ? 'lm.omi_below' : own > hi ? 'lm.omi_above' : 'lm.omi_in'))}</p>` : '';
  const head = `<b>€${f(psqm)}/m²</b>${condo > 0 ? `<small>${esc(t('lm.incl_condo'))}</small>` : ''}`;
  if (dp == null) return `<div class="lm-gauge-head">${head}</div>${omi}`;
  const tone = dp <= -5 ? 'good' : dp >= 5 ? 'bad' : '';
  const step = dp < -15 ? 0 : dp < -5 ? 1 : dp < 5 ? 2 : dp < 15 ? 3 : 4;
  const verdict = Math.abs(dp) < 1 ? t('card.in_line')
    : t(dp < 0 ? 'card.below_nearby' : 'card.above_nearby', { pct: fmt(Math.round(Math.abs(dp))) });
  // The centre is the benchmark the delta was taken against (nearby flats
  // sized like this one; for sales weighed with OMI), so the two numbers
  // always agree with the percentage — the raw median often doesn't.
  const centre = psqm / (1 + dp / 100);
  return `<div class="lm-gauge-head">${head}<span class="${tone ? 'tone-' + tone : ''}">${esc(verdict)}</span></div>
    <div class="lm-gauge" role="img" aria-label="${esc(verdict)}">
      ${_LM_STEPS.map((k, i) => `<span class="lm-gauge-seg s${i}"></span>`).join('')}
      <span class="lm-gauge-pin ${tone ? 'tone-' + tone : ''}" style="left:${_lmGaugePos(dp).toFixed(1)}%"></span>
    </div>
    <div class="lm-gauge-steps">${_LM_STEPS.map((k, i) => `<span${i === step ? ' class="is-here"' : ''}>${esc(t(k))}</span>`).join('')}</div>
    <p class="lm-gauge-med">${esc(sale && med && Math.abs(centre / med - 1) > 0.02
      ? t('lm.gauge_centre_omi', { v: f(centre), m: f(med) }) : t('lm.gauge_centre', { v: f(centre) }))}</p>
    ${omi}`;
}

function _lmPriceVsHtml(l, kind) {
  const esc = escapeHtml, sale = kind === 'sale';
  const scale = _lmScaleHtml(l, kind);
  const method = sale ? t('sale.delta_tip', { n: l.comps_sale_n || 0 }) : compsMethodTip(l);
  const n = sale ? 0 : (l.comps_n || 0);
  if (!scale && !method) return '';
  return `<section class="lm-sec" id="lmVs">
    <h3 class="lm-sec-title">${esc(t('lm.price_vs'))}</h3>
    ${scale}
    ${method ? `<p class="lm-note">${icon('info-circle')}<span>${esc(method)}</span></p>` : ''}
    ${n > 0 ? `<details class="lm-fold lm-fold-inner" id="lmCompsFold"><summary>${esc(t('lm.see_comps', { n: fmt(n) }))}${icon('chevron-down')}</summary>
      <div class="lm-comps" id="lmComps"></div></details>` : ''}
  </section>`;
}

// Location: the map, then the nearby list (metro lines from the station table).
function _lmLocationHtml(l) {
  const esc = escapeHtml;
  const dist = m => m != null ? `${fmt(Math.round(m))} m` : null;
  const near = (ic, label, val) => val == null ? '' : `<li>${icon(ic)}<span>${label}</span><span class="lm-muted">${esc(val)}</span></li>`;
  const lines = l.metro_nearest_name ? _stationLines(l.metro_nearest_name).map(x => `<b class="mline" style="--ml:${_MLINE[x]}"${x === 'M3' ? ' data-dark' : ''}>${x}</b>`).join('') : '';
  const metroLabel = l.metro_nearest_name ? `${lines}${esc(l.metro_nearest_name)}` : esc(t('lm.metro'));
  const metroVal = l.geo_precision === 'zone' ? t('lm.from_zone_centre') : l.geo_precision === 'metro' ? t('lm.metro_named')
                 : l.metro_walk_min != null ? t('lm.walk', { n: l.metro_walk_min }) : dist(l.metro_nearest_dist_m);
  const approx = l.geo_precision && l.geo_precision !== 'map' && l.latitude != null;
  const snip = lmMapSnipHtml(l);
  return `<section class="lm-sec" id="lmLoc">
    <h3 class="lm-sec-title">${esc(t('lm.location'))}</h3>
    ${snip ? `<div class="lm-map-wrap">${snip.replace(/<a class="lm-maplink"[\s\S]*<\/a>/, '')}${approx ? `<span class="lm-map-approx">${esc(t('know.approx'))}</span>` : ''}</div>` : ''}
    <ul class="lm-near">
      ${near('train', metroLabel, metroVal)}
      ${near('bus', esc(t('lm.tram')), dist(l.tram_nearest_dist_m))}
      ${near('shopping-cart', esc(t('lm.supermarket')), dist(l.supermarket_nearest_dist_m))}
      ${near('trees', esc(t('lm.park')), dist(l.park_nearest_dist_m))}
      ${near('school', esc(t('lm.university')), dist(l.university_nearest_dist_m))}
    </ul>
    ${snip ? (snip.match(/<a class="lm-maplink"[\s\S]*<\/a>/) || [''])[0] : ''}
  </section>`;
}

// Why it scores N — folded; the ring at the top opens it.
function _lmScoreFoldHtml(l) {
  const esc = escapeHtml;
  const w = { price: scoreSettings.w_price ?? 0.45, prop: scoreSettings.w_property ?? 0.25,
              loc: scoreSettings.w_location ?? 0.20, pen: scoreSettings.w_penalty ?? 0.10 };
  const bar = (k, val, wt) => `<div class="lm-bar sc-${scoreClass(val ?? 50)}"><span class="lm-bar-k">${esc(t(k))} <span class="lm-muted">· ${esc(t('lm.counts', { p: Math.round(wt * 100) }))}</span></span>
      <span class="lm-bar-track"><span class="lm-bar-fill" style="width:${Math.max(0, Math.min(100, val ?? 50))}%"></span></span>
      <span class="lm-bar-v">${val ?? '–'}</span></div>`;
  const reasons = typeof reasonsBulletsHtml === 'function' ? reasonsBulletsHtml(l) : '';
  return `<details class="lm-fold" id="lmScoreFold"><summary>${esc(t('lm.why_score', { n: l.score_total ?? '–' }))}${icon('chevron-down')}</summary>
    <div class="lm-fold-body">
      ${bar('lm.price', l.score_price, w.price)}
      ${bar('lm.location', l.score_location ?? l.geo_score, w.loc)}
      ${bar('lm.property', l.score_property, w.prop)}
      ${bar('lm.flags_free', l.score_penalty, w.pen)}
      ${l.score_cap_reason ? `<div class="lm-cap">${esc(t('card.cap', { reason: l.score_cap_reason }))}</div>` : ''}
      <div id="lmReasons">${reasons}</div>
    </div></details>`;
}

// About the photo reading — folded: sub-scores, flags, features, the
// remaining tags, the reading date and (locally) the user's verdict form.
function _lmPhotoFoldHtml(l) {
  if (!l.photo_deal) return '';
  const esc = escapeHtml;
  const sc = (k, v) => v != null ? `${t(k)} ${v}/5` : null;
  const scores = [sc('photo.condition', l.photo_condition), sc('photo.kitchen', l.photo_kitchen),
                  sc('photo.bathroom', l.photo_bathroom), sc('photo.light', l.photo_light)].filter(Boolean).join(' · ');
  const flags = (l.photo_flags || []).map(f => t('photo.flag.' + f)).join(', ');
  const f = l.photo_features || {};
  const val = (k, v) => v === true ? t('pfeat.' + k) : v === false ? t('pfeat.no_' + k)
                      : typeof v === 'number' ? t('pfeat.' + k + '_n', { n: v }) : t('pfeat.' + k + '.' + v);
  const feats = Object.entries(f).filter(([, v]) => v != null && v !== '').map(([k, v]) => val(k, v)).join(' · ');
  const tags = (l.photo_tags || []).filter(k => k in PHOTO_TAGS);
  const extra = [...tags.filter(k => PHOTO_TAGS[k] > 0).slice(6), ...tags.filter(k => PHOTO_TAGS[k] < 0).slice(4)].map(k => t('ptag.' + k)).join(', ');
  const note = (currentLang === 'it' ? l.photo_note_it : l.photo_note_en) || l.photo_note_it || '';
  const sonnet = l.photo_by_user && l.photo_sonnet_deal ? t('pfb.sonnet_said', { v: t('photo.' + l.photo_sonnet_deal) }) + (note ? ' — ' + note : '') : '';
  const state = l.photo_by_user ? t('pfb.' + l.photo_deal) : t('pfb.none');
  return `<details class="lm-fold" id="lmPhotoFold"><summary><span>${esc(t('lm.photo_reading'))}${typeof IS_LIVE !== 'undefined' && IS_LIVE ? `<span class="lm-muted" id="lmPhotoState"> · ${esc(t('lm.your_verdict', { v: state }))}</span>` : ''}</span>${icon('chevron-down')}</summary>
    <div class="lm-fold-body lm-photo-meta">
      ${[scores, flags, feats, extra, sonnet].filter(Boolean).map(x => `<div>${esc(x)}</div>`).join('')}
      <div class="lm-muted">${esc(t('photo.read', { date: _lmDate(l.photo_read_at) || '–', conf: t('photo.conf.' + (l.photo_confidence || 'low')) }))}</div>
      ${_lmPhotoFeedbackHtml(l)}
    </div></details>`;
}

// About this listing: site, agency, ids and dates, what was read, collections.
function _lmAboutHtml(l, kind) {
  const esc = escapeHtml, id = esc(String(l.id));
  const subito = l.source === 'subito', casait = l.source === 'casait';
  const provenance = subito ? t('lm.subito_read')
    : l.detail_fetched_at ? t('lm.page_read', { date: _lmDate(l.detail_fetched_at) })
    : casait ? t('lm.casait_read') : t('lm.page_not_read');
  const dates = [`ID ${id}`, l.published_date ? t('lm.online_since', { d: _lmDate(l.published_date) }) : null,
                 l.first_seen_date ? t('lm.first_seen', { date: _lmDate(l.first_seen_date) }) : null].filter(Boolean).join(' · ');
  return `<section class="lm-sec lm-about" id="lmAbout">
    <h3 class="lm-sec-title">${esc(t('lm.about'))}</h3>
    <div>${esc(siteName(l.source))}${kind !== 'sale' && isPrivate(l) ? ' · ' + esc(t('card.private').toLowerCase()) : ''}${_lmAlsoOn(l)}</div>
    ${l.agency_name ? `<div>${esc(t('lm.agency', { v: l.agency_name }))}</div>` : ''}
    <div>${dates}</div>
    <div class="lm-note">${icon(l.detail_fetched_at || subito ? 'check' : 'clock')}<span>${esc(provenance)}</span></div>
    <button type="button" class="btn btn-secondary btn-sm lm-col-btn" data-lm-collections="${id}" data-lm-kind="${kind === 'sale' ? 'sale' : 'rent'}" aria-expanded="false" aria-controls="lmCollections">${icon('folder')}<span>${esc(_lmColLabel(l.id, kind === 'sale' ? 'sale' : 'rent'))}</span></button>
    <div class="lm-collections" id="lmCollections" data-kind="${kind === 'sale' ? 'sale' : 'rent'}" hidden></div>
  </section>`;
}

// One footer row: ♡, Share, and Open — the only filled button.
function _lmActionsHtml(l, kind) {
  const esc = escapeHtml, id = esc(String(l.id)), site = siteName(l.source);
  const fav = kind === 'sale' ? isSaleFav(l.id) : isRentFav(l.id);
  const favBtn = kind === 'sale'
    ? `<button type="button" class="btn btn-secondary btn-icon btn-fav${fav ? ' is-fav' : ''}" data-sale-id="${id}" aria-pressed="${fav}" aria-label="${esc(t(fav ? 'card.unsave' : 'card.save'))}">${icon('heart')}</button>`
    : `<button type="button" class="btn btn-secondary btn-icon btn-fav${fav ? ' is-fav' : ''}" data-lm-fav="${id}" aria-pressed="${fav}" aria-label="${esc(t(fav ? 'card.unsave' : 'card.save'))}">${icon('heart')}</button>`;
  return `<div class="lm-actions">
    ${favBtn}
    <button type="button" class="btn btn-secondary btn-icon" data-listing-id="${id}" aria-label="${esc(t('lm.share'))}" title="${esc(t('lm.share'))}" onclick="shareListing(this.dataset.listingId, event)">${icon('share')}</button>
    <a class="btn btn-primary lm-open" href="${_safeUrl(l.url)}" target="_blank" rel="noopener" data-listing-id="${id}"
       ${kind === 'sale' ? '' : `onclick="_promptArchiveListing(this.dataset.listingId, 'rental', this)"`}><span>${esc(t('lm.open_on', { site }))}</span>${icon('external-link')}</a>
  </div>`;
}

// Header: zone · street, swapping to "€… · 2 rooms · 40 m² · Floor 5" once
// the summary has scrolled under it.
function _lmHeadHtml(l, kind) {
  const esc = escapeHtml;
  const zone = formatAddress(l.neighbourhood) || '';
  const street = l.address ? formatAddress(String(l.address).replace(/,?\s*milano\s*$/i, '')) : '';
  const place = zone && street && street.toLowerCase() !== zone.toLowerCase()
    ? `<span class="place-zone">${esc(zone)}</span><span class="place-street"> · ${esc(street)}</span>`
    : esc(zone || street || (l.title || '').trim() || '–');
  return `<div class="lm-head">
    <h2 id="lmTitle" class="lm-title"><span class="lm-title-place">${place}</span><span class="lm-title-mini" aria-hidden="true">${_lmMiniText(l, kind)}</span></h2>
    <button type="button" class="btn btn-secondary btn-icon lm-close" aria-label="${esc(t('lm.close'))}">${icon('x')}</button>
  </div>`;
}

// The hero: a full-width photo strip (filled by _lmLoadPhotos), the card's
// badges in the same corners.
function _lmHeroHtml(l, kind) {
  const gem = kind === 'sale' ? '' : l.hidden_gem
    ? `<span class="gem-photo-chip chip-hidden">${icon('diamond')}<span>${escapeHtml(t('gem.hidden'))}</span></span>`
    : l.good_value ? `<span class="gem-photo-chip chip-good">${icon('coin')}<span>${escapeHtml(t('gem.good'))}</span></span>` : '';
  const topRight = _priceCutBadge(l) || ((kind === 'sale' ? false : isNew(l)) ? `<span class="card-photo-new-badge">${escapeHtml(t('badge.new'))}</span>` : '');
  const grade = _photoChipHtml(l);
  return `<div class="lm-hero${l.thumbnail ? '' : ' is-empty'}" id="lmHero">
    <div class="lm-hero-track" id="lmPhotos">${l.thumbnail ? `<button type="button" class="lm-hero-slide" data-pv-open="0"><img src="${_safeUrl(l.thumbnail)}" alt=""></button>` : `<span>${icon('photo')} ${escapeHtml(t('lm.no_photos'))}</span>`}</div>
    ${gem}${topRight}${grade ? `<span class="lm-hero-grade">${grade}</span>` : ''}
    <button type="button" class="card-photo-nav prev" data-lm-hero-step="-1" aria-label="${escapeHtml(t('pv.prev'))}">${icon('chevron-left')}</button>
    <button type="button" class="card-photo-nav next" data-lm-hero-step="1" aria-label="${escapeHtml(t('pv.next'))}">${icon('chevron-right')}</button>
    <span class="card-photo-count lm-hero-count" hidden></span>
  </div>`;
}

// Monthly costs (rentals): the table, unchanged.
function _lmCostsSecHtml(l) {
  return `<section class="lm-sec" id="lmCosts"><h3 class="lm-sec-title">${escapeHtml(t('lm.monthly_costs'))}</h3>${_lmCostTable(l)}</section>`;
}

// The whole sheet: one skeleton for rentals and sales.
function lmSheetHtml(l, kind) {
  return `${_lmHeadHtml(l, kind)}
  <div class="lm-scroll">
    ${_lmHeroHtml(l, kind)}
    ${_lmSummaryHtml(l, kind)}
    ${_lmKnowHtml(l, kind)}
    ${_lmDescHtml(l)}
    <div class="lm-pair">
      ${kind === 'sale' ? _saleOwnTableHtml(l, saleFilters()) : _lmCostsSecHtml(l)}
      ${_lmPriceVsHtml(l, kind)}
    </div>
    ${_lmFactsListHtml(l, kind)}
    ${_lmLocationHtml(l)}
    ${_lmScoreFoldHtml(l)}
    ${_lmPhotoFoldHtml(l)}
    ${_lmAboutHtml(l, kind)}
  </div>
  ${_lmActionsHtml(l, kind)}`;
}

// After a sheet is in the DOM: "Read all" when the text is clamped, the
// header swap, the comparables on first open.
function _lmAfterRender(l, kind) {
  const box = document.getElementById('listingModal');
  if (box && typeof _observeScoreRings === 'function') _observeScoreRings(box, false);   // the ring's final number
  const desc = document.querySelector('#lmDesc .lm-desc-text'), more = document.querySelector('[data-lm-desc]');
  if (desc && more) requestAnimationFrame(() => { if (desc.scrollHeight > desc.clientHeight + 2) more.hidden = false; });
  const sum = document.getElementById('lmSum'), scroll = box?.querySelector('.lm-scroll');
  if (sum && scroll && 'IntersectionObserver' in window) {
    const io = new IntersectionObserver(([en]) => box.classList.toggle('is-past-sum', !en.isIntersecting && en.boundingClientRect.top < 100),
                                        { root: scroll, threshold: 0 });
    io.observe(sum);
  }
  const fold = document.getElementById('lmCompsFold');
  if (fold && kind !== 'sale') fold.addEventListener('toggle', () => {
    const c = document.getElementById('lmComps');
    if (fold.open && c && !c.dataset.loaded) { c.dataset.loaded = '1'; _loadCompsList(l, c); }
  });
}

document.addEventListener('click', e => {
  const tog = e.target.closest?.('[data-lm-desc]');
  if (tog) {
    const sec = tog.closest('.lm-desc'), open = sec.classList.toggle('is-open');
    tog.textContent = t(open ? 'lm.show_less' : 'lm.read_all');
    return;
  }
  const go = e.target.closest?.('[data-lm-goto]');
  if (go) {
    const el = document.getElementById(go.dataset.lmGoto);
    if (el) { el.open = true; el.scrollIntoView({ block: 'start', behavior: 'smooth' }); }
    return;
  }
  const step = e.target.closest?.('[data-lm-hero-step]');
  if (step) {
    const track = document.getElementById('lmPhotos');
    if (track) track.scrollBy({ left: Number(step.dataset.lmHeroStep) * track.clientWidth, behavior: 'smooth' });
  }
});

function listingModalHtml(l) {
  return lmSheetHtml(l, 'rent');
}

// The Collection button says where the flat already is.
function _lmColLabel(listingId, kind) {
  const inCols = Collections.ofKind(kind).filter(c => c.listing_ids.includes(String(listingId)));
  return inCols.length > 1 ? t('lm.in_collections', { n: fmt(inCols.length) })
    : inCols.length ? t('lm.in_collection', { name: inCols[0].name }) : t('lm.add_collection');
}

// After a ♡ in Details: the button's label, and the open list of collections.
function _lmRefreshCollections() {
  const btn = document.querySelector('#listingModal [data-lm-collections]');
  if (!btn) return;
  const id = btn.dataset.lmCollections, kind = btn.dataset.lmKind || 'rent';
  const span = btn.querySelector('span');
  if (span) span.textContent = _lmColLabel(id, kind);
  if (!document.getElementById('lmCollections')?.hidden) renderLmCollections(id, kind);
}

// "Collection" in Details: tick the collections (of this flat's kind) it
// belongs to, or make a new one — inline, no prompt().
function renderLmCollections(listingId, kind = 'rent') {
  const box = document.getElementById('lmCollections');
  if (!box) return;
  const cols = Collections.ofKind(kind);
  const span = document.querySelector('#listingModal [data-lm-collections] span');
  if (span) span.textContent = _lmColLabel(listingId, kind);
  box.innerHTML = `
    <h3 class="lm-section-title" style="margin-top:0">${escapeHtml(t('lm.collection'))}</h3>
    ${cols.length ? cols.map(c => `
      <label class="lm-col-row">
        <input type="checkbox" data-col-toggle="${escapeHtml(c.id)}" ${Collections.isInCollection(c.id, listingId) ? 'checked' : ''}>
        <span>${escapeHtml(c.name)}</span>
        <span class="col-count">${fmt(c.listing_ids.length)}</span>
      </label>`).join('') : `<p class="lm-unknown-line">${escapeHtml(t('col.none_yet'))}</p>`}
    <form class="lm-col-new" data-col-new="${escapeHtml(String(listingId))}" data-col-kind="${kind}" autocomplete="off">
      <input type="text" class="input" maxlength="60" placeholder="${escapeHtml(t('col.new_placeholder'))}" aria-label="${escapeHtml(t('col.new_placeholder'))}">
      <button type="submit" class="btn btn-secondary">${icon('plus')}<span>${escapeHtml(t('col.create'))}</span></button>
    </form>`;
}
