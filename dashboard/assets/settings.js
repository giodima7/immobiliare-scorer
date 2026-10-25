// Lume dashboard — Settings tab: areas, scoring and badge settings, data quality.
// Plain script (no bundler); shares globals with the other assets/*.js files.
// Declarations only: code that runs at start-up lives in init.js, which loads
// last, so any function here may call any other.

// ── Settings tab ─────────────────────────────────────────────────────────────
// _settingsLoaded is keyed on (city, IS_LIVE) so switching the city tab
// inside the Scan Areas card forces a fresh fetch. _settingsScanCity is
// the currently-selected city for the picker (lazy-inits to currentCity).
let _settingsLoaded    = false;
let _settingsLoadedFor = null;
let _settingsScanCity  = null;

function _activeScanCity() {
  return _settingsScanCity
      || (typeof currentCity === 'string' && currentCity)
      || 'milano';
}

// Milan only — the multi-city picker is parked in full.html.
const _SCAN_AREAS_CITY_ORDER = ['milano'];

function _renderScanAreasCityTabs() {
  const wrap = document.getElementById('scanAreasCityTabs');
  if (!wrap) return;
  const single = _SCAN_AREAS_CITY_ORDER.length < 2;
  wrap.hidden = single;
  const lbl = document.getElementById('scanAreasCityLabel');
  if (lbl) lbl.hidden = single;
  if (single) return;
  const active = _activeScanCity();
  wrap.innerHTML = _SCAN_AREAS_CITY_ORDER.map(code => {
    const meta  = (typeof _SCAN_CITY_META !== 'undefined' && _SCAN_CITY_META[code])
                  || { flag: '', label: code };
    const klass = code === active ? 'scan-areas-city-tab is-active' : 'scan-areas-city-tab';
    return `<button type="button" class="${klass}" data-scancity="${code}"
                    onclick="_setScanAreasCity('${code}')">
              <span>${meta.flag}</span><span>${escapeHtml(meta.label)}</span>
            </button>`;
  }).join('');
}

function _setScanAreasCity(code) {
  if (code === _activeScanCity()) return;
  _settingsScanCity     = code;
  _settingsLoaded       = false;
  _settingsCurrentAreas = [];
  _renderScanAreasCityTabs();
  _settingsLoad();
}

function _toAreaId(name) {
  // Convert display name to URL slug (matches api.py _to_url_slug / fetch_rentals.py to_url_slug)
  return name.normalize('NFKD').replace(/[\u0300-\u036f]/g, '')
    .toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
}

async function _settingsLoad() {
  // Treat "loaded with the local API actually available + same city" as
  // the cache key. Early calls (pre-IS_LIVE) and city-tab switches both
  // need to retry the fetch instead of being short-circuited.
  const city = _activeScanCity();
  if (_settingsLoaded && IS_LIVE && _settingsLoadedFor === city) return;
  _renderScanAreasCityTabs();

  const cardEl     = document.getElementById('scanAreasCard');
  const loadEl     = document.getElementById('settingsLoading');
  const pickerEl   = document.getElementById('areaPickerUI');
  const noPickerEl = document.getElementById('scanAreasNoPicker');
  if (cardEl) cardEl.hidden = !IS_LIVE;
  if (!IS_LIVE) {
    if (loadEl) {
      loadEl.textContent = 'Settings require the local Flask API. Run python3 api.py and reopen the dashboard.';
      loadEl.style.display = '';
    }
    if (pickerEl)   pickerEl.style.display   = 'none';
    if (noPickerEl) noPickerEl.style.display = 'none';
    return;
  }

  // Macro-area grid works for every city the slug mapping covers.
  // Cities with an empty mapping (Roma / Napoli / La Maddalena pre-
  // verification) show the "mapping not configured" note instead.
  if (pickerEl)   pickerEl.style.display   = 'none';
  if (noPickerEl) noPickerEl.style.display = 'none';
  if (loadEl)     loadEl.style.display     = '';

  try {
    const [resp] = await Promise.all([
      fetch(`/area-settings?city=${encodeURIComponent(city)}`),
      _loadSlugMapping(),
    ]);
    if (!resp.ok) throw new Error(`API error (${resp.status})`);
    const data = await resp.json();
    _settingsCurrentAreas = (data.areas || []).map(a => ({...a}));
    await _renderScanAreasGrid();
    if (loadEl) loadEl.style.display = 'none';
    _settingsLoaded    = true;
    _settingsLoadedFor = city;
  } catch (e) {
    if (loadEl) { loadEl.textContent = '✗ ' + e.message; loadEl.style.display = ''; }
  }
}

// ── Macro-area card grid (Scan Areas Settings redesign) ─────────────
// One card per Immobiliare macro-area = one HTTP request to the site.
// The macro list comes from /api/slug-mapping (single source of truth);
// the user's active selection is the union of macro slugs currently
// flagged active in _settingsCurrentAreas — keeping that in-memory state
// alive means the existing scanAreaMsd / drawer chip sync paths still
// work without modification.

let _SLUG_MAPPING = null;   // { city: { internal_slug: macro_slug } }

// Mirror of area_slug_mapper._PRETTIFY_OVERRIDES so the client renders
// the same labels without an extra round-trip. Single point of truth in
// Python; this is the dashboard's locked copy.
const _MACRO_PRETTY = {
  'abbiategrasso-chiesa-rossa':           'Abbiategrasso, Chiesa Rossa',
  'affori-bovisa':                        'Affori, Bovisa',
  'arco-della-pace-arena-pagano':         'Arco della Pace, Arena, Pagano',
  'bande-nere-inganni':                   'Bande Nere, Inganni',
  'bicocca-niguarda':                     'Bicocca, Niguarda',
  'bisceglie-baggio-olmi':                'Bisceglie, Baggio, Olmi',
  'cenisio-sarpi-isola':                  'Cenisio, Sarpi, Isola',
  'centrale-repubblica':                  'Centrale, Repubblica',
  'centro':                               'Centro',
  'cimiano-crescenzago-adriano':          'Cimiano, Crescenzago, Adriano',
  'citta-studi-susa':                     'Città Studi, Susa',
  'citta-studi':                          'Città Studi',
  'corvetto-rogoredo':                    'Corvetto, Rogoredo',
  'famagosta-barona':                     'Famagosta, Barona',
  'fiera-sempione-city-life-portello':     'Fiera, Sempione, CityLife, Portello',
  'forlanini':                            'Forlanini',
  'garibaldi-moscova-porta-nuova':        'Garibaldi, Moscova, Porta Nuova',
  'genova-ticinese':                      'Genova, Ticinese',
  'maggiolina-istria':                    'Maggiolina, Istria',
  'napoli-soderini':                      'Napoli, Soderini',
  'navigli':                              'Navigli',
  'pasteur-rovereto':                     'Pasteur, Rovereto',
  'ponte-lambro-santa-giulia':            'Ponte Lambro, Santa Giulia',
  'porta-romana-cadore-montenero':        'Porta Romana, Cadore, Montenero',
  'porta-venezia-indipendenza':           'Porta Venezia, Indipendenza',
  'porta-vittoria-lodi':                  'Porta Vittoria, Lodi',
  'precotto-turro':                       'Precotto, Turro',
  'quadronno-palestro-guastalla':         'Quadronno, Palestro, Guastalla',
  'ripamonti-vigentino':                  'Ripamonti, Vigentino',
  'san-siro-trenno':                      'San Siro, Trenno',
  'solari-washington':                    'Solari, Washington',
  'udine-lambrate':                       'Udine, Lambrate',
  'uptown-cascina-merlata-viale-certosa': 'Uptown, Cascina Merlata, Viale Certosa',
  'bocconi':          'Bocconi',
  'de-angeli':        'De Angeli',
  'giambellino':      'Giambellino',
  'city-life':        'CityLife',
  'citylife':         'CityLife',
  'qt8':              'QT8',
};

function _prettifyMacroSlug(slug) {
  if (!slug) return '';
  if (_MACRO_PRETTY[slug]) return _MACRO_PRETTY[slug];
  // Fallback: title-case each hyphen-token and comma-join. Imperfect
  // for unknown slugs but the override table covers everything verified.
  return slug.split('-')
    .map(w => w ? w[0].toUpperCase() + w.slice(1) : '')
    .join(', ');
}

async function _loadSlugMapping() {
  if (_SLUG_MAPPING) return _SLUG_MAPPING;
  try {
    const r = await fetch('/api/slug-mapping');
    _SLUG_MAPPING = r.ok ? await r.json() : {};
  } catch {
    _SLUG_MAPPING = {};
  }
  return _SLUG_MAPPING;
}

function _getMacroAreasForCity(city) {
  const cityMap = (_SLUG_MAPPING || {})[city] || {};
  const grouped = {};
  for (const [internal, macro] of Object.entries(cityMap)) {
    if (internal.startsWith('_')) continue;          // skip _doc / _comment / _notes
    if (typeof macro !== 'string' || !macro) continue;
    (grouped[macro] = grouped[macro] || []).push(internal);
  }
  return Object.entries(grouped)
    .map(([slug, internals]) => ({
      slug,
      displayName: _prettifyMacroSlug(slug),
      coversCount: internals.length,
    }))
    .sort((a, b) => a.displayName.localeCompare(b.displayName));
}

function _getActiveMacroSlugs() {
  // Source of truth: _settingsCurrentAreas. Each entry has id = macro slug
  // (after the redesign) and an active flag. The id field maps cleanly
  // to the URL slug fetch_rentals / fetch_listings expect.
  return new Set(_settingsCurrentAreas
    .filter(a => a && a.active)
    .map(a => a.id || a.name || ''));
}

function _renderAreaPicker() {
  // Repurposed name — kept so existing call sites still trigger a re-render.
  _renderScanAreasGrid();
}

async function _renderScanAreasGrid() {
  const grid    = document.getElementById('scanAreasGrid');
  const noPick  = document.getElementById('scanAreasNoPicker');
  const pickEl  = document.getElementById('areaPickerUI');
  if (!grid) return;
  await _loadSlugMapping();
  const city = _activeScanCity();
  const macros = _getMacroAreasForCity(city);
  if (!macros.length) {
    if (noPick) noPick.style.display = '';
    if (pickEl) pickEl.style.display = 'none';
    return;
  }
  if (noPick) noPick.style.display = 'none';
  if (pickEl) pickEl.style.display = '';
  const active = _getActiveMacroSlugs();
  const zonesLabel = (typeof t === 'function' && t('settings.zones')) || 'zones';
  grid.innerHTML = macros.map(m => {
    const on = active.has(m.slug);
    return `<label class="scan-area-card${on ? ' scan-area-card-on' : ''}" data-slug="${m.slug}">
              <input type="checkbox" ${on ? 'checked' : ''}>
              <div class="scan-area-card-content">
                <div class="scan-area-name">${escapeHtml(m.displayName)}</div>
                <div class="scan-area-meta">${m.coversCount} ${zonesLabel}</div>
              </div>
              <div class="scan-area-toggle">
                <div class="toggle-track${on ? ' toggle-on' : ''}"><div class="toggle-thumb"></div></div>
              </div>
            </label>`;
  }).join('');
  // Single delegated click — avoid one listener per card.
  grid.onclick = (ev) => {
    const card = ev.target.closest?.('.scan-area-card');
    if (!card) return;
    ev.preventDefault();
    const slug = card.dataset.slug;
    const wasOn = card.classList.contains('scan-area-card-on');
    _toggleMacroArea(slug, !wasOn);
  };
  _updateScanAreasCount();
}

function _toggleMacroArea(slug, enabled) {
  if (!slug) return;
  const existing = _settingsCurrentAreas.find(a => (a.id || a.name) === slug);
  if (existing) {
    existing.active = !!enabled;
  } else if (enabled) {
    _settingsCurrentAreas.push({
      id:     slug,
      name:   _prettifyMacroSlug(slug),
      active: true,
    });
  }
  // Cheap DOM update (no full re-render) so toggling 32 cards isn't N²
  const card = document.querySelector(`.scan-area-card[data-slug="${slug}"]`);
  if (card) {
    card.classList.toggle('scan-area-card-on', !!enabled);
    const trk = card.querySelector('.toggle-track');
    if (trk) trk.classList.toggle('toggle-on', !!enabled);
    const cb = card.querySelector('input[type="checkbox"]');
    if (cb) cb.checked = !!enabled;
  }
  _updateScanAreasCount();
}

function _updateScanAreasCount() {
  const el = document.getElementById('scanAreasCount');
  if (!el) return;
  const total  = _getMacroAreasForCity(_activeScanCity()).length;
  const active = _getActiveMacroSlugs().size;
  const lbl    = (typeof t === 'function' && t('settings.areas_active')) || 'active';
  el.textContent = `${active} / ${total} ${lbl}`;
}

// ── Scoring settings panel ────────────────────────────────────────────────────
const _SCORE_FIELDS = [
  { key: 'w_price',           label: 'Weight: price vs comps',   desc: '0–1, default 0.45', type: 'range', min: 0, max: 1, step: 0.05 },
  { key: 'w_property',        label: 'Weight: property quality', desc: '0–1, default 0.25', type: 'range', min: 0, max: 1, step: 0.05 },
  { key: 'w_location',        label: 'Weight: location/geo',     desc: '0–1, default 0.20', type: 'range', min: 0, max: 1, step: 0.05 },
  { key: 'w_penalty',         label: 'Weight: penalty',          desc: '0–1, default 0.10', type: 'range', min: 0, max: 1, step: 0.05 },
  // (Price bands and € impact belonged to the old stepped price curve; the
  //  smooth curve — scoring.price_score_smooth — doesn't read them.)
  { key: 'penalty_dom_warn',  label: 'Days on market: warn',     desc: 'above this → mild penalty (default 30)', type: 'number', step: 1 },
  { key: 'penalty_dom_stale', label: 'Days on market: stale',    desc: 'above this → heavy penalty (default 60)', type: 'number', step: 1 },
  { key: 'min_comps',         label: 'Min comps per radius',     desc: 'fallback when fewer than ~20 flats lie within 1.5 km: min listings before widening (default 5)', type: 'number', step: 1 },
];

// ── Badge threshold settings ───────────────────────────────────────────────
// Keep these in lock-step with scoring.py's _DEFAULT_SETTINGS — defaults
// shown here are what the UI hints, _GEM_DEFAULTS below is what we fall
// back to when scoring_settings.json hasn't loaded yet.
const _GEM_FIELDS = [
  { key: 'rent_gem_z_max',            label: 'Max discount (z)',      desc: 'discount ÷ its uncertainty ≤ this, e.g. −1.3 ≈ −23 % (default −1.3)', step: 0.1, min: -5, max: 0 },
  { key: 'rent_gem_min_comps',        label: 'Min comparables',       desc: 'comparable flats behind the benchmark (default 20)', step: 1, min: 5, max: 60 },
  { key: 'rent_gem_property_min',     label: 'Min property score',    desc: 'score_property ≥ this (default 62)',     step: 1, min: 0, max: 100 },
  { key: 'rent_gem_location_min',     label: 'Min location score',    desc: 'score_location ≥ this (default 80)',     step: 1, min: 0, max: 100 },
  { key: 'rent_gem_penalty_min',      label: 'Min penalty score',     desc: 'score_penalty ≥ this (default 75)',      step: 1, min: 0, max: 100 },
  { key: 'rent_gem_metro_max_m',      label: 'Max metro distance (m)', desc: 'metro_nearest_dist_m ≤ this (default 700 m)', step: 50, min: 0, max: 5000 },
  { key: 'rent_gem_min_sqm_per_room', label: 'Min m² per room',       desc: 'sqm / rooms ≥ this — no crammed flats (default 22)', step: 1, min: 0, max: 60 },
];
const _GV_FIELDS = [
  { key: 'rent_gv_z_max',             label: 'Max discount (z)',      desc: 'discount ÷ its uncertainty ≤ this, e.g. −1.1 ≈ −20 % (default −1.1)', step: 0.1, min: -5, max: 0 },
  { key: 'rent_gv_min_comps',         label: 'Min comparables',       desc: 'comparable flats behind the benchmark (default 15)', step: 1, min: 5, max: 60 },
  { key: 'rent_gv_property_min',      label: 'Min property score',    desc: 'score_property ≥ this (default 45)',     step: 1, min: 0, max: 100 },
  { key: 'rent_gv_location_min',      label: 'Min location score',    desc: 'score_location ≥ this (default 50)',     step: 1, min: 0, max: 100 },
  { key: 'rent_gv_penalty_min',       label: 'Min penalty score',     desc: 'score_penalty ≥ this (default 60)',      step: 1, min: 0, max: 100 },
  { key: 'rent_label_delta_floor',    label: 'Deepest discount (%)',  desc: 'deeper than this → "verify first", no badge (both badges; default −30)', step: 1, min: -60, max: 0 },
  { key: 'rent_label_max_age_days',   label: 'Max days on market',    desc: 'first seen longer ago → no badge (both badges; default 60)', step: 5, min: 0, max: 365 },
];

// Rental badges (scoring.rental_labels). Sales keep their own gem_*/gv_*
// thresholds in scoring_settings.json.
const _GEM_DEFAULTS = {
  rent_gem_z_max: -1.3, rent_gem_min_comps: 20, rent_gem_property_min: 62,
  rent_gem_location_min: 80, rent_gem_penalty_min: 75, rent_gem_metro_max_m: 700,
  rent_gem_min_sqm_per_room: 22,
  rent_gv_z_max: -1.1, rent_gv_min_comps: 15, rent_gv_property_min: 45,
  rent_gv_location_min: 50, rent_gv_penalty_min: 60,
  rent_label_delta_floor: -30, rent_label_max_age_days: 60,
};

function _buildGemGrid(fields, gridId, settings) {
  const grid = document.getElementById(gridId);
  if (!grid) return;
  grid.innerHTML = '';
  for (const f of fields) {
    const val = (settings[f.key] ?? _GEM_DEFAULTS[f.key] ?? '');
    const div = document.createElement('div');
    div.className = 'ss-field';
    div.innerHTML = `
      <label for="gs_${f.key}">${f.label}</label>
      <input type="number" id="gs_${f.key}" name="${f.key}"
             step="${f.step}" min="${f.min}" max="${f.max}" value="${val}">
      <span class="ss-desc">${f.desc}</span>`;
    grid.appendChild(div);
  }
}

function _loadGemSettings() {
  _buildGemGrid(_GEM_FIELDS, 'gemSettingsGrid', scoreSettings);
  _buildGemGrid(_GV_FIELDS,  'gvSettingsGrid',  scoreSettings);
}

function _buildScoreSettingsGrid(settings) {
  const grid = document.getElementById('scoreSettingsGrid');
  if (!grid) return;
  grid.innerHTML = '';
  for (const f of _SCORE_FIELDS) {
    const val = settings[f.key] ?? '';
    const div = document.createElement('div');
    div.className = 'ss-field';
    if (f.type === 'range') {
      div.innerHTML = `
        <label for="ss_${f.key}">${f.label}</label>
        <div class="ss-range-row">
          <input type="range" id="ss_${f.key}" name="${f.key}"
                 min="${f.min}" max="${f.max}" step="${f.step}" value="${val}">
          <span class="ss-range-val" id="ss_${f.key}_val">${Number(val).toFixed(2)}</span>
        </div>
        <span class="ss-desc">${f.desc}</span>`;
      grid.appendChild(div);
      const inp = div.querySelector('input');
      const valEl = div.querySelector('.ss-range-val');
      inp.addEventListener('input', () => { valEl.textContent = Number(inp.value).toFixed(2); });
    } else {
      div.innerHTML = `
        <label for="ss_${f.key}">${f.label}</label>
        <input type="number" id="ss_${f.key}" name="${f.key}" step="${f.step}" value="${val}">
        <span class="ss-desc">${f.desc}</span>`;
      grid.appendChild(div);
    }
  }
}

async function _loadScoreSettings() {
  if (IS_LIVE) {
    try {
      const r = await fetch('/api/scoring-settings');
      if (r.ok) {
        const data = await r.json();
        Object.assign(scoreSettings, data);
      }
    } catch { /* use defaults */ }
  }
  try { _buildScoreSettingsGrid(scoreSettings); } catch(e) { console.error('scoreGrid:', e); }
}

// ── Data Quality dashboard ────────────────────────────────────────────────────
const _DQ_FIELDS = {
  critical:  ['id','rent_mo','sqm','ask_psqm','latitude','longitude','omi_zona','omi_loc_min','omi_loc_max','omi_source'],
  important: ['floor_n','elevator','condition','is_external','rooms','address','neighbourhood','metro_nearest_dist_m','metro_walk_min','metro_nearest_line','comps_median','comps_n','comps_confidence','geo_score','score_physical'],
  useful:    ['has_balcony','has_parking','energy_class','heating_type','furnished','bathrooms','spese_condominiali','days_on_market','photo_count','tram_nearest_dist_m','supermarket_nearest_dist_m','park_nearest_dist_m','university_nearest_dist_m','score_explanation','suggested_rent_mo'],
};
const _DQ_GRADE_COLORS      = {A:'#16a34a',B:'#0d9488',C:'#d97706',D:'#ea580c',F:'#dc2626'};
const _DQ_OMI_COLORS        = {polygon:'#16a34a',centroid:'#d97706','nominatim+polygon':'#2563eb','nominatim+centroid':'#7c3aed',keyword_fallback:'#dc2626',no_coordinates:'#9f1239',failed:'#6b7280'};
const _DQ_OMI_CLS           = {polygon:'omi-polygon',centroid:'omi-centroid','nominatim+polygon':'omi-nominatim','nominatim+centroid':'omi-nominatim',keyword_fallback:'omi-keyword',no_coordinates:'omi-nocoord'};

let _dqData      = null;
let _dqFetchedAt = 0;
let _dqSortKey   = 'completeness_pct';
let _dqSortDir   = 'asc';
let _dqActiveCell = null;
let _dqChart      = null;

async function _dqLoad(force = false) {
  if (!IS_LIVE) return;   // data-quality endpoint only available locally
  if (!force && _dqData && Date.now() - _dqFetchedAt < 60000) {
    _dqRender(); return;
  }
  const sk = document.getElementById('dqSkeleton');
  const ct = document.getElementById('dqContent');
  const em = document.getElementById('dqEmpty');
  const er = document.getElementById('dqError');
  if (sk) sk.style.display = 'block';
  if (ct) ct.style.display = 'none';
  if (em) em.style.display = 'none';
  if (er) er.style.display = 'none';
  try {
    const r = await fetch('/data-quality');
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    _dqData = await r.json();
    _dqFetchedAt = Date.now();
    _dqRender();
  } catch(e) {
    if (sk) sk.style.display = 'none';
    if (er) { er.style.display = 'block'; er.textContent = 'Could not load data quality — is the API running? (' + e.message + ')'; }
  }
}

function _dqRender() {
  const sk = document.getElementById('dqSkeleton');
  const ct = document.getElementById('dqContent');
  const em = document.getElementById('dqEmpty');
  if (sk) sk.style.display = 'none';
  if (!_dqData || !_dqData.summary) return;
  if (_dqData.summary.total_listings === 0) { if (em) em.style.display = 'block'; return; }
  if (ct) ct.style.display = 'block';
  _dqRenderCards(_dqData.summary);
  _dqRenderHeatmap(_dqData.summary.field_coverage);
  _dqRenderDonut(_dqData.summary.omi_source_breakdown);
  _dqPopulateFieldFilter(_dqData.summary.field_coverage);
  _dqRenderTable(_dqData.listings);
  const ts = document.getElementById('dqLastUpdated');
  if (ts) ts.textContent = 'Updated ' + new Date(_dqFetchedAt).toLocaleTimeString();
}

function _dqBuildCards(s, targetId) {
  const el = document.getElementById(targetId);
  if (!el) return;
  const avg  = s.avg_completeness_pct;
  const avgC = avg >= 80 ? '#16a34a' : avg >= 60 ? '#d97706' : '#dc2626';
  const re   = s.listings_needing_reenrichment;
  const cg   = Object.keys(s.critical_field_gaps || {}).length;
  const gd   = s.grade_distribution || {};
  const tot  = s.total_listings;
  const grades = ['A','B','C','D','F'];
  const barSegs = grades.map(g => {
    const w = tot ? (gd[g]||0)/tot*100 : 0;
    return w > 0 ? `<div class="dq-grade-seg" style="width:${w}%;background:${_DQ_GRADE_COLORS[g]}"></div>` : '';
  }).join('');
  const gradeLabels = grades.filter(g => gd[g]).map(g =>
    `<span style="color:${_DQ_GRADE_COLORS[g]};font-size:12px">${g}: ${gd[g]}</span>`
  ).join(' ');
  el.innerHTML = `
    <div class="dq-card">
      <div class="dq-card-value" style="color:${avgC}">${avg}%</div>
      <div class="dq-card-label">Avg completeness</div>
    </div>
    <div class="dq-card">
      <div class="dq-card-value" style="color:${re>0?'var(--coral-ink)':'var(--green-ink)'}">${re}${re>0?' <span style="display:inline-block;width:9px;height:9px;background:var(--coral-ink);border-radius:50%;vertical-align:middle;margin-left:2px"></span>':''}</div>
      <div class="dq-card-label">Need re-enrichment</div>
    </div>
    <div class="dq-card">
      <div class="dq-card-value" style="color:${cg>0?'var(--coral-ink)':'var(--green-ink)'}">${cg}</div>
      <div class="dq-card-label">Critical fields &lt; 80%</div>
    </div>
    <div class="dq-card" style="flex:2">
      <div class="dq-card-label" style="margin-bottom:6px">Grade distribution (${tot} listings)</div>
      <div class="dq-grade-bar">${barSegs}</div>
      <div style="display:flex;gap:10px;margin-top:5px;flex-wrap:wrap">${gradeLabels}</div>
    </div>`;
}
function _dqRenderCards(s) { _dqBuildCards(s, 'dqCards'); }

function _dqRenderHeatmap(cov) {
  const el = document.getElementById('dqHeatmap');
  if (!el) return;
  const tiers = [
    {label:'Critical fields', fields:_DQ_FIELDS.critical},
    {label:'Important fields', fields:_DQ_FIELDS.important},
    {label:'Useful fields',    fields:_DQ_FIELDS.useful},
  ];
  el.innerHTML = tiers.map(t => {
    const cells = t.fields.map(f => {
      const pct = cov[f] ?? 0;
      const cls = pct>=90?'cov-green':pct>=70?'cov-amber':pct>=50?'cov-orange':'cov-red';
      const act = _dqActiveCell === f ? ' dq-active' : '';
      return `<div class="dq-cell ${cls}${act}" onclick="_dqCellClick('${f}')" title="${f}: ${pct}% coverage">
        <div class="dq-cell-name">${f}</div>
        <div class="dq-cell-pct">${pct}%</div>
      </div>`;
    }).join('');
    return `<div class="dq-heatmap-row">
      <div class="dq-heatmap-tier">${t.label}</div>
      <div class="dq-heatmap-cells">${cells}</div>
    </div>`;
  }).join('');
}

function _dqCellClick(field) {
  const sel = document.getElementById('dqFieldFilter');
  _dqActiveCell = _dqActiveCell === field ? null : field;
  if (sel) sel.value = _dqActiveCell || '';
  _dqRenderHeatmap(_dqData.summary.field_coverage);
  _dqRenderTable(_dqData.listings);
}

function _dqRenderDonut(breakdown) {
  const canvas = document.getElementById('dqDonut');
  const legend = document.getElementById('dqDonutLegend');
  if (!canvas || !breakdown) return;
  const entries = Object.entries(breakdown).sort((a,b) => b[1]-a[1]);
  const labels  = entries.map(e => e[0]);
  const values  = entries.map(e => e[1]);
  const colors  = labels.map(l => _DQ_OMI_COLORS[l] || '#9ca3af');
  const total   = values.reduce((a,b)=>a+b,0);
  if (_dqChart) { _dqChart.destroy(); _dqChart = null; }
  if (typeof Chart !== 'undefined' && total > 0) {
    _dqChart = new Chart(canvas, {
      type: 'doughnut',
      data: { labels, datasets: [{ data: values, backgroundColor: colors, borderWidth: 1, borderColor: 'var(--bg-card)' }] },
      options: {
        responsive: false,
        cutout: '65%',
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: ctx => ` ${ctx.label}: ${ctx.raw} (${Math.round(ctx.raw/total*100)}%)` } },
        },
      },
    });
  }
  if (legend) {
    legend.innerHTML = entries.map(([lbl, cnt]) => {
      const c = _DQ_OMI_COLORS[lbl] || '#9ca3af';
      return `<div style="display:flex;align-items:center;gap:7px">
        <div style="width:11px;height:11px;border-radius:2px;background:${c};flex-shrink:0"></div>
        <span>${lbl}</span>
        <span style="color:var(--text-secondary)">${cnt} (${total?Math.round(cnt/total*100):0}%)</span>
      </div>`;
    }).join('');
  }
}

function _dqPopulateFieldFilter(cov) {
  const sel = document.getElementById('dqFieldFilter');
  if (!sel) return;
  const all = [..._DQ_FIELDS.critical, ..._DQ_FIELDS.important, ..._DQ_FIELDS.useful];
  sel.innerHTML = `<option value="">All fields</option>` +
    all.map(f => `<option value="${f}">${f} (${cov[f]??0}%)</option>`).join('');
  if (_dqActiveCell) sel.value = _dqActiveCell;
}

function _dqSortComparator(sortKey, sortDir) {
  return (a, b) => {
    let va = a[sortKey], vb = b[sortKey];
    if (sortKey === 'n_critical')  { va = (a.critical_missing||[]).length;  vb = (b.critical_missing||[]).length; }
    if (sortKey === 'n_important') { va = (a.important_missing||[]).length; vb = (b.important_missing||[]).length; }
    if (va == null) va = sortDir === 'asc' ? '\uFFFF' : '';
    if (vb == null) vb = sortDir === 'asc' ? '\uFFFF' : '';
    if (typeof va === 'string') return sortDir === 'asc' ? va.localeCompare(vb) : vb.localeCompare(va);
    return sortDir === 'asc' ? va - vb : vb - va;
  };
}

function _dqRenderTable(rows) {
  const grade  = document.getElementById('dqGradeFilter')?.value || 'all';
  const field  = document.getElementById('dqFieldFilter')?.value || '';
  const src    = document.getElementById('dqSrcFilter')?.value || 'all';
  const tbody  = document.getElementById('dqTblBody');
  const count  = document.getElementById('dqTblCount');
  if (!tbody) return;

  let data = rows.slice();
  const gradeMap = {F:['F'], DF:['D','F'], CDF:['C','D','F']};
  if (gradeMap[grade]) data = data.filter(r => gradeMap[grade].includes(r.grade));
  if (field) data = data.filter(r =>
    [...(r.critical_missing||[]), ...(r.important_missing||[]), ...(r.useful_missing||[])].includes(field)
  );
  if (src !== 'all') data = data.filter(r => r.source === src);

  data.sort(_dqSortComparator(_dqSortKey, _dqSortDir));
  const more = document.getElementById('dqMore');

  if (!data.length) {
    tbody.innerHTML = `<tr><td colspan="9" style="text-align:center;color:var(--text-secondary);padding:20px">${escapeHtml(t('empty.no_results'))}</td></tr>`;
    if (count) count.textContent = '';
    if (more) more.hidden = true;
    return;
  }

  // 50 rows at a time — the full table (5k+ rows) made Tools 180,000 px tall.
  const shown = data.slice(0, _dqLimit);
  if (more) {
    more.hidden = shown.length >= data.length;
    more.onclick = () => { _dqLimit += 50; _dqRenderTable(rows); };
  }
  tbody.innerHTML = shown.map(r => {
    const critN = (r.critical_missing||[]).length;
    const impN  = (r.important_missing||[]).length;
    const critTip = (r.critical_missing||[]).join(', ') || '—';
    const impTip  = (r.important_missing||[]).join(', ') || '—';
    const omiCls  = _DQ_OMI_CLS[r.omi_source] || 'omi-other';
    const omiSrc  = r.omi_source || 'unknown';
    return `<tr>
      <td style="max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(r.neighbourhood||'')}">${escapeHtml(r.neighbourhood||'—')}</td>
      <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(r.address||'')}">${escapeHtml(r.address||'—')}</td>
      <td>${escapeHtml(r.source||'—')}</td>
      <td><span class="grade-badge-${escapeHtml(r.grade)}" style="padding:2px 9px;border-radius:10px;font-size:12px;font-weight:700">${escapeHtml(r.grade)}</span></td>
      <td>${r.completeness_pct}%</td>
      <td>${critN>0?`<span class="dq-pill dq-pill-crit" title="${escapeHtml(critTip)}">${critN}</span>`:'<span style="color:var(--text-secondary)">—</span>'}</td>
      <td>${impN>0?`<span class="dq-pill dq-pill-imp" title="${escapeHtml(impTip)}">${impN}</span>`:'<span style="color:var(--text-secondary)">—</span>'}</td>
      <td><span class="omi-tag ${omiCls}">${escapeHtml(omiSrc)}</span></td>
      <td><button type="button" class="btn btn-ghost btn-sm" data-dq-id="${escapeHtml(String(r.id))}" onclick="_dqClearSingle(this.dataset.dqId,this)">Re-enrich</button></td>
    </tr>`;
  }).join('');

  if (count) count.textContent = t('scroll.of', { a: fmt(shown.length), b: fmt(data.length) });
}
let _dqLimit = 50;

// Tools cards fold away (the header toggles them); the open/closed state
// is remembered. Scanner and alerts start open, the rest folded.
const _CARD_OPEN_DEFAULT = new Set(['scannerCard', 'digestSettingsWrap']);
function initCollapsibleCards() {
  let state = {};
  try { state = JSON.parse(localStorage.getItem('lume_tools_open') || '{}'); } catch { /* defaults */ }
  document.querySelectorAll('#settingsView .settings-card').forEach((card, i) => {
    if (card.dataset.collapsible) return;
    const head = card.querySelector(':scope > .settings-card-header');
    if (!head) return;
    const key = card.id || 'card' + i;
    card.dataset.collapsible = key;
    card.classList.add('is-collapsible');
    const open = key in state ? state[key] : _CARD_OPEN_DEFAULT.has(key);
    card.classList.toggle('is-collapsed', !open);
    const ind = document.createElement('span');
    ind.className = 'collapse-ind';
    ind.innerHTML = icon('chevron-down');
    head.appendChild(ind);
    head.setAttribute('role', 'button');
    head.tabIndex = 0;
    head.setAttribute('aria-expanded', String(open));
    const toggle = () => {
      const nowOpen = card.classList.toggle('is-collapsed') === false;
      head.setAttribute('aria-expanded', String(nowOpen));
      state[key] = nowOpen;
      try { localStorage.setItem('lume_tools_open', JSON.stringify(state)); } catch { /* not persisted */ }
    };
    head.addEventListener('click', e => { if (!e.target.closest('button, a, input, select')) toggle(); });
    head.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } });
  });
}

async function _dqClearSingle(id, btn) {
  btn.disabled = true; btn.textContent = '…';
  try {
    const r = await fetch('/cache-clear-single', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({listing_id: id}),
    });
    btn.textContent = r.ok ? 'Cleared' : 'Failed';
    btn.style.color = r.ok ? 'var(--green-ink)' : 'var(--coral-ink)';
  } catch(e) { btn.textContent = '✗'; btn.disabled = false; }
}


// ── Photo reading status (Tools) ─────────────────────────────────────────────
// photo_eval.status via /api/photo-status: today vs the daily budget, the
// queue, a reading in progress. Polled every 20 s while Tools is open.
let _psTimer = null;
async function loadPhotoStatus() {
  const card = document.getElementById('photoStatusCard');
  if (!card) return;
  card.hidden = !(typeof IS_LIVE !== 'undefined' && IS_LIVE);
  if (card.hidden) return;
  let st = null;
  try { st = await fetch('/api/photo-status').then(r => r.json()); } catch { /* API down */ }
  const box = document.getElementById('photoStatus');
  if (!st || !box) return;
  const tile = (k, v, s = '') => `<div class="ps-tile"><div class="ps-k">${escapeHtml(k)}</div><div class="ps-v">${v}</div>${s ? `<div class="ps-s">${escapeHtml(s)}</div>` : ''}</div>`;
  const p = st.progress;
  const pct = p && p.total ? Math.round(100 * p.done / p.total) : 0;
  box.innerHTML = [
    tile(t('ps.today'), `${fmt(st.today)} <span class="ps-of">/ ${fmt(st.budget)}</span>`,
         st.today >= st.budget ? t('ps.budget_used') : t('ps.budget_left', { n: fmt(st.budget - st.today) })),
    tile(t('ps.queue'), fmt(st.queued_new + st.queued_again),
         t('ps.queue_sub', { a: fmt(st.queued_new), b: fmt(st.queued_again) })),
    tile(t('ps.now'), p ? `${fmt(p.done)} <span class="ps-of">/ ${fmt(p.total)}</span>` : escapeHtml(t(st.busy ? 'ps.busy' : 'ps.idle')),
         p ? `${pct}%` : (st.last_read ? t('ps.last', { d: fmtDate(new Date(st.last_read)) + ' ' + st.last_read.slice(11, 16) }) : '')),
    tile(t('ps.cache'), fmt(st.cached_photos), t('ps.cache_sub')),
  ].join('') + (p ? `<div class="ps-bar"><span style="width:${pct}%"></span></div>` : '');
  clearTimeout(_psTimer);
  if (typeof activeTab !== 'undefined' && activeTab === 'settings') _psTimer = setTimeout(loadPhotoStatus, 20000);
}
