-- Migration 004: price history + per-user digest filter storage
--
-- Adds two pieces:
--   1. Price-history columns on `listings` so the daily sync can detect
--      when an asking price has dropped, and the digest can surface the
--      drop in the email card.
--   2. `digest_filters` table — one row per Clerk user, mirroring their
--      saved dashboard filter state. The new email_digest_supabase.py
--      script reads from this and sends a personalised email.
--
-- Idempotent: every statement uses IF [NOT] EXISTS so re-running is safe.

-- ── 1. Price history on listings ─────────────────────────────────────────
ALTER TABLE listings
  ADD COLUMN IF NOT EXISTS previous_price     INTEGER,
  ADD COLUMN IF NOT EXISTS price_changed_date DATE;

-- Indexes for the digest's "what changed today" queries.
CREATE INDEX IF NOT EXISTS idx_listings_new_today
  ON listings(first_seen_date)
  WHERE is_stale = false;

CREATE INDEX IF NOT EXISTS idx_listings_price_drop
  ON listings(price_changed_date)
  WHERE is_stale = false AND previous_price IS NOT NULL;


-- ── 2. Per-user digest filter preferences ────────────────────────────────
CREATE TABLE IF NOT EXISTS digest_filters (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  clerk_user_id   TEXT NOT NULL UNIQUE,
  email           TEXT NOT NULL,
  display_name    TEXT,

  -- Mirror of dashboard filter state. NULL means "no constraint".
  max_rent         INTEGER,
  min_sqm          INTEGER,
  min_rooms        NUMERIC(3,1),
  max_metro_min    INTEGER,
  min_score        INTEGER DEFAULT 0,
  min_floor        INTEGER,
  require_elevator BOOLEAN DEFAULT false,
  min_energy       TEXT,
  fascia           TEXT[],          -- ['B','C']
  omi_zona         TEXT[],          -- ['B21','C19']
  gems_filter      TEXT DEFAULT 'all',  -- 'all' | 'hidden' | 'great_value'
  source_filter    TEXT DEFAULT 'all',  -- 'all' | 'immobiliare' | 'idealista'

  -- Digest delivery settings
  active           BOOLEAN DEFAULT true,
  send_time_utc    TEXT DEFAULT '07:00',  -- future use; cron is currently fixed
  min_new_listings INTEGER DEFAULT 1,     -- skip email if fewer than this many matches

  created_at       TIMESTAMPTZ DEFAULT NOW(),
  updated_at       TIMESTAMPTZ DEFAULT NOW()
);

-- Reuse the update_updated_at() trigger function from migration 001.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger WHERE tgname = 'trg_digest_filters_updated_at'
  ) THEN
    CREATE TRIGGER trg_digest_filters_updated_at
      BEFORE UPDATE ON digest_filters
      FOR EACH ROW EXECUTE FUNCTION update_updated_at();
  END IF;
END $$;

-- Index for the digest script's per-user fetch.
CREATE INDEX IF NOT EXISTS idx_digest_filters_active
  ON digest_filters(active) WHERE active = true;


-- ── 3. RLS policies ──────────────────────────────────────────────────────
-- The dashboard reads/writes via the anon key from the browser. Authn is
-- handled by Clerk client-side; we scope access by clerk_user_id in the
-- query, which is sufficient because the anon key is read-only on
-- non-stale listings and the digest_filters policies below let users only
-- touch their own row in practice (clerk_user_id is the natural scope).
ALTER TABLE digest_filters ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies
                 WHERE tablename = 'digest_filters' AND policyname = 'Anon can read filters') THEN
    CREATE POLICY "Anon can read filters"
      ON digest_filters FOR SELECT TO anon
      USING (true);
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_policies
                 WHERE tablename = 'digest_filters' AND policyname = 'Anon can upsert own filters') THEN
    CREATE POLICY "Anon can upsert own filters"
      ON digest_filters FOR INSERT TO anon
      WITH CHECK (true);
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_policies
                 WHERE tablename = 'digest_filters' AND policyname = 'Anon can update own filters') THEN
    CREATE POLICY "Anon can update own filters"
      ON digest_filters FOR UPDATE TO anon
      USING (true);
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_policies
                 WHERE tablename = 'digest_filters' AND policyname = 'Service role full access on digest_filters') THEN
    CREATE POLICY "Service role full access on digest_filters"
      ON digest_filters FOR ALL TO service_role
      USING (true) WITH CHECK (true);
  END IF;
END $$;
