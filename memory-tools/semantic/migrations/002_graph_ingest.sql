-- 002_graph_ingest — additive graph-ingestion accounting (Astra transition Batch 2).
--
-- Applied ONLY by an explicit migration step (graph_state.migrate via
-- `sem_backfill.py --migrate` or `sem.py init`). Never applied on a read path.
-- Existing tables (entity, alias, edge, mention, suppressed_observation,
-- distillation, consolidation_run, rejected_candidate, alias_proposal_seen)
-- are untouched. Legacy `mention` rows stay unclassified; the new automatic
-- layer lives only in graph_auto_mention.

-- Singleton local ingestion configuration. `mode` is 'shadow' until an
-- independently reviewed activation flips it; shadow rows are written but
-- never consumed by readers. This is local configuration, not authorization
-- to add sources.
CREATE TABLE IF NOT EXISTS graph_ingest_control (
  id                  INTEGER PRIMARY KEY CHECK (id = 1),
  schema_revision     INTEGER NOT NULL,
  activation_revision TEXT    NOT NULL,
  mode                TEXT    NOT NULL DEFAULT 'shadow' CHECK (mode IN ('shadow', 'active')),
  source_scope_sha256 TEXT    NOT NULL,          -- hash of the approved source scope
  updated_at          TEXT    NOT NULL
);

-- One row per backfill attempt. Failed/interrupted runs are preserved when
-- later runs pass; last attempt and last completed scan are separate facts.
CREATE TABLE IF NOT EXISTS graph_ingest_run (
  run_id          TEXT PRIMARY KEY,
  entry           TEXT NOT NULL,                 -- direct|scheduled|fixture
  mode            TEXT NOT NULL,                 -- shadow|active (control mode at start)
  source_revision TEXT,                          -- JSON: per-store source identity guards
  reader_revision TEXT NOT NULL,
  alias_revision  TEXT NOT NULL,
  policy_revision TEXT NOT NULL,
  started_at      TEXT NOT NULL,                 -- UTC ISO
  ended_at        TEXT,                          -- UTC ISO, NULL while running/interrupted
  result          TEXT NOT NULL DEFAULT 'running',
                  -- running|completed|remaining|unavailable|identity_conflict|failed|interrupted
  counters_json   TEXT NOT NULL DEFAULT '{}',    -- phase counters, safe reason codes only
  error_code      TEXT                           -- safe error code, never source narrative
);

-- Continuation state per (store, phase). phase is 'new' (bounded new-record
-- lane) or 'reconcile' (rotating reconciliation lane). The high-water boundary
-- is fixed for the epoch; the cursor moves only after a durable outcome exists
-- and is advanced by compare-and-swap in the publishing transaction.
CREATE TABLE IF NOT EXISTS graph_scan_cursor (
  store                TEXT    NOT NULL,
  phase                TEXT    NOT NULL,
  epoch                INTEGER NOT NULL DEFAULT 0,
  high_water           TEXT,                     -- max id (DB stores) / inventory hash (file)
  boundary_json        TEXT,                     -- file store: the sorted inventory names
  cursor               TEXT,                     -- last durably processed key
  source_identity      TEXT,                     -- identity guard of the source at epoch start
  revision_json        TEXT,                     -- {"alias":..,"reader":..,"policy":..} at epoch start
  last_completed_epoch INTEGER,
  last_completed_at    TEXT,
  completion_json      TEXT,                     -- membership/expected counts at close
  updated_at           TEXT    NOT NULL,
  PRIMARY KEY (store, phase)
);

-- Derived coverage state per source record for THIS local installation.
-- Records derived coverage, not decisions or project status.
CREATE TABLE IF NOT EXISTS graph_record_state (
  store                 TEXT    NOT NULL,
  ref                   TEXT    NOT NULL,
  ref_num               INTEGER,                 -- numeric key for DB stores (keyset order); NULL for file
  identity_guard        TEXT    NOT NULL,        -- immutable once written; mismatch => identity_conflict
  content_sha256        TEXT,                    -- length-delimited SHA-256 of selected fields
  reader_revision       TEXT,
  alias_revision        TEXT,
  policy_revision       TEXT,
  observed_at           TEXT    NOT NULL,
  verified_at           TEXT,
  last_seen_epoch       INTEGER,
  status                TEXT    NOT NULL,
                        -- matched|unmatched|excluded|pending|unsupported|unavailable|identity_conflict|missing_inactive
  reason                TEXT,                    -- safe reason code
  last_success_revision TEXT,
  last_success_run      TEXT,
  last_attempt_run      TEXT    NOT NULL,
  PRIMARY KEY (store, ref)
);
CREATE INDEX IF NOT EXISTS idx_graph_record_state_order  ON graph_record_state(store, ref_num);
CREATE INDEX IF NOT EXISTS idx_graph_record_state_status ON graph_record_state(store, status);

-- Automatic links owned by the new backfill writer only. A source update
-- transaction replaces only its own rows for that (store, ref). ON DELETE
-- RESTRICT: an entity with owned support cannot be cascade-deleted.
CREATE TABLE IF NOT EXISTS graph_auto_mention (
  store           TEXT    NOT NULL,
  ref             TEXT    NOT NULL,
  entity_id       INTEGER NOT NULL REFERENCES entity(id) ON DELETE RESTRICT,
  content_sha256  TEXT    NOT NULL,
  alias_revision  TEXT    NOT NULL,
  reader_revision TEXT    NOT NULL,
  policy_revision TEXT    NOT NULL,
  weight          REAL    NOT NULL,
  run_id          TEXT    NOT NULL,
  created_at      TEXT    NOT NULL,
  PRIMARY KEY (store, ref, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_graph_auto_mention_entity ON graph_auto_mention(entity_id);
