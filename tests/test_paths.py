from __future__ import annotations

import hashlib
import os
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from disk_steward import paths
from disk_steward.model import ErrorCode, StewardError
from disk_steward.paths import Bases, default_data_dir, item_key, match_pattern
from support import FakeHome, render_error


class BasesTests(unittest.TestCase):
    def setUp(self):
        self.fh = FakeHome().__enter__()
        self.addCleanup(self.fh.cleanup)
        self.b = self.fh.bases

    def assertInvalid(self, fn, *args):
        with self.assertRaises(StewardError) as cm:
            fn(*args)
        self.assertEqual(cm.exception.code, ErrorCode.INVALID)
        self.assertEqual(cm.exception.message_key, 'error.invalid_path')
        render_error(cm.exception)

    def test_round_trip(self):
        for logical in ('~', '~/a', '~/Library/Caches/com.x.y/z w', '$TMPDIR', '$TMPDIR/foo', '$CACHEDIR/a/b',
                        '~/back\\slash', '~/.hidden', '~/中文/é'):
            with self.subTest(logical=logical):
                self.assertEqual(self.b.to_logical(self.b.to_abs(logical)), logical)

    def test_to_abs_maps_prefixes(self):
        self.assertEqual(self.b.to_abs('~/a/b'), self.fh.home / 'a' / 'b')
        self.assertEqual(self.b.to_abs('$TMPDIR/a'), self.fh.tmpdir / 'a')
        self.assertEqual(self.b.to_abs('$CACHEDIR/a'), self.fh.cachedir / 'a')
        self.assertEqual(self.b.to_abs('~'), self.fh.home)

    def test_backslash_is_an_ordinary_character(self):
        self.assertEqual(self.b.to_abs('~/a\\b'), self.fh.home / 'a\\b')
        self.assertEqual(self.b.to_abs('~/..\\x'), self.fh.home / '..\\x')

    def test_to_abs_rejections(self):
        for bad in ('', '/etc/passwd', 'relative/x', '~foo', '~/', '~//a', '~/a//b', '~/a/', '~/./a', '~/a/.',
                    '~/a/..', '~/../x', '~/a\0b', '~\0', '$tmpdir/x', '$HOME/x', '~~/x', '$TMPDIRX/a',
                    '$CACHEDIR/../x', ' ~/a'):
            with self.subTest(bad=bad):
                self.assertInvalid(self.b.to_abs, bad)

    def test_to_abs_rejects_non_strings(self):
        self.assertInvalid(self.b.to_abs, None)

    def test_to_logical_picks_longest_base(self):
        nested = Bases(self.fh.home, self.fh.home / 'tmp', self.fh.home / 'tmp' / 'cache')
        self.assertEqual(nested.to_logical(self.fh.home / 'tmp' / 'cache' / 'x'), '$CACHEDIR/x')
        self.assertEqual(nested.to_logical(self.fh.home / 'tmp' / 'y'), '$TMPDIR/y')
        self.assertEqual(nested.to_logical(self.fh.home / 'other'), '~/other')
        self.assertEqual(nested.to_logical(self.fh.home / 'tmp'), '$TMPDIR')

    def test_to_logical_does_not_match_sibling_prefix(self):
        self.assertInvalid(self.b.to_logical, Path(str(self.fh.home) + 'x') / 'a')

    def test_to_logical_rejections(self):
        self.assertInvalid(self.b.to_logical, Path('/etc/passwd'))
        self.assertInvalid(self.b.to_logical, Path('relative/a'))
        self.assertInvalid(self.b.to_logical, self.fh.home / '..' / 'x')
        self.assertInvalid(self.b.to_logical, self.fh.root)

    def test_to_logical_does_not_resolve_symlinks(self):
        link = self.fh.symlink('~/link', '$TMPDIR/real')
        self.assertEqual(self.b.to_logical(link), '~/link')

    def test_cachedir_unavailable(self):
        b = Bases(self.fh.home, self.fh.tmpdir, None)
        self.assertEqual(b.to_abs('~/a'), self.fh.home / 'a')
        with self.assertRaises(StewardError) as cm:
            b.to_abs('$CACHEDIR/x')
        self.assertEqual(cm.exception.code, ErrorCode.UNAVAILABLE)
        self.assertEqual(cm.exception.message_key, 'error.cachedir_unavailable')
        render_error(cm.exception)
        self.assertInvalid(b.to_logical, self.fh.cachedir / 'x')

    def test_detect_resolves_home_and_tmpdir(self):
        link_home = self.fh.root / 'home-link'
        link_tmp = self.fh.root / 'tmp-link'
        os.symlink(self.fh.home, link_home)
        os.symlink(self.fh.tmpdir, link_tmp)
        with mock.patch.dict(os.environ, {'TMPDIR': str(link_tmp)}):
            b = Bases.detect(link_home)
        self.assertEqual(b.home, self.fh.home)
        self.assertEqual(b.tmpdir, self.fh.tmpdir)

    def test_detect_defaults(self):
        with mock.patch.dict(os.environ, {'TMPDIR': ''}):
            b = Bases.detect()
        self.assertEqual(b.home, Path(os.path.realpath(Path.home())))
        self.assertTrue(b.tmpdir.is_absolute())

    @unittest.skipUnless(os.sys.platform == 'darwin', 'getconf DARWIN_USER_CACHE_DIR is macOS only')
    def test_detect_cachedir_from_getconf(self):
        out = subprocess.run(['/usr/bin/getconf', 'DARWIN_USER_CACHE_DIR'], capture_output=True, text=True,
                             check=True).stdout.strip()
        self.assertEqual(Bases.detect(self.fh.home).cachedir, Path(os.path.realpath(out)))

    def test_detect_without_getconf_support(self):
        with mock.patch.object(paths.sys, 'platform', 'linux'):
            b = Bases.detect(self.fh.home)
        self.assertIsNone(b.cachedir)
        with self.assertRaises(StewardError) as cm:
            b.to_abs('$CACHEDIR/x')
        self.assertEqual(cm.exception.code, ErrorCode.UNAVAILABLE)


class MatchPatternTests(unittest.TestCase):
    CASES = [
        # literal and single-segment wildcards
        ('~/a/b', '~/a/b', True),
        ('~/a/b', '~/a/c', False),
        ('~/a/b', '~/a/b/c', False),
        ('~/a/*', '~/a/b', True),
        ('~/a/*', '~/a/b/c', False),
        ('~/a/*', '~/a', False),
        ('~/*', '~/.hidden', True),
        ('~/Lib*/Cach*', '~/Library/Caches', True),
        ('~/*.log', '~/a.log', True),
        ('~/*.log', '~/.log', True),
        ('~/*.log', '~/a.logx', False),
        ('~/a?/b', '~/ab/b', True),
        ('~/a?/b', '~/a/b', False),
        ('~/[ab]/x', '~/a/x', True),
        ('~/[ab]/x', '~/c/x', False),
        ('~/Library', '~/library', False),
        ('~/a**', '~/abc', True),
        ('~/a**', '~/a/b', False),
        # '**' as the last segment: zero or more
        ('~/a/**', '~/a', True),
        ('~/a/**', '~/a/b', True),
        ('~/a/**', '~/a/b/c/d', True),
        ('~/a/**', '~/ab', False),
        ('~/**', '~', True),
        ('**', '~/x', True),
        ('**', '', True),
        # '**' first and in the middle
        ('**/x', '~/x', True),
        ('**/x', 'x', True),
        ('**/x', '~/a/b/x', True),
        ('**/x', '~/a/b/x/y', False),
        ('~/**/cache', '~/cache', True),
        ('~/**/cache', '~/a/b/cache', True),
        ('~/**/cache', '~/a/b/cache/c', False),
        ('~/**/**/x', '~/x', True),
        ('~/**/a/**/b', '~/a/b', True),
        ('~/**/a/**/b', '~/x/a/y/z/b', True),
        ('~/**/a/**/b', '~/x/b/a', False),
        ('~/**/*.log', '~/a/b/c.log', True),
        ('~/**/*.log', '~/a/b/c.txt', False),
        ('~/**/a/**/a', '~/a', False),
        ('~/**/a/**/a', '~/a/a', True),
        # empty pattern
        ('', '', True),
        ('', '~', False),
    ]

    def test_table(self):
        for pattern, logical, expected in self.CASES:
            with self.subTest(pattern=pattern, logical=logical):
                self.assertEqual(match_pattern(pattern, logical), expected)

    def test_no_exponential_blowup(self):
        deep = '/'.join(['a'] * 3000)
        started = time.monotonic()
        self.assertFalse(match_pattern('**/**/**/**/**/**/b', deep))
        self.assertTrue(match_pattern('**/**/**/**/**/**/a', deep))
        self.assertLess(time.monotonic() - started, 5)


class KeyAndDataDirTests(unittest.TestCase):
    def test_item_key(self):
        self.assertEqual(item_key('~/a'), hashlib.sha1(b'~/a').hexdigest()[:20])
        self.assertEqual(len(item_key('~/a')), 20)
        self.assertNotEqual(item_key('~/a'), item_key('~/b'))

    def test_item_key_handles_undecodable_names(self):
        raw = os.fsdecode(b'~/bad-\xff-name')
        self.assertEqual(item_key(raw), hashlib.sha1(b'~/bad-\xff-name').hexdigest()[:20])

    def test_default_data_dir_env_override(self):
        with mock.patch.dict(os.environ, {'DISK_STEWARD_DATA_DIR': '/tmp/ds-data'}):
            self.assertEqual(default_data_dir(), Path('/tmp/ds-data'))

    def test_default_data_dir_default(self):
        env = {k: v for k, v in os.environ.items() if k != 'DISK_STEWARD_DATA_DIR'}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(default_data_dir(), Path.home() / 'Library' / 'Application Support' / 'Disk Steward')


if __name__ == '__main__':
    unittest.main()
