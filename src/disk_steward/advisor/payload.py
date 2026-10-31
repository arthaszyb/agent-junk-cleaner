"""The anonymised payload: numbers and catalog vocabulary only.

Nothing here may carry a path, file or folder name, bundle id, project name, label_args value, evidence argument,
user name or file content. Group rows are built from an allowlist of fields; the token map that turns a reply back
into group keys never leaves this process."""
from __future__ import annotations

import bisect
import json
import secrets
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from .. import i18n, queries
from ..classify import is_protected_variant
from ..model import AgeBasis, Group, Tier
from ..report import _previous_done
from ..store import Store

FORMAT_VERSION = 1
MAX_BYTES = 48 * 1024
DAY_SECONDS = 86400.0
# Upper bounds in days of the first five buckets; the last one is open-ended.
AGE_EDGES = (7, 14, 30, 90, 180)
AGE_NAMES = ('lt7', 'd7_14', 'd14_30', 'd30_90', 'd90_180', 'gt180')
_SLACK = 64    # the coverage numbers grow by a few digits after the base size was measured


@dataclass(frozen=True)
class Built:
    scan_id: int
    payload: Dict[str, Any]
    json: str                                   # compact; exactly what is sent
    tokens: Dict[str, Dict[str, Any]]           # token -> {key, rule_id, label_args, protected}; stays in memory
    coverage: Dict[str, int]


def dumps(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _age_buckets(store: Store, scan_id: int, groups: List[Group], now: float) -> Dict[str, List[List[int]]]:
    """Per group key: [[items, alloc]] for each AGE_NAMES bucket, aged by the group's own age basis."""
    by_key = {g.key: g for g in groups}
    out: Dict[str, List[List[int]]] = {g.key: [[0, 0] for _ in AGE_NAMES] for g in groups}
    for _key, group_key, _tier, _domain, _unit, _nlink, newest_ns, last_used_ns, _taint, alloc, _reclaim \
            in queries.item_rows(store, scan_id):
        ns = newest_ns
        if by_key[group_key].age_basis == AgeBasis.LAST_USED and last_used_ns is not None and last_used_ns > ns:
            ns = last_used_ns
        bucket = out[group_key][bisect.bisect_right(AGE_EDGES, (now - ns / 1e9) / DAY_SECONDS)]
        bucket[0] += 1
        bucket[1] += alloc
    return out


def _label_key(group: Group, en: Mapping[str, str]) -> str:
    """The catalog key of the group's label, never the rendered text (it may hold the group's path)."""
    own = f'group.{group.key}.label'
    return own if own in en else f'rule.{group.rule_id}.label'


def _growth(store: Store, group: Group, previous: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if previous is None:
        return None
    before = next((alloc for sid, _fin, alloc, _n in store.group_history(group.key) if sid == previous['id']), None)
    return {'delta_alloc': group.alloc - (before or 0), 'new': before is None}


def _protected(group: Group) -> bool:
    return group.tier == Tier.PROTECTED or is_protected_variant(group.key)


def _row(store: Store, group: Group, token: str, ages: List[List[int]], previous: Optional[Mapping[str, Any]],
         en: Mapping[str, str]) -> Dict[str, Any]:
    return {'token': token, 'domain': group.domain.value, 'tier': group.tier.value, 'kind': group.kind.value,
            'provider': group.provider, 'label_key': _label_key(group, en),
            'actions': [a.value for a in group.actions], 'has_hint': bool(group.hint), 'items': group.items,
            'alloc': group.alloc, 'reclaim': group.reclaim,
            'ages': dict(zip(AGE_NAMES, ages)),
            'growth': _growth(store, group, previous),
            'evidence': sorted({e.key for e in group.evidence}), 'protected': _protected(group)}


def _totals(groups: List[Group]) -> Dict[str, Any]:
    def sums() -> Dict[str, int]:
        return {'alloc': 0, 'reclaim': 0, 'items': 0, 'groups': 0}
    by_tier: Dict[str, Dict[str, int]] = {t.value: sums() for t in Tier}
    by_domain: Dict[str, Dict[str, int]] = {}
    for g in groups:
        for acc in (by_tier[g.tier.value], by_domain.setdefault(g.domain.value, sums())):
            acc['alloc'] += g.alloc
            acc['reclaim'] += g.reclaim
            acc['items'] += g.items
            acc['groups'] += 1
    return {'by_tier': by_tier, 'by_domain': dict(sorted(by_domain.items()))}


def _new_token(taken: Mapping[str, Any]) -> str:
    while True:
        token = 'g' + secrets.token_hex(4)
        if token not in taken:
            return token


def build_payload(store: Store, scan_id: int, now: float) -> Built:
    """Groups in descending alloc until MAX_BYTES; `coverage` says how much of the scan they stand for."""
    scan = store.scan(scan_id)
    groups = sorted(store.groups(scan_id), key=lambda g: (-g.alloc, g.key))
    previous = _previous_done(store, scan_id)
    en = i18n.catalog('en')
    ages = _age_buckets(store, scan_id, groups, now)
    alloc_total = sum(g.alloc for g in groups)
    used = None if scan['vol_total'] is None or scan['vol_free'] is None else scan['vol_total'] - scan['vol_free']

    def frame(rows: List[Dict[str, Any]], coverage: Mapping[str, int]) -> Dict[str, Any]:
        return {'format': FORMAT_VERSION, 'coverage': dict(coverage),
                'scan': {'domains': scan['domains'], 'totals': _totals(groups),
                         'volume': {'total': scan['vol_total'], 'used': used, 'free': scan['vol_free']},
                         'previous_scan_age_days': None if previous is None
                         else round((now - previous['finished_at']) / DAY_SECONDS, 2)},
                'groups': rows}

    worst = {'groups_included': len(groups), 'groups_total': len(groups), 'alloc_included': alloc_total,
             'alloc_total': alloc_total}
    used_bytes = len(dumps(frame([], worst))) + _SLACK
    rows: List[Dict[str, Any]] = []
    tokens: Dict[str, Dict[str, Any]] = {}
    alloc_included = 0
    for g in groups:
        token = _new_token(tokens)
        row = _row(store, g, token, ages[g.key], previous, en)
        size = len(dumps(row)) + 1     # the comma between rows
        if used_bytes + size > MAX_BYTES:
            break
        used_bytes += size
        rows.append(row)
        alloc_included += g.alloc
        tokens[token] = {'key': g.key, 'rule_id': g.rule_id, 'label_args': dict(g.label_args),
                         'protected': _protected(g)}
    coverage = {'groups_included': len(rows), 'groups_total': len(groups), 'alloc_included': alloc_included,
                'alloc_total': alloc_total}
    payload = frame(rows, coverage)
    text = dumps(payload)
    if len(text.encode('utf-8')) > MAX_BYTES:
        raise RuntimeError(f'advisor payload is {len(text)} bytes, over the {MAX_BYTES} cap')
    return Built(scan_id=scan_id, payload=payload, json=text, tokens=tokens, coverage=coverage)
