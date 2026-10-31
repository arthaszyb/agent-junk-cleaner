from __future__ import annotations

import json
import os
import threading
import time
import unittest
from unittest import mock

import m2_fixture as fx
from disk_steward import actions
from disk_steward.macos import OpenFiles
from disk_steward import settings as settings_mod
from disk_steward.model import ErrorCode, PlanState, StewardError
from disk_steward.service import App

from m3_support import make_app, make_mac
from m4_support import JUNK_14D, JUNK_ALL, make_plan
from support import Clock, FakeHome

LOG = '~/.claude/debug/x.txt'
CLAUDE_APP = 'com.anthropic.claudefordesktop'
BIG = '~/.claude/projects/-Users-me-proj/big.jsonl'
PROJECTS = [{'op': 'select_group', 'group': 'claude.projects:~/.claude/projects/-Users-me-proj', 'on': True},
            {'op': 'set_filter', 'min_age_days': 14}]


class RunCase(unittest.TestCase):
    def setUp(self):
        self.fh = FakeHome(Clock()).__enter__()
        self.addCleanup(self.fh.cleanup)
        fx.build(self.fh)
        self.mac = make_mac()
        self.apps = []
        self.app = self.new_app(mac=self.mac)

    def new_app(self, **kw):
        app = make_app(self.fh, **kw)
        self.addCleanup(app.close)
        self.apps.append(app)
        return app

    def plan(self, ops=JUNK_14D, app=None):
        app = app or self.app
        if app.latest_scan() is None:
            app.scan()
        return make_plan(app, ops)

    def args(self, plan):
        return plan['id'], plan['revision'], plan['entries_digest']

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


class RunPlanTests(RunCase):
    def test_end_to_end_on_the_fixture(self):
        plan = self.plan()
        self.assertEqual(plan['entries']['total'], 3)
        paths = [r['path'] for r in self.app.entries(plan['id'])['rows']]
        first_scan = self.app.latest_scan()['id']
        events = []
        summary = self.app.run_plan(*self.args(plan), on_event=lambda k, d: events.append(k))
        self.assertEqual(summary['counts'], {'done': 3})
        self.assertEqual(summary['counts']['done'], plan['totals']['count'])
        for p in paths:
            self.assertFalse(os.path.lexists(self.fh.path(p)), p)
        latest = self.app.latest_scan()
        self.assertEqual(summary['rescan'], {'scan_id': latest['id']})
        self.assertGreater(latest['id'], first_scan)
        everything = {r['path'] for r in self.app.items(limit=None)['rows']}
        self.assertFalse(everything & set(paths))
        self.assertIn('~/.claude/projects/-Users-me-proj/a.jsonl', everything)      # not in the plan: still there
        bus = [e['type'] for e in self.app.events.since(0)]
        last_scan = max(i for i, kind in enumerate(bus) if kind == 'scan.done')
        self.assertLess(bus.index('run.started'), bus.index('run.finished'))
        self.assertLess(bus.index('run.finished'), last_scan)       # the rescan comes after the run
        self.assertEqual(bus[-1], 'scan.done')
        self.assertEqual(events[0], 'run.started')
        self.assertEqual(events[-1], 'run.finished')
        job = self.app.job()
        self.assertEqual((job['status'], job['plan_id'], job['plan_state'], job['phase']), ('done', plan['id'], 'done', ''))
        self.assertEqual(job['summary']['counts'], {'done': 3})
        self.assertEqual(self.app.plan(plan['id'])['state'], 'done')
        json.dumps(summary)
        json.dumps(job)

    def test_the_operation_lock_is_held_during_the_run_and_released_after(self):
        plan = self.plan()
        seen = []
        holder = []
        app = self.new_app(mac=self.mac, hooks={'after_claim': lambda e: seen.append(holder[0].lock.held)})
        holder.append(app)
        app.run_plan(*self.args(plan))
        self.assertEqual(set(seen), {True})
        self.app.scan()   # not BUSY any more

    def test_a_refused_run_changes_nothing_and_reports_the_error(self):
        plan = self.plan()
        self.expect(ErrorCode.REVISION_MISMATCH, 'error.REVISION_MISMATCH', self.app.run_plan, plan['id'], 99,
                          plan['entries_digest'])
        job = self.app.job()
        self.assertEqual((job['status'], job['error_code']), ('error', 'REVISION_MISMATCH'))
        self.assertEqual(self.app.plan(plan['id'])['state'], 'draft')
        self.assertEqual(self.app.latest_scan()['id'], 1)      # no rescan: nothing happened
        self.assertTrue(self.fh.path('~/.codex/..codex-global-state.json.tmp-1784-aaaa').exists())

    def test_rescan_failure_is_reported_not_raised(self):
        plan = self.plan()
        with mock.patch.object(App, 'scan', side_effect=StewardError(ErrorCode.BUSY, 'error.busy')):
            summary = self.app.run_plan(*self.args(plan))
        self.assertEqual(summary['counts'], {'done': 3})
        self.assertIn('BUSY', summary['rescan']['error'])
        self.assertIn('scan.failed', [e['type'] for e in self.app.events.since(0)])

    def test_run_plan_refuses_a_second_run(self):
        plan = self.plan()
        gate, reached = threading.Event(), threading.Event()

        def block(entry):
            reached.set()
            gate.wait(30)
        app = self.new_app(mac=self.mac, hooks={'after_claim': block})
        t = threading.Thread(target=lambda: app.run_plan(*self.args(plan)))
        t.start()
        self.assertTrue(reached.wait(30))
        try:
            self.expect(ErrorCode.BUSY, 'error.busy', app.run_plan, *self.args(plan))
            self.expect(ErrorCode.BUSY, 'error.busy', app.start_run, *self.args(plan))
            self.expect(ErrorCode.BUSY, 'error.busy', app.scan)
            self.assertEqual(app.job()['status'], 'running')
        finally:
            gate.set()
            t.join(30)
        self.assertEqual(app.job()['status'], 'done')

    def test_another_process_holding_the_lock_makes_run_plan_busy(self):
        plan = self.plan()
        other = self.new_app(mac=self.mac)
        with other.lock:
            self.expect(ErrorCode.BUSY, 'error.busy', self.app.run_plan, *self.args(plan))
        self.assertEqual(self.app.job()['status'], 'idle')     # the claim on the run slot was given back
        self.app.run_plan(*self.args(plan))


class BackgroundRunTests(RunCase):
    def setUp(self):
        super().setUp()
        self.gate = threading.Event()
        self.reached = threading.Event()
        self.seen = []

        def block(entry):
            if entry.seq == 2:
                self.reached.set()
                self.gate.wait(30)
        self.app = self.new_app(mac=self.mac, hooks={'after_claim': block})

    def test_start_pause_resume(self):
        plan = self.plan(JUNK_ALL)
        started = self.app.start_run(*self.args(plan))
        self.assertEqual((started['status'], started['plan_id']), ('running', plan['id']))
        self.assertTrue(self.reached.wait(30))
        paused = self.app.pause()
        self.assertTrue(paused['pause_requested'])
        self.gate.set()
        self.wait_for(lambda: self.app.job()['status'] == 'paused')
        job = self.app.job()
        self.assertEqual(job['counts']['done'], 2)
        self.assertEqual(job['counts']['total'], 12)
        self.assertEqual(self.app.plan(plan['id'])['state'], 'paused')
        self.assertEqual(self.app.entries(plan['id'], state='pending')['total'], 10)
        self.app.resume()
        self.wait_for(lambda: self.app.job()['status'] == 'done')
        job = self.app.job()
        self.assertEqual(job['summary']['counts'], {'done': 12})
        self.assertEqual(job['counts']['done'], 12)
        self.assertEqual(job['summary']['rescan']['scan_id'], self.app.latest_scan()['id'])
        types = [e['type'] for e in self.app.events.since(0)]
        self.assertIn('run.paused', types)
        self.assertIn('run.resumed', types)

    def test_cancel(self):
        plan = self.plan(JUNK_ALL)
        self.app.start_run(*self.args(plan))
        self.assertTrue(self.reached.wait(30))
        self.app.cancel()
        self.gate.set()
        self.wait_for(lambda: self.app.job()['status'] == 'done')
        job = self.app.job()
        self.assertEqual((job['plan_state'], job['summary']['counts']), ('cancelled', {'done': 2, 'cancelled': 10}))
        self.assertEqual(self.app.plan(plan['id'])['state'], 'cancelled')

    def test_cancel_a_paused_run(self):
        plan = self.plan(JUNK_ALL)
        self.app.start_run(*self.args(plan))
        self.assertTrue(self.reached.wait(30))
        self.app.pause()
        self.gate.set()
        self.wait_for(lambda: self.app.job()['status'] == 'paused')
        self.app.cancel()
        self.wait_for(lambda: self.app.job()['status'] == 'done')
        self.assertEqual(self.app.job()['plan_state'], 'cancelled')

    def test_a_failing_background_run_is_visible_in_job(self):
        plan = self.plan(JUNK_ALL)
        self.mac.open_files = mock.Mock(side_effect=RuntimeError('lsof exited with 1'))
        self.app.start_run(*self.args(plan))
        self.wait_for(lambda: self.app.job()['status'] == 'error')
        job = self.app.job()
        self.assertIn('lsof exited with 1', job['error'])
        self.assertEqual(self.app.plan(plan['id'])['state'], 'paused')
        self.expect(ErrorCode.NOT_FOUND, 'error.no_run', self.app.pause)
        # the facade is free again
        self.mac.open_files = lambda: OpenFiles()
        self.gate.set()
        self.app.run_plan(*self.args(plan))
        self.assertEqual(self.app.job()['status'], 'done')

    def test_a_refused_background_run_reports_the_code(self):
        plan = self.plan(JUNK_ALL)
        self.app.start_run(plan['id'], 5, plan['entries_digest'])
        self.wait_for(lambda: self.app.job()['status'] == 'error')
        self.assertEqual(self.app.job()['error_code'], 'REVISION_MISMATCH')

    def test_classification_settings_cannot_change_under_a_live_or_paused_run(self):
        plan = self.plan(JUNK_ALL)
        before = self.app.settings
        self.app.start_run(*self.args(plan))
        self.assertTrue(self.reached.wait(30))
        self.expect(ErrorCode.BUSY, 'error.busy', self.app.update_settings, {'idle_days': 30})
        self.app.pause()
        self.gate.set()
        self.wait_for(lambda: self.app.job()['status'] == 'paused')
        self.expect(ErrorCode.BUSY, 'error.busy', self.app.update_settings, {'domains': ['agent']})
        # one blocked field blocks the whole patch
        self.expect(ErrorCode.BUSY, 'error.busy', self.app.update_settings, {'locale': 'zh-CN', 'idle_days': 1})
        self.assertEqual(self.app.settings, before)
        self.app.resume()
        self.wait_for(lambda: self.app.job()['status'] == 'done')
        self.assertEqual(self.app.update_settings({'idle_days': 30}).idle_days, 30)

    def test_settings_outside_the_hash_may_change_during_a_run(self):
        plan = self.plan(JUNK_ALL)
        self.app.start_run(*self.args(plan))
        self.assertTrue(self.reached.wait(30))
        self.assertEqual(self.app.update_settings({'default_min_age_days': 30}).default_min_age_days, 30)
        self.app.pause()
        self.gate.set()
        self.wait_for(lambda: self.app.job()['status'] == 'paused')
        self.assertEqual(self.app.update_settings({'locale': 'zh-CN'}).locale, 'zh-CN')
        self.assertEqual(self.app.locale, 'zh-CN')
        new = self.app.update_settings({'advisor_enabled': True, 'advisor_provider': 'codex'})
        self.assertEqual((new.advisor_enabled, new.advisor_provider), (True, 'codex'))
        self.app.resume()   # the run was planned under settings whose hash did not change, so it carries on
        self.wait_for(lambda: self.app.job()['status'] == 'done')
        self.assertEqual(self.app.job()['plan_state'], 'done')
        self.assertEqual(self.app.settings.default_min_age_days, 30)

    def test_run_safe_settings_are_exactly_the_ones_outside_the_settings_hash(self):
        import dataclasses
        from disk_steward.service import RUN_SAFE_SETTINGS
        fields = {f.name for f in dataclasses.fields(settings_mod.Settings)}
        # a new field must be put in one list or the other on purpose
        self.assertEqual(RUN_SAFE_SETTINGS, fields - set(settings_mod._HASHED))

    def test_a_paused_run_notices_settings_rewritten_by_another_process(self):
        plan = self.plan(JUNK_ALL)
        self.app.start_run(*self.args(plan))
        self.assertTrue(self.reached.wait(30))
        self.app.pause()
        self.gate.set()
        self.wait_for(lambda: self.app.job()['status'] == 'paused')
        settings_mod.save(self.fh.data_dir, settings_mod.replace(self.app.settings, idle_days=999))
        self.app.resume()
        self.wait_for(lambda: self.app.job()['status'] == 'error')
        job = self.app.job()
        self.assertEqual(job['error_code'], 'RULES_CHANGED')
        row = self.app.plan(plan['id'])
        self.assertEqual(row['state'], 'paused')
        self.assertIn('RULES_CHANGED', row['error'])
        self.assertEqual(self.app.entries(plan['id'], state='done')['total'], 2)
        self.assertEqual(self.app.entries(plan['id'], state='pending')['total'], 10)

    def test_control_without_a_run(self):
        for fn in (self.app.pause, self.app.resume, self.app.cancel):
            self.expect(ErrorCode.NOT_FOUND, 'error.no_run', fn)
        job = self.app.job()
        self.assertEqual((job['status'], job['plan_id']), ('idle', None))
        self.assertFalse(job['pause_requested'])


class PreflightTests(RunCase):
    def test_blockers_are_grouped_by_reason(self):
        plan = self.plan(JUNK_ALL)
        st = os.stat(self.fh.path(LOG))
        self.mac.running = {CLAUDE_APP}
        self.mac.open = OpenFiles(by_inode={(st.st_dev, st.st_ino): ['vim(42)']})
        pre = self.app.preflight(plan['id'])
        app_key = f'app_running:{CLAUDE_APP}'
        self.assertEqual(set(pre['blockers']), {app_key, 'open:vim(42)'})
        rows = {r['path']: r for r in self.app.entries(plan['id'], limit=None)['rows']}
        quit_paths = [p for p, r in rows.items() if p.startswith(('~/Library/Application Support/Claude/', '~/Library/Caches/com.anthropic'))]
        self.assertEqual(pre['blockers'][app_key]['count'], 4)
        self.assertEqual(pre['blockers'][app_key]['alloc'], sum(rows[p]['alloc'] for p in quit_paths))
        self.assertEqual(pre['blockers']['open:vim(42)'], {'count': 1, 'alloc': rows[LOG]['alloc']})
        self.assertEqual((pre['blocked_entries'], pre['pending'], pre['hashes_ok'], pre['ready']), (5, 12, True, True))
        self.assertEqual(pre['state'], 'draft')
        json.dumps(pre)

    def test_apps_named_by_the_items_evidence_are_grouped_like_the_rules_own(self):
        from disk_steward.settings import Settings
        support = '~/Library/Application Support'
        self.fh.file(f'{support}/Notion/Cache/Cache_Data/a', size=100, age_days=30)
        self.fh.file(f'{support}/Notion/GPUCache/data_0', size=100, age_days=30)
        info = {'path': '/Applications/Notion.app', 'name': 'Notion', 'display_name': '', 'executable': '',
                'stem': 'Notion'}
        mac = make_mac(app_info={'notion.id': info}, running=['notion.id'])
        app = self.new_app(mac=mac, settings=Settings(domains=('agent', 'app-library')))
        plan = self.plan([{'op': 'select_group', 'group': f'app.electron.cache:{support}/Notion', 'on': True},
                          {'op': 'set_filter', 'min_age_days': 0}], app=app)
        pre = app.preflight(plan['id'])
        self.assertEqual(set(pre['blockers']), {'app_running:notion.id'})
        slot = pre['blockers']['app_running:notion.id']
        self.assertEqual((slot['count'], slot['app']), (2, 'Notion'))
        self.assertEqual((pre['blocked_entries'], pre['pending']), (2, 2))

    def test_preflight_changes_nothing(self):
        plan = self.plan(JUNK_ALL)
        self.app.preflight(plan['id'])
        self.assertEqual(self.app.plan(plan['id'])['state'], 'draft')
        self.assertEqual(self.app.entries(plan['id'], state='pending')['total'], 12)
        self.assertEqual(self.app.job()['status'], 'idle')
        self.assertTrue(self.fh.path(LOG).exists())
        self.assertFalse((self.fh.data_dir / 'staging').exists())

    def test_hash_changes_are_reported_per_part(self):
        plan = self.plan(JUNK_ALL)
        link = self.fh.path('~/.local/bin/claude')
        os.unlink(link)
        os.symlink(self.fh.path('~/.local/share/claude/versions/2.1.289'), link)
        pre = self.app.preflight(plan['id'])
        self.assertEqual(pre['hashes'], {'catalog': True, 'settings': True})
        self.assertEqual((pre['hashes_ok'], pre['ready']), (True, True))
        # The repointed version is the one entry run() would keep; preflight says so up front.
        self.assertEqual(pre['blockers']['rules_changed']['count'], 1)
        self.app.update_settings({'large_file_min_bytes': 1})
        pre = self.app.preflight(plan['id'])
        self.assertEqual((pre['hashes']['settings'], pre['hashes_ok'], pre['ready']), (False, False, False))

    def test_a_started_plan_is_judged_like_validate_for_run_does(self):
        plan = self.plan(JUNK_ALL)
        self.app.store.set_plan_state(plan['id'], PlanState.PAUSED)
        link = self.fh.path('~/.local/bin/claude')
        os.unlink(link)
        os.symlink(self.fh.path('~/.local/share/claude/versions/2.1.289'), link)
        pre = self.app.preflight(plan['id'])
        self.assertEqual(pre['hashes'], {'catalog': True, 'settings': True})      # facts are not compared any more
        self.assertEqual((pre['hashes_ok'], pre['ready']), (True, True))

    def test_expired_plan(self):
        plan = self.plan(JUNK_ALL)
        self.fh.clock.advance(days=2)
        pre = self.app.preflight(plan['id'])
        self.assertEqual((pre['expired'], pre['ready']), (True, False))

    def test_free_space_for_archives(self):
        self.fh.file(BIG, size=200_000, age_days=20)
        plan = self.plan(PROJECTS)
        pre = self.app.preflight(plan['id'])
        self.assertTrue(pre['archive']['ok'])
        self.mac.free = 10 * 1024 * 1024
        pre = self.app.preflight(plan['id'])
        self.assertFalse(pre['archive']['ok'])
        self.assertEqual((pre['ready'], pre['warnings'][0]['key']), (False, 'warning.archive_space'))

    def test_unknown_plan(self):
        self.expect(ErrorCode.NOT_FOUND, 'error.NOT_FOUND', self.app.preflight, 'nope')

    def test_snapshot_failure_is_loud(self):
        plan = self.plan(JUNK_ALL)
        self.mac.running_bundle_ids = mock.Mock(side_effect=RuntimeError('lsappinfo exited with 1'))
        with self.assertRaises(RuntimeError):
            self.app.preflight(plan['id'])


class ArchiveFacadeTests(RunCase):
    def setUp(self):
        super().setUp()
        self.big = self.fh.file(BIG, content=os.urandom(50_000), age_days=20)
        self.data = self.big.read_bytes()
        plan = self.plan(PROJECTS)
        self.summary = self.app.run_plan(*self.args(plan))
        self.plan_ = plan

    def test_archives_list(self):
        rows = self.app.archives()
        self.assertEqual(len(rows), 4)
        row = next(r for r in rows if r['path'] == BIG)
        self.assertEqual((row['state'], row['fmt'], row['orig_size']), ('stored', 'gz', len(self.data)))
        json.dumps(rows)

    def test_restore_and_purge(self):
        rows = {r['path']: r for r in self.app.archives()}
        self.assertFalse(self.big.exists())
        result = self.app.restore(rows[BIG]['id'])
        self.assertEqual((result['path'], result['bytes']), (BIG, len(self.data)))
        self.assertEqual(self.big.read_bytes(), self.data)
        other = rows['~/.claude/projects/-Users-me-proj/a.jsonl']
        purged = self.app.purge_archive(other['id'])
        self.assertEqual(purged['archive_id'], other['id'])
        states = {r['path']: r['state'] for r in self.app.archives()}
        self.assertEqual(states[BIG], 'restored')
        self.assertEqual(states['~/.claude/projects/-Users-me-proj/a.jsonl'], 'purged')
        types = [e['type'] for e in self.app.events.since(0)]
        self.assertIn('archive.restored', types)
        self.assertIn('archive.purged', types)
        self.assertFalse(self.fh.path('~/.claude/projects/-Users-me-proj/a.jsonl').exists())

    def test_restore_refuses_an_existing_file(self):
        rows = {r['path']: r for r in self.app.archives()}
        self.big.write_bytes(b'new')
        self.expect(ErrorCode.INVALID, 'error.restore_exists', self.app.restore, rows[BIG]['id'])
        self.assertEqual(self.big.read_bytes(), b'new')

    def test_both_take_the_operation_lock(self):
        rows = self.app.archives()
        with self.app.lock:
            self.expect(ErrorCode.BUSY, 'error.busy', self.app.restore, rows[0]['id'])
            self.expect(ErrorCode.BUSY, 'error.busy', self.app.purge_archive, rows[0]['id'])
        self.assertEqual({r['state'] for r in self.app.archives()}, {'stored'})

    def test_unknown_ids(self):
        self.expect(ErrorCode.NOT_FOUND, 'error.NOT_FOUND', self.app.restore, 'nope')
        self.expect(ErrorCode.NOT_FOUND, 'error.NOT_FOUND', self.app.purge_archive, 'nope')


class RecoveryAtStartTests(RunCase):
    def crash(self, plan):
        def die(entry):
            raise SystemExit('simulated crash')
        app = self.new_app(mac=self.mac, hooks={'before_unlink': die})
        with self.assertRaises(SystemExit):
            app.run_plan(*self.args(plan))
        return app

    def test_a_new_app_finishes_what_a_crashed_run_left(self):
        plan = self.plan([{'op': 'select_group', 'group': 'claude.logs', 'on': True}, {'op': 'set_filter', 'min_age_days': 0}])
        self.crash(plan)
        self.assertFalse(self.fh.path(LOG).exists())
        self.assertEqual(self.app.entries(plan['id'])['rows'][0]['state'], 'in_progress')
        fresh = self.new_app(mac=self.mac)
        self.assertEqual([(e['path'], e['state']) for e in fresh.recovery['entries']], [(LOG, 'done')])
        self.assertEqual(fresh.plan(plan['id'])['state'], 'paused')
        self.assertEqual(fresh.plan(plan['id'])['error'], 'interrupted')
        done = [e for e in fresh.events.since(0) if e['type'] == 'recover.done']
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]['data']['plans'], [plan['id']])
        json.dumps(fresh.recovery)

    def test_nothing_to_recover_publishes_nothing(self):
        fresh = self.new_app(mac=self.mac)
        self.assertIsNone(fresh.recovery)
        self.assertEqual([e['type'] for e in fresh.events.since(0)], [])

    def test_recovery_waits_when_another_process_is_running(self):
        plan = self.plan([{'op': 'select_group', 'group': 'claude.logs', 'on': True}, {'op': 'set_filter', 'min_age_days': 0}])
        self.crash(plan)
        with self.app.lock:    # somebody else's live run holds the operation lock
            fresh = self.new_app(mac=self.mac)
        self.assertIsNone(fresh.recovery)
        self.assertEqual([e['type'] for e in fresh.events.since(0)], ['recover.skipped'])
        self.assertEqual(fresh.entries(plan['id'])['rows'][0]['state'], 'in_progress')


if __name__ == '__main__':
    unittest.main()
