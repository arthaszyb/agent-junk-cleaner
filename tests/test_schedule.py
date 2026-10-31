from __future__ import annotations

import contextlib
import io
import os
import plistlib
import shutil
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import m2_fixture as fx
from disk_steward import schedule
from disk_steward.cli import main, schedule_cmd
from disk_steward.report import fmt_bytes
from m3_support import make_app
from support import Clock, FakeHome

REAL_AGENTS = schedule.LAUNCH_AGENTS_DIR       # captured before any test patches it
LABEL = 'io.github.disk-steward.scan'

# A launchctl that keeps its "loaded" state in a file. print exits 113 and bootout exits 3 for a service that is
# not loaded, as the real one does. `<verb>.fail` files turn that verb into a failure with the file's text.
LAUNCHCTL = """#!/bin/sh
S='{state}'
printf '%s\\n' "$*" >> "$S/launchctl.calls"
if [ -e "$S/$1.fail" ]; then cat "$S/$1.fail" >&2; exit 5; fi
case "$1" in
print)
  if [ -e "$S/loaded" ]; then exit 0; fi
  echo 'Could not find service "{label}" in domain for user gui: 501' >&2
  exit 113;;
bootstrap) : > "$S/loaded"; exit 0;;
bootout)
  if [ -e "$S/loaded" ]; then rm "$S/loaded"; exit 0; fi
  echo 'Boot-out failed: 3: No such process' >&2
  exit 3;;
esac
echo "unexpected: $*" >&2
exit 99
"""

# Writes the arguments NUL-separated to a file. `osascript.fail` makes it fail with the file's text, and
# `osascript.sleep` makes it hang (exec, so the timeout kills the sleeper itself).
OSASCRIPT = """#!/bin/sh
S='{state}'
printf '%s\\0' "$@" > "$S/osascript.args"
if [ -e "$S/osascript.sleep" ]; then exec sleep 5; fi
if [ -e "$S/osascript.fail" ]; then cat "$S/osascript.fail" >&2; exit 1; fi
exit 0
"""


class FakeTools(unittest.TestCase):
    """Temp LaunchAgents folder, fake launchctl and fake osascript, patched into `schedule`."""

    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix='ds-sched-')))
        self.addCleanup(shutil.rmtree, self.tmp)       # only the temp folder created above
        self.agents = self.tmp / 'LaunchAgents'
        self.state = self.tmp / 'state'
        self.state.mkdir()
        (self.tmp / 'bin').mkdir()
        self.launchctl = self.fake('launchctl', LAUNCHCTL.format(state=self.state, label=LABEL))
        self.osascript = self.fake('osascript', OSASCRIPT.format(state=self.state))
        for name, value in (('LAUNCH_AGENTS_DIR', self.agents), ('LAUNCHCTL', str(self.launchctl)),
                            ('OSASCRIPT', str(self.osascript))):
            p = mock.patch.object(schedule, name, value)
            p.start()
            self.addCleanup(p.stop)
        agents_mtime = self._mtime(REAL_AGENTS)
        self.addCleanup(lambda: self.assertEqual(self._mtime(REAL_AGENTS), agents_mtime,
                                                 'the real LaunchAgents folder was touched'))
        self.uid = os.getuid()
        self.plist = self.agents / f'{LABEL}.plist'

    @staticmethod
    def _mtime(path):
        try:
            return os.stat(path).st_mtime_ns
        except FileNotFoundError:
            return None

    def fake(self, name, body):
        path = self.tmp / 'bin' / name
        path.write_text(body, encoding='utf-8')
        path.chmod(0o755)
        return path

    def calls(self):
        try:
            return (self.state / 'launchctl.calls').read_text().splitlines()
        except FileNotFoundError:
            return []

    def set_loaded(self, on=True):
        (self.state / 'loaded').touch() if on else (self.state / 'loaded').unlink()

    def fail_verb(self, verb, text):
        (self.state / f'{verb}.fail').write_text(text)

    def parsed(self):
        with open(self.plist, 'rb') as f:
            return plistlib.load(f)

    def osascript_args(self):
        raw = (self.state / 'osascript.args').read_bytes().decode('utf-8')
        return raw.split('\0')[:-1]


SRC_TREE = Path('/work/disk-steward/src/disk_steward')
INSTALLED = Path('/x/lib/python3.9/site-packages/disk_steward')


class InstallTests(FakeTools):
    def install(self, data_dir=None, weekday=1, hour=10, pass_data_dir=False, lang='en', package=SRC_TREE):
        with mock.patch.object(schedule, 'PACKAGE_DIR', package):
            return schedule.install(data_dir or self.tmp / 'data', weekday, hour, pass_data_dir, lang)

    def test_writes_the_expected_plist_and_bootstraps_it(self):
        data = self.tmp / 'data'
        self.install(data, weekday=3, hour=7, pass_data_dir=True, lang='zh-CN')
        plist = self.parsed()
        log = str(data / 'logs' / 'schedule.log')
        self.assertEqual(plist, {
            'Label': LABEL,
            'ProgramArguments': [sys.executable, '-m', 'disk_steward', 'scan', '--notify', '--data-dir', str(data),
                                 '--lang', 'zh-CN'],
            'StartCalendarInterval': {'Weekday': 3, 'Hour': 7, 'Minute': 0},
            'StandardOutPath': log, 'StandardErrorPath': log,
            'ProcessType': 'Background', 'LowPriorityIO': True,
            'EnvironmentVariables': {'PYTHONPATH': '/work/disk-steward/src'},
        })
        self.assertEqual(stat.S_IMODE(os.stat(self.plist).st_mode), 0o644)
        self.assertEqual(os.listdir(self.agents), [f'{LABEL}.plist'])           # no temp file left behind
        self.assertTrue((data / 'logs').is_dir())
        self.assertEqual(self.calls(), [f'print gui/{self.uid}/{LABEL}', f'bootstrap gui/{self.uid} {self.plist}'])

    def test_the_standard_data_dir_is_not_passed_on(self):
        self.install(pass_data_dir=False)
        self.assertEqual(self.parsed()['ProgramArguments'],
                         [sys.executable, '-m', 'disk_steward', 'scan', '--notify', '--lang', 'en'])

    def test_an_installed_package_needs_no_pythonpath_a_source_tree_does(self):
        self.install(package=INSTALLED)
        self.assertNotIn('EnvironmentVariables', self.parsed())
        self.install(package=Path('/usr/lib/python3/dist-packages/disk_steward'))
        self.assertNotIn('EnvironmentVariables', self.parsed())
        self.install(package=SRC_TREE)
        self.assertEqual(self.parsed()['EnvironmentVariables'], {'PYTHONPATH': '/work/disk-steward/src'})

    def test_this_checkout_is_a_source_tree_unless_it_sits_in_site_packages(self):
        here = Path(schedule.__file__).resolve().parent
        expected = None if 'site-packages' in here.parts or 'dist-packages' in here.parts else str(here.parent)
        self.assertEqual(schedule._source_root(), expected)

    def test_reinstall_boots_the_loaded_job_out_first(self):
        self.install(weekday=1, hour=10)
        self.install(weekday=5, hour=18)
        target = f'gui/{self.uid}/{LABEL}'
        self.assertEqual(self.calls(), [
            f'print {target}', f'bootstrap gui/{self.uid} {self.plist}',
            f'print {target}', f'bootout {target}', f'bootstrap gui/{self.uid} {self.plist}'])
        self.assertEqual(self.parsed()['StartCalendarInterval'], {'Weekday': 5, 'Hour': 18, 'Minute': 0})
        self.assertEqual(schedule.status(self.tmp / 'data')['loaded'], True)

    def test_a_relative_data_dir_becomes_absolute(self):
        old = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, old)
        self.install(Path('rel-data'), pass_data_dir=True)
        args = self.parsed()['ProgramArguments']
        self.assertEqual(args[args.index('--data-dir') + 1], str(self.tmp / 'rel-data'))
        self.assertEqual(self.parsed()['StandardOutPath'], str(self.tmp / 'rel-data' / 'logs' / 'schedule.log'))

    def test_bootstrap_failure_raises_with_launchctl_stderr(self):
        self.fail_verb('bootstrap', 'Bootstrap failed: 5: Input/output error')
        with self.assertRaises(RuntimeError) as cm:
            self.install()
        self.assertIn('Input/output error', str(cm.exception))
        self.assertFalse((self.state / 'loaded').exists())

    def test_bootout_failure_during_reinstall_raises(self):
        self.install()
        self.fail_verb('bootout', 'Boot-out failed: 5: Input/output error')
        with self.assertRaises(RuntimeError) as cm:
            self.install()
        self.assertIn('Input/output error', str(cm.exception))
        self.assertNotIn('bootstrap', ' '.join(self.calls()[2:]))      # it never went on to load a second copy

    def test_an_unexplained_print_failure_raises(self):
        self.fail_verb('print', 'launchctl: something else broke')
        with self.assertRaises(RuntimeError) as cm:
            self.install()
        self.assertIn('something else broke', str(cm.exception))


class RemoveStatusTests(FakeTools):
    def setUp(self):
        super().setUp()
        self.data = self.tmp / 'data'

    def install(self):
        with mock.patch.object(schedule, 'PACKAGE_DIR', INSTALLED):
            schedule.install(self.data, 2, 9, False, 'en')

    def test_remove_boots_out_then_deletes_the_plist(self):
        self.install()
        self.assertTrue(schedule.remove())
        self.assertFalse(self.plist.exists())
        self.assertFalse((self.state / 'loaded').exists())
        self.assertEqual(self.calls()[-1], f'bootout gui/{self.uid}/{LABEL}')

    def test_remove_with_nothing_installed_is_not_an_error(self):
        self.assertFalse(schedule.remove())
        self.assertEqual(self.calls(), [f'bootout gui/{self.uid}/{LABEL}'])

    def test_remove_deletes_a_plist_whose_job_is_not_loaded(self):
        # e.g. left behind by a failed bootstrap; the plist is ours, so removing it is always safe.
        self.install()
        self.set_loaded(False)
        self.assertTrue(schedule.remove())
        self.assertFalse(self.plist.exists())

    def test_remove_failure_keeps_the_plist(self):
        self.install()
        self.fail_verb('bootout', 'Boot-out failed: 5: Input/output error')
        with self.assertRaises(RuntimeError) as cm:
            schedule.remove()
        self.assertIn('Input/output error', str(cm.exception))
        self.assertTrue(self.plist.exists())

    def test_status_when_nothing_is_installed(self):
        info = schedule.status(self.data)
        self.assertEqual(info, {'plist': str(self.plist), 'installed': False, 'loaded': False, 'weekday': None,
                                'hour': None, 'log': str(self.data / 'logs' / 'schedule.log'), 'log_tail': []})

    def test_status_reads_the_schedule_and_the_log_back_from_the_plist(self):
        self.install()
        log = self.data / 'logs' / 'schedule.log'
        log.write_text(''.join(f'line {i}\n' for i in range(8)), encoding='utf-8')
        info = schedule.status(self.tmp / 'some-other-data-dir')       # the installed plist decides, not the caller
        self.assertEqual((info['installed'], info['loaded'], info['weekday'], info['hour']), (True, True, 2, 9))
        self.assertEqual(info['log'], str(log))
        self.assertEqual(info['log_tail'], [f'line {i}' for i in range(3, 8)])
        self.set_loaded(False)
        self.assertEqual(schedule.status(self.data)['loaded'], False)


class ScheduleCommandTests(FakeTools):
    def setUp(self):
        super().setUp()
        self.fh = FakeHome(Clock()).__enter__()
        self.addCleanup(self.fh.cleanup)
        self.app = make_app(self.fh)
        self.addCleanup(self.app.close)
        p = mock.patch.object(schedule, 'PACKAGE_DIR', SRC_TREE)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop('DISK_STEWARD_DATA_DIR', None)

    def run_cli(self, *argv):
        self.app.locale = 'en'                         # main() keeps a --lang on the App it was given
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(list(argv), app_factory=lambda: self.app)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_install_prints_exactly_what_it_installed(self):
        code, out, err = self.run_cli('schedule', 'install', '--weekday', '2', '--hour', '5')
        self.assertEqual((code, err), (0, ''))
        plist = self.parsed()
        self.assertEqual(plist['StartCalendarInterval'], {'Weekday': 2, 'Hour': 5, 'Minute': 0})
        self.assertIn(f'Installed LaunchAgent {LABEL}: {self.plist}', out)
        self.assertIn('Runs: every Tuesday at 05:00', out)
        self.assertIn(f'Command: {sys.executable} -m disk_steward scan --notify --data-dir {self.fh.data_dir} --lang en',
                      out)
        self.assertIn('Environment: PYTHONPATH=/work/disk-steward/src', out)
        self.assertIn(f'Log: {self.fh.data_dir}/logs/schedule.log', out)
        self.assertIn('It only scans and posts a notification. It never deletes anything.', out)

    def test_defaults_are_monday_at_ten(self):
        self.assertEqual(self.run_cli('schedule', 'install')[0], 0)
        self.assertEqual(self.parsed()['StartCalendarInterval'], {'Weekday': 1, 'Hour': 10, 'Minute': 0})

    def test_the_language_follows_the_user_and_the_chinese_text_is_complete(self):
        code, out, _ = self.run_cli('schedule', 'install', '--weekday', '7', '--lang', 'zh-CN')
        self.assertEqual(code, 0)
        args = self.parsed()['ProgramArguments']
        self.assertEqual(args[-2:], ['--lang', 'zh-CN'])
        self.assertIn('每周日 10:00', out)
        self.assertIn('不会删除任何内容', out)

    def test_a_data_dir_from_the_default_is_left_out_one_from_the_environment_is_passed(self):
        with mock.patch.object(schedule_cmd, 'default_data_dir', return_value=self.fh.data_dir):
            self.assertEqual(self.run_cli('schedule', 'install')[0], 0)
            self.assertNotIn('--data-dir', self.parsed()['ProgramArguments'])
            os.environ['DISK_STEWARD_DATA_DIR'] = str(self.fh.data_dir)
            self.assertEqual(self.run_cli('schedule', 'install')[0], 0)
            self.assertIn('--data-dir', self.parsed()['ProgramArguments'])

    def test_weekday_and_hour_are_range_checked(self):
        for bad in (('--weekday', '8'), ('--weekday', '-1'), ('--hour', '24'), ('--hour', 'x')):
            with self.subTest(bad=bad):
                self.assertEqual(self.run_cli('schedule', 'install', *bad)[0], 2)
        self.assertFalse(self.plist.exists())
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.run_cli('schedule')[0], 2)

    def test_status_and_remove(self):
        code, out, _ = self.run_cli('schedule', 'status')
        self.assertEqual(code, 0)
        self.assertIn('Not installed', out)
        self.assertIn('launchd: not loaded', out)
        self.assertIn('No log yet', out)
        self.run_cli('schedule', 'install', '--weekday', '4', '--hour', '23')
        (self.fh.data_dir / 'logs' / 'schedule.log').write_text('scan done\nall good\n', encoding='utf-8')
        code, out, _ = self.run_cli('schedule', 'status')
        self.assertEqual(code, 0)
        self.assertIn(f'Installed: {self.plist}', out)
        self.assertIn('launchd: loaded', out)
        self.assertIn('Runs: every Thursday at 23:00', out)
        self.assertIn('  scan done\n  all good', out)
        zh = self.run_cli('schedule', 'status', '--lang', 'zh-CN')[1]
        self.assertIn('已安装', zh)
        self.assertIn('每周四 23:00', zh)
        code, out, _ = self.run_cli('schedule', 'remove')
        self.assertEqual(code, 0)
        self.assertIn(f'Removed {self.plist}', out)
        self.assertFalse(self.plist.exists())
        code, out, _ = self.run_cli('schedule', 'remove')
        self.assertEqual((code, out.strip()), (0, 'The weekly scan is not installed.'))

    def test_a_launchctl_failure_is_not_swallowed(self):
        self.fail_verb('bootstrap', 'Bootstrap failed: 5: Input/output error')
        with self.assertRaises(RuntimeError) as cm:
            self.run_cli('schedule', 'install')
        self.assertIn('Input/output error', str(cm.exception))


class NotifyTests(FakeTools):
    """`scan --notify` with a fake osascript, on the M2 fixture tree."""

    QUOTED = '~/.claude/projects/-Users-me-"q"\\x'     # a folder name with a quote and a backslash

    def setUp(self):
        super().setUp()
        self.fh = FakeHome(Clock()).__enter__()
        self.addCleanup(self.fh.cleanup)
        fx.build(self.fh)
        self.fh.file(f'{self.QUOTED}/a.jsonl', size=100, age_days=20)
        self.app = make_app(self.fh)
        self.addCleanup(self.app.close)

    def run_cli(self, *argv):
        self.app.locale = 'en'                         # main() keeps a --lang on the App it was given
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(list(argv), app_factory=lambda: self.app)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def grow_quoted_group(self):
        self.fh.file(f'{self.QUOTED}/a.jsonl', size=300_000, age_days=20)

    def test_first_scan_posts_the_offer_and_says_there_is_nothing_to_compare(self):
        code, out, err = self.run_cli('scan', '--notify')
        self.assertEqual((code, err), (0, ''))
        self.assertIn('Scan #1', out)                                        # the report is still printed
        script_flag, script = self.osascript_args()
        offered = fmt_bytes(self.app.overview()['offered']['alloc'])
        self.assertEqual(script_flag, '-e')
        self.assertEqual(script, f'display notification "No earlier scan to compare with." with title '
                                 f'"Disk Steward" subtitle "Junk you can clean now: {offered}"')

    def test_growth_names_the_biggest_growing_group_with_quotes_and_backslashes_escaped(self):
        self.assertEqual(self.run_cli('scan')[0], 0)
        self.grow_quoted_group()
        code, _, err = self.run_cli('scan', '--notify')
        self.assertEqual((code, err), (0, ''))
        ov = self.app.overview()
        delta = fmt_bytes(ov['growth']['alloc_delta'], signed=True)
        top = ov['growth']['groups'][0]
        self.assertGreater(top['delta'], 250_000)
        label = 'Claude Code sessions (~/.claude/projects/-Users-me-\\"q\\"\\\\x)'         # as AppleScript wants it
        self.assertEqual(len(self.osascript_args()), 2)                      # one -e, one script: no shell splitting
        script = self.osascript_args()[1]
        self.assertEqual(script, (
            f'display notification "Scanned space changed by {delta} since the last scan. Biggest growth: {label}, '
            f'{fmt_bytes(top["delta"], signed=True)}." with title "Disk Steward" subtitle "Junk you can clean now: '
            f'{fmt_bytes(ov["offered"]["alloc"])}"'))
        self.assertTrue(delta.startswith('+'))

    def test_the_script_is_in_the_users_language(self):
        self.run_cli('scan')
        self.grow_quoted_group()
        code, _, _ = self.run_cli('scan', '--notify', '--lang', 'zh-CN')
        self.assertEqual(code, 0)
        script = self.osascript_args()[1]
        self.assertIn('自上次扫描以来，已扫描空间变化 +', script)
        self.assertIn('增长最多：Claude Code 会话(~/.claude/projects/-Users-me-\\"q\\"\\\\x)，+', script)
        self.assertIn('subtitle "现在可清理的垃圾：', script)
        self.assertIn('with title "Disk Steward"', script)

    def test_no_notification_without_the_flag(self):
        self.assertEqual(self.run_cli('scan')[0], 0)
        self.assertFalse((self.state / 'osascript.args').exists())

    def test_osascript_failure_exits_1_and_the_scan_stays_saved(self):
        (self.state / 'osascript.fail').write_text('execution error: Not allowed to send Apple events. (-1743)')
        code, out, err = self.run_cli('scan', '--notify')
        self.assertEqual(code, 1)
        self.assertIn('Could not post the notification', err)
        self.assertIn('Not allowed to send Apple events', err)
        self.assertIn('Scan #1', out)
        self.assertEqual(self.app.latest_scan()['id'], 1)
        self.assertEqual(self.app.latest_scan()['status'], 'done')

    def test_a_missing_osascript_exits_1_too(self):
        with mock.patch.object(schedule, 'OSASCRIPT', str(self.tmp / 'no-such-osascript')):
            code, _, err = self.run_cli('scan', '--notify')
        self.assertEqual(code, 1)
        self.assertIn('Could not post the notification', err)
        self.assertEqual(self.app.latest_scan()['status'], 'done')

    def test_a_hanging_osascript_times_out_and_exits_1(self):
        (self.state / 'osascript.sleep').touch()
        with mock.patch.object(schedule, 'TIMEOUT', 0.3):
            code, _, err = self.run_cli('scan', '--notify')
        self.assertEqual(code, 1)
        self.assertIn('timed out', err)
        self.assertEqual(self.app.latest_scan()['status'], 'done')

    def test_the_notification_never_plans_or_deletes(self):
        before = sorted(str(p) for p in self.fh.home.rglob('*'))
        self.run_cli('scan', '--notify')
        self.assertEqual(sorted(str(p) for p in self.fh.home.rglob('*')), before)
        con = sqlite3.connect(self.fh.data_dir / 'steward.db')
        try:
            self.assertEqual(con.execute('SELECT count(*) FROM plans').fetchone()[0], 0)
        finally:
            con.close()


class TextTests(unittest.TestCase):
    def test_applescript_string_escapes_quotes_and_backslashes(self):
        esc = schedule.applescript_string
        self.assertEqual(esc('plain'), 'plain')
        self.assertEqual(esc('say "hi"'), 'say \\"hi\\"')
        self.assertEqual(esc('a\\b'), 'a\\\\b')
        self.assertEqual(esc('\\"'), '\\\\\\"')
        self.assertEqual(esc('中文 "引号"'), '中文 \\"引号\\"')

    def test_control_characters_become_spaces(self):
        self.assertEqual(schedule.applescript_string('a\nb\r\tc'), 'a b  c')

    def test_a_crafted_name_cannot_close_the_string(self):
        evil = '" & (do shell script "touch /tmp/x") & "\\'
        out = schedule.applescript_string(evil)
        run = 0
        for ch in out:
            if ch == '\\':
                run += 1
                continue
            if ch == '"':
                self.assertEqual(run % 2, 1, 'an unescaped quote would end the AppleScript string')
            run = 0
        self.assertEqual(run % 2, 0)                                           # a final backslash is itself escaped
        unescaped = out.replace('\\"', '"').replace('\\\\', '\\')
        self.assertEqual(unescaped, evil)

    def test_notification_text_without_a_growing_group(self):
        overview = {'offered': {'alloc': 2048},
                    'growth': {'previous_scan_id': 1, 'alloc_delta': -1536,
                               'groups': [{'key': 'x', 'rule_id': 'x', 'label_args': {}, 'delta': -1536}]}}
        self.assertEqual(schedule.notification_text(overview, 'en'),
                         ('Junk you can clean now: 2.00 KiB', 'Scanned space changed by -1.50 KiB since the last scan.'))
        subtitle, body = schedule.notification_text(overview, 'zh-CN')
        self.assertEqual((subtitle, body), ('现在可清理的垃圾：2.00 KiB', '自上次扫描以来，已扫描空间变化 -1.50 KiB。'))

    def test_notification_text_without_a_comparison(self):
        overview = {'offered': {'alloc': 0}, 'growth': {'previous_scan_id': None, 'alloc_delta': None, 'groups': []}}
        self.assertEqual(schedule.notification_text(overview, 'en'),
                         ('Junk you can clean now: 0 B', 'No earlier scan to compare with.'))


if __name__ == '__main__':
    unittest.main()
