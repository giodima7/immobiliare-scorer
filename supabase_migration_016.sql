-- 016: what photo_eval.py saw in a listing's photos (scoring stamps these
-- photo_* fields; a confident "contradicts" also caps the score at 69).
ALTER TABLE listings
  ADD COLUMN IF NOT EXISTS photo_deal        TEXT,
  ADD COLUMN IF NOT EXISTS photo_confidence  TEXT,
  ADD COLUMN IF NOT EXISTS photo_condition   SMALLINT,
  ADD COLUMN IF NOT EXISTS photo_kitchen     SMALLINT,
  ADD COLUMN IF NOT EXISTS photo_bathroom    SMALLINT,
  ADD COLUMN IF NOT EXISTS photo_light       SMALLINT,
  ADD COLUMN IF NOT EXISTS photo_flags       JSONB,
  ADD COLUMN IF NOT EXISTS photo_note_it     TEXT,
  ADD COLUMN IF NOT EXISTS photo_note_en     TEXT,
  ADD COLUMN IF NOT EXISTS photo_read_at     TIMESTAMPTZ;

NOTIFY pgrst, 'reload schema';
