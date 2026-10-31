"""Helpers for the advisor tests: an App over the scanned M2 fixture, and fake `codex` / `claude` executables.

No test may reach a real CLI: PATH holds only the fake directory plus /usr/bin:/bin, and HOME is the FakeHome."""
from __future__ import annotations

import json
import os
import re
import stat
import sys
import unittest
from pathlib import Path
from typing import Any, Dict
from unittest import mock

import m2_fixture as fx
from m3_support import make_app
from support import Clock, FakeHome

TOKEN = re.compile(r'"token":"(g[0-9a-f]{8})"')

FEATURES = ''.join(f'{name:<40} stable             true\n' for name in (
    'shell_tool', 'unified_exec', 'apps', 'plugins', 'browser_use', 'computer_use', 'in_app_browser', 'multi_agent',
    'image_generation', 'view_image', 'memories', 'hooks', 'tool_suggest', 'skill_search', 'code_mode_host', 'goals',
    'fast_mode', 'sqlite'))

CLAUDE_HELP = '''Usage: claude [options] [command] [prompt]
Options:
  --json-schema <schema>   JSON Schema for structured output validation.
  --no-session-persistence Disable session persistence
  --output-format <format> Output format
  --permission-prompts <target>  Who answers permission prompts
  -p, --print              Print response and exit
  --safe-mode              Start with all customizations disabled
  --strict-mcp-config      Only use MCP servers from --mcp-config
  --system-prompt <prompt> System prompt to use for the session
  --tools <tools...>       Specify the list of available tools
  --allowedTools <tools...> Comma or space-separated list of tool names to allow
'''

# One script serves both fakes. The plan file next to it says what to do, so a test changes behaviour by
# rewriting JSON and the script never needs regenerating. Everything it saw is written to the record file.
SCRIPT = r'''#!{python}
import json, os, subprocess, sys, time

PLAN = {plan!r}
KIND = {kind!r}
plan = json.load(open(PLAN))
argv = sys.argv[1:]
started = time.time()
if plan.get('calls'):
    with open(plan['calls'], 'a') as f:
        f.write(json.dumps({{'argv': argv[:2], 'start': started}}) + '\n')
if plan.get('probe_sleep') and (argv == ['--version'] or argv[:2] == ['features', 'list'] or argv == ['--help']):
    time.sleep(plan['probe_sleep'])
    with open(plan['calls'], 'a') as f:
        f.write(json.dumps({{'argv': argv[:2], 'end': time.time()}}) + '\n')

if argv == ['--version']:
    if plan.get('version_exit'):
        sys.stderr.write('boom\n')
        sys.exit(plan['version_exit'])
    print(plan.get('version', 'fake-9.9.9'))
    sys.exit(0)
if KIND == 'codex' and argv[:2] == ['features', 'list']:
    sys.stdout.write(plan.get('features', ''))
    sys.exit(0)
if KIND == 'claude' and argv == ['--help']:
    sys.stdout.write(plan.get('help', ''))
    sys.exit(0)

stdin = sys.stdin.read()
record = {{'argv': argv, 'stdin': stdin, 'cwd': os.getcwd(), 'listing': sorted(os.listdir('.')),
          'pid': os.getpid(), 'pgid': os.getpgid(0), 'home': os.environ.get('HOME'),
          'env_keys': sorted(os.environ)}}
for flag in ('--output-schema',):
    if flag in argv:
        record['schema_text'] = open(argv[argv.index(flag) + 1]).read()
json.dump(record, open(plan['record'], 'w'))

def payload():
    return json.loads(stdin.split('Disk usage groups (JSON):\n', 1)[1])

def echo_reply():
    recs = []
    for g in payload()['groups']:
        verdict = 'keep' if g['protected'] else plan.get('verdict', 'review')
        recs.append({{'group': g['token'], 'verdict': verdict, 'reason': 'canned reason ' + g['label_key']}})
    return {{'summary': plan.get('summary', 'canned summary'), 'recommendations': recs[:plan.get('limit', 60)]}}

def codex_home():
    return os.environ.get('CODEX_HOME') or os.path.join(os.environ['HOME'], '.codex')

def first_line_session(directory, cwd):
    d = os.path.join(codex_home(), directory, '2026', '10', '28')
    os.makedirs(d, exist_ok=True)
    meta = {{'timestamp': 'x', 'type': 'session_meta', 'payload': {{'id': 'abc', 'cwd': cwd}}}}
    with open(os.path.join(d, 'rollout-fake.jsonl'), 'w') as f:
        f.write(json.dumps(meta) + '\n' + '{{"type":"response_item"}}\n')

if plan.get('child_pid_file'):
    child = subprocess.Popen(['sleep', '60'])
    open(plan['child_pid_file'], 'w').write(str(child.pid))
if plan.get('flood'):
    sys.stdout.write('x' * plan['flood'])
    sys.stdout.flush()
if plan.get('sleep'):
    time.sleep(plan['sleep'])
if plan.get('stderr'):
    sys.stderr.write(plan['stderr'])
if plan.get('session') == 'ours':
    first_line_session(plan.get('session_dir', 'sessions'), os.getcwd())
elif plan.get('session') == 'other':
    first_line_session(plan.get('session_dir', 'sessions'), '/somewhere/else')
elif plan.get('session') == 'garbled':
    d = os.path.join(codex_home(), 'sessions', '2026')
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, 'rollout-garbled.jsonl'), 'w').write('{{not json ' + os.getcwd() + '\n')
if plan.get('claude_project') == 'ours':
    name = os.getcwd().replace('/', '-').replace('.', '-')
    d = os.path.join(os.environ['HOME'], '.claude', 'projects', name)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, 'session.jsonl'), 'a').write('x\n')
elif plan.get('claude_project') == 'other':
    os.makedirs(os.path.join(os.environ['HOME'], '.claude', 'projects', '-some-other-dir'), exist_ok=True)

if plan.get('write_in_cwd'):
    open(plan['write_in_cwd'], 'w').write('junk')
    os.makedirs('subdir/deeper', exist_ok=True)
    open('subdir/deeper/x', 'w').write('junk')
if plan.get('log_file'):
    line = os.getcwd() if plan.get('log_line') == 'ours' else 'something else'
    with open(os.path.join(codex_home(), plan['log_file']), 'a') as f:
        f.write(json.dumps({{'text': line}}) + '\n')
if plan.get('claude_json'):
    key = os.getcwd() if plan['claude_json'] == 'ours' else '/some/other/dir'
    json.dump({{'projects': {{key: {{}}}}}}, open(os.path.join(os.environ['HOME'], '.claude.json'), 'w'))

reply = plan.get('reply')
if reply == 'echo':
    reply = json.dumps(echo_reply())
if KIND == 'codex':
    if reply is not None:
        open(argv[argv.index('-o') + 1], 'w').write(reply)
else:
    if plan.get('envelope') is not None:
        sys.stdout.write(plan['envelope'])
    elif reply is not None:
        sys.stdout.write(json.dumps({{'type': 'result', 'subtype': 'success', 'is_error': False,
                                     'result': reply, 'structured_output': json.loads(reply)}}))
sys.exit(plan.get('exit', 0))
'''


class Fakes:
    """Fake executables in a directory that is first on PATH."""

    def __init__(self, root: Path):
        self.bin = root / 'bin'
        self.plans = root / 'plans'
        self.bin.mkdir()
        self.plans.mkdir()

    def install(self, kind: str, **plan: Any) -> Path:
        record = self.plans / f'{kind}.record.json'
        record.unlink(missing_ok=True)
        plan.setdefault('record', str(record))
        plan.setdefault('calls', str(self.plans / f'{kind}.calls'))
        Path(plan['calls']).unlink(missing_ok=True)
        if kind == 'codex':
            plan.setdefault('features', FEATURES)
            plan.setdefault('version', 'codex-cli 9.9.9')
        else:
            plan.setdefault('help', CLAUDE_HELP)
            plan.setdefault('version', '9.9.9 (Claude Code)')
        plan_path = self.plans / f'{kind}.plan.json'
        plan_path.write_text(json.dumps(plan), encoding='utf-8')
        exe = self.bin / kind
        exe.write_text(SCRIPT.format(python=sys.executable, plan=str(plan_path), kind=kind), encoding='utf-8')
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        return exe

    def update(self, kind: str, **changes: Any) -> None:
        path = self.plans / f'{kind}.plan.json'
        plan = json.loads(path.read_text(encoding='utf-8'))
        plan.update(changes)
        path.write_text(json.dumps(plan), encoding='utf-8')

    def record(self, kind: str) -> Dict[str, Any]:
        return json.loads((self.plans / f'{kind}.record.json').read_text(encoding='utf-8'))

    def called(self, kind: str) -> bool:
        return (self.plans / f'{kind}.record.json').exists()

    def calls(self, kind: str) -> list:
        """Every invocation of the fake, probes included: [{'argv', 'start'}, {'argv', 'end'}...]."""
        path = self.plans / f'{kind}.calls'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []

    def probe_runs(self, kind: str) -> int:
        return sum(1 for c in self.calls(kind) if 'start' in c and c['argv'] in (['--version'], ['features', 'list'],
                                                                                 ['--help']))


class AdvisorCase(unittest.TestCase):
    """A FakeHome with the fixture, a scanned App, HOME and PATH pointed at the fakes."""

    scan = True

    def setUp(self) -> None:
        self.fh = FakeHome(Clock()).__enter__()
        self.addCleanup(self.fh.cleanup)
        fx.build(self.fh)
        self.fakes = Fakes(self.fh.root)
        patch = mock.patch.dict(os.environ, {'HOME': str(self.fh.home), 'PATH': f'{self.fakes.bin}:/usr/bin:/bin'})
        patch.start()
        self.addCleanup(patch.stop)
        for name in ('CODEX_HOME', 'CLAUDE_CONFIG_DIR'):
            os.environ.pop(name, None)
        self.app = make_app(self.fh)
        self.addCleanup(self.app.close)
        if self.scan:
            self.app.scan()

    @property
    def adv(self) -> Any:
        return self.app.advisor

    def enable(self) -> Any:
        return self.adv.set_enabled(True)

    def expect(self, code: Any, key: str, fn: Any, *args: Any, **kw: Any) -> Any:
        from disk_steward.model import StewardError
        from support import render_error
        with self.assertRaises(StewardError) as cm:
            fn(*args, **kw)
        self.assertEqual((cm.exception.code, cm.exception.message_key), (code, key))
        render_error(cm.exception)   # the key exists in every locale and its details fill the placeholders
        return cm.exception


def reply_for(payload: Dict[str, Any], verdict: str = 'review', summary: str = 'ok') -> Dict[str, Any]:
    """A valid reply for a payload, protected groups kept."""
    return {'summary': summary,
            'recommendations': [{'group': g['token'], 'verdict': 'keep' if g['protected'] else verdict,
                                 'reason': 'because'} for g in payload['groups']]}


def other_app(fh: FakeHome, **kw: Any) -> Any:
    """A second App on the same data dir, standing in for a later CLI process."""
    from disk_steward.settings import Settings
    return make_app(fh, settings=Settings(domains=('agent',), advisor_enabled=True), **kw)
