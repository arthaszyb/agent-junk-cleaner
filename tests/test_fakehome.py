from __future__ import annotations

import os
import stat
import tempfile
import time
import unittest
from pathlib import Path

from disk_steward.model import StatInfo
from support import Clock, FakeHome, FakeMacOS, real_home_guard

DAY_NS = 86400 * 10 ** 9
HAS_BIRTHTIME = hasattr(os.stat_result, 'st_birthtime')


class ApfsBirthtimeTests(unittest.TestCase):
    """The assumption FakeHome's ages rest on; see the note at the top of support.py."""

    @unittest.skipUnless(HAS_BIRTHTIME, 'needs st_birthtime')
    def test_utime_into_the_past_moves_birthtime_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, 'f')
            open(p, 'w').close()
            created = os.stat(p).st_birthtime
            old = time.time() - 30 * 86400
            os.utime(p, (old, old))
            st = os.stat(p)
            self.assertAlmostEqual(st.st_birthtime, old, delta=1)
            self.assertLess(st.st_birthtime, created - 29 * 86400)
            self.assertAlmostEqual(StatInfo.from_stat(st).changed_ns / 1e9, old, delta=1)

    @unittest.skipUnless(HAS_BIRTHTIME, 'needs st_birthtime')
    def test_utime_into_the_future_leaves_birthtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, 'f')
            open(p, 'w').close()
            created = os.stat(p).st_birthtime
            future = time.time() + 86400
            os.utime(p, (future, future))
            self.assertEqual(os.stat(p).st_birthtime, created)
            self.assertAlmostEqual(StatInfo.from_stat(os.stat(p)).changed_ns / 1e9, future, delta=1)

    @unittest.skipUnless(HAS_BIRTHTIME, 'needs st_birthtime')
    def test_directories_and_symlinks_behave_the_same(self):
        with tempfile.TemporaryDirectory() as tmp:
            d, link = os.path.join(tmp, 'd'), os.path.join(tmp, 'l')
            os.mkdir(d)
            os.symlink(d, link)
            old = time.time() - 10 * 86400
            os.utime(d, (old, old))
            os.utime(link, (old, old), follow_symlinks=False)
            self.assertAlmostEqual(os.stat(d).st_birthtime, old, delta=1)
            self.assertAlmostEqual(os.lstat(link).st_birthtime, old, delta=1)


class FakeHomeTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(2_000_000_000)
        self.fh = FakeHome(self.clock).__enter__()
        self.addCleanup(self.fh.cleanup)

    def changed_ns(self, logical):
        return StatInfo.from_stat(os.lstat(self.fh.path(logical))).changed_ns

    def test_layout(self):
        fh = self.fh
        self.assertEqual(fh.bases.home, fh.home)
        for d in (fh.home, fh.tmpdir, fh.cachedir, fh.data_dir):
            self.assertTrue(d.is_dir())
            self.assertEqual(d, Path(os.path.realpath(d)))
        self.assertEqual(len({fh.home, fh.tmpdir, fh.cachedir, fh.data_dir}), 4)
        self.assertFalse(str(fh.data_dir).startswith(str(fh.home)))

    def test_file_sizes_and_content(self):
        dense = self.fh.file('~/a/dense', size=5000)
        sparse = self.fh.file('~/a/sparse', size=50 * 1024 * 1024)
        text = self.fh.file('~/a/text', content='héllo')
        raw = self.fh.file('$TMPDIR/raw', content=b'\x00\x01', size=99)
        self.assertEqual(os.stat(dense).st_size, 5000)
        self.assertGreaterEqual(os.stat(dense).st_blocks * 512, 5000)
        self.assertEqual(os.stat(sparse).st_size, 50 * 1024 * 1024)
        self.assertLess(os.stat(sparse).st_blocks * 512, 1024 * 1024)
        self.assertEqual(text.read_text(encoding='utf-8'), 'héllo')
        self.assertEqual(raw.read_bytes(), b'\x00\x01')
        self.assertEqual(self.fh.file('$CACHEDIR/empty').stat().st_size, 0)

    def test_ages_follow_the_clock_exactly(self):
        self.fh.file('~/f', age_days=10)
        self.assertEqual(os.lstat(self.fh.path('~/f')).st_mtime_ns, 2_000_000_000 * 10 ** 9 - 10 * DAY_NS)
        self.fh.file('~/fresh')
        self.assertEqual(os.lstat(self.fh.path('~/fresh')).st_mtime_ns, 2_000_000_000 * 10 ** 9)
        self.fh.file('~/half', age_days=0.5)
        self.assertEqual(os.lstat(self.fh.path('~/half')).st_mtime_ns, 2_000_000_000 * 10 ** 9 - DAY_NS // 2)

    @unittest.skipUnless(HAS_BIRTHTIME, 'needs st_birthtime')
    def test_changed_ns_reflects_the_requested_age_in_both_directions(self):
        now_ns = 2_000_000_000 * 10 ** 9
        self.fh.file('~/f', age_days=30)
        self.assertAlmostEqual(self.changed_ns('~/f'), now_ns - 30 * DAY_NS, delta=10 ** 6)
        self.fh.set_age('~/f', 1)
        self.assertAlmostEqual(self.changed_ns('~/f'), now_ns - 1 * DAY_NS, delta=10 ** 6)
        self.fh.set_age('~/f', 90)
        self.assertAlmostEqual(self.changed_ns('~/f'), now_ns - 90 * DAY_NS, delta=10 ** 6)

    def test_dir_and_set_age(self):
        self.fh.file('~/d/inner', age_days=2)
        self.fh.dir('~/d', age_days=5)
        self.assertEqual(os.stat(self.fh.path('~/d')).st_mtime_ns, 2_000_000_000 * 10 ** 9 - 5 * DAY_NS)
        self.fh.dir('~/e/f/g')
        self.assertTrue(self.fh.path('~/e/f/g').is_dir())
        self.fh.set_age('~/d/inner', 20)
        self.assertEqual(os.stat(self.fh.path('~/d/inner')).st_mtime_ns, 2_000_000_000 * 10 ** 9 - 20 * DAY_NS)

    def test_symlinks(self):
        self.fh.file('$TMPDIR/real')
        by_logical = self.fh.symlink('~/l1', '$TMPDIR/real', age_days=3)
        literal = self.fh.symlink('~/sub/l2', '../relative/target')
        dangling = self.fh.symlink('~/l3', '/nonexistent/abs')
        self.assertEqual(os.readlink(by_logical), str(self.fh.tmpdir / 'real'))
        self.assertEqual(os.readlink(literal), '../relative/target')
        self.assertEqual(os.readlink(dangling), '/nonexistent/abs')
        self.assertTrue(stat.S_ISLNK(os.lstat(by_logical).st_mode))
        self.assertEqual(os.lstat(by_logical).st_mtime_ns, 2_000_000_000 * 10 ** 9 - 3 * DAY_NS)
        self.assertEqual(os.stat(self.fh.path('$TMPDIR/real')).st_mtime_ns, 2_000_000_000 * 10 ** 9)  # target untouched

    def test_cleanup_removes_only_the_temp_root(self):
        root = self.fh.root
        self.fh.file('~/a/b/c', size=10)
        marker = root.parent / 'ds-test-neighbour-marker'
        marker.write_text('x')
        self.addCleanup(marker.unlink)
        self.fh.cleanup()
        self.assertFalse(root.exists())
        self.assertTrue(marker.exists())
        self.fh.cleanup()                                     # idempotent

    def test_context_manager(self):
        with FakeHome() as fh:
            root = fh.root
            self.assertTrue(root.is_dir())
        self.assertFalse(root.exists())

    def test_default_clock_is_real_time(self):
        with FakeHome() as fh:
            self.assertAlmostEqual(fh.clock(), time.time(), delta=5)

    def test_fakemacos_is_re_exported(self):
        self.assertEqual(FakeMacOS(running=['x']).running_bundle_ids(), {'x'})

    def test_real_home_stays_untouched(self):
        with real_home_guard():
            self.fh.file('~/.codex/sessions/x', size=100, age_days=40)
            self.fh.file('~/.claude/projects/p/x.jsonl', content='x')
            self.fh.file('~/Library/Caches/com.x/y', size=10)


class ClockTests(unittest.TestCase):
    def test_set_advance_call(self):
        c = Clock(100.0)
        self.assertEqual(c(), 100.0)
        c.advance(seconds=5)
        c.advance(days=1)
        self.assertEqual(c(), 100.0 + 5 + 86400)
        c.set(7)
        self.assertEqual(c(), 7.0)


class RealHomeGuardTests(unittest.TestCase):
    def test_detects_change_and_ignores_quiet_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            watched = Path(tmp) / 'watched'
            watched.mkdir()
            missing = Path(tmp) / 'missing'
            with real_home_guard([watched, missing]):
                pass
            with self.assertRaisesRegex(AssertionError, 'watched'):
                with real_home_guard([watched, missing]):
                    (watched / 'new').write_text('x')
            with self.assertRaisesRegex(AssertionError, 'missing'):
                with real_home_guard([watched, missing]):
                    missing.mkdir()

    def test_default_paths_are_cheap_and_unchanged_by_a_noop(self):
        with real_home_guard():
            pass


if __name__ == '__main__':
    unittest.main()
