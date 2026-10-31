"""Keep the newest of several sibling folders (VS Code's CachedData/<commit> folders)."""
from __future__ import annotations

import os
import stat
from typing import Any, Dict, Optional

from ..fsutil import stat_nofollow
from ..model import Evidence, Refinement, Rule, StatInfo, Tier
from ._common import last_segment, parent_of

name = 'newest_dir'

_GLOBS = '*?['


def prepare(ctx: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    stamps: Dict[str, Any] = {}
    candidates: Dict[str, Any] = {}
    for rule in ctx.rules:
        for pattern in rule.patterns:
            parent = parent_of(pattern)
            if any(c in parent for c in _GLOBS) or last_segment(pattern) != '*':
                raise ValueError(f'newest_dir needs a pattern of the form <literal dir>/*, got {pattern!r}')
            newest: Optional[tuple] = None
            dirs = []
            try:
                names = sorted(os.listdir(ctx.bases.to_abs(parent)))
            except FileNotFoundError:
                names = []
            for n in names:
                info = stat_nofollow(ctx.bases.to_abs(f'{parent}/{n}'))
                if not stat.S_ISDIR(info.mode):
                    continue
                dirs.append(n)
                if newest is None or (info.changed_ns, n) > newest:
                    newest = (info.changed_ns, n)
            out[parent] = newest[1] if newest else None
            stamps[parent] = newest[0] if newest else None
            candidates[parent] = dirs
    return {'newest': out, 'newest_ns': stamps, 'candidates': candidates}


def refine(rule: Rule, path: str, st: StatInfo, is_dir: bool, facts: Dict[str, Any]) -> Optional[Refinement]:
    folder = last_segment(path)
    parent = parent_of(path)
    newest = facts['newest'].get(parent)
    if folder == newest:
        return Refinement(tier=Tier.PROTECTED, evidence=(Evidence('evidence.newest_dir', {'name': folder}),))
    stamp = facts['newest_ns'].get(parent)
    # A folder that is new since prepare(), or has been touched past the recorded newest one, may be the
    # newest now; the facts cannot say, so keep it.
    if folder not in facts['candidates'].get(parent, ()) or (stamp is not None and st.changed_ns > stamp):
        return Refinement(tier=Tier.PROTECTED, evidence=(Evidence('evidence.appeared_during_scan', {}),))
    return None
