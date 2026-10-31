"""The App methods the portal relies on: start_scan, selection, pending_totals and validate_run."""
from __future__ import annotations

import sqlite3
import threading
import time
import unittest
from unittest import mock

import m2_fixture as fx
from disk_steward import planner
from disk_steward.locking import OperationLock
from disk_steward.model import ErrorCode, StewardError

from m3_support import make_app
from m4_support import JUNK_14D, make_plan
from support import Clock, FakeHome


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.fh = FakeHome(Clock()).__enter__()
        self.addCleanup(self.fh.cleanup)
        fx.build(self.fh)
        self.reached = threading.Event()
        self.gate = threading.Event()
        self.addCleanup(self.gate.set)
        self.app = make_app(self.fh, hooks={'after_claim': self.hold})
        self.addCleanup(self.app.close)

    def hold(self, entry):
        self.reached.set()
        self.gate.wait(30)

    def wait_for(self, condition, timeout=30):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail('timed out')
            time.sleep(0.01)

    def expect(self, code, key, fn, *args, **kw):
        with self.assertRaises(StewardError) as cm:
            fn(*args, **kw)
        self.assertEqual((cm.exception.code, cm.exception.message_key), (code, key))
        return cm.exception


class StartScanTests(ServiceCase):
    def test_scans_in_the_background_and_reports_progress_in_the_job(self):
        job = self.app.start_scan()
        self.assertEqual((job['status'], job['kind'], job['phase']), ('running', 'scan', 'scan'))
        self.wait_for(lambda: self.app.job()['status'] != 'running')
        job = self.app.job()
        self.assertEqual((job['status'], job['kind'], job['scan_id'], job['error']), ('done', 'scan', 1, None))
        self.assertEqual(set(job['scan']), {'phase', 'root', 'files', 'alloc', 'current'})
        self.assertEqual(self.app.latest_scan()['id'], 1)
        self.assertEqual([e['type'] for e in self.app.events.since(0)].count('scan.done'), 1)

    def test_only_the_requested_areas(self):
        self.app.start_scan(['agent'])
        self.wait_for(lambda: self.app.job()['status'] == 'done')
        self.assertEqual(self.app.latest_scan()['domains'], ['agent'])

    def test_a_bad_area_is_refused_before_anything_starts(self):
        self.expect(ErrorCode.INVALID, 'error.invalid_setting', self.app.start_scan, ['nowhere'])
        self.assertEqual(self.app.job()['status'], 'idle')
        self.assertIsNone(self.app.latest_scan())

    def test_a_second_start_and_a_run_are_busy(self):
        gate = threading.Event()
        original = self.app.scan
        with mock.patch.object(self.app, 'scan', side_effect=lambda *a, **kw: (gate.wait(30), original(*a, **kw))[1]):
            self.app.start_scan()
            self.expect(ErrorCode.BUSY, 'error.busy', self.app.start_scan)
            self.expect(ErrorCode.BUSY, 'error.busy', self.app._claim_run, 'x')
            self.expect(ErrorCode.BUSY, 'error.busy', self.app.update_settings, {'idle_days': 1})   # classification is locked
            self.app.update_settings({'locale': 'zh-CN'})                                           # the language is not
            gate.set()
            self.wait_for(lambda: self.app.job()['status'] == 'done')
        self.app.start_scan()   # the slot was given back
        self.wait_for(lambda: self.app.job()['scan_id'] == 2)

    def test_busy_while_another_process_holds_the_operation_lock(self):
        other = OperationLock(self.app.data_dir)   # same data dir: the in-process lock and the flock both refuse
        other.__enter__()
        try:
            self.expect(ErrorCode.BUSY, 'error.busy', self.app.start_scan)
            self.assertEqual(self.app.job()['status'], 'idle')
        finally:
            other.__exit__(None, None, None)
        self.app.start_scan()
        self.wait_for(lambda: self.app.job()['status'] == 'done')

    def test_cancel_stops_the_scan_and_leaves_no_scan_behind(self):
        gate = threading.Event()
        original = self.app.scan

        def slow(domains=None, progress=None, cancel=None):
            gate.wait(30)
            return original(domains, progress=progress, cancel=lambda: True)
        with mock.patch.object(self.app, 'scan', side_effect=slow):
            self.app.start_scan()
            self.app.cancel()
            gate.set()
            self.wait_for(lambda: self.app.job()['status'] != 'running')
        job = self.app.job()
        self.assertEqual((job['status'], job['error'], job['error_code']), ('idle', None, None))
        self.assertIsNone(self.app.latest_scan())

    def test_a_failing_scan_is_recorded_in_the_job(self):
        with mock.patch.object(self.app, 'scan', side_effect=RuntimeError('disk on fire')):
            self.app.start_scan()
            self.wait_for(lambda: self.app.job()['status'] != 'running')
        job = self.app.job()
        self.assertEqual(job['status'], 'error')
        self.assertIn('disk on fire', job['error'])
        self.app.start_scan()   # and the slot is free again
        self.wait_for(lambda: self.app.job()['status'] == 'done')

    def test_runs_report_their_kind(self):
        self.app.scan()
        plan = make_plan(self.app, JUNK_14D)
        self.gate.set()
        self.app.start_run(plan['id'], plan['revision'], plan['entries_digest'])
        self.assertEqual(self.app.job()['kind'], 'run')
        self.wait_for(lambda: self.app.job()['status'] == 'done')
        self.assertEqual(self.app.job()['kind'], 'run')


class SelectionTests(ServiceCase):
    def test_selection_reads_without_changing_anything(self):
        self.app.scan()
        first = self.app.selection()
        self.assertEqual((first['spec']['version'], first['summary']['count']), (0, 0))
        self.assertEqual(self.app.selection()['digest'], first['digest'])
        self.assertEqual(self.app.spec().version, 0)

    def test_selection_matches_what_apply_ops_returned(self):
        self.app.scan()
        changed = self.app.apply_ops(JUNK_14D)
        again = self.app.selection()
        for key in ('spec', 'scan_id', 'summary', 'digest'):
            self.assertEqual(again[key], changed[key], key)
        self.assertEqual(set(again), set(changed))

    def test_reading_the_selection_keeps_the_migration_notes(self):
        self.app.scan()
        self.app.apply_ops(JUNK_14D)
        self.app.scan()
        self.assertIsNotNone(self.app.spec_notes())
        self.assertEqual(self.app.selection()['scan_id'], 2)
        self.assertIsNotNone(self.app.spec_notes())   # only an edit of the selection clears them
        self.app.apply_ops([{'op': 'clear'}])
        self.assertIsNone(self.app.spec_notes())


class PlanTotalsTests(ServiceCase):
    def test_pending_totals_follow_exclusions_and_have_the_summary_shape(self):
        self.app.scan()
        plan = make_plan(self.app, JUNK_14D + [{'op': 'select_group', 'group': 'claude.projects:~/.claude/projects/-Users-me-proj', 'on': True}])
        totals = self.app.pending_totals(plan['id'])
        self.assertEqual(totals['count'], plan['totals']['count'])
        self.assertEqual(totals['by_action'], plan['totals']['by_action'])
        self.assertEqual((totals['alloc'], totals['reclaim']), (plan['totals']['alloc'], plan['totals']['reclaim']))
        self.assertIn('frees_estimate', totals['by_action']['archive'])
        rows = self.app.entries(plan['id'], limit=None)['rows']
        archive_seq = next(r['seq'] for r in rows if r['action'] == 'archive')
        self.app.exclude(plan['id'], [archive_seq], plan['revision'])
        after = self.app.pending_totals(plan['id'])
        self.assertEqual(after['count'], totals['count'] - 1)
        self.assertEqual(after['by_action']['archive']['count'], totals['by_action']['archive']['count'] - 1)
        self.assertEqual(self.app.plan(plan['id'])['totals'], plan['totals'])   # the plan as created is untouched
        self.expect(ErrorCode.NOT_FOUND, 'error.NOT_FOUND', self.app.pending_totals, 'nope')

    def test_nothing_pending_after_a_run(self):
        self.app.scan()
        plan = make_plan(self.app, JUNK_14D)
        self.gate.set()
        self.app.run_plan(plan['id'], plan['revision'], plan['entries_digest'])
        self.assertEqual(self.app.pending_totals(plan['id']), {'count': 0, 'alloc': 0, 'reclaim': 0, 'by_action': {}})


class ValidateRunTests(ServiceCase):
    def test_raises_what_a_run_would_raise_and_starts_nothing(self):
        self.app.scan()
        plan = make_plan(self.app, JUNK_14D)
        args = (plan['id'], plan['revision'], plan['entries_digest'])
        self.app.validate_run(*args)
        self.expect(ErrorCode.REVISION_MISMATCH, 'error.REVISION_MISMATCH', self.app.validate_run, plan['id'], 9, args[2])
        self.expect(ErrorCode.REVISION_MISMATCH, 'error.REVISION_MISMATCH', self.app.validate_run, plan['id'], 1, 'x')
        self.expect(ErrorCode.NOT_FOUND, 'error.NOT_FOUND', self.app.validate_run, 'nope', 1, 'x')
        self.app.update_settings({'idle_days': 3})
        self.expect(ErrorCode.RULES_CHANGED, 'error.RULES_CHANGED', self.app.validate_run, *args)
        self.app.update_settings({'idle_days': 180})
        self.fh.clock.advance(days=2)
        self.expect(ErrorCode.PLAN_EXPIRED, 'error.PLAN_EXPIRED', self.app.validate_run, *args)
        self.assertEqual(self.app.job()['status'], 'idle')

    def test_a_running_or_finished_plan_cannot_be_started_again(self):
        self.app.scan()
        plan = make_plan(self.app, JUNK_14D)
        args = (plan['id'], plan['revision'], plan['entries_digest'])
        self.app.start_run(*args)
        self.assertTrue(self.reached.wait(30))
        self.expect(ErrorCode.IN_PROGRESS, 'error.IN_PROGRESS', self.app.validate_run, *args)
        self.gate.set()
        self.wait_for(lambda: self.app.job()['status'] == 'done')
        exc = self.expect(ErrorCode.INVALID, 'error.plan_state', self.app.validate_run, *args)
        self.assertEqual(exc.details, {'state': 'done'})


class SnapshotTests(ServiceCase):
    """A scan that finishes (and prunes the previous scan's rows) while a request is half way must not give a 500."""

    def rescan_in_another_thread(self):
        """Scan 2 finishes and scan 1's rows are deleted, as seen by any new reader. The scanning thread then waits
        for the readers that still hold a snapshot (its WAL checkpoint), so it is joined at the end of the test."""
        t = threading.Thread(target=self.app.scan)
        t.start()
        self.addCleanup(t.join, 60)
        self.wait_for(self.old_rows_gone)

    def old_rows_gone(self):
        # a connection opened beforehand: opening one during the scan's checkpoint would wait for it
        row = self.probe.execute("SELECT (SELECT MAX(id) FROM scans WHERE status = 'done'), "
                                 "(SELECT COUNT(*) FROM items WHERE scan_id = 1)").fetchone()
        return row == (2, 0)

    def setUp(self):
        super().setUp()
        self.gate.set()
        self.app.scan()
        self.probe = sqlite3.connect(str(self.app.data_dir / 'steward.db'), isolation_level=None, timeout=1)
        self.addCleanup(self.probe.close)
        self.probe.execute('SELECT 1').fetchone()
        self.expected = {k: v for k, v in make_plan_args(self.app).items()}

    def test_a_reader_keeps_its_view_until_the_block_ends(self):
        store = self.app.store
        before = store.latest_scan()['id']
        with store.read_snapshot():
            self.assertEqual(store.latest_scan()['id'], before)
            self.rescan_in_another_thread()                       # another connection commits scan 2 and prunes scan 1
            self.assertEqual(store.latest_scan()['id'], before)   # still the old view ...
            self.assertGreater(len(list(store.items(before))), 0)   # ... with the old rows
            with store.read_snapshot():                           # nesting joins it
                self.assertEqual(store.latest_scan()['id'], before)
        self.assertEqual(store.latest_scan()['id'], before + 1)
        self.assertEqual(list(store.items(before)), [])
        self.assertFalse(store._conn().in_transaction)

    def test_a_scan_between_the_resolution_and_the_rows_still_gives_a_correct_plan(self):
        real = planner.resolve

        def resolve_then_rescan(*a, **kw):
            res = real(*a, **kw)
            self.rescan_in_another_thread()
            return res
        with mock.patch.object(planner, 'resolve', resolve_then_rescan):
            plan = self.app.create_plan('default', self.expected)
        self.assertEqual(plan['scan_id'], 1)
        self.assertEqual(plan['entries']['total'], plan['totals']['count'])
        self.assertEqual(plan['entries']['total'], 3)
        self.assertEqual(self.app.latest_scan()['id'], 2)

    def test_a_scan_before_the_planner_reads_is_spec_stale(self):
        real = self.app.spec_notes

        def notes_after_rescan(session='default'):
            self.rescan_in_another_thread()
            return real(session)
        with mock.patch.object(self.app, 'spec_notes', notes_after_rescan):
            exc = self.expect(ErrorCode.INVALID, 'error.spec_stale', self.app.create_plan, 'default', self.expected)
        self.assertEqual((exc.details['from_scan'], exc.details['to_scan']), (1, 2))

    def test_an_item_page_is_read_from_one_state(self):
        real = self.app._resolve

        def resolve_then_rescan(*a, **kw):
            res = real(*a, **kw)
            self.rescan_in_another_thread()
            return res
        with mock.patch.object(self.app, '_resolve', resolve_then_rescan):
            page = self.app.items(group='codex.state-tmp')
        self.assertEqual((page['total'], len(page['rows'])), (2, 2))
        self.assertTrue(all(r['decision']['state'] for r in page['rows']))

    def test_a_scan_before_the_page_is_read_is_spec_stale(self):
        real = self.app._require_current

        def require_after_rescan(*a, **kw):
            self.rescan_in_another_thread()
            return real(*a, **kw)
        with mock.patch.object(self.app, '_require_current', require_after_rescan):
            exc = self.expect(ErrorCode.INVALID, 'error.spec_stale', self.app.items, group='codex.state-tmp')
        self.assertEqual((exc.details['from_scan'], exc.details['to_scan']), (1, 2))
        self.assertEqual(self.app.items(group='codex.state-tmp')['total'], 2)   # the next request is fine

    def test_groups_are_read_from_one_state_too(self):
        real = self.app._resolve

        def resolve_then_rescan(*a, **kw):
            res = real(*a, **kw)
            self.rescan_in_another_thread()
            return res
        with mock.patch.object(self.app, '_resolve', resolve_then_rescan):
            self.assertTrue(self.app.groups())


class DraftLifecycleTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.gate.set()
        self.app.scan()

    def draft(self):
        return make_plan(self.app, JUNK_14D)

    def test_a_scan_invalidates_the_drafts_of_older_scans_only(self):
        a, b = self.draft(), self.draft()
        ran = self.draft()
        self.app.run_plan(ran['id'], ran['revision'], ran['entries_digest'])   # rescans too: scan 2, then
        for p in (a, b):
            row = self.app.plan(p['id'])
            self.assertEqual((row['state'], row['error'], row['entries']['total']), ('invalid', 'superseded', 0))
        done = self.app.plan(ran['id'])
        self.assertEqual((done['state'], done['entries']['total']), ('done', 3))
        self.fh.file('~/.claude/debug/again.txt', size=100, age_days=30)   # something to offer again
        self.app.scan()
        fresh = self.draft()                       # made from the latest scan: only the next scan replaces it
        self.assertEqual(self.app.store.supersede_drafts(self.app.latest_scan()['id']), 0)
        self.assertEqual(self.app.plan(fresh['id'])['state'], 'draft')
        self.app.scan()
        self.assertEqual(self.app.plan(fresh['id'])['state'], 'invalid')

    def test_discarding(self):
        plan = self.draft()
        self.app.discard_plan(plan['id'])
        row = self.app.plan(plan['id'])
        self.assertEqual((row['state'], row['error'], row['entries']['total']), ('invalid', 'discarded', 0))
        exc = self.expect(ErrorCode.INVALID, 'error.plan_state', self.app.discard_plan, plan['id'])
        self.assertEqual(exc.details, {'state': 'invalid'})
        self.expect(ErrorCode.NOT_FOUND, 'error.NOT_FOUND', self.app.discard_plan, 'nope')

    def test_the_cleanup_list_never_offers_a_replaced_or_expired_draft(self):
        old = self.draft()
        self.assertEqual(self.app.plans()['draft']['id'], old['id'])
        self.fh.clock.advance(days=2)
        self.assertIsNone(self.app.plans()['draft'])
        self.fh.clock.advance(days=-2)
        self.app.scan()
        self.assertIsNone(self.app.plans()['draft'])

    def test_excluded_entries_come_back_only_into_a_draft(self):
        plan = self.draft()
        pid = plan['id']
        self.assertEqual(self.app.exclude(pid, [1, 2], 1), 2)
        self.expect(ErrorCode.REVISION_MISMATCH, 'error.REVISION_MISMATCH', self.app.include, pid, [1], 1)
        self.assertEqual(self.app.include(pid, [1], 2), 3)
        self.assertEqual(self.app.plan(pid)['entries']['by_state'], {'pending': 2, 'excluded': 1})
        self.assertEqual(self.app.include(pid, [1], 3), 4)                      # already pending: nothing to undo, no harm
        self.expect(ErrorCode.NOT_FOUND, 'error.NOT_FOUND', self.app.include, pid, [99], 4)
        self.app.run_plan(pid, 4, plan['entries_digest'])
        exc = self.expect(ErrorCode.INVALID, 'error.plan_state', self.app.include, pid, [2], 4)
        self.assertEqual(exc.details, {'state': 'done'})

    def test_group_edits_and_totals_by_group(self):
        plan = self.draft()
        pid = plan['id']
        groups = self.app.plan_groups(pid)
        self.assertEqual(sum(g['entries'] for g in groups), 3)
        first = groups[0]['group']
        rev = self.app.exclude_group(pid, first, 1)
        self.assertEqual(self.app.pending_totals(pid)['count'], 3 - groups[0]['entries'])
        self.assertEqual(self.app.include_group(pid, first, rev), rev + 1)
        self.assertEqual(self.app.pending_totals(pid)['count'], 3)
        self.assertEqual(self.app.exclude_group(pid, 'no.such.group', rev + 1), rev + 1)   # nothing to exclude: no change


def make_plan_args(app):
    r = app.apply_ops(JUNK_14D)
    return {'spec_version': r['spec']['version'], 'digest': r['digest'], 'scan_id': r['scan_id'],
            'resolved_at': r['resolved_at']}


if __name__ == '__main__':
    unittest.main()
