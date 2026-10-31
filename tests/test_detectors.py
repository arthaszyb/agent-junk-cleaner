from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from disk_steward.catalog import build_catalog, load_catalog
from disk_steward.detectors import REGISTRY, DetectorContext, codex_bloat, codex_dupes, compute_facts, facts_digest
from disk_steward.macos import FakeMacOS
from disk_steward.model import Domain, Evidence, Kind, Rule, StatInfo, Tier
from disk_steward.settings import Settings

from support import FakeHome

CAT = load_catalog(Settings())
STAT = StatInfo(dev=1, ino=1, mode=0o100644, nlink=1, uid=1, size=0, blocks=0, mtime_ns=0, birth_ns=0, flags=0)
VERSIONS = '~/.local/share/claude/versions'
ENGINES = '~/Library/Application Support/Claude/claude-code'
VM = '~/Library/Application Support/Claude/claude-code-vm'
ID1 = '11111111-aaaa-7000-8000-000000000001'
ID2 = '22222222-aaaa-5000-8000-000000000002'
ID3 = '33333333-aaaa-5000-8000-000000000003'


def prepare(fh, name):
    det = REGISTRY[name]
    rules = tuple(r for r in CAT.rules if r.detector == name)
    return det.prepare(DetectorContext(fh.bases, FakeMacOS(), Settings(), rules))


def refine(name, rule_id, logical, facts, is_dir=False):
    return REGISTRY[name].refine(CAT.rule(rule_id), logical, STAT, is_dir, facts)


def keys(ref):
    return [e.key for e in ref.evidence]


class Base(unittest.TestCase):
    def setUp(self):
        self.fh = FakeHome().__enter__()
        self.addCleanup(self.fh.cleanup)


class ClaudeVersionsTests(Base):
    def make(self, names=('2.1.247', '2.1.289', '2.1.299'), link='2.1.299'):
        for n in names:
            self.fh.file(f'{VERSIONS}/{n}', size=10)
        if link is not None:
            os.makedirs(self.fh.path('~/.local/bin'), exist_ok=True)
            self.fh.symlink('~/.local/bin/claude', f'{VERSIONS}/{link}')

    def ref(self, facts, v):
        return refine('claude_versions', 'claude.cli-versions', f'{VERSIONS}/{v}', facts)

    def test_current_and_newest_are_kept_the_rest_is_junk(self):
        self.make(link='2.1.289')   # the user runs an older build than the newest installed
        facts = prepare(self.fh, 'claude_versions')
        self.assertEqual((facts['status'], facts['current'], facts['newest']), ('ok', '2.1.289', '2.1.299'))
        for v in ('2.1.289', '2.1.299'):
            r = self.ref(facts, v)
            self.assertEqual(r.tier, Tier.PROTECTED)
            self.assertEqual(keys(r), ['evidence.current_version'])
        r = self.ref(facts, '2.1.247')
        self.assertIsNone(r.tier)
        self.assertEqual(r.evidence, (Evidence('evidence.not_current_version',
                                               {'current': '2.1.289', 'newest': '2.1.299'}),))

    def test_versions_sort_numerically(self):
        self.make(names=('2.1.9', '2.1.100', '2.1.20'), link='2.1.9')
        facts = prepare(self.fh, 'claude_versions')
        self.assertEqual(facts['versions'], ['2.1.9', '2.1.20', '2.1.100'])
        self.assertEqual(facts['newest'], '2.1.100')

    def test_symlink_chain_and_relative_targets(self):
        for n in ('2.1.1', '2.1.2'):
            self.fh.file(f'{VERSIONS}/{n}', size=1)
        os.makedirs(self.fh.path('~/.local/bin'))
        os.symlink('../share/claude/versions/2.1.1', self.fh.path('~/.local/bin/hop'))   # relative
        os.symlink('hop', self.fh.path('~/.local/bin/claude'))                           # link to a link
        facts = prepare(self.fh, 'claude_versions')
        self.assertEqual((facts['status'], facts['current']), ('ok', '2.1.1'))

    def test_link_into_a_version_folder(self):
        self.fh.file(f'{VERSIONS}/2.1.5/bin/claude', size=1)
        os.makedirs(self.fh.path('~/.local/bin'))
        os.symlink(str(self.fh.path(f'{VERSIONS}/2.1.5/bin/claude')), self.fh.path('~/.local/bin/claude'))
        facts = prepare(self.fh, 'claude_versions')
        self.assertEqual((facts['status'], facts['current']), ('ok', '2.1.5'))

    def check_unverifiable(self, status):
        facts = prepare(self.fh, 'claude_versions')
        self.assertEqual((facts['status'], facts['current']), (status, None))
        for v in ('2.1.247', '2.1.289', '2.1.299'):
            r = self.ref(facts, v)
            self.assertEqual(r.tier, Tier.PROTECTED)
            self.assertEqual(r.evidence, (Evidence('evidence.version_unverifiable', {'reason': status}),))

    def test_missing_symlink_protects_everything(self):
        self.make(link=None)
        self.check_unverifiable('missing')

    def test_symlink_outside_the_versions_dir_protects_everything(self):
        self.make(link=None)
        self.fh.file('~/elsewhere/claude', size=1)
        os.makedirs(self.fh.path('~/.local/bin'))
        os.symlink(str(self.fh.path('~/elsewhere/claude')), self.fh.path('~/.local/bin/claude'))
        self.check_unverifiable('outside')

    def test_symlink_outside_every_base_protects_everything(self):
        self.make(link=None)
        os.makedirs(self.fh.path('~/.local/bin'))
        os.symlink('/usr/bin/true', self.fh.path('~/.local/bin/claude'))
        self.check_unverifiable('outside')

    def test_dangling_symlink_protects_everything(self):
        self.make(link=None)
        os.makedirs(self.fh.path('~/.local/bin'))
        os.symlink(str(self.fh.path(f'{VERSIONS}/9.9.9')), self.fh.path('~/.local/bin/claude'))
        self.check_unverifiable('unresolvable')

    def test_plain_file_instead_of_a_symlink_protects_everything(self):
        self.make(link=None)
        self.fh.file('~/.local/bin/claude', size=1)
        self.check_unverifiable('unresolvable')

    def test_symlink_loop_protects_everything(self):
        self.make(link=None)
        os.makedirs(self.fh.path('~/.local/bin'))
        os.symlink('b', self.fh.path('~/.local/bin/claude'))
        os.symlink('claude', self.fh.path('~/.local/bin/b'))
        self.check_unverifiable('unresolvable')

    def test_link_to_a_non_version_name_is_unresolvable(self):
        self.make(names=('2.1.1', 'latest'), link='latest')
        facts = prepare(self.fh, 'claude_versions')
        self.assertEqual(facts['status'], 'unresolvable')

    def test_non_version_names_are_skipped(self):
        self.make()
        facts = prepare(self.fh, 'claude_versions')
        for name in ('latest', '2.1.299.partial', '.DS_Store', '2.1'.replace('2.1', 'x')):
            self.assertTrue(self.ref(facts, name).skip, name)

    def test_no_versions_dir(self):
        facts = prepare(self.fh, 'claude_versions')
        self.assertEqual((facts['versions'], facts['newest'], facts['status']), ([], None, 'missing'))

    def test_a_version_that_appeared_after_prepare_is_protected(self):
        self.make()
        facts = prepare(self.fh, 'claude_versions')
        self.fh.file(f'{VERSIONS}/2.1.400', size=1)   # an update landed while the scan was running
        r = self.ref(facts, '2.1.400')
        self.assertEqual((r.tier, r.evidence), (Tier.PROTECTED, (Evidence('evidence.appeared_during_scan', {}),)))
        self.assertIsNone(self.ref(facts, '2.1.247').tier)   # known versions are judged as before


class DesktopEnginesTests(Base):
    def make(self, claude_code=('2.1.304', '2.1.308'), vm=('2.1.246', '2.1.250'), pin='2.1.246'):
        for v in claude_code:
            self.fh.file(f'{ENGINES}/{v}/bin', size=1)
        for v in vm:
            self.fh.file(f'{VM}/{v}/bin', size=1)
        if pin is not None:
            self.fh.file(f'{VM}/.sdk-version', content=pin)

    def ref(self, facts, root, v):
        return refine('desktop_engines', 'claude.desktop-engines', f'{root}/{v}', facts, True)

    def test_newest_and_pinned_are_kept(self):
        self.make()
        facts = prepare(self.fh, 'desktop_engines')
        self.assertEqual(keys(self.ref(facts, ENGINES, '2.1.308')), ['evidence.engine_newest'])
        r = self.ref(facts, ENGINES, '2.1.304')
        self.assertIsNone(r.tier)
        self.assertEqual(keys(r), ['evidence.engine_superseded'])
        self.assertEqual(keys(self.ref(facts, VM, '2.1.250')), ['evidence.engine_newest'])
        self.assertEqual(keys(self.ref(facts, VM, '2.1.246')), ['evidence.engine_pinned'])
        self.assertEqual(self.ref(facts, VM, '2.1.246').tier, Tier.PROTECTED)

    def test_roots_are_independent(self):
        self.make(vm=('2.1.100', '2.1.200'), pin='2.1.100')
        facts = prepare(self.fh, 'desktop_engines')
        self.assertIsNone(self.ref(facts, ENGINES, '2.1.304').tier)
        self.assertEqual(self.ref(facts, VM, '2.1.100').tier, Tier.PROTECTED)

    def test_version_that_is_both_newest_and_pinned_carries_both_reasons(self):
        self.make(vm=('2.1.246',), pin='2.1.246')
        facts = prepare(self.fh, 'desktop_engines')
        self.assertEqual(keys(self.ref(facts, VM, '2.1.246')), ['evidence.engine_newest', 'evidence.engine_pinned'])

    def test_no_pin_file_means_only_the_newest_is_kept(self):
        self.make(vm=('2.1.1', '2.1.2'), pin=None)
        facts = prepare(self.fh, 'desktop_engines')
        self.assertIsNone(self.ref(facts, VM, '2.1.1').tier)
        self.assertEqual(self.ref(facts, VM, '2.1.2').tier, Tier.PROTECTED)

    def check_all_protected(self, root_versions):
        facts = prepare(self.fh, 'desktop_engines')
        self.assertTrue(facts['roots'][VM]['unverifiable'])
        for v in root_versions:
            r = self.ref(facts, VM, v)
            self.assertEqual(r.tier, Tier.PROTECTED, v)
            self.assertEqual(keys(r), ['evidence.version_unverifiable'])
        # the other root is not affected
        self.assertFalse(facts['roots'][ENGINES]['unverifiable'])

    def test_pin_naming_a_missing_version_protects_the_whole_root(self):
        self.make(vm=('2.1.1', '2.1.2'), pin='2.1.99')
        self.check_all_protected(('2.1.1', '2.1.2'))

    def test_unreadable_pin_protects_the_whole_root(self):
        self.make(vm=('2.1.1', '2.1.2'))
        pin = self.fh.path(f'{VM}/.sdk-version')
        os.chmod(pin, 0)
        self.addCleanup(os.chmod, pin, 0o644)
        self.check_all_protected(('2.1.1', '2.1.2'))

    def test_empty_or_garbage_pin_protects_the_whole_root(self):
        for content in ('', 'not-a-version\n', '\xff\xfe'):
            with self.subTest(content=content):
                fh = FakeHome().__enter__()
                try:
                    for v in ('2.1.1', '2.1.2'):
                        fh.file(f'{VM}/{v}/bin', size=1)
                    fh.file(f'{VM}/.sdk-version', content=content.encode('latin-1'))
                    facts = prepare(fh, 'desktop_engines')
                    self.assertTrue(facts['roots'][VM]['unverifiable'])
                finally:
                    fh.cleanup()

    def test_pin_with_trailing_newline_is_accepted(self):
        self.make(vm=('2.1.1', '2.1.2'), pin='2.1.1\n')
        facts = prepare(self.fh, 'desktop_engines')
        self.assertEqual(facts['roots'][VM]['pinned'], '2.1.1')

    def test_non_numeric_names_are_skipped(self):
        self.make()
        facts = prepare(self.fh, 'desktop_engines')
        for name in ('latest', 'tmp-download', '.sdk-version'):
            self.assertTrue(self.ref(facts, VM, name).skip, name)

    def test_an_engine_that_appeared_after_prepare_is_protected(self):
        self.make()
        facts = prepare(self.fh, 'desktop_engines')
        for root in (ENGINES, VM):
            r = self.ref(facts, root, '2.1.999')
            self.assertEqual((r.tier, keys(r)), (Tier.PROTECTED, ['evidence.appeared_during_scan']), root)
        self.assertIsNone(self.ref(facts, ENGINES, '2.1.304').tier)


class ClaudePluginsTests(Base):
    CACHE = '~/.claude/plugins/cache/mkt'

    def manifest(self, data):
        self.fh.file('~/.claude/plugins/installed_plugins.json', content=json.dumps(data))

    def abs(self, logical):
        return str(self.fh.path(logical))

    def versions(self, *names, plugin='p'):
        for n in names:
            self.fh.dir(f'{self.CACHE}/{plugin}/{n}')

    def ref(self, facts, version, plugin='p'):
        return refine('claude_plugins', 'claude.plugin-cache', f'{self.CACHE}/{plugin}/{version}', facts, True)

    def test_installed_and_stale(self):
        self.versions('1.0', '0.9')
        self.manifest({'version': 2, 'plugins': {'p@mkt': [{'installPath': self.abs(f'{self.CACHE}/p/1.0')}]}})
        facts = prepare(self.fh, 'claude_plugins')
        self.assertEqual(facts, {'status': 'ok', 'installed': [f'{self.CACHE}/p/1.0'],
                                 'cache': [f'{self.CACHE}/p/0.9', f'{self.CACHE}/p/1.0']})
        r = self.ref(facts, '1.0')
        self.assertEqual((r.tier, keys(r)), (Tier.PROTECTED, ['evidence.plugin_installed']))
        r = self.ref(facts, '0.9')
        self.assertEqual((r.tier, keys(r)), (None, ['evidence.plugin_not_in_manifest']))

    def test_a_cache_entry_that_appeared_after_prepare_is_protected(self):
        self.versions('1.0')
        self.manifest({'version': 2, 'plugins': {}})
        facts = prepare(self.fh, 'claude_plugins')
        self.versions('2.0')   # installed while the scan was running; the manifest we read is older
        r = self.ref(facts, '2.0')
        self.assertEqual((r.tier, keys(r)), (Tier.PROTECTED, ['evidence.appeared_during_scan']))
        self.assertIsNone(self.ref(facts, '1.0').tier)

    def test_install_path_below_or_above_the_version_folder_keeps_it(self):
        self.versions('1.0')
        self.versions('9', plugin='q')
        self.manifest({'version': 2, 'plugins': {
            'a': [{'installPath': self.abs(f'{self.CACHE}/p/1.0/sub')}],
            'b': [{'installPath': self.abs(f'{self.CACHE}/q')}]}})
        facts = prepare(self.fh, 'claude_plugins')
        self.assertEqual(self.ref(facts, '1.0').tier, Tier.PROTECTED)
        self.assertEqual(self.ref(facts, '9', plugin='q').tier, Tier.PROTECTED)

    def test_install_path_spelled_through_a_symlink_is_resolved(self):
        self.versions('1.0', '0.9')
        os.symlink(self.abs('~/.claude/plugins'), self.abs('~/plugins-link'))
        self.manifest({'version': 2, 'plugins': {'p': [{'installPath': self.abs('~/plugins-link/cache/mkt/p/1.0')}]}})
        facts = prepare(self.fh, 'claude_plugins')
        self.assertEqual(facts['status'], 'ok')
        self.assertEqual(facts['installed'], [f'{self.CACHE}/p/1.0'])
        self.assertEqual(self.ref(facts, '1.0').tier, Tier.PROTECTED)
        self.assertIsNone(self.ref(facts, '0.9').tier)

    def test_install_path_outside_the_home_cannot_be_a_cache_entry(self):
        self.versions('1.0')
        self.manifest({'version': 2, 'plugins': {'p': [{'installPath': '/opt/elsewhere/p'}]}})
        facts = prepare(self.fh, 'claude_plugins')
        self.assertEqual((facts['status'], facts['installed']), ('ok', []))
        self.assertIsNone(self.ref(facts, '1.0').tier)

    def test_install_path_inside_the_home_that_cannot_be_mapped_protects_everything(self):
        from disk_steward.model import ErrorCode, StewardError
        from disk_steward.paths import Bases
        self.versions('1.0', '0.9')
        self.manifest({'version': 2, 'plugins': {'p': [{'installPath': self.abs(f'{self.CACHE}/p/1.0')}]}})
        failing = mock.patch.object(Bases, 'to_logical', side_effect=StewardError(ErrorCode.INVALID, 'error.invalid_path',
                                                                                  path='x'))
        with failing:
            facts = prepare(self.fh, 'claude_plugins')
        self.assertEqual(facts['status'], 'unverifiable')
        for v in ('1.0', '0.9'):
            r = self.ref(facts, v)
            self.assertEqual((r.tier, keys(r)), (Tier.PROTECTED, ['evidence.plugin_manifest_unreadable']), v)

    def check_unreadable(self, status):
        self.versions('1.0')
        facts = prepare(self.fh, 'claude_plugins')
        self.assertEqual(facts['status'], status)
        r = self.ref(facts, '1.0')
        self.assertEqual((r.tier, keys(r)), (Tier.PROTECTED, ['evidence.plugin_manifest_unreadable']))

    def test_missing_manifest_protects_everything(self):
        self.check_unreadable('missing')

    def test_invalid_json_protects_everything(self):
        self.fh.file('~/.claude/plugins/installed_plugins.json', content='{nope')
        self.check_unreadable('invalid')

    def test_unreadable_manifest_protects_everything(self):
        p = self.fh.file('~/.claude/plugins/installed_plugins.json', content='{}')
        os.chmod(p, 0)
        self.addCleanup(os.chmod, p, 0o644)
        self.check_unreadable('unreadable')

    def test_unlistable_cache_protects_everything(self):
        self.versions('1.0')
        self.manifest({'version': 2, 'plugins': {}})
        cache = self.fh.path('~/.claude/plugins/cache')
        os.chmod(cache, 0)
        self.addCleanup(os.chmod, cache, 0o755)
        facts = prepare(self.fh, 'claude_plugins')
        self.assertEqual(facts, {'status': 'unreadable', 'installed': [], 'cache': []})

    def test_unexpected_shapes_protect_everything(self):
        shapes = [[], {'version': 3, 'plugins': {}}, {'version': 2}, {'version': 2, 'plugins': []},
                  {'version': 2, 'plugins': {'p': {}}}, {'version': 2, 'plugins': {'p': [{'x': 1}]}},
                  {'version': 2, 'plugins': {'p': [{'installPath': 'relative/path'}]}},
                  {'version': 2, 'plugins': {'p': [{'installPath': 5}]}}]
        for shape in shapes:
            with self.subTest(shape=shape):
                fh = FakeHome().__enter__()
                try:
                    fh.file('~/.claude/plugins/installed_plugins.json', content=json.dumps(shape))
                    self.assertEqual(prepare(fh, 'claude_plugins')['status'], 'invalid')
                finally:
                    fh.cleanup()


def rollout(session_id, ts, lines=3, first_extra=''):
    meta = {'timestamp': ts, 'type': 'session_meta', 'payload': {'id': session_id, 'timestamp': ts}}
    return json.dumps(meta) + first_extra + '\n' + ('{"type":"x"}\n' * lines)


class CodexDupesTests(Base):
    SESS = '~/.codex/sessions/2026/10/02'
    # version nibble (15th character of the uuid): 7 = new id, 5 = legacy name-derived id
    NEW1, NEW2 = ID1, '44444444-aaaa-7000-8000-000000000004'
    OLD1, OLD2 = ID2, ID3

    def name(self, sid, stamp='2026-10-02T11-04-41'):
        return f'rollout-{stamp}-{sid}.jsonl'

    def put(self, sid, ts='2026-10-02T11:04:41Z', where=None, **kw):
        logical = f'{where or self.SESS}/{self.name(sid)}'
        self.fh.file(logical, content=rollout(sid, ts, **kw))
        return logical

    def index(self, *ids):
        self.fh.file('~/.codex/session_index.jsonl',
                     content=''.join(json.dumps({'id': i, 'thread_name': 't'}) + '\n' for i in ids))

    def ref(self, facts, logical, rule='codex.sessions'):
        return refine('codex_dupes', rule, logical, facts)

    def test_version_nibble(self):
        from disk_steward.detectors.codex_dupes import _is_legacy_id
        self.assertTrue(_is_legacy_id('c0ba8ee6-b51e-5e2e-9a95-c924bd7a3c53'))
        self.assertFalse(_is_legacy_id('01a03337-7f73-7843-b49b-6acd9e15feaf'))
        self.assertFalse(_is_legacy_id('55555555-5555-4555-8555-555555555555'))

    def test_pair_only_the_legacy_copy_lands_in_the_duplicates_group(self):
        new, old = self.put(self.NEW1), self.put(self.OLD1)
        self.index(self.NEW1, self.OLD1)
        facts = prepare(self.fh, 'codex_dupes')
        self.assertEqual(sorted(facts['dupes']), sorted([new, old]))
        legacy = self.ref(facts, old)
        self.assertEqual(legacy.group_key, 'codex.sessions:duplicates')
        self.assertEqual(legacy.evidence, (Evidence('evidence.codex_legacy_copy', {'partner': self.name(self.NEW1)}),
                                           Evidence('evidence.codex_both_indexed', {})))
        kept = self.ref(facts, new)
        self.assertIsNone(kept.group_key)
        self.assertEqual(kept.evidence, (Evidence('evidence.codex_duplicate_kept', {'partner': self.name(self.OLD1)}),))
        self.assertIsNone(kept.tier)
        self.assertFalse(kept.skip)

    def test_partner_only_indexed_is_not_both_indexed(self):
        self.put(self.NEW1)
        old = self.put(self.OLD1)
        self.index(self.NEW1)
        facts = prepare(self.fh, 'codex_dupes')
        self.assertEqual(keys(self.ref(facts, old)), ['evidence.codex_legacy_copy'])

    def test_missing_index_still_pairs(self):
        self.put(self.NEW1)
        old = self.put(self.OLD1)
        facts = prepare(self.fh, 'codex_dupes')
        self.assertEqual(len(facts['dupes']), 2)
        self.assertEqual(keys(self.ref(facts, old)), ['evidence.codex_legacy_copy'])

    def test_equal_size_but_different_session_timestamp_is_not_a_pair(self):
        self.put(self.NEW1, ts='2026-10-02T11:04:41Z')
        self.put(self.OLD1, ts='2026-10-02T11:04:42Z')   # same file-name second, same size, different header
        self.assertEqual(prepare(self.fh, 'codex_dupes')['dupes'], {})

    def test_different_size_is_not_a_pair(self):
        self.put(self.NEW1)
        self.put(self.OLD1, lines=4)
        self.assertEqual(prepare(self.fh, 'codex_dupes')['dupes'], {})

    def test_different_start_time_in_the_name_is_not_a_pair(self):
        self.put(self.NEW1)
        self.fh.file(f'{self.SESS}/{self.name(self.OLD1, "2026-10-02T11-04-42")}',
                     content=rollout(self.OLD1, '2026-10-02T11:04:41Z'))
        self.assertEqual(prepare(self.fh, 'codex_dupes')['dupes'], {})

    def test_triple_of_two_legacy_copies_and_one_new_file(self):
        new, old1, old2 = self.put(self.NEW1), self.put(self.OLD1), self.put(self.OLD2)
        self.index(self.NEW1, self.OLD1, self.OLD2)
        facts = prepare(self.fh, 'codex_dupes')
        for old in (old1, old2):
            ref = self.ref(facts, old)
            self.assertEqual(ref.group_key, 'codex.sessions:duplicates')
            self.assertEqual(ref.evidence[0], Evidence('evidence.codex_legacy_copy', {'partner': self.name(self.NEW1)}))
            self.assertEqual(keys(ref), ['evidence.codex_legacy_copy', 'evidence.codex_both_indexed'])
        kept = self.ref(facts, new)
        self.assertIsNone(kept.group_key)
        self.assertEqual(kept.evidence, (Evidence('evidence.codex_duplicate_kept', {'partner': self.name(self.OLD1)}),))
        self.index(self.NEW1, self.OLD1)   # one member unindexed: the flag is about the whole set
        facts = prepare(self.fh, 'codex_dupes')
        self.assertEqual(keys(self.ref(facts, old1)), ['evidence.codex_legacy_copy'])

    def check_unresolved(self, logicals):
        facts = prepare(self.fh, 'codex_dupes')
        self.assertEqual(sorted(facts['dupes']), sorted(logicals))
        for logical in logicals:
            ref = self.ref(facts, logical)
            self.assertIsNone(ref.group_key, logical)
            self.assertIsNone(ref.tier)
            self.assertEqual(keys(ref), ['evidence.codex_duplicate_unresolved'])
            self.assertNotEqual(ref.evidence[0].args['partner'], last_name(logical))

    def test_two_new_ids_are_unresolved(self):
        self.check_unresolved([self.put(self.NEW1), self.put(self.NEW2)])

    def test_two_legacy_ids_are_unresolved(self):
        self.check_unresolved([self.put(self.OLD1), self.put(self.OLD2)])

    def test_one_legacy_and_two_new_ids_are_unresolved(self):
        self.check_unresolved([self.put(self.NEW1), self.put(self.NEW2), self.put(self.OLD1)])

    def test_pair_across_sessions_and_archived_sessions(self):
        for new_dir, old_dir in (('~/.codex/sessions/2026/10/02', '~/.codex/archived_sessions'),
                                 ('~/.codex/archived_sessions', '~/.codex/sessions/2026/10/02')):
            with self.subTest(new=new_dir, old=old_dir):
                fh = FakeHome().__enter__()
                try:
                    new = fh.file(f'{new_dir}/{self.name(self.NEW1)}', content=rollout(self.NEW1, '2026-10-02T11:04:41Z'))
                    old = fh.file(f'{old_dir}/{self.name(self.OLD1)}', content=rollout(self.OLD1, '2026-10-02T11:04:41Z'))
                    facts = prepare(fh, 'codex_dupes')
                    rule_of = lambda d: 'codex.archived-sessions' if 'archived' in d else 'codex.sessions'
                    legacy = refine('codex_dupes', rule_of(old_dir), f'{old_dir}/{self.name(self.OLD1)}', facts)
                    kept = refine('codex_dupes', rule_of(new_dir), f'{new_dir}/{self.name(self.NEW1)}', facts)
                    self.assertEqual(legacy.group_key, 'codex.sessions:duplicates')
                    self.assertIsNone(kept.group_key)
                    self.assertEqual(keys(kept), ['evidence.codex_duplicate_kept'])
                finally:
                    fh.cleanup()

    # content verification ------------------------------------------------------------------------------

    def put_bytes(self, sid, body, ts='2026-10-02T11:04:41Z', header_id=None, where=None):
        first = json.dumps({'timestamp': ts, 'type': 'session_meta',
                            'payload': {'id': header_id or sid, 'timestamp': ts}}).encode() + b'\n'
        logical = f'{where or self.SESS}/{self.name(sid)}'
        self.fh.file(logical, content=first + body)
        return logical, len(first)

    def test_content_that_differs_after_the_first_line_is_unresolved(self):
        a, _ = self.put_bytes(self.NEW1, b'{"type":"x"}\n' * 5)
        b, _ = self.put_bytes(self.OLD1, b'{"type":"x"}\n' * 4 + b'{"type":"y"}\n')   # same size
        facts = prepare(self.fh, 'codex_dupes')
        self.assertEqual({v['role'] for v in facts['dupes'].values()}, {'unresolved'})
        for logical in (a, b):
            ref = self.ref(facts, logical)
            self.assertIsNone(ref.group_key)
            self.assertEqual(keys(ref), ['evidence.codex_duplicate_unresolved'])

    def test_the_ids_in_the_content_are_masked_before_comparing(self):
        body = lambda sid: b'{"type":"x"}\n{"session":"' + sid.encode() + b'"}\n{"type":"x"}\n'
        self.put_bytes(self.NEW1, body(self.NEW1))
        old, _ = self.put_bytes(self.OLD1, body(self.OLD1))
        facts = prepare(self.fh, 'codex_dupes')
        self.assertEqual(facts['dupes'][old]['role'], 'legacy')

    def test_an_id_straddling_a_read_boundary_is_still_masked(self):
        for offset in (0, 3, 30, 50, 63, 64):
            with self.subTest(offset=offset), mock.patch.object(codex_dupes, 'CHUNK', 64):
                fh = FakeHome().__enter__()
                try:
                    self.fh = fh
                    body = lambda sid: b'x' * offset + sid.encode() + b'y' * 200
                    self.put_bytes(self.NEW1, body(self.NEW1))
                    old, _ = self.put_bytes(self.OLD1, body(self.OLD1))
                    self.assertEqual(prepare(fh, 'codex_dupes')['dupes'][old]['role'], 'legacy')
                finally:
                    fh.cleanup()

    def test_big_files_are_compared_in_eight_windows(self):
        with mock.patch.object(codex_dupes, 'FULL_COMPARE_MAX', 100), mock.patch.object(codex_dupes, 'WINDOW', 64):
            rest = 2000
            body = bytearray(b'a' * rest)

            def run(change_at=None):
                fh = FakeHome().__enter__()
                try:
                    self.fh = fh
                    other = bytearray(body)
                    if change_at is not None:
                        other[change_at] ^= 1
                    self.put_bytes(self.NEW1, bytes(body))
                    old, _ = self.put_bytes(self.OLD1, bytes(other))
                    return prepare(fh, 'codex_dupes')['dupes'][old]['role']
                finally:
                    fh.cleanup()
            self.assertEqual(run(), 'legacy')
            windows = [i * (rest - 64) // 7 for i in range(8)]
            self.assertEqual(run(windows[0] + 5), 'unresolved')
            self.assertEqual(run(windows[3] + 63), 'unresolved')
            self.assertEqual(run(windows[7] + 63), 'unresolved')       # the last window ends at the end of the file
            self.assertEqual(run(windows[0] + 100), 'legacy')          # between windows: not looked at

    def test_a_big_file_is_not_read_whole(self):
        with mock.patch.object(codex_dupes, 'FULL_COMPARE_MAX', 100), mock.patch.object(codex_dupes, 'WINDOW', 64):
            body = b'z' * 5000
            self.put_bytes(self.NEW1, body)
            self.put_bytes(self.OLD1, body)
            read = []
            real_open = open

            def spy(path, mode='r', *a, **kw):
                f = real_open(path, mode, *a, **kw)
                if 'rollout-' in str(path) and 'b' in mode:
                    orig = f.read
                    f.read = lambda n=-1: (read.append(n), orig(n))[1]
                return f
            with mock.patch('builtins.open', spy):
                prepare(self.fh, 'codex_dupes')
            self.assertTrue(read)
            self.assertLessEqual(max(read), 64 + 35 + 35)

    def test_first_line_must_name_the_id_of_its_own_file(self):
        self.put_bytes(self.NEW1, b'x\n')
        self.put_bytes(self.OLD1, b'x\n', header_id=self.OLD2)   # header says another id
        self.assertEqual(prepare(self.fh, 'codex_dupes')['dupes'], {})

    def test_a_member_with_a_foreign_header_id_drops_out_of_a_triple(self):
        self.put_bytes(self.NEW1, b'x\n')
        old, _ = self.put_bytes(self.OLD1, b'x\n')
        self.put_bytes(self.OLD2, b'x\n', header_id=self.NEW2)
        facts = prepare(self.fh, 'codex_dupes')
        self.assertEqual(len(facts['dupes']), 2)
        self.assertEqual(facts['dupes'][old]['role'], 'legacy')

    def test_header_id_comparison_ignores_case(self):
        self.put_bytes(self.NEW1, b'x\n', header_id=self.NEW1.upper())
        old, _ = self.put_bytes(self.OLD1, b'x\n')
        self.assertEqual(prepare(self.fh, 'codex_dupes')['dupes'][old]['role'], 'legacy')

    def test_pair_in_different_day_folders(self):
        self.put(self.NEW1)
        self.put(self.OLD1, where='~/.codex/sessions/2026/10/03')
        self.assertEqual(len(prepare(self.fh, 'codex_dupes')['dupes']), 2)

    def test_other_files_are_ignored(self):
        self.put(self.NEW1)
        self.fh.file(f'{self.SESS}/notes.jsonl', content=rollout(self.OLD1, '2026-10-02T11:04:41Z'))
        self.fh.file(f'{self.SESS}/rollout-2026-10-02T11-04-41-short.jsonl', content='{}\n')
        self.assertEqual(prepare(self.fh, 'codex_dupes')['dupes'], {})

    def test_unparseable_or_oversized_first_line_is_never_a_pair(self):
        self.fh.file(f'{self.SESS}/{self.name(self.NEW1)}', content='not json\n' + 'x' * 10)
        self.fh.file(f'{self.SESS}/{self.name(self.OLD1)}', content='not json\n' + 'x' * 10)
        self.assertEqual(prepare(self.fh, 'codex_dupes')['dupes'], {})
        fh = FakeHome().__enter__()
        try:
            big = rollout(self.NEW1, '2026-10-02T11:04:41Z', first_extra=' ' * (1024 * 1024 + 10))
            fh.file(f'{self.SESS}/{self.name(self.NEW1)}', content=big)
            fh.file(f'{self.SESS}/{self.name(self.OLD1)}', content=big.replace(self.NEW1, self.OLD1))
            self.assertEqual(prepare(fh, 'codex_dupes')['dupes'], {})
        finally:
            fh.cleanup()

    def test_symlinked_rollouts_are_ignored(self):
        self.put(self.NEW1)
        self.fh.symlink(f'{self.SESS}/{self.name(self.OLD1)}', f'{self.SESS}/{self.name(self.NEW1)}')
        self.assertEqual(prepare(self.fh, 'codex_dupes')['dupes'], {})

    def test_deterministic_and_json_safe(self):
        for i in (self.NEW1, self.OLD1, self.OLD2):
            self.put(i)
        self.put(self.NEW1, where='~/.codex/sessions/2026/09/01', ts='2026-09-01T00:00:00Z')
        a, b = prepare(self.fh, 'codex_dupes'), prepare(self.fh, 'codex_dupes')
        self.assertEqual(json.dumps(a), json.dumps(b))
        self.assertEqual(facts_digest({'codex_dupes': a}), facts_digest({'codex_dupes': b}))

    def test_unpaired_file_is_not_refined(self):
        solo = self.put(self.NEW1)
        facts = prepare(self.fh, 'codex_dupes')
        self.assertIsNone(self.ref(facts, solo))

    def test_no_codex_dir(self):
        self.assertEqual(prepare(self.fh, 'codex_dupes'), {'dupes': {}})


def last_name(logical):
    return logical.rpartition('/')[2]


class CodexStateTmpTests(Base):
    TMP = '~/.codex/..codex-global-state.json.tmp-1-a'

    def test_applies_only_when_the_state_file_exists(self):
        self.fh.file(self.TMP, size=1)
        facts = prepare(self.fh, 'codex_state_tmp')
        self.assertEqual(facts, {'state_file': False})
        self.assertTrue(refine('codex_state_tmp', 'codex.state-tmp', self.TMP, facts).skip)
        self.fh.file('~/.codex/.codex-global-state.json', size=1)
        facts = prepare(self.fh, 'codex_state_tmp')
        self.assertEqual(facts, {'state_file': True})
        self.assertIsNone(refine('codex_state_tmp', 'codex.state-tmp', self.TMP, facts))

    def test_a_directory_of_that_name_does_not_count(self):
        self.fh.dir('~/.codex/.codex-global-state.json')
        self.assertEqual(prepare(self.fh, 'codex_state_tmp'), {'state_file': False})


class EditorExtensionsTests(Base):
    EXT = '~/.cursor/extensions'

    def ref(self, facts, name, rule='cursor.extensions'):
        return refine('editor_extensions', rule, f'{self.EXT}/{name}', facts, True)

    def test_only_listed_folders_apply(self):
        self.fh.file(f'{self.EXT}/.obsolete', content=json.dumps({'a.old-1.0.0': True, 'b.keep-1': False}))
        for n in ('a.old-1.0.0', 'a.new-2.0.0', 'b.keep-1'):
            self.fh.dir(f'{self.EXT}/{n}')
        facts = prepare(self.fh, 'editor_extensions')
        self.assertEqual(facts['dirs'][self.EXT], {'status': 'ok', 'obsolete': ['a.old-1.0.0']})
        r = self.ref(facts, 'a.old-1.0.0')
        self.assertFalse(r.skip)
        self.assertEqual(r.evidence, (Evidence('evidence.extension_obsolete', {'name': 'a.old-1.0.0'}),))
        self.assertTrue(self.ref(facts, 'a.new-2.0.0').skip)
        self.assertTrue(self.ref(facts, 'b.keep-1').skip)

    def test_missing_obsolete_file_skips_everything(self):
        self.fh.dir(f'{self.EXT}/a.old-1.0.0')
        facts = prepare(self.fh, 'editor_extensions')
        self.assertEqual(facts['dirs'][self.EXT]['status'], 'missing')
        self.assertTrue(self.ref(facts, 'a.old-1.0.0').skip)

    def test_invalid_obsolete_file_skips_everything(self):
        for content in ('{nope', '["a.old-1.0.0"]'):
            with self.subTest(content=content):
                fh = FakeHome().__enter__()
                try:
                    fh.file(f'{self.EXT}/.obsolete', content=content)
                    facts = prepare(fh, 'editor_extensions')
                    self.assertEqual(facts['dirs'][self.EXT]['status'], 'invalid')
                    self.assertTrue(self.ref(facts, 'a.old-1.0.0').skip)
                finally:
                    fh.cleanup()

    def test_each_editor_has_its_own_directory(self):
        self.fh.file('~/.windsurf/extensions/.obsolete', content=json.dumps({'w.old-1': True}))
        facts = prepare(self.fh, 'editor_extensions')
        self.assertEqual(set(facts['dirs']), {'~/.cursor/extensions', '~/.antigravity/extensions',
                                              '~/.windsurf/extensions', '~/.vscode/extensions'})
        self.assertFalse(refine('editor_extensions', 'windsurf.extensions', '~/.windsurf/extensions/w.old-1',
                                facts, True).skip)
        self.assertTrue(refine('editor_extensions', 'cursor.extensions', '~/.cursor/extensions/w.old-1',
                               facts, True).skip)


class NewestDirTests(Base):
    BASE = '~/Library/Application Support/Cursor/CachedData'

    def test_newest_by_changed_time_is_kept(self):
        for name, age in (('h1', 30), ('h2', 3), ('h3', 10)):
            self.fh.file(f'{self.BASE}/{name}/f', size=1)
            self.fh.set_age(f'{self.BASE}/{name}', age)
        facts = prepare(self.fh, 'newest_dir')
        self.assertEqual(facts['newest'][self.BASE], 'h2')
        r = refine('newest_dir', 'cursor.cached-data', f'{self.BASE}/h2', facts, True)
        self.assertEqual((r.tier, keys(r)), (Tier.PROTECTED, ['evidence.newest_dir']))
        self.assertIsNone(refine('newest_dir', 'cursor.cached-data', f'{self.BASE}/h1', facts, True))

    def test_files_are_not_candidates_and_ties_break_by_name(self):
        self.fh.file(f'{self.BASE}/stray-file', size=1)
        self.fh.dir(f'{self.BASE}/a', age_days=5)
        self.fh.dir(f'{self.BASE}/b', age_days=5)
        self.fh.set_age(f'{self.BASE}/stray-file', 0)
        self.assertEqual(prepare(self.fh, 'newest_dir')['newest'][self.BASE], 'b')

    def test_missing_or_empty_directory(self):
        self.assertIsNone(prepare(self.fh, 'newest_dir')['newest'][self.BASE])
        self.fh.dir(self.BASE)
        facts = prepare(self.fh, 'newest_dir')
        self.assertEqual((facts['newest'][self.BASE], facts['candidates'][self.BASE]), (None, []))
        # a folder that shows up later was never looked at
        r = refine('newest_dir', 'cursor.cached-data', f'{self.BASE}/h1', facts, True)
        self.assertEqual((r.tier, keys(r)), (Tier.PROTECTED, ['evidence.appeared_during_scan']))

    def test_facts_record_the_candidates_and_the_newest_stamp(self):
        self.fh.file(f'{self.BASE}/stray-file', size=1)
        for name, age in (('h1', 30), ('h2', 3)):
            self.fh.file(f'{self.BASE}/{name}/f', size=1)
            self.fh.set_age(f'{self.BASE}/{name}', age)
        facts = prepare(self.fh, 'newest_dir')
        self.assertEqual(facts['candidates'][self.BASE], ['h1', 'h2'])
        self.assertEqual(facts['newest_ns'][self.BASE], StatInfo.from_stat(os.lstat(self.fh.path(f'{self.BASE}/h2'))).changed_ns)

    def test_a_folder_that_appeared_or_became_newer_after_prepare_is_protected(self):
        for name, age in (('h1', 30), ('h2', 3)):
            self.fh.file(f'{self.BASE}/{name}/f', size=1)
            self.fh.set_age(f'{self.BASE}/{name}', age)
        facts = prepare(self.fh, 'newest_dir')
        self.fh.file(f'{self.BASE}/h3/f', size=1)   # new folder
        r = refine('newest_dir', 'cursor.cached-data', f'{self.BASE}/h3', facts, True)
        self.assertEqual((r.tier, keys(r)), (Tier.PROTECTED, ['evidence.appeared_during_scan']))
        self.fh.set_age(f'{self.BASE}/h1', 0)   # an old folder is written to and is now the newest
        info = StatInfo.from_stat(os.lstat(self.fh.path(f'{self.BASE}/h1')))
        r = REGISTRY['newest_dir'].refine(CAT.rule('cursor.cached-data'), f'{self.BASE}/h1', info, True, facts)
        self.assertEqual((r.tier, keys(r)), (Tier.PROTECTED, ['evidence.appeared_during_scan']))
        info = StatInfo.from_stat(os.lstat(self.fh.path(f'{self.BASE}/h2')))
        self.assertEqual(REGISTRY['newest_dir'].refine(CAT.rule('cursor.cached-data'), f'{self.BASE}/h2', info, True,
                                                       facts).evidence[0].key, 'evidence.newest_dir')

    def test_pattern_must_be_a_literal_dir_with_a_trailing_star(self):
        for pattern in ('~/a/*/b/*', '~/a/b'):
            with self.subTest(pattern=pattern):
                bad = Rule(id='x.y', provider='p', domain=Domain.AGENT, tier=Tier.JUNK, kind=Kind.CACHE,
                           patterns=(pattern,), unit=CAT.rule('cursor.cached-data').unit, detector='newest_dir',
                           actions=CAT.rule('cursor.cached-data').actions,
                           default_action=CAT.rule('cursor.cached-data').default_action)
                ctx = DetectorContext(self.fh.bases, FakeMacOS(), Settings(), (bad,))
                with self.assertRaises(ValueError):
                    REGISTRY['newest_dir'].prepare(ctx)


class ComputeFactsTests(Base):
    def ctx(self):
        return DetectorContext(self.fh.bases, FakeMacOS(), Settings())

    def test_only_referenced_detectors_run(self):
        plain = build_catalog([r for r in CAT.rules if r.detector is None], CAT.roots, CAT.privacy_skip)
        self.assertEqual(compute_facts(self.ctx(), plain)[0], {})
        only = build_catalog([CAT.rule('claude.projects'), CAT.rule('claude.cli-versions'), CAT.rule('codex.state-tmp')],
                             CAT.roots, CAT.privacy_skip)
        facts, digest = compute_facts(self.ctx(), only)
        self.assertEqual(set(facts), {'claude_versions', 'codex_state_tmp'})
        self.assertEqual(digest, facts_digest(facts))

    def test_full_catalog_covers_every_registered_detector(self):
        facts, _ = compute_facts(self.ctx(), CAT)
        self.assertEqual(set(facts), set(REGISTRY))
        json.dumps(facts)

    def test_hash_follows_disk_state(self):
        _, h1 = compute_facts(self.ctx(), CAT)
        _, h2 = compute_facts(self.ctx(), CAT)
        self.assertEqual(h1, h2)
        self.fh.file('~/.codex/.codex-global-state.json', size=1)
        _, h3 = compute_facts(self.ctx(), CAT)
        self.assertNotEqual(h1, h3)

    def test_unknown_detector_is_an_error(self):
        broken = build_catalog([Rule(id='x.y', provider='p', domain=Domain.AGENT, tier=Tier.PROTECTED, kind=Kind.CACHE,
                                     patterns=('~/x/**',), detector='nope')], (), ())
        with self.assertRaisesRegex(ValueError, 'unknown detector'):
            compute_facts(self.ctx(), broken)

    def test_detector_facts_must_be_json_safe(self):
        class Bad:
            name = 'bad'

            def prepare(self, ctx):
                return {'x': {1, 2}}

            def refine(self, *a):
                return None
        cat = build_catalog([CAT.rule('codex.state-tmp')], (), ())
        with mock.patch.dict(REGISTRY, {'codex_state_tmp': Bad()}):
            with self.assertRaises(TypeError):
                compute_facts(self.ctx(), cat)


SECRET_LINE = 'the user typed a very secret prompt about the quarterly numbers'


def settings_event(ts, pad=0):
    return json.dumps({'timestamp': ts, 'type': 'event_msg',
                       'payload': {'type': 'thread_settings_applied', 'cwd': '/private/project/location',
                                   'pad': 'p' * pad}}) + '\n'


class CodexBloatTests(Base):
    def write(self, text):
        path = self.fh.path('~/.codex/sessions/s.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode('utf-8') if isinstance(text, str) else text)
        return str(path)

    def sample(self):
        lines = [json.dumps({'timestamp': '2026-10-01T00:00:00Z', 'type': 'session_meta',
                             'payload': {'id': ID1, 'cwd': '/private/project/location'}}) + '\n',
                 json.dumps({'timestamp': '2026-10-01T00:00:01Z', 'type': 'response_item',
                             'payload': {'type': 'message', 'content': [{'text': SECRET_LINE}]}}) + '\n',
                 settings_event('2026-10-01T00:00:02Z', 300), settings_event('2026-10-01T00:00:09Z', 300),
                 settings_event('2026-10-01T00:00:05Z', 300),
                 'this line is not json and mentions nothing_secret_at_all_really\n',
                 json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'content': SECRET_LINE}}) + '\n']
        return ''.join(lines), lines

    def test_counts_bytes_and_settings_fraction(self):
        text, lines = self.sample()
        result = codex_bloat.diagnose(self.write(text))
        self.assertEqual(result['bytes'], len(text.encode()))
        self.assertEqual((result['lines'], result['invalid_lines']), (7, 1))
        by_type = result['by_type']
        self.assertEqual(by_type['event_msg/thread_settings_applied']['count'], 3)
        self.assertEqual(by_type['session_meta'], {'count': 1, 'bytes': len(lines[0].encode())})
        self.assertEqual(by_type['response_item/message']['count'], 2)
        settings = result['settings']
        self.assertEqual(settings['count'], 3)
        self.assertEqual(settings['bytes'], sum(len(l.encode()) for l in lines[2:5]))
        self.assertAlmostEqual(settings['fraction'], settings['bytes'] / result['bytes'])
        self.assertEqual((settings['first_timestamp'], settings['last_timestamp']),
                         ('2026-10-01T00:00:02Z', '2026-10-01T00:00:09Z'))
        self.assertFalse(result['changed_during_read'])
        self.assertEqual(sum(v['bytes'] for v in by_type.values()) + len(lines[5].encode()), result['bytes'])

    def test_result_never_contains_input_text(self):
        text, _ = self.sample()
        out = json.dumps(codex_bloat.diagnose(self.write(text)))
        strings = set()

        def collect(v, key=None):
            if isinstance(v, dict):
                for k, x in v.items():
                    collect(x, k)
            elif isinstance(v, list):
                for x in v:
                    collect(x)
            elif isinstance(v, str) and key not in ('type',):
                strings.add(v)
        for line in text.splitlines():
            try:
                collect(json.loads(line))
            except ValueError:
                strings.add(line)
        long_strings = {s for s in strings if len(s) > 20}
        self.assertTrue(long_strings)
        for s in long_strings:
            self.assertNotIn(s, out)
        for piece in ('secret', 'quarterly', '/private/project', ID1, 'nothing_secret'):
            self.assertNotIn(piece, out)

    def test_type_labels_are_restricted_to_a_safe_alphabet(self):
        weird = json.dumps({'type': 'a very long free text that is not an event type at all, ' * 3}) + '\n'
        out = codex_bloat.diagnose(self.write(weird + weird))
        self.assertEqual(list(out['by_type']), ['other'])
        self.assertEqual(out['by_type']['other']['count'], 2)

    def test_distinct_labels_are_capped(self):
        text = ''.join(json.dumps({'type': f't{i}'}) + '\n' for i in range(codex_bloat.MAX_TYPES + 20))
        out = codex_bloat.diagnose(self.write(text))
        self.assertEqual(len(out['by_type']), codex_bloat.MAX_TYPES + 1)
        self.assertEqual(out['by_type']['other']['count'], 20)

    def test_non_object_lines_blank_lines_and_missing_trailing_newline(self):
        out = codex_bloat.diagnose(self.write('[1,2]\n\n"x"\n{"type":"a"}'))
        self.assertEqual((out['lines'], out['invalid_lines']), (4, 2))
        self.assertEqual(out['by_type'], {'a': {'count': 1, 'bytes': len('{"type":"a"}')}})

    def test_bad_timestamps_are_not_reported(self):
        evt = json.dumps({'timestamp': 'secret free text', 'type': 'event_msg',
                          'payload': {'type': 'thread_settings_applied'}}) + '\n'
        out = codex_bloat.diagnose(self.write(evt))
        self.assertEqual(out['settings']['count'], 1)
        self.assertIsNone(out['settings']['first_timestamp'])
        self.assertNotIn('secret', json.dumps(out))

    def test_oversized_line_is_counted_as_invalid_without_parsing(self):
        with mock.patch.object(codex_bloat, 'MAX_LINE', 64):
            text = '{"type":"a"}\n' + '{"type":"' + 'z' * 200 + '"}\n' + '{"type":"b"}\n'
            out = codex_bloat.diagnose(self.write(text))
        self.assertEqual((out['lines'], out['invalid_lines'], out['bytes']), (3, 1, len(text)))
        self.assertEqual(set(out['by_type']), {'a', 'b'})

    def test_empty_file(self):
        out = codex_bloat.diagnose(self.write(''))
        self.assertEqual((out['bytes'], out['lines'], out['settings']['fraction']), (0, 0, 0.0))

    def test_detects_a_file_that_changes_while_it_is_read(self):
        path = self.write('{"type":"a"}\n{"type":"b"}\n')
        real = json.loads
        seen = []

        def appending(data, *a, **kw):
            if not seen:
                seen.append(1)
                with open(path, 'ab') as f:
                    f.write(b'{"type":"c"}\n')
            return real(data, *a, **kw)
        with mock.patch.object(codex_bloat.json, 'loads', appending):
            out = codex_bloat.diagnose(path)
        self.assertTrue(out['changed_during_read'])

    def test_is_not_a_registered_detector(self):
        self.assertNotIn('codex_bloat', REGISTRY)


if __name__ == '__main__':
    unittest.main()
