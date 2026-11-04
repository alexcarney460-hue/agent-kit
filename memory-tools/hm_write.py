#!/usr/bin/env python3
"""hm_write — safe, parameterized, provenance-stamped hivemind learning writer.

Fixes the silent-drop bug: the documented `sqlite3 hivemind.db "INSERT ... '$RULE'"`
path throws a syntax error (and drops the write) on any apostrophe. This uses
parameter binding, WAL + busy_timeout + retry, auto-stamps provenance, auto-flags
PROTECT_RE-matching content as protected, and adjudicates against
near-neighbors at write time: DUPLICATE reinforces the existing row instead of
inserting; SUPERSEDES inserts and expires the target (active=0 + superseded_by —
invalidate, never delete); CONTRADICTS inserts both and warns loudly. Rows are
never DELETEd. --no-adjudicate or HM_NO_LLM=1 restores plain-INSERT behavior.

Usage:
  hm_write.py CATEGORY "RULE" [--context "..."] [--confidence 0.8]
              [--source-session ID] [--device mac]
"""
from __future__ import annotations
import argparse, os, sqlite3, sys, time, datetime, random, re

DB = os.path.expanduser("~/hivemind.db")
PROTECT_RE = re.compile(r"client-a|project-b|partner-name", re.I)  # EDIT: keywords whose memories get pinned/protected — your ventures, clients, partners

def connect():
    conn = sqlite3.connect(DB, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.text_factory = lambda b: b.decode("utf-8", "replace") if isinstance(b, (bytes, bytearray)) else b
    return conn

def with_retry(fn, tries=6):
    for i in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and i < tries - 1:
                time.sleep(0.05 * (2 ** i) + random.random() * 0.05)
                continue
            raise

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("category")
    ap.add_argument("rule")
    ap.add_argument("--context", default=None)
    ap.add_argument("--confidence", type=float, default=0.8)
    ap.add_argument("--source-session", default=os.environ.get("CLAUDE_SESSION_ID", "manual"))
    ap.add_argument("--device", default=os.environ.get("MEM_DEVICE", "local"))
    ap.add_argument("--entity", action="append", default=[],
                    help="entity slug to link (repeatable); auto-detected too")
    ap.add_argument("--no-adjudicate", action="store_true",
                    help="skip the write-time LLM neighbor adjudication")
    a = ap.parse_args()

    blob = f"{a.rule} {a.context or ''}"
    protected = 1 if PROTECT_RE.search(blob) else 0
    now = datetime.datetime.now().isoformat(timespec="seconds")
    src = f"{a.source_session}@{a.device}"

    conn = connect()
    _ensure_bitemporal(conn)
    # neighbor scan: same category, high word overlap (was a WARN; now feeds
    # the Mem0-style write-time adjudication below)
    neighbors = []
    words = set(w for w in re.findall(r"[a-z0-9]{4,}", a.rule.lower()))
    if words:
        rows = with_retry(lambda: conn.execute(
            "SELECT id, rule, context, protected FROM learnings WHERE active=1 AND category=?",
            (a.category,)).fetchall())
        for rid, r, ctx, prot in rows:
            rw = set(re.findall(r"[a-z0-9]{4,}", (r or "").lower()))
            if rw and len(words & rw) / max(1, len(words)) >= 0.6:
                neighbors.append((rid, r, ctx, prot))
                sys.stderr.write(f"[hm_write WARN] near-neighbor learning {rid}: {r[:80]!r}\n")

    verdict = None
    if neighbors and os.environ.get("HM_ADJUDICATE") == "1" and not a.no_adjudicate and not os.environ.get("HM_NO_LLM"):
        verdict = adjudicate(a.rule, a.context, neighbors[:3])

    if verdict and verdict.get("verdict") == "DUPLICATE" and verdict.get("target") in {n[0] for n in neighbors}:
        t = verdict["target"]
        # retry-idempotent: compute the absolute target value once, so a locked
        # commit retried does not double-increment
        cur = with_retry(lambda: conn.execute(
            "SELECT times_applied FROM learnings WHERE id=?", (t,)).fetchone())
        nv = (cur[0] if cur else 0) + 1
        with_retry(lambda: (conn.execute(
            "UPDATE learnings SET times_applied=?, last_applied=? WHERE id=?",
            (nv, now, t)), conn.commit()))
        conn.close()
        print(f"DEDUP: duplicate of learning id={t} — reinforced it instead of inserting")
        return

    def ins():
        cur = conn.execute(
            "INSERT INTO learnings (category, rule, context, source_session, confidence, "
            "created_at, active, protected, times_applied, times_helpful, recorded_at, valid_from) "
            "VALUES (?,?,?,?,?,?,1,?,0,0,?,?)",
            (a.category, a.rule, a.context, src, a.confidence, now, protected, now, now))
        conn.commit()
        return cur.lastrowid
    rid = with_retry(ins)

    if verdict and verdict.get("verdict") == "SUPERSEDES" and verdict.get("target") in {n[0] for n in neighbors}:
        t = verdict["target"]
        t_prot = next(n[3] for n in neighbors if n[0] == t)
        if t_prot and not protected:
            sys.stderr.write(f"[hm_write WARN] verdict SUPERSEDES protected learning {t} but new row is "
                             f"unprotected — NOT expiring it; review manually\n")
        else:
            with_retry(lambda: (conn.execute(
                "UPDATE learnings SET active=0, superseded_by=?, expired_at=? WHERE id=?",
                (rid, now, t)), conn.commit()))
            print(f"SUPERSEDED learning id={t} (expired, superseded_by={rid})")
    elif verdict and verdict.get("verdict") == "CONTRADICTS":
        sys.stderr.write(f"[hm_write CONTRADICTION] new learning {rid} contradicts learning "
                         f"{verdict.get('target')} — both kept active; resolve manually\n")

    conn.close()
    print(f"SAVED learning id={rid} category={a.category} protected={protected} src={src}")

    # Semantic layer: link the new learning to the entities it is about, so it is
    # retrievable by entity immediately instead of waiting for the next backfill.
    # Entirely optional — a missing or broken layer never fails a write.
    linked = link_entities(rid, blob, a.entity)
    if linked:
        print(f"  linked to entities: {', '.join(linked)}")


def _ensure_bitemporal(conn) -> None:
    """Bi-temporal columns; idempotent, shared shape with consolidate.py."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(learnings)")}
    for col, decl in [("valid_from", "TEXT"), ("valid_to", "TEXT"),
                      ("recorded_at", "TEXT"), ("expired_at", "TEXT"),
                      ("superseded_by", "INTEGER")]:
        if col not in cols:
            conn.execute(f"ALTER TABLE learnings ADD COLUMN {col} {decl}")
    conn.commit()


ADJUDICATE_PROMPT = """A memory system is about to store a new rule. Compare it against its nearest existing neighbors and decide the relationship.

NEW: %s
CONTEXT: %s

NEIGHBORS:
%s

Reply ONLY JSON: {"verdict":"NEW"|"DUPLICATE"|"SUPERSEDES"|"CONTRADICTS","target":<neighbor id or null>,"reason":"<15 words max>"}
DUPLICATE = same fact, nothing new. SUPERSEDES = new one replaces/corrects the neighbor. CONTRADICTS = they cannot both be true and it is unclear which is right. NEW = related but distinct. If unsure, NEW."""


def adjudicate(rule: str, context: str | None, neighbors: list) -> dict | None:
    """Write-time Mem0-style adjudication. Any failure returns None (= plain ADD)."""
    import json as _json
    import subprocess
    lines = "\n".join(f"id={rid}: {(r or '')[:200]} | {(c or '')[:150]}" for rid, r, c, _ in neighbors)
    try:
        proc = subprocess.run(
            ["claude", "-p", ADJUDICATE_PROMPT % (rule[:400], (context or "")[:300], lines),
             "--settings", '{"enabledPlugins":{"claude-mem@thedotmack":false}}'],
            capture_output=True, text=True, timeout=45)
        m = re.search(r"\{.*\}", proc.stdout, re.S)
        v = _json.loads(m.group(0)) if m else None
        if v and v.get("verdict") in ("NEW", "DUPLICATE", "SUPERSEDES", "CONTRADICTS"):
            sys.stderr.write(f"[hm_write adjudicate] {v['verdict']} target={v.get('target')} ({v.get('reason','')})\n")
            return v
    except Exception as exc:
        sys.stderr.write(f"[hm_write adjudicate] unavailable ({exc}); treating as NEW\n")
    return None


def link_entities(rid: int, blob: str, explicit: list[str]) -> list[str]:
    """Attach mentions for this learning. Returns the entity slugs linked."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import sem
    except Exception:
        return []
    if not sem.available():
        return []
    try:
        sconn = sem.connect(create=False)
        slugs: list[str] = []
        for slug in explicit:
            try:
                sem.add_mention(sconn, sem.entity_id(sconn, slug), "hivemind", str(rid), 1.0)
                slugs.append(slug)
            except SystemExit:
                sys.stderr.write(f"[hm_write WARN] unknown entity slug {slug!r} — skipped\n")
        # auto-detect from the text, so the common case needs no flags at all
        for m in sem.resolve(sconn, blob, soft=False):
            if m.slug not in slugs:
                sem.add_mention(sconn, m.entity_id, "hivemind", str(rid), 0.8)
                slugs.append(m.slug)
        sconn.commit()
        sconn.close()
        return slugs
    except Exception as exc:
        sys.stderr.write(f"[hm_write] semantic link skipped: {exc}\n")
        return []

if __name__ == "__main__":
    main()
