#!/usr/bin/env python3
"""Vector search from a memory-mapped matrix instead of re-reading every vector out of SQLite on each recall.

A large index benefits from avoiding repeated SQLite reads."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest
if os.name == "nt":
    pytest.skip("mapped matrix acceleration is unavailable on Windows; the scan is tested separately", allow_module_level=True)
np = pytest.importorskip("numpy")

import embed_index

DIM = 8


def unit(*values):
    v = np.zeros(DIM, dtype=np.float32)
    v[:len(values)] = values
    return v / (np.linalg.norm(v) or 1.0)


class Index(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.tmp = Path(self.directory.name)
        # MATRIX_MIN_FREE 0 avoids dependence on disk capacity; disk guard tests set their own value.
        for name, value in (("DIM", DIM), ("DB", str(self.tmp / "embeddings.db")),
                            ("MATRIX_DIR", str(self.tmp)), ("MATRIX_MIN_FREE", 0)):
            patcher = mock.patch.object(embed_index, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.conn = embed_index.connect(str(self.tmp / "embeddings.db"))
        self.addCleanup(self.conn.close)
        self.clock = iter(f"2026-11-09T10:00:{second:02d}" for second in range(60))  # a day before any build
        self.put("file", "rates.md", unit(1, 0))
        self.put("file", "tmux.md", unit(0, 1))
        self.put("hivemind", "7", unit(1, 0.2))
        self.put("hivemind", "8", unit(0.2, 1))
        for i in range(40):
            self.put("claudemem", str(i), unit(1, 0.05 * i))
        self.query(unit(1, 0))

    def put(self, store, ref, vec, at=None):
        self.conn.execute(
            "INSERT INTO embedding (store, ref, model, text_hash, vec, created_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(store, ref) DO UPDATE SET vec=excluded.vec, created_at=excluded.created_at",
            (store, ref, embed_index.MODEL, "h", np.asarray(vec, dtype=np.float32).tobytes(), at or next(self.clock)))
        self.conn.commit()

    def query(self, vec):
        patcher = mock.patch.object(embed_index, "embed_batch", return_value=[list(map(float, vec))])
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, at="2026-11-10T11:00:00"):
        with mock.patch.object(embed_index, "now", return_value=at):
            return embed_index.build_matrix(self.conn)

    def scan(self, k=10):
        with mock.patch.object(embed_index, "_matrix", return_value=None):
            return embed_index.search(self.conn, "q", k=k)


class MatrixSearch(Index):
    def test_matrix_search_returns_what_the_full_scan_returns(self):
        expected = self.scan(k=12)
        self.build()
        with mock.patch.object(embed_index, "_scan_scores", side_effect=AssertionError("full scan used")):
            got = embed_index.search(self.conn, "q", k=12)
        self.assertEqual([(s, r) for s, r, _ in got], [(s, r) for s, r, _ in expected])
        for (_, _, a), (_, _, b) in zip(got, expected):
            self.assertAlmostEqual(a, b, places=5)

    def test_without_a_matrix_it_falls_back_to_the_full_scan(self):
        self.assertEqual(self.scan(k=3)[0][:2], ("file", "rates.md"))
        got = embed_index.search(self.conn, "q", k=3)  # nothing built yet
        self.assertEqual(got[0][:2], ("file", "rates.md"))

    def test_a_damaged_matrix_falls_back_to_the_full_scan(self):
        info = self.build()
        Path(info["path"], "matrix.f32").write_bytes(b"short")
        self.assertEqual(embed_index.search(self.conn, "q", k=1)[0][:2], ("file", "rates.md"))

    def test_a_record_added_after_the_build_is_found(self):
        self.build(at="2026-11-10T11:00:00")
        self.put("file", "new-note.md", unit(1, 0.01), at="2026-11-10T11:30:00")
        refs = [r for _s, r, _ in embed_index.search(self.conn, "q", k=3)]
        self.assertIn("new-note.md", refs)

    def test_a_record_rewritten_after_the_build_uses_its_new_vector_once(self):
        self.build(at="2026-11-10T11:00:00")
        self.put("file", "rates.md", unit(0, 1), at="2026-11-10T11:30:00")  # now about something else
        hits = embed_index.search(self.conn, "q", k=50)
        self.assertEqual(sum(1 for s, r, _ in hits if (s, r) == ("file", "rates.md")), 1)
        self.assertLess(dict(((s, r), score) for s, r, score in hits)[("file", "rates.md")], 0.1)

    def test_another_models_vectors_are_never_mixed_in(self):
        self.conn.execute("INSERT INTO embedding VALUES ('file','old-model.md','some-other-model','h',?,?)",
                          (unit(1, 0).tobytes(), "2026-11-10T09:00:00"))
        self.conn.commit()
        self.build()
        self.assertNotIn("old-model.md", [r for _s, r, _ in embed_index.search(self.conn, "q", k=60)])

    def test_a_new_build_replaces_the_old_one_and_removes_its_files(self):
        first = self.build(at="2026-11-10T11:00:00")
        second = self.build(at="2026-11-10T12:00:00")
        self.assertNotEqual(first["path"], second["path"])
        self.assertFalse(Path(first["path"]).exists())
        self.assertTrue(Path(second["path"], "matrix.f32").is_file())
        self.assertEqual(second["count"], 44)

    def test_what_changed_since_the_build_does_not_depend_on_timestamps(self):
        """a timestamp the writer chooses must not decide what search sees."""
        self.build(at="2026-11-10T11:00:00")
        self.put("file", "old-stamp-new-note.md", unit(1, 0.01), at="2020-01-01T00:00:00")
        self.put("file", "rates.md", unit(0, 1), at="2020-01-01T00:00:00")  # rewritten, stamped years back
        hits = {(s, r): score for s, r, score in embed_index.search(self.conn, "q", k=60)}
        self.assertIn(("file", "old-stamp-new-note.md"), hits)
        self.assertLess(hits[("file", "rates.md")], 0.1)

    def test_a_record_whose_key_was_changed_is_found_under_the_new_key_only(self):
        self.build()
        self.conn.execute("UPDATE embedding SET ref='rates-2026.md' WHERE store='file' AND ref='rates.md'")
        self.conn.commit()
        refs = [r for s, r, _ in embed_index.search(self.conn, "q", k=60) if s == "file"]
        self.assertIn("rates-2026.md", refs)
        self.assertNotIn("rates.md", refs)

    def test_a_record_moved_to_another_model_after_the_build_is_left_out(self):
        self.build()
        self.conn.execute("UPDATE embedding SET model='some-other-model' WHERE store='file' AND ref='rates.md'")
        self.conn.commit()
        self.assertNotIn("rates.md", [r for _s, r, _ in embed_index.search(self.conn, "q", k=60)])

    def test_a_build_empties_the_log_and_a_search_still_on_an_older_build_falls_back_to_the_scan(self):
        """two builds during one search must not lose a change.
        Each build stamps the database; a search checks the stamp in the same read as the change list."""
        count = lambda: self.conn.execute("SELECT COUNT(*) FROM embedding_change").fetchone()[0]
        self.build(at="2026-11-10T11:00:00")
        self.put("file", "victim.md", unit(1, 0.01))
        old_reader = embed_index._matrix()          # a search that mapped this build and then paused
        self.build(at="2026-11-10T12:00:00")
        self.assertEqual(count(), 0)                # everything up to the build is in the matrix
        self.conn.execute("DELETE FROM embedding WHERE store='file' AND ref='victim.md'")
        self.conn.commit()
        self.build(at="2026-11-10T13:00:00")
        self.assertEqual(count(), 0)
        expected = self.scan(k=44)
        with mock.patch.object(embed_index, "_matrix", return_value=old_reader):
            got = embed_index.search(self.conn, "q", k=44)
            self.assertFalse(embed_index.matrix_status(self.conn)["present"])
        self.assertEqual([(s, r) for s, r, _ in got], [(s, r) for s, r, _ in expected])
        self.assertNotIn("victim.md", [r for _s, r, _ in got])

    def copy_of_database(self, name):
        target = sqlite3.connect(str(self.tmp / name))
        self.conn.backup(target)
        target.close()
        copy = embed_index.connect(str(self.tmp / name))
        self.addCleanup(copy.close)
        return copy

    def test_a_copy_forked_before_the_build_is_not_answered_from_it(self):
        """same database id, same change count, different history."""
        fork = self.copy_of_database("fork.db")
        self.put("file", "original-only.md", unit(1, 0.01))
        self.build()
        fork.execute("INSERT INTO embedding VALUES ('file','fork-only.md',?,'h',?,'x')", (embed_index.MODEL, unit(1, 0.02).tobytes()))
        fork.commit()
        refs = [r for _s, r, _ in embed_index.search(fork, "q", k=60)]
        self.assertIn("fork-only.md", refs)
        self.assertNotIn("original-only.md", refs)
        self.assertFalse(embed_index.matrix_status(fork)["present"])

    def test_a_copy_made_after_the_build_uses_it_with_its_own_later_changes(self):
        self.build()
        copy = self.copy_of_database("copy.db")
        copy.execute("INSERT INTO embedding VALUES ('file','copy-only.md',?,'h',?,'x')", (embed_index.MODEL, unit(1, 0.02).tobytes()))
        copy.commit()
        with mock.patch.object(embed_index, "_scan_scores", side_effect=AssertionError("full scan used")):
            refs = [r for _s, r, _ in embed_index.search(copy, "q", k=60)]
        self.assertIn("copy-only.md", refs)

    def test_a_change_counter_that_is_not_a_number_means_the_scan_not_an_error(self):
        self.put("file", "a.md", unit(1, 0.3))
        self.build()
        self.conn.execute("UPDATE sqlite_sequence SET seq='x' WHERE name='embedding_change'")
        self.conn.commit()
        self.assertEqual(embed_index.search(self.conn, "q", k=1)[0][:2], ("file", "rates.md"))
        report = embed_index.refresh_matrix(self.conn)
        self.assertFalse(report["built"])
        self.assertIn("error", report)

    def test_every_store_can_be_walked_past_any_fixed_cut(self):
        """a caller that filters must be able to keep reading."""
        self.build()
        self.put("claudemem", "after-build", unit(1, 0.001))
        ranked = embed_index.ranked_by_store(self.conn, "q", ["claudemem", "file", "hm_redirect"])
        observations = list(ranked["claudemem"])
        self.assertEqual(len(observations), 41)
        self.assertEqual([score for _r, score in observations], sorted((s for _r, s in observations), reverse=True))
        self.assertEqual({r for r, _ in observations}, {str(i) for i in range(40)} | {"after-build"})
        self.assertEqual([r for r, _ in ranked["file"]], ["rates.md", "tmux.md"])
        self.assertEqual(list(ranked["hm_redirect"]), [])

    def test_a_build_that_fails_part_way_leaves_the_old_one_in_use(self):
        first = self.build(at="2026-11-10T11:00:00")
        for name, target, error in (("save", embed_index.np, OSError("disk full")),
                                    ("replace", embed_index.os, OSError("disk full")),
                                    ("_stamp_database", embed_index, sqlite3.OperationalError("database is locked"))):
            with self.subTest(fails_at=name), mock.patch.object(target, name, side_effect=error):
                with self.assertRaises(type(error)):
                    self.build(at="2026-11-10T12:00:00")
            self.assertTrue(Path(first["path"], "matrix.f32").is_file())
            self.assertEqual(embed_index.matrix_status(self.conn)["built_at"], "2026-11-10T11:00:00")
            left = sorted(p.name for p in self.tmp.iterdir()
                          if not p.name.startswith("embeddings.db") and p.name != embed_index.MATRIX_LOCK)
            self.assertEqual(left, sorted([embed_index.MATRIX_POINTER, Path(first["path"]).name]))

    def test_the_disk_check_counts_the_key_files_too(self):
        self.put("file", "x" * 4000 + ".md", unit(1, 0.5))  # long keys make the key file large
        rows = self.conn.execute("SELECT COUNT(*) FROM embedding").fetchone()[0]
        vectors_only = rows * DIM * 4
        free = embed_index.shutil.disk_usage(self.tmp).free
        with mock.patch.object(embed_index, "MATRIX_MIN_FREE", free - vectors_only - 20_000):
            with self.assertRaises(embed_index.MatrixError):  # the vectors alone would fit; with the keys it does not
                self.build()

    def test_the_key_files_are_sized_from_what_is_actually_written(self):
        """a key stored as bytes is written as its much longer text form."""
        self.conn.execute("INSERT INTO embedding VALUES ('file', ?, ?, 'h', ?, 'x')",
                          (b"\xff" * 2000, embed_index.MODEL, unit(1, 0.4).tobytes()))
        self.conn.commit()
        rows = self.conn.execute("SELECT COUNT(*) FROM embedding").fetchone()[0]
        usage = embed_index.shutil.disk_usage(self.tmp)
        by_database_length = rows * (DIM * 4 + 1 + 2000) + 64 * 1024   # what the stored length suggests, plus slack
        with mock.patch.object(embed_index.shutil, "disk_usage", return_value=usage), \
                mock.patch.object(embed_index, "MATRIX_MIN_FREE", usage.free - by_database_length):
            with self.assertRaises(embed_index.MatrixError):  # the text form is about four times as long
                self.build()
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.startswith(embed_index.MATRIX_PREFIX)], [])

    def test_disk_running_out_at_any_step_of_a_build_stops_it(self):
        usage = embed_index.shutil.disk_usage(self.tmp)
        for good_checks in (1, 2, 3, 4):  # start, the rows, the two key files, the description
            answers = iter([usage] * good_checks)
            with self.subTest(good_checks=good_checks), \
                    mock.patch.object(embed_index.shutil, "disk_usage",
                                      side_effect=lambda _p: next(answers, usage._replace(free=1))):
                with self.assertRaises(embed_index.MatrixError):
                    self.build()
            self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.startswith(embed_index.MATRIX_PREFIX)], [])
        self.assertFalse(embed_index.matrix_status(self.conn)["present"])

    def test_a_matrix_built_from_another_database_is_not_used(self):
        """A different database must not be answered from an existing matrix."""
        self.build()
        other = embed_index.connect(str(self.tmp / "other.db"))
        self.addCleanup(other.close)
        other.execute("INSERT INTO embedding VALUES ('file','only-here.md',?,'h',?,'2026-11-10T09:00:00')",
                      (embed_index.MODEL, unit(0.5, 0.5).tobytes()))
        other.commit()
        self.assertEqual([(s, r) for s, r, _ in embed_index.search(other, "q", k=5)], [("file", "only-here.md")])
        self.assertFalse(embed_index.matrix_status(other)["present"])
        self.assertTrue(embed_index.matrix_status(self.conn)["present"])

    def test_a_database_put_back_to_an_earlier_state_is_not_answered_from_a_later_matrix(self):
        self.put("file", "a.md", unit(1, 0.3))
        self.build()
        self.conn.execute("DELETE FROM embedding_change")
        self.conn.execute("DELETE FROM sqlite_sequence WHERE name='embedding_change'")  # as a restored older copy
        self.conn.commit()
        self.assertFalse(embed_index.matrix_status(self.conn)["present"])
        self.assertEqual(embed_index.search(self.conn, "q", k=1)[0][:2], ("file", "rates.md"))
        self.assertTrue(embed_index.refresh_matrix(self.conn)["built"])  # and the hourly refresh rebuilds it

    def test_a_vector_cut_short_after_the_build_is_left_out_as_the_scan_leaves_it_out(self):
        """a damaged vector must not be answered from its old matrix row."""
        self.build()
        self.conn.execute("UPDATE embedding SET vec=X'0000' WHERE store='file' AND ref='rates.md'")
        self.conn.commit()
        self.assertEqual([(s, r) for s, r, _ in embed_index.search(self.conn, "q", k=44)],
                         [(s, r) for s, r, _ in self.scan(k=44)])
        self.assertNotIn("rates.md", [r for _s, r, _ in embed_index.search(self.conn, "q", k=44)])

    def test_key_files_of_the_wrong_kind_fall_back_to_the_scan(self):
        info = self.build()
        np.save(Path(info["path"], "refs.npy"), np.asarray(["x"] * info["count"], dtype="<U20"))
        self.assertEqual(embed_index.search(self.conn, "q", k=1)[0][:2], ("file", "rates.md"))
        info = self.build(at="2026-11-10T12:00:00")
        np.save(Path(info["path"], "stores.npy"), np.zeros(info["count"], dtype=np.float64))
        self.assertEqual(embed_index.search(self.conn, "q", k=1)[0][:2], ("file", "rates.md"))

    def test_an_abandoned_build_folder_is_removed_even_when_the_new_build_is_refused(self):
        """A killed build leaves a large temporary file behind; a refused build must not leave it there for good."""
        current = self.build()
        stray = self.tmp / (embed_index.MATRIX_PREFIX + "killed")
        stray.mkdir()
        (stray / "matrix.f32").write_bytes(b"x" * 100)
        with mock.patch.object(embed_index, "MATRIX_MIN_FREE", 10 ** 18):
            with self.assertRaises(embed_index.MatrixError):
                self.build(at="2026-11-10T12:00:00")
        self.assertFalse(stray.exists())
        self.assertTrue(Path(current["path"], "matrix.f32").is_file())

    def test_a_connection_without_the_change_tables_still_gets_an_answer(self):
        self.build()
        plain = sqlite3.connect(str(self.tmp / "plain.db"))
        self.addCleanup(plain.close)
        plain.execute("CREATE TABLE embedding (store TEXT, ref TEXT, model TEXT, text_hash TEXT, vec BLOB, created_at TEXT)")
        plain.execute("INSERT INTO embedding VALUES ('file','plain.md',?,'h',?,'x')", (embed_index.MODEL, unit(1, 0).tobytes()))
        plain.commit()
        self.assertEqual(embed_index.search(plain, "q", k=3), [("file", "plain.md", 1.0)])
        self.assertEqual(embed_index.matrix_status(plain), {"present": False})
        self.assertEqual([r for r, _ in embed_index.ranked_by_store(plain, "q", ["file"])["file"]], ["plain.md"])
        self.assertFalse(embed_index.refresh_matrix(plain)["built"])

    def test_no_embedding_service_means_no_result_as_before(self):
        self.build()
        with mock.patch.object(embed_index, "embed_batch", return_value=None):
            self.assertIsNone(embed_index.search(self.conn, "q", k=3))


class PerStore(Index):
    """Curated notes are 3% of the vectors: one global list lets raw observations crowd them out."""

    def test_each_store_contributes_its_own_best_matches(self):
        self.build()
        hits = embed_index.search_by_store(self.conn, "q", {"file": 2, "hivemind": 1, "claudemem": 3})
        self.assertEqual([r for r, _ in hits["file"]], ["rates.md", "tmux.md"])
        self.assertEqual([r for r, _ in hits["hivemind"]], ["7"])
        self.assertEqual([r for r, _ in hits["claudemem"]], ["0", "1", "2"])

    def test_a_global_top_three_would_have_held_only_one_store(self):
        self.build()
        self.put("claudemem", "exact-a", unit(1, 0))
        self.put("claudemem", "exact-b", unit(1, 0))
        self.put("claudemem", "exact-c", unit(1, 0))
        self.build(at="2026-11-10T12:00:00")
        top = embed_index.search(self.conn, "q", k=3)
        by_store = embed_index.search_by_store(self.conn, "q", {"file": 1, "hivemind": 1, "claudemem": 1})
        self.assertLessEqual(len({s for s, _r, _ in top}), 2)
        self.assertEqual(by_store["file"][0][0], "rates.md")
        self.assertEqual(by_store["hivemind"][0][0], "7")

    def test_scores_are_in_descending_order_and_stores_without_vectors_are_empty(self):
        self.build()
        hits = embed_index.search_by_store(self.conn, "q", {"claudemem": 5, "hm_redirect": 5})
        scores = [score for _r, score in hits["claudemem"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(hits["hm_redirect"], [])

    def test_it_works_without_a_matrix_too(self):
        hits = embed_index.search_by_store(self.conn, "q", {"file": 1})
        self.assertEqual(hits["file"][0][0], "rates.md")


class Refresh(Index):
    def refresh(self, at, **kwargs):
        with mock.patch.object(embed_index, "now", return_value=at):
            return embed_index.refresh_matrix(self.conn, **kwargs)

    def test_refresh_builds_a_matrix_when_none_exists_and_skips_when_nothing_changed(self):
        self.assertTrue(self.refresh("2026-11-10T11:00:00")["built"])
        again = self.refresh("2026-11-10T12:00:00")
        self.assertFalse(again["built"])
        self.assertEqual(again["newer_records"], 0)
        self.assertTrue(self.refresh("2026-11-10T12:00:00", force=True)["built"])

    def test_refresh_rebuilds_after_enough_new_records(self):
        self.refresh("2026-11-10T11:00:00")
        with mock.patch.object(embed_index, "MATRIX_REBUILD_AFTER", 5):
            for i in range(5):
                self.put("claudemem", f"later-{i}", unit(0.5, 0.5), at="2026-11-10T11:30:00")
            self.assertFalse(self.refresh("2026-11-10T12:00:00")["built"])
            self.put("claudemem", "later-5", unit(0.5, 0.5), at="2026-11-10T11:30:00")
            self.assertTrue(self.refresh("2026-11-10T12:00:00")["built"])


if __name__ == "__main__":
    unittest.main()
