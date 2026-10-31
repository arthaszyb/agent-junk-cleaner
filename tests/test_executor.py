from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sqlite3
import stat
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import m2_fixture as fx
from disk_steward import actions
from disk_steward.catalog import ScanRoot, build_catalog, load_catalog
from disk_steward.executor import Control, Executor
from disk_steward.fsutil import SF_DATALESS, UF_IMMUTABLE
from disk_steward.guards import SnapshotError
from disk_steward.macos import OpenFiles
from disk_steward import settings as settings_mod
from disk_steward.model import (Action, AgeBasis, Domain, EntryState, ErrorCode, Kind, PlanState, Rule, StewardError, Tier,
                                Unit)
from disk_steward.settings import Settings

from m3_support import make_app, make_mac
from m4_support import JUNK_ALL, make_plan, tree_snapshot
from support import Clock, FakeHome

LOG = '~/.claude/debug/x.txt'                       # claude.logs, 5 days old
CODEX_LOG = '~/.codex/log/codex-tui.log'            # codex.logs
CACHE = '~/Library/Application Support/Claude/Cache/data_0'      # claude.electron.cache: Claude Desktop must be quit
SHIPIT = '~/Library/Caches/com.anthropic.claudefordesktop.ShipIt/update.zip'
CURSOR_BUNDLE = '~/Library/Application Support/Cursor/CachedData/h1'
TMP_A = '~/.codex/..codex-global-state.json.tmp-1784-aaaa'
V_OLD = '~/.local/share/claude/versions/2.1.289'
CLAUDE_APP = 'com.anthropic.claudefordesktop'
LOGS_ONLY = [{'op': 'select_group', 'group': 'claude.logs', 'on': True}, {'op': 'set_filter', 'min_age_days': 0}]
PROJECTS = 'claude.projects:~/.claude/projects/-Users-me-proj'
BIG = '~/.claude/projects/-Users-me-proj/big.jsonl'


def scratch_catalog(settings):
    """The real catalog plus a bundle rule and a file rule that offer every action, for archive and trash tests."""
    base = load_catalog(settings)
    extra = (
        Rule(id='test.bundle', provider='test', domain=Domain.AGENT, tier=Tier.REVIEW, kind=Kind.CACHE,
             unit=Unit.BUNDLE, patterns=('~/scratch/*',), priority=300,
             actions=(Action.ARCHIVE, Action.DELETE, Action.TRASH), default_action=Action.ARCHIVE),
        Rule(id='test.file', provider='test', domain=Domain.AGENT, tier=Tier.REVIEW, kind=Kind.CACHE,
             patterns=('~/scratch-files/**',), priority=300,
             actions=(Action.ARCHIVE, Action.DELETE, Action.TRASH), default_action=Action.ARCHIVE),
        Rule(id='test.used', provider='test', domain=Domain.AGENT, tier=Tier.REVIEW, kind=Kind.IDLE_FILE,
             patterns=('~/scratch-used/**',), priority=300, age_basis=AgeBasis.LAST_USED,
             actions=(Action.DELETE,), default_action=Action.DELETE),
    )
    return build_catalog(list(base.rules) + list(extra),
                         list(base.roots) + [ScanRoot(p, Domain.AGENT)
                                             for p in ('~/scratch', '~/scratch-files', '~/scratch-used')],
                         base.privacy_skip)


class ExecCase(unittest.TestCase):
    catalog = None   # set to scratch_catalog by subclasses that need it
    settings = None  # Settings for the App; the default scans the agent area only
    mac_kwargs = {}

    def setUp(self):
        self.fh = FakeHome(Clock()).__enter__()
        self.addCleanup(self.fh.cleanup)
        fx.build(self.fh)
        self.mac = make_mac(**self.mac_kwargs)
        if self.catalog is not None:
            for target in ('disk_steward.service.load_catalog', 'disk_steward.executor.load_catalog',
                           'disk_steward.planner.load_catalog'):
                patcher = mock.patch(target, self.catalog)
                patcher.start()
                self.addCleanup(patcher.stop)
        self.app = make_app(self.fh, mac=self.mac, settings=self.settings)
        self.addCleanup(self.app.close)
        self.scanned = False

    # building plans and running them ---------------------------------------------------------------

    def scan(self):
        self.app.scan()

    def plan(self, ops=JUNK_ALL, session='default'):
        self.scan()
        return make_plan(self.app, ops, session)

    def executor(self, hooks=None, mac=None, settings=None):
        app = self.app
        return Executor(app.store, load_catalog(app.settings) if self.catalog is None else self.catalog(app.settings),
                        None, app.bases, mac or app.mac, app.audit, app.clock, settings or app.settings,
                        app.data_dir, hooks=hooks, lock=app.lock)

    def run_plan(self, plan, hooks=None, control=None, events=None, mac=None, executor=None, settings=None):
        ex = executor or self.executor(hooks, mac, settings)
        on_event = None if events is None else (lambda kind, data: events.append((kind, data)))
        with self.app.lock:
            return ex.run(plan['id'], plan['revision'], plan['entries_digest'], control or Control(), on_event)

    def rows(self, plan):
        return {r['path']: r for r in self.app.entries(plan['id'], limit=None)['rows']}

    def seq_of(self, plan, path):
        return self.rows(plan)[path]['seq']

    def outcome(self, plan, path):
        r = self.rows(plan)[path]
        return r['state'], r['reason']

    def plan_row(self, plan):
        return self.app.plan(plan['id'])

    def assert_kept(self, plan, path, reason, state='skipped', content=None):
        self.assertEqual(self.outcome(plan, path), (state, reason), path)
        self.assertTrue(os.path.lexists(self.fh.path(path)), path)

    def audit_lines(self):
        p = self.fh.data_dir / 'audit' / 'audit.jsonl'
        return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

    def staging_left(self, plan):
        d = self.fh.data_dir / 'staging' / plan['id']
        return sorted(os.listdir(d)) if d.exists() else []

    def wait_for(self, condition, timeout=30):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail('timed out')
            time.sleep(0.01)


class HappyPathTests(ExecCase):
    def test_every_entry_is_handled_and_reported(self):
        plan = self.plan()
        n = plan['entries']['total']
        self.assertEqual(n, 12)
        events = []
        summary = self.run_plan(plan, events=events)
        self.assertEqual(summary['counts'], {'done': 12})
        self.assertEqual(summary['state'], 'done')
        for path in self.rows(plan):
            self.assertFalse(os.path.lexists(self.fh.path(path)), path)
        self.assertEqual(self.plan_row(plan)['state'], 'done')
        self.assertEqual(self.staging_left(plan), [])
        self.assertFalse((self.fh.data_dir / 'staging' / plan['id']).exists())
        # the protected neighbours are all still there
        self.assertTrue(self.fh.path('~/.local/share/claude/versions/2.1.299').exists())
        self.assertTrue(self.fh.path('~/.codex/auth.json').exists())
        self.assertTrue(self.fh.path('~/.claude/settings.json').exists())

    def test_summary_numbers(self):
        plan = self.plan()
        self.mac.snapshot_names = ['com.apple.TimeMachine.2026-10-01']
        before = self.mac.free
        summary = self.run_plan(plan)
        rows = self.rows(plan).values()
        self.assertEqual(summary['removed_alloc'], sum(r['alloc'] for r in rows))
        self.assertEqual(summary['freed'], sum(r['reclaim'] for r in rows))
        self.assertEqual(summary['estimated_delta'], summary['freed'])
        self.assertEqual((summary['free_before'], summary['free_after'], summary['measured_delta']), (before, before, 0))
        self.assertEqual(summary['moved_to_trash'], 0)
        self.assertEqual(summary['by_action']['delete']['count'], 12)
        self.assertEqual(summary['snapshots'], ['com.apple.TimeMachine.2026-10-01'])
        self.assertEqual([w['key'] for w in summary['warnings']], ['warning.snapshots'])
        json.dumps(summary)

    def test_events_in_order(self):
        plan = self.plan()
        events = []
        self.run_plan(plan, events=events)
        kinds = [k for k, _ in events]
        self.assertEqual(kinds[0], 'run.started')
        self.assertEqual(kinds[-1], 'run.finished')
        self.assertEqual(kinds.count('entry.done'), 12)
        self.assertIn('run.progress', kinds)
        started = events[0][1]
        self.assertEqual((started['plan_id'], started['total']), (plan['id'], 12))
        done = next(d for k, d in events if k == 'entry.done')
        self.assertEqual(set(done), {'plan_id', 'seq', 'state', 'reason', 'freed', 'path'})
        progress = [d for k, d in events if k == 'run.progress'][-1]
        self.assertEqual((progress['done'], progress['total']), (12, 12))
        self.assertEqual(events[-1][1]['summary']['state'], 'done')
        json.dumps(events)

    def test_audit_has_an_intent_before_and_an_outcome_after_each_entry(self):
        plan = self.plan(LOGS_ONLY)
        self.run_plan(plan)
        lines = self.audit_lines()
        kinds = [l['event'] for l in lines]
        self.assertEqual(kinds, ['run.start', 'entry.intent', 'entry.outcome', 'run.end'])
        intent, outcome = lines[1], lines[2]
        self.assertEqual((intent['path'], intent['action'], intent['unit']), (LOG, 'delete', 'file'))
        self.assertEqual((outcome['path'], outcome['state']), (LOG, 'done'))
        for line in lines:
            self.assertEqual(line['plan_id'], plan['id'])
            self.assertNotIn(str(self.fh.root), json.dumps(line))   # logical paths only

    def test_intent_is_written_before_the_file_is_touched(self):
        plan = self.plan(LOGS_ONLY)
        seen = []
        self.run_plan(plan, hooks={'before_unlink': lambda e: seen.append([l['event'] for l in self.audit_lines()])})
        self.assertEqual(seen, [['run.start', 'entry.intent']])

    def test_skipped_entries_get_only_an_outcome_line(self):
        plan = self.plan(LOGS_ONLY)
        with open(self.fh.path(LOG), 'ab') as f:
            f.write(b'x')
        self.run_plan(plan)
        self.assertEqual([l['event'] for l in self.audit_lines()], ['run.start', 'entry.outcome', 'run.end'])


class PreflightTests(ExecCase):
    def test_state_must_be_draft_or_paused(self):
        plan = self.plan(LOGS_ONLY)
        self.run_plan(plan)
        with self.assertRaises(StewardError) as cm:
            self.run_plan(plan)
        self.assertEqual((cm.exception.code, cm.exception.message_key), (ErrorCode.INVALID, 'error.plan_state'))
        plan2 = self.plan([{'op': 'select_group', 'group': 'codex.logs', 'on': True}, {'op': 'set_filter', 'min_age_days': 0}])
        self.app.store.set_plan_state(plan2['id'], PlanState.RUNNING)
        with self.assertRaises(StewardError) as cm:
            self.run_plan(plan2)
        self.assertEqual(cm.exception.code, ErrorCode.IN_PROGRESS)

    def test_wrong_revision_or_digest(self):
        plan = self.plan(LOGS_ONLY)
        with self.assertRaises(StewardError) as cm:
            with self.app.lock:
                self.executor().run(plan['id'], plan['revision'] + 1, plan['entries_digest'], Control())
        self.assertEqual(cm.exception.code, ErrorCode.REVISION_MISMATCH)
        with self.assertRaises(StewardError) as cm:
            with self.app.lock:
                self.executor().run(plan['id'], plan['revision'], 'f' * 64, Control())
        self.assertEqual(cm.exception.code, ErrorCode.REVISION_MISMATCH)
        self.assertTrue(self.fh.path(LOG).exists())

    def test_expired_plan(self):
        plan = self.plan(LOGS_ONLY)
        self.fh.clock.advance(days=2)
        with self.assertRaises(StewardError) as cm:
            self.run_plan(plan)
        self.assertEqual(cm.exception.code, ErrorCode.PLAN_EXPIRED)
        self.assertTrue(self.fh.path(LOG).exists())

    def test_operation_lock_must_be_held(self):
        plan = self.plan(LOGS_ONLY)
        with self.assertRaises(RuntimeError):
            self.executor().run(plan['id'], plan['revision'], plan['entries_digest'], Control())
        self.assertTrue(self.fh.path(LOG).exists())

    def test_not_enough_space_for_archives(self):
        self.fh.file(BIG, size=200_000, age_days=20)
        plan = self.plan([{'op': 'select_group', 'group': PROJECTS, 'on': True}, {'op': 'set_filter', 'min_age_days': 14}])
        self.mac.free = 10 * 1024 * 1024
        with self.assertRaises(StewardError) as cm:
            self.run_plan(plan)
        self.assertEqual((cm.exception.code, cm.exception.message_key), (ErrorCode.UNAVAILABLE, 'error.no_space'))
        self.assertEqual(self.plan_row(plan)['state'], 'draft')
        self.assertEqual(self.app.entries(plan['id'], state='pending')['total'], plan['entries']['total'])

    def test_data_dir_on_another_volume_is_refused(self):
        plan = self.plan(LOGS_ONLY)
        real = os.fstat

        class Other:
            def __init__(self, st):
                self._st = st
                self.st_dev = st.st_dev + 1

            def __getattr__(self, name):
                return getattr(self._st, name)

        def fstat(fd):
            st = real(fd)
            return Other(st) if stat.S_ISDIR(st.st_mode) else st    # the staging folder, not the lock file

        with mock.patch('disk_steward.executor.os.fstat', fstat):
            with self.assertRaises(StewardError) as cm:
                self.run_plan(plan)
        self.assertEqual(cm.exception.message_key, 'error.staging_other_volume')
        self.assertTrue(self.fh.path(LOG).exists())


class RulesChangedTests(ExecCase):
    def test_catalog_change_touches_nothing(self):
        plan = self.plan()
        base = load_catalog(self.app.settings)
        extra = Rule(id='test.extra', provider='test', domain=Domain.AGENT, tier=Tier.JUNK, kind=Kind.CACHE,
                     patterns=('~/nowhere/**',), actions=(Action.DELETE,), default_action=Action.DELETE)
        changed = build_catalog(list(base.rules) + [extra], base.roots, base.privacy_skip)
        with mock.patch('disk_steward.executor.load_catalog', lambda settings: changed):
            with self.assertRaises(StewardError) as cm:
                self.run_plan(plan)
        self.assertEqual((cm.exception.code, cm.exception.details['changed']), (ErrorCode.RULES_CHANGED, ['catalog_hash']))
        self.assertEqual(self.app.entries(plan['id'], state='pending')['total'], 12)
        for path in self.rows(plan):
            self.assertTrue(os.path.lexists(self.fh.path(path)), path)
        self.assertEqual(self.plan_row(plan)['state'], 'draft')
        self.assertFalse(any(l['event'].startswith('entry.') for l in self.audit_lines()))

    def test_settings_change_touches_nothing(self):
        plan = self.plan()
        with self.assertRaises(StewardError) as cm:
            self.run_plan(plan, settings=Settings(domains=('agent',), idle_days=999))
        self.assertEqual(cm.exception.code, ErrorCode.RULES_CHANGED)
        self.assertIn('settings_hash', cm.exception.details['changed'])
        for path in self.rows(plan):
            self.assertTrue(os.path.lexists(self.fh.path(path)), path)

    def test_a_facts_change_keeps_exactly_the_entries_it_affects(self):
        # Facts drift while apps run; they never stop a plan as a whole. The entry whose verdict changed is kept.
        plan = self.plan()
        link = self.fh.path('~/.local/bin/claude')
        os.unlink(link)
        os.symlink(self.fh.path(V_OLD), link)   # `claude` now runs a version the plan deletes
        self.run_plan(plan)
        self.assertTrue(self.fh.path(V_OLD).exists())
        row = self.rows(plan)[V_OLD]
        self.assertEqual((row['state'], row['reason']), ('skipped', 'skip.rules_changed'))
        others = [r for path, r in self.rows(plan).items() if path != V_OLD]
        self.assertTrue(others and all(r['state'] == 'done' for r in others), others)

    def test_a_version_made_current_in_the_middle_of_a_run_is_kept(self):
        plan = self.plan([{'op': 'select_group', 'group': 'claude.cli-versions', 'on': True},
                          {'op': 'set_filter', 'min_age_days': 0}])
        rows = sorted(self.rows(plan).values(), key=lambda r: r['seq'])
        self.assertEqual(len(rows), 2)
        first, second = rows
        link = self.fh.path('~/.local/bin/claude')

        def repoint(entry):   # a Claude CLI update repoints `claude` while the plan runs
            if entry.seq == first['seq']:
                os.unlink(link)
                os.symlink(self.fh.path(second['path']), link)

        ex = self.executor(hooks={'after_claim': repoint})
        ex.facts_ttl = 0
        summary = self.run_plan(plan, executor=ex)
        self.assertEqual(self.outcome(plan, first['path']), ('done', ''))
        self.assertEqual(self.outcome(plan, second['path']), ('skipped', 'skip.rules_changed'))
        self.assertTrue(self.fh.path(second['path']).exists())
        self.assertEqual(summary['counts'], {'done': 1, 'skipped': 1})

    def test_removals_of_the_plan_itself_do_not_stop_the_run(self):
        plan = self.plan()   # includes two old versions: deleting them changes the facts (the version list)
        ex = self.executor()
        ex.facts_ttl = 0
        summary = self.run_plan(plan, executor=ex)
        self.assertEqual(summary['counts'], {'done': 12})


class PerEntryCheckTests(ExecCase):
    """Every case ends with the file still there and the entry skipped (or failed), never deleted."""

    def setUp(self):
        super().setUp()
        self.plan_ = self.plan(LOGS_ONLY)
        self.path = self.fh.path(LOG)

    def run_and_expect(self, reason, **kw):
        summary = self.run_plan(self.plan_, **kw)
        self.assert_kept(self.plan_, LOG, reason)
        self.assertEqual(summary['skipped_reasons'], {reason: 1})
        self.assertEqual(self.staging_left(self.plan_), [])
        return summary

    def test_inode_swapped_between_plan_and_run(self):
        data, st = self.path.read_bytes(), os.stat(self.path)
        replacement = self.path.with_name('x.new')
        replacement.write_bytes(data)
        os.utime(replacement, ns=(st.st_mtime_ns, st.st_mtime_ns))
        os.replace(replacement, self.path)
        self.run_and_expect('skip.changed')
        self.assertEqual(self.path.read_bytes(), data)

    def test_file_appended_after_the_plan(self):
        with open(self.path, 'ab') as f:
            f.write(b'appended')
        self.run_and_expect('skip.changed')
        self.assertTrue(self.path.read_bytes().endswith(b'appended'))

    def test_mtime_touched_after_the_plan(self):
        st = os.stat(self.path)
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        self.run_and_expect('skip.changed')

    def test_file_replaced_by_a_symlink(self):
        os.unlink(self.path)
        target = self.fh.path('~/precious.txt')
        target.write_text('precious')
        os.symlink(target, self.path)
        self.run_and_expect('skip.changed')
        self.assertEqual(target.read_text(), 'precious')
        self.assertTrue(os.path.islink(self.path))

    def test_file_replaced_by_a_directory(self):
        os.unlink(self.path)
        os.mkdir(self.path)
        self.run_and_expect('skip.changed')
        self.assertTrue(self.path.is_dir())

    def test_ancestor_replaced_by_a_symlink_after_the_claim(self):
        debug = self.fh.path('~/.claude/debug')
        moved = self.fh.path('~/.claude/debug.real')

        def swap(entry):
            os.rename(debug, moved)
            os.symlink(moved, debug)
        summary = self.run_plan(self.plan_, hooks={'after_claim': swap})
        self.assertEqual(self.outcome(self.plan_, LOG), ('skipped', 'skip.unsafe_parent'))
        self.assertTrue((moved / 'x.txt').exists())

    def test_ancestor_that_is_a_file_or_gone(self):
        debug = self.fh.path('~/.claude/debug')

        def gone(entry):
            os.rename(debug, self.fh.path('~/.claude/elsewhere'))
        self.run_plan(self.plan_, hooks={'after_claim': gone})
        self.assertEqual(self.outcome(self.plan_, LOG), ('skipped', 'skip.vanished'))
        self.assertTrue(self.fh.path('~/.claude/elsewhere/x.txt').exists())

    def test_file_becomes_hardlinked(self):
        os.link(self.path, self.fh.path('~/twin'))
        self.run_and_expect('skip.changed')
        self.assertTrue(self.fh.path('~/twin').exists())

    def test_file_vanished(self):
        os.unlink(self.path)
        self.run_plan(self.plan_)
        self.assertEqual(self.outcome(self.plan_, LOG), ('skipped', 'skip.vanished'))

    def patched_stat(self, change):
        real = actions.stat_at

        def stat_at(dir_fd, name):
            info = real(dir_fd, name)
            return dataclasses.replace(info, **change(info)) if name == 'x.txt' else info
        return mock.patch.object(actions, 'stat_at', stat_at)

    def test_owner_changed(self):
        with self.patched_stat(lambda i: {'uid': i.uid + 1}):
            self.run_and_expect('skip.foreign_owner')

    def test_immutable_flag(self):
        with self.patched_stat(lambda i: {'flags': i.flags | UF_IMMUTABLE}):
            self.run_and_expect('skip.locked')

    def test_dataless_flag(self):
        with self.patched_stat(lambda i: {'flags': i.flags | SF_DATALESS}):
            self.run_and_expect('skip.dataless')

    def test_other_volume(self):
        with self.patched_stat(lambda i: {'dev': i.dev + 1}):
            self.run_and_expect('skip.other_volume')

    def test_a_git_folder_appeared_next_to_it(self):
        os.mkdir(self.fh.path('~/.claude/debug/.git'))   # file_context now says: inside a work tree
        self.run_and_expect('skip.rules_changed')

    def test_a_database_appeared_next_to_it(self):
        self.fh.file('~/.claude/debug/state.sqlite', size=100)
        self.run_and_expect('skip.rules_changed')

    def test_age_below_the_threshold_because_the_clock_moved_back(self):
        self.fh.clock.advance(days=-30)
        self.run_and_expect('skip.too_recent')

    def test_age_check_uses_the_plans_threshold(self):
        plan = self.plan([{'op': 'select_group', 'group': 'claude.logs', 'on': True},
                          {'op': 'set_filter', 'min_age_days': 5}])   # exactly as old as the file
        self.assertEqual(plan['entries']['total'], 1)
        self.fh.clock.advance(days=-1)
        self.run_plan(plan)
        self.assertEqual(self.outcome(plan, LOG), ('skipped', 'skip.too_recent'))

    def test_blocked_by_a_running_app_and_the_others_still_run(self):
        plan = self.plan()
        self.mac.running = {CLAUDE_APP}
        summary = self.run_plan(plan)
        blocked = f'skip.blocked:app_running:{CLAUDE_APP}'
        for path in (CACHE, SHIPIT, '~/Library/Application Support/Claude/Partitions/p1/Cache/d',
                     '~/Library/Application Support/Claude/Code Cache/js/abc'):
            self.assert_kept(plan, path, blocked)
        for path in (LOG, CODEX_LOG, TMP_A, V_OLD, CURSOR_BUNDLE):
            self.assertEqual(self.outcome(plan, path), ('done', ''), path)
            self.assertFalse(os.path.lexists(self.fh.path(path)), path)
        self.assertEqual(summary['counts'], {'done': 8, 'skipped': 4})
        self.assertEqual(summary['skipped_reasons'], {blocked: 4})

    def test_open_file(self):
        st = os.stat(self.path)
        self.mac.open = OpenFiles(by_inode={(st.st_dev, st.st_ino): ['vim(42)']})
        self.run_and_expect('skip.blocked:open:vim(42)')

    def test_open_file_known_only_by_path(self):
        self.mac.open = OpenFiles(path_holders={str(self.path): ['tail(7)']})
        self.run_and_expect('skip.blocked:open:tail(7)')

    def test_executing_binary(self):
        st = os.stat(self.path)
        key = (st.st_dev, st.st_ino)
        self.mac.open = OpenFiles(by_inode={key: ['claude(456)']}, executing={key: ['claude(456)']})
        self.run_and_expect('skip.blocked:executing:claude(456)')

    def test_bundle_with_an_open_file_inside(self):
        plan = self.plan()
        inside = str(self.fh.path(f'{CURSOR_BUNDLE}/js'))
        self.mac.open = OpenFiles(path_holders={inside: ['Cursor(9)']})
        self.run_plan(plan)
        self.assert_kept(plan, CURSOR_BUNDLE, 'skip.blocked:open:Cursor(9)')
        self.assertTrue(self.fh.path(f'{CURSOR_BUNDLE}/js').exists())

    def test_bundle_with_a_running_binary_inside(self):
        plan = self.plan()
        inside = self.fh.path(f'{CURSOR_BUNDLE}/js')
        st = os.stat(inside)
        key = (st.st_dev, st.st_ino)
        self.mac.open = OpenFiles(by_inode={key: ['node(5)']}, executing={key: ['node(5)']},
                                  path_holders={str(inside): ['node(5)']})
        self.run_plan(plan)
        self.assert_kept(plan, CURSOR_BUNDLE, 'skip.blocked:executing:node(5)')

    def test_unexpected_exception_fails_only_that_entry(self):
        plan = self.plan()

        def boom(entry):
            if entry.path == LOG:
                raise ValueError('kaboom')
        summary = self.run_plan(plan, hooks={'after_guard': boom})
        state, reason = self.outcome(plan, LOG)
        self.assertEqual(state, 'failed')
        self.assertEqual(reason, 'fail.exception:ValueError: kaboom')
        self.assertTrue(self.fh.path(LOG).exists())
        self.assertEqual(summary['counts'], {'done': 11, 'failed': 1})
        self.assertEqual(self.staging_left(plan), [])

    def test_failure_inside_the_action_puts_the_file_back(self):
        real = os.unlink

        def unlink(name, dir_fd=None):
            if dir_fd is not None and name == '1':
                raise PermissionError('read-only volume')
            real(name, dir_fd=dir_fd)
        with mock.patch.object(actions.os, 'unlink', unlink):
            self.run_plan(self.plan_)
        state, reason = self.outcome(self.plan_, LOG)
        self.assertEqual(state, 'failed')
        self.assertIn('PermissionError', reason)
        self.assertTrue(self.path.exists())
        self.assertEqual(self.staging_left(self.plan_), [])

    def test_hooks_fire_in_checklist_order(self):
        order = []
        names = ('after_claim', 'after_parent', 'after_lstat', 'after_fingerprint', 'after_classify', 'after_guard',
                 'before_unlink')
        self.run_plan(self.plan_, hooks={n: (lambda e, n=n: order.append(n)) for n in names})
        self.assertEqual(order, list(names))

    def test_unknown_hook_names_are_rejected(self):
        with self.assertRaises(ValueError):
            self.executor(hooks={'after_lunch': lambda e: None})


class StagedNameTakenTests(ExecCase):
    def test_original_name_recreated_while_the_item_is_staged(self):
        plan = self.plan(LOGS_ONLY)
        seq = self.seq_of(plan, LOG)
        real = actions.stat_at
        path = self.fh.path(LOG)

        def stat_at(dir_fd, name):
            info = real(dir_fd, name)
            if name == str(seq):   # the check of the staged file: somebody makes a new file under the old name
                path.write_text('a new file with the old name')
                return dataclasses.replace(info, size=info.size + 1)
            return info
        with mock.patch.object(actions, 'stat_at', stat_at):
            summary = self.run_plan(plan)
        state, reason = self.outcome(plan, LOG)
        staged = self.fh.data_dir / 'staging' / plan['id'] / str(seq)
        self.assertEqual((state, reason), ('needs_attention', f'attention.staged_left:{staged}'))
        self.assertTrue(staged.exists())
        self.assertEqual(path.read_text(), 'a new file with the old name')
        self.assertEqual(summary['counts'], {'needs_attention': 1})
        self.assertEqual(self.plan_row(plan)['state'], 'done')


SUPPORT = '~/Library/Application Support'
NOTION_CACHE = f'{SUPPORT}/Notion/Cache/Cache_Data/a'
NOTION_GPU = f'{SUPPORT}/Notion/GPUCache/data_0'
NOTION = [{'op': 'select_group', 'group': f'app.electron.cache:{SUPPORT}/Notion', 'on': True},
          {'op': 'set_filter', 'min_age_days': 0}]


def installed(stem, bundle_id):
    return {'path': f'/Applications/{stem}.app', 'name': stem, 'display_name': '', 'executable': '', 'stem': stem}


class RequiresQuitEvidenceTests(ExecCase):
    """A generic Electron cache learns its app at scan time: the quit requirement lives in the item's evidence."""
    settings = Settings(domains=('agent', 'app-library'))
    mac_kwargs = dict(app_info={'notion.id': installed('Notion', 'notion.id')})

    def setUp(self):
        super().setUp()
        self.fh.file(NOTION_CACHE, size=100, age_days=30)
        self.fh.file(NOTION_GPU, size=100, age_days=30)

    def test_the_rule_itself_names_no_app(self):
        self.assertEqual(load_catalog(self.app.settings).rule('app.electron.cache').quit_apps, ())

    def test_a_running_app_blocks_what_the_evidence_names(self):
        plan = self.plan(NOTION)
        self.assertEqual(plan['entries']['total'], 2)
        self.mac.running = {'notion.id'}
        summary = self.run_plan(plan)
        for path in (NOTION_CACHE, NOTION_GPU):
            self.assert_kept(plan, path, 'skip.blocked:app_running:notion.id')
        self.assertEqual(summary['skipped_reasons'], {'skip.blocked:app_running:notion.id': 2})

    def test_the_same_entries_are_removed_once_the_app_is_quit(self):
        plan = self.plan(NOTION)
        summary = self.run_plan(plan)
        self.assertEqual(summary['counts'], {'done': 2})
        self.assertFalse(self.fh.path(NOTION_CACHE).exists())

    def test_an_app_that_is_not_the_owner_does_not_block(self):
        plan = self.plan(NOTION)
        self.mac.running = {'some.other.app'}
        self.assertEqual(self.run_plan(plan)['counts'], {'done': 2})

    def test_two_apps_sharing_a_folder_name_must_both_be_quit(self):
        self.mac.app_info['twin.id'] = installed('Notion', 'twin.id')
        plan = self.plan(NOTION)
        self.mac.running = {'twin.id'}
        self.run_plan(plan)
        self.assert_kept(plan, NOTION_CACHE, 'skip.blocked:app_running:twin.id')

    def test_the_scan_rows_are_not_needed(self):
        # the fresh classification names the app on its own; a pruned scan row is no obstacle
        plan = self.plan(NOTION)
        self.mac.running = {'notion.id'}
        gone = StewardError(ErrorCode.NOT_FOUND, 'error.NOT_FOUND', what='item', id='x')
        with mock.patch.object(type(self.app.store), 'item', side_effect=gone):
            self.run_plan(plan)
        self.assert_kept(plan, NOTION_CACHE, 'skip.blocked:app_running:notion.id')

    def test_a_requirement_the_scan_recorded_still_counts_when_the_app_is_no_longer_found(self):
        plan = self.plan(NOTION)
        self.app.store.set_plan_state(plan['id'], PlanState.PAUSED)   # a started plan: facts are not compared
        self.mac.app_info.clear()                                       # the fresh classification lost the app
        self.mac.running = {'notion.id'}
        self.run_plan(plan)
        self.assert_kept(plan, NOTION_CACHE, 'skip.blocked:app_running:notion.id')

    def test_without_the_scan_row_the_lost_app_cannot_be_known(self):
        plan = self.plan(NOTION)
        self.app.store.set_plan_state(plan['id'], PlanState.PAUSED)
        self.mac.app_info.clear()
        self.mac.running = {'notion.id'}
        gone = StewardError(ErrorCode.NOT_FOUND, 'error.NOT_FOUND', what='item', id='x')
        with mock.patch.object(type(self.app.store), 'item', side_effect=gone):
            self.run_plan(plan)
        self.assertEqual(self.outcome(plan, NOTION_CACHE), ('done', ''))   # nothing says it needs quitting any more

    def test_a_folder_whose_app_was_uninstalled_since_has_nothing_to_quit(self):
        plan = self.plan(NOTION)
        self.mac.app_info.clear()    # facts changed: the cache is still a cache, and nothing is running to quit
        self.run_plan(plan)
        self.assertFalse(self.fh.path(NOTION_CACHE).exists())

    def test_the_app_named_at_scan_time_still_blocks_after_it_vanishes_from_the_app_list(self):
        plan = self.plan(NOTION)
        self.mac.app_info.clear()
        self.mac.running = {'notion.id'}   # the scan recorded the requirement; losing the app from mdfind drops nothing
        self.run_plan(plan)
        self.assert_kept(plan, NOTION_CACHE, 'skip.blocked:app_running:notion.id')


class CrashInsideRemovalTests(ExecCase):
    """A crash in the middle of removing a staged bundle leaves a half-deleted tree in staging."""
    catalog = staticmethod(scratch_catalog)

    def setUp(self):
        super().setUp()
        for name in ('a', 'b', 'c', 'd'):
            self.fh.file(f'~/scratch/b1/{name}.bin', content=os.urandom(20_000), age_days=20)
        self.fh.file('~/scratch/b1/sub/e.bin', content=os.urandom(20_000), age_days=20)
        self.fh.dir('~/scratch/b1/sub', age_days=20)
        self.fh.dir('~/scratch/b1', age_days=20)
        self.before = tree_snapshot(self.fh.path('~/scratch/b1'))
        self.ops = [{'op': 'select_group', 'group': 'test.bundle', 'on': True},
                    {'op': 'set_filter', 'min_age_days': 14}]

    def staged(self, plan):
        return self.fh.data_dir / 'staging' / plan['id'] / str(self.seq_of(plan, '~/scratch/b1'))

    def crash_inside_rmtree(self, plan, unlinks=2):
        """Run the plan and die (SystemExit) after `unlinks` files of the staged bundle were removed."""
        state = {'armed': False, 'n': 0}
        real = os.unlink

        def unlink(name, *args, **kwargs):
            if state['armed']:
                state['n'] += 1
                if state['n'] > unlinks:
                    raise SystemExit('simulated crash inside rmtree')
            return real(name, *args, **kwargs)
        with mock.patch.object(os, 'unlink', unlink):
            with self.assertRaises(SystemExit):
                self.run_plan(plan, hooks={'before_unlink': lambda e: state.update(armed=True)})
        left = self.staged(plan)
        self.assertTrue(left.is_dir())
        self.assertEqual(len([p for p in left.rglob('*') if p.is_file()]), 5 - unlinks)   # really half removed
        self.assertFalse(self.fh.path('~/scratch/b1').exists())

    def recover(self):
        with self.app.lock:
            return self.executor().recover()

    def audit_events(self):
        return [l['event'] for l in self.audit_lines()]

    def test_half_removed_archived_bundle_is_finished_and_its_archive_kept(self):
        plan = self.plan(self.ops)
        self.crash_inside_rmtree(plan)
        (row,) = self.app.store.archives()
        report = self.recover()
        self.assertEqual([(e['state'], e['reason']) for e in report['entries']], [('done', 'archive.stored')])
        self.assertFalse(self.staged(plan).exists())                    # the removal was finished ...
        self.assertEqual(self.app.store.archive(row['id'])['state'], 'stored')   # ... and the archive was not touched
        self.assertTrue((self.fh.data_dir / 'archives' / f'{row["id"]}.tar.gz').exists())
        self.assertNotIn('archive.discarded', self.audit_events())
        self.assertIn('recover.intent', self.audit_events())
        self.assertEqual(self.outcome(plan, '~/scratch/b1'), ('done', 'archive.stored'))
        self.app.restore(row['id'])                                      # the archive has all of it
        self.assertEqual(tree_snapshot(self.fh.path('~/scratch/b1')), self.before)

    def test_half_removed_bundle_of_a_delete_is_finished(self):
        plan = self.plan(self.ops + [{'op': 'set_action', 'group': 'test.bundle', 'action': 'delete'}])
        self.crash_inside_rmtree(plan)
        report = self.recover()
        self.assertEqual([(e['state'], e['reason']) for e in report['entries']], [('done', '')])
        self.assertFalse(self.staged(plan).exists())
        self.assertFalse(self.fh.path('~/scratch/b1').exists())          # never put back as a partial tree

    def test_half_removed_bundle_without_a_good_archive_is_never_put_back(self):
        plan = self.plan(self.ops)
        self.crash_inside_rmtree(plan)
        (row,) = self.app.store.archives()
        gz = self.fh.data_dir / 'archives' / f'{row["id"]}.tar.gz'
        blob = bytearray(gz.read_bytes())
        blob[len(blob) // 2] ^= 0xFF
        gz.write_bytes(bytes(blob))
        self.recover()
        state, reason = self.outcome(plan, '~/scratch/b1')
        self.assertEqual((state, reason), ('needs_attention', f'attention.staged_left:{self.staged(plan)}'))
        self.assertTrue(self.staged(plan).is_dir())                      # what is left stays where the user can find it
        self.assertFalse(self.fh.path('~/scratch/b1').exists())
        self.assertEqual(self.app.store.archive(row['id'])['state'], 'stored')   # the damaged archive is not discarded

    def test_a_whole_bundle_without_a_good_archive_goes_back_and_only_then_is_its_archive_discarded(self):
        plan = self.plan(self.ops)
        self.crash(plan, 'before_unlink', lambda e: True)      # archived and staged, nothing removed yet
        (row,) = self.app.store.archives()
        gz = self.fh.data_dir / 'archives' / f'{row["id"]}.tar.gz'
        gz.write_bytes(gz.read_bytes()[:-50])
        report = self.recover()
        self.assertEqual([(e['state'], e['reason']) for e in report['entries']], [('pending', 'interrupted')])
        self.assertEqual(tree_snapshot(self.fh.path('~/scratch/b1')), self.before)      # whole, so it may go back
        self.assertEqual(self.app.store.archive(row['id'])['state'], 'discarded')       # and the bad archive goes
        self.assertIn('archive.discarded', self.audit_events())

    def crash(self, plan, hook_name, when):
        def hook(entry):
            if when(entry):
                raise SystemExit('simulated crash')
        with self.assertRaises(SystemExit):
            self.run_plan(plan, hooks={hook_name: hook})

    def test_a_staged_bundle_that_is_not_the_planned_folder_goes_back_untouched(self):
        plan = self.plan(self.ops)
        self.crash(plan, 'before_unlink', lambda e: True)
        staged = self.staged(plan)
        # swap the staged folder for a different one, as if a stranger had been renamed in at staging time
        stranger = staged.with_name('stranger')
        stranger.mkdir()
        (stranger / 'theirs.txt').write_text('not ours')
        import shutil
        shutil.rmtree(staged)
        os.rename(stranger, staged)
        (row,) = self.app.store.archives()
        self.recover()
        self.assertEqual(self.outcome(plan, '~/scratch/b1'), ('skipped', 'skip.changed'))
        self.assertEqual((self.fh.path('~/scratch/b1/theirs.txt')).read_text(), 'not ours')
        self.assertEqual(self.app.store.archive(row['id'])['state'], 'stored')    # it did not match: archive kept

    def test_recovery_audits_what_it_removes(self):
        plan = self.plan(self.ops)
        self.crash(plan, 'archive_chunk', lambda e: True)
        self.recover()
        self.assertIn('part.removed', self.audit_events())


class RecreatedNameTests(ExecCase):
    catalog = staticmethod(scratch_catalog)

    def setUp(self):
        super().setUp()
        self.path = self.fh.file('~/scratch-files/f.bin', content=os.urandom(50_000), age_days=20)
        self.ops = [{'op': 'select_group', 'group': 'test.file', 'on': True}, {'op': 'set_filter', 'min_age_days': 14}]

    def crash_before_unlink(self, plan):
        def die(entry):
            raise SystemExit('simulated crash')
        with self.assertRaises(SystemExit):
            self.run_plan(plan, hooks={'before_unlink': die})
        seq = self.seq_of(plan, '~/scratch-files/f.bin')
        return self.fh.data_dir / 'staging' / plan['id'] / str(seq)

    def recover(self):
        with self.app.lock:
            return self.executor().recover()

    def test_a_new_file_under_the_old_name_after_the_unlink_keeps_the_archive(self):
        plan = self.plan(self.ops)
        staged = self.crash_before_unlink(plan)
        os.unlink(staged)                                   # the unlink the crash cut off ...
        self.path.write_text('the app wrote a fresh file here')   # ... and the app starts over under the same name
        (row,) = self.app.store.archives()
        self.recover()
        self.assertEqual(self.outcome(plan, '~/scratch-files/f.bin'), ('done', 'archive.stored'))
        self.assertEqual(self.app.store.archive(row['id'])['state'], 'stored')
        self.assertEqual(self.path.read_text(), 'the app wrote a fresh file here')
        self.assertEqual(self.rows(plan)['~/scratch-files/f.bin']['archive_id'], row['id'])

    def test_the_same_with_an_archive_that_no_longer_verifies_needs_attention(self):
        plan = self.plan(self.ops)
        staged = self.crash_before_unlink(plan)
        os.unlink(staged)
        self.path.write_text('fresh')
        (row,) = self.app.store.archives()
        gz = self.fh.data_dir / 'archives' / f'{row["id"]}.gz'
        gz.write_bytes(gz.read_bytes()[:-20])
        self.recover()
        state, reason = self.outcome(plan, '~/scratch-files/f.bin')
        self.assertEqual((state, reason), ('needs_attention', f'attention.archive_unverified:{row["id"]}'))
        self.assertEqual(self.app.store.archive(row['id'])['state'], 'stored')
        self.assertEqual(self.path.read_text(), 'fresh')

    def test_a_new_file_after_a_plain_delete_is_left_alone(self):
        plan = self.plan(self.ops + [{'op': 'set_action', 'group': 'test.file', 'action': 'delete'}])
        staged = self.crash_before_unlink(plan)
        os.unlink(staged)
        self.path.write_text('fresh')
        self.recover()
        self.assertEqual(self.outcome(plan, '~/scratch-files/f.bin'), ('done', 'recovered.gone'))
        self.assertEqual(self.path.read_text(), 'fresh')

    def test_the_original_still_in_place_means_nothing_was_removed(self):
        plan = self.plan(self.ops)
        staged = self.crash_before_unlink(plan)
        os.rename(staged, self.path)                        # as if the crash came before the rename into staging
        self.recover()
        self.assertEqual(self.outcome(plan, '~/scratch-files/f.bin'), ('pending', 'interrupted'))
        self.assertEqual([a['state'] for a in self.app.store.archives()], ['discarded'])
        self.assertIn('archive.discarded', [l['event'] for l in self.audit_lines()])


class UnregisteredArchiveTests(ExecCase):
    catalog = staticmethod(scratch_catalog)

    def setUp(self):
        super().setUp()
        self.path = self.fh.file('~/scratch-files/f.bin', content=os.urandom(50_000), age_days=20)
        self.data = self.path.read_bytes()
        self.plan_ = self.plan([{'op': 'select_group', 'group': 'test.file', 'on': True},
                                {'op': 'set_filter', 'min_age_days': 14}])

        def die(entry):
            raise SystemExit('simulated crash')
        with self.assertRaises(SystemExit):
            self.run_plan(self.plan_, hooks={'before_unlink': die})
        (self.row,) = self.app.store.archives()
        con = sqlite3.connect(self.fh.data_dir / 'steward.db')   # the crash came before the row was committed
        try:
            con.execute('DELETE FROM archives WHERE id = ?', (self.row['id'],))
            con.commit()
        finally:
            con.close()
        self.assertEqual(self.app.store.archives(), [])

    def recover(self):
        with self.app.lock:
            return self.executor().recover()

    def events(self):
        return [l['event'] for l in self.audit_lines()]

    def test_an_archive_whose_sidecar_and_content_verify_is_registered_and_the_removal_finished(self):
        report = self.recover()
        self.assertEqual([(e['state'], e['reason']) for e in report['entries']], [('done', 'archive.stored')])
        (row,) = self.app.store.archives()
        self.assertEqual((row['id'], row['state']), (self.row['id'], 'stored'))
        self.assertEqual(self.rows(self.plan_)['~/scratch-files/f.bin']['archive_id'], row['id'])
        self.assertIn('archive.reregistered', self.events())
        self.assertEqual(self.staging_left(self.plan_), [])
        self.app.restore(row['id'])
        self.assertEqual(self.path.read_bytes(), self.data)

    def test_an_archive_that_does_not_verify_is_removed_and_the_file_goes_back(self):
        gz = self.fh.data_dir / 'archives' / f'{self.row["id"]}.gz'
        blob = bytearray(gz.read_bytes())
        blob[100] ^= 0xFF
        gz.write_bytes(bytes(blob))
        self.recover()
        self.assertEqual(self.outcome(self.plan_, '~/scratch-files/f.bin'), ('pending', 'interrupted'))
        self.assertEqual(self.path.read_bytes(), self.data)
        self.assertEqual(os.listdir(self.fh.data_dir / 'archives'), [])
        self.assertIn('archive.unregistered_removed', self.events())

    def test_a_sidecar_that_describes_another_entry_is_not_trusted(self):
        sidecar = self.fh.data_dir / 'archives' / f'{self.row["id"]}.json'
        manifest = json.loads(sidecar.read_text())
        manifest['path'] = '~/scratch-files/other.bin'
        sidecar.write_text(json.dumps(manifest))
        self.recover()
        self.assertEqual(self.app.store.archives(), [])
        self.assertEqual(self.outcome(self.plan_, '~/scratch-files/f.bin'), ('pending', 'interrupted'))
        self.assertEqual(self.path.read_bytes(), self.data)


class ResumeTests(ExecCase):
    """A run that was paused for hours is checked again before it goes on."""

    def setUp(self):
        super().setUp()
        self.plan_ = self.plan([{'op': 'select_group', 'group': g, 'on': True}
                                for g in ('claude.logs', 'codex.logs', 'codex.cache')]
                               + [{'op': 'set_filter', 'min_age_days': 0}])
        self.assertEqual(self.plan_['entries']['total'], 3)
        self.control = Control()
        self.result = {}

        def pause(entry):
            if entry.seq == 2:
                self.control.pause()

        def work():
            try:
                self.result['summary'] = self.run_plan(self.plan_, hooks={'after_claim': pause}, control=self.control)
            except BaseException as e:
                self.result['error'] = e
        self.thread = threading.Thread(target=work)
        self.thread.start()
        self.wait_for(lambda: self.app.store.plan(self.plan_['id']).state == PlanState.PAUSED)

    def resume(self):
        self.control.resume()
        self.thread.join(30)
        self.assertFalse(self.thread.is_alive())

    def assert_stays_paused(self, code, text):
        error = self.result['error']
        self.assertIsInstance(error, StewardError)
        self.assertEqual(error.code, code)
        row = self.plan_row(self.plan_)
        self.assertEqual(row['state'], 'paused')
        self.assertIn(text, row['error'])
        self.assertEqual(self.app.entries(self.plan_['id'], state='pending')['total'], 1)   # the third was not touched

    def pending_path(self):
        (row,) = self.app.entries(self.plan_['id'], state='pending')['rows']
        return self.fh.path(row['path'])

    def test_a_plan_that_expired_during_the_pause_does_not_go_on(self):
        self.fh.clock.advance(days=2)
        self.resume()
        self.assert_stays_paused(ErrorCode.PLAN_EXPIRED, 'PLAN_EXPIRED')
        self.assertTrue(self.pending_path().exists())

    def test_settings_changed_on_disk_during_the_pause_stop_the_run(self):
        settings_mod.save(self.fh.data_dir, Settings(domains=('agent',), idle_days=999))
        self.resume()
        self.assert_stays_paused(ErrorCode.RULES_CHANGED, 'RULES_CHANGED')
        self.assertIn('settings_hash', self.result['error'].details['changed'])
        self.assertTrue(self.pending_path().exists())

    def test_settings_rewritten_without_a_change_are_fine(self):
        settings_mod.save(self.fh.data_dir, self.app.settings)
        self.resume()
        self.assertEqual(self.result['summary']['counts'], {'done': 3})

    def test_facts_are_not_compared_for_a_started_plan(self):
        link = self.fh.path('~/.local/bin/claude')
        os.unlink(link)
        os.symlink(self.fh.path(V_OLD), link)
        self.resume()
        self.assertEqual(self.result['summary']['counts'], {'done': 3})

    def test_an_exclusion_made_during_the_pause_does_not_count_as_an_edit(self):
        (row,) = self.app.entries(self.plan_['id'], state='pending')['rows']
        self.app.exclude(self.plan_['id'], [row['seq']], self.plan_row(self.plan_)['revision'])
        self.resume()
        self.assertEqual(self.result['summary']['counts'], {'done': 2, 'excluded': 1})
        self.assertTrue(self.fh.path(row['path']).exists())


class LastUsedTests(ExecCase):
    catalog = staticmethod(scratch_catalog)
    OPS = [{'op': 'select_group', 'group': 'test.used', 'on': True}, {'op': 'set_filter', 'min_age_days': 14}]

    def setUp(self):
        super().setUp()
        self.files = {}
        for name in ('a.bin', 'b.bin'):
            path = self.fh.file(f'~/scratch-used/{name}', size=100, age_days=30)
            self.files[name] = path
            self.mac.last_used_map[str(path)] = self.fh.clock() - 30 * 86400    # Spotlight agrees: idle for a month

    def used(self, name, days_ago):
        self.mac.last_used_map[str(self.files[name])] = self.fh.clock() - days_ago * 86400

    def test_idle_files_are_removed(self):
        plan = self.plan(self.OPS)
        self.assertEqual(plan['entries']['total'], 2)
        self.assertEqual(self.run_plan(plan)['counts'], {'done': 2})

    def test_a_file_opened_since_the_scan_is_kept(self):
        plan = self.plan(self.OPS)
        self.used('a.bin', 1)                        # the user opened it after the scan; its mtime did not change
        summary = self.run_plan(plan)
        self.assert_kept(plan, '~/scratch-used/a.bin', 'skip.too_recent')
        self.assertEqual(self.outcome(plan, '~/scratch-used/b.bin'), ('done', ''))
        self.assertEqual(summary['counts'], {'done': 1, 'skipped': 1})

    def test_an_unreadable_last_used_date_skips_the_entry(self):
        plan = self.plan(self.OPS)
        self.mac.last_used = mock.Mock(side_effect=RuntimeError('mdls exited with 1'))
        summary = self.run_plan(plan)
        for name in self.files:
            self.assert_kept(plan, f'~/scratch-used/{name}', 'skip.last_used_unknown')
        self.assertEqual(summary['skipped_reasons'], {'skip.last_used_unknown': 2})
        self.assertIn('run.last_used_failed', [l['event'] for l in self.audit_lines()])

    def test_the_dates_are_read_once_for_the_whole_run(self):
        plan = self.plan(self.OPS)
        before = len([c for c in self.mac.calls if c[0] == 'last_used'])
        self.run_plan(plan)
        calls = [c for c in self.mac.calls if c[0] == 'last_used'][before:]
        self.assertEqual(len(calls), 1)
        self.assertEqual(sorted(calls[0][1][0]), sorted(str(p) for p in self.files.values()))

    def test_values_older_than_the_snapshot_ttl_are_read_again(self):
        plan = self.plan(self.OPS)
        rows = sorted(self.rows(plan).values(), key=lambda r: r['seq'])
        first, second = rows
        name = second['path'].rpartition('/')[2]

        def later(entry):
            if entry.seq == first['seq']:
                self.fh.clock.advance(seconds=6)       # past the ttl ...
                self.used(name, 1)                     # ... and the user opens the next file meanwhile
        summary = self.run_plan(plan, hooks={'after_claim': later})
        self.assertEqual(self.outcome(plan, first['path']), ('done', ''))
        self.assert_kept(plan, second['path'], 'skip.too_recent')
        self.assertEqual(summary['counts'], {'done': 1, 'skipped': 1})

    def test_the_scan_row_is_optional(self):
        plan = self.plan(self.OPS)
        gone = StewardError(ErrorCode.NOT_FOUND, 'error.NOT_FOUND', what='item', id='x')
        with mock.patch.object(type(self.app.store), 'item', side_effect=gone):
            self.assertEqual(self.run_plan(plan)['counts'], {'done': 2})


class AbortTests(ExecCase):
    def test_a_lost_operation_lock_stops_the_run(self):
        plan = self.plan()

        class Lock:
            held = True
        lock = Lock()
        ex = self.executor(hooks={'after_claim': lambda e: setattr(lock, 'held', False) if e.seq == 2 else None})
        ex.lock = lock
        with self.assertRaises(RuntimeError) as cm:
            ex.run(plan['id'], plan['revision'], plan['entries_digest'], Control())
        self.assertIn('lock was lost', str(cm.exception))
        row = self.plan_row(plan)
        self.assertEqual(row['state'], 'paused')
        self.assertIn('LockLost', row['error'])
        self.assertEqual(self.app.entries(plan['id'], state='done')['total'], 2)
        self.assertEqual(self.app.entries(plan['id'], state='pending')['total'], 10)

    def test_a_store_error_aborts_the_run_and_recovery_settles_the_entry(self):
        plan = self.plan(LOGS_ONLY)

        def finish(plan_id, seq, outcome):
            raise sqlite3.OperationalError('disk I/O error')
        with mock.patch.object(type(self.app.store), 'finish_entry', side_effect=finish):
            with self.assertRaises(sqlite3.OperationalError):
                self.run_plan(plan)
        row = self.plan_row(plan)
        self.assertEqual(row['state'], 'paused')
        self.assertIn('disk I/O error', row['error'])
        self.assertEqual(self.outcome(plan, LOG)[0], 'in_progress')          # the file is gone, the record is not written
        self.assertFalse(self.fh.path(LOG).exists())
        with self.app.lock:
            report = self.executor().recover()
        self.assertEqual([(e['state'], e['reason']) for e in report['entries']], [('done', 'recovered.gone')])


class MidRunTests(ExecCase):
    def test_entry_excluded_between_entries_is_never_touched(self):
        plan = self.plan()
        target = TMP_A
        target_seq = self.seq_of(plan, target)
        done = []

        def exclude(entry):
            if entry.seq == 1 and not done:
                done.append(self.app.exclude(plan['id'], [target_seq], plan['revision']))
        summary = self.run_plan(plan, hooks={'after_claim': exclude})
        self.assertEqual(done, [2])
        self.assertTrue(self.fh.path(target).exists())
        self.assertEqual(self.outcome(plan, target), ('excluded', ''))
        self.assertEqual(summary['counts'], {'done': 11, 'excluded': 1})
        self.assertEqual(self.plan_row(plan)['state'], 'done')

    def test_snapshot_failure_before_the_run_aborts_it(self):
        plan = self.plan()
        self.mac.open_files = mock.Mock(side_effect=RuntimeError('lsof exited with 2'))
        with self.assertRaises(RuntimeError) as cm:
            self.run_plan(plan)
        self.assertIsInstance(cm.exception, SnapshotError)
        row = self.plan_row(plan)
        self.assertEqual(row['state'], 'paused')
        self.assertIn('lsof exited with 2', row['error'])
        for path in self.rows(plan):
            self.assertTrue(os.path.lexists(self.fh.path(path)), path)
        self.assertEqual(self.app.entries(plan['id'], state='pending')['total'], 12)

    def test_snapshot_failure_in_the_middle_stops_every_further_deletion(self):
        plan = self.plan()
        failing = []

        def go_stale(entry):
            if entry.seq == 3 and not failing:   # ten seconds later the snapshot is too old, and lsof now fails
                failing.append(1)
                self.fh.clock.advance(seconds=10)
                self.mac.open_files = mock.Mock(side_effect=RuntimeError('lsof timed out'))
        with self.assertRaises(SnapshotError):
            self.run_plan(plan, hooks={'after_claim': go_stale})
        row = self.plan_row(plan)
        self.assertEqual((row['state'], 'lsof timed out' in row['error']), ('paused', True))
        states = {r['seq']: r['state'] for r in self.rows(plan).values()}
        self.assertEqual([states[1], states[2]], ['done', 'done'])
        self.assertEqual(states[3], 'pending')     # claimed, but nothing happened: back in the queue
        self.assertEqual(set(states[s] for s in range(4, 13)), {'pending'})
        gone = [p for p, r in self.rows(plan).items() if not os.path.lexists(self.fh.path(p))]
        self.assertEqual(len(gone), 2)

    def test_snapshot_is_refreshed_after_its_ttl(self):
        plan = self.plan()
        advance = lambda entry: self.fh.clock.advance(seconds=6)
        self.run_plan(plan, hooks={'after_claim': advance})
        calls = [c for c in self.mac.calls if c[0] == 'open_files']
        self.assertGreaterEqual(len(calls), 12)

    def test_snapshot_is_reused_within_its_ttl(self):
        plan = self.plan()
        self.run_plan(plan)
        self.assertEqual([c[0] for c in self.mac.calls].count('open_files'), 1)

    def test_cancel_marks_the_rest_cancelled(self):
        plan = self.plan()
        control = Control()

        def cancel(entry):
            if entry.seq == 2:
                control.cancel()
        events = []
        summary = self.run_plan(plan, hooks={'after_claim': cancel}, control=control, events=events)
        self.assertEqual(summary['state'], 'cancelled')
        self.assertEqual(summary['counts'], {'done': 2, 'cancelled': 10})
        self.assertEqual(self.plan_row(plan)['state'], 'cancelled')
        left = [p for p in self.rows(plan) if os.path.lexists(self.fh.path(p))]
        self.assertEqual(len(left), 10)
        self.assertEqual(events[-1][0], 'run.finished')

    def test_cancel_before_start(self):
        plan = self.plan()
        control = Control()
        control.cancel()
        summary = self.run_plan(plan, control=control)
        self.assertEqual(summary['counts'], {'cancelled': 12})
        for path in self.rows(plan):
            self.assertTrue(os.path.lexists(self.fh.path(path)), path)

    def test_pause_between_entries_blocks_until_resume(self):
        plan = self.plan()
        control = Control()
        events = []
        gate = threading.Event()

        def pause(entry):
            if entry.seq == 2:
                control.pause()
        result = {}

        def work():
            result['summary'] = self.run_plan(plan, hooks={'after_claim': pause}, control=control, events=events)
            gate.set()
        t = threading.Thread(target=work)
        t.start()
        self.wait_for(lambda: self.app.store.plan(plan['id']).state == PlanState.PAUSED)
        self.assertEqual(self.app.store.count_entries(plan['id'], EntryState.DONE), 2)   # the entry in flight finished
        self.assertEqual(self.app.store.count_entries(plan['id'], EntryState.PENDING), 10)
        self.assertIn('run.paused', [k for k, _ in events])
        self.assertFalse(gate.is_set())
        control.resume()
        t.join(30)
        self.assertEqual(result['summary']['counts'], {'done': 12})
        self.assertIn('run.resumed', [k for k, _ in events])

    def test_cancel_while_paused(self):
        plan = self.plan()
        control = Control()

        def pause(entry):
            if entry.seq == 2:
                control.pause()
        result = {}
        t = threading.Thread(target=lambda: result.update(summary=self.run_plan(plan, hooks={'after_claim': pause},
                                                                              control=control)))
        t.start()
        self.wait_for(lambda: self.app.store.plan(plan['id']).state == PlanState.PAUSED)
        control.cancel()
        t.join(30)
        self.assertEqual(result['summary']['counts'], {'done': 2, 'cancelled': 10})


class ArchiveRunTests(ExecCase):
    def setUp(self):
        super().setUp()
        self.big = self.fh.file(BIG, content=os.urandom(300_000) + b'z' * 300_000, age_days=20)
        self.projects = [{'op': 'select_group', 'group': PROJECTS, 'on': True},
                         {'op': 'set_filter', 'min_age_days': 14}]
        patcher = mock.patch.object(actions, 'CHUNK', 64 * 1024)
        patcher.start()
        self.addCleanup(patcher.stop)

    def part_files(self):
        d = self.fh.data_dir / 'archives'
        return [n for n in os.listdir(d) if n.endswith('.part')] if d.exists() else []

    def test_archive_run_and_restore(self):
        data = self.big.read_bytes()
        plan = self.plan(self.projects)
        self.assertEqual(plan['totals']['by_action']['archive']['count'], 4)
        summary = self.run_plan(plan)
        self.assertEqual(summary['counts'], {'done': 4})
        self.assertFalse(self.big.exists())
        row = self.rows(plan)[BIG]
        self.assertEqual(row['reason'], 'archive.stored')
        archive = self.app.store.archive(row['archive_id'])
        self.assertEqual(row['freed'], row['reclaim'] - archive['stored_bytes'])
        self.assertEqual(summary['freed'], sum(r['freed'] for r in self.rows(plan).values()))
        self.assertLess(archive['stored_bytes'], archive['orig_size'])     # half of it was compressible
        self.app.restore(row['archive_id'])
        self.assertEqual(self.big.read_bytes(), data)

    def test_pause_in_the_middle_of_an_archive_rolls_it_back_and_resume_completes_it(self):
        data = self.big.read_bytes()
        plan = self.plan(self.projects)
        control = Control()
        paused_once = []
        observed = {}

        def pause(entry):
            if entry.path == BIG and not paused_once:
                paused_once.append(1)
                control.pause()
        result = {}
        t = threading.Thread(target=lambda: result.update(summary=self.run_plan(plan, hooks={'archive_chunk': pause},
                                                                              control=control)))
        t.start()
        self.wait_for(lambda: self.app.store.plan(plan['id']).state == PlanState.PAUSED)
        row = self.rows(plan)[BIG]
        observed.update(state=row['state'], reason=row['reason'], exists=self.big.exists(), parts=self.part_files(),
                        staged=self.staging_left(plan), bytes=self.big.read_bytes() if self.big.exists() else None)
        self.assertEqual((observed['state'], observed['reason']), ('pending', 'rolled_back'))
        self.assertTrue(observed['exists'])
        self.assertEqual(observed['bytes'], data)
        self.assertEqual(observed['parts'], [])
        self.assertEqual(observed['staged'], [])
        self.assertEqual(self.plan_row(plan)['state'], 'paused')
        self.assertEqual(self.app.store.archives(), [r for r in self.app.store.archives() if r['path'] != BIG])
        control.resume()
        t.join(60)
        self.assertEqual(result['summary']['counts'], {'done': 4})
        self.assertFalse(self.big.exists())
        self.app.restore(self.rows(plan)[BIG]['archive_id'])
        self.assertEqual(self.big.read_bytes(), data)

    def test_cancel_in_the_middle_of_an_archive(self):
        data = self.big.read_bytes()
        plan = self.plan(self.projects)
        control = Control()
        summary = self.run_plan(plan, hooks={'archive_chunk': lambda e: control.cancel() if e.path == BIG else None},
                                control=control)
        self.assertEqual(summary['state'], 'cancelled')
        self.assertEqual(self.big.read_bytes(), data)
        self.assertEqual(self.part_files(), [])
        self.assertEqual(self.staging_left(plan), [])
        self.assertEqual(self.outcome(plan, BIG), ('cancelled', 'cancelled'))

    def test_disk_filling_up_mid_archive_fails_that_entry_only(self):
        plan = self.plan(self.projects)

        def fill(entry):
            if entry.path == BIG:
                self.mac.free = 100 * 1024 * 1024

        def empty_again(entry):
            self.mac.free = 300 * 10 ** 9
        summary = self.run_plan(plan, hooks={'archive_chunk': fill, 'after_claim': empty_again})
        self.assertEqual(self.outcome(plan, BIG), ('failed', 'fail.disk_full'))
        self.assertTrue(self.big.exists())
        self.assertEqual(self.part_files(), [])
        self.assertEqual(summary['counts'], {'done': 3, 'failed': 1})

    def test_file_appended_during_the_archive_is_kept(self):
        plan = self.plan(self.projects)

        def append(entry):
            if entry.path == BIG:
                with open(self.big, 'ab') as f:
                    f.write(b'late write')
        self.run_plan(plan, hooks={'archive_chunk': append})
        self.assertEqual(self.outcome(plan, BIG), ('skipped', 'skip.changed'))
        self.assertTrue(self.big.read_bytes().endswith(b'late write'))
        self.assertEqual(self.part_files(), [])
        self.assertEqual([a for a in self.app.store.archives() if a['path'] == BIG], [])


class BundleRunTests(ExecCase):
    catalog = staticmethod(scratch_catalog)

    def setUp(self):
        super().setUp()
        self.bundle = self.fh.path('~/scratch/b1')
        self.fh.file('~/scratch/b1/a.bin', content=os.urandom(4000), age_days=20)
        self.fh.file('~/scratch/b1/sub/b.txt', content='hello\n' * 100, age_days=20)
        self.fh.symlink('~/scratch/b1/sub/link', '../a.bin', age_days=20)
        self.fh.dir('~/scratch/b1/sub', age_days=20)
        self.fh.dir('~/scratch/b1', age_days=20)
        self.ops = [{'op': 'select_group', 'group': 'test.bundle', 'on': True}, {'op': 'set_filter', 'min_age_days': 14}]

    def test_archive_a_bundle_and_restore_it(self):
        before = tree_snapshot(self.bundle)
        plan = self.plan(self.ops)
        self.assertEqual(plan['totals']['by_action']['archive']['count'], 1)
        summary = self.run_plan(plan)
        self.assertEqual(summary['counts'], {'done': 1})
        self.assertFalse(self.bundle.exists())
        archive_id = self.rows(plan)['~/scratch/b1']['archive_id']
        self.app.restore(archive_id)
        self.assertEqual(tree_snapshot(self.bundle), before)
        self.assertEqual(os.readlink(self.bundle / 'sub' / 'link'), '../a.bin')

    def test_trash_action(self):
        self.ops.append({'op': 'set_action', 'group': 'test.bundle', 'action': 'trash'})
        plan = self.plan(self.ops)
        summary = self.run_plan(plan)
        self.assertEqual(self.mac.trashed, [str(self.bundle)])
        self.assertEqual(self.outcome(plan, '~/scratch/b1'), ('done', 'trash.moved'))
        self.assertEqual(summary['freed'], 0)
        self.assertEqual(summary['moved_to_trash'], plan['totals']['alloc'])
        self.assertEqual([w['key'] for w in summary['warnings']], ['warning.trash_not_freed'])

    def test_trash_needs_the_trash_binary(self):
        self.ops.append({'op': 'set_action', 'group': 'test.bundle', 'action': 'trash'})
        plan = self.plan(self.ops)
        self.mac.trash_ok = False
        with self.assertRaises(StewardError) as cm:
            self.run_plan(plan)
        self.assertEqual(cm.exception.message_key, 'error.trash_unavailable')
        self.assertTrue(self.bundle.exists())

    def test_bundle_with_a_git_folder_appearing_is_protected_at_run_time(self):
        plan = self.plan(self.ops)
        os.mkdir(self.bundle / '.git')
        self.run_plan(plan)
        self.assertEqual(self.outcome(plan, '~/scratch/b1'), ('skipped', 'skip.changed'))   # taint shows in the re-walk
        self.assertTrue((self.bundle / '.git').is_dir())

    def test_bundle_inode_swapped(self):
        plan = self.plan(self.ops)
        os.rename(self.bundle, self.fh.path('~/scratch/b1.old'))
        shutil_copy = self.fh.path('~/scratch/b1')
        shutil_copy.mkdir()
        (shutil_copy / 'new.txt').write_text('different folder, same name')
        self.run_plan(plan)
        self.assertEqual(self.outcome(plan, '~/scratch/b1'), ('skipped', 'skip.changed'))
        self.assertTrue((shutil_copy / 'new.txt').exists())

    def test_bundle_inside_a_git_work_tree_is_protected_by_context(self):
        plan = self.plan(self.ops)
        os.mkdir(self.fh.path('~/scratch/.git'))
        self.run_plan(plan)
        state, reason = self.outcome(plan, '~/scratch/b1')
        self.assertEqual((state, reason), ('skipped', 'skip.rules_changed'))
        self.assertTrue(self.bundle.exists())


class CrashRecoveryTests(ExecCase):
    catalog = staticmethod(scratch_catalog)

    def crash(self, plan, hook_name, when, **kw):
        def hook(entry):
            if when(entry):
                raise SystemExit('simulated crash')
        with self.assertRaises(SystemExit):
            self.run_plan(plan, hooks={hook_name: hook}, **kw)

    def recover(self):
        with self.app.lock:
            return self.executor().recover()

    def test_crash_after_staging_a_file_finishes_the_delete(self):
        plan = self.plan(LOGS_ONLY)
        self.crash(plan, 'before_unlink', lambda e: e.path == LOG)
        seq = self.seq_of(plan, LOG)
        staged = self.fh.data_dir / 'staging' / plan['id'] / str(seq)
        self.assertTrue(staged.exists())
        self.assertFalse(self.fh.path(LOG).exists())
        self.assertEqual(self.outcome(plan, LOG)[0], 'in_progress')
        self.assertEqual(self.plan_row(plan)['state'], 'running')
        report = self.recover()
        self.assertEqual([(e['seq'], e['state']) for e in report['entries']], [(seq, 'done')])
        self.assertFalse(staged.exists())
        self.assertEqual(self.outcome(plan, LOG), ('done', ''))
        row = self.plan_row(plan)
        self.assertEqual((row['state'], row['error']), ('paused', 'interrupted'))
        self.assertEqual(self.staging_left(plan), [])

    def test_a_staged_file_that_changed_goes_back_when_recovering(self):
        plan = self.plan(LOGS_ONLY)
        self.crash(plan, 'before_unlink', lambda e: True)
        seq = self.seq_of(plan, LOG)
        staged = self.fh.data_dir / 'staging' / plan['id'] / str(seq)
        with open(staged, 'ab') as f:
            f.write(b'someone wrote to it')
        self.recover()
        self.assertEqual(self.outcome(plan, LOG), ('skipped', 'skip.changed'))
        self.assertTrue(self.fh.path(LOG).read_bytes().endswith(b'someone wrote to it'))

    def test_recovery_with_the_name_taken_needs_attention(self):
        plan = self.plan(LOGS_ONLY)
        self.crash(plan, 'before_unlink', lambda e: True)
        seq = self.seq_of(plan, LOG)
        staged = self.fh.data_dir / 'staging' / plan['id'] / str(seq)
        with open(staged, 'ab') as f:
            f.write(b'x')
        self.fh.path(LOG).write_text('new file under the old name')
        self.recover()
        state, reason = self.outcome(plan, LOG)
        self.assertEqual((state, reason), ('needs_attention', f'attention.staged_left:{staged}'))
        self.assertTrue(staged.exists())
        self.assertEqual(self.fh.path(LOG).read_text(), 'new file under the old name')

    def test_crash_before_staging_goes_back_to_pending_and_can_be_run_again(self):
        plan = self.plan([{'op': 'select_group', 'group': g, 'on': True} for g in ('claude.logs', 'codex.logs', 'codex.cache')]
                         + [{'op': 'set_filter', 'min_age_days': 0}])
        self.assertEqual(plan['entries']['total'], 3)
        self.crash(plan, 'after_guard', lambda e: e.seq == 2)
        report = self.recover()
        self.assertEqual([(e['seq'], e['state'], e['reason']) for e in report['entries']], [(2, 'pending', 'interrupted')])
        self.assertEqual(self.app.entries(plan['id'], state='done')['total'], 1)
        summary = self.run_plan(plan)    # the paused plan resumes where it stopped
        self.assertEqual(summary['counts'], {'done': 3})

    def test_crash_after_the_unlink_is_recognised(self):
        plan = self.plan(LOGS_ONLY)
        seq = self.seq_of(plan, LOG)
        self.assertTrue(self.app.store.claim_entry(plan['id'], seq))      # a run that claimed it ...
        self.app.store.set_plan_state(plan['id'], PlanState.RUNNING)
        os.unlink(self.fh.path(LOG))                                       # ... and died right after the unlink
        self.recover()
        self.assertEqual(self.outcome(plan, LOG), ('done', 'recovered.gone'))

    def test_crash_in_a_run_that_had_not_claimed_anything_still_pauses_the_plan(self):
        plan = self.plan(LOGS_ONLY)
        self.app.store.set_plan_state(plan['id'], PlanState.RUNNING)
        report = self.recover()
        self.assertEqual((report['entries'], report['plans']), ([], [plan['id']]))
        self.assertEqual(self.plan_row(plan)['state'], 'paused')

    def archive_crash_setup(self):
        self.fh.file('~/scratch-files/f.bin', content=os.urandom(200_000), age_days=20)
        patcher = mock.patch.object(actions, 'CHUNK', 64 * 1024)
        patcher.start()
        self.addCleanup(patcher.stop)
        return self.plan([{'op': 'select_group', 'group': 'test.file', 'on': True},
                          {'op': 'set_filter', 'min_age_days': 14}])

    def test_crash_after_a_verified_archive_finishes_the_delete(self):
        plan = self.archive_crash_setup()
        path = self.fh.path('~/scratch-files/f.bin')
        data = path.read_bytes()
        self.crash(plan, 'before_unlink', lambda e: True)
        self.assertFalse(path.exists())
        report = self.recover()
        (entry,) = report['entries']
        self.assertEqual(entry['state'], 'done')
        row = self.rows(plan)['~/scratch-files/f.bin']
        self.assertEqual(row['reason'], 'archive.stored')
        self.assertTrue(row['archive_id'])
        self.assertEqual(self.staging_left(plan), [])
        self.app.restore(row['archive_id'])
        self.assertEqual(path.read_bytes(), data)

    def test_crash_while_streaming_a_bundle_removes_the_part_file_and_puts_it_back(self):
        self.fh.file('~/scratch/b1/a.bin', content=os.urandom(100_000), age_days=20)
        self.fh.dir('~/scratch/b1', age_days=20)
        patcher = mock.patch.object(actions, 'CHUNK', 8 * 1024)
        patcher.start()
        self.addCleanup(patcher.stop)
        plan = self.plan([{'op': 'select_group', 'group': 'test.bundle', 'on': True},
                          {'op': 'set_filter', 'min_age_days': 14}])
        before = tree_snapshot(self.fh.path('~/scratch/b1'))
        self.crash(plan, 'archive_chunk', lambda e: True)
        archives = self.fh.data_dir / 'archives'
        self.assertTrue(any(n.endswith('.part') for n in os.listdir(archives)))
        self.assertFalse(self.fh.path('~/scratch/b1').exists())      # it sits in staging, being archived
        report = self.recover()
        self.assertEqual([(e['state'], e['reason']) for e in report['entries']], [('pending', 'interrupted')])
        self.assertEqual(os.listdir(archives), [])
        self.assertEqual(tree_snapshot(self.fh.path('~/scratch/b1')), before)

    def test_crash_after_a_bundle_archive_finishes_the_removal(self):
        self.fh.file('~/scratch/b1/a.bin', content=os.urandom(50_000), age_days=20)
        self.fh.dir('~/scratch/b1', age_days=20)
        plan = self.plan([{'op': 'select_group', 'group': 'test.bundle', 'on': True},
                          {'op': 'set_filter', 'min_age_days': 14}])
        self.crash(plan, 'before_unlink', lambda e: True)
        self.assertTrue(any(n.endswith('.tar.gz') for n in os.listdir(self.fh.data_dir / 'archives')))
        report = self.recover()
        self.assertEqual([e['state'] for e in report['entries']], ['done'])
        self.assertFalse(self.fh.path('~/scratch/b1').exists())
        self.assertEqual(self.staging_left(plan), [])
        archive_id = self.rows(plan)['~/scratch/b1']['archive_id']
        self.app.restore(archive_id)
        self.assertEqual((self.fh.path('~/scratch/b1/a.bin')).stat().st_size, 50_000)

    def test_recovery_does_not_trust_a_damaged_archive(self):
        plan = self.archive_crash_setup()
        path = self.fh.path('~/scratch-files/f.bin')
        data = path.read_bytes()
        self.crash(plan, 'before_unlink', lambda e: True)
        (row,) = self.app.store.archives()
        gz = self.fh.data_dir / 'archives' / f'{row["id"]}.gz'
        blob = bytearray(gz.read_bytes())
        blob[100] ^= 0xFF
        gz.write_bytes(bytes(blob))
        self.recover()
        self.assertEqual(path.read_bytes(), data)     # the only good copy goes back instead of being deleted
        self.assertEqual(self.outcome(plan, '~/scratch-files/f.bin'), ('pending', 'interrupted'))
        self.assertEqual(self.app.store.archive(row['id'])['state'], 'discarded')

    def test_archive_complete_but_file_never_staged_goes_back_to_pending(self):
        plan = self.archive_crash_setup()
        path = self.fh.path('~/scratch-files/f.bin')
        self.crash(plan, 'before_unlink', lambda e: True)
        # undo the staging by hand: the crash is now "after the archive, before staging"
        seq = self.seq_of(plan, '~/scratch-files/f.bin')
        os.rename(self.fh.data_dir / 'staging' / plan['id'] / str(seq), path)
        self.recover()
        self.assertEqual(self.outcome(plan, '~/scratch-files/f.bin'), ('pending', 'interrupted'))
        self.assertTrue(path.exists())
        self.assertEqual([a['state'] for a in self.app.store.archives()], ['discarded'])

    def test_recovery_removes_files_of_an_archive_that_never_reached_the_database(self):
        plan = self.archive_crash_setup()
        self.crash(plan, 'archive_chunk', lambda e: True)
        adir = self.fh.data_dir / 'archives'
        orphan = adir / f'{plan["id"][:8]}-1-abcdef.gz'
        orphan.write_bytes(b'unregistered')
        self.recover()
        self.assertEqual(os.listdir(adir), [])


class PerformanceTests(unittest.TestCase):
    def test_two_thousand_small_files(self):
        class Ticking(Clock):
            def __call__(self):
                self._now += 0.0005    # time passes, so the lsof snapshot is refreshed now and then
                return self._now

        with FakeHome(Ticking()) as fh:
            for i in range(2000):
                fh.file(f'~/.codex/log/f{i:04d}.log', size=64, age_days=10)
            fh.file('~/.codex/config.toml', size=10)
            mac = make_mac()
            app = make_app(fh, mac=mac)
            try:
                app.scan()
                plan = make_plan(app, [{'op': 'select_group', 'group': 'codex.logs', 'on': True},
                                       {'op': 'set_filter', 'min_age_days': 0}])
                self.assertEqual(plan['entries']['total'], 2000)
                started = time.monotonic()
                summary = app.run_plan(plan['id'], plan['revision'], plan['entries_digest'])
                elapsed = time.monotonic() - started
            finally:
                app.close()
            self.assertEqual(summary['counts'], {'done': 2000})
            self.assertEqual(os.listdir(fh.path('~/.codex/log')), [])
            self.assertLess(elapsed, 10.0)
            self.assertLessEqual([c[0] for c in mac.calls].count('open_files'), 12)


if __name__ == '__main__':
    unittest.main()
