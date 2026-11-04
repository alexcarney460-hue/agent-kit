# agent-kit v3

The portable core of Alex's AI agent setup, packaged for the Codex CLI: a memory that
persists between sessions, working rules, and about 40 skills. It ships empty: tools,
schemas and instructions only, with no memory data and no credentials.

**Agents doing the install: follow [INSTALL-FOR-AGENT.md](INSTALL-FOR-AGENT.md).**

## What's inside

| Folder / file | What it is |
|---|---|
| `install_codex.py` | Cross-platform installer (Windows, macOS, Linux, WSL). Idempotent, keeps your edits, backs up anything it replaces. `--dry-run` shows the plan. |
| `AGENTS.codex.md` | Global instructions for Codex, rendered into `~/.codex/AGENTS.md` with this machine's paths. |
| `memory-tools/` | `hm_write.py` (apostrophe-safe memory writer), `mem_recall.py` (hybrid recall: keyword, reinforcement, semantic entities, optional vectors and graph ranking), `sem*.py` (entity and alias layer), `consolidate.py` (merge and distil), tests, schemas. |
| `rules/` | Operating mode, the 13 production layers, coding style, testing, security, git workflow, agent orchestration, Python and TypeScript rules. |
| `skills/` | Design, debugging, TDD, planning, code review, adversarial review, blast radius, benchmarking, Next.js/React/TypeScript/Python, recall, remember, handoff, promptify, writing skills, and more. |

## Design notes

- One fact per memory; corrections are stored with the reason; recall favours what gets used.
- Always write memory through `hm_write.py`: a bare `sqlite3 INSERT` silently drops apostrophes.
- Memory stays on this machine. `hm_write.py` can ask the Claude CLI to merge near-duplicate notes, but only
  when you set `HM_ADJUDICATE=1`; that sends the notes involved to your Claude account.
- The impeccable design skill ships without its live-preview mode, whose local server let other websites
  read project files.
- Credentials live in a private folder such as `~/keys/` and are referred to by path only.
  The memory indexer reads only the file names there, never the contents (enforced by tests).
- Some skills mention Claude Code; they work the same way in Codex.
