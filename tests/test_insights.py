"""Insights.build through App.insights: environment, KPIs, composition, history, growth, agents and every TODO rule."""
from __future__ import annotations

import json
import os
import re
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs

from disk_steward import i18n
from disk_steward import schedule as sched
from disk_steward.model import Action, EntryState, Evidence, Outcome
from disk_steward.portal import api as api_mod
from disk_steward.report import group_effect
from disk_steward.service import App
from disk_steward.settings import Settings
from insights_support import GIB, MIB, TOTAL, Synth, todo, todo_ids
from m3_support import DAY, NOW, make_app, make_mac
from support import Clock, FakeHome
from test_store import make_entry, make_plan

HERE = Path(__file__).resolve().parent
BROWSE_JS = HERE.parent / 'src' / 'disk_steward' / 'portal' / 'static' / 'views' / 'browse.mjs'


def tearDownModule():
    if i18n.MISSING:
        raise AssertionError(f'untranslated keys: {sorted(i18n.MISSING)}')


class InsightsCase(unittest.TestCase):
    domains = ('agent', 'app-library')

    def setUp(self):
        self.clock = Clock(NOW)
        self.fh = FakeHome(self.clock).__enter__()
        self.addCleanup(self.fh.cleanup)
        self.mac = make_mac(tool_versions={'codex': 'codex-cli 0.160.0', 'claude': '2.1.299 (Claude Code)'},
                            snapshot_names=['com.apple.TimeMachine.1', 'com.apple.TimeMachine.2'])
        self.app = make_app(self.fh, mac=self.mac, settings=Settings(domains=self.domains))
        self.addCleanup(self.app.close)
        self.synth = Synth(self.app, self.clock)
        agents = self.fh.root / 'LaunchAgents'
        self.bin = self.fh.root / 'bin'
        self.bin.mkdir()
        for patch in (mock.patch.object(sched, 'LAUNCH_AGENTS_DIR', agents),
                      mock.patch.object(sched, 'is_loaded', return_value=False),
                      mock.patch.dict(os.environ, {'PATH': str(self.bin)})):   # no real tool is ever asked anything
            patch.start()
            self.addCleanup(patch.stop)

    def build(self, window=30, locale='en'):
        return self.app.insights(window, locale)

    def cleanup(self, plan_id, entries, measured=None, estimated=None, at_days_ago=1.0):
        """A finished plan: `entries` are (state, action, alloc, freed); `measured` and `estimated` fill the run
        summary the service keeps in meta."""
        store = self.app.store
        scan = store.latest_scan() or {'id': self.synth.scan(30, {'codex.logs': 1})}
        self.clock.set(NOW - at_days_ago * DAY)
        built = [make_entry(plan_id, i + 1, action=Action(a), alloc=alloc, reclaim=alloc)
                 for i, (_s, a, alloc, _f) in enumerate(entries)]
        store.save_plan(make_plan(plan_id, scan_id=scan['id'], created_at=self.clock()), built)
        for seq, (state, _a, _alloc, freed) in enumerate(entries, 1):
            store.finish_entry(plan_id, seq, Outcome(EntryState(state), '', freed=freed))
        if measured is not None:
            store.set_meta(f'run:{plan_id}', json.dumps({'measured_delta': measured, 'estimated_delta': estimated}))
        self.clock.set(NOW)

    def add_archive(self, archive_id, days_old, size, state='stored'):
        store = self.app.store
        if not store.latest_scan():
            self.synth.scan(1, {'codex.logs': MIB})
        plan_id = 'pa' + archive_id
        store.save_plan(make_plan(plan_id, scan_id=store.latest_scan()['id']), [])
        store.add_archive({'id': archive_id, 'plan_id': plan_id, 'seq': 1, 'path': '~/x', 'fmt': 'gz', 'orig_size': 1,
                           'orig_alloc': 1, 'stored_bytes': size, 'sha_orig': 'o', 'sha_stored': 's', 'manifest': {},
                           'state': state, 'created_at': NOW - days_old * DAY})

    def reset(self):
        """A second, empty App for a test that checks several scenarios one after the other."""
        self.setUp()


class EnvironmentTests(InsightsCase):
    def test_everything_the_page_shows_about_this_mac(self):
        sid = self.synth.scan(1, {'codex.logs': (5 * MIB, 3), 'codex.state': (MIB, 2)})
        for code in ('skipped_privacy', 'skipped_privacy', 'permission_denied'):
            self.app.store.add_scan_error(sid, '~/x', code)
        env = self.build()['environment']
        self.assertEqual(set(env), {'macos', 'arch', 'python', 'tool_version', 'volume', 'snapshots', 'data_dir',
                                    'data_dir_bytes', 'clis', 'clis_errors', 'advisor_enabled', 'schedule', 'areas',
                                    'last_scan'})
        self.assertEqual(env['volume'], {'total': TOTAL, 'used': TOTAL - 300 * 10 ** 9, 'free': 300 * 10 ** 9})
        self.assertEqual(env['snapshots'], 2)
        self.assertEqual(env['data_dir'], str(self.fh.data_dir))        # outside ~, $TMPDIR and $CACHEDIR
        self.assertGreater(env['data_dir_bytes'], 0)
        self.assertEqual(env['clis'], {'codex': '0.160.0', 'claude': '2.1.299', 'ollama': None})
        self.assertEqual(env['clis_errors'], {})
        self.assertIs(env['advisor_enabled'], False)
        self.assertEqual(env['schedule'], {'installed': False, 'weekday': None, 'hour': None})
        self.assertEqual(env['areas'], ['agent', 'app-library'])
        last = env['last_scan']
        self.assertEqual((last['id'], last['items'], last['privacy_skipped'], last['errors']), (sid, 5, 2, 1))
        self.assertEqual(last['finished_at'], NOW - DAY)
        self.assertEqual(last['duration_s'], 0.0)

    def test_a_failing_tmutil_leaves_the_count_unknown_and_says_why(self):
        self.synth.scan(1, {'codex.logs': 1})
        self.mac.snapshots = mock.Mock(side_effect=RuntimeError('tmutil exploded'))
        env = self.build()['environment']
        self.assertIsNone(env['snapshots'])
        self.assertEqual(env['snapshots_error'], 'tmutil exploded')

    def test_no_error_key_while_tmutil_works(self):
        self.assertNotIn('snapshots_error', self.build()['environment'])

    def test_a_data_folder_under_home_has_a_logical_name(self):
        data = self.fh.home / 'Library' / 'Application Support' / 'Disk Steward'
        app = App(home=self.fh.home, data_dir=data, mac=self.mac, clock=self.clock, locale='en',
                  settings=Settings(domains=self.domains), bases=self.fh.bases)
        self.addCleanup(app.close)
        self.assertEqual(app.insights(30, 'en')['environment']['data_dir'], '~/Library/Application Support/Disk Steward')

    def test_tool_versions_are_asked_once_per_ten_minutes(self):
        def asked():
            return sum(1 for name, _ in self.mac.calls if name == 'tool_version')
        self.build()
        self.assertEqual(asked(), 3)
        self.clock.advance(seconds=599)
        self.build()
        self.assertEqual(asked(), 3)
        self.clock.advance(seconds=2)
        self.build()
        self.assertEqual(asked(), 6)

    def test_a_tool_that_cannot_report_its_version_is_shown_not_hidden(self):
        real = self.mac.tool_version

        def flaky(name):
            if name == 'ollama':
                raise RuntimeError('ollama --version exited with 1')
            return real(name)
        self.mac.tool_version = flaky
        env = self.build()['environment']
        self.assertIsNone(env['clis']['ollama'])
        self.assertEqual(env['clis_errors'], {'ollama': 'RuntimeError: ollama --version exited with 1'})
        self.assertEqual(env['clis']['codex'], '0.160.0')

    def test_an_installed_schedule_is_reported_and_needs_no_tip(self):
        plist = sched.build_plist(self.fh.data_dir, 3, 9, False, 'en')
        sched._write_plist(sched.plist_path(), plist)
        data = self.build()
        self.assertEqual(data['environment']['schedule'], {'installed': True, 'weekday': 3, 'hour': 9})
        self.assertNotIn('schedule', todo_ids(data))

    def test_largest_apps_appear_only_after_someone_measured_them_and_are_never_measured_here(self):
        self.synth.scan(1, {'codex.logs': 1})
        self.assertNotIn('largest_apps', self.build())
        self.assertFalse([c for c in self.mac.calls if c[0] == 'app_sizes'])
        apps = self.fh.root / 'Applications'
        for name in ('Big.app', 'Small.app'):
            (apps / name).mkdir(parents=True)
        self.mac.app_size_map = {str(apps / 'Big.app'): {'bytes': 3 * GIB}, str(apps / 'Small.app'): {'bytes': MIB}}
        with mock.patch('disk_steward.service.SYSTEM_APPLICATIONS', apps):
            self.app.app_sizes()
        calls = len([c for c in self.mac.calls if c[0] == 'app_sizes'])
        data = self.build()
        self.assertEqual(data['largest_apps'], [{'path': str(apps / 'Big.app'), 'bytes': 3 * GIB},
                                                {'path': str(apps / 'Small.app'), 'bytes': MIB}])
        self.assertEqual(len([c for c in self.mac.calls if c[0] == 'app_sizes']), calls)


class NoScanTests(InsightsCase):
    def test_a_mac_that_was_never_scanned_still_has_a_page(self):
        data = self.build()
        self.assertIsNone(data['environment']['last_scan'])
        self.assertEqual(data['kpis']['scanned'], {'value': None, 'delta': None, 'series': []})
        self.assertEqual(data['kpis']['safe_to_clean'], {'bytes': 0, 'items': 0, 'days': 14})
        self.assertIsNone(data['kpis']['growth_per_day'])
        self.assertEqual(data['history'], {'scans': [], 'cleanups': []})
        self.assertEqual(data['growth'], {'up': [], 'down': []})
        self.assertEqual(data['agents'], [])
        self.assertEqual(todo_ids(data), ['first_scan', 'schedule'])
        first = todo(data, 'first_scan')
        self.assertEqual((first['severity'], first['title'], first['action']['href']),
                         ('act', 'Run the first scan', '#/overview?scan=1'))
        comp = {c['key']: c['bytes'] for c in data['composition']}
        self.assertEqual(comp, {'free': 300 * 10 ** 9, 'outside': TOTAL - 300 * 10 ** 9, 'protected': 0, 'review': 0,
                                'report_only': 0, 'junk': 0})


class CompositionTests(InsightsCase):
    KEYS = ['free', 'outside', 'protected', 'review', 'report_only', 'junk']

    def groups(self):
        return {'codex.logs': 11 * GIB, 'app.chrome-cache': GIB, 'codex.runtimes': 7 * GIB, 'ollama.models': 3 * GIB,
                'dev.homebrew': GIB, 'codex.state': 5 * GIB, 'unknown:~/.claude/cache': GIB}

    def test_the_bars_add_up_to_the_volume(self):
        self.synth.scan(1, self.groups())
        comp = self.build()['composition']
        self.assertEqual([c['key'] for c in comp], self.KEYS)
        self.assertEqual(sum(c['bytes'] for c in comp), TOTAL)
        by = {c['key']: c['bytes'] for c in comp}
        self.assertEqual(by['junk'], 12 * GIB)
        self.assertEqual(by['review'], 7 * GIB)
        self.assertEqual(by['report_only'], 4 * GIB)
        self.assertEqual(by['protected'], 6 * GIB)
        self.assertEqual(by['free'], 300 * 10 ** 9)
        self.assertEqual(by['outside'], TOTAL - 300 * 10 ** 9 - 29 * GIB)
        self.assertEqual([c['label'] for c in comp],
                         ['Free space', 'Not scanned', 'Protected', 'Needs review', 'Shown only', 'Junk'])

    def test_labels_follow_the_locale(self):
        self.synth.scan(1, self.groups())
        self.assertEqual([c['label'] for c in self.build(locale='zh-CN')['composition']],
                         ['可用空间', '未扫描', '受保护', '待审阅', '仅展示', '垃圾'])

    def test_more_scanned_than_used_is_scaled_down_never_negative(self):
        # clones and sparse files count twice in a scan; the bars still have to add up
        self.synth.scan(1, {'codex.logs': 150 * 10 ** 9, 'codex.state': 100 * 10 ** 9, 'codex.runtimes': 3 * 10 ** 8})
        comp = {c['key']: c['bytes'] for c in self.build()['composition']}
        self.assertEqual(sum(comp.values()), TOTAL)
        self.assertEqual(comp['outside'], 0)
        self.assertTrue(all(v >= 0 for v in comp.values()))
        self.assertGreater(comp['junk'], comp['protected'])

    def test_it_describes_the_latest_scan_whatever_the_window(self):
        self.synth.scan(60, {'codex.logs': 50 * GIB})
        self.synth.scan(1, {'codex.logs': 2 * GIB})
        for window in (7, 0):
            by = {c['key']: c['bytes'] for c in self.build(window)['composition']}
            self.assertEqual(by['junk'], 2 * GIB)


class KpiTests(InsightsCase):
    def seed(self):
        self.synth.scan(40, {'codex.logs': 10 * GIB, 'codex.state': GIB}, free=100 * 10 ** 9)
        self.synth.scan(20, {'codex.logs': 8 * GIB, 'codex.state': GIB}, free=120 * 10 ** 9)
        self.synth.scan(10, {'codex.logs': 6 * GIB, 'codex.state': GIB}, free=140 * 10 ** 9)
        self.synth.scan(2, {'codex.logs': 4 * GIB, 'codex.state': GIB}, free=200 * 10 ** 9)
        self.mac.free = 260 * 10 ** 9

    def test_the_window_cuts_the_scans_that_count(self):
        self.seed()
        cases = {30: [20, 10, 2], 7: [2], 0: [40, 20, 10, 2], 1: []}
        for window, days in cases.items():
            with self.subTest(window=window):
                k = self.build(window)['kpis']
                times = [NOW - d * DAY for d in days]
                self.assertEqual([p['t'] for p in k['scanned']['series']], times)
                self.assertEqual([p['t'] for p in k['free']['series']], times + [NOW])   # plus "now"
                self.assertEqual(k['free']['value'], 260 * 10 ** 9)
                self.assertEqual(k['free']['series'][-1], {'t': NOW, 'v': 260 * 10 ** 9})

    def test_deltas_are_now_minus_the_first_point_inside_the_window(self):
        self.seed()
        k = self.build(30)['kpis']
        self.assertEqual(k['free']['delta'], 260 * 10 ** 9 - 120 * 10 ** 9)
        self.assertEqual([p['v'] for p in k['free']['series']], [120 * 10 ** 9, 140 * 10 ** 9, 200 * 10 ** 9,
                                                                  260 * 10 ** 9])
        self.assertEqual(k['scanned']['value'], 5 * GIB)
        self.assertEqual(k['scanned']['delta'], 5 * GIB - 9 * GIB)
        self.assertEqual([p['v'] for p in k['scanned']['series']], [9 * GIB, 7 * GIB, 5 * GIB])
        self.assertEqual(self.build(0)['kpis']['scanned']['delta'], 5 * GIB - 11 * GIB)

    def test_growth_per_day_needs_two_scans_and_spans_first_to_last(self):
        self.seed()
        self.assertEqual(self.build(30)['kpis']['growth_per_day'], round((5 * GIB - 9 * GIB) / 18))
        self.assertIsNone(self.build(7)['kpis']['growth_per_day'])     # one scan inside
        self.assertIsNone(self.build(1)['kpis']['growth_per_day'])     # none

    def test_a_window_without_scans_has_no_scanned_delta_but_still_a_value(self):
        self.seed()
        k = self.build(1)['kpis']
        self.assertEqual(k['scanned']['value'], 5 * GIB)
        self.assertIsNone(k['scanned']['delta'])
        self.assertEqual(k['free']['delta'], 0)                          # the only point is "now"

    def test_less_than_two_days_of_history_gives_no_daily_rate(self):
        # one cleanup or one big download inside a day must not read as a daily trend
        self.synth.scan(1.5, {'codex.logs': GIB})
        self.synth.scan(0.01, {'codex.logs': GIB})
        self.synth.scan(0.0, {'codex.logs': 2 * GIB})
        self.assertIsNone(self.build(30)['kpis']['growth_per_day'])

    def test_two_days_of_history_give_a_daily_rate_over_the_whole_span(self):
        self.synth.scan(3.0, {'codex.logs': GIB})
        self.synth.scan(0.0, {'codex.logs': 2 * GIB})
        self.assertEqual(self.build(30)['kpis']['growth_per_day'], round(GIB / 3.0))

    def test_freed_total_counts_every_cleanup_since_the_first(self):
        self.synth.scan(30, {'codex.logs': 1})
        self.cleanup('p1', [('done', 'delete', 100, 90), ('done', 'trash', 50, 0)], at_days_ago=20)
        self.cleanup('p2', [('done', 'archive', 200, 120), ('skipped', 'delete', 5, 0)], at_days_ago=5)
        self.cleanup('p3', [('skipped', 'delete', 5, 0)], at_days_ago=3)         # nothing done: not a cleanup
        k = self.build()['kpis']['freed_total']
        self.assertEqual(k, {'value': 210, 'count': 2, 'since': NOW - 20 * DAY})
        self.assertEqual(self.build(7)['kpis']['freed_total'], k)           # a total, whatever the window

    def test_no_cleanup_yet(self):
        self.synth.scan(1, {'codex.logs': 1})
        self.assertEqual(self.build()['kpis']['freed_total'], {'value': 0, 'count': 0, 'since': None})


class HistoryTests(InsightsCase):
    def test_scan_rows_carry_the_four_buckets_and_the_volume(self):
        self.synth.scan(5, {'codex.logs': 3 * GIB, 'codex.runtimes': 2 * GIB, 'ollama.models': GIB,
                            'codex.state': 4 * GIB}, free=111 * 10 ** 9)
        rows = self.build()['history']['scans']
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual({k: row[k] for k in ('t', 'used', 'free', 'scanned', 'junk', 'review', 'protected',
                                               'report_only', 'backfilled')},
                         {'t': NOW - 5 * DAY, 'used': TOTAL - 111 * 10 ** 9, 'free': 111 * 10 ** 9,
                          'scanned': 10 * GIB, 'junk': 3 * GIB, 'review': 2 * GIB, 'protected': 4 * GIB,
                          'report_only': GIB, 'backfilled': False})
        self.assertIsInstance(row['id'], int)

    def test_cleanups_inside_the_window_only_newest_first(self):
        self.synth.scan(30, {'codex.logs': 1})
        self.cleanup('old', [('done', 'delete', 10, 10)], at_days_ago=20, measured=5, estimated=10)
        self.cleanup('new', [('done', 'delete', 20, 20), ('failed', 'delete', 1, 0)], at_days_ago=2)
        rows = self.build(7)['history']['cleanups']
        self.assertEqual([r['plan_id'] for r in rows], ['new'])
        rows = self.build(30)['history']['cleanups']
        self.assertEqual([r['plan_id'] for r in rows], ['new', 'old'])
        self.assertEqual((rows[1]['measured_delta'], rows[1]['estimated']), (5, 10))
        self.assertEqual((rows[0]['items_failed'], rows[0]['measured_delta']), (1, None))
        self.assertEqual(set(rows[0]), {'kind', 'plan_id', 'created_at', 'finished_at', 'items_done', 'items_skipped',
                                        'items_failed', 'items_excluded', 'removed', 'freed', 'moved_to_trash',
                                        'measured_delta', 'estimated'})


class GrowthTests(InsightsCase):
    def test_up_and_down_from_the_first_scan_in_the_window_to_the_latest(self):
        self.synth.scan(20, {'codex.logs': 100 * MIB, 'app.chrome-cache': 3 * GIB, 'codex.runtimes': GIB,
                             'vscode.vsix': 500 * MIB, 'dev.homebrew': GIB, 'codex.state': 2 * GIB})
        self.synth.scan(0, {'codex.logs': 4100 * MIB, 'app.chrome-cache': GIB, 'codex.runtimes': GIB,
                            'vscode.vsix': 700 * MIB, 'codex.state': 2 * GIB, 'app.shipit:~/Library/Caches/x.ShipIt': 40 * MIB})
        g = self.build(30)['growth']
        self.assertEqual([r['group'] for r in g['up']], ['codex.logs', 'vscode.vsix',
                                                         'app.shipit:~/Library/Caches/x.ShipIt'])
        self.assertEqual([r['group'] for r in g['down']], ['app.chrome-cache', 'dev.homebrew'])
        logs = g['up'][0]
        self.assertEqual(logs, {'group': 'codex.logs', 'label': logs['label'], 'domain': 'agent', 'tier': 'junk',
                                'delta': 4000 * MIB, 'per_day': 200 * MIB, 'now': 4100 * MIB, 'before': 100 * MIB})
        self.assertEqual(g['up'][2]['before'], 0)                       # a group that did not exist before
        self.assertEqual(g['up'][2]['per_day'], round(40 * MIB / 20))
        gone = g['down'][1]                                              # a group that vanished
        self.assertEqual((gone['now'], gone['before'], gone['delta'], gone['domain'], gone['tier']),
                         (0, GIB, -GIB, 'dev-shared', 'review'))
        self.assertEqual(g['down'][0]['per_day'], -round(2 * GIB / 20))

    def test_labels_are_translated_with_the_groups_own_text(self):
        self.synth.scan(20, {'codex.logs': MIB})
        self.synth.scan(0, {'codex.logs': 3 * MIB, 'codex.sessions:~/.codex/sessions/2026/08': MIB})
        en = {r['group']: r['label'] for r in self.build(30, 'en')['growth']['up']}
        zh = {r['group']: r['label'] for r in self.build(30, 'zh-CN')['growth']['up']}
        self.assertEqual(en['codex.logs'], i18n.t('rule.codex.logs.label', 'en'))
        self.assertEqual(zh['codex.logs'], i18n.t('rule.codex.logs.label', 'zh-CN'))
        self.assertIn('2026/08', en['codex.sessions:~/.codex/sessions/2026/08'])
        self.assertNotEqual(en['codex.logs'], zh['codex.logs'])

    def test_the_lists_are_capped_at_eight_up_and_five_down(self):
        keys = [f'app.caches:~/Library/Caches/c{i:02d}' for i in range(20)]
        self.synth.scan(10, {k: (i + 1) * MIB if i < 12 else (i + 1) * 10 * MIB for i, k in enumerate(keys)})
        self.synth.scan(0, {k: (i + 2) * MIB if i < 12 else MIB for i, k in enumerate(keys)})
        g = self.build(30)['growth']
        self.assertEqual(len(g['up']), 8)
        self.assertEqual(len(g['down']), 5)
        self.assertEqual([r['delta'] for r in g['up']], [MIB] * 8)        # equal deltas: ordered by key
        self.assertEqual([r['group'] for r in g['up']], keys[:8])
        self.assertEqual([r['delta'] for r in g['down']], sorted(r['delta'] for r in g['down']))
        self.assertEqual(g['down'][0]['group'], keys[19])

    def test_the_window_picks_the_baseline(self):
        self.synth.scan(40, {'codex.logs': MIB})
        self.synth.scan(5, {'codex.logs': 10 * MIB})
        self.synth.scan(0, {'codex.logs': 12 * MIB})
        self.assertEqual(self.build(0)['growth']['up'][0]['delta'], 11 * MIB)
        up = self.build(30)['growth']['up'][0]
        self.assertEqual((up['before'], up['delta'], up['per_day']), (10 * MIB, 2 * MIB, round(2 * MIB / 5)))
        self.assertEqual(self.build(1)['growth'], {'up': [], 'down': []})   # a single scan has nothing to compare

    def test_a_span_under_a_day_does_not_extrapolate(self):
        self.synth.scan(0.02, {'codex.logs': MIB})
        self.synth.scan(0.0, {'codex.logs': 51 * MIB})
        up = self.build(30)['growth']['up'][0]
        self.assertEqual((up['delta'], up['per_day']), (50 * MIB, 50 * MIB))


class AgentTests(InsightsCase):
    def test_sizes_changes_and_attribution_by_rule_or_by_path(self):
        self.synth.scan(10, {'codex.logs': GIB, 'codex.runtimes': GIB, 'claude.projects:~/.claude/projects/x': 5 * MIB,
                             'ollama.models': 4 * GIB})
        self.synth.scan(0, {'codex.logs': 2 * GIB, 'codex.runtimes': GIB, 'claude.projects:~/.claude/projects/x': 9 * MIB,
                            'unknown:~/.claude/cache': 3 * MIB, 'unknown:~/.gemini/tmp': 2 * MIB,
                            'prot.databases:agent': 700 * MIB, 'app.caches:~/Library/Caches/foo': GIB,
                            'dev.homebrew': GIB, 'unknown:~/Library/Application Support/Codex': 5 * MIB})
        rows = self.build(30)['agents']
        self.assertEqual([(r['provider'], r['bytes'], r['delta']) for r in rows], [
            ('codex', 3 * GIB + 5 * MIB, GIB + 5 * MIB),
            ('claude', 12 * MIB, 12 * MIB - 5 * MIB),
            ('gemini', 2 * MIB, 2 * MIB)])
        self.assertEqual([r['label'] for r in rows], ['Codex', 'Claude', 'Gemini'])
        # ollama's models are gone now: no row for an agent without data
        self.assertNotIn('ollama', [r['provider'] for r in rows])

    def test_a_single_scan_has_no_change(self):
        self.synth.scan(1, {'codex.logs': GIB})
        self.assertEqual(self.build(30)['agents'], [{'provider': 'codex', 'label': 'Codex', 'bytes': GIB, 'delta': 0}])

    def test_labels_are_translated(self):
        self.synth.scan(1, {'codex.logs': GIB, 'cursor.logs': MIB})
        self.assertEqual([r['label'] for r in self.build(30, 'zh-CN')['agents']], ['Codex', 'Cursor'])


class TodoRuleTests(InsightsCase):
    def only(self, groups, **kw):
        self.synth.scan(1, groups, **kw)
        return self.build()

    # 1 ------------------------------------------------------------------------------------------------------
    def test_1_junk_that_qualifies_is_offered_as_one_action(self):
        data = self.only({'codex.logs': (5 * MIB, 3), 'codex.state': MIB})
        t = todo(data, 'clean_junk')
        self.assertEqual((t['severity'], t['title'], t['bytes'], t['command']),
                         ('act', 'Clean 5.00 MiB of rebuildable junk', 5 * MIB, None))
        self.assertEqual(t['action']['href'], '#/overview?quick=1')
        self.assertIn('3 items', t['detail'])
        self.assertIn('14 days', t['detail'])
        self.assertEqual(data['kpis']['safe_to_clean'], {'bytes': 5 * MIB, 'items': 3, 'days': 14})

    def test_1_nothing_qualifies_nothing_is_offered(self):
        for groups, age in (({'codex.logs': 5 * MIB}, 3.0),                          # too recent
                            ({'codex.runtimes': GIB, 'codex.state': GIB}, 90.0),     # review and protected
                            ({'dev.homebrew': GIB}, 90.0)):                          # report only
            with self.subTest(groups=list(groups)):
                self.reset()
                data = self.only(groups, age_days=age)
                self.assertNotIn('clean_junk', todo_ids(data))
                self.assertEqual(data['kpis']['safe_to_clean']['bytes'], 0)

    def test_1_the_text_follows_the_locale_and_the_age_filter(self):
        self.app.update_settings({'default_min_age_days': 0})
        data = self.only({'codex.logs': 5 * MIB})
        self.assertIn('of any age', todo(data, 'clean_junk')['detail'])
        zh = self.build(30, 'zh-CN')
        self.assertEqual(todo(zh, 'clean_junk')['title'], '清理 5.00 MiB 可重建的垃圾')

    # 2 ------------------------------------------------------------------------------------------------------
    def test_2_a_running_app_that_holds_back_junk_is_named_with_the_size(self):
        self.mac.running = {'com.openai.codex'}
        data = self.only({'codex.browser-cache': 10 * MIB, 'codex.logs': 2 * MIB})
        t = todo(data, 'quit_app:com.openai.codex')
        self.assertEqual((t['severity'], t['title'], t['bytes'], t['action']['href']),
                         ('act', 'Quit Codex to clean 10.00 MiB more', 10 * MIB, '#/overview?quick=1'))

    def test_2_an_app_that_is_not_running_blocks_nothing(self):
        data = self.only({'codex.browser-cache': 10 * MIB})
        self.assertFalse([i for i in todo_ids(data) if i.startswith('quit_app')])

    def test_2_apps_named_by_the_items_evidence_count_too_and_sizes_add_up(self):
        slack = Evidence('evidence.requires_quit', {'bundle_id': 'com.tinyspeck.slackmacgap', 'app': 'Slack'})
        self.mac.running = {'com.tinyspeck.slackmacgap', 'com.openai.codex'}
        data = self.only({'app.electron.cache:~/Library/Application Support/Slack': 3 * MIB,
                          'app.electron.cache:~/Library/Application Support/Slack2': 4 * MIB,
                          'app.caches:~/Library/Caches/foo': MIB},
                         evidence={'app.electron.cache:~/Library/Application Support/Slack': [slack],
                                   'app.electron.cache:~/Library/Application Support/Slack2': [slack]})
        t = todo(data, 'quit_app:com.tinyspeck.slackmacgap')
        self.assertEqual((t['title'], t['bytes']), ('Quit Slack to clean 7.00 MiB more', 7 * MIB))
        self.assertNotIn('quit_app:com.openai.codex', todo_ids(data))

    def test_2_apps_without_a_translated_name_use_the_one_the_catalog_knows(self):
        self.mac.running = {'com.microsoft.VSCode'}
        data = self.only({'vscode.electron.cache': MIB})
        self.assertEqual(todo(data, 'quit_app:com.microsoft.VSCode')['title'], 'Quit VS Code to clean 1.00 MiB more')

    def test_2_every_app_a_rule_names_has_a_readable_name(self):
        for locale in i18n.LOCALES:
            cat = i18n.catalog(locale)
            for rule in self.synth.catalog.rules:
                for bundle in rule.quit_apps:
                    with self.subTest(locale=locale, bundle=bundle):
                        self.assertTrue(cat.get(f'app.{bundle}') or cat.get(f'insights.app.{bundle}'))

    def test_2_a_group_that_nothing_selects_does_not_block(self):
        self.mac.running = {'com.openai.codex'}
        data = self.only({'codex.browser-cache': 10 * MIB}, age_days=0.0)     # younger than the filter
        self.assertFalse([i for i in todo_ids(data) if i.startswith('quit_app')])

    def test_2_the_running_apps_are_not_listed_when_no_group_needs_them(self):
        self.only({'codex.logs': MIB})
        self.assertNotIn('running_bundle_ids', [c[0] for c in self.mac.calls])

    def test_2_failing_to_list_the_running_apps_is_a_warning_not_silence(self):
        self.mac.running_bundle_ids = mock.Mock(side_effect=RuntimeError('lsappinfo exited with 1'))
        data = self.only({'codex.browser-cache': 10 * MIB})
        t = todo(data, 'running_apps_unknown')
        self.assertEqual(t['severity'], 'warn')
        self.assertIn('lsappinfo exited with 1', t['detail'])
        self.assertIn('clean_junk', todo_ids(data))

    # 3 ------------------------------------------------------------------------------------------------------
    def test_3_a_review_group_of_half_a_gib_needs_a_decision(self):
        data = self.only({'codex.runtimes': 500 * MIB})
        t = todo(data, 'review:codex.runtimes')
        self.assertEqual((t['severity'], t['bytes'], t['command']), ('review', 500 * MIB, None))
        self.assertEqual(t['title'], 'Codex runtimes: 500.00 MiB needs your decision')
        self.assertEqual(t['detail'], 'Language runtimes and tools Codex downloads to run code.')   # first sentence
        self.assertTrue(i18n.t('rule.codex.runtimes.effect', 'en').startswith(t['detail'] + ' '))
        self.assertEqual(t['action']['href'], '#/browse?group=codex.runtimes')

    def test_3_a_byte_less_is_not_enough_and_protected_or_report_only_never_count(self):
        data = self.only({'codex.runtimes': 500 * MIB - 1, 'codex.state': 5 * GIB, 'ollama.models': 5 * GIB,
                          'app.chrome-cache': 5 * GIB})
        self.assertFalse([i for i in todo_ids(data) if i.startswith('review:')])

    def test_3_the_top_three_by_size_and_the_detail_is_the_first_sentence(self):
        groups = {'codex.runtimes': 3 * GIB, 'app.spotify-cache': 2 * GIB, 'claude.projects:~/.claude/projects/x': GIB,
                  'codex.sessions:~/.codex/sessions/2026/08': 700 * MIB}
        data = self.only(groups)
        got = [t['id'] for t in data['todos'] if t['id'].startswith('review:')]
        self.assertEqual(got, ['review:codex.runtimes', 'review:app.spotify-cache',
                               'review:claude.projects:~/.claude/projects/x'])
        group = next(g for g in self.app.store.groups(data['environment']['last_scan']['id'])
                     if g.key == 'claude.projects:~/.claude/projects/x')
        effect = group_effect(group, 'en')
        self.assertTrue(todo(data, 'review:claude.projects:~/.claude/projects/x')['detail'] in effect)
        self.assertTrue(effect.startswith(todo(data, 'review:claude.projects:~/.claude/projects/x')['detail']))

    def test_3_a_group_in_a_switched_off_area_cannot_be_decided(self):
        self.app.update_settings({'domains': ['app-library']})
        data = self.only({'codex.runtimes': GIB, 'app.spotify-cache': GIB})
        self.assertEqual([i for i in todo_ids(data) if i.startswith('review:')], ['review:app.spotify-cache'])

    def test_3_hrefs_carry_the_group_key_percent_encoded(self):
        key = 'codex.sessions:~/.codex/sessions/2026/08'
        data = self.only({key: GIB})
        href = todo(data, f'review:{key}')['action']['href']
        self.assertTrue(href.startswith('#/browse?group='))
        self.assertNotIn('/', href[len('#/browse?group='):])
        self.assertEqual(parse_qs(href.split('?', 1)[1])['group'], [key])

    # 4 ------------------------------------------------------------------------------------------------------
    def test_4_a_report_only_group_with_a_command_is_a_tip_with_that_command(self):
        data = self.only({'dev.homebrew': 200 * MIB})
        t = todo(data, 'report_only:dev.homebrew')
        self.assertEqual((t['severity'], t['bytes'], t['command']), ('tip', 200 * MIB, 'brew cleanup --prune=all'))
        self.assertEqual(t['title'], 'Run `brew cleanup --prune=all` to free about 200.00 MiB')
        self.assertEqual(t['action']['href'], '#/browse?report_only=1')

    def test_4_below_the_limit_without_a_hint_or_protected_there_is_no_tip(self):
        data = self.only({'dev.homebrew': 200 * MIB - 1, 'dev.gradle:~/.gradle/caches/x': 5 * GIB,
                          'codex.state': 5 * GIB})
        self.assertFalse([i for i in todo_ids(data) if i.startswith('report_only:')])

    def test_4_it_does_not_matter_whether_the_dev_area_is_switched_on(self):
        self.assertNotIn('dev-shared', self.domains)
        data = self.only({'dev.homebrew': GIB})
        self.assertIn('report_only:dev.homebrew', todo_ids(data))

    def test_4_at_most_three_biggest(self):
        data = self.only({'dev.homebrew': GIB, 'dev.uv:~/.cache/uv/a': 2 * GIB, 'dev.pip:~/.cache/pip/a': 3 * GIB,
                          'dev.npm': 4 * GIB, 'ollama.models': 5 * GIB})
        self.assertEqual([i for i in todo_ids(data) if i.startswith('report_only:')],
                         ['report_only:ollama.models', 'report_only:dev.npm', 'report_only:dev.pip:~/.cache/pip/a'])

    # 5 ------------------------------------------------------------------------------------------------------
    def grew(self, before, after, days):
        self.synth.scan(days, {'app.chrome-cache': before})
        self.synth.scan(0, {'app.chrome-cache': after})
        return self.build(0)

    def test_5_one_gib_in_the_window_is_a_warning(self):
        data = self.grew(MIB, MIB + GIB, 30)
        t = todo(data, 'growth:app.chrome-cache')
        self.assertEqual((t['severity'], t['bytes'], t['action']['href']), ('warn', GIB, '#/browse?group=app.chrome-cache'))
        self.assertEqual(t['title'], f'{data["growth"]["up"][0]["label"]} grew 1.00 GiB in 30 days')

    def test_5_just_under_the_total_and_the_daily_rate_is_quiet(self):
        data = self.grew(MIB, MIB + GIB - 1, 30)      # 34 MiB a day
        self.assertNotIn('growth:app.chrome-cache', todo_ids(data))

    def test_5_two_hundred_mib_a_day_is_a_warning_even_below_a_gib(self):
        data = self.grew(MIB, MIB + 400 * MIB, 2)
        t = todo(data, 'growth:app.chrome-cache')
        self.assertTrue(t['title'].endswith('grew 400.00 MiB in 2 days'))

    def test_5_the_title_reads_in_the_singular_for_one_day(self):
        data = self.grew(MIB, MIB + 200 * MIB, 1)
        self.assertTrue(todo(data, 'growth:app.chrome-cache')['title'].endswith('grew 200.00 MiB in 1 day'))

    def test_5_just_under_the_rate_is_quiet(self):
        data = self.grew(MIB, MIB + 200 * MIB - 2, 1)
        self.assertNotIn('growth:app.chrome-cache', todo_ids(data))

    def test_5_the_three_biggest_climbers(self):
        self.synth.scan(5, {f'app.caches:~/Library/Caches/c{i}': MIB for i in range(5)})
        self.synth.scan(0, {f'app.caches:~/Library/Caches/c{i}': MIB + (i + 1) * GIB for i in range(5)})
        data = self.build(30)
        self.assertEqual([i for i in todo_ids(data) if i.startswith('growth:')],
                         [f'growth:app.caches:~/Library/Caches/c{i}' for i in (4, 3, 2)])

    def test_5_a_single_scan_has_nothing_to_compare(self):
        data = self.only({'app.chrome-cache': 50 * GIB})
        self.assertFalse([i for i in todo_ids(data) if i.startswith('growth:')])

    # 6 ------------------------------------------------------------------------------------------------------
    def test_6_legacy_id_copies_are_a_review_item_with_the_first_sentence(self):
        data = self.only({'codex.sessions:duplicates': 3 * MIB})
        t = todo(data, 'codex_duplicates')
        self.assertEqual((t['severity'], t['bytes']), ('review', 3 * MIB))
        self.assertEqual(t['title'], '3.00 MiB of Codex sessions are legacy-ID copies')
        self.assertEqual(t['detail'], 'Each file is the legacy-ID copy of a session that also exists under its new ID.')
        self.assertEqual(t['action']['href'], '#/browse?group=codex.sessions%3Aduplicates')

    def test_6_without_copies_there_is_nothing(self):
        for groups in ({'codex.sessions:~/.codex/sessions/2026/08': GIB}, {'codex.sessions:duplicates': 0}):
            with self.subTest(groups=groups):
                self.reset()
                self.assertNotIn('codex_duplicates', todo_ids(self.only(groups)))

    def test_6_a_big_copy_group_is_listed_once(self):
        data = self.only({'codex.sessions:duplicates': GIB})
        self.assertEqual([i for i in todo_ids(data) if 'duplicates' in i], ['codex_duplicates'])

    # 7 ------------------------------------------------------------------------------------------------------
    def test_7_a_scan_older_than_a_week_is_a_warning(self):
        self.synth.scan(8.5, {'codex.logs': MIB})
        t = todo(self.build(), 'scan_stale')
        self.assertEqual((t['severity'], t['title'], t['action']['href']),
                         ('warn', 'Last scan was 8 days ago', '#/overview?scan=1'))
        self.assertEqual(todo(self.build(30, 'zh-CN'), 'scan_stale')['title'], '上次扫描在 8 天前')

    def test_7_just_over_a_week_is_stale(self):
        self.synth.scan(7.5, {'codex.logs': MIB})
        self.assertEqual(todo(self.build(), 'scan_stale')['title'], 'Last scan was 7 days ago')

    def test_7_a_week_old_scan_is_still_fine(self):
        for days in (7.0, 3.0, 0.0):
            with self.subTest(days=days):
                self.reset()
                self.synth.scan(days, {'codex.logs': MIB})
                self.assertNotIn('scan_stale', todo_ids(self.build()))

    def test_7_no_scan_at_all_asks_for_the_first_one(self):
        self.assertIn('first_scan', todo_ids(self.build()))
        self.synth.scan(1, {'codex.logs': MIB})
        self.assertNotIn('first_scan', todo_ids(self.build()))

    # 8 ------------------------------------------------------------------------------------------------------
    def test_8_the_weekly_scan_is_suggested_until_it_is_installed(self):
        self.synth.scan(1, {'codex.logs': MIB})
        t = todo(self.build(), 'schedule')
        self.assertEqual((t['severity'], t['title'], t['command'], t['action']['href'], t['bytes']),
                         ('tip', 'Turn on the weekly scan (scans only, never deletes)', 'disk-steward schedule install',
                          '#/settings', None))
        sched._write_plist(sched.plist_path(), sched.build_plist(self.fh.data_dir, 1, 10, False, 'en'))
        self.assertNotIn('schedule', todo_ids(self.build()))

    # 9 ------------------------------------------------------------------------------------------------------
    def test_9_old_archives_that_still_hold_data_are_counted(self):
        self.add_archive('a1', 31, 1000)
        self.add_archive('a2', 40, 2000, state='restored')
        self.add_archive('a3', 50, 4000, state='purged')       # gone from the disk
        self.add_archive('a4', 29.9, 8000)                     # still young
        t = todo(self.build(), 'archives_old')
        self.assertEqual((t['severity'], t['bytes'], t['action']['href']), ('tip', 3000, '#/archives'))
        self.assertEqual(t['title'], '2 archives (2.93 KiB) older than 30 days')

    def test_9_one_archive_reads_in_the_singular_and_none_is_quiet(self):
        self.synth.scan(1, {'codex.logs': MIB})
        self.assertNotIn('archives_old', todo_ids(self.build()))
        self.add_archive('a1', 45, 2048)
        self.assertEqual(todo(self.build(), 'archives_old')['title'], '1 archive (2.00 KiB) older than 30 days')

    # 10 -----------------------------------------------------------------------------------------------------
    def test_10_space_that_snapshots_hold_back_is_explained(self):
        self.synth.scan(1, {'codex.logs': MIB})
        self.cleanup('p1', [('done', 'delete', 100, 100)], measured=499, estimated=1000)
        t = todo(self.build(), 'snapshots_held')
        self.assertEqual((t['severity'], t['bytes']), ('tip', 501))
        self.assertEqual(t['title'], 'Freed space is held by 2 local snapshots until macOS removes them')
        self.assertIsNone(t['action'])

    def test_10_a_cleanup_that_freed_half_or_more_is_fine(self):
        self.synth.scan(1, {'codex.logs': MIB})
        self.cleanup('p1', [('done', 'delete', 100, 100)], measured=500, estimated=1000)
        self.assertNotIn('snapshots_held', todo_ids(self.build()))

    def test_10_without_snapshots_there_is_nothing_to_blame(self):
        self.synth.scan(1, {'codex.logs': MIB})
        self.cleanup('p1', [('done', 'delete', 100, 100)], measured=0, estimated=1000)
        self.mac.snapshot_names = []
        self.assertNotIn('snapshots_held', todo_ids(self.build()))

    def test_10_the_last_cleanup_decides_and_one_snapshot_reads_in_the_singular(self):
        self.synth.scan(1, {'codex.logs': MIB})
        self.cleanup('old', [('done', 'delete', 100, 100)], measured=0, estimated=1000, at_days_ago=9)
        self.cleanup('new', [('done', 'delete', 100, 100)], measured=900, estimated=1000, at_days_ago=1)
        self.assertNotIn('snapshots_held', todo_ids(self.build()))
        self.cleanup('newest', [('done', 'delete', 100, 100)], at_days_ago=0.5)        # no run summary kept
        self.assertNotIn('snapshots_held', todo_ids(self.build()))
        self.cleanup('last', [('done', 'delete', 100, 100)], measured=-5, estimated=1000, at_days_ago=0.1)
        self.mac.snapshot_names = ['com.apple.TimeMachine.1']
        self.assertEqual(todo(self.build(), 'snapshots_held')['title'],
                         'Freed space is held by 1 local snapshot until macOS removes it')


class TodoOrderTests(InsightsCase):
    def crowded(self):
        self.mac.running = {'com.openai.codex', 'com.microsoft.VSCode', 'com.google.Chrome', 'com.spotify.client'}
        self.synth.scan(10, {f'app.caches:~/Library/Caches/c{i}': MIB for i in range(4)})
        self.synth.scan(0, {
            'codex.browser-cache': 30 * MIB, 'vscode.electron.cache': 20 * MIB, 'app.chrome-cache': 10 * MIB,
            'codex.runtimes': 3 * GIB, 'app.spotify-cache': 2 * GIB, 'claude.projects:~/.claude/projects/x': GIB,
            'codex.sessions:duplicates': 5 * MIB, 'dev.homebrew': GIB, 'dev.npm': 2 * GIB, 'ollama.models': 3 * GIB,
            **{f'app.caches:~/Library/Caches/c{i}': MIB + (i + 1) * GIB for i in range(4)}})
        self.cleanup('p1', [('done', 'delete', 100, 100)], measured=0, estimated=1000)
        self.add_archive('a1', 45, 2048)
        return self.build()

    def test_severity_order_then_size_and_the_cap(self):
        data = self.crowded()
        todos = data['todos']
        self.assertEqual(len(todos), 12)
        rank = {'act': 0, 'warn': 1, 'review': 2, 'tip': 3}
        keys = [(rank[t['severity']], -(t['bytes'] if t['bytes'] is not None else -1)) for t in todos]
        self.assertEqual(keys, sorted(keys))
        # more than twelve qualify; the lowest ranked fall off the end
        self.assertEqual([t['severity'] for t in todos].count('act'), 4)
        self.assertEqual(todos[0]['id'], 'clean_junk')
        self.assertEqual(todos[-1]['severity'], 'tip')
        self.assertNotIn('archives_old', todo_ids(data))

    def test_every_todo_has_the_documented_shape(self):
        for t in self.crowded()['todos']:
            with self.subTest(todo=t['id']):
                self.assertEqual(set(t) - {'runnable'}, {'id', 'severity', 'title', 'detail', 'bytes', 'action',
                                                          'command'})
                self.assertEqual('runnable' in t, t['id'].startswith('report_only:'))
                self.assertIn(t['severity'], ('act', 'review', 'tip', 'warn'))
                self.assertTrue(t['title'] and t['detail'])
                self.assertTrue(t['bytes'] is None or isinstance(t['bytes'], int))
                if t['action'] is not None:
                    self.assertEqual(set(t['action']), {'label', 'href'})
                    self.assertTrue(t['action']['label'] and t['action']['href'].startswith('#/'))
                self.assertTrue(t['command'] is None or isinstance(t['command'], str))

    def test_every_todo_reads_in_both_languages(self):
        en, zh = self.crowded()['todos'], self.build(30, 'zh-CN')['todos']
        self.assertEqual([t['id'] for t in en], [t['id'] for t in zh])
        for a, b in zip(en, zh):
            self.assertNotEqual(a['title'], b['title'], a['id'])
            self.assertEqual(a['action'] and a['action']['href'], b['action'] and b['action']['href'])


class TodoLinkTests(InsightsCase):
    """Every link must lead to a place that shows the same thing: Browse understands `group` and the overview
    filters the portal exposes, the other pages need no parameters."""

    def links(self):
        self.mac.running = {'com.openai.codex'}
        self.synth.scan(10, {'app.caches:~/Library/Caches/c0': MIB})
        self.synth.scan(0, {'codex.browser-cache': 30 * MIB, 'codex.runtimes': 3 * GIB, 'dev.homebrew': GIB,
                            'codex.sessions:duplicates': 5 * MIB, 'ollama.models': 2 * GIB,
                            'app.caches:~/Library/Caches/c0': 3 * GIB})
        return [t['action']['href'] for t in self.build()['todos'] if t['action']]

    def test_links_use_only_what_the_pages_understand(self):
        hrefs = self.links()
        self.assertGreater(len(hrefs), 6)
        browse_keys = set(api_mod.FILTERS) | {'group'}
        for href in hrefs:
            with self.subTest(href=href):
                path, _, query = href.partition('?')
                params = parse_qs(query, keep_blank_values=True)
                if path == '#/browse':
                    self.assertLessEqual(set(params), browse_keys)
                elif path == '#/overview':
                    self.assertLessEqual(set(params), {'quick', 'scan'})
                    self.assertEqual({k: v for k, v in params.items()}, {k: ['1'] for k in params})
                else:
                    self.assertIn(path, ('#/settings', '#/archives'))
                    self.assertEqual(query, '')

    def test_group_links_name_groups_the_scan_has_and_that_browse_can_open(self):
        hrefs = [h for h in self.links() if h.startswith('#/browse?group=')]
        self.assertGreater(len(hrefs), 2)
        have = {g['key'] for g in self.app.groups()}
        for href in hrefs:
            key = parse_qs(href.split('?', 1)[1])['group'][0]
            with self.subTest(key=key):
                self.assertIn(key, have)
                self.app.items(group=key, limit=5)           # what Browse asks for when it opens the group

    def test_the_report_only_link_is_the_overviews_own_filter_and_lists_the_hinted_groups(self):
        hrefs = self.links()
        self.assertIn('#/browse?report_only=1', hrefs)
        flt = api_mod.overview_filters(14)['rows']['report_only']
        self.assertEqual(flt, {'report_only': '1'})
        shown = self.app.browse_scope(flt)
        self.assertTrue({'dev.homebrew', 'ollama.models'} <= set(shown['groups']))

    def test_the_quick_link_is_the_cleanup_flow_with_the_same_number(self):
        self.synth.scan(0, {'codex.logs': (7 * MIB, 4)})
        t = todo(self.build(), 'clean_junk')
        self.assertEqual(t['action']['href'], '#/overview?quick=1')
        quick = self.app.quick()
        self.assertEqual((t['bytes'], quick['summary']['alloc']), (7 * MIB, 7 * MIB))
        self.assertEqual(quick['summary']['count'], 4)

    @unittest.skipUnless(BROWSE_JS.is_file(), 'the portal has no Browse view here')
    def test_browse_itself_reads_the_group_parameter_and_the_report_only_filter(self):
        source = BROWSE_JS.read_text(encoding='utf-8')
        keys = re.search(r'FILTER_KEYS\s*=\s*\[([^\]]*)\]', source)
        if keys is None:
            self.skipTest('browse.mjs no longer lists FILTER_KEYS')
        self.assertIn("'report_only'", keys.group(1))
        self.assertIn('route.query.group', source)


if __name__ == '__main__':
    unittest.main()
