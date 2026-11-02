"""`tools` and `tools run`: the native cleanup commands of report-only tools."""
from __future__ import annotations

import argparse
import signal
import sys
import threading
from typing import Any, Dict, Mapping, Optional

from .. import i18n
from ..model import ErrorCode, StewardError
from ..report import fmt_bytes, table
from ..service import App
from .commands import EXIT_NOT_FINISHED, EXIT_OK, Translator, _ask, dump


def configure(p: argparse.ArgumentParser, common: argparse.ArgumentParser) -> None:
    p.add_argument('--json', action='store_true', help='print JSON')
    sub = p.add_subparsers(dest='tools_command')   # no subcommand lists the tools
    q = sub.add_parser('run', help='run the cleanup command of one tool', parents=[common])
    q.add_argument('rule_id', metavar='RULE_ID', help='the rule listed by `disk-steward tools`, e.g. dev.homebrew')
    q.add_argument('--target', metavar='NAME', help='the model to remove (ollama.models only), as `ollama list` names it')
    q.add_argument('--yes', action='store_true', help='do not ask for confirmation')


def tools(app: App, args: argparse.Namespace, T: Translator) -> int:
    if getattr(args, 'tools_command', None) == 'run':
        return _run(app, args, T)
    rows = app.native_tools(app.locale)
    if args.json:
        dump(rows)
        return EXIT_OK
    print(T('native.cli.title'))
    if not rows:
        print(T('native.cli.none'))
        return EXIT_OK
    head = (T('native.cli.col.rule'), T('native.cli.col.size'), T('native.cli.col.command'),
            T('native.cli.col.status'))
    print(table([head] + [(r['rule_id'], fmt_bytes(r['bytes']), r['command'] or '-',
                           T('native.cli.status.runnable') if r['runnable']
                           else T('native.cli.status.trash') if r['folders'] else r['reason']) for r in rows], 'lrll'))
    for r in rows:
        if r['targets']:
            print('\n' + T('native.cli.targets'))
            print(table([(T('native.cli.col.rule'), T('native.cli.col.size'))] +
                        [(t['name'], t['size']) for t in r['targets']], 'lr'))
    if any(r['runnable'] for r in rows):
        print('\n' + T('native.cli.run_hint'))
    return EXIT_OK


def _row(app: App, rule_id: str) -> Mapping[str, Any]:
    for row in app.native_tools(app.locale):
        if row['rule_id'] == rule_id:
            return row
    raise StewardError(ErrorCode.INVALID, 'error.native_unknown', rule_id=rule_id)


def _command_of(row: Mapping[str, Any], target: Optional[str]) -> str:
    """The text a run has to be given: the row's command, or the one of the chosen model."""
    if row['targets'] is None or not target:
        return row['command'] or ''
    found = [t for t in row['targets'] if t['name'] == target]
    return found[0]['command'] if found else ''


def _run(app: App, args: argparse.Namespace, T: Translator) -> int:
    row = _row(app, args.rule_id)
    command = _command_of(row, args.target)
    app.check_native(args.rule_id, args.target, command)   # refuses before it asks
    if not args.yes:
        if not sys.stdin.isatty():
            raise StewardError(ErrorCode.INVALID, 'error.confirm_required')
        try:
            typed = _ask(T('native.cli.confirm', rule_id=args.rule_id, command=command))
        except EOFError:
            typed = ''
        if typed != args.rule_id:
            raise StewardError(ErrorCode.INVALID, 'error.confirm_mismatch')
    print(T('native.cli.running', command=command), file=sys.stderr, flush=True)

    def show(kind: str, data: Dict[str, Any]) -> None:
        if kind == 'native.output':
            print(data['line'], flush=True)
        elif kind == 'native.done':   # the rescan that follows takes a while: say what happened first
            _numbers(command, data, T)
            if app.latest_scan() is not None:
                print(T('native.cli.rescanning'), file=sys.stderr, flush=True)

    previous = None
    if threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGINT, lambda *_: app.cancel())   # stops the tool, then the run ends normally
    try:
        result = app.run_native(args.rule_id, args.target, command, on_event=show)
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
    return _report(result, T, app.locale)


def _numbers(command: str, data: Mapping[str, Any], T: Translator) -> None:
    print(T('native.cli.finished', command=command, exit_code=data['exit_code']))
    print(T('native.cli.result', before=fmt_bytes(data['before']), after=fmt_bytes(data['after']),
            freed=fmt_bytes(data['freed'])))
    if data['output_truncated']:
        print(T('native.cli.truncated'))


def _report(result: Mapping[str, Any], T: Translator, locale: str) -> int:
    """What `native.done` did not already print: a run that was stopped or never started, and the exit code."""
    if result['error'] is not None:
        if result['before'] is not None and result['after'] is not None:
            print(T('native.cli.result', before=fmt_bytes(result['before']), after=fmt_bytes(result['after']),
                    freed=fmt_bytes(result['freed'])))
        if result['output_truncated']:
            print(T('native.cli.truncated'))
        print(T('cli.error', code=result['error']['code'], message=i18n.t(
            result['error']['message_key'], locale, **result['error']['details'])), file=sys.stderr)
    return EXIT_OK if result['status'] == 'ok' else EXIT_NOT_FINISHED
