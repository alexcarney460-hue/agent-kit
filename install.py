"""Install agent-kit v4 for Claude Code and Codex. No software or jobs are installed."""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

KIT = Path(__file__).resolve().parent
START = '<!-- agent-kit:start (managed by agent-kit install_codex.py; edits inside are replaced on re-install) -->'
END = '<!-- agent-kit:end -->'
RECORD = 'agent kit v4 installed on this machine'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def files(root):
    if root.is_dir():
        for path in sorted(root.rglob('*')):
            if path.is_file() and not path.is_symlink() and not path.name.startswith('._') and not any(x in path.parts for x in ('__pycache__', '.DS_Store')) and path.suffix not in ('.pyc', '.db'):
                yield path


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.agent-kit-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def short_windows_path(path):
    """8.3 form of a Windows path (no spaces), or '' when the volume has none."""
    import ctypes
    path = Path(path)
    suffix = []
    while not path.exists() and path != path.parent:
        suffix.insert(0, path.name)
        path = path.parent
    buf = ctypes.create_unicode_buffer(32768)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, len(buf))
    short = str(Path(buf.value).joinpath(*suffix)) if n and n < len(buf) else ''
    return short.replace('\\', '/') if short and re.fullmatch(r'[A-Za-z0-9_./:~\\-]+', short) else ''


def py_token(host):
    """How the written instructions call this Python, for the shell that host uses."""
    exe = str(Path(sys.executable).resolve())
    if os.name != 'nt':
        return shlex.quote(exe) + ' -X utf8'
    plain = exe.replace('\\', '/')
    if ' ' not in plain:
        return plain + ' -X utf8'
    short = short_windows_path(exe)
    if short:
        return short + ' -X utf8'  # no spaces: parsed the same by PowerShell, cmd and Git Bash
    # Codex runs PowerShell on Windows; Claude Code's Bash tool is Git Bash.
    return ('& "%s" -X utf8' if host == 'codex' else '"%s" -X utf8') % plain


def hook_entry(script):
    """Exec-form hook: Claude Code starts the program directly, so no shell quoting applies."""
    return {'type': 'command', 'command': str(Path(sys.executable).resolve()), 'args': [str(script)]}


def stock_v3_block(inner, home, codex_home):
    """True when the text between the markers is exactly what version 3 installed here.

    Version 3 filled this machine's paths into its template. Every path is rebuilt the way
    version 3 built it, so a block whose paths the owner changed does not match. The one value
    that cannot be rebuilt is the Python command (it depends on the interpreter used then), so
    that single placeholder matches any text on its line.
    """
    try:
        template = (KIT / 'legacy' / 'AGENTS.codex.v3.md').read_text(encoding='utf-8')
        text = inner.decode('utf-8')
    except (OSError, UnicodeDecodeError):
        return False
    values = {'MEM': (home / '.claude' / 'memory-tools').as_posix(), 'RULES': (home / '.claude' / 'rules').as_posix(),
              'PRIMER': (home / '.claude' / 'primer.md').as_posix(), 'LESSONS': (home / '.claude' / 'lessons.md').as_posix(),
              'HIVEMIND': (home / 'hivemind.db').as_posix(), 'SKILLS': (home / '.agents' / 'skills').as_posix(),
              'CODEX_HOME': codex_home.as_posix(), 'KEYS': (home / 'keys').as_posix()}
    rendered = template.replace('\r\n', '\n').strip()
    for key, value in values.items():
        rendered = rendered.replace('{{' + key + '}}', value)
    pattern = re.escape(rendered).replace(re.escape('{{PY}}'), '[^\\n`]+?')
    return re.fullmatch(pattern, text.replace('\r\n', '\n').strip()) is not None


def load_v3_files():
    """Checksums of every file version 3 installed (line endings normalised), by installed path."""
    try:
        data = json.loads((KIT / 'legacy' / 'v3-files.json').read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def lf_digest(data):
    return digest(data.replace(b'\r\n', b'\n'))


class Installer:
    def __init__(self, home, codex, host, dry):
        self.home, self.codex, self.host, self.dry = home, codex, host, dry
        self.run_id = dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex
        self.backup = home / ('agent-kit-backup-' + self.run_id)
        self.manifest_path = home / '.claude' / 'agent-kit-manifest.json'
        try:
            self.old = json.loads(self.manifest_path.read_text(encoding='utf-8'))
            if not isinstance(self.old, dict):
                raise ValueError('not an object')
        except FileNotFoundError:
            self.old = {}
        except (ValueError, OSError):
            self.old = {}
            self.manifest_unreadable = True
        self.v3 = load_v3_files()
        self.manifest = dict(self.old)
        self.desired = set()
        self.log = {k: [] for k in ('installed', 'updated', 'unchanged', 'kept_yours', 'skipped_skills', 'removed_to_backup', 'notes', 'failed')}
        self.created_db = False
        if getattr(self, 'manifest_unreadable', False):
            self.log['failed'].append('the record of what the kit installed before (agent-kit-manifest.json) could not be read. It was backed up and rebuilt. Unchanged version 3 files were still recognised and updated; any other installed file that differs from the kit was kept as yours: check the kept_yours list')
        self.backup_reserved = False

    def backup_file(self, path, key):
        if not self.backup_reserved:
            while True:
                try:
                    self.backup.mkdir()
                    self.backup_reserved = True
                    break
                except FileExistsError:
                    self.run_id = dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex
                    self.backup = self.home / ('agent-kit-backup-' + self.run_id)
        target = self.backup / key
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)

    def place(self, src, dest):
        key = dest.relative_to(self.home).as_posix()
        self.desired.add(key)
        new = digest(src.read_bytes())
        if dest.is_symlink() or (dest.exists() and not dest.is_file()):
            self.log['kept_yours'].append(key); return
        if not dest.exists(): action = 'installed'
        elif digest(dest.read_bytes()) == new:
            self.manifest[key] = new; self.log['unchanged'].append(key); return
        elif self.old.get(key) == digest(dest.read_bytes()) or self.v3.get(key) == lf_digest(dest.read_bytes()): action = 'updated'
        else:
            self.log['kept_yours'].append(key); return
        if not self.dry:
            if action == 'updated': self.backup_file(dest, key)
            atomic(dest, src.read_bytes())
        self.manifest[key] = new
        self.log[action].append(key)

    def tree(self, source, dest):
        for p in files(source): self.place(p, dest / p.relative_to(source))

    def skills(self, root):
        source = KIT / 'skills'
        if not source.is_dir(): return
        for skill in sorted(p for p in source.iterdir() if p.is_dir()):
            dest = root / skill.name
            prefix = dest.relative_to(self.home).as_posix() + '/'
            if dest.exists() and not any(k.startswith(prefix) for k in self.old) and not any(k.startswith(prefix) for k in self.v3):
                self.log['skipped_skills'].append(prefix); continue
            self.tree(skill, dest)

    def obsolete(self, prefixes):
        known = dict(self.old)
        for key, v3hash in self.v3.items():
            known.setdefault(key, None)
        for key, oldhash in list(known.items()):
            if not isinstance(key, str) or not key.startswith(prefixes) or key in self.desired or '..' in Path(key).parts: continue
            path = self.home / key
            if path.is_symlink() or not path.is_file():
                if not path.exists() and not path.is_symlink(): self.manifest.pop(key, None)
                continue
            data = path.read_bytes()
            if digest(data) != oldhash and self.v3.get(key) != lf_digest(data):
                self.log['kept_yours'].append(key + ' (removed from kit)'); continue
            if not self.dry:
                self.backup_file(path, key)
                path.unlink()
            self.manifest.pop(key, None)
            self.log['removed_to_backup'].append(key)

    def render(self, template, skills, host):
        values = {'PY': py_token(host), 'MEM': (self.home / '.claude/memory-tools').as_posix(),
                  'RULES': (self.home / '.claude/rules').as_posix(), 'PRIMER': (self.home / '.claude/primer.md').as_posix(),
                  'LESSONS': (self.home / '.claude/lessons.md').as_posix(), 'HIVEMIND': (self.home / 'hivemind.db').as_posix(),
                  'SKILLS': skills.as_posix(), 'CODEX_HOME': self.codex.as_posix(), 'KEYS': (self.home / 'keys').as_posix()}
        text = template.read_text(encoding='utf-8')
        for key, value in values.items(): text = text.replace('{{' + key + '}}', value)
        return text.rstrip()

    def block(self, dest, template, name, skills):
        key = '@block:' + name
        old = dest.read_bytes() if dest.exists() else b''
        try:
            if old.startswith((b'\xff\xfe', b'\xfe\xff')) or b'\x00' in old:
                raise UnicodeDecodeError('utf-8', old[:2], 0, 1, 'looks like UTF-16')
            old.decode('utf-8')
        except UnicodeDecodeError:
            self.log['failed'].append(name + ' instructions file is not UTF-8 text (Windows PowerShell saves UTF-16 by default); it was left untouched. Save it as UTF-8 and run the installer again'); return
        start, end = START.encode(), END.encode()
        if old.count(start) != old.count(end) or old.count(start) > 1 or (start in old and old.index(start) > old.index(end)):
            self.log['failed'].append(name + ' managed markers malformed; file kept'); return
        block = (START + '\n' + self.render(template, skills, name) + '\n' + END).encode('utf-8')
        if start in old:
            a, b = old.index(start), old.index(end) + len(end)
            current = old[a:b]
            stock_v3 = key not in self.old and stock_v3_block(current[len(start):-len(end)], self.home, self.codex)
            if current != block and self.old.get(key) != digest(current) and not stock_v3:
                self.log['kept_yours'].append(name + ' managed block (you edited it, so it was kept; to take the new instructions, delete the text between the two agent-kit markers and run the installer again)'); return
            if stock_v3 and current != block:
                # Version 3 recorded no hash for its block; it is replaced only when it still matches
                # the text version 3 installed, with nothing of the owner's in it.
                self.log['notes'].append(name + ' managed block from version 3 (unedited) replaced; the old file is in the backup folder')
            new = old[:a] + block + old[b:]
        else:
            new = old + (b'\n\n' if old and not old.endswith(b'\n') else b'\n' if old else b'') + block + b'\n'
        self.manifest[key] = digest(block)
        if new == old: self.log['unchanged'].append(name + ' managed block'); return
        if not self.dry:
            if dest.exists(): self.backup_file(dest, name + '-instructions.md')
            atomic(dest, new)
        self.log['updated' if old else 'installed'].append(name + ' managed block')

    def settings(self):
        path = self.home / '.claude/settings.json'
        try:
            settings = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
            if not isinstance(settings, dict): raise ValueError('root is not an object')
            hooks = settings.get('hooks', {})
            if not isinstance(hooks, dict): raise ValueError('hooks is not an object')
            entries = hooks.get('SessionEnd', [])
            if not isinstance(entries, list): raise ValueError('SessionEnd is not a list')
            script = str(self.home / '.claude' / 'hooks' / 'agent-kit' / 'session_end.py')
            entry_hook = hook_entry(script)
            self.manifest['@hook-script'] = script
            self.manifest.pop('@hook-command', None)
            if any(isinstance(h, dict) and h.get('args') == [script] for e in entries if isinstance(e, dict) and isinstance(e.get('hooks', []), list) for h in e.get('hooks', [])):
                self.log['unchanged'].append('Claude SessionEnd hook'); return
            entry = {'hooks': [entry_hook]}
            settings['hooks'] = dict(hooks, SessionEnd=entries + [entry])
            existed = path.exists()
            if not self.dry:
                if existed: self.backup_file(path, '.claude/settings.json')
                atomic(path, (json.dumps(settings, indent=2, ensure_ascii=False) + '\n').encode('utf-8'))
            self.log['updated' if existed else 'installed'].append('Claude SessionEnd hook')
        except (OSError, ValueError, TypeError) as exc:
            self.log['failed'].append('settings.json or hook: ' + str(exc))

    def notes(self):
        for name, body in (('primer.md', '# Primer\n\nActive project: (none yet)\n'), ('lessons.md', '# Lessons\n\n[date] | what went wrong | rule to follow next time\n')):
            p = self.home / '.claude' / name
            if not p.exists():
                if not self.dry: atomic(p, body.encode())
                self.log['installed'].append('.claude/' + name)

    def environment(self, home, db):
        env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), HIVEMIND_DB=str(db), CLAUDE_MEMORY_DIR=str(home / '.claude/smoke-memory'), PYTHONIOENCODING='utf-8', PYTHONUTF8='1', HM_NO_LLM='1', AGENT_KIT_CLOUD='0', HM_ADJUDICATE='0')
        return env

    def run_tool(self, home, db, *args):
        return subprocess.run([sys.executable, '-B', *args], env=self.environment(home, db), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', timeout=60)

    def new_database(self, path, schema):
        con = sqlite3.connect(str(path))
        try:
            con.executescript(schema); con.commit()
        finally:
            con.close()  # "with connect()" commits but leaves the file open, which Windows will not delete

    def memory(self, smoke):
        db = self.home / 'hivemind.db'
        schema = (KIT / 'memory-tools/hivemind-schema.sql').read_text(encoding='utf-8')
        if not db.exists():
            if not self.dry:
                self.new_database(db, schema)
                self.created_db = True
            self.log['installed'].append('hivemind.db (new)')
        else: self.log['notes'].append('existing hivemind.db kept')
        if self.dry or not smoke: return
        mem = self.home / '.claude/memory-tools'
        temp = tempfile.mkdtemp(prefix='agent-kit-smoke-')
        try:
            temp_home = Path(temp); temp_db = temp_home / 'hivemind.db'
            temp_mem = temp_home / '.claude/memory-tools'
            shutil.copytree(mem, temp_mem, ignore=shutil.ignore_patterns('*.db', '*.db-wal', '*.db-shm', '*.lock', 'logs', '__pycache__', '*.pyc', '*.jsonl'))
            self.new_database(temp_db, schema)
            w = self.run_tool(temp_home, temp_db, str(temp_mem / 'hm_write.py'), 'context', RECORD, '--no-adjudicate')
            r = self.run_tool(temp_home, temp_db, str(temp_mem / 'mem_recall.py'), 'agent kit v4 installed', '--k', '3')
            if w.returncode or r.returncode or RECORD not in r.stdout:
                detail = (w.stdout + w.stderr + r.stdout + r.stderr).strip().splitlines()[-3:]
                self.log['failed'].append('isolated memory smoke failed: write rc=%s recall rc=%s %s' % (w.returncode, r.returncode, ' | '.join(detail))); return
            self.log['notes'].append('isolated memory write and recall passed')
            if self.created_db:
                w = self.run_tool(self.home, db, str(mem / 'hm_write.py'), 'context', RECORD, '--no-adjudicate')
                if w.returncode: self.log['failed'].append('new database install record failed rc=%s' % w.returncode)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.log['failed'].append('memory smoke failed: %s: %s' % (type(exc).__name__, exc))
        finally:
            shutil.rmtree(temp, ignore_errors=True)

    def go(self, smoke):
        common = ('.claude/rules/', '.claude/memory-tools/')
        prefixes = list(common)
        self.tree(KIT / 'rules', self.home / '.claude/rules')
        self.tree(KIT / 'memory-tools', self.home / '.claude/memory-tools')
        if self.host in ('both', 'codex'):
            self.skills(self.home / '.agents/skills'); prefixes.append('.agents/skills/')
            self.block(self.codex / 'AGENTS.md', KIT / 'AGENTS.codex.md', 'codex', self.home / '.agents/skills')
        if self.host in ('both', 'claude'):
            self.skills(self.home / '.claude/skills'); prefixes.append('.claude/skills/')
            for name in ('agents', 'commands'):
                self.tree(KIT / 'claude' / name, self.home / '.claude' / name)
                prefixes.append('.claude/' + name + '/')
            self.tree(KIT / 'claude/hooks', self.home / '.claude/hooks/agent-kit'); prefixes.append('.claude/hooks/agent-kit/')
            self.block(self.home / '.claude/CLAUDE.md', KIT / 'claude/CLAUDE.template.md', 'claude', self.home / '.claude/skills')
            self.settings()
        self.obsolete(tuple(prefixes)); self.notes()
        self.memory(smoke)
        if not self.dry:
            self.manifest['@version'] = '4'
            data = (json.dumps(self.manifest, indent=2, sort_keys=True) + '\n').encode()
            try:
                current = self.manifest_path.read_bytes() if self.manifest_path.is_file() else None
                if current is not None and current != data:
                    self.backup_file(self.manifest_path, '.claude/agent-kit-manifest.json')
                if current != data:
                    atomic(self.manifest_path, data)
            except OSError as exc:
                self.log['failed'].append('could not save the install record (agent-kit-manifest.json): %s' % type(exc).__name__)
        return self.report()

    def report(self):
        lines = ['# agent-kit v4 install report' + (' (dry run)' if self.dry else ''), '', '- Run: ' + self.run_id, '- Backup: ' + (str(self.backup) if self.backup_reserved else 'none needed'), '']
        for key, items in self.log.items():
            lines += ['## %s: %s' % (key, len(items))] + (['- ' + x for x in items] if key != 'unchanged' else []) + ['']
        lines += ['Next: review the install guide for optional plugins, gstack and scheduled jobs.', '']
        text = '\n'.join(lines); print(text)
        if not self.dry:
            with (self.home / ('agent-kit-install-report-' + self.run_id + '.md')).open('x', encoding='utf-8') as f: f.write(text)
        return 2 if self.log['failed'] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', choices=('both', 'claude', 'codex'), default='both')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--home')
    parser.add_argument('--no-smoke', action='store_true')
    args = parser.parse_args()
    home = Path(args.home).expanduser().resolve() if args.home else Path.home()
    codex = home / '.codex' if args.home else Path(os.environ.get('CODEX_HOME') or home / '.codex').expanduser()
    return Installer(home, codex, args.host, args.dry_run).go(not args.no_smoke)

if __name__ == '__main__':
    sys.exit(main())
