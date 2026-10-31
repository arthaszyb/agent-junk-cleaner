"""What the built artifacts hold. Needs dist/ from scripts/build_release.sh and is skipped without it, or when the
artifacts are older than the sources."""
from __future__ import annotations

import fnmatch
import tarfile
import unittest
import zipfile
from pathlib import Path

from disk_steward import __version__

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / 'dist'
PACKAGE = ROOT / 'src' / 'disk_steward'

FORBIDDEN_FILES = ('*.db', '*.db-wal', '*.db-shm', '*.pyc', '.DS_Store')
FORBIDDEN_DIRS = {'__pycache__', 'data', 'report', 'reports', 'screenshot', 'screenshots', 'dist', 'build', '.git',
                  '.gitlab'}
SDIST_TOP = {'src', 'tests', 'docs', 'scripts', 'README.md', 'LICENSE', 'CHANGELOG.md', 'SECURITY.md',
             'CONTRIBUTING.md', 'pyproject.toml', 'MANIFEST.in', 'Open Disk Steward.command', 'PKG-INFO', 'setup.cfg'}


def package_paths():
    return [p for p in PACKAGE.rglob('*')
            if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc' and p.name != '.DS_Store']


def package_files():
    """Every file of the package that a user needs, as a path relative to src/."""
    return sorted(p.relative_to(ROOT / 'src').as_posix() for p in package_paths())


def newest_source():
    return max(p.stat().st_mtime for p in [ROOT / 'pyproject.toml', ROOT / 'MANIFEST.in'] + package_paths())


def artifact(pattern):
    found = sorted(DIST.glob(pattern)) if DIST.is_dir() else []
    if not found:
        raise unittest.SkipTest(f'no dist/{pattern}: run scripts/build_release.sh')
    if found[-1].stat().st_mtime < newest_source():
        raise unittest.SkipTest(f'dist/{found[-1].name} is older than the sources: run scripts/build_release.sh')
    return found[-1]


def check_names(testcase, names):
    for name in names:
        parts = name.rstrip('/').split('/')
        with testcase.subTest(member=name):
            testcase.assertFalse(any(fnmatch.fnmatchcase(parts[-1], pat) for pat in FORBIDDEN_FILES), name)
            testcase.assertFalse(set(parts[:-1]) & FORBIDDEN_DIRS or (name.endswith('/') and parts[-1] in FORBIDDEN_DIRS),
                                 name)


class SdistTests(unittest.TestCase):
    def setUp(self):
        self.path = artifact('*.tar.gz')
        with tarfile.open(self.path) as tar:
            self.names = tar.getnames()
        self.root = self.names[0].split('/')[0]

    def test_it_holds_only_the_allowed_top_level_entries(self):
        top = {n.split('/')[1] for n in self.names if '/' in n}
        self.assertEqual(top - SDIST_TOP, set())

    def test_it_holds_no_data_caches_or_screenshots(self):
        check_names(self, self.names)

    def test_it_has_the_files_a_builder_needs(self):
        have = set(self.names)
        for needed in [f'src/{rel}' for rel in package_files()] + [
                'README.md', 'LICENSE', 'CHANGELOG.md', 'SECURITY.md', 'CONTRIBUTING.md', 'pyproject.toml',
                'docs/catalog.md', 'scripts/build_release.sh', 'scripts/install.sh', 'tests/test_packaging.py',
                'tests/test_install.py', 'Open Disk Steward.command']:
            with self.subTest(needed=needed):
                self.assertIn(f'{self.root}/{needed}', have)


class WheelTests(unittest.TestCase):
    def setUp(self):
        self.path = artifact('*.whl')
        with zipfile.ZipFile(self.path) as z:
            self.names = z.namelist()
            self.entry_points = next(z.read(n).decode() for n in self.names if n.endswith('entry_points.txt'))
            self.metadata = next(z.read(n).decode() for n in self.names if n.endswith('.dist-info/METADATA'))
            self.license_text = next(z.read(n).decode() for n in self.names if n.endswith('.dist-info/licenses/LICENSE'))

    def test_it_holds_no_data_caches_or_screenshots(self):
        check_names(self, self.names)

    def test_every_package_file_is_inside(self):
        have = set(self.names)
        for rel in package_files():
            with self.subTest(rel=rel):
                self.assertIn(rel, have)

    def test_nothing_but_the_package_is_inside(self):
        outside = [n for n in self.names if not n.startswith('disk_steward/') and '.dist-info/' not in n]
        self.assertEqual(outside, [])

    def test_it_declares_the_mit_license(self):
        self.assertIn('License-Expression: MIT', self.metadata)
        self.assertNotIn('Classifier: License ::', self.metadata)
        self.assertIn('MIT License', self.license_text)

    def test_the_command_is_installed(self):
        self.assertIn('disk-steward = disk_steward.cli:main', self.entry_points)
        self.assertIn(f'-{__version__}-', self.path.name)


class ZipappTests(unittest.TestCase):
    def setUp(self):
        self.path = artifact('*.pyz')
        with zipfile.ZipFile(self.path) as z:
            self.names = z.namelist()

    def test_it_holds_no_data_caches_or_screenshots(self):
        check_names(self, self.names)

    def test_every_package_file_is_inside(self):
        have = set(self.names)
        for rel in package_files():
            with self.subTest(rel=rel):
                self.assertIn(rel, have)

    def test_it_starts_with_the_interpreter_line_and_has_a_main(self):
        self.assertEqual(self.path.read_bytes().split(b'\n', 1)[0], b'#!/usr/bin/env python3')
        self.assertIn('__main__.py', self.names)


if __name__ == '__main__':
    unittest.main()
