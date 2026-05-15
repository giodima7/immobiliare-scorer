-- Migration 003: score_reasons TEXT[] → JSONB
--
-- Each row in score_reasons is a {component, sentiment, text} dict, but
-- migration 002 declared the column as TEXT[]. PostgREST silently
-- stringifies the dicts on upsert, so every entry comes back to the
-- dashboard as a JSON-encoded string. The dashboard reads `r.sentiment`
-- → undefined → defaults to 'negative' → every reason renders with the
-- ⚠ warning icon, even on green "Great Value" listings.
--
-- Fix: store as JSONB. Keep the existing data by parsing each TEXT entry
-- back into a JSON object and rebuilding the array.
--
-- Postgres rejects subqueries inside ALTER COLUMN ... USING, so we run
-- the per-element parse through a session-local plpgsql helper.
--
-- Idempotent — guarded by the column-type check, so re-running is safe.

CREATE OR REPLACE FUNCTION pg_temp._text_array_to_jsonb_array(arr text[])
RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE
AS $$
DECLARE
  result jsonb := '[]'::jsonb;
  elem   text;
BEGIN
  IF arr IS NULL THEN RETURN NULL; END IF;
  FOREACH elem IN ARRAY arr LOOP
    IF elem IS NOT NULL AND length(trim(elem)) > 0 THEN
      result := result || elem::jsonb;
    END IF;
  END LOOP;
  RETURN result;
END $$;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'listings'
      AND column_name = 'score_reasons'
      AND data_type = 'ARRAY'
  ) THEN
    ALTER TABLE listings
      ALTER COLUMN score_reasons TYPE jsonb
      USING pg_temp._text_array_to_jsonb_array(score_reasons);
  END IF;
END $$;
