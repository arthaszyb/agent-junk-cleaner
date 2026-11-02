"""The data dir (or only the database) is deleted while Disk Steward runs: it is made again, the user is told once,
and nothing that was in flight carries on into the new database."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import m2_fixture as fx
from disk_steward import i18n
from disk_steward.cli import main
from disk_steward.model import ErrorCode, StewardError
from disk_steward.portal import launch
from disk_steward.portal.server import PortalServer
from disk_steward.settings import Settings
from disk_steward.store import Store

from m3_support import make_app, make_mac
from m4_support import JUNK_14D, make_plan
from portal_support import Client, make_portal_app
from support import Clock, FakeHome

KEY = 'error.DATA_DIR_RECREATED'
EN = i18n.t(KEY, 'en')
ZH = i18n.t(KEY, 'zh-CN')


def tearDownModule():
    if i18n.MISSING:
        raise AssertionError(f'untranslated keys: {sorted(i18n.MISSING)}')


def drop(fh, only_db=False):
    """Delete the data dir (or only steward.db and its WAL files) the way `rm -rf ~/Library` would."""
    if not only_db:
        shutil.rmtree(fh.data_dir)
        return
    for name in ('steward.db', 'steward.db-wal', 'steward.db-shm'):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(fh.data_dir / name)


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class Base(unittest.TestCase):
    def setUp(self):
        self.fh = FakeHome(Clock()).__enter__()
        self.addCleanup(self.fh.cleanup)
        fx.build(self.fh)
        self.mac = make_mac()

    def new_app(self, **kw):
        app = make_app(self.fh, mac=self.mac, **kw)
        self.addCleanup(app.close)
        return app

    def assertEmptyUsableDb(self, store):
        store.check()   # the calling thread may hold a connection to the old file: a new operation starts afresh
        self.assertIsNone(store.latest_scan())
        store.set_meta('probe', '1')
        self.assertEqual(store.meta('probe'), '1')
        self.assertEqual(mode(store.path), 0o600)

    def assertLost(self, fn, *args, **kw):
        with self.assertRaises(StewardError) as cm:
            fn(*args, **kw)
        self.assertEqual((cm.exception.code, cm.exception.message_key), (ErrorCode.DATA_DIR_RECREATED, KEY))


class StoreTests(Base):
    def setUp(self):
        super().setUp()
        self.store = Store(self.fh.data_dir / 'steward.db')
        self.addCleanup(self.store.close)
        self.calls = []
        self.store.on_recreated = lambda: self.calls.append(1)

    def test_nothing_to_do_while_the_file_is_where_it_was(self):
        self.assertFalse(self.store.check())
        self.assertEqual(self.calls, [])

    def test_a_deleted_data_dir_is_made_again_private_and_empty(self):
        self.store.set_meta('old', 'x')
        drop(self.fh)
        self.assertTrue(self.store.check())
        self.assertEqual(mode(self.fh.data_dir), 0o700)
        self.assertEmptyUsableDb(self.store)
        self.assertIsNone(self.store.meta('old'))
        self.assertEqual(self.calls, [1])
        self.assertFalse(self.store.check())   # once

    def test_a_deleted_database_file_is_made_again(self):
        self.store.set_meta('old', 'x')
        drop(self.fh, only_db=True)
        self.assertTrue(self.store.check())
        self.assertEmptyUsableDb(self.store)
        self.assertIsNone(self.store.meta('old'))

    def test_a_database_replaced_by_another_process_is_adopted(self):
        drop(self.fh)
        other = Store(self.fh.data_dir / 'steward.db')   # what a CLI run in the meantime would do
        self.addCleanup(other.close)
        other.set_meta('theirs', '1')
        self.assertTrue(self.store.check())
        self.assertEqual(self.store.meta('theirs'), '1')   # not wiped: the file is theirs now

    def test_the_new_database_survives_the_old_connection_being_closed(self):
        self.store.set_meta('old', 'x')
        drop(self.fh)
        self.store.check()
        self.store.set_meta('new', '1')
        self.store.close()
        again = Store(self.fh.data_dir / 'steward.db')
        self.addCleanup(again.close)
        self.assertEqual((again.meta('new'), again.meta('old')), ('1', None))

    def test_work_on_the_old_file_stops_and_never_reaches_the_new_one(self):
        sid = self.store.begin_scan('c', 'f', 's', ['agent'])
        drop(self.fh)
        self.assertLost(self.store.finish_scan, sid, 'done', 1, 1, 1)
        self.assertEqual(self.calls, [1])
        self.assertLost(self.store.discard_items, sid)   # every call of that thread, until it starts afresh
        self.assertFalse(self.store.check())
        self.assertIsNone(self.store.latest_scan())
        self.assertEqual(self.store._conn().execute('SELECT COUNT(*) FROM scans').fetchone()[0], 0)

    def test_a_running_thread_stops_when_another_thread_found_the_loss_first(self):
        sid = self.store.begin_scan('c', 'f', 's', ['agent'])
        drop(self.fh)
        found = []
        t = threading.Thread(target=lambda: found.append(self.store.check()))
        t.start()
        t.join()
        self.assertEqual(found, [True])
        self.assertLost(self.store.finish_scan, sid, 'done', 1, 1, 1)
        self.assertEqual(self.calls, [1])

    def test_a_new_thread_just_works_on_the_new_file(self):
        drop(self.fh)
        out = []
        t = threading.Thread(target=lambda: out.append(self.store.latest_scan()))
        t.start()
        t.join()
        self.assertEqual(out, [None])
        self.assertEqual(self.calls, [1])
        self.assertTrue(os.path.exists(self.store.path))


class AppTests(Base):
    def test_what_lived_only_in_files_is_written_back(self):
        app = self.new_app()
        app.scan()
        drop(self.fh)
        self.assertTrue(app.check_data_dir())
        self.assertEqual(mode(self.fh.data_dir), 0o700)
        self.assertIsNone(app.latest_scan())
        self.assertTrue((self.fh.data_dir / 'steward.lock').exists())
        self.assertEqual(json.loads((self.fh.data_dir / 'settings.json').read_text())['domains'], ['agent'])
        audit = (self.fh.data_dir / 'audit' / 'audit.jsonl').read_text().splitlines()
        self.assertEqual([json.loads(line)['event'] for line in audit], ['data_dir.recreated'])
        events = [e for e in app.events.since(0) if e['type'] == 'data_dir.recreated']
        self.assertEqual([e['data']['message_key'] for e in events], [KEY])
        app.scan()   # a new scan is all that is needed
        self.assertEqual(app.latest_scan()['status'], 'done')
        with app.lock:
            pass

    def test_default_settings_are_not_written(self):
        app = self.new_app(settings=Settings())
        drop(self.fh)
        app.check_data_dir()
        self.assertFalse((self.fh.data_dir / 'settings.json').exists())

    def test_a_scan_in_progress_ends_with_the_message_and_writes_nothing(self):
        app = self.new_app()
        seen = []

        def progress(event):
            if not seen:
                seen.append(event)
                drop(self.fh)
        with self.assertRaises(StewardError) as cm:
            app.scan(progress=progress)
        self.assertEqual((cm.exception.code, cm.exception.message_key), (ErrorCode.DATA_DIR_RECREATED, KEY))
        self.assertTrue(seen)
        self.assertFalse(app.check_data_dir())   # the scan thread's own heal; this call only starts afresh
        self.assertIsNone(app.latest_scan())
        self.assertEqual(app.store._conn().execute('SELECT COUNT(*) FROM items').fetchone()[0], 0)
        app.scan()

    def test_a_cleanup_in_progress_ends_with_the_message_and_removes_nothing_more(self):
        done = []

        def lose(entry):
            if not done:
                done.append(entry.path)
                drop(self.fh)
        app = self.new_app(hooks={'after_claim': lose})
        app.scan()
        plan = make_plan(app, JUNK_14D)
        paths = [r['path'] for r in app.entries(plan['id'])['rows']]
        self.assertEqual(len(paths), 3)
        with self.assertRaises(StewardError) as cm:
            app.run_plan(plan['id'], plan['revision'], plan['entries_digest'])
        self.assertEqual((cm.exception.code, cm.exception.message_key), (ErrorCode.DATA_DIR_RECREATED, KEY))
        for path in paths[1:]:
            self.assertTrue(self.fh.path(path).exists(), path)   # the entries after the one in flight were not touched
        job = app.job()
        self.assertEqual((job['status'], job['error_code']), ('error', 'DATA_DIR_RECREATED'))
        self.assertNotIn('scan.failed', [e['type'] for e in app.events.since(0)])   # no rescan of a database that is gone
        self.assertFalse(app.check_data_dir())
        self.assertIsNone(app.latest_scan())
        with app.lock:   # the lock is free again
            pass


class PortalTests(Base):
    def setUp(self):
        super().setUp()
        self.app = make_portal_app(self.fh, mac=self.mac)
        self.addCleanup(self.app.close)
        self.app.scan()
        self.server = PortalServer(self.app, 0)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.c = Client(self.server).login()
        self.info = json.loads((self.fh.data_dir / 'portal.json').read_text())

    def assertFindable(self):
        self.assertEqual(json.loads((self.fh.data_dir / 'portal.json').read_text()), self.info)
        self.assertEqual(mode(self.fh.data_dir / 'portal.json'), 0o600)
        url = launch.request_launch_url(self.fh.data_dir)   # what `disk-steward portal --url` does
        self.assertEqual(Client(self.server).post('/api/session', {'code': url.split('code=')[1]}, token=False)[0], 200)

    def check(self, only_db):
        before = self.c.get('/api/state')[1]
        self.assertEqual(before['scan']['status'], 'done')
        drop(self.fh, only_db)
        status, state = self.c.get('/api/state')
        self.assertEqual(status, 200, state)
        self.assertIsNone(state['scan'])
        if not only_db:
            self.assertEqual(mode(self.fh.data_dir), 0o700)
        self.assertEmptyUsableDb(self.app.store)
        self.assertFindable()
        status, events = self.c.get('/api/events?since=0&timeout=0')
        self.assertEqual(status, 200)
        found = [e for e in events['events'] if e['type'] == 'data_dir.recreated']
        self.assertEqual(len(found), 1)   # told once
        self.assertEqual(found[0]['data']['message'], EN)
        self.assertEqual(self.c.get('/api/overview')[0], 409)   # NO_SCAN: a new scan is needed
        self.assertEqual(self.c.get('/api/events?since=0&timeout=0')[1]['events'].count(found[0]), 1)

    def test_a_deleted_data_dir_is_made_again_on_the_next_request(self):
        self.check(only_db=False)

    def test_a_deleted_database_is_made_again_on_the_next_request(self):
        self.check(only_db=True)

    def test_the_message_follows_the_users_language(self):
        self.app.update_settings({'locale': 'zh-CN'})
        drop(self.fh)
        self.c.get('/api/state')
        events = self.c.get('/api/events?since=0&timeout=0')[1]['events']
        self.assertEqual([e['data']['message'] for e in events if e['type'] == 'data_dir.recreated'], [ZH])

    def test_the_request_that_finds_it_is_not_a_500(self):
        drop(self.fh)
        for path in ('/api/overview', '/api/plans', '/api/groups', '/api/job', '/api/archives', '/api/insights'):
            with self.subTest(path=path):
                self.assertLess(self.c.get(path)[0], 500)
            self.assertTrue((self.fh.data_dir / 'steward.db').exists())

    def test_a_scan_the_page_started_ends_with_the_message_not_a_traceback(self):
        started, go = threading.Event(), threading.Event()
        real = self.app.store.add_items

        def slow(*a, **kw):
            started.set()
            go.wait(30)
            return real(*a, **kw)
        with mock.patch.object(self.app.store, 'add_items', side_effect=slow):
            self.assertEqual(self.c.post('/api/scan/start', {})[0], 200)
            self.assertTrue(started.wait(30))
            drop(self.fh)
            self.c.get('/api/state')   # the next request makes the dir again; the scan still holds the old file
            go.set()
            deadline = time.monotonic() + 30
            while self.app.job()['status'] == 'running':
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
        job = self.c.get('/api/job')[1]['job']
        self.assertEqual((job['status'], job['error_code']), ('error', 'DATA_DIR_RECREATED'))
        self.assertEqual(job['error_text'], EN)
        self.app.check_data_dir()
        self.assertIsNone(self.app.latest_scan())

    def test_an_idle_portal_writes_portal_json_back_by_itself(self):
        with mock.patch('disk_steward.portal.server.WATCH_SECONDS', 0.05):
            watched = PortalServer(self.app, 0)
            self.server.stop()   # one portal per data dir
            watched.start()
            self.addCleanup(watched.stop)
            self.info = json.loads((self.fh.data_dir / 'portal.json').read_text())
            drop(self.fh)
            deadline = time.monotonic() + 30
            while not (self.fh.data_dir / 'portal.json').exists():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
        self.server = watched
        self.assertFindable()


class CliTests(Base):
    def run_main(self, argv, app):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv, app_factory=lambda: app)
        return code, out.getvalue(), err.getvalue()

    def test_a_command_after_the_loss_says_so_once_and_carries_on(self):
        app = self.new_app()
        app.scan()
        drop(self.fh)
        code, out, err = self.run_main(['report'], app)
        self.assertEqual(err.count(f'Error [DATA_DIR_RECREATED]: {EN}'), 1)
        self.assertEqual(code, 2)   # report has no scan to print now
        self.assertIn('NO_SCAN', err)
        self.assertEmptyUsableDb(app.store)
        self.assertEqual(mode(self.fh.data_dir), 0o700)
        code, out, err = self.run_main(['report'], app)   # not again
        self.assertNotIn('DATA_DIR_RECREATED', err)

    def test_the_text_follows_lang(self):
        app = self.new_app()
        drop(self.fh)
        _, _, err = self.run_main(['--lang', 'zh-CN', 'report'], app)
        self.assertIn(ZH, err)
        self.assertEqual(err.count(ZH), 1)

    def test_a_json_command_keeps_stdout_clean(self):
        app = self.new_app()
        app.scan()
        drop(self.fh)
        code, out, err = self.run_main(['list', '--json'], app)
        self.assertIn(EN, err)
        self.assertNotIn(KEY, out)

    def test_a_command_that_loses_the_dir_halfway_exits_with_the_message(self):
        app = self.new_app()
        real = app.store.begin_scan

        def lose(*a, **kw):
            drop(self.fh)
            return real(*a, **kw)
        with mock.patch.object(app.store, 'begin_scan', side_effect=lose):
            code, out, err = self.run_main(['scan'], app)
        self.assertEqual(code, 2)
        self.assertEqual(err.count(f'Error [DATA_DIR_RECREATED]: {EN}'), 1)
        self.assertNotIn('Traceback', err)
        self.assertEqual(mode(self.fh.data_dir), 0o700)
        self.assertEmptyUsableDb(app.store)


if __name__ == '__main__':
    unittest.main()
