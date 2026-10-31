from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from advisor_support import AdvisorCase
from disk_steward.advisor import prompts, providers
from disk_steward.advisor.providers import CODEX_DISABLE, ClaudeProvider, CodexProvider
from disk_steward.model import ErrorCode

PAYLOAD = json.dumps({'groups': [{'token': 'gaaaaaaa1', 'protected': False, 'label_key': 'rule.codex.cache.label'},
                                 {'token': 'gaaaaaaa2', 'protected': True, 'label_key': 'rule.prot.git.label'}]},
                     separators=(',', ':'))
GOOD = json.dumps({'summary': 's', 'recommendations': []})


class ProviderCase(AdvisorCase):
    scan = False

    @property
    def cwd(self):
        return self.fh.data_dir / 'advisor-cwd'

    def codex(self, **plan):
        self.fakes.install('codex', **plan)
        return CodexProvider(self.fh.home, self.cwd)

    def claude(self, **plan):
        self.fakes.install('claude', **plan)
        return ClaudeProvider(self.fh.home, self.cwd)

    def pid_gone(self, pid, wait=5.0):
        end = time.monotonic() + wait
        while time.monotonic() < end:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.05)
        return False


class CodexProbeTests(ProviderCase):
    def test_not_installed(self):
        probe = CodexProvider(self.fh.home, self.cwd).probe()
        self.assertEqual((probe.available, probe.version), (False, None))
        self.assertIn('not found', probe.reason)

    def test_probe_reads_version_and_features(self):
        probe = self.codex().probe()
        self.assertEqual((probe.available, probe.version, probe.reason), (True, 'codex-cli 9.9.9', ''))
        self.assertEqual(tuple(probe.disable), CODEX_DISABLE)

    def test_only_features_the_probe_lists_are_disabled(self):
        names = ('goals', 'unified_exec', 'apps', 'shell_tool', 'hooks', 'something_else')
        listed = ''.join(f'{n:<40} stable true\n' for n in names)
        impl = self.codex(features=listed, reply=GOOD)
        probe = impl.probe()
        self.assertTrue(probe.available)
        self.assertEqual(tuple(probe.disable), ('shell_tool', 'unified_exec', 'apps', 'hooks', 'goals'))
        self.assertEqual(impl.run(probe, PAYLOAD)[0]['summary'], 's')   # the shell is switchable, so it runs
        argv = self.fakes.record('codex')['argv']
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == '--disable'],
                         ['shell_tool', 'unified_exec', 'apps', 'hooks', 'goals'])

    def test_a_codex_whose_shell_cannot_be_disabled_is_unavailable_and_never_run(self):
        cases = {'no shell_tool': ('unified_exec', 'apps'), 'no unified_exec': ('shell_tool', 'apps'),
                 'neither': ('apps', 'goals')}
        for missing, names in cases.items():
            listed = ''.join(f'{n:<40} stable true\n' for n in names)
            impl = self.codex(features=listed, reply=GOOD)
            with self.subTest(missing=missing):
                probe = impl.probe()
                self.assertEqual((probe.available, probe.reason), (False, 'shell cannot be disabled'))
                self.assertEqual(probe.version, 'codex-cli 9.9.9')
                err = self.expect(ErrorCode.UNAVAILABLE, 'error.advisor_provider_unavailable', impl.run, probe,
                                  PAYLOAD)
                self.assertEqual(err.details, {'provider': 'codex', 'reason': 'shell cannot be disabled'})
                self.assertFalse(self.fakes.called('codex'))

    def test_a_failing_version_probe_makes_it_unavailable(self):
        probe = self.codex(version_exit=3).probe()
        self.assertFalse(probe.available)
        self.assertIn('exited with 3', probe.reason)


class CodexRunTests(ProviderCase):
    def test_exact_argv_cwd_stdin_and_schema(self):
        listed = ''.join(f'{n:<40} stable true\n' for n in ('shell_tool', 'unified_exec', 'plugins', 'goals', 'not_ours'))
        impl = self.codex(features=listed, reply=GOOD)
        probe = impl.probe()
        self.assertEqual(impl.run(probe, PAYLOAD), ({'summary': 's', 'recommendations': []}, []))
        rec = self.fakes.record('codex')
        argv = rec['argv']
        cwd, schema, out = argv[argv.index('-C') + 1], argv[argv.index('--output-schema') + 1], argv[argv.index('-o') + 1]
        self.assertEqual(argv, ['exec', '--ephemeral', '--sandbox', 'read-only', '--skip-git-repo-check',
                                '--ignore-user-config', '--ignore-rules',
                                '--disable', 'shell_tool', '--disable', 'unified_exec', '--disable', 'plugins',
                                '--disable', 'goals', '-C', cwd, '--output-schema', schema, '-o', out, '-'])
        self.assertEqual(cwd, os.path.realpath(self.cwd), 'one fixed working directory, not a fresh one per call')
        self.assertEqual(rec['cwd'], cwd)
        self.assertEqual(rec['listing'], [], 'the working directory must start empty')
        self.assertEqual(rec['stdin'], prompts.combined_prompt(PAYLOAD))
        self.assertEqual(json.loads(rec['schema_text']), prompts.REPLY_SCHEMA)
        self.assertEqual(rec['pgid'], rec['pid'], 'the child must lead its own process group')
        self.assertEqual(rec['home'], str(self.fh.home))
        for path in (schema, out):
            self.assertFalse(os.path.exists(path), 'the temp files must be gone afterwards')
        self.assertEqual(os.listdir(cwd), [])

    def test_the_command_line_helper_uses_placeholders_and_the_same_flags(self):
        impl = self.codex()
        argv = impl.command_line(impl.probe())
        self.assertEqual(argv[1:8], ['exec', '--ephemeral', '--sandbox', 'read-only', '--skip-git-repo-check',
                                     '--ignore-user-config', '--ignore-rules'])
        self.assertEqual(argv[-7:], ['-C', os.path.realpath(self.cwd), '--output-schema',
                                     providers.PLACEHOLDERS['schema'], '-o', providers.PLACEHOLDERS['out'], '-'])
        self.assertFalse(self.fakes.called('codex'))

    def test_the_user_environment_is_kept(self):
        impl = self.codex(reply=GOOD)
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'k', 'SOME_LOGIN_VAR': 'v'}):
            impl.run(impl.probe(), PAYLOAD)
        env_keys = self.fakes.record('codex')['env_keys']
        self.assertIn('HOME', env_keys)
        self.assertIn('OPENAI_API_KEY', env_keys)
        self.assertIn('SOME_LOGIN_VAR', env_keys)

    def test_nonzero_exit_is_a_cli_failure(self):
        impl = self.codex(exit=1, stderr='no network\n')
        err = self.expect(ErrorCode.UNAVAILABLE, 'error.advisor_cli_failed', impl.run, impl.probe(), PAYLOAD)
        self.assertEqual(err.details['provider'], 'codex')
        self.assertIn('no network', err.details['reason'])

    def test_no_reply_file_is_a_cli_failure(self):
        impl = self.codex()
        self.expect(ErrorCode.UNAVAILABLE, 'error.advisor_cli_failed', impl.run, impl.probe(), PAYLOAD)

    def test_the_reply_file_must_be_exactly_the_json(self):
        for reply in ('```json\n' + GOOD + '\n```', GOOD + ' and some words', 'sorry'):
            impl = self.codex(reply=reply)
            with self.subTest(reply=reply[:20]):
                self.expect(ErrorCode.INVALID, 'error.advisor_invalid_reply', impl.run, impl.probe(), PAYLOAD)

    def test_an_unavailable_provider_refuses_to_run(self):
        impl = CodexProvider(self.fh.home, self.cwd)
        self.expect(ErrorCode.UNAVAILABLE, 'error.advisor_provider_unavailable', impl.run, impl.probe(), PAYLOAD)

    def test_timeout_kills_the_whole_process_group(self):
        pid_file = self.fh.root / 'child.pid'
        impl = self.codex(sleep=60, child_pid_file=str(pid_file), reply=GOOD)
        probe = impl.probe()
        started = time.monotonic()
        with mock.patch.object(providers, 'TIMEOUT_SECONDS', 1):
            err = self.expect(ErrorCode.UNAVAILABLE, 'error.advisor_timeout', impl.run, probe, PAYLOAD)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(err.details, {'seconds': 1})
        rec = self.fakes.record('codex')
        self.assertTrue(self.pid_gone(rec['pid']), 'the CLI itself must be dead')
        self.assertTrue(self.pid_gone(int(pid_file.read_text())), 'its child in the same group must be dead too')

    def test_runaway_output_is_cut_off(self):
        impl = self.codex(flood=5000, sleep=60, reply=GOOD)
        started = time.monotonic()
        with mock.patch.object(providers, 'OUTPUT_CAP', 1000):
            err = self.expect(ErrorCode.INVALID, 'error.advisor_output_too_large', impl.run, impl.probe(), PAYLOAD)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(err.details, {'what': 'stdout.txt'})
        self.assertTrue(self.pid_gone(self.fakes.record('codex')['pid']))

    def test_output_that_arrives_just_before_exit_is_checked_too(self):
        impl = self.codex(flood=5000, reply=GOOD)
        with mock.patch.object(providers, 'OUTPUT_CAP', 1000):
            self.expect(ErrorCode.INVALID, 'error.advisor_output_too_large', impl.run, impl.probe(), PAYLOAD)

    def test_a_huge_reply_file_is_refused(self):
        impl = self.codex(reply=json.dumps({'summary': 'x' * 2000, 'recommendations': []}))
        with mock.patch.object(providers, 'OUTPUT_CAP', 1000):
            self.expect(ErrorCode.INVALID, 'error.advisor_output_too_large', impl.run, impl.probe(), PAYLOAD)


class StableWorkingDirectoryTests(ProviderCase):
    def test_every_call_uses_the_same_private_empty_directory(self):
        impl = self.codex(reply=GOOD)
        probe = impl.probe()
        self.assertFalse(self.cwd.exists(), 'nothing is created before the first call')
        impl.run(probe, PAYLOAD)
        first = self.fakes.record('codex')
        impl.run(probe, PAYLOAD)
        second = self.fakes.record('codex')
        self.assertEqual(first['cwd'], second['cwd'])
        self.assertEqual(second['cwd'], os.path.realpath(self.cwd))
        self.assertEqual(os.stat(self.cwd).st_mode & 0o777, 0o700)
        self.assertEqual(os.listdir(self.cwd), [])

    def test_a_loosely_permissioned_directory_is_tightened(self):
        self.cwd.mkdir(parents=True, mode=0o755)
        os.chmod(self.cwd, 0o755)
        impl = self.codex(reply=GOOD)
        impl.run(impl.probe(), PAYLOAD)
        self.assertEqual(os.stat(self.cwd).st_mode & 0o777, 0o700)

    def test_a_directory_that_is_not_empty_refuses_the_call(self):
        self.cwd.mkdir(parents=True)
        (self.cwd / 'left-over.txt').write_text('x')
        impl = self.codex(reply=GOOD)
        err = self.expect(ErrorCode.INVALID, 'error.advisor_cwd_not_empty', impl.run, impl.probe(), PAYLOAD)
        self.assertEqual(err.details, {'path': os.path.realpath(self.cwd)})
        self.assertFalse(self.fakes.called('codex'))
        self.assertTrue((self.cwd / 'left-over.txt').exists(), 'what it did not write is not deleted by a refusal')

    def test_whatever_a_call_writes_there_is_removed_afterwards(self):
        impl = self.codex(reply=GOOD, write_in_cwd='junk.txt')
        self.assertEqual(impl.run(impl.probe(), PAYLOAD)[0]['summary'], 's')
        self.assertEqual(os.listdir(self.cwd), [])
        self.fakes.update('codex', exit=1)
        self.expect(ErrorCode.UNAVAILABLE, 'error.advisor_cli_failed', impl.run, impl.probe(), PAYLOAD)
        self.assertEqual(os.listdir(self.cwd), [], 'a failed call is cleaned up too')

    def test_a_timeout_leaves_the_directory_empty(self):
        impl = self.codex(sleep=60, reply=GOOD, write_in_cwd='junk.txt')
        with mock.patch.object(providers, 'TIMEOUT_SECONDS', 1):
            self.expect(ErrorCode.UNAVAILABLE, 'error.advisor_timeout', impl.run, impl.probe(), PAYLOAD)
        self.assertEqual(os.listdir(self.cwd), [])

    def test_the_dry_run_command_names_the_fixed_directory(self):
        impl = self.codex()
        argv = impl.command_line(impl.probe())
        self.assertEqual(argv[argv.index('-C') + 1], os.path.realpath(self.cwd))


class ProcessGroupAfterExitTests(ProviderCase):
    def test_a_child_left_running_after_a_normal_exit_is_killed(self):
        pid_file = self.fh.root / 'child.pid'
        impl = self.codex(child_pid_file=str(pid_file), reply=GOOD)   # the fake exits 0 and leaves `sleep 60` behind
        self.assertEqual(impl.run(impl.probe(), PAYLOAD)[0]['summary'], 's')
        self.assertTrue(self.pid_gone(int(pid_file.read_text())), 'no child may survive the call')

    def test_the_same_for_claude(self):
        pid_file = self.fh.root / 'child.pid'
        impl = self.claude(child_pid_file=str(pid_file), reply=GOOD)
        self.assertEqual(impl.run(impl.probe(), PAYLOAD)[0]['summary'], 's')
        self.assertTrue(self.pid_gone(int(pid_file.read_text())))


class CodexHistoryCheckTests(ProviderCase):
    def codex_file(self, name):
        return Path(self.fh.home, '.codex', name)

    def test_a_history_line_that_names_our_directory_fails_loudly(self):
        for name in ('history.jsonl', 'session_index.jsonl'):
            impl = self.codex(reply=GOOD, log_file=name, log_line='ours')
            with self.subTest(file=name):
                err = self.expect(ErrorCode.INVALID, 'error.advisor_left_session', impl.run, impl.probe(), PAYLOAD)
                self.assertEqual(err.details['path'], str(self.codex_file(name)))

    def test_lines_about_something_else_are_not_ours(self):
        for name in ('history.jsonl', 'session_index.jsonl'):
            impl = self.codex(reply=GOOD, log_file=name, log_line='other')
            with self.subTest(file=name):
                self.assertEqual(impl.run(impl.probe(), PAYLOAD)[0]['summary'], 's')
                self.assertGreater(self.codex_file(name).stat().st_size, 0)

    def test_only_what_was_appended_during_the_call_counts(self):
        old = self.codex_file('history.jsonl')
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_text(json.dumps({'text': os.path.realpath(self.cwd)}) + '\n')   # an earlier mention
        impl = self.codex(reply=GOOD, log_file='history.jsonl', log_line='other')
        self.assertEqual(impl.run(impl.probe(), PAYLOAD)[0]['summary'], 's')

    def test_a_failed_call_that_wrote_history_reports_it(self):
        impl = self.codex(exit=1, log_file='history.jsonl', log_line='ours')
        self.expect(ErrorCode.INVALID, 'error.advisor_left_session', impl.run, impl.probe(), PAYLOAD)

    def test_a_shrunk_file_is_not_a_finding(self):
        path = self.codex_file('history.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('x' * 5000)
        impl = self.codex(reply=GOOD)
        probe = impl.probe()
        before = impl.snapshot(Path(os.path.realpath(self.cwd)))
        path.write_text('y')
        self.assertEqual(impl.check_snapshot(before, Path(os.path.realpath(self.cwd))), [])


class CodexSessionCheckTests(ProviderCase):
    def sessions(self):
        return Path(self.fh.home, '.codex')

    def test_a_session_with_our_cwd_fails_loudly(self):
        impl = self.codex(reply=GOOD, session='ours')
        err = self.expect(ErrorCode.INVALID, 'error.advisor_left_session', impl.run, impl.probe(), PAYLOAD)
        self.assertTrue(err.details['path'].endswith('rollout-fake.jsonl'))
        self.assertTrue(os.path.exists(err.details['path']), 'the leftover is reported, not deleted')

    def test_archived_sessions_are_checked_too(self):
        impl = self.codex(reply=GOOD, session='ours', session_dir='archived_sessions')
        self.expect(ErrorCode.INVALID, 'error.advisor_left_session', impl.run, impl.probe(), PAYLOAD)

    def test_a_session_with_another_cwd_is_not_ours(self):
        impl = self.codex(reply=GOOD, session='other')
        self.assertEqual(impl.run(impl.probe(), PAYLOAD)[0]['summary'], 's')

    def test_an_unparseable_first_line_that_names_our_dir_still_counts(self):
        impl = self.codex(reply=GOOD, session='garbled')
        self.expect(ErrorCode.INVALID, 'error.advisor_left_session', impl.run, impl.probe(), PAYLOAD)

    def test_files_that_were_there_before_are_ignored(self):
        old = self.sessions() / 'sessions' / '2026' / '10' / '01'
        old.mkdir(parents=True)
        (old / 'rollout-old.jsonl').write_text('{"type":"session_meta","payload":{"cwd":"/x"}}\n')
        impl = self.codex(reply=GOOD)
        self.assertEqual(impl.run(impl.probe(), PAYLOAD)[0]['summary'], 's')

    def test_a_failed_call_that_left_a_session_reports_the_session(self):
        impl = self.codex(exit=1, session='ours')
        self.expect(ErrorCode.INVALID, 'error.advisor_left_session', impl.run, impl.probe(), PAYLOAD)

    def test_codex_home_overrides_the_default_location(self):
        impl = self.codex(reply=GOOD, session='ours')
        elsewhere = self.fh.root / 'codex-home'
        with mock.patch.dict(os.environ, {'CODEX_HOME': str(elsewhere)}):
            err = self.expect(ErrorCode.INVALID, 'error.advisor_left_session', impl.run, impl.probe(), PAYLOAD)
        self.assertTrue(err.details['path'].startswith(str(elsewhere)))


class ClaudeProbeTests(ProviderCase):
    def test_not_installed(self):
        probe = ClaudeProvider(self.fh.home, self.cwd).probe()
        self.assertFalse(probe.available)
        self.assertIn('not found', probe.reason)

    def test_probe_reads_the_version_and_checks_every_flag(self):
        probe = self.claude().probe()
        self.assertEqual((probe.available, probe.version), (True, '9.9.9 (Claude Code)'))

    def test_a_missing_flag_makes_it_unavailable_with_the_flag_named(self):
        from advisor_support import CLAUDE_HELP
        probe = self.claude(help=CLAUDE_HELP.replace('--safe-mode', '--safe')).probe()
        self.assertFalse(probe.available)
        self.assertIn('--safe-mode', probe.reason)
        self.assertEqual(probe.version, '9.9.9 (Claude Code)')

    def test_a_flag_that_only_appears_inside_another_does_not_count(self):
        from advisor_support import CLAUDE_HELP
        helped = CLAUDE_HELP.replace('  --tools <tools...>       Specify the list of available tools\n', '')
        probe = self.claude(help=helped).probe()   # '--allowedTools' is still listed
        self.assertFalse(probe.available)
        self.assertIn('--tools', probe.reason)


class ClaudeRunTests(ProviderCase):
    def test_exact_argv_cwd_and_stdin(self):
        impl = self.claude(reply=GOOD)
        self.assertEqual(impl.run(impl.probe(), PAYLOAD), ({'summary': 's', 'recommendations': []}, []))
        rec = self.fakes.record('claude')
        self.assertEqual(rec['argv'], ['-p', '--tools', '', '--safe-mode', '--strict-mcp-config',
                                       '--no-session-persistence', '--permission-prompts', 'none',
                                       '--output-format', 'json', '--json-schema', prompts.schema_json(),
                                       '--system-prompt', prompts.SYSTEM_PROMPT])
        self.assertNotIn('--bare', rec['argv'])
        self.assertEqual(rec['stdin'], prompts.user_message(PAYLOAD))
        self.assertEqual(rec['listing'], [])
        self.assertEqual((rec['cwd'], os.listdir(rec['cwd'])), (os.path.realpath(self.cwd), []))
        self.assertEqual(rec['pgid'], rec['pid'])
        self.assertEqual(rec['home'], str(self.fh.home))

    def test_the_command_line_helper_matches(self):
        impl = self.claude()
        argv = impl.command_line(impl.probe())
        self.assertEqual(argv[1:], ['-p', '--tools', '', '--safe-mode', '--strict-mcp-config',
                                    '--no-session-persistence', '--permission-prompts', 'none', '--output-format',
                                    'json', '--json-schema', prompts.schema_json(), '--system-prompt',
                                    prompts.SYSTEM_PROMPT])

    def test_an_error_envelope_is_a_failure(self):
        env = json.dumps({'type': 'result', 'subtype': 'success', 'is_error': True, 'result': 'Not logged in'})
        impl = self.claude(envelope=env)
        err = self.expect(ErrorCode.UNAVAILABLE, 'error.advisor_cli_failed', impl.run, impl.probe(), PAYLOAD)
        self.assertIn('Not logged in', err.details['reason'])

    def test_non_json_stdout_is_an_invalid_reply(self):
        impl = self.claude(envelope='hello')
        self.expect(ErrorCode.INVALID, 'error.advisor_invalid_reply', impl.run, impl.probe(), PAYLOAD)

    def test_nonzero_exit(self):
        impl = self.claude(exit=1, stderr='crashed')
        err = self.expect(ErrorCode.UNAVAILABLE, 'error.advisor_cli_failed', impl.run, impl.probe(), PAYLOAD)
        self.assertIn('crashed', err.details['reason'])

    def test_timeout(self):
        impl = self.claude(sleep=60, reply=GOOD)
        with mock.patch.object(providers, 'TIMEOUT_SECONDS', 1):
            self.expect(ErrorCode.UNAVAILABLE, 'error.advisor_timeout', impl.run, impl.probe(), PAYLOAD)
        self.assertTrue(self.pid_gone(self.fakes.record('claude')['pid']))


class ClaudeSessionCheckTests(ProviderCase):
    def test_a_project_directory_for_our_cwd_fails_loudly(self):
        impl = self.claude(reply=GOOD, claude_project='ours')
        err = self.expect(ErrorCode.INVALID, 'error.advisor_left_session', impl.run, impl.probe(), PAYLOAD)
        self.assertIn(str(Path(self.fh.home, '.claude', 'projects')), err.details['path'])

    def test_an_unrelated_project_directory_is_fine(self):
        impl = self.claude(reply=GOOD, claude_project='other')
        self.assertEqual(impl.run(impl.probe(), PAYLOAD)[0]['summary'], 's')

    def test_candidate_names_cover_both_encodings_and_the_real_path(self):
        names = providers.claude_project_names(Path('/tmp/a.b_c/x'))
        self.assertIn('-tmp-a-b_c-x', names)
        self.assertIn('-tmp-a-b-c-x', names)
        real = os.path.realpath('/tmp/a.b_c/x')
        self.assertIn(real.replace('/', '-').replace('.', '-'), names)

    def test_claude_config_dir_is_honoured(self):
        impl = self.claude(reply=GOOD)
        elsewhere = self.fh.root / 'cc'
        cwd = Path(os.path.realpath(self.cwd))
        name = str(cwd).replace('/', '-').replace('.', '-')
        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(elsewhere)}):
            self.assertEqual(impl.snapshot(cwd), {})
            (elsewhere / 'projects' / name).mkdir(parents=True)
            self.assertEqual(list(impl.snapshot(cwd)), [str(elsewhere / 'projects' / name)])


class ClaudeFixedDirectoryTests(ProviderCase):
    def projects(self):
        return Path(self.fh.home, '.claude', 'projects')

    def test_a_project_directory_that_was_already_there_and_did_not_change_is_not_a_finding(self):
        name = os.path.realpath(self.cwd).replace('/', '-').replace('.', '-')
        old = self.projects() / name
        old.mkdir(parents=True)
        (old / 'session.jsonl').write_text('earlier\n')
        impl = self.claude(reply=GOOD)
        self.assertEqual(impl.run(impl.probe(), PAYLOAD)[0]['summary'], 's')

    def test_a_project_directory_that_grew_during_the_call_is_a_finding(self):
        name = os.path.realpath(self.cwd).replace('/', '-').replace('.', '-')
        old = self.projects() / name
        old.mkdir(parents=True)
        (old / 'earlier.jsonl').write_text('earlier\n')
        impl = self.claude(reply=GOOD, claude_project='ours')
        self.expect(ErrorCode.INVALID, 'error.advisor_left_session', impl.run, impl.probe(), PAYLOAD)

    def test_claude_json_entry_for_our_directory_is_reported_not_failed(self):
        impl = self.claude(reply=GOOD, claude_json='ours')
        reply, traces = impl.run(impl.probe(), PAYLOAD)
        self.assertEqual(reply['summary'], 's')
        self.assertEqual(traces, ['~/.claude.json'])

    def test_no_entry_and_no_file_mean_no_trace(self):
        impl = self.claude(reply=GOOD)
        self.assertEqual(impl.run(impl.probe(), PAYLOAD)[1], [])
        impl = self.claude(reply=GOOD, claude_json='other')
        self.assertEqual(impl.run(impl.probe(), PAYLOAD)[1], [])

    def test_claude_config_dir_moves_the_json_file(self):
        elsewhere = self.fh.root / 'cc'
        elsewhere.mkdir()
        impl = self.claude(reply=GOOD)
        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(elsewhere)}):
            Path(elsewhere, '.claude.json').write_text(json.dumps({'projects': {os.path.realpath(self.cwd): {}}}))
            self.assertEqual(impl.run(impl.probe(), PAYLOAD)[1], [str(elsewhere / '.claude.json')])

    def test_the_json_file_is_only_read(self):
        path = Path(self.fh.home, '.claude.json')
        path.write_text(json.dumps({'projects': {os.path.realpath(self.cwd): {}}}))
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        impl = self.claude(reply=GOOD)
        self.assertEqual(impl.run(impl.probe(), PAYLOAD)[1], ['~/.claude.json'])
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)


class ModuleTests(unittest.TestCase):
    def test_defaults_match_the_contract(self):
        self.assertEqual(providers.TIMEOUT_SECONDS, 180)
        self.assertEqual(providers.OUTPUT_CAP, 1024 * 1024)
        self.assertEqual(CODEX_DISABLE, ('shell_tool', 'unified_exec', 'apps', 'plugins', 'browser_use',
                                         'computer_use', 'in_app_browser', 'multi_agent', 'image_generation',
                                         'view_image', 'memories', 'hooks', 'tool_suggest', 'skill_search',
                                         'code_mode_host', 'goals'))


if __name__ == '__main__':
    unittest.main()
