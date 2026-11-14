import json
from pathlib import Path
import plistlib
from types import SimpleNamespace
import zipfile

import pytest
import memory_backup_local as backup
import memory_jobs as jobs
import sem_artifacts


def test_credential_components_and_links(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    desktop, documents = tmp_path / 'Desktop', tmp_path / 'Documents'
    paths = [desktop / 'api-keys/note.txt', documents / 'My Passwords/x.md',
             desktop / 'project/auth-tokens.txt']
    good = desktop / 'notes/meeting.md'
    for path in paths + [good]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('sample', encoding='utf-8')
    for i, target in enumerate(paths):
        try:
            (desktop / ('link%d.md' % i)).symlink_to(target)
        except (OSError, NotImplementedError):
            pass
    assert [Path(p) for p, _ in sem_artifacts.walk(str(desktop), 5)] == [good]
    assert list(sem_artifacts.walk(str(documents), 5)) == []
    for path in paths:
        assert not sem_artifacts.safe_path(str(path), str(desktop if desktop in path.parents else documents))
    for name in ('keyboard.md', 'author.md'):
        file = good.parent / name
        file.write_text('sample', encoding='utf-8')
        assert not sem_artifacts.safe_path(str(file), str(desktop))


def test_backup_retention_only_owned(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('USERPROFILE', str(home))
    monkeypatch.setenv('CLAUDE_MEMORY_DIR', str(home / 'memories'))
    monkeypatch.setenv('HIVEMIND_DB', str(home / 'missing.db'))
    monkeypatch.setenv('SEM_DB_PATH', str(home / 'missing-sem.db'))
    dest = tmp_path / 'backups'
    dest.mkdir()
    personal = dest / 'memory-20000101T000000000000Z-personal.zip'
    personal.write_bytes(b'personal')
    unmarked = dest / ('memory-20000101T000000000000Z-' + 'a' * 32 + '.zip')
    with zipfile.ZipFile(unmarked, 'w') as archive:
        archive.writestr('note.txt', 'owner')
    broken = dest / ('memory-20000101T000000000000Z-' + 'b' * 32 + '.zip')
    broken.write_bytes(b'not a zip')
    backup.run(dest, 2)
    backup.run(dest, 2)
    backup.run(dest, 2)
    assert personal.exists() and unmarked.exists() and broken.exists()
    found = backup.backups(dest)
    assert len(found) == 2
    with zipfile.ZipFile(found[0]) as archive:
        marker = json.loads(archive.read('agent-kit-backup.json'))
    assert set(marker) == {'kit', 'format_version', 'created_at'}
    for path in (personal, unmarked, broken):
        with pytest.raises(ValueError):
            backup.verify(path)
        with pytest.raises(ValueError):
            backup.restore(path, tmp_path / 'restore', False)


def test_windows_preserves_existing_and_only_removes_own_xml(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(jobs, 'JOBS', {'backup': []})
    monkeypatch.setattr(jobs, 'windows_user', lambda: 'test')
    python, tools = '/python', tmp_path / 'tools'
    xml = tools / 'jobs/backup.xml'
    xml.parent.mkdir(parents=True)
    xml.write_text('owner content', encoding='utf-8')
    state = ['installed']
    monkeypatch.setattr(jobs, 'windows_state', lambda *args: state[0])
    calls = []
    monkeypatch.setattr(jobs.subprocess, 'run', lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(returncode=0, stdout='', stderr=''))
    assert jobs.run_windows(python, tools, tmp_path, 'install', False) == 0
    assert not calls and xml.read_text(encoding='utf-8') == 'owner content'
    assert 'already installed, left as it is' in capsys.readouterr().out
    assert '/F' not in jobs.windows_commands(python, tools, tmp_path, 'install')[0]
    state[0] = 'not installed'
    assert jobs.run_windows(python, tools, tmp_path, 'remove', False) == 0
    assert xml.read_text(encoding='utf-8') == 'owner content'
    assert 'left' in capsys.readouterr().out
    xml.write_bytes(jobs.windows_helper_content(python, tools, 'backup').encode('utf-16'))  # bytes, as the installer writes it
    assert jobs.run_windows(python, tools, tmp_path, 'remove', False) == 0
    assert not xml.exists()


def test_mac_preserves_existing_schedule(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(jobs, 'JOBS', {'backup': []})
    python, tools, logs = '/python', tmp_path / 'tools', tmp_path / 'logs'
    directory = tmp_path / 'launch'
    directory.mkdir()
    path = directory / 'com.agent-kit.backup.plist'
    content = plistlib.loads(jobs.mac_plists(python, tools, logs)[path.name].encode())
    content['StartCalendarInterval']['Hour'] = 18
    path.write_bytes(plistlib.dumps(content))
    original = path.read_bytes()
    calls = []
    monkeypatch.setattr(jobs.subprocess, 'run', lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(returncode=0, stdout='', stderr=''))
    assert jobs.run_mac(python, tools, logs, directory, 'install', False) == 0
    assert path.read_bytes() == original and not calls
    assert 'already installed, left as it is' in capsys.readouterr().out
    assert jobs.run_mac(python, tools, logs, directory, 'remove', False) == 0
    assert not path.exists() and calls[0][1] == 'unload'


def test_windows_modified_schedule_is_ours_and_preserved(tmp_path, monkeypatch, capsys):
    import xml.etree.ElementTree as ET
    monkeypatch.setattr(jobs, 'JOBS', {'backup': []})
    monkeypatch.setattr(jobs, 'windows_user', lambda: 'test')
    python, tools = '/python', str(tmp_path / 'tools')
    root = ET.fromstring(jobs.windows_xml(python, tools, 'backup'))
    ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
    root.find('.//t:StartBoundary', ns).text = '2000-01-01T18:00:00'
    modified = ET.tostring(root, encoding='unicode')
    monkeypatch.setattr(jobs, 'windows_exists', lambda task: True)
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=modified, stderr='')
    monkeypatch.setattr(jobs.subprocess, 'run', run)
    assert jobs.run_windows(python, tools, tmp_path, 'install', False) == 0
    assert len(calls) == 1 and '/Query' in calls[0]
    assert 'already installed, left as it is' in capsys.readouterr().out
