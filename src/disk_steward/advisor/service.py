"""AdvisorService: the optional AI advisor behind the App.

The advisor only suggests. Its results are badges and reasons attached to group keys; no code path here reads or
writes the selection spec, a plan, or anything on disk besides the advice table and the manual-export record."""
from __future__ import annotations

import collections
import hashlib
import json
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ..model import ErrorCode, StewardError
from ..report import group_label
from . import prompts, validate
from .payload import Built, build_payload
from .providers import CWD_NAME, PROVIDERS, Probe, Provider

REQUEST_TTL = 3600       # seconds a preview stays usable
MAX_REQUESTS = 5
PROBE_OK_TTL = 30        # seconds a successful CLI probe is reused
PROBE_FAIL_TTL = 10      # a failed one is retried sooner: the user may be fixing it
NOTICE_KEY = 'advisor.notice.own_instructions'   # what Disk Steward cannot show: each CLI's own instructions
_UNKNOWN = {'available': None, 'version': None}
EXPORT_META = 'advisor.export'   # the latest manual export, so a later CLI process can import the reply


@dataclass(frozen=True)
class Request:
    id: str
    scan_id: int
    created_at: float
    expires_at: float
    digest: str
    tokens: Dict[str, Dict[str, Any]]
    json: str = ''     # the payload as sent; empty for a request restored from the export record

    def record(self) -> Dict[str, Any]:
        return {'id': self.id, 'scan_id': self.scan_id, 'created_at': self.created_at,
                'expires_at': self.expires_at, 'digest': self.digest, 'tokens': self.tokens}


def _fail(code: ErrorCode, key: str, **details: Any) -> StewardError:
    return StewardError(code, key, **details)


class AdvisorService:
    def __init__(self, app: Any):
        self._app = app
        self._lock = threading.Lock()
        self._requests: 'collections.OrderedDict[str, Request]' = collections.OrderedDict()
        self._busy = False
        self._probes: Dict[str, Tuple[float, Probe]] = {}
        cwd = app.data_dir / CWD_NAME
        self._impl: Dict[str, Provider] = {name: cls(app.bases.home, cwd) for name, cls in PROVIDERS.items()}

    # status and switch -----------------------------------------------------------------------------------

    def status(self, refresh: bool = False, probe: bool = True) -> Dict[str, Any]:
        """Never runs a CLI while the advisor is off. `probe=False` never runs one at all (cached answers only):
        pages that just need `last` must not wait for the CLIs."""
        settings = self._app.settings
        providers: Dict[str, Any] = {}
        if not settings.advisor_enabled:
            providers.update({name: dict(_UNKNOWN, reason='disabled') for name in self._impl})
        elif probe:
            with ThreadPoolExecutor(max_workers=len(self._impl)) as pool:   # the CLIs are probed side by side
                probes = pool.map(lambda n: self._probe(n, refresh), list(self._impl))
                providers.update({name: found.info() for name, found in zip(self._impl, probes)})
        else:
            for name in self._impl:
                found = self._cached(name)
                providers[name] = dict(_UNKNOWN, reason='not probed') if found is None else found.info()
        providers['manual'] = {'available': True}
        return {'enabled': settings.advisor_enabled, 'provider': settings.advisor_provider,
                'providers': providers, 'last': self.last()}

    def set_enabled(self, on: bool) -> Dict[str, Any]:
        self._app.update_settings({'advisor_enabled': on})
        if not on:   # nothing pending may outlive the switch
            with self._lock:
                self._requests.clear()
            self._app.store.delete_meta(EXPORT_META)
        return self.status()

    def _require_enabled(self) -> None:
        if not self._app.settings.advisor_enabled:
            raise _fail(ErrorCode.UNAVAILABLE, 'error.advisor_disabled')

    def _cached(self, name: str) -> Optional[Probe]:
        with self._lock:
            hit = self._probes.get(name)
        if hit is None:
            return None
        ttl = PROBE_OK_TTL if hit[1].available else PROBE_FAIL_TTL
        return hit[1] if time.monotonic() - hit[0] < ttl else None

    def _probe(self, name: str, refresh: bool = False, reuse_failure: bool = True) -> Probe:
        found = None if refresh else self._cached(name)
        if found is not None and (found.available or reuse_failure):
            return found
        found = self._impl[name].probe()
        with self._lock:
            self._probes[name] = (time.monotonic(), found)
        return found

    def last(self) -> Optional[Dict[str, Any]]:
        """The newest advice for the latest scan. Advice for an older scan is stale and is not shown."""
        latest = self._app.store.latest_scan()
        row = None if latest is None else self._app.store.latest_advice(latest['id'])
        if row is None:
            return None
        return {'id': row['id'], 'scan_id': row['scan_id'], 'provider': row['provider'],
                'cli_version': row['cli_version'], 'created_at': row['created_at'],
                'summary': row['result']['summary'], 'recommendations': row['result']['recommendations'],
                'traces': row['result'].get('traces', []), 'stale': False}

    # preview ---------------------------------------------------------------------------------------------

    def preview(self) -> Dict[str, Any]:
        """Build the exact payload a run would send and keep its token map for the reply."""
        self._require_enabled()
        app = self._app
        latest = app.store.latest_scan()
        if latest is None:
            raise _fail(ErrorCode.NO_SCAN, 'error.NO_SCAN')
        now = app.clock()
        built = build_payload(app.store, latest['id'], now)
        again = app.store.latest_scan()
        if again is None or again['id'] != built.scan_id:   # a scan finished while the payload was read
            raise _fail(ErrorCode.BUSY, 'error.advisor_scan_changed')
        req = self._remember(built, now)
        return {'request_id': req.id, 'scan_id': req.scan_id, 'payload': built.payload, 'bytes': len(built.json),
                'coverage': built.coverage, 'expires_at': req.expires_at, 'notice_key': NOTICE_KEY}

    def _remember(self, built: Built, now: float) -> Request:
        req = Request(id=secrets.token_urlsafe(9), scan_id=built.scan_id, created_at=now, expires_at=now + REQUEST_TTL,
                      digest=hashlib.sha256(built.json.encode('utf-8')).hexdigest(), tokens=built.tokens,
                      json=built.json)
        with self._lock:
            self._requests[req.id] = req
            while len(self._requests) > MAX_REQUESTS:
                self._requests.popitem(last=False)
        return req

    def _request(self, request_id: Optional[str], exported: bool = False) -> Request:
        """A live request. `exported` also accepts the manual-export record (request_id None = that record)."""
        with self._lock:
            req = self._requests.get(request_id) if request_id is not None else None
        if req is None and exported:
            req = self._exported(request_id)
        if req is None:
            raise _fail(ErrorCode.NOT_FOUND, 'error.advisor_unknown_request')
        self._check_fresh(req)
        return req

    def _check_fresh(self, req: Request) -> None:
        if self._app.clock() > req.expires_at:
            with self._lock:
                self._requests.pop(req.id, None)
            raise _fail(ErrorCode.INVALID, 'error.advisor_request_expired')

    def _exported(self, request_id: Optional[str]) -> Optional[Request]:
        text = self._app.store.meta(EXPORT_META)
        if text is None:
            return None
        rec = json.loads(text)
        if request_id is not None and rec['id'] != request_id:
            return None
        return Request(id=rec['id'], scan_id=rec['scan_id'], created_at=rec['created_at'],
                       expires_at=rec['expires_at'], digest=rec['digest'], tokens=rec['tokens'])

    # providers -------------------------------------------------------------------------------------------

    def _provider(self, name: Optional[str]) -> Tuple[str, Provider, Probe]:
        name = name or self._app.settings.advisor_provider
        if name is None:
            raise _fail(ErrorCode.INVALID, 'error.advisor_no_provider')
        if name == 'manual':
            raise _fail(ErrorCode.INVALID, 'error.advisor_manual_only')
        if name not in self._impl:
            raise _fail(ErrorCode.INVALID, 'error.advisor_unknown_provider', provider=str(name))
        probe = self._probe(name, reuse_failure=False)   # an explicit request: ask again rather than trust a failure
        if not probe.available:
            raise _fail(ErrorCode.UNAVAILABLE, 'error.advisor_provider_unavailable', provider=name,
                        reason=probe.reason)
        return name, self._impl[name], probe

    def command(self, provider: Optional[str], request_id: str) -> Dict[str, Any]:
        """What run() would execute: the argv (temp paths as placeholders) and the size of the stdin prompt."""
        self._require_enabled()
        name, impl, probe = self._provider(provider)
        req = self._request(request_id)
        return {'provider': name, 'version': probe.version, 'argv': impl.command_line(probe),
                'stdin_bytes': len(impl.stdin_text(req.json).encode('utf-8'))}

    def run(self, provider: Optional[str], request_id: str) -> Dict[str, Any]:
        """Blocking: one CLI call, then validation. Only one advisor call runs at a time."""
        name, impl, probe, req = self._claim(provider, request_id)
        return self._execute(name, impl, probe, req)

    def start_run(self, provider: Optional[str], request_id: str) -> Dict[str, Any]:
        """run() in a background thread; the outcome arrives as advisor.done / advisor.failed events."""
        name, impl, probe, req = self._claim(provider, request_id)

        def work() -> None:
            try:
                self._execute(name, impl, probe, req)
            except Exception:   # published as advisor.failed; a thread has nobody to raise to
                pass

        threading.Thread(target=work, name='disk-steward-advisor', daemon=True).start()
        return {'request_id': req.id, 'provider': name}

    def _claim(self, provider: Optional[str], request_id: str) -> Tuple[str, Provider, Probe, Request]:
        """Every refusal happens here, in the caller's thread, before the one-call-at-a-time slot is taken."""
        self._require_enabled()
        name, impl, probe = self._provider(provider)
        req = self._request(request_id)
        with self._lock:
            if self._busy:
                raise _fail(ErrorCode.BUSY, 'error.busy')
            self._busy = True
        return name, impl, probe, req

    def _release(self) -> None:
        with self._lock:
            self._busy = False

    def _execute(self, name: str, impl: Provider, probe: Probe, req: Request) -> Dict[str, Any]:
        events = self._app.events
        events.publish('advisor.started', provider=name, request_id=req.id)
        try:
            reply, traces = impl.run(probe, req.json)
            result = self._finish(req, name, probe.version, reply, traces)
        except BaseException as e:
            self._release()
            if isinstance(e, StewardError):
                events.publish('advisor.failed', code=e.code.value, message_key=e.message_key, details=e.details)
            else:
                events.publish('advisor.failed', code='INTERNAL', message_key='error.advisor_internal',
                               details={'error': f'{type(e).__name__}: {e}'})
            raise
        self._release()
        events.publish('advisor.done', result=result)
        return result

    # manual fallback -------------------------------------------------------------------------------------

    def export_prompt(self, request_id: str) -> Dict[str, str]:
        """The text to paste into any chat. The request is also saved, so `import` can run in a later process."""
        self._require_enabled()
        req = self._request(request_id)
        self._app.store.set_meta(EXPORT_META, json.dumps(req.record(), sort_keys=True))
        return {'text': prompts.manual_prompt(req.json)}

    def import_reply(self, request_id: Optional[str], text: str) -> Dict[str, Any]:
        """Validate a pasted reply. `request_id` None means the latest manual export."""
        self._require_enabled()
        req = self._request(request_id, exported=True)
        result = self._finish(req, 'manual', None, validate.parse_manual(text), [])
        text_now = self._app.store.meta(EXPORT_META)
        if text_now is not None and json.loads(text_now)['id'] == req.id:
            self._app.store.delete_meta(EXPORT_META)
        return result

    # result ----------------------------------------------------------------------------------------------

    def _finish(self, req: Request, provider: str, cli_version: Optional[str], reply: Any,
                traces: List[str]) -> Dict[str, Any]:
        """Validate, then map tokens back to group keys and labels, and persist. Nothing before validation."""
        app = self._app
        self._require_enabled()   # switched off while the call ran: the answer is discarded, not stored or shown
        checked = validate.validate_reply(reply, req.tokens)
        self._check_fresh(req)
        recs: List[Dict[str, str]] = []
        for rec in checked['recommendations']:
            group = req.tokens[rec['group']]
            recs.append({'group': group['key'], 'label': group_label(group, app.locale),
                         'verdict': rec['verdict'], 'reason': rec['reason']})
        latest = app.store.latest_scan()
        created = app.clock()
        advice_id = app.store.add_advice({
            'scan_id': req.scan_id, 'provider': provider, 'cli_version': cli_version or '',
            'payload_digest': req.digest,
            'result': {'summary': checked['summary'], 'recommendations': recs, 'traces': traces},
            'created_at': created})
        return {'id': advice_id, 'scan_id': req.scan_id, 'provider': provider, 'cli_version': cli_version or '',
                'created_at': created, 'summary': checked['summary'], 'recommendations': recs, 'traces': traces,
                'stale': latest is None or latest['id'] != req.scan_id}
