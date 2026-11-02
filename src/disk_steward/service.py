"""The App facade: the only API that cli/ and portal/ use."""
from __future__ import annotations

import collections
import concurrent.futures
import copy
import json
import math
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import actions, planner, queries, report, scanner
from . import insights as insights_mod
from . import native as native_mod
from . import settings as settings_mod
from .audit import Audit
from .catalog import Catalog, is_report_only, load_catalog
from .classify import Classifier, split_group_key
from .detectors import DetectorContext, compute_facts
from .detectors.codex_bloat import diagnose
from .events import EventBus
from .executor import Control, Executor
from .i18n import LOCALES, detect_locale
from .i18n import t as translate
from .locking import OperationLock
from .macos import MacOS, RealMacOS
from .model import (Action, Domain, EntryState, ErrorCode, PlanState, Spec, StatInfo, StewardError, Tier, Unit,
                    asdict)
from .paths import Bases, default_data_dir
from .selection import (CODEX_SESSION_RULES, Resolution, _action_totals, apply_ops, migrate_spec, qualify_groups,
                        resolve)
from .settings import Settings
from .store import Store

CACHE_SECONDS = 5          # how long a Resolution is reused for paging through items
SEEN_DIGESTS = 32          # digests remembered per App, to rebuild what a client saw when its plan is refused
SYSTEM_APPLICATIONS = Path('/Applications')   # tests point it at a temp folder
QUICK_SESSION = 'quick'   # the "safe to clean" selection; the user's own selection ('default') is never touched by it
FINISHED_SHOWN = 5
DAY_SECONDS = 86400.0
COMPACT_AFTER_DAYS = 7   # a finished plan keeps its done and cancelled entries this long (core.json plan.compacted_note)
# Settings outside settings_hash(): changing them cannot affect a plan that is running, so a run does not block them.
RUN_SAFE_SETTINGS = frozenset(('locale', 'advisor_enabled', 'advisor_provider', 'default_min_age_days'))


def _invalid(field: str) -> StewardError:
    return StewardError(ErrorCode.INVALID, 'error.invalid_op', field=field)


def _notes_key(session: str) -> str:
    return f'spec_notes:{session}'


class App:
    def __init__(self, home: Optional[Path] = None, data_dir: Optional[Path] = None, mac: Optional[MacOS] = None,
                 clock: Callable[[], float] = time.time, locale: Optional[str] = None,
                 settings: Optional[Settings] = None, bases: Optional[Bases] = None,
                 hooks: Optional[Mapping[str, Callable[[Any], None]]] = None):
        """`bases` replaces the detected home, $TMPDIR and $CACHEDIR (tests pass FakeHome's). `hooks` go to every
        Executor (fault-injection tests only)."""
        self.clock = clock
        self.bases = bases if bases is not None else Bases.detect(home)
        self.data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.mac: MacOS = mac if mac is not None else RealMacOS(self.bases.home)
        self._settings = settings if settings is not None else settings_mod.load(self.data_dir)
        if locale is not None and locale not in LOCALES:
            raise StewardError(ErrorCode.INVALID, 'error.invalid_locale', locale=str(locale))
        self.locale: str = locale or self._settings.locale or detect_locale()
        self.store = Store(self.data_dir / 'steward.db', clock)
        self.store.on_recreated = self._data_dir_recreated
        self.audit = Audit(self.data_dir, clock)
        self.lock = OperationLock(self.data_dir)
        self.events = EventBus(clock=clock)
        self._catalog: Optional[Catalog] = None
        self._cache: Dict[str, Tuple[Tuple[int, int, int], Resolution]] = {}
        self._qualify: Dict[str, Tuple[Resolution, Dict[str, Any]]] = {}   # session -> (its Resolution, figures)
        self._seen: 'collections.OrderedDict[Tuple[str, str], float]' = collections.OrderedDict()
        self._cache_lock = threading.Lock()
        self._spec_lock = threading.Lock()   # specs are read-modify-write; two portal requests must not interleave
        self._hooks = dict(hooks or {})
        self._job_lock = threading.Lock()
        self._active = False                 # a run (including its rescan) is in progress
        self._control: Optional[Control] = None
        self._job: Dict[str, Any] = _idle_job()
        self.recovery: Optional[Dict[str, Any]] = None
        self._app_sizes: Optional[Dict[str, Any]] = None
        self._quick_built: Optional[Tuple[Any, ...]] = None   # (scan id, days, areas, spec version) of the last quick()
        self._native = native_mod.Probe(clock)
        self._recover()
        try:
            self.compact_plans()
            self.store.shrink_if_fragmented()
        except BaseException:
            self.store.close()
            raise

    def close(self) -> None:
        self.store.close()

    def check_data_dir(self) -> bool:
        """Call where an operation starts (a portal request, a CLI command). If the data dir or the database was
        deleted since the App opened it, make both again (see _data_dir_recreated) and return True. Work that is
        already running does not get here: its next store call raises DATA_DIR_RECREATED."""
        return self.store.check()

    def _data_dir_recreated(self) -> None:
        """The store made the data dir and a fresh database again. What lived only in files is gone (scans, plans,
        history, audit log, archives, staging); the settings and the lock exist in memory or as a name, so they
        are written back."""
        self.lock.ensure_file()
        if self._settings != Settings():
            settings_mod.save(self.data_dir, self._settings)
        self.audit.write({'event': 'data_dir.recreated'})
        self.events.publish('data_dir.recreated', message_key='error.DATA_DIR_RECREATED')

    @property
    def advisor(self) -> Any:
        """The optional AI advisor, built on first use so a run that never advises does not import it."""
        with self._cache_lock:
            if getattr(self, '_advisor', None) is None:
                from .advisor.service import AdvisorService
                self._advisor = AdvisorService(self)
            return self._advisor

    # settings --------------------------------------------------------------------------------------------

    @property
    def settings(self) -> Settings:
        return self._settings

    def update_settings(self, patch: Mapping[str, Any]) -> Settings:
        with self._job_lock:   # a run, paused or not, holds the settings it was planned under
            if self._active and not set(patch) <= RUN_SAFE_SETTINGS:
                raise StewardError(ErrorCode.BUSY, 'error.busy')
            new = settings_mod.replace(self._settings, **patch)
            settings_mod.save(self.data_dir, new)
            self._settings = new
            self._catalog = None   # rule thresholds come from settings
            with self._cache_lock:
                self._cache.clear()
                self._qualify.clear()
            if 'locale' in patch:
                self.locale = new.locale or detect_locale()
            return new

    def _load_catalog(self) -> Catalog:
        if self._catalog is None:
            self._catalog = load_catalog(self._settings)
        return self._catalog

    # scanning --------------------------------------------------------------------------------------------

    def scan(self, domains: Optional[Sequence[str]] = None,
             progress: Optional[Callable[[Dict[str, Any]], None]] = None,
             cancel: Optional[Callable[[], bool]] = None) -> int:
        settings = self._settings
        wanted = tuple(domains) if domains else settings.domains
        if not set(wanted) <= {d.value for d in Domain}:
            raise StewardError(ErrorCode.INVALID, 'error.invalid_setting', field='domains')

        def on_progress(event: Dict[str, Any]) -> None:
            self.events.publish('scan.progress', **event)
            if progress is not None:
                progress(event)

        with self.lock:
            catalog = self._load_catalog()
            facts, _ = compute_facts(DetectorContext(self.bases, self.mac, settings), catalog)
            classifier = Classifier(catalog, facts, self.bases, settings)
            # The previous scan's rows are what the selection notes measure "before" against, so they go only
            # after the selections moved to the new scan.
            scan_id = scanner.scan(self.store, classifier, catalog, self.bases, self.mac, settings, wanted,
                                   on_progress, cancel, prune=False)
            try:
                migrated = self._migrate_specs(scan_id)
            finally:
                self.store.prune_scans()
            self.store.supersede_drafts(scan_id)   # a draft was made from rows that no longer exist; it must not run
            with self._cache_lock:
                self._cache.clear()
                self._qualify.clear()
        self._retain()
        info = self.store.scan(scan_id)
        problems = sum(n for code, n in self._error_counts(scan_id).items() if code != report.PRIVACY_CODE)
        self.events.publish('scan.done', scan_id=scan_id, attributed=info['attributed'], errors=problems,
                            migrated=migrated)
        return scan_id

    def _error_counts(self, scan_id: int) -> Dict[str, int]:
        return dict(collections.Counter(e['error'] for e in self.store.scan_errors(scan_id)))

    def _migrate_specs(self, scan_id: int) -> Dict[str, Dict[str, Any]]:
        migrated: Dict[str, Dict[str, Any]] = {}
        with self._spec_lock:
            for session in queries.spec_sessions(self.store):
                spec = self.store.load_spec(session)
                if spec is None or spec.scan_id == scan_id:
                    continue
                migrated[session] = self._carry_over(session, spec, scan_id)[1]
        return migrated

    def _carry_over(self, session: str, spec: Spec, scan_id: int) -> Tuple[Spec, Dict[str, Any]]:
        """Move a session's selection to a newer scan and keep the notes about what changed, for every client
        (CLI, portal, other processes) until the user next edits the selection."""
        new, notes = migrate_spec(self.store, spec, scan_id, now=self.clock(), enabled_domains=self._settings.domains,
                                  trash_available=self.mac.trash_available(), session=session)
        self.store.save_spec(session, new)
        self.store.set_meta(_notes_key(session), json.dumps(notes, sort_keys=True))
        return new, notes

    def spec_notes(self, session: str = 'default') -> Optional[Dict[str, Any]]:
        """What the last carry-over of this session's selection dropped or changed; None once the user edited it."""
        text = self.store.meta(_notes_key(session))
        return None if text is None else json.loads(text)

    def latest_scan(self) -> Optional[Dict[str, Any]]:
        return self.store.latest_scan()

    def _latest(self) -> Dict[str, Any]:
        latest = self.store.latest_scan()
        if latest is None:
            raise StewardError(ErrorCode.NO_SCAN, 'error.NO_SCAN')
        return latest

    def overview(self) -> Dict[str, Any]:
        latest = self._latest()
        return report.build_report(self.store, latest['id'], self._load_catalog(), self._settings, self.clock(),
                                   mac=self.mac, snapshots=self.mac.snapshots(), bases=self.bases)

    def app_sizes(self) -> Dict[str, Any]:
        """Allocated size of every top-level folder or .app in /Applications and ~/Applications, biggest first, as
        {'measured_at', 'total', 'entries': [{'path', 'bytes' | 'error'}]}. `du` over big apps takes minutes, so it
        runs once per App. A path under /Applications stays absolute, since no logical prefix covers it."""
        if self._app_sizes is None:
            found: List[Tuple[str, str]] = []   # (absolute path, path as shown)
            for folder in (SYSTEM_APPLICATIONS, self.bases.home / 'Applications'):
                try:
                    children = sorted(folder.iterdir())
                except FileNotFoundError:
                    continue   # ~/Applications exists only for users who keep apps there
                for child in children:
                    if child.is_dir() and not child.is_symlink():
                        found.append((str(child), str(child) if folder is SYSTEM_APPLICATIONS
                                      else self.bases.to_logical(child)))
            sizes = self.mac.app_sizes([path for path, _ in found])
            entries = [dict(sizes[path], path=shown) for path, shown in found]
            entries.sort(key=lambda e: ('error' in e, -e.get('bytes', 0), e['path']))
            self._app_sizes = {'measured_at': self.clock(), 'total': sum(e.get('bytes', 0) for e in entries),
                               'entries': entries}
        return copy.deepcopy(self._app_sizes)

    # selection -------------------------------------------------------------------------------------------

    def spec(self, session: str = 'default') -> Spec:
        with self._spec_lock:
            return self._spec_locked(session)

    def _spec_locked(self, session: str) -> Spec:
        latest = self._latest()
        spec = self.store.load_spec(session)
        if spec is None:
            return Spec(scan_id=latest['id'], filter_min_age_days=self._settings.default_min_age_days)
        if spec.scan_id != latest['id']:   # saved by a scan that ran in another process
            spec = self._carry_over(session, spec, latest['id'])[0]
        return spec

    def _resolve(self, session: str, spec: Spec) -> Resolution:
        now = self.clock()
        key = (spec.scan_id, spec.version, int(now) // CACHE_SECONDS)
        with self._cache_lock:
            hit = self._cache.get(session)
            if hit is not None and hit[0] == key:
                return hit[1]
        res = resolve(self.store, spec, now, self._settings, self.mac)
        with self._cache_lock:
            self._cache[session] = (key, res)
            self._seen[(session, res.digest)] = res.now
            while len(self._seen) > SEEN_DIGESTS:
                self._seen.popitem(last=False)
        return res

    def _qualify_for(self, session: str, spec: Spec, res: Resolution) -> Dict[str, Any]:
        """selection.qualify_groups() for `res`, kept until the session resolves again. It is tied to `res` itself
        so that its figures and res.summary always come from the same clock."""
        with self._cache_lock:
            hit = self._qualify.get(session)
        if hit is not None and hit[0] is res:
            return hit[1]
        out = qualify_groups(self.store, spec, res, self._settings.domains, self.mac.trash_available())
        with self._cache_lock:
            self._qualify[session] = (res, out)
        return out

    def apply_ops(self, ops: Sequence[Mapping[str, Any]], session: str = 'default',
                  qualify: bool = False) -> Dict[str, Any]:
        """`qualify` adds the per-group figures of selection.qualify_groups() as 'qualify' (the portal's Browse)."""
        with self._spec_lock:
            new = apply_ops(self._spec_locked(session), ops, self.store, self._settings.domains)
            self.store.save_spec(session, new)
            self.store.delete_meta(_notes_key(session))   # the user has seen the selection and is editing it
        res = self._resolve(session, new)
        out = {'spec': new.to_json(), 'scan_id': new.scan_id, 'summary': res.summary, 'digest': res.digest,
               'resolved_at': res.now}
        if qualify:
            out['qualify'] = self._qualify_for(session, new, res)
        return out

    def groups(self, domain: Optional[str] = None, tier: Optional[str] = None,
               session: str = 'default') -> List[Dict[str, Any]]:
        if domain is not None and domain not in {d.value for d in Domain}:
            raise _invalid('domain')
        if tier is not None and tier not in {t.value for t in Tier}:
            raise _invalid('tier')
        spec = self.spec(session)
        rows = []
        with self.store.read_snapshot():   # one state of the database even if a scan finishes and prunes meanwhile
            self._require_current(spec, session)
            res = self._resolve(session, spec)
            for g in self.store.groups(spec.scan_id, domain=domain, tier=tier):
                row = asdict(g)
                sel = spec.groups.get(g.key)
                row['selector'] = None if sel is None else asdict(sel)
                row['selection'] = res.summary['groups'][g.key]
                row['label'] = report.group_label(g, self.locale)
                row['effect'] = report.group_effect(g, self.locale)
                rows.append(row)
        return rows

    def _require_current(self, spec: Spec, session: str) -> None:
        """error.spec_stale when a scan finished after `spec` was read: the rows it names may be gone."""
        latest = self.store.latest_scan()
        if latest is None or latest['id'] != spec.scan_id:
            raise StewardError(ErrorCode.INVALID, 'error.spec_stale', from_scan=spec.scan_id,
                               to_scan=None if latest is None else latest['id'], notes=self.spec_notes(session))

    def items(self, group: Optional[str] = None, order: str = 'alloc', offset: int = 0, limit: Optional[int] = 100,
              query: str = '', session: str = 'default', keys: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        spec = self.spec(session)
        rows = []
        with self.store.read_snapshot():   # the page, its decisions and its total come from one state of the database
            self._require_current(spec, session)
            if group is not None:
                self.store.group(spec.scan_id, group)   # NOT_FOUND for a mistyped key instead of an empty page
            res = self._resolve(session, spec)
            found = self.store.items(spec.scan_id, group_key=group, keys=keys, order=order, offset=offset, limit=limit,
                                     query=query)
            for item in found:
                row = asdict(item)
                row['decision'] = asdict(res.decisions[item.key])
                rows.append(row)
            total = self.store.count_items(spec.scan_id, group_key=group, keys=keys, query=query)
        return {'total': total, 'rows': rows}

    # plans -----------------------------------------------------------------------------------------------

    def create_plan(self, session: str, expected: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(expected, Mapping):
            raise _invalid('expected')
        spec = self.spec(session)
        expected = dict(expected)
        digest = expected.get('digest')
        if 'resolved_at' not in expected and isinstance(digest, str):
            seen = self._seen.get((session, digest))
            if seen is not None:
                expected['resolved_at'] = seen
        plan = planner.create_plan(self.store, spec, self.clock(), expected, self._settings, self.mac,
                                   catalog=self._load_catalog(), notes=self.spec_notes(session))
        return self.plan(plan.id)

    def plan(self, plan_id: str) -> Dict[str, Any]:
        plan = self.store.plan(plan_id)
        row = asdict(plan)
        by_state = {s.value: self.store.count_entries(plan_id, s) for s in EntryState}
        compacted = self.store.compacted_at(plan_id)
        if compacted is not None:   # the rows that were removed are counted from the summary
            summary = self._summary_of(plan_id)
            by_state['done'] += summary['items_done']
            by_state['cancelled'] += summary['items_cancelled']
        row['compacted_at'] = compacted
        row['entries'] = {'total': sum(by_state.values()), 'by_state': {s: n for s, n in by_state.items() if n}}
        return row

    def _summary_of(self, plan_id: str) -> Dict[str, Any]:
        summary = self.store.run_summary(plan_id)
        if summary is None:   # compact_plan refuses a plan without one
            raise RuntimeError(f'plan {plan_id} is compacted but has no run summary')
        return summary

    def entries(self, plan_id: str, state: Optional[str] = None, offset: int = 0,
                limit: Optional[int] = 100, group: Optional[str] = None) -> Dict[str, Any]:
        self.store.plan(plan_id)   # NOT_FOUND for an unknown plan
        if state is not None and state not in {s.value for s in EntryState}:
            raise _invalid('state')
        return {'total': self.store.count_entries(plan_id, state, group),
                'rows': [asdict(e) for e in self.store.entries(plan_id, state=state, offset=offset, limit=limit,
                                                                group=group)]}

    def exclude(self, plan_id: str, seqs: Sequence[int], revision: int) -> int:
        return self.store.exclude(plan_id, seqs, revision)

    def include(self, plan_id: str, seqs: Sequence[int], revision: int) -> int:
        """Put excluded entries of a draft plan back. Returns the new revision."""
        return self.store.unexclude(plan_id, seqs, revision)

    def exclude_group(self, plan_id: str, group: str, revision: int) -> int:
        """Exclude every pending entry of one group."""
        self.store.plan(plan_id)
        return self.store.exclude(plan_id, queries.plan_seqs(self.store, plan_id, group, 'pending'), revision)

    def include_group(self, plan_id: str, group: str, revision: int) -> int:
        """Put every excluded entry of one group of a draft plan back."""
        self.store.plan(plan_id)
        return self.store.unexclude(plan_id, queries.plan_seqs(self.store, plan_id, group, 'excluded'), revision)

    def plan_groups(self, plan_id: str) -> List[Dict[str, Any]]:
        """A plan's entries by group: per group the entries in each state, and what is still pending."""
        self.store.plan(plan_id)
        groups: 'collections.OrderedDict[str, Dict[str, Any]]' = collections.OrderedDict()
        for key, rule_id, state, action, n, alloc, reclaim in queries.plan_group_totals(self.store, plan_id):
            g = groups.setdefault(key, {'group': key, 'rule_id': rule_id, 'entries': 0, 'alloc': 0, 'by_state': {},
                                        'pending': {'count': 0, 'alloc': 0, 'reclaim': 0}, 'actions': []})
            g['entries'] += n
            g['alloc'] += alloc
            g['by_state'][state] = g['by_state'].get(state, 0) + n
            if action not in g['actions']:
                g['actions'].append(action)
            if state == EntryState.PENDING.value:
                g['pending']['count'] += n
                g['pending']['alloc'] += alloc
                g['pending']['reclaim'] += reclaim
        if self.store.compacted_at(plan_id) is not None:   # the done entries that were removed, from the summary
            for key, done in self._summary_of(plan_id)['by_group'].items():
                g = groups.setdefault(key, {'group': key, 'rule_id': split_group_key(key)[0], 'entries': 0, 'alloc': 0,
                                            'by_state': {}, 'pending': {'count': 0, 'alloc': 0, 'reclaim': 0},
                                            'actions': []})
                g['entries'] += done['done']
                g['alloc'] += done['alloc']
                g['by_state']['done'] = g['by_state'].get('done', 0) + done['done']
                g['actions'] += [a for a in done['actions'] if a not in g['actions']]
        return sorted(groups.values(), key=lambda g: (-g['alloc'], g['group']))

    def discard_plan(self, plan_id: str) -> None:
        """Give up a draft: it becomes invalid and its entries go. Only a draft can be discarded."""
        plan = self.store.plan(plan_id)
        if plan.state != PlanState.DRAFT:
            raise StewardError(ErrorCode.INVALID, 'error.plan_state', state=plan.state.value)
        self.store.invalidate_drafts([plan_id], 'discarded')   # shrinks the file itself

    def plan_result(self, plan_id: str) -> Optional[Dict[str, Any]]:
        """What a run of this plan reported (the executor's summary plus the rescan it triggered); None while it has
        not finished a run."""
        self.store.plan(plan_id)
        summary = self.store.run_summary(plan_id)
        return None if summary is None or summary.get('synthesized') else summary   # a built one has no run figures

    def plans(self) -> Dict[str, Any]:
        """For the cleanup tab: the plan that is running or paused, else the newest draft of the latest scan that has
        not expired, and the last few finished plans with their results. Drafts of older scans are gone already."""
        latest = self.store.latest_scan()
        now = self.clock()
        live = self.store.plans_in_state(PlanState.RUNNING) + self.store.plans_in_state(PlanState.PAUSED)
        drafts = [p for p in self.store.plans_in_state(PlanState.DRAFT)
                  if latest is not None and p.scan_id == latest['id'] and now < p.expires_at]
        done: List[Any] = []
        for state in (PlanState.DONE, PlanState.CANCELLED, PlanState.FAILED):
            done += self.store.plans_in_state(state)
        done.sort(key=lambda p: p.created_at, reverse=True)

        def row(plan: Any, pending: bool = False) -> Dict[str, Any]:
            out = dict(self.plan(plan.id), result=self.plan_result(plan.id))
            if pending:
                out['pending'] = self.pending_totals(plan.id)   # what a run would still handle
            return out
        return {'active': row(live[-1], True) if live else None, 'draft': row(drafts[-1], True) if drafts else None,
                'finished': [row(p) for p in done[:FINISHED_SHOWN]]}

    def drift(self, plan_id: str) -> Dict[str, int]:
        """Pending entries whose item is gone or is no longer what the scan saw. A run leaves them alone; this says how
        many (files are compared with the fingerprint, folders by identity: a full re-walk is the run's job)."""
        count = alloc = 0
        for e in self.store.entries(plan_id, EntryState.PENDING):
            try:
                st = StatInfo.from_stat(os.lstat(self.bases.to_abs(e.path)))
            except FileNotFoundError:
                same = False
            else:
                same = actions.file_matches(st, e.fp) if e.unit == Unit.FILE else actions.dir_matches(st, e.fp)
            if not same:
                count += 1
                alloc += e.alloc
        return {'count': count, 'alloc': alloc}

    def selection(self, session: str = 'default', qualify: bool = False) -> Dict[str, Any]:
        """The session's selection in the shape apply_ops() returns it, without changing anything."""
        spec = self.spec(session)
        res = self._resolve(session, spec)
        out = {'spec': spec.to_json(), 'scan_id': spec.scan_id, 'summary': res.summary, 'digest': res.digest,
               'resolved_at': res.now}
        if qualify:
            out['qualify'] = self._qualify_for(session, spec, res)
        return out

    # the "safe to clean" selection and Browse's filters -----------------------------------------------------

    def _quick_days(self, days: Optional[float]) -> Any:
        if days is None:
            return self._settings.default_min_age_days
        if isinstance(days, bool) or not isinstance(days, (int, float)) or not math.isfinite(days) or days < 0:
            raise _invalid('days')
        return int(days) if float(days).is_integer() else days

    def quick(self, days: Optional[float] = None) -> Dict[str, Any]:
        """Every junk group of the enabled areas, judged with an age filter of `days` (default: the settings'): what
        `plan --select junk --older-than` picks. It lives in its own session, so the selection the user builds in
        Browse stays as it is. Asking again for the same thing changes nothing (the digest stays valid)."""
        days = self._quick_days(days)
        latest = self._latest()
        key = (latest['id'], days, tuple(self._settings.domains))
        built = self._quick_built
        if built is not None and built[:3] == key and self.spec(QUICK_SESSION).version == built[3]:
            return self.selection(QUICK_SESSION)
        out = self.apply_ops([{'op': 'clear'}, {'op': 'set_filter', 'min_age_days': days},
                              {'op': 'select_tier', 'tier': 'junk', 'on': True}], QUICK_SESSION)
        self._quick_built = key + (out['spec']['version'],)
        return out

    def quick_plan(self, days: Optional[float], expected: Mapping[str, Any]) -> Dict[str, Any]:
        """The plan for exactly what quick(days) showed: `expected` carries its digest, so a selection that moved on
        (another tab, a scan, the clock) is refused with DIGEST_MISMATCH instead of planning something else."""
        days = self._quick_days(days)
        if self.spec(QUICK_SESSION).filter_min_age_days != days:
            self.quick(days)   # not what the caller saw: its spec_version no longer matches
        return self.create_plan(QUICK_SESSION, expected)

    def browse_scope(self, flt: Mapping[str, str], days: Optional[float] = None) -> Dict[str, Any]:
        """The items a Browse filter names, counted per group: {'groups': {key: {'items', 'alloc'}}, 'items', 'alloc',
        'days'}. `flt` may hold domain, tier, report_only ('1' groups without a cleanup action, '0' the others),
        state (decision states joined by '|', or 'other': junk with a cleanup action that is neither offered nor
        too recent) and reason (a protected reason). Properties of the scan decide all but `state`, which is the
        decision under quick(days): the numbers of the overview."""
        days = self._quick_days(days)
        states = set(flt['state'].split('|')) if flt.get('state') else None
        reason = flt.get('reason') or None
        spec = None
        if states is not None:
            self.quick(days)
            spec = self.spec(QUICK_SESSION)
        with self.store.read_snapshot():
            latest = self._latest()
            if spec is not None:
                self._require_current(spec, QUICK_SESSION)
            groups = self._scope_groups(latest['id'], flt)
            tally: Dict[str, Dict[str, int]] = {}
            if states is None and reason is None:
                tally = {k: {'items': g.items, 'alloc': g.alloc} for k, g in groups.items()}
            elif states is None:
                taint = queries.taint_split(self.store, latest['id'])
                for key, g in groups.items():
                    for tainted in (False, True):
                        n, alloc = taint.get((key, tainted), (0, 0))
                        if n and g.tier == Tier.PROTECTED and ('tainted' if tainted else report._protected_reason(g)) == reason:
                            tally[key] = {'items': n, 'alloc': alloc}
            else:
                decisions = self._resolve(QUICK_SESSION, spec).decisions   # type: ignore[arg-type]
                for row in queries.item_rows(self.store, latest['id']):
                    g = groups.get(row[1])
                    if g is not None and self._row_matches(row, g, decisions, states, reason):
                        slot = tally.setdefault(row[1], {'items': 0, 'alloc': 0})
                        slot['items'] += 1
                        slot['alloc'] += row[9]
        return {'groups': tally, 'items': sum(v['items'] for v in tally.values()),
                'alloc': sum(v['alloc'] for v in tally.values()), 'days': days}

    def _scope_groups(self, scan_id: int, flt: Mapping[str, str]) -> Dict[str, Any]:
        domain, tier = flt.get('domain') or None, flt.get('tier') or None
        if domain is not None and domain not in {d.value for d in Domain}:
            raise _invalid('domain')
        if tier is not None and tier not in {t.value for t in Tier}:
            raise _invalid('tier')
        report_only = flt.get('report_only')
        if report_only not in (None, '', '0', '1'):
            raise _invalid('report_only')
        groups = {g.key: g for g in self.store.groups(scan_id, domain=domain, tier=tier)}
        if report_only:
            groups = {k: g for k, g in groups.items() if (not g.actions and g.tier != Tier.PROTECTED) == (report_only == '1')}
        return groups

    @staticmethod
    def _row_matches(row: Sequence[Any], group: Any, decisions: Mapping[str, Any], states: Optional[set],
                     reason: Optional[str]) -> bool:
        if states is not None:
            state = decisions[row[0]].state.value
            other = ('other' in states and group.tier == Tier.JUNK and bool(group.actions)
                     and state not in ('selected', 'too_recent', 'too_recent_floor'))
            if state not in states and not other:
                return False
        if reason is not None:
            if group.tier != Tier.PROTECTED or ('tainted' if row[8] else report._protected_reason(group)) != reason:
                return False
        return True

    def items_matching(self, group: str, flt: Mapping[str, str], days: Optional[float] = None, order: str = 'alloc',
                       offset: int = 0, limit: int = 50) -> Dict[str, Any]:
        """The items of one group that a Browse filter names (see browse_scope), a page of them with their decision
        under quick(days). `order` is alloc, newest or oldest."""
        if order not in ('alloc', 'newest', 'oldest'):
            raise _invalid('order')
        days = self._quick_days(days)
        states = set(flt['state'].split('|')) if flt.get('state') else None
        reason = flt.get('reason') or None
        self.quick(days)
        spec = self.spec(QUICK_SESSION)
        with self.store.read_snapshot():
            self._require_current(spec, QUICK_SESSION)
            g = self.store.group(spec.scan_id, group)   # NOT_FOUND for a mistyped key
            decisions = self._resolve(QUICK_SESSION, spec).decisions
            found = [(r[9], r[6], r[0]) for r in queries.item_rows(self.store, spec.scan_id)
                     if r[1] == group and self._row_matches(r, g, decisions, states, reason)]
            key = {'alloc': lambda f: (-f[0], f[2]), 'newest': lambda f: (-f[1], f[2]), 'oldest': lambda f: (f[1], f[2])}[order]
            found.sort(key=key)
            keys = [f[2] for f in found[offset:offset + limit]]
            by_key = {i.key: i for i in self.store.items(spec.scan_id, keys=keys, limit=None)} if keys else {}
            rows = []
            for k in keys:
                row = asdict(by_key[k])
                row['decision'] = asdict(decisions[k])
                rows.append(row)
        return {'total': len(found), 'rows': rows}

    def browse_counts(self, days: Optional[float] = None) -> Dict[str, Any]:
        """What each Browse filter would show, for its menu: the overview's buckets at `days`. `states` are the junk
        states (selected, too_recent, too_recent_floor, other), `reasons` the protected reasons, `report_only` the
        groups that only report."""
        days = self._quick_days(days)
        self.quick(days)
        spec = self.spec(QUICK_SESSION)
        with self.store.read_snapshot():
            self._require_current(spec, QUICK_SESSION)
            groups = self.store.groups(spec.scan_id)
            per = self._resolve(QUICK_SESSION, spec).summary['groups']
            taint = queries.taint_split(self.store, spec.scan_id)
        states = {k: {'items': 0, 'alloc': 0} for k in ('selected', 'too_recent', 'too_recent_floor', 'other')}
        reasons: Dict[str, Dict[str, int]] = {}
        report_only = {'items': 0, 'alloc': 0}
        for g in groups:
            if g.tier == Tier.PROTECTED:
                for tainted in (False, True):
                    n, alloc = taint.get((g.key, tainted), (0, 0))
                    if n:
                        slot = reasons.setdefault('tainted' if tainted else report._protected_reason(g), {'items': 0, 'alloc': 0})
                        slot['items'] += n
                        slot['alloc'] += alloc
            elif not g.actions:
                report_only['items'] += g.items
                report_only['alloc'] += g.alloc
            elif g.tier == Tier.JUNK:
                s = per[g.key]
                for name, n, alloc in (('selected', s['selected'], s['alloc']), ('too_recent', s['too_recent'], s['too_recent_alloc']),
                                       ('too_recent_floor', s['floor_held'], s['floor_held_alloc'])):
                    states[name]['items'] += n
                    states[name]['alloc'] += alloc
                states['other']['items'] += g.items - s['selected'] - s['too_recent'] - s['floor_held']
                states['other']['alloc'] += g.alloc - s['alloc'] - s['too_recent_alloc'] - s['floor_held_alloc']
        return {'days': days, 'states': states, 'reasons': reasons, 'report_only': report_only}

    def snapshots(self) -> List[str]:
        """Local Time Machine snapshots, which can hold removed space until they age out. May raise RuntimeError."""
        return self.mac.snapshots()

    def pending_totals(self, plan_id: str) -> Dict[str, Any]:
        """What a run of this plan would still handle (its pending entries), with `by_action` shaped like the one in
        a selection summary. plan()['totals'] stays as created; exclusions change only these figures."""
        self.store.plan(plan_id)   # NOT_FOUND for an unknown plan
        acc: Dict[Action, List[int]] = {}
        for e in self.store.entries(plan_id, EntryState.PENDING):
            v = acc.setdefault(e.action, [0, 0, 0])
            v[0] += 1
            v[1] += e.alloc
            v[2] += e.reclaim
        return {'count': sum(v[0] for v in acc.values()), 'alloc': sum(v[1] for v in acc.values()),
                'reclaim': sum(v[2] for v in acc.values()),
                'by_action': {a.value: _action_totals(a, v) for a, v in sorted(acc.items(), key=lambda kv: kv[0].value)}}

    def validate_run(self, plan_id: str, revision: int, entries_digest: str) -> None:
        """Raise what run_plan() would raise before it starts anything. A caller that runs in the background would
        otherwise learn of the refusal only from job()."""
        plan = self.store.plan(plan_id)
        if plan.state == PlanState.RUNNING:
            raise StewardError(ErrorCode.IN_PROGRESS, 'error.IN_PROGRESS')
        if plan.state not in (PlanState.DRAFT, PlanState.PAUSED):
            raise StewardError(ErrorCode.INVALID, 'error.plan_state', state=plan.state.value)
        catalog = load_catalog(self._settings)
        _, facts_hash = compute_facts(DetectorContext(self.bases, self.mac, self._settings), catalog)
        planner.validate_for_run(plan, {'catalog_hash': catalog.hash, 'facts_hash': facts_hash,
                                        'settings_hash': settings_mod.settings_hash(self._settings)},
                                 self.clock(), revision, entries_digest)

    # diagnosis -------------------------------------------------------------------------------------------

    def diagnose_session(self, item_key: str) -> Dict[str, Any]:
        item = self.store.item(self._latest()['id'], item_key)
        if not item.rule_id.startswith(CODEX_SESSION_RULES):
            raise StewardError(ErrorCode.INVALID, 'error.not_a_session', item=item_key)
        try:
            return diagnose(str(self.bases.to_abs(item.path)))
        except FileNotFoundError:
            raise StewardError(ErrorCode.NOT_FOUND, 'error.NOT_FOUND', what='file', id=item.path) from None

    # running plans -----------------------------------------------------------------------------------------

    def _executor(self) -> Executor:
        return Executor(self.store, self._load_catalog(), None, self.bases, self.mac, self.audit, self.clock,
                        self._settings, self.data_dir, hooks=self._hooks, lock=self.lock)

    def _action_context(self) -> actions.ActionContext:
        return actions.ActionContext(store=self.store, bases=self.bases, mac=self.mac, audit=self.audit,
                                     clock=self.clock, data_dir=self.data_dir)

    def _recover(self) -> None:
        """Settle what a crashed run left behind. Skipped while another process holds the operation lock: its
        `in_progress` entries are alive, not crashed."""
        if not (self.store.entries_in_state(EntryState.IN_PROGRESS) or self.store.plans_in_state(PlanState.RUNNING)):
            return
        try:
            with self.lock:
                report_ = self._executor().recover()
        except StewardError as e:
            if e.code != ErrorCode.BUSY:
                raise
            self.events.publish('recover.skipped', reason='busy')
            return
        self.recovery = report_
        self.events.publish('recover.done', **report_)

    def preflight(self, plan_id: str) -> Dict[str, Any]:
        """What a run of this plan would meet right now. Changes nothing."""
        return self._executor().preflight(plan_id)

    def _claim_run(self, plan_id: str) -> Control:
        with self._job_lock:
            if self._active:
                raise StewardError(ErrorCode.BUSY, 'error.busy')
            self._active = True
            self._control = Control()
            self._job = dict(_idle_job(), status='running', kind='run', plan_id=plan_id, phase='run')
            return self._control

    def _release_run(self, status: str, **fields: Any) -> None:
        with self._job_lock:
            self._active = False
            self._control = None
            self._job.update(status=status, phase='', **fields)

    def _begin(self, plan_id: str) -> Control:
        """Claim the run slot and the operation lock, in the calling thread, so BUSY is reported to the caller."""
        control = self._claim_run(plan_id)
        try:
            self.lock.__enter__()
        except BaseException:
            self._release_run('idle', plan_id=None)
            raise
        return control

    def _track(self, kind: str, data: Dict[str, Any]) -> None:
        with self._job_lock:
            job = self._job
            if kind == 'run.started':
                job['counts'].update(total=data['total'], done=0, bytes=0)
            elif kind == 'entry.done':   # progress events are throttled; this keeps the count exact
                job['counts']['done'] += 1
                job['current'] = data['path']
            elif kind == 'run.progress':
                job['counts'].update(done=data['done'], total=data['total'], bytes=data['bytes'])
                job['current'] = data['current'] or job['current']
            elif kind == 'run.paused' and not data.get('error'):
                job['status'] = 'paused'
            elif kind == 'run.resumed':
                job['status'] = 'running'

    def _execute(self, control: Control, plan_id: str, revision: int, entries_digest: str,
                 on_event: Optional[Callable[[str, Dict[str, Any]], None]]) -> Dict[str, Any]:
        """Runs with the operation lock already held; releases it, then rescans."""
        started = False

        def relay(kind: str, data: Dict[str, Any]) -> None:
            nonlocal started
            started = started or kind == 'run.started'
            self._track(kind, data)
            self.events.publish(kind, **data)
            if on_event is not None:
                on_event(kind, data)

        try:
            try:
                summary = self._executor().run(plan_id, revision, entries_digest, control, relay)
            finally:
                self.lock.__exit__(None, None, None)
                with self._job_lock:
                    self._control = None   # pause/resume/cancel are meaningless from here on
                    self._job['phase'] = 'rescan' if started else ''
            if started:
                summary = self.store.save_run_summary(plan_id, summary)   # the result outlives the process
                summary['rescan'] = self._rescan(plan_id)
                self.store.save_run_summary(plan_id, summary)
        except BaseException as e:
            lost = isinstance(e, StewardError) and e.code == ErrorCode.DATA_DIR_RECREATED
            if started and not lost:   # with the data dir gone there are no scan or plan rows to rescan from
                self._rescan(plan_id)   # a partly finished run changed the disk too
            self._release_run('error', error=f'{type(e).__name__}: {e}',
                              error_code=e.code.value if isinstance(e, StewardError) else None)
            raise
        self._release_run('done', summary=summary, plan_state=summary['state'])
        self._retain()
        return summary

    def _rescan(self, plan_id: str) -> Dict[str, Any]:
        """Scan the same areas again so the report and the next plan do not offer what was just removed."""
        try:
            domains = self.store.scan(self.store.plan(plan_id).scan_id)['domains']
            return {'scan_id': self.scan(domains=domains)}
        except Exception as e:   # the run already happened; its result must still reach the caller
            self.events.publish('scan.failed', error=f'{type(e).__name__}: {e}')
            return {'error': f'{type(e).__name__}: {e}'}

    def run_plan(self, plan_id: str, revision: int, entries_digest: str,
                 on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None) -> Dict[str, Any]:
        """Blocking. `on_event(type, data)` also receives every run.* event the bus carries."""
        control = self._begin(plan_id)
        return self._execute(control, plan_id, revision, entries_digest, on_event)

    def start_run(self, plan_id: str, revision: int, entries_digest: str,
                  on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None) -> Dict[str, Any]:
        """run_plan() in a background thread. Progress and the outcome are in job()."""
        control = self._begin(plan_id)

        def work() -> None:
            try:
                self._execute(control, plan_id, revision, entries_digest, on_event)
            except Exception:   # recorded in job(); a thread has nobody to raise to
                pass

        threading.Thread(target=work, name=f'disk-steward-run-{plan_id}', daemon=True).start()
        return self.job()

    def start_scan(self, domains: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        """scan() in a background thread, on the same slot as a run: progress is in job() ('scan' holds the latest
        progress event), cancel() stops it, and no run or second scan can start meanwhile."""
        if domains is not None and not set(domains) <= {d.value for d in Domain}:
            raise StewardError(ErrorCode.INVALID, 'error.invalid_setting', field='domains')   # before the thread
        if self.lock.held_by_other():
            raise StewardError(ErrorCode.BUSY, 'error.busy')
        with self._job_lock:
            if self._active:
                raise StewardError(ErrorCode.BUSY, 'error.busy')
            self._active = True
            control = self._control = Control()
            self._job = dict(_idle_job(), status='running', kind='scan', phase='scan')

        def work() -> None:
            try:
                scan_id = self.scan(domains, progress=self._track_scan, cancel=lambda: control.cancelled)
            except StewardError as e:   # recorded in job(); a thread has nobody to raise to
                if e.message_key == 'error.scan_cancelled':
                    self._release_run('idle')
                else:
                    self._release_run('error', error=f'{type(e).__name__}: {e}', error_code=e.code.value)
            except Exception as e:
                self._release_run('error', error=f'{type(e).__name__}: {e}', error_code=None)
            else:
                self._release_run('done', scan_id=scan_id)

        threading.Thread(target=work, name='disk-steward-scan', daemon=True).start()
        return self.job()

    def _track_scan(self, event: Dict[str, Any]) -> None:
        with self._job_lock:
            self._job['scan'] = dict(event)

    def _active_control(self) -> Control:
        with self._job_lock:
            if self._control is None:
                raise StewardError(ErrorCode.NOT_FOUND, 'error.no_run')
            return self._control

    def _pausable_control(self) -> Control:
        control = self._active_control()
        if self.job()['kind'] == 'native':   # a tool's process has no checkpoint to wait at
            raise StewardError(ErrorCode.INVALID, 'error.native_no_pause')
        return control

    def pause(self) -> Dict[str, Any]:
        self._pausable_control().pause()
        return self.job()

    def resume(self) -> Dict[str, Any]:
        self._pausable_control().resume()
        return self.job()

    def cancel(self) -> Dict[str, Any]:
        self._active_control().cancel()
        return self.job()

    def job(self) -> Dict[str, Any]:
        """idle | running | paused | done | error, plus plan_id, counts, the current path and the last error."""
        with self._job_lock:
            out = copy.deepcopy(self._job)
            out['pause_requested'] = bool(self._control is not None and self._control.paused)
            return out

    # native commands ---------------------------------------------------------------------------------------

    def native_tools(self, locale: Optional[str] = None) -> List[Dict[str, Any]]:
        """The report-only groups of the latest scan that hold data, biggest first, with the command that cleans each and
        whether it can run now: {rule_id, group, label, bytes, items, command, runnable, reason, tool, tool_version,
        targets, folders}. `command` is the text a run must be given back unchanged; `reason` (translated) says why a
        row cannot run; `targets` lists the models `ollama list` shows ({name, size, bytes, command}) and is None for any
        other tool. `folders` lists, for a tool without a command whose rule offers a manual action, each folder of the
        group ({item, path, bytes}) that trash_plan() can move to the Trash; None otherwise. Empty before the first
        scan."""
        if locale is not None and locale not in LOCALES:
            raise StewardError(ErrorCode.INVALID, 'error.invalid_locale', locale=str(locale))
        locale = locale or self.locale
        latest = self.store.latest_scan()
        if latest is None:
            return []
        groups = [g for g in self.store.groups(latest['id']) if not g.actions and g.tier != Tier.PROTECTED and g.alloc > 0]
        states = {g.key: self._native.state(g.rule_id) for g in groups}
        catalog = self._load_catalog()
        trash_ok = self.mac.trash_available()
        names = sorted({(st.argv[0] if st.argv else 'ollama') for st in states.values() if st.tool is not None})
        if names:   # a version is a process start each; the first listing asks for all of them at once
            with concurrent.futures.ThreadPoolExecutor(len(names)) as pool:
                list(pool.map(self._native.version, names))
        rows = []
        for g in groups:
            st = states[g.key]
            if st.argv is not None:
                command: Optional[str] = native_mod.display(st.argv)
            elif g.rule_id == native_mod.OLLAMA:
                command = 'ollama rm <model>'
            else:
                command = g.hint if g.rule_id in native_mod.COPY_ONLY else None
            targets = None
            if g.rule_id == native_mod.OLLAMA and st.targets is not None:
                targets = [dict(t, command=native_mod.display(native_mod.ollama_argv(t['name']))) for t in st.targets]
            folders = None
            reason = st.reason
            if command is None and Action.TRASH in catalog.rule(g.rule_id).manual_actions:
                if trash_ok:
                    folders = [{'item': i.key, 'path': i.path, 'bytes': i.alloc}
                               for i in self.store.items(latest['id'], group_key=g.key)]
                    reason = None
                else:
                    reason = ('trash_unavailable', {})
            rows.append({
                'rule_id': g.rule_id, 'group': g.key, 'label': report.group_label(g, locale), 'bytes': g.alloc,
                'items': g.items, 'command': command, 'runnable': st.runnable, 'folders': folders,
                'reason': None if reason is None else translate(f'native.reason.{reason[0]}', locale, **reason[1]),
                'tool': st.tool, 'tool_version': None if st.tool is None else self._native.version(
                    st.argv[0] if st.argv else 'ollama'), 'targets': targets})
        rows.sort(key=lambda r: (-r['bytes'], r['group']))
        return rows

    def trash_plan(self, item_key: str) -> Dict[str, Any]:
        """A draft plan that moves one folder of native_tools()' `folders` to the Trash. Nothing moves until the plan
        runs, with every check a plan gets."""
        latest = self._latest()
        plan = planner.create_manual_plan(self.store, latest['id'], item_key, Action.TRASH, self.clock(), self._settings,
                                          self.mac, catalog=self._load_catalog())
        return self.plan(plan.id)

    def _native_request(self, rule_id: Any, target: Any, command: Any) -> Tuple[Any, native_mod.ToolState,
                                                                                Tuple[str, ...], str]:
        """Everything that must be true before a command is started: a report-only rule of the registry, a tool that
        is there and works, an exact model name for Ollama, and the command text the user confirmed. Nothing here
        is taken from the request except those three values, and each is compared with what the program derives."""
        if not isinstance(rule_id, str) or not rule_id:
            raise _invalid('rule_id')
        if target is not None and not isinstance(target, str):
            raise _invalid('target')
        if not isinstance(command, str):
            raise _invalid('command')
        try:
            rule = self._load_catalog().rule(rule_id)
        except KeyError:
            rule = None
        if rule is None or not is_report_only(rule):
            raise StewardError(ErrorCode.INVALID, 'error.native_unknown', rule_id=rule_id)
        state = self._native.state(rule_id, fresh=True)
        if not state.runnable:
            key, args = state.reason   # type: ignore[misc]
            raise StewardError(ErrorCode.UNAVAILABLE, 'error.native_not_runnable', rule_id=rule_id,
                               reason_key=f'native.reason.{key}', reason_args=args,
                               reason=translate(f'native.reason.{key}', self.locale, **args))
        if rule_id == native_mod.OLLAMA:
            if not target:
                raise StewardError(ErrorCode.INVALID, 'error.native_target_required', rule_id=rule_id)
            if target not in {t['name'] for t in state.targets or ()}:
                raise StewardError(ErrorCode.INVALID, 'error.native_bad_target', target=target)
            argv = native_mod.ollama_argv(target)
        else:
            if target:
                raise _invalid('target')
            assert state.argv is not None
            argv = state.argv
        shown = native_mod.display(argv)
        if command != shown:
            raise StewardError(ErrorCode.INVALID, 'error.native_command_changed', expected=shown, command=command)
        return rule, state, argv, shown

    def check_native(self, rule_id: str, target: Optional[str], command: str) -> None:
        """Raise what start_native() would raise about the request itself (not BUSY), starting nothing."""
        self._native_request(rule_id, target, command)

    def _begin_native(self, rule_id: Any, target: Any, command: Any) -> Tuple[Control, Any, str, Tuple[str, ...], str]:
        """Check the request, take the job slot and the operation lock in the calling thread (so BUSY reaches the
        caller) and describe the job."""
        rule, state, argv, shown = self._native_request(rule_id, target, command)
        with self._job_lock:
            if self._active:
                raise StewardError(ErrorCode.BUSY, 'error.busy')
            self._active = True
            control = self._control = Control()
            self._job = dict(_idle_job(), status='running', kind='native', phase='native', rule_id=rule_id,
                             target=target or None, command=shown, started_at=self.clock(), output=[],
                             output_lines=0, native=None)
        try:
            self.lock.__enter__()
        except BaseException:
            self._release_run('idle')
            with self._job_lock:
                self._job = _idle_job()   # nothing started: no trace of this request in job()
            raise
        assert state.tool is not None
        return control, rule, state.tool, argv, shown

    def start_native(self, rule_id: str, target: Optional[str], command: str) -> Dict[str, Any]:
        """Run the native cleanup command of a report-only tool in a background thread (the job slot of a scan or a
        run: BUSY while another is active or another process holds the operation lock). `command` must be the text
        native_tools() showed, unchanged. job() reports kind 'native' with rule_id, target, command, started_at and the
        last output lines; cancel() stops the tool. Output arrives as 'native.output' events, the end as
        'native.done' or 'native.failed'. The result stays in job()['native']."""
        control, rule, tool, argv, shown = self._begin_native(rule_id, target, command)

        def work() -> None:
            try:
                self._native_job(control, rule, tool, argv, shown, target or None, None)
            except Exception:   # recorded in job(); a thread has nobody to raise to
                pass

        threading.Thread(target=work, name='disk-steward-native', daemon=True).start()
        return self.job()

    def run_native(self, rule_id: str, target: Optional[str], command: str,
                   on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None) -> Dict[str, Any]:
        """start_native() in the calling thread. Returns the result: {rule_id, target, command, status ('ok', 'failed'
        for a non-zero exit, 'timeout', 'cancelled' or 'error'), exit_code, before, after, freed, started_at,
        finished_at, output_truncated, output_tail, error, rescan}. `on_event(kind, data)` also gets every native.*
        event."""
        control, rule, tool, argv, shown = self._begin_native(rule_id, target, command)
        return self._native_job(control, rule, tool, argv, shown, target or None, on_event)

    def _native_job(self, control: Control, rule: Any, tool: str, argv: Tuple[str, ...], shown: str,
                    target: Optional[str], on_event: Optional[Callable[[str, Dict[str, Any]], None]]) -> Dict[str, Any]:
        try:
            result = self._native_core(control, rule, tool, argv, shown, target, on_event)
        except BaseException as e:   # a bug: the job must still end
            self._release_run('error', error=f'{type(e).__name__}: {e}', error_code=None)
            raise
        error = result['error']
        status = {'ok': 'done', 'failed': 'done', 'cancelled': 'idle'}.get(result['status'], 'error')
        self._release_run(status, native=result, error=None if error is None else error['text'],
                          error_code=None if error is None else error['code'])
        return result

    def _native_core(self, control: Control, rule: Any, tool: str, argv: Tuple[str, ...], shown: str,
                     target: Optional[str], on_event: Optional[Callable[[str, Dict[str, Any]], None]]) -> Dict[str, Any]:
        started = self.clock()
        result: Dict[str, Any] = {
            'rule_id': rule.id, 'target': target, 'command': shown, 'status': 'error', 'exit_code': None,
            'before': None, 'after': None, 'freed': None, 'started_at': started, 'finished_at': None,
            'output_truncated': False, 'output_tail': [], 'error': None, 'rescan': None}

        def emit(kind: str, **data: Any) -> None:
            self.events.publish(kind, **data)
            if on_event is not None:
                on_event(kind, data)

        def on_line(line: str) -> None:
            with self._job_lock:
                out = self._job['output']
                out.append(line)
                del out[:-native_mod.TAIL_LINES]
                self._job['output_lines'] += 1
                lines = self._job['output_lines']
            if lines <= native_mod.EVENT_LINES:   # the bus keeps 500 events; a chatty tool must not push everything out
                emit('native.output', line=line)

        def failure(e: StewardError) -> None:
            text = translate(e.message_key, self.locale, **e.details)
            result['error'] = {'code': e.code.value, 'message_key': e.message_key, 'details': e.details, 'text': text}

        ran = False
        tmp: Optional[str] = None
        try:
            try:
                self.audit.write({'event': 'native.intent', 'rule_id': rule.id, 'target': target, 'command': shown,
                                  'argv': [tool] + list(argv[1:])})
                result['before'] = native_mod.measure(self.bases, rule, lambda: control.cancelled)
                tmp = tempfile.mkdtemp(prefix='disk-steward-native-')
                try:
                    run: Optional[native_mod.RunResult] = native_mod.run_command(
                        [tool] + list(argv[1:]), tmp, os.environ, lambda: control.cancelled, on_line)
                except OSError as e:   # the tool could not be started
                    failure(StewardError(ErrorCode.UNAVAILABLE, 'error.native_spawn', command=shown, error=str(e)))
                    run = None
                if run is not None:
                    ran = True
                    result.update(status=run.status, exit_code=run.exit_code, output_truncated=run.truncated,
                                  output_tail=run.tail)
                    result['after'] = native_mod.measure(self.bases, rule)
                    result['freed'] = max(result['before'] - result['after'], 0)
            except StewardError as e:
                if e.message_key != 'error.scan_cancelled':   # the measurement before the run was cancelled
                    raise
                result['status'] = 'cancelled'
            finally:
                if tmp is not None:
                    shutil.rmtree(tmp, ignore_errors=True)   # ours, and empty unless the tool wrote into it
        finally:
            self.lock.__exit__(None, None, None)
        result['finished_at'] = self.clock()
        if result['status'] == 'timeout':
            failure(StewardError(ErrorCode.UNAVAILABLE, 'error.native_timeout', command=shown,
                                 minutes=int(native_mod.TIMEOUT // 60)))
        elif result['status'] == 'cancelled':
            failure(StewardError(ErrorCode.IN_PROGRESS, 'error.native_cancelled', command=shown))
        self.audit.write({'event': 'native.outcome', 'rule_id': rule.id, 'target': target, 'command': shown,
                          'status': result['status'], 'exit_code': result['exit_code'], 'before': result['before'],
                          'after': result['after'], 'freed': result['freed'],
                          'seconds': round(result['finished_at'] - started, 3)})
        if ran:
            self._save_native(result)
            self._native.invalidate()   # e.g. `ollama rm` removed a model: the next listing must not offer it again
        if result['error'] is not None:
            err = result['error']
            emit('native.failed', code=err['code'], message_key=err['message_key'], details=err['details'])
        elif ran:
            emit('native.done', rule_id=rule.id, target=target, exit_code=result['exit_code'], before=result['before'],
                 after=result['after'], freed=result['freed'], output_tail=result['output_tail'],
                 output_truncated=result['output_truncated'])
        if ran and self.store.latest_scan() is not None:   # so every number on screen follows the tool
            with self._job_lock:
                self._job['phase'] = 'rescan'
            try:
                result['rescan'] = {'scan_id': self.scan(domains=list(self._settings.domains))}
            except Exception as e:   # the tool already ran; its result must still reach the caller
                self.events.publish('scan.failed', error=f'{type(e).__name__}: {e}')
                result['rescan'] = {'error': f'{type(e).__name__}: {e}'}
        return result

    def _save_native(self, result: Mapping[str, Any]) -> None:
        """meta 'native:<epoch ms>' (Insights lists it with the cleanups). A second run in the same millisecond takes
        the next free key."""
        ms = int(result['started_at'] * 1000)
        while self.store.meta(f'native:{ms}') is not None:
            ms += 1
        keep = ('rule_id', 'target', 'command', 'status', 'exit_code', 'before', 'after', 'freed', 'started_at',
                'finished_at')
        self.store.set_meta(f'native:{ms}', json.dumps({k: result[k] for k in keep}))

    # retention ---------------------------------------------------------------------------------------------

    def compact_plans(self) -> int:
        """Keep the database small. A plan that has finished (done, cancelled, failed) gets a complete run summary at
        once; COMPACT_AFTER_DAYS later its done and cancelled entries, nearly all of them, are deleted and only the
        summary and the few entries that explain outcomes stay. The audit log already holds every removed path for
        good. Plans that are draft, running or paused are never touched. Returns how many plans were compacted."""
        now = self.clock()
        compacted = 0
        for plan_id in self.store.finished_plan_ids():
            summary = self.store.run_summary(plan_id)
            if summary is None or 'finished_at' not in summary:
                summary = self.store.save_run_summary(plan_id)
            if now - summary['finished_at'] < COMPACT_AFTER_DAYS * DAY_SECONDS:
                continue
            removed = self.store.compact_plan(plan_id, now)
            self.audit.write({'event': 'plan.compacted', 'plan_id': plan_id, 'removed_entries': removed})
            compacted += 1
        return compacted

    def _retain(self) -> None:
        """compact_plans() after a scan or a run. Both already happened and must still reach the caller, so a failure
        is published (and audited) instead of raised; the next pass tries again."""
        try:
            self.compact_plans()
        except Exception as e:
            error = f'{type(e).__name__}: {e}'
            self.events.publish('retention.failed', error=error)
            self.audit.write({'event': 'retention.failed', 'error': error})

    # archives ----------------------------------------------------------------------------------------------

    def archives(self) -> List[Dict[str, Any]]:
        return self.store.archives()

    def restore(self, archive_id: str) -> Dict[str, Any]:
        with self.lock:
            result = actions.restore(self._action_context(), archive_id)
        self.events.publish('archive.restored', **result)
        return result

    def purge_archive(self, archive_id: str) -> Dict[str, Any]:
        with self.lock:
            result = actions.purge_archive(self._action_context(), archive_id)
        self.events.publish('archive.purged', **result)
        return result

    # insights ----------------------------------------------------------------------------------------------

    def insights(self, window_days: int = 30, locale: Optional[str] = None) -> Dict[str, Any]:
        """Environment, history, current state, results and follow-up TODOs for the Insights page, texts in `locale`
        (default: the App's). `window_days` is how far back history and deltas reach; 0 means all of it. Reads only
        what earlier scans and runs stored, plus the volume and a few quick OS queries; it never walks the disk and
        never measures /Applications (largest_apps appears only once app_sizes() has)."""
        if isinstance(window_days, bool) or not isinstance(window_days, int) or window_days < 0:
            raise _invalid('window')
        if locale is not None and locale not in LOCALES:
            raise StewardError(ErrorCode.INVALID, 'error.invalid_locale', locale=str(locale))
        return insights_mod.build(insights_mod.Parts.of(self), window_days, locale or self.locale, self.clock())


def _idle_job() -> Dict[str, Any]:
    return {'status': 'idle', 'kind': None, 'plan_id': None, 'phase': '', 'counts': {'done': 0, 'total': 0, 'bytes': 0},
            'scan': None, 'scan_id': None, 'current': '', 'error': None, 'error_code': None, 'summary': None,
            'plan_state': None}
