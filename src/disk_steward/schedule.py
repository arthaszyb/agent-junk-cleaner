"""The weekly scan-only check: a LaunchAgent that runs `scan --notify`, and the notification it posts.
Nothing here deletes or plans anything."""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import i18n
from .macos import _fail, _run
from .report import fmt_bytes, group_label

LABEL = 'io.github.disk-steward.scan'
# Module-level so tests can point them at a temp folder and a fake executable.
LAUNCH_AGENTS_DIR = Path.home() / 'Library' / 'LaunchAgents'
LAUNCHCTL = '/bin/launchctl'
OSASCRIPT = '/usr/bin/osascript'
PACKAGE_DIR = Path(__file__).resolve().parent
NOT_LOADED = (3, 113)    # launchctl exits 3 (bootout) or 113 (print) for a service that is not loaded
TIMEOUT = 30
LOG_TAIL = 5


def plist_path() -> Path:
    return LAUNCH_AGENTS_DIR / f'{LABEL}.plist'


def log_path(data_dir: Path) -> Path:
    return Path(data_dir) / 'logs' / 'schedule.log'


def _domain() -> str:
    return f'gui/{os.getuid()}'


def _launchctl(*args: str) -> Tuple[List[str], subprocess.CompletedProcess]:
    argv = [LAUNCHCTL, *args]
    return argv, _run(argv, TIMEOUT)


def is_loaded() -> bool:
    argv, r = _launchctl('print', f'{_domain()}/{LABEL}')
    if r.returncode == 0:
        return True
    if r.returncode in NOT_LOADED:
        return False
    raise _fail(argv, r)


def _source_root() -> Optional[str]:
    """The folder to put on PYTHONPATH when the package runs from a source checkout: launchd starts the job with
    a bare environment, so `-m disk_steward` would not find it. None for an installed package."""
    if any(part in ('site-packages', 'dist-packages') for part in PACKAGE_DIR.parts):
        return None
    return str(PACKAGE_DIR.parent)


def build_plist(data_dir: Path, weekday: int, hour: int, pass_data_dir: bool, lang: str) -> Dict[str, Any]:
    # launchd has no LANG, so the locale of the notification is fixed at install time
    args = [sys.executable, '-m', 'disk_steward', 'scan', '--notify']
    if pass_data_dir:
        args += ['--data-dir', str(data_dir)]
    args += ['--lang', lang]
    log = str(log_path(data_dir))
    plist: Dict[str, Any] = {
        'Label': LABEL,
        'ProgramArguments': args,
        'StartCalendarInterval': {'Weekday': weekday, 'Hour': hour, 'Minute': 0},
        'StandardOutPath': log,
        'StandardErrorPath': log,
        'ProcessType': 'Background',
        'LowPriorityIO': True,
    }
    root = _source_root()
    if root is not None:
        plist['EnvironmentVariables'] = {'PYTHONPATH': root}
    return plist


def _write_plist(path: Path, plist: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f'.{path.name}.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            plistlib.dump(dict(plist), f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)   # mkstemp creates 0600; launchd wants the file readable and not writable by others
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def install(data_dir: Path, weekday: int, hour: int, pass_data_dir: bool, lang: str) -> Dict[str, Any]:
    """Write the plist, then (re)load it. Returns the plist. Any launchctl failure raises RuntimeError."""
    data_dir = Path(os.path.abspath(data_dir))   # the job starts in /, so a relative --data-dir would point elsewhere
    plist = build_plist(data_dir, weekday, hour, pass_data_dir, lang)
    log = log_path(data_dir)
    log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)   # launchd does not create the log's folder
    _write_plist(plist_path(), plist)
    if is_loaded():
        argv, r = _launchctl('bootout', f'{_domain()}/{LABEL}')
        if r.returncode != 0:
            raise _fail(argv, r)
    argv, r = _launchctl('bootstrap', _domain(), str(plist_path()))
    if r.returncode != 0:
        raise _fail(argv, r)
    return plist


def remove() -> bool:
    """Unload the job and delete its plist. False when nothing was installed. A job that is not loaded (e.g. after
    a failed bootstrap) is fine: the plist is ours, so it is still removed."""
    path = plist_path()
    argv, r = _launchctl('bootout', f'{_domain()}/{LABEL}')
    if r.returncode != 0 and r.returncode not in NOT_LOADED:
        raise _fail(argv, r)
    if not path.exists():
        return r.returncode == 0
    path.unlink()
    return True


def status(data_dir: Path) -> Dict[str, Any]:
    """What is on disk and in launchd. The schedule and the log path are read back from the installed plist."""
    path = plist_path()
    installed = path.exists()
    plist: Mapping[str, Any] = {}
    if installed:
        with open(path, 'rb') as f:
            plist = plistlib.load(f)
    when = plist.get('StartCalendarInterval') or {}
    log = Path(plist.get('StandardOutPath') or log_path(data_dir))
    try:
        tail = log.read_text(encoding='utf-8', errors='replace').splitlines()[-LOG_TAIL:]
    except FileNotFoundError:
        tail = []
    return {'plist': str(path), 'installed': installed, 'loaded': is_loaded(), 'weekday': when.get('Weekday'),
            'hour': when.get('Hour'), 'log': str(log), 'log_tail': tail}


# notification -----------------------------------------------------------------------------------------------

def applescript_string(text: str) -> str:
    """`text` as the inside of an AppleScript string literal. Quotes and backslashes are escaped; a control
    character (a file name may hold a newline) becomes a space."""
    flat = ''.join(' ' if ord(c) < 32 else c for c in text)
    return flat.replace('\\', '\\\\').replace('"', '\\"')


def notification_text(overview: Mapping[str, Any], locale: str) -> Tuple[str, str]:
    """(subtitle, body): the junk offered under the default filter, then the growth since the previous scan."""
    def T(key: str, **args: Any) -> str:
        return i18n.t(key, locale, **args)

    subtitle = T('cli.notify.subtitle', offered=fmt_bytes(overview['offered']['alloc']))
    growth = overview['growth']
    delta = growth['alloc_delta']
    if delta is None:
        return subtitle, T('cli.growth.none')
    top = next((g for g in growth['groups'] if g['delta'] > 0), None)   # groups are ordered by |change|
    if top is None:
        return subtitle, T('cli.notify.growth', delta=fmt_bytes(delta, signed=True))
    return subtitle, T('cli.notify.growth_top', delta=fmt_bytes(delta, signed=True), group=group_label(top, locale),
                       group_delta=fmt_bytes(top['delta'], signed=True))


def notify(subtitle: str, body: str) -> None:
    """Post one notification. Raises RuntimeError with osascript's message when it refuses."""
    script = (f'display notification "{applescript_string(body)}" with title "Disk Steward" '
              f'subtitle "{applescript_string(subtitle)}"')
    argv = [OSASCRIPT, '-e', script]
    r = _run(argv, TIMEOUT)
    if r.returncode != 0:
        raise _fail(argv, r)
