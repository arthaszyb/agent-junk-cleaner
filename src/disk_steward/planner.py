"""Plans: a frozen copy of what the user selected, checked again at the moment it is created."""
from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any, Dict, List, Mapping, Optional

from . import queries
from .catalog import Catalog, load_catalog
from .macos import MacOS
from .model import Action, DecisionState, Entry, ErrorCode, Plan, PlanState, Spec, StewardError, Unit
from .selection import Resolution, _action_totals, resolve
from .settings import Settings, settings_hash
from .store import Store

PLAN_TTL_SECONDS = 24 * 3600
DIFF_LIMIT = 50   # entries listed per side of a DIGEST_MISMATCH
TOTALS_FIELDS = ('count', 'alloc', 'reclaim', 'by_action', 'warnings')


def compute_entries_digest(entries: List[Entry]) -> str:
    canonical = json.dumps([[e.seq, e.item_key, e.action.value, dict(e.fp)] for e in entries],
                           sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _stale(from_scan: Any, to_scan: Any, notes: Optional[Mapping[str, Any]]) -> StewardError:
    return StewardError(ErrorCode.INVALID, 'error.spec_stale', from_scan=from_scan, to_scan=to_scan,
                        notes=None if notes is None else dict(notes))


def create_plan(store: Store, spec: Spec, now: float, expected: Mapping[str, Any], settings: Settings, mac: MacOS,
                *, catalog: Optional[Catalog] = None, notes: Optional[Mapping[str, Any]] = None) -> Plan:
    """Re-resolve `spec` at `now` and save the result as a draft plan.

    `expected` is what the client last saw: {'spec_version', 'digest', 'scan_id'}, optionally 'resolved_at' (the
    `now` of that resolution). Refusals, in order:
    - INVALID error.spec_stale: the spec belongs to an older scan than the latest, or the client looked at an
      older scan than the spec now targets (it was migrated). `notes` (the migration notes the caller kept)
      ride along in the details.
    - RULES_CHANGED: the catalog or the classification settings differ from what the scan used. `catalog` saves
      loading it again. Facts are checked when the plan runs.
    - DIGEST_MISMATCH: the selection differs. Listing what changed needs 'resolved_at' to rebuild the earlier
      selection; without it the lists are empty and the counts None.
    - EMPTY_PLAN: nothing is selected."""
    # One database view for the resolution and the rows: a scan that finishes meanwhile prunes the previous scan's
    # rows, and a resolution of one state next to rows of another would not add up.
    with store.read_snapshot():
        scan = store.scan(spec.scan_id)
        latest = store.latest_scan()
        if latest is None or latest['id'] != spec.scan_id:
            raise _stale(spec.scan_id, None if latest is None else latest['id'], notes)
        if not isinstance(expected, Mapping) or not isinstance(expected.get('digest'), str) \
                or any(isinstance(expected.get(k), bool) or not isinstance(expected.get(k), int)
                       for k in ('spec_version', 'scan_id')):
            raise StewardError(ErrorCode.INVALID, 'error.invalid_op', field='expected')
        if expected['scan_id'] != spec.scan_id:
            raise _stale(expected['scan_id'], spec.scan_id, notes)
        current = {'catalog_hash': (catalog or load_catalog(settings)).hash, 'settings_hash': settings_hash(settings)}
        which = [name for name, value in current.items() if scan[name] != value]
        if which:
            raise StewardError(ErrorCode.RULES_CHANGED, 'error.RULES_CHANGED', which=which)
        res = resolve(store, spec, now, settings, mac)
        if res.digest != expected['digest'] or spec.version != expected['spec_version']:
            raise _mismatch(store, spec, res, expected, settings, mac)
        if res.summary['count'] == 0:
            raise StewardError(ErrorCode.EMPTY_PLAN, 'error.EMPTY_PLAN', breakdown=res.summary['scope_by_state'],
                               total_breakdown=res.summary['by_state'])
        plan_id = secrets.token_hex(12)
        decisions = res.decisions
        picked = [r for r in queries.plan_item_rows(store, spec.scan_id)
                  if decisions[r[0]].state == DecisionState.SELECTED]
        consistent = len(picked) == res.summary['count']
    if not consistent:   # unreachable inside a snapshot; if it ever happens the selection no longer fits its scan
        newer = store.latest_scan()
        raise _stale(spec.scan_id, None if newer is None else newer['id'], notes)
    # Biggest first within a group, so a plan that is paused halfway has done the most useful part.
    picked.sort(key=lambda r: (r[4], -r[5], r[0]))
    rows = []
    pieces = []
    bundle = Unit.BUNDLE.value
    # The fingerprint JSON is written out by hand (keys sorted, no spaces) because building 10^5 dicts and dumping
    # them costs seconds. It must equal json.dumps(Item.fingerprint(), sort_keys=True, separators=(',', ':')).
    for seq, (key, path, unit, rule_id, group_key, alloc, reclaim, dev, ino, size, mtime, birth, nlink, files,
              newest) in enumerate(picked, 1):
        d = decisions[key]
        action = d.action.value  # type: ignore[union-attr]
        if unit == bundle:
            fp = f'{{"alloc":{alloc},"dev":{dev},"files":{files},"ino":{ino},"newest_ns":{newest}}}'
        else:
            fp = f'{{"birth_ns":{birth},"dev":{dev},"ino":{ino},"mtime_ns":{mtime},"nlink":{nlink},"size":{size}}}'
        rows.append((plan_id, seq, key, path, unit, action, rule_id, group_key, d.threshold_days, alloc, reclaim, fp,
                     'pending', '', 0, None, now))
        quoted = f'"{key}"' if key.isascii() and key.isalnum() else json.dumps(key)
        pieces.append(f'[{seq},{quoted},"{action}",{fp}]')
    digest = hashlib.sha256(('[' + ','.join(pieces) + ']').encode('utf-8')).hexdigest()
    plan = Plan(id=plan_id, created_at=now, expires_at=now + PLAN_TTL_SECONDS, scan_id=spec.scan_id,
                catalog_hash=scan['catalog_hash'], facts_hash=scan['facts_hash'], settings_hash=scan['settings_hash'],
                spec=spec.to_json(), revision=1, entries_digest=digest, state=PlanState.DRAFT,
                totals={k: res.summary[k] for k in TOTALS_FIELDS})
    queries.insert_plan(store, plan, rows)
    return plan


def create_manual_plan(store: Store, scan_id: int, item_key: str, action: Action, now: float, settings: Settings,
                       mac: MacOS, *, catalog: Optional[Catalog] = None) -> Plan:
    """A draft plan of one item of a report-only group, with one of its rule's manual_actions: the Move to Trash of
    the tools table. It is checked like any plan when it runs. Refusals: INVALID error.spec_stale (not the latest
    scan), RULES_CHANGED, NOT_FOUND (no such item), INVALID error.manual_not_allowed (the rule does not offer the
    action for one folder), UNAVAILABLE error.trash_unavailable."""
    cat = catalog or load_catalog(settings)
    with store.read_snapshot():
        scan = store.scan(scan_id)
        latest = store.latest_scan()
        if latest is None or latest['id'] != scan_id:
            raise _stale(scan_id, None if latest is None else latest['id'], None)
        current = {'catalog_hash': cat.hash, 'settings_hash': settings_hash(settings)}
        which = [name for name, value in current.items() if scan[name] != value]
        if which:
            raise StewardError(ErrorCode.RULES_CHANGED, 'error.RULES_CHANGED', which=which)
        row = next((r for r in queries.plan_item_rows(store, scan_id) if r[0] == item_key), None)
    if row is None:
        raise StewardError(ErrorCode.NOT_FOUND, 'error.NOT_FOUND', what='item', id=item_key)
    key, path, unit, rule_id, group_key, alloc, reclaim, dev, ino, size, mtime, birth, nlink, files, newest = row
    rule = cat.rule(rule_id)
    if action not in rule.manual_actions:
        raise StewardError(ErrorCode.INVALID, 'error.manual_not_allowed', action=action.value, rule_id=rule_id)
    if action == Action.TRASH and not mac.trash_available():
        raise StewardError(ErrorCode.UNAVAILABLE, 'error.trash_unavailable')
    # The same fingerprint JSON as create_plan writes, and the digest over the one entry.
    if unit == Unit.BUNDLE.value:
        fp = f'{{"alloc":{alloc},"dev":{dev},"files":{files},"ino":{ino},"newest_ns":{newest}}}'
    else:
        fp = f'{{"birth_ns":{birth},"dev":{dev},"ino":{ino},"mtime_ns":{mtime},"nlink":{nlink},"size":{size}}}'
    plan_id = secrets.token_hex(12)
    threshold = rule.min_age_floor_days
    rows = [(plan_id, 1, key, path, unit, action.value, rule_id, group_key, threshold, alloc, reclaim, fp,
             'pending', '', 0, None, now)]
    digest = hashlib.sha256(f'[[1,{json.dumps(key)},"{action.value}",{fp}]]'.encode('utf-8')).hexdigest()
    spec = Spec(scan_id=scan_id, include={key: action})
    totals = {'count': 1, 'alloc': alloc, 'reclaim': reclaim,
              'by_action': {action.value: _action_totals(action, [1, alloc, reclaim])}, 'warnings': []}
    plan = Plan(id=plan_id, created_at=now, expires_at=now + PLAN_TTL_SECONDS, scan_id=scan_id,
                catalog_hash=scan['catalog_hash'], facts_hash=scan['facts_hash'], settings_hash=scan['settings_hash'],
                spec=spec.to_json(), revision=1, entries_digest=digest, state=PlanState.DRAFT, totals=totals)
    queries.insert_plan(store, plan, rows)
    return plan


def _mismatch(store: Store, spec: Spec, res: Resolution, expected: Mapping[str, Any], settings: Settings,
              mac: MacOS) -> StewardError:
    earlier = _earlier_selection(store, spec, res.now, expected, settings, mac)
    added: List[Dict[str, Any]] = []
    removed: List[Dict[str, Any]] = []
    counts: Dict[str, Optional[int]] = {'added_count': None, 'removed_count': None}
    if earlier is not None:
        now_sel = res.selected_actions()
        new_keys = sorted(k for k, a in now_sel.items() if earlier.get(k) != a)
        gone_keys = sorted(k for k, a in earlier.items() if now_sel.get(k) != a)
        counts = {'added_count': len(new_keys), 'removed_count': len(gone_keys)}
        added = _describe(store, spec.scan_id, new_keys, now_sel)
        removed = _describe(store, spec.scan_id, gone_keys, earlier)
    return StewardError(ErrorCode.DIGEST_MISMATCH, 'error.DIGEST_MISMATCH', added=added, removed=removed,
                        spec_version=spec.version, **counts)


def _earlier_selection(store: Store, spec: Spec, now: float, expected: Mapping[str, Any], settings: Settings,
                       mac: MacOS) -> Optional[Dict[str, Action]]:
    """The selection the client saw, rebuilt by resolving the same spec at the time it was resolved.
    None when that cannot be reproduced (spec edited since, settings changed, no timestamp given)."""
    at = expected.get('resolved_at')
    if isinstance(at, bool) or not isinstance(at, (int, float)) or spec.version != expected['spec_version']:
        return None
    if at == now:
        return None   # the same resolution again; it cannot differ from the digest we just refused
    before = resolve(store, spec, at, settings, mac)
    return before.selected_actions() if before.digest == expected['digest'] else None


def _describe(store: Store, scan_id: int, keys: List[str], actions: Mapping[str, Action]) -> List[Dict[str, Any]]:
    return [{'path': path, 'action': actions[key].value, 'alloc': alloc}
            for key, path, alloc in queries.describe_rows(store, scan_id, keys, DIFF_LIMIT)]


def validate_for_run(plan: Plan, current_hashes: Mapping[str, str], now: float, revision: int,
                     entries_digest: str) -> None:
    """Raises PLAN_EXPIRED, RULES_CHANGED or REVISION_MISMATCH. `current_hashes` holds catalog_hash, facts_hash
    and settings_hash as they are now; `revision` and the digest are what the client believes the plan is."""
    if now >= plan.expires_at:
        raise StewardError(ErrorCode.PLAN_EXPIRED, 'error.PLAN_EXPIRED', expired_at=plan.expires_at)
    # Facts (current CLI version, installed apps, newest editor cache, ...) drift while apps run and a plan's own
    # removals change them, so they never block a plan as a whole. The executor re-classifies every entry with
    # fresh facts and skips any whose verdict changed (skip.rules_changed); preflight reports those up front.
    changed = [name for name in ('catalog_hash', 'settings_hash') if current_hashes[name] != getattr(plan, name)]
    if changed:
        raise StewardError(ErrorCode.RULES_CHANGED, 'error.RULES_CHANGED', changed=changed)
    which = [name for name, ok in (('revision', revision == plan.revision),
                                   ('entries_digest', entries_digest == plan.entries_digest)) if not ok]
    if which:
        raise StewardError(ErrorCode.REVISION_MISMATCH, 'error.REVISION_MISMATCH', which=which,
                           current=plan.revision, given=revision)
