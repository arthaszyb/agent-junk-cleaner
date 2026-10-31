"""Claude: ~/.claude (Claude Code), the claude CLI installs and Claude Desktop (com.anthropic.claudefordesktop)."""
from __future__ import annotations

from typing import Tuple

from . import ScanRoot, electron_cache_rules
from ..model import Action, Domain, Kind, Rule, Tier, Unit

APP = 'com.anthropic.claudefordesktop'
SUPPORT = '~/Library/Application Support/Claude'
VERSIONS = '~/.local/share/claude/versions'
D = Domain.AGENT

LOG_NAMES = ('*.log', '*.log.*', '*.txt', '*.json', '*.jsonl', '*.gz')
_DELETE = dict(actions=(Action.DELETE,), default_action=Action.DELETE)
_ARCHIVE_OR_DELETE = dict(actions=(Action.ARCHIVE, Action.DELETE), default_action=Action.ARCHIVE)

ROOTS = tuple(ScanRoot(p, D) for p in (
    '~/.claude', '~/.local/share/claude', '~/.cache/claude', SUPPORT, '~/Library/Application Support/Claude-3p',
    '~/Library/Caches/claude-cli-nodejs', '~/Library/Caches/com.anthropic.claudefordesktop',
    '~/Library/Caches/com.anthropic.claudefordesktop.ShipIt', '~/Library/Logs/Claude'))


def rules(settings: object) -> Tuple[Rule, ...]:
    def rule(**kw: object) -> Rule:
        return Rule(provider='claude', domain=D, **kw)  # type: ignore[arg-type]

    return electron_cache_rules('claude.electron', 'claude', D, SUPPORT, APP) + (
        rule(id='claude.logs', tier=Tier.JUNK, kind=Kind.LOGS, name_include=LOG_NAMES, min_age_floor_days=1,
             patterns=('~/.claude/debug/**', '~/.claude/telemetry/**', '~/Library/Logs/Claude/**'),
             priority=300, **_DELETE),
        rule(id='claude.cli-cache', tier=Tier.JUNK, kind=Kind.CACHE,
             patterns=('~/Library/Caches/claude-cli-nodejs/**',), priority=300, **_DELETE),
        # Cache.db* in here stays protected by prot.databases.
        rule(id='claude.net-cache', tier=Tier.JUNK, kind=Kind.CACHE, quit_apps=(APP,), priority=300,
             patterns=(f'~/Library/Caches/{APP}/**',), **_DELETE),
        rule(id='claude.shipit', tier=Tier.JUNK, kind=Kind.UPDATE_LEFTOVER, min_age_floor_days=1,
             quit_apps=(APP,), patterns=(f'~/Library/Caches/{APP}.ShipIt/**',), priority=300, **_DELETE),
        # Versions are single binaries on current installs and directories on older ones.
        rule(id='claude.cli-versions', tier=Tier.JUNK, kind=Kind.OLD_VERSION, patterns=(f'{VERSIONS}/*',),
             detector='claude_versions', priority=300, **_DELETE),
        rule(id='claude.cli-versions-dir', tier=Tier.JUNK, kind=Kind.OLD_VERSION, unit=Unit.BUNDLE,
             patterns=(f'{VERSIONS}/*',), detector='claude_versions', priority=300, **_DELETE),
        rule(id='claude.desktop-engines', tier=Tier.JUNK, kind=Kind.OLD_VERSION, unit=Unit.BUNDLE,
             quit_apps=(APP,), detector='desktop_engines', priority=300,
             patterns=(f'{SUPPORT}/claude-code/*', f'{SUPPORT}/claude-code-vm/*'), **_DELETE),
        # Transcripts are appended to by open-and-close writes that lsof never sees, hence the age floor.
        rule(id='claude.projects', tier=Tier.REVIEW, kind=Kind.SESSION_HISTORY, group_depth=4, priority=300,
             min_age_floor_days=1.0,
             patterns=('~/.claude/projects/*/*.jsonl', '~/.claude/projects/*/*/subagents/**',
                       '~/.claude/projects/*/*/tool-results/**'), **_ARCHIVE_OR_DELETE),
        rule(id='claude.file-history', tier=Tier.REVIEW, kind=Kind.SESSION_HISTORY, min_age_floor_days=1.0,
             patterns=('~/.claude/file-history/**',), priority=300, **_ARCHIVE_OR_DELETE),
        # sessiondata.img is not listed on purpose: it holds the VM's user data and stays unclassified.
        rule(id='claude.vm-images', tier=Tier.REVIEW, kind=Kind.VM_IMAGE, quit_apps=(APP,),
             patterns=(f'{SUPPORT}/vm_bundles/**',), name_include=('rootfs.img', 'vmlinuz', 'initrd*'),
             priority=300, **_ARCHIVE_OR_DELETE),
        rule(id='claude.plugin-cache', tier=Tier.REVIEW, kind=Kind.CACHE, unit=Unit.BUNDLE,
             patterns=('~/.claude/plugins/cache/*/*/*',), detector='claude_plugins', priority=300, **_DELETE),
        # Each session folder is a whole agent workspace: the user's outputs and uploads, node_modules, audit
        # keys and the account state of the nested .claude. Nothing in it is ours to sort.
        rule(id='claude.desktop-workspaces', tier=Tier.PROTECTED, kind=Kind.WORK_PRODUCT, priority=900,
             patterns=(f'{SUPPORT}/local-agent-mode-sessions/**', f'{SUPPORT}/claude-code-sessions/**')),
        rule(id='claude.memory', tier=Tier.PROTECTED, kind=Kind.WORK_PRODUCT,
             patterns=('~/.claude/projects/*/memory/**',), priority=900),
        rule(id='claude.config', tier=Tier.PROTECTED, kind=Kind.CONFIG, priority=900,
             patterns=('~/.claude/settings.json', '~/.claude/settings.local.json', '~/.claude/CLAUDE.md',
                       '~/.claude/history.jsonl', '~/.claude/agents/**', '~/.claude/commands/**',
                       '~/.claude/skills/**', '~/.claude/hooks/**', '~/.claude/plans/**',
                       '~/.claude/plugins/marketplaces/**', '~/.claude/plugins/*.json')),
        rule(id='claude.desktop-state', tier=Tier.PROTECTED, kind=Kind.DATABASE, priority=900,
             patterns=tuple(f'{SUPPORT}/{d}/**' for d in
                            ('IndexedDB', 'Local Storage', 'Session Storage', 'Service Worker', 'WebStorage'))),
    )
