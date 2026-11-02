"""Native cleanup commands: the registry, the probes, the runner and the service flow, with fake tools on a temp PATH
and the tool folders in a FakeHome. Nothing here starts a real tool."""
from __future__ import annotations

import json
import os
import shlex
import threading
import time
import unittest
from unittest import mock

import m2_fixture as fx
from disk_steward import i18n, native
from disk_steward.catalog import is_report_only, load_catalog
from disk_steward.model import ErrorCode, StewardError
from disk_steward.service import App
from disk_steward.settings import Settings
from m3_support import make_app, make_mac
from m4_support import JUNK_14D, make_plan
from native_support import TREES, FakeBin, alive, build_trees, wait_until
from support import Clock, FakeHome, render_error

EXPECTED = {
    'dev.homebrew': ['brew', 'cleanup', '--prune=all'], 'dev.pip': ['pip3', 'cache', 'purge'],
    'dev.go-build': ['go', 'clean', '-cache'], 'dev.go-mod': ['go', 'clean', '-modcache'],
    'dev.pnpm': ['pnpm', 'store', 'prune'], 'dev.uv': ['uv', 'cache', 'prune'],
    'dev.npm': ['npm', 'cache', 'clean', '--force'], 'dev.yarn': ['yarn', 'cache', 'clean'],
    'dev.colima': ['colima', 'prune', '--force'], 'dev.coresimulator': ['xcrun', 'simctl', 'delete', 'unavailable']}
OLLAMA_LIST = ("NAME  ID  SIZE  MODIFIED\\n"
               "llama3:latest   abc123def456    2.0 GB    3 weeks ago\\n"
               "tiny:1b         a1b2c3d4e5f6    774 MB    2 days ago\\n"
               "a$HOME;b:v1     0123456789ab    1.5 KB    now\\n")


def tearDownModule():
    if i18n.MISSING:
        raise AssertionError(f'untranslated keys: {sorted(i18n.MISSING)}')


class RegistryTests(unittest.TestCase):
    def test_every_entry_is_exactly_this_argv(self):
        self.assertEqual({k: list(v) for k, v in native.COMMANDS.items()}, EXPECTED)
        self.assertEqual(native.COPY_ONLY, {'dev.playwright': 'copy_network'})
        self.assertEqual(native.OLLAMA, 'ollama.models')
        self.assertEqual(native.ollama_argv('llama3:latest'), ('ollama', 'rm', 'llama3:latest'))

    def test_the_display_string_is_the_shell_quoted_argv(self):
        self.assertEqual(native.display(native.COMMANDS['dev.homebrew']), 'brew cleanup --prune=all')
        self.assertEqual(native.display(native.ollama_argv('a b; rm -rf ~')), "ollama rm 'a b; rm -rf ~'")
        for argv in list(native.COMMANDS.values()) + [native.ollama_argv("it's$HOME")]:
            self.assertEqual(shlex.split(native.display(argv)), list(argv))

    def test_every_entry_is_a_report_only_rule_the_catalog_has(self):
        catalog = load_catalog(Settings())
        for rule_id in list(native.COMMANDS) + list(native.COPY_ONLY) + [native.OLLAMA]:
            with self.subTest(rule=rule_id):
                rule = catalog.rule(rule_id)
                self.assertTrue(is_report_only(rule) and not rule.actions)
        self.assertFalse(set(native.COMMANDS) & set(native.COPY_ONLY))

    def test_nothing_in_the_registry_deletes_by_path_or_goes_through_a_shell(self):
        for argv in native.COMMANDS.values():
            self.assertNotIn(argv[0], ('rm', 'sh', 'bash', 'zsh', 'sudo', 'env'))
            self.assertTrue(all(not set(a) & set(';|&$`<>*?~') for a in argv), argv)

    def test_ollama_list_is_parsed_by_name_and_size(self):
        rows = native.parse_ollama_list(OLLAMA_LIST.replace('\\n', '\n'))
        self.assertEqual([(r['name'], r['size']) for r in rows], [('llama3:latest', '2.0 GB'), ('tiny:1b', '774 MB'),
                                                                   ('a$HOME;b:v1', '1.5 KB')])
        self.assertEqual([r['bytes'] for r in rows], [2_000_000_000, 774_000_000, 1500])
        self.assertEqual(native.parse_ollama_list('NAME ID SIZE MODIFIED\n'), [])
        self.assertEqual(native.parse_ollama_list('garbage\nnot a row\n'), [])


class ToolCase(unittest.TestCase):
    """A temp PATH with fake tools and a FakeHome that holds their folders."""
    domains = ('agent', 'app-library', 'dev-shared')

    def setUp(self):
        self.clock = Clock()
        self.fh = FakeHome(self.clock).__enter__()
        self.addCleanup(self.fh.cleanup)
        fx.build(self.fh)
        build_trees(self.fh)
        self.bin = FakeBin(self.fh.root)
        patch = mock.patch.dict(os.environ, {'PATH': str(self.bin.dir), 'DS_TEST_MARK': 'from-the-user-env'})
        patch.start()
        self.addCleanup(patch.stop)
        self.mac = make_mac()
        self.app = self.open_app()

    def open_app(self, **kw):
        app = make_app(self.fh, mac=self.mac, settings=Settings(domains=self.domains), **kw)
        self.addCleanup(app.close)
        return app

    def path(self, rule_id):
        return str(self.fh.path(TREES[rule_id][0]))

    def row(self, rule_id, locale='en'):
        return {r['rule_id']: r for r in self.app.native_tools(locale)}[rule_id]

    def install(self):
        """The tools a Mac with Homebrew, uv, Ollama and Xcode has."""
        self.bin.tool('brew', f'record()\nprint("Removing downloads")\nprint("done")\nwipe({self.path("dev.homebrew")!r})')
        self.bin.tool('uv', f'record()\nwipe({self.path("dev.uv")!r})')
        self.bin.tool('xcrun', f'if args[:2] == ["simctl", "help"]:\n    sys.exit(0)\nrecord()\n'
                               f'wipe({self.path("dev.coresimulator")!r})')
        self.bin.tool('ollama', f'if args == ["list"]:\n    sys.stdout.write("{OLLAMA_LIST}")\n    sys.exit(0)\nrecord()\n'
                                f'wipe({self.path("ollama.models")!r})')

    def wait_job(self):
        wait_until(lambda: self.app.job()['status'] != 'running')
        return self.app.job()

    def start(self, rule_id, command=None, target=None, app=None):
        app = app or self.app
        row = {r['rule_id']: r for r in app.native_tools('en')}[rule_id]
        if command is None:
            command = row['command'] if target is None else [t for t in row['targets'] if t['name'] == target][0]['command']
        return app.start_native(rule_id, target, command)

    base = 0   # bus version to count events from

    def events(self, kind):
        return [e['data'] for e in self.app.events.since(self.base) if e['type'] == kind]

    def audit(self):
        path = self.fh.data_dir / 'audit' / 'audit.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def metas(self):
        conn = self.app.store._conn()
        return {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'native:%'")}

    def expect(self, code, key, fn, *args, **kw):
        with self.assertRaises(StewardError) as cm:
            fn(*args, **kw)
        self.assertEqual((cm.exception.code, cm.exception.message_key), (code, key))
        render_error(cm.exception)
        return cm.exception


class TrashTests(ToolCase):
    """Move to Trash of one folder of a tool without a command: a draft plan of that folder, run like any plan."""

    def folder(self, rule_id):
        return self.row(rule_id)['folders'][0]

    def test_the_plan_holds_exactly_the_listed_folder_and_nothing_moves_until_it_runs(self):
        self.app.scan()
        folder = self.folder('dev.gradle')
        plan = self.app.trash_plan(folder['item'])
        self.assertEqual((plan['state'], plan['totals']['count'], plan['totals']['alloc']), ('draft', 1, folder['bytes']))
        entries = self.app.entries(plan['id'])['rows']
        self.assertEqual([(e['path'], e['action'], e['rule_id']) for e in entries], [(folder['path'], 'trash', 'dev.gradle')])
        self.assertEqual(self.mac.trashed, [])
        self.fh.clock.advance(seconds=60)   # the folder itself was made at wall-clock time, a moment after the test clock
        self.app.run_plan(plan['id'], plan['revision'], plan['entries_digest'])
        self.assertEqual(self.mac.trashed, [str(self.fh.path(folder['path']))])
        self.assertEqual(self.app.entries(plan['id'])['rows'][0]['state'], 'done')

    def test_a_folder_that_changed_since_the_scan_is_skipped(self):
        self.app.scan()
        folder = self.folder('dev.npx')
        plan = self.app.trash_plan(folder['item'])
        self.fh.file('~/.npm/_npx/abc/new.js', size=10_000, age_days=0)
        self.app.run_plan(plan['id'], plan['revision'], plan['entries_digest'])
        self.assertEqual(self.mac.trashed, [])
        self.assertEqual(self.app.entries(plan['id'])['rows'][0]['state'], 'skipped')

    def test_only_a_listed_folder_of_a_tool_without_a_command_can_be_planned(self):
        self.app.scan()
        items = {i.rule_id: i.key for i in self.app.store.items(self.app.latest_scan()['id'])}
        for rule_id in ('dev.homebrew', 'dev.playwright', 'ollama.models'):   # these have their own command
            with self.subTest(rule=rule_id):
                self.expect(ErrorCode.INVALID, 'error.manual_not_allowed', self.app.trash_plan, items[rule_id])
        junk = next(k for r, k in items.items() if not r.startswith(('dev.', 'ollama.')))
        self.expect(ErrorCode.INVALID, 'error.manual_not_allowed', self.app.trash_plan, junk)
        self.expect(ErrorCode.NOT_FOUND, 'error.NOT_FOUND', self.app.trash_plan, 'no-such-item')
        self.mac.trash_ok = False
        self.expect(ErrorCode.UNAVAILABLE, 'error.trash_unavailable', self.app.trash_plan, items['dev.gradle'])

    def test_a_folder_holding_a_credential_is_protected_and_never_listed(self):
        self.fh.file('~/.gradle/caches/modules-2/credentials.json', size=100, age_days=30)
        self.app.scan()
        row = {r['rule_id']: r for r in self.app.native_tools('en')}.get('dev.gradle')
        self.assertTrue(row is None or not row['folders'])
        groups = {g.key: g for g in self.app.store.groups(self.app.latest_scan()['id'])}
        self.assertEqual(groups['dev.gradle#protected'].tier.value, 'protected')


class ListingTests(ToolCase):
    def test_nothing_before_the_first_scan(self):
        self.install()
        self.assertEqual(self.app.native_tools('en'), [])

    def test_report_only_groups_with_data_biggest_first_each_with_its_command_and_state(self):
        self.install()
        self.app.scan()
        rows = self.app.native_tools('en')
        self.assertEqual([r['rule_id'] for r in rows], ['dev.homebrew', 'ollama.models', 'dev.uv', 'dev.coresimulator',
                                                         'dev.gradle', 'dev.playwright', 'dev.npx', 'dev.cargo'])
        self.assertEqual([r['bytes'] for r in rows], sorted((r['bytes'] for r in rows), reverse=True))
        for r in rows:
            self.assertEqual(set(r), {'rule_id', 'group', 'label', 'bytes', 'items', 'command', 'runnable', 'reason',
                                      'tool', 'tool_version', 'targets', 'folders'})
            self.assertGreater(r['bytes'], 0)
        brew = rows[0]
        self.assertEqual((brew['command'], brew['runnable'], brew['reason'], brew['tool'], brew['tool_version'],
                          brew['targets'], brew['group'], brew['items']),
                         ('brew cleanup --prune=all', True, None, str(self.bin.dir / 'brew'), '1.2.3', None,
                          'dev.homebrew', 1))
        self.assertEqual(brew['bytes'], 5 * 102400)       # five 100,000-byte files, allocated in 4 KiB blocks
        self.assertEqual(brew['label'], i18n.t('rule.dev.homebrew.label', 'en'))

    def test_only_report_only_groups_are_listed(self):
        self.install()
        self.app.scan()
        listed = {r['rule_id'] for r in self.app.native_tools('en')}
        kinds = {g.rule_id: (g.tier.value, bool(g.actions)) for g in self.app.store.groups(self.app.latest_scan()['id'])}
        self.assertFalse(any(kinds[r][1] or kinds[r][0] == 'protected' for r in listed))
        self.assertNotIn('codex.logs', listed)                       # junk
        self.assertNotIn('prot.databases', listed)                   # protected

    def test_a_tool_that_is_not_installed_is_listed_but_cannot_run(self):
        self.app.scan()
        row = self.row('dev.homebrew')
        self.assertEqual((row['command'], row['runnable'], row['tool'], row['tool_version']),
                         ('brew cleanup --prune=all', False, None, None))
        self.assertEqual(row['reason'], 'brew was not found on your PATH.')
        self.assertEqual(self.row('dev.homebrew', 'zh-CN')['reason'], '在 PATH 中找不到 brew。')

    def test_a_copy_only_row_shows_its_command_and_why(self):
        self.app.scan()
        playwright = self.row('dev.playwright')
        self.assertEqual(playwright['command'], 'npx playwright uninstall --all')
        self.assertEqual((playwright['runnable'], playwright['tool'], playwright['tool_version'], playwright['targets'],
                          playwright['folders']), (False, None, None, None, None))
        self.assertIn('network', playwright['reason'])

    def test_a_tool_without_a_command_lists_its_folders_for_the_trash_and_no_rm(self):
        self.app.scan()
        for rule_id, path in (('dev.npx', '~/.npm/_npx'), ('dev.gradle', '~/.gradle/caches/modules-2'),
                              ('dev.cargo', '~/.cargo/registry/cache')):
            with self.subTest(rule=rule_id):
                row = self.row(rule_id)
                self.assertEqual((row['command'], row['runnable'], row['reason'], row['targets']), (None, False, None, None))
                self.assertEqual([f['path'] for f in row['folders']], [path])
                self.assertEqual(sum(f['bytes'] for f in row['folders']), row['bytes'])
        self.assertFalse(any('rm -rf' in (r['command'] or '') + (r['reason'] or '') for r in self.app.native_tools('en')))

    def test_without_the_system_trash_the_folders_are_not_offered_and_the_row_says_why(self):
        self.mac.trash_ok = False
        self.app.scan()
        row = self.row('dev.gradle')
        self.assertEqual((row['folders'], row['reason']), (None, 'Move to Trash needs macOS 15 or later (/usr/bin/trash).'))
        self.assertEqual(self.row('dev.gradle', 'zh-CN')['reason'], '移到废纸篓需要 macOS 15 或更高版本（/usr/bin/trash）。')

    def test_simctl_has_to_work_and_the_reason_says_how_to_fix_it(self):
        self.bin.tool('xcrun', 'if args[:2] == ["simctl", "help"]:\n    sys.exit(1)\nrecord()')
        self.app.scan()
        row = self.row('dev.coresimulator')
        self.assertEqual((row['runnable'], row['command']), (False, 'xcrun simctl delete unavailable'))
        self.assertEqual(row['reason'], 'Needs full Xcode selected: sudo xcode-select -s /Applications/Xcode.app')
        self.assertEqual(row['tool'], str(self.bin.dir / 'xcrun'))     # it is there, it just cannot do simctl
        self.assertEqual(self.bin.calls(), [])                          # `simctl help` is a probe, not a run

    def test_simctl_that_works_makes_the_row_runnable(self):
        self.install()
        self.app.scan()
        self.assertTrue(self.row('dev.coresimulator')['runnable'])

    def test_ollama_models_are_listed_as_targets_each_with_its_own_command(self):
        self.install()
        self.app.scan()
        row = self.row('ollama.models')
        self.assertEqual((row['command'], row['runnable']), ('ollama rm <model>', True))
        self.assertEqual([(t['name'], t['size'], t['command']) for t in row['targets']], [
            ('llama3:latest', '2.0 GB', 'ollama rm llama3:latest'), ('tiny:1b', '774 MB', 'ollama rm tiny:1b'),
            ('a$HOME;b:v1', '1.5 KB', "ollama rm 'a$HOME;b:v1'")])
        self.assertEqual(row['targets'][0]['bytes'], 2_000_000_000)

    def test_ollama_that_cannot_list_or_lists_nothing_cannot_run(self):
        self.bin.tool('ollama', 'if args == ["list"]:\n    sys.stderr.write("Error: could not connect to ollama\\n")\n'
                                '    sys.exit(1)')
        self.app.scan()
        row = self.row('ollama.models')
        self.assertEqual((row['runnable'], row['targets']), (False, None))
        self.assertEqual(row['reason'], '`ollama list` failed: Error: could not connect to ollama')
        self.bin.tool('ollama', 'if args == ["list"]:\n    print("NAME ID SIZE MODIFIED")\n    sys.exit(0)')
        self.app = self.open_app()                                    # a new listing, not the cached one
        row = self.row('ollama.models')
        self.assertEqual((row['runnable'], row['targets'], row['reason']), (False, [], '`ollama list` shows no models.'))

    def test_versions_are_asked_once_per_ten_minutes(self):
        self.install()
        self.app.scan()
        self.app.native_tools('en')
        self.app.native_tools('en')
        self.assertEqual(sorted(self.bin.versions()), ['brew', 'ollama', 'uv', 'xcrun'])
        self.clock.advance(seconds=599)
        self.app.native_tools('en')
        self.assertEqual(len(self.bin.versions()), 4)
        self.clock.advance(seconds=2)
        self.app.native_tools('en')
        self.assertEqual(len(self.bin.versions()), 8)

    def test_a_tool_that_will_not_say_its_version_has_none(self):
        self.bin.tool('brew', version=None)
        self.app.scan()
        row = self.row('dev.homebrew')
        self.assertEqual((row['runnable'], row['tool_version']), (True, None))

    def test_go_is_asked_with_its_own_spelling(self):
        self.assertEqual(native.VERSION_ARGV['go'], ('go', 'version'))
        self.bin.tool('go', version='go version go1.22.1 darwin/arm64')
        self.assertEqual(native.Probe(self.clock).version('go'), '1.22.1')

    def test_an_unknown_locale_is_refused(self):
        with self.assertRaises(StewardError) as cm:
            self.app.native_tools('fr')
        self.assertEqual(cm.exception.message_key, 'error.invalid_locale')


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = FakeHome().__enter__()
        self.addCleanup(self.tmp.cleanup)
        self.bin = FakeBin(self.tmp.root)
        self.lines = []

    def run_tool(self, body, cancelled=lambda: False, **kw):
        self.bin.tool('tool', body)
        return native.run_command([str(self.bin.dir / 'tool')], str(self.tmp.root), os.environ,
                                  cancelled, self.lines.append, **kw)

    def test_output_comes_line_by_line_stdout_and_stderr_together(self):
        result = self.run_tool('print("one"); sys.stderr.write("two\\n"); sys.stderr.flush(); print("three")')
        self.assertEqual((result.status, result.exit_code), ('ok', 0))
        self.assertEqual(sorted(self.lines), ['one', 'three', 'two'])
        self.assertEqual(result.tail, self.lines)
        self.assertEqual(result.lines, 3)

    def test_carriage_returns_end_lines_too_and_empty_lines_are_dropped(self):
        self.run_tool('sys.stdout.write("10%\\r50%\\r100%\\n\\n\\nok\\r\\nend"); sys.stdout.flush()')
        self.assertEqual(self.lines, ['10%', '50%', '100%', 'ok', 'end'])

    def test_a_long_line_is_cut_and_an_endless_one_does_not_pile_up(self):
        self.run_tool('print("a" * 10000); sys.stdout.write("b" * 50000)')
        self.assertEqual(self.lines[0], 'a' * native.MAX_LINE)
        self.assertTrue(self.lines[1:] and all(set(x) == {'b'} for x in self.lines[1:]))
        self.assertTrue(all(len(x) <= native.MAX_LINE for x in self.lines))

    def test_a_failing_tool_reports_its_exit_code(self):
        result = self.run_tool('print("bad"); sys.exit(3)')
        self.assertEqual((result.status, result.exit_code, self.lines), ('failed', 3, ['bad']))

    def test_the_tail_is_the_last_forty_lines(self):
        result = self.run_tool('for i in range(500): print("line", i)')
        self.assertEqual(result.lines, 500)
        self.assertEqual(result.tail, [f'line {i}' for i in range(460, 500)])
        self.assertEqual(len(self.lines), 500)

    def test_output_beyond_two_mib_is_read_and_dropped_not_blocked_on(self):
        result = self.run_tool('line = "x" * 99\nfor i in range(40000): print(line)')   # 4 MB
        self.assertEqual((result.status, result.exit_code, result.truncated), ('ok', 0, True))
        self.assertLessEqual(sum(len(x) for x in self.lines), native.MAX_OUTPUT)
        self.assertGreater(sum(len(x) for x in self.lines), native.MAX_OUTPUT - 200)

    def test_a_timeout_kills_the_whole_process_group(self):
        pids = self.tmp.root / 'pids'
        body = (f'import subprocess\nchild = subprocess.Popen(["/bin/sleep", "300"])\n'
                f'open({str(pids)!r}, "w").write("%d %d" % (os.getpid(), child.pid))\nprint("started", flush=True)\n'
                'time.sleep(300)')
        with mock.patch.object(native, 'TIMEOUT', 1.0), mock.patch.object(native, 'TERM_GRACE', 5.0):
            started = time.monotonic()
            result = self.run_tool(body)
        self.assertEqual(result.status, 'timeout')
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(self.lines, ['started'])
        for pid in map(int, pids.read_text().split()):
            wait_until(lambda pid=pid: not alive(pid), 5)

    def test_a_tool_that_ignores_sigterm_is_killed_after_the_grace_period_with_its_children(self):
        pids = self.tmp.root / 'pids'
        body = (f'import subprocess\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                f'child = subprocess.Popen(["/bin/sleep", "300"])\n'   # inherits the ignored SIGTERM
                f'open({str(pids)!r}, "w").write("%d %d" % (os.getpid(), child.pid))\nprint("up", flush=True)\n'
                'time.sleep(300)')
        with mock.patch.object(native, 'TIMEOUT', 1.5), mock.patch.object(native, 'TERM_GRACE', 0.5):
            result = self.run_tool(body)
        self.assertEqual((result.status, result.exit_code), ('timeout', -9))
        for pid in map(int, pids.read_text().split()):
            wait_until(lambda pid=pid: not alive(pid), 5)

    def test_cancel_stops_the_group_as_well(self):
        started = self.tmp.root / 'started'
        body = f'open({str(started)!r}, "w").write("1")\ntime.sleep(300)'
        result = self.run_tool(body, cancelled=lambda: started.exists())
        self.assertEqual(result.status, 'cancelled')

    def test_a_tool_that_cannot_be_started_raises(self):
        with self.assertRaises(OSError):
            native.run_command([str(self.bin.dir / 'nothing')], str(self.tmp.root), os.environ, lambda: False,
                               self.lines.append)

    def test_the_tool_runs_where_told_with_the_given_environment_and_no_stdin(self):
        with mock.patch.dict(os.environ, {'DS_TEST_MARK': 'mine'}):
            self.run_tool('record()')
        call = self.bin.calls()[0]
        self.assertEqual(os.path.realpath(call['cwd']), os.path.realpath(str(self.tmp.root)))
        self.assertEqual((call['mark'], call['stdin_closed']), ('mine', True))

    def test_a_child_that_outlives_the_tool_does_not_hold_the_run(self):
        body = 'import subprocess\nsubprocess.Popen(["/bin/sleep", "3"])\nprint("parent done")'
        started = time.monotonic()
        result = self.run_tool(body)
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual((result.status, self.lines), ('ok', ['parent done']))


class RunTests(ToolCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.app.scan()
        self.scan_before = self.app.latest_scan()['id']
        self.base = self.app.events.version

    def test_a_run_cleans_measures_and_leaves_the_numbers_up_to_date(self):
        before = self.row('dev.homebrew')['bytes']
        job = self.start('dev.homebrew')
        self.assertEqual((job['kind'], job['status'], job['rule_id'], job['target'], job['command']),
                         ('native', 'running', 'dev.homebrew', None, 'brew cleanup --prune=all'))
        job = self.wait_job()
        result = job['native']
        self.assertEqual((job['status'], job['error'], job['error_code']), ('done', None, None))
        self.assertEqual((result['status'], result['exit_code'], result['before'], result['after'], result['freed']),
                         ('ok', 0, before, 0, before))
        self.assertEqual(result['rescan'], {'scan_id': self.scan_before + 1})
        self.assertEqual(self.app.latest_scan()['id'], self.scan_before + 1)
        self.assertNotIn('dev.homebrew', [r['rule_id'] for r in self.app.native_tools('en')])   # the group is gone
        self.assertEqual(result['output_tail'], ['Removing downloads', 'done'])

    def test_the_tool_gets_exactly_the_registry_argv_a_fresh_empty_directory_and_the_users_environment(self):
        self.start('dev.homebrew')
        self.wait_job()
        (call,) = self.bin.calls()
        self.assertEqual((call['tool'], call['argv'], call['mark'], call['cwd_entries']),
                         ('brew', ['cleanup', '--prune=all'], 'from-the-user-env', []))
        self.assertTrue(call['stdin_closed'])
        self.assertNotEqual(os.path.realpath(call['cwd']), os.path.realpath(os.getcwd()))
        self.assertFalse(os.path.exists(call['cwd']))           # the directory was ours and is gone

    def test_events_carry_every_line_then_the_result_then_the_rescan(self):
        self.start('dev.homebrew')
        self.wait_job()
        kinds = [e['type'] for e in self.app.events.since(self.base) if e['type'].startswith(('native.', 'scan.done'))]
        self.assertEqual(kinds, ['native.output', 'native.output', 'native.done', 'scan.done'])
        self.assertEqual([e['line'] for e in self.events('native.output')], ['Removing downloads', 'done'])
        (done,) = self.events('native.done')
        self.assertEqual(set(done), {'rule_id', 'target', 'exit_code', 'before', 'after', 'freed', 'output_tail',
                                     'output_truncated'})
        self.assertEqual((done['rule_id'], done['target'], done['exit_code'], done['freed']),
                         ('dev.homebrew', None, 0, 5 * 102400))
        self.assertEqual(self.events('native.failed'), [])

    def test_job_reports_the_tool_while_it_runs_with_the_last_output_lines(self):
        gate, started = self.fh.root / 'gate', self.fh.root / 'started'
        self.bin.tool('brew', f'record()\nprint("first", flush=True)\nopen({str(started)!r}, "w").write("1")\n'
                              f'wait_for({str(gate)!r})\nprint("last")')
        self.start('dev.homebrew')
        wait_until(started.exists)
        wait_until(lambda: self.app.job()['output'])
        job = self.app.job()
        self.assertEqual((job['kind'], job['status'], job['phase'], job['rule_id'], job['output'], job['output_lines']),
                         ('native', 'running', 'native', 'dev.homebrew', ['first'], 1))
        self.assertEqual(job['started_at'], self.clock())
        self.assertEqual(job['native'], None)
        gate.write_text('go')
        job = self.wait_job()
        self.assertEqual((job['status'], job['phase'], job['output']), ('done', '', ['first', 'last']))

    def test_the_run_is_audited_before_and_after(self):
        self.start('dev.homebrew')
        self.wait_job()
        events = [e for e in self.audit() if e['event'].startswith('native.')]
        self.assertEqual([e['event'] for e in events], ['native.intent', 'native.outcome'])
        intent, outcome = events
        self.assertEqual((intent['rule_id'], intent['command'], intent['argv']),
                         ('dev.homebrew', 'brew cleanup --prune=all', [str(self.bin.dir / 'brew'), 'cleanup', '--prune=all']))
        self.assertEqual((outcome['status'], outcome['exit_code'], outcome['before'], outcome['after'], outcome['freed']),
                         ('ok', 0, 5 * 102400, 0, 5 * 102400))

    def test_the_result_is_kept_in_meta(self):
        self.start('dev.homebrew')
        self.wait_job()
        ((key, saved),) = self.metas().items()
        self.assertRegex(key, r'^native:\d{13}$')
        self.assertEqual(saved, {'rule_id': 'dev.homebrew', 'target': None, 'command': 'brew cleanup --prune=all',
                                 'status': 'ok', 'exit_code': 0, 'before': 5 * 102400, 'after': 0,
                                 'freed': 5 * 102400, 'started_at': self.clock(), 'finished_at': self.clock()})
        self.assertEqual(key, f'native:{int(self.clock() * 1000)}')
        self.start('dev.uv')                                   # the same millisecond: the next key
        self.wait_job()
        self.assertEqual(len(self.metas()), 2)

    def test_the_command_has_to_be_the_one_the_user_confirmed(self):
        for wrong in ('brew cleanup', 'brew cleanup --prune=all ', 'BREW cleanup --prune=all', '',
                      'brew cleanup --prune=all; rm -rf ~', 'rm -rf ~'):
            with self.subTest(command=wrong):
                err = self.expect(ErrorCode.INVALID, 'error.native_command_changed', self.app.start_native,
                                  'dev.homebrew', None, wrong)
                self.assertEqual(err.details['expected'], 'brew cleanup --prune=all')
        self.assertEqual(self.bin.calls(), [])
        self.assertEqual((self.app.job()['kind'], self.app.job()['status']), (None, 'idle'))
        self.assertEqual(self.metas(), {})
        self.assertNotIn('native.intent', [e['event'] for e in self.audit()])

    def test_what_is_not_a_known_report_only_tool_is_refused(self):
        for rule_id in ('nonexistent', 'codex.logs', 'prot.databases', '', 'DEV.HOMEBREW', 'dev.homebrew; rm'):
            with self.subTest(rule=rule_id):
                if rule_id == '':
                    self.expect(ErrorCode.INVALID, 'error.invalid_op', self.app.start_native, rule_id, None, 'x')
                else:
                    self.expect(ErrorCode.INVALID, 'error.native_unknown', self.app.start_native, rule_id, None, 'x')
        for bad in (None, 3, ['dev.homebrew']):
            self.expect(ErrorCode.INVALID, 'error.invalid_op', self.app.start_native, bad, None, 'x')
        self.expect(ErrorCode.INVALID, 'error.invalid_op', self.app.start_native, 'dev.homebrew', None, None)
        self.expect(ErrorCode.INVALID, 'error.invalid_op', self.app.start_native, 'dev.homebrew', 3, 'x')

    def test_a_tool_that_cannot_run_is_refused_with_its_reason(self):
        for rule_id, fragment in (('dev.playwright', 'network'), ('dev.npx', 'No official'), ('dev.gradle', 'No official'),
                                  ('dev.cargo', 'No official')):
            with self.subTest(rule=rule_id):
                err = self.expect(ErrorCode.UNAVAILABLE, 'error.native_not_runnable', self.app.start_native, rule_id,
                                  None, 'anything')
                self.assertIn(fragment, err.details['reason'])
                self.assertTrue(err.details['reason_key'].startswith('native.reason.'))
        (self.bin.dir / 'brew').unlink()
        err = self.expect(ErrorCode.UNAVAILABLE, 'error.native_not_runnable', self.app.start_native, 'dev.homebrew',
                          None, 'brew cleanup --prune=all')
        self.assertEqual((err.details['reason_key'], err.details['reason_args']),
                         ('native.reason.tool_missing', {'tool': 'brew'}))
        self.assertEqual(self.bin.calls(), [])

    def test_the_tool_is_found_when_the_run_starts_not_when_it_was_listed(self):
        row = self.row('dev.homebrew')
        other = self.fh.root / 'other-bin'
        other.mkdir()
        replacement = FakeBin.__new__(FakeBin)
        replacement.dir, replacement.log, replacement.vlog = other, self.bin.log, self.bin.vlog
        replacement.tool('brew', 'record(path="replacement")')
        with mock.patch.dict(os.environ, {'PATH': str(other)}):
            self.app.start_native('dev.homebrew', None, row['command'])
            self.wait_job()
        (call,) = self.bin.calls()
        self.assertEqual(call['path'], 'replacement')
        self.assertEqual([e['argv'][0] for e in self.audit() if e['event'] == 'native.intent'], [str(other / 'brew')])

    def test_a_tool_that_exits_with_an_error_is_a_result_not_a_failure_of_the_job(self):
        self.bin.tool('brew', f'record()\nprint("nope")\nwipe({self.path("dev.homebrew")!r})\nsys.exit(3)')
        self.start('dev.homebrew')
        job = self.wait_job()
        result = job['native']
        self.assertEqual((job['status'], result['status'], result['exit_code'], result['error']), ('done', 'failed', 3, None))
        self.assertEqual(result['freed'], 5 * 102400)               # what it did before it failed still counts
        (done,) = self.events('native.done')
        self.assertEqual(done['exit_code'], 3)
        self.assertEqual(len(self.metas()), 1)

    def test_a_tool_that_grows_a_folder_frees_nothing_not_less_than_nothing(self):
        self.bin.tool('brew', f'record()\nopen({str(self.fh.path("~/Library/Caches/Homebrew/downloads/new"))!r}, "w")'
                              '.write("x" * 500000)')
        self.start('dev.homebrew')
        result = self.wait_job()['native']
        self.assertEqual(result['freed'], 0)
        self.assertGreater(result['after'], result['before'])

    def test_a_tool_that_cannot_be_started_is_an_error_and_leaves_no_trace_of_a_run(self):
        path = self.bin.tool('brew')
        path.write_text('#!/nonexistent/interpreter\n')
        self.start('dev.homebrew')
        job = self.wait_job()
        self.assertEqual((job['status'], job['error_code']), ('error', 'UNAVAILABLE'))
        self.assertEqual(job['native']['error']['message_key'], 'error.native_spawn')
        self.assertIn('brew cleanup --prune=all', job['error'])
        self.assertEqual(self.metas(), {})
        self.assertEqual(self.app.latest_scan()['id'], self.scan_before)          # nothing ran, nothing to rescan
        (failed,) = self.events('native.failed')
        self.assertEqual((failed['code'], failed['message_key']), ('UNAVAILABLE', 'error.native_spawn'))
        self.assertEqual([e['status'] for e in self.audit() if e['event'] == 'native.outcome'], ['error'])
        self.app.start_scan()                                                       # the slot is free again
        self.wait_job()

    def test_a_timeout_ends_the_group_and_is_reported(self):
        pids = self.fh.root / 'pids'
        self.bin.tool('brew', f'import subprocess\nrecord()\nchild = subprocess.Popen(["/bin/sleep", "300"])\n'
                              f'open({str(pids)!r}, "w").write("%d %d" % (os.getpid(), child.pid))\n'
                              f'wipe({self.path("dev.homebrew")!r})\nprint("working", flush=True)\ntime.sleep(300)')
        with mock.patch.object(native, 'TIMEOUT', 1.0), mock.patch.object(native, 'TERM_GRACE', 3.0):
            self.start('dev.homebrew')
            job = self.wait_job()
        result = job['native']
        self.assertEqual((job['status'], job['error_code'], result['status']), ('error', 'UNAVAILABLE', 'timeout'))
        self.assertEqual(result['error']['message_key'], 'error.native_timeout')
        self.assertEqual(result['output_tail'], ['working'])
        for pid in map(int, pids.read_text().split()):
            wait_until(lambda pid=pid: not alive(pid), 5)
        self.assertEqual(result['freed'], 5 * 102400)                 # measured and saved all the same
        self.assertEqual(len(self.metas()), 1)
        self.assertEqual(result['rescan'], {'scan_id': self.scan_before + 1})
        (failed,) = self.events('native.failed')
        self.assertEqual(failed['message_key'], 'error.native_timeout')
        self.assertEqual(self.events('native.done'), [])

    def test_cancel_stops_the_tool_and_the_job_ends_idle(self):
        started = self.fh.root / 'started'
        self.bin.tool('brew', f'record()\nwipe({self.path("dev.homebrew")!r})\nopen({str(started)!r}, "w").write("1")\n'
                              'time.sleep(300)')
        self.start('dev.homebrew')
        wait_until(started.exists)
        self.assertEqual(self.app.cancel()['kind'], 'native')
        job = self.wait_job()
        result = job['native']
        self.assertEqual((job['status'], result['status']), ('idle', 'cancelled'))
        self.assertEqual(result['error']['message_key'], 'error.native_cancelled')
        self.assertEqual(result['freed'], 5 * 102400)
        (failed,) = self.events('native.failed')
        self.assertEqual((failed['code'], failed['message_key']), ('IN_PROGRESS', 'error.native_cancelled'))
        self.assertEqual(len(self.metas()), 1)

    def test_output_events_stop_at_two_thousand_lines_but_the_count_and_the_tail_go_on(self):
        self.bin.tool('brew', 'for i in range(5000): print("line", i)')
        seen = []
        result = self.app.run_native('dev.homebrew', None, 'brew cleanup --prune=all', lambda k, d: seen.append((k, d)))
        lines = [d['line'] for k, d in seen if k == 'native.output']
        self.assertEqual(lines, [f'line {i}' for i in range(native.EVENT_LINES)])
        job = self.app.job()
        self.assertEqual(job['output_lines'], 5000)
        self.assertEqual(job['output'], [f'line {i}' for i in range(4960, 5000)])
        self.assertEqual(result['output_tail'], job['output'])

    def test_output_beyond_two_mib_is_cut_and_said_so(self):
        self.bin.tool('brew', 'line = "y" * 99\nfor i in range(40000): print(line)')
        self.start('dev.homebrew')
        result = self.wait_job()['native']
        self.assertTrue(result['output_truncated'])
        (done,) = self.events('native.done')
        self.assertTrue(done['output_truncated'])
        self.assertLessEqual(self.app.job()['output_lines'] * 99, native.MAX_OUTPUT)

    def test_ollama_removes_exactly_the_model_named(self):
        self.app.start_native('ollama.models', 'tiny:1b', 'ollama rm tiny:1b')
        job = self.wait_job()
        self.assertEqual((job['status'], job['target'], job['native']['target']), ('done', 'tiny:1b', 'tiny:1b'))
        (call,) = self.bin.calls()
        self.assertEqual(call['argv'], ['rm', 'tiny:1b'])
        self.assertGreater(job['native']['freed'], 0)

    def test_a_listing_after_a_run_asks_the_tool_again(self):
        # Otherwise a model `ollama rm` just removed would stay on offer until the probe cache expired.
        self.row('ollama.models')
        self.assertIn('ollama', self.app._native._cache)
        self.app.start_native('ollama.models', 'tiny:1b', 'ollama rm tiny:1b')
        self.wait_job()
        self.assertNotIn('ollama', self.app._native._cache)
        self.assertNotIn('simctl', self.app._native._cache)

    def test_a_model_name_reaches_the_tool_untouched_no_shell_sees_it(self):
        self.app.start_native('ollama.models', 'a$HOME;b:v1', "ollama rm 'a$HOME;b:v1'")
        self.wait_job()
        (call,) = self.bin.calls()
        self.assertEqual(call['argv'], ['rm', 'a$HOME;b:v1'])

    def test_ollama_needs_a_model_that_is_listed_now(self):
        self.expect(ErrorCode.INVALID, 'error.native_target_required', self.app.start_native, 'ollama.models', None,
                    'ollama rm <model>')
        for target in ('llama3', 'LLAMA3:latest', 'llama3:latest ', 'nothing', '--all', '../x', ''):
            with self.subTest(target=target):
                self.expect(ErrorCode.INVALID, 'error.native_target_required' if target == '' else
                            'error.native_bad_target', self.app.start_native, 'ollama.models', target,
                            f'ollama rm {target}')
        self.expect(ErrorCode.INVALID, 'error.native_command_changed', self.app.start_native, 'ollama.models',
                    'tiny:1b', 'ollama rm llama3:latest')
        self.assertEqual(self.bin.calls(), [])

    def test_only_ollama_takes_a_target(self):
        self.expect(ErrorCode.INVALID, 'error.invalid_op', self.app.start_native, 'dev.homebrew', 'x',
                    'brew cleanup --prune=all')

    def test_a_model_removed_since_the_listing_is_refused_at_the_run(self):
        row = self.row('ollama.models')
        self.bin.tool('ollama', f'if args == ["list"]:\n    print("NAME ID SIZE MODIFIED")\n'
                                f'    print("tiny:1b a1 774 MB now")\n    sys.exit(0)\nrecord()')
        self.app = self.open_app()
        self.expect(ErrorCode.INVALID, 'error.native_bad_target', self.app.start_native, 'ollama.models', 'llama3:latest',
                    row['targets'][0]['command'])


class BusyTests(ToolCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.app.scan()
        self.gate = self.fh.root / 'gate'
        self.started = self.fh.root / 'started'

    def hold(self):
        self.bin.tool('brew', f'record()\nopen({str(self.started)!r}, "w").write("1")\nwait_for({str(self.gate)!r})')
        self.start('dev.homebrew')
        wait_until(self.started.exists)

    def release(self):
        self.gate.write_text('go')
        self.wait_job()

    def test_a_second_run_a_scan_and_a_plan_wait_for_the_first(self):
        self.hold()
        try:
            self.expect(ErrorCode.BUSY, 'error.busy', self.app.start_native, 'dev.uv', None, 'uv cache prune')
            self.expect(ErrorCode.BUSY, 'error.busy', self.app.start_scan)
            plan = make_plan(self.app, JUNK_14D)
            self.expect(ErrorCode.BUSY, 'error.busy', self.app.start_run, plan['id'], plan['revision'],
                        plan['entries_digest'])
            self.expect(ErrorCode.BUSY, 'error.busy', self.app.scan)         # the operation lock is held too
        finally:
            self.release()
        self.assertEqual(len(self.bin.calls()), 1)

    def test_a_native_run_waits_for_a_plan_run(self):
        reached, release = threading.Event(), threading.Event()

        def block(entry):
            reached.set()
            release.wait(30)
        app = self.open_app(hooks={'after_claim': block})
        plan = make_plan(app, JUNK_14D)
        runner = threading.Thread(target=lambda: app.run_plan(plan['id'], plan['revision'], plan['entries_digest']))
        runner.start()
        try:
            self.assertTrue(reached.wait(30))
            self.expect(ErrorCode.BUSY, 'error.busy', app.start_native, 'dev.homebrew', None, 'brew cleanup --prune=all')
        finally:
            release.set()
            runner.join(60)
        self.assertEqual(self.bin.calls(), [])

    def test_another_process_holding_the_operation_lock_is_busy(self):
        other = self.open_app()
        with other.lock:
            self.expect(ErrorCode.BUSY, 'error.busy', self.app.start_native, 'dev.homebrew', None,
                        'brew cleanup --prune=all')
        self.assertEqual((self.app.job()['kind'], self.app.job()['status']), (None, 'idle'))
        self.start('dev.homebrew')                                  # and fine once it is let go
        self.wait_job()

    def test_a_tool_cannot_be_paused_but_cancel_ends_it(self):
        self.hold()
        self.expect(ErrorCode.INVALID, 'error.native_no_pause', self.app.pause)       # a tool's process cannot wait
        self.expect(ErrorCode.INVALID, 'error.native_no_pause', self.app.resume)
        self.assertEqual(self.app.job()['pause_requested'], False)
        self.app.cancel()
        self.assertEqual(self.wait_job()['native']['status'], 'cancelled')

    def test_the_lock_and_the_slot_are_free_after_a_run(self):
        self.start('dev.homebrew')
        self.wait_job()
        self.assertFalse(self.app.lock.held_by_other())
        self.app.scan()
        self.start('dev.uv')
        self.assertEqual(self.wait_job()['status'], 'done')


class BlockingRunTests(ToolCase):
    def test_run_native_returns_the_result_and_passes_every_event_on(self):
        self.install()
        self.app.scan()
        seen = []
        result = self.app.run_native('dev.homebrew', None, 'brew cleanup --prune=all', lambda k, d: seen.append((k, d)))
        self.assertEqual((result['status'], result['exit_code']), ('ok', 0))
        self.assertEqual([k for k, _ in seen], ['native.output', 'native.output', 'native.done'])
        self.assertEqual(self.app.job()['status'], 'done')
        self.expect(ErrorCode.INVALID, 'error.native_command_changed', self.app.run_native, 'dev.homebrew', None, 'x')

    def test_without_a_scan_there_is_nothing_to_rescan(self):
        self.install()
        result = self.app.run_native('dev.homebrew', None, 'brew cleanup --prune=all')
        self.assertEqual((result['status'], result['rescan']), ('ok', None))
        self.assertIsNone(self.app.latest_scan())

    def test_check_native_raises_what_a_start_would_and_starts_nothing(self):
        self.install()
        self.app.check_native('dev.homebrew', None, 'brew cleanup --prune=all')
        self.expect(ErrorCode.INVALID, 'error.native_command_changed', self.app.check_native, 'dev.homebrew', None, 'x')
        self.assertEqual((self.bin.calls(), self.app.job()['kind']), ([], None))


if __name__ == '__main__':
    unittest.main()
