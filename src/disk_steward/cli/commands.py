"""CLI commands. Each takes the App, the parsed args and a translator, prints, and returns the exit code."""
from __future__ import annotations

import argparse
import fnmatch
import json
import shutil
import signal
import subprocess
import sys
import threading
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .. import i18n
from .. import schedule as sched
from ..model import ErrorCode, StewardError
from ..paths import match_pattern
from ..report import (PRIVACY_CODE, cloud_roots_of, cloud_synced, cloud_warning_lines, display_width, fmt_bytes,
                      fmt_time, render_apps, render_growth, render_text, table)
from ..service import App

Translator = Callable[..., str]
SESSION = 'cli'
TOP_GROUPS = 15
MAX_QUIET_WARNINGS = 5
EXIT_OK, EXIT_SCAN_ERRORS, EXIT_ERROR, EXIT_BUSY = 0, 1, 2, 3
EXIT_NOT_FINISHED = 1   # apply: the run was cancelled before it handled every entry
TOP_SKIPPED = 10
TOP_IN_USE = 5


def dump(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def _fit(text: str, width: int) -> str:
    while display_width(text) > width and text:
        text = text[:-1]
    return text


def _scan_progress(T: Translator) -> Callable[[Dict[str, Any]], None]:
    def show(event: Dict[str, Any]) -> None:
        if event['phase'] == 'last_used':
            text = T('cli.scan.progress_last_used')
        else:
            text = T('cli.scan.progress', files=event['files'], size=fmt_bytes(event['alloc']), root=event['root'])
        width = shutil.get_terminal_size().columns - 1
        sys.stderr.write('\r\x1b[2K' + _fit(text, width))
        sys.stderr.flush()
    return show


def scan(app: App, args: argparse.Namespace, T: Translator) -> int:
    tty = sys.stderr.isatty()
    app.scan(domains=args.domain or None, progress=_scan_progress(T) if tty else None)
    if tty:
        sys.stderr.write('\r\x1b[2K')
        sys.stderr.flush()
    overview = app.overview()
    if args.json:
        dump(overview)
    else:
        print(render_text(overview, app.locale), end='')
    problems = sum(n for code, n in overview['not_included']['errors'].items() if code != PRIVACY_CODE)
    if args.notify:
        try:
            sched.notify(*sched.notification_text(overview, app.locale))
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:   # the scan itself is already stored
            print(T('cli.notify.failed', error=e), file=sys.stderr)
            return EXIT_SCAN_ERRORS
    return EXIT_SCAN_ERRORS if problems else EXIT_OK


def report(app: App, args: argparse.Namespace, T: Translator) -> int:
    overview = app.overview()
    if args.apps and sys.stderr.isatty():
        print(T('cli.apps.measuring'), file=sys.stderr)
    apps = app.app_sizes() if args.apps else None
    if args.json:
        data = {'scan': overview['scan'], 'growth': overview['growth']} if args.since_last else dict(overview)
        if apps is not None:
            data['apps'] = apps
        dump(data)
    else:
        print(render_growth(overview, app.locale) if args.since_last else render_text(overview, app.locale), end='')
        if apps is not None:
            print('\n' + render_apps(apps, app.locale), end='')
    return EXIT_OK


# list ------------------------------------------------------------------------------------------------------

def _qualify_spec(app: App, keys: Sequence[str], days: float) -> None:
    """Point the CLI session at "these groups, filtered by age" so the decisions show what would qualify."""
    ops: List[Dict[str, Any]] = [{'op': 'clear'}, {'op': 'set_filter', 'min_age_days': days}]
    ops += [{'op': 'select_group', 'group': k, 'on': True} for k in keys]
    app.apply_ops(ops, SESSION)


def _decision_text(T: Translator, d: Mapping[str, Any]) -> str:
    text = T(f'decision.{d["state"]}')
    return f'{text} ({T("action." + d["action"])})' if d['state'] == 'selected' else text


def list_command(app: App, args: argparse.Namespace, T: Translator) -> int:
    days = args.older_than if args.older_than is not None else app.settings.default_min_age_days
    if args.group:
        known = {g['key']: g for g in app.groups(session=SESSION)}
        if args.group not in known:
            raise StewardError(ErrorCode.NOT_FOUND, 'error.NOT_FOUND', what='group', id=args.group)
        _qualify_spec(app, [args.group], days)
        page = app.items(group=args.group, limit=50 if args.limit is None else args.limit, session=SESSION)
        if args.json:
            dump({'group': known[args.group]['key'], 'min_age_days': days, **page})
            return EXIT_OK
        g = known[args.group]
        print(f'{g["label"]}  [{g["key"]}]')
        print(g['effect'])
        if g['hint']:
            print(T('cli.list.hint', hint=g['hint']))
        rows = [(T('cli.col.size'), T('cli.col.age'), T('cli.col.decision'), T('cli.col.key'), T('cli.col.path'))]
        for r in page['rows']:
            d = r['decision']
            rows.append((fmt_bytes(r['alloc']), '-' if d['age_days'] is None else f'{d["age_days"]:.1f}',
                         _decision_text(T, d), r['key'], r['path']))
        print()
        print(table(rows, 'rrlll'))
        print(T('cli.list.showing', shown=len(page['rows']), total=page['total'], days=days))
        return EXIT_OK
    listed = app.groups(domain=args.domain, tier=args.tier, session=SESSION)
    _qualify_spec(app, [g['key'] for g in listed if g['tier'] != 'protected' and g['actions']], days)
    listed = app.groups(domain=args.domain, tier=args.tier, session=SESSION)
    if args.limit is not None:
        listed = listed[:args.limit]
    if args.json:
        dump({'min_age_days': days, 'groups': listed})
        return EXIT_OK
    rows = [(T('cli.col.tier'), T('cli.col.size'), T('cli.col.items'), T('cli.col.qualify'),
             T('cli.col.key'), T('cli.col.group'))]
    for g in listed:
        sel = g['selection']
        rows.append((T(f'tier.{g["tier"]}'), fmt_bytes(g['alloc']), str(g['items']),
                     '-' if not g['actions'] or g['tier'] == 'protected' else f'{sel["selected"]} / {sel["total"]}',
                     g['key'], g['label']))
    print(table(rows, 'lrrrll'))
    print(T('cli.list.qualify_note', days=days))
    return EXIT_OK


# plan ------------------------------------------------------------------------------------------------------

def _action_effect(T: Translator, action: str, v: Mapping[str, Any]) -> str:
    if action == 'delete':
        return T('cli.effect.delete', frees=fmt_bytes(v['frees']))
    if action == 'trash':
        return T('cli.effect.trash', moves=fmt_bytes(v['moves']))
    return T('cli.effect.archive', frees=fmt_bytes(v['frees_estimate']))


def _warning_text(T: Translator, w: Mapping[str, Any], labels: Mapping[str, str]) -> str:
    args = dict(w)
    args.pop('key')
    if 'group' in args:
        args['group'] = labels.get(args['group'], args['group'])
    return T(w['key'], **args)


def _summary_lines(T: Translator, summary: Mapping[str, Any], selected_groups: Sequence[str],
                   labels: Mapping[str, str], with_states: bool = True) -> List[str]:
    out = [T('cli.plan.selected', count=summary['count'], total=summary['total'], alloc=fmt_bytes(summary['alloc']),
             reclaim=fmt_bytes(summary['reclaim']))]
    if summary['by_action']:
        rows = [(T('cli.col.action'), T('cli.col.items'), T('cli.col.size'), T('cli.col.effect'))]
        for action, v in summary['by_action'].items():
            rows.append((T(f'action.{action}'), str(v['count']), fmt_bytes(v['alloc']), _action_effect(T, action, v)))
        out += ['', table(rows, 'lrrl')]
    per = summary['groups']
    chosen = sorted(selected_groups, key=lambda k: (-per[k]['alloc'], k))
    if chosen:
        rows = [(T('cli.col.group'), T('cli.col.qualify'), T('cli.col.size'))]
        for key in chosen[:TOP_GROUPS]:
            rows.append((labels.get(key, key), T('cli.plan.qualify', selected=per[key]['selected'],
                                                 total=per[key]['total']), fmt_bytes(per[key]['alloc'])))
        out += ['', T('cli.plan.groups'), table(rows, 'lrr')]
        if len(chosen) > TOP_GROUPS:
            out.append(T('cli.plan.more_groups', count=len(chosen) - TOP_GROUPS))
    if summary['warnings']:
        shown: List[Mapping[str, Any]] = []
        none_qualify = quiet = 0
        for w in summary['warnings']:
            # a broad selection can leave dozens of groups with nothing that qualifies; the group table shows them
            if w['key'] == 'warning.group_selected_none_qualify':
                none_qualify += 1
                if none_qualify > MAX_QUIET_WARNINGS:
                    quiet += 1
                    continue
            shown.append(w)
        out += [''] + [T('cli.plan.warning', text=_warning_text(T, w, labels)) for w in shown]
        if quiet:
            out.append(T('cli.plan.more_warnings', count=quiet))
    states = [(s, v) for s, v in summary['by_state'].items() if s != 'selected']
    if states and with_states:
        rows = [(T('cli.col.decision'), T('cli.col.items'), T('cli.col.size'))]
        rows += [(T(f'decision.{s}'), str(v['count']), fmt_bytes(v['alloc'])) for s, v in states]
        out += ['', T('cli.plan.not_selected'), table(rows, 'lrr')]
    return out


def _matches(glob: str, path: str) -> bool:
    if '/' in glob:
        return match_pattern(glob, path)
    return fnmatch.fnmatchcase(path.rpartition('/')[2], glob)


def _cloud_roots(app: App, groups: Sequence[Mapping[str, Any]], chosen: Sequence[str],
                 include: Sequence[str]) -> Dict[str, str]:
    """The cloud-synced user roots holding an item this plan would take, found from the item paths."""
    synced = cloud_synced(app.bases, app.settings)
    if not synced:
        return {}
    rows: List[Mapping[str, Any]] = []
    for g in groups:
        if g['key'] in chosen and g['domain'] == 'user-files':
            rows += app.items(group=g['key'], limit=None, session=SESSION)['rows']
    if include:
        rows += app.items(keys=include, limit=None, session=SESSION)['rows']
    return cloud_roots_of((r['path'] for r in rows if r['domain'] == 'user-files'
                           and r['decision']['state'] == 'selected'), synced)


def plan(app: App, args: argparse.Namespace, T: Translator) -> int:
    days = args.older_than if args.older_than is not None else app.settings.default_min_age_days
    action = args.action
    ops: List[Dict[str, Any]] = [{'op': 'clear'}, {'op': 'set_filter', 'min_age_days': days}]
    keys = [s for s in args.select if s != 'junk']
    if 'junk' in args.select:
        ops.append({'op': 'select_tier', 'tier': 'junk', 'on': True})
    ops += [{'op': 'select_group', 'group': k, 'on': True, **({'action': action} if action else {})} for k in keys]
    result = app.apply_ops(ops, SESSION)
    chosen = sorted(result['spec']['groups'])
    if action and 'junk' in args.select:
        # select_tier has no action; a group that cannot do it fails here instead of being skipped silently
        junk = [g for g in chosen if g not in keys]
        result = app.apply_ops([{'op': 'set_action', 'group': g, 'action': action} for g in junk], SESSION)
    if args.exclude or args.include:
        edits: List[Dict[str, Any]] = []
        for g in chosen:
            for row in app.items(group=g, limit=None, session=SESSION)['rows']:
                if row['decision']['state'] == 'selected' and any(_matches(x, row['path']) for x in args.exclude):
                    edits.append({'op': 'toggle_item', 'item': row['key'], 'on': False})
        edits += [{'op': 'toggle_item', 'item': k, 'on': True, **({'action': action} if action else {})}
                  for k in args.include]
        if edits:
            result = app.apply_ops(edits, SESSION)
    summary = result['summary']
    groups = app.groups(session=SESSION)
    labels = {g['key']: g['label'] for g in groups}
    if not summary['count']:
        # the groups and warnings explain why; the error handler prints the per-state breakdown
        if not args.json:
            print('\n'.join(_summary_lines(T, summary, chosen, labels, with_states=False)), file=sys.stderr)
        raise StewardError(ErrorCode.EMPTY_PLAN, 'error.EMPTY_PLAN', breakdown=summary['scope_by_state'],
                           total_breakdown=summary['by_state'])
    created: Optional[Dict[str, Any]] = None
    if not args.dry_run:
        created = app.create_plan(SESSION, {'spec_version': result['spec']['version'], 'digest': result['digest'],
                                            'scan_id': result['scan_id']})
    cloud = _cloud_roots(app, groups, chosen, args.include)
    if args.json:
        dump({'dry_run': args.dry_run, 'digest': result['digest'], 'spec': result['spec'], 'summary': summary,
              'plan': created, 'cloud_synced': cloud})
        return EXIT_OK
    print('\n'.join(_summary_lines(T, summary, chosen, labels)))
    for line in cloud_warning_lines(cloud, app.locale):
        print(T('cli.plan.warning', text=line))
    if args.include:
        for row in app.items(keys=args.include, limit=None, session=SESSION)['rows']:
            d = row['decision']
            if d['state'] != 'selected':
                print(T('cli.plan.include_not_selected', path=row['path'],
                        reason=T(d['reason_key'], **d['reason_args'])), file=sys.stderr)
    if created is None:
        print('\n' + T('cli.plan.dry_run'))
    else:
        print('\n' + T('cli.plan.created', id=created['id'], expires=fmt_time(created['expires_at'])))
        print(T('cli.plan.next_show', id=created['id']))
        print(T('cli.plan.next_apply', id=created['id']))
    return EXIT_OK


# show ------------------------------------------------------------------------------------------------------

def show(app: App, args: argparse.Namespace, T: Translator) -> int:
    p = app.plan(args.plan_id)
    with_entries = args.entries or args.state is not None
    page = (app.entries(args.plan_id, state=args.state, limit=50 if args.limit is None else args.limit)
            if with_entries else None)
    if args.json:
        dump({'plan': p, 'entries': page})
        return EXIT_OK
    totals = p['totals']
    print(T('cli.show.header', id=p['id'], state=T(f'plan.{p["state"]}'), scan=p['scan_id'], revision=p['revision']))
    print(T('cli.show.times', created=fmt_time(p['created_at']), expires=fmt_time(p['expires_at'])))
    print(T('cli.show.selected', count=totals['count'], alloc=fmt_bytes(totals['alloc']),
            reclaim=fmt_bytes(totals['reclaim'])))
    if totals['by_action']:
        rows = [(T('cli.col.action'), T('cli.col.items'), T('cli.col.size'), T('cli.col.effect'))]
        for action, v in totals['by_action'].items():
            rows.append((T(f'action.{action}'), str(v['count']), fmt_bytes(v['alloc']), _action_effect(T, action, v)))
        print('\n' + table(rows, 'lrrl'))
    if totals['warnings']:
        labels = {}
        if any('group' in w for w in totals['warnings']):
            labels = {g['key']: g['label'] for g in app.groups(session=SESSION)}   # a plan only stores group keys
        print()
        for w in totals['warnings']:
            print(T('cli.plan.warning', text=_warning_text(T, w, labels)))
    rows = [(T('cli.col.state'), T('cli.col.items'))] + [(T(f'entry.{s}'), str(n))
                                                         for s, n in p['entries']['by_state'].items()]
    print('\n' + table(rows, 'lr'))
    if page is not None:
        rows = [(T('cli.col.seq'), T('cli.col.action'), T('cli.col.state'), T('cli.col.size'), T('cli.col.path'))]
        for e in page['rows']:
            rows.append((str(e['seq']), T(f'action.{e["action"]}'), T(f'entry.{e["state"]}'), fmt_bytes(e['alloc']),
                         e['path']))
        print('\n' + table(rows, 'rllrl'))
        print(T('cli.list.showing_entries', shown=len(page['rows']), total=page['total']))
    return EXIT_OK


# apply -----------------------------------------------------------------------------------------------------

def reason_text(locale: str, reason: str) -> str:
    """Text for an entry reason, which is an i18n key with an optional ':detail' ('skip.blocked:open:Claude(1)')."""
    if not reason:
        return ''
    key, _, detail = reason.partition(':')
    cat = i18n.catalog(locale)
    if key not in cat:
        return reason
    if key == 'skip.blocked':
        detail = blocker_text(locale, detail)
    return i18n.t(key, locale, detail=detail)


def blocker_text(locale: str, code: str) -> str:
    kind, _, rest = code.partition(':')
    cat = i18n.catalog(locale)
    if f'blocker.{kind}' not in cat:
        return code
    app = cat.get(f'app.{rest}', rest) if kind == 'app_running' else ''
    return i18n.t(f'blocker.{kind}', locale, app=app, detail=rest)


def _ask(prompt: str) -> str:
    """One line from stdin, prompt on stderr so JSON on stdout stays clean. EOF raises EOFError."""
    sys.stderr.write(prompt)
    sys.stderr.flush()
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    return line.strip()


class _Progress:
    def __init__(self, T: Translator, enabled: bool):
        self.T = T
        self.enabled = enabled
        self._shown = False

    def event(self, kind: str, data: Mapping[str, Any]) -> None:
        if not self.enabled or kind != 'run.progress' or not data['current']:
            return
        text = self.T('cli.apply.progress', done=data['done'], total=data['total'], size=fmt_bytes(data['bytes']),
                      path=data['current'])
        sys.stderr.write('\r\x1b[2K' + _fit(text, shutil.get_terminal_size().columns - 1))
        sys.stderr.flush()
        self._shown = True

    def clear(self) -> None:
        if self._shown:
            sys.stderr.write('\r\x1b[2K')
            sys.stderr.flush()
            self._shown = False


def _preflight_lines(app: App, T: Translator, pre: Mapping[str, Any]) -> List[str]:
    cat = i18n.catalog(app.locale)
    lines: List[str] = []
    in_use: List[Any] = []
    for reason, v in sorted(pre['blockers'].items(), key=lambda kv: (-kv[1]['alloc'], kv[0])):
        if reason.startswith('app_running:'):
            bundle = reason.partition(':')[2]
            lines.append(T('cli.apply.quit_app', app=v.get('app') or cat.get(f'app.{bundle}', bundle),
                           count=v['count'], size=fmt_bytes(v['alloc'])))
        elif reason == 'rules_changed':
            lines.append(T('cli.apply.changed', count=v['count'], size=fmt_bytes(v['alloc'])))
        else:
            in_use.append((reason, v))
    for reason, v in in_use[:TOP_IN_USE]:
        lines.append(T('cli.apply.in_use', reason=blocker_text(app.locale, reason), count=v['count'],
                       size=fmt_bytes(v['alloc'])))
    if len(in_use) > TOP_IN_USE:
        lines.append(T('cli.plan.more_warnings', count=len(in_use) - TOP_IN_USE))
    for w in pre['warnings']:
        args = {k: v for k, v in w.items() if k != 'key'}
        lines.append(T('cli.apply.warning', text=T(w['key'], **args)))
    return lines


def _refuse(plan: Mapping[str, Any], pre: Mapping[str, Any]) -> None:
    """Raise the error a run would raise, before the user is asked to confirm."""
    if plan['state'] not in ('draft', 'paused'):
        raise StewardError(ErrorCode.INVALID, 'error.plan_state', state=plan['state'])
    if pre['expired']:
        raise StewardError(ErrorCode.PLAN_EXPIRED, 'error.PLAN_EXPIRED', expired_at=plan['expires_at'])
    if not pre['hashes_ok']:
        raise StewardError(ErrorCode.RULES_CHANGED, 'error.RULES_CHANGED',
                           changed=[f'{n}_hash' for n, ok in pre['hashes'].items() if not ok])
    if not pre['trash']['available']:
        raise StewardError(ErrorCode.UNAVAILABLE, 'error.trash_unavailable')
    if not pre['archive']['ok']:
        raise StewardError(ErrorCode.UNAVAILABLE, 'error.no_space', needed=pre['archive']['needed'],
                           free=pre['archive']['free'])


def _exclude_pending(app: App, plan_id: str, glob: str, T: Translator) -> None:
    rows = app.entries(plan_id, state='pending', limit=None)['rows']
    seqs = [r['seq'] for r in rows if _matches(glob, r['path'])]
    if not seqs:
        print(T('cli.apply.exclude_none', glob=glob), file=sys.stderr)
        return
    app.exclude(plan_id, seqs, app.plan(plan_id)['revision'])
    print(T('cli.apply.excluded', count=len(seqs)), file=sys.stderr)


def _handle_interrupt(app: App, plan_id: str, T: Translator, finished: threading.Event, progress: _Progress) -> None:
    """Ctrl-C: pause, then let the user continue, exclude entries or abort."""
    try:
        app.pause()
    except StewardError as e:
        if e.code == ErrorCode.NOT_FOUND:   # the run ended just now
            return
        raise
    while not finished.is_set() and app.job()['status'] != 'paused':
        finished.wait(0.02)
    if finished.is_set():
        return
    progress.clear()
    if not sys.stdin.isatty():
        print(T('cli.apply.paused_noninteractive'), file=sys.stderr)
        app.cancel()
        return
    while True:
        try:
            answer = _ask(T('cli.apply.paused_prompt'))
        except EOFError:
            app.cancel()
            return
        choice, _, glob = answer.partition(' ')
        if choice == 'c':
            app.resume()
            return
        if choice == 'a':
            app.cancel()
            return
        if choice == 'e' and glob.strip():
            _exclude_pending(app, plan_id, glob.strip(), T)


def _run_with_ui(app: App, plan: Mapping[str, Any], T: Translator, show_progress: bool) -> Dict[str, Any]:
    """The run goes in a thread so this one can wait for Ctrl-C."""
    outcome: Dict[str, Any] = {}
    finished = threading.Event()
    interrupted = threading.Event()
    progress = _Progress(T, show_progress)

    def work() -> None:
        try:
            outcome['summary'] = app.run_plan(plan['id'], plan['revision'], plan['entries_digest'],
                                              on_event=progress.event)
        except BaseException as e:   # re-raised below, in the thread that owns the terminal
            outcome['error'] = e
        finally:
            finished.set()

    worker = threading.Thread(target=work, name='disk-steward-apply', daemon=True)
    previous = None
    main_thread = threading.current_thread() is threading.main_thread()
    if main_thread:
        previous = signal.signal(signal.SIGINT, lambda *_: interrupted.set())
    try:
        worker.start()
        while not finished.wait(0.05):
            if interrupted.is_set():
                interrupted.clear()
                _handle_interrupt(app, plan['id'], T, finished, progress)
        worker.join()
    finally:
        if main_thread:
            signal.signal(signal.SIGINT, previous)
        progress.clear()
    if 'error' in outcome:
        raise outcome['error']
    return outcome['summary']


def _print_summary(app: App, T: Translator, summary: Mapping[str, Any]) -> None:
    cancelled = summary['state'] == 'cancelled'
    print(T('cli.apply.cancelled' if cancelled else 'cli.apply.finished', id=summary['plan_id'],
            seconds=summary['seconds']))
    rows = [(T('cli.col.state'), T('cli.col.items'))]
    rows += [(T(f'entry.{s}'), str(n)) for s, n in sorted(summary['counts'].items())]
    print('\n' + T('cli.apply.counts') + '\n' + table(rows, 'lr'))
    if summary['skipped_reasons']:
        rows = [(T('cli.col.reason'), T('cli.col.items'))]
        rows += [(reason_text(app.locale, r), str(n)) for r, n in list(summary['skipped_reasons'].items())[:TOP_SKIPPED]]
        print('\n' + T('cli.apply.skipped') + '\n' + table(rows, 'lr'))
    print('\n' + T('cli.apply.totals', removed=fmt_bytes(summary['removed_alloc']), freed=fmt_bytes(summary['freed']),
                    trash=fmt_bytes(summary['moved_to_trash'])))
    print(T('cli.apply.free_space', estimated=fmt_bytes(summary['estimated_delta'], signed=True),
            measured=fmt_bytes(summary['measured_delta'], signed=True)))
    attention = summary['counts'].get('needs_attention', 0)
    if attention:
        print(T('cli.apply.attention', count=attention, id=summary['plan_id']))
    for w in summary['warnings']:
        args = dict(w)
        key = args.pop('key')
        if 'size' in args:
            args['size'] = fmt_bytes(args['size'])
        print(T('cli.apply.warning', text=T(key, **args)))


def apply(app: App, args: argparse.Namespace, T: Translator) -> int:
    plan = app.plan(args.plan_id)
    pre = app.preflight(args.plan_id)
    if not args.json:
        print(T('cli.apply.header', id=plan['id'], count=pre['pending'], alloc=fmt_bytes(pre['alloc'])))
        if pre['hashes_ok']:
            print(T('cli.apply.hashes_ok'))
        else:
            what = ', '.join(n for n, ok in pre['hashes'].items() if not ok)
            print(T('cli.apply.hashes_changed', what=what))
        if pre['expired']:
            print(T('cli.apply.expired'))
        for line in _preflight_lines(app, T, pre):
            print(line)
    _refuse(plan, pre)
    if not args.yes:
        if not sys.stdin.isatty():
            raise StewardError(ErrorCode.INVALID, 'error.confirm_required')
        try:
            typed = _ask(T('cli.apply.confirm', count=pre['pending']))
        except EOFError:
            typed = ''
        if typed != str(pre['pending']):
            raise StewardError(ErrorCode.INVALID, 'error.confirm_mismatch')
    summary = _run_with_ui(app, plan, T, show_progress=sys.stderr.isatty() and not args.json)
    if args.json:
        dump({'plan': plan, 'preflight': pre, 'summary': summary})
    else:
        _print_summary(app, T, summary)
    return EXIT_NOT_FINISHED if summary['state'] == 'cancelled' else EXIT_OK


# archives --------------------------------------------------------------------------------------------------

def archives(app: App, args: argparse.Namespace, T: Translator) -> int:
    action = args.archives_command
    if action == 'list':
        rows = app.archives()
        if args.json:
            dump({'archives': rows})
            return EXIT_OK
        if not rows:
            print(T('cli.archives.empty'))
            return EXIT_OK
        table_rows = [(T('cli.col.id'), T('cli.col.state'), T('cli.col.size'), T('cli.col.stored'),
                       T('cli.col.created'), T('cli.col.path'))]
        for r in rows:
            table_rows.append((r['id'], T(f'cli.archive_state.{r["state"]}'), fmt_bytes(r['orig_size']),
                               fmt_bytes(r['stored_bytes']), fmt_time(r['manifest']['created_at']), r['path']))
        print(table(table_rows, 'llrrll'))
        return EXIT_OK
    if action == 'restore':
        result = app.restore(args.archive_id)
        print(T('cli.archives.restored', path=result['path'], size=fmt_bytes(result['bytes'])))
        return EXIT_OK
    row = next((r for r in app.archives() if r['id'] == args.archive_id), None)
    if row is None:
        raise StewardError(ErrorCode.NOT_FOUND, 'error.NOT_FOUND', what='archive', id=args.archive_id)
    if not args.yes:
        if not sys.stdin.isatty():
            raise StewardError(ErrorCode.INVALID, 'error.confirm_required')
        try:
            typed = _ask(T('cli.archives.confirm', id=row['id'], path=row['path']))
        except EOFError:
            typed = ''
        if typed.lower() != 'y':
            raise StewardError(ErrorCode.INVALID, 'error.confirm_mismatch')
    result = app.purge_archive(args.archive_id)
    print(T('cli.archives.purged', id=result['archive_id'], size=fmt_bytes(result['bytes'])))
    return EXIT_OK


# diagnose-session ------------------------------------------------------------------------------------------

def diagnose_session(app: App, args: argparse.Namespace, T: Translator) -> int:
    result = app.diagnose_session(args.item_key)
    if args.json:
        dump(result)
        return EXIT_OK
    path = app.store.item(app.latest_scan()['id'], args.item_key).path   # type: ignore[index]
    print(T('cli.diagnose.header', path=path, size=fmt_bytes(result['bytes']), lines=result['lines'],
            invalid=result['invalid_lines']))
    rows = [(T('cli.col.event'), T('cli.col.count'), T('cli.col.size'))]
    rows += [(k, str(v['count']), fmt_bytes(v['bytes'])) for k, v in result['by_type'].items()]
    print('\n' + table(rows, 'lrr'))
    st = result['settings']
    print('\n' + T('cli.diagnose.settings', count=st['count'], size=fmt_bytes(st['bytes']),
                    share=f'{st["fraction"]:.0%}'))
    if result['changed_during_read']:
        print(T('cli.diagnose.changed'))
    return EXIT_OK
