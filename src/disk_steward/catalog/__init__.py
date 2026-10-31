"""The rule catalog: what may be cleaned, what must never be, and where to look.

Each module listed in _MODULES exposes `rules(settings)` and may expose `ROOTS` (or `roots(settings)`)
and `PRIVACY_SKIP`. Adding an area to the tool is one line in _MODULES."""
from __future__ import annotations

import hashlib
import importlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from ..model import (CATALOG_SCHEMA, Action, Domain, Kind, Rule, Tier, Unit, asdict)
from ..paths import PREFIXES, split_logical

_MODULES = ('protections', 'claude', 'codex', 'other_agents', 'app_library', 'user_files', 'dev_shared')

_ID_RE = re.compile(r'^[a-z0-9][a-z0-9.\-]*$')
_ELECTRON_CACHE_DIRS = ('Cache', 'Code Cache', 'GPUCache', 'DawnCache', 'DawnWebGPUCache', 'DawnGraphiteCache',
                        'GraphiteDawnCache', 'ShaderCache', 'GPUPersistentCache')


@dataclass(frozen=True)
class ScanRoot:
    path: str          # logical
    domain: Domain     # fallback domain for unmatched files under it


@dataclass(frozen=True)
class Catalog:
    rules: Tuple[Rule, ...]          # sorted by priority desc, then id
    roots: Tuple[ScanRoot, ...]
    privacy_skip: Tuple[str, ...]
    hash: str
    _by_id: Dict[str, Rule] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, '_by_id', {r.id: r for r in self.rules})

    def rule(self, rule_id: str) -> Rule:
        try:
            return self._by_id[rule_id]
        except KeyError:
            raise KeyError(f'no catalog rule {rule_id!r}') from None


def is_report_only(rule: Rule) -> bool:
    """Report-only rules show size and a native command but offer no action. A hint or the dev-shared
    domain marks that intent, so a forgotten `actions=` on a junk rule is caught by validation."""
    return rule.hint is not None or rule.domain == Domain.DEV_SHARED


def _bad(rule_id: str, why: str) -> ValueError:
    return ValueError(f'catalog rule {rule_id!r}: {why}')


def _check_logical_pattern(owner: str, pattern: str) -> None:
    prefix, _, rest = pattern.partition('/')
    if prefix not in PREFIXES or not rest:
        raise _bad(owner, f'pattern {pattern!r} must start with ~/, $TMPDIR/ or $CACHEDIR/')
    if any(s in ('', '.', '..') for s in rest.split('/')):
        raise _bad(owner, f'pattern {pattern!r} has an empty, "." or ".." segment')


def _validate_rule(rule: Rule) -> None:
    if not _ID_RE.match(rule.id):
        raise _bad(rule.id, 'id must be lowercase letters, digits, dots and dashes')
    if not rule.patterns:
        raise _bad(rule.id, 'no patterns')
    for pattern in rule.patterns:
        _check_logical_pattern(rule.id, pattern)
    for glob in rule.name_include + rule.name_exclude:
        if not glob or '/' in glob:
            raise _bad(rule.id, f'bad name glob {glob!r}')
    if rule.min_size < 0 or rule.min_age_floor_days < 0:
        raise _bad(rule.id, 'negative size or age floor')
    if rule.group_depth is not None and rule.group_depth < 1:
        raise _bad(rule.id, 'group_depth must be at least 1')
    if rule.unit == Unit.BUNDLE and (rule.min_size or rule.name_include or rule.name_exclude):
        raise _bad(rule.id, 'bundle rules match directories; name and size filters do not apply')
    if rule.default_action is not None and rule.default_action not in rule.actions:
        raise _bad(rule.id, 'default_action is not among actions')
    if rule.actions and rule.default_action is None:
        raise _bad(rule.id, 'rule with actions needs a default_action')
    if rule.tier == Tier.PROTECTED or is_report_only(rule):
        if rule.actions:
            raise _bad(rule.id, 'protected and report-only rules cannot have actions')
    elif not rule.actions:
        raise _bad(rule.id, 'junk/review rule needs actions (or a hint to make it report-only)')


def build_catalog(rules: Iterable[Rule], roots: Iterable[ScanRoot], privacy_skip: Iterable[str]) -> Catalog:
    ordered = tuple(sorted(rules, key=lambda r: (-r.priority, r.id)))
    seen: Dict[str, Rule] = {}
    for rule in ordered:
        if rule.id in seen:
            raise _bad(rule.id, 'duplicate id')
        seen[rule.id] = rule
        _validate_rule(rule)
    root_list: Dict[str, ScanRoot] = {}
    for root in roots:
        split_logical(root.path)
        if root.path in root_list:
            raise ValueError(f'duplicate scan root {root.path!r}')
        root_list[root.path] = root
    privacy = tuple(sorted(set(privacy_skip)))
    for pattern in privacy:
        _check_logical_pattern('privacy_skip', pattern)
    sorted_roots = tuple(sorted(root_list.values(), key=lambda r: r.path))
    canonical = json.dumps({'rules': [asdict(r) for r in ordered], 'roots': [asdict(r) for r in sorted_roots],
                            'privacy_skip': list(privacy), 'schema': CATALOG_SCHEMA},
                           sort_keys=True, separators=(',', ':'))
    return Catalog(rules=ordered, roots=sorted_roots, privacy_skip=privacy,
                   hash=hashlib.sha256(canonical.encode('utf-8')).hexdigest())


def _contribution(mod: Any, constant: str, function: str, settings: Any) -> Sequence[Any]:
    fn = getattr(mod, function, None)
    if fn is not None:
        return fn(settings)
    return getattr(mod, constant, ())


def load_catalog(settings: Any) -> Catalog:
    rules: List[Rule] = []
    roots: List[ScanRoot] = []
    privacy: List[str] = []
    for name in _MODULES:
        mod = importlib.import_module(f'{__name__}.{name}')
        rules.extend(mod.rules(settings))
        roots.extend(_contribution(mod, 'ROOTS', 'roots', settings))
        privacy.extend(getattr(mod, 'PRIVACY_SKIP', ()))
    return build_catalog(rules, roots, privacy)


# Helpers shared by the rule modules ----------------------------------------------------------------

def electron_cache_rules(id_prefix: str, provider: str, domain: Domain, support_dir: str, bundle_id: str,
                         priority: int = 300) -> Tuple[Rule, ...]:
    """Chromium/Electron disk caches: rebuilt by the app on the next start, so junk once the app is quit."""
    patterns = tuple(f'{support_dir}/{d}/**' for d in _ELECTRON_CACHE_DIRS)
    patterns += tuple(f'{support_dir}/Partitions/*/{d}/**' for d in _ELECTRON_CACHE_DIRS)
    return (Rule(id=f'{id_prefix}.cache', provider=provider, domain=domain, tier=Tier.JUNK, kind=Kind.CACHE,
                 patterns=patterns, actions=(Action.DELETE,), default_action=Action.DELETE,
                 quit_apps=(bundle_id,), priority=priority),)


def vscode_family_rules(id_prefix: str, provider: str, domain: Domain, support_dir: str, ext_dir: str,
                        bundle_id: str) -> Tuple[Rule, ...]:
    common: Dict[str, Any] = dict(provider=provider, domain=domain, tier=Tier.JUNK, actions=(Action.DELETE,),
                                  default_action=Action.DELETE, quit_apps=(bundle_id,), priority=300)
    return electron_cache_rules(f'{id_prefix}.electron', provider, domain, support_dir, bundle_id) + (
        Rule(id=f'{id_prefix}.vsix', kind=Kind.CACHE, patterns=(f'{support_dir}/CachedExtensionVSIXs/**',),
             **common),
        Rule(id=f'{id_prefix}.cached-data', kind=Kind.OLD_VERSION, patterns=(f'{support_dir}/CachedData/*',),
             unit=Unit.BUNDLE, detector='newest_dir', **common),
        Rule(id=f'{id_prefix}.logs', kind=Kind.LOGS, patterns=(f'{support_dir}/logs/**',),
             min_age_floor_days=1, **common),
        Rule(id=f'{id_prefix}.extensions', kind=Kind.OLD_VERSION, patterns=(f'{ext_dir}/*',),
             unit=Unit.BUNDLE, detector='editor_extensions', **common),
    )
