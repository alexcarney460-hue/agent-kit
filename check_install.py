"""Read-only agent-kit inventory, safe to share.

It changes nothing except writing its own report file in the home folder. It prints fixed
labels, counts, version numbers and names that are in the published kit. Anything
the kit did not install (other skills, plugins, MCP servers, database tables) is only
counted, never named, and no file contents, memory text, environment values or paths are
printed.

The only programs it starts are `claude --version`, `codex --version`, `git --version`,
`node --version` and `bun --version` (when installed; they may do their own housekeeping in
their own folders) and, on Windows, `schtasks /Query` to see whether the kit's four job names
are registered. It runs no script from the installed folders.
"""
from __future__ import annotations
import contextlib
import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

KIT = Path(__file__).resolve().parent
HOME = Path.home()
CLAUDE = HOME / '.claude'
CODEX = Path(os.environ.get('CODEX_HOME') or HOME / '.codex').expanduser()
START = '<!-- agent-kit:start (managed by agent-kit install_codex.py; edits inside are replaced on re-install) -->'
END = '<!-- agent-kit:end -->'
VERSION = re.compile(r'(?<![\d.])\d+(?:\.\d+){1,3}')
JOBS = ('backup', 'compact', 'consolidate', 'semantic-refresh')
PLATFORMS = ('Windows', 'Darwin', 'Linux')
MACHINES = ('AMD64', 'ARM64', 'x86_64', 'arm64', 'aarch64', 'x86')


def load(path):
    try:
        with path.open(encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def one_of(value, allowed):
    return value if value in allowed else 'other'


def tool_version(command):
    """A dotted version number, or a fixed word. Never raw program output."""
    try:
        found = shutil.which(command)
        if not found:
            return 'not found'
        # npm installs .cmd shims on Windows; those need cmd.exe to start.
        argv = ['cmd', '/c', found, '--version'] if found.lower().endswith(('.cmd', '.bat')) else [found, '--version']
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10, text=True, encoding='utf-8', errors='replace')
        match = VERSION.search(p.stdout[:1000])
        return match.group(0) if p.returncode == 0 and match else 'unavailable'
    except (OSError, subprocess.TimeoutExpired):
        return 'unavailable'


def kit_names(relative, directories):
    """Names the published kit contains, read from the kit folder this script is in.

    These names are public (they are in the kit's repository), so they are safe to print.
    Nothing is taken from the install record on this machine, which anyone could have edited.
    """
    try:
        return {p.name for p in (KIT / relative).iterdir() if (p.is_dir() if directories else p.is_file()) and not p.name.startswith('.')}
    except OSError:
        return set()


def present(folder, directories):
    try:
        return {p.name for p in folder.iterdir() if (p.is_dir() if directories else p.is_file()) and not p.name.startswith('.')}
    except OSError:
        return set()


def inventory(label, folder, kit_names, directories):
    have = present(folder, directories)
    ours = sorted(have & kit_names)
    missing = len(kit_names - have)
    return '- %s: %s from the kit (%s)%s; %s not from the kit' % (
        label, len(ours), ', '.join(ours) or 'none', '; %s kit items missing' % missing if missing else '', len(have - kit_names))


def block(path, key, manifest):
    try:
        data = path.read_bytes(); start = START.encode(); end = END.encode()
        if data.count(start) != 1 or data.count(end) != 1 or data.index(start) > data.index(end):
            return 'absent or malformed'
        payload = data[data.index(start):data.index(end) + len(end)]
        saved = manifest.get('@block:' + key)
        if not saved:
            return 'present, from an older kit version'
        return 'present, unchanged' if hashlib.sha256(payload).hexdigest() == saved else 'present, edited'
    except (OSError, ValueError):
        return 'absent or malformed'


def memory_lines():
    db = HOME / 'hivemind.db'
    if not db.is_file():
        return ['- Memory database: absent']
    try:
        # immutable: SQLite reads the file as it is and creates no journal or lock files.
        uri = db.resolve().as_uri() + '?mode=ro&immutable=1'
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as con:
            tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            rows = con.execute('SELECT count(*) FROM learnings').fetchone()[0] if 'learnings' in tables else 0
            ok = con.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        return ['- Memory database: present, %s tables' % len(tables), '- Memories stored: %s' % int(rows),
                '- Memory database check: ' + ('passed' if ok else 'FAILED')]
    except sqlite3.Error:
        return ['- Memory database: present, could not be read']


def job_lines():
    """Whether tasks with the kit's job names are registered. Asks the system scheduler
    itself and reads nothing but task names; no installed script is run."""
    if os.name == 'nt':
        try:
            done = subprocess.run(['schtasks', '/Query', '/FO', 'CSV', '/NH'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, text=True, errors='replace')
        except (OSError, subprocess.TimeoutExpired):
            return ['- Scheduled memory jobs: status unavailable']
        if done.returncode:
            return ['- Scheduled memory jobs: status unavailable']
        registered = {line.split('","')[0].strip().strip('"').lower() for line in done.stdout.splitlines()}
        return ['- Scheduled job %s: %s' % (job, 'registered' if ('\\agent-kit\\' + job) in registered else 'not registered') for job in JOBS]
    if sys.platform == 'darwin':
        folder = HOME / 'Library' / 'LaunchAgents'
        return ['- Scheduled job %s: %s' % (job, 'registered' if (folder / ('com.agent-kit.' + job + '.plist')).is_file() else 'not registered') for job in JOBS]
    return ['- Scheduled memory jobs: not checked on this system (see the crontab)']


def main():
    manifest = load(CLAUDE / 'agent-kit-manifest.json')
    if not isinstance(manifest, dict):
        manifest = {}
    kit_version = str(manifest.get('@version', 'unknown'))
    lines = ['# agent-kit inventory', '',
             '- Kit version: ' + (kit_version if re.fullmatch(r'\d{1,3}', kit_version) else 'unknown (version 3 or earlier, or not installed)'),
             '- Platform: ' + one_of(platform.system(), PLATFORMS), '- Architecture: ' + one_of(platform.machine(), MACHINES),
             '- Python: ' + '.'.join(str(n) for n in sys.version_info[:3])]
    for name in ('claude', 'codex', 'git', 'node', 'bun'):
        lines.append('- %s version: %s' % (name, tool_version(name)))

    lines += ['', '## Claude Code',
              inventory('Skills', CLAUDE / 'skills', kit_names('skills', True), True),
              inventory('Agents', CLAUDE / 'agents', kit_names('claude/agents', False), False),
              inventory('Commands', CLAUDE / 'commands', kit_names('claude/commands', False), False),
              '- Instructions block in CLAUDE.md: ' + block(CLAUDE / 'CLAUDE.md', 'claude', manifest)]
    settings = load(CLAUDE / 'settings.json')
    hooks = settings.get('hooks', {}).get('SessionEnd', []) if isinstance(settings, dict) and isinstance(settings.get('hooks', {}), dict) else []
    script = manifest.get('@hook-script')
    found = bool(script) and isinstance(hooks, list) and any(
        isinstance(h, dict) and h.get('args') == [script]
        for e in hooks if isinstance(e, dict) and isinstance(e.get('hooks', []), list) for h in e.get('hooks', []))
    lines.append('- Session-end hook: ' + ('present' if found else 'absent'))
    plugins = load(CLAUDE / 'plugins' / 'installed_plugins.json')
    entries = plugins.get('plugins', {}) if isinstance(plugins, dict) else {}
    entries = entries if isinstance(entries, dict) else {}
    mem_plugin = [k for k in entries if isinstance(k, str) and k.split('@')[0] == 'claude-mem']
    lines.append('- claude-mem plugin: ' + ('present' if mem_plugin else 'absent'))
    lines.append('- Other plugins: %s' % (len(entries) - len(mem_plugin)))
    config = load(HOME / '.claude.json')
    servers = config.get('mcpServers', {}) if isinstance(config, dict) else {}
    lines.append('- MCP servers configured: %s' % (len(servers) if isinstance(servers, dict) else 0))

    lines += ['', '## Codex',
              inventory('Skills', HOME / '.agents' / 'skills', kit_names('skills', True), True),
              '- Instructions block in AGENTS.md: ' + block(CODEX / 'AGENTS.md', 'codex', manifest)]

    lines += ['', '## Shared',
              inventory('Rule folders', CLAUDE / 'rules', kit_names('rules', True), True),
              inventory('Memory tools', CLAUDE / 'memory-tools', kit_names('memory-tools', False), False)]
    gstack = any((base / 'gstack').exists() for base in (CLAUDE / 'skills', HOME / '.agents' / 'skills', CODEX / 'skills', HOME))
    lines.append('- gstack: ' + ('present' if gstack else 'not found'))
    lines += memory_lines() + job_lines()

    run_id = datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
    name = 'agent-kit-check-' + run_id + '.md'
    lines += ['', 'Report file: ' + name + ' in the home folder', '']
    text = '\n'.join(lines)
    with (HOME / name).open('x', encoding='utf-8') as f:
        f.write(text)
    print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
