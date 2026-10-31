"""Dynamic refinements of catalog rules. A detector reads a little disk state in prepare() (the facts, hashed
into every scan) and refine() uses those facts to skip a rule, protect an item or regroup it."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol, Tuple

from ..macos import MacOS
from ..model import Refinement, Rule, StatInfo
from ..paths import Bases
from ..settings import Settings


@dataclass(frozen=True)
class DetectorContext:
    bases: Bases
    mac: MacOS
    settings: Settings
    # Catalog rules that name this detector; compute_facts fills it. Detectors shared by several products
    # (newest_dir, editor_extensions) read their directories from it instead of repeating them.
    rules: Tuple[Rule, ...] = ()


class Detector(Protocol):
    name: str

    def prepare(self, ctx: DetectorContext) -> Dict[str, Any]: ...

    def refine(self, rule: Rule, path: str, st: StatInfo, is_dir: bool,
               facts: Dict[str, Any]) -> Optional[Refinement]: ...


from . import (claude_plugins, claude_versions, codex_dupes, codex_state_tmp, desktop_engines,  # noqa: E402
               editor_extensions, electron_apps, newest_dir, orphans, project_deps)

REGISTRY: Dict[str, Detector] = {m.name: m for m in (  # type: ignore[misc]
    claude_versions, desktop_engines, claude_plugins, codex_dupes, codex_state_tmp, editor_extensions, newest_dir,
    electron_apps, orphans, project_deps)}


def facts_digest(facts: Dict[str, Any]) -> str:
    # json.dumps raises on anything that is not JSON-safe, which is the check we want for detector facts.
    canonical = json.dumps(facts, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def compute_facts(ctx: DetectorContext, catalog: Any) -> Tuple[Dict[str, Any], str]:
    """Run prepare() of every detector some catalog rule refers to; returns (facts, facts_hash)."""
    facts: Dict[str, Any] = {}
    for name in sorted({r.detector for r in catalog.rules if r.detector}):
        detector = REGISTRY.get(name)
        if detector is None:
            raise ValueError(f'catalog refers to unknown detector {name!r}')
        scoped = dataclasses.replace(ctx, rules=tuple(r for r in catalog.rules if r.detector == name))
        facts[name] = detector.prepare(scoped)
    return facts, facts_digest(facts)
