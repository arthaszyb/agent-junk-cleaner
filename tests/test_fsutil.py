from __future__ import annotations

import dataclasses
import errno
import os
import stat
import sys
import unittest
from unittest import mock

from disk_steward import fsutil
from disk_steward.model import ErrorCode, StewardError

from support import FakeHome


def walk(fh, logical, **kw):
    root = fh.path(logical)
    st = os.lstat(root)
    return fsutil.walk_bundle(str(root), st.st_dev, os.getuid(), **kw)


def patch_stat(**by_name):
    """Make _stat_entry report altered values for entries with the given names (uid=, dev=, flags= offsets)."""
    real = fsutil._stat_entry

    def fake(entry):
        info = real(entry)
        change = by_name.get(entry.name)
        return dataclasses.replace(info, **change(info)) if change else info
    return mock.patch.object(fsutil, '_stat_entry', fake)


class WalkBundleTests(unittest.TestCase):
    def setUp(self):
        self.fh = FakeHome().__enter__()
        self.addCleanup(self.fh.cleanup)

    def test_sizes_counts_and_reclaim(self):
        fh = self.fh
        fh.file('~/b/a.bin', size=5000)
        fh.file('~/b/sub/c.bin', size=9000)
        fh.file('~/b/sub/deep/d.bin', size=1)
        summary = walk(fh, '~/b')
        self.assertEqual((summary.files, summary.size), (3, 14001))
        allocs = [os.lstat(fh.path(p)).st_blocks * 512 for p in ('~/b/a.bin', '~/b/sub/c.bin', '~/b/sub/deep/d.bin')]
        self.assertEqual((summary.alloc, summary.reclaim), (sum(allocs), sum(allocs)))
        self.assertEqual(summary.taint, '')

    def test_hardlinks_count_in_alloc_but_not_in_reclaim(self):
        fh = self.fh
        fh.file('~/b/one', size=8192)
        fh.file('~/b/solo', size=4096)
        os.link(fh.path('~/b/one'), fh.path('~/b/two'))
        summary = walk(fh, '~/b')
        one = os.lstat(fh.path('~/b/one')).st_blocks * 512
        solo = os.lstat(fh.path('~/b/solo')).st_blocks * 512
        self.assertEqual(summary.files, 3)
        self.assertEqual(summary.alloc, 2 * one + solo)
        self.assertEqual(summary.reclaim, solo)

    def test_symlinks_are_files_of_zero_bytes_and_never_followed(self):
        fh = self.fh
        fh.file('~/outside/big.bin', size=40000)
        fh.file('~/b/real', size=100)
        fh.symlink('~/b/link-to-dir', '~/outside')
        fh.symlink('~/b/link-to-file', '~/outside/big.bin')
        summary = walk(fh, '~/b')
        self.assertEqual((summary.files, summary.size), (3, 100))
        self.assertEqual(summary.taint, '')

    def test_newest_covers_directories_as_well_as_files(self):
        fh = self.fh
        fh.file('~/b/old', size=10, age_days=30)
        fh.dir('~/b/sub', age_days=2)   # newer than every file
        fh.set_age('~/b', 20)
        expected = fsutil.stat_nofollow(fh.path('~/b/sub')).changed_ns
        self.assertEqual(walk(fh, '~/b').newest_ns, expected)
        fh.file('~/b/newer', size=10, age_days=1)
        fh.set_age('~/b', 20)
        self.assertEqual(walk(fh, '~/b').newest_ns, fsutil.stat_nofollow(fh.path('~/b/newer')).changed_ns)

    def test_root_directory_counts_for_newest(self):
        fh = self.fh
        fh.file('~/b/old', size=10, age_days=30)
        fh.set_age('~/b', 1)
        self.assertEqual(walk(fh, '~/b').newest_ns, fsutil.stat_nofollow(fh.path('~/b')).changed_ns)

    def test_taint_foreign_owner(self):
        self.fh.file('~/b/theirs', size=10)
        with patch_stat(theirs=lambda i: dict(uid=i.uid + 1)):
            self.assertEqual(walk(self.fh, '~/b').taint, 'foreign_owner')

    def test_taint_mount_and_the_mount_is_not_walked(self):
        self.fh.file('~/b/vol/inside', size=10)
        with patch_stat(vol=lambda i: dict(dev=i.dev + 1)):
            summary = walk(self.fh, '~/b')
        self.assertEqual(summary.taint, 'mount')
        self.assertEqual(summary.files, 0)   # the directory itself is not a file and was not entered

    def test_taint_immutable_and_dataless(self):
        for flag, reason in ((fsutil.UF_IMMUTABLE, 'immutable'), (fsutil.SF_IMMUTABLE, 'immutable'),
                             (fsutil.UF_APPEND, 'immutable'), (fsutil.SF_APPEND, 'immutable'),
                             (fsutil.SF_DATALESS, 'dataless')):
            with self.subTest(flag=hex(flag)):
                fh = FakeHome().__enter__()
                try:
                    fh.file('~/b/locked', size=10)
                    with patch_stat(locked=lambda i, flag=flag: dict(flags=i.flags | flag)):
                        self.assertEqual(walk(fh, '~/b').taint, reason)
                finally:
                    fh.cleanup()

    def test_a_dataless_directory_inside_is_tainted_and_not_entered(self):
        self.fh.file('~/b/cloud/inside', size=10)
        self.fh.file('~/b/ok', size=10)
        entered = []
        real = os.scandir

        def spy(path='.'):
            entered.append(os.path.basename(os.fspath(path)))
            return real(path)
        with patch_stat(cloud=lambda i: dict(flags=i.flags | fsutil.SF_DATALESS)), mock.patch('os.scandir', spy):
            summary = walk(self.fh, '~/b')
        self.assertEqual(summary.taint, 'dataless')
        self.assertNotIn('cloud', entered)
        self.assertEqual(summary.files, 1)

    def test_a_dataless_root_is_not_listed_at_all(self):
        self.fh.file('~/b/inside', size=10)
        real = fsutil.stat_nofollow

        def fake(path):
            info = real(path)
            return dataclasses.replace(info, flags=info.flags | fsutil.SF_DATALESS) if str(path).endswith('/b') else info
        entered = []
        real_scandir = os.scandir
        with mock.patch.object(fsutil, 'stat_nofollow', fake), \
                mock.patch('os.scandir', lambda p='.': (entered.append(os.fspath(p)), real_scandir(p))[1]):
            summary = walk(self.fh, '~/b')
        self.assertEqual((summary.taint, summary.files, summary.size), ('dataless', 0, 0))
        self.assertEqual(entered, [])

    def test_flag_constants(self):
        self.assertEqual(fsutil.SF_DATALESS, 0x40000000)
        self.assertEqual(fsutil.UF_IMMUTABLE, stat.UF_IMMUTABLE)

    def test_taint_git_directory_and_worktree_file(self):
        self.fh.dir('~/b/proj/.git')
        self.assertEqual(walk(self.fh, '~/b').taint, 'git')
        fh = FakeHome().__enter__()
        try:
            fh.file('~/b/wt/.git', content='gitdir: /elsewhere')
            self.assertEqual(walk(fh, '~/b').taint, 'git')
        finally:
            fh.cleanup()

    def test_taint_database_and_credential_names(self):
        for name, reason in (('x.sqlite', 'database'), ('x.sqlite-wal', 'database'), ('Cache.db', 'database'),
                             ('MANIFEST-000001', 'database'), ('LOCK', 'database'), ('AUTH.JSON', 'credential'),
                             ('server.pem', 'credential'), ('id_rsa.pub', 'credential'), ('my-token.json', 'credential')):
            with self.subTest(name=name):
                fh = FakeHome().__enter__()
                try:
                    fh.file(f'~/b/sub/{name}', size=10)
                    self.assertEqual(walk(fh, '~/b').taint, reason)
                finally:
                    fh.cleanup()

    def test_name_taint_off_ignores_names_but_keeps_structural_taint(self):
        fh = self.fh
        fh.file('~/b/auth.json', size=10)
        fh.file('~/b/x.sqlite', size=10)
        summary = walk(fh, '~/b', name_taint=False)
        self.assertEqual((summary.taint, summary.files), ('', 2))
        fh.dir('~/b/p/.git')
        self.assertEqual(walk(fh, '~/b', name_taint=False).taint, 'git')
        with patch_stat(**{'x.sqlite': lambda i: dict(uid=i.uid + 1)}):
            self.assertEqual(walk(fh, '~/b', name_taint=False).taint, 'foreign_owner')

    def test_unreadable_directory_is_a_taint_not_an_error(self):
        fh = self.fh
        fh.file('~/b/ok', size=10)
        fh.file('~/b/locked/inside', size=10)
        os.chmod(fh.path('~/b/locked'), 0)
        self.addCleanup(os.chmod, fh.path('~/b/locked'), 0o755)
        summary = walk(fh, '~/b')
        self.assertEqual(summary.taint, 'unreadable')
        self.assertEqual(summary.files, 1)

    def test_entry_vanishing_during_the_walk_is_unreadable(self):
        self.fh.file('~/b/gone', size=10)
        real = fsutil._stat_entry

        def fake(entry):
            raise FileNotFoundError(errno.ENOENT, 'gone', entry.path)
        with mock.patch.object(fsutil, '_stat_entry', fake):
            self.assertEqual(walk(self.fh, '~/b').taint, 'unreadable')
        self.assertIs(fsutil._stat_entry, real)

    def test_missing_root_is_unreadable(self):
        self.assertEqual(fsutil.walk_bundle(str(self.fh.path('~/nope')), 0, os.getuid()).taint, 'unreadable')

    def test_taint_order_is_independent_of_scan_order(self):
        self.fh.file('~/b/auth.json', size=10)
        self.fh.file('~/b/zz/x.sqlite', size=10)
        self.fh.dir('~/b/aa/.git')
        self.assertEqual(walk(self.fh, '~/b').taint, 'git')   # git outranks database and credential
        with patch_stat(**{'auth.json': lambda i: dict(uid=i.uid + 1)}):
            self.assertEqual(walk(self.fh, '~/b').taint, 'foreign_owner')

    def test_cancel_raises_the_scan_cancelled_error(self):
        self.fh.file('~/b/a', size=10)
        with self.assertRaises(StewardError) as cm:
            walk(self.fh, '~/b', cancel=lambda: True)
        self.assertEqual(cm.exception.code, ErrorCode.IN_PROGRESS)
        self.assertEqual(cm.exception.message_key, 'error.scan_cancelled')

    def test_stat_nofollow_reports_the_link_itself(self):
        self.fh.file('~/t/real', size=10)
        self.fh.symlink('~/t/link', '~/t/real')
        self.assertTrue(stat.S_ISLNK(fsutil.stat_nofollow(self.fh.path('~/t/link')).mode))
        self.assertTrue(stat.S_ISREG(fsutil.stat_nofollow(self.fh.path('~/t/real')).mode))


class OpenParentTests(unittest.TestCase):
    def setUp(self):
        self.fh = FakeHome().__enter__()
        self.addCleanup(self.fh.cleanup)

    def test_yields_parent_fd_and_name(self):
        self.fh.file('~/a/b/file', size=3)
        with fsutil.open_parent_nofollow(self.fh.path('~/a/b/file')) as (fd, name):
            self.assertEqual(name, 'file')
            self.assertEqual(os.stat(name, dir_fd=fd).st_size, 3)
            self.assertEqual(os.fstat(fd).st_ino, os.stat(self.fh.path('~/a/b')).st_ino)

    def test_fd_is_closed_afterwards(self):
        self.fh.file('~/a/file', size=3)
        with fsutil.open_parent_nofollow(self.fh.path('~/a/file')) as (fd, _):
            pass
        with self.assertRaises(OSError):
            os.fstat(fd)

    def test_refuses_a_symlinked_ancestor(self):
        self.fh.file('~/real/dir/file', size=3)
        self.fh.symlink('~/link', '~/real')
        with self.assertRaises(OSError) as cm:
            with fsutil.open_parent_nofollow(self.fh.path('~/link/dir/file')):
                pass
        self.assertIn(cm.exception.errno, (errno.ELOOP, errno.ENOTDIR))

    def test_final_component_may_itself_be_a_symlink(self):
        self.fh.file('~/a/real', size=3)
        self.fh.symlink('~/a/link', '~/a/real')
        with fsutil.open_parent_nofollow(self.fh.path('~/a/link')) as (fd, name):
            self.assertTrue(stat.S_ISLNK(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode))

    def test_missing_ancestor_and_bad_paths(self):
        with self.assertRaises(FileNotFoundError):
            with fsutil.open_parent_nofollow(self.fh.path('~/nope/file')):
                pass
        for bad in ('relative/path', '/', '/a/../b'):
            with self.subTest(path=bad), self.assertRaises(ValueError):
                with fsutil.open_parent_nofollow(bad):
                    pass


@unittest.skipUnless(sys.platform == 'darwin', 'renameatx_np is macOS only')
class RenameExclTests(unittest.TestCase):
    def setUp(self):
        self.fh = FakeHome().__enter__()
        self.addCleanup(self.fh.cleanup)

    def move(self, src, dst):
        with fsutil.open_parent_nofollow(self.fh.path(src)) as (sfd, sname), \
                fsutil.open_parent_nofollow(self.fh.path(dst)) as (dfd, dname):
            fsutil.rename_excl(sfd, sname, dfd, dname)

    def test_moves_a_file(self):
        self.fh.file('~/src/f', content='data')
        self.fh.dir('~/dst')
        self.move('~/src/f', '~/dst/g')
        self.assertFalse(self.fh.path('~/src/f').exists())
        self.assertEqual(self.fh.path('~/dst/g').read_text(), 'data')

    def test_moves_a_directory(self):
        self.fh.file('~/src/d/inner', content='x')
        self.fh.dir('~/dst')
        self.move('~/src/d', '~/dst/d2')
        self.assertEqual(self.fh.path('~/dst/d2/inner').read_text(), 'x')
        self.assertFalse(self.fh.path('~/src/d').exists())

    def test_refuses_to_overwrite_a_file(self):
        self.fh.file('~/src/f', content='new')
        self.fh.file('~/dst/f', content='old')
        with self.assertRaises(OSError) as cm:
            self.move('~/src/f', '~/dst/f')
        self.assertEqual(cm.exception.errno, errno.EEXIST)
        self.assertEqual(self.fh.path('~/src/f').read_text(), 'new')
        self.assertEqual(self.fh.path('~/dst/f').read_text(), 'old')

    def test_refuses_to_overwrite_an_empty_directory(self):
        self.fh.file('~/src/d/inner', content='x')
        self.fh.dir('~/dst/d')
        with self.assertRaises(OSError) as cm:
            self.move('~/src/d', '~/dst/d')
        self.assertEqual(cm.exception.errno, errno.EEXIST)
        self.assertTrue(self.fh.path('~/src/d/inner').exists())

    def test_missing_source_reports_enoent(self):
        self.fh.dir('~/src')
        self.fh.dir('~/dst')
        with self.assertRaises(OSError) as cm:
            self.move('~/src/none', '~/dst/x')
        self.assertEqual(cm.exception.errno, errno.ENOENT)


class RenameExclPortabilityTests(unittest.TestCase):
    def test_other_platforms_raise_not_implemented(self):
        with mock.patch.object(fsutil.sys, 'platform', 'linux'):
            with self.assertRaises(NotImplementedError):
                fsutil.rename_excl(0, 'a', 0, 'b')


if __name__ == '__main__':
    unittest.main()
