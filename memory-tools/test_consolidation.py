#!/usr/bin/env python3
"""Tests for consolidate.py + the hm_write/mem_recall integration.

Runs standalone (python3 test_consolidation.py) or under pytest. All tests use
temp databases; the LLM is stubbed — no claude calls, no real stores touched.
"""
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import consolidate  # noqa: E402


def make_hm(path):
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE learnings (
        id INTEGER PRIMARY KEY, category TEXT, rule TEXT, context TEXT,
        source_session TEXT, confidence REAL, times_applied INTEGER DEFAULT 0,
        times_helpful INTEGER DEFAULT 0, created_at TIMESTAMP,
        last_applied TIMESTAMP, active INTEGER DEFAULT 1, protected INTEGER DEFAULT 0)""")
    c.commit()
    return c


def make_cm(path):
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE observations (
        id INTEGER PRIMARY KEY, memory_session_id TEXT, project TEXT, text TEXT,
        type TEXT, title TEXT, facts TEXT, narrative TEXT, created_at_epoch INTEGER,
        relevance_count INTEGER DEFAULT 0)""")
    c.commit()
    return c


def make_sem(path):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE entity (id INTEGER PRIMARY KEY, slug TEXT)")
    c.execute("CREATE TABLE mention (entity_id INTEGER, store TEXT, ref TEXT, weight REAL)")
    c.commit()
    return c


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = self.dir.name
        self.hm = make_hm(os.path.join(d, "hm.db"))
        self.cm = make_cm(os.path.join(d, "cm.db"))
        self.sem = make_sem(os.path.join(d, "sem.db"))
        consolidate.ensure_schema(self.hm, self.sem)

    def tearDown(self):
        for c in (self.hm, self.cm, self.sem):
            c.close()
        self.dir.cleanup()

    def add_learning(self, rule, cat="context", ctx=None, protected=0, active=1):
        cur = self.hm.execute(
            "INSERT INTO learnings (category, rule, context, confidence, created_at, active, protected) "
            "VALUES (?,?,?,0.8,'2026-07-01T00:00:00',?,?)", (cat, rule, ctx, active, protected))
        self.hm.commit()
        return cur.lastrowid

    def add_obs(self, title, project="p", epoch=1753000000, rel=0):
        cur = self.cm.execute(
            "INSERT INTO observations (project, title, text, facts, created_at_epoch, relevance_count) "
            "VALUES (?,?,'body','facts',?,?)", (project, title, epoch, rel))
        self.cm.commit()
        return cur.lastrowid


class TestSchema(Base):
    def test_migration_idempotent(self):
        consolidate.ensure_schema(self.hm, self.sem)  # second run must not raise
        cols = {r[1] for r in self.hm.execute("PRAGMA table_info(learnings)")}
        for c in ("valid_from", "valid_to", "recorded_at", "expired_at", "superseded_by"):
            self.assertIn(c, cols)
        tables = {r[0] for r in self.sem.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertLessEqual({"suppressed_observation", "distillation", "consolidation_run"}, tables)


class TestKimiNoise(Base):
    def test_noise_expired_real_kept(self):
        n1 = self.add_learning('kimi-auto: Kimi session "new session" in /x')
        n2 = self.add_learning('kimi-auto: Kimi session "proceed" in /x')
        keep = self.add_learning('kimi-auto: Kimi session "fix the invoice mapper" in /x')
        m = consolidate.phase_kimi(self.hm, apply=True)
        self.assertEqual(m["kimi_noise_expired"], 2)
        actives = {r[0] for r in self.hm.execute("SELECT id FROM learnings WHERE active=1")}
        self.assertEqual(actives, {keep})
        for rid in (n1, n2):
            row = self.hm.execute("SELECT expired_at FROM learnings WHERE id=?", (rid,)).fetchone()
            self.assertIsNotNone(row[0])

    def test_dry_run_mutates_nothing(self):
        self.add_learning('kimi-auto: Kimi session "proceed" in /x')
        m = consolidate.phase_kimi(self.hm, apply=False)
        self.assertEqual(m["kimi_noise_found"], 1)
        self.assertEqual(m["kimi_noise_expired"], 0)
        self.assertEqual(self.hm.execute(
            "SELECT COUNT(*) FROM learnings WHERE active=1").fetchone()[0], 1)


class TestHmMerge(Base):
    def test_clusters_respect_category(self):
        self.add_learning("fly deploys need the config token not the stale key file", cat="context")
        self.add_learning("fly deploys need the config token not the stale key file", cat="correction")
        self.assertEqual(consolidate.find_hm_clusters(self.hm), [])

    def test_merge_expires_losers_with_provenance(self):
        a = self.add_learning("contract terms agreed with john ten thousand monthly equity ratchet",
                              ctx="ORIGINAL CURATED CONTEXT")
        b = self.add_learning("contract terms agreed with john ten thousand monthly equity ratchet capped")
        with mock.patch.object(consolidate, "ask_model",
                               return_value={"action": "merge", "canonical_id": a, "merged_context": "folded"}):
            m = consolidate.phase_hm(self.hm, apply=True)
        self.assertEqual(m["hm_merged"], 1)
        loser = self.hm.execute(
            "SELECT active, superseded_by, expired_at FROM learnings WHERE id=?", (b,)).fetchone()
        self.assertEqual(loser[0], 0)
        self.assertEqual(loser[1], a)
        self.assertIsNotNone(loser[2])
        canon = self.hm.execute("SELECT active, context FROM learnings WHERE id=?", (a,)).fetchone()
        self.assertEqual(canon[0], 1)
        # H2 regression: the canonical's own context is APPENDED to, never replaced
        self.assertIn("ORIGINAL CURATED CONTEXT", canon[1])
        self.assertIn("folded", canon[1])

    def test_malformed_verdicts_never_crash_or_merge(self):
        # H3 regression: lists, string ids, and prose confidence must be inert
        self.add_learning("identical duplicate learning row for malformed verdict testing")
        b = self.add_learning("identical duplicate learning row for malformed verdict testing too")
        for bad in ({"action": "merge", "canonical_id": "not-an-int"},
                    {"action": "merge", "canonical_id": None},
                    {"action": ["merge"]}):
            with mock.patch.object(consolidate, "ask_model", return_value=bad):
                m = consolidate.phase_hm(self.hm, apply=True)
            self.assertEqual(m["hm_merged"], 0, bad)
        self.assertEqual(self.hm.execute(
            "SELECT COUNT(*) FROM learnings WHERE active=1").fetchone()[0], 2)

    def test_ask_model_rejects_non_dict_json(self):
        # contract: ask_model returns a dict or None — never a list/str/number
        for stdout in ("[1, 2, 3]", "no json here at all", '"just a string"'):
            fake = mock.Mock(stdout=stdout, returncode=0)
            with mock.patch.object(consolidate.subprocess, "run", return_value=fake):
                self.assertIsNone(consolidate.ask_model("prompt"), stdout)
        fake = mock.Mock(stdout='ok {"action":"merge"} done', returncode=0)
        with mock.patch.object(consolidate.subprocess, "run", return_value=fake):
            self.assertEqual(consolidate.ask_model("prompt"), {"action": "merge"})

    def test_non_utf8_rule_does_not_crash_clustering(self):
        # C1 regression: legacy rows contain invalid UTF-8; connect() must decode leniently
        raw = sqlite3.connect(self.hm.execute("PRAGMA database_list").fetchone()[2])
        raw.execute("INSERT INTO learnings (category, rule, confidence, created_at, active, protected) "
                    "VALUES ('context', CAST(X'4A75706974657220ff50657270' AS TEXT), 0.8, '2026-07-01', 1, 0)")
        raw.commit(); raw.close()
        path = self.hm.execute("PRAGMA database_list").fetchone()[2]
        self.hm.close()
        self.hm = consolidate.connect(path)
        clusters = consolidate.find_hm_clusters(self.hm)  # must not raise
        self.assertIsInstance(clusters, list)

    def test_protected_loser_never_folds_into_unprotected_canonical(self):
        a = self.add_learning("acme contoso invoice rule northwind twenty percent service fee", protected=0)
        b = self.add_learning("acme contoso invoice rule northwind twenty percent service fee always", protected=1)
        with mock.patch.object(consolidate, "ask_model",
                               return_value={"action": "merge", "canonical_id": a, "merged_context": None}):
            consolidate.phase_hm(self.hm, apply=True)
        self.assertEqual(self.hm.execute(
            "SELECT active FROM learnings WHERE id=?", (b,)).fetchone()[0], 1)

    def test_llm_failure_merges_nothing(self):
        self.add_learning("same fact repeated once more for clustering purposes here")
        self.add_learning("same fact repeated once more for clustering purposes here twice")
        with mock.patch.object(consolidate, "ask_model", return_value=None):
            m = consolidate.phase_hm(self.hm, apply=True)
        self.assertEqual(m["hm_merged"], 0)
        self.assertEqual(self.hm.execute(
            "SELECT COUNT(*) FROM learnings WHERE active=1").fetchone()[0], 2)


class TestCmSuppress(Base):
    def test_duplicates_suppressed_canonical_kept(self):
        base = 1753000000
        a = self.add_obs("Contract Terms Agreed with John", epoch=base, rel=0)
        b = self.add_obs("Contract terms agreed with John!", epoch=base + 3600, rel=5)
        c = self.add_obs("Contract Terms Agreed with John", epoch=base + 7200, rel=1)
        solo = self.add_obs("Completely different observation", epoch=base)
        m = consolidate.phase_cm(self.cm, self.sem, apply=True)
        self.assertEqual(m["cm_suppressed"], 2)
        rows = dict(self.sem.execute(
            "SELECT obs_id, canonical_id FROM suppressed_observation").fetchall())
        self.assertEqual(set(rows), {a, c})           # b wins on relevance_count
        self.assertEqual(set(rows.values()), {b})
        self.assertNotIn(solo, rows)

    def test_window_guard_keeps_far_apart_rows(self):
        base = 1753000000
        self.add_obs("Weekly metrics cron repaired", epoch=base)
        self.add_obs("Weekly metrics cron repaired", epoch=base + 40 * 86400)
        m = consolidate.phase_cm(self.cm, self.sem, apply=True)
        self.assertEqual(m["cm_suppressed"], 0)

    def test_millisecond_epochs_normalized(self):
        # prod claude-mem stores epochs in MILLIS; the 14-day window must hold
        base_ms = 1785000000000
        a = self.add_obs("Dup in millis land", epoch=base_ms)
        b = self.add_obs("Dup in millis land", epoch=base_ms + 3 * 86400 * 1000, rel=2)
        self.add_obs("Dup in millis land far", epoch=base_ms)
        self.add_obs("Dup in millis land far", epoch=base_ms + 40 * 86400 * 1000)
        m = consolidate.phase_cm(self.cm, self.sem, apply=True)
        rows = dict(self.sem.execute(
            "SELECT obs_id, canonical_id FROM suppressed_observation").fetchall())
        self.assertEqual(rows, {a: b})  # 3-day-apart pair collapses; 40-day pair survives

    def test_idempotent_across_runs(self):
        base = 1753000000
        self.add_obs("Dup title", epoch=base)
        self.add_obs("Dup title", epoch=base + 60, rel=2)
        consolidate.phase_cm(self.cm, self.sem, apply=True)
        m2 = consolidate.phase_cm(self.cm, self.sem, apply=True)
        self.assertEqual(m2["cm_suppressed"], 0)
        self.assertEqual(self.sem.execute(
            "SELECT COUNT(*) FROM suppressed_observation").fetchone()[0], 1)


class TestDistill(Base):
    def _wire_entity(self, slug, n_obs):
        self.sem.execute("INSERT INTO entity (slug) VALUES (?)", (slug,))
        eid = self.sem.execute("SELECT id FROM entity WHERE slug=?", (slug,)).fetchone()[0]
        import time
        recent = int(time.time()) - 3600
        for i in range(n_obs):
            oid = self.add_obs(f"{slug} observation {i}", epoch=recent - i)
            self.sem.execute(
                "INSERT INTO mention (entity_id, store, ref, weight) VALUES (?, 'claudemem', ?, 1.0)",
                (eid, str(oid)))
        self.sem.commit()

    def test_below_threshold_not_distilled(self):
        self._wire_entity("smalltopic", consolidate.DISTILL_MIN_OBS - 1)
        with mock.patch.object(consolidate, "ask_model") as am:
            m = consolidate.phase_distill(self.cm, self.sem, apply=False)
        am.assert_not_called()
        self.assertEqual(m["distill_candidates"], 0)

    def test_hub_entity_excluded_on_real_corpus(self):
        # M4 regression: >8% of a >=200-mention corpus = hub, never distilled
        self._wire_entity("hubtopic", 238)
        self._wire_entity("narrowtopic", 12)
        with mock.patch.object(consolidate, "ask_model", return_value={"rule": None}):
            m = consolidate.phase_distill(self.cm, self.sem, apply=False)
        self.assertEqual(m["distill_candidates"], 1)  # narrowtopic only

    def test_distills_via_hm_write_and_records(self):
        self._wire_entity("bigtopic", 12)
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=0, stderr="")
        with mock.patch.object(consolidate, "ask_model",
                               return_value={"rule": "durable insight", "context": "why", "confidence": 0.6}), \
             mock.patch.object(consolidate.subprocess, "run", side_effect=fake_run):
            m = consolidate.phase_distill(self.cm, self.sem, apply=True)
        self.assertEqual(len(m["distilled"]), 1)
        self.assertTrue(calls and "--no-adjudicate" in calls[0])
        self.assertIn("[distilled:bigtopic]", " ".join(calls[0]))
        row = self.sem.execute(
            "SELECT obs_count_at_distill FROM distillation WHERE entity_slug='bigtopic'").fetchone()
        self.assertEqual(row[0], 12)

    def test_no_redistill_without_new_material(self):
        self._wire_entity("bigtopic", 12)
        self.sem.execute(
            "INSERT INTO distillation (entity_slug, last_distilled_at, obs_count_at_distill) "
            "VALUES ('bigtopic', '2026-07-29T00:00:00', 12)")
        self.sem.commit()
        with mock.patch.object(consolidate, "ask_model") as am:
            m = consolidate.phase_distill(self.cm, self.sem, apply=True)
        am.assert_not_called()
        self.assertEqual(m["distill_candidates"], 0)

    def test_llm_null_rule_creates_nothing(self):
        self._wire_entity("bigtopic", 12)
        with mock.patch.object(consolidate, "ask_model", return_value={"rule": None}), \
             mock.patch.object(consolidate.subprocess, "run") as sr:
            m = consolidate.phase_distill(self.cm, self.sem, apply=True)
        sr.assert_not_called()
        self.assertEqual(m["distilled"], [])


class TestIndexSync(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.d = self.dir.name

    def tearDown(self):
        self.dir.cleanup()

    def test_adds_missing_never_deletes(self):
        Path(self.d, "MEMORY.md").write_text(
            "# Memory Index\n- [Known](known.md) — a hook.\n- [Gone](gone.md) — dangling.\n")
        Path(self.d, "known.md").write_text("known")
        Path(self.d, "orphan.md").write_text("---\ndescription: an orphan fact\n---\nbody")
        m = consolidate.phase_index(self.d, apply=True)
        idx = Path(self.d, "MEMORY.md").read_text()
        self.assertIn("orphan.md", idx)
        self.assertIn("an orphan fact", idx)
        self.assertIn("gone.md", idx)                      # dangling reported, NOT removed
        self.assertEqual(m["index_dangling"], ["gone.md"])

    def test_dry_run_leaves_index_untouched(self):
        Path(self.d, "MEMORY.md").write_text("# Memory Index\n")
        Path(self.d, "orphan.md").write_text("x")
        before = Path(self.d, "MEMORY.md").read_text()
        consolidate.phase_index(self.d, apply=False)
        self.assertEqual(Path(self.d, "MEMORY.md").read_text(), before)


class TestRecallSuppression(unittest.TestCase):
    def test_entity_boost_path_also_filtered(self):
        # H1 regression: a suppressed observation reachable ONLY via entity boost
        # must not resurface in claudemem()
        import mem_recall
        with tempfile.TemporaryDirectory() as d:
            cmdb = os.path.join(d, "cm.db")
            c = make_cm(cmdb)
            c.execute("INSERT INTO observations (id, project, title, text, created_at_epoch, relevance_count) "
                      "VALUES (42, 'p', 'Suppressed dup', 'body', 1753000000, 0)")
            c.execute("INSERT INTO observations (id, project, title, text, created_at_epoch, relevance_count) "
                      "VALUES (7, 'p', 'Canonical obs', 'body', 1753000000, 3)")
            c.commit(); c.close()
            with mock.patch.object(mem_recall, "CM", cmdb), \
                 mock.patch.object(mem_recall, "suppressed_obs_ids", return_value={42}):
                boosts = {("claudemem", "42"): 1.0, ("claudemem", "7"): 1.0}
                out, _mode = mem_recall.claudemem("q", ["zzznomatch"], boosts)
        ids = {row[1] for row in out}
        self.assertIn(7, ids)
        self.assertNotIn(42, ids)

    def test_mem_recall_filters_overlay(self):
        import mem_recall
        with tempfile.TemporaryDirectory() as d:
            semdb = os.path.join(d, "semantic.db")
            c = sqlite3.connect(semdb)
            c.execute("CREATE TABLE suppressed_observation (obs_id INTEGER PRIMARY KEY,"
                      " canonical_id INTEGER, reason TEXT, created_at TEXT)")
            c.execute("INSERT INTO suppressed_observation VALUES (42, 7, 'dup', 'now')")
            c.commit(); c.close()
            with mock.patch.object(mem_recall.os.path, "expanduser",
                                   side_effect=lambda p: semdb if "semantic.db" in p else p):
                ids = mem_recall.suppressed_obs_ids()
        self.assertEqual(ids, {42})

    def test_fail_open_when_overlay_missing(self):
        import mem_recall
        with mock.patch.object(mem_recall.os.path, "expanduser",
                               side_effect=lambda p: "/nonexistent/semantic.db" if "semantic.db" in p else p):
            self.assertEqual(mem_recall.suppressed_obs_ids(), set())


class TestKimiIngestFilter(unittest.TestCase):
    def test_noise_patterns(self):
        pat = re.compile(r"^(new session|proceed|continue|go|ok(ay)?|yes|no|test|hi|hello"
                         r"|are you there\??|\.{1,3})$", re.I)
        for noise in ("new session", "Proceed", "are you there?", "ok", "..."):
            self.assertTrue(pat.match(noise), noise)
        for real in ("fix the invoice mapper", "new session handling for drivers", "go over the census"):
            self.assertFalse(pat.match(real), real)


if __name__ == "__main__":
    unittest.main(verbosity=1)
