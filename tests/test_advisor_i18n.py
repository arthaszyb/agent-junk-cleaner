from __future__ import annotations

import re
import unittest
from pathlib import Path

import disk_steward.advisor as advisor_pkg
from disk_steward.i18n import LOCALES, catalog
from disk_steward.model import ErrorCode, StewardError
from support import render_error

# Details every advisor error is raised with; a message may use no placeholder outside its entry.
DETAILS = {
    'error.advisor_disabled': {},
    'error.advisor_provider_unavailable': {'provider': 'codex', 'reason': 'codex was not found on PATH'},
    'error.advisor_timeout': {'seconds': 180},
    'error.advisor_left_session': {'path': '/tmp/x/rollout.jsonl'},
    'error.advisor_cli_failed': {'provider': 'claude', 'reason': 'exit 1: boom'},
    'error.advisor_output_too_large': {'what': 'stdout.txt'},
    'error.advisor_invalid_reply': {'reason': 'summary is not a string'},
    'error.advisor_unknown_request': {},
    'error.advisor_request_expired': {},
    'error.advisor_no_provider': {},
    'error.advisor_manual_only': {},
    'error.advisor_unknown_provider': {'provider': 'gpt'},
    'error.advisor_scan_changed': {},
    'error.advisor_internal': {'error': 'ValueError: x'},
    'error.advisor_toggle_alone': {},
    'error.advisor_file': {'path': 'reply.json', 'reason': 'No such file'},
    'error.advisor_cwd_not_empty': {'path': '/data/advisor-cwd'},
}


class AdvisorCatalogTests(unittest.TestCase):
    def test_every_error_key_used_in_the_advisor_code_is_defined_and_renders(self):
        root = Path(advisor_pkg.__file__).parent
        used = set()
        for py in list(root.glob('*.py')) + list((root.parent / 'cli').glob('advise_cmd.py')):
            used.update(re.findall(r"'(error\.advisor_[A-Za-z_]+)'", py.read_text(encoding='utf-8')))
        self.assertTrue(used)
        self.assertLessEqual(used, set(DETAILS))
        for loc in LOCALES:
            self.assertEqual({k for k in catalog(loc) if k.startswith('error.advisor_')}, set(DETAILS))
        for key, details in DETAILS.items():
            with self.subTest(key=key):
                self.assertTrue(render_error(StewardError(ErrorCode.INVALID, key, **details)))

    def test_verdict_and_cli_texts_exist_in_both_locales(self):
        for loc in LOCALES:
            for verdict in ('likely_safe', 'review', 'keep'):
                self.assertIn(f'advisor.verdict.{verdict}', catalog(loc))
            self.assertIn('advisor.cli.footer', catalog(loc))
            self.assertIn('AGENTS.md', catalog(loc)['advisor.notice.own_instructions'])
            self.assertIn('CLAUDE.md', catalog(loc)['advisor.notice.own_instructions'])
        self.assertEqual(catalog('en')['advisor.cli.footer'], 'Suggestions only — nothing was selected or changed.')


if __name__ == '__main__':
    unittest.main()
