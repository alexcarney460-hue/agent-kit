#!/usr/bin/env python3
"""Tests for the hybrid retrieval path: RRF fusion, PPR, embedding
index. Standalone or pytest; no Ollama, no live stores — everything stubbed."""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mem_recall  # noqa: E402
import embed_index  # noqa: E402
import ppr as ppr_mod  # noqa: E402
import numpy as np  # noqa: E402


class TestRRF(unittest.TestCase):
    def _fuse(self, hm=None, cm=None, fl=None, vec=None, pprl=None, sup=None, red=None):
        with mock.patch.object(mem_recall, "hm_redirects", return_value=red or {}), \
             mock.patch.object(mem_recall, "hivemind", return_value=hm or []), \
             mock.patch.object(mem_recall, "claudemem", return_value=(cm or [], "fts5")), \
             mock.patch.object(mem_recall, "files", return_value=fl or []), \
             mock.patch.object(mem_recall, "_vector_list", return_value=vec), \
             mock.patch.object(mem_recall, "_ppr_list", return_value=pprl), \
             mock.patch.object(mem_recall, "suppressed_obs_ids", return_value=sup or set()), \
             mock.patch.object(mem_recall, "sem", None):
            return mem_recall.fused_search("q", k=10)

    def test_multi_signal_item_outranks_single_signal(self):
        hm = [(9.0, 7, 0, "context", "rule seven", False, True),
              (8.0, 8, 0, "context", "rule eight", False, True)]
        vec = [("hivemind", "8"), ("hivemind", "99")]
        ranked = self._fuse(hm=hm, vec=vec)
        self.assertEqual(ranked[0][:2], ("hivemind", "8"))   # in both lists
        keys = [(s, r) for s, r, _ in ranked]
        self.assertIn(("hivemind", "99"), keys)               # vector-only still present

    def test_refs_are_strings_and_stores_merge(self):
        cm = [(5.0, 42, 0, "obs title", False)]
        vec = [("claudemem", "42")]
        ranked = self._fuse(cm=cm, vec=vec)
        self.assertEqual(len(ranked), 1)                      # int 42 and str "42" merged
        self.assertIsInstance(ranked[0][1], str)

    def test_suppressed_never_in_fused(self):
        cm = [(5.0, 42, 0, "dup", False), (4.0, 7, 0, "canon", False)]
        ranked = self._fuse(cm=cm, sup={42})
        keys = [(s, r) for s, r, _ in ranked]
        self.assertNotIn(("claudemem", "42"), keys)
        self.assertIn(("claudemem", "7"), keys)

    def test_vector_none_degrades_cleanly(self):
        hm = [(9.0, 1, 0, "context", "only signal", False, True)]
        ranked = self._fuse(hm=hm, vec=None, pprl=None)
        self.assertEqual(ranked[0][:2], ("hivemind", "1"))


    def test_ungrounded_floaters_do_not_vote(self):
        # protected floaters (no keyword hit, no entity link) stay OUT of fusion
        hm = [(9.0, 1, 1, "context", "protected floater", False, False),
              (5.0, 2, 0, "context", "grounded hit", False, True)]
        ranked = self._fuse(hm=hm)
        keys = [(s, r) for s, r, _ in ranked]
        self.assertNotIn(("hivemind", "1"), keys)
        self.assertIn(("hivemind", "2"), keys)

    def test_vector_weighting_applied(self):
        vec = [("hivemind", "9")]
        ranked = self._fuse(vec=vec)
        self.assertAlmostEqual(ranked[0][2], mem_recall.VECTOR_WEIGHT / 61, places=6)


    def test_redirect_vector_hit_credits_canonical(self):
        # a vector hit on an expired row's embedding must surface the ROOT
        vec = [("hm_redirect", "42")]
        ranked = self._fuse(vec=vec, red={"42": "7"})
        self.assertEqual(ranked[0][:2], ("hivemind", "7"))
        keys = [(s, r) for s, r, _ in ranked]
        self.assertNotIn(("hm_redirect", "42"), keys)

    def test_redirect_dead_chain_dropped(self):
        vec = [("hm_redirect", "42")]
        ranked = self._fuse(vec=vec, red={})
        self.assertEqual(ranked, [])

    def test_redirect_and_canonical_in_same_list_not_double_counted(self):
        vec = [("hm_redirect", "42"), ("hivemind", "7")]
        ranked = self._fuse(vec=vec, red={"42": "7"})
        self.assertEqual(len(ranked), 1)
        self.assertAlmostEqual(ranked[0][2], mem_recall.VECTOR_WEIGHT / 61, places=6)

    def test_stale_index_expired_hivemind_ref_mapped_to_root(self):
        # row expired AFTER its embedding was built (stale window between
        # hourly refreshes) — a direct hivemind vector hit still maps to root
        vec = [("hivemind", "42")]
        ranked = self._fuse(vec=vec, red={"42": "7"})
        self.assertEqual(ranked[0][:2], ("hivemind", "7"))

    def test_rrf_math(self):
        # one list, rank 1 => 1/(60+1)
        hm = [(9.0, 1, 0, "context", "r", False, True)]
        ranked = self._fuse(hm=hm)
        self.assertAlmostEqual(ranked[0][2], 1 / 61, places=6)


class TestPPR(unittest.TestCase):
    def _toy_sem(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "semantic.db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE mention (entity_id INTEGER, store TEXT, ref TEXT, weight REAL)")
        c.execute("CREATE TABLE edge (src INTEGER, dst INTEGER)")
        # entity 1 = narrow (2 records), entity 2 = hub (many records), sharing rec B
        c.execute("INSERT INTO mention VALUES (1, 'hivemind', 'A', 1.0)")
        c.execute("INSERT INTO mention VALUES (1, 'hivemind', 'B', 1.0)")
        for i in range(30):
            c.execute("INSERT INTO mention VALUES (2, 'claudemem', ?, 1.0)", (f"H{i}",))
        c.execute("INSERT INTO mention VALUES (2, 'hivemind', 'B', 1.0)")
        c.commit(); c.close()
        return path

    def test_seeded_entity_records_rank_first_and_hub_dilutes(self):
        path = self._toy_sem()
        with mock.patch.object(ppr_mod, "SEM", path):
            ranks = ppr_mod.ranks({1: 1.0}, top=50)
        self.assertIsNotNone(ranks)
        top_keys = [k for k, _ in ranks[:2]]
        self.assertIn(("hivemind", "A"), top_keys)
        self.assertIn(("hivemind", "B"), top_keys)
        # hub records get mass only through the shared node — strictly less
        scores = dict(ranks)
        self.assertGreater(scores[("hivemind", "A")],
                           scores.get(("claudemem", "H0"), 0.0))

    def test_no_seeds_returns_none(self):
        self.assertIsNone(ppr_mod.ranks({}))

    def test_missing_db_fails_open(self):
        with mock.patch.object(ppr_mod, "SEM", "/nonexistent/sem.db"):
            self.assertIsNone(ppr_mod.ranks({1: 1.0}))


class TestEmbedIndex(unittest.TestCase):
    def test_incremental_skips_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "emb.db")
            conn = embed_index.connect(db)
            fake_corpus = [("hivemind", "1", "rule one"), ("file", "a.md", "file text"),
                           ("__ok__", frozenset({"hivemind", "file"}), "")]
            calls = []
            def fake_embed(texts, prefix=embed_index.DOC_PREFIX, timeout=120):
                calls.append(len(texts))
                return [[0.1] * embed_index.DIM for _ in texts]
            with mock.patch.object(embed_index, "corpus", side_effect=lambda: iter(fake_corpus)), \
                 mock.patch.object(embed_index, "embed_batch", side_effect=fake_embed):
                m1 = embed_index.update(conn)
                m2 = embed_index.update(conn)
            self.assertEqual(m1["embedded"], 2)
            self.assertEqual(m2["embedded"], 0)      # hashes unchanged => no re-embed
            self.assertEqual(sum(calls), 2)
            conn.close()

    def test_vanished_records_pruned(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "emb.db")
            conn = embed_index.connect(db)
            def fake_embed(texts, prefix=embed_index.DOC_PREFIX, timeout=120):
                return [[0.1] * embed_index.DIM for _ in texts]
            with mock.patch.object(embed_index, "embed_batch", side_effect=fake_embed):
                with mock.patch.object(embed_index, "corpus",
                                       side_effect=lambda: iter([("hivemind", "1", "x"), ("hivemind", "2", "y"),
                                                                 ("__ok__", frozenset({"hivemind"}), "")])):
                    embed_index.update(conn)
                with mock.patch.object(embed_index, "corpus",
                                       side_effect=lambda: iter([("hivemind", "1", "x"), ("__ok__", frozenset({"hivemind"}), "")])):
                    m = embed_index.update(conn)
            self.assertEqual(m["pruned"], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM embedding").fetchone()[0], 1)
            conn.close()

    def test_ollama_down_leaves_index_intact(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "emb.db")
            conn = embed_index.connect(db)
            with mock.patch.object(embed_index, "corpus",
                                   side_effect=lambda: iter([("hivemind", "1", "x"), ("__ok__", frozenset({"hivemind"}), "")])), \
                 mock.patch.object(embed_index, "embed_batch", return_value=None):
                m = embed_index.update(conn)
            self.assertEqual(m["embedded"], 0)
            self.assertEqual(m["failed"], 1)
            conn.close()


    def test_model_switch_triggers_reembed(self):
        # the bug that shipped: text hash matched, model changed, 0 re-embedded
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "emb.db")
            conn = embed_index.connect(db)
            def fake_embed(texts, prefix=embed_index.DOC_PREFIX, timeout=120):
                return [[0.1] * embed_index.DIM for _ in texts]
            fake_corpus = lambda: iter([("hivemind", "1", "same text"), ("__ok__", frozenset({"hivemind"}), "")])
            with mock.patch.object(embed_index, "corpus", side_effect=fake_corpus), \
                 mock.patch.object(embed_index, "embed_batch", side_effect=fake_embed):
                embed_index.update(conn)
                with mock.patch.object(embed_index, "MODEL", "other-model"):
                    m = embed_index.update(conn)
            self.assertEqual(m["embedded"], 1)
            conn.close()


    def test_failed_store_read_never_prunes(self):
        # HIGH-2 regression: a store that fails to read keeps ALL its rows
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "emb.db")
            conn = embed_index.connect(db)
            def fake_embed(texts, prefix=embed_index.DOC_PREFIX, timeout=120):
                return [[0.1] * embed_index.DIM for _ in texts]
            with mock.patch.object(embed_index, "embed_batch", side_effect=fake_embed):
                with mock.patch.object(embed_index, "corpus",
                        side_effect=lambda: iter([("hivemind", "1", "x"),
                                                  ("__ok__", frozenset({"hivemind"}), "")])):
                    embed_index.update(conn)
                # store read fails -> yields nothing for hivemind, ok set empty
                with mock.patch.object(embed_index, "corpus",
                        side_effect=lambda: iter([("__ok__", frozenset(), "")])):
                    m = embed_index.update(conn)
            self.assertEqual(m["pruned"], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM embedding").fetchone()[0], 1)
            conn.close()

    def test_ragged_vector_skipped_not_stored(self):
        # MED-3 regression: wrong-dim row must never reach the table
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "emb.db")
            conn = embed_index.connect(db)
            def ragged(texts, prefix=embed_index.DOC_PREFIX, timeout=120):
                return [[0.1] * 7 for _ in texts]  # wrong dim, passes vecs[0] pre-check? no — len!=DIM
            with mock.patch.object(embed_index, "corpus",
                    side_effect=lambda: iter([("hivemind", "1", "x"),
                                              ("__ok__", frozenset({"hivemind"}), "")])), \
                 mock.patch.object(embed_index, "embed_batch",
                                   return_value=[[0.1] * embed_index.DIM, [0.1] * 7]):
                pass  # shape mismatch scenario covered below with 2 records
            def two_corpus():
                return iter([("hivemind", "1", "x"), ("hivemind", "2", "y"),
                             ("__ok__", frozenset({"hivemind"}), "")])
            with mock.patch.object(embed_index, "corpus", side_effect=two_corpus), \
                 mock.patch.object(embed_index, "embed_batch",
                                   return_value=[[0.1] * embed_index.DIM, [0.1] * 7]):
                embed_index.update(conn)
            rows = conn.execute("SELECT ref, length(vec) FROM embedding").fetchall()
            self.assertEqual(rows, [("1", embed_index.DIM * 4)])
            conn.close()

    def test_matrix_roundtrip_and_normalization(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "emb.db")
            conn = embed_index.connect(db)
            v = np.zeros(embed_index.DIM, dtype=np.float32); v[0] = 2.0
            conn.execute("INSERT INTO embedding VALUES ('hivemind','1',?,'h',?,'t')",
                         (embed_index.MODEL,) +
                         (v.tobytes(),))
            conn.commit()
            keys, mat = embed_index.load_matrix(conn)
            self.assertEqual(keys, [("hivemind", "1")])
            self.assertAlmostEqual(float(np.linalg.norm(mat[0])), 1.0, places=5)
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=1)
