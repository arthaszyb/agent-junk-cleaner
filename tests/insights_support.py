"""Helpers for the Insights tests: finished scans written straight into an App's store (no disk walk), with groups
that carry the real catalog's rules, so sizes can be chosen freely (a test cannot create a 500 MiB folder)."""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from disk_steward.classify import split_group_key
from disk_steward.model import AgeBasis, Domain, Evidence, Group, Item, Kind, Tier, Unit
from disk_steward.paths import item_key
from disk_steward.store import group_key_class

from m3_support import DAY, NOW

TOTAL = 500 * 10 ** 9
GIB = 1024 ** 3
MIB = 1024 ** 2

Spec = Union[int, Tuple[int, int]]   # alloc, or (alloc, number of items)


class Synth:
    """`scan()` writes one finished scan. The clock is moved to the scan's time and back to NOW afterwards."""

    def __init__(self, app: Any, clock: Any):
        self.app, self.clock = app, clock
        self.store = app.store
        self.catalog = app._load_catalog()

    def group(self, scan_id: int, key: str, alloc: int, items: int = 1, evidence: Sequence[Evidence] = ()) -> Group:
        rule_id, suffix = split_group_key(key)
        tier, domain, _report_only = group_key_class(key, self.catalog)
        label_args = {'path': suffix} if suffix else {}
        if rule_id == 'unknown':   # the scanner's own protected group for what no rule claims
            return Group(scan_id=scan_id, key=key, rule_id=rule_id, provider='system', domain=Domain(domain),
                         tier=Tier.PROTECTED, kind=Kind.UNKNOWN, unit=Unit.FILE, label_args=label_args, items=items,
                         alloc=alloc, reclaim=alloc, newest_ns=1, oldest_ns=1, actions=(), default_action=None,
                         min_age_floor_days=0.0, default_min_age_days=None, age_basis=AgeBasis.CHANGED,
                         evidence=tuple(evidence))
        rule = self.catalog.rule(rule_id)
        protected = tier == 'protected'
        return Group(scan_id=scan_id, key=key, rule_id=rule_id, provider=rule.provider, domain=Domain(domain),
                     tier=Tier(tier), kind=rule.kind, unit=rule.unit, label_args=label_args,
                     items=items, alloc=alloc, reclaim=alloc, newest_ns=1, oldest_ns=1,
                     actions=() if protected else rule.actions, default_action=None if protected else
                     rule.default_action, min_age_floor_days=rule.min_age_floor_days,
                     default_min_age_days=rule.default_min_age_days, age_basis=rule.age_basis, hint=rule.hint,
                     evidence=tuple(evidence))

    def items_of(self, group: Group, age_days: float) -> List[Item]:
        ns = int((NOW - age_days * DAY) * 1e9)
        each, extra = divmod(group.alloc, group.items)
        out = []
        for i in range(group.items):
            path = f'~/Library/Caches/seed/{group.key.replace("/", "_")}/{i}'
            alloc = each + (extra if i == 0 else 0)
            out.append(Item(scan_id=group.scan_id, key=item_key(path), path=path, rule_id=group.rule_id,
                            group_key=group.key, tier=group.tier, domain=group.domain, kind=group.kind,
                            unit=group.unit, dev=1, ino=i + 1, size=alloc, alloc=alloc, reclaim=alloc, files=1,
                            mtime_ns=ns, birth_ns=ns, newest_ns=ns, last_used_ns=None, nlink=1, uid=501, flags=0,
                            taint='', evidence=group.evidence))
        return out

    def scan(self, days_ago: float, groups: Mapping[str, Spec], free: int = 300 * 10 ** 9, total: int = TOTAL,
             age_days: float = 30.0, evidence: Optional[Mapping[str, Sequence[Evidence]]] = None,
             attributed: Optional[int] = None) -> int:
        self.clock.set(NOW - days_ago * DAY)
        try:
            sid = self.store.begin_scan(self.catalog.hash, 'f', 's', self.app.settings.domains)
            built = []
            for key, spec in groups.items():
                alloc, n = (spec, 1) if isinstance(spec, int) else spec
                built.append(self.group(sid, key, alloc, n, (evidence or {}).get(key, ())))
            self.store.add_groups(sid, built)
            self.store.add_items(sid, [i for g in built for i in self.items_of(g, age_days)])
            self.store.finish_scan(sid, 'done', total, free, sum(g.alloc for g in built) if attributed is None
                                   else attributed)
        finally:
            self.clock.set(NOW)
        return sid


def todo_ids(data: Mapping[str, Any]) -> List[str]:
    return [t['id'] for t in data['todos']]


def todo(data: Mapping[str, Any], todo_id: str) -> Dict[str, Any]:
    found = [t for t in data['todos'] if t['id'] == todo_id]
    assert len(found) == 1, f'{todo_id}: {todo_ids(data)}'
    return found[0]
