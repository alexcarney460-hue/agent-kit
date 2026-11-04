-- Semantic layer for the memory stack.
-- Sits ABOVE hivemind / claude-mem / file memories. Never writes to them.
--
-- The stores record episodes (what happened) and rules (what to do). This records
-- THINGS: canonical entities, the names they go by, how they relate, and which
-- records in which store are about them. That is what makes retrieval resolve by
-- meaning instead of by string overlap.

PRAGMA journal_mode=WAL;

-- A canonical thing. One row per real-world referent, however many names it has.
CREATE TABLE IF NOT EXISTS entity (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  slug        TEXT    NOT NULL UNIQUE,          -- stable kebab id, safe to hardcode
  kind        TEXT    NOT NULL,                 -- person|org|venture|system|artifact|account|concept
  name        TEXT    NOT NULL,                 -- display name
  summary     TEXT,                             -- one line, what an agent needs first
  valid_as_of TEXT,                             -- ISO date these facts were last verified
  source      TEXT,                             -- provenance: where the facts came from
  confidence  REAL    NOT NULL DEFAULT 0.7,
  protected   INTEGER NOT NULL DEFAULT 0,       -- pin in recall (your key ventures/clients)
  created_at  TEXT    NOT NULL,
  updated_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entity_kind ON entity(kind);

-- Every string that refers to an entity. This is the piece that fixes the
-- "searched one name, missed records filed under another" failure class.
CREATE TABLE IF NOT EXISTS alias (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_id INTEGER NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
  alias     TEXT    NOT NULL,                   -- stored lowercase
  UNIQUE(entity_id, alias)
);
CREATE INDEX IF NOT EXISTS idx_alias_alias ON alias(alias);

-- Typed relations. The existing [[wiki-links]] carry no relation, so nothing can
-- answer "what supersedes this?" — which is the question primer.md keeps losing.
CREATE TABLE IF NOT EXISTS edge (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  src         INTEGER NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
  rel         TEXT    NOT NULL,
  dst         INTEGER NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
  note        TEXT,
  valid_as_of TEXT,
  created_at  TEXT    NOT NULL,
  UNIQUE(src, rel, dst)
);
CREATE INDEX IF NOT EXISTS idx_edge_src ON edge(src);
CREATE INDEX IF NOT EXISTS idx_edge_dst ON edge(dst);

-- Entity -> a record in an underlying store. The join that lets a query resolve to
-- an entity and then pull records with ZERO lexical overlap with the query.
CREATE TABLE IF NOT EXISTS mention (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_id  INTEGER NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
  store      TEXT    NOT NULL,                  -- hivemind|claudemem|file|artifact
  ref        TEXT    NOT NULL,                  -- hivemind id | obs id | filename | path
  weight     REAL    NOT NULL DEFAULT 1.0,
  created_at TEXT    NOT NULL,
  UNIQUE(entity_id, store, ref)
);
CREATE INDEX IF NOT EXISTS idx_mention_entity ON mention(entity_id);
CREATE INDEX IF NOT EXISTS idx_mention_ref    ON mention(store, ref);

-- Candidates the classifier judged not to be real entities. Cached so the hourly
-- run does not re-ask about the same noise forever: with nothing new to judge,
-- the model call is skipped entirely and the refresh costs nothing.
CREATE TABLE IF NOT EXISTS rejected_candidate (
  phrase     TEXT PRIMARY KEY,   -- lowercase
  reason     TEXT,
  rejected_at TEXT NOT NULL
);
