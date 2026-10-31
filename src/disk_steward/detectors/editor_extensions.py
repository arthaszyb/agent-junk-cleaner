"""VS Code family editors list extension folders they no longer want in <extensions>/.obsolete."""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..model import Evidence, Refinement, Rule, StatInfo
from ._common import last_segment, parent_of, read_json

name = 'editor_extensions'


def prepare(ctx: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for ext_dir in sorted({parent_of(p) for rule in ctx.rules for p in rule.patterns}):
        status, data = read_json(ctx.bases.to_abs(f'{ext_dir}/.obsolete'))
        if status == 'ok' and not isinstance(data, dict):
            status, data = 'invalid', None
        out[ext_dir] = {'status': status, 'obsolete': sorted(k for k, v in data.items() if v) if status == 'ok' else []}
    return {'dirs': out}


def refine(rule: Rule, path: str, st: StatInfo, is_dir: bool, facts: Dict[str, Any]) -> Optional[Refinement]:
    entry = facts['dirs'].get(parent_of(path))
    folder = last_segment(path)
    if entry is None or folder not in entry['obsolete']:
        return Refinement(skip=True)  # not marked obsolete (or no readable list): the editor may still use it
    return Refinement(evidence=(Evidence('evidence.extension_obsolete', {'name': folder}),))
