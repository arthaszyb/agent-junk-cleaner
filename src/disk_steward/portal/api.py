"""The portal's JSON API: routes over the App facade.

The browser never decides anything. Every text that needs Python formatting (decision reasons, warnings, skip
reasons, blockers) is rendered here, in the request's locale, next to the data it explains."""
from __future__ import annotations

import math
import re
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Tuple
from urllib.parse import unquote

from .. import i18n
from ..classify import split_group_key
from ..cli.commands import blocker_text, reason_text
from ..model import ErrorCode, StewardError, asdict
from ..report import REASONS, cloud_warning_lines, fmt_bytes, group_effect, group_label
from ..service import QUICK_SESSION, RUN_SAFE_SETTINGS, App

Json = Dict[str, Any]
SESSION = 'default'
FILTERS = ('domain', 'tier', 'report_only', 'state', 'reason')   # the Browse filters, as query parameters
MAX_LONG_POLL = 25.0
MAX_ITEMS_PAGE = 500
MAX_ENTRIES_PAGE = 1000
INSIGHTS_WINDOWS = (0, 7, 30, 90)   # days of history the Insights page offers; 0 is all of it
DAY_SECONDS = 86400.0
BYTE_ARGS = ('size', 'needed', 'free')   # warning arguments that are byte counts

STATUS = {ErrorCode.INVALID: 400, ErrorCode.NOT_FOUND: 404, ErrorCode.DIGEST_MISMATCH: 409,
          ErrorCode.REVISION_MISMATCH: 409, ErrorCode.RULES_CHANGED: 409, ErrorCode.PLAN_EXPIRED: 409,
          ErrorCode.NO_SCAN: 409, ErrorCode.EMPTY_PLAN: 422, ErrorCode.BUSY: 423, ErrorCode.IN_PROGRESS: 423,
          ErrorCode.UNAVAILABLE: 503, ErrorCode.DATA_DIR_RECREATED: 409}


class HttpError(Exception):
    """A refusal that is about the request itself (host, origin, size, route), not about the App."""

    def __init__(self, status: int, code: str, message_key: str, **details: Any):
        super().__init__(f'{status} {code}: {message_key}')
        self.status, self.code, self.message_key, self.details = status, code, message_key, details


def bad_field(field: str) -> StewardError:
    return StewardError(ErrorCode.INVALID, 'portal.error.bad_field', field=field)


def error_response(exc: Exception, locale: str, labeler: Optional[Callable[[str], str]] = None) -> Tuple[int, Json]:
    """(status, {'error': {code, message_key, message, details}}) for a StewardError or an HttpError."""
    if isinstance(exc, HttpError):
        status, code = exc.status, exc.code
    elif isinstance(exc, StewardError):
        status, code = STATUS[exc.code], exc.code.value
    else:
        raise TypeError(f'not an API error: {exc!r}')
    details = dict(exc.details)
    args = dict(details)
    if exc.message_key == 'error.not_selectable':
        args['reason'] = i18n.t(f'reason.{details["reason"]}', locale)
    if exc.message_key == 'error.native_not_runnable':   # the reason is a text of its own, in the request's language
        args['reason'] = details['reason'] = i18n.t(details['reason_key'], locale, **details['reason_args'])
    if exc.message_key == 'error.spec_stale' and details.get('notes') and labeler is not None:
        details['labels'] = _note_labels(details['notes'], labeler)
    return status, {'error': {'code': code, 'message_key': exc.message_key,
                              'message': _render(exc.message_key, locale, args), 'details': details}}


def _render(key: str, locale: str, args: Mapping[str, Any]) -> str:
    """i18n.t with the arguments in a dict: a detail may itself be called `locale` (error.invalid_locale)."""
    template = i18n.catalog(locale).get(key)
    return i18n.t(key, locale) if template is None else template.format_map(args)


def _note_labels(notes: Mapping[str, Any], labeler: Callable[[str], str]) -> Dict[str, str]:
    keys = list(notes.get('dropped_groups', ())) + [c['group'] for c in notes.get('group_changes', ())]
    return {k: labeler(k) for k in keys}


class Request(NamedTuple):
    method: str
    query: Dict[str, str]
    body: Optional[Dict[str, Any]]
    locale: str
    args: Tuple[str, ...]


def _int(value: Any, field: str, lo: Optional[int] = None, hi: Optional[int] = None) -> int:
    """A JSON integer (not a bool, not a string)."""
    if isinstance(value, bool) or not isinstance(value, int) or (lo is not None and value < lo) \
            or (hi is not None and value > hi):
        raise bad_field(field)
    return value


def _query_int(text: str, field: str, lo: Optional[int] = None, hi: Optional[int] = None) -> int:
    try:
        return _int(int(text), field, lo, hi)
    except ValueError:
        raise bad_field(field) from None


def _str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise bad_field(field)
    return value


def _body(r: Request) -> Dict[str, Any]:
    assert r.body is not None
    return r.body


def overview_filters(days: Any) -> Json:
    """The Browse filter that shows exactly the items behind each number of the overview (the buckets of
    report.build_report), so the page does not have to guess. `days` is the age filter the overview was judged with."""
    return {
        'tier': {t: {'tier': t} for t in ('junk', 'review', 'protected')},
        'rows': {'offered': {'tier': 'junk', 'state': 'selected', 'days': days},
                 'too_recent': {'tier': 'junk', 'state': 'too_recent', 'days': days},
                 'floor_held': {'tier': 'junk', 'state': 'too_recent_floor', 'days': days},
                 'review': {'tier': 'review', 'report_only': '0'},
                 'report_only': {'report_only': '1'},
                 'other': {'tier': 'junk', 'state': 'other', 'days': days},
                 'protected': {'tier': 'protected'}},
        'reasons': {r: {'tier': 'protected', 'reason': r} for r in REASONS},
    }


def _age_days(newest_ns: int, last_used_ns: Optional[int], basis: Optional[str], now: float) -> float:
    ns = newest_ns
    if basis == 'last_used' and last_used_ns is not None and last_used_ns > ns:
        ns = last_used_ns
    return max(0.0, (now - ns / 1e9) / DAY_SECONDS)


def _entry_newest_ns(fp: Mapping[str, int]) -> int:
    return fp['newest_ns'] if 'newest_ns' in fp else max(fp['mtime_ns'], fp['birth_ns'])


class Api:
    def __init__(self, app: App):
        self.app = app
        route = self._route
        self._routes: List[Tuple[str, 're.Pattern[str]', Callable[[Request], Json]]] = []
        route('GET', r'/api/state', self.state)
        route('GET', r'/api/events', self.events)
        route('POST', r'/api/scan(?:/start)?', self.scan_start)
        route('GET', r'/api/overview', self.overview)
        route('GET', r'/api/quick', self.quick)
        route('POST', r'/api/quick/plan', self.quick_plan)
        route('GET', r'/api/apps', self.apps)
        route('GET', r'/api/insights', self.insights)
        route('GET', r'/api/native', self.native)
        route('POST', r'/api/native/run', self.native_run)
        route('POST', r'/api/native/trash', self.native_trash)
        route('GET', r'/api/groups', self.groups)
        route('GET', r'/api/items', self.items)
        route('GET', r'/api/spec', self.spec)
        route('GET', r'/api/spec/notes', self.spec_notes)
        route('POST', r'/api/spec/ops', self.spec_ops)
        route('POST', r'/api/plans', self.plan_create)
        route('GET', r'/api/plans', self.plans)
        route('GET', r'/api/plans/([^/]+)', self.plan)
        route('GET', r'/api/plans/([^/]+)/entries', self.plan_entries)
        route('GET', r'/api/plans/([^/]+)/groups', self.plan_groups)
        route('POST', r'/api/plans/([^/]+)/exclude', self.plan_exclude)
        route('POST', r'/api/plans/([^/]+)/include', self.plan_include)
        route('POST', r'/api/plans/([^/]+)/discard', self.plan_discard)
        route('POST', r'/api/plans/([^/]+)/run', self.plan_run)
        route('GET', r'/api/preflight/([^/]+)', self.preflight)
        route('GET', r'/api/job', self.job)
        route('POST', r'/api/job/(pause|resume|cancel)', self.job_control)
        route('GET', r'/api/archives', self.archives)
        route('POST', r'/api/archives/([^/]+)/(restore|purge)', self.archive_action)
        route('GET', r'/api/advisor', self.advisor_status)
        route('GET', r'/api/advisor/last', self.advisor_last)
        route('POST', r'/api/advisor/(enable|preview|run|export|import)', self.advisor_action)
        route('GET', r'/api/settings', self.settings_get)
        route('PUT', r'/api/settings', self.settings_put)
        route('GET', r'/api/i18n/([^/]+)', self.i18n_catalog)

    def _route(self, method: str, pattern: str, fn: Callable[[Request], Json]) -> None:
        self._routes.append((method, re.compile(pattern + '$'), fn))

    def dispatch(self, method: str, path: str, query: Dict[str, str], body: Optional[Dict[str, Any]],
                 locale: str) -> Tuple[int, Json]:
        self.app.check_data_dir()   # a deleted data dir is made again here, before the request reads or writes it
        allowed: List[str] = []
        for m, pattern, fn in self._routes:
            found = pattern.match(path)
            if found is None:
                continue
            if m != method:
                allowed.append(m)
                continue
            return 200, fn(Request(method, query, body, locale, tuple(unquote(g) for g in found.groups())))
        if allowed:
            raise HttpError(405, 'METHOD_NOT_ALLOWED', 'portal.error.method_not_allowed', allowed=sorted(set(allowed)))
        raise HttpError(404, 'NOT_FOUND', 'portal.error.no_route', path=path)

    # texts -----------------------------------------------------------------------------------------------

    def label_of_key(self, key: str, locale: str) -> str:
        """A group's label from its key alone, for groups the current scan no longer has (label_args derive from
        the key exactly as the scanner builds them)."""
        rule_id, suffix = split_group_key(key)
        return group_label({'key': key, 'rule_id': rule_id, 'label_args': {'path': suffix} if suffix else {}}, locale)

    def labeler(self, locale: str) -> Callable[[str], str]:
        return lambda key: self.label_of_key(key, locale)

    def _warning(self, w: Mapping[str, Any], locale: str) -> Json:
        out = dict(w)
        args = {k: v for k, v in w.items() if k != 'key'}
        if 'group' in args:
            args['group'] = out['group_label'] = self.label_of_key(args['group'], locale)
        for name in BYTE_ARGS:
            if name in args:
                args[name] = fmt_bytes(args[name])
        out['text'] = i18n.t(w['key'], locale, **args)
        return out

    def _decision(self, d: Mapping[str, Any], locale: str) -> Json:
        out = dict(d)
        out['text'] = i18n.t(d['reason_key'], locale, **d['reason_args'])
        return out

    def _evidence(self, evidence: List[Mapping[str, Any]], locale: str) -> List[str]:
        return [i18n.t(e['key'], locale, **e['args']) for e in evidence]

    def _selection(self, sel: Json, locale: str) -> Json:
        sel['summary'] = dict(sel['summary'], warnings=[self._warning(w, locale) for w in sel['summary']['warnings']])
        return sel

    def _run_summary(self, s: Mapping[str, Any], locale: str) -> Json:
        out = dict(s)
        out['skipped'] = [{'reason': r, 'count': n, 'text': reason_text(locale, r)}
                          for r, n in s['skipped_reasons'].items()]
        out['warnings'] = [self._warning(w, locale) for w in s['warnings']]
        return out

    def _job(self, locale: str) -> Json:
        job = self.app.job()
        if job['summary'] is not None:
            job['summary'] = self._run_summary(job['summary'], locale)
        if job['error_code']:
            job['error_text'] = i18n.t(f'error.{job["error_code"]}', locale)
        return job

    # state and events ------------------------------------------------------------------------------------

    def state(self, r: Request) -> Json:
        app = self.app
        latest = app.latest_scan()
        scan = None if latest is None else {k: latest[k] for k in (
            'id', 'status', 'started_at', 'finished_at', 'domains', 'vol_total', 'vol_free', 'attributed')}
        return {'version': app.events.version, 'locale': r.locale, 'job': self._job(r.locale),
                'settings': asdict(app.settings), 'scan': scan, 'recovery': app.recovery}

    def events(self, r: Request) -> Json:
        since = _query_int(r.query.get('since', '0'), 'since', lo=0)
        try:
            timeout = float(r.query.get('timeout', MAX_LONG_POLL))
        except ValueError:
            raise bad_field('timeout') from None
        if not math.isfinite(timeout) or not 0 <= timeout <= MAX_LONG_POLL:   # 'nan' and 'inf' parse as floats
            raise bad_field('timeout')
        bus = self.app.events
        if since > bus.version:   # the client's counter is from another server run
            return {'version': bus.version, 'events': [], 'reset': True}
        found = bus.wait(since, timeout) if timeout else bus.since(since)
        out = []
        for e in found:
            data = e['data']
            if e['type'] == 'entry.done':
                data = dict(data, reason_text=reason_text(r.locale, data['reason']))
            elif e['type'] == 'run.finished':
                data = dict(data, summary=self._run_summary(data['summary'], r.locale))
            elif e['type'] in ('advisor.failed', 'native.failed', 'data_dir.recreated') and 'message_key' in data:
                data = dict(data, message=_render(data['message_key'], r.locale, data.get('details') or {}))
            out.append(dict(e, data=data))
        # a gap means the bounded log dropped events the client never saw
        return {'version': found[-1]['version'] if found else since, 'events': out,
                'reset': bool(found) and found[0]['version'] > since + 1}

    def job(self, r: Request) -> Json:
        return {'job': self._job(r.locale)}

    def job_control(self, r: Request) -> Json:
        action = r.args[0]
        if action in ('pause', 'resume') and self.app.job()['kind'] == 'scan':
            raise StewardError(ErrorCode.INVALID, 'portal.error.scan_no_pause')
        getattr(self.app, action)()
        return {'job': self._job(r.locale)}

    # scan, overview, selection ---------------------------------------------------------------------------

    def scan_start(self, r: Request) -> Json:
        domains = _body(r).get('domains')
        if domains is not None and (not isinstance(domains, list) or not all(isinstance(d, str) for d in domains)):
            raise bad_field('domains')
        self.app.start_scan(domains)
        return {'job': self._job(r.locale)}

    def overview(self, r: Request) -> Json:
        ov = self.app.overview()
        for g in (ov['top_groups'] + ov['growth']['groups'] + ov['not_included']['report_only']['groups']):
            g['label'] = group_label(g, r.locale)
        ov['cloud_notes'] = cloud_warning_lines(ov.get('cloud_synced') or {}, r.locale)
        ov['filters'] = overview_filters(ov['offered']['min_age_days'])
        return ov

    def quick(self, r: Request) -> Json:
        days = r.query.get('days')
        try:
            sel = self.app.quick(float(days) if days else None)
        except ValueError:
            raise bad_field('days') from None
        return dict(self._selection(sel, r.locale), days=sel['spec']['filter_min_age_days'],
                    default_days=self.app.settings.default_min_age_days)

    def quick_plan(self, r: Request) -> Json:
        body = _body(r)
        days = body.get('days')
        if isinstance(days, bool) or not isinstance(days, (int, float)):
            raise bad_field('days')
        return self._plan_json(self.app.quick_plan(days, body.get('expected')), r.locale)  # type: ignore[arg-type]

    def apps(self, r: Request) -> Json:
        return self.app.app_sizes()   # du over /Applications: slow once, then cached by the App

    def native(self, r: Request) -> Json:
        return {'tools': self.app.native_tools(r.locale)}

    def native_run(self, r: Request) -> Json:
        body = _body(r)
        rule_id, command, target = body.get('rule_id'), body.get('command'), body.get('target')
        _str(rule_id, 'rule_id')
        _str(command, 'command')
        if target is not None:
            _str(target, 'target')
        self.app.start_native(rule_id, target, command)
        return {'job': self._job(r.locale)}

    def native_trash(self, r: Request) -> Json:
        item = _body(r).get('item')
        _str(item, 'item')
        return self._plan_json(self.app.trash_plan(item), r.locale)  # type: ignore[arg-type]

    def insights(self, r: Request) -> Json:
        window = _query_int(r.query.get('window', '30'), 'window')
        if window not in INSIGHTS_WINDOWS:
            raise bad_field('window')
        return self.app.insights(window, r.locale)

    @staticmethod
    def _filter(q: Mapping[str, str]) -> Dict[str, str]:
        return {k: q[k] for k in FILTERS if q.get(k)}

    def _days(self, q: Mapping[str, str]) -> Optional[float]:
        if not q.get('days'):
            return None
        try:
            return float(q['days'])
        except ValueError:
            raise bad_field('days') from None

    def groups(self, r: Request) -> Json:
        """The groups a filter names. With `state` the answer is "what would the quick selection at `days` do", read from
        the quick session; `match` says how much of each group the filter names. `counts=1` adds the numbers for the
        filter menus."""
        flt, days = self._filter(r.query), self._days(r.query)
        preview = 'state' in flt
        session = QUICK_SESSION if preview else SESSION
        if preview:
            self.app.quick(days)
        rows = self.app.groups(domain=flt.get('domain'), tier=flt.get('tier'), session=session)
        scope = self.app.browse_scope(flt, days)
        rows = [dict(row, match=scope['groups'][row['key']]) for row in rows if row['key'] in scope['groups']]
        for row in rows:
            row['label'] = group_label(row, r.locale)
            row['effect'] = group_effect(row, r.locale)
            row['evidence_text'] = self._evidence(row['evidence'], r.locale)
        out: Json = {'groups': rows, 'filter': dict(flt, days=scope['days']) if preview else flt,
                     'scope': {'groups': len(rows), 'items': scope['items'], 'alloc': scope['alloc']}, 'preview': preview}
        if r.query.get('counts') == '1':
            out['counts'] = self.app.browse_counts(days)
        return out

    def items(self, r: Request) -> Json:
        q = r.query
        group = q.get('group') or None
        flt = self._filter(q)
        offset, limit = _query_int(q.get('offset', '0'), 'offset', lo=0), _query_int(q.get('limit', '50'), 'limit', lo=1, hi=MAX_ITEMS_PAGE)
        if 'state' in flt or 'reason' in flt:   # judged by the quick selection, see groups()
            if group is None:
                raise bad_field('group')
            page = self.app.items_matching(group, flt, self._days(q), q.get('order', 'alloc'), offset, limit)
        else:
            page = self.app.items(group=group, order=q.get('order', 'alloc'), offset=offset, limit=limit,
                                  query=q.get('q', ''), session=SESSION)
        basis = {g['key']: g['age_basis'] for g in self.app.groups(session=SESSION)}
        now = self.app.clock()
        keep = ('key', 'path', 'rule_id', 'group_key', 'tier', 'unit', 'size', 'alloc', 'reclaim', 'files', 'newest_ns',
                'last_used_ns', 'nlink', 'taint')
        rows = []
        for item in page['rows']:
            row = {k: item[k] for k in keep}
            row['age_basis'] = basis[item['group_key']]
            row['age_days'] = _age_days(item['newest_ns'], item['last_used_ns'], row['age_basis'], now)
            row['decision'] = self._decision(item['decision'], r.locale)
            row['evidence_text'] = self._evidence(item['evidence'], r.locale)
            rows.append(row)
        return {'total': page['total'], 'rows': rows}

    def spec(self, r: Request) -> Json:
        return self._selection(self.app.selection(SESSION, qualify=True), r.locale)

    def spec_ops(self, r: Request) -> Json:
        return self._selection(self.app.apply_ops(_body(r).get('ops'), SESSION, qualify=True), r.locale)  # type: ignore[arg-type]

    def spec_notes(self, r: Request) -> Json:
        notes = self.app.spec_notes(SESSION)
        return {'notes': notes, 'labels': {} if notes is None else _note_labels(notes, self.labeler(r.locale))}

    # plans -----------------------------------------------------------------------------------------------

    def plan_create(self, r: Request) -> Json:
        return self._plan_json(self.app.create_plan(SESSION, _body(r).get('expected')), r.locale)  # type: ignore[arg-type]

    def _plan_json(self, plan: Json, locale: str) -> Json:
        plan = dict(plan)
        plan['totals'] = dict(plan['totals'], warnings=[self._warning(w, locale) for w in plan['totals']['warnings']])
        return plan

    def plan(self, r: Request) -> Json:
        plan = self._plan_json(self.app.plan(r.args[0]), r.locale)
        plan['pending'] = self.app.pending_totals(r.args[0])
        plan['result'] = self._result(self.app.plan_result(r.args[0]), r.locale)
        return plan

    def _result(self, summary: Optional[Mapping[str, Any]], locale: str) -> Optional[Json]:
        return None if summary is None else self._run_summary(summary, locale)

    def plans(self, r: Request) -> Json:
        found = self.app.plans()
        for key in ('active', 'draft'):
            if found[key] is not None:
                found[key] = self._listed(found[key], r.locale)
        found['finished'] = [self._listed(p, r.locale) for p in found['finished']]
        return found

    def _listed(self, plan: Json, locale: str) -> Json:
        return dict(self._plan_json(plan, locale), result=self._result(plan['result'], locale))

    def plan_groups(self, r: Request) -> Json:
        rows = self.app.plan_groups(r.args[0])
        for g in rows:
            ref = {'key': g['group'], 'rule_id': g['rule_id'], 'label_args': self._label_args(g['group'])}
            g['label'] = group_label(ref, r.locale)
            g['effect'] = group_effect(ref, r.locale)
        return {'groups': rows}

    def plan_discard(self, r: Request) -> Json:
        self.app.discard_plan(r.args[0])
        return {'discarded': True}

    def plan_entries(self, r: Request) -> Json:
        q = r.query
        page = self.app.entries(r.args[0], state=q.get('state') or None,
                                offset=_query_int(q.get('offset', '0'), 'offset', lo=0),
                                limit=_query_int(q.get('limit', '50'), 'limit', lo=1, hi=MAX_ENTRIES_PAGE),
                                group=q.get('group') or None)
        basis = {g['key']: g['age_basis'] for g in self.app.groups(session=SESSION)}
        used = {i['key']: i['last_used_ns'] for i in self._items_by_key([e['item_key'] for e in page['rows']
                                                                          if basis.get(e['group_key']) == 'last_used'])}
        now = self.app.clock()
        rows = []
        for e in page['rows']:
            row = dict(e)
            row['label'] = group_label({'key': e['group_key'], 'rule_id': e['rule_id'],
                                        'label_args': self._label_args(e['group_key'])}, r.locale)
            row['effect'] = group_effect({'key': e['group_key'], 'rule_id': e['rule_id'],
                                          'label_args': self._label_args(e['group_key'])}, r.locale)
            row['age_basis'] = basis.get(e['group_key'], 'changed')
            row['age_days'] = _age_days(_entry_newest_ns(e['fp']), used.get(e['item_key']), row['age_basis'], now)
            row['reason_text'] = reason_text(r.locale, e['reason'])
            rows.append(row)
        return {'total': page['total'], 'rows': rows}

    @staticmethod
    def _label_args(key: str) -> Dict[str, str]:
        suffix = split_group_key(key)[1]
        return {'path': suffix} if suffix else {}

    def _items_by_key(self, keys: List[str]) -> List[Json]:
        """Scan rows of the current scan for these item keys (the plan's own scan may be gone; missing keys are
        simply absent)."""
        if not keys:
            return []
        return self.app.items(keys=keys, limit=None, session=SESSION)['rows']

    def plan_exclude(self, r: Request) -> Json:
        return self._change(r, self.app.exclude, self.app.exclude_group, 'excluded')

    def plan_include(self, r: Request) -> Json:
        return self._change(r, self.app.include, self.app.include_group, 'included')

    def _change(self, r: Request, by_seqs: Callable[..., int], by_group: Callable[..., int], name: str) -> Json:
        """Exclude or include entries named by seq, or every one of a group's."""
        body = _body(r)
        revision = _int(body.get('revision'), 'revision')
        if 'group' in body:
            return {'revision': by_group(r.args[0], _str(body['group'], 'group'), revision)}
        seqs = body.get('seqs')
        if not isinstance(seqs, list) or not all(isinstance(s, int) and not isinstance(s, bool) for s in seqs):
            raise bad_field('seqs')
        return {'revision': by_seqs(r.args[0], seqs, revision), name: len(set(seqs))}

    def plan_run(self, r: Request) -> Json:
        body = _body(r)
        revision = _int(body.get('revision'), 'revision')
        digest = _str(body.get('entries_digest'), 'entries_digest')
        self.app.validate_run(r.args[0], revision, digest)   # refusals as HTTP errors, not as a failed job later
        self.app.start_run(r.args[0], revision, digest)
        return {'job': self._job(r.locale)}

    def preflight(self, r: Request) -> Json:
        pre = self.app.preflight(r.args[0])
        cat = i18n.catalog(r.locale)
        groups = []
        for reason, v in sorted(pre['blockers'].items(), key=lambda kv: (-kv[1]['alloc'], kv[0])):
            number, count = ('one' if v['count'] == 1 else 'other'), f'{v["count"]:,}'
            if reason.startswith('app_running:'):
                bundle = reason.partition(':')[2]
                text = i18n.t(f'portal.preflight.quit_app_{number}', r.locale,
                              app=v.get('app') or cat.get(f'app.{bundle}', bundle), count=count, size=fmt_bytes(v['alloc']))
                kind = 'quit_app'
            elif reason == 'rules_changed':
                text = i18n.t(f'portal.preflight.changed_{number}', r.locale, count=count, size=fmt_bytes(v['alloc']))
                kind = 'changed'
            else:
                text = i18n.t(f'portal.preflight.in_use_{number}', r.locale, reason=blocker_text(r.locale, reason),
                              count=count, size=fmt_bytes(v['alloc']))
                kind = 'in_use'
            groups.append({'reason': reason, 'kind': kind, 'count': v['count'], 'alloc': v['alloc'], 'text': text})
        drift = self.app.drift(r.args[0])
        if drift['count']:
            groups.append({'reason': 'drift', 'kind': 'drift', 'count': drift['count'], 'alloc': drift['alloc'],
                           'text': i18n.t(f'portal.preflight.drift_{"one" if drift["count"] == 1 else "other"}', r.locale,
                                          count=f'{drift["count"]:,}', size=fmt_bytes(drift['alloc']))})
        warnings = list(pre['warnings'])
        try:
            snapshots = self.app.snapshots()
            if snapshots:
                warnings.append({'key': 'warning.snapshots', 'count': len(snapshots)})
        except RuntimeError as e:   # tmutil failing must not hide the rest of the preflight (the run summary does the same)
            warnings.append({'key': 'warning.snapshots_unknown', 'error': str(e)})
        return dict(pre, blocker_groups=groups, warnings=[self._warning(w, r.locale) for w in warnings])

    # archives --------------------------------------------------------------------------------------------

    def archives(self, r: Request) -> Json:
        keep = ('id', 'path', 'fmt', 'orig_size', 'stored_bytes', 'state', 'created_at')
        return {'archives': [dict({k: a[k] for k in keep}, unit=a['manifest']['unit']) for a in self.app.archives()]}

    def archive_action(self, r: Request) -> Json:
        archive_id, action = r.args
        return self.app.restore(archive_id) if action == 'restore' else self.app.purge_archive(archive_id)

    # advisor ---------------------------------------------------------------------------------------------

    def _advisor(self) -> Any:
        try:
            return self.app.advisor
        except (AttributeError, ImportError) as e:
            raise StewardError(ErrorCode.UNAVAILABLE, 'portal.error.advisor_missing',
                               reason=f'{type(e).__name__}: {e}') from e

    def advisor_status(self, r: Request) -> Json:
        """With probe=0 no CLI is run, whatever the state: cached answers only. The advisor tab asks for the full
        status (the CLIs are probed, but only while the advisor is on)."""
        probe = r.query.get('probe', '1') != '0'
        return self._advisor().status(probe=probe)   # type: ignore[no-any-return]

    def advisor_last(self, r: Request) -> Json:
        """The newest advice, for the badges in Browse. Cheap: it never touches a CLI."""
        return {'last': self._advisor().last()}

    def advisor_action(self, r: Request) -> Json:
        action, body = r.args[0], _body(r)
        advisor = self._advisor()
        if action == 'enable':
            enabled = body.get('enabled')
            if not isinstance(enabled, bool):
                raise bad_field('enabled')
            advisor.set_enabled(enabled)
            return advisor.status()   # type: ignore[no-any-return]
        if action == 'preview':
            return advisor.preview()   # type: ignore[no-any-return]
        request_id = _str(body.get('request_id'), 'request_id')
        if action == 'run':
            return dict(advisor.start_run(_str(body.get('provider'), 'provider'), request_id) or {}, started=True)
        if action == 'export':
            return advisor.export_prompt(request_id)   # type: ignore[no-any-return]
        return advisor.import_reply(request_id, _str(body.get('text'), 'text'))   # type: ignore[no-any-return]

    # settings and catalog --------------------------------------------------------------------------------

    def _settings_json(self) -> Json:
        return {'settings': asdict(self.app.settings), 'live_fields': sorted(RUN_SAFE_SETTINGS)}

    def settings_get(self, r: Request) -> Json:
        return self._settings_json()

    def settings_put(self, r: Request) -> Json:
        self.app.update_settings(_body(r))
        return self._settings_json()

    def i18n_catalog(self, r: Request) -> Json:
        return i18n.catalog(r.args[0])
