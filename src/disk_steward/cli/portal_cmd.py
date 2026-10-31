"""`disk-steward portal`: the local web portal, or (--url) a fresh one-time link to the one already running."""
from __future__ import annotations

import argparse

from ..portal import launch
from ..service import App
from .commands import EXIT_OK, Translator

MAX_PORT = 65535


def configure(p: argparse.ArgumentParser) -> None:
    p.add_argument('--open', action='store_true', help='open the portal in the default browser')
    p.add_argument('--port', type=int, default=0, choices=range(MAX_PORT + 1), metavar='N',
                   help='port to listen on (default: a random free one)')
    p.add_argument('--url', action='store_true', help='print a new one-time link to the running portal and exit')


def portal(app: App, args: argparse.Namespace, T: Translator) -> int:
    if args.url:
        print(launch.request_launch_url(app.data_dir))
        return EXIT_OK
    from ..portal.server import serve   # imported here: the portal pulls in the CLI's own text helpers

    def announce(url: str) -> None:
        print(T('portal.cli.running'), flush=True)
        print(url, flush=True)
        print(T('portal.cli.hint'), flush=True)

    return serve(app, port=args.port, open_browser=args.open, announce=announce,
                 default_locale=getattr(args, 'lang', None))
