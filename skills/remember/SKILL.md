---
name: remember
description: Store a durable memory or lesson using the installed memory tools.
---

# Remember

Use the Python that installed the kit (`python`, `py -3` or `python3`); substitute it for `python` in these commands if needed. `$HOME` works in bash and PowerShell.

Store the requested content with the safe writer:

```bash
python "$HOME/.claude/memory-tools/hm_write.py" context "one-line fact" --context "details and why" --confidence 0.8
```

Use `correction` instead of `context` for a lesson from a mistake. For a durable fact worth loading each session, print the shared file-memory directory:

```bash
python -c "import sys, os; sys.path.insert(0, os.path.join(os.path.expanduser(chr(126)), '.claude', 'memory-tools')); import memdir; print(memdir.memory_dir())"
```

Write a one-fact file there with `name`, `description`, and `metadata.type` front matter; add a pointer to `MEMORY.md` in the same directory. Update existing files rather than duplicating. The database used by the safe writer is separate from this file-memory directory; the installed tools resolve both. Never store secrets. Report what was stored and where.
