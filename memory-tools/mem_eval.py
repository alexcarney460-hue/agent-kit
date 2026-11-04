#!/usr/bin/env python3
"""mem_eval — first real recall-quality metric for the memory system.

Runs a golden query set (facts this workspace actually knows) through mem_recall
and measures Hit@k: did the expected source/keyword surface in the top-k of ANY
store? Today's only number was a cost metric ("92% token savings"); this is the
first quality signal. Read-only (passes --no-bump so eval doesn't skew counters).

Usage: mem_eval.py [--k 8]
"""
import subprocess, sys, os, argparse, json

HERE = os.path.dirname(os.path.abspath(__file__))

# (query, list-of-acceptable substrings that should appear in the recall output)
GOLDEN = [
    # Replace with golden queries against YOUR memory once it has content, e.g.:
    ("example project build status", ["example", "build"]),
    ("my coding style preference", ["style", "preference"]),
]

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--k", type=int, default=8)
    a = ap.parse_args()
    hits = 0
    print(f"=== mem_eval: {len(GOLDEN)} golden queries, Hit@{a.k} ===")
    for q, expect in GOLDEN:
        out = subprocess.run([sys.executable, os.path.join(HERE, "mem_recall.py"), q,
                              "--k", str(a.k), "--no-bump"],
                             capture_output=True, text=True).stdout.lower()
        hit = any(e.lower() in out for e in expect)
        hits += hit
        print(f" [{'HIT ' if hit else 'MISS'}] {q!r}  (looked for {expect})")
    pct = 100.0 * hits / len(GOLDEN)
    print(f"\nHit@{a.k} = {hits}/{len(GOLDEN)} = {pct:.0f}%")
    return 0 if pct >= 80 else 1

if __name__ == "__main__":
    sys.exit(main())
