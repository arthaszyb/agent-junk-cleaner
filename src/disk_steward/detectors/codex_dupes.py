"""Codex can hold the same session under two ids: a legacy name-derived UUID (version 5) and a new UUID
(version 7). Files are a duplicate set when they share the start time in the file name, the byte size and
the timestamp inside their first (session_meta) line, and each file's first line names the id in its own
file name. The rest of the content must match too: a full compare up to 64 MiB, else eight 1 MiB windows;
the ids themselves are masked because a copy under a new id mentions it. Any mismatch leaves the whole set
unresolved.

Only a set made of one or more version-5 files plus exactly one other file is resolved: each version-5 file
is a legacy copy and goes to the duplicates group, the other file keeps its normal group. Any other shape is
reported as unresolved and left in its normal group, so selecting the duplicates group can never remove
every copy of a session."""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from ..model import Evidence, Refinement, Rule, StatInfo
from ._common import last_segment

name = 'codex_dupes'

SESSIONS = '~/.codex/sessions'
ARCHIVED = '~/.codex/archived_sessions'
INDEX = '~/.codex/session_index.jsonl'
GROUP_KEY = 'codex.sessions:duplicates'
FIRST_LINE_CAP = 1024 * 1024
FULL_COMPARE_MAX = 64 * 1024 * 1024
WINDOWS = 8
WINDOW = 1024 * 1024
CHUNK = 4 * 1024 * 1024
_MASK = b'\0' * 36
_HOLD = len(_MASK) - 1   # an id can straddle two reads by at most this many bytes

_UUID = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
_ROLLOUT = re.compile(rf'^rollout-(\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}-\d{{2}}-\d{{2}})-({_UUID})\.jsonl$')
_UUID_BYTES = re.compile(_UUID.encode('ascii'))


def _rollouts(ctx: Any) -> List[Tuple[str, str, str, int]]:
    """(logical path, start time, uuid, size) of every rollout file, sorted by path."""
    found: List[Tuple[str, str, str, int]] = []
    for top, recurse in ((SESSIONS, True), (ARCHIVED, False)):
        stack = [top]
        while stack:
            logical = stack.pop()
            try:
                with os.scandir(ctx.bases.to_abs(logical)) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except (FileNotFoundError, NotADirectoryError):
                continue
            for e in entries:
                if e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    if recurse:
                        stack.append(f'{logical}/{e.name}')
                    continue
                m = _ROLLOUT.match(e.name)
                if m:
                    found.append((f'{logical}/{e.name}', m.group(1), m.group(2).lower(),
                                  e.stat(follow_symlinks=False).st_size))
    return sorted(found)


def _is_legacy_id(uuid: str) -> bool:
    return uuid[14] == '5'   # the version nibble of the UUID string


def _header(ctx: Any, logical: str, uuid: str) -> Optional[Tuple[str, int]]:
    """(payload.timestamp, byte length of the first line) if the first line is a session_meta whose
    payload.id is the id in the file name; None for a missing, oversized or inconsistent first line."""
    with open(ctx.bases.to_abs(logical), 'rb') as f:
        line = f.readline(FIRST_LINE_CAP)
    if not line.endswith(b'\n') and len(line) >= FIRST_LINE_CAP:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    payload = obj.get('payload') if isinstance(obj, dict) else None
    if not isinstance(payload, dict):
        return None
    ts, session_id = payload.get('timestamp'), payload.get('id')
    if not (isinstance(ts, str) and ts and isinstance(session_id, str) and session_id.lower() == uuid):
        return None
    return ts, len(line)


def _mask(buf: bytes, ids: List[bytes]) -> bytes:
    for i in ids:
        buf = buf.replace(i, _MASK)
    return buf


def _digest(ctx: Any, logical: str, size: int, first_len: int, ids: List[bytes]) -> str:
    """SHA-256 of the content after the first line with the ids masked (all of it, or eight windows)."""
    h = hashlib.sha256()
    with open(ctx.bases.to_abs(logical), 'rb') as f:
        if size <= FULL_COMPARE_MAX:
            f.seek(first_len)
            tail = b''
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                buf = _mask(tail + chunk, ids)
                keep = min(_HOLD, len(buf))
                h.update(buf[:len(buf) - keep])
                tail = buf[len(buf) - keep:]
            h.update(tail)
        else:
            rest = size - first_len
            for i in range(WINDOWS):
                start = first_len + i * (rest - WINDOW) // (WINDOWS - 1)
                lo = max(first_len, start - _HOLD)   # read a margin so an id on a window edge is masked whole
                f.seek(lo)
                buf = _mask(f.read(start - lo + WINDOW + _HOLD), ids)
                h.update(buf[start - lo:start - lo + WINDOW])
    return h.hexdigest()


def _indexed_ids(ctx: Any, wanted: Set[str]) -> Set[str]:
    """Which of the wanted uuids occur in session_index.jsonl. Streams the file; nothing else is kept."""
    seen: Set[str] = set()
    if not wanted:
        return seen
    try:
        f = open(ctx.bases.to_abs(INDEX), 'rb')
    except FileNotFoundError:
        return seen
    with f:
        for line in f:
            for m in _UUID_BYTES.findall(line):
                u = m.decode('ascii').lower()
                if u in wanted:
                    seen.add(u)
    return seen


def prepare(ctx: Any) -> Dict[str, Any]:
    by_key: Dict[Tuple[str, int], List[Tuple[str, str]]] = {}
    for logical, start, uuid, size in _rollouts(ctx):
        by_key.setdefault((start, size), []).append((logical, uuid))
    sets: List[Tuple[int, List[Tuple[str, str]], bool]] = []   # (size, members, content matches)
    for (start, size) in sorted(by_key):
        members = by_key[(start, size)]
        if len(members) < 2:
            continue
        by_meta: Dict[str, List[Tuple[str, str, int]]] = {}
        for logical, uuid in members:
            header = _header(ctx, logical, uuid)
            if header is not None:
                by_meta.setdefault(header[0], []).append((logical, uuid, header[1]))
        for _, found in sorted(by_meta.items()):
            if len(found) < 2:
                continue
            found.sort()
            ids = [u.encode('ascii') for _, u, _ in found]
            digests = {_digest(ctx, logical, size, first_len, ids) for logical, _, first_len in found}
            sets.append((size, [(logical, uuid) for logical, uuid, _ in found], len(digests) == 1))
    indexed = _indexed_ids(ctx, {uuid for _, members, _ in sets for _, uuid in members})
    dupes: Dict[str, Any] = {}
    for _, members, same in sets:
        both = all(uuid in indexed for _, uuid in members)
        legacy = [(p, u) for p, u in members if _is_legacy_id(u)]
        current = [(p, u) for p, u in members if not _is_legacy_id(u)]
        resolved = same and bool(legacy) and len(current) == 1
        for logical, uuid in members:
            if resolved and _is_legacy_id(uuid):
                role, partner = 'legacy', current[0][0]
            elif resolved:
                role, partner = 'kept', legacy[0][0]
            else:
                role, partner = 'unresolved', next(p for p, _ in members if p != logical)
            dupes[logical] = {'role': role, 'partner': last_segment(partner), 'both_indexed': both}
    return {'dupes': dupes}


def refine(rule: Rule, path: str, st: StatInfo, is_dir: bool, facts: Dict[str, Any]) -> Optional[Refinement]:
    entry = facts['dupes'].get(path)
    if entry is None:
        return None
    args = {'partner': entry['partner']}
    if entry['role'] == 'legacy':
        evidence = [Evidence('evidence.codex_legacy_copy', args)]
        if entry['both_indexed']:
            evidence.append(Evidence('evidence.codex_both_indexed', {}))
        return Refinement(group_key=GROUP_KEY, evidence=tuple(evidence))
    if entry['role'] == 'kept':
        return Refinement(evidence=(Evidence('evidence.codex_duplicate_kept', args),))
    return Refinement(evidence=(Evidence('evidence.codex_duplicate_unresolved', args),))
