"""Codex: ~/.codex, the Codex desktop app (ChatGPT.app, bundle id com.openai.codex) and its runtimes."""
from __future__ import annotations

from typing import Tuple

from . import ScanRoot, electron_cache_rules
from ..model import Action, Domain, Kind, Rule, Tier, Unit

APP = 'com.openai.codex'
SUPPORT = '~/Library/Application Support/Codex'
CACHES = '~/Library/Caches/com.openai.codex'
D = Domain.AGENT

LOG_NAMES = ('*.log', '*.log.*', '*.txt', '*.json', '*.jsonl', '*.gz')
_DELETE = dict(actions=(Action.DELETE,), default_action=Action.DELETE)
_ARCHIVE_OR_DELETE = dict(actions=(Action.ARCHIVE, Action.DELETE), default_action=Action.ARCHIVE)
_USER_DIRS = ('skills', 'rules', 'memories', 'automations', 'attachments', 'generated_images', 'visualizations',
              'worktrees', 'plugins', 'dictation-history', 'vendor_imports', 'ambient-suggestions')

ROOTS = tuple(ScanRoot(p, D) for p in (
    '~/.codex', '~/.cache/codex-runtimes', SUPPORT, '~/Library/Application Support/com.openai.codex',
    '~/Library/Caches/Codex', CACHES, '~/Library/Logs/com.openai.codex'))


def rules(settings: object) -> Tuple[Rule, ...]:
    def rule(**kw: object) -> Rule:
        return Rule(provider='codex', domain=D, **kw)  # type: ignore[arg-type]

    return electron_cache_rules('codex.electron', 'codex', D, SUPPORT, APP) + (
        rule(id='codex.logs', tier=Tier.JUNK, kind=Kind.LOGS, name_include=LOG_NAMES, min_age_floor_days=1,
             patterns=('~/.codex/log/**', '~/Library/Logs/com.openai.codex/**'), priority=300, **_DELETE),
        rule(id='codex.cache', tier=Tier.JUNK, kind=Kind.CACHE, patterns=('~/.codex/cache/**',),
             min_age_floor_days=1, priority=300, **_DELETE),
        rule(id='codex.browser-cache', tier=Tier.JUNK, kind=Kind.CACHE, quit_apps=(APP,), priority=300,
             patterns=('~/Library/Caches/Codex/**', f'{CACHES}/fsCachedData/**'), **_DELETE),
        rule(id='codex.sparkle', tier=Tier.JUNK, kind=Kind.UPDATE_LEFTOVER, unit=Unit.BUNDLE, quit_apps=(APP,),
             min_age_floor_days=1, priority=300,
             patterns=(f'{CACHES}/org.sparkle-project.Sparkle/Installation/*',
                       f'{CACHES}/org.sparkle-project.Sparkle/PersistentDownloads/*'), **_DELETE),
        rule(id='codex.component-cache', tier=Tier.JUNK, kind=Kind.CACHE, quit_apps=(APP,), priority=300,
             patterns=(f'{SUPPORT}/component_crx_cache/**',), **_DELETE),
        # Above codex.state (900): the atomic-write leftovers sit next to the live state file.
        rule(id='codex.state-tmp', tier=Tier.JUNK, kind=Kind.TEMP, min_age_floor_days=7, priority=950,
             patterns=('~/.codex/..codex-global-state.json*.tmp-*',), detector='codex_state_tmp', **_DELETE),
        rule(id='codex.runtime-install', tier=Tier.JUNK, kind=Kind.TEMP, unit=Unit.BUNDLE, min_age_floor_days=3,
             patterns=('~/.cache/codex-runtimes/codex-runtime-install-*',), priority=320, **_DELETE),
        rule(id='codex.runtimes', tier=Tier.REVIEW, kind=Kind.RUNTIME, unit=Unit.BUNDLE,
             patterns=('~/.cache/codex-runtimes/*',), priority=300, **_DELETE),
        # A live session file is appended to by open-and-close writes that lsof never sees: hence the age floor.
        rule(id='codex.sessions', tier=Tier.REVIEW, kind=Kind.SESSION_HISTORY, patterns=('~/.codex/sessions/**',),
             name_include=('*.jsonl',), group_depth=5, detector='codex_dupes', priority=300,
             min_age_floor_days=1.0, **_ARCHIVE_OR_DELETE),
        rule(id='codex.archived-sessions', tier=Tier.REVIEW, kind=Kind.SESSION_HISTORY,
             patterns=('~/.codex/archived_sessions/*.jsonl',), detector='codex_dupes', priority=300,
             min_age_floor_days=1.0, **_ARCHIVE_OR_DELETE),
        # store/ holds a live .owner.sock and runtime-generations/ the current generation.
        rule(id='codex.artifact-runtime', tier=Tier.PROTECTED, kind=Kind.RUNTIME,
             patterns=(f'{SUPPORT}/artifact-sessions/**',), priority=900),
        rule(id='codex.plugin-exclusions', tier=Tier.PROTECTED, kind=Kind.CONFIG,
             patterns=('~/.codex/cache/bundled_plugin_exclusions/**',), priority=900),
        rule(id='codex.state', tier=Tier.PROTECTED, kind=Kind.CONFIG, patterns=('~/.codex/*',), priority=900),
        rule(id='codex.plugin-staging', tier=Tier.PROTECTED, kind=Kind.TEMP, patterns=('~/.codex/.tmp/**',),
             priority=900),
        rule(id='codex.user-content', tier=Tier.PROTECTED, kind=Kind.WORK_PRODUCT, group_depth=3, priority=900,
             patterns=tuple(f'~/.codex/{d}/**' for d in _USER_DIRS)),
    )
