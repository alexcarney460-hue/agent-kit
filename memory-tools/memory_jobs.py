"""Manage optional local scheduled memory jobs."""
import argparse
import getpass
import os
import ntpath
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from xml.parsers.expat import ExpatError
import memdir

JOBS = {'backup': ('memory_backup_local.py', 'run'),
        'compact': ('memory_index_compact.py', '--apply'),
        'consolidate': ('consolidate.py', '--apply'),
        'semantic-refresh': ('sem_refresh.py',)}
TIMES = {'backup': (2, 0), 'compact': (2, 20), 'consolidate': (2, 40), 'semantic-refresh': (3, 20)}


def windows_user():
    """The account Task Scheduler should run the jobs as: the person running this command.

    `whoami` gives the name Windows itself resolves (machine\\user or domain\\user). The
    USERDOMAIN variable is not used: in some sessions it holds a workgroup name that does
    not resolve to an account.
    """
    if os.name == 'nt':
        try:
            out = subprocess.run(['whoami'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=15).stdout.strip()
            if out:
                return out
        except (OSError, subprocess.TimeoutExpired):
            pass
    return os.environ.get("USERNAME") or getpass.getuser()


def windows_python(python):
    """Use the windowless sibling when it is present."""
    directory = ntpath.dirname(str(python))
    candidate = ntpath.join(directory, 'pythonw.exe')
    return candidate if os.path.isfile(candidate) else str(python)


def windows_xml(python, tools, name, user=None):
    ns = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ET.register_namespace("", ns)
    def child(parent, tag, value=None):
        element = ET.SubElement(parent, "{" + ns + "}" + tag)
        if value is not None:
            element.text = value
        return element
    root = ET.Element("{" + ns + "}Task", version="1.2")
    triggers = child(root, "Triggers")
    trigger = child(triggers, "CalendarTrigger")
    hour, minute = TIMES[name]
    child(trigger, "StartBoundary", f"2026-01-01T{hour:02}:{minute:02}:00")
    child(trigger, "Enabled", "true")
    child(child(trigger, "ScheduleByDay"), "DaysInterval", "1")
    principals = child(root, "Principals")
    principal = child(principals, "Principal")
    principal.set("id", "Author")
    child(principal, "UserId", user or windows_user())
    child(principal, "LogonType", "InteractiveToken")
    child(principal, "RunLevel", "LeastPrivilege")
    settings = child(root, "Settings")
    for key, value in (("MultipleInstancesPolicy", "IgnoreNew"),
                       ("DisallowStartIfOnBatteries", "false"),
                       ("StopIfGoingOnBatteries", "false"),
                       ("StartWhenAvailable", "true"),  # a laptop asleep at the set time runs the job when it next can
                       ("ExecutionTimeLimit", "PT2H"), ("Enabled", "true")):
        child(settings, key, value)
    actions = child(root, "Actions")
    actions.set("Context", "Author")
    execute = child(actions, "Exec")
    child(execute, "Command", str(python))
    child(execute, "Arguments", subprocess.list2cmdline([ntpath.join(str(tools), "job_run.py"), name]))
    return ET.tostring(root, encoding="unicode", xml_declaration=False)


def windows_commands(python, tools, logs, action):
    commands = []
    for name in JOBS:
        task = 'agent-kit\\' + name
        if action == 'install':
            xml_path = ntpath.join(str(tools), 'jobs', name + '.xml')
            commands.append(['schtasks', '/Create', '/TN', task, '/XML', xml_path])
        else:
            commands.append(['schtasks', '/Query' if action == 'status' else '/Delete', '/TN', task] + (['/F'] if action == 'remove' else []))
    return commands


def mac_plists(python, tools, logs):
    result = {}
    for name, args in JOBS.items():
        hour, minute = TIMES[name]
        label = 'com.agent-kit.' + name
        data = {'Label': label, 'ProgramArguments': [python, str(Path(tools) / 'job_run.py'), name],
                'StartCalendarInterval': {'Hour': hour, 'Minute': minute},
                'StandardOutPath': str(Path(logs) / (name + '.log')),
                'StandardErrorPath': str(Path(logs) / (name + '.err.log'))}
        result[label + '.plist'] = plistlib.dumps(data).decode('utf-8')
    return result


def linux_lines(python, tools, logs):
    result = []
    for name, args in JOBS.items():
        hour, minute = TIMES[name]
        cmd = ' '.join(shlex.quote(x) for x in [python, str(Path(tools) / 'job_run.py'), name])
        result.append(f'{minute} {hour} * * * {cmd}')
    return result


def windows_exists(task):
    """Is a task with this name registered? Reads the task list, so it does not depend on
    the wording or language of a "not found" message."""
    done = subprocess.run(['schtasks', '/Query', '/FO', 'CSV', '/NH'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='replace')
    if done.returncode:
        raise RuntimeError((done.stderr or done.stdout).strip() or 'scheduler list failed')
    wanted = ('\\' + task).lower()
    for line in done.stdout.splitlines():
        first = line.split('","')[0].strip().strip('"').lower()
        if first == wanted:
            return True
    return False


def windows_state(task, python, tools, name):
    if not windows_exists(task):
        return 'not installed'
    done = subprocess.run(['schtasks', '/Query', '/TN', task, '/XML'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='replace')
    if done.returncode:
        raise RuntimeError((done.stderr or done.stdout).strip() or 'scheduler query failed')
    try:
        root = ET.fromstring(done.stdout)
        ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        actions = root.findall('./t:Actions/*', ns)
        command = actions[0].findtext('t:Command', namespaces=ns) if len(actions) == 1 else None
        arguments = actions[0].findtext('t:Arguments', namespaces=ns) if len(actions) == 1 else None
        expected = subprocess.list2cmdline([ntpath.join(str(tools), 'job_run.py'), name])
        # Ownership is the runner in THIS tools folder plus the job name. The interpreter is not
        # compared: after a Python upgrade the task still belongs to the kit and can be removed.
        if actions and actions[0].tag.endswith('}Exec') and command and arguments == expected:
            return 'installed'
    except (ET.ParseError, IndexError):
        pass
    return 'different task with this name'


def windows_helper_content(python, tools, name):
    return '<?xml version="1.0" encoding="UTF-16"?>\n' + windows_xml(python, tools, name)


def remove_windows_helper(path, content, name):
    if path.is_symlink():
        print(name, 'helper left: not the installed content')
    elif path.exists():
        try:
            matches = path.read_bytes() == content.encode('utf-16')
        except OSError:
            matches = False
        if matches:
            path.unlink()
        else:
            print(name, 'helper left: not the installed content')


def run_windows(python, tools, logs, action, dry_run):
    """Returns 0 when every job ended in the state asked for, 1 otherwise. No tracebacks for the owner."""
    failed = 0
    for name, command in zip(JOBS, windows_commands(python, tools, logs, action)):
        task = command[command.index('/TN') + 1]
        try:
            if dry_run and action != 'status':
                print(name, 'dry run:', subprocess.list2cmdline(command))
                continue
            state = windows_state(task, python, tools, name)
            if action == 'status':
                print(name, state)
            elif state == 'different task with this name':
                print(name, 'skipped: a different task with this name exists')
                failed = 1
            elif action == 'install' and state == 'installed':
                print(name, 'already installed, left as it is (run remove first to re-create it)')
            elif action == 'remove' and state == 'not installed':
                remove_windows_helper(Path(tools) / 'jobs' / (name + '.xml'),
                                      windows_helper_content(python, tools, name), name)
                print(name, state)
            else:
                xml_path = Path(tools) / 'jobs' / (name + '.xml')
                if action == 'install':
                    xml_path.parent.mkdir(parents=True, exist_ok=True)
                    content = windows_helper_content(python, tools, name)
                    if xml_path.is_symlink() or (xml_path.exists() and xml_path.read_bytes() != content.encode('utf-16')):
                        raise RuntimeError('helper exists with different content; left as it is')
                    if not xml_path.exists():
                        with xml_path.open('xb') as helper:
                            helper.write(content.encode('utf-16'))
                done = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='replace')
                if done.returncode:
                    raise RuntimeError((done.stderr or done.stdout).strip() or 'scheduler command failed')
                if action == 'remove':
                    remove_windows_helper(xml_path, windows_helper_content(python, tools, name), name)
                print(name, 'installed' if action == 'install' else 'removed')
        except (OSError, RuntimeError, UnicodeError, subprocess.SubprocessError) as exc:
            failed = 1
            print(name, 'FAILED:', str(exc).splitlines()[0] if str(exc) else 'scheduler error')
    return failed


def run_mac(python, tools, logs, directory, action, dry_run):
    failed = 0
    for filename, content in mac_plists(python, tools, logs).items():
        name = filename[len('com.agent-kit.'):-len('.plist')]
        path = directory / filename
        try:
            if path.is_symlink():
                state = 'different task with this name'
            elif not path.exists():
                state = 'not installed'
            else:
                try:
                    with path.open('rb') as stream:
                        data = plistlib.load(stream)
                    expected = [python, str(Path(tools) / 'job_run.py'), name]
                    arguments = data.get('ProgramArguments')
                    state = 'installed' if isinstance(arguments, list) and len(arguments) == len(expected) and arguments[1:] == expected[1:] else 'different task with this name'
                except (ValueError, TypeError, AttributeError, plistlib.InvalidFileException, ExpatError):
                    state = 'different task with this name'
            if action == 'status':
                print(name, state)
            elif state == 'different task with this name':
                print(name, 'skipped: a different task with this name exists')
                failed = 1
            elif action == 'install' and state == 'installed':
                print(name, 'already installed, left as it is (run remove first to re-create it)')
            elif action == 'remove' and state == 'not installed':
                print(name, state)
            elif dry_run:
                print(name, 'dry run:', action)
            else:
                if action == 'install':
                    directory.mkdir(parents=True, exist_ok=True)
                    logs.mkdir(parents=True, exist_ok=True)
                    with path.open('x', encoding='utf-8') as stream:
                        stream.write(content)
                done = subprocess.run(['launchctl', 'load' if action == 'install' else 'unload', str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='replace')
                if done.returncode:
                    raise RuntimeError((done.stderr or done.stdout).strip() or 'launchctl failed')
                if action == 'remove':
                    path.unlink()
                print(name, 'installed' if action == 'install' else 'removed')
        except (OSError, RuntimeError, UnicodeError, subprocess.SubprocessError) as exc:
            failed = 1
            print(name, 'FAILED:', str(exc).splitlines()[0] if str(exc) else 'scheduler error')
    return failed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'status', 'remove'))
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    tools = Path(memdir.tools_dir())
    logs = tools / 'logs'
    python = os.path.abspath(sys.executable)
    if sys.platform.startswith('linux'):
        print('Linux: no changes made; suggested crontab entries:')
        print('\n'.join(linux_lines(python, tools, logs)))
        return 0
    if sys.platform == 'darwin':
        return run_mac(python, tools, logs, Path.home() / 'Library/LaunchAgents', args.action, args.dry_run)
    if os.name == 'nt':
        return run_windows(windows_python(python), tools, logs, args.action, args.dry_run)
    parser.error('unsupported platform')


if __name__ == '__main__':
    sys.exit(main())
