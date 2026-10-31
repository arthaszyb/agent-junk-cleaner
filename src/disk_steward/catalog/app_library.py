"""General Mac app data: caches, logs, update leftovers, Electron caches, VS Code, leftovers of apps that are
gone and the temp folders. The agent-specific rules (priority 300) win over the generic ones here."""
from __future__ import annotations

import dataclasses
from typing import Tuple

from . import ScanRoot, electron_cache_rules, vscode_family_rules
from ..model import Action, Domain, Kind, Rule, Tier, Unit

D = Domain.APP_LIBRARY
CACHES = '~/Library/Caches'
SUPPORT = '~/Library/Application Support'
SPOTIFY = 'com.spotify.client'
# Chromium browsers keep each profile's disk cache in two folders under ~/Library/Caches/<vendor>/<profile>. One
# item per folder: a single Chrome profile holds 10^5 cache files.
BROWSER_CACHE_DIRS = ('Cache', 'Code Cache')
BROWSERS = (   # rule id, folder holding one subfolder per profile, bundle id
    ('app.chrome-cache', f'{CACHES}/Google/Chrome', 'com.google.Chrome'),
    ('app.chrome-beta-cache', f'{CACHES}/Google/Chrome Beta', 'com.google.Chrome.beta'),
    ('app.chrome-canary-cache', f'{CACHES}/Google/Chrome Canary', 'com.google.Chrome.canary'),
    ('app.brave-cache', f'{CACHES}/BraveSoftware/Brave-Browser', 'com.brave.Browser'),
    ('app.edge-cache', f'{CACHES}/Microsoft Edge', 'com.microsoft.edgemac'),
    ('app.arc-cache', f'{CACHES}/Arc', 'company.thebrowser.Browser'),
    ('app.chromium-cache', f'{CACHES}/Chromium', 'org.chromium.Chromium'),
    ('app.vivaldi-cache', f'{CACHES}/Vivaldi', 'com.vivaldi.Vivaldi'),
)

LOG_NAMES = ('*.log', '*.log.*', '*.txt', '*.json', '*.jsonl', '*.gz', '*.ips', '*.crash', '*.diag', '*.spin',
             '*.hang')
_DELETE = dict(actions=(Action.DELETE,), default_action=Action.DELETE)

ROOTS = tuple(ScanRoot(p, D) for p in (
    CACHES, '~/Library/Logs', SUPPORT, '~/.vscode/extensions', '$TMPDIR', '$CACHEDIR'))

# Other apps' scratch space under the per-user temp folders; macOS's own is not ours to list.
PRIVACY_SKIP = ('$TMPDIR/com.apple.*', '$TMPDIR/TemporaryItems', '$CACHEDIR/com.apple.*')


def rules(settings: object) -> Tuple[Rule, ...]:
    def rule(**kw: object) -> Rule:
        return Rule(provider='macos', domain=D, **kw)  # type: ignore[arg-type]

    # The Electron cache folders of every app. The app is only known at scan time, so no quit_apps here: the
    # detector adds an 'evidence.requires_quit' per installed app that owns the folder.
    (electron,) = electron_cache_rules('app.electron', 'macos', D, f'{SUPPORT}/*', 'any', priority=200)
    electron = dataclasses.replace(electron, quit_apps=(), group_depth=4, detector='electron_apps')

    browsers = tuple(
        rule(id=rule_id, tier=Tier.JUNK, kind=Kind.CACHE, unit=Unit.BUNDLE, quit_apps=(bundle_id,), priority=150,
             patterns=tuple(f'{root}/*/{d}' for d in BROWSER_CACHE_DIRS), **_DELETE)
        for rule_id, root, bundle_id in BROWSERS)

    return (electron,) + browsers + vscode_family_rules(
        'vscode', 'vscode', D, f'{SUPPORT}/Code', '~/.vscode/extensions', 'com.microsoft.VSCode') + (
        # Update downloads that stayed behind.
        rule(id='app.sparkle', tier=Tier.JUNK, kind=Kind.UPDATE_LEFTOVER, group_depth=4, priority=120,
             patterns=(f'{CACHES}/*/org.sparkle-project.Sparkle/**',), min_age_floor_days=1, **_DELETE),
        rule(id='app.updater-cache', tier=Tier.JUNK, kind=Kind.UPDATE_LEFTOVER, group_depth=4, priority=110,
             patterns=(f'{CACHES}/*-updater/**',), min_age_floor_days=1, **_DELETE),
        rule(id='app.shipit', tier=Tier.JUNK, kind=Kind.UPDATE_LEFTOVER, group_depth=4, priority=110,
             patterns=(f'{CACHES}/*.ShipIt/**',), min_age_floor_days=1, **_DELETE),
        # Disk Steward's own folder (database, archives): managed on the Archives page, never offered here.
        rule(id='app.steward-data', tier=Tier.PROTECTED, kind=Kind.WORK_PRODUCT, unit=Unit.BUNDLE, priority=800,
             patterns=(f'{SUPPORT}/Disk Steward',)),
        # Spotify keeps the songs you downloaded for offline play in its cache folder.
        rule(id='app.spotify-cache', tier=Tier.REVIEW, kind=Kind.CACHE, quit_apps=(SPOTIFY,), priority=100,
             patterns=(f'{CACHES}/{SPOTIFY}/**',), **_DELETE),
        # What headless Chrome runs (browser-automation tools, AI agents) leave behind; no app to quit.
        rule(id='app.headless-chrome', tier=Tier.JUNK, kind=Kind.TEMP, unit=Unit.BUNDLE, priority=150,
             patterns=(f'{CACHES}/Google/Chrome-headless/scoped_dir*',), min_age_floor_days=1, **_DELETE),
        # Cache.db and friends stay protected by prot.databases, and so do their neighbours.
        rule(id='app.caches', tier=Tier.JUNK, kind=Kind.CACHE, group_depth=4, priority=50,
             patterns=(f'{CACHES}/*/**',), min_age_floor_days=1, **_DELETE),
        rule(id='app.logs', tier=Tier.JUNK, kind=Kind.LOGS, group_depth=4, priority=100,
             patterns=('~/Library/Logs/**',), name_include=LOG_NAMES, min_age_floor_days=1, **_DELETE),
        # A whole folder at once, because a leftover is judged by the newest file in it. Name taint protects a
        # folder that holds databases or credentials, which is most of them.
        rule(id='app.leftovers', tier=Tier.REVIEW, kind=Kind.LEFTOVER, unit=Unit.BUNDLE, priority=100,
             patterns=(f'{SUPPORT}/*',), min_age_floor_days=90, detector='orphans',
             actions=(Action.TRASH, Action.DELETE), default_action=Action.TRASH),
        rule(id='app.tmp', tier=Tier.JUNK, kind=Kind.TEMP, priority=100, patterns=('$TMPDIR/*',),
             min_age_floor_days=3, **_DELETE),
        rule(id='app.tmp-dir', tier=Tier.JUNK, kind=Kind.TEMP, unit=Unit.BUNDLE, priority=100,
             patterns=('$TMPDIR/*',), min_age_floor_days=3, **_DELETE),
        rule(id='app.user-cache-dir', tier=Tier.JUNK, kind=Kind.CACHE, unit=Unit.BUNDLE, priority=100,
             patterns=('$CACHEDIR/*',), min_age_floor_days=3, **_DELETE),
    )
