"""Restore an exported memory archive while the controller is stopped.

Usage: python scripts/restore.py backup.zip /data
Existing state is preserved in an adjacent timestamped backup directory.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path


def restore(archive: Path, destination: Path) -> None:
    allowed = {'memory.sqlite3', 'sessions', 'skills', 'harness2', 'README.txt'}
    with tempfile.TemporaryDirectory(prefix='terminal-restore-') as tmp:
        staging = Path(tmp)
        with zipfile.ZipFile(archive) as source:
            for info in source.infolist():
                path = Path(info.filename)
                if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] not in allowed:
                    raise ValueError(f'Invalid export path: {info.filename}')
                if info.file_size > 100_000_000:
                    raise ValueError('Archive member exceeds the restore limit.')
            if sum(info.file_size for info in source.infolist()) > 1_000_000_000:
                raise ValueError('Archive exceeds the 1 GB restore limit.')
            source.extractall(staging)
        database = staging / 'memory.sqlite3'
        if not database.is_file():
            raise ValueError('The archive has no memory database.')
        with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Memory database failed its integrity check.')
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'tasks', 'commands', 'memories', 'settings', 'messages', 'events'} <= tables:
                raise ValueError('This is not a terminal.. memory database.')
        destination.mkdir(parents=True, exist_ok=True)
        backup = destination / ('before-restore-' + datetime.now(UTC).strftime('%Y%m%d-%H%M%S-%f'))
        backup.mkdir()
        for name in ('memory.sqlite3', 'memory.sqlite3-wal', 'memory.sqlite3-shm', 'sessions', 'skills', 'harness2'):
            path = destination / name
            if path.exists():
                shutil.move(str(path), backup / name)
        for name in ('memory.sqlite3', 'sessions', 'skills', 'harness2'):
            path = staging / name
            if path.exists():
                shutil.move(str(path), destination / name)
        print(f'Restored memory. Previous state preserved at {backup}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    restore(args.archive, args.destination)
