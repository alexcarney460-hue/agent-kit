---
name: remember
description: Store a durable memory/lesson via the unified memory stack (hivemind SAFE WRITER + file memories)
---

# Remember

> The memory stack is hivemind (`~/hivemind.db`) + the semantic entity layer, plus claude-mem when Claude Code has that plugin (see the global AGENTS.md / CLAUDE.md).

Store the content from $ARGUMENTS durably:

1. **Hivemind (primary, cross-device)** — always:
   ```bash
   python3 ~/.claude/memory-tools/hm_write.py context "<one-line fact>" --context "<details/why>" --confidence 0.8
   ```
   Use `correction` instead of `context` when it's a lesson from something that
   went wrong. NEVER a bare `sqlite3 INSERT` (silently drops apostrophes).

2. **File memory** (only if it's a durable fact worth auto-loading every
   session) — write a one-fact file in
   Claude Code's auto-memory directory for the home project
   (`~/.claude/projects/-Users-$USER/memory/`) with the standard frontmatter
   (`name`, `description`, `metadata.type: user|feedback|project|reference`) and
   add a one-line pointer to `MEMORY.md` in the same directory. Update existing
   files rather than duplicating.

3. Never store secrets — reference key paths like `~/keys/<name>` only.

Respond with what was stored and where (hivemind id, and file name if created).
