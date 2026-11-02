"""Shared data contract. Every other module imports types from here; change only with a migration note.

Paths inside the tool are *logical*: '~/…', '$TMPDIR/…' or '$CACHEDIR/…'. Only paths.Bases converts
them to absolute paths, so rules, plans and the database never hold arbitrary absolute paths.
"""
from __future__ import annotations

import dataclasses
import enum
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

CATALOG_SCHEMA = 1


class _Str(str, enum.Enum):
    def __str__(self) -> str:  # json.dumps and f-strings emit the bare value
        return self.value


class Tier(_Str):
    JUNK = 'junk'            # rebuildable; can be selected in bulk
    REVIEW = 'review'        # selectable per group/file after reading the effect
    PROTECTED = 'protected'  # browsable, never selectable


class Domain(_Str):
    AGENT = 'agent'
    APP_LIBRARY = 'app-library'
    USER_FILES = 'user-files'
    DEV_SHARED = 'dev-shared'  # report only in 0.4


class Kind(_Str):
    LOGS = 'logs'
    CACHE = 'cache'
    UPDATE_LEFTOVER = 'update-leftover'
    OLD_VERSION = 'old-version'
    SESSION_HISTORY = 'session-history'
    RUNTIME = 'runtime'
    VM_IMAGE = 'vm-image'
    MODEL = 'model'
    DATABASE = 'database'
    CONFIG = 'config'
    CREDENTIAL = 'credential'
    WORK_PRODUCT = 'work-product'
    TEMP = 'temp'
    LEFTOVER = 'leftover'          # data of an app that is no longer installed
    LARGE_FILE = 'large-file'
    IDLE_FILE = 'idle-file'
    INSTALLER = 'installer'
    PROJECT_DEPS = 'project-deps'
    UNKNOWN = 'unknown'


class Unit(_Str):
    FILE = 'file'
    BUNDLE = 'bundle'  # a directory handled atomically (walked fully, one item)


class Action(_Str):
    DELETE = 'delete'
    ARCHIVE = 'archive'
    TRASH = 'trash'


class AgeBasis(_Str):
    CHANGED = 'changed'      # max(mtime, birthtime); ctime is ignored on purpose
    LAST_USED = 'last_used'  # max(Spotlight kMDItemLastUsedDate, changed)


class DecisionState(_Str):
    SELECTED = 'selected'
    UNSELECTED = 'unselected'
    TOO_RECENT = 'too_recent'              # younger than the active filter
    TOO_RECENT_FLOOR = 'too_recent_floor'  # younger than the rule's floor; explicit include cannot override
    EXCLUDED = 'excluded'
    PROTECTED = 'protected'
    NO_GAIN = 'no_gain'                    # hardlinked file: removing this path frees nothing
    UNSUPPORTED_ACTION = 'unsupported_action'  # includes report-only rules (no actions)
    DOMAIN_OFF = 'domain_off'


class PlanState(_Str):
    DRAFT = 'draft'
    RUNNING = 'running'
    PAUSED = 'paused'
    DONE = 'done'
    CANCELLED = 'cancelled'
    FAILED = 'failed'
    EXPIRED = 'expired'
    INVALID = 'invalid'


class EntryState(_Str):
    PENDING = 'pending'
    IN_PROGRESS = 'in_progress'
    DONE = 'done'
    SKIPPED = 'skipped'
    FAILED = 'failed'
    EXCLUDED = 'excluded'
    CANCELLED = 'cancelled'
    NEEDS_ATTENTION = 'needs_attention'  # staged item could not be renamed back; shown to the user


class ErrorCode(_Str):
    NO_SCAN = 'NO_SCAN'
    BUSY = 'BUSY'
    NOT_FOUND = 'NOT_FOUND'
    INVALID = 'INVALID'
    DIGEST_MISMATCH = 'DIGEST_MISMATCH'      # plan would differ from what the UI showed (HTTP 409)
    EMPTY_PLAN = 'EMPTY_PLAN'                # nothing qualifies (HTTP 422, carries breakdown)
    PLAN_EXPIRED = 'PLAN_EXPIRED'
    RULES_CHANGED = 'RULES_CHANGED'          # catalog/facts/settings hash differs from the plan
    REVISION_MISMATCH = 'REVISION_MISMATCH'  # plan was edited (exclusions) since the client looked
    IN_PROGRESS = 'IN_PROGRESS'
    UNAVAILABLE = 'UNAVAILABLE'              # e.g. advisor CLI missing, /usr/bin/trash missing
    DATA_DIR_RECREATED = 'DATA_DIR_RECREATED'  # the data dir was deleted while running; it is back, empty (HTTP 409)


class StewardError(Exception):
    """Expected, user-facing failure. `message_key` is an i18n key; `details` is JSON-safe."""

    def __init__(self, code: ErrorCode, message_key: str, **details: Any):
        super().__init__(f'{code}: {message_key} {details}' if details else f'{code}: {message_key}')
        self.code = code
        self.message_key = message_key
        self.details = details


@dataclass(frozen=True)
class Evidence:
    key: str                                   # i18n key, e.g. 'evidence.not_current_version'
    args: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Rule:
    """Declarative catalog entry. Matching: the item's logical path matches one of `patterns`
    ('*' within a segment, '**' any number of segments), basename passes include/exclude, size >= min_size.
    Rules are tried by descending priority; the first that matches (and is not skipped by its detector) wins."""
    id: str
    provider: str                              # i18n 'provider.<provider>'
    domain: Domain
    tier: Tier
    kind: Kind
    patterns: Tuple[str, ...]
    unit: Unit = Unit.FILE
    name_include: Tuple[str, ...] = ()         # basename fnmatch globs; empty = any
    name_exclude: Tuple[str, ...] = ()
    min_size: int = 0                          # logical bytes; file units only
    actions: Tuple[Action, ...] = ()           # empty = report only
    default_action: Optional[Action] = None
    min_age_floor_days: float = 0.0            # never bypassed, not even by explicit include
    default_min_age_days: Optional[float] = None  # pre-filled group filter (e.g. idle files 180)
    age_basis: AgeBasis = AgeBasis.CHANGED
    quit_apps: Tuple[str, ...] = ()            # bundle ids that must not be running during execution
    group_depth: Optional[int] = None          # leading logical segments forming the group; None = one group per rule
    priority: int = 100                        # protections 1000; specific 100-500; generic fallbacks < 100
    detector: Optional[str] = None             # detectors registry name; may refine or skip
    hint: Optional[str] = None                 # native command for report-only rules, shown verbatim
    # Report-only rules without a native command: actions offered for one listed folder at a time from the tools
    # table. The selection never offers them, so no bulk pick can reach a shared cache.
    manual_actions: Tuple[Action, ...] = ()

    @property
    def label_key(self) -> str:
        return f'rule.{self.id}.label'

    @property
    def effect_key(self) -> str:
        return f'rule.{self.id}.effect'


@dataclass(frozen=True)
class StatInfo:
    dev: int
    ino: int
    mode: int
    nlink: int
    uid: int
    size: int
    blocks: int
    mtime_ns: int
    birth_ns: int
    flags: int

    @property
    def alloc(self) -> int:
        return self.blocks * 512

    @property
    def changed_ns(self) -> int:
        return max(self.mtime_ns, self.birth_ns)

    @classmethod
    def from_stat(cls, st: os.stat_result) -> 'StatInfo':
        birth = getattr(st, 'st_birthtime', 0.0)
        return cls(dev=st.st_dev, ino=st.st_ino, mode=st.st_mode, nlink=st.st_nlink, uid=st.st_uid,
                   size=st.st_size, blocks=getattr(st, 'st_blocks', 0), mtime_ns=st.st_mtime_ns,
                   # st_birthtime_ns does not exist on macOS Pythons; round to microseconds so the
                   # value is identical every time the same inode is stat'ed.
                   birth_ns=int(round(birth * 1_000_000)) * 1000,
                   flags=getattr(st, 'st_flags', 0))


@dataclass(frozen=True)
class BundleSummary:
    files: int
    size: int
    alloc: int
    reclaim: int          # alloc of files with nlink == 1 (hardlinked files free nothing)
    newest_ns: int        # newest changed_ns over all descendants, directories included
    taint: str = ''       # non-empty reason => bundle is protected (sqlite, .git, credential, mount, foreign uid, dataless, immutable)


@dataclass(frozen=True)
class Refinement:
    """Detector verdict for a candidate rule match."""
    skip: bool = False                 # rule does not apply; classifier continues with the next rule
    tier: Optional[Tier] = None        # override (usually PROTECTED, e.g. current version)
    group_key: Optional[str] = None    # override (e.g. Codex duplicate pairs)
    evidence: Tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class Match:
    rule_id: str
    tier: Tier
    domain: Domain
    kind: Kind
    unit: Unit
    group_key: str
    evidence: Tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class Item:
    scan_id: int
    key: str               # sha1(logical path)[:20]
    path: str              # logical path
    rule_id: str
    group_key: str
    tier: Tier
    domain: Domain
    kind: Kind
    unit: Unit
    dev: int
    ino: int
    size: int
    alloc: int
    reclaim: int
    files: int
    mtime_ns: int
    birth_ns: int
    newest_ns: int         # file: changed_ns; bundle: newest descendant
    last_used_ns: Optional[int]
    nlink: int
    uid: int
    flags: int
    taint: str = ''
    evidence: Tuple[Evidence, ...] = ()

    def fingerprint(self) -> Dict[str, int]:
        if self.unit == Unit.BUNDLE:
            return dict(dev=self.dev, ino=self.ino, files=self.files, alloc=self.alloc, newest_ns=self.newest_ns)
        return dict(dev=self.dev, ino=self.ino, size=self.size, mtime_ns=self.mtime_ns,
                    birth_ns=self.birth_ns, nlink=self.nlink)


@dataclass(frozen=True)
class Group:
    scan_id: int
    key: str
    rule_id: str
    provider: str
    domain: Domain
    tier: Tier
    kind: Kind
    unit: Unit
    label_args: Mapping[str, Any]   # e.g. {'path': '~/.claude/projects/x'} for depth-grouped rules
    items: int
    alloc: int
    reclaim: int
    newest_ns: int
    oldest_ns: int
    actions: Tuple[Action, ...]
    default_action: Optional[Action]
    min_age_floor_days: float
    default_min_age_days: Optional[float]
    age_basis: AgeBasis
    hint: Optional[str] = None
    evidence: Tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class GroupSel:
    min_age_days: Optional[float] = None   # None => max(group.default_min_age_days, spec.filter_min_age_days)
    action: Optional[Action] = None        # None => rule default


@dataclass(frozen=True)
class Spec:
    """User's selection intent. Server-owned; the browser sends ops, never a computed list."""
    scan_id: int
    version: int = 0
    filter_min_age_days: float = 0.0
    groups: Mapping[str, GroupSel] = field(default_factory=dict)
    include: Mapping[str, Optional[Action]] = field(default_factory=dict)  # item_key -> action override
    exclude: frozenset = frozenset()                                       # item keys; disjoint from include

    def to_json(self) -> Dict[str, Any]:
        return dict(scan_id=self.scan_id, version=self.version, filter_min_age_days=self.filter_min_age_days,
                    groups={k: dict(min_age_days=v.min_age_days, action=v.action and v.action.value)
                            for k, v in sorted(self.groups.items())},
                    include={k: v and v.value for k, v in sorted(self.include.items())},
                    exclude=sorted(self.exclude))

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> 'Spec':
        return cls(scan_id=int(d['scan_id']), version=int(d.get('version', 0)),
                   filter_min_age_days=float(d.get('filter_min_age_days', 0)),
                   groups={k: GroupSel(v.get('min_age_days'), Action(v['action']) if v.get('action') else None)
                           for k, v in d.get('groups', {}).items()},
                   include={k: Action(v) if v else None for k, v in d.get('include', {}).items()},
                   exclude=frozenset(d.get('exclude', ())))


@dataclass(frozen=True)
class Decision:
    state: DecisionState
    reason_key: str
    reason_args: Mapping[str, Any] = field(default_factory=dict)
    action: Optional[Action] = None
    via: Optional[str] = None            # 'group' | 'explicit' when selected
    age_days: Optional[float] = None
    threshold_days: Optional[float] = None


@dataclass(frozen=True)
class Plan:
    id: str
    created_at: float
    expires_at: float
    scan_id: int
    catalog_hash: str
    facts_hash: str
    settings_hash: str
    spec: Mapping[str, Any]
    revision: int
    entries_digest: str
    state: PlanState
    totals: Mapping[str, Any]
    error: str = ''


@dataclass(frozen=True)
class Entry:
    plan_id: str
    seq: int
    item_key: str
    path: str
    unit: Unit
    action: Action
    rule_id: str
    group_key: str
    threshold_days: float
    alloc: int
    reclaim: int
    fp: Mapping[str, int]
    state: EntryState = EntryState.PENDING
    reason: str = ''
    freed: int = 0
    archive_id: Optional[str] = None


@dataclass(frozen=True)
class Outcome:
    state: EntryState     # DONE / SKIPPED / FAILED / NEEDS_ATTENTION (or PENDING after rollback)
    reason: str = ''      # i18n key or short code, e.g. 'skip.changed', 'skip.open:Claude(123)'
    freed: int = 0
    archive_id: Optional[str] = None


def asdict(obj: Any) -> Dict[str, Any]:
    """JSON-safe dict for any contract dataclass (enums become their values)."""
    def conv(v: Any) -> Any:
        if isinstance(v, enum.Enum):
            return v.value
        if dataclasses.is_dataclass(v):
            return {f.name: conv(getattr(v, f.name)) for f in dataclasses.fields(v)}
        if isinstance(v, Mapping):
            return {str(k): conv(x) for k, x in v.items()}
        if isinstance(v, (list, tuple, set, frozenset)):
            items = [conv(x) for x in v]
            return sorted(items) if isinstance(v, (set, frozenset)) else items
        return v
    return conv(obj)
