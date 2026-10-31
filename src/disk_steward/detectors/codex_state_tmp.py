"""Leftover atomic-write files of Codex's global state are junk only while the real state file exists."""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from ..model import Refinement, Rule, StatInfo

name = 'codex_state_tmp'

STATE_FILE = '~/.codex/.codex-global-state.json'


def prepare(ctx: Any) -> Dict[str, Any]:
    # If the state file is gone, a temp file may be the only copy of the user's state.
    return {'state_file': os.path.isfile(ctx.bases.to_abs(STATE_FILE))}


def refine(rule: Rule, path: str, st: StatInfo, is_dir: bool, facts: Dict[str, Any]) -> Optional[Refinement]:
    return None if facts['state_file'] else Refinement(skip=True)
