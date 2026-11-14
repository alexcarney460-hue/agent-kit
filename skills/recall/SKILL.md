---
name: recall
description: Search durable memory and past Claude Code and Codex sessions.
metadata:
  author: arjunkmrm
  version: "0.3.0"
  license: MIT
---

# Recall

Use the Python that installed the kit (`python`, `py -3` or `python3`); in the examples below, substitute that interpreter for `python` if necessary. These commands use `$HOME` in both bash and PowerShell.

The shared file-memory directory is printed by:

```bash
python -c "import sys, os; sys.path.insert(0, os.path.join(os.path.expanduser(chr(126)), '.claude', 'memory-tools')); import memdir; print(memdir.memory_dir())"
```

Search the installed unified memory tools:

```bash
python "$HOME/.claude/memory-tools/mem_recall.py" "your query"
```

To search past sessions, locate the directory containing this skill's `SKILL.md` as supplied by the active agent, then substitute its actual path for `SKILL_DIRECTORY`:

```bash
python "SKILL_DIRECTORY/scripts/recall.py" "your query"
```

The session index is `session-recall.db` inside the shared file-memory directory. It searches local Claude Code and Codex transcripts; it does not upload them. Options include `--project PATH`, `--days N`, `--source claude|codex`, `--limit N` and `--reindex`. Supports FTS5 phrases, AND, OR, NOT, and prefix searches. To inspect a result, run `python "SKILL_DIRECTORY/scripts/read_session.py" "FILE_PATH"` with the result's path. Creating a new index does not migrate or delete legacy indexes; it rebuilds from local transcripts.
