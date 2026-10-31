"""Logical paths: '~/…', '$TMPDIR/…', '$CACHEDIR/…'. Only Bases turns them into absolute paths."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import List, Optional, Tuple

from .model import ErrorCode, StewardError

GETCONF = '/usr/bin/getconf'
PREFIXES = ('~', '$TMPDIR', '$CACHEDIR')


def _invalid(logical: object) -> StewardError:
    return StewardError(ErrorCode.INVALID, 'error.invalid_path', path=str(logical))


def split_logical(logical: str) -> Tuple[str, List[str]]:
    """Validate a logical path and return (prefix, segments below the prefix).

    Rejecting '.', '..' and empty segments up front means a joined path can never leave its base."""
    if not isinstance(logical, str) or '\0' in logical:
        raise _invalid(logical)
    prefix, sep, rest = logical.partition('/')
    if prefix not in PREFIXES:
        raise _invalid(logical)
    if not sep:
        return prefix, []
    segments = rest.split('/')
    if any(s in ('', '.', '..') for s in segments):
        raise _invalid(logical)
    return prefix, segments


def _darwin_cache_dir() -> Optional[Path]:
    # getconf only knows this variable on macOS. Elsewhere $CACHEDIR stays unavailable and
    # to_abs raises if something actually asks for it.
    if sys.platform != 'darwin':
        return None
    r = subprocess.run([GETCONF, 'DARWIN_USER_CACHE_DIR'], capture_output=True, text=True, timeout=10)
    out = r.stdout.strip()
    if r.returncode != 0 or not out:
        raise RuntimeError(f'getconf DARWIN_USER_CACHE_DIR failed ({r.returncode}): {r.stderr.strip()}')
    return Path(os.path.realpath(out))


@dataclass(frozen=True)
class Bases:
    home: Path
    tmpdir: Path
    cachedir: Optional[Path]   # None where getconf cannot report it (non-macOS)

    @classmethod
    def detect(cls, home: Optional[Path] = None) -> 'Bases':
        home_path = Path(os.path.realpath(home if home is not None else Path.home()))
        tmp = os.environ.get('TMPDIR') or tempfile.gettempdir()
        return cls(home_path, Path(os.path.realpath(tmp)), _darwin_cache_dir())

    def _base(self, prefix: str) -> Path:
        if prefix == '~':
            return self.home
        if prefix == '$TMPDIR':
            return self.tmpdir
        if self.cachedir is None:
            raise StewardError(ErrorCode.UNAVAILABLE, 'error.cachedir_unavailable')
        return self.cachedir

    def to_abs(self, logical: str) -> Path:
        prefix, segments = split_logical(logical)
        return self._base(prefix).joinpath(*segments)

    def to_logical(self, p: Path) -> str:
        p = Path(p)
        if not p.is_absolute() or '..' in p.parts:
            raise _invalid(p)
        best: Optional[Tuple[int, str]] = None
        for prefix, base in (('~', self.home), ('$TMPDIR', self.tmpdir), ('$CACHEDIR', self.cachedir)):
            if base is None:
                continue
            n = len(base.parts)
            if p.parts[:n] == base.parts and (best is None or n > best[0]):
                best = (n, prefix)
        if best is None:
            raise _invalid(p)
        logical = '/'.join((best[1],) + p.parts[best[0]:])
        split_logical(logical)  # catches NUL and any other segment a logical path may not carry
        return logical


def match_pattern(pattern: str, logical: str) -> bool:
    """Segment glob. '**' consumes zero or more whole segments, every other segment is an fnmatchcase."""
    segs = logical.split('/')
    # reachable[i] means the pattern segments consumed so far can end exactly before segs[i]
    reachable = {0}
    for pat in pattern.split('/'):
        if pat == '**':
            reachable = set(range(min(reachable), len(segs) + 1))
        else:
            reachable = {i + 1 for i in reachable if i < len(segs) and fnmatchcase(segs[i], pat)}
        if not reachable:
            return False
    return len(segs) in reachable


def item_key(logical: str) -> str:
    return hashlib.sha1(logical.encode('utf-8', 'surrogateescape')).hexdigest()[:20]


def default_data_dir() -> Path:
    env = os.environ.get('DISK_STEWARD_DATA_DIR')
    if env:
        return Path(os.path.abspath(os.path.expanduser(env)))
    return Path.home() / 'Library' / 'Application Support' / 'Disk Steward'
