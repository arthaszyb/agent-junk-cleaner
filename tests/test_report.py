from __future__ import annotations

import json
import os
import unittest

import m2_fixture as fx
from disk_steward import i18n, report
from disk_steward.model import GroupSel, Spec, StewardError, Tier
from disk_steward.report import (build_report, display_width, fmt_bytes, group_effect, group_label, render_growth,
                                 render_text, table)

from m3_support import make_app, make_mac
from support import DAY, Clock, FakeHome

KIB = 1024


class FmtBytesTests(unittest.TestCase):
    def test_units_and_decimals(self):
        for n, text in ((0, '0 B'), (1, '1 B'), (1023, '1023 B'), (1024, '1.00 KiB'), (1536, '1.50 KiB'),
                        (1024 ** 2, '1.00 MiB'), (5 * 1024 ** 2 + 512 * KIB, '5.50 MiB'), (3 * 1024 ** 3, '3.00 GiB'),
                        (2048 * 1024 ** 3, '2048.00 GiB'), (1023 * KIB, '1023.00 KiB')):
            with self.subTest(n=n):
                self.assertEqual(fmt_bytes(n), text)

    def test_sign(self):
        self.assertEqual(fmt_bytes(-1536), '-1.50 KiB')
        self.assertEqual(fmt_bytes(-5), '-5 B')
        self.assertEqual(fmt_bytes(1536, signed=True), '+1.50 KiB')
        self.assertEqual(fmt_bytes(-1536, signed=True), '-1.50 KiB')
        self.assertEqual(fmt_bytes(0, signed=True), '0 B')


class TableTests(unittest.TestCase):
    def test_columns_line_up(self):
        out = table([('a', 'bb', 'c'), ('xxxx', '1', 'z'), ('y', '22222', 'long text')], 'lrl').split('\n')
        self.assertEqual(out[0], 'a        bb  c')
        self.assertEqual(out[1], '----  -----  ---------')
        self.assertEqual(out[2], 'xxxx      1  z')
        self.assertEqual(out[3], 'y     22222  long text')

    def test_wide_characters_count_twice(self):
        out = table([('名称', '大小'), ('abcdef', '1.00 KiB'), ('中文名', '2.00 MiB')], 'lr').split('\n')
        self.assertEqual(len({display_width(line) for line in out}), 1)
        self.assertEqual(display_width('中a'), 3)

    def test_empty(self):
        self.assertEqual(table([]), '')


class ReportCase(unittest.TestCase):
    def setUp(self):
        self.fh = FakeHome(Clock()).__enter__()
        self.addCleanup(self.fh.cleanup)
        fx.build(self.fh)
        self.app = make_app(self.fh, mac=make_mac(snapshot_names=['com.apple.TimeMachine.one']))
        self.addCleanup(self.app.close)

    def unlink(self, logical):
        os.unlink(self.fh.path(logical))

    def scan(self):
        self.sid = self.app.scan()
        self.catalog = self.app._load_catalog()
        return self.app.overview()

    def build(self, spec=None, now=None):
        return build_report(self.app.store, self.sid, self.catalog, self.app.settings,
                            self.fh.clock() if now is None else now, spec=spec)


class PartitionTests(ReportCase):
    def assert_partition(self, r):
        ni = r['not_included']
        parts = (ni['protected']['alloc'] + ni['review']['alloc'] + ni['report_only']['alloc'] + ni['too_recent']['alloc']
                 + ni['floor_held']['alloc'] + ni['other']['alloc'])
        self.assertEqual(parts + r['offered']['alloc'], r['totals']['alloc'])
        items = (ni['protected']['items'] + ni['review']['items'] + ni['report_only']['items'] + ni['too_recent']['items']
                 + ni['floor_held']['items'] + ni['other']['items'])
        self.assertEqual(items + r['offered']['items'], r['scan']['items'])
        self.assertEqual(sum(v['alloc'] for v in ni['protected']['by_reason'].values()), ni['protected']['alloc'])
        self.assertEqual(sum(v['items'] for v in ni['protected']['by_reason'].values()), ni['protected']['items'])

    def test_the_buckets_add_up_to_the_scanned_total_minus_what_is_offered(self):
        r = self.scan()
        self.assert_partition(r)
        self.assertEqual(r['totals']['alloc'], self.app.store.scan(self.sid)['attributed'])
        self.assertGreater(r['offered']['alloc'], 0)
        self.assertGreater(r['not_included']['too_recent']['alloc'], 0)

    def test_it_holds_for_other_filters_and_clocks(self):
        self.scan()
        for filter_days in (0, 3, 14, 60):
            for ahead in (0, 8, 45):
                with self.subTest(filter=filter_days, ahead=ahead):
                    spec = Spec(scan_id=self.sid, filter_min_age_days=filter_days)
                    self.assert_partition(self.build(spec, self.fh.clock() + ahead * DAY))

    def test_offered_too_recent_and_floor_held_follow_the_filter(self):
        self.fh.file('~/.claude/debug/new.txt', size=10, age_days=0.2)         # younger than the 1-day floor of claude.logs
        self.scan()
        # independent oracle: junk of enabled areas, one link, judged by age against the rule floor and the filter
        store, now = self.app.store, self.fh.clock()
        groups = {g.key: g for g in store.groups(self.sid)}

        def oracle(filter_days):
            offered = recent = held = 0
            for i in store.iter_items(self.sid):
                g = groups[i.group_key]
                if g.tier != Tier.JUNK or not g.actions or i.taint or (i.unit.value == 'file' and i.nlink > 1):
                    continue
                age = (now - i.newest_ns / 1e9) / DAY
                if age < g.min_age_floor_days:
                    held += i.alloc
                elif age < filter_days:
                    recent += i.alloc
                else:
                    offered += i.alloc
            return offered, recent, held
        for days in (0, 3, 14, 100):
            ni = (r := self.build(Spec(scan_id=self.sid, filter_min_age_days=days)))['not_included']
            self.assertEqual((r['offered']['alloc'], ni['too_recent']['alloc'], ni['floor_held']['alloc']), oracle(days), days)
            self.assertEqual(r['offered']['min_age_days'], days)
            self.assertEqual(ni['too_recent']['min_age_days'], days)
        self.assertGreater(oracle(0)[2], 0)                       # the floor really holds something back
        self.assertEqual(self.build(Spec(scan_id=self.sid, filter_min_age_days=0))['not_included']['too_recent']['items'], 0)

    def test_the_too_recent_row_reads_sensibly_without_a_filter(self):
        self.fh.file('~/.claude/debug/new.txt', size=10, age_days=0.2)
        self.scan()
        for days, shown in ((14, True), (0, False)):
            text = render_text(self.build(Spec(scan_id=self.sid, filter_min_age_days=days)), 'en')
            self.assertIn("Junk held back by its rule's minimum age", text)
            self.assertEqual('Junk newer than 14 days' in text, shown)
            self.assertNotIn('older than 0 days', text)
            self.assertNotIn('newer than 0 days', text)
            self.assertEqual('Offered now: junk older than 14 days' in text, shown)
            self.assertEqual('Offered now: junk of any age (rule minimums still apply)' in text, not shown)
        zh = render_text(self.build(Spec(scan_id=self.sid, filter_min_age_days=0)), 'zh-CN')
        self.assertIn('任意年龄', zh)
        self.assertIn('未达规则最短保留期', zh)

    def test_default_filter_comes_from_settings(self):
        self.scan()
        r = self.build()
        self.assertEqual(r['offered']['min_age_days'], 14)
        self.app.update_settings({'default_min_age_days': 3})
        self.assertEqual(self.build()['offered']['min_age_days'], 3)

    def test_spec_group_overrides_are_honoured(self):
        self.scan()
        plain = self.build(Spec(scan_id=self.sid, filter_min_age_days=14))
        loose = self.build(Spec(scan_id=self.sid, filter_min_age_days=14, groups={'codex.logs': GroupSel(min_age_days=0)}))
        self.assertGreater(loose['offered']['alloc'], plain['offered']['alloc'])

    def test_protected_split_by_reason(self):
        self.fh.file('~/.cache/codex-runtimes/codex-runtime-install-t/payload/data.sqlite', size=10, age_days=10)
        self.fh.dir('~/.cache/codex-runtimes/codex-runtime-install-t', age_days=10)
        r = self.scan()
        by = r['not_included']['protected']['by_reason']
        self.assertEqual(set(by), {'credentials', 'databases', 'config', 'user_content', 'current_version', 'tainted',
                                   'unknown', 'other'})
        self.assertEqual(by['credentials']['items'], 1)                 # ~/.codex/auth.json and ~/.ollama/id_ed25519 roll up
        self.assertEqual(by['tainted']['items'], 1)                     # the runtime install holding a database
        self.assertGreaterEqual(by['current_version']['items'], 5)      # newest engines, the claude in use, plugins ...
        self.assertTrue(set(by) <= set(report.REASONS))
        self.assert_partition(r)

    def test_review_and_report_only(self):
        r = self.scan()
        ni = r['not_included']
        groups = self.app.store.groups(self.sid)
        review = [g for g in groups if g.tier == Tier.REVIEW and g.actions]
        self.assertEqual((ni['review']['alloc'], ni['review']['groups']), (sum(g.alloc for g in review), len(review)))
        (only,) = ni['report_only']['groups']
        self.assertEqual((only['key'], only['hint']), ('ollama.models', 'ollama list  # then: ollama rm <model>'))
        self.assertEqual(ni['report_only']['alloc'], only['alloc'])

    def test_hardlinked_junk_and_disabled_areas_land_in_other(self):
        self.fh.file('~/.codex/log/hl.log', size=2000, age_days=10)
        os.link(self.fh.path('~/.codex/log/hl.log'), self.fh.path('~/.codex/cache/hl-link.json'))
        self.scan()
        before = self.build()
        self.app.update_settings({'domains': ['app-library']})
        r = self.build()
        self.assert_partition(before)
        self.assert_partition(r)
        self.assertGreaterEqual(before['not_included']['other']['alloc'], 4096)         # the hardlinked log
        self.assertEqual(r['offered']['alloc'], 0)                      # no scanned junk belongs to an enabled area now
        self.assertGreater(r['not_included']['other']['alloc'], before['not_included']['other']['alloc'])


class SectionTests(ReportCase):
    def test_scan_meta_and_json_safety(self):
        r = self.scan()
        info = self.app.store.scan(self.sid)
        self.assertEqual(r['scan']['id'], self.sid)
        self.assertEqual((r['scan']['domains'], r['scan']['finished_at']), (['agent'], info['finished_at']))
        self.assertEqual(r['scan']['items'], self.app.store.count_items(self.sid))
        self.assertEqual(r['scan']['groups'], len(self.app.store.groups(self.sid)))
        self.assertIs(r['scan']['rules_changed'], False)
        json.dumps(r)

    def test_rules_changed_follows_settings_that_alter_classification(self):
        self.scan()
        self.app.update_settings({'default_min_age_days': 3})           # does not affect classification
        self.assertIs(self.app.overview()['scan']['rules_changed'], False)
        self.app.update_settings({'idle_days': 7})
        self.assertIs(self.app.overview()['scan']['rules_changed'], True)

    def test_volume(self):
        r = self.scan()
        v = r['volume']
        attributed = self.app.store.scan(self.sid)['attributed']
        self.assertEqual((v['total'], v['free'], v['used']), (500 * 10 ** 9, 300 * 10 ** 9, 200 * 10 ** 9))
        self.assertEqual((v['attributed'], v['unattributed']), (attributed, 200 * 10 ** 9 - attributed))
        self.assertEqual(v['snapshots'], ['com.apple.TimeMachine.one'])

    def test_snapshots_are_none_when_the_caller_gave_none(self):
        self.scan()
        self.assertIsNone(self.build()['volume']['snapshots'])

    def test_totals_by_tier_and_domain(self):
        r = self.scan()
        groups = self.app.store.groups(self.sid)
        for tier in Tier:
            mine = [g for g in groups if g.tier == tier]
            self.assertEqual(r['totals']['by_tier'][tier.value],
                             {'alloc': sum(g.alloc for g in mine), 'reclaim': sum(g.reclaim for g in mine),
                              'items': sum(g.items for g in mine), 'groups': len(mine)})
        self.assertEqual(set(r['totals']['by_domain']), {'agent'})
        self.assertEqual(r['totals']['by_domain']['agent']['items'], r['scan']['items'])
        self.assertEqual(r['totals']['alloc'], sum(v['alloc'] for v in r['totals']['by_tier'].values()))

    def test_top_groups_are_the_30_largest(self):
        r = self.scan()
        top = r['top_groups']
        self.assertEqual(len(top), 30)
        self.assertEqual([g['alloc'] for g in top], sorted((g['alloc'] for g in top), reverse=True))
        self.assertEqual(top[0]['alloc'], max(g.alloc for g in self.app.store.groups(self.sid)))
        self.assertNotIn('delta', top[0])                               # nothing to compare with yet
        self.assertTrue({'key', 'rule_id', 'label_args', 'tier', 'domain', 'alloc', 'reclaim', 'items', 'actions',
                         'hint'} <= set(top[0]))

    def test_scan_error_counts(self):
        r = self.scan()
        store = self.app.store
        for path, code in (('~/a', 'permission_denied'), ('~/b', 'permission_denied'), ('~/c', 'skipped_privacy'),
                           ('~/d', 'vanished')):
            store.add_scan_error(self.sid, path, code)
        ni = self.build()['not_included']
        self.assertEqual(ni['errors'], {'permission_denied': 2, 'skipped_privacy': 1, 'vanished': 1})
        self.assertEqual(ni['privacy_skipped'], 1)
        self.assertEqual(r['not_included']['errors'], {})
        text = render_text(self.build(), 'en')
        self.assertIn('Skipped for privacy: 1 locations.', text)
        self.assertIn('permission denied 2', text)
        self.assertIn('vanished during scan 1', text)


class GrowthTests(ReportCase):
    def test_no_baseline_on_the_first_scan(self):
        r = self.scan()
        self.assertEqual(r['growth'], {'previous_scan_id': None, 'previous_finished_at': None, 'alloc_delta': None,
                                       'groups': []})
        self.assertEqual(render_growth(r, 'en'), 'No earlier scan to compare with.\n')

    def test_growth_against_the_previous_done_scan(self):
        first = self.scan()
        sid1 = self.sid
        self.fh.file('~/.claude/debug/big.txt', size=40 * KIB, age_days=5)           # claude.logs grows
        self.fh.file('~/Library/Caches/Codex/web/a.bin', size=8 * KIB, age_days=2)    # a group that did not exist
        self.unlink('~/.codex/cache/models.json')                                      # codex.cache disappears
        second = self.scan()
        store = self.app.store
        self.assertEqual(store.count_items(sid1), 0)    # growth reads group_history; the old scan's rows are gone
        g = second['growth']
        self.assertEqual(g['previous_scan_id'], sid1)
        self.assertEqual(g['previous_finished_at'], store.scan(sid1)['finished_at'])
        self.assertEqual(g['alloc_delta'], store.scan(self.sid)['attributed'] - store.scan(sid1)['attributed'])
        by_key = {x['key']: x for x in g['groups']}
        logs = by_key['claude.logs']
        self.assertEqual((logs['new'], logs['delta']), (False, store.group(self.sid, 'claude.logs').alloc -
                                                       next(a for s, _f, a, _n in store.group_history('claude.logs') if s == sid1)))
        self.assertGreaterEqual(logs['delta'], 40 * KIB)
        new = by_key['codex.browser-cache']
        self.assertEqual((new['new'], new['previous_alloc'], new['delta']), (True, 0, new['alloc']))
        self.assertNotIn('codex.cache', by_key)                                        # gone groups are not listed
        self.assertEqual([abs(x['delta']) for x in g['groups']], sorted((abs(x['delta']) for x in g['groups']), reverse=True))
        top = {x['key']: x for x in second['top_groups']}
        self.assertEqual(top['claude.logs']['delta'], logs['delta'])
        self.assertIs(top['claude.logs']['new'], False)
        self.assertEqual(first['scan']['id'], sid1)

    def test_a_cancelled_scan_in_between_is_skipped(self):
        self.scan()
        sid1 = self.sid
        with self.assertRaises(StewardError):
            self.app.scan(cancel=lambda: True)
        self.fh.file('~/.claude/debug/more.txt', size=20 * KIB, age_days=5)
        r = self.scan()
        self.assertEqual(r['growth']['previous_scan_id'], sid1)
        self.assertGreater(self.sid, sid1 + 1)

    def test_text_shows_the_change_column_and_new_groups(self):
        self.scan()
        self.fh.file('~/Library/Caches/Codex/web/a.bin', size=8 * KIB, age_days=2)
        r = self.scan()
        text = render_text(r, 'en')
        self.assertIn('Change', text)
        self.assertIn('new', text)
        growth = render_growth(r, 'en')
        self.assertIn('Change since scan #', growth)
        self.assertIn('Codex browser caches (new)', growth)
        self.assertIn('+', growth)
        self.assertIn('相比扫描', render_growth(r, 'zh-CN'))

    def test_unchanged_disk(self):
        self.scan()
        r = self.scan()
        self.assertEqual((r['growth']['alloc_delta'], r['growth']['groups']), (0, []))
        self.assertIn('No group changed in size.', render_growth(r, 'en'))


class LabelTests(unittest.TestCase):
    GROUP = {'key': 'claude.projects:~/.claude/projects/x', 'rule_id': 'claude.projects',
             'label_args': {'path': '~/.claude/projects/x'}}

    def test_rule_label_with_args(self):
        self.assertEqual(group_label(self.GROUP, 'en'), 'Claude Code sessions (~/.claude/projects/x)')
        self.assertIn('~/.claude/projects/x', group_label(self.GROUP, 'zh-CN'))
        self.assertEqual(group_effect(self.GROUP, 'en'), i18n.t('rule.claude.projects.effect', 'en', path='x'))

    def test_group_specific_text_wins(self):
        g = {'key': 'codex.sessions:duplicates', 'rule_id': 'codex.sessions', 'label_args': {'path': 'duplicates'}}
        self.assertEqual(group_label(g, 'en'), i18n.t('group.codex.sessions:duplicates.label', 'en'))
        self.assertNotIn('duplicates)', group_label(g, 'en'))
        self.assertEqual(group_effect(g, 'zh-CN'), i18n.t('group.codex.sessions:duplicates.effect', 'zh-CN'))

    def test_protected_sibling_is_marked(self):
        g = {'key': 'claude.cli-versions#protected', 'rule_id': 'claude.cli-versions', 'label_args': {}}
        self.assertEqual(group_label(g, 'en'), 'Old Claude Code versions (kept)')
        self.assertEqual(group_effect(g, 'en'), i18n.t('rule.claude.cli-versions.effect', 'en'))

    def test_protected_sibling_that_carries_a_path_suffix(self):
        g = {'key': 'claude.projects#protected:~/.claude/projects/x', 'rule_id': 'claude.projects',
             'label_args': {'path': '~/.claude/projects/x'}}
        self.assertEqual(group_label(g, 'en'), 'Claude Code sessions (~/.claude/projects/x) (kept)')
        self.assertEqual(group_label(g, 'zh-CN'), 'Claude Code 会话(~/.claude/projects/x)（保留）')

    def test_a_key_that_merely_contains_the_marker_text_is_not_a_sibling(self):
        g = {'key': 'unknown:~/odd#protected', 'rule_id': 'unknown', 'label_args': {'path': '~/odd#protected'}}
        self.assertEqual(group_label(g, 'en'), 'Unclassified: ~/odd#protected')

    def test_group_objects_work_too(self):
        from test_selection import mk_group
        g = mk_group('codex.logs', rule_id='codex.logs', label_args={})
        self.assertEqual(group_label(g, 'en'), 'Codex logs')


class RenderTests(ReportCase):
    def test_both_locales_render_without_missing_keys(self):
        self.fh.file('~/.cache/codex-runtimes/codex-runtime-install-t/payload/data.sqlite', size=10, age_days=10)
        self.fh.dir('~/.cache/codex-runtimes/codex-runtime-install-t', age_days=10)
        self.scan()
        self.app.store.add_scan_error(self.sid, '~/a', 'permission_denied')
        self.fh.file('~/Library/Caches/Codex/web/a.bin', size=8 * KIB, age_days=2)
        self.scan()
        r = self.app.overview()
        missing_before = set(i18n.MISSING)
        out = {loc: render_text(r, loc) for loc in i18n.LOCALES}
        growth = {loc: render_growth(r, loc) for loc in i18n.LOCALES}
        self.assertEqual(set(i18n.MISSING), missing_before)
        for loc, text in out.items():
            self.assertNotIn('cli.', text, loc)
            self.assertNotIn('{', text, loc)
            self.assertTrue(text.endswith('\n'))
        self.assertIn('Space by tier', out['en'])
        self.assertIn('GiB', out['en'])
        self.assertIn('按等级统计', out['zh-CN'])
        self.assertIn('可回收', out['zh-CN'])
        self.assertIn('ollama list', out['en'])
        self.assertIn('ollama list', out['zh-CN'])
        self.assertTrue(all(growth.values()))

    def test_every_cli_key_used_in_the_sources_exists_in_both_locales(self):
        import re
        from pathlib import Path
        src = Path(report.__file__).resolve().parent
        used = set()
        dynamic = []
        for py in [src / 'report.py', src / 'service.py', src / 'selection.py', src / 'planner.py'] + list((src / 'cli').glob('*.py')):
            text = py.read_text(encoding='utf-8')
            used.update(re.findall(r"""['"]((?:cli|warning|error|group)\.[A-Za-z0-9_.:\-]+)['"]""", text))
            dynamic.extend(re.findall(r"""f['"]((?:cli)\.[A-Za-z_.]+)\{""", text))
        for loc in i18n.LOCALES:
            cat = i18n.catalog(loc)
            self.assertEqual(sorted(k for k in used if k not in cat and not k.endswith('.')), [], loc)
        # dynamic keys: every value of the enumeration they range over has text
        self.assertLessEqual({'cli.mismatch.', 'cli.reason.', 'cli.scan_error.'}, set(dynamic))   # others belong to other modules
        for loc in i18n.LOCALES:
            cat = i18n.catalog(loc)
            for side in ('added', 'removed'):
                self.assertIn(f'cli.mismatch.{side}', cat)
            for reason in report.REASONS:
                self.assertIn(f'cli.reason.{reason}', cat)
            for code in ('skipped_privacy', 'permission_denied', 'vanished', 'undecodable_name', 'other_volume',
                         'root_symlink', 'not_a_directory'):
                self.assertIn(f'cli.scan_error.{code}', cat)


if __name__ == '__main__':
    unittest.main()
