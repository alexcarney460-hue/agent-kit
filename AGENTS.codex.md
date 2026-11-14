# Global agent instructions (installed by agent-kit v4)

These instructions come from Alex's agent setup, adapted for Codex. They give you a
memory that persists between sessions, a set of working rules, and a library of skills.
The installer filled in the paths below for this machine.

## Session start

1. Read `{{PRIMER}}`: the current state (active project, what's done, next step,
   blockers). If it is empty, ask what we're working on.
2. Before acting on anything non-trivial, recall what we already know:
   `{{PY}} "{{MEM}}/mem_recall.py" "<query>"`
3. Read `{{RULES}}/common/operating-mode.md` once per session. It sets how to work:
   act instead of over-planning, build only what's asked, lead with the outcome, and
   ground every progress claim in real command output.

## Rules (read the one that fits before doing that kind of work)

- Planning a feature or a new project: `{{RULES}}/common/production-layers.md` (the 13
  production layers every plan must cover) and `{{RULES}}/common/development-workflow.md`.
- Writing code: `{{RULES}}/common/coding-style.md`, `{{RULES}}/common/testing.md`,
  `{{RULES}}/common/security.md`, plus `{{RULES}}/python/` or `{{RULES}}/typescript/` for
  those languages.
- Committing: `{{RULES}}/common/git-workflow.md`.
- Running several agents: `{{RULES}}/common/agents.md`.

## Memory (the second brain)

- Recall: `{{PY}} "{{MEM}}/mem_recall.py" "<query>"`. It searches the hivemind database
  (`{{HIVEMIND}}`) by keyword, reinforcement and the semantic entity layer.
- Store important context through the safe writer, never a bare sqlite INSERT (a bare
  INSERT silently drops apostrophes):
  `{{PY}} "{{MEM}}/hm_write.py" context "<what happened>" --context "<details>" --confidence 0.8`
- After the user corrects you, store the lesson:
  `{{PY}} "{{MEM}}/hm_write.py" correction "<rule>" --context "<what happened>" --confidence 0.8`
  and append `[date] | what went wrong | rule to follow next time` to `{{LESSONS}}`.
- Hivemind is the primary memory. The primer is a short state note on top of it.
- If recall misses something you know is stored, add an alias:
  `{{PY}} "{{MEM}}/sem.py" alias <slug> "the name it missed"`.
- To teach recall about the people and projects you work with, edit
  `{{MEM}}/semantic/seed.json` and run `{{PY}} "{{MEM}}/sem.py" seed`.
- After finishing a task, overwrite `{{PRIMER}}` (keep it under about 4 KB) with: active
  project, what's done, exact next step, open blockers.

## Workflow

- Plan before any task with three or more steps. If something goes wrong mid-task, stop
  and re-plan.
- Never call a task done without proving it works: tests run, output shown, live check done.
- Bugs: find the root cause and fix it; don't hand the problem back.
- Important changes get an independent review before "done". Use the `adversarial-review`
  or `code-reviewer` skill, ideally in a fresh session that didn't write the code.
- Commit at logical checkpoints with conventional commit messages. Before closing, check
  for uncommitted changes and remind the user to commit.

## Skills

The kit's skills are in `{{SKILLS}}` (design, debugging, test-driven development,
planning, review, Next.js/React/TypeScript/Python, recall, remember, handoff, promptify
and more). gstack, if installed, adds its own skills under `{{CODEX_HOME}}/skills/`. Use the
matching skill whenever a task fits one.

## Hard rules

- Never put API keys, passwords or tokens into memory, notes, chat or any repository.
  Keep live credentials in a private folder such as `{{KEYS}}` and refer to them by path.
- Confirm before anything hard to reverse or outward-facing (deleting data, production
  writes, sending messages or money) unless the user has explicitly said to go ahead.

## Preferences (edit to taste)

- One clear next action per response, not a list.
- Flag anything uncertain with [UNCLEAR].
- Remind me to commit at the end of a session.
