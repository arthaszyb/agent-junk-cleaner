"""Plugin cache entries Claude Code still lists in installed_plugins.json must stay."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..model import Evidence, Refinement, Rule, StatInfo, StewardError, Tier
from ._common import read_json

name = 'claude_plugins'

MANIFEST = '~/.claude/plugins/installed_plugins.json'
CACHE = '~/.claude/plugins/cache'


def _install_paths(data: Any) -> Optional[List[str]]:
    """installPath of every entry, or None if the document does not have the shape of version 2."""
    if not isinstance(data, dict) or data.get('version') != 2 or not isinstance(data.get('plugins'), dict):
        return None
    out: List[str] = []
    for entries in data['plugins'].values():
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get('installPath'), str) \
                    or not entry['installPath'].startswith('/'):
                return None
            out.append(entry['installPath'])
    return out


def _cache_versions(ctx: Any) -> Optional[List[str]]:
    """Every <marketplace>/<plugin>/<version> folder that exists now, as logical paths; None if a folder on
    the way cannot be listed."""
    level = [CACHE]
    for _ in range(3):
        nxt: List[str] = []
        for logical in level:
            try:
                with os.scandir(ctx.bases.to_abs(logical)) as it:
                    nxt.extend(f'{logical}/{e.name}' for e in it if e.is_dir(follow_symlinks=False))
            except (FileNotFoundError, NotADirectoryError):
                continue
            except PermissionError:
                return None
        level = nxt
    return sorted(level)


def prepare(ctx: Any) -> Dict[str, Any]:
    cache = _cache_versions(ctx)
    if cache is None:
        return {'status': 'unreadable', 'installed': [], 'cache': []}
    status, data = read_json(ctx.bases.to_abs(MANIFEST))
    if status != 'ok':
        return {'status': status, 'installed': [], 'cache': cache}
    paths = _install_paths(data)
    if paths is None:
        return {'status': 'invalid', 'installed': [], 'cache': cache}
    home = os.path.realpath(ctx.bases.home)
    installed = set()
    for p in paths:
        real = os.path.realpath(p)   # the manifest may spell the path through a symlink
        try:
            installed.add(ctx.bases.to_logical(Path(real)))
        except StewardError:
            if real == home or real.startswith(home + os.sep):
                # Inside the home but not expressible as a logical path: we cannot tell which cache
                # entry it is, so none may be offered.
                return {'status': 'unverifiable', 'installed': [], 'cache': cache}
            # Outside the home entirely: it cannot be one of the cache entries we look at.
    return {'status': 'ok', 'installed': sorted(installed), 'cache': cache}


def refine(rule: Rule, path: str, st: StatInfo, is_dir: bool, facts: Dict[str, Any]) -> Optional[Refinement]:
    if facts['status'] != 'ok':
        return Refinement(tier=Tier.PROTECTED, evidence=(
            Evidence('evidence.plugin_manifest_unreadable', {'reason': facts['status']}),))
    if path not in facts['cache']:   # created after prepare(): nobody has checked it
        return Refinement(tier=Tier.PROTECTED, evidence=(Evidence('evidence.appeared_during_scan', {}),))
    for installed in facts['installed']:
        # An install path may be the version folder or lie below or above it; all of them keep it.
        if installed == path or installed.startswith(path + '/') or path.startswith(installed + '/'):
            return Refinement(tier=Tier.PROTECTED, evidence=(Evidence('evidence.plugin_installed', {}),))
    return Refinement(evidence=(Evidence('evidence.plugin_not_in_manifest', {}),))
