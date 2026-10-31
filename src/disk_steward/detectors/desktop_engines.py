"""Claude Desktop bundles one Claude Code engine per version under claude-code/ and claude-code-vm/."""
from __future__ import annotations

import errno
from typing import Any, Dict, List, Optional

from ..model import Evidence, Refinement, Rule, StatInfo, Tier
from ._common import last_segment, list_dir, newest_version, parse_version, parent_of

name = 'desktop_engines'

PIN_FILE = '.sdk-version'


def _roots(ctx: Any) -> List[str]:
    return sorted({parent_of(p) for rule in ctx.rules for p in rule.patterns})


def _read_pin(ctx: Any, root: str) -> Dict[str, Any]:
    """{'state': 'none'|'ok'|'unreadable', 'version': str|None} for the root's .sdk-version file."""
    try:
        text = (ctx.bases.to_abs(f'{root}/{PIN_FILE}')).read_text(encoding='utf-8')
    except FileNotFoundError:
        return {'state': 'none', 'version': None}
    except (OSError, UnicodeDecodeError) as e:
        if isinstance(e, UnicodeDecodeError) or e.errno in (errno.EACCES, errno.EPERM, errno.EISDIR):
            return {'state': 'unreadable', 'version': None}
        raise
    version = text.strip()
    return {'state': 'ok' if parse_version(version) is not None else 'unreadable', 'version': version or None}


def prepare(ctx: Any) -> Dict[str, Any]:
    roots: Dict[str, Any] = {}
    for root in _roots(ctx):
        entries = list_dir(ctx.bases, root) or []
        versions = sorted((n for n in entries if parse_version(n) is not None),
                          key=lambda n: (parse_version(n), n))
        pin = _read_pin(ctx, root)
        # A pin we cannot read, or one naming a version that is not installed, means we do not know which
        # engine the app still needs: keep all of them.
        unverifiable = pin['state'] == 'unreadable' or (pin['state'] == 'ok' and pin['version'] not in versions)
        roots[root] = {'versions': versions, 'newest': newest_version(versions),
                       'pinned': pin['version'] if pin['state'] == 'ok' else None, 'unverifiable': unverifiable}
    return {'roots': roots}


def refine(rule: Rule, path: str, st: StatInfo, is_dir: bool, facts: Dict[str, Any]) -> Optional[Refinement]:
    version = last_segment(path)
    root = facts['roots'].get(parent_of(path))
    if root is None or parse_version(version) is None:
        return Refinement(skip=True)
    if root['unverifiable']:
        return Refinement(tier=Tier.PROTECTED, evidence=(
            Evidence('evidence.version_unverifiable', {'reason': 'sdk-version'}),))
    if version not in root['versions']:   # created after prepare(): nobody has checked it
        return Refinement(tier=Tier.PROTECTED, evidence=(Evidence('evidence.appeared_during_scan', {}),))
    kept = []
    if version == root['newest']:
        kept.append(Evidence('evidence.engine_newest', {'version': version}))
    if version == root['pinned']:
        kept.append(Evidence('evidence.engine_pinned', {'version': version}))
    if kept:
        return Refinement(tier=Tier.PROTECTED, evidence=tuple(kept))
    return Refinement(evidence=(Evidence('evidence.engine_superseded', {'newest': root['newest']}),))
