#!/usr/bin/env python3
"""ppr — Personalized PageRank over the mention graph (local3367, HippoRAG 2).

The semantic layer's one-hop entity boost with hub-exclusion + IDF was a
hand-tuned approximation of what a graph walk does naturally: mass flowing from
query entities through the bipartite entity↔record graph, hubs diluting
themselves by degree. This does the walk properly with scipy sparse power
iteration. ~19k mentions builds in ~50ms; the walk converges in <20 iterations.

Used by mem_recall as one ranked signal into RRF — it does not replace the
one-hop boosts, fusion arbitrates.

Astra Batch 2: the walk consumes the SAME coherent eligibility view as every
other reader (graph_policy.EligibilityView): excluded, inactive and missing
records are removed BEFORE degrees are computed, owned automatic links join
only in `active` mode, and there is no writable fallback. When a complete
eligible view cannot be built, the signal is unavailable (None) — never a
clipped graph.
"""
from __future__ import annotations

import os
import memdir
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graph_policy  # noqa: E402

SEM = memdir.semantic_db()
DAMPING = 0.85
ITERS = 25
TOL = 1e-8


def ranks(seed_entity_ids: dict[int, float], top: int = 200,
          view: graph_policy.EligibilityView | None = None):
    """PPR scores for records given {entity_id: seed_weight}.
    Returns ranked [((store, ref), score)] or None when the signal is
    unavailable (no seeds, no eligible view, no numpy/scipy)."""
    if not seed_entity_ids:
        return None
    try:
        import numpy as np
        from scipy.sparse import csr_matrix

        if view is None:
            view = graph_policy.load_view(None, graph_policy.Sources(sem_db=SEM))
        rows = [(eid, store, ref, weight) for eid, store, ref, weight, _own in view.links
                if store in graph_policy.GRAPH_STORES]
        edges = [(s, d) for s, d, _rel in view.edges]
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
    except ImportError:
        return None
    except graph_policy.GraphUnavailable as exc:
        sys.stderr.write(f"[ppr unavailable: {exc.reason}]\n")
        return None
    except Exception as exc:
        sys.stderr.write(f"[ppr skipped: {exc}]\n")
        return None
