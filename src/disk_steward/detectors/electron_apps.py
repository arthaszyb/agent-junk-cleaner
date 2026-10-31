"""Electron caches under ~/Library/Application Support/<App>: name the installed app that owns the folder, so the
executor can ask for it to be quit. A folder that matches no installed app is still cache, just without a quit."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..model import Evidence, Refinement, Rule, StatInfo

name = 'electron_apps'

# Names an app's support folder may carry: the .app file name or one of three Info.plist strings.
_NAME_FIELDS = ('stem', 'name', 'display_name', 'executable')


def prepare(ctx: Any) -> Dict[str, Any]:
    owners: Dict[str, Dict[str, str]] = {}
    for bundle_id, info in ctx.mac.installed_app_info().items():
        for field in _NAME_FIELDS:
            if info[field]:
                owners.setdefault(info[field].lower(), {})[bundle_id] = info['stem']
    return {'apps': {n: sorted([b, app] for b, app in found.items()) for n, found in sorted(owners.items())}}


def refine(rule: Rule, path: str, st: StatInfo, is_dir: bool, facts: Dict[str, Any]) -> Optional[Refinement]:
    folder = path.split('/', 4)[3]   # ~ / Library / Application Support / <App> / ...
    found: List[List[str]] = facts['apps'].get(folder.lower(), [])
    if not found:
        return Refinement(evidence=(Evidence('evidence.app_not_installed', {'id': folder}),))
    # Two installed apps can share a name; every one of them must be quit.
    return Refinement(evidence=tuple(Evidence('evidence.requires_quit', {'bundle_id': b, 'app': app})
                                     for b, app in found))
