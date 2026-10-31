from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from disk_steward.locking import OperationLock
from disk_steward.model import ErrorCode, StewardError
from support import render_error

SRC = str(Path(__file__).resolve().parent.parent / 'src')
HOLDER = (
    'import sys\n'
    'from pathlib import Path\n'
    'from disk_steward.locking import OperationLock\n'
    'with OperationLock(Path(sys.argv[1])):\n'
    '    print("ready", flush=True)\n'
    '    sys.stdin.readline()\n'
)


class LockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / 'data'

    def assertBusy(self, lock):
        with self.assertRaises(StewardError) as cm:
            with lock:
                self.fail('lock should have been refused')
        self.assertEqual(cm.exception.code, ErrorCode.BUSY)
        self.assertEqual(cm.exception.message_key, 'error.busy')
        render_error(cm.exception)

    def test_acquire_release_and_reacquire(self):
        lock = OperationLock(self.dir)
        with lock:
            pass
        with lock:
            pass
        with OperationLock(self.dir):
            pass

    def test_lock_file_is_private_and_stays(self):
        with OperationLock(self.dir):
            pass
        path = self.dir / 'steward.lock'
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)

    def test_second_holder_in_the_same_thread_is_busy(self):
        with OperationLock(self.dir):
            self.assertBusy(OperationLock(self.dir))
        with OperationLock(self.dir):
            pass

    def test_reentering_the_same_instance_is_busy_not_a_deadlock(self):
        lock = OperationLock(self.dir)
        with lock:
            self.assertBusy(lock)
        with lock:
            pass

    def test_other_thread_is_busy(self):
        results = []
        with OperationLock(self.dir):
            def other():
                try:
                    with OperationLock(self.dir):
                        results.append('acquired')
                except StewardError as e:
                    results.append(e.code)
            t = threading.Thread(target=other)
            t.start()
            t.join()
        self.assertEqual(results, [ErrorCode.BUSY])

    def test_different_data_dirs_do_not_conflict(self):
        with OperationLock(self.dir):
            with OperationLock(Path(self.tmp.name) / 'other'):
                pass

    def test_released_on_exception(self):
        with self.assertRaises(ZeroDivisionError):
            with OperationLock(self.dir):
                1 / 0
        with OperationLock(self.dir):
            pass

    def test_exit_without_enter_is_an_error(self):
        with self.assertRaises(RuntimeError):
            OperationLock(self.dir).__exit__(None, None, None)

    def test_symlinked_lock_file_is_refused_and_releases_the_thread_lock(self):
        self.dir.mkdir()
        target = Path(self.tmp.name) / 'elsewhere'
        target.write_text('')
        os.symlink(target, self.dir / 'steward.lock')
        with self.assertRaises(OSError):
            with OperationLock(self.dir):
                pass
        (self.dir / 'steward.lock').unlink()
        with OperationLock(self.dir):
            pass

    def test_held_is_true_only_while_this_object_holds_a_live_lock_file(self):
        lock = OperationLock(self.dir)
        self.assertFalse(lock.held)
        with lock:
            self.assertTrue(lock.held)
        self.assertFalse(lock.held)

    def test_held_is_false_once_the_lock_file_was_removed_or_replaced(self):
        # a removed or replaced file means the next process locks a different inode: nothing is excluded any more
        for how in ('removed', 'replaced'):
            with self.subTest(how=how):
                lock = OperationLock(self.dir)
                with lock:
                    path = self.dir / 'steward.lock'
                    os.unlink(path)
                    if how == 'replaced':
                        path.write_text('')
                    self.assertFalse(lock.held)

    def test_held_by_other_probe(self):
        lock = OperationLock(self.dir)
        self.assertFalse(lock.held_by_other())          # no lock file yet
        with lock:
            self.assertFalse(lock.held_by_other())      # we hold it ourselves
            self.assertTrue(OperationLock(self.dir).held_by_other())
        self.assertFalse(lock.held_by_other())
        self.assertFalse(OperationLock(self.dir).held_by_other())
        with lock:
            pass                                         # the probe must not leave the lock taken
        self.assertFalse(OperationLock(self.dir).held_by_other())

    def test_another_process_excludes_us(self):
        proc = subprocess.Popen([sys.executable, '-c', HOLDER, str(self.dir)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, text=True, env={**os.environ, 'PYTHONPATH': SRC})
        try:
            self.assertEqual(proc.stdout.readline().strip(), 'ready')
            mine = OperationLock(self.dir)
            self.assertTrue(mine.held_by_other())
            self.assertBusy(mine)
        finally:
            proc.stdin.write('\n')
            proc.stdin.close()
            proc.wait(timeout=10)
            proc.stdout.close()
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(mine.held_by_other())
        with mine:
            pass

    def test_lock_dies_with_its_process(self):
        proc = subprocess.Popen([sys.executable, '-c', HOLDER, str(self.dir)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, text=True, env={**os.environ, 'PYTHONPATH': SRC})
        self.assertEqual(proc.stdout.readline().strip(), 'ready')
        proc.kill()
        proc.wait(timeout=10)
        proc.stdin.close()
        proc.stdout.close()
        with OperationLock(self.dir):
            pass


if __name__ == '__main__':
    unittest.main()
