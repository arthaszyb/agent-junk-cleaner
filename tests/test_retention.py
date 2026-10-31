"""Retention: a complete run summary for every finished plan, compaction of its entries after a week, and the file
shrinking back."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import m2_fixture as fx
from disk_steward import i18n, service
from disk_steward.model import Action, EntryState, ErrorCode, Outcome, PlanState, StewardError
from disk_steward.portal.api import Api
from disk_steward.settings import Settings
from disk_steward.store import Store
from m3_support import make_app, make_mac
from m4_support import JUNK_14D, JUNK_ALL, make_plan
from support import Clock, FakeHome
from test_store import make_entry, make_plan as make_plan_row

DAY = 86400.0
CLAUDE_APP = 'com.anthropic.claudefordesktop'
NEW_KEYS = {'finished_at', 'items_done', 'items_skipped', 'items_failed', 'items_excluded', 'items_needs_attention',
            'items_cancelled', 'removed', 'by_group', 'skipped_reasons', 'counts', 'freed', 'moved_to_trash'}
EXECUTOR_KEYS = {'plan_id', 'state', 'total', 'by_action', 'removed_alloc', 'free_before', 'free_after',
                 'estimated_delta', 'measured_delta', 'snapshots', 'warnings', 'seconds', 'rescan'}


def tearDownModule():
    if i18n.MISSING:
        raise AssertionError(f'untranslated keys: {sorted(i18n.MISSING)}')


class RetentionCase(unittest.TestCase):
    running = ()

    def setUp(self):
        self.clock = Clock()
        self.fh = FakeHome(self.clock).__enter__()
        self.addCleanup(self.fh.cleanup)
        fx.build(self.fh)
        self.mac = make_mac(running=self.running, snapshot_names=['com.apple.1'])
        self.app = self.open_app()
        self.app.scan()

    def open_app(self):
        app = make_app(self.fh, mac=self.mac, settings=Settings(domains=('agent', 'app-library')))
        self.addCleanup(app.close)
        return app

    def run_plan(self, ops=JUNK_14D):
        plan = make_plan(self.app, ops)
        self.app.run_plan(plan['id'], plan['revision'], plan['entries_digest'])
        return plan['id']

    def audit_events(self):
        path = self.fh.data_dir / 'audit' / 'audit.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()]

    def view(self, plan_id):
        """Everything a screen shows about a finished plan."""
        plan = self.app.plan(plan_id)
        plan.pop('compacted_at')
        history = [r for r in self.app.insights(30, 'en')['history']['cleanups'] if r['plan_id'] == plan_id]
        return {'plan': plan, 'result': self.app.plan_result(plan_id), 'history': history,
                'groups': [{k: v for k, v in g.items() if k not in ('actions',)} for g in self.app.plan_groups(plan_id)],
                'freed': self.app.insights(30, 'en')['kpis']['freed_total']}


class SummaryTests(RetentionCase):
    running = (CLAUDE_APP,)

    def test_a_finished_run_leaves_a_complete_summary(self):
        plan_id = self.run_plan(JUNK_ALL)        # the running Claude Desktop makes some entries skip
        summary = self.app.store.run_summary(plan_id)
        self.assertGreaterEqual(set(summary), NEW_KEYS | EXECUTOR_KEYS)
        done = self.app.entries(plan_id, state='done', limit=None)['rows']
        skipped = self.app.entries(plan_id, state='skipped', limit=None)['rows']
        self.assertTrue(done and skipped)
        self.assertEqual((summary['items_done'], summary['items_skipped']), (len(done), len(skipped)))
        self.assertEqual((summary['items_failed'], summary['items_excluded'], summary['items_needs_attention'],
                          summary['items_cancelled']), (0, 0, 0, 0))
        self.assertEqual(summary['removed'], sum(e['alloc'] for e in done))
        self.assertEqual(summary['freed'], sum(e['freed'] for e in done))
        self.assertEqual(summary['moved_to_trash'], 0)
        self.assertEqual(summary['skipped_reasons'], {r: sum(1 for e in skipped if e['reason'] == r)
                                                      for r in {e['reason'] for e in skipped}})
        groups = {}
        for e in done:
            g = groups.setdefault(e['group_key'], {'done': 0, 'alloc': 0, 'freed': 0, 'actions': []})
            g['done'] += 1
            g['alloc'] += e['alloc']
            g['freed'] += e['freed']
            if e['action'] not in g['actions']:
                g['actions'].append(e['action'])
        self.assertEqual(summary['by_group'], groups)
        self.assertEqual(sum(g['done'] for g in summary['by_group'].values()), summary['items_done'])
        self.assertEqual(summary['counts'], {'done': len(done), 'skipped': len(skipped)})
        self.assertIsNotNone(summary['measured_delta'])
        self.assertEqual(summary['estimated_delta'], summary['freed'])

    def test_finished_at_is_the_last_thing_the_run_did(self):
        plan_id = self.run_plan(JUNK_ALL)
        conn = self.app.store._conn()
        last = conn.execute('SELECT MAX(updated_at) FROM plan_entries WHERE plan_id = ?', (plan_id,)).fetchone()[0]
        self.assertEqual(self.app.store.run_summary(plan_id)['finished_at'], last)

    def test_the_result_the_portal_shows_is_that_summary(self):
        plan_id = self.run_plan(JUNK_ALL)
        result = self.app.plan_result(plan_id)
        self.assertEqual(result, self.app.store.run_summary(plan_id))
        self.assertEqual(result['rescan'], {'scan_id': self.app.latest_scan()['id']})
        self.assertNotIn('synthesized', result)          # the rescan's own retention pass did not replace it

    def test_a_cancelled_plan_counts_what_it_did_not_do(self):
        plan = make_plan(self.app, JUNK_14D)
        rows = self.app.entries(plan['id'], limit=None)['rows']
        for e in rows[:2]:
            self.app.store.finish_entry(plan['id'], e['seq'], Outcome(EntryState.DONE, '', freed=e['alloc']))
        self.app.store.finish_entry(plan['id'], rows[2]['seq'], Outcome(EntryState.NEEDS_ATTENTION, 'x'))
        for e in rows[3:]:
            self.app.store.finish_entry(plan['id'], e['seq'], Outcome(EntryState.CANCELLED, 'cancelled'))
        self.app.store.set_plan_state(plan['id'], PlanState.CANCELLED)
        summary = self.app.store.save_run_summary(plan['id'])
        self.assertTrue(summary['synthesized'])
        self.assertEqual((summary['items_done'], summary['items_needs_attention'], summary['items_cancelled']),
                         (2, 1, len(rows) - 3))
        self.assertEqual(summary['state'], 'cancelled')
        self.assertIsNone(self.app.plan_result(plan['id']))      # no run figures, so no "result" to show

    def test_a_summary_from_an_older_version_is_completed_not_replaced(self):
        plan_id = self.run_plan(JUNK_ALL)
        old = {k: v for k, v in self.app.store.run_summary(plan_id).items() if k not in NEW_KEYS - {'counts', 'freed',
                                                                                                    'moved_to_trash'}}
        self.app.store.set_meta(f'run:{plan_id}', json.dumps(old))
        done = self.app.store.save_run_summary(plan_id)
        self.assertGreaterEqual(set(done), NEW_KEYS | EXECUTOR_KEYS)
        self.assertEqual(done['measured_delta'], old['measured_delta'])
        self.assertNotIn('synthesized', done)
        self.assertEqual(self.app.store.save_run_summary(plan_id), done)      # nothing more to add


class CompactionTests(RetentionCase):
    running = (CLAUDE_APP,)

    def finished(self):
        plan_id = self.run_plan(JUNK_ALL)
        self.assertGreater(self.app.store.run_summary(plan_id)['items_skipped'], 0)
        return plan_id

    def test_nothing_goes_before_a_week_and_the_done_entries_go_at_a_week(self):
        plan_id = self.finished()
        before = self.app.plan(plan_id)['entries']
        self.clock.advance(days=service.COMPACT_AFTER_DAYS - 0.01)
        self.assertEqual(self.app.compact_plans(), 0)
        self.assertEqual(self.app.entries(plan_id, state='done')['total'], before['by_state']['done'])
        self.clock.advance(days=0.02)
        self.assertEqual(self.app.compact_plans(), 1)
        self.assertEqual(self.app.entries(plan_id, state='done'), {'total': 0, 'rows': []})
        kept = self.app.entries(plan_id, limit=None)
        self.assertEqual({r['state'] for r in kept['rows']}, {'skipped'})      # the rows that explain outcomes stay
        self.assertEqual(kept['total'], before['by_state']['skipped'])
        self.assertEqual(self.app.compact_plans(), 0)                           # once

    def test_every_screen_shows_the_same_before_and_after(self):
        plan_id = self.finished()
        before = self.view(plan_id)
        self.assertEqual(self.app.plan(plan_id)['compacted_at'], None)
        self.clock.advance(days=8)
        self.assertEqual(self.app.compact_plans(), 1)
        self.assertEqual(self.app.plan(plan_id)['compacted_at'], self.clock())
        self.assertEqual(self.view(plan_id), before)

    def test_history_takes_the_finish_time_from_the_summary_when_it_has_one(self):
        plan_id = self.finished()
        summary = self.app.store.run_summary(plan_id)
        summary['finished_at'] += 3
        self.app.store.set_meta(f'run:{plan_id}', json.dumps(summary))
        row = self.app.insights(30, 'en')['history']['cleanups'][0]
        self.assertEqual(row['finished_at'], summary['finished_at'])

    def test_the_portals_plan_page_reads_the_summary_too(self):
        plan_id = self.finished()
        api = Api(self.app)

        def page():
            body = api.dispatch('GET', f'/api/plans/{plan_id}', {}, None, 'en')[1]
            body.pop('compacted_at')
            return body
        before = page()
        self.clock.advance(days=8)
        self.app.compact_plans()
        after = page()
        self.assertEqual(after, before)
        self.assertEqual(after['entries']['by_state']['done'], self.app.store.run_summary(plan_id)['items_done'])
        listed = api.dispatch('GET', '/api/plans', {}, None, 'en')[1]['finished']
        self.assertEqual([p['id'] for p in listed], [plan_id])
        self.assertEqual(listed[0]['entries'], before['entries'])

    def test_the_audit_log_records_the_compaction(self):
        plan_id = self.finished()
        done = self.app.store.run_summary(plan_id)['items_done']
        self.clock.advance(days=8)
        self.app.compact_plans()
        event = [e for e in self.audit_events() if e['event'] == 'plan.compacted']
        self.assertEqual([(e['plan_id'], e['removed_entries']) for e in event], [(plan_id, done)])

    def test_it_runs_at_start_and_after_a_scan(self):
        plan_id = self.finished()
        self.clock.advance(days=8)
        self.assertEqual(self.app.store.compacted_at(plan_id), None)
        self.app.scan()
        self.assertEqual(self.app.store.compacted_at(plan_id), self.clock())
        second = self.finished()
        self.clock.advance(days=8)
        self.assertIsNone(self.app.store.compacted_at(second))
        self.open_app()                                              # a new process
        self.assertEqual(self.app.store.compacted_at(second), self.clock())

    def test_it_runs_after_a_run(self):
        first = self.finished()
        self.clock.advance(days=8)
        second = self.finished()
        self.assertEqual(self.app.store.compacted_at(first), self.clock())
        self.assertIsNone(self.app.store.compacted_at(second))      # it finished just now

    def test_it_also_runs_after_a_run_whose_rescan_failed(self):
        first = self.finished()
        self.clock.advance(days=8)
        with mock.patch.object(self.app, 'scan', side_effect=RuntimeError('boom')):
            second = self.finished()
        self.assertEqual(self.app.store.run_summary(second)['rescan'], {'error': 'RuntimeError: boom'})
        self.assertEqual(self.app.store.compacted_at(first), self.clock())

    def test_a_plan_without_a_summary_gets_one_first_and_is_still_there_afterwards(self):
        plan_id = self.finished()
        done = self.app.store.run_summary(plan_id)['items_done']
        self.app.store.delete_meta(f'run:{plan_id}')
        self.assertIsNone(self.app.plan_result(plan_id))
        before = self.app.plan(plan_id)['entries']
        history = [r for r in self.app.insights(30, 'en')['history']['cleanups']]
        self.clock.advance(days=8)
        self.app.compact_plans()
        summary = self.app.store.run_summary(plan_id)
        self.assertTrue(summary['synthesized'])
        self.assertEqual(summary['items_done'], done)
        self.assertIsNone(self.app.plan_result(plan_id))
        self.assertEqual(self.app.plan(plan_id)['entries'], before)
        after = self.app.insights(30, 'en')['history']['cleanups']
        self.assertEqual([{k: v for k, v in r.items() if k != 'finished_at'} for r in after],
                         [{k: v for k, v in r.items() if k != 'finished_at'} for r in history])

    def test_a_cancelled_plan_loses_its_done_and_cancelled_entries_and_keeps_the_rest(self):
        plan = make_plan(self.app, JUNK_ALL)
        rows = self.app.entries(plan['id'], limit=None)['rows']
        states = [EntryState.DONE, EntryState.CANCELLED, EntryState.SKIPPED, EntryState.FAILED, EntryState.EXCLUDED,
                  EntryState.NEEDS_ATTENTION]
        for e, state in zip(rows, states):
            self.app.store.finish_entry(plan['id'], e['seq'], Outcome(state, 'why', freed=e['alloc'] if
                                                                      state == EntryState.DONE else 0))
        for e in rows[len(states):]:
            self.app.store.finish_entry(plan['id'], e['seq'], Outcome(EntryState.CANCELLED, 'cancelled'))
        self.app.store.set_plan_state(plan['id'], PlanState.CANCELLED)
        before = self.app.plan(plan['id'])['entries']
        self.clock.advance(days=8)
        self.assertEqual(self.app.compact_plans(), 1)
        kept = self.app.entries(plan['id'], limit=None)['rows']
        self.assertEqual(sorted(r['state'] for r in kept), ['excluded', 'failed', 'needs_attention', 'skipped'])
        self.assertEqual(self.app.plan(plan['id'])['entries'], before)
        self.assertEqual(before['by_state']['cancelled'], len(rows) - 5)

    def test_draft_running_and_paused_plans_are_never_touched(self):
        draft = make_plan(self.app, JUNK_14D)['id']
        paused = make_plan(self.app, JUNK_ALL)['id']
        running = make_plan(self.app, JUNK_ALL)['id']
        for plan_id, state in ((paused, PlanState.PAUSED), (running, PlanState.RUNNING)):
            for e in self.app.entries(plan_id, limit=2)['rows']:
                self.app.store.finish_entry(plan_id, e['seq'], Outcome(EntryState.DONE, '', freed=1))
            self.app.store.set_plan_state(plan_id, state)
        counts = {p: self.app.store.count_entries(p) for p in (draft, paused, running)}
        self.clock.advance(days=60)
        self.assertEqual(self.app.compact_plans(), 0)
        self.assertEqual({p: self.app.store.count_entries(p) for p in counts}, counts)
        for plan_id in counts:
            with self.assertRaises(StewardError) as cm:
                self.app.store.compact_plan(plan_id, self.clock())
            self.assertEqual(cm.exception.code, ErrorCode.INVALID)
            self.assertIsNone(self.app.store.run_summary(plan_id))      # nothing was summarised either

    def test_a_plan_whose_summary_is_incomplete_is_refused(self):
        plan_id = self.finished()
        self.app.store.set_meta(f'run:{plan_id}', json.dumps({'state': 'done'}))
        with self.assertRaises(RuntimeError):
            self.app.store.compact_plan(plan_id, self.clock())
        self.assertGreater(self.app.store.count_entries(plan_id, EntryState.DONE), 0)

    def test_history_lists_compacted_and_fresh_plans_together_newest_first(self):
        first = self.finished()
        self.clock.advance(days=3)
        self.fh.file('~/.claude/debug/again.txt', size=500, age_days=20)
        self.app.scan()
        second = self.finished()
        self.clock.advance(days=5)
        self.app.compact_plans()
        self.assertIsNotNone(self.app.store.compacted_at(first))
        self.assertIsNone(self.app.store.compacted_at(second))
        rows = self.app.insights(30, 'en')['history']['cleanups']
        self.assertEqual([r['plan_id'] for r in rows], [second, first])
        self.assertEqual(self.app.insights(30, 'en')['kpis']['freed_total']['count'], 2)

    def test_a_failure_after_a_scan_is_published_and_audited_not_raised(self):
        plan_id = self.finished()
        self.clock.advance(days=8)
        with mock.patch.object(Store, 'compact_plan', side_effect=sqlite3.OperationalError('database is locked')):
            self.app.scan()                                          # still returns
        events = [e for e in self.app.events.since(0) if e['type'] == 'retention.failed']
        self.assertEqual(len(events), 1)
        self.assertIn('database is locked', events[0]['data']['error'])
        self.assertIn('retention.failed', [e['event'] for e in self.audit_events()])
        self.assertIsNone(self.app.store.compacted_at(plan_id))
        self.app.scan()                                              # the next pass tries again
        self.assertIsNotNone(self.app.store.compacted_at(plan_id))

    def test_at_start_a_failure_is_loud(self):
        self.finished()
        self.clock.advance(days=8)
        with mock.patch.object(Store, 'compact_plan', side_effect=sqlite3.OperationalError('database is locked')):
            with self.assertRaises(sqlite3.OperationalError):
                make_app(self.fh, mac=self.mac, settings=Settings(domains=('agent', 'app-library')))

    def test_the_note_exists_in_both_languages_and_names_the_period(self):
        for locale in i18n.LOCALES:
            text = i18n.t('plan.compacted_note', locale)
            self.assertNotEqual(text, 'plan.compacted_note')
            self.assertIn(str(service.COMPACT_AFTER_DAYS), text)


class ShrinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'steward.db'
        self.clock = Clock(1_790_000_000.0)
        self.store = Store(self.path, clock=self.clock)
        self.addCleanup(self.store.close)
        self.scan = self.store.begin_scan('c', 'f', 's', ['agent'])
        self.store.finish_scan(self.scan, 'done', 100, 50, 25)

    def big_plan(self, plan_id='p1', n=30_000, state=EntryState.DONE, plan_state=PlanState.DONE):
        entries = [make_entry(plan_id, i + 1, state=state, path=f'~/Library/Caches/some/long/enough/folder/{i}',
                              rule_id='app.caches', group_key='app.caches:~/Library/Caches/some') for i in range(n)]
        self.store.save_plan(make_plan_row(plan_id, scan_id=self.scan, state=plan_state), entries)

    def size(self):
        self.store._conn().execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()
        return os.path.getsize(self.path)

    def free_pages(self):
        return self.store._conn().execute('PRAGMA freelist_count').fetchone()[0]

    def test_compacting_a_big_plan_gives_the_file_back(self):
        self.big_plan()
        self.store.save_run_summary('p1')
        before = self.size()
        self.assertGreater(before, 2_000_000)
        removed = self.store.compact_plan('p1', self.clock())
        self.assertEqual(removed, 30_000)
        self.assertEqual(self.free_pages(), 0)
        self.assertLess(self.size(), before // 5)

    def test_discarding_a_big_draft_gives_the_file_back_too(self):
        self.big_plan(state=EntryState.PENDING, plan_state=PlanState.DRAFT)
        before = self.size()
        self.store.invalidate_drafts(['p1'], 'discarded')
        self.assertEqual(self.free_pages(), 0)
        self.assertLess(self.size(), before // 5)

    def test_free_pages_left_by_an_older_version_are_reclaimed_at_start(self):
        self.big_plan()
        conn = self.store._conn()
        conn.execute('DELETE FROM plan_entries WHERE seq <= 18000')   # rows gone, pages kept, as an older version left it
        pages = conn.execute('PRAGMA page_count').fetchone()[0]
        self.assertTrue(0.3 * pages < self.free_pages() < 0.8 * pages)
        before = self.size()
        self.assertTrue(self.store.shrink_if_fragmented())
        self.assertEqual(self.free_pages(), 0)
        self.assertLess(self.size(), before * 0.6)
        self.assertFalse(self.store.shrink_if_fragmented())

    def test_a_little_free_space_is_left_alone(self):
        self.big_plan(n=2000)
        conn = self.store._conn()
        conn.execute('DELETE FROM plan_entries WHERE seq <= 100')
        pages = conn.execute('PRAGMA page_count').fetchone()[0]
        free = self.free_pages()
        self.assertTrue(0 < free < 0.1 * pages)
        self.assertFalse(self.store.shrink_if_fragmented())
        self.assertEqual(self.free_pages(), free)

    def test_the_app_shrinks_such_a_file_when_it_starts(self):
        self.big_plan()
        self.store._conn().execute('DELETE FROM plan_entries')
        self.store.close()
        with FakeHome(Clock(1_790_000_000.0)) as fh:
            shutil.copyfile(self.path, fh.data_dir / 'steward.db')
            before = os.path.getsize(fh.data_dir / 'steward.db')
            app = make_app(fh)
            self.addCleanup(app.close)
            conn = app.store._conn()
            self.assertEqual(conn.execute('PRAGMA freelist_count').fetchone()[0], 0)
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()
            self.assertLess(os.path.getsize(fh.data_dir / 'steward.db'), before // 5)


if __name__ == '__main__':
    unittest.main()
