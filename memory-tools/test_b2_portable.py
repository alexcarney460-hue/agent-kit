from contextlib import closing
import datetime
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from unittest import mock
import pytest

import consolidate
import sem_artifacts
import sem_discover
import memory_backup_local as backup
import memory_index_compact as compact
import memory_jobs as jobs


def test_model_gates(monkeypatch):
    monkeypatch.setattr(consolidate, 'MODEL_ALLOWED', True)
    monkeypatch.setattr(consolidate.memdir, 'cloud_ok', lambda: False)
    with mock.patch.object(consolidate.subprocess, 'run', side_effect=AssertionError('model called')):
        assert consolidate.ask_model('private') is None
    with mock.patch.object(sem_discover.model_cli, 'run_model', side_effect=AssertionError('model called')):
        assert sem_discover.classify([object()]) == {}


def test_discovery_dry_run_never_starts_model(monkeypatch):
    candidate = SimpleNamespace(phrase='Example Entity', score=lambda: 8)
    conn = mock.MagicMock()
    conn.execute.return_value.fetchone.return_value = None
    monkeypatch.setattr(sem_discover.sem, 'connect', lambda: conn)
    monkeypatch.setattr(sem_discover, 'discover', lambda _conn: [candidate])
    monkeypatch.setattr(sem_discover.memdir, 'cloud_ok', lambda: True)
    monkeypatch.setattr(sys, 'argv', ['sem_discover.py', '--dry-run'])
    with mock.patch.object(sem_discover.model_cli, 'run_model', side_effect=AssertionError('model called')):
        sem_discover.main()


def test_scanner_refuses_credential_links(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    desktop = tmp_path / 'Desktop'
    desktop.mkdir()
    keys = tmp_path / 'keys'
    keys.mkdir()
    (keys / 'secret.md').write_text('secret', encoding='utf-8')
    (desktop / 'safe.md').write_text('safe', encoding='utf-8')
    try:
        (desktop / 'link.md').symlink_to(keys / 'secret.md')
        (desktop / 'linked-dir').symlink_to(keys, target_is_directory=True)
    except OSError:
        pytest.skip('platform refused symlink creation')
    assert [Path(p).name for p, _ in sem_artifacts.walk(str(desktop), 4)] == ['safe.md']
    assert not sem_artifacts.safe_path(str(keys / 'secret.md'), str(keys))


def test_backup_roundtrip_and_overwrite(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('USERPROFILE', str(home))
    monkeypatch.setenv('SEM_DB_PATH', str(home / '.claude/memory-tools/semantic.db'))
    for path in (home / 'hivemind.db', home / '.claude/memory-tools/semantic.db'):
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(path))) as db, db:
            db.execute('create table sample (value text)')
            db.execute("insert into sample values ('ok')")
    memories = home / 'files'
    memories.mkdir()
    (memories / 'note.md').write_text('note', encoding='utf-8')
    monkeypatch.setenv('CLAUDE_MEMORY_DIR', str(memories))
    dest = tmp_path / 'backups'
    backup.run(dest, 14)
    archive = backup.backups(dest)[0]
    backup.verify(archive)
    out = tmp_path / 'out'
    backup.restore(archive, out, False)
    assert (out / 'memories/note.md').read_text(encoding='utf-8') == 'note'
    with pytest.raises(FileExistsError):
        backup.restore(archive, out, False)


def test_job_builders():
    windows = jobs.windows_commands('C:/Program Files/Python/python.exe', 'C:/My Tools', 'C:/My Logs', 'install')
    import xml.etree.ElementTree as ET
    assert len(windows) == 4 and all('/Create' in command and '/XML' in command for command in windows)
    xml = jobs.windows_xml('C:/Program Files/Python/python.exe', 'C:/My Tools & Data', 'backup')
    root = ET.fromstring(xml)
    ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
    expected = {'StartWhenAvailable': 'true', 'DisallowStartIfOnBatteries': 'false',
                'StopIfGoingOnBatteries': 'false', 'ExecutionTimeLimit': 'PT2H',
                'MultipleInstancesPolicy': 'IgnoreNew', 'LogonType': 'InteractiveToken'}
    for tag, value in expected.items():
        assert root.find('.//t:' + tag, ns).text == value
    assert root.find('.//t:Exec/t:Command', ns).text == 'C:/Program Files/Python/python.exe'
    assert 'My Tools & Data' in root.find('.//t:Exec/t:Arguments', ns).text
    plists = jobs.mac_plists('/a b/python', '/a b/tools', '/a b/logs')
    assert len(plists) == 4 and '/a b/python' in next(iter(plists.values()))
    lines = jobs.linux_lines('/a b/python', '/a b/tools', '/a b/logs')
    assert len(lines) == 4 and "'/a b/python'" in lines[0]


def test_compaction_missing_and_keep(tmp_path):
    result = compact.compact(tmp_path, datetime.date.today(), apply=True)
    assert result['skipped'] == 'missing index'
    entry = compact.parse_entry('- [A](a.md) — KEEP: never move', datetime.date.today())
    assert entry.exempt


def test_windows_home_with_spaces_is_quoted():
    command = jobs.windows_commands(r'C:\Users\Jane Doe\Python\python.exe',
                                    r'C:\Users\Jane Doe\.claude\memory-tools',
                                    r'C:\Users\Jane Doe\.claude\memory-tools\logs', 'install')[0]
    assert command[command.index('/XML') + 1].endswith(r'jobs\backup.xml')
    import xml.etree.ElementTree as ET
    root = ET.fromstring(jobs.windows_xml(r'C:\Users\Jane Doe\Python\python.exe',
                                          r'C:\Users\Jane Doe\.claude\memory-tools', 'backup'))
    ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
    assert root.find('.//t:Exec/t:Command', ns).text == r'C:\Users\Jane Doe\Python\python.exe'
    assert root.find('.//t:Exec/t:Arguments', ns).text == r'"C:\Users\Jane Doe\.claude\memory-tools\job_run.py" backup'


def test_artifact_open_refuses_swapped_file(tmp_path, monkeypatch):
    root = tmp_path / 'Desktop'
    root.mkdir()
    file = root / 'note.md'
    file.write_text('public', encoding='utf-8')
    replacement = root / 'replacement.md'
    replacement.write_text('private', encoding='utf-8')
    real_open = os.open
    def swapped(path, flags, *args, **kwargs):
        os.replace(str(replacement), str(file))
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(sem_artifacts.os, 'open', swapped)
    assert sem_artifacts.read_safe(str(file), str(root)) == ''


def test_refresh_lock_excludes_second_instance(tmp_path):
    import sem_refresh
    path = tmp_path / 'refresh.lock'
    with sem_refresh.refresh_lock(path):
        with pytest.raises(BlockingIOError):
            with sem_refresh.refresh_lock(path):
                pass
    with sem_refresh.refresh_lock(path):
        pass


def test_consolidation_failure_returns_nonzero(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['consolidate.py'])
    monkeypatch.setattr(consolidate, 'connect', mock.Mock(side_effect=OSError('fixture')))
    assert consolidate.main() == 1


def test_skipped_model_phases_are_counted_without_candidates(monkeypatch):
    monkeypatch.setattr(consolidate, 'MODEL_ALLOWED', False)
    consolidate.reset_llm_stats()
    for name in ('ensure_schema', 'phase_hm', 'phase_distill'):
        monkeypatch.setattr(consolidate, name, lambda *args: {})
    monkeypatch.setattr(consolidate.mem_check, 'phase_check', lambda *args: {}, raising=False)
    consolidate.run_phases((None, None, None), ['hm', 'distill', 'check'], False)
    assert consolidate.llm_stats()['llm_skipped'] == 3


def test_discovery_dependency_rules_do_not_retire_supported_entities():
    base = {'support': 0, 'extra_aliases': [], 'edges': 0, 'created_at': '2000-01-01'}
    assert sem_discover.safe_to_retire(base)
    for change in ({'support': 1}, {'extra_aliases': ['manual']}, {'edges': 1},
                   {'created_at': datetime.datetime.now().isoformat()}):
        assert not sem_discover.safe_to_retire({**base, **change})


def test_backup_collision_preserves_existing_archive(tmp_path, monkeypatch):
    class FixedDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, tzinfo=tz)
    monkeypatch.setattr(backup.datetime, 'datetime', FixedDateTime)
    monkeypatch.setattr(backup.uuid, 'uuid4', lambda: SimpleNamespace(hex='fixed'))
    existing = tmp_path / 'memory-20260101T000000000000Z-fixed.zip'
    existing.write_bytes(b'existing')
    with pytest.raises(FileExistsError):
        backup.run(tmp_path, 14)
    assert existing.read_bytes() == b'existing'


def test_windows_dry_run_prints_xml_without_writing(tmp_path, capsys, monkeypatch):
    logs = tmp_path / 'not-created' / 'logs'
    monkeypatch.setattr(jobs, 'windows_user', lambda: 'TESTPC\\jane')  # the account lookup is not the scheduler
    monkeypatch.setattr(jobs.subprocess, 'run', lambda *args, **kwargs: pytest.fail('scheduler executed'))
    jobs.run_windows(r'C:\Program Files\Python\python.exe', r'C:\My Tools', logs, 'install', True)
    output = capsys.readouterr().out
    assert output.count('schtasks /Create') == 4
    assert not logs.exists()


def test_job_runner_appends_start_and_exit(tmp_path, monkeypatch):
    import job_run
    monkeypatch.setattr(job_run.memdir, 'tools_dir', lambda: str(tmp_path))
    (tmp_path / 'memory_backup_local.py').write_text('import sys\nprint("fixture output")\nsys.exit(7)\n', encoding='utf-8')
    assert job_run.main(['backup']) == 7
    log = (tmp_path / 'logs' / 'backup.log').read_text(encoding='utf-8')
    assert 'START backup' in log and 'fixture output' in log and 'EXIT backup 7' in log
