"""Dev only: serve the portal on a fake home with a fake macOS, for the browser E2E in tests/e2e.

Builds a FakeHome from the M2 fixture plus a few app-library and user-files files, scans nothing unless --history
is on (the default: seed_history gives it a past of scans and cleanups), prints one JSON line {url, home, data_dir, pid} and serves until SIGTERM or SIGINT. The real home is never
read or written, and no real codex or claude CLI runs: the advisor is a fake."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(REPO / 'tests'))

import m2_fixture  # noqa: E402
from m3_support import make_mac  # noqa: E402
from m4_support import make_plan  # noqa: E402
from portal_support import make_portal_app  # noqa: E402
from support import Clock, FakeHome  # noqa: E402

from disk_steward import insights, schedule, service  # noqa: E402
from disk_steward.portal.server import PortalServer  # noqa: E402
from disk_steward.settings import Settings  # noqa: E402

CLAUDE_DESKTOP = 'com.anthropic.claudefordesktop'
LOGS = '~/Library/Logs/Acme'
CLAUDE_CACHE = '~/Library/Application Support/Claude/Cache'


def build_files(fh: FakeHome) -> None:
    m2_fixture.build(fh)
    f = fh.file
    # one group with a file past the 14-day filter and one inside it, for the "too recent" rows
    for i in range(4):
        f(f'{LOGS}/old{i}.log', size=100 + i, age_days=16)
    f(f'{LOGS}/recent.log', size=100, age_days=9)
    f('~/Library/Caches/com.acme.Tool/blob1', size=3000, age_days=30)
    f('~/Library/Caches/com.acme.Tool/blob2', size=3000, age_days=30)
    # old files an app that may be running would block
    for i in range(3):
        f(f'{CLAUDE_CACHE}/old{i}', size=2000, age_days=30)
    f('~/Downloads/Tool-1.0.dmg', size=5000, age_days=60)
    f('~/Documents/Taxes/2025.pdf', size=100, age_days=400)


DAY = 86400.0
GIB = 1024 ** 3
SESSIONS = '~/.codex/sessions/2026/09'
SESSIONS_GROUP = f'codex.sessions:{SESSIONS}'
SEED_CACHE = '~/Library/Caches/com.seed.Cache'
SEED_SHIPIT = '~/Library/Caches/com.seed.Tool.ShipIt'


def seed_history(app, fh) -> None:
    """Give the fixture a past for the Insights page: scans spread over 40 days while a Codex session folder grows
    and update leftovers and caches appear, plus two completed cleanups (the first archives the sessions, so there
    are archives older than 30 days). Everything runs on the fixture clock, which ends where it started, and prints
    nothing. The growth thresholds of Insights are lowered here because the fixture is measured in kilobytes."""
    clock, mac = fh.clock, app.mac
    now = clock()
    # dev only: the rules ask for hundreds of MiB, a fixture has hundreds of KiB
    insights.REVIEW_MIN, insights.REPORT_ONLY_MIN = 256 * 1024, 64 * 1024
    insights.GROWTH_TOTAL, insights.GROWTH_PER_DAY = 512 * 1024, 64 * 1024

    def at(days_ago: float, free: int) -> None:
        clock.set(now - days_ago * DAY)
        mac.free = free

    def files(folder: str, n: int, size: int, age_days: float, name: str = 'blob', start: int = 0,
              ext: str = '') -> None:
        for i in range(start, start + n):
            fh.file(f'{folder}/{name}{i}{ext}', size=size, age_days=age_days)

    def clean(ratio: float, archive_sessions: bool) -> None:
        """Junk, and with `archive_sessions` the Codex sessions too, archived: `ratio` of what the run removes shows
        as free space afterwards (a snapshot may hold the rest back, which is what the run's measured figure says)."""
        ops = [{'op': 'select_tier', 'tier': 'junk', 'on': True}, {'op': 'set_filter', 'min_age_days': 14}]
        if archive_sessions:
            ops.append({'op': 'select_group', 'group': SESSIONS_GROUP, 'on': True, 'action': 'archive'})
        plan = make_plan(app, ops)
        hooks, app._hooks = app._hooks, {'after_claim': lambda e: setattr(mac, 'free', mac.free + int(e.reclaim * ratio))}
        try:   # a fixture started with --slow must not slow the cleanups seeded here
            app.run_plan(plan['id'], plan['revision'], plan['entries_digest'])
        finally:
            app._hooks = hooks

    try:
        at(40, 300_000_000_000)
        files(SEED_CACHE, 6, 150_000, 30)
        fh.file('~/.ollama/models/blobs/sha256-seed', size=400_000, age_days=30)
        app.scan()
        at(37, 297_600_000_000)
        files(SESSIONS, 4, 200_000, 20, 'rollout', 0, '.jsonl')
        app.scan()
        at(35, 296_100_000_000)
        files(f'{SEED_SHIPIT}/update', 3, 100_000, 30)
        app.scan()
        at(34, 296_100_000_000)
        clean(1.0, True)                  # rescans by itself
        at(24, 291_000_000_000)
        files(SEED_CACHE, 6, 150_000, 30, start=10)
        files(SESSIONS, 5, 200_000, 20, 'rollout', 10, '.jsonl')
        app.scan()
        at(12, 284_500_000_000)
        files(f'{SEED_SHIPIT}/update', 4, 100_000, 30, start=10)
        fh.file('~/Library/Caches/Homebrew/downloads/seed.tar.gz', size=300_000, age_days=30)
        app.scan()
        at(11, 284_500_000_000)
        clean(0.3, False)                 # a snapshot holds most of it back
        at(6, 281_900_000_000)
        files(SESSIONS, 4, 200_000, 20, 'rollout', 20, '.jsonl')
        app.scan()
        at(1, 279_300_000_000)
        files(SEED_CACHE, 5, 150_000, 30, start=20)
        files(f'{SEED_SHIPIT}/update', 2, 100_000, 30, start=20)
        app.scan()
    finally:
        clock.set(now)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--running', action='append', default=[], metavar='BUNDLE_ID',
                        help='pretend this app is running (repeatable)')
    parser.add_argument('--slow', type=float, default=0.0, metavar='SECONDS',
                        help='wait this long before each plan entry, so a run can be paused midway')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--history', action=argparse.BooleanOptionalAction, default=True,
                        help='start with 40 days of scans and two cleanups behind it, for the Insights page '
                             '(--no-history starts empty, the Scan button makes the first one)')
    parser.add_argument('--real-tools', action='store_true',
                        help="let the tool table find this Mac's real brew, ollama, pip3 ... (a Run click would then "
                             'clean real caches); by default only fake tools under the temp folder are found')
    args = parser.parse_args()

    clock = Clock()
    fh = FakeHome(clock).__enter__()
    if not args.real_tools:
        # "Run" in the report-only table must never reach a real tool from a dev fixture: keep only PATH entries
        # under the temp folder (the E2E puts its fake brew/ollama there) plus the base system folders.
        temp = os.path.realpath(tempfile.gettempdir())
        kept = [d for d in os.environ.get('PATH', '').split(os.pathsep)
                if d and os.path.realpath(d).startswith(temp + os.sep)]
        # Fake tools are `#!/usr/bin/env python3` scripts; give them this interpreter alone, not the folder it sits
        # in (that folder also holds the real brew, pip3 and ollama).
        own = str(fh.root / 'python-only')   # inside the fake home, so it goes away with it
        os.mkdir(own)
        os.symlink(sys.executable, os.path.join(own, 'python3'))
        os.environ['PATH'] = os.pathsep.join(kept + [own, '/bin', '/usr/sbin', '/sbin'])
    build_files(fh)
    # "Largest applications" lists a folder of apps: a fake one, so the real /Applications is never read
    apps = fh.root / 'Applications'
    for name in ('Big.app', 'Small.app'):
        (apps / name).mkdir(parents=True)
    service.SYSTEM_APPLICATIONS = apps
    schedule.LAUNCH_AGENTS_DIR = fh.root / 'LaunchAgents'   # Insights asks whether the weekly scan is installed
    mac = make_mac(running=args.running, snapshot_names=['com.apple.TimeMachine.2026-10-28-100000.local'])
    mac.app_size_map = {str(apps / 'Big.app'): {'bytes': 3 * 1024 ** 3}, str(apps / 'Small.app'): {'bytes': 5 * 1024 ** 2}}
    hooks = {'after_claim': lambda entry: time.sleep(args.slow)} if args.slow else None
    app = make_portal_app(fh, mac=mac, settings=Settings(domains=('agent', 'app-library', 'user-files')), hooks=hooks)
    if args.history:
        seed_history(app, fh)
    server = PortalServer(app, args.port)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        server.start()
        print(json.dumps({'url': server.new_launch_url(), 'home': str(fh.home), 'data_dir': str(fh.data_dir),
                          'pid': os.getpid()}), flush=True)
        while not stop.wait(0.5):
            pass
    finally:
        server.stop()
        app.close()
        fh.cleanup()
    return 0


if __name__ == '__main__':
    sys.exit(main())
