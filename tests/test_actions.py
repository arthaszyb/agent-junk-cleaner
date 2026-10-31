from __future__ import annotations

import dataclasses
import gzip
import hashlib
import io
import os
import stat
import tarfile
import unittest
from pathlib import Path
from unittest import mock

from disk_steward import actions
from disk_steward.model import Action, EntryState, ErrorCode, Unit, StewardError

from m4_support import ActionEnv, PLAN_ID, tree_snapshot
from support import Clock, FakeHome

FILE = '~/.claude/debug/x.txt'
BUNDLE = '~/scratch/b1'


class Case(unittest.TestCase):
    def setUp(self):
        self.fh = FakeHome(Clock()).__enter__()
        self.addCleanup(self.fh.cleanup)
        self.env = ActionEnv(self)

    def make_bundle(self, logical=BUNDLE):
        fh = self.fh
        fh.file(f'{logical}/a.bin', content=os.urandom(5000), age_days=3)
        fh.file(f'{logical}/sub/b.txt', content='hello\n' * 300, age_days=3)
        fh.file(f'{logical}/sub/deep/c.sh', content='#!/bin/sh\n', age_days=3)
        os.chmod(fh.path(f'{logical}/sub/deep/c.sh'), 0o755)
        fh.dir(f'{logical}/empty', age_days=3)
        os.symlink('../a.bin', fh.path(f'{logical}/sub/link'))
        os.symlink('missing-target', fh.path(f'{logical}/dangling'))
        fh.dir(f'{logical}/sub/deep', age_days=3)
        fh.dir(f'{logical}/sub', age_days=3)
        fh.dir(logical, age_days=3)
        return fh.path(logical)


class DeleteFileTests(Case):
    def test_deletes_through_staging(self):
        path = self.fh.file(FILE, size=3000, age_days=20)
        entry = self.env.entry(FILE)
        out = self.env.call(actions.delete_file, entry)
        self.assertEqual((out.state, out.reason, out.freed), (EntryState.DONE, '', entry.reclaim))
        self.assertFalse(os.path.lexists(path))
        self.assertEqual(self.env.staged(), [])

    def test_changed_file_goes_back(self):
        path = self.fh.file(FILE, size=3000, age_days=20)
        entry = self.env.entry(FILE)
        with open(path, 'ab') as f:
            f.write(b'x')
        out = self.env.call(actions.delete_file, entry)
        self.assertEqual((out.state, out.reason), (EntryState.SKIPPED, 'skip.changed'))
        self.assertEqual(os.path.getsize(path), 3001)
        self.assertEqual(self.env.staged(), [])

    def test_swapped_inode_goes_back(self):
        path = self.fh.file(FILE, content='original', age_days=20)
        entry = self.env.entry(FILE)
        other = path.with_name('other')
        other.write_text('replacement')
        os.replace(other, path)
        out = self.env.call(actions.delete_file, entry)
        self.assertEqual(out.reason, 'skip.changed')
        self.assertEqual(path.read_text(), 'replacement')

    def test_name_taken_while_staged_leaves_the_item_in_staging(self):
        path = self.fh.file(FILE, content='original', age_days=20)
        entry = self.env.entry(FILE)
        real = actions.stat_at

        def stat_at(dir_fd, name):
            info = real(dir_fd, name)
            if name == str(entry.seq):   # the check of the staged item: somebody re-creates the name meanwhile
                path.write_text('new owner of the name')
                return dataclasses.replace(info, size=info.size + 1)
            return info

        with mock.patch.object(actions, 'stat_at', stat_at):
            out = self.env.call(actions.delete_file, entry)
        self.assertEqual(out.state, EntryState.NEEDS_ATTENTION)
        staged = self.fh.data_dir / 'staging' / PLAN_ID / str(entry.seq)
        self.assertEqual(out.reason, f'attention.staged_left:{staged}')
        self.assertEqual(staged.read_text(), 'original')
        self.assertEqual(path.read_text(), 'new owner of the name')

    def test_unlink_failure_puts_the_file_back_and_raises(self):
        path = self.fh.file(FILE, content='keep me', age_days=20)
        entry = self.env.entry(FILE)
        with mock.patch.object(actions.os, 'unlink', side_effect=PermissionError('nope')):
            with self.assertRaises(PermissionError):
                self.env.call(actions.delete_file, entry)
        self.assertEqual(path.read_text(), 'keep me')
        self.assertEqual(self.env.staged(), [])

    def test_hardlinked_file_is_not_removed(self):
        path = self.fh.file(FILE, content='data', age_days=20)
        entry = self.env.entry(FILE)
        os.link(path, path.with_name('twin'))
        out = self.env.call(actions.delete_file, entry)
        self.assertEqual(out.reason, 'skip.changed')
        self.assertTrue(path.exists() and path.with_name('twin').exists())


class DeleteBundleTests(Case):
    def test_deletes_a_tree_including_read_only_folders(self):
        root = self.make_bundle()
        os.chmod(self.fh.path(f'{BUNDLE}/sub/deep'), 0o555)
        os.chmod(self.fh.path(f'{BUNDLE}/sub'), 0o555)
        entry = self.env.entry(BUNDLE, unit=Unit.BUNDLE)
        out = self.env.call(actions.delete_bundle, entry, name_taint=False)
        self.assertEqual((out.state, out.freed), (EntryState.DONE, entry.reclaim))
        self.assertFalse(os.path.lexists(root))
        self.assertEqual(self.env.staged(), [])

    def test_changed_bundle_goes_back_untouched(self):
        root = self.make_bundle()
        entry = self.env.entry(BUNDLE, unit=Unit.BUNDLE)
        before = tree_snapshot(root)
        self.fh.file(f'{BUNDLE}/sub/new.txt', content='added after the plan')
        out = self.env.call(actions.delete_bundle, entry, name_taint=False)
        self.assertEqual(out.reason, 'skip.changed')
        after = tree_snapshot(root)
        self.assertEqual(set(after) - set(before), {'sub/new.txt'})
        self.assertEqual(self.env.staged(), [])

    def test_a_git_folder_that_appears_taints_the_bundle(self):
        root = self.make_bundle()
        entry = self.env.entry(BUNDLE, unit=Unit.BUNDLE)
        os.mkdir(root / '.git')
        out = self.env.call(actions.delete_bundle, entry, name_taint=False)
        self.assertEqual(out.reason, 'skip.changed')
        self.assertTrue((root / '.git').is_dir())

    def test_a_database_name_taints_only_where_the_rule_checks_names(self):
        root = self.make_bundle()
        self.fh.file(f'{BUNDLE}/cache.sqlite', size=10)
        entry = self.env.entry(BUNDLE, unit=Unit.BUNDLE)   # fingerprint taken without name taint
        out = self.env.call(actions.delete_bundle, entry, name_taint=True)
        self.assertEqual(out.reason, 'skip.changed')
        self.assertTrue((root / 'cache.sqlite').exists())

    def test_partial_failure_is_flagged_and_not_put_back(self):
        root = self.make_bundle()
        entry = self.env.entry(BUNDLE, unit=Unit.BUNDLE)
        with mock.patch.object(actions, 'rmtree_ours', side_effect=OSError('disk on fire')):
            out = self.env.call(actions.delete_bundle, entry, name_taint=False)
        self.assertEqual(out.state, EntryState.NEEDS_ATTENTION)
        self.assertTrue(out.reason.startswith('attention.partial_delete:'))
        self.assertFalse(os.path.lexists(root))
        self.assertEqual(self.env.staged(), [str(entry.seq)])


class TrashTests(Case):
    def test_hands_the_path_to_the_system_trash(self):
        path = self.fh.file(FILE, size=100, age_days=20)
        entry = self.env.entry(FILE, Action.TRASH)
        out = actions.trash(self.env.ctx, entry, str(path))
        self.assertEqual((out.state, out.reason, out.freed), (EntryState.DONE, 'trash.moved', 0))
        self.assertEqual(self.env.mac.trashed, [str(path)])

    def test_a_changed_file_is_left_alone(self):
        path = self.fh.file(FILE, size=100, age_days=20)
        entry = self.env.entry(FILE, Action.TRASH)
        path.write_text('changed')
        out = actions.trash(self.env.ctx, entry, str(path))
        self.assertEqual(out.reason, 'skip.changed')
        self.assertEqual(self.env.mac.trashed, [])

    def test_a_changed_bundle_is_left_alone(self):
        self.make_bundle()
        entry = self.env.entry(BUNDLE, Action.TRASH, Unit.BUNDLE)
        self.fh.file(f'{BUNDLE}/late.txt', size=1)
        out = actions.trash(self.env.ctx, entry, str(self.fh.path(BUNDLE)), name_taint=False)
        self.assertEqual(out.reason, 'skip.changed')
        self.assertEqual(self.env.mac.trashed, [])


class ArchiveFileTests(Case):
    def archive(self, content=None, mode=0o640):
        path = self.fh.file(FILE, content=content if content is not None else os.urandom(200_000), age_days=20)
        os.chmod(path, mode)
        os.utime(path, ns=(1_700_000_000_123_456_789, 1_700_000_000_123_456_789))
        entry = self.env.entry(FILE, Action.ARCHIVE)
        return path, entry, self.env.call(actions.archive, entry)

    def test_round_trip_keeps_bytes_mode_and_mtime(self):
        data = os.urandom(300_000) + b'text ' * 5000
        path, entry, out = self.archive(content=data)
        self.assertEqual((out.state, out.reason), (EntryState.DONE, 'archive.stored'))
        self.assertFalse(os.path.lexists(path))
        row = self.env.store.archive(out.archive_id)
        self.assertEqual((row['state'], row['fmt'], row['path'], row['orig_size']), ('stored', 'gz', FILE, len(data)))
        self.assertEqual(out.freed, entry.reclaim - row['stored_bytes'])
        files = self.env.archive_files()
        self.assertEqual(files, [f'{out.archive_id}.gz', f'{out.archive_id}.json'])   # no .part left behind
        gz = self.fh.data_dir / 'archives' / f'{out.archive_id}.gz'
        self.assertEqual(hashlib.sha256(gz.read_bytes()).hexdigest(), row['sha_stored'])
        self.assertEqual(hashlib.sha256(gzip.decompress(gz.read_bytes())).hexdigest(), row['sha_orig'])
        self.assertEqual(os.stat(gz).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.fh.data_dir / 'archives').st_mode & 0o777, 0o700)
        sidecar = (self.fh.data_dir / 'archives' / f'{out.archive_id}.json').read_text()
        for key in ('plan_id', 'logical', 'sha_orig', 'sha_stored', 'rule_id', 'created_at', 'mtime_ns', 'mode'):
            if key != 'logical':
                self.assertIn(f'"{key}"', sidecar)
        self.assertIn(FILE, sidecar)

        result = actions.restore(self.env.ctx, out.archive_id)
        self.assertEqual((result['path'], result['bytes']), (FILE, len(data)))
        self.assertEqual(path.read_bytes(), data)
        info = os.lstat(path)
        self.assertEqual(info.st_mode & 0o777, 0o640)
        self.assertEqual(info.st_mtime_ns, 1_700_000_000_123_456_789)
        self.assertEqual(self.env.store.archive(out.archive_id)['state'], 'restored')
        self.assertEqual([n for n in os.listdir(path.parent) if n.startswith('.ds-restore')], [])

    def test_empty_file_round_trips(self):
        path, entry, out = self.archive(content=b'')
        self.assertEqual(out.state, EntryState.DONE)
        actions.restore(self.env.ctx, out.archive_id)
        self.assertEqual(path.read_bytes(), b'')

    def test_restore_loops_over_short_writes(self):
        data = os.urandom(100_000)
        path, entry, out = self.archive(content=data)
        real = os.write

        def short(fd, buf):
            return real(fd, bytes(buf)[:max(1, len(buf) // 3)])
        with mock.patch.object(actions.os, 'write', short):
            actions.restore(self.env.ctx, out.archive_id)
        self.assertEqual(path.read_bytes(), data)

    def test_a_restored_file_shorter_than_the_archive_says_is_refused(self):
        path, entry, out = self.archive(content=os.urandom(5000))
        real = actions.write_all
        with mock.patch.object(actions, 'write_all', lambda fd, data: real(fd, data[:-1])):
            with self.assertRaises(StewardError) as cm:
                actions.restore(self.env.ctx, out.archive_id)
        self.assertEqual(cm.exception.message_key, 'error.archive_corrupt')
        self.assertFalse(os.path.lexists(path))
        self.assertEqual([n for n in os.listdir(path.parent) if n.startswith('.ds-restore')], [])

    def test_archives_are_flushed_to_the_disk_with_a_full_sync(self):
        import fcntl
        seen = []
        real = fcntl.fcntl
        with mock.patch.object(actions.fcntl, 'fcntl', lambda fd, cmd, *a: seen.append(cmd) or real(fd, cmd, *a)):
            path, entry, out = self.archive(content=b'x' * 1000)
            actions.restore(self.env.ctx, out.archive_id)
        self.assertGreaterEqual(seen.count(fcntl.F_FULLFSYNC), 5)   # .part, sidecar, archive folder, restored file, folder

    def test_without_f_fullsync_the_plain_fsync_is_used(self):
        import types
        with mock.patch.object(actions, 'fcntl', types.SimpleNamespace()), mock.patch.object(actions.os, 'fsync') as fsync:
            actions.fullsync(7)
        fsync.assert_called_once_with(7)

    def test_a_failing_full_sync_is_an_error_not_a_fallback(self):
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, w)
        with self.assertRaises(OSError):
            actions.fullsync(w)    # a pipe cannot be flushed: the error must show, not be papered over with fsync

    def test_restore_never_overwrites(self):
        path, entry, out = self.archive(content=b'archived')
        path.write_text('somebody wrote here since')
        with self.assertRaises(StewardError) as cm:
            actions.restore(self.env.ctx, out.archive_id)
        self.assertEqual((cm.exception.code, cm.exception.message_key), (ErrorCode.INVALID, 'error.restore_exists'))
        self.assertEqual(path.read_text(), 'somebody wrote here since')
        self.assertEqual(self.env.store.archive(out.archive_id)['state'], 'stored')
        self.assertEqual([n for n in os.listdir(path.parent) if n.startswith('.ds-restore')], [])

    def test_restore_refuses_a_dangling_symlink_at_the_destination(self):
        path, entry, out = self.archive(content=b'archived')
        os.symlink('/nonexistent/target', path)
        with self.assertRaises(StewardError) as cm:
            actions.restore(self.env.ctx, out.archive_id)
        self.assertEqual(cm.exception.message_key, 'error.restore_exists')
        self.assertTrue(os.path.islink(path))

    def test_restore_recreates_missing_parents_privately(self):
        path, entry, out = self.archive(content=b'archived')
        os.rmdir(path.parent)
        actions.restore(self.env.ctx, out.archive_id)
        self.assertEqual(path.read_bytes(), b'archived')
        self.assertEqual(os.stat(path.parent).st_mode & 0o777, 0o700)

    def test_restore_twice_is_refused(self):
        path, entry, out = self.archive(content=b'archived')
        actions.restore(self.env.ctx, out.archive_id)
        with self.assertRaises(StewardError) as cm:
            actions.restore(self.env.ctx, out.archive_id)
        self.assertEqual(cm.exception.message_key, 'error.archive_state')

    def test_a_damaged_archive_is_never_restored(self):
        path, entry, out = self.archive(content=os.urandom(5000))
        gz = self.fh.data_dir / 'archives' / f'{out.archive_id}.gz'
        blob = bytearray(gz.read_bytes())
        blob[len(blob) // 2] ^= 0xFF
        gz.write_bytes(bytes(blob))
        with self.assertRaises(StewardError) as cm:
            actions.restore(self.env.ctx, out.archive_id)
        self.assertEqual(cm.exception.message_key, 'error.archive_corrupt')
        self.assertFalse(os.path.lexists(path))
        self.assertEqual([n for n in os.listdir(path.parent) if n.startswith('.ds-restore')], [])

    def test_purge_removes_the_files_once(self):
        path, entry, out = self.archive(content=b'archived')
        result = actions.purge_archive(self.env.ctx, out.archive_id)
        self.assertEqual(result['archive_id'], out.archive_id)
        self.assertEqual(self.env.archive_files(), [])
        self.assertEqual(self.env.store.archive(out.archive_id)['state'], 'purged')
        with self.assertRaises(StewardError) as cm:
            actions.purge_archive(self.env.ctx, out.archive_id)
        self.assertEqual(cm.exception.message_key, 'error.archive_state')
        with self.assertRaises(StewardError) as cm:
            actions.restore(self.env.ctx, out.archive_id)
        self.assertEqual(cm.exception.message_key, 'error.archive_state')

    def test_purge_after_restore(self):
        path, entry, out = self.archive(content=b'archived')
        actions.restore(self.env.ctx, out.archive_id)
        actions.purge_archive(self.env.ctx, out.archive_id)
        self.assertEqual(self.env.archive_files(), [])
        self.assertEqual(path.read_bytes(), b'archived')

    def test_unknown_archive(self):
        with self.assertRaises(StewardError) as cm:
            actions.restore(self.env.ctx, 'nope')
        self.assertEqual(cm.exception.code, ErrorCode.NOT_FOUND)

    def test_the_source_is_kept_when_the_archive_cannot_be_written(self):
        path = self.fh.file(FILE, content=os.urandom(4000), age_days=20)
        entry = self.env.entry(FILE, Action.ARCHIVE)
        self.env.mac.free = 100 * 1024 * 1024   # below the 1 GiB reserve
        with mock.patch.object(actions, 'CHUNK', 1024):
            out = self.env.call(actions.archive, entry)
        self.assertEqual((out.state, out.reason), (EntryState.FAILED, 'fail.disk_full'))
        self.assertTrue(path.exists())
        self.assertEqual(self.env.archive_files(), [])
        self.assertEqual(self.env.staged(), [])
        removed = [e for e in self.env.audit_events() if e['event'] == 'part.removed']
        self.assertEqual([e['reason'] for e in removed], ['DiskFull'])      # the removal is on record

    def test_a_file_that_changes_while_it_is_read_is_kept(self):
        path = self.fh.file(FILE, content=os.urandom(6000), age_days=20)
        entry = self.env.entry(FILE, Action.ARCHIVE)

        def append(e):
            with open(path, 'ab') as f:
                f.write(b'more')
        self.env.hooks['archive_chunk'] = append
        with mock.patch.object(actions, 'CHUNK', 1024):
            out = self.env.call(actions.archive, entry)
        self.assertEqual((out.state, out.reason), (EntryState.SKIPPED, 'skip.changed'))
        self.assertTrue(path.exists())
        self.assertEqual(self.env.archive_files(), [])

    def test_a_changed_file_is_never_archived(self):
        path = self.fh.file(FILE, content=b'one', age_days=20)
        entry = self.env.entry(FILE, Action.ARCHIVE)
        path.write_bytes(b'two!')
        out = self.env.call(actions.archive, entry)
        self.assertEqual(out.reason, 'skip.changed')
        self.assertEqual(self.env.archive_files(), [])

    def test_pause_rolls_the_archive_back(self):
        path = self.fh.file(FILE, content=os.urandom(6000), age_days=20)
        entry = self.env.entry(FILE, Action.ARCHIVE)
        calls = []

        def checkpoint():
            calls.append(1)
            if len(calls) == 3:
                raise actions.Paused()
        self.env.checkpoint = checkpoint
        with mock.patch.object(actions, 'CHUNK', 1024):
            out = self.env.call(actions.archive, entry)
        self.assertEqual((out.state, out.reason), (EntryState.PENDING, 'rolled_back'))
        self.assertTrue(path.exists())
        self.assertEqual(self.env.archive_files(), [])

    def test_failure_after_the_archive_discards_it(self):
        path = self.fh.file(FILE, content=b'keep me', age_days=20)
        entry = self.env.entry(FILE, Action.ARCHIVE)
        real = os.unlink

        def unlink(name, dir_fd=None):
            if dir_fd == self.env.staging_fd:   # only the removal of the staged file fails, not the cleanup
                raise PermissionError('no')
            real(name, dir_fd=dir_fd)
        with mock.patch.object(actions.os, 'unlink', unlink):
            with self.assertRaises(PermissionError):
                self.env.call(actions.archive, entry)
        self.assertEqual(path.read_bytes(), b'keep me')
        self.assertEqual(self.env.archive_files(), [])    # an archive of a file that was not removed is clutter
        (row,) = self.env.store.archives()
        self.assertEqual(row['state'], 'discarded')
        (event,) = [e for e in self.env.audit_events() if e['event'] == 'archive.discarded']
        self.assertEqual((event['archive_id'], event['reason']), (row['id'], 'the file could not be removed'))


class ArchiveBundleTests(Case):
    def archive(self):
        root = self.make_bundle()
        os.chmod(self.fh.path(f'{BUNDLE}/sub/deep'), 0o555)
        os.chmod(self.fh.path(f'{BUNDLE}/empty'), 0o700)
        entry = self.env.entry(BUNDLE, Action.ARCHIVE, Unit.BUNDLE)
        before = tree_snapshot(root)
        root_stat = os.lstat(root)
        out = self.env.call(actions.archive, entry, name_taint=False)
        return root, entry, before, root_stat, out

    def test_round_trip(self):
        root, entry, before, root_stat, out = self.archive()
        self.assertEqual((out.state, out.reason), (EntryState.DONE, 'archive.stored'))
        self.assertFalse(os.path.lexists(root))
        self.assertEqual(self.env.staged(), [])
        row = self.env.store.archive(out.archive_id)
        self.assertEqual((row['fmt'], row['state'], row['path']), ('tar.gz', 'stored', BUNDLE))
        self.assertEqual(self.env.archive_files(), [f'{out.archive_id}.json', f'{out.archive_id}.tar.gz'])
        with tarfile.open(self.fh.data_dir / 'archives' / f'{out.archive_id}.tar.gz') as tar:
            names = tar.getnames()
        self.assertTrue(all(not n.startswith(('/', '.')) and '..' not in n.split('/') for n in names), names)
        self.assertIn('sub/link', names)

        actions.restore(self.env.ctx, out.archive_id)
        self.assertEqual(tree_snapshot(root), before)
        self.assertEqual(os.readlink(root / 'sub' / 'link'), '../a.bin')       # stored as a link, never followed
        self.assertEqual(os.readlink(root / 'dangling'), 'missing-target')
        self.assertEqual(os.lstat(root).st_mode & 0o777, root_stat.st_mode & 0o777)
        self.assertEqual(os.lstat(root).st_mtime_ns, root_stat.st_mtime_ns)
        self.assertEqual(os.lstat(root / 'sub' / 'deep').st_mode & 0o777, 0o555)
        os.chmod(root / 'sub' / 'deep', 0o755)   # so the temp tree can be cleaned up
        self.assertEqual(self.env.store.archive(out.archive_id)['state'], 'restored')

    def test_restore_refuses_an_existing_destination_and_leaves_no_trace(self):
        root, entry, before, root_stat, out = self.archive()
        os.mkdir(root)
        with self.assertRaises(StewardError) as cm:
            actions.restore(self.env.ctx, out.archive_id)
        self.assertEqual(cm.exception.message_key, 'error.restore_exists')
        self.assertEqual(os.listdir(root), [])
        self.assertEqual([n for n in os.listdir(root.parent) if n.startswith('.ds-restore')], [])

    def test_empty_bundle_round_trips(self):
        self.fh.dir('~/scratch/empty-one', age_days=3)
        entry = self.env.entry('~/scratch/empty-one', Action.ARCHIVE, Unit.BUNDLE)
        out = self.env.call(actions.archive, entry, name_taint=False)
        self.assertEqual(out.state, EntryState.DONE)
        actions.restore(self.env.ctx, out.archive_id)
        self.assertEqual(os.listdir(self.fh.path('~/scratch/empty-one')), [])

    def test_the_bundle_is_staged_first_and_rolled_back_on_pause(self):
        root = self.make_bundle()
        entry = self.env.entry(BUNDLE, Action.ARCHIVE, Unit.BUNDLE)
        before = tree_snapshot(root)
        seen = []

        def checkpoint():
            seen.append(self.env.staged())     # while streaming, the bundle lives in staging
            raise actions.Cancelled()
        self.env.checkpoint = checkpoint
        with mock.patch.object(actions, 'CHUNK', 512):
            out = self.env.call(actions.archive, entry, name_taint=False)
        self.assertEqual((out.state, out.reason), (EntryState.PENDING, 'rolled_back'))
        self.assertEqual(seen[0], [str(entry.seq)])
        self.assertEqual(tree_snapshot(root), before)
        self.assertEqual(self.env.staged(), [])
        self.assertEqual(self.env.archive_files(), [])

    def test_disk_full_while_streaming_a_bundle_puts_it_back(self):
        root = self.make_bundle()
        entry = self.env.entry(BUNDLE, Action.ARCHIVE, Unit.BUNDLE)
        before = tree_snapshot(root)
        self.env.mac.free = 10 * 1024 * 1024
        with mock.patch.object(actions, 'CHUNK', 512):
            out = self.env.call(actions.archive, entry, name_taint=False)
        self.assertEqual((out.state, out.reason), (EntryState.FAILED, 'fail.disk_full'))
        self.assertEqual(tree_snapshot(root), before)
        self.assertEqual(self.env.staged(), [])
        self.assertEqual(self.env.archive_files(), [])

    def staged_member(self, entry, rel):
        return self.fh.data_dir / 'staging' / PLAN_ID / str(entry.seq) / rel

    def test_a_member_written_to_while_it_is_copied_aborts_the_archive(self):
        root = self.fh.path('~/scratch/b2')
        self.fh.file('~/scratch/b2/only.bin', content=os.urandom(60_000), age_days=3)
        self.fh.dir('~/scratch/b2', age_days=3)
        entry = self.env.entry('~/scratch/b2', Action.ARCHIVE, Unit.BUNDLE)
        hit = []

        def append(e):   # fires while only.bin is being copied (CHUNK is far smaller than the file)
            if not hit:
                hit.append(1)
                with open(self.staged_member(entry, 'only.bin'), 'ab') as f:
                    f.write(b'late write')
        self.env.hooks['archive_chunk'] = append
        # Switch the second walk off, so that only the check on the member itself can notice.
        with mock.patch.object(actions, 'CHUNK', 512), \
                mock.patch.object(actions, 'bundle_summary_matches', lambda *a: True):
            out = self.env.call(actions.archive, entry, name_taint=False)
        self.assertEqual(hit, [1])
        self.assertEqual((out.state, out.reason), (EntryState.SKIPPED, 'skip.changed'))
        self.assertEqual(self.env.archive_files(), [])                 # no archive, no .part
        self.assertEqual(self.env.staged(), [])
        self.assertTrue((root / 'only.bin').read_bytes().endswith(b'late write'))   # back in place, nothing deleted

    def test_a_member_changed_after_it_was_copied_is_caught_by_the_second_walk(self):
        root = self.fh.path('~/scratch/b2')
        self.fh.file('~/scratch/b2/a.bin', content=os.urandom(5000), age_days=3)
        self.fh.file('~/scratch/b2/z.bin', content=os.urandom(60_000), age_days=3)
        self.fh.dir('~/scratch/b2', age_days=3)
        entry = self.env.entry('~/scratch/b2', Action.ARCHIVE, Unit.BUNDLE)
        hit = []

        def append(e):   # a.bin went in long ago; this fires while z.bin is being copied
            if not hit:
                hit.append(1)
                with open(self.staged_member(entry, 'a.bin'), 'ab') as f:
                    f.write(b'after the copy')
        self.env.hooks['archive_chunk'] = append
        with mock.patch.object(actions, 'CHUNK', 512):
            out = self.env.call(actions.archive, entry, name_taint=False)
        self.assertEqual(hit, [1])
        self.assertEqual((out.state, out.reason), (EntryState.SKIPPED, 'skip.changed'))
        self.assertEqual(self.env.archive_files(), [])
        self.assertTrue((root / 'a.bin').read_bytes().endswith(b'after the copy'))

    def test_a_fifo_inside_aborts_and_the_bundle_goes_back(self):
        root = self.make_bundle()
        os.mkfifo(root / 'pipe')
        entry = self.env.entry(BUNDLE, Action.ARCHIVE, Unit.BUNDLE)
        out = self.env.call(actions.archive, entry, name_taint=False)
        self.assertEqual(out.state, EntryState.SKIPPED)
        self.assertEqual(out.reason, 'skip.unsupported_member:pipe')
        self.assertTrue(stat.S_ISFIFO(os.lstat(root / 'pipe').st_mode))
        self.assertEqual(self.env.staged(), [])
        self.assertEqual(self.env.archive_files(), [])

    def test_a_link_that_restore_would_refuse_is_not_archived(self):
        root = self.make_bundle()
        os.symlink('/etc/hosts', root / 'abs-link')
        entry = self.env.entry(BUNDLE, Action.ARCHIVE, Unit.BUNDLE)
        out = self.env.call(actions.archive, entry, name_taint=False)
        self.assertEqual(out.reason, 'skip.unsupported_member:abs-link')
        self.assertTrue(os.path.islink(root / 'abs-link'))

    def test_a_changed_bundle_is_not_archived(self):
        root = self.make_bundle()
        entry = self.env.entry(BUNDLE, Action.ARCHIVE, Unit.BUNDLE)
        self.fh.file(f'{BUNDLE}/late.txt', size=5)
        out = self.env.call(actions.archive, entry, name_taint=False)
        self.assertEqual(out.reason, 'skip.changed')
        self.assertTrue((root / 'late.txt').exists())
        self.assertEqual(self.env.archive_files(), [])

    def test_leftover_after_a_failed_removal_keeps_its_archive(self):
        root = self.make_bundle()
        entry = self.env.entry(BUNDLE, Action.ARCHIVE, Unit.BUNDLE)
        with mock.patch.object(actions, 'rmtree_ours', side_effect=OSError('boom')):
            out = self.env.call(actions.archive, entry, name_taint=False)
        self.assertEqual(out.state, EntryState.NEEDS_ATTENTION)
        self.assertIsNotNone(out.archive_id)
        self.assertEqual(self.env.store.archive(out.archive_id)['state'], 'stored')


class MaliciousArchiveTests(Case):
    """Archives built by hand, as an attacker with write access to the data folder would."""

    def craft(self, build, destination='~/scratch/target'):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w') as tar:
            build(tar)
        raw = buf.getvalue()
        compressed = gzip.compress(raw, mtime=0)
        archive_id = f'evil-{len(self.env.store.archives())}'
        adir = self.fh.data_dir / 'archives'
        adir.mkdir(mode=0o700, exist_ok=True)
        (adir / f'{archive_id}.tar.gz').write_bytes(compressed)
        manifest = dict(id=archive_id, path=destination, unit='bundle', fmt='tar.gz', orig_size=len(raw),
                        orig_alloc=len(raw), mode=0o755, mtime_ns=1_700_000_000_000_000_000,
                        sha_orig=hashlib.sha256(raw).hexdigest(), sha_stored=hashlib.sha256(compressed).hexdigest(),
                        stream_len=len(raw), stored_bytes=len(compressed), plan_id=PLAN_ID, seq=1, rule_id='x',
                        created_at=0.0)
        self.env._save_plan()
        self.env.store.add_archive(dict(
            id=archive_id, plan_id=PLAN_ID, seq=1, path=destination, fmt='tar.gz', orig_size=len(raw),
            orig_alloc=len(raw), stored_bytes=len(compressed), sha_orig=manifest['sha_orig'],
            sha_stored=manifest['sha_stored'], manifest=manifest, state='stored'))
        return archive_id

    @staticmethod
    def file_member(name, data=b'x'):
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mode = 0o644
        return info, io.BytesIO(data)

    def assert_refused(self, build, member_hint=''):
        outside_before = set(os.listdir(self.fh.root))
        archive_id = self.craft(build)
        with self.assertRaises(StewardError) as cm:
            actions.restore(self.env.ctx, archive_id)
        self.assertEqual((cm.exception.code, cm.exception.message_key), (ErrorCode.INVALID, 'error.restore_unsafe'))
        self.assertIn(member_hint, cm.exception.details['member'])
        self.assertFalse(os.path.lexists(self.fh.path('~/scratch/target')))
        parent = self.fh.path('~/scratch')
        self.assertEqual([n for n in os.listdir(parent) if n.startswith('.ds-restore')] if parent.exists() else [], [])
        self.assertEqual(set(os.listdir(self.fh.root)), outside_before)
        self.assertEqual(self.env.store.archive(archive_id)['state'], 'stored')

    def test_parent_directory_member(self):
        def build(tar):
            tar.addfile(*self.file_member('../evil.txt'))
        self.assert_refused(build, 'evil')
        self.assertFalse(os.path.lexists(self.fh.path('~/evil.txt')))

    def test_parent_directory_in_the_middle(self):
        def build(tar):
            tar.addfile(*self.file_member('a/../../evil2.txt'))
        self.assert_refused(build, 'evil2')

    def test_absolute_member(self):
        target = self.fh.root / 'absolute-evil.txt'

        def build(tar):
            tar.addfile(*self.file_member(str(target)))
        self.assert_refused(build, 'absolute-evil')
        self.assertFalse(target.exists())

    def test_dot_and_empty_components(self):
        for name in ('./x', 'a//b', 'a/./b', '..'):
            with self.subTest(name=name):
                def build(tar, name=name):
                    tar.addfile(*self.file_member(name))
                archive_id = self.craft(build)
                with self.assertRaises(StewardError) as cm:
                    actions.restore(self.env.ctx, archive_id)
                self.assertEqual(cm.exception.message_key, 'error.restore_unsafe')

    def test_symlink_escaping_the_bundle(self):
        for target in ('../../outside', '/etc/passwd', '..', 'a/../../x'):
            with self.subTest(target=target):
                def build(tar, target=target):
                    info = tarfile.TarInfo('link')
                    info.type = tarfile.SYMTYPE
                    info.linkname = target
                    tar.addfile(info)
                archive_id = self.craft(build)
                with self.assertRaises(StewardError) as cm:
                    actions.restore(self.env.ctx, archive_id)
                self.assertEqual(cm.exception.message_key, 'error.restore_unsafe')
                self.assertFalse(os.path.lexists(self.fh.path('~/scratch/target')))

    def test_chain_of_links_that_leaves_the_bundle(self):
        # d/e -> .. points at the bundle root (fine); x -> d/e/.. looks like "d" but really leads outside.
        def build(tar):
            d = tarfile.TarInfo('d')
            d.type = tarfile.DIRTYPE
            tar.addfile(d)
            e = tarfile.TarInfo('d/e')
            e.type = tarfile.SYMTYPE
            e.linkname = '..'
            tar.addfile(e)
            x = tarfile.TarInfo('x')
            x.type = tarfile.SYMTYPE
            x.linkname = 'd/e/..'
            tar.addfile(x)
        self.assert_refused(build)

    def test_write_through_a_link_member_is_refused(self):
        def build(tar):
            info = tarfile.TarInfo('d')
            info.type = tarfile.SYMTYPE
            info.linkname = '.'
            tar.addfile(info)
            tar.addfile(*self.file_member('d/inside.txt'))   # lands in the bundle root, never through the link
        archive_id = self.craft(build)
        with self.assertRaises(StewardError) as cm:
            actions.restore(self.env.ctx, archive_id)
        self.assertEqual(cm.exception.message_key, 'error.restore_unsafe')

    def test_devices_fifos_and_hard_links(self):
        for kind in (tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE, tarfile.LNKTYPE):
            with self.subTest(kind=kind):
                def build(tar, kind=kind):
                    info = tarfile.TarInfo('thing')
                    info.type = kind
                    if kind == tarfile.LNKTYPE:
                        info.linkname = '/etc/passwd'
                    tar.addfile(info)
                archive_id = self.craft(build)
                with self.assertRaises(StewardError) as cm:
                    actions.restore(self.env.ctx, archive_id)
                self.assertEqual(cm.exception.message_key, 'error.restore_unsafe')

    def test_the_same_path_twice(self):
        def build(tar):
            tar.addfile(*self.file_member('same.txt', b'1'))
            tar.addfile(*self.file_member('same.txt', b'2'))
        self.assert_refused(build, 'same.txt')

    def test_archive_that_does_not_match_its_manifest(self):
        def build(tar):
            tar.addfile(*self.file_member('fine.txt', b'content'))
        archive_id = self.craft(build)
        row = self.env.store.archive(archive_id)
        gz = self.fh.data_dir / 'archives' / f'{archive_id}.tar.gz'
        raw = gzip.decompress(gz.read_bytes())
        other = io.BytesIO()
        with tarfile.open(fileobj=other, mode='w') as tar:
            tar.addfile(*self.file_member('fine.txt', b'CONTENT'))
        gz.write_bytes(gzip.compress(other.getvalue(), mtime=0))
        with self.assertRaises(StewardError) as cm:
            actions.restore(self.env.ctx, archive_id)
        self.assertEqual(cm.exception.message_key, 'error.archive_corrupt')
        self.assertFalse(os.path.lexists(self.fh.path('~/scratch/target')))
        self.assertEqual(self.env.store.archive(archive_id)['state'], 'stored')

    def test_a_stream_longer_than_the_manifest_promises_is_cut_off(self):
        def build(tar):
            tar.addfile(*self.file_member('big.bin', b'\0' * 400_000))
        archive_id = self.craft(build)
        row = self.env.store.archive(archive_id)
        manifest = dict(row['manifest'], stream_len=2048)       # the manifest claims a much shorter stream
        with mock.patch.object(self.env.store, 'archive', return_value=dict(row, manifest=manifest)):
            with self.assertRaises(StewardError) as cm:
                actions.restore(self.env.ctx, archive_id)
        self.assertEqual(cm.exception.message_key, 'error.archive_corrupt')
        self.assertIn('longer than the manifest', cm.exception.details['reason'])
        self.assertEqual([n for n in os.listdir(self.fh.path('~/scratch')) if n.startswith('.ds-restore')], [])

    def test_a_clean_hand_made_archive_does_restore(self):
        def build(tar):
            d = tarfile.TarInfo('dir')
            d.type = tarfile.DIRTYPE
            d.mode = 0o755
            tar.addfile(d)
            tar.addfile(*self.file_member('dir/ok.txt', b'fine'))
            link = tarfile.TarInfo('dir/rel')
            link.type = tarfile.SYMTYPE
            link.linkname = '../dir/ok.txt'
            tar.addfile(link)
        archive_id = self.craft(build)
        actions.restore(self.env.ctx, archive_id)
        self.assertEqual((self.fh.path('~/scratch/target/dir/ok.txt')).read_bytes(), b'fine')
        self.assertEqual(os.readlink(self.fh.path('~/scratch/target/dir/rel')), '../dir/ok.txt')


class ParentSafetyTests(Case):
    def test_symlinked_ancestor_is_refused(self):
        self.fh.file('~/real/inner/f.txt', size=10)
        os.symlink(self.fh.path('~/real'), self.fh.path('~/link'))
        with self.assertRaises(OSError):
            with actions.open_parent_checked(self.fh.path('~/link/inner/f.txt'), self.fh.home, os.getuid()):
                pass

    def test_foreign_owner_below_the_base_is_refused(self):
        self.fh.file('~/a/b/f.txt', size=10)
        real = os.fstat

        class Foreign:
            def __init__(self, st):
                self._st = st
                self.st_uid = st.st_uid + 1

            def __getattr__(self, name):
                return getattr(self._st, name)

        def fstat(fd):
            st = real(fd)
            return Foreign(st) if stat.S_ISDIR(st.st_mode) and os.path.samestat(st, os.lstat(self.fh.path('~/a'))) else st

        with mock.patch.object(actions.os, 'fstat', fstat):
            with self.assertRaises(actions.UnsafeParent):
                with actions.open_parent_checked(self.fh.path('~/a/b/f.txt'), self.fh.home, os.getuid()):
                    pass

    def test_root_owned_ancestors_above_the_base_are_fine(self):
        self.fh.file('~/a/f.txt', size=10)
        with actions.open_parent_checked(self.fh.path('~/a/f.txt'), self.fh.home, os.getuid()) as (fd, name):
            self.assertEqual(name, 'f.txt')


if __name__ == '__main__':
    unittest.main()
