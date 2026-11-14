from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
import os
import consolidate
import embed_index
import eval_recall
import hm_write
import memdir
import memory_jobs as jobs
import sem_artifacts
import sem_discover


def test_private_paths_and_links(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    desktop = tmp_path / 'Desktop'
    workspace = tmp_path / 'workspace'
    documents = tmp_path / 'Documents'
    for path in (desktop / 'CrEdEnTiAlS', workspace / 'project' / 'KeYs', documents / 'notes'):
        path.mkdir(parents=True)
    private = [desktop / 'CrEdEnTiAlS' / 'x.txt', workspace / 'project' / 'KeYs' / 'y.md', documents / 'notes' / '.env']
    for path in private:
        path.write_text('private', encoding='utf-8')
    (desktop / 'normal.md').write_text('ordinary note', encoding='utf-8')
    for index, target in enumerate(private):
        try:
            (desktop / ('link%d.md' % index)).symlink_to(target)
        except (OSError, NotImplementedError):
            pass
    (desktop / 'API-ToKeN.txt').write_text('private', encoding='utf-8')
    hidden = desktop / '.hidden'
    hidden.mkdir()
    (hidden / 'note.md').write_text('private', encoding='utf-8')
    for root in (desktop, workspace, documents):
        found = [Path(path) for path, _ in sem_artifacts.walk(str(root), 5)]
        assert found == ([desktop / 'normal.md'] if root == desktop else [])
        for path in private:
            if root in path.parents:
                assert not sem_artifacts.safe_path(str(path), str(root))
    assert [Path(path).name for path, _ in sem_artifacts.walk(str(hidden), 1)] == ['note.md']
    cloud = tmp_path / '.config' / 'gcloud'
    cloud.mkdir(parents=True)
    (cloud / 'note.md').write_text('private', encoding='utf-8')
    assert list(sem_artifacts.walk(str(cloud), 1)) == []
    nested = workspace / 'project' / 'KeYs' / 'nested'
    nested.mkdir()
    (nested / 'note.md').write_text('private', encoding='utf-8')
    assert list(sem_artifacts.walk(str(nested), 1)) == []


def test_private_directories_never_listed(tmp_path, monkeypatch):
    root = tmp_path / 'Desktop'
    private = root / 'SeCrEtS'
    private.mkdir(parents=True)
    (private / 'content.md').write_text('private', encoding='utf-8')
    scandir = os.scandir
    def checked(path):
        assert Path(path) != private
        return scandir(path)
    monkeypatch.setattr(sem_artifacts.os, 'scandir', checked)
    assert list(sem_artifacts.walk(str(root), 3)) == []


@pytest.mark.parametrize('name', ['.env.local', 'a.PEM', 'a.key', 'a.pfx', 'a.p12', 'a.kdbx',
                                 'a.keystore', 'a.jks', 'id_RSA.pub', 'id_ed25519.pub',
                                 'id_ecdsa.pub', 'myCredential.txt', 'mySecret.txt',
                                 'apiToken.txt', 'myPassword.txt', 'myPasswd.txt',
                                 '.netrc', '.npmrc', '.pypirc', 'AUTH.JSON'])
def test_private_filename_patterns(name):
    assert sem_artifacts.private_file(name)


def test_legacy_opt_in_cannot_reach_sender(monkeypatch):
    monkeypatch.delenv('AGENT_KIT_CLOUD', raising=False)
    monkeypatch.setenv('HM_ADJUDICATE', '1')
    monkeypatch.setattr(consolidate, 'MODEL_ALLOWED', True)
    with mock.patch.object(consolidate.subprocess, 'run', side_effect=AssertionError('sender reached')):
        assert not memdir.cloud_ok()
        assert consolidate.ask_model('sample') is None
        assert hm_write.adjudicate('sample', None, []) is None
        assert sem_discover.classify([object()]) == {}
        with mock.patch.object(eval_recall, 'sample_records', side_effect=AssertionError('sampling reached')):
            assert eval_recall.generate() is None
    with mock.patch.object(embed_index.urllib.request, 'urlopen', side_effect=AssertionError('sender reached')):
        assert embed_index.embed_batch(['sample']) is None
    monkeypatch.setenv('AGENT_KIT_CLOUD', '1')
    assert memdir.cloud_ok()


def test_windows_ownership_and_cleanup(tmp_path, monkeypatch, capsys):
    python = r'C:\Python\python.exe'
    tools = str(tmp_path / 'tools')
    monkeypatch.setattr(jobs, 'windows_user', lambda: 'test')
    names = list(jobs.JOBS)
    definitions = {names[0]: jobs.windows_xml(python, tools, names[0]),
                   names[1]: jobs.windows_xml('foreign.exe', str(tmp_path / 'someone-elses-tools'), names[1])}
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if '/TN' not in command:  # the task list, which is how existence is decided
            rows = ['"\\Microsoft\\Windows\\Other","N/A","Ready"'] + ['"\\agent-kit\\%s","11/15/2026 2:00:00 AM","Ready"' % n for n in definitions]
            return SimpleNamespace(returncode=0, stdout='\n'.join(rows) + '\n', stderr='')
        name = command[command.index('/TN') + 1].split('\\')[-1]
        if '/Query' in command:
            xml = definitions.get(name)
            # a wording no code matches on: existence must come from the list, not from this text
            return SimpleNamespace(returncode=0 if xml else 1, stdout=xml or '', stderr='' if xml else 'FEHLER: Das System kann den angegebenen Pfad nicht finden.')
        if '/Create' in command:
            definitions[name] = jobs.windows_xml(python, tools, name)
        else:
            definitions.pop(name)
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(jobs.subprocess, 'run', run)
    assert jobs.run_windows(python, tools, tmp_path / 'logs', 'status', False) == 0
    output = capsys.readouterr().out
    assert names[0] + ' installed' in output
    assert names[1] + ' different task with this name' in output
    assert names[2] + ' not installed' in output
    assert jobs.run_windows(python, tools, tmp_path / 'logs', 'install', False) == 1
    assert names[1] + ' skipped: a different task with this name exists' in capsys.readouterr().out
    assert not any('/Create' in c and c[c.index('/TN') + 1].endswith(names[1]) for c in calls)
    assert not (Path(tools) / 'jobs' / (names[0] + '.xml')).exists()
    assert jobs.run_windows(python, tools, tmp_path / 'logs', 'remove', False) == 1
    assert names[1] in definitions and names[0] not in definitions
    assert not list((Path(tools) / 'jobs').glob('*.xml'))
    assert not any('/Delete' in c and c[c.index('/TN') + 1].endswith(names[1]) for c in calls)


def test_mac_ownership_and_cleanup(tmp_path, monkeypatch, capsys):
    python, tools, logs = '/test/python', tmp_path / 'tools', tmp_path / 'logs'
    directory = tmp_path / 'launch'
    directory.mkdir()
    names = list(jobs.JOBS)
    files = jobs.mac_plists(python, tools, logs)
    (directory / ('com.agent-kit.' + names[0] + '.plist')).write_text(files['com.agent-kit.' + names[0] + '.plist'], encoding='utf-8')
    foreign = directory / ('com.agent-kit.' + names[1] + '.plist')
    foreign.write_bytes(b'foreign')
    calls = []
    monkeypatch.setattr(jobs.subprocess, 'run', lambda command, **kwargs: calls.append(command) or SimpleNamespace(returncode=0, stdout='', stderr=''))
    assert jobs.run_mac(python, tools, logs, directory, 'status', False) == 0
    output = capsys.readouterr().out
    assert names[0] + ' installed' in output and names[1] + ' different task with this name' in output and names[2] + ' not installed' in output
    assert jobs.run_mac(python, tools, logs, directory, 'install', False) == 1
    assert foreign.read_bytes() == b'foreign'
    assert ['launchctl', 'unload', str(directory / ('com.agent-kit.' + names[0] + '.plist'))] not in calls
    assert jobs.run_mac(python, tools, logs, directory, 'remove', False) == 1
    assert foreign.read_bytes() == b'foreign'
    assert not (directory / ('com.agent-kit.' + names[0] + '.plist')).exists()
    assert not any(str(foreign) in command for command in calls)


def test_mac_updates_our_unloaded_job(tmp_path, monkeypatch):
    python, tools, logs = '/test/python', tmp_path / 'tools', tmp_path / 'logs'
    directory = tmp_path / 'launch'
    directory.mkdir()
    for filename, content in jobs.mac_plists(python, tools, logs).items():
        (directory / filename).write_text(content, encoding='utf-8')
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[1] == 'unload':
            return SimpleNamespace(returncode=1, stdout='', stderr='Could not find specified service')
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(jobs.subprocess, 'run', run)
    assert jobs.run_mac(python, tools, logs, directory, 'install', False) == 0
    assert not calls


@pytest.mark.parametrize('platform', ['windows', 'mac'])
def test_scheduler_errors_reported_without_traceback(tmp_path, monkeypatch, capsys, platform):
    def fail(*args, **kwargs):
        raise OSError('scheduler unavailable\nextra')
    monkeypatch.setattr(jobs.subprocess, 'run', fail)
    if platform == 'windows':
        result = jobs.run_windows('/python', tmp_path, tmp_path / 'logs', 'status', False)
    else:
        result = jobs.run_mac('/python', tmp_path, tmp_path / 'logs', tmp_path / 'launch', 'install', False)
    assert result == 1
    output = capsys.readouterr().out
    assert output.count('FAILED: scheduler unavailable') == len(jobs.JOBS)
    assert 'Traceback' not in output and 'extra' not in output
