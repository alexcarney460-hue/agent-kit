"""Install agent-kit v3 for the Codex CLI on Windows, macOS, Linux or WSL.

    python install_codex.py --dry-run     # show what would change, change nothing
    python install_codex.py               # install (safe to re-run)

What it does:
  skills        -> ~/.agents/skills/<name>/      (Codex reads this folder)
  rules         -> ~/.claude/rules/
  memory tools  -> ~/.claude/memory-tools/      (the tools expect this exact path)
  memory DB     -> ~/hivemind.db                 (created empty from the kit schema)
  instructions  -> $CODEX_HOME/AGENTS.md         (a marked block; the rest of the file is kept)
  state notes   -> ~/.claude/primer.md, ~/.claude/lessons.md (created only if missing)

It never deletes anything, never touches Codex login, config.toml, sandbox or approval
settings, and never overwrites a file you changed: it keeps yours and reports it. Files it
replaces are copied to ~/agent-kit-backup-<time>/ first. --home installs into another folder
(used for testing).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent
MARK_START = "<!-- agent-kit:start (managed by agent-kit install_codex.py; edits inside are replaced on re-install) -->"
MARK_END = "<!-- agent-kit:end -->"
INSTALL_RECORD = "agent kit v3 installed on this machine"
SKIP_NAMES = {"__pycache__", ".DS_Store"}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def kit_files(src: Path):
    for p in sorted(src.rglob("*")):
        if p.is_file() and not any(part in SKIP_NAMES for part in p.parts) and p.suffix not in {".pyc", ".db"}:
            yield p


class Installer:
    def __init__(self, home: Path, codex_home: Path, dry: bool):
        self.home, self.codex_home, self.dry = home, codex_home, dry
        self.stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.backup = home / f"agent-kit-backup-{self.stamp}"
        self.manifest_path = home / ".claude" / "agent-kit-manifest.json"
        try:
            self.old_manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.old_manifest = {}
        self.manifest: dict[str, str] = {}
        self.log: dict[str, list[str]] = {"installed": [], "updated": [], "unchanged": [], "kept_yours": [], "skipped_skills": [], "notes": [], "failed": []}

    # ---- file placement -------------------------------------------------
    def back_up(self, dest: Path) -> None:
        target = self.backup / dest.relative_to(self.home)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dest, target)

    def place(self, src: Path, dest: Path) -> None:
        key = dest.relative_to(self.home).as_posix()
        new = sha(src)
        if not dest.exists():
            action = "installed"
        elif sha(dest) == new:
            self.manifest[key] = new
            self.log["unchanged"].append(key)
            return
        elif self.old_manifest.get(key) == sha(dest):
            action = "updated"  # still exactly what the kit put there last time
        else:
            self.log["kept_yours"].append(key)
            return
        if not self.dry:
            if action == "updated":
                self.back_up(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        self.manifest[key] = new
        self.log[action].append(key)

    def place_tree(self, src: Path, dest: Path) -> None:
        for f in kit_files(src):
            self.place(f, dest / f.relative_to(src))

    def skills(self) -> None:
        root = self.home / ".agents" / "skills"
        for skill in sorted(p for p in (KIT / "skills").iterdir() if p.is_dir()):
            dest = root / skill.name
            ours = any(k.startswith(f".agents/skills/{skill.name}/") for k in self.old_manifest)
            if dest.exists() and not ours:
                self.log["skipped_skills"].append(f"{skill.name} (you already have a skill with this name)")
                continue
            self.place_tree(skill, dest)

    # ---- memory ---------------------------------------------------------
    def env(self) -> dict[str, str]:
        e = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home), PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        return e

    def run(self, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-X", "utf8", *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=self.env(), timeout=timeout)

    def memory(self) -> None:
        db = self.home / "hivemind.db"
        if db.exists():
            self.log["notes"].append("hivemind.db already exists; kept as is")
        elif not self.dry:
            schema = (KIT / "memory-tools" / "hivemind-schema.sql").read_text(encoding="utf-8")
            with sqlite3.connect(db) as conn:
                conn.executescript(schema)
            self.log["installed"].append("hivemind.db (empty, from the kit schema)")
        else:
            self.log["installed"].append("hivemind.db (empty, from the kit schema)")
        if self.dry:
            return
        mem = self.home / ".claude" / "memory-tools"
        if (mem / "semantic.db").exists():
            self.log["notes"].append("semantic.db already exists; not re-seeded, so your entities are kept")
            return
        r = self.run(str(mem / "sem.py"), "seed")
        (self.log["notes"] if r.returncode == 0 else self.log["failed"]).append(
            f"semantic seed rc={r.returncode} {(r.stdout + r.stderr).strip()[-300:]}")

    def smoke(self) -> None:
        mem = self.home / ".claude" / "memory-tools"
        with sqlite3.connect(self.home / "hivemind.db") as conn:
            have = conn.execute("SELECT count(*) FROM learnings WHERE rule LIKE ?", (INSTALL_RECORD + "%",)).fetchone()[0]
        if not have:
            w = self.run(str(mem / "hm_write.py"), "context", INSTALL_RECORD,
                         "--context", "fresh install for Codex; memory starts empty", "--confidence", "0.9")
            if w.returncode != 0:
                self.log["failed"].append(f"memory write rc={w.returncode} {(w.stdout + w.stderr).strip()[-300:]}")
                return
        r = self.run(str(mem / "mem_recall.py"), "agent kit installed", "--k", "3")
        if r.returncode == 0 and INSTALL_RECORD in r.stdout:
            self.log["notes"].append("memory smoke test passed: wrote the install record and recall found it")
        else:
            self.log["failed"].append(f"memory recall rc={r.returncode} did not find the install record: {(r.stdout + r.stderr).strip()[-400:]}")

    # ---- instructions ---------------------------------------------------
    def agents_md(self) -> None:
        mem = self.home / ".claude" / "memory-tools"
        exe = sys.executable
        if " " not in exe:
            py = f"{exe} -X utf8"
        elif os.name == "nt":
            py = f'& "{exe}" -X utf8'  # PowerShell (Codex's Windows shell) needs & to run a quoted path
        else:
            py = f'"{exe}" -X utf8'
        values = {
            "{{PY}}": py,
            "{{MEM}}": mem.as_posix(),
            "{{RULES}}": (self.home / ".claude" / "rules").as_posix(),
            "{{PRIMER}}": (self.home / ".claude" / "primer.md").as_posix(),
            "{{LESSONS}}": (self.home / ".claude" / "lessons.md").as_posix(),
            "{{HIVEMIND}}": (self.home / "hivemind.db").as_posix(),
            "{{SKILLS}}": (self.home / ".agents" / "skills").as_posix(),
            "{{CODEX_HOME}}": self.codex_home.as_posix(),
            "{{KEYS}}": (self.home / "keys").as_posix(),
        }
        text = (KIT / "AGENTS.codex.md").read_text(encoding="utf-8")
        for k, v in values.items():
            text = text.replace(k, v)
        block = f"{MARK_START}\n{text.rstrip()}\n{MARK_END}\n"
        dest = self.codex_home / "AGENTS.md"
        old = dest.read_text(encoding="utf-8") if dest.exists() else ""
        if MARK_START in old and MARK_END in old:
            before, rest = old.split(MARK_START, 1)
            after = rest.split(MARK_END, 1)[1].lstrip("\n")
            new = before + block + ("\n" + after if after else "")
        else:
            new = (old.rstrip() + "\n\n" if old.strip() else "") + block
        key = "AGENTS.md (in CODEX_HOME)"
        if new == old:
            self.log["unchanged"].append(key)
            return
        if not self.dry:
            if dest.exists():
                target = self.backup / "codex-AGENTS.md"
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, target)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(new, encoding="utf-8")
        self.log["updated" if old else "installed"].append(key + (" (your existing text kept outside the agent-kit block)" if old else ""))

    def notes_files(self) -> None:
        for name, body in (("primer.md", "# Primer\n\nActive project: (none yet)\n"),
                           ("lessons.md", "# Lessons\n\n[date] | what went wrong | rule to follow next time\n")):
            p = self.home / ".claude" / name
            if p.exists():
                continue
            if not self.dry:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(body, encoding="utf-8")
            self.log["installed"].append(f".claude/{name}")

    # ---- main -----------------------------------------------------------
    def go(self, smoke: bool) -> int:
        self.skills()
        self.place_tree(KIT / "rules", self.home / ".claude" / "rules")
        self.place_tree(KIT / "memory-tools", self.home / ".claude" / "memory-tools")
        self.notes_files()
        self.agents_md()
        if not self.dry:
            merged = {**{k: v for k, v in self.old_manifest.items()}, **self.manifest}
            self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
            self.manifest_path.write_text(json.dumps(merged, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        self.memory()
        if smoke and not self.dry:
            self.smoke()
        return self.report()

    def report(self) -> int:
        lines = [f"# agent-kit v3 install report ({'DRY RUN, nothing changed' if self.dry else self.stamp})", "",
                 f"- Python: {sys.version.split()[0]} at {sys.executable}",
                 f"- Platform: {sys.platform}; home: {self.home}; CODEX_HOME: {self.codex_home}",
                 f"- Backups of replaced files: {self.backup if self.backup.exists() else 'none needed'}", ""]
        for key, items in self.log.items():
            if key in ("unchanged",):
                lines.append(f"## {key}: {len(items)} files")
                continue
            lines.append(f"## {key}: {len(items)}")
            lines += [f"- {i}" for i in items[:400]]
            lines.append("")
        text = "\n".join(lines) + "\n"
        print(text)
        if not self.dry:
            (self.home / f"agent-kit-install-report-{self.stamp}.md").write_text(text, encoding="utf-8")
        return 2 if self.log["failed"] else 0


def main() -> int:
    if sys.version_info < (3, 9):
        print(f"Python 3.9 or newer is needed; this is {sys.version.split()[0]}")
        return 1
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    ap.add_argument("--home", help="install into this folder instead of your home folder (testing)")
    ap.add_argument("--no-smoke", action="store_true", help="skip the memory write/recall check")
    a = ap.parse_args()
    home = Path(a.home).resolve() if a.home else Path.home()
    codex_home = home / ".codex" if a.home else Path(os.environ.get("CODEX_HOME") or home / ".codex")
    return Installer(home, codex_home, a.dry_run).go(smoke=not a.no_smoke)


if __name__ == "__main__":
    sys.exit(main())
