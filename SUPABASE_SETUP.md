# Supabase migration — manual setup checklist

The code in this commit is ready, but Supabase has to be provisioned by hand
once. Steps below are the sequence you have to follow in the browser. After
they're done, the daily GitHub Actions scan will keep Supabase up to date and
the deployed Cloudflare Pages dashboard will read from it on every visit.

Supabase is the **sole** data source for the deployed dashboard — there is no
JSON-snapshot fallback. If Supabase is unreachable the grid renders empty and
the header shows a "Couldn't reach Supabase" notice.

---

## 1. Create the Supabase project

1. Go to https://supabase.com → **New project**
2. Region: **West EU (eu-west-2 / London)** for best Milan latency
3. Name: anything (e.g. `lume-milano`)
4. Database password: generate, save in 1Password / Bitwarden
5. Wait ~2 minutes for provisioning to finish

## 2. Note the keys (Settings → API)

Three values you'll need:

| Where it goes | What it is |
|---|---|
| `SUPABASE_URL` | Project URL, e.g. `https://abcdefgh.supabase.co` |
| `SUPABASE_ANON_KEY` | anon public key — safe to embed in HTML, read-only via RLS |
| `SUPABASE_SERVICE_KEY` | service_role key — write access, **never** commit or expose |

## 3. Create the schema (SQL editor)

Run this **once** in the Supabase SQL editor:

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS listings (
  id               TEXT PRIMARY KEY,
  source           TEXT NOT NULL,
  listing_type     TEXT NOT NULL CHECK (listing_type IN ('rental', 'sale')),

  title            TEXT,
  address          TEXT,
  neighbourhood    TEXT,
  url              TEXT,
  thumbnail        TEXT,

  price            INTEGER,
  ask_psqm         NUMERIC(10,2),
  ask_psqm_rent    NUMERIC(10,2),

  sqm              INTEGER,
  rooms            NUMERIC(4,1),
  floor_n          INTEGER,
  floor_label      TEXT,
  elevator         BOOLEAN,
  has_balcony      BOOLEAN,
  has_parking      BOOLEAN,
  furnished        BOOLEAN,
  condition        TEXT,
  year_built       INTEGER,
  energy_class     TEXT,
  bathrooms        INTEGER,
  condominium_fees INTEGER,
  heating_type     TEXT,
  is_external      BOOLEAN,
  is_below_ground  BOOLEAN,
  is_ground_floor  BOOLEAN,

  latitude         NUMERIC(10,7),
  longitude        NUMERIC(10,7),
  omi_zona         TEXT,
  omi_fascia       TEXT,
  omi_descr        TEXT,
  metro_walk_min   INTEGER,
  metro_nearest_name TEXT,
  metro_nearest_line TEXT,
  metro_nearest_dist_m INTEGER,
  park_nearest_dist_m  INTEGER,
  supermarket_nearest_dist_m INTEGER,
  university_nearest_dist_m  INTEGER,
  tram_nearest_dist_m        INTEGER,

  omi_compr_mid    NUMERIC(10,2),
  omi_compr_min    NUMERIC(10,2),
  omi_compr_max    NUMERIC(10,2),
  omi_loc_mid      NUMERIC(10,2),
  omi_loc_min      NUMERIC(10,2),
  omi_loc_max      NUMERIC(10,2),
  omi_source       TEXT,
  omi_fallback     BOOLEAN,

  score_total      INTEGER,
  score_price      INTEGER,
  score_property   INTEGER,
  score_location   INTEGER,
  score_penalty    INTEGER,
  ldi_score        NUMERIC(6,2),
  comps_delta_pct  NUMERIC(8,2),
  comps_n          INTEGER,
  comps_median     NUMERIC(10,2),
  comps_confidence INTEGER,
  comps_source     TEXT,
  comps_ids        TEXT[],
  hidden_gem       BOOLEAN DEFAULT false,
  good_value       BOOLEAN DEFAULT false,
  vs_omi_pct       NUMERIC(8,2),
  boosted_price_score INTEGER,
  is_corporate_rental BOOLEAN,
  suggested_rent_mo   INTEGER,
  suggested_rent_psqm NUMERIC(8,2),

  room_efficiency_flag TEXT,
  absolute_value_gate_applied BOOLEAN,

  estimated_rent_mo    INTEGER,
  estimated_rent_psqm  NUMERIC(8,2),
  estimated_yield_pct  NUMERIC(6,2),

  first_seen_date DATE,
  last_seen_date  DATE,
  is_stale        BOOLEAN DEFAULT false,
  days_since_seen INTEGER,
  days_on_market  INTEGER,

  scan_date       TIMESTAMPTZ DEFAULT NOW(),
  updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_listings_type_score  ON listings(listing_type, score_total DESC) WHERE is_stale = false;
CREATE INDEX idx_listings_zona        ON listings(omi_zona);
CREATE INDEX idx_listings_fascia      ON listings(omi_fascia);
CREATE INDEX idx_listings_price       ON listings(price)        WHERE is_stale = false;
CREATE INDEX idx_listings_gem         ON listings(hidden_gem)   WHERE hidden_gem = true AND is_stale = false;
CREATE INDEX idx_listings_coords      ON listings(latitude, longitude) WHERE latitude IS NOT NULL;
CREATE INDEX idx_listings_updated     ON listings(updated_at DESC);
CREATE INDEX idx_listings_address_trgm ON listings USING gin(address gin_trgm_ops);
CREATE INDEX idx_listings_nbhd_trgm    ON listings USING gin(neighbourhood gin_trgm_ops);

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$ BEGIN NEW.updated_at = NOW(); RETURN NEW; END; $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_listings_updated_at
  BEFORE UPDATE ON listings
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

ALTER TABLE listings ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Public read non-stale"
  ON listings FOR SELECT TO anon
  USING (is_stale = false);

CREATE POLICY "Service role full access"
  ON listings FOR ALL TO service_role
  USING (true) WITH CHECK (true);
```

## 4. GitHub Actions secrets

Repository → **Settings → Secrets and variables → Actions** → add:

| Name | Value |
|---|---|
| `SUPABASE_URL` | from step 2 |
| `SUPABASE_SERVICE_KEY` | from step 2 (service_role) |

The `Sync listings to Supabase` step in `daily_scan.yml` reads these. If
either is missing, `supabase_sync.py` exits 0 immediately and the rest of
the workflow continues.

## 5. Cloudflare Pages env vars + build command

Pages project → **Settings → Environment variables** (Production):

| Name | Value |
|---|---|
| `SUPABASE_URL` | from step 2 |
| `SUPABASE_ANON_KEY` | from step 2 (anon, NOT service) |

Pages project → **Settings → Builds & deployments**:

| Setting | Value |
|---|---|
| Build command | `npm run build` |
| Build output directory | `dashboard` |

The root `package.json` exists solely so Cloudflare's build detector picks
the Node toolchain and runs `npm run build` (which forwards to `bash build.sh`)
instead of trying `pip install -r requirements.txt`.

There are two `build.sh` files — both committed:

- `build.sh` at the repo root is a one-line wrapper that forwards to
  `dashboard/build.sh`. It exists because Cloudflare's build command
  always runs from the repo root regardless of the Root directory
  setting.
- `dashboard/build.sh` is the real script. It substitutes
  `%%SUPABASE_URL%%` / `%%SUPABASE_ANON_KEY%%` in `dashboard/index.html`
  at build time. If the env vars aren't set the script exits 0 with a
  warning — the deploy succeeds but the dashboard cannot load any
  listings until the credentials are configured.

## 6. First-run sync (optional)

If you want to populate Supabase right away without waiting for tomorrow's
scan, run locally:

```bash
SUPABASE_URL='https://xxxx.supabase.co' \
SUPABASE_SERVICE_KEY='eyJ...' \
python3 supabase_sync.py
```

Should print `~57 batches succeeded` for rentals + `~18 batches` for sales.

## 7. Verify the deployed dashboard

After Cloudflare's next build:

1. Open https://immobiliare-scorer.pages.dev/
2. Open DevTools → Network — the first XHRs should hit
   `xxxx.supabase.co/rest/v1/listings` (one per page until the cursor exhausts).
3. The grid populates within a couple of seconds. If it stays empty and the
   header shows "Couldn't reach Supabase", check the env vars and the
   `Sync listings to Supabase` step of the most recent Actions run.

## 8. Failure-mode test

Temporarily set `SUPABASE_ANON_KEY` to garbage in Cloudflare Pages env vars,
re-deploy, reload the site. Console should log `[supabase] HTTP 401 for
rentals`, the grid renders empty, and the header shows the "Couldn't reach
Supabase" notice.

Restore the real key and re-deploy when done.
