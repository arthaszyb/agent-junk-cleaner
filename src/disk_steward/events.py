"""In-process event log for the portal's long poll: a version counter plus the last 500 events."""
from __future__ import annotations

import collections
import threading
import time
from typing import Any, Callable, Deque, Dict, List

MAX_EVENTS = 500


class EventBus:
    def __init__(self, maxlen: int = MAX_EVENTS, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._events: Deque[Dict[str, Any]] = collections.deque(maxlen=maxlen)
        self._cond = threading.Condition()
        self._version = 0

    @property
    def version(self) -> int:
        with self._cond:
            return self._version

    def publish(self, type: str, **data: Any) -> int:
        with self._cond:
            self._version += 1
            self._events.append({'version': self._version, 'time': self._clock(), 'type': type, 'data': data})
            self._cond.notify_all()
            return self._version

    def since(self, version: int) -> List[Dict[str, Any]]:
        """Events newer than `version` that are still retained; older ones may have been dropped."""
        with self._cond:
            return self._newer(version)

    def wait(self, since: int, timeout: float) -> List[Dict[str, Any]]:
        """Like since(), but blocks up to `timeout` seconds while there is nothing newer. [] on timeout."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._version <= since:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            return self._newer(since)

    def _newer(self, version: int) -> List[Dict[str, Any]]:
        return [e for e in self._events if e['version'] > version]
