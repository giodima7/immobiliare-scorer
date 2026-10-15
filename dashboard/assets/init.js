// Lume dashboard — start-up: event wiring and everything that runs while
// the page loads, in the original order. Loads last (see index.html), so
// every function in the other assets/*.js files exists by now.


// ── from i18n.js · Internationalisation ──
document.documentElement.lang = currentLang;   // screen readers / hyphenation

// ── from shell.js · Global tooltip (item 4 + 5: fixed-position, no clipping) ──
// Delegate tooltip triggers off document (works for dynamically created cards)
document.addEventListener('mouseenter', e => {
  // Score badge wrap → score breakdown tooltip
  const bdWrap = _safeClosest(e.target, '.score-badge-wrap[data-sc-rent], .score-ring-wrap[data-sc-rent]');
  if (bdWrap) {
    // If a different score is pinned, leave it pinned — hover doesn't unpin
    if (_gtPinnedAnchor && _gtPinnedAnchor !== bdWrap) return;
    _gtShow(_scoreBreakdownHtml(bdWrap), bdWrap, 'cursor', e);
    return;
  }
  // OMI zone pill (item 5)
  const omiPill = _safeClosest(e.target, '.omi-zone-pill[data-omi-title]');
  if (omiPill) {
    _gtShow(`<div class="gt-title">${omiPill.dataset.omiCode || 'OMI Zone'}</div>${omiPill.dataset.omiTitle}`, omiPill, 'anchor');
    return;
  }
  // Geo tooltip wrap
  const geoWrap = _safeClosest(e.target, '.geo-tooltip-wrap[data-geo-lines]');
  if (geoWrap) {
    try {
      const lines = JSON.parse(decodeURIComponent(geoWrap.dataset.geoLines || '[]'));
      if (lines.length) {
        _gtShow('<div class="gt-title">Proximity</div>' + lines.join('<br>'), geoWrap, 'cursor', e);
      }
    } catch { /* ignore decode errors */ }
  }
}, true);  // capture phase so it fires before child elements swallow it
document.addEventListener('mouseleave', e => {
  // Don't hide a pinned (clicked) score-breakdown tooltip on hover-out.
  if (_gtPinnedAnchor) return;
  if (_safeClosest(e.target, '.score-badge-wrap[data-sc-rent], .score-ring-wrap[data-sc-rent]') ||
      _safeClosest(e.target, '.geo-tooltip-wrap[data-geo-lines]') ||
      _safeClosest(e.target, '.omi-zone-pill[data-omi-title]')) {
    _gtHide();
  }
}, true);
// Click pins / unpins the score-breakdown tooltip. Clicking outside the
// score badge (anywhere else on the page) unpins and hides it.
document.addEventListener('click', e => {
  const bdWrap = _safeClosest(e.target, '.score-badge-wrap[data-sc-rent], .score-ring-wrap[data-sc-rent]');
  if (bdWrap) {
    if (_gtPinnedAnchor === bdWrap) {
      // Tap again on the pinned badge → close
      _gtPinnedAnchor.classList.remove('is-pinned');
      _gtPinnedAnchor = null;
      _gtHide();
    } else {
      // Pin a new badge (un-pinning any previously-pinned one)
      if (_gtPinnedAnchor) _gtPinnedAnchor.classList.remove('is-pinned');
      _gtPinnedAnchor = bdWrap;
      bdWrap.classList.add('is-pinned');
      _gtShow(_scoreBreakdownHtml(bdWrap), bdWrap, 'anchor');
    }
    e.stopPropagation();
    return;
  }
  // Click anywhere else → if a tooltip is pinned, close it
  if (_gtPinnedAnchor && !_gtEl.contains(e.target)) {
    _gtPinnedAnchor.classList.remove('is-pinned');
    _gtPinnedAnchor = null;
    _gtHide();
  }
}, true);
document.addEventListener('mousemove', e => {
  const hoverTarget = _safeClosest(e.target, '.score-badge-wrap[data-sc-rent], .score-ring-wrap[data-sc-rent], .geo-tooltip-wrap[data-geo-lines]');
  if (hoverTarget && _gtEl.style.display !== 'none') {
    _gtPosition(e);
  }
}, true);
window.addEventListener('scroll', () => _gtPosition(), true);
window.addEventListener('resize', () => _gtPosition());

// ── from shell.js · Init ──
window.addEventListener('resize', _updateStickyTops);

// ── from shell.js · Filter bar sticky shadow — sentinel-based detection ──
// Set sticky top offsets once the DOM is painted
requestAnimationFrame(_updateStickyTops);
// Wire up the filter-bar "is-stuck" shadow detector
requestAnimationFrame(_wireFilterBarStickyShadow);
// Apply current language to all data-i18n labels + light up the EN/IT pill.
// Runs once on initial paint; setLang() handles every subsequent toggle.
requestAnimationFrame(() => {
  updateLangToggle();
  // Apply translations to any data-i18n labels already in the DOM.
  document.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = t(el.dataset.i18n); });
  document.querySelectorAll('[data-i18n-placeholder]').forEach(el => { el.placeholder = t(el.dataset.i18nPlaceholder); });
  document.querySelectorAll('[data-i18n-title]').forEach(el => { el.title = t(el.dataset.i18nTitle); });
  // City selector — first paint uses the baked-in fallback list. init()
  // re-runs both updates once loadAvailableCities() resolves, so any
  // localised display_name / centre / has_metro from Supabase
  // overwrites this initial render.
  updateCityToggleUI();
  updateFiltersForCurrentCity();
});

// ── from shell.js · ID search: lives inside the rentals filter bar ──
// Each keystroke debounces a re-render of the grid.
(function _wireIdSearch() {
  const rInput = document.getElementById('rIdSearch');
  let _rDeb = null;
  const fireRent = () => {
    if (_rDeb) clearTimeout(_rDeb);
    _rDeb = setTimeout(() => {
      _rDeb = null;
      if (typeof renderRentals === 'function') try { renderRentals(); } catch (_) {}
      if (typeof saveFilters === 'function')   try { saveFilters();   } catch (_) {}
    }, 120);
  };
  if (rInput) {
    rInput.addEventListener('input',  fireRent);
    rInput.addEventListener('search', fireRent);
  }
  // Click-to-search from any card's #id pill.
  document.addEventListener('click', (e) => {
    const pill = e.target.closest && e.target.closest('.id-pill[data-listing-id]');
    if (!pill) return;
    const id = pill.dataset.listingId;
    if (rInput) rInput.value = id;
    rInput?.focus();
    fireRent();
  });
})();
updateFavBadge();
init();

// ── from shell.js · Tab switching ──
document.querySelectorAll('.tab-btn').forEach(btn =>
  btn.addEventListener('click', () => switchTab(btn.dataset.tab)));

// ── from widgets.js · Multi-select component ──
// Instantiate for rental scanner config
const scanAreaMsd = buildMultiSelect({
  wrapId: 'scanAreaWrap', triggerId: 'scanAreaTrigger', panelId: 'scanAreaPanel',
  searchId: 'scanAreaSearch', listId: 'scanAreaList',
  clearId: 'scanAreaClear', doneId: 'scanAreaDone',
  onChange: () => { if (typeof saveScanPrefs === 'function') saveScanPrefs(); },
});
const filterAreaMsd = buildMultiSelect({
  wrapId: 'filterAreaWrap', triggerId: 'filterAreaTrigger', panelId: 'filterAreaPanel',
  searchId: 'filterAreaSearch', listId: 'filterAreaList',
  clearId: 'filterAreaClear', doneId: 'filterAreaDone',
  onChange: areas => {
    rentSelectedAreas = areas;
    renderRentals();
    if (typeof saveFilters === 'function') saveFilters();
  },
});
// If restoreFilters has already populated rentSelectedAreas (it runs in
// showApp(), which can fire before this script-tag block during certain
// startup orderings), reflect that selection in the MSD trigger now that
// it exists.
if (Array.isArray(rentSelectedAreas) && rentSelectedAreas.length && filterAreaMsd?.setSelected) {
  try { filterAreaMsd.setSelected(rentSelectedAreas); } catch (_) {}
}
const mapAreaMsd = buildMultiSelect({
  wrapId: 'mapAreaWrap', triggerId: 'mapAreaTrigger', panelId: 'mapAreaPanel',
  searchId: 'mapAreaSearch', listId: 'mapAreaList',
  clearId: 'mapAreaClear', doneId: 'mapAreaDone',
  onChange: areas => { mapSelectedAreas = areas; if (typeof _mapRender === 'function') _mapRender(); },
});

// ── from alerts.js · Email alerts: identity + transport ──
// One delegator that handles every per-alert action (edit / toggle /
// delete). Attached to document once; the buttons themselves carry the
// data-* attributes the handlers need. Avoids inline onclick string
// concatenation that breaks on names containing quotes.
//
// CAPTURE PHASE (3rd arg = true) — the alerts-manager modal's inner
// .save-alert-modal carries onclick="event.stopPropagation()" to keep
// content-clicks from dismissing the backdrop. That call kills bubbling
// in mid-flight, so a document-level BUBBLE listener never fires for
// clicks inside the modal. Capture runs before bubble, so our delegator
// receives the event before stopPropagation can swallow it.
if (!window._digestActionDelegatorBound) {
  window._digestActionDelegatorBound = true;
  document.addEventListener('click', (e) => {
    const btn = _safeClosest(e.target, '[data-digest-action]');
    if (!btn) return;
    const wrap = _safeClosest(btn, '.digest-alert-actions');
    if (!wrap) return;
    const id     = wrap.dataset.alertId;
    const name   = wrap.dataset.alertName || '';
    const active = wrap.dataset.alertActive === '1';
    if (!id) return;
    switch (btn.dataset.digestAction) {
      case 'edit':   editDigestAlert(id);            break;
      case 'toggle': toggleDigestActive(id, active); break;
      case 'delete': deleteDigestAlert(id, name);    break;
    }
  }, true);
}

// ── from cards.js · New listing badge helpers ──
// Tap/click (or Enter) on a card's "N comps ⓘ" opens the method text below
// the row — the title tooltip alone never shows on touch screens.
document.addEventListener('click', e => {
  const c = e.target.closest && e.target.closest('[data-comps-tip]');
  if (!c) return;
  const tip = c.closest('.comps-row')?.nextElementSibling;
  if (tip && tip.classList.contains('comps-tip-text')) tip.hidden = !tip.hidden;
});
document.addEventListener('keydown', e => {
  if (e.key === 'Enter' && e.target.matches && e.target.matches('[data-comps-tip]')) e.target.click();
});

// ── from rentals.js · Rentals — data loading ──
document.getElementById('rShowStale')?.addEventListener('change', () => { if (IS_LIVE) loadRentals(); });

// ── from scanner.js · Multi-city enrichment panel ──
// "Run for all cities" — single POST kicks off the backend orchestrator,
// which drains a queue of (city × kind) jobs sequentially. The button
// flips to a queued/running state optimistically so the user sees
// feedback in <50 ms instead of waiting for the next 1 s scanner poll.
(function () {
  const btn = document.getElementById('btnEnrichAll');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    if (!IS_LIVE) return;
    btn.disabled = true;

    // Optimistic UI: seed every (city, kind) slot with a "queued" state so
    // the rows light up the moment the user clicks. The next /scanner-status
    // poll will replace this with the real server state.
    const optimistic = { geo_jobs: {}, geo_queue: [], geo_queue_running: true };
    for (const c of _ENRICH_CITY_ORDER) for (const k of _ENRICH_KINDS) {
      optimistic.geo_jobs[`${c}:${k}`] = {
        city: c, kind: k, state: 'queued', running: false,
        done: 0, total: 0, error: null,
      };
      optimistic.geo_queue.push({ city: c, kind: k });
    }
    _renderEnrichmentPanel(optimistic);

    try {
      const r = await fetch('/geo-enrich-queue', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({
          cities: _ENRICH_CITY_ORDER, kinds: _ENRICH_KINDS,
        }),
      });
      if (!r.ok && r.status !== 409) {
        const j = await r.json().catch(() => ({}));
        const q = document.getElementById('enrichQueueStatus');
        if (q) { q.textContent = '✗ ' + (j.error || ('HTTP ' + r.status)); q.style.color = '#b91c1c'; }
        btn.disabled = false;
        return;
      }
    } catch (e) {
      const q = document.getElementById('enrichQueueStatus');
      if (q) { q.textContent = '✗ ' + e.message; q.style.color = '#b91c1c'; }
      btn.disabled = false;
      return;
    }
    // Bump the poll once so the UI catches up faster than waiting for the
    // next 1 s tick. The orchestrator may still be initialising slots,
    // so even a slightly stale read is fine — the next poll fixes it.
    if (typeof updateScannerStatus === 'function') updateScannerStatus();
  });
})();
(function () {
  const btn = document.getElementById('btnEnrichStop');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    if (!IS_LIVE) return;
    btn.disabled    = true;
    btn.textContent = '■ Stopping…';
    // Manual Stop = user-intent override. Disable the auto-enqueue path
    // until the page reloads, otherwise the next 10 s poll would re-fire
    // the queue and immediately undo the Stop.
    _enrichAutoPausedUntilReload = true;

    // Optimistic UI: synthesise a status payload where every queued slot
    // is already "stopped" + the running slot reads "Stopping…". The next
    // /scanner-status poll will replace this with the real server state
    // — usually within a second once the DELETE round-trip completes.
    if (typeof window !== 'undefined' && window._lastScannerStatus) {
      const st = JSON.parse(JSON.stringify(window._lastScannerStatus));
      const jobs = st.geo_jobs || {};
      for (const k of Object.keys(jobs)) {
        const j = jobs[k];
        if (!j) continue;
        if (j.state === 'queued') {
          j.state = 'stopped';
          j.running = false;
          j.phase = null;
          j.phase_label = null;
        } else if (j.running) {
          j.phase       = 'stopping';
          j.phase_label = 'Stopping…';
        }
      }
      st.geo_queue = [];
      st.geo_queue_running = true;  // until the DELETE clears the orchestrator
      _renderEnrichmentPanel(st);
    }

    try {
      // DELETE on either endpoint cancels both the active worker AND the
      // pending queue (both endpoints set _geo_queue_stop).
      await fetch('/geo-enrich', { method: 'DELETE' });
    } catch (_) { /* ok */ }
    btn.textContent = '■ Stop';
    btn.disabled    = false;
    if (typeof updateScannerStatus === 'function') updateScannerStatus();
  });
})();

// ── from scanner.js · Scanner start/stop buttons ──
// Start Lume kicks off the rentals scan for the toggled-on cities via
// /start-scanner (fetch_rentals.py daemon).
document.getElementById('btnStartScanner').addEventListener('click', async () => {
  saveScanPrefs();
  const cities = _readScanCities();
  const maxRent  = document.getElementById('sScanMaxRent').value;
  const minRooms = document.getElementById('sScanMinRooms').value;
  const pages    = parseInt(document.getElementById('sScanPages').value) || 3;

  const tasks = [];

  const body = { pages, cities };
  if (maxRent)  body.max_rent  = parseInt(maxRent);
  if (minRooms) body.min_rooms = parseInt(minRooms);
  tasks.push(
    fetch('/start-scanner', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(r => ({ kind: 'rentals', ok: r.ok || r.status === 409, status: r.status }))
  );

  try {
    const results = await Promise.all(tasks);
    const failures = results.filter(r => !r.ok);
    if (failures.length) {
      alert('Some scans failed to start:\n' +
            failures.map(f => `  ${f.kind}: HTTP ${f.status}`).join('\n'));
    }
    await updateScannerStatus();
  } catch (e) { alert('Could not start scanner: ' + e.message); }
});
document.getElementById('btnStopScanner').addEventListener('click', async () => {
  try {
    await Promise.all([
      fetch('/stop-scanner',    { method: 'POST' }).catch(() => null),
    ]);
    await updateScannerStatus();
  } catch (e) { alert('Could not stop scanner: ' + e.message); }
});

// ── from controls.js · Rentals filter event listeners ──
['rFascia','rMaxMetro','rMinFloor','rMinEnergy','rListed'].forEach(id =>
  document.getElementById(id).addEventListener('change', () => { renderRentals(); saveFilters(); }));
['rMaxRent','rMinSqm','rMinRooms'].forEach(id =>
  document.getElementById(id).addEventListener('input', _renderRentalsDebounced));
document.getElementById('rElevator').addEventListener('change', () => { renderRentals(); saveFilters(); });
document.getElementById('rShowStale')?.addEventListener('change', renderRentals);
document.querySelectorAll('#rSourceFilter .sort-tab').forEach(btn =>
  btn.addEventListener('click', () => {
    document.querySelectorAll('#rSourceFilter .sort-tab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    renderRentals();
    saveFilters();
  }));
document.querySelectorAll('#rGemFilter .gem-tab').forEach(btn =>
  btn.addEventListener('click', () => {
    document.querySelectorAll('#rGemFilter .gem-tab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    renderRentals();
    saveFilters();
  }));

// ── from controls.js · Pill-group sliding thumb ──
// Hook into every existing pill-click. The .active class flip already
// happens inside each handler above; we just need to recompute the thumb
// once the DOM has settled. Capture-phase listener wins the race against
// the per-group click handlers without needing to reorder them.
document.addEventListener('click', e => {
  const pill = e.target.closest('.filter-pill');
  if (!pill) return;
  const group = pill.closest('.filter-pill-group');
  if (!group) return;
  // Defer one tick so the .active class change made by the handler is
  // already applied when we measure.
  requestAnimationFrame(() => _updatePillThumb(group));
}, true);
// Initial paint + responsive recompute. The filter-bar wraps on narrow
// viewports so the thumb width can change without any click.
requestAnimationFrame(_updateAllPillThumbs);
window.addEventListener('resize', () => {
  // Throttle: one rAF coalesces multiple resize events into a single
  // measurement pass.
  if (window._pillThumbRaf) return;
  window._pillThumbRaf = requestAnimationFrame(() => {
    window._pillThumbRaf = null;
    _updateAllPillThumbs();
  });
});
document.getElementById('rScoreNum')?.addEventListener('input', function () {
  syncScoreFromNumber(this.value);
  renderRentals();
});
document.getElementById('rScoreNum')?.addEventListener('blur', function () {
  // Normalise on blur so a half-typed "12" stays put but an out-of-range
  // "999" snaps back to 100 (and "" snaps to 0).
  this.value = _rScoreClamp(this.value);
});
document.getElementById('rScore').addEventListener('input', function () {
  syncScoreFromSlider(this.value);
  renderRentals();
  saveFilters();
});
document.querySelectorAll('#rentSortTabs .sort-tab').forEach(btn => {
  btn.addEventListener('click', () => {
    rentSortKey = btn.dataset.rsort;
    document.querySelectorAll('#rentSortTabs .sort-tab').forEach(t =>
      t.classList.toggle('active', t === btn));
    renderRentals();
    saveFilters();
  });
});
document.getElementById('rBtnReset').addEventListener('click', () => {
  const _idEl = document.getElementById('rIdSearch');
  if (_idEl) _idEl.value = '';
  filterAreaMsd.clear();
  rentSelectedAreas = [];
  document.getElementById('rFascia').value    = '';
  // OMI Zone filter removed from the bar — no rOmiZone reset needed.
  document.getElementById('rMaxRent').value   = '';
  document.getElementById('rMinSqm').value    = '';
  document.getElementById('rMinRooms').value  = '';
  document.getElementById('rScore').value     = 0;
  // Score slider + number input + hidden text mirror — keep all three in sync.
  const _rScoreNum = document.getElementById('rScoreNum');
  if (_rScoreNum) _rScoreNum.value = 0;
  document.getElementById('rScoreVal').textContent = '0';
  document.getElementById('rMaxMetro').value  = '';
  document.getElementById('rMinFloor').value  = '';
  document.getElementById('rElevator').checked = false;
  const rst = document.getElementById('rShowStale'); if (rst) rst.checked = false;
  document.getElementById('rMinEnergy').value = '';
  document.getElementById('rListed').value    = '';
  // filters.js state: must-haves, not-yet-known, condition / discount / contract
  rentMust.clear();
  rentIncludeUnknown = false;
  ['rIncludeUnknown', 'dIncludeUnknown'].forEach(id => { const c = document.getElementById(id); if (c) c.checked = false; });
  ['rCondition', 'rMinDiscount', 'rContract'].forEach(id => { const c = document.getElementById(id); if (c) c.value = ''; });
  document.querySelectorAll('#rGemFilter .gem-tab').forEach(t =>
    t.classList.toggle('active', t.dataset.gem === ''));
  document.querySelectorAll('#rSourceFilter .sort-tab').forEach(t =>
    t.classList.toggle('active', (t.dataset.rsource || '') === ''));
  rentSortKey = 'score_total';
  document.querySelectorAll('#rentSortTabs .sort-tab').forEach(t =>
    t.classList.toggle('active', t.dataset.rsort === 'score_total'));
  // Reset moved the .active class on the pill groups → recompute thumbs
  // so the indicator snaps back to position 1 (All / Score) instead of
  // staying parked under the previously-selected option.
  if (typeof _updateAllPillThumbs === 'function') {
    requestAnimationFrame(_updateAllPillThumbs);
  }
  renderRentals();
  saveFilters();
});

// ── from controls.js · Scanner filter persistence ──
// Persist whenever any scanner field changes
['sScanPages','sScanMaxRent','sScanMinRooms'].forEach(id =>
  document.getElementById(id).addEventListener('change', saveScanPrefs));
// Restore saved prefs (scanAreaMsd is now fully initialised above)
restoreScanPrefs();
// Always populate drawer chips immediately (uses MILAN_AREAS fallback in static mode)
_populateDrawerAreaFromActive();
if (IS_LIVE) {
  fetch('/area-settings').then(r => r.ok ? r.json() : null).then(data => {
    if (!data || _settingsLoaded) return;   // Settings tab already rendered — don't overwrite
    _settingsCurrentAreas = (data.areas || []).map(a => ({...a}));
    _populateDrawerAreaFromActive();   // repopulate with real active areas once loaded
    // Always populate scanner panel from area_settings.json — areas are owned there,
    // not in localStorage, so we never skip this step.
    const activeNames = _settingsCurrentAreas.filter(a => a.active).map(a => a.name);
    if (activeNames.length) {
      scanAreaMsd.setSelected(activeNames);
    }
  }).catch(() => {});
}

// ── from controls.js · Cache stats ──
document.getElementById('btnCacheClear')?.addEventListener('click', async () => {
  if (!IS_LIVE) return;
  if (!confirm('This will delete the enrichment cache. All listings will be re-enriched on the next scan.\n\nContinue?')) return;
  const btn = document.getElementById('btnCacheClear');
  btn.disabled = true;
  btn.textContent = 'Clearing…';
  try {
    const r = await fetch('/cache-clear', { method: 'POST' });
    const j = await r.json().catch(() => ({}));
    if (j.ok) {
      await loadCacheStats();
      btn.textContent = '✓ Cleared';
      setTimeout(() => { btn.disabled = false; btn.textContent = 'Clear cache'; }, 2000);
    } else {
      alert('Could not clear cache: ' + (j.error || 'Unknown error'));
      btn.disabled = false;
      btn.textContent = 'Clear cache';
    }
  } catch (e) {
    alert('Error: ' + e.message);
    btn.disabled = false;
    btn.textContent = 'Clear cache';
  }
});

// ── from controls.js · Apply OMI polygon button ──
(function () {
  const btn    = document.getElementById('btnApplyOmi');
  const status = document.getElementById('applyOmiStatus');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    if (!IS_LIVE) return;
    btn.disabled    = true;
    btn.textContent = '…';
    if (status) status.textContent = 'applying…';
    try {
      const r = await fetch('/apply-omi', { method: 'POST' });
      const j = await r.json();
      if (r.ok) {
        if (status) status.textContent = `✓ ${j.updated} updated`;
        await loadRentals();   // refresh the grid
      } else {
        if (status) status.textContent = '✗ ' + (j.error || 'failed');
      }
    } catch (e) {
      if (status) status.textContent = '✗ ' + e.message;
    }
    btn.disabled    = false;
    btn.textContent = 'Apply OMI';
    setTimeout(() => { if (status) status.textContent = ''; }, 4000);
  });
})();

// ── from controls.js · Download POIs button ──
(function () {
  const btn = document.getElementById('btnDownloadPois');
  if (!btn) return;

  // Show current status on load
  fetch('/api/pois-status').then(r => r.json()).then(d => {
    if (d.exists) {
      btn.title = `POI data downloaded ${d.downloaded_at || ''}. Click to refresh.`;
      btn.style.opacity = '0.6';
    }
  }).catch(() => {});

  btn.addEventListener('click', async () => {
    if (!IS_LIVE) return;
    btn.disabled = true;
    btn.textContent = '⏳ Downloading…';
    const gStatus = document.getElementById('geoEnrichStatus');
    try {
      const r = await fetch('/api/download-pois', { method: 'POST' });
      const j = await r.json();
      if (j.ok) {
        btn.textContent = '✓ POI data';
        btn.style.opacity = '0.6';
        if (gStatus) gStatus.textContent = `✓ ${j.metro ?? 0} metro · ${j.tram ?? 0} tram · ${j.supermarket ?? 0} supermarkets · ${j.park ?? 0} parks`;
      } else {
        btn.textContent = 'POI data';
        if (gStatus) gStatus.textContent = '✗ ' + (j.error || 'download failed');
      }
    } catch (e) {
      btn.textContent = 'POI data';
      if (gStatus) gStatus.textContent = '✗ ' + e.message;
    } finally {
      btn.disabled = false;
    }
  });
})();

// ── from controls.js · Geo enrichment button ──
(function () {
  const btn = document.getElementById('btnEnrichGeo');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    if (!IS_LIVE) return;   // button hidden in static mode, but guard anyway
    const stopping = btn.textContent.trim().startsWith('■');
    if (stopping) {
      btn.disabled = true;
      try { await fetch('/geo-enrich', { method: 'DELETE' }); } catch { /* ok */ }
      await updateScannerStatus();
    } else {
      btn.disabled = true;
      const gStatus = document.getElementById('geoEnrichStatus');
      try {
        const r = await fetch('/geo-enrich', { method: 'POST' });
        if (!r.ok && r.status !== 409) {
          const j = await r.json().catch(() => ({}));
          if (gStatus) gStatus.textContent = '✗ ' + (j.error || 'HTTP ' + r.status);
          btn.disabled = false;
          return;
        }
      } catch (e) {
        if (gStatus) gStatus.textContent = '✗ ' + e.message;
        btn.disabled = false;
        return;
      }
      // Trigger immediate status poll to refresh the progress UI
      await updateScannerStatus();
    }
  });
})();

// ── from controls.js · Mobile filter drawer (Rentals) ──
(function () {
  const backdrop     = document.getElementById('mobileFilterBackdrop');
  const drawer       = document.getElementById('mobileFilterDrawer');
  const openBtn      = document.getElementById('mobileFilterBtn');
  const mapFilterBtn = document.getElementById('mapFilterBtn');
  const showBtn      = document.getElementById('drawerShowBtn');
  const resetBtn     = document.getElementById('drawerReset');
  if (!drawer) return;

  // Instantiate the searchable area multiselects once the drawer DOM exists
  dRentAreaMsearch = AreaMSearch('dAreaInput',  'dAreaTags',  'dAreaDropdown',  [], () => _updateShowBtn());
  // Populate options from whatever area data is already loaded
  if (typeof _populateDrawerAreaFromActive === 'function') _populateDrawerAreaFromActive();

  // Update "Show X results" label
  function _updateShowBtn() {
    if (_drawerIsMap) {
      const countEl = document.getElementById('mapListingCount');
      const n = countEl ? countEl.textContent : '–';
      showBtn.textContent = (n === '–' || n == null) ? 'Show results' : `Show ${n} results`;
      return;
    }
    if (typeof _rentFilteredData !== 'undefined') {
      showBtn.textContent = `Show ${_rentFilteredData.length} results`;
    }
  }

  function openDrawer(opts = {}) {
    const mode = opts.mode || 'rent';
    _drawerIsMap = (mode === 'rent') && !!opts.isMap;
    // Gem filter only applies to the rentals list (not map)
    const gemField = document.getElementById('dGemFilter')?.closest('.drawer-field');
    if (gemField) gemField.style.display = _drawerIsMap ? 'none' : '';
    drawer.querySelectorAll('.drawer-rent-only').forEach(el => { el.style.display = _drawerIsMap ? 'none' : ''; });
    backdrop.classList.add('is-open');
    drawer.classList.remove('is-closing');
    drawer.classList.add('is-open');
    document.body.style.overflow = 'hidden';
    _syncDrawerFromMain();
    _updateShowBtn();
  }
  function closeDrawer(apply) {
    drawer.classList.add('is-closing');
    drawer.classList.remove('is-open');
    backdrop.classList.remove('is-open');
    document.body.style.overflow = '';
    if (apply) _applyDrawerToMain();
  }

  // Sync drawer controls from the appropriate main filter source
  function _syncDrawerFromMain() {
    const get = id => document.getElementById(id);

    // Rentals (and rentals-on-map) mode
    const idPrefix = _drawerIsMap ? 'mRent' : 'r';
    const fMaxRent  = get(idPrefix + 'MaxRent');
    const fMinSqm   = get(idPrefix + 'MinSqm');
    const fMinRooms = get(idPrefix + 'MinRooms');
    const fFascia   = get(idPrefix + 'Fascia');
    const fMaxMetro = get(idPrefix + 'MaxMetro');
    const fScore    = get(idPrefix + 'Score');
    const sourceAreas = _drawerIsMap ? mapSelectedAreas : rentSelectedAreas;
    if (dRentAreaMsearch) dRentAreaMsearch.setSelected(sourceAreas);
    if (fMaxRent)  get('dMaxRent').value  = fMaxRent.value;
    if (fMinSqm)   get('dMinSqm').value   = fMinSqm.value;
    if (fMinRooms) get('dMinRooms').value = fMinRooms.value;
    if (fFascia)   get('dFascia').value   = fFascia.value;
    if (fMaxMetro) get('dMaxMetro').value = fMaxMetro.value;
    if (fScore) {
      const sv = fScore.value;
      get('dScore').value = sv;
      get('dScoreVal').textContent = sv;
    }
    if (!_drawerIsMap) {
      const fGemActive = document.querySelector('#rGemFilter .gem-tab.active');
      const gem = fGemActive ? (fGemActive.dataset.gem || '') : '';
      document.querySelectorAll('#dGemFilter .gem-tab').forEach(b => {
        b.classList.toggle('active', b.dataset.gem === gem);
      });
      get('dCondition').value   = get('rCondition')?.value || '';
      get('dMinDiscount').value = get('rMinDiscount')?.value || '';
      get('dIncludeUnknown').checked = rentIncludeUnknown;
    }
  }

  // Apply drawer values back to the appropriate main filters and re-render
  function _applyDrawerToMain() {
    const get = id => document.getElementById(id);

    // Rentals mode — apply to BOTH the rentals list AND the map
    const areas = dRentAreaMsearch ? dRentAreaMsearch.getSelected() : [];

    // List state
    rentSelectedAreas = areas;
    if (typeof filterAreaMsd !== 'undefined') {
      areas.length ? filterAreaMsd.setSelected(areas) : filterAreaMsd.clear();
    }
    if (get('rMaxRent'))  get('rMaxRent').value  = get('dMaxRent').value;
    if (get('rMinSqm'))   get('rMinSqm').value   = get('dMinSqm').value;
    if (get('rMinRooms')) get('rMinRooms').value = get('dMinRooms').value;
    if (get('rFascia'))   get('rFascia').value   = get('dFascia').value;
    if (get('rMaxMetro')) get('rMaxMetro').value = get('dMaxMetro').value;
    if (get('rScore')) {
      const sv = get('dScore').value;
      get('rScore').value = sv;
      const rvEl = get('rScoreVal');
      if (rvEl) rvEl.textContent = sv;
    }
    const dGemActive = document.querySelector('#dGemFilter .gem-tab.active');
    const gem = dGemActive ? (dGemActive.dataset.gem || '') : '';
    document.querySelectorAll('#rGemFilter .gem-tab').forEach(b => {
      b.classList.toggle('active', b.dataset.gem === gem);
    });
    if (get('rCondition'))   get('rCondition').value   = get('dCondition').value;
    if (get('rMinDiscount')) get('rMinDiscount').value = get('dMinDiscount').value;

    // Map state — mirror rental filters into mRent* and mapSelectedAreas
    mapSelectedAreas = areas;
    if (typeof mapAreaMsd !== 'undefined') mapAreaMsd.setSelected(areas);
    if (get('mRentMaxRent'))  get('mRentMaxRent').value  = get('dMaxRent').value;
    if (get('mRentMinSqm'))   get('mRentMinSqm').value   = get('dMinSqm').value;
    if (get('mRentMinRooms')) get('mRentMinRooms').value = get('dMinRooms').value;
    if (get('mRentFascia'))   get('mRentFascia').value   = get('dFascia').value;
    if (get('mRentMaxMetro')) get('mRentMaxMetro').value = get('dMaxMetro').value;
    if (get('mRentScore')) {
      const sv = get('dScore').value;
      get('mRentScore').value = sv;
      const lb = document.getElementById('mRentScoreVal');
      if (lb) lb.textContent = sv;
    }

    if (typeof renderRentals === 'function') renderRentals();
    if (typeof _mapRender === 'function')    _mapRender();
    if (typeof saveFilters === 'function')   saveFilters();
  }

  // Header filter button — choose mode based on active tab
  if (openBtn) openBtn.addEventListener('click', () => {
    const activeTab = document.querySelector('.tab-btn.active')?.dataset.tab;
    if (activeTab === 'map') {
      openDrawer({ mode: 'rent', isMap: true });
    } else {
      openDrawer({ mode: 'rent' });
    }
  });
  if (mapFilterBtn) mapFilterBtn.addEventListener('click', () => openDrawer({ mode: 'rent', isMap: true }));
  backdrop.addEventListener('click', () => closeDrawer(false));
  showBtn.addEventListener('click',  () => closeDrawer(true));

  // Save-alert button (mobile parity with desktop's pill). Apply the
  // drawer's filters back to the main filter bar FIRST (closeDrawer
  // with apply=true), then open the save-alert modal. Without the
  // apply step, the modal would seed from the stale pre-drawer state.
  const drawerSaveAlertBtn = document.getElementById('drawerSaveAlertBtn');
  if (drawerSaveAlertBtn) {
    drawerSaveAlertBtn.addEventListener('click', () => {
      closeDrawer(true);
      if (typeof saveDigestFilters === 'function') {
        // setTimeout 0 so the drawer's closing animation paints first;
        // the save-alert modal then layers on top without flicker.
        setTimeout(() => saveDigestFilters(), 0);
      }
    });
  }

  // Drawer range sync (rent)
  const dScore = document.getElementById('dScore');
  if (dScore) dScore.addEventListener('input', () => {
    document.getElementById('dScoreVal').textContent = dScore.value;
  });

  // Drawer gem tabs (rent)
  document.querySelectorAll('#dGemFilter .gem-tab').forEach(btn =>
    btn.addEventListener('click', () => {
      document.querySelectorAll('#dGemFilter .gem-tab').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
    }));

  // Drawer reset
  if (resetBtn) resetBtn.addEventListener('click', () => {
    ['dMaxRent','dMinSqm','dMinRooms'].forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
    ['dFascia','dMaxMetro'].forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
    if (dRentAreaMsearch) dRentAreaMsearch.clear();
    const dsc = document.getElementById('dScore'); if (dsc) dsc.value = 0;
    document.getElementById('dScoreVal').textContent = '0';
    document.querySelectorAll('#dGemFilter .gem-tab').forEach(b =>
      b.classList.toggle('active', b.dataset.gem === ''));
    ['dCondition', 'dMinDiscount'].forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
    rentMust.clear();
    const el = document.getElementById('rElevator'); if (el) el.checked = false;
    rentIncludeUnknown = false;
    ['rIncludeUnknown', 'dIncludeUnknown'].forEach(id => { const c = document.getElementById(id); if (c) c.checked = false; });
    renderRentals();
  });

  // Swipe-down to close. Restrict to touches that START on the drawer
  // handle / title row — NOT inside the scrollable body. Previously any
  // downward scroll of >80px inside the area-list closed the drawer,
  // which made list scrolling feel like the modal was being dragged out
  // from under the user's finger.
  let _touchStartY = 0;
  let _touchInHandle = false;
  const _handleEls = drawer.querySelectorAll('.drawer-handle, .drawer-title-row');
  _handleEls.forEach(el => {
    el.addEventListener('touchstart', e => {
      _touchStartY   = e.touches[0].clientY;
      _touchInHandle = true;
    }, { passive: true });
  });
  drawer.addEventListener('touchstart', e => {
    // Reset the in-handle flag if the touch starts elsewhere.
    if (!Array.from(_handleEls).some(el => el.contains(e.target))) {
      _touchInHandle = false;
    }
  }, { passive: true });
  drawer.addEventListener('touchend', e => {
    if (!_touchInHandle) return;
    const dy = e.changedTouches[0].clientY - _touchStartY;
    if (dy > 80) closeDrawer(false);
    _touchInHandle = false;
  }, { passive: true });
})();

// ── from controls.js · Onboarding ──
(function () {
  const overlay = document.getElementById('onboardingOverlay');
  if (!overlay) return;

  // Event listeners and animations are set up here (at parse time).
  // Visibility is decided later by _checkOnboarding() inside showApp().

  const slides     = document.getElementById('onboardSlides');
  const dots       = document.querySelectorAll('.onboard-dot');
  const skipBtn    = document.getElementById('onboardSkip');
  const startBtn   = document.getElementById('onboardStart');
  const ob2Number  = document.getElementById('ob2Number');
  const ob4Score   = document.getElementById('ob4Score');
  let _currentSlide = 0;
  let _ob2Done = false, _ob4Done = false;

  function _finish() {
    // Store under the global unscoped key so the flag survives Clerk
    // sign-in / sign-out cycles. Still set the legacy per-user key for
    // back-compat with any code that hasn't been updated yet.
    _markOnboarded();
    Storage.set('onboarded', true);
    overlay.classList.add('hiding');
    setTimeout(() => overlay.remove(), 420);
  }

  skipBtn  && skipBtn.addEventListener('click', _finish);
  startBtn && startBtn.addEventListener('click', _finish);

  // Update progress dots on scroll
  slides.addEventListener('scroll', () => {
    const idx = Math.round(slides.scrollTop / slides.clientHeight);
    if (idx !== _currentSlide) {
      _currentSlide = idx;
      dots.forEach((d, i) => d.classList.toggle('active', i === idx));
      _triggerSlideAnimations(idx);
    }
  }, { passive: true });

  // Trigger entrance animations using IntersectionObserver on slide elements
  const obEntrance = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.querySelectorAll('.ob-animate').forEach(el => el.classList.add('ob-visible'));
      }
    });
  }, { threshold: 0.3 });

  document.querySelectorAll('.onboard-slide').forEach(s => obEntrance.observe(s));

  // Also trigger immediately for slide 0
  document.querySelectorAll('.onboard-slide')[0]?.querySelectorAll('.ob-animate').forEach(el => {
    setTimeout(() => el.classList.add('ob-visible'), 200);
  });

  function _triggerSlideAnimations(idx) {
    // Slide 2: count up 0→73
    if (idx === 1 && !_ob2Done && ob2Number) {
      _ob2Done = true;
      let v = 0;
      const start = performance.now();
      const dur = 800;
      function tick(now) {
        const t = Math.min((now - start) / dur, 1);
        const ease = 1 - Math.pow(1 - t, 3);
        v = Math.round(ease * 73);
        ob2Number.textContent = v + '%';
        if (t < 1) requestAnimationFrame(tick);
      }
      requestAnimationFrame(tick);
    }
    // Slide 4: score count up + bar fills
    if (idx === 3 && !_ob4Done && ob4Score) {
      _ob4Done = true;
      let v = 0;
      const start = performance.now();
      const dur = 600;
      function tick(now) {
        const t = Math.min((now - start) / dur, 1);
        const ease = 1 - Math.pow(1 - t, 3);
        v = Math.round(ease * 84);
        ob4Score.textContent = v;
        if (t < 1) requestAnimationFrame(tick);
      }
      requestAnimationFrame(tick);
      // Animate bars
      setTimeout(() => {
        document.querySelectorAll('.onboard-bar-fill').forEach(bar => {
          const w = bar.dataset.width;
          bar.style.width = w + '%';
        });
      }, 300);
    }
  }
})();
// Start polling — data load happens inside init() once IS_LIVE is resolved
startScannerPoll();

// ── from insights.js · Insights tab ──
document.getElementById('insightsAgencySearch')?.addEventListener('input', _renderAgencyTable);

// ── from settings.js · Macro-area card grid (Scan Areas Settings redesign) ──
document.getElementById('btnAreaSelectAll')?.addEventListener('click', () => {
  const city  = _activeScanCity();
  const macros = _getMacroAreasForCity(city);
  for (const m of macros) _toggleMacroArea(m.slug, true);
});
document.getElementById('btnAreaClearAll')?.addEventListener('click', () => {
  // Flip every currently-active entry off — _settingsCurrentAreas may
  // hold legacy display-name entries too, so we deactivate them all
  // to avoid silently keeping stale Milan zones active across saves.
  _settingsCurrentAreas.forEach(a => { a.active = false; });
  _renderScanAreasGrid();
});

// ── from settings.js · Badge threshold settings ──
_loadGemSettings();   // build immediately with current scoreSettings
document.getElementById('btnGemSettingsSave')?.addEventListener('click', async () => {
  if (!IS_LIVE) return;
  const statusEl = document.getElementById('gemSettingsStatus');
  const payload  = {};
  for (const f of [..._GEM_FIELDS, ..._GV_FIELDS]) {
    const el = document.getElementById(`gs_${f.key}`);
    if (el) payload[f.key] = Number(el.value);
  }
  statusEl.textContent = 'Saving…';
  statusEl.style.color = '';
  try {
    // 1. Persist thresholds
    const saveR = await fetch('/api/scoring-settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!saveR.ok) throw new Error((await saveR.json()).error || 'Save failed');
    Object.assign(scoreSettings, payload);

    // 2. Immediately rescore
    statusEl.textContent = 'Rescoring…';
    const rescoreR = await fetch('/api/rescore', { method: 'POST' });
    const j = await rescoreR.json();
    if (!rescoreR.ok) throw new Error(j.error || 'Rescore failed');

    statusEl.textContent = `✓ Saved & rescored ${j.count || ''} listings`;
    statusEl.style.color = 'var(--green-mid)';
    await loadRentals();   // refresh cards
  } catch (e) {
    statusEl.textContent = '✗ ' + e.message;
    statusEl.style.color = 'var(--coral)';
  }
  setTimeout(() => { statusEl.textContent = ''; statusEl.style.color = ''; }, 4000);
});
// Build with defaults immediately so the grid is never empty on first open
try { _buildScoreSettingsGrid(scoreSettings); } catch(e) { console.error('scoreGrid init:', e); }
document.getElementById('btnScoreSettingsSave')?.addEventListener('click', async () => {
  const statusEl = document.getElementById('scoreSettingsStatus');
  const payload = {};
  for (const f of _SCORE_FIELDS) {
    const el = document.getElementById(`ss_${f.key}`);
    if (el) payload[f.key] = f.type === 'range' || f.step < 1 ? Number(el.value) : parseInt(el.value);
  }
  statusEl.textContent = 'Saving…';
  try {
    const r = await fetch('/api/scoring-settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const j = await r.json();
    statusEl.textContent = r.ok ? '✓ Saved' : '✗ ' + (j.error || 'Error');
    statusEl.style.color = r.ok ? 'var(--green-mid)' : 'var(--coral)';
    if (r.ok) Object.assign(scoreSettings, payload);
    setTimeout(() => { statusEl.textContent = ''; statusEl.style.color = ''; }, 3000);
  } catch (e) {
    statusEl.textContent = '✗ ' + e.message;
    statusEl.style.color = 'var(--coral)';
  }
});
document.getElementById('btnRescore')?.addEventListener('click', async () => {
  const statusEl = document.getElementById('scoreSettingsStatus');
  statusEl.textContent = 'Rescoring…';
  try {
    const r = await fetch('/api/rescore', { method: 'POST' });
    const j = await r.json();
    statusEl.textContent = r.ok ? `✓ Rescored ${j.count || ''} listings` : '✗ ' + (j.error || 'Error');
    statusEl.style.color = r.ok ? 'var(--green-mid)' : 'var(--coral)';
    if (r.ok) { await loadRentals(); }
    setTimeout(() => { statusEl.textContent = ''; statusEl.style.color = ''; }, 4000);
  } catch (e) {
    statusEl.textContent = '✗ ' + e.message;
    statusEl.style.color = 'var(--coral)';
  }
});
document.getElementById('btnSettingsSave')?.addEventListener('click', async () => {
  const statusEl = document.getElementById('settingsStatus');
  const setStatus = (msg, color = '') => { if (statusEl) { statusEl.textContent = msg; statusEl.style.color = color; } };
  const city = _activeScanCity();
  setStatus('Saving…');
  // New endpoint writes {active_areas:[slug,...]} directly + syncs
  // scan_prefs.json for Milan so the scan daemon picks up the change
  // without a separate POST.
  const activeSlugs = [..._getActiveMacroSlugs()];
  try {
    const r = await fetch('/api/save-areas', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ city, areas: activeSlugs }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    const savedLbl = (typeof t === 'function' && t('settings.areas_saved')) || 'Scan areas saved';
    setStatus(`✓ ${savedLbl} — ${city} (${activeSlugs.length})`, 'var(--green-mid)');
    // Drawer chips still speak display names. Hand them the prettified
    // macro labels so the user sees the same set the Settings card just
    // confirmed (Milan only — other cities don't anchor those multi-selects).
    if (city === 'milano') {
      const displayNames = activeSlugs.map(_prettifyMacroSlug);
      if (typeof scanAreaMsd      !== 'undefined') scanAreaMsd.setSelected(displayNames);
      if (typeof saveScanPrefs       === 'function') saveScanPrefs();
      if (typeof _populateDrawerAreaFromActive === 'function') _populateDrawerAreaFromActive();
    }
  } catch (e) {
    const errLbl = (typeof t === 'function' && t('settings.areas_save_error')) || 'Could not save scan areas';
    setStatus('✗ ' + errLbl + ': ' + e.message, 'var(--coral)');
  }
  setTimeout(() => setStatus(''), 4000);
});

// ── from settings.js · Data Quality dashboard ──
document.addEventListener('click', e => {
  const th = _safeClosest(e.target, '[data-dqsort]');
  if (!th) return;
  const key = th.dataset.dqsort;
  if (_dqSortKey === key) _dqSortDir = _dqSortDir === 'asc' ? 'desc' : 'asc';
  else { _dqSortKey = key; _dqSortDir = 'asc'; }
  if (_dqData) _dqRenderTable(_dqData.listings);
});
['dqGradeFilter','dqFieldFilter','dqSrcFilter'].forEach(id => {
  document.getElementById(id)?.addEventListener('change', () => {
    if (id === 'dqFieldFilter') {
      _dqActiveCell = document.getElementById(id).value || null;
      if (_dqData) _dqRenderHeatmap(_dqData.summary.field_coverage);
    }
    if (_dqData) _dqRenderTable(_dqData.listings);
  });
});
document.getElementById('btnDqRefresh')?.addEventListener('click', () => {
  _dqLoad(true);
});
document.getElementById('btnDqReenrichF')?.addEventListener('click', async () => {
  if (!_dqData) return;
  const fListings = _dqData.listings.filter(r => r.grade === 'F');
  if (!fListings.length) { alert('No F-grade listings found.'); return; }
  if (!confirm(`This will clear cache for ${fListings.length} listing(s) and re-enrich on the next scan. Continue?`)) return;
  let done = 0;
  for (const r of fListings) {
    await fetch('/cache-clear-single', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({listing_id: r.id}),
    }).catch(() => {});
    done++;
  }
  alert(`Cleared ${done} listing(s). Re-enrichment will run on the next scan or when you click Enrich.`);
  _dqLoad(true);
});
document.getElementById('btnDqExport')?.addEventListener('click', () => {
  if (!_dqData) return;
  const cols = ['id','neighbourhood','address','source','grade','completeness_pct','critical_missing','important_missing','omi_source','needs_reenrichment'];
  const header = cols.join(',');
  const rows = _dqData.listings.map(r =>
    cols.map(c => {
      const v = Array.isArray(r[c]) ? r[c].join(';') : (r[c] ?? '');
      return `"${String(v).replace(/"/g,'""')}"`;
    }).join(',')
  ).join('\n');
  const blob = new Blob([header+'\n'+rows], {type:'text/csv'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
  a.download = `data_quality_${new Date().toISOString().slice(0,10)}.csv`;
  a.click();
});
// ── Boot Auth ─────────────────────────────────────────────────────────────────
Auth.init();


// ── from filters.js · filter bar wiring ──
document.getElementById('rMustBtn')?.addEventListener('click', () => toggleRentPanel('must'));
document.getElementById('rMoreBtn')?.addEventListener('click', () => toggleRentPanel('more'));
['rMustGrid', 'dMustGrid'].forEach(id => document.getElementById(id)?.addEventListener('click', e => {
  const chip = e.target.closest('[data-must]');
  if (chip) toggleRentMust(chip.dataset.must);
}));
['rIncludeUnknown', 'dIncludeUnknown'].forEach(id => document.getElementById(id)?.addEventListener('change', e => {
  rentIncludeUnknown = e.target.checked;
  ['rIncludeUnknown', 'dIncludeUnknown'].forEach(o => { const c = document.getElementById(o); if (c) c.checked = rentIncludeUnknown; });
  renderRentals();
  saveFilters();
}));
['rCondition', 'rMinDiscount', 'rContract'].forEach(id =>
  document.getElementById(id)?.addEventListener('change', () => { renderRentals(); saveFilters(); }));
document.getElementById('rActiveChips')?.addEventListener('click', e => {
  const chip = e.target.closest('[data-clear]');
  if (chip) clearRentFilter(chip.dataset.clear);
});
// Panels close on Escape and on a click outside the filter bar.
document.addEventListener('keydown', e => { if (e.key === 'Escape') toggleRentPanel(null); });
document.addEventListener('click', e => {
  // A must-have chip re-renders while handling its own click, so the target
  // may already be detached — that's a click inside, not outside.
  if (!e.target.isConnected) return;
  if (!e.target.closest('.fbar') && !e.target.closest('.msd-panel')) toggleRentPanel(null);
});


// ── from details.js · Details view wiring ──
document.addEventListener('click', e => {
  const opener = e.target.closest && e.target.closest('[data-open-details]');
  if (opener) { e.preventDefault(); openListingModal(opener.dataset.openDetails); return; }
  const box = document.getElementById('listingModal');
  if (!box || box.hidden) return;
  if (e.target === box || e.target.closest('.lm-close')) { closeListingModal(); return; }
  const fav = e.target.closest('[data-lm-fav]');
  if (fav) {
    const now = toggleRentFav(fav.dataset.lmFav);
    fav.classList.toggle('on', now);
    fav.querySelector('span').textContent = now ? t('lm.saved') : t('lm.save');
    document.querySelectorAll(`.btn-fav[data-rent-id="${CSS.escape(fav.dataset.lmFav)}"]`).forEach(b => {
      b.classList.toggle('is-fav', now);
      b.innerHTML = favBtnInner();
    });
  }
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeListingModal(); });
document.getElementById('rSortSelect')?.addEventListener('change', e => {
  document.querySelector(`#rentSortTabs .sort-tab[data-rsort="${CSS.escape(e.target.value)}"]`)?.click();
});

// ── from shell.js · theme toggle ──
renderThemeToggle();
