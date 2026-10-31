"""Local CLI providers: Codex and Claude run as child processes in a throwaway directory with their tools off.

Neither needs an API key; both use the login the user already has. The prompt goes in on stdin and nothing in the
argv carries user data. After each call the agent's own session store is checked, because a leftover session file
would add exactly the kind of bloat this tool exists to clean up."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from ..model import ErrorCode, StewardError
from . import prompts, validate

TIMEOUT_SECONDS = 180
OUTPUT_CAP = 1024 * 1024
PROBE_TIMEOUT = 20
POLL_SECONDS = 0.1
FIRST_LINE_CAP = 8 * 1024 * 1024    # a Codex session's first line carries the whole instructions text
CLAUDE_JSON_CAP = 64 * 1024 * 1024

# Codex features that give the model hands (shell, browser, plugins, memory...). The model here only reads text.
CODEX_DISABLE = ('shell_tool', 'unified_exec', 'apps', 'plugins', 'browser_use', 'computer_use', 'in_app_browser',
                 'multi_agent', 'image_generation', 'view_image', 'memories', 'hooks', 'tool_suggest',
                 'skill_search', 'code_mode_host', 'goals')
CLAUDE_FLAGS = ('--print', '--tools', '--safe-mode', '--strict-mcp-config', '--no-session-persistence',
                '--permission-prompts', '--output-format', '--json-schema', '--system-prompt')

# Shown in a dry run, where the per-call temp files do not exist yet.
PLACEHOLDERS = {'schema': '<schema file>', 'out': '<output file>'}
CWD_NAME = 'advisor-cwd'


@dataclass(frozen=True)
class Probe:
    available: bool
    version: Optional[str]
    reason: str
    exe: Optional[str] = None
    disable: Sequence[str] = ()    # Codex only: the CODEX_DISABLE entries this build lists

    def info(self) -> Dict[str, Any]:
        return {'available': self.available, 'version': self.version, 'reason': self.reason}


@dataclass
class RunPaths:
    cwd: Path
    schema: Path
    out: Path
    stdin: Path
    stdout: Path
    stderr: Path


@dataclass
class Output:
    stdout: str
    stderr: str
    reply: str       # Codex: the -o file; Claude: unused
    traces: List[str] = field(default_factory=list)   # files where the CLI recorded our working directory


def _tail(text: str, limit: int = 300) -> str:
    return ' '.join(text.split())[-limit:]


def _probe_run(argv: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), capture_output=True, text=True, encoding='utf-8', errors='replace',
                          timeout=PROBE_TIMEOUT, stdin=subprocess.DEVNULL, start_new_session=True)


def _probe_command(argv: Sequence[str]) -> str:
    """stdout of a probe command; raises RuntimeError with a short reason."""
    try:
        r = _probe_run(argv)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'{shlex.join(argv[1:])} timed out') from None
    except OSError as e:
        raise RuntimeError(f'{shlex.join(argv[1:])} could not start: {e.strerror or e}') from None
    if r.returncode != 0:
        raise RuntimeError(f'{shlex.join(argv[1:])} exited with {r.returncode}: {_tail(r.stderr or r.stdout, 120)}')
    return r.stdout


def _kill_group(proc: 'subprocess.Popen[bytes]') -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)   # start_new_session made the child its own group leader
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS answers EPERM for a group whose leader already exited and waits to be reaped: nothing is left to kill
        if proc.poll() is None:
            raise
    proc.wait()


def _read_capped(path: Path, what: str) -> str:
    with open(path, 'rb') as f:
        data = f.read(OUTPUT_CAP + 1)
    if len(data) > OUTPUT_CAP:
        raise StewardError(ErrorCode.INVALID, 'error.advisor_output_too_large', what=what)
    return data.decode('utf-8', errors='replace')


def run_child(argv: Sequence[str], paths: RunPaths, timeout: float) -> int:
    """Run to completion in its own process group. The environment is the user's own (the login needs HOME and
    friends) and is never logged. Output goes to files so a runaway child is noticed by size, not by memory."""
    with open(paths.stdin, 'rb') as fin, open(paths.stdout, 'wb') as fout, open(paths.stderr, 'wb') as ferr:
        try:
            proc = subprocess.Popen(list(argv), stdin=fin, stdout=fout, stderr=ferr, cwd=str(paths.cwd),
                                    start_new_session=True, env=dict(os.environ))
        except OSError as e:
            raise StewardError(ErrorCode.UNAVAILABLE, 'error.advisor_cli_failed', provider=Path(argv[0]).name,
                               reason=f'could not start: {e.strerror or e}') from None
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    proc.wait(timeout=max(0.0, min(POLL_SECONDS, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    pass
                for path in (paths.stdout, paths.stderr):
                    if path.stat().st_size > OUTPUT_CAP:
                        _kill_group(proc)
                        raise StewardError(ErrorCode.INVALID, 'error.advisor_output_too_large', what=path.name)
                if time.monotonic() >= deadline:
                    _kill_group(proc)
                    raise StewardError(ErrorCode.UNAVAILABLE, 'error.advisor_timeout', seconds=int(timeout))
        except BaseException:
            if proc.poll() is None:
                _kill_group(proc)
            raise
        _kill_group(proc)   # whatever the CLI left running in its group must not outlive the call
    return int(proc.returncode)


class Provider:
    name = ''
    exe_name = ''

    def __init__(self, home: Path, cwd: Path, timeout: Optional[float] = None):
        """`cwd` is the one fixed, empty working directory of every call, so the agent's per-directory records
        (Claude's ~/.claude.json entry, Codex's cwd fields) cannot grow with the number of calls."""
        self.home = Path(home)
        self.cwd = Path(cwd)
        self.timeout = timeout

    # to be provided by subclasses -------------------------------------------------------------------------

    def probe(self) -> Probe:
        raise NotImplementedError

    def argv(self, probe: Probe, paths: Mapping[str, str]) -> List[str]:
        raise NotImplementedError

    def stdin_text(self, payload_json: str) -> str:
        raise NotImplementedError

    def snapshot(self, cwd: Path) -> Any:
        raise NotImplementedError

    def check_snapshot(self, before: Any, cwd: Path) -> List[str]:
        """Raise if the call left a session behind; otherwise return the files that merely mention `cwd`."""
        raise NotImplementedError

    def reply_object(self, out: Output) -> Any:
        raise NotImplementedError

    # shared -----------------------------------------------------------------------------------------------

    def command_line(self, probe: Probe) -> List[str]:
        """The argv of a call, with placeholders where a call would use its temp files."""
        return self.argv(probe, dict(PLACEHOLDERS, cwd=os.path.realpath(self.cwd)))

    def _which(self) -> Optional[str]:
        return shutil.which(self.exe_name)

    def _prepare_cwd(self) -> Path:
        """The fixed working directory, private and verified empty."""
        self.cwd.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.cwd, 0o700)
        cwd = Path(os.path.realpath(self.cwd))
        if os.listdir(cwd):
            raise StewardError(ErrorCode.INVALID, 'error.advisor_cwd_not_empty', path=str(cwd))
        return cwd

    @staticmethod
    def _empty_cwd(cwd: Path) -> None:
        for entry in os.scandir(cwd):   # only what is inside the advisor's own directory
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path, ignore_errors=True)
            else:
                os.unlink(entry.path)

    def invoke(self, probe: Probe, payload_json: str) -> Output:
        """One call. Raises StewardError for a timeout, a failed CLI, oversized output or a leftover session."""
        if not probe.available:
            raise StewardError(ErrorCode.UNAVAILABLE, 'error.advisor_provider_unavailable', provider=self.name,
                               reason=probe.reason)
        cwd = self._prepare_cwd()
        root = Path(os.path.realpath(tempfile.mkdtemp(prefix='disk-steward-advisor-')))
        try:
            paths = RunPaths(cwd=cwd, schema=root / 'schema.json', out=root / 'reply.json',
                             stdin=root / 'stdin.txt', stdout=root / 'stdout.txt', stderr=root / 'stderr.txt')
            paths.schema.write_text(prompts.schema_json(), encoding='utf-8')
            paths.stdin.write_text(self.stdin_text(payload_json), encoding='utf-8')
            for p in (paths.stdout, paths.stderr):
                p.touch()
            argv = self.argv(probe, {'cwd': str(cwd), 'schema': str(paths.schema), 'out': str(paths.out)})
            before = self.snapshot(cwd)
            try:
                code = run_child(argv, paths, self.timeout if self.timeout is not None else TIMEOUT_SECONDS)
                out = Output(stdout=_read_capped(paths.stdout, 'stdout'), stderr=_read_capped(paths.stderr, 'stderr'),
                             reply=_read_capped(paths.out, 'reply') if paths.out.exists() else '')
            except BaseException:
                self.check_snapshot(before, cwd)   # a failed call may still have written a session
                raise
            out.traces = self.check_snapshot(before, cwd)
            if code != 0:
                raise StewardError(ErrorCode.UNAVAILABLE, 'error.advisor_cli_failed', provider=self.name,
                                   reason=f'exit {code}: {_tail(out.stderr or out.stdout)}')
            return out
        finally:
            shutil.rmtree(root, ignore_errors=True)   # only the directory created above
            self._empty_cwd(cwd)

    def run(self, probe: Probe, payload_json: str) -> Tuple[Any, List[str]]:
        """The reply object, parsed but not yet validated against the request, and the trace files."""
        out = self.invoke(probe, payload_json)
        return self.reply_object(out), out.traces


# Codex -------------------------------------------------------------------------------------------------------

def _codex_home(home: Path) -> Path:
    env = os.environ.get('CODEX_HOME')
    return Path(env) if env else home / '.codex'


def _files_under(*roots: Path) -> Set[str]:
    found: Set[str] = set()
    for root in roots:
        for here, _dirs, names in os.walk(root):   # a missing root yields nothing; symlinks are not followed
            found.update(os.path.join(here, n) for n in names)
    return found


CODEX_LOGS = ('history.jsonl', 'session_index.jsonl')   # appended to by every session; read only to look for us


def _names_our_dir(first_line: str, ours: Set[str]) -> bool:
    try:
        meta = json.loads(first_line)
        return bool(meta.get('type') == 'session_meta' and meta['payload']['cwd'] in ours)
    except (ValueError, KeyError, TypeError, AttributeError):
        return any(d in first_line for d in ours)   # unparseable (maybe cut at the cap) but names our directory


class CodexProvider(Provider):
    name = 'codex'
    exe_name = 'codex'

    def probe(self) -> Probe:
        exe = self._which()
        if exe is None:
            return Probe(False, None, 'codex was not found on PATH')
        try:
            version = _probe_command([exe, '--version']).strip().splitlines()[0].strip()
            listed = _probe_command([exe, 'features', 'list'])
        except (RuntimeError, IndexError) as e:
            return Probe(False, None, str(e) or 'codex --version printed nothing', exe)
        names = {line.split()[0] for line in listed.splitlines() if line.strip()}
        if not {'shell_tool', 'unified_exec'} <= names:
            # A read-only sandbox still lets a shell read the whole disk, so without the switch we do not run.
            return Probe(False, version, 'shell cannot be disabled', exe)
        return Probe(True, version, '', exe, tuple(n for n in CODEX_DISABLE if n in names))

    def argv(self, probe: Probe, paths: Mapping[str, str]) -> List[str]:
        argv = [probe.exe or self.exe_name, 'exec', '--ephemeral', '--sandbox', 'read-only', '--skip-git-repo-check',
                '--ignore-user-config', '--ignore-rules']
        for name in probe.disable:
            argv += ['--disable', name]
        return argv + ['-C', paths['cwd'], '--output-schema', paths['schema'], '-o', paths['out'], '-']

    def stdin_text(self, payload_json: str) -> str:
        return prompts.combined_prompt(payload_json)

    def _dirs(self) -> List[Path]:
        base = _codex_home(self.home)
        return [base / 'sessions', base / 'archived_sessions']

    def _log_files(self) -> List[Path]:
        return [_codex_home(self.home) / name for name in CODEX_LOGS]

    def snapshot(self, cwd: Path) -> Dict[str, Any]:
        sizes = {}
        for path in self._log_files():
            try:
                sizes[str(path)] = path.stat().st_size
            except FileNotFoundError:
                sizes[str(path)] = 0
        return {'files': _files_under(*self._dirs()), 'sizes': sizes}

    def check_snapshot(self, before: Dict[str, Any], cwd: Path) -> List[str]:
        """Fail if a session file with our working directory as its cwd appeared, or if the history files grew by
        lines that name it. Other new files may belong to the user's own live Codex and are ignored."""
        ours = {str(cwd), os.path.realpath(cwd)}
        for path in sorted(_files_under(*self._dirs()) - before['files']):
            try:
                with open(path, 'rb') as f:
                    line = f.readline(FIRST_LINE_CAP).decode('utf-8', errors='replace')
            except OSError:
                continue   # gone again: someone else's temporary file
            if _names_our_dir(line, ours):
                raise StewardError(ErrorCode.INVALID, 'error.advisor_left_session', path=path)
        for path_text, old_size in before['sizes'].items():
            try:
                with open(path_text, 'rb') as f:
                    f.seek(old_size)
                    appended = f.read(FIRST_LINE_CAP).decode('utf-8', errors='replace')
            except FileNotFoundError:
                continue
            if any(d in appended for d in ours):
                raise StewardError(ErrorCode.INVALID, 'error.advisor_left_session', path=path_text)
        return []

    def reply_object(self, out: Output) -> Any:
        if not out.reply.strip():
            raise StewardError(ErrorCode.UNAVAILABLE, 'error.advisor_cli_failed', provider=self.name,
                               reason=f'no reply file: {_tail(out.stderr)}')
        return validate.parse_exact(out.reply)


# Claude ------------------------------------------------------------------------------------------------------

def _claude_projects(home: Path) -> Path:
    env = os.environ.get('CLAUDE_CONFIG_DIR')
    return (Path(env) if env else home / '.claude') / 'projects'


def _claude_json(home: Path) -> Path:
    env = os.environ.get('CLAUDE_CONFIG_DIR')
    return Path(env) / '.claude.json' if env else home / '.claude.json'


def claude_project_names(cwd: Path) -> Set[str]:
    """Directory names Claude Code could use under projects/ for this cwd. Its encoding replaces path separators
    and dots with '-'; the wider form also replaces every other non-alphanumeric character."""
    names: Set[str] = set()
    for p in {str(cwd), os.path.realpath(cwd)}:
        names.add(p.replace('/', '-').replace('.', '-'))
        names.add(re.sub(r'[^A-Za-z0-9]', '-', p))
    return names


class ClaudeProvider(Provider):
    name = 'claude'
    exe_name = 'claude'

    def probe(self) -> Probe:
        exe = self._which()
        if exe is None:
            return Probe(False, None, 'claude was not found on PATH')
        try:
            version = _probe_command([exe, '--version']).strip().splitlines()[0].strip()
            help_text = _probe_command([exe, '--help'])
        except (RuntimeError, IndexError) as e:
            return Probe(False, None, str(e) or 'claude --version printed nothing', exe)
        missing = [f for f in CLAUDE_FLAGS if not re.search(r'(?<![\w-])' + re.escape(f) + r'(?![\w-])', help_text)]
        if missing:
            return Probe(False, version, 'this claude lacks ' + ', '.join(missing), exe)
        return Probe(True, version, '', exe)

    def argv(self, probe: Probe, paths: Mapping[str, str]) -> List[str]:
        return [probe.exe or self.exe_name, '-p', '--tools', '', '--safe-mode', '--strict-mcp-config',
                '--no-session-persistence', '--permission-prompts', 'none', '--output-format', 'json',
                '--json-schema', prompts.schema_json(), '--system-prompt', prompts.SYSTEM_PROMPT]

    def stdin_text(self, payload_json: str) -> str:
        return prompts.user_message(payload_json)

    def snapshot(self, cwd: Path) -> Dict[str, Any]:
        """What sits under projects/ for our directory: path -> (mtime, size), directories included."""
        found: Dict[str, Any] = {}
        projects = _claude_projects(self.home)
        for name in sorted(claude_project_names(cwd)):
            for here, dirs, files in os.walk(projects / name):
                for entry in [here] + [os.path.join(here, n) for n in files]:
                    st = os.lstat(entry)
                    found[entry] = (st.st_mtime_ns, st.st_size)
        return found

    def check_snapshot(self, before: Dict[str, Any], cwd: Path) -> List[str]:
        after = self.snapshot(cwd)
        if after and after != before:
            raise StewardError(ErrorCode.INVALID, 'error.advisor_left_session', path=sorted(after)[0])
        return self._claude_json_traces(cwd)

    def _claude_json_traces(self, cwd: Path) -> List[str]:
        """~/.claude.json keeps one entry per directory Claude Code has run in. With a fixed working directory
        that is one entry for good, so it is reported, not treated as a failure. A plain text search: the file is
        rewritten by the user's live Claude and is only read here."""
        path = _claude_json(self.home)
        try:
            with open(path, 'rb') as f:
                text = f.read(CLAUDE_JSON_CAP).decode('utf-8', errors='replace')
        except FileNotFoundError:
            return []
        for p in {str(cwd), os.path.realpath(cwd)}:
            if json.dumps(p) in text or json.dumps(p, ensure_ascii=False) in text:
                home = str(self.home)
                return [str(path).replace(home, '~', 1) if str(path).startswith(home + os.sep) else str(path)]
        return []

    def reply_object(self, out: Output) -> Any:
        return validate.claude_structured_output(out.stdout)


PROVIDERS = {'codex': CodexProvider, 'claude': ClaudeProvider}
