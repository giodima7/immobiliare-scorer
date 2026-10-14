// Lume dashboard — filter controls, pill thumbs, maintenance buttons (cache, OMI, POIs, geo), mobile filter drawer, onboarding.
// Plain script (no bundler); shares globals with the other assets/*.js files.
// Declarations only: code that runs at start-up lives in init.js, which loads
// last, so any function here may call any other.

// ── Rentals filter event listeners ───────────────────────────────────────────
const _debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const _renderRentalsDebounced = _debounce(() => { renderRentals(); saveFilters(); }, 350);

// ── Pill-group sliding thumb ───────────────────────────────────────────────
// Each .filter-pill-group has a single absolutely-positioned ::before
// pseudo-element acting as the "thumb" — its left + width are driven by
// two CSS custom properties that we update here. The CSS transition on
// those properties does the sliding animation; no JS-side animation
// frame work required.
function _updatePillThumb(group) {
  if (!group) return;
  const active = group.querySelector('.filter-pill.active');
  if (!active) {
    group.style.removeProperty('--thumb-width');
    return;
  }
  const groupRect = group.getBoundingClientRect();
  const btnRect   = active.getBoundingClientRect();
  // Subtract the group's padding-start so the thumb sits flush inside
  // the rail's 3 px inner gutter at left:0 + 3px.
  const left = btnRect.left - groupRect.left;
  group.style.setProperty('--thumb-left',  `${left}px`);
  group.style.setProperty('--thumb-width', `${btnRect.width}px`);
}
function _updateAllPillThumbs() {
  document.querySelectorAll('.filter-pill-group').forEach(_updatePillThumb);
}

// Score slider ↔ number-input sync. Slider stays the source of truth so
// getRentFilters() + every other reader of rScore.value keeps working.
// rScoreVal (the old text mirror) is now a hidden node — preserved so
// any stray writes to it from elsewhere don't throw.
function _rScoreClamp(v) {
  const n = parseInt(v, 10);
  if (!Number.isFinite(n)) return 0;
  return Math.max(0, Math.min(100, n));
}
function syncScoreFromSlider(v) {
  const clamped = _rScoreClamp(v);
  const numEl   = document.getElementById('rScoreNum');
  const valEl   = document.getElementById('rScoreVal');
  if (numEl && numEl.value !== String(clamped)) numEl.value = clamped;
  if (valEl) valEl.textContent = String(clamped);
}
function syncScoreFromNumber(v) {
  const clamped  = _rScoreClamp(v);
  const sliderEl = document.getElementById('rScore');
  const valEl    = document.getElementById('rScoreVal');
  if (sliderEl && sliderEl.value !== String(clamped)) sliderEl.value = clamped;
  if (valEl) valEl.textContent = String(clamped);
}

// "What Lume sends" panel + its handlers were removed during UI
// consolidation. Daily-digest config now lives in Settings → Daily Alerts
// (Supabase digest_filters). The local /email-config + /send-digest-now
// endpoints still exist for back-compat with one-off CLI invocations.

// ── Scanner filter persistence ─────────────────────────────────────────────────
const SCAN_PREFS_KEY = 'immo_scan_prefs';

function saveScanPrefs() {
  const prefs = {
    pages:    document.getElementById('sScanPages').value,
    maxRent:  document.getElementById('sScanMaxRent').value,
    minRooms: document.getElementById('sScanMinRooms').value,
  };
  localStorage.setItem(SCAN_PREFS_KEY, JSON.stringify(prefs));
  // Also persist to server so api.py auto-start picks them up on next launch
  if (IS_LIVE) {
    fetch('/scan-prefs', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        pages:     parseInt(prefs.pages) || 3,
        max_rent:  parseInt(prefs.maxRent)  || 0,
        min_rooms: parseInt(prefs.minRooms) || 0,
      }),
    }).catch(() => {});
  }
}

function restoreScanPrefs() {
  try {
    const prefs = JSON.parse(localStorage.getItem(SCAN_PREFS_KEY));
    if (!prefs) return;
    if (prefs.pages)    document.getElementById('sScanPages').value    = prefs.pages;
    if (prefs.maxRent)  document.getElementById('sScanMaxRent').value  = prefs.maxRent;
    if (prefs.minRooms) document.getElementById('sScanMinRooms').value = prefs.minRooms;
    // Areas are owned by area_settings.json (Settings tab) — not restored from localStorage
  } catch { /* corrupt prefs — ignore */ }
}

// Early area-settings load: populate mobile drawer + pre-select scanner areas.
// _settingsCurrentAreas is shared with the Settings tab; this fetch only writes it
// when the Settings tab hasn't opened yet (_settingsLoaded === false) to avoid
// clobbering in-progress picker edits.
let _settingsCurrentAreas = [];

// ── Cache stats ───────────────────────────────────────────────────────────────
async function loadCacheStats() {
  const countEl  = document.getElementById('cacheCount');
  const newestEl = document.getElementById('cacheNewest');
  const cacheRow = document.getElementById('cacheRow');
  if (!IS_LIVE) {
    if (cacheRow) cacheRow.style.display = 'none';
    return;
  }
  try {
    const r = await fetch('/cache-stats');
    if (!r.ok) return;
    const s = await r.json();
    if (countEl)  countEl.textContent  = s.total != null ? s.total.toLocaleString('it-IT') + ' enriched' : '–';
    if (newestEl && s.newest) {
      const d = new Date(s.newest);
      const ts = d.toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' });
      newestEl.textContent = '· last updated ' + ts;
      newestEl.style.color = 'var(--muted)';
    } else if (newestEl) {
      newestEl.textContent = '';
    }
  } catch { /* /cache-stats unavailable */ }
}

// ── Apply OMI polygon button ──────────────────────────────────────────────────

// ── Download POIs button ──────────────────────────────────────────────────────

// ── Geo enrichment button ─────────────────────────────────────────────────────

// ── Mobile filter drawer (Rentals) ──────────────────────────────────────────
let _drawerIsMap = false;            // true when the drawer was opened from the map view

// ── Onboarding ──────────────────────────────────────────────────────────────
// Called by showApp() AFTER auth resolves so Auth.userId() is the real user ID.
// Global unscoped key for the onboarded flag. Previously this used
// Storage.get('onboarded') which keys by Auth.userId() — but userId()
// returns 'guest' before Clerk resolves and the real id after, so the
// flag set during one phase couldn't be read in the other and the
// slideshow showed on every page load. A device-wide UI preference
// belongs in a single key, not a per-user namespace.
const _ONBOARDED_KEY = 'lume_onboarded_v1';
function _hasSeenOnboarding() {
  try {
    // Check both the new global key AND every legacy per-user key — if
    // the user has clicked through the slideshow under ANY identity,
    // count them as onboarded so they don't see it again.
    if (localStorage.getItem(_ONBOARDED_KEY)) return true;
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && k.startsWith('scorer_onboarded_') && localStorage.getItem(k) === 'true') {
        // Self-heal: copy into the new global key so future loads
        // don't pay the iteration cost.
        try { localStorage.setItem(_ONBOARDED_KEY, '1'); } catch (_) {}
        return true;
      }
    }
  } catch (_) {}
  return false;
}
function _markOnboarded() {
  try { localStorage.setItem(_ONBOARDED_KEY, '1'); } catch (_) {}
}

function _checkOnboarding() {
  const overlay = document.getElementById('onboardingOverlay');
  if (!overlay) return;
  const isLocal = ['localhost','127.0.0.1'].includes(location.hostname);
  if (isLocal || _hasSeenOnboarding()) {
    overlay.remove();   // Dev mode: always skip; production: skip if already seen
  } else {
    overlay.classList.add('is-visible');
  }
}
