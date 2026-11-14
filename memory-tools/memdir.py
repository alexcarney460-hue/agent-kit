#!/usr/bin/env python3
"""memdir: where this machine's memory lives, and whether memory may leave it.

Every memory tool gets its paths and its cloud permission from here, so there is
one place to override them (tests and the installer use the overrides).

Paths
  memory_dir()   Claude Code's auto-memory folder:
                 ~/.claude/projects/<encoded-home>/memory, where <encoded-home> is
                 the home path with every non-ASCII letter or digit replaced by "-"
                 (/Users/jamie -> -Users-jamie, C:\\Users\\Jamie -> C--Users-Jamie).
                 If the new project folder is absent, use an existing folder from
                 the earlier slash-and-colon encoding. Override with CLAUDE_MEMORY_DIR.
  hivemind_db()  the hivemind database, ~/hivemind.db. Override with HIVEMIND_DB.
  tools_dir()    the folder these tools are installed in.

Cloud permission
  cloud_ok()     True only when the owner has opted in with AGENT_KIT_CLOUD=1
                 Any code path that
                 would hand memory text to a model CLI or a network service must call
                 this first and do nothing when it is False. A --dry-run never sends
                 anything, opted in or not.
"""
import os
import re


def home() -> str:
    return os.path.expanduser("~")


def memory_dir() -> str:
    override = os.environ.get("CLAUDE_MEMORY_DIR")
    if override:
        return os.path.expanduser(override)
    h = home()
    encoded = re.sub(r"[^A-Za-z0-9]", "-", h)
    legacy = h.replace("\\", "-").replace("/", "-").replace(":", "-")
    projects = os.path.join(h, ".claude", "projects")
    modern = os.path.join(projects, encoded)
    old = os.path.join(projects, legacy)
    new_memory, old_memory = os.path.join(modern, "memory"), os.path.join(old, "memory")
    # Use the old folder while it is the one that holds memories: Claude Code creates the new
    # project folder by itself on its first session, which must not hide notes already written.
    if modern != old and _has_files(old_memory) and not _has_files(new_memory):
        return old_memory
    return new_memory


def _has_files(folder: str) -> bool:
    try:
        return any(os.scandir(folder))
    except OSError:
        return False


def hivemind_db() -> str:
    override = os.environ.get("HIVEMIND_DB")
    if override:
        return os.path.expanduser(override)
    return os.path.join(home(), "hivemind.db")


def tools_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def semantic_db() -> str:
    return os.path.expanduser(os.environ.get("SEM_DB_PATH") or os.path.join(tools_dir(), "semantic.db"))


def cloud_ok() -> bool:
    return os.environ.get("AGENT_KIT_CLOUD") == "1"
