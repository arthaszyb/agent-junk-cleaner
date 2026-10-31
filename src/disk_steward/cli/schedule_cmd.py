"""`schedule install|remove|status`: the weekly scan-only LaunchAgent."""
from __future__ import annotations

import argparse
import os
import shlex
from typing import Optional

from .. import schedule as sched
from ..paths import default_data_dir
from ..service import App
from .commands import EXIT_OK, Translator


def _when(T: Translator, weekday: Optional[int], hour: Optional[int]) -> str:
    if weekday is None or hour is None:   # a plist edited by hand
        return '-'
    return T('cli.schedule.when', day=T(f'cli.schedule.weekday.{weekday % 7}'), hour=hour)


def schedule(app: App, args: argparse.Namespace, T: Translator) -> int:
    action = args.schedule_command
    if action == 'install':
        # DISK_STEWARD_DATA_DIR is not in the job's environment, so a data dir that came from it must be passed on
        explicit = bool(os.environ.get('DISK_STEWARD_DATA_DIR')) or app.data_dir != default_data_dir()
        plist = sched.install(app.data_dir, args.weekday, args.hour, explicit, app.locale)
        print(T('cli.schedule.installed', label=sched.LABEL, path=sched.plist_path()))
        print(T('cli.schedule.runs', when=_when(T, args.weekday, args.hour)))
        print(T('cli.schedule.command', command=shlex.join(plist['ProgramArguments'])))
        for name, value in sorted(plist.get('EnvironmentVariables', {}).items()):
            print(T('cli.schedule.env', name=name, value=value))
        print(T('cli.schedule.log', path=plist['StandardOutPath']))
        print(T('cli.schedule.safe'))
        return EXIT_OK
    if action == 'remove':
        if sched.remove():
            print(T('cli.schedule.removed', path=sched.plist_path()))
        else:
            print(T('cli.schedule.not_installed'))
        return EXIT_OK
    info = sched.status(app.data_dir)
    print(T('cli.schedule.status.plist_yes' if info['installed'] else 'cli.schedule.status.plist_no',
            path=info['plist']))
    print(T('cli.schedule.status.loaded_yes' if info['loaded'] else 'cli.schedule.status.loaded_no'))
    if info['installed']:
        print(T('cli.schedule.runs', when=_when(T, info['weekday'], info['hour'])))
    if info['log_tail']:
        print(T('cli.schedule.status.log_tail', path=info['log']))
        for line in info['log_tail']:
            print(f'  {line}')
    else:
        print(T('cli.schedule.status.log_none', path=info['log']))
    return EXIT_OK
