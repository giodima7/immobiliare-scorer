// Lume dashboard — email alerts (digest_filters through the local API).
// Plain script (no bundler); shares globals with the other assets/*.js files.
// Declarations only: code that runs at start-up lives in init.js, which loads
// last, so any function here may call any other.

// ── Digest filters (Supabase-backed daily email alerts) ──────────────────────
// The 🔔 Save alert button in the rentals filter bar dumps the current filter
// state into Supabase's digest_filters table; the daily_digest.yml workflow
// then reads it next morning to send a per-user email of new + price-dropped
// listings. Settings tab surfaces a pause/resume control.
//
// Filter element IDs (rentals tab): rMaxRent, rMinSqm, rMinRooms, rMaxMetro,
// rMinFloor, rElevator, rMinEnergy, rScore, rFascia, rOmiZone. Source +
// Gems are button groups read from .active state.
function _readActiveFilterState() {
  const num  = id => { const v = parseFloat(document.getElementById(id)?.value); return Number.isFinite(v) ? v : null; };
  const str  = id => document.getElementById(id)?.value || null;
  const bool = id => !!document.getElementById(id)?.checked;
  const activeBtn = (sel, attr) => {
    const b = document.querySelector(`${sel} .active`);
    return b ? (b.dataset[attr] || '') : '';
  };
  // Single-value selects → arrays (so the schema mirrors the multi-select
  // form the digest script + future UI both expect).
  const fasciaVal = str('rFascia');
  const omiVal    = str('rOmiZone');
  const gemRaw    = activeBtn('#rGemFilter', 'gem');           // '' | 'hidden' | 'good'
  const srcRaw    = activeBtn('#rSourceFilter', 'rsource');    // '' | 'immobiliare' | 'idealista' | 'subito' | 'casait' | 'private'

  return {
    max_rent:        num('rMaxRent')   || null,
    min_sqm:         num('rMinSqm')    || null,
    min_rooms:       num('rMinRooms')  || null,
    max_metro_min:   num('rMaxMetro')  || null,
    min_score:       num('rScore')     || 0,
    min_floor:       num('rMinFloor')  || null,
    require_elevator: bool('rElevator'),
    min_energy:      str('rMinEnergy') || null,
    fascia:          fasciaVal ? [fasciaVal] : [],
    omi_zona:        omiVal    ? [omiVal]    : [],
    // Map dashboard's internal codes onto digest_filters' canonical values:
    //   button data-gem ''       → 'all'
    //                  'hidden'  → 'hidden'
    //                  'good'    → 'great_value'
    gems_filter:     gemRaw === 'hidden' ? 'hidden'
                   : gemRaw === 'good'   ? 'great_value' : 'all',
    source_filter:   srcRaw || 'all',
  };
}

// Entry point for the 🔔 Save alert button. Always opens in CREATE mode
// pre-filled from the current rentals filter state — users can now have
// multiple alerts in parallel, so this never overwrites an existing one.
// To edit an existing alert, the Settings tab cards call
// openSaveAlertModal(existing) directly with mode='edit'.
// ── Email alerts: identity + transport ─────────────────────────────────────────
// Alerts live in Supabase digest_filters, closed to the public key since
// migration 013 (anyone could read or rewrite them). The local API proxies
// them with the service key (/api/digest-filters); online they can't be
// managed. Locally there is no Clerk sign-in, so the owner's identity is
// taken from the existing alert rows, or asked for once.
let _localAlertIdentity = null;

function _alertUser() {
  const u = (typeof Auth !== 'undefined') ? Auth.user : null;
  if (IS_LIVE && _localAlertIdentity) return _localAlertIdentity;
  return IS_LIVE ? u : null;
}

function _alertsFetch(query, opts = {}) {
  if (!IS_LIVE) return Promise.resolve(new Response('[]', { status: 403 }));
  const headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
  delete headers.apikey; delete headers.Authorization;
  return fetch(`/api/digest-filters${query ? '?' + query : ''}`, Object.assign({}, opts, { headers }));
}

async function _loadLocalAlertIdentity() {
  if (!IS_LIVE) return;
  try {
    const r = await _alertsFetch('select=clerk_user_id,email,display_name&order=created_at.desc&limit=1');
    const rows = r.ok ? await r.json() : [];
    let id = rows[0]?.clerk_user_id, email = rows[0]?.email, name = rows[0]?.display_name || '';
    if (!id) {
      try { email = localStorage.getItem('lume_alert_email') || ''; } catch { email = ''; }
      if (!email) return;            // asked for on the first save instead
      id = 'local';
    }
    _localAlertIdentity = { id, fullName: name, primaryEmailAddress: { emailAddress: email } };
  } catch { /* API down → alerts unavailable */ }
}

// First save with no alerts yet and no Clerk: ask for the email once.
function _ensureLocalAlertIdentity() {
  if (!IS_LIVE || _localAlertIdentity) return !!_alertUser();
  const email = (prompt('Email address for the daily alert:') || '').trim();
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) return false;
  try { localStorage.setItem('lume_alert_email', email); } catch {}
  _localAlertIdentity = { id: 'local', fullName: '', primaryEmailAddress: { emailAddress: email } };
  return true;
}

async function saveDigestFilters() {
  if (!IS_LIVE) { showToast(t('alerts.local_only')); return; }
  _ensureLocalAlertIdentity();
  const user = _alertUser();
  if (!user) {
    showToast(t('alerts.local_only'));
    return;
  }
  await openSaveAlertModal(null);  // null → create mode, seed from rentals filter bar
}

function _setSelectValue(id, value) {
  const el = document.getElementById(id);
  if (!el) return;
  el.value = (value == null ? '' : String(value));
}

// Convert a UTC HH:MM string to the user's local HH:MM for the time picker.
// PostgreSQL stores send_time_utc as TEXT 'HH:MM'; we keep storage in UTC
// so the cron-driven worker doesn't have to think about timezones, but the
// picker speaks the user's local time for sanity.
function _utcStringToLocalTime(utcHHMM) {
  if (!utcHHMM || !/^\d{1,2}:\d{2}$/.test(utcHHMM)) return '07:00';
  const [h, m] = utcHHMM.split(':').map(Number);
  const d = new Date();
  d.setUTCHours(h, m, 0, 0);
  return d.toTimeString().slice(0, 5);   // "HH:MM" in local TZ
}

function _localTimeToUtcString(localHHMM) {
  if (!localHHMM || !/^\d{1,2}:\d{2}$/.test(localHHMM)) return '07:00';
  const [h, m] = localHHMM.split(':').map(Number);
  const d = new Date();
  d.setHours(h, m, 0, 0);
  return String(d.getUTCHours()).padStart(2, '0') + ':' + String(d.getUTCMinutes()).padStart(2, '0');
}

async function openSaveAlertModal(existing) {
  // Two modes:
  //   • CREATE — existing == null. Seed from the live rentals filter bar
  //     so the alert mirrors what the user is currently looking at.
  //   • EDIT  — existing is a digest_filters row. Seed every field from
  //     that row; submit will PATCH instead of INSERT.
  const isEdit   = !!(existing && existing.id);
  const liveState = isEdit ? {} : _readActiveFilterState();
  const seed      = existing || {};

  // Track the editing id on the form so submit knows which mode it's in.
  const editingIdEl = document.getElementById('amEditingId');
  if (editingIdEl) editingIdEl.value = isEdit ? existing.id : '';

  // Update title + primary button label to reflect mode.
  const titleEl = document.getElementById('saveAlertTitle');
  if (titleEl) titleEl.textContent = isEdit
    ? (t('alert_modal.title_edit')   || 'Edit alert')
    : (t('alert_modal.title')        || 'Daily email alert');
  const submitBtnEl = document.getElementById('amSubmitBtn');
  if (submitBtnEl) submitBtnEl.textContent = isEdit
    ? (t('alert_modal.save_changes') || 'Save changes')
    : (t('alert_modal.save')         || 'Save alert');

  const pick = (key, fallback) => {
    const v = seed[key];
    return (v == null || v === '') ? fallback : v;
  };

  _setSelectValue('amName',      seed.name || '');
  _setSelectValue('amMaxRent',   pick('max_rent',      liveState.max_rent));
  _setSelectValue('amMinSqm',    pick('min_sqm',       liveState.min_sqm));
  _setSelectValue('amMinRooms',  pick('min_rooms',     liveState.min_rooms));
  _setSelectValue('amMaxMetro',  pick('max_metro_min', liveState.max_metro_min));
  _setSelectValue('amMinFloor',  pick('min_floor',     liveState.min_floor));
  _setSelectValue('amMinEnergy', pick('min_energy',    liveState.min_energy));
  // fascia / omi_zona are stored as arrays but the dashboard's filter is
  // single-select — take the first entry for prefill.
  _setSelectValue('amFascia', (seed.fascia && seed.fascia[0]) || (liveState.fascia && liveState.fascia[0]) || '');
  _setSelectValue('amGems',    seed.gems_filter   || liveState.gems_filter   || 'all');
  _setSelectValue('amSource',  seed.source_filter || liveState.source_filter || 'all');

  const score = pick('min_score', liveState.min_score || 0);
  _setSelectValue('amScore', score);
  const scoreLabel = document.getElementById('amScoreVal');
  if (scoreLabel) scoreLabel.textContent = score;

  const elev = document.getElementById('amElevator');
  if (elev) elev.checked = (seed.require_elevator != null ? !!seed.require_elevator : !!liveState.require_elevator);

  const sendTime = _utcStringToLocalTime(seed.send_time_utc || '07:00');
  _setSelectValue('amSendTime', sendTime);

  // Surface the local-timezone label in the hint so the user knows what
  // "local" means without having to guess.
  const hint = document.getElementById('amSendTimeHint');
  if (hint) {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || 'local';
    hint.textContent = (t('alert_modal.send_time_hint') || 'Hourly resolution. Stored as UTC.')
                       + ` (${tz})`;
  }

  _setSelectValue('amMinMatches', seed.min_new_listings || 1);

  const err = document.getElementById('amError');
  if (err) { err.hidden = true; err.textContent = ''; }

  const back = document.getElementById('saveAlertBackdrop');
  if (!back) return;
  back.hidden = false;
  // Re-translate every data-i18n inside in case the user changed languages
  // since last open. Cheap; only ~30 elements.
  back.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = t(el.dataset.i18n); });
  back.querySelectorAll('[data-i18n-placeholder]').forEach(el => { el.placeholder = t(el.dataset.i18nPlaceholder); });
  // Focus the alert-name input so keyboard-only users land in a useful place.
  setTimeout(() => { document.getElementById('amName')?.focus(); }, 50);
  // Esc to close.
  document.addEventListener('keydown', _saveAlertEscHandler);
}

function closeSaveAlertModal(ev) {
  // Only close on backdrop clicks if the click ORIGINATED on the backdrop
  // itself (event delegation guards against accidental dismissals from
  // input clicks bubbling up through stopPropagation gaps).
  if (ev && ev.target && ev.currentTarget && ev.target !== ev.currentTarget) return;
  const back = document.getElementById('saveAlertBackdrop');
  if (back) back.hidden = true;
  document.removeEventListener('keydown', _saveAlertEscHandler);
}

function _saveAlertEscHandler(ev) {
  if (ev.key === 'Escape') closeSaveAlertModal();
}

async function submitSaveAlert() {
  if (!IS_LIVE) { showToast(t('alerts.local_only')); return; }
  _ensureLocalAlertIdentity();
  const user = _alertUser();
  if (!user) {
    showToast(t('alerts.local_only'));
    return;
  }
  const submitBtn = document.getElementById('amSubmitBtn');
  const errEl = document.getElementById('amError');
  if (errEl) { errEl.hidden = true; errEl.textContent = ''; }

  // Read modal field values
  const num  = id => { const v = parseFloat(document.getElementById(id)?.value); return Number.isFinite(v) ? v : null; };
  const str  = id => document.getElementById(id)?.value || null;
  const bool = id => !!document.getElementById(id)?.checked;

  const editingId = (document.getElementById('amEditingId')?.value || '').trim();
  const isEdit    = !!editingId;
  const fascia    = str('amFascia');
  const email     = user.primaryEmailAddress?.emailAddress
                 || user.emailAddresses?.[0]?.emailAddress
                 || '';

  // Build the row body. clerk_user_id + email + display_name only matter on
  // INSERT (we don't want a PATCH to e.g. silently overwrite display_name
  // with stale Clerk state), so they're added conditionally below.
  // Multi-city expansion (migration 007): scope every alert to the city
  // the user is currently viewing. The digest worker honours
  // digest_filters.cities (TEXT[]) when filtering listings, so an alert
  // saved on Roma only matches Roma listings even though the user has
  // Milano alerts too.
  const filterBody = {
    name:             (str('amName') || '').trim() || null,
    max_rent:         num('amMaxRent')   || null,
    min_sqm:          num('amMinSqm')    || null,
    min_rooms:        num('amMinRooms')  || null,
    max_metro_min:    num('amMaxMetro')  || null,
    min_score:        num('amScore')     || 0,
    min_floor:        num('amMinFloor')  || null,
    require_elevator: bool('amElevator'),
    min_energy:       str('amMinEnergy') || null,
    fascia:           fascia ? [fascia] : [],
    omi_zona:         [],
    cities:           [currentCity],
    gems_filter:      str('amGems')   || 'all',
    source_filter:    str('amSource') || 'all',
    send_time_utc:    _localTimeToUtcString(str('amSendTime') || '07:00'),
    min_new_listings: Math.max(1, Math.min(50, parseInt(num('amMinMatches')) || 1)),
    active:           true,
  };

  if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = t('filter.saving') || '⏳ Saving…'; }

  try {
    let resp;
    if (isEdit) {
      // PATCH the row by primary key. clerk_user_id is preserved by
      // omission (Supabase doesn't change unspecified columns).
      resp = await _alertsFetch(`id=eq.${encodeURIComponent(editingId)}`, {
        method: 'PATCH',
        headers: {
          'apikey':        SUPABASE_ANON_KEY,
          'Authorization': `Bearer ${SUPABASE_ANON_KEY}`,
          'Content-Type':  'application/json',
          'Prefer':        'return=minimal',
        },
        body: JSON.stringify(filterBody),
      });
    } else {
      // INSERT a brand-new alert. clerk_user_id / email / display_name
      // attach the row to this user.
      const insertBody = Object.assign({
        clerk_user_id: user.id,
        email,
        display_name:  user.fullName || [user.firstName, user.lastName].filter(Boolean).join(' ') || '',
      }, filterBody);
      resp = await _alertsFetch('', {
        method: 'POST',
        headers: {
          'apikey':        SUPABASE_ANON_KEY,
          'Authorization': `Bearer ${SUPABASE_ANON_KEY}`,
          'Content-Type':  'application/json',
          'Prefer':        'return=minimal',
        },
        body: JSON.stringify(insertBody),
      });
    }
    if (!resp.ok) {
      const detail = await resp.text();
      throw new Error(`HTTP ${resp.status} ${detail.slice(0, 240)}`);
    }
    showToast(t('toast.alert_saved') || 'Daily alert saved.');
    closeSaveAlertModal();
    // Refresh the Settings tab section AND the alerts-manager modal if
    // either is currently in the DOM. renderDigestSettingsSection also
    // updates the avatar dot, so a save always lights up the badge.
    if (typeof _refreshDigestAlertBadge === 'function') _refreshDigestAlertBadge();
    const wrap = document.getElementById('digestSettingsWrap');
    if (wrap) renderDigestSettingsSection().then(html => { wrap.innerHTML = html; });
    const mgrBody = document.getElementById('alertsManagerBody');
    const mgrBack = document.getElementById('alertsManagerBackdrop');
    if (mgrBody && mgrBack && !mgrBack.hidden) {
      renderDigestSettingsSection().then(html => { mgrBody.innerHTML = html; });
    }
  } catch (err) {
    console.warn('[digest] save failed:', err);
    if (errEl) {
      errEl.hidden = false;
      errEl.textContent = `${t('alert_modal.error') || 'Could not save alert:'} ${err.message}`;
    }
  } finally {
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.textContent = isEdit
        ? (t('alert_modal.save_changes') || 'Save changes')
        : (t('alert_modal.save')         || 'Save alert');
    }
  }
}

// Fetch ALL saved alerts for a user (migration 005 dropped the unique
// constraint, so a user may now have several). Returns [] on any failure.
async function _fetchDigestFilters(userId) {
  if (!userId) return [];
  try {
    const resp = await _alertsFetch(`clerk_user_id=eq.${encodeURIComponent(userId)}&select=*&order=created_at.desc`);
    if (!resp.ok) return [];
    return await resp.json();
  } catch (_) { return []; }
}

async function toggleDigestActive(alertId, currentlyActive) {
  const user = _alertUser();
  if (!user || !alertId) return;
  try {
    const resp = await _alertsFetch(
      `id=eq.${encodeURIComponent(alertId)}&select=id`,
      {
        method: 'PATCH',
        headers: {
          'apikey':        SUPABASE_ANON_KEY,
          'Authorization': `Bearer ${SUPABASE_ANON_KEY}`,
          'Content-Type':  'application/json',
          'Accept':        'application/json',
          'Prefer':        'return=representation',
        },
        body: JSON.stringify({ active: !currentlyActive }),
      }
    );
    if (!resp.ok) {
      const detail = await resp.text();
      throw new Error(`HTTP ${resp.status} ${detail.slice(0, 240)}`);
    }
    // Same RLS-silent-drop check as deleteDigestAlert — if Postgres
    // filtered the row out, the PATCH succeeds with 0 rows and the UI
    // would lie about toggling.
    let rows = [];
    try { rows = await resp.json(); } catch (_) {}
    if (Array.isArray(rows) && rows.length === 0) {
      throw new Error('PATCH returned no rows — likely blocked by RLS.');
    }
    showToast(currentlyActive
      ? (t('toast.alert_paused')  || 'Alert paused')
      : (t('toast.alert_resumed') || 'Alert resumed'));
    const wrap = document.getElementById('digestSettingsWrap');
    if (wrap) wrap.innerHTML = await renderDigestSettingsSection();
    const mgrBody = document.getElementById('alertsManagerBody');
    const mgrBack = document.getElementById('alertsManagerBackdrop');
    if (mgrBody && mgrBack && !mgrBack.hidden) {
      mgrBody.innerHTML = await renderDigestSettingsSection();
    }
  } catch (err) {
    console.warn('[digest] toggle failed:', err);
    showToast('Could not update alert: ' + err.message);
  }
}

async function editDigestAlert(alertId) {
  const user = _alertUser();
  if (!user || !alertId) return;
  const rows = await _fetchDigestFilters(user.id);
  const row  = rows.find(r => r.id === alertId);
  if (!row) return;
  // Close the alerts-manager modal first so the edit form isn't rendered
  // behind it. Without this, both modals stack and the save-alert form
  // (with the prefilled values) is hidden under the manager card.
  closeAlertsManager();
  await openSaveAlertModal(row);
}

async function deleteDigestAlert(alertId, displayName) {
  const user = _alertUser();
  if (!user || !alertId) return;
  const confirmMsg = (t('alert_modal.delete_confirm') || 'Delete this alert? You won\'t receive its emails anymore.')
                   + (displayName ? `\n\n"${displayName}"` : '');
  if (!confirm(confirmMsg)) return;
  try {
    // Ask PostgREST to send back the deleted rows so we can detect the
    // "RLS silently dropped your DELETE" failure mode that bit us before
    // migration 006 added the anon DELETE policy. A successful delete
    // returns one row; if Postgres filters via RLS we get [].
    const resp = await _alertsFetch(
      `id=eq.${encodeURIComponent(alertId)}&select=id`,
      {
        method: 'DELETE',
        headers: {
          'apikey':        SUPABASE_ANON_KEY,
          'Authorization': `Bearer ${SUPABASE_ANON_KEY}`,
          'Accept':        'application/json',
          'Prefer':        'return=representation',
        },
      }
    );
    if (!resp.ok) {
      const detail = await resp.text();
      throw new Error(`HTTP ${resp.status} ${detail.slice(0, 240)}`);
    }
    // Verify a row actually came back. Without this check, an RLS-denied
    // DELETE looks identical to a successful one to the user — the
    // alert reappears on next refresh and trust evaporates.
    let rows = [];
    try { rows = await resp.json(); } catch (_) { /* return=minimal style */ }
    if (Array.isArray(rows) && rows.length === 0) {
      throw new Error('DELETE returned no rows — likely blocked by RLS. Run supabase_migration_006.sql.');
    }
    showToast(t('toast.alert_deleted') || 'Alert deleted');
    // Refresh wherever the list is currently visible. renderDigestSettingsSection
    // also updates the avatar badge cache as a side-effect.
    if (typeof _refreshDigestAlertBadge === 'function') _refreshDigestAlertBadge();
    const wrap = document.getElementById('digestSettingsWrap');
    if (wrap) wrap.innerHTML = await renderDigestSettingsSection();
    const mgrBody = document.getElementById('alertsManagerBody');
    const mgrBack = document.getElementById('alertsManagerBackdrop');
    if (mgrBody && mgrBack && !mgrBack.hidden) {
      mgrBody.innerHTML = await renderDigestSettingsSection();
    }
  } catch (err) {
    console.warn('[digest] delete failed:', err);
    showToast((t('toast.alert_delete_failed') || 'Could not delete alert:') + ' ' + err.message);
  }
}

// Build chip labels summarising one saved alert. Order matches the modal's
// field order so reading the chips left-to-right mirrors filling the form.
// Localised city display name for an alert row. Returns the city name in
// the active language (Milan / Milano), or null for alerts that pre-date
// migration 007 and have an empty cities array (treated as "all cities"
// historically — we leave the chip off so the card doesn't lie).
function _cityNameForAlert(f) {
  const codes = Array.isArray(f && f.cities) ? f.cities : [];
  if (!codes.length) return null;
  const cfg = availableCities.find(c => c.code === codes[0]);
  if (cfg) return _cityDisplayName(cfg);
  return codes[0];
}

function _digestChipsForAlert(f) {
  const chips = [];
  const cityName = _cityNameForAlert(f);
  if (cityName) chips.push(cityName);
  if (f.max_rent)             chips.push(`Max €${f.max_rent}/mo`);
  if (f.min_sqm)              chips.push(`≥${f.min_sqm} m²`);
  if (f.min_rooms)            chips.push(`≥${f.min_rooms} rooms`);
  if (f.max_metro_min)        chips.push(`Metro ≤${f.max_metro_min} min`);
  if (f.min_score && f.min_score > 0) chips.push(`Score ≥${f.min_score}`);
  if (f.min_floor != null)    chips.push(`Floor ≥${f.min_floor}`);
  if (f.require_elevator)     chips.push('Elevator');
  if (f.min_energy)           chips.push(`Energy ≥${f.min_energy}`);
  if (f.fascia?.length)       chips.push(`Fascia ${f.fascia.join('/')}`);
  if (f.omi_zona?.length)     chips.push(`Zone ${f.omi_zona.join('/')}`);
  if (f.gems_filter && f.gems_filter !== 'all') {
    chips.push(f.gems_filter === 'hidden' ? 'Hidden Gems only' : 'Great Value+');
  }
  if (f.source_filter && f.source_filter !== 'all') chips.push(`Source ${f.source_filter}`);
  return chips;
}

// Renders the Settings tab's Daily-alerts section. Returns an HTML string
// (not a DOM node) so the caller can innerHTML-assign it. Each alert is
// rendered as a card with Edit / Pause-Resume / Delete buttons.
async function renderDigestSettingsSection() {
  const user = _alertUser();
  if (!user) {
    return `<div class="settings-section-title">${escapeHtml(t('settings.alerts') || 'Daily alerts')}</div>
            <p style="font-size:13px;color:var(--text-secondary);margin:0">
              Sign in to save alert filters and receive a daily email of matching listings.
            </p>`;
  }
  const rows = await _fetchDigestFilters(user.id);
  // Update the avatar badge cache from this same fetch — saves a round-trip
  // and keeps the badge in sync with whatever the user just saved/deleted.
  window._digestTotalCount  = rows.length;
  window._digestActiveCount = rows.filter(r => r.active).length;
  const _dot = document.getElementById('avatar-alert-dot');
  if (_dot) _dot.style.display = (window._digestActiveCount > 0) ? 'block' : 'none';
  const headerHtml = `<div class="settings-section-title" style="display:flex;align-items:center;justify-content:space-between;gap:10px">
      <span>${escapeHtml(t('settings.alerts') || 'Daily alerts')}</span>
      <button onclick="saveDigestFilters()"
              class="settings-alert-new-btn"
              type="button">${escapeHtml(t('settings.new_alert') || '+ New alert')}</button>
    </div>`;

  if (!rows.length) {
    return headerHtml + `
      <p style="font-size:13px;color:var(--text-secondary);margin:0;line-height:1.6">
        ${escapeHtml(t('settings.alert_none') || 'No alert set up yet. Go to Rentals, set your filters, and click Save alert.')}
      </p>`;
  }

  // Local time string for display — pure presentation, the stored value
  // stays in UTC.
  const formatSendTime = (utc) => {
    if (!utc || !/^\d{1,2}:\d{2}$/.test(utc)) return '—';
    const [h, m] = utc.split(':').map(Number);
    const d = new Date();
    d.setUTCHours(h, m, 0, 0);
    return d.toLocaleTimeString(currentLang === 'it' ? 'it-IT' : 'en-GB',
      { hour: '2-digit', minute: '2-digit' });
  };

  const cardsHtml = rows.map(f => {
    const chips = _digestChipsForAlert(f);
    const stateColor = f.active ? '#2A7A5A' : 'var(--text-secondary)';
    const stateLabel = f.active
      ? (t('settings.alert_active') || 'Active')
      : (t('settings.alert_paused') || 'Paused');
    const displayName = (f.name && f.name.trim())
      || (t('settings.alert_unnamed') || 'Unnamed alert');
    const summary = chips.length ? chips.join(' · ')
                                 : (t('settings.alert_all') || 'All listings');
    const sendLocal = formatSendTime(f.send_time_utc);
    const minMatch  = f.min_new_listings || 1;

    // Action buttons read everything from data-* attributes instead of
    // inline JS arguments — embedding the name via JSON.stringify into
    // an inline onclick (e.g. `onclick="...('${id}', ${JSON.stringify
    // (name)})"`) breaks the HTML attribute as soon as the name has a
    // double quote in it, killing the entire <script> tail with
    // "Unexpected end of input". escapeHtml on the data values is
    // enough; the click delegator parses them back safely.
    return `
      <div class="digest-alert-card">
        <div class="digest-alert-row1">
          <div style="flex:1;min-width:0">
            <div class="digest-alert-name" title="${escapeHtml(displayName)}">${escapeHtml(displayName)}</div>
            <div class="digest-alert-meta">
              <span style="color:${stateColor};font-weight:600">● ${escapeHtml(stateLabel)}</span>
              <span>·</span>
              <span>${escapeHtml(t('settings.alert_sends_at') || 'Sends at')} <strong>${escapeHtml(sendLocal)}</strong></span>
              <span>·</span>
              <span>${escapeHtml((t('settings.alert_min_n')  || 'min {n}').replace('{n}', minMatch))}</span>
            </div>
          </div>
        </div>
        <div class="digest-alert-chips">
          ${chips.length
            ? chips.map(c => `<span class="digest-alert-chip">${escapeHtml(c)}</span>`).join('')
            : `<span style="color:var(--text-secondary);font-size:12px">${escapeHtml(summary)}</span>`}
        </div>
        <div class="digest-alert-actions"
             data-alert-id="${escapeHtml(f.id)}"
             data-alert-name="${escapeHtml(displayName)}"
             data-alert-active="${f.active ? '1' : '0'}">
          <button type="button"
                  data-digest-action="edit">${escapeHtml(t('settings.update_filters') || 'Edit')}</button>
          <button type="button"
                  data-digest-action="toggle">${escapeHtml(f.active
                    ? (t('settings.pause_alert')  || '⏸ Pause')
                    : (t('settings.resume_alert') || '▶ Resume'))}</button>
          <button type="button" class="digest-alert-delete"
                  data-digest-action="delete">${escapeHtml(t('settings.delete_alert') || 'Delete')}</button>
        </div>
      </div>`;
  }).join('');

  return headerHtml + cardsHtml;
}

function formatRelativeTime(iso) {
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (!Number.isFinite(diff)) return '';
  const rtf = new Intl.RelativeTimeFormat(currentLang === 'it' ? 'it' : 'en', { numeric: 'auto' });
  if (diff < 60)    return rtf.format(0, 'second');
  if (diff < 3600)  return rtf.format(-Math.floor(diff / 60), 'minute');
  if (diff < 86400) return rtf.format(-Math.floor(diff / 3600), 'hour');
  return rtf.format(-Math.floor(diff / 86400), 'day');
}
