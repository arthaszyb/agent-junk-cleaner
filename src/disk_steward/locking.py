"""One operation lock per data dir, shared by the CLI and the portal."""
from __future__ import annotations

import fcntl
import os
import threading
from pathlib import Path
from typing import Dict, Optional

from .model import ErrorCode, StewardError

LOCK_NAME = 'steward.lock'

_thread_locks: Dict[str, threading.Lock] = {}
_registry_guard = threading.Lock()


def _thread_lock(key: str) -> threading.Lock:
    with _registry_guard:
        return _thread_locks.setdefault(key, threading.Lock())


def _busy() -> StewardError:
    return StewardError(ErrorCode.BUSY, 'error.busy')


class OperationLock:
    """Context manager over flock(LOCK_EX | LOCK_NB) on data_dir/steward.lock.

    The flock excludes other processes. The in-process lock excludes other threads and makes a
    re-entry on the same data dir fail with BUSY instead of deadlocking. The lock file is never
    unlinked: removing it would let two processes lock different inodes."""

    def __init__(self, data_dir: Path):
        self._dir = Path(data_dir)
        self._path = self._dir / LOCK_NAME
        self._thread_lock = _thread_lock(os.path.realpath(self._path))
        self._fd: Optional[int] = None

    def __enter__(self) -> 'OperationLock':
        if not self._thread_lock.acquire(blocking=False):
            raise _busy()
        try:
            self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise _busy() from None
            except BaseException:
                os.close(fd)
                raise
            self._fd = fd
        except BaseException:
            self._thread_lock.release()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            raise RuntimeError('OperationLock released without being held')
        try:
            os.close(fd)  # closing the descriptor drops the flock
        finally:
            self._thread_lock.release()

    @property
    def held(self) -> bool:
        """True while this object holds the lock (the executor checks it before and during a run). A lock file that
        was removed or replaced no longer excludes anybody: another process would lock a different inode."""
        fd = self._fd
        if fd is None:
            return False
        try:
            mine, current = os.fstat(fd), os.lstat(self._path)
        except OSError:
            return False
        return (mine.st_dev, mine.st_ino) == (current.st_dev, current.st_ino)

    def held_by_other(self) -> bool:
        """True if someone else holds the lock. Briefly takes the lock itself to find out, so a
        concurrent acquire may see BUSY for a few microseconds."""
        if self._fd is not None:
            return False
        if self._thread_lock.locked():
            return True
        try:
            fd = os.open(self._path, os.O_RDWR | os.O_NOFOLLOW)
        except FileNotFoundError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            os.close(fd)
