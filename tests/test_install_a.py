import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

KIT = Path(__file__).resolve().parents[1]


def run(home, script='install.py', *args):
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(home / '.codex'), HIVEMIND_DB=str(home / 'unused.db'), AGENT_KIT_CLOUD='0', HM_ADJUDICATE='0', PYTHONDONTWRITEBYTECODE='1')
    return subprocess.run([sys.executable, str(KIT / script), '--home', str(home), '--no-smoke', *args], env=env, capture_output=True, text=True)


def test_both_idempotent_and_upgrade(tmp_path):
    home = tmp_path / 'home'; home.mkdir()
    skill = home / '.agents/skills' / next(p.name for p in (KIT / 'skills').iterdir() if p.is_dir())
    skill.mkdir(parents=True)
    old = skill / 'retired.txt'; old.write_text('old', encoding='utf-8')
    import hashlib
    manifest = home / '.claude/agent-kit-manifest.json'; manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({old.relative_to(home).as_posix(): hashlib.sha256(b'old').hexdigest()}), encoding='utf-8')
    a = run(home); assert a.returncode == 0, a.stdout + a.stderr
    assert not old.exists()
    assert list(home.glob('agent-kit-backup-*/.agents/skills/*/retired.txt'))
    assert (home / '.claude/CLAUDE.md').is_file() and (home / '.codex/AGENTS.md').is_file()
    b = run(home); assert b.returncode == 0, b.stdout + b.stderr
    assert len(list(home.glob('agent-kit-install-report-*.md'))) == 2
    assert len(json.loads((home / '.claude/settings.json').read_text())['hooks']['SessionEnd']) == 1


def test_block_edit_and_bad_markers(tmp_path):
    home = tmp_path / 'home'; home.mkdir()
    assert run(home).returncode == 0
    p = home / '.codex/AGENTS.md'; raw = p.read_bytes(); p.write_bytes(b'outside\r\n' + raw.replace(b'Global agent', b'My agent') + b'\r\ntail')
    before = p.read_bytes(); r = run(home); assert r.returncode == 0 and p.read_bytes() == before
    import importlib.util
    spec = importlib.util.spec_from_file_location('kit_install', KIT / 'install.py'); mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    p.write_text(mod.END + '\n' + mod.START, encoding='utf-8'); r = run(home)
    assert r.returncode == 2 and p.read_text(encoding='utf-8') == mod.END + '\n' + mod.START


def test_settings_keeps_values_and_invalid(tmp_path):
    home = tmp_path / 'home'; (home / '.claude').mkdir(parents=True)
    settings = home / '.claude/settings.json'; original = {'env': {'PRIVATE': 'abc'}, 'hooks': {'SessionEnd': [{'hooks': [{'type': 'command', 'command': 'other'}]}]}}
    settings.write_text(json.dumps(original), encoding='utf-8')
    assert run(home).returncode == 0
    data = json.loads(settings.read_text()); assert data['env'] == original['env'] and data['hooks']['SessionEnd'][0] == original['hooks']['SessionEnd'][0]
    settings.write_text('{broken', encoding='utf-8'); result = run(home)
    assert result.returncode == 2 and settings.read_text() == '{broken'


def _check(home):
    # An empty PATH: the version probes find no claude, codex, git, node or bun, so nothing
    # those programs write into their own folders can appear in the test home.
    empty = home.parent / 'empty-path'; empty.mkdir(exist_ok=True)
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(home / '.codex'), PYTHONDONTWRITEBYTECODE='1', PATH=str(empty))
    return subprocess.run([sys.executable, str(KIT / 'check_install.py')], env=env, capture_output=True, text=True)


def test_inventory_never_names_what_the_kit_did_not_install(tmp_path):
    home = tmp_path / 'jane.doe@example.com home'; home.mkdir()
    assert run(home).returncode == 0
    secret = 'FakeSecretDoNotPrint123'
    (home / '.claude.json').write_text(json.dumps({'mcpServers': {secret + 'server': {'env': {'KEY': secret}}}}), encoding='utf-8')
    settings = json.loads((home / '.claude/settings.json').read_text()); settings['env'] = {'KEY': secret}
    (home / '.claude/settings.json').write_text(json.dumps(settings), encoding='utf-8')
    (home / 'keys').mkdir(); (home / 'keys/private').write_text(secret, encoding='utf-8')
    (home / '.claude/skills' / (secret + '-skill')).mkdir()
    (home / '.claude/agents' / (secret + '.md')).write_text('x')
    (home / '.claude/memory-tools' / (secret + '.py')).write_text('x')
    (home / '.claude/plugins').mkdir()
    (home / '.claude/plugins/installed_plugins.json').write_text(json.dumps({'plugins': {secret + '@market': [{'version': secret}], 'claude-mem@thedotmack': [{'version': '1.2.3'}]}}))
    con = sqlite3.connect(str(home / 'hivemind.db')); con.execute('CREATE TABLE "%s" (x)' % secret)
    con.execute("INSERT INTO learnings(category, rule) VALUES('context', ?)", (secret,)); con.commit(); con.close()
    before = sorted(p.relative_to(home).as_posix() for p in home.rglob('*'))
    result = _check(home); assert result.returncode == 0, result.stderr
    report = next(home.glob('agent-kit-check-*.md'))
    for text in (result.stdout, report.read_text(encoding='utf-8')):
        assert secret not in text and 'example.com' not in text and str(home) not in text and '@' not in text
        assert '- claude-mem plugin: present' in text and '- Other plugins: 1' in text and '- MCP servers configured: 1' in text
        assert '- Kit version: 4' in text and '1 not from the kit' in text and '- Memory database check: passed' in text
        assert '- Session-end hook: present' in text and 'Instructions block in CLAUDE.md: present, unchanged' in text
    after = sorted(p.relative_to(home).as_posix() for p in home.rglob('*'))
    assert [x for x in after if x not in before] == [report.name]  # nothing else was created, no database sidecars


def test_inventory_on_a_machine_without_the_kit(tmp_path):
    home = tmp_path / 'home'; home.mkdir()
    result = _check(home); assert result.returncode == 0, result.stderr
    assert 'Kit version: unknown' in result.stdout and '- Memory database: absent' in result.stdout


def test_shim_and_existing_database(tmp_path):
    a = tmp_path / 'a'; b = tmp_path / 'b'; a.mkdir(); b.mkdir()
    assert run(a, 'install.py').returncode == run(b, 'install_codex.py').returncode == 0
    db = a / 'hivemind.db'
    with sqlite3.connect(str(db)) as con: con.execute("INSERT INTO learnings(category, rule) VALUES('context','owner')")
    before = db.read_bytes(); assert run(a, 'install_codex.py').returncode == 0
    assert db.read_bytes() == before


def test_smoke_existing_database_is_unchanged(tmp_path):
    home = tmp_path / 'home'; home.mkdir()
    assert run(home).returncode == 0
    db = home / 'hivemind.db'
    with sqlite3.connect(str(db)) as con: con.execute("INSERT INTO learnings(category, rule) VALUES('context','owner')")
    with sqlite3.connect(str(db)) as con: before = con.execute('SELECT * FROM learnings').fetchall()
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), HIVEMIND_DB=str(home / 'unrelated.db'), AGENT_KIT_CLOUD='0', HM_ADJUDICATE='0', PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, str(KIT / 'install.py'), '--home', str(home)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'isolated memory write and recall passed' in result.stdout
    with sqlite3.connect(str(db)) as con: assert con.execute('SELECT * FROM learnings').fetchall() == before


def test_dry_run_and_host_selection(tmp_path):
    home = tmp_path / 'dry'; home.mkdir()
    result = run(home, 'install.py', '--dry-run', '--host', 'codex')
    assert result.returncode == 0 and not list(home.iterdir())
    assert run(home, 'install.py', '--host', 'codex').returncode == 0
    assert (home / '.codex/AGENTS.md').exists() and not (home / '.claude/CLAUDE.md').exists()


def test_retired_changed_file_kept(tmp_path):
    import hashlib
    home = tmp_path / 'home'; (home / '.claude/rules').mkdir(parents=True)
    old = home / '.claude/rules/retired.txt'; old.write_text('changed', encoding='utf-8')
    (home / '.claude/agent-kit-manifest.json').write_text(json.dumps({'.claude/rules/retired.txt': hashlib.sha256(b'original').hexdigest()}), encoding='utf-8')
    result = run(home)
    assert result.returncode == 0 and old.read_text() == 'changed' and 'removed from kit' in result.stdout


def test_session_hook(tmp_path):
    home = tmp_path / 'home'; home.mkdir()
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
    p = subprocess.run([sys.executable, str(KIT / 'claude/hooks/session_end.py')], env=env, capture_output=True, text=True, cwd=str(home), timeout=3)
    assert p.returncode == 0 and p.stdout == p.stderr == ''
    entry = json.loads((home / '.claude/memory-tools/session-log.jsonl').read_text())
    assert entry['cwd'] == str(home) and set(entry) == {'time', 'cwd', 'branch', 'last_commit'}


def test_fresh_smoke_records_only_new_database(tmp_path):
    home = tmp_path / 'home'; home.mkdir()
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), PYTHONDONTWRITEBYTECODE='1', AGENT_KIT_CLOUD='0', HM_ADJUDICATE='0')
    result = subprocess.run([sys.executable, str(KIT / 'install.py'), '--home', str(home)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'isolated memory write and recall passed' in result.stdout
    with sqlite3.connect(str(home / 'hivemind.db')) as con:
        assert con.execute('SELECT rule FROM learnings').fetchall() == [('agent kit v4 installed on this machine',)]


def test_hook_is_exec_form(tmp_path):
    home = tmp_path / 'home with space'; home.mkdir()
    assert run(home).returncode == 0
    hook = json.loads((home / '.claude/settings.json').read_text())['hooks']['SessionEnd'][0]['hooks'][0]
    assert hook['type'] == 'command' and hook['command'] == str(Path(sys.executable).resolve())
    assert hook['args'] == [str(home / '.claude' / 'hooks' / 'agent-kit' / 'session_end.py')]
    assert Path(hook['args'][0]).is_file()
    p = subprocess.run([hook['command']] + hook['args'], env=dict(os.environ, HOME=str(home), USERPROFILE=str(home)), capture_output=True, text=True, cwd=str(home), timeout=5)
    assert p.returncode == 0 and p.stdout == p.stderr == ''


def _v3_block(mod, home, edit=None):
    text = (KIT / 'legacy' / 'AGENTS.codex.v3.md').read_text(encoding='utf-8')
    for key, value in {'PY': '& "C:/Program Files/Python312/python.exe" -X utf8', 'MEM': (home / '.claude/memory-tools').as_posix(),
                       'RULES': (home / '.claude/rules').as_posix(), 'PRIMER': (home / '.claude/primer.md').as_posix(),
                       'LESSONS': (home / '.claude/lessons.md').as_posix(), 'HIVEMIND': (home / 'hivemind.db').as_posix(),
                       'SKILLS': (home / '.agents/skills').as_posix(), 'CODEX_HOME': (home / '.codex').as_posix(), 'KEYS': (home / 'keys').as_posix()}.items():
        text = text.replace('{{' + key + '}}', value)
    if edit: text = text.replace(*edit)
    return mod.START + '\n' + text.rstrip() + '\n' + mod.END + '\n'


def _load_installer():
    import importlib.util
    spec = importlib.util.spec_from_file_location('kit_install', KIT / 'install.py'); mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def test_unedited_version3_block_is_upgraded_with_backup(tmp_path):
    mod = _load_installer()
    home = tmp_path / 'home'; (home / '.codex').mkdir(parents=True); (home / '.claude').mkdir()
    agents = home / '.codex/AGENTS.md'
    agents.write_bytes(('my own notes\n\n' + _v3_block(mod, home) + 'after\n').replace('\n', '\r\n').encode('utf-8'))
    (home / '.claude/agent-kit-manifest.json').write_text(json.dumps({'.agents/skills/recall/SKILL.md': 'x'}), encoding='utf-8')
    r = run(home); assert r.returncode == 0, r.stdout + r.stderr
    data = agents.read_bytes()
    assert data.startswith(b'my own notes\r\n\r\n') and data.endswith(b'after\r\n') and b'agent-kit v3' not in data and b'agent-kit v4' in data
    assert 'managed block from version 3 (unedited) replaced' in r.stdout
    backups = list(home.glob('agent-kit-backup-*/codex-instructions.md'))
    assert len(backups) == 1 and b'agent-kit v3' in backups[0].read_bytes()
    assert len(list(home.glob('agent-kit-backup-*/.claude/agent-kit-manifest.json'))) == 1


def test_edited_version3_block_is_kept(tmp_path):
    mod = _load_installer()
    home = tmp_path / 'home'; (home / '.codex').mkdir(parents=True); (home / '.claude').mkdir()
    agents = home / '.codex/AGENTS.md'
    agents.write_text(_v3_block(mod, home, ('One clear next action per response, not a list.', 'Always answer in Spanish.')), encoding='utf-8')
    before = agents.read_bytes()
    (home / '.claude/agent-kit-manifest.json').write_text(json.dumps({'.agents/skills/recall/SKILL.md': 'x'}), encoding='utf-8')
    r = run(home); assert r.returncode == 0, r.stdout + r.stderr
    assert agents.read_bytes() == before and 'you edited it, so it was kept' in r.stdout


def test_manifest_is_not_rewritten_when_nothing_changed(tmp_path):
    home = tmp_path / 'home'; home.mkdir()
    assert run(home).returncode == 0
    first = (home / '.claude/agent-kit-manifest.json').read_bytes()
    r = run(home); assert r.returncode == 0 and '- Backup: none needed' in r.stdout
    assert (home / '.claude/agent-kit-manifest.json').read_bytes() == first and len(list(home.glob('agent-kit-backup-*'))) == 0


def test_sidecar_files_are_not_installed(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location('kit_install', KIT / 'install.py'); mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    src = tmp_path / 'src'; src.mkdir(); (src / 'real.md').write_text('x'); (src / '._real.md').write_text('y'); (src / '.DS_Store').write_text('z')
    assert [p.name for p in mod.files(src)] == ['real.md']


def test_first_install_reports_hook_installed_and_no_backup(tmp_path):
    home = tmp_path / 'home'; home.mkdir()
    r = run(home); assert r.returncode == 0
    assert '- Backup: none needed' in r.stdout and 'Claude SessionEnd hook' in r.stdout.split('## updated')[0]


def test_utf16_instructions_file_is_left_untouched(tmp_path):
    home = tmp_path / 'home'; (home / '.codex').mkdir(parents=True)
    agents = home / '.codex/AGENTS.md'; agents.write_text('my notes\n', encoding='utf-16')
    before = agents.read_bytes()
    r = run(home); assert r.returncode == 2 and 'not UTF-8 text' in r.stdout
    assert agents.read_bytes() == before


def test_unreadable_manifest_is_backed_up_and_reported_as_a_failure(tmp_path):
    home = tmp_path / 'home'; (home / '.claude').mkdir(parents=True)
    (home / '.claude/agent-kit-manifest.json').write_text('{not json', encoding='utf-8')
    r = run(home); assert r.returncode == 2, r.stdout
    assert 'could not be read' in r.stdout.split('## failed')[1]
    saved = list(home.glob('agent-kit-backup-*/.claude/agent-kit-manifest.json'))
    assert len(saved) == 1 and saved[0].read_text(encoding='utf-8') == '{not json'


def test_unchanged_version3_files_are_recognised_without_a_manifest(tmp_path):
    """A version 3 file is the kit's when its content still matches what version 3 shipped,
    even if the install record is gone; with Windows line endings too."""
    mod = _load_installer()
    home = tmp_path / 'home'; home.mkdir()
    stock = home / '.claude/memory-tools/hm_write.py'; stock.parent.mkdir(parents=True)
    stock.write_bytes(b'old line one\r\nold line two\r\n')
    edited = home / '.claude/memory-tools/mem_recall.py'; edited.write_bytes(b'my own change\n')
    retired = home / '.agents/skills/impeccable/scripts/cleanup-deprecated.mjs'; retired.parent.mkdir(parents=True)
    retired.write_bytes(b'old helper\n')
    inst = mod.Installer(home, home / '.codex', 'both', False)
    inst.v3 = {'.claude/memory-tools/hm_write.py': mod.lf_digest(b'old line one\nold line two\n'),
               '.claude/memory-tools/mem_recall.py': mod.lf_digest(b'what version 3 shipped\n'),
               '.agents/skills/impeccable/scripts/cleanup-deprecated.mjs': mod.lf_digest(b'old helper\n'),
               '.agents/skills/impeccable/SKILL.md': 'x'}
    inst.go(False)
    assert stock.read_bytes() == (KIT / 'memory-tools/hm_write.py').read_bytes()
    assert edited.read_bytes() == b'my own change\n' and '.claude/memory-tools/mem_recall.py' in inst.log['kept_yours']
    assert not retired.exists() and '.agents/skills/impeccable/scripts/cleanup-deprecated.mjs' in inst.log['removed_to_backup']
    assert (home / '.agents/skills/impeccable/SKILL.md').is_file()  # the skill was not skipped as "yours"


def test_unwritable_install_record_is_a_reported_failure(tmp_path, monkeypatch):
    mod = _load_installer()
    home = tmp_path / 'home'; home.mkdir()
    inst = mod.Installer(home, home / '.codex', 'both', False)
    real = mod.atomic
    def atomic(path, data):
        if path.name == 'agent-kit-manifest.json': raise PermissionError('denied')
        return real(path, data)
    monkeypatch.setattr(mod, 'atomic', atomic)
    assert inst.go(False) == 2 and any('could not save the install record' in f for f in inst.log['failed'])
    assert list(home.glob('agent-kit-install-report-*.md'))


def test_version3_block_with_a_changed_path_is_kept(tmp_path):
    mod = _load_installer()
    home = tmp_path / 'home'; (home / '.codex').mkdir(parents=True); (home / '.claude').mkdir()
    agents = home / '.codex/AGENTS.md'
    agents.write_text(_v3_block(mod, home, ((home / '.claude/primer.md').as_posix(), (home / 'my-own-notes/primer.md').as_posix())), encoding='utf-8')
    before = agents.read_bytes()
    (home / '.claude/agent-kit-manifest.json').write_text(json.dumps({'.agents/skills/recall/SKILL.md': 'x'}), encoding='utf-8')
    r = run(home); assert r.returncode == 0, r.stdout + r.stderr
    assert agents.read_bytes() == before and 'you edited it, so it was kept' in r.stdout


def test_inventory_ignores_names_planted_in_the_install_record(tmp_path):
    home = tmp_path / 'home'; home.mkdir()
    assert run(home).returncode == 0
    token = 'owner-token-EXAMPLE123'
    (home / '.claude/skills' / token).mkdir()
    record = home / '.claude/agent-kit-manifest.json'
    data = json.loads(record.read_text()); data['.claude/skills/' + token + '/SKILL.md'] = 'x'; data['@version'] = token
    record.write_text(json.dumps(data), encoding='utf-8')
    jobs = home / '.claude/memory-tools/memory_jobs.py'
    jobs.write_text("import pathlib; pathlib.Path.home().joinpath('extra-write').write_text('x')", encoding='utf-8')
    result = _check(home); assert result.returncode == 0, result.stderr
    assert token not in result.stdout and '1 not from the kit' in result.stdout and 'Kit version: unknown' in result.stdout
    assert not (home / 'extra-write').exists()  # no installed script was run
