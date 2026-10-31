"""`insights`: environment, history, current state, results and follow-up TODOs as text, or as the portal's JSON."""
from __future__ import annotations

import argparse
from typing import Any, List, Mapping

from .. import i18n
from ..report import fmt_bytes, fmt_time, table
from ..service import App
from .commands import EXIT_OK, Translator, dump

DEFAULT_WINDOW = 30


def configure(p: argparse.ArgumentParser) -> None:
    p.add_argument('--window', type=int, default=DEFAULT_WINDOW, metavar='DAYS',
                   help=f'days of history to cover, 0 for all of it (default: {DEFAULT_WINDOW})')
    p.add_argument('--json', action='store_true', help='print JSON')


def insights(app: App, args: argparse.Namespace, T: Translator) -> int:
    data = app.insights(args.window, app.locale)
    if args.json:
        dump(data)
    else:
        print(render_text(data, app.locale), end='')
    return EXIT_OK


def _signed(n: int) -> str:
    return fmt_bytes(n, signed=True)


def _tool(env: Mapping[str, Any], name: str, T: Translator) -> str:
    if env['clis_errors'].get(name):
        return T('insights.cli.env.tool_error', error=env['clis_errors'][name])
    return env['clis'][name] or T('insights.cli.env.tool_missing')


def _environment(data: Mapping[str, Any], locale: str, T: Translator) -> List[str]:
    env = data['environment']
    vol = env['volume']
    out = [T('insights.cli.env.system', macos=env['macos'], arch=env['arch'], python=env['python'],
             version=env['tool_version']),
           T('insights.cli.env.volume', total=fmt_bytes(vol['total']), used=fmt_bytes(vol['used']),
             free=fmt_bytes(vol['free']))]
    if env['snapshots'] is None:
        out.append(T('insights.cli.env.snapshots_unknown', error=env['snapshots_error']))
    else:
        out.append(T('insights.cli.env.snapshots', count=env['snapshots']))
    out.append(T('insights.cli.env.data_dir', path=env['data_dir'], size=fmt_bytes(env['data_dir_bytes'])))
    out.append(T('insights.cli.env.tools', **{n: _tool(env, n, T) for n in ('codex', 'claude', 'ollama')}))
    job = env['schedule']
    if job['installed'] and job['weekday'] is not None and job['hour'] is not None:
        when = T('cli.schedule.when', day=T(f'cli.schedule.weekday.{job["weekday"] % 7}'), hour=job['hour'])
        out.append(T('insights.cli.env.schedule_on', when=when))
    elif job['installed']:   # a plist edited by hand
        out.append(T('insights.cli.env.schedule_on', when='-'))
    else:
        out.append(T('insights.cli.env.schedule_off'))
    out.append(T('insights.cli.env.advisor_on' if env['advisor_enabled'] else 'insights.cli.env.advisor_off'))
    out.append(T('insights.cli.env.areas', areas=', '.join(T(f'domain.{d}') for d in env['areas']) or '-'))
    last = env['last_scan']
    if last is None:
        out.append(T('insights.cli.env.no_scan'))
    else:
        out.append(T('insights.cli.env.last_scan', id=last['id'], finished=fmt_time(last['finished_at']),
                     duration=last['duration_s'], items=f'{last["items"]:,}', privacy=last['privacy_skipped'],
                     errors=last['errors']))
    return out


def _kpis(data: Mapping[str, Any], T: Translator) -> List[str]:
    k = data['kpis']
    out = [T('insights.cli.kpi.free', value=fmt_bytes(k['free']['value']), delta=_signed(k['free']['delta']))]
    scanned = k['scanned']
    if scanned['value'] is None:
        out.append(T('insights.cli.kpi.scanned_none'))
    elif scanned['delta'] is None:
        out.append(T('insights.cli.kpi.scanned_flat', value=fmt_bytes(scanned['value'])))
    else:
        out.append(T('insights.cli.kpi.scanned', value=fmt_bytes(scanned['value']), delta=_signed(scanned['delta'])))
    freed = k['freed_total']
    if freed['count']:
        out.append(T('insights.cli.kpi.freed', value=fmt_bytes(freed['value']), count=freed['count'],
                     since=fmt_time(freed['since'])))
    else:
        out.append(T('insights.cli.kpi.freed_none'))
    safe = k['safe_to_clean']
    out.append(T('insights.cli.kpi.safe' if safe['days'] else 'insights.cli.kpi.safe_any',
                 size=fmt_bytes(safe['bytes']), items=f'{safe["items"]:,}', days=safe['days']))
    if k['growth_per_day'] is None:
        out.append(T('insights.cli.kpi.growth_none'))
    else:
        out.append(T('insights.cli.kpi.growth', size=_signed(k['growth_per_day'])))
    return out


def _composition(data: Mapping[str, Any], T: Translator) -> str:
    total = sum(c['bytes'] for c in data['composition']) or 1
    return table([(T('insights.cli.col.part'), T('cli.col.size'), T('insights.cli.col.share'))] +
                 [(c['label'], fmt_bytes(c['bytes']), f'{100 * c["bytes"] / total:.1f}%')
                  for c in data['composition']], 'lrr')


def _history(data: Mapping[str, Any], T: Translator) -> List[str]:
    scans = data['history']['scans']
    if not scans:
        return [T('insights.cli.empty.history')]
    rows = [(T('insights.cli.col.scan'), T('insights.cli.col.date'), T('insights.cli.col.used'),
             T('insights.cli.col.free'), T('insights.cli.col.scanned'), T('insights.cli.col.junk'))]
    for s in scans:
        mark = '~' if s['backfilled'] else ''
        rows.append((f'#{s["id"]}', fmt_time(s['t']), fmt_bytes(s['used']), fmt_bytes(s['free']),
                     fmt_bytes(s['scanned']), mark + fmt_bytes(s['junk'])))
    out = [table(rows, 'llrrrr')]
    if any(s['backfilled'] for s in scans):
        out.append(T('insights.cli.history_note'))
    return out


def _cleanups(data: Mapping[str, Any], T: Translator) -> List[str]:
    rows = data['history']['cleanups']
    if not rows:
        return [T('insights.cli.empty.cleanups')]
    head = (T('insights.cli.col.date'), T('insights.cli.col.done'), T('insights.cli.col.skipped'),
            T('insights.cli.col.failed'), T('insights.cli.col.removed'), T('insights.cli.col.freed'),
            T('insights.cli.col.measured'))
    return [table([head] + [(fmt_time(c['finished_at']), str(c['items_done']), str(c['items_skipped']),
                             str(c['items_failed']), fmt_bytes(c['removed']), fmt_bytes(c['freed']),
                             '-' if c['measured_delta'] is None else _signed(c['measured_delta'])) for c in rows],
                 'lrrrrrr')]


def _growth(rows: List[Mapping[str, Any]], T: Translator) -> str:
    head = (T('cli.col.change'), T('insights.cli.col.per_day'), T('insights.cli.col.now'), T('cli.col.group'))
    return table([head] + [(_signed(r['delta']), _signed(r['per_day']), fmt_bytes(r['now']), r['label'])
                           for r in rows], 'rrrl')


def _agents(data: Mapping[str, Any], T: Translator) -> str:
    if not data['agents']:
        return T('insights.cli.empty.agents')
    return table([(T('insights.cli.col.agent'), T('cli.col.size'), T('cli.col.change'))] +
                 [(a['label'], fmt_bytes(a['bytes']), _signed(a['delta'])) for a in data['agents']], 'lrr')


def _todos(data: Mapping[str, Any], T: Translator) -> List[str]:
    if not data['todos']:
        return [T('insights.cli.empty.todos')]
    out: List[str] = []
    for n, todo in enumerate(data['todos'], 1):
        out.append(f'{n:>2}. [{T("insights.severity." + todo["severity"])}] {todo["title"]}')
        out.append(f'    {todo["detail"]}')
        if todo['command']:
            out.append('    ' + T('insights.cli.todo.command', command=todo['command']))
    return out


def render_text(data: Mapping[str, Any], locale: str) -> str:
    def T(key: str, **args: Any) -> str:
        return i18n.t(key, locale, **args)

    window = data['window_days']
    out = [T('insights.cli.title', days=window) if window else T('insights.cli.title_all')]

    def section(name: str, *body: Any) -> None:
        out.extend(['', T(f'insights.cli.section.{name}')])
        for part in body:
            out.extend(part if isinstance(part, list) else [part])

    section('environment', ['  ' + line for line in _environment(data, locale, T)])
    section('kpis', ['  ' + line for line in _kpis(data, T)])
    section('composition', _composition(data, T))
    section('history', _history(data, T))
    section('cleanups', _cleanups(data, T))
    growth = data['growth']
    if growth['up'] or growth['down']:
        if growth['up']:
            section('growth_up', _growth(growth['up'], T))
        if growth['down']:
            section('growth_down', _growth(growth['down'], T))
    else:
        section('growth_up', T('insights.cli.empty.growth'))
    section('agents', _agents(data, T))
    if 'largest_apps' in data:
        section('apps', table([(T('cli.col.size'), T('cli.col.path'))] +
                              [(fmt_bytes(a['bytes']), a['path']) for a in data['largest_apps']], 'rl'))
    section('todos', _todos(data, T))
    return '\n'.join(out) + '\n'
