#!/usr/bin/env python3
"""sem_artifacts — index files on disk as artifact mentions.

The gap this closes: recall could return prose ABOUT a document but never the
document itself, so drafts and receipts had to be found by hand.

A filename can be strong evidence — example-service-agreement.docx can match
an example entity — so filename matches outrank content matches.

SECURITY: credential roots are excluded entirely. File contents are used for
matching only and are never stored:
the mention table holds a path, a weight, and nothing else.

Usage: sem_artifacts.py [--dry-run] [--reset]
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import re
import sys
import stat
from pathlib import Path

import sem
import sem_backfill

# (root, read_content, max_depth, extensions or None for the default set)
# Source code is deliberately excluded from ~/workspace: recall should return the
# note or document — not thousands of source files that happen to mention it.
ROOTS: list[tuple[str, bool, int, set[str] | None]] = [
    ("~/Desktop", True, 2, None),
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
PRIVATE_DIRS = {"keys", "key", "credentials", "creds", "secrets", "secret", "tokens",
                "passwords", "private", "vault", ".ssh", ".aws", ".gnupg", ".gpg",
                ".azure", ".kube", ".docker", ".password-store", "keychains"}
PRIVATE_FILES = (".env*", "*.pem", "*.key", "*.pfx", "*.p12", "*.kdbx", "*.keystore",
                 "*.jks", "id_rsa*", "id_ed25519*", "id_ecdsa*", "*credential*",
                 "*secret*", "*token*", "*password*", "*passwd*", ".netrc", ".npmrc",
                 ".pypirc", "auth.json")
PRIVATE_SUBSTRINGS = ("key", "credential", "cred", "secret", "token", "password", "passwd",
                      "passphrase", "private", "vault", "wallet", "cert", "ssh", "gpg", "pgp",
                      "auth", "login", "bank", "tax", "medical", "passport", "license",
                      "licence", "ssn", ".env", "recovery", "seed", "service-account", "serviceaccount",
                      "backup-code", "2fa", "mfa", "otp", "pin")


def private_file(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name.lower(), pattern) for pattern in PRIVATE_FILES)


def private_components(path: Path, root: Path, is_dir: bool) -> bool:
    parts = path.relative_to(root).parts
    # Deliberately over-exclude ordinary names such as keyboard and author.
    if any(term in part.lower() for part in parts for term in PRIVATE_SUBSTRINGS):
        return True
    directories = (root.name,) + (parts if is_dir else parts[:-1])
    if any(part.lower() in PRIVATE_DIRS for part in directories):
        return True
    if any(part.startswith('.') for part in directories[1:]):
        return True
    all_dirs = path.parts if is_dir else path.parent.parts
    if any(a.lower() == '.config' and b.lower() == 'gcloud'
           for a, b in zip(all_dirs, all_dirs[1:])):
        return True
    home = Path.home().resolve()
    if path == home or home in path.parents:
        home_parts = path.relative_to(home).parts
        home_dirs = home_parts if is_dir else home_parts[:-1]
        if any(part.lower() in PRIVATE_DIRS for part in home_dirs):
            return True
    return not is_dir and private_file(path.name)


def walk(root: str, max_depth: int, exts: set[str] | None = None):
    root = os.path.expanduser(root)
    if os.path.islink(root):
        return
    root = os.path.realpath(root)
    if not safe_path(root, root) or not os.path.isdir(root):
        return
    base_depth = root.rstrip("/").count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".claude")
                       and safe_path(os.path.join(dirpath, d), root)]
        if dirpath.rstrip("/").count(os.sep) - base_depth >= max_depth:
            dirnames[:] = []
        for fn in filenames:
            if SKIP_FILES.search(fn) or private_file(fn):
                continue
            ext = os.path.splitext(fn)[1].lower()
            allowed = exts if exts is not None else (TEXT_EXT | NAME_ONLY_EXT)
            if ext not in allowed:
                continue
            path = os.path.join(dirpath, fn)
            if safe_path(path, root):
                yield path, ext


def safe_path(path: str, root: str) -> bool:
    """Reject links/reparse points and credential paths, including root components."""
    home = Path.home().resolve()
    credentials = [home / 'keys', home / '.ssh', home / '.aws',
                   home / '.config' / 'gcloud']
    try:
        candidate = Path(path).absolute()
        resolved = candidate.resolve(strict=True)
        resolved_root = Path(root).resolve(strict=True)
        if resolved != resolved_root and resolved_root not in resolved.parents:
            return False
        if (private_components(candidate, Path(root).absolute(), resolved.is_dir()) or
                private_components(resolved, resolved_root, resolved.is_dir())):
            return False
        for parent in (candidate, *candidate.parents):
            if parent == resolved_root.parent:
                break
            info = parent.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                return False
        for forbidden in credentials:
            target = forbidden.resolve()
            if resolved == target or target in resolved.parents:
                return False
        for base, prefix in ((home / '.codex', 'auth'), (home / '.claude', '.credentials')):
            base = base.resolve()
            if base in resolved.parents:
                first = resolved.relative_to(base).parts[0]
                if first.lower().startswith(prefix):
                    return False
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def read_safe(path: str, root: str) -> str:
    """Read only the checked regular file; reject a changed file or path."""
    if not safe_path(path, root):
        return ""
    parents = []
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink > 1 or getattr(before, 'st_file_attributes', 0) & 0x400:
            return ""
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        if os.open in os.supports_dir_fd and hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'):
            relative = os.path.relpath(path, root)
            parts = Path(relative).parts
            if not parts or '..' in parts:
                return ""
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            parent = os.open(root, directory_flags)
            parents.append(parent)
            for part in parts[:-1]:
                parent = os.open(part, directory_flags, dir_fd=parent)
                parents.append(parent)
            fd = os.open(parts[-1], flags, dir_fd=parent)
        else:
            fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            after = os.lstat(path)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink > 1 or after.st_nlink > 1 or
                    not os.path.samestat(before, opened) or
                    not os.path.samestat(after, opened) or not safe_path(path, root)):
                return ""
            with os.fdopen(fd, 'r', encoding='utf-8', errors='ignore') as fh:
                fd = -1
                return fh.read(MAX_BYTES)
        finally:
            if fd >= 0:
                os.close(fd)
    except OSError:
        return ""
    finally:
        for parent in reversed(parents):
            os.close(parent)


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
                body = read_safe(path, os.path.expanduser(root))
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
    print("\nfiles now reachable for 'example service agreement':")
    for path, w in conn.execute(
            "SELECT m.ref, m.weight FROM mention m JOIN entity e ON e.id=m.entity_id "
            "WHERE m.store='artifact' AND e.slug='example-service-agreement' "
            "ORDER BY m.weight DESC LIMIT 8"):
        print(f"  [{w:.2f}] {path.replace(os.path.expanduser('~'), '~')}")
    conn.close()


if __name__ == "__main__":
    main()
