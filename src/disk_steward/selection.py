"""Selection: decide() is the one function that says whether an item may be removed, resolve() applies it to a
whole scan, apply_ops() edits the user's selection. The planner, the UI counts and the report all go
through decide(), so what the user sees is what a plan contains."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Set, Tuple

from . import queries
from .macos import MacOS
from .model import (Action, AgeBasis, Decision, DecisionState, Domain, ErrorCode, Group, GroupSel, Rule, Spec, StewardError,
                    Tier, Unit)
from .settings import Settings
from .store import Store

DAY_SECONDS = 86400.0
# Share of an archived item's reclaimable bytes that is assumed to come back. The real figure depends on
# how well the data compresses; the run report shows the measured value.
ARCHIVE_FREES_RATIO = 0.7
CODEX_SESSION_RULES = ('codex.sessions', 'codex.archived-sessions')
_CODEX_MARKERS = ('evidence.codex_legacy_copy', 'evidence.codex_duplicate_kept')

_S = DecisionState
_PROTECTED = Decision(_S.PROTECTED, 'reason.protected')
_TAINTED = Decision(_S.PROTECTED, 'reason.tainted')
_DOMAIN_OFF = Decision(_S.DOMAIN_OFF, 'reason.domain_off')
_NO_GAIN = Decision(_S.NO_GAIN, 'reason.no_gain')
_EXCLUDED = Decision(_S.EXCLUDED, 'reason.excluded')
_UNSELECTED = Decision(_S.UNSELECTED, 'reason.unselected')
_unsupported_cache: Dict[Tuple[bool, Optional[Action]], Decision] = {}


def _unsupported(action: Optional[Action], report_only: bool) -> Decision:
    found = _unsupported_cache.get((report_only, action))
    if found is None:
        found = _unsupported_cache[(report_only, action)] = Decision(
            _S.UNSUPPORTED_ACTION, 'reason.report_only' if report_only else 'reason.unsupported_action',
            {'action': None if action is None else action.value})
    return found


def decide(item: Any, group: Group, rule: Optional[Rule], spec: Spec, now: float, enabled_domains: Iterable[str],
           trash_available: bool) -> Decision:
    """First matching step wins (docs/architecture.md, Selection).

    `item` needs only key, group_key, tier, domain, unit, nlink, newest_ns, last_used_ns and taint, so resolve()
    can pass light rows. `rule` supplies actions, default_action, min_age_floor_days and age_basis; when it is None
    the group's copies of those are used (the scan stores them, and resolve() has no catalog)."""
    if item.tier == Tier.PROTECTED or item.taint:
        return _TAINTED if item.taint else _PROTECTED
    if item.domain not in enabled_domains:
        return _DOMAIN_OFF
    if item.unit == Unit.FILE and item.nlink > 1:
        return _NO_GAIN
    key = item.key
    if key in spec.exclude:
        return _EXCLUDED
    sel = spec.groups.get(item.group_key)
    explicit = key in spec.include
    if sel is None and not explicit:
        return _UNSELECTED
    src = group if rule is None else rule
    action = ((spec.include.get(key) if explicit else None)
              or (sel.action if sel is not None else None) or src.default_action)
    if action is None or action not in src.actions or (action == Action.TRASH and not trash_available):
        return _unsupported(action, not src.actions)
    ns = item.newest_ns
    if src.age_basis == AgeBasis.LAST_USED and item.last_used_ns is not None and item.last_used_ns > ns:
        ns = item.last_used_ns
    age = (now - ns / 1e9) / DAY_SECONDS
    floor = src.min_age_floor_days
    if age < floor:
        return Decision(_S.TOO_RECENT_FLOOR, 'reason.too_recent_floor', {'age': age, 'floor': floor},
                        age_days=age, threshold_days=floor)
    if explicit:
        # "Include anyway": the user picked this item, so the age filter yields, but never the rule's floor.
        return Decision(_S.SELECTED, 'reason.selected_explicit', action=action, via='explicit', age_days=age,
                        threshold_days=floor)
    # A per-group override is the user's explicit word. Otherwise a rule default (e.g. idle files 180d) may only
    # make the global filter stricter, never looser than what the user set.
    if sel.min_age_days is not None:
        wanted = sel.min_age_days
    else:
        wanted = max(group.default_min_age_days or 0.0, spec.filter_min_age_days)
    threshold = max(floor, wanted)
    if age < threshold:
        return Decision(_S.TOO_RECENT, 'reason.too_recent', {'age': age, 'threshold': threshold}, age_days=age,
                        threshold_days=threshold)
    return Decision(_S.SELECTED, 'reason.selected_group', action=action, via='group', age_days=age,
                    threshold_days=threshold)


# resolve ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Resolution:
    decisions: Dict[str, Decision]   # every item of the scan
    summary: Dict[str, Any]
    digest: str
    now: float                       # the clock the decisions were made at

    def selected_actions(self) -> Dict[str, Action]:
        return {k: d.action for k, d in self.decisions.items() if d.state == DecisionState.SELECTED}  # type: ignore[misc]


class _Row(NamedTuple):
    key: str
    group_key: str
    tier: Tier
    domain: Domain
    unit: Unit
    nlink: int
    newest_ns: int
    last_used_ns: Optional[int]
    taint: str
    alloc: int
    reclaim: int


_TIERS = {t.value: t for t in Tier}
_DOMAINS = {d.value: d for d in Domain}
_UNITS = {u.value: u for u in Unit}
# per-group counters: items, selected, selected alloc, selected reclaim, too recent (filter), too recent alloc,
# held by the rule floor, held alloc, excluded, protected
(_T, _SEL, _SEL_ALLOC, _SEL_RECLAIM, _RECENT, _RECENT_ALLOC, _FLOOR, _FLOOR_ALLOC, _EXCL, _PROT) = range(10)


def resolve(store: Store, spec: Spec, now: float, settings: Settings, mac: MacOS) -> Resolution:
    return resolve_for(store, spec, now, settings.domains, mac.trash_available())


def resolve_for(store: Store, spec: Spec, now: float, enabled_domains: Iterable[str],
                trash_available: bool) -> Resolution:
    """resolve() with the two facts it takes from settings and the Mac passed in directly."""
    store.scan(spec.scan_id)  # NOT_FOUND for an unknown scan
    enabled = frozenset(enabled_domains)
    groups = {g.key: g for g in store.groups(spec.scan_id)}
    decisions: Dict[str, Decision] = {}
    by_state: Dict[DecisionState, List[int]] = {}
    by_action: Dict[Action, List[int]] = {}
    in_scope: Dict[DecisionState, List[int]] = {}   # by_state restricted to selected groups and explicit includes
    per_group: Dict[str, List[int]] = {k: [0] * 10 for k in groups}
    chosen_groups, included = spec.groups, spec.include
    selected: List[Tuple[str, str]] = []
    for r in queries.item_rows(store, spec.scan_id):
        item = _Row(r[0], r[1], _TIERS[r[2]], _DOMAINS[r[3]], _UNITS[r[4]], r[5], r[6], r[7], r[8], r[9], r[10])
        d = decide(item, groups[item.group_key], None, spec, now, enabled, trash_available)
        decisions[item.key] = d
        state = d.state
        acc = by_state.get(state)
        if acc is None:
            acc = by_state[state] = [0, 0]
        acc[0] += 1
        acc[1] += item.alloc
        if item.group_key in chosen_groups or item.key in included:
            acc = in_scope.get(state)
            if acc is None:
                acc = in_scope[state] = [0, 0]
            acc[0] += 1
            acc[1] += item.alloc
        g = per_group[item.group_key]
        g[_T] += 1
        if state == _S.SELECTED:
            g[_SEL] += 1
            g[_SEL_ALLOC] += item.alloc
            g[_SEL_RECLAIM] += item.reclaim
            selected.append((item.key, d.action.value))  # type: ignore[union-attr]
            a = by_action.get(d.action)  # type: ignore[arg-type]
            if a is None:
                a = by_action[d.action] = [0, 0, 0]  # type: ignore[index]
            a[0] += 1
            a[1] += item.alloc
            a[2] += item.reclaim
        elif state == _S.TOO_RECENT:
            g[_RECENT] += 1
            g[_RECENT_ALLOC] += item.alloc
        elif state == _S.TOO_RECENT_FLOOR:
            g[_FLOOR] += 1
            g[_FLOOR_ALLOC] += item.alloc
        elif state == _S.EXCLUDED:
            g[_EXCL] += 1
        elif state == _S.PROTECTED:
            g[_PROT] += 1
    selected.sort()
    summary: Dict[str, Any] = {
        'count': len(selected),
        'alloc': sum(a[1] for a in by_action.values()),
        'reclaim': sum(a[2] for a in by_action.values()),
        'total': len(decisions),
        'by_action': {a.value: _action_totals(a, v) for a, v in sorted(by_action.items(), key=lambda kv: kv[0].value)},
        'by_state': {s.value: {'count': by_state[s][0], 'alloc': by_state[s][1]} for s in _S if s in by_state},
        'scope_by_state': {s.value: {'count': in_scope[s][0], 'alloc': in_scope[s][1]} for s in _S if s in in_scope},
        'groups': {k: {'total': v[_T], 'selected': v[_SEL], 'alloc': v[_SEL_ALLOC], 'reclaim': v[_SEL_RECLAIM],
                       'too_recent': v[_RECENT], 'too_recent_alloc': v[_RECENT_ALLOC], 'floor_held': v[_FLOOR],
                       'floor_held_alloc': v[_FLOOR_ALLOC], 'excluded': v[_EXCL], 'protected': v[_PROT]}
                   for k, v in sorted(per_group.items())},
    }
    summary['warnings'] = _warnings(store, spec, groups, decisions, summary['groups'])
    canonical = json.dumps([spec.scan_id, spec.version, [list(p) for p in selected]], separators=(',', ':'))
    return Resolution(decisions, summary, hashlib.sha256(canonical.encode('utf-8')).hexdigest(), now)


def _action_totals(action: Action, v: List[int]) -> Dict[str, Any]:
    out: Dict[str, Any] = {'count': v[0], 'alloc': v[1], 'reclaim': v[2]}
    if action == Action.DELETE:
        out['frees'] = v[2]
    elif action == Action.TRASH:
        out['frees'] = 0       # space comes back only when the Trash is emptied
        out['moves'] = v[2]
    else:
        out['frees'] = out['frees_estimate'] = int(round(v[2] * ARCHIVE_FREES_RATIO))
        out['estimated'] = True
    return out


def _warnings(store: Store, spec: Spec, groups: Mapping[str, Group], decisions: Mapping[str, Decision],
              group_summary: Mapping[str, Mapping[str, int]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    both = _codex_both_copies(store, spec.scan_id, groups, decisions)
    if both:
        out.append({'key': 'warning.codex_both_copies', 'count': both})
    for key in sorted(spec.groups):
        if key in group_summary and group_summary[key]['selected'] == 0:
            out.append({'key': 'warning.group_selected_none_qualify', 'count': group_summary[key]['total'],
                        'group': key})
    return out


def _codex_both_copies(store: Store, scan_id: int, groups: Mapping[str, Group],
                       decisions: Mapping[str, Decision]) -> int:
    """Pairs of Codex session files (legacy-ID copy and the copy that stays) that are selected together:
    removing both deletes the session. Partners are matched by file name, as the evidence records only that."""
    keys = [k for k, g in groups.items() if g.rule_id.startswith(CODEX_SESSION_RULES)]
    by_name: Dict[str, List[str]] = {}
    partners: List[Tuple[str, str]] = []   # (item key, partner file name)
    for key, path, evidence in queries.evidence_rows(store, scan_id, keys):
        by_name.setdefault(path.rpartition('/')[2], []).append(key)
        if any(m in evidence for m in _CODEX_MARKERS):
            for ev in json.loads(evidence):
                if ev['key'] in _CODEX_MARKERS:
                    partners.append((key, ev['args']['partner']))

    def chosen(k: str) -> bool:
        return decisions[k].state == _S.SELECTED

    pairs: Set[frozenset] = set()
    for key, partner in partners:
        if not chosen(key):
            continue
        for other in by_name.get(partner, ()):
            if other != key and chosen(other):
                pairs.add(frozenset((key, other)))
                break
    return len(pairs)


# apply_ops -------------------------------------------------------------------------------------------------

def _invalid(field: str) -> StewardError:
    return StewardError(ErrorCode.INVALID, 'error.invalid_op', field=field)


def _check_keys(op: Mapping[str, Any], required: Sequence[str], optional: Sequence[str] = ()) -> None:
    for name in required:
        if name not in op:
            raise _invalid(name)
    for name in op:
        if name != 'op' and name not in required and name not in optional:
            raise _invalid(name)


def _key(op: Mapping[str, Any], name: str) -> str:
    v = op[name]
    if not isinstance(v, str):
        raise _invalid(name)
    return v


def _days(op: Mapping[str, Any], name: str, optional: bool = False) -> Optional[float]:
    v = op.get(name)
    if v is None and optional:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        raise _invalid(name)
    return v


def _flag(op: Mapping[str, Any], name: str) -> bool:
    v = op[name]
    if not isinstance(v, bool):
        raise _invalid(name)
    return v


def _action(op: Mapping[str, Any], name: str = 'action', optional: bool = True) -> Optional[Action]:
    v = op.get(name)
    if v is None:
        if optional:
            return None
        raise _invalid(name)
    try:
        return Action(v)
    except ValueError:
        raise _invalid(name) from None


def _selectable(item: Any, group: Group, enabled: Set[str]) -> None:
    """Refuse to include what decide() could never select. The reason codes match the `reason.*` texts."""
    reason = None
    if item.tier == Tier.PROTECTED:
        reason = 'protected'
    elif item.taint:
        reason = 'tainted'
    elif item.domain.value not in enabled:
        reason = 'domain_off'
    elif not group.actions:
        reason = 'report_only'
    if reason is not None:
        raise StewardError(ErrorCode.INVALID, 'error.not_selectable', reason=reason, path=item.path, item=item.key)


def _allowed(group: Group, action: Optional[Action]) -> None:
    if action is not None and action not in group.actions:
        raise StewardError(ErrorCode.INVALID, 'error.action_not_allowed', action=action.value, target=group.key)


def apply_ops(spec: Spec, ops: Sequence[Mapping[str, Any]], store: Store,
              enabled_domains: Optional[Iterable[str]] = None) -> Spec:
    """A new Spec with `ops` applied in order and the version bumped once. Nothing is stored; a failing op leaves
    the caller's spec untouched. `enabled_domains` (default: the domains the scan covered) limits select_tier."""
    if isinstance(ops, (str, bytes)) or not isinstance(ops, (list, tuple)):
        raise _invalid('ops')
    scan = store.scan(spec.scan_id)
    enabled = set(scan['domains'] if enabled_domains is None else enabled_domains)
    groups = dict(spec.groups)
    include = dict(spec.include)
    exclude = set(spec.exclude)
    filter_days = spec.filter_min_age_days
    for op in ops:
        if not isinstance(op, Mapping):
            raise _invalid('op')
        name = op.get('op')
        if name == 'set_filter':
            _check_keys(op, ('min_age_days',))
            filter_days = _days(op, 'min_age_days')  # type: ignore[assignment]
        elif name == 'select_group':
            _check_keys(op, ('group', 'on'), ('action', 'min_age_days'))
            group = store.group(spec.scan_id, _key(op, 'group'))
            if _flag(op, 'on'):
                action = _action(op)
                _allowed(group, action)
                groups[group.key] = GroupSel(_days(op, 'min_age_days', optional=True), action)
            else:
                groups.pop(group.key, None)
        elif name == 'select_tier':
            _check_keys(op, ('tier', 'on'), ('domain',))
            if op['tier'] != Tier.JUNK.value:
                raise _invalid('tier')
            domain = op.get('domain')
            if domain is not None and (not isinstance(domain, str) or domain not in _DOMAINS):
                raise _invalid('domain')
            on = _flag(op, 'on')
            for g in store.groups(spec.scan_id, domain=domain, tier=Tier.JUNK):
                if not on:
                    groups.pop(g.key, None)
                elif g.domain.value in enabled and g.actions:
                    groups.setdefault(g.key, GroupSel())  # keeps an earlier per-group override
        elif name == 'toggle_item':
            _check_keys(op, ('item', 'on'), ('action',))
            item = store.item(spec.scan_id, _key(op, 'item'))
            group = store.group(spec.scan_id, item.group_key)
            action = _action(op)
            on = _flag(op, 'on')
            if on:
                _selectable(item, group, enabled)
            _allowed(group, action)
            if on:
                # Undoing an exclusion hands the item back to its group's filter; any other "on" is an explicit
                # include, which is how a too-recent item is included anyway.
                if item.key in exclude and group.key in groups and action is None:
                    exclude.discard(item.key)
                else:
                    exclude.discard(item.key)
                    include[item.key] = action if action is not None else include.get(item.key)
            else:
                include.pop(item.key, None)
                if group.key in groups:
                    exclude.add(item.key)
        elif name == 'set_action':
            _check_keys(op, ('action',), ('group', 'item'))
            if ('group' in op) == ('item' in op):
                raise _invalid('group')
            action = _action(op, optional=False)
            if 'group' in op:
                group = store.group(spec.scan_id, _key(op, 'group'))
                _allowed(group, action)
                if group.key not in groups:
                    raise StewardError(ErrorCode.INVALID, 'error.not_selected', target=group.key)
                groups[group.key] = GroupSel(groups[group.key].min_age_days, action)
            else:
                item = store.item(spec.scan_id, _key(op, 'item'))
                _allowed(store.group(spec.scan_id, item.group_key), action)
                if item.key not in include:
                    raise StewardError(ErrorCode.INVALID, 'error.not_selected', target=item.key)
                include[item.key] = action
        elif name == 'clear':
            _check_keys(op, ())
            groups.clear()
            include.clear()
            exclude.clear()
        else:
            raise _invalid('op')
    return Spec(scan_id=spec.scan_id, version=spec.version + 1, filter_min_age_days=filter_days, groups=groups,
                include=include, exclude=frozenset(exclude))


NOTE_PATHS = 50   # paths listed per kind in the migration notes


def migrate_spec(store: Store, spec: Spec, scan_id: int, *, now: Optional[float] = None,
                 enabled_domains: Optional[Iterable[str]] = None,
                 trash_available: bool = False, session: str = 'default') -> Tuple[Spec, Dict[str, Any]]:
    """Carry a selection over from spec.scan_id to the newer `scan_id`. Returns (new spec, notes).

    - Group selectors stay when the group still exists.
    - Exclusions always stay: they can only narrow a selection, so a missing item never loosens it.
    - An explicit include stays only if the new scan's item has the fingerprint the item had when the user picked
      it (Store.save_spec records it per `session`). The user chose that file as it was; a replaced or rewritten
      file is a different decision, and so is an include nothing was recorded for.
    - notes: from_scan, to_scan, dropped_groups, include_changed and include_missing ({count, paths[:50]}),
      kept_excludes, and group_changes (selected and size per selected group before and after). group_changes
      resolves both scans at `now` (enabled_domains defaults to the new scan's domains); without `now` it stays
      empty. The old scan's rows are gone once its successor was pruned in, and its figures then read zero."""
    old_id = spec.scan_id
    present_groups = {g.key for g in store.groups(scan_id)}
    groups = {k: v for k, v in spec.groups.items() if k in present_groups}
    wanted = sorted(spec.include)
    new_items = {i.key: i for i in store.items(scan_id, keys=wanted)} if wanted else {}
    recorded = store.include_fingerprints(session, wanted) if wanted else {}
    include: Dict[str, Optional[Action]] = {}
    changed: List[str] = []
    missing: List[str] = []
    for key in wanted:
        new, was = new_items.get(key), recorded.get(key)
        if new is None:
            missing.append(key if was is None else was[0])
        elif was is not None and was[1] == new.fingerprint():
            include[key] = spec.include[key]
        else:
            changed.append(new.path)
    migrated = Spec(scan_id=scan_id, version=spec.version + 1, filter_min_age_days=spec.filter_min_age_days,
                    groups=groups, include=include, exclude=frozenset(spec.exclude))
    notes: Dict[str, Any] = {
        'from_scan': old_id, 'to_scan': scan_id,
        'dropped_groups': sorted(k for k in spec.groups if k not in present_groups),
        'include_changed': {'count': len(changed), 'paths': sorted(changed)[:NOTE_PATHS]},
        'include_missing': {'count': len(missing), 'paths': sorted(missing)[:NOTE_PATHS]},
        'kept_excludes': len(spec.exclude), 'group_changes': []}
    if now is not None and spec.groups:
        domains = store.scan(scan_id)['domains'] if enabled_domains is None else enabled_domains
        before = resolve_for(store, spec, now, domains, trash_available).summary['groups']
        after = resolve_for(store, migrated, now, domains, trash_available).summary['groups']
        for key in sorted(spec.groups):
            old, new = before.get(key), after.get(key)
            notes['group_changes'].append({
                'group': key, 'old_selected': old['selected'] if old else 0, 'new_selected': new['selected'] if new else 0,
                'old_alloc': old['alloc'] if old else 0, 'new_alloc': new['alloc'] if new else 0})
    return migrated, notes
