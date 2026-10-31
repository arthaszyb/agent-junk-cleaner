"""The "safe to clean" selection, Browse's filters, the plan lifecycle and the cleanup list, over HTTP."""
from __future__ import annotations

import contextlib
import io
import json
import os
import threading
import unittest
from urllib.parse import urlencode

from disk_steward.cli import main as cli_main
from disk_steward.portal.api import overview_filters
from disk_steward.report import REASONS

from test_portal_server import JUNK_14D, PortalCase


class QuickCase(PortalCase):
    def setUp(self):
        super().setUp()
        self.scan()

    def quick(self, days=None):
        return self.ok(self.c.get('/api/quick' + ('' if days is None else f'?days={days}')))

    def expected(self, q):
        return {'scan_id': q['scan_id'], 'spec_version': q['spec']['version'], 'digest': q['digest'],
                'resolved_at': q['resolved_at']}

    def quick_plan(self, q, days=None):
        return self.c.post('/api/quick/plan', {'days': q['days'] if days is None else days, 'expected': self.expected(q)})

    def cli_dry_run(self, days):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli_main(['plan', '--select', 'junk', '--older-than', f'{days}d', '--dry-run', '--json'],
                                      app_factory=lambda: self.app), 0)
        return json.loads(out.getvalue())['summary']


class QuickTests(QuickCase):
    def test_the_numbers_are_those_of_a_junk_plan_at_that_age(self):
        self.assertEqual(self.quick(1000)['summary']['count'], 0)   # (the CLI refuses an empty plan, so no comparison)
        for days in (14, 0, 3, 30):
            with self.subTest(days=days):
                q = self.quick(days)
                cli = self.cli_dry_run(days)
                self.assertEqual((q['summary']['count'], q['summary']['alloc'], q['summary']['reclaim']),
                                 (cli['count'], cli['alloc'], cli['reclaim']))
                self.assertEqual(q['summary']['by_action'], cli['by_action'])
                self.assertEqual(q['days'], days)

    def test_the_default_age_is_the_settings(self):
        q = self.quick()
        self.assertEqual((q['days'], q['default_days']), (14, 14))
        self.assertEqual(q['summary']['count'], 3)
        self.app.update_settings({'default_min_age_days': 0})
        self.assertEqual(self.quick()['days'], 0)
        self.assertEqual(self.quick()['summary']['count'], self.quick(0)['summary']['count'])

    def test_the_overview_says_the_same_at_its_age(self):
        ov = self.ok(self.c.get('/api/overview'))
        q = self.quick(ov['offered']['min_age_days'])
        self.assertEqual((q['summary']['count'], q['summary']['alloc']), (ov['offered']['items'], ov['offered']['alloc']))
        scope = q['summary']['scope_by_state']
        self.assertEqual(scope['too_recent'], {'count': ov['not_included']['too_recent']['items'],
                                               'alloc': ov['not_included']['too_recent']['alloc']})

    def test_asking_again_changes_nothing_and_the_users_selection_is_never_touched(self):
        mine = self.select([{'op': 'select_group', 'group': 'claude.projects:~/.claude/projects/-Users-me-proj', 'on': True},
                            {'op': 'set_filter', 'min_age_days': 7}])
        a = self.quick(14)
        b = self.quick(14)
        self.assertEqual((a['digest'], a['spec']['version']), (b['digest'], b['spec']['version']))
        c = self.quick(3)
        self.assertNotEqual(c['digest'], a['digest'])
        self.assertEqual(self.quick(3)['digest'], c['digest'])
        after = self.ok(self.c.get('/api/spec'))
        self.assertEqual((after['spec'], after['digest']), (mine['spec'], mine['digest']))
        self.assertEqual(list(after['spec']['groups']), ['claude.projects:~/.claude/projects/-Users-me-proj'])

    def test_bad_ages(self):
        for days in ('-1', 'nan', 'inf', 'x', ''):
            with self.subTest(days=days):
                status, body = self.c.get(f'/api/quick?days={days}')
                if days == '':
                    self.assertEqual(status, 200)   # an empty value is no value: the default
                else:
                    self.error((status, body), 400, 'INVALID')

    def test_quick_needs_a_scan(self):
        from disk_steward.portal.server import PortalServer
        from portal_support import Client, make_portal_app
        from support import FakeHome
        with FakeHome() as fh:
            app = make_portal_app(fh)
            server = PortalServer(app, 0)
            server.start()
            try:
                self.error(Client(server).login().get('/api/quick'), 409, 'NO_SCAN')
            finally:
                server.stop()
                app.close()


class QuickPlanTests(QuickCase):
    def test_the_plan_is_exactly_what_was_shown(self):
        q = self.quick(14)
        status, plan = self.quick_plan(q)
        self.assertEqual(status, 200)
        self.assertEqual((plan['totals']['count'], plan['totals']['alloc']), (q['summary']['count'], q['summary']['alloc']))
        self.assertEqual(plan['entries']['total'], q['summary']['count'])
        self.assertEqual(plan['state'], 'draft')
        detail = self.ok(self.c.get(f'/api/plans/{plan["id"]}'))
        self.assertEqual((detail['pending']['count'], detail['pending']['alloc']), (q['summary']['count'], q['summary']['alloc']))
        # and it is the plan the CLI would make
        self.assertEqual(self.cli_dry_run(14)['count'], plan['totals']['count'])

    def test_a_plan_at_another_age_than_the_numbers_shown_is_refused(self):
        q = self.quick(14)
        status, body = self.quick_plan(q, days=3)
        err = self.error((status, body), 409, 'DIGEST_MISMATCH')
        self.assertEqual(err['message'], 'The selection changed since you last looked at it. Review it again.')
        self.assertEqual(self.quick(3)['summary']['count'], self.cli_dry_run(3)['count'])

    def test_a_plan_after_the_selection_moved_on_is_refused_with_what_changed(self):
        q = self.quick(14)
        self.quick(3)   # another tab changed the age
        self.error(self.quick_plan(q), 409, 'DIGEST_MISMATCH')
        q = self.quick(14)
        self.fh.clock.advance(days=10)   # files came of age after the numbers were shown
        err = self.error(self.quick_plan(q), 409, 'DIGEST_MISMATCH')
        self.assertGreater(err['details']['added_count'], 0)
        self.assertEqual(err['details']['removed_count'], 0)
        fresh = self.quick(14)   # the numbers shown now are the ones that plan
        self.assertGreater(fresh['summary']['count'], q['summary']['count'])
        status, plan = self.quick_plan(fresh)
        self.assertEqual((status, plan['totals']['count']), (200, fresh['summary']['count']))

    def test_nothing_to_plan_is_a_422(self):
        q = self.quick(100000)
        self.assertEqual(q['summary']['count'], 0)
        err = self.error(self.quick_plan(q), 422, 'EMPTY_PLAN')
        self.assertIn('too_recent', err['details']['breakdown'])

    def test_a_rescan_makes_the_numbers_stale(self):
        q = self.quick(14)
        self.app.scan()
        self.error(self.quick_plan(q), 400, 'INVALID')   # error.spec_stale
        self.assertEqual(self.quick(14)['scan_id'], 2)

    def test_bad_requests(self):
        q = self.quick(14)
        self.error(self.c.post('/api/quick/plan', {'expected': self.expected(q)}), 400, 'INVALID')
        self.error(self.c.post('/api/quick/plan', {'days': True, 'expected': self.expected(q)}), 400, 'INVALID')
        self.error(self.c.post('/api/quick/plan', {'days': 14, 'expected': 'x'}), 400, 'INVALID')
        self.error(self.c.post('/api/quick/plan', {'days': -1, 'expected': self.expected(q)}), 400, 'INVALID')


class FilterTests(QuickCase):
    def rows(self, ov):
        ni = ov['not_included']
        reasons = {r: ni['protected']['by_reason'][r] for r in ni['protected']['by_reason']}
        rows = {'offered': ov['offered'], 'too_recent': ni['too_recent'], 'floor_held': ni['floor_held'],
                'review': ni['review'], 'report_only': ni['report_only'], 'other': ni['other'], 'protected': ni['protected']}
        return rows, reasons

    def scope(self, flt):
        body = self.ok(self.c.get('/api/groups?' + urlencode(flt)))
        return body

    def test_every_overview_number_has_a_filter_that_shows_exactly_it(self):
        ov = self.ok(self.c.get('/api/overview'))
        self.assertEqual(ov['filters'], overview_filters(ov['offered']['min_age_days']))
        rows, reasons = self.rows(ov)
        self.assertGreater(len(reasons), 4)
        for name, bucket in rows.items():
            with self.subTest(row=name):
                body = self.scope(ov['filters']['rows'][name])
                self.assertEqual((body['scope']['items'], body['scope']['alloc']), (bucket['items'], bucket['alloc']), name)
                self.assertEqual(body['scope']['groups'], len(body['groups']))
                self.assertEqual(sum(g['match']['items'] for g in body['groups']), bucket['items'])
        for reason, bucket in reasons.items():
            with self.subTest(reason=reason):
                body = self.scope(ov['filters']['reasons'][reason])
                self.assertEqual((body['scope']['items'], body['scope']['alloc']), (bucket['items'], bucket['alloc']), reason)
        for tier, totals in ov['totals']['by_tier'].items():
            with self.subTest(tier=tier):
                body = self.scope(ov['filters']['tier'][tier])
                self.assertEqual((body['scope']['items'], body['scope']['alloc']), (totals['items'], totals['alloc']))
                self.assertEqual(len(body['groups']), totals['groups'])

    def test_the_overview_filters_hold_at_another_age(self):
        days = 3
        q = self.quick(days)
        ov = self.ok(self.c.get('/api/overview'))
        flt = dict(ov['filters']['rows']['offered'], days=days)
        body = self.scope(flt)
        self.assertEqual((body['scope']['items'], body['scope']['alloc']), (q['summary']['count'], q['summary']['alloc']))
        self.assertEqual(body['filter']['days'], days)
        self.assertTrue(body['preview'])
        too_recent = self.scope(dict(ov['filters']['rows']['too_recent'], days=days))
        self.assertEqual(too_recent['scope']['items'], q['summary']['scope_by_state']['too_recent']['count'])

    def test_menu_counts_are_the_same_buckets(self):
        ov = self.ok(self.c.get('/api/overview'))
        counts = self.ok(self.c.get('/api/groups?counts=1'))['counts']
        rows, reasons = self.rows(ov)
        for state, row in (('selected', 'offered'), ('too_recent', 'too_recent'), ('too_recent_floor', 'floor_held'),
                           ('other', 'other')):
            self.assertEqual(counts['states'][state], {'items': rows[row]['items'], 'alloc': rows[row]['alloc']}, state)
        self.assertEqual(counts['report_only'], {'items': rows['report_only']['items'], 'alloc': rows['report_only']['alloc']})
        self.assertEqual({k: (v['items'], v['alloc']) for k, v in counts['reasons'].items()},
                         {k: (v['items'], v['alloc']) for k, v in reasons.items()})
        self.assertEqual(set(counts['reasons']) - set(REASONS), set())
        self.assertEqual(counts['days'], 14)

    def test_groups_carry_what_matched_and_what_the_quick_selection_makes_of_them(self):
        body = self.scope({'tier': 'junk', 'state': 'too_recent', 'days': 14})
        self.assertTrue(body['groups'])
        for g in body['groups']:
            self.assertEqual(g['tier'], 'junk')
            self.assertEqual(g['match']['items'], g['selection']['too_recent'])    # the quick selection's own counters
            self.assertIsNotNone(g['selector'])                                     # every junk group is selected there
        offered = self.scope({'tier': 'junk', 'state': 'selected', 'days': 14})
        self.assertEqual({g['key'] for g in offered['groups']},
                         {k for k, v in self.quick(14)['summary']['groups'].items() if v['selected']})

    def test_filtered_items_are_the_ones_counted(self):
        for flt in ({'tier': 'junk', 'state': 'too_recent'}, {'tier': 'junk', 'state': 'selected'}, {'tier': 'junk', 'state': 'too_recent_floor'},
                    {'tier': 'protected', 'reason': 'databases'}, {'tier': 'protected', 'reason': 'user_content'}):
            body = self.scope(dict(flt, days=14))
            for g in body['groups']:
                with self.subTest(flt=flt, group=g['key']):
                    page = self.ok(self.c.get('/api/items?' + urlencode(dict(flt, group=g['key'], days=14, limit=500))))
                    self.assertEqual(page['total'], g['match']['items'])
                    self.assertEqual(len(page['rows']), g['match']['items'])
                    self.assertEqual(sum(r['alloc'] for r in page['rows']), g['match']['alloc'])
                    if 'state' in flt:
                        self.assertEqual({r['decision']['state'] for r in page['rows']}, {flt['state']})
                        self.assertTrue(all(r['decision']['text'] for r in page['rows']))
        self.error(self.c.get('/api/items?state=too_recent'), 400, 'INVALID')   # which group?
        self.error(self.c.get('/api/items?group=claude.logs&state=selected&order=path'), 400, 'INVALID')

    def test_an_item_page_can_be_paged_and_ordered(self):
        group = 'claude.projects:~/.claude/projects/-Users-me-proj'
        full = self.ok(self.c.get('/api/items?' + urlencode({'group': group, 'tier': 'review', 'state': 'unselected', 'limit': 10})))
        self.assertEqual(full['total'], 3)
        one = self.ok(self.c.get('/api/items?' + urlencode({'group': group, 'state': 'unselected', 'limit': 1, 'offset': 2})))
        self.assertEqual([r['key'] for r in one['rows']], [full['rows'][2]['key']])
        oldest = self.ok(self.c.get('/api/items?' + urlencode({'group': group, 'state': 'unselected', 'order': 'oldest'})))
        self.assertEqual(sorted(r['key'] for r in oldest['rows']), sorted(r['key'] for r in full['rows']))

    def test_the_plain_filters(self):
        every = {g['key']: g for g in self.ok(self.c.get('/api/groups'))['groups']}
        for flt, check in (({'tier': 'review'}, lambda g: g['tier'] == 'review'),
                           ({'report_only': '1'}, lambda g: not g['actions'] and g['tier'] != 'protected'),
                           ({'report_only': '0', 'tier': 'review'}, lambda g: g['actions'] and g['tier'] == 'review'),
                           ({'domain': 'agent', 'tier': 'junk'}, lambda g: g['domain'] == 'agent' and g['tier'] == 'junk')):
            with self.subTest(flt=flt):
                body = self.scope(flt)
                self.assertEqual({g['key'] for g in body['groups']}, {k for k, g in every.items() if check(g)})
                self.assertFalse(body['preview'])
                self.assertEqual(body['scope']['items'], sum(g['items'] for g in body['groups']))
        for bad in ({'tier': 'gold'}, {'domain': 'mars'}, {'report_only': '2'}, {'days': 'x', 'state': 'selected'}):
            with self.subTest(bad=bad):
                self.error(self.c.get('/api/groups?' + urlencode(bad)), 400, 'INVALID')

    def test_a_state_filter_does_not_touch_the_users_selection(self):
        mine = self.select(JUNK_14D)
        self.scope({'tier': 'junk', 'state': 'too_recent', 'days': 3})
        self.assertEqual(self.ok(self.c.get('/api/spec'))['digest'], mine['digest'])
        plain = self.ok(self.c.get('/api/groups'))['groups']
        self.assertEqual(sum(g['selection']['selected'] for g in plain), mine['summary']['count'])   # 14 days, not 3

    def test_filters_and_a_scan_between_requests_are_consistent(self):
        before = self.scope({'tier': 'junk', 'state': 'selected', 'days': 14})['scope']
        self.app.scan()
        after = self.scope({'tier': 'junk', 'state': 'selected', 'days': 14})['scope']
        self.assertEqual(before, after)   # nothing changed on disk


class LifecycleTests(QuickCase):
    def plan(self, days=14):
        status, plan = self.quick_plan(self.quick(days))
        self.assertEqual(status, 200, plan)
        return plan

    def test_a_new_scan_invalidates_drafts_of_older_scans_and_drops_their_entries(self):
        plan = self.plan()
        other = self.make_plan()
        self.assertEqual(self.ok(self.c.get(f'/api/plans/{plan["id"]}'))['entries']['total'], 3)
        self.app.scan()
        for p in (plan, other):
            gone = self.ok(self.c.get(f'/api/plans/{p["id"]}'))
            self.assertEqual((gone['state'], gone['error'], gone['entries']['total']), ('invalid', 'superseded', 0))
        self.assertEqual(self.ok(self.c.get(f'/api/plans/{plan["id"]}/entries'))['total'], 0)
        self.assertEqual(self.ok(self.c.get(f'/api/plans/{plan["id"]}/groups'))['groups'], [])
        self.assertIsNone(self.ok(self.c.get('/api/plans'))['draft'])
        self.error(self.c.post(f'/api/plans/{plan["id"]}/run', {'revision': 1, 'entries_digest': plan['entries_digest']}), 400, 'INVALID')

    def test_other_plans_survive_a_scan(self):
        plan = self.plan()
        self.ok(self.c.post(f'/api/plans/{plan["id"]}/run', {'revision': 1, 'entries_digest': plan['entries_digest']}))
        self.wait_for(lambda: self.c.get('/api/job')[1]['job']['status'] == 'done')   # the run rescans: scan 2
        done = self.ok(self.c.get(f'/api/plans/{plan["id"]}'))
        self.assertEqual((done['state'], done['entries']['by_state']), ('done', {'done': 3}))
        self.assertIsNotNone(done['result'])

    def test_a_draft_can_be_discarded_and_nothing_else(self):
        plan = self.plan()
        self.assertEqual(self.ok(self.c.post(f'/api/plans/{plan["id"]}/discard')), {'discarded': True})
        gone = self.ok(self.c.get(f'/api/plans/{plan["id"]}'))
        self.assertEqual((gone['state'], gone['error'], gone['entries']['total']), ('invalid', 'discarded', 0))
        err = self.error(self.c.post(f'/api/plans/{plan["id"]}/discard'), 400, 'INVALID')
        self.assertEqual(err['details'], {'state': 'invalid'})
        self.error(self.c.post('/api/plans/nope/discard'), 404, 'NOT_FOUND')
        ran = self.plan()
        self.ok(self.c.post(f'/api/plans/{ran["id"]}/run', {'revision': 1, 'entries_digest': ran['entries_digest']}))
        self.wait_for(lambda: self.c.get('/api/job')[1]['job']['status'] == 'done')
        self.error(self.c.post(f'/api/plans/{ran["id"]}/discard'), 400, 'INVALID')
        self.assertEqual(self.ok(self.c.get(f'/api/plans/{ran["id"]}'))['entries']['total'], 3)

    def test_the_cleanup_list(self):
        empty = self.ok(self.c.get('/api/plans'))
        self.assertEqual((empty['active'], empty['draft'], empty['finished']), (None, None, []))
        old = self.plan()
        self.fh.clock.advance(seconds=1)
        newer = self.plan(0)   # a second draft of the same scan
        listed = self.ok(self.c.get('/api/plans'))
        self.assertEqual(listed['draft']['id'], newer['id'])    # the newest one, never an older one
        self.assertEqual(listed['draft']['pending']['count'], newer['totals']['count'])
        self.assertEqual(listed['draft']['pending']['alloc'], newer['totals']['alloc'])
        self.fh.clock.advance(days=2)   # drafts expire
        self.assertIsNone(self.ok(self.c.get('/api/plans'))['draft'])
        self.fh.clock.advance(days=-2)
        self.ok(self.c.post(f'/api/plans/{old["id"]}/discard'))
        self.ok(self.c.post(f'/api/plans/{newer["id"]}/discard'))
        ids = []
        for i in range(7):
            self.fh.clock.advance(seconds=60)   # the list is ordered by the time a plan was made
            self.fh.file(f'~/.claude/debug/x{i}.txt', size=100, age_days=30)
            self.app.scan()
            plan = self.plan(14)
            self.ok(self.c.post(f'/api/plans/{plan["id"]}/run', {'revision': 1, 'entries_digest': plan['entries_digest']}))
            self.wait_for(lambda: self.c.get('/api/job')[1]['job']['status'] == 'done')
            ids.append(plan['id'])
        finished = self.ok(self.c.get('/api/plans'))['finished']
        self.assertEqual([p['id'] for p in finished], ids[::-1][:5])   # the last five, newest first
        for p in finished:
            self.assertEqual(p['state'], 'done')
            self.assertEqual(p['result']['counts'], {'done': p['totals']['count']})
            self.assertIn('skipped', p['result'])

    def test_a_running_plan_is_the_active_one(self):
        plan = self.plan()
        gate = threading.Event()
        started = threading.Event()

        def hold(entry):
            started.set()
            gate.wait(30)
        self.app._hooks['after_claim'] = hold
        self.addCleanup(gate.set)
        self.ok(self.c.post(f'/api/plans/{plan["id"]}/run', {'revision': 1, 'entries_digest': plan['entries_digest']}))
        self.assertTrue(started.wait(30))
        listed = self.ok(self.c.get('/api/plans'))
        self.assertEqual((listed['active']['id'], listed['active']['state']), (plan['id'], 'running'))
        gate.set()
        self.wait_for(lambda: self.c.get('/api/job')[1]['job']['status'] == 'done')
        self.assertIsNone(self.ok(self.c.get('/api/plans'))['active'])

    def test_the_result_is_kept_with_the_plan(self):
        plan = self.plan()
        self.ok(self.c.post(f'/api/plans/{plan["id"]}/run', {'revision': 1, 'entries_digest': plan['entries_digest']}))
        self.wait_for(lambda: self.c.get('/api/job')[1]['job']['status'] == 'done')
        job = self.ok(self.c.get('/api/job'))['job']['summary']
        stored = self.ok(self.c.get(f'/api/plans/{plan["id"]}'))['result']
        for key in ('counts', 'removed_alloc', 'freed', 'moved_to_trash', 'estimated_delta', 'measured_delta', 'skipped', 'warnings', 'rescan'):
            self.assertEqual(stored[key], job[key], key)
        self.assertEqual(stored['removed_alloc'], plan['totals']['alloc'])   # what the button said is what was removed


class PlanEditTests(QuickCase):
    def plan(self):
        status, plan = self.quick_plan(self.quick(0))
        self.assertEqual(status, 200)
        return plan

    def test_plan_groups(self):
        plan = self.plan()
        groups = self.ok(self.c.get(f'/api/plans/{plan["id"]}/groups'))['groups']
        self.assertEqual(sum(g['entries'] for g in groups), plan['totals']['count'])
        self.assertEqual(sum(g['pending']['alloc'] for g in groups), plan['totals']['alloc'])
        self.assertEqual(sum(g['pending']['count'] for g in groups), plan['totals']['count'])
        self.assertEqual([g['alloc'] for g in groups], sorted((g['alloc'] for g in groups), reverse=True))
        for g in groups:
            self.assertTrue(g['label'] and g['effect'])
            self.assertEqual(g['by_state'], {'pending': g['entries']})
            self.assertEqual(g['actions'], ['delete'])
        first = groups[0]['group']
        page = self.ok(self.c.get(f'/api/plans/{plan["id"]}/entries?' + urlencode({'group': first, 'limit': 100})))
        self.assertEqual((page['total'], len(page['rows'])), (groups[0]['entries'], groups[0]['entries']))
        self.assertEqual({r['group_key'] for r in page['rows']}, {first})

    def test_a_whole_group_can_be_left_out_and_put_back(self):
        plan = self.plan()
        pid = plan['id']
        groups = self.ok(self.c.get(f'/api/plans/{pid}/groups'))['groups']
        g = groups[0]
        out = self.ok(self.c.post(f'/api/plans/{pid}/exclude', {'group': g['group'], 'revision': 1}))
        self.assertEqual(out, {'revision': 2})
        detail = self.ok(self.c.get(f'/api/plans/{pid}'))
        self.assertEqual(detail['pending']['count'], plan['totals']['count'] - g['entries'])
        self.assertEqual(detail['pending']['alloc'], plan['totals']['alloc'] - g['alloc'])
        after = {x['group']: x for x in self.ok(self.c.get(f'/api/plans/{pid}/groups'))['groups']}
        self.assertEqual(after[g['group']]['by_state'], {'excluded': g['entries']})
        self.assertEqual(after[g['group']]['pending']['count'], 0)
        self.error(self.c.post(f'/api/plans/{pid}/exclude', {'group': g['group'], 'revision': 1}), 409, 'REVISION_MISMATCH')
        back = self.ok(self.c.post(f'/api/plans/{pid}/include', {'group': g['group'], 'revision': 2}))
        self.assertEqual(back, {'revision': 3})
        detail = self.ok(self.c.get(f'/api/plans/{pid}'))
        self.assertEqual((detail['pending']['count'], detail['pending']['alloc']), (plan['totals']['count'], plan['totals']['alloc']))
        self.assertEqual(detail['entries_digest'], plan['entries_digest'])   # edits never change what the plan is
        self.ok(self.c.post(f'/api/plans/{pid}/run', {'revision': 3, 'entries_digest': plan['entries_digest']}))
        self.wait_for(lambda: self.c.get('/api/job')[1]['job']['status'] == 'done')
        self.assertEqual(self.ok(self.c.get('/api/job'))['job']['summary']['counts'], {'done': plan['totals']['count']})

    def test_single_entries_go_out_and_come_back(self):
        plan = self.plan()
        pid = plan['id']
        seqs = [e['seq'] for e in self.ok(self.c.get(f'/api/plans/{pid}/entries?limit=100'))['rows']][:2]
        self.assertEqual(self.ok(self.c.post(f'/api/plans/{pid}/exclude', {'seqs': seqs, 'revision': 1})), {'revision': 2, 'excluded': 2})
        self.assertEqual(self.ok(self.c.post(f'/api/plans/{pid}/include', {'seqs': seqs[:1], 'revision': 2})), {'revision': 3, 'included': 1})
        self.assertEqual(self.ok(self.c.get(f'/api/plans/{pid}'))['entries']['by_state'],
                         {'pending': plan['totals']['count'] - 1, 'excluded': 1})
        self.error(self.c.post(f'/api/plans/{pid}/include', {'seqs': [9999], 'revision': 3}), 404, 'NOT_FOUND')
        self.error(self.c.post(f'/api/plans/{pid}/include', {'seqs': 'all', 'revision': 3}), 400, 'INVALID')
        self.error(self.c.post(f'/api/plans/{pid}/include', {'group': '', 'revision': 3}), 400, 'INVALID')

    def test_only_a_draft_takes_entries_back(self):
        plan = self.plan()
        pid = plan['id']
        g = self.ok(self.c.get(f'/api/plans/{pid}/groups'))['groups'][0]
        self.ok(self.c.post(f'/api/plans/{pid}/exclude', {'group': g['group'], 'revision': 1}))
        self.ok(self.c.post(f'/api/plans/{pid}/run', {'revision': 2, 'entries_digest': plan['entries_digest']}))
        self.wait_for(lambda: self.c.get('/api/job')[1]['job']['status'] == 'done')
        err = self.error(self.c.post(f'/api/plans/{pid}/include', {'group': g['group'], 'revision': 2}), 400, 'INVALID')
        self.assertEqual(err['details'], {'state': 'done'})

    def test_the_preflight_names_what_will_be_kept(self):
        plan = self.plan()
        pre = self.ok(self.c.get(f'/api/preflight/{plan["id"]}'))
        self.assertEqual([b for b in pre['blocker_groups'] if b['kind'] == 'drift'], [])
        with open(self.fh.path('~/.codex/log/codex-tui.log'), 'a') as f:
            f.write('changed after the plan')
        gone = next(e for e in self.ok(self.c.get(f'/api/plans/{plan["id"]}/entries?limit=100'))['rows']
                    if e['path'].endswith('state.json.tmp-1784-aaaa'))
        os.unlink(self.fh.path(gone['path']))
        pre = self.ok(self.c.get(f'/api/preflight/{plan["id"]}'))
        drift = [b for b in pre['blocker_groups'] if b['kind'] == 'drift']
        self.assertEqual([b['count'] for b in drift], [2])
        self.assertRegex(drift[0]['text'], r'^Modified or removed since the scan, will be left alone: 2 items · \d+\.\d\d KiB$')
        zh = self.ok(self.c.get(f'/api/preflight/{plan["id"]}', headers={'Accept-Language': 'zh-CN'}))
        self.assertIn('扫描后', [b for b in zh['blocker_groups'] if b['kind'] == 'drift'][0]['text'])

    def test_counts_in_blocker_texts_have_thousands_separators(self):
        self.mac.running.add('com.anthropic.claudefordesktop')
        for i in range(1200):
            self.fh.file(f'~/Library/Application Support/Claude/Cache/old{i}', size=10, age_days=30)
        self.app.scan()
        q = self.quick(14)
        status, plan = self.quick_plan(q)
        self.assertEqual(status, 200)
        text = [b for b in self.ok(self.c.get(f'/api/preflight/{plan["id"]}'))['blocker_groups'] if b['kind'] == 'quit_app'][0]['text']
        self.assertRegex(text, r'^Quit Claude Desktop to clean 1,2\d\d more items · ')
        zh = self.ok(self.c.get(f'/api/preflight/{plan["id"]}', headers={'Accept-Language': 'zh-CN'}))
        self.assertRegex([b for b in zh['blocker_groups'] if b['kind'] == 'quit_app'][0]['text'], r'后可多清理 1,2\d\d 项 · ')


if __name__ == '__main__':
    unittest.main()
