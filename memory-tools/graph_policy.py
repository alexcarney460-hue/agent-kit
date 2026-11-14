#!/usr/bin/env python3
"""graph_policy — one explicit eligibility result shared by graph ingestion and
graph/recall readers (Astra transition Batch 2, staged).

This module does not invent policy. It binds the restrictions that are already
verified in code and applies them consistently:

  * claudemem   — the consolidation suppression overlay (`suppressed_observation`
                  in semantic.db) is REQUIRED state. Missing, unreadable or
                  wrong-schema overlay => the claudemem store is `unavailable`
                  and is withheld across every path. Never an empty exclusion set.
                  A record must also still exist in the source.
  * hivemind    — `learnings.active=1` (existing retrieval contract). Inactive
                  rows are `missing_inactive`; an inactive row whose
                  `superseded_by` chain resolves to an active root is retrievable
                  only THROUGH that root (existing local3381 redirect).
  * file        — the record must be a current top-level regular `.md` file.
  * artifact    — UNMAPPED at read time (legacy rows pass through unvalidated;
                  existing behaviour preserved, reported as such).

Owned automatic links (graph_auto_mention) are consumed only when the
graph_ingest_control mode is `active`, the record state is `matched` at the
current content hash, and the link was produced at the current alias and
reader revision. In `shadow` mode they are counted and withheld.

Sources are opened read-only with `query_only=1`; there is no writable fallback,
no `immutable=1`, and text is never decoded with replacement here.

Every reader traversal uses ONE complete EligibilityView built under explicit
node/edge/byte/time budgets. If the view cannot be completed within budget the
result is GraphUnavailable — never a clipped graph.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import sqlite3
import stat
import struct
import time
from pathlib import Path
import memdir

SEM_DB = memdir.semantic_db()
HIVEMIND_DB = memdir.hivemind_db()
CLAUDEMEM_DB = os.path.join(memdir.home(), ".claude-mem", "claude-mem.db")
MEMDIR = memdir.memory_dir()

# Bumped only when the bound authority per store changes (POLICY-MATRIX-DRAFT.md).
POLICY_MATRIX_VERSION = "batch2-stage-draft-1"
CODE_REVISION = "batch2-stage-1"

CONTROL_TABLES = ("graph_ingest_control", "graph_ingest_run", "graph_scan_cursor",
                  "graph_record_state", "graph_auto_mention")
GRAPH_STORES = ("hivemind", "claudemem", "file")   # stores that join PPR / graph statistics
STORES = GRAPH_STORES

SUPPRESSION_COLUMNS = ("obs_id", "canonical_id", "reason", "created_at")


class PolicyUnavailable(Exception):
    """A required privacy/eligibility input cannot be established."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GraphUnavailable(Exception):
    """A complete eligible graph view could not be established within budget."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclasses.dataclass(frozen=True)
class Budgets:
    max_links: int = 300_000
    max_entities: int = 10_000
    max_edges: int = 50_000
    max_source_ids: int = 2_000_000

    # Budget checks are cooperative and fail closed if a complete view is unavailable.
    wall_seconds: float = 15.0
    max_bytes: int = 64 * 1024 * 1024


@dataclasses.dataclass(frozen=True)
class Sources:
    """Resolved at construction time from the module globals, so tests that
    redirect `graph_policy.SEM_DB` etc. are honoured."""
    sem_db: str = dataclasses.field(default_factory=lambda: SEM_DB)
    hivemind: str = dataclasses.field(default_factory=lambda: HIVEMIND_DB)
    claudemem: str = dataclasses.field(default_factory=lambda: CLAUDEMEM_DB)
    memdir: str = dataclasses.field(default_factory=lambda: MEMDIR)


def source_scope() -> dict:
    """The approved source scope as declared by this module: which stores, at
    which paths, which fields, under which eligibility rule."""
    return {
        "policy_matrix_version": POLICY_MATRIX_VERSION,
        "stores": {
            "hivemind": {"path": HIVEMIND_DB, "table": "learnings", "fields": ["rule", "context"],
                          "eligibility": "active=1; superseded_by chain redirect"},
            "claudemem": {"path": CLAUDEMEM_DB, "table": "observations",
                           "fields": ["title", "text", "facts", "narrative", "concepts"],
                           "eligibility": "suppressed_observation overlay (required state)"},
            "file": {"path": MEMDIR, "pattern": "top-level *.md regular files"},
        },
    }


def source_scope_sha256() -> str:
    """SHA-256 of the canonical JSON of `source_scope()`; stored in
    graph_ingest_control.source_scope_sha256 and rechecked by the writer."""
    return hashlib.sha256(json.dumps(source_scope(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def ld_sha256(*parts: bytes) -> str:
    """Length-delimited SHA-256: each part is prefixed by its 8-byte big-endian
    length so field boundaries can never be ambiguous."""
    h = hashlib.sha256()
    for p in parts:
        if not isinstance(p, (bytes, bytearray)):
            raise TypeError("ld_sha256 takes bytes only")
        h.update(struct.pack(">Q", len(p)))
        h.update(p)
    return h.hexdigest()


def open_ro(path: str, timeout: float = 10.0) -> sqlite3.Connection:
    """Ordinary read-only connection. No writable fallback, no immutable=1.
    Raises sqlite3.Error (callers translate to unavailable)."""
    if not os.path.isfile(path):
        raise sqlite3.OperationalError("source_missing")
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=timeout)
    conn.execute("PRAGMA query_only=1")
    conn.execute("PRAGMA busy_timeout=%d" % int(timeout * 1000))
    conn.text_factory = bytes
    return conn


def utf8_strict(b) -> str:
    """Decode bytes strictly. TEXT already decoded (str) passes through."""
    if isinstance(b, str):
        return b
    if b is None:
        return ""
    return bytes(b).decode("utf-8")  # raises UnicodeDecodeError: caller records unsupported


class _Deadline:
    def __init__(self, seconds: float) -> None:
        self.t0 = time.monotonic()
        self.seconds = seconds

    def check(self, what: str) -> None:
        if time.monotonic() - self.t0 > self.seconds:
            raise GraphUnavailable(f"budget:time:{what}")

    def elapsed(self) -> float:
        return time.monotonic() - self.t0


# --------------------------------------------------------------------------- #
# suppression overlay (REQUIRED state for claudemem)
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class PolicyOverlay:
    suppressed: frozenset
    revision: str
    row_count: int


def load_policy(sconn: sqlite3.Connection) -> PolicyOverlay:
    try:
        cols = [r[1] for r in sconn.execute("PRAGMA table_info(suppressed_observation)")]
        cols = [c.decode() if isinstance(c, bytes) else c for c in cols]
        if not cols:
            raise PolicyUnavailable("overlay_missing")
        if tuple(cols[:4]) != SUPPRESSION_COLUMNS:
            raise PolicyUnavailable("overlay_wrong_schema")
        rows = sconn.execute(
            "SELECT obs_id, canonical_id FROM suppressed_observation ORDER BY obs_id").fetchall()
    except sqlite3.Error as exc:
        raise PolicyUnavailable(f"overlay_read_error:{type(exc).__name__}") from exc
    h = hashlib.sha256(POLICY_MATRIX_VERSION.encode())
    ids = []
    for obs_id, canon in rows:
        if type(obs_id) is not int or type(canon) is not int:
            raise PolicyUnavailable("overlay_wrong_shape")
        h.update(struct.pack(">qq", obs_id, canon))
        ids.append(obs_id)
    return PolicyOverlay(frozenset(ids), h.hexdigest(), len(ids))


# --------------------------------------------------------------------------- #
# alias mapping revision (ordered, normalized, ambiguity explicit)
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class AliasMap:
    revision: str
    mapping: dict          # normalized alias -> entity_id (unambiguous only)
    conflicts: dict        # normalized alias -> tuple of entity ids (ambiguous)
    entity_ids: frozenset


def normalize_alias(alias: str) -> str:
    return re.sub(r"\s+", " ", alias.strip().lower())


def load_alias_map(sconn: sqlite3.Connection) -> AliasMap:
    try:
        rows = sconn.execute(
            "SELECT CAST(a.alias AS BLOB), a.entity_id FROM alias a JOIN entity e ON e.id=a.entity_id"
        ).fetchall()
        ent = sconn.execute("SELECT id FROM entity").fetchall()
    except sqlite3.Error as exc:
        raise PolicyUnavailable(f"alias_read_error:{type(exc).__name__}") from exc
    groups: dict = {}
    for raw, eid in rows:
        try:
            alias = utf8_strict(raw)
        except UnicodeDecodeError as exc:
            raise PolicyUnavailable("alias_invalid_utf8") from exc
        if type(eid) is not int:
            raise PolicyUnavailable("alias_wrong_shape")
        norm = normalize_alias(alias)
        if not norm:
            continue
        groups.setdefault(norm, set()).add(eid)
    mapping, conflicts = {}, {}
    h = hashlib.sha256(CODE_REVISION.encode())
    for norm in sorted(groups):
        eids = tuple(sorted(groups[norm]))
        h.update(struct.pack(">Q", len(norm)))
        h.update(norm.encode())
        h.update(struct.pack(">Q", len(eids)))
        for e in eids:
            h.update(struct.pack(">q", e))
        if len(eids) == 1:
            mapping[norm] = eids[0]
        else:
            conflicts[norm] = eids
    return AliasMap(h.hexdigest(), mapping, conflicts, frozenset(r[0] for r in ent))


def reader_revision() -> str:
    """Hash of the matcher's constants and code (imported lazily to avoid a cycle)."""
    import inspect
    import sem_backfill
    return ld_sha256(
        CODE_REVISION.encode(),
        sem_backfill.MATCHER_VERSION.encode(),
        str(sem_backfill.MIN_ALIAS).encode(),
        ",".join(sorted(sem_backfill.AMBIGUOUS)).encode(),
        inspect.getsource(sem_backfill.hits_for).encode(),
        inspect.getsource(sem_backfill.build_matcher_full).encode(),
    )


# --------------------------------------------------------------------------- #
# ingestion control (new tables may be ABSENT: legacy-only view)
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class IngestControl:
    present: bool
    mode: str                 # 'legacy-only' when tables absent
    schema_revision: int | None = None
    activation_revision: str | None = None
    source_scope_sha256: str | None = None


def tables_present(sconn: sqlite3.Connection) -> set:
    rows = sconn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN (%s)"
        % ",".join("?" * len(CONTROL_TABLES)), CONTROL_TABLES).fetchall()
    return {r[0].decode() if isinstance(r[0], bytes) else r[0] for r in rows}


def load_control(sconn: sqlite3.Connection) -> IngestControl:
    try:
        present = tables_present(sconn)
        if not present:
            return IngestControl(False, "legacy-only")
        if present != set(CONTROL_TABLES):
            raise GraphUnavailable("partial_migration")
        row = sconn.execute(
            "SELECT schema_revision, activation_revision, mode, source_scope_sha256 FROM graph_ingest_control WHERE id=1"
        ).fetchone()
    except sqlite3.Error as exc:
        raise GraphUnavailable(f"control_read_error:{type(exc).__name__}") from exc
    if row is None:
        raise GraphUnavailable("control_row_missing")
    mode = row[2].decode() if isinstance(row[2], bytes) else row[2]
    if mode not in ("shadow", "active"):
        raise GraphUnavailable("control_mode_invalid")
    act = row[1].decode() if isinstance(row[1], bytes) else row[1]
    scope = row[3].decode() if isinstance(row[3], bytes) else row[3]
    return IngestControl(True, mode, row[0], act, scope)


# --------------------------------------------------------------------------- #
# source membership (existence / active) for the graph stores
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class StoreState:
    status: str     # eligible|unavailable
    reason: str = ""


def hivemind_membership(path: str, budgets: Budgets, deadline: _Deadline):
    """(active_ids: frozenset[str], redirect: {expired_id: active_root_id})."""
    c = open_ro(path)
    try:
        rows = c.execute("SELECT id, active, superseded_by FROM learnings").fetchall()
    finally:
        c.close()
    if len(rows) > budgets.max_source_ids:
        raise GraphUnavailable("budget:source_ids:hivemind")
    state = {}
    for rid, active, sup in rows:
        if type(rid) is not int:
            raise GraphUnavailable("hivemind_shape")
        state[rid] = (bool(active), sup if type(sup) is int else None)
    active = frozenset(str(rid) for rid, (a, _s) in state.items() if a)
    red = {}
    for rid, (a, sup) in state.items():
        if a or sup is None:
            continue
        root, seen = sup, set()
        while root in state and root not in seen:
            seen.add(root)
            ra, nxt = state[root]
            if ra:
                red[str(rid)] = str(root)
                break
            root = nxt
    deadline.check("hivemind")
    return active, red


def claudemem_membership(path: str, budgets: Budgets, deadline: _Deadline) -> frozenset:
    c = open_ro(path)
    try:
        rows = c.execute("SELECT id FROM observations").fetchall()
    finally:
        c.close()
    if len(rows) > budgets.max_source_ids:
        raise GraphUnavailable("budget:source_ids:claudemem")
    ids = set()
    for (oid,) in rows:
        if type(oid) is not int:
            raise GraphUnavailable("claudemem_shape")
        ids.add(oid)
    deadline.check("claudemem")
    return frozenset(ids)


def file_inventory(memdir: str) -> tuple:
    """Sorted top-level regular `.md` names. Symlinks are not followed."""
    if not os.path.isdir(memdir) or os.path.islink(memdir):
        raise OSError("memdir_unavailable")
    names = []
    for fn in os.listdir(memdir):
        if not fn.endswith(".md"):
            continue
        st = os.lstat(os.path.join(memdir, fn))
        if stat.S_ISREG(st.st_mode):
            names.append(fn)
    return tuple(sorted(names))


# --------------------------------------------------------------------------- #
# the coherent eligibility view
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class EligibilityView:
    revision: str
    policy_revision: str
    alias_revision: str
    reader_revision: str
    control: IngestControl
    stores: dict                       # store -> StoreState
    links: tuple                       # (entity_id, store, ref, weight, ownership)
    edges: tuple                       # (src, dst, rel)
    eligible_keys: frozenset           # (store, ref) for GRAPH_STORES: exists & not excluded
    excluded_keys: frozenset           # (store, ref) withheld by a verified policy decision
    hivemind_redirect: dict            # expired id -> active root id
    counts: dict
    budgets: Budgets
    elapsed: float

    def store_available(self, store: str) -> bool:
        st = self.stores.get(store)
        return st is not None and st.status == "eligible"

    def is_eligible(self, store: str, ref) -> bool:
        if store == "artifact":
            return False
        if not self.store_available(store):
            return False
        return (store, str(ref)) in self.eligible_keys

    def unavailable_stores(self) -> list:
        return [s for s in STORES if not self.store_available(s)]

    def summary(self) -> str:
        c = self.counts
        parts = [f"rev={self.revision[:12]}", f"mode={self.control.mode}",
                 f"legacy={c.get('legacy_eligible', 0)}/{c.get('legacy_total', 0)}",
                 f"owned={c.get('owned_eligible', 0)}/{c.get('owned_total', 0)}"]
        if c.get("owned_shadow"):
            parts.append(f"owned_shadow={c['owned_shadow']}")
        if c.get("owned_stale"):
            parts.append(f"owned_stale={c['owned_stale']}")
        if c.get("legacy_excluded"):
            parts.append(f"excluded={c['legacy_excluded']}")
        if c.get("legacy_missing"):
            parts.append(f"missing={c['legacy_missing']}")
        ua = self.unavailable_stores()
        if ua:
            parts.append("UNAVAILABLE=" + ",".join(f"{s}({self.stores[s].reason})" for s in ua))
        return " ".join(parts)


def _decode_cell(v):
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8")
    return v


def load_view(sconn: sqlite3.Connection | None = None, sources: Sources | None = None,
              budgets: Budgets | None = None) -> EligibilityView:
    """Build one complete eligibility view. Raises GraphUnavailable when the
    semantic layer itself cannot be read or a budget is exhausted; a single
    source's unavailability is recorded per store instead (that store is
    withheld everywhere)."""
    sources = sources or Sources()
    budgets = budgets or Budgets()
    deadline = _Deadline(budgets.wall_seconds)
    own = sconn is None
    if own:
        try:
            sconn = open_ro(sources.sem_db)
        except sqlite3.Error as exc:
            raise GraphUnavailable(f"semantic_db_unavailable:{type(exc).__name__}") from exc

    def progress():
        return 1 if deadline.elapsed() > budgets.wall_seconds else 0

    sconn.set_progress_handler(progress, 20_000)
    try:
        return _build(sconn, sources, budgets, deadline)
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc).lower():
            raise GraphUnavailable("budget:time:semantic_query") from exc
        raise GraphUnavailable(f"semantic_read_error:{type(exc).__name__}") from exc
    except sqlite3.Error as exc:
        raise GraphUnavailable(f"semantic_read_error:{type(exc).__name__}") from exc
    finally:
        sconn.set_progress_handler(None, 0)
        if own:
            sconn.close()


def _build(sconn, sources, budgets, deadline) -> EligibilityView:
    stores: dict = {}
    counts: dict = {}
    # 1. required privacy state
    try:
        policy = load_policy(sconn)
        policy_rev = policy.revision
        suppressed = policy.suppressed
        stores["claudemem"] = StoreState("eligible")
    except PolicyUnavailable as exc:
        policy_rev = "unavailable:" + exc.reason
        suppressed = frozenset()
        stores["claudemem"] = StoreState("unavailable", exc.reason)
    try:
        aliases = load_alias_map(sconn)
    except PolicyUnavailable as exc:
        raise GraphUnavailable("aliases:" + exc.reason) from exc
    control = load_control(sconn)
    rrev = reader_revision()
    deadline.check("policy")

    # 2. source membership
    hm_active, hm_redirect = frozenset(), {}
    try:
        hm_active, hm_redirect = hivemind_membership(sources.hivemind, budgets, deadline)
        stores["hivemind"] = StoreState("eligible")
    except (sqlite3.Error, OSError) as exc:
        stores["hivemind"] = StoreState("unavailable", f"source:{type(exc).__name__}")
    cm_ids = frozenset()
    if stores["claudemem"].status == "eligible":
        try:
            cm_ids = claudemem_membership(sources.claudemem, budgets, deadline)
        except (sqlite3.Error, OSError) as exc:
            stores["claudemem"] = StoreState("unavailable", f"source:{type(exc).__name__}")
    file_names = frozenset()
    try:
        file_names = frozenset(file_inventory(sources.memdir))
        stores["file"] = StoreState("eligible")
    except OSError as exc:
        stores["file"] = StoreState("unavailable", f"source:{type(exc).__name__}")
    stores["artifact"] = StoreState("unavailable", "unsupported")

    eligible = set()
    excluded = set()
    if stores["hivemind"].status == "eligible":
        eligible.update(("hivemind", r) for r in hm_active)
        eligible.update(("hivemind", r) for r in hm_redirect)
    if stores["claudemem"].status == "eligible":
        for oid in cm_ids:
            key = ("claudemem", str(oid))
            if oid in suppressed:
                excluded.add(key)
            else:
                eligible.add(key)
    if stores["file"].status == "eligible":
        eligible.update(("file", n) for n in file_names)
    deadline.check("membership")

    # 3. legacy links (unclassified ownership) filtered by eligibility
    n_ent = sconn.execute("SELECT count(*) FROM entity").fetchone()[0]
    if n_ent > budgets.max_entities:
        raise GraphUnavailable("budget:entities")
    n_links = sconn.execute("SELECT count(*) FROM mention").fetchone()[0]
    if n_links > budgets.max_links:
        raise GraphUnavailable("budget:links")
    combined: dict = {}
    byte_count = 0
    c_total = c_ok = c_excl = c_missing = c_unavail = 0
    for eid, store_b, ref_b, w in sconn.execute("SELECT entity_id, store, ref, weight FROM mention"):
        c_total += 1
        store = _decode_cell(store_b)
        ref = _decode_cell(ref_b)
        byte_count += len(ref_b) if isinstance(ref_b, (bytes, bytearray)) else len(str(ref))
        if byte_count > budgets.max_bytes:
            raise GraphUnavailable("budget:bytes")
        st = stores.get(store)
        if st is None or st.status != "eligible":
            c_unavail += 1
            continue
        if store == "artifact":
            ok = True
        else:
            key = (store, ref)
            if key in excluded:
                c_excl += 1
                continue
            ok = key in eligible
        if not ok:
            c_missing += 1
            continue
        c_ok += 1
        k = (eid, store, ref)
        prev = combined.get(k)
        wt = float(w) if w is not None else 0.5
        combined[k] = (max(prev[0], wt) if prev else wt, "legacy")
    counts.update(legacy_total=c_total, legacy_eligible=c_ok, legacy_excluded=c_excl,
                  legacy_missing=c_missing, legacy_withheld_unavailable=c_unavail)
    deadline.check("legacy_links")

    # 4. owned links (consumed only in active mode at current revisions)
    o_total = o_ok = o_stale = o_shadow = o_withheld = 0
    if control.present and control.mode != "active":
        # Shadow owned links never enter the view: count them in SQL (bounded by the progress-handler deadline)
        # and keep them off the link budget. Charging them switched the whole graph off once shadow rows piled
        # up as legacy and shadow rows accumulate.
        o_total = o_shadow = sconn.execute(
            "SELECT count(*) FROM graph_auto_mention am "
            "JOIN graph_record_state rs ON rs.store=am.store AND rs.ref=am.ref").fetchone()[0]
    elif control.present:
        n_owned = sconn.execute("SELECT count(*) FROM graph_auto_mention").fetchone()[0]
        if n_links + n_owned > budgets.max_links:
            raise GraphUnavailable("budget:links")
        for eid, store_b, ref_b, w, arev, rrev_row, ahash, status_b, shash in sconn.execute(
                "SELECT am.entity_id, am.store, am.ref, am.weight, am.alias_revision, am.reader_revision, "
                "am.content_sha256, rs.status, rs.content_sha256 FROM graph_auto_mention am "
                "JOIN graph_record_state rs ON rs.store=am.store AND rs.ref=am.ref"):
            o_total += 1
            store, ref = _decode_cell(store_b), _decode_cell(ref_b)
            status = _decode_cell(status_b)
            if status != "matched" or _decode_cell(ahash) != _decode_cell(shash) \
                    or _decode_cell(arev) != aliases.revision or _decode_cell(rrev_row) != rrev:
                o_stale += 1
                continue
            st = stores.get(store)
            key = (store, ref)
            if st is None or st.status != "eligible" or key in excluded or key not in eligible:
                o_withheld += 1
                continue
            o_ok += 1
            k = (eid, store, ref)
            prev = combined.get(k)
            wt = float(w)
            if prev:
                combined[k] = (max(prev[0], wt), "both")
            else:
                combined[k] = (wt, "owned")
    counts.update(owned_total=o_total, owned_eligible=o_ok, owned_stale=o_stale,
                  owned_shadow=o_shadow, owned_withheld=o_withheld)
    deadline.check("owned_links")

    # 5. edges
    n_edges = sconn.execute("SELECT count(*) FROM edge").fetchone()[0]
    if n_edges > budgets.max_edges:
        raise GraphUnavailable("budget:edges")
    edges = tuple((s, d, _decode_cell(r)) for s, d, r in sconn.execute("SELECT src, dst, rel FROM edge"))
    deadline.check("edges")

    links = tuple(sorted((eid, store, ref, wt, own) for (eid, store, ref), (wt, own) in combined.items()))
    counts["links_combined"] = len(links)
    counts["entities"] = n_ent
    counts["edges"] = len(edges)
    revision = ld_sha256(policy_rev.encode(), aliases.revision.encode(), rrev.encode(),
                         control.mode.encode(),
                         ",".join(f"{s}:{stores[s].status}" for s in sorted(stores)).encode())
    return EligibilityView(
        revision=revision, policy_revision=policy_rev, alias_revision=aliases.revision,
        reader_revision=rrev, control=control, stores=stores, links=links, edges=edges,
        eligible_keys=frozenset(eligible), excluded_keys=frozenset(excluded),
        hivemind_redirect=dict(hm_redirect), counts=counts, budgets=budgets,
        elapsed=deadline.elapsed())


def fallback_view(reason: str, sources: Sources | None = None,
                  budgets: Budgets | None = None) -> EligibilityView:
    """The view used when the semantic layer itself cannot be read: no graph
    links, claudemem WITHHELD (its privacy overlay lives in semantic.db),
    hivemind/file membership still established from their sources so the
    independently eligible lexical paths keep working."""
    sources = sources or Sources()
    budgets = budgets or Budgets()
    deadline = _Deadline(budgets.wall_seconds)
    stores = {"artifact": StoreState("unavailable", f"semantic_layer:{reason}")}
    eligible = set()
    excluded = set()
    hm_redirect: dict = {}
    # claude-mem stays independently eligible ONLY if its privacy overlay can
    # still be read (a graph budget failure does not make the overlay unreadable);
    # otherwise it is withheld.
    policy_rev = "unavailable:" + reason
    try:
        if not os.path.exists(sources.sem_db):
            policy = PolicyOverlay(frozenset(), "no-overlay", 0)
        else:
            sconn = open_ro(sources.sem_db)
            try:
                policy = load_policy(sconn)
            finally:
                sconn.close()
        cm_ids = claudemem_membership(sources.claudemem, budgets, deadline)
        for oid in cm_ids:
            key = ("claudemem", str(oid))
            (excluded if oid in policy.suppressed else eligible).add(key)
        stores["claudemem"] = StoreState("eligible")
        policy_rev = policy.revision
    except (sqlite3.Error, OSError, PolicyUnavailable, GraphUnavailable) as exc:
        stores["claudemem"] = StoreState("unavailable", f"overlay:{getattr(exc, 'reason', type(exc).__name__)}")
    try:
        hm_active, hm_redirect = hivemind_membership(sources.hivemind, budgets, deadline)
        eligible.update(("hivemind", r) for r in hm_active)
        eligible.update(("hivemind", r) for r in hm_redirect)
        stores["hivemind"] = StoreState("eligible")
    except (sqlite3.Error, OSError, GraphUnavailable) as exc:
        stores["hivemind"] = StoreState("unavailable", f"source:{type(exc).__name__}")
    try:
        eligible.update(("file", n) for n in file_inventory(sources.memdir))
        stores["file"] = StoreState("eligible")
    except OSError as exc:
        stores["file"] = StoreState("unavailable", f"source:{type(exc).__name__}")
    control = IngestControl(False, "unavailable")
    return EligibilityView(
        revision="unavailable:" + reason, policy_revision=policy_rev,
        alias_revision="unavailable", reader_revision="unavailable", control=control,
        stores=stores, links=(), edges=(), eligible_keys=frozenset(eligible), excluded_keys=frozenset(excluded),
        hivemind_redirect=dict(hm_redirect), counts={"graph": "unavailable", "legacy_excluded": len(excluded)},
        budgets=budgets, elapsed=deadline.elapsed())


def recheck(view: EligibilityView, sconn: sqlite3.Connection | None = None,
            sources: Sources | None = None) -> tuple:
    """Cheap pre-emit recheck of the policy/alias/control revisions. Returns
    (ok, reason). A mismatch means the graph result is unavailable/retryable."""
    sources = sources or Sources()
    if view.control.mode == "unavailable":
        # Fallback view: there is no graph contribution to invalidate, but claude-mem
        # may be eligible on it — its overlay must still be current at emit time.
        if not view.store_available("claudemem"):
            return True, ""
        try:
            if view.policy_revision == "no-overlay" and not os.path.exists(sources.sem_db):
                return True, ""
            c = open_ro(sources.sem_db)
            try:
                current = load_policy(c).revision
            finally:
                c.close()
        except (sqlite3.Error, PolicyUnavailable) as exc:
            return False, f"recheck_error:{getattr(exc, 'reason', type(exc).__name__)}"
        return (True, "") if current == view.policy_revision else (False, "policy_revision_changed")
    own = sconn is None
    try:
        if own:
            sconn = open_ro(sources.sem_db)
        try:
            policy_rev = load_policy(sconn).revision
        except PolicyUnavailable as exc:
            policy_rev = "unavailable:" + exc.reason
        alias_rev = load_alias_map(sconn).revision
        control = load_control(sconn)
    except (sqlite3.Error, PolicyUnavailable, GraphUnavailable) as exc:
        return False, f"recheck_error:{getattr(exc, 'reason', type(exc).__name__)}"
    finally:
        if own and sconn is not None:
            sconn.close()
    if policy_rev != view.policy_revision:
        return False, "policy_revision_changed"
    if alias_rev != view.alias_revision:
        return False, "alias_revision_changed"
    if control.mode != view.control.mode or control.present != view.control.present:
        return False, "control_changed"
    return True, ""
