"""queries.quick_totals must give exactly what selection.resolve_for gives for the "every junk group" spec: the
Insights page offers the second's number and links to the page that shows it."""
from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path

from disk_steward import queries
from disk_steward.model import Action, AgeBasis, Domain, GroupSel, Spec, Tier, Unit
from disk_steward.selection import resolve_for
from disk_steward.store import Store
from m3_support import DAY, NOW, mk_group, mk_item, ns_ago

ALL_DOMAINS = ('agent', 'app-library', 'user-files', 'dev-shared')
ACTIONS = ((), (Action.DELETE,), (Action.TRASH, Action.DELETE), (Action.ARCHIVE, Action.DELETE),
           (Action.DELETE, Action.ARCHIVE))


def probe_totals(store, scan_id, now, days, domains, trash):
    spec = Spec(scan_id=scan_id, filter_min_age_days=days, groups={
        g.key: GroupSel() for g in store.groups(scan_id)
        if g.tier == Tier.JUNK and g.actions and g.domain.value in domains})
    res = resolve_for(store, spec, now, domains, trash)
    return {k: (v['selected'], v['alloc']) for k, v in res.summary['groups'].items() if v['selected']}


class QuickTotalsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 's.db')
        self.addCleanup(self.store.close)

    def begin(self):
        return self.store.begin_scan('c', 'f', 's', ['agent'])

    def finish(self, sid, groups, items):
        self.store.add_groups(sid, groups)
        self.store.add_items(sid, items)
        self.store.finish_scan(sid, 'done', 10 ** 9, 10 ** 8, 1)
        return sid

    def check(self, sid, now=NOW, days=14, domains=('agent', 'app-library'), trash=True):
        want = probe_totals(self.store, sid, now, days, domains, trash)
        got = queries.quick_totals(self.store, sid, now, days, domains, trash)
        self.assertEqual(got, want, f'now={now} days={days} domains={domains} trash={trash}')
        return got

    def one_group(self, items, **group):
        sid = self.begin()
        return self.finish(sid, [mk_group('g', scan_id=sid, **group)],
                           [mk_item(k, a, 'g', scan_id=sid, **kw) for k, a, kw in items])

    def test_the_age_filter_and_the_rules_floor(self):
        sid = self.one_group([('old', 20, {}), ('mid', 10, {}), ('new', 0.5, {})], min_age_floor_days=1.0)
        self.assertEqual(self.check(sid, days=14)['g'][0], 1)
        self.assertEqual(self.check(sid, days=5)['g'][0], 2)
        self.assertEqual(self.check(sid, days=0)['g'][0], 2)          # the floor holds even with no filter
        self.assertEqual(self.check(sid, days=100), {})

    def test_a_rule_default_can_only_make_the_filter_stricter(self):
        sid = self.one_group([('a', 20, {}), ('b', 40, {})], default_min_age_days=30.0)
        self.assertEqual(self.check(sid, days=14)['g'][0], 1)
        self.assertEqual(self.check(sid, days=60), {})

    def test_exactly_on_the_threshold_matches(self):
        sid = self.one_group([(f'k{i}', 14 + i * 1e-9, {}) for i in range(-5, 6)])
        self.check(sid, days=14)

    def test_taint_protection_hard_links_and_the_wrong_area_are_never_selected(self):
        sid = self.one_group([('plain', 30, {}), ('tainted', 30, {'taint': 'sqlite'}),
                              ('prot', 30, {'tier': Tier.PROTECTED}), ('linked', 30, {'nlink': 2}),
                              ('linked_dir', 30, {'nlink': 2, 'unit': Unit.BUNDLE}),
                              ('elsewhere', 30, {'domain': Domain.USER_FILES})])
        self.assertEqual(self.check(sid)['g'][0], 2)                   # plain, and the linked folder
        self.assertEqual(self.check(sid, domains=('agent', 'app-library', 'user-files'))['g'][0], 3)

    def test_trash_needs_a_trash_and_an_unsupported_default_selects_nothing(self):
        trash = self.one_group([('a', 30, {})], actions=(Action.TRASH, Action.DELETE), default_action=Action.TRASH)
        self.assertEqual(self.check(trash, trash=True)['g'][0], 1)
        self.assertEqual(self.check(trash, trash=False), {})
        none = self.one_group([('a', 30, {})], actions=(Action.DELETE,), default_action=None)
        self.assertEqual(self.check(none), {})
        odd = self.one_group([('a', 30, {})], actions=(Action.DELETE,), default_action=Action.ARCHIVE)
        self.assertEqual(self.check(odd), {})

    def test_last_used_beats_the_change_time_only_for_the_groups_that_ask_for_it(self):
        for basis in (AgeBasis.LAST_USED, AgeBasis.CHANGED):
            with self.subTest(basis=basis):
                sid = self.one_group([('used', 40, {'last_used_ns': ns_ago(2)}), ('unknown', 40, {}),
                                      ('older', 3, {'last_used_ns': ns_ago(90)})], age_basis=basis)
                got = self.check(sid, days=14)
                self.assertEqual(got.get('g', (0,))[0], 1 if basis == AgeBasis.LAST_USED else 2)

    def test_only_junk_groups_with_actions_in_enabled_areas_count(self):
        sid = self.begin()
        groups = [mk_group('junk', scan_id=sid), mk_group('review', scan_id=sid, tier=Tier.REVIEW),
                  mk_group('prot', scan_id=sid, tier=Tier.PROTECTED, actions=(), default_action=None),
                  mk_group('report', scan_id=sid, actions=(), default_action=None),
                  mk_group('user', scan_id=sid, domain=Domain.USER_FILES)]
        items = [mk_item(f'{g.key}-i', 30, g.key, scan_id=sid, domain=g.domain) for g in groups]
        self.finish(sid, groups, items)
        self.assertEqual(set(self.check(sid)), {'junk'})
        self.assertEqual(set(self.check(sid, domains=('user-files',))), {'user'})

    def test_random_stores_agree(self):
        rng = random.Random(20260429)
        for round_no in range(12):
            sid = self.begin()
            groups, items = [], []
            for n in range(25):
                key = f'g{n}'
                domain = rng.choice(ALL_DOMAINS)
                actions = rng.choice(ACTIONS)
                default = rng.choice(actions + (None,)) if actions else None
                groups.append(mk_group(
                    key, scan_id=sid, tier=rng.choice((Tier.JUNK, Tier.JUNK, Tier.JUNK, Tier.REVIEW, Tier.PROTECTED)),
                    domain=Domain(domain), actions=actions, default_action=default,
                    min_age_floor_days=rng.choice((0.0, 0.0, 1.0, 7.0)),
                    default_min_age_days=rng.choice((None, None, 30.0, 3.5)),
                    age_basis=rng.choice((AgeBasis.CHANGED, AgeBasis.LAST_USED))))
                for i in range(rng.randint(1, 30)):
                    kw = {}
                    if rng.random() < 0.1:
                        kw['taint'] = rng.choice(('sqlite', 'git'))
                    if rng.random() < 0.05:
                        kw['tier'] = Tier.PROTECTED
                    if rng.random() < 0.05:
                        kw['domain'] = Domain(rng.choice(ALL_DOMAINS))
                    if rng.random() < 0.15:
                        kw['nlink'] = 2
                    if rng.random() < 0.3:
                        kw['unit'] = Unit.BUNDLE
                    if rng.random() < 0.4:
                        kw['last_used_ns'] = ns_ago(rng.choice((0.1, 3, 14, 60, 200)))
                    items.append(mk_item(f'{key}-{i}', rng.choice((0.2, 1, 3.5, 7, 14, 30, 30.0000001, 90, 400)), key,
                                         scan_id=sid, alloc=rng.randint(1, 10 ** 7), reclaim=1, **kw))
            self.finish(sid, groups, items)
            for days in (0, 1, 3.5, 14, 30, 365):
                for domains in (('agent', 'app-library'), ALL_DOMAINS, ('user-files',)):
                    for trash in (True, False):
                        self.check(sid, now=NOW + rng.choice((0, 3600, 7 * DAY)), days=days, domains=domains,
                                   trash=trash)


if __name__ == '__main__':
    unittest.main()
