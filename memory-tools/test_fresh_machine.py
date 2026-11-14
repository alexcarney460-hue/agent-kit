"""Exercise the installed CLIs in an isolated, initially empty home."""
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest


@pytest.fixture
def installed(tmp_path):
    home = tmp_path / 'home with spaces'
    tools = home / '.claude' / 'memory-tools'
    tools.parent.mkdir(parents=True)
    shutil.copytree(Path(__file__).parent, tools, ignore=shutil.ignore_patterns('__pycache__', '*.db', '*.db-*', 'logs', '*.pyc', 'consolidation-report-latest.md', 'consolidate.log'))
    with sqlite3.connect(str(home / 'hivemind.db')) as db:
        db.executescript((tools / 'hivemind-schema.sql').read_text(encoding='utf-8'))
    db.close()
    env = os.environ.copy()
    for key in ('HM_NO_LLM', 'HM_NO_SEM', 'AGENT_KIT_CLOUD', 'HM_ADJUDICATE', 'HIVEMIND_DB', 'SEM_DB_PATH', 'CLAUDE_MEMORY_DIR'):
        env.pop(key, None)
    env.update(HOME=str(home), USERPROFILE=str(home))
    return home, tools, env


def run(tools, env, *args):
    result = subprocess.run([sys.executable, str(tools / args[0]), *map(str, args[1:])],
                            env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
    assert result.returncode == 0, (args, result.returncode, result.stdout, result.stderr)
    assert 'Traceback (most recent call last)' not in result.stdout + result.stderr, (args, result.stdout, result.stderr)


def test_fresh_and_populated(installed):
    home, tools, env = installed
    for iteration in range(2):
        if iteration:
            for n in range(10):
                run(tools, env, 'hm_write.py', 'context', 'Fixture memory number ' + str(n))
        commands = [
            ('mem_recall.py', 'q'), ('hm_write.py', 'context', 'Fixture memory'),
            ('mem_check.py',), ('sem.py', 'seed'), ('sem_refresh.py',),
            ('sem_backfill.py',), ('sem_discover.py',), ('sem_artifacts.py',),
            ('alias_propose.py',), ('consolidate.py',), ('consolidate.py', '--apply'),
            ('memory_index_compact.py',), ('memory_index_compact.py', '--apply'),
            ('memory_backup_local.py', 'run'), ('memory_backup_local.py', 'verify'),
            ('memory_backup_local.py', 'list'), ('memory_backup_local.py', 'restore', home / ('restore-' + str(iteration))),
            ('memory_jobs.py', 'status'), ('memory_jobs.py', 'install', '--dry-run')]
        for command in commands:
            run(tools, env, *command)


def test_corrupt_database_fails_health_check(installed):
    home, tools, env = installed
    (home / 'hivemind.db').write_bytes(b'not a database')
    result = subprocess.run([sys.executable, str(tools / 'mem_check.py')], env=env, capture_output=True, text=True)
    assert result.returncode != 0
