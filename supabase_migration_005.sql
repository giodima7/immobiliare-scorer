-- Migration 005: multiple alerts per user
--
-- The initial digest_filters table (migration 004) had a UNIQUE
-- constraint on clerk_user_id, capping each user at one saved alert.
-- Users want to keep several distinct alerts in parallel ("Centro under
-- €1500", "Bicocca 2-bed with elevator", etc.) and toggle / delete them
-- independently. This migration:
--
--   1. Drops the unique constraint on clerk_user_id.
--   2. Adds a `name` TEXT column for the user-facing label.
--   3. Replaces the partial active-only index with a composite
--      (clerk_user_id, active) index for the "show me my alerts" query.
--
-- Idempotent: every statement guards with IF [NOT] EXISTS or a catalog
-- lookup, so re-running is safe.

-- 1. Drop the UNIQUE constraint. Postgres auto-named it
--    digest_filters_clerk_user_id_key — confirm with a catalog lookup
--    so the migration doesn't fail if Supabase renamed it.
DO $$
DECLARE
  cname TEXT;
BEGIN
  SELECT con.conname INTO cname
  FROM pg_constraint con
  JOIN pg_class       rel ON rel.oid = con.conrelid
  WHERE rel.relname = 'digest_filters'
    AND con.contype = 'u'
    AND ARRAY['clerk_user_id'::name] = (
      SELECT array_agg(att.attname ORDER BY u.ord)
      FROM unnest(con.conkey) WITH ORDINALITY AS u(attnum, ord)
      JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = u.attnum
    );
  IF cname IS NOT NULL THEN
    EXECUTE format('ALTER TABLE digest_filters DROP CONSTRAINT %I', cname);
  END IF;
END $$;

-- 2. Add the human-readable name column.
ALTER TABLE digest_filters
  ADD COLUMN IF NOT EXISTS name TEXT;

-- 3. Replace the single-column active index with a composite that
--    serves the dashboard's per-user listing query AND the digest's
--    "all active rows" scan.
DROP INDEX IF EXISTS idx_digest_filters_active;
CREATE INDEX IF NOT EXISTS idx_digest_filters_user_active
  ON digest_filters(clerk_user_id, active);
CREATE INDEX IF NOT EXISTS idx_digest_filters_active_only
  ON digest_filters(active) WHERE active = true;
