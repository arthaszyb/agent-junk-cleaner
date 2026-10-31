from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from pathlib import Path

from advisor_support import AdvisorCase, other_app, reply_for
from disk_steward.cli import main

FOOTER = 'Suggestions only — nothing was selected or changed.'


class CliCase(AdvisorCase):
    def run_cli(self, *argv, app=None):
        out, err = io.StringIO(), io.StringIO()
        app = app or self.app
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(list(argv), app_factory=lambda: app)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()


class SwitchTests(CliCase):
    def test_off_by_default_and_the_error_says_how_to_turn_it_on(self):
        for argv in (['advise'], ['advise', '--dry-run'], ['advise', '--provider', 'codex'],
                     ['advise', '--export', 'x.txt'], ['advise', '--import', 'x.txt']):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(*argv)
                self.assertEqual((code, out), (2, ''))
                self.assertIn('disk-steward advise --enable', err)

    def test_enable_and_disable(self):
        code, out, _ = self.run_cli('advise', '--enable')
        self.assertEqual(code, 0)
        self.assertIn('only suggests', out)
        self.assertTrue(self.app.settings.advisor_enabled)
        code, out, _ = self.run_cli('advise', '--disable')
        self.assertEqual((code, out.strip()), (0, 'The AI advisor is off.'))
        self.assertFalse(self.app.settings.advisor_enabled)

    def test_enable_json(self):
        code, out, _ = self.run_cli('advise', '--enable', '--json')
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)['enabled'])

    def test_a_toggle_stands_alone(self):
        code, _, err = self.run_cli('advise', '--enable', '--provider', 'codex')
        self.assertEqual(code, 2)
        self.assertIn('cannot be combined', err)
        self.assertEqual(self.run_cli('advise', '--enable', '--dry-run')[0], 2)   # argparse: mutually exclusive
        self.assertFalse(self.app.settings.advisor_enabled)


class DryRunTests(CliCase):
    def setUp(self):
        super().setUp()
        self.fakes.install('codex', reply='echo')
        self.fakes.install('claude', reply='echo')
        self.adv.set_enabled(True)

    def test_prints_payload_and_command_and_calls_nothing(self):
        code, out, err = self.run_cli('advise', '--dry-run', '--provider', 'codex')
        self.assertEqual((code, err), (0, ''))
        self.assertIn('The JSON holds no paths, names or file contents.', out)
        self.assertIn('Only the JSON payload shown here comes from Disk Steward.', out)
        self.assertIn('Codex may include your global AGENTS.md and its built-in instructions', out)
        self.assertIn('Claude runs in safe mode', out)
        self.assertIn('"label_key": "rule.', out)
        self.assertIn('exec --ephemeral --sandbox read-only --skip-git-repo-check --ignore-user-config --ignore-rules',
                      out)
        self.assertIn('--disable shell_tool', out)
        self.assertIn(f'-C {os.path.realpath(self.fh.data_dir / "advisor-cwd")} ', out)
        self.assertIn('Dry run: nothing was sent.', out)
        self.assertFalse(self.fakes.called('codex'))

    def test_claude_command_line(self):
        code, out, _ = self.run_cli('advise', '--dry-run', '--provider', 'claude')
        self.assertEqual(code, 0)
        self.assertIn('-p --tools', out)
        self.assertIn('--safe-mode --strict-mcp-config --no-session-persistence --permission-prompts none', out)
        self.assertNotIn('--bare', out)
        self.assertFalse(self.fakes.called('claude'))

    def test_json_carries_the_payload_and_the_exact_argv(self):
        code, out, _ = self.run_cli('advise', '--dry-run', '--provider', 'codex', '--json')
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(data['command']['provider'], 'codex')
        self.assertEqual(data['command']['argv'][1:3], ['exec', '--ephemeral'])
        self.assertEqual(data['payload']['coverage'], data['coverage'])
        self.assertIn('Only the JSON payload shown here comes from Disk Steward.', data['notice'])
        self.assertEqual(data['notice_key'], 'advisor.notice.own_instructions')
        self.assertGreater(data['command']['stdin_bytes'], data['bytes'])
        self.assertFalse(self.fakes.called('codex'))

    def test_chinese_dry_run_carries_the_notice_too(self):
        code, out, _ = self.run_cli('--lang', 'zh-CN', 'advise', '--dry-run', '--provider', 'claude')
        self.assertEqual(code, 0)
        self.assertIn('AGENTS.md', out)
        self.assertIn('Disk Steward 无法显示', out)

    def test_manual_has_no_command(self):
        code, out, _ = self.run_cli('advise', '--dry-run', '--provider', 'manual')
        self.assertEqual(code, 0)
        self.assertIn('Manual mode: nothing runs.', out)
        self.assertNotIn('Command that would run', out)

    def test_a_missing_provider_is_a_clear_error(self):
        code, _, err = self.run_cli('advise', '--dry-run')
        self.assertEqual(code, 2)
        self.assertIn('--provider codex, claude or manual', err)

    def test_an_unavailable_provider(self):
        (self.fakes.bin / 'codex').unlink()
        self.adv.status(refresh=True)
        code, _, err = self.run_cli('advise', '--dry-run', '--provider', 'codex')
        self.assertEqual(code, 2)
        self.assertIn('not available', err)


class RunTests(CliCase):
    def setUp(self):
        super().setUp()
        self.adv.set_enabled(True)

    def test_run_prints_summary_recommendations_and_the_footer(self):
        self.fakes.install('claude', reply='echo', verdict='likely_safe', summary='Mostly rebuildable caches.')
        code, out, err = self.run_cli('advise', '--provider', 'claude')
        self.assertEqual(code, 0)
        self.assertIn('Asking claude (9.9.9 (Claude Code))', err)
        self.assertIn('Summary\n  Mostly rebuildable caches.', out)
        self.assertIn('Likely safe  Claude logs', out)
        self.assertIn('    canned reason rule.claude.logs.label', out)
        self.assertIn('Keep  ', out)
        self.assertTrue(out.rstrip().endswith(FOOTER))

    def test_a_claude_json_entry_is_noted(self):
        self.fakes.install('claude', reply='echo', claude_json='ours')
        code, out, _ = self.run_cli('advise', '--provider', 'claude')
        self.assertEqual(code, 0)
        self.assertIn("Note: claude recorded the advisor's fixed working folder in ~/.claude.json.", out)
        self.assertTrue(out.rstrip().endswith(FOOTER))

    def test_no_note_without_a_trace(self):
        self.fakes.install('claude', reply='echo')
        self.assertNotIn('Note:', self.run_cli('advise', '--provider', 'claude')[1])

    def test_the_provider_setting_is_the_default(self):
        self.fakes.install('codex', reply='echo')
        self.app.update_settings({'advisor_provider': 'codex'})
        code, out, _ = self.run_cli('advise')
        self.assertEqual(code, 0)
        self.assertIn(FOOTER, out)

    def test_json_result(self):
        self.fakes.install('codex', reply='echo')
        code, out, err = self.run_cli('advise', '--provider', 'codex', '--json')
        self.assertEqual((code, err), (0, ''))
        result = json.loads(out)
        self.assertEqual(result['provider'], 'codex')
        self.assertFalse(result['stale'])
        self.assertTrue(result['recommendations'])

    def test_chinese_output(self):
        self.fakes.install('codex', reply='echo')
        code, out, _ = self.run_cli('--lang', 'zh-CN', 'advise', '--provider', 'codex')
        self.assertEqual(code, 0)
        self.assertIn('仅为建议，没有选中或改动任何东西。', out)
        self.assertIn('总结', out)

    def test_a_bad_reply_is_an_error_not_a_result(self):
        self.fakes.install('codex', reply=json.dumps({'summary': 's', 'recommendations': [
            {'group': 'gdeadbeef', 'verdict': 'keep', 'reason': 'x'}]}))
        code, out, err = self.run_cli('advise', '--provider', 'codex')
        self.assertEqual((code, out), (2, ''))
        self.assertIn('rejected', err)

    def test_manual_cannot_run(self):
        code, _, err = self.run_cli('advise', '--provider', 'manual')
        self.assertEqual(code, 2)
        self.assertIn('--export FILE', err)

    def test_the_selection_is_untouched(self):
        self.fakes.install('codex', reply='echo', verdict='likely_safe')
        self.app.apply_ops([{'op': 'select_group', 'group': 'claude.logs', 'on': True}])
        before = self.app.spec().to_json()
        self.assertEqual(self.run_cli('advise', '--provider', 'codex')[0], 0)
        self.assertEqual(self.app.spec().to_json(), before)


class ManualFlowTests(CliCase):
    def setUp(self):
        super().setUp()
        self.adv.set_enabled(True)
        self.file = self.fh.root / 'prompt.txt'

    def exported_payload(self):
        text = self.file.read_text(encoding='utf-8')
        body = text.split('--- INPUT ---\n', 1)[1].split('\n\n--- REPLY SCHEMA', 1)[0]
        return json.loads(body)

    def test_export_then_import_in_a_later_process(self):
        code, out, err = self.run_cli('advise', '--export', str(self.file))
        self.assertEqual((code, err), (0, ''))
        self.assertIn(f'Wrote the prompt to {self.file}', out)
        self.assertIn('disk-steward advise --import FILE', out)
        payload = self.exported_payload()
        reply = self.fh.root / 'reply.json'
        reply.write_text('```json\n' + json.dumps(reply_for(payload, 'review', 'Pasted from a chat.')) + '\n```',
                         encoding='utf-8')
        later = other_app(self.fh)
        self.addCleanup(later.close)
        code, out, err = self.run_cli('advise', '--import', str(reply), app=later)
        self.assertEqual((code, err), (0, ''))
        self.assertIn('The reply passed validation.', out)
        self.assertIn('Pasted from a chat.', out)
        self.assertIn('Review  ', out)
        self.assertTrue(out.rstrip().endswith(FOOTER))
        # one import per export
        code, _, err = self.run_cli('advise', '--import', str(reply), app=later)
        self.assertEqual(code, 2)
        self.assertIn('No such advisor request', err)

    def test_import_rejects_a_reply_that_is_not_for_the_export(self):
        self.run_cli('advise', '--export', str(self.file))
        other = self.fh.root / 'other.txt'
        self.run_cli('advise', '--export', str(other))   # a newer export replaces the record
        reply = self.fh.root / 'reply.json'
        reply.write_text(json.dumps(reply_for(self.exported_payload())), encoding='utf-8')
        code, _, err = self.run_cli('advise', '--import', str(reply))
        self.assertEqual(code, 2)
        self.assertIn('not a group of this request', err)

    def test_import_file_problems_are_clear(self):
        code, _, err = self.run_cli('advise', '--import', str(self.fh.root / 'missing.json'))
        self.assertEqual(code, 2)
        self.assertIn('Cannot use', err)

    def test_export_to_an_unwritable_path(self):
        code, _, err = self.run_cli('advise', '--export', str(self.fh.root / 'no' / 'such' / 'dir' / 'p.txt'))
        self.assertEqual(code, 2)
        self.assertIn('Cannot use', err)

    def test_export_json(self):
        code, out, _ = self.run_cli('advise', '--export', str(self.file), '--json')
        data = json.loads(out)
        self.assertEqual((code, Path(data['path'])), (0, self.file))
        self.assertIn('request_id', data)


if __name__ == '__main__':
    unittest.main()
