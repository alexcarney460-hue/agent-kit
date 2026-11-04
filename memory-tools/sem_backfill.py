#!/usr/bin/env python3
"""sem_backfill — link entities to the records that are about them.

Scans hivemind learnings, claude-mem observations and the file memories, matches
canonical aliases against their text, and writes rows into semantic.mention.

That mention table is the whole point: once a record is linked to an entity, a
query that resolves to that entity retrieves the record even with ZERO lexical
overlap — which is precisely what the LIKE-based retriever cannot do.

READ-ONLY against hivemind and claude-mem. Only semantic.db is written.

Usage: sem_backfill.py [--dry-run] [--limit-obs N]
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys

import sem

from memdir import memory_dir

HM = os.path.expanduser("~/hivemind.db")
CM = os.path.expanduser("~/.claude-mem/claude-mem.db")
MEMDIR = memory_dir()

# An alias this short matches far too much prose to be trusted on its own.
MIN_ALIAS = 3
# Aliases that are real words in ordinary English cause false links; require that
# these appear with more specific company of the same entity before counting.
# EDIT to match your world: list every entity alias that is also a common word.
AMBIGUOUS = {"edge", "tank", "linear", "atlas", "compass", "origin", "brain", "q"}


def open_ro(path: str) -> sqlite3.Connection | None:
    if not os.path.exists(path):
        sys.stderr.write(f"[skip] {path} not found\n")
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    except sqlite3.OperationalError:
        # mode=ro fails on a WAL db with no -shm sidecar (routine on WAL dbs)
        conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.text_factory = lambda b: b.decode("utf-8", "replace") if isinstance(b, (bytes, bytearray)) else b
    return conn


def build_matcher(conn: sqlite3.Connection) -> tuple[re.Pattern[str], dict[str, int], set[str]]:
    """One compiled alternation over every alias, longest-first."""
    rows = conn.execute(
        "SELECT a.alias, a.entity_id FROM alias a JOIN entity e ON e.id=a.entity_id"
    ).fetchall()
    amap: dict[str, int] = {}
    for alias, eid in rows:
        a = re.sub(r"\s+", " ", alias.strip().lower())
        if len(a) >= MIN_ALIAS:
            amap[a] = eid
    if not amap:
        raise SystemExit("no aliases — run: sem.py seed")
    ordered = sorted(amap, key=len, reverse=True)
    pattern = re.compile(r"\b(" + "|".join(re.escape(a) for a in ordered) + r")\b")
    return pattern, amap, {a for a in amap if a in AMBIGUOUS}


def hits_for(text: str, pattern: re.Pattern[str], amap: dict[str, int],
             ambiguous: set[str], discriminable: set[int]) -> dict[int, float]:
    """Entity -> weight for one record. Longer, rarer aliases weigh more.

    `discriminable` is the set of entities that own at least one NON-ambiguous
    alias. For those, an ambiguous common word alone is not evidence — we expect
    the better name to appear. For an entity whose only name IS an ambiguous word
    (say ventures named Atlas or Compass), dropping it would mean it can never
    link at all, so the match counts at a reduced weight instead.
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reset", action="store_true", help="clear mentions before rebuilding")
    ap.add_argument("--limit-obs", type=int, default=20000)
    a = ap.parse_args()

    sconn = sem.connect()
    pattern, amap, ambiguous = build_matcher(sconn)
    discriminable = {eid for al, eid in amap.items() if al not in ambiguous}
    print(f"matcher: {len(amap)} aliases ({len(ambiguous)} ambiguous, "
          f"{len(discriminable)} entities have a discriminating name)")
    if a.reset:
        sconn.execute("DELETE FROM mention"); sconn.commit(); print("cleared prior mentions")

    written = 0
    counts: dict[str, int] = {"hivemind": 0, "claudemem": 0, "file": 0}

    def record(store: str, ref: str, text: str) -> None:
        nonlocal written
        for eid, w in hits_for(text, pattern, amap, ambiguous, discriminable).items():
            counts[store] += 1
            if not a.dry_run:
                sem.add_mention(sconn, eid, store, ref, w)
            written += 1

    hc = open_ro(HM)
    if hc:
        for rid, rule, ctx in hc.execute("SELECT id, rule, COALESCE(context,'') FROM learnings WHERE active=1"):
            record("hivemind", str(rid), f"{rule} {ctx}")
        hc.close()

    cc = open_ro(CM)
    if cc:
        rows = cc.execute(
            "SELECT id, COALESCE(title,''), COALESCE(text,''), COALESCE(facts,''), "
            "COALESCE(narrative,''), COALESCE(concepts,'') FROM observations LIMIT ?", (a.limit_obs,)
        ).fetchall()
        for oid, title, text, facts, narr, concepts in rows:
            record("claudemem", str(oid), " ".join((title, text, facts, narr, concepts)))
        cc.close()
        print(f"scanned {len(rows)} observations")

    if os.path.isdir(MEMDIR):
        for fn in sorted(os.listdir(MEMDIR)):
            if not fn.endswith(".md"):
                continue
            body = open(os.path.join(MEMDIR, fn), encoding="utf-8", errors="ignore").read()
            # the filename itself is a strong signal about what the memory is for
            record("file", fn, f"{fn.replace('-', ' ')} {body}")

    if not a.dry_run:
        sconn.commit()
    print(f"\n{'would write' if a.dry_run else 'wrote'} {written} mentions")
    for store, n in counts.items():
        print(f"  {store:<10} {n}")

    top = sconn.execute(
        "SELECT e.slug, count(*) c FROM mention m JOIN entity e ON e.id=m.entity_id "
        "GROUP BY e.slug ORDER BY c DESC LIMIT 12").fetchall()
    print("\ntop-linked entities:")
    for slug, c in top:
        print(f"  {slug:<34} {c}")
    orphans = sconn.execute(
        "SELECT slug FROM entity WHERE id NOT IN (SELECT entity_id FROM mention)").fetchall()
    if orphans:
        print(f"\nentities with no mentions ({len(orphans)}): {', '.join(o[0] for o in orphans)}")
    sconn.close()


if __name__ == "__main__":
    main()
