#!/usr/bin/env python3
"""consolidate — nightly memory lifecycle: the stack learns instead of accreting.

Five phases, all capped, all invalidate-never-delete:

  kimi     mark structural-noise kimi-auto learnings inactive (deterministic)
  hm       merge near-duplicate hivemind learnings (LLM adjudicates clusters;
           losers get active=0 + superseded_by + expired_at)
  cm       suppress cross-session duplicate claude-mem observations via an
           OVERLAY in semantic.db (the plugin's DB is never structurally
           touched; mem_recall filters through the overlay)
  distill  episodic -> semantic: entities with enough recent observations get
           ONE distilled hivemind learning (Letta sleep-time / reflection)
  index    MEMORY.md mechanical sync: add entries for unindexed files; report
           (never delete) dangling entries

DRY RUN by default — nothing mutates without --apply. Every applied change is
recorded in semantic.db consolidation_run for audit. Protected learnings are
never expired unless their replacement is also protected.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memdir import memory_dir  # noqa: E402

HM = os.path.expanduser("~/hivemind.db")
CM = os.path.expanduser("~/.claude-mem/claude-mem.db")
SEM = os.path.expanduser("~/.claude/memory-tools/semantic.db")
MEMDIR = memory_dir()
REPORT = os.path.expanduser("~/.claude/memory-tools/consolidation-report-latest.md")
LOG = os.path.expanduser("~/.claude/memory-tools/consolidate.log")

HM_CLUSTER_CAP = 15      # LLM-adjudicated merge clusters per run
CM_SUPPRESS_CAP = 200    # observation suppressions per run
DISTILL_CAP = 3          # distilled learnings per run
DISTILL_MIN_OBS = 8      # observations an entity needs before distillation
DISTILL_MIN_NEW = 10     # new observations since last distill to re-distill
JACCARD_MERGE = 0.60     # token overlap that makes two learnings cluster
LLM_TIMEOUT = 120

KIMI_NOISE_RE = re.compile(
    r'^kimi-auto: Kimi session "(new session|proceed|continue|go|ok(ay)?|yes|no'
    r'|test|hi|hello|are you there\??|\.{1,3})"', re.I)

now = lambda: datetime.datetime.now().isoformat(timespec="seconds")


def epoch_s(v) -> float:
    """claude-mem stores created_at_epoch in MILLISECONDS; tests and other
    stores use seconds. Normalize like mem_recall.recency does."""
    v = v or 0
    return v / 1000 if v > 1e12 else v


def log(msg: str) -> None:
    line = f"{now()} {msg}"
    print(line)
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 262144:
            tail = Path(LOG).read_text()[-131072:]
            Path(LOG).write_text(tail)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL;")
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
                      ("superseded_by", "INTEGER")]:
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


# ---------------------------------------------------------------------------
# LLM helper — sem_discover's proven pattern: any failure returns None
# ---------------------------------------------------------------------------

CLAUDE_BIN = (__import__("shutil").which("claude")
              or os.path.expanduser("~/.local/bin/claude"))


def ask_model(prompt: str, timeout: int = LLM_TIMEOUT):
    """Returns a dict verdict or None. Only dicts are accepted — every prompt
    here demands a JSON object, and a list/string would crash the callers."""
    if os.environ.get("HM_NO_LLM"):
        return None
    try:
        proc = subprocess.run([CLAUDE_BIN, "-p", prompt, "--settings",
                               '{"enabledPlugins":{"claude-mem@thedotmack":false}}'],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"[llm] unavailable ({exc}); phase degrades to no-op")
        return None
    m = re.search(r"\{.*\}", proc.stdout, re.S)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(0))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Phase: kimi noise (deterministic, no LLM)
# ---------------------------------------------------------------------------

def phase_kimi(hm_conn, apply: bool) -> dict:
    rows = hm_conn.execute(
        "SELECT id, rule FROM learnings WHERE active=1 AND rule LIKE 'kimi-auto:%'").fetchall()
    noise = [(rid, rule) for rid, rule in rows if KIMI_NOISE_RE.match(rule or "")]
    if apply:
        for rid, _ in noise:
            hm_conn.execute(
                "UPDATE learnings SET active=0, expired_at=?, context="
                "COALESCE(context,'')||' [consolidation: kimi structural noise]' WHERE id=?",
                (now(), rid))
        hm_conn.commit()
    return {"kimi_noise_found": len(noise),
            "kimi_noise_expired": len(noise) if apply else 0,
            "sample": [r for _, r in noise[:3]]}


# ---------------------------------------------------------------------------
# Phase: hivemind near-duplicate merge (LLM adjudicated)
# ---------------------------------------------------------------------------

def _tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9]{4,}", (text or "").lower()))


def find_hm_clusters(hm_conn) -> list[list[tuple]]:
    """Greedy Jaccard clustering of active learnings within a category."""
    rows = hm_conn.execute(
        "SELECT id, category, rule, context, confidence, protected, created_at "
        "FROM learnings WHERE active=1 AND rule NOT LIKE 'kimi-auto:%'").fetchall()
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
        details.append({"canonical": canon, "expired": [r[0] for r in losers]})
        if apply:
            for r in losers:
                hm_conn.execute(
                    "UPDATE learnings SET active=0, superseded_by=?, expired_at=? WHERE id=?",
                    (canon, now(), r[0]))
            mc = verdict.get("merged_context")
            if mc and isinstance(mc, str):
                # APPEND — the canonical's own context is never replaced
                # (invalidate-never-delete applies to the winner too)
                hm_conn.execute(
                    "UPDATE learnings SET context=COALESCE(context,'')||' [merged '||?||': '||?||']',"
                    " valid_from=COALESCE(valid_from, created_at) WHERE id=?",
                    (now()[:10], mc[:600], canon))
            hm_conn.commit()
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

def phase_index(memdir: str, apply: bool) -> dict:
    idx_path = os.path.join(memdir, "MEMORY.md")
    try:
        idx = Path(idx_path).read_text()
    except OSError:
        return {"index": "MEMORY.md unreadable — skipped"}
    linked = set(re.findall(r"\]\(([^)]+\.md)\)", idx))
    on_disk = {f for f in os.listdir(memdir)
               if f.endswith(".md") and f != "MEMORY.md" and not f.startswith("_")}
    missing = sorted(on_disk - linked)
    dangling = sorted(linked - on_disk)
    added = []
    for f in missing:
        desc = ""
        try:
            head = Path(memdir, f).read_text()[:800]
            m = re.search(r"^description:\s*>?-?\s*(.+?)$", head, re.M)
            if m:
                desc = m.group(1).strip().strip("'\"")[:160]
        except OSError:
            pass
        title = f[:-3].replace("-", " ")
        added.append(f"- [{title}]({f}) — {desc or 'auto-indexed by consolidation'}")
    if apply and added:
        lines = idx.rstrip("\n").split("\n")
        Path(idx_path).write_text("\n".join(lines + added) + "\n")
    return {"index_added": added if apply else [],
            "index_missing_found": missing, "index_dangling": dangling}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="mutate; default is dry-run")
    ap.add_argument("--phases", default="kimi,hm,cm,distill,index")
    a = ap.parse_args()

    lock = os.path.expanduser("~/.claude/memory-tools/.consolidate.lock")
    try:
        os.mkdir(lock)
    except FileExistsError:
        import time as _t
        if os.path.getmtime(lock) < _t.time() - 3600:
            os.rmdir(lock)
            os.mkdir(lock)
        else:
            log("locked — another run in flight; exiting")
            return 0
    metrics = {"apply": a.apply, "errors": {}}
    try:
        hm_conn, cm_conn, sem_conn = connect(HM), connect(CM), connect(SEM)
        ensure_schema(hm_conn, sem_conn)
        phases = a.phases.split(",")
        # each phase is isolated: one phase crashing must not stop the others,
        # and the audit row + report are written NO MATTER WHAT (an untested
        # alarm is indistinguishable from no alarm — hard-earned lesson)
        runners = {"kimi": lambda: phase_kimi(hm_conn, a.apply),
                   "hm": lambda: phase_hm(hm_conn, a.apply),
                   "cm": lambda: phase_cm(cm_conn, sem_conn, a.apply),
                   "distill": lambda: phase_distill(cm_conn, sem_conn, a.apply),
                   "index": lambda: phase_index(MEMDIR, a.apply)}
        for name in phases:
            if name not in runners:
                continue
            try:
                metrics.update(runners[name]())
            except Exception as exc:
                metrics["errors"][name] = f"{type(exc).__name__}: {exc}"[:300]
                log(f"[{name}] FAILED {exc}")
            try:
                os.utime(lock)  # keep the lock fresh through long LLM phases
            except OSError:
                pass
        failed = bool(metrics["errors"])
        try:
            sem_conn.execute(
                "INSERT INTO consolidation_run (ran_at, applied, metrics) VALUES (?,?,?)",
                (now(), 1 if a.apply else 0, json.dumps(metrics, default=str)))
            sem_conn.commit()
        except Exception as exc:
            log(f"audit row write failed: {exc}")
        status = ("FAILED-PARTIAL " if failed else "") + ("APPLIED" if a.apply else "DRY RUN")
        Path(REPORT).write_text(
            f"# Consolidation report — {now()} ({status})\n\n"
            + json.dumps(metrics, indent=2, default=str) + "\n")
        log(f"DONE {status}: " + json.dumps(
            {k: v for k, v in metrics.items() if not isinstance(v, (list, dict))}))
        if failed:
            log(f"LAST_RUN_FAILED phases={list(metrics['errors'])}")
        for c in (hm_conn, cm_conn, sem_conn):
            c.close()
        return 0
    except Exception as exc:
        log(f"FATAL {exc}")
        try:
            Path(REPORT).write_text(
                f"# Consolidation report — {now()} (FATAL — nothing/partial ran)\n\n"
                f"{type(exc).__name__}: {exc}\n\n" + json.dumps(metrics, indent=2, default=str) + "\n")
        except OSError:
            pass
        return 0  # a broken consolidation must never fail a schedule
    finally:
        try:
            os.rmdir(lock)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
