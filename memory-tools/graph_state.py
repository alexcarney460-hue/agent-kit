#!/usr/bin/env python3
"""graph_state — SQLite state/transaction helper and OS lock for the graph
backfill writer (Astra transition Batch 2, staged).

Responsibilities, deliberately small:
  * explicit versioned migration of the additive graph tables (never on a read path)
  * the writer lock: an OS advisory `flock` on `<semantic.db>.graph-writer.lock`
    held for the owner's lifetime and inherited by child work through the
    open file description (env GRAPH_WRITER_LOCK_FD). No PID-age takeover,
    no --force, no process killing.
  * run records (attempt vs. last completed scan are separate facts)
  * cursor epochs with compare-and-swap advancement
  * ONE publishing transaction per batch: owned auto links + record state +
    cursor together, after a fresh policy-revision recheck.
"""
from __future__ import annotations

import dataclasses
import datetime
try:
    import fcntl
except ImportError:
    fcntl = None
    import msvcrt
import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from pathlib import Path

import graph_policy

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "semantic", "migrations")
SCHEMA_REVISION = 2
LOCK_ENV = "GRAPH_WRITER_LOCK_FD"

STATUSES = ("matched", "unmatched", "excluded", "pending", "unsupported", "unavailable",
            "identity_conflict", "missing_inactive")
SUCCESS_STATUSES = ("matched", "unmatched", "excluded", "missing_inactive")

# Test-only hook: fixtures set graph_state._debug_hook = fn(point) to inject a
# crash at a named transaction boundary. Inert (None) in production.
_debug_hook = None


class StateError(RuntimeError):
    pass


class LockHeld(StateError):
    pass


class CursorConflict(StateError):
    pass


class PolicyChanged(StateError):
    pass


def utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def new_run_id() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]


def _hook(point: str) -> None:
    if _debug_hook is not None:
        _debug_hook(point)


# --------------------------------------------------------------------------- #
# migration
# --------------------------------------------------------------------------- #
def migration_files() -> list:
    if not os.path.isdir(MIGRATIONS_DIR):
        return []
    return sorted(f for f in os.listdir(MIGRATIONS_DIR) if f.endswith(".sql"))


def migrations_sha256() -> str:
    h = hashlib.sha256()
    for f in migration_files():
        with open(os.path.join(MIGRATIONS_DIR, f), "rb") as fh:
            h.update(f.encode() + b"\0" + fh.read() + b"\0")
    return h.hexdigest()


def activation_revision() -> str:
    """Policy-matrix version + migration content hash (plain token)."""
    return f"{graph_policy.POLICY_MATRIX_VERSION}+migrations:{migrations_sha256()[:16]}"


def migrate(conn: sqlite3.Connection, source_scope_sha256: str) -> dict:
    """Apply the additive migrations explicitly. Idempotent. Never called by readers."""
    before = graph_policy.tables_present(conn)
    files = migration_files()
    if not files:
        raise StateError("no migration files found")
    if not re.fullmatch(r"[0-9a-f]{64}", source_scope_sha256):
        raise StateError("source scope must be a SHA-256 hex digest (graph_policy.source_scope_sha256)")
    script = ["BEGIN IMMEDIATE;"]
    for f in files:
        with open(os.path.join(MIGRATIONS_DIR, f), encoding="utf-8") as fh:
            script.append(fh.read())
    script.append(
        "INSERT OR IGNORE INTO graph_ingest_control "
        "(id, schema_revision, activation_revision, mode, source_scope_sha256, updated_at) "
        f"VALUES (1, {int(SCHEMA_REVISION)}, '{activation_revision()}', 'shadow', "
        f"'{source_scope_sha256}', '{utcnow()}');")
    script.append("COMMIT;")
    old = conn.isolation_level
    conn.isolation_level = None            # explicit transaction inside the script only
    try:
        conn.executescript("\n".join(script))
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.isolation_level = old
    after = graph_policy.tables_present(conn)
    return {"tables_before": sorted(before), "tables_after": sorted(after), "files": files,
            "migrations_sha256": migrations_sha256(), "activation_revision": activation_revision(),
            "source_scope_sha256": source_scope_sha256}


def accept_scope(conn: sqlite3.Connection, previous: str, current: str) -> str:
    """Re-approve a changed source scope: one transaction, compare-and-swap on the
    stored digest, activation_revision recomputed. Returns the new activation
    revision. Never touches cursors, record state or owned links."""
    if not re.fullmatch(r"[0-9a-f]{64}", current) or previous == current:
        raise StateError("accept_scope needs a new 64-hex digest")
    act = activation_revision()
    conn.execute("BEGIN IMMEDIATE")
    try:
        n = conn.execute(
            "UPDATE graph_ingest_control SET source_scope_sha256=?, activation_revision=?, updated_at=? "
            "WHERE id=1 AND source_scope_sha256=?", (current, act, utcnow(), previous)).rowcount
        if n != 1:
            raise StateError("stored source scope changed underneath accept_scope")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return act


def writer_conn(db_path: str) -> sqlite3.Connection:
    """Writer connection in autocommit mode: every write in this module runs
    inside its own explicit BEGIN IMMEDIATE ... COMMIT. Opened with
    `mode=rw`: an absent database is an error, never created here."""
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=rw", uri=True,
                           timeout=10, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# --------------------------------------------------------------------------- #
# writer lock
# --------------------------------------------------------------------------- #
def lock_path_for(db_path: str) -> str:
    return db_path + ".graph-writer.lock"


class WriterLock:
    """Exclusive advisory lock held for the lifetime of the owner.

    If GRAPH_WRITER_LOCK_FD names an inherited descriptor for the same lock
    file, that descriptor is adopted: flock on the same open file description
    is idempotent, so the child proves it holds the parent's lock rather than
    competing with it. Otherwise a fresh descriptor is opened. The lock is
    never taken over by age, and no process is ever killed to obtain it.
    """

    def __init__(self, db_path: str) -> None:
        self.path = lock_path_for(db_path)
        self.fd: int | None = None
        self.adopted = False

    def __enter__(self) -> "WriterLock":
        env = os.environ.get(LOCK_ENV)
        if env:
            if fcntl is None:
                raise StateError("inherited graph locks are not supported on Windows")
            try:
                fd = int(env)
                st_fd = os.fstat(fd)
                st_path = os.stat(self.path)
                same = (st_fd.st_ino == st_path.st_ino and st_fd.st_dev == st_path.st_dev
                        and stat.S_ISREG(st_fd.st_mode))
            except (OSError, ValueError):
                same = False
            if not same:
                raise StateError("GRAPH_WRITER_LOCK_FD does not name the writer lock file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LockHeld("inherited descriptor does not hold the writer lock") from exc
            self.fd, self.adopted = fd, True
            return self
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise StateError("writer lock is not a regular file")
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except (BlockingIOError, PermissionError) as exc:
            os.close(fd)
            raise LockHeld("another writer holds the graph lock") from exc
        except Exception:
            os.close(fd)
            raise
        os.set_inheritable(fd, True)   # child work inherits the same open file description
        self.fd = fd
        return self

    def __exit__(self, *exc) -> None:
        if self.fd is not None and not self.adopted:
            try:
                if fcntl is not None:
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
                else:
                    os.lseek(self.fd, 0, os.SEEK_SET)
                    msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            finally:
                os.close(self.fd)
        self.fd = None

    def child_env(self) -> dict:
        """Environment for child work that must run under this lock."""
        return {LOCK_ENV: str(self.fd)}


# --------------------------------------------------------------------------- #
# runs
# --------------------------------------------------------------------------- #
def start_run(conn: sqlite3.Connection, entry: str, mode: str, source_revision: dict,
              reader_rev: str, alias_rev: str, policy_rev: str) -> str:
    run_id = new_run_id()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO graph_ingest_run (run_id, entry, mode, source_revision, reader_revision, "
        "alias_revision, policy_revision, started_at, result) VALUES (?,?,?,?,?,?,?,?,'running')",
        (run_id, entry, mode, json.dumps(source_revision, sort_keys=True), reader_rev, alias_rev,
         policy_rev, utcnow()))
    conn.execute("COMMIT")
    return run_id


def finish_run(conn: sqlite3.Connection, run_id: str, result: str, counters: dict,
               error_code: str | None = None) -> None:
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE graph_ingest_run SET ended_at=?, result=?, counters_json=?, error_code=? WHERE run_id=?",
        (utcnow(), result, json.dumps(counters, sort_keys=True), error_code, run_id))
    conn.execute("COMMIT")


def mark_interrupted_runs(conn: sqlite3.Connection) -> int:
    """Runs left 'running' by a dead owner become 'interrupted' (evidence kept).
    Only called by a process that holds the writer lock, so no live run exists."""
    conn.execute("BEGIN IMMEDIATE")
    n = conn.execute(
        "UPDATE graph_ingest_run SET result='interrupted', ended_at=COALESCE(ended_at, ?) "
        "WHERE result='running'", (utcnow(),)).rowcount
    conn.execute("COMMIT")
    return n


# --------------------------------------------------------------------------- #
# cursors
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class Cursor:
    store: str
    phase: str
    epoch: int
    high_water: str | None
    boundary_json: str | None
    cursor: str | None
    source_identity: str | None
    revision_json: str | None
    last_completed_epoch: int | None
    last_completed_at: str | None


def get_cursor(conn: sqlite3.Connection, store: str, phase: str) -> Cursor | None:
    row = conn.execute(
        "SELECT store, phase, epoch, high_water, boundary_json, cursor, source_identity, revision_json, "
        "last_completed_epoch, last_completed_at FROM graph_scan_cursor WHERE store=? AND phase=?",
        (store, phase)).fetchone()
    return Cursor(*row) if row else None


def start_epoch(conn: sqlite3.Connection, store: str, phase: str, high_water: str,
                boundary_json: str | None, source_identity: str, revisions: dict,
                initial_cursor: str) -> Cursor:
    """Open a new epoch with a fixed boundary. Previous completion facts are kept."""
    prev = get_cursor(conn, store, phase)
    epoch = (prev.epoch + 1) if prev else 1
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO graph_scan_cursor (store, phase, epoch, high_water, boundary_json, cursor, "
        "source_identity, revision_json, last_completed_epoch, last_completed_at, completion_json, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(store, phase) DO UPDATE SET epoch=excluded.epoch, high_water=excluded.high_water, "
        "boundary_json=excluded.boundary_json, cursor=excluded.cursor, source_identity=excluded.source_identity, "
        "revision_json=excluded.revision_json, updated_at=excluded.updated_at",
        (store, phase, epoch, high_water, boundary_json, initial_cursor, source_identity,
         json.dumps(revisions, sort_keys=True), prev.last_completed_epoch if prev else None,
         prev.last_completed_at if prev else None, None, utcnow()))
    conn.execute("COMMIT")
    return get_cursor(conn, store, phase)


def close_epoch(conn: sqlite3.Connection, cur: Cursor, completion: dict) -> None:
    """Record completion only for the exact epoch/cursor observed (CAS)."""
    conn.execute("BEGIN IMMEDIATE")
    n = conn.execute(
        "UPDATE graph_scan_cursor SET last_completed_epoch=?, last_completed_at=?, completion_json=?, updated_at=? "
        "WHERE store=? AND phase=? AND epoch=? AND IFNULL(cursor,'')=IFNULL(?,'') AND cursor=high_water",
        (cur.epoch, utcnow(), json.dumps(completion, sort_keys=True), utcnow(),
         cur.store, cur.phase, cur.epoch, cur.cursor)).rowcount
    if n != 1:
        conn.execute("ROLLBACK")
        raise CursorConflict(f"close_epoch CAS failed for {cur.store}/{cur.phase}")
    conn.execute("COMMIT")


# --------------------------------------------------------------------------- #
# publishing
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class Outcome:
    store: str
    ref: str
    ref_num: int | None
    identity_guard: str
    content_sha256: str | None
    status: str
    reason: str
    hits: dict                      # entity_id -> weight (matched only)
    epoch: int

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown status {self.status!r}")


def publish(conn: sqlite3.Connection, cur: Cursor, new_cursor: str, outcomes: list,
            run_id: str, revisions: dict, counters: dict, policy_check) -> None:
    """Commit owned links + record state + cursor in ONE transaction.

    `policy_check(conn)` must return True when the policy revision fixed for
    this batch is still current; otherwise nothing is published and the batch
    is retryable. Cursor advancement is compare-and-swap on the expected
    epoch/cursor; a concurrent change aborts the whole batch.
    """
    now = utcnow()
    _hook("before_publish")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not policy_check(conn):
            raise PolicyChanged("policy revision changed before publish")
        for o in outcomes:
            success = o.status in SUCCESS_STATUSES
            if success:
                conn.execute("DELETE FROM graph_auto_mention WHERE store=? AND ref=?", (o.store, o.ref))
            if o.status == "matched":
                for eid, w in sorted(o.hits.items()):
                    conn.execute(
                        "INSERT INTO graph_auto_mention (store, ref, entity_id, content_sha256, alias_revision, "
                        "reader_revision, policy_revision, weight, run_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (o.store, o.ref, eid, o.content_sha256, revisions["alias"], revisions["reader"],
                         revisions["policy"], float(w), run_id, now))
            conn.execute(
                "INSERT INTO graph_record_state (store, ref, ref_num, identity_guard, content_sha256, "
                "reader_revision, alias_revision, policy_revision, observed_at, verified_at, last_seen_epoch, "
                "status, reason, last_success_revision, last_success_run, last_attempt_run) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(store, ref) DO UPDATE SET "
                "  ref_num=excluded.ref_num, "
                "  content_sha256=CASE WHEN ? THEN excluded.content_sha256 ELSE graph_record_state.content_sha256 END, "
                "  reader_revision=excluded.reader_revision, alias_revision=excluded.alias_revision, "
                "  policy_revision=excluded.policy_revision, verified_at=excluded.verified_at, "
                "  last_seen_epoch=excluded.last_seen_epoch, status=excluded.status, reason=excluded.reason, "
                "  last_success_revision=CASE WHEN ? THEN excluded.last_success_revision ELSE graph_record_state.last_success_revision END, "
                "  last_success_run=CASE WHEN ? THEN excluded.last_success_run ELSE graph_record_state.last_success_run END, "
                "  last_attempt_run=excluded.last_attempt_run",
                (o.store, o.ref, o.ref_num, o.identity_guard, o.content_sha256, revisions["reader"],
                 revisions["alias"], revisions["policy"], now, now, o.epoch, o.status, o.reason,
                 json.dumps(revisions, sort_keys=True) if success else None, run_id if success else None,
                 run_id, int(success), int(success), int(success)))
        _hook("after_rows_before_cursor")
        n = conn.execute(
            "UPDATE graph_scan_cursor SET cursor=?, updated_at=? "
            "WHERE store=? AND phase=? AND epoch=? AND IFNULL(cursor,'')=IFNULL(?,'')",
            (new_cursor, now, cur.store, cur.phase, cur.epoch, cur.cursor)).rowcount
        if n != 1:
            raise CursorConflict(f"cursor CAS failed for {cur.store}/{cur.phase} epoch {cur.epoch}")
        conn.execute("UPDATE graph_ingest_run SET counters_json=? WHERE run_id=?",
                     (json.dumps(counters, sort_keys=True), run_id))
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    _hook("after_commit")


def state_rows(conn: sqlite3.Connection, store: str, after: str | None, upto: str | None,
               limit: int, numeric: bool) -> list:
    """Keyset page of record_state rows for the reconciliation lane."""
    if numeric:
        a = int(after) if after not in (None, "") else -1
        u = int(upto) if upto not in (None, "") else None
        if u is None:
            return []
        return conn.execute(
            "SELECT ref, ref_num, identity_guard, content_sha256, status, alias_revision, reader_revision "
            "FROM graph_record_state WHERE store=? AND ref_num>? AND ref_num<=? ORDER BY ref_num LIMIT ?",
            (store, a, u, limit)).fetchall()
    return conn.execute(
        "SELECT ref, ref_num, identity_guard, content_sha256, status, alias_revision, reader_revision "
        "FROM graph_record_state WHERE store=? AND ref>? ORDER BY ref LIMIT ?",
        (store, after or "", limit)).fetchall()


def pending_rows(conn: sqlite3.Connection, store: str, limit: int) -> list:
    return conn.execute(
        "SELECT ref, ref_num, identity_guard, content_sha256, status, alias_revision, reader_revision "
        "FROM graph_record_state WHERE store=? AND status='pending' ORDER BY ref_num, ref LIMIT ?",
        (store, limit)).fetchall()


def owned_support(conn: sqlite3.Connection) -> dict:
    """entity_id -> set of (store, ref) with owned automatic rows (storage level,
    independent of reader eligibility). Empty when the tables are absent."""
    if graph_policy.tables_present(conn) != set(graph_policy.CONTROL_TABLES):
        return {}
    out: dict = {}
    for eid, store, ref in conn.execute("SELECT entity_id, store, ref FROM graph_auto_mention"):
        out.setdefault(eid, set()).add((store, ref))
    return out
