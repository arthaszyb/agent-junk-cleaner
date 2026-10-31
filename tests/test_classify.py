from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

import m2_fixture as fx
from disk_steward.catalog import ScanRoot, build_catalog, load_catalog, protections
from disk_steward.classify import PROTECTED_SUFFIX, Classifier
from disk_steward.detectors import DetectorContext, compute_facts
from disk_steward.fsutil import stat_nofollow
from disk_steward.macos import FakeMacOS
from disk_steward.fsutil import SF_DATALESS
from disk_steward.model import (Action, Domain, Evidence, Kind, Rule, StatInfo, Tier, Unit)
from disk_steward.paths import Bases
from disk_steward.settings import Settings

from support import FakeHome

BASES = Bases(Path('/h'), Path('/t'), Path('/c'))


def st(size=0, **kw):
    base = dict(dev=1, ino=1, mode=0o100644, nlink=1, uid=1, size=size, blocks=0, mtime_ns=0, birth_ns=0, flags=0)
    base.update(kw)
    return StatInfo(**base)


def rule(**kw):
    base = dict(id='t.rule', provider='codex', domain=Domain.AGENT, tier=Tier.JUNK, kind=Kind.CACHE,
                patterns=('~/x/**',), actions=(Action.DELETE,), default_action=Action.DELETE)
    base.update(kw)
    return Rule(**base)


def classifier(*rules, privacy=(), facts=None):
    cat = build_catalog(rules, [ScanRoot('~/x', Domain.AGENT)], privacy)
    return Classifier(cat, facts or {}, BASES, Settings())


class RealisticTreeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fh = FakeHome().__enter__()
        fx.build(cls.fh)
        cls.classifier, cls.catalog, cls.facts = fx.make_classifier(cls.fh)

    @classmethod
    def tearDownClass(cls):
        cls.fh.cleanup()

    def test_expected_table(self):
        for logical, (rule_id, tier, group_key, is_dir) in fx.EXPECTED.items():
            with self.subTest(path=logical):
                m = self.classifier.classify(logical, stat_nofollow(self.fh.path(logical)), is_dir)
                self.assertEqual((m.rule_id, m.tier.value, m.group_key), (rule_id, tier, group_key))

    def match(self, logical, is_dir=False):
        return self.classifier.classify(logical, stat_nofollow(self.fh.path(logical)), is_dir)

    def test_memory_and_cache_db_and_sessiondata_are_protected(self):
        mem = self.match('~/.claude/projects/-Users-me-proj/memory/MEMORY.md')
        self.assertEqual((mem.rule_id, mem.tier), ('claude.memory', Tier.PROTECTED))
        db = self.match('~/Library/Caches/com.anthropic.claudefordesktop/Cache.db')
        self.assertEqual((db.rule_id, db.tier, db.kind), ('prot.databases', Tier.PROTECTED, Kind.DATABASE))
        sd = self.match(f'{fx.SUPPORT}/vm_bundles/b1/sessiondata.img')
        self.assertEqual((sd.rule_id, sd.tier, sd.kind), ('unknown', Tier.PROTECTED, Kind.UNKNOWN))

    def test_cli_versions(self):
        v = '~/.local/share/claude/versions'
        got = {n: self.match(f'{v}/{n}') for n in ('2.1.247', '2.1.289', '2.1.299')}
        self.assertEqual({n: m.tier for n, m in got.items()},
                         {'2.1.247': Tier.JUNK, '2.1.289': Tier.JUNK, '2.1.299': Tier.PROTECTED})
        self.assertEqual(got['2.1.299'].evidence[0].key, 'evidence.current_version')
        self.assertEqual(got['2.1.247'].evidence, (Evidence('evidence.not_current_version',
                                                            {'current': '2.1.299', 'newest': '2.1.299'}),))

    def test_desktop_engines(self):
        self.assertEqual(self.match(f'{fx.ENGINES}/2.1.304', True).tier, Tier.JUNK)
        self.assertEqual(self.match(f'{fx.ENGINES}/2.1.308', True).tier, Tier.PROTECTED)
        self.assertEqual(self.match(f'{fx.VM}/2.1.246', True).evidence[0].key, 'evidence.engine_pinned')
        self.assertEqual(self.match(f'{fx.VM}/2.1.250', True).tier, Tier.PROTECTED)
        self.assertEqual(self.match(f'{fx.VM}/2.1.240', True).tier, Tier.JUNK)

    def test_only_the_legacy_id_copy_is_grouped_as_a_duplicate(self):
        new, legacy = self.match(fx.PAIR_A), self.match(fx.PAIR_B)
        self.assertEqual(legacy.group_key, 'codex.sessions:duplicates')
        self.assertEqual([e.key for e in legacy.evidence], ['evidence.codex_legacy_copy', 'evidence.codex_both_indexed'])
        self.assertTrue(legacy.evidence[0].args['partner'].endswith(f'-{fx.ID_A}.jsonl'))
        self.assertEqual(new.group_key, 'codex.sessions:~/.codex/sessions/2026/10')
        self.assertEqual([e.key for e in new.evidence], ['evidence.codex_duplicate_kept'])
        self.assertTrue(new.evidence[0].args['partner'].endswith(f'-{fx.ID_B}.jsonl'))
        solo = self.match(fx.SOLO)
        self.assertEqual(solo.group_key, new.group_key)
        self.assertEqual(solo.evidence, ())

    def test_group_keys_follow_the_depth_of_the_rule(self):
        self.assertEqual(self.match(fx.SOLO).group_key, 'codex.sessions:~/.codex/sessions/2026/10')
        self.assertEqual(self.match('~/.claude/projects/-Users-me-proj/a/subagents/x.jsonl').group_key,
                         'claude.projects:~/.claude/projects/-Users-me-proj')

    def test_state_tmp_beats_state_and_state_beats_nothing(self):
        self.assertEqual(self.match('~/.codex/..codex-global-state.json.tmp-1784-aaaa').rule_id, 'codex.state-tmp')
        self.assertEqual(self.match('~/.codex/.codex-global-state.json').rule_id, 'codex.state')

    def test_state_tmp_is_not_junk_without_the_state_file(self):
        with FakeHome() as fh:
            fh.file('~/.codex/..codex-global-state.json.tmp-1-a', size=1)
            cl, _, _ = fx.make_classifier(fh)
            m = cl.classify('~/.codex/..codex-global-state.json.tmp-1-a',
                            stat_nofollow(fh.path('~/.codex/..codex-global-state.json.tmp-1-a')))
            self.assertEqual((m.rule_id, m.tier), ('codex.state', Tier.PROTECTED))

    def test_missing_symlink_makes_every_cli_version_protected(self):
        with FakeHome() as fh:
            for v in ('2.1.1', '2.1.2'):
                fh.file(f'~/.local/share/claude/versions/{v}', size=1)
            cl, _, _ = fx.make_classifier(fh)
            for v in ('2.1.1', '2.1.2'):
                m = cl.classify(f'~/.local/share/claude/versions/{v}', stat_nofollow(
                    fh.path(f'~/.local/share/claude/versions/{v}')))
                self.assertEqual((m.tier, m.evidence[0].key, m.group_key),
                                 (Tier.PROTECTED, 'evidence.version_unverifiable',
                                  'claude.cli-versions#protected'))

    def test_non_version_file_in_the_versions_dir_is_unknown(self):
        m = self.classifier.classify('~/.local/share/claude/versions/latest', st())
        self.assertEqual(m.rule_id, 'unknown')

    def test_names_match_case_insensitively(self):
        for name in ('AUTH.JSON', 'Auth.Json', 'ID_RSA', 'Server.PEM', 'logs_2.SQLite'):
            m = self.classifier.classify(f'~/.codex/skills/{name}', st())
            self.assertEqual(m.tier, Tier.PROTECTED, name)
            self.assertTrue(m.rule_id.startswith('prot.'), name)

    def test_credentials_anywhere_under_the_home_are_protected(self):
        for logical in ('~/Documents/x/id_ed25519', '~/Library/Caches/foo/credentials.json',
                        '~/.codex/log/token.json', '~/.codex/cache/my-token.json', '$TMPDIR/a/b/server.key'):
            m = self.classifier.classify(logical, st())
            self.assertEqual(m.rule_id, 'prot.credentials', logical)

    def test_git_directories_are_protected_bundles(self):
        m = self.classifier.classify_dir('~/Projects/app/.git', st(mode=0o040755))
        self.assertEqual((m.rule_id, m.tier, m.unit), ('prot.git', Tier.PROTECTED, Unit.BUNDLE))
        self.assertIsNone(self.classifier.classify_dir('~/Projects/app/src', st(mode=0o040755)))

    def test_protections_take_the_domain_of_the_root_and_group_by_domain(self):
        c = self.classifier
        for domain in Domain:
            with self.subTest(domain=domain):
                m = c.classify('~/Somewhere/data.sqlite', st(), root_domain=domain)
                self.assertEqual((m.rule_id, m.domain, m.group_key), ('prot.databases', domain,
                                                                      f'prot.databases:{domain.value}'))
                m = c.classify('~/Somewhere/auth.json', st(), root_domain=domain)
                self.assertEqual((m.domain, m.group_key), (domain, f'prot.credentials:{domain.value}'))
                g = c.classify_dir('~/Somewhere/app/.git', st(mode=0o040755), domain)
                self.assertEqual((g.rule_id, g.domain, g.group_key), ('prot.git', domain, f'prot.git:{domain.value}'))
        # ordinary rules keep the domain they were written for
        m = c.classify('~/.codex/cache/models.json', st(), root_domain=Domain.APP_LIBRARY)
        self.assertEqual((m.domain, m.group_key), (Domain.AGENT, 'codex.cache'))

    def test_classify_dir_returns_none_for_plain_directories(self):
        for logical in ('~/.codex', '~/.codex/sessions', '~/.claude/projects', '~/.cache/codex-runtimes',
                        '~/.codex/sessions/2026/10'):
            self.assertIsNone(self.classifier.classify_dir(logical, st(mode=0o040755)), logical)

    def test_classify_with_is_dir_falls_back_to_a_protected_unknown_bundle(self):
        m = self.classifier.classify('~/.codex/skills', st(mode=0o040755), is_dir=True, root_domain=Domain.AGENT)
        self.assertEqual((m.rule_id, m.unit, m.tier, m.group_key), ('unknown', Unit.BUNDLE, Tier.PROTECTED,
                                                                     'unknown:~/.codex/skills'))

    def test_unknown_group_uses_three_segments_and_four_inside_library_containers(self):
        c = self.classifier
        self.assertEqual(c.classify('~/.codex/skills/x/y.md', st()).rule_id, 'codex.user-content')
        self.assertEqual(c.classify('~/.gemini/antigravity/deep/er/file', st()).group_key, 'unknown:~/.gemini/antigravity')
        self.assertEqual(c.classify('~/.gemini/file', st()).group_key, 'unknown:~/.gemini')
        self.assertEqual(c.classify('~/Library/Logs/Claude/x.bin', st()).group_key, 'unknown:~/Library/Logs/Claude')
        self.assertEqual(c.classify('~/Library/Foo/Bar/x', st()).group_key, 'unknown:~/Library/Foo')

    def test_unknown_takes_the_root_domain(self):
        m = self.classifier.classify('~/.gemini/x', st(), root_domain=Domain.USER_FILES)
        self.assertEqual((m.domain, m.rule_id), (Domain.USER_FILES, 'unknown'))

    def test_facts_are_required_for_detector_rules(self):
        with self.assertRaisesRegex(ValueError, 'no facts'):
            Classifier(self.catalog, {}, self.fh.bases, Settings())

    def test_facts_hash_is_exposed(self):
        self.assertRegex(self.classifier.facts_hash, r'^[0-9a-f]{64}$')


class PrivacyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        catalog = load_catalog(Settings())
        bases = Bases(Path('/nonexistent-home'), Path('/t'), Path('/c'))
        facts, _ = compute_facts(DetectorContext(bases, FakeMacOS(), Settings()), catalog)
        cls.classifier = Classifier(catalog, facts, bases, Settings())

    def test_listed_directories_and_everything_below_them(self):
        c = self.classifier
        for logical in ('~/Library/Messages', '~/Library/Messages/chat.db', '~/Library/Mail/V10/x',
                        '~/Library/Containers/com.x/Data', '~/Library/Metadata/CoreSpotlight/a',
                        '~/Library/Application Support/AddressBook', '~/.Trash', '~/.Trash/old',
                        '~/Music/Music/Media', '~/Library/Caches/com.apple.Safari/x',
                        '~/Library/Application Support/com.apple.sharedfilelist', '~/Pictures/Family.photoslibrary/x',
                        '~/Library/Mobile Documents/com~apple~CloudDocs', '~/Library/CloudStorage/OneDrive'):
            with self.subTest(path=logical):
                self.assertTrue(c.privacy_skipped(logical))

    def test_ordinary_paths_are_not_skipped(self):
        c = self.classifier
        for logical in ('~', '~/Library', '~/Library/Caches', '~/Library/Caches/Codex', '~/Library/MessagesX',
                        '~/Library/Application Support/Claude', '~/Pictures', '~/Pictures/photos', '~/Music',
                        '~/.codex', '$TMPDIR/x'):
            with self.subTest(path=logical):
                self.assertFalse(c.privacy_skipped(logical))

    def test_results_are_cached_consistently(self):
        c = self.classifier
        for _ in range(2):
            self.assertTrue(c.privacy_skipped('~/Library/Messages/a/b'))
            self.assertFalse(c.privacy_skipped('~/Library/Caches/x'))
            self.assertTrue(c.privacy_skipped('~/Library/Messages'))


class MatchingSemanticsTests(unittest.TestCase):
    def test_double_star_in_the_middle_and_at_the_ends(self):
        c = classifier(rule(id='a.mid', patterns=('~/x/**/cache/*.bin',)),
                       rule(id='a.tail', patterns=('~/x/tail/**',), priority=50))
        self.assertEqual(c.classify('~/x/cache/f.bin', st()).rule_id, 'a.mid')          # zero segments
        self.assertEqual(c.classify('~/x/p/q/cache/f.bin', st()).rule_id, 'a.mid')
        self.assertEqual(c.classify('~/x/cache/f.txt', st()).rule_id, 'unknown')
        self.assertEqual(c.classify('~/x/cache/sub/f.bin', st()).rule_id, 'unknown')
        self.assertEqual(c.classify('~/x/tail/f', st()).rule_id, 'a.tail')
        self.assertEqual(c.classify('~/x/tail/a/b/c', st()).rule_id, 'a.tail')

    def test_single_star_stays_within_a_segment_and_prefix_wildcards_work(self):
        c = classifier(rule(id='a.star', patterns=('~/x/*/log-*.txt',)))
        self.assertEqual(c.classify('~/x/d/log-1.txt', st()).rule_id, 'a.star')
        self.assertEqual(c.classify('~/x/d/e/log-1.txt', st()).rule_id, 'unknown')
        self.assertEqual(c.classify('~/x/log-1.txt', st()).rule_id, 'unknown')

    def test_priority_decides_between_overlapping_rules(self):
        c = classifier(rule(id='a.low', priority=10), rule(id='a.high', priority=20))
        self.assertEqual(c.classify('~/x/y/z', st()).rule_id, 'a.high')

    def test_name_include_exclude_and_min_size(self):
        c = classifier(rule(id='a.r', name_include=('*.log', '*.txt'), name_exclude=('keep*',), min_size=100))
        self.assertEqual(c.classify('~/x/a.log', st(size=100)).rule_id, 'a.r')
        self.assertEqual(c.classify('~/x/a.LOG', st(size=100)).rule_id, 'a.r')
        self.assertEqual(c.classify('~/x/a.log', st(size=99)).rule_id, 'unknown')
        self.assertEqual(c.classify('~/x/keep.log', st(size=500)).rule_id, 'unknown')
        self.assertEqual(c.classify('~/x/a.bin', st(size=500)).rule_id, 'unknown')

    def test_file_rules_do_not_match_directories_and_bundle_rules_do_not_match_files(self):
        c = classifier(rule(id='a.file', patterns=('~/x/f/*',)),
                       rule(id='a.dir', patterns=('~/x/d/*',), unit=Unit.BUNDLE))
        self.assertEqual(c.classify('~/x/f/a', st()).rule_id, 'a.file')
        self.assertIsNone(c.classify_dir('~/x/f/a', st()))
        self.assertEqual(c.classify_dir('~/x/d/a', st()).rule_id, 'a.dir')
        self.assertEqual(c.classify('~/x/d/a', st()).rule_id, 'unknown')

    def test_group_depth_is_clamped_to_the_directory(self):
        c = classifier(rule(id='a.depth', patterns=('~/x/**',), group_depth=4))
        self.assertEqual(c.classify('~/x/a/b/c/d.txt', st()).group_key, 'a.depth:~/x/a/b')
        self.assertEqual(c.classify('~/x/a/b.txt', st()).group_key, 'a.depth:~/x/a')
        self.assertEqual(c.classify('~/x/b.txt', st()).group_key, 'a.depth:~/x')

    def test_bundle_group_depth(self):
        c = classifier(rule(id='a.bd', patterns=('~/x/*/*',), unit=Unit.BUNDLE, group_depth=3))
        self.assertEqual(c.classify_dir('~/x/p/q', st()).group_key, 'a.bd:~/x/p')
        c = classifier(rule(id='a.bd', patterns=('~/x/*',), unit=Unit.BUNDLE, group_depth=5))
        self.assertEqual(c.classify_dir('~/x/p', st()).group_key, 'a.bd:~/x/p')

    def test_detector_skip_override_and_group_key(self):
        from disk_steward.detectors import REGISTRY

        class Fake:
            name = 'fake'

            def prepare(self, ctx):
                return {}

            def refine(self, r, path, info, is_dir, facts):
                from disk_steward.model import Refinement
                if path.endswith('skip'):
                    return Refinement(skip=True)
                if path.endswith('protect'):
                    return Refinement(tier=Tier.PROTECTED, evidence=(Evidence('evidence.x', {}),))
                if path.endswith('regroup'):
                    return Refinement(group_key='a.fake:special')
                return None
        from unittest import mock
        with mock.patch.dict(REGISTRY, {'fake': Fake()}):
            c = classifier(rule(id='a.fake', detector='fake', priority=20),
                           rule(id='a.next', priority=10), facts={'fake': {}})
            self.assertEqual(c.classify('~/x/skip', st()).rule_id, 'a.next')
            m = c.classify('~/x/protect', st())
            self.assertEqual((m.rule_id, m.tier, m.group_key), ('a.fake', Tier.PROTECTED, 'a.fake' + PROTECTED_SUFFIX))
            self.assertEqual(m.evidence, (Evidence('evidence.x', {}),))
            self.assertEqual(c.classify('~/x/regroup', st()).group_key, 'a.fake:special')
            self.assertEqual(c.classify('~/x/plain', st()).group_key, 'a.fake')

    def test_unknown_detector_name_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'unknown detector'):
            classifier(rule(detector='nope'), facts={'nope': {}})


class ContextProtectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rules = list(protections.rules(None)) + [
            rule(id='t.junk', patterns=('~/x/**',), priority=300),
            rule(id='t.review', tier=Tier.REVIEW, patterns=('~/r/**',), priority=300),
            rule(id='t.deps', kind=Kind.PROJECT_DEPS, unit=Unit.BUNDLE, patterns=('~/x/**/node_modules',),
                 priority=300),
            rule(id='t.build', kind=Kind.CACHE, unit=Unit.BUNDLE, patterns=('~/x/**/build',), priority=300),
            rule(id='t.keep-bundle', tier=Tier.PROTECTED, unit=Unit.BUNDLE, patterns=('~/x/**/keepdir',),
                 actions=(), default_action=None, priority=300),
            rule(id='t.labelled', tier=Tier.PROTECTED, patterns=('~/x/keep/**',), actions=(), default_action=None,
                 priority=900),
            rule(id='t.labelled-junk', patterns=('~/x/hot/**',), priority=950),
        ]
        cat = build_catalog(rules, [ScanRoot('~/x', Domain.AGENT)], ())
        cls.c = Classifier(cat, {}, BASES, Settings(), sniff=False)

    def test_files_in_a_git_tree_are_all_protected_by_prot_git_tree(self):
        for name in ('main.py', 'auth.json', 'x.sqlite', 'notes.log'):
            with self.subTest(name=name):
                m = self.c.classify(f'~/x/repo/{name}', st(), in_git_tree=True)
                self.assertEqual((m.rule_id, m.tier, m.group_key, m.domain), ('prot.git-tree', Tier.PROTECTED,
                                                                              'prot.git-tree:agent', Domain.AGENT))
        self.assertEqual(self.c.classify('~/x/repo/main.py', st()).rule_id, 't.junk')   # outside a tree: normal
        m = self.c.classify('~/x/repo/main.py', st(), root_domain=Domain.USER_FILES, in_git_tree=True)
        self.assertEqual((m.domain, m.group_key), (Domain.USER_FILES, 'prot.git-tree:user-files'))

    def test_inside_a_git_tree_only_dependency_folders_and_protections_may_claim_a_directory(self):
        c = self.c
        d = st(mode=0o040755)
        self.assertEqual(c.classify_dir('~/x/repo/node_modules', d, in_git_tree=True).rule_id, 't.deps')
        self.assertEqual(c.classify_dir('~/x/repo/node_modules', d).rule_id, 't.deps')
        self.assertIsNone(c.classify_dir('~/x/repo/build', d, in_git_tree=True))
        self.assertEqual(c.classify_dir('~/x/repo/build', d).rule_id, 't.build')
        self.assertEqual(c.classify_dir('~/x/repo/keepdir', d, in_git_tree=True).rule_id, 't.keep-bundle')
        self.assertEqual(c.classify_dir('~/x/repo/.git', d, in_git_tree=True).rule_id, 'prot.git')
        self.assertEqual(c.classify('~/x/repo/build', d, is_dir=True, in_git_tree=True).rule_id, 'unknown')

    def test_database_neighbours_lose_generic_junk_and_review_matches(self):
        c = self.c
        for logical, rule_id in (('~/x/data/000003.log', 't.junk'), ('~/r/data/a.jsonl', 't.review')):
            with self.subTest(path=logical):
                self.assertEqual(c.classify(logical, st()).rule_id, rule_id)
                m = c.classify(logical, st(), db_dir=True)
                self.assertEqual((m.rule_id, m.tier, m.group_key), ('prot.databases', Tier.PROTECTED,
                                                                    'prot.databases:agent'))
        m = c.classify('~/x/data/000003.log', st(), root_domain=Domain.APP_LIBRARY, db_dir=True)
        self.assertEqual((m.domain, m.group_key), (Domain.APP_LIBRARY, 'prot.databases:app-library'))

    def test_database_neighbours_keep_labelled_and_protected_verdicts(self):
        c = self.c
        self.assertEqual(c.classify('~/x/keep/a', st(), db_dir=True).rule_id, 't.labelled')       # priority 900
        self.assertEqual(c.classify('~/x/hot/a', st(), db_dir=True).rule_id, 't.labelled-junk')   # priority 950
        self.assertEqual(c.classify('~/x/d/auth.json', st(), db_dir=True).group_key, 'prot.credentials:agent')
        self.assertEqual(c.classify('~/elsewhere/a.bin', st(), db_dir=True).rule_id, 'unknown')

    def test_a_git_tree_outranks_a_database_directory(self):
        m = self.c.classify('~/x/repo/a.log', st(), in_git_tree=True, db_dir=True)
        self.assertEqual(m.rule_id, 'prot.git-tree')

    def test_flags_are_ignored_by_a_catalog_without_the_protections(self):
        c = classifier(rule(id='a.junk', patterns=('~/x/**',)))
        self.assertEqual(c.classify('~/x/a', st(), in_git_tree=True, db_dir=True).rule_id, 'a.junk')

    def test_database_markers(self):
        c = self.c
        for names in (['x.sqlite'], ['a.sqlite3'], ['a.sqlite-wal'], ['Cache.db'], ['a.db-wal'], ['x.ldb'],
                      ['a.realm'], ['state.vscdb'], ['a.duckdb'], ['CURRENT', 'MANIFEST-000001'],
                      ['manifest-000001', 'current'], ['readme', 'DATA.DB']):
            with self.subTest(names=names):
                self.assertTrue(c.has_database_markers(names))
        for names in ([], ['CURRENT'], ['MANIFEST-000001'], ['LOG', 'LOCK'], ['notes.txt', 'a.json', 'history'],
                      ['Cookies']):
            with self.subTest(names=names):
                self.assertFalse(c.has_database_markers(names))

    def test_file_context_from_the_disk(self):
        with FakeHome() as fh:
            fh.file('~/x/repo/.git', content='gitdir: /elsewhere')
            fh.file('~/x/repo/sub/deep/a.py', size=1)
            fh.file('~/x/plain/a.py', size=1)
            fh.file('~/x/db/a.log', size=1)
            fh.file('~/x/db/state.sqlite', size=1)
            c = Classifier(build_catalog(list(protections.rules(None)), [], ()), {}, fh.bases, Settings(),
                           sniff=False)
            self.assertEqual(c.file_context('~/x/repo/sub/deep/a.py'), (True, False))
            self.assertEqual(c.file_context('~/x/plain/a.py'), (False, False))
            self.assertEqual(c.file_context('~/x/db/a.log'), (False, True))
            fh.file('~/.git', content='gitdir: /elsewhere')   # a .git above the scanned roots counts too
            self.assertEqual(c.file_context('~/x/plain/a.py'), (True, False))


class SqliteHeaderTests(unittest.TestCase):
    def setUp(self):
        self.fh = FakeHome().__enter__()
        self.addCleanup(self.fh.cleanup)
        cat = build_catalog([rule(id='t.review', tier=Tier.REVIEW, patterns=('~/r/**',), group_depth=3),
                             rule(id='t.junk', patterns=('~/j/**',))], [], ())
        self.c = Classifier(cat, {}, self.fh.bases, Settings())

    def classify(self, logical, **kw):
        info = stat_nofollow(self.fh.path(logical)) if self.fh.path(logical).is_symlink() or self.fh.path(logical).exists() \
            else st(size=100)
        return self.c.classify(logical, info, **kw)

    def test_a_sqlite_file_with_any_name_is_protected(self):
        self.fh.file('~/r/d/sessions.jsonl', content=b'SQLite format 3\0' + b'\0' * 100)
        m = self.classify('~/r/d/sessions.jsonl')
        self.assertEqual((m.rule_id, m.tier, m.group_key), ('t.review', Tier.PROTECTED, 't.review#protected:~/r/d'))
        self.assertEqual(m.evidence, (Evidence('evidence.sqlite_header', {}),))

    def test_ordinary_review_files_stay_review(self):
        self.fh.file('~/r/d/a.jsonl', content=b'{"type":"x"}\n')
        self.fh.file('~/r/d/short', content=b'SQLite')
        self.fh.file('~/r/d/empty', content=b'')
        for name in ('a.jsonl', 'short', 'empty'):
            m = self.classify(f'~/r/d/{name}')
            self.assertEqual((m.tier, m.evidence), (Tier.REVIEW, ()), name)

    def test_junk_files_are_not_read(self):
        self.fh.file('~/j/a', content=b'SQLite format 3\0' + b'\0' * 100)
        self.assertEqual(self.classify('~/j/a').tier, Tier.JUNK)

    def test_an_unreadable_header_protects(self):
        path = self.fh.file('~/r/d/locked', content=b'plain')
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o644)
        m = self.classify('~/r/d/locked')
        self.assertEqual((m.tier, m.evidence), (Tier.PROTECTED, (Evidence('evidence.header_unreadable',
                                                                          {'reason': 'EACCES'}),)))
        gone = self.c.classify('~/r/d/vanished', st(size=5))
        self.assertEqual((gone.tier, gone.evidence[0].args), (Tier.PROTECTED, {'reason': 'ENOENT'}))

    def test_a_symlink_is_not_followed_by_the_read(self):
        self.fh.file('~/outside/db', content=b'SQLite format 3\0' + b'\0' * 100)
        self.fh.symlink('~/r/d/link', '~/outside/db')
        m = self.c.classify('~/r/d/link', st(size=5))   # as if it had been stat-ed as a regular file
        self.assertEqual((m.tier, m.evidence[0].key), (Tier.PROTECTED, 'evidence.header_unreadable'))

    def test_non_regular_files_are_not_opened(self):
        m = self.c.classify('~/r/d/fifo', st(mode=0o010644))
        self.assertEqual((m.tier, m.evidence), (Tier.REVIEW, ()))

    def test_dataless_files_are_protected_without_being_read(self):
        m = self.c.classify('~/r/d/cloud', st(size=5, flags=SF_DATALESS))
        self.assertEqual((m.tier, m.evidence), (Tier.PROTECTED, (Evidence('evidence.header_unreadable',
                                                                          {'reason': 'dataless'}),)))

    def test_sniffing_can_be_turned_off(self):
        c = Classifier(build_catalog([rule(id='t.review', tier=Tier.REVIEW, patterns=('~/r/**',))], [], ()), {},
                       self.fh.bases, Settings(), sniff=False)
        self.assertEqual(c.classify('~/r/missing', st(size=5)).tier, Tier.REVIEW)

    def test_a_protected_detector_verdict_is_not_read_again(self):
        # the match is already protected, so no read (the path does not even exist)
        from unittest import mock
        from disk_steward.detectors import REGISTRY
        from disk_steward.model import Refinement

        class Keep:
            name = 'keep'

            def prepare(self, ctx):
                return {}

            def refine(self, r, path, info, is_dir, facts):
                return Refinement(tier=Tier.PROTECTED)
        with mock.patch.dict(REGISTRY, {'keep': Keep()}):
            c = Classifier(build_catalog([rule(id='t.review', tier=Tier.REVIEW, patterns=('~/r/**',),
                                               detector='keep')], [], ()), {'keep': {}}, self.fh.bases, Settings())
            m = c.classify('~/r/missing', st(size=5))
        self.assertEqual((m.tier, m.evidence), (Tier.PROTECTED, ()))


class PerformanceTests(unittest.TestCase):
    def test_classifying_200k_synthetic_paths_is_fast(self):
        with FakeHome() as fh:
            settings = Settings()
            catalog = load_catalog(settings)
            facts, _ = compute_facts(DetectorContext(fh.bases, FakeMacOS(), settings), catalog)
            cl = Classifier(catalog, facts, fh.bases, settings, sniff=False)
        dirs = ['~/.codex/sessions/2026/10/02', '~/.codex/sessions/2026/09/11', '~/.codex/skills/a/b',
                '~/.codex/log', '~/.codex/cache', '~/.claude/projects/-Users-me-p', '~/.claude/projects/-Users-me-p/a/subagents',
                '~/.claude/file-history/s1', '~/.claude/debug', '~/Library/Application Support/Claude/Cache',
                '~/Library/Application Support/Claude/Code Cache/js', '~/Library/Application Support/Claude/IndexedDB/x',
                '~/Library/Application Support/Claude/vm_bundles/b1', '~/Library/Caches/com.anthropic.claudefordesktop',
                '~/.cursor/extensions/pub.x-1/dist', '~/.ollama/blobs', '~/.gemini/antigravity/brain',
                '~/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/lib', '~/Library/Logs/Claude',
                '~/Documents/some/project/src']
        names = ['rollout-2026-10-02T11-04-41-aaaaaaaa-0000-7000-8000-000000000001.jsonl', 'x.jsonl', 'data_0',
                 'file.bin', 'notes.txt', 'app.log', 'auth.json', 'index.sqlite', 'image.png', 'SKILL.md']
        info = st(size=1000)
        paths = [f'{dirs[i % 20]}/{names[(i // 20) % 10]}{i}' if i % 7 else f'{dirs[i % 20]}/{names[(i // 20) % 10]}'
                 for i in range(200_000)]
        start = time.perf_counter()
        classify = cl.classify
        results = [classify(p, info) for p in paths]
        elapsed = time.perf_counter() - start
        sys.stderr.write(f'\n[benchmark] classify 200,000 synthetic paths: {elapsed:.2f}s\n')
        self.assertLess(elapsed, 6.0)
        self.assertEqual(len(results), 200_000)
        self.assertGreater(len({r.rule_id for r in results}), 8)   # the sample really exercised many rules


if __name__ == '__main__':
    unittest.main()
