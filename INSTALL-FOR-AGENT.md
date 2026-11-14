# Install guide for the agent

Install agent-kit v4 for the owner using Claude Code or Codex. The kit contains tools and instructions, not another person's memories or credentials. Stop and tell the owner if you find private information in it. Ask the owner before installing software, deleting anything, or running as administrator. Do not change login, model, permission, sandbox or approval settings.

1. Check `git --version` and `python --version`; Python 3.9 or newer is required. On Windows, `py -3 --version` is an alternative. Ask before installing any missing software.
2. Get the kit. If version 3 was installed earlier, the folder `agent-kit` already exists in the
   owner's home folder: run `git pull` inside it instead of cloning, then continue with the two
   `install.py` lines below. A version 3 install is upgraded in place: files the owner changed
   are kept and reported. The version 3 instruction block in `AGENTS.md` is replaced (after a
   backup) only if the owner never edited it; an edited block is kept, and the report says how
   to take the new one. Otherwise
   clone the public repository and enter it:

   ```sh
   git clone https://github.com/alexcarney460-hue/agent-kit.git agent-kit
   cd agent-kit
   python install.py --dry-run
   python install.py
   ```

   `--host claude` or `--host codex` selects a host; default is both. The installer keeps owner edits, backs up replaced files and unchanged retired kit files under `~/agent-kit-backup-<run id>/`, and reports failures. It creates an initially empty memory database if none exists. The smoke check uses an isolated temporary database and records installation only in a database this run created.
3. Optional Claude Code plugin: ask first. It records observations of the owner's sessions on this machine and summarises them with the owner's own Claude account. It needs Node.js.

   ```sh
   claude plugin marketplace add thedotmack/claude-mem
   claude plugin install claude-mem@thedotmack
   ```
4. Optional gstack for both hosts: ask before installing software. This public suite is at https://github.com/garrytan/gstack and needs bun and a bash shell, such as Git Bash on Windows. Clone the repository, then from its directory run:

   ```sh
   ./setup
   ./setup --host codex
   ```
5. Optional scheduled memory jobs: ask the owner before registering them. Run `python "$HOME/.claude/memory-tools/memory_jobs.py" install --dry-run`, inspect the result, then run `python "$HOME/.claude/memory-tools/memory_jobs.py" install`. (`$HOME` works in PowerShell and in bash.) Linux may require installing the printed crontab lines manually.
6. Local memory backups default to `~/agent-memory-backups`. Use `python "$HOME/.claude/memory-tools/memory_backup_local.py" list` and `verify`, and run `python "$HOME/.claude/memory-tools/memory_backup_local.py" restore <empty-target-directory>` to restore the latest verified backup into a separate directory, then inspect it before copying anything back. Review the command help first. Backups contain personal memory, so keep them private.
7. Memory stays on this computer by default. Setting `AGENT_KIT_CLOUD=1` is the only way to allow the memory tools to hand memory text to the Claude command line program (the README lists exactly which tools do); a dry run never sends. Ask the owner before turning it on, and do not set it for them.
8. Run `python check_install.py` and show the owner its output. It saves the same text as `agent-kit-check-<run id>.md` in the home folder; the report holds versions, counts and the names of kit files only, so the owner can send it to whoever is helping. The nightly index job leaves a memory index alone unless it has a `## By topic` section; that is normal for a new install. Check the install report and resolve any failures. Start a new session for each host and check that its instructions and skills are available.

The kit does not include the author's own memory or credentials, company tooling, a private team chat network, or Mac-only desktop automation.

To update later:

```sh
cd ~/agent-kit && git pull && python install_codex.py
```
