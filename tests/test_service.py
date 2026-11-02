from __future__ import annotations

import json
import os
import unittest
from unittest import mock

import m2_fixture as fx
from disk_steward import selection
from disk_steward.model import Action, ErrorCode, GroupSel, Spec, StewardError
from disk_steward.paths import item_key
from disk_steward.service import App

from m3_support import make_app, make_mac
from support import Clock, FakeHome

LOG = '~/.claude/debug/x.txt'          # claude.logs: 5 days old, rule floor 1 day
JUNK_ALL = [{'op': 'select_tier', 'tier': 'junk', 'on': True}, {'op': 'set_filter', 'min_age_days': 0}]


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.fh = FakeHome(Clock()).__enter__()
        self.addCleanup(self.fh.cleanup)
        fx.build(self.fh)
        self.app = make_app(self.fh)
        self.addCleanup(self.app.close)

    def expect(self, code, key, fn, *args, **kw):
        with self.assertRaises(StewardError) as cm:
            fn(*args, **kw)
        self.assertEqual((cm.exception.code, cm.exception.message_key), (code, key))
        return cm.exception

    def unlink(self, logical):
        os.unlink(self.fh.path(logical))


class BeforeAnyScanTests(ServiceCase):
    def test_nothing_works_without_a_scan(self):
        self.assertIsNone(self.app.latest_scan())
        for fn in (self.app.overview, self.app.groups, self.app.spec, self.app.items):
            self.expect(ErrorCode.NO_SCAN, 'error.NO_SCAN', fn)
        self.expect(ErrorCode.NO_SCAN, 'error.NO_SCAN', self.app.apply_ops, [])
        self.expect(ErrorCode.NO_SCAN, 'error.NO_SCAN', self.app.diagnose_session, 'k')

    def test_data_dir_is_created_private(self):
        target = self.fh.root / 'fresh' / 'data'
        app = App(data_dir=target, mac=make_mac(), clock=self.fh.clock, locale='en', bases=self.fh.bases)
        self.addCleanup(app.close)
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o700)
        self.assertTrue((target / 'steward.db').exists())

    def test_locale_comes_from_the_argument_then_settings_then_detection(self):
        self.assertEqual(self.app.locale, 'en')
        from disk_steward.settings import Settings
        zh = make_app(self.fh, settings=Settings(domains=('agent',), locale='zh-CN'), locale=None)
        self.addCleanup(zh.close)
        self.assertEqual(zh.locale, 'zh-CN')
        with mock.patch.dict(os.environ, {'LC_ALL': 'zh_CN.UTF-8'}):
            auto = make_app(self.fh, locale=None)
            self.addCleanup(auto.close)
        self.assertEqual(auto.locale, 'zh-CN')
        self.expect(ErrorCode.INVALID, 'error.invalid_locale', make_app, self.fh, locale='fr')


class ScanTests(ServiceCase):
    def test_scan_publishes_progress_and_done(self):
        seen = []
        sid = self.app.scan(progress=seen.append)
        self.assertEqual(self.app.latest_scan()['id'], sid)
        self.assertTrue(seen)
        self.assertEqual(set(seen[0]), {'phase', 'root', 'files', 'alloc', 'current'})
        events = self.app.events.since(0)
        types = [e['type'] for e in events]
        self.assertIn('scan.progress', types)
        self.assertEqual(types[-1], 'scan.done')
        done = events[-1]['data']
        self.assertEqual((done['scan_id'], done['errors'], done['migrated']), (sid, 0, {}))
        self.assertEqual(sum(1 for t in types if t == 'scan.done'), 1)

    def test_scan_takes_the_operation_lock(self):
        with self.app.lock:
            self.expect(ErrorCode.BUSY, 'error.busy', self.app.scan)
        other = make_app(self.fh)
        self.addCleanup(other.close)
        with other.lock:
            self.expect(ErrorCode.BUSY, 'error.busy', self.app.scan)
        self.assertIsNone(self.app.latest_scan())
        self.app.scan()          # and it is released again afterwards
        self.app.scan()

    def test_domains_argument(self):
        self.expect(ErrorCode.INVALID, 'error.invalid_setting', self.app.scan, domains=['nowhere'])
        sid = self.app.scan(domains=['app-library'])
        self.assertEqual(self.app.store.scan(sid)['domains'], ['app-library'])
        self.assertEqual({r['domain'] for r in self.app.store.scan_roots(sid)}, {'app-library'})   # agent roots stay out

    def test_cancelled_scan_leaves_the_previous_one_in_charge(self):
        first = self.app.scan()
        self.expect(ErrorCode.IN_PROGRESS, 'error.scan_cancelled', self.app.scan, cancel=lambda: True)
        self.assertEqual(self.app.latest_scan()['id'], first)
        self.assertEqual([e['type'] for e in self.app.events.since(0)].count('scan.done'), 1)   # only the first scan finished
        self.assertEqual(self.app.store.scan(first + 1)['status'], 'cancelled')

    def test_overview_adds_snapshots_and_volume(self):
        app = make_app(self.fh, mac=make_mac(snapshot_names=['com.apple.TimeMachine.a', 'com.apple.TimeMachine.b']))
        self.addCleanup(app.close)
        app.scan()
        vol = app.overview()['volume']
        self.assertEqual(vol['snapshots'], ['com.apple.TimeMachine.a', 'com.apple.TimeMachine.b'])
        self.assertEqual((vol['total'], vol['free'], vol['used']), (500 * 10 ** 9, 300 * 10 ** 9, 200 * 10 ** 9))


class RoundTripTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.sid = self.app.scan()

    def test_scan_groups_ops_plan_entries(self):
        groups = self.app.groups()
        self.assertTrue(groups)
        row = next(g for g in groups if g['key'] == 'codex.logs')
        self.assertEqual((row['tier'], row['domain'], row['selector']), ('junk', 'agent', None))
        self.assertEqual(row['label'], 'Codex logs')
        self.assertTrue(row['effect'])
        self.assertEqual(row['selection']['selected'], 0)
        result = self.app.apply_ops(JUNK_ALL)
        self.assertEqual(set(result), {'spec', 'scan_id', 'summary', 'digest', 'resolved_at'})
        self.assertEqual(result['scan_id'], self.sid)
        self.assertGreater(result['summary']['count'], 3)
        self.assertEqual(result['spec']['version'], 1)
        self.assertEqual(self.app.spec().to_json(), result['spec'])
        row = next(g for g in self.app.groups() if g['key'] == 'codex.logs')
        self.assertEqual((row['selector'], row['selection']['selected']), ({'min_age_days': None, 'action': None}, 1))
        json.dumps(result)                                             # everything JSON-safe
        plan = self.app.create_plan('default', {'spec_version': 1, 'digest': result['digest'], 'scan_id': result['scan_id']})
        self.assertEqual((plan['state'], plan['revision'], plan['scan_id']), ('draft', 1, self.sid))
        self.assertEqual(plan['entries'], {'total': result['summary']['count'], 'by_state': {'pending': result['summary']['count']}})
        self.assertEqual(plan['totals']['count'], result['summary']['count'])
        page = self.app.entries(plan['id'])
        self.assertEqual(page['total'], result['summary']['count'])
        self.assertEqual([r['seq'] for r in page['rows']], list(range(1, page['total'] + 1)))
        self.assertEqual(page['rows'][0]['state'], 'pending')
        self.assertEqual(self.app.plan(plan['id']), plan)
        json.dumps(page)

    def test_entries_paging_and_state_filter(self):
        result = self.app.apply_ops(JUNK_ALL)
        plan = self.app.create_plan('default', {'spec_version': result['spec']['version'], 'digest': result['digest'],
                                                'scan_id': result['scan_id']})
        n = plan['entries']['total']
        self.assertEqual([r['seq'] for r in self.app.entries(plan['id'], offset=2, limit=3)['rows']], [3, 4, 5])
        self.assertEqual(self.app.entries(plan['id'], limit=None)['total'], n)
        self.assertEqual(len(self.app.entries(plan['id'], limit=None)['rows']), n)
        self.assertEqual(self.app.entries(plan['id'], state='done'), {'total': 0, 'rows': []})
        self.expect(ErrorCode.INVALID, 'error.invalid_op', self.app.entries, plan['id'], state='bogus')
        self.expect(ErrorCode.NOT_FOUND, 'error.NOT_FOUND', self.app.entries, 'nope')
        self.expect(ErrorCode.NOT_FOUND, 'error.NOT_FOUND', self.app.plan, 'nope')

    def test_exclude_bumps_the_revision(self):
        result = self.app.apply_ops(JUNK_ALL)
        plan = self.app.create_plan('default', {'spec_version': result['spec']['version'], 'digest': result['digest'],
                                                'scan_id': result['scan_id']})
        self.assertEqual(self.app.exclude(plan['id'], [1, 2], plan['revision']), 2)
        after = self.app.plan(plan['id'])
        self.assertEqual((after['revision'], after['entries_digest']), (2, plan['entries_digest']))
        self.assertEqual(after['entries']['by_state']['excluded'], 2)
        self.expect(ErrorCode.REVISION_MISMATCH, 'error.REVISION_MISMATCH', self.app.exclude, plan['id'], [3], 1)

    def test_empty_selection_cannot_become_a_plan(self):
        result = self.app.apply_ops([{'op': 'select_group', 'group': 'claude.logs', 'on': True}])   # 5 days old, filter 14
        e = self.expect(ErrorCode.EMPTY_PLAN, 'error.EMPTY_PLAN', self.app.create_plan, 'default',
                        {'spec_version': result['spec']['version'], 'digest': result['digest'],
                         'scan_id': result['scan_id']})
        self.assertEqual(e.details['breakdown']['too_recent']['count'], 1)

    def test_a_clock_crossing_a_threshold_is_reported_with_the_item_named(self):
        result = self.app.apply_ops([{'op': 'select_group', 'group': 'claude.logs', 'on': True}])
        self.fh.clock.advance(days=10)
        e = self.expect(ErrorCode.DIGEST_MISMATCH, 'error.DIGEST_MISMATCH', self.app.create_plan, 'default',
                        {'spec_version': result['spec']['version'], 'digest': result['digest'],
                         'scan_id': result['scan_id']})
        self.assertEqual([a['path'] for a in e.details['added']], [LOG])
        self.assertEqual((e.details['added_count'], e.details['removed_count']), (1, 0))
        fresh = self.app.apply_ops([])       # the client looks again and gets the new digest
        plan = self.app.create_plan('default', {'spec_version': fresh['spec']['version'], 'digest': fresh['digest'],
                                                'scan_id': fresh['scan_id']})
        self.assertEqual(plan['entries']['total'], 1)

    def test_create_plan_needs_a_mapping(self):
        self.expect(ErrorCode.INVALID, 'error.invalid_op', self.app.create_plan, 'default', None)

    def test_sessions_are_independent(self):
        self.app.apply_ops(JUNK_ALL, session='a')
        self.assertEqual(self.app.spec('b').groups, {})
        self.assertEqual(self.app.spec('b').version, 0)
        self.assertGreater(len(self.app.spec('a').groups), 3)

    def test_fresh_spec_uses_the_default_filter_from_settings(self):
        self.assertEqual(self.app.spec().filter_min_age_days, 14)
        self.app.update_settings({'default_min_age_days': 30})
        self.assertEqual(self.app.spec('new').filter_min_age_days, 30)


class ItemsAndGroupsTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.app.scan()

    def test_groups_filters_and_text(self):
        junk = self.app.groups(tier='junk')
        self.assertTrue(junk and all(g['tier'] == 'junk' for g in junk))
        self.assertTrue(all(g['domain'] == 'agent' for g in self.app.groups(domain='agent')))
        self.assertEqual(self.app.groups(domain='app-library'), [])
        self.expect(ErrorCode.INVALID, 'error.invalid_op', self.app.groups, tier='gold')
        self.expect(ErrorCode.INVALID, 'error.invalid_op', self.app.groups, domain='moon')
        dup = next(g for g in self.app.groups() if g['key'] == 'codex.sessions:duplicates')
        self.assertEqual(dup['label'], 'Codex sessions — legacy-ID copies')
        self.app.locale = 'zh-CN'
        dup = next(g for g in self.app.groups() if g['key'] == 'codex.sessions:duplicates')
        self.assertEqual(dup['label'], 'Codex 会话 — 旧 ID 副本')
        kept = next(g for g in self.app.groups() if g['key'] == 'claude.cli-versions#protected')
        self.assertTrue(kept['label'].endswith('（保留）'))

    def test_items_rows_carry_the_decision(self):
        self.app.apply_ops([{'op': 'select_group', 'group': 'claude.projects:~/.claude/projects/-Users-me-proj', 'on': True},
                            {'op': 'set_filter', 'min_age_days': 14}])
        page = self.app.items(group='claude.projects:~/.claude/projects/-Users-me-proj')
        self.assertEqual(page['total'], 3)
        for row in page['rows']:
            self.assertEqual(row['decision']['state'], 'selected')     # all 20 days old
            self.assertEqual((row['decision']['action'], row['decision']['via']), ('archive', 'group'))
            self.assertEqual(row['tier'], 'review')
        other = self.app.items(group='codex.logs')['rows'][0]
        self.assertEqual(other['decision']['state'], 'unselected')
        self.assertEqual(other['decision']['reason_key'], 'reason.unselected')

    def test_items_paging_ordering_query_and_keys(self):
        everything = self.app.items(limit=None)
        self.assertEqual(everything['total'], len(everything['rows']))
        page = self.app.items(order='path', offset=1, limit=2)
        self.assertEqual(page['total'], everything['total'])
        self.assertEqual([r['path'] for r in page['rows']], sorted(r['path'] for r in everything['rows'])[1:3])
        self.assertEqual([r['path'] for r in self.app.items(query='X.TXT')['rows']], [LOG])
        self.assertEqual(self.app.items(query='x.txt')['total'], 1)
        key = item_key(LOG)
        self.assertEqual([r['key'] for r in self.app.items(keys=[key])['rows']], [key])
        self.expect(ErrorCode.INVALID, 'error.INVALID', self.app.items, order='bogus')
        self.expect(ErrorCode.NOT_FOUND, 'error.NOT_FOUND', self.app.items, group='no.such.group')


class ResolutionCacheTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.app.scan()
        base = int(self.fh.clock()) // 5 * 5 + 5      # the start of a cache bucket
        self.fh.clock.set(base + 0.5)

    def count_resolves(self):
        return mock.patch('disk_steward.service.resolve', side_effect=selection.resolve)

    def test_paging_reuses_one_resolution(self):
        with self.count_resolves() as m:
            self.app.items(limit=3)
            self.app.items(offset=3, limit=3)
            self.app.groups()
            self.assertEqual(m.call_count, 1)

    def test_a_new_spec_version_resolves_again(self):
        with self.count_resolves() as m:
            self.app.items()
            self.app.apply_ops([{'op': 'select_group', 'group': 'codex.logs', 'on': True}])
            self.assertEqual(m.call_count, 2)
            self.app.items()
            self.app.groups()
            self.assertEqual(m.call_count, 2)       # apply_ops left its resolution in the cache
            self.app.apply_ops([{'op': 'select_group', 'group': 'codex.logs', 'on': False}])
            self.assertEqual(m.call_count, 3)

    def test_the_cache_expires_after_five_seconds(self):
        with self.count_resolves() as m:
            self.app.items()
            self.fh.clock.advance(seconds=3)
            self.app.items()
            self.assertEqual(m.call_count, 1)
            self.fh.clock.advance(seconds=3)
            self.app.items()
            self.assertEqual(m.call_count, 2)

    def test_sessions_do_not_share_cached_resolutions(self):
        with self.count_resolves() as m:
            self.app.items(session='a')
            self.app.items(session='b')
            self.assertEqual(m.call_count, 2)

    def test_settings_changes_drop_the_cache(self):
        self.app.apply_ops([{'op': 'select_tier', 'tier': 'junk', 'on': True}, {'op': 'set_filter', 'min_age_days': 0}])
        before = self.app.items(group='codex.logs')['rows'][0]['decision']['state']
        self.app.update_settings({'domains': ['app-library']})
        after = self.app.items(group='codex.logs')['rows'][0]['decision']['state']
        self.assertEqual((before, after), ('selected', 'domain_off'))


class QualifyTests(ServiceCase):
    """The per-group figures Browse shows next to the selection summary (claude.logs: 5 days old, rule floor 1 day)."""

    PROJ = 'claude.projects:~/.claude/projects/-Users-me-proj'    # three 20-day-old review items

    def setUp(self):
        super().setUp()
        self.app.scan()

    def test_only_the_selection_calls_carry_the_figures(self):
        self.assertNotIn('qualify', self.app.selection())
        self.assertNotIn('qualify', self.app.apply_ops([]))
        sel = self.app.selection(qualify=True)
        self.assertEqual(sel['qualify'], self.app.apply_ops([], qualify=True)['qualify'])
        keys = {g['key'] for g in self.app.groups() if g['tier'] != 'protected' and g['actions']}
        self.assertEqual(set(sel['qualify']), keys)

    def test_ticked_rows_add_up_to_the_summary(self):
        out = self.app.apply_ops(JUNK_ALL[:1] + [{'op': 'select_group', 'group': self.PROJ, 'on': True},
                                                  {'op': 'set_filter', 'min_age_days': 3},
                                                  {'op': 'toggle_item', 'item': item_key(LOG), 'on': False}], qualify=True)
        ticked = [k for k in out['qualify'] if k in out['spec']['groups']]
        self.assertTrue(ticked)
        self.assertEqual(sum(out['qualify'][k]['items'] for k in ticked), out['summary']['count'])
        self.assertEqual(sum(out['qualify'][k]['alloc'] for k in ticked), out['summary']['alloc'])
        self.assertEqual(out['qualify']['claude.logs']['excluded'], 1)
        for k in ticked:   # what a ticked row shows is what the summary holds for that group
            self.assertEqual((out['qualify'][k]['items'], out['qualify'][k]['alloc']),
                             (out['summary']['groups'][k]['selected'], out['summary']['groups'][k]['alloc']))

    def test_an_unticked_group_shows_what_it_would_select(self):
        before = self.app.apply_ops([{'op': 'clear'}, {'op': 'set_filter', 'min_age_days': 14}], qualify=True)
        self.assertEqual(before['summary']['count'], 0)
        would = before['qualify'][self.PROJ]
        self.assertEqual((would['items'], would['total']), (3, 3))
        after = self.app.apply_ops([{'op': 'select_group', 'group': self.PROJ, 'on': True}], qualify=True)
        self.assertEqual(after['qualify'][self.PROJ], would)
        self.assertEqual((after['summary']['count'], after['summary']['alloc']), (would['items'], would['alloc']))

    def test_a_group_of_only_too_recent_items_says_zero_and_why(self):
        out = self.app.apply_ops([{'op': 'clear'}, {'op': 'set_filter', 'min_age_days': 14}], qualify=True)
        logs = out['qualify']['claude.logs']
        self.assertEqual((logs['items'], logs['alloc'], logs['total'], logs['too_recent']), (0, 0, 1, 1))
        self.assertEqual((logs['min_age_days'], logs['floor_days']), (14, 1))
        out = self.app.apply_ops([{'op': 'set_filter', 'min_age_days': 3}], qualify=True)
        self.assertEqual((out['qualify']['claude.logs']['items'], out['qualify']['claude.logs']['too_recent']), (1, 0))

    def test_the_figures_are_resolved_once_per_selection(self):
        with mock.patch('disk_steward.service.qualify_groups', side_effect=selection.qualify_groups) as m:
            self.app.apply_ops([{'op': 'select_group', 'group': 'codex.logs', 'on': True}], qualify=True)
            self.app.selection(qualify=True)
            self.app.selection(qualify=True)
            self.assertEqual(m.call_count, 1)
            self.app.apply_ops([{'op': 'select_group', 'group': 'codex.logs', 'on': False}], qualify=True)
            self.assertEqual(m.call_count, 2)

    def test_changed_settings_recompute_them(self):
        self.app.apply_ops([{'op': 'clear'}, {'op': 'set_filter', 'min_age_days': 0}])
        self.assertEqual(self.app.selection(qualify=True)['qualify']['codex.logs']['items'], 1)
        self.app.update_settings({'domains': ['app-library']})
        logs = self.app.selection(qualify=True)['qualify']['codex.logs']
        self.assertEqual((logs['items'], logs['other']), (0, 1))


class SpecMigrationTests(ServiceCase):
    def test_a_rescan_carries_the_selection_over(self):
        first = self.app.scan()
        proj = 'claude.projects:~/.claude/projects/-Users-me-proj'
        a_jsonl = '~/.claude/projects/-Users-me-proj/a.jsonl'
        self.app.apply_ops([
            {'op': 'select_group', 'group': 'codex.logs', 'on': True},
            {'op': 'select_group', 'group': proj, 'on': True},
            {'op': 'select_group', 'group': 'codex.cache', 'on': True},                       # will vanish
            {'op': 'toggle_item', 'item': item_key(LOG), 'on': True},                          # will vanish
            {'op': 'toggle_item', 'item': item_key(a_jsonl), 'on': False},                     # will vanish
            {'op': 'toggle_item', 'item': item_key('~/.claude/file-history/s1/f@v1'), 'on': True, 'action': 'delete'},
            {'op': 'set_filter', 'min_age_days': 9}])
        self.app.apply_ops([{'op': 'select_group', 'group': 'codex.logs', 'on': True}], session='other')
        before = self.app.spec()
        self.unlink(LOG)
        self.unlink(a_jsonl)
        self.unlink('~/.codex/cache/models.json')
        second = self.app.scan()
        self.assertEqual(second, first + 1)
        spec = self.app.spec()
        self.assertEqual(spec.scan_id, second)
        self.assertEqual(set(spec.groups), {'codex.logs', proj})
        self.assertEqual(spec.include, {item_key('~/.claude/file-history/s1/f@v1'): Action.DELETE})
        self.assertEqual(spec.exclude, frozenset([item_key(a_jsonl)]))               # exclusions are never dropped
        self.assertEqual(spec.filter_min_age_days, 9)
        self.assertEqual(spec.version, before.version + 1)
        self.assertEqual(self.app.store.load_spec('default'), spec)                # saved, not only returned
        done = [e for e in self.app.events.since(0) if e['type'] == 'scan.done'][-1]['data']
        notes = done['migrated']['default']
        self.assertEqual((notes['from_scan'], notes['to_scan']), (first, second))
        self.assertEqual(notes['dropped_groups'], ['codex.cache'])
        self.assertEqual(notes['include_missing'], {'count': 1, 'paths': [LOG]})
        self.assertEqual(notes['include_changed']['count'], 0)
        self.assertEqual(notes['kept_excludes'], 1)
        self.assertEqual(done['migrated']['other']['dropped_groups'], [])
        self.assertEqual(self.app.spec_notes(), notes)                                # kept for every client ...
        self.assertEqual(json.loads(self.app.store.meta('spec_notes:default')), notes)
        self.app.apply_ops([])
        self.assertIsNone(self.app.spec_notes())                                      # ... until the next edit
        self.assertEqual(self.app.store.load_spec('other').scan_id, second)
        # and it still resolves against the new scan
        self.assertGreater(self.app.apply_ops([])['summary']['count'], 0)

    def test_the_old_scan_goes_after_the_selection_moved_and_the_notes_keep_its_figures(self):
        first = self.app.scan()
        self.app.apply_ops([{'op': 'select_group', 'group': 'codex.logs', 'on': True}, {'op': 'set_filter', 'min_age_days': 0}])
        before = self.app.apply_ops([])['summary']['groups']['codex.logs']
        self.assertGreater(before['selected'], 0)
        self.assertGreater(self.app.store.count_items(first), 0)
        second = self.app.scan()
        self.assertEqual((self.app.store.count_items(first), self.app.store.groups(first)), (0, []))
        self.assertGreater(self.app.store.count_items(second), 0)
        change = {c['group']: c for c in self.app.spec_notes()['group_changes']}['codex.logs']
        self.assertEqual((change['old_selected'], change['old_alloc']), (before['selected'], before['alloc']))
        self.assertEqual((change['new_selected'], change['new_alloc']), (before['selected'], before['alloc']))
        self.assertEqual(self.app.store.scan(first)['status'], 'done')

    def test_a_failed_migration_still_prunes_the_old_scan(self):
        first = self.app.scan()
        self.app.apply_ops([{'op': 'select_group', 'group': 'codex.logs', 'on': True}])
        with mock.patch.object(App, '_migrate_specs', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                self.app.scan()
        self.assertEqual(self.app.store.count_items(first), 0)
        self.assertGreater(self.app.store.count_items(self.app.latest_scan()['id']), 0)

    def test_an_include_survives_a_rescan_only_when_the_file_is_the_one_that_was_picked(self):
        keep, rewritten = '~/.claude/file-history/s1/f@v1', LOG
        self.app.scan()
        self.app.apply_ops([{'op': 'toggle_item', 'item': item_key(keep), 'on': True},
                            {'op': 'toggle_item', 'item': item_key(rewritten), 'on': True}])
        self.assertEqual(self.app.store.include_fingerprints('default', [item_key(keep), item_key(rewritten)]).keys(),
                         {item_key(keep), item_key(rewritten)})
        self.unlink(rewritten)
        self.fh.file(rewritten, size=10, age_days=5)                                  # same path, new inode
        second = self.app.scan()
        spec = self.app.spec()
        self.assertEqual((spec.scan_id, set(spec.include)), (second, {item_key(keep)}))
        notes = self.app.spec_notes()
        self.assertEqual((notes['include_changed'], notes['include_missing']),
                         ({'count': 1, 'paths': [rewritten]}, {'count': 0, 'paths': []}))
        self.assertEqual(set(self.app.store.include_fingerprints('default', [item_key(keep), item_key(rewritten)])),
                         {item_key(keep)})                                            # the dropped include is forgotten

    def test_a_spec_saved_for_an_older_scan_is_migrated_when_loaded(self):
        first = self.app.scan()
        self.app.scan()
        self.app.store.save_spec('late', Spec(scan_id=first, version=3, groups={'codex.logs': GroupSel(), 'gone': GroupSel()}))
        spec = self.app.spec('late')
        self.assertEqual((spec.scan_id, spec.version, set(spec.groups)), (self.app.latest_scan()['id'], 4, {'codex.logs'}))
        self.assertEqual(self.app.store.load_spec('late'), spec)

    def test_a_client_holding_the_old_scan_is_told_what_the_carry_over_changed(self):
        first = self.app.scan()
        seen = self.app.apply_ops([{'op': 'select_group', 'group': 'codex.logs', 'on': True},
                                   {'op': 'select_group', 'group': 'codex.cache', 'on': True}])
        self.unlink('~/.codex/cache/models.json')
        second = self.app.scan()
        e = self.expect(ErrorCode.INVALID, 'error.spec_stale', self.app.create_plan, 'default',
                        {'spec_version': seen['spec']['version'], 'digest': seen['digest'], 'scan_id': first})
        self.assertEqual((e.details['from_scan'], e.details['to_scan']), (first, second))
        self.assertEqual(e.details['notes']['dropped_groups'], ['codex.cache'])

    def test_planner_refuses_a_stale_spec(self):
        first = self.app.scan()
        self.app.scan()
        from disk_steward import planner
        stale = Spec(scan_id=first, groups={'codex.logs': GroupSel()})
        self.expect(ErrorCode.INVALID, 'error.spec_stale', planner.create_plan, self.app.store, stale, self.fh.clock(),
                    {'spec_version': 0, 'digest': 'x', 'scan_id': first}, self.app.settings, self.app.mac)


class DiagnoseTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.app.scan()

    def test_session_file_gives_counts_only(self):
        out = self.app.diagnose_session(item_key(fx.PAIR_A))
        self.assertEqual(set(out), {'bytes', 'lines', 'invalid_lines', 'by_type', 'settings', 'changed_during_read'})
        self.assertEqual((out['lines'], out['invalid_lines']), (21, 0))
        self.assertEqual(out['by_type']['session_meta']['count'], 1)
        text = json.dumps(out)
        for private in ('hello', '/secret/project/path'):
            self.assertNotIn(private, text)

    def test_archived_session_is_allowed(self):
        self.assertEqual(self.app.diagnose_session(item_key(fx.ARCHIVED))['lines'], 5)

    def test_other_items_are_refused(self):
        for path in (LOG, '~/.claude/projects/-Users-me-proj/a.jsonl', '~/.codex/log/codex-tui.log'):
            with self.subTest(path=path):
                self.expect(ErrorCode.INVALID, 'error.not_a_session', self.app.diagnose_session, item_key(path))

    def test_unknown_key_and_vanished_file(self):
        self.expect(ErrorCode.NOT_FOUND, 'error.NOT_FOUND', self.app.diagnose_session, 'f' * 20)
        self.unlink(fx.SOLO)
        e = self.expect(ErrorCode.NOT_FOUND, 'error.NOT_FOUND', self.app.diagnose_session, item_key(fx.SOLO))
        self.assertEqual(e.details['id'], fx.SOLO)


class SettingsTests(ServiceCase):
    def test_update_persists_and_validates(self):
        out = self.app.update_settings({'default_min_age_days': 21, 'idle_days': 90})
        self.assertEqual((out.default_min_age_days, out.idle_days), (21, 90))
        self.assertIs(self.app.settings, out)
        saved = json.loads((self.fh.data_dir / 'settings.json').read_text())
        self.assertEqual((saved['default_min_age_days'], saved['idle_days']), (21, 90))
        self.expect(ErrorCode.INVALID, 'error.invalid_setting', self.app.update_settings, {'idle_days': -1})
        self.expect(ErrorCode.INVALID, 'error.invalid_setting', self.app.update_settings, {'no_such_setting': 1})
        self.assertEqual(self.app.settings.idle_days, 90)
        self.assertEqual(json.loads((self.fh.data_dir / 'settings.json').read_text())['idle_days'], 90)

    def test_locale_change_applies_at_once(self):
        self.app.update_settings({'locale': 'zh-CN'})
        self.assertEqual(self.app.locale, 'zh-CN')
        self.app.update_settings({'locale': 'en'})
        self.assertEqual(self.app.locale, 'en')


if __name__ == '__main__':
    unittest.main()
