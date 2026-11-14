"""Best-effort session checkpoint; never interrupt the host."""
import datetime
import json
import os
from pathlib import Path
import sqlite3
import subprocess


def main():
    home = Path.home()
    tools = home / '.claude' / 'memory-tools'
    tools.mkdir(parents=True, exist_ok=True)
    def git(*args):
        try:
            result = subprocess.run(['git', *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    timeout=0.2, encoding='utf-8', errors='replace')
            return result.stdout.strip() if result.returncode == 0 else ''
        except Exception:
            return ''
    entry = {'time': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'cwd': os.getcwd(),
             'branch': git('branch', '--show-current'), 'last_commit': git('log', '-1', '--pretty=%s')}
    with (tools / 'session-log.jsonl').open('a', encoding='utf-8') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    db = home / 'hivemind.db'
    if db.is_file():
        with sqlite3.connect(str(db), timeout=0.1) as con:
            con.execute('PRAGMA wal_checkpoint(PASSIVE)')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        pass
