from __future__ import annotations

import dataclasses
import hashlib
import json
import random
import tempfile
import time
import unittest
from pathlib import Path

import m2_fixture as fx
from disk_steward import planner
from disk_steward.catalog import load_catalog
from disk_steward.macos import FakeMacOS
from disk_steward.model import (Action, AgeBasis, DecisionState, EntryState, ErrorCode, GroupSel, PlanState, Spec,
                                StewardError, Tier, Unit)
from disk_steward.paths import item_key
from disk_steward.selection import apply_ops, migrate_spec, resolve
from disk_steward.settings import Settings, settings_hash
from disk_steward.store import Store

from m3_support import NOW, mk_group, mk_item, mk_store, scan_store
from support import DAY, Clock, FakeHome


class PlannerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # class cleanups run even when a later line of setUpClass raises, so the temp tree never leaks
        cls.fh = FakeHome(Clock()).__enter__()
        cls.addClassCleanup(cls.fh.cleanup)
        fx.build(cls.fh)
        cls.store = Store(cls.fh.data_dir / 'steward.db')
        cls.addClassCleanup(cls.store.close)
        cls.sid, cls.catalog, cls.settings, cls.mac = scan_store(cls.fh, cls.store)
        cls.now = cls.fh.clock()

    def spec(self, *ops, filter_days=14):
        return apply_ops(Spec(scan_id=self.sid, filter_min_age_days=filter_days), list(ops), self.store)

    def expected(self, spec, res, **extra):
        return {'spec_version': spec.version, 'digest': res.digest, 'scan_id': spec.scan_id, **extra}

    def create(self, spec, now=None, **expected_extra):
        now = self.now if now is None else now
        res = resolve(self.store, spec, now, self.settings, self.mac)
        expected = self.expected(spec, res, **expected_extra)
        return planner.create_plan(self.store, spec, now, expected, self.settings, self.mac), res


class CreatePlanTests(PlannerCase):
    def test_plan_row_and_entries(self):
        spec = self.spec({'op': 'select_group', 'group': 'codex.sessions:~/.codex/sessions/2026/10', 'on': True},
                         {'op': 'select_group', 'group': 'claude.projects:~/.claude/projects/-Users-me-proj', 'on': True},
                         {'op': 'select_group', 'group': 'codex.state-tmp', 'on': True})
        plan, res = self.create(spec)
        scan = self.store.scan(self.sid)
        self.assertEqual(len(plan.id), 24)
        int(plan.id, 16)
        self.assertEqual((plan.state, plan.revision, plan.scan_id), (PlanState.DRAFT, 1, self.sid))
        self.assertEqual((plan.catalog_hash, plan.facts_hash, plan.settings_hash),
                         (scan['catalog_hash'], scan['facts_hash'], scan['settings_hash']))
        self.assertEqual((plan.created_at, plan.expires_at), (self.now, self.now + 24 * 3600))
        self.assertEqual(plan.spec, spec.to_json())
        self.assertEqual(self.store.plan(plan.id), plan)
        self.assertEqual(plan.entries_digest, planner.compute_entries_digest(self.store.entries(plan.id)))
        self.assertEqual(plan.totals, {k: res.summary[k] for k in ('count', 'alloc', 'reclaim', 'by_action', 'warnings')})
        entries = self.store.entries(plan.id)
        self.assertEqual([e.seq for e in entries], list(range(1, len(entries) + 1)))
        self.assertEqual(len(entries), res.summary['count'])
        self.assertTrue(all(e.state == EntryState.PENDING for e in entries))
        for e in entries:
            item = self.store.item(self.sid, e.item_key)
            self.assertEqual((e.path, e.unit, e.rule_id, e.group_key, e.alloc, e.reclaim, dict(e.fp)),
                             (item.path, item.unit, item.rule_id, item.group_key, item.alloc, item.reclaim, item.fingerprint()))
            self.assertEqual(e.threshold_days, res.decisions[e.item_key].threshold_days)

    def test_entries_are_ordered_by_group_then_size(self):
        spec = self.spec({'op': 'select_tier', 'tier': 'junk', 'on': True}, {'op': 'select_group', 'group': 'claude.projects:~/.claude/projects/-Users-me-proj', 'on': True},
                         filter_days=0)
        plan, _ = self.create(spec)
        keys = [(e.group_key, -e.alloc) for e in self.store.entries(plan.id)]
        self.assertEqual(keys, sorted(keys))
        self.assertGreater(len({g for g, _ in keys}), 3)

    def test_stored_fingerprints_are_the_models_canonical_json(self):
        # create_plan writes fingerprint JSON by hand for speed; it must equal what the model would dump.
        spec = self.spec({'op': 'select_tier', 'tier': 'junk', 'on': True},
                         {'op': 'select_group', 'group': 'ollama.models', 'on': True},
                         {'op': 'select_group', 'group': 'codex.runtimes', 'on': True}, filter_days=0)
        plan, res = self.create(spec, now=self.now + 100 * DAY)
        rows = self.store._conn().execute('SELECT item_key, unit, fp FROM plan_entries WHERE plan_id = ?', (plan.id,)).fetchall()
        self.assertEqual({r['unit'] for r in rows}, {'file', 'bundle'})
        self.assertEqual(len(rows), res.summary['count'])
        for r in rows:
            item = self.store.item(self.sid, r['item_key'])
            self.assertEqual(r['fp'], json.dumps(item.fingerprint(), sort_keys=True, separators=(',', ':')))

    def test_threshold_of_an_explicit_include_is_the_rule_floor(self):
        key = item_key('~/.claude/debug/x.txt')               # claude.logs: floor 1 day, 5 days old
        plan, _ = self.create(self.spec({'op': 'toggle_item', 'item': key, 'on': True}))
        (entry,) = self.store.entries(plan.id)
        self.assertEqual((entry.item_key, entry.threshold_days, entry.action), (key, 1.0, Action.DELETE))

    def test_totals_count_each_action(self):
        spec = self.spec({'op': 'select_group', 'group': 'codex.sessions:duplicates', 'on': True, 'action': 'delete'},
                         {'op': 'select_group', 'group': 'codex.sessions:~/.codex/sessions/2026/10', 'on': True})
        plan, _ = self.create(spec)
        self.assertEqual({a: v['count'] for a, v in plan.totals['by_action'].items()}, {'delete': 1, 'archive': 2})
        self.assertEqual(plan.totals['warnings'], [{'key': 'warning.codex_both_copies', 'count': 1}])

    def test_empty_plan_carries_the_breakdown_of_what_was_selected(self):
        spec = self.spec({'op': 'select_group', 'group': 'claude.logs', 'on': True})   # 5 days old, filter 14
        res = resolve(self.store, spec, self.now, self.settings, self.mac)
        with self.assertRaises(StewardError) as cm:
            planner.create_plan(self.store, spec, self.now, self.expected(spec, res), self.settings, self.mac)
        e = cm.exception
        self.assertEqual((e.code, e.message_key), (ErrorCode.EMPTY_PLAN, 'error.EMPTY_PLAN'))
        self.assertEqual(e.details['breakdown'], {'too_recent': {'count': 1, 'alloc': e.details['breakdown']['too_recent']['alloc']}})
        self.assertEqual(e.details['breakdown']['too_recent']['count'], 1)
        # the whole scan is reported separately, so the breakdown is not drowned in unrelated items
        self.assertEqual(e.details['total_breakdown'], res.summary['by_state'])
        self.assertGreater(e.details['total_breakdown']['protected']['count'], 5)

    def test_empty_plan_scope_includes_explicit_includes(self):
        key = item_key('~/.claude/plugins/cache/mkt/pluginA/1.0.0')     # protected: an include cannot select it
        spec = Spec(scan_id=self.sid, version=1, include={key: None})
        res = resolve(self.store, spec, self.now, self.settings, self.mac)
        with self.assertRaises(StewardError) as cm:
            planner.create_plan(self.store, spec, self.now, self.expected(spec, res), self.settings, self.mac)
        self.assertEqual(list(cm.exception.details['breakdown']), ['protected'])

    def test_spec_of_an_older_scan_is_stale(self):
        store2 = Store(self.fh.data_dir / 'second.db')
        self.addCleanup(store2.close)
        sid1, _, settings, mac = scan_store(self.fh, store2)
        sid2, _, _, _ = scan_store(self.fh, store2)
        old = Spec(scan_id=sid1, filter_min_age_days=0, groups={'codex.logs': GroupSel()})
        expected = {'spec_version': 0, 'digest': 'x', 'scan_id': sid1}
        with self.assertRaises(StewardError) as cm:
            planner.create_plan(store2, old, self.now, expected, settings, mac)
        self.assertEqual((cm.exception.code, cm.exception.message_key), (ErrorCode.INVALID, 'error.spec_stale'))
        self.assertEqual((cm.exception.details['from_scan'], cm.exception.details['to_scan'],
                          cm.exception.details['notes']), (sid1, sid2, None))
        new = dataclasses.replace(old, scan_id=sid2)
        res = resolve(store2, new, self.now, settings, mac)
        plan = planner.create_plan(store2, new, self.now, {'spec_version': 0, 'digest': res.digest, 'scan_id': sid2},
                                   settings, mac)
        self.assertEqual(plan.scan_id, sid2)

    def test_a_client_that_looked_at_an_older_scan_gets_spec_stale_with_the_migration_notes(self):
        store2 = Store(self.fh.data_dir / 'second.db')
        self.addCleanup(store2.close)
        sid1, _, settings, mac = scan_store(self.fh, store2)
        seen = apply_ops(Spec(scan_id=sid1, filter_min_age_days=0), [{'op': 'select_group', 'group': 'codex.logs', 'on': True}], store2)
        before = resolve(store2, seen, self.now, settings, mac)
        self.assertEqual(before.summary['count'], 1)
        expected = self.expected(seen, before)                                  # what the client holds: scan sid1
        self.fh.file('~/.codex/log/second.log', size=10, age_days=10)           # the disk moved on, a rescan follows
        sid2, _, _, _ = scan_store(self.fh, store2, prune=False)               # App.scan prunes after migrating
        migrated, notes = migrate_spec(store2, seen, sid2, now=self.now, enabled_domains=settings.domains)
        self.assertEqual(notes['group_changes'], [{'group': 'codex.logs', 'old_selected': 1, 'new_selected': 2,
                                                   'old_alloc': before.summary['alloc'], 'new_alloc': notes['group_changes'][0]['new_alloc']}])
        with self.assertRaises(StewardError) as cm:
            planner.create_plan(store2, migrated, self.now, expected, settings, mac, notes=notes)
        e = cm.exception
        self.assertEqual((e.code, e.message_key), (ErrorCode.INVALID, 'error.spec_stale'))
        self.assertEqual((e.details['from_scan'], e.details['to_scan'], e.details['notes']), (sid1, sid2, notes))
        # looking again (the new scan id and digest) works
        res = resolve(store2, migrated, self.now, settings, mac)
        plan = planner.create_plan(store2, migrated, self.now, self.expected(migrated, res), settings, mac)
        self.assertEqual((plan.scan_id, plan.totals['count']), (sid2, 2))

    def test_the_digest_cannot_be_replayed_against_another_scan(self):
        # same spec content, same version, different scan: the digest differs because it names the scan
        store2 = Store(self.fh.data_dir / 'third.db')
        self.addCleanup(store2.close)
        sid1, _, settings, mac = scan_store(self.fh, store2)
        sid2, _, _, _ = scan_store(self.fh, store2)
        one = Spec(scan_id=sid1, version=3, filter_min_age_days=0, groups={'codex.logs': GroupSel()})
        two = dataclasses.replace(one, scan_id=sid2)
        self.assertNotEqual(resolve(store2, one, self.now, settings, mac).digest, resolve(store2, two, self.now, settings, mac).digest)

    def test_rules_changed_since_the_scan_refuses_the_plan(self):
        spec = self.spec({'op': 'select_group', 'group': 'codex.state-tmp', 'on': True})
        other = dataclasses.replace(self.settings, idle_days=9)                 # classification setting
        res = resolve(self.store, spec, self.now, other, self.mac)
        with self.assertRaises(StewardError) as cm:
            planner.create_plan(self.store, spec, self.now, self.expected(spec, res), other, self.mac)
        self.assertEqual((cm.exception.code, cm.exception.message_key), (ErrorCode.RULES_CHANGED, 'error.RULES_CHANGED'))
        scan = self.store.scan(self.sid)
        expected_which = [name for name, now_hash in (('catalog_hash', load_catalog(other).hash),
                                                      ('settings_hash', settings_hash(other))) if scan[name] != now_hash]
        self.assertIn('settings_hash', expected_which)
        self.assertEqual(cm.exception.details['which'], expected_which)
        # a setting that does not affect classification is fine
        calm = dataclasses.replace(self.settings, default_min_age_days=30)
        plan = planner.create_plan(self.store, spec, self.now, self.expected(spec, res), calm, self.mac)
        self.assertEqual(plan.settings_hash, self.store.scan(self.sid)['settings_hash'])

    def test_a_changed_catalog_refuses_the_plan_and_a_given_catalog_is_used(self):
        spec = self.spec({'op': 'select_group', 'group': 'codex.state-tmp', 'on': True})
        res = resolve(self.store, spec, self.now, self.settings, self.mac)
        expected = self.expected(spec, res)
        edited = dataclasses.replace(self.catalog, hash='f' * 64)
        with self.assertRaises(StewardError) as cm:
            planner.create_plan(self.store, spec, self.now, expected, self.settings, self.mac, catalog=edited)
        self.assertEqual((cm.exception.code, cm.exception.details['which']), (ErrorCode.RULES_CHANGED, ['catalog_hash']))
        planner.create_plan(self.store, spec, self.now, expected, self.settings, self.mac, catalog=self.catalog)
        planner.create_plan(self.store, spec, self.now, expected, self.settings, self.mac)      # loaded from settings

    def test_rules_changed_is_reported_before_a_digest_mismatch_and_after_staleness(self):
        spec = self.spec({'op': 'select_group', 'group': 'codex.state-tmp', 'on': True})
        other = dataclasses.replace(self.settings, idle_days=9)
        with self.assertRaises(StewardError) as cm:
            planner.create_plan(self.store, spec, self.now, {'spec_version': 99, 'digest': 'x', 'scan_id': spec.scan_id},
                                other, self.mac)
        self.assertEqual(cm.exception.code, ErrorCode.RULES_CHANGED)
        with self.assertRaises(StewardError) as cm:
            planner.create_plan(self.store, spec, self.now, {'spec_version': 99, 'digest': 'x', 'scan_id': spec.scan_id + 7},
                                other, self.mac)
        self.assertEqual(cm.exception.message_key, 'error.spec_stale')

    def test_bad_expected(self):
        spec = self.spec({'op': 'select_group', 'group': 'codex.state-tmp', 'on': True})
        sid = spec.scan_id
        for expected in (None, {}, {'digest': 'x'}, {'spec_version': 'a', 'digest': 'x', 'scan_id': sid},
                         {'spec_version': 1, 'digest': 5, 'scan_id': sid}, {'spec_version': 1, 'digest': 'x'},
                         {'spec_version': 1, 'digest': 'x', 'scan_id': '1'}, {'spec_version': True, 'digest': 'x', 'scan_id': sid}):
            with self.subTest(expected=expected), self.assertRaises(StewardError) as cm:
                planner.create_plan(self.store, spec, self.now, expected, self.settings, self.mac)
            self.assertEqual(cm.exception.code, ErrorCode.INVALID)


class DigestMismatchTests(PlannerCase):
    LOG = '~/.claude/debug/x.txt'    # claude.logs, 5 days old; the filter below is 14 days

    def spec_logs(self):
        return self.spec({'op': 'select_group', 'group': 'claude.logs', 'on': True})

    def test_clock_advance_names_the_item_that_crossed_the_threshold(self):
        spec = self.spec_logs()
        seen = resolve(self.store, spec, self.now, self.settings, self.mac)
        self.assertEqual(seen.summary['count'], 0)
        later = self.now + 10 * DAY                  # the file is now 15 days old
        expected = self.expected(spec, seen, resolved_at=self.now)
        with self.assertRaises(StewardError) as cm:
            planner.create_plan(self.store, spec, later, expected, self.settings, self.mac)
        e = cm.exception
        self.assertEqual((e.code, e.message_key), (ErrorCode.DIGEST_MISMATCH, 'error.DIGEST_MISMATCH'))
        item = self.store.item(self.sid, item_key(self.LOG))
        self.assertEqual(e.details['added'], [{'path': self.LOG, 'action': 'delete', 'alloc': item.alloc}])
        self.assertEqual((e.details['removed'], e.details['added_count'], e.details['removed_count']), ([], 1, 0))

    def test_the_plan_can_be_created_once_the_new_digest_is_accepted(self):
        spec = self.spec_logs()
        later = self.now + 10 * DAY
        plan, res = self.create(spec, now=later)
        self.assertEqual([e.path for e in self.store.entries(plan.id)], [self.LOG])

    def test_without_a_timestamp_the_lists_are_empty_and_counts_unknown(self):
        spec = self.spec_logs()
        seen = resolve(self.store, spec, self.now, self.settings, self.mac)
        with self.assertRaises(StewardError) as cm:
            planner.create_plan(self.store, spec, self.now + 10 * DAY, self.expected(spec, seen), self.settings, self.mac)
        d = cm.exception.details
        self.assertEqual((d['added'], d['removed'], d['added_count'], d['removed_count']), ([], [], None, None))

    def test_a_different_spec_version_is_a_mismatch(self):
        spec = self.spec({'op': 'select_group', 'group': 'codex.state-tmp', 'on': True})
        res = resolve(self.store, spec, self.now, self.settings, self.mac)
        with self.assertRaises(StewardError) as cm:
            planner.create_plan(self.store, spec, self.now, self.expected(spec, res, spec_version=spec.version - 1,
                                                                          resolved_at=self.now), self.settings, self.mac)
        self.assertEqual(cm.exception.code, ErrorCode.DIGEST_MISMATCH)
        self.assertEqual(cm.exception.details['added_count'], None)   # the earlier spec is gone: nothing to diff

    def test_a_wrong_digest_is_a_mismatch_even_when_the_timestamp_matches_nothing(self):
        spec = self.spec_logs()
        with self.assertRaises(StewardError) as cm:
            planner.create_plan(self.store, spec, self.now, {'spec_version': spec.version, 'digest': '0' * 64,
                                                             'scan_id': spec.scan_id, 'resolved_at': self.now},
                                self.settings, self.mac)
        self.assertEqual(cm.exception.code, ErrorCode.DIGEST_MISMATCH)
        self.assertEqual(cm.exception.details['added_count'], None)

    def test_the_list_is_capped_at_50_largest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'many.db')
            self.addCleanup(store.close)
            sid = store.begin_scan(self.catalog.hash, 'f', settings_hash(self.settings), ['agent'])
            store.add_groups(sid, [mk_group('g', scan_id=sid)])
            store.add_items(sid, [mk_item(f'k{i:03d}', age=10, group='g', scan_id=sid, alloc=4096 * (i + 1), reclaim=4096 * (i + 1))
                                  for i in range(70)])
            store.finish_scan(sid, 'done', 1, 1, 1)
            spec = Spec(scan_id=sid, version=1, filter_min_age_days=14, groups={'g': GroupSel()})
            t0 = NOW
            seen = resolve(store, spec, t0, self.settings, self.mac)
            self.assertEqual(seen.summary['count'], 0)
            with self.assertRaises(StewardError) as cm:
                planner.create_plan(store, spec, t0 + 10 * DAY, {'spec_version': 1, 'digest': seen.digest,
                                                                 'scan_id': sid, 'resolved_at': t0}, self.settings, self.mac)
            d = cm.exception.details
            self.assertEqual((len(d['added']), d['added_count']), (50, 70))
            self.assertEqual(d['added'][0]['alloc'], 4096 * 70)
            self.assertEqual([a['alloc'] for a in d['added']], sorted((a['alloc'] for a in d['added']), reverse=True))


class ValidateForRunTests(PlannerCase):
    def setUp(self):
        spec = self.spec({'op': 'select_group', 'group': 'codex.state-tmp', 'on': True})
        self.plan, _ = self.create(spec)
        self.hashes = {'catalog_hash': self.plan.catalog_hash, 'facts_hash': self.plan.facts_hash,
                       'settings_hash': self.plan.settings_hash}

    def check(self, now=None, hashes=None, revision=None, digest=None):
        planner.validate_for_run(self.plan, hashes or self.hashes, self.now + 60 if now is None else now,
                                 self.plan.revision if revision is None else revision,
                                 self.plan.entries_digest if digest is None else digest)

    def expect(self, code, **kw):
        with self.assertRaises(StewardError) as cm:
            self.check(**kw)
        self.assertEqual((cm.exception.code, cm.exception.message_key), (code, f'error.{code.value}'))
        return cm.exception

    def test_fresh_plan_passes(self):
        self.check()

    def test_expiry(self):
        self.check(now=self.plan.expires_at - 1)
        self.expect(ErrorCode.PLAN_EXPIRED, now=self.plan.expires_at)
        self.expect(ErrorCode.PLAN_EXPIRED, now=self.plan.expires_at + DAY)

    def test_facts_never_block_a_plan_as_a_whole(self):
        self.check(hashes=dict(self.hashes, facts_hash='different'))   # entries are re-classified at run time

    def test_each_hash_changes_the_rules(self):
        for name in ('catalog_hash', 'settings_hash'):
            with self.subTest(name):
                e = self.expect(ErrorCode.RULES_CHANGED, hashes=dict(self.hashes, **{name: 'different'}))
                self.assertEqual(e.details['changed'], [name])

    def test_a_started_plan_tolerates_its_own_facts_change_but_not_rule_changes(self):
        # Deleting planned versions changes facts; resuming must not be refused for that.
        self.plan = dataclasses.replace(self.plan, state=PlanState.PAUSED)
        self.check(hashes=dict(self.hashes, facts_hash='different'))
        for name in ('catalog_hash', 'settings_hash'):
            with self.subTest(name):
                e = self.expect(ErrorCode.RULES_CHANGED, hashes=dict(self.hashes, **{name: 'different'}))
                self.assertEqual(e.details['changed'], [name])

    def test_stale_client_view_says_which_part_mismatched(self):
        e = self.expect(ErrorCode.REVISION_MISMATCH, revision=self.plan.revision + 1)
        self.assertEqual((e.details['which'], e.details['current'], e.details['given']), (['revision'], 1, 2))
        self.assertEqual(self.expect(ErrorCode.REVISION_MISMATCH, digest='0' * 64).details['which'], ['entries_digest'])
        self.assertEqual(self.expect(ErrorCode.REVISION_MISMATCH, revision=9, digest='0' * 64).details['which'],
                         ['revision', 'entries_digest'])

    def test_checks_run_in_the_documented_order(self):
        bad = dict(self.hashes, catalog_hash='x')
        self.expect(ErrorCode.PLAN_EXPIRED, now=self.plan.expires_at, hashes=bad, revision=99)
        self.expect(ErrorCode.RULES_CHANGED, hashes=bad, revision=99)

    def test_exclusions_bump_the_revision_but_not_the_entries_digest(self):
        before = self.store.plan(self.plan.id)
        new_revision = self.store.exclude(self.plan.id, [1], before.revision)
        after = self.store.plan(self.plan.id)
        self.assertEqual((new_revision, after.revision), (2, 2))
        self.assertEqual(after.entries_digest, before.entries_digest)
        self.assertEqual(planner.compute_entries_digest(self.store.entries(self.plan.id)), after.entries_digest)  # excluded entry keeps its place
        planner.validate_for_run(after, self.hashes, self.now, 2, before.entries_digest)
        with self.assertRaises(StewardError) as cm:
            planner.validate_for_run(after, self.hashes, self.now, 1, before.entries_digest)
        self.assertEqual(cm.exception.code, ErrorCode.REVISION_MISMATCH)

    def test_entries_digest_covers_every_entry_and_its_fingerprint(self):
        entries = self.store.entries(self.plan.id)
        self.assertEqual(planner.compute_entries_digest(entries), self.plan.entries_digest)
        changed = [dataclasses.replace(entries[0], fp=dict(entries[0].fp, size=entries[0].fp['size'] + 1))] + entries[1:]
        self.assertNotEqual(planner.compute_entries_digest(changed), self.plan.entries_digest)
        self.assertNotEqual(planner.compute_entries_digest(entries[:-1]), self.plan.entries_digest)


class MigrateOverRescansTests(unittest.TestCase):
    """migrate_spec against real rescans of a changing disk."""

    def setUp(self):
        self.fh = FakeHome(Clock()).__enter__()
        self.addCleanup(self.fh.cleanup)
        fx.build(self.fh)
        self.store = Store(self.fh.data_dir / 'steward.db')
        self.addCleanup(self.store.close)
        self.sid, _, self.settings, self.mac = scan_store(self.fh, self.store)
        self.now = self.fh.clock()

    def rescan(self):
        sid, _, _, _ = scan_store(self.fh, self.store)
        return sid

    def migrate(self, spec, sid):
        return migrate_spec(self.store, spec, sid, now=self.now, enabled_domains=self.settings.domains)

    def test_a_replaced_file_loses_its_include_and_an_untouched_one_keeps_it(self):
        models = '~/.codex/cache/models.json'                    # codex.cache, not selected: an explicit include
        tui = '~/.codex/log/codex-tui.log'
        spec = apply_ops(Spec(scan_id=self.sid), [{'op': 'toggle_item', 'item': item_key(models), 'on': True},
                                                 {'op': 'toggle_item', 'item': item_key(tui), 'on': True}], self.store)
        self.assertEqual(set(spec.include), {item_key(models), item_key(tui)})
        self.store.save_spec('default', spec)                    # records what the two files look like now
        self.fh.path(models).unlink()
        self.fh.file(models, size=100, age_days=10)             # same path, same size, same age: a new inode
        sid2 = self.rescan()
        out, notes = self.migrate(spec, sid2)
        self.assertEqual(set(out.include), {item_key(tui)})
        self.assertEqual(notes['include_changed'], {'count': 1, 'paths': [models]})
        self.assertEqual(notes['include_missing'], {'count': 0, 'paths': []})
        # a file rewritten in place (same inode, new content and mtime) is also a different decision
        spec2 = dataclasses.replace(out, scan_id=sid2)
        self.fh.path(tui).write_text('rewritten after you chose it')
        sid3 = self.rescan()
        again, notes = self.migrate(spec2, sid3)
        self.assertEqual((again.include, notes['include_changed']['paths']), ({}, [tui]))

    def test_an_include_whose_file_is_gone_is_reported_missing(self):
        tui = '~/.codex/log/codex-tui.log'
        spec = apply_ops(Spec(scan_id=self.sid), [{'op': 'toggle_item', 'item': item_key(tui), 'on': True}], self.store)
        self.store.save_spec('default', spec)
        self.fh.path(tui).unlink()
        out, notes = self.migrate(spec, self.rescan())
        self.assertEqual((out.include, notes['include_missing']), ({}, {'count': 1, 'paths': [tui]}))

    def test_an_excluded_item_that_vanishes_for_one_scan_stays_excluded_when_it_returns(self):
        log = '~/.claude/debug/x.txt'
        spec = apply_ops(Spec(scan_id=self.sid, filter_min_age_days=0),
                         [{'op': 'select_group', 'group': 'claude.logs', 'on': True},
                          {'op': 'toggle_item', 'item': item_key(log), 'on': False}], self.store)
        self.assertEqual(spec.exclude, frozenset([item_key(log)]))
        self.fh.path(log).unlink()
        sid2 = self.rescan()
        gone, notes = self.migrate(spec, sid2)
        self.assertEqual((gone.exclude, notes['kept_excludes']), (frozenset([item_key(log)]), 1))
        self.fh.file(log, size=100, age_days=5)                  # it comes back
        sid3 = self.rescan()
        back, _ = self.migrate(gone, sid3)
        self.assertEqual(back.exclude, frozenset([item_key(log)]))
        res = resolve(self.store, back, self.now, self.settings, self.mac)
        self.assertEqual(res.decisions[item_key(log)].state, DecisionState.EXCLUDED)
        self.assertEqual(res.summary['count'], 0)


def random_spec(rng, scan_id, groups, item_keys):
    """Anything a client could end up with, including nonsense: includes of protected items, actions a rule lacks."""
    chosen = {}
    for g in rng.sample(groups, rng.randint(0, min(8, len(groups)))):
        action = rng.choice([None] + [a for a in Action])
        chosen[g.key] = GroupSel(rng.choice([None, None, 0, 2, 8, 14, 25, 100]), action if rng.random() < 0.5 else None)
    include = {k: rng.choice([None, None, Action.DELETE, Action.ARCHIVE, Action.TRASH])
               for k in rng.sample(item_keys, rng.randint(0, min(5, len(item_keys))))}
    rest = [k for k in item_keys if k not in include]
    exclude = frozenset(rng.sample(rest, rng.randint(0, min(5, len(rest)))))
    return Spec(scan_id=scan_id, version=rng.randint(0, 50), filter_min_age_days=rng.choice([0, 1, 3, 7, 14, 30, 90]),
                groups=chosen, include=include, exclude=exclude)


def oracle(store, scan_id, spec, now, enabled, trash):
    """What a plan may contain, worked out from the raw group and item rows alone: {key: (action, threshold)}."""
    groups = {g.key: g for g in store.groups(scan_id)}
    out = {}
    for item in store.iter_items(scan_id):
        group = groups[item.group_key]
        if item.tier == Tier.PROTECTED or item.taint or item.domain.value not in enabled:
            continue
        if item.unit == Unit.FILE and item.nlink > 1 or item.key in spec.exclude:
            continue
        sel = spec.groups.get(item.group_key)
        included = item.key in spec.include
        if sel is None and not included:
            continue
        action = ((spec.include.get(item.key) if included else None) or (sel.action if sel else None)
                  or group.default_action)
        if action is None or action not in group.actions or (action == Action.TRASH and not trash):
            continue
        ns = item.newest_ns
        if group.age_basis == AgeBasis.LAST_USED and item.last_used_ns and item.last_used_ns > ns:
            ns = item.last_used_ns
        age = (now - ns / 1e9) / 86400.0
        floor = group.min_age_floor_days
        if age < floor:
            continue
        if included:
            threshold = floor            # an explicit include ("include anyway") yields only to the rule floor
        else:
            wanted = sel.min_age_days if sel.min_age_days is not None else max(group.default_min_age_days or 0.0,
                                                                                spec.filter_min_age_days)
            threshold = max(floor, wanted)
        if age >= threshold:
            out[item.key] = (action, threshold)
    return out


class PropertyTests(unittest.TestCase):
    """For any spec: a plan holds exactly the items the raw rows allow, each with a valid action and threshold,
    and the same selection that resolve() reports."""
    FIXTURE_ROUNDS = 200
    SYNTHETIC_ROUNDS = 120

    @classmethod
    def setUpClass(cls):
        cls.fh = FakeHome(Clock()).__enter__()
        cls.addClassCleanup(cls.fh.cleanup)
        fx.build(cls.fh)
        cls.store = Store(cls.fh.data_dir / 'steward.db')
        cls.addClassCleanup(cls.store.close)
        cls.sid, cls.catalog, cls.settings, _ = scan_store(cls.fh, cls.store)
        cls.now = cls.fh.clock()
        cls.coverage = {'plans': 0, 'empty': 0, 'trash_entries': 0, 'no_trash_rounds': 0}

    def check(self, store, sid, settings, mac, spec, now, label):
        enabled = set(settings.domains)
        trash = mac.trash_available()
        res = resolve(store, spec, now, settings, mac)
        expected = {'spec_version': spec.version, 'digest': res.digest, 'scan_id': sid}
        allowed = oracle(store, sid, spec, now, enabled, trash)
        if not trash:
            self.coverage['no_trash_rounds'] += 1
        if not allowed:
            self.coverage['empty'] += 1
            with self.assertRaises(StewardError) as cm:
                planner.create_plan(store, spec, now, expected, settings, mac)
            self.assertEqual(cm.exception.code, ErrorCode.EMPTY_PLAN, label)
            self.assertNotIn('selected', cm.exception.details['breakdown'])
            return
        self.coverage['plans'] += 1
        plan = planner.create_plan(store, spec, now, expected, settings, mac)
        entries = store.entries(plan.id)
        keys = [e.item_key for e in entries]
        self.assertEqual(len(keys), len(set(keys)), 'duplicate keys')
        # completeness and exactness against the raw-row oracle
        self.assertEqual({e.item_key: (e.action, e.threshold_days) for e in entries}, allowed, label)
        # and against resolve()
        self.assertEqual({(e.item_key, e.action) for e in entries}, set(res.selected_actions().items()))
        groups = {g.key: g for g in store.groups(sid)}
        for e in entries:
            item, group = store.item(sid, e.item_key), groups[e.group_key]
            self.assertNotEqual(item.tier, Tier.PROTECTED)
            self.assertEqual(item.taint, '')
            self.assertIn(item.domain.value, enabled)
            self.assertFalse(item.unit == Unit.FILE and item.nlink > 1)
            self.assertNotIn(e.item_key, spec.exclude)
            self.assertTrue(e.group_key in spec.groups or e.item_key in spec.include)
            self.assertIn(e.action, group.actions)
            if e.action == Action.TRASH:
                self.assertTrue(trash)
                self.coverage['trash_entries'] += 1
            ns = max(item.newest_ns, item.last_used_ns or 0) if group.age_basis == AgeBasis.LAST_USED else item.newest_ns
            age = (now - ns / 1e9) / 86400.0
            self.assertGreaterEqual(age, e.threshold_days)
            self.assertGreaterEqual(e.threshold_days, group.min_age_floor_days)
            self.assertEqual((e.path, e.unit, e.rule_id, e.alloc, e.reclaim, dict(e.fp)),
                             (item.path, item.unit, item.rule_id, item.alloc, item.reclaim, item.fingerprint()))
        self.assertEqual(plan.totals['count'], len(entries))
        self.assertEqual(plan.totals['alloc'], sum(e.alloc for e in entries))
        self.assertEqual(plan.totals['reclaim'], sum(e.reclaim for e in entries))
        self.assertEqual(plan.totals, {k: res.summary[k] for k in ('count', 'alloc', 'reclaim', 'by_action', 'warnings')})
        for action, v in plan.totals['by_action'].items():
            mine = [e for e in entries if e.action.value == action]
            self.assertEqual((v['count'], v['alloc'], v['reclaim']),
                             (len(mine), sum(e.alloc for e in mine), sum(e.reclaim for e in mine)))
        self.assertEqual(planner.compute_entries_digest(entries), plan.entries_digest)
        ordered = [(e.group_key, -e.alloc, e.item_key) for e in entries]
        self.assertEqual(ordered, sorted(ordered))

    def test_random_specs(self):
        self.coverage.update(plans=0, empty=0, trash_entries=0, no_trash_rounds=0)
        self.fixture_scan()
        self.synthetic_scans()
        # the fixed seeds must reach every branch, or the property proves little
        c = self.coverage
        self.assertGreater(c['plans'], 150)
        self.assertGreater(c['empty'], 30)
        self.assertGreater(c['trash_entries'], 5)
        self.assertGreater(c['no_trash_rounds'], 100)

    def fixture_scan(self):
        rng = random.Random(20261028)
        groups = self.store.groups(self.sid)
        keys = [i.key for i in self.store.iter_items(self.sid)]
        for n in range(self.FIXTURE_ROUNDS):
            mac = FakeMacOS(trash_ok=rng.random() < 0.5)
            spec = random_spec(rng, self.sid, groups, keys)
            self.check(self.store, self.sid, self.settings, mac, spec, self.now + rng.uniform(-1, 60) * DAY, n)

    def synthetic_scans(self):
        rng = random.Random(7)
        for domains in (('agent',), ('app-library',), ('agent', 'app-library')):
            settings = Settings(domains=domains)
            with tempfile.TemporaryDirectory() as tmp:
                store, sid = mk_store(tmp, catalog_hash=load_catalog(settings).hash, settings_hash=settings_hash(settings),
                                      tainted=True, domains=domains)
                try:
                    groups = store.groups(sid)
                    keys = [i.key for i in store.iter_items(sid)]
                    for n in range(self.SYNTHETIC_ROUNDS):
                        mac = FakeMacOS(trash_ok=rng.random() < 0.5)
                        spec = random_spec(rng, sid, groups, keys)
                        self.check(store, sid, settings, mac, spec, NOW + rng.uniform(-1, 60) * DAY, (domains, n))
                finally:
                    store.close()



class PerformanceTests(unittest.TestCase):
    N = 150_000
    BUDGET = 3.0

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.settings = Settings(domains=('agent',))
        cls.store = Store(Path(tmp.name) / 'big.db')
        cls.addClassCleanup(cls.store.close)
        cls.sid = cls.store.begin_scan(load_catalog(cls.settings).hash, 'f', settings_hash(cls.settings), ['agent'])
        cls.groups = [mk_group(f'g{j}', scan_id=cls.sid, tier=Tier.REVIEW) for j in range(400)]
        cls.store.add_groups(cls.sid, cls.groups)
        cls.store.add_items(cls.sid, (mk_item(f'{i:020x}', age=i % 40 + 1, group=f'g{i % 400}', scan_id=cls.sid)
                                      for i in range(cls.N)))
        cls.store.finish_scan(cls.sid, 'done', 1, 1, 1)

    def test_create_plan_with_150k_entries(self):
        mac = FakeMacOS()
        spec = Spec(scan_id=self.sid, version=1, filter_min_age_days=0, groups={g.key: GroupSel() for g in self.groups})
        res = resolve(self.store, spec, NOW, self.settings, mac)
        self.assertEqual(res.summary['count'], self.N)
        expected = {'spec_version': 1, 'digest': res.digest, 'scan_id': self.sid}
        best = None
        for _ in range(2):
            t0 = time.perf_counter()
            plan = planner.create_plan(self.store, spec, NOW, expected, self.settings, mac)
            took = time.perf_counter() - t0
            best = took if best is None else min(best, took)
        print(f'\ncreate_plan({self.N} entries): best of 2 = {best:.3f}s')
        self.assertLess(best, self.BUDGET)
        self.assertEqual((plan.totals['count'], self.store.count_entries(plan.id)), (self.N, self.N))
        # the digest written at that speed is the canonical one
        rows = self.store._conn().execute('SELECT seq, item_key, action, fp FROM plan_entries WHERE plan_id = ? ORDER BY seq',
                                          (plan.id,)).fetchall()
        canonical = json.dumps([[r['seq'], r['item_key'], r['action'], json.loads(r['fp'])] for r in rows],
                               sort_keys=True, separators=(',', ':'))
        self.assertEqual(hashlib.sha256(canonical.encode()).hexdigest(), plan.entries_digest)
        self.assertEqual([r['seq'] for r in rows[:3]], [1, 2, 3])


if __name__ == '__main__':
    unittest.main()
