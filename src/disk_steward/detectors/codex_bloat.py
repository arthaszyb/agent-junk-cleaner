"""Diagnose a Codex session file that grew oversized. Not a registered detector: the portal calls it on demand.

It streams the JSONL once and returns counts and byte sizes only. No message, prompt or output text ever
leaves this function: the only strings in the result are event type labels (restricted to a safe alphabet)
and ISO timestamps."""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional, Tuple

MAX_LINE = 64 * 1024 * 1024   # a longer line is counted as invalid without being parsed
MAX_TYPES = 50                # distinct type labels reported; the rest are pooled as 'other'
SETTINGS_TYPE = 'event_msg/thread_settings_applied'

_LABEL = re.compile(r'^[A-Za-z0-9_.:/\-]{1,64}$')
_TIMESTAMP = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?$')


def _fingerprint(st: os.stat_result) -> Tuple[int, int, int, int]:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


def _label(obj: Dict[str, Any]) -> str:
    kind = obj.get('type')
    payload = obj.get('payload')
    sub = payload.get('type') if isinstance(payload, dict) else None
    parts = [p for p in (kind, sub) if isinstance(p, str)]
    label = '/'.join(parts) if parts else 'untyped'
    return label if _LABEL.match(label) else 'other'


def diagnose(abs_path: str) -> Dict[str, Any]:
    by_type: Dict[str, Dict[str, int]] = {}
    total_bytes = lines = invalid = 0
    settings_first: Optional[str] = None
    settings_last: Optional[str] = None
    with open(abs_path, 'rb') as f:
        before = _fingerprint(os.fstat(f.fileno()))
        while True:
            line = f.readline(MAX_LINE)
            if not line:
                break
            size = len(line)
            oversized = size >= MAX_LINE and not line.endswith(b'\n')
            while oversized:  # drain the rest of the line without keeping it
                rest = f.readline(MAX_LINE)
                size += len(rest)
                oversized = len(rest) >= MAX_LINE and not rest.endswith(b'\n')
            total_bytes += size
            lines += 1
            if oversized or size >= MAX_LINE:
                invalid += 1
                continue
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                invalid += 1
                continue
            if not isinstance(obj, dict):
                invalid += 1
                continue
            label = _label(obj)
            if label not in by_type and len(by_type) >= MAX_TYPES:
                label = 'other'
            slot = by_type.setdefault(label, {'count': 0, 'bytes': 0})
            slot['count'] += 1
            slot['bytes'] += size
            if label == SETTINGS_TYPE:
                ts = obj.get('timestamp')
                if isinstance(ts, str) and _TIMESTAMP.match(ts):
                    settings_first = ts if settings_first is None or ts < settings_first else settings_first
                    settings_last = ts if settings_last is None or ts > settings_last else settings_last
        after_fd = _fingerprint(os.fstat(f.fileno()))
    after_path = _fingerprint(os.stat(abs_path))
    settings = by_type.get(SETTINGS_TYPE, {'count': 0, 'bytes': 0})
    return {
        'bytes': total_bytes,
        'lines': lines,
        'invalid_lines': invalid,
        'by_type': {k: by_type[k] for k in sorted(by_type, key=lambda k: (-by_type[k]['bytes'], k))},
        'settings': {'count': settings['count'], 'bytes': settings['bytes'],
                     'fraction': (settings['bytes'] / total_bytes) if total_bytes else 0.0,
                     'first_timestamp': settings_first, 'last_timestamp': settings_last},
        'changed_during_read': not (before == after_fd == after_path),
    }
