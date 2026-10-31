from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from disk_steward import macos
from disk_steward.macos import FakeMacOS, OpenFiles, RealMacOS, parse_lsof
from disk_steward.model import ErrorCode, StewardError
from support import render_error

DARWIN = sys.platform == 'darwin'


def completed(argv, rc=0, out='', err=''):
    return subprocess.CompletedProcess(argv, rc, out, err)


LSOF_SAMPLE = '\n'.join([
    'p100', 'cEditor', 'fcwd', 'tDIR', 'D0x1000010', 'i11', 'n/work',
    'ftxt', 'tREG', 'D0x1000010', 'i12', 'n/Applications/Editor.app/Contents/MacOS/Editor',
    'f3', 'tREG', 'D0x1000010', 'i13', 'n/work/notes.txt',
    'f4', 'tREG', 'D0x1000010', 'i13', 'n/work/notes.txt',          # same file, second descriptor
    'f5', 'tPIPE',
    'f6', 'tunix', 'n/private/var/run/sock',
    'p200', 'cGoogle Chrome Helper', 'f9', 'tREG', 'D0x1000010', 'i13', 'n/work/notes.txt',
    'f10', 'tREG', 'D0x2000001', 'i13', 'n/Volumes/Other/notes.txt',
    'f11', 'tREG', 'D0x1000010', 'i14', 'n/work2/other.txt',
    '',
])


class ParseLsofTests(unittest.TestCase):
    def setUp(self):
        self.snap = parse_lsof(LSOF_SAMPLE, 42.0)

    def test_by_inode_uses_hex_device_and_dedupes_labels(self):
        dev = 0x1000010
        self.assertEqual(self.snap.taken_at, 42.0)
        self.assertEqual(self.snap.holders(dev, 13), ['Editor(100)', 'Google Chrome Helper(200)'])
        self.assertEqual(self.snap.holders(0x2000001, 13), ['Google Chrome Helper(200)'])
        self.assertEqual(self.snap.holders(dev, 999), [])
        self.assertEqual(self.snap.holders(dev, 11), ['Editor(100)'])

    def test_executing_holds_only_txt_entries(self):
        self.assertEqual(self.snap.executing, {(0x1000010, 12): ['Editor(100)']})
        self.assertEqual(self.snap.holders(0x1000010, 12), ['Editor(100)'])

    def test_paths_are_sorted_absolute_names_only(self):
        self.assertEqual(self.snap.paths, sorted(self.snap.paths))
        self.assertIn('/work/notes.txt', self.snap.paths)
        self.assertIn('/private/var/run/sock', self.snap.paths)
        self.assertTrue(all(p.startswith('/') for p in self.snap.paths))

    def test_holders_under_respects_segment_boundaries(self):
        self.assertEqual(self.snap.holders_under('/work'), ['Editor(100)', 'Google Chrome Helper(200)'])
        self.assertEqual(self.snap.holders_under('/work/'), ['Editor(100)', 'Google Chrome Helper(200)'])
        self.assertEqual(self.snap.holders_under('/work2'), ['Google Chrome Helper(200)'])
        self.assertEqual(self.snap.holders_under('/wor'), [])
        self.assertEqual(self.snap.holders_under('/work/notes.txt'), ['Editor(100)', 'Google Chrome Helper(200)'])
        self.assertEqual(self.snap.holders_under('/work/notes'), [])
        self.assertEqual(self.snap.holders_under('/Applications/Editor.app'), ['Editor(100)'])
        self.assertEqual(self.snap.holders_under('/nothing'), [])
        self.assertEqual(len(self.snap.holders_under('/')), 2)

    def test_unexpected_field_fails_loudly(self):
        with self.assertRaises(RuntimeError):
            parse_lsof('p1\ncX\nfcwd\nZoops\n', 0)

    # What lsof prints for odd names in a non-UTF-8 locale (verified against the real tool): every non-ASCII byte as
    # \xNN, tab, newline and backslash as \t \n \\, other control characters as ^A.
    ESCAPED = '\n'.join([
        'p7', 'cApp', 'f3', 'tREG', 'D0x1', 'i31', r'n/Users/me/\xe5\xbe\xae\xe4\xbf\xa1 dir/f.txt',
        'f4', 'tREG', 'D0x1', 'i32', r'n/Users/me/caf\xc3\xa9/a\tb\nc\\d.txt',
        'f5', 'tREG', 'D0x1', 'i33', 'n/Users/me/ctl^Ax/f',
        'f6', 'tREG', 'D0x1', 'i34', r'n/Users/me/del\x7fx/f',
        '',
    ])

    def test_escaped_names_are_decoded_so_holders_under_finds_them(self):
        snap = parse_lsof(self.ESCAPED, 0)
        self.assertIn('/Users/me/微信 dir/f.txt', snap.paths)
        self.assertEqual(snap.holders_under('/Users/me/微信 dir'), ['App(7)'])
        self.assertEqual(snap.holders_under('/Users/me/café'), ['App(7)'])
        self.assertIn('/Users/me/café/a\tb\nc\\d.txt', snap.paths)
        self.assertIn('/Users/me/del\x7fx/f', snap.paths)
        self.assertTrue(all('\\x' not in p for p in snap.paths))

    def test_a_caret_may_be_a_control_character_or_a_literal_caret(self):
        snap = parse_lsof(self.ESCAPED, 0)
        self.assertIn('/Users/me/ctl^Ax/f', snap.paths)
        self.assertIn('/Users/me/ctl\x01x/f', snap.paths)     # registered both ways: stricter, never looser
        self.assertEqual(snap.holders_under('/Users/me/ctl\x01x'), ['App(7)'])

    def test_bytes_that_are_not_utf8_fail_loudly(self):
        for bad in (r'n/Users/me/\xff\xfe/f', r'n/Users/me/\xe5\xbe/f'):
            with self.subTest(bad=bad), self.assertRaisesRegex(RuntimeError, 'not valid UTF-8'):
                parse_lsof(f'p1\ncX\nf3\ntREG\nD0x1\ni2\n{bad}\n', 0)

    def test_plain_backslashes_that_are_no_escape_stay(self):
        snap = parse_lsof('p1\ncX\nf3\ntREG\nD0x1\ni2\nn/a/b\\q/\\xZZ\n', 0)
        self.assertEqual(snap.paths, ['/a/b\\q/\\xZZ'])

    def test_an_undecodable_name_makes_the_guard_fail_rather_than_see_nothing(self):
        from disk_steward.guards import Guard, SnapshotError
        real = RealMacOS()
        bad = 'p1\ncX\nf3\ntREG\nD0x1\ni2\nn/a/\\xff\n'
        with mock.patch.object(macos, '_run', side_effect=lambda argv, timeout, env=None: completed(argv, 0, bad)):
            with self.assertRaises(SnapshotError):
                Guard(real, lambda: 0.0).refresh()

    def test_malformed_numbers_fail_loudly(self):
        with self.assertRaises(ValueError):
            parse_lsof('p1\ncX\nf3\nDnothex\n', 0)
        with self.assertRaises(ValueError):
            parse_lsof('pabc\n', 0)

    def test_empty_output(self):
        snap = parse_lsof('', 1.0)
        self.assertEqual((snap.by_inode, snap.paths, snap.executing), ({}, [], {}))


class ParseMdlsTests(unittest.TestCase):
    def test_last_used_values(self):
        self.assertIsNone(macos._parse_last_used('(null)'))
        self.assertEqual(macos._parse_last_used('2026-10-28 10:07:57 +0000'),
                         datetime(2026, 10, 28, 10, 7, 57, tzinfo=timezone.utc).timestamp())
        self.assertEqual(macos._parse_last_used('2026-10-28 18:07:57 +0800'),
                         datetime(2026, 10, 28, 10, 7, 57, tzinfo=timezone.utc).timestamp())
        with self.assertRaises(ValueError):
            macos._parse_last_used('yesterday')


class ScriptedRealMacOSTests(unittest.TestCase):
    """RealMacOS against canned command output, so the parsing rules are covered on any platform."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.real = RealMacOS(Path(self.tmp.name) / 'home')

    def patch_run(self, fn):
        p = mock.patch.object(macos, '_run', side_effect=fn)
        p.start()
        self.addCleanup(p.stop)

    def test_running_bundle_ids(self):
        out = (' 1) "Finder" ASN:0x0-0x1001:\n    bundleID="com.apple.finder"\n    bundle path="/x"\n'
               ' 2) "NoID" ASN:0x0-0x2:\n    bundleID=[ NULL ]\n 3) "Terminal"\n    bundleID="com.apple.Terminal"\n')
        self.patch_run(lambda argv, timeout: completed(argv, 0, out))
        self.assertEqual(self.real.running_bundle_ids(), {'com.apple.finder', 'com.apple.Terminal'})

    def test_running_bundle_ids_failure_is_not_an_empty_set(self):
        self.patch_run(lambda argv, timeout: completed(argv, 1, '', 'no window server'))
        with self.assertRaisesRegex(RuntimeError, 'no window server'):
            self.real.running_bundle_ids()

    def test_lsof_exit_codes(self):
        text = 'p1\ncX\nf3\ntREG\nD0x1\ni2\nn/a\n'
        for rc, out, ok in ((0, text, True), (1, text, True), (1, '', False), (2, text, False), (0, '', False)):
            with self.subTest(rc=rc, has_output=bool(out)):
                self.patch_run(lambda argv, timeout, env=None, rc=rc, out=out: completed(argv, rc, out, 'denied'))
                if ok:
                    self.assertEqual(self.real.open_files().holders(1, 2), ['X(1)'])
                else:
                    with self.assertRaises(RuntimeError):
                        self.real.open_files()

    def test_lsof_command_line(self):
        seen = []
        self.patch_run(lambda argv, timeout, env=None: seen.append((argv, timeout, env)) or completed(argv, 0, 'p1\ncX\n'))
        with mock.patch.dict(os.environ, {'LC_ALL': 'C', 'LANG': 'C', 'KEEP': 'me'}):
            self.real.open_files()
        ((argv, timeout, env),) = seen
        self.assertEqual((argv, timeout), (['/usr/sbin/lsof', '-nP', '-F', 'pcftDin', '-w'], 30))
        self.assertEqual((env['LC_ALL'], env['LANG'], env['KEEP']), ('en_US.UTF-8', 'en_US.UTF-8', 'me'))   # UTF-8 whatever the caller has

    def test_last_used_batches_of_200(self):
        calls = []

        def fake(argv, timeout):
            paths = argv[6:]
            calls.append(len(paths))
            return completed(argv, 0, '\0'.join('2026-10-28 10:00:00 +0000' if p.endswith('0') else '(null)'
                                                for p in paths))
        self.patch_run(fake)
        paths = []
        for n in range(450):
            p = os.path.join(self.tmp.name, f'f{n}')
            open(p, 'w').close()
            paths.append(p)
        got = self.real.last_used(paths + [os.path.join(self.tmp.name, 'gone')])
        self.assertEqual(calls, [200, 200, 50])
        self.assertEqual(len(got), 451)
        self.assertIsNone(got[os.path.join(self.tmp.name, 'gone')])      # never sent to mdls
        self.assertIsNone(got[paths[1]])
        self.assertEqual(got[paths[10]], datetime(2026, 10, 28, 10, tzinfo=timezone.utc).timestamp())

    def test_last_used_retries_one_by_one_when_a_file_vanishes_mid_batch(self):
        a, b = (os.path.join(self.tmp.name, n) for n in 'ab')
        for p in (a, b):
            open(p, 'w').close()

        def fake(argv, timeout):
            paths = argv[6:]
            if len(paths) == 2:
                return completed(argv, 1, f'{a}: could not find {b}.\n')
            if paths == [a]:
                return completed(argv, 0, '2026-10-28 10:00:00 +0000')
            return completed(argv, 1, f'{b}: could not find {b}.\n')
        self.patch_run(fake)
        self.assertEqual(self.real.last_used([a, b]),
                         {a: datetime(2026, 10, 28, 10, tzinfo=timezone.utc).timestamp(), b: None})

    def test_last_used_other_failures_are_loud(self):
        a = os.path.join(self.tmp.name, 'a')
        open(a, 'w').close()
        self.patch_run(lambda argv, timeout: completed(argv, 1, 'mdls: something else broke'))
        with self.assertRaises(RuntimeError):
            self.real.last_used([a])
        self.patch_run(lambda argv, timeout: completed(argv, 0, 'x\0y'))
        with self.assertRaisesRegex(RuntimeError, '2 values for 1 paths'):
            self.real.last_used([a])

    def test_last_used_nothing_to_ask(self):
        self.patch_run(lambda argv, timeout: self.fail('mdls must not run'))
        self.assertEqual(self.real.last_used([]), {})

    def test_installed_apps_reads_plists_and_skips_unreadable(self):
        root = Path(self.tmp.name)

        def make_app(parent, name, bundle_id, binary=False):
            info = parent / f'{name}.app' / 'Contents' / 'Info.plist'
            info.parent.mkdir(parents=True)
            fmt = plistlib.FMT_BINARY if binary else plistlib.FMT_XML
            info.write_bytes(plistlib.dumps({'CFBundleIdentifier': bundle_id} if bundle_id else {}, fmt=fmt))
        spot = root / 'spot'
        spot.mkdir()
        make_app(spot, 'Found', 'com.example.found')
        make_app(spot, 'Binary', 'com.example.binary', binary=True)
        make_app(spot, 'NoId', None)
        (spot / 'Broken.app' / 'Contents').mkdir(parents=True)
        (spot / 'Broken.app' / 'Contents' / 'Info.plist').write_text('<plist><dict>')
        (spot / 'Empty.app').mkdir()
        home_apps = root / 'home' / 'Applications'
        home_apps.mkdir(parents=True)
        make_app(home_apps, 'Mine', 'com.example.mine')
        make_app(home_apps, 'Found', 'com.example.found')           # duplicate id: first path (sorted) wins
        mdfind_out = ''.join(f'{spot / n}.app\n' for n in ('Found', 'Binary', 'NoId', 'Broken', 'Empty')) + '\n'
        self.patch_run(lambda argv, timeout: completed(argv, 0, mdfind_out))
        apps = self.real.installed_apps()
        self.assertEqual(apps['com.example.found'], str(home_apps / 'Found.app'))
        self.assertEqual(apps['com.example.binary'], str(spot / 'Binary.app'))
        self.assertEqual(apps['com.example.mine'], str(home_apps / 'Mine.app'))
        self.assertFalse([v for v in apps.values() if v.endswith(('NoId.app', 'Broken.app', 'Empty.app'))])

    def test_installed_apps_mdfind_failure_is_loud(self):
        self.patch_run(lambda argv, timeout: completed(argv, 3, '', 'spotlight off'))
        with self.assertRaisesRegex(RuntimeError, 'spotlight off'):
            self.real.installed_apps()

    def test_snapshots_keep_only_snapshot_lines(self):
        out = ("Snapshots for volume group containing disk '/':\n"
               'com.apple.TimeMachine.2026-10-27-120000.local\n'
               'com.apple.os.update-ABCD\n')
        self.patch_run(lambda argv, timeout: completed(argv, 0, out))
        self.assertEqual(self.real.snapshots(), ['com.apple.TimeMachine.2026-10-27-120000.local',
                                                 'com.apple.os.update-ABCD'])
        self.patch_run(lambda argv, timeout: completed(argv, 1, '', 'tmutil: nope'))
        with self.assertRaises(RuntimeError):
            self.real.snapshots()

    def test_trash_missing_binary(self):
        with mock.patch.object(macos, 'TRASH', os.path.join(self.tmp.name, 'no-such-trash')):
            self.assertFalse(self.real.trash_available())
            with self.assertRaises(StewardError) as cm:
                self.real.trash('/tmp/x')
        self.assertEqual(cm.exception.code, ErrorCode.UNAVAILABLE)
        self.assertEqual(cm.exception.message_key, 'error.trash_unavailable')
        render_error(cm.exception)

    def test_trash_failure_and_arguments(self):
        fake_trash = os.path.join(self.tmp.name, 'trash')
        open(fake_trash, 'w').close()
        os.chmod(fake_trash, 0o755)
        seen = []
        with mock.patch.object(macos, 'TRASH', fake_trash):
            self.patch_run(lambda argv, timeout: seen.append(argv) or completed(argv, 0))
            self.real.trash('/some/file')
            self.assertEqual(seen, [[fake_trash, '/some/file']])
            with self.assertRaises(ValueError):
                self.real.trash('relative/file')
            self.patch_run(lambda argv, timeout: completed(argv, 1, '', 'permission denied'))
            with self.assertRaisesRegex(RuntimeError, 'permission denied'):
                self.real.trash('/some/file')

    def test_tool_version(self):
        self.assertIsNone(self.real.tool_version('definitely-not-an-installed-tool'))
        with mock.patch.object(macos.shutil, 'which', return_value='/x/codex'):
            self.patch_run(lambda argv, timeout: completed(argv, 0, ' codex-cli 1.2.3 \nmore\n'))
            self.assertEqual(self.real.tool_version('codex'), 'codex-cli 1.2.3')
            self.patch_run(lambda argv, timeout: completed(argv, 0, '\n'))
            with self.assertRaises(RuntimeError):
                self.real.tool_version('codex')
            self.patch_run(lambda argv, timeout: completed(argv, 2, '', 'bad flag'))
            with self.assertRaises(RuntimeError):
                self.real.tool_version('codex')


class FakeMacOSTests(unittest.TestCase):
    def test_defaults_and_call_recording(self):
        fake = FakeMacOS(running=['com.a'], last_used_map={'/x': 5.0}, apps={'com.a': '/Applications/A.app'},
                         free=123, usage=(10, 4), snapshot_names=['com.apple.s'], tool_versions={'codex': '1.0'})
        self.assertEqual(fake.running_bundle_ids(), {'com.a'})
        self.assertEqual(fake.last_used(['/x', '/y']), {'/x': 5.0, '/y': None})
        self.assertEqual(fake.installed_apps(), {'com.a': '/Applications/A.app'})
        self.assertEqual((fake.statfs_free('/z'), fake.volume_usage('/z')), (123, (10, 4)))
        self.assertEqual(fake.snapshots(), ['com.apple.s'])
        self.assertEqual((fake.tool_version('codex'), fake.tool_version('claude')), ('1.0', None))
        self.assertEqual([c[0] for c in fake.calls], ['running_bundle_ids', 'last_used', 'installed_apps',
                                                      'statfs_free', 'volume_usage', 'snapshots', 'tool_version',
                                                      'tool_version'])
        self.assertEqual(fake.calls[1], ('last_used', (['/x', '/y'],)))

    def test_trash_records_or_refuses(self):
        fake = FakeMacOS()
        fake.trash('/a')
        self.assertEqual(fake.trashed, ['/a'])
        fake.trash_ok = False
        self.assertFalse(fake.trash_available())
        with self.assertRaises(StewardError) as cm:
            fake.trash('/b')
        self.assertEqual(cm.exception.code, ErrorCode.UNAVAILABLE)
        self.assertEqual(fake.trashed, ['/a'])

    def test_open_snapshot_is_scriptable(self):
        snap = OpenFiles(1.0, by_inode={(1, 2): ['Foo(3)']}, path_holders={'/a/b': ['Foo(3)']})
        fake = FakeMacOS(open_snapshot=snap)
        self.assertIs(fake.open_files(), snap)
        fake.open = OpenFiles()
        self.assertEqual(fake.open_files().holders(1, 2), [])

    def test_state_is_not_shared_between_instances(self):
        a, b = FakeMacOS(), FakeMacOS()
        a.running.add('x')
        a.trash('/p')
        self.assertEqual((b.running, b.trashed, b.calls), (set(), [], []))


@unittest.skipUnless(DARWIN, 'needs macOS')
class RealMacOSTests(unittest.TestCase):
    """Read-only probes of the real system. Nothing here writes outside a temp dir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.real = RealMacOS()

    def test_lsof_sees_a_file_this_process_holds_open(self):
        path = os.path.join(os.path.realpath(self.tmp.name), 'held 中文.txt')
        with open(path, 'wb') as f:
            f.write(b'x')
            st = os.fstat(f.fileno())
            snap = self.real.open_files()
            pid_label = f'({os.getpid()})'
            holders = snap.holders(st.st_dev, st.st_ino)       # lsof's hex D field must equal st_dev
            self.assertTrue(any(h.endswith(pid_label) for h in holders), holders)
            self.assertIn(path, snap.paths)
            self.assertTrue(any(h.endswith(pid_label) for h in snap.holders_under(os.path.dirname(path))))
            self.assertLessEqual(snap.taken_at, time.time())
        after = self.real.open_files()
        self.assertFalse(any(h.endswith(pid_label) for h in after.holders(st.st_dev, st.st_ino)))

    def test_lsof_finds_a_file_in_a_non_ascii_folder_whatever_the_callers_locale(self):
        folder = os.path.join(os.path.realpath(self.tmp.name), '微信 dir')
        os.mkdir(folder)
        pid_label = f'({os.getpid()})'
        for locale in ('C', 'POSIX', 'en_US.UTF-8'):
            with self.subTest(locale=locale):
                path = os.path.join(folder, f'f-{locale}.txt')
                with mock.patch.dict(os.environ, {'LC_ALL': locale, 'LANG': locale}):
                    with open(path, 'wb') as f:
                        f.write(b'x')
                        snap = self.real.open_files()
                self.assertIn(path, snap.paths)
                self.assertTrue(any(h.endswith(pid_label) for h in snap.holders_under(folder)))

    def test_lsof_reports_the_running_interpreter_as_executing(self):
        snap = self.real.open_files()
        self.assertTrue(snap.executing)
        self.assertTrue(set(snap.executing) <= set(snap.by_inode))
        mine = [k for k, v in snap.executing.items() if any(h.endswith(f'({os.getpid()})') for h in v)]
        self.assertTrue(mine)

    def test_lsappinfo_lists_running_apps(self):
        ids = self.real.running_bundle_ids()
        self.assertTrue(ids)
        self.assertTrue(all(isinstance(i, str) and i for i in ids))

    def test_mdls_on_temp_files(self):
        base = os.path.realpath(self.tmp.name)
        paths = []
        for n in range(205):                                   # crosses the 200-path batch boundary
            p = os.path.join(base, f'f{n}')
            open(p, 'w').close()
            paths.append(p)
        gone = os.path.join(base, 'gone')
        got = self.real.last_used(paths + [gone])
        self.assertEqual(set(got), set(paths) | {gone})
        self.assertIsNone(got[gone])
        for value in got.values():
            self.assertTrue(value is None or isinstance(value, float))

    def test_mdls_knows_a_system_app(self):
        got = self.real.last_used(['/System/Library/CoreServices/Finder.app'])
        self.assertEqual(list(got), ['/System/Library/CoreServices/Finder.app'])

    def test_installed_apps(self):
        apps = self.real.installed_apps()
        self.assertTrue(apps)
        self.assertTrue(all(path.endswith('.app') for path in apps.values()))
        bundle_id, path = sorted(apps.items())[0]
        with open(os.path.join(path, 'Contents', 'Info.plist'), 'rb') as f:
            self.assertEqual(plistlib.load(f)['CFBundleIdentifier'], bundle_id)

    def test_statvfs(self):
        base = os.path.realpath(self.tmp.name)
        total, used = self.real.volume_usage(base)
        free = self.real.statfs_free(base)
        self.assertGreater(total, 0)
        self.assertGreater(used, 0)
        self.assertGreater(free, 0)
        self.assertLessEqual(used, total)
        self.assertLessEqual(free, total)

    def test_trash_available_matches_the_binary(self):
        self.assertEqual(self.real.trash_available(), os.access('/usr/bin/trash', os.X_OK))

    def test_snapshots(self):
        snaps = self.real.snapshots()
        self.assertTrue(all(s.startswith('com.apple') for s in snaps))

    def test_tool_version(self):
        self.assertTrue(self.real.tool_version('python3').startswith('Python 3'))
        self.assertIsNone(self.real.tool_version('definitely-not-an-installed-tool'))


if __name__ == '__main__':
    unittest.main()
