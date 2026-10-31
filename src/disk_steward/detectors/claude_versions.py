"""Claude CLI installs under ~/.local/share/claude/versions: keep the one the `claude` command runs and the newest."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ..model import Evidence, Refinement, Rule, StatInfo, StewardError, Tier
from ..paths import Bases
from ._common import last_segment, list_dir, newest_version, parse_version

name = 'claude_versions'

LINK = '~/.local/bin/claude'
VERSIONS_DIR = '~/.local/share/claude/versions'
_MAX_HOPS = 8


def _current(bases: Bases) -> Tuple[str, Optional[str]]:
    """(status, version entry the claude command points to). The chain is followed link by link instead of
    with realpath so that a link pointing outside the versions directory is reported as such."""
    link = str(bases.to_abs(LINK))
    path = link
    for _ in range(_MAX_HOPS):
        if not os.path.islink(path):
            break
        target = os.readlink(path)
        path = os.path.normpath(os.path.join(os.path.dirname(path), target))
    else:
        return 'unresolvable', None
    if path == link:
        return ('missing' if not os.path.lexists(link) else 'unresolvable'), None
    if not os.path.lexists(path):
        return 'unresolvable', None
    try:
        logical = bases.to_logical(Path(path))
    except StewardError:
        return 'outside', None
    prefix = VERSIONS_DIR + '/'
    if not logical.startswith(prefix):
        return 'outside', None
    return 'ok', logical[len(prefix):].split('/', 1)[0]


def prepare(ctx: Any) -> Dict[str, Any]:
    names = list_dir(ctx.bases, VERSIONS_DIR) or []
    versions = sorted((n for n in names if parse_version(n) is not None), key=lambda n: (parse_version(n), n))
    status, current = _current(ctx.bases)
    if status == 'ok' and current not in versions:
        status, current = 'unresolvable', None  # the link names something that is not an installed version
    return {'status': status, 'current': current, 'newest': newest_version(versions), 'versions': versions}


def refine(rule: Rule, path: str, st: StatInfo, is_dir: bool, facts: Dict[str, Any]) -> Optional[Refinement]:
    version = last_segment(path)
    if parse_version(version) is None:
        return Refinement(skip=True)
    if facts['status'] != 'ok':
        return Refinement(tier=Tier.PROTECTED, evidence=(
            Evidence('evidence.version_unverifiable', {'reason': facts['status']}),))
    if version not in facts['versions']:   # created after prepare(): nobody has checked it
        return Refinement(tier=Tier.PROTECTED, evidence=(Evidence('evidence.appeared_during_scan', {}),))
    if version in (facts['current'], facts['newest']):
        return Refinement(tier=Tier.PROTECTED, evidence=(Evidence('evidence.current_version', {
            'version': version, 'current': facts['current'], 'newest': facts['newest']}),))
    return Refinement(evidence=(Evidence('evidence.not_current_version', {
        'current': facts['current'], 'newest': facts['newest']}),))
