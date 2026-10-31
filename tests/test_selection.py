from __future__ import annotations

import dataclasses
import json
import tempfile
import time
import unittest
from pathlib import Path

import m2_fixture as fx
from disk_steward import selection
from disk_steward.macos import FakeMacOS
from disk_steward.model import (Action, AgeBasis, DecisionState, Domain, ErrorCode, GroupSel, Kind, Rule, Spec,
                                StewardError, Tier, Unit)
from disk_steward.paths import item_key
from disk_steward.selection import apply_ops, decide, resolve
from disk_steward.settings import Settings
from disk_steward.store import Store

from m3_support import NOW, mk_group, mk_item, mk_store, ns_ago, scan_store
from support import Clock, FakeHome

DAY = 86400.0
S = DecisionState
ENABLED = frozenset(('agent',))


def spec_for(group='g', sel=None, filter_days=14.0, **kw):
    return Spec(scan_id=1, version=1, filter_min_age_days=filter_days,
                groups={} if group is None else {group: sel or GroupSel()}, **kw)


def run(item, group=None, spec=None, rule=None, now=NOW, enabled=ENABLED, trash=True):
    return decide(item, group or mk_group(), rule, spec or spec_for(), now, enabled, trash)


class DecideTests(unittest.TestCase):
    """One test per branch, in the order decide() checks them."""

    def test_1_protected_tier(self):
        d = run(mk_item(tier=Tier.PROTECTED), mk_group(tier=Tier.PROTECTED, actions=()))
        self.assertEqual((d.state, d.reason_key), (S.PROTECTED, 'reason.protected'))

    def test_1_taint_protects_an_item_of_an_unprotected_tier(self):
        d = run(mk_item(taint='sqlite'))
        self.assertEqual((d.state, d.reason_key), (S.PROTECTED, 'reason.tainted'))

    def test_1_protection_beats_every_later_step(self):
        item = mk_item(tier=Tier.PROTECTED, domain=Domain.USER_FILES, nlink=3)
        spec = spec_for(include={item.key: None}, exclude=frozenset())
        self.assertEqual(run(item, spec=spec).state, S.PROTECTED)

    def test_2_domain_off(self):
        d = run(mk_item(domain=Domain.APP_LIBRARY))
        self.assertEqual((d.state, d.reason_key), (S.DOMAIN_OFF, 'reason.domain_off'))

    def test_2_domain_off_beats_no_gain_and_exclude(self):
        item = mk_item(domain=Domain.APP_LIBRARY, nlink=2)
        self.assertEqual(run(item, spec=spec_for(exclude=frozenset([item.key]))).state, S.DOMAIN_OFF)

    def test_3_hardlinked_file_frees_nothing(self):
        d = run(mk_item(nlink=2))
        self.assertEqual((d.state, d.reason_key), (S.NO_GAIN, 'reason.no_gain'))

    def test_3_hardlink_count_is_ignored_for_bundles(self):
        self.assertEqual(run(mk_item(unit=Unit.BUNDLE, nlink=5)).state, S.SELECTED)

    def test_3_no_gain_beats_exclude(self):
        item = mk_item(nlink=2)
        self.assertEqual(run(item, spec=spec_for(exclude=frozenset([item.key]))).state, S.NO_GAIN)

    def test_4_excluded_beats_group_and_include(self):
        item = mk_item()
        spec = spec_for(include={item.key: None}, exclude=frozenset([item.key]))   # not reachable via apply_ops
        d = run(item, spec=spec)
        self.assertEqual((d.state, d.reason_key), (S.EXCLUDED, 'reason.excluded'))

    def test_5_unselected(self):
        d = run(mk_item(), spec=spec_for(group=None))
        self.assertEqual((d.state, d.reason_key), (S.UNSELECTED, 'reason.unselected'))

    def test_5_other_groups_selection_does_not_apply(self):
        self.assertEqual(run(mk_item(), spec=spec_for(group='other')).state, S.UNSELECTED)

    def test_6_report_only_rule(self):
        d = run(mk_item(), mk_group(actions=(), default_action=None, hint='ollama rm x'))
        self.assertEqual((d.state, d.reason_key), (S.UNSUPPORTED_ACTION, 'reason.report_only'))

    def test_6_action_the_rule_lacks(self):
        d = run(mk_item(), spec=spec_for(sel=GroupSel(action=Action.TRASH)))
        self.assertEqual((d.state, d.reason_key), (S.UNSUPPORTED_ACTION, 'reason.unsupported_action'))
        self.assertEqual(d.reason_args, {'action': 'trash'})

    def test_6_trash_unavailable(self):
        group = mk_group(actions=(Action.DELETE, Action.TRASH))
        spec = spec_for(sel=GroupSel(action=Action.TRASH))
        self.assertEqual(run(mk_item(), group, spec, trash=False).state, S.UNSUPPORTED_ACTION)
        ok = run(mk_item(), group, spec, trash=True)
        self.assertEqual((ok.state, ok.action), (S.SELECTED, Action.TRASH))

    def test_6_action_precedence_include_then_group_then_default(self):
        group = mk_group(actions=(Action.DELETE, Action.ARCHIVE, Action.TRASH), default_action=Action.DELETE)
        item = mk_item()
        self.assertEqual(run(item, group, spec_for()).action, Action.DELETE)
        self.assertEqual(run(item, group, spec_for(sel=GroupSel(action=Action.ARCHIVE))).action, Action.ARCHIVE)
        both = spec_for(sel=GroupSel(action=Action.ARCHIVE), include={item.key: Action.TRASH})
        self.assertEqual(run(item, group, both).action, Action.TRASH)
        no_override = spec_for(sel=GroupSel(action=Action.ARCHIVE), include={item.key: None})
        self.assertEqual(run(item, group, no_override).action, Action.ARCHIVE)

    def test_7_floor_applies_to_group_selection(self):
        group = mk_group(min_age_floor_days=7.0)
        d = run(mk_item(age=3), group, spec_for(filter_days=0))
        self.assertEqual((d.state, d.reason_key, d.threshold_days), (S.TOO_RECENT_FLOOR, 'reason.too_recent_floor', 7.0))
        self.assertAlmostEqual(d.age_days, 3.0, places=3)
        self.assertEqual(set(d.reason_args), {'age', 'floor'})

    def test_7_floor_is_not_bypassed_by_explicit_include(self):
        item = mk_item(age=3)
        d = run(item, mk_group(min_age_floor_days=7.0), spec_for(group=None, include={item.key: None}))
        self.assertEqual(d.state, S.TOO_RECENT_FLOOR)

    def test_8_explicit_include_bypasses_the_filter(self):
        item = mk_item(age=2)
        d = run(item, mk_group(min_age_floor_days=1.0), spec_for(group=None, include={item.key: None}))
        self.assertEqual((d.state, d.via, d.threshold_days, d.reason_key),
                         (S.SELECTED, 'explicit', 1.0, 'reason.selected_explicit'))

    def test_8_include_inside_a_selected_group_is_included_anyway(self):
        group = mk_group(actions=(Action.DELETE, Action.ARCHIVE), default_action=Action.ARCHIVE)
        item = mk_item(age=5)
        spec = spec_for(include={item.key: Action.DELETE})                   # group selected, filter 14 days
        d = run(item, group, spec)
        self.assertEqual((d.state, d.action, d.via, d.threshold_days), (S.SELECTED, Action.DELETE, 'explicit', 0.0))
        # a group override cannot hold back an explicit include either; only the floor can
        spec = spec_for(sel=GroupSel(min_age_days=30), include={item.key: None})
        self.assertEqual(run(mk_item(age=20), group, spec).via, 'explicit')
        floored = mk_group(min_age_floor_days=7.0)
        self.assertEqual(run(item, floored, spec_for(include={item.key: None})).state, S.TOO_RECENT_FLOOR)

    def test_8_include_of_an_unselected_group_item_bypasses_the_filter(self):
        item = mk_item(age=5)
        d = run(item, spec=spec_for(group='other', include={item.key: None}))
        self.assertEqual((d.state, d.via, d.threshold_days), (S.SELECTED, 'explicit', 0.0))

    def test_8_explicit_include_does_not_bypass_protection(self):
        item = mk_item(tier=Tier.PROTECTED)
        self.assertEqual(run(item, spec=spec_for(group=None, include={item.key: None})).state, S.PROTECTED)

    def test_9_filter_selects_a_16_day_old_file_and_not_a_9_day_old_one(self):
        old = run(mk_item(age=16))
        young = run(mk_item(age=9))
        self.assertEqual((old.state, old.via, old.threshold_days, old.reason_key),
                         (S.SELECTED, 'group', 14.0, 'reason.selected_group'))
        self.assertEqual((young.state, young.threshold_days, young.reason_key), (S.TOO_RECENT, 14.0, 'reason.too_recent'))
        self.assertAlmostEqual(young.age_days, 9.0, places=3)

    def test_9_threshold_is_never_below_the_floor(self):
        d = run(mk_item(age=5), mk_group(min_age_floor_days=7.0), spec_for(filter_days=1))
        self.assertEqual(d.state, S.TOO_RECENT_FLOOR)
        d = run(mk_item(age=8), mk_group(min_age_floor_days=7.0), spec_for(filter_days=1))
        self.assertEqual((d.state, d.threshold_days), (S.SELECTED, 7.0))

    def test_9_per_group_override_beats_the_global_filter_both_ways(self):
        item = mk_item(age=10)
        self.assertEqual(run(item, spec=spec_for(filter_days=14)).state, S.TOO_RECENT)
        self.assertEqual(run(item, spec=spec_for(sel=GroupSel(min_age_days=5), filter_days=14)).state, S.SELECTED)
        self.assertEqual(run(item, spec=spec_for(sel=GroupSel(min_age_days=30), filter_days=0)).state, S.TOO_RECENT)

    def test_9_group_default_beats_the_filter_and_override_beats_the_default(self):
        group = mk_group(default_min_age_days=180.0)
        item = mk_item(age=100)
        self.assertEqual(run(item, group, spec_for(filter_days=14)).state, S.TOO_RECENT)
        self.assertEqual(run(item, group, spec_for(sel=GroupSel(min_age_days=30))).state, S.SELECTED)
        # The rule default may only tighten the user's filter: 365d set globally still wins over 180d.
        d = run(mk_item(age=200), group, spec_for(filter_days=365))
        self.assertEqual((d.state, d.threshold_days), (S.TOO_RECENT, 365.0))

    def test_age_basis_last_used_takes_the_later_of_the_two(self):
        group = mk_group(age_basis=AgeBasis.LAST_USED)
        item = mk_item(age=40, last_used_ns=ns_ago(2))
        d = run(item, group)
        self.assertEqual(d.state, S.TOO_RECENT)
        self.assertAlmostEqual(d.age_days, 2.0, places=3)
        older_use = mk_item(age=2, last_used_ns=ns_ago(40))
        self.assertAlmostEqual(run(older_use, group).age_days, 2.0, places=3)
        self.assertEqual(run(mk_item(age=40, last_used_ns=None), group).state, S.SELECTED)

    def test_age_basis_changed_ignores_last_used(self):
        item = mk_item(age=40, last_used_ns=ns_ago(2))
        self.assertEqual(run(item, mk_group()).state, S.SELECTED)

    def test_rule_argument_wins_over_the_group_copy(self):
        rule = Rule(id='r', provider='codex', domain=Domain.AGENT, tier=Tier.JUNK, kind=Kind.CACHE, patterns=('~/x/**',),
                    actions=(Action.DELETE,), default_action=Action.DELETE, min_age_floor_days=30.0)
        self.assertEqual(run(mk_item(age=20), rule=rule, spec=spec_for(filter_days=0)).state, S.TOO_RECENT_FLOOR)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store, self.sid = mk_store(self.tmp.name)
        self.addCleanup(self.store.close)
        self.empty = Spec(scan_id=self.sid, filter_min_age_days=14)

    def ops(self, *ops, spec=None, **kw):
        return apply_ops(spec or self.empty, list(ops), self.store, **kw)


class ApplyOpsTests(StoreCase):
    def test_pure_and_bumps_version_once_per_call(self):
        out = self.ops({'op': 'set_filter', 'min_age_days': 3}, {'op': 'select_group', 'group': 'g.junk', 'on': True},
                       {'op': 'toggle_item', 'item': 'j16', 'on': False})
        self.assertEqual(out.version, self.empty.version + 1)
        self.assertEqual(self.empty.groups, {})
        self.assertEqual(self.empty.version, 0)
        self.assertEqual(self.ops(spec=out).version, out.version + 1)    # an empty call still bumps

    def test_failing_op_leaves_nothing_behind(self):
        with self.assertRaises(StewardError):
            self.ops({'op': 'select_group', 'group': 'g.junk', 'on': True}, {'op': 'toggle_item', 'item': 'nope', 'on': True})
        self.assertEqual(self.empty.groups, {})

    def test_set_filter(self):
        self.assertEqual(self.ops({'op': 'set_filter', 'min_age_days': 0}).filter_min_age_days, 0)
        self.assertEqual(self.ops({'op': 'set_filter', 'min_age_days': 1.5}).filter_min_age_days, 1.5)
        for bad in (-1, 'x', None, True, float('nan'), float('inf')):
            with self.subTest(bad=bad), self.assertRaises(StewardError) as cm:
                self.ops({'op': 'set_filter', 'min_age_days': bad})
            self.assertEqual((cm.exception.code, cm.exception.message_key), (ErrorCode.INVALID, 'error.invalid_op'))

    def test_select_group_on_off_with_options(self):
        s = self.ops({'op': 'select_group', 'group': 'g.review', 'on': True, 'action': 'delete', 'min_age_days': 30})
        self.assertEqual(s.groups, {'g.review': GroupSel(30, Action.DELETE)})
        self.assertEqual(self.ops({'op': 'select_group', 'group': 'g.review', 'on': False}, spec=s).groups, {})
        self.assertEqual(self.ops({'op': 'select_group', 'group': 'g.review', 'on': False}).groups, {})

    def test_select_group_validates_the_key_against_the_scan(self):
        with self.assertRaises(StewardError) as cm:
            self.ops({'op': 'select_group', 'group': 'no.such', 'on': True})
        self.assertEqual(cm.exception.code, ErrorCode.NOT_FOUND)

    def test_action_the_rule_does_not_allow_is_invalid(self):
        for op in ({'op': 'select_group', 'group': 'g.review', 'on': True, 'action': 'trash'},
                   {'op': 'select_group', 'group': 'g.prot', 'on': True, 'action': 'delete'},
                   {'op': 'toggle_item', 'item': 'r40', 'on': True, 'action': 'trash'}):
            with self.subTest(op=op), self.assertRaises(StewardError) as cm:
                self.ops(op)
            self.assertEqual((cm.exception.code, cm.exception.message_key), (ErrorCode.INVALID, 'error.action_not_allowed'))
        for bad in ('shred', 5):
            with self.assertRaises(StewardError) as cm:
                self.ops({'op': 'select_group', 'group': 'g.junk', 'on': True, 'action': bad})
            self.assertEqual(cm.exception.message_key, 'error.invalid_op')

    def test_select_tier_junk_takes_selectable_junk_groups_of_enabled_domains(self):
        s = self.ops({'op': 'select_tier', 'tier': 'junk', 'on': True}, enabled_domains=('agent',))
        self.assertEqual(set(s.groups), {'g.junk'})
        both = self.ops({'op': 'select_tier', 'tier': 'junk', 'on': True}, enabled_domains=('agent', 'app-library'))
        self.assertEqual(set(both.groups), {'g.junk', 'g.app'})
        # default: the domains the scan covered
        self.assertEqual(set(self.ops({'op': 'select_tier', 'tier': 'junk', 'on': True}).groups), {'g.junk', 'g.app'})

    def test_select_tier_domain_filter_and_off(self):
        s = self.ops({'op': 'select_tier', 'tier': 'junk', 'domain': 'app-library', 'on': True})
        self.assertEqual(set(s.groups), {'g.app'})
        s = self.ops({'op': 'select_tier', 'tier': 'junk', 'on': True},
                     {'op': 'select_tier', 'tier': 'junk', 'domain': 'app-library', 'on': False})
        self.assertEqual(set(s.groups), {'g.junk'})
        self.assertEqual(self.ops({'op': 'select_tier', 'tier': 'junk', 'on': False}, spec=s).groups, {})

    def test_select_tier_keeps_an_existing_per_group_override(self):
        s = self.ops({'op': 'select_group', 'group': 'g.junk', 'on': True, 'min_age_days': 3},
                     {'op': 'select_tier', 'tier': 'junk', 'on': True})
        self.assertEqual(s.groups['g.junk'], GroupSel(3, None))

    def test_select_tier_only_junk(self):
        for tier in ('review', 'protected', 'x', None):
            with self.subTest(tier=tier), self.assertRaises(StewardError):
                self.ops({'op': 'select_tier', 'tier': tier, 'on': True})
        with self.assertRaises(StewardError):
            self.ops({'op': 'select_tier', 'tier': 'junk', 'domain': 'nowhere', 'on': True})

    def test_toggle_off_a_group_selected_item_excludes_it(self):
        s = self.ops({'op': 'select_group', 'group': 'g.junk', 'on': True}, {'op': 'toggle_item', 'item': 'j16', 'on': False})
        self.assertEqual((s.exclude, s.include), (frozenset(['j16']), {}))

    def test_toggle_on_an_excluded_item_removes_the_exclusion_only(self):
        s = self.ops({'op': 'select_group', 'group': 'g.junk', 'on': True}, {'op': 'toggle_item', 'item': 'j16', 'on': False},
                     {'op': 'toggle_item', 'item': 'j16', 'on': True})
        self.assertEqual((s.exclude, s.include), (frozenset(), {}))

    def test_toggle_on_in_an_unselected_group_includes_explicitly(self):
        s = self.ops({'op': 'toggle_item', 'item': 'r20', 'on': True})
        self.assertEqual((s.include, s.exclude), ({'r20': None}, frozenset()))
        s = self.ops({'op': 'toggle_item', 'item': 'r20', 'on': True, 'action': 'delete'})
        self.assertEqual(s.include, {'r20': Action.DELETE})

    def test_toggle_on_keeps_an_existing_override_without_new_action(self):
        s = self.ops({'op': 'toggle_item', 'item': 'r20', 'on': True, 'action': 'delete'},
                     {'op': 'toggle_item', 'item': 'r20', 'on': True})
        self.assertEqual(s.include, {'r20': Action.DELETE})

    def test_toggle_on_with_action_in_a_selected_group_makes_it_explicit(self):
        s = self.ops({'op': 'select_group', 'group': 'g.review', 'on': True},
                     {'op': 'toggle_item', 'item': 'r20', 'on': True, 'action': 'delete'})
        self.assertEqual(s.include, {'r20': Action.DELETE})

    def test_toggle_off_an_explicit_include_removes_it(self):
        s = self.ops({'op': 'toggle_item', 'item': 'r20', 'on': True}, {'op': 'toggle_item', 'item': 'r20', 'on': False})
        self.assertEqual((s.include, s.exclude), ({}, frozenset()))

    def test_toggle_off_an_explicit_include_inside_a_selected_group_also_excludes(self):
        s = self.ops({'op': 'select_group', 'group': 'g.review', 'on': True},
                     {'op': 'toggle_item', 'item': 'r20', 'on': True, 'action': 'delete'},
                     {'op': 'toggle_item', 'item': 'r20', 'on': False})
        self.assertEqual((s.include, s.exclude), ({}, frozenset(['r20'])))

    def test_toggle_off_something_not_selected_is_a_no_op(self):
        s = self.ops({'op': 'toggle_item', 'item': 'r20', 'on': False})
        self.assertEqual((s.include, s.exclude), ({}, frozenset()))

    def test_toggle_on_after_the_group_was_deselected_includes_explicitly(self):
        s = self.ops({'op': 'select_group', 'group': 'g.junk', 'on': True}, {'op': 'toggle_item', 'item': 'j16', 'on': False},
                     {'op': 'select_group', 'group': 'g.junk', 'on': False}, {'op': 'toggle_item', 'item': 'j16', 'on': True})
        self.assertEqual((s.include, s.exclude), ({'j16': None}, frozenset()))

    def test_include_and_exclude_never_overlap(self):
        import itertools
        steps = [{'op': 'toggle_item', 'item': 'j16', 'on': True}, {'op': 'toggle_item', 'item': 'j16', 'on': False},
                 {'op': 'select_group', 'group': 'g.junk', 'on': True}, {'op': 'select_group', 'group': 'g.junk', 'on': False},
                 {'op': 'toggle_item', 'item': 'j16', 'on': True, 'action': 'delete'}]
        for combo in itertools.product(steps, repeat=4):
            s = self.ops(*combo)
            self.assertFalse(set(s.include) & set(s.exclude), combo)

    def test_item_key_is_validated(self):
        with self.assertRaises(StewardError) as cm:
            self.ops({'op': 'toggle_item', 'item': 'nope', 'on': True})
        self.assertEqual(cm.exception.code, ErrorCode.NOT_FOUND)
        with self.assertRaises(StewardError) as cm:
            self.ops({'op': 'toggle_item', 'item': 5, 'on': True})
        self.assertEqual(cm.exception.message_key, 'error.invalid_op')

    def test_set_action_for_a_group_and_an_item(self):
        s = self.ops({'op': 'select_group', 'group': 'g.review', 'on': True, 'min_age_days': 9},
                     {'op': 'set_action', 'group': 'g.review', 'action': 'delete'})
        self.assertEqual(s.groups['g.review'], GroupSel(9, Action.DELETE))
        s = self.ops({'op': 'toggle_item', 'item': 'r20', 'on': True}, {'op': 'set_action', 'item': 'r20', 'action': 'archive'})
        self.assertEqual(s.include, {'r20': Action.ARCHIVE})

    def test_set_action_needs_a_selected_target_and_an_allowed_action(self):
        for op, key in (({'op': 'set_action', 'group': 'g.review', 'action': 'delete'}, 'error.not_selected'),
                        ({'op': 'set_action', 'item': 'r20', 'action': 'delete'}, 'error.not_selected'),
                        ({'op': 'set_action', 'item': 'r20', 'action': 'trash'}, 'error.action_not_allowed'),
                        ({'op': 'set_action', 'action': 'delete'}, 'error.invalid_op'),
                        ({'op': 'set_action', 'group': 'g.review', 'item': 'r20', 'action': 'delete'}, 'error.invalid_op'),
                        ({'op': 'set_action', 'group': 'g.review'}, 'error.invalid_op')):
            with self.subTest(op=op), self.assertRaises(StewardError) as cm:
                self.ops(op)
            self.assertEqual((cm.exception.code, cm.exception.message_key), (ErrorCode.INVALID, key))

    def test_clear_drops_the_selection_but_keeps_the_filter(self):
        s = self.ops({'op': 'set_filter', 'min_age_days': 3}, {'op': 'select_group', 'group': 'g.junk', 'on': True},
                     {'op': 'toggle_item', 'item': 'j16', 'on': False}, {'op': 'toggle_item', 'item': 'r20', 'on': True})
        c = self.ops({'op': 'clear'}, spec=s)
        self.assertEqual((c.groups, c.include, c.exclude, c.filter_min_age_days), ({}, {}, frozenset(), 3))

    def test_malformed_ops_are_invalid(self):
        for ops in ('select', None, [5], [{}], [{'op': 'frobnicate'}], [{'op': 'clear', 'extra': 1}],
                    [{'op': 'select_group', 'on': True}], [{'op': 'select_group', 'group': 'g.junk'}],
                    [{'op': 'select_group', 'group': 'g.junk', 'on': 'yes'}],
                    [{'op': 'select_group', 'group': 'g.junk', 'on': True, 'bogus': 1}]):
            with self.subTest(ops=ops), self.assertRaises(StewardError) as cm:
                apply_ops(self.empty, ops, self.store)
            self.assertEqual(cm.exception.code, ErrorCode.INVALID)

    def test_unknown_scan(self):
        with self.assertRaises(StewardError) as cm:
            apply_ops(Spec(scan_id=999), [], self.store)
        self.assertEqual(cm.exception.code, ErrorCode.NOT_FOUND)

    def test_toggle_on_refuses_what_could_never_be_selected(self):
        for item, reason in (('p1', 'protected'), ('rep', 'report_only')):
            with self.subTest(item=item), self.assertRaises(StewardError) as cm:
                self.ops({'op': 'toggle_item', 'item': item, 'on': True})
            e = cm.exception
            self.assertEqual((e.code, e.message_key, e.details['reason']), (ErrorCode.INVALID, 'error.not_selectable', reason))
            self.assertEqual((e.details['item'], e.details['path']), (item, f'~/x/{item}'))
        with self.assertRaises(StewardError) as cm:
            self.ops({'op': 'toggle_item', 'item': 'a30', 'on': True}, enabled_domains=('agent',))
        self.assertEqual(cm.exception.details['reason'], 'domain_off')
        self.assertEqual(self.ops({'op': 'toggle_item', 'item': 'a30', 'on': True}).include, {'a30': None})   # scan domains
        # the refusal beats the action check: a protected item says why, not "action not allowed"
        with self.assertRaises(StewardError) as cm:
            self.ops({'op': 'toggle_item', 'item': 'p1', 'on': True, 'action': 'delete'})
        self.assertEqual(cm.exception.message_key, 'error.not_selectable')

    def test_toggle_on_refuses_a_tainted_item_and_off_is_always_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, sid = mk_store(tmp, tainted=True)
            try:
                spec = Spec(scan_id=sid)
                with self.assertRaises(StewardError) as cm:
                    apply_ops(spec, [{'op': 'toggle_item', 'item': 'jt', 'on': True}], store)
                self.assertEqual(cm.exception.details['reason'], 'tainted')
                with self.assertRaises(StewardError) as cm:
                    apply_ops(spec, [{'op': 'toggle_item', 'item': 'jp', 'on': True}], store)
                self.assertEqual(cm.exception.details['reason'], 'protected')
                out = apply_ops(spec, [{'op': 'toggle_item', 'item': 'jt', 'on': False}, {'op': 'toggle_item', 'item': 'p1', 'on': False}], store)
                self.assertEqual((out.include, out.exclude), ({}, frozenset()))
            finally:
                store.close()


class MigrateTests(unittest.TestCase):
    """migrate_spec moves a selection from one scan to the next."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store, self.old = mk_store(self.tmp.name)
        self.addCleanup(self.store.close)

    def rescan(self, groups, items, keep_old=False):
        sid = self.store.begin_scan('c', 'f', 's', ['agent'])
        self.store.add_groups(sid, [mk_group(k, scan_id=sid, **kw) for k, kw in groups.items()])
        self.store.add_items(sid, [mk_item(name, age, g, scan_id=sid, **kw) for g, name, age, kw in items])
        self.store.finish_scan(sid, 'done', 1, 1, 1, prune=not keep_old)
        return sid

    def test_group_selectors_stay_only_while_the_group_exists(self):
        new = self.rescan({'g.junk': {}}, [('g.junk', 'j16', 16, {})])
        spec = Spec(scan_id=self.old, version=4, filter_min_age_days=9,
                    groups={'g.junk': GroupSel(3, None), 'g.review': GroupSel()})
        out, notes = selection.migrate_spec(self.store, spec, new)
        self.assertEqual((out.scan_id, out.version, out.filter_min_age_days), (new, 5, 9))
        self.assertEqual(out.groups, {'g.junk': GroupSel(3, None)})
        self.assertEqual((notes['from_scan'], notes['to_scan'], notes['dropped_groups']), (self.old, new, ['g.review']))
        self.assertEqual(notes['group_changes'], [])                           # no clock given

    def test_exclusions_are_never_dropped(self):
        new = self.rescan({'g.junk': {}}, [('g.junk', 'j16', 16, {})])        # j9 is gone from the new scan
        spec = Spec(scan_id=self.old, groups={'g.junk': GroupSel()}, exclude=frozenset(['j9', 'j16', 'never-existed']))
        out, notes = selection.migrate_spec(self.store, spec, new)
        self.assertEqual(out.exclude, frozenset(['j9', 'j16', 'never-existed']))
        self.assertEqual(notes['kept_excludes'], 3)
        back = self.rescan({'g.junk': {}}, [('g.junk', 'j16', 16, {}), ('g.junk', 'j9', 20, {})])   # j9 returns
        again, _ = selection.migrate_spec(self.store, out, back)
        self.assertEqual(again.exclude, frozenset(['j9', 'j16', 'never-existed']))
        res = selection.resolve_for(self.store, again, NOW, ('agent',), True)
        self.assertEqual((res.decisions['j9'].state, res.summary['count']), (S.EXCLUDED, 0))

    def test_an_include_survives_only_with_the_same_fingerprint(self):
        spec = Spec(scan_id=self.old, include={'j16': None, 'j9': Action.DELETE, 'j0': None, 'r20': None, 'r40': None})
        self.store.save_spec('default', spec)                              # records the five fingerprints
        new = self.rescan({'g.junk': {}, 'g.review': {'tier': Tier.REVIEW}},
                          [('g.junk', 'j16', 16, {'size': 999}),            # rewritten: different size
                           ('g.junk', 'j9', 9, {}),                         # unchanged
                           ('g.junk', 'j0', 0.5, {'ino': 77}),              # replaced: new inode
                           ('g.review', 'r20', 20, {})])                    # unchanged; r40 is gone
        out, notes = selection.migrate_spec(self.store, spec, new)
        self.assertEqual(out.include, {'j9': Action.DELETE, 'r20': None})
        self.assertEqual(notes['include_changed'], {'count': 2, 'paths': ['~/x/j0', '~/x/j16']})
        self.assertEqual(notes['include_missing'], {'count': 1, 'paths': ['~/x/r40']})

    def test_an_include_nothing_was_recorded_for_counts_as_changed(self):
        new = self.rescan({'g.junk': {}}, [('g.junk', 'j16', 16, {})])      # identical to the old j16
        out, notes = selection.migrate_spec(self.store, Spec(scan_id=self.old, include={'j16': None}), new)
        self.assertEqual((out.include, notes['include_changed']['count']), ({}, 1))

    def test_the_recorded_fingerprint_belongs_to_its_session(self):
        spec = Spec(scan_id=self.old, include={'j16': None})
        self.store.save_spec('mine', spec)
        new = self.rescan({'g.junk': {}}, [('g.junk', 'j16', 16, {})])
        kept, _ = selection.migrate_spec(self.store, spec, new, session='mine')
        other, notes = selection.migrate_spec(self.store, spec, new, session='yours')
        self.assertEqual((kept.include, other.include, notes['include_changed']['count']), ({'j16': None}, {}, 1))

    def test_a_missing_include_is_named_by_the_path_recorded_with_it(self):
        spec = Spec(scan_id=self.old, include={'r40': None})
        self.store.save_spec('default', spec)
        new = self.rescan({'g.review': {'tier': Tier.REVIEW}}, [('g.review', 'r20', 20, {})])
        out, notes = selection.migrate_spec(self.store, spec, new)
        self.assertEqual((out.include, notes['include_missing']), ({}, {'count': 1, 'paths': ['~/x/r40']}))

    def test_note_paths_are_capped_at_50(self):
        names = [f'f{i:03d}' for i in range(70)]
        old = self.rescan({'g': {}}, [('g', n, 20, {}) for n in names])
        spec = Spec(scan_id=old, include={n: None for n in names})
        self.store.save_spec('default', spec)
        new = self.rescan({'g': {}}, [('g', n, 20, {'size': 5}) for n in names])
        out, notes = selection.migrate_spec(self.store, spec, new)
        self.assertEqual((out.include, notes['include_changed']['count'], len(notes['include_changed']['paths'])), ({}, 70, 50))

    def test_group_changes_compare_what_would_be_selected_before_and_after(self):
        new = self.rescan({'g.junk': {'min_age_floor_days': 1.0, 'actions': (Action.DELETE, Action.TRASH)}},
                          [('g.junk', 'j16', 16, {}), ('g.junk', 'n1', 30, {}), ('g.junk', 'n2', 30, {}), ('g.junk', 'j9', 9, {})],
                          keep_old=True)                                    # the old figures come from its rows
        spec = Spec(scan_id=self.old, filter_min_age_days=14, groups={'g.junk': GroupSel(), 'g.review': GroupSel()})
        out, notes = selection.migrate_spec(self.store, spec, new, now=NOW, enabled_domains=('agent',))
        changes = {c['group']: c for c in notes['group_changes']}
        self.assertEqual(set(changes), {'g.junk', 'g.review'})
        # old scan: only j16 qualifies (j9 too recent, j0 under the floor, jhl hardlinked); new scan: j16, n1, n2
        self.assertEqual(changes['g.junk'], {'group': 'g.junk', 'old_selected': 1, 'new_selected': 3,
                                             'old_alloc': 4000, 'new_alloc': 4096 * 3})
        self.assertEqual(changes['g.review'], {'group': 'g.review', 'old_selected': 2, 'new_selected': 0,
                                               'old_alloc': 8000, 'new_alloc': 0})
        self.assertEqual(notes['dropped_groups'], ['g.review'])
        json.dumps(notes)


class ResolveTests(StoreCase):
    def resolve(self, spec, now=NOW, trash=True, domains=('agent',)):
        return selection.resolve_for(self.store, spec, now, domains, trash)

    def select(self, *ops, **kw):
        return self.ops(*ops, **kw)

    def test_summary_shape_and_states(self):
        spec = self.select({'op': 'select_tier', 'tier': 'junk', 'on': True}, {'op': 'select_group', 'group': 'g.review', 'on': True},
                           {'op': 'select_group', 'group': 'g.report', 'on': True}, enabled_domains=('agent',))
        res = self.resolve(spec)
        states = {k: v['count'] for k, v in res.summary['by_state'].items()}
        self.assertEqual(states, {'selected': 3, 'domain_off': 1, 'too_recent': 1, 'too_recent_floor': 1,
                                  'protected': 1, 'no_gain': 1, 'unsupported_action': 1})
        self.assertEqual(res.summary['total'], 9)
        chosen = {k for k, d in res.decisions.items() if d.state == S.SELECTED}
        self.assertEqual(chosen, {'j16', 'r40', 'r20'})
        self.assertEqual(res.summary['count'], 3)
        self.assertEqual(res.decisions['jhl'].state, S.NO_GAIN)
        self.assertEqual(res.decisions['rep'].reason_key, 'reason.report_only')
        self.assertEqual(res.decisions['a30'].state, S.DOMAIN_OFF)   # checked before whether its group is selected
        g = res.summary['groups']['g.junk']
        self.assertEqual((g['total'], g['selected'], g['too_recent'], g['floor_held'], g['excluded'], g['protected']),
                         (4, 1, 1, 1, 0, 0))
        self.assertEqual((g['too_recent_alloc'], g['floor_held_alloc']), (1000 * 3, 1000 * 3))   # j9 and j0
        self.assertEqual(res.summary['groups']['g.prot']['protected'], 1)

    def test_scope_by_state_covers_selected_groups_and_includes_only(self):
        spec = self.select({'op': 'select_group', 'group': 'g.junk', 'on': True},
                           {'op': 'toggle_item', 'item': 'r20', 'on': True})
        res = self.resolve(spec)
        scope = {k: v['count'] for k, v in res.summary['scope_by_state'].items()}
        self.assertEqual(scope, {'selected': 2, 'too_recent': 1, 'too_recent_floor': 1, 'no_gain': 1})
        self.assertEqual(res.summary['scope_by_state']['too_recent']['alloc'], 1000 * 3)
        self.assertEqual(res.summary['by_state']['unselected']['count'], 2)    # r40 and rep stay out of scope
        self.assertEqual(self.resolve(self.empty).summary['scope_by_state'], {})

    def test_include_anyway_inside_a_selected_group_and_undoing_an_exclusion(self):
        ops = [{'op': 'select_group', 'group': 'g.junk', 'on': True},
               {'op': 'toggle_item', 'item': 'j9', 'on': True, 'action': 'trash'}]      # 9 days old, filter 14
        res = self.resolve(self.select(*ops))
        self.assertEqual((res.decisions['j9'].state, res.decisions['j9'].action, res.decisions['j9'].via),
                         (S.SELECTED, Action.TRASH, 'explicit'))
        self.assertEqual(res.decisions['j16'].action, Action.DELETE)               # only the included item changes
        # Excluding then re-checking a qualifying item hands it back to the group instead of pinning it.
        back = self.select({'op': 'select_group', 'group': 'g.junk', 'on': True},
                           {'op': 'toggle_item', 'item': 'j16', 'on': False},
                           {'op': 'toggle_item', 'item': 'j16', 'on': True})
        self.assertNotIn('j16', back.include)
        self.assertEqual(self.resolve(back).decisions['j16'].via, 'group')

    def test_the_digest_names_the_scan(self):
        sid2 = self.store.begin_scan('c', 'f', 's', ['agent', 'app-library'])
        self.store.add_groups(sid2, [dataclasses.replace(g, scan_id=sid2) for g in self.store.groups(self.sid)])
        self.store.add_items(sid2, [dataclasses.replace(i, scan_id=sid2) for i in self.store.iter_items(self.sid)])
        self.store.finish_scan(sid2, 'done', 1, 1, 1, prune=False)
        one = Spec(scan_id=self.sid, version=3, groups={'g.review': GroupSel()})
        two = dataclasses.replace(one, scan_id=sid2)
        a, b = self.resolve(one), self.resolve(two)
        self.assertEqual(a.summary['count'], b.summary['count'])
        self.assertNotEqual(a.digest, b.digest)

    def test_domain_off_counts(self):
        spec = Spec(scan_id=self.sid, groups={'g.app': GroupSel()})
        res = self.resolve(spec)
        self.assertEqual(res.decisions['a30'].state, S.DOMAIN_OFF)
        self.assertEqual(self.resolve(spec, domains=('agent', 'app-library')).decisions['a30'].state, S.SELECTED)

    def test_by_action_delete_frees_reclaim(self):
        res = self.resolve(self.select({'op': 'select_group', 'group': 'g.junk', 'on': True}, {'op': 'set_filter', 'min_age_days': 0}))
        d = res.summary['by_action']['delete']
        j = {'j16': 4000, 'j9': 3000, 'j0': 3000}
        self.assertEqual(set(res.summary['by_action']), {'delete'})
        # j0 is 0.5 days old and the rule floor is 1 day, so only j16 and j9 are selected
        self.assertEqual((d['count'], d['alloc'], d['reclaim'], d['frees']), (2, j['j16'] + j['j9'], j['j16'] + j['j9'], j['j16'] + j['j9']))
        self.assertNotIn('moves', d)

    def test_by_action_trash_frees_nothing_until_emptied(self):
        res = self.resolve(self.select({'op': 'select_group', 'group': 'g.junk', 'on': True, 'action': 'trash'},
                                       {'op': 'set_filter', 'min_age_days': 0}))
        t = res.summary['by_action']['trash']
        self.assertEqual((t['count'], t['frees'], t['moves']), (2, 0, 7000))
        self.assertEqual(t['reclaim'], 7000)

    def test_trash_unavailable_makes_the_group_unsupported(self):
        spec = self.select({'op': 'select_group', 'group': 'g.junk', 'on': True, 'action': 'trash'})
        res = self.resolve(spec, trash=False)
        self.assertEqual(res.summary['count'], 0)
        self.assertEqual(res.decisions['j16'].state, S.UNSUPPORTED_ACTION)

    def test_by_action_archive_is_an_estimate(self):
        res = self.resolve(self.select({'op': 'select_group', 'group': 'g.review', 'on': True}))
        a = res.summary['by_action']['archive']
        self.assertEqual((a['count'], a['reclaim']), (2, 8000))
        self.assertEqual(a['frees_estimate'], round(8000 * 0.7))
        self.assertIs(a['estimated'], True)

    def test_totals_across_actions(self):
        res = self.resolve(self.select({'op': 'select_group', 'group': 'g.review', 'on': True, 'action': 'delete'},
                                       {'op': 'select_group', 'group': 'g.junk', 'on': True}))
        self.assertEqual(res.summary['count'], 3)
        self.assertEqual(res.summary['alloc'], sum(v['alloc'] for v in res.summary['by_action'].values()))
        self.assertEqual(res.summary['reclaim'], sum(v['reclaim'] for v in res.summary['by_action'].values()))

    def test_exclude_counts_in_the_group_summary(self):
        spec = self.select({'op': 'select_group', 'group': 'g.review', 'on': True}, {'op': 'toggle_item', 'item': 'r20', 'on': False})
        res = self.resolve(spec)
        self.assertEqual(res.decisions['r20'].state, S.EXCLUDED)
        self.assertEqual((res.summary['groups']['g.review']['selected'], res.summary['groups']['g.review']['excluded']), (1, 1))

    def test_digest_is_stable_and_follows_the_selection_and_version(self):
        spec = self.select({'op': 'select_group', 'group': 'g.review', 'on': True})
        a, b = self.resolve(spec), self.resolve(spec, now=NOW + 3600)
        self.assertEqual(a.digest, b.digest)           # the clock moved but nothing crossed a threshold
        self.assertEqual(len(a.digest), 64)
        bumped = self.ops(spec=spec)                   # same selection, new version
        self.assertNotEqual(self.resolve(bumped).digest, a.digest)
        other = self.ops({'op': 'toggle_item', 'item': 'r20', 'on': False}, spec=spec)
        self.assertNotEqual(self.resolve(other).digest, a.digest)
        def at_version_7(groups):
            return self.resolve(Spec(scan_id=self.sid, version=7, filter_min_age_days=14, groups=groups)).digest
        self.assertNotEqual(at_version_7({'g.review': GroupSel(action=Action.DELETE)}), at_version_7({'g.review': GroupSel()}))

    def test_digest_changes_when_time_moves_an_item_over_the_threshold(self):
        spec = self.select({'op': 'select_group', 'group': 'g.junk', 'on': True})
        before = self.resolve(spec, now=NOW)
        after = self.resolve(spec, now=NOW + 6 * DAY)     # j9 turns 15 days old
        self.assertNotEqual(before.digest, after.digest)
        self.assertEqual(after.decisions['j9'].state, S.SELECTED)

    def test_selected_actions(self):
        res = self.resolve(self.select({'op': 'select_group', 'group': 'g.review', 'on': True}))
        self.assertEqual(res.selected_actions(), {'r40': Action.ARCHIVE, 'r20': Action.ARCHIVE})

    def test_none_qualify_warning(self):
        spec = self.select({'op': 'select_group', 'group': 'g.junk', 'on': True, 'min_age_days': 400},
                           {'op': 'select_group', 'group': 'g.review', 'on': True})
        warnings = self.resolve(spec).summary['warnings']
        self.assertEqual(warnings, [{'key': 'warning.group_selected_none_qualify', 'count': 4, 'group': 'g.junk'}])

    def test_unknown_scan_is_not_found(self):
        with self.assertRaises(StewardError) as cm:
            self.resolve(Spec(scan_id=77))
        self.assertEqual(cm.exception.code, ErrorCode.NOT_FOUND)

    def test_resolve_asks_the_mac_about_trash_once(self):
        mac = FakeMacOS(trash_ok=False)
        res = resolve(self.store, self.select({'op': 'select_group', 'group': 'g.junk', 'on': True, 'action': 'trash'}),
                      NOW, Settings(domains=('agent',)), mac)
        self.assertEqual([c for c in mac.calls if c[0] == 'trash_available'], [('trash_available', ())])
        self.assertEqual(res.summary['count'], 0)


class CodexWarningTests(unittest.TestCase):
    """Uses the scanned fixture: PAIR_B is the legacy-ID copy of PAIR_A."""

    @classmethod
    def setUpClass(cls):
        cls.fh = FakeHome(Clock()).__enter__()
        cls.addClassCleanup(cls.fh.cleanup)
        fx.build(cls.fh)
        cls.store = Store(cls.fh.data_dir / 'steward.db')
        cls.addClassCleanup(cls.store.close)
        cls.sid, _, cls.settings, cls.mac = scan_store(cls.fh, cls.store)
        cls.now = cls.fh.clock()

    def warnings(self, *ops):
        spec = apply_ops(Spec(scan_id=self.sid, filter_min_age_days=14), [{'op': 'set_filter', 'min_age_days': 0}] + list(ops),
                         self.store)
        return resolve(self.store, spec, self.now, self.settings, self.mac).summary['warnings']

    def test_both_copies_selected(self):
        w = self.warnings({'op': 'select_group', 'group': 'codex.sessions:duplicates', 'on': True},
                          {'op': 'select_group', 'group': 'codex.sessions:~/.codex/sessions/2026/10', 'on': True})
        self.assertEqual(w, [{'key': 'warning.codex_both_copies', 'count': 1}])

    def test_only_one_side_is_fine(self):
        self.assertEqual(self.warnings({'op': 'select_group', 'group': 'codex.sessions:duplicates', 'on': True}), [])
        self.assertEqual(self.warnings({'op': 'select_group', 'group': 'codex.sessions:~/.codex/sessions/2026/10', 'on': True}), [])

    def test_explicit_includes_count_too_and_a_pair_counts_once(self):
        w = self.warnings({'op': 'toggle_item', 'item': item_key(fx.PAIR_A), 'on': True},
                          {'op': 'toggle_item', 'item': item_key(fx.PAIR_B), 'on': True})
        self.assertEqual(w, [{'key': 'warning.codex_both_copies', 'count': 1}])

    def test_excluding_the_kept_copy_clears_the_warning(self):
        w = self.warnings({'op': 'select_group', 'group': 'codex.sessions:duplicates', 'on': True},
                          {'op': 'select_group', 'group': 'codex.sessions:~/.codex/sessions/2026/10', 'on': True},
                          {'op': 'toggle_item', 'item': item_key(fx.PAIR_A), 'on': False})
        self.assertEqual(w, [])

    def test_none_qualify_names_the_group(self):
        w = self.warnings({'op': 'select_group', 'group': 'claude.logs', 'on': True, 'min_age_days': 400})
        self.assertEqual(w, [{'key': 'warning.group_selected_none_qualify', 'count': 1, 'group': 'claude.logs'}])

    def test_protected_group_selected_by_mistake_qualifies_nothing(self):
        w = self.warnings({'op': 'select_group', 'group': 'codex.state', 'on': True})
        self.assertEqual(w, [{'key': 'warning.group_selected_none_qualify', 'count': 1, 'group': 'codex.state'}])


class PerformanceTests(unittest.TestCase):
    N = 150_000
    BUDGET = 1.5

    def test_resolve_150k_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'big.db')
            self.addCleanup(store.close)
            sid = store.begin_scan('c', 'f', 's', ['agent'])
            n_groups = 400
            groups = [mk_group(f'g{j}', scan_id=sid, tier=Tier.REVIEW) for j in range(n_groups)]

            def items():
                for i in range(self.N):
                    yield mk_item(f'{i:020x}', age=i % 40, group=f'g{i % n_groups}', scan_id=sid)
            store.add_groups(sid, groups)
            store.add_items(sid, items())
            store.finish_scan(sid, 'done', 1, 1, 1)
            spec = Spec(scan_id=sid, version=1, filter_min_age_days=14, groups={g.key: GroupSel() for g in groups[:300]})
            best = None
            for _ in range(3):
                t0 = time.perf_counter()
                res = selection.resolve_for(store, spec, NOW, ('agent',), True)
                took = time.perf_counter() - t0
                best = took if best is None else min(best, took)
            print(f'\nresolve({self.N} items): best of 3 = {best:.3f}s')
            self.assertEqual(res.summary['total'], self.N)
            self.assertGreater(res.summary['count'], 0)
            self.assertLess(best, self.BUDGET)


if __name__ == '__main__':
    unittest.main()
