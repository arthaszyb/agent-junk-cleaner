"""Scanner: walks the catalog's roots once, classifies what it finds and writes items and groups to the store."""
from __future__ import annotations

import dataclasses
import errno
import os
import stat
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from .catalog import Catalog, ScanRoot, is_report_only
from .classify import UNKNOWN, Classifier, protect_group_key, split_group_key
from .fsutil import SF_DATALESS, walk_bundle
from .macos import MacOS
from .model import (AgeBasis, Domain, ErrorCode, Evidence, Group, Item, Kind, Match, Rule, StatInfo, StewardError, Tier,
                    Unit)
from .paths import Bases, item_key
from .settings import Settings, settings_hash
from .store import Store

ROLLUP_LIMIT = 50 * 1024 * 1024        # protected files below this size are rolled up per group
BATCH_ROWS = 5000                      # items held in memory before they are written to the store
PROGRESS_INTERVAL = 0.25
EVIDENCE_PER_GROUP = 5
# Kinds whose folders legitimately contain files that look like databases or credentials (engine and
# runtime bundles carry certificate stores, update caches carry whatever the vendor shipped).
NO_NAME_TAINT = frozenset((Kind.OLD_VERSION, Kind.UPDATE_LEFTOVER, Kind.PROJECT_DEPS, Kind.RUNTIME))

_CHECK_EVERY = 256   # files between cancel and progress checks
_now = time.monotonic  # tests replace it to drive the progress throttle
_DENIED = (errno.EPERM, errno.EACCES)
_GONE = (errno.ENOENT, errno.ENOTDIR)


def safe_path(path: str) -> str:
    """A path that may hold undecodable bytes, rendered so that it can be stored in SQLite."""
    return path.encode('utf-8', 'surrogateescape').decode('utf-8', 'backslashreplace')


class _Roll:
    """Accumulator for the protected files of one group."""

    def __init__(self, match: Match, directory: str):
        self.match = match
        self.prefix = directory
        self.files = self.size = self.alloc = self.reclaim = self.newest = 0
        self.evidence: List[Evidence] = []

    def add(self, directory: str, match: Match, st: StatInfo) -> None:
        while not (directory == self.prefix or directory.startswith(self.prefix + '/')):
            self.prefix = self.prefix.rpartition('/')[0]
        self.files += 1
        self.size += st.size
        self.alloc += st.alloc
        if st.nlink <= 1:
            self.reclaim += st.alloc
        if st.changed_ns > self.newest:
            self.newest = st.changed_ns
        for ev in match.evidence:
            if ev not in self.evidence and len(self.evidence) < EVIDENCE_PER_GROUP:
                self.evidence.append(ev)


class _Group:
    """What the scan knows about one group so far; the Group row is built from it at the end."""

    def __init__(self, item: Item):
        self.tier, self.domain = item.tier, item.domain    # groups never mix tiers: the first item decides
        self.items = self.alloc = self.reclaim = 0
        self.newest = self.oldest = item.newest_ns
        self.evidence: List[Evidence] = []


class _Scan:
    def __init__(self, store: Store, classifier: Classifier, catalog: Catalog, bases: Bases, mac: MacOS,
                 settings: Settings, domains: Set[str], scan_id: int,
                 progress: Optional[Callable[[Dict[str, Any]], None]], cancel: Optional[Callable[[], bool]]):
        self.store, self.classifier, self.catalog = store, classifier, catalog
        self.bases, self.mac, self.settings = bases, mac, settings
        self.domains = domains
        self.scan_id = scan_id
        self.progress = progress
        self.cancel = cancel
        self.uid = os.getuid()
        self.batch: List[Item] = []               # items not yet written to the store
        self.last_used: List[Item] = []           # items that wait for the last-used dates (a few per scan)
        self.last_used_rules = frozenset(r.id for r in catalog.rules if r.age_basis == AgeBasis.LAST_USED)
        self.aggregates: Dict[str, _Group] = {}
        self.attributed = 0
        self.rolls: Dict[Tuple[str, str], _Roll] = {}
        self.seen: Set[Tuple[int, int]] = set()   # (dev, ino) of hard-linked files already counted
        self.errors: List[Tuple[str, str]] = []
        self.files = 0
        self.alloc = 0
        self.phase = 'scan'
        self.root = ''
        self._last_emit: Optional[float] = None
        self._since_check = 0

    # progress and cancel ------------------------------------------------------------------------------

    def _checkpoint(self, current: str = '') -> None:
        if self.cancel is not None and self.cancel():
            raise StewardError(ErrorCode.IN_PROGRESS, 'error.scan_cancelled')
        if self.progress is not None:
            now = _now()
            if self._last_emit is None or now - self._last_emit >= PROGRESS_INTERVAL:
                self._last_emit = now
                self.progress({'phase': self.phase, 'root': self.root, 'files': self.files,
                               'alloc': self.alloc, 'current': current})

    def _error(self, logical: str, code: str) -> None:
        self.errors.append((safe_path(logical), code))

    # walking ------------------------------------------------------------------------------------------

    def run(self) -> None:
        picked = sorted({r.path: r for r in self.catalog.roots if r.domain.value in self.domains}.values(),
                        key=lambda r: r.path)
        # A root inside another enabled root is covered by the outer walk; scanning it again would
        # double-count everything below it.
        top = [r for r in picked if not any(o is not r and r.path.startswith(o.path + '/') for o in picked)]
        for root in top:
            self._scan_root(root)
        self.phase = 'last_used'
        self._checkpoint()
        self._apply_last_used()

    def _scan_root(self, root: ScanRoot) -> None:
        self.root = root.path
        files_before, alloc_before = self.files, self.alloc
        status = self._walk_root(root)
        self.store.add_scan_root(self.scan_id, root.path, root.domain, status,
                                 self.files - files_before, self.alloc - alloc_before)

    def _walk_root(self, root: ScanRoot) -> str:
        if self.classifier.privacy_skipped(root.path):
            self._error(root.path, 'skipped_privacy')
            return 'skipped_privacy'
        abs_root = str(self.bases.to_abs(root.path))
        try:
            st = os.lstat(abs_root)
        except OSError as e:
            if e.errno in _GONE:
                return 'missing'
            if e.errno in _DENIED:
                self._error(root.path, 'permission_denied')
                return 'skipped_privacy'
            raise
        # A root reached through a symlink (itself or an ancestor) is somebody's redirect, not our tree.
        if stat.S_ISLNK(st.st_mode) or os.path.realpath(abs_root) != abs_root:
            self._error(root.path, 'root_symlink')
            return 'error'
        if not stat.S_ISDIR(st.st_mode):
            self._error(root.path, 'not_a_directory')
            return 'error'
        if st.st_dev != os.stat(self.bases.home).st_dev:
            self._error(root.path, 'other_volume')
            return 'error'
        if getattr(st, 'st_flags', 0) & SF_DATALESS:
            self._error(root.path, 'dataless_skipped')
            return 'error'
        stack: List[Tuple[str, str, bool]] = [(abs_root, root.path, False)]
        status = 'ok'
        while stack:
            directory, logical, in_tree = stack.pop()
            self._checkpoint(logical)
            try:
                with os.scandir(directory) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError as e:
                if e.errno in _DENIED:
                    self._error(logical, 'permission_denied')
                    if logical == root.path:
                        status = 'skipped_privacy'
                elif e.errno in _GONE:
                    self._error(logical, 'vanished')
                else:
                    raise
                continue
            # A .git entry (a folder, or the pointer file of a worktree) makes this whole subtree a work tree.
            in_tree = in_tree or any(e.name == '.git' for e in entries)
            db_dir = self.classifier.has_database_markers(self._plain_file_names(entries))
            subdirs: List[Tuple[str, str, bool]] = []
            for entry in entries:
                self._entry(entry, logical, st.st_dev, root.domain, subdirs, in_tree, db_dir)
            stack.extend(reversed(subdirs))  # keeps the walk in name order
        return status

    @staticmethod
    def _plain_file_names(entries: List['os.DirEntry[str]']) -> List[str]:
        names = []
        for e in entries:
            try:
                if not e.is_symlink() and not e.is_dir(follow_symlinks=False):
                    names.append(e.name)
            except OSError:
                continue   # the entry loop reports it
        return names

    def _entry(self, entry: 'os.DirEntry[str]', logical: str, root_dev: int, domain: Domain,
               subdirs: List[Tuple[str, str, bool]], in_tree: bool, db_dir: bool) -> None:
        name = entry.name
        child = f'{logical}/{name}'
        if not name.isascii():
            try:
                name.encode('utf-8')
            except UnicodeEncodeError:
                self._error(child, 'undecodable_name')
                return
        try:
            if entry.is_symlink():
                return
            if entry.is_dir(follow_symlinks=False):
                self._directory(entry, child, root_dev, domain, subdirs, in_tree)
            else:
                self._file(entry, logical, child, domain, in_tree, db_dir)
        except OSError as e:
            if e.errno in _DENIED:
                self._error(child, 'permission_denied')
            elif e.errno in _GONE:
                self._error(child, 'vanished')
            else:
                raise

    def _directory(self, entry: 'os.DirEntry[str]', child: str, root_dev: int, domain: Domain,
                   subdirs: List[Tuple[str, str, bool]], in_tree: bool) -> None:
        if self.classifier.privacy_skipped(child):
            self._error(child, 'skipped_privacy')
            return
        st = StatInfo.from_stat(entry.stat(follow_symlinks=False))
        if st.dev != root_dev:
            self._error(child, 'other_volume')
            return
        match = self.classifier.classify_dir(child, st, domain, in_git_tree=in_tree)
        if match is None:
            if st.flags & SF_DATALESS:   # listing a cloud placeholder would download it
                self._error(child, 'dataless_skipped')
                return
            subdirs.append((entry.path, child, in_tree))
            return
        summary = walk_bundle(entry.path, root_dev, self.uid, name_taint=match.kind not in NO_NAME_TAINT,
                              cancel=self.cancel)
        tier, group_key, evidence = match.tier, match.group_key, list(match.evidence)
        # Taint guards what a cleanup would remove. A report-only rule removes nothing, and the folders it names
        # (module caches, simulator data) are full of files that look like databases or credentials. One that can move
        # a folder to the Trash (manual_actions) removes something, so its folders are guarded like any other.
        rule = self.catalog.rule(match.rule_id)
        taint = '' if is_report_only(rule) and not rule.manual_actions else summary.taint
        if taint:
            if tier != Tier.PROTECTED:
                group_key = protect_group_key(group_key)
            tier = Tier.PROTECTED
            evidence.append(Evidence('evidence.tainted', {'reason': taint}))
        self._add(Item(
            scan_id=self.scan_id, key=item_key(child), path=child, rule_id=match.rule_id, group_key=group_key,
            tier=tier, domain=match.domain, kind=match.kind, unit=Unit.BUNDLE, dev=st.dev, ino=st.ino,
            size=summary.size, alloc=summary.alloc, reclaim=summary.reclaim, files=summary.files,
            mtime_ns=st.mtime_ns, birth_ns=st.birth_ns, newest_ns=max(summary.newest_ns, st.changed_ns),
            last_used_ns=None, nlink=st.nlink, uid=st.uid, flags=st.flags, taint=taint,
            evidence=tuple(evidence)))
        self.files += summary.files
        self.alloc += summary.alloc

    def _file(self, entry: 'os.DirEntry[str]', parent: str, child: str, domain: Domain, in_tree: bool,
              db_dir: bool) -> None:
        st = StatInfo.from_stat(entry.stat(follow_symlinks=False))
        if st.nlink > 1:
            ident = (st.dev, st.ino)
            if ident in self.seen:
                return  # another path of the same data: counting it again would overstate usage
            self.seen.add(ident)
        match = self.classifier.classify(child, st, False, domain, in_git_tree=in_tree, db_dir=db_dir)
        self.files += 1
        self.alloc += st.alloc
        self._since_check += 1
        if self._since_check >= _CHECK_EVERY:
            self._since_check = 0
            self._checkpoint(parent)
        if match.tier == Tier.PROTECTED and st.size < ROLLUP_LIMIT:
            base = child.partition('/')[0]
            roll = self.rolls.get((match.group_key, base))
            if roll is None:
                roll = self.rolls[(match.group_key, base)] = _Roll(match, parent)
            roll.add(parent, match, st)
            return
        self._add(Item(
            scan_id=self.scan_id, key=item_key(child), path=child, rule_id=match.rule_id,
            group_key=match.group_key, tier=match.tier, domain=match.domain, kind=match.kind, unit=Unit.FILE,
            dev=st.dev, ino=st.ino, size=st.size, alloc=st.alloc, reclaim=st.alloc if st.nlink <= 1 else 0,
            files=1, mtime_ns=st.mtime_ns, birth_ns=st.birth_ns, newest_ns=st.changed_ns, last_used_ns=None,
            nlink=st.nlink, uid=st.uid, flags=st.flags, evidence=match.evidence))

    # items --------------------------------------------------------------------------------------------

    def _account(self, item: Item) -> None:
        """Fold the item into its group's totals. Done in walk order, as the group takes its tier and domain from
        the first item it sees."""
        g = self.aggregates.get(item.group_key)
        if g is None:
            g = self.aggregates[item.group_key] = _Group(item)
        g.items += 1
        g.alloc += item.alloc
        g.reclaim += item.reclaim
        if item.newest_ns > g.newest:
            g.newest = item.newest_ns
        if item.newest_ns < g.oldest:
            g.oldest = item.newest_ns
        for ev in item.evidence:
            if len(g.evidence) >= EVIDENCE_PER_GROUP:
                break
            if ev not in g.evidence:
                g.evidence.append(ev)
        self.attributed += item.alloc

    def _add(self, item: Item) -> None:
        self._account(item)
        if item.rule_id in self.last_used_rules:
            self.last_used.append(item)   # written after the walk, with the date Spotlight has for it
        else:
            self._write(item)

    def _write(self, item: Item) -> None:
        self.batch.append(item)
        if len(self.batch) >= BATCH_ROWS:
            self._flush()

    def _flush(self) -> None:
        if self.batch:
            self.store.add_items(self.scan_id, self.batch)
            self.batch = []

    # after the walk -----------------------------------------------------------------------------------

    def _rollup_items(self) -> List[Item]:
        out: List[Item] = []
        for (group_key, base), roll in sorted(self.rolls.items()):
            m = roll.match
            # The first base keeps the documented key; a group that spans $TMPDIR or $CACHEDIR as well gets
            # one rollup per base, because a rollup's path must stay inside one base.
            seed = f'rollup:{group_key}' if base == '~' else f'rollup:{group_key}|{base}'
            out.append(Item(
                scan_id=self.scan_id, key=item_key(seed), path=roll.prefix, rule_id=m.rule_id,
                group_key=group_key, tier=Tier.PROTECTED, domain=m.domain, kind=m.kind, unit=Unit.BUNDLE,
                dev=0, ino=0, size=roll.size, alloc=roll.alloc, reclaim=roll.reclaim, files=roll.files,
                mtime_ns=roll.newest, birth_ns=0, newest_ns=roll.newest, last_used_ns=None, nlink=0, uid=0,
                flags=0, evidence=tuple(roll.evidence)))
        return out

    def _apply_last_used(self) -> None:
        wanted, self.last_used = self.last_used, []
        if not wanted:
            return
        abs_paths = [str(self.bases.to_abs(i.path)) for i in wanted]
        used = self.mac.last_used(abs_paths)
        for i, p in zip(wanted, abs_paths):
            self._write(dataclasses.replace(i, last_used_ns=None if used.get(p) is None else int(used[p] * 1e9)))

    def finish(self) -> None:
        """Write what is still waiting: the rollups, which exist only once the walk is over, and the last batch."""
        for item in self._rollup_items():
            self._account(item)
            self._write(item)
        self._flush()

    def groups(self) -> List[Group]:
        out: List[Group] = []
        for key in sorted(self.aggregates):
            g = self.aggregates[key]
            rule_id, suffix = split_group_key(key)
            try:
                rule: Optional[Rule] = self.catalog.rule(rule_id)
            except KeyError:
                if rule_id != UNKNOWN:
                    raise
                rule = None
            protected = g.tier == Tier.PROTECTED
            out.append(Group(
                scan_id=self.scan_id, key=key, rule_id=rule_id,
                provider=rule.provider if rule else 'system', domain=g.domain, tier=g.tier,
                kind=rule.kind if rule else Kind.UNKNOWN, unit=rule.unit if rule else Unit.FILE,
                label_args={'path': suffix} if suffix else {}, items=g.items, alloc=g.alloc,
                reclaim=g.reclaim, newest_ns=g.newest, oldest_ns=g.oldest,
                actions=() if protected or rule is None else rule.actions,
                default_action=None if protected or rule is None else rule.default_action,
                min_age_floor_days=rule.min_age_floor_days if rule else 0.0,
                default_min_age_days=rule.default_min_age_days if rule else None,
                age_basis=rule.age_basis if rule else AgeBasis.CHANGED,
                hint=rule.hint if rule else None, evidence=tuple(g.evidence)))
        return out


def scan(store: Store, classifier: Classifier, catalog: Catalog, bases: Bases, mac: MacOS, settings: Settings,
         domains: Iterable[Any], progress: Optional[Callable[[Dict[str, Any]], None]] = None,
         cancel: Optional[Callable[[], bool]] = None, prune: bool = True) -> int:
    """`prune=False` keeps the previous scan's rows until the caller runs store.prune_scans()."""
    enabled = sorted({Domain(d).value for d in domains})
    scan_id = store.begin_scan(catalog.hash, classifier.facts_hash, settings_hash(settings), enabled)
    run = _Scan(store, classifier, catalog, bases, mac, settings, set(enabled), scan_id, progress, cancel)
    try:
        run.run()
        run.finish()
        store.add_groups(scan_id, run.groups())
        for path, code in run.errors:
            store.add_scan_error(scan_id, path, code)
        home = str(bases.home)
        total, _used = mac.volume_usage(home)
        store.finish_scan(scan_id, 'done', total, mac.statfs_free(home), run.attributed, prune=prune)
    except StewardError as e:
        store.discard_items(scan_id)   # a scan that did not finish leaves no items behind
        cancelled = e.message_key == 'error.scan_cancelled'
        store.finish_scan(scan_id, 'cancelled' if cancelled else 'failed', None, None, None,
                          error='' if cancelled else str(e))
        raise
    except BaseException as e:
        store.discard_items(scan_id)
        store.finish_scan(scan_id, 'failed', None, None, None, error=f'{type(e).__name__}: {e}')
        raise
    return scan_id
