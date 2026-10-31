"""Test helpers: FakeHome, Clock, real_home_guard, FakeMacOS.

APFS birth time (verified on macOS 27.0.1 by tests/test_fakehome.py): os.utime() with an mtime
earlier than the file's birth time moves the birth time back to that mtime. A file back-dated 30 days
therefore has birth == mtime == 30 days ago, so StatInfo.changed_ns (max(mtime, birth)) reports the
age the test asked for and no second mechanism is needed. Making a file younger again leaves birth
old and changed_ns follows the new mtime, so set_age() works in both directions.
"""
from __future__ import annotations

import contextlib
import os
import pwd
import shutil
import tempfile
import time
from pathlib import Path
from typing import Dict, Iterator, Optional, Sequence, Union

from disk_steward.i18n import LOCALES, t
from disk_steward.macos import FakeMacOS, OpenFiles  # noqa: F401  (re-exported for tests)
from disk_steward.model import StewardError
from disk_steward.paths import PREFIXES, Bases

DAY = 86400.0
DENSE_LIMIT = 1024 * 1024


def render_error(exc: StewardError) -> str:
    """Render a StewardError in every locale; fails if its message key is missing or its details do not fit."""
    out = ''
    for loc in LOCALES:
        out = t(exc.message_key, loc, **exc.details)
        if out == exc.message_key:
            raise AssertionError(f'{exc.message_key} has no {loc} translation')
    return out


class Clock:
    """Settable time source. Pass the instance wherever production code takes a clock."""

    def __init__(self, now: Optional[float] = None):
        self._now = time.time() if now is None else float(now)

    def __call__(self) -> float:
        return self._now

    def set(self, now: float) -> None:
        self._now = float(now)

    def advance(self, seconds: float = 0.0, days: float = 0.0) -> None:
        self._now += seconds + days * DAY


class FakeHome:
    """Temp HOME, $TMPDIR, $CACHEDIR and data dir under one temp root. Not related to the real HOME env."""

    def __init__(self, clock: Optional[Clock] = None):
        self.clock = clock if clock is not None else Clock()
        self.root: Optional[Path] = None

    def __enter__(self) -> 'FakeHome':
        self.root = Path(os.path.realpath(tempfile.mkdtemp(prefix='ds-test-')))
        self.home = self.root / 'home'
        self.tmpdir = self.root / 'tmp'
        self.cachedir = self.root / 'cache'
        self.data_dir = self.root / 'data'
        for d in (self.home, self.tmpdir, self.cachedir, self.data_dir):
            d.mkdir()
        self.bases = Bases(self.home, self.tmpdir, self.cachedir)
        return self

    def __exit__(self, *exc: object) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        if self.root is not None:
            shutil.rmtree(self.root)  # only ever the temp root created above
            self.root = None

    def path(self, logical: str) -> Path:
        return self.bases.to_abs(logical)

    def file(self, logical: str, size: int = 0, age_days: float = 0, content: Union[bytes, str, None] = None) -> Path:
        """Create a file. `content` wins over `size`. Sizes above 1 MiB are sparse (logical size only)."""
        p = self.path(logical)
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            content = content.encode('utf-8')
        with open(p, 'wb') as f:
            if content is not None:
                f.write(content)
            elif size > DENSE_LIMIT:
                f.truncate(size)
            else:
                f.write(b'\0' * size)
        self._age(p, age_days)
        return p

    def dir(self, logical: str, age_days: float = 0) -> Path:
        """Create a directory. Creating children later bumps its mtime, so age directories last."""
        p = self.path(logical)
        p.mkdir(parents=True, exist_ok=True)
        self._age(p, age_days)
        return p

    def symlink(self, logical: str, target: str, age_days: float = 0) -> Path:
        """`target` may be a logical path or any literal link text."""
        p = self.path(logical)
        p.parent.mkdir(parents=True, exist_ok=True)
        link_text = str(self.path(target)) if target.partition('/')[0] in PREFIXES else target
        os.symlink(link_text, p)
        self._age(p, age_days)
        return p

    def set_age(self, logical: str, days: float) -> None:
        self._age(self.path(logical), days)

    def _age(self, p: Path, days: float) -> None:
        ns = int((self.clock() - days * DAY) * 1e9)
        os.utime(p, ns=(ns, ns), follow_symlinks=False)


@contextlib.contextmanager
def real_home_guard(paths: Optional[Sequence[Path]] = None) -> Iterator[None]:
    """Fail if the mtime of the real ~/.codex, ~/.claude or ~/Library/Caches changes inside the block.

    Directory mtimes only move when an entry is added or removed directly in that directory, so
    unrelated activity in subfolders does not trip it; a direct write by another app during the
    block can (hence: keep the block short)."""
    if paths is None:
        real = Path(pwd.getpwuid(os.getuid()).pw_dir)
        paths = [real / '.codex', real / '.claude', real / 'Library' / 'Caches']

    def snap() -> Dict[Path, Optional[int]]:
        out: Dict[Path, Optional[int]] = {}
        for p in paths:
            try:
                out[p] = os.stat(p).st_mtime_ns
            except FileNotFoundError:
                out[p] = None
        return out

    before = snap()
    yield
    after = snap()
    changed = sorted(str(p) for p in before if before[p] != after[p])
    if changed:
        raise AssertionError(f'the real home was touched: {changed}')
