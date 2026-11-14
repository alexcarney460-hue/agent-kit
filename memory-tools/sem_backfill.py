#!/usr/bin/env python3
"""sem_backfill — link entities to the records that are about them.

Scans hivemind learnings, claude-mem observations and the file memories, matches
canonical aliases against their text, and writes OWNED automatic links into
semantic.graph_auto_mention (Astra transition Batch 2). Legacy `mention` rows
are never written, relabelled or deleted by this program.

Continuation is deterministic and accountable:
  * ordered numeric keyset scans with a high-water boundary captured per epoch
    (`id > cursor AND id <= high_water ORDER BY id LIMIT n`); files use a
    sorted, versioned inventory of top-level `.md` names;
  * a bounded NEW-record lane plus a rotating RECONCILIATION lane that re-reads
    full fields and recomputes a length-delimited SHA-256, so edits,
    deactivations and deletions below the cursor are found even when the
    source's own hash/mtime/sync_rev is stale;
  * one alias/reader/policy revision fixed per batch; conflicting normalized
    aliases give an explicit `pending:alias_ambiguity` result, never last-row-wins;
  * outcomes are `matched|unmatched|excluded|pending|unsupported|unavailable|
    identity_conflict|missing_inactive`; a cursor advances only after a durable
    outcome exists; owned links, record state and cursor commit in ONE
    transaction after a fresh recheck of source identity/content and policy;
  * sources are read `mode=ro`, `query_only=1`, with per-record byte limits,
    strict UTF-8 (no replacement) and a wall/byte budget per invocation.

READ-ONLY against hivemind and claude-mem. Only semantic.db is written, and
only while holding the writer lock (graph_state.WriterLock).

Usage: sem_backfill.py [--dry-run] [--migrate] [--preflight] [--accept-scope SHA256]
                       [--budget-obs N] [--budget-hm N] [--budget-files N]
                       [--wall-seconds S] [--max-bytes B] [--entry direct|scheduled]
  --limit-obs N   DEPRECATED alias for --budget-obs (a per-invocation budget,
                  NOT a corpus ceiling). --reset is rejected.
  --dry-run       preview only: opens no writer, initializes no schema,
                  consumes no cursor. Surfaces a source-scope mismatch.
  --preflight     check inputs; prints `scope: control=<sha16> current=<sha16>
                  match=true|false`; no writes.
  --accept-scope SHA256
                  re-approve a changed source scope (the reviewed control path):
                  succeeds only when SHA256 equals the digest computed by this
                  process (graph_policy.source_scope_sha256()) AND differs from
                  the stored graph_ingest_control.source_scope_sha256; updates
                  source_scope_sha256, activation_revision and updated_at in one
                  transaction under the writer lock; prints
                  `RESULT scope_accepted {"previous":…,"current":…}`; never runs
                  a batch in the same invocation. mode stays as it is (shadow).

Exit codes: 0 completed/remaining (or scope accepted / preview), 1 failed
(incl. `scope_accept_mismatch`), 3 a source or policy input is unavailable /
identity conflict (nothing healthy is claimed), 4 writer lock held by a live
owner, 5 graph tables absent (run --migrate first), 6 source scope mismatch
(preflight / dry-run: the stored control digest differs from the current one —
remedy: `sem_backfill.py --accept-scope <current digest>`).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import re
import sqlite3
import stat
import sys
import time
from typing import NamedTuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graph_policy  # noqa: E402
import graph_state  # noqa: E402
import sem  # noqa: E402

HM = graph_policy.HIVEMIND_DB
CM = graph_policy.CLAUDEMEM_DB
MEMDIR = graph_policy.MEMDIR

# An alias this short matches far too much prose to be trusted on its own.
MIN_ALIAS = 3
# Aliases that are real words in ordinary English cause false links; require that
# these appear with more specific company of the same entity before counting.
# The examples here are ordinary words; add your own short aliases that are also everyday words.
AMBIGUOUS = {"apple", "delta", "mint", "sage", "scout", "summit", "q"}
# Part of the reader revision. Bump only when hits_for's weight rules change.
MATCHER_VERSION = "weights-2026-07-30"

DEFAULT_BUDGET_OBS = 500
DEFAULT_BUDGET_HM = 200
DEFAULT_BUDGET_FILES = 50
DEFAULT_WALL_SECONDS = 120.0
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
MAX_RECORD_BYTES = 1024 * 1024        # per source record, all selected fields
MAX_FILE_BYTES = 2 * 1024 * 1024
NEW_LANE_SHARE = 0.6                  # the rest of the budget goes to reconciliation
PENDING_FIRST = 50                    # retryable rows re-examined before rotating
GAP_ENQUEUE_LIMIT = 200               # membership gaps queued as pending per epoch close
SQL_EXCLUDE_CHUNK = 5000              # excluded ids per NOT IN clause; the range is walked in chunks

STORES = ("hivemind", "claudemem", "file")

EXIT_OK, EXIT_FAILED, EXIT_UNAVAILABLE, EXIT_LOCKED, EXIT_NO_TABLES, EXIT_SCOPE = 0, 1, 3, 4, 5, 6


# --------------------------------------------------------------------------- #
# matcher (call contract preserved for sem_artifacts.py)
# --------------------------------------------------------------------------- #
class Matcher(NamedTuple):
    pattern: re.Pattern[str]
    amap: dict[str, int]
    ambiguous: set[str]
    discriminable: set[int]
    conflicts: dict[str, tuple[int, ...]]
    conflict_pattern: re.Pattern[str] | None
    alias_revision: str


def _alternation(aliases: list[str]) -> re.Pattern[str]:
    ordered = sorted(aliases, key=lambda a: (-len(a), a))   # deterministic tie order
    return re.compile(r"\b(" + "|".join(re.escape(a) for a in ordered) + r")\b")


def build_matcher_full(conn: sqlite3.Connection) -> Matcher:
    """One compiled alternation over every unambiguous alias, longest-first.
    Normalized aliases that map to more than one entity are NOT last-row-wins:
    they are compiled separately so a record touching one yields an explicit
    ambiguity result."""
    am = graph_policy.load_alias_map(conn)
    amap = {a: eid for a, eid in am.mapping.items() if len(a) >= MIN_ALIAS}
    conflicts = {a: eids for a, eids in am.conflicts.items() if len(a) >= MIN_ALIAS}
    pattern = _alternation(list(amap)) if amap else re.compile(r"(?!x)x")
    ambiguous = {a for a in amap if a in AMBIGUOUS}
    discriminable = {eid for al, eid in amap.items() if al not in ambiguous}
    cpat = _alternation(list(conflicts)) if conflicts else None
    return Matcher(pattern, amap, ambiguous, discriminable, conflicts, cpat, am.revision)


def build_matcher(conn: sqlite3.Connection) -> tuple[re.Pattern[str], dict[str, int], set[str]]:
    """Legacy 3-tuple contract used by sem_artifacts.py. Conflicting aliases are
    excluded from the alternation and reported on stderr (never silently
    resolved to one entity)."""
    m = build_matcher_full(conn)
    if m.conflicts:
        sys.stderr.write(f"[matcher] {len(m.conflicts)} ambiguous alias(es) excluded: "
                         f"{', '.join(sorted(m.conflicts))}\n")
    return m.pattern, m.amap, m.ambiguous


def hits_for(text: str, pattern: re.Pattern[str], amap: dict[str, int],
             ambiguous: set[str], discriminable: set[int]) -> dict[int, float]:
    """Entity -> weight for one record. Longer, rarer aliases weigh more.

    `discriminable` is the set of entities that own at least one NON-ambiguous
    alias. For those, an ambiguous common word alone is not evidence — we expect
    the better name to appear. For an entity whose only name IS an ambiguous word
    (Atlas, Compass), dropping it would mean it can never link at all, so the
    match counts at a reduced weight instead.
    """
    found = pattern.findall(text.lower())
    if not found:
        return {}
    per_alias: dict[str, int] = {}
    for a in found:
        per_alias[a] = per_alias.get(a, 0) + 1
    strong_eids = {amap[a] for a in per_alias if a not in ambiguous}
    out: dict[int, float] = {}
    for alias, n in per_alias.items():
        eid = amap[alias]
        penalty = 1.0
        if alias in ambiguous and eid not in strong_eids:
            if eid in discriminable:
                continue  # a better name exists and did not appear — not a mention
            penalty = 0.6  # ambiguous is all this entity has; count it, weakly
        w = min(1.0, 0.35 + 0.12 * len(alias.split()) + 0.08 * min(n, 5)) * penalty
        out[eid] = max(out.get(eid, 0.0), round(w, 3))
    return out


# --------------------------------------------------------------------------- #
# source records
# --------------------------------------------------------------------------- #
class Raw(NamedTuple):
    ref: str
    ref_num: int | None
    identity_guard: str
    content_sha256: str | None
    text: str | None
    inactive: bool
    unsupported: str | None      # reason code when the record cannot be processed
    byte_len: int


def _blob(v) -> bytes:
    if v is None:
        return b""
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    return str(v).encode("utf-8")


def _text_fields(blobs: list[bytes], types: list[str]) -> tuple[str | None, str | None, int]:
    """(joined text, unsupported reason, byte length) for selected TEXT fields."""
    total = sum(len(b) for b in blobs)
    if any(t not in ("text", "null") for t in types):
        return None, "shape", total
    if any(len(b) > MAX_RECORD_BYTES for b in blobs):
        return None, "oversized", total
    try:
        parts = [b.decode("utf-8") for b in blobs]
    except UnicodeDecodeError:
        return None, "invalid_utf8", total
    return " ".join(parts), None, total


class Adapter:
    store = ""
    numeric = True
    path = ""

    def open(self) -> None: ...
    def close(self) -> None: ...
    def identity(self) -> str: ...
    def max_key(self) -> str: ...
    def fetch_batch(self, after: str, upto: str, limit: int, excluded: frozenset) -> tuple[list[Raw], list[Raw]]: ...
    def fetch_by_keys(self, keys: list[str], identity_only: bool = False) -> dict[str, Raw]: ...
    def keys_upto(self, upto: str) -> set[str]: ...
    def count_beyond(self, upto: str) -> int: ...


class HivemindAdapter(Adapter):
    store, numeric = "hivemind", True
    FIELDS = ("rule", "context")

    def __init__(self, path: str) -> None:
        self.path, self.conn = path, None

    def open(self) -> None:
        self.conn = graph_policy.open_ro(self.path)
        cols = [r[1].decode() if isinstance(r[1], bytes) else r[1]
                for r in self.conn.execute("PRAGMA table_info(learnings)")]
        for need in ("id", "active", "rule", "context", "created_at"):
            if need not in cols:
                raise sqlite3.OperationalError(f"learnings lacks column {need}")

    def close(self) -> None:
        if self.conn:
            self.conn.close()

    def identity(self) -> str:
        row = self.conn.execute("SELECT id, CAST(COALESCE(created_at,'') AS BLOB) FROM learnings ORDER BY id LIMIT 1").fetchone()
        anchor = b"empty" if row is None else str(row[0]).encode() + b"|" + _blob(row[1])
        return graph_policy.ld_sha256(b"hivemind", b"learnings", anchor)

    def max_key(self) -> str:
        row = self.conn.execute("SELECT COALESCE(max(id), 0) FROM learnings").fetchone()
        return str(row[0])

    def _rows(self, where: str, params: tuple) -> list[Raw]:
        sql = ("SELECT id, active, typeof(rule), typeof(context), CAST(COALESCE(created_at,'') AS BLOB), "
               "substr(CAST(COALESCE(rule,'') AS BLOB),1,?), substr(CAST(COALESCE(context,'') AS BLOB),1,?) "
               f"FROM learnings {where}")
        out = []
        for rid, active, t_rule, t_ctx, created, rule_b, ctx_b in self.conn.execute(
                sql, (MAX_RECORD_BYTES + 1, MAX_RECORD_BYTES + 1) + params):
            guard = graph_policy.ld_sha256(b"hivemind", str(rid).encode(), _blob(created))
            text, unsupported, blen = _text_fields([_blob(rule_b), _blob(ctx_b)],
                                                   [_blob(t_rule).decode(), _blob(t_ctx).decode()])
            chash = None
            if unsupported is None:
                chash = graph_policy.ld_sha256(_blob(rule_b), _blob(ctx_b))
            out.append(Raw(str(rid), rid, guard, chash, text, active != 1, unsupported, blen))
        return out

    def fetch_batch(self, after: str, upto: str, limit: int, excluded: frozenset) -> tuple[list[Raw], list[Raw]]:
        rows = self._rows("WHERE id>? AND id<=? ORDER BY id LIMIT ?", (int(after or 0), int(upto), limit))
        return rows, []

    def fetch_by_keys(self, keys: list[str], identity_only: bool = False) -> dict[str, Raw]:
        out: dict[str, Raw] = {}
        for i in range(0, len(keys), 400):
            chunk = [int(k) for k in keys[i:i + 400]]
            for r in self._rows(f"WHERE id IN ({','.join('?' * len(chunk))}) ORDER BY id", tuple(chunk)):
                out[r.ref] = r
        return out

    def keys_upto(self, upto: str) -> set[str]:
        return {str(r[0]) for r in self.conn.execute("SELECT id FROM learnings WHERE id<=?", (int(upto),))}

    def count_beyond(self, upto: str) -> int:
        return self.conn.execute("SELECT count(*) FROM learnings WHERE id>?", (int(upto),)).fetchone()[0]


class ClaudememAdapter(Adapter):
    store, numeric = "claudemem", True
    FIELDS = ("title", "text", "facts", "narrative", "concepts")

    def __init__(self, path: str) -> None:
        self.path, self.conn = path, None

    def open(self) -> None:
        self.conn = graph_policy.open_ro(self.path)
        cols = [r[1].decode() if isinstance(r[1], bytes) else r[1]
                for r in self.conn.execute("PRAGMA table_info(observations)")]
        for need in ("id", "created_at_epoch", "memory_session_id") + self.FIELDS:
            if need not in cols:
                raise sqlite3.OperationalError(f"observations lacks column {need}")

    def close(self) -> None:
        if self.conn:
            self.conn.close()

    def identity(self) -> str:
        row = self.conn.execute(
            "SELECT id, CAST(COALESCE(created_at_epoch,'') AS BLOB), CAST(COALESCE(memory_session_id,'') AS BLOB) "
            "FROM observations ORDER BY id LIMIT 1").fetchone()
        anchor = b"empty" if row is None else str(row[0]).encode() + b"|" + _blob(row[1]) + b"|" + _blob(row[2])
        return graph_policy.ld_sha256(b"claudemem", b"observations", anchor)

    def max_key(self) -> str:
        return str(self.conn.execute("SELECT COALESCE(max(id), 0) FROM observations").fetchone()[0])

    @staticmethod
    def _guard(oid: int, epoch, session) -> str:
        return graph_policy.ld_sha256(b"claudemem", str(oid).encode(), _blob(epoch), _blob(session))

    def _rows(self, where: str, params: tuple) -> list[Raw]:
        f = self.FIELDS
        sql = ("SELECT id, CAST(COALESCE(created_at_epoch,'') AS BLOB), CAST(COALESCE(memory_session_id,'') AS BLOB), "
               + ", ".join(f"typeof({c})" for c in f) + ", "
               + ", ".join(f"substr(CAST(COALESCE({c},'') AS BLOB),1,?)" for c in f)
               + f" FROM observations {where}")
        out = []
        for row in self.conn.execute(sql, (MAX_RECORD_BYTES + 1,) * len(f) + params):
            oid, epoch, session = row[0], row[1], row[2]
            types = [_blob(t).decode() for t in row[3:3 + len(f)]]
            blobs = [_blob(b) for b in row[3 + len(f):3 + 2 * len(f)]]
            text, unsupported, blen = _text_fields(blobs, types)
            chash = graph_policy.ld_sha256(*blobs) if unsupported is None else None
            out.append(Raw(str(oid), oid, self._guard(oid, epoch, session), chash, text, False, unsupported, blen))
        return out

    def fetch_batch(self, after: str, upto: str, limit: int, excluded: frozenset) -> tuple[list[Raw], list[Raw]]:
        """Keyset page with the exclusion set applied IN SQL: the range is walked in
        sub-ranges of at most SQL_EXCLUDE_CHUNK excluded ids each, so excluded
        content is never transferred, whatever the size of the overlay."""
        lo, hi = int(after or 0), int(upto)
        ex = sorted(int(x) for x in excluded if lo < int(x) <= hi)
        rows: list[Raw] = []
        start, i = lo, 0
        while len(rows) < limit and start < hi:
            chunk = ex[i:i + SQL_EXCLUDE_CHUNK]
            sub_hi = chunk[-1] if (chunk and i + SQL_EXCLUDE_CHUNK < len(ex)) else hi
            where = "WHERE id>? AND id<=?"
            params: tuple = (start, sub_hi)
            if chunk:
                where += f" AND id NOT IN ({','.join('?' * len(chunk))})"
                params += tuple(chunk)
            rows.extend(self._rows(where + " ORDER BY id LIMIT ?", params + (limit - len(rows),)))
            start, i = sub_hi, i + SQL_EXCLUDE_CHUNK
        last = rows[-1].ref_num if len(rows) == limit else hi
        ex_in = [x for x in ex if lo < x <= last]
        return rows, self.fetch_identity(ex_in)

    def fetch_identity(self, keys: list[int]) -> list[Raw]:
        """Identity guards only — content of excluded records is never read."""
        out: list[Raw] = []
        for i in range(0, len(keys), 400):
            chunk = keys[i:i + 400]
            for oid, epoch, session in self.conn.execute(
                    "SELECT id, CAST(COALESCE(created_at_epoch,'') AS BLOB), CAST(COALESCE(memory_session_id,'') AS BLOB) "
                    f"FROM observations WHERE id IN ({','.join('?' * len(chunk))}) ORDER BY id", chunk):
                out.append(Raw(str(oid), oid, self._guard(oid, epoch, session), None, None, False, None, 0))
        return out

    def fetch_by_keys(self, keys: list[str], identity_only: bool = False) -> dict[str, Raw]:
        if identity_only:
            return {r.ref: r for r in self.fetch_identity([int(k) for k in keys])}
        out: dict[str, Raw] = {}
        for i in range(0, len(keys), 400):
            chunk = [int(k) for k in keys[i:i + 400]]
            for r in self._rows(f"WHERE id IN ({','.join('?' * len(chunk))}) ORDER BY id", tuple(chunk)):
                out[r.ref] = r
        return out

    def keys_upto(self, upto: str) -> set[str]:
        return {str(r[0]) for r in self.conn.execute("SELECT id FROM observations WHERE id<=?", (int(upto),))}

    def count_beyond(self, upto: str) -> int:
        return self.conn.execute("SELECT count(*) FROM observations WHERE id>?", (int(upto),)).fetchone()[0]


class FileAdapter(Adapter):
    store, numeric = "file", False

    def __init__(self, memdir: str) -> None:
        self.path, self.inventory = memdir, ()
        self.known: set[str] = set()     # names that already have a record state (new lane skips them)

    def open(self) -> None:
        self.inventory = graph_policy.file_inventory(self.path)   # raises OSError

    def close(self) -> None: ...

    def identity(self) -> str:
        return graph_policy.ld_sha256(b"file", self.path.encode())

    def max_key(self) -> str:
        return graph_policy.ld_sha256(*(n.encode() for n in self.inventory))

    def _read(self, name: str) -> Raw:
        guard = graph_policy.ld_sha256(b"file", name.encode())
        p = os.path.join(self.path, name)
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            return Raw(name, None, guard, None, None, True, "absent", 0)
        if not stat.S_ISREG(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
            return Raw(name, None, guard, None, None, False, "not_regular", 0)
        if st.st_size > MAX_FILE_BYTES:
            return Raw(name, None, guard, None, None, False, "oversized", st.st_size)
        fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            if not os.path.samestat(st, os.fstat(fd)) or not os.path.samestat(st, os.lstat(p)):
                return Raw(name, None, guard, None, None, False, "identity_changed", 0)
            body = os.read(fd, MAX_FILE_BYTES + 1)
        finally:
            os.close(fd)
        if len(body) > MAX_FILE_BYTES:
            return Raw(name, None, guard, None, None, False, "oversized", len(body))
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return Raw(name, None, guard, None, None, False, "invalid_utf8", len(body))
        chash = graph_policy.ld_sha256(name.encode(), body)
        # the filename itself is a strong signal about what the memory is for
        return Raw(name, None, guard, chash, f"{name.replace('-', ' ')} {text}", False, None, len(body))

    def fetch_batch(self, after: str, upto: str, limit: int, excluded: frozenset) -> tuple[list[Raw], list[Raw]]:
        names = [n for n in self.inventory if n > (after or "") and n not in self.known][:limit]
        return [self._read(n) for n in names], []

    def fetch_by_keys(self, keys: list[str], identity_only: bool = False) -> dict[str, Raw]:
        out = {}
        for k in keys:
            r = self._read(k)
            if r.unsupported != "absent":
                out[k] = r
        return out

    def keys_upto(self, upto: str) -> set[str]:
        return set(self.inventory)

    def count_beyond(self, upto: str) -> int:
        return 0


def adapters() -> dict[str, Adapter]:
    return {"hivemind": HivemindAdapter(HM), "claudemem": ClaudememAdapter(CM), "file": FileAdapter(MEMDIR)}


def installed_adapters() -> dict[str, Adapter]:
    paths = {"hivemind": HM, "claudemem": CM, "file": MEMDIR}
    return {name: adapter for name, adapter in adapters().items() if os.path.exists(paths[name])}


# --------------------------------------------------------------------------- #
# outcomes
# --------------------------------------------------------------------------- #
class Context(NamedTuple):
    matcher: Matcher
    policy: graph_policy.PolicyOverlay | None     # None => claudemem unavailable
    reader_rev: str
    policy_rev: str

    def revisions(self) -> dict:
        return {"alias": self.matcher.alias_revision, "reader": self.reader_rev, "policy": self.policy_rev}


def outcome(raw: Raw, store: str, epoch: int, ctx: Context, excluded: bool = False) -> graph_state.Outcome:
    m = ctx.matcher
    base = dict(store=store, ref=raw.ref, ref_num=raw.ref_num, identity_guard=raw.identity_guard,
                content_sha256=raw.content_sha256, hits={}, epoch=epoch)
    if excluded:
        return graph_state.Outcome(status="excluded", reason="consolidation_suppressed", **base)
    if raw.inactive:
        return graph_state.Outcome(status="missing_inactive", reason="inactive" if raw.unsupported is None else raw.unsupported, **base)
    if raw.unsupported:
        return graph_state.Outcome(status="unsupported", reason=raw.unsupported, **base)
    text = raw.text or ""
    if m.conflict_pattern is not None and m.conflict_pattern.search(text.lower()):
        return graph_state.Outcome(status="pending", reason="alias_ambiguity", **base)
    hits = hits_for(text, m.pattern, m.amap, m.ambiguous, m.discriminable)
    if hits:
        base["hits"] = hits
        return graph_state.Outcome(status="matched", reason="", **base)
    return graph_state.Outcome(status="unmatched", reason="", **base)


def recheck(outcomes: list[graph_state.Outcome], adapter: Adapter) -> list[graph_state.Outcome]:
    """Fresh read of every record in the batch immediately before publishing. A
    record whose identity or content moved becomes `pending` (retryable); a
    record that vanished while we scanned it becomes pending too — absence is
    only recorded after the reconciliation lane's confirmed re-read."""
    ex_keys = [o.ref for o in outcomes if o.status == "excluded"]
    keys = [o.ref for o in outcomes if o.status != "excluded"]
    fresh = adapter.fetch_by_keys(keys) if keys else {}
    if ex_keys:
        fresh.update(adapter.fetch_by_keys(ex_keys, identity_only=True))
    out = []
    for o in outcomes:
        f = fresh.get(o.ref)
        if o.status == "missing_inactive" and o.reason in ("absent", "absent_confirmed"):
            out.append(o if f is None else dataclasses.replace(o, status="pending", reason="reappeared_during_scan", hits={}))
            continue
        if f is None:
            out.append(dataclasses.replace(o, status="pending", reason="vanished_during_scan", hits={}))
            continue
        if o.status == "identity_conflict":
            out.append(o)                      # already an explicit conflict from the reconciliation read
            continue
        if f.identity_guard != o.identity_guard:
            out.append(dataclasses.replace(o, status="identity_conflict", reason="identity_changed_during_scan", hits={}))
            continue
        if o.status in ("matched", "unmatched", "unsupported") and f.content_sha256 != o.content_sha256:
            out.append(dataclasses.replace(o, status="pending", reason="changed_during_scan", hits={}))
            continue
        if o.status == "missing_inactive" and not f.inactive:
            out.append(dataclasses.replace(o, status="pending", reason="reactivated_during_scan", hits={}))
            continue
        out.append(o)
    return out


# --------------------------------------------------------------------------- #
# lanes
# --------------------------------------------------------------------------- #
class Budget:
    def __init__(self, wall_seconds: float, max_bytes: int) -> None:
        self.t0, self.wall, self.max_bytes, self.bytes = time.monotonic(), wall_seconds, max_bytes, 0

    def exhausted(self) -> str | None:
        if time.monotonic() - self.t0 > self.wall:
            return "wall"
        if self.bytes > self.max_bytes:
            return "bytes"
        return None


class StoreResult(NamedTuple):
    store: str
    state: str          # completed|remaining|unavailable|identity_conflict|failed
    reason: str
    counters: dict


def _count(counters: dict, key: str, n: int = 1) -> None:
    counters[key] = counters.get(key, 0) + n


def _record_outcomes(counters: dict, lane: str, outs: list[graph_state.Outcome]) -> None:
    for o in outs:
        _count(counters, f"{lane}.{o.status}")
        if o.reason:
            _count(counters, f"{lane}.reason.{o.status}:{o.reason}")


def _policy_still(ctx: Context):
    """Fresh in-transaction recheck: the policy AND alias revisions fixed for
    this batch must still hold, or nothing from the batch is published."""
    def check(conn) -> bool:
        try:
            current = graph_policy.load_policy(conn).revision
        except graph_policy.PolicyUnavailable as exc:
            current = "unavailable:" + exc.reason
        if current != ctx.policy_rev:
            return False
        try:
            return graph_policy.load_alias_map(conn).revision == ctx.matcher.alias_revision
        except graph_policy.PolicyUnavailable:
            return False
    return check


def new_lane(sconn, adapter: Adapter, ctx: Context, run_id: str, limit: int, budget: Budget,
             counters: dict, epoch_meta: dict) -> tuple[str, int]:
    """Process up to `limit` new records. Returns (state, processed)."""
    store = adapter.store
    cur = graph_state.get_cursor(sconn, store, "new")
    excluded = ctx.policy.suppressed if (store == "claudemem" and ctx.policy) else frozenset()
    if store == "file":
        adapter.known = {r[0] for r in sconn.execute("SELECT ref FROM graph_record_state WHERE store='file'")}
    if cur is not None and cur.cursor == cur.high_water and cur.last_completed_epoch != cur.epoch:
        _close_new_epoch(sconn, adapter, cur, ctx, run_id, counters)   # an interrupted close
        cur = graph_state.get_cursor(sconn, store, "new")
    if cur is None or cur.cursor == cur.high_water:
        hw = adapter.max_key()
        if cur is not None and cur.high_water == hw:
            counters[f"{store}.new.beyond_high_water"] = adapter.count_beyond(hw)
            return "completed", 0
        if adapter.numeric and cur is not None and cur.high_water and int(hw) < int(cur.high_water):
            return "identity_conflict", 0
        boundary = json.dumps(list(adapter.inventory)) if store == "file" else None
        start = (cur.high_water if (cur is not None and adapter.numeric) else ("0" if adapter.numeric else ""))
        cur = graph_state.start_epoch(sconn, store, "new", hw, boundary, epoch_meta["identity"],
                                      ctx.revisions(), start)
        _count(counters, f"{store}.new.epochs_started")
    processed = 0
    while processed < limit:
        why = budget.exhausted()
        if why:
            _count(counters, f"budget.{why}")
            return "remaining", processed
        if cur.cursor == cur.high_water:
            break
        n = min(limit - processed, 100)
        rows, ex_rows = adapter.fetch_batch(cur.cursor or "", cur.high_water, n, excluded)
        budget.bytes += sum(r.byte_len for r in rows)
        if adapter.numeric:
            last = str(rows[-1].ref_num) if len(rows) == n else cur.high_water
        else:
            last = rows[-1].ref if len(rows) == n else cur.high_water
            if not rows:
                last = cur.high_water
        outs = [outcome(r, store, cur.epoch, ctx) for r in rows]
        outs += [outcome(r, store, cur.epoch, ctx, excluded=True) for r in ex_rows]
        outs = recheck(outs, adapter)
        _record_outcomes(counters, f"{store}.new", outs)
        graph_state.publish(sconn, cur, last, outs, run_id, ctx.revisions(), counters,
                            _policy_still(ctx))
        processed += len(outs)
        cur = graph_state.get_cursor(sconn, store, "new")
    if cur.cursor == cur.high_water:
        _close_new_epoch(sconn, adapter, cur, ctx, run_id, counters)
        counters[f"{store}.new.beyond_high_water"] = adapter.count_beyond(cur.high_water)
        return ("remaining" if counters[f"{store}.new.beyond_high_water"] else "completed"), processed
    return "remaining", processed


def _close_new_epoch(sconn, adapter: Adapter, cur, ctx: Context, run_id: str, counters: dict) -> None:
    """Reconcile membership at the fixed boundary; queue gaps as pending."""
    store = adapter.store
    expected = adapter.keys_upto(cur.high_water)
    if adapter.numeric:
        covered = {str(r[0]) for r in sconn.execute(
            "SELECT ref_num FROM graph_record_state WHERE store=? AND ref_num<=?", (store, int(cur.high_water)))}
    else:
        covered = {r[0] for r in sconn.execute("SELECT ref FROM graph_record_state WHERE store=?", (store,))}
    gaps = sorted(expected - covered, key=lambda k: (len(k), k))
    queued = 0
    if gaps:
        excluded = ctx.policy.suppressed if (store == "claudemem" and ctx.policy) else frozenset()
        head = gaps[:GAP_ENQUEUE_LIMIT]
        ex_keys = [g for g in head if store == "claudemem" and int(g) in excluded]
        ok_keys = [g for g in head if g not in set(ex_keys)]
        fresh = adapter.fetch_by_keys(ok_keys) if ok_keys else {}
        outs = [graph_state.Outcome(store, r.ref, r.ref_num, r.identity_guard, None, "pending",
                                    "membership_gap", {}, cur.epoch) for r in fresh.values()]
        if ex_keys:                                       # decision known: identity only, content never read
            for r in adapter.fetch_by_keys(ex_keys, identity_only=True).values():
                outs.append(graph_state.Outcome(store, r.ref, r.ref_num, r.identity_guard, None, "excluded",
                                                "consolidation_suppressed", {}, cur.epoch))
        if outs:
            graph_state.publish(sconn, cur, cur.cursor, outs, run_id, ctx.revisions(), counters,
                                lambda c: True)
            queued = len(outs)
            cur = graph_state.get_cursor(sconn, store, "new")
    completion = {"expected": len(expected), "covered": len(expected & covered), "gap_count": len(gaps),
                  "gap_sample": gaps[:20], "gaps_queued_pending": queued, "run_id": run_id}
    graph_state.close_epoch(sconn, cur, completion)
    counters[f"{store}.new.epoch_completed"] = cur.epoch
    counters[f"{store}.new.membership_gap"] = len(gaps)


def reconcile_lane(sconn, adapter: Adapter, ctx: Context, run_id: str, limit: int, budget: Budget,
                   counters: dict, epoch_meta: dict) -> tuple[str, int]:
    """Rotating re-read of records already covered by the new lane."""
    store = adapter.store
    new_cur = graph_state.get_cursor(sconn, store, "new")
    if new_cur is None or not new_cur.cursor or new_cur.cursor in ("0", ""):
        return "completed", 0
    cur = graph_state.get_cursor(sconn, store, "reconcile")
    if cur is None or cur.cursor == cur.high_water:
        hw = new_cur.cursor if adapter.numeric else "~"     # '~' sorts after any file name
        cur = graph_state.start_epoch(sconn, store, "reconcile", hw, None, epoch_meta["identity"],
                                      ctx.revisions(), "0" if adapter.numeric else "")
        _count(counters, f"{store}.reconcile.epochs_started")
    processed = 0
    # retryable rows first (bounded), without moving the cursor
    pend = graph_state.pending_rows(sconn, store, min(limit, PENDING_FIRST))
    if pend:
        outs = _reconcile_rows(adapter, pend, cur.epoch, ctx, budget)
        _record_outcomes(counters, f"{store}.reconcile.pending", outs)
        graph_state.publish(sconn, cur, cur.cursor, outs, run_id, ctx.revisions(), counters,
                            _policy_still(ctx))
        processed += len(outs)
        cur = graph_state.get_cursor(sconn, store, "reconcile")
    while processed < limit and cur.cursor != cur.high_water:
        why = budget.exhausted()
        if why:
            _count(counters, f"budget.{why}")
            return "remaining", processed
        n = min(limit - processed, 100)
        rows = graph_state.state_rows(sconn, store, cur.cursor, cur.high_water, n, adapter.numeric)
        if not rows:
            last = cur.high_water
            outs: list[graph_state.Outcome] = []
        else:
            last = (str(rows[-1][1]) if adapter.numeric else rows[-1][0]) if len(rows) == n else cur.high_water
            outs = _reconcile_rows(adapter, rows, cur.epoch, ctx, budget)
        _record_outcomes(counters, f"{store}.reconcile", outs)
        graph_state.publish(sconn, cur, last, outs, run_id, ctx.revisions(), counters,
                            _policy_still(ctx))
        processed += len(outs)
        cur = graph_state.get_cursor(sconn, store, "reconcile")
    if cur.cursor == cur.high_water:
        graph_state.close_epoch(sconn, cur, {"run_id": run_id, "high_water": cur.high_water})
        counters[f"{store}.reconcile.epoch_completed"] = cur.epoch
        return "completed", processed
    return "remaining", processed


def _reconcile_rows(adapter: Adapter, rows: list, epoch: int, ctx: Context, budget: Budget) -> list:
    """State-driven re-read: detects edits, deactivation and (confirmed) absence."""
    store = adapter.store
    excluded = ctx.policy.suppressed if (store == "claudemem" and ctx.policy) else frozenset()
    ex_keys = [r[0] for r in rows if store == "claudemem" and r[1] in excluded]
    keys = [r[0] for r in rows if r[0] not in set(ex_keys)]
    fresh = adapter.fetch_by_keys(keys) if keys else {}
    if ex_keys:
        fresh.update(adapter.fetch_by_keys(ex_keys, identity_only=True))
    absent = [r[0] for r in rows if r[0] not in fresh]
    if absent:                                       # confirm absence with a second read
        fresh.update(adapter.fetch_by_keys([k for k in absent if k not in set(ex_keys)]))
        fresh.update(adapter.fetch_by_keys([k for k in absent if k in set(ex_keys)], identity_only=True))
    outs = []
    for ref, ref_num, guard, _chash, _status, _arev, _rrev in rows:
        raw = fresh.get(ref)
        if raw is None:
            outs.append(graph_state.Outcome(store, ref, ref_num, guard, None, "missing_inactive",
                                            "absent_confirmed", {}, epoch))
            continue
        budget.bytes += raw.byte_len
        if raw.identity_guard != guard:
            outs.append(graph_state.Outcome(store, ref, ref_num, guard, raw.content_sha256, "identity_conflict",
                                            "identity_guard_mismatch", {}, epoch))
            continue
        is_ex = store == "claudemem" and raw.ref_num in excluded
        outs.append(outcome(raw, store, epoch, ctx, excluded=is_ex))
    return recheck(outs, adapter)


def process_store(sconn, adapter: Adapter, ctx: Context, run_id: str, limit: int, budget: Budget,
                  counters: dict) -> StoreResult:
    store = adapter.store
    if store == "claudemem" and ctx.policy is None:
        return StoreResult(store, "unavailable", "policy_overlay_unavailable", {})
    try:
        adapter.open()
    except (sqlite3.Error, OSError) as exc:
        return StoreResult(store, "unavailable", f"source:{type(exc).__name__}", {})
    try:
        identity = adapter.identity()
        for phase in ("new", "reconcile"):
            cur = graph_state.get_cursor(sconn, store, phase)
            if cur is not None and cur.source_identity and cur.source_identity != identity:
                counters[f"{store}.identity_conflict"] = 1
                return StoreResult(store, "identity_conflict", "source_identity_changed", {})
        meta = {"identity": identity}
        n_new = math.ceil(limit * NEW_LANE_SHARE)
        st_new, used = new_lane(sconn, adapter, ctx, run_id, n_new, budget, counters, meta)
        if st_new == "identity_conflict":
            counters[f"{store}.identity_conflict"] = 1
            return StoreResult(store, "identity_conflict", "high_water_regressed", {})
        st_rec, used_rec = reconcile_lane(sconn, adapter, ctx, run_id, limit - used, budget, counters, meta)
        if st_rec == "completed" and st_new == "completed" and used + used_rec < limit and budget.exhausted() is None:
            # spare budget: let the new lane use it (it may have been short-changed by the split)
            st_new, more = new_lane(sconn, adapter, ctx, run_id, limit - used - used_rec, budget, counters, meta)
            used += more
        # `completed` = the new-record epoch is closed at its boundary, nothing lies beyond it and no
        # record is left pending. The reconciliation lane is a perpetual rotation: its position is
        # reported, it never by itself claims completion or hides remaining new work.
        pending = sconn.execute(
            "SELECT count(*) FROM graph_record_state WHERE store=? AND status='pending'", (store,)).fetchone()[0]
        rc = graph_state.get_cursor(sconn, store, "reconcile")
        counters[f"{store}.pending_rows"] = pending
        counters[f"{store}.reconcile.position"] = (
            f"epoch {rc.epoch} at {str(rc.cursor)[:16]} of {str(rc.high_water)[:16]} ({st_rec})" if rc else "none")
        state = "completed" if (st_new == "completed" and pending == 0) else "remaining"
        counters[f"{store}.processed"] = used + used_rec
        return StoreResult(store, state, "" if state == "completed" else
                           ("pending_rows" if st_new == "completed" else "new_records_remaining"), {})
    except graph_state.PolicyChanged as exc:
        return StoreResult(store, "failed", f"policy_changed:{exc}", {})
    except graph_state.CursorConflict as exc:
        return StoreResult(store, "failed", f"cursor_conflict:{exc}", {})
    except sqlite3.Error as exc:
        return StoreResult(store, "failed", _sqlite_reason(exc), {})
    finally:
        adapter.close()


# --------------------------------------------------------------------------- #
# dry run / preflight
# --------------------------------------------------------------------------- #
def dry_run(sconn, ctx: Context, limits: dict) -> dict:
    """Preview the next new-lane batch per store. Opens no writer, touches no
    cursor, initializes nothing."""
    tables = graph_policy.tables_present(sconn) == set(graph_policy.CONTROL_TABLES)
    report: dict = {"mode": "dry-run", "tables_present": tables, "stores": {}, "scope": _scope_status(sconn, tables)}
    for store, adapter in adapters().items():
        entry: dict = {}
        if store == "claudemem" and ctx.policy is None:
            report["stores"][store] = {"state": "unavailable", "reason": "policy_overlay_unavailable"}
            continue
        try:
            adapter.open()
        except (sqlite3.Error, OSError) as exc:
            report["stores"][store] = {"state": "unavailable", "reason": f"source:{type(exc).__name__}"}
            continue
        try:
            cur = graph_state.get_cursor(sconn, store, "new") if tables else None
            if cur and cur.cursor != cur.high_water:          # an open epoch: preview its next batch
                after, hw = cur.cursor, cur.high_water
            elif cur and adapter.numeric:                     # closed epoch: preview from its boundary
                after, hw = cur.high_water, adapter.max_key()
            else:
                after, hw = ("0" if adapter.numeric else ""), adapter.max_key()
            excluded = ctx.policy.suppressed if (store == "claudemem" and ctx.policy) else frozenset()
            if store == "file" and tables:
                adapter.known = {r[0] for r in sconn.execute("SELECT ref FROM graph_record_state WHERE store='file'")}
            rows, ex_rows = adapter.fetch_batch(after or "", hw, limits[store], excluded)
            outs = [outcome(r, store, 0, ctx) for r in rows] + [outcome(r, store, 0, ctx, True) for r in ex_rows]
            for o in outs:
                entry[o.status] = entry.get(o.status, 0) + 1
            entry["would_write_links"] = sum(len(o.hits) for o in outs)
            entry["cursor"] = after
            entry["high_water"] = hw
            entry["beyond_high_water"] = adapter.count_beyond(hw)
            entry["state"] = "preview"
        finally:
            adapter.close()
        report["stores"][store] = entry
    return report


def _scope_status(sconn, tables_present: bool) -> dict:
    """control vs current source-scope digest (read-only)."""
    current = graph_policy.source_scope_sha256()
    if not tables_present:
        return {"control": None, "current": current, "match": None}
    try:
        control = graph_policy.load_control(sconn).source_scope_sha256
    except graph_policy.GraphUnavailable as exc:
        return {"control": None, "current": current, "match": None, "error": exc.reason}
    return {"control": control, "current": current, "match": control == current}


def scope_line(scope: dict) -> str:
    c = (scope.get("control") or "absent")[:16]
    return f"scope: control={c} current={scope['current'][:16]} match={'true' if scope.get('match') else 'false'}"


def preflight() -> tuple[int, dict]:
    out: dict = {"semantic_db": "ok", "tables": "absent", "policy": "ok", "sources": {}}
    code = EXIT_OK
    try:
        sconn = sem.connect_ro()
    except sqlite3.Error as exc:
        return EXIT_FAILED, {"semantic_db": f"unavailable:{type(exc).__name__}"}
    try:
        present = graph_policy.tables_present(sconn)
        if present == set(graph_policy.CONTROL_TABLES):
            out["tables"] = "present"
            out["mode"] = graph_policy.load_control(sconn).mode
            out["scope"] = _scope_status(sconn, True)
            if out["scope"]["match"] is False:
                code = EXIT_SCOPE
        elif present:
            out["tables"] = "partial"
            code = EXIT_FAILED
        if os.path.exists(CM):
            try:
                graph_policy.load_policy(sconn)
            except graph_policy.PolicyUnavailable as exc:
                out["policy"] = f"unavailable:{exc.reason}"
                code = EXIT_UNAVAILABLE
        else:
            out["policy"] = "not installed"
    except graph_policy.GraphUnavailable as exc:
        out["tables"] = f"unavailable:{exc.reason}"
        code = EXIT_FAILED
    finally:
        sconn.close()
    for store, adapter in installed_adapters().items():
        try:
            adapter.open()
            adapter.close()
            out["sources"][store] = "ok"
        except (sqlite3.Error, OSError) as exc:
            out["sources"][store] = f"unavailable:{type(exc).__name__}"
            code = max(code, EXIT_UNAVAILABLE) if code != EXIT_FAILED else code
    return code, out


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--migrate", action="store_true", help="apply the additive graph migration explicitly")
    ap.add_argument("--preflight", action="store_true", help="check inputs; no writes")
    ap.add_argument("--accept-scope", metavar="SHA256", default=None,
                    help="re-approve a changed source scope (must equal the current digest); runs no batch")
    ap.add_argument("--reset", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--budget-obs", type=int, default=DEFAULT_BUDGET_OBS)
    ap.add_argument("--budget-hm", type=int, default=DEFAULT_BUDGET_HM)
    ap.add_argument("--budget-files", type=int, default=DEFAULT_BUDGET_FILES)
    ap.add_argument("--limit-obs", type=int, default=None,
                    help="DEPRECATED: per-invocation observation budget alias for --budget-obs")
    ap.add_argument("--wall-seconds", type=float, default=DEFAULT_WALL_SECONDS)
    ap.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    ap.add_argument("--entry", choices=("direct", "scheduled", "fixture"), default="direct")
    a = ap.parse_args(argv)
    if a.reset:
        ap.error("--reset is rejected: legacy mention rows are preserved; owned rows are reconciled, not reset")
    if a.accept_scope is not None and (a.dry_run or a.preflight or a.migrate):
        ap.error("--accept-scope cannot be combined with --dry-run, --preflight or --migrate")
    if a.limit_obs is not None:
        sys.stderr.write("[deprecated] --limit-obs is a per-invocation budget alias for --budget-obs, "
                         "not a corpus ceiling\n")
        a.budget_obs = a.limit_obs
    for name in ("budget_obs", "budget_hm", "budget_files"):
        if getattr(a, name) <= 0:
            ap.error(f"--{name.replace('_', '-')} must be positive")
    return a


def _context(sconn) -> Context:
    matcher = build_matcher_full(sconn)
    try:
        policy = graph_policy.load_policy(sconn)
        prev = policy.revision
    except graph_policy.PolicyUnavailable as exc:
        policy, prev = None, "unavailable:" + exc.reason
    return Context(matcher, policy, graph_policy.reader_revision(), prev)


def _sqlite_reason(exc: Exception) -> str:
    text = str(exc).lower()
    if "locked" in text or "busy" in text:
        return "sqlite_locked"
    return f"sqlite:{type(exc).__name__}"


def _overall(results: list[StoreResult]) -> str:
    states = {r.state for r in results}
    if "failed" in states:
        return "failed"
    if "unavailable" in states or "identity_conflict" in states:
        return "unavailable"
    if "remaining" in states:
        return "remaining"
    return "completed"


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    if not a.dry_run:
        conn = sem.connect()
        conn.close()
    if a.preflight:
        code, info = preflight()
        if "scope" in info:
            print(scope_line(info["scope"]))
        print("PREFLIGHT " + json.dumps(info, sort_keys=True))
        return code
    limits = {"claudemem": a.budget_obs, "hivemind": a.budget_hm, "file": a.budget_files}

    if a.dry_run:
        try:
            sconn = sem.connect_ro()
        except sqlite3.Error as exc:
            print(f"RESULT unavailable {json.dumps({'reason': 'semantic_db:' + type(exc).__name__})}")
            return EXIT_UNAVAILABLE
        try:
            ctx = _context(sconn)
            print(f"matcher: {len(ctx.matcher.amap)} aliases ({len(ctx.matcher.ambiguous)} ambiguous, "
                  f"{len(ctx.matcher.discriminable)} entities have a discriminating name, "
                  f"{len(ctx.matcher.conflicts)} conflicting)")
            report = dry_run(sconn, ctx, limits)
        finally:
            sconn.close()
        print(scope_line(report["scope"]))
        print("RESULT preview " + json.dumps(report, sort_keys=True))
        return EXIT_SCOPE if report["scope"].get("match") is False else EXIT_OK

    if not os.path.isfile(sem.DB):
        # never create a database (or its lock file) at a stray path
        print("RESULT failed " + json.dumps({"reason": "semantic_db_missing", "path": sem.DB}))
        return EXIT_FAILED
    try:
        lock = graph_state.WriterLock(sem.DB).__enter__()
    except graph_state.LockHeld as exc:
        print(f"RESULT locked {json.dumps({'reason': str(exc)})}")
        return EXIT_LOCKED
    try:
        if a.accept_scope is not None:
            return _accept_scope(a.accept_scope)
        return _run_locked(a, limits)
    finally:
        lock.__exit__(None, None, None)


def _accept_scope(argument: str) -> int:
    """The reviewed control path for a changed source scope. No batch runs here."""
    current = graph_policy.source_scope_sha256()
    if argument != current:
        print("RESULT failed " + json.dumps({"reason": "scope_accept_mismatch", "argument": argument[:16],
                                             "current": current}, sort_keys=True))
        return EXIT_FAILED
    try:
        sconn = graph_state.writer_conn(sem.DB)
    except sqlite3.OperationalError as exc:
        print("RESULT failed " + json.dumps({"reason": _sqlite_reason(exc), "phase": "open"}))
        return EXIT_FAILED
    try:
        if graph_policy.tables_present(sconn) != set(graph_policy.CONTROL_TABLES):
            print("RESULT no_tables " + json.dumps({"reason": "graph tables absent; run sem_backfill.py --migrate"}))
            return EXIT_NO_TABLES
        control = graph_policy.load_control(sconn)
        if control.source_scope_sha256 == current:
            print("RESULT failed " + json.dumps({"reason": "scope_accept_unchanged", "current": current}))
            return EXIT_FAILED
        try:
            act = graph_state.accept_scope(sconn, control.source_scope_sha256, current)
        except (sqlite3.OperationalError, graph_state.StateError) as exc:
            reason = _sqlite_reason(exc) if isinstance(exc, sqlite3.OperationalError) else f"state:{exc}"
            print("RESULT failed " + json.dumps({"reason": reason, "phase": "accept_scope"}))
            return EXIT_FAILED
        print("RESULT scope_accepted " + json.dumps({"previous": control.source_scope_sha256, "current": current,
                                                     "activation_revision": act, "mode": control.mode}, sort_keys=True))
        return EXIT_OK
    except graph_policy.GraphUnavailable as exc:
        print("RESULT failed " + json.dumps({"reason": f"control:{exc.reason}"}))
        return EXIT_FAILED
    finally:
        sconn.close()


def _run_locked(a: argparse.Namespace, limits: dict) -> int:
    try:
        sconn = graph_state.writer_conn(sem.DB)
    except sqlite3.OperationalError as exc:
        print("RESULT failed " + json.dumps({"reason": _sqlite_reason(exc), "phase": "open"}))
        return EXIT_FAILED
    try:
        # setup writes (migration, interrupted-run marking, run row) can hit
        # another writer's BEGIN IMMEDIATE: report, never trace back
        try:
            if a.migrate or not graph_policy.tables_present(sconn):
                info = graph_state.migrate(sconn, graph_policy.source_scope_sha256())
                print("MIGRATED " + json.dumps(info, sort_keys=True))
            if graph_policy.tables_present(sconn) != set(graph_policy.CONTROL_TABLES):
                print("RESULT no_tables " + json.dumps({"reason": "graph tables absent; run sem_backfill.py --migrate"}))
                return EXIT_NO_TABLES
            control = graph_policy.load_control(sconn)
            scope = graph_policy.source_scope_sha256()
            if control.source_scope_sha256 != scope:
                print("RESULT failed " + json.dumps({"reason": "source_scope_changed",
                                                     "control": control.source_scope_sha256, "current": scope}))
                return EXIT_FAILED
            ctx = _context(sconn)
            interrupted = graph_state.mark_interrupted_runs(sconn)
            print(f"matcher: {len(ctx.matcher.amap)} aliases ({len(ctx.matcher.ambiguous)} ambiguous, "
                  f"{len(ctx.matcher.discriminable)} entities have a discriminating name, "
                  f"{len(ctx.matcher.conflicts)} conflicting); mode={control.mode}; "
                  f"interrupted runs marked={interrupted}")
            run_id = graph_state.start_run(sconn, a.entry, control.mode, {}, ctx.reader_rev,
                                           ctx.matcher.alias_revision, ctx.policy_rev)
        except sqlite3.OperationalError as exc:
            print("RESULT failed " + json.dumps({"reason": _sqlite_reason(exc), "phase": "setup"}))
            return EXIT_FAILED
        budget = Budget(a.wall_seconds, a.max_bytes)
        counters: dict = {"alias_conflicts": len(ctx.matcher.conflicts)}
        results = []
        for store, adapter in installed_adapters().items():
            results.append(process_store(sconn, adapter, ctx, run_id, limits[store], budget, counters))
        overall = _overall(results)
        counters["elapsed_seconds"] = round(time.monotonic() - budget.t0, 3)
        counters["bytes_read"] = budget.bytes
        stores = {r.store: {"state": r.state, "reason": r.reason} for r in results}
        error_code = None
        if overall not in ("completed", "remaining"):
            error_code = next((r.reason for r in results if r.reason == "sqlite_locked"), overall)
        try:
            graph_state.finish_run(sconn, run_id, overall, {**counters, "stores": stores}, error_code)
            owned = sconn.execute("SELECT count(*) FROM graph_auto_mention").fetchone()[0]
        except sqlite3.OperationalError as exc:
            print("RESULT failed " + json.dumps({"reason": _sqlite_reason(exc), "phase": "finish_run",
                                                 "run_id": run_id, "stores": stores}, sort_keys=True))
            return EXIT_FAILED
        summary = {"run_id": run_id, "mode": control.mode, "stores": stores, "counters": counters,
                   "owned_links_total": owned, "reader_revision": ctx.reader_rev[:12],
                   "alias_revision": ctx.matcher.alias_revision[:12], "policy_revision": ctx.policy_rev[:12]}
        print(f"RESULT {overall} " + json.dumps(summary, sort_keys=True))
        if overall == "failed":
            return EXIT_FAILED
        if overall == "unavailable":
            return EXIT_UNAVAILABLE
        return EXIT_OK
    finally:
        sconn.close()


if __name__ == "__main__":
    sys.exit(main())
