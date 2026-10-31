"""Command line front end."""
from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import string
import sys
from pathlib import Path
from typing import Callable, List, Optional

from .. import __version__, i18n
from ..macos import RealMacOS
from ..model import Domain, ErrorCode, StewardError, Tier
from ..paths import default_data_dir
from ..report import fmt_bytes, table
from ..service import App
from . import advise_cmd, commands, insights_cmd, portal_cmd, schedule_cmd, tools_cmd

_AGE = re.compile(r'^(\d+(?:\.\d+)?)([dh]?)$')
_SCAN_DOMAINS = tuple(d.value for d in Domain)


def _doctor(data_dir: Optional[Path] = None) -> int:
    report = {
        'version': __version__,
        'python': platform.python_version(),
        'platform': platform.platform(),
        'data_dir': str(data_dir.expanduser().absolute() if data_dir else default_data_dir()),
        'codex': shutil.which('codex'),
        'claude': shutil.which('claude'),
        'trash': RealMacOS().trash_available(),
    }
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write('\n')
    return 0


def parse_age(text: str) -> float:
    """'14d' -> 14, '36h' -> 1.5, '0' -> 0, a bare number counts days."""
    m = _AGE.match(text.strip())
    if not m:
        raise argparse.ArgumentTypeError(f'invalid age {text!r}; examples: 14d, 36h, 0')
    n = float(m.group(1))
    return n / 24 if m.group(2) == 'h' else n


def _parser() -> argparse.ArgumentParser:
    # The shared options default to SUPPRESS so that giving them before or after the command both work
    # and neither position overwrites the other with a default.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--data-dir', type=Path, default=argparse.SUPPRESS,
                        help='data directory (default: the standard one)')
    common.add_argument('--lang', choices=i18n.LOCALES, default=argparse.SUPPRESS, help='output language')
    parser = argparse.ArgumentParser(prog='disk-steward', parents=[common])
    parser.add_argument('--version', action='version', version=f'disk-steward {__version__}')
    sub = parser.add_subparsers(dest='command', required=True)

    def command(name: str, help: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help, parents=[common])

    command('doctor', 'print environment diagnostics as JSON (read-only)')

    p = command('scan', 'scan the disk (read-only) and print the report')
    p.add_argument('--domain', action='append', choices=_SCAN_DOMAINS, help='area to scan; repeat for several')
    p.add_argument('--json', action='store_true', help='print JSON')
    p.add_argument('--notify', action='store_true', help='post a macOS notification with the result')

    p = command('report', 'print the report of the latest scan')
    p.add_argument('--json', action='store_true', help='print JSON')
    p.add_argument('--since-last', action='store_true', help='show only the growth since the previous scan')
    p.add_argument('--apps', action='store_true', help='also list the largest applications (measured with du)')

    p = command('schedule', 'run a weekly scan that only notifies, never deletes')
    schedule_sub = p.add_subparsers(dest='schedule_command', required=True)
    q = schedule_sub.add_parser('install', help='install the weekly scan LaunchAgent', parents=[common])
    q.add_argument('--weekday', type=int, choices=range(8), default=1, metavar='0-7',
                   help='0 or 7 is Sunday (default: 1, Monday)')
    q.add_argument('--hour', type=int, choices=range(24), default=10, metavar='0-23', help='default: 10')
    schedule_sub.add_parser('remove', help='unload and delete the LaunchAgent', parents=[common])
    schedule_sub.add_parser('status', help='show whether it is installed, loaded and what it last logged',
                            parents=[common])

    p = command('list', 'list groups, or the items of one group')
    p.add_argument('--tier', choices=[t.value for t in Tier])
    p.add_argument('--domain', choices=_SCAN_DOMAINS)
    p.add_argument('--group', metavar='KEY', help='list the items of this group')
    p.add_argument('--older-than', type=parse_age, metavar='AGE', help='age filter, e.g. 14d, 36h, 0')
    p.add_argument('--limit', type=int, metavar='N')
    p.add_argument('--json', action='store_true', help='print JSON')

    p = command('plan', 'build a cleanup plan from your selection')
    p.add_argument('--select', action='append', required=True, metavar='junk|GROUP_KEY',
                   help="'junk' for every junk group, or a group key; repeat for several")
    p.add_argument('--older-than', type=parse_age, metavar='AGE', help='age filter, e.g. 14d, 36h, 0')
    p.add_argument('--exclude', action='append', default=[], metavar='GLOB', help='leave out matching paths')
    p.add_argument('--include', action='append', default=[], metavar='ITEM_KEY', help='add one item explicitly')
    p.add_argument('--action', choices=('delete', 'archive', 'trash'), help='action for the selected groups')
    p.add_argument('--dry-run', action='store_true', help='show what would be planned, create nothing')
    p.add_argument('--json', action='store_true', help='print JSON')

    p = command('apply', 'run a plan: delete, archive or trash what it lists')
    p.add_argument('plan_id')
    p.add_argument('--yes', action='store_true', help='do not ask for confirmation')
    p.add_argument('--json', action='store_true', help='print JSON')

    p = command('archives', 'list, restore or purge archives made by earlier runs')
    archives_sub = p.add_subparsers(dest='archives_command', required=True)
    q = archives_sub.add_parser('list', help='list archives', parents=[common])
    q.add_argument('--json', action='store_true', help='print JSON')
    q = archives_sub.add_parser('restore', help='put an archived item back (never overwrites)', parents=[common])
    q.add_argument('archive_id')
    q = archives_sub.add_parser('purge', help='delete an archive for good', parents=[common])
    q.add_argument('archive_id')
    q.add_argument('--yes', action='store_true', help='do not ask for confirmation')

    p = command('diagnose-session', 'show what makes a Codex session file large (counts only)')
    p.add_argument('item_key')
    p.add_argument('--json', action='store_true', help='print JSON')

    p = command('show', 'show a plan')
    p.add_argument('plan_id')
    p.add_argument('--entries', action='store_true', help='list the entries')
    p.add_argument('--state', metavar='STATE', help='only entries in this state (implies --entries)')
    p.add_argument('--limit', type=int, metavar='N')
    p.add_argument('--json', action='store_true', help='print JSON')
    p = command('advise', 'optional AI advice on which groups look safe to clean (suggestions only, off by default)')
    advise_cmd.configure(p)
    p = command('insights', 'environment, history, results and follow-up TODOs of this Mac (read-only)')
    insights_cmd.configure(p)
    p = command('tools', 'list the report-only tools that have a cleanup command, or run one (tools run RULE_ID)')
    tools_cmd.configure(p, common)
    p = command('portal', 'serve the local web portal (a browser UI on 127.0.0.1)')
    portal_cmd.configure(p)
    return parser


_COMMANDS = {'scan': commands.scan, 'report': commands.report, 'schedule': schedule_cmd.schedule,
             'list': commands.list_command,
             'plan': commands.plan, 'show': commands.show, 'apply': commands.apply, 'archives': commands.archives,
             'diagnose-session': commands.diagnose_session, 'advise': advise_cmd.advise,
             'portal': portal_cmd.portal, 'insights': insights_cmd.insights, 'tools': tools_cmd.tools}


def _details(e: StewardError, locale: str) -> dict:
    """The error's details with machine codes turned into text where the message needs it."""
    details = dict(e.details)
    if e.message_key == 'error.not_selectable':
        details['reason'] = i18n.t(f'reason.{details["reason"]}', locale)
    if e.message_key == 'error.native_not_runnable':
        details['reason'] = i18n.t(details['reason_key'], locale, **details['reason_args'])
    return details


def _print_stale_notes(notes: dict, locale: str) -> None:
    T = lambda key, **a: i18n.t(key, locale, **a)   # noqa: E731
    for group in notes['dropped_groups']:
        print(T('cli.stale.dropped_group', group=group), file=sys.stderr)
    for kind in ('include_changed', 'include_missing'):
        found = notes[kind]
        if found['count']:
            print(T(f'cli.stale.{kind}', count=found['count']), file=sys.stderr)
            for path in found['paths']:
                print(f'    {path}', file=sys.stderr)
    if notes['kept_excludes']:
        print(T('cli.stale.kept_excludes', count=notes['kept_excludes']), file=sys.stderr)
    for change in notes['group_changes']:
        print(T('cli.stale.group_change', group=change['group'], old=change['old_selected'], new=change['new_selected'],
                old_size=fmt_bytes(change['old_alloc']), new_size=fmt_bytes(change['new_alloc'])), file=sys.stderr)


def _print_error(e: StewardError, locale: str, as_json: bool) -> None:
    template = i18n.catalog(locale).get(e.message_key, '')
    message = i18n.t(e.message_key, locale, **_details(e, locale))
    if as_json:
        print(json.dumps({'error': {'code': e.code.value, 'message_key': e.message_key, 'message': message,
                                    'details': e.details}}, indent=2, sort_keys=True))
        return
    print(i18n.t('cli.error', locale, code=e.code.value, message=message), file=sys.stderr)
    used = {name for _, name, _, _ in string.Formatter().parse(template) if name}
    for key, value in e.details.items():
        if key in used or key in ('breakdown', 'total_breakdown', 'added', 'removed', 'notes', 'reason_key',
                                  'reason_args'):
            continue
        print(f'  {key}: {value}', file=sys.stderr)
    if e.message_key == 'error.spec_stale' and e.details.get('notes'):
        _print_stale_notes(e.details['notes'], locale)
    if e.code == ErrorCode.EMPTY_PLAN:
        rows = [(i18n.t('cli.col.decision', locale), i18n.t('cli.col.items', locale), i18n.t('cli.col.size', locale))]
        rows += [(i18n.t(f'decision.{s}', locale), str(v['count']), fmt_bytes(v['alloc']))
                 for s, v in e.details['breakdown'].items()]
        print(table(rows, 'lrr'), file=sys.stderr)
    if e.code == ErrorCode.DIGEST_MISMATCH:
        for side in ('added', 'removed'):
            for entry in e.details[side]:
                action = i18n.t(f'action.{entry["action"]}', locale)
                print(i18n.t(f'cli.mismatch.{side}', locale, path=entry['path'], action=action,
                             size=fmt_bytes(entry['alloc'])), file=sys.stderr)


def main(argv: Optional[List[str]] = None, app_factory: Optional[Callable[[], App]] = None) -> int:
    """`app_factory` builds the App (tests inject one on a fake home); by default it comes from --data-dir
    and --lang. Exit codes: 0 ok, 1 scan finished with errors, 2 expected error, 3 another operation is running."""
    args = _parser().parse_args(argv)
    if args.command == 'doctor':
        return _doctor(getattr(args, 'data_dir', None))
    lang = getattr(args, 'lang', None)
    factory = app_factory or (lambda: App(data_dir=getattr(args, 'data_dir', None), locale=lang))
    app: Optional[App] = None
    locale = lang or i18n.detect_locale()
    try:
        app = factory()
        if lang:
            app.locale = lang
        locale = app.locale
        if app.recovery and app.recovery['entries']:
            print(i18n.t('cli.recover.notice', locale, count=len(app.recovery['entries'])), file=sys.stderr)
        return _COMMANDS[args.command](app, args, lambda key, **a: i18n.t(key, locale, **a))
    except StewardError as e:
        _print_error(e, locale, getattr(args, 'json', False))
        return commands.EXIT_BUSY if e.code == ErrorCode.BUSY else commands.EXIT_ERROR
    finally:
        if app is not None and app_factory is None:
            app.close()
