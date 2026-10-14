# Handover — immobiliare-scorer (as of 2026-10-14 ~08:40)

Personal tool: scrapes Milan rentals (Immobiliare.it + Idealista.it), enriches with location data, scores deals, shows them in a local dashboard (`localhost:8000`) and an online one (Supabase-backed). **Scope right now: Milan only, one scan per day.** Other cities are parked.

## Current state (verified)
- `main` = `90da3c9`. Everything below is merged (PRs #2–#5) and live.
- API: `nohup python3 -u api.py >> /tmp/lume-api.log` (unbuffered log), serves dashboard + endpoints.
- Daemons (started via `POST /start-scanner`, daily mode): next scans **2026-10-15 ~00:29 (Immobiliare)** and **~00:34 (Idealista)**; if the laptop sleeps, they run on wake.
- Milan rentals: 28,424 rows; **9,958 visible**; **Good Value 349, Hidden Gem 63** (both ~50/50 Immobiliare/Idealista, spread over fascia B/C/D). Online (Supabase) = local: identical visible set and labels.
- Supabase project `sexbjvmedgappzqmcdjv` was paused (DNS gone) until today; user resumed it. Pushes fail **silently** — check `/tmp/lume-api.log` for `[supabase_sync] ... batches succeeded`.

## Operational gotchas (read before touching anything)
1. **Restarting `api.py` kills the scanner daemons** (startup orphan sweep). Always re-run `POST /start-scanner` with `{"pages":50,"cities":["milano"],"max_rent":3000,"min_rooms":2}` afterwards.
2. **The dashboard auto-starts enrichment for every city with pending work** whenever it's open (only the in-page Stop button pauses it until reload; `DELETE /geo-enrich` via curl doesn't). Other cities show everything pending (no POI-version stamps) → Roma/Napoli runs keep appearing. Harmless, but block restarts.
3. **Guard restarts with a real `if … then exit`**, not `cmd && kill; …` — a chained guard once failed open and killed the API mid-enrichment.
4. **Legacy files `dashboard/rentals_latest.json` / `sales_latest.json` are stale May data.** Never push them to Supabase. The *old* `/api/rescore` targeted them (fixed in #5: now rescores the city file). Startup still rescores them locally (harmless).
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

## Open items / next steps
- **Validate scoring against outcomes** once ~3–6 weeks of daily scans exist: listing lifetime (first→last seen) vs price_z / score. Pre-October scans were too sporadic. Location already predicts faster renting (ρ ≈ −0.19).
- Label membership is sensitive to the comps pool (removing archived comps: Jaccard ≈ 0.4). Revisit after the pool refreshes.
- Residual size bias (large flats still read ~−10 % vs comps); 1st floor scored as ground floor (deliberate in code — revisit); `furnished` data one-sided; condo fees known for ~2 %.
- Geocoding (Idealista has no coordinates anywhere): address-keyed geocode cache + failure memo, fix `_parse_street` (keeps neighbourhood), Milan-hardcoded checks in `enrich_geo.py`, optionally self-hosted Photon.
- Scanner watchdog (heartbeat → restart) not built; timeouts cover hangs for now.
- Show failed Supabase pushes in the dashboard enrichment panel.
- PR branch `claude/scanner-reliability-enrichment-speed`, `claude/scoring-*`, `claude/supabase-visibility-fixes` still exist (merged; safe to delete).

## Incidents today (all repaired)
1. First daily scan trimmed ~12k rows incl. live ones → restored from backup; trim made coverage-aware.
2. Restore + no-date branch revived ~18k archived rows in the grid → flags restored; stale pass now keeps flags it can't judge.
3. API force-killed during an auto Roma enrichment (guard failed open) → no data damage.
4. Old `/api/rescore` pushed 6/28 batches of stale legacy rows to Supabase → Milan re-pushed, 13 legacy-only rows flagged stale; parity verified.

Backups of pre-change data files were taken to the session scratchpad (`…/scratchpad/verify/backup/`), which is session-specific and may be gone.
