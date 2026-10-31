"""Dependency folders (node_modules, .venv, venv, target) of projects the user has not touched for a while.

The folder only counts as a dependency folder when the project next to it says so (package.json, Cargo.toml) or
it says so itself (pyvenv.cfg). An idle project makes it a REVIEW item; an active one keeps it protected."""
from __future__ import annotations

import errno
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from ..model import Evidence, Refinement, Rule, StatInfo, Tier
from ._common import last_segment

name = 'project_deps'

DAY = 86400
# The marker file: inside the folder for virtualenvs, beside it for the others.
_INSIDE = {'.venv': 'pyvenv.cfg', 'venv': 'pyvenv.cfg'}
_BESIDE = {'node_modules': 'package.json', 'target': 'Cargo.toml'}
# A dependency folder inside one of these belongs to the outer folder. 'target' is left out: it is also an
# ordinary folder name, and whether it is a build folder depends on the Cargo.toml beside it.
_NESTING = ('node_modules', '.venv', 'venv')


def prepare(ctx: Any) -> Dict[str, Any]:
    # refine() reads the project folder, and a detector has no Bases of its own.
    return {'home': str(ctx.bases.home), 'idle_days': ctx.settings.project_idle_days}


def _activity_ns(project: Path, deps_name: str) -> int:
    """Newest change among the project's own entries, skipping the dependency folder, and the git index or HEAD."""
    newest = 0
    with os.scandir(project) as it:
        for entry in it:
            if entry.name != deps_name:
                newest = max(newest, StatInfo.from_stat(entry.stat(follow_symlinks=False)).changed_ns)
    for marker in ('index', 'HEAD'):
        try:
            newest = max(newest, os.lstat(project / '.git' / marker).st_mtime_ns)
        except (FileNotFoundError, NotADirectoryError):
            pass   # no git, or a worktree whose .git is a file
    if newest == 0:   # a folder holding nothing but the virtualenv: its own age is all there is
        newest = StatInfo.from_stat(os.lstat(project)).changed_ns
    return newest


def refine(rule: Rule, path: str, st: StatInfo, is_dir: bool, facts: Dict[str, Any]) -> Optional[Refinement]:
    folder = last_segment(path)
    segments = path.split('/')
    if any(s in _NESTING for s in segments[:-1]):
        return Refinement(skip=True)
    project = Path(facts['home']).joinpath(*segments[1:-1])
    marker = (project / folder / _INSIDE[folder]) if folder in _INSIDE else (project / _BESIDE[folder])
    try:
        if not marker.is_file():
            return Refinement(skip=True)
        newest = _activity_ns(project, folder)
    except (FileNotFoundError, NotADirectoryError):
        return Refinement(skip=True)   # gone since the scanner listed it
    except PermissionError as e:
        return Refinement(tier=Tier.PROTECTED, evidence=(
            Evidence('evidence.project_unreadable', {'reason': errno.errorcode.get(e.errno, str(e.errno))}),))
    age = time.time() - newest / 1e9
    days = int(age // DAY)
    if age > facts['idle_days'] * DAY:
        return Refinement(evidence=(Evidence('evidence.project_idle', {'days': days}),))
    return Refinement(tier=Tier.PROTECTED, evidence=(Evidence('evidence.project_active', {'days': days}),))
