"""Helpers shared by detectors."""
from __future__ import annotations

import errno
import json
import os
import re
from pathlib import Path
from typing import Any, List, Optional, Tuple

from ..paths import Bases

_VERSION_RE = re.compile(r'^\d+(\.\d+)+$')


def parse_version(name: str) -> Optional[Tuple[int, ...]]:
    """'2.1.299' -> (2, 1, 299); anything else (partial downloads, 'latest', hidden files) -> None."""
    if not _VERSION_RE.match(name):
        return None
    return tuple(int(p) for p in name.split('.'))


def newest_version(names: List[str]) -> Optional[str]:
    versions = [n for n in names if parse_version(n) is not None]
    return max(versions, key=lambda n: (parse_version(n) or (), n)) if versions else None


def list_dir(bases: Bases, logical: str) -> Optional[List[str]]:
    """Sorted entry names, or None when the directory does not exist."""
    try:
        return sorted(os.listdir(bases.to_abs(logical)))
    except (FileNotFoundError, NotADirectoryError):
        return None


def read_json(path: Path) -> Tuple[str, Any]:
    """('ok', value) | ('missing', None) | ('unreadable', None) | ('invalid', None). Never raises for those."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return 'missing', None
    except OSError as e:
        if e.errno in (errno.EACCES, errno.EPERM, errno.EISDIR, errno.ENOTDIR):
            return 'unreadable', None
        raise
    try:
        return 'ok', json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return 'invalid', None


def last_segment(logical: str) -> str:
    return logical.rpartition('/')[2]


def parent_of(logical: str) -> str:
    return logical.rpartition('/')[0]
