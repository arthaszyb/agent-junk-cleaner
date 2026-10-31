from __future__ import annotations

import ast
import hashlib
import json
import os
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import disk_steward.advisor as advisor_pkg
from advisor_support import AdvisorCase, other_app, reply_for
from disk_steward import settings as settings_mod
from disk_steward.advisor.service import MAX_REQUESTS, REQUEST_TTL
from disk_steward.model import ErrorCode, PlanState


class FakeTime:
    """Stands in for the `time` module inside advisor.service, so probe caching can be stepped through."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


class DefaultOffTests(AdvisorCase):
    def test_while_off_status_runs_no_cli_and_reports_unknown(self):
        self.fakes.install('codex')
        self.fakes.install('claude')
        status = self.adv.status()
        self.assertFalse(status['enabled'])
        self.assertIsNone(status['provider'])
        self.assertIsNone(status['last'])
        self.assertEqual(status['providers']['manual'], {'available': True})
        for name in ('codex', 'claude'):
            self.assertEqual(status['providers'][name], {'available': None, 'version': None, 'reason': 'disabled'})
            self.assertEqual(self.fakes.calls(name), [], f'{name} must not be started while the advisor is off')
        self.adv.status(refresh=True)
        self.adv.set_enabled(False)
        for name in ('codex', 'claude'):
            self.assertEqual(self.fakes.calls(name), [])

    def test_enabled_status_names_what_is_missing(self):
        status = self.enable()
        for name in ('codex', 'claude'):
            self.assertFalse(status['providers'][name]['available'])
            self.assertIn('not found', status['providers'][name]['reason'])
            self.assertIsNone(status['providers'][name]['version'])

    def test_every_entry_point_refuses_while_off(self):
        for fn, args in ((self.adv.preview, ()), (self.adv.run, ('codex', 'x')), (self.adv.start_run, ('codex', 'x')),
                         (self.adv.export_prompt, ('x',)), (self.adv.import_reply, ('x', '{}')),
                         (self.adv.command, ('codex', 'x'))):
            with self.subTest(fn=fn.__name__):
                self.expect(ErrorCode.UNAVAILABLE, 'error.advisor_disabled', fn, *args)

    def test_set_enabled_persists_and_reports_status(self):
        status = self.enable()
        self.assertTrue(status['enabled'])
        self.assertTrue(self.app.settings.advisor_enabled)
        self.assertTrue(settings_mod.load(self.fh.data_dir).advisor_enabled)
        self.assertFalse(self.adv.set_enabled(False)['enabled'])
        self.assertFalse(settings_mod.load(self.fh.data_dir).advisor_enabled)

    def test_enabling_does_not_touch_other_settings(self):
        before = self.app.settings
        self.enable()
        import dataclasses
        after = dataclasses.replace(self.app.settings, advisor_enabled=False)
        self.assertEqual(before, after)

    def test_the_property_is_one_lazily_built_service(self):
        self.assertIs(self.app.advisor, self.app.advisor)

    def test_status_names_the_probed_providers(self):
        self.fakes.install('codex')
        self.fakes.install('claude')
        self.enable()
        providers = self.adv.status(refresh=True)['providers']
        self.assertEqual(providers['codex'], {'available': True, 'version': 'codex-cli 9.9.9', 'reason': ''})
        self.assertEqual(providers['claude'], {'available': True, 'version': '9.9.9 (Claude Code)', 'reason': ''})


class PreviewTests(AdvisorCase):
    def test_preview_returns_the_exact_payload(self):
        self.enable()
        p = self.adv.preview()
        self.assertEqual(set(p), {'request_id', 'scan_id', 'payload', 'bytes', 'coverage', 'expires_at',
                                  'notice_key'})
        self.assertEqual(p['notice_key'], 'advisor.notice.own_instructions')
        self.assertEqual(p['scan_id'], self.app.latest_scan()['id'])
        self.assertEqual(p['bytes'], len(json.dumps(p['payload'], sort_keys=True, separators=(',', ':'))))
        self.assertEqual(p['coverage'], p['payload']['coverage'])
        self.assertEqual(p['expires_at'], self.fh.clock() + REQUEST_TTL)

    def test_tokens_are_fresh_per_preview(self):
        self.enable()
        a, b = self.adv.preview(), self.adv.preview()
        self.assertNotEqual(a['request_id'], b['request_id'])
        ta = {g['token'] for g in a['payload']['groups']}
        tb = {g['token'] for g in b['payload']['groups']}
        self.assertFalse(ta & tb)

    def test_at_most_five_requests_stay_live(self):
        self.enable()
        previews = [self.adv.preview() for _ in range(MAX_REQUESTS + 1)]
        self.expect(ErrorCode.NOT_FOUND, 'error.advisor_unknown_request', self.adv.export_prompt,
                    previews[0]['request_id'])
        for p in previews[1:]:
            self.adv.export_prompt(p['request_id'])

    def test_disabling_forgets_every_request(self):
        self.enable()
        rid = self.adv.preview()['request_id']
        self.adv.set_enabled(False)
        self.enable()
        self.expect(ErrorCode.NOT_FOUND, 'error.advisor_unknown_request', self.adv.export_prompt, rid)

    def test_a_scan_that_lands_during_the_build_is_reported(self):
        self.enable()
        real = self.app.store.latest_scan
        calls = []

        def racing(status='done'):
            calls.append(1)
            if len(calls) == 2:   # the check after the payload was built
                self.app.scan()
            return real(status)
        self.app.store.latest_scan = racing
        try:
            self.expect(ErrorCode.BUSY, 'error.advisor_scan_changed', self.adv.preview)
        finally:
            del self.app.store.latest_scan


class NoScanTests(AdvisorCase):
    scan = False

    def test_preview_needs_a_scan(self):
        self.enable()
        self.expect(ErrorCode.NO_SCAN, 'error.NO_SCAN', self.adv.preview)


class RunTests(AdvisorCase):
    def setUp(self):
        super().setUp()
        self.fakes.install('codex', reply='echo', verdict='likely_safe')
        self.enable()

    def test_run_validates_maps_and_persists(self):
        p = self.adv.preview()
        result = self.adv.run('codex', p['request_id'])
        self.assertEqual(set(result), {'id', 'scan_id', 'provider', 'cli_version', 'created_at', 'summary',
                                       'recommendations', 'traces', 'stale'})
        self.assertEqual(result['traces'], [])
        self.assertEqual((result['provider'], result['cli_version'], result['summary'], result['stale']),
                         ('codex', 'codex-cli 9.9.9', 'canned summary', False))
        self.assertEqual(len(result['recommendations']), p['coverage']['groups_included'])
        labels = {g['key']: g['label'] for g in self.app.groups()}
        for rec in result['recommendations']:
            self.assertEqual(set(rec), {'group', 'label', 'verdict', 'reason'})
            self.assertEqual(rec['label'], labels[rec['group']])
            self.assertIn(rec['verdict'], ('likely_safe', 'keep'))
            self.assertTrue(rec['reason'].startswith('canned reason '))
        self.assertEqual({r['group'] for r in result['recommendations']}, set(labels))
        row = self.app.store.latest_advice(p['scan_id'])
        self.assertEqual((row['id'], row['provider'], row['cli_version']), (result['id'], 'codex', 'codex-cli 9.9.9'))
        self.assertEqual(row['payload_digest'], hashlib.sha256(json.dumps(
            p['payload'], sort_keys=True, separators=(',', ':')).encode()).hexdigest())
        self.assertEqual(row['result']['recommendations'], result['recommendations'])

    def test_status_shows_the_latest_result_for_the_current_scan(self):
        self.assertIsNone(self.adv.status()['last'])
        result = self.adv.run('codex', self.adv.preview()['request_id'])
        last = self.adv.status()['last']
        self.assertEqual({k: last[k] for k in result}, result)

    def test_the_cli_got_the_same_payload_the_preview_showed(self):
        p = self.adv.preview()
        self.adv.run('codex', p['request_id'])
        stdin = self.fakes.record('codex')['stdin']
        sent = json.loads(stdin.split('Disk usage groups (JSON):\n', 1)[1])
        self.assertEqual(sent, p['payload'])

    def test_a_reply_with_a_foreign_token_is_rejected_and_nothing_is_stored(self):
        self.fakes.update('codex', reply=json.dumps({'summary': 's', 'recommendations': [
            {'group': 'gdeadbeef', 'verdict': 'keep', 'reason': 'x'}]}))
        p = self.adv.preview()
        self.expect(ErrorCode.INVALID, 'error.advisor_invalid_reply', self.adv.run, 'codex', p['request_id'])
        self.assertIsNone(self.app.store.latest_advice(p['scan_id']))

    def test_provider_defaults_to_the_setting_and_has_clear_errors(self):
        p = self.adv.preview()
        self.expect(ErrorCode.INVALID, 'error.advisor_no_provider', self.adv.run, None, p['request_id'])
        self.expect(ErrorCode.INVALID, 'error.advisor_manual_only', self.adv.run, 'manual', p['request_id'])
        self.expect(ErrorCode.INVALID, 'error.advisor_unknown_provider', self.adv.run, 'gpt', p['request_id'])
        self.app.update_settings({'advisor_provider': 'codex'})
        self.assertEqual(self.adv.run(None, p['request_id'])['provider'], 'codex')

    def test_an_unavailable_provider(self):
        p = self.adv.preview()
        err = self.expect(ErrorCode.UNAVAILABLE, 'error.advisor_provider_unavailable', self.adv.run, 'claude',
                          p['request_id'])
        self.assertEqual(err.details['provider'], 'claude')
        self.assertIn('not found', err.details['reason'])

    def test_an_unknown_request(self):
        self.expect(ErrorCode.NOT_FOUND, 'error.advisor_unknown_request', self.adv.run, 'codex', 'nope')

    def test_an_expired_request(self):
        p = self.adv.preview()
        self.fh.clock.advance(seconds=REQUEST_TTL + 1)
        self.expect(ErrorCode.INVALID, 'error.advisor_request_expired', self.adv.run, 'codex', p['request_id'])
        self.assertFalse(self.fakes.called('codex'))
        self.expect(ErrorCode.NOT_FOUND, 'error.advisor_unknown_request', self.adv.run, 'codex', p['request_id'])

    def test_a_request_is_good_up_to_its_expiry(self):
        p = self.adv.preview()
        self.fh.clock.advance(seconds=REQUEST_TTL)
        self.adv.run('codex', p['request_id'])

    def test_a_result_for_an_older_scan_is_stale_and_not_current(self):
        p = self.adv.preview()
        self.app.scan()   # a newer scan lands before the reply is used
        result = self.adv.run('codex', p['request_id'])
        self.assertTrue(result['stale'])
        self.assertEqual(result['scan_id'], p['scan_id'])
        self.assertIsNone(self.adv.status()['last'])
        self.assertIsNotNone(self.app.store.latest_advice(p['scan_id']))   # kept, just not shown as current

    def test_run_emits_started_and_done(self):
        since = self.app.events.version
        p = self.adv.preview()
        result = self.adv.run('codex', p['request_id'])
        events = [(e['type'], e['data']) for e in self.app.events.since(since) if e['type'].startswith('advisor.')]
        self.assertEqual([t for t, _ in events], ['advisor.started', 'advisor.done'])
        self.assertEqual(events[0][1], {'provider': 'codex', 'request_id': p['request_id']})
        self.assertEqual(events[1][1], {'result': result})

    def test_failures_emit_advisor_failed_and_free_the_slot(self):
        self.fakes.update('codex', reply=None, exit=1, stderr='nope')
        since = self.app.events.version
        p = self.adv.preview()
        self.expect(ErrorCode.UNAVAILABLE, 'error.advisor_cli_failed', self.adv.run, 'codex', p['request_id'])
        failed = [e['data'] for e in self.app.events.since(since) if e['type'] == 'advisor.failed']
        self.assertEqual(len(failed), 1)
        self.assertEqual((failed[0]['code'], failed[0]['message_key']), ('UNAVAILABLE', 'error.advisor_cli_failed'))
        self.fakes.update('codex', reply='echo', exit=0)
        self.adv.run('codex', p['request_id'])   # not BUSY: the slot was released


class ProbeTests(AdvisorCase):
    def setUp(self):
        super().setUp()
        self.clock = FakeTime()
        patch = mock.patch('disk_steward.advisor.service.time', self.clock)
        patch.start()
        self.addCleanup(patch.stop)

    def test_both_clis_are_probed_side_by_side(self):
        self.fakes.install('codex', probe_sleep=0.5)
        self.fakes.install('claude', probe_sleep=0.5)
        self.enable()
        self.adv.status(refresh=True)

        def window(kind):
            calls = self.fakes.calls(kind)
            return min(c['start'] for c in calls if 'start' in c), max(c['end'] for c in calls if 'end' in c)
        (c_start, c_end), (l_start, l_end) = window('codex'), window('claude')
        self.assertLess(c_start, l_end)
        self.assertLess(l_start, c_end)   # one probe was still running when the other began: not one after the other

    def test_a_success_is_cached_for_30_seconds(self):
        self.fakes.install('codex')
        self.fakes.install('claude')
        self.enable()
        runs = self.fakes.probe_runs('codex')
        self.assertGreater(runs, 0)
        self.clock.now += 29
        self.adv.status()
        self.assertEqual(self.fakes.probe_runs('codex'), runs)
        self.clock.now += 2
        self.adv.status()
        self.assertGreater(self.fakes.probe_runs('codex'), runs)

    def test_a_failure_is_cached_for_10_seconds(self):
        self.fakes.install('codex', version_exit=3)
        self.enable()
        runs = self.fakes.probe_runs('codex')
        self.assertGreater(runs, 0)
        self.clock.now += 9
        status = self.adv.status()
        self.assertEqual(self.fakes.probe_runs('codex'), runs)
        self.assertIn('exited with 3', status['providers']['codex']['reason'])
        self.clock.now += 2
        self.adv.status()
        self.assertGreater(self.fakes.probe_runs('codex'), runs)

    def test_refresh_forces_a_probe(self):
        self.fakes.install('codex')
        self.enable()
        runs = self.fakes.probe_runs('codex')
        self.adv.status(refresh=True)
        self.assertGreater(self.fakes.probe_runs('codex'), runs)

    def test_probe_false_never_starts_a_cli(self):
        self.app.update_settings({'advisor_enabled': True})   # on, but nothing has probed yet
        self.fakes.install('codex')
        status = self.adv.status(probe=False)
        self.assertEqual(self.fakes.calls('codex'), [])
        self.assertEqual(status['providers']['codex'], {'available': None, 'version': None, 'reason': 'not probed'})
        self.assertEqual(status['providers']['manual'], {'available': True})
        self.assertIn('last', status)

    def test_probe_false_reports_what_is_already_known(self):
        self.fakes.install('codex')
        self.enable()
        runs = self.fakes.probe_runs('codex')
        status = self.adv.status(probe=False)
        self.assertEqual(status['providers']['codex']['version'], 'codex-cli 9.9.9')
        self.assertEqual(self.fakes.probe_runs('codex'), runs)

    def test_a_run_does_not_trust_a_cached_failure(self):
        self.enable()   # nothing installed: both failed, and are cached
        self.fakes.install('codex', reply='echo')
        p = self.adv.preview()
        self.assertEqual(self.adv.run('codex', p['request_id'])['provider'], 'codex')

    def test_a_codex_without_the_shell_switch_is_unavailable_in_status_and_refused_by_run(self):
        self.fakes.install('codex', reply='echo', features='apps  stable true\ngoals  stable true\n')
        status = self.enable()
        self.assertEqual(status['providers']['codex'], {'available': False, 'version': 'codex-cli 9.9.9',
                                                         'reason': 'shell cannot be disabled'})
        p = self.adv.preview()
        err = self.expect(ErrorCode.UNAVAILABLE, 'error.advisor_provider_unavailable', self.adv.run, 'codex',
                          p['request_id'])
        self.assertEqual(err.details['reason'], 'shell cannot be disabled')
        self.assertFalse(self.fakes.called('codex'))


class BackgroundTests(AdvisorCase):
    def wait_for(self, types, since, timeout=20):
        end = time.monotonic() + timeout
        found = []
        while time.monotonic() < end:
            found = [e for e in self.app.events.wait(since, 1) if e['type'] in types]
            if found:
                return found
        self.fail(f'no {types} event')

    def test_start_run_publishes_started_then_done(self):
        self.fakes.install('codex', reply='echo', verdict='review')
        self.enable()
        since = self.app.events.version
        p = self.adv.preview()
        started = self.adv.start_run('codex', p['request_id'])
        self.assertEqual(started, {'request_id': p['request_id'], 'provider': 'codex'})
        done = self.wait_for({'advisor.done', 'advisor.failed'}, since)
        self.assertEqual(done[0]['type'], 'advisor.done')
        types = [e['type'] for e in self.app.events.since(since) if e['type'].startswith('advisor.')]
        self.assertEqual(types, ['advisor.started', 'advisor.done'])
        self.assertEqual(done[0]['data']['result']['provider'], 'codex')

    def test_start_run_failure_is_an_event(self):
        self.fakes.install('codex', exit=1, stderr='boom')
        self.enable()
        since = self.app.events.version
        self.adv.start_run('codex', self.adv.preview()['request_id'])
        failed = self.wait_for({'advisor.done', 'advisor.failed'}, since)
        self.assertEqual(failed[0]['type'], 'advisor.failed')
        self.assertEqual(failed[0]['data']['message_key'], 'error.advisor_cli_failed')

    def test_start_run_refuses_in_the_callers_thread(self):
        self.enable()
        p = self.adv.preview()
        self.expect(ErrorCode.UNAVAILABLE, 'error.advisor_provider_unavailable', self.adv.start_run, 'codex',
                    p['request_id'])

    def test_a_second_call_while_one_runs_is_busy(self):
        self.fakes.install('codex', reply='echo', sleep=2)
        self.enable()
        a, b = self.adv.preview(), self.adv.preview()
        since = self.app.events.version
        self.adv.start_run('codex', a['request_id'])
        self.expect(ErrorCode.BUSY, 'error.busy', self.adv.run, 'codex', b['request_id'])
        self.expect(ErrorCode.BUSY, 'error.busy', self.adv.start_run, 'codex', b['request_id'])
        self.wait_for({'advisor.done', 'advisor.failed'}, since)
        self.assertEqual(self.adv.run('codex', b['request_id'])['provider'], 'codex')

    def test_concurrent_runs_from_two_threads_admit_exactly_one(self):
        self.fakes.install('codex', reply='echo', sleep=1)
        self.enable()
        rids = [self.adv.preview()['request_id'] for _ in range(2)]
        outcomes = []

        def go(rid):
            try:
                self.adv.run('codex', rid)
                outcomes.append('ok')
            except Exception as e:
                outcomes.append(e.message_key)
        threads = [threading.Thread(target=go, args=(r,)) for r in rids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(outcomes), ['error.busy', 'ok'])


class SwitchedOffDuringACallTests(AdvisorCase):
    def wait_for(self, since, types, timeout=20):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            found = [e for e in self.app.events.wait(since, 1) if e['type'] in types]
            if found:
                return found
        self.fail(f'no {types} event')

    def test_the_answer_is_discarded_when_the_advisor_is_turned_off_mid_call(self):
        self.fakes.install('codex', reply='echo', sleep=2)
        self.enable()
        since = self.app.events.version
        p = self.adv.preview()
        self.adv.start_run('codex', p['request_id'])
        self.wait_for(since, {'advisor.started'})
        self.adv.set_enabled(False)
        outcome = self.wait_for(since, {'advisor.done', 'advisor.failed'})
        self.assertEqual([e['type'] for e in outcome], ['advisor.failed'])
        self.assertEqual((outcome[0]['data']['code'], outcome[0]['data']['message_key']),
                         ('UNAVAILABLE', 'error.advisor_disabled'))
        time.sleep(0.3)
        types = [e['type'] for e in self.app.events.since(since) if e['type'].startswith('advisor.')]
        self.assertEqual(types, ['advisor.started', 'advisor.failed'])
        self.assertIsNone(self.app.store.latest_advice(p['scan_id']), 'nothing may be stored')
        self.assertIsNone(self.adv.status()['last'])

    def test_a_blocking_run_raises_disabled_and_stores_nothing(self):
        self.fakes.install('codex', reply='echo', sleep=1.5)
        self.enable()
        p = self.adv.preview()
        outcome = []

        def go():
            try:
                self.adv.run('codex', p['request_id'])
            except Exception as e:
                outcome.append(e)
        t = threading.Thread(target=go)
        t.start()
        end = time.monotonic() + 10
        while not self.fakes.called('codex') and time.monotonic() < end:
            time.sleep(0.05)
        self.adv.set_enabled(False)
        t.join()
        self.assertEqual([(e.code, e.message_key) for e in outcome], [(ErrorCode.UNAVAILABLE, 'error.advisor_disabled')])
        self.assertIsNone(self.app.store.latest_advice(p['scan_id']))
        self.enable()
        self.fakes.update('codex', sleep=0)   # the slot was freed: a new request can run
        self.assertEqual(self.adv.run('codex', self.adv.preview()['request_id'])['provider'], 'codex')


class TraceTests(AdvisorCase):
    def test_a_claude_json_entry_is_reported_in_the_result_and_in_status(self):
        self.fakes.install('claude', reply='echo', claude_json='ours')
        self.enable()
        result = self.adv.run('claude', self.adv.preview()['request_id'])
        self.assertEqual(result['traces'], ['~/.claude.json'])
        self.assertEqual(self.adv.status()['last']['traces'], ['~/.claude.json'])
        self.assertEqual(self.app.store.latest_advice(result['scan_id'])['result']['traces'], ['~/.claude.json'])

    def test_a_codex_history_line_about_the_advisor_fails_the_run(self):
        self.fakes.install('codex', reply='echo', log_file='history.jsonl', log_line='ours')
        self.enable()
        p = self.adv.preview()
        self.expect(ErrorCode.INVALID, 'error.advisor_left_session', self.adv.run, 'codex', p['request_id'])
        self.assertIsNone(self.app.store.latest_advice(p['scan_id']))

    def test_the_working_directory_lives_in_the_data_dir(self):
        self.fakes.install('codex', reply='echo')
        self.enable()
        self.adv.run('codex', self.adv.preview()['request_id'])
        self.assertEqual(self.fakes.record('codex')['cwd'], os.path.realpath(self.fh.data_dir / 'advisor-cwd'))


class AdviceNeverActsTests(AdvisorCase):
    def snapshot(self):
        stored = self.app.store.load_spec('default')
        return {'spec': self.app.spec().to_json(), 'stored': None if stored is None else stored.to_json(),
                'plans': self.app.store.plans_in_state(PlanState.DRAFT),
                'groups': [(g['key'], g['selector'], g['selection']) for g in self.app.groups()],
                'settings': settings_mod.load(self.fh.data_dir),
                'disk': sorted(str(p) for p in self.fh.home.rglob('*'))}

    def test_a_run_changes_no_selection_plan_setting_or_file(self):
        self.fakes.install('codex', reply='echo', verdict='likely_safe')
        self.enable()
        self.app.apply_ops([{'op': 'select_tier', 'tier': 'junk', 'on': True}, {'op': 'set_filter', 'min_age_days': 0}])
        before = self.snapshot()
        p = self.adv.preview()
        result = self.adv.run('codex', p['request_id'])
        self.assertTrue(any(r['verdict'] == 'likely_safe' for r in result['recommendations']))
        self.assertEqual(self.snapshot(), before)

    def test_importing_a_reply_changes_nothing_either(self):
        self.enable()
        before = self.snapshot()
        p = self.adv.preview()
        self.adv.export_prompt(p['request_id'])
        self.adv.import_reply(p['request_id'], json.dumps(reply_for(p['payload'], 'likely_safe')))
        self.assertEqual(self.snapshot(), before)

    def test_the_selection_after_advice_equals_the_selection_without_it(self):
        self.enable()
        ops = [{'op': 'select_group', 'group': 'claude.logs', 'on': True}, {'op': 'set_filter', 'min_age_days': 0}]
        plain = self.app.apply_ops(ops)['summary']
        p = self.adv.preview()
        self.adv.import_reply(p['request_id'], json.dumps(reply_for(p['payload'], 'likely_safe')))
        self.assertEqual(self.app.apply_ops(ops)['summary'], plain)

    def test_no_advisor_module_can_select_plan_or_run(self):
        banned_modules = {'selection', 'planner', 'executor', 'actions', 'scanner', 'guards'}
        banned_names = {'apply_ops', 'save_spec', 'create_plan', 'save_plan', 'run_plan', 'GroupSel', 'Spec',
                        'migrate_spec'}
        files = sorted(Path(advisor_pkg.__file__).parent.glob('*.py'))
        self.assertGreaterEqual(len(files), 5)
        for path in files:
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn((node.module or '').split('.')[-1], banned_modules, path.name)
                    self.assertFalse({a.name for a in node.names} & (banned_names | banned_modules), path.name)
                elif isinstance(node, ast.Import):
                    self.assertFalse({a.name.split('.')[-1] for a in node.names} & banned_modules, path.name)
                elif isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, banned_names, path.name)
                elif isinstance(node, ast.Name):
                    self.assertNotIn(node.id, banned_names, path.name)


class ManualTests(AdvisorCase):
    def setUp(self):
        super().setUp()
        self.enable()

    def test_export_prompt_has_instructions_payload_and_schema(self):
        p = self.adv.preview()
        text = self.adv.export_prompt(p['request_id'])['text']
        self.assertIn('You review anonymized disk-usage groups', text)
        self.assertIn(json.dumps(p['payload'], sort_keys=True, separators=(',', ':')), text)
        self.assertIn('"additionalProperties": false', text)

    def test_import_reply_accepts_a_fenced_block(self):
        p = self.adv.preview()
        body = json.dumps(reply_for(p['payload'], 'review', summary='From a chat.'))
        result = self.adv.import_reply(p['request_id'], f'```json\n{body}\n```')
        self.assertEqual((result['provider'], result['cli_version'], result['summary']), ('manual', '', 'From a chat.'))
        self.assertEqual(self.app.store.latest_advice(p['scan_id'])['provider'], 'manual')

    def test_import_reply_rejects_bad_replies(self):
        p = self.adv.preview()
        good = reply_for(p['payload'])
        self.expect(ErrorCode.INVALID, 'error.advisor_invalid_reply', self.adv.import_reply, p['request_id'],
                    'I think everything is fine.')
        self.expect(ErrorCode.INVALID, 'error.advisor_invalid_reply', self.adv.import_reply, p['request_id'],
                    json.dumps(dict(good, extra=1)))
        dupe = dict(good, recommendations=good['recommendations'][:1] * 2)
        self.expect(ErrorCode.INVALID, 'error.advisor_invalid_reply', self.adv.import_reply, p['request_id'],
                    json.dumps(dupe))
        self.assertIsNone(self.app.store.latest_advice(p['scan_id']))

    def test_a_reply_for_another_request_is_rejected(self):
        a, b = self.adv.preview(), self.adv.preview()
        self.expect(ErrorCode.INVALID, 'error.advisor_invalid_reply', self.adv.import_reply, a['request_id'],
                    json.dumps(reply_for(b['payload'])))

    def test_an_expired_request_cannot_be_imported(self):
        p = self.adv.preview()
        self.fh.clock.advance(seconds=REQUEST_TTL + 1)
        self.expect(ErrorCode.INVALID, 'error.advisor_request_expired', self.adv.import_reply, p['request_id'],
                    json.dumps(reply_for(p['payload'])))

    def test_a_later_process_can_import_the_latest_export(self):
        p = self.adv.preview()
        self.adv.export_prompt(p['request_id'])
        later = other_app(self.fh)   # stands in for `disk-steward advise --import` in a new process
        self.addCleanup(later.close)
        result = later.advisor.import_reply(None, json.dumps(reply_for(p['payload'], 'review')))
        self.assertEqual(result['provider'], 'manual')
        self.assertTrue(result['recommendations'])
        self.expect(ErrorCode.NOT_FOUND, 'error.advisor_unknown_request', later.advisor.import_reply, None, '{}')

    def test_an_import_by_id_must_name_the_exported_request(self):
        p = self.adv.preview()
        self.adv.export_prompt(p['request_id'])
        later = other_app(self.fh)
        self.addCleanup(later.close)
        self.expect(ErrorCode.NOT_FOUND, 'error.advisor_unknown_request', later.advisor.import_reply, 'other', '{}')

    def test_the_export_record_expires_and_disabling_removes_it(self):
        p = self.adv.preview()
        self.adv.export_prompt(p['request_id'])
        later = other_app(self.fh)
        self.addCleanup(later.close)
        self.fh.clock.advance(seconds=REQUEST_TTL + 1)
        self.expect(ErrorCode.INVALID, 'error.advisor_request_expired', later.advisor.import_reply, None,
                    json.dumps(reply_for(p['payload'])))
        self.adv.set_enabled(False)
        self.assertIsNone(self.app.store.meta('advisor.export'))

    def test_a_stale_manual_reply_is_marked_stale(self):
        p = self.adv.preview()
        self.app.scan()
        result = self.adv.import_reply(p['request_id'], json.dumps(reply_for(p['payload'])))
        self.assertTrue(result['stale'])
        self.assertIsNone(self.adv.status()['last'])


class LazyImportTests(unittest.TestCase):
    def test_an_app_that_never_advises_never_imports_the_advisor(self):
        import subprocess
        import sys
        code = 'import sys; from disk_steward import service; print("disk_steward.advisor" in sys.modules)'
        src = str(Path(advisor_pkg.__file__).parents[2])
        out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=True,
                             env={'PYTHONPATH': src}).stdout.strip()
        self.assertEqual(out, 'False')


if __name__ == '__main__':
    unittest.main()
