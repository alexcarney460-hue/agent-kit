#!/usr/bin/env python3
"""embed_index — dense vectors for every memory record (local3367).

One sqlite file (embeddings.db) of float32 blobs, embedded locally via Ollama
nomic-embed-text (768d, task prefixes per the model card). Incremental: a
text_hash per row means unchanged records are never re-embedded. Rebuildable
from the stores at any time — this file is cache, not truth.

Corpus: active hivemind learnings (rule+context), non-suppressed claude-mem
observations (title+facts/text), file memories (per .md file).

CLI:
  embed_index.py --update      # embed new/changed records (hourly-safe)
  embed_index.py --stats
  embed_index.py --query "..." # debug: top-10 cosine neighbors
Ollama down => clean no-op (recall degrades to lexical+entity, never breaks).
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime
import ctypes
import mmap
import platform
try:
    import fcntl
except ImportError:
    fcntl = None
import hashlib
import itertools
import json
import math
import os
import struct
import secrets
import shutil
import sqlite3
import sys
import tempfile
import urllib.request

try:
    import numpy as np
except ImportError:
    np = None
import memdir
from pathlib import Path

HM = memdir.hivemind_db()
CM = os.path.join(memdir.home(), ".claude-mem", "claude-mem.db")
SEM = memdir.semantic_db()
MEMDIR = memdir.memory_dir()
DB = os.path.join(memdir.tools_dir(), "embeddings.db")

MODEL = "qwen3-embedding:0.6b"   # A/B vs nomic via eval-history (model col keys rows)

# With the service default of 0 every query embedding paid about 5 s of model load, and under load the 30 s
# timeout expired and recall silently fell back to keywords. Same value as mem_recall.RERANK_KEEP_ALIVE_S.
KEEP_ALIVE_S = 600
NUM_CTX = 2048  # inputs are cut at 2000 characters; measured identical vectors to the default context (cosine 1.000000)
DIM = 1024
OLLAMA = "http://localhost:11434/api/embed"
BATCH = 32
# Memory-map normalised vectors to avoid streaming a large index from SQLite on every search.
# It is derived data: kept outside this folder so the nightly backup never archives it, and rebuilt at will.
MATRIX_DIR = os.path.join(memdir.tools_dir(), "embedding-matrix")
MATRIX_POINTER = "current"            # names the folder of the build in use
MATRIX_PREFIX = "build-"
MATRIX_LOCK = "build.lock"
MATRIX_REBUILD_AFTER = 5000           # changes logged since the build before the hourly refresh rebuilds it
MATRIX_F32_MISSING_MAX = 64 * 1024 ** 2
I8_CANARY_SIZES = (1, 2, 63, 64, 65, 264, 768, 1152, 4096)
_I8_RUNTIME_OK = set()
_I8_RUNTIME_FAILED = False
MATRIX_MIN_FREE = 6 * 1024 ** 3       # disk that must remain free after a build; without it recall uses the slow scan
DOC_PREFIX = ""                       # qwen3: documents are embedded raw
QUERY_PREFIX = "Instruct: Given a query, retrieve relevant memory records\nQuery: "

now = lambda: datetime.datetime.now().isoformat(timespec="seconds")


def connect(path: str = DB) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=8000;")
    conn.text_factory = lambda b: b.decode("utf-8", "replace") if isinstance(b, (bytes, bytearray)) else b
    conn.execute(
        "CREATE TABLE IF NOT EXISTS embedding ("
        " store TEXT NOT NULL, ref TEXT NOT NULL, model TEXT NOT NULL,"
        " text_hash TEXT NOT NULL, vec BLOB NOT NULL, created_at TEXT NOT NULL,"
        " PRIMARY KEY (store, ref))")
    # Every insert, rewrite and delete is logged by the database itself, so search() knows exactly which keys
    # changed since the matrix was built, whoever wrote them and whatever timestamp they carry.
    conn.execute("CREATE TABLE IF NOT EXISTS embedding_change ("
                 " seq INTEGER PRIMARY KEY AUTOINCREMENT, store TEXT NOT NULL, ref TEXT NOT NULL)")
    conn.execute("CREATE TRIGGER IF NOT EXISTS embedding_change_insert AFTER INSERT ON embedding BEGIN"
                 " INSERT INTO embedding_change (store, ref) VALUES (NEW.store, NEW.ref); END")
    conn.execute("CREATE TRIGGER IF NOT EXISTS embedding_change_update AFTER UPDATE ON embedding BEGIN"
                 " INSERT INTO embedding_change (store, ref) VALUES (OLD.store, OLD.ref);"
                 " INSERT INTO embedding_change (store, ref) VALUES (NEW.store, NEW.ref); END")
    conn.execute("CREATE TRIGGER IF NOT EXISTS embedding_change_delete AFTER DELETE ON embedding BEGIN"
                 " INSERT INTO embedding_change (store, ref) VALUES (OLD.store, OLD.ref); END")
    # Each matrix build stamps the database it was built from ('matrix_epoch'); search uses a matrix only while
    # the database still carries that stamp (a test database once got production's matrix).
    conn.execute("CREATE TABLE IF NOT EXISTS embedding_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("DROP INDEX IF EXISTS embedding_created_at")  # remove the obsolete timestamp index
    conn.commit()
    return conn


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:24]


def embed_batch(texts: list[str], prefix: str = DOC_PREFIX, timeout: int = 120) -> list[list[float]] | None:
    """Ollama batch embed; None on ANY failure (caller degrades gracefully)."""
    if not memdir.cloud_ok():
        return None
    from urllib.parse import urlsplit
    target = urlsplit(OLLAMA)
    if target.scheme != "http" or target.hostname not in ("localhost", "127.0.0.1", "::1"):
        return None
    body = json.dumps({"model": MODEL, "input": [prefix + t[:2000] for t in texts],
                       "keep_alive": KEEP_ALIVE_S, "options": {"num_ctx": NUM_CTX}}).encode()
    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read())
        vecs = out.get("embeddings")
        if not vecs or len(vecs) != len(texts) or len(vecs[0]) != DIM:
            return None
        return vecs
    except Exception as exc:
        sys.stderr.write(f"[embed] ollama unavailable ({exc})\n")
        return None


def open_ro(path: str) -> sqlite3.Connection:
    """Read-only open with a fallback: mode=ro URI fails on a WAL db whose -shm
    sidecar is absent. Fall back to a normal open — we
    still only SELECT."""
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=8)


def corpus():
    """Yield ok_stores marker last: (store, ref, text) records, then a final
    ("__ok__", frozenset, "") sentinel naming stores that were read successfully.
    A store that failed to read must NOT have its rows pruned by update()."""
    lenient = lambda b: b.decode("utf-8", "replace") if isinstance(b, (bytes, bytearray)) else b
    ok = set()
    try:
        c = open_ro(HM); c.text_factory = lenient
        for rid, rule, ctx in c.execute(
                "SELECT id, rule, COALESCE(context,'') FROM learnings WHERE active=1"):
            yield "hivemind", str(rid), f"{rule}\n{ctx}"[:2400]

        # vocabulary for a fact that still exists — embed it so queries phrased
        # the old way land, then mem_recall maps hm_redirect -> the active root.
        rows = c.execute("SELECT id, active, superseded_by, rule, COALESCE(context,'') "
                         "FROM learnings WHERE superseded_by IS NOT NULL").fetchall()
        state = {r[0]: (r[1], r[2]) for r in c.execute(
            "SELECT id, active, superseded_by FROM learnings")}
        for rid, active, sup, rule, ctx in rows:
            if active:
                continue
            root, seen_ids = sup, set()
            while root in state and root not in seen_ids:
                seen_ids.add(root)
                a, nxt = state[root]
                if a:
                    break
                root = nxt
            else:
                continue  # chain broken or cyclic — no active root
            if root in state and state[root][0]:
                yield "hm_redirect", str(rid), f"{rule}\n{ctx}"[:2400]
        c.close()
        ok.add("hivemind")
        ok.add("hm_redirect")
    except Exception as exc:
        sys.stderr.write(f"[embed] hivemind skipped: {exc}\n")
    sup = set()
    try:
        s = open_ro(SEM)
        sup = {r[0] for r in s.execute("SELECT obs_id FROM suppressed_observation")}
        s.close()
    except Exception:
        pass
    try:
        c = open_ro(CM); c.text_factory = lenient
        for oid, title, facts in c.execute(
                "SELECT id, COALESCE(title,''), COALESCE(facts, SUBSTR(COALESCE(text,''),1,800)) "
                "FROM observations"):
            if oid in sup:
                continue
            yield "claudemem", str(oid), f"{title}\n{facts}"[:2400]
        c.close()
        ok.add("claudemem")
    except Exception as exc:
        sys.stderr.write(f"[embed] claude-mem skipped: {exc}\n")
    try:
        for fn in sorted(os.listdir(MEMDIR)):
            if fn.endswith(".md") and fn != "MEMORY.md" and not fn.startswith("_"):
                text = open(os.path.join(MEMDIR, fn), encoding="utf-8", errors="replace").read()
                yield "file", fn, text[:2400]
        ok.add("file")
    except Exception as exc:
        sys.stderr.write(f"[embed] files skipped: {exc}\n")
    yield "__ok__", frozenset(ok), ""


def update(conn: sqlite3.Connection) -> dict:
    if np is None:
        return {"corpus": 0, "embedded": 0, "failed": 0, "pruned": 0, "unavailable": "numpy not installed"}
    # a row is current only if BOTH the text and the model match — otherwise a
    # model switch silently re-embeds nothing and load_matrix() goes empty
    existing = {(s, r): (h, m) for s, r, h, m in
                conn.execute("SELECT store, ref, text_hash, model FROM embedding")}
    todo, seen, ok_stores = [], set(), frozenset()
    for store, ref, text in corpus():
        if store == "__ok__":
            ok_stores = ref
            continue
        seen.add((store, ref))
        h = _hash(text)
        if existing.get((store, ref)) != (h, MODEL):
            todo.append((store, ref, text, h))
    # rows whose source record vanished — pruned ONLY for stores that were read
    # successfully; a transient open failure must never gut the index
    stale = [k for k in existing if k not in seen and k[0] in ok_stores]
    for store, ref in stale:
        conn.execute("DELETE FROM embedding WHERE store=? AND ref=?", (store, ref))
    conn.commit()
    done = fail = 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        vecs = embed_batch([t for _, _, t, _ in chunk])
        if vecs is None:
            fail += len(todo) - i
            break
        for (store, ref, _t, h), v in zip(chunk, vecs):
            if len(v) != DIM:  # one ragged row would poison load_matrix forever
                sys.stderr.write(f"[embed] wrong-dim vec for {store}:{ref} — skipped\n")
                continue
            conn.execute(
                "INSERT INTO embedding (store, ref, model, text_hash, vec, created_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(store, ref) DO UPDATE SET "
                "model=excluded.model, text_hash=excluded.text_hash, "
                "vec=excluded.vec, created_at=excluded.created_at",
                (store, ref, MODEL, h, np.asarray(v, dtype=np.float32).tobytes(), now()))
        conn.commit()
        done += len(chunk)
    return {"corpus": len(seen), "embedded": done, "failed": fail, "pruned": len(stale)}


def load_matrix(conn: sqlite3.Connection):
    """Return (keys, unit-normalized matrix). ~8k x 768 float32 ≈ 24MB."""
    rows = conn.execute("SELECT store, ref, vec FROM embedding WHERE model=?", (MODEL,)).fetchall()
    if not rows:
        return [], None
    keys = [(s, r) for s, r, _ in rows]
    mat = np.frombuffer(b"".join(v for _, _, v in rows), dtype=np.float32).reshape(len(rows), DIM)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return keys, mat / norms


SEARCH_CHUNK = 4096


class MatrixError(RuntimeError):
    """The mapped matrix could not be built; search still works by reading SQLite."""


def _unit_rows(blobs: list) -> np.ndarray:
    mat = np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(len(blobs), DIM)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def _last_change(conn: sqlite3.Connection) -> int:
    """The highest change number ever given out (it survives trimming of the log)."""
    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='embedding_change'").fetchone()
    if row is None:
        return 0
    if isinstance(row[0], bool) or not isinstance(row[0], int):
        raise MatrixError("the change counter is not a number")
    return row[0]


def _current_folder() -> str | None:
    try:
        with open(os.path.join(MATRIX_DIR, MATRIX_POINTER), encoding="utf-8") as f:
            return os.path.join(MATRIX_DIR, os.path.basename(f.read().strip()))
    except OSError:
        return None


def _remove_builds(keep: str | None) -> None:
    """Remove every build folder but `keep`. Only called by the one builder that holds the lock."""
    for other in os.listdir(MATRIX_DIR):
        path = os.path.join(MATRIX_DIR, other)
        if other.startswith(MATRIX_PREFIX) and path != keep and os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)


def _epoch(conn: sqlite3.Connection) -> str | None:
    """The stamp the newest matrix build left in this database, or None."""
    row = conn.execute("SELECT value FROM embedding_meta WHERE key='matrix_epoch'").fetchone()
    return row[0] if row else None


def _matrix_for(conn: sqlite3.Connection):
    """The build in use, when this database carries its stamp; otherwise None.

    A build writes a fresh stamp into the database it read and empties the change log up to its own change
    number in the same transaction. So a database with the stamp holds, in its log, every change made since
    that build; a different database, a copy taken before the build, or a database a newer build has since
    stamped does not have it and is answered by the full scan."""
    # Windows cannot safely replace/delete builds that another reader has mapped.
    # Use the ordinary SQLite scan instead of keeping a large resident copy.
    if os.name == "nt":
        return None
    mapped = _matrix()
    if mapped is None:
        return None
    try:
        if mapped[3]["epoch"] != _epoch(conn) or _last_change(conn) < mapped[3]["seq"]:
            return None
    except (sqlite3.Error, MatrixError):
        return None
    return mapped


@contextmanager
def _matrix_build_lock():
    """Serialize builders using the platform's process-released file lock."""
    path = os.path.join(MATRIX_DIR, MATRIX_LOCK)
    with open(path, "a+b") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        elif os.name == "nt":
            import msvcrt
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            raise MatrixError("matrix builds require a supported file lock")


def build_matrix(conn: sqlite3.Connection) -> dict:
    """Write every vector of MODEL, unit-normalised, to a new folder and make it the build in use.

    Readers follow the pointer file, so they see the old build or the new one, never a mixture; a reader that
    already mapped the old files keeps them until it exits, and falls back to the scan once the database
    carries the new build's stamp. One builder at a time, because a build removes every other build folder."""
    os.makedirs(MATRIX_DIR, exist_ok=True)
    with _matrix_build_lock():
        return _build_locked(conn)


def _require_space(needed: int) -> None:
    """Refuse to go on when writing `needed` more bytes would leave less than MATRIX_MIN_FREE. A check, not a
    reservation: another job can still take the space afterwards, which is why it is repeated at every step."""
    if shutil.disk_usage(MATRIX_DIR).free - needed < MATRIX_MIN_FREE:
        raise MatrixError(f"not enough free disk for the vector matrix ({needed / 1024 ** 3:.2f} GiB still to write, "
                          f"{MATRIX_MIN_FREE / 1024 ** 3:.0f} GiB must stay free)")


def _point_at(folder: str) -> None:
    pointer = os.path.join(MATRIX_DIR, MATRIX_POINTER)
    with open(pointer + ".tmp", "w", encoding="utf-8") as f:
        f.write(os.path.basename(folder))
    os.replace(pointer + ".tmp", pointer)


def _stamp_database(conn: sqlite3.Connection, epoch: str, seq: int) -> None:
    """Stamp the database with the new build and empty the log up to it, in ONE transaction: a search sees
    either the old stamp with the old log, or the new stamp with only the changes made after `seq`. Until this
    commits, a search that already follows the pointer to the new build finds no matching stamp and scans."""
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT OR REPLACE INTO embedding_meta VALUES ('matrix_epoch', ?)", (epoch,))
    conn.execute("DELETE FROM embedding_change WHERE seq <= ?", (seq,))
    conn.commit()


def _build_locked(conn: sqlite3.Connection) -> dict:
    pointer = os.path.join(MATRIX_DIR, MATRIX_POINTER)
    folder = None
    _remove_builds(keep=_current_folder())  # what a killed build left behind, before the disk check counts it
    conn.commit()
    conn.execute("BEGIN")  # one read snapshot: the rows written out are exactly the state at change number `seq`
    try:
        seq = _last_change(conn)
        expected, longest = conn.execute("SELECT COUNT(*), COALESCE(MAX(LENGTH(CAST(ref AS BLOB))), 0) "
                                         "FROM embedding WHERE model=?", (MODEL,)).fetchone()
        names = sorted(row[0] for row in conn.execute("SELECT DISTINCT store FROM embedding WHERE model=?", (MODEL,)))
        if len(names) > 255:
            raise MatrixError("more than 255 stores")
        code = {s: i for i, s in enumerate(names)}
        meta = {"model": MODEL, "dim": DIM, "count": 0, "built_at": now(), "seq": seq,
                "epoch": secrets.token_hex(16), "store_names": names}
        keys_bytes = expected * (1 + max(longest, 1)) + 2 * 4096  # store codes and keys, written after the rows
        meta_bytes = len(json.dumps(meta)) + 4096
        _require_space(expected * DIM * 4 + keys_bytes + meta_bytes)
        folder = tempfile.mkdtemp(prefix=MATRIX_PREFIX, dir=MATRIX_DIR)
        stores, refs, count = [], [], 0
        cur = conn.execute("SELECT store, ref, vec FROM embedding WHERE model=?", (MODEL,))
        with open(os.path.join(folder, "matrix.f32"), "wb") as out:
            while rows := cur.fetchmany(SEARCH_CHUNK):
                rows = [row for row in rows if len(row[2]) == DIM * 4]
                if not rows:
                    continue
                keys = [str(r).encode() for _, r, _ in rows]
                # the key file is as wide as its longest key AS WRITTEN (a key stored as bytes is written as its
                # text form, about four times longer than the length the database reports)
                longest = max(longest, max(len(key) for key in keys))
                keys_bytes = max(expected, count + len(rows)) * (1 + max(longest, 1)) + 2 * 4096
                _require_space(max(expected - count, len(rows)) * DIM * 4 + keys_bytes + meta_bytes)
                out.write(_unit_rows([v for _, _, v in rows]).astype(np.float32).tobytes())
                stores.extend(code[s] for s, _, _ in rows)
                refs.extend(keys)
                count += len(rows)
            out.flush()
            os.fsync(out.fileno())
        codes = np.asarray(stores, dtype=np.uint8)
        keys = np.asarray(refs, dtype=np.bytes_) if refs else np.zeros(0, dtype="S1")
        _require_space(codes.nbytes + keys.nbytes + 2 * 4096 + meta_bytes)  # their real sizes, now known
        np.save(os.path.join(folder, "stores.npy"), codes)
        _require_space(keys.nbytes + 4096 + meta_bytes)
        np.save(os.path.join(folder, "refs.npy"), keys)
        meta = {**meta, "count": count}
        try:
            meta = {**meta, **_write_int8(folder, count)}
        except (OSError, MatrixError, MemoryError):
            try:
                os.unlink(os.path.join(folder, "matrix.i8"))
            except OSError:
                pass  # optional accelerator; float32 remains authoritative
        _require_space(meta_bytes)
        with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f)
        conn.rollback()  # end of the read snapshot
        replaced = _current_folder()
        _point_at(folder)
        try:
            _stamp_database(conn, meta["epoch"], seq)
        except BaseException:  # the database still carries the previous build's stamp and its whole log
            conn.rollback()
            if replaced:
                _point_at(replaced)
            else:
                os.unlink(pointer)
            raise
    except BaseException:
        conn.rollback()
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
        try:
            os.unlink(pointer + ".tmp")
        except OSError:
            pass
        raise
    _remove_builds(keep=folder)  # the build this one replaces; a search that mapped it keeps its mapping
    return {**meta, "path": folder}


def _matrix():
    """(mapped matrix, store code per row, ref per row, meta) of the build in use, or None when there is none
    or it does not match this model, this dimension or its own sizes."""
    for _attempt in range(2):  # a build can swap folders between reading the pointer and opening the files
        try:
            with open(os.path.join(MATRIX_DIR, MATRIX_POINTER), encoding="utf-8") as f:
                folder = os.path.join(MATRIX_DIR, os.path.basename(f.read().strip()))
            with open(os.path.join(folder, "meta.json"), encoding="utf-8") as f:
                meta = json.load(f)
            count, names, seq = meta["count"], meta["store_names"], meta["seq"]
            if (meta["model"] != MODEL or meta["dim"] != DIM or not isinstance(count, int) or count <= 0
                    or isinstance(seq, bool) or not isinstance(seq, int) or seq < 0
                    or not isinstance(meta["epoch"], str) or not meta["epoch"]
                    or not isinstance(names, list) or not isinstance(meta["built_at"], str)
                    or os.path.getsize(os.path.join(folder, "matrix.f32")) != count * DIM * 4):
                return None
            matrix_path = os.path.join(folder, "matrix.f32")
            matrix = (np.fromfile(matrix_path, dtype=np.float32).reshape(count, DIM) if os.name == "nt"
                      else np.memmap(matrix_path, dtype=np.float32, mode="r", shape=(count, DIM)))
            codes = np.load(os.path.join(folder, "stores.npy"))
            refs = np.load(os.path.join(folder, "refs.npy"), mmap_mode=None if os.name == "nt" else "r")
            if (len(codes) != count or len(refs) != count or codes.dtype != np.uint8 or refs.dtype.kind != "S"
                    or int(codes.max()) >= len(names)):
                return None
            return matrix, codes, refs, {**meta, "_i8": _open_int8(folder, meta, count)}
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


def _cosines(blobs: list, q: np.ndarray) -> np.ndarray:
    return np.nan_to_num(_unit_rows(blobs) @ q, nan=-1.0) if blobs else np.zeros(0, dtype=np.float32)


def _scan_scores(conn: sqlite3.Connection, q: np.ndarray):
    """The slow path: (stores, refs, cosines) for every vector, read out of SQLite."""
    cur = conn.execute("SELECT store, ref, vec FROM embedding WHERE model=?", (MODEL,))
    stores, refs, parts = [], [], []
    while rows := cur.fetchmany(SEARCH_CHUNK):
        rows = [row for row in rows if len(row[2]) == DIM * 4]
        parts.append(_cosines([v for _, _, v in rows], q))
        stores.extend(s for s, _, _ in rows)
        refs.extend(str(r) for _, r, _ in rows)
    return stores, refs, np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


def _changed_scores(conn: sqlite3.Connection, q: np.ndarray, meta: dict):
    """(changed keys, stores, refs, cosines) for what the change log lists since the build `meta` describes, or
    None when this database no longer carries that build's stamp.

    Every listed key replaces its matrix row: with its current vector when the record still has one of this
    model, with nothing when it was deleted or re-embedded by another model. The stamp and the log are read
    in one snapshot, so a build that finishes meanwhile cannot take away changes this search still needs."""
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")
    try:
        if _epoch(conn) != meta["epoch"]:
            return None
        rows = conn.execute(
            "SELECT c.store, c.ref, e.vec FROM (SELECT DISTINCT store, ref FROM embedding_change WHERE seq > ?) c "
            "LEFT JOIN embedding e ON e.store = c.store AND e.ref = c.ref AND e.model = ?",
            (meta["seq"], MODEL)).fetchall()
    finally:
        if own:
            conn.rollback()
    changed = {(s, str(r)) for s, r, _ in rows}
    live = [(s, str(r), v) for s, r, v in rows if v is not None and len(v) == DIM * 4]
    return changed, [s for s, _, _ in live], [r for _, r, _ in live], _cosines([v for _, _, v in live], q)


def _write_int8(folder: str, count: int) -> dict:
    """Use the data maximum to avoid a clipped-row error tail.

    float64 differences/norms measure the worst finite row against the int8 values times the stored scale.
    -128 marks a nonfinite row, always re-scored exactly; valid values use -127..127.
    """
    if not count or os.name == "nt":
        return {}
    matrix = np.memmap(os.path.join(folder, "matrix.f32"), dtype=np.float32, mode="r", shape=(count, DIM))
    clip, norm_error = 0.0, 0.0
    for start in range(0, count, SEARCH_CHUNK):
        part = np.asarray(matrix[start:start + SEARCH_CHUNK])
        finite = part[np.isfinite(part).all(axis=1) & np.any(part != 0, axis=1)]
        if len(finite):
            clip = max(clip, float(np.max(np.abs(finite))))
            norm_error = max(norm_error, float(np.max(np.abs(np.linalg.norm(finite.astype(np.float64), axis=1) - 1))))
    scale = np.float32(clip / 127 if clip else 1.0)
    error = 0.0
    _require_space(count * DIM + 8192)
    with open(os.path.join(folder, "matrix.i8"), "wb") as out:
        for start in range(0, count, SEARCH_CHUNK):
            part = np.asarray(matrix[start:start + SEARCH_CHUNK])
            invalid = ~np.isfinite(part).all(axis=1) | ~np.any(part != 0, axis=1)
            clean = np.where(invalid[:, None], 0, part)
            quant = np.clip(np.rint(clean / scale), -127, 127).astype(np.int8)
            dequant = quant.astype(np.float64) * float(scale)
            norms = np.linalg.norm(clean.astype(np.float64) - dequant.astype(np.float64), axis=1)
            error = max(error, float(np.max(norms)))
            quant[invalid] = -128
            _require_space((count - start) * DIM + 8192)
            out.write(quant.tobytes())
        out.flush()
        os.fsync(out.fileno())
    seed = secrets.randbits(63)
    return {"i8_scale": float(scale), "i8_error": float(np.nextafter(error, np.inf)), "i8_clip": clip,
            "i8_seed": seed, "i8_norm_error": norm_error, "i8_signature": _platform_signature(),
            "i8_bits_identical": _check_int8_bits(matrix, seed),
            "i8_canary": _build_runtime_canary(matrix, seed),
            "i8_sha256": _file_sha256(os.path.join(folder, "matrix.i8"))}


def _open_int8(folder: str, meta: dict, count: int):
    """A missing, truncated or invalid optional copy must not disable the float32 build."""
    if os.name == "nt":
        return None
    try:
        if not _int8_metadata_valid(meta) or meta["i8_signature"] != _platform_signature():
            return None
        if os.path.getsize(os.path.join(folder, "matrix.i8")) != count * DIM:
            return None
        small = np.memmap(os.path.join(folder, "matrix.i8"), dtype=np.int8, mode="r", shape=(count, DIM))
        matrix = np.memmap(os.path.join(folder, "matrix.f32"), dtype=np.float32, mode="r", shape=(count, DIM))
        return small if _validate_int8_pair(matrix, small, meta, count) else None
    except Exception:
        return None


def _int8_sample_rows(count, seed):
    """One seeded row per stratum across the entire build; all rows for small builds."""
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("invalid int8 seed")
    edges = np.linspace(0, count, min(count, 256) + 1, dtype=np.intp)
    return np.random.default_rng(seed).integers(edges[:-1], edges[1:])


def _validate_int8_pair(matrix, small, meta, count):
    """it catches a file from another build; it does not catch a few differing rows or a deliberate edit;
    `matrix.f32`, `refs.npy` and `stores.npy` have no content check either.
    """
    try:
        rows = _int8_sample_rows(count, meta["i8_seed"])
        return _int8_rows_valid(matrix[rows], small[rows], meta)
    except Exception:
        return False


def _int8_rows_valid(full, quant, meta):
    full = full.astype(np.float64)
    special = ~np.isfinite(full).all(axis=1) | ~np.any(full != 0, axis=1)
    marker = np.all(quant == -128, axis=1)
    if not np.array_equal(marker, special) or np.any(quant[~special] == -128):
        return False
    errors = np.linalg.norm(full[~special] - quant[~special].astype(np.float64) * meta["i8_scale"], axis=1)
    return bool(np.all(errors <= meta["i8_error"]))


def _platform_signature():
    try:
        config = getattr(np.__config__, "CONFIG", None)
        deps = config.get("Build Dependencies")
        blas = deps.get("blas")
        if not isinstance(blas, dict):
            return None
        return {"numpy": np.__version__, "machine": platform.machine(),
                "platform": platform.platform(), "blas": str(blas)}
    except Exception:
        return None


def _int8_metadata_valid(meta):
    try:
        if not isinstance(meta, dict):
            return False
        for key in ("i8_scale", "i8_error", "i8_clip", "i8_norm_error"):
            value = meta[key]
            if type(value) not in (int, float) or not np.isfinite(value) or value < 0:
                return False
        seed = meta["i8_seed"]
        return (meta["i8_scale"] > 0 and type(seed) is int and seed >= 0
                and type(meta["i8_bits_identical"]) is bool
                and isinstance(meta["i8_signature"], dict)
                and isinstance(meta["i8_canary"], dict)
                and isinstance(meta["i8_sha256"], str) and len(meta["i8_sha256"]) == 64
                and all(char in "0123456789abcdef" for char in meta["i8_sha256"]))
    except Exception:
        return False


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_int8(folder=None):
    """Fully check the optional pair in chunks; discard only the accelerator on any failure."""
    folder = folder or _current_folder()
    if not folder:
        print("int8 verification failed: no build")
        return False
    try:
        with open(os.path.join(folder, "meta.json"), encoding="utf-8") as source:
            meta = json.load(source)
        count = meta["count"]
        if type(count) is not int or count <= 0 or not _int8_metadata_valid(meta):
            raise ValueError("metadata")
        if (os.path.getsize(os.path.join(folder, "matrix.i8")) != count * DIM
                or os.path.getsize(os.path.join(folder, "matrix.f32")) != count * DIM * 4):
            raise ValueError("file size")
        matrix = np.memmap(os.path.join(folder, "matrix.f32"), dtype="f4", mode="r", shape=(count, DIM))
        small = np.memmap(os.path.join(folder, "matrix.i8"), dtype="i1", mode="r", shape=(count, DIM))
        for start in range(0, count, SEARCH_CHUNK):
            if not _int8_rows_valid(matrix[start:start + SEARCH_CHUNK], small[start:start + SEARCH_CHUNK], meta):
                raise ValueError("row pair")
        if _file_sha256(os.path.join(folder, "matrix.i8")) != meta["i8_sha256"]:
            raise ValueError("sha256")
        stamp = {"time": datetime.datetime.now(datetime.timezone.utc).isoformat(), "rows_checked": count}
        with open(os.path.join(folder, "i8-verified.json"), "w", encoding="utf-8") as target:
            json.dump(stamp, target)
        print(f"int8 verified: {count} rows")
        return True
    except Exception:
        for name in ("matrix.i8", "i8-verified.json"):
            try:
                os.unlink(os.path.join(folder, name))
            except OSError:
                pass
        print("int8 verification failed: accelerator removed")
        return False


def _int8_verification_due(folder):
    try:
        with open(os.path.join(folder, "i8-verified.json"), encoding="utf-8") as source:
            stamp = json.load(source)
        age = datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(stamp["time"])
        return age.total_seconds() < 0 or age.total_seconds() > 24 * 3600
    except Exception:
        return True


def _gathered_product(matrix, rows, q):
    if DIM != 1024 or len(matrix) < 64:
        return np.nan_to_num(matrix @ q, nan=-1.0)[rows]
    part = np.zeros((max(64, len(rows)), DIM), dtype=np.float32)
    part[:len(rows)] = matrix[rows]
    return np.nan_to_num((part @ q)[:len(rows)], nan=-1.0)


def _canary_inputs(matrix, seed):
    rng = np.random.default_rng(seed)
    # Replacement allows every requested gathered shape even for tiny fixture builds.
    rows = rng.choice(len(matrix), 4096, replace=len(matrix) < 4096)
    queries = rng.normal(size=(4, DIM)).astype(np.float32)
    queries /= np.linalg.norm(queries, axis=1, keepdims=True)
    return rows, queries


def _check_int8_bits(matrix, seed):
    """Empirical canary over search padding/shapes, not a universal BLAS theorem."""
    rows, queries = _canary_inputs(matrix, seed)
    identical = True
    for q in queries:
        full = np.nan_to_num(matrix @ q, nan=-1.0)
        for size in I8_CANARY_SIZES:
            gathered = _gathered_product(matrix, rows[:size], q)
            identical = (full[rows[:size]].tobytes() == gathered.tobytes()) and identical
    return identical


def _runtime_digest(matrix, rows, queries):
    digest = hashlib.sha256()
    # Read 328 source rows once, then reuse them for both queries (including the padding).
    parts = [np.asarray(matrix[rows[:size]]) for size in (64, 264)]
    for q in queries[:2]:
        for part in parts:
            digest.update(_gathered_product(part, np.arange(len(part)), q).tobytes())
    return digest.hexdigest()


def _build_runtime_canary(matrix, seed):
    rows, queries = _canary_inputs(matrix, seed)
    return {"seed": seed, "rows": rows[:264].tolist(),
            "sha256": _runtime_digest(matrix, rows, queries)}


def _runtime_int8_ok(meta):
    global _I8_RUNTIME_FAILED
    if _I8_RUNTIME_FAILED:
        return False
    try:
        canary = meta["i8_canary"]
        path = os.path.join(os.path.dirname(meta["_i8"].filename), "matrix.f32")
        key = (path, json.dumps(canary, sort_keys=True), json.dumps(meta["i8_signature"], sort_keys=True))
        if key in _I8_RUNTIME_OK:
            return True
        matrix = np.memmap(path, dtype="f4", mode="r", shape=meta["_i8"].shape)
        rows, queries = _canary_inputs(matrix, canary["seed"])
        if canary["seed"] != meta["i8_seed"] or rows[:264].tolist() != canary["rows"]:
            raise ValueError("canary rows")
        if _runtime_digest(matrix, rows, queries) != canary["sha256"]:
            raise ValueError("canary mismatch")
        _I8_RUNTIME_OK.add(key)
        return True
    except Exception:
        _I8_RUNTIME_FAILED = True
        return False


def _int8_usable(meta, q=None):
    """The floating-point allowance assumes unit rows and a finite unit query."""
    try:
        if (not _int8_metadata_valid(meta) or meta.get("_i8") is None
                or meta["i8_bits_identical"] is not True or meta["i8_norm_error"] > 1e-3
                or meta["i8_signature"] != _platform_signature()):
            return False
        if q is not None and (not np.isfinite(q).all()
                or abs(float(np.linalg.norm(q.astype(np.float64))) - 1) > 1e-3):
            return False
        return _runtime_int8_ok(meta)
    except Exception:
        return False


def _resident_share(matrix):
    """mincore inspects residency without faulting the mapped file's data pages in."""
    if os.name == "nt":
        raise OSError("mincore unavailable on Windows")
    address = matrix.ctypes.data
    aligned = address - address % mmap.PAGESIZE
    length = matrix.nbytes + address - aligned
    pages = (length + mmap.PAGESIZE - 1) // mmap.PAGESIZE
    vector = (ctypes.c_ubyte * pages)()
    libc = ctypes.CDLL(None, use_errno=True)
    mincore = libc.mincore
    mincore.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p)
    mincore.restype = ctypes.c_int
    if mincore(aligned, length, vector) != 0:
        raise OSError(ctypes.get_errno(), "mincore failed")
    flags = np.ctypeslib.as_array(vector) & 1
    resident = int(np.count_nonzero(flags)) * mmap.PAGESIZE
    if pages:
        resident -= (address - aligned) * int(flags[0])
        resident -= (pages * mmap.PAGESIZE - length) * int(flags[-1])
    return resident / matrix.nbytes if matrix.nbytes else 0.0


def _matrix_residency(matrix):
    try:
        return _resident_share(matrix)
    except Exception:
        return None


def _missing_bytes(matrix, residency):
    return None if residency is None else int(round(matrix.nbytes * (1 - residency)))


def _score_path(meta, missing_bytes, q=None):
    return ("int8" if _int8_usable(meta, q)
            and (missing_bytes is None or missing_bytes > MATRIX_F32_MISSING_MAX) else "float32")


class _Int8Scores:
    """Approximate scores and exact candidates. Floating-point allowance assumes unit rows and query."""

    def __init__(self, matrix, small, q, meta):
        self.matrix, self.q = matrix, q
        self.approx = np.empty(len(matrix), dtype=np.float32)
        self.invalid = np.zeros(len(matrix), dtype=bool)
        for start in range(0, len(matrix), SEARCH_CHUNK):
            part = small[start:start + SEARCH_CHUNK]
            dequant = part.astype(np.float32) * np.float32(meta["i8_scale"])
            scores = dequant @ q
            invalid = np.any(part == -128, axis=1)
            self.invalid[start:start + len(part)] = invalid
            scores[invalid] = -1.0
            self.approx[start:start + len(part)] = scores
        # Cauchy-Schwarz uses the actual query norm; gamma bounds cover BOTH float32 dots.
        eps = np.finfo(np.float32).eps
        gamma = DIM * eps / (1 - DIM * eps)
        self.bound = float(meta["i8_error"]) * float(np.linalg.norm(q.astype(np.float64)))
        self.bound += 4 * gamma * (1 + self.bound)
        self.exact = np.empty(len(matrix), dtype=np.float32)
        self.known = np.zeros(len(matrix), dtype=bool)
        self.rounds, self.rows_read = [], 0

    def __len__(self):
        return len(self.approx)

    def __getitem__(self, rows):
        if isinstance(rows, (bool, np.bool_)):
            raise TypeError("boolean scalar index")
        if isinstance(rows, (int, np.integer)):
            ids = np.asarray([rows], dtype=np.intp)
        elif isinstance(rows, slice):
            ids = np.arange(*rows.indices(len(self)))
        elif isinstance(rows, np.ndarray) and rows.ndim == 1:
            if rows.dtype == np.bool_ and len(rows) == len(self):
                ids = np.flatnonzero(rows)
            elif rows.dtype.kind in "iu":
                ids = rows
            else:
                raise TypeError("expected integer array or full-length boolean mask")
        else:
            raise TypeError("unsupported score index")
        self._rescore(np.unique(ids))
        return self.exact[rows]

    def _rescore(self, rows):
        missing = rows[~self.known[rows]]
        if not len(missing):
            return
        # Tiny dimensions can switch BLAS kernels with row count: retain the original product there.
        if DIM != 1024 or len(self.matrix) < 64:
            self.exact[:] = np.nan_to_num(self.matrix @ self.q, nan=-1.0)
            self.known[:] = True
            self.rows_read += len(self.matrix)
            return
        for start in range(0, len(missing), SEARCH_CHUNK):
            ids = missing[start:start + SEARCH_CHUNK]
            self.exact[ids] = _gathered_product(self.matrix, ids, self.q)
        self.known[missing] = True
        self.rows_read += len(missing)

    def select(self, pool, k):
        if k <= 0 or not len(pool):
            return np.zeros(0, dtype=np.intp)
        k = min(k, len(pool))
        count = min(len(pool), max(64, 2 * k))
        while True:
            self.rounds.append(count)
            order = _top(self.approx[pool], min(len(pool), count + 1))
            candidates = np.union1d(pool[order[:count]], pool[self.invalid[pool]])  # stable row-order ties
            self._rescore(candidates)
            winners = candidates[_top(self.exact[candidates], k)]
            if count == len(pool):
                return winners
            outside = float(self.approx[pool[order[count]]]) + self.bound
            # Strict inequality also certifies the row-order tie rule at the boundary.
            if float(self.exact[winners[-1]]) > outside:
                return winners
            count = min(len(pool), count * 4)


def _scores(conn: sqlite3.Connection, query: str):
    """None when the query cannot be embedded, else (mapped, changed keys, read).

    mapped = (store codes, store names, refs, cosines) from the matrix, or None when there is no usable one.
    read = (stores, refs, cosines) from SQLite: the records changed since the build, or everything without one."""
    vec = embed_batch([query], prefix=QUERY_PREFIX, timeout=30)
    if vec is None:
        return None
    q = np.asarray(vec[0], dtype=np.float32)
    q = q / (np.linalg.norm(q) or 1.0)
    mapped = _matrix_for(conn)
    try:
        delta = _changed_scores(conn, q, mapped[3]) if mapped is not None else None
    except sqlite3.Error:
        delta = None
    if delta is None:
        return None, set(), _scan_scores(conn, q)
    matrix, codes, refs, meta = mapped
    small = meta.get("_i8")
    residency = _matrix_residency(matrix) if small is not None else 0.0
    try:
        sims = (_Int8Scores(matrix, small, q, meta)
                if _score_path(meta, _missing_bytes(matrix, residency), q) == "int8" else None)
    except Exception:
        sims = None
    if sims is None:
        sims = np.nan_to_num(np.asarray(matrix @ q, dtype=np.float32), nan=-1.0)
    changed, *read = delta
    return (codes, meta["store_names"], refs, sims), changed, tuple(read)


def _top(sims: np.ndarray, k: int) -> np.ndarray:
    """Indices of the k largest values, largest first; equal values keep their order."""
    if k <= 0 or not len(sims):
        return np.zeros(0, dtype=np.intp)
    keep = np.arange(len(sims))
    if len(sims) > k:
        keep = np.flatnonzero(sims >= np.partition(sims, len(sims) - k)[len(sims) - k])
    return keep[np.argsort(-sims[keep], kind="stable")][:k]


def _select(scored: tuple, k: int, store: str | None = None) -> list:
    """Best k as [(store, ref, cosine)], best first, over every store or one.

    A key the change log lists never comes from the matrix: its current vector, if it has one, was read from
    SQLite. So a rewritten record counts once, with its new vector, and a deleted one not at all."""
    mapped, changed, (read_stores, read_refs, read_sims) = scored
    rows = np.asarray([i for i, s in enumerate(read_stores) if store is None or s == store], dtype=np.intp)
    hits = []
    if mapped is not None:
        codes, names, refs, sims = mapped
        if store is None:
            pool = np.arange(len(sims))
        else:
            pool = np.flatnonzero(codes == names.index(store)) if store in names else np.zeros(0, dtype=np.intp)
        dropped = sum(1 for s, _ in changed if store is None or s == store)  # at most this many winners fall out
        selected = (sims.select(pool, k + dropped) if isinstance(sims, _Int8Scores)
                    else pool[_top(sims[pool], k + dropped)])
        for i in selected:
            key = (names[codes[i]], bytes(refs[i]).decode())
            if key not in changed:
                hits.append((key[0], key[1], float(sims[i])))
        hits = hits[:k]
    hits.extend((read_stores[i], read_refs[i], float(read_sims[i])) for i in rows[_top(read_sims[rows], k)])
    return sorted(hits, key=lambda hit: -hit[2])[:k]


def _ranked(scored: tuple, store: str | None = None):
    """Yield (store, ref, cosine), best first, over every store or one, for as long as the caller reads."""
    want, given = 64, 0
    while True:
        hits = _select(scored, want, store)
        yield from hits[given:]
        if len(hits) < want:
            return
        given, want = len(hits), want * 4


def search(conn: sqlite3.Connection, query: str, k: int = 10):
    """Cosine top-k over every store as [(store, ref, cosine)], or None when nothing can be scored."""
    if np is None:
        scores = _plain_scores(conn, query)
        return scores[:k] or None if scores is not None else None
    scored = _scores(conn, query)
    if scored is None:
        return None
    return _select(scored, k) or None


def ranked_by_store(conn: sqlite3.Connection, query: str, stores):
    """{store: iterator of (ref, cosine), best first, over ALL of that store's vectors}, or None.

    One global list is dominated by the largest store (for example, raw observations), so a curated
    note that is the best match in its own store can sit hundreds of places down. Each store gets its own
    ranking, and a caller that filters (withheld records, superseded copies) reads on until it has enough.
    Everything is read from the database before this returns; the iterators need no connection."""
    if np is None:
        scores = _plain_scores(conn, query)
        if scores is None:
            return None
        return {store: iter([(ref, score) for current, ref, score in scores if current == store])
                for store in stores}
    scored = _scores(conn, query)
    if scored is None:
        return None
    return {store: ((ref, score) for _store, ref, score in _ranked(scored, store)) for store in stores}


def _plain_scores(conn: sqlite3.Connection, query: str):
    vectors = embed_batch([query], prefix=QUERY_PREFIX, timeout=30)
    if not vectors or len(vectors[0]) != DIM:
        return None
    q = vectors[0]
    qnorm = math.sqrt(sum(value * value for value in q)) or 1.0
    result = []
    for store, ref, blob in conn.execute("SELECT store, ref, vec FROM embedding WHERE model=?", (MODEL,)):
        if len(blob) != DIM * 4:
            continue
        vector = struct.unpack(f"{DIM}f", blob)
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        score = sum(a * b for a, b in zip(q, vector)) / (qnorm * norm)
        result.append((store, ref, score if math.isfinite(score) else -1.0))
    return sorted(result, key=lambda item: -item[2])


def search_by_store(conn: sqlite3.Connection, query: str, quotas: dict):
    """{store: [(ref, cosine), best first]} with at most quotas[store] entries each, or None."""
    ranked = ranked_by_store(conn, query, list(quotas))
    if ranked is None:
        return None
    return {store: list(itertools.islice(ranked[store], quota)) for store, quota in quotas.items()}


def matrix_status(conn: sqlite3.Connection) -> dict:
    """What the watchdog needs: is a matrix in use, how old, and how many changes are read from SQLite on top."""
    mapped = _matrix_for(conn)
    if mapped is None:
        return {"present": False}
    meta = mapped[3]
    newer = conn.execute("SELECT COUNT(*) FROM embedding_change WHERE seq > ?", (meta["seq"],)).fetchone()[0]
    residency = _matrix_residency(mapped[0])
    return {"present": True, "count": meta["count"], "built_at": meta["built_at"], "newer_records": newer,
            "int8": meta.get("_i8") is not None, "i8_error": meta.get("i8_error"),
            "residency": residency, "missing_bytes": _missing_bytes(mapped[0], residency)
            if residency is not None else "unknown",
            "search_path": _score_path(meta, _missing_bytes(mapped[0], residency))}


def refresh_matrix(conn: sqlite3.Connection, force: bool = False) -> dict:
    """Build the matrix when there is none, or when more than MATRIX_REBUILD_AFTER changes were logged since."""
    status = {"present": False}
    try:
        folder = _current_folder()
        if folder and os.path.exists(os.path.join(folder, "matrix.i8")) and _int8_verification_due(folder):
            verify_int8(folder)
        status = matrix_status(conn)
        if status["present"] and not force and status["newer_records"] <= MATRIX_REBUILD_AFTER:
            return {"built": False, **status}
        return {"built": True, **build_matrix(conn)}
    except (MatrixError, OSError, sqlite3.Error) as exc:
        return {"built": False, "error": f"{type(exc).__name__}: {exc}", **status}


def main():
    if np is None:
        print("Vector support is unavailable; install numpy to use the embedding index.")
        return
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--query")
    ap.add_argument("--build-matrix", action="store_true", help="rebuild the memory-mapped matrix now")
    ap.add_argument("--verify-int8", action="store_true", help="verify every row of the optional int8 pair")
    a = ap.parse_args()
    if a.verify_int8:
        verify_int8()
        return
    conn = connect()
    if a.update:
        print(json.dumps(update(conn)))
        print(json.dumps({k: v for k, v in refresh_matrix(conn).items() if k != "store_names"}))
    if a.build_matrix:
        print(json.dumps({k: v for k, v in build_matrix(conn).items() if k != "store_names"}))
    if a.stats:
        for row in conn.execute("SELECT store, COUNT(*) FROM embedding GROUP BY store"):
            print(*row)
    if a.query:
        hits = search(conn, a.query)
        for h in (hits or []):
            print(f"{h[2]:.3f} {h[0]}:{h[1]}")
    conn.close()


if __name__ == "__main__":
    main()
