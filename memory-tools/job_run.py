"""Run a scheduled memory job and append its output to a local log."""
import datetime
import os
from pathlib import Path
import subprocess
import sys

import memdir
from memory_jobs import JOBS


def console_python():
    executable = sys.executable
    if os.name == 'nt' and os.path.basename(executable).lower() == 'pythonw.exe':
        candidate = os.path.join(os.path.dirname(executable), 'python.exe')
        if os.path.isfile(candidate):
            return candidate
    return executable


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in JOBS:
        if sys.stderr is not None:
            print('usage: job_run.py <job>', file=sys.stderr)
        return 2
    name = args[0]
    root = Path(memdir.tools_dir())
    logs = root / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    tool, *arguments = JOBS[name]
    with (logs / (name + '.log')).open('a', encoding='utf-8') as output:
        def stamp(event):
            output.write(f'{datetime.datetime.now().isoformat(timespec="seconds")} {event}\n')
            output.flush()
        stamp('START ' + name)
        try:
            options = {'stdout': output, 'stderr': subprocess.STDOUT, 'check': False}
            if os.name == 'nt':
                options['creationflags'] = subprocess.CREATE_NO_WINDOW
            code = subprocess.run([console_python(), str(root / tool), *arguments], **options).returncode
        except OSError as exc:
            output.write(f'Failed to start: {exc}\n')
            code = 1
        stamp(f'EXIT {name} {code}')
    return code


if __name__ == '__main__':
    sys.exit(main())
