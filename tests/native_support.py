"""Helpers for the native-command tests: fake tools on a temp PATH (Python scripts, so a test can make them print a lot,
sleep, start children or ignore SIGTERM), and a small tree of tool folders in a FakeHome for them to clean."""
from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

TEMPLATE = '''#!{python}
import json, os, shutil, signal, sys, time
NAME, LOG, VLOG = {name!r}, {log!r}, {vlog!r}
args = sys.argv[1:]


def record(**extra):
    entry = dict(tool=NAME, argv=args, cwd=os.getcwd(), cwd_entries=sorted(os.listdir('.')),
                 mark=os.environ.get('DS_TEST_MARK'), stdin_closed=not os.isatty(0), **extra)
    with open(LOG, 'a') as f:
        f.write(json.dumps(entry) + chr(10))


def wipe(*paths):
    for path in paths:
        shutil.rmtree(path, ignore_errors=True)


def wait_for(path):
    while not os.path.exists(path):
        time.sleep(0.02)


if args in (['--version'], ['version']):
    with open(VLOG, 'a') as f:
        f.write(NAME + chr(10))
    {version_line}
{body}
'''


class FakeBin:
    """A folder of fake executables. `calls()` is what they recorded, `versions()` how often they were asked for their
    version (the answer to --version, or `version` for go)."""

    def __init__(self, root: Path):
        self.dir = root / 'fakebin'
        self.dir.mkdir()
        self.log = root / 'native-calls.jsonl'
        self.vlog = root / 'native-versions.log'

    def tool(self, name: str, body: str = 'record()', version: Optional[str] = '1.2.3') -> Path:
        """`body` is Python: record(), wipe(*paths), wait_for(path) and `args` are there. Without a version the tool
        fails when asked for it."""
        path = self.dir / name
        line = f'print({version!r}); sys.exit(0)' if version is not None else 'sys.exit(1)'
        path.write_text(TEMPLATE.format(python=sys.executable, name=name, log=str(self.log), vlog=str(self.vlog),
                                        version_line=line, body=body))
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return path

    def calls(self) -> List[Dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def versions(self) -> List[str]:
        return self.vlog.read_text().splitlines() if self.vlog.exists() else []


def wait_until(condition, timeout: float = 30.0, step: float = 0.02):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError('timed out')
        time.sleep(step)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# Folders of the report-only tools, each big enough to show in a scan: logical path -> (files, size of each).
TREES: Dict[str, Any] = {
    'dev.homebrew': ('~/Library/Caches/Homebrew/downloads', 5, 100_000),
    'dev.uv': ('~/.cache/uv/archive-v0', 2, 60_000),
    'ollama.models': ('~/.ollama/models/blobs', 1, 300_000),
    'dev.playwright': ('~/Library/Caches/ms-playwright/chromium-1', 1, 50_000),
    'dev.npx': ('~/.npm/_npx/abc', 1, 40_000),
    'dev.gradle': ('~/.gradle/caches/modules-2', 1, 70_000),
    'dev.cargo': ('~/.cargo/registry/cache', 1, 30_000),
    'dev.coresimulator': ('~/Library/Developer/CoreSimulator/Devices/D1', 1, 80_000),
}


def build_trees(fh: Any, which: Optional[List[str]] = None) -> None:
    for rule_id, (folder, count, size) in TREES.items():
        if which is None or rule_id in which:
            for i in range(count):
                fh.file(f'{folder}/f{i}', size=size, age_days=30)
