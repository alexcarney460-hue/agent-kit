#!/usr/bin/env python3
"""Confirm memory freshness and flag possible contradictions for human review."""
from __future__ import annotations

import argparse
from contextlib import closing
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import memdir

STALE_DAYS = 30
DURABLE_CATEGORIES = {"correction", "feedback", "preference", "decision",
                      "workflow", "process", "security", "warning"}
STATUS_RE = re.compile(r"(?i)^.{0,40}?\bgroup\s+[a-h]\b.{0,60}?\bpass\s?\d+")
SAFE_CATEGORY_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")
_CANDIDATES_NOTE = ""


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def confirmed_at(last_confirmed, valid_from, recorded_at, created_at) -> str | None:
    return next((str(v) for v in (last_confirmed, valid_from, recorded_at, created_at) if v), None)


def _date(value):
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


def is_stale(category, confirmed_iso, now=None) -> bool:
    if (category or "").lower() in DURABLE_CATEGORIES:
        return False
    date = _date(confirmed_iso)
    if date is None:
        return False
    today = (now or dt.datetime.now()).date()
    return (today - date).days > STALE_DAYS


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _learning_rows(hm_conn, ids=None):
    cols = _columns(hm_conn, "learnings")
    names = ("last_confirmed", "valid_from", "recorded_at", "created_at")
    selects = ", ".join(n if n in cols else f"NULL AS {n}" for n in names)
    where = "WHERE active=1"
    params = []
    if ids is not None:
        if not ids:
            return []
        where += " AND id IN (" + ",".join("?" for _ in ids) + ")"
        params = list(ids)
    rule = "rule" if "rule" in cols else "NULL AS rule"
    context = "context" if "context" in cols else "NULL AS context"
    return hm_conn.execute(f"SELECT id, category, {rule}, {context}, {selects} FROM learnings {where}", params).fetchall()


def stale_map(hm_conn, ids) -> dict[int, str]:
    result = {}
    for rid, category, _, _, *dates in _learning_rows(hm_conn, ids):
        date = confirmed_at(*dates)
        if is_stale(category, date):
            result[int(rid)] = _date(date).isoformat()
    return result


def _frontmatter(path):
    text = Path(path).read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return "", text
    parts = text.split("---", 2)
    return (parts[1], parts[2]) if len(parts) == 3 else ("", text)


def stale_note(path) -> str | None:
    try:
        front, _ = _frontmatter(path)
        # Only top-level type or metadata.type describes this note.
        top = re.search(r"(?m)^type:\s*['\"]?(\w+)", front)
        meta = re.search(r"(?ms)^metadata:\s*\n(?:[ \t]+[^\n]*\n)*?[ \t]+type:\s*['\"]?(\w+)", front)
        if not (top and top.group(1) == "project" or meta and meta.group(1) == "project"):
            return None
        date = dt.datetime.fromtimestamp(Path(path).stat().st_mtime).date()
        return date.isoformat() if (dt.date.today() - date).days > STALE_DAYS else None
    except (OSError, UnicodeError, ValueError):
        return None


def confirm(hm_conn, ids) -> int:
    if not ids:
        return 0
    import consolidate
    guard = consolidate.mutable_learning_guard(hm_conn)
    cur = hm_conn.execute("UPDATE learnings SET last_confirmed=? WHERE active=1 AND id IN (" +
                          ",".join("?" for _ in ids) + ")" + guard, [now(), *ids])
    hm_conn.commit()
    return cur.rowcount


def ensure_schema(sem_conn):
    sem_conn.execute("""CREATE TABLE IF NOT EXISTS memory_flag (
        id INTEGER PRIMARY KEY, kind TEXT NOT NULL, a_ref TEXT NOT NULL,
        b_ref TEXT NOT NULL, reason TEXT, suggest_keep TEXT, source TEXT NOT NULL,
        created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', card TEXT,
        closed_at TEXT, UNIQUE(kind, a_ref, b_ref))""")
    sem_conn.execute("""CREATE TABLE IF NOT EXISTS checked_pair (
        a_ref TEXT, b_ref TEXT, checked_at TEXT, verdict TEXT,
        PRIMARY KEY(a_ref, b_ref))""")
    sem_conn.execute("CREATE TABLE IF NOT EXISTS checker_state (key TEXT PRIMARY KEY, value TEXT)")
    sem_conn.commit()


def ordered(a, b):
    return tuple(sorted((str(a), str(b)), key=int))


def record_flag(sem_conn, a, b, reason, suggest_keep, source):
    a, b = ordered(a, b)
    sem_conn.execute("""INSERT INTO memory_flag
        (kind,a_ref,b_ref,reason,suggest_keep,source,created_at)
        VALUES ('contradiction',?,?,?,?,?,?)
        ON CONFLICT(kind,a_ref,b_ref) DO NOTHING""",
        (a, b, reason, str(suggest_keep) if suggest_keep is not None else None, source, now()))
    sem_conn.commit()
    return sem_conn.execute("SELECT id FROM memory_flag WHERE kind='contradiction' AND a_ref=? AND b_ref=?",
                            (a, b)).fetchone()[0]


def candidate_pairs(keys, mat, eligible, fresh, done, k=3, min_cos=.80, max_cos=.97, cap=40):
    import numpy as np
    if not len(keys):
        return []
    found = {}
    allowed = np.array([i for i, ref in enumerate(keys) if ref in eligible], dtype=np.int32)
    if len(allowed) < 2:
        return []
    sub = mat[allowed]
    for start in range(0, len(allowed), 800):
        sims = sub[start:start + 800] @ sub.T
        for local, row in enumerate(sims):
            idx = start + local
            row[idx] = -1
            row[(row < min_cos) | (row >= max_cos)] = -1
            # Only eligible neighbors are in sub. Stable sorting resolves ties.
            choices = np.argsort(-row, kind="stable")[:k]
            for j in choices:
                cos = float(row[j])
                if not min_cos <= cos < max_cos:
                    continue
                a, b = ordered(keys[allowed[idx]], keys[allowed[j]])
                if (a, b) not in done:
                    found[(a, b)] = cos
    result = [(a, b, cos) for (a, b), cos in found.items()]
    result.sort(key=lambda item: (-int(item[1]), -int(item[0])))
    fresh_pairs = [item for item in result if item[0] in fresh or item[1] in fresh]
    backlog = [item for item in result if item[0] not in fresh and item[1] not in fresh]
    fresh_quota = min(cap // 2, len(fresh_pairs))
    backlog_quota = min(cap - fresh_quota, len(backlog))
    return (fresh_pairs[:fresh_quota] + backlog[:backlog_quota] +
            fresh_pairs[fresh_quota:fresh_quota + cap - fresh_quota - backlog_quota])


def load_candidates(hm_conn, sem_conn, emb_conn, cap):
    global _CANDIDATES_NOTE
    _CANDIDATES_NOTE = ""
    if emb_conn is None:
        _CANDIDATES_NOTE = "embeddings.db unavailable"
        return []
    try:
        import numpy as np
        import embed_index
        rows = _learning_rows(hm_conn)
        active = {str(r[0]): r for r in rows}
        eligible = {ref for ref, r in active.items()
                    if not STATUS_RE.match(r[2] or "")}
        state = sem_conn.execute("SELECT value FROM checker_state WHERE key='last_run'").fetchone()
        last = state[0] if state else None
        fresh = {ref for ref, r in active.items() if last and
                 str(r[6] or r[7] or "").replace(" ", "T", 1) > last.replace(" ", "T", 1)}
        done = {ordered(a, b) for a, b in sem_conn.execute("SELECT a_ref,b_ref FROM checked_pair")}
        done.update(ordered(a, b) for a, b in sem_conn.execute("SELECT a_ref,b_ref FROM memory_flag"))
        vectors = []
        keys = []
        for ref, blob in emb_conn.execute("SELECT ref,vec FROM embedding WHERE store='hivemind' AND model=?",
                                           (embed_index.MODEL,)):
            if ref not in eligible or len(blob) != 4 * embed_index.DIM:
                continue
            vec = np.frombuffer(blob, dtype=np.float32)
            norm = np.linalg.norm(vec)
            if norm > 0:
                keys.append(ref)
                vectors.append(vec / norm)
        _CANDIDATES_NOTE = f"{len(keys)} eligible vectors"
        return candidate_pairs(keys, np.asarray(vectors, dtype=np.float32), eligible, fresh, done, cap=cap)
    except (ImportError, OSError, sqlite3.Error, ValueError) as exc:
        _CANDIDATES_NOTE = f"candidate load unavailable: {type(exc).__name__}: {exc}"
        return []


def _record_line(row):
    rid, cat, rule, context, *dates = row
    return f"{rid} | {cat} | {confirmed_at(*dates) or 'unknown'} | {(rule or '')[:220]} | {(context or '')[:160]}"


def adjudicate_pairs(hm_conn, sem_conn, pairs, apply):
    import consolidate
    records = {str(r[0]): r for r in _learning_rows(hm_conn)}
    judged = contradictions = batch_errors = 0
    for start in range(0, len(pairs), 10):
        batch = [(a, b, score) for a, b, score in pairs[start:start + 10]
                 if a in records and b in records]
        if not batch:
            continue
        lines = []
        for n, (a, b, _) in enumerate(batch, 1):
            lines.append(f"{n}. {_record_line(records[a])}\n   {_record_line(records[b])}")
        prompt = ("Each numbered pair holds two records from an operating-memory store, with their category and date. "
                  "Decide whether they can both be true right now. CONTRADICTS = they cannot both be true and it is "
                  "unclear which is right; give suggest_keep (the id that looks current) only if the evidence says so. "
                  "CONSISTENT = both can be true, including when a later dated status updates an earlier status of "
                  "the same ongoing work (that is progress, not a contradiction), or they are about different things. "
                  "If unsure, CONSISTENT. Reply ONLY JSON: "
                  '{"results":[{"n":1,"verdict":"CONTRADICTS"|"CONSISTENT",'
                  '"suggest_keep":<id or null>,"reason":"<20 words max>"}]}\n' + "\n".join(lines)).replace("\x00", "")
        try:
            answer = consolidate.ask_model(prompt)
        except Exception:
            batch_errors += 1
            continue
        if not isinstance(answer, dict) or not isinstance(answer.get("results"), list):
            continue
        seen = set()
        for result in answer["results"]:
            if not isinstance(result, dict) or type(result.get("n")) is not int:
                continue
            n = result["n"]
            if not 1 <= n <= len(batch) or n in seen:
                continue
            a, b, _ = batch[n - 1]
            verdict = result.get("verdict")
            keep = result.get("suggest_keep")
            reason = result.get("reason")
            if (verdict not in ("CONTRADICTS", "CONSISTENT") or
                    (keep is not None and str(keep) not in (a, b)) or
                    (reason is not None and not isinstance(reason, str))):
                continue
            seen.add(n)
            judged += 1
            if verdict == "CONTRADICTS":
                contradictions += 1
            if apply:
                if verdict == "CONTRADICTS":
                    record_flag(sem_conn, a, b, str(reason).replace("\x00", "")[:200] if reason is not None else None,
                                keep, "nightly")
                sem_conn.execute("INSERT OR REPLACE INTO checked_pair VALUES (?,?,?,?)", (a, b, now(), verdict))
        if apply:
            sem_conn.commit()
    return judged, contradictions, batch_errors


def orphan_notes(memdir) -> list[str]:
    notes = [p for p in Path(memdir).glob("*.md") if p.name != "MEMORY.md" and not p.name.startswith("_")]
    parsed = {}
    for path in notes:
        try:
            front, body = _frontmatter(path)
            name = re.search(r"(?m)^name:\s*(.+)$", front)
            links = re.findall(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]", front + body)
            parsed[path] = ({path.stem, name.group(1).strip().strip("'\"") if name else path.stem},
                            {link.strip() for link in links})
        except (OSError, UnicodeError):
            continue
    return sorted(path.name for path, (aliases, _) in parsed.items()
                  if not any(aliases & links for other, (_, links) in parsed.items() if other != path))


def _open_embeddings():
    path = Path(os.environ.get("MEM_CHECK_EMB_DB") or os.path.join(memdir.tools_dir(), "embeddings.db"))
    try:
        return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def phase_check(hm_conn, sem_conn, apply, memory_directory):
    previous = sem_conn.execute("SELECT value FROM checker_state WHERE key='last_run'").fetchone()
    last = previous[0] if previous else None
    stale = stale_map(hm_conn, [r[0] for r in _learning_rows(hm_conn)])
    stale_new = sum(1 for date in stale.values() if last and
                    (_date(date) + dt.timedelta(days=STALE_DAYS + 1)) > _date(last))
    notes = sum(stale_note(p) is not None for p in Path(memory_directory).glob("*.md"))
    orphans = orphan_notes(memory_directory)
    emb = _open_embeddings()
    try:
        pairs = load_candidates(hm_conn, sem_conn, emb, 40)
    finally:
        if emb:
            emb.close()
    judged, contradictions, batch_errors = adjudicate_pairs(hm_conn, sem_conn, pairs, apply) if apply and memdir.cloud_ok() else (0, 0, 0)
    metrics = {"check_candidates": len(pairs), "check_pairs_judged": judged,
               "check_contradictions_new": contradictions, "check_batch_errors": batch_errors,
               "check_candidates_note": _CANDIDATES_NOTE,
               "stale_learnings": len(stale), "stale_new": stale_new,
               "stale_notes": notes, "orphan_notes": len(orphans), "orphan_sample": orphans[:20]}
    if apply:
        sem_conn.execute("INSERT OR REPLACE INTO checker_state VALUES ('last_run',?)", (now(),))
        sem_conn.commit()
    metrics["check_flags_open"] = sem_conn.execute("SELECT COUNT(*) FROM memory_flag WHERE status='open'").fetchone()[0]
    metrics["check_flags"] = [dict(zip(("id", "a_ref", "b_ref", "reason", "suggest_keep"), row)) for row in sem_conn.execute("SELECT id,a_ref,b_ref,reason,suggest_keep FROM memory_flag WHERE status='open' ORDER BY id")]
    if metrics["check_flags"]:
        print("Open contradiction flags:", json.dumps(metrics["check_flags"]))
    return metrics


def resolve(hm_conn, sem_conn, keep, drop):
    import consolidate
    keep, drop = int(keep), int(drop)
    if keep == drop:
        raise ValueError("KEEP and DROP must differ")
    rows = hm_conn.execute("SELECT id,active,protected FROM learnings WHERE id IN (?,?)", (keep, drop)).fetchall()
    by_id = {r[0]: r for r in rows}
    if keep not in by_id or drop not in by_id:
        raise ValueError("both learning ids must exist")
    if not by_id[keep][1]:
        raise ValueError("KEEP must be active")
    if not by_id[drop][1]:
        raise ValueError("DROP must be active")
    if by_id[drop][2] and not by_id[keep][2]:
        raise ValueError("cannot drop a protected learning in favor of an unprotected one")
    guard = consolidate.mutable_learning_guard(hm_conn)
    if not hm_conn.execute("SELECT 1 FROM learnings WHERE id=?" + guard, (drop,)).fetchone():
        raise ValueError("DROP is governed by memory_save_operations")
    a, b = ordered(keep, drop)
    stamp = now()
    # Both stores use WAL: an attached commit is atomic per file, not across files.
    # If a cross-file crash leaves an inactive learning's flag open, nightly
    # file_card closes that flag as resolved.
    sem_path = sem_conn.execute("PRAGMA database_list").fetchone()[2]
    attached = bool(sem_path and sem_path != ":memory:")
    if hm_conn.in_transaction:
        raise ValueError("resolve needs a hivemind connection with no pending transaction")
    if attached:
        hm_conn.execute("ATTACH DATABASE ? AS check_sem", (sem_path,))
    try:
        hm_conn.execute("BEGIN IMMEDIATE")
        hm_conn.execute("UPDATE learnings SET active=0,superseded_by=?,expired_at=? WHERE id=?" + guard,
                        (keep, stamp, drop))
        hm_conn.execute("UPDATE learnings SET last_confirmed=? WHERE id=?" + guard, (stamp, keep))
        if attached:
            hm_conn.execute("UPDATE check_sem.memory_flag SET status='resolved',closed_at=? "
                            "WHERE a_ref=? AND b_ref=? AND status IN ('open','filed')", (stamp, a, b))
        hm_conn.commit()
        if not attached:
            sem_conn.execute("UPDATE memory_flag SET status='resolved',closed_at=? "
                             "WHERE a_ref=? AND b_ref=? AND status IN ('open','filed')", (stamp, a, b))
            sem_conn.commit()
    except Exception:
        try:
            if hm_conn.in_transaction:
                hm_conn.rollback()
        except Exception:
            pass
        try:
            if not attached and sem_conn.in_transaction:
                sem_conn.rollback()
        except Exception:
            pass
        raise
    finally:
        if attached:
            handling_error = sys.exc_info()[0] is not None
            try:
                hm_conn.execute("DETACH DATABASE check_sem")
            except Exception:
                if not handling_error:
                    raise


def dismiss(sem_conn, flag_id):
    cur = sem_conn.execute("UPDATE memory_flag SET status='dismissed',closed_at=? WHERE id=?",
                           (now(), flag_id))
    sem_conn.commit()
    return cur.rowcount


def main(argv=None):
    if not argv:
        return health_check()
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("confirm", "resolve", "dismiss", "flags", "stale"):
        cmd = sub.add_parser(name)
        if name == "confirm":
            cmd.add_argument("ids", type=int, nargs="+")
        elif name == "resolve":
            cmd.add_argument("keep", type=int)
            cmd.add_argument("drop", type=int)
        elif name == "dismiss":
            cmd.add_argument("flag_id", type=int)
        elif name == "flags":
            cmd.add_argument("--all", action="store_true")
        else:
            cmd.add_argument("--limit", type=int, default=100)
    args = ap.parse_args(argv)
    hm_path = memdir.hivemind_db()
    sem_path = memdir.semantic_db()
    try:
        with closing(sqlite3.connect(hm_path)) as hm, closing(sqlite3.connect(sem_path)) as sem:
            hm.text_factory = lambda b: b.decode("utf-8", "replace") if isinstance(b, bytes) else b
            if "last_confirmed" not in _columns(hm, "learnings"):
                hm.execute("ALTER TABLE learnings ADD COLUMN last_confirmed TEXT")
                hm.commit()
            ensure_schema(sem)
            if args.command == "confirm":
                print(confirm(hm, args.ids))
            elif args.command == "resolve":
                resolve(hm, sem, args.keep, args.drop)
                print("resolved")
            elif args.command == "dismiss":
                print(dismiss(sem, args.flag_id))
            elif args.command == "flags":
                where = "" if args.all else "WHERE status IN ('open','filed')"
                for fid, a, b, status, reason in sem.execute(
                        f"SELECT id,a_ref,b_ref,status,reason FROM memory_flag {where} ORDER BY id"):
                    rules = hm.execute("SELECT id,rule FROM learnings WHERE id IN (?,?)", (a, b)).fetchall()
                    print(f"flag {fid} [{status}] {reason or ''}")
                    for rid, rule in rules:
                        print(f"  #{rid}: {rule}")
            else:
                stale = stale_map(hm, [r[0] for r in _learning_rows(hm)])
                for rid, date in sorted(stale.items(), key=lambda item: item[1])[:max(0, args.limit)]:
                    rule = hm.execute("SELECT rule FROM learnings WHERE id=?", (rid,)).fetchone()[0]
                    print(f"#{rid} unconfirmed since {date}: {rule}")
    except (sqlite3.Error, ValueError) as exc:
        print(f"mem_check: {exc}", file=sys.stderr)
        return 1
    return 0


def health_check():
    stores = (("memory database", memdir.hivemind_db()),
              ("plugin database", os.path.join(memdir.home(), ".claude-mem", "claude-mem.db")))
    problems = []
    for name, path in stores:
        if not os.path.isfile(path):
            continue
        try:
            with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
                if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    problems.append(name)
        except sqlite3.Error:
            problems.append(name)
    if problems:
        print("Memory check: unable to verify " + ", ".join(problems))
        return 1
    print("Memory check: local stores OK or not installed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
