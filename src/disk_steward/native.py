"""Native cleanup commands for report-only tools: a fixed registry of argv lists (never built from user or AI input),
the checks that say whether one can run here, a measurement of the folders it cleans, and a runner that streams its
output, enforces a timeout and a cancel, and kills the whole process group.

Nothing here decides anything about files: the tool does its own cleanup, Disk Steward only starts it after the user
confirmed the exact command, and measures what was freed."""
from __future__ import annotations

import collections
import glob
import os
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Tuple

from .fsutil import walk_bundle
from .model import Rule
from .paths import Bases

TIMEOUT = 15 * 60.0        # seconds a command may run before its process group is killed
TERM_GRACE = 5.0           # seconds between SIGTERM and SIGKILL
EVENT_LINES = 2000         # output lines published as events; later lines are still counted and end up in the tail
MAX_OUTPUT = 2 * 1024 * 1024   # bytes of output streamed; the rest is read and dropped so the tool never blocks
MAX_LINE = 4096            # characters of one output line
TAIL_LINES = 40            # lines reported with the result and in job()
VERSION_TTL = 600.0
PROBE_TTL = 10.0           # how long a listing reuses `ollama list` or the simctl check; a run always asks again
PROBE_TIMEOUT = 20.0

OLLAMA = 'ollama.models'
NEEDS_XCODE = 'dev.coresimulator'
# rule id -> argv. The only commands this program ever starts for a report-only group.
COMMANDS: Mapping[str, Tuple[str, ...]] = {
    'dev.homebrew': ('brew', 'cleanup', '--prune=all'),
    'dev.pip': ('pip3', 'cache', 'purge'),
    'dev.go-build': ('go', 'clean', '-cache'),
    'dev.go-mod': ('go', 'clean', '-modcache'),
    'dev.pnpm': ('pnpm', 'store', 'prune'),
    'dev.uv': ('uv', 'cache', 'prune'),
    'dev.npm': ('npm', 'cache', 'clean', '--force'),
    'dev.yarn': ('yarn', 'cache', 'clean'),
    'dev.colima': ('colima', 'prune', '--force'),
    NEEDS_XCODE: ('xcrun', 'simctl', 'delete', 'unavailable'),
}
# Shown with their command but never run from here, and why (native.reason.<value>).
COPY_ONLY: Mapping[str, str] = {'dev.playwright': 'copy_network'}
VERSION_ARGV: Mapping[str, Tuple[str, ...]] = {'go': ('go', 'version')}
_VERSION = re.compile(r'\d+(?:\.\d+)+')
_OLLAMA_ROW = re.compile(r'^(\S+)\s+\S+\s+(\d+(?:\.\d+)?)\s*([KMGT]?B)\b')
_LINE_BREAK = re.compile(rb'\r\n|\n|\r')
_UNITS = {'B': 1, 'KB': 10 ** 3, 'MB': 10 ** 6, 'GB': 10 ** 9, 'TB': 10 ** 12}   # ollama prints decimal units


def display(argv: Tuple[str, ...]) -> str:
    """The command as the user reads and confirms it. A run must be given exactly this string."""
    return shlex.join(argv)


def ollama_argv(target: str) -> Tuple[str, ...]:
    return ('ollama', 'rm', target)


def parse_ollama_list(text: str) -> List[Dict[str, Any]]:
    """[{'name', 'size' (as ollama prints it), 'bytes'}] from `ollama list`; the header and anything that is not a
    model row are ignored."""
    out = []
    for line in text.splitlines()[1:]:
        m = _OLLAMA_ROW.match(line.strip())
        if m:
            out.append({'name': m.group(1), 'size': f'{m.group(2)} {m.group(3)}',
                        'bytes': int(float(m.group(2)) * _UNITS[m.group(3)])})
    return out


@dataclass
class ToolState:
    """Whether one report-only rule can be run now. `reason` is (native.reason.<key>, args) when it cannot."""
    rule_id: str
    argv: Optional[Tuple[str, ...]]       # None: nothing to run (copy only, no command, or ollama before a target)
    runnable: bool
    reason: Optional[Tuple[str, Dict[str, Any]]] = None
    tool: Optional[str] = None            # the path shutil.which found
    targets: Optional[List[Dict[str, Any]]] = None


class Probe:
    """The questions about tools on this Mac: where a tool is, its version, whether simctl works, what `ollama list`
    says. Versions are kept for ten minutes, the other two answers for ten seconds."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: Dict[str, Tuple[float, Any]] = {}

    def _cached(self, key: str, ttl: float, fresh: bool, make: Callable[[], Any]) -> Any:
        now = self._clock()
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None and not fresh and now - hit[0] < ttl:
            return hit[1]
        value = make()
        with self._lock:
            self._cache[key] = (now, value)
        return value

    def invalidate(self) -> None:
        """Forget the short-lived answers (models, simctl), so a listing after a run shows what the tool left."""
        with self._lock:
            for key in ('ollama', 'simctl'):
                self._cache.pop(key, None)

    @staticmethod
    def which(name: str) -> Optional[str]:
        return shutil.which(name)

    def version(self, name: str) -> Optional[str]:
        """The version number a tool reports, None when it is missing or does not say."""
        def ask() -> Optional[str]:
            path = self.which(name)
            if path is None:
                return None
            argv = [path] + list(VERSION_ARGV.get(name, (name, '--version'))[1:])
            try:
                r = subprocess.run(argv, capture_output=True, text=True, errors='replace', timeout=PROBE_TIMEOUT,
                                   stdin=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired):
                return None
            first = (r.stdout.strip().splitlines() or [''])[0]
            if r.returncode != 0 or not first:
                return None
            found = _VERSION.search(first)
            return found.group(0) if found else first[:80]
        return self._cached(f'version:{name}', VERSION_TTL, False, ask)

    def simctl_ready(self, fresh: bool = False) -> bool:
        def ask() -> bool:
            xcrun = self.which('xcrun')
            if xcrun is None:
                return False
            try:
                r = subprocess.run([xcrun, 'simctl', 'help'], capture_output=True, timeout=PROBE_TIMEOUT,
                                   stdin=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired):
                return False
            return r.returncode == 0
        return bool(self._cached('simctl', PROBE_TTL, fresh, ask))

    def ollama_targets(self, fresh: bool = False) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """(models, None), or ([], what went wrong) when `ollama list` fails (its server is not running)."""
        def ask() -> Tuple[List[Dict[str, Any]], Optional[str]]:
            path = self.which('ollama')
            if path is None:
                return [], 'ollama was not found'
            try:
                r = subprocess.run([path, 'list'], capture_output=True, text=True, errors='replace',
                                   timeout=PROBE_TIMEOUT, stdin=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired) as e:
                return [], f'{type(e).__name__}: {e}'
            if r.returncode != 0:
                return [], (r.stderr.strip().splitlines() or [f'exit code {r.returncode}'])[0][:200]
            return parse_ollama_list(r.stdout), None
        return self._cached('ollama', PROBE_TTL, fresh, ask)

    def state(self, rule_id: str, fresh: bool = False) -> ToolState:
        """Everything a listing or a run needs to know about one rule. `fresh` skips the short-lived answers."""
        if rule_id in COMMANDS:
            argv = COMMANDS[rule_id]
            tool = self.which(argv[0])
            if tool is None:
                return ToolState(rule_id, argv, False, ('tool_missing', {'tool': argv[0]}))
            if rule_id == NEEDS_XCODE and not self.simctl_ready(fresh):
                return ToolState(rule_id, argv, False, ('needs_xcode', {}), tool)
            return ToolState(rule_id, argv, True, None, tool)
        if rule_id == OLLAMA:
            tool = self.which('ollama')
            if tool is None:
                return ToolState(rule_id, None, False, ('tool_missing', {'tool': 'ollama'}))
            targets, error = self.ollama_targets(fresh)
            if error is not None:
                return ToolState(rule_id, None, False, ('ollama_list_failed', {'error': error}), tool)
            if not targets:
                return ToolState(rule_id, None, False, ('ollama_no_models', {}), tool, [])
            return ToolState(rule_id, None, True, None, tool, targets)
        if rule_id in COPY_ONLY:
            return ToolState(rule_id, None, False, (COPY_ONLY[rule_id], {}))
        return ToolState(rule_id, None, False, ('no_command', {}))


def measure(bases: Bases, rule: Rule, cancel: Optional[Callable[[], bool]] = None) -> int:
    """Allocated bytes of everything the rule's patterns match now: what the scan counts as the group. A
    folder is walked, a file counts its blocks, a symlink nothing."""
    seen = set()
    total = 0
    for pattern in rule.patterns:
        prefix, _, rest = pattern.partition('/')   # only the rule's own '*' is a wildcard, not a '[' in the home path
        for path in sorted(glob.glob(os.path.join(glob.escape(str(bases.to_abs(prefix))), rest))):
            if path in seen:
                continue
            seen.add(path)
            try:
                st = os.lstat(path)
            except FileNotFoundError:   # removed since the glob
                continue
            if os.path.isdir(path) and not os.path.islink(path):
                total += walk_bundle(path, st.st_dev, os.getuid(), name_taint=False, cancel=cancel).alloc
            elif not os.path.islink(path):
                total += st.st_blocks * 512
    return total


@dataclass
class RunResult:
    status: str                 # 'ok' | 'failed' (non-zero exit) | 'timeout' | 'cancelled'
    exit_code: Optional[int]
    seconds: float
    lines: int = 0
    truncated: bool = False
    tail: List[str] = field(default_factory=list)


def _stop(proc: 'subprocess.Popen[bytes]') -> None:
    """Ask the process group to end, then end it. The tool leads its own group (start_new_session), so its children
    go with it and nothing else is touched."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        # macOS answers EPERM for a group whose leader already exited and waits to be reaped
        if proc.poll() is None:
            raise
        return
    try:
        proc.wait(TERM_GRACE)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            if proc.poll() is None:
                raise
        proc.wait()


def run_command(argv: List[str], cwd: str, env: Mapping[str, str], cancelled: Callable[[], bool],
                on_line: Callable[[str], None], timeout: Optional[float] = None) -> RunResult:
    """Run argv (no shell) in `cwd`, passing each output line (stdout and stderr together, ends of line by \\n, \\r or
    \\r\\n, empty lines dropped) to `on_line` until MAX_OUTPUT bytes have been streamed. The rest is read and dropped
    so the tool never blocks on a full pipe. Ends the process group on timeout or when `cancelled()` turns true."""
    limit = TIMEOUT if timeout is None else timeout
    started = time.monotonic()
    proc = subprocess.Popen(argv, cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    sel = selectors.DefaultSelector()
    sel.register(fd, selectors.EVENT_READ)
    tail: Deque[str] = collections.deque(maxlen=TAIL_LINES)
    state = {'bytes': 0, 'lines': 0, 'truncated': False}
    pending = b''

    def emit(raw: bytes) -> None:
        text = raw.decode('utf-8', 'replace').rstrip()
        if not text:
            return
        if state['bytes'] + len(raw) > MAX_OUTPUT:
            state['truncated'] = True
            return
        state['bytes'] += len(raw)
        state['lines'] += 1
        text = text[:MAX_LINE]
        tail.append(text)
        on_line(text)

    def feed(chunk: bytes, last: bool = False) -> None:
        nonlocal pending
        parts = _LINE_BREAK.split(pending + chunk)
        pending = parts.pop()
        for part in parts:
            emit(part)
        if len(pending) > MAX_LINE or last:   # a line without an end is cut where it is too long, or at the end
            emit(pending)
            pending = b''

    status = None
    try:
        while status is None:
            if cancelled():
                status = 'cancelled'
            elif time.monotonic() - started > limit:
                status = 'timeout'
            elif sel.select(0.2):
                chunk = os.read(fd, 65536)
                if chunk:
                    feed(chunk)
                else:
                    break                       # end of output: every writer has closed it
            elif proc.poll() is not None:       # exited, and something it started still holds the pipe
                break
        if status is not None:
            _stop(proc)
        proc.wait()
        for _ in range(32):                     # what the tool wrote last (bounded: a leftover child may keep writing)
            if not sel.select(0):
                break
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            feed(chunk)
        feed(b'', last=True)
    finally:
        sel.close()
        proc.stdout.close()
    seconds = time.monotonic() - started
    code = proc.returncode
    if status is None:
        status = 'ok' if code == 0 else 'failed'
    return RunResult(status, code, seconds, state['lines'], state['truncated'], list(tail))
