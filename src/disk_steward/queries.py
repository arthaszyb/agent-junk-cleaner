"""Read-only SQL that Store has no method for yet. Each helper is a candidate to move into Store."""
from __future__ import annotations

import dataclasses
import json
from typing import Any, Dict, Iterable, Iterator, List, Sequence, Tuple

from .model import Entry, Plan
from .store import BY_KEY, PATH_SQL, RUN_KEY, Store, _insert_sql, _params

# The columns resolve() needs for every item. Building full Item objects (23 columns, JSON evidence)
# costs about 9 us per row, which alone would exceed the time budget for 150k items.
ITEM_ROW_COLUMNS = ('key', 'group_key', 'tier', 'domain', 'unit', 'nlink', 'newest_ns', 'last_used_ns',
                    'taint', 'alloc', 'reclaim')

# An item row stores rule/tier/domain/kind/unit only when they differ from its group's i_* values.


def item_rows(store: Store, scan_id: int) -> Iterator[Tuple[Any, ...]]:
    """Plain tuples in ITEM_ROW_COLUMNS order, one per item of the scan."""
    cur = store._conn().cursor()
    cur.row_factory = None  # tuples instead of sqlite3.Row
    # NOT INDEXED: a walk over the table is cheaper than 10^5 random visits through the (scan_id, key) index.
    cur.execute('SELECT i.key, g.key, COALESCE(i.tier, g.i_tier), COALESCE(i.domain, g.i_domain), '
                'COALESCE(i.unit, g.i_unit), i.nlink, i.newest_ns, i.last_used_ns, i.taint, i.alloc, i.reclaim '
                'FROM items i NOT INDEXED JOIN groups g ON g.id = i.group_id WHERE i.scan_id = ?', (scan_id,))
    return iter(cur)


def spec_sessions(store: Store) -> List[str]:
    return [r[0] for r in store._conn().execute('SELECT session FROM specs ORDER BY session')]


def evidence_rows(store: Store, scan_id: int, group_keys: Sequence[str]) -> List[Tuple[str, str, str]]:
    """(key, path, raw evidence JSON) of every item in the given groups."""
    if not group_keys:
        return []
    # CROSS JOIN pins the order: the few groups first, then their items through items_group.
    sql = (f"SELECT i.key, {PATH_SQL}, COALESCE(e.json, '[]') FROM groups g CROSS JOIN items i INDEXED BY items_group "
           'LEFT JOIN dirs d ON d.id = i.dir_id LEFT JOIN evidence e ON e.id = i.evidence_id '
           'WHERE g.scan_id = ? AND g.key IN (SELECT value FROM json_each(?)) AND i.scan_id = ? '
           'AND i.group_id = g.id')
    return [(r[0], r[1], r[2]) for r in store._conn().execute(sql, (scan_id, json.dumps(list(group_keys)), scan_id))]


def taint_split(store: Store, scan_id: int) -> Dict[Tuple[str, bool], Tuple[int, int]]:
    """(group key, tainted?) -> (items, alloc). Taint is item level, so groups alone cannot answer it."""
    sql = ("SELECT g.key, s.tainted, s.n, s.alloc FROM (SELECT group_id, taint != '' AS tainted, COUNT(*) AS n, "
           'SUM(alloc) AS alloc FROM items WHERE scan_id = ? GROUP BY group_id, tainted) s '
           'JOIN groups g ON g.id = s.group_id')
    return {(r[0], bool(r[1])): (int(r[2]), int(r[3])) for r in store._conn().execute(sql, (scan_id,))}


# plan creation ---------------------------------------------------------------------------------------------

PLAN_ITEM_COLUMNS = ('key', 'path', 'unit', 'rule_id', 'group_key', 'alloc', 'reclaim', 'dev', 'ino', 'size',
                     'mtime_ns', 'birth_ns', 'nlink', 'files', 'newest_ns')


def plan_item_rows(store: Store, scan_id: int) -> Iterator[Tuple[Any, ...]]:
    """Every item of the scan as a plain tuple in PLAN_ITEM_COLUMNS order. Everything a plan entry and its
    fingerprint need is here, so no Item objects are built for the (up to 10^5) selected items."""
    cur = store._conn().cursor()
    cur.row_factory = None
    cur.execute(f'SELECT i.key, {PATH_SQL}, COALESCE(i.unit, g.i_unit), COALESCE(i.rule_id, g.i_rule_id), g.key, '
                'i.alloc, i.reclaim, i.dev, i.ino, i.size, i.mtime_ns, i.birth_ns, i.nlink, i.files, i.newest_ns '
                f'FROM items i NOT INDEXED JOIN groups g ON g.id = i.group_id LEFT JOIN dirs d ON d.id = i.dir_id '
                'WHERE i.scan_id = ?', (scan_id,))
    return iter(cur)


def plan_group_totals(store: Store, plan_id: str) -> List[Tuple[str, str, str, str, int, int, int]]:
    """(group key, rule id, state, action, entries, alloc, reclaim) of a plan, grouped; the database does the adding."""
    sql = ('SELECT group_key, rule_id, state, action, COUNT(*), SUM(alloc), SUM(reclaim) FROM plan_entries '
           'WHERE plan_id = ? GROUP BY group_key, rule_id, state, action ORDER BY group_key, state, action')
    return [tuple(r) for r in store._conn().execute(sql, (plan_id,))]


def plan_seqs(store: Store, plan_id: str, group_key: str, state: str) -> List[int]:
    """The seq numbers of a plan's entries of one group in one state."""
    return [r[0] for r in store._conn().execute(
        'SELECT seq FROM plan_entries WHERE plan_id = ? AND group_key = ? AND state = ? ORDER BY seq',
        (plan_id, group_key, state))]


def describe_rows(store: Store, scan_id: int, keys: Sequence[str], limit: int) -> List[Tuple[str, str, int]]:
    """(key, path, alloc) of the `limit` largest of the given items; the database does the ordering."""
    if not keys:
        return []
    sql = (f'SELECT i.key, {PATH_SQL}, i.alloc FROM items i{BY_KEY} LEFT JOIN dirs d ON d.id = i.dir_id '
           'WHERE i.scan_id = ? AND i.key IN (SELECT value FROM json_each(?)) ORDER BY i.alloc DESC, i.key LIMIT ?')
    return [(r[0], r[1], r[2]) for r in store._conn().execute(sql, (scan_id, json.dumps(list(keys)), limit))]


# Entry columns in dataclass order, plus updated_at (the order Store.save_plan inserts them in).
_ENTRY_FIELDS = tuple(f.name for f in dataclasses.fields(Entry))
if _ENTRY_FIELDS != ('plan_id', 'seq', 'item_key', 'path', 'unit', 'action', 'rule_id', 'group_key', 'threshold_days',
                     'alloc', 'reclaim', 'fp', 'state', 'reason', 'freed', 'archive_id'):
    raise RuntimeError(f'model.Entry changed ({_ENTRY_FIELDS}); queries.insert_plan must follow')


def insert_plan(store: Store, plan: Plan, entry_rows: Iterable[Tuple[Any, ...]]) -> None:
    """Store.save_plan without building an Entry per row: one transaction, one executemany. A row is
    (plan_id, seq, item_key, path, unit, action, rule_id, group_key, threshold_days, alloc, reclaim, fp_json,
    state, reason, freed, archive_id, updated_at), exactly what save_plan writes."""
    with store._tx() as c:
        c.execute(_insert_sql('plans', Plan), _params(plan))
        c.executemany(_insert_sql('plan_entries', Entry, ('updated_at',)), entry_rows)


# Insights ---------------------------------------------------------------------------------------------------

def done_scans(store: Store) -> List[Dict[str, Any]]:
    """Every finished scan, oldest first: id, started_at, finished_at, vol_total, vol_free, attributed."""
    return [dict(r) for r in store._conn().execute(
        "SELECT id, started_at, finished_at, vol_total, vol_free, attributed FROM scans "
        "WHERE status = 'done' AND vol_total IS NOT NULL AND vol_free IS NOT NULL ORDER BY id")]


def scan_total_rows(store: Store) -> Dict[int, Dict[str, Any]]:
    """scan id -> {'junk', 'review', 'protected', 'report_only', 'items', 'backfilled'}: scan_totals folded into the
    four buckets of the composition chart (alloc each). report-only wins over the tier, as in the report."""
    out: Dict[int, Dict[str, Any]] = {}
    for scan_id, tier, report_only, alloc, items, backfilled in store._conn().execute(
            'SELECT scan_id, tier, report_only, SUM(alloc), SUM(items), MAX(backfilled) FROM scan_totals '
            'GROUP BY scan_id, tier, report_only'):
        slot = out.setdefault(scan_id, {'junk': 0, 'review': 0, 'protected': 0, 'report_only': 0, 'items': 0,
                                        'backfilled': False})
        slot['report_only' if report_only else tier] += alloc
        slot['items'] += items
        slot['backfilled'] = slot['backfilled'] or bool(backfilled)
    return out


def history_allocs(store: Store, scan_id: int) -> Dict[str, int]:
    """group key -> alloc of one scan, from group_history (which outlives the scan's rows)."""
    return {r[0]: r[1] for r in store._conn().execute(
        'SELECT key, alloc FROM group_history WHERE scan_id = ?', (scan_id,))}


def cleanup_rows(store: Store) -> List[Dict[str, Any]]:
    """One row per plan with at least one done entry, newest first. `removed` counts every done entry (Trash moves
    included, `moved_to_trash` is their share). measured_delta, estimated and, when the summary has it, finished_at
    come from the run summary kept in meta ('run:<plan_id>'); without one they are None and the time of the last done
    entry. A plan whose entries were compacted is read from its summary alone. The entries are summed over covering
    indexes only (plan_entries_done, plan_entries_state), so a long history stays cheap."""
    conn = store._conn()
    other: Dict[str, Dict[str, int]] = {}
    for plan_id, state, n in conn.execute("SELECT plan_id, state, COUNT(*) FROM plan_entries "
                                          "WHERE state IN ('skipped', 'failed', 'excluded') GROUP BY plan_id, state"):
        other.setdefault(plan_id, {})[state] = n
    runs = {r[0]: json.loads(r[1]) for r in conn.execute(
        "SELECT substr(key, ?), json_extract(value, '$.measured_delta', '$.estimated_delta', '$.finished_at', "
        "'$.items_done', '$.items_skipped', '$.items_failed', '$.items_excluded', '$.removed', '$.freed', "
        "'$.moved_to_trash') FROM meta WHERE key LIKE ?", (len(RUN_KEY) + 1, RUN_KEY + '%'))}
    created = {r[0]: r[1] for r in conn.execute('SELECT id, created_at FROM plans')}
    rows = []
    for plan_id in store.compacted_ids():
        measured, estimated, finished, done, skipped, failed, excluded, removed, freed, trashed = runs[plan_id]
        if done:
            rows.append({'kind': 'plan', 'plan_id': plan_id, 'created_at': created[plan_id], 'finished_at': finished,
                         'items_done': done, 'items_skipped': skipped, 'items_failed': failed,
                         'items_excluded': excluded, 'removed': removed, 'freed': freed, 'moved_to_trash': trashed,
                         'measured_delta': measured, 'estimated': estimated})
    for plan_id, done, removed, freed, trashed, last_done in conn.execute(
            "SELECT plan_id, COUNT(*), SUM(alloc), SUM(freed), SUM(CASE WHEN action = 'trash' THEN alloc ELSE 0 END), "
            "MAX(updated_at) FROM plan_entries WHERE state = 'done' GROUP BY plan_id"):
        counts = other.get(plan_id, {})
        measured, estimated, finished = runs.get(plan_id, (None, None, None))[:3]
        rows.append({'kind': 'plan', 'plan_id': plan_id, 'created_at': created[plan_id],
                     'finished_at': last_done if finished is None else finished, 'items_done': done,
                     'items_skipped': counts.get('skipped', 0), 'items_failed': counts.get('failed', 0),
                     'items_excluded': counts.get('excluded', 0), 'removed': removed, 'freed': freed,
                     'moved_to_trash': trashed, 'measured_delta': measured, 'estimated': estimated})
    return sorted(rows, key=lambda r: (-r['finished_at'], r['plan_id']))


def native_rows(store: Store) -> List[Dict[str, Any]]:
    """The runs of native cleanup commands (meta 'native:<ms>') in the shape of a cleanup row: kind 'native', no plan,
    no item counts, `freed` what the tool freed (before minus after, never below 0), plus rule_id, target, command,
    exit_code, before and after. Newest first."""
    rows = []
    for _key, value in store._conn().execute("SELECT key, value FROM meta WHERE key LIKE 'native:%'"):
        r = json.loads(value)
        rows.append({'kind': 'native', 'plan_id': None, 'rule_id': r['rule_id'], 'target': r['target'],
                     'command': r['command'], 'created_at': r['started_at'], 'finished_at': r['finished_at'],
                     'items_done': None, 'items_skipped': None, 'items_failed': None, 'items_excluded': None,
                     'removed': r['freed'], 'freed': r['freed'], 'moved_to_trash': 0, 'measured_delta': None,
                     'estimated': None, 'exit_code': r['exit_code'], 'before': r['before'], 'after': r['after']})
    return sorted(rows, key=lambda r: (-r['finished_at'], r['command']))


def stale_archives(store: Store, before: float) -> Tuple[int, int]:
    """(count, stored bytes) of archives made before `before` that still hold data on disk (stored or restored)."""
    row = store._conn().execute(
        "SELECT COUNT(*), COALESCE(SUM(stored_bytes), 0) FROM archives "
        "WHERE state IN ('stored', 'restored') AND created_at < ?", (before,)).fetchone()
    return int(row[0]), int(row[1])


def quick_totals(store: Store, scan_id: int, now: float, filter_days: float, domains: Sequence[str],
                 trash_available: bool) -> Dict[str, Tuple[int, int]]:
    """group key -> (selected items, their alloc) when every junk group that has an action, in the enabled `domains`, is
    selected with the age filter `filter_days`: what selection.resolve_for returns for that spec (the overview's
    "safe to clean"), worked out by the database instead of one Python call per item. That is ten times faster on
    10^5 items; the Insights page is rebuilt on every window switch. It mirrors selection.decide() step by step
    (tier or taint, area, hard links, supported default action, floor and age threshold, the same float arithmetic),
    and tests/test_insights_queries.py holds the two to the same answer. Groups with nothing selected are absent."""
    sql = ("SELECT g.key, COUNT(*), SUM(i.alloc) FROM groups g CROSS JOIN items i INDEXED BY items_group "
           "WHERE g.scan_id = ?1 AND g.provider IS NOT NULL AND g.tier = 'junk' AND g.actions != '[]' "
           "AND g.domain IN (SELECT value FROM json_each(?2)) "
           "AND EXISTS (SELECT 1 FROM json_each(g.actions) WHERE value = g.default_action) "
           "AND (?3 OR g.default_action != 'trash') "
           "AND i.scan_id = ?1 AND i.group_id = g.id "
           "AND COALESCE(i.tier, g.i_tier) != 'protected' AND i.taint = '' "
           "AND COALESCE(i.domain, g.i_domain) IN (SELECT value FROM json_each(?2)) "
           "AND NOT (COALESCE(i.unit, g.i_unit) = 'file' AND i.nlink > 1) "
           "AND (?4 - MAX(i.newest_ns, COALESCE(CASE WHEN g.age_basis = 'last_used' THEN i.last_used_ns END, "
           "i.newest_ns)) / 1e9) / 86400.0 >= MAX(g.min_age_floor_days, COALESCE(g.default_min_age_days, 0.0), ?5) "
           "GROUP BY g.key")
    return {r[0]: (int(r[1]), int(r[2])) for r in store._conn().execute(
        sql, (scan_id, json.dumps(list(domains)), int(trash_available), now, filter_days))}
