// Lume dashboard — scanner status polling, enrichment panel, archive prompt, scanner controls.
// Plain script (no bundler); shares globals with the other assets/*.js files.
// Declarations only: code that runs at start-up lives in init.js, which loads
// last, so any function here may call any other.

// ── Scanner status polling ────────────────────────────────────────────────────
let _scannerPollTimer = null;

function startScannerPoll() {
  // /scanner-status only exists on the local API; online (Cloudflare) the
  // SPA fallback answered it with the page itself, every 10 s.
  if (_scannerPollTimer || !IS_LIVE) return;
  updateScannerStatus();   // immediate check
  _scannerPollTimer = setInterval(updateScannerStatus, 10000);
}

function stopScannerPoll() {
  if (_scannerPollTimer) { clearInterval(_scannerPollTimer); _scannerPollTimer = null; }
}

let _lastScannerRun = null;   // ISO string of last known run — detect new run to reload

async function updateScannerStatus() {
  try {
    const st    = await fetch('/scanner-status').then(r => r.json());
    // running = daemon alive (always, in daily mode); scanning = a scan is
    // in progress right now.
    const anyRunning  = !!st.running;
    const scanning    = !!st.scanning;

    const dot   = document.getElementById('scannerDot');
    const lbl   = document.getElementById('scannerLabel');
    const meta  = document.getElementById('scannerLastRun');
    const badge = document.getElementById('scannerNewBadge');

    dot.classList.toggle('status-running', anyRunning);
    dot.classList.toggle('status-idle', !anyRunning);
    let label = t('scan.stopped');
    if (scanning)        label = t('scan.scanning');
    else if (anyRunning) label = t('scan.daily_on');
    lbl.textContent   = label;
    lbl.style.color   = anyRunning ? 'var(--green-ink)' : 'var(--text-secondary)';

    document.getElementById('btnStartScanner').disabled = anyRunning;
    document.getElementById('btnStopScanner').disabled  = !anyRunning;

    if (st.last_run) {
      meta.textContent = t('scan.last_run', { when: st.last_run.replace('T', ' ').slice(0, 16) });
      // Auto-reload the grid when a scan finishes. The first poll only
      // records the current run — the page already loaded that data.
      if (!scanning && st.last_run !== _lastScannerRun) {
        const isFirstPoll = _lastScannerRun === null;
        _lastScannerRun = st.last_run;
        if (!isFirstPoll) await loadRentals();
        if (!isFirstPoll) showToast(t('scan.updated'));
      }
      _lastScannerRun = st.last_run;
    }

    _renderPipeline(st.pipeline);

    // Idealista status row (shown only when idealista data is present)
    if (st.idealista) {
      const iRow  = document.getElementById('idealistaStatusRow');
      const iDot  = document.getElementById('idealistaDot');
      const iLbl  = document.getElementById('idealistaLabel');
      const iMeta = document.getElementById('idealistaLastRun');
      const iBadge= document.getElementById('idealistaNewBadge');
      if (iRow) {
        iRow.style.display = 'flex';
        iDot.classList.toggle('status-running', !!st.idealista.running);
        iDot.classList.toggle('status-idle', !st.idealista.running);
        iLbl.textContent  = st.idealista.scanning ? t('scan.idealista_scanning')
                          : st.idealista.running ? t('scan.idealista_on') : t('scan.idealista_stopped');
        iLbl.style.color  = st.idealista.running ? 'var(--green-ink)' : 'var(--text-secondary)';
        if (st.idealista.last_run)
          iMeta.textContent = t('scan.last_run', { when: st.idealista.last_run.replace('T', ' ').slice(0, 16) });
        if (st.idealista.new_count > 0)
          iBadge.textContent = t('scan.new_n', { n: fmt(st.idealista.new_count) });
      }
    }
    if (st.new_count != null && st.new_count > 0) {
      badge.textContent = t('scan.new_n', { n: fmt(st.new_count) });
      badge.classList.add('visible');
    } else {
      badge.classList.remove('visible');
    }

    // Skipped areas — amber pill (compact, in the Rentals header) +
    // expandable drill-down in the Settings → Scanner card.
    const skipWarn = document.getElementById('scannerSkippedWarn');
    const skipped  = st.immobiliare?.skipped_areas;
    if (skipWarn) {
      if (skipped && skipped.length > 0) {
        skipWarn.style.display = '';
        const lbl = t('scan.areas_skipped') || 'areas skipped';
        skipWarn.textContent   = `${skipped.length} ${lbl}`;
        skipWarn.title         = skipped.map(s => `${s.name}: ${s.reason}`).join('\n');
        skipWarn.onclick       = () => {
          switchTab('settings');
          // Defer to next tick so the Settings DOM is laid out before
          // we expand + scroll to the details block.
          setTimeout(() => {
            const d = document.getElementById('scanSkippedDetails');
            if (d) { d.open = true; d.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
          }, 50);
        };
      } else {
        skipWarn.style.display = 'none';
      }
    }
    // Settings-tab drill-down: per-area reason + suggested slug fix.
    const skipDet = document.getElementById('scanSkippedDetails');
    if (skipDet) {
      if (skipped && skipped.length > 0) {
        skipDet.hidden = false;
        const sum = document.getElementById('scanSkippedSummary');
        if (sum) {
          const lbl = t('scan.areas_skipped') || 'areas skipped';
          sum.textContent = `${skipped.length} ${lbl}`;
        }
        const list = document.getElementById('scanSkippedList');
        if (list) {
          list.innerHTML = skipped.map(s => {
            const hint = s.suggested_slug
              ? ` <span class="scan-fix-hint">→ try slug: <code>${escapeHtml(s.suggested_slug)}</code></span>`
              : '';
            return `<div class="scan-skipped-item">`
                 + `<code>${escapeHtml(s.name || '?')}</code> — ${escapeHtml(s.reason || '')}`
                 + hint
                 + `</div>`;
          }).join('');
        }
      } else {
        skipDet.hidden = true;
      }
    }

    // Geo enrichment progress — rentals
    if (st.geo_enrich !== undefined) {
      const ge        = st.geo_enrich;
      const gBtn      = document.getElementById('btnEnrichGeo');
      const gBar      = document.getElementById('geoProgressBar');
      const gFill     = document.getElementById('geoProgressFill');
      const gStatus   = document.getElementById('geoEnrichStatus');
      if (gBtn) {
        const wasRunning = gBtn._geoWasRunning || false;
        if (ge.running) {
          gBtn._geoWasRunning = true;
          gBtn.textContent = '■ Stop';
          gBtn.disabled    = false;
          if (gBar)  gBar.style.display = 'block';
          if (gFill && ge.total > 0)
            gFill.style.width = Math.round(ge.done / ge.total * 100) + '%';
          if (gStatus) gStatus.textContent = ge.total > 0 ? ge.done + ' / ' + ge.total : '…';
        } else {
          gBtn.textContent = ge.done > 0 ? 'Enrich more' : 'Enrich';
          gBtn.disabled    = false;
          if (gBar)    gBar.style.display  = 'none';
          if (gStatus) gStatus.textContent = ge.error
            ? '✗ ' + ge.error
            : (ge.done > 0 ? '✓ done' : '');
          // Refresh cache stats when enrichment just finished
          if (wasRunning && !ge.running && ge.done > 0) {
            gBtn._geoWasRunning = false;
            loadCacheStats();
          }
        }
      }
    }

    // Multi-city enrichment panel (Settings tab). Renders per-(city,kind)
    // progress rows + the queue. Safe to call every tick — DOM diff is
    // a single innerHTML write.
    if (typeof _renderEnrichmentPanel === 'function') {
      _renderEnrichmentPanel(st);
      // Stash for the Stop button's optimistic render path so it can
      // diff from the last known-good payload.
      window._lastScannerStatus = st;
      // While the queue is actually working, drop into a 1.5 s poll so
      // the per-city progress bars feel live instead of the default 10 s
      // scanner cadence. _maybeArmEnrichmentFastPoll() handles arm/disarm.
      _maybeArmEnrichmentFastPoll(st);
      // Auto-fire Run-all when new pending listings arrive (e.g. after a
      // sales fetch or a cache invalidation), so the panel doesn't sit
      // at "done" forever waiting for a manual click.
      _maybeAutoEnqueueEnrichment(st);
    }
  } catch { /* /scanner-status unavailable — no-op */ }
}

// ── Enrichment fast-poll ────────────────────────────────────────────────────
// 1.5 s status refresh while any (city, kind) job is running OR the queue
// orchestrator is alive. Self-extinguishes the moment everything's idle
// so we're not hammering Flask after the queue drains.
let _enrichFastPollTimer = null;
function _maybeArmEnrichmentFastPoll(st) {
  const jobs       = (st && st.geo_jobs) || {};
  const anyRunning = Object.values(jobs).some(j => j && j.running);
  const queueLive  = !!(st && (st.geo_queue_running || (st.geo_queue || []).length));
  const wantFast   = anyRunning || queueLive;

  if (wantFast && !_enrichFastPollTimer) {
    _enrichFastPollTimer = setInterval(() => {
      if (typeof updateScannerStatus === 'function') updateScannerStatus();
    }, 1500);
  } else if (!wantFast && _enrichFastPollTimer) {
    clearInterval(_enrichFastPollTimer);
    _enrichFastPollTimer = null;
  }
}

// ── Auto-enqueue enrichment when new pending work appears ───────────────────
// Listings keep arriving (sales fetches, scanner ticks, manual cache
// invalidations). Previously the Settings panel would sit at "all done"
// until the user clicked Run-all again. Now we auto-fire the same POST
// the button does, with two guards:
//   1. _enrichAutoPausedUntilReload — set when the user clicks Stop, so
//      we don't undo their intent until the page reloads.
//   2. 60 s cooldown between auto-fires — caps the failure-loop blast
//      radius if a worker can't make progress on the same batch.
// ── Archive-listing prompt (fired by the card's View button) ────────────────
// When the user clicks View, a small floating banner appears bottom-right.
// If they confirm the listing is gone from the source site, one click
// flips is_stale=true + excluded=true in Supabase AND in the local JSON,
// and the card disappears from the grid immediately.
function _archiveBannerHost() {
  let host = document.getElementById('archiveBannerStack');
  if (!host) {
    host = document.createElement('div');
    host.id = 'archiveBannerStack';
    host.className = 'archive-banner-stack';
    document.body.appendChild(host);
  }
  return host;
}

// Per-listing probe lock — if the user clicks View on the same listing
// twice in quick succession, we skip the second probe (the first one's
// banner, if any, is already on its way).
const _probedListings = new Set();

async function _promptArchiveListing(lid, listingType, anchorEl) {
  if (!lid || !IS_LIVE) return true;   // static mode: just let the link open
  if (_probedListings.has(lid))  return true;
  _probedListings.add(lid);
  // Clear the per-listing lock after a generous window so subsequent
  // clicks on the same card (after a re-render) can probe again.
  setTimeout(() => _probedListings.delete(lid), 60000);

  // Pull a short title hint NOW (the card may scroll out of view by the
  // time the probe finishes).
  const card = anchorEl?.closest?.('.sale-card, .rental-card, .lume-card, .card');
  const titleHint = (card?.querySelector('.card-title, .listing-title, h2, h3')
                      ?.textContent || `#${lid}`).trim().slice(0, 50);
  const listingUrl = anchorEl?.href || '';

  // Probe in the background. Fire-and-forget — the link is already
  // opening in a new tab via target=_blank.
  let probe = null;
  try {
    const r = await fetch('/api/check-listing-availability', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ listing_id: lid, url: listingUrl,
                                listing_type: listingType || 'rental' }),
    });
    probe = r.ok ? await r.json() : null;
  } catch { /* network blip — treat as inconclusive */ }

  // Only surface the banner when the probe is CONFIDENT the listing is
  // gone. 'unknown' / probe failure / available = no banner (silent).
  if (!probe || probe.available !== false) return true;

  const host = _archiveBannerHost();
  if (host.querySelector(`[data-lid="${lid}"]`)) return true;   // double-fire safety

  const reasonHint = ({
    redirected: 'page redirected away',
    no_data:    'no listing data on page',
  })[probe.reason] || probe.reason || '';

  const banner = document.createElement('div');
  banner.className = 'archive-banner';
  banner.dataset.lid = lid;
  banner.innerHTML = `
    <div class="archive-banner-text">
      ${escapeHtml((typeof t === 'function' && t('card.archive_prompt')) || 'This listing appears to be unavailable.')}
      <div class="archive-banner-sub">${escapeHtml(titleHint)}${reasonHint ? ' — ' + escapeHtml(reasonHint) : ''}</div>
    </div>
    <div class="archive-banner-actions">
      <button class="archive-banner-btn primary" data-action="archive">📦 ${escapeHtml((typeof t === 'function' && t('card.archive')) || 'Archive')}</button>
      <button class="archive-banner-btn subtle"  data-action="dismiss" aria-label="Dismiss">✕</button>
    </div>
  `;
  host.appendChild(banner);

  // Auto-dismiss after 30 s so banners don't pile up if the user
  // browses through many listings.
  const autoDismiss = setTimeout(() => banner.remove(), 30000);

  banner.addEventListener('click', async (ev) => {
    const action = ev.target?.dataset?.action;
    if (!action) return;
    clearTimeout(autoDismiss);
    if (action === 'dismiss') { banner.remove(); return; }
    if (action !== 'archive') return;
    const btn = ev.target;
    btn.disabled = true;
    try {
      const r = await fetch('/api/archive-listing', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({
          listing_id:   lid,
          listing_type: listingType || 'rental',
          reason:       'User-archived from View-button banner',
        }),
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      banner.classList.add('is-done');
      banner.innerHTML = `<div class="archive-banner-text">✓ ${escapeHtml((typeof t === 'function' && t('card.archived_ok')) || 'Archived')}</div>`;
      // Hide the card immediately so the dashboard reflects the change
      // without waiting for the next render.
      if (card) card.style.display = 'none';
      setTimeout(() => banner.remove(), 2500);
    } catch (err) {
      banner.classList.add('is-error');
      const lbl = (typeof t === 'function' && t('card.archive_failed')) || 'Could not archive listing';
      banner.innerHTML = `<div class="archive-banner-text" style="color:var(--coral,var(--coral))">✗ ${escapeHtml(lbl)}: ${escapeHtml(err.message)}</div>`;
      setTimeout(() => banner.remove(), 4500);
    }
  });
  return true;   // don't block the View link from opening
}

const _AUTO_ENRICH               = [['milano', 'rental']];
let _enrichAutoLastFireAt        = 0;
let _enrichAutoPausedUntilReload = false;
const _ENRICH_AUTO_COOLDOWN_MS   = 60 * 1000;
async function _maybeAutoEnqueueEnrichment(st) {
  if (!IS_LIVE) return;
  if (_enrichAutoPausedUntilReload) return;
  const jobs       = (st && st.geo_jobs) || {};
  const queue      = (st && st.geo_queue) || [];
  const pending    = (st && st.geo_pending) || {};
  const queueLive  = !!(st && st.geo_queue_running);
  const anyRunning = Object.values(jobs).some(j => j && j.running);
  if (queueLive || queue.length || anyRunning) return;
  // Only the active scope (Milan rentals) — other cities/sales are parked,
  // and the API refuses to auto-queue them anyway (ACTIVE_ENRICH_CITIES).
  const totalPending = _AUTO_ENRICH.reduce(
    (s, [c, k]) => s + ((pending[`${c}:${k}`] || {}).count || 0), 0);
  if (totalPending <= 0) return;
  const now = Date.now();
  if (now - _enrichAutoLastFireAt < _ENRICH_AUTO_COOLDOWN_MS) return;
  _enrichAutoLastFireAt = now;
  try {
    const r = await fetch('/geo-enrich-queue', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        cities: _AUTO_ENRICH.map(([c]) => c), kinds: _AUTO_ENRICH.map(([, k]) => k),
      }),
    });
    // 409 = orchestrator already running (race with manual click). Either
    // outcome is fine — both mean the queue is now alive.
    if (r.ok || r.status === 409) {
      if (typeof updateScannerStatus === 'function') updateScannerStatus();
    }
  } catch { /* network blip — next poll will retry after cooldown */ }
}

// ── Multi-city enrichment panel ──────────────────────────────────────────────
// Renders one row per (city, kind) job from the backend's _geo_jobs slot.
// Rows that haven't been touched yet appear in "idle" state so the user
// always sees the full grid of cities × kinds. Once they click "Run all"
// the orchestrator drains the queue sequentially and each row transitions
// queued → running → done in turn.
// Milan rentals only (scope since 2026-10-14).
const _ENRICH_CITY_ORDER = ['milano'];
const _ENRICH_CITY_LABELS = {
  milano: 'Milano', roma: 'Roma', napoli: 'Napoli', la_maddalena: 'La Maddalena',
};
const _ENRICH_KINDS = ['rental'];

function _enrichCityLabel(code) {
  // Prefer the same display name the city pill is using so the i18n
  // toggle propagates here too.
  if (typeof availableCities !== 'undefined') {
    const cfg = availableCities.find(c => c.code === code);
    if (cfg) {
      return (typeof _cityDisplayName === 'function')
        ? _cityDisplayName(cfg)
        : (cfg.display_name_en || cfg.code);
    }
  }
  return _ENRICH_CITY_LABELS[code] || code;
}

function _renderEnrichmentPanel(st) {
  const wrap = document.getElementById('enrichmentPanel');
  if (!wrap) return;
  // Hide in static mode (no Flask backend = nothing to enrich).
  wrap.hidden = !IS_LIVE;
  if (!IS_LIVE) return;

  const body = document.getElementById('enrichmentPanelBody');
  if (!body) return;

  const jobs       = (st && st.geo_jobs)    || {};
  const queue      = (st && st.geo_queue)   || [];
  const pending    = (st && st.geo_pending) || {};
  const queueLive  = !!(st && st.geo_queue_running);

  // Toolbar state — "Run for all" disabled while any job is running OR
  // queue is draining; "Stop" only visible while there's something to stop.
  const anyRunning  = Object.values(jobs).some(j => j && j.running);
  const queueActive = queueLive || queue.length > 0 || anyRunning;
  const totalPending = Object.values(pending).reduce(
    (s, p) => s + ((p && p.count) || 0), 0);
  const runBtn  = document.getElementById('btnEnrichAll');
  const stopBtn = document.getElementById('btnEnrichStop');
  if (runBtn) {
    runBtn.disabled = queueActive;
    // Bake the total pending count into the button label when nothing is
    // running so the user knows exactly how much work clicking will do.
    runBtn.textContent = queueActive
      ? t('tools.enrich_running')
      : (totalPending > 0 ? t('tools.enrich_run_n', { n: fmt(totalPending) }) : t('tools.enrich_run'));
  }
  if (stopBtn) stopBtn.hidden  = !queueActive;

  const queueLabel = document.getElementById('enrichQueueStatus');
  if (queueLabel) {
    if (queueActive) {
      const done    = Object.values(jobs).filter(j => j && j.state === 'done').length;
      const total   = _ENRICH_CITY_ORDER.length * _ENRICH_KINDS.length;
      queueLabel.textContent = `Queue: ${done}/${total} jobs done`;
      queueLabel.style.color = 'var(--green-deep)';
    } else {
      queueLabel.textContent = '';
    }
  }

  // Build one row per city, with rentals + sales as side-by-side cells.
  const rows = [];
  for (const city of _ENRICH_CITY_ORDER) {
    const cellsHtml = _ENRICH_KINDS.map(kind => {
      const key  = `${city}:${kind}`;
      const job  = jobs[key] || { state: 'idle', done: 0, total: 0 };
      const pend = pending[key] || { count: 0, total: 0, stale: false };
      const state = job.state || (job.running ? 'running'
                                              : (job.error ? 'error'
                                                           : (job.done > 0 ? 'done' : 'idle')));
      const pctNum = job.total > 0 ? Math.round((job.done / job.total) * 100) : 0;
      const indet  = job.running && (!job.total || job.total === 0);

      // Idle cells show the pending-listing preview so the user knows up
      // front how much each job will process. Running/queued/done cells
      // show the in-flight done/total counter.
      let primary, sub;
      if (state === 'idle') {
        if (pend.stale) {
          primary = '—';
          sub     = 'no data';
        } else if (pend.count === 0) {
          primary = '✓ 0';
          sub     = pend.total > 0 ? `${pend.total} listings` : 'all done';
        } else {
          primary = `<span class="pending-num">${pend.count}</span>`;
          sub     = `of ${pend.total} need enrichment`;
        }
      } else if (state === 'queued') {
        primary = pend.count > 0 ? String(pend.count) : '…';
        sub     = 'queued';
      } else if (state === 'running') {
        primary = job.total > 0 ? `${job.done}/${job.total}` : '…';
        // Phase label takes precedence over the generic "in progress" so
        // the user knows whether we're geocoding, rescoring, or pushing.
        sub     = job.phase_label || 'in progress';
      } else if (state === 'done') {
        primary = `✓ ${job.done}`;
        sub     = job.total > 0 ? `of ${job.total}` : 'done';
      } else if (state === 'stopped') {
        primary = job.done > 0 ? `${job.done}/${job.total || '?'}` : '—';
        sub     = 'stopped';
      } else { // error
        primary = '✗';
        sub     = 'failed';
      }
      const stateLabel = {
        idle:    'Idle',
        queued:  'Queued',
        running: job.phase_label || 'Running…',
        done:    'Done',
        stopped: 'Stopped',
        error:   'Error',
      }[state] || state;

      const kindLabel = kind === 'rental' ? 'Rentals' : 'Sales';
      const kindClass = kind === 'rental' ? 'is-rental' : 'is-sale';

      const barHtml = (state === 'idle' || state === 'queued')
        ? '<div class="enrich-bar" style="visibility:hidden"></div>'
        : `<div class="enrich-bar"><div class="enrich-bar-fill${indet ? ' is-indeterminate' : ''}"
             style="width:${indet ? 30 : pctNum}%"></div></div>`;

      return `
        <div class="enrich-cell is-${state}" data-job="${escapeHtml(key)}">
          <span class="kind-pill ${kindClass}">${escapeHtml(kindLabel)}</span>
          ${barHtml}
          <div class="meta">
            <div class="enrich-count">${primary}</div>
            <div class="enrich-state is-${state}" title="${escapeHtml(sub)}">${escapeHtml(stateLabel)}</div>
          </div>
          ${job.error
            ? `<div class="cell-error">${escapeHtml(job.error)}</div>`
            : ''}
        </div>`;
    }).join('');

    // City-row aggregate: sum pending across rentals + sales for the title.
    const cityPending = _ENRICH_KINDS.reduce((s, k) => {
      const j = jobs[`${city}:${k}`];
      // Skip slots whose state has already settled this session so the
      // header doesn't double-count "pending" after a row reports done.
      if (j && (j.state === 'done' || j.state === 'stopped')) return s;
      return s + ((pending[`${city}:${k}`] || {}).count || 0);
    }, 0);
    const cityRowState = _ENRICH_KINDS.some(
      k => jobs[`${city}:${k}`] && jobs[`${city}:${k}`].running) ? 'is-running' : '';
    const cityRowError = _ENRICH_KINDS.some(
      k => jobs[`${city}:${k}`] && jobs[`${city}:${k}`].state === 'error') ? 'is-error' : '';

    rows.push(`
      <div class="enrich-row ${cityRowState} ${cityRowError}">
        <div class="city-name">
          ${escapeHtml(_enrichCityLabel(city))}
          ${cityPending > 0
            ? `<span class="city-pending-total">${cityPending} pending</span>`
            : '<span class="city-pending-total">all enriched</span>'}
        </div>
        ${cellsHtml}
      </div>`);
  }
  body.innerHTML = rows.join('');
}

// ── Scanner: city selector ───────────────────────────────────────────────────
// Per-user selection of which cities the local scanner targets. Persisted
// in localStorage so a reload remembers the choice. The Settings card
// reads this both when rendering the checkboxes AND when POSTing to
// /start-scanner so the user can scan 1+ cities in one click.
const SCAN_CITIES_KEY = 'lume_scan_cities';
// Milan only (the multi-city picker is parked in full.html): whatever an
// old browser stored, the scanner gets Milan.
function _readScanCities() {
  return ['milano'];
}

const _SCAN_CITY_META = {
  milano:       { flag: '',  label: 'Milano' },
  roma:         { flag: '',  label: 'Roma' },
  napoli:       { flag: '',  label: 'Napoli' },
  la_maddalena: { flag: '',  label: 'La Maddalena' },
};

function toggleScanCity(code, enabled) {
  const current = _readScanCities();
  const next = enabled
    ? Array.from(new Set([...current, code]))
    : current.filter(c => c !== code);
  // Guarantee at least one city so the Start button never fires an empty list.
  if (!next.length) next.push('milano');
  try { localStorage.setItem(SCAN_CITIES_KEY, JSON.stringify(next)); } catch (_) {}
  const card  = document.getElementById(`cityScan_${code}`);
  const track = card?.querySelector('.toggle-track');
  if (card)  card.classList.toggle('city-scan-card-on', enabled);
  if (track) track.classList.toggle('toggle-on', enabled);
}

function _renderCityScanGrid() {
  const grid = document.getElementById('cityScanGrid');
  if (!grid) return;
  const enabled = new Set(_readScanCities());
  // Use availableCities (loaded from Supabase) when present so the grid
  // matches the city pill exactly; fall back to the fixed _SCAN_CITY_META
  // ordering for first-paint before Supabase resolves.
  const order = (typeof availableCities !== 'undefined' && availableCities.length)
    ? availableCities.map(c => c.code)
    : Object.keys(_SCAN_CITY_META);
  grid.innerHTML = order.map(code => {
    const meta = _SCAN_CITY_META[code] || { flag: '', label: code };
    const on   = enabled.has(code);
    return `
      <label class="city-scan-card ${on ? 'city-scan-card-on' : ''}"
             id="cityScan_${code}">
        <input type="checkbox" data-city="${code}" ${on ? 'checked' : ''}
               onchange="toggleScanCity('${code}', this.checked)">
        <div class="city-scan-icon">${meta.flag}</div>
        <div class="city-scan-name">${escapeHtml(meta.label)}</div>
        <div class="toggle-track ${on ? 'toggle-on' : ''}">
          <div class="toggle-thumb"></div>
        </div>
      </label>`;
  }).join('');
}

// ── Scanner start/stop buttons ────────────────────────────────────────────────


// The four "workers" (Tools → Scanner): zone tour, new-listing search,
// listing pages, photo reading — what each did last, today and next, and
// progress on the top listings (api.py _pipeline_status).
function _renderPipeline(p) {
  const box = document.getElementById('pipelineBox');
  if (!box) return;
  if (!p || p.error || !p.sites) { box.hidden = true; return; }
  const esc = escapeHtml;
  const hm = iso => iso ? String(iso).slice(11, 16) : '–';
  const next = iso => !iso ? '–' : (new Date(iso) <= new Date() ? t('w.soon') : hm(iso));
  const name = { immobiliare: 'Immobiliare', idealista: 'Idealista' };
  const area = a => formatAddress(String(a || '').replace(/-/g, ' '));
  const S = p.sites, srcs = ['immobiliare', 'idealista'];
  const lines = f => srcs.map(src => `<div class="w-line">${esc(f(src, S[src] || {}))}</div>`).join('');
  const prog = (label, v) => {
    const n = p.top.listings || 1, pct = Math.round(v / n * 100);
    return `<div class="pipe-bar"><span>${esc(label)}</span><span class="pipe-track"><i style="width:${pct}%"></i></span><span>${fmt(v)} / ${fmt(p.top.listings)}</span></div>`;
  };
  const card = (ic, title, sub, body) => `<section class="w-card">
      <div class="w-head">${icon(ic)}<div><div class="w-title">${esc(title)}</div><div class="w-sub">${esc(sub)}</div></div></div>${body}</section>`;
  const sum = (f) => srcs.reduce((a, s) => a + (f(S[s] || {}) || 0), 0);

  const slots = lines((src, s) => s.slot?.last
    ? t('w.slot_line', { site: name[src], at: hm(s.slot.last.at), area: area(s.slot.last.area), n: fmt(s.slot.last.new || 0), next: next(s.slot.next) })
    : t('w.never', { site: name[src] }))
    + `<div class="w-today">${esc(t('w.today_slots', { n: sum(s => s.slot?.today), k: fmt(sum(s => s.slot?.new_today)) }))}</div>`;
  const disc = lines((src, s) => s.discover?.last
    ? t('w.discover_line', { site: name[src], at: hm(s.discover.last.at), n: fmt(s.discover.last.new || 0), next: next(s.discover.next) })
    : t('w.never', { site: name[src] }))
    + `<div class="w-today">${esc(t('w.today_discover', { n: fmt(sum(s => s.discover?.new_today)) }))}</div>`;
  const pages = lines((src, s) => {
    const d = s.pages || {};
    if (d.paused_until) return t('w.pages_paused', { site: name[src], until: hm(d.paused_until) });
    return d.last ? t('w.pages_line', { site: name[src], at: hm(d.last.at), ok: fmt(d.last.ok), h: fmt(d.per_hour) })
                  : t('w.never', { site: name[src] });
  }) + `<div class="w-today">${esc(t('w.today_pages', { n: fmt(sum(s => s.pages?.read_today)) }))}</div>`
    + prog(t('w.prog_pages'), p.top.page_read) + prog(t('w.prog_costs'), p.top.costs_known);
  const ph = p.photos || {};
  const photos = `<div class="w-line">${esc(ph.last ? t('w.photos_line', { at: hm(ph.last.at), n: fmt(ph.last.stored) }) : '–')}</div>`
    + `<div class="w-today">${esc(t('w.today_photos', { n: fmt(ph.today || 0) }))}</div>`
    + prog(t('w.prog_photos'), p.top.photos_read)
    + `<div class="w-line">${esc(t('w.waiting', { n: fmt(p.top.photos_waiting || 0) }))}</div>`;

  box.innerHTML = card('map', '1 · ' + t('w.slots'), t('w.slots_sub'), slots)
    + card('sparkles', '2 · ' + t('w.discover'), t('w.discover_sub'), disc)
    + card('file-text', '3 · ' + t('w.pages'), t('w.pages_sub', { min: p.min_score }), pages)
    + card('photo', '4 · ' + t('w.photos'), t('w.photos_sub'), photos);
  box.hidden = false;
}
