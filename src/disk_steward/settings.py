"""User settings: settings.json in the data dir."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple, Union

from . import i18n
from .model import Domain, ErrorCode, StewardError, asdict
from .paths import split_logical

FILENAME = 'settings.json'
MIB = 1024 * 1024

Number = Union[int, float]


@dataclass(frozen=True)
class Settings:
    locale: Optional[str] = None                      # None = detect
    domains: Tuple[str, ...] = ('agent', 'app-library')
    default_min_age_days: Number = 14
    large_file_min_bytes: int = 500 * MIB
    idle_file_min_bytes: int = 50 * MIB
    idle_days: Number = 180
    installer_age_days: Number = 30
    project_idle_days: Number = 60
    user_roots: Tuple[str, ...] = ('~/Downloads', '~/Desktop', '~/Documents', '~/Movies')
    advisor_enabled: bool = False
    advisor_provider: Optional[str] = None

    def __post_init__(self) -> None:
        for f in dataclasses.fields(self):
            object.__setattr__(self, f.name, _VALIDATORS[f.name](f.name, getattr(self, f.name)))


def _bad(name: str) -> StewardError:
    return StewardError(ErrorCode.INVALID, 'error.invalid_setting', field=name)


def _locale(name: str, v: Any) -> Optional[str]:
    if v is None or v in i18n.LOCALES:
        return v
    raise _bad(name)


def _str_tuple(name: str, v: Any) -> Tuple[str, ...]:
    if not isinstance(v, (list, tuple)) or not all(isinstance(x, str) for x in v):
        raise _bad(name)
    return tuple(v)


def _domains(name: str, v: Any) -> Tuple[str, ...]:
    out = _str_tuple(name, v)
    if not set(out) <= {d.value for d in Domain}:
        raise _bad(name)
    return out


def _roots(name: str, v: Any) -> Tuple[str, ...]:
    out = _str_tuple(name, v)
    for root in out:
        try:
            split_logical(root)
        except StewardError as e:
            raise _bad(name) from e
    return out


def _days(name: str, v: Any) -> Number:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        raise _bad(name)
    # 14.0 and 14 must hash the same
    return int(v) if float(v).is_integer() else v


def _bytes(name: str, v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise _bad(name)
    return v


def _flag(name: str, v: Any) -> bool:
    if not isinstance(v, bool):
        raise _bad(name)
    return v


def _provider(name: str, v: Any) -> Optional[str]:
    if v is None or (isinstance(v, str) and v):
        return v
    raise _bad(name)


_VALIDATORS: Dict[str, Callable[[str, Any], Any]] = {
    'locale': _locale,
    'domains': _domains,
    'default_min_age_days': _days,
    'large_file_min_bytes': _bytes,
    'idle_file_min_bytes': _bytes,
    'idle_days': _days,
    'installer_age_days': _days,
    'project_idle_days': _days,
    'user_roots': _roots,
    'advisor_enabled': _flag,
    'advisor_provider': _provider,
}


def replace(settings: Settings, **patch: Any) -> Settings:
    for key in patch:
        if key not in _VALIDATORS:
            raise _bad(key)
    return dataclasses.replace(settings, **patch)


def load(data_dir: Path) -> Settings:
    try:
        raw = (Path(data_dir) / FILENAME).read_text(encoding='utf-8')
    except FileNotFoundError:
        return Settings()  # first run
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise StewardError(ErrorCode.INVALID, 'error.settings_corrupt', reason=str(e)) from e
    if not isinstance(data, dict):
        raise StewardError(ErrorCode.INVALID, 'error.settings_corrupt', reason='top level is not an object')
    return replace(Settings(), **data)


def save(data_dir: Path, settings: Settings) -> None:
    data_dir = Path(data_dir)
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    body = json.dumps(asdict(settings), indent=2, sort_keys=True) + '\n'
    fd, tmp = tempfile.mkstemp(dir=str(data_dir), prefix='.settings.', suffix='.tmp')  # created 0600
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, data_dir / FILENAME)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    dir_fd = os.open(str(data_dir), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


# Everything except locale, advisor_* and default_min_age_days: none of them changes classification, and plans
# carry their own thresholds.
_HASHED = ('domains', 'large_file_min_bytes', 'idle_file_min_bytes', 'idle_days',
           'installer_age_days', 'project_idle_days', 'user_roots')


def settings_hash(settings: Settings) -> str:
    d: Dict[str, Any] = {name: getattr(settings, name) for name in _HASHED}
    d['domains'] = sorted(set(d['domains']))
    d['user_roots'] = sorted(set(d['user_roots']))
    canonical = json.dumps(d, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()
