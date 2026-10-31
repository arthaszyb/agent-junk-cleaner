"""Application Support folders named after a bundle id that no installed app carries."""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

from ..model import Evidence, Refinement, Rule, StatInfo
from ._common import last_segment

name = 'orphans'

# com.foo.bar: a letters-only first label and at least two more. 'v1.2.3' or 'node-v18.0.1' are not bundle ids.
_REVERSE_DNS = re.compile(r'^[a-z]+(\.[a-z0-9_-]+){2,}$')


def prepare(ctx: Any) -> Dict[str, Any]:
    return {'installed': sorted({bundle_id.lower() for bundle_id in ctx.mac.installed_app_info()})}


def refine(rule: Rule, path: str, st: StatInfo, is_dir: bool, facts: Dict[str, Any]) -> Optional[Refinement]:
    folder = last_segment(path)
    lowered = folder.lower()
    if not _REVERSE_DNS.match(lowered) or lowered.startswith('com.apple.'):
        return Refinement(skip=True)
    for installed in facts['installed']:
        # com.foo.bar and its helper com.foo.bar.helper share data; either may be the folder's name
        if lowered == installed or lowered.startswith(installed + '.') or installed.startswith(lowered + '.'):
            return Refinement(skip=True)
    return Refinement(evidence=(Evidence('evidence.app_not_installed', {'id': folder}),))
