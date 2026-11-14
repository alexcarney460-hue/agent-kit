from contextlib import closing
"""Portable recall, privacy gate and default-off sender checks."""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest import mock

import embed_index
import eval_recall
import graph_policy
import hm_write
import mem_recall


def test_fusion_fades_and_redirects_once():
    result = mem_recall.fuse([(1.0, 1, ["a", "old", "root"])],
                             lambda key: "root" if key == "old" else key)
    assert result["a"] == 1 / 61
    assert abs(result["root"] - (1 / 61) * (2 / 3)) < 1e-12


def test_no_remote_sender_when_cloud_disabled(monkeypatch):
    monkeypatch.delenv("AGENT_KIT_CLOUD", raising=False)
    monkeypatch.delenv("HM_ADJUDICATE", raising=False)
    with mock.patch("subprocess.run", side_effect=AssertionError("sender reached")):
        assert hm_write.adjudicate("text", "context", [(1, "earlier", "", 0)]) is None
        assert eval_recall.generate() is None
    for endpoint in ("https://example.invalid/embed", "http://127.0.0.1:11434/api/embed"):
        with mock.patch.object(embed_index, "OLLAMA", endpoint), \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("sender reached")) as sender:
            assert embed_index.embed_batch(["private memory"]) is None
            assert not sender.called


def test_eval_dry_run_never_calls_sender(monkeypatch):
    monkeypatch.setenv("AGENT_KIT_CLOUD", "1")
    monkeypatch.setattr(sys, "argv", ["eval_recall.py", "--generate", "--run", "--dry-run"])
    with mock.patch.object(eval_recall, "generate", side_effect=AssertionError("sender reached")), \
         mock.patch.object(eval_recall, "run", side_effect=AssertionError("evaluation reached")):
        eval_recall.main()


def test_empty_overlay_fallback_finds_plugin_record(tmp_path):
    db = tmp_path / "plugin.db"
    with closing(sqlite3.connect(str(db))) as conn, conn:
        conn.execute("CREATE TABLE observations (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO observations VALUES (1)")
    sources = graph_policy.Sources(sem_db=str(tmp_path / "absent.db"),
        hivemind=str(tmp_path / "absent-hive.db"), claudemem=str(db),
        memdir=str(tmp_path / "absent-dir"))
    view = graph_policy.fallback_view("missing", sources=sources)
    assert view.is_eligible("claudemem", "1")
    assert graph_policy.recheck(view, sources=sources) == (True, "")


def test_fresh_hive_apostrophe_recalled(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    db = home / "memory.db"
    tools = Path(__file__).resolve().parent
    with closing(sqlite3.connect(str(db))) as conn, conn:
        conn.executescript((tools / "hivemind-schema.sql").read_text(encoding="utf-8"))
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), HIVEMIND_DB=str(db),
               HM_NO_SEM="1", HM_NO_LLM="1")
    env.pop("AGENT_KIT_CLOUD", None)
    env.pop("HM_ADJUDICATE", None)
    cmd = [sys.executable, str(tools / "hm_write.py"), "context", "Teacher's favorite telescope"]
    write = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True)
    assert "SAVED" in write.stdout
    recall = subprocess.run([sys.executable, str(tools / "mem_recall.py"), "telescope", "--no-bump"],
                            env=env, capture_output=True, text=True, check=True)
    assert "Teacher's favorite telescope" in recall.stdout


def test_eval_missing_stores(monkeypatch, tmp_path):
    monkeypatch.setattr(eval_recall, "HM", str(tmp_path / "missing-hm"))
    monkeypatch.setattr(eval_recall, "CM", str(tmp_path / "missing-cm"))
    monkeypatch.setattr(eval_recall, "MEMDIR", str(tmp_path / "missing-dir"))
    assert eval_recall.sample_records() == []


def test_health_empty_home_and_corrupt_store(monkeypatch, tmp_path, capsys):
    import mem_check
    monkeypatch.setattr(mem_check.memdir, "hivemind_db", lambda: str(tmp_path / "hm"))
    monkeypatch.setattr(mem_check.memdir, "home", lambda: str(tmp_path))
    assert mem_check.main() == 0
    assert "OK" in capsys.readouterr().out
    (tmp_path / "hm").write_bytes(b"not a database")
    assert mem_check.main() == 1
    assert "unable to verify" in capsys.readouterr().out


def test_sender_guards_are_live(monkeypatch, tmp_path):
    """Opt-in permits the shared launcher and the local embedding sender."""
    monkeypatch.setenv("AGENT_KIT_CLOUD", "1")
    with mock.patch.object(eval_recall, "sample_records", return_value=[{"store": "file", "ref": "x", "text": "memory"}]), \
         mock.patch.object(eval_recall.model_cli, "run_model", return_value='[]') as sender:
        eval_recall.generate(qfile=str(tmp_path / "questions.json"))
        assert sender.call_count == 1
    with mock.patch.object(embed_index.urllib.request, "urlopen", side_effect=AssertionError("sender reached")) as sender:
        embed_index.embed_batch(["memory"])
        assert sender.call_count == 1
    with mock.patch.object(eval_recall.model_cli, "run_model", return_value='{"verdict":"NEW"}') as sender:
        assert hm_write.adjudicate("memory", "", [(1, "old", "", 0)])["verdict"] == "NEW"
        assert sender.call_count == 1


def test_parallel_store_reads_and_fusion():
    import threading
    barrier = threading.Barrier(4, timeout=5)
    view = mock.Mock()
    view.store_available.return_value = True
    view.is_eligible.return_value = True
    view.hivemind_redirect = {}
    def leg(value):
        def run(*args, **kwargs):
            barrier.wait()
            return value
        return run
    hm = [(9.0, 7, 0, "context", "rule", False, True)]
    with mock.patch.object(mem_recall, "sem", None), \
         mock.patch.object(mem_recall, "hivemind", side_effect=leg(hm)), \
         mock.patch.object(mem_recall, "claudemem", side_effect=leg(([], "fts5"))), \
         mock.patch.object(mem_recall, "files", side_effect=leg([])), \
         mock.patch.object(mem_recall, "_vector_lists", side_effect=leg([[('hivemind', '7')]])), \
         mock.patch.object(graph_policy, "recheck", return_value=(True, "")):
        ranked = mem_recall.fused_search("rule", view=view)
    assert ranked[0][:2] == ("hivemind", "7")
    assert ranked[0][2] > 1 / 61


def test_view_budget_rejects_excess_source_ids(tmp_path):
    import pytest
    db = tmp_path / "hive.db"
    with closing(sqlite3.connect(str(db))) as c, c:
        c.execute("CREATE TABLE learnings (id INTEGER, active INTEGER, superseded_by INTEGER)")
        c.executemany("INSERT INTO learnings VALUES (?, 1, NULL)", [(1,), (2,)])
    budget = graph_policy.Budgets(max_source_ids=1)
    with pytest.raises(graph_policy.GraphUnavailable, match="budget"):
        graph_policy.hivemind_membership(str(db), budget, graph_policy._Deadline(15))


def test_vector_lists_take_each_store_and_skip_ineligible(monkeypatch, tmp_path):
    import pytest
    if embed_index.np is None:
        pytest.skip("vector acceleration requires optional numpy")
    db = tmp_path / "index.db"
    db.touch()
    monkeypatch.setattr(embed_index, "DB", str(db))
    monkeypatch.setattr(embed_index, "connect", lambda: mock.Mock())
    rows = {"file": [("a.md", .9), ("b.md", .8)], "hivemind": [("1", .8)],
            "claudemem": [], "hm_redirect": []}
    monkeypatch.setattr(embed_index, "ranked_by_store", lambda conn, q, stores: {s: iter(rows[s]) for s in stores})
    view = mock.Mock()
    view.hivemind_redirect = {}
    view.store_available.return_value = True
    view.is_eligible.side_effect = lambda store, ref: ref != "a.md"
    found = mem_recall._vector_lists("query", view)
    assert [("file", "b.md")] in found
    assert [("hivemind", "1")] in found


def test_semantic_boosts_only_use_eligible_view(monkeypatch, tmp_path):
    import sem
    db = tmp_path / "semantic.db"
    with closing(sqlite3.connect(str(db))) as c, c:
        c.executescript((Path(__file__).parent / "semantic" / "schema.sql").read_text(encoding="utf-8"))
        c.execute("INSERT INTO entity (id, slug, kind, name, created_at, updated_at) VALUES (1, 'observatory', 'concept', 'Observatory', 'now', 'now')")
        c.execute("INSERT INTO alias (entity_id, alias) VALUES (1, 'observatory')")
    monkeypatch.setattr(graph_policy, "SEM_DB", str(db))
    monkeypatch.setattr(sem, "DB", str(db))
    view = mock.Mock()
    view.links = ((1, "file", "allowed.md", 1.0, "legacy"),)
    view.edges = ()
    boosts, matched = sem.recall_boosts("observatory", view=view)
    assert matched and ("file", "allowed.md") in boosts
    assert ("file", "withheld.md") not in boosts


def test_graph_writer_lock_is_exclusive(tmp_path):
    import graph_state
    import pytest
    target = str(tmp_path / "semantic.db")
    with graph_state.WriterLock(target):
        with pytest.raises(graph_state.LockHeld):
            with graph_state.WriterLock(target):
                pass
    with graph_state.WriterLock(target):
        pass


def test_embedding_update_without_numpy_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(embed_index, "np", None)
    conn = embed_index.connect(str(tmp_path / "embeddings.db"))
    try:
        with mock.patch.object(embed_index, "corpus", side_effect=AssertionError("read started")), \
             mock.patch.object(embed_index, "embed_batch", side_effect=AssertionError("sender reached")):
            result = embed_index.update(conn)
        assert result["unavailable"] == "numpy not installed"
        assert conn.execute("SELECT COUNT(*) FROM embedding").fetchone()[0] == 0
    finally:
        conn.close()


def test_embedding_rejects_remote_even_with_opt_in(monkeypatch):
    monkeypatch.setenv("AGENT_KIT_CLOUD", "1")
    monkeypatch.setattr(embed_index, "OLLAMA", "https://example.invalid/embed")
    with mock.patch.object(embed_index.urllib.request, "urlopen", side_effect=AssertionError("sender reached")) as sender:
        assert embed_index.embed_batch(["memory"]) is None
        assert not sender.called


def test_graph_writer_uri_escapes_reserved_characters(tmp_path):
    import graph_state
    db = tmp_path / ("memory #1.db" if os.name == "nt" else "memory #1?.db")
    sqlite3.connect(str(db)).close()
    conn = graph_state.writer_conn(str(db))
    try:
        conn.execute("CREATE TABLE sample (id INTEGER)")
    finally:
        conn.close()
    with closing(sqlite3.connect(str(db))) as conn, conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='sample'").fetchone() == ("sample",)
