"""Create and inspect local memory backups; restore only to a named directory.

Each database is snapshotted consistently on its own; databases and files are
collected a moment apart, not at a single shared point in time.
"""
import argparse
from contextlib import closing
import datetime
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import tempfile
import uuid
import zipfile
import memdir

BACKUP_NAME = re.compile(r'memory-[0-9]{8}T[0-9]{12}Z-[0-9a-f]{32}\.zip\Z')
MARKER = 'agent-kit-backup.json'


def sources():
    home = Path.home()
    return {'hivemind.db': Path(memdir.hivemind_db()),
            'semantic.db': Path(memdir.semantic_db()),
            'primer.md': home / '.claude/primer.md',
            'lessons.md': home / '.claude/lessons.md'}


def backups(dest):
    return sorted((p for p in Path(dest).glob('memory-*.zip') if is_kit_backup(p)),
                  key=lambda p: p.name, reverse=True)


def is_kit_backup(path):
    if not BACKUP_NAME.fullmatch(Path(path).name) or Path(path).is_symlink():
        return False
    try:
        with zipfile.ZipFile(path) as archive:
            return MARKER in archive.namelist()
    except (OSError, zipfile.BadZipFile, ValueError):
        return False


def require_kit_backup(path):
    if not is_kit_backup(path):
        raise ValueError('not a kit backup')


def snapshot(db, target):
    with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(str(target))) as dst:
            src.backup(dst)


def run(dest, keep):
    if keep < 1:
        raise ValueError('--keep must be at least 1')
    dest.mkdir(parents=True, exist_ok=True)
    created_at = datetime.datetime.now(datetime.timezone.utc)
    name = 'memory-' + created_at.strftime('%Y%m%dT%H%M%S%fZ') + '-' + uuid.uuid4().hex + '.zip'
    path = dest / name
    created = False
    try:
        with tempfile.TemporaryDirectory() as tmp, path.open('xb') as out:
            created = True
            with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(MARKER, json.dumps({'kit': 'agent-kit', 'format_version': 1,
                                                    'created_at': created_at.isoformat()}))
                _write_archive(archive, Path(tmp))
        verify(path)
    except Exception:
        if created:
            path.unlink(missing_ok=True)
        raise
    for old in backups(dest)[keep:]:
        old.unlink()
    print('created', path)


def _write_archive(archive, tmp):
    for name, source in sources().items():
        if source.is_file():
            if name.endswith('.db'):
                target = tmp / name
                snapshot(source, target)
                archive.write(target, name)
            else:
                archive.write(source, name)
    root = Path(memdir.memory_dir())
    if root.is_dir():
        for file in sorted(root.rglob('*')):
            if file.is_file() and not file.is_symlink() and root.resolve() in file.resolve().parents:
                archive.write(file, 'memories/' + file.relative_to(root).as_posix())


def verify(path):
    require_kit_backup(path)
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(path) as archive:
        bad = archive.testzip()
        if bad:
            raise ValueError('corrupt archive member: ' + bad)
        for name in ('hivemind.db', 'semantic.db'):
            if name in archive.namelist():
                target = Path(tmp) / name
                target.write_bytes(archive.read(name))
                with closing(sqlite3.connect(str(target))) as conn:
                    result = conn.execute('PRAGMA integrity_check').fetchall()
                if result != [('ok',)]:
                    raise ValueError(name + ' integrity check failed')
                print(name, 'integrity_check: ok')
    print('verified', path)


def restore(path, target, force):
    require_kit_backup(path)
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        for item in members:
            parts = PurePosixPath(item.filename).parts
            if not parts or item.filename.startswith('/') or any(p in ('..', '.') for p in parts) or '\\' in item.filename or ':' in item.filename:
                raise ValueError('unsafe archive path')
            output = target.joinpath(*parts)
            if output.exists() and not force:
                raise FileExistsError(str(output))
            if any(p.is_symlink() for p in (output, *output.parents)):
                raise ValueError('symlink in restore path')
        for item in members:
            output = target.joinpath(*PurePosixPath(item.filename).parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as src, output.open('wb' if force else 'xb') as dst:
                import shutil
                shutil.copyfileobj(src, dst)
    print('restored', path, 'to', target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('run', 'verify', 'list', 'restore'))
    parser.add_argument('target', nargs='?', help='restore destination directory')
    parser.add_argument('--dest', type=Path, default=Path.home() / 'agent-memory-backups')
    parser.add_argument('--keep', type=int, default=14)
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    if args.command == 'run':
        run(args.dest, args.keep)
    elif args.command == 'list':
        for path in backups(args.dest):
            print(path)
    else:
        found = backups(args.dest)
        if not found:
            parser.error('no backups found')
        if args.command == 'verify':
            verify(found[0])
        elif not args.target:
            parser.error('restore requires a target directory')
        else:
            verify(found[0])
            restore(found[0], Path(args.target), args.force)


if __name__ == '__main__':
    main()
