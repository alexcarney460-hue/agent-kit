"""Run an explicitly opted-in model request without interactive capabilities."""
import os
from pathlib import Path
import shutil
import subprocess

import memdir


def run_model(prompt, timeout, on_status=None):
    """Return model output or None. Report a failure category to an optional callback."""
    def status(value):
        if on_status is not None:
            try:
                on_status(value)
            except Exception:
                pass
    try:
        if not memdir.cloud_ok():
            status('disabled')
            return None
        binary = shutil.which('claude')
        if not binary:
            fallback = os.path.expanduser('~/.local/bin/claude')
            binary = fallback if os.path.isfile(fallback) else None
        if not binary:
            status('not_started')
            return None
        root = Path(__file__).resolve().parent
        args = [binary, '-p', '--tools', '', '--strict-mcp-config',
                '--mcp-config', str(root / 'model-mcp.json'),
                '--disable-slash-commands', '--no-session-persistence',
                '--setting-sources', '', '--settings', str(root / 'model-settings.json')]
        if binary.lower().endswith(('.cmd', '.bat')):
            # A batch shim needs cmd.exe. The whole line is handed over as ONE string: given a
            # list, Python would quote the already-quoted inner line a second time and cmd.exe
            # would not find the program.
            args = 'cmd /d /s /c "' + subprocess.list2cmdline(args) + '"'
        result = subprocess.run(args, input=prompt, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
        if result.returncode:
            status('exit')
            return None
        status('ran')
        return result.stdout
    except subprocess.TimeoutExpired:
        status('timeout')
    except Exception:
        status('not_started')
    return None
