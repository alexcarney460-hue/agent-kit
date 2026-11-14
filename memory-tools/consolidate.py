#!/usr/bin/env python3
"""consolidate — nightly memory lifecycle: the stack learns instead of accreting.

Five phases, all capped, all invalidate-never-delete:

  hm       merge near-duplicate hivemind learnings (LLM adjudicates clusters;
           losers get active=0 + superseded_by + expired_at)
  cm       suppress cross-session duplicate claude-mem observations via an
           OVERLAY in semantic.db (the plugin's DB is never structurally
           touched; mem_recall filters through the overlay)
  distill  episodic -> semantic: entities with enough recent observations get
           ONE distilled hivemind learning (Letta sleep-time / reflection)
  index    MEMORY.md mechanical sync: add entries for unindexed files; report
           (never delete) dangling entries
  check    flag possible contradictions and report stale or orphaned memories

DRY RUN by default — nothing mutates without --apply. Every applied change is
recorded in semantic.db consolidation_run for audit. Protected learnings are
never expired unless their replacement is also protected.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import datetime
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
if __name__ == "__main__":
    # launchd runs this file as __main__; mem_check does `import consolidate` for the check phase, which would load a
    # SECOND copy of this module with its own LLM_STATS
    sys.modules.setdefault("consolidate", sys.modules[__name__])
import mem_check
import memdir
import model_cli

HM = memdir.hivemind_db()
CM = os.path.expanduser("~/.claude-mem/claude-mem.db")
SEM = memdir.semantic_db()
MEMDIR = memdir.memory_dir()
REPORT = os.path.join(memdir.tools_dir(), "consolidation-report-latest.md")
LOG = os.path.join(memdir.tools_dir(), "consolidate.log")
DRY_MODE = False
MODEL_ALLOWED = False
IN_RUN = False

HM_CLUSTER_CAP = 15      # LLM-adjudicated merge clusters per run
CM_SUPPRESS_CAP = 200    # observation suppressions per run
DISTILL_CAP = 3          # distilled learnings per run
DISTILL_MIN_OBS = 8      # observations an entity needs before distillation
DISTILL_MIN_NEW = 10     # new observations since last distill to re-distill
JACCARD_MERGE = 0.60     # token overlap that makes two learnings cluster
LLM_TIMEOUT = 120

now = lambda: datetime.datetime.now().isoformat(timespec="seconds")


def epoch_s(v) -> float:
    """claude-mem stores created_at_epoch in MILLISECONDS; tests and other
    stores use seconds. Normalize like mem_recall.recency does."""
    v = v or 0
    return v / 1000 if v > 1e12 else v


def log(msg: str) -> None:
    line = f"{now()} {msg}"
    print(line)
    if DRY_MODE:
        return
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 262144:
            tail = Path(LOG).read_text(encoding="utf-8")[-131072:]
            Path(LOG).write_text(tail, encoding="utf-8")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def connect(path: str, writable: bool = True) -> sqlite3.Connection:
    if writable:
        conn = sqlite3.connect(path, timeout=15)
        conn.execute("PRAGMA journal_mode=WAL;")
    else:
        # Simulate schema/derived changes in memory; do not mutate live dry-run inputs.
        source = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
        conn = sqlite3.connect(":memory:")
        try:
            deadline = time.monotonic() + 120
            def progress(status, remaining, total):
                if time.monotonic() > deadline:
                    raise TimeoutError("Dry-run SQLite snapshot timed out")
            source.backup(conn, pages=256, progress=progress, sleep=0.05)
        finally:
            source.close()
    conn.execute("PRAGMA busy_timeout=8000;")
    # legacy rows contain non-UTF-8 bytes; decode leniently like hm_write/mem_recall
    conn.text_factory = lambda b: b.decode("utf-8", "replace") if isinstance(b, (bytes, bytearray)) else b
    return conn


# ---------------------------------------------------------------------------
# Schema — idempotent; hm_write imports this too
# ---------------------------------------------------------------------------

def ensure_schema(hm_conn: sqlite3.Connection, sem_conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in hm_conn.execute("PRAGMA table_info(learnings)")}
    for col, decl in [("valid_from", "TEXT"), ("valid_to", "TEXT"),
                      ("recorded_at", "TEXT"), ("expired_at", "TEXT"),
                      ("superseded_by", "INTEGER"), ("last_confirmed", "TEXT")]:
        if col not in cols:
            hm_conn.execute(f"ALTER TABLE learnings ADD COLUMN {col} {decl}")
    hm_conn.commit()
    sem_conn.execute(
        "CREATE TABLE IF NOT EXISTS suppressed_observation ("
        " obs_id INTEGER PRIMARY KEY, canonical_id INTEGER NOT NULL,"
        " reason TEXT, created_at TEXT NOT NULL)")
    sem_conn.execute(
        "CREATE TABLE IF NOT EXISTS distillation ("
        " entity_slug TEXT PRIMARY KEY, last_distilled_at TEXT,"
        " obs_count_at_distill INTEGER)")
    sem_conn.execute(
        "CREATE TABLE IF NOT EXISTS consolidation_run ("
        " id INTEGER PRIMARY KEY, ran_at TEXT, applied INTEGER, metrics TEXT)")
    sem_conn.commit()
    if hasattr(mem_check, "ensure_schema"):
        mem_check.ensure_schema(sem_conn)


# ---------------------------------------------------------------------------
# LLM helper — sem_discover's proven pattern: any failure returns None
# ---------------------------------------------------------------------------

LLM_RETRIES = 3          # more tries when the CLI cannot be started: it is being replaced by its own update
LLM_RETRY_WAIT_S = 20
LLM_WAIT_BUDGET_S = 300  # waiting for retries, all calls of one run together; the backup lock is held meanwhile
LLM_STAT_KEYS = ("llm_skipped", "llm_calls", "llm_unavailable", "llm_not_started", "llm_wait_s")
LLM_STATS = dict.fromkeys(LLM_STAT_KEYS, 0)   # one run's tally; main() zeroes it, finish_run() reports it


def reset_llm_stats() -> None:
    LLM_STATS.update(dict.fromkeys(LLM_STAT_KEYS, 0))


def llm_stats() -> dict:
    return dict(LLM_STATS)


def _pause(seconds: float) -> None:
    time.sleep(seconds)


def _start_model(prompt: str, timeout: int):
    """Retry only failures to start, subject to the shared waiting budget."""
    for attempt in range(LLM_RETRIES + 1):
        statuses = []
        output = model_cli.run_model(prompt, timeout, statuses.append)
        status = statuses[-1] if statuses else 'not_started'
        if status != 'not_started':
            return status, output
        if attempt >= LLM_RETRIES or LLM_STATS["llm_wait_s"] + LLM_RETRY_WAIT_S > LLM_WAIT_BUDGET_S:
            return status, None
        log(f"[llm] could not start; retrying in {LLM_RETRY_WAIT_S} s")
        _pause(LLM_RETRY_WAIT_S)
        LLM_STATS["llm_wait_s"] += LLM_RETRY_WAIT_S
    return 'not_started', None


def _json_object(stdout: str):
    m = re.search(r"\{.*\}", stdout, re.S)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(0))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def ask_model(prompt: str, timeout: int = LLM_TIMEOUT):
    """Returns a dict verdict or None. Only dicts are accepted — every prompt
    here demands a JSON object, and a list/string would crash the callers.

    Counts for the run report. llm_calls: every call that reaches the CLI. llm_unavailable: a call that got
    no answer from the model: the CLI never started (also counted in llm_not_started), it timed out, or it exited
    non-zero without a JSON object (refused: usage limit, not logged in; not retried). Output that is not a JSON
    object after a zero exit is a call that ran, not an unavailable one."""
    if not MODEL_ALLOWED or not memdir.cloud_ok() or os.environ.get("HM_NO_LLM"):
        if not IN_RUN:
            LLM_STATS["llm_skipped"] += 1
        return None
    LLM_STATS["llm_calls"] += 1
    status, output = _start_model(prompt, timeout)
    if output is None:
        LLM_STATS["llm_unavailable"] += 1
        if status == "not_started":
            LLM_STATS["llm_not_started"] += 1
        return None
    return _json_object(output)


def as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
def mutable_learning_guard(hm_conn):
    """Exact sourced decisions are changed only through their operation ledger."""
    exists = hm_conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_save_operations'").fetchone()
    return " AND id NOT IN (SELECT learning_id FROM memory_save_operations)" if exists else ""


# ---------------------------------------------------------------------------
# Phase: hivemind near-duplicate merge (LLM adjudicated)
# ---------------------------------------------------------------------------

def _tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9]{4,}", (text or "").lower()))


def find_hm_clusters(hm_conn) -> list[list[tuple]]:
    """Greedy Jaccard clustering of active learnings within a category."""
    rows = hm_conn.execute(
        "SELECT id, category, rule, context, confidence, protected, created_at "
        "FROM learnings WHERE active=1" + mutable_learning_guard(hm_conn)).fetchall()
    by_cat = defaultdict(list)
    for r in rows:
        by_cat[r[1]].append(r)
    clusters = []
    for cat_rows in by_cat.values():
        toks = {r[0]: _tokens(r[2]) for r in cat_rows}
        used, order = set(), sorted(cat_rows, key=lambda r: r[0])
        for r in order:
            if r[0] in used or not toks[r[0]]:
                continue
            cluster = [r]
            for s in order:
                if s[0] <= r[0] or s[0] in used or not toks[s[0]]:
                    continue
                inter = len(toks[r[0]] & toks[s[0]])
                union = len(toks[r[0]] | toks[s[0]])
                if union and inter / union >= JACCARD_MERGE:
                    cluster.append(s)
            if len(cluster) > 1:
                clusters.append(cluster)
                used.update(x[0] for x in cluster)
    return clusters


HM_MERGE_PROMPT = """You maintain a database of durable operating rules. The rows below look like near-duplicates. Decide whether they record the SAME fact/rule (merge) or genuinely different facts (keep_all).

Rows (id | confidence | created | rule | context-excerpt):
%s

Reply with ONLY JSON: {"action":"merge","canonical_id":<id>,"merged_context":"<=400 chars folding any unique detail from the losers into the canonical, or null"} or {"action":"keep_all"}.
Choose as canonical the most complete/current row. If unsure, keep_all."""


def apply_hm_merge(hm_conn, ids, canon, losers, merged_context):
    # Recheck under a write transaction: model latency must not race source authority.
    with hm_conn:
        hm_conn.execute("BEGIN IMMEDIATE")
        guard = mutable_learning_guard(hm_conn)
        marks = ','.join('?' for _ in ids)
        still_mutable = {r[0] for r in hm_conn.execute(
            f"SELECT id FROM learnings WHERE id IN ({marks})" + guard, ids)}
        if set(ids) != still_mutable:
            return False
        for row in losers:
            hm_conn.execute(
                "UPDATE learnings SET active=0, superseded_by=?, expired_at=? WHERE id=?" + guard,
                (canon, now(), row[0]))
        if merged_context and isinstance(merged_context, str):
            # Preserve the canonical's own narrative; only legacy records may receive model context.
            hm_conn.execute(
                "UPDATE learnings SET context=COALESCE(context,'')||' [merged '||?||': '||?||']',"
                " valid_from=COALESCE(valid_from, created_at) WHERE id=?" + guard,
                (now()[:10], merged_context[:600], canon))
    return True


def phase_hm(hm_conn, apply: bool) -> dict:
    clusters = find_hm_clusters(hm_conn)[:HM_CLUSTER_CAP]
    merged = kept = 0
    details = []
    for cluster in clusters:
        lines = "\n".join(
            f"{r[0]} | {r[4]} | {r[6]} | {(r[2] or '')[:160]} | {(r[3] or '')[:120]}"
            for r in cluster)
        verdict = ask_model(HM_MERGE_PROMPT % lines)
        if not verdict or verdict.get("action") != "merge":
            kept += 1
            continue
        ids = [r[0] for r in cluster]
        canon = as_int(verdict.get("canonical_id"))
        if canon not in ids:
            kept += 1
            continue
        canon_row = next(r for r in cluster if r[0] == canon)
        losers = [r for r in cluster if r[0] != canon]
        # protected guard: a protected loser may only fold into a protected canonical
        losers = [r for r in losers if not (r[5] and not canon_row[5])]
        if not losers:
            kept += 1
            continue
        if apply and not apply_hm_merge(hm_conn, ids, canon, losers, verdict.get("merged_context")):
            kept += 1
            continue
        details.append({"canonical": canon, "expired": [r[0] for r in losers]})
        merged += 1
    return {"hm_clusters_seen": len(clusters), "hm_merged": merged,
            "hm_kept_all": kept, "hm_detail": details[:10]}


# ---------------------------------------------------------------------------
# Phase: claude-mem cross-session duplicate suppression (deterministic overlay)
# ---------------------------------------------------------------------------

def _norm_title(t: str) -> str:
    t = re.sub(r"[^a-z0-9 ]", " ", (t or "").lower())
    return " ".join(sorted(w for w in t.split() if len(w) > 2))


def phase_cm(cm_conn, sem_conn, apply: bool) -> dict:
    already = {r[0] for r in sem_conn.execute("SELECT obs_id FROM suppressed_observation")}
    rows = cm_conn.execute(
        "SELECT id, title, project, COALESCE(relevance_count,0), created_at_epoch "
        "FROM observations WHERE title IS NOT NULL AND title != ''").fetchall()
    groups = defaultdict(list)
    for r in rows:
        if r[0] in already:
            continue
        key = (r[2], _norm_title(r[1]))
        if key[1]:
            groups[key].append(r)
    suppressed = []
    for (_proj, _nt), grp in groups.items():
        if len(grp) < 2:
            continue
        # window guard: only collapse rows within 14 days of each other
        grp.sort(key=lambda r: epoch_s(r[4]))
        head, window = grp[0], [grp[0]]
        for r in grp[1:]:
            if epoch_s(r[4]) - epoch_s(head[4]) <= 14 * 86400:
                window.append(r)
        if len(window) < 2:
            continue
        canon = max(window, key=lambda r: (r[3], r[4] or 0))
        for r in window:
            if r[0] != canon[0]:
                suppressed.append((r[0], canon[0]))
        if len(suppressed) >= CM_SUPPRESS_CAP:
            break
    suppressed = suppressed[:CM_SUPPRESS_CAP]
    if apply:
        for obs_id, canon_id in suppressed:
            sem_conn.execute(
                "INSERT OR IGNORE INTO suppressed_observation (obs_id, canonical_id, reason, created_at) "
                "VALUES (?,?,?,?)", (obs_id, canon_id, "cross-session duplicate title", now()))
        sem_conn.commit()
    return {"cm_suppressed": len(suppressed) if apply else 0,
            "cm_suppress_candidates": len(suppressed),
            "cm_total_overlay": len(already) + (len(suppressed) if apply else 0)}


# ---------------------------------------------------------------------------
# Phase: distillation — episodic observations -> one semantic learning
# ---------------------------------------------------------------------------

DISTILL_PROMPT = """Below are session observations about "%s" from an engineering memory system. Distill the DURABLE knowledge only: what stays true and useful next month (rules, gotchas, decisions, architecture) — not day-to-day narrative.

Observations (title | facts excerpt):
%s

Reply ONLY JSON: {"rule":"<one dense sentence, <=300 chars>","context":"<supporting detail, <=500 chars>","confidence":0.6} or {"rule":null} if nothing durable emerges."""


def phase_distill(cm_conn, sem_conn, apply: bool, hm_write_path: str | None = None) -> dict:
    hm_write_path = hm_write_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "hm_write.py")
    suppressed = {r[0] for r in sem_conn.execute("SELECT obs_id FROM suppressed_observation")}
    cutoff = int(datetime.datetime.now().timestamp()) - 30 * 86400
    ment = sem_conn.execute(
        "SELECT e.slug, m.ref FROM mention m JOIN entity e ON e.id=m.entity_id "
        "WHERE m.store='claudemem'").fetchall()
    by_entity = defaultdict(list)
    for slug, ref in ment:
        try:
            oid = int(ref)
        except ValueError:
            continue
        if oid not in suppressed:
            by_entity[slug].append(oid)
    done = {r[0]: (r[1], r[2] or 0) for r in
            sem_conn.execute("SELECT entity_slug, last_distilled_at, obs_count_at_distill FROM distillation")}
    # hub exclusion, same philosophy as sem.py: an entity mentioned by a large
    # fraction of the store names everything and distills to mush; narrow
    # entities are the point of distillation
    total_ment = sum(len(v) for v in by_entity.values()) or 1
    candidates = []
    for slug, oids in by_entity.items():
        if len(oids) < DISTILL_MIN_OBS:
            continue
        # hub semantics need a real corpus; on a tiny store every entity is "a hub"
        if total_ment >= 200 and len(oids) / total_ment > 0.08:
            continue  # hub — skip
        prev = done.get(slug)
        if prev and len(oids) - prev[1] < DISTILL_MIN_NEW:
            continue
        candidates.append((slug, oids))
    candidates.sort(key=lambda c: -len(c[1]))
    distilled = []
    for slug, oids in candidates[:DISTILL_CAP]:
        ph = ",".join("?" * min(len(oids), 40))
        rows = cm_conn.execute(
            f"SELECT title, COALESCE(facts, SUBSTR(COALESCE(text,''),1,200)), created_at_epoch "
            f"FROM observations WHERE id IN ({ph}) ORDER BY created_at_epoch DESC LIMIT 40",
            tuple(oids[:40])).fetchall()
        # 30-day filter in python: the column is millis in prod, seconds in tests
        obs = [(t, f) for t, f, ep in rows if epoch_s(ep) > cutoff][:25]
        if len(obs) < DISTILL_MIN_OBS:
            continue
        lines = "\n".join(f"- {t[:120]} | {(f or '')[:200]}" for t, f in obs)
        verdict = ask_model(DISTILL_PROMPT % (slug, lines))
        if not verdict or not isinstance(verdict.get("rule"), str) or not verdict["rule"]:
            continue
        rule = f"[distilled:{slug}] {verdict['rule']}"[:400]
        ctx = str(verdict.get("context") or "")[:800]
        try:
            conf = min(0.75, max(0.5, float(verdict.get("confidence", 0.6))))
        except (TypeError, ValueError):
            conf = 0.6
        distilled.append({"entity": slug, "rule": rule})
        if apply:
            try:
                r = subprocess.run(
                    [sys.executable, hm_write_path, "context", rule, "--context", ctx,
                     "--confidence", str(conf), "--source-session", "consolidation",
                     "--no-adjudicate", "--entity", slug],
                    capture_output=True, text=True, timeout=60)
            except subprocess.TimeoutExpired:
                log(f"[distill] hm_write timed out for {slug}")
                continue
            if r.returncode == 0:
                sem_conn.execute(
                    "INSERT INTO distillation (entity_slug, last_distilled_at, obs_count_at_distill) "
                    "VALUES (?,?,?) ON CONFLICT(entity_slug) DO UPDATE SET "
                    "last_distilled_at=excluded.last_distilled_at, "
                    "obs_count_at_distill=excluded.obs_count_at_distill",
                    (slug, now(), len(oids)))
                sem_conn.commit()
            else:
                log(f"[distill] hm_write failed for {slug}: {r.stderr[:150]}")
    return {"distill_candidates": len(candidates), "distilled": distilled}


# ---------------------------------------------------------------------------
# Phase: MEMORY.md mechanical sync (add-only; deletions are reported, never done)
# ---------------------------------------------------------------------------

def _replace_index(path: str, was: str, text: str) -> None:
    """Write `text` to a temporary file beside `path` and put it in place in one step, so that an interrupted write
    cannot leave the index empty or half written. Agent sessions edit the index at any time: if it no longer reads
    as `was`, nothing is replaced and the phase fails (the next run adds the lines)."""
    handle, temp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".MEMORY.md.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        shutil.copymode(path, temp)
        if Path(path).read_text(encoding="utf-8") != was:
            raise RuntimeError("MEMORY.md changed while the index phase ran: nothing added this run")
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def phase_index(memdir: str, apply: bool) -> dict:
    """Add a line to MEMORY.md for every note that no index links yet.

    A note counts as linked when MEMORY.md or a topic index file links it. Counting only MEMORY.md can
    duplicate existing links and overflow the index. New lines go in front of the
    `## By topic` section when there is one, and carry the day they were added."""
    idx_path = os.path.join(memdir, "MEMORY.md")
    try:
        idx = Path(idx_path).read_text(encoding="utf-8")
    except OSError:
        return {"index": "MEMORY.md unreadable — skipped"}
    link = re.compile(r"\]\(([^)]+\.md)\)")
    linked = set(link.findall(idx))
    for name in sorted(os.listdir(memdir)):
        if name.startswith("index-") and name.endswith(".md"):
            try:
                linked |= set(link.findall(Path(memdir, name).read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError) as exc:
                # never add what a topic file may already hold; raised so that the run reports the phase as failed
                raise RuntimeError(f"topic index {name} unreadable ({type(exc).__name__}): index phase skipped") from exc
    on_disk = {f for f in os.listdir(memdir)
               if f.endswith(".md") and f != "MEMORY.md" and not f.startswith("_")}
    missing = sorted(on_disk - linked)
    dangling = sorted(linked - on_disk)
    added = []
    day = datetime.datetime.now().strftime("%m-%d")
    for f in missing:
        desc = ""
        try:
            head = Path(memdir, f).read_text(encoding="utf-8")[:800]
            m = re.search(r"^description:\s*>?-?\s*(.+?)$", head, re.M)
            if m:
                desc = m.group(1).strip().strip("'\"")[:160]
        except (OSError, UnicodeDecodeError):
            pass
        title = f[:-3].replace("-", " ")
        added.append(f"- [{title}]({f}) — auto-indexed {day}: {desc or 'no description'}")
    if apply and added:
        lines = idx.rstrip("\n").split("\n") if idx.strip() else []
        at = next((n for n, line in enumerate(lines) if line.startswith("## By topic")), len(lines))
        while at > 0 and not lines[at - 1].strip():
            at -= 1  # in front of the blank lines that precede the topic section
        gap = [""] if at < len(lines) and lines[at].strip() else []
        _replace_index(idx_path, idx, "\n".join(lines[:at] + added + gap + lines[at:]) + "\n")
    return {"index_added": added if apply else [],
            "index_missing_found": missing, "index_dangling": dangling}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def run_phases(connections, phases, apply):
    hm_conn, cm_conn, sem_conn = connections
    ensure_schema(hm_conn, sem_conn)
    runners = {"hm": lambda: phase_hm(hm_conn, apply),
               "cm": lambda: phase_cm(cm_conn, sem_conn, apply) if cm_conn else {"cm_status": "not installed"},
               "distill": lambda: phase_distill(cm_conn, sem_conn, apply) if cm_conn else {"distill_status": "not installed"},
               "index": lambda: phase_index(MEMDIR, apply),
               "check": lambda: mem_check.phase_check(hm_conn, sem_conn, apply, MEMDIR)}
    metrics = {"apply": apply, "errors": {}}
    for name in phases:
        if name in ("hm", "distill", "check") and not MODEL_ALLOWED:
            LLM_STATS["llm_skipped"] += 1
        if name not in runners:
            metrics["errors"][name] = "unknown phase"
            continue
        try:
            metrics.update(runners[name]())
        except Exception as exc:
            metrics["errors"][name] = type(exc).__name__
            log(f"[{name}] FAILED {type(exc).__name__}")
    return metrics


def finish_run(sem_conn, metrics, generation):
    metrics.update(llm_stats())  # not an error: degraded phases are a warning for memory-watch, the run still succeeded
    failed = bool(metrics["errors"])
    if metrics["apply"]:
        try:
            sem_conn.execute("INSERT INTO consolidation_run (ran_at, applied, metrics) VALUES (?,?,?)",
                             (now(), 1, json.dumps(metrics, default=str)))
            sem_conn.commit()
        except Exception as exc:
            metrics["errors"]["audit"] = type(exc).__name__
            failed = True
        Path(REPORT).write_text(f"# Consolidation report — {now()}\n\n" + json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    status = "FAILED-PARTIAL" if failed else ("APPLIED" if metrics["apply"] else "DRY RUN")
    log(f"DONE {status}: " + json.dumps(metrics, default=str))
    return 1 if failed else 0


def main() -> int:
    global DRY_MODE, MODEL_ALLOWED, IN_RUN
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="mutate; default is dry-run")
    ap.add_argument("--phases", default="hm,cm,distill,index,check")
    a = ap.parse_args()
    DRY_MODE = not a.apply
    MODEL_ALLOWED = a.apply and memdir.cloud_ok() and not os.environ.get("HM_NO_LLM")
    IN_RUN = True
    reset_llm_stats()
    try:
        with ExitStack() as stack:
            if a.apply:
                from sem_refresh import refresh_lock
                stack.enter_context(refresh_lock(Path(memdir.tools_dir()) / 'logs' / 'consolidate.lock'))
            connections = []
            for path in (HM, CM, SEM):
                if path == CM and not os.path.exists(path):
                    connections.append(None)
                    continue
                if path == SEM and not os.path.exists(path) and not a.apply:
                    conn = sqlite3.connect(":memory:")
                else:
                    conn = connect(path, writable=a.apply)
                if path == SEM:
                    import sem
                    with open(sem.SCHEMA, encoding="utf-8") as schema:
                        conn.executescript(schema.read())
                connections.append(conn)
                stack.callback(conn.close)
            metrics = run_phases(connections, a.phases.split(","), a.apply)
            return finish_run(connections[2], metrics, None)
    except Exception as exc:
        print(f"CONSOLIDATION FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        IN_RUN = False


if __name__ == "__main__":
    sys.exit(main())
