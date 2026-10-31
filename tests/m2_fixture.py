"""A small copy of the real Mac layout (Codex, Claude, Cursor, Ollama) used by the classifier and scanner tests."""
from __future__ import annotations

import json
import os
from typing import Dict, Tuple

from disk_steward.catalog import load_catalog
from disk_steward.classify import Classifier
from disk_steward.detectors import DetectorContext, compute_facts
from disk_steward.macos import FakeMacOS
from disk_steward.settings import Settings

ID_A = 'aaaaaaaa-0000-7000-8000-000000000001'   # version 7: the current id of the session
ID_B = '5c4f0b7e-0000-5000-8000-000000000001'   # version 5: its legacy, name-derived id
ID_C = 'cccccccc-0000-7000-8000-000000000003'
ID_D = 'dddddddd-0000-7000-8000-000000000004'
CODEX = 'com.openai.codex'

SESSIONS = '~/.codex/sessions/2026/10'
PAIR_A = f'{SESSIONS}/02/rollout-2026-10-02T11-04-41-{ID_A}.jsonl'
PAIR_B = f'{SESSIONS}/02/rollout-2026-10-02T11-04-41-{ID_B}.jsonl'
SOLO = f'{SESSIONS}/03/rollout-2026-10-03T09-00-00-{ID_C}.jsonl'
ARCHIVED = f'~/.codex/archived_sessions/rollout-2026-09-01T10-00-00-{ID_D}.jsonl'
VM = '~/Library/Application Support/Claude/claude-code-vm'
ENGINES = '~/Library/Application Support/Claude/claude-code'
SUPPORT = '~/Library/Application Support/Claude'
CODEX_SUPPORT = '~/Library/Application Support/Codex'
WORKSPACE = f'{SUPPORT}/local-agent-mode-sessions/org1/user1/local_abc'


def _rollout(session_id: str, ts: str, pad: int) -> str:
    meta = {'timestamp': ts, 'type': 'session_meta',
            'payload': {'id': session_id, 'timestamp': ts, 'cwd': '/secret/project/path'}}
    return json.dumps(meta) + '\n' + ('{"type":"response_item","payload":{"text":"hello"}}\n' * pad)


def build(fh) -> None:
    """Populate a FakeHome. File contents are small; sizes above 1 MiB are only used where a test asks."""
    f, d = fh.file, fh.dir
    # Codex
    f(PAIR_A, content=_rollout(ID_A, '2026-10-02T11:04:41.000Z', 20), age_days=30)
    f(PAIR_B, content=_rollout(ID_B, '2026-10-02T11:04:41.000Z', 20), age_days=30)
    f(SOLO, content=_rollout(ID_C, '2026-10-03T09:00:00.000Z', 5), age_days=29)
    f(ARCHIVED, content=_rollout(ID_D, '2026-09-01T10:00:00.000Z', 4), age_days=40)
    f('~/.codex/session_index.jsonl',
      content=''.join(json.dumps({'id': i, 'thread_name': 'x'}) + '\n' for i in (ID_A, ID_B, ID_C)))
    f('~/.codex/..codex-global-state.json.tmp-1784-aaaa', size=100, age_days=40)
    f('~/.codex/..codex-global-state.json.bak.tmp-1787-bbbb', size=100, age_days=40)
    f('~/.codex/.codex-global-state.json', size=200)
    f('~/.codex/config.toml', size=50)
    f('~/.codex/auth.json', size=30)
    f('~/.codex/logs_2.sqlite', size=4096)
    f('~/.codex/logs_2.sqlite-wal', size=4096)
    f('~/.codex/.tmp/plugins/x/README.md', size=10)
    f('~/.codex/skills/s1/SKILL.md', size=10)
    f('~/.codex/skills/s2/SKILL.md', size=10)
    f('~/.codex/log/codex-tui.log', size=100, age_days=10)
    f('~/.codex/log/other.bin', size=100, age_days=10)
    f('~/.codex/cache/models.json', size=100, age_days=10)
    f('~/.cache/codex-runtimes/codex-primary-runtime/runtime.json', size=100)
    f('~/.cache/codex-runtimes/codex-runtime-install-abc123/payload/x.bin', size=100, age_days=10)
    d('~/.cache/codex-runtimes/codex-runtime-install-abc123', age_days=10)
    # Claude Code
    f('~/.claude/projects/-Users-me-proj/a.jsonl', size=100, age_days=20)
    f('~/.claude/projects/-Users-me-proj/a/subagents/x.jsonl', size=100, age_days=20)
    f('~/.claude/projects/-Users-me-proj/a/tool-results/t.txt', size=100, age_days=20)
    f('~/.claude/projects/-Users-me-proj/memory/MEMORY.md', size=100)
    f('~/.claude/settings.json', size=20)
    f('~/.claude/debug/x.txt', size=100, age_days=5)
    f('~/.claude/file-history/s1/f@v1', size=100, age_days=5)
    for v in ('2.1.247', '2.1.289', '2.1.299'):
        f(f'~/.local/share/claude/versions/{v}', size=1000)
    os.makedirs(fh.path('~/.local/bin'), exist_ok=True)
    fh.symlink('~/.local/bin/claude', '~/.local/share/claude/versions/2.1.299')
    plugins = {'version': 2, 'plugins': {'a@mkt': [{
        'scope': 'user', 'installPath': str(fh.path('~/.claude/plugins/cache/mkt/pluginA/1.0.0'))}]}}
    f('~/.claude/plugins/installed_plugins.json', content=json.dumps(plugins))
    f('~/.claude/plugins/cache/mkt/pluginA/1.0.0/p.md', size=10)
    f('~/.claude/plugins/cache/mkt/pluginA/0.9.0/p.md', size=10)
    f('~/.claude/plugins/cache/mkt/pluginB/2.0.0/p.md', size=10)
    # Claude Desktop
    for v in ('2.1.304', '2.1.308'):
        f(f'{ENGINES}/{v}/bin/claude', size=100)
    for v in ('2.1.240', '2.1.246', '2.1.250'):
        f(f'{VM}/{v}/rootfs', size=100)
    f(f'{VM}/.sdk-version', content='2.1.246')
    f(f'{SUPPORT}/Cache/data_0', size=100)
    f(f'{SUPPORT}/Code Cache/js/abc', size=100)
    f(f'{SUPPORT}/Partitions/p1/Cache/d', size=100)
    f(f'{SUPPORT}/IndexedDB/https_claude.ai_0.indexeddb.leveldb/000003.log', size=100)
    f(f'{SUPPORT}/vm_bundles/b1/rootfs.img', size=100)
    f(f'{SUPPORT}/vm_bundles/b1/sessiondata.img', size=100)
    f('~/Library/Caches/com.anthropic.claudefordesktop/Cache.db', size=100)
    f('~/Library/Caches/com.anthropic.claudefordesktop/other', size=100)
    f('~/Library/Caches/com.anthropic.claudefordesktop.ShipIt/update.zip', size=100, age_days=3)
    # Cursor, Ollama
    f('~/.cursor/extensions/.obsolete', content=json.dumps({'pub.old-1.0.0': True}))
    f('~/.cursor/extensions/pub.old-1.0.0/package.json', size=10)
    f('~/.cursor/extensions/pub.new-2.0.0/package.json', size=10)
    f('~/Library/Application Support/Cursor/CachedData/h1/js', size=10, age_days=20)
    f('~/Library/Application Support/Cursor/CachedData/h2/js', size=10, age_days=1)
    d('~/Library/Application Support/Cursor/CachedData/h1', age_days=20)
    d('~/Library/Application Support/Cursor/CachedData/h2', age_days=1)
    f('~/.ollama/models/blobs/sha256-1', size=1000)
    f('~/.ollama/id_ed25519', size=10)
    # privacy
    f('~/Library/Messages/chat.db', size=10)
    # Claude Desktop agent workspace: a whole working folder of the user's, not cache
    f(f'{WORKSPACE}/outputs/build.py', size=20)
    f(f'{WORKSPACE}/outputs/morning-brief.html', size=20)
    f(f'{WORKSPACE}/outputs/node_modules/pkg/index.js', size=20)
    f(f'{WORKSPACE}/uploads/report.pdf', size=20)
    f(f'{WORKSPACE}/uploads-tmp/upload.part', size=20)
    f(f'{WORKSPACE}/.audit-key', size=10)
    f(f'{WORKSPACE}/.claude/.claude.json', size=10)
    f(f'{WORKSPACE}/.claude/projects/p/a.jsonl', size=10)
    f(f'{WORKSPACE}/audit.jsonl', size=10)
    f(f'{SUPPORT}/local-agent-mode-sessions/rpm/plugins/p.json', size=10)
    f(f'{SUPPORT}/local-agent-mode-sessions/cowork-skills-cache.json', size=10)
    f(f'{SUPPORT}/claude-code-sessions/s1/state.json', size=10)
    # Codex artifact runtime and plugin settings
    f(f'{CODEX_SUPPORT}/artifact-sessions/store/.owner.sock', size=0)
    f(f'{CODEX_SUPPORT}/artifact-sessions/store/state.bin', size=10)
    f(f'{CODEX_SUPPORT}/artifact-sessions/runtime-generations/g1/bin', size=10)
    f('~/.codex/cache/bundled_plugin_exclusions/disabled-bundled-plugin-ids', size=10)


# logical path -> (rule_id, tier, group_key, is_dir)
EXPECTED: Dict[str, Tuple[str, str, str, bool]] = {
    PAIR_A: ('codex.sessions', 'review', 'codex.sessions:~/.codex/sessions/2026/10', False),   # new-ID file stays
    PAIR_B: ('codex.sessions', 'review', 'codex.sessions:duplicates', False),                 # legacy-ID copy
    SOLO: ('codex.sessions', 'review', 'codex.sessions:~/.codex/sessions/2026/10', False),
    ARCHIVED: ('codex.archived-sessions', 'review', 'codex.archived-sessions', False),
    '~/.codex/session_index.jsonl': ('codex.state', 'protected', 'codex.state', False),
    '~/.codex/config.toml': ('codex.state', 'protected', 'codex.state', False),
    '~/.codex/.codex-global-state.json': ('codex.state', 'protected', 'codex.state', False),
    '~/.codex/auth.json': ('prot.credentials', 'protected', 'prot.credentials:agent', False),
    '~/.codex/logs_2.sqlite': ('prot.databases', 'protected', 'prot.databases:agent', False),
    '~/.codex/logs_2.sqlite-wal': ('prot.databases', 'protected', 'prot.databases:agent', False),
    '~/.codex/..codex-global-state.json.tmp-1784-aaaa': ('codex.state-tmp', 'junk', 'codex.state-tmp', False),
    '~/.codex/..codex-global-state.json.bak.tmp-1787-bbbb': ('codex.state-tmp', 'junk', 'codex.state-tmp', False),
    '~/.codex/.tmp/plugins/x/README.md': ('codex.plugin-staging', 'protected', 'codex.plugin-staging', False),
    '~/.codex/skills/s1/SKILL.md': ('codex.user-content', 'protected', 'codex.user-content:~/.codex/skills', False),
    '~/.codex/log/codex-tui.log': ('codex.logs', 'junk', 'codex.logs', False),
    '~/.codex/log/other.bin': ('unknown', 'protected', 'unknown:~/.codex/log', False),
    '~/.codex/cache/models.json': ('codex.cache', 'junk', 'codex.cache', False),
    '~/.cache/codex-runtimes/codex-primary-runtime': ('codex.runtimes', 'review', 'codex.runtimes', True),
    '~/.cache/codex-runtimes/codex-runtime-install-abc123': ('codex.runtime-install', 'junk',
                                                             'codex.runtime-install', True),
    '~/.claude/projects/-Users-me-proj/a.jsonl': ('claude.projects', 'review',
                                                  'claude.projects:~/.claude/projects/-Users-me-proj', False),
    '~/.claude/projects/-Users-me-proj/a/subagents/x.jsonl': (
        'claude.projects', 'review', 'claude.projects:~/.claude/projects/-Users-me-proj', False),
    '~/.claude/projects/-Users-me-proj/a/tool-results/t.txt': (
        'claude.projects', 'review', 'claude.projects:~/.claude/projects/-Users-me-proj', False),
    '~/.claude/projects/-Users-me-proj/memory/MEMORY.md': ('claude.memory', 'protected', 'claude.memory', False),
    '~/.claude/settings.json': ('claude.config', 'protected', 'claude.config', False),
    '~/.claude/plugins/installed_plugins.json': ('claude.config', 'protected', 'claude.config', False),
    '~/.claude/debug/x.txt': ('claude.logs', 'junk', 'claude.logs', False),
    '~/.claude/file-history/s1/f@v1': ('claude.file-history', 'review', 'claude.file-history', False),
    '~/.local/share/claude/versions/2.1.247': ('claude.cli-versions', 'junk', 'claude.cli-versions', False),
    '~/.local/share/claude/versions/2.1.289': ('claude.cli-versions', 'junk', 'claude.cli-versions', False),
    '~/.local/share/claude/versions/2.1.299': ('claude.cli-versions', 'protected',
                                               'claude.cli-versions#protected', False),
    '~/.claude/plugins/cache/mkt/pluginA/1.0.0': ('claude.plugin-cache', 'protected',
                                                  'claude.plugin-cache#protected', True),
    '~/.claude/plugins/cache/mkt/pluginA/0.9.0': ('claude.plugin-cache', 'review', 'claude.plugin-cache', True),
    '~/.claude/plugins/cache/mkt/pluginB/2.0.0': ('claude.plugin-cache', 'review', 'claude.plugin-cache', True),
    f'{ENGINES}/2.1.304': ('claude.desktop-engines', 'junk', 'claude.desktop-engines', True),
    f'{ENGINES}/2.1.308': ('claude.desktop-engines', 'protected', 'claude.desktop-engines#protected', True),
    f'{VM}/2.1.240': ('claude.desktop-engines', 'junk', 'claude.desktop-engines', True),
    f'{VM}/2.1.246': ('claude.desktop-engines', 'protected', 'claude.desktop-engines#protected', True),
    f'{VM}/2.1.250': ('claude.desktop-engines', 'protected', 'claude.desktop-engines#protected', True),
    f'{VM}/.sdk-version': ('unknown', 'protected', f'unknown:{SUPPORT}', False),
    f'{SUPPORT}/Cache/data_0': ('claude.electron.cache', 'junk', 'claude.electron.cache', False),
    f'{SUPPORT}/Code Cache/js/abc': ('claude.electron.cache', 'junk', 'claude.electron.cache', False),
    f'{SUPPORT}/Partitions/p1/Cache/d': ('claude.electron.cache', 'junk', 'claude.electron.cache', False),
    f'{SUPPORT}/IndexedDB/https_claude.ai_0.indexeddb.leveldb/000003.log': (
        'claude.desktop-state', 'protected', 'claude.desktop-state', False),
    f'{SUPPORT}/vm_bundles/b1/rootfs.img': ('claude.vm-images', 'review', 'claude.vm-images', False),
    f'{SUPPORT}/vm_bundles/b1/sessiondata.img': ('unknown', 'protected', f'unknown:{SUPPORT}', False),
    '~/Library/Caches/com.anthropic.claudefordesktop/Cache.db': ('prot.databases', 'protected', 'prot.databases:agent', False),
    '~/Library/Caches/com.anthropic.claudefordesktop/other': ('claude.net-cache', 'junk',
                                                              'claude.net-cache', False),
    '~/Library/Caches/com.anthropic.claudefordesktop.ShipIt/update.zip': ('claude.shipit', 'junk',
                                                                           'claude.shipit', False),
    '~/.cursor/extensions/pub.old-1.0.0': ('cursor.extensions', 'junk', 'cursor.extensions', True),
    '~/.cursor/extensions/pub.new-2.0.0/package.json': ('unknown', 'protected',
                                                        'unknown:~/.cursor/extensions', False),
    '~/Library/Application Support/Cursor/CachedData/h1': ('cursor.cached-data', 'junk',
                                                           'cursor.cached-data', True),
    '~/Library/Application Support/Cursor/CachedData/h2': ('cursor.cached-data', 'protected',
                                                           'cursor.cached-data#protected', True),
    '~/.ollama/models': ('ollama.models', 'review', 'ollama.models', True),
    '~/.ollama/id_ed25519': ('prot.credentials', 'protected', 'prot.credentials:agent', False),
    f'{WORKSPACE}/outputs/build.py': ('claude.desktop-workspaces', 'protected', 'claude.desktop-workspaces', False),
    f'{WORKSPACE}/outputs/morning-brief.html': ('claude.desktop-workspaces', 'protected',
                                                'claude.desktop-workspaces', False),
    f'{WORKSPACE}/outputs/node_modules/pkg/index.js': ('claude.desktop-workspaces', 'protected',
                                                       'claude.desktop-workspaces', False),
    f'{WORKSPACE}/uploads/report.pdf': ('claude.desktop-workspaces', 'protected', 'claude.desktop-workspaces', False),
    f'{WORKSPACE}/uploads-tmp/upload.part': ('claude.desktop-workspaces', 'protected',
                                             'claude.desktop-workspaces', False),
    f'{WORKSPACE}/.audit-key': ('prot.credentials', 'protected', 'prot.credentials:agent', False),
    f'{WORKSPACE}/.claude/.claude.json': ('claude.desktop-workspaces', 'protected', 'claude.desktop-workspaces', False),
    f'{WORKSPACE}/.claude/projects/p/a.jsonl': ('claude.desktop-workspaces', 'protected',
                                                'claude.desktop-workspaces', False),
    f'{WORKSPACE}/audit.jsonl': ('claude.desktop-workspaces', 'protected', 'claude.desktop-workspaces', False),
    f'{SUPPORT}/local-agent-mode-sessions/rpm/plugins/p.json': ('claude.desktop-workspaces', 'protected',
                                                                'claude.desktop-workspaces', False),
    f'{SUPPORT}/local-agent-mode-sessions/cowork-skills-cache.json': ('claude.desktop-workspaces', 'protected',
                                                                      'claude.desktop-workspaces', False),
    f'{SUPPORT}/claude-code-sessions/s1/state.json': ('claude.desktop-workspaces', 'protected',
                                                      'claude.desktop-workspaces', False),
    f'{CODEX_SUPPORT}/artifact-sessions/store/.owner.sock': ('codex.artifact-runtime', 'protected',
                                                             'codex.artifact-runtime', False),
    f'{CODEX_SUPPORT}/artifact-sessions/store/state.bin': ('codex.artifact-runtime', 'protected',
                                                           'codex.artifact-runtime', False),
    f'{CODEX_SUPPORT}/artifact-sessions/runtime-generations/g1/bin': ('codex.artifact-runtime', 'protected',
                                                                      'codex.artifact-runtime', False),
    '~/.codex/cache/bundled_plugin_exclusions/disabled-bundled-plugin-ids': ('codex.plugin-exclusions', 'protected',
                                                                             'codex.plugin-exclusions', False),
}


def make_classifier(fh, mac=None, settings=None):
    settings = settings or Settings(domains=('agent',))
    catalog = load_catalog(settings)
    mac = mac or FakeMacOS()
    facts, _ = compute_facts(DetectorContext(fh.bases, mac, settings), catalog)
    return Classifier(catalog, facts, fh.bases, settings), catalog, facts
