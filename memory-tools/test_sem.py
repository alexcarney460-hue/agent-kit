#!/usr/bin/env python3
"""Tests for the memory semantic layer.

Runs under pytest if it is installed, and standalone if it is not:
    python3 test_sem.py
Every test builds its own throwaway database, so the real semantic.db is never
touched.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sem  # noqa: E402


def fresh() -> sqlite3.Connection:
    """A schema-applied database in a temp file, isolated from the real one."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    sem.DB = path
    return sem.connect()


def build(conn: sqlite3.Connection) -> dict[str, int]:
    ids = {}
    ids["jamie"] = sem.upsert_entity(conn, "jamie", "person", "Jamie Alexandra Rivera", protected=1)
    for a in ("jamie rivera", "jamie alexandra rivera", "jrivera-dev"):
        sem.add_alias(conn, ids["jamie"], a)
    ids["group"] = sem.upsert_entity(conn, "acme", "venture", "Acme")
    sem.add_alias(conn, ids["group"], "acme")
    ids["debt"] = sem.upsert_entity(conn, "acme-holdings", "org", "Acme Holdings LLC",
                                    summary="THE PARENT COMPANY. Owns the group.")
    sem.add_alias(conn, ids["debt"], "acme holdings llc")
    sem.add_alias(conn, ids["debt"], "acme holdings")
    sem.add_edge(conn, ids["debt"], "part-of", ids["group"])
    conn.commit()
    return ids


# --------------------------------------------------------------------------- #
def test_alias_resolves_legal_and_common_name_to_one_entity() -> None:
    conn = fresh(); ids = build(conn)
    a = sem.resolve(conn, "notes about Jamie Rivera")
    b = sem.resolve(conn, "notes about Jamie Alexandra Rivera")
    assert a and b, "both phrasings must resolve"
    assert a[0].entity_id == b[0].entity_id == ids["jamie"]
    conn.close()


def test_longest_alias_wins_over_substring() -> None:
    """'acme holdings llc' must beat the bare 'acme' umbrella."""
    conn = fresh(); ids = build(conn)
    top = sem.resolve(conn, "who owes money at acme holdings llc")[0]
    assert top.entity_id == ids["debt"], f"resolved to {top.slug} instead of the specific entity"
    conn.close()


def test_soft_resolution_matches_description_not_name() -> None:
    """A query with none of the entity's names still reaches it via its summary."""
    conn = fresh(); ids = build(conn)
    hits = [m.entity_id for m in sem.resolve(conn, "which is the parent company carries balance")]
    assert ids["debt"] in hits
    conn.close()


def test_soft_resolution_needs_two_terms() -> None:
    conn = fresh(); build(conn)
    assert sem._soft(conn, "debt", set()) == []
    conn.close()


def test_hub_is_not_expanded_into() -> None:
    """A hub neighbour must not ride in and flood the result."""
    conn = fresh(); ids = build(conn)
    for i in range(400):                      # make 'acme' a hub
        sem.add_mention(conn, ids["group"], "claudemem", str(i))
    sem.add_mention(conn, ids["debt"], "claudemem", "9001")
    conn.commit()
    nb = sem.neighbors(conn, [ids["debt"]])
    assert ids["debt"] in nb
    assert ids["group"] not in nb, "hub was expanded into"
    conn.close()


def test_hub_still_usable_when_resolved_directly() -> None:
    conn = fresh(); ids = build(conn)
    for i in range(400):
        sem.add_mention(conn, ids["group"], "claudemem", str(i))
    conn.commit()
    nb = sem.neighbors(conn, [ids["group"]])
    assert nb.get(ids["group"]) == 1.0
    conn.close()


def test_idf_damps_hub_contributions() -> None:
    """A record linked only to a hub must score below one linked to a rare entity."""
    conn = fresh(); ids = build(conn)
    for i in range(400):
        sem.add_mention(conn, ids["group"], "claudemem", str(i))
    sem.add_mention(conn, ids["debt"], "claudemem", "9001")
    conn.commit()
    ms = sem.mentions(conn, {ids["group"]: 1.0, ids["debt"]: 1.0})
    assert ms[("claudemem", "9001")] > ms[("claudemem", "5")]
    conn.close()


def test_mention_upsert_keeps_max_weight() -> None:
    conn = fresh(); ids = build(conn)
    sem.add_mention(conn, ids["jamie"], "hivemind", "7", 0.4)
    sem.add_mention(conn, ids["jamie"], "hivemind", "7", 0.9)
    sem.add_mention(conn, ids["jamie"], "hivemind", "7", 0.2)
    conn.commit()
    w = conn.execute("SELECT weight FROM mention WHERE ref='7'").fetchone()[0]
    assert w == 0.9
    assert conn.execute("SELECT count(*) FROM mention WHERE ref='7'").fetchone()[0] == 1
    conn.close()


def test_vocabularies_are_enforced() -> None:
    conn = fresh(); ids = build(conn)
    for call, bad in (
        (lambda: sem.upsert_entity(conn, "x", "wombat", "X"), "kind"),
        (lambda: sem.add_edge(conn, ids["jamie"], "loves", ids["debt"]), "rel"),
        (lambda: sem.add_mention(conn, ids["jamie"], "postgres", "1"), "store"),
    ):
        try:
            call()
        except ValueError:
            continue
        raise AssertionError(f"invalid {bad} was accepted")
    conn.close()


def test_edges_are_idempotent() -> None:
    conn = fresh(); ids = build(conn)
    for _ in range(3):
        sem.add_edge(conn, ids["debt"], "part-of", ids["group"])
    conn.commit()
    assert conn.execute("SELECT count(*) FROM edge").fetchone()[0] == 1
    conn.close()


def test_recall_boosts_degrade_when_db_absent() -> None:
    """A missing layer must never break recall."""
    sem.DB = "/nonexistent/path/semantic.db"
    assert sem.available() is False
    assert sem.recall_boosts("anything") == ({}, [])


def test_mem_recall_runs_without_the_layer() -> None:
    """mem_recall must import and score with an empty boost map."""
    import mem_recall
    assert mem_recall.terms("the Acme contract") == ["acme", "contract"]
    out = mem_recall.files("acme", ["acme"], {})
    assert isinstance(out, list)


def test_keys_tree_is_never_content_read() -> None:
    """~/keys holds live credentials. Indexing must use paths only, forever."""
    import sem_artifacts
    roots = {r[0]: r[1] for r in sem_artifacts.ROOTS}
    assert "~/keys" in roots, "the keys root disappeared from the config"
    assert roots["~/keys"] is False, "SECURITY: ~/keys is configured to read file contents"


def test_workspace_indexes_notes_not_source() -> None:
    """Recall should return the note or the contract, not thousands of .py files."""
    import sem_artifacts
    ws = [r for r in sem_artifacts.ROOTS if r[0] == "~/workspace"][0]
    assert ws[3] == {".md", ".txt"}, f"workspace extension filter widened to {ws[3]}"


def test_artifact_walk_skips_junk_directories() -> None:
    import sem_artifacts
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "node_modules"))
        os.makedirs(os.path.join(d, "keep"))
        open(os.path.join(d, "node_modules", "a.md"), "w").close()
        open(os.path.join(d, "keep", "b.md"), "w").close()
        found = [os.path.basename(p) for p, _ in sem_artifacts.walk(d, 3)]
        assert "b.md" in found
        assert "a.md" not in found, "walked into node_modules"


def test_capitalisation_ratio_separates_names_from_common_nouns() -> None:
    """The feature the whole detector rests on."""
    import sem_discover
    name = sem_discover.Cand("Contoso")
    name.total, name.anycase = 40, 40          # never written lowercase
    noun = sem_discover.Cand("Change")
    noun.total, noun.anycase = 40, 400         # lowercase almost always
    assert name.cap_ratio == 1.0
    assert noun.cap_ratio < 0.2
    for c in (name, noun):
        c.midcap = c.total
        c.records = set(range(20))
    assert name.score() > noun.score() * 3, "a common noun scores too close to a real name"


def test_all_caps_single_token_is_penalised() -> None:
    """PDT / UTC / DOM are shouty formatting, not names."""
    import sem_discover
    shout = sem_discover.Cand("PDT")
    shout.total = shout.anycase = shout.midcap = 40
    shout.records = set(range(20))
    normal = sem_discover.Cand("Contoso")
    normal.total = normal.anycase = normal.midcap = 40
    normal.records = set(range(20))
    assert shout.score() < normal.score()


def test_classifier_failure_creates_nothing() -> None:
    """No CLI, a timeout or bad JSON must never create a garbage entity."""
    import sem_discover
    c = sem_discover.Cand("Whatever")
    c.total = c.anycase = 5
    real = sem_discover.subprocess.run
    try:
        sem_discover.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(OSError("no cli"))
        assert sem_discover.classify([c]) == {}
    finally:
        sem_discover.subprocess.run = real


def test_rejected_candidates_table_exists() -> None:
    conn = fresh()
    conn.execute("INSERT INTO rejected_candidate (phrase, reason, rejected_at) VALUES (?,?,?)",
                 ("pdt", "not an entity", sem.now()))
    conn.commit()
    assert conn.execute("SELECT count(*) FROM rejected_candidate").fetchone()[0] == 1
    conn.close()


def test_provisional_entities_rank_below_curated() -> None:
    """An auto-detected entity must never outrank a curated one on the same query."""
    conn = fresh()
    cur = sem.upsert_entity(conn, "acme-global", "org", "Acme Global", confidence=0.7)
    sem.add_alias(conn, cur, "acme global")
    prov = sem.upsert_entity(conn, "acme-globe", "org", "Acme Globe",
                             confidence=0.35, source="auto-detected")
    sem.add_alias(conn, prov, "acme globe")
    conn.commit()
    a = sem.resolve(conn, "acme global")[0]
    b = sem.resolve(conn, "acme globe")[0]
    assert a.entity_id == cur and b.entity_id == prov
    assert a.score > b.score, "provisional entity scored at or above a curated one"
    conn.close()


def test_artifact_store_accepted_by_mention() -> None:
    conn = fresh(); ids = build(conn)
    sem.add_mention(conn, ids["jamie"], "artifact", "/tmp/x.docx", 1.0)
    conn.commit()
    assert conn.execute(
        "SELECT count(*) FROM mention WHERE store='artifact'").fetchone()[0] == 1
    conn.close()


# --------------------------------------------------------------------------- #
def _run_standalone() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    real_db = sem.DB
    for name, fn in tests:
        sem.DB = real_db
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as exc:  # noqa: BLE001 - test runner reports everything
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    sem.DB = real_db
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
