# SPDX-License-Identifier: Apache-2.0
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def audio_hash(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def file_fingerprint(path):
    stat = path.stat()
    return (
        stat.st_dev, stat.st_ino, stat.st_size,
        stat.st_mtime_ns, stat.st_ctime_ns,
    )


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.writing-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def atomic_output(path):
    """Commit a generated binary file only after successful creation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix='.writing-', suffix=path.suffix, dir=path.parent
    )
    os.close(fd)
    temporary = Path(name)
    try:
        yield temporary
        with temporary.open('rb') as stream:
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def backup(path, directory):
    if path.exists():
        directory.mkdir(parents=True, exist_ok=True)
        shutil.copy2(
            path, directory / f'{path.name}.{uuid.uuid4().hex[:12]}.bak'
        )


def check_output_paths(root, outputs):
    """Catch directory conflicts and write failures before API calls."""
    for directory in (root, root / 'cache', root / 'history'):
        if directory.exists() and not directory.is_dir():
            raise ValueError(f'Expected an output directory: {directory}')
        parent = directory
        while not parent.exists():
            parent = parent.parent
        try:
            with tempfile.TemporaryFile(dir=parent):
                pass
        except OSError as exc:
            raise ValueError(
                f'Cannot write outputs under {directory}: {exc}'
            ) from exc
    for output in outputs:
        if output.exists() and not output.is_file():
            raise ValueError(f'Expected an output file: {output}')
