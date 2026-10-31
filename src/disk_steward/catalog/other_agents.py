"""Other AI tools: VS Code family editors (Cursor, Antigravity, Windsurf) and Ollama."""
from __future__ import annotations

from typing import Tuple

from . import ScanRoot, vscode_family_rules
from ..model import Action, Domain, Kind, Rule, Tier, Unit

D = Domain.AGENT
_EDITORS = (
    # id prefix, support dir, extensions dir, bundle id
    ('cursor', '~/Library/Application Support/Cursor', '~/.cursor/extensions', 'com.todesktop.230313mzl4w4u92'),
    ('antigravity', '~/Library/Application Support/Antigravity', '~/.antigravity/extensions',
     'com.google.antigravity'),
    ('windsurf', '~/Library/Application Support/Windsurf', '~/.windsurf/extensions', 'com.exafunction.windsurf'),
)
_SHIPIT = (('antigravity', 'com.google.antigravity'), ('cursor', 'com.todesktop.230313mzl4w4u92'))

ROOTS = tuple(ScanRoot(p, D) for p in (
    '~/Library/Application Support/Cursor', '~/Library/Application Support/Antigravity',
    '~/Library/Application Support/Windsurf',
    '~/Library/Caches/com.google.antigravity.ShipIt', '~/Library/Caches/com.todesktop.230313mzl4w4u92.ShipIt',
    '~/.cursor', '~/.antigravity', '~/.windsurf', '~/.ollama', '~/.gemini'))


def rules(settings: object) -> Tuple[Rule, ...]:
    out: Tuple[Rule, ...] = ()
    for prefix, support, ext, bundle in _EDITORS:
        out += vscode_family_rules(prefix, prefix, D, support, ext, bundle)
    for prefix, bundle in _SHIPIT:
        out += (Rule(id=f'{prefix}.shipit', provider=prefix, domain=D, tier=Tier.JUNK, kind=Kind.UPDATE_LEFTOVER,
                     patterns=(f'~/Library/Caches/{bundle}.ShipIt/**',), min_age_floor_days=1,
                     actions=(Action.DELETE,), default_action=Action.DELETE, quit_apps=(bundle,), priority=300),)
    return out + (
        Rule(id='ollama.models', provider='ollama', domain=D, tier=Tier.REVIEW, kind=Kind.MODEL,
             patterns=('~/.ollama/models',), unit=Unit.BUNDLE, hint='ollama list  # then: ollama rm <model>',
             priority=300),
    )
