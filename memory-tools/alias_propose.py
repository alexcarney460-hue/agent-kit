#!/usr/bin/env python3
"""alias_propose — MinHash/LSH alias candidates for the semantic layer.

The Graphiti/Cognee-converged recipe: character-shingle MinHash signatures over
every existing entity alias; frequent capitalized phrases from recent records
become candidates; a candidate whose Jaccard similarity to some entity's alias
set clears the bar is PROPOSED as an alias for that entity.

Nothing is auto-added: proposals land in sem-alias-candidates.md for review
(`sem.py alias <slug> "<name>"` accepts them). Hand-curated aliases stay ground
truth — this just stops recall misses from being the discovery mechanism.
Proposals are cached so the same string is never re-proposed.
"""
from __future__ import annotations

import datetime
import os
import re
import sqlite3
import sys

SEM = os.path.expanduser("~/.claude/memory-tools/semantic.db")
HM = os.path.expanduser("~/hivemind.db")
CM = os.path.expanduser("~/.claude-mem/claude-mem.db")
OUT = os.path.expanduser("~/.claude/memory-tools/sem-alias-candidates.md")

N_HASH = 64
SHINGLE = 3
JACCARD_PROPOSE = 0.55   # estimated via MinHash; propose-for-review bar
MAX_PROPOSALS = 10       # per run
PHRASE_RE = re.compile(r"\b([A-Z][A-Za-z0-9&.-]{2,}(?:[ ][A-Z][A-Za-z0-9&.-]{2,}){0,3})\b")

_PRIMES = (2147483647, 402653189)


def _shingles(s: str) -> set:
    s = re.sub(r"\s+", " ", s.lower().strip())
    if len(s) < SHINGLE:
        return {s}
    return {s[i:i + SHINGLE] for i in range(len(s) - SHINGLE + 1)}


def _stable_hash(s: str) -> int:
    # python's str hash() is per-process randomized (PYTHONHASHSEED) — MinHash
    # signatures must be deterministic across runs
    import zlib
    return zlib.crc32(s.encode("utf-8", "replace"))


def _minhash(sh: set) -> tuple:
    hashes = [_stable_hash(g) for g in sh]
    sig = []
    for i in range(N_HASH):
        best = min(((h ^ (i * _PRIMES[1])) % _PRIMES[0]) for h in hashes)
        sig.append(best)
    return tuple(sig)


def _est_jaccard(a: tuple, b: tuple) -> float:
    return sum(1 for x, y in zip(a, b) if x == y) / N_HASH


def main() -> int:
    try:
        sem_conn = sqlite3.connect(SEM, timeout=8)
        sem_conn.execute(
            "CREATE TABLE IF NOT EXISTS alias_proposal_seen ("
            " phrase TEXT PRIMARY KEY, decided TEXT, created_at TEXT)")
        entities = {}
        for slug, name in sem_conn.execute(
                "SELECT e.slug, a.alias FROM alias a JOIN entity e ON e.id=a.entity_id"):
            entities.setdefault(slug, set()).add(name)
        for slug, name in sem_conn.execute("SELECT slug, name FROM entity"):
            entities.setdefault(slug, set()).add(name)
        seen = {r[0] for r in sem_conn.execute("SELECT phrase FROM alias_proposal_seen")}
        known = {n.lower() for names in entities.values() for n in names}
    except Exception as exc:
        sys.stderr.write(f"[alias_propose] semantic layer unavailable: {exc}\n")
        return 0

    sigs = {slug: [_minhash(_shingles(n)) for n in names]
            for slug, names in entities.items()}

    # candidate phrases from recent records
    texts = []
    lenient = lambda b: b.decode("utf-8", "replace") if isinstance(b, (bytes, bytearray)) else b
    try:
        from embed_index import open_ro
        c = open_ro(HM); c.text_factory = lenient
        texts += [r[0] for r in c.execute(
            "SELECT rule || ' ' || COALESCE(context,'') FROM learnings "
            "WHERE active=1 ORDER BY id DESC LIMIT 300")]
        c.close()
        c = open_ro(CM); c.text_factory = lenient
        texts += [r[0] for r in c.execute(
            "SELECT COALESCE(title,'') || ' ' || COALESCE(facts,'') FROM observations "
            "ORDER BY id DESC LIMIT 600")]
        c.close()
    except Exception as exc:
        sys.stderr.write(f"[alias_propose] store read failed: {exc}\n")

    freq = {}
    for t in texts:
        for m in PHRASE_RE.finditer(t or ""):
            p = m.group(1).strip()
            if 3 <= len(p) <= 60:
                freq[p] = freq.get(p, 0) + 1

    proposals = []
    for phrase, n in sorted(freq.items(), key=lambda kv: -kv[1]):
        if n < 3 or phrase in seen or phrase.lower() in known:
            continue
        psig = _minhash(_shingles(phrase))
        best_slug, best_j = None, 0.0
        for slug, slug_sigs in sigs.items():
            for s in slug_sigs:
                j = _est_jaccard(psig, s)
                if j > best_j:
                    best_slug, best_j = slug, j
        if best_slug and best_j >= JACCARD_PROPOSE:
            proposals.append((phrase, best_slug, round(best_j, 2), n))
        if len(proposals) >= MAX_PROPOSALS:
            break

    now = datetime.datetime.now().isoformat(timespec="seconds")
    for phrase, *_rest in proposals:
        sem_conn.execute(
            "INSERT OR IGNORE INTO alias_proposal_seen (phrase, decided, created_at) "
            "VALUES (?, 'proposed', ?)", (phrase, now))
    sem_conn.commit()
    sem_conn.close()

    if proposals:
        with open(OUT, "a") as f:
            f.write(f"\n## {now}\n")
            for phrase, slug, j, n in proposals:
                f.write(f'- `sem.py alias {slug} "{phrase}"`  (minhash-jaccard {j}, seen {n}x)\n')
    print(f"proposed {len(proposals)} alias candidates")
    return 0


if __name__ == "__main__":
    import argparse
    argparse.ArgumentParser(description=__doc__).parse_args()  # so --help doesn't run the job
    sys.exit(main())
