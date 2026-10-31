from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import m2_fixture as fx
from disk_steward import __version__, i18n
from disk_steward.cli import main, parse_age
from disk_steward.model import PlanState
from disk_steward.paths import item_key
from disk_steward.store import Store
from m3_support import make_app
from support import Clock, FakeHome, real_home_guard


class CliTests(unittest.TestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(argv)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_version(self):
        code, out, _ = self.run_main(['--version'])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), f'disk-steward {__version__}')
        self.assertEqual(__version__, '0.4.0')

    def test_doctor_prints_json(self):
        with FakeHome() as fh, mock.patch.dict(os.environ, {'DISK_STEWARD_DATA_DIR': str(fh.data_dir)}):
            with real_home_guard():
                code, out, _ = self.run_main(['doctor'])
            self.assertEqual(os.listdir(fh.data_dir), [])           # read-only: it did not even create the data dir
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(set(report), {'version', 'python', 'platform', 'data_dir', 'codex', 'claude', 'trash'})
        self.assertEqual(report['version'], __version__)
        self.assertEqual(report['data_dir'], str(fh.data_dir))
        self.assertIsInstance(report['trash'], bool)
        for tool in ('codex', 'claude'):
            self.assertTrue(report[tool] is None or os.path.isabs(report[tool]))

    def test_no_command_is_a_usage_error(self):
        code, _, err = self.run_main([])
        self.assertEqual(code, 2)
        self.assertIn('usage', err)

    def test_unknown_command_is_a_usage_error(self):
        self.assertEqual(self.run_main(['frobnicate'])[0], 2)


LOG = '~/.claude/debug/x.txt'          # claude.logs, 5 days old (rule floor 1 day)
CODEX_LOG = '~/.codex/log/codex-tui.log'     # codex.logs, 10 days old


class TtyIO(io.StringIO):
    def isatty(self):
        return True


class CommandCase(unittest.TestCase):
    def setUp(self):
        self.fh = FakeHome(Clock()).__enter__()
        self.addCleanup(self.fh.cleanup)
        fx.build(self.fh)
        self.app = make_app(self.fh)
        self.addCleanup(self.app.close)

    def run_cli(self, *argv, app=None, stderr=None):
        out, err = io.StringIO(), stderr or io.StringIO()
        app = app or self.app
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(list(argv), app_factory=lambda: app)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def scanned(self):
        self.assertEqual(self.run_cli('scan')[0], 0)

    def plan_ids(self):
        con = sqlite3.connect(self.fh.data_dir / 'steward.db')
        try:
            return [r[0] for r in con.execute('SELECT id FROM plans ORDER BY rowid')]
        finally:
            con.close()

    def entries_of(self, plan_id):
        code, out, _ = self.run_cli('show', plan_id, '--entries', '--limit', '1000', '--json')
        self.assertEqual(code, 0)
        return json.loads(out)['entries']['rows']


class ScanCommandTests(CommandCase):
    def test_scan_prints_the_report(self):
        code, out, err = self.run_cli('scan')
        self.assertEqual((code, err), (0, ''))
        self.assertIn('Scan #1', out)
        self.assertIn('Space by tier', out)
        self.assertIn('Largest groups', out)
        self.assertIn('Where the scanned space goes', out)

    def test_scan_json(self):
        code, out, _ = self.run_cli('scan', '--json', '--domain', 'agent')
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data['scan']['id'], 1)
        self.assertEqual(set(data) >= {'scan', 'volume', 'totals', 'top_groups', 'growth', 'offered', 'not_included'}, True)

    def test_progress_is_one_updating_line_on_a_terminal(self):
        err = TtyIO()
        code, out, _ = self.run_cli('scan', stderr=err)
        self.assertEqual(code, 0)
        text = err.getvalue()
        self.assertIn('\r\x1b[2K', text)
        self.assertIn('Scanning ', text)
        self.assertNotIn('\n', text)
        self.assertTrue(text.endswith('\r\x1b[2K'))
        self.assertNotIn('Scanning', out)

    def test_no_progress_when_stderr_is_not_a_terminal(self):
        self.assertEqual(self.run_cli('scan')[2], '')

    def test_exit_code_1_for_scan_errors_but_not_for_privacy_skips(self):
        with mock.patch.object(Store, 'scan_errors', return_value=[{'path': '~/x', 'error': 'skipped_privacy'}]):
            self.assertEqual(self.run_cli('scan')[0], 0)
        with mock.patch.object(Store, 'scan_errors', return_value=[{'path': '~/x', 'error': 'permission_denied'}]):
            code, out, _ = self.run_cli('scan')
        self.assertEqual(code, 1)
        self.assertIn('permission denied 1', out)

    def test_busy_exits_3(self):
        with self.app.lock:
            code, out, err = self.run_cli('scan')
        self.assertEqual((code, out), (3, ''))
        self.assertIn('Error [BUSY]', err)
        self.assertIn('already running', err)

    def test_unknown_domain_is_a_usage_error(self):
        self.assertEqual(self.run_cli('scan', '--domain', 'moon')[0], 2)


class ReportCommandTests(CommandCase):
    def test_no_scan_yet(self):
        code, out, err = self.run_cli('report')
        self.assertEqual((code, out), (2, ''))
        self.assertIn('Error [NO_SCAN]: No scan yet. Run a scan first.', err)

    def test_errors_as_json_go_to_stdout(self):
        code, out, err = self.run_cli('report', '--json')
        self.assertEqual((code, err), (2, ''))
        error = json.loads(out)['error']
        self.assertEqual((error['code'], error['message_key']), ('NO_SCAN', 'error.NO_SCAN'))
        self.assertEqual(error['message'], 'No scan yet. Run a scan first.')

    def test_report_text_and_json(self):
        self.scanned()
        code, out, _ = self.run_cli('report')
        self.assertEqual(code, 0)
        self.assertIn('Space by tier', out)
        data = json.loads(self.run_cli('report', '--json')[1])
        self.assertEqual(data['scan']['id'], 1)

    def test_since_last(self):
        self.scanned()
        self.assertEqual(self.run_cli('report', '--since-last')[1], 'No earlier scan to compare with.\n')
        self.fh.file('~/.claude/debug/big.txt', size=40 * 1024, age_days=5)
        self.scanned()
        code, out, _ = self.run_cli('report', '--since-last')
        self.assertEqual(code, 0)
        self.assertIn('Change since scan #1', out)
        self.assertIn('Claude logs', out)
        self.assertNotIn('Space by tier', out)
        data = json.loads(self.run_cli('report', '--since-last', '--json')[1])
        self.assertEqual(set(data), {'scan', 'growth'})
        self.assertEqual(data['growth']['previous_scan_id'], 1)


class ListCommandTests(CommandCase):
    def setUp(self):
        super().setUp()
        self.scanned()

    def test_group_table(self):
        code, out, _ = self.run_cli('list')
        self.assertEqual(code, 0)
        for text in ('claude.logs', 'Claude logs', 'codex.sessions:duplicates', 'Qualify', 'Tier'):
            self.assertIn(text, out)
        self.assertIn('Qualify counts items at least 14 days old', out)

    def test_filters_and_limit(self):
        code, out, _ = self.run_cli('list', '--tier', 'junk', '--domain', 'agent', '--limit', '3')
        self.assertEqual(code, 0)
        rows = out.split('\n')[2:5]
        self.assertTrue(all(r.startswith('Junk') for r in rows), rows)
        self.assertNotIn('Protected', out)
        data = json.loads(self.run_cli('list', '--tier', 'review', '--json')[1])
        self.assertTrue(data['groups'] and all(g['tier'] == 'review' for g in data['groups']))
        self.assertEqual(data['min_age_days'], 14)

    def test_qualify_counts_follow_older_than(self):
        for age, expected in (('14d', '0 / 1'), ('3d', '1 / 1')):
            out = self.run_cli('list', '--older-than', age)[1]
            row = next(r for r in out.split('\n') if ' claude.logs ' in r)
            self.assertIn(expected, row, age)

    def test_protected_groups_do_not_qualify(self):
        out = self.run_cli('list', '--tier', 'protected', '--older-than', '0')[1]
        for row in out.split('\n')[2:-2]:
            self.assertIn(' - ', row)

    def test_items_of_a_group_with_decisions(self):
        code, out, _ = self.run_cli('list', '--group', 'claude.logs', '--older-than', '3d')
        self.assertEqual(code, 0)
        self.assertIn('Claude logs  [claude.logs]', out)
        self.assertIn(LOG, out)
        self.assertIn(item_key(LOG), out)
        self.assertIn('Selected (Delete)', out)
        self.assertIn('Decisions use an age filter of 3 days.', out)
        out = self.run_cli('list', '--group', 'claude.logs', '--older-than', '14d')[1]
        self.assertIn('Too recent', out)
        self.assertIn('5.0', out)
        out = self.run_cli('list', '--group', 'claude.logs', '--older-than', '0')[1]
        self.assertIn('Selected (Delete)', out)
        out = self.run_cli('list', '--group', 'claude.logs', '--older-than', '5d')[1]
        self.assertIn('Selected (Delete)', out)      # exactly as old as the filter still qualifies

    def test_floor_of_a_rule_is_shown(self):
        self.fh.file('~/.claude/debug/new.txt', size=10, age_days=0.2)
        self.scanned()
        out = self.run_cli('list', '--group', 'claude.logs', '--older-than', '0')[1]
        self.assertIn('Too recent for this rule', out)

    def test_group_items_json_and_report_only_hint(self):
        data = json.loads(self.run_cli('list', '--group', 'claude.logs', '--json', '--older-than', '1d')[1])
        self.assertEqual((data['group'], data['total'], data['min_age_days']), ('claude.logs', 1, 1))
        self.assertEqual(data['rows'][0]['decision']['state'], 'selected')
        out = self.run_cli('list', '--group', 'ollama.models')[1]
        self.assertIn('ollama rm <model>', out)
        self.assertIn('No cleanup action', out)

    def test_protected_items(self):
        out = self.run_cli('list', '--group', 'prot.databases:agent')[1]
        self.assertIn('Protected', out)

    def test_limit_for_items(self):
        out = self.run_cli('list', '--group', 'claude.electron.cache', '--limit', '1')[1]
        self.assertIn('Showing 1 of 3 items.', out)

    def test_unknown_group_and_bad_age(self):
        code, _, err = self.run_cli('list', '--group', 'no.such')
        self.assertEqual(code, 2)
        self.assertIn('Error [NOT_FOUND]', err)
        self.assertEqual(self.run_cli('list', '--older-than', 'soon')[0], 2)
        self.assertEqual(self.run_cli('list', '--tier', 'gold')[0], 2)


class PlanCommandTests(CommandCase):
    def setUp(self):
        super().setUp()
        self.scanned()

    def test_dry_run_creates_nothing(self):
        code, out, err = self.run_cli('plan', '--select', 'junk', '--older-than', '4d', '--dry-run')
        self.assertEqual((code, err), (0, ''))
        self.assertIn('Dry run: no plan was created.', out)
        self.assertIn('Selected 6 of', out)
        self.assertIn('frees', out)
        self.assertIn('Qualify', out)
        self.assertIn('Left out', out)
        self.assertEqual(self.plan_ids(), [])

    def test_plan_is_created_and_can_be_shown(self):
        code, out, _ = self.run_cli('plan', '--select', 'junk', '--older-than', '4d')
        self.assertEqual(code, 0)
        (plan_id,) = self.plan_ids()
        self.assertIn(f'Plan {plan_id} created.', out)
        self.assertIn(f'disk-steward show {plan_id} --entries', out)
        self.assertIn(f'disk-steward apply {plan_id}', out)
        code, out, _ = self.run_cli('show', plan_id)
        self.assertEqual(code, 0)
        self.assertIn(f'Plan {plan_id}: Draft.', out)
        self.assertIn('Pending', out)
        self.assertNotIn('Seq', out)
        code, out, _ = self.run_cli('show', plan_id, '--entries', '--limit', '2')
        self.assertIn('Showing 2 of 6 entries.', out)
        self.assertIn('Delete', out)
        code, out, _ = self.run_cli('show', plan_id, '--state', 'excluded')
        self.assertIn('Showing 0 of 0 entries.', out)

    def test_plan_json(self):
        code, out, _ = self.run_cli('plan', '--select', 'junk', '--older-than', '4d', '--json')
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual((data['dry_run'], data['plan']['entries']['total'], data['summary']['count']), (False, 6, 6))
        self.assertEqual(data['plan']['totals']['count'], 6)
        self.assertEqual(data['digest'], json.loads(out)['digest'])
        code, out, _ = self.run_cli('plan', '--select', 'junk', '--older-than', '4d', '--json', '--dry-run')
        data = json.loads(out)
        self.assertEqual((data['dry_run'], data['plan']), (True, None))
        self.assertEqual(len(self.plan_ids()), 1)

    def test_show_json_and_entries(self):
        self.run_cli('plan', '--select', 'junk', '--older-than', '4d')
        (plan_id,) = self.plan_ids()
        data = json.loads(self.run_cli('show', plan_id, '--json')[1])
        self.assertEqual((data['plan']['id'], data['entries']), (plan_id, None))
        rows = self.entries_of(plan_id)
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(r['action'] == 'delete' and r['state'] == 'pending' for r in rows))
        pending = json.loads(self.run_cli('show', plan_id, '--state', 'pending', '--limit', '2', '--json')[1])['entries']
        self.assertEqual((pending['total'], len(pending['rows'])), (6, 2))

    def test_an_include_younger_than_the_threshold_is_included_anyway(self):
        code, out, err = self.run_cli('plan', '--select', 'junk', '--older-than', '14d', '--include', item_key(LOG),
                                      '--action', 'delete', '--dry-run')
        self.assertEqual(code, 0)
        self.assertIn('Selected 4 of 49 scanned items', out)
        self.assertNotIn('Not selected although you included it', err)

    def test_selecting_a_protected_item_names_the_reason_in_words(self):
        code, out, err = self.run_cli('plan', '--select', 'junk', '--older-than', '14d', '--include',
                                      item_key('~/.claude/plugins/cache/mkt/pluginA/1.0.0'))
        self.assertEqual(code, 2)
        self.assertIn('cannot be selected (Protected data.', err)
        self.assertNotIn('(protected)', err)

    def test_show_names_groups_in_warnings(self):
        self.run_cli('plan', '--select', 'junk', '--older-than', '4d')
        (plan_id,) = self.plan_ids()
        out = self.run_cli('show', plan_id)[1]
        self.assertIn('Warning: Claude Desktop caches: none of its 3 items qualifies right now', out)
        self.assertNotIn('claude.electron.cache:', out)

    def test_show_unknown_plan(self):
        code, _, err = self.run_cli('show', 'nope')
        self.assertEqual(code, 2)
        self.assertIn('Error [NOT_FOUND]: Not found: plan nope.', err)

    def test_exclude_by_basename_and_by_path_glob(self):
        base = json.loads(self.run_cli('plan', '--select', 'junk', '--older-than', '4d', '--dry-run', '--json')[1])['summary']['count']
        self.run_cli('plan', '--select', 'junk', '--older-than', '4d', '--exclude', '*.log')
        paths = {r['path'] for r in self.entries_of(self.plan_ids()[-1])}
        self.assertNotIn(CODEX_LOG, paths)
        self.assertEqual(len(paths), base - 1)
        self.run_cli('plan', '--select', 'junk', '--older-than', '4d', '--exclude', '~/.codex/**')
        paths = {r['path'] for r in self.entries_of(self.plan_ids()[-1])}
        self.assertFalse([p for p in paths if p.startswith('~/.codex/')])
        self.assertIn(LOG, paths)
        self.run_cli('plan', '--select', 'junk', '--older-than', '4d', '--exclude', 'x.txt', '--exclude', '*.json')
        paths = {r['path'] for r in self.entries_of(self.plan_ids()[-1])}
        self.assertNotIn(LOG, paths)
        self.assertNotIn('~/.codex/cache/models.json', paths)

    def test_exclude_only_touches_selected_items(self):
        out = self.run_cli('plan', '--select', 'junk', '--older-than', '4d', '--exclude', '*', '--dry-run')
        self.assertEqual(out[0], 2)       # everything excluded: nothing left

    def test_include_adds_an_item_explicitly(self):
        solo = item_key(fx.SOLO)
        code, out, err = self.run_cli('plan', '--select', 'junk', '--older-than', '4d', '--include', solo)
        self.assertEqual((code, err), (0, ''))
        rows = {r['path']: r for r in self.entries_of(self.plan_ids()[-1])}
        self.assertEqual(rows[fx.SOLO]['action'], 'archive')
        self.assertEqual(len(rows), 7)

    def test_include_that_cannot_be_selected_is_reported(self):
        # Only the rule floor (1 day for claude.logs) can hold back an explicit include.
        self.fh.set_age(LOG, 0.5)
        self.scanned()
        code, out, err = self.run_cli('plan', '--select', 'junk', '--older-than', '14d', '--include', item_key(LOG))
        self.assertEqual(code, 0)
        self.assertIn(f'Not selected although you included it: {LOG}', err)
        self.assertIn('Changed 0.5 days ago. This rule never removes anything newer than 1 days.', err)
        code, _, err = self.run_cli('plan', '--select', 'junk', '--older-than', '14d', '--include', 'f' * 20)
        self.assertEqual(code, 2)
        self.assertIn('NOT_FOUND', err)

    def test_action_for_groups(self):
        code, *_ = self.run_cli('plan', '--select', 'codex.sessions:~/.codex/sessions/2026/10', '--action', 'delete')
        self.assertEqual(code, 0)
        self.assertEqual({r['action'] for r in self.entries_of(self.plan_ids()[-1])}, {'delete'})
        code, _, err = self.run_cli('plan', '--select', 'codex.sessions:~/.codex/sessions/2026/10', '--action', 'trash')
        self.assertEqual(code, 2)
        self.assertIn('Error [INVALID]', err)
        self.assertIn("'trash' is not available for codex.sessions", err)
        self.assertEqual(self.run_cli('plan', '--select', 'junk', '--action', 'shred')[0], 2)

    def test_action_that_junk_groups_lack_fails_instead_of_skipping(self):
        code, _, err = self.run_cli('plan', '--select', 'junk', '--action', 'archive', '--older-than', '0')
        self.assertEqual(code, 2)
        self.assertIn("'archive' is not available for", err)
        self.assertEqual(self.plan_ids(), [])

    def test_group_keys_mix_with_junk(self):
        code, out, _ = self.run_cli('plan', '--select', 'junk', '--select', 'codex.sessions:duplicates',
                                    '--select', 'codex.sessions:~/.codex/sessions/2026/10', '--older-than', '14d',
                                    '--dry-run')
        self.assertEqual(code, 0)
        self.assertIn('Archive', out)
        self.assertIn('Warning: 1 Codex session(s) are selected together with their duplicate copy.', out)

    def test_nothing_qualifies(self):
        code, out, err = self.run_cli('plan', '--select', 'claude.logs', '--older-than', '14d')
        self.assertEqual((code, out), (2, ''))
        self.assertIn('Error [EMPTY_PLAN]: Nothing qualifies for cleanup with the current selection.', err)
        self.assertIn('Too recent', err)
        self.assertIn('Claude logs', err)                       # the group table says which group is empty
        self.assertIn('0 of 1', err)
        self.assertIn('none of its 1 items qualifies', err)
        self.assertEqual(self.plan_ids(), [])
        self.assertEqual(self.run_cli('plan', '--select', 'claude.logs', '--older-than', '14d', '--dry-run')[0], 2)

    def test_nothing_qualifies_as_json(self):
        code, out, err = self.run_cli('plan', '--select', 'claude.logs', '--older-than', '14d', '--json')
        self.assertEqual((code, err), (2, ''))
        error = json.loads(out)['error']
        self.assertEqual(error['code'], 'EMPTY_PLAN')
        self.assertEqual(error['details']['breakdown']['too_recent']['count'], 1)

    def test_protected_group_selected_by_mistake(self):
        code, _, err = self.run_cli('plan', '--select', 'prot.databases:agent', '--older-than', '0')
        self.assertEqual(code, 2)
        self.assertIn('EMPTY_PLAN', err)
        self.assertIn('Protected', err)

    def test_unknown_group(self):
        code, _, err = self.run_cli('plan', '--select', 'no.such.group')
        self.assertEqual(code, 2)
        self.assertIn('Not found: group no.such.group.', err)

    def test_select_is_required(self):
        self.assertEqual(self.run_cli('plan')[0], 2)

    def test_plan_builds_from_scratch_each_time(self):
        self.run_cli('plan', '--select', 'junk', '--older-than', '4d', '--exclude', '*.log')
        self.run_cli('plan', '--select', 'junk', '--older-than', '4d')
        paths = {r['path'] for r in self.entries_of(self.plan_ids()[-1])}
        self.assertIn(CODEX_LOG, paths)


class LanguageAndFactoryTests(CommandCase):
    def test_lang_before_or_after_the_command(self):
        self.scanned()
        for argv in (('--lang', 'zh-CN', 'report'), ('report', '--lang', 'zh-CN')):
            code, out, _ = self.run_cli(*argv)
            self.assertEqual(code, 0)
            self.assertIn('按等级统计', out)
        self.assertEqual(self.app.locale, 'zh-CN')
        self.assertEqual(self.run_cli('report', '--lang', 'fr')[0], 2)

    def test_every_command_speaks_both_languages_without_missing_keys(self):
        before = set(i18n.MISSING)
        for lang in ('en', 'zh-CN'):
            self.app.locale = lang
            outputs = [self.run_cli('--lang', lang, 'scan'), self.run_cli('--lang', lang, 'report'),
                       self.run_cli('--lang', lang, 'report', '--since-last'), self.run_cli('--lang', lang, 'list'),
                       self.run_cli('--lang', lang, 'list', '--group', 'claude.logs'),
                       self.run_cli('--lang', lang, 'plan', '--select', 'junk', '--older-than', '4d', '--dry-run'),
                       self.run_cli('--lang', lang, 'plan', '--select', 'junk', '--older-than', '4d'),
                       self.run_cli('--lang', lang, 'plan', '--select', 'claude.logs'),
                       self.run_cli('--lang', lang, 'plan', '--select', 'junk', '--older-than', '14d', '--include',
                                    item_key(LOG)),
                       self.run_cli('--lang', lang, 'show', self.plan_ids()[0], '--entries'),
                       self.run_cli('--lang', lang, 'show', 'nope')]
            for code, out, err in outputs:
                for text in (out, err):
                    self.assertNotIn('cli.', text)
                    self.assertNotIn('decision.', text)
                    self.assertNotIn('entry.', text)
                    self.assertNotIn('error.', text)
        self.assertEqual(set(i18n.MISSING), before)

    def test_default_factory_uses_data_dir_option_and_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'dd'
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(['--data-dir', str(target), '--lang', 'en', 'report'])
            self.assertEqual(code, 2)
            self.assertIn('No scan yet', err.getvalue())
            self.assertTrue((target / 'steward.db').exists())
            other = Path(tmp) / 'env'
            with mock.patch.dict(os.environ, {'DISK_STEWARD_DATA_DIR': str(other)}), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(['report', '--lang', 'en'])
            self.assertEqual(code, 2)
            self.assertTrue((other / 'steward.db').exists())

    def test_unexpected_exceptions_propagate(self):
        self.scanned()
        with mock.patch.object(type(self.app), 'overview', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                self.run_cli('report')

    def test_corrupt_settings_are_an_expected_error(self):
        (self.fh.data_dir / 'settings.json').write_text('{not json')
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--data-dir', str(self.fh.data_dir), '--lang', 'en', 'report'])
        self.assertEqual(code, 2)
        self.assertIn('settings.json is not valid', err.getvalue())


class ParseAgeTests(unittest.TestCase):
    def test_forms(self):
        for text, days in (('14d', 14), ('36h', 1.5), ('0', 0), ('7', 7), ('0.5d', 0.5), ('48h', 2), (' 3d ', 3)):
            with self.subTest(text=text):
                self.assertEqual(parse_age(text), days)

    def test_bad_forms(self):
        import argparse
        for text in ('', 'd', '-1d', '1w', 'abc', '1.d', '1d2h'):
            with self.subTest(text=text), self.assertRaises(argparse.ArgumentTypeError):
                parse_age(text)


class TtyIn(io.StringIO):
    def isatty(self):
        return True


RUN_LOG = '~/.codex/log/codex-tui.log'
PROJECT_FILE = '~/.claude/projects/-Users-me-proj/a.jsonl'


class RunCase(CommandCase):
    """The fixture is scanned and a three-entry plan (two files and a folder, all junk older than 14 days) exists."""

    def setUp(self):
        super().setUp()
        self.scanned()
        code, out, _ = self.run_cli('plan', '--select', 'junk', '--older-than', '14d')
        self.assertEqual(code, 0)
        (self.plan_id,) = self.plan_ids()
        self.paths = [r['path'] for r in self.entries_of(self.plan_id)]
        self.assertEqual(len(self.paths), 3)

    def gone(self):
        return [p for p in self.paths if not os.path.lexists(self.fh.path(p))]

    def run_with_stdin(self, text, *argv, tty=True, app=None, stderr=None):
        stdin = TtyIn(text) if tty else io.StringIO(text)
        with mock.patch('sys.stdin', stdin):
            return self.run_cli(*argv, app=app, stderr=stderr)


class ApplyCommandTests(RunCase):
    def test_yes_runs_the_plan_and_prints_the_summary(self):
        code, out, err = self.run_with_stdin('', 'apply', self.plan_id, '--yes', tty=False)
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(len(self.gone()), 3)
        self.assertIn(f'Plan {self.plan_id}: 3 entries pending', out)
        self.assertIn('Rules and settings are unchanged', out)
        self.assertIn(f'Plan {self.plan_id} finished in', out)
        self.assertIn('Entries by outcome', out)
        self.assertRegex(out, r'Done\s+3')
        self.assertIn('Removed 12.00 KiB, freed about 12.00 KiB, moved to the Trash 0 B.', out)
        self.assertIn('Free space: estimated change +12.00 KiB, measured change 0 B.', out)
        self.assertEqual(self.app.latest_scan()['id'], 2)          # rescanned afterwards
        code, out, _ = self.run_cli('show', self.plan_id)
        self.assertIn('Done', out)

    def test_json(self):
        code, out, err = self.run_with_stdin('', 'apply', self.plan_id, '--yes', '--json', tty=False)
        self.assertEqual((code, err), (0, ''))
        data = json.loads(out)
        self.assertEqual(set(data), {'plan', 'preflight', 'summary'})
        self.assertEqual(data['summary']['counts'], {'done': 3})
        self.assertTrue(data['preflight']['ready'])

    def test_without_yes_a_non_terminal_is_refused(self):
        code, out, err = self.run_with_stdin('3\n', 'apply', self.plan_id, tty=False)
        self.assertEqual(code, 2)
        self.assertIn('Error [INVALID]', err)
        self.assertIn('Pass --yes', err)
        self.assertEqual(self.gone(), [])
        self.assertEqual(self.app.job()['status'], 'idle')

    def test_confirmation_is_the_number_of_entries(self):
        code, out, err = self.run_with_stdin('2\n', 'apply', self.plan_id)
        self.assertEqual(code, 2)
        self.assertIn('Type the number of entries (3) to confirm: ', err)
        self.assertIn('did not match', err)
        self.assertEqual(self.gone(), [])
        code, out, err = self.run_with_stdin('', 'apply', self.plan_id)       # EOF
        self.assertEqual(code, 2)
        self.assertEqual(self.gone(), [])
        code, out, err = self.run_with_stdin('3\n', 'apply', self.plan_id)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.gone()), 3)

    def test_json_keeps_the_prompt_off_stdout(self):
        code, out, err = self.run_with_stdin('3\n', 'apply', self.plan_id, '--json')
        self.assertEqual(code, 0)
        json.loads(out)
        self.assertIn('Type the number of entries', err)

    def test_blockers_are_shown_before_the_confirmation(self):
        self.app.mac.running = {'com.anthropic.claudefordesktop'}
        code, out, err = self.run_cli('plan', '--select', 'junk', '--older-than', '0')
        plan_id = self.plan_ids()[-1]
        code, out, err = self.run_with_stdin('', 'apply', plan_id, '--yes', tty=False)
        self.assertEqual(code, 0)
        self.assertIn('Quit Claude Desktop to include 4 entries (', out)
        self.assertIn('Claude Desktop is running', out)         # in the skipped table
        self.assertRegex(out, r'Skipped\s+4')
        self.assertTrue(os.path.exists(self.fh.path('~/Library/Application Support/Claude/Cache/data_0')))
        self.assertFalse(os.path.exists(self.fh.path('~/.claude/debug/x.txt')))

    def test_open_files_are_listed_as_skipped(self):
        from disk_steward.macos import OpenFiles
        st = os.stat(self.fh.path(RUN_LOG))
        self.app.mac.open = OpenFiles(by_inode={(st.st_dev, st.st_ino): ['tail(77)']})
        self.run_cli('plan', '--select', 'junk', '--older-than', '0')
        plan_id = self.plan_ids()[-1]
        code, out, err = self.run_with_stdin('', 'apply', plan_id, '--yes', tty=False)
        self.assertIn('In use, will be skipped: open in tail(77): 1 entries', out)
        self.assertIn('In use: open in tail(77)', out)
        self.assertTrue(os.path.exists(self.fh.path(RUN_LOG)))

    def test_changed_settings_are_refused_before_asking(self):
        self.app.update_settings({'large_file_min_bytes': 1})
        code, out, err = self.run_with_stdin('3\n', 'apply', self.plan_id)
        self.assertEqual(code, 2)
        # The size threshold is also built into the user-files rules, so the catalog changes with it.
        self.assertIn('The rules or settings changed after the scan this plan is based on (catalog, settings). '
                      'Scan again, then create a new plan.', out)
        self.assertIn('Error [RULES_CHANGED]', err)
        self.assertNotIn('Type the number', err)
        self.assertEqual(self.gone(), [])

    def test_a_started_plan_is_not_refused_for_changed_facts(self):
        self.app.store.set_plan_state(self.plan_id, PlanState.PAUSED)
        link = self.fh.path('~/.local/bin/claude')
        os.unlink(link)
        os.symlink(self.fh.path('~/.local/share/claude/versions/2.1.289'), link)
        code, out, err = self.run_with_stdin('', 'apply', self.plan_id, '--yes', tty=False)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.gone()), 3)

    def test_expired_plan(self):
        self.fh.clock.advance(days=2)
        code, out, err = self.run_with_stdin('', 'apply', self.plan_id, '--yes', tty=False)
        self.assertEqual(code, 2)
        self.assertIn('The plan has expired.', out)
        self.assertIn('Error [PLAN_EXPIRED]', err)

    def test_a_finished_plan_cannot_run_again(self):
        self.run_with_stdin('', 'apply', self.plan_id, '--yes', tty=False)
        code, out, err = self.run_with_stdin('', 'apply', self.plan_id, '--yes', tty=False)
        self.assertEqual(code, 2)
        self.assertIn('This plan is done and cannot be run.', err)

    def test_unknown_plan(self):
        code, out, err = self.run_with_stdin('', 'apply', 'nope', '--yes', tty=False)
        self.assertEqual(code, 2)
        self.assertIn('Error [NOT_FOUND]', err)

    def test_busy_when_another_operation_holds_the_lock(self):
        with self.app.lock:
            code, out, err = self.run_with_stdin('', 'apply', self.plan_id, '--yes', tty=False)
        self.assertEqual(code, 3)
        self.assertEqual(self.gone(), [])

    def test_progress_is_one_updating_line_on_a_terminal(self):
        err = TtyIO()
        code, out, _ = self.run_with_stdin('', 'apply', self.plan_id, '--yes', tty=False, stderr=err)
        self.assertEqual(code, 0)
        text = err.getvalue()
        self.assertIn('\r\x1b[2K', text)
        self.assertRegex(text, r'\d+/3 entries, ')
        self.assertNotIn('\n', text)
        self.assertTrue(text.endswith('\r\x1b[2K'))
        self.assertNotIn('entries,', out)

    def test_no_progress_when_stderr_is_not_a_terminal(self):
        self.assertEqual(self.run_with_stdin('', 'apply', self.plan_id, '--yes', tty=False)[2], '')

    def test_both_languages_without_missing_keys(self):
        before = set(i18n.MISSING)
        for lang in ('en', 'zh-CN'):
            self.app.locale = lang
            code, out, err = self.run_cli('plan', '--select', 'junk', '--older-than', '14d')
            plan_id = self.plan_ids()[-1]
            self.app.mac.running = {'com.anthropic.claudefordesktop'}
            outputs = [self.run_with_stdin('', '--lang', lang, 'apply', plan_id, '--yes', tty=False),
                       self.run_with_stdin('', '--lang', lang, 'apply', plan_id, '--yes', tty=False),
                       self.run_with_stdin('', '--lang', lang, 'apply', 'nope', '--yes', tty=False),
                       self.run_with_stdin('', '--lang', lang, 'archives', 'list', tty=False)]
            for code, out, err in outputs:
                for text in (out, err):
                    for marker in ('cli.', 'skip.', 'blocker.', 'warning.', 'error.', 'entry.'):
                        self.assertNotIn(marker, text)
            self.scanned()
        self.assertEqual(set(i18n.MISSING), before)

    def test_summary_reasons_are_translated(self):
        self.app.locale = 'zh-CN'
        self.fh.file(self.paths[0], size=1)   # the file was rewritten after the plan
        code, out, err = self.run_with_stdin('', '--lang', 'zh-CN', 'apply', self.plan_id, '--yes', tty=False)
        self.assertEqual(code, 0)
        self.assertIn('自计划生成后已发生变化', out)


class QuitAppFromEvidenceTests(CommandCase):
    def test_the_preflight_asks_for_the_app_the_scan_found(self):
        from disk_steward.settings import Settings
        support = '~/Library/Application Support'
        self.fh.file(f'{support}/Notion/Cache/Cache_Data/a', size=100, age_days=30)
        self.fh.file(f'{support}/Notion/GPUCache/data_0', size=100, age_days=30)
        info = {'path': '/Applications/Notion.app', 'name': 'Notion', 'display_name': '', 'executable': '',
                'stem': 'Notion'}
        from m3_support import make_mac
        app = make_app(self.fh, mac=make_mac(app_info={'notion.id': info}, running=['notion.id']),
                       settings=Settings(domains=('agent', 'app-library')))
        self.addCleanup(app.close)
        self.assertEqual(self.run_cli('scan', app=app)[0], 0)
        code, out, err = self.run_cli('plan', '--select', f'app.electron.cache:{support}/Notion', '--older-than', '0',
                                      app=app)
        self.assertEqual(code, 0, err)
        con = sqlite3.connect(self.fh.data_dir / 'steward.db')
        try:
            plan_id = con.execute('SELECT id FROM plans').fetchone()[0]
        finally:
            con.close()
        with mock.patch('sys.stdin', io.StringIO('')):
            code, out, err = self.run_cli('apply', plan_id, '--yes', app=app)
        self.assertEqual(code, 0)
        self.assertIn('Quit Notion to include 2 entries (', out)
        self.assertIn('In use: notion.id is running', out)
        self.assertTrue(os.path.exists(self.fh.path(f'{support}/Notion/Cache/Cache_Data/a')))


class InterruptTests(CommandCase):
    """Ctrl-C while a plan runs. The executor is held at the second entry until the CLI has asked for the pause."""

    def setUp(self):
        super().setUp()
        self.scanned()
        self.app.close()
        self.hold = True

        def interrupt(entry):
            if entry.seq == 2 and self.hold:
                self.hold = False
                os.kill(os.getpid(), signal.SIGINT)
                deadline = time.monotonic() + 30
                while not self.app.job()['pause_requested'] and time.monotonic() < deadline:
                    time.sleep(0.005)

        self.app = make_app(self.fh, hooks={'after_claim': interrupt})
        self.addCleanup(self.app.close)
        self.run_cli('plan', '--select', 'junk', '--older-than', '14d')
        (self.plan_id,) = self.plan_ids()
        self.paths = [r['path'] for r in self.entries_of(self.plan_id)]

    def run_interrupted(self, answers, tty=True):
        stdin = TtyIn(answers) if tty else io.StringIO(answers)
        with mock.patch('sys.stdin', stdin):
            return self.run_cli('apply', self.plan_id, '--yes')

    def test_exclude_then_continue(self):
        target = self.paths[2]
        code, out, err = self.run_interrupted(f'e {target.rpartition("/")[2]}\nc\n')
        self.assertEqual(code, 0)
        self.assertIn('Paused. [c]ontinue / [e]xclude GLOB / [a]bort: ', err)
        self.assertIn('Excluded 1 pending entries.', err)
        self.assertTrue(os.path.lexists(self.fh.path(target)))
        self.assertEqual(self.app.plan(self.plan_id)['entries']['by_state'], {'done': 2, 'excluded': 1})
        self.assertIn('Plan ' + self.plan_id + ' finished', out)
        self.assertEqual(self.app.plan(self.plan_id)['revision'], 2)

    def test_a_glob_that_matches_nothing_is_reported_and_asked_again(self):
        code, out, err = self.run_interrupted('e *.nothing\nc\n')
        self.assertEqual(code, 0)
        self.assertIn('No pending entry matches *.nothing.', err)
        self.assertEqual(self.app.plan(self.plan_id)['entries']['by_state'], {'done': 3})

    def test_continue(self):
        code, out, err = self.run_interrupted('x\nc\n')
        self.assertEqual(code, 0)
        self.assertEqual(self.app.plan(self.plan_id)['entries']['by_state'], {'done': 3})

    def test_abort_cancels_the_rest(self):
        code, out, err = self.run_interrupted('a\n')
        self.assertEqual(code, 1)
        self.assertIn(f'Plan {self.plan_id} was cancelled.', out)
        self.assertEqual(self.app.plan(self.plan_id)['entries']['by_state'], {'done': 2, 'cancelled': 1})
        self.assertEqual(self.app.plan(self.plan_id)['state'], 'cancelled')
        self.assertTrue(os.path.lexists(self.fh.path(self.paths[2])))

    def test_end_of_input_aborts(self):
        code, out, err = self.run_interrupted('')
        self.assertEqual(code, 1)
        self.assertEqual(self.app.plan(self.plan_id)['state'], 'cancelled')

    def test_without_a_terminal_ctrl_c_cancels(self):
        code, out, err = self.run_interrupted('c\n', tty=False)
        self.assertEqual(code, 1)
        self.assertIn('Interrupted. Cancelling the remaining entries.', err)
        self.assertEqual(self.app.plan(self.plan_id)['entries']['by_state'], {'done': 2, 'cancelled': 1})

    def test_the_previous_sigint_handler_is_restored(self):
        before = signal.getsignal(signal.SIGINT)
        self.run_interrupted('c\n')
        self.assertIs(signal.getsignal(signal.SIGINT), before)


BIG_PROJECT = '~/.claude/projects/-Users-me-proj/big.jsonl'


class ArchivesCommandTests(CommandCase):
    def setUp(self):
        super().setUp()
        self.big = self.fh.file(BIG_PROJECT, content=os.urandom(30_000), age_days=20)
        self.data = self.big.read_bytes()
        self.scanned()
        code, out, err = self.run_cli('plan', '--select', 'claude.projects:~/.claude/projects/-Users-me-proj',
                                      '--older-than', '14d')
        self.assertEqual(code, 0, err)
        (self.plan_id,) = self.plan_ids()
        with mock.patch('sys.stdin', io.StringIO('')):
            self.assertEqual(self.run_cli('apply', self.plan_id, '--yes')[0], 0)
        self.rows = {r['path']: r for r in self.app.archives()}
        self.archive_id = self.rows[BIG_PROJECT]['id']

    def test_list(self):
        code, out, err = self.run_cli('archives', 'list')
        self.assertEqual((code, err), (0, ''))
        for text in (self.archive_id, BIG_PROJECT, 'Stored', 'State', 'Created'):
            self.assertIn(text, out)
        self.assertEqual(len([l for l in out.split('\n') if '~/.claude/projects' in l]), 4)

    def test_list_json(self):
        data = json.loads(self.run_cli('archives', 'list', '--json')[1])
        self.assertEqual(len(data['archives']), 4)
        self.assertEqual({a['state'] for a in data['archives']}, {'stored'})

    def test_list_when_there_are_none(self):
        with FakeHome(Clock()) as fh:
            app = make_app(fh)
            try:
                code, out, _ = self.run_cli('archives', 'list', app=app)
            finally:
                app.close()
        self.assertEqual((code, out), (0, 'No archives.\n'))

    def test_restore(self):
        self.assertFalse(self.big.exists())
        code, out, err = self.run_cli('archives', 'restore', self.archive_id)
        self.assertEqual((code, err), (0, ''))
        self.assertIn(f'Restored {BIG_PROJECT} (', out)
        self.assertEqual(self.big.read_bytes(), self.data)
        self.assertIn('Restored', self.run_cli('archives', 'list')[1])

    def test_restore_never_overwrites(self):
        self.big.write_bytes(b'new')
        code, out, err = self.run_cli('archives', 'restore', self.archive_id)
        self.assertEqual(code, 2)
        self.assertIn('already exists', err)
        self.assertEqual(self.big.read_bytes(), b'new')

    def test_restore_unknown_id(self):
        code, out, err = self.run_cli('archives', 'restore', 'nope')
        self.assertEqual(code, 2)
        self.assertIn('Error [NOT_FOUND]', err)

    def test_purge_needs_confirmation(self):
        with mock.patch('sys.stdin', io.StringIO('y\n')):
            code, out, err = self.run_cli('archives', 'purge', self.archive_id)
        self.assertEqual(code, 2)
        self.assertIn('Pass --yes', err)
        with mock.patch('sys.stdin', TtyIn('n\n')):
            code, out, err = self.run_cli('archives', 'purge', self.archive_id)
        self.assertEqual(code, 2)
        self.assertEqual({a['state'] for a in self.app.archives()}, {'stored'})
        with mock.patch('sys.stdin', TtyIn('y\n')):
            code, out, err = self.run_cli('archives', 'purge', self.archive_id)
        self.assertEqual(code, 0)
        self.assertIn(f'Purged archive {self.archive_id}', out)
        self.assertIn('Delete archive', err)

    def test_purge_yes(self):
        code, out, err = self.run_cli('archives', 'purge', self.archive_id, '--yes')
        self.assertEqual((code, err), (0, ''))
        states = {a['id']: a['state'] for a in self.app.archives()}
        self.assertEqual(states[self.archive_id], 'purged')
        self.assertIn('Purged', self.run_cli('archives', 'list')[1])
        code, out, err = self.run_cli('archives', 'purge', self.archive_id, '--yes')
        self.assertEqual(code, 2)
        self.assertIn('is purged', err)

    def test_purge_unknown_id(self):
        code, out, err = self.run_cli('archives', 'purge', 'nope', '--yes')
        self.assertEqual(code, 2)
        self.assertIn('Error [NOT_FOUND]', err)

    def test_a_subcommand_is_required(self):
        self.assertEqual(self.run_cli('archives')[0], 2)


class DiagnoseCommandTests(CommandCase):
    def setUp(self):
        super().setUp()
        self.scanned()

    def test_text(self):
        code, out, err = self.run_cli('diagnose-session', item_key(fx.PAIR_A))
        self.assertEqual((code, err), (0, ''))
        self.assertIn(fx.PAIR_A, out)
        self.assertIn('21 lines, 0 unreadable', out)
        self.assertIn('session_meta', out)
        self.assertIn('Settings-applied events: 0', out)

    def test_json(self):
        data = json.loads(self.run_cli('diagnose-session', item_key(fx.PAIR_A), '--json')[1])
        self.assertEqual(data['lines'], 21)
        self.assertIn('by_type', data)

    def test_not_a_session(self):
        code, out, err = self.run_cli('diagnose-session', item_key(LOG))
        self.assertEqual(code, 2)
        self.assertIn('is not a Codex session file', err)

    def test_unknown_item(self):
        code, out, err = self.run_cli('diagnose-session', 'f' * 20)
        self.assertEqual(code, 2)
        self.assertIn('Error [NOT_FOUND]', err)

    def test_chinese(self):
        code, out, err = self.run_cli('--lang', 'zh-CN', 'diagnose-session', item_key(fx.PAIR_A))
        self.assertEqual(code, 0)
        self.assertIn('无法解析', out)
        self.assertNotIn('cli.', out)


class NoticeTests(CommandCase):
    def test_recovery_is_announced_on_stderr(self):
        self.scanned()
        self.app.recovery = {'entries': [{'seq': 1}, {'seq': 2}], 'plans': ['p'], 'parts_removed': 0}
        code, out, err = self.run_cli('report')
        self.assertEqual(code, 0)
        self.assertIn('Recovered 2 entries left by an interrupted run.', err)


class StaleSelectionTests(CommandCase):
    def test_spec_stale_names_what_the_carry_over_changed(self):
        self.scanned()
        seen = self.app.apply_ops([{'op': 'select_group', 'group': 'codex.logs', 'on': True},
                                   {'op': 'select_group', 'group': 'codex.cache', 'on': True},
                                   {'op': 'toggle_item', 'item': item_key(LOG), 'on': True}])
        os.unlink(self.fh.path('~/.codex/cache/models.json'))
        os.unlink(self.fh.path(LOG))
        first = seen['scan_id']
        self.scanned()
        from disk_steward.model import StewardError
        from disk_steward.cli import _print_error
        with self.assertRaises(StewardError) as cm:
            self.app.create_plan('default', {'spec_version': seen['spec']['version'], 'digest': seen['digest'],
                                             'scan_id': first})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _print_error(cm.exception, 'en', False)
        text = err.getvalue()
        self.assertIn('Error [INVALID]', text)
        self.assertIn('No longer in the scan, dropped from the selection: codex.cache', text)
        self.assertIn('1 individually chosen item(s) no longer exist and were dropped:', text)
        self.assertIn(f'    {LOG}', text)
        self.assertNotIn('notes:', text)


if __name__ == '__main__':
    unittest.main()
