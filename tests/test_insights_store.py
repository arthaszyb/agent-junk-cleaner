"""Schema 3: scan_totals (written by finish_scan, backfilled by the migration) and the cleanup history query."""
from __future__ import annotations

import collections
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import m2_fixture as fx
from disk_steward import queries, store as store_mod
from disk_steward.catalog import load_catalog
from disk_steward.model import Action, EntryState, Outcome, Tier
from disk_steward.settings import Settings
from disk_steward.store import Store, group_key_class
from m3_support import make_app
from support import Clock, FakeHome
from test_store import make_entry, make_plan


def v2_database(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute('PRAGMA foreign_keys = ON')
    for statement in store_mod._SCHEMA_V1 + store_mod._SCHEMA_V2:
        conn.execute(statement)
    conn.execute("INSERT INTO meta VALUES ('schema_version', '2')")
    return conn


def add_scan(conn, scan_id, status='done', finished=100.0):
    conn.execute("INSERT INTO scans (id, started_at, finished_at, status, catalog_hash, facts_hash, settings_hash, "
                 "domains, vol_total, vol_free, attributed) VALUES (?, ?, ?, ?, 'c', 'f', 's', '[\"agent\"]', 1000, "
                 "400, 600)", (scan_id, finished - 5, finished, status))


# group key -> (alloc, items); every tier, area and report-only flag the mapping has to get right
HISTORY = {
    'codex.logs': (100, 2),                                        # junk, agent
    'app.chrome-cache': (110, 1),                                  # junk, app-library
    'codex.runtimes': (200, 1),                                    # review, agent
    'user.large:~/Downloads/big': (210, 1),                        # review, user-files
    'dev.homebrew': (400, 1),                                      # review, dev-shared, report only (a hint)
    'ollama.models': (800, 1),                                     # review, agent, report only (a hint)
    'codex.state': (1600, 3),                                      # protected rule
    'claude.cli-versions#protected': (3200, 1),                    # protected variant of a junk rule
    'unknown:~/.claude/cache': (6400, 1),                          # unclaimed under an agent root
    'unknown:~/Library/Application Support/Foo': (12800, 1),       # unclaimed, app-library
    'unknown:~/Library/Application Support/Codex': (25600, 1),     # inside an agent root, but app-library wins
    'unknown:~/Downloads/x': (51200, 1),                           # unclaimed under a user root
    'prot.databases:agent': (102400, 1),                           # carries its area in the key
    'prot.credentials:app-library': (204800, 1),
    'gone.rule:~/.codex/x': (409600, 1),                           # a rule today's catalog no longer has
}
EXPECTED = {   # (tier, domain, report_only) -> (alloc, items)
    ('junk', 'agent', 0): (100, 2), ('junk', 'app-library', 0): (110, 1),
    ('review', 'agent', 0): (200, 1), ('review', 'user-files', 0): (210, 1),
    ('review', 'dev-shared', 1): (400, 1), ('review', 'agent', 1): (800, 1),
    ('protected', 'agent', 0): (1600 + 3200 + 6400 + 102400 + 409600, 3 + 1 + 1 + 1 + 1),
    ('protected', 'app-library', 0): (12800 + 25600 + 204800, 3),
    ('protected', 'user-files', 0): (51200, 1),
}


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'steward.db'

    def build_v2(self):
        conn = v2_database(self.path)
        for scan_id, finished in ((1, 100.0), (2, 200.0), (3, 300.0)):
            add_scan(conn, scan_id, finished=finished)
        add_scan(conn, 4, status='cancelled', finished=400.0)
        for scan_id in (1, 2, 3):
            for key, (alloc, items) in HISTORY.items():
                conn.execute('INSERT INTO group_history VALUES (?, ?, 1.0, ?, ?)', (key, scan_id, alloc, items))
        # the latest scan still has its groups: those are totalled exactly, whatever the catalog says today
        for n, (key, tier, domain, actions, alloc, reclaim, items) in enumerate((
                ('codex.logs', 'junk', 'agent', '["delete"]', 100, 70, 2),
                ('dev.homebrew', 'review', 'dev-shared', '[]', 400, 400, 1),
                ('codex.state', 'protected', 'agent', '[]', 1600, 1600, 3),
                ('weird.exact', 'review', 'agent', '["trash"]', 5, 4, 1))):
            conn.execute("INSERT INTO groups (id, scan_id, key, i_rule_id, i_tier, i_domain, i_kind, i_unit, provider, "
                         "domain, tier, actions, items, alloc, reclaim) VALUES (?, 3, ?, 'r', ?, ?, 'cache', 'file', "
                         "'p', ?, ?, ?, ?, ?, ?)", (n + 1, key, tier, domain, domain, tier, actions, items, alloc,
                                                      reclaim))
        conn.close()

    def rows(self, store, scan_id):
        return {(r[0], r[1], r[2]): tuple(r[3:]) for r in store._conn().execute(
            'SELECT tier, domain, report_only, alloc, items, reclaim, backfilled FROM scan_totals WHERE scan_id = ?',
            (scan_id,))}

    def test_old_scans_are_rebuilt_from_history_with_todays_catalog_and_flagged(self):
        self.build_v2()
        store = Store(self.path)
        self.addCleanup(store.close)
        self.assertEqual(store.meta('schema_version'), '3')
        for scan_id in (1, 2):
            got = self.rows(store, scan_id)
            self.assertEqual({k: v[:2] for k, v in got.items()}, EXPECTED)
            self.assertEqual({v[2:] for v in got.values()}, {(0, 1)})   # reclaim unknown, backfilled
        # the same key in two scans is the same row of data
        self.assertEqual(self.rows(store, 1), self.rows(store, 2))

    def test_the_latest_scan_is_totalled_exactly_from_its_groups(self):
        self.build_v2()
        store = Store(self.path)
        self.addCleanup(store.close)
        self.assertEqual(self.rows(store, 3), {
            ('junk', 'agent', 0): (100, 2, 70, 0), ('review', 'dev-shared', 1): (400, 1, 400, 0),
            ('protected', 'agent', 0): (1600, 3, 1600, 0), ('review', 'agent', 0): (5, 1, 4, 0)})

    def test_only_done_scans_get_totals_and_a_second_open_changes_nothing(self):
        self.build_v2()
        store = Store(self.path)
        before = store._conn().execute('SELECT * FROM scan_totals ORDER BY scan_id, tier, domain, report_only').fetchall()
        self.assertEqual({r['scan_id'] for r in before}, {1, 2, 3})
        store.close()
        again = Store(self.path)
        self.addCleanup(again.close)
        after = again._conn().execute('SELECT * FROM scan_totals ORDER BY scan_id, tier, domain, report_only').fetchall()
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])

    def test_a_database_without_history_migrates_to_an_empty_table(self):
        conn = v2_database(self.path)
        conn.close()
        store = Store(self.path)
        self.addCleanup(store.close)
        self.assertEqual(store._conn().execute('SELECT COUNT(*) FROM scan_totals').fetchone()[0], 0)
        self.assertEqual(store.meta('schema_version'), '3')

    def test_the_mapping_by_itself(self):
        catalog = load_catalog(Settings())
        for key, expected in (
                ('codex.logs', ('junk', 'agent', False)), ('codex.runtimes', ('review', 'agent', False)),
                ('ollama.models', ('review', 'agent', True)), ('dev.uv', ('review', 'dev-shared', True)),
                ('codex.state', ('protected', 'agent', False)),
                ('claude.cli-versions#protected', ('protected', 'agent', False)),
                ('claude.projects#protected:~/.claude/projects/x', ('protected', 'agent', False)),
                ('prot.git-tree:app-library', ('protected', 'app-library', False)),
                ('unknown:$CACHEDIR', ('protected', 'app-library', False)),
                ('unknown:~/.codex/ipc', ('protected', 'agent', False)),
                ('unknown:~/Documents/Taxes', ('protected', 'user-files', False)),
                ('unknown:~/Somewhere/else', ('protected', 'app-library', False)),   # outside every root
                ('removed.rule:~/Downloads', ('protected', 'user-files', False))):
            with self.subTest(key=key):
                self.assertEqual(group_key_class(key, catalog), expected)


class ScanTotalsTests(unittest.TestCase):
    def setUp(self):
        self.fh = FakeHome(Clock()).__enter__()
        self.addCleanup(self.fh.cleanup)
        fx.build(self.fh)
        self.app = make_app(self.fh, settings=Settings(domains=('agent', 'app-library')))
        self.addCleanup(self.app.close)

    def totals(self, scan_id):
        return {(r[0], r[1], r[2]): tuple(r[3:]) for r in self.app.store._conn().execute(
            'SELECT tier, domain, report_only, alloc, reclaim, items, backfilled FROM scan_totals WHERE scan_id = ?',
            (scan_id,))}

    def expected(self, scan_id):
        acc = collections.defaultdict(lambda: [0, 0, 0])
        for g in self.app.store.groups(scan_id):
            slot = acc[(g.tier.value, g.domain.value, int(not g.actions and g.tier != Tier.PROTECTED))]
            slot[0] += g.alloc
            slot[1] += g.reclaim
            slot[2] += g.items
        return {k: tuple(v) + (0,) for k, v in acc.items()}

    def test_a_new_scan_writes_its_totals_exactly(self):
        sid = self.app.scan()
        got = self.totals(sid)
        self.assertEqual(got, self.expected(sid))
        self.assertGreater(len(got), 3)
        self.assertEqual(sum(v[0] for v in got.values()), self.app.store.scan(sid)['attributed'])
        self.assertTrue(any(k[2] == 1 for k in got), 'the fixture holds a report-only group (ollama)')

    def test_totals_outlive_the_pruning_of_their_scan(self):
        first = self.app.scan()
        before = self.totals(first)
        self.fh.file('~/.claude/debug/new.txt', size=4096, age_days=3)
        second = self.app.scan()
        self.assertEqual(self.app.store.count_items(first), 0)          # the rows of the first scan are gone
        self.assertEqual(self.totals(first), before)                     # its totals are not
        self.assertEqual(self.totals(second), self.expected(second))
        self.assertNotEqual(self.totals(second), before)

    def test_a_cancelled_scan_writes_no_totals(self):
        from disk_steward.model import StewardError
        with self.assertRaises(StewardError):
            self.app.scan(cancel=lambda: True)
        self.assertEqual(self.app.store._conn().execute('SELECT COUNT(*) FROM scan_totals').fetchone()[0], 0)


class CleanupHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock(1_790_000_000.0)
        self.store = Store(Path(self.tmp.name) / 'steward.db', clock=self.clock)
        self.addCleanup(self.store.close)
        self.scan = self.store.begin_scan('c', 'f', 's', ['agent'])
        self.store.finish_scan(self.scan, 'done', 100, 50, 25)

    def plan(self, plan_id, entries, created=None, summary=None):
        """`entries` is a list of (state, action, alloc, freed); every entry ends up in that state at a later
        second than the one before."""
        if created is not None:
            self.clock.set(created)
        built = [make_entry(plan_id, i + 1, action=Action(a), alloc=alloc, reclaim=alloc)
                 for i, (_s, a, alloc, _f) in enumerate(entries)]
        self.store.save_plan(make_plan(plan_id, scan_id=self.scan, created_at=self.clock()), built)
        for seq, (state, _a, _alloc, freed) in enumerate(entries, 1):
            self.clock.advance(1)
            if state != 'pending':
                self.store.finish_entry(plan_id, seq, Outcome(EntryState(state), '', freed=freed))
        if summary is not None:
            self.store.set_meta(f'run:{plan_id}', json.dumps(summary))

    def test_one_row_per_plan_with_a_done_entry_newest_first(self):
        self.plan('p1', [('done', 'delete', 4096, 4096), ('done', 'trash', 8192, 0), ('skipped', 'delete', 1, 0),
                         ('failed', 'delete', 1, 0), ('excluded', 'delete', 1, 0), ('pending', 'delete', 1, 0)],
                  created=1_790_000_100.0, summary={'measured_delta': 5000, 'estimated_delta': 4096, 'freed': 4096})
        self.plan('p2', [('done', 'archive', 100, 30), ('skipped', 'delete', 1, 0), ('skipped', 'delete', 1, 0)],
                  created=1_790_001_000.0)
        self.plan('p3', [('skipped', 'delete', 1, 0), ('pending', 'delete', 1, 0)], created=1_790_002_000.0)
        self.plan('p4', [], created=1_790_003_000.0)
        rows = queries.cleanup_rows(self.store)
        self.assertEqual([r['plan_id'] for r in rows], ['p2', 'p1'])
        p1 = rows[1]
        self.assertEqual({k: p1[k] for k in ('items_done', 'items_skipped', 'items_failed', 'items_excluded',
                                              'removed', 'freed', 'moved_to_trash', 'measured_delta', 'estimated',
                                              'created_at')},
                         {'items_done': 2, 'items_skipped': 1, 'items_failed': 1, 'items_excluded': 1,
                          'removed': 4096 + 8192, 'freed': 4096, 'moved_to_trash': 8192, 'measured_delta': 5000,
                          'estimated': 4096, 'created_at': 1_790_000_100.0})
        self.assertEqual(p1['finished_at'], 1_790_000_100.0 + 2)      # the second done entry, two seconds on
        p2 = rows[0]
        self.assertEqual((p2['items_done'], p2['items_skipped'], p2['removed'], p2['freed'], p2['moved_to_trash']),
                         (1, 2, 100, 30, 0))
        self.assertEqual((p2['measured_delta'], p2['estimated']), (None, None))   # no run summary kept

    def test_a_summary_that_lacks_the_figures_gives_none(self):
        self.plan('p1', [('done', 'delete', 10, 10)], summary={'freed': 10})
        row = queries.cleanup_rows(self.store)[0]
        self.assertEqual((row['measured_delta'], row['estimated']), (None, None))

    def test_the_index_the_query_needs_exists_and_is_used(self):
        self.plan('p1', [('done', 'delete', 10, 10)])
        conn = self.store._conn()
        plans = ' | '.join(r[3] for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT plan_id, COUNT(*), SUM(alloc), SUM(freed), "
            "SUM(CASE WHEN action = 'trash' THEN alloc ELSE 0 END), MAX(updated_at) FROM plan_entries "
            "WHERE state = 'done' GROUP BY plan_id"))
        self.assertIn('COVERING INDEX plan_entries_done', plans)


if __name__ == '__main__':
    unittest.main()
