#!/usr/bin/env python3
"""memdir — locate the Claude Code auto-memory directory on this machine.

Claude Code keeps per-project auto-memory under
~/.claude/projects/<encoded-home>/memory, where <encoded-home> is the home
directory path with every "/" replaced by "-" (e.g. /Users/jamie ->
-Users-jamie). Set CLAUDE_MEMORY_DIR to override the derived location.
"""
import os


def memory_dir() -> str:
    override = os.environ.get("CLAUDE_MEMORY_DIR")
    if override:
        return os.path.expanduser(override)
    home = os.path.expanduser("~")
    encoded = home.replace("\\", "-").replace("/", "-").replace(":", "-")
    return os.path.join(home, ".claude", "projects", encoded, "memory")
