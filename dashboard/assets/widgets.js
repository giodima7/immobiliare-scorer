// Lume dashboard — shared widgets: Milan areas, multiselects, score-ring animation, cross-source de-duplication, toasts.
// Plain script (no bundler); shares globals with the other assets/*.js files.
// Declarations only: code that runs at start-up lives in init.js, which loads
// last, so any function here may call any other.

// ── Milan areas — Immobiliare.it macrozone names ──────────────────────────────
// Names match the macrozone/microzone values returned by the Immobiliare.it API
// so the URL slug derived from each name lands on a valid search page.
const MILAN_AREAS = {
  // ── Fascia A — prime ─────────────────────────────────────────────────────────
  // Includes both Immobiliare.it macrozone names and Idealista sub-zone names
  // that have been normalised to a canonical equivalent (see neighbourhood_synonyms.json).
  A: ['Amendola - Buonarroti','Arco della Pace','Brera','Buenos Aires',
      'Cadorna - Castello','Carrobbio','Centrale','Corso Genova','Duomo',
      'Garibaldi - Corso Como','Guastalla','Indipendenza','Isola',
      'Melchiorre Gioia','Moscova','Navigli - Darsena','Paolo Sarpi',
      'Piave - Tricolore','Porta Nuova',"Porta Romana - Medaglie d'Oro",
      'Porta Venezia','Sempione','Solari','Ticinese','Vincenzo Monti','Washington'],
  // ── Fascia B — semi-central ──────────────────────────────────────────────────
  B: ['Arena','Ascanio Sforza','Bocconi','Borgogna - Largo Augusto','Cadore',
      'Cenisio','Città Studi','Corso San Gottardo','De Angeli','Dezza','Farini',
      'Fiera','Lanza','Lodi - Brenta','Maggiolina','Missori','Montenero',
      'Monte Rosa - Lotto','Morgagni','Palestro','Plebisciti - Susa',
      'Portello - Tre Torri','Quadronno - Crocetta','Repubblica','San Babila',
      "Sant'Ambrogio",'Scala - Manzoni','Turati','Vercelli - Wagner',
      'San Vittore','Zara'],
  // ── Fascia C — peripheral ────────────────────────────────────────────────────
  // Also includes Idealista-specific sub-zones that don't map to any Immobiliare area.
  C: ['Adriano','Affori','Argonne - Corsica','Bande Nere - Gambara','Bicocca',
      'Bisceglie','Bonola','Bovisa','Bruzzano','Cascina dei Pomi','Cascina Merlata',
      'Certosa','Cimiano','Comasina','Corvetto','Crescenzago','Dergano',
      'Famagosta','Fondazione Prada - Scalo Romana','Forlanini','Frua',
      'Giambellino','Govone','Gratosoglio','Lambrate','Lorenteggio','Loreto',
      'Martini - Insubria','Mecenate','Musocco','Niguarda','Pasteur',
      'QT8','Quarto Oggiaro','Ripamonti','Rogoredo','Romagna','Romolo',
      'San Siro','Teodosio - Porpora','Tibaldi - Pezzotti','Tripoli - Soderini',
      'Ungheria','Via Padova','Vigentino'],
};
// Group headers in the area picker (translated at render time).
const FASCIA_LABEL = { A: 'areas.group_A', B: 'areas.group_B', C: 'areas.group_C' };

// Populate the mobile drawer area searchable multiselects with the available
// area list. Uses active scan areas when the local server is available,
// otherwise falls back to all MILAN_AREAS so options always appear in static
// (Cloudflare Pages) mode.
function _populateDrawerAreaFromActive() {
  // Active areas from server, or every known Milan area as fallback
  const activeNames = (_settingsCurrentAreas || []).filter(a => a.active).length
    ? (_settingsCurrentAreas || []).filter(a => a.active).map(a => a.name).sort((a, b) => a.localeCompare(b))
    : Object.values(MILAN_AREAS).flat().sort((a, b) => a.localeCompare(b));

  if (typeof dRentAreaMsearch !== 'undefined' && dRentAreaMsearch) {
    dRentAreaMsearch.setAllAreas(activeNames);
  }
}

// ── Searchable multiselect for drawer area pickers ──
// Builds a tag-style multiselect with a typeahead dropdown. Returns an API
// matching the old chip helpers: getSelected / setSelected / setAllAreas.
function AreaMSearch(inputId, tagsId, dropdownId, allAreas, onChange) {
  const input    = document.getElementById(inputId);
  const tagsEl   = document.getElementById(tagsId);
  const dropdown = document.getElementById(dropdownId);
  if (!input || !tagsEl || !dropdown) return null;

  const wrap = input.closest('.area-msearch');
  let options = [...(allAreas || [])];
  const selected = new Set();
  let focusedIdx = -1;
  let lastFiltered = [];

  function _emit() { if (typeof onChange === 'function') onChange([...selected]); }

  function _renderTags() {
    tagsEl.innerHTML = '';
    [...selected].forEach(name => {
      const tag = document.createElement('span');
      tag.className = 'area-msearch-tag';
      tag.textContent = name;
      const x = document.createElement('button');
      x.type = 'button';
      x.setAttribute('aria-label', t('chip.remove') + ': ' + name);
      x.textContent = '×';
      x.addEventListener('mousedown', e => {
        e.preventDefault(); e.stopPropagation();
        selected.delete(name);
        _renderTags();
        _renderDropdown(input.value);
        _emit();
      });
      tag.appendChild(x);
      tagsEl.appendChild(tag);
    });
  }

  function _renderDropdown(q = '') {
    dropdown.innerHTML = '';
    const ql = (q || '').toLowerCase();
    const list = options.filter(n => !ql || n.toLowerCase().includes(ql));
    lastFiltered = list;
    focusedIdx = -1;
    if (!list.length) {
      const empty = document.createElement('div');
      empty.className = 'area-msearch-empty';
      empty.textContent = t(options.length ? 'fb.no_match' : 'fb.no_areas');
      dropdown.appendChild(empty);
      return;
    }
    list.forEach((name, i) => {
      const opt = document.createElement('div');
      opt.className = 'area-msearch-option' + (selected.has(name) ? ' selected' : '');
      opt.dataset.idx = String(i);
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.className = 'opt-check';
      cb.checked = selected.has(name);
      cb.tabIndex = -1;
      const label = document.createElement('span');
      label.textContent = name;
      opt.appendChild(cb);
      opt.appendChild(label);
      // Use mousedown to fire before input loses focus
      opt.addEventListener('mousedown', e => {
        e.preventDefault();
        if (selected.has(name)) selected.delete(name);
        else                    selected.add(name);
        _renderTags();
        _renderDropdown(input.value);
        _emit();
        input.focus();
      });
      dropdown.appendChild(opt);
    });
  }

  function open() {
    dropdown.hidden = false;
    _renderDropdown(input.value);
  }
  function close() {
    dropdown.hidden = true;
    focusedIdx = -1;
  }

  // Clicking the wrap focuses the input
  wrap.addEventListener('click', e => {
    if (e.target === wrap || e.target === tagsEl) input.focus();
  });

  input.addEventListener('focus', open);
  input.addEventListener('input', () => _renderDropdown(input.value));
  input.addEventListener('blur', () => {
    // Delay close to allow option mousedown to fire on touch devices
    setTimeout(close, 120);
  });
  input.addEventListener('keydown', e => {
    if (e.key === 'Backspace' && !input.value && selected.size) {
      const last = [...selected].pop();
      selected.delete(last);
      _renderTags(); _renderDropdown(input.value); _emit();
      return;
    }
    if (e.key === 'Escape') { close(); input.blur(); return; }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (dropdown.hidden) open();
      const opts = dropdown.querySelectorAll('.area-msearch-option');
      if (!opts.length) return;
      focusedIdx = e.key === 'ArrowDown'
        ? Math.min(focusedIdx + 1, opts.length - 1)
        : Math.max(focusedIdx - 1, 0);
      opts.forEach((o, i) => o.classList.toggle('focused', i === focusedIdx));
      opts[focusedIdx].scrollIntoView({ block: 'nearest' });
      return;
    }
    if (e.key === 'Enter') {
      e.preventDefault();
      if (focusedIdx >= 0 && lastFiltered[focusedIdx]) {
        const name = lastFiltered[focusedIdx];
        if (selected.has(name)) selected.delete(name);
        else                    selected.add(name);
        _renderTags(); _renderDropdown(input.value); _emit();
      }
    }
  });

  _renderTags();

  return {
    getSelected: () => [...selected],
    setSelected: (arr) => {
      selected.clear();
      (arr || []).forEach(v => selected.add(v));
      _renderTags();
      if (!dropdown.hidden) _renderDropdown(input.value);
    },
    setAllAreas: (arr) => {
      options = [...(arr || [])];
      // Drop any currently-selected items that aren't in the new list
      [...selected].forEach(v => { if (!options.includes(v)) selected.delete(v); });
      _renderTags();
      if (!dropdown.hidden) _renderDropdown(input.value);
    },
    clear: () => { selected.clear(); _renderTags(); if (!dropdown.hidden) _renderDropdown(input.value); },
  };
}

// Instances are created lazily after the drawer DOM exists. They're
// referenced by name in _populateDrawerAreaFromActive and the drawer
// sync/apply helpers below.
let dRentAreaMsearch = null;

// ── Multi-select component ────────────────────────────────────────────────────
function buildMultiSelect({ wrapId, triggerId, panelId, searchId, listId,
                             clearId, doneId, onChange, items = null }) {
  const trigger = document.getElementById(triggerId);
  const panel   = document.getElementById(panelId);
  const search  = document.getElementById(searchId);
  const list    = document.getElementById(listId);
  const selected = new Set();

  function _makeOption(key, label) {
    const lbl = document.createElement('label');
    lbl.className = 'msd-option';
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.value = key; cb.checked = selected.has(key);
    cb.addEventListener('change', () => {
      cb.checked ? selected.add(key) : selected.delete(key);
      updateTrigger(); onChange([...selected]);
    });
    lbl.appendChild(cb);
    lbl.append(' ' + label);
    return lbl;
  }

  function renderList(q = '') {
    list.innerHTML = '';
    const ql = q.toLowerCase();
    if (items) {
      for (const item of items) {
        const key   = typeof item === 'string' ? item : item.key;
        const label = typeof item === 'string' ? item : item.label;
        if (ql && !label.toLowerCase().includes(ql)) continue;
        list.appendChild(_makeOption(key, label));
      }
    } else {
      for (const [fascia, names] of Object.entries(MILAN_AREAS)) {
        const matches = names.filter(n => !ql || n.toLowerCase().includes(ql));
        if (!matches.length) continue;
        const hdr = document.createElement('div');
        hdr.className = 'msd-group-header';
        hdr.textContent = t(FASCIA_LABEL[fascia]);
        list.appendChild(hdr);
        for (const name of matches) list.appendChild(_makeOption(name, name));
      }
    }
  }

  function updateTrigger() {
    const n = selected.size;
    const label = n === 0 ? t('fb.all_areas')
      : n === 1 ? [...selected][0]
      : t('fb.n_areas', { n });
    trigger.innerHTML = `<span class="msd-label">${escapeHtml(label)}</span>${icon('chevron-down')}`;
    trigger.classList.toggle('has-selection', n > 0);
    trigger.setAttribute('aria-expanded', String(isOpen()));
  }

  function open()  { panel.classList.add('is-open');    trigger.setAttribute('aria-expanded', 'true'); }
  function close() { panel.classList.remove('is-open'); trigger.setAttribute('aria-expanded', 'false'); }
  function isOpen(){ return panel.classList.contains('is-open'); }

  // Toggle panel open/close
  trigger.addEventListener('click', e => {
    e.stopPropagation();
    if (!isOpen()) {
      open();
      search.value = '';
      renderList();
      search.focus();
    } else {
      close();
    }
  });

  // Close on click outside
  document.addEventListener('click', e => {
    if (!document.getElementById(wrapId).contains(e.target)) close();
  });

  search.addEventListener('input', () => renderList(search.value));

  document.getElementById(clearId).addEventListener('click', e => {
    e.stopPropagation();
    selected.clear();
    renderList(search.value);
    updateTrigger();
    onChange([]);
  });

  document.getElementById(doneId).addEventListener('click', e => {
    e.stopPropagation();
    close();
  });

  updateTrigger();
  // Escape closes the panel and gives focus back to the trigger.
  panel.addEventListener('keydown', e => { if (e.key === 'Escape' && isOpen()) { close(); trigger.focus(); } });

  return {
    getSelected: ()    => [...selected],
    refresh:     ()    => updateTrigger(),
    clear:       ()    => { selected.clear(); updateTrigger(); },
    setSelected: (arr) => {
      selected.clear();
      (arr || []).forEach(v => selected.add(v));
      updateTrigger();
    },
  };
}

// Instantiate for filter bar (client-side filtering of loaded data)
let rentSelectedAreas = [];

// Instantiate for map filter bar
let mapSelectedAreas = [];

// ── Score-ring entrance animation ─────────────────────────────────────────────
// Each `.score-ring-wrap` carries `data-score-target` and a circle whose
// `stroke-dashoffset` starts at full circumference. When the card enters
// the viewport, animate the ring to its final offset and count up the number.
const _reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

const _scoreRingObserver = ('IntersectionObserver' in window) ? new IntersectionObserver(
  (entries, obs) => {
    entries.forEach(e => {
      if (!e.isIntersecting) return;
      const wrap = e.target;
      obs.unobserve(wrap);
      _animateScoreRing(wrap);
    });
  },
  { threshold: 0.18 }
) : null;

function _animateScoreRing(wrap) {
  const target = parseInt(wrap.dataset.scoreTarget || '0', 10) || 0;
  const fill   = wrap.querySelector('.score-ring-fill');
  const text   = wrap.querySelector('.score-ring-text');
  if (!fill || !text) return;

  if (_reduceMotion) {
    const finalOff = parseFloat(fill.dataset.finalOffset || '0');
    fill.setAttribute('stroke-dashoffset', finalOff);
    text.textContent = target;
    return;
  }

  // Trigger CSS transition on the ring (one frame later so the browser
  // commits the initial state first).
  requestAnimationFrame(() => {
    const finalOff = parseFloat(fill.dataset.finalOffset || '0');
    fill.setAttribute('stroke-dashoffset', finalOff);
  });

  // Number count-up — 520ms ease-out cubic
  const start = performance.now();
  const dur   = 520;
  const tick  = (now) => {
    const t = Math.min((now - start) / dur, 1);
    const eased = 1 - Math.pow(1 - t, 3);
    text.textContent = Math.round(target * eased);
    if (t < 1) requestAnimationFrame(tick);
    else      text.textContent = target;
  };
  requestAnimationFrame(tick);
}

// Rings animate on the first paint only; later renders (filters, a
// language switch) show the final number straight away — a count-up from 0
// on every re-render showed misleading scores mid-animation.
let _ringsAnimatedOnce = false;
function _setRingFinal(w) {
  const fill = w.querySelector('.score-ring-fill');
  const text = w.querySelector('.score-ring-text');
  if (fill) fill.setAttribute('stroke-dashoffset', fill.dataset.finalOffset || '0');
  if (text) text.textContent = w.dataset.scoreTarget || '0';
}
function _observeScoreRings(rootEl, animate = !_ringsAnimatedOnce) {
  if (!rootEl) return;
  rootEl.querySelectorAll('.score-ring-wrap[data-score-target]').forEach(w => {
    if (w.dataset.scoreObserved) return;
    w.dataset.scoreObserved = '1';
    if (!animate || !_scoreRingObserver) _setRingFinal(w);
    else _scoreRingObserver.observe(w);
  });
}

// ── Cross-source de-duplication ───────────────────────────────────────────────
// Same flat is often listed on Immobiliare AND Idealista (different IDs but
// same flat). The fetchers de-dup within a single source; we collapse the
// residual cross-source twins client-side at load time.
//
// Two passes are needed because the same flat sometimes has slightly
// different lat/lng from each scraper (e.g. 45.4837 vs 45.4837043,
// 9.1986 vs 9.1984046 — differs by ~15 m, falls into different 4-dp buckets):
//   Pass 1: geo @ 4dp + sqm + rooms + price       — cheap, catches most
//   Pass 2: normalized street address + sqm + rooms + price  — catches the rest

function _normalizeAddress(addr, neighbourhood) {
  if (!addr) return '';
  let a = String(addr).toLowerCase().trim();
  // Strip trailing ", <neighbourhood>, milano" or just ", milano"
  if (neighbourhood) {
    const suffix = ', ' + String(neighbourhood).toLowerCase() + ', milano';
    if (a.endsWith(suffix)) a = a.slice(0, -suffix.length);
  }
  if (a.endsWith(', milano')) a = a.slice(0, -8);
  return a.replace(/\s+/g, ' ').replace(/,\s*$/, '').trim();
}

function _dedupListings(list, isRental) {
  if (!Array.isArray(list)) return list;
  const priceField = isRental ? 'rent_mo' : 'price';

  // Score function — pick the most informative entry from a duplicate group.
  const FIELDS_FOR_COMPLETENESS = [
    'thumbnail', 'condition', 'energy_class', 'floor_n', 'elevator',
    'has_balcony', 'has_parking', 'photo_count', 'days_on_market',
    'first_seen_date',
  ];
  function score(l) {
    let s = 0;
    for (const f of FIELDS_FOR_COMPLETENESS) {
      if (l[f] != null && l[f] !== '') s += 1;
    }
    s += (l.score_total || 0) / 100;
    if (l.source === 'immobiliare' || l.source === 'sale') s += 0.05;
    if (!l.is_stale) s += 100;   // a live copy always beats an archived one
    return s;
  }

  function collapse(items, keyFn) {
    const groups = new Map();
    for (const l of items) {
      const k = keyFn(l);
      if (k == null) { groups.set(`__nokey__/${l.id}`, [l]); continue; }
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(l);
    }
    let collapsed = 0;
    const out = [];
    for (const grp of groups.values()) {
      if (grp.length === 1) { out.push(grp[0]); continue; }
      grp.sort((a, b) => score(b) - score(a));
      const winner = grp[0];
      // Preserve earliest first_seen_date so the New badge logic is honest.
      const dates = grp.map(x => x.first_seen_date).filter(Boolean).sort();
      if (dates.length) winner.first_seen_date = dates[0];
      out.push(winner);
      collapsed += grp.length - 1;
    }
    return { out, collapsed };
  }

  // Pass 1 — geo + sqm + rooms + price (catches exact-coord duplicates)
  const r1 = collapse(list, l => {
    if (l.latitude == null || l.longitude == null) return null;
    return [
      Math.round(l.latitude  * 1e4) / 1e4,
      Math.round(l.longitude * 1e4) / 1e4,
      l.sqm ?? '?', l.rooms ?? '?', l[priceField] ?? '?',
    ].join('|');
  });

  // Pass 2 — normalized street address + sqm + rooms + price
  // Catches cases where Immobiliare and Idealista geocoded the same flat to
  // slightly-different lat/lng (e.g. 9.1986 vs 9.1984), or where the address
  // is the only common identifier.
  const r2 = collapse(r1.out, l => {
    const norm = _normalizeAddress(l.address, l.neighbourhood);
    if (!norm) return null;
    return [norm, l.sqm ?? '?', l.rooms ?? '?', l[priceField] ?? '?'].join('|');
  });

  const totalCollapsed = r1.collapsed + r2.collapsed;
  if (totalCollapsed > 0) {
    console.log(
      `[dedup] collapsed ${totalCollapsed} cross-source duplicate(s) ` +
      `(geo: ${r1.collapsed}, address: ${r2.collapsed}) — ` +
      `${r2.out.length} unique of ${list.length} input`
    );
  }
  return r2.out;
}

// ── Toast utility ─────────────────────────────────────────────────────────────
let _toastTimer = null;
function showToast(msg, ms = 2400) {
  let el = document.querySelector('.lume-toast');
  if (!el) {
    el = document.createElement('div');
    el.className = 'lume-toast';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  // Force reflow so the transition fires every time
  void el.offsetWidth;
  el.classList.add('show');
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove('show'), ms);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// ── Icons ─────────────────────────────────────────────────────────────────────
// Tabler outline icons from the assets/icons.svg sprite (tools/build_icons.py).
function icon(name, cls = '') {
  return `<svg class="i${cls ? ' ' + cls : ''}" aria-hidden="true"><use href="assets/icons.svg#${name}"></use></svg>`;
}
