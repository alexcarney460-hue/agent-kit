"""Run one local semantic refresh at a time, with an append-only log."""
from contextlib import contextmanager
import datetime
import os
from pathlib import Path
import subprocess
import sys

import memdir


@contextmanager
def refresh_lock(path):
    """A process-held advisory lock, released by the OS even after a crash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BlockingIOError('refresh already running') from exc
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == 'nt':
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def main():
    root = Path(memdir.tools_dir())
    logs = root / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    try:
        with refresh_lock(logs / 'sem-refresh.lock'):
            with (logs / 'sem-refresh.log').open('a', encoding='utf-8') as log:
                def run(*step):
                    log.write(f'{datetime.datetime.now().isoformat(timespec="seconds")} START {step[0]}\n')
                    log.flush()
                    result = subprocess.run([sys.executable, str(root / step[0]), *step[1:]],
                                            stdout=log, stderr=subprocess.STDOUT, check=False)
                    log.write(f'{datetime.datetime.now().isoformat(timespec="seconds")} EXIT {step[0]} {result.returncode}\n')
                    log.flush()
                    return result.returncode

                preflight = run('sem_backfill.py', '--preflight')
                if preflight in (1, 5, 6):
                    print(f'refresh: preflight failed ({preflight}); see {log.name}')
                    return 1
                failed = preflight != 0
                prune = run('sem.py', 'prune')
                failed |= prune != 0
                if not prune:
                    failed |= run('sem_discover.py') != 0
                    failed |= run('sem_backfill.py', '--entry', 'scheduled') != 0
                    failed |= run('sem_artifacts.py') != 0
                if memdir.cloud_ok():
                    failed |= run('embed_index.py', '--update') != 0
                else:
                    log.write('embedding skipped: cloud permission is off\n')
                failed |= run('alias_propose.py') != 0
                log.write('DONE ' + ('degraded' if failed else 'ok') + '\n')
                print('refresh:', 'degraded' if failed else 'ok', 'log:', log.name)
                return int(failed)
    except BlockingIOError:
        print('refresh skipped: another refresh is running')
        return 0


if __name__ == '__main__':
    sys.exit(main())
