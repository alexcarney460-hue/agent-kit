#!/usr/bin/env python3
"""sem_artifacts — index files on disk as artifact mentions.

The gap this closes: recall could return prose ABOUT a document but never the
document itself, so drafts and receipts had to be found by hand.

A filename is strong evidence — a file called
Acme_Contractor_Agreement_Jamie_Alexandra_Rivera_2026-07-29_DRAFT.docx is
unambiguously about that entity — so filename matches outrank content matches.

SECURITY: content under ~/keys is NEVER read. That tree holds live credentials;
only its paths are indexed, which is the same thing memory already records
("~/keys/<name>"). File contents are used for matching only and are never stored:
the mention table holds a path, a weight, and nothing else.

Usage: sem_artifacts.py [--dry-run] [--reset]
"""
from __future__ import annotations

import argparse
import os
import re
import sys

import sem
import sem_backfill

# (root, read_content, max_depth, extensions or None for the default set)
# Source code is deliberately excluded from ~/workspace: recall should return the
# note, the contract or the receipt — not 6k .py files that happen to say "acme".
ROOTS: list[tuple[str, bool, int, set[str] | None]] = [
    ("~/Desktop", True, 2, None),
    ("~/keys", False, 3, None),                # credentials — paths only, never content
    ("~/.claude/memory-tools", True, 2, None),
    ("~/.claude/rules", True, 3, None),
    ("~/workspace", True, 2, {".md", ".txt"}),  # notes and plans only
]

TEXT_EXT = {".md", ".txt", ".json", ".csv", ".yaml", ".yml", ".sql", ".py", ".sh"}
# indexed by NAME only — cannot cheaply read, but the filename is the signal
NAME_ONLY_EXT = {".docx", ".pdf", ".xlsx", ".pptx", ".zip", ".png", ".jpg", ".jpeg", ".heic"}
SKIP_DIRS = {"node_modules", ".git", "dist", "build", "__pycache__", ".next", ".venv",
             "venv", "worktrees", ".claude-mem", "backups", "coverage", ".turbo"}
SKIP_FILES = re.compile(r"\.(db|db-wal|db-shm|lock|log|pyc|map|bak-\d+)$|^\.DS_Store$")
MAX_BYTES = 60_000


def walk(root: str, max_depth: int, exts: set[str] | None = None):
    root = os.path.expanduser(root)
    if not os.path.isdir(root):
        return
    base_depth = root.rstrip("/").count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".claude")]
        if dirpath.rstrip("/").count(os.sep) - base_depth >= max_depth:
            dirnames[:] = []
        for fn in filenames:
            if SKIP_FILES.search(fn):
                continue
            ext = os.path.splitext(fn)[1].lower()
            allowed = exts if exts is not None else (TEXT_EXT | NAME_ONLY_EXT)
            if ext not in allowed:
                continue
            yield os.path.join(dirpath, fn), ext


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()

    conn = sem.connect()
    pattern, amap, ambiguous = sem_backfill.build_matcher(conn)
    discriminable = {eid for al, eid in amap.items() if al not in ambiguous}

    if a.reset and not a.dry_run:
        conn.execute("DELETE FROM mention WHERE store='artifact'")
        conn.commit()
        print("cleared prior artifact mentions")

    seen = scanned = written = 0
    by_root: dict[str, int] = {}
    for root, read_content, depth, exts in ROOTS:
        n_root = 0
        for path, ext in walk(root, depth, exts):
            scanned += 1
            # the path itself, with separators and underscores opened up
            name_blob = re.sub(r"[/_\-.]+", " ", path)
            hits = sem_backfill.hits_for(name_blob, pattern, amap, ambiguous, discriminable)
            # filename evidence is definitive; give it full weight
            hits = {eid: min(1.0, w + 0.25) for eid, w in hits.items()}

            if read_content and ext in TEXT_EXT:
                try:
                    with open(path, encoding="utf-8", errors="ignore") as fh:
                        body = fh.read(MAX_BYTES)
                except OSError:
                    body = ""
                for eid, w in sem_backfill.hits_for(
                        body, pattern, amap, ambiguous, discriminable).items():
                    hits.setdefault(eid, round(w * 0.7, 3))  # content is weaker than filename

            if not hits:
                continue
            seen += 1
            n_root += 1
            for eid, w in hits.items():
                written += 1
                if not a.dry_run:
                    sem.add_mention(conn, eid, "artifact", path, w)
        by_root[root] = n_root
        print(f"  {root:<28} {n_root} matched files")

    if not a.dry_run:
        conn.commit()
    print(f"\nscanned {scanned} files, {seen} matched, "
          f"{'would write' if a.dry_run else 'wrote'} {written} artifact mentions")

    print("\ntop artifact-linked entities:")
    for slug, c in conn.execute(
            "SELECT e.slug, count(*) c FROM mention m JOIN entity e ON e.id=m.entity_id "
            "WHERE m.store='artifact' GROUP BY e.slug ORDER BY c DESC LIMIT 10"):
        print(f"  {slug:<34} {c}")

    # prove the gap is closed (EDIT the slug to an artifact entity in your world)
    print("\nfiles now reachable for 'the contractor agreement':")
    for path, w in conn.execute(
            "SELECT m.ref, m.weight FROM mention m JOIN entity e ON e.id=m.entity_id "
            "WHERE m.store='artifact' AND e.slug='acme-contractor-agreement' "
            "ORDER BY m.weight DESC LIMIT 8"):
        print(f"  [{w:.2f}] {path.replace(os.path.expanduser('~'), '~')}")
    conn.close()


if __name__ == "__main__":
    main()
