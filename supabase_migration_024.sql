-- Migration 024 — what the description says (2026-10-31)
--
-- The Rentals and For sale filters "Parquet" and "Hide case di ringhiera"
-- read text_flags (['parquet', 'ringhiera']), stamped by scoring from the
-- listing's description — which a grid load leaves out. supabase_sync reads
-- the live columns, so the next push fills it.
-- Run in the Supabase SQL editor. Idempotent.

ALTER TABLE listings
  ADD COLUMN IF NOT EXISTS text_flags TEXT[];

NOTIFY pgrst, 'reload schema';
