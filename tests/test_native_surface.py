"""Native cleanup commands through their doors: Insights, GET /api/native and POST /api/native/run, and
`disk-steward tools`. Fake tools on a temp PATH; nothing real is started."""
from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from unittest import mock

import test_native as base
from disk_steward import i18n, native
from disk_steward import schedule as sched
from disk_steward.cli import main
from disk_steward.portal.server import PortalServer
from portal_support import Client
from native_support import wait_until

OLLAMA_NAMES = ['llama3:latest', 'tiny:1b', 'a$HOME;b:v1']


def tearDownModule():
    if i18n.MISSING:
        raise AssertionError(f'untranslated keys: {sorted(i18n.MISSING)}')


class SurfaceCase(base.ToolCase):
    def setUp(self):
        super().setUp()
        for patch in (mock.patch.object(sched, 'LAUNCH_AGENTS_DIR', self.fh.root / 'LaunchAgents'),
                      mock.patch.object(sched, 'is_loaded', return_value=False)):
            patch.start()
            self.addCleanup(patch.stop)
        self.install()
        self.app.scan()
        self.base = self.app.events.version


class InsightsTests(SurfaceCase):
    def clean(self, rule_id='dev.homebrew'):
        self.start(rule_id)
        return self.wait_job()['native']

    def test_native_runs_are_cleanups_with_their_own_kind(self):
        result = self.clean()
        rows = self.app.insights(30, 'en')['history']['cleanups']
        (row,) = rows
        self.assertEqual(row, {
            'kind': 'native', 'plan_id': None, 'rule_id': 'dev.homebrew', 'target': None,
            'command': 'brew cleanup --prune=all', 'created_at': result['started_at'],
            'finished_at': result['finished_at'], 'items_done': None, 'items_skipped': None, 'items_failed': None,
            'items_excluded': None, 'removed': result['freed'], 'freed': result['freed'], 'moved_to_trash': 0,
            'measured_delta': None, 'estimated': None, 'exit_code': 0, 'before': result['before'], 'after': 0})
        self.assertEqual(row['freed'], result['before'] - result['after'])

    def test_freed_total_adds_what_tools_freed_to_what_plans_freed(self):
        first = self.clean('dev.homebrew')
        self.clock.advance(days=1)
        second = self.clean('dev.uv')
        k = self.app.insights(30, 'en')['kpis']['freed_total']
        self.assertEqual(k, {'value': first['freed'] + second['freed'], 'count': 2, 'since': first['started_at']})
        self.assertGreater(second['freed'], 0)

    def test_with_plans_and_tools_together_they_sort_by_finish_time_and_the_window_applies(self):
        self.clean('dev.homebrew')
        self.clock.advance(days=40)
        self.clean('dev.uv')
        rows = self.app.insights(30, 'en')['history']['cleanups']
        self.assertEqual([r['rule_id'] for r in rows], ['dev.uv'])
        rows = self.app.insights(0, 'en')['history']['cleanups']
        self.assertEqual([r['rule_id'] for r in rows], ['dev.uv', 'dev.homebrew'])

    def test_a_failed_run_is_listed_with_its_exit_code_and_a_cancelled_one_too(self):
        self.bin.tool('brew', f'record()\nwipe({self.path("dev.homebrew")!r})\nsys.exit(2)')
        self.clean()
        (row,) = self.app.insights(30, 'en')['history']['cleanups']
        self.assertEqual((row['exit_code'], row['freed'] > 0), (2, True))

    def test_the_snapshot_tip_only_looks_at_plans(self):
        self.mac.snapshot_names = ['com.apple.1']
        self.clean()
        self.assertNotIn('snapshots_held', [t['id'] for t in self.app.insights(30, 'en')['todos']])

    def test_a_report_only_tip_says_whether_its_tool_can_run(self):
        with mock.patch('disk_steward.insights.REPORT_ONLY_MIN', 1):   # the fixture folders are small
            todos = {t['id']: t for t in self.app.insights(30, 'en')['todos'] if t['id'].startswith('report_only:')}
        self.assertTrue(todos['report_only:dev.homebrew']['runnable'])
        self.assertEqual(todos['report_only:dev.homebrew']['action']['href'], '#/browse?report_only=1')
        self.assertEqual(todos['report_only:dev.homebrew']['command'], 'brew cleanup --prune=all')
        (self.bin.dir / 'brew').unlink()
        with mock.patch('disk_steward.insights.REPORT_ONLY_MIN', 1), mock.patch('disk_steward.insights.TOP_REPORT_ONLY', 20):
            todos = {t['id']: t for t in self.app.insights(30, 'en')['todos'] if t['id'].startswith('report_only:')}
        self.assertFalse(todos['report_only:dev.homebrew']['runnable'])
        self.assertTrue(all(isinstance(t['runnable'], bool) for t in todos.values()))
        for rule_id in ('dev.gradle', 'dev.playwright'):               # no run is offered for those
            key = f'report_only:{rule_id}'
            if key in todos:
                self.assertFalse(todos[key]['runnable'])


class ApiTests(SurfaceCase):
    def setUp(self):
        super().setUp()
        self.server = PortalServer(self.app, 0)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.c = Client(self.server).login()

    def error(self, resp, status, code, key):
        self.assertEqual(resp[0], status, resp[1])
        err = resp[1]['error']
        self.assertEqual((err['code'], err['message_key']), (code, key))
        self.assertTrue(err['message'])
        return err

    def run_body(self, rule_id='dev.homebrew', command='brew cleanup --prune=all', **extra):
        return dict({'rule_id': rule_id, 'command': command}, **extra)

    def wait(self):
        wait_until(lambda: self.c.get('/api/job')[1]['job']['status'] != 'running')
        return self.c.get('/api/job')[1]['job']

    def test_the_list_is_the_services_rows_in_the_requests_language(self):
        status, body = self.c.get('/api/native')
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {'tools'})
        self.assertEqual(body['tools'], json.loads(json.dumps(self.app.native_tools('en'))))
        self.assertEqual(body['tools'][0]['rule_id'], 'dev.homebrew')
        zh = self.c.get('/api/native', headers={'Accept-Language': 'zh-CN'})[1]['tools']
        row = {r['rule_id']: r for r in zh}['dev.gradle']
        self.assertEqual((row['reason'], [f['path'] for f in row['folders']]), (None, ['~/.gradle/caches/modules-2']))

    def test_move_to_trash_makes_a_draft_plan_of_one_folder(self):
        folder = {r['rule_id']: r for r in self.app.native_tools('en')}['dev.gradle']['folders'][0]
        status, plan = self.c.post('/api/native/trash', {'item': folder['item']})
        self.assertEqual(status, 200, plan)
        self.assertEqual((plan['state'], plan['totals']['count']), ('draft', 1))
        self.assertEqual(self.c.post('/api/native/trash', {'item': folder['item']}, token=False)[0], 401)
        self.assertEqual(self.c.post('/api/native/trash', {'item': 3})[0], 400)
        self.assertEqual(self.c.get('/api/native/trash')[0], 405)

    def test_it_needs_the_session_and_the_right_methods(self):
        self.assertEqual(self.c.get('/api/native', token=False)[0], 401)
        self.assertEqual(self.c.post('/api/native', {})[0], 405)
        self.assertEqual(self.c.get('/api/native/run')[0], 405)
        self.assertEqual(self.c.post('/api/native/run', {}, token=False)[0], 401)

    def test_a_run_is_started_and_its_job_reports_it(self):
        status, body = self.c.post('/api/native/run', self.run_body())
        self.assertEqual(status, 200, body)
        self.assertEqual(set(body), {'job'})
        self.assertEqual((body['job']['kind'], body['job']['rule_id'], body['job']['target'], body['job']['command']),
                         ('native', 'dev.homebrew', None, 'brew cleanup --prune=all'))
        job = self.wait()
        self.assertEqual((job['status'], job['native']['status'], job['native']['exit_code']), ('done', 'ok', 0))
        events = self.c.get('/api/events?since=0&timeout=0')[1]['events']
        self.assertIn('native.done', [e['type'] for e in events])
        self.assertEqual([e['data']['line'] for e in events if e['type'] == 'native.output'][-2:],
                         ['Removing downloads', 'done'])

    def test_a_model_is_removed_by_name(self):
        status, body = self.c.post('/api/native/run', self.run_body('ollama.models', 'ollama rm tiny:1b', target='tiny:1b'))
        self.assertEqual(status, 200, body)
        self.assertEqual(body['job']['target'], 'tiny:1b')
        self.wait()
        self.assertEqual(self.bin.calls()[0]['argv'], ['rm', 'tiny:1b'])

    def test_malformed_bodies_are_bad_fields(self):
        for body, field in (({}, 'rule_id'), ({'rule_id': 'dev.homebrew'}, 'command'), ({'command': 'x'}, 'rule_id'),
                            (self.run_body(rule_id=3), 'rule_id'), (self.run_body(rule_id=''), 'rule_id'),
                            (self.run_body(command=['brew']), 'command'), (self.run_body(command=''), 'command'),
                            (self.run_body(target=3), 'target'), (self.run_body(target=''), 'target')):
            with self.subTest(body=body):
                err = self.error(self.c.post('/api/native/run', body), 400, 'INVALID', 'portal.error.bad_field')
                self.assertEqual(err['details'], {'field': field})
        self.assertEqual(self.bin.calls(), [])

    def test_an_unknown_tool_a_changed_command_and_a_bad_model_are_refused_with_their_own_message(self):
        err = self.error(self.c.post('/api/native/run', self.run_body('codex.logs')), 400, 'INVALID', 'error.native_unknown')
        self.assertIn('codex.logs', err['message'])
        err = self.error(self.c.post('/api/native/run', self.run_body(command='brew cleanup')), 400, 'INVALID',
                         'error.native_command_changed')
        self.assertEqual(err['details']['expected'], 'brew cleanup --prune=all')
        self.assertIn('brew cleanup --prune=all', err['message'])
        self.error(self.c.post('/api/native/run', self.run_body('ollama.models', 'ollama rm nope', target='nope')), 400,
                   'INVALID', 'error.native_bad_target')
        self.error(self.c.post('/api/native/run', self.run_body('ollama.models', 'ollama rm <model>')), 400, 'INVALID',
                   'error.native_target_required')
        self.assertEqual(self.bin.calls(), [])

    def test_a_tool_that_cannot_run_is_503_with_the_reason_in_the_requests_language(self):
        err = self.error(self.c.post('/api/native/run', self.run_body('dev.playwright', 'x')), 503, 'UNAVAILABLE',
                         'error.native_not_runnable')
        self.assertIn('network', err['message'])
        zh = self.c.post('/api/native/run', self.run_body('dev.gradle', 'x'), headers={'Accept-Language': 'zh-CN'})
        err = self.error(zh, 503, 'UNAVAILABLE', 'error.native_not_runnable')
        self.assertIn('没有官方清理命令', err['message'])
        self.assertEqual(err['details']['reason'], '没有官方清理命令。')
        self.assertEqual(err['details']['reason_args'], {})

    def test_a_second_run_while_one_is_going_is_busy_and_cancel_stops_it(self):
        started = self.fh.root / 'started'
        self.bin.tool('brew', f'record()\nopen({str(started)!r}, "w").write("1")\ntime.sleep(300)')
        self.assertEqual(self.c.post('/api/native/run', self.run_body())[0], 200)
        wait_until(started.exists)
        self.error(self.c.post('/api/native/run', self.run_body('dev.uv', 'uv cache prune')), 423, 'BUSY', 'error.busy')
        self.assertEqual(self.c.post('/api/scan', {})[0], 423)
        status, body = self.c.post('/api/job/cancel', {})
        self.assertEqual((status, body['job']['kind']), (200, 'native'))
        job = self.wait()
        self.assertEqual((job['status'], job['native']['status']), ('idle', 'cancelled'))
        events = self.c.get('/api/events?since=0&timeout=0')[1]['events']
        (failed,) = [e for e in events if e['type'] == 'native.failed']
        self.assertEqual(failed['data']['message_key'], 'error.native_cancelled')
        self.assertEqual(failed['data']['message'], 'brew cleanup --prune=all was stopped before it finished.')

    def test_a_tool_run_cannot_be_paused(self):
        started = self.fh.root / 'started'
        self.bin.tool('brew', f'record()\nopen({str(started)!r}, "w").write("1")\ntime.sleep(300)')
        self.c.post('/api/native/run', self.run_body())
        wait_until(started.exists)
        for action in ('pause', 'resume'):
            self.error(self.c.post(f'/api/job/{action}', {}), 400, 'INVALID', 'error.native_no_pause')
        self.c.post('/api/job/cancel', {})
        self.wait()

    def test_the_job_in_the_state_answer_is_translated_too(self):
        started = self.fh.root / 'started'
        self.bin.tool('brew', f'record()\nopen({str(started)!r}, "w").write("1")\ntime.sleep(300)')
        self.c.post('/api/native/run', self.run_body())
        wait_until(started.exists)
        self.app.cancel()
        job = self.wait()
        self.assertEqual(job['status'], 'idle')
        self.assertEqual(self.c.get('/api/state')[1]['job']['kind'], 'native')


class FakeTty(io.StringIO):
    def isatty(self):
        return True


class CliTests(SurfaceCase):
    def run_cli(self, *argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with mock.patch('sys.stdin', stdin if stdin is not None else io.StringIO('')):
                try:
                    code = main(list(argv), app_factory=lambda: self.app)
                except SystemExit as e:
                    code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_the_list_shows_rule_command_size_and_state(self):
        code, out, err = self.run_cli('tools', '--lang', 'en')
        self.assertEqual((code, err), (0, ''))
        for text in ('Report-only tools that have a cleanup command', 'Rule', 'dev.homebrew', 'brew cleanup --prune=all',
                     'can run', '500.00 KiB', 'ollama.models', 'ollama rm <model>', 'dev.gradle',
                     "no command: Move to Trash in the portal's tools table", 'dev.playwright', 'npx playwright uninstall --all',
                     'Models you can remove with --target NAME:', 'llama3:latest', '2.0 GB', 'tiny:1b',
                     'Run one with: disk-steward tools run RULE_ID'):
            self.assertIn(text, out)

    def test_the_list_in_chinese_and_as_json(self):
        code, out, _ = self.run_cli('tools', '--lang', 'zh-CN')
        self.assertEqual(code, 0)
        for text in ('有自带清理命令的仅展示工具', '规则', '可运行', '无官方命令：可在门户的工具表格里移到废纸篓', '可用 --target NAME 移除的模型：'):
            self.assertIn(text, out)
        code, out, _ = self.run_cli('tools', '--json', '--lang', 'zh-CN')
        rows = json.loads(out)
        self.assertEqual(rows, json.loads(json.dumps(self.app.native_tools('zh-CN'))))
        self.assertEqual(rows[0]['rule_id'], 'dev.homebrew')

    def test_before_any_scan_there_is_nothing_to_list(self):
        for lang, text in (('en', 'No report-only group with data in the latest scan. Run a scan first.'),
                           ('zh-CN', '最近一次扫描中没有占用空间的仅展示分组。请先扫描。')):
            fresh = self.open_app_without_scan()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                main(['tools', '--lang', lang], app_factory=lambda: fresh)
            self.assertIn(text, out.getvalue())

    def open_app_without_scan(self):
        other = base.FakeHome(self.clock).__enter__()
        self.addCleanup(other.cleanup)
        app = base.make_app(other, mac=self.mac)
        self.addCleanup(app.close)
        return app

    def test_run_with_yes_streams_the_output_and_prints_what_was_freed(self):
        code, out, err = self.run_cli('tools', 'run', 'dev.homebrew', '--yes', '--lang', 'en')
        self.assertEqual(code, 0, err)
        self.assertIn('Removing downloads\ndone\n', out)
        self.assertIn('brew cleanup --prune=all exited with code 0.', out)
        self.assertIn('Rescanning so every number follows the tool.', err)
        self.assertIn('Before 500.00 KiB, after 0 B, freed 500.00 KiB.', out)
        self.assertIn('Running brew cleanup --prune=all', err)
        self.assertEqual(len(self.bin.calls()), 1)
        self.assertEqual(self.app.job()['status'], 'done')

    def test_run_in_chinese(self):
        code, out, err = self.run_cli('tools', 'run', 'dev.homebrew', '--yes', '--lang', 'zh-CN')
        self.assertEqual(code, 0, err)
        self.assertIn('运行前 500.00 KiB，运行后 0 B，释放 500.00 KiB。', out)
        self.assertIn('正在运行 brew cleanup --prune=all', err)

    def test_without_a_terminal_it_will_not_run_unless_told_to(self):
        code, out, err = self.run_cli('tools', 'run', 'dev.homebrew')
        self.assertEqual((code, out), (2, ''))
        self.assertIn('confirm', err.lower())
        self.assertEqual(self.bin.calls(), [])

    def test_at_a_terminal_the_rule_id_must_be_typed(self):
        code, out, err = self.run_cli('tools', 'run', 'dev.homebrew', stdin=FakeTty('dev.homebrew\n'))
        self.assertEqual(code, 0, err)
        self.assertIn('Type the rule id (dev.homebrew) to run `brew cleanup --prune=all`: ', err)
        self.assertEqual(len(self.bin.calls()), 1)
        for typed in ('yes\n', 'dev.homebrew\n', '\n', ''):
            with self.subTest(typed=typed):
                code, out, err = self.run_cli('tools', 'run', 'dev.uv', stdin=FakeTty(typed))
                self.assertEqual((code, out), (2, ''))
        self.assertEqual(len(self.bin.calls()), 1)

    def test_what_cannot_run_is_refused_before_anything_is_asked(self):
        for rule_id, text in (('dev.playwright', 'network'), ('dev.gradle', 'No official cleanup command'),
                              ('nope', 'not a tool Disk Steward can clean')):
            with self.subTest(rule=rule_id):
                code, out, err = self.run_cli('tools', 'run', rule_id, stdin=FakeTty('dev.homebrew\n'))
                self.assertEqual((code, out), (2, ''))
                self.assertIn(text, err)
                self.assertNotIn('Type the rule id', err)
        self.assertEqual(self.bin.calls(), [])
        code, out, err = self.run_cli('tools', 'run', 'dev.gradle', '--yes', '--lang', 'zh-CN')
        self.assertIn('没有官方清理命令', err)

    def test_ollama_needs_a_model_and_removes_that_one(self):
        code, _, err = self.run_cli('tools', 'run', 'ollama.models', '--yes')
        self.assertEqual(code, 2)
        self.assertIn('needs a model name', err)
        code, _, err = self.run_cli('tools', 'run', 'ollama.models', '--target', 'nope', '--yes')
        self.assertEqual(code, 2)
        self.assertIn('not one of the models', err)
        self.assertEqual(self.bin.calls(), [])
        code, out, err = self.run_cli('tools', 'run', 'ollama.models', '--target', 'tiny:1b', '--yes')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.bin.calls()[0]['argv'], ['rm', 'tiny:1b'])
        self.assertIn('freed', out)

    def test_a_tool_that_fails_gives_exit_code_one_with_its_numbers(self):
        self.bin.tool('brew', f'record()\nprint("broken")\nwipe({self.path("dev.homebrew")!r})\nsys.exit(4)')
        code, out, err = self.run_cli('tools', 'run', 'dev.homebrew', '--yes')
        self.assertEqual(code, 1)
        self.assertIn('broken', out)
        self.assertIn('exited with code 4', out)
        self.assertIn('freed 500.00 KiB', out)

    def test_a_timeout_says_so_and_exits_one(self):
        self.bin.tool('brew', 'record()\ntime.sleep(300)')
        with mock.patch.object(native, 'TIMEOUT', 1.0), mock.patch.object(native, 'TERM_GRACE', 3.0):
            code, out, err = self.run_cli('tools', 'run', 'dev.homebrew', '--yes')
        self.assertEqual(code, 1)
        self.assertIn('did not finish within', err)

    def test_the_options_work_before_or_after_the_command(self):
        code, out, err = self.run_cli('--lang', 'zh-CN', 'tools', 'run', 'dev.homebrew', '--yes')
        self.assertEqual(code, 0, err)
        self.assertIn('释放', out)


if __name__ == '__main__':
    unittest.main()
