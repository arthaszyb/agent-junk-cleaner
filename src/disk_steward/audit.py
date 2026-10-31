"""Append-only audit log: data_dir/audit/audit.jsonl, rotated at 4 MiB, five generations kept."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

MAX_BYTES = 4 * 1024 * 1024
KEEP = 5
NAME = 'audit.jsonl'


class Audit:
    def __init__(self, data_dir: Path, clock: Callable[[], float] = time.time):
        self._dir = Path(data_dir) / 'audit'
        self._path = self._dir / NAME
        self._clock = clock
        self._lock = threading.Lock()

    def write(self, event: Mapping[str, Any]) -> None:
        if 'time' in event:
            raise ValueError("audit events must not carry their own 'time'")
        stamp = datetime.fromtimestamp(self._clock(), timezone.utc).isoformat(timespec='milliseconds')
        line = json.dumps({'time': stamp, **event}, separators=(',', ':')) + '\n'
        with self._lock:
            self._dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._dir.mkdir(mode=0o700, exist_ok=True)
            self._rotate_if_needed()
            fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, line.encode('utf-8'))
                os.fsync(fd)
            finally:
                os.close(fd)

    def _rotate_if_needed(self) -> None:
        try:
            size = os.lstat(self._path).st_size
        except FileNotFoundError:
            return
        if size <= MAX_BYTES:
            return
        oldest = self._dir / f'{NAME}.{KEEP}'
        if oldest.exists():
            oldest.unlink()
        for n in range(KEEP - 1, 0, -1):
            src = self._dir / f'{NAME}.{n}'
            if src.exists():
                os.rename(src, self._dir / f'{NAME}.{n + 1}')
        os.rename(self._path, self._dir / f'{NAME}.1')
