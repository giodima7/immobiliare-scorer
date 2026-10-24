// Lume dashboard — auth, Supabase client, storage. Plain script (no bundler), loaded by index.html;
// shares globals with the other assets/*.js files.
// ── Auth (Clerk) ─────────────────────────────────────────────────────────────
// Public interface preserved from the previous Netlify Identity build:
//   Auth.init()         → boot the auth flow (called from <body> on load)
//   Auth.userId()       → stable per-user key for localStorage scoping
//   Auth.userName()     → display name / email
//   Auth.userInitial()  → first letter for the avatar circle
//   Auth.signOut()      → sign out with confirm()
//   Auth.openSignIn()   → open Clerk's hosted sign-in modal

// ─── Supabase configuration ─────────────────────────────────────────────────
// On Cloudflare Pages, build.sh substitutes the %% placeholders with the
// real values from the SUPABASE_URL / SUPABASE_ANON_KEY env vars. Locally
// (file://, `python3 api.py`, or any path that skips the build step) the
// placeholders are never replaced — so we fall through to a hardcoded
// project URL + publishable anon key.
//
// The anon key is safe to commit — it's public, read-only, and Row Level
// Security restricts it to non-stale listings. The service_role key (which
// CAN write) lives only in GitHub Secrets and is never exposed to the
// browser.
const SUPABASE_URL = '%%SUPABASE_URL%%'.startsWith('%%')
  ? 'https://sexbjvmedgappzqmcdjv.supabase.co'
  : '%%SUPABASE_URL%%';

const SUPABASE_ANON_KEY = '%%SUPABASE_ANON_KEY%%'.startsWith('%%')
  ? 'sb_publishable_rxIMYBFh3IeQvNUgfAFEKQ_CUc_Rsba'
  : '%%SUPABASE_ANON_KEY%%';

const SupabaseClient = {
  _base: null,
  init() {
    // SUPABASE_URL falls back to the hardcoded project URL when the build
    // placeholder hasn't been substituted, so a leading '%%' here means
    // both Cloudflare's sed AND the fallback failed — i.e. someone broke
    // the constants above. Treat that as "not configured" rather than
    // crashing later.
    if (!SUPABASE_URL || SUPABASE_URL.startsWith('%%')) {
      console.warn('[supabase] not configured — dashboard cannot load listings');
      return;
    }
    this._base = `${SUPABASE_URL}/rest/v1`;
    console.log('[supabase] configured');
  },
  get configured() { return !!this._base; },
  /**
   * Fetch all non-stale listings of a given type from Supabase.
   * Returns null on any failure (callers render empty + a notice).
   * Re-maps DB column names with a leading underscore back to the
   * legacy listing-dict shape the rest of the dashboard expects.
   */
  // Columns the rentals view reads. photos / comps_ids / description are the
  // heaviest (a third of each page) and only needed on demand. If the table
  // lacks one of these (e.g. before migration 014 ran) the first page comes
  // back 400 and fetchAll falls back to select=*.
  _RENT_COLUMNS: 'absolute_value_gate_applied,address,agency_id,agency_name,agency_type,agency_url,air_conditioning,ask_psqm,ask_psqm_rent,availability,bathrooms,bedrooms,city,comps_adjusted,comps_conf_label,comps_delta_pct,comps_median,comps_n,comps_p40,comps_p60,comps_radius_m,comps_source,condition,condo_included,condominium_fees,contract_type,days_on_market,days_since_seen,deposit_eur,deposit_months,detail_fetched_at,dup_group,elevator,energy_class,excluded,extreme_underpricing,extreme_underpricing_delta,first_seen_date,floor,floor_label,floor_n,furnished,geo_precision,geo_score,gone_checked,good_value,has_balcony,has_parking,heating_mo,heating_type,hidden_gem,id,is_corporate_rental,is_external,is_stale,last_seen_date,latitude,ldi_score,listing_type,longitude,metro_nearest_dist_m,metro_nearest_line,metro_nearest_name,metro_walk_min,neighbourhood,omi_descr,omi_fallback,omi_fascia,omi_loc_max,omi_loc_mid,omi_loc_min,omi_source,omi_zona,park_nearest_dist_m,photo_bathroom,photo_by_user,photo_condition,photo_confidence,photo_count,photo_deal,photo_flags,photo_grade,photo_kitchen,photo_light,photo_note_en,photo_note_it,photo_read_at,photo_sonnet_deal,photo_user_note,previous_price,price,price_changed_date,price_suspect,price_z,published_date,relisted_from_date,relisted_from_id,relisted_from_rent,room_efficiency_flag,rooms,score_cap_reason,score_location,score_penalty,score_physical,score_price,score_property,score_reasons,score_total,score_was_capped,source,sqm,suggested_rent_mo,suggested_rent_psqm,supermarket_nearest_dist_m,thumbnail,title,tram_nearest_dist_m,twin_filled,university_nearest_dist_m,url,vs_omi_label,vs_omi_pct,year_built',
  lastLoadPartial: false,

  _headers() {
    return {
      'apikey':        SUPABASE_ANON_KEY,
      'Authorization': `Bearer ${SUPABASE_ANON_KEY}`,
      'Accept':        'application/json',
      'Prefer':        'count=none',
    };
  },

  async _page(listingType, city, order, offset, select) {
    const params = new URLSearchParams({
      select,
      listing_type: `eq.${listingType}`,
      city:         `eq.${city}`,
      is_stale:     'eq.false',
      // id breaks score ties — without it offset paging skips/repeats rows.
      order:        `${order},id.asc`,
      limit:        '1000',
      offset:       String(offset),
    });
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const resp = await fetch(`${this._base}/listings?${params}`, {
          headers: this._headers(), signal: AbortSignal.timeout(30000),
        });
        if (resp.ok) return { ok: true, status: resp.status, rows: await resp.json() };
        console.warn(`[supabase] HTTP ${resp.status} at offset ${offset}`);
        if (resp.status < 500) return { ok: false, status: resp.status, rows: [], error: await resp.text().catch(() => '') };
      } catch (err) {
        console.warn(`[supabase] fetch error at offset ${offset}: ${err.message}`);
      }
      await new Promise(r => setTimeout(r, 1500));   // transient 5xx / network: one retry
    }
    return { ok: false, status: 0, rows: [] };
  },

  _reshape(rows, listingType) {
    // Re-shape DB rows into the dict shape the rest of the dashboard expects:
    //   - underscore-prefixed flags (Postgres can't name a column "_foo")
    //   - rent_mo as an alias for the unified `price` column on rentals
    //   - ask_psqm_rent / spese_condominiali alias from the DB column names
    const isRental = listingType === 'rental';
    return rows.map(r => ({
      ...r,
      _room_efficiency_flag:        r.room_efficiency_flag,
      _absolute_value_gate_applied: r.absolute_value_gate_applied,
      rent_mo:                      isRental ? (r.rent_mo ?? r.price)          : r.rent_mo,
      ask_psqm_rent:                isRental ? (r.ask_psqm_rent ?? r.ask_psqm) : r.ask_psqm_rent,
      spese_condominiali:           r.spese_condominiali ?? r.condominium_fees,
    }));
  },

  /**
   * Fetch all non-stale listings of a given type. The first page is handed
   * to opts.onFirstPage as soon as it arrives (so the grid can paint); the
   * rest load four pages at a time. Returns null when nothing could be
   * loaded; sets lastLoadPartial when a later page failed.
   */
  async fetchAll(listingType, opts = {}) {
    if (!this.configured) return null;
    const { order = 'score_total.desc', onFirstPage = null } = opts;
    const PAGE = 1000, WAVE = 4;
    const _city = (typeof currentCity === 'string' && currentCity) || 'milano';
    let select = listingType === 'rental' ? this._RENT_COLUMNS : '*';
    this.lastLoadPartial = false;

    let first = await this._page(listingType, _city, order, 0, select);
    // A column the table doesn't have yet (migration not run): drop just
    // that one and retry; anything else → all columns.
    for (let tries = 0; !first.ok && first.status === 400 && select !== '*' && tries < 8; tries++) {
      const m = /column (?:\w+\.)?(\w+) does not exist/.exec(first.error || '');
      select = m && select.split(',').includes(m[1])
        ? select.split(',').filter(c => c !== m[1]).join(',')
        : '*';
      first = await this._page(listingType, _city, order, 0, select);
    }
    if (!first.ok) return null;
    if (onFirstPage && first.rows.length) {
      try { onFirstPage(this._reshape(first.rows, listingType)); } catch (e) { console.warn(e); }
    }
    let all = first.rows;
    let done = first.rows.length < PAGE;
    for (let offset = PAGE; !done; offset += WAVE * PAGE) {
      const pages = await Promise.all(Array.from({ length: WAVE },
        (_, i) => this._page(listingType, _city, order, offset + i * PAGE, select)));
      for (const p of pages) {
        if (!p.ok) { this.lastLoadPartial = true; done = true; break; }
        all = all.concat(p.rows);
        if (p.rows.length < PAGE) { done = true; break; }
      }
    }
    console.log(`[supabase] loaded ${all.length} ${_city} ${listingType}s${this.lastLoadPartial ? ' (partial)' : ''}`);
    return this._reshape(all, listingType);
  },

  /** Rows by id — archived ones included (comparable flats can be). */
  async fetchByIds(ids, select = '*') {
    if (!this.configured || !ids.length) return [];
    const list = ids.map(x => `"${String(x).replace(/"/g, '')}"`).join(',');
    try {
      const resp = await fetch(`${this._base}/listings?select=${encodeURIComponent(select)}&id=in.(${encodeURIComponent(list)})`,
        { headers: this._headers(), signal: AbortSignal.timeout(30000) });
      return resp.ok ? this._reshape(await resp.json(), 'rental') : [];
    } catch { return []; }
  },
};
SupabaseClient.init();

const Auth = (() => {
  const isLocal = ['localhost','127.0.0.1'].includes(location.hostname);

  // Wait for the Clerk SDK to attach to window. The loader script is async,
  // so init() may run before window.Clerk is defined.
  const _waitForClerk = () => new Promise(resolve => {
    if (window.Clerk) return resolve(window.Clerk);
    const t0 = Date.now();
    const iv = setInterval(() => {
      if (window.Clerk) { clearInterval(iv); resolve(window.Clerk); }
      else if (Date.now() - t0 > 8000) { clearInterval(iv); resolve(null); }
    }, 50);
  });

  const module = {
    user: null,         // Clerk user object (null while signed out)
    _clerk: null,       // Clerk SDK instance (null on localhost)

    async init() {
      // Always show the app — sign-in is optional (only required when saving).
      // On localhost we skip Clerk entirely; on prod we still load it in the
      // background so we can detect an existing session and let the user
      // sign in to access their favourites/collections.
      showApp();
      // Render shared-collection (?c=…) deep-link banner if present
      if (typeof handleSharedCollectionParam === 'function') {
        try { handleSharedCollectionParam(); } catch (_) {}
      }

      if (isLocal) {
        updateUserAvatar();
        return;
      }

      const clerk = await _waitForClerk();
      if (!clerk) {
        // Clerk failed to load (network blip, ad-blocker, etc.) — leave the
        // app open as a guest. updateUserAvatar() will show a "Sign in" button.
        updateUserAvatar();
        return;
      }
      this._clerk = clerk;
      try { await clerk.load(); } catch (_) {}

      this.user = clerk.user || null;
      // Re-render whenever Clerk's auth state changes (sign-in / sign-out / token refresh)
      clerk.addListener(({ user }) => {
        const wasSignedIn = !!this.user;
        this.user = user || null;
        if (this.user && !wasSignedIn)        this._onLogin(this.user);
        else if (!this.user && wasSignedIn)   this._onLogout();
      });

      if (this.user) {
        // Already signed in from a previous session — migrate any guest data
        // and refresh the UI without disturbing the visible app.
        this._onLogin(this.user);
      } else {
        updateUserAvatar();
      }
    },

    /**
     * Gate a save / write action behind sign-in. Returns true if signed in
     * (caller may proceed), or false after opening the Clerk sign-in modal.
     * Optional `message` is logged so callers can surface a toast if they wish.
     */
    requireSignIn(message) {
      if (this.user) return true;
      if (message && typeof showToast === 'function') {
        try { showToast(message); } catch (_) {}
      }
      this.openSignIn();
      return false;
    },

    isSignedIn() { return !!this.user; },

    userId() { return this.user?.id || 'guest'; },
    userName() {
      const u = this.user;
      if (!u) return 'User';
      return u.fullName
          || [u.firstName, u.lastName].filter(Boolean).join(' ')
          || u.primaryEmailAddress?.emailAddress
          || u.username
          || 'User';
    },
    userInitial() { return (this.userName()[0] || 'U').toUpperCase(); },

    openSignIn() {
      if (this._clerk?.openSignIn) this._clerk.openSignIn();
      else if (this._clerk?.redirectToSignIn) this._clerk.redirectToSignIn();
    },

    _onLogin(user) {
      // App is already visible (no auth gate). Just migrate any guest data
      // into this user's namespace and refresh UI.
      Storage.migrate(user.id);
      // Cache the Clerk user ID so other static pages (e.g. favourites.html)
      // can scope their localStorage keys without re-loading the Clerk SDK.
      try { localStorage.setItem('lume_clerk_user_id', user.id); } catch (_) {}
      // Re-apply persisted filters now that Storage.key() resolves to the
      // real user ID. showApp() called restoreFilters() before Clerk had
      // loaded, so that first pass read the empty `scorer_filters_guest`
      // bucket; the saves from the user's previous session live under
      // `scorer_filters_<realUserId>` and only become readable here.
      if (typeof restoreFilters === 'function') {
        try { restoreFilters(); } catch (_) {}
      }
      updateUserAvatar();
      updateFavBadge?.();
      // Show the "🔔 Save alert" buttons (desktop pill + mobile drawer)
      // — saving a daily digest needs an identified user (clerk_user_id
      // is the digest_filters PK).
      const _saveBtn = document.getElementById('save-digest-btn');
      if (_saveBtn) _saveBtn.style.display = 'inline-flex';
      const _drawerSaveBtn = document.getElementById('drawerSaveAlertBtn');
      if (_drawerSaveBtn) _drawerSaveBtn.style.display = 'block';
      // Refresh the avatar's red dot from Supabase. Async fire-and-forget;
      // the dot stays hidden until the fetch resolves.
      if (typeof _refreshDigestAlertBadge === 'function') _refreshDigestAlertBadge();
      // Re-render listings so favourite hearts reflect the user's saved set
      if (typeof renderRentals === 'function') try { renderRentals(); } catch (_) {}
      if (typeof renderSales   === 'function') try { renderSales();   } catch (_) {}
    },
    _onLogout() {
      try { localStorage.removeItem('lume_clerk_user_id'); } catch (_) {}
      updateUserAvatar();
      updateFavBadge?.();
      const _saveBtn = document.getElementById('save-digest-btn');
      if (_saveBtn) _saveBtn.style.display = 'none';
      const _drawerSaveBtn = document.getElementById('drawerSaveAlertBtn');
      if (_drawerSaveBtn) _drawerSaveBtn.style.display = 'none';
      // Clear the alert dot — the now-anonymous user has no scoped alerts.
      if (typeof _refreshDigestAlertBadge === 'function') _refreshDigestAlertBadge();
      if (typeof renderRentals === 'function') try { renderRentals(); } catch (_) {}
      if (typeof renderSales   === 'function') try { renderSales();   } catch (_) {}
    },

    signOut() {
      if (confirm(`Signed in as ${this.userName()}\n\nSign out?`)) {
        this._clerk?.signOut?.();
      }
    }
  };
  return module;
})();

// ── Per-user storage ──────────────────────────────────────────────────────────
const Storage = {
  key(name) { return `scorer_${name}_${Auth.userId()}`; },

  get(name, fallback = null) {
    try {
      const raw = localStorage.getItem(this.key(name));
      return raw !== null ? JSON.parse(raw) : fallback;
    } catch { return fallback; }
  },

  set(name, value) {
    try { localStorage.setItem(this.key(name), JSON.stringify(value)); }
    catch (e) { console.warn('Storage.set failed:', e); }
    _pushUserData(name, value);
  },

  remove(name) { localStorage.removeItem(this.key(name)); },

  // On first login, migrate any data saved under 'guest' key to real user key.
  // Also lifts pre-auth raw keys (immo_favourites etc.) into the scoped namespace.
  migrate(userId) {
    // guest-scoped → user-scoped
    ['favourites', 'rentFavourites', 'filters', 'onboarded', 'saleFetchPrefs'].forEach(name => {
      const guestKey = `scorer_${name}_guest`;
      const userKey  = `scorer_${name}_${userId}`;
      const guestVal = localStorage.getItem(guestKey);
      if (guestVal && !localStorage.getItem(userKey)) {
        localStorage.setItem(userKey, guestVal);
        localStorage.removeItem(guestKey);
      }
    });
    // legacy raw keys → scoped user keys (one-time lift on first login)
    const legacyMap = {
      'immo_favourites':      `scorer_favourites_${userId}`,
      'immo_rent_favourites': `scorer_rentFavourites_${userId}`,
    };
    Object.entries(legacyMap).forEach(([oldKey, newKey]) => {
      const val = localStorage.getItem(oldKey);
      if (val && !localStorage.getItem(newKey)) {
        localStorage.setItem(newKey, val);
        localStorage.removeItem(oldKey);
      }
    });
  }
};

// ── Local copy of saved items ─────────────────────────────────────────────────
// Locally (IS_LIVE) favourites and collections are mirrored to api.py
// (/api/user-data → user_data.json), so a cleared or different browser on
// this machine keeps them. Online they stay in this browser's storage.
const _USER_DATA_KEYS = ['rentFavourites', 'favourites', 'collections'];

function _pushUserData(name, value) {
  let live = false;
  try { live = IS_LIVE; } catch { /* declared in a later script, not run yet */ }
  if (!live || !_USER_DATA_KEYS.includes(name)) return;
  fetch(`/api/user-data/${name}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value),
  }).catch(() => {});
}

async function _syncUserDataFromServer() {
  if (!IS_LIVE) return;
  try {
    const r = await fetch('/api/user-data');
    if (!r.ok) return;
    const server = await r.json();
    for (const k of _USER_DATA_KEYS) {
      if (server[k] != null) {
        localStorage.setItem(Storage.key(k), JSON.stringify(server[k]));   // the file wins
      } else {
        const local = Storage.get(k, null);
        if (local != null) _pushUserData(k, local);                         // first run: seed it
      }
    }
  } catch { /* offline file → keep browser copy */ }
}

// ── Auth screen helpers ───────────────────────────────────────────────────────
function showLoginScreen() {
  document.getElementById('login-screen').style.display = 'flex';
  const r = document.getElementById('app-root');
  if (r) r.style.display = 'none';
}
function hideLoginScreen() {
  document.getElementById('login-screen').style.display = 'none';
}
function showApp() {
  const r = document.getElementById('app-root');
  if (r) r.style.display = 'block';
  restoreFilters();
  // Deferred until here so Auth.userId() is the real user ID, not 'guest'
  if (typeof _checkOnboarding === 'function') _checkOnboarding();
  // Router checks the URL: if ?view=detail&l=ID is present we render the
  // detail page instead of the dashboard. Safe to call before sales data
  // arrives — it shows a "Listing not loaded yet" empty state and the next
  // call after loadSales() completes will populate it.
  if (typeof Router !== 'undefined') Router.init();
  // Restore the active tab from the user's previous session (per-user via
  // Storage). Runs after the tab DOM exists; skips itself when the URL
  // targets a detail page or shared collection.
  if (typeof restoreActiveTab === 'function') restoreActiveTab();
}
function hideApp() {
  const r = document.getElementById('app-root');
  if (r) r.style.display = 'none';
}

// Signed in: a round initial that opens the account menu. Signed out
// (online only): a quiet "Sign in" button. The local dashboard has no
// accounts, so nothing shows there.
function updateUserAvatar() {
  const el   = document.getElementById('user-avatar');
  const wrap = document.getElementById('user-avatar-wrap');
  if (!el || !wrap) return;
  const local = ['localhost', '127.0.0.1'].includes(location.hostname);
  const signedIn = typeof Auth !== 'undefined' && Auth.isSignedIn && Auth.isSignedIn();
  wrap.hidden = local && !signedIn;
  el.classList.toggle('is-signed-in', !!signedIn);
  if (signedIn) {
    el.textContent = Auth.userInitial();
    el.title = Auth.userName();
    el.setAttribute('aria-label', t('menu.account'));
    el.setAttribute('onclick', 'showUserMenu(event)');
  } else {
    el.innerHTML = `${icon('user')}<span class="fav-label">${escapeHtml(t('header.sign_in'))}</span>`;
    el.title = t('header.sign_in_tip');
    el.setAttribute('onclick', 'Auth.openSignIn()');
  }
}

// Persist EVERY filter knob on both tabs so a refresh — or even a session
// resume after closing the tab — keeps the user's current view intact. The
// payload is `{ rentals: {...}, sales: {...} }`; an old flat blob (legacy
// pre-2026-05-19 schema) is detected and read as rentals for back-compat.
//
// Inputs that don't exist yet (sales area MSD is lazy, built when the user
// first opens the Sales tab) are handled by writing to the underlying var
// (saleSelectedAreas) here and re-syncing the MSD trigger when it's built.
function restoreFilters() {
  const filters = Storage.get('filters');
  if (!filters) return;

  const setVal = (id, val) => {
    const el = document.getElementById(id);
    if (el && val != null && val !== '') el.value = val;
  };
  const setChk = (id, val) => {
    const el = document.getElementById(id);
    if (el && val != null) el.checked = !!val;
  };
  const setTab = (groupSel, dataAttr, val) => {
    if (val == null) return;
    const tabs = document.querySelectorAll(groupSel);
    if (!tabs.length) return;
    tabs.forEach(t => t.classList.toggle('active', (t.dataset[dataAttr] || '') === val));
  };

  // Back-compat: old payload was flat (rentals-only); new payload nests
  // under `rentals` + `sales` keys.
  const r = filters.rentals || filters;
  const s = filters.sales   || {};

  // Rentals
  setVal('rFascia',    r.fascia);
  setVal('rMaxRent',   r.maxRent);
  setVal('rMinSqm',    r.minSqm);
  setVal('rMinRooms',  r.minRooms);
  setVal('rMaxMetro',  r.maxMetro);
  setVal('rMinFloor',  r.minFloor);
  setVal('rMinEnergy', r.minEnergy);
  setVal('rMinPhoto',  r.minPhoto);
  setVal('rListed',    r.fListed);
  if (r.minScore != null && r.minScore !== '') {
    setVal('rScore',    r.minScore);
    setVal('rScoreNum', r.minScore);
    const valEl = document.getElementById('rScoreVal');
    if (valEl) valEl.textContent = String(r.minScore);
  }
  setChk('rElevator',  r.elevator);
  setChk('rShowStale', r.showStale);
  setVal('rCondition',   r.condition);
  setVal('rMinDiscount', r.minDiscount);
  setVal('rContract',    r.contract);
  if (typeof rentMust !== 'undefined' && Array.isArray(r.must)) {
    rentMust = new Set(r.must.filter(k => k !== 'lift'));
  }
  if (typeof rentIncludeUnknown !== 'undefined' && r.includeUnknown != null) {
    rentIncludeUnknown = !!r.includeUnknown;
    setChk('rIncludeUnknown', rentIncludeUnknown);
    setChk('dIncludeUnknown', rentIncludeUnknown);
  }
  setTab('#rSourceFilter .sort-tab', 'rsource', r.source);
  setTab('#rGemFilter .gem-tab',     'gem',     r.gemFilter);
  if (r.sortKey && typeof rentSortKey !== 'undefined') rentSortKey = r.sortKey;
  ['rSortSelect', 'dSort'].forEach(id => setVal(id, r.sortKey));
  if (Array.isArray(r.areas)) {
    if (typeof rentSelectedAreas !== 'undefined') rentSelectedAreas = r.areas.slice();
    if (typeof filterAreaMsd !== 'undefined' && filterAreaMsd && filterAreaMsd.setSelected) {
      try { filterAreaMsd.setSelected(r.areas); } catch (_) {}
    }
  }

  // Sales
  setVal('sFascia',       s.fascia);
  setVal('sOmiZone',      s.omiZone);
  setVal('sMaxPrice',     s.maxPrice);
  setVal('sMinSqm',       s.minSqm);
  setVal('sMinRooms',     s.minRooms);
  setVal('sMaxMetro',     s.maxMetro);
  setVal('sMinFloor',     s.minFloor);
  setVal('sMinEnergy',    s.minEnergy);
  setVal('sMinYield',     s.minYield);
  if (s.minScore != null && s.minScore !== '') {
    setVal('sScore', s.minScore);
    const valEl = document.getElementById('sScoreVal');
    if (valEl) valEl.textContent = String(s.minScore);
  }
  setChk('sElevator',     s.elevator);
  setChk('sShowAuctions', s.showAuctions);
  setChk('sShowNuda',     s.showNuda);
  setChk('sShowStale',    s.showStale);
  setTab('#sSourceFilter .sort-tab', 'ssource', s.source);
  setTab('#sGemFilterBar .gem-tab',  'sgem',    s.gem);
  setTab('#sSortTabs .sort-tab',     'ssort',   s.sortKey);
  if (s.sortKey != null && typeof sSortKey !== 'undefined')       sSortKey       = s.sortKey;
  if (s.gem     != null && typeof sSaleGemFilter !== 'undefined') sSaleGemFilter = s.gem;
  if (Array.isArray(s.areas)) {
    if (typeof saleSelectedAreas !== 'undefined') saleSelectedAreas = s.areas.slice();
    if (typeof saleAreaMsd !== 'undefined' && saleAreaMsd && saleAreaMsd.setSelected) {
      try { saleAreaMsd.setSelected(s.areas); } catch (_) {}
    }
  }

}

function saveFilters() {
  const get = id => document.getElementById(id);
  const filters = {
    rentals: {
      areas:     Array.isArray(typeof rentSelectedAreas !== 'undefined' ? rentSelectedAreas : null)
                 ? rentSelectedAreas.slice() : [],
      fascia:    get('rFascia')?.value,
      maxRent:   get('rMaxRent')?.value,
      minSqm:    get('rMinSqm')?.value,
      minRooms:  get('rMinRooms')?.value,
      maxMetro:  get('rMaxMetro')?.value,
      minFloor:  get('rMinFloor')?.value,
      minEnergy: get('rMinEnergy')?.value,
      minPhoto:  get('rMinPhoto')?.value,
      fListed:   get('rListed')?.value,
      minScore:  get('rScore')?.value,
      elevator:  get('rElevator')?.checked,
      showStale: get('rShowStale')?.checked,
      must:           typeof rentMust !== 'undefined' ? [...rentMust] : [],
      includeUnknown: typeof rentIncludeUnknown !== 'undefined' ? rentIncludeUnknown : false,
      condition:      get('rCondition')?.value || '',
      minDiscount:    get('rMinDiscount')?.value || '',
      contract:       get('rContract')?.value || '',
      source:    document.querySelector('#rSourceFilter .sort-tab.active')?.dataset.rsource || '',
      gemFilter: document.querySelector('#rGemFilter .gem-tab.active')?.dataset.gem || '',
      sortKey:   typeof rentSortKey !== 'undefined' ? rentSortKey : 'score_total',
    },
    sales: {
      areas:        Array.isArray(typeof saleSelectedAreas !== 'undefined' ? saleSelectedAreas : null)
                    ? saleSelectedAreas.slice() : [],
      fascia:       get('sFascia')?.value,
      omiZone:      get('sOmiZone')?.value,
      maxPrice:     get('sMaxPrice')?.value,
      minSqm:       get('sMinSqm')?.value,
      minRooms:     get('sMinRooms')?.value,
      maxMetro:     get('sMaxMetro')?.value,
      minFloor:     get('sMinFloor')?.value,
      minEnergy:    get('sMinEnergy')?.value,
      minYield:     get('sMinYield')?.value,
      minScore:     get('sScore')?.value,
      elevator:     get('sElevator')?.checked,
      showAuctions: get('sShowAuctions')?.checked,
      showNuda:     get('sShowNuda')?.checked,
      showStale:    get('sShowStale')?.checked,
      source:       document.querySelector('#sSourceFilter .sort-tab.active')?.dataset.ssource || '',
      gem:          typeof sSaleGemFilter !== 'undefined' ? sSaleGemFilter : '',
      sortKey:      typeof sSortKey !== 'undefined' ? sSortKey : 'score_total',
    },
  };
  Storage.set('filters', filters);
}
