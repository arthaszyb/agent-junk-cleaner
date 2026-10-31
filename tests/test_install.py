"""scripts/install.sh in a temporary HOME: install, update, refuse to overwrite a file of someone else's, uninstall.
Nothing here touches the real home: the script only writes under the HOME it is given."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
import zipapp
from pathlib import Path

from disk_steward import schedule

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / 'scripts' / 'install.sh'
PYTHON3 = shutil.which('python3')

MAIN = ("import sys\n"
        "print('disk-steward {version}' if sys.argv[1:] == ['--version'] else 'args: ' + ' '.join(sys.argv[1:]))\n")


@unittest.skipUnless(os.path.exists('/bin/sh') and PYTHON3, 'needs /bin/sh and python3')
class InstallScriptTests(unittest.TestCase):
    shell = '/bin/sh'

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='ds-install-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / 'home'
        self.home.mkdir()
        self.share = self.home / '.local' / 'share' / 'disk-steward'
        self.pyz = self.share / 'disk-steward.pyz'
        self.wrapper = self.home / '.local' / 'bin' / 'disk-steward'
        self.data_dir = self.home / 'Library' / 'Application Support' / 'Disk Steward'

    def make_pyz(self, version='9.9.9', name='disk-steward-9.9.9.pyz'):
        stage = self.tmp / f'stage-{version}'
        stage.mkdir()
        (stage / '__main__.py').write_text(MAIN.format(version=version), encoding='utf-8')
        target = self.tmp / name
        zipapp.create_archive(stage, target, interpreter='/usr/bin/env python3')
        return target

    def env(self, on_path=False):
        bin_dir = os.path.dirname(PYTHON3)
        parts = ([str(self.wrapper.parent)] if on_path else []) + [bin_dir, '/usr/bin', '/bin']
        return {'HOME': str(self.home), 'PATH': os.pathsep.join(parts)}

    def run_script(self, *args, on_path=False):
        return subprocess.run([self.shell, str(INSTALL), *map(str, args)], env=self.env(on_path),
                              capture_output=True, text=True, timeout=60)

    def run_wrapper(self, *args):
        return subprocess.run([str(self.wrapper), *args], env=self.env(), capture_output=True, text=True, timeout=60)

    def test_install_writes_the_pyz_and_a_wrapper_that_runs_it(self):
        r = self.run_script(self.make_pyz())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('Installed disk-steward 9.9.9', r.stdout)
        self.assertTrue(self.pyz.is_file())
        self.assertTrue(os.access(self.wrapper, os.X_OK))
        text = self.wrapper.read_text(encoding='utf-8')
        self.assertIn('exec /usr/bin/env python3 "$HOME/.local/share/disk-steward/disk-steward.pyz" "$@"', text)
        out = self.run_wrapper('portal', '--open')
        self.assertEqual((out.returncode, out.stdout.strip()), (0, 'args: portal --open'), out.stderr)
        self.assertEqual(self.run_wrapper('--version').stdout.strip(), 'disk-steward 9.9.9')

    def test_it_writes_only_the_two_files(self):
        self.run_script(self.make_pyz())
        found = sorted(p.relative_to(self.home).as_posix() for p in self.home.rglob('*') if p.is_file())
        self.assertEqual(found, ['.local/bin/disk-steward', '.local/share/disk-steward/disk-steward.pyz'])

    def test_it_says_how_to_add_the_bin_folder_to_path_only_when_it_is_missing(self):
        pyz = self.make_pyz()
        missing = self.run_script(pyz)
        self.assertIn('is not on your PATH', missing.stdout)
        self.assertIn('.zshrc', missing.stdout)
        present = self.run_script(pyz, on_path=True)
        self.assertNotIn('PATH', present.stdout)
        self.assertIn('Run: disk-steward portal --open', present.stdout)

    def test_installing_again_replaces_the_pyz(self):
        self.run_script(self.make_pyz('9.9.9'))
        r = self.run_script(self.make_pyz('9.9.8', 'disk-steward-9.9.8.pyz'))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('Updated disk-steward 9.9.8', r.stdout)
        self.assertEqual(self.run_wrapper('--version').stdout.strip(), 'disk-steward 9.9.8')
        self.assertEqual(sorted(p.name for p in self.share.iterdir()), ['disk-steward.pyz'])
        self.assertEqual(sorted(p.name for p in self.wrapper.parent.iterdir()), ['disk-steward'])

    def test_it_refuses_to_overwrite_a_file_that_is_not_its_wrapper(self):
        self.wrapper.parent.mkdir(parents=True)
        self.wrapper.write_text('#!/bin/sh\necho mine\n', encoding='utf-8')
        r = self.run_script(self.make_pyz())
        self.assertEqual(r.returncode, 1)
        self.assertIn('did not write it', r.stderr)
        self.assertEqual(self.wrapper.read_text(encoding='utf-8'), '#!/bin/sh\necho mine\n')
        self.assertFalse(self.share.exists())

    def test_it_refuses_a_symlink_at_the_wrapper_path_and_leaves_its_target_alone(self):
        target = self.tmp / 'target'
        target.write_text('# disk-steward wrapper written by install.sh\n', encoding='utf-8')
        self.wrapper.parent.mkdir(parents=True)
        os.symlink(target, self.wrapper)
        r = self.run_script(self.make_pyz())
        self.assertEqual(r.returncode, 1)
        self.assertTrue(self.wrapper.is_symlink())
        self.assertEqual(target.read_text(encoding='utf-8'), '# disk-steward wrapper written by install.sh\n')
        self.assertFalse(self.share.exists())

    def test_it_refuses_a_file_that_does_not_run_as_disk_steward(self):
        page = self.tmp / 'disk-steward-9.9.9.pyz'
        page.write_text('<html>Not Found</html>\n', encoding='utf-8')
        r = self.run_script(page)
        self.assertEqual(r.returncode, 1)
        self.assertIn('download it again', r.stderr)
        self.assertFalse((self.home / '.local').exists())

    def test_it_needs_one_path_or_a_flag(self):
        for args in ([], ['--nope'], ['a.pyz', 'b.pyz'], ['--uninstall', 'x']):
            with self.subTest(args=args):
                r = self.run_script(*args)
                self.assertEqual(r.returncode, 2)
                self.assertIn('Usage', r.stderr)
        self.assertFalse((self.home / '.local').exists())

    def test_uninstall_removes_the_wrapper_and_the_pyz_and_keeps_the_data(self):
        self.run_script(self.make_pyz())
        self.data_dir.mkdir(parents=True)
        (self.data_dir / 'steward.db').write_text('data', encoding='utf-8')
        r = self.run_script('--uninstall')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.wrapper.exists())
        self.assertFalse(self.pyz.exists())
        self.assertFalse(self.share.exists())
        self.assertEqual((self.data_dir / 'steward.db').read_text(encoding='utf-8'), 'data')
        self.assertIn('Your data stays', r.stdout)
        self.assertIn(f'rm -r "{self.data_dir}"', r.stdout)

    def test_uninstall_leaves_a_wrapper_it_did_not_write(self):
        self.run_script(self.make_pyz())
        self.wrapper.write_text('#!/bin/sh\necho mine\n', encoding='utf-8')
        r = self.run_script('--uninstall')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('Left', r.stdout)
        self.assertEqual(self.wrapper.read_text(encoding='utf-8'), '#!/bin/sh\necho mine\n')
        self.assertFalse(self.pyz.exists())

    def test_uninstall_without_an_install_removes_nothing(self):
        r = self.run_script('--uninstall')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('Nothing to remove', r.stdout)

    def test_uninstall_waits_while_the_weekly_scan_runs_the_installed_pyz(self):
        self.run_script(self.make_pyz())
        plist = self.home / 'Library' / 'LaunchAgents' / f'{schedule.LABEL}.plist'
        plist.parent.mkdir(parents=True)
        plist.write_text(f'<plist><string>{self.pyz}</string></plist>\n', encoding='utf-8')
        r = self.run_script('--uninstall')
        self.assertEqual(r.returncode, 1)
        self.assertIn('schedule remove', r.stderr)
        self.assertTrue(self.pyz.is_file())
        self.assertTrue(self.wrapper.is_file())
        plist.write_text('<plist><string>/somewhere/else/disk-steward.pyz</string></plist>\n', encoding='utf-8')
        self.assertEqual(self.run_script('--uninstall').returncode, 0)
        self.assertFalse(self.pyz.exists())

    def test_the_weekly_scan_label_is_the_packages(self):
        self.assertIn(f'{schedule.LABEL}.plist', INSTALL.read_text(encoding='utf-8'))

    def test_it_never_asks_for_root(self):
        code = [line for line in INSTALL.read_text(encoding='utf-8').splitlines() if not line.lstrip().startswith('#')]
        self.assertNotIn('sudo', '\n'.join(code))

    def test_the_script_is_executable_and_parses(self):
        self.assertTrue(INSTALL.stat().st_mode & stat.S_IXUSR)
        self.assertEqual(subprocess.run([self.shell, '-n', str(INSTALL)]).returncode, 0)


@unittest.skipUnless(os.path.exists('/bin/dash'), 'needs /bin/dash')
class InstallScriptUnderDashTests(InstallScriptTests):
    """The same checks under a strict POSIX shell, so a bashism in install.sh shows up."""
    shell = '/bin/dash'


if __name__ == '__main__':
    unittest.main()
