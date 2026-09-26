-- FROZEN copy of golf/schema.sql as first shipped (2026-09-25), before the shots/roundHandicap upgrade.
-- tests/test_foundation.py builds a database from this and checks that golf.db.connect() upgrades it in place.
-- golf.db schema. This file is the contract between modules; change it deliberately.
-- Conventions: dates are ISO strings (played_on_local = 'YYYY-MM-DD' in the player's timezone),
-- JSON columns hold JSON text, "-1" is never stored (use NULL for unknown).
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta(
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- Every file brought in (export snapshot, screenshot batch, note sync, notebook page).
CREATE TABLE IF NOT EXISTS imports(
  import_id    INTEGER PRIMARY KEY,
  kind         TEXT NOT NULL CHECK(kind IN ('18b_export','screenshot','note_sync','notebook','manual')),
  path         TEXT,
  sha256       TEXT,
  imported_at  TEXT NOT NULL,
  summary      TEXT,            -- JSON: counts inserted/updated/unchanged/flagged
  unknown_keys TEXT             -- JSON list of unexpected keys seen in the source
);
CREATE UNIQUE INDEX IF NOT EXISTS imports_kind_sha ON imports(kind, sha256) WHERE sha256 IS NOT NULL;

-- ---------------------------------------------------------------- courses
CREATE TABLE IF NOT EXISTS clubs(
  club_id TEXT PRIMARY KEY,     -- 18Birdies facility UUID (rounds[].clubId.id)
  name    TEXT,
  city    TEXT,
  state   TEXT
);

CREATE TABLE IF NOT EXISTS courses(
  course_key TEXT PRIMARY KEY,  -- slug, e.g. 'stow-acres-north'
  club_id    TEXT REFERENCES clubs(club_id),
  name       TEXT NOT NULL,
  holes      INTEGER,
  par        INTEGER,
  source     TEXT,              -- 'courses.yaml' | 'opengolfapi' | 'screenshot'
  source_ref TEXT               -- e.g. OpenGolfAPI course id
);

CREATE TABLE IF NOT EXISTS tees(
  tee_id        TEXT PRIMARY KEY,   -- '<course_key>:<tee name>:<gender>'
  course_key    TEXT NOT NULL REFERENCES courses(course_key),
  name          TEXT NOT NULL,
  gender        TEXT CHECK(gender IN ('M','F')),
  par           INTEGER,
  yards         INTEGER,
  cr18          REAL, slope18  INTEGER,
  cr_f9         REAL, slope_f9 INTEGER,
  cr_b9         REAL, slope_b9 INTEGER,
  rating_source TEXT,
  checked_on    TEXT,               -- date Shane confirmed the rating against a scorecard/USGA
  is_default    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tee_holes(
  tee_id TEXT NOT NULL REFERENCES tees(tee_id),
  hole   INTEGER NOT NULL,
  par    INTEGER,
  si     INTEGER,                   -- stroke index / handicap hole
  yards  INTEGER,
  PRIMARY KEY(tee_id, hole)
);

-- ----------------------------------------------------------------- rounds
CREATE TABLE IF NOT EXISTS rounds(
  round_id        TEXT PRIMARY KEY,  -- 18Birdies round id; 'ss-<hash>' screenshot-only; 'man-<n>' manual
  source          TEXT NOT NULL CHECK(source IN ('18b_export','screenshot','manual')),
  played_at_utc   TEXT,
  played_on_local TEXT NOT NULL,
  club_id         TEXT,
  course_key      TEXT,
  tee_id          TEXT,
  entry_mode      TEXT NOT NULL CHECK(entry_mode IN ('hole_by_hole','total_only','partial','abandoned')),
  holes_played    INTEGER,
  nine            TEXT CHECK(nine IN ('front','back','unknown')),   -- NULL for 18-hole rounds
  gross           INTEGER,
  to_par          INTEGER,
  par_played      INTEGER,           -- gross - to_par
  fw_hit INTEGER, fw_left INTEGER, fw_right INTEGER, fw_short INTEGER, fw_long INTEGER, fw_chances INTEGER,
  gir INTEGER, gir_left INTEGER, gir_right INTEGER, gir_short INTEGER, gir_long INTEGER, gir_chances INTEGER,
  putts           INTEGER,
  putts_tracked   INTEGER NOT NULL DEFAULT 0,
  eagles_plus INTEGER, birdies INTEGER, pars INTEGER, bogeys INTEGER, dbl_plus INTEGER,
  dq_flags        TEXT NOT NULL DEFAULT '[]',   -- JSON list, e.g. ["sum_mismatch"]
  raw             TEXT,                         -- JSON of the source record (PII-free subset)
  first_import_id INTEGER REFERENCES imports(import_id),
  last_import_id  INTEGER REFERENCES imports(import_id),
  deleted_in_source INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS rounds_date ON rounds(played_on_local);

-- Human-owned; importers never write here.
CREATE TABLE IF NOT EXISTS round_overrides(
  round_id   TEXT PRIMARY KEY,
  course_key TEXT,
  tee_id     TEXT,
  exclude    INTEGER NOT NULL DEFAULT 0,
  reason     TEXT
);

CREATE TABLE IF NOT EXISTS round_holes(
  round_id    TEXT NOT NULL REFERENCES rounds(round_id),
  hole        INTEGER NOT NULL,
  strokes     INTEGER,
  par         INTEGER,
  si          INTEGER,
  putts       INTEGER,
  fairway     TEXT CHECK(fairway IN ('hit','left','right','short','long','miss','not_applicable')),
  gir         INTEGER,          -- 1/0/NULL
  gir_miss    TEXT CHECK(gir_miss IN ('left','right','short','long','no_chance')),
  penalties   INTEGER,
  chips       INTEGER,
  sand        INTEGER,
  strokes_src TEXT CHECK(strokes_src IN ('18b_export','screenshot','manual')),
  stats_src   TEXT CHECK(stats_src IN ('18b_export','screenshot','manual')),
  PRIMARY KEY(round_id, hole)
);

-- WHS bookkeeping, recomputed in date order by `golf recompute`.
CREATE TABLE IF NOT EXISTS handicap_history(
  round_id          TEXT PRIMARY KEY REFERENCES rounds(round_id),
  as_of             TEXT NOT NULL,
  n_scores          INTEGER,
  ags               INTEGER,
  differential      REAL,
  differential_kind TEXT CHECK(differential_kind IN ('ndb_adjusted','gross_upper_bound','nine_hole_scaled')),
  hi_before         REAL,
  hi_after          REAL,
  low_hi            REAL,
  cap_applied       TEXT,
  esr               REAL
);

-- ------------------------------------------------------------------- LLM
-- One row per real API call. cache_key makes re-runs free and makes tests replayable.
CREATE TABLE IF NOT EXISTS llm_calls(
  call_id            INTEGER PRIMARY KEY,
  cache_key          TEXT NOT NULL UNIQUE,
  purpose            TEXT NOT NULL,     -- 'round_screenshots' | 'note' | 'notebook_page' | 'reread' | ...
  model              TEXT NOT NULL,
  prompt_version     TEXT NOT NULL,
  schema_version     TEXT NOT NULL,
  request_meta       TEXT,              -- JSON: image hashes, doc ids, effort (never note text or image bytes)
  response_json      TEXT,              -- raw JSON text returned by the model
  stop_reason        TEXT,
  input_tokens       INTEGER,
  output_tokens      INTEGER,
  cache_read_tokens  INTEGER,
  cost_usd           REAL,
  created_at         TEXT NOT NULL
);

-- Vision extractions of round screenshots.
CREATE TABLE IF NOT EXISTS extractions(
  extraction_id  INTEGER PRIMARY KEY,
  round_id       TEXT,                  -- matched/created round
  image_paths    TEXT NOT NULL,         -- JSON list, relative to data_dir
  image_sha256s  TEXT NOT NULL,         -- JSON list
  call_id        INTEGER REFERENCES llm_calls(call_id),
  status         TEXT NOT NULL CHECK(status IN ('extracted','auto_accepted','needs_review','accepted','rejected')),
  flags          TEXT NOT NULL DEFAULT '[]',   -- JSON list of {code, severity, hole, field, message}
  result_json    TEXT,                  -- validated ExtractedRound JSON
  created_at     TEXT NOT NULL,
  reviewed_at    TEXT
);

-- Every human correction (rounds or events). The corrected set becomes the golden eval set.
CREATE TABLE IF NOT EXISTS corrections(
  id              INTEGER PRIMARY KEY,
  entity          TEXT NOT NULL CHECK(entity IN ('round_hole','round','event')),
  entity_id       TEXT NOT NULL,
  field           TEXT NOT NULL,
  model_value     TEXT,
  corrected_value TEXT,
  extraction_id   INTEGER,
  call_id         INTEGER,
  corrected_at    TEXT NOT NULL
);

-- ---------------------------------------------------------- notes/timeline
CREATE TABLE IF NOT EXISTS source_docs(
  doc_id        TEXT PRIMARY KEY,       -- 'an-<apple note id hash>' | 'nb-<image sha[:16]>' | 'man-<n>'
  source        TEXT NOT NULL CHECK(source IN ('apple_notes','notebook','manual')),
  external_id   TEXT,                   -- Apple Notes x-coredata id; image path for notebook pages
  title         TEXT,
  folder        TEXT,
  created_at    TEXT,                   -- ISO local datetime
  modified_at   TEXT,
  text          TEXT,                   -- note plaintext / notebook transcription (never leaves the machine except to the API)
  content_hash  TEXT,
  status        TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','locked','deleted')),
  flags         TEXT NOT NULL DEFAULT '[]',
  extracted_key TEXT,                   -- extraction signature reflected in events (golf.notes.extract.extraction_signature)
  deleted_at    TEXT
);

CREATE TABLE IF NOT EXISTS events(
  event_id          TEXT PRIMARY KEY,   -- sha256(doc_id|event_type|norm(excerpt)|ordinal)[:16]
  doc_id            TEXT NOT NULL REFERENCES source_docs(doc_id),
  call_id           INTEGER REFERENCES llm_calls(call_id),
  status            TEXT NOT NULL CHECK(status IN ('pending','auto_accepted','orphaned','superseded')),
  event_type        TEXT NOT NULL,
  is_planned        INTEGER NOT NULL DEFAULT 0,
  date              TEXT,               -- resolved representative date (YYYY-MM-DD)
  date_start        TEXT,
  date_end          TEXT,
  date_precision    TEXT CHECK(date_precision IN ('exact','day','week','month','inferred')),
  date_source       TEXT,               -- 'explicit_text'|'relative_reference'|'entry_header'|'note_created'|'log_position'|'manual'
  date_expression   TEXT,
  date_reasoning    TEXT,
  coach             TEXT,
  lesson_format     TEXT,
  location          TEXT,
  location_kind     TEXT,
  duration_minutes  INTEGER,
  focus_areas       TEXT,               -- JSON
  game_areas        TEXT,               -- comma list for filtering
  drills            TEXT,               -- JSON
  swing_thoughts    TEXT,               -- JSON
  equipment         TEXT,               -- JSON
  measurements      TEXT,               -- JSON
  injury            TEXT,               -- JSON
  goal_text         TEXT,
  summary           TEXT,
  source_excerpt    TEXT,
  line_start        INTEGER,
  line_end          INTEGER,
  excerpt_verified  INTEGER NOT NULL DEFAULT 0,
  confidence        TEXT,
  flags             TEXT NOT NULL DEFAULT '[]',
  linked_round_id   TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_date ON events(date);

-- Human overlay; the extractor never writes here.
CREATE TABLE IF NOT EXISTS event_reviews(
  event_id    TEXT PRIMARY KEY,
  decision    TEXT NOT NULL CHECK(decision IN ('accepted','rejected','edited','merged')),
  merged_into TEXT,
  edits_json  TEXT,                     -- JSON merge-patch applied on read
  comment     TEXT,
  reviewed_at TEXT NOT NULL
);

-- Events that count: auto-accepted and never reviewed, or reviewed as accepted/edited.
-- (edits_json is applied in Python by golf.notes.timeline().)
CREATE VIEW IF NOT EXISTS timeline_raw AS
SELECT e.*, r.decision AS review_decision, r.edits_json AS review_edits
FROM events e LEFT JOIN event_reviews r ON r.event_id = e.event_id
WHERE (r.decision IS NULL AND e.status = 'auto_accepted')
   OR (r.decision IN ('accepted','edited') AND e.status IN ('pending','auto_accepted'));

-- Rounds that count for analysis (overrides applied).
CREATE VIEW IF NOT EXISTS v_rounds AS
SELECT r.*,
       COALESCE(o.course_key, r.course_key) AS course_key_eff,
       COALESCE(o.tee_id, r.tee_id)         AS tee_id_eff,
       COALESCE(o.exclude, 0)               AS excluded
FROM rounds r LEFT JOIN round_overrides o ON o.round_id = r.round_id
WHERE r.deleted_in_source = 0 AND r.entry_mode <> 'abandoned';
