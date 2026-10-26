-- 021: when a listing was first seen, to the minute (2026-10-26). first_seen_date
-- alone can't tell last night's scan from the night before; For you's
-- "New tonight" needs the time.
ALTER TABLE listings ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMP;
