from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from advisor_support import AdvisorCase
from disk_steward import i18n
from disk_steward.advisor import payload as pl
from disk_steward.catalog import load_catalog
from disk_steward.model import Action, AgeBasis, Domain, Tier
from disk_steward.store import Store
from m3_support import NOW, mk_group, mk_item, ns_ago

ALLOWED_GROUP_KEYS = {'token', 'domain', 'tier', 'kind', 'provider', 'label_key', 'actions', 'has_hint', 'items',
                      'alloc', 'reclaim', 'ages', 'growth', 'evidence', 'protected'}
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


def segments(root: Path):
    for here, dirs, files in os.walk(root):
        for name in dirs + files:
            yield name


class PayloadPrivacyTests(AdvisorCase):
    def build(self):
        return pl.build_payload(self.app.store, self.app.latest_scan()['id'], self.fh.clock())

    def test_no_path_name_or_identifier_reaches_the_payload(self):
        built = self.build()
        text = built.json
        groups = self.app.store.groups(built.scan_id)
        self.assertTrue(any(g.label_args for g in groups), 'the fixture must hold depth-grouped groups')
        for g in groups:
            for value in g.label_args.values():
                if '/' in str(value):   # 'agent' and 'duplicates' are catalog words, the rest are paths
                    self.assertNotIn(str(value), text)
            suffix = g.key.partition(':')[2]   # the part of a group key that comes from the disk
            if '/' in suffix:
                self.assertNotIn(suffix, text)
        for fragment in ('/Users', '~/', '$TMPDIR', '$CACHEDIR', 'com.', str(self.fh.root), 'ds-test-', '/secret',
                         '-Users-me-proj', 'pluginA', 'local_abc', 'rollout', 'session_index', 'MEMORY'):
            self.assertNotIn(fragment, text)
        self.assertIsNone(UUID.search(text))
        # every folder and file name of the fixture, unless it is plain catalog vocabulary
        words = ' '.join(i18n.catalog('en'))
        for rule in load_catalog(self.app.settings).rules:
            words += f' {rule.id} {rule.provider} {rule.kind.value}'
        vocabulary = set(re.split(r'[^A-Za-z0-9_-]+', words)) | set(re.split(r'[^A-Za-z0-9]+', words))
        names = {n.lstrip('.') for n in segments(self.fh.home)}
        without_tokens = re.sub(r'g[0-9a-f]{8}', '', text)   # random hex can spell short names
        leaked = sorted(n for n in names if len(n) >= 3 and n not in vocabulary and n in without_tokens)
        self.assertEqual(leaked, [])

    def test_group_rows_use_an_allowlist_of_fields(self):
        built = self.build()
        self.assertTrue(built.payload['groups'])
        for row in built.payload['groups']:
            self.assertEqual(set(row), ALLOWED_GROUP_KEYS)
            self.assertRegex(row['token'], r'^g[0-9a-f]{8}$')
            self.assertNotRegex(row['label_key'], r'[~/ ]')
            self.assertIn(row['label_key'], i18n.catalog('en'))
            self.assertTrue(all(k.startswith('evidence.') for k in row['evidence']))
            self.assertEqual(set(row['ages']), set(pl.AGE_NAMES))
        self.assertEqual(set(built.payload), {'format', 'coverage', 'scan', 'groups'})
        self.assertEqual(set(built.payload['scan']), {'domains', 'totals', 'volume', 'previous_scan_age_days'})

    def test_depth_grouped_and_own_label_keys(self):
        keys = {r['label_key'] for r in self.build().payload['groups']}
        self.assertIn('rule.claude.projects.label', keys)
        self.assertIn('group.codex.sessions:duplicates.label', keys)   # a static catalog key, not the group's path

    def test_protected_flag_covers_protected_variants(self):
        built = self.build()
        by_key = {t['key']: t for t in built.tokens.values()}
        self.assertTrue(any('#protected' in k for k in by_key))
        for token, info in built.tokens.items():
            row = next(r for r in built.payload['groups'] if r['token'] == token)
            self.assertEqual(row['protected'], info['protected'])
            if '#protected' in info['key'] or row['tier'] == 'protected':
                self.assertTrue(row['protected'])

    def test_tokens_are_fresh_per_request(self):
        a, b = self.build(), self.build()
        self.assertEqual(len(a.tokens), len(b.tokens))
        self.assertFalse(set(a.tokens) & set(b.tokens))

    def test_the_payload_is_what_json_says_and_stays_small(self):
        built = self.build()
        self.assertEqual(json.loads(built.json), built.payload)
        self.assertLessEqual(len(built.json.encode('utf-8')), pl.MAX_BYTES)
        self.assertEqual(built.coverage['groups_included'], built.coverage['groups_total'])
        self.assertEqual(built.coverage['alloc_included'], built.coverage['alloc_total'])

    def test_totals_and_volume_are_plain_numbers(self):
        scan = self.build().payload['scan']
        self.assertEqual(scan['volume'], {'total': 500 * 10 ** 9, 'used': 200 * 10 ** 9, 'free': 300 * 10 ** 9})
        self.assertEqual(sum(v['alloc'] for v in scan['totals']['by_tier'].values()),
                         sum(g.alloc for g in self.app.store.groups(self.app.latest_scan()['id'])))
        self.assertEqual(scan['domains'], ['agent'])


class GrowthTests(AdvisorCase):
    def test_no_previous_scan_means_no_growth_figure(self):
        built = pl.build_payload(self.app.store, self.app.latest_scan()['id'], self.fh.clock())
        self.assertIsNone(built.payload['scan']['previous_scan_age_days'])
        self.assertTrue(all(r['growth'] is None for r in built.payload['groups']))

    def test_growth_against_the_previous_scan(self):
        self.fh.clock.advance(days=3)
        self.fh.file('~/.codex/log/more.log', size=8192, age_days=10)
        self.app.scan()
        built = pl.build_payload(self.app.store, self.app.latest_scan()['id'], self.fh.clock())
        self.assertEqual(built.payload['scan']['previous_scan_age_days'], 3.0)
        rows = {built.tokens[r['token']]['key']: r for r in built.payload['groups']}
        self.assertEqual(rows['codex.logs']['growth'], {'delta_alloc': 8192, 'new': False})
        self.assertEqual(rows['claude.logs']['growth'], {'delta_alloc': 0, 'new': False})

    def test_a_group_that_was_not_there_before_is_new(self):
        self.fh.file('~/.cache/codex-runtimes/codex-runtime-install-new1/payload/y.bin', size=100, age_days=10)
        self.fh.dir('~/.cache/codex-runtimes/codex-runtime-install-new1', age_days=10)
        self.fh.clock.advance(days=1)
        self.app.scan()
        built = pl.build_payload(self.app.store, self.app.latest_scan()['id'], self.fh.clock())
        growth = [r['growth'] for r in built.payload['groups']]
        self.assertTrue(all(g is not None for g in growth))
        self.assertTrue(all(isinstance(g['new'], bool) for g in growth))


class StoreCase(unittest.TestCase):
    """A hand-made scan, for caps and age buckets."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 's.db')
        self.addCleanup(self.store.close)

    def scan(self, groups, items):
        sid = self.store.begin_scan('c', 'f', 's', ['agent'])
        self.store.add_groups(sid, groups(sid))
        self.store.add_items(sid, items(sid))
        self.store.finish_scan(sid, 'done', 10 ** 9, 5 * 10 ** 8, 0)
        return sid


class AgeBucketTests(StoreCase):
    def test_boundaries_and_basis(self):
        ages = (0, 6.9, 7.0, 13.9, 14.0, 29.9, 30.0, 89.9, 90.0, 179.9, 180.0, 400)

        def groups(sid):
            return [mk_group('g.changed', scan_id=sid, items=len(ages)),
                    mk_group('g.used', scan_id=sid, age_basis=AgeBasis.LAST_USED, items=1)]

        def items(sid):
            rows = [mk_item(f'k{i}', age, 'g.changed', scan_id=sid, alloc=100 + i) for i, age in enumerate(ages)]
            # an old file that was used yesterday counts as young under LAST_USED, as selection does
            rows.append(mk_item('used', 200, 'g.used', scan_id=sid, alloc=5, last_used_ns=ns_ago(1)))
            return rows

        sid = self.scan(groups, items)
        built = pl.build_payload(self.store, sid, NOW)
        rows = {built.tokens[r['token']]['key']: r for r in built.payload['groups']}
        self.assertEqual([rows['g.changed']['ages'][n][0] for n in pl.AGE_NAMES], [2, 2, 2, 2, 2, 2])
        self.assertEqual(rows['g.changed']['ages']['lt7'], [2, 100 + 101])
        self.assertEqual([rows['g.used']['ages'][n][0] for n in pl.AGE_NAMES], [1, 0, 0, 0, 0, 0])


class CapTests(StoreCase):
    def test_the_cap_keeps_the_largest_groups_and_reports_coverage(self):
        count = 700

        def groups(sid):
            return [mk_group(f'g.{i:04d}', scan_id=sid, alloc=1000 + i * 7, reclaim=1000 + i * 7,
                             label_args={'path': f'~/very/private/path/{i}'}) for i in range(count)]

        def items(sid):
            return [mk_item(f'i{i}', 20, f'g.{i:04d}', scan_id=sid, alloc=1000 + i * 7) for i in range(count)]

        sid = self.scan(groups, items)
        built = pl.build_payload(self.store, sid, NOW)
        self.assertLessEqual(len(built.json.encode('utf-8')), pl.MAX_BYTES)
        self.assertGreater(len(built.json.encode('utf-8')), pl.MAX_BYTES - 1500)
        cov = built.coverage
        self.assertEqual(cov['groups_total'], count)
        self.assertLess(cov['groups_included'], count)
        self.assertEqual(cov['groups_included'], len(built.payload['groups']))
        allocs = [r['alloc'] for r in built.payload['groups']]
        self.assertEqual(allocs, sorted(allocs, reverse=True))
        self.assertEqual(allocs[0], 1000 + (count - 1) * 7)
        self.assertEqual(cov['alloc_included'], sum(allocs))
        self.assertEqual(cov['alloc_total'], sum(1000 + i * 7 for i in range(count)))
        self.assertEqual(built.payload['coverage'], cov)
        self.assertEqual(len(built.tokens), cov['groups_included'])
        self.assertNotIn('very/private', built.json)
        # totals still describe the whole scan, not just the included groups
        self.assertEqual(built.payload['scan']['totals']['by_domain']['agent']['groups'], count)

    def test_protected_default_tier_group_flag(self):
        def groups(sid):
            return [mk_group('g.p', scan_id=sid, tier=Tier.PROTECTED, actions=(), default_action=None),
                    mk_group('g.j', scan_id=sid, domain=Domain.AGENT, actions=(Action.DELETE,))]

        sid = self.scan(groups, lambda sid: [mk_item('a', 20, 'g.p', scan_id=sid, tier=Tier.PROTECTED),
                                             mk_item('b', 20, 'g.j', scan_id=sid)])
        built = pl.build_payload(self.store, sid, NOW)
        flags = {built.tokens[r['token']]['key']: r['protected'] for r in built.payload['groups']}
        self.assertEqual(flags, {'g.p': True, 'g.j': False})


if __name__ == '__main__':
    unittest.main()
