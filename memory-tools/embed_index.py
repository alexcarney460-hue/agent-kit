#!/usr/bin/env python3
"""embed_index — dense vectors for every memory record.

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
import datetime
import hashlib
import json
import os
import sqlite3
import sys
import urllib.request

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memdir import memory_dir  # noqa: E402

HM = os.path.expanduser("~/hivemind.db")
CM = os.path.expanduser("~/.claude-mem/claude-mem.db")
SEM = os.path.expanduser("~/.claude/memory-tools/semantic.db")
MEMDIR = memory_dir()
DB = os.path.expanduser("~/.claude/memory-tools/embeddings.db")

MODEL = "qwen3-embedding:0.6b"   # A/B vs nomic via eval-history (model col keys rows)
DIM = 1024
OLLAMA = "http://localhost:11434/api/embed"
BATCH = 32
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
    conn.commit()
    return conn


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:24]


def embed_batch(texts: list[str], prefix: str = DOC_PREFIX, timeout: int = 120) -> list[list[float]] | None:
    """Ollama batch embed; None on ANY failure (caller degrades gracefully)."""
    body = json.dumps({"model": MODEL, "input": [prefix + t[:2000] for t in texts]}).encode()
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
    sidecar is absent (routine on this box). Fall back to a normal open — we
    still only SELECT."""
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=8)
    except sqlite3.OperationalError:
        return sqlite3.connect(path, timeout=8)


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
        # supersession redirects: an expired row's text is the OLD
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
    # successfully; a transient open failure must never gut the index (HIGH-2)
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


def search(conn: sqlite3.Connection, query: str, k: int = 10):
    vec = embed_batch([query], prefix=QUERY_PREFIX, timeout=30)
    if vec is None:
        return None
    keys, mat = load_matrix(conn)
    if mat is None:
        return None
    q = np.asarray(vec[0], dtype=np.float32)
    q = q / (np.linalg.norm(q) or 1.0)
    sims = mat @ q
    top = np.argsort(-sims)[:k]
    return [(keys[i][0], keys[i][1], float(sims[i])) for i in top]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--query")
    a = ap.parse_args()
    conn = connect()
    if a.update:
        print(json.dumps(update(conn)))
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
