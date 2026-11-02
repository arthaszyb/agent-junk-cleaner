"""The general-Mac catalog: app-library, user-files and dev-shared rules, their detectors and their texts."""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from disk_steward import i18n, scanner, selection
from disk_steward.catalog import dev_shared, load_catalog, user_files
from disk_steward.classify import Classifier
from disk_steward.detectors import REGISTRY, DetectorContext, compute_facts
from disk_steward.i18n import LOCALES
from disk_steward.macos import FakeMacOS
from disk_steward.model import (Action, AgeBasis, DecisionState, Domain, ErrorCode, Evidence, GroupSel, Kind,
                                Spec, StatInfo, StewardError, Tier, Unit)
from disk_steward.settings import Settings
from disk_steward.store import Store

from support import DAY, FakeHome

MIB = 1024 * 1024
STAT = StatInfo(dev=1, ino=1, mode=0o100644, nlink=1, uid=1, size=0, blocks=0, mtime_ns=0, birth_ns=0, flags=0)
CAT = load_catalog(Settings())
SUPPORT = '~/Library/Application Support'
CACHES = '~/Library/Caches'


def app(stem, bundle_id, name='', display_name='', executable=''):
    return {'path': f'/Applications/{stem}.app', 'name': name, 'display_name': display_name,
            'executable': executable, 'stem': stem}


class Base(unittest.TestCase):
    def setUp(self):
        self.fh = FakeHome().__enter__()
        self.addCleanup(self.fh.cleanup)
        self.store = Store(self.fh.data_dir / 'steward.db')
        self.addCleanup(self.store.close)

    def scan(self, domains=('app-library',), mac=None, **settings):
        self.settings = Settings(domains=tuple(domains), **settings)
        self.catalog = load_catalog(self.settings)
        self.mac = mac or FakeMacOS()
        facts, _ = compute_facts(DetectorContext(self.fh.bases, self.mac, self.settings), self.catalog)
        classifier = Classifier(self.catalog, facts, self.fh.bases, self.settings)
        return scanner.scan(self.store, classifier, self.catalog, self.fh.bases, self.mac, self.settings, domains)

    def items(self, sid):
        return {i.path: i for i in self.store.items(sid)}

    def states(self, sid, *group_keys, filter_days=0.0, now=None):
        """Decision state per item path when every named group is selected."""
        spec = Spec(scan_id=sid, filter_min_age_days=filter_days, groups={k: GroupSel() for k in group_keys})
        res = selection.resolve(self.store, spec, now if now is not None else self.fh.clock(), self.settings,
                                self.mac)
        return {i.path: res.decisions[i.key].state for i in self.store.items(sid)}

    def seen(self, item):
        return (item.rule_id, item.tier, item.group_key)

    def age_tree(self, logical, days):
        """Back-date a folder and everything in it: a bundle is as old as its newest entry."""
        root = self.fh.path(logical)
        ns = int((self.fh.clock() - days * DAY) * 1e9)
        for base, dirs, files in os.walk(root, topdown=False):
            for name in dirs + files:
                os.utime(os.path.join(base, name), ns=(ns, ns), follow_symlinks=False)
        os.utime(root, ns=(ns, ns), follow_symlinks=False)


class UpdateLeftoverTests(Base):
    def test_updater_shipit_and_sparkle_folders(self):
        fh = self.fh
        fh.file(f'{CACHES}/ai-ops-updater/pending/update.zip', size=100, age_days=5)
        fh.file(f'{CACHES}/ai-ops-updater/current.blockmap', size=10, age_days=5)
        fh.file(f'{CACHES}/com.foo.app.ShipIt/update.zip', size=100, age_days=5)
        fh.file(f'{CACHES}/com.bar.app/org.sparkle-project.Sparkle/Installation/x/app.zip', size=100, age_days=5)
        items = self.items(self.scan())
        self.assertEqual(self.seen(items[f'{CACHES}/ai-ops-updater/pending/update.zip']),
                         ('app.updater-cache', Tier.JUNK, f'app.updater-cache:{CACHES}/ai-ops-updater'))
        self.assertEqual(self.seen(items[f'{CACHES}/ai-ops-updater/current.blockmap'])[0], 'app.updater-cache')
        self.assertEqual(self.seen(items[f'{CACHES}/com.foo.app.ShipIt/update.zip']),
                         ('app.shipit', Tier.JUNK, f'app.shipit:{CACHES}/com.foo.app.ShipIt'))
        self.assertEqual(self.seen(items[f'{CACHES}/com.bar.app/org.sparkle-project.Sparkle/Installation/x/app.zip']),
                         ('app.sparkle', Tier.JUNK, f'app.sparkle:{CACHES}/com.bar.app'))
        for rule_id in ('app.updater-cache', 'app.shipit', 'app.sparkle'):
            r = self.catalog.rule(rule_id)
            self.assertEqual((r.kind, r.min_age_floor_days, r.group_depth), (Kind.UPDATE_LEFTOVER, 1, 4), rule_id)

    def test_agent_specific_rules_keep_priority(self):
        fh = self.fh
        fh.file(f'{CACHES}/com.openai.codex/org.sparkle-project.Sparkle/Installation/u1/a.zip', size=10, age_days=5)
        fh.file(f'{CACHES}/com.anthropic.claudefordesktop.ShipIt/update.zip', size=10, age_days=5)
        items = self.items(self.scan(domains=('agent', 'app-library')))
        codex = items[f'{CACHES}/com.openai.codex/org.sparkle-project.Sparkle/Installation/u1']
        self.assertEqual((codex.rule_id, codex.domain, codex.unit), ('codex.sparkle', Domain.AGENT, Unit.BUNDLE))
        claude = items[f'{CACHES}/com.anthropic.claudefordesktop.ShipIt/update.zip']
        self.assertEqual((claude.rule_id, claude.domain), ('claude.shipit', Domain.AGENT))
        self.assertGreater(self.catalog.rule('claude.shipit').priority, self.catalog.rule('app.shipit').priority)

    def test_floor_is_one_day(self):
        fh = self.fh
        fh.file(f'{CACHES}/x-updater/old.zip', size=10, age_days=3)
        fh.file(f'{CACHES}/x-updater/new.zip', size=10, age_days=0.1)
        sid = self.scan()
        states = self.states(sid, f'app.updater-cache:{CACHES}/x-updater')
        self.assertEqual(states[f'{CACHES}/x-updater/old.zip'], DecisionState.SELECTED)
        self.assertEqual(states[f'{CACHES}/x-updater/new.zip'], DecisionState.TOO_RECENT_FLOOR)


class GenericCacheTests(Base):
    def test_cache_db_stays_protected_and_its_neighbours_in_the_same_folder_too(self):
        fh = self.fh
        fh.file(f'{CACHES}/com.foo.bar/Cache.db', size=100, age_days=30)
        fh.file(f'{CACHES}/com.foo.bar/Cache.db-wal', size=100, age_days=30)
        fh.file(f'{CACHES}/com.foo.bar/fsCachedData/blob1', size=100, age_days=30)
        fh.file(f'{CACHES}/com.foo.bar/fsCachedData/blob2', size=100, age_days=30)
        fh.file(f'{CACHES}/com.foo.bar/note.bin', size=100, age_days=30)
        items = self.items(self.scan())
        self.assertEqual(self.seen(items[f'{CACHES}/com.foo.bar/fsCachedData/blob1']),
                         ('app.caches', Tier.JUNK, f'app.caches:{CACHES}/com.foo.bar'))
        self.assertEqual(items[f'{CACHES}/com.foo.bar/fsCachedData/blob2'].tier, Tier.JUNK)
        protected = [i for i in items.values() if i.tier == Tier.PROTECTED]
        self.assertEqual(sum(i.files for i in protected), 3)   # Cache.db, its journal and the neighbour
        self.assertNotIn(f'{CACHES}/com.foo.bar/note.bin', items)   # rolled up with the other protected files

    def test_spotify_is_review_and_needs_spotify_quit(self):
        self.fh.file(f'{CACHES}/com.spotify.client/Data/ab/offline.file', size=100, age_days=30)
        sid = self.scan()
        item = self.items(sid)[f'{CACHES}/com.spotify.client/Data/ab/offline.file']
        self.assertEqual(self.seen(item), ('app.spotify-cache', Tier.REVIEW, 'app.spotify-cache'))
        self.assertEqual(self.catalog.rule('app.spotify-cache').quit_apps, ('com.spotify.client',))
        self.assertEqual(item.kind, Kind.CACHE)

    def test_generic_cache_is_the_lowest_app_rule(self):
        self.assertEqual(CAT.rule('app.caches').priority, 50)
        self.assertEqual(min(r.priority for r in CAT.rules if r.id.startswith('app.')), 50)
        r = CAT.rule('app.caches')
        self.assertEqual((r.tier, r.kind, r.min_age_floor_days, r.group_depth, r.actions),
                         (Tier.JUNK, Kind.CACHE, 1, 4, (Action.DELETE,)))


class BrowserCacheTests(Base):
    """A Chromium profile's cache folders are one item each, not one item per cache file."""

    BROWSERS = (('app.chrome-cache', f'{CACHES}/Google/Chrome', 'com.google.Chrome'),
                ('app.chrome-beta-cache', f'{CACHES}/Google/Chrome Beta', 'com.google.Chrome.beta'),
                ('app.chrome-canary-cache', f'{CACHES}/Google/Chrome Canary', 'com.google.Chrome.canary'),
                ('app.brave-cache', f'{CACHES}/BraveSoftware/Brave-Browser', 'com.brave.Browser'),
                ('app.edge-cache', f'{CACHES}/Microsoft Edge', 'com.microsoft.edgemac'),
                ('app.arc-cache', f'{CACHES}/Arc', 'company.thebrowser.Browser'),
                ('app.chromium-cache', f'{CACHES}/Chromium', 'org.chromium.Chromium'),
                ('app.vivaldi-cache', f'{CACHES}/Vivaldi', 'com.vivaldi.Vivaldi'))

    def test_every_browser_is_a_bundle_rule_that_needs_the_browser_quit(self):
        self.assertEqual({r for r, _, _ in self.BROWSERS}, {r.id for r in CAT.rules if r.id.endswith('-cache')
                                                           and r.id.startswith('app.') and r.unit == Unit.BUNDLE})
        for rule_id, root, bundle_id in self.BROWSERS:
            r = CAT.rule(rule_id)
            with self.subTest(rule_id):
                self.assertEqual((r.tier, r.kind, r.unit, r.quit_apps, r.actions, r.default_action),
                                 (Tier.JUNK, Kind.CACHE, Unit.BUNDLE, (bundle_id,), (Action.DELETE,), Action.DELETE))
                self.assertEqual(r.patterns, (f'{root}/*/Cache', f'{root}/*/Code Cache'))
                self.assertGreater(r.priority, CAT.rule('app.caches').priority)
                self.assertIsNone(r.group_depth)

    def test_a_profile_becomes_two_items_and_its_other_files_stay_with_the_generic_rule(self):
        for rule_id, root, _ in self.BROWSERS:
            for n in range(5):
                self.fh.file(f'{root}/Default/Cache/Cache_Data/f_{n:06d}', size=100, age_days=30)
            self.fh.file(f'{root}/Default/Cache/Cache_Data/index', size=100, age_days=30)
            self.fh.file(f'{root}/Profile 1/Code Cache/js/a_0', size=100, age_days=30)
            self.fh.file(f'{root}/Default/Visited Links', size=100, age_days=30)
        sid = self.scan()
        items = self.items(sid)
        for rule_id, root, _ in self.BROWSERS:
            with self.subTest(rule_id):
                cache = items[f'{root}/Default/Cache']
                self.assertEqual((cache.rule_id, cache.group_key, cache.tier, cache.kind, cache.unit, cache.domain),
                                 (rule_id, rule_id, Tier.JUNK, Kind.CACHE, Unit.BUNDLE, Domain.APP_LIBRARY))
                self.assertEqual(cache.files, 6)
                self.assertEqual(self.seen(items[f'{root}/Profile 1/Code Cache']), (rule_id, Tier.JUNK, rule_id))
                self.assertEqual(self.store.group(sid, rule_id).items, 2)
                self.assertNotIn(f'{root}/Default/Cache/Cache_Data/f_000001', items)   # nothing inside is an item
        generic = items[f'{CACHES}/Google/Chrome/Default/Visited Links']
        self.assertEqual(generic.rule_id, 'app.caches')

    def test_a_cache_of_a_browser_not_listed_stays_with_the_generic_rule(self):
        self.fh.file(f'{CACHES}/Opera Software/Opera Stable/Cache/f_000001', size=100, age_days=30)
        items = self.items(self.scan())
        self.assertEqual(items[f'{CACHES}/Opera Software/Opera Stable/Cache/f_000001'].rule_id, 'app.caches')

    def test_the_bundle_is_selectable_as_a_whole_and_judged_by_its_newest_file(self):
        root = f'{CACHES}/Google/Chrome'
        self.fh.file(f'{root}/Default/Cache/Cache_Data/old', size=100)
        self.fh.file(f'{root}/Default/Cache/Cache_Data/new', size=100)
        self.fh.file(f'{root}/Profile 1/Cache/Cache_Data/old', size=100)
        self.age_tree(f'{root}/Default/Cache', 30)
        self.age_tree(f'{root}/Profile 1/Cache', 30)
        self.fh.set_age(f'{root}/Default/Cache/Cache_Data/new', 1)
        sid = self.scan()
        by_path = self.states(sid, 'app.chrome-cache', filter_days=14)
        self.assertEqual(by_path[f'{root}/Default/Cache'], DecisionState.TOO_RECENT)
        self.assertEqual(by_path[f'{root}/Profile 1/Cache'], DecisionState.SELECTED)
        self.assertEqual(self.states(sid, 'app.chrome-cache', filter_days=0)[f'{root}/Default/Cache'],
                         DecisionState.SELECTED)

    def test_the_cache_of_every_profile_is_found_wherever_the_profile_folder_is_named(self):
        root = f'{CACHES}/Microsoft Edge'
        for profile in ('Default', 'Profile 1', 'Guest Profile', 'System Profile'):
            self.fh.file(f'{root}/{profile}/Cache/Cache_Data/f_0', size=100, age_days=30)
        items = self.items(self.scan())
        self.assertEqual(sorted(p for p, i in items.items() if i.rule_id == 'app.edge-cache'),
                         sorted(f'{root}/{p}/Cache' for p in ('Default', 'Profile 1', 'Guest Profile', 'System Profile')))

    def test_headless_chrome_leftovers_are_junk_temp_bundles_with_a_one_day_floor(self):
        root = f'{CACHES}/Google/Chrome-headless'
        self.fh.file(f'{root}/scoped_dir101/Default/Cache/data_0', size=100)
        self.fh.file(f'{root}/scoped_dir102/x', size=100)
        self.fh.file(f'{root}/not-a-scoped-dir/x', size=100, age_days=5)
        self.age_tree(f'{root}/scoped_dir101', 5)
        self.age_tree(f'{root}/scoped_dir102', 0.2)
        sid = self.scan()
        items = self.items(sid)
        item = items[f'{root}/scoped_dir101']
        self.assertEqual((item.rule_id, item.tier, item.kind, item.unit, item.group_key),
                         ('app.headless-chrome', Tier.JUNK, Kind.TEMP, Unit.BUNDLE, 'app.headless-chrome'))
        self.assertEqual(items[f'{CACHES}/Google/Chrome-headless/not-a-scoped-dir/x'].rule_id, 'app.caches')
        r = self.catalog.rule('app.headless-chrome')
        self.assertEqual((r.min_age_floor_days, r.quit_apps, r.actions), (1, (), (Action.DELETE,)))
        self.assertGreater(r.priority, self.catalog.rule('app.caches').priority)
        states = self.states(sid, 'app.headless-chrome')
        self.assertEqual((states[f'{root}/scoped_dir101'], states[f'{root}/scoped_dir102']),
                         (DecisionState.SELECTED, DecisionState.TOO_RECENT_FLOOR))

    def test_texts_exist_in_every_language(self):
        for rule_id in [r for r, _, _ in self.BROWSERS] + ['app.headless-chrome']:
            for loc in LOCALES:
                with self.subTest(rule_id=rule_id, locale=loc):
                    label = i18n.t(f'rule.{rule_id}.label', loc)
                    effect = i18n.t(f'rule.{rule_id}.effect', loc)
                    self.assertNotEqual(label, f'rule.{rule_id}.label')
                    self.assertNotEqual(effect, f'rule.{rule_id}.effect')
                    self.assertNotIn('{', label + effect)


class LogTests(Base):
    def test_logs_by_name(self):
        fh = self.fh
        fh.file('~/Library/Logs/DiagnosticReports/Foo-2026.ips', size=100, age_days=30)
        fh.file('~/Library/Logs/DiagnosticReports/Retired/Bar.crash', size=100, age_days=30)
        fh.file('~/Library/Logs/Zoom/zoom.log.1', size=100, age_days=30)
        fh.file('~/Library/Logs/Zoom/readme.md', size=100, age_days=30)
        fh.file('~/Library/Logs/Claude/main.log', size=100, age_days=30)
        items = self.items(self.scan(domains=('agent', 'app-library')))
        self.assertEqual(self.seen(items['~/Library/Logs/DiagnosticReports/Foo-2026.ips']),
                         ('app.logs', Tier.JUNK, 'app.logs:~/Library/Logs/DiagnosticReports'))
        self.assertEqual(items['~/Library/Logs/DiagnosticReports/Retired/Bar.crash'].rule_id, 'app.logs')
        self.assertEqual(self.seen(items['~/Library/Logs/Zoom/zoom.log.1'])[2], 'app.logs:~/Library/Logs/Zoom')
        self.assertEqual(items['~/Library/Logs/Claude/main.log'].rule_id, 'claude.logs')
        self.assertNotIn('~/Library/Logs/Zoom/readme.md', items)
        self.assertEqual(self.catalog.rule('app.logs').min_age_floor_days, 1)
        unknown = [i for i in items.values() if i.rule_id == 'unknown']
        self.assertEqual([i.group_key for i in unknown], ['unknown:~/Library/Logs/Zoom'])


class ElectronCacheTests(Base):
    def test_installed_app_adds_a_quit_requirement(self):
        fh = self.fh
        fh.file(f'{SUPPORT}/Notion/Cache/Cache_Data/a', size=100, age_days=30)
        fh.file(f'{SUPPORT}/Notion/GPUCache/data_0', size=100, age_days=30)
        fh.file(f'{SUPPORT}/Notion/Partitions/p1/Code Cache/js/b', size=100, age_days=30)
        fh.file(f'{SUPPORT}/Notion/state.json', size=100, age_days=30)
        mac = FakeMacOS(app_info={'notion.id': app('Notion', 'notion.id', name='Notion')})
        sid = self.scan(mac=mac)
        items = self.items(sid)
        cached = items[f'{SUPPORT}/Notion/Cache/Cache_Data/a']
        self.assertEqual(self.seen(cached), ('app.electron.cache', Tier.JUNK,
                                             f'app.electron.cache:{SUPPORT}/Notion'))
        self.assertEqual(cached.evidence, (Evidence('evidence.requires_quit',
                                                    {'bundle_id': 'notion.id', 'app': 'Notion'}),))
        self.assertEqual(items[f'{SUPPORT}/Notion/GPUCache/data_0'].rule_id, 'app.electron.cache')
        self.assertEqual(items[f'{SUPPORT}/Notion/Partitions/p1/Code Cache/js/b'].rule_id, 'app.electron.cache')
        self.assertNotIn(f'{SUPPORT}/Notion/state.json', items)
        self.assertEqual(self.catalog.rule('app.electron.cache').quit_apps, ())
        (group,) = [g for g in self.store.groups(sid) if g.rule_id == 'app.electron.cache']
        self.assertEqual((group.items, group.actions), (3, (Action.DELETE,)))

    def test_missing_app_is_still_junk_with_its_own_evidence(self):
        self.fh.file(f'{SUPPORT}/Ghost/Code Cache/js/a', size=100, age_days=30)
        item = self.items(self.scan())[f'{SUPPORT}/Ghost/Code Cache/js/a']
        self.assertEqual((item.tier, item.rule_id), (Tier.JUNK, 'app.electron.cache'))
        self.assertEqual(item.evidence, (Evidence('evidence.app_not_installed', {'id': 'Ghost'}),))

    def test_agent_rules_outrank_the_generic_one(self):
        fh = self.fh
        fh.file(f'{SUPPORT}/Claude/Cache/a', size=10, age_days=30)
        fh.file(f'{SUPPORT}/Cursor/GPUCache/a', size=10, age_days=30)
        fh.file(f'{SUPPORT}/Code/Cache/a', size=10, age_days=30)
        items = self.items(self.scan(domains=('agent', 'app-library')))
        self.assertEqual(items[f'{SUPPORT}/Claude/Cache/a'].rule_id, 'claude.electron.cache')
        self.assertEqual(items[f'{SUPPORT}/Cursor/GPUCache/a'].rule_id, 'cursor.electron.cache')
        self.assertEqual(items[f'{SUPPORT}/Code/Cache/a'].rule_id, 'vscode.electron.cache')

    def test_detector_matches_every_name_an_app_goes_by_case_insensitively(self):
        mac = FakeMacOS(app_info={
            'a.id': app('Visual Studio Code', 'a.id', name='Code', executable='Electron'),
            'b.id': app('Slack', 'b.id', display_name='Slack Chat'),
            'c.id': app('Twin', 'c.id'), 'd.id': app('TWIN', 'd.id')})
        facts = REGISTRY['electron_apps'].prepare(DetectorContext(self.fh.bases, mac, Settings()))
        rule = CAT.rule('app.electron.cache')

        def refine(folder):
            return REGISTRY['electron_apps'].refine(rule, f'{SUPPORT}/{folder}/Cache/x', STAT, False, facts)

        for folder, bundle in (('Code', 'a.id'), ('electron', 'a.id'), ('visual studio code', 'a.id'),
                               ('SLACK', 'b.id'), ('slack chat', 'b.id')):
            with self.subTest(folder=folder):
                self.assertEqual([(e.key, e.args['bundle_id']) for e in refine(folder).evidence],
                                 [('evidence.requires_quit', bundle)])
        twin = refine('twin')
        self.assertEqual(sorted(e.args['bundle_id'] for e in twin.evidence), ['c.id', 'd.id'])   # both must quit
        self.assertEqual(refine('Other').evidence[0].key, 'evidence.app_not_installed')
        self.assertFalse(refine('Other').skip)
        self.assertEqual(json.loads(json.dumps(facts)), facts)

    def test_real_adapter_reads_names_from_info_plists(self):
        import plistlib
        from disk_steward.macos import RealMacOS
        bundle = self.fh.root / 'Applications' / 'Foo Bar.app'
        (bundle / 'Contents').mkdir(parents=True)
        with open(bundle / 'Contents' / 'Info.plist', 'wb') as f:
            plistlib.dump({'CFBundleIdentifier': 'com.foo.bar', 'CFBundleName': 'Foo', 'CFBundleExecutable': 'foo-bin',
                           'CFBundleDisplayName': 5}, f)
        mac = RealMacOS(home=self.fh.home)
        with mock.patch.object(RealMacOS, 'installed_apps', return_value={'com.foo.bar': str(bundle)}):
            info = mac.installed_app_info()
        self.assertEqual(info, {'com.foo.bar': {'path': str(bundle), 'name': 'Foo', 'display_name': '',
                                                'executable': 'foo-bin', 'stem': 'Foo Bar'}})


class MacOSAdapterTests(unittest.TestCase):
    def test_fake_adapter_hands_out_copies_and_records_the_call(self):
        mac = FakeMacOS(app_info={'a.b': app('A', 'a.b', name='A')})
        info = mac.installed_app_info()
        info['a.b']['name'] = 'changed'
        self.assertEqual(mac.installed_app_info()['a.b']['name'], 'A')
        self.assertEqual(mac.calls, [('installed_app_info', ()), ('installed_app_info', ())])
        self.assertEqual(FakeMacOS().installed_app_info(), {})


class VsCodeTests(Base):
    def test_old_code_caches_obsolete_extensions_and_logs(self):
        fh = self.fh
        fh.file(f'{SUPPORT}/Code/CachedData/aaa/x.bin', size=100, age_days=40)
        fh.file(f'{SUPPORT}/Code/CachedData/bbb/x.bin', size=100, age_days=2)
        fh.file(f'{SUPPORT}/Code/logs/20260101/main.log', size=100, age_days=40)
        fh.file(f'{SUPPORT}/Code/CachedExtensionVSIXs/e.vsix', size=100, age_days=40)
        fh.file(f'{SUPPORT}/Code/WebStorage/5/x', size=100, age_days=40)
        fh.file('~/.vscode/extensions/.obsolete', content=json.dumps({'old.ext-1.0.0': True, 'live.ext-1.0.0': False}))
        fh.file('~/.vscode/extensions/old.ext-1.0.0/package.json', content='{}', age_days=40)
        fh.file('~/.vscode/extensions/live.ext-1.0.0/package.json', content='{}', age_days=40)
        for d in ('aaa', 'bbb'):
            fh.set_age(f'{SUPPORT}/Code/CachedData/{d}', 40 if d == 'aaa' else 2)
        items = self.items(self.scan())
        old, newest = items[f'{SUPPORT}/Code/CachedData/aaa'], items[f'{SUPPORT}/Code/CachedData/bbb']
        self.assertEqual((old.rule_id, old.tier, old.unit), ('vscode.cached-data', Tier.JUNK, Unit.BUNDLE))
        self.assertEqual((newest.tier, newest.group_key), (Tier.PROTECTED, 'vscode.cached-data#protected'))
        self.assertEqual(items[f'{SUPPORT}/Code/logs/20260101/main.log'].rule_id, 'vscode.logs')
        self.assertEqual(items[f'{SUPPORT}/Code/CachedExtensionVSIXs/e.vsix'].rule_id, 'vscode.vsix')
        ext = items['~/.vscode/extensions/old.ext-1.0.0']
        self.assertEqual((ext.rule_id, ext.tier, [e.key for e in ext.evidence]),
                         ('vscode.extensions', Tier.JUNK, ['evidence.extension_obsolete']))
        self.assertNotIn('~/.vscode/extensions/live.ext-1.0.0', items)   # not obsolete: the scanner descends
        self.assertNotIn(f'{SUPPORT}/Code/WebStorage/5/x', items)   # nobody offers VS Code's browser storage
        self.assertEqual({r.quit_apps for r in self.catalog.rules if r.id.startswith('vscode.')},
                         {('com.microsoft.VSCode',)})


class LeftoverTests(Base):
    def test_unknown_reverse_dns_folder_is_a_review_leftover(self):
        fh = self.fh
        fh.file(f'{SUPPORT}/com.gone.app/data.txt', size=100, age_days=200)
        fh.file(f'{SUPPORT}/com.here.app/data.txt', size=100, age_days=200)
        fh.file(f'{SUPPORT}/com.here.app.helper/data.txt', size=100, age_days=200)
        fh.file(f'{SUPPORT}/com.here/data.txt', size=100, age_days=200)
        fh.file(f'{SUPPORT}/com.apple.thing/data.txt', size=100, age_days=200)
        fh.file(f'{SUPPORT}/Slack/data.txt', size=100, age_days=200)
        fh.file(f'{SUPPORT}/com.young.app/data.txt', size=100, age_days=10)
        for d in ('com.gone.app', 'com.here.app', 'com.here.app.helper', 'com.here', 'com.apple.thing', 'Slack',
                  'com.young.app'):
            fh.set_age(f'{SUPPORT}/{d}', 200 if d != 'com.young.app' else 10)
        mac = FakeMacOS(app_info={'com.here.app': app('Here', 'com.here.app')})
        sid = self.scan(mac=mac)
        items = self.items(sid)
        gone = items[f'{SUPPORT}/com.gone.app']
        self.assertEqual((gone.rule_id, gone.tier, gone.unit, gone.kind, gone.group_key),
                         ('app.leftovers', Tier.REVIEW, Unit.BUNDLE, Kind.LEFTOVER, 'app.leftovers'))
        self.assertEqual(gone.evidence, (Evidence('evidence.app_not_installed', {'id': 'com.gone.app'}),))
        # installed apps, their helpers and prefixes, Apple's folders and plain names are all left alone
        self.assertEqual({p for p, i in items.items() if i.rule_id == 'app.leftovers'},
                         {f'{SUPPORT}/com.gone.app', f'{SUPPORT}/com.young.app'})
        r = self.catalog.rule('app.leftovers')
        self.assertEqual((r.actions, r.default_action, r.min_age_floor_days),
                         ((Action.TRASH, Action.DELETE), Action.TRASH, 90))
        states = self.states(sid, 'app.leftovers')
        self.assertEqual(states[f'{SUPPORT}/com.gone.app'], DecisionState.SELECTED)
        self.assertEqual(states[f'{SUPPORT}/com.young.app'], DecisionState.TOO_RECENT_FLOOR)

    def test_detector_only_accepts_reverse_dns_names(self):
        mac = FakeMacOS(app_info={'com.Foo.Bar': app('Foo', 'com.Foo.Bar')})
        facts = REGISTRY['orphans'].prepare(DetectorContext(self.fh.bases, mac, Settings()))
        self.assertEqual(facts, {'installed': ['com.foo.bar']})
        rule = CAT.rule('app.leftovers')

        def skipped(folder):
            return REGISTRY['orphans'].refine(rule, f'{SUPPORT}/{folder}', STAT, True, facts).skip

        for folder in ('Slack', 'node-v18.0.1', 'v1.2.3', 'com.foo', 'com', 'com.apple.Safari', 'COM.APPLE.x.y',
                       'com.foo.bar', 'COM.FOO.BAR', 'com.foo.bar.helper', 'My App.v1.2', '.hidden.a.b'):
            with self.subTest(folder=folder):
                self.assertTrue(skipped(folder), folder)
        for folder in ('com.gone.app', 'org.x.y.z', 'io.some-tool.cli', 'com.foobar.baz', 'com.foo.barbaz'):
            with self.subTest(folder=folder):
                self.assertFalse(skipped(folder), folder)

    def test_macos_private_folders_are_never_opened(self):
        self.fh.file(f'{SUPPORT}/com.apple.thing/a', size=10, age_days=200)
        sid = self.scan()
        self.assertEqual(self.items(sid), {})
        self.assertIn((f'{SUPPORT}/com.apple.thing', 'skipped_privacy'),
                      [(e['path'], e['error']) for e in self.store.scan_errors(sid)])


class TempTests(Base):
    def test_files_and_folders_in_tmpdir_respect_the_three_day_floor(self):
        fh = self.fh
        fh.file('$TMPDIR/old.dat', size=10, age_days=10)
        fh.file('$TMPDIR/young.dat', size=10, age_days=1)
        fh.file('$TMPDIR/olddir/a.txt', size=10, age_days=10)
        fh.set_age('$TMPDIR/olddir', 10)
        fh.file('$TMPDIR/younger/a.txt', size=10, age_days=1)
        fh.set_age('$TMPDIR/younger', 1)
        sid = self.scan()
        items = self.items(sid)
        self.assertEqual(self.seen(items['$TMPDIR/old.dat']), ('app.tmp', Tier.JUNK, 'app.tmp'))
        self.assertEqual(self.seen(items['$TMPDIR/olddir']), ('app.tmp-dir', Tier.JUNK, 'app.tmp-dir'))
        self.assertEqual((items['$TMPDIR/olddir'].unit, items['$TMPDIR/olddir'].kind), (Unit.BUNDLE, Kind.TEMP))
        states = self.states(sid, 'app.tmp', 'app.tmp-dir')
        self.assertEqual({p: s for p, s in states.items() if p.startswith('$TMPDIR/')},
                         {'$TMPDIR/old.dat': DecisionState.SELECTED, '$TMPDIR/young.dat': DecisionState.TOO_RECENT_FLOOR,
                          '$TMPDIR/olddir': DecisionState.SELECTED, '$TMPDIR/younger': DecisionState.TOO_RECENT_FLOOR})

    def test_a_folder_with_a_database_in_it_is_protected(self):
        self.fh.file('$TMPDIR/app-state/data.sqlite', size=10, age_days=10)
        self.fh.set_age('$TMPDIR/app-state', 10)
        item = self.items(self.scan())['$TMPDIR/app-state']
        self.assertEqual((item.tier, item.taint), (Tier.PROTECTED, 'database'))

    def test_macos_scratch_folders_are_skipped(self):
        fh = self.fh
        fh.file('$TMPDIR/com.apple.foo/a', size=10, age_days=10)
        fh.file('$TMPDIR/TemporaryItems/a', size=10, age_days=10)
        fh.file('$CACHEDIR/com.apple.bar/a', size=10, age_days=10)
        sid = self.scan()
        self.assertEqual(self.items(sid), {})
        skipped = {e['path'] for e in self.store.scan_errors(sid) if e['error'] == 'skipped_privacy'}
        self.assertEqual(skipped, {'$TMPDIR/com.apple.foo', '$TMPDIR/TemporaryItems', '$CACHEDIR/com.apple.bar'})

    def test_user_cache_dir_folders(self):
        fh = self.fh
        fh.file('$CACHEDIR/com.foo.cache/blob', size=10, age_days=10)
        fh.set_age('$CACHEDIR/com.foo.cache', 10)
        fh.file('$CACHEDIR/com.bar.cache/blob', size=10, age_days=1)
        fh.set_age('$CACHEDIR/com.bar.cache', 1)
        sid = self.scan()
        item = self.items(sid)['$CACHEDIR/com.foo.cache']
        self.assertEqual((item.rule_id, item.tier, item.kind, item.unit),
                         ('app.user-cache-dir', Tier.JUNK, Kind.CACHE, Unit.BUNDLE))
        states = self.states(sid, 'app.user-cache-dir')
        self.assertEqual(states['$CACHEDIR/com.foo.cache'], DecisionState.SELECTED)
        self.assertEqual(states['$CACHEDIR/com.bar.cache'], DecisionState.TOO_RECENT_FLOOR)


class UserFileTests(Base):
    def scan_user(self, mac=None, **settings):
        return self.scan(domains=('user-files',), mac=mac, **settings)

    def test_installers_and_apps_in_downloads(self):
        fh = self.fh
        fh.file('~/Downloads/tool.dmg', size=100, age_days=90)
        fh.file('~/Downloads/sub/setup.PKG', size=100, age_days=90)
        fh.file('~/Downloads/notes.txt', size=100, age_days=90)
        fh.file('~/Downloads/Foo.app/Contents/MacOS/foo', size=100, age_days=90)
        self.age_tree('~/Downloads/Foo.app', 90)
        mac = FakeMacOS(last_used_map={str(fh.path('~/Downloads/sub/setup.PKG')): fh.clock() - 3 * DAY})
        sid = self.scan_user(mac=mac)
        items = self.items(sid)
        dmg = items['~/Downloads/tool.dmg']
        self.assertEqual(self.seen(dmg), ('user.installers', Tier.REVIEW, 'user.installers:~/Downloads'))
        self.assertEqual(dmg.kind, Kind.INSTALLER)
        foo = items['~/Downloads/Foo.app']
        self.assertEqual((foo.rule_id, foo.unit, foo.tier, foo.kind), ('user.download-apps', Unit.BUNDLE, Tier.REVIEW,
                                                                       Kind.INSTALLER))
        self.assertNotIn('~/Downloads/notes.txt', items)
        for rule_id in ('user.installers', 'user.download-apps'):
            r = self.catalog.rule(rule_id)
            self.assertEqual((r.actions, r.default_action, r.default_min_age_days, r.age_basis),
                             ((Action.TRASH, Action.DELETE), Action.TRASH, 30, AgeBasis.LAST_USED), rule_id)
        # Spotlight says the installer was used 3 days ago: the 30-day default keeps it out.
        states = self.states(sid, 'user.installers:~/Downloads', 'user.download-apps:~/Downloads')
        self.assertEqual(states['~/Downloads/tool.dmg'], DecisionState.SELECTED)
        self.assertEqual(states['~/Downloads/sub/setup.PKG'], DecisionState.TOO_RECENT)
        self.assertEqual(states['~/Downloads/Foo.app'], DecisionState.SELECTED)
        (call,) = [c for c in self.mac.calls if c[0] == 'last_used']
        self.assertIn(str(fh.path('~/Downloads/tool.dmg')), call[1][0])

    def test_installer_age_follows_the_setting(self):
        self.scan_user(installer_age_days=7)
        self.assertEqual(self.catalog.rule('user.installers').default_min_age_days, 7)

    def test_packages_are_protected_whole(self):
        fh = self.fh
        fh.file('~/Documents/Photos.photoslibrary/database/a.bin', size=100, age_days=400)
        fh.file('~/Documents/Photos.photoslibrary/originals/b.jpg', size=100, age_days=400)
        fh.file('~/Documents/Tool.app/Contents/MacOS/tool', size=100, age_days=400)
        fh.file('~/Documents/Proj.xcarchive/Info.plist', size=100, age_days=400)
        fh.file('~/Downloads/Old.xcarchive/Info.plist', size=100, age_days=400)
        fh.file('~/Movies/Edit.fcpbundle/x', size=100, age_days=400)
        for p in ('~/Documents/Photos.photoslibrary', '~/Documents/Tool.app', '~/Documents/Proj.xcarchive',
                  '~/Downloads/Old.xcarchive', '~/Movies/Edit.fcpbundle'):
            fh.set_age(p, 400)
        items = self.items(self.scan_user())
        for p in ('~/Documents/Photos.photoslibrary', '~/Documents/Tool.app', '~/Documents/Proj.xcarchive',
                  '~/Downloads/Old.xcarchive', '~/Movies/Edit.fcpbundle'):
            with self.subTest(path=p):
                self.assertEqual((items[p].rule_id, items[p].tier, items[p].unit), ('user.packages', Tier.PROTECTED,
                                                                                    Unit.BUNDLE))
        self.assertEqual(sum(i.files for i in items.values() if i.rule_id == 'user.packages'), 6)
        self.assertEqual([p for p in items if p.startswith('~/Documents/Photos.photoslibrary/')], [])
        self.assertEqual(self.catalog.rule('user.packages').patterns.count('~/Downloads/**/*.app'), 0)

    def test_large_and_idle_files_use_last_used_end_to_end(self):
        fh = self.fh
        fh.file('~/Documents/big-new.bin', size=600 * MIB, age_days=400)
        fh.file('~/Documents/big-old.bin', size=600 * MIB, age_days=400)
        fh.file('~/Documents/sub/idle-a.mov', size=60 * MIB, age_days=400)
        fh.file('~/Documents/sub/idle-b.mov', size=60 * MIB, age_days=400)
        fh.file('~/Documents/small.txt', size=100, age_days=400)
        used = {str(fh.path('~/Documents/big-new.bin')): fh.clock() - 5 * DAY,
                str(fh.path('~/Documents/big-old.bin')): fh.clock() - 300 * DAY,
                str(fh.path('~/Documents/sub/idle-b.mov')): fh.clock() - 20 * DAY}
        sid = self.scan_user(mac=FakeMacOS(last_used_map=used))
        items = self.items(sid)
        self.assertEqual(self.seen(items['~/Documents/big-new.bin']),
                         ('user.large', Tier.REVIEW, 'user.large:~/Documents'))
        self.assertEqual(self.seen(items['~/Documents/sub/idle-a.mov']),
                         ('user.idle', Tier.REVIEW, 'user.idle:~/Documents'))
        self.assertEqual(items['~/Documents/big-new.bin'].last_used_ns, int(used[str(fh.path('~/Documents/big-new.bin'))] * 1e9))
        self.assertIsNone(items['~/Documents/sub/idle-a.mov'].last_used_ns)
        self.assertNotIn('~/Documents/small.txt', items)
        self.assertEqual(self.catalog.rule('user.idle').default_min_age_days, 180)
        self.assertEqual(self.catalog.rule('user.idle').min_size, 50 * MIB)
        self.assertEqual(self.catalog.rule('user.large').min_size, 500 * MIB)
        self.assertGreater(self.catalog.rule('user.large').priority, self.catalog.rule('user.idle').priority)
        states = self.states(sid, 'user.large:~/Documents', 'user.idle:~/Documents', filter_days=30)
        self.assertEqual(states['~/Documents/big-new.bin'], DecisionState.TOO_RECENT)    # used 5 days ago
        self.assertEqual(states['~/Documents/big-old.bin'], DecisionState.SELECTED)
        self.assertEqual(states['~/Documents/sub/idle-a.mov'], DecisionState.SELECTED)   # idle: 400 days
        self.assertEqual(states['~/Documents/sub/idle-b.mov'], DecisionState.TOO_RECENT)   # used 20 days ago < 180
        self.assertEqual(len([c for c in self.mac.calls if c[0] == 'last_used']), 1)

    def test_size_limits_follow_the_settings(self):
        self.fh.file('~/Documents/mid.bin', size=20 * MIB, age_days=400)
        sid = self.scan_user(idle_file_min_bytes=10 * MIB, large_file_min_bytes=15 * MIB)
        self.assertEqual(self.items(sid)['~/Documents/mid.bin'].rule_id, 'user.large')
        self.assertEqual(self.catalog.rule('user.large').min_size, 15 * MIB)

    def test_unmatched_files_are_listed_as_other_files_and_never_offered(self):
        fh = self.fh
        fh.file('~/Documents/Taxes/2025/return.pdf', size=100, age_days=400)
        fh.file('~/Documents/Taxes/readme.txt', size=100, age_days=400)
        fh.file('~/Documents/loose.txt', size=100, age_days=400)
        sid = self.scan_user()
        groups = {g.key: g for g in self.store.groups(sid)}
        taxes, loose = groups['user.other:~/Documents/Taxes'], groups['user.other:~/Documents']
        self.assertEqual((taxes.tier, taxes.domain, taxes.actions, taxes.items), (Tier.PROTECTED, Domain.USER_FILES, (), 1))
        self.assertEqual(taxes.label_args, {'path': '~/Documents/Taxes'})
        self.assertEqual(loose.label_args, {'path': '~/Documents'})
        self.assertFalse([g for g in groups.values() if g.rule_id == 'unknown'])
        self.assertEqual(i18n.t('rule.user.other.label', 'en', **taxes.label_args), 'Other files in ~/Documents/Taxes')
        self.assertEqual(i18n.t('rule.user.other.label', 'zh-CN', **taxes.label_args), '~/Documents/Taxes 中的其他文件')

    def test_roots_come_from_settings_and_are_validated(self):
        cat = load_catalog(Settings(user_roots=('~/Work', '~/Work', '~/Downloads/x')))
        self.assertEqual([r.path for r in cat.roots if r.domain == Domain.USER_FILES], ['~/Downloads/x', '~/Work'])
        self.assertEqual(cat.rule('user.installers').patterns, ('~/Downloads/x/**',))
        self.assertIn('~/Work/**/*.app', cat.rule('user.packages').patterns)
        self.assertNotIn('~/Downloads/x/**/*.app', cat.rule('user.packages').patterns)
        for bad in (('~',), ('$TMPDIR/x',), ('$CACHEDIR/y',)):
            with self.subTest(roots=bad):
                with self.assertRaises(StewardError) as cm:
                    load_catalog(Settings(user_roots=bad))
                self.assertEqual((cm.exception.code, cm.exception.message_key, cm.exception.details),
                                 (ErrorCode.INVALID, 'error.invalid_setting', {'field': 'user_roots'}))
        none = load_catalog(Settings(user_roots=()))
        self.assertEqual([r for r in none.rules if r.id.startswith('user.')], [])
        no_downloads = load_catalog(Settings(user_roots=('~/Documents',)))
        self.assertEqual({r.id for r in no_downloads.rules if r.id.startswith('user.')},
                         {'user.packages', 'user.project-deps', 'user.large', 'user.idle', 'user.other'})

    def test_cloud_synced_roots(self):
        fh = self.fh
        roots = ('~/Desktop', '~/Documents', '~/Downloads', '~/Movies')
        self.assertEqual(user_files.cloud_synced_roots(fh.bases, roots), {})
        fh.dir('~/Library/Mobile Documents/com~apple~CloudDocs/Documents')
        self.assertEqual(user_files.cloud_synced_roots(fh.bases, roots), {'~/Documents': 'icloud_desktop_documents'})
        fh.dir('~/Library/CloudStorage/Dropbox/stuff')
        fh.symlink('~/Movies', '~/Library/CloudStorage/Dropbox/stuff')   # a root that redirects into a cloud folder
        self.assertEqual(user_files.cloud_synced_roots(fh.bases, roots + ('~/Library/CloudStorage/Dropbox/stuff',)),
                         {'~/Documents': 'icloud_desktop_documents', '~/Movies': 'cloud_folder',
                          '~/Library/CloudStorage/Dropbox/stuff': 'cloud_folder'})
        fh.dir('~/Library/Mobile Documents/com~apple~CloudDocs/Desktop')
        self.assertEqual(user_files.cloud_synced_roots(fh.bases, ('~/Desktop/Sub', '~/Downloads')),
                         {'~/Desktop/Sub': 'icloud_desktop_documents'})


class ProjectDepsTests(Base):
    def project(self, name='proj', deps='node_modules', age=100, marker=True, files=('src/a.js',)):
        fh = self.fh
        base = f'~/Documents/{name}'
        if marker:
            fh.file(f'{base}/package.json', content='{}', age_days=age)
        for f in files:
            fh.file(f'{base}/{f}', size=10, age_days=age)
        fh.file(f'{base}/{deps}/pkg/index.js', size=100, age_days=age)
        if 'src/a.js' in files:
            fh.set_age(f'{base}/src', age)
        fh.set_age(f'{base}/{deps}', age)
        return base

    def scan_deps(self):
        return self.scan(domains=('user-files',))

    def test_idle_project_is_reviewable_and_active_one_is_protected(self):
        self.project('idle', age=100)
        self.project('busy', age=5)
        items = self.items(self.scan_deps())
        idle, busy = items['~/Documents/idle/node_modules'], items['~/Documents/busy/node_modules']
        self.assertEqual((idle.rule_id, idle.tier, idle.kind, idle.unit, idle.group_key),
                         ('user.project-deps', Tier.REVIEW, Kind.PROJECT_DEPS, Unit.BUNDLE,
                          'user.project-deps:~/Documents'))
        self.assertEqual([e.key for e in idle.evidence], ['evidence.project_idle'])
        self.assertAlmostEqual(idle.evidence[0].args['days'], 100, delta=1)
        self.assertEqual((busy.tier, busy.group_key), (Tier.PROTECTED, 'user.project-deps#protected:~/Documents'))
        self.assertEqual([e.key for e in busy.evidence], ['evidence.project_active'])
        self.assertAlmostEqual(busy.evidence[0].args['days'], 5, delta=1)
        r = self.catalog.rule('user.project-deps')
        self.assertEqual((r.actions, r.default_action), ((Action.DELETE, Action.TRASH), Action.DELETE))

    def test_idle_threshold_follows_the_setting(self):
        self.project('p', age=100)
        sid = self.scan(domains=('user-files',), project_idle_days=200)
        self.assertEqual(self.items(sid)['~/Documents/p/node_modules'].tier, Tier.PROTECTED)

    def test_the_dependency_folder_itself_does_not_count_as_activity(self):
        self.project('p', age=100)
        self.fh.file('~/Documents/p/node_modules/.package-lock.json', content='{}', age_days=0)
        self.assertEqual(self.items(self.scan_deps())['~/Documents/p/node_modules'].tier, Tier.REVIEW)

    def test_a_new_file_beside_it_is_activity(self):
        self.project('p', age=100)
        self.fh.file('~/Documents/p/notes.md', size=10, age_days=1)
        item = self.items(self.scan_deps())['~/Documents/p/node_modules']
        self.assertEqual((item.tier, item.evidence[0].key), (Tier.PROTECTED, 'evidence.project_active'))

    def test_git_activity_counts(self):
        self.project('g', age=100)
        self.fh.file('~/Documents/g/.git/HEAD', content='ref: refs/heads/main', age_days=100)
        self.fh.file('~/Documents/g/.git/index', size=10, age_days=2)
        self.fh.set_age('~/Documents/g/.git', 100)
        item = self.items(self.scan_deps())['~/Documents/g/node_modules']
        self.assertEqual((item.tier, item.evidence[0].key), (Tier.PROTECTED, 'evidence.project_active'))

    def test_deps_inside_a_git_work_tree_are_still_found(self):
        fh = self.fh
        self.project('repo', age=100)
        fh.file('~/Documents/repo/.git/HEAD', content='ref: refs/heads/main', age_days=100)
        fh.set_age('~/Documents/repo/.git', 100)
        sid = self.scan_deps()
        items = self.items(sid)
        deps = items['~/Documents/repo/node_modules']
        self.assertEqual((deps.rule_id, deps.tier), ('user.project-deps', Tier.REVIEW))
        self.assertEqual(items['~/Documents/repo/.git'].rule_id, 'prot.git')
        self.assertNotIn('~/Documents/repo/src/a.js', items)   # protected and small: rolled up
        tree = [i for i in items.values() if i.rule_id == 'prot.git-tree']
        self.assertEqual((sum(i.files for i in tree), {i.tier for i in tree}), (2, {Tier.PROTECTED}))   # package.json, a.js

    def test_nested_dependency_folders_are_not_matched_separately(self):
        fh = self.fh
        self.project('p', age=100)
        fh.file('~/Documents/p/node_modules/pkg/package.json', content='{}', age_days=100)
        fh.file('~/Documents/p/node_modules/pkg/node_modules/inner/x.js', size=10, age_days=100)
        items = self.items(self.scan_deps())
        self.assertEqual([p for p, i in items.items() if i.rule_id == 'user.project-deps'],
                         ['~/Documents/p/node_modules'])

    def test_nested_inside_a_folder_that_is_not_itself_a_dependency_folder(self):
        fh = self.fh
        fh.file('~/Documents/loose/node_modules/pkg/package.json', content='{}', age_days=100)
        fh.file('~/Documents/loose/node_modules/pkg/node_modules/inner/x.js', size=10, age_days=100)
        items = self.items(self.scan_deps())
        self.assertEqual([p for p, i in items.items() if i.rule_id == 'user.project-deps'], [])

    def test_markers_decide_what_counts(self):
        fh = self.fh
        fh.file('~/Documents/nopkg/node_modules/a.js', size=10, age_days=100)                    # no package.json
        fh.file('~/Documents/novenv/.venv/bin/python', size=10, age_days=100)                   # no pyvenv.cfg
        fh.file('~/Documents/okvenv/.venv/pyvenv.cfg', content='home = /usr/bin', age_days=100)
        fh.file('~/Documents/okvenv2/venv/pyvenv.cfg', content='home = /usr/bin', age_days=100)
        fh.file('~/Documents/notarget/target/debug/x', size=10, age_days=100)                   # no Cargo.toml
        fh.file('~/Documents/crate/Cargo.toml', content='[package]', age_days=100)
        fh.file('~/Documents/crate/target/debug/x', size=10, age_days=100)
        fh.file('~/Documents/venvdir/pyvenv.cfg', content='home = /usr/bin', age_days=100)       # beside, not inside
        fh.file('~/Documents/venvdir/venv/lib/x', size=10, age_days=100)
        for venv in ('okvenv/.venv', 'okvenv2/venv'):   # nothing but the virtualenv: judged by the folder's own age
            fh.set_age(f'~/Documents/{venv}', 100)
            fh.set_age(f'~/Documents/{venv.partition("/")[0]}', 100)
        items = self.items(self.scan_deps())
        found = sorted(p for p, i in items.items() if i.rule_id == 'user.project-deps')
        self.assertEqual(found, ['~/Documents/crate/target', '~/Documents/okvenv/.venv', '~/Documents/okvenv2/venv'])
        self.assertTrue(all(items[p].tier == Tier.REVIEW for p in found))

    def test_a_virtualenv_alone_in_its_folder_is_judged_by_the_folder(self):
        fh = self.fh
        fh.file('~/Documents/solo/.venv/pyvenv.cfg', content='home = /usr/bin', age_days=100)
        fh.set_age('~/Documents/solo/.venv', 100)
        fh.set_age('~/Documents/solo', 100)
        item = self.items(self.scan_deps())['~/Documents/solo/.venv']
        self.assertEqual((item.tier, item.evidence[0].key), (Tier.REVIEW, 'evidence.project_idle'))

    def test_deps_are_not_reported_in_a_nested_root_twice(self):
        self.project('p', age=100)
        sid = self.scan(domains=('user-files',), user_roots=('~/Documents', '~/Documents/p'))
        self.assertEqual(len([i for i in self.store.items(sid) if i.rule_id == 'user.project-deps']), 1)

    def test_facts_are_stable_and_json_safe(self):
        facts = REGISTRY['project_deps'].prepare(DetectorContext(self.fh.bases, FakeMacOS(), Settings()))
        self.assertEqual(facts, {'home': str(self.fh.home), 'idle_days': 60})
        self.assertEqual(json.loads(json.dumps(facts)), facts)

    def test_detector_reports_a_vanished_project_as_skipped(self):
        facts = {'home': str(self.fh.home), 'idle_days': 60}
        ref = REGISTRY['project_deps'].refine(CAT.rule('user.project-deps'), '~/Documents/gone/node_modules', STAT,
                                              True, facts)
        self.assertTrue(ref.skip)


class DevSharedTests(Base):
    EXPECTED_HINTS = {
        'dev.homebrew': 'brew cleanup --prune=all', 'dev.uv': 'uv cache prune', 'dev.pip': 'pip cache purge',
        'dev.npm': 'npm cache clean --force',
        'dev.pnpm': 'pnpm store prune', 'dev.yarn': 'yarn cache clean', 'dev.go-mod': 'go clean -modcache',
        'dev.go-build': 'go clean -cache',
        'dev.playwright': 'npx playwright uninstall --all',
        'dev.coresimulator': 'xcrun simctl delete unavailable  # with Xcode selected: sudo xcode-select -s '
                             '/Applications/Xcode.app',
        'dev.colima': 'colima prune'}

    def test_every_rule_is_report_only(self):
        rules = [r for r in CAT.rules if r.domain == Domain.DEV_SHARED]
        self.assertEqual(len(rules), len(dev_shared._TOOLS))
        for r in rules:
            with self.subTest(rule=r.id):
                self.assertEqual((r.actions, r.default_action, r.tier, r.unit, r.group_depth),
                                 ((), None, Tier.REVIEW, Unit.BUNDLE, None))
                self.assertIn(r.kind, (Kind.CACHE, Kind.RUNTIME))
        self.assertEqual({r.id: r.hint for r in rules if r.hint}, self.EXPECTED_HINTS)
        # a tool with no command of its own offers Move to Trash for one listed folder instead; nobody is told to rm
        self.assertEqual({r.id for r in rules if r.manual_actions == (Action.TRASH,)}, {r.id for r in rules if not r.hint})
        self.assertFalse(any('rm ' in (r.hint or '') for r in rules))

    def test_every_tool_folder_the_brief_names_is_covered(self):
        patterns = {p for r in CAT.rules if r.domain == Domain.DEV_SHARED for p in r.patterns}
        for p in ('~/Library/Caches/Homebrew', '~/.npm/_cacache', '~/.npm/_npx', '~/Library/pnpm/store',
                  '~/Library/Caches/pnpm', '~/Library/Caches/Yarn', '~/Library/Caches/go-build', '~/Library/Caches/pip',
                  '~/Library/Caches/ms-playwright', '~/Library/Caches/node-gyp', '~/Library/Caches/puccinialin',
                  '~/Library/Developer/CoreSimulator/Devices', '~/Library/Developer/Xcode/DerivedData',
                  '~/Library/Developer/Xcode/iOS DeviceSupport'):
            self.assertIn(p, patterns)
        # a tool whose own folder is a scan root is matched one level down, since a root is never classified itself
        roots = {r.path for r in CAT.roots if r.domain == Domain.DEV_SHARED}
        self.assertEqual(roots, {'~/.cache/uv', '~/.cache/pip', '~/.cache/puppeteer', '~/.npm', '~/Library/pnpm',
                                 '~/.pnpm-store', '~/go/pkg/mod', '~/.cargo/registry', '~/.gradle/caches',
                                 '~/.m2/repository', '~/.colima', '~/Library/Developer'})
        self.assertEqual(patterns & roots, set())

    def build(self):
        fh = self.fh
        fh.file('~/.cache/uv/archive-v0/a.whl', size=100, age_days=30)
        fh.file('~/.cache/uv/wheels-v5/b.whl', size=100, age_days=30)
        fh.file('~/go/pkg/mod/github.com/x/y@v1/z.go', size=100, age_days=30)
        fh.file('~/.npm/_cacache/content-v2/a', size=100, age_days=30)
        fh.file('~/.npm/_npx/abc/index.js', size=100, age_days=30)
        fh.file('~/Library/Caches/Homebrew/downloads/bottle.tar.gz', size=100, age_days=30)
        fh.file('~/Library/Caches/ms-playwright/chromium-1/chrome', size=100, age_days=30)
        fh.file('~/Library/Developer/CoreSimulator/Devices/D1/data/a.plist', size=100, age_days=30)
        fh.file('~/Library/Developer/CoreSimulator/Devices/D1/data/Library/db.sqlite', size=100, age_days=30)
        fh.file('~/Library/Developer/Xcode/DerivedData/App-abc/Build/x', size=100, age_days=30)
        fh.file('~/Library/Developer/Xcode/Archives/2026/keep.xcarchive/Info.plist', size=100, age_days=30)

    def test_one_group_per_tool_with_its_command(self):
        self.build()
        sid = self.scan(domains=('dev-shared',))
        groups = {g.rule_id: g for g in self.store.groups(sid) if g.rule_id.startswith('dev.')}
        self.assertEqual(sorted(groups), ['dev.coresimulator', 'dev.go-mod', 'dev.npm', 'dev.npx', 'dev.uv',
                                          'dev.xcode-derived-data'])   # Homebrew and Playwright live in app-library's roots
        uv = groups['dev.uv']
        self.assertEqual((uv.key, uv.items, uv.tier, uv.domain, uv.actions, uv.default_action, uv.hint),
                         ('dev.uv', 2, Tier.REVIEW, Domain.DEV_SHARED, (), None, 'uv cache prune'))
        self.assertIsNone(groups['dev.xcode-derived-data'].hint)
        sim = self.items(sid)['~/Library/Developer/CoreSimulator/Devices']
        self.assertEqual((sim.tier, sim.taint, sim.kind), (Tier.REVIEW, '', Kind.RUNTIME))   # its sqlite files taint nothing
        spec = Spec(scan_id=sid, groups={'dev.uv': GroupSel()})
        res = selection.resolve(self.store, spec, self.fh.clock(), self.settings, self.mac)
        states = {i.path: res.decisions[i.key] for i in self.store.items(sid) if i.rule_id == 'dev.uv'}
        for decision in states.values():
            self.assertEqual((decision.state, decision.reason_key), (DecisionState.UNSUPPORTED_ACTION,
                                                                      'reason.report_only'))
        self.assertEqual(res.summary['count'], 0)

    def test_names_that_look_like_databases_or_credentials_do_not_hide_a_report(self):
        fh = self.fh
        fh.file('~/.cache/uv/archive-v0/x/cache.sqlite', size=100, age_days=30)
        fh.file('~/.cache/uv/environments-v2/client_secret.json', size=100, age_days=30)
        fh.file('~/.cache/uv/sdists-v9/pkg/.git/HEAD', size=100, age_days=30)
        sid = self.scan(domains=('dev-shared',))
        items = {i.path: i for i in self.store.items(sid)}
        self.assertEqual(sorted(items), ['~/.cache/uv/archive-v0', '~/.cache/uv/environments-v2',
                                         '~/.cache/uv/sdists-v9'])
        for item in items.values():
            self.assertEqual((item.rule_id, item.tier, item.taint, item.evidence), ('dev.uv', Tier.REVIEW, '', ()))
        (group,) = self.store.groups(sid)
        self.assertEqual((group.key, group.tier), ('dev.uv', Tier.REVIEW))

    def test_folders_in_library_caches_show_as_shared_caches_even_with_the_domain_off(self):
        self.build()
        sid = self.scan(domains=('app-library',))
        items = self.items(sid)
        for path, rule_id in (('~/Library/Caches/Homebrew', 'dev.homebrew'),
                              ('~/Library/Caches/ms-playwright', 'dev.playwright')):
            with self.subTest(path=path):
                self.assertEqual((items[path].rule_id, items[path].domain, items[path].unit),
                                 (rule_id, Domain.DEV_SHARED, Unit.BUNDLE))
        self.assertFalse([i for i in items.values() if i.rule_id == 'app.caches'])
        self.assertNotIn('~/.cache/uv/archive-v0', items)   # outside the app-library roots
        states = self.states(sid, 'dev.homebrew')
        self.assertEqual(states['~/Library/Caches/Homebrew'], DecisionState.DOMAIN_OFF)
        self.assertTrue(CAT.rule('dev.homebrew').priority > CAT.rule('app.caches').priority)

    def test_dev_roots_are_only_walked_when_the_domain_is_on(self):
        self.build()
        sid = self.scan(domains=('app-library', 'dev-shared'))
        roots = {r['path']: r['status'] for r in self.store.scan_roots(sid)}
        self.assertEqual(roots['~/.cache/uv'], 'ok')
        self.assertEqual(roots['~/Library/Developer'], 'ok')
        self.assertEqual(roots['~/.colima'], 'missing')
        sid = self.scan(domains=('app-library',))
        self.assertNotIn('~/.cache/uv', {r['path'] for r in self.store.scan_roots(sid)})


class CatalogShapeTests(unittest.TestCase):
    def test_general_rules_sit_below_the_agent_specific_ones(self):
        agent_floor = min(r.priority for r in CAT.rules if r.domain == Domain.AGENT and r.tier != Tier.PROTECTED)
        for r in CAT.rules:
            if r.id.startswith(('app.', 'user.', 'dev.')):
                self.assertLess(r.priority, 900 if r.tier == Tier.PROTECTED else agent_floor, r.id)

    def test_new_detectors_are_registered_and_used(self):
        used = {r.detector for r in CAT.rules if r.detector}
        for name in ('electron_apps', 'orphans', 'project_deps'):
            self.assertIn(name, REGISTRY)
            self.assertIn(name, used)

    def test_each_new_text_exists_and_fills_in_both_locales(self):
        samples = {'evidence.requires_quit': {'bundle_id': 'com.x.y', 'app': 'X'},
                   'evidence.app_not_installed': {'id': 'com.x.y'},
                   'evidence.project_idle': {'days': 90}, 'evidence.project_active': {'days': 3},
                   'evidence.project_unreadable': {'reason': 'EACCES'}}
        for loc in LOCALES:
            for key, args in samples.items():
                with self.subTest(locale=loc, key=key):
                    text = i18n.t(key, loc, **args)
                    self.assertNotEqual(text, key)
                    for value in args.values():
                        self.assertIn(str(value), text)
            for provider in ('macos', 'vscode', 'user', 'devtools'):
                self.assertNotEqual(i18n.t(f'provider.{provider}', loc), f'provider.{provider}')


if __name__ == '__main__':
    unittest.main()
