from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from disk_steward import audit as audit_mod
from disk_steward.audit import Audit
from support import Clock


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / 'data'
        self.clock = Clock(1_790_000_000.5)
        self.audit = Audit(self.data, clock=self.clock)
        self.log = self.data / 'audit' / 'audit.jsonl'

    def lines(self, path=None):
        return (path or self.log).read_text(encoding='utf-8').splitlines()

    def test_line_format_and_modes(self):
        self.audit.write({'event': 'intent', 'plan': 'p1', 'path': '~/x', 'note': '中文 "q"'})
        (line,) = self.lines()
        rec = json.loads(line)
        self.assertEqual(list(rec)[0], 'time')
        self.assertEqual(rec['event'], 'intent')
        self.assertEqual(rec['note'], '中文 "q"')
        self.assertEqual(datetime.fromisoformat(rec['time']), datetime.fromtimestamp(1_790_000_000.5, timezone.utc))
        self.assertTrue(rec['time'].endswith('+00:00'))
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.log.parent.stat().st_mode), 0o700)

    def test_appends(self):
        for n in range(5):
            self.clock.advance(1)
            self.audit.write({'n': n})
        self.assertEqual([json.loads(l)['n'] for l in self.lines()], list(range(5)))
        again = Audit(self.data)                                  # a new instance keeps appending
        again.write({'n': 5})
        self.assertEqual(len(self.lines()), 6)

    def test_event_may_not_carry_time(self):
        with self.assertRaises(ValueError):
            self.audit.write({'time': 'x'})
        self.assertFalse(self.log.exists())

    def test_unserialisable_event_is_loud_and_writes_nothing(self):
        with self.assertRaises(TypeError):
            self.audit.write({'bad': object()})
        self.assertFalse(self.log.exists())

    def test_lone_surrogates_in_paths_survive(self):
        self.audit.write({'path': os.fsdecode(b'~/bad-\xff')})
        self.assertEqual(json.loads(self.lines()[0])['path'], os.fsdecode(b'~/bad-\xff'))

    def test_rotation_keeps_five_generations(self):
        with mock.patch.object(audit_mod, 'MAX_BYTES', 400):
            for n in range(120):
                self.audit.write({'n': n, 'pad': 'x' * 40})
        names = sorted(p.name for p in self.log.parent.iterdir())
        self.assertEqual(names, ['audit.jsonl'] + [f'audit.jsonl.{i}' for i in range(1, 6)])
        seen = []
        for name in ['audit.jsonl.5', 'audit.jsonl.4', 'audit.jsonl.3', 'audit.jsonl.2', 'audit.jsonl.1',
                     'audit.jsonl']:
            seen += [json.loads(l)['n'] for l in self.lines(self.log.parent / name)]
        self.assertEqual(seen, list(range(seen[0], 120)))        # contiguous, newest events kept in order
        self.assertGreater(seen[0], 0)                           # the oldest were dropped
        for path in self.log.parent.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_rotates_when_larger_than_the_limit_only(self):
        with mock.patch.object(audit_mod, 'MAX_BYTES', 10_000):
            self.audit.write({'n': 0})
            self.audit.write({'n': 1})
        self.assertEqual(sorted(p.name for p in self.log.parent.iterdir()), ['audit.jsonl'])
        self.log.write_bytes(b'x' * (audit_mod.MAX_BYTES + 1))
        self.audit.write({'n': 2})
        self.assertEqual(len(self.lines()), 1)
        self.assertEqual((self.log.parent / 'audit.jsonl.1').stat().st_size, audit_mod.MAX_BYTES + 1)

    def test_symlinked_log_is_refused(self):
        self.log.parent.mkdir(parents=True)
        target = Path(self.tmp.name) / 'elsewhere'
        target.write_text('')
        os.symlink(target, self.log)
        with self.assertRaises(OSError):
            self.audit.write({'n': 1})
        self.assertEqual(target.read_text(), '')

    def test_concurrent_writers_never_interleave(self):
        def worker(idx):
            for n in range(40):
                self.audit.write({'who': idx, 'n': n, 'pad': 'y' * 200})
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        recs = [json.loads(l) for l in self.lines()]
        self.assertEqual(len(recs), 320)
        for idx in range(8):
            self.assertEqual([r['n'] for r in recs if r['who'] == idx], list(range(40)))


if __name__ == '__main__':
    unittest.main()
