#!/usr/bin/env python3
"""sem_eval — does the semantic layer actually retrieve better than string match?

Design: synonym-equivalence. For each case we have a CANONICAL phrasing that the
old lexical retriever handles well (the record literally contains those words),
and a SYNONYM phrasing a person would plausibly use that shares no vocabulary
with the records at all.

The canonical query's top-k defines the gold set. We then ask: how much of that
gold set does the synonym query recover, with the entity layer off vs on?

A retriever that only matches strings scores ~0 on every one of these, because
that is exactly the failure being fixed. Nothing here is graded on a curve: the
gold set is produced by the SAME retriever, so this measures the layer and not
my taste in results.

Usage: sem_eval.py [--k 10]
"""
from __future__ import annotations

import argparse

import mem_recall
import sem

# (label, canonical phrasing -> defines gold, synonym phrasing -> the real test)
# BARE cases isolate name resolution: the two phrasings share no words at all,
# so a lexical retriever has literally nothing to go on.
# These are placeholder pairs for the fictional seed world — EDIT to real
# entities from YOUR memory once it has content, or every case reports no gold.
CASES: list[tuple[str, str, str]] = [
    ("person alias", "Sample Person", "example-person"),
    ("company alias", "Example Co", "example-organization"),
    ("document alias", "example service agreement", "sample document"),
    ("project alias", "Example Project", "sample-project"),
]


def run(query: str, k: int, semantic: bool) -> list[tuple[str, str]]:
    """Return ranked (store, ref) pairs from the real retriever."""
    tg = mem_recall.terms(query)
    if not tg:
        return []
    boosts: dict[tuple[str, str], float] = {}
    if semantic:
        boosts, _ = sem.recall_boosts(query)
    out: list[tuple[str, str]] = []
    for _s, rid, *_ in mem_recall.hivemind(query, tg, boosts)[:k]:
        out.append(("hivemind", str(rid)))
    cm, _mode = mem_recall.claudemem(query, tg, boosts)
    for _s, oid, *_ in cm[:k]:
        out.append(("claudemem", str(oid)))
    for _s, fn, *_ in mem_recall.files(query, tg, boosts)[:k]:
        out.append(("file", fn))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=10)
    a = ap.parse_args()

    print(f"=== semantic-layer A/B — synonym recovery @k={a.k} ===\n")
    print(f"{'case':<28} {'gold':>5} {'lexical':>9} {'entity':>8}   verdict")
    print("-" * 74)

    tot_lex = tot_sem = tot_gold = 0
    for label, canonical, synonym in CASES:
        gold = set(run(canonical, a.k, semantic=False))
        if not gold:
            print(f"{label:<28} {'--':>5} {'':>9} {'':>8}   no gold (canonical found nothing)")
            continue
        lex = set(run(synonym, a.k, semantic=False)) & gold
        sm = set(run(synonym, a.k, semantic=True)) & gold
        tot_gold += len(gold); tot_lex += len(lex); tot_sem += len(sm)
        delta = len(sm) - len(lex)
        verdict = "same" if delta == 0 else (f"+{delta} recovered" if delta > 0 else f"{delta} LOST")
        print(f"{label:<28} {len(gold):>5} {len(lex):>9} {len(sm):>8}   {verdict}")

    print("-" * 74)
    if tot_gold:
        print(f"{'TOTAL':<28} {tot_gold:>5} {tot_lex:>9} {tot_sem:>8}")
        print(f"\nsynonym recall: lexical {tot_lex / tot_gold:.1%}  ->  entity {tot_sem / tot_gold:.1%}")
        if tot_lex:
            print(f"improvement: {tot_sem / tot_lex:.2f}x")
        else:
            print("improvement: from zero — lexical recovered nothing on any synonym query")


if __name__ == "__main__":
    main()
