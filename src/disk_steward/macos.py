"""Every query the tool makes of macOS goes through this adapter so tests can script it."""
from __future__ import annotations

import bisect
import os
import plistlib
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Set, Tuple
from xml.parsers.expat import ExpatError

from .model import ErrorCode, StewardError

LSAPPINFO = '/usr/bin/lsappinfo'
LSOF = '/usr/sbin/lsof'
MDLS = '/usr/bin/mdls'
MDFIND = '/usr/bin/mdfind'
TRASH = '/usr/bin/trash'
TMUTIL = '/usr/bin/tmutil'
DU = '/usr/bin/du'
MDLS_BATCH = 200

Inode = Tuple[int, int]


class OpenFiles:
    """Snapshot of open files. `by_inode` covers every open file, `executing` only the 'txt' ones (binaries and
    libraries mapped by a running process). `path_holders` maps an absolute path to its process labels."""

    def __init__(self, taken_at: float = 0.0, by_inode: Optional[Mapping[Inode, Sequence[str]]] = None,
                 executing: Optional[Mapping[Inode, Sequence[str]]] = None,
                 path_holders: Optional[Mapping[str, Sequence[str]]] = None):
        self.taken_at = taken_at
        self.by_inode: Dict[Inode, List[str]] = {k: list(v) for k, v in (by_inode or {}).items()}
        self.executing: Dict[Inode, List[str]] = {k: list(v) for k, v in (executing or {}).items()}
        self._path_holders: Dict[str, List[str]] = {k: list(v) for k, v in (path_holders or {}).items()}
        self.paths: List[str] = sorted(self._path_holders)

    def holders(self, dev: int, ino: int) -> List[str]:
        return list(self.by_inode.get((dev, ino), ()))

    def holders_under(self, abs_dir: str) -> List[str]:
        """Labels of processes holding abs_dir itself or anything below it."""
        base = abs_dir.rstrip('/')
        prefix = base + '/'
        found: Set[str] = set(self._path_holders.get(base or '/', ()))
        i = bisect.bisect_left(self.paths, prefix)
        while i < len(self.paths) and self.paths[i].startswith(prefix):
            found.update(self._path_holders[self.paths[i]])
            i += 1
        return sorted(found)


_SIMPLE_ESCAPES = {'\\': b'\\', 'a': b'\a', 'b': b'\b', 'f': b'\f', 'n': b'\n', 'r': b'\r', 't': b'\t', 'v': b'\v'}


def unescape_lsof_name(raw: str) -> str:
    """Undo lsof's name escaping: \\xNN is one byte (UTF-8 bytes of a non-ASCII name arrive that way in a
    non-UTF-8 locale), \\n, \\t and friends are control characters, \\\\ is a backslash. Raises RuntimeError
    when the bytes do not form valid UTF-8: such a name cannot be compared with a path, and a missed name would
    mean a missed open file."""
    if '\\' not in raw:
        return raw
    out = bytearray()
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == '\\' and i + 1 < len(raw):
            nxt = raw[i + 1]
            if nxt == 'x' and re.fullmatch(r'[0-9a-fA-F]{2}', raw[i + 2:i + 4]):
                out.append(int(raw[i + 2:i + 4], 16))
                i += 4
                continue
            if nxt in _SIMPLE_ESCAPES:
                out += _SIMPLE_ESCAPES[nxt]
                i += 2
                continue
        out += ch.encode('utf-8', 'surrogateescape')
        i += 1
    try:
        return out.decode('utf-8')
    except UnicodeDecodeError as e:
        raise RuntimeError(f'lsof printed a name that is not valid UTF-8: {raw!r}') from e


_CARET = re.compile(r'\^([@-_])')


def lsof_name_variants(raw: str) -> List[str]:
    """The paths a raw lsof name may stand for. lsof also prints a control character as ^A, which a file name can
    contain literally, so such a name is registered both ways: a path counted twice only makes the guard stricter."""
    plain = unescape_lsof_name(raw)
    carets = _CARET.sub(lambda m: chr(ord(m.group(1)) - 0x40), plain)
    return [plain] if carets == plain else [plain, carets]


def parse_lsof(text: str, taken_at: float) -> OpenFiles:
    """Parse `lsof -F pcftDin`. On macOS the 'D' field is hex and equals os.stat().st_dev, 'i' is decimal."""
    by_inode: Dict[Inode, Dict[str, None]] = {}
    executing: Dict[Inode, Dict[str, None]] = {}
    path_holders: Dict[str, Dict[str, None]] = {}
    pid = 0
    cmd = ''
    fd: Optional[str] = None
    dev: Optional[int] = None
    ino: Optional[int] = None
    name: Optional[str] = None

    def flush() -> None:
        if fd is None:
            return
        label = f'{cmd}({pid})'
        if dev is not None and ino is not None:
            by_inode.setdefault((dev, ino), {})[label] = None
            if fd == 'txt':
                executing.setdefault((dev, ino), {})[label] = None
        if name is not None and name.startswith('/'):
            for variant in lsof_name_variants(name):
                path_holders.setdefault(variant, {})[label] = None

    for line in text.split('\n'):
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == 'p':
            flush()
            fd = None
            pid = int(value)
        elif tag == 'c':
            cmd = value
        elif tag == 'f':
            flush()
            fd, dev, ino, name = value, None, None, None
        elif tag == 'D':
            dev = int(value, 16)
        elif tag == 'i':
            ino = int(value)
        elif tag == 'n':
            name = value
        elif tag != 't':
            raise RuntimeError(f'unexpected lsof field {line!r}')
    flush()
    return OpenFiles(taken_at,
                     {k: list(v) for k, v in by_inode.items()},
                     {k: list(v) for k, v in executing.items()},
                     {k: list(v) for k, v in path_holders.items()})


def _run(argv: List[str], timeout: float, env: Optional[Mapping[str, str]] = None) -> subprocess.CompletedProcess:
    # surrogateescape: file names are not guaranteed to be valid UTF-8
    return subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', errors='surrogateescape',
                          timeout=timeout, env=None if env is None else dict(env))


def _lsof_env() -> Dict[str, str]:
    """lsof prints every non-ASCII byte as \\xNN unless its locale says the character is printable, so it runs in a
    UTF-8 locale whatever the caller's is (a bare LaunchAgent has none)."""
    env = dict(os.environ)
    env['LC_ALL'] = env['LANG'] = 'en_US.UTF-8'
    return env


def _fail(argv: List[str], r: subprocess.CompletedProcess) -> RuntimeError:
    return RuntimeError(f'{argv[0]} exited with {r.returncode}: {(r.stderr or r.stdout).strip()}')


def _parse_last_used(value: str) -> Optional[float]:
    if value == '(null)':
        return None
    return datetime.strptime(value, '%Y-%m-%d %H:%M:%S %z').timestamp()


def _plist_text(plist: Mapping[str, object], key: str) -> str:
    value = plist.get(key)
    return value if isinstance(value, str) else ''


class MacOS(Protocol):
    def running_bundle_ids(self) -> Set[str]: ...
    def open_files(self) -> OpenFiles: ...
    def last_used(self, abs_paths: List[str]) -> Dict[str, Optional[float]]: ...
    def installed_apps(self) -> Dict[str, str]: ...
    def installed_app_info(self) -> Dict[str, Dict[str, str]]: ...
    def trash(self, abs_path: str) -> None: ...
    def trash_available(self) -> bool: ...
    def statfs_free(self, abs_path: str) -> int: ...
    def volume_usage(self, abs_path: str) -> Tuple[int, int]: ...
    def snapshots(self) -> List[str]: ...
    def tool_version(self, name: str) -> Optional[str]: ...
    def app_sizes(self, paths: List[str], timeout: float = 180) -> Dict[str, Dict[str, Any]]: ...


class RealMacOS:
    def __init__(self, home: Optional[Path] = None):
        self._home = Path(home) if home is not None else Path.home()

    def running_bundle_ids(self) -> Set[str]:
        argv = [LSAPPINFO, 'list']
        r = _run(argv, 30)
        if r.returncode != 0:
            raise _fail(argv, r)
        return set(re.findall(r'bundleID="([^"]+)"', r.stdout))

    def open_files(self) -> OpenFiles:
        argv = [LSOF, '-nP', '-F', 'pcftDin', '-w']
        taken_at = time.time()
        r = _run(argv, 30, env=_lsof_env())
        # lsof exits 1 when it could not inspect some processes but still lists the rest
        if r.returncode not in (0, 1) or not r.stdout:
            raise _fail(argv, r)
        return parse_lsof(r.stdout, taken_at)

    def last_used(self, abs_paths: List[str]) -> Dict[str, Optional[float]]:
        result: Dict[str, Optional[float]] = {}
        # mdls aborts at the first missing path and prints its complaint on stdout, so vanished files
        # are answered here and never reach it.
        existing = []
        for p in abs_paths:
            if os.path.lexists(p):
                existing.append(p)
            else:
                result[p] = None
        for i in range(0, len(existing), MDLS_BATCH):
            batch = existing[i:i + MDLS_BATCH]
            values = self._mdls(batch)
            if values is None:  # something vanished after the check above: ask one by one
                values = []
                for p in batch:
                    single = self._mdls([p])
                    values.append(single[0] if single is not None else None)
            result.update(zip(batch, (_parse_last_used(v) if v is not None else None for v in values)))
        return result

    @staticmethod
    def _mdls(paths: List[str]) -> Optional[List[str]]:
        """Raw values in input order, or None if mdls could not find one of the paths."""
        argv = [MDLS, '-raw', '-nullMarker', '(null)', '-name', 'kMDItemLastUsedDate'] + paths
        r = _run(argv, 60)
        if r.returncode != 0:
            if 'could not find' in r.stdout:
                return None
            raise _fail(argv, r)
        values = r.stdout.split('\0')  # -raw separates values with NUL and adds none after the last
        if len(values) != len(paths):
            raise RuntimeError(f'mdls returned {len(values)} values for {len(paths)} paths')
        return values

    def installed_apps(self) -> Dict[str, str]:
        argv = [MDFIND, "kMDItemContentType == 'com.apple.application-bundle'"]
        r = _run(argv, 60)
        if r.returncode != 0:
            raise _fail(argv, r)
        paths = {line for line in r.stdout.split('\n') if line}
        # Spotlight can be disabled or lag behind, so also list the standard folders.
        for folder in ('/Applications', str(self._home / 'Applications'), '/System/Applications'):
            try:
                names = os.listdir(folder)
            except FileNotFoundError:
                continue
            paths.update(os.path.join(folder, n) for n in names if n.endswith('.app'))
        apps: Dict[str, str] = {}
        for app in sorted(paths):
            try:
                with open(os.path.join(app, 'Contents', 'Info.plist'), 'rb') as f:
                    info = plistlib.load(f)
            except (OSError, plistlib.InvalidFileException, ExpatError):
                continue  # not a readable app bundle
            bundle_id = info.get('CFBundleIdentifier') if isinstance(info, dict) else None
            if isinstance(bundle_id, str) and bundle_id:
                apps.setdefault(bundle_id, app)
        return apps

    def installed_app_info(self) -> Dict[str, Dict[str, str]]:
        """bundle id -> {'path', 'name', 'display_name', 'executable', 'stem'} of every app installed_apps() finds.
        The four name fields are the names an app's data folders are called after; '' when the plist has none."""
        info: Dict[str, Dict[str, str]] = {}
        for bundle_id, app in sorted(self.installed_apps().items()):
            try:
                with open(os.path.join(app, 'Contents', 'Info.plist'), 'rb') as f:
                    plist = plistlib.load(f)
            except (OSError, plistlib.InvalidFileException, ExpatError):
                continue  # vanished since installed_apps() read it
            if not isinstance(plist, dict):
                continue
            base = os.path.basename(app)
            info[bundle_id] = {'path': app, 'name': _plist_text(plist, 'CFBundleName'),
                               'display_name': _plist_text(plist, 'CFBundleDisplayName'),
                               'executable': _plist_text(plist, 'CFBundleExecutable'),
                               'stem': base[:-4] if base.endswith('.app') else base}
        return info

    def trash_available(self) -> bool:
        return os.access(TRASH, os.X_OK)

    def trash(self, abs_path: str) -> None:
        if not self.trash_available():
            raise StewardError(ErrorCode.UNAVAILABLE, 'error.trash_unavailable')
        if not os.path.isabs(abs_path):
            raise ValueError(f'trash needs an absolute path, got {abs_path!r}')
        argv = [TRASH, abs_path]
        r = _run(argv, 120)
        if r.returncode != 0:
            raise _fail(argv, r)

    def statfs_free(self, abs_path: str) -> int:
        st = os.statvfs(abs_path)
        return st.f_bavail * st.f_frsize

    def volume_usage(self, abs_path: str) -> Tuple[int, int]:
        st = os.statvfs(abs_path)
        return st.f_blocks * st.f_frsize, (st.f_blocks - st.f_bfree) * st.f_frsize

    def snapshots(self) -> List[str]:
        argv = [TMUTIL, 'listlocalsnapshots', '/']
        r = _run(argv, 30)
        if r.returncode != 0:
            raise _fail(argv, r)
        return [line.strip() for line in r.stdout.split('\n') if line.startswith('com.apple')]

    def tool_version(self, name: str) -> Optional[str]:
        exe = shutil.which(name)
        if exe is None:
            return None
        argv = [exe, '--version']
        r = _run(argv, 10)
        if r.returncode != 0:
            raise _fail(argv, r)
        lines = r.stdout.strip().split('\n')
        if not lines[0].strip():
            raise RuntimeError(f'{exe} --version printed nothing')
        return lines[0].strip()

    def app_sizes(self, paths: List[str], timeout: float = 180) -> Dict[str, Dict[str, Any]]:
        """path -> {'bytes': allocated size} or {'error': text}, one `du` per path. `timeout` covers all of them
        together: a path the budget does not reach is an error, and one failing path never stops the others."""
        deadline = time.monotonic() + timeout
        sizes: Dict[str, Dict[str, Any]] = {}
        for path in paths:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                sizes[path] = {'error': 'timed out'}
                continue
            argv = [DU, '-sk', '-x', path]
            try:
                r = _run(argv, remaining)
            except subprocess.TimeoutExpired:
                sizes[path] = {'error': 'timed out'}
                continue
            kib = r.stdout.partition('\t')[0].strip()
            # du still prints a total when it could not read part of the tree; that total would be too small
            if r.returncode != 0 or not kib.isdigit():
                sizes[path] = {'error': str(_fail(argv, r))}
            else:
                sizes[path] = {'bytes': int(kib) * 1024}
        return sizes


class FakeMacOS:
    """Scriptable stand-in: set the plain attributes, read `trashed` and `calls` afterwards."""

    def __init__(self, running: Iterable[str] = (), open_snapshot: Optional[OpenFiles] = None,
                 last_used_map: Optional[Mapping[str, Optional[float]]] = None,
                 apps: Optional[Mapping[str, str]] = None, free: int = 10 ** 12,
                 usage: Tuple[int, int] = (500 * 10 ** 9, 200 * 10 ** 9), trash_ok: bool = True,
                 snapshot_names: Iterable[str] = (), tool_versions: Optional[Mapping[str, Optional[str]]] = None,
                 app_info: Optional[Mapping[str, Mapping[str, str]]] = None):
        self.running: Set[str] = set(running)
        self.open: OpenFiles = open_snapshot if open_snapshot is not None else OpenFiles()
        self.last_used_map: Dict[str, Optional[float]] = dict(last_used_map or {})
        self.apps: Dict[str, str] = dict(apps or {})
        self.app_info: Dict[str, Dict[str, str]] = {k: dict(v) for k, v in (app_info or {}).items()}
        self.trashed: List[str] = []
        self.free = free
        self.usage = usage
        self.trash_ok = trash_ok
        self.snapshot_names: List[str] = list(snapshot_names)
        self.tool_versions: Dict[str, Optional[str]] = dict(tool_versions or {})
        self.app_size_map: Dict[str, Dict[str, Any]] = {}   # path -> {'bytes': n} or {'error': text}
        self.calls: List[Tuple[str, tuple]] = []

    def running_bundle_ids(self) -> Set[str]:
        self.calls.append(('running_bundle_ids', ()))
        return set(self.running)

    def open_files(self) -> OpenFiles:
        self.calls.append(('open_files', ()))
        return self.open

    def last_used(self, abs_paths: List[str]) -> Dict[str, Optional[float]]:
        self.calls.append(('last_used', (list(abs_paths),)))
        return {p: self.last_used_map.get(p) for p in abs_paths}

    def installed_apps(self) -> Dict[str, str]:
        self.calls.append(('installed_apps', ()))
        return dict(self.apps)

    def installed_app_info(self) -> Dict[str, Dict[str, str]]:
        self.calls.append(('installed_app_info', ()))
        return {k: dict(v) for k, v in self.app_info.items()}

    def trash(self, abs_path: str) -> None:
        self.calls.append(('trash', (abs_path,)))
        if not self.trash_ok:
            raise StewardError(ErrorCode.UNAVAILABLE, 'error.trash_unavailable')
        self.trashed.append(abs_path)

    def trash_available(self) -> bool:
        self.calls.append(('trash_available', ()))
        return self.trash_ok

    def statfs_free(self, abs_path: str) -> int:
        self.calls.append(('statfs_free', (abs_path,)))
        return self.free

    def volume_usage(self, abs_path: str) -> Tuple[int, int]:
        self.calls.append(('volume_usage', (abs_path,)))
        return self.usage

    def snapshots(self) -> List[str]:
        self.calls.append(('snapshots', ()))
        return list(self.snapshot_names)

    def tool_version(self, name: str) -> Optional[str]:
        self.calls.append(('tool_version', (name,)))
        return self.tool_versions.get(name)

    def app_sizes(self, paths: List[str], timeout: float = 180) -> Dict[str, Dict[str, Any]]:
        self.calls.append(('app_sizes', (list(paths), timeout)))
        return {p: dict(self.app_size_map.get(p, {'bytes': 0})) for p in paths}
