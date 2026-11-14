#!/usr/bin/env python3
"""Isolated regression tests for observation candidates and body scoring."""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import graph_policy
import mem_recall
import sem


class ObservationFixture(unittest.TestCase):
    """Temp claude-mem plus a temp semantic.db whose suppression overlay is REAL
    (Astra Batch 2: the overlay is required state; nothing here mocks it)."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = os.path.join(self.directory.name, "observations.db")
        self.connection = sqlite3.connect(self.database)
        self.addCleanup(self.connection.close)
        self.connection.execute(
            "CREATE TABLE observations (id INTEGER PRIMARY KEY, title TEXT, "
            "text TEXT, facts TEXT, narrative TEXT, created_at_epoch INTEGER, "
            "relevance_count INTEGER DEFAULT 0, subtitle TEXT, concepts TEXT)"
        )
        self.semantic = os.path.join(self.directory.name, "semantic.db")
        patcher = mock.patch.object(sem, "DB", self.semantic)
        patcher.start(); self.addCleanup(patcher.stop)
        c = sem.connect()
        c.execute("CREATE TABLE IF NOT EXISTS suppressed_observation ("
                  " obs_id INTEGER PRIMARY KEY, canonical_id INTEGER NOT NULL, reason TEXT, created_at TEXT NOT NULL)")
        c.commit(); c.close()
        for name, value in (("SEM_DB", self.semantic), ("CLAUDEMEM_DB", self.database),
                            ("HIVEMIND_DB", os.path.join(self.directory.name, "absent-hivemind.db")),
                            ("MEMDIR", os.path.join(self.directory.name, "absent-memory"))):
            patcher = mock.patch.object(graph_policy, name, value)
            patcher.start(); self.addCleanup(patcher.stop)

    def suppress(self, *ids):
        c = sqlite3.connect(self.semantic)
        c.executemany("INSERT INTO suppressed_observation VALUES (?, 0, 'dup', 'now')", [(i,) for i in ids])
        c.commit(); c.close()

    def add_records(self, records, with_fts=True):
        self.connection.executemany(
            "INSERT INTO observations "
            "(id, title, text, facts, narrative, created_at_epoch, relevance_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", records
        )
        if with_fts:
            self.connection.execute(
                "CREATE VIRTUAL TABLE observations_fts USING fts5("
                "title, subtitle, narrative, text, facts, concepts, "
                "content='observations', content_rowid='id')"
            )
            self.connection.execute(
                "INSERT INTO observations_fts(observations_fts) VALUES ('rebuild')"
            )
        self.connection.commit()

    @staticmethod
    def record(identifier, title="memory", text=None, facts=None,
               narrative=None, epoch=None):
        return (identifier, title, text, facts, narrative,
                identifier if epoch is None else epoch, 0)

    def rank(self, query="memory", boosts=None):
        with mock.patch.object(mem_recall, "CM", self.database), \
             mock.patch.object(mem_recall, "recency", return_value=1.0):
            return mem_recall.claudemem(query, mem_recall.terms(query), boosts or {})


class TestObservationCandidates(ObservationFixture):
    def large_corpus(self, with_fts):
        self.add_records(
            [self.record(identifier) for identifier in range(1, 1301)]
            + [self.record(1301, title="memory " * 20, epoch=0),
               self.record(1302, title="memory newest decision", epoch=9999)],
            with_fts=with_fts,
        )

    def test_fts_keeps_newest_and_older_relevant_match_beyond_old_cap(self):
        self.large_corpus(with_fts=True)
        rows, mode = mem_recall._cm_lexical(self.connection, ["memory"])
        self.assertEqual(mode, "fts5")
        self.assertIn(1302, rows)
        self.assertIn(1301, rows)
        self.assertLessEqual(len(rows), 1200)

    def test_fallback_keeps_newest_and_older_relevant_match_beyond_old_cap(self):
        self.large_corpus(with_fts=False)
        rows, mode = mem_recall._cm_lexical(self.connection, ["memory"])
        self.assertEqual(mode, "like")
        self.assertIn(1302, rows)
        self.assertIn(1301, rows)
        self.assertLessEqual(len(rows), 800)

    def test_relevance_and_recent_pools_are_deduplicated(self):
        self.add_records([self.record(1), self.record(2)])
        rows, mode = mem_recall._cm_lexical(self.connection, ["memory"])
        self.assertEqual(mode, "fts5")
        self.assertEqual(set(rows), {1, 2})
        self.assertEqual(len(rows), 2)

    def test_disjoint_relevance_and_recent_pools_both_contribute(self):
        self.add_records([
            self.record(1, title="memory " * 20, epoch=0),
            self.record(2, title="memory " * 10, epoch=1),
            *[self.record(identifier, title="memory " + "filler " * 100)
              for identifier in range(3, 7)],
        ])
        for helper, limit_name in (
            (mem_recall._cm_lexical, "CM_FTS_POOL_LIMIT"),
            (mem_recall._cm_like, "CM_LIKE_POOL_LIMIT"),
        ):
            with self.subTest(helper=helper.__name__), \
                 mock.patch.object(mem_recall, limit_name, 2):
                result = helper(self.connection, ["memory"])
                rows = result[0] if isinstance(result, tuple) else result
                self.assertEqual(set(rows), {1, 2, 5, 6})

    def test_failed_second_fts_query_does_not_leak_partial_candidates(self):
        self.add_records([self.record(1)], with_fts=False)
        connection = mock.Mock(wraps=self.connection)
        successful_query = mock.Mock()
        successful_query.fetchall.return_value = [(9999, "stale", "", 0, 0)]
        connection.execute.side_effect = [
            successful_query, sqlite3.OperationalError("FTS unavailable"),
            self.connection.execute("SELECT id,title,'',created_at_epoch,0 FROM observations"),
            self.connection.execute("SELECT id,title,'',created_at_epoch,0 FROM observations"),
        ]
        rows, mode = mem_recall._cm_lexical(connection, ["memory"])
        self.assertEqual(mode, "like")
        self.assertEqual(set(rows), {1})
        connection.close.assert_not_called()
        self.assertEqual(self.connection.execute("SELECT 1").fetchone(), (1,))

    def test_null_bodies_stay_safe_and_legacy_text_remains_searchable(self):
        self.add_records([
            self.record(1, title=None, text="memory legacy"),
            self.record(2, title=None),
        ])
        rows, mode = mem_recall._cm_lexical(self.connection, ["memory"])
        self.assertEqual(mode, "fts5")
        self.assertEqual(set(rows), {1})
        self.assertEqual(rows[1][1], "")
        self.assertIn("memory legacy", rows[1][2])

    def test_fts_equal_rank_and_date_use_descending_id(self):
        self.add_records([self.record(identifier, epoch=1) for identifier in range(1, 5)])
        rows, _mode = mem_recall._cm_lexical(self.connection, ["memory"])
        self.assertEqual(list(rows), [4, 3, 2, 1])

    def test_fallback_equal_rank_and_date_use_descending_id(self):
        self.add_records(
            [self.record(identifier, epoch=1) for identifier in range(1, 5)],
            with_fts=False,
        )
        rows, _mode = mem_recall._cm_lexical(self.connection, ["memory"])
        self.assertEqual(list(rows), [4, 3, 2, 1])

    def test_fts_returns_all_populated_body_fields(self):
        self.add_records([self.record(
            1, text="legacy memory", facts='["confirmed fact"]',
            narrative="explanatory narrative",
        )])
        rows, _mode = mem_recall._cm_lexical(self.connection, ["memory"])
        self.assertIn("legacy memory", rows[1][2])
        self.assertIn("confirmed fact", rows[1][2])
        self.assertIn("explanatory narrative", rows[1][2])
        self.assertEqual(len(rows[1]), 5)

    def test_fallback_returns_all_populated_body_fields(self):
        self.add_records([self.record(
            1, text="legacy memory", facts='["confirmed fact"]',
            narrative="explanatory narrative",
        )], with_fts=False)
        rows, _mode = mem_recall._cm_lexical(self.connection, ["memory"])
        self.assertIn("legacy memory", rows[1][2])
        self.assertIn("confirmed fact", rows[1][2])
        self.assertIn("explanatory narrative", rows[1][2])

    def test_fallback_null_title_does_not_hide_body_only_match(self):
        self.add_records([self.record(1, title=None, facts="memory fact")], with_fts=False)
        rows, _mode = mem_recall._cm_lexical(self.connection, ["memory"])
        self.assertIn(1, rows)
        self.assertEqual(rows[1][1], "")
        self.assertIn("memory fact", rows[1][2])

    def test_no_matches_returns_empty_fts_result(self):
        self.add_records([self.record(1)])
        rows, mode = mem_recall._cm_lexical(self.connection, ["absent"])
        self.assertEqual((rows, mode), ({}, "fts5"))

    def test_no_matches_returns_empty_fallback_result(self):
        self.add_records([self.record(1)], with_fts=False)
        rows, mode = mem_recall._cm_lexical(self.connection, ["absent"])
        self.assertEqual((rows, mode), ({}, "like"))

    def test_empty_terms_return_no_candidates(self):
        self.add_records([self.record(1)])
        rows, _mode = mem_recall._cm_lexical(self.connection, [])
        self.assertEqual(rows, {})

    def test_fallback_multiple_terms_keep_parameter_order(self):
        self.add_records([
            self.record(1, title="memory"),
            self.record(2, title="neutral", facts="latest latest latest"),
            self.record(3, title="unrelated"),
        ], with_fts=False)
        rows, mode = mem_recall._cm_lexical(self.connection, ["memory", "latest"])
        self.assertEqual(mode, "like")
        self.assertEqual(list(rows), [2, 1])

    def test_fallback_body_repetition_cannot_starve_precise_old_title(self):
        self.add_records([
            self.record(1, title="memory recall storage", epoch=0),
            *[self.record(identifier, title="general", facts="memory " * 100)
              for identifier in range(2, 12)],
        ], with_fts=False)
        with mock.patch.object(mem_recall, "CM_LIKE_POOL_LIMIT", 2):
            rows, mode = mem_recall._cm_lexical(
                self.connection, ["memory", "recall", "storage"]
            )
        self.assertEqual(mode, "like")
        self.assertTrue(1 in rows)


class TestFastObservationQuery(ObservationFixture):
    """The index-only query must return the same candidates as the joined reference query."""

    @staticmethod
    def joined_reference(connection, query_terms, limit):
        """Reference joined query."""
        match = " OR ".join('"' + term + '"' for term in query_terms)
        statement = (f"SELECT {mem_recall.CM_FIELDS_SQL} FROM observations_fts f JOIN observations o ON o.id=f.rowid "
                     "WHERE observations_fts MATCH ? ")
        relevant = connection.execute(statement + f"ORDER BY f.rank, {mem_recall.CM_NEWEST_SQL} LIMIT ?",
                                      (match, limit)).fetchall()
        recent = connection.execute(statement + f"ORDER BY {mem_recall.CM_NEWEST_SQL} LIMIT ?", (match, limit)).fetchall()
        return {row[0]: row for row in (*relevant, *recent)}

    def varied_corpus(self, time_index):
        import random
        rng = random.Random(3367)
        words = ["memory", "backup", "invoice", "tmux", "reranker", "filler", "note", "lane"]
        records = [self.record(identifier, title=" ".join(rng.choices(words, k=rng.randint(1, 6))),
                               facts=" ".join(rng.choices(words, k=rng.randint(0, 12))) or None,
                               epoch=rng.randint(0, 80))  # ids do not follow time, and many share a time
                   for identifier in range(1, 401)]
        records += [self.record(identifier, title="memory", epoch=5) for identifier in range(401, 431)]  # equal rank
        self.add_records(records)
        if time_index:
            self.connection.execute("CREATE INDEX idx_observations_created ON observations(created_at_epoch)")
            self.connection.commit()

    def test_same_pools_in_the_same_order_as_the_joined_query(self):
        for time_index in (True, False):
            with self.subTest(time_index=time_index):
                self.setUp()
                self.varied_corpus(time_index)
                for terms in (["memory"], ["memory", "backup"], ["invoice", "tmux", "reranker"], ["lane"]):
                    for limit in (1, 3, 10, 25, 600):
                        with mock.patch.object(mem_recall, "CM_FTS_POOL_LIMIT", limit), \
                             mock.patch.object(mem_recall, "CM_FTS_TIE_MARGIN", 2):
                            rows, mode = mem_recall._cm_lexical(self.connection, terms)
                        expected = self.joined_reference(self.connection, terms, limit)
                        self.assertEqual(mode, "fts5")
                        self.assertEqual(list(rows), list(expected), (terms, limit))
                        self.assertEqual(rows, expected)

    def test_recent_pool_follows_time_not_id_order(self):
        self.add_records([self.record(1, epoch=900), self.record(2, epoch=10), self.record(3, epoch=800),
                          self.record(4, epoch=20), self.record(5, title="memory " * 30, epoch=1)])
        with mock.patch.object(mem_recall, "CM_FTS_POOL_LIMIT", 1):
            rows, _mode = mem_recall._cm_lexical(self.connection, ["memory"])
        self.assertEqual(list(rows), [5, 1])  # best ranked, then newest by time (not id 4)

    def test_equal_rank_at_the_pool_edge_is_settled_by_newest_however_many_tie(self):
        self.add_records([self.record(identifier, epoch=identifier % 7) for identifier in range(1, 200)])
        with mock.patch.object(mem_recall, "CM_FTS_POOL_LIMIT", 3), mock.patch.object(mem_recall, "CM_FTS_TIE_MARGIN", 1):
            rows, _mode = mem_recall._cm_lexical(self.connection, ["memory"])
        self.assertEqual(list(rows)[:3], [195, 188, 181])  # time 6, highest ids first

    def test_times_that_are_not_numbers_sort_as_the_database_sorts_them(self):
        """SQLite will store text or bytes in the time column; ordering them in Python
        must neither fail nor differ from the old statement."""
        self.add_records([self.record(1, epoch=50), self.record(2, epoch="unknown"), self.record(3, epoch=None),
                          self.record(4, epoch="2026-11-10"), self.record(5, epoch=b"\x00\xff"), self.record(6, epoch=7.5),
                          self.record(7, epoch=b"\x00\x01"), self.record(8, epoch="unknown")])
        for limit in (1, 2, 3, 5, 600):
            with mock.patch.object(mem_recall, "CM_FTS_POOL_LIMIT", limit), mock.patch.object(mem_recall, "CM_FTS_TIE_MARGIN", 1):
                rows, mode = mem_recall._cm_lexical(self.connection, ["memory"])
            self.assertEqual(mode, "fts5")
            self.assertEqual(list(rows), list(self.joined_reference(self.connection, ["memory"], limit)), limit)

    def test_the_slow_joined_statement_is_not_used(self):
        self.add_records([self.record(1), self.record(2)])
        statements = []
        self.connection.set_trace_callback(statements.append)
        self.addCleanup(self.connection.set_trace_callback, None)
        mem_recall._cm_lexical(self.connection, ["memory"])
        self.assertFalse([s for s in statements if "JOIN observations" in s], statements)

    def test_an_index_entry_without_its_observation_is_left_out(self):
        self.add_records([self.record(1), self.record(2)])
        self.connection.execute("DELETE FROM observations WHERE id=2")  # the search index still lists it
        self.connection.commit()
        rows, mode = mem_recall._cm_lexical(self.connection, ["memory"])
        self.assertEqual((list(rows), mode), ([1], "fts5"))


class TestObservationBodyScoring(ObservationFixture):
    def test_empty_terms_do_not_prevent_entity_retrieval(self):
        self.add_records([self.record(1, title="neutral", facts="specific fact")])
        ranked, mode = self.rank(query="", boosts={("claudemem", "1"): 1.0})
        self.assertEqual(mode, "none")
        self.assertEqual([row[1] for row in ranked], [1])
        self.assertTrue(ranked[0][4])

    def test_fts_facts_only_evidence_beats_title_only_match(self):
        self.add_records([
            self.record(1, title="memory"),
            self.record(2, title="neutral", facts="memory recall storage"),
        ])
        ranked, mode = self.rank(query="memory recall storage")
        self.assertEqual(mode, "fts5")
        self.assertEqual(ranked[0][1], 2)
        self.assertEqual(ranked[0][0], 8.0)

    def test_fallback_narrative_only_evidence_beats_title_only_match(self):
        self.add_records([
            self.record(1, title="memory"),
            self.record(2, title="neutral", narrative="memory recall storage"),
        ], with_fts=False)
        ranked, mode = self.rank(query="memory recall storage")
        self.assertEqual(mode, "like")
        self.assertEqual(ranked[0][1], 2)
        self.assertEqual(ranked[0][0], 8.0)

    def test_entity_fetch_uses_facts_and_narrative(self):
        self.add_records([
            self.record(1, title="neutral"),
            self.record(2, title="neutral", facts="memory recall", narrative="storage"),
        ])
        with mock.patch.object(mem_recall, "_cm_lexical", return_value=({}, "fts5")):
            ranked, _mode = self.rank(query="memory recall storage", boosts={
                ("claudemem", "1"): 1.0, ("claudemem", "2"): 1.0,
            })
        self.assertEqual(ranked[0][1], 2)
        self.assertEqual(ranked[0][0], 8.0 + mem_recall.ENTITY_BOOST)
        self.assertTrue(ranked[0][4])

    def test_body_repetition_does_not_manufacture_relevance(self):
        self.add_records([
            self.record(1, title="neutral", facts="memory"),
            self.record(2, title="neutral", facts="memory " * 100),
        ])
        ranked, _mode = self.rank()
        scores = {row[1]: row[0] for row in ranked}
        self.assertEqual(scores[1], scores[2])

    def test_repeated_body_matches_do_not_drown_precise_title(self):
        self.add_records([
            self.record(1, title="memory recall storage", facts="memory"),
            self.record(2, title="general", narrative="recall storage " * 100),
        ])
        ranked, _mode = self.rank(query="memory recall storage")
        self.assertEqual(ranked[0][1], 1)

    def test_same_term_in_facts_and_narrative_counts_once(self):
        self.add_records([
            self.record(1, title="neutral", facts="memory", narrative="memory"),
        ])
        ranked, _mode = self.rank()
        self.assertEqual(ranked[0][0], 4.0)

    def test_suppressed_body_only_match_stays_filtered(self):
        self.add_records([
            self.record(1, facts="memory memory memory"),
            self.record(2),
        ])
        self.suppress(1)
        with mock.patch.object(mem_recall, "CM", self.database):
            ranked, _mode = mem_recall.claudemem(
                "memory", ["memory"], {("claudemem", "1"): 1.0}
            )
        self.assertEqual([row[1] for row in ranked], [2])

    def test_missing_overlay_makes_claudemem_unavailable_not_empty(self):
        self.add_records([self.record(1), self.record(2)])
        c = sqlite3.connect(self.semantic)
        c.execute("DROP TABLE suppressed_observation"); c.commit(); c.close()
        with mock.patch.object(mem_recall, "CM", self.database):
            ranked, mode = mem_recall.claudemem("memory", ["memory"], {("claudemem", "1"): 1.0})
        self.assertEqual((ranked, mode), ([], "unavailable"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
