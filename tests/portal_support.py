"""Helpers for the portal tests: a fake advisor, an App that carries it, and a small HTTP client."""
from __future__ import annotations

import http.client
import json
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from disk_steward.model import ErrorCode, StewardError
from disk_steward.service import App
from disk_steward.settings import Settings

from m3_support import make_mac


class FakeAdvisor:
    """The contract M7's AdvisorService fulfils (docs: status, set_enabled, preview, start_run, run, export_prompt,
    import_reply), with no model behind it: a run takes `delay` seconds and recommends the first groups of the scan."""

    def __init__(self, app: App, delay: float = 0.05):
        self.app = app
        self.delay = delay
        self.enabled = app.settings.advisor_enabled
        self.last_result: Optional[Dict[str, Any]] = None
        self.calls: List[Tuple[str, Tuple[Any, ...]]] = []
        self.fail_with: Optional[str] = None
        self._previews: Dict[str, Dict[str, Any]] = {}

    def status(self, refresh: bool = False, probe: bool = True) -> Dict[str, Any]:
        self.calls.append(('status', (probe,)))
        if not self.enabled:   # the real service probes nothing while it is off
            providers = {n: {'available': None, 'version': None, 'reason': 'disabled'} for n in ('codex', 'claude')}
        elif probe:
            providers = {'codex': {'available': True, 'version': 'fake-codex 1.0', 'reason': ''},
                         'claude': {'available': False, 'version': None, 'reason': 'claude was not found on PATH'}}
        else:
            providers = {n: {'available': None, 'version': None, 'reason': 'not probed'} for n in ('codex', 'claude')}
        return {'enabled': self.enabled, 'provider': None, 'providers': dict(providers, manual={'available': True}),
                'last': self.last_result}

    def last(self) -> Optional[Dict[str, Any]]:
        self.calls.append(('last', ()))
        return self.last_result

    def set_enabled(self, enabled: bool) -> None:
        self.calls.append(('set_enabled', (enabled,)))
        self.enabled = enabled
        self.app.update_settings({'advisor_enabled': enabled})

    def preview(self) -> Dict[str, Any]:
        self.calls.append(('preview', ()))
        self._need_enabled()
        groups = self.app.groups()
        request_id = f'req-{len(self._previews) + 1}'
        payload = {'groups': [{'token': f'g{i}', 'tier': g['tier'], 'alloc': g['alloc'], 'items': g['items']}
                              for i, g in enumerate(groups)]}
        found = {'request_id': request_id, 'payload': payload, 'bytes': len(json.dumps(payload)),
                 'notice_key': 'advisor.notice.own_instructions',
                 'coverage': {'groups_included': len(groups), 'groups_total': len(groups),
                              'alloc_included': sum(g['alloc'] for g in groups),
                              'alloc_total': sum(g['alloc'] for g in groups)},
                 'expires_at': self.app.clock() + 600}
        self._previews[request_id] = found
        return found

    def _need_enabled(self) -> None:
        if not self.enabled:
            raise StewardError(ErrorCode.UNAVAILABLE, 'error.UNAVAILABLE')

    def _result(self, provider: str, summary: str) -> Dict[str, Any]:
        groups = [g for g in self.app.groups() if g['tier'] != 'protected'][:3]
        verdicts = ('likely_safe', 'review', 'keep')
        return {'id': len(self.calls), 'scan_id': self.app.latest_scan()['id'], 'provider': provider,
                'cli_version': 'fake 1.0', 'created_at': self.app.clock(), 'summary': summary, 'stale': False,
                'traces': ['~/.claude.json'],
                'recommendations': [{'group': g['key'], 'label': g['label'], 'verdict': verdicts[i % 3],
                                     'reason': f'<b>reason {i}</b> & more'} for i, g in enumerate(groups)]}

    def run(self, provider: str, request_id: str) -> Dict[str, Any]:
        self.calls.append(('run', (provider, request_id)))
        self._need_enabled()
        if request_id not in self._previews:
            raise StewardError(ErrorCode.NOT_FOUND, 'error.NOT_FOUND', what='request', id=request_id)
        time.sleep(self.delay)
        if self.fail_with:
            raise StewardError(ErrorCode.UNAVAILABLE, 'error.UNAVAILABLE', reason=self.fail_with)
        self.last_result = self._result(provider, 'Summary <i>from the fake advisor</i>.')
        return self.last_result

    def start_run(self, provider: str, request_id: str) -> Dict[str, Any]:
        self.calls.append(('start_run', (provider, request_id)))
        self._need_enabled()
        if request_id not in self._previews:
            raise StewardError(ErrorCode.NOT_FOUND, 'error.NOT_FOUND', what='request', id=request_id)

        def work() -> None:
            self.app.events.publish('advisor.started', provider=provider, request_id=request_id)
            time.sleep(self.delay)
            if self.fail_with:   # the shape of the real service's failure event
                self.app.events.publish('advisor.failed', code='UNAVAILABLE', message_key='error.UNAVAILABLE',
                                        details={'reason': self.fail_with})
                return
            self.last_result = self._result(provider, 'Summary <i>from the fake advisor</i>.')
            self.app.events.publish('advisor.done', result=self.last_result)

        threading.Thread(target=work, daemon=True).start()
        return {'request_id': request_id, 'provider': provider}

    def export_prompt(self, request_id: str) -> Dict[str, str]:
        self.calls.append(('export_prompt', (request_id,)))
        if request_id not in self._previews:
            raise StewardError(ErrorCode.NOT_FOUND, 'error.NOT_FOUND', what='request', id=request_id)
        return {'text': f'PROMPT for {request_id}'}

    def import_reply(self, request_id: str, text: str) -> Dict[str, Any]:
        self.calls.append(('import_reply', (request_id, text)))
        if request_id not in self._previews:
            raise StewardError(ErrorCode.NOT_FOUND, 'error.NOT_FOUND', what='request', id=request_id)
        try:
            json.loads(text)
        except ValueError:
            raise StewardError(ErrorCode.INVALID, 'error.INVALID') from None
        self.last_result = self._result('manual', 'Imported.')
        return self.last_result


class AdvisorApp(App):
    """An App with a FakeAdvisor, whatever the real `advisor` property does."""

    @property
    def advisor(self) -> Any:
        if getattr(self, '_fake_advisor', None) is None:
            self._fake_advisor = FakeAdvisor(self)
        return self._fake_advisor


def make_portal_app(fh, mac=None, settings: Optional[Settings] = None, **kw) -> AdvisorApp:
    return AdvisorApp(home=fh.home, data_dir=fh.data_dir, mac=mac or make_mac(), clock=fh.clock, locale='en',
                      settings=settings or Settings(domains=('agent',)), bases=fh.bases, **kw)


class Client:
    """A browser stand-in: one session token, the Host and Origin a browser would send for this server."""

    def __init__(self, server, host: Optional[str] = None):
        self.server = server
        self.host = f'127.0.0.1:{server.port}' if host is None else host
        self.token: Optional[str] = None

    def request(self, method: str, path: str, body: Any = None, headers: Optional[Dict[str, str]] = None,
                raw: Optional[bytes] = None, origin: Any = True, token: Any = True, cookie: Optional[str] = None,
                timeout: float = 30) -> Tuple[int, Dict[str, str], bytes]:
        """`token`: True sends the client's own, a string sends that one, False none. `cookie` is a raw Cookie header."""
        h = {'Host': self.host}
        if token is True and self.token:
            h['X-Disk-Steward-Token'] = self.token
        elif isinstance(token, str):
            h['X-Disk-Steward-Token'] = token
        if cookie is not None:
            h['Cookie'] = cookie
        data = raw
        if method in ('POST', 'PUT'):
            h.update({'Content-Type': 'application/json', 'X-Disk-Steward': '1'})
            if origin is True:
                h['Origin'] = f'http://{self.host}'
            elif isinstance(origin, str):
                h['Origin'] = origin
            if data is None:
                data = json.dumps({} if body is None else body).encode()
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        conn = http.client.HTTPConnection('127.0.0.1', self.server.port, timeout=timeout)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for k, v in h.items():
                conn.putheader(k, v)
            if data is not None:
                conn.putheader('Content-Length', str(len(data)))
            conn.endheaders(data)
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        finally:
            conn.close()

    def json(self, method: str, path: str, body: Any = None, **kw) -> Tuple[int, Any]:
        status, headers, data = self.request(method, path, body, **kw)
        return status, json.loads(data) if data else None

    def get(self, path: str, **kw) -> Tuple[int, Any]:
        return self.json('GET', path, **kw)

    def post(self, path: str, body: Any = None, **kw) -> Tuple[int, Any]:
        return self.json('POST', path, body, **kw)

    def put(self, path: str, body: Any = None, **kw) -> Tuple[int, Any]:
        return self.json('PUT', path, body, **kw)

    def login(self) -> 'Client':
        """What the page does with the code of a launch URL."""
        code = self.server.new_launch_url().split('code=')[1]
        status, body = self.post('/api/session', {'code': code}, token=False)
        assert status == 200, (status, body)
        self.token = body['token']
        return self
