from __future__ import annotations

import dataclasses
import sqlite3
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from disk_steward import queries, store as store_mod
from disk_steward.model import (Action, AgeBasis, Domain, Entry, EntryState, ErrorCode, Evidence, Group, Item, Kind,
                                Outcome, Plan, PlanState, Spec, GroupSel, StewardError, Tier, Unit)
from disk_steward.paths import item_key
from disk_steward.store import Store
from support import Clock, render_error

DEV = 16777233


def make_item(scan_id, n, **kw):
    path = kw.pop('path', f'~/Library/Caches/app/{n}')
    base = dict(scan_id=scan_id, key=item_key(path), path=path, rule_id='cache.app', group_key='cache.app:~/Library',
                tier=Tier.JUNK, domain=Domain.APP_LIBRARY, kind=Kind.CACHE, unit=Unit.FILE, dev=DEV,
                ino=2 ** 62 + n, size=100 * n, alloc=4096 * n, reclaim=4096 * n, files=1,
                mtime_ns=1_790_000_000_123_456_789, birth_ns=1_789_000_000_000_000_000,
                newest_ns=1_790_000_000_123_456_789, last_used_ns=1_790_500_000_000_000_001, nlink=1, uid=501,
                flags=0, taint='', evidence=(Evidence('evidence.a', {'n': n, 'xs': ['a', 'b'], 'u': '中文'}),
                                              Evidence('evidence.b')))
    base.update(kw)
    return Item(**base)


def make_group(scan_id, key='cache.app:~/Library', **kw):
    base = dict(scan_id=scan_id, key=key, rule_id='cache.app', provider='apple', domain=Domain.APP_LIBRARY,
                tier=Tier.JUNK, kind=Kind.CACHE, unit=Unit.FILE, label_args={'path': '~/Library', 'n': 2},
                items=3, alloc=12288, reclaim=8192, newest_ns=1_790_000_000_000_000_000,
                oldest_ns=1_700_000_000_000_000_000, actions=(Action.DELETE, Action.TRASH),
                default_action=Action.TRASH, min_age_floor_days=1.5, default_min_age_days=30.0,
                age_basis=AgeBasis.LAST_USED, hint='run brew cleanup',
                evidence=(Evidence('evidence.g', {'k': 1}),))
    base.update(kw)
    return Group(**base)


def make_plan(plan_id='p1', scan_id=1, **kw):
    base = dict(id=plan_id, created_at=1_790_000_000.25, expires_at=1_790_086_400.5, scan_id=scan_id,
                catalog_hash='c' * 64, facts_hash='f' * 64, settings_hash='s' * 64,
                spec={'scan_id': scan_id, 'version': 3, 'groups': {'g': {'min_age_days': None, 'action': 'trash'}}},
                revision=1, entries_digest='d' * 64, state=PlanState.DRAFT, totals={'count': 2, 'alloc': 9},
                error='')
    base.update(kw)
    return Plan(**base)


def make_entry(plan_id, seq, **kw):
    base = dict(plan_id=plan_id, seq=seq, item_key=item_key(f'~/x/{seq}'), path=f'~/x/{seq}', unit=Unit.BUNDLE,
                action=Action.ARCHIVE, rule_id='r', group_key='g', threshold_days=14.0, alloc=4096 * seq,
                reclaim=4096 * seq, fp={'dev': DEV, 'ino': 2 ** 62 + seq, 'files': 3},
                state=EntryState.PENDING, reason='', freed=0, archive_id=None)
    base.update(kw)
    return Entry(**base)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'data' / 'steward.db'
        self.clock = Clock(1_790_000_000.0)
        self.store = Store(self.path, clock=self.clock)
        self.addCleanup(self.store.close)

    def new_scan(self, finish=None):
        sid = self.store.begin_scan('cat', 'facts', 'settings', [Domain.AGENT, 'app-library'])
        if finish:
            self.store.finish_scan(sid, finish, 100, 50, 25)
        return sid

    def assertStewardError(self, code, fn, *args, **kwargs):
        with self.assertRaises(StewardError) as cm:
            fn(*args, **kwargs)
        self.assertEqual(cm.exception.code, code)
        render_error(cm.exception)
        return cm.exception


class SchemaTests(StoreCase):
    def raw(self):
        conn = sqlite3.connect(str(self.path))
        self.addCleanup(conn.close)
        return conn

    def test_tables_and_version(self):
        names = {r[0] for r in self.raw().execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertGreaterEqual(names, {'meta', 'scans', 'scan_roots', 'groups', 'items', 'group_history',
                                        'scan_errors', 'specs', 'plans', 'plan_entries', 'archives', 'advice',
                                        'dirs', 'evidence', 'spec_include_fp'})
        self.assertEqual(self.raw().execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0],
                         str(len(store_mod.MIGRATIONS)))

    def test_item_indexes(self):
        idx = {r[0]: [c[2] for c in self.raw().execute(f'PRAGMA index_info({r[0]})')]
               for r in self.raw().execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'items' "
                                           "AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual(sorted(idx.values()), [['scan_id', 'alloc'], ['scan_id', 'group_id', 'alloc'],
                                                ['scan_id', 'key']])

    def plans(self, fn):
        """EXPLAIN QUERY PLAN text of every SELECT that `fn` makes through the store's connection."""
        seen = []
        conn = self.store._conn()
        conn.set_trace_callback(lambda sql: seen.append(sql) if sql.lstrip().startswith('SELECT') else None)
        try:
            fn()
        finally:
            conn.set_trace_callback(None)
        raw = self.raw()
        return ' | '.join(r[3] for sql in seen for r in raw.execute('EXPLAIN QUERY PLAN ' + sql))

    def test_item_queries_use_their_indexes(self):
        # Every index on items serves one of these, and no query walks the table when an index can serve it.
        sid = self.new_scan()
        self.store.add_groups(sid, [make_group(sid, key='g')])
        self.store.add_items(sid, [make_item(sid, n, group_key='g') for n in range(5)])
        key = make_item(sid, 1).key
        for what, call, index in (
                ('keys', lambda: self.store.items(sid, keys=[key]), 'items_key'),
                ('one item', lambda: self.store.item(sid, key), 'items_key'),
                ('describe_rows', lambda: queries.describe_rows(self.store, sid, [key], 5), 'items_key'),
                ('a group page', lambda: self.store.items(sid, group_key='g'), 'items_group'),
                ('count in a group', lambda: self.store.count_items(sid, group_key='g'), 'items_group'),
                ('evidence_rows', lambda: queries.evidence_rows(self.store, sid, ['g']), 'items_group'),
                ('largest overall', lambda: self.store.items(sid, limit=10), 'items_alloc')):
            with self.subTest(what):
                self.assertIn(f'USING INDEX {index}', self.plans(call).replace('COVERING ', ''))
        # these two read every row; a walk of the table is the cheaper plan
        for what, call in (('item_rows', lambda: list(queries.item_rows(self.store, sid))),
                           ('plan_item_rows', lambda: list(queries.plan_item_rows(self.store, sid)))):
            with self.subTest(what):
                self.assertNotIn('USING', self.plans(call).split('SEARCH g')[0])

    def test_connection_pragmas_and_file_mode(self):
        c = self.store._conn()
        self.assertEqual(c.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        self.assertEqual(c.execute('PRAGMA busy_timeout').fetchone()[0], 5000)
        self.assertEqual(c.execute('PRAGMA foreign_keys').fetchone()[0], 1)
        self.assertEqual(c.execute('PRAGMA synchronous').fetchone()[0], 1)  # NORMAL; archive rows use _durable()
        self.assertEqual(c.execute('PRAGMA fullfsync').fetchone()[0], 0)
        self.assertEqual(c.execute('PRAGMA auto_vacuum').fetchone()[0], 2)  # incremental
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_pragmas_hold_on_other_threads(self):
        seen = []
        t = threading.Thread(target=lambda: seen.append(
            (self.store._conn().execute('PRAGMA foreign_keys').fetchone()[0],
             self.store._conn().execute('PRAGMA busy_timeout').fetchone()[0])))
        t.start()
        t.join()
        self.assertEqual(seen, [(1, 5000)])
        seen = []
        t = threading.Thread(target=lambda: seen.append(self.store._conn().execute('PRAGMA synchronous').fetchone()[0]))
        t.start()
        t.join()
        self.assertEqual(seen, [1])

    def archive_row(self):
        if not self.store.plans_in_state('draft'):
            self.store.save_plan(make_plan(scan_id=self.new_scan()), [])
        return dict(id='a1', plan_id='p1', seq=1, path='~/x/1', fmt='gz', orig_size=10, orig_alloc=4096,
                    stored_bytes=5, sha_orig='o' * 64, sha_stored='s' * 64, manifest={}, state='stored')

    def test_archive_rows_are_written_with_a_full_drive_flush(self):
        conn = self.store._conn()
        statements = []
        conn.set_trace_callback(statements.append)
        self.addCleanup(conn.set_trace_callback, None)
        self.store.add_archive(dict(self.archive_row()))
        self.store.set_archive_state(self.archive_row()['id'], 'restored')
        writes = [i for i, s in enumerate(statements) if s.startswith(('INSERT INTO archives', 'UPDATE archives'))]
        self.assertEqual(len(writes), 2)
        for i in writes:
            before = statements[:i]
            self.assertEqual(before[-2:], ['PRAGMA synchronous = FULL', 'PRAGMA fullfsync = ON'])
        self.assertEqual(statements[-2:], ['PRAGMA fullfsync = OFF', 'PRAGMA synchronous = NORMAL'])
        self.assertEqual(conn.execute('PRAGMA fullfsync').fetchone()[0], 0)

    def test_a_durable_write_inside_a_transaction_fails_loudly(self):
        row = self.archive_row()
        with self.assertRaises(RuntimeError):
            with self.store._tx():
                self.store.add_archive(row)

    def test_reopen_keeps_data_and_does_not_remigrate(self):
        sid = self.new_scan('done')
        self.store.close()
        again = Store(self.path)
        self.addCleanup(again.close)
        self.assertEqual(again.scan(sid)['status'], 'done')

    def test_newer_schema_is_refused(self):
        self.raw().execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'").connection.commit()
        with self.assertRaises(RuntimeError):
            Store(self.path)

    def test_migrations_apply_in_order(self):
        extra = ('CREATE TABLE extra_for_test (x INTEGER)',)
        original = store_mod.MIGRATIONS
        store_mod.MIGRATIONS = original + (extra,)
        try:
            again = Store(self.path)
            self.addCleanup(again.close)
        finally:
            store_mod.MIGRATIONS = original
        c = self.raw()
        self.assertEqual(c.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0],
                         str(len(original) + 1))
        self.assertTrue(c.execute("SELECT 1 FROM sqlite_master WHERE name = 'extra_for_test'").fetchone())

    def test_foreign_keys_are_enforced(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add_items(999, [make_item(999, 1)])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save_plan(make_plan(scan_id=999), [])

    def test_closed_store_refuses(self):
        self.store.close()
        with self.assertRaises(RuntimeError):
            self.store.latest_scan()

    def test_dead_thread_connections_are_closed(self):
        for _ in range(3):
            t = threading.Thread(target=self.store.latest_scan)
            t.start()
            t.join()
        t = threading.Thread(target=self.store.latest_scan)
        t.start()
        t.join()
        self.assertLessEqual(len(self.store._conns), 2)


class RoundTripTests(StoreCase):
    def test_scan_lifecycle(self):
        sid = self.new_scan()
        self.assertIsNone(self.store.latest_scan())
        running = self.store.scan(sid)
        self.assertEqual((running['status'], running['finished_at'], running['domains']),
                         ('running', None, ['agent', 'app-library']))
        self.assertEqual(running['started_at'], 1_790_000_000.0)
        self.clock.advance(5)
        self.store.finish_scan(sid, 'done', 1000, 400, 300)
        latest = self.store.latest_scan()
        self.assertEqual((latest['id'], latest['status'], latest['finished_at']), (sid, 'done', 1_790_000_005.0))
        self.assertEqual((latest['vol_total'], latest['vol_free'], latest['attributed']), (1000, 400, 300))
        self.assertEqual((latest['catalog_hash'], latest['facts_hash'], latest['settings_hash']),
                         ('cat', 'facts', 'settings'))
        self.assertEqual(self.store.latest_scan('running'), None)

    def test_finish_scan_misuse(self):
        sid = self.new_scan('done')
        with self.assertRaises(RuntimeError):
            self.store.finish_scan(sid, 'done', 1, 1, 1)
        with self.assertRaises(ValueError):
            self.store.finish_scan(self.new_scan(), 'running', 1, 1, 1)
        self.assertStewardError(ErrorCode.NOT_FOUND, self.store.finish_scan, 999, 'done', 1, 1, 1)
        self.assertStewardError(ErrorCode.NOT_FOUND, self.store.scan, 999)

    def test_failed_scan_keeps_error_text(self):
        sid = self.new_scan()
        self.store.finish_scan(sid, 'failed', None, None, None, error='disk gone')
        self.assertEqual(self.store.scan(sid)['error'], 'disk gone')
        self.assertIsNone(self.store.scan(sid)['vol_total'])

    def test_item_round_trip_exact(self):
        sid = self.new_scan()
        items = [make_item(sid, 1),
                 make_item(sid, 2, unit=Unit.BUNDLE, last_used_ns=None, taint='sqlite', tier=Tier.PROTECTED,
                           domain=Domain.USER_FILES, kind=Kind.UNKNOWN, evidence=(), flags=0x20000, nlink=3),
                 make_item(sid, 3, path='~/Library/Caches/中文 é/日本', evidence=(Evidence('evidence.\udcff'),))]
        self.store.add_items(sid, items)
        for original in items:
            got = self.store.item(sid, original.key)
            self.assertEqual(got, original)
            self.assertIsInstance(got.tier, Tier)
            self.assertIsInstance(got.unit, Unit)
            self.assertIsInstance(got.evidence, tuple)
            self.assertTrue(all(isinstance(e, Evidence) for e in got.evidence))
            self.assertEqual(got.fingerprint(), original.fingerprint())
        self.assertEqual(self.store.item(sid, items[0].key).ino, 2 ** 62 + 1)

    def test_group_round_trip_exact(self):
        sid = self.new_scan()
        groups = [make_group(sid),
                  make_group(sid, key='g2', default_action=None, default_min_age_days=None, hint=None, actions=(),
                             evidence=(), label_args={}, age_basis=AgeBasis.CHANGED, tier=Tier.PROTECTED,
                             domain=Domain.DEV_SHARED, unit=Unit.BUNDLE, kind=Kind.LARGE_FILE,
                             min_age_floor_days=0.0)]
        self.store.add_groups(sid, groups)
        for original in groups:
            got = self.store.group(sid, original.key)
            self.assertEqual(got, original)
            self.assertIsInstance(got.actions, tuple)
            self.assertTrue(all(isinstance(a, Action) for a in got.actions))
        self.assertEqual(self.store.groups(sid), sorted(groups, key=lambda g: (-g.alloc, g.key)))
        self.assertStewardError(ErrorCode.NOT_FOUND, self.store.group, sid, 'nope')

    def test_group_filters(self):
        sid = self.new_scan()
        self.store.add_groups(sid, [
            make_group(sid, key='a', alloc=30), make_group(sid, key='b', alloc=20, domain=Domain.AGENT),
            make_group(sid, key='c', alloc=10, tier=Tier.REVIEW)])
        self.assertEqual([g.key for g in self.store.groups(sid)], ['a', 'b', 'c'])
        self.assertEqual([g.key for g in self.store.groups(sid, domain=Domain.AGENT)], ['b'])
        self.assertEqual([g.key for g in self.store.groups(sid, domain='app-library')], ['a', 'c'])
        self.assertEqual([g.key for g in self.store.groups(sid, tier=Tier.REVIEW)], ['c'])
        self.assertEqual(self.store.groups(sid, domain=Domain.AGENT, tier=Tier.REVIEW), [])

    def test_plan_and_entries_round_trip_exact(self):
        sid = self.new_scan()
        plan = make_plan(scan_id=sid)
        entries = [make_entry('p1', 1), make_entry('p1', 2, unit=Unit.FILE, action=Action.DELETE,
                                                    state=EntryState.DONE, reason='ok', freed=4096,
                                                    archive_id='arc1', fp={'dev': DEV, 'ino': 5, 'size': 9,
                                                                           'mtime_ns': 1_790_000_000_123_456_789,
                                                                           'birth_ns': 1, 'nlink': 1})]
        self.store.save_plan(plan, entries)
        self.assertEqual(self.store.plan('p1'), plan)
        self.assertEqual(self.store.entries('p1'), entries)
        self.assertIsInstance(self.store.plan('p1').state, PlanState)
        self.assertIsInstance(self.store.entries('p1')[1].action, Action)
        self.assertStewardError(ErrorCode.NOT_FOUND, self.store.plan, 'nope')

    def test_save_plan_is_atomic_and_checks_ownership(self):
        sid = self.new_scan()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save_plan(make_plan(scan_id=sid), [make_entry('p1', 1), make_entry('p1', 1)])
        self.assertStewardError(ErrorCode.NOT_FOUND, self.store.plan, 'p1')
        with self.assertRaises(ValueError):
            self.store.save_plan(make_plan(scan_id=sid), [make_entry('other', 1)])
        self.assertStewardError(ErrorCode.NOT_FOUND, self.store.plan, 'p1')

    def test_entry_listing(self):
        sid = self.new_scan()
        self.store.save_plan(make_plan(scan_id=sid), [make_entry('p1', n) for n in range(1, 8)])
        self.store.claim_entry('p1', 2)
        self.store.finish_entry('p1', 2, Outcome(EntryState.DONE, 'done', freed=10, archive_id='a'))
        self.store.finish_entry('p1', 3, Outcome(EntryState.SKIPPED, 'skip.changed'))
        self.assertEqual([e.seq for e in self.store.entries('p1')], list(range(1, 8)))
        self.assertEqual([e.seq for e in self.store.entries('p1', offset=2, limit=3)], [3, 4, 5])
        self.assertEqual([e.seq for e in self.store.entries('p1', state=EntryState.DONE)], [2])
        self.assertEqual([e.seq for e in self.store.entries('p1', state='pending', limit=2)], [1, 4])
        self.assertEqual(self.store.count_entries('p1'), 7)
        self.assertEqual(self.store.count_entries('p1', EntryState.PENDING), 5)
        done = self.store.entries('p1', state=EntryState.DONE)[0]
        self.assertEqual((done.reason, done.freed, done.archive_id), ('done', 10, 'a'))
        self.assertStewardError(ErrorCode.NOT_FOUND, self.store.finish_entry, 'p1', 99, Outcome(EntryState.DONE))

    def test_plan_state(self):
        sid = self.new_scan()
        self.store.save_plan(make_plan(scan_id=sid), [])
        self.store.set_plan_state('p1', PlanState.PAUSED, 'interrupted')
        p = self.store.plan('p1')
        self.assertEqual((p.state, p.error), (PlanState.PAUSED, 'interrupted'))
        self.store.set_plan_state('p1', 'running')
        self.assertEqual(self.store.plan('p1').state, PlanState.RUNNING)
        self.assertEqual(self.store.plan('p1').error, '')
        self.assertStewardError(ErrorCode.NOT_FOUND, self.store.set_plan_state, 'nope', PlanState.DONE)

    def test_spec_round_trip(self):
        sid = self.new_scan()
        spec = Spec(scan_id=sid, version=4, filter_min_age_days=7.5,
                    groups={'g': GroupSel(30.0, Action.ARCHIVE), 'h': GroupSel()},
                    include={'k1': Action.TRASH, 'k2': None}, exclude=frozenset({'x', 'y'}))
        self.assertIsNone(self.store.load_spec('default'))
        self.store.save_spec('default', spec)
        self.assertEqual(self.store.load_spec('default'), spec)
        newer = dataclasses.replace(spec, version=5)
        self.store.save_spec('default', newer)
        self.assertEqual(self.store.load_spec('default'), newer)
        self.assertIsNone(self.store.load_spec('other'))

    def test_archives(self):
        sid = self.new_scan()
        self.store.save_plan(make_plan(scan_id=sid), [])
        row = dict(id='a1', plan_id='p1', seq=1, path='~/x/1', fmt='tar.gz', orig_size=10, orig_alloc=4096,
                   stored_bytes=5, sha_orig='o' * 64, sha_stored='s' * 64,
                   manifest={'files': [{'p': 'a/b', 'mode': 420}], 'u': '中文'}, state='verified')
        self.store.add_archive(row)
        self.store.add_archive(dict(row, id='a0', created_at=1.0))
        got = self.store.archive('a1')
        self.assertEqual({k: v for k, v in got.items() if k != 'created_at'}, row)
        self.assertEqual(got['created_at'], 1_790_000_000.0)
        self.assertEqual([a['id'] for a in self.store.archives()], ['a0', 'a1'])
        self.store.set_archive_state('a1', 'purged')
        self.assertEqual(self.store.archive('a1')['state'], 'purged')
        self.assertStewardError(ErrorCode.NOT_FOUND, self.store.archive, 'nope')
        self.assertStewardError(ErrorCode.NOT_FOUND, self.store.set_archive_state, 'nope', 'x')
        with self.assertRaises(ValueError):
            self.store.add_archive(dict(row, id='a2', extra=1))
        with self.assertRaises(ValueError):
            self.store.add_archive({k: v for k, v in row.items() if k != 'sha_orig'})

    def test_advice(self):
        sid = self.new_scan()
        self.assertIsNone(self.store.latest_advice(sid))
        a = self.store.add_advice(dict(scan_id=sid, provider='codex', cli_version='1.0', payload_digest='d1',
                                       result={'suggestions': [1, 2]}))
        b = self.store.add_advice(dict(scan_id=sid, provider='claude', cli_version='2.0', payload_digest='d2',
                                       result={'suggestions': []}, created_at=5.0))
        self.assertGreater(b, a)
        latest = self.store.latest_advice(sid)
        self.assertEqual((latest['provider'], latest['result'], latest['created_at']), ('claude', {'suggestions': []}, 5.0))
        with self.assertRaises(ValueError):
            self.store.add_advice(dict(scan_id=sid, provider='x'))

    def test_scan_roots_and_errors(self):
        sid = self.new_scan()
        self.store.add_scan_root(sid, '~/Library/Caches', Domain.APP_LIBRARY, 'ok', files=3, alloc=12)
        self.store.add_scan_root(sid, '~/.codex', 'agent', 'missing')
        self.store.add_scan_error(sid, '~/x', 'EACCES')
        self.assertEqual([(r['path'], r['domain'], r['status'], r['files'], r['alloc'])
                          for r in self.store.scan_roots(sid)],
                         [('~/Library/Caches', 'app-library', 'ok', 3, 12), ('~/.codex', 'agent', 'missing', 0, 0)])
        self.assertEqual([(e['path'], e['error']) for e in self.store.scan_errors(sid)], [('~/x', 'EACCES')])

    def test_items_added_for_another_scan_are_refused(self):
        sid = self.new_scan()
        with self.assertRaises(ValueError):
            self.store.add_items(sid, [make_item(sid + 1, 1)])
        with self.assertRaises(ValueError):
            self.store.add_groups(sid, [make_group(sid + 1)])
        self.assertEqual(self.store.count_items(sid), 0)

    def test_duplicate_item_is_refused_and_batch_rolls_back(self):
        sid = self.new_scan()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add_items(sid, [make_item(sid, 1), make_item(sid, 2), make_item(sid, 1)])
        self.assertEqual(self.store.count_items(sid), 0)


class CompactSchemaTests(StoreCase):
    """Items are stored by directory id, group id and evidence id; none of that may show through the API."""

    def rows(self, sql, *args):
        return self.store._conn().execute(sql, args).fetchall()

    def test_round_trip_with_a_rule_override_inside_a_group_and_no_last_used(self):
        sid = self.new_scan()
        self.store.add_groups(sid, [make_group(sid, key='codex.sessions:~/.codex/sessions', rule_id='codex.sessions')])
        items = [
            make_item(sid, 1, group_key='codex.sessions:~/.codex/sessions', rule_id='codex.sessions', last_used_ns=None),
            make_item(sid, 2, group_key='codex.sessions:~/.codex/sessions', rule_id='codex.archived-sessions',
                      unit=Unit.BUNDLE, kind=Kind.LOGS, domain=Domain.AGENT, tier=Tier.REVIEW),
            make_item(sid, 3, group_key='codex.sessions:~/.codex/sessions', rule_id='codex.sessions',
                      tier=Tier.PROTECTED, taint='sqlite', evidence=())]
        self.store.add_items(sid, items)
        for original in items:
            self.assertEqual(self.store.item(sid, original.key), original)
        self.assertEqual(sorted(self.store.iter_items(sid), key=lambda i: i.key), sorted(items, key=lambda i: i.key))
        self.assertIsNone(self.store.item(sid, items[0].key).last_used_ns)
        # only what differs from the group is stored on the item row
        stored = {r['key']: tuple(r) for r in self.rows('SELECT key, rule_id, tier, domain, kind, unit FROM items')}
        self.assertEqual(stored[items[0].key][1:], (None,) * 5)
        self.assertEqual(stored[items[1].key][1:], ('codex.archived-sessions', 'review', 'agent', 'logs', 'bundle'))
        self.assertEqual(stored[items[2].key][1:], (None, 'protected', None, None, None))

    def test_evidence_is_stored_once_and_a_path_is_split_into_directory_and_name(self):
        sid = self.new_scan()
        quit_app = (Evidence('evidence.requires_quit', {'bundle_id': 'com.x', 'app': 'X'}),)
        items = [make_item(sid, n, path=f'~/Library/Application Support/X/Cache/f{n}', evidence=quit_app)
                 for n in range(50)]
        items.append(make_item(sid, 99, path='~/Library/Application Support/X/GPUCache/g', evidence=()))
        self.store.add_items(sid, items)
        self.assertEqual(self.rows('SELECT COUNT(*) FROM evidence')[0][0], 1)
        self.assertEqual(self.rows('SELECT COUNT(*) FROM items WHERE evidence_id IS NULL')[0][0], 1)
        self.assertEqual(self.rows('SELECT path FROM dirs ORDER BY path')[0][0],
                         '~/Library/Application Support/X/Cache')
        self.assertEqual(self.rows('SELECT COUNT(*) FROM dirs')[0][0], 2)
        self.assertEqual(self.rows('SELECT name FROM items WHERE key = ?', items[3].key)[0][0], 'f3')
        self.assertEqual(self.store.items(sid, query='gpucache/G', limit=5), [items[-1]])
        self.assertEqual(self.store.item(sid, items[7].key).evidence, quit_app)

    def test_odd_paths_survive(self):
        sid = self.new_scan()
        items = [make_item(sid, 1, path='~'), make_item(sid, 2, path='/abs/name'), make_item(sid, 3, path='/top'),
                 make_item(sid, 4, path='~/trailing/'), make_item(sid, 5, path='~/a//b')]
        self.store.add_items(sid, items)
        self.assertEqual({i.path for i in self.store.iter_items(sid)}, {i.path for i in items})
        self.assertEqual(self.store.items(sid, order='path'), sorted(items, key=lambda i: (i.path, i.key)))
        self.assertEqual(self.store.items(sid, query='trailing/'), [items[3]])

    def test_a_group_started_by_its_items_keeps_their_values_when_the_group_row_arrives(self):
        sid = self.new_scan()
        first = make_item(sid, 1, group_key='ga', rule_id='r.items', tier=Tier.REVIEW, kind=Kind.LOGS)
        later = make_item(sid, 2, group_key='ga', rule_id='r.group', tier=Tier.JUNK, kind=Kind.CACHE)
        self.store.add_items(sid, [first])
        self.assertEqual(self.store.groups(sid), [])                     # a group without its row does not exist
        self.assertStewardError(ErrorCode.NOT_FOUND, self.store.group, sid, 'ga')
        self.store.add_groups(sid, [make_group(sid, key='ga', rule_id='r.group', tier=Tier.JUNK, kind=Kind.CACHE)])
        self.store.add_items(sid, [later])
        self.assertEqual(self.store.item(sid, first.key), first)
        self.assertEqual(self.store.item(sid, later.key), later)
        self.assertEqual([g.key for g in self.store.groups(sid)], ['ga'])
        self.assertEqual(self.store.group(sid, 'ga').rule_id, 'r.group')
        self.assertEqual(self.store.count_items(sid, group_key='ga'), 2)
        self.assertEqual(self.rows('SELECT COUNT(*) FROM groups')[0][0], 1)

    def test_a_group_row_added_twice_is_refused(self):
        sid = self.new_scan()
        self.store.add_groups(sid, [make_group(sid, key='ga')])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add_groups(sid, [make_group(sid, key='ga')])
        self.store.add_items(sid, [make_item(sid, 1, group_key='gb')])
        self.store.add_groups(sid, [make_group(sid, key='gb')])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add_groups(sid, [make_group(sid, key='gb')])

    def test_a_long_iterable_is_written_in_chunks_in_one_transaction(self):
        sid = self.new_scan()
        original = store_mod.ITEM_BATCH
        store_mod.ITEM_BATCH = 7
        try:
            self.store.add_items(sid, (make_item(sid, n) for n in range(30)))
            self.assertEqual(self.store.count_items(sid), 30)
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.add_items(sid, [make_item(sid, 100 + n) for n in range(20)] + [make_item(sid, 100)])
        finally:
            store_mod.ITEM_BATCH = original
        self.assertEqual(self.store.count_items(sid), 30)                # the failed call left nothing, not even a chunk

    def test_items_still_round_trip_in_bulk_with_unicode_names(self):
        sid = self.new_scan()
        items = [make_item(sid, n, path=f'~/Library/Caches/{n % 7}/中文 é/{n}') for n in range(300)]
        self.store.add_items(sid, items)
        self.assertEqual(sorted(self.store.iter_items(sid), key=lambda i: i.key), sorted(items, key=lambda i: i.key))


class IncludeFingerprintTests(StoreCase):
    def test_saving_a_spec_records_its_includes_and_forgets_the_removed_ones(self):
        sid = self.new_scan()
        a, b = make_item(sid, 1), make_item(sid, 2, ino=7)
        self.store.add_items(sid, [a, b])
        self.store.save_spec('s', Spec(scan_id=sid, version=1, include={a.key: None, b.key: Action.DELETE}))
        got = self.store.include_fingerprints('s', [a.key, b.key, 'nope'])
        self.assertEqual(got, {a.key: (a.path, a.fingerprint()), b.key: (b.path, b.fingerprint())})
        self.assertEqual(self.store.include_fingerprints('other', [a.key]), {})
        self.store.save_spec('s', Spec(scan_id=sid, version=2, include={b.key: None}))
        self.assertEqual(set(self.store.include_fingerprints('s', [a.key, b.key])), {b.key})
        self.store.save_spec('s', Spec(scan_id=sid, version=3))
        self.assertEqual(self.store.include_fingerprints('s', [a.key, b.key]), {})

    def test_a_fingerprint_is_recorded_once_and_survives_a_later_save(self):
        old, new = self.new_scan(), None
        item = make_item(old, 1)
        self.store.add_items(old, [item])
        self.store.save_spec('s', Spec(scan_id=old, include={item.key: None}))
        self.store.finish_scan(old, 'done', 1, 1, 1)
        new = self.new_scan()
        changed = make_item(new, 1, ino=12345)
        self.store.add_items(new, [changed])
        self.store.save_spec('s', Spec(scan_id=new, version=1, include={item.key: None}))   # as a migration saves
        self.assertEqual(self.store.include_fingerprints('s', [item.key])[item.key][1], item.fingerprint())

    def test_an_include_of_an_item_the_scan_lacks_gets_no_row_and_a_spec_still_saves(self):
        sid = self.new_scan()
        self.store.save_spec('s', Spec(scan_id=sid, include={'ghost': None}))
        self.assertEqual(self.store.include_fingerprints('s', ['ghost']), {})
        self.assertEqual(self.store.load_spec('s').include, {'ghost': None})

    def test_a_bundle_fingerprint_round_trips_as_json(self):
        sid = self.new_scan()
        bundle = make_item(sid, 1, unit=Unit.BUNDLE, files=4)
        self.store.add_items(sid, [bundle])
        self.store.save_spec('s', Spec(scan_id=sid, include={bundle.key: None}))
        self.assertEqual(self.store.include_fingerprints('s', [bundle.key])[bundle.key][1], bundle.fingerprint())


class MigrationTests(unittest.TestCase):
    """A database written by the first release (schema 1) opens with the current code."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'steward.db'

    def v1_database(self, filler=0):
        """A schema 1 file holding one done scan with items, a plan with entries, an archive, a spec, history,
        advice and a meta row, plus `filler` more items to give the VACUUM something to give back."""
        conn = sqlite3.connect(str(self.path), isolation_level=None)
        conn.execute('PRAGMA foreign_keys = ON')
        for statement in store_mod._SCHEMA_V1:
            conn.execute(statement)
        conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
        conn.execute("INSERT INTO meta VALUES ('spec_notes:default', '{\"kept\": 1}')")
        conn.execute("INSERT INTO scans (id, started_at, finished_at, status, catalog_hash, facts_hash, settings_hash, "
                     "domains, vol_total, vol_free, attributed) VALUES (1, 1.0, 2.0, 'done', 'c', 'f', 's', '[\"agent\"]', "
                     "100, 50, 25)")
        conn.execute(store_mod._insert_sql('groups', Group), store_mod._params(make_group(1)))
        old_items = [make_item(1, n) for n in range(3 + filler)]
        conn.executemany(store_mod._insert_sql('items', Item), [store_mod._params(i) for i in old_items])
        conn.execute("INSERT INTO scan_roots (scan_id, path, domain, status) VALUES (1, '~/r', 'agent', 'ok')")
        conn.execute("INSERT INTO scan_errors (scan_id, path, error) VALUES (1, '~/e', 'EPERM')")
        conn.execute("INSERT INTO group_history VALUES ('cache.app:~/Library', 1, 2.0, 12288, 3)")
        self.plan = make_plan(scan_id=1)
        self.entries = [make_entry('p1', 1), make_entry('p1', 2, unit=Unit.FILE, action=Action.DELETE)]
        conn.execute(store_mod._insert_sql('plans', Plan), store_mod._params(self.plan))
        conn.executemany(store_mod._insert_sql('plan_entries', Entry, ('updated_at',)),
                         [store_mod._params(e) + (3.0,) for e in self.entries])
        conn.execute("INSERT INTO archives (id, plan_id, seq, path, fmt, orig_size, orig_alloc, stored_bytes, sha_orig, "
                     "sha_stored, manifest, state, created_at) VALUES ('a1', 'p1', 1, '~/x/1', 'gz', 1, 2, 3, 'o', 's', "
                     "'{\"m\": 1}', 'ok', 4.0)")
        conn.execute("INSERT INTO specs VALUES ('default', 1, 2, '{\"scan_id\": 1, \"version\": 2}', 5.0)")
        conn.execute("INSERT INTO advice (scan_id, provider, cli_version, payload_digest, result, created_at) "
                     "VALUES (1, 'claude', '1', 'd', '{\"s\": []}', 6.0)")
        conn.close()

    def test_history_plans_archives_specs_and_meta_survive_and_the_scan_tables_start_empty(self):
        self.v1_database()
        store = Store(self.path)
        self.addCleanup(store.close)
        c = store._conn()
        self.assertEqual(store.meta('schema_version'), str(len(store_mod.MIGRATIONS)))
        self.assertEqual(store.plan('p1'), self.plan)
        self.assertEqual(store.entries('p1'), self.entries)
        self.assertEqual([a['id'] for a in store.archives()], ['a1'])
        self.assertEqual(store.archive('a1')['manifest'], {'m': 1})
        self.assertEqual(store.load_spec('default').version, 2)
        self.assertEqual(store.group_history('cache.app:~/Library'), [(1, 2.0, 12288, 3)])
        self.assertEqual(store.latest_advice(1)['result'], {'s': []})
        self.assertEqual(store.meta('spec_notes:default'), '{"kept": 1}')
        self.assertEqual((store.scan(1)['status'], store.scan(1)['attributed']), ('done', 25))
        # rebuilt by the next scan
        self.assertEqual((store.count_items(1), store.groups(1), store.scan_roots(1), store.scan_errors(1)),
                         (0, [], [], []))
        self.assertIn('dir_id', [r['name'] for r in c.execute('PRAGMA table_info(items)')])
        for table in ('dirs', 'evidence', 'spec_include_fp'):
            self.assertEqual(c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)

    def test_a_scan_runs_on_the_migrated_file(self):
        self.v1_database()
        store = Store(self.path)
        self.addCleanup(store.close)
        sid = store.begin_scan('c', 'f', 's', ['agent'])
        store.add_groups(sid, [make_group(sid)])
        item = make_item(sid, 1)
        store.add_items(sid, [item])
        store.finish_scan(sid, 'done', 1, 1, 1)
        self.assertEqual(store.item(sid, item.key), item)
        self.assertEqual(store.plan('p1'), self.plan)          # still points at scan 1, whose rows are gone
        self.assertEqual([h[0] for h in store.group_history('cache.app:~/Library')], [sid, 1])

    def test_the_file_is_vacuumed_once_and_becomes_incremental(self):
        self.v1_database(filler=3000)
        before = self.path.stat().st_size
        conn = sqlite3.connect(str(self.path))
        self.assertEqual(conn.execute('PRAGMA auto_vacuum').fetchone()[0], 0)
        conn.close()
        store = Store(self.path)
        self.addCleanup(store.close)
        c = store._conn()
        self.assertEqual(c.execute('PRAGMA auto_vacuum').fetchone()[0], 2)
        self.assertEqual(c.execute('PRAGMA freelist_count').fetchone()[0], 0)
        c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        self.assertLess(self.path.stat().st_size, before // 4)

    def test_a_current_file_is_not_vacuumed_again(self):
        self.v1_database()
        store = Store(self.path)
        sid = store.begin_scan('c', 'f', 's', ['agent'])
        store.add_items(sid, (make_item(sid, n) for n in range(2000)))
        store._conn().execute('DELETE FROM items')
        free = store._conn().execute('PRAGMA freelist_count').fetchone()[0]
        store.close()
        self.assertGreater(free, 0)
        again = Store(self.path)       # a VACUUM here would empty the freelist
        self.addCleanup(again.close)
        self.assertEqual(again._conn().execute('PRAGMA freelist_count').fetchone()[0], free)

    def test_a_fresh_file_is_created_incremental_and_at_the_current_version(self):
        store = Store(self.path)
        self.addCleanup(store.close)
        self.assertEqual(store._conn().execute('PRAGMA auto_vacuum').fetchone()[0], 2)
        self.assertEqual(store.meta('schema_version'), str(len(store_mod.MIGRATIONS)))

    def test_pruning_returns_pages_to_the_file_system(self):
        store = Store(self.path)
        self.addCleanup(store.close)
        clock = Clock(1_790_000_000.0)
        first = store.begin_scan('c', 'f', 's', ['agent'])
        store.add_items(first, (make_item(first, n, path=f'~/Library/Caches/d{n % 50}/file-{n}') for n in range(6000)))
        store.finish_scan(first, 'done', 1, 1, 1)
        big = self.path.stat().st_size
        second = store.begin_scan('c', 'f', 's', ['agent'])
        store.add_items(second, [make_item(second, n) for n in range(10)])
        store.finish_scan(second, 'done', 1, 1, 1)                 # drops the 6000 rows of `first`
        self.assertEqual(store._conn().execute('PRAGMA freelist_count').fetchone()[0], 0)
        self.assertLess(self.path.stat().st_size, big // 4)


class ItemQueryTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.sid = self.new_scan()
        self.items = [
            make_item(self.sid, 1, path='~/Library/Caches/Alpha/one', group_key='ga', size=500, newest_ns=300),
            make_item(self.sid, 2, path='~/Library/Caches/alpha/two', group_key='ga', size=100, newest_ns=100),
            make_item(self.sid, 3, path='~/Library/Caches/Beta/three', group_key='gb', size=900, newest_ns=200),
            make_item(self.sid, 4, path='~/Library/Caches/Éclair/four', group_key='gb', size=300, newest_ns=400),
            make_item(self.sid, 5, path='~/Library/Caches/100%_done/five', group_key='gb', size=10, newest_ns=500),
        ]
        self.store.add_items(self.sid, self.items)
        self.by = {i.path.rsplit('/', 1)[1]: i for i in self.items}

    def names(self, **kw):
        return [i.path.rsplit('/', 1)[1] for i in self.store.items(self.sid, **kw)]

    def test_default_order_is_alloc_desc(self):
        self.assertEqual(self.names(), ['five', 'four', 'three', 'two', 'one'])

    def test_orders(self):
        self.assertEqual(self.names(order='size'), ['three', 'one', 'four', 'two', 'five'])
        self.assertEqual(self.names(order='path'), ['five', 'one', 'three', 'two', 'four'])  # byte order
        self.assertEqual(self.names(order='newest'), ['five', 'four', 'one', 'three', 'two'])
        self.assertEqual(self.names(order='oldest'), ['two', 'three', 'one', 'four', 'five'])
        e = self.assertStewardError(ErrorCode.INVALID, self.store.items, self.sid, order='; DROP TABLE items')
        self.assertEqual(e.details['field'], 'order')

    def test_group_filter(self):
        self.assertEqual(self.names(group_key='ga'), ['two', 'one'])
        self.assertEqual(self.names(group_key='nope'), [])

    def test_keys_filter(self):
        keys = [self.by['one'].key, self.by['three'].key, 'unknown']
        self.assertEqual(self.names(keys=keys), ['three', 'one'])
        self.assertEqual(self.names(keys=[]), [])
        self.assertEqual(self.names(keys=iter(keys[:1])), ['one'])
        many = [i.key for i in self.items] + [f'k{n}' for n in range(5000)]
        self.assertEqual(len(self.store.items(self.sid, keys=many)), 5)

    def test_query_is_case_insensitive_substring_on_path(self):
        self.assertEqual(self.names(query='ALPHA'), ['two', 'one'])
        self.assertEqual(self.names(query='caches/alpha/o'), ['one'])
        self.assertEqual(self.names(query='éclair'), ['four'])
        self.assertEqual(self.names(query='ÉCLAIR'), ['four'])
        self.assertEqual(self.names(query='%'), ['five'])      # no LIKE wildcards
        self.assertEqual(self.names(query='_done'), ['five'])
        self.assertEqual(self.names(query='nomatch'), [])
        self.assertEqual(len(self.names(query='')), 5)

    def test_filters_combine(self):
        self.assertEqual(self.names(group_key='gb', query='e'), ['five', 'four', 'three'])
        self.assertEqual(self.names(group_key='gb', query='beta'), ['three'])
        self.assertEqual(self.names(group_key='ga', keys=[self.by['one'].key], query='alpha'), ['one'])

    def test_offset_and_limit(self):
        self.assertEqual(self.names(offset=1, limit=2), ['four', 'three'])
        self.assertEqual(self.names(offset=4), ['one'])
        self.assertEqual(self.names(offset=9), [])
        self.assertEqual(self.names(limit=0), [])

    def test_count_matches_filters(self):
        self.assertEqual(self.store.count_items(self.sid), 5)
        self.assertEqual(self.store.count_items(self.sid, group_key='gb'), 3)
        self.assertEqual(self.store.count_items(self.sid, query='ALPHA'), 2)
        self.assertEqual(self.store.count_items(self.sid, group_key='gb', query='alpha'), 0)
        self.assertEqual(self.store.count_items(self.sid, keys=[self.by['one'].key]), 1)

    def test_iter_items_and_item(self):
        self.assertEqual(sorted(self.store.iter_items(self.sid), key=lambda i: i.key),
                         sorted(self.items, key=lambda i: i.key))
        self.assertEqual(list(self.store.iter_items(self.sid + 1)), [])
        self.assertEqual(self.store.item(self.sid, self.by['two'].key), self.by['two'])
        self.assertStewardError(ErrorCode.NOT_FOUND, self.store.item, self.sid, 'nope')
        self.assertStewardError(ErrorCode.NOT_FOUND, self.store.item, self.sid + 1, self.by['two'].key)

    def test_iter_items_while_writing_on_the_same_thread(self):
        seen = 0
        for item in self.store.iter_items(self.sid):
            self.store.add_scan_error(self.sid, item.path, 'x')
            seen += 1
        self.assertEqual((seen, len(self.store.scan_errors(self.sid))), (5, 5))


class ClaimAndExcludeTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.sid = self.new_scan()
        self.store.save_plan(make_plan(scan_id=self.sid), [make_entry('p1', n) for n in range(1, 6)])

    def states(self):
        return {e.seq: e.state for e in self.store.entries('p1')}

    def test_claim_once(self):
        self.assertTrue(self.store.claim_entry('p1', 1))
        self.assertFalse(self.store.claim_entry('p1', 1))
        self.assertEqual(self.states()[1], EntryState.IN_PROGRESS)

    def test_claim_refuses_non_pending_and_unknown(self):
        self.store.exclude('p1', [2], 1)
        self.assertFalse(self.store.claim_entry('p1', 2))
        self.store.claim_entry('p1', 3)
        self.store.finish_entry('p1', 3, Outcome(EntryState.DONE))
        self.assertFalse(self.store.claim_entry('p1', 3))
        self.assertFalse(self.store.claim_entry('p1', 99))
        self.assertFalse(self.store.claim_entry('nope', 1))

    def test_finish_can_return_an_entry_to_pending(self):
        self.store.claim_entry('p1', 1)
        self.store.finish_entry('p1', 1, Outcome(EntryState.PENDING))
        self.assertTrue(self.store.claim_entry('p1', 1))

    def test_concurrent_claims_have_exactly_one_winner_per_entry(self):
        n_entries, n_threads = 300, 6
        self.store.save_plan(make_plan('race', scan_id=self.sid), [make_entry('race', n) for n in range(n_entries)])
        barrier = threading.Barrier(n_threads)
        wins = [[] for _ in range(n_threads)]
        errors = []

        def worker(idx):
            try:
                barrier.wait()
                for seq in range(n_entries):
                    if self.store.claim_entry('race', seq):
                        wins[idx].append(seq)
            except BaseException as e:  # surfaced below
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        claimed = sorted(seq for w in wins for seq in w)
        self.assertEqual(claimed, list(range(n_entries)))
        self.assertEqual(self.store.count_entries('race', EntryState.IN_PROGRESS), n_entries)

    def test_concurrent_claim_and_exclude_never_both_win(self):
        for round_no in range(20):
            pid = f'ce{round_no}'
            self.store.save_plan(make_plan(pid, scan_id=self.sid), [make_entry(pid, n) for n in range(50)])
            claimed = []
            excluded = []
            refused = []
            barrier = threading.Barrier(2)

            def claimer():
                barrier.wait()
                claimed.extend(s for s in range(50) if self.store.claim_entry(pid, s))

            def excluder():
                barrier.wait()
                for s in range(50):
                    try:
                        self.store.exclude(pid, [s], self.store.plan(pid).revision)
                        excluded.append(s)
                    except StewardError as e:
                        refused.append(e.code)

            ts = [threading.Thread(target=claimer), threading.Thread(target=excluder)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertLessEqual(set(refused), {ErrorCode.IN_PROGRESS, ErrorCode.REVISION_MISMATCH})
            self.assertEqual(set(claimed) & set(excluded), set())
            final = {e.seq: e.state for e in self.store.entries(pid)}
            self.assertTrue(all(final[s] == EntryState.IN_PROGRESS for s in claimed))
            self.assertTrue(all(final[s] == EntryState.EXCLUDED for s in excluded))

    def test_exclude_marks_entries_and_bumps_revision(self):
        self.assertEqual(self.store.exclude('p1', [2, 4], 1), 2)
        self.assertEqual(self.store.plan('p1').revision, 2)
        self.assertEqual([s for s, st in self.states().items() if st == EntryState.EXCLUDED], [2, 4])
        self.assertEqual(self.store.exclude('p1', [5], 2), 3)

    def test_exclude_revision_mismatch_changes_nothing(self):
        e = self.assertStewardError(ErrorCode.REVISION_MISMATCH, self.store.exclude, 'p1', [1], 7)
        self.assertEqual((e.details['current'], e.details['given']), (1, 7))
        self.assertEqual(self.store.plan('p1').revision, 1)
        self.assertEqual(set(self.states().values()), {EntryState.PENDING})

    def test_exclude_non_pending_is_all_or_nothing(self):
        self.store.claim_entry('p1', 2)
        e = self.assertStewardError(ErrorCode.IN_PROGRESS, self.store.exclude, 'p1', [1, 2, 3], 1)
        self.assertEqual(e.details['seqs'], [2])
        self.assertEqual(self.store.plan('p1').revision, 1)
        self.assertEqual(self.states()[1], EntryState.PENDING)
        self.assertEqual(self.states()[3], EntryState.PENDING)
        self.store.exclude('p1', [1], 1)
        self.assertStewardError(ErrorCode.IN_PROGRESS, self.store.exclude, 'p1', [1], 2)  # already excluded

    def test_exclude_unknown_plan_or_entry(self):
        self.assertStewardError(ErrorCode.NOT_FOUND, self.store.exclude, 'nope', [1], 1)
        self.assertStewardError(ErrorCode.NOT_FOUND, self.store.exclude, 'p1', [1, 99], 1)
        self.assertEqual(self.states()[1], EntryState.PENDING)

    def test_exclude_nothing_checks_revision_but_keeps_it(self):
        self.assertEqual(self.store.exclude('p1', [], 1), 1)
        self.assertStewardError(ErrorCode.REVISION_MISMATCH, self.store.exclude, 'p1', [], 5)

    def test_exclude_duplicate_seqs(self):
        self.assertEqual(self.store.exclude('p1', [3, 3, 3], 1), 2)


class PruneTests(StoreCase):
    def populate(self, tag):
        sid = self.new_scan()
        self.store.add_groups(sid, [make_group(sid, key='ga', items=2, alloc=100 * tag),
                                    make_group(sid, key=f'only-{tag}', items=1, alloc=5)])
        self.store.add_items(sid, [make_item(sid, tag * 10 + n, group_key='ga') for n in range(2)])
        self.store.add_scan_root(sid, '~/r', Domain.AGENT, 'ok', files=2)
        self.store.add_scan_error(sid, '~/e', 'EPERM')
        return sid

    def counts(self, sid):
        return (self.store.count_items(sid), len(self.store.groups(sid)), len(self.store.scan_roots(sid)),
                len(self.store.scan_errors(sid)))

    def test_only_the_latest_done_scan_keeps_its_rows(self):
        plan_scan = self.populate(1)
        self.store.finish_scan(plan_scan, 'done', 10, 5, 1)
        self.store.save_plan(make_plan(scan_id=plan_scan), [make_entry('p1', 1)])
        self.store.save_spec('default', Spec(scan_id=plan_scan, version=1))
        sids = [plan_scan]
        for tag in range(2, 6):
            sid = self.populate(tag)
            self.clock.advance(10)
            self.store.finish_scan(sid, 'done', 10, 5, 1)
            sids.append(sid)
        self.assertEqual([self.counts(s) for s in sids], [(0, 0, 0, 0)] * 4 + [(2, 2, 1, 1)])
        for sid in sids:
            self.assertEqual(self.store.scan(sid)['status'], 'done')   # scan rows themselves stay
        self.assertEqual(self.store.plan('p1').scan_id, plan_scan)
        self.assertEqual(len(self.store.entries('p1')), 1)
        self.assertEqual(self.store.load_spec('default').scan_id, plan_scan)

    def test_prune_can_wait_for_the_caller(self):
        old = self.populate(1)
        self.store.finish_scan(old, 'done', 1, 1, 1)
        new = self.populate(2)
        self.store.finish_scan(new, 'done', 1, 1, 1, prune=False)
        self.assertEqual((self.counts(old), self.counts(new)), ((2, 2, 1, 1), (2, 2, 1, 1)))
        self.assertEqual(len(self.store.group_history('ga')), 2)    # history is written either way
        self.store.prune_scans()
        self.assertEqual((self.counts(old), self.counts(new)), ((0, 0, 0, 0), (2, 2, 1, 1)))
        self.store.prune_scans()                                     # nothing left to do
        self.assertEqual(self.counts(new), (2, 2, 1, 1))

    def test_prune_scans_before_any_scan_finished_deletes_nothing(self):
        running = self.populate(1)
        self.store.prune_scans()
        self.assertEqual(self.counts(running), (2, 2, 1, 1))

    def test_pruning_collects_unused_dirs_and_evidence(self):
        def rows(table):
            return self.store._conn().execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        shared = (Evidence('evidence.shared', {'app': 'x'}),)
        old = self.new_scan()
        self.store.add_items(old, [make_item(old, 1, path='~/keep/a', evidence=shared),
                                   make_item(old, 2, path='~/old/only/b', evidence=(Evidence('evidence.only_old'),))])
        self.store.finish_scan(old, 'done', 1, 1, 1)
        self.assertEqual((rows('dirs'), rows('evidence')), (2, 2))
        new = self.new_scan()
        self.store.add_items(new, [make_item(new, 1, path='~/keep/a', evidence=shared),
                                   make_item(new, 3, path='~/keep/c', evidence=shared)])
        self.store.finish_scan(new, 'done', 1, 1, 1)
        self.assertEqual((rows('dirs'), rows('evidence')), (1, 1))   # only what the latest scan's items use
        self.assertEqual([(i.path, i.evidence) for i in self.store.items(new, order='path')],
                         [('~/keep/a', shared), ('~/keep/c', shared)])

    def test_group_history_is_never_pruned(self):
        sids = []
        for tag in range(1, 7):
            sid = self.populate(tag)
            self.clock.advance(10)
            self.store.finish_scan(sid, 'done', 10, 5, 1)
            sids.append(sid)
        hist = self.store.group_history('ga', limit=20)
        self.assertEqual([h[0] for h in hist], sids[::-1])
        self.assertEqual([h[2] for h in hist], [600, 500, 400, 300, 200, 100])
        self.assertEqual([h[3] for h in hist], [2] * 6)
        self.assertEqual(hist[0][1], self.store.scan(sids[-1])['finished_at'])
        self.assertEqual(len(self.store.group_history('ga', limit=2)), 2)
        self.assertEqual(len(self.store.group_history('only-1')), 1)
        self.assertEqual(self.store.group_history('nope'), [])

    def test_unfinished_and_unsuccessful_scans(self):
        done = []
        for t in (1, 2, 3):
            done.append(self.populate(t))
            self.store.finish_scan(done[-1], 'done', 1, 1, 1)
        cancelled = self.populate(4)
        self.store.finish_scan(cancelled, 'cancelled', None, None, None)
        self.assertEqual(self.counts(cancelled), (2, 2, 1, 1))            # no pruning on a cancelled scan
        self.assertEqual(self.store.group_history('only-4'), [])           # and no history either
        self.assertEqual([self.counts(s) for s in done], [(0, 0, 0, 0)] * 2 + [(2, 2, 1, 1)])
        fourth = self.populate(5)
        self.store.finish_scan(fourth, 'done', 1, 1, 1)
        self.assertEqual(self.counts(done[2]), (0, 0, 0, 0))
        self.assertEqual(self.counts(cancelled), (0, 0, 0, 0))             # not the latest done scan
        self.assertEqual(self.counts(fourth), (2, 2, 1, 1))

    def test_rows_of_a_crashed_running_scan_are_cleaned_up_by_the_next_finished_scan(self):
        crashed = self.populate(1)
        self.store.finish_scan(self.populate(2), 'done', 1, 1, 1)
        self.assertEqual(self.store.scan(crashed)['status'], 'running')
        self.assertEqual(self.counts(crashed), (0, 0, 0, 0))

    def test_discard_items_drops_what_a_cancelled_scan_streamed_in(self):
        kept = self.populate(1)
        self.store.finish_scan(kept, 'done', 1, 1, 1)
        partial = self.populate(2)
        self.store.add_items(partial, [make_item(partial, 7, path='~/gone/file', group_key='streamed-only')])
        self.store.discard_items(partial)
        self.assertEqual((self.store.count_items(partial), len(self.store.groups(partial)),
                          len(self.store.scan_roots(partial))), (0, 0, 1))   # roots are the scan's own record
        self.assertEqual(self.counts(kept), (2, 2, 1, 1))
        self.assertEqual(self.store._conn().execute("SELECT COUNT(*) FROM dirs WHERE path = '~/gone'").fetchone()[0], 0)

    def test_archives_and_advice_survive_pruning(self):
        first = self.populate(1)
        self.store.finish_scan(first, 'done', 1, 1, 1)
        self.store.save_plan(make_plan(scan_id=first), [])
        self.store.add_archive(dict(id='a', plan_id='p1', seq=1, path='~/x', fmt='gz', orig_size=1, orig_alloc=1,
                                    stored_bytes=1, sha_orig='a', sha_stored='b', manifest={}, state='ok'))
        self.store.add_advice(dict(scan_id=first, provider='p', cli_version='v', payload_digest='d', result={}))
        for tag in range(2, 6):
            self.store.finish_scan(self.populate(tag), 'done', 1, 1, 1)
        self.assertEqual(self.counts(first), (0, 0, 0, 0))
        self.assertEqual(len(self.store.archives()), 1)
        self.assertIsNotNone(self.store.latest_advice(first))


if __name__ == '__main__':
    unittest.main()
