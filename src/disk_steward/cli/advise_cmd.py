"""`disk-steward advise`: the optional AI advisor. Suggestions only; nothing here selects or changes anything."""
from __future__ import annotations

import argparse
import shlex
import shutil
import sys
import textwrap
from pathlib import Path
from typing import Any, Callable, Mapping

from ..model import ErrorCode, StewardError
from ..report import fmt_time
from ..service import App
from .commands import EXIT_OK, dump

Translator = Callable[..., str]


def _file_error(path: Path, e: Exception) -> StewardError:
    return StewardError(ErrorCode.INVALID, 'error.advisor_file', path=str(path), reason=str(e))


def _share(coverage: Mapping[str, int]) -> str:
    total = coverage['alloc_total']
    return '100%' if not total else f'{100 * coverage["alloc_included"] / total:.0f}%'


def _print_result(result: Mapping[str, Any], T: Translator) -> None:
    width = max(40, shutil.get_terminal_size().columns - 1)
    print(T('advisor.cli.summary'))
    print(textwrap.fill(result['summary'], width=width, initial_indent='  ', subsequent_indent='  ') or '  -')
    print()
    if not result['recommendations']:
        print(T('advisor.cli.none'))
    for rec in result['recommendations']:
        print(f'{T("advisor.verdict." + rec["verdict"])}  {rec["label"]}')
        print(textwrap.fill(rec['reason'], width=width, initial_indent='    ', subsequent_indent='    '))
    if result['stale']:
        print()
        print(T('advisor.cli.stale'))
    for path in result['traces']:
        print()
        print(T('advisor.cli.trace', provider=result['provider'], path=path))
    print()
    print(T('advisor.cli.footer'))


def _dry_run(app: App, args: argparse.Namespace, T: Translator) -> int:
    adv = app.advisor
    preview = adv.preview()
    command: Any = None
    if args.provider != 'manual':
        command = adv.command(args.provider, preview['request_id'])
    if args.json:
        dump(dict(preview, command=command, notice=T(preview['notice_key'])))
        return EXIT_OK
    print(T('advisor.cli.payload', bytes=preview['bytes'], included=preview['coverage']['groups_included'],
            total=preview['coverage']['groups_total'], share=_share(preview['coverage'])))
    dump(preview['payload'])
    print()
    if command is None:
        print(T('advisor.cli.manual_dry_run'))
    else:
        print(T('advisor.cli.command'))
        print('  ' + shlex.join(command['argv']))
        print(T('advisor.cli.stdin', bytes=command['stdin_bytes']))
        print(T(preview['notice_key']))
    print(T('advisor.cli.dry_run_done'))
    return EXIT_OK


def _export(app: App, args: argparse.Namespace, T: Translator) -> int:
    adv = app.advisor
    preview = adv.preview()
    text = adv.export_prompt(preview['request_id'])['text']
    path = args.export
    try:
        path.write_text(text, encoding='utf-8')
    except OSError as e:
        raise _file_error(path, e) from None
    if args.json:
        dump({'path': str(path), 'request_id': preview['request_id'], 'expires_at': preview['expires_at'],
              'bytes': preview['bytes'], 'coverage': preview['coverage']})
    else:
        print(T('advisor.cli.exported', path=path, expires=fmt_time(preview['expires_at'])))
    return EXIT_OK


def _import(app: App, args: argparse.Namespace, T: Translator) -> int:
    path = args.import_file
    try:
        text = path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as e:
        raise _file_error(path, e) from None
    result = app.advisor.import_reply(None, text)
    if args.json:
        dump(result)
    else:
        print(T('advisor.cli.imported'))
        print()
        _print_result(result, T)
    return EXIT_OK


def _run(app: App, args: argparse.Namespace, T: Translator) -> int:
    adv = app.advisor
    preview = adv.preview()
    command = adv.command(args.provider, preview['request_id'])
    if not args.json:
        print(T('advisor.cli.running', provider=command['provider'], version=command['version'],
                groups=preview['coverage']['groups_included']), file=sys.stderr)
    result = adv.run(command['provider'], preview['request_id'])
    if args.json:
        dump(result)
    else:
        _print_result(result, T)
    return EXIT_OK


def advise(app: App, args: argparse.Namespace, T: Translator) -> int:
    toggle = args.enable or args.disable
    if toggle:
        if args.provider:
            raise StewardError(ErrorCode.INVALID, 'error.advisor_toggle_alone')
        status = app.advisor.set_enabled(args.enable)
        if args.json:
            dump(status)
        else:
            print(T('advisor.cli.enabled' if status['enabled'] else 'advisor.cli.disabled'))
        return EXIT_OK
    if not app.settings.advisor_enabled:
        raise StewardError(ErrorCode.UNAVAILABLE, 'error.advisor_disabled')
    if args.dry_run:
        return _dry_run(app, args, T)
    if args.export:
        return _export(app, args, T)
    if args.import_file:
        return _import(app, args, T)
    return _run(app, args, T)


def configure(p: argparse.ArgumentParser) -> None:
    p.add_argument('--provider', choices=('codex', 'claude', 'manual'),
                   help='codex or claude run a local CLI; manual is for a chat of your own (default: the setting)')
    what = p.add_mutually_exclusive_group()
    what.add_argument('--dry-run', action='store_true', help='print the exact payload and command; send nothing')
    what.add_argument('--export', type=Path, metavar='FILE', help='write the manual prompt to FILE')
    what.add_argument('--import', dest='import_file', type=Path, metavar='FILE',
                      help='validate a reply saved in FILE against the latest export')
    what.add_argument('--enable', action='store_true', help='turn the advisor on')
    what.add_argument('--disable', action='store_true', help='turn the advisor off')
    p.add_argument('--json', action='store_true', help='print JSON')
