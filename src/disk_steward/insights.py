"""Insights: the environment, the history of scans and cleanups, the current state and the follow-up TODOs as one
JSON-safe dict, with every text already in the caller's locale.

Nothing here walks the disk. The numbers come from scans, group_history, scan_totals, plans and meta; the only live
queries are the volume, tmutil, lsappinfo and the version of three tools (cached for ten minutes)."""
from __future__ import annotations

import concurrent.futures
import os
import platform
import re
import subprocess
import threading
import weakref
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple
from urllib.parse import quote

from . import __version__, i18n, queries
from . import schedule as sched
from .catalog import Catalog
from .classify import split_group_key
from .macos import MacOS
from .model import Domain, Group, StewardError, Tier
from .paths import Bases
from .report import fmt_bytes, group_effect, group_label
from .settings import Settings
from .store import Store, group_key_class

DAY = 86400.0
MIB = 1024 * 1024
GIB = 1024 * MIB
REVIEW_MIN = 500 * MIB          # a review group this big gets its own TODO
REPORT_ONLY_MIN = 200 * MIB     # likewise for a report-only group that has a command
GROWTH_TOTAL = GIB              # a group that grew this much in the window
GROWTH_PER_DAY = 200 * MIB      # or this much per day
STALE_SCAN_DAYS = 7
OLD_ARCHIVE_DAYS = 30
SNAPSHOT_HOLD_RATIO = 0.5       # a cleanup that freed less than this share of its estimate left space held back
TOP_REVIEW = TOP_REPORT_ONLY = TOP_GROWTH_ALERTS = 3
TOP_UP, TOP_DOWN, TOP_APPS = 8, 5, 5
MAX_TODOS = 12
TOOLS = ('codex', 'claude', 'ollama')
TOOL_TTL = 600.0
SEVERITIES = ('act', 'warn', 'review', 'tip')
COMPOSITION = ('free', 'outside', 'protected', 'review', 'report_only', 'junk')
CODEX_DUPLICATES = 'codex.sessions:duplicates'
QUICK_HREF = '#/overview?quick=1'

_VERSION = re.compile(r'\d+(?:\.\d+)+')
_SENTENCE_END = re.compile(r'[。！？]|[.!?](?=\s|$)')

# Tool versions cost a process start each (ollama's takes half a second), so they are asked for once per ten
# minutes per MacOS adapter. Keyed by the adapter so that tests with different fakes never share an answer.
_tool_cache: 'weakref.WeakKeyDictionary[Any, Dict[str, Tuple[float, Optional[str], str]]]' = weakref.WeakKeyDictionary()
_tool_lock = threading.Lock()


class Parts(NamedTuple):
    """What build() reads from the App, so this module never imports the service."""
    store: Store
    mac: MacOS
    bases: Bases
    settings: Settings
    catalog: Catalog
    data_dir: Path
    app_sizes: Optional[Mapping[str, Any]]   # App.app_sizes()'s cached answer, None until someone measured
    native: Any                              # native.Probe: whether a report-only tool's command can run here

    @classmethod
    def of(cls, app: Any) -> 'Parts':
        return cls(app.store, app.mac, app.bases, app.settings, app._load_catalog(), app.data_dir, app._app_sizes,
                   app._native)


# small helpers ---------------------------------------------------------------------------------------------

def _plural(key: str, count: int) -> str:
    return f'{key}_{"one" if count == 1 else "other"}'


def _num(value: float) -> Any:
    """14.0 reads as 14 in a sentence; 0.5 stays."""
    return int(value) if float(value).is_integer() else value


def _first_sentence(text: str) -> str:
    m = _SENTENCE_END.search(text)
    return text[:m.end()].strip() if m else text.strip()


def _group_args(key: str) -> Dict[str, Any]:
    """The label of a group from its key alone: the scanner builds label_args the same way, and the groups of an
    older scan are gone."""
    rule_id, suffix = split_group_key(key)
    return {'key': key, 'rule_id': rule_id, 'label_args': {'path': suffix} if suffix else {}}


def _group_href(key: str) -> str:
    return '#/browse?group=' + quote(key, safe='')


def _data_dir_bytes(path: Path) -> int:
    """Allocated size of everything in the data folder (database, archives, logs): a folder of a few dozen files,
    unlike the disk a scan walks."""
    total = 0
    stack = [str(path)]
    while stack:
        with os.scandir(stack.pop()) as entries:
            for entry in entries:
                try:
                    st = entry.stat(follow_symlinks=False)
                except FileNotFoundError:   # a staged file the run just removed
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                else:
                    total += st.st_blocks * 512
    return total


# environment -----------------------------------------------------------------------------------------------

def _short_version(raw: str) -> str:
    found = _VERSION.search(raw)
    return found.group(0) if found else raw


def _probe_tool(mac: MacOS, name: str) -> Tuple[Optional[str], str]:
    try:
        raw = mac.tool_version(name)
    except (RuntimeError, subprocess.TimeoutExpired) as e:   # installed, but --version failed: shown, not hidden
        return None, f'{type(e).__name__}: {e}'
    return (None if raw is None else _short_version(raw)), ''


def _tool_versions(mac: MacOS, now: float) -> Tuple[Dict[str, Optional[str]], Dict[str, str]]:
    with _tool_lock:
        cache = _tool_cache.setdefault(mac, {})
        stale = [n for n in TOOLS if n not in cache or now - cache[n][0] >= TOOL_TTL]
    if stale:
        with concurrent.futures.ThreadPoolExecutor(len(stale)) as pool:
            answers = list(pool.map(lambda n: _probe_tool(mac, n), stale))
        with _tool_lock:
            for name, (version, error) in zip(stale, answers):
                cache[name] = (now, version, error)
    with _tool_lock:
        found = {n: cache[n] for n in TOOLS}
    return ({n: v for n, (_, v, _e) in found.items()}, {n: e for n, (_, _v, e) in found.items() if e})


def _environment(parts: Parts, latest: Optional[Mapping[str, Any]], groups: Sequence[Group],
                 errors: Mapping[str, int], free_now: int, total: int, now: float) -> Dict[str, Any]:
    mac = parts.mac
    snapshots: Optional[int] = None
    snapshots_error = ''
    try:
        snapshots = len(mac.snapshots())
    except RuntimeError as e:   # tmutil failing leaves the count unknown; the page says why
        snapshots_error = str(e)
    try:
        data_dir = parts.bases.to_logical(parts.data_dir)
    except StewardError:   # a data folder outside ~, $TMPDIR and $CACHEDIR has no logical name
        data_dir = str(parts.data_dir)
    clis, clis_errors = _tool_versions(mac, now)
    job = sched.status(parts.data_dir)
    last = None
    if latest is not None:
        last = {'id': latest['id'], 'finished_at': latest['finished_at'],
                'duration_s': round(latest['finished_at'] - latest['started_at'], 1),
                'items': sum(g.items for g in groups),
                'privacy_skipped': errors.get('skipped_privacy', 0),
                'errors': sum(n for code, n in errors.items() if code != 'skipped_privacy')}
    env: Dict[str, Any] = {
        'macos': platform.mac_ver()[0], 'arch': platform.machine(), 'python': platform.python_version(),
        'tool_version': __version__,
        'volume': {'total': total, 'used': total - free_now, 'free': free_now},
        'snapshots': snapshots, 'data_dir': data_dir, 'data_dir_bytes': _data_dir_bytes(parts.data_dir),
        'clis': clis, 'clis_errors': clis_errors, 'advisor_enabled': parts.settings.advisor_enabled,
        'schedule': {'installed': job['installed'], 'weekday': job['weekday'], 'hour': job['hour']},
        'areas': list(parts.settings.domains), 'last_scan': last}
    if snapshots is None:
        env['snapshots_error'] = snapshots_error
    return env


# history and growth ----------------------------------------------------------------------------------------

def _series(scans: Sequence[Mapping[str, Any]], field: str) -> List[Dict[str, Any]]:
    return [{'t': s['finished_at'], 'v': s[field]} for s in scans]


def _composition(totals: Optional[Mapping[str, Any]], total: int, free: int, locale: str) -> List[Dict[str, Any]]:
    """The volume as six bars that add up to its size. `outside` is what the scan did not attribute. A scan can
    attribute more than is in use (APFS clones and sparse files count twice), so the four scanned bars are then
    scaled down together; a negative bar would be worse than a slightly smaller one."""
    used = total - free
    names = ('protected', 'review', 'report_only', 'junk')
    scanned = {n: (totals[n] if totals else 0) for n in names}
    outside = used - sum(scanned.values())
    if outside < 0:
        have = sum(scanned.values())
        scaled = {n: scanned[n] * used // have for n in names}
        scaled[max(names, key=lambda n: scanned[n])] += used - sum(scaled.values())
        scanned, outside = scaled, 0
    amounts = dict(scanned, free=free, outside=outside)
    return [{'key': k, 'bytes': amounts[k], 'label': i18n.t(f'insights.composition.{k}', locale)}
            for k in COMPOSITION]


def _agent_roots(catalog: Catalog) -> List[Tuple[str, str]]:
    """(root path, provider) of every agent root, longest path first. A root's provider is the one whose rules
    live under it; a root no rule covers is named after the provider its folder name contains, or after the
    folder itself (~/.gemini)."""
    known = sorted({r.provider for r in catalog.rules if r.domain == Domain.AGENT and r.provider != 'system'})
    out = []
    for root in catalog.roots:
        if root.domain != Domain.AGENT:
            continue
        provider = next((r.provider for r in catalog.rules
                         if r.domain == Domain.AGENT and r.provider != 'system'
                         and any(p == root.path or p.startswith(root.path + '/') for p in r.patterns)), None)
        if provider is None:   # com.openai.codex, Claude-3p: named after the provider they contain
            name = root.path.rpartition('/')[2].lstrip('.').lower()
            provider = next((p for p in known if p in name), name)
        out.append((root.path, provider))
    return sorted(out, key=lambda r: -len(r[0]))


def _agent_provider(key: str, catalog: Catalog, roots: Sequence[Tuple[str, str]]) -> Optional[str]:
    rule_id, suffix = split_group_key(key)
    try:
        rule = catalog.rule(rule_id)
    except KeyError:   # a rule this catalog no longer has: only its path can tell
        rule = None
    if rule is not None and rule.domain == Domain.AGENT and rule.provider != 'system':
        return rule.provider
    for path, provider in roots:   # protections and unclaimed folders carry the path in their key
        if suffix == path or suffix.startswith(path + '/'):
            return provider
    return None


def _agents(now_alloc: Mapping[str, int], before_alloc: Optional[Mapping[str, int]], catalog: Catalog,
            locale: str) -> List[Dict[str, Any]]:
    roots = _agent_roots(catalog)
    now_by: Dict[str, int] = Counter()
    before_by: Dict[str, int] = Counter()
    for source, acc in ((now_alloc, now_by), (before_alloc or {}, before_by)):
        for key, alloc in source.items():
            provider = _agent_provider(key, catalog, roots)
            if provider is not None:
                acc[provider] += alloc
    cat = i18n.catalog(locale)
    rows = [{'provider': p, 'label': cat.get(f'provider.{p}', p.capitalize()), 'bytes': b,
             'delta': b - before_by[p] if before_alloc is not None else 0}
            for p, b in now_by.items() if b > 0]
    return sorted(rows, key=lambda r: (-r['bytes'], r['provider']))


MIN_RATE_DAYS = 2.0


def _growth(now_alloc: Mapping[str, int], before_alloc: Optional[Mapping[str, int]], span_days: float,
            groups: Mapping[str, Group], catalog: Catalog, locale: str) -> Tuple[List[Dict], List[Dict]]:
    if before_alloc is None:
        return [], []
    # Rescans minutes apart (every cleanup ends with one) must not turn a 50 MiB change into 70 GiB a day.
    days = max(span_days, 1.0)
    rows = []
    for key in sorted(set(now_alloc) | set(before_alloc)):
        now, before = now_alloc.get(key, 0), before_alloc.get(key, 0)
        if now == before:
            continue
        group = groups.get(key)
        tier, domain = ((group.tier.value, group.domain.value) if group is not None
                        else group_key_class(key, catalog)[:2])
        rows.append({'group': key, 'label': group_label(_group_args(key), locale), 'domain': domain, 'tier': tier,
                     'delta': now - before, 'per_day': int(round((now - before) / days)), 'now': now,
                     'before': before})
    up = sorted((r for r in rows if r['delta'] > 0), key=lambda r: (-r['delta'], r['group']))
    down = sorted((r for r in rows if r['delta'] < 0), key=lambda r: (r['delta'], r['group']))
    return up, down


# the quick selection ---------------------------------------------------------------------------------------

def _quick(parts: Parts, scan_id: int, now: float) -> Dict[str, Any]:
    """What the overview offers as "safe to clean": every junk group with an action in an enabled area, judged with
    the default age filter (queries.quick_totals gives selection.resolve_for's answer for that spec; nothing is
    saved)."""
    settings = parts.settings
    days = settings.default_min_age_days
    found = queries.quick_totals(parts.store, scan_id, now, days, settings.domains, parts.mac.trash_available())
    return {'days': days, 'bytes': sum(a for _, a in found.values()), 'items': sum(n for n, _ in found.values()),
            'by_group': {k: a for k, (_, a) in found.items() if a}, 'groups': len(found)}


def _blocked_apps(parts: Parts, by_group: Mapping[str, int], groups: Mapping[str, Group],
                  locale: str) -> Tuple[List[Tuple[str, str, int]], str]:
    """(bundle id, app name, bytes) of running apps that hold back part of the quick selection, biggest first, and
    the error text when the running apps could not be listed. A group needs the apps its rule names plus those its
    items' evidence names (generic Electron caches learn their app from the detector at scan time)."""
    needs: Dict[str, Tuple[str, int]] = {}   # bundle id -> (name, bytes of groups that need it)
    for key, alloc in by_group.items():
        group = groups[key]
        try:
            names = {b: '' for b in parts.catalog.rule(group.rule_id).quit_apps}
        except KeyError:   # the rule is gone; a run would refuse the plan (rules changed) before any app mattered
            continue
        for ev in group.evidence:
            if ev.key == 'evidence.requires_quit':
                names[ev.args['bundle_id']] = ev.args.get('app', '')
        for bundle, name in names.items():
            known = needs.get(bundle, (name, 0))
            needs[bundle] = (known[0] or name, known[1] + alloc)
    if not needs:
        return [], ''
    try:
        running = parts.mac.running_bundle_ids()
    except RuntimeError as e:   # lsappinfo failed: the page says so instead of pretending nothing is blocked
        return [], str(e)
    cat = i18n.catalog(locale)
    rows = [(b, cat.get(f'app.{b}') or cat.get(f'insights.app.{b}') or name or b, size)
            for b, (name, size) in needs.items() if b in running]
    return sorted(rows, key=lambda r: (-r[2], r[0])), ''


# TODOs -----------------------------------------------------------------------------------------------------

def _todo(todo_id: str, severity: str, title: str, detail: str, size: Optional[int] = None,
          action: Optional[Tuple[str, str]] = None, command: Optional[str] = None,
          runnable: Optional[bool] = None) -> Dict[str, Any]:
    out = {'id': todo_id, 'severity': severity, 'title': title, 'detail': detail, 'bytes': size,
           'action': None if action is None else {'label': action[0], 'href': action[1]}, 'command': command}
    if runnable is not None:   # a report-only tip: the page can offer a Run button when its tool can run here
        out['runnable'] = runnable
    return out


def _todos(parts: Parts, locale: str, now: float, latest: Optional[Mapping[str, Any]], groups: Sequence[Group],
           quick: Optional[Dict[str, Any]], growth_up: Sequence[Mapping[str, Any]], span_days: float,
           cleanups: Sequence[Mapping[str, Any]], snapshots: Optional[int],
           stale_archives: Tuple[int, int], schedule_installed: bool) -> List[Dict[str, Any]]:
    def T(key: str, **args: Any) -> str:
        return i18n.t(f'insights.todo.{key}', locale, **args)

    out: List[Dict[str, Any]] = []
    enabled = set(parts.settings.domains)
    by_key = {g.key: g for g in groups}

    if latest is None:                                                                                    # 7
        out.append(_todo('first_scan', 'act', T('first_scan.title'), T('first_scan.detail'),
                         action=(T('first_scan.action'), '#/overview?scan=1')))
    else:
        age_days = int((now - latest['finished_at']) // DAY)
        if now - latest['finished_at'] > STALE_SCAN_DAYS * DAY:
            out.append(_todo('scan_stale', 'warn', T(_plural('scan_stale.title', age_days), days=age_days),
                             T('scan_stale.detail'), action=(T('scan_stale.action'), '#/overview?scan=1')))

    if quick is not None and quick['bytes'] > 0:                                                          # 1
        days = quick['days']
        detail = T('clean_junk.detail' if days else 'clean_junk.detail_any', items=f'{quick["items"]:,}',
                   groups=quick['groups'], days=_num(days))
        out.append(_todo('clean_junk', 'act', T('clean_junk.title', size=fmt_bytes(quick['bytes'])), detail,
                         quick['bytes'], (T('clean_junk.action'), QUICK_HREF)))
        blocked, error = _blocked_apps(parts, quick['by_group'], by_key, locale)                          # 2
        for bundle, name, size in blocked:
            out.append(_todo(f'quit_app:{bundle}', 'act', T('quit_app.title', app=name, size=fmt_bytes(size)),
                             T('quit_app.detail', app=name, size=fmt_bytes(size)), size,
                             (T('quit_app.action'), QUICK_HREF)))
        if error:
            out.append(_todo('running_apps_unknown', 'warn', T('running_apps_unknown.title'),
                             T('running_apps_unknown.detail', error=error)))

    visible = [g for g in groups if g.domain.value in enabled]
    reviews = sorted((g for g in visible if g.tier == Tier.REVIEW and g.actions and g.alloc >= REVIEW_MIN
                      and g.key != CODEX_DUPLICATES), key=lambda g: (-g.alloc, g.key))
    for g in reviews[:TOP_REVIEW]:                                                                        # 3
        out.append(_todo(f'review:{g.key}', 'review',
                         T('review.title', label=group_label(g, locale), size=fmt_bytes(g.alloc)),
                         _first_sentence(group_effect(g, locale)), g.alloc,
                         (T('review.action'), _group_href(g.key))))

    # Not limited to enabled areas: dev-shared groups are found through app-library's roots and never selectable.
    reports = sorted((g for g in groups if g.tier != Tier.PROTECTED and not g.actions and g.hint
                      and g.alloc >= REPORT_ONLY_MIN), key=lambda g: (-g.alloc, g.key))
    for g in reports[:TOP_REPORT_ONLY]:                                                                   # 4
        out.append(_todo(f'report_only:{g.key}', 'tip',
                         T('report_only.title', hint=g.hint, size=fmt_bytes(g.alloc)),
                         T('report_only.detail', label=group_label(g, locale)), g.alloc,
                         (T('report_only.action'), '#/browse?report_only=1'), command=g.hint,
                         runnable=parts.native.state(g.rule_id).runnable))

    alerts = [r for r in growth_up if r['delta'] >= GROWTH_TOTAL or r['per_day'] >= GROWTH_PER_DAY]
    span = max(int(round(span_days)), 1)
    for r in alerts[:TOP_GROWTH_ALERTS]:                                                                  # 5
        out.append(_todo(f'growth:{r["group"]}', 'warn',
                         T(_plural('growth.title', span), label=r['label'], delta=fmt_bytes(r['delta']), days=span),
                         T('growth.detail', before=fmt_bytes(r['before']), now=fmt_bytes(r['now']),
                           per_day=fmt_bytes(r['per_day'])), r['delta'],
                         (T('growth.action'), _group_href(r['group']))))

    dupes = by_key.get(CODEX_DUPLICATES)
    if dupes is not None and dupes.alloc > 0 and dupes.domain.value in enabled:                           # 6
        out.append(_todo('codex_duplicates', 'review', T('codex_duplicates.title', size=fmt_bytes(dupes.alloc)),
                         _first_sentence(group_effect(dupes, locale)), dupes.alloc,
                         (T('codex_duplicates.action'), _group_href(CODEX_DUPLICATES))))

    if not schedule_installed:                                                                            # 8
        out.append(_todo('schedule', 'tip', T('schedule.title'), T('schedule.detail'),
                         action=(T('schedule.action'), '#/settings'), command='disk-steward schedule install'))

    count, size = stale_archives                                                                          # 9
    if count:
        out.append(_todo('archives_old', 'tip',
                         T(_plural('archives_old.title', count), count=count, size=fmt_bytes(size)),
                         T('archives_old.detail'), size, (T('archives_old.action'), '#/archives')))

    last = next((c for c in cleanups if c['kind'] == 'plan'), None)                                       # 10
    if (snapshots and last is not None and last['measured_delta'] is not None and last['estimated']
            and last['measured_delta'] < SNAPSHOT_HOLD_RATIO * last['estimated']):
        held = last['estimated'] - last['measured_delta']
        out.append(_todo('snapshots_held', 'tip', T(_plural('snapshots_held.title', snapshots), n=snapshots),
                         T('snapshots_held.detail', estimated=fmt_bytes(last['estimated']),
                           measured=fmt_bytes(last['measured_delta'])), held))

    rank = {s: i for i, s in enumerate(SEVERITIES)}
    out.sort(key=lambda t: (rank[t['severity']], -(t['bytes'] if t['bytes'] is not None else -1)))
    return out[:MAX_TODOS]


# build -----------------------------------------------------------------------------------------------------

def build(app_or_parts: Any, window_days: int, locale: str, now: float) -> Dict[str, Any]:
    """The Insights page. `app_or_parts` is a Parts or an App. `window_days` limits the history, growth and KPI
    deltas to the last N days (0 = all of it); the composition, agents' sizes and TODOs always describe the latest
    scan."""
    parts = app_or_parts if isinstance(app_or_parts, Parts) else Parts.of(app_or_parts)
    store, mac, catalog = parts.store, parts.mac, parts.catalog
    since = None if window_days == 0 else now - window_days * DAY

    with store.read_snapshot():   # a scan finishing meanwhile prunes rows these reads still need
        scans = queries.done_scans(store)
        totals = queries.scan_total_rows(store)
        cleanups = sorted(queries.cleanup_rows(store) + queries.native_rows(store),
                          key=lambda r: (-r['finished_at'], r['plan_id'] or '', r.get('command') or ''))
        stale_archives = queries.stale_archives(store, now - OLD_ARCHIVE_DAYS * DAY)
        latest = store.latest_scan()
        groups = store.groups(latest['id']) if latest is not None else []
        errors = Counter(e['error'] for e in store.scan_errors(latest['id'])) if latest is not None else Counter()
        inside = [s for s in scans if since is None or s['finished_at'] >= since]
        first = inside[0] if inside else None
        now_alloc = queries.history_allocs(store, latest['id']) if latest is not None else {}
        compared = first is not None and latest is not None and first['id'] != latest['id']
        before_alloc = queries.history_allocs(store, first['id']) if compared else None
        quick = _quick(parts, latest['id'], now) if latest is not None else None

    home = str(parts.bases.home)
    total, _used = mac.volume_usage(home)
    free_now = mac.statfs_free(home)
    by_key = {g.key: g for g in groups}
    span_days = (inside[-1]['finished_at'] - first['finished_at']) / DAY if compared else 0.0
    up, down = _growth(now_alloc, before_alloc, span_days, by_key, catalog, locale)
    env = _environment(parts, latest, groups, errors, free_now, total, now)

    free_series = _series(inside, 'vol_free') + [{'t': now, 'v': free_now}]
    scanned_series = _series(inside, 'attributed')
    scanned_value = None if latest is None else latest['attributed']
    # A rate needs history: over less than two days one cleanup or one big download reads as a daily trend.
    growth_per_day = None
    if len(inside) >= 2 and span_days >= MIN_RATE_DAYS:
        growth_per_day = int(round((inside[-1]['attributed'] - inside[0]['attributed']) / span_days))
    kpis = {
        'free': {'value': free_now, 'delta': free_now - free_series[0]['v'], 'series': free_series},
        'scanned': {'value': scanned_value, 'delta': (scanned_value - scanned_series[0]['v']) if scanned_series
                    else None, 'series': scanned_series},
        'freed_total': {'value': sum(c['freed'] for c in cleanups), 'count': len(cleanups),
                        'since': min((c['created_at'] for c in cleanups), default=None)},
        'safe_to_clean': {'bytes': quick['bytes'] if quick else 0, 'items': quick['items'] if quick else 0,
                          'days': quick['days'] if quick else parts.settings.default_min_age_days},
        'growth_per_day': growth_per_day}

    history_scans = []
    for s in inside:
        t = totals.get(s['id'], {})
        history_scans.append({'id': s['id'], 't': s['finished_at'], 'used': s['vol_total'] - s['vol_free'],
                              'free': s['vol_free'], 'scanned': s['attributed'], 'junk': t.get('junk', 0),
                              'review': t.get('review', 0), 'protected': t.get('protected', 0),
                              'report_only': t.get('report_only', 0), 'backfilled': t.get('backfilled', False)})
    out: Dict[str, Any] = {
        'generated_at': now, 'window_days': window_days, 'environment': env, 'kpis': kpis,
        'composition': _composition(totals.get(latest['id']) if latest is not None else None, total, free_now,
                                    locale),
        'history': {'scans': history_scans,
                    'cleanups': [c for c in cleanups if since is None or c['finished_at'] >= since]},
        'growth': {'up': up[:TOP_UP], 'down': down[:TOP_DOWN]},
        'agents': _agents(now_alloc, before_alloc if compared else None, catalog, locale),
        'todos': _todos(parts, locale, now, latest, groups, quick, up, span_days, cleanups, env['snapshots'],
                        stale_archives, env['schedule']['installed'])}
    if parts.app_sizes is not None:
        sized = [e for e in parts.app_sizes['entries'] if 'bytes' in e][:TOP_APPS]
        out['largest_apps'] = [{'path': e['path'], 'bytes': e['bytes']} for e in sized]
    return out
