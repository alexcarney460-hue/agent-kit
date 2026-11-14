"""Regression tests for memory paths, model isolation, and job execution."""
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest import mock
import xml.etree.ElementTree as ET

import pytest
import consolidate
import job_run
import memdir
import memory_jobs
import model_cli
import sem_artifacts


@pytest.mark.parametrize('home,encoded', [
    (r'C:\Users\Jane Doe', 'C--Users-Jane-Doe'),
    (r'C:\Users\j.doe', 'C--Users-j-doe'),
    ('/home/a_b', '-home-a-b'),
])
def test_memory_dir_encoding(monkeypatch, home, encoded):
    monkeypatch.delenv('CLAUDE_MEMORY_DIR', raising=False)
    monkeypatch.setattr(memdir, 'home', lambda: home)
    monkeypatch.setattr(memdir.os.path, 'exists', lambda p: False)
    monkeypatch.setattr(memdir.os.path, 'isdir', lambda p: False)
    assert memdir.memory_dir() == os.path.join(home, '.claude', 'projects', encoded, 'memory')


def test_memory_dir_legacy_and_override(tmp_path, monkeypatch):
    home = tmp_path / 'a_b'
    monkeypatch.setattr(memdir, 'home', lambda: str(home))
    monkeypatch.delenv('CLAUDE_MEMORY_DIR', raising=False)
    old = home / '.claude' / 'projects' / str(home).replace('\\', '-').replace('/', '-').replace(':', '-') / 'memory'
    modern = home / '.claude' / 'projects' / __import__('re').sub(r'[^A-Za-z0-9]', '-', str(home))
    assert memdir.memory_dir() == str(modern / 'memory')          # nothing exists yet: the name Claude Code uses
    old.mkdir(parents=True)
    assert memdir.memory_dir() == str(modern / 'memory')          # an empty old folder holds nothing worth keeping
    (old / 'note.md').write_text('x', encoding='utf-8')
    assert memdir.memory_dir() == str(old)                        # notes live in the old folder
    modern.mkdir()                                                # Claude Code creates its project folder by itself
    assert memdir.memory_dir() == str(old)                        # ...which must not hide those notes
    (modern / 'memory').mkdir(); (modern / 'memory' / 'new.md').write_text('y', encoding='utf-8')
    assert memdir.memory_dir() == str(modern / 'memory')          # once the new folder has notes, it is used
    monkeypatch.setenv('CLAUDE_MEMORY_DIR', str(tmp_path / 'chosen'))
    assert memdir.memory_dir() == str(tmp_path / 'chosen')
    monkeypatch.setenv('CLAUDE_MEMORY_DIR', str(tmp_path / 'custom'))
    assert memdir.memory_dir() == str(tmp_path / 'custom')


def test_launcher_opt_in_and_flags(monkeypatch):
    child = mock.Mock(return_value=SimpleNamespace(stdout='answer', returncode=0))
    monkeypatch.setattr(model_cli.subprocess, 'run', child)
    monkeypatch.setattr(model_cli.shutil, 'which', lambda _: '/bin/claude')
    monkeypatch.delenv('AGENT_KIT_CLOUD', raising=False)
    assert model_cli.run_model('private prompt', 7) is None
    child.assert_not_called()
    monkeypatch.setenv('AGENT_KIT_CLOUD', '1')
    assert model_cli.run_model('private prompt', 7) == 'answer'
    args = child.call_args.args[0]
    for flag in ('--tools', '--strict-mcp-config', '--mcp-config', '--disable-slash-commands',
                 '--no-session-persistence', '--setting-sources', '--settings'):
        assert flag in args
    assert args[args.index('--tools') + 1] == ''
    assert 'private prompt' not in args
    assert child.call_args.kwargs['input'] == 'private prompt'
    assert child.call_args.kwargs['timeout'] == 7


def test_cmd_launcher_and_missing(monkeypatch):
    monkeypatch.setenv('AGENT_KIT_CLOUD', '1')
    child = mock.Mock(return_value=SimpleNamespace(stdout='ok', returncode=0))
    monkeypatch.setattr(model_cli.subprocess, 'run', child)
    monkeypatch.setattr(model_cli.shutil, 'which', lambda _: r'C:\Program Files\claude.cmd')
    assert model_cli.run_model('secret', 3) == 'ok'
    args = child.call_args.args[0]
    assert isinstance(args, str) and args.startswith('cmd /d /s /c ""C:\\Program Files\\claude.cmd" -p --tools "" ')
    assert args.endswith('"') and 'secret' not in args
    assert child.call_args.kwargs['encoding'] == 'utf-8'
    assert child.call_args.kwargs['input'] == 'secret'
    monkeypatch.setattr(model_cli.shutil, 'which', lambda _: None)
    monkeypatch.setattr(model_cli.os.path, 'isfile', lambda _: False)
    child.reset_mock()
    assert model_cli.run_model('secret', 3) is None
    child.assert_not_called()


def test_retry_only_start_failure(monkeypatch):
    statuses = iter(['not_started', 'ran'])
    def run(prompt, timeout, callback):
        result = next(statuses)
        callback(result)
        return '{"ok": true}' if result == 'ran' else None
    monkeypatch.setattr(consolidate.model_cli, 'run_model', run)
    monkeypatch.setattr(consolidate, '_pause', lambda seconds: None)
    monkeypatch.setattr(consolidate, 'log', lambda message: None)
    consolidate.reset_llm_stats()
    assert consolidate._start_model('x', 1) == ('ran', '{"ok": true}')
    assert consolidate.llm_stats()['llm_wait_s'] == consolidate.LLM_RETRY_WAIT_S


def test_hard_link_rejected(tmp_path):
    root = tmp_path / 'Desktop'
    root.mkdir()
    original = tmp_path / 'secret'
    original.write_text('private', encoding='utf-8')
    linked = root / 'document.txt'
    try:
        os.link(original, linked)
    except (OSError, NotImplementedError):
        pytest.skip('hard links unavailable')
    assert sem_artifacts.read_safe(str(linked), str(root)) == ''
    normal = root / 'normal.txt'
    normal.write_text('public', encoding='utf-8')
    assert sem_artifacts.read_safe(str(normal), str(root)) == 'public'


def test_windows_command_and_ownership(monkeypatch):
    python = r'C:\Python\python.exe'
    monkeypatch.setattr(memory_jobs.os.path, 'isfile', lambda p: p == r'C:\Python\pythonw.exe')
    chosen = memory_jobs.windows_python(python)
    assert chosen == r'C:\Python\pythonw.exe'
    xml = memory_jobs.windows_xml(chosen, r'C:\My Tools', 'backup', user='test')
    root = ET.fromstring(xml)
    ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
    assert root.find('.//t:Command', ns).text == chosen
    assert root.find('.//t:StartWhenAvailable', ns).text == 'true'
    monkeypatch.setattr(memory_jobs, 'windows_exists', lambda task: True)
    monkeypatch.setattr(memory_jobs.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=xml, stderr=''))
    assert memory_jobs.windows_state('agent-kit\\backup', chosen, r'C:\My Tools', 'backup') == 'installed'
    # another interpreter (a Python upgrade): still the kit's own job, so it can be removed
    assert memory_jobs.windows_state('agent-kit\\backup', python, r'C:\My Tools', 'backup') == 'installed'
    # another tools folder or job name: not ours
    assert memory_jobs.windows_state('agent-kit\\backup', chosen, r'C:\Other Tools', 'backup') == 'different task with this name'
    assert memory_jobs.windows_state('agent-kit\\backup', chosen, r'C:\My Tools', 'compact') == 'different task with this name'
    monkeypatch.setattr(memory_jobs.os.path, 'isfile', lambda p: False)
    assert memory_jobs.windows_python(python) == python


def test_windowless_job_runner(tmp_path, monkeypatch):
    import ntpath
    monkeypatch.setattr(job_run, 'os', SimpleNamespace(name='nt', path=ntpath))
    monkeypatch.setattr(job_run.sys, 'executable', r'C:\Python\pythonw.exe')
    monkeypatch.setattr(job_run.sys, 'stdout', None)
    monkeypatch.setattr(job_run.sys, 'stderr', None)
    monkeypatch.setattr(ntpath, 'isfile', lambda p: p == r'C:\Python\python.exe')
    monkeypatch.setattr(job_run.memdir, 'tools_dir', lambda: str(tmp_path))
    monkeypatch.setattr(job_run.subprocess, 'CREATE_NO_WINDOW', 0x08000000, raising=False)
    child = mock.Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(job_run.subprocess, 'run', child)
    assert job_run.main(['backup']) == 0
    assert child.call_args.args[0][0] == r'C:\Python\python.exe'
    assert child.call_args.kwargs['creationflags'] == 0x08000000
    assert 'EXIT backup 0' in (tmp_path / 'logs' / 'backup.log').read_text()
    assert job_run.main([]) == 2
