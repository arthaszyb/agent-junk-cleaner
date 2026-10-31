"""Executor: runs a plan entry by entry, re-checking everything at the moment of action.

The scan and the plan may be hours old. Every entry therefore goes through the whole checklist again (identity,
fingerprint, classification, age, running apps and open files) and is skipped, never forced, when anything is off.
The checklist and the action order are documented in docs/architecture.md (Executor)."""
from __future__ import annotations

import collections
import contextlib
import errno
import os
import sqlite3
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from . import actions
from . import settings as settings_mod
from .actions import ActionContext, Cancelled, Interrupt, Paused, UnsafeParent
from .audit import Audit
from .catalog import Catalog, load_catalog
from .classify import Classifier
from .detectors import DetectorContext, compute_facts
from .fsutil import LOCKED_FLAGS, SF_DATALESS, walk_bundle
from .guards import SNAPSHOT_TTL, Guard, SnapshotError
from .locking import OperationLock
from .macos import MacOS
from .model import (Action, AgeBasis, Entry, EntryState, ErrorCode, Item, Outcome, Plan, PlanState, StatInfo, StewardError,
                    Unit)
from .planner import validate_for_run
from .settings import Settings, settings_hash
from .store import Store

__all__ = ['Control', 'Cancelled', 'Executor', 'HOOK_NAMES', 'LockLost']

HOOK_NAMES = ('after_claim', 'after_parent', 'after_lstat', 'after_fingerprint', 'after_classify', 'after_guard',
              'before_unlink', 'archive_chunk')
DAY_SECONDS = 86400.0
FACTS_TTL = 15.0         # seconds before the detector facts are recomputed during a run (a CLI update can repoint `claude`)
CONTEXT_TTL = 2.0        # seconds a directory's git/database context is reused; listing a huge folder per entry is quadratic
ARCHIVE_SPACE_FACTOR = 1.2
LAST_USED_BATCH = 200    # paths per mdls call, as in macos.MDLS_BATCH
PROGRESS_INTERVAL = 0.25
OnEvent = Optional[Callable[[str, Dict[str, Any]], None]]


class LockLost(RuntimeError):
    pass


class LastUsedUnknown(RuntimeError):
    """Spotlight could not say when an item was last used."""


class Control:
    """Pause, resume and cancel a run from another thread. The run checks it between entries and, while
    archiving, every 8 MiB."""

    def __init__(self) -> None:
        self._running = threading.Event()
        self._running.set()
        self._cancelled = threading.Event()

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self._running.set()

    def cancel(self) -> None:
        self._cancelled.set()
        self._running.set()   # wakes a run that is waiting out a pause

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def checkpoint(self, blocking: bool = True, on_pause: Optional[Callable[[], None]] = None,
                   on_resume: Optional[Callable[[], None]] = None) -> None:
        """Raises Cancelled after cancel(). While paused: blocks until resume() (blocking) or raises Paused."""
        if self._cancelled.is_set():
            raise Cancelled()
        if self._running.is_set():
            return
        if not blocking:
            raise Paused()
        if on_pause is not None:
            on_pause()
        self._running.wait()
        if self._cancelled.is_set():
            raise Cancelled()
        if on_resume is not None:
            on_resume()


class _Run:
    """Everything one run() call needs, so the Executor object itself stays free of per-run state."""

    def __init__(self, plan: Plan, control: Control, on_event: OnEvent, classifier: Classifier, catalog: Catalog,
                 guard: Guard, ctx: ActionContext, bases_dev: Dict[str, int], total: int):
        self.plan = plan
        self.control = control
        self.on_event = on_event
        self.classifier = classifier
        self.catalog = catalog
        self.guard = guard
        self.ctx = ctx
        self.bases_dev = bases_dev
        self.total = total
        self.done = 0
        self.bytes = 0
        self.facts_at = time.monotonic()
        self.context_cache: Dict[str, Tuple[float, Tuple[bool, bool]]] = {}
        self.last_progress = 0.0
        self.uid = os.getuid()
        # LAST_USED rules: abs path -> (when it was read, Spotlight's date), and the order the entries come in
        self.last_used: Dict[str, Tuple[float, Optional[float]]] = {}
        self.lu_order: List[str] = []
        self.lu_index: Dict[str, int] = {}


def _skip(reason: str) -> Outcome:
    return Outcome(EntryState.SKIPPED, reason)


def required_apps(evidence: Any) -> Dict[str, str]:
    """bundle id -> app name for every 'evidence.requires_quit' among a classification's evidence."""
    return {ev.args['bundle_id']: ev.args.get('app', ev.args['bundle_id'])
            for ev in evidence if ev.key == 'evidence.requires_quit'}


class Executor:
    def __init__(self, store: Store, catalog: Catalog, classifier: Optional[Classifier], bases: Any, mac: MacOS,
                 audit: Audit, clock: Callable[[], float], settings: Settings, data_dir: Path,
                 hooks: Optional[Mapping[str, Callable[[Any], None]]] = None,
                 lock: Optional[OperationLock] = None):
        """`classifier` may be None; run() then builds one from the facts it computes anyway. `lock` is the
        operation lock the caller holds; when given, it is checked before every entry."""
        unknown = set(hooks or ()) - set(HOOK_NAMES)
        if unknown:
            raise ValueError(f'unknown executor hooks: {sorted(unknown)}')
        self.store, self.catalog, self.classifier, self.bases = store, catalog, classifier, bases
        self.mac, self.audit, self.clock, self.settings = mac, audit, clock, settings
        self.data_dir = Path(os.path.abspath(data_dir))
        self.hooks: Dict[str, Callable[[Any], None]] = dict(hooks or {})
        self.lock = lock
        self.facts_ttl = FACTS_TTL
        self.context_ttl = CONTEXT_TTL

    # helpers ---------------------------------------------------------------------------------------------

    def _hook(self, name: str, entry: Entry) -> None:
        fn = self.hooks.get(name)
        if fn is not None:
            fn(entry)

    def _current(self) -> Tuple[Catalog, Dict[str, Any], Dict[str, str]]:
        """Catalog, facts and the three hashes as they are right now."""
        catalog = load_catalog(self.settings)
        facts, facts_hash = compute_facts(DetectorContext(self.bases, self.mac, self.settings), catalog)
        return catalog, facts, {'catalog_hash': catalog.hash, 'facts_hash': facts_hash,
                                'settings_hash': settings_hash(self.settings)}

    def _fetch_last_used(self, run: _Run, paths: List[str]) -> None:
        now = self.clock()
        for i in range(0, len(paths), LAST_USED_BATCH):
            batch = paths[i:i + LAST_USED_BATCH]
            found = self.mac.last_used(batch)
            for path in batch:
                run.last_used[path] = (now, found.get(path))

    def _fresh_last_used(self, run: _Run, abs_path: str) -> Optional[float]:
        """Spotlight's last-used date as of a moment ago. The scan's value may be days old, and a file opened
        since then is not idle. Re-read in batches (from this entry on) once the values are older than the guard's
        snapshots; a failed read raises LastUsedUnknown."""
        hit = run.last_used.get(abs_path)
        if hit is None or not 0 <= self.clock() - hit[0] <= SNAPSHOT_TTL:
            start = run.lu_index.get(abs_path)
            batch = run.lu_order[start:start + LAST_USED_BATCH] if start is not None else [abs_path]
            try:
                self._fetch_last_used(run, batch)
            except RuntimeError as e:
                raise LastUsedUnknown(str(e)) from e
        return run.last_used[abs_path][1]

    def _revalidate(self, run: _Run) -> None:
        """After a pause of any length: the plan may have expired and the rules or settings may have changed (the
        settings file is read again, another process could have rewritten it). Facts are not compared, as for any
        started plan. A failure leaves the plan paused with the error."""
        plan = self.store.plan(run.plan.id)
        settings = self._settings_now()
        catalog = load_catalog(settings)
        validate_for_run(plan, {'catalog_hash': catalog.hash, 'facts_hash': plan.facts_hash,
                                'settings_hash': settings_hash(settings)}, self.clock(), plan.revision,
                         plan.entries_digest)

    def _settings_now(self) -> Settings:
        if (self.data_dir / settings_mod.FILENAME).exists():
            return settings_mod.load(self.data_dir)
        return self.settings

    def _refresh_facts(self, run: _Run) -> None:
        """Re-read the detector facts and classify with them from now on. A plan's own removals change facts
        (the list of installed versions), so they are not compared with the plan's hash; what matters is that
        a version `claude` was repointed to meanwhile is seen as current and therefore kept."""
        facts, facts_hash = compute_facts(DetectorContext(self.bases, self.mac, self.settings), run.catalog)
        run.facts_at = time.monotonic()
        if facts_hash != run.classifier.facts_hash:
            run.classifier = Classifier(run.catalog, facts, self.bases, self.settings)

    def _required_apps(self, plan: Plan, entry: Entry, match: Any) -> Dict[str, str]:
        """Apps to quit for this entry: what the fresh classification names, plus what the scan's item named. The
        scan row is looked up only if it still exists; if the app was uninstalled since, the old name does no harm
        (it only blocks while that app runs), and without the row the fresh evidence stands alone."""
        found = required_apps(match.evidence if match is not None else ())
        item = self._plan_item(plan, entry)
        if item is not None:
            for bundle_id, app in required_apps(item.evidence).items():
                found.setdefault(bundle_id, app)
        return found

    def _plan_item(self, plan: Plan, entry: Entry) -> Optional[Item]:
        """The scan's row for this entry (evidence, last-used date); None once the scan was pruned."""
        try:
            return self.store.item(plan.scan_id, entry.item_key)
        except StewardError as e:
            if e.code != ErrorCode.NOT_FOUND:
                raise
            return None

    def _context(self, classifier: Classifier, cache: Dict[str, Tuple[float, Tuple[bool, bool]]],
                 logical: str) -> Tuple[bool, bool]:
        """(in_git_tree, db_dir) for the entry's directory, reused for a moment between entries of one folder."""
        parent = logical.rpartition('/')[0]
        now = time.monotonic()
        hit = cache.get(parent)
        if hit is not None and now - hit[0] < self.context_ttl:
            return hit[1]
        found = classifier.file_context(logical)
        cache[parent] = (now, found)
        return found

    def _classify(self, classifier: Classifier, cache: Dict[str, Tuple[float, Tuple[bool, bool]]], entry: Entry,
                  rule: Any, st: StatInfo) -> Any:
        """The classification of an entry as it is on disk now (None: no bundle rule claims the folder)."""
        in_git, db_dir = self._context(classifier, cache, entry.path)
        if entry.unit == Unit.BUNDLE:
            return classifier.classify_dir(entry.path, st, rule.domain, in_git_tree=in_git)
        return classifier.classify(entry.path, st, False, rule.domain, in_git_tree=in_git, db_dir=db_dir)

    def _emit(self, run: _Run, event: str, **data: Any) -> None:
        if run.on_event is not None:
            run.on_event(event, {'plan_id': run.plan.id, **data})

    def _audit(self, event: str, plan_id: str, **fields: Any) -> None:
        self.audit.write({'event': event, 'plan_id': plan_id, **fields})

    # preflight ---------------------------------------------------------------------------------------------

    def preflight(self, plan_id: str) -> Dict[str, Any]:
        """What run() would find right now, without changing anything."""
        plan = self.store.plan(plan_id)
        now = self.clock()
        catalog, facts, hashes = self._current()
        # The same rule as validate_for_run: only the catalog and settings can stop a plan as a whole. Facts are
        # applied per entry below, exactly as run() will.
        unchanged = {name: hashes[f'{name}_hash'] == getattr(plan, f'{name}_hash') for name in ('catalog', 'settings')}
        pending = self.store.entries(plan_id, EntryState.PENDING)
        guard = Guard(self.mac, self.clock)
        guard.refresh(force=True)
        classifier = Classifier(catalog, facts, self.bases, self.settings)
        context_cache: Dict[str, Tuple[float, Tuple[bool, bool]]] = {}
        blockers: Dict[str, Dict[str, Any]] = {}
        blocked = blocked_alloc = 0
        for entry in pending:
            try:
                rule = catalog.rule(entry.rule_id)
                abs_path = str(self.bases.to_abs(entry.path))
                st = StatInfo.from_stat(os.lstat(abs_path))
                found_now = self._classify(classifier, context_cache, entry, rule, st)
            except (KeyError, OSError):
                continue   # gone or unknown to the catalog: run() skips it for its own reason
            if found_now is None or found_now.rule_id != entry.rule_id or found_now.tier.value == 'protected' \
                    or entry.action not in rule.actions:
                # Same test as run(): the verdict changed since the scan, so this entry will be kept.
                slot = blockers.setdefault('rules_changed', {'count': 0, 'alloc': 0})
                slot['count'] += 1
                slot['alloc'] += entry.alloc
                blocked += 1
                blocked_alloc += entry.alloc
                continue
            apps = self._required_apps(plan, entry, found_now)
            found = guard.blockers(entry, rule, abs_path, entry.unit == Unit.BUNDLE, st, apps)
            for reason in found:
                slot = blockers.setdefault(reason, {'count': 0, 'alloc': 0})
                slot['count'] += 1
                slot['alloc'] += entry.alloc
                bundle = reason.partition(':')[2]
                if reason.startswith('app_running:') and apps.get(bundle):
                    slot['app'] = apps[bundle]
            if found:
                blocked += 1
                blocked_alloc += entry.alloc
        trash_needed = any(e.action == Action.TRASH for e in pending)
        archive_alloc = sum(e.alloc for e in pending if e.action == Action.ARCHIVE)
        archive: Dict[str, Any] = {'needed': 0, 'free': None, 'ok': True}
        if archive_alloc:
            need = int(ARCHIVE_SPACE_FACTOR * archive_alloc) + actions.FREE_RESERVE
            free = self.mac.statfs_free(str(self.data_dir))
            archive = {'needed': need, 'free': free, 'ok': free >= need}
        trash_ok = (not trash_needed) or self.mac.trash_available()
        expired = now >= plan.expires_at
        runnable = plan.state in (PlanState.DRAFT, PlanState.PAUSED)
        warnings: List[Dict[str, Any]] = []
        if not trash_ok:
            warnings.append({'key': 'warning.trash_missing'})
        if not archive['ok']:
            warnings.append({'key': 'warning.archive_space', 'needed': archive['needed'], 'free': archive['free']})
        return {
            'plan_id': plan_id, 'state': plan.state.value, 'revision': plan.revision, 'pending': len(pending),
            'alloc': sum(e.alloc for e in pending), 'expired': expired, 'hashes': unchanged,
            'hashes_ok': all(unchanged.values()), 'blockers': blockers, 'blocked_entries': blocked,
            'blocked_alloc': blocked_alloc, 'trash': {'needed': trash_needed, 'available': trash_ok},
            'archive': archive, 'warnings': warnings,
            'ready': runnable and not expired and all(unchanged.values()) and trash_ok and archive['ok'],
        }

    # run ---------------------------------------------------------------------------------------------------

    def run(self, plan_id: str, revision: int, entries_digest: str, control: Control,
            on_event: OnEvent = None) -> Dict[str, Any]:
        plan = self.store.plan(plan_id)
        if plan.state == PlanState.RUNNING:
            raise StewardError(ErrorCode.IN_PROGRESS, 'error.IN_PROGRESS')
        if plan.state not in (PlanState.DRAFT, PlanState.PAUSED):
            raise StewardError(ErrorCode.INVALID, 'error.plan_state', state=plan.state.value)
        catalog, facts, hashes = self._current()
        validate_for_run(plan, hashes, self.clock(), revision, entries_digest)
        if self.lock is not None and not self.lock.held:
            raise RuntimeError('the operation lock must be held while a plan runs')
        pending = self.store.entries(plan_id, EntryState.PENDING)
        classifier = self.classifier
        if classifier is None or classifier.facts_hash != hashes['facts_hash'] or self.catalog.hash != catalog.hash:
            # Whatever the caller built may predate a change that validate_for_run just accepted.
            classifier = Classifier(catalog, facts, self.bases, self.settings)
        staging_fd = actions.private_dir(self.data_dir / 'staging' / plan_id)
        try:
            return self._run(plan, pending, catalog, classifier, staging_fd, control, on_event)
        finally:
            os.close(staging_fd)
            with contextlib.suppress(OSError):   # not empty means something was left for the user; keep it
                os.rmdir(self.data_dir / 'staging' / plan_id)

    def _run(self, plan: Plan, pending: List[Entry], catalog: Catalog, classifier: Classifier, staging_fd: int,
             control: Control, on_event: OnEvent) -> Dict[str, Any]:
        plan_id = plan.id
        staging_dev = os.fstat(staging_fd).st_dev
        bases_dev: Dict[str, int] = {}
        for prefix in sorted({e.path.partition('/')[0] for e in pending}):
            bases_dev[prefix] = os.stat(self.bases.to_abs(prefix)).st_dev
            if bases_dev[prefix] != staging_dev:
                raise StewardError(ErrorCode.INVALID, 'error.staging_other_volume', base=prefix)
        archive_entries = [e for e in pending if e.action == Action.ARCHIVE]
        if archive_entries:
            adir = actions.private_dir(self.data_dir / 'archives')
            os.close(adir)
            if os.stat(self.data_dir / 'archives').st_dev != staging_dev:
                raise StewardError(ErrorCode.INVALID, 'error.staging_other_volume', base='archives')
            need = int(ARCHIVE_SPACE_FACTOR * sum(e.alloc for e in archive_entries)) + actions.FREE_RESERVE
            free = self.mac.statfs_free(str(self.data_dir / 'archives'))
            if free < need:
                raise StewardError(ErrorCode.UNAVAILABLE, 'error.no_space', needed=need, free=free)
        if any(e.action == Action.TRASH for e in pending) and not self.mac.trash_available():
            raise StewardError(ErrorCode.UNAVAILABLE, 'error.trash_unavailable')

        guard = Guard(self.mac, self.clock)
        ctx = ActionContext(store=self.store, bases=self.bases, mac=self.mac, audit=self.audit, clock=self.clock,
                            data_dir=self.data_dir, plan_id=plan_id, staging_fd=staging_fd,
                            checkpoint=lambda: control.checkpoint(blocking=False), hook=lambda n, e: self._hook(n, e))
        run = _Run(plan, control, on_event, classifier, catalog, guard, ctx, bases_dev, len(pending))
        started = time.monotonic()
        free_before = self.mac.statfs_free(str(self.bases.home))
        self.store.set_plan_state(plan_id, PlanState.RUNNING)
        self._audit('run.start', plan_id, entries=len(pending), alloc=sum(e.alloc for e in pending))
        self._emit(run, 'run.started', total=len(pending), alloc=sum(e.alloc for e in pending))
        final = PlanState.DONE
        for entry in pending:
            try:
                last_used_rule = catalog.rule(entry.rule_id).age_basis == AgeBasis.LAST_USED
            except KeyError:
                last_used_rule = False
            if last_used_rule:
                path = str(self.bases.to_abs(entry.path))
                run.lu_index[path] = len(run.lu_order)
                run.lu_order.append(path)
        try:
            guard.refresh(force=True)
            try:
                self._fetch_last_used(run, run.lu_order)
            except RuntimeError as e:   # each entry asks again for itself and is skipped if it still cannot be read
                self._audit('run.last_used_failed', plan_id, error=str(e))
            self._loop(run, pending)
        except Cancelled:
            for entry in self.store.entries(plan_id, EntryState.PENDING):
                self.store.finish_entry(plan_id, entry.seq, Outcome(EntryState.CANCELLED, 'cancelled'))
            final = PlanState.CANCELLED
        except Exception as e:
            # Snapshot failure, lost lock, store error, facts changed mid-run: stop where we are, loudly.
            self.store.set_plan_state(plan_id, PlanState.PAUSED, f'{type(e).__name__}: {e}')
            self._audit('run.abort', plan_id, error=f'{type(e).__name__}: {e}')
            self._emit(run, 'run.paused', error=f'{type(e).__name__}: {e}')
            raise
        self.store.set_plan_state(plan_id, final)
        summary = self._summary(run, final, free_before, time.monotonic() - started)
        self._audit('run.end', plan_id, state=final.value, counts=summary['counts'], freed=summary['freed'])
        self._emit(run, 'run.progress', done=run.done, total=run.total, bytes=run.bytes, current='')
        self._emit(run, 'run.finished', summary=summary)
        return summary

    def _loop(self, run: _Run, pending: List[Entry]) -> None:
        plan_id = run.plan.id
        control = run.control

        def on_pause() -> None:
            self.store.set_plan_state(plan_id, PlanState.PAUSED)
            self._audit('run.pause', plan_id)
            self._emit(run, 'run.paused')

        def on_resume() -> None:
            self._revalidate(run)
            self.store.set_plan_state(plan_id, PlanState.RUNNING)
            self._audit('run.resume', plan_id)
            self._emit(run, 'run.resumed')

        i = 0
        while i < len(pending):
            entry = pending[i]
            control.checkpoint(on_pause=on_pause, on_resume=on_resume)
            self._guard_run(run)
            outcome = self._entry(run, entry)
            if outcome is not None and outcome.state == EntryState.PENDING:
                continue   # rolled back by a pause or cancel: the checkpoint above waits or cancels, then it is retried
            i += 1
            run.done += 1
            if outcome is not None and outcome.state == EntryState.DONE and entry.action != Action.TRASH:
                run.bytes += entry.alloc
            now = time.monotonic()
            if now - run.last_progress >= PROGRESS_INTERVAL:
                run.last_progress = now
                self._emit(run, 'run.progress', done=run.done, total=run.total, bytes=run.bytes, current=entry.path)

    def _guard_run(self, run: _Run) -> None:
        """Between entries: the lock is still ours and the classifier's facts are recent."""
        if self.lock is not None and not self.lock.held:
            raise LockLost('the operation lock was lost during the run')
        if time.monotonic() - run.facts_at >= self.facts_ttl:
            self._refresh_facts(run)

    # one entry -----------------------------------------------------------------------------------------------

    def _entry(self, run: _Run, entry: Entry) -> Optional[Outcome]:
        plan_id = run.plan.id
        if not self.store.claim_entry(plan_id, entry.seq):
            return None   # excluded since the plan was made
        try:
            outcome = self._attempt(run, entry)
        except sqlite3.Error:
            raise   # the store itself failed, so there is nothing to write the entry's state to
        except (SnapshotError, LockLost):
            self.store.finish_entry(plan_id, entry.seq, Outcome(EntryState.PENDING, 'run_aborted'))
            raise
        except Interrupt:
            raise   # actions turn these into a rollback; one reaching here is a bug worth seeing
        except Exception as e:
            outcome = Outcome(EntryState.FAILED, f'fail.exception:{type(e).__name__}: {e}')
        self.store.finish_entry(plan_id, entry.seq, outcome)
        self._audit('entry.outcome', plan_id, seq=entry.seq, path=entry.path, action=entry.action.value,
                    state=outcome.state.value, reason=outcome.reason, freed=outcome.freed,
                    archive_id=outcome.archive_id)
        if outcome.state != EntryState.PENDING:
            self._emit(run, 'entry.done', seq=entry.seq, state=outcome.state.value, reason=outcome.reason,
                       freed=outcome.freed, path=entry.path)
        return outcome

    def _attempt(self, run: _Run, entry: Entry) -> Outcome:
        self._hook('after_claim', entry)
        try:
            rule = run.catalog.rule(entry.rule_id)
        except KeyError:
            return _skip('skip.rules_changed')
        abs_path = self.bases.to_abs(entry.path)
        base = self.bases.to_abs(entry.path.partition('/')[0])
        with contextlib.ExitStack() as stack:
            try:
                parent_fd, name = stack.enter_context(actions.open_parent_checked(abs_path, base, run.uid))
            except UnsafeParent:
                return _skip('skip.unsafe_parent')
            except OSError as e:
                if e.errno == errno.ENOENT:
                    return _skip('skip.vanished')
                if e.errno in (errno.ELOOP, errno.ENOTDIR):   # a symlink or a file where a folder should be
                    return _skip('skip.unsafe_parent')
                raise
            return self._checked(run, entry, rule, parent_fd, name, abs_path)

    def _checked(self, run: _Run, entry: Entry, rule: Any, parent_fd: int, name: str, abs_path: Path) -> Outcome:
        self._hook('after_parent', entry)
        bundle = entry.unit == Unit.BUNDLE
        try:
            st = actions.stat_at(parent_fd, name)
        except FileNotFoundError:
            return _skip('skip.vanished')
        if not (stat.S_ISDIR if bundle else stat.S_ISREG)(st.mode):   # a symlink is neither
            return _skip('skip.changed')
        if st.uid != run.uid:
            return _skip('skip.foreign_owner')
        prefix = entry.path.partition('/')[0]
        if st.dev != run.bases_dev[prefix] or st.dev != os.fstat(run.ctx.staging_fd).st_dev:
            return _skip('skip.other_volume')
        if st.flags & SF_DATALESS:
            return _skip('skip.dataless')
        if st.flags & LOCKED_FLAGS:
            return _skip('skip.locked')
        self._hook('after_lstat', entry)
        if not (actions.dir_matches(st, entry.fp) if bundle else actions.file_matches(st, entry.fp)):
            return _skip('skip.changed')
        self._hook('after_fingerprint', entry)

        match = self._classify(run.classifier, run.context_cache, entry, rule, st)
        if match is None or match.rule_id != entry.rule_id or match.tier.value == 'protected' \
                or entry.action not in rule.actions:
            return _skip('skip.rules_changed')
        self._hook('after_classify', entry)

        newest_ns = entry.fp['newest_ns'] if bundle else st.changed_ns
        if rule.age_basis == AgeBasis.LAST_USED:
            # The newest of: changed, the scan's Spotlight date (if its row is still there) and Spotlight's date now.
            item = self._plan_item(run.plan, entry)
            if item is not None and item.last_used_ns is not None:
                newest_ns = max(newest_ns, item.last_used_ns)
            try:
                fresh = self._fresh_last_used(run, str(abs_path))
            except LastUsedUnknown:
                return _skip('skip.last_used_unknown')
            if fresh is not None:
                newest_ns = max(newest_ns, int(fresh * 1e9))
        if (self.clock() - newest_ns / 1e9) / DAY_SECONDS < entry.threshold_days:
            return _skip('skip.too_recent')

        # Apps this very item needs quit: the fresh classification names them (generic Electron caches learn their
        # app from the detector facts), on top of the rule's own quit_apps.
        found = run.guard.blockers(entry, rule, str(abs_path), bundle, st,
                                   self._required_apps(run.plan, entry, match))
        if found:
            return _skip(f'skip.blocked:{found[0]}')
        self._hook('after_guard', entry)

        self._audit('entry.intent', run.plan.id, seq=entry.seq, path=entry.path, action=entry.action.value,
                    unit=entry.unit.value, rule_id=entry.rule_id, alloc=entry.alloc)
        name_taint = actions.name_taint_for(rule.kind)
        if entry.action == Action.DELETE:
            if bundle:
                return actions.delete_bundle(run.ctx, entry, parent_fd, name, st, name_taint)
            return actions.delete_file(run.ctx, entry, parent_fd, name, st)
        if entry.action == Action.ARCHIVE:
            return actions.archive(run.ctx, entry, parent_fd, name, st, name_taint)
        return actions.trash(run.ctx, entry, str(abs_path), name_taint)

    # summary -------------------------------------------------------------------------------------------------

    def _summary(self, run: _Run, final: PlanState, free_before: int, seconds: float) -> Dict[str, Any]:
        entries = self.store.entries(run.plan.id)
        counts = collections.Counter(e.state.value for e in entries)
        reasons = collections.Counter(e.reason for e in entries if e.state == EntryState.SKIPPED)
        by_action: Dict[str, Dict[str, int]] = {}
        removed = freed = trashed = 0
        for e in entries:
            if e.state != EntryState.DONE:
                continue
            slot = by_action.setdefault(e.action.value, {'count': 0, 'alloc': 0, 'freed': 0})
            slot['count'] += 1
            slot['alloc'] += e.alloc
            slot['freed'] += e.freed
            freed += e.freed
            if e.action == Action.TRASH:
                trashed += e.alloc
            else:
                removed += e.alloc
        free_after = self.mac.statfs_free(str(self.bases.home))
        snapshots: List[str] = []
        snapshots_error = ''
        try:
            snapshots = self.mac.snapshots()
        except RuntimeError as e:   # tmutil failing must not hide the result of a run that already happened
            snapshots_error = str(e)
        warnings: List[Dict[str, Any]] = []
        if snapshots:
            warnings.append({'key': 'warning.snapshots', 'count': len(snapshots)})
        if snapshots_error:
            warnings.append({'key': 'warning.snapshots_unknown', 'error': snapshots_error})
        if trashed:
            warnings.append({'key': 'warning.trash_not_freed', 'size': trashed})
        return {
            'plan_id': run.plan.id, 'state': final.value, 'counts': dict(counts), 'total': len(entries),
            'skipped_reasons': dict(reasons.most_common()), 'by_action': by_action, 'removed_alloc': removed,
            'freed': freed, 'moved_to_trash': trashed, 'free_before': free_before, 'free_after': free_after,
            'estimated_delta': freed, 'measured_delta': free_after - free_before, 'snapshots': snapshots,
            'warnings': warnings, 'seconds': round(seconds, 3),
        }

    # recovery ------------------------------------------------------------------------------------------------

    def recover(self) -> Dict[str, Any]:
        """Settle what a crashed run left behind. The caller holds the operation lock, so nothing here can be
        live: every .part file is stale, and an `in_progress` entry was cut off at an unknown point.

        Where the entry's item is tells how far it got. The rule that decides everything: an item that matches its
        fingerprint may be put back or finished; a bundle that does not match may be half removed, so it is never
        put back, and it is finished only when its removal was already authorised (a verified archive, or a delete).
        An archive is discarded only when the item it came from went back to its place unchanged."""
        report: Dict[str, Any] = {'entries': [], 'plans': [], 'parts_removed': 0}
        interrupted = self.store.entries_in_state(EntryState.IN_PROGRESS)
        running = self.store.plans_in_state(PlanState.RUNNING)
        adir = self.data_dir / 'archives'
        if interrupted and adir.is_dir():
            for part in sorted(adir.glob('*.part')):
                self._audit('part.removed', '', name=part.name, reason='recovery')
                part.unlink()
                report['parts_removed'] += 1
        plan_ids: Dict[str, None] = {p.id: None for p in running}
        for entry in interrupted:
            plan_ids[entry.plan_id] = None
            try:
                outcome = self._recover_entry(entry)
            except sqlite3.Error:
                raise
            except Exception as e:
                outcome = Outcome(EntryState.FAILED, f'fail.exception:{type(e).__name__}: {e}')
            self.store.finish_entry(entry.plan_id, entry.seq, outcome)
            self._audit('entry.recovered', entry.plan_id, seq=entry.seq, path=entry.path, action=entry.action.value,
                        state=outcome.state.value, reason=outcome.reason, archive_id=outcome.archive_id)
            report['entries'].append({'plan_id': entry.plan_id, 'seq': entry.seq, 'path': entry.path,
                                      'state': outcome.state.value, 'reason': outcome.reason})
        for plan_id in plan_ids:
            self.store.set_plan_state(plan_id, PlanState.PAUSED, 'interrupted')
            with contextlib.suppress(OSError):
                os.rmdir(self.data_dir / 'staging' / plan_id)
            report['plans'].append(plan_id)
        return report

    def _recover_ctx(self, entry: Entry, staging_fd: int) -> ActionContext:
        return ActionContext(store=self.store, bases=self.bases, mac=self.mac, audit=self.audit, clock=self.clock,
                             data_dir=self.data_dir, plan_id=entry.plan_id, staging_fd=staging_fd)

    def _recover_entry(self, entry: Entry) -> Outcome:
        staging = self.data_dir / 'staging' / entry.plan_id
        sname = str(entry.seq)
        orig = self.bases.to_abs(entry.path)
        base = self.bases.to_abs(entry.path.partition('/')[0])
        actions.settle_unregistered_archives(self._recover_ctx(entry, -1), entry)
        stored = [a for a in self.store.archives()
                  if a['plan_id'] == entry.plan_id and a['seq'] == entry.seq
                  and a['state'] == actions.ARCHIVE_STATE_STORED]
        verified: List[Optional[Dict[str, Any]]] = []

        def good_archive() -> Optional[Dict[str, Any]]:
            """The first stored archive that still verifies, checked once."""
            if not verified:
                verified.append(next((a for a in stored if self._archive_ok(entry, a)), None))
            return verified[0]

        if os.path.lexists(staging / sname):
            staging_fd = actions.private_dir(staging)
            try:
                return self._recover_staged(entry, staging_fd, sname, orig, base, stored, good_archive)
            finally:
                os.close(staging_fd)
        try:
            current: Optional[os.stat_result] = os.lstat(orig)
        except FileNotFoundError:
            current = None
        if current is not None and (current.st_dev, current.st_ino) == (entry.fp['dev'], entry.fp['ino']):
            # Never staged: the original is still there. An archive made on the way is only clutter.
            ctx = self._recover_ctx(entry, -1)
            for a in stored:
                actions.discard_archive(ctx, a['id'], a['fmt'], 'recovery: the original was never removed')
            return Outcome(EntryState.PENDING, 'interrupted')
        # The original is gone, and what stands at its path now (if anything) is not it.
        if entry.action != Action.ARCHIVE or not stored:
            return Outcome(EntryState.DONE, 'recovered.gone')   # unlinked just before the crash, or gone by itself
        row = good_archive()
        if row is None:
            return Outcome(EntryState.NEEDS_ATTENTION, f'attention.archive_unverified:{", ".join(a["id"] for a in stored)}')
        return Outcome(EntryState.DONE, 'archive.stored', freed=entry.reclaim - row['stored_bytes'],
                       archive_id=row['id'])

    def _archive_ok(self, entry: Entry, row: Mapping[str, Any]) -> bool:
        ctx = self._recover_ctx(entry, -1)
        try:
            actions.verify_archive_file(actions.archive_file_path(ctx, row), row['manifest'])
        except (actions.ArchiveCorrupt, OSError, StewardError):
            return False
        return True

    def _recover_staged(self, entry: Entry, staging_fd: int, sname: str, orig: Path, base: Path,
                        stored: List[Dict[str, Any]], good_archive: Callable[[], Optional[Dict[str, Any]]]) -> Outcome:
        ctx = self._recover_ctx(entry, staging_fd)
        bundle = entry.unit == Unit.BUNDLE
        archiving = entry.action == Action.ARCHIVE
        root = actions.stat_at(staging_fd, sname)
        if bundle:
            same_folder = actions.dir_matches(root, entry.fp)
            whole = False
            if same_folder:
                try:
                    name_taint = actions.name_taint_for(self.catalog.rule(entry.rule_id).kind)
                except KeyError:
                    name_taint = True
                summary = walk_bundle(str(ctx.staging_path / sname), entry.fp['dev'], os.getuid(), name_taint=name_taint)
                whole = actions.bundle_summary_matches(summary, root, entry.fp)
            unchanged = same_folder and whole
        else:
            same_folder = whole = unchanged = actions.file_matches(root, entry.fp)
        row = good_archive() if archiving and same_folder else None
        authorised = row is not None if archiving else entry.action == Action.DELETE   # trash never stages anything
        if authorised and (same_folder if bundle else unchanged):
            # The removal was authorised (a verified archive, or a plain delete) and staging is private, so a
            # bundle that no longer matches can only be one the crash interrupted half way through.
            return self._finish_removal(ctx, entry, sname, row)
        if bundle and same_folder and not whole:
            # An archive entry without a verified archive and a tree that is not whole: it may be partly removed,
            # and a partial tree must never go back looking like the real one.
            return Outcome(EntryState.NEEDS_ATTENTION, f'attention.staged_left:{ctx.staging_path / sname}')
        # Whole and unauthorised, or not the planned item at all: back to its place.
        try:
            with actions.open_parent_checked(orig, base, os.getuid()) as (pfd, name):
                left = actions.put_back(ctx, entry, pfd, name, sname)
        except (UnsafeParent, OSError) as e:
            actions.audit_event(ctx, 'staged_left', seq=entry.seq, path=entry.path, error=f'{type(e).__name__}: {e}')
            left = Outcome(EntryState.NEEDS_ATTENTION, f'attention.staged_left:{ctx.staging_path / sname}')
        if left is not None:
            return left
        if unchanged:
            for a in stored:   # the item matched its fingerprint and is back, so an archive of it is clutter
                actions.discard_archive(ctx, a['id'], a['fmt'], 'recovery: the item went back unchanged')
            return Outcome(EntryState.PENDING, 'interrupted')
        return _skip('skip.changed')

    def _finish_removal(self, ctx: ActionContext, entry: Entry, sname: str,
                        row: Optional[Dict[str, Any]]) -> Outcome:
        actions.audit_event(ctx, 'recover.intent', seq=entry.seq, path=entry.path, action=entry.action.value,
                            archive_id=row['id'] if row else None)
        archive_id = row['id'] if row else None
        freed = entry.reclaim - row['stored_bytes'] if row else entry.reclaim
        if entry.unit == Unit.BUNDLE:
            return actions.remove_staged_tree(ctx, entry, sname, freed, archive_id)
        os.unlink(sname, dir_fd=ctx.staging_fd)
        return Outcome(EntryState.DONE, 'archive.stored' if row else '', freed=freed, archive_id=archive_id)
