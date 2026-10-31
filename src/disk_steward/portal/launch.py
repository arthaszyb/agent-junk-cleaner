"""portal.json: how `disk-steward portal --url` finds a running portal and asks it for a fresh launch code."""
from __future__ import annotations

import http.client
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from ..model import ErrorCode, StewardError

FILENAME = 'portal.json'
SECRET_HEADER = 'X-Launch-Secret'


def write_info(data_dir: Path, info: Dict[str, Any]) -> None:
    """Atomically, readable by the owner only: the file holds the secret that mints launch codes."""
    fd, tmp = tempfile.mkstemp(dir=str(data_dir), prefix='.portal.', suffix='.tmp')   # created 0600
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(info, f, indent=2, sort_keys=True)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, Path(data_dir) / FILENAME)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_info(data_dir: Path) -> Optional[Dict[str, Any]]:
    try:
        text = (Path(data_dir) / FILENAME).read_text(encoding='utf-8')
    except FileNotFoundError:
        return None
    bad = StewardError(ErrorCode.INVALID, 'portal.cli.portal_json_bad', path=str(Path(data_dir) / FILENAME))
    try:
        info = json.loads(text)
    except ValueError as e:
        raise bad from e
    if not isinstance(info, dict) or not {'port', 'pid', 'launch_secret'} <= set(info):
        raise bad
    return info


def remove_info(data_dir: Path, pid: int) -> None:
    """Remove portal.json if it is still ours (a portal that started later owns the file it wrote)."""
    try:
        info = read_info(data_dir)
    except StewardError:
        return   # unreadable: not ours to judge
    if info is not None and info['pid'] == pid:
        os.unlink(Path(data_dir) / FILENAME)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists, owned by someone else
    return True


def running_info(data_dir: Path) -> Optional[Dict[str, Any]]:
    """portal.json of a live portal; None when there is none or its process is gone."""
    info = read_info(data_dir)
    if info is None or not pid_alive(int(info['pid'])):
        return None
    return info


def request_launch_url(data_dir: Path) -> str:
    """A fresh one-time URL from the running portal of this data dir."""
    info = running_info(data_dir)
    if info is None:
        raise StewardError(ErrorCode.UNAVAILABLE, 'portal.cli.not_running', data_dir=str(data_dir))
    # http.client talks to the loopback address directly; urllib would route it through a configured HTTP proxy
    conn = http.client.HTTPConnection('127.0.0.1', int(info['port']), timeout=10)
    try:
        conn.request('POST', '/internal/launch-code', body=b'{}', headers={SECRET_HEADER: info['launch_secret']})
        resp = conn.getresponse()
        body = resp.read(65536)   # a hostile listener can send more; a link is under 100 bytes
    except OSError as e:
        raise StewardError(ErrorCode.UNAVAILABLE, 'portal.cli.unreachable', port=int(info['port']), reason=str(e)) from e
    finally:
        conn.close()
    if resp.status != 200:
        raise StewardError(ErrorCode.UNAVAILABLE, 'portal.cli.refused', status=resp.status)
    try:
        url = json.loads(body)['url']
    except (ValueError, KeyError, TypeError):
        url = None
    # Whatever answers on that port is not necessarily our portal; the URL goes to a terminal and a browser.
    port = int(info['port'])
    if not isinstance(url, str) or not re.fullmatch(rf'http://127\.0\.0\.1:{port}/launch\?code=[A-Za-z0-9_-]+', url):
        raise StewardError(ErrorCode.UNAVAILABLE, 'portal.cli.bad_url', port=port)
    return url
