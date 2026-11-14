#!/usr/bin/env python3
"""sem_discover — find entities nobody added, and add the confident ones.

The layer's weak point was that a new person, company or system stayed invisible
until someone remembered to add it. Nobody remembers. So this runs hourly and
creates them.

Auto-creating from a weak signal is worse than not creating: a garbage entity
misleads confidently instead of failing to match. So candidates are scored on
features that actually separate proper nouns from prose, and only high scorers
are created — as PROVISIONAL entities with low confidence, which `resolve()`
multiplies through, so they always rank below curated ones and can never
outweigh a real entity.

Two stages, because neither alone is enough:

1. STATISTICS propose. The decisive feature is the capitalisation ratio — how
   often a string is capitalised when it appears at all. A proper noun is
   essentially never written lowercase ("ExampleEntity" ~1.0) while a common noun
   capitalised at line starts is ("change", "page", "account" ~0.1). This
   calibrates itself; no hand-maintained word list can keep up. Mid-sentence
   capitalisation, record spread, multi-word shape and LLC/Inc/@ patterns refine
   it. Heuristics still have limited precision: every fix exposes another noise
   class — common nouns, then dates and timezones, then UI labels.

2. A MODEL disposes. Judging whether a string names a real thing is what models
   are good at. The classifier can reject dates, time zones and interface labels
   while retaining fictional entities such as SampleEntity. It writes a factual
   one-line summary from the supplied context.

Rejections are cached, so with nothing new to judge the model call is skipped
entirely and a refresh costs nothing. Any classifier failure creates nothing.

Compatibility guard: the provisional cap
counts support from the DEDUPLICATED union of legacy `mention` rows and owned
`graph_auto_mention` rows, and retires ONLY entities with no dependencies at
all (no support of either kind, no alias beyond the one this program created,
no relation) that are older than RETIRE_MIN_AGE_DAYS. Anything else is left
pending and further admissions are HELD at the cap with a visible reason —
ownership is never inferred and legacy rows are never retagged. The model
corpus reader applies the same suppression overlay as recall; when the overlay
is unavailable the claude-mem part of the corpus is withheld and reported.
Classifier settings, source roots and the model prompt are unchanged.

Usage: sem_discover.py [--dry-run] [--min-score N] [--max-new N] [--no-classify]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sqlite3
import model_cli
import sys

import datetime
import sqlite3 as _sqlite3

import graph_policy
import memdir
import graph_state
import sem

HM = graph_policy.HIVEMIND_DB
CM = graph_policy.CLAUDEMEM_DB
CANDIDATES = os.environ.get("SEM_CANDIDATES_PATH") or os.path.join(memdir.tools_dir(), "sem-candidates.md")

AUTO_SOURCE = "auto-detected"
AUTO_CONFIDENCE = 0.35          # keeps provisional entities below curated ones
DEFAULT_MIN_SCORE = 6.0
DEFAULT_MAX_NEW = 12            # per run, so a bad day cannot flood the layer
MAX_PROVISIONAL = 60            # total cap; only dependency-free entities are retired to make room
RETIRE_MIN_AGE_DAYS = 7         # a zero-support entity younger than this is still waiting for its links

CAND_RE = re.compile(r"([A-Z][A-Za-z0-9]{2,}(?:[ ][A-Z][A-Za-z0-9]{2,}){0,2})")

# Words that are capitalised constantly in this corpus but name nothing.
NOISE = {
    "done", "users", "demo", "verified", "gates", "live", "artifact", "platform",
    "desktop", "meeting", "review", "metrics", "head", "note", "notes", "fixed",
    "added", "used", "new", "all", "not", "but", "and", "for", "use", "set", "run",
    "see", "now", "was", "has", "its", "via", "the", "this", "that", "when", "with",
    "from", "also", "next", "todo", "done", "pass", "fail", "error", "warning",
    "true", "false", "null", "none", "test", "tests", "build", "deploy", "status",
    "summary", "context", "result", "results", "output", "input", "file", "files",
    "code", "data", "user", "session", "sessions", "agent", "agents", "task",
    "tasks", "phase", "step", "steps", "plan", "goal", "issue", "issues", "commit",
    "branch", "merge", "main", "prod", "production", "staging", "local", "remote",
    "read", "write", "create", "update", "delete", "check", "verify", "confirm",
    "start", "stop", "open", "close", "final", "draft", "ready", "blocked", "open",
    "high", "medium", "low", "critical", "important", "before", "after", "during",
    "python", "javascript", "typescript", "node", "npm", "git", "github", "sql",
    "json", "yaml", "html", "css", "api", "cli", "http", "https", "url", "uuid",
    "readme", "changelog", "license", "makefile", "dockerfile",
}


def open_ro(path: str) -> sqlite3.Connection | None:
    """Ordinary read-only source connection; no writable fallback, bytes out."""
    try:
        return graph_policy.open_ro(path)
    except sqlite3.Error:
        return None


CORPUS_NOTES: list[str] = []


def _dec(b) -> str | None:
    """Strict decode; None for undecodable bytes (the row is skipped and counted)."""
    try:
        return graph_policy.utf8_strict(b)
    except UnicodeDecodeError:
        return None


def corpus() -> list[str]:
    """Model-phase input: active hivemind rules + the 4,000 most recent
    observations MINUS the suppression overlay. Overlay unavailable => the
    claude-mem part is withheld (not read) and reported."""
    out: list[str] = []
    CORPUS_NOTES.clear()
    skipped = 0
    hm = open_ro(HM)
    if hm:
        for rule, ctx in hm.execute("SELECT rule, context FROM learnings WHERE active=1"):
            a, b = _dec(rule), _dec(ctx)
            if a is None or b is None:
                skipped += 1
                continue
            out.append(f"{a} {b}")
        hm.close()
    else:
        CORPUS_NOTES.append("hivemind unavailable")
    try:
        sconn = sem.connect_ro()
        try:
            suppressed = graph_policy.load_policy(sconn).suppressed
        finally:
            sconn.close()
    except (sqlite3.Error, graph_policy.PolicyUnavailable) as exc:
        CORPUS_NOTES.append(f"claude-mem withheld: suppression overlay unavailable ({getattr(exc, 'reason', type(exc).__name__)})")
        suppressed = None
    cm = open_ro(CM) if suppressed is not None else None
    if cm:
        for oid, title, text, facts in cm.execute(
                "SELECT id, title, text, facts FROM observations ORDER BY created_at_epoch DESC LIMIT 4000"):
            if oid in suppressed:
                continue
            t, x, f = _dec(title), _dec(text), _dec(facts)
            if t is None or x is None or f is None:
                skipped += 1
                continue
            out.append(f"{t}. {x} {f}")
        cm.close()
    elif suppressed is not None:
        CORPUS_NOTES.append("claude-mem unavailable")
    if skipped:
        CORPUS_NOTES.append(f"{skipped} undecodable row(s) skipped")
    return out


class Cand:
    __slots__ = ("phrase", "total", "midcap", "records", "shape", "cooccur", "anycase", "example")

    def __init__(self, phrase: str) -> None:
        self.phrase = phrase
        self.total = 0          # times seen Capitalised
        self.midcap = 0
        self.records: set[int] = set()
        self.shape = False
        self.cooccur = 0
        self.anycase = 0        # times seen in ANY case (filled in pass 2)
        self.example = ""      # a representative sentence, for the classifier

    @property
    def cap_ratio(self) -> float:
        """How often this string is capitalised when it appears at all.

        THE discriminating feature. A proper noun is essentially never written
        lowercase — "ExampleEntity" and "SampleEntity" sit at ~1.0. An ordinary noun that
        merely gets capitalised at line starts — "change", "page", "account",
        "search" — sits far below, because prose is full of the lowercase form.
        No hand-maintained word list can keep up; this calibrates itself.
        """
        return self.total / max(self.total, self.anycase)

    def score(self) -> float:
        if not self.total:
            return 0.0
        cap = self.cap_ratio
        midcap_ratio = self.midcap / self.total
        s = 0.0
        s += 6.0 * cap                                # dominant signal
        s += 2.0 * midcap_ratio
        s += min(2.5, len(self.records) / 8.0)        # seen across many records
        s += 1.5 * (len(self.phrase.split()) - 1)     # multi-word => proper noun
        s += 2.0 if self.shape else 0.0               # LLC/Inc/.com/@ etc.
        s += min(1.0, self.cooccur / 10.0)            # keeps company with real entities
        if len(self.records) < 4:                     # one-off bursts are not entities
            s *= 0.35
        if midcap_ratio < 0.30:                       # basically always line-initial
            s *= 0.25
        if cap < 0.80:                                # written lowercase too often to be a name
            s *= 0.15
        # a single ALL-CAPS token is shouty formatting, not a name
        if len(self.phrase.split()) == 1 and self.phrase.isupper() and not self.shape:
            s *= 0.2
        return round(s, 2)


SHAPE_RE = re.compile(r"\b(LLC|L\.L\.C|Inc|Corp|Ltd|GmbH)\b|\.(com|ai|io|net|org)\b|@")


_CORPUS_CACHE: list[str] = []


def discover(conn: sqlite3.Connection) -> list[Cand]:
    global _CORPUS_CACHE
    known_aliases = {a.lower() for (a,) in conn.execute("SELECT alias FROM alias")}
    known_aliases.update(graph_policy.load_alias_map(conn).conflicts)
    known_words = set()
    for a in known_aliases:
        known_words.update(a.split())

    _CORPUS_CACHE = corpus()
    cands: dict[str, Cand] = {}
    for idx, text in enumerate(_CORPUS_CACHE):
        if not text:
            continue
        has_known = any(w in known_words for w in re.findall(r"[a-z]{4,}", text.lower()))
        for m in CAND_RE.finditer(text):
            phrase = m.group(1).strip()
            low = phrase.lower()
            if low in known_aliases or low in NOISE:
                continue
            if all(w.lower() in NOISE for w in phrase.split()):
                continue
            c = cands.get(low)
            if c is None:
                c = cands[low] = Cand(phrase)
            c.total += 1
            c.records.add(idx)
            # capitalised mid-sentence? look back past whitespace for a boundary
            j = m.start() - 1
            while j >= 0 and text[j] in " \t":
                j -= 1
            if j >= 0 and text[j] not in ".!?:\n•-*|/([":
                c.midcap += 1
            if not c.example:
                lo = max(0, m.start() - 110)
                c.example = text[lo:m.end() + 150].strip()
            if SHAPE_RE.search(text[max(0, m.start() - 40):m.end() + 40]):
                c.shape = True
            if has_known:
                c.cooccur += 1

    # Pass 2: how often does each candidate appear in ANY case? One regex over
    # the corpus rather than 12k substring scans. Only the plausible head of the
    # list is worth measuring.
    head = sorted(cands.values(), key=lambda c: -(len(c.records) + c.total))[:900]
    if head:
        by_low = {c.phrase.lower(): c for c in head}
        alt = re.compile(
            r"\b(" + "|".join(re.escape(p) for p in sorted(by_low, key=len, reverse=True)) + r")\b",
            re.IGNORECASE)
        for text in _CORPUS_CACHE:
            for found in alt.findall(text):
                c = by_low.get(found.lower())
                if c is not None:
                    c.anycase += 1
    return sorted(head, key=lambda c: -c.score())


def slugify(phrase: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", phrase.lower()).strip("-")[:48]


def guess_kind(phrase: str) -> str:
    if SHAPE_RE.search(phrase) or re.search(r"\b(LLC|Inc|Corp|Ltd)\b", phrase):
        return "org"
    if len(phrase.split()) == 2 and all(w[:1].isupper() for w in phrase.split()):
        return "person"          # "Firstname Lastname" shape
    return "concept"


PROMPT = """You are curating the entity index of a personal memory system for a \
software professional working across their projects and ventures. (EDIT this \
description to match your world — e.g. "a founder running a logistics company \
(Example Co) with an example project".)
Below are strings extracted statistically from their notes. Decide for each whether it \
names a REAL, SPECIFIC entity worth indexing — a person, company, product, system, \
customer account, or document.

Reject: generic prose, status words, UI labels, dates, timezones, units, file formats, \
generic technical terms, and anything too vague to be a specific thing.

Be strict. When unsure, keep=false. A wrong entity is worse than a missing one, because \
it misleads confidently.

For each kept entity write a one-line summary using ONLY facts visible in the context \
given. Do not invent. If the context does not support a factual summary, keep=false.

Return ONLY a JSON array, no prose, no markdown fence:
[{"phrase":"<exact input string>","keep":true,"kind":"person|org|venture|system|account|artifact|concept","summary":"<one line>"},
 {"phrase":"<exact input string>","keep":false}]

CANDIDATES:
%s
"""


def classify(cands: list[Cand], timeout: int = 180) -> dict[str, dict]:
    """Ask a model which candidates are real. Returns {phrase_lower: decision}.

    Heuristics get recall cheaply but have limited precision — every
    fix exposes another noise class (common nouns, then dates, then UI labels).
    Judging whether a string names a real thing is exactly what a model is good
    at, so the statistics propose and the model disposes.

    Any failure (no CLI, timeout, unparseable output) returns {} and the caller
    creates nothing. Degrading to "added no entities" is always safe.
    """
    if not cands or not memdir.cloud_ok():
        return {}
    lines = []
    for i, c in enumerate(cands, 1):
        ctx = (c.example or "")[:260].replace("\n", " ")
        lines.append(f'{i}. "{c.phrase}" (seen in {len(c.records)} notes) — context: {ctx}')
    raw = model_cli.run_model(PROMPT % "\n".join(lines), timeout)
    if raw is None:
        sys.stderr.write("[sem_discover] classifier unavailable; creating nothing\n")
        return {}
    raw = raw.strip()
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        sys.stderr.write("[sem_discover] classifier returned no JSON; creating nothing\n")
        return {}
    try:
        decisions = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"[sem_discover] classifier JSON invalid ({exc}); creating nothing\n")
        return {}
    out = {}
    for d in decisions:
        if isinstance(d, dict) and isinstance(d.get("phrase"), str):
            out[d["phrase"].strip().lower()] = d
    return out


# --------------------------------------------------------------------------- #
# provisional cap: deduplicated support, explicit dependency enumeration
# --------------------------------------------------------------------------- #
def provisional_dependencies(conn: sqlite3.Connection) -> list[dict]:
    """Every provisional entity with its dependencies enumerated. Support is the
    deduplicated union of legacy and owned (store, ref) links; ownership of
    legacy rows is NOT inferred."""
    owned = graph_state.owned_support(conn)
    out = []
    for eid, slug, name, created in conn.execute(
            "SELECT id, slug, name, created_at FROM entity WHERE source=? ORDER BY id", (AUTO_SOURCE,)):
        legacy = {(s, r) for s, r in conn.execute("SELECT store, ref FROM mention WHERE entity_id=?", (eid,))}
        own = owned.get(eid, set())
        aliases = {a for (a,) in conn.execute("SELECT alias FROM alias WHERE entity_id=?", (eid,))}
        edges = conn.execute("SELECT count(*) FROM edge WHERE src=? OR dst=?", (eid, eid)).fetchone()[0]
        out.append({"id": eid, "slug": slug, "name": name, "created_at": created or "",
                    "legacy": len(legacy), "owned": len(own), "support": len(legacy | own),
                    "extra_aliases": sorted(aliases - {name.strip().lower()}), "edges": edges})
    return out


def _age_days(created: str) -> float:
    try:
        return (datetime.datetime.now() - datetime.datetime.fromisoformat(created[:19])).total_seconds() / 86400
    except ValueError:
        return 0.0


def safe_to_retire(dep: dict) -> bool:
    """Only an entity with NO dependencies of any kind may be retired automatically."""
    return (dep["support"] == 0 and not dep["extra_aliases"] and dep["edges"] == 0
            and _age_days(dep["created_at"]) >= RETIRE_MIN_AGE_DAYS)


def pending_reason(dep: dict) -> str:
    parts = []
    if dep["legacy"]:
        parts.append(f"legacy/unclassified links {dep['legacy']}")
    if dep["owned"]:
        parts.append(f"owned links {dep['owned']}")
    if dep["extra_aliases"]:
        parts.append(f"manual aliases {len(dep['extra_aliases'])}")
    if dep["edges"]:
        parts.append(f"relations {dep['edges']}")
    if not parts:
        parts.append(f"younger than {RETIRE_MIN_AGE_DAYS}d")
    return ", ".join(parts)


def retire(conn: sqlite3.Connection, deps: list[dict]) -> list[str]:
    """Delete dependency-free provisional entities. graph_auto_mention has ON
    DELETE RESTRICT, so a race that adds owned support makes the delete fail
    visibly instead of cascading."""
    gone = []
    for d in deps:
        try:
            conn.execute("DELETE FROM entity WHERE id=?", (d["id"],))
            gone.append(d["slug"])
        except _sqlite3.IntegrityError:
            sys.stderr.write(f"[sem_discover] retirement of {d['slug']} refused: owned support appeared\n")
    return gone


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE)
    ap.add_argument("--max-new", type=int, default=DEFAULT_MAX_NEW)
    ap.add_argument("--no-classify", action="store_true", help="skip the model pass (creates nothing)")
    a = ap.parse_args()

    conn = sem.connect()
    ranked = discover(conn)
    accepted = [c for c in ranked if c.score() >= a.min_score][:a.max_new]
    shortlist = [c for c in ranked if 3.0 <= c.score() < a.min_score][:40]

    # statistics propose, the model disposes
    rejected_before = {r[0] for r in conn.execute("SELECT phrase FROM rejected_candidate")}
    fresh = [c for c in accepted
             if c.phrase.strip().lower() not in rejected_before
             and not conn.execute("SELECT 1 FROM entity WHERE slug=?",
                                  (slugify(c.phrase),)).fetchone()]
    if not fresh:
        print("no new candidates — skipping the classifier")
        for note in CORPUS_NOTES:
            print(f"corpus: {note}")
        conn.close()
        return
    # Admission at the cap: room = cap - current provisional count; only
    # dependency-free entities can be retired to make more room. Candidates
    # beyond that are HELD (not classified, not cached as rejected).
    deps = provisional_dependencies(conn)
    safe = sorted([d for d in deps if safe_to_retire(d)], key=lambda d: d["created_at"])
    unsafe = [d for d in deps if not safe_to_retire(d)]
    room = MAX_PROVISIONAL - len(deps)
    admissible = max(0, room + len(safe))
    held = fresh[admissible:]
    fresh = fresh[:admissible]
    skip_model = a.no_classify or a.dry_run or not memdir.cloud_ok()
    if skip_model:
        print(f"classifier skipped: {len(fresh)} candidates")
    decisions = {} if (skip_model or not fresh) else classify(fresh)
    created, rejected = [], []
    for c in fresh:
        slug = slugify(c.phrase)
        d = decisions.get(c.phrase.strip().lower())
        if not slug or not d or not d.get("keep"):
            rejected.append(c)
            if d and not a.dry_run:      # only cache an actual judgement
                conn.execute(
                    "INSERT OR IGNORE INTO rejected_candidate (phrase, reason, rejected_at) "
                    "VALUES (?,?,?)", (c.phrase.strip().lower(), "classifier: not an entity", sem.now()))
            continue
        kind = d.get("kind") if d.get("kind") in sem.KINDS else guess_kind(c.phrase)
        summary = (d.get("summary") or "").strip()
        if not summary:
            rejected.append(c)
            if not a.dry_run:
                conn.execute(
                    "INSERT OR IGNORE INTO rejected_candidate (phrase, reason, rejected_at) "
                    "VALUES (?,?,?)", (c.phrase.strip().lower(), "classifier: no factual summary", sem.now()))
            continue
        if not a.dry_run:
            eid = sem.upsert_entity(
                conn, slug, kind, c.phrase, summary=summary,
                valid_as_of=sem.now()[:10], source=AUTO_SOURCE, confidence=AUTO_CONFIDENCE,
            )
            sem.add_alias(conn, eid, c.phrase)
        created.append((c, slug, kind, summary))

    # Cap provisional entities so auto-detection can never dominate the layer —
    # by retiring ONLY dependency-free entities, as many as the kept creations
    # need. Entities with any legacy/owned/manual dependency stay (pending).
    retired = 0
    retired_slugs: list[str] = []
    if not a.dry_run:
        need = max(0, len(deps) + len(created) - MAX_PROVISIONAL)   # creations above happened in this transaction
        if need:
            retired_slugs = retire(conn, safe[:need])
            retired = len(retired_slugs)
        conn.commit()

    print(f"scanned {len(ranked)} candidates")
    print(f"{'would create' if a.dry_run else 'created'} {len(created)} provisional entities:")
    for c, slug, kind, summary in created:
        print(f"  [{c.score():>5}] {c.phrase:<32} [{kind}] {summary[:90]}")
    if rejected:
        print(f"classifier rejected {len(rejected)}: "
              f"{', '.join(c.phrase for c in rejected[:12])}")
    if retired:
        print(f"retired {retired} dependency-free provisional entities over the cap of {MAX_PROVISIONAL}: "
              f"{', '.join(retired_slugs)}")
    if held:
        print(f"HELD {len(held)} candidate(s) at the provisional cap ({len(deps)}/{MAX_PROVISIONAL}, "
              f"{len(unsafe)} not safely retirable): {', '.join(c.phrase for c in held[:12])}")
        for d in unsafe[:12]:
            print(f"  pending retirement {d['slug']}: {pending_reason(d)}")
    for note in CORPUS_NOTES:
        print(f"corpus: {note}")

    if not a.dry_run:
        with open(CANDIDATES, "w", encoding="utf-8") as fh:
            fh.write("# Entity candidates — below the auto-create threshold\n\n")
            fh.write("Auto-generated. Real ones are worth promoting with a proper summary:\n")
            fh.write("`sem.py add <slug> <kind> \"Name\" --summary \"...\"`\n\n")
            fh.write("| score | phrase | records |\n|---|---|---|\n")
            for c in shortlist:
                fh.write(f"| {c.score()} | {c.phrase} | {len(c.records)} |\n")
        print(f"shortlist ({len(shortlist)}) -> {CANDIDATES}")
    conn.close()


if __name__ == "__main__":
    main()
