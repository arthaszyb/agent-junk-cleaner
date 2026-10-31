"""SQLite persistence. Logical paths only; JSON columns are TEXT; enums are stored as their values."""
from __future__ import annotations

import contextlib
import dataclasses
import enum
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple, Type

from .model import (Action, AgeBasis, Domain, Entry, EntryState, ErrorCode, Evidence, Group, Item, Kind, Outcome,
                    Plan, PlanState, Spec, StewardError, Tier, Unit, asdict)

FINAL_SCAN_STATUSES = ('done', 'cancelled', 'failed')
BUSY_TIMEOUT_MS = 5000
RUN_KEY = 'run:'             # meta key prefix: the summary of a finished plan (see Store.save_run_summary)
COMPACTED_KEY = 'compacted:'  # meta key prefix: when a finished plan's done and cancelled entries were removed
FINISHED_PLAN_STATES = ('done', 'cancelled', 'failed')
FRAGMENTED = 0.10            # share of free pages in the file above which it is shrunk when the App starts

_SCHEMA_V1: Tuple[str, ...] = (
    'CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)',
    '''CREATE TABLE scans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at REAL NOT NULL, finished_at REAL, status TEXT NOT NULL,
        catalog_hash TEXT NOT NULL, facts_hash TEXT NOT NULL, settings_hash TEXT NOT NULL,
        domains TEXT NOT NULL, vol_total INTEGER, vol_free INTEGER, attributed INTEGER,
        error TEXT NOT NULL DEFAULT '')''',
    '''CREATE TABLE scan_roots (
        scan_id INTEGER NOT NULL REFERENCES scans(id), path TEXT NOT NULL, domain TEXT NOT NULL,
        status TEXT NOT NULL, files INTEGER NOT NULL DEFAULT 0, alloc INTEGER NOT NULL DEFAULT 0)''',
    'CREATE INDEX scan_roots_scan ON scan_roots (scan_id)',
    '''CREATE TABLE scan_errors (
        scan_id INTEGER NOT NULL REFERENCES scans(id), path TEXT NOT NULL, error TEXT NOT NULL)''',
    'CREATE INDEX scan_errors_scan ON scan_errors (scan_id)',
    '''CREATE TABLE groups (
        scan_id INTEGER NOT NULL REFERENCES scans(id), key TEXT NOT NULL, rule_id TEXT NOT NULL,
        provider TEXT NOT NULL, domain TEXT NOT NULL, tier TEXT NOT NULL, kind TEXT NOT NULL,
        unit TEXT NOT NULL, label_args TEXT NOT NULL, items INTEGER NOT NULL, alloc INTEGER NOT NULL,
        reclaim INTEGER NOT NULL, newest_ns INTEGER NOT NULL, oldest_ns INTEGER NOT NULL,
        actions TEXT NOT NULL, default_action TEXT, min_age_floor_days REAL NOT NULL,
        default_min_age_days REAL, age_basis TEXT NOT NULL, hint TEXT, evidence TEXT NOT NULL,
        PRIMARY KEY (scan_id, key))''',
    '''CREATE TABLE items (
        scan_id INTEGER NOT NULL REFERENCES scans(id), key TEXT NOT NULL, path TEXT NOT NULL,
        rule_id TEXT NOT NULL, group_key TEXT NOT NULL, tier TEXT NOT NULL, domain TEXT NOT NULL,
        kind TEXT NOT NULL, unit TEXT NOT NULL, dev INTEGER NOT NULL, ino INTEGER NOT NULL,
        size INTEGER NOT NULL, alloc INTEGER NOT NULL, reclaim INTEGER NOT NULL, files INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL, birth_ns INTEGER NOT NULL, newest_ns INTEGER NOT NULL,
        last_used_ns INTEGER, nlink INTEGER NOT NULL, uid INTEGER NOT NULL, flags INTEGER NOT NULL,
        taint TEXT NOT NULL, evidence TEXT NOT NULL,
        PRIMARY KEY (scan_id, key))''',
    'CREATE INDEX items_group ON items (scan_id, group_key, alloc DESC)',
    'CREATE INDEX items_alloc ON items (scan_id, alloc DESC)',
    '''CREATE TABLE group_history (
        key TEXT NOT NULL, scan_id INTEGER NOT NULL REFERENCES scans(id), finished_at REAL NOT NULL,
        alloc INTEGER NOT NULL, items INTEGER NOT NULL, PRIMARY KEY (key, scan_id))''',
    '''CREATE TABLE specs (
        session TEXT PRIMARY KEY, scan_id INTEGER NOT NULL REFERENCES scans(id),
        version INTEGER NOT NULL, body TEXT NOT NULL, updated_at REAL NOT NULL)''',
    '''CREATE TABLE plans (
        id TEXT PRIMARY KEY, created_at REAL NOT NULL, expires_at REAL NOT NULL,
        scan_id INTEGER NOT NULL REFERENCES scans(id), catalog_hash TEXT NOT NULL, facts_hash TEXT NOT NULL,
        settings_hash TEXT NOT NULL, spec TEXT NOT NULL, revision INTEGER NOT NULL,
        entries_digest TEXT NOT NULL, state TEXT NOT NULL, totals TEXT NOT NULL, error TEXT NOT NULL)''',
    '''CREATE TABLE plan_entries (
        plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE, seq INTEGER NOT NULL,
        item_key TEXT NOT NULL, path TEXT NOT NULL, unit TEXT NOT NULL, action TEXT NOT NULL,
        rule_id TEXT NOT NULL, group_key TEXT NOT NULL, threshold_days REAL NOT NULL, alloc INTEGER NOT NULL,
        reclaim INTEGER NOT NULL, fp TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL,
        freed INTEGER NOT NULL, archive_id TEXT, updated_at REAL NOT NULL,
        PRIMARY KEY (plan_id, seq))''',
    'CREATE INDEX plan_entries_state ON plan_entries (plan_id, state)',
    '''CREATE TABLE archives (
        id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES plans(id), seq INTEGER NOT NULL,
        path TEXT NOT NULL, fmt TEXT NOT NULL, orig_size INTEGER NOT NULL, orig_alloc INTEGER NOT NULL,
        stored_bytes INTEGER NOT NULL, sha_orig TEXT NOT NULL, sha_stored TEXT NOT NULL,
        manifest TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL)''',
    '''CREATE TABLE advice (
        id INTEGER PRIMARY KEY AUTOINCREMENT, scan_id INTEGER NOT NULL REFERENCES scans(id),
        provider TEXT NOT NULL, cli_version TEXT NOT NULL, payload_digest TEXT NOT NULL,
        result TEXT NOT NULL, created_at REAL NOT NULL)''',
)

# v2: compact scan tables. Everything a scan wrote is rebuilt by the next scan, so the old tables are dropped, not
# converted; plans, archives, history, specs and meta are not touched. An item row holds its directory (dirs) and its
# evidence (evidence) by id and its group by groups.id. Its rule/tier/domain/kind/unit columns are NULL while they
# equal the group's i_* columns (groups never mix tiers, but one group may hold items of two rules). A groups row
# exists from the first item added to it (provider IS NULL until add_groups fills in the Group fields), so items can
# be streamed before the aggregates are known. dir_id, group_id and evidence_id carry no foreign key: SQLite would
# scan items for every parent row the garbage collection deletes.
_SCHEMA_V2: Tuple[str, ...] = (
    'DROP TABLE items',
    'DROP TABLE groups',
    'DROP TABLE scan_errors',
    'DROP TABLE scan_roots',
    'CREATE TABLE dirs (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE)',
    'CREATE TABLE evidence (id INTEGER PRIMARY KEY, json TEXT NOT NULL UNIQUE)',
    '''CREATE TABLE scan_roots (
        scan_id INTEGER NOT NULL REFERENCES scans(id), path TEXT NOT NULL, domain TEXT NOT NULL,
        status TEXT NOT NULL, files INTEGER NOT NULL DEFAULT 0, alloc INTEGER NOT NULL DEFAULT 0)''',
    'CREATE INDEX scan_roots_scan ON scan_roots (scan_id)',
    '''CREATE TABLE scan_errors (
        scan_id INTEGER NOT NULL REFERENCES scans(id), path TEXT NOT NULL, error TEXT NOT NULL)''',
    'CREATE INDEX scan_errors_scan ON scan_errors (scan_id)',
    '''CREATE TABLE groups (
        id INTEGER PRIMARY KEY, scan_id INTEGER NOT NULL REFERENCES scans(id), key TEXT NOT NULL,
        i_rule_id TEXT NOT NULL, i_tier TEXT NOT NULL, i_domain TEXT NOT NULL, i_kind TEXT NOT NULL,
        i_unit TEXT NOT NULL,
        rule_id TEXT, provider TEXT, domain TEXT, tier TEXT, kind TEXT, unit TEXT, label_args TEXT, items INTEGER,
        alloc INTEGER, reclaim INTEGER, newest_ns INTEGER, oldest_ns INTEGER, actions TEXT, default_action TEXT,
        min_age_floor_days REAL, default_min_age_days REAL, age_basis TEXT, hint TEXT, evidence TEXT,
        UNIQUE (scan_id, key))''',
    '''CREATE TABLE items (
        scan_id INTEGER NOT NULL REFERENCES scans(id), key TEXT NOT NULL, dir_id INTEGER, name TEXT NOT NULL,
        group_id INTEGER NOT NULL, rule_id TEXT, tier TEXT, domain TEXT, kind TEXT, unit TEXT,
        dev INTEGER NOT NULL, ino INTEGER NOT NULL, size INTEGER NOT NULL, alloc INTEGER NOT NULL,
        reclaim INTEGER NOT NULL, files INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, birth_ns INTEGER NOT NULL,
        newest_ns INTEGER NOT NULL, last_used_ns INTEGER, nlink INTEGER NOT NULL, uid INTEGER NOT NULL,
        flags INTEGER NOT NULL, taint TEXT NOT NULL, evidence_id INTEGER)''',
    'CREATE UNIQUE INDEX items_key ON items (scan_id, key)',
    'CREATE INDEX items_group ON items (scan_id, group_id, alloc DESC)',
    'CREATE INDEX items_alloc ON items (scan_id, alloc DESC)',
    # What an explicit include looked like when it was added, so that a rescan can tell whether the file is still
    # the one the user picked without the old scan's rows. path names the item in migration notes.
    '''CREATE TABLE spec_include_fp (
        session TEXT NOT NULL, item_key TEXT NOT NULL, fp TEXT NOT NULL, path TEXT NOT NULL,
        PRIMARY KEY (session, item_key))''',
)

# v3: per-scan totals by tier, area and report-only flag, written when a scan finishes and never pruned, so the
# Insights history survives the pruning of the groups it is made from. Rows of scans that finished before v3 are
# rebuilt from group_history (see _backfill_scan_totals) and carry backfilled = 1.
_SCHEMA_V3: Tuple[str, ...] = (
    '''CREATE TABLE scan_totals (
        scan_id INTEGER NOT NULL REFERENCES scans(id), tier TEXT NOT NULL, domain TEXT NOT NULL,
        report_only INTEGER NOT NULL, alloc INTEGER NOT NULL, reclaim INTEGER NOT NULL, items INTEGER NOT NULL,
        backfilled INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (scan_id, tier, domain, report_only))''',
    # The cleanup history sums every done entry of every plan (10^5 rows per big run). Over this index alone that
    # takes a fifth of the time a walk over the wide plan_entries rows needs, and it holds only done entries.
    "CREATE INDEX plan_entries_done ON plan_entries (plan_id, action, alloc, freed, updated_at) WHERE state = 'done'",
)

# Index n holds the statements that take the schema from version n to n + 1.
MIGRATIONS: Tuple[Tuple[str, ...], ...] = (_SCHEMA_V1, _SCHEMA_V2, _SCHEMA_V3)
INCREMENTAL_VACUUM_SCHEMA = 2   # first version created with auto_vacuum=INCREMENTAL; older files are VACUUMed once
ITEM_BATCH = 5000               # rows per executemany when add_items is fed a long iterable

# An item's path is its directory's dirs.path plus '/' plus items.name; a path without '/' has no directory row.
PATH_SQL = "COALESCE(d.path || '/', '') || i.name"
_ITEM_ORDERS = {
    'alloc': 'i.alloc DESC, i.key',
    'size': 'i.size DESC, i.key',
    'path': f'{PATH_SQL}, i.key',
    'newest': 'i.newest_ns DESC, i.key',
    'oldest': 'i.newest_ns, i.key',
}

_ITEM_INSERT = f'INSERT INTO items VALUES ({", ".join("?" * 25)})'   # in the column order of the table
_ITEM_SELECT = (
    'SELECT i.scan_id, i.key, d.path, i.name, g.key, COALESCE(i.rule_id, g.i_rule_id), COALESCE(i.tier, g.i_tier), '
    'COALESCE(i.domain, g.i_domain), COALESCE(i.kind, g.i_kind), COALESCE(i.unit, g.i_unit), i.dev, i.ino, i.size, '
    'i.alloc, i.reclaim, i.files, i.mtime_ns, i.birth_ns, i.newest_ns, i.last_used_ns, i.nlink, i.uid, i.flags, '
    'i.taint, e.json FROM items i{index} JOIN groups g ON g.id = i.group_id LEFT JOIN dirs d ON d.id = i.dir_id '
    'LEFT JOIN evidence e ON e.id = i.evidence_id')
# A key list is a handful of keys, but without statistics the planner walks items_alloc to dodge the sort instead.
BY_KEY = ' INDEXED BY items_key'
_TIERS = {v.value: v for v in Tier}
_DOMAINS = {v.value: v for v in Domain}
_KINDS = {v.value: v for v in Kind}
_UNITS = {v.value: v for v in Unit}

_ARCHIVE_COLUMNS = ('id', 'plan_id', 'seq', 'path', 'fmt', 'orig_size', 'orig_alloc', 'stored_bytes',
                    'sha_orig', 'sha_stored', 'manifest', 'state')
_ADVICE_COLUMNS = ('scan_id', 'provider', 'cli_version', 'payload_digest', 'result')


def _dump(value: Any) -> str:
    return json.dumps(asdict(value), sort_keys=True, separators=(',', ':'))


def _load_evidence(text: str) -> Tuple[Evidence, ...]:
    return tuple(Evidence(key=e['key'], args=e['args']) for e in json.loads(text))


def _load_actions(text: str) -> Tuple[Action, ...]:
    return tuple(Action(a) for a in json.loads(text))


# Columns that are not plain scalars, by dataclass field name. Anything not listed is stored as is.
_JSON_FIELDS = frozenset(('evidence', 'label_args', 'actions', 'spec', 'totals', 'fp'))
_DECODERS: Dict[Type[Any], Dict[str, Callable[[Any], Any]]] = {
    Group: dict(domain=Domain, tier=Tier, kind=Kind, unit=Unit, label_args=json.loads, actions=_load_actions,
                default_action=Action, age_basis=AgeBasis, evidence=_load_evidence),
    Plan: dict(state=PlanState, spec=json.loads, totals=json.loads),
    Entry: dict(unit=Unit, action=Action, state=EntryState, fp=json.loads),
}


def _columns(cls: Type[Any]) -> Tuple[str, ...]:
    return tuple(f.name for f in dataclasses.fields(cls))


def _params(obj: Any) -> Tuple[Any, ...]:
    out: List[Any] = []
    for name in _columns(type(obj)):
        v = getattr(obj, name)
        if name in _JSON_FIELDS:
            out.append(_dump(v))
        elif isinstance(v, enum.Enum):
            out.append(v.value)
        else:
            out.append(v)
    return tuple(out)


def _from_row(cls: Type[Any], row: sqlite3.Row) -> Any:
    decoders = _DECODERS[cls]
    kwargs = {}
    for name in _columns(cls):
        v = row[name]
        kwargs[name] = decoders[name](v) if name in decoders and v is not None else v
    return cls(**kwargs)


def _insert_sql(table: str, cls: Type[Any], extra: Sequence[str] = ()) -> str:
    cols = _columns(cls) + tuple(extra)
    return f'INSERT INTO {table} ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))})'


def _enum_value(v: Any) -> Any:
    return v.value if isinstance(v, enum.Enum) else v


def _not_found(what: str, ident: Any) -> StewardError:
    return StewardError(ErrorCode.NOT_FOUND, 'error.NOT_FOUND', what=what, id=str(ident))


def group_key_class(key: str, catalog: Any) -> Tuple[str, str, bool]:
    """(tier, domain, report_only) of a group key, read from `catalog` as it is today. group_history keeps nothing
    but the key, so this is an approximation for scans whose groups are gone: a rule the catalog no longer has
    counts as protected, and a rule whose tier changed since the scan is judged by its current tier."""
    from .classify import is_protected_variant, split_group_key   # lazy: classify imports the catalog
    rule_id, suffix = split_group_key(key)
    try:
        rule = catalog.rule(rule_id)
    except KeyError:
        rule = None
    if rule is None:
        return Tier.PROTECTED.value, _domain_by_prefix(suffix, catalog), False
    if rule_id.startswith('prot.'):   # the scanner keys these 'prot.<x>:<domain>' (one group per area)
        domain = suffix if suffix in _DOMAINS else rule.domain.value
        return Tier.PROTECTED.value, domain, False
    if is_protected_variant(key):
        return Tier.PROTECTED.value, rule.domain.value, False
    return rule.tier.value, rule.domain.value, not rule.actions and rule.tier != Tier.PROTECTED


def _domain_by_prefix(path: str, catalog: Any) -> str:
    """The area of an unclaimed path: the scanner walks the union of the roots once and files what it finds under the
    outermost root, so the shortest root that contains `path` decides. Outside every root: app-library."""
    hits = [r for r in catalog.roots if path == r.path or path.startswith(r.path + '/')]
    return min(hits, key=lambda r: len(r.path)).domain.value if hits else Domain.APP_LIBRARY.value


class Store:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self._clock = clock
        self._local = threading.local()
        self._conns: List[Tuple[threading.Thread, sqlite3.Connection]] = []
        self._conns_lock = threading.Lock()
        self._closed = False
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600))  # sqlite would use the umask
        try:
            self._migrate()
        except BaseException:
            self.close()
            raise

    # connections ---------------------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError('store is closed')
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = self._local.conn = self._open()
        return conn

    def _open(self) -> sqlite3.Connection:
        # isolation_level=None: transactions are explicit (_tx), never implicit.
        # check_same_thread=False only so that close() and the dead-thread sweep below may close them.
        conn = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute(f'PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}')
        conn.execute('PRAGMA foreign_keys = ON')
        # WAL + NORMAL never corrupts the file; a power loss may only roll back the newest commits, which the executor
        # tolerates (a lost claim or outcome is re-derived from the disk by recovery). Archive rows, which must be on
        # disk before their source is unlinked, are written through _durable() instead of paying that on every commit.
        conn.execute('PRAGMA synchronous = NORMAL')
        # Only takes effect on an empty file (and at a VACUUM, which _migrate runs once for a v1 file), and it must
        # come before the switch to WAL, which writes the file header.
        conn.execute('PRAGMA auto_vacuum = INCREMENTAL')
        mode = conn.execute('PRAGMA journal_mode = WAL').fetchone()[0]
        if mode.lower() != 'wal':
            conn.close()
            raise RuntimeError(f'SQLite refused WAL mode (journal_mode={mode}); is the data dir on a network volume?')
        conn.create_function('ds_fold', 1, lambda s: s.casefold(), deterministic=True)
        with self._conns_lock:
            # Request-per-thread servers would otherwise leak one connection per request.
            live = []
            for thread, c in self._conns:
                if thread.is_alive():
                    live.append((thread, c))
                else:
                    c.close()
            live.append((threading.current_thread(), conn))
            self._conns = live
        return conn

    def close(self) -> None:
        with self._conns_lock:
            self._closed = True
            for _, c in self._conns:
                c.close()
            self._conns = []

    @contextlib.contextmanager
    def _durable(self) -> Iterator[sqlite3.Connection]:
        """Autocommit writes inside are flushed through the drive cache (F_FULLFSYNC) before returning."""
        conn = self._conn()
        if conn.in_transaction:
            # The pragmas below are ignored inside a transaction, which would silently weaken durability.
            raise RuntimeError('durable write requested inside an open transaction')
        conn.execute('PRAGMA synchronous = FULL')
        conn.execute('PRAGMA fullfsync = ON')
        try:
            yield conn
        finally:
            conn.execute('PRAGMA fullfsync = OFF')
            conn.execute('PRAGMA synchronous = NORMAL')

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        # IMMEDIATE takes the write lock up front, so a busy database fails at BEGIN (and waits
        # busy_timeout) instead of failing halfway through with a lock upgrade deadlock.
        conn = self._conn()
        conn.execute('BEGIN IMMEDIATE')
        try:
            yield conn
        except BaseException:
            conn.execute('ROLLBACK')
            raise
        conn.execute('COMMIT')

    @contextlib.contextmanager
    def read_snapshot(self) -> Iterator[None]:
        """Reads on this thread's connection inside the block see one moment of the database (a WAL snapshot), even
        if a scan finishes and prunes the previous scan's rows meanwhile. Nests: an inner block, or a block inside
        one of this thread's write transactions, joins the view that is already open. Do not write inside it from a
        block that opened it: a snapshot that is out of date cannot be upgraded to a write."""
        conn = self._conn()
        if conn.in_transaction:
            yield
            return
        conn.execute('BEGIN DEFERRED')   # the snapshot starts at the first read
        try:
            yield
        finally:
            if conn.in_transaction:   # SQLite ends the transaction itself after some errors
                conn.execute('COMMIT')

    def _migrate(self) -> None:
        with self._tx() as c:
            has_meta = c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone()
            current = 0
            if has_meta:
                row = c.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
                current = int(row[0]) if row else 0
            if current > len(MIGRATIONS):
                raise RuntimeError(f'database schema version {current} is newer than this program supports '
                                   f'({len(MIGRATIONS)})')
            for n in range(current, len(MIGRATIONS)):
                for statement in MIGRATIONS[n]:
                    c.execute(statement)
                if MIGRATIONS[n] is _SCHEMA_V3:
                    self._backfill_scan_totals(c)
                c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)", (str(n + 1),))
        if 0 < current < INCREMENTAL_VACUUM_SCHEMA:
            # The dropped scan tables are free pages now; VACUUM returns them to the file system and switches the
            # file to auto_vacuum=INCREMENTAL (see _open). It cannot run inside a transaction.
            self._conn().execute('VACUUM')

    @staticmethod
    def _backfill_scan_totals(c: sqlite3.Connection) -> None:
        """scan_totals for every done scan that finished before schema 3. A scan whose groups still exist (the
        latest) is totalled exactly. An older one is rebuilt from group_history, which keeps only the key, size and
        item count of a group: its tier, area and report-only flag are looked up in today's catalog (group_key_class),
        and its reclaimable size is unknown, so it is stored as 0."""
        done = [r[0] for r in c.execute("SELECT id FROM scans WHERE status = 'done' ORDER BY id")]
        if not done:
            return
        exact = {r[0] for r in c.execute('SELECT DISTINCT scan_id FROM groups WHERE provider IS NOT NULL')}
        for scan_id in done:
            if scan_id in exact:
                Store._insert_totals(c, scan_id)
        rebuilt = [i for i in done if i not in exact]
        if not rebuilt:
            return
        from .catalog import load_catalog   # lazy: the catalog imports settings, which imports i18n
        from .settings import Settings
        catalog = load_catalog(Settings())
        for scan_id in rebuilt:
            totals: Dict[Tuple[str, str, int], List[int]] = {}
            for key, alloc, items in c.execute('SELECT key, alloc, items FROM group_history WHERE scan_id = ?',
                                               (scan_id,)).fetchall():
                tier, domain, report_only = group_key_class(key, catalog)
                slot = totals.setdefault((tier, domain, int(report_only)), [0, 0])
                slot[0] += alloc
                slot[1] += items
            c.executemany('INSERT INTO scan_totals (scan_id, tier, domain, report_only, alloc, reclaim, items, '
                          'backfilled) VALUES (?, ?, ?, ?, ?, 0, ?, 1)',
                          [(scan_id, tier, domain, ro, a, n) for (tier, domain, ro), (a, n) in sorted(totals.items())])

    @staticmethod
    def _insert_totals(c: sqlite3.Connection, scan_id: int) -> None:
        """scan_totals of one scan from its groups: report-only means no action and not protected, the same split
        the report makes."""
        c.execute("INSERT INTO scan_totals (scan_id, tier, domain, report_only, alloc, reclaim, items, backfilled) "
                  "SELECT scan_id, tier, domain, actions = '[]' AND tier != 'protected', SUM(alloc), SUM(reclaim), "
                  "SUM(items), 0 FROM groups WHERE scan_id = ? AND provider IS NOT NULL "
                  "GROUP BY tier, domain, actions = '[]' AND tier != 'protected'", (scan_id,))

    # scans ---------------------------------------------------------------------------------------

    def begin_scan(self, catalog_hash: str, facts_hash: str, settings_hash: str, domains: Iterable[Any]) -> int:
        cur = self._conn().execute(
            'INSERT INTO scans (started_at, status, catalog_hash, facts_hash, settings_hash, domains) '
            "VALUES (?, 'running', ?, ?, ?, ?)",
            (self._clock(), catalog_hash, facts_hash, settings_hash, json.dumps([_enum_value(d) for d in domains])))
        return int(cur.lastrowid)

    def add_items(self, scan_id: int, items: Iterable[Item]) -> None:
        """One transaction. A long iterable is consumed in chunks of ITEM_BATCH rows, so a generator never has to
        fit in memory."""
        with self._tx() as c:
            groups = {r[1]: r for r in c.execute(
                'SELECT id, key, i_rule_id, i_tier, i_domain, i_kind, i_unit FROM groups WHERE scan_id = ?',
                (scan_id,))}
            dir_ids: Dict[str, int] = {}
            evidence_ids: Dict[str, int] = {}
            chunk: List[Tuple[Any, ...]] = []
            for item in items:
                if item.scan_id != scan_id:
                    raise ValueError(f'item {item.key} belongs to scan {item.scan_id}, not {scan_id}')
                tier, domain, kind, unit = (_enum_value(item.tier), _enum_value(item.domain), _enum_value(item.kind),
                                            _enum_value(item.unit))
                base = groups.get(item.group_key)
                if base is None:
                    base = groups[item.group_key] = self._new_group(c, scan_id, item.group_key, item.rule_id, tier,
                                                                    domain, kind, unit)
                directory, slash, name = item.path.rpartition('/')
                dir_id = None
                if slash:
                    dir_id = dir_ids.get(directory)
                    if dir_id is None:
                        dir_id = dir_ids[directory] = self._interned(c, 'dirs', 'path', directory)
                evidence_id = None
                if item.evidence:
                    text = _dump(item.evidence)
                    evidence_id = evidence_ids.get(text)
                    if evidence_id is None:
                        evidence_id = evidence_ids[text] = self._interned(c, 'evidence', 'json', text)
                chunk.append((
                    scan_id, item.key, dir_id, name, base[0], None if item.rule_id == base[2] else item.rule_id,
                    None if tier == base[3] else tier, None if domain == base[4] else domain,
                    None if kind == base[5] else kind, None if unit == base[6] else unit, item.dev, item.ino,
                    item.size, item.alloc, item.reclaim, item.files, item.mtime_ns, item.birth_ns, item.newest_ns,
                    item.last_used_ns, item.nlink, item.uid, item.flags, item.taint, evidence_id))
                if len(chunk) >= ITEM_BATCH:
                    c.executemany(_ITEM_INSERT, chunk)
                    chunk = []
            c.executemany(_ITEM_INSERT, chunk)

    @staticmethod
    def _interned(c: sqlite3.Connection, table: str, column: str, value: str) -> int:
        row = c.execute(f'SELECT id FROM {table} WHERE {column} = ?', (value,)).fetchone()
        if row is not None:
            return int(row[0])
        return int(c.execute(f'INSERT INTO {table} ({column}) VALUES (?)', (value,)).lastrowid)

    @staticmethod
    def _new_group(c: sqlite3.Connection, scan_id: int, key: str, rule_id: str, tier: str, domain: str, kind: str,
                   unit: str) -> Tuple[Any, ...]:
        """The groups row for `key`, made by whichever of add_items and add_groups meets the group first.
        Item rows inherit rule/tier/domain/kind/unit from it, so it never changes afterwards."""
        gid = c.execute('INSERT INTO groups (scan_id, key, i_rule_id, i_tier, i_domain, i_kind, i_unit) '
                        'VALUES (?,?,?,?,?,?,?)', (scan_id, key, rule_id, tier, domain, kind, unit)).lastrowid
        return (gid, key, rule_id, tier, domain, kind, unit)

    def add_groups(self, scan_id: int, groups: Iterable[Group]) -> None:
        columns = _columns(Group)[2:]   # after scan_id and key
        update = f'UPDATE groups SET {", ".join(f"{name} = ?" for name in columns)} WHERE id = ?'
        with self._tx() as c:
            started = {r[0]: r[1] for r in c.execute(
                'SELECT key, id FROM groups WHERE scan_id = ? AND provider IS NULL', (scan_id,))}
            for group in groups:
                if group.scan_id != scan_id:
                    raise ValueError(f'group {group.key} belongs to scan {group.scan_id}, not {scan_id}')
                values = _params(group)
                gid = started.get(group.key)
                if gid is None:   # no item arrived first; a duplicate key fails on UNIQUE (scan_id, key)
                    gid = self._new_group(c, scan_id, group.key, group.rule_id, _enum_value(group.tier),
                                          _enum_value(group.domain), _enum_value(group.kind),
                                          _enum_value(group.unit))[0]
                c.execute(update, values[2:] + (gid,))

    def add_scan_root(self, scan_id: int, path: str, domain: Any, status: str, files: int = 0, alloc: int = 0) -> None:
        self._conn().execute('INSERT INTO scan_roots (scan_id, path, domain, status, files, alloc) VALUES (?,?,?,?,?,?)',
                             (scan_id, path, _enum_value(domain), status, files, alloc))

    def add_scan_error(self, scan_id: int, path: str, error: str) -> None:
        self._conn().execute('INSERT INTO scan_errors (scan_id, path, error) VALUES (?,?,?)', (scan_id, path, error))

    def discard_items(self, scan_id: int) -> None:
        """Drop the items and groups a scan streamed in before it was cancelled or failed; nothing reads them."""
        with self._tx() as c:
            c.execute('DELETE FROM items WHERE scan_id = ?', (scan_id,))
            c.execute('DELETE FROM groups WHERE scan_id = ?', (scan_id,))
            self._collect_garbage(c)
        self._shrink()

    def finish_scan(self, scan_id: int, status: str, vol_total: Optional[int], vol_free: Optional[int],
                    attributed: Optional[int], error: str = '', prune: bool = True) -> None:
        """A 'done' scan makes every other scan's rows obsolete: only the latest scan is ever browsed, and plans,
        history and specs carry what they need themselves. `prune=False` leaves the rows for the caller to drop
        with prune_scans() once it has used them (the service carries selections over first)."""
        if status not in FINAL_SCAN_STATUSES:
            raise ValueError(f'scan status must be one of {FINAL_SCAN_STATUSES}, got {status!r}')
        now = self._clock()
        with self._tx() as c:
            row = c.execute('SELECT status FROM scans WHERE id = ?', (scan_id,)).fetchone()
            if row is None:
                raise _not_found('scan', scan_id)
            if row['status'] != 'running':
                raise RuntimeError(f'scan {scan_id} is already {row["status"]}')
            c.execute('UPDATE scans SET status = ?, finished_at = ?, vol_total = ?, vol_free = ?, attributed = ?, '
                      'error = ? WHERE id = ?', (status, now, vol_total, vol_free, attributed, error, scan_id))
            if status != 'done':
                return
            c.execute('INSERT INTO group_history (key, scan_id, finished_at, alloc, items) '
                      'SELECT key, scan_id, ?, alloc, items FROM groups WHERE scan_id = ? AND provider IS NOT NULL',
                      (now, scan_id))
            self._insert_totals(c, scan_id)
            if prune:
                self._prune_rows(c)
        if prune:
            self._shrink()

    def prune_scans(self) -> None:
        """Keep the rows of the latest done scan only (finish_scan does this unless told not to)."""
        with self._tx() as c:
            self._prune_rows(c)
        self._shrink()

    @staticmethod
    def _collect_garbage(c: sqlite3.Connection) -> None:
        c.execute('DELETE FROM dirs WHERE id NOT IN (SELECT dir_id FROM items WHERE dir_id IS NOT NULL)')
        c.execute('DELETE FROM evidence WHERE id NOT IN (SELECT evidence_id FROM items WHERE evidence_id IS NOT NULL)')

    def _prune_rows(self, c: sqlite3.Connection) -> None:
        # scans rows stay: plans and group_history point at them. Rows of any other scan go too, including a
        # "running" one left by a crash; live scans never overlap (operation lock).
        latest = c.execute("SELECT id FROM scans WHERE status = 'done' ORDER BY id DESC LIMIT 1").fetchone()
        if latest is None:
            return
        for table in ('items', 'groups', 'scan_errors', 'scan_roots'):
            c.execute(f'DELETE FROM {table} WHERE scan_id != ?', (latest[0],))
        self._collect_garbage(c)

    def _shrink(self) -> None:
        """Give the pages a prune freed back to the file system, and fold the WAL it wrote into the file."""
        conn = self._conn()
        conn.execute('PRAGMA incremental_vacuum').fetchall()   # the pragma frees one page per fetched row
        # TRUNCATE reports busy (and does nothing) while another connection reads; the next one gets it.
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()

    @staticmethod
    def _scan_dict(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d['domains'] = json.loads(d['domains'])
        return d

    def latest_scan(self, status: str = 'done') -> Optional[Dict[str, Any]]:
        row = self._conn().execute('SELECT * FROM scans WHERE status = ? ORDER BY id DESC LIMIT 1',
                                   (status,)).fetchone()
        return None if row is None else self._scan_dict(row)

    def scan(self, scan_id: int) -> Dict[str, Any]:
        row = self._conn().execute('SELECT * FROM scans WHERE id = ?', (scan_id,)).fetchone()
        if row is None:
            raise _not_found('scan', scan_id)
        return self._scan_dict(row)

    def scan_roots(self, scan_id: int) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._conn().execute(
            'SELECT * FROM scan_roots WHERE scan_id = ? ORDER BY rowid', (scan_id,))]

    def scan_errors(self, scan_id: int) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._conn().execute(
            'SELECT * FROM scan_errors WHERE scan_id = ? ORDER BY rowid', (scan_id,))]

    # groups and items ----------------------------------------------------------------------------

    def groups(self, scan_id: int, domain: Any = None, tier: Any = None) -> List[Group]:
        sql = 'SELECT * FROM groups WHERE scan_id = ? AND provider IS NOT NULL'
        args: List[Any] = [scan_id]
        if domain is not None:
            sql += ' AND domain = ?'
            args.append(_enum_value(domain))
        if tier is not None:
            sql += ' AND tier = ?'
            args.append(_enum_value(tier))
        return [_from_row(Group, r) for r in self._conn().execute(sql + ' ORDER BY alloc DESC, key', args)]

    def group(self, scan_id: int, key: str) -> Group:
        row = self._conn().execute('SELECT * FROM groups WHERE scan_id = ? AND key = ? AND provider IS NOT NULL',
                                   (scan_id, key)).fetchone()
        if row is None:
            raise _not_found('group', key)
        return _from_row(Group, row)

    @staticmethod
    def _item_where(scan_id: int, group_key: Optional[str], keys: Optional[Iterable[str]],
                    query: str) -> Tuple[str, List[Any]]:
        where: List[str] = ['i.scan_id = ?']
        args: List[Any] = [scan_id]
        if group_key is not None:
            where.append('i.group_id = (SELECT id FROM groups WHERE scan_id = ? AND key = ?)')
            args += [scan_id, group_key]
        if keys is not None:
            where.append('i.key IN (SELECT value FROM json_each(?))')
            args.append(json.dumps(list(keys)))
        if query:
            where.append(f'instr(ds_fold({PATH_SQL}), ?) > 0')
            args.append(query.casefold())
        return ' AND '.join(where), args

    def _items(self, sql: str, args: Sequence[Any]) -> Iterator[Item]:
        cur = self._conn().cursor()
        cur.row_factory = None   # tuples instead of sqlite3.Row
        evidence: Dict[Optional[str], Tuple[Evidence, ...]] = {None: ()}
        for (scan_id, key, directory, name, group_key, rule_id, tier, domain, kind, unit, dev, ino, size, alloc,
             reclaim, files, mtime_ns, birth_ns, newest_ns, last_used_ns, nlink, uid, flags, taint,
             evidence_json) in cur.execute(sql, args):
            found = evidence.get(evidence_json)
            if found is None:
                found = evidence[evidence_json] = _load_evidence(evidence_json)
            yield Item(scan_id=scan_id, key=key, path=name if directory is None else f'{directory}/{name}',
                       rule_id=rule_id, group_key=group_key, tier=_TIERS[tier], domain=_DOMAINS[domain],
                       kind=_KINDS[kind], unit=_UNITS[unit], dev=dev, ino=ino, size=size, alloc=alloc,
                       reclaim=reclaim, files=files, mtime_ns=mtime_ns, birth_ns=birth_ns, newest_ns=newest_ns,
                       last_used_ns=last_used_ns, nlink=nlink, uid=uid, flags=flags, taint=taint, evidence=found)

    def items(self, scan_id: int, group_key: Optional[str] = None, keys: Optional[Iterable[str]] = None,
              order: str = 'alloc', offset: int = 0, limit: Optional[int] = None, query: str = '') -> List[Item]:
        if order not in _ITEM_ORDERS:
            raise StewardError(ErrorCode.INVALID, 'error.INVALID', field='order', value=str(order))
        where, args = self._item_where(scan_id, group_key, keys, query)
        select = _ITEM_SELECT.format(index=BY_KEY if keys is not None else '')
        sql = f'{select} WHERE {where} ORDER BY {_ITEM_ORDERS[order]} LIMIT ? OFFSET ?'
        args += [-1 if limit is None else limit, offset]
        return list(self._items(sql, args))

    def count_items(self, scan_id: int, group_key: Optional[str] = None, keys: Optional[Iterable[str]] = None,
                    query: str = '') -> int:
        where, args = self._item_where(scan_id, group_key, keys, query)
        index = BY_KEY if keys is not None else ''
        join = 'LEFT JOIN dirs d ON d.id = i.dir_id' if query else ''
        return int(self._conn().execute(f'SELECT COUNT(*) FROM items i{index} {join} WHERE {where}',
                                        args).fetchone()[0])

    def iter_items(self, scan_id: int) -> Iterator[Item]:
        return self._items(f'{_ITEM_SELECT.format(index="")} WHERE i.scan_id = ? ORDER BY i.key', (scan_id,))

    def item(self, scan_id: int, key: str) -> Item:
        found = list(self._items(f'{_ITEM_SELECT.format(index="")} WHERE i.scan_id = ? AND i.key = ?',
                                 (scan_id, key)))
        if not found:
            raise _not_found('item', key)
        return found[0]

    def group_history(self, key: str, limit: int = 20) -> List[Tuple[int, float, int, int]]:
        return [(r['scan_id'], r['finished_at'], r['alloc'], r['items']) for r in self._conn().execute(
            'SELECT scan_id, finished_at, alloc, items FROM group_history WHERE key = ? '
            'ORDER BY scan_id DESC LIMIT ?', (key, limit))]

    # meta ----------------------------------------------------------------------------------------

    def meta(self, key: str) -> Optional[str]:
        row = self._conn().execute('SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
        return None if row is None else row['value']

    def set_meta(self, key: str, value: str) -> None:
        self._conn().execute('INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)', (key, value))

    def delete_meta(self, key: str) -> None:
        self._conn().execute('DELETE FROM meta WHERE key = ?', (key,))

    # specs ---------------------------------------------------------------------------------------

    def save_spec(self, session: str, spec: Spec) -> None:
        """Saves the spec and keeps spec_include_fp in step with its includes: a row goes when its include is
        gone, and an include that has no row yet gets the fingerprint its item has in spec.scan_id now (an include
        of an item that scan does not hold gets none, and a rescan treats it as changed)."""
        with self._tx() as c:
            c.execute('INSERT OR REPLACE INTO specs (session, scan_id, version, body, updated_at) VALUES (?,?,?,?,?)',
                      (session, spec.scan_id, spec.version, json.dumps(spec.to_json(), sort_keys=True),
                       self._clock()))
            have = {r[0] for r in c.execute('SELECT item_key FROM spec_include_fp WHERE session = ?', (session,))}
            c.executemany('DELETE FROM spec_include_fp WHERE session = ? AND item_key = ?',
                          [(session, k) for k in have - set(spec.include)])
            added = sorted(set(spec.include) - have)
            if added:
                c.executemany('INSERT INTO spec_include_fp (session, item_key, fp, path) VALUES (?,?,?,?)',
                              [(session, i.key, json.dumps(i.fingerprint(), sort_keys=True, separators=(',', ':')),
                                i.path) for i in self.items(spec.scan_id, keys=added)])

    def include_fingerprints(self, session: str, keys: Iterable[str]) -> Dict[str, Tuple[str, Dict[str, int]]]:
        """item key -> (path, fingerprint) recorded when the session's include of that item was saved."""
        return {r[0]: (r[2], json.loads(r[1])) for r in self._conn().execute(
            'SELECT item_key, fp, path FROM spec_include_fp WHERE session = ? '
            'AND item_key IN (SELECT value FROM json_each(?))', (session, json.dumps(list(keys))))}

    def load_spec(self, session: str) -> Optional[Spec]:
        row = self._conn().execute('SELECT body FROM specs WHERE session = ?', (session,)).fetchone()
        return None if row is None else Spec.from_json(json.loads(row['body']))

    # plans and entries ---------------------------------------------------------------------------

    def save_plan(self, plan: Plan, entries: Iterable[Entry]) -> None:
        now = self._clock()

        def rows() -> Iterator[Tuple[Any, ...]]:
            for entry in entries:
                if entry.plan_id != plan.id:
                    raise ValueError(f'entry {entry.seq} belongs to plan {entry.plan_id}, not {plan.id}')
                yield _params(entry) + (now,)
        with self._tx() as c:
            c.execute(_insert_sql('plans', Plan), _params(plan))
            c.executemany(_insert_sql('plan_entries', Entry, ('updated_at',)), rows())

    def plan(self, plan_id: str) -> Plan:
        row = self._conn().execute('SELECT * FROM plans WHERE id = ?', (plan_id,)).fetchone()
        if row is None:
            raise _not_found('plan', plan_id)
        return _from_row(Plan, row)

    def entries(self, plan_id: str, state: Any = None, offset: int = 0, limit: Optional[int] = None,
                group: Optional[str] = None) -> List[Entry]:
        sql = 'SELECT * FROM plan_entries WHERE plan_id = ?'
        args: List[Any] = [plan_id]
        if state is not None:
            sql += ' AND state = ?'
            args.append(_enum_value(state))
        if group is not None:
            sql += ' AND group_key = ?'
            args.append(group)
        sql += ' ORDER BY seq LIMIT ? OFFSET ?'
        args += [-1 if limit is None else limit, offset]
        return [_from_row(Entry, r) for r in self._conn().execute(sql, args)]

    def count_entries(self, plan_id: str, state: Any = None, group: Optional[str] = None) -> int:
        sql = 'SELECT COUNT(*) FROM plan_entries WHERE plan_id = ?'
        args: List[Any] = [plan_id]
        if state is not None:
            sql += ' AND state = ?'
            args.append(_enum_value(state))
        if group is not None:
            sql += ' AND group_key = ?'
            args.append(group)
        return int(self._conn().execute(sql, args).fetchone()[0])

    def invalidate_drafts(self, plan_ids: Sequence[str], error: str) -> None:
        """Draft plans that will never run: state 'invalid' with `error`, and their entries (which never ran either) are
        deleted, which also keeps the database small. Anything that is not a draft is left alone."""
        deleted = False
        with self._tx() as c:
            for plan_id in plan_ids:
                n = c.execute("UPDATE plans SET state = 'invalid', error = ? WHERE id = ? AND state = 'draft'",
                              (error, plan_id)).rowcount
                if n:
                    c.execute('DELETE FROM plan_entries WHERE plan_id = ?', (plan_id,))
                    deleted = True
        if deleted:
            self._shrink()

    def supersede_drafts(self, scan_id: int) -> int:
        """A scan replaced what the drafts of older scans were made from. Returns how many were invalidated."""
        ids = [r[0] for r in self._conn().execute("SELECT id FROM plans WHERE state = 'draft' AND scan_id < ?", (scan_id,))]
        self.invalidate_drafts(ids, 'superseded')
        return len(ids)

    def unexclude(self, plan_id: str, seqs: Iterable[int], revision: int) -> int:
        """exclude() the other way round: excluded entries are pending again. Only a draft may do this; a plan that
        has started works from the list of pending entries it began with."""
        wanted = sorted(set(seqs))
        with self._tx() as c:
            row = c.execute('SELECT revision, state FROM plans WHERE id = ?', (plan_id,)).fetchone()
            if row is None:
                raise _not_found('plan', plan_id)
            if row['revision'] != revision:
                raise StewardError(ErrorCode.REVISION_MISMATCH, 'error.REVISION_MISMATCH',
                                   current=row['revision'], given=revision)
            if row['state'] != 'draft':
                raise StewardError(ErrorCode.INVALID, 'error.plan_state', state=row['state'])
            if not wanted:
                return int(row['revision'])
            for seq in wanted:
                found = c.execute('SELECT state FROM plan_entries WHERE plan_id = ? AND seq = ?', (plan_id, seq)).fetchone()
                if found is None:
                    raise _not_found('entry', f'{plan_id}/{seq}')
                if found['state'] not in ('excluded', 'pending'):
                    raise StewardError(ErrorCode.IN_PROGRESS, 'error.IN_PROGRESS', seqs=[seq])
            c.executemany("UPDATE plan_entries SET state = 'pending', updated_at = ? "
                          "WHERE plan_id = ? AND seq = ? AND state = 'excluded'",
                          [(self._clock(), plan_id, seq) for seq in wanted])
            c.execute('UPDATE plans SET revision = revision + 1 WHERE id = ?', (plan_id,))
            return int(row['revision']) + 1

    def plans_in_state(self, state: Any) -> List[Plan]:
        return [_from_row(Plan, r) for r in self._conn().execute(
            'SELECT * FROM plans WHERE state = ? ORDER BY created_at, id', (_enum_value(state),))]

    def entries_in_state(self, state: Any) -> List[Entry]:
        """Entries of every plan in `state` (crash recovery looks for `in_progress` ones)."""
        return [_from_row(Entry, r) for r in self._conn().execute(
            'SELECT * FROM plan_entries WHERE state = ? ORDER BY plan_id, seq', (_enum_value(state),))]

    def set_plan_state(self, plan_id: str, state: Any, error: str = '') -> None:
        n = self._conn().execute('UPDATE plans SET state = ?, error = ? WHERE id = ?',
                                 (_enum_value(state), error, plan_id)).rowcount
        if n != 1:
            raise _not_found('plan', plan_id)

    def claim_entry(self, plan_id: str, seq: int) -> bool:
        """pending -> in_progress as one UPDATE, so exactly one caller wins and an excluded entry is never claimed."""
        n = self._conn().execute(
            "UPDATE plan_entries SET state = 'in_progress', updated_at = ? "
            "WHERE plan_id = ? AND seq = ? AND state = 'pending'", (self._clock(), plan_id, seq)).rowcount
        return n == 1

    def finish_entry(self, plan_id: str, seq: int, outcome: Outcome) -> None:
        n = self._conn().execute(
            'UPDATE plan_entries SET state = ?, reason = ?, freed = ?, archive_id = ?, updated_at = ? '
            'WHERE plan_id = ? AND seq = ?',
            (outcome.state.value, outcome.reason, outcome.freed, outcome.archive_id, self._clock(),
             plan_id, seq)).rowcount
        if n != 1:
            raise _not_found('entry', f'{plan_id}/{seq}')

    def exclude(self, plan_id: str, seqs: Iterable[int], revision: int) -> int:
        wanted = sorted(set(seqs))
        with self._tx() as c:
            row = c.execute('SELECT revision FROM plans WHERE id = ?', (plan_id,)).fetchone()
            if row is None:
                raise _not_found('plan', plan_id)
            if row['revision'] != revision:
                raise StewardError(ErrorCode.REVISION_MISMATCH, 'error.REVISION_MISMATCH',
                                   current=row['revision'], given=revision)
            if not wanted:
                return int(row['revision'])
            states = {}
            for seq in wanted:
                found = c.execute('SELECT state FROM plan_entries WHERE plan_id = ? AND seq = ?',
                                  (plan_id, seq)).fetchone()
                if found is None:
                    raise _not_found('entry', f'{plan_id}/{seq}')
                states[seq] = found['state']
            busy = [seq for seq, st in states.items() if st != 'pending']
            if busy:
                raise StewardError(ErrorCode.IN_PROGRESS, 'error.IN_PROGRESS', seqs=busy)
            c.executemany("UPDATE plan_entries SET state = 'excluded', updated_at = ? WHERE plan_id = ? AND seq = ?",
                          [(self._clock(), plan_id, seq) for seq in wanted])
            c.execute('UPDATE plans SET revision = revision + 1 WHERE id = ?', (plan_id,))
            return int(row['revision']) + 1

    # retention -----------------------------------------------------------------------------------

    def run_summary(self, plan_id: str) -> Optional[Dict[str, Any]]:
        text = self.meta(RUN_KEY + plan_id)
        return None if text is None else json.loads(text)

    def save_run_summary(self, plan_id: str, summary: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """Make the run summary of a finished plan complete and store it as meta 'run:<plan_id>'. `summary` is what the
        executor reported; without it the stored one is completed. A plan that has none (it finished before summaries
        were kept) gets one built from its entries and marked `synthesized`: it has no free-space figures and is not
        shown as the run's result. What the entries can tell (state counts, sizes, per-group totals, skipped reasons,
        finished_at) is added where the summary lacks it, so that the entries may be compacted without losing it."""
        stored = self.run_summary(plan_id)
        base = dict(summary) if summary is not None else stored
        plan = self.plan(plan_id)
        out = dict(base or {})
        if base is None:
            out.update(plan_id=plan_id, state=plan.state.value, warnings=[], snapshots=[], synthesized=True)
        for key, value in self._entry_totals(plan_id, plan.created_at).items():
            out.setdefault(key, value)
        if out != stored:
            self.set_meta(RUN_KEY + plan_id, json.dumps(out))
        return out

    def _entry_totals(self, plan_id: str, created_at: float) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        by_action: Dict[str, Dict[str, int]] = {}
        by_group: Dict[str, Dict[str, Any]] = {}
        reasons: Dict[str, int] = {}
        removed = freed = trashed = kept_alloc = 0
        finished = created_at
        for state, action, group, reason, n, alloc, freed_sum, newest in self._conn().execute(
                'SELECT state, action, group_key, reason, COUNT(*), SUM(alloc), SUM(freed), MAX(updated_at) '
                'FROM plan_entries WHERE plan_id = ? GROUP BY state, action, group_key, reason', (plan_id,)):
            counts[state] = counts.get(state, 0) + n
            finished = max(finished, newest)
            if state == 'skipped':
                reasons[reason] = reasons.get(reason, 0) + n
            if state != 'done':
                continue
            removed += alloc
            freed += freed_sum
            if action == 'trash':
                trashed += alloc
            else:
                kept_alloc += alloc
            slot = by_action.setdefault(action, {'count': 0, 'alloc': 0, 'freed': 0})
            slot['count'] += n
            slot['alloc'] += alloc
            slot['freed'] += freed_sum
            g = by_group.setdefault(group, {'done': 0, 'alloc': 0, 'freed': 0, 'actions': []})
            g['done'] += n
            g['alloc'] += alloc
            g['freed'] += freed_sum
            if action not in g['actions']:
                g['actions'].append(action)
        return {
            'counts': counts, 'total': sum(counts.values()), 'items_done': counts.get('done', 0),
            'items_skipped': counts.get('skipped', 0), 'items_failed': counts.get('failed', 0),
            'items_excluded': counts.get('excluded', 0), 'items_needs_attention': counts.get('needs_attention', 0),
            'items_cancelled': counts.get('cancelled', 0),
            'skipped_reasons': dict(sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))),
            'by_action': by_action, 'by_group': by_group, 'removed': removed, 'removed_alloc': kept_alloc,
            'freed': freed, 'moved_to_trash': trashed, 'finished_at': finished}

    def finished_plan_ids(self) -> List[str]:
        """Plans in done, cancelled or failed whose entries have not been compacted, oldest first."""
        marks = f"SELECT key FROM meta WHERE key LIKE '{COMPACTED_KEY}%'"
        return [r[0] for r in self._conn().execute(
            f"SELECT id FROM plans WHERE state IN {FINISHED_PLAN_STATES!r} AND '{COMPACTED_KEY}' || id NOT IN ({marks}) "
            'ORDER BY created_at, id')]

    def compacted_ids(self) -> List[str]:
        return [r[0] for r in self._conn().execute(
            'SELECT substr(key, ?) FROM meta WHERE key LIKE ?', (len(COMPACTED_KEY) + 1, COMPACTED_KEY + '%'))]

    def compacted_at(self, plan_id: str) -> Optional[float]:
        text = self.meta(COMPACTED_KEY + plan_id)
        return None if text is None else float(text)

    def compact_plan(self, plan_id: str, when: float) -> int:
        """Delete the done and cancelled entries of a finished plan (the skipped, failed, needs_attention and
        excluded ones are few and explain outcomes, so they stay) and mark the plan compacted. Refuses a plan that is
        not finished, and one whose stored summary is not complete: the summary is all that is left of what was
        deleted. Returns the number of entries removed."""
        with self._tx() as c:
            row = c.execute('SELECT state FROM plans WHERE id = ?', (plan_id,)).fetchone()
            if row is None:
                raise _not_found('plan', plan_id)
            if row['state'] not in FINISHED_PLAN_STATES:
                raise StewardError(ErrorCode.INVALID, 'error.plan_state', state=row['state'])
            stored = c.execute('SELECT value FROM meta WHERE key = ?', (RUN_KEY + plan_id,)).fetchone()
            if stored is None or not {'finished_at', 'items_done', 'by_group'} <= set(json.loads(stored[0])):
                raise RuntimeError(f'plan {plan_id} has no complete run summary; compacting it would lose its result')
            removed = c.execute("DELETE FROM plan_entries WHERE plan_id = ? AND state IN ('done', 'cancelled')",
                                (plan_id,)).rowcount
            c.execute('INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)', (COMPACTED_KEY + plan_id, repr(when)))
        self._shrink()
        return int(removed)

    def shrink_if_fragmented(self) -> bool:
        """Give back the free pages when they are more than FRAGMENTED of the file (a file written by an older version
        never reclaimed them). True when it did."""
        conn = self._conn()
        free = conn.execute('PRAGMA freelist_count').fetchone()[0]
        pages = conn.execute('PRAGMA page_count').fetchone()[0]
        if not pages or free <= FRAGMENTED * pages:
            return False
        self._shrink()
        return True

    # archives and advice -------------------------------------------------------------------------

    @staticmethod
    def _archive_dict(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d['manifest'] = json.loads(d['manifest'])
        return d

    def add_archive(self, row: Mapping[str, Any]) -> None:
        """`row` holds the archive columns; manifest is any JSON-safe value, created_at is optional."""
        extra = set(row) - set(_ARCHIVE_COLUMNS) - {'created_at'}
        missing = set(_ARCHIVE_COLUMNS) - set(row)
        if extra or missing:
            raise ValueError(f'archive row: unknown keys {sorted(extra)}, missing keys {sorted(missing)}')
        values = [json.dumps(row[c], sort_keys=True) if c == 'manifest' else _enum_value(row[c])
                  for c in _ARCHIVE_COLUMNS]
        with self._durable() as conn:
            conn.execute(
                f'INSERT INTO archives ({", ".join(_ARCHIVE_COLUMNS)}, created_at) VALUES ({", ".join("?" * 13)})',
                values + [row.get('created_at', self._clock())])

    def archives(self) -> List[Dict[str, Any]]:
        return [self._archive_dict(r) for r in self._conn().execute('SELECT * FROM archives ORDER BY created_at, id')]

    def archive(self, archive_id: str) -> Dict[str, Any]:
        row = self._conn().execute('SELECT * FROM archives WHERE id = ?', (archive_id,)).fetchone()
        if row is None:
            raise _not_found('archive', archive_id)
        return self._archive_dict(row)

    def set_archive_state(self, archive_id: str, state: str) -> None:
        with self._durable() as conn:
            n = conn.execute('UPDATE archives SET state = ? WHERE id = ?', (state, archive_id)).rowcount
        if n != 1:
            raise _not_found('archive', archive_id)

    def add_advice(self, row: Mapping[str, Any]) -> int:
        """`row` holds scan_id, provider, cli_version, payload_digest and result (JSON-safe); created_at is optional."""
        extra = set(row) - set(_ADVICE_COLUMNS) - {'created_at'}
        missing = set(_ADVICE_COLUMNS) - set(row)
        if extra or missing:
            raise ValueError(f'advice row: unknown keys {sorted(extra)}, missing keys {sorted(missing)}')
        values = [json.dumps(row[c], sort_keys=True) if c == 'result' else row[c] for c in _ADVICE_COLUMNS]
        cur = self._conn().execute(
            f'INSERT INTO advice ({", ".join(_ADVICE_COLUMNS)}, created_at) VALUES (?,?,?,?,?,?)',
            values + [row.get('created_at', self._clock())])
        return int(cur.lastrowid)

    def latest_advice(self, scan_id: int) -> Optional[Dict[str, Any]]:
        row = self._conn().execute('SELECT * FROM advice WHERE scan_id = ? ORDER BY id DESC LIMIT 1',
                                   (scan_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d['result'] = json.loads(d['result'])
        return d
