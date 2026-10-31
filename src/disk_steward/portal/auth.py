"""Launch codes and session tokens. A code is printed in the terminal, valid for two minutes and good for one
browser, which trades it for a token it sends in a header. No cookie is involved: a cookie for 127.0.0.1 goes to every
server on that address, whatever its port."""
from __future__ import annotations

import collections
import hmac
import secrets
import threading
import time
from typing import Callable, Dict, Optional

TOKEN_HEADER = 'X-Disk-Steward-Token'
LEGACY_COOKIE = 'ds_session'   # set by earlier versions; removed from browsers that still send it
CODE_TTL = 120.0
MAX_CODES = 32       # codes minted by `portal --url` and never used must not pile up
MAX_SESSIONS = 64


class Auth:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._codes: Dict[str, float] = {}   # code -> expiry
        self._sessions: 'collections.OrderedDict[str, None]' = collections.OrderedDict()

    def new_code(self) -> str:
        code = secrets.token_urlsafe(24)
        with self._lock:
            now = self._clock()
            for old in [c for c, expiry in self._codes.items() if expiry <= now]:
                del self._codes[old]
            while len(self._codes) >= MAX_CODES:
                del self._codes[next(iter(self._codes))]
            self._codes[code] = now + CODE_TTL
        return code

    def redeem(self, code: str) -> Optional[str]:
        """A new session token for a valid code, which is spent; None for an unknown, used or expired one."""
        with self._lock:
            now = self._clock()
            match = None
            for known, expiry in self._codes.items():   # no early exit: the time taken must not tell how close a guess was
                if hmac.compare_digest(known.encode(), code.encode('utf-8', 'replace')) and expiry > now:
                    match = known
            for old in [c for c, expiry in self._codes.items() if expiry <= now or c == match]:
                del self._codes[old]
            if match is None:
                return None
            token = secrets.token_urlsafe(32)
            self._sessions[token] = None
            while len(self._sessions) > MAX_SESSIONS:
                self._sessions.popitem(last=False)
            return token

    def valid_token(self, token: Optional[str]) -> bool:
        if not token:
            return False
        with self._lock:
            found = False
            for known in self._sessions:   # every one is compared, so the time does not tell how close a guess was
                found = hmac.compare_digest(known.encode(), token.encode('utf-8', 'replace')) or found
            return found


def has_legacy_cookie(cookie_header: Optional[str]) -> bool:
    """Whether a Cookie header carries the old session cookie. Plain splitting: a header is user input, and the
    cookie parser of the standard library raises on malformed ones."""
    if not cookie_header:
        return False
    return any(part.split('=', 1)[0].strip() == LEGACY_COOKIE for part in cookie_header.split(';'))
