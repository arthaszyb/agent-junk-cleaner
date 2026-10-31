from __future__ import annotations

import json
import unittest

from disk_steward.advisor import prompts, validate
from disk_steward.model import ErrorCode, StewardError

TOKENS = {'gaaaaaaa1': {'protected': False}, 'gaaaaaaa2': {'protected': False}, 'gbbbbbbb1': {'protected': True}}


def reply(**changes):
    base = {'summary': 'Short summary.',
            'recommendations': [{'group': 'gaaaaaaa1', 'verdict': 'likely_safe', 'reason': 'Old cache.'},
                                {'group': 'gbbbbbbb1', 'verdict': 'keep', 'reason': 'Protected.'}]}
    base.update(changes)
    return base


class Case(unittest.TestCase):
    def rejects(self, fn, *args, contains=''):
        with self.assertRaises(StewardError) as cm:
            fn(*args)
        self.assertEqual((cm.exception.code, cm.exception.message_key),
                         (ErrorCode.INVALID, 'error.advisor_invalid_reply'))
        self.assertIn(contains, cm.exception.details['reason'])
        return cm.exception


class ValidReplyTests(Case):
    def test_accepts_a_valid_reply(self):
        out = validate.validate_reply(reply(), TOKENS)
        self.assertEqual(out['summary'], 'Short summary.')
        self.assertEqual([r['group'] for r in out['recommendations']], ['gaaaaaaa1', 'gbbbbbbb1'])
        self.assertEqual(out['recommendations'][0], {'group': 'gaaaaaaa1', 'verdict': 'likely_safe',
                                                     'reason': 'Old cache.'})

    def test_an_empty_recommendation_list_is_valid(self):
        self.assertEqual(validate.validate_reply(reply(recommendations=[]), TOKENS)['recommendations'], [])

    def test_limits_are_inclusive(self):
        many = [{'group': f'g{i:08x}', 'verdict': 'review', 'reason': 'x' * prompts.MAX_REASON}
                for i in range(prompts.MAX_RECOMMENDATIONS)]
        tokens = {r['group']: {'protected': False} for r in many}
        out = validate.validate_reply({'summary': 's' * prompts.MAX_SUMMARY, 'recommendations': many}, tokens)
        self.assertEqual(len(out['recommendations']), prompts.MAX_RECOMMENDATIONS)

    def test_newlines_and_tabs_are_allowed_in_text(self):
        validate.validate_reply(reply(summary='line one\nline two\tend'), TOKENS)


class RejectionTests(Case):
    def test_unknown_token(self):
        bad = reply(recommendations=[{'group': 'gdeadbeef', 'verdict': 'review', 'reason': 'x'}])
        self.rejects(validate.validate_reply, bad, TOKENS, contains='not a group of this request')

    def test_duplicate_token(self):
        row = {'group': 'gaaaaaaa1', 'verdict': 'review', 'reason': 'x'}
        self.rejects(validate.validate_reply, reply(recommendations=[row, dict(row)]), TOKENS, contains='twice')

    def test_extra_keys_anywhere(self):
        self.rejects(validate.validate_reply, dict(reply(), extra=1), TOKENS, contains='unexpected key')
        row = {'group': 'gaaaaaaa1', 'verdict': 'review', 'reason': 'x', 'command': 'rm -rf /'}
        self.rejects(validate.validate_reply, reply(recommendations=[row]), TOKENS, contains='unexpected key')

    def test_missing_keys(self):
        self.rejects(validate.validate_reply, {'summary': 's'}, TOKENS, contains='recommendations')
        self.rejects(validate.validate_reply, reply(recommendations=[{'group': 'gaaaaaaa1', 'verdict': 'keep'}]),
                     TOKENS, contains='reason')

    def test_wrong_verdict(self):
        for verdict in ('safe', 'LIKELY_SAFE', 'delete', '', None, 1, True):
            row = {'group': 'gaaaaaaa1', 'verdict': verdict, 'reason': 'x'}
            with self.subTest(verdict=verdict):
                self.rejects(validate.validate_reply, reply(recommendations=[row]), TOKENS, contains='verdict')

    def test_oversize_fields(self):
        self.rejects(validate.validate_reply, reply(summary='s' * (prompts.MAX_SUMMARY + 1)), TOKENS,
                     contains='summary is longer')
        row = {'group': 'gaaaaaaa1', 'verdict': 'review', 'reason': 'r' * (prompts.MAX_REASON + 1)}
        self.rejects(validate.validate_reply, reply(recommendations=[row]), TOKENS, contains='reason is longer')
        rows = [{'group': 'gaaaaaaa1', 'verdict': 'review', 'reason': 'x'}] * (prompts.MAX_RECOMMENDATIONS + 1)
        self.rejects(validate.validate_reply, reply(recommendations=rows), TOKENS, contains='more than')

    def test_wrong_types(self):
        self.rejects(validate.validate_reply, [], TOKENS, contains='not an object')
        self.rejects(validate.validate_reply, reply(summary=5), TOKENS, contains='not a string')
        self.rejects(validate.validate_reply, reply(recommendations={}), TOKENS, contains='not a list')
        self.rejects(validate.validate_reply, reply(recommendations=['x']), TOKENS, contains='not an object')
        row = {'group': 7, 'verdict': 'review', 'reason': 'x'}
        self.rejects(validate.validate_reply, reply(recommendations=[row]), TOKENS, contains='not a string')
        row = {'group': 'gaaaaaaa1', 'verdict': 'review', 'reason': None}
        self.rejects(validate.validate_reply, reply(recommendations=[row]), TOKENS, contains='not a string')

    def test_control_characters_are_refused(self):
        self.rejects(validate.validate_reply, reply(summary='hello \x1b[31mred'), TOKENS, contains='control')
        row = {'group': 'gaaaaaaa1', 'verdict': 'review', 'reason': 'bell\x07'}
        self.rejects(validate.validate_reply, reply(recommendations=[row]), TOKENS, contains='control')

    def test_invisible_format_characters_are_refused(self):
        # bidi embeddings/overrides/isolates, zero-width marks, the BOM, tag characters, the soft hyphen
        bad = ['\u202a', '\u202b', '\u202c', '\u202d', '\u202e', '\u2066', '\u2067', '\u2068', '\u2069',
               '\u200b', '\u200c', '\u200d', '\u200e', '\u200f', '\ufeff', '\U000e0041', '\u00ad']
        for ch in bad:
            with self.subTest(char=f'U+{ord(ch):04X}'):
                self.rejects(validate.validate_reply, reply(summary=f'safe{ch}text'), TOKENS, contains='invisible')
                row = {'group': 'gaaaaaaa1', 'verdict': 'review', 'reason': f'x{ch}y'}
                self.rejects(validate.validate_reply, reply(recommendations=[row]), TOKENS, contains='invisible')
        self.assertEqual(len({__import__('unicodedata').category(c) for c in bad}), 1)   # all of them are Cf

    def test_ordinary_text_with_accents_cjk_and_emoji_passes(self):
        row = {'group': 'gaaaaaaa1', 'verdict': 'review', 'reason': 'Caché 缓存 – 40 GB \u2014 ok 🙂'}
        validate.validate_reply(reply(summary='Résumé 总结', recommendations=[row]), TOKENS)

    def test_a_protected_group_cannot_be_likely_safe(self):
        row = {'group': 'gbbbbbbb1', 'verdict': 'likely_safe', 'reason': 'x'}
        self.rejects(validate.validate_reply, reply(recommendations=[row]), TOKENS, contains='protected')
        for verdict in ('keep', 'review'):
            row['verdict'] = verdict
            validate.validate_reply(reply(recommendations=[row]), TOKENS)

    def test_echoed_reply_text_is_short(self):
        row = {'group': 'g' + 'z' * 5000, 'verdict': 'review', 'reason': 'x'}
        err = self.rejects(validate.validate_reply, reply(recommendations=[row]), TOKENS)
        self.assertLess(len(err.details['reason']), 200)


class ParseTests(Case):
    def test_exact_json_only(self):
        self.assertEqual(validate.parse_exact(' \n{"a": 1}\n'), {'a': 1})
        for text in ('', 'not json', '{"a": 1} trailing', '```json\n{"a": 1}\n```', '{"a": 1}{"b": 2}', '[1', 'NaN',
                     '{"a": Infinity}'):
            with self.subTest(text=text):
                self.rejects(validate.parse_exact, text, contains='not valid JSON')

    def test_duplicate_keys_are_refused(self):
        self.rejects(validate.parse_exact, '{"summary": "a", "summary": "b"}', contains='duplicate key')

    def test_deep_nesting_is_refused_not_crashed(self):
        deep = '[' * 100000 + ']' * 100000
        try:
            parsed = validate.parse_exact(deep)   # Python 3.9 raises RecursionError inside, newer ones parse it
        except StewardError as e:
            self.assertEqual(e.message_key, 'error.advisor_invalid_reply')
        else:
            self.rejects(validate.validate_reply, parsed, TOKENS, contains='not an object')

    def test_manual_accepts_bare_json_and_one_fenced_block(self):
        body = json.dumps(reply())
        self.assertEqual(validate.parse_manual(body), reply())
        for fenced in (f'```json\n{body}\n```', f'  ```json\r\n{body}\r\n```\n', f'```\n{body}\n```',
                       f'```JSON\n{body}\n```'):
            with self.subTest(fenced=fenced):
                self.assertEqual(validate.parse_manual(fenced), reply())

    def test_manual_refuses_everything_else(self):
        body = json.dumps(reply())
        self.rejects(validate.parse_manual, f'```json\n{body}\n```\n\n```json\n{body}\n```',
                     contains='more than one code block')
        self.rejects(validate.parse_manual, f'Here you go:\n```json\n{body}\n```', contains='outside the code block')
        self.rejects(validate.parse_manual, f'```json\n{body}\n```\nHope that helps!',
                     contains='outside the code block')
        self.rejects(validate.parse_manual, f'Sure. {body}', contains='not valid JSON')
        self.rejects(validate.parse_manual, '', contains='not valid JSON')
        self.rejects(validate.parse_manual, '```json\n```', contains='outside the code block')


class ClaudeEnvelopeTests(Case):
    def envelope(self, **kw):
        # the keys a real `claude -p --output-format json --json-schema` envelope carried (Claude Code 2.1.299)
        base = {'type': 'result', 'subtype': 'success', 'is_error': False, 'api_error_status': None,
                'stop_reason': 'tool_use', 'terminal_reason': 'completed', 'num_turns': 2,
                'result': json.dumps(reply()), 'structured_output': reply(), 'session_id': 'x-y-z',
                'total_cost_usd': 0.02, 'usage': {'input_tokens': 2}, 'modelUsage': {}, 'permission_denials': []}
        base.update(kw)
        return json.dumps(base)

    def test_structured_output_is_returned(self):
        self.assertEqual(validate.claude_structured_output(self.envelope()), reply())

    def test_is_error_is_a_cli_failure(self):
        with self.assertRaises(StewardError) as cm:
            validate.claude_structured_output(self.envelope(is_error=True, subtype='error_during_execution',
                                                            result='Not logged in'))
        self.assertEqual((cm.exception.code, cm.exception.message_key),
                         (ErrorCode.UNAVAILABLE, 'error.advisor_cli_failed'))
        self.assertEqual(cm.exception.details, {'provider': 'claude', 'reason': 'Not logged in'})

    def test_a_non_success_subtype_is_a_cli_failure(self):
        for subtype in ('error_max_turns', 'error_during_execution'):
            with self.subTest(subtype=subtype):
                with self.assertRaises(StewardError) as cm:
                    validate.claude_structured_output(self.envelope(subtype=subtype, is_error=False, result=''))
                self.assertEqual(cm.exception.message_key, 'error.advisor_cli_failed')
                self.assertEqual(cm.exception.details['reason'], subtype)

    def test_missing_structured_output_and_bad_shapes(self):
        env = json.loads(self.envelope())
        del env['structured_output']
        self.rejects(validate.claude_structured_output, json.dumps(env), contains='structured_output')
        self.rejects(validate.claude_structured_output, '[]', contains='not a JSON object')
        self.rejects(validate.claude_structured_output, 'plain text', contains='not valid JSON')
        self.rejects(validate.claude_structured_output, '', contains='not valid JSON')

    def test_the_result_text_is_not_trusted_instead_of_structured_output(self):
        env = json.loads(self.envelope())
        env['structured_output'] = {'summary': 's'}   # present but wrong: validation (later) rejects it
        out = validate.claude_structured_output(json.dumps(env))
        self.rejects(validate.validate_reply, out, TOKENS, contains='recommendations')


class SchemaTests(unittest.TestCase):
    def test_schema_matches_the_limits_the_validator_enforces(self):
        s = prompts.REPLY_SCHEMA
        self.assertFalse(s['additionalProperties'])
        self.assertEqual(s['properties']['summary']['maxLength'], 1500)
        recs = s['properties']['recommendations']
        self.assertEqual(recs['maxItems'], 60)
        self.assertEqual(recs['items']['properties']['reason']['maxLength'], 400)
        self.assertFalse(recs['items']['additionalProperties'])
        self.assertEqual(recs['items']['properties']['verdict']['enum'], ['likely_safe', 'review', 'keep'])
        self.assertEqual(json.loads(prompts.schema_json()), s)

    def test_system_prompt_states_the_boundaries(self):
        text = prompts.SYSTEM_PROMPT
        for needle in ('cannot see', 'review or keep', 'Never recommend commands', 'protected', 'JSON'):
            self.assertIn(needle, text)

    def test_manual_prompt_carries_instructions_payload_and_schema(self):
        text = prompts.manual_prompt('{"groups":[]}')
        self.assertIn(prompts.SYSTEM_PROMPT, text)
        self.assertIn('{"groups":[]}', text)
        self.assertIn('"additionalProperties": false', text)


if __name__ == '__main__':
    unittest.main()
