"""Caches and runtimes that developer tools share across projects. Report only: each tool cleans its own store
with a native command, which a rule's hint shows verbatim. A tool without one gets a Move to Trash for each listed
folder instead, so nobody is sent to type an rm by hand."""
from __future__ import annotations

from typing import Optional, Tuple

from . import ScanRoot
from ..model import Action, Domain, Kind, Rule, Tier, Unit

D = Domain.DEV_SHARED
_CACHE, _RUNTIME = Kind.CACHE, Kind.RUNTIME

# The scanner lists a root's children and never classifies the root itself, so a tool whose own folder is a
# root is matched one level down ('<folder>/*') and still forms one group; the others name their folder.
# (rule id, kind, patterns, hint: the tool's own cleanup command, or None when it has none)
_TOOLS: Tuple[Tuple[str, Kind, Tuple[str, ...], Optional[str]], ...] = (
    ('homebrew', _CACHE, ('~/Library/Caches/Homebrew',), 'brew cleanup --prune=all'),
    ('uv', _CACHE, ('~/.cache/uv/*',), 'uv cache prune'),
    ('pip', _CACHE, ('~/.cache/pip/*', '~/Library/Caches/pip'), 'pip cache purge'),
    ('npm', _CACHE, ('~/.npm/_cacache',), 'npm cache clean --force'),
    ('npx', _CACHE, ('~/.npm/_npx',), None),
    ('pnpm', _CACHE, ('~/Library/pnpm/store', '~/.pnpm-store/*', '~/Library/Caches/pnpm'), 'pnpm store prune'),
    ('yarn', _CACHE, ('~/Library/Caches/Yarn',), 'yarn cache clean'),
    ('go-mod', _CACHE, ('~/go/pkg/mod/*',), 'go clean -modcache'),
    ('go-build', _CACHE, ('~/Library/Caches/go-build',), 'go clean -cache'),
    ('cargo', _CACHE, ('~/.cargo/registry/*',), None),
    ('gradle', _CACHE, ('~/.gradle/caches/*',), None),
    ('maven', _CACHE, ('~/.m2/repository/*',), None),
    ('playwright', _CACHE, ('~/Library/Caches/ms-playwright',), 'npx playwright uninstall --all'),
    ('puppeteer', _CACHE, ('~/.cache/puppeteer/*',), None),
    ('node-gyp', _CACHE, ('~/Library/Caches/node-gyp',), None),
    ('puccinialin', _CACHE, ('~/Library/Caches/puccinialin',), None),
    ('coresimulator', _RUNTIME, ('~/Library/Developer/CoreSimulator/Devices',),
     'xcrun simctl delete unavailable  # with Xcode selected: sudo xcode-select -s /Applications/Xcode.app'),
    ('xcode-derived-data', _CACHE, ('~/Library/Developer/Xcode/DerivedData',), None),
    ('xcode-device-support', _CACHE, ('~/Library/Developer/Xcode/iOS DeviceSupport',), None),
    ('colima', _RUNTIME, ('~/.colima/*',), 'colima prune'),
)

# Only walked when the domain is on. The folders under ~/Library/Caches are also reached through app-library's
# own root, which is why they must outrank app.caches there.
ROOTS = tuple(ScanRoot(p, D) for p in (
    '~/.cache/uv', '~/.cache/pip', '~/.cache/puppeteer', '~/.npm', '~/Library/pnpm', '~/.pnpm-store',
    '~/go/pkg/mod', '~/.cargo/registry', '~/.gradle/caches', '~/.m2/repository', '~/.colima', '~/Library/Developer'))


def rules(settings: object) -> Tuple[Rule, ...]:
    return tuple(Rule(id=f'dev.{tool}', provider='devtools', domain=D, tier=Tier.REVIEW, kind=kind, patterns=patterns,
                      unit=Unit.BUNDLE, hint=hint, priority=150, manual_actions=() if hint else (Action.TRASH,))
                 for tool, kind, patterns, hint in _TOOLS)
