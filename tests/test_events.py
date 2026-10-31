from __future__ import annotations

import threading
import time
import unittest

from disk_steward.events import EventBus

from support import Clock


class EventBusTests(unittest.TestCase):
    def test_versions_start_at_one_and_increase(self):
        bus = EventBus()
        self.assertEqual(bus.version, 0)
        self.assertEqual([bus.publish('a'), bus.publish('b', n=1), bus.publish('c')], [1, 2, 3])
        self.assertEqual(bus.version, 3)

    def test_event_shape_uses_the_clock(self):
        bus = EventBus(clock=Clock(1234.5))
        bus.publish('scan.done', scan_id=7)
        self.assertEqual(bus.since(0), [{'version': 1, 'time': 1234.5, 'type': 'scan.done', 'data': {'scan_id': 7}}])

    def test_since_returns_only_newer_events_in_order(self):
        bus = EventBus()
        for i in range(5):
            bus.publish('e', i=i)
        self.assertEqual([e['data']['i'] for e in bus.since(2)], [2, 3, 4])
        self.assertEqual(bus.since(5), [])
        self.assertEqual(bus.since(99), [])
        self.assertEqual(len(bus.since(0)), 5)

    def test_log_is_bounded_to_500(self):
        bus = EventBus()
        for i in range(620):
            bus.publish('e', i=i)
        events = bus.since(0)
        self.assertEqual(len(events), 500)
        self.assertEqual((events[0]['version'], events[-1]['version']), (121, 620))
        self.assertEqual(bus.version, 620)

    def test_wait_returns_at_once_when_something_is_newer(self):
        bus = EventBus()
        bus.publish('a')
        t0 = time.monotonic()
        self.assertEqual([e['type'] for e in bus.wait(0, timeout=5)], ['a'])
        self.assertLess(time.monotonic() - t0, 1)

    def test_wait_times_out_with_an_empty_list(self):
        bus = EventBus()
        bus.publish('a')
        t0 = time.monotonic()
        self.assertEqual(bus.wait(1, timeout=0.15), [])
        self.assertGreaterEqual(time.monotonic() - t0, 0.1)

    def test_wait_wakes_when_another_thread_publishes(self):
        bus = EventBus()
        got = []
        started = threading.Event()

        def waiter():
            started.set()
            got.extend(bus.wait(0, timeout=10))
        t = threading.Thread(target=waiter)
        t.start()
        started.wait()
        time.sleep(0.05)
        t0 = time.monotonic()
        bus.publish('late', x=1)
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertLess(time.monotonic() - t0, 2)
        self.assertEqual([(e['type'], e['data']) for e in got], [('late', {'x': 1})])

    def test_concurrent_publishers_get_unique_consecutive_versions(self):
        bus = EventBus()
        versions = []
        lock = threading.Lock()

        def work():
            mine = [bus.publish('e') for _ in range(200)]
            with lock:
                versions.extend(mine)
        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(versions), list(range(1, 1601)))
        self.assertEqual(bus.version, 1600)
        self.assertEqual([e['version'] for e in bus.since(0)], list(range(1101, 1601)))


if __name__ == '__main__':
    unittest.main()
