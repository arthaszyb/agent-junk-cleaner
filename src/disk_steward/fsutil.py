"""Filesystem primitives: lstat-only stats, bundle walking with taint detection, symlink-safe parent
opening and the atomic no-overwrite rename the executor relies on."""
from __future__ import annotations

import contextlib
import ctypes
import errno
import functools
import os
import re
import stat
import sys
from fnmatch import translate
from typing import Callable, Iterator, Optional, Pattern, Sequence, Set, Tuple, Union

from .model import BundleSummary, ErrorCode, StatInfo, StewardError

PathLike = Union[str, 'os.PathLike[str]']

UF_IMMUTABLE = getattr(stat, 'UF_IMMUTABLE', 0x00000002)
UF_APPEND = getattr(stat, 'UF_APPEND', 0x00000004)
SF_IMMUTABLE = getattr(stat, 'SF_IMMUTABLE', 0x00020000)
SF_APPEND = getattr(stat, 'SF_APPEND', 0x00040000)
SF_DATALESS = getattr(stat, 'SF_DATALESS', 0x40000000)  # missing from older Pythons
LOCKED_FLAGS = UF_IMMUTABLE | SF_IMMUTABLE | UF_APPEND | SF_APPEND

RENAME_EXCL = 0x4

# Several taints can apply to one bundle; the reported one must not depend on scandir order, because the
# executor compares the taint of a re-walk with the one stored by the scan.
TAINT_ORDER = ('mount', 'foreign_owner', 'immutable', 'dataless', 'git', 'database', 'credential', 'unreadable')

_CANCEL_EVERY = 2048  # entries between cancel() polls inside one huge directory
_VANISHED_OR_DENIED = (errno.EPERM, errno.EACCES, errno.ENOENT, errno.ENOTDIR)


def stat_nofollow(abs_path: PathLike) -> StatInfo:
    return StatInfo.from_stat(os.lstat(abs_path))


def _stat_entry(entry: 'os.DirEntry[str]') -> StatInfo:
    # One seam for tests: they replace it to fake foreign owners, other devices and file flags without root.
    return StatInfo.from_stat(entry.stat(follow_symlinks=False))


def name_regex(globs: Sequence[str]) -> Pattern[str]:
    """One regex for a glob list. Names are compared lowercased (APFS is case-insensitive by default),
    so a credential called 'Auth.JSON' is as protected as 'auth.json'."""
    if not globs:
        return re.compile(r'(?!)')
    return re.compile('|'.join(translate(g.lower()) for g in globs))


@functools.lru_cache(maxsize=None)
def _name_taint_regexes() -> Tuple[Pattern[str], Pattern[str]]:
    # Imported lazily: the catalog package is only needed when a walk actually checks names.
    from .catalog import protections
    return name_regex(protections.DATABASE_NAMES), name_regex(protections.CREDENTIAL_NAMES)


def _cancelled() -> StewardError:
    return StewardError(ErrorCode.IN_PROGRESS, 'error.scan_cancelled')


def _taint_of(st: StatInfo, root_dev: int, uid: int) -> Set[str]:
    found: Set[str] = set()
    if st.dev != root_dev:
        found.add('mount')
    if st.uid != uid:
        found.add('foreign_owner')
    if st.flags & SF_DATALESS:
        found.add('dataless')
    if st.flags & LOCKED_FLAGS:
        found.add('immutable')
    return found


def walk_bundle(abs_dir: PathLike, root_dev: int, uid: int, name_taint: bool = True,
                cancel: Optional[Callable[[], bool]] = None) -> BundleSummary:
    """Summarise a directory tree without following symlinks.

    Symlinks count as files of 0 bytes. A hard-linked file (nlink > 1) adds to alloc but not to reclaim:
    removing this path leaves the data in place. Directories contribute to newest_ns because adding or
    removing an entry changes them even when no file does. Anything that makes automatic removal unsafe
    is reported as `taint`; the walk still finishes so the caller can show the size."""
    db_re, cred_re = _name_taint_regexes() if name_taint else (None, None)
    taints: Set[str] = set()
    files = size = alloc = reclaim = newest = 0

    try:
        root = stat_nofollow(abs_dir)
    except OSError as e:
        if e.errno not in _VANISHED_OR_DENIED:
            raise
        return BundleSummary(0, 0, 0, 0, 0, 'unreadable')
    taints |= _taint_of(root, root_dev, uid)
    newest = root.changed_ns
    if root.flags & SF_DATALESS:  # listing a placeholder directory would download it
        return BundleSummary(0, 0, 0, 0, newest, 'dataless')

    stack = [os.fspath(abs_dir)]
    polled = 0
    while stack:
        if cancel is not None and cancel():
            raise _cancelled()
        current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError as e:
            if e.errno not in _VANISHED_OR_DENIED:
                raise
            taints.add('unreadable')
            continue
        with it:
            while True:
                try:
                    entry = next(it)
                except StopIteration:
                    break
                except OSError as e:
                    if e.errno not in _VANISHED_OR_DENIED:
                        raise
                    taints.add('unreadable')
                    break
                polled += 1
                if cancel is not None and polled % _CANCEL_EVERY == 0 and cancel():
                    raise _cancelled()
                try:
                    st = _stat_entry(entry)
                except OSError as e:
                    if e.errno not in _VANISHED_OR_DENIED:
                        raise
                    taints.add('unreadable')
                    continue
                taints |= _taint_of(st, root_dev, uid)
                if st.changed_ns > newest:
                    newest = st.changed_ns
                if entry.name == '.git':  # a directory, or the pointer file of a worktree or submodule
                    taints.add('git')
                if stat.S_ISDIR(st.mode):
                    # Never walk into another volume, and never into a cloud placeholder (it would be downloaded).
                    if st.dev == root_dev and not st.flags & SF_DATALESS:
                        stack.append(entry.path)
                    continue
                files += 1
                if stat.S_ISLNK(st.mode):
                    continue
                size += st.size
                alloc += st.alloc
                if st.nlink <= 1:
                    reclaim += st.alloc
                if db_re is not None:
                    lowered = entry.name.lower()
                    if db_re.match(lowered):
                        taints.add('database')
                    if cred_re.match(lowered):
                        taints.add('credential')

    taint = next((t for t in TAINT_ORDER if t in taints), '')
    return BundleSummary(files=files, size=size, alloc=alloc, reclaim=reclaim, newest_ns=newest, taint=taint)


@contextlib.contextmanager
def open_parent_nofollow(abs_path: PathLike,
                         inspect: Optional[Callable[[int, int], None]] = None) -> Iterator[Tuple[int, str]]:
    """Open every ancestor of abs_path from '/' with O_NOFOLLOW|O_DIRECTORY and yield (parent_fd, name).

    A symlinked ancestor fails with OSError (ELOOP or ENOTDIR) instead of redirecting the caller, so
    operations done through parent_fd with dir_fd= cannot be steered outside the intended tree.
    `inspect(fd, index)` runs on each opened ancestor (index = its position among the path's segments) and may
    raise to refuse it, e.g. for an owner check that must look at the very directory that was opened."""
    path = os.fspath(abs_path)
    if not os.path.isabs(path):
        raise ValueError(f'absolute path required, got {path!r}')
    parts = [p for p in path.split('/') if p]
    if not parts or any(p in ('.', '..') for p in parts):
        raise ValueError(f'unusable path {path!r}')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(parts[:-1]):
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
            if inspect is not None:
                inspect(fd, index)
        yield fd, parts[-1]
    finally:
        os.close(fd)


_renameatx_np = None


def _load_renameatx() -> Callable[..., int]:
    global _renameatx_np
    if _renameatx_np is None:
        libc = ctypes.CDLL(None, use_errno=True)
        fn = libc.renameatx_np  # AttributeError on a libc without it surfaces as is
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        _renameatx_np = fn
    return _renameatx_np


def rename_excl(src_dir_fd: int, src_name: str, dst_dir_fd: int, dst_name: str) -> None:
    """Atomic rename that fails with EEXIST instead of replacing an existing destination."""
    if sys.platform != 'darwin':
        raise NotImplementedError('rename_excl needs renameatx_np (macOS)')
    fn = _load_renameatx()
    if fn(src_dir_fd, os.fsencode(src_name), dst_dir_fd, os.fsencode(dst_name), RENAME_EXCL) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), src_name, None, dst_name)
