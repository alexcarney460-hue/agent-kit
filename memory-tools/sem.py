#!/usr/bin/env python3
"""sem — the semantic layer over the memory stack.

The stores (hivemind, claude-mem, file memories) record episodes and rules. They
record no THINGS, so retrieval can only match strings and misses anything filed
under a different name. This module adds the missing vocabulary: canonical
entities, their aliases, typed relations, and which record in which store is
about them.

It never writes to hivemind or claude-mem. Its own database is
~/.claude/memory-tools/semantic.db and is safe to delete and rebuild.

Usage:
  sem.py init                                  create/upgrade the schema
  sem.py seed [--file semantic/seed.json]      upsert curated entities + edges
  sem.py resolve "query text"                  query -> entities (via aliases)
  sem.py show <slug>                           entity, aliases, edges, mentions
  sem.py brief "query text" [--k 6]            task-scoped context brief
  sem.py add <slug> <kind> "Name" [--summary .. --alias a --alias b --protected]
  sem.py alias <slug> "another name"
  sem.py link <src-slug> <rel> <dst-slug> [--note ..]
  sem.py mention <slug> <store> <ref> [--weight 1.0]
  sem.py stats
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from typing import Iterable, NamedTuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memdir import memory_dir  # noqa: E402

DB = os.path.expanduser("~/.claude/memory-tools/semantic.db")
SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "semantic", "schema.sql")
SEED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "semantic", "seed.json")

KINDS = {"person", "org", "venture", "system", "artifact", "account", "concept"}
RELS = {
    "part-of", "owns", "party-to", "counterparty-of", "works-with",
    "supersedes", "contradicts", "evidence-for", "depends-on", "about",
}
STORES = {"hivemind", "claudemem", "file", "artifact"}

# Aliases shorter than this only match on a whole-word boundary AND are never
# allowed to be the sole reason an entity resolves (too much noise: "q", "nwf").
_SHORT_ALIAS = 4
# an entity linked to more than this share of all records carries no signal
HUB_FRACTION = 0.08
# floor for a weakly-resolved entity's seed weight
MIN_SEED = 0.3


class Match(NamedTuple):
    """An entity the query resolved to."""

    entity_id: int
    slug: str
    kind: str
    name: str
    summary: str
    protected: int
    alias: str
    score: float


@dataclass(frozen=True)
class Mention:
    store: str
    ref: str
    weight: float


def now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def connect(create: bool = True) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    conn = sqlite3.connect(DB, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.text_factory = lambda b: b.decode("utf-8", "replace") if isinstance(b, (bytes, bytearray)) else b
    if create:
        with open(SCHEMA, encoding="utf-8") as fh:
            conn.executescript(fh.read())
        conn.commit()
    return conn


def available() -> bool:
    """True when the layer exists and has entities. Callers degrade gracefully."""
    if not os.path.exists(DB):
        return False
    try:
        conn = sqlite3.connect(DB, timeout=5)
        n = conn.execute("SELECT count(*) FROM entity").fetchone()[0]
        conn.close()
        return n > 0
    except sqlite3.Error:
        return False


# --------------------------------------------------------------------------- #
# resolution: query text -> entities
# --------------------------------------------------------------------------- #
_STOP = {"and", "the", "for", "with", "that", "this", "from", "into", "are", "was", "our",
         "your", "you", "how", "what", "when", "which", "who", "does", "did", "not", "but",
         "all", "any", "its", "via", "has", "have", "can", "should", "would", "about"}


def _soft(conn: sqlite3.Connection, query: str, exclude: set[int]) -> list[Match]:
    """Descriptive fallback: match query terms against name/kind/summary.

    Alias matching is exact, so a query phrased by description rather than by
    name ("who signs for the parent company") resolves to nothing. Summaries carry
    those descriptions, so scan them — scored well below a real alias hit, and
    requiring at least two query terms to agree so it does not fire on noise.
    """
    tg = [w for w in re.findall(r"[a-z0-9]{3,}", query.lower()) if w not in _STOP]
    if len(tg) < 2:
        return []
    rows = conn.execute(
        "SELECT id, slug, kind, name, COALESCE(summary,''), protected, confidence FROM entity"
    ).fetchall()
    out: list[Match] = []
    for eid, slug, kind, name, summary, prot, conf in rows:
        if eid in exclude:
            continue
        blob = f"{kind} {name} {summary}".lower()
        hits = sum(1 for t in tg if re.search(rf"\b{re.escape(t)}", blob))
        if hits < 2:
            continue
        score = 2.0 * hits * (1.0 + 0.5 * prot) * max(0.1, conf)
        out.append(Match(eid, slug, kind, name, summary, prot, f"~summary×{hits}", score))
    return sorted(out, key=lambda m: -m.score)


def resolve(conn: sqlite3.Connection, query: str, limit: int = 12, soft: bool = True) -> list[Match]:
    """Resolve free text to entities by alias match, then by description.

    Matches whole-word, longest-alias-first, so "acme holdings llc" resolves to
    the parent company rather than to the bare "acme" umbrella. This is the piece
    that makes "Jamie Alexandra Rivera" and "Jamie Rivera" the same node.
    """
    q = " " + re.sub(r"[^a-z0-9]+", " ", query.lower()).strip() + " "
    rows = conn.execute(
        "SELECT a.alias, e.id, e.slug, e.kind, e.name, COALESCE(e.summary,''), e.protected, e.confidence "
        "FROM alias a JOIN entity e ON e.id = a.entity_id"
    ).fetchall()

    best: dict[int, Match] = {}
    for alias, eid, slug, kind, name, summary, prot, conf in rows:
        norm = " " + re.sub(r"[^a-z0-9]+", " ", alias.lower()).strip() + " "
        if norm.strip() == "" or norm not in q:
            continue
        # longer aliases are more specific; short ones contribute but can't stand alone
        specificity = len(norm.strip())
        score = specificity * (1.0 + 0.5 * prot) * max(0.1, conf)
        if specificity < _SHORT_ALIAS:
            score *= 0.35
        prev = best.get(eid)
        if prev is None or score > prev.score:
            best[eid] = Match(eid, slug, kind, name, summary, prot, alias, score)

    out = sorted(best.values(), key=lambda m: -m.score)
    # drop entities that only matched on a short alias when better matches exist
    strong = [m for m in out if len(m.alias.strip()) >= _SHORT_ALIAS]
    out = strong or out
    if soft:
        out = out + _soft(conn, query, {m.entity_id for m in out})
    return out[:limit]


def neighbors(conn: sqlite3.Connection, ids: Iterable[int] | dict[int, float],
              decay: float = 0.45) -> dict[int, float]:
    """One hop out along typed edges, down-weighted. Returns {entity_id: weight}.

    Accepts either bare ids (all seeded at full strength) or {id: confidence}.
    Passing confidence matters: a query that matched a weak three-letter alias
    should not boost as hard as one that matched a full name, and a neighbour
    inherits the confidence of the seed it hung off.
    """
    seeds: dict[int, float] = dict(ids) if isinstance(ids, dict) else {i: 1.0 for i in ids}
    if not seeds:
        return {}
    ids_l = list(seeds)
    marks = ",".join("?" * len(ids_l))
    rows = conn.execute(
        f"SELECT src, dst, rel FROM edge WHERE src IN ({marks}) OR dst IN ({marks})",
        ids_l + ids_l,
    ).fetchall()
    seed = set(seeds)
    out: dict[int, float] = dict(seeds)
    # Never EXPAND into a hub. An umbrella company like `acme` ends up linked to
    # ~12% of all records, so one hop from any Acme entity would pull in every
    # note ever filed under the umbrella. A hub reached directly by the query is
    # still fine — this only blocks it from riding in as a neighbour.
    total = conn.execute("SELECT count(*) FROM mention").fetchone()[0] or 1
    hub_cut = max(50, int(HUB_FRACTION * total))
    hubs = {r[0] for r in conn.execute(
        "SELECT entity_id FROM mention GROUP BY entity_id HAVING count(*) > ?", (hub_cut,))}
    for src, dst, rel in rows:
        other = dst if src in seed else src
        if other in seed or other in hubs:
            continue
        # a superseding/contradicting neighbour matters more than a passing one
        base = seeds.get(src, seeds.get(dst, 1.0))
        bump = decay * base * (1.4 if rel in ("supersedes", "contradicts", "evidence-for") else 1.0)
        out[other] = max(out.get(other, 0.0), bump)
    return out


def mentions(conn: sqlite3.Connection, weights: dict[int, float]) -> dict[tuple[str, str], float]:
    """Records in the underlying stores that are about these entities.

    Contributions are scaled by inverse mention frequency. Without this, a hub
    entity drowns the result: an umbrella like `acme` can be linked to ~1k
    records, so expanding one hop from any Acme entity into the umbrella pulls
    in every homepage-redesign note ever written. A rare entity is strong
    evidence; a hub is nearly none.
    """
    if not weights:
        return {}
    marks = ",".join("?" * len(weights))
    # IDF is computed PER STORE. Globally-normalised IDF meant that indexing
    # thousands of files silently re-ranked hivemind records, which is wrong: a
    # learning's weight must not depend on how many documents live on disk.
    totals = dict(conn.execute("SELECT store, count(*) FROM mention GROUP BY store").fetchall())
    freq = {(s, e): n for s, e, n in conn.execute(
        f"SELECT store, entity_id, count(*) FROM mention WHERE entity_id IN ({marks}) "
        f"GROUP BY store, entity_id", list(weights)).fetchall()}
    rows = conn.execute(
        f"SELECT entity_id, store, ref, weight FROM mention WHERE entity_id IN ({marks})",
        list(weights),
    ).fetchall()
    out: dict[tuple[str, str], float] = {}
    for eid, store, ref, w in rows:
        total = max(2, totals.get(store, 1))
        n = max(1, freq.get((store, eid), 1))
        idf = math.log(total / n) / math.log(total)  # 1.0 for a singleton, ->0 for a hub
        key = (store, str(ref))
        out[key] = max(out.get(key, 0.0), w * weights.get(eid, 0.0) * idf)
    return out


def recall_boosts(query: str) -> tuple[dict[tuple[str, str], float], list[Match]]:
    """Entry point used by mem_recall. Returns ({(store, ref): boost}, matches).

    Never raises: a missing or broken layer degrades to no boosts.
    """
    if not available():
        return {}, []
    try:
        conn = connect(create=False)
        matched = resolve(conn, query)
        if not matched:
            conn.close()
            return {}, []
        # Seed each entity by HOW WELL it resolved, not uniformly. A bare "nwf"
        # match is far weaker evidence than "Northwind Finance" and must not
        # boost equally — that uniform seeding was the precision leak.
        top = max(m.score for m in matched) or 1.0
        seeds = {m.entity_id: max(MIN_SEED, min(1.0, m.score / top)) for m in matched}
        weights = neighbors(conn, seeds)
        boosts = mentions(conn, weights)
        conn.close()
        return boosts, matched
    except sqlite3.Error as exc:
        sys.stderr.write(f"[semantic layer skipped: {exc}]\n")
        return {}, []


# --------------------------------------------------------------------------- #
# writes
# --------------------------------------------------------------------------- #
def upsert_entity(conn: sqlite3.Connection, slug: str, kind: str, name: str, *,
                  summary: str | None = None, valid_as_of: str | None = None,
                  source: str | None = None, confidence: float = 0.7,
                  protected: int = 0) -> int:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}, got {kind!r}")
    ts = now()
    conn.execute(
        "INSERT INTO entity (slug,kind,name,summary,valid_as_of,source,confidence,protected,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(slug) DO UPDATE SET kind=excluded.kind, name=excluded.name, "
        "summary=COALESCE(excluded.summary, entity.summary), "
        "valid_as_of=COALESCE(excluded.valid_as_of, entity.valid_as_of), "
        "source=COALESCE(excluded.source, entity.source), "
        "confidence=excluded.confidence, protected=excluded.protected, updated_at=excluded.updated_at",
        (slug, kind, name, summary, valid_as_of, source, confidence, protected, ts, ts),
    )
    return conn.execute("SELECT id FROM entity WHERE slug=?", (slug,)).fetchone()[0]


def add_alias(conn: sqlite3.Connection, entity_id: int, alias: str) -> None:
    a = alias.strip().lower()
    if a:
        conn.execute("INSERT OR IGNORE INTO alias (entity_id, alias) VALUES (?,?)", (entity_id, a))


def add_edge(conn: sqlite3.Connection, src: int, rel: str, dst: int,
             note: str | None = None, valid_as_of: str | None = None) -> None:
    if rel not in RELS:
        raise ValueError(f"rel must be one of {sorted(RELS)}, got {rel!r}")
    conn.execute(
        "INSERT OR IGNORE INTO edge (src,rel,dst,note,valid_as_of,created_at) VALUES (?,?,?,?,?,?)",
        (src, rel, dst, note, valid_as_of, now()),
    )


def add_mention(conn: sqlite3.Connection, entity_id: int, store: str, ref: str,
                weight: float = 1.0) -> None:
    if store not in STORES:
        raise ValueError(f"store must be one of {sorted(STORES)}, got {store!r}")
    conn.execute(
        "INSERT INTO mention (entity_id,store,ref,weight,created_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(entity_id,store,ref) DO UPDATE SET weight=MAX(mention.weight, excluded.weight)",
        (entity_id, store, str(ref), weight, now()),
    )


def prune(conn: sqlite3.Connection) -> dict[str, int]:
    """Drop mentions whose target no longer exists.

    Used by the scheduled refresh instead of `--reset`. A blanket delete leaves a
    window where the layer is empty, and a session querying during that window
    silently gets no entity boosts. Pruning only what is actually gone keeps the
    layer continuously usable.
    """
    hm = os.path.expanduser("~/hivemind.db")
    cm = os.path.expanduser("~/.claude-mem/claude-mem.db")
    memdir = memory_dir()
    removed = {"artifact": 0, "file": 0, "hivemind": 0, "claudemem": 0}

    for (ref,) in conn.execute("SELECT DISTINCT ref FROM mention WHERE store='artifact'").fetchall():
        if not os.path.exists(ref):
            removed["artifact"] += conn.execute(
                "DELETE FROM mention WHERE store='artifact' AND ref=?", (ref,)).rowcount

    for (ref,) in conn.execute("SELECT DISTINCT ref FROM mention WHERE store='file'").fetchall():
        if not os.path.exists(os.path.join(memdir, ref)):
            removed["file"] += conn.execute(
                "DELETE FROM mention WHERE store='file' AND ref=?", (ref,)).rowcount

    for store, path, sql in (
        ("hivemind", hm, "SELECT id FROM learnings WHERE active=1"),
        ("claudemem", cm, "SELECT id FROM observations"),
    ):
        if not os.path.exists(path):
            continue
        try:
            src = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
            live = {str(r[0]) for r in src.execute(sql)}
            src.close()
        except sqlite3.Error:
            continue
        for (ref,) in conn.execute(
                "SELECT DISTINCT ref FROM mention WHERE store=?", (store,)).fetchall():
            if ref not in live:
                removed[store] += conn.execute(
                    "DELETE FROM mention WHERE store=? AND ref=?", (store, ref)).rowcount
    conn.commit()
    return removed


def entity_id(conn: sqlite3.Connection, slug: str) -> int:
    row = conn.execute("SELECT id FROM entity WHERE slug=?", (slug,)).fetchone()
    if row is None:
        raise SystemExit(f"no such entity: {slug}")
    return row[0]


def load_seed(conn: sqlite3.Connection, path: str) -> tuple[int, int, int]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    n_e = n_a = n_x = 0
    for e in data.get("entities", []):
        eid = upsert_entity(
            conn, e["slug"], e["kind"], e["name"], summary=e.get("summary"),
            valid_as_of=e.get("valid_as_of"), source=e.get("source"),
            confidence=float(e.get("confidence", 0.7)), protected=int(e.get("protected", 0)),
        )
        n_e += 1
        for a in [e["name"], e["slug"].replace("-", " "), *e.get("aliases", [])]:
            add_alias(conn, eid, a)
            n_a += 1
    for x in data.get("edges", []):
        try:
            add_edge(conn, entity_id(conn, x["src"]), x["rel"], entity_id(conn, x["dst"]), x.get("note"))
            n_x += 1
        except SystemExit:
            sys.stderr.write(f"[seed] skipped edge with unknown slug: {x}\n")
    conn.commit()
    return n_e, n_a, n_x


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def cmd_brief(conn: sqlite3.Connection, query: str, k: int) -> None:
    matched = resolve(conn, query)
    if not matched:
        print(f"no entities resolved from {query!r}")
        return
    weights = neighbors(conn, [m.entity_id for m in matched])
    print(f"=== semantic brief: {query!r} ===\n")
    print(f"— RESOLVED ({len(matched)}) —")
    for m in matched[:k]:
        pin = "🔒" if m.protected else "  "
        stale = ""
        row = conn.execute("SELECT valid_as_of FROM entity WHERE id=?", (m.entity_id,)).fetchone()
        if row and row[0]:
            try:
                age = (datetime.date.today() - datetime.date.fromisoformat(row[0])).days
                if age > 45:
                    stale = f"  ⚠ verified {age}d ago"
            except ValueError:
                pass
        print(f" {pin} [{m.kind}] {m.name}  (via {m.alias!r}){stale}")
        if m.summary:
            print(f"      {m.summary}")
    extra = [i for i in weights if i not in {m.entity_id for m in matched}]
    if extra:
        marks = ",".join("?" * len(extra))
        rows = conn.execute(
            f"SELECT slug, kind, name, COALESCE(summary,'') FROM entity WHERE id IN ({marks})", extra
        ).fetchall()
        print(f"\n— RELATED ({len(rows)}) —")
        for slug, kind, name, summary in rows[:k]:
            print(f"    [{kind}] {name} — {summary[:110]}")
    ms = mentions(conn, weights)
    if ms:
        print(f"\n— RECORDS ABOUT THESE ({len(ms)}) —")
        by_store: dict[str, list[tuple[str, float]]] = {}
        for (store, ref), w in sorted(ms.items(), key=lambda kv: -kv[1]):
            by_store.setdefault(store, []).append((ref, w))
        for store, items in by_store.items():
            head = ", ".join(f"{r}({w:.2f})" for r, w in items[:8])
            print(f"    {store}: {len(items)} — {head}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")
    p = sub.add_parser("seed"); p.add_argument("--file", default=SEED)
    p = sub.add_parser("resolve"); p.add_argument("query"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("show"); p.add_argument("slug")
    p = sub.add_parser("brief"); p.add_argument("query"); p.add_argument("--k", type=int, default=6)
    p = sub.add_parser("add")
    p.add_argument("slug"); p.add_argument("kind"); p.add_argument("name")
    p.add_argument("--summary"); p.add_argument("--alias", action="append", default=[])
    p.add_argument("--valid-as-of"); p.add_argument("--source")
    p.add_argument("--confidence", type=float, default=0.7); p.add_argument("--protected", action="store_true")
    p = sub.add_parser("alias"); p.add_argument("slug"); p.add_argument("text")
    p = sub.add_parser("link"); p.add_argument("src"); p.add_argument("rel"); p.add_argument("dst"); p.add_argument("--note")
    p = sub.add_parser("mention")
    p.add_argument("slug"); p.add_argument("store"); p.add_argument("ref"); p.add_argument("--weight", type=float, default=1.0)
    sub.add_parser("stats")
    sub.add_parser("prune")
    a = ap.parse_args()

    conn = connect()
    if a.cmd == "init":
        print(f"schema applied at {DB}")
    elif a.cmd == "seed":
        e, al, x = load_seed(conn, a.file)
        print(f"seeded: {e} entities, {al} aliases, {x} edges  ->  {DB}")
    elif a.cmd == "resolve":
        ms = resolve(conn, a.query)
        if a.json:
            print(json.dumps([m._asdict() for m in ms], indent=1))
        else:
            print(f"=== resolve {a.query!r} -> {len(ms)} ===")
            for m in ms:
                print(f" {'🔒' if m.protected else '  '} {m.slug:<32} [{m.kind}] via {m.alias!r}  ({m.score:.1f})")
    elif a.cmd == "show":
        eid = entity_id(conn, a.slug)
        row = conn.execute(
            "SELECT slug,kind,name,COALESCE(summary,''),COALESCE(valid_as_of,'?'),COALESCE(source,'?'),confidence,protected "
            "FROM entity WHERE id=?", (eid,)).fetchone()
        print(f"{row[2]}  [{row[1]}]  slug={row[0]}  {'🔒' if row[7] else ''}")
        print(f"  {row[3]}")
        print(f"  verified {row[4]} · source {row[5]} · confidence {row[6]}")
        als = [r[0] for r in conn.execute("SELECT alias FROM alias WHERE entity_id=? ORDER BY length(alias) DESC", (eid,))]
        print(f"  aliases ({len(als)}): {', '.join(als)}")
        for src, rel, dst, note in conn.execute(
            "SELECT s.name, e.rel, d.name, COALESCE(e.note,'') FROM edge e "
            "JOIN entity s ON s.id=e.src JOIN entity d ON d.id=e.dst WHERE e.src=? OR e.dst=?", (eid, eid)):
            print(f"  edge: {src} --{rel}--> {dst}" + (f"   ({note})" if note else ""))
        for store, ref, w in conn.execute(
            "SELECT store, ref, weight FROM mention WHERE entity_id=? ORDER BY weight DESC LIMIT 20", (eid,)):
            print(f"  mention: {store}:{ref} ({w:.2f})")
    elif a.cmd == "brief":
        cmd_brief(conn, a.query, a.k)
    elif a.cmd == "add":
        eid = upsert_entity(conn, a.slug, a.kind, a.name, summary=a.summary,
                            valid_as_of=a.valid_as_of, source=a.source,
                            confidence=a.confidence, protected=int(a.protected))
        for al in [a.name, a.slug.replace("-", " "), *a.alias]:
            add_alias(conn, eid, al)
        conn.commit(); print(f"entity {a.slug} id={eid}")
    elif a.cmd == "alias":
        add_alias(conn, entity_id(conn, a.slug), a.text); conn.commit(); print(f"alias added to {a.slug}")
    elif a.cmd == "link":
        add_edge(conn, entity_id(conn, a.src), a.rel, entity_id(conn, a.dst), a.note)
        conn.commit(); print(f"{a.src} --{a.rel}--> {a.dst}")
    elif a.cmd == "mention":
        add_mention(conn, entity_id(conn, a.slug), a.store, a.ref, a.weight)
        conn.commit(); print(f"mention {a.store}:{a.ref} -> {a.slug}")
    elif a.cmd == "prune":
        gone = prune(conn)
        total = sum(gone.values())
        print(f"pruned {total} dangling mentions")
        for store, n in gone.items():
            if n:
                print(f"  {store:<10} {n}")
    elif a.cmd == "stats":
        for label, sql in (("entities", "SELECT count(*) FROM entity"),
                           ("aliases", "SELECT count(*) FROM alias"),
                           ("edges", "SELECT count(*) FROM edge"),
                           ("mentions", "SELECT count(*) FROM mention")):
            print(f"  {label:<10} {conn.execute(sql).fetchone()[0]}")
        print("  by kind:")
        for kind, n in conn.execute("SELECT kind, count(*) FROM entity GROUP BY kind ORDER BY 2 DESC"):
            print(f"    {kind:<10} {n}")
        print("  mentions by store:")
        for store, n in conn.execute("SELECT store, count(*) FROM mention GROUP BY store ORDER BY 2 DESC"):
            print(f"    {store:<10} {n}")
    conn.close()


if __name__ == "__main__":
    main()
