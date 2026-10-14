# Handover — immobiliare-scorer (as of 2026-10-14 ~22:15)

Personal tool: scrapes Milan rentals (Immobiliare.it + Idealista.it), enriches with location data, scores deals, shows them in a local dashboard (`localhost:8000`) and an online one (Supabase-backed). **Scope right now: Milan only, one scan per day.** Other cities are parked.

## Current state (verified)
- `main` = `90da3c9` (PRs #2–#5). Evening work (section "2026-10-14 evening") is on branch `claude/data-coverage-frontend-fixes`, **uncommitted**, and already running locally (API + daemons restarted on it).
- **Two SQL files must be run once in the Supabase SQL editor:** `supabase_migration_013.sql` (closes the alerts table `digest_filters` to the public key — it was readable/writable by anyone) and `supabase_migration_014.sql` (columns for the new fields; until then the online page falls back to `select=*` and the new fields stay local-only).
- API: `nohup python3 -u api.py >> /tmp/lume-api.log` (unbuffered log), serves dashboard + endpoints.
- Daemons (started via `POST /start-scanner`, daily mode): next scans **2026-10-15 ~00:29 (Immobiliare)** and **~00:34 (Idealista)**; if the laptop sleeps, they run on wake.
- Milan rentals: 28,424 rows; **9,944 visible**; **Great Value 337, Hidden Gem 70**. Online (Supabase) = local: identical visible set and labels (checked 22:20).
- Supabase project `sexbjvmedgappzqmcdjv` was paused (DNS gone) until today; user resumed it. Pushes fail **silently** — check `/tmp/lume-api.log` for `[supabase_sync] ... batches succeeded`.

## Operational gotchas (read before touching anything)
1. **Restarting `api.py` kills the scanner daemons** (startup orphan sweep) — **even a second `api.py` that then fails to bind the port** sweeps them. Kill the old API by PID first, wait for port 8000 to free, then start, then re-run `POST /start-scanner` with `{"pages":50,"cities":["milano"],"max_rent":3000,"min_rooms":2}`. In zsh, `kill $VAR` with several PIDs doesn't split — use an array (`pids=($(pgrep …)); kill "${pids[@]}"`). The API's process name is `Python -u api.py` (match `"u api.py"`).
2. **The dashboard auto-starts enrichment for every city with pending work** whenever it's open (only the in-page Stop button pauses it until reload; `DELETE /geo-enrich` via curl doesn't). Other cities show everything pending (no POI-version stamps) → Roma/Napoli runs keep appearing. Harmless, but block restarts.
3. **Guard restarts with a real `if … then exit`**, not `cmd && kill; …` — a chained guard once failed open and killed the API mid-enrichment.
4. **Legacy files `dashboard/rentals_latest.json` / `sales_latest.json` are stale May data.** Never push them. Now enforced: `supabase_sync.push_local_json` refuses them, the API no longer rescores them at start-up, and the autosync watcher watches `milano_rentals_latest.json` instead (it watched the legacy files, so every restart pushed May data — happened twice on 2026-10-14). Side effect: nightly scans now reach Supabase on their own (autosync pushes ~10 s after the file settles; skips versions a rescore/enrichment already pushed).
5. Before rewriting a data file use the scanners' lock (`<file>.lock`, `fcntl.flock`) and atomic tmp+replace (`dashboard_io.write_snapshot`). `/api/rescore` and the enrichment worker already do (`_flush_geo`).
6. Supabase sometimes returns transient HTTP 500 right after a big push — retry with 10 s backoff.
7. Don't run `archive_old_listings.py` until every active listing has been re-seen once (Idealista rentals only got `last_seen_date` from 2026-10-14; rotation needs ~4 days).

## What changed today (by area)
**Ops / reliability**
- Geo-enrich Stop works (stop checks around rescore, no redundant second rescore, stale queue-stop flag).
- `browser_session.py`: Chrome (Edge 122 is blocked by Immobiliare), persistent profiles `~/.lume-scanner/profiles/<name>`, 30 s launch wait, timeouts on every navigation/evaluate, restart on hang, SIGTERM cleanup (`os._exit`). Used by all scrapers, the View-button probe (minimized), the single-listing scrape and the manual tools.
- Daily scan mode (`--scan daily`, default with `--daemon`): discover newest-first → sweep 1/4 of areas (rotates daily) → Immobiliare detail pages for new listings (50/day; energy class, publication date, condo fees). Wall-clock scheduling, last scan stored in `.last_scan_<source>_milano`.
- Stale/trim is **coverage-aware**: only judges listings whose areas were all swept since last seen (`.sweep_coverage_milano.json`); never revives archived rows. Until all 17 Immobiliare areas / 10 Idealista zones are swept (~4 days), nothing is trimmed.
- Parser fixes: ga4features/ga4Heating/ga4Garage (heating, balcony, parking, furnished, exposure were 100 % null), no typology in `condition`, sales `date` import, redirect re-filter (`navigli → navigli-darsena`), Fiera slug `fiera-sempione-city-life-portello`, Idealista zone matcher (17/17 areas), Idealista stamps `last_seen_date` + `is_stale=False`.
- POI query fixed (`out center;` → parks restored, Milan only), city-aware OMI pass, fascia repaired from zone table, `is_stale` pushed as boolean.

**Scoring (rentals only; sales untouched)** — every change is a settings switch
- Comps speedup (zone/fascia buckets, 417 s → ~25 s), suspect listings and duplicates out of comps.
- Benchmark (`comps_engine.RENT_BENCHMARK_DEFAULTS`): short-let platforms out of comps (Spacest/Roomless/HousingAnywhere/Uniplaces/Spotahome), nearest 30 flats ≤1.5 km same fascia (same condition first), size elasticity −0.30, recency half-life 90 d, symmetric trim, no OMI blend. Backtest: median error 12.8 % → 11.9 %, bias 0, source gap 0.
- Score structure (`scoring._DEFAULT_SETTINGS`): smooth price `100·Φ(−1.4·delta/σ)`, σ = 0.134 + 0.56 × comps spread; property: unknown features = run average, condition left to the benchmark.
- Labels (`scoring.rental_labels`, `rent_*` settings, editable in the dashboard settings panel): GV z ≤ −1.1; HG z ≤ −1.3 + property ≥65, location ≥80, metro ≤700 m, ≥22 m²/room, ≥20 comps; never for short-lets, listings first seen >60 days ago, discounts beyond −30 %.
- Display: excluded/fake rows hidden in rentals grid; explanation lead uses real labels; dead LDI "price bonus" removed.

## 2026-10-14 evening — data coverage + frontend
**Listing pages (`listing_details.py`)** — Idealista cards carry no condition/heating/energy/bathrooms; condition alone moves rent ±6–16 % vs neighbours. Both scanner daemons now drip listing pages between scans (`wait_and_drip`): ≤150/day/source, 08–23 h, 3 pages per browser session every 12–24 min, possible deals first (price_z ≤ −0.8, ~1,500), then listings first seen ≤3 days, then the rest. Results in `detail_cache_milano.json` (gitignored), applied by `scoring.score_all` on every scoring path (`apply_details`); never written into the listings file. A page saying "no longer available/published" marks the listing gone → hidden (`is_stale`) unless a scan sees it again afterwards — the first batches showed most top "deals" were already gone. Blocked → 6 h pause. Scanner's own 50/day detail step set to 0 (drip replaces it). Idealista "buono stato" covers renovated flats too → mapped to renovated only when the description says so, else condition group stays unknown.
**Scoring** (measured on today's data, all settings switches in `scoring._DEFAULT_SETTINGS`): same-flat twins fill each other's gaps (`fill_from_twins`, ~900 rows); Idealista yes-only card flags ignored until a page/twin answers (`property_ignore_card_flags`) — they inflated property scores *and* the imputed averages for everyone; rent compared on asking rent both sides (`rent_condo_basis: "asking"`); discounts beyond −30 % get a neutral price score + `price_suspect` flag (`rent_price_suspect_neutral`; 20 → 0 of the top 100); Hidden Gem property bar 65 → 62 (= the old 40 % pass rate before the inflation fix); dead LDI boost removed; `score_cap_reason`, `comps_ids` (non-stale rows) stored. Labels went GV 347 → 361, HG 64 → 44 → 70 with the recalibrated bar.
**Supabase**: `supabase_sync.schema_columns()` reads the live table columns (10-min cache) so new fields never fail a push. Migrations 013/014 above.
**API**: `/rentals?visible=1` (live rows only, gzip, ETag — 87 MB → ~5 MB), `/rentals/by-ids`, `/api/user-data` (favourites/collections mirrored to `user_data.json`), `/api/digest-filters` (alerts proxy with the service key), `scanning` in `/scanner-status` (`.scan_running_<source>` markers), `/full.html`, `/assets/*`, auto-enrichment limited to Milan rentals (`ACTIVE_ENRICH_CITIES`). Dead Netlify deploys removed.
**Frontend**: `dashboard/full.html` = the old full dashboard (sales/investor/valuation/cities) kept as is. `index.html` (1.4k lines) + `assets/`: `lume.css`, `core.js` (auth, Supabase client, storage), `i18n.js`, `shell.js`, `widgets.js`, `alerts.js`, `collections.js`, `cards.js`, `rentals.js`, `scanner.js`, `controls.js`, `map.js`, `insights.js`, `settings.js`, `init.js` — plain files, no build. **Rule: feature files hold declarations only; anything that runs at load (event wiring, start-up, values built by calling app functions) goes in `init.js`, which loads last** — classic scripts don't hoist functions across files, so a load-time call into a later file would throw. The split was checked with acorn (every load-time reference resolves; all 372 statements byte-identical) and in the browser (both modes, every tab). `build.sh` substitutes placeholders in assets/*.js too; **Cloudflare builds from git, so assets/ must be committed with index.html**. Fixes: online loading parallel + first page painted at ~2 s (was ~18 s), slim column list with `select=*` fallback, id tie-breaker, partial-data note; archived rows dropped before duplicate merging; counts over displayable rows; real weights (0.45/0.25) + cap reason in breakdown; sorts Score / Discount / Newest / Rent; card: discount shown once, "first seen" pill, "verify" message on suspect discounts, discount strength (price_z), comparable-flats list, condition/contract/availability/AC rows, twin note; dead settings knobs and CI pill removed; saving without sign-in; alerts local-only (identity from the existing alert row); scraped text escaped (cards, map popups, data-quality table, favourites page); SRI on CDN scripts; local dates; `lang` attribute; focus outline. Favourites page fetches only its rows.

## Open items / next steps
- **Validate scoring against outcomes** once ~3–6 weeks of daily scans exist: listing lifetime (first→last seen) vs price_z / score. Pre-October scans were too sporadic. Location already predicts faster renting (ρ ≈ −0.19).
- Label membership is sensitive to the comps pool (removing archived comps: Jaccard ≈ 0.4). Revisit after the pool refreshes.
- Residual size bias (large flats still read ~−10 % vs comps); 1st floor scored as ground floor (deliberate in code — revisit); `furnished` data one-sided; condo fees known for ~2 %.
- Geocoding (Idealista has no coordinates anywhere): address-keyed geocode cache + failure memo, fix `_parse_street` (keeps neighbourhood), Milan-hardcoded checks in `enrich_geo.py`, optionally self-hosted Photon.
- Scanner watchdog (heartbeat → restart) not built; timeouts cover hangs for now.
- Show failed Supabase pushes in the dashboard enrichment panel.
- PR branch `claude/scanner-reliability-enrichment-speed`, `claude/scoring-*`, `claude/supabase-visibility-fixes` still exist (merged; safe to delete).
- After ~10 days of drip: re-measure labels with real Idealista condition/condo data; reconsider `rent_condo_basis` (imputed fees for unknowns once enough are known) and whether Idealista "buono stato" needs the description heuristic.
- The local API now serves `/manifest.json` and `/icon-*.png` (were 404s) — live after the next API restart.
- Clerk uses a development (`pk_test`) key; no CSP. The unused Netlify token in `netlify_config.json` (gitignored, 401 now) can be revoked.

## Incidents today (all repaired)
1. First daily scan trimmed ~12k rows incl. live ones → restored from backup; trim made coverage-aware.
2. Restore + no-date branch revived ~18k archived rows in the grid → flags restored; stale pass now keeps flags it can't judge.
3. API force-killed during an auto Roma enrichment (guard failed open) → no data damage.
4. Old `/api/rescore` pushed 6/28 batches of stale legacy rows to Supabase → Milan re-pushed, 13 legacy-only rows flagged stale; parity verified.
5. (22:00) A restart's start-up rescore of the legacy file + the autosync watcher pushed it again (5,576 rows) → root cause removed (see gotcha 4), Milan re-pushed, 17 legacy-only rows flagged stale; parity verified.
6. (21:59) A second `api.py` started while the first still held the port: its orphan sweep killed both daemons before it failed to bind → clean restart; no data impact.

Backups of pre-change data files were taken to the session scratchpad (`…/scratchpad/verify/backup/`), which is session-specific and may be gone.
