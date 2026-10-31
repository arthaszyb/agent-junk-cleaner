"""Running-app and open-file snapshots. The executor asks `blockers()` before it touches an entry."""
from __future__ import annotations

import bisect
import os
from typing import Callable, Iterable, List, Optional, Set

from .macos import MacOS, OpenFiles
from .model import Entry, Rule, StatInfo

SNAPSHOT_TTL = 5.0   # seconds a snapshot may be reused; lsof over a whole Mac takes about a second


class SnapshotError(RuntimeError):
    """lsof or lsappinfo failed. Never read as "nothing is open": the run stops instead."""


class Guard:
    def __init__(self, mac: MacOS, clock: Callable[[], float], ttl: float = SNAPSHOT_TTL):
        self._mac = mac
        self._clock = clock
        self._ttl = ttl
        self._running: Optional[Set[str]] = None
        self._open: Optional[OpenFiles] = None
        self._taken: Optional[float] = None

    def _stale(self) -> bool:
        if self._taken is None:
            return True
        age = self._clock() - self._taken
        return not 0 <= age <= self._ttl   # a clock that jumped backwards counts as stale too

    def refresh(self, force: bool = False) -> None:
        if not force and not self._stale():
            return
        try:
            running = self._mac.running_bundle_ids()
            open_files = self._mac.open_files()
        except Exception as e:
            raise SnapshotError(f'cannot take the running-app and open-file snapshot: {type(e).__name__}: {e}') from e
        self._running, self._open, self._taken = running, open_files, self._clock()

    def blockers(self, entry: Entry, rule: Rule, abs_path: str, is_bundle: bool, st: StatInfo,
                 extra_quit_apps: Iterable[str] = ()) -> List[str]:
        """Reason codes that forbid touching this entry now; empty means clear.
        'app_running:<bundle id>', 'executing:<process>', 'open:<process>'. `extra_quit_apps` are apps the item
        itself needs quit on top of the rule's (generic Electron caches learn their app at scan time)."""
        self.refresh()
        assert self._running is not None and self._open is not None
        needed = set(rule.quit_apps) | set(extra_quit_apps)
        found: List[str] = [f'app_running:{app}' for app in sorted(needed & self._running)]
        executing: Set[str] = set()
        opened: Set[str] = set(self._open.holders_under(abs_path))   # the path itself and, for a folder, all below it
        if is_bundle:
            # lsof lists a mapped binary with its path, so every executing file below the folder is among
            # the open paths; its (dev, ino) tells which of them are running code rather than plain opens.
            prefix = abs_path.rstrip('/') + '/'
            i = bisect.bisect_left(self._open.paths, prefix)
            while i < len(self._open.paths) and self._open.paths[i].startswith(prefix):
                executing.update(self._executing_labels(self._open.paths[i]))
                i += 1
        else:
            opened.update(self._open.holders(st.dev, st.ino))
            executing.update(self._open.executing.get((st.dev, st.ino), ()))
        found += [f'executing:{label}' for label in sorted(executing)]
        found += [f'open:{label}' for label in sorted(opened - executing)]
        return found

    def _executing_labels(self, path: str) -> List[str]:
        assert self._open is not None
        try:
            info = os.lstat(path)
        except OSError:
            return []   # closed or gone since the snapshot; its holder still shows as an open path
        return self._open.executing.get((info.st_dev, info.st_ino), [])
