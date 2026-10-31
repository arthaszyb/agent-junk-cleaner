"""The portal server against a real loopback socket, on an App over a FakeHome with a FakeMacOS and a fake advisor."""
from __future__ import annotations

import contextlib
import hmac
import io
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

import m2_fixture as fx
from disk_steward import i18n
from disk_steward.cli import main as cli_main
from disk_steward.model import ErrorCode, StewardError
from disk_steward.portal import api as api_mod
from disk_steward.portal import launch
from disk_steward.portal.server import CSP, MAX_BODY, PortalServer, serve
from disk_steward.service import App
from disk_steward.settings import Settings

from m3_support import make_mac
from portal_support import Client, make_portal_app
from support import Clock, FakeHome

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / 'src'
LOG = '~/.claude/debug/x.txt'
CLAUDE_APP = 'com.anthropic.claudefordesktop'
JUNK_14D = [{'op': 'select_tier', 'tier': 'junk', 'on': True}, {'op': 'set_filter', 'min_age_days': 14}]


def tearDownModule():
    # the API renders texts with i18n.t, which records any key it cannot find
    if i18n.MISSING:
        raise AssertionError(f'untranslated keys: {sorted(i18n.MISSING)}')


class PortalCase(unittest.TestCase):
    """A running server, a signed-in client and an App on the M2 fixture (not yet scanned)."""
    server_kwargs: dict = {}
    app_kwargs: dict = {}

    def setUp(self):
        self.fh = FakeHome(Clock()).__enter__()
        self.addCleanup(self.fh.cleanup)
        fx.build(self.fh)
        self.mac = make_mac()
        self.app = make_portal_app(self.fh, mac=self.mac, **self.app_kwargs)
        self.addCleanup(self.app.close)
        self.now = [1000.0]
        self.server = PortalServer(self.app, 0, clock=lambda: self.now[0], **self.server_kwargs)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.c = Client(self.server).login()

    # helpers
    def ok(self, resp, status=200):
        self.assertEqual(resp[0], status, resp[1])
        return resp[1]

    def error(self, resp, status, code):
        self.assertEqual(resp[0], status, resp[1])
        err = resp[1]['error']
        self.assertEqual(err['code'], code)
        self.assertEqual(set(err), {'code', 'message_key', 'message', 'details'})
        self.assertTrue(err['message'])
        return err

    def wait_for(self, condition, timeout=30):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail('timed out')
            time.sleep(0.01)

    def scan(self):
        self.ok(self.c.post('/api/scan'))
        self.wait_for(lambda: self.c.get('/api/job')[1]['job']['status'] in ('done', 'error'))
        self.assertEqual(self.c.get('/api/job')[1]['job']['status'], 'done')

    def select(self, ops=JUNK_14D):
        return self.ok(self.c.post('/api/spec/ops', {'ops': ops}))

    def make_plan(self, ops=JUNK_14D):
        sel = self.select(ops)
        expected = {'scan_id': sel['scan_id'], 'spec_version': sel['spec']['version'], 'digest': sel['digest'],
                    'resolved_at': sel['resolved_at']}
        return self.ok(self.c.post('/api/plans', {'expected': expected}))


# -- authentication ------------------------------------------------------------------------------------------------

class AuthTests(PortalCase):
    def code(self):
        return self.server.new_launch_url().split('code=')[1]

    def session(self, code, **kw):
        return Client(self.server).post('/api/session', {'code': code}, token=False, **kw)

    def test_a_launch_code_is_traded_for_a_token_once(self):
        code = self.code()
        status, body = self.session(code)
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {'token'})
        self.assertGreaterEqual(len(body['token']), 40)
        self.assertEqual(Client(self.server).get('/api/state', token=body['token'])[0], 200)
        err = self.error(self.session(code), 403, 'FORBIDDEN')    # single use
        self.assertEqual(err['message_key'], 'portal.error.bad_code')
        self.assertNotEqual(self.session(self.code())[1]['token'], body['token'])

    def test_a_launch_code_expires_after_two_minutes(self):
        code = self.code()
        self.now[0] += 119
        fresh = self.code()
        self.now[0] += 2   # the first is now 121 s old, the second 2 s
        self.error(self.session(code), 403, 'FORBIDDEN')
        self.assertEqual(self.session(fresh)[0], 200)

    def test_wrong_or_missing_codes_are_refused_and_spend_nothing(self):
        code = self.code()
        for body in ({}, {'code': ''}, {'code': 'nope'}, {'code': code + 'x'}, {'code': code[:-1]}, {'code': None},
                     {'code': 7}, {'code': [code]}, {'code': {'a': code}}, {'code': '\ud800'}, {'token': code}):
            with self.subTest(body=body):
                err = self.error(Client(self.server).post('/api/session', body, token=False), 403, 'FORBIDDEN')
                self.assertEqual(err['message_key'], 'portal.error.bad_code')
        self.assertEqual(self.session(code)[0], 200)   # the real one still works

    def test_the_session_route_has_the_same_request_checks_as_every_write(self):
        code = self.code()
        c = Client(self.server)
        self.assertEqual(c.request('POST', '/api/session', {'code': code}, origin=False, token=False)[0], 403)   # no Origin
        self.assertEqual(c.request('POST', '/api/session', {'code': code}, origin='http://evil.example.com', token=False)[0], 403)
        self.assertEqual(c.request('POST', '/api/session', {'code': code}, headers={'X-Disk-Steward': None}, token=False)[0], 403)
        self.assertEqual(c.request('POST', '/api/session', {'code': code}, headers={'Content-Type': 'text/plain'}, token=False)[0], 415)
        self.assertEqual(c.request('POST', '/api/session', raw=b'[]', token=False)[0], 400)
        self.assertEqual(c.request('GET', '/api/session', token=False)[0], 401)   # GET is not a route: it needs a token like the rest
        self.assertEqual(self.session(code)[0], 200)   # none of the refusals spent the code

    def test_launch_serves_the_page_and_neither_spends_the_code_nor_sets_a_cookie(self):
        code = self.code()
        for path in (f'/launch?code={code}', '/launch', '/launch?code=nope', '/?code=' + code):
            with self.subTest(path=path):
                status, headers, body = Client(self.server).request('GET', path, token=False)
                self.assertEqual(status, 200)
                self.assertIn(b'<script type="module" src="/static/app.mjs">', body)
                self.assertNotIn('set-cookie', headers)
                self.assertEqual(headers['cache-control'], 'no-store')
        self.assertEqual(self.session(code)[0], 200)

    def test_the_root_is_the_same_page_for_everybody(self):
        for token in (True, False, 'bogus'):
            status, headers, body = self.c.request('GET', '/', token=token)
            self.assertEqual(status, 200)
            self.assertIn(b'/static/app.mjs', body)
            self.assertNotIn('set-cookie', headers)

    def test_every_api_route_needs_the_token(self):
        routes = [(m, pattern.pattern[:-1]) for m, pattern, _ in self.server.api._routes]
        self.assertGreater(len(routes), 20)
        concrete = {'([^/]+)': 'x', '(?:/start)?': '', '(pause|resume|cancel)': 'pause', '(restore|purge)': 'restore',
                    '(enable|preview|run|export|import)': 'preview'}
        for method, pattern in routes:
            for k, v in concrete.items():
                pattern = pattern.replace(k, v)
            for token in (False, '', 'bogus', 'x' * 43):
                with self.subTest(method=method, path=pattern, token=token):
                    err = self.error(Client(self.server).json(method, pattern, {} if method != 'GET' else None, token=token),
                                     401, 'UNAUTHORIZED')
                    self.assertEqual(err['message_key'], 'portal.error.unauthorized')
        for method, path in (('GET', '/api/nope'), ('DELETE', '/api/state'), ('PATCH', '/api/state')):   # unknown routes too
            self.assertEqual(Client(self.server).request(method, path, token=False)[0], 401)

    def test_a_cookie_is_no_credential_and_no_cookie_is_ever_set(self):
        jar = f'ds_session={self.c.token}; other=1; ds_token={self.c.token}'
        for cookie in (jar, 'ds_session=bogus', f'session={self.c.token}'):
            with self.subTest(cookie=cookie):
                self.error(Client(self.server).get('/api/state', token=False, cookie=cookie), 401, 'UNAUTHORIZED')
        c = Client(self.server)
        responses = [c.request('GET', '/', token=False), self.c.request('GET', '/api/state'), self.c.request('GET', '/static/app.mjs'),
                     c.request('GET', '/api/state', token=False), self.c.request('POST', '/api/session', {'code': 'x'}, token=False)]
        responses.append(Client(self.server).request('POST', '/api/session', {'code': self.code()}, token=False))
        for status, headers, _ in responses:
            self.assertNotIn('set-cookie', headers, status)

    def test_a_cookie_of_another_app_on_127_0_0_1_does_not_matter(self):
        # browsers send every 127.0.0.1 cookie to every port
        noise = ['a=1', 'sessionid=abc; csrftoken=def', 'x' * 4000, 'ds_session', '=', ';;;', ' ; ', 'a=b=c', 'é=ü', 'ds_session=a;ds_session=b']
        for cookie in noise:
            with self.subTest(cookie=cookie[:30]):
                status, _, _ = self.c.request('GET', '/api/state', cookie=cookie)
                self.assertEqual(status, 200)
                status, _, _ = Client(self.server).request('GET', '/api/state', token=False, cookie=cookie)
                self.assertEqual(status, 401)

    def test_a_legacy_session_cookie_is_removed_once(self):
        status, headers, _ = self.c.request('GET', '/api/state', cookie='ds_session=old; other=1')
        self.assertEqual(status, 200)
        self.assertEqual(headers['set-cookie'], 'ds_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict')
        for path in ('/', '/static/app.mjs', '/launch'):   # whatever page the browser asks for first
            self.assertIn('Max-Age=0', Client(self.server).request('GET', path, token=False, cookie='x=1; ds_session=old')[1]['set-cookie'])
        self.assertIn('Max-Age=0', Client(self.server).request('GET', '/api/state', token=False, cookie='ds_session=old')[1]['set-cookie'])
        for cookie in (None, 'other=1', 'ds_session_x=1', 'xds_session=1'):   # and nothing is set for a browser without it
            self.assertNotIn('set-cookie', self.c.request('GET', '/api/state', cookie=cookie)[1])

    def test_the_token_is_compared_in_constant_time(self):
        with mock.patch('disk_steward.portal.auth.hmac.compare_digest', wraps=hmac.compare_digest) as spy:
            self.c.get('/api/state')
            Client(self.server).get('/api/state', token='y' * 43)
        self.assertGreaterEqual(spy.call_count, 2)

    def test_tokens_survive_for_the_life_of_the_server_and_are_independent(self):
        other = Client(self.server).login()
        self.assertNotEqual(other.token, self.c.token)
        self.assertEqual(self.c.get('/api/state')[0], 200)
        self.assertEqual(other.get('/api/state')[0], 200)

    def test_static_files_do_not_need_the_token(self):
        # the locked page needs its stylesheet; none of it is secret
        for path in ('/static/styles.css', '/static/app.mjs', '/static/locked.html'):
            self.assertEqual(Client(self.server).request('GET', path, token=False)[0], 200, path)

    def test_the_locked_page_says_how_to_get_a_link(self):
        _, _, body = Client(self.server).request('GET', '/static/locked.html', token=False)
        self.assertIn(b'disk-steward portal --url', body)
        self.assertIn('此页面已锁定'.encode(), body)

    def test_posts_need_the_token_too(self):
        self.error(Client(self.server).post('/api/scan', token=False), 401, 'UNAUTHORIZED')


# -- request checks ------------------------------------------------------------------------------------------------

class RequestCheckTests(PortalCase):
    def test_host_must_be_exactly_loopback_with_our_port(self):
        port = self.server.port
        good = (f'127.0.0.1:{port}', f'localhost:{port}')
        bad = ('evil.example.com', f'evil.example.com:{port}', '127.0.0.1', 'localhost', f'[::1]:{port}', '[::1]',
               f'127.0.0.1:{port + 1}', f'0.0.0.0:{port}', f'127.0.0.1.evil.com:{port}', f'LOCALHOST:{port}',
               f'127.0.0.1:{port}.evil.com', f'user@127.0.0.1:{port}', '')
        for host in good:
            with self.subTest(host=host):
                self.ok(Client(self.server, host=host).login().get('/api/state'))
        for host in bad:
            with self.subTest(host=host):
                c = Client(self.server, host=host)
                c.token = self.c.token
                for method, path in (('GET', '/api/state'), ('GET', '/'), ('GET', '/static/app.mjs'), ('GET', '/launch?code=x'),
                                     ('POST', '/internal/launch-code')):
                    status, _, body = c.request(method, path, headers={launch.SECRET_HEADER: self.server.launch_secret})
                    self.assertEqual(status, 421, (method, path))
                    self.assertEqual(json.loads(body)['error']['code'], 'MISDIRECTED')

    def test_a_request_without_a_host_header_is_refused(self):
        s = socket.create_connection(('127.0.0.1', self.server.port), timeout=10)
        self.addCleanup(s.close)
        s.sendall(b'GET /api/state HTTP/1.0\r\n\r\n')
        data = b''
        while chunk := s.recv(4096):
            data += chunk
        self.assertTrue(data.startswith(b'HTTP/1.0 421'), data[:60])

    def test_writes_need_a_matching_origin(self):
        port = self.server.port
        for origin in (None, 'null', 'http://evil.example.com', f'http://127.0.0.1:{port + 1}', f'https://127.0.0.1:{port}',
                       f'http://localhost:{port}', f'http://127.0.0.1:{port}/', ''):
            with self.subTest(origin=origin):
                headers = {'Origin': origin} if origin is not None else {'Origin': None}
                status, _, body = self.c.request('POST', '/api/job/pause', headers=headers, origin=False)
                self.assertEqual(status, 403)
                self.assertEqual(json.loads(body)['error']['message_key'], 'portal.error.bad_origin')
        # the same writes are fine when Host and Origin agree, whichever of the two names is used
        other = Client(self.server, host=f'localhost:{port}')
        other.token = self.c.token
        self.error(other.post('/api/job/pause'), 404, 'NOT_FOUND')   # past every check: "no run is active"
        self.error(self.c.post('/api/job/pause'), 404, 'NOT_FOUND')

    def test_writes_need_the_custom_header(self):
        for value in (None, '', '0', 'true'):
            with self.subTest(value=value):
                status, _, body = self.c.request('POST', '/api/job/pause', headers={'X-Disk-Steward': value})
                self.assertEqual(status, 403)
                self.assertEqual(json.loads(body)['error']['message_key'], 'portal.error.missing_header')

    def test_writes_must_be_json(self):
        for ctype in ('text/plain', 'application/x-www-form-urlencoded', 'multipart/form-data; boundary=x', '', 'application/jsonx'):
            with self.subTest(ctype=ctype):
                status, _, body = self.c.request('POST', '/api/job/pause', headers={'Content-Type': ctype})
                self.assertEqual(status, 415)
                self.assertEqual(json.loads(body)['error']['code'], 'UNSUPPORTED_MEDIA_TYPE')
        for ctype in ('application/json', 'application/json; charset=utf-8', 'Application/JSON'):
            with self.subTest(ctype=ctype):
                self.error(self.c.json('POST', '/api/job/pause', headers={'Content-Type': ctype}), 404, 'NOT_FOUND')

    def test_the_body_is_limited_to_one_mebibyte_and_must_be_a_json_object(self):
        pad = MAX_BODY - len(b'{"pad":""}')
        fits = b'{"pad":"' + b'x' * pad + b'"}'
        self.assertEqual(len(fits), MAX_BODY)
        self.error(self.c.json('POST', '/api/job/pause', raw=fits), 404, 'NOT_FOUND')   # accepted, then no run to pause
        self.error(self.c.json('POST', '/api/job/pause', raw=fits + b' '), 413, 'PAYLOAD_TOO_LARGE')
        self.error(self.c.json('POST', '/api/job/pause', raw=b'x' * (3 * MAX_BODY)), 413, 'PAYLOAD_TOO_LARGE')
        for raw in (b'', b'{', b'[]', b'"text"', b'7', b'null', b'{"a": NaN}', b'{"a": Infinity}', b'\xff\xfe',
                    b'{"a":1} trailing'):
            with self.subTest(raw=raw):
                self.error(self.c.json('POST', '/api/job/pause', raw=raw), 400, 'BAD_REQUEST')

    def test_a_write_without_content_length_is_refused(self):
        s = socket.create_connection(('127.0.0.1', self.server.port), timeout=10)
        self.addCleanup(s.close)
        s.sendall((f'POST /api/job/pause HTTP/1.0\r\nHost: 127.0.0.1:{self.server.port}\r\nX-Disk-Steward-Token: {self.c.token}\r\n'
                   f'Origin: http://127.0.0.1:{self.server.port}\r\nX-Disk-Steward: 1\r\n'
                   'Content-Type: application/json\r\n\r\n{}').encode())
        data = b''
        while chunk := s.recv(4096):
            data += chunk
        self.assertTrue(data.startswith(b'HTTP/1.0 411'), data[:60])

    def test_a_silent_connection_is_dropped_instead_of_holding_a_thread(self):
        from disk_steward.portal.server import _Handler
        with mock.patch.object(_Handler, 'timeout', 0.3):
            s = socket.create_connection(('127.0.0.1', self.server.port), timeout=10)
            self.addCleanup(s.close)
            started = time.monotonic()
            self.assertEqual(s.recv(10), b'')   # the server closed it
            self.assertLess(time.monotonic() - started, 5)
        self.ok(self.c.get('/api/state'))

    def test_unknown_routes_and_methods(self):
        self.error(self.c.get('/api/nope'), 404, 'NOT_FOUND')
        self.error(self.c.get('/nope'), 404, 'NOT_FOUND')
        self.error(self.c.get('/api/plans/abc/run'), 405, 'METHOD_NOT_ALLOWED')    # a POST-only route
        self.error(self.c.post('/api/state'), 405, 'METHOD_NOT_ALLOWED')
        for method in ('DELETE', 'PATCH', 'OPTIONS'):
            with self.subTest(method=method):
                status, headers, body = self.c.request(method, '/api/state')
                self.assertEqual(status, 405)
                self.assertEqual(json.loads(body)['error']['code'], 'METHOD_NOT_ALLOWED')
        self.assertEqual(self.c.request('HEAD', '/api/state')[0], 405)

    def test_the_internal_launch_code_endpoint_needs_the_secret_and_no_origin(self):
        c = Client(self.server)
        url = launch.request_launch_url(self.app.data_dir)
        self.assertRegex(url, rf'^http://127\.0\.0\.1:{self.server.port}/launch\?code=[\w-]{{32}}$')
        self.assertEqual(Client(self.server).post('/api/session', {'code': url.split('code=')[1]}, token=False)[0], 200)
        secret = self.server.launch_secret
        for headers in ({}, {launch.SECRET_HEADER: 'wrong'}, {launch.SECRET_HEADER: secret[:-1]}, {launch.SECRET_HEADER: secret + 'x'},
                        {launch.SECRET_HEADER: ''}):
            with self.subTest(headers=headers):
                status, _, body = c.request('POST', '/internal/launch-code', raw=b'{}', headers={'Content-Type': None, **headers},
                                            origin=False, token=False)
                self.assertEqual(status, 403)
                self.assertEqual(json.loads(body)['error']['message_key'], 'portal.error.bad_secret')
        # a browser names an Origin; the endpoint is not for browsers, secret or not
        status, _, _ = c.request('POST', '/internal/launch-code', raw=b'{}', headers={launch.SECRET_HEADER: secret,
                                 'Origin': f'http://127.0.0.1:{self.server.port}'}, origin=False, token=False)
        self.assertEqual(status, 403)


class RobustnessTests(PortalCase):
    def test_a_malformed_accept_language_never_crashes_anything(self):
        for header in ('en;q=1..5', 'zh-CN;q=abc,en', ';q=', 'zh;q=.', 'xx;q=1e5', '*;q=0.5,,,', ',' * 300, 'zh-CN;q=0.9;q=x',
                       'é', 'en;q=1..5, zh-CN;q=0.8', 'q=1', ';', 'zh-CN;q=.5.5'):
            with self.subTest(header=header):
                status, body = self.c.get('/api/state', headers={'Accept-Language': header})
                self.assertEqual(status, 200)
                self.assertIn(body['locale'], ('en', 'zh-CN'))
                self.error(self.c.get('/api/overview', headers={'Accept-Language': header}), 409, 'NO_SCAN')
                self.assertEqual(Client(self.server).get('/api/state', token=False, headers={'Accept-Language': header})[0], 401)
        # the tag whose weight does not parse is skipped, the rest still counts
        self.assertEqual(self.c.get('/api/state', headers={'Accept-Language': 'en;q=1..5, zh-CN;q=0.8'})[1]['locale'], 'zh-CN')
        self.assertEqual(i18n.detect_locale(None, 'en;q=1..5'), 'en')
        self.assertEqual(i18n.detect_locale(None, 'zh;q=x,en;q=0.1'), 'en')

    def test_the_error_paths_do_not_depend_on_locale_detection(self):
        err_out = io.StringIO()
        with mock.patch.object(PortalServer, 'locale_for', side_effect=RuntimeError('detection broke')), \
                contextlib.redirect_stderr(err_out):
            status, headers, body = self.c.request('GET', '/api/state')
            self.assertEqual(status, 500)
            err = json.loads(body)['error']
            self.assertEqual((err['code'], err['message']), ('INTERNAL', i18n.t('portal.error.internal', 'en')))
            self.assertEqual(headers['content-security-policy'], CSP)
            s = socket.create_connection(('127.0.0.1', self.server.port), timeout=10)   # a request line the parser refuses
            self.addCleanup(s.close)
            s.sendall(b'GET /' + b'a' * 70000 + b' HTTP/1.0\r\n\r\n')
            data = b''
            while chunk := s.recv(4096):
                data += chunk
        self.assertTrue(data.startswith(b'HTTP/1.0 414'), data[:40])
        self.assertEqual(json.loads(data.split(b'\r\n\r\n', 1)[1])['error']['code'], 'BAD_REQUEST')
        self.assertIn('detection broke', err_out.getvalue())
        self.ok(self.c.get('/api/state'))

    def test_deeply_nested_json_is_a_400_not_a_crash(self):
        # (Python 3.14 parses 100,000 nested objects without recursing; the request is then an ordinary one)
        for deep, answers in ((b'{"a":' + b'[' * 200000 + b']' * 200000 + b'}', (400,)),
                              (b'{"a":' * 100000 + b'1' + b'}' * 100000, (400, 404)), (b'[' * 500000, (400,))):
            with self.subTest(size=len(deep)):
                self.assertLessEqual(len(deep), MAX_BODY)
                status, body = self.c.json('POST', '/api/job/pause', raw=deep)
                self.assertIn(status, answers, body)
                self.assertIn(self.c.json('POST', '/api/session', raw=deep, token=False)[0], (400, 403))
        self.ok(self.c.get('/api/state'))

    def test_requests_beyond_the_thread_cap_are_answered_503_and_the_slots_come_back(self):
        server = PortalServer(self.app, 0, max_threads=3)
        server.start()
        self.addCleanup(server.stop)
        c = Client(server).login()
        since = c.get('/api/state')[1]['version']
        answers = []
        pollers = [threading.Thread(target=lambda: answers.append(c.get(f'/api/events?since={since}&timeout=20')))
                   for _ in range(3)]
        for t in pollers:
            t.start()
        self.wait_for(lambda: server._slots._value == 0)   # three long polls hold every slot
        status, headers, body = c.request('GET', '/api/state')
        self.assertEqual(status, 503)
        err = json.loads(body)['error']
        self.assertEqual((err['code'], err['message_key']), ('UNAVAILABLE', 'portal.error.too_busy'))
        self.assertEqual(headers['content-security-policy'], CSP)
        self.assertEqual(headers['retry-after'], '1')
        self.assertEqual(Client(server).request('GET', '/static/app.mjs', token=False)[0], 503)   # all kinds of requests
        self.app.events.publish('wake.up')
        for t in pollers:
            t.join(30)
        self.assertEqual([a[0] for a in answers], [200, 200, 200])
        self.wait_for(lambda: server._slots._value == 3)
        self.assertEqual(c.get('/api/state')[0], 200)

    def test_a_refused_request_does_not_leak_a_slot(self):
        server = PortalServer(self.app, 0, max_threads=2)
        server.start()
        self.addCleanup(server.stop)
        c = Client(server).login()
        for _ in range(20):
            self.assertEqual(c.get('/api/state')[0], 200)
        self.wait_for(lambda: server._slots._value == 2)


# -- headers -------------------------------------------------------------------------------------------------------

class HeaderTests(PortalCase):
    def test_security_headers_are_on_every_kind_of_response(self):
        anon = Client(self.server)
        bad_host = Client(self.server, host='evil.example.com')
        cases = [
            ('html', self.c.request('GET', '/')), ('locked page', anon.request('GET', '/static/locked.html', token=False)),
            ('launch page', anon.request('GET', '/launch?code=x', token=False)),
            ('js', self.c.request('GET', '/static/app.mjs')), ('module in a folder', self.c.request('GET', '/static/views/browse.mjs')),
            ('css', self.c.request('GET', '/static/styles.css')), ('json', self.c.request('GET', '/api/state')),
            ('401', anon.request('GET', '/api/state', token=False)), ('404', self.c.request('GET', '/api/nope')),
            ('static 404', self.c.request('GET', '/static/nope.js')), ('405', self.c.request('POST', '/api/state')),
            ('403', self.c.request('POST', '/api/job/pause', origin=False)), ('reset', self.c.request('GET', '/api/events?since=99999')),
            ('421', bad_host.request('GET', '/api/state')), ('session', Client(self.server).request('POST', '/api/session', {'code': 'x'}, token=False)),
            ('i18n', self.c.request('GET', '/api/i18n/en')),
        ]
        for name, (status, headers, _) in cases:
            with self.subTest(name=name, status=status):
                self.assertEqual(headers['content-security-policy'], CSP)
                self.assertEqual(headers['x-content-type-options'], 'nosniff')
                self.assertEqual(headers['referrer-policy'], 'no-referrer')
        self.assertEqual(CSP, "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                              "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

    def test_api_responses_are_never_cached_and_content_types_are_right(self):
        for path in ('/api/state', '/api/i18n/en', '/api/nope'):
            status, headers, _ = self.c.request('GET', path)
            self.assertEqual(headers['cache-control'], 'no-store')
            self.assertEqual(headers['content-type'], 'application/json; charset=utf-8')
        self.assertEqual(self.c.request('GET', '/static/app.mjs')[1]['content-type'], 'text/javascript; charset=utf-8')
        self.assertEqual(self.c.request('GET', '/static/styles.css')[1]['content-type'], 'text/css; charset=utf-8')
        self.assertEqual(self.c.request('GET', '/')[1]['cache-control'], 'no-store')


# -- static files --------------------------------------------------------------------------------------------------

class StaticTests(PortalCase):
    def test_every_packaged_asset_is_served(self):
        static = SRC / 'disk_steward' / 'portal' / 'static'
        files = sorted(p.relative_to(static).as_posix() for p in static.rglob('*')
                       if p.is_file() and p.suffix in ('.html', '.css', '.mjs'))
        self.assertIn('views/browse.mjs', files)
        for rel in files:
            with self.subTest(rel=rel):
                status, _, body = self.c.request('GET', f'/static/{rel}')
                self.assertEqual(status, 200)
                self.assertEqual(body, (static / rel).read_bytes())

    def test_path_traversal_is_a_404(self):
        attempts = ['/static/../server.py', '/static/%2e%2e/server.py', '/static/%2E%2E/server.py', '/static/%2e%2e%2fserver.py',
                    '/static/..%2fserver.py', '/static/views/../../server.py', '/static/views/%2e%2e/%2e%2e/server.py',
                    '/static/%252e%252e/server.py', '/static//etc/passwd', '/static/%2fetc%2fpasswd', '/static/views/',
                    '/static/views', '/static/', '/static', '/static/..\\server.py', '/static/%5c..%5cserver.py',
                    '/static/.hidden.js', '/static/app.mjs%00.png', '/static/app.mjs/', '/static/app.mjs/../../api.py',
                    '/static/index.html/../../api.py', '/static/./app.mjs', '/static/views/./browse.mjs', '/static/styles.css.map',
                    '/static/app.mjs?x=../../api.py', '/static/%c0%ae%c0%ae/server.py', '/static/....//server.py',
                    '/static/views//browse.mjs', '/static/file:///etc/passwd', '/static/C:/x.js']
        for path in attempts:
            with self.subTest(path=path):
                status, _, body = self.c.request('GET', path)
                if path == '/static/app.mjs?x=../../api.py':
                    self.assertEqual(status, 200)   # a query string is not a path
                    continue
                self.assertEqual(status, 404, path)
                self.assertNotIn(b'import ', body)
                self.assertEqual(json.loads(body)['error']['code'], 'NOT_FOUND')

    def test_only_known_file_types_are_served(self):
        for path in ('/static/app.py', '/static/portal.json', '/static/__init__.py', '/static/app.mjs.bak'):
            self.assertEqual(self.c.request('GET', path)[0], 404, path)


# -- errors --------------------------------------------------------------------------------------------------------

class ErrorMappingTests(PortalCase):
    def test_every_error_code_has_a_status(self):
        self.assertEqual(set(api_mod.STATUS), set(ErrorCode))

    def test_envelope_status_and_translated_message(self):
        err = self.error(self.c.get('/api/overview'), 409, 'NO_SCAN')
        self.assertEqual((err['message_key'], err['message']), ('error.NO_SCAN', i18n.t('error.NO_SCAN', 'en')))
        self.scan()
        err = self.error(self.c.post('/api/spec/ops', {'ops': [{'op': 'frobnicate'}]}), 400, 'INVALID')
        self.assertEqual(err['message_key'], 'error.invalid_op')
        self.assertEqual(err['details'], {'field': 'op'})
        self.assertEqual(err['message'], i18n.t('error.invalid_op', 'en', field='op'))
        err = self.error(self.c.get('/api/plans/nope'), 404, 'NOT_FOUND')
        self.assertEqual(err['details'], {'what': 'plan', 'id': 'nope'})
        self.error(self.c.get('/api/items?group=nope'), 404, 'NOT_FOUND')
        self.error(self.c.get('/api/items?limit=0'), 400, 'INVALID')
        self.error(self.c.get('/api/items?order=sideways'), 400, 'INVALID')

    def test_the_message_follows_the_language_of_the_request(self):
        zh = {'Accept-Language': 'zh-CN,zh;q=0.9'}
        err = self.error(self.c.get('/api/overview', headers=zh), 409, 'NO_SCAN')
        self.assertEqual(err['message'], i18n.t('error.NO_SCAN', 'zh-CN'))
        self.assertEqual(self.c.get('/api/state', headers=zh)[1]['locale'], 'zh-CN')
        self.assertEqual(self.c.get('/api/state', headers={'Accept-Language': 'en-US'})[1]['locale'], 'en')
        # a language chosen in the settings beats whatever the browser asks for
        self.ok(self.c.put('/api/settings', {'locale': 'en'}))
        self.assertEqual(self.c.get('/api/state', headers=zh)[1]['locale'], 'en')
        self.assertEqual(self.error(self.c.get('/api/overview', headers=zh), 409, 'NO_SCAN')['message'], i18n.t('error.NO_SCAN', 'en'))

    def test_digest_mismatch_is_a_409_with_what_changed(self):
        self.scan()
        sel = self.select()
        expected = {'scan_id': sel['scan_id'], 'spec_version': sel['spec']['version'], 'digest': sel['digest'],
                    'resolved_at': sel['resolved_at']}
        self.fh.clock.advance(days=10)   # files that were too recent have come of age
        err = self.error(self.c.post('/api/plans', {'expected': expected}), 409, 'DIGEST_MISMATCH')
        d = err['details']
        self.assertGreater(d['added_count'], 0)
        self.assertEqual(d['removed_count'], 0)
        self.assertEqual(len(d['added']), d['added_count'])
        self.assertEqual(set(d['added'][0]), {'path', 'action', 'alloc'})
        self.assertIn(LOG, [a['path'] for a in d['added']])
        self.assertEqual(err['message'], i18n.t('error.DIGEST_MISMATCH', 'en'))

    def test_a_digest_mismatch_without_a_timestamp_has_empty_lists(self):
        self.scan()
        sel = self.select()
        err = self.error(self.c.post('/api/plans', {'expected': {'scan_id': sel['scan_id'], 'spec_version': sel['spec']['version'],
                                                                  'digest': 'x' * 64}}), 409, 'DIGEST_MISMATCH')
        self.assertEqual((err['details']['added'], err['details']['added_count']), ([], None))

    def test_an_empty_plan_is_a_422_with_the_breakdown(self):
        self.scan()
        sel = self.select([{'op': 'select_tier', 'tier': 'junk', 'on': True}, {'op': 'set_filter', 'min_age_days': 3650}])
        self.assertEqual(sel['summary']['count'], 0)
        err = self.error(self.c.post('/api/plans', {'expected': {'scan_id': sel['scan_id'], 'spec_version': sel['spec']['version'],
                                                                  'digest': sel['digest']}}), 422, 'EMPTY_PLAN')
        self.assertIn('too_recent', err['details']['breakdown'])
        self.assertEqual(set(err['details']['breakdown']['too_recent']), {'count', 'alloc'})

    def test_a_selection_carried_to_a_newer_scan_is_refused_with_the_notes(self):
        self.scan()
        sel = self.select()
        self.app.scan()   # another client rescanned: the selection moved to scan 2
        err = self.error(self.c.post('/api/plans', {'expected': {'scan_id': sel['scan_id'], 'spec_version': sel['spec']['version'],
                                                                  'digest': sel['digest']}}), 400, 'INVALID')
        self.assertEqual(err['message_key'], 'error.spec_stale')
        notes = err['details']['notes']
        self.assertEqual((notes['from_scan'], notes['to_scan']), (1, 2))
        self.assertTrue(notes['group_changes'])
        for change in notes['group_changes']:
            self.assertTrue(err['details']['labels'][change['group']])
        banner = self.ok(self.c.get('/api/spec/notes'))
        self.assertEqual(banner['notes'], notes)
        self.assertEqual(banner['labels'], err['details']['labels'])
        self.select([{'op': 'clear'}])   # editing the selection is what clears the notes
        self.assertIsNone(self.ok(self.c.get('/api/spec/notes'))['notes'])

    def test_revision_expiry_and_rules_changes_are_409s(self):
        self.scan()
        plan = self.make_plan()
        err = self.error(self.c.post(f'/api/plans/{plan["id"]}/exclude', {'seqs': [1], 'revision': 99}), 409, 'REVISION_MISMATCH')
        self.assertEqual(err['details'], {'current': 1, 'given': 99})
        self.error(self.c.post(f'/api/plans/{plan["id"]}/run', {'revision': 99, 'entries_digest': plan['entries_digest']}),
                   409, 'REVISION_MISMATCH')
        self.error(self.c.post(f'/api/plans/{plan["id"]}/run', {'revision': 1, 'entries_digest': 'x'}), 409, 'REVISION_MISMATCH')
        self.assertIsNone(self.ok(self.c.get('/api/job'))['job']['plan_id'])   # a refusal starts nothing
        self.app.update_settings({'idle_days': 7})
        self.error(self.c.post(f'/api/plans/{plan["id"]}/run', {'revision': 1, 'entries_digest': plan['entries_digest']}),
                   409, 'RULES_CHANGED')
        self.app.update_settings({'idle_days': 180})
        self.fh.clock.advance(days=2)
        self.error(self.c.post(f'/api/plans/{plan["id"]}/run', {'revision': 1, 'entries_digest': plan['entries_digest']}),
                   409, 'PLAN_EXPIRED')
        self.assertIsNone(self.ok(self.c.get('/api/job'))['job']['plan_id'])

    def test_bad_fields_are_400s_naming_the_field(self):
        self.scan()
        plan = self.make_plan()
        for path, body, field in ((f'/api/plans/{plan["id"]}/exclude', {'seqs': 'all', 'revision': 1}, 'seqs'),
                                  (f'/api/plans/{plan["id"]}/exclude', {'seqs': [True], 'revision': 1}, 'seqs'),
                                  (f'/api/plans/{plan["id"]}/exclude', {'seqs': [1]}, 'revision'),
                                  (f'/api/plans/{plan["id"]}/run', {'revision': '1', 'entries_digest': 'x'}, 'revision'),
                                  (f'/api/plans/{plan["id"]}/run', {'revision': 1}, 'entries_digest'),
                                  ('/api/scan', {'domains': 'agent'}, 'domains')):
            with self.subTest(path=path, body=body):
                err = self.error(self.c.post(path, body), 400, 'INVALID')
                self.assertEqual(err['details'], {'field': field})
        self.error(self.c.post('/api/scan', {'domains': ['nowhere']}), 400, 'INVALID')
        self.error(self.c.post('/api/plans', {'expected': 'soon'}), 400, 'INVALID')
        self.error(self.c.post('/api/spec/ops', {'ops': 'clear'}), 400, 'INVALID')

    def test_an_unexpected_exception_is_a_500_without_a_traceback_for_the_client(self):
        secret = 'secret-detail-/Users/someone/private'
        err_out = io.StringIO()
        with mock.patch.object(self.app, 'overview', side_effect=RuntimeError(secret)), contextlib.redirect_stderr(err_out):
            status, headers, body = self.c.request('GET', '/api/overview')
        self.assertEqual(status, 500)
        err = json.loads(body)['error']
        self.assertEqual((err['code'], err['message_key']), ('INTERNAL', 'portal.error.internal'))
        self.assertNotIn(secret, body.decode())
        self.assertNotIn('Traceback', body.decode())
        self.assertEqual(err['details'], {})
        self.assertEqual(headers['content-security-policy'], CSP)
        self.assertIn('RuntimeError', err_out.getvalue())     # the operator sees it
        self.assertIn(secret, err_out.getvalue())
        self.assertIn('Traceback', err_out.getvalue())
        self.ok(self.c.get('/api/state'))                     # and the server carries on

    def test_a_scan_started_during_a_scan_is_a_423(self):
        gate = threading.Event()
        original = self.app.scan

        def slow_scan(*a, **kw):
            gate.wait(30)
            return original(*a, **kw)
        with mock.patch.object(self.app, 'scan', side_effect=slow_scan):
            self.ok(self.c.post('/api/scan'))
            self.error(self.c.post('/api/scan'), 423, 'BUSY')
            self.assertEqual(self.ok(self.c.get('/api/job'))['job']['kind'], 'scan')
            gate.set()
            self.wait_for(lambda: self.c.get('/api/job')[1]['job']['status'] == 'done')

    def test_unavailable_advisor_is_a_503(self):
        class NoAdvisor(App):
            @property
            def advisor(self):
                raise ImportError('advisor.service is not built yet')
        with FakeHome(Clock()) as fh:
            app = NoAdvisor(home=fh.home, data_dir=fh.data_dir, mac=make_mac(), clock=fh.clock, locale='en',
                            settings=Settings(domains=('agent',)), bases=fh.bases)
            server = PortalServer(app, 0)
            server.start()
            try:
                c = Client(server).login()
                for method, path in (('GET', '/api/advisor'), ('POST', '/api/advisor/preview'), ('POST', '/api/advisor/enable')):
                    err = self.error(c.json(method, path, {'enabled': True} if method == 'POST' else None), 503, 'UNAVAILABLE')
                    self.assertEqual(err['message_key'], 'portal.error.advisor_missing')
                    self.assertIn('ImportError', err['details']['reason'])
            finally:
                server.stop()
                app.close()


# -- long poll -----------------------------------------------------------------------------------------------------

class LongPollTests(PortalCase):
    def test_returns_at_once_for_a_published_event(self):
        since = self.ok(self.c.get('/api/state'))['version']
        threading.Timer(0.3, self.app.events.publish, ('test.ping',), {'n': 1}).start()
        started = time.monotonic()
        body = self.ok(self.c.get(f'/api/events?since={since}&timeout=20'))
        self.assertLess(time.monotonic() - started, 10)
        self.assertGreaterEqual(time.monotonic() - started, 0.25)
        self.assertEqual([e['type'] for e in body['events']], ['test.ping'])
        self.assertEqual(body['events'][0]['data'], {'n': 1})
        self.assertEqual(body['version'], since + 1)
        self.assertFalse(body['reset'])

    def test_returns_empty_on_timeout_and_keeps_the_version(self):
        since = self.ok(self.c.get('/api/state'))['version']
        started = time.monotonic()
        body = self.ok(self.c.get(f'/api/events?since={since}&timeout=0.4'))
        self.assertGreaterEqual(time.monotonic() - started, 0.35)
        self.assertEqual((body['events'], body['version']), ([], since))

    def test_events_already_there_come_back_without_waiting(self):
        self.app.events.publish('a')
        self.app.events.publish('b')
        started = time.monotonic()
        body = self.ok(self.c.get('/api/events?since=0&timeout=20'))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual([e['type'] for e in body['events']], ['a', 'b'])
        self.assertEqual(self.ok(self.c.get('/api/events?since=1&timeout=0'))['events'][0]['type'], 'b')

    def test_a_counter_from_another_server_run_is_a_reset(self):
        body = self.ok(self.c.get('/api/events?since=999999&timeout=20'))
        self.assertEqual((body['events'], body['reset']), ([], True))
        self.assertEqual(body['version'], self.app.events.version)

    def test_events_the_log_dropped_are_a_reset(self):
        for i in range(self.app.events._events.maxlen + 20):
            self.app.events.publish('x', i=i)
        self.assertTrue(self.ok(self.c.get('/api/events?since=0&timeout=0'))['reset'])

    def test_bad_parameters(self):
        for query in ('since=-1', 'since=abc', 'since=1.5', 'timeout=soon', 'timeout=nan', 'timeout=NaN', 'timeout=inf',
                      'timeout=-inf', 'timeout=1e999', 'timeout=-0.1', 'timeout=25.01', 'timeout=26', 'timeout=1000', 'timeout='):
            with self.subTest(query=query):
                err = self.error(self.c.get(f'/api/events?{query}'), 400, 'INVALID')
                self.assertEqual(err['details']['field'], query.split('=')[0])
        for query in ('timeout=0', 'timeout=0.0', 'timeout=25', 'timeout=1e-3'):   # the edges are fine
            self.ok(self.c.get(f'/api/events?since=999999&{query}'))


# -- portal.json and the command line ------------------------------------------------------------------------------

class PortalJsonTests(unittest.TestCase):
    def setUp(self):
        self.fh = FakeHome(Clock()).__enter__()
        self.addCleanup(self.fh.cleanup)
        self.app = make_portal_app(self.fh)
        self.addCleanup(self.app.close)
        self.path = self.fh.data_dir / 'portal.json'

    def test_lifecycle(self):
        server = PortalServer(self.app, 0)
        self.assertFalse(self.path.exists())
        server.start()
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        info = json.loads(self.path.read_text())
        self.assertEqual(set(info), {'port', 'pid', 'started_at', 'launch_secret'})
        self.assertEqual((info['port'], info['pid']), (server.port, os.getpid()))
        self.assertEqual(info['launch_secret'], server.launch_secret)
        self.assertEqual([p.name for p in self.fh.data_dir.glob('.portal.*')], [])   # no temp file left behind
        server.stop()
        self.assertFalse(self.path.exists())

    def test_the_server_listens_on_loopback_only(self):
        server = PortalServer(self.app, 0)
        self.addCleanup(server.server_close)
        self.assertEqual(server.server_address[0], '127.0.0.1')

    def test_a_fixed_port(self):
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        server = PortalServer(self.app, port)
        server.start()
        self.addCleanup(server.stop)
        self.assertEqual(json.loads(self.path.read_text())['port'], port)

    def test_a_stale_file_is_replaced_but_a_live_portal_is_respected(self):
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait()
        launch.write_info(self.fh.data_dir, {'port': 1, 'pid': dead.pid, 'started_at': 0, 'launch_secret': 'old'})
        server = PortalServer(self.app, 0)
        server.start()
        self.assertEqual(json.loads(self.path.read_text())['pid'], os.getpid())
        server.stop()
        alive = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(lambda: (alive.kill(), alive.wait()))
        launch.write_info(self.fh.data_dir, {'port': 2, 'pid': alive.pid, 'started_at': 0, 'launch_secret': 'theirs'})
        second = PortalServer(self.app, 0)
        self.addCleanup(second.server_close)
        with self.assertRaises(StewardError) as cm:
            second.start()
        self.assertEqual((cm.exception.code, cm.exception.message_key), (ErrorCode.BUSY, 'portal.cli.already_running'))
        self.assertEqual(json.loads(self.path.read_text())['launch_secret'], 'theirs')   # untouched
        second.stop()
        self.assertEqual(json.loads(self.path.read_text())['launch_secret'], 'theirs')   # and not removed by the loser

    def test_url_asks_the_running_portal_for_a_fresh_code(self):
        server = PortalServer(self.app, 0)
        server.start()
        self.addCleanup(server.stop)
        first = launch.request_launch_url(self.fh.data_dir)
        second = launch.request_launch_url(self.fh.data_dir)
        self.assertNotEqual(first, second)
        c = Client(server)
        for url in (first, second):
            status, headers, _ = c.request('POST', '/api/session', {'code': url.split('code=')[1]}, token=False)
            self.assertEqual(status, 200)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli_main(['portal', '--url'], app_factory=lambda: self.app)
        self.assertEqual(code, 0)
        self.assertEqual(Client(server).post('/api/session', {'code': out.getvalue().strip().split('code=')[1]}, token=False)[0], 200)

    def test_url_prints_nothing_that_is_not_a_launch_link_of_that_port(self):
        server = PortalServer(self.app, 0)
        server.start()
        self.addCleanup(server.stop)
        port = server.port
        for bad in ('http://evil.example.com/launch?code=abc', f'http://127.0.0.1:{port}/other?code=abc',
                    f'http://127.0.0.1:{port + 1}/launch?code=abc', f'http://127.0.0.1:{port}/launch?code=abc&x=1',
                    f'http://127.0.0.1:{port}/launch?code=', f'http://127.0.0.1:{port}/launch', 'javascript:alert(1)',
                    f'http://127.0.0.1:{port}/launch?code=a b', f'http://127.0.0.1:{port}/launch?code=abc\n',
                    f'http://127.0.0.1:{port}/launch?code=abc\x1b[31m', f'https://127.0.0.1:{port}/launch?code=abc',
                    f'http://localhost:{port}/launch?code=abc', f' http://127.0.0.1:{port}/launch?code=abc', 7, None, ['x']):
            with self.subTest(url=bad), mock.patch.object(server, 'new_launch_url', return_value=bad):
                with self.assertRaises(StewardError) as cm:
                    launch.request_launch_url(self.fh.data_dir)
                self.assertEqual((cm.exception.code, cm.exception.message_key), (ErrorCode.UNAVAILABLE, 'portal.cli.bad_url'))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(server, 'new_launch_url', return_value='http://evil.example.com/'), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli_main(['portal', '--url'], app_factory=lambda: self.app), 2)
        self.assertEqual(out.getvalue(), '')
        self.assertIn('Nothing was printed', err.getvalue())
        self.assertRegex(launch.request_launch_url(self.fh.data_dir), r'^http://127\.0\.0\.1:\d+/launch\?code=[\w-]+$')

    def test_url_refuses_a_stranger_listening_on_the_port(self):
        import http.server

        class Stranger(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = b'<html>hello</html>'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        stranger = http.server.HTTPServer(('127.0.0.1', 0), Stranger)
        t = threading.Thread(target=stranger.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        t.start()
        self.addCleanup(lambda: (stranger.shutdown(), stranger.server_close(), t.join()))
        launch.write_info(self.fh.data_dir, {'port': stranger.server_address[1], 'pid': os.getpid(), 'started_at': 0,
                                             'launch_secret': 'whatever'})
        with self.assertRaises(StewardError) as cm:
            launch.request_launch_url(self.fh.data_dir)
        self.assertEqual(cm.exception.message_key, 'portal.cli.bad_url')
        os.unlink(self.path)

    def test_url_without_a_running_portal_says_so(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli_main(['portal', '--url'], app_factory=lambda: self.app)
        self.assertEqual(code, 2)
        self.assertIn('No portal is running', err.getvalue())
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait()
        launch.write_info(self.fh.data_dir, {'port': 1, 'pid': dead.pid, 'started_at': 0, 'launch_secret': 'old'})
        with self.assertRaises(StewardError) as cm:
            launch.request_launch_url(self.fh.data_dir)
        self.assertEqual(cm.exception.message_key, 'portal.cli.not_running')
        self.path.write_text('not json')
        with self.assertRaises(StewardError) as cm:
            launch.request_launch_url(self.fh.data_dir)
        self.assertEqual(cm.exception.message_key, 'portal.cli.portal_json_bad')

    def test_the_command_passes_its_options_to_serve(self):
        out = io.StringIO()
        seen = {}

        def fake_serve(app, port=0, open_browser=False, announce=None, **kw):
            seen.update(port=port, open_browser=open_browser, **kw)
            announce('http://127.0.0.1:1/launch?code=abc')
            return 0
        with mock.patch('disk_steward.portal.server.serve', fake_serve), contextlib.redirect_stdout(out):
            code = cli_main(['portal', '--open', '--port', '8123', '--lang', 'zh-CN'], app_factory=lambda: self.app)
        self.assertEqual(code, 0)
        self.assertEqual(seen, {'port': 8123, 'open_browser': True, 'default_locale': 'zh-CN'})
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[1], 'http://127.0.0.1:1/launch?code=abc')   # the URL stands on its own line
        self.assertIn('门户', lines[0])
        for bad in (['--port', '70000'], ['--port', '-1'], ['--port', 'x']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli_main(['portal', *bad], app_factory=lambda: self.app)

    def test_serve_opens_the_browser_only_when_asked_and_stops_on_a_signal(self):
        for want in (False, True):
            urls, opened = [], []
            threading.Timer(1.0, os.kill, (os.getpid(), signal.SIGINT)).start()
            code = serve(self.app, 0, open_browser=want, announce=urls.append, opener=opened.append)
            self.assertEqual(code, 0)
            self.assertEqual(len(urls), 1)
            self.assertRegex(urls[0], r'^http://127\.0\.0\.1:\d+/launch\?code=')
            self.assertEqual(opened, urls if want else [])
            self.assertFalse(self.path.exists())
        # a failing `open` is reported and the portal still serves
        err = io.StringIO()

        def failing(url):
            raise subprocess.CalledProcessError(1, 'open')
        threading.Timer(1.0, os.kill, (os.getpid(), signal.SIGINT)).start()
        with contextlib.redirect_stderr(err):
            self.assertEqual(serve(self.app, 0, open_browser=True, announce=lambda u: None, opener=failing), 0)
        self.assertIn('Could not open the browser', err.getvalue())

    def test_serve_refuses_a_port_that_is_taken(self):
        busy = socket.socket()
        busy.bind(('127.0.0.1', 0))
        busy.listen()
        self.addCleanup(busy.close)
        with self.assertRaises(StewardError) as cm:
            serve(self.app, busy.getsockname()[1], announce=lambda u: None)
        self.assertEqual((cm.exception.code, cm.exception.message_key), (ErrorCode.UNAVAILABLE, 'portal.cli.bind_failed'))

    def test_sigterm_and_sigint_stop_a_real_portal_cleanly(self):
        script = (
            'import json, sys\n'
            f'sys.path[:0] = [{str(HERE)!r}, {str(SRC)!r}]\n'
            'from support import FakeHome\n'
            'from m3_support import make_app\n'
            'from disk_steward.portal.server import serve\n'
            'fh = FakeHome().__enter__()\n'
            'print(json.dumps({"data_dir": str(fh.data_dir)}), flush=True)\n'
            'app = make_app(fh)\n'
            'code = serve(app, 0, announce=lambda url: print(url, flush=True))\n'
            'app.close()\n'
            'fh.cleanup()\n'
            'sys.exit(code)\n')
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=sig.name):
                proc = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    data_dir = Path(json.loads(proc.stdout.readline())['data_dir'])
                    url = proc.stdout.readline().strip()
                    self.assertRegex(url, r'^http://127\.0\.0\.1:\d+/launch\?code=')
                    self.assertEqual(os.stat(data_dir / 'portal.json').st_mode & 0o777, 0o600)
                    self.assertEqual(json.loads((data_dir / 'portal.json').read_text())['pid'], proc.pid)
                    proc.send_signal(sig)
                    self.assertEqual(proc.wait(timeout=30), 0, proc.stderr.read())
                    self.assertFalse((data_dir / 'portal.json').exists())
                finally:
                    if proc.poll() is None:
                        proc.kill()
                    proc.stdout.close()
                    proc.stderr.close()


# -- the whole API, end to end -------------------------------------------------------------------------------------

class GatedRunCase(PortalCase):
    """A run that stops before its first entry until the test lets it go."""

    def setUp(self):
        self.reached = threading.Event()
        self.gate = threading.Event()
        self.addCleanup(self.gate.set)

        def hook(entry):
            self.reached.set()
            self.gate.wait(30)
        self.app_kwargs = {'hooks': {'after_claim': hook}}
        super().setUp()


class SettingsDuringARunTests(GatedRunCase):
    def test_language_may_change_during_a_paused_run_but_classification_settings_may_not(self):
        self.scan()
        plan = self.make_plan()
        self.ok(self.c.post(f'/api/plans/{plan["id"]}/run', {'revision': plan['revision'], 'entries_digest': plan['entries_digest']}))
        self.assertTrue(self.reached.wait(30))
        self.ok(self.c.post('/api/job/pause'))
        self.gate.set()
        self.wait_for(lambda: self.c.get('/api/job')[1]['job']['status'] == 'paused')
        live = self.ok(self.c.get('/api/settings'))['live_fields']
        self.assertEqual(live, ['advisor_enabled', 'advisor_provider', 'default_min_age_days', 'locale'])
        body = self.ok(self.c.put('/api/settings', {'locale': 'zh-CN'}))
        self.assertEqual(body['settings']['locale'], 'zh-CN')
        self.assertEqual(self.c.get('/api/state')[1]['locale'], 'zh-CN')
        self.ok(self.c.put('/api/settings', {'default_min_age_days': 30, 'advisor_enabled': True}))
        err = self.error(self.c.put('/api/settings', {'idle_days': 10}), 423, 'BUSY')
        self.assertEqual(err['message'], i18n.t('error.busy', 'zh-CN'))
        self.error(self.c.put('/api/settings', {'domains': ['agent'], 'locale': 'en'}), 423, 'BUSY')
        self.assertEqual(self.c.get('/api/settings')[1]['settings']['locale'], 'zh-CN')   # the blocked patch changed nothing
        self.ok(self.c.post('/api/job/resume'))
        self.wait_for(lambda: self.c.get('/api/job')[1]['job']['status'] == 'done')
        self.assertEqual(self.c.get('/api/job')[1]['job']['plan_state'], 'done')
        self.ok(self.c.put('/api/settings', {'idle_days': 10}))   # allowed again once the run is over

    def test_run_and_scan_cannot_start_while_a_run_is_active(self):
        self.scan()
        plan = self.make_plan()
        body = {'revision': plan['revision'], 'entries_digest': plan['entries_digest']}
        self.ok(self.c.post(f'/api/plans/{plan["id"]}/run', body))
        self.assertTrue(self.reached.wait(30))
        self.error(self.c.post(f'/api/plans/{plan["id"]}/run', body), 423, 'IN_PROGRESS')
        self.error(self.c.post('/api/scan'), 423, 'BUSY')
        self.error(self.c.post('/api/archives/none/restore'), 423, 'BUSY')   # restore takes the operation lock too
        self.assertEqual(self.c.post('/api/job/cancel')[0], 200)
        self.gate.set()
        self.wait_for(lambda: self.c.get('/api/job')[1]['job']['status'] == 'done')
        self.assertEqual(self.c.get('/api/job')[1]['job']['plan_state'], 'cancelled')


class ApiFlowTests(PortalCase):
    def test_scan_select_plan_preflight_run_archive_and_advise(self):
        c = self.c
        # nothing before a scan; the state says so
        state = self.ok(c.get('/api/state'))
        self.assertIsNone(state['scan'])
        self.assertEqual(state['job']['status'], 'idle')
        self.assertEqual(state['locale'], 'en')
        self.assertEqual(state['settings']['domains'], ['agent'])

        # scan in the background, progress through events and the job
        since = state['version']
        job = self.ok(c.post('/api/scan', {}))['job']
        self.assertEqual((job['status'], job['kind']), ('running', 'scan'))
        self.wait_for(lambda: c.get('/api/state')[1]['scan'] is not None and c.get('/api/job')[1]['job']['status'] == 'done')
        types = [e['type'] for e in self.ok(c.get(f'/api/events?since={since}&timeout=0'))['events']]
        self.assertIn('scan.progress', types)
        self.assertEqual(types.count('scan.done'), 1)
        self.assertEqual(self.c.get('/api/job')[1]['job']['scan_id'], 1)

        overview = self.ok(c.get('/api/overview'))
        self.assertEqual(overview['scan']['id'], 1)
        self.assertEqual(overview['cloud_notes'], [])
        self.assertTrue(all(g['label'] and 'rule.' not in g['label'] for g in overview['top_groups']))
        report_only = overview['not_included']['report_only']['groups']
        self.assertEqual(report_only[0]['label'], 'Ollama models')
        self.assertTrue(report_only[0]['hint'])

        groups = self.ok(c.get('/api/groups'))['groups']
        by_key = {g['key']: g for g in groups}
        self.assertEqual(by_key['claude.logs']['label'], 'Claude logs')
        self.assertTrue(by_key['claude.logs']['effect'])
        self.assertEqual(by_key['claude.projects:~/.claude/projects/-Users-me-proj']['label'],
                         'Claude Code sessions (~/.claude/projects/-Users-me-proj)')
        self.assertEqual({g['key'] for g in self.ok(c.get('/api/groups?tier=junk'))['groups']},
                         {k for k, g in by_key.items() if g['tier'] == 'junk'})
        self.error(c.get('/api/groups?tier=gold'), 400, 'INVALID')

        # the selection lives on the server; decisions carry the server's reason
        sel = self.select()
        self.assertEqual(self.ok(c.get('/api/spec'))['digest'], sel['digest'])
        self.assertEqual(sel['summary']['count'], 3)
        self.assertEqual(sel['summary']['by_action']['delete']['count'], 3)
        warning = next(w for w in sel['summary']['warnings'] if w['key'] == 'warning.group_selected_none_qualify')
        self.assertIn(by_key[warning['group']]['label'], warning['text'])
        items = self.ok(c.get('/api/items?group=claude.logs'))
        row = items['rows'][0]
        self.assertEqual(row['path'], LOG)
        self.assertEqual(row['decision']['state'], 'too_recent')
        self.assertEqual(row['decision']['text'], 'Changed 5 days ago, which is newer than the 14-day threshold.')
        self.assertAlmostEqual(row['age_days'], 5, places=2)
        self.assertEqual(row['age_basis'], 'changed')
        for private in ('dev', 'ino', 'uid', 'flags', 'mtime_ns', 'scan_id'):
            self.assertNotIn(private, row)
        both = self.ok(c.get('/api/items?group=codex.sessions:duplicates'))['rows'][0]
        self.assertTrue(both['evidence_text'])
        paged = self.ok(c.get('/api/items?group=codex.state-tmp&limit=1&offset=1&order=path'))
        self.assertEqual((paged['total'], len(paged['rows'])), (2, 1))
        self.assertEqual(self.ok(c.get('/api/items?group=codex.state-tmp&q=bak'))['total'], 1)

        # the plan, its preview and the exclusion
        plan = self.make_plan()
        self.assertEqual(plan['state'], 'draft')
        self.assertEqual(plan['revision'], 1)
        detail = self.ok(c.get(f'/api/plans/{plan["id"]}'))
        self.assertEqual(detail['pending']['count'], 3)
        self.assertEqual(detail['pending']['by_action']['delete']['frees'], detail['pending']['alloc'])
        entries = self.ok(c.get(f'/api/plans/{plan["id"]}/entries'))
        self.assertEqual(entries['total'], 3)
        e0 = entries['rows'][0]
        for key in ('label', 'effect', 'age_days', 'age_basis', 'reason_text', 'path', 'action', 'alloc'):
            self.assertIn(key, e0)
        self.assertGreaterEqual(e0['age_days'], 14)
        self.assertEqual(self.ok(c.get(f'/api/plans/{plan["id"]}/entries?state=pending&limit=2'))['rows'][1]['seq'], 2)
        self.error(c.get(f'/api/plans/{plan["id"]}/entries?state=bogus'), 400, 'INVALID')
        pre = self.ok(c.get(f'/api/preflight/{plan["id"]}'))
        self.assertTrue(pre['ready'])
        self.assertEqual((pre['pending'], pre['blocker_groups']), (3, []))
        excluded_path = entries['rows'][-1]['path']
        out = self.ok(c.post(f'/api/plans/{plan["id"]}/exclude', {'seqs': [entries['rows'][-1]['seq']], 'revision': 1}))
        self.assertEqual(out, {'revision': 2, 'excluded': 1})
        detail = self.ok(c.get(f'/api/plans/{plan["id"]}'))
        self.assertEqual((detail['pending']['count'], detail['revision']), (2, 2))
        self.assertEqual(detail['entries']['by_state'], {'pending': 2, 'excluded': 1})

        # run it in the background; the event stream carries translated outcomes
        since = self.ok(c.get('/api/state'))['version']
        started = self.ok(c.post(f'/api/plans/{plan["id"]}/run', {'revision': 2, 'entries_digest': plan['entries_digest']}))['job']
        self.assertEqual(started['plan_id'], plan['id'])
        self.wait_for(lambda: c.get('/api/job')[1]['job']['status'] == 'done')
        job = self.ok(c.get('/api/job'))['job']
        self.assertEqual(job['summary']['counts'], {'done': 2, 'excluded': 1})
        self.assertEqual(job['summary']['skipped'], [])
        self.assertIn('rescan', job['summary'])
        events = self.ok(c.get(f'/api/events?since={since}&timeout=0'))['events']
        done = [e for e in events if e['type'] == 'entry.done']
        self.assertEqual(len(done), 2)
        self.assertTrue(all(e['data']['state'] == 'done' and e['data']['reason_text'] == '' for e in done))
        finished = next(e for e in events if e['type'] == 'run.finished')
        self.assertEqual(finished['data']['summary']['skipped'], [])
        for e in entries['rows'][:-1]:
            self.assertFalse(os.path.lexists(self.fh.path(e['path'])), e['path'])
        self.assertTrue(os.path.lexists(self.fh.path(excluded_path)))
        self.assertEqual(self.ok(c.get('/api/state'))['scan']['id'], 2)   # the post-run rescan

        # archive one session file and restore it; restore never overwrites
        session = '~/.claude/projects/-Users-me-proj/a.jsonl'
        original = self.fh.path(session).read_bytes()
        item = next(r for r in self.ok(c.get('/api/items?group=claude.projects:~/.claude/projects/-Users-me-proj&limit=10'))['rows']
                    if r['path'] == session)
        self.ok(c.post('/api/spec/ops', {'ops': [{'op': 'clear'}, {'op': 'toggle_item', 'item': item['key'], 'on': True}]}))
        plan2 = self.make_plan([{'op': 'set_filter', 'min_age_days': 14}])
        self.assertEqual(plan2['totals']['by_action']['archive']['count'], 1)
        self.ok(c.post(f'/api/plans/{plan2["id"]}/run', {'revision': 1, 'entries_digest': plan2['entries_digest']}))
        self.wait_for(lambda: c.get('/api/job')[1]['job']['status'] == 'done')
        self.assertFalse(os.path.lexists(self.fh.path(session)))
        archives = self.ok(c.get('/api/archives'))['archives']
        self.assertEqual(len(archives), 1)
        a = archives[0]
        self.assertEqual((a['path'], a['state'], a['unit']), (session, 'stored', 'file'))
        self.assertEqual(set(a), {'id', 'path', 'fmt', 'orig_size', 'stored_bytes', 'state', 'created_at', 'unit'})
        self.fh.path(session).write_text('in the way')
        err = self.error(c.post(f'/api/archives/{a["id"]}/restore'), 400, 'INVALID')
        self.assertEqual(err['message_key'], 'error.restore_exists')
        self.assertIn(session, err['message'])
        self.assertEqual(self.fh.path(session).read_text(), 'in the way')
        self.fh.path(session).unlink()
        restored = self.ok(c.post(f'/api/archives/{a["id"]}/restore'))
        self.assertEqual((restored['path'], restored['archive_id']), (session, a['id']))
        self.assertEqual(self.fh.path(session).read_bytes(), original)
        self.assertEqual(self.ok(c.get('/api/archives'))['archives'][0]['state'], 'restored')
        purged = self.ok(c.post(f'/api/archives/{a["id"]}/purge'))
        self.assertEqual(purged['archive_id'], a['id'])
        self.assertEqual(self.ok(c.get('/api/archives'))['archives'][0]['state'], 'purged')
        self.error(c.post('/api/archives/nope/restore'), 404, 'NOT_FOUND')

    def test_a_blocked_and_a_changed_entry_are_skipped_with_translated_reasons(self):
        self.mac.running.add(CLAUDE_APP)
        self.scan()
        plan = self.make_plan([{'op': 'select_tier', 'tier': 'junk', 'on': True}, {'op': 'set_filter', 'min_age_days': 0}])
        pre = self.ok(self.c.get(f'/api/preflight/{plan["id"]}'))
        quit_app = [b for b in pre['blocker_groups'] if b['kind'] == 'quit_app']
        self.assertEqual([b['reason'] for b in quit_app], [f'app_running:{CLAUDE_APP}'])
        self.assertRegex(quit_app[0]['text'], r'^Quit Claude Desktop to clean \d+ more items · \d+\.\d\d KiB$')
        self.assertTrue(pre['ready'])
        self.assertEqual(pre['warnings'], [])
        self.mac.snapshot_names = ['com.apple.TimeMachine.2026-10-28-100000.local']
        warned = self.ok(self.c.get(f'/api/preflight/{plan["id"]}'))['warnings']
        self.assertEqual([(w['key'], w['count']) for w in warned], [('warning.snapshots', 1)])
        self.assertIn('Removed space may not show as free', warned[0]['text'])
        with mock.patch.object(self.mac, 'snapshots', side_effect=RuntimeError('tmutil failed')):
            unknown = self.ok(self.c.get(f'/api/preflight/{plan["id"]}'))['warnings']
        self.assertEqual([w['key'] for w in unknown], ['warning.snapshots_unknown'])
        self.assertIn('tmutil failed', unknown[0]['text'])
        zh = self.ok(self.c.get(f'/api/preflight/{plan["id"]}', headers={'Accept-Language': 'zh-CN'}))
        self.assertIn(i18n.catalog('zh-CN')[f'app.{CLAUDE_APP}'], zh['blocker_groups'][0]['text'])
        self.assertNotIn('Quit', zh['blocker_groups'][0]['text'])
        with open(self.fh.path('~/.codex/log/codex-tui.log'), 'a') as f:
            f.write('changed after the plan')
        self.ok(self.c.post(f'/api/plans/{plan["id"]}/run', {'revision': 1, 'entries_digest': plan['entries_digest']}))
        self.wait_for(lambda: self.c.get('/api/job')[1]['job']['status'] == 'done')
        skipped = {s['reason'].partition(':')[0]: s for s in self.c.get('/api/job')[1]['job']['summary']['skipped']}
        self.assertEqual(skipped['skip.changed']['text'], 'Changed since the plan was made')
        self.assertEqual(skipped['skip.blocked']['text'], 'In use: Claude Desktop is running')
        self.assertEqual(skipped['skip.changed']['count'], 1)
        events = self.ok(self.c.get('/api/events?since=0&timeout=0'))['events']
        texts = {e['data']['reason_text'] for e in events if e['type'] == 'entry.done' and e['data']['state'] == 'skipped'}
        self.assertEqual(texts, {'Changed since the plan was made', 'In use: Claude Desktop is running'})
        self.assertTrue(os.path.lexists(self.fh.path('~/.codex/log/codex-tui.log')))

    def test_every_text_the_api_renders_exists_in_both_languages(self):
        for lang in ('en', 'zh-CN'):
            h = {'Accept-Language': lang}
            self.ok(self.c.get('/api/state', headers=h))
        self.scan()
        sel = self.select([{'op': 'select_tier', 'tier': 'junk', 'on': True}, {'op': 'set_filter', 'min_age_days': 0}])
        for lang in ('en', 'zh-CN'):
            h = {'Accept-Language': lang}
            self.ok(self.c.get('/api/overview', headers=h))
            for g in self.ok(self.c.get('/api/groups', headers=h))['groups']:
                self.ok(self.c.get(f'/api/items?group={quote(g["key"], safe="")}&limit=500', headers=h))
        self.assertEqual(i18n.MISSING, set())
        self.assertEqual(sel['summary']['count'] > 0, True)

    def test_applications_are_measured_on_request(self):
        apps = self.fh.root / 'Applications'
        for name in ('Big.app', 'Small.app'):
            (apps / name).mkdir(parents=True)
        self.mac.app_size_map = {str(apps / 'Big.app'): {'bytes': 5000}, str(apps / 'Small.app'): {'bytes': 7}}
        with mock.patch('disk_steward.service.SYSTEM_APPLICATIONS', apps):
            body = self.ok(self.c.get('/api/apps'))
        self.assertEqual([(e['path'], e['bytes']) for e in body['entries']], [(str(apps / 'Big.app'), 5000), (str(apps / 'Small.app'), 7)])
        self.assertEqual(body['total'], 5007)

    def test_i18n_catalog_endpoint_is_the_merged_catalog(self):
        for lang in ('en', 'zh-CN'):
            self.assertEqual(self.ok(self.c.get(f'/api/i18n/{lang}')), i18n.catalog(lang))
        self.assertIn('portal.nav.overview', self.ok(self.c.get('/api/i18n/en')))
        self.assertIn('tier.junk', self.ok(self.c.get('/api/i18n/zh-CN')))
        self.error(self.c.get('/api/i18n/fr'), 400, 'INVALID')

    def test_settings_roundtrip(self):
        body = self.ok(self.c.get('/api/settings'))
        self.assertEqual(body['settings']['default_min_age_days'], 14)
        out = self.ok(self.c.put('/api/settings', {'default_min_age_days': 21, 'idle_days': 90}))
        self.assertEqual((out['settings']['default_min_age_days'], out['settings']['idle_days']), (21, 90))
        self.assertEqual(json.loads((self.fh.data_dir / 'settings.json').read_text())['idle_days'], 90)
        err = self.error(self.c.put('/api/settings', {'idle_days': -1}), 400, 'INVALID')
        self.assertEqual(err['details'], {'field': 'idle_days'})
        self.error(self.c.put('/api/settings', {'no_such_setting': 1}), 400, 'INVALID')
        self.error(self.c.put('/api/settings', {'locale': 'fr'}), 400, 'INVALID')

    def test_include_anyway_selects_a_too_recent_item_of_a_selected_group(self):
        # What the "Include anyway" button sends: an explicit include yields to the rule floor only.
        self.scan()
        self.select([{'op': 'select_group', 'group': 'claude.logs', 'on': True}, {'op': 'set_filter', 'min_age_days': 14}])
        item = self.ok(self.c.get('/api/items?group=claude.logs'))['rows'][0]
        self.assertEqual(item['decision']['state'], 'too_recent')
        sel = self.ok(self.c.post('/api/spec/ops', {'ops': [{'op': 'toggle_item', 'item': item['key'], 'on': True, 'action': 'delete'}]}))
        self.assertEqual(sel['summary']['count'], 1)
        again = self.ok(self.c.get('/api/items?group=claude.logs'))['rows'][0]
        self.assertEqual((again['decision']['state'], again['decision']['via']), ('selected', 'explicit'))

    def test_the_advisor_routes_follow_the_contract_and_never_touch_the_selection(self):
        c = self.c
        self.scan()
        before = self.select()
        status = self.ok(c.get('/api/advisor'))
        self.assertEqual((status['enabled'], status['last']), (False, None))
        self.assertTrue(status['providers']['manual']['available'])
        self.error(c.post('/api/advisor/preview'), 503, 'UNAVAILABLE')            # disabled by default
        self.assertEqual(self.ok(c.post('/api/advisor/enable', {'enabled': True}))['enabled'], True)
        self.assertTrue(self.ok(c.get('/api/state'))['settings']['advisor_enabled'])
        self.error(c.post('/api/advisor/enable', {'enabled': 'yes'}), 400, 'INVALID')
        preview = self.ok(c.post('/api/advisor/preview'))
        self.assertEqual(set(preview), {'request_id', 'payload', 'bytes', 'coverage', 'expires_at', 'notice_key'})
        self.assertIn(preview['notice_key'], i18n.catalog('en'))   # the page renders it with t()
        since = self.ok(c.get('/api/state'))['version']
        self.assertEqual(self.ok(c.post('/api/advisor/run', {'provider': 'codex', 'request_id': preview['request_id']})),
                         {'started': True, 'request_id': preview['request_id'], 'provider': 'codex'})
        events = self.ok(c.get(f'/api/events?since={since}&timeout=10'))['events']
        deadline = time.monotonic() + 10
        types = [e['type'] for e in events]
        while 'advisor.done' not in types and time.monotonic() < deadline:
            since = events[-1]['version']
            events = self.ok(c.get(f'/api/events?since={since}&timeout=5'))['events']
            types += [e['type'] for e in events]
        self.assertIn('advisor.done', types)
        last = self.ok(c.get('/api/advisor'))['last']
        self.assertEqual(last['provider'], 'codex')
        self.assertTrue(last['recommendations'])
        self.assertEqual(last['traces'], ['~/.claude.json'])
        self.assertEqual(self.ok(c.post('/api/advisor/export', {'request_id': preview['request_id']}))['text'], f'PROMPT for {preview["request_id"]}')
        self.error(c.post('/api/advisor/export', {'request_id': 'nope'}), 404, 'NOT_FOUND')
        self.error(c.post('/api/advisor/import', {'request_id': preview['request_id'], 'text': 'not json'}), 400, 'INVALID')
        imported = self.ok(c.post('/api/advisor/import', {'request_id': preview['request_id'], 'text': '{"ok": true}'}))
        self.assertEqual(imported['provider'], 'manual')
        self.error(c.post('/api/advisor/run', {'provider': '', 'request_id': 'x'}), 400, 'INVALID')
        self.error(c.post('/api/advisor/run', {'provider': 'codex'}), 400, 'INVALID')
        self.app.advisor.fail_with = 'no network'
        since = self.ok(c.get('/api/state'))['version']
        self.ok(c.post('/api/advisor/run', {'provider': 'codex', 'request_id': preview['request_id']}))
        failed = None
        deadline = time.monotonic() + 10
        while failed is None and time.monotonic() < deadline:
            for e in self.ok(c.get(f'/api/events?since={since}&timeout=5'))['events']:
                since = e['version']
                if e['type'] == 'advisor.failed':
                    failed = e['data']
        self.assertEqual(failed['message'], i18n.t('error.UNAVAILABLE', 'en'))   # translated for the browser
        self.assertEqual(failed['message_key'], 'error.UNAVAILABLE')
        self.assertEqual(self.ok(c.get('/api/spec'))['digest'], before['digest'])   # advice changed nothing
        self.assertEqual(self.ok(c.get('/api/spec'))['spec'], before['spec'])
        self.assertEqual(self.ok(c.post('/api/advisor/enable', {'enabled': False}))['enabled'], False)

    def test_the_stored_advice_is_read_without_starting_any_cli(self):
        c = self.c
        self.scan()
        self.ok(c.post('/api/advisor/enable', {'enabled': True}))
        advisor = self.app.advisor
        advisor.calls.clear()
        self.assertEqual(self.ok(c.get('/api/advisor/last')), {'last': None})
        self.assertEqual(advisor.calls, [('last', ())])          # status() was not called, so no probe could run
        advisor.calls.clear()
        cached = self.ok(c.get('/api/advisor?probe=0'))
        self.assertEqual(advisor.calls, [('status', (False,))])
        self.assertEqual(cached['providers']['codex'], {'available': None, 'version': None, 'reason': 'not probed'})
        self.ok(c.get('/api/advisor'))
        self.assertEqual(advisor.calls[-1], ('status', (True,)))   # the advisor tab probes
        full = self.ok(c.get('/api/advisor'))
        self.assertEqual(full['providers']['codex']['available'], True)
        self.ok(c.post('/api/advisor/enable', {'enabled': False}))
        off = self.ok(c.get('/api/advisor'))
        self.assertEqual(off['providers']['claude'], {'available': None, 'version': None, 'reason': 'disabled'})

    def test_a_cancelled_scan_leaves_the_job_idle(self):
        gate = threading.Event()
        original = self.app.scan

        def slow_scan(domains=None, progress=None, cancel=None):
            gate.wait(30)
            return original(domains, progress=progress, cancel=lambda: True)
        with mock.patch.object(self.app, 'scan', side_effect=slow_scan):
            self.ok(self.c.post('/api/scan'))
            self.error(self.c.post('/api/job/pause'), 400, 'INVALID')   # a scan cannot be paused
            self.ok(self.c.post('/api/job/cancel'))
            gate.set()
            self.wait_for(lambda: self.c.get('/api/job')[1]['job']['status'] != 'running')
        job = self.c.get('/api/job')[1]['job']
        self.assertEqual((job['status'], job['error']), ('idle', None))
        self.assertIsNone(self.c.get('/api/state')[1]['scan'])


if __name__ == '__main__':
    unittest.main()
