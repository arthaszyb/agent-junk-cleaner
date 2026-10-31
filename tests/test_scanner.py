from __future__ import annotations

import dataclasses
import errno
import os
import unittest
from unittest import mock

import m2_fixture as fx
from disk_steward import i18n, scanner
from disk_steward.catalog import ScanRoot, build_catalog, load_catalog, protections
from disk_steward.classify import Classifier, PROTECTED_SUFFIX
from disk_steward.detectors import DetectorContext, compute_facts
from disk_steward.fsutil import SF_DATALESS
from disk_steward.macos import FakeMacOS
from disk_steward.model import (Action, AgeBasis, Domain, ErrorCode, Evidence, Kind, Rule, StatInfo, StewardError,
                                Tier, Unit)
from disk_steward.paths import item_key
from disk_steward.settings import Settings, settings_hash
from disk_steward.store import Store

from support import Clock, FakeHome

MIB = 1024 * 1024


def run(fh, store, mac=None, cat=None, settings=None, domains=('agent',), **kw):
    settings = settings or Settings(domains=('agent',))
    cat = cat or load_catalog(settings)
    mac = mac or FakeMacOS()
    facts, _ = compute_facts(DetectorContext(fh.bases, mac, settings), cat)
    cl = Classifier(cat, facts, fh.bases, settings)
    return scanner.scan(store, cl, cat, fh.bases, mac, settings, domains, **kw)


def custom_catalog(rules, roots, privacy=()):
    return build_catalog(rules, roots, privacy)


def rule(**kw):
    base = dict(id='t.rule', provider='codex', domain=Domain.AGENT, tier=Tier.JUNK, kind=Kind.CACHE,
                patterns=('~/x/**',), actions=(Action.DELETE,), default_action=Action.DELETE)
    base.update(kw)
    return Rule(**base)


class ScanCase(unittest.TestCase):
    def setUp(self):
        self.fh = FakeHome(Clock()).__enter__()
        self.addCleanup(self.fh.cleanup)
        self.store = Store(self.fh.data_dir / 'steward.db')
        self.addCleanup(self.store.close)

    def items(self, scan_id, group=None):
        return {i.path: i for i in self.store.items(scan_id, group_key=group)}


class EndToEndTests(ScanCase):
    @classmethod
    def setUpClass(cls):
        cls.fh = FakeHome(Clock()).__enter__()
        fx.build(cls.fh)
        cls.store = Store(cls.fh.data_dir / 'steward.db')
        cls.mac = FakeMacOS(usage=(500 * 10 ** 9, 123 * 10 ** 9), free=77 * 10 ** 9)
        cls.settings = Settings(domains=('agent',))
        cls.catalog = load_catalog(cls.settings)
        cls.sid = run(cls.fh, cls.store, cls.mac, cls.catalog, cls.settings)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.fh.cleanup()

    def setUp(self):
        pass

    def test_scan_row(self):
        info = self.store.scan(self.sid)
        facts, digest = compute_facts(DetectorContext(self.fh.bases, FakeMacOS(), self.settings), self.catalog)
        self.assertEqual(info['status'], 'done')
        self.assertEqual(info['domains'], ['agent'])
        self.assertEqual(info['catalog_hash'], self.catalog.hash)
        self.assertEqual(info['facts_hash'], digest)
        self.assertEqual(info['settings_hash'], settings_hash(self.settings))
        self.assertEqual((info['vol_total'], info['vol_free']), (500 * 10 ** 9, 77 * 10 ** 9))
        self.assertEqual(info['attributed'], sum(i.alloc for i in self.store.iter_items(self.sid)))
        self.assertGreater(info['attributed'], 0)
        self.assertIn(('volume_usage', (str(self.fh.home),)), self.mac.calls)
        self.assertIn(('statfs_free', (str(self.fh.home),)), self.mac.calls)

    def test_roots(self):
        roots = {r['path']: r for r in self.store.scan_roots(self.sid)}
        self.assertEqual(roots['~/.codex']['status'], 'ok')
        self.assertGreater(roots['~/.codex']['files'], 10)
        self.assertGreater(roots['~/.codex']['alloc'], 0)
        self.assertEqual(roots['~/.windsurf']['status'], 'missing')
        self.assertEqual(roots['~/.windsurf']['files'], 0)
        self.assertEqual(roots['~/.cursor']['status'], 'ok')
        self.assertEqual(len(roots), len([r for r in self.catalog.roots if r.domain == Domain.AGENT]))
        self.assertEqual(self.store.scan_errors(self.sid), [])

    def test_file_item(self):
        item = self.store.item(self.sid, item_key(fx.PAIR_B))
        st = os.lstat(self.fh.path(fx.PAIR_B))
        self.assertEqual((item.rule_id, item.tier, item.unit, item.group_key),
                         ('codex.sessions', Tier.REVIEW, Unit.FILE, 'codex.sessions:duplicates'))
        self.assertEqual((item.size, item.alloc, item.reclaim, item.files), (st.st_size, st.st_blocks * 512,
                                                                          st.st_blocks * 512, 1))
        self.assertEqual((item.dev, item.ino, item.nlink, item.uid), (st.st_dev, st.st_ino, 1, os.getuid()))
        self.assertEqual(item.mtime_ns, st.st_mtime_ns)
        self.assertEqual(item.newest_ns, StatInfo.from_stat(st).changed_ns)
        self.assertEqual([e.key for e in item.evidence], ['evidence.codex_legacy_copy', 'evidence.codex_both_indexed'])
        self.assertIsNone(item.last_used_ns)

    def test_bundle_item(self):
        p = '~/.cache/codex-runtimes/codex-runtime-install-abc123'
        item = self.store.item(self.sid, item_key(p))
        self.assertEqual((item.rule_id, item.tier, item.unit, item.files, item.size, item.taint),
                         ('codex.runtime-install', Tier.JUNK, Unit.BUNDLE, 1, 100, ''))
        st = os.lstat(self.fh.path(p))
        self.assertEqual((item.dev, item.ino), (st.st_dev, st.st_ino))
        self.assertGreaterEqual(item.newest_ns, StatInfo.from_stat(st).changed_ns)
        self.assertEqual(item.fingerprint(), dict(dev=st.st_dev, ino=st.st_ino, files=1, alloc=item.alloc,
                                                  newest_ns=item.newest_ns))

    def test_duplicates_group_holds_only_the_legacy_id_copy(self):
        g = self.store.group(self.sid, 'codex.sessions:duplicates')
        legacy = os.lstat(self.fh.path(fx.PAIR_B)).st_blocks * 512
        self.assertEqual((g.rule_id, g.tier, g.items, g.alloc), ('codex.sessions', Tier.REVIEW, 1, legacy))
        self.assertEqual(list(self.items(self.sid, 'codex.sessions:duplicates')), [fx.PAIR_B])
        self.assertEqual(g.label_args, {'path': 'duplicates'})
        self.assertEqual((g.actions, g.default_action), ((Action.ARCHIVE, Action.DELETE), Action.ARCHIVE))
        self.assertEqual(g.provider, 'codex')
        self.assertEqual([e.key for e in g.evidence], ['evidence.codex_legacy_copy', 'evidence.codex_both_indexed'])
        month = self.items(self.sid, 'codex.sessions:~/.codex/sessions/2026/10')
        self.assertEqual(set(month), {fx.PAIR_A, fx.SOLO})
        self.assertEqual([e.key for e in month[fx.PAIR_A].evidence], ['evidence.codex_duplicate_kept'])

    def test_group_fields_are_copied_from_the_rule(self):
        g = self.store.group(self.sid, 'codex.logs')
        r = self.catalog.rule('codex.logs')
        self.assertEqual((g.min_age_floor_days, g.default_min_age_days, g.age_basis, g.hint, g.kind, g.unit, g.domain),
                         (1, None, AgeBasis.CHANGED, None, Kind.LOGS, Unit.FILE, Domain.AGENT))
        self.assertEqual((g.actions, g.default_action, g.label_args), (r.actions, Action.DELETE, {}))
        ollama = self.store.group(self.sid, 'ollama.models')
        self.assertEqual((ollama.actions, ollama.default_action, ollama.hint, ollama.tier),
                         ((), None, 'ollama list  # then: ollama rm <model>', Tier.REVIEW))
        projects = self.store.group(self.sid, 'claude.projects:~/.claude/projects/-Users-me-proj')
        self.assertEqual(projects.label_args, {'path': '~/.claude/projects/-Users-me-proj'})
        self.assertEqual(projects.items, 3)

    def test_protected_variants_live_in_their_own_groups(self):
        junk = self.store.group(self.sid, 'claude.cli-versions')
        kept = self.store.group(self.sid, 'claude.cli-versions' + PROTECTED_SUFFIX)
        self.assertEqual((junk.tier, junk.items, kept.tier, kept.items), (Tier.JUNK, 2, Tier.PROTECTED, 1))
        self.assertEqual((kept.actions, kept.default_action), ((), None))
        self.assertEqual(kept.rule_id, 'claude.cli-versions')
        engines = self.store.group(self.sid, 'claude.desktop-engines' + PROTECTED_SUFFIX)
        self.assertEqual((engines.tier, engines.items), (Tier.PROTECTED, 3))

    def test_unknown_group(self):
        g = self.store.group(self.sid, 'unknown:~/.codex/log')
        self.assertEqual((g.rule_id, g.tier, g.kind, g.provider, g.actions, g.default_action, g.hint),
                         ('unknown', Tier.PROTECTED, Kind.UNKNOWN, 'system', (), None, None))
        self.assertEqual(g.label_args, {'path': '~/.codex/log'})

    def test_every_group_is_the_sum_of_its_items(self):
        groups = {g.key: g for g in self.store.groups(self.sid)}
        by_group = {}
        for item in self.store.iter_items(self.sid):
            by_group.setdefault(item.group_key, []).append(item)
        self.assertEqual(set(groups), set(by_group))
        for key, items in by_group.items():
            g = groups[key]
            with self.subTest(group=key):
                self.assertEqual((g.items, g.alloc, g.reclaim), (len(items), sum(i.alloc for i in items),
                                                                  sum(i.reclaim for i in items)))
                self.assertEqual((g.newest_ns, g.oldest_ns), (max(i.newest_ns for i in items),
                                                              min(i.newest_ns for i in items)))
                self.assertEqual({i.tier for i in items}, {g.tier})
                self.assertLessEqual(len(g.evidence), 5)

    def test_agent_workspaces_artifact_runtime_and_plugin_settings_are_not_selectable(self):
        for key, files in (('claude.desktop-workspaces', 11), ('codex.artifact-runtime', 3),
                           ('codex.plugin-exclusions', 1)):
            g = self.store.group(self.sid, key)
            self.assertEqual((g.tier, g.actions, g.default_action), (Tier.PROTECTED, (), None), key)
            items = self.store.items(self.sid, group_key=key)
            self.assertEqual({i.tier for i in items}, {Tier.PROTECTED}, key)
            self.assertEqual(sum(i.files for i in items), files, key)
        # nothing under those areas is offered, and a node_modules inside a workspace is just part of it
        for item in self.store.iter_items(self.sid):
            if item.path.startswith((fx.WORKSPACE, f'{fx.CODEX_SUPPORT}/artifact-sessions')):
                self.assertEqual(item.tier, Tier.PROTECTED, item.path)
        self.assertEqual([i for i in self.store.iter_items(self.sid)
                          if 'node_modules' in i.path and i.tier != Tier.PROTECTED], [])

    def test_group_history_is_recorded_on_finish(self):
        self.assertEqual(len(self.store.group_history('codex.logs')), 1)

    def test_texts_exist_for_every_group_and_evidence(self):
        for g in self.store.groups(self.sid):
            for loc in i18n.LOCALES:
                label = i18n.t(f'rule.{g.rule_id}.label', loc, **g.label_args)
                self.assertNotEqual(label, f'rule.{g.rule_id}.label')
                self.assertNotIn('{', label)
                self.assertTrue(i18n.t(f'rule.{g.rule_id}.effect', loc))
                for ev in g.evidence:
                    self.assertNotEqual(i18n.t(ev.key, loc, **ev.args), ev.key)
        for item in self.store.iter_items(self.sid):
            for ev in item.evidence:
                i18n.t(ev.key, 'en', **ev.args)

    def test_unclassified_files_are_visible_not_hidden(self):
        att = self.store.scan(self.sid)['attributed']
        total = sum(g.alloc for g in self.store.groups(self.sid))
        self.assertEqual(att, total)

    def test_repeating_the_scan_gives_identical_rows(self):
        # prune=False: the other tests of this class read the first scan's rows
        again = run(self.fh, self.store, self.mac, self.catalog, self.settings, prune=False)
        strip = lambda i: dataclasses.replace(i, scan_id=0)
        self.assertEqual([strip(i) for i in self.store.iter_items(self.sid)],
                         [strip(i) for i in self.store.iter_items(again)])
        self.assertEqual([dataclasses.replace(g, scan_id=0) for g in self.store.groups(self.sid)],
                         [dataclasses.replace(g, scan_id=0) for g in self.store.groups(again)])

    def test_no_last_used_query_in_m2(self):
        self.assertFalse([c for c in self.mac.calls if c[0] == 'last_used'])


class RollupTests(ScanCase):
    def test_small_protected_files_roll_up_and_large_ones_do_not(self):
        fh = self.fh
        fh.file('~/.codex/a.sqlite', size=100, age_days=5)
        fh.file('~/.codex/b.sqlite', size=200, age_days=2)
        fh.file('~/.codex/big.sqlite', size=60 * MIB)
        fh.file('~/.codex/just-under.sqlite', size=50 * MIB - 1)
        fh.file('~/.codex/exactly.sqlite', size=50 * MIB)
        fh.file('~/.codex/skills/x/1.md', size=10, age_days=9)
        fh.file('~/.codex/skills/y/2.md', size=20, age_days=1)
        sid = run(fh, self.store)
        db = self.items(sid, 'prot.databases:agent')
        individual = {p for p, i in db.items() if i.unit == Unit.FILE}
        self.assertEqual(individual, {'~/.codex/big.sqlite', '~/.codex/exactly.sqlite'})
        big = db['~/.codex/big.sqlite']
        self.assertEqual((big.size, big.tier, big.files), (60 * MIB, Tier.PROTECTED, 1))
        (roll,) = [i for i in db.values() if i.unit == Unit.BUNDLE]
        self.assertEqual((roll.path, roll.files, roll.size, roll.tier), ('~/.codex', 3, 100 + 200 + 50 * MIB - 1,
                                                                         Tier.PROTECTED))
        self.assertEqual((roll.dev, roll.ino), (0, 0))
        self.assertEqual(roll.key, item_key('rollup:prot.databases:agent'))
        newest = max(os.lstat(fh.path(p)).st_mtime_ns for p in ('~/.codex/a.sqlite', '~/.codex/b.sqlite'))
        self.assertGreaterEqual(roll.newest_ns, newest)
        skills = self.items(sid, 'codex.user-content:~/.codex/skills')
        self.assertEqual(list(skills), ['~/.codex/skills'])
        item = skills['~/.codex/skills']
        self.assertEqual((item.files, item.size, item.unit, item.key),
                         (2, 30, Unit.BUNDLE, item_key('rollup:codex.user-content:~/.codex/skills')))
        g = self.store.group(sid, 'prot.databases:agent')
        self.assertEqual((g.items, g.domain), (3, Domain.AGENT))   # rows, not files

    def test_rollup_path_is_the_longest_common_directory(self):
        fh = self.fh
        fh.file('~/.codex/skills/a/b/1.md', size=1)
        fh.file('~/.codex/skills/a/c/2.md', size=1)
        fh.file('~/.codex/skills/a/c/d/3.md', size=1)
        sid = run(fh, self.store)
        self.assertEqual(list(self.items(sid, 'codex.user-content:~/.codex/skills')), ['~/.codex/skills/a'])

    def test_unknown_files_roll_up_per_group(self):
        fh = self.fh
        fh.file('~/.codex/log/a.bin', size=10)
        fh.file('~/.codex/log/sub/b.bin', size=10)
        fh.file('~/.codex/ipc/c.sock', size=10)
        sid = run(fh, self.store)
        log = self.items(sid, 'unknown:~/.codex/log')
        self.assertEqual({(p, i.files) for p, i in log.items()}, {('~/.codex/log', 2)})
        self.assertEqual(self.items(sid, 'unknown:~/.codex/ipc')['~/.codex/ipc'].files, 1)

    def test_junk_and_review_files_are_never_rolled_up(self):
        fh = self.fh
        for n in range(5):
            fh.file(f'~/.codex/cache/f{n}.json', size=10)
        sid = run(fh, self.store)
        items = self.items(sid, 'codex.cache')
        self.assertEqual(len(items), 5)
        self.assertTrue(all(i.unit == Unit.FILE for i in items.values()))

    def test_protected_evidence_survives_the_rollup(self):
        fh = self.fh
        for v in ('2.1.1', '2.1.2'):
            fh.file(f'~/.local/share/claude/versions/{v}', size=10)
        sid = run(fh, self.store)   # no symlink: every version is protected as unverifiable
        (roll,) = self.items(sid, 'claude.cli-versions' + PROTECTED_SUFFIX).values()
        self.assertEqual((roll.files, roll.path), (2, '~/.local/share/claude/versions'))
        self.assertEqual(roll.evidence, (Evidence('evidence.version_unverifiable', {'reason': 'missing'}),))


def deps_rules():
    """Real protections plus a dependency-folder rule (PROJECT_DEPS arrives with a later milestone) and a
    junk rule that would match every file below ~/x."""
    return list(protections.rules(None)) + [
        rule(id='t.junk', patterns=('~/x/**',), priority=300),
        rule(id='t.deps', kind=Kind.PROJECT_DEPS, unit=Unit.BUNDLE, patterns=('~/x/**/node_modules',), priority=300)]


class ContextProtectionScanTests(ScanCase):
    def scan_x(self, rules=None):
        cat = custom_catalog(rules or deps_rules(), [ScanRoot('~/x', Domain.AGENT)])
        return run(self.fh, self.store, cat=cat)

    def test_a_git_work_tree_is_protected_but_its_dependency_folder_is_still_found(self):
        fh = self.fh
        fh.file('~/x/repo/main.py', size=100, age_days=90)
        fh.file('~/x/repo/.git', content='gitdir: /somewhere/else')   # a worktree: .git is a file
        fh.file('~/x/repo/big.bin', size=60 * MIB, age_days=90)
        fh.file('~/x/repo/src/deep/util.py', size=10, age_days=90)
        fh.file('~/x/repo/node_modules/pkg/index.js', size=10, age_days=90)
        fh.file('~/x/repo/src/node_modules/inner/a.js', size=10, age_days=90)
        fh.file('~/x/loose.py', size=10, age_days=90)   # outside the work tree: the junk rule applies
        sid = self.scan_x()
        items = self.items(sid)
        self.assertEqual(items['~/x/loose.py'].rule_id, 't.junk')
        self.assertEqual(items['~/x/loose.py'].tier, Tier.JUNK)
        self.assertEqual(items['~/x/repo/big.bin'].tier, Tier.PROTECTED)          # individual: it is large
        self.assertEqual((items['~/x/repo/big.bin'].rule_id, items['~/x/repo/big.bin'].size),
                         ('prot.git-tree', 60 * MIB))
        for deps in ('~/x/repo/node_modules', '~/x/repo/src/node_modules'):
            self.assertEqual((items[deps].rule_id, items[deps].tier, items[deps].unit),
                             ('t.deps', Tier.JUNK, Unit.BUNDLE), deps)
        tree = self.items(sid, 'prot.git-tree:agent')
        small = [i for i in tree.values() if i.unit == Unit.BUNDLE]
        self.assertEqual(len(small), 1)
        self.assertEqual((small[0].path, small[0].files, small[0].tier), ('~/x/repo', 3, Tier.PROTECTED))
        self.assertEqual([i.path for i in items.values() if i.path.endswith(('main.py', 'util.py'))], [])   # rolled up
        self.assertEqual({i.tier for i in tree.values()}, {Tier.PROTECTED})

    def test_a_git_directory_is_a_protected_bundle_and_marks_its_parent_a_work_tree(self):
        fh = self.fh
        fh.file('~/x/repo/.git/HEAD', size=10)
        fh.file('~/x/repo/main.py', size=10)
        sid = self.scan_x()
        items = self.items(sid)
        self.assertEqual((items['~/x/repo/.git'].rule_id, items['~/x/repo/.git'].tier), ('prot.git', Tier.PROTECTED))
        self.assertEqual(items['~/x/repo'].rule_id, 'prot.git-tree')

    def test_the_work_tree_flag_is_inherited_below_but_not_above(self):
        fh = self.fh
        fh.file('~/x/above/a.py', size=10)
        fh.file('~/x/above/repo/.git', content='gitdir: /elsewhere')
        fh.file('~/x/above/repo/a/b/c/d.py', size=10)
        sid = self.scan_x()
        items = self.items(sid)
        self.assertEqual(items['~/x/above/a.py'].rule_id, 't.junk')
        self.assertEqual(items['~/x/above/repo'].rule_id, 'prot.git-tree')
        self.assertEqual(items['~/x/above/repo'].files, 2)   # .git and d.py

    def test_a_leveldb_directory_protects_its_files(self):
        fh = self.fh
        fh.file('~/x/ldb/000003.log', size=10, age_days=90)
        fh.file('~/x/ldb/CURRENT', content='MANIFEST-000001\n')
        fh.file('~/x/ldb/MANIFEST-000001', size=10)
        fh.file('~/x/ldb/sub/other.log', size=10, age_days=90)   # a subdirectory is not a sibling
        fh.file('~/x/plain/000003.log', size=10, age_days=90)
        sid = self.scan_x()
        items = self.items(sid)
        self.assertEqual(items['~/x/plain/000003.log'].rule_id, 't.junk')
        self.assertEqual(items['~/x/ldb/sub/other.log'].rule_id, 't.junk')
        (db,) = self.items(sid, 'prot.databases:agent').values()
        self.assertEqual((db.path, db.files, db.tier), ('~/x/ldb', 3, Tier.PROTECTED))

    def test_sqlite_neighbours_are_protected_but_current_alone_is_not_a_marker(self):
        fh = self.fh
        fh.file('~/x/app/state.sqlite', size=10)
        fh.file('~/x/app/state.log', size=10, age_days=90)
        fh.file('~/x/solo/CURRENT', size=10)
        fh.file('~/x/solo/a.log', size=10, age_days=90)
        sid = self.scan_x()
        items = self.items(sid)
        self.assertNotIn('~/x/app/state.log', items)
        self.assertEqual(items['~/x/solo/a.log'].tier, Tier.JUNK)
        (db,) = self.items(sid, 'prot.databases:agent').values()   # state.sqlite, state.log and solo/CURRENT
        self.assertEqual((db.path, db.files), ('~/x', 3))

    def test_labelled_rules_beside_a_database_keep_their_verdict(self):
        # ~/.codex holds sqlite files; the state-tmp leftovers next to them are still offered
        fh = self.fh
        fh.file('~/.codex/.codex-global-state.json', size=10)
        fh.file('~/.codex/..codex-global-state.json.tmp-1-a', size=10, age_days=40)
        fh.file('~/.codex/logs_2.sqlite', size=10)
        sid = run(fh, self.store)
        self.assertEqual(self.items(sid)['~/.codex/..codex-global-state.json.tmp-1-a'].tier, Tier.JUNK)


class ProtectionDomainTests(ScanCase):
    def test_protections_are_attributed_to_the_domain_of_their_root(self):
        fh = self.fh
        fh.file('~/x/data.sqlite', size=100)
        fh.file('~/y/data.sqlite', size=100)
        fh.file('~/x/auth.json', size=10)
        fh.file('~/y/auth.json', size=10)
        fh.file('~/y/proj/.git/HEAD', size=10)
        cat = custom_catalog(list(protections.rules(None)),
                             [ScanRoot('~/x', Domain.AGENT), ScanRoot('~/y', Domain.APP_LIBRARY)])
        sid = run(fh, self.store, cat=cat, domains=('agent', 'app-library'))
        groups = {g.key: g for g in self.store.groups(sid)}
        self.assertEqual(set(groups), {'prot.databases:agent', 'prot.databases:app-library',
                                       'prot.credentials:agent', 'prot.credentials:app-library',
                                       'prot.git:app-library'})
        for key, domain in (('prot.databases:agent', Domain.AGENT), ('prot.databases:app-library', Domain.APP_LIBRARY),
                            ('prot.credentials:agent', Domain.AGENT), ('prot.git:app-library', Domain.APP_LIBRARY)):
            with self.subTest(group=key):
                g = groups[key]
                self.assertEqual((g.domain, g.tier, g.rule_id), (domain, Tier.PROTECTED, key.split(':')[0]))
                self.assertTrue(all(i.domain == domain for i in self.store.items(sid, group_key=key)))
        self.assertEqual(self.items(sid, 'prot.databases:app-library').keys(), {'~/y'})
        self.assertEqual(self.items(sid, 'prot.databases:agent').keys(), {'~/x'})


class WalkRuleTests(ScanCase):
    def test_hardlinks_are_counted_once(self):
        fh = self.fh
        fh.file('~/.claude/file-history/h/a', size=8192)
        os.link(fh.path('~/.claude/file-history/h/a'), fh.path('~/.claude/file-history/h/b'))
        fh.file('~/.claude/file-history/h/c', size=4096)
        sid = run(fh, self.store)
        items = self.items(sid, 'claude.file-history')
        self.assertEqual(sorted(items), ['~/.claude/file-history/h/a', '~/.claude/file-history/h/c'])
        a = items['~/.claude/file-history/h/a']
        self.assertEqual((a.nlink, a.reclaim), (2, 0))
        self.assertEqual(a.alloc, os.lstat(fh.path('~/.claude/file-history/h/a')).st_blocks * 512)
        roots = {r['path']: r for r in self.store.scan_roots(sid)}
        self.assertEqual(roots['~/.claude']['files'], 2)

    def test_symlinks_are_skipped_not_followed(self):
        fh = self.fh
        fh.file('~/outside/secret/huge.log', size=40_000)
        fh.file('~/.claude/debug/real.txt', size=100, age_days=2)
        fh.symlink('~/.claude/debug/linked-dir', '~/outside/secret')
        fh.symlink('~/.claude/debug/linked-file', '~/outside/secret/huge.log')
        fh.symlink('~/.claude/debug/dangling', '~/outside/none')
        sid = run(fh, self.store)
        paths = [i.path for i in self.store.iter_items(sid)]
        self.assertEqual([p for p in paths if 'linked' in p or 'dangling' in p or 'outside' in p], [])
        self.assertEqual(self.items(sid, 'claude.logs').keys(), {'~/.claude/debug/real.txt'})
        self.assertEqual(self.store.scan_errors(sid), [])
        self.assertEqual({r['path']: r['files'] for r in self.store.scan_roots(sid)}['~/.claude'], 1)

    def test_a_symlinked_root_is_not_followed(self):
        fh = self.fh
        fh.file('~/elsewhere/file', size=10)
        fh.symlink('~/.cursor', '~/elsewhere')
        sid = run(fh, self.store)
        roots = {r['path']: r for r in self.store.scan_roots(sid)}
        self.assertEqual(roots['~/.cursor']['status'], 'error')
        self.assertIn(('~/.cursor', 'root_symlink'), [(e['path'], e['error']) for e in self.store.scan_errors(sid)])
        self.assertEqual([i.path for i in self.store.iter_items(sid)], [])

    def test_never_crosses_into_another_device(self):
        fh = self.fh
        fh.file('~/.claude/debug/ok.txt', size=10, age_days=3)
        fh.file('~/.claude/mnt/inside.txt', size=10)
        target = os.stat(fh.path('~/.claude/mnt')).st_ino
        real = StatInfo.from_stat

        def fake(st):
            info = real(st)
            return dataclasses.replace(info, dev=info.dev + 1) if st.st_ino == target else info
        with mock.patch.object(StatInfo, 'from_stat', staticmethod(fake)):
            sid = run(fh, self.store)
        self.assertIn(('~/.claude/mnt', 'other_volume'), [(e['path'], e['error']) for e in self.store.scan_errors(sid)])
        self.assertEqual([p for p in (i.path for i in self.store.iter_items(sid)) if 'mnt' in p], [])
        self.assertIn('~/.claude/debug/ok.txt', [i.path for i in self.store.iter_items(sid)])

    def test_tainted_bundle_is_protected_with_evidence(self):
        fh = self.fh
        fh.file('~/.claude/plugins/installed_plugins.json', content='{"version": 2, "plugins": {}}')
        fh.file('~/.claude/plugins/cache/mkt/p/1.0/data.sqlite', size=10)
        fh.file('~/.claude/plugins/cache/mkt/p/1.0/readme.md', size=10)
        fh.file('~/.claude/plugins/cache/mkt/p/0.9/readme.md', size=10)
        sid = run(fh, self.store)
        tainted = self.store.item(sid, item_key('~/.claude/plugins/cache/mkt/p/1.0'))
        self.assertEqual((tainted.tier, tainted.taint, tainted.group_key),
                         (Tier.PROTECTED, 'database', 'claude.plugin-cache' + PROTECTED_SUFFIX))
        self.assertIn(Evidence('evidence.tainted', {'reason': 'database'}), tainted.evidence)
        self.assertEqual(tainted.files, 2)
        clean = self.store.item(sid, item_key('~/.claude/plugins/cache/mkt/p/0.9'))
        self.assertEqual((clean.tier, clean.taint, clean.group_key), (Tier.REVIEW, '', 'claude.plugin-cache'))

    def test_kinds_that_opt_out_of_name_taint_still_taint_structurally(self):
        fh = self.fh
        fh.file('~/Library/Application Support/Claude/claude-code/2.1.1/certs/cacert.pem', size=10)
        fh.file('~/Library/Application Support/Claude/claude-code/2.1.2/x.db', size=10)
        fh.file('~/Library/Application Support/Claude/claude-code/2.1.2/.git/HEAD', size=10)
        fh.file('~/Library/Application Support/Claude/claude-code/2.1.3/bin', size=10)
        sid = run(fh, self.store)
        base = '~/Library/Application Support/Claude/claude-code'
        old = self.store.item(sid, item_key(f'{base}/2.1.1'))
        self.assertEqual((old.taint, old.tier), ('', Tier.JUNK))   # pem and db names do not matter for engines
        git = self.store.item(sid, item_key(f'{base}/2.1.2'))
        self.assertEqual((git.taint, git.tier), ('git', Tier.PROTECTED))

    def test_git_repositories_become_protected_bundles(self):
        self.fh.file('~/.codex/worktrees/w1/.git/HEAD', size=10)
        self.fh.file('~/.codex/worktrees/w1/src/a.py', size=10)
        sid = run(self.fh, self.store)
        item = self.store.item(sid, item_key('~/.codex/worktrees/w1/.git'))
        self.assertEqual((item.rule_id, item.tier, item.unit, item.files), ('prot.git', Tier.PROTECTED, Unit.BUNDLE, 1))
        # everything else in the work tree is protected as one rolled-up group, whatever rule would match it
        (tree,) = self.items(sid, 'prot.git-tree:agent').values()
        self.assertEqual((tree.path, tree.files, tree.tier, tree.unit), ('~/.codex/worktrees/w1/src', 1, Tier.PROTECTED,
                                                                             Unit.BUNDLE))


class PrivacyScanTests(ScanCase):
    def catalog(self, roots):
        return custom_catalog([rule(patterns=('~/Library/Caches/Codex/**',))], roots, protections.PRIVACY_SKIP)

    def spy(self):
        calls = []
        real_scandir, real_lstat = os.scandir, os.lstat

        def scandir(path='.'):
            calls.append(os.fspath(path))
            return real_scandir(path)

        def lstat(path, *a, **kw):
            calls.append(os.fspath(path))
            return real_lstat(path, *a, **kw)
        return calls, mock.patch('os.scandir', scandir), mock.patch('os.lstat', lstat)

    def test_privacy_directories_are_never_opened(self):
        fh = self.fh
        fh.file('~/Library/Messages/chat.db', size=10)
        fh.file('~/Library/Mail/V10/mail.emlx', size=10)
        fh.file('~/Library/Caches/com.apple.Safari/x', size=10)
        fh.file('~/Library/Caches/Codex/ok.bin', size=10, age_days=1)
        fh.file('~/Library/Application Support/AddressBook/ab.db', size=10)
        cat = self.catalog([ScanRoot('~/Library', Domain.AGENT)])
        calls, scandir, lstat = self.spy()
        with scandir, lstat:
            sid = run(fh, self.store, cat=cat)
        forbidden = [str(fh.path(p)) for p in ('~/Library/Messages', '~/Library/Mail', '~/Library/Caches/com.apple.Safari',
                                               '~/Library/Application Support/AddressBook')]
        self.assertTrue(calls)
        self.assertEqual([c for c in calls if any(c == f or c.startswith(f + '/') for f in forbidden)], [])
        errors = {(e['path'], e['error']) for e in self.store.scan_errors(sid)}
        self.assertIn(('~/Library/Messages', 'skipped_privacy'), errors)
        self.assertIn(('~/Library/Mail', 'skipped_privacy'), errors)
        self.assertIn(('~/Library/Caches/com.apple.Safari', 'skipped_privacy'), errors)
        self.assertIn(('~/Library/Application Support/AddressBook', 'skipped_privacy'), errors)
        self.assertEqual({i.path for i in self.store.iter_items(sid)}, {'~/Library/Caches/Codex/ok.bin'})

    def test_a_privacy_root_is_skipped_without_touching_it(self):
        fh = self.fh
        fh.file('~/Library/Messages/chat.db', size=10)
        cat = self.catalog([ScanRoot('~/Library/Messages', Domain.AGENT)])
        calls, scandir, lstat = self.spy()
        with scandir, lstat:
            sid = run(fh, self.store, cat=cat)
        self.assertEqual([c for c in calls if 'Messages' in c], [])
        (root,) = self.store.scan_roots(sid)
        self.assertEqual((root['path'], root['status']), ('~/Library/Messages', 'skipped_privacy'))
        self.assertEqual([(e['path'], e['error']) for e in self.store.scan_errors(sid)],
                         [('~/Library/Messages', 'skipped_privacy')])


class ErrorHandlingTests(ScanCase):
    def test_permission_denied_directory(self):
        fh = self.fh
        fh.file('~/.claude/debug/ok.txt', size=10, age_days=3)
        fh.file('~/.claude/telemetry/x.json', size=10)
        os.chmod(fh.path('~/.claude/telemetry'), 0)
        self.addCleanup(os.chmod, fh.path('~/.claude/telemetry'), 0o755)
        sid = run(fh, self.store)
        errors = [(e['path'], e['error']) for e in self.store.scan_errors(sid)]
        self.assertEqual(errors, [('~/.claude/telemetry', 'permission_denied')])
        self.assertEqual({r['path']: r['status'] for r in self.store.scan_roots(sid)}['~/.claude'], 'ok')
        self.assertIn('~/.claude/debug/ok.txt', [i.path for i in self.store.iter_items(sid)])

    def test_permission_denied_root_is_reported_as_skipped(self):
        fh = self.fh
        fh.file('~/.claude/debug/ok.txt', size=10)
        os.chmod(fh.path('~/.claude'), 0)
        self.addCleanup(os.chmod, fh.path('~/.claude'), 0o755)
        sid = run(fh, self.store)
        roots = {r['path']: r['status'] for r in self.store.scan_roots(sid)}
        self.assertEqual(roots['~/.claude'], 'skipped_privacy')
        self.assertIn(('~/.claude', 'permission_denied'), [(e['path'], e['error']) for e in self.store.scan_errors(sid)])

    def test_safe_path_renders_undecodable_bytes(self):
        raw = os.fsdecode(b'/x/bad\xff')
        self.assertEqual(scanner.safe_path(raw), '/x/bad\\xff')
        self.assertEqual(scanner.safe_path('/x/fine-é'), '/x/fine-é')

    def test_undecodable_names_are_recorded_and_not_stored_as_items(self):
        fh = self.fh
        fh.file('~/.claude/debug/ok.txt', size=10, age_days=3)
        directory = os.fsencode(str(fh.path('~/.claude/debug')))
        extra = []
        try:
            os.close(os.open(directory + b'/bad\xff', os.O_CREAT | os.O_WRONLY, 0o600))
        except OSError as e:
            if e.errno != errno.EILSEQ:   # APFS refuses names that are not valid UTF-8
                raise
            extra = [_FakeEntry(os.fsdecode(b'bad\xff'), os.fsdecode(directory + b'/bad\xff')),
                     _FakeEntry(os.fsdecode(b'dir\xfe'), os.fsdecode(directory + b'/dir\xfe'), is_dir=True)]
        real = os.scandir
        target = os.fsdecode(directory)

        def scandir(path='.'):
            return _Listing(real(path), extra) if os.fspath(path) == target and extra else real(path)
        with mock.patch('os.scandir', scandir):
            sid = run(fh, self.store)
        errors = {(e['path'], e['error']) for e in self.store.scan_errors(sid)}
        self.assertIn(('~/.claude/debug/bad\\xff', 'undecodable_name'), errors)
        self.assertEqual({i.path for i in self.store.iter_items(sid)}, {'~/.claude/debug/ok.txt'})
        self.assertEqual(self.store.scan(sid)['status'], 'done')
        if extra:
            self.assertIn(('~/.claude/debug/dir\\xfe', 'undecodable_name'), errors)

    def test_a_sqlite_file_under_a_review_rule_is_protected_in_its_own_group_variant(self):
        fh = self.fh
        fh.file('~/.claude/projects/p/real.jsonl', content=b'{"type":"x"}\n', age_days=5)
        fh.file('~/.claude/projects/p/disguised.jsonl', content=b'SQLite format 3\0' + b'\0' * 64, age_days=5)
        sid = run(fh, self.store)
        plain = self.store.group(sid, 'claude.projects:~/.claude/projects/p')
        kept = self.store.group(sid, 'claude.projects#protected:~/.claude/projects/p')
        self.assertEqual((plain.tier, plain.items, kept.tier, kept.items), (Tier.REVIEW, 1, Tier.PROTECTED, 1))
        self.assertEqual((kept.rule_id, kept.label_args, kept.actions), ('claude.projects',
                                                                         {'path': '~/.claude/projects/p'}, ()))
        item = self.items(sid, kept.key)['~/.claude/projects/p']
        self.assertEqual([e.key for e in item.evidence], ['evidence.sqlite_header'])
        for loc in i18n.LOCALES:
            self.assertIn('~/.claude/projects/p', i18n.t(f'rule.{kept.rule_id}.label', loc, **kept.label_args))

    def test_cancel_marks_the_scan_cancelled(self):
        for n in range(30):
            self.fh.file(f'~/.codex/cache/d{n}/f', size=10)
        calls = []

        def cancel():
            calls.append(1)
            return len(calls) > 3
        with self.assertRaises(StewardError) as cm:
            run(self.fh, self.store, cancel=cancel)
        self.assertEqual((cm.exception.code, cm.exception.message_key), (ErrorCode.IN_PROGRESS, 'error.scan_cancelled'))
        self.assertIsNone(self.store.latest_scan('done'))
        cancelled = self.store.latest_scan('cancelled')
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertIsNone(cancelled['attributed'])
        self.assertEqual(list(self.store.iter_items(cancelled['id'])), [])

    def test_cancel_inside_a_bundle_walk(self):
        self.fh.file('~/.cache/codex-runtimes/codex-runtime-install-x/a', size=10)
        state = {'armed': False}
        real = scanner.walk_bundle

        def arming(*a, **kw):
            state['armed'] = True
            return real(*a, **kw)
        with mock.patch.object(scanner, 'walk_bundle', arming):
            with self.assertRaises(StewardError):
                run(self.fh, self.store, cancel=lambda: state['armed'])
        self.assertIsNotNone(self.store.latest_scan('cancelled'))

    def test_a_crash_marks_the_scan_failed_and_propagates(self):
        self.fh.file('~/.codex/cache/f', size=10)
        mac = FakeMacOS()
        mac.volume_usage = mock.Mock(side_effect=RuntimeError('disk gone'))
        with self.assertRaisesRegex(RuntimeError, 'disk gone'):
            run(self.fh, self.store, mac)
        failed = self.store.latest_scan('failed')
        self.assertIn('disk gone', failed['error'])


class ProgressTests(ScanCase):
    def test_throttled_to_one_event_per_quarter_second(self):
        for d in range(40):
            self.fh.file(f'~/.codex/cache/d{d}/f', size=10)
        clock = {'t': 0.0}

        def now():
            clock['t'] += 0.1
            return clock['t']
        events = []
        with mock.patch.object(scanner, '_now', now):
            run(self.fh, self.store, progress=lambda e: events.append((clock['t'], e)))
        self.assertGreaterEqual(len(events), 3)
        times = [t for t, _ in events]
        self.assertTrue(all(b - a >= 0.25 - 1e-9 for a, b in zip(times, times[1:])), times)
        for _, e in events:
            self.assertEqual(set(e), {'phase', 'root', 'files', 'alloc', 'current'})
        self.assertEqual(events[0][1]['phase'], 'scan')
        self.assertEqual([e['files'] for _, e in events], sorted(e['files'] for _, e in events))

    def test_real_clock_emits_at_least_the_first_event_and_few_after(self):
        for d in range(20):
            self.fh.file(f'~/.codex/cache/d{d}/f', size=10)
        events = []
        run(self.fh, self.store, progress=events.append)
        self.assertGreaterEqual(len(events), 1)
        self.assertLess(len(events), 8)


class LastUsedTests(ScanCase):
    def test_items_with_last_used_basis_are_batched_in_one_call(self):
        fh = self.fh
        fh.file('~/x/a.bin', size=10)
        fh.file('~/x/b.bin', size=10)
        fh.file('~/x/dir/c.bin', size=10)
        cat = custom_catalog([rule(age_basis=AgeBasis.LAST_USED, patterns=('~/x/*.bin',)),
                              rule(id='t.dir', unit=Unit.BUNDLE, age_basis=AgeBasis.LAST_USED, patterns=('~/x/dir',))],
                             [ScanRoot('~/x', Domain.AGENT)])
        mac = FakeMacOS(last_used_map={str(fh.path('~/x/a.bin')): 1_700_000_000.5,
                                       str(fh.path('~/x/dir')): 1_600_000_000.0})
        sid = run(fh, self.store, mac, cat)
        calls = [c for c in mac.calls if c[0] == 'last_used']
        self.assertEqual(len(calls), 1)
        self.assertEqual(sorted(calls[0][1][0]), sorted(str(fh.path(p)) for p in ('~/x/a.bin', '~/x/b.bin', '~/x/dir')))
        items = self.items(sid)
        self.assertEqual(items['~/x/a.bin'].last_used_ns, 1_700_000_000_500_000_000)
        self.assertEqual(items['~/x/dir'].last_used_ns, 1_600_000_000_000_000_000)
        self.assertIsNone(items['~/x/b.bin'].last_used_ns)


class StreamingTests(ScanCase):
    """Items go to the store in batches while the walk runs; nothing is held back but the current batch."""

    def spy(self):
        sizes = []
        real = self.store.add_items

        def add_items(scan_id, items):
            items = list(items)
            sizes.append(len(items))
            return real(scan_id, items)
        return sizes, mock.patch.object(self.store, 'add_items', add_items)

    def rows(self, sid):
        strip = lambda i: dataclasses.replace(i, scan_id=0)   # noqa: E731
        return ([strip(i) for i in self.store.iter_items(sid)],
                [dataclasses.replace(g, scan_id=0) for g in self.store.groups(sid)],
                self.store.scan(sid)['attributed'])

    def test_a_small_batch_size_gives_several_writes_and_the_same_rows(self):
        fx.build(self.fh)
        whole = run(self.fh, self.store, prune=False)
        sizes, spy = self.spy()
        with mock.patch.object(scanner, 'BATCH_ROWS', 7), spy:
            streamed = run(self.fh, self.store, prune=False)
        self.assertGreater(len(sizes), 5)
        self.assertEqual(set(sizes[:-1]), {7})
        self.assertLessEqual(sizes[-1], 7)
        total = self.store.count_items(streamed)
        self.assertEqual(sum(sizes), total)
        self.assertGreater(total, 40)
        self.assertEqual(self.rows(streamed), self.rows(whole))

    def test_the_default_batch_is_five_thousand_rows(self):
        self.assertEqual(scanner.BATCH_ROWS, 5000)

    def test_items_wait_for_their_last_used_date_across_batch_boundaries(self):
        fh = self.fh
        for n in range(12):
            fh.file(f'~/x/plain/f{n:02d}', size=10)
        for n in range(4):
            fh.file(f'~/x/used/{n}.bin', size=10)
        cat = custom_catalog([rule(id='t.plain', patterns=('~/x/plain/*',)),
                              rule(age_basis=AgeBasis.LAST_USED, patterns=('~/x/used/*.bin',))],
                             [ScanRoot('~/x', Domain.AGENT)])
        mac = FakeMacOS(last_used_map={str(fh.path('~/x/used/1.bin')): 1_700_000_000.0})
        sizes, spy = self.spy()
        with mock.patch.object(scanner, 'BATCH_ROWS', 3), spy:
            sid = run(fh, self.store, mac, cat)
        self.assertGreater(len(sizes), 3)
        self.assertEqual(len([c for c in mac.calls if c[0] == 'last_used']), 1)
        used = {i.path: i.last_used_ns for i in self.store.iter_items(sid) if i.rule_id == 't.rule'}
        self.assertEqual(used, {'~/x/used/0.bin': None, '~/x/used/1.bin': 1_700_000_000_000_000_000,
                                '~/x/used/2.bin': None, '~/x/used/3.bin': None})
        self.assertEqual(self.store.count_items(sid), 16)

    def test_a_cancelled_scan_leaves_none_of_the_batches_it_already_wrote(self):
        for n in range(30):
            self.fh.file(f'~/.codex/cache/d{n}/f', size=10)
        calls = []

        def cancel():
            calls.append(1)
            return len(calls) > 20
        sizes, spy = self.spy()
        with mock.patch.object(scanner, 'BATCH_ROWS', 4), spy, self.assertRaises(StewardError):
            run(self.fh, self.store, cancel=cancel)
        self.assertGreater(len(sizes), 0)        # something was streamed in before the cancel
        sid = self.store.latest_scan('cancelled')['id']
        self.assertEqual((self.store.count_items(sid), self.store.groups(sid)), (0, []))
        conn = self.store._conn()
        self.assertEqual([conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ('items', 'groups', 'dirs')],
                         [0, 0, 0])

    def test_a_failed_scan_leaves_none_of_the_batches_it_already_wrote(self):
        for n in range(30):
            self.fh.file(f'~/.codex/cache/d{n}/f', size=10)
        # the 'done' write fails, as a full disk would make it; the scan must then be recorded as failed, emptied
        with mock.patch.object(scanner, 'BATCH_ROWS', 4), mock.patch.object(
                self.store, 'finish_scan', side_effect=[RuntimeError('boom'), None]) as finish:
            with self.assertRaises(RuntimeError):
                run(self.fh, self.store)
        self.assertEqual(finish.call_args_list[-1].args[1], 'failed')
        self.assertEqual(self.store._conn().execute('SELECT COUNT(*) FROM items').fetchone()[0], 0)

    def test_a_finished_scan_prunes_the_one_before_unless_asked_not_to(self):
        fx.build(self.fh)
        first = run(self.fh, self.store)
        kept = run(self.fh, self.store, prune=False)
        self.assertGreater(self.store.count_items(first), 0)
        self.assertGreater(self.store.count_items(kept), 0)
        last = run(self.fh, self.store)
        self.assertEqual((self.store.count_items(first), self.store.count_items(kept)), (0, 0))
        self.assertGreater(self.store.count_items(last), 0)
        self.assertEqual(self.store.scan(first)['status'], 'done')     # the record of the scan stays


class RootSelectionTests(ScanCase):
    def test_nested_roots_are_not_scanned_twice(self):
        fh = self.fh
        fh.file('~/x/a/1', size=10)
        fh.file('~/x/a/b/2', size=10)
        cat = custom_catalog([rule()], [ScanRoot('~/x', Domain.AGENT), ScanRoot('~/x/a', Domain.AGENT),
                                        ScanRoot('~/x/a/b', Domain.AGENT)])
        sid = run(fh, self.store, cat=cat)
        self.assertEqual([r['path'] for r in self.store.scan_roots(sid)], ['~/x'])
        self.assertEqual(sorted(self.items(sid)), ['~/x/a/1', '~/x/a/b/2'])

    def test_domains_select_roots(self):
        fh = self.fh
        fh.file('~/x/a', size=10)
        fh.file('~/y/b', size=10)
        cat = custom_catalog([rule(patterns=('~/x/**', '~/y/**'))],
                             [ScanRoot('~/x', Domain.AGENT), ScanRoot('~/y', Domain.APP_LIBRARY)])
        sid = run(fh, self.store, cat=cat, domains=['app-library'])
        self.assertEqual(sorted(self.items(sid)), ['~/y/b'])
        self.assertEqual(self.store.scan(sid)['domains'], ['app-library'])
        sid = run(fh, self.store, cat=cat, domains=(Domain.AGENT, Domain.APP_LIBRARY))
        self.assertEqual(sorted(self.items(sid)), ['~/x/a', '~/y/b'])
        sid = run(fh, self.store, cat=cat, domains=())
        self.assertEqual(list(self.items(sid)), [])
        self.assertEqual(self.store.scan(sid)['status'], 'done')

    def test_unknown_files_use_the_roots_domain(self):
        self.fh.file('~/y/b', size=10)
        cat = custom_catalog([rule(patterns=('~/x/**',))], [ScanRoot('~/y', Domain.USER_FILES)])
        sid = run(self.fh, self.store, cat=cat, domains=['user-files'])
        (g,) = self.store.groups(sid)
        self.assertEqual((g.domain, g.rule_id), (Domain.USER_FILES, 'unknown'))

    def test_missing_roots_are_recorded(self):
        cat = custom_catalog([rule()], [ScanRoot('~/nope', Domain.AGENT)])
        sid = run(self.fh, self.store, cat=cat)
        (root,) = self.store.scan_roots(sid)
        self.assertEqual((root['status'], root['files'], root['alloc']), ('missing', 0, 0))


class RootAndDatalessTests(ScanCase):
    def test_a_root_on_another_volume_is_skipped(self):
        fh = self.fh
        fh.file('~/.codex/cache/a', size=10)
        fh.file('~/.claude/debug/x.txt', size=10, age_days=3)
        target = str(fh.path('~/.codex'))
        real = os.lstat

        def lstat(path, *a, **kw):
            st = real(path, *a, **kw)
            if os.fspath(path) == target:
                fields = list(st)
                fields[2] += 1   # st_dev
                return os.stat_result(fields)
            return st
        with mock.patch('os.lstat', lstat):
            sid = run(fh, self.store)
        roots = {r['path']: r['status'] for r in self.store.scan_roots(sid)}
        self.assertEqual((roots['~/.codex'], roots['~/.claude']), ('error', 'ok'))
        self.assertIn(('~/.codex', 'other_volume'), [(e['path'], e['error']) for e in self.store.scan_errors(sid)])
        self.assertEqual([i for i in self.store.iter_items(sid) if i.path.startswith('~/.codex')], [])

    def test_a_root_below_a_symlinked_ancestor_is_skipped(self):
        fh = self.fh
        fh.file('~/real/inside/file', size=10)
        os.symlink(fh.path('~/real'), fh.path('~/link'))
        cat = custom_catalog([rule(patterns=('~/link/inside/**',))], [ScanRoot('~/link/inside', Domain.AGENT)])
        sid = run(fh, self.store, cat=cat)
        (root,) = self.store.scan_roots(sid)
        self.assertEqual(root['status'], 'error')
        self.assertEqual([(e['path'], e['error']) for e in self.store.scan_errors(sid)],
                         [('~/link/inside', 'root_symlink')])
        self.assertEqual(list(self.store.iter_items(sid)), [])

    def datalessly(self, path):
        target = os.stat(path).st_ino
        real = StatInfo.from_stat

        def fake(st):
            info = real(st)
            return dataclasses.replace(info, flags=info.flags | SF_DATALESS) if st.st_ino == target else info
        return mock.patch.object(StatInfo, 'from_stat', staticmethod(fake))

    def scandir_spy(self):
        calls = []
        real = os.scandir

        def spy(path='.'):
            calls.append(os.fspath(path))
            return real(path)
        return calls, mock.patch('os.scandir', spy)

    def test_a_dataless_directory_is_never_opened_and_is_recorded(self):
        fh = self.fh
        fh.file('~/.claude/debug/ok.txt', size=10, age_days=3)
        fh.file('~/.claude/cloud/inside.txt', size=10)
        calls, spy = self.scandir_spy()
        with self.datalessly(fh.path('~/.claude/cloud')), spy:
            sid = run(fh, self.store)
        self.assertEqual([c for c in calls if c.endswith('/cloud')], [])
        self.assertIn(('~/.claude/cloud', 'dataless_skipped'), [(e['path'], e['error']) for e in self.store.scan_errors(sid)])
        self.assertEqual([i.path for i in self.store.iter_items(sid) if 'cloud' in i.path], [])

    def test_a_dataless_bundle_is_tainted_and_not_listed(self):
        fh = self.fh
        fh.file('~/.cache/codex-runtimes/codex-runtime-install-x/payload/a', size=10)
        bundle = fh.path('~/.cache/codex-runtimes/codex-runtime-install-x')
        calls, spy = self.scandir_spy()
        with self.datalessly(bundle), spy:
            sid = run(fh, self.store)
        item = self.store.item(sid, item_key('~/.cache/codex-runtimes/codex-runtime-install-x'))
        self.assertEqual((item.tier, item.taint, item.files), (Tier.PROTECTED, 'dataless', 0))
        self.assertEqual([c for c in calls if 'codex-runtime-install-x' in c], [])


class _FakeEntry:
    def __init__(self, name, path, is_dir=False):
        self.name, self.path, self._dir = name, path, is_dir

    def is_symlink(self):
        return False

    def is_dir(self, follow_symlinks=True):
        return self._dir

    def stat(self, follow_symlinks=True):
        raise AssertionError('an undecodable entry must not be stat-ed')


class _Listing:
    def __init__(self, real, extra):
        self._real, self._extra = real, extra

    def __enter__(self):
        return iter(list(self._real) + self._extra)

    def __exit__(self, *exc):
        self._real.close()
        return False


if __name__ == '__main__':
    unittest.main()
