"""The portal's HTTP server: loopback only, token auth, CSRF checks, JSON API and static ES modules."""
from __future__ import annotations

import hmac
import json
import os
import pkgutil
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from .. import i18n
from ..model import ErrorCode, StewardError
from ..service import App
from . import auth as auth_mod
from . import launch
from .api import Api, HttpError, error_response

HOST = '127.0.0.1'
MAX_BODY = 1024 * 1024
MAX_DRAIN = 8 * 1024 * 1024   # of a refused body, read and dropped so the client gets the answer, not a reset
MAX_THREADS = 32              # requests in flight, long polls included; the next one is answered 503
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
SECURITY_HEADERS = (('Content-Security-Policy', CSP), ('X-Content-Type-Options', 'nosniff'),
                    ('Referrer-Policy', 'no-referrer'))
CONTENT_TYPES = {'.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
                 '.mjs': 'text/javascript; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
                 '.svg': 'image/svg+xml'}
STATIC_SEGMENT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
PACKAGE = 'disk_steward.portal'


def read_static(rel: str) -> Optional[Tuple[bytes, str]]:
    """(content, content type) of a packaged asset, None if `rel` names none. Only plain segments pass, so no
    spelling of '..' (or an encoded one) can leave the static folder. pkgutil works from a wheel and a zipapp."""
    segments = rel.split('/')
    ext = os.path.splitext(segments[-1])[1]
    if ext not in CONTENT_TYPES or not all(STATIC_SEGMENT.match(s) for s in segments):
        return None
    try:
        data = pkgutil.get_data(PACKAGE, 'static/' + rel)
    except OSError as e:
        # zipimport reports a missing member as a bare OSError with errno 0, and a folder as IsADirectoryError
        if isinstance(e, (FileNotFoundError, IsADirectoryError, NotADirectoryError)) or e.errno == 0:
            return None
        raise
    if data is None:
        raise RuntimeError(f'cannot load resources of {PACKAGE}')
    return data, CONTENT_TYPES[ext]


def _reject_constant(name: str) -> Any:
    raise ValueError(f'invalid JSON constant {name}')


class _Handler(BaseHTTPRequestHandler):
    server: 'PortalServer'
    server_version = 'DiskSteward'
    sys_version = ''
    timeout = 60   # a client that connects and then says nothing must not hold a thread for ever

    def log_message(self, format: str, *args: Any) -> None:
        pass   # the request line of /launch carries the one-time code

    def do_GET(self) -> None:
        self._serve('GET')

    def do_POST(self) -> None:
        self._serve('POST')

    def do_PUT(self) -> None:
        self._serve('PUT')

    def do_DELETE(self) -> None:
        self._serve('DELETE')

    def do_PATCH(self) -> None:
        self._serve('PATCH')

    def do_HEAD(self) -> None:
        self._serve('HEAD')

    def do_OPTIONS(self) -> None:
        self._serve('OPTIONS')

    def send_error(self, code: int, message: Optional[str] = None, explain: Optional[str] = None) -> None:
        # the parser's own refusals (bad request line, header too long) get the same envelope and headers
        self._send_json(code, {'error': {'code': 'BAD_REQUEST', 'message_key': 'portal.error.bad_request',
                                         'message': i18n.t('portal.error.bad_request', self._report_locale()),
                                         'details': {}}})

    # plumbing --------------------------------------------------------------------------------------------

    def locale_for(self) -> str:
        return self.server.locale_for(self.headers.get('Accept-Language') if hasattr(self, 'headers') else None)

    def _report_locale(self) -> str:
        """The language for an error about a request that may itself be malformed. This runs while a failure is being
        reported, so it must not be able to fail again: whatever goes wrong here, English."""
        try:
            return self.locale_for()
        except Exception:
            return 'en'

    def _headers(self, status: int, content_type: str, length: int, cache: str,
                 extra: Tuple[Tuple[str, str], ...] = ()) -> None:
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(length))
        self.send_header('Cache-Control', cache)
        for name, value in SECURITY_HEADERS + extra:
            self.send_header(name, value)
        if getattr(self, 'legacy_cookie', False):   # an old portal's session cookie: sent to every port, so removed
            self.send_header('Set-Cookie', f'{auth_mod.LEGACY_COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict')
        self.end_headers()

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode('utf-8')
        self._headers(status, 'application/json; charset=utf-8', len(body), 'no-store')
        if getattr(self, 'command', None) != 'HEAD':
            self.wfile.write(body)

    def _send_bytes(self, status: int, body: bytes, content_type: str, cache: str,
                    extra: Tuple[Tuple[str, str], ...] = ()) -> None:
        self._headers(status, content_type, len(body), cache, extra)
        if getattr(self, 'command', None) != 'HEAD':
            self.wfile.write(body)

    def _fail(self, exc: Exception) -> None:
        status, payload = error_response(exc, self.locale_for(), self.server.api.labeler(self.locale_for()))
        self._send_json(status, payload)

    def _serve(self, method: str) -> None:
        self.legacy_cookie = auth_mod.has_legacy_cookie(self.headers.get('Cookie'))
        try:
            try:
                self._handle(method)
            except (HttpError, StewardError) as e:
                self._fail(e)
        except (BrokenPipeError, ConnectionResetError):
            pass   # the browser went away (a long poll outlived its tab)
        except Exception:
            traceback.print_exc(file=sys.stderr)   # for the operator; the client gets nothing to learn from
            try:
                self._send_json(500, {'error': {'code': 'INTERNAL', 'message_key': 'portal.error.internal',
                                                'message': i18n.t('portal.error.internal', self._report_locale()),
                                                'details': {}}})
            except OSError:
                pass

    # checks ----------------------------------------------------------------------------------------------

    def _check_host(self) -> str:
        host = self.headers.get('Host')
        port = self.server.port
        if host not in (f'{HOST}:{port}', f'localhost:{port}'):
            raise HttpError(421, 'MISDIRECTED', 'portal.error.bad_host')
        return host

    def _check_write(self, host: str) -> Dict[str, Any]:
        """Origin, the custom header, the media type, the size and the shape of a POST or PUT body."""
        if self.headers.get('Origin') != f'http://{host}':
            raise HttpError(403, 'FORBIDDEN', 'portal.error.bad_origin')
        if self.headers.get('X-Disk-Steward') != '1':
            raise HttpError(403, 'FORBIDDEN', 'portal.error.missing_header')
        if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            raise HttpError(415, 'UNSUPPORTED_MEDIA_TYPE', 'portal.error.bad_content_type')
        raw = self.headers.get('Content-Length')
        if raw is None or not raw.isdigit():
            raise HttpError(411, 'LENGTH_REQUIRED', 'portal.error.length_required')
        length = int(raw)
        if length > MAX_BODY:
            self.rfile.read(min(length, MAX_DRAIN))
            raise HttpError(413, 'PAYLOAD_TOO_LARGE', 'portal.error.too_large', limit=MAX_BODY)
        try:
            body = json.loads(self.rfile.read(length).decode('utf-8'), parse_constant=_reject_constant)
        except (ValueError, RecursionError):   # RecursionError: 100,000 nested brackets fit in a mebibyte
            raise HttpError(400, 'BAD_REQUEST', 'portal.error.bad_json') from None
        if not isinstance(body, dict):
            raise HttpError(400, 'BAD_REQUEST', 'portal.error.bad_json')
        return body

    # routes ----------------------------------------------------------------------------------------------

    def _handle(self, method: str) -> None:
        host = self._check_host()
        parts = urlsplit(self.path)
        path = parts.path
        query = {k: v[-1] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        if path == '/internal/launch-code' and method == 'POST':
            return self._internal_launch_code()
        if path == '/api/session' and method == 'POST':
            return self._session(self._check_write(host))
        if path.startswith('/api/'):
            if not self.server.auth.valid_token(self.headers.get(auth_mod.TOKEN_HEADER)):
                raise HttpError(401, 'UNAUTHORIZED', 'portal.error.unauthorized')
            body = None
            if method in ('POST', 'PUT'):
                body = self._check_write(host)
            elif method != 'GET':
                raise HttpError(405, 'METHOD_NOT_ALLOWED', 'portal.error.method_not_allowed', allowed=['GET'])
            status, payload = self.server.api.dispatch(method, path, query, body, self.locale_for())
            return self._send_json(status, payload)
        if method != 'GET':
            raise HttpError(405, 'METHOD_NOT_ALLOWED', 'portal.error.method_not_allowed', allowed=['GET'])
        if path in ('/', '/launch'):
            # The page is the same for everybody: it cannot know whether the browser holds a token. It exchanges the
            # code of /launch for one, or shows the locked page when it has none (the code never reaches a server log).
            return self._page('index.html', 200)
        if path.startswith('/static/'):
            found = read_static(unquote(path[len('/static/'):]))
            if found is None:
                raise HttpError(404, 'NOT_FOUND', 'portal.error.no_route', path=path)
            return self._send_bytes(200, found[0], found[1], 'no-cache')
        raise HttpError(404, 'NOT_FOUND', 'portal.error.no_route', path=path)

    def _page(self, name: str, status: int) -> None:
        found = read_static(name)
        if found is None:
            raise RuntimeError(f'packaged page {name} is missing')
        self._send_bytes(status, found[0], found[1], 'no-store')

    def _session(self, body: Dict[str, Any]) -> None:
        """Exchange a launch code for a session token. The token is kept by the page and sent in a header: a cookie
        would go to every server on 127.0.0.1, whatever its port."""
        code = body.get('code')
        token = self.server.auth.redeem(code) if isinstance(code, str) else None
        if token is None:
            raise HttpError(403, 'FORBIDDEN', 'portal.error.bad_code')
        self._send_json(200, {'token': token})

    def _internal_launch_code(self) -> None:
        # Not for browsers: a request that names an Origin is one, and so is refused even with the secret.
        given = self.headers.get(launch.SECRET_HEADER, '')
        if self.headers.get('Origin') is not None or not hmac.compare_digest(
                given.encode(), self.server.launch_secret.encode()):
            raise HttpError(403, 'FORBIDDEN', 'portal.error.bad_secret')
        url = self.server.new_launch_url()
        self._send_json(200, {'url': url})


class PortalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, app: App, port: int = 0, clock: Callable[[], float] = time.monotonic,
                 default_locale: Optional[str] = None, max_threads: int = MAX_THREADS):
        self.app = app
        self._slots = threading.BoundedSemaphore(max_threads)
        self.api = Api(app)
        self.auth = auth_mod.Auth(clock)
        self.launch_secret = secrets.token_urlsafe(32)
        self.default_locale = default_locale
        self._thread: Optional[threading.Thread] = None
        super().__init__((HOST, port), _Handler)
        self.port: int = self.server_address[1]

    def locale_for(self, accept_language: Optional[str]) -> str:
        """The language the user chose in the settings; else Accept-Language of the request; else the App's."""
        chosen = self.app.settings.locale
        if chosen:
            return chosen
        if self.default_locale:
            return self.default_locale
        if accept_language:
            return i18n.detect_locale(None, accept_language)
        return self.app.locale

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self._refuse(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def _refuse(self, request: socket.socket) -> None:
        """503 for a request that finds every slot taken. Answered here, in the accepting thread, without parsing
        it: that costs no thread, which is the point."""
        body = json.dumps({'error': {'code': 'UNAVAILABLE', 'message_key': 'portal.error.too_busy',
                                     'message': i18n.t('portal.error.too_busy', self.locale_for(None)),
                                     'details': {}}}).encode('utf-8')
        head = ('HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json; charset=utf-8\r\n'
                f'Content-Length: {len(body)}\r\nCache-Control: no-store\r\nRetry-After: 1\r\n'
                + ''.join(f'{k}: {v}\r\n' for k, v in SECURITY_HEADERS) + '\r\n').encode('ascii')
        try:
            request.settimeout(0.2)
            try:
                request.recv(65536)   # a request left unread would make the close a reset that eats the answer
            except OSError:
                pass
            request.sendall(head + body)
        except OSError:
            pass   # the client is gone
        finally:
            self.shutdown_request(request)

    def new_launch_url(self) -> str:
        return f'http://{HOST}:{self.port}/launch?code={self.auth.new_code()}'

    def start(self) -> None:
        """Write portal.json and serve in a background thread. Refuses while another portal owns the data dir."""
        data_dir = self.app.data_dir
        existing = launch.running_info(data_dir)
        if existing is not None and int(existing['pid']) != os.getpid():
            raise StewardError(ErrorCode.BUSY, 'portal.cli.already_running', pid=existing['pid'], port=existing['port'])
        launch.write_info(data_dir, {'port': self.port, 'pid': os.getpid(), 'started_at': time.time(),
                                     'launch_secret': self.launch_secret})
        # a short poll interval: shutdown() waits for the loop to notice it, half a second by default
        self._thread = threading.Thread(target=self.serve_forever, args=(0.05,), name='disk-steward-portal', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            self.shutdown()
            self._thread.join()
            self._thread = None
        self.server_close()
        launch.remove_info(self.app.data_dir, os.getpid())


def open_in_browser(url: str) -> None:
    subprocess.run(['open', url], check=True)


def serve(app: App, port: int = 0, open_browser: bool = False, announce: Optional[Callable[[str], None]] = None,
          opener: Callable[[str], None] = open_in_browser, default_locale: Optional[str] = None) -> int:
    """Run the portal until SIGINT or SIGTERM; returns 0 after a clean shutdown. `announce(url)` receives the
    one-time launch URL once the server is up (default: print it)."""
    try:
        server = PortalServer(app, port, default_locale=default_locale)
    except OSError as e:
        raise StewardError(ErrorCode.UNAVAILABLE, 'portal.cli.bind_failed', port=port, reason=str(e)) from e
    stop = threading.Event()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())   # shutdown() must not run in the serving thread
    try:
        server.start()
        url = server.new_launch_url()
        (announce or print)(url)
        if open_browser:
            try:
                opener(url)
            except (OSError, subprocess.CalledProcessError) as e:   # the portal still runs; the URL is printed
                print(i18n.t('portal.cli.open_failed', server.locale_for(None), reason=str(e)), file=sys.stderr)
        while not stop.wait(0.5):
            pass
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        server.stop()
    return 0
