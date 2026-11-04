#!/usr/bin/env python3
"""ppr — Personalized PageRank over the mention graph (HippoRAG-2 style).

The semantic layer's one-hop entity boost with hub-exclusion + IDF was a
hand-tuned approximation of what a graph walk does naturally: mass flowing from
query entities through the bipartite entity↔record graph, hubs diluting
themselves by degree. This does the walk properly with scipy sparse power
iteration. ~19k mentions builds in ~50ms; the walk converges in <20 iterations.

Used by mem_recall as one ranked signal into RRF — it does not replace the
one-hop boosts, fusion arbitrates.
"""
from __future__ import annotations

import os
import sqlite3
import sys

SEM = os.path.expanduser("~/.claude/memory-tools/semantic.db")
DAMPING = 0.85
ITERS = 25
TOL = 1e-8


def ranks(seed_entity_ids: dict[int, float], top: int = 200):
    """PPR scores for records given {entity_id: seed_weight}.
    Returns ranked [((store, ref), score)] or None on any failure (fail open)."""
    if not seed_entity_ids:
        return None
    try:
        import numpy as np
        from scipy.sparse import csr_matrix

        try:
            conn = sqlite3.connect(f"file:{SEM}?mode=ro", uri=True, timeout=8)
        except sqlite3.OperationalError:
            # mode=ro fails on a WAL db with no -shm sidecar (routine here)
            conn = sqlite3.connect(SEM, timeout=8)
        rows = conn.execute(
            "SELECT entity_id, store, ref, COALESCE(weight, 0.5) FROM mention "
            "WHERE store IN ('hivemind','claudemem','file')").fetchall()
        edges = conn.execute("SELECT src, dst FROM edge").fetchall()
        conn.close()
        if not rows:
            return None

        # node index: entities first, then records
        ent_ids = sorted({r[0] for r in rows} | {e for pair in edges for e in pair})
        ent_ix = {e: i for i, e in enumerate(ent_ids)}
        rec_keys = sorted({(r[1], r[2]) for r in rows})
        rec_ix = {k: len(ent_ids) + i for i, k in enumerate(rec_keys)}
        n = len(ent_ids) + len(rec_keys)

        src, dst, w = [], [], []
        for eid, store, ref, weight in rows:
            a, b = ent_ix[eid], rec_ix[(store, ref)]
            src += [a, b]; dst += [b, a]; w += [weight, weight]
        for s_, d_ in edges:  # typed entity-entity relations join the walk
            if s_ in ent_ix and d_ in ent_ix:
                src += [ent_ix[s_], ent_ix[d_]]
                dst += [ent_ix[d_], ent_ix[s_]]
                w += [1.0, 1.0]

        A = csr_matrix((w, (src, dst)), shape=(n, n))
        # column-normalize: each node spreads its mass over its out-links —
        # this is exactly the hub dilution the old heuristics hand-coded
        col = np.asarray(A.sum(axis=0)).ravel()
        col[col == 0] = 1.0
        A = A.multiply(1.0 / col).tocsr()

        s = np.zeros(n)
        for eid, weight in seed_entity_ids.items():
            if eid in ent_ix:
                s[ent_ix[eid]] = max(weight, 0.05)
        if s.sum() == 0:
            return None
        s /= s.sum()

        p = s.copy()
        for _ in range(ITERS):
            p_next = (1 - DAMPING) * s + DAMPING * (A @ p)
            if abs(p_next - p).sum() < TOL:
                p = p_next
                break
            p = p_next

        rec_scores = [(k, float(p[ix])) for k, ix in rec_ix.items() if p[ix] > 0]
        rec_scores.sort(key=lambda kv: -kv[1])
        return rec_scores[:top]
    except Exception as exc:
        sys.stderr.write(f"[ppr skipped: {exc}]\n")
        return None
