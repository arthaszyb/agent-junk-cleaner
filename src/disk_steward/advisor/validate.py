"""Strict validation of every advisor reply. Nothing from a reply is used before it passes here."""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Dict, List, Mapping

from ..model import ErrorCode, StewardError
from .prompts import MAX_REASON, MAX_RECOMMENDATIONS, MAX_SUMMARY, VERDICTS

_FENCE = re.compile(r'^```[ \t]*(?:json|JSON)?[ \t]*\r?\n(.*?)\r?\n[ \t]*```$', re.S)
_ALLOWED_CONTROL = ('\n', '\t')
_SHOW = 60   # characters of reply text echoed in an error message


def invalid(reason: str) -> StewardError:
    return StewardError(ErrorCode.INVALID, 'error.advisor_invalid_reply', reason=reason)


def _short(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= _SHOW else text[:_SHOW] + '...'


def _reject_constant(name: str) -> Any:
    raise ValueError(f'the constant {name} is not JSON')


def _no_duplicate_keys(pairs: List[Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f'duplicate key {key!r}')
        out[key] = value
    return out


def loads_strict(text: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as e:
        raise invalid(f'not valid JSON ({e})') from None


def parse_exact(text: str) -> Any:
    """A CLI's output file: the JSON object and nothing else (surrounding whitespace aside)."""
    return loads_strict(text.strip().lstrip('﻿'))


def parse_manual(text: str) -> Any:
    """A reply pasted from a chat: bare JSON, or one ```json fenced block with nothing around it."""
    text = text.strip().lstrip('﻿')
    fences = text.count('```')
    if fences:
        if fences != 2:
            raise invalid('the reply holds more than one code block')
        found = _FENCE.match(text)
        if found is None:
            raise invalid('text outside the code block')
        text = found.group(1)
    return loads_strict(text)


def _object(value: Any, where: str, keys: tuple) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise invalid(f'{where} is not an object')
    for key in keys:
        if key not in value:
            raise invalid(f'{where} lacks "{key}"')
    for key in value:
        if key not in keys:
            raise invalid(f'{where} has the unexpected key {_short(key)}')
    return value


def _text(value: Any, where: str, limit: int) -> str:
    if not isinstance(value, str):
        raise invalid(f'{where} is not a string')
    if len(value) > limit:
        raise invalid(f'{where} is longer than {limit} characters')
    # The text reaches terminals and web pages. Control characters (escape sequences) and invisible format
    # characters (bidi overrides, zero-width marks, the BOM, tag characters) can disguise what is shown.
    for ch in value:
        category = unicodedata.category(ch)
        if category == 'Cf' or (category == 'Cc' and ch not in _ALLOWED_CONTROL):
            raise invalid(f'{where} contains a control or invisible formatting character')
    return value


def validate_reply(obj: Any, tokens: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    """The reply as {summary, recommendations: [{group: token, verdict, reason}]}.

    `tokens` is this request's token map; a token it does not hold, a repeated token, or "likely_safe" on a
    protected group is refused."""
    top = _object(obj, 'the reply', ('summary', 'recommendations'))
    summary = _text(top['summary'], 'summary', MAX_SUMMARY)
    recs = top['recommendations']
    if not isinstance(recs, list):
        raise invalid('recommendations is not a list')
    if len(recs) > MAX_RECOMMENDATIONS:
        raise invalid(f'more than {MAX_RECOMMENDATIONS} recommendations')
    seen = set()
    out: List[Dict[str, str]] = []
    for i, rec in enumerate(recs):
        where = f'recommendations[{i}]'
        row = _object(rec, where, ('group', 'verdict', 'reason'))
        token = row['group']
        if not isinstance(token, str):
            raise invalid(f'{where}.group is not a string')
        if token not in tokens:
            raise invalid(f'{where}.group {_short(token)} is not a group of this request')
        if token in seen:
            raise invalid(f'{where}.group {_short(token)} appears twice')
        seen.add(token)
        verdict = row['verdict']
        if not isinstance(verdict, str) or verdict not in VERDICTS:
            raise invalid(f'{where}.verdict {_short(verdict)} is not one of {", ".join(VERDICTS)}')
        if verdict == 'likely_safe' and tokens[token]['protected']:
            raise invalid(f'{where} calls a protected group likely_safe')
        out.append({'group': token, 'verdict': verdict, 'reason': _text(row['reason'], f'{where}.reason', MAX_REASON)})
    return {'summary': summary, 'recommendations': out}


def claude_structured_output(stdout: str) -> Any:
    """The object inside Claude Code's `--output-format json` envelope. A failed call is a CLI failure, not an
    invalid reply."""
    envelope = loads_strict(stdout)
    if not isinstance(envelope, dict):
        raise invalid('the Claude output is not a JSON object')
    if envelope.get('is_error') or envelope.get('subtype', 'success') != 'success':
        detail = envelope.get('result')
        reason = detail if isinstance(detail, str) and detail.strip() else str(envelope.get('subtype', 'error'))
        raise StewardError(ErrorCode.UNAVAILABLE, 'error.advisor_cli_failed', provider='claude',
                           reason=' '.join(reason.split())[:300])
    if 'structured_output' not in envelope:
        raise invalid('the Claude output has no structured_output')
    return envelope['structured_output']
