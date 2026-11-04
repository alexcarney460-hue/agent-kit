# Install guide for the agent doing the install (Codex)

You are installing Alex's agent setup (agent-kit v3) for the owner of this computer, for the
Codex CLI. It adds a memory that persists between sessions, working rules, and about 40
skills. The kit holds tools and instructions only: no memory data from anyone else, no
passwords, keys or tokens. If you ever find something that looks like a password, key or
someone's private business information in it, stop and tell the owner.

Work through the steps in order and show the output of each check. If a step fails, fix it
or report it plainly; never skip it silently. Ask the owner before deleting or replacing any
file of theirs, before running anything as administrator, and before installing software.
Do not change the Codex login, config.toml, sandbox or approval settings.

## 1. Prerequisites

- git: `git --version`.
- Python 3.9 or newer: `python --version` (on Windows also try `py -3 --version`; inside WSL,
  macOS or Linux use `python3`). If Python is missing, ask the owner, then on Windows:
  `winget install Python.Python.3.12` and open a new terminal.
- Optional, for better recall: `python -m pip install --user numpy scipy`.

## 2. Get the kit

Clone it into a folder named `agent-kit` in the owner's home folder (Windows:
`%USERPROFILE%\agent-kit`):

```
git clone https://github.com/alexcarney460-hue/agent-kit.git agent-kit
cd agent-kit
```

The repository is public, so no GitHub login is needed.

## 3. Install

Run the installer with the same Python you checked above, first as a dry run:

```
python install_codex.py --dry-run
python install_codex.py
```

It prints a report and also saves it as `agent-kit-install-report-<time>.md` in the home folder.
It is safe to re-run. What it does:

| Piece | Where |
|---|---|
| ~40 skills (design, debugging, TDD, planning, code review, adversarial review, Next.js/React/TypeScript/Python, recall, remember, handoff, promptify, and more) | `~/.agents/skills/` (Codex reads skills here) |
| Rules (operating mode, the 13 production layers, coding style, testing, security, git, agents, Python, TypeScript) | `~/.claude/rules/` |
| Memory system (safe writer, unified recall, semantic entity layer, tests) | `~/.claude/memory-tools/` |
| Memory database, created empty | `~/hivemind.db` |
| Global instructions for Codex, inside a marked block; anything already in the file is kept | `~/.codex/AGENTS.md` (or `$CODEX_HOME/AGENTS.md`) |
| State notes | `~/.claude/primer.md`, `~/.claude/lessons.md` |

It never deletes anything. If the owner already has a skill with the same name, or edited a
kit file since the last install, it keeps theirs and lists it under `kept_yours` or
`skipped_skills`. Anything it replaces is copied to `~/agent-kit-backup-<time>/` first.
Exit status 2 means a step under `failed` needs attention.

## 4. gstack (recommended)

gstack is a public skill suite (browser QA, code review, shipping, design review, security
audit and more) with its own Codex installer. It needs bun and a bash shell (on Windows,
Git Bash or WSL). Show the owner the commands and ask before running them:

```
git clone --single-branch --depth 1 https://github.com/garrytan/gstack.git ~/gstack
cd ~/gstack && ./setup --host codex
```

If bun is missing, gstack's README says how to install it. If setup fails on this machine,
report the error and move on; the rest of the kit does not depend on gstack.

## 5. Verify (show the output of each)

```
python ~/.claude/memory-tools/mem_recall.py "agent kit installed" --k 3
```
(On Windows use the full path, for example
`python "$env:USERPROFILE\.claude\memory-tools\mem_recall.py" "agent kit installed" --k 3`.)
It should print the line "agent kit v3 installed on this machine".

Then start a NEW Codex session and check:
- it describes the operating rules from AGENTS.md when asked "what are your operating rules?"
- it lists the kit's skills when asked "which skills do you have?"
- the memory tests pass (optional): `python -m pip install --user pytest numpy scipy`, then
  `python -m pytest -q` inside `~/.claude/memory-tools`.

## 6. Personalise (ask the owner)

- Add the people, projects and tools the owner works with to
  `~/.claude/memory-tools/semantic/seed.json`, then run `python ~/.claude/memory-tools/sem.py seed`.
- Edit the "Preferences" section at the end of the agent-kit block in `~/.codex/AGENTS.md`
  to taste. Re-running the installer replaces that block, so personal notes belong outside it.
- The Firecrawl skills need a Firecrawl API key. If the owner wants them, keep the key in a
  private file (for example `~/keys/firecrawl.txt`) and never in a repository.

## 7. Report back

Tell the owner: what was installed and where, what was skipped and why, the full error
text for anything that failed, and where the backup folder is.

## Updating later

```
cd ~/agent-kit && git pull && python install_codex.py
```
