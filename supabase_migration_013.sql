-- Migration 013: close digest_filters to the public (anon) key
--
-- Migrations 004 and 006 gave the anon role SELECT/INSERT/UPDATE/DELETE on
-- digest_filters with USING(true). The anon key ships in the dashboard (and
-- the repo is public), so anyone could read every alert's email address,
-- change or delete alerts, or insert alerts that the digest job would then
-- email. Rows were only "scoped" by a clerk_user_id query parameter the
-- client chose itself.
--
-- After this migration only the service role can touch the table. The local
-- dashboard manages alerts through api.py (/api/digest-filters), which uses
-- the service key from .env; email_digest_supabase.py already uses it.
--
-- Run once in the Supabase SQL editor. Idempotent.

DROP POLICY IF EXISTS "Anon can read filters"         ON digest_filters;
DROP POLICY IF EXISTS "Anon can upsert own filters"   ON digest_filters;
DROP POLICY IF EXISTS "Anon can update own filters"   ON digest_filters;
DROP POLICY IF EXISTS "Anon can delete own filters"   ON digest_filters;

ALTER TABLE digest_filters ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON digest_filters FROM anon;
