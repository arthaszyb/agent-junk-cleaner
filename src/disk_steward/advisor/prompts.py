"""Prompt text and the reply schema. English only: the model's words are shown next to localized labels."""
from __future__ import annotations

import json
from typing import Any, Dict

MAX_SUMMARY = 1500
MAX_RECOMMENDATIONS = 60
MAX_REASON = 400
VERDICTS = ('likely_safe', 'review', 'keep')

REPLY_SCHEMA: Dict[str, Any] = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['summary', 'recommendations'],
    'properties': {
        'summary': {'type': 'string', 'maxLength': MAX_SUMMARY},
        'recommendations': {
            'type': 'array',
            'maxItems': MAX_RECOMMENDATIONS,
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': ['group', 'verdict', 'reason'],
                'properties': {
                    'group': {'type': 'string'},
                    'verdict': {'type': 'string', 'enum': list(VERDICTS)},
                    'reason': {'type': 'string', 'maxLength': MAX_REASON},
                },
            },
        },
    },
}

SYSTEM_PROMPT = (
    'You review anonymized disk-usage groups for a macOS cleanup tool. You cannot see any file, name or path. '
    'Each group is described only by numbers and catalog categories, so judge from category, tier, age and '
    'growth.\n'
    '\n'
    'Input is one JSON object.\n'
    '- groups: token (opaque id; answer with it), domain, tier, kind, provider (the product that writes the data), '
    'label_key (catalog name of the category), actions (what the tool could do with it; empty means report only), '
    'has_hint, items, alloc and reclaim (bytes), ages (a pair [items, bytes] for each age bucket in days since the '
    'last change or use: lt7, d7_14, d14_30, d30_90, d90_180, gt180), growth (bytes gained since the previous scan; null when there was no '
    'earlier scan), evidence (catalog evidence keys), protected.\n'
    '- Tiers: junk is rebuildable data; review needs a look from the user first; protected is never removable.\n'
    '- scan: totals by tier and domain, volume numbers and coverage. Only the largest groups are included; coverage '
    'says how much of the total they cover.\n'
    '\n'
    'Task: pick the groups that matter most for freeing space and give each a verdict.\n'
    '- likely_safe: rebuildable or regenerable data that is mostly old or idle; removing it is low regret.\n'
    '- review: removable, but it depends on the user\'s habits; the user should look first.\n'
    '- keep: valuable, recently active, protected, or unclear.\n'
    'When uncertain, choose review or keep. A protected group is always keep. Never recommend commands, paths, '
    'file names or programs to run, and never recommend deleting a protected group. You only advise: the user '
    'decides and nothing is selected for them.\n'
    '\n'
    'Reply with one JSON object that matches the schema and nothing else: no prose, no code fence. '
    '{"summary": string, "recommendations": [{"group": token, "verdict": "likely_safe" | "review" | "keep", '
    '"reason": string}]}. '
    f'summary: at most {MAX_SUMMARY} characters. At most {MAX_RECOMMENDATIONS} recommendations, most valuable '
    f'first, each token at most once, tokens copied exactly from the input. reason: at most {MAX_REASON} '
    'characters of plain text about category, size, age or growth.')


def schema_json() -> str:
    return json.dumps(REPLY_SCHEMA, sort_keys=True, separators=(',', ':'))


def user_message(payload_json: str) -> str:
    """What goes on stdin of a CLI that has a separate system-prompt channel."""
    return 'Disk usage groups (JSON):\n' + payload_json + '\n'


def combined_prompt(payload_json: str) -> str:
    """For a CLI without a system-prompt flag: instructions and input in one prompt."""
    return SYSTEM_PROMPT + '\n\n' + user_message(payload_json)


def manual_prompt(payload_json: str) -> str:
    """Text to paste into any chat: instructions, the exact payload and the schema the reply must match."""
    return (SYSTEM_PROMPT + '\n\n--- INPUT ---\n' + payload_json + '\n\n--- REPLY SCHEMA (JSON Schema) ---\n'
            + json.dumps(REPLY_SCHEMA, indent=2, sort_keys=True) + '\n\n'
            'Reply with the JSON object only. One ```json block is accepted; any other text is rejected.\n')
