from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from disk_steward import macos, service
from disk_steward.cli import main
from disk_steward.macos import FakeMacOS, RealMacOS
from disk_steward.paths import item_key
from disk_steward.report import build_report, cloud_roots_of, cloud_synced, fmt_time, render_apps, render_text
from disk_steward.settings import Settings
from m3_support import make_app, make_mac
from support import Clock, FakeHome

GIB = 1024 ** 3
ICLOUD = '~/Library/Mobile Documents/com~apple~CloudDocs'

# A du that answers by the name of the path it is asked about. Nothing it prints is real.
DU = """#!/bin/sh
S='{state}'
for last; do :; done
printf '%s\\n' "$*" >> "$S/du.calls"
case "$last" in
*ok*) printf '12345\\t%s\\n' "$last"; exit 0;;
*denied*) printf '99\\t%s\\n' "$last"; echo "du: $last/inner: Operation not permitted" >&2; exit 1;;
*garbage*) echo 'nonsense'; exit 0;;
*slow*) exec sleep 5;;
esac
echo "du: $last: No such file or directory" >&2
exit 1
"""


class AppSizesAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix='ds-du-')))
        self.addCleanup(shutil.rmtree, self.tmp)         # only the temp folder created above
        self.du = self.tmp / 'du'
        self.du.write_text(DU.format(state=self.tmp), encoding='utf-8')
        self.du.chmod(0o755)
        p = mock.patch.object(macos, 'DU', str(self.du))
        p.start()
        self.addCleanup(p.stop)

    def calls(self):
        try:
            return (self.tmp / 'du.calls').read_text().splitlines()
        except FileNotFoundError:
            return []

    def test_sizes_errors_and_the_rest_carry_on(self):
        paths = ['/fake/ok1', '/fake/denied', '/fake/garbage', '/fake/missing', '/fake/ok2']
        sizes = RealMacOS().app_sizes(paths)
        self.assertEqual(list(sizes), paths)
        self.assertEqual(sizes['/fake/ok1'], {'bytes': 12345 * 1024})
        self.assertEqual(sizes['/fake/ok2'], {'bytes': 12345 * 1024})
        # a partial total must not pass for a size
        self.assertEqual(set(sizes['/fake/denied']), {'error'})
        self.assertIn('exited with 1', sizes['/fake/denied']['error'])
        self.assertIn('Operation not permitted', sizes['/fake/denied']['error'])
        self.assertEqual(set(sizes['/fake/garbage']), {'error'})
        self.assertIn('No such file or directory', sizes['/fake/missing']['error'])
        self.assertEqual(self.calls(), [f'-sk -x {p}' for p in paths])          # sequential, read-only flags

    def test_timeout_is_one_budget_for_all_paths(self):
        sizes = RealMacOS().app_sizes(['/fake/ok1', '/fake/slow', '/fake/ok2', '/fake/ok3'], timeout=2.5)
        self.assertEqual(sizes['/fake/ok1'], {'bytes': 12345 * 1024})
        self.assertEqual(sizes['/fake/slow'], {'error': 'timed out'})
        self.assertEqual(sizes['/fake/ok2'], {'error': 'timed out'})            # the budget was gone
        self.assertEqual(sizes['/fake/ok3'], {'error': 'timed out'})
        self.assertEqual(self.calls(), ['-sk -x /fake/ok1', '-sk -x /fake/slow'])    # and du never ran for them

    def test_nothing_to_measure(self):
        self.assertEqual(RealMacOS().app_sizes([]), {})
        self.assertEqual(self.calls(), [])


@unittest.skipUnless(sys.platform == 'darwin' and os.access('/usr/bin/du', os.X_OK), 'needs the macOS du')
class RealDuTests(unittest.TestCase):
    def test_measures_a_temp_folder_and_reports_a_missing_one(self):
        with tempfile.TemporaryDirectory(prefix='ds-realdu-') as tmp:
            app = Path(tmp) / 'Some.app'
            (app / 'Contents').mkdir(parents=True)
            (app / 'Contents' / 'blob').write_bytes(os.urandom(300 * 1024))
            sizes = RealMacOS().app_sizes([str(app), str(Path(tmp) / 'Gone.app')])
        self.assertGreaterEqual(sizes[str(app)]['bytes'], 300 * 1024)
        self.assertLess(sizes[str(app)]['bytes'], 400 * 1024)
        self.assertEqual(sizes[str(app)]['bytes'] % 1024, 0)
        self.assertIn('No such file', sizes[str(Path(tmp) / 'Gone.app')]['error'])


class FakeMacAppSizesTests(unittest.TestCase):
    def test_scriptable_and_recorded(self):
        mac = FakeMacOS()
        mac.app_size_map = {'/A/x.app': {'bytes': 5}, '/A/y.app': {'error': 'boom'}}
        out = mac.app_sizes(['/A/x.app', '/A/y.app', '/A/z.app'], timeout=9)
        self.assertEqual(out, {'/A/x.app': {'bytes': 5}, '/A/y.app': {'error': 'boom'}, '/A/z.app': {'bytes': 0}})
        self.assertEqual(mac.calls, [('app_sizes', (['/A/x.app', '/A/y.app', '/A/z.app'], 9))])
        out['/A/x.app']['bytes'] = 99
        self.assertEqual(mac.app_size_map['/A/x.app'], {'bytes': 5})


class AppsCase(unittest.TestCase):
    """An App on a FakeHome, with /Applications replaced by a folder in the temp root."""

    def setUp(self):
        self.fh = FakeHome(Clock()).__enter__()
        self.addCleanup(self.fh.cleanup)
        self.system = self.fh.root / 'Applications'
        self.system.mkdir()
        p = mock.patch.object(service, 'SYSTEM_APPLICATIONS', self.system)
        p.start()
        self.addCleanup(p.stop)
        self.mac = make_mac()

    def app_dir(self, base, name, size=None, error=None):
        path = base / name
        (path / 'Contents').mkdir(parents=True)
        if size is not None:
            self.mac.app_size_map[str(path)] = {'bytes': size}
        if error is not None:
            self.mac.app_size_map[str(path)] = {'error': error}
        return path

    def make(self, **kw):
        self.app = make_app(self.fh, mac=self.mac, **kw)
        self.addCleanup(self.app.close)
        return self.app


class AppSizesServiceTests(AppsCase):
    def test_lists_top_level_folders_skips_files_and_symlinks_and_sorts_by_size(self):
        big = self.app_dir(self.system, 'Big.app', 50 * GIB)
        self.app_dir(self.system, 'Small.app', 2 * GIB)
        self.app_dir(self.system, 'Utilities', 7 * GIB)                  # a plain folder counts too
        self.app_dir(self.system, 'Broken.app', error='du exited with 1: nope')
        (self.system / 'notes.txt').write_text('x')
        (self.system / 'Link.app').symlink_to(big)
        mine = self.app_dir(self.fh.home / 'Applications', 'Mine.app', 9 * GIB)
        app = self.make()
        out = app.app_sizes()
        self.assertEqual([e['path'] for e in out['entries']],
                         [f'{self.system}/Big.app', '~/Applications/Mine.app', f'{self.system}/Utilities',
                          f'{self.system}/Small.app', f'{self.system}/Broken.app'])
        self.assertEqual(out['entries'][0], {'path': f'{self.system}/Big.app', 'bytes': 50 * GIB})
        self.assertEqual(out['entries'][-1], {'path': f'{self.system}/Broken.app', 'error': 'du exited with 1: nope'})
        self.assertEqual(out['total'], 68 * GIB)
        self.assertEqual(out['measured_at'], self.fh.clock())
        asked = [c for c in self.mac.calls if c[0] == 'app_sizes']
        self.assertEqual(len(asked), 1)
        self.assertEqual(sorted(asked[0][1][0]), sorted(str(p) for p in (
            big, mine, self.system / 'Small.app', self.system / 'Utilities', self.system / 'Broken.app')))

    def test_measured_once_per_app_and_the_cache_is_not_shared_or_editable(self):
        self.app_dir(self.system, 'One.app', GIB)
        app = self.make()
        first = app.app_sizes()
        self.fh.clock.advance(seconds=600)
        self.app_dir(self.system, 'Two.app', 3 * GIB)                    # arrives after the measurement
        first['entries'].clear()
        again = app.app_sizes()
        self.assertEqual([e['path'] for e in again['entries']], [f'{self.system}/One.app'])
        self.assertEqual(again['measured_at'], first['measured_at'])
        self.assertEqual(len([c for c in self.mac.calls if c[0] == 'app_sizes']), 1)
        other = make_app(self.fh, mac=self.mac)
        self.addCleanup(other.close)
        self.assertEqual(len(other.app_sizes()['entries']), 2)           # a new App measures again
        self.assertGreater(other.app_sizes()['measured_at'], first['measured_at'])
        self.assertEqual(len([c for c in self.mac.calls if c[0] == 'app_sizes']), 2)

    def test_missing_folders_are_empty_not_an_error(self):
        shutil.rmtree(self.system)
        out = self.make().app_sizes()
        self.assertEqual((out['entries'], out['total']), ([], 0))


class ReportAppsCommandTests(AppsCase):
    def setUp(self):
        super().setUp()
        self.fh.file('~/.codex/log/codex-tui.log', size=100, age_days=10)
        for i in range(1, 13):
            self.app_dir(self.system, f'A{i:02d}.app', i * GIB)
        self.app_dir(self.fh.home / 'Applications', 'Mine.app', int(5.5 * GIB))
        self.app_dir(self.system, 'Broken.app', error='du exited with 1: Operation not permitted')
        self.make()
        self.app.scan()

    def run_cli(self, *argv):
        self.app.locale = 'en'                          # main() keeps a --lang on the App it was given
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(list(argv), app_factory=lambda: self.app)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def sizes_calls(self):
        return [c for c in self.mac.calls if c[0] == 'app_sizes']

    def test_text_section_lists_the_top_ten_with_the_time_and_the_note(self):
        code, out, err = self.run_cli('report', '--apps')
        self.assertEqual((code, err), (0, ''))
        self.assertIn('Space by tier', out)                                          # the normal report is still there
        section = out[out.index('Largest applications'):]
        self.assertTrue(section.startswith('Largest applications (not managed by Disk Steward)\n'))
        self.assertIn(f'Measured {fmt_time(self.fh.clock())}. Top 10 of 13 entries, 83.50 GiB in total.', section)
        order = [f'{self.system}/A12.app', f'{self.system}/A11.app', f'{self.system}/A10.app', f'{self.system}/A09.app',
                 f'{self.system}/A08.app', f'{self.system}/A07.app', f'{self.system}/A06.app', '~/Applications/Mine.app',
                 f'{self.system}/A05.app', f'{self.system}/A04.app']
        at = [section.index(p) for p in order]
        self.assertEqual(at, sorted(at))
        self.assertIn('12.00 GiB  ' + order[0], section)
        self.assertIn('5.50 GiB  ~/Applications/Mine.app', section)
        for left_out in ('A03.app', 'A02.app', 'A01.app'):
            self.assertNotIn(left_out, section)
        self.assertIn(f'Could not measure 1 entries: {self.system}/Broken.app: du exited with 1: Operation not permitted',
                      section)
        self.assertIn('Disk Steward does not clean applications. If you no longer use one, uninstall it yourself.',
                      section)

    def test_chinese_section(self):
        out = self.run_cli('report', '--apps', '--lang', 'zh-CN')[1]
        self.assertIn('占用最大的应用（不由 Disk Steward 管理）', out)
        self.assertIn('请自行卸载', out)
        self.assertIn('有 1 项无法测量', out)

    def test_json_carries_the_full_list(self):
        code, out, _ = self.run_cli('report', '--apps', '--json')
        self.assertEqual(code, 0)
        data = json.loads(out)
        apps = data['apps']
        self.assertEqual(set(apps), {'measured_at', 'total', 'entries'})
        self.assertEqual(len(apps['entries']), 14)                                   # 13 measured and 1 failed
        self.assertEqual(apps['entries'][0], {'path': f'{self.system}/A12.app', 'bytes': 12 * GIB})
        self.assertEqual(apps['entries'][-1]['error'], 'du exited with 1: Operation not permitted')
        self.assertEqual(apps['measured_at'], self.fh.clock())
        self.assertIn('cloud_synced', data)
        code, out, _ = self.run_cli('report', '--apps', '--since-last', '--json')
        self.assertEqual(set(json.loads(out)), {'scan', 'growth', 'apps'})

    def test_apps_with_since_last_prints_the_growth_then_the_apps(self):
        out = self.run_cli('report', '--apps', '--since-last')[1]
        self.assertTrue(out.startswith('No earlier scan to compare with.\n'))
        self.assertIn('Largest applications', out)
        self.assertNotIn('Space by tier', out)

    def test_nothing_is_measured_without_the_flag(self):
        code, out, _ = self.run_cli('report')
        self.assertEqual(code, 0)
        self.assertNotIn('Largest applications', out)
        self.assertNotIn('apps', json.loads(self.run_cli('report', '--json')[1]))
        self.assertEqual(self.sizes_calls(), [])

    def test_a_second_report_reuses_the_measurement(self):
        self.run_cli('report', '--apps')
        self.run_cli('report', '--apps', '--json')
        self.assertEqual(len(self.sizes_calls()), 1)

    def test_no_applications_and_only_failures_read_sensibly(self):
        self.mac.app_size_map.clear()
        for child in list(self.system.iterdir()) + list((self.fh.home / 'Applications').iterdir()):
            shutil.rmtree(child)
        fresh = make_app(self.fh, mac=self.mac)
        self.addCleanup(fresh.close)
        out = render_apps(fresh.app_sizes(), 'en')
        self.assertIn('No applications found in /Applications or ~/Applications.', out)
        self.assertNotIn('Measured', out)
        text = render_apps({'measured_at': 0, 'total': 0, 'entries': [{'path': '/A/x.app', 'error': 'boom'}]}, 'en')
        self.assertNotIn('No applications found', text)
        self.assertIn('Could not measure 1 entries: /A/x.app: boom', text)


class CloudCase(unittest.TestCase):
    def setUp(self):
        self.fh = FakeHome(Clock()).__enter__()
        self.addCleanup(self.fh.cleanup)
        self.mac = make_mac()

    def icloud(self, *folders):
        for f in folders:
            self.fh.dir(f'{ICLOUD}/{f}')

    def make(self, domains=('agent', 'user-files'), roots=('~/Documents', '~/Downloads')):
        self.app = make_app(self.fh, mac=self.mac, settings=Settings(
            domains=domains, user_roots=roots, large_file_min_bytes=100_000))
        self.addCleanup(self.app.close)
        return self.app

    def run_cli(self, *argv):
        self.app.locale = 'en'
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(list(argv), app_factory=lambda: self.app)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()


class CloudReportTests(CloudCase):
    EN = ('Warning: {root} is synced through iCloud Drive (Desktop & Documents). Removing files there removes them '
          'from your other devices too. Move to Trash keeps them recoverable.')

    def test_icloud_desktop_and_documents_are_warned_about_once_each(self):
        self.fh.file('~/Documents/a.txt', size=10)
        self.icloud('Documents', 'Desktop')
        self.make(roots=('~/Documents', '~/Desktop', '~/Downloads'))
        self.app.scan()
        overview = self.app.overview()
        self.assertEqual(overview['cloud_synced'], {'~/Documents': 'icloud_desktop_documents',
                                                    '~/Desktop': 'icloud_desktop_documents'})
        text = render_text(overview, 'en')
        self.assertEqual(text.count('iCloud'), 2)
        self.assertIn(self.EN.format(root='~/Documents'), text)
        self.assertIn(self.EN.format(root='~/Desktop'), text)
        self.assertNotIn('~/Downloads is synced', text)
        zh = render_text(overview, 'zh-CN')
        self.assertIn('警告：~/Documents 通过 iCloud 云盘（桌面与文稿）同步。在这里删除文件，也会从你的其他设备上删除。移到废纸篓可以恢复。', zh)
        code, out, _ = self.run_cli('scan')
        self.assertIn(self.EN.format(root='~/Documents'), out)
        data = json.loads(self.run_cli('report', '--json')[1])
        self.assertEqual(data['cloud_synced'], overview['cloud_synced'])

    def test_a_cloud_storage_folder_has_its_own_wording(self):
        self.fh.dir('~/Library/CloudStorage/Dropbox/stuff')
        settings = Settings(user_roots=('~/Library/CloudStorage/Dropbox/stuff',), domains=('agent', 'user-files'))
        self.assertEqual(cloud_synced(self.fh.bases, settings), {'~/Library/CloudStorage/Dropbox/stuff': 'cloud_folder'})
        self.make(roots=('~/Library/CloudStorage/Dropbox/stuff', '~/Downloads'))
        self.fh.file('~/Library/CloudStorage/Dropbox/stuff/f.txt', size=10)
        self.app.scan()
        text = render_text(self.app.overview(), 'en')
        self.assertIn('Warning: ~/Library/CloudStorage/Dropbox/stuff is in a cloud-synced folder. Removing files there '
                      'removes them from your other devices too. Move to Trash keeps them recoverable.', text)

    def test_nothing_is_said_when_the_user_files_area_is_off_or_nothing_is_synced(self):
        self.icloud('Documents')
        self.make(domains=('agent',))
        self.app.scan()
        overview = self.app.overview()
        self.assertEqual(overview['cloud_synced'], {})
        self.assertNotIn('iCloud', render_text(overview, 'en'))
        self.app.update_settings({'domains': ['agent', 'user-files']})
        self.assertEqual(self.app.overview()['cloud_synced'], {'~/Documents': 'icloud_desktop_documents'})
        shutil.rmtree(self.fh.path(f'{ICLOUD}/Documents'))
        self.assertEqual(self.app.overview()['cloud_synced'], {})

    def test_without_bases_build_report_looks_for_nothing(self):
        self.icloud('Documents')
        self.make()
        self.app.scan()
        sid = self.app.latest_scan()['id']
        args = (self.app.store, sid, self.app._load_catalog(), self.app.settings, self.fh.clock())
        self.assertEqual(build_report(*args)['cloud_synced'], {})
        self.assertEqual(build_report(*args, bases=self.fh.bases)['cloud_synced'],
                         {'~/Documents': 'icloud_desktop_documents'})

    def test_cloud_roots_of_picks_the_innermost_root_and_respects_segment_boundaries(self):
        synced = {'~/Desktop': 'icloud_desktop_documents', '~/Desktop/Sub': 'cloud_folder'}
        self.assertEqual(cloud_roots_of(['~/Desktop/Sub/x', '~/Desktop/y'], synced), synced)
        self.assertEqual(cloud_roots_of(['~/Desktop/Sub/x'], synced), {'~/Desktop/Sub': 'cloud_folder'})
        self.assertEqual(cloud_roots_of(['~/DesktopOld/x', '~/Downloads/z'], synced), {})
        self.assertEqual(cloud_roots_of(['~/Desktop'], synced), {'~/Desktop': 'icloud_desktop_documents'})


class PlanCloudWarningTests(CloudCase):
    DOCS = 'user.large:~/Documents'
    DOWNLOADS = 'user.large:~/Downloads'

    def setUp(self):
        super().setUp()
        self.fh.file('~/Documents/big.bin', size=200_000, age_days=30)
        self.fh.file('~/Downloads/big2.bin', size=200_000, age_days=30)
        self.fh.file('~/.claude/debug/x.txt', size=100, age_days=5)             # junk of the agent area
        self.icloud('Documents')
        self.make()
        self.app.scan()

    def plan(self, *argv):
        return self.run_cli('plan', '--older-than', '0', *argv)

    def test_a_plan_with_entries_under_a_synced_root_warns(self):
        code, out, err = self.plan('--select', self.DOCS, '--dry-run')
        self.assertEqual((code, err), (0, ''))
        self.assertIn('Warning: ~/Documents is synced through iCloud Drive', out)
        self.assertIn('Move to Trash keeps them recoverable.', out)
        self.assertEqual(out.count('iCloud'), 1)
        self.assertIn('Dry run: no plan was created.', out)
        self.assertLess(out.index('iCloud'), out.index('Dry run'))

    def test_a_created_plan_and_json_carry_it_too(self):
        code, out, _ = self.plan('--select', self.DOCS)
        self.assertEqual(code, 0)
        self.assertIn('iCloud Drive', out)
        self.assertIn('Plan ', out)
        code, out, _ = self.plan('--select', self.DOCS, '--select', self.DOWNLOADS, '--json')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['cloud_synced'], {'~/Documents': 'icloud_desktop_documents'})

    def test_no_warning_for_a_plan_outside_the_synced_roots(self):
        for select in (['--select', self.DOWNLOADS], ['--select', 'junk']):
            with self.subTest(select=select):
                code, out, err = self.plan(*select, '--dry-run')
                self.assertEqual((code, err), (0, ''))
                self.assertNotIn('iCloud', out)
        code, out, _ = self.plan('--select', self.DOWNLOADS, '--dry-run', '--json')
        self.assertEqual(json.loads(out)['cloud_synced'], {})

    def test_an_explicitly_included_item_counts(self):
        code, out, _ = self.plan('--select', self.DOWNLOADS, '--include', item_key('~/Documents/big.bin'), '--dry-run')
        self.assertEqual(code, 0)
        self.assertIn('Warning: ~/Documents is synced through iCloud Drive', out)

    def test_an_entry_the_plan_does_not_take_does_not_warn(self):
        # the only file under the synced root is excluded, so the plan holds nothing from it
        code, out, _ = self.plan('--select', self.DOCS, '--select', self.DOWNLOADS, '--exclude', '*/Documents/*',
                                 '--dry-run')
        self.assertEqual(code, 0)
        self.assertNotIn('iCloud', out)

    def test_chinese_warning(self):
        code, out, _ = self.plan('--select', self.DOCS, '--dry-run', '--lang', 'zh-CN')
        self.assertEqual(code, 0)
        self.assertIn('警告：~/Documents 通过 iCloud 云盘（桌面与文稿）同步', out)

    def test_no_warning_when_the_user_files_area_is_off(self):
        self.app.update_settings({'domains': ['agent']})
        self.assertEqual(cloud_synced(self.app.bases, self.app.settings), {})
        code, out, _ = self.plan('--select', 'junk', '--dry-run')
        self.assertEqual(code, 0)
        self.assertNotIn('iCloud', out)


if __name__ == '__main__':
    unittest.main()
