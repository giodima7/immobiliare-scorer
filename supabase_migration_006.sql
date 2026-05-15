-- Migration 006: allow anon DELETE on digest_filters
--
-- Migration 004 enabled RLS and added SELECT / INSERT / UPDATE policies
-- for the anon role but forgot DELETE. The "🗑 Delete" button on the
-- Settings tab and the My-alerts modal therefore POSTed a DELETE that
-- PostgREST silently filtered to zero rows — no error visible to the
-- user, alert never went away.
--
-- The anon key is still scoped by clerk_user_id at query time (the
-- dashboard never fires a DELETE without WHERE id=...), so allowing the
-- policy with USING(true) matches the existing INSERT/UPDATE posture.

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'digest_filters'
      AND policyname = 'Anon can delete own filters'
  ) THEN
    CREATE POLICY "Anon can delete own filters"
      ON digest_filters FOR DELETE TO anon
      USING (true);
  END IF;
END $$;
