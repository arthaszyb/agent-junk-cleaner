"""Actions that change the disk: delete, archive, trash, restore, purge.

Nothing here decides *whether* to act; the executor has already run its checklist. What lives here is how to act
without ever holding the only copy in a state that cannot be undone: an item is renamed (never copied) into a
private staging folder, checked there against its plan fingerprint, and only then unlinked. Anything unexpected
puts it back under its original name; if that name is taken, the item stays in staging and the entry says so."""
from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import gzip
import hashlib
import json
import os
import secrets
import shutil
import stat
import sys
import tarfile
import zlib
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple

from .audit import Audit
from .fsutil import open_parent_nofollow, rename_excl, walk_bundle
from .macos import MacOS
from .model import BundleSummary, Entry, EntryState, ErrorCode, Outcome, StatInfo, StewardError, Unit
from .paths import Bases
from .scanner import NO_NAME_TAINT
from .store import Store

CHUNK = 8 * 1024 * 1024          # bytes between pause checks while archiving
GZIP_LEVEL = 6
FREE_RESERVE = 1024 ** 3         # an archive run keeps this much free on top of what it still has to write
ARCHIVE_STATE_STORED = 'stored'


class Interrupt(Exception):
    """The user asked the run to stop while an action was in flight; the action rolls itself back."""


class Cancelled(Interrupt):
    pass


class Paused(Interrupt):
    pass


class UnsafeParent(Exception):
    """An ancestor below the base belongs to somebody else."""


class DiskFull(Exception):
    pass


class UnsupportedMember(Exception):
    """A bundle holds something a tar archive here cannot round-trip (device, fifo, socket)."""


class ArchiveCorrupt(Exception):
    pass


class _SourceChanged(Exception):
    """The source changed while it was being archived."""


def _no_hook(name: str, entry: Any) -> None:
    return None


def fullsync(fd: int) -> None:
    """Flush to the disk itself. fsync() on macOS only reaches the drive's cache, so a power cut could still lose
    a file whose original was deleted on the strength of it. Elsewhere (no F_FULLFSYNC) fsync is the best there is."""
    if hasattr(fcntl, 'F_FULLFSYNC'):
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    else:
        os.fsync(fd)


def write_all(fd: int, data: bytes) -> None:
    """os.write until everything is written; a short write (full disk, signal) must not pass for a complete file."""
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


@dataclasses.dataclass
class ActionContext:
    store: Store
    bases: Bases
    mac: MacOS
    audit: Audit
    clock: Callable[[], float]
    data_dir: Path
    plan_id: str = ''
    staging_fd: int = -1                                   # opened by the executor, 0700, owned by us
    checkpoint: Callable[[], None] = lambda: None          # raises Interrupt when the user paused or cancelled
    hook: Callable[[str, Any], None] = _no_hook            # fault-injection seam

    @property
    def staging_path(self) -> Path:
        return self.data_dir / 'staging' / self.plan_id

    @property
    def archives_path(self) -> Path:
        return self.data_dir / 'archives'


# filesystem helpers ------------------------------------------------------------------------------------------

def stat_at(dir_fd: int, name: str) -> StatInfo:
    """lstat through a directory fd. The one seam tests replace to fake owners and flags without root."""
    return StatInfo.from_stat(os.stat(name, dir_fd=dir_fd, follow_symlinks=False))


@contextlib.contextmanager
def open_parent_checked(abs_path: Any, base: Any, uid: int) -> Iterator[Tuple[int, str]]:
    """open_parent_nofollow, plus: every ancestor below `base` must belong to us or to root."""
    below = len([p for p in str(base).split('/') if p])

    def inspect(fd: int, index: int) -> None:
        if index >= below:
            owner = os.fstat(fd).st_uid
            if owner not in (uid, 0):
                raise UnsafeParent(f'ancestor owned by uid {owner}')

    with open_parent_nofollow(abs_path, inspect) as found:
        yield found


def private_dir(path: Path) -> int:
    """Create (if needed) and open a directory that only we may use. Raises if it exists but is not ours."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise StewardError(ErrorCode.INVALID, 'error.private_dir', path=str(path))
    return fd


def file_matches(st: StatInfo, fp: Mapping[str, int]) -> bool:
    return (stat.S_ISREG(st.mode) and st.nlink == 1
            and all(getattr(st, k) == fp[k] for k in ('dev', 'ino', 'size', 'mtime_ns', 'birth_ns')))


def dir_matches(st: StatInfo, fp: Mapping[str, int]) -> bool:
    return stat.S_ISDIR(st.mode) and st.dev == fp['dev'] and st.ino == fp['ino']


def name_taint_for(kind: Any) -> bool:
    """The scanner skips name-based taint (databases, credentials) for these kinds; a re-walk must agree."""
    return kind not in NO_NAME_TAINT


def bundle_summary_matches(summary: BundleSummary, root: StatInfo, fp: Mapping[str, int]) -> bool:
    newest = max(summary.newest_ns, root.changed_ns)   # same formula as the scanner's Item.newest_ns
    return (summary.taint == '' and summary.files == fp['files'] and summary.alloc == fp['alloc']
            and newest == fp['newest_ns'])


def audit_event(ctx: ActionContext, event: str, **fields: Any) -> None:
    ctx.audit.write({'event': event, 'plan_id': ctx.plan_id, **fields})


# staging ---------------------------------------------------------------------------------------------------

def _stage(ctx: ActionContext, entry: Entry, parent_fd: int, name: str) -> str:
    sname = str(entry.seq)
    rename_excl(parent_fd, name, ctx.staging_fd, sname)
    return sname


def put_back(ctx: ActionContext, entry: Entry, parent_fd: int, name: str, sname: str) -> Optional[Outcome]:
    """Rename a staged item back under its original name. None on success; otherwise the entry needs attention
    and the item stays where it is."""
    try:
        rename_excl(ctx.staging_fd, sname, parent_fd, name)
    except OSError as e:
        audit_event(ctx, 'staged_left', seq=entry.seq, path=entry.path, error=f'{type(e).__name__}: {e}')
        return Outcome(EntryState.NEEDS_ATTENTION, f'attention.staged_left:{ctx.staging_path / sname}')
    return None


def _guarded_put_back(ctx: ActionContext, entry: Entry, parent_fd: int, name: str, sname: str,
                      result: Outcome) -> Outcome:
    """Put the item back and report `result`, unless putting it back failed."""
    return put_back(ctx, entry, parent_fd, name, sname) or result


def _rmtree_handler(root: str, uid: int) -> Callable[[Any, str, BaseException], None]:
    def handle(func: Any, path: str, exc: BaseException) -> None:
        if not isinstance(exc, PermissionError):
            raise exc
        # A read-only folder (0555, as Go's module cache makes them) blocks removing what is inside it. Only
        # folders of ours inside the staged tree are ever made writable.
        for candidate in (path, os.path.dirname(path)):
            if candidate != root and not candidate.startswith(root + '/'):
                continue
            try:
                info = os.lstat(candidate)
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode) and info.st_uid == uid:
                os.chmod(candidate, info.st_mode | 0o700)
        func(path)   # a second failure propagates and aborts the removal
    return handle


def rmtree_ours(path: str) -> None:
    handler = _rmtree_handler(path, os.getuid())
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=handler)
    else:
        shutil.rmtree(path, onerror=lambda func, p, info: handler(func, p, info[1]))


def remove_staged_tree(ctx: ActionContext, entry: Entry, sname: str, freed: int,
                        archive_id: Optional[str] = None) -> Outcome:
    staged = str(ctx.staging_path / sname)
    try:
        rmtree_ours(staged)
    except Exception as e:
        # A half-removed bundle is not put back: it would look installed and be broken.
        audit_event(ctx, 'staged_left', seq=entry.seq, path=entry.path, error=f'{type(e).__name__}: {e}')
        return Outcome(EntryState.NEEDS_ATTENTION, f'attention.partial_delete:{staged}', archive_id=archive_id)
    return Outcome(EntryState.DONE, 'archive.stored' if archive_id else '', freed=freed, archive_id=archive_id)


# delete ----------------------------------------------------------------------------------------------------

def delete_file(ctx: ActionContext, entry: Entry, parent_fd: int, name: str, st: StatInfo) -> Outcome:
    sname = _stage(ctx, entry, parent_fd, name)
    try:
        # The staging folder is private, so what we see here cannot change under us any more.
        changed = not file_matches(stat_at(ctx.staging_fd, sname), entry.fp)
        if not changed:
            ctx.hook('before_unlink', entry)
            os.unlink(sname, dir_fd=ctx.staging_fd)
    except Exception:
        left = put_back(ctx, entry, parent_fd, name, sname)
        if left is not None:
            return left
        raise
    if changed:
        return _guarded_put_back(ctx, entry, parent_fd, name, sname, Outcome(EntryState.SKIPPED, 'skip.changed'))
    return Outcome(EntryState.DONE, '', freed=entry.reclaim)


def _stage_verified_bundle(ctx: ActionContext, entry: Entry, parent_fd: int, name: str,
                           name_taint: bool) -> Tuple[Optional[Outcome], str, Optional[BundleSummary], Optional[StatInfo]]:
    """Rename a bundle into staging and re-walk it there. Returns (outcome, staged name, summary, root stat);
    a non-None outcome means the bundle is already back (or flagged) and nothing more may be done with it."""
    sname = _stage(ctx, entry, parent_fd, name)
    try:
        root = stat_at(ctx.staging_fd, sname)
        summary = None
        ok = dir_matches(root, entry.fp)
        if ok:
            summary = walk_bundle(str(ctx.staging_path / sname), entry.fp['dev'], os.getuid(), name_taint=name_taint)
            ok = bundle_summary_matches(summary, root, entry.fp)
    except Exception:
        left = put_back(ctx, entry, parent_fd, name, sname)
        if left is not None:
            return left, sname, None, None
        raise
    if not ok:
        return (_guarded_put_back(ctx, entry, parent_fd, name, sname, Outcome(EntryState.SKIPPED, 'skip.changed')),
                sname, None, None)
    return None, sname, summary, root


def delete_bundle(ctx: ActionContext, entry: Entry, parent_fd: int, name: str, st: StatInfo,
                  name_taint: bool = True) -> Outcome:
    outcome, sname, _summary, _root = _stage_verified_bundle(ctx, entry, parent_fd, name, name_taint)
    if outcome is not None:
        return outcome
    ctx.hook('before_unlink', entry)
    return remove_staged_tree(ctx, entry, sname, entry.reclaim)


# archive streams -------------------------------------------------------------------------------------------

class _Sink:
    """Receives the uncompressed stream, hashes it, gzips it into the .part file and runs the per-chunk checks."""

    def __init__(self, ctx: ActionContext, entry: Entry, fd: int, total: int):
        self._ctx, self._entry, self._fd, self._total = ctx, entry, fd, total
        self._out = os.fdopen(os.dup(fd), 'wb')
        self._gz = gzip.GzipFile(filename='', mode='wb', compresslevel=GZIP_LEVEL, fileobj=self._out, mtime=0)
        self.sha = hashlib.sha256()
        self.length = 0
        self._since = 0
        self._dead = False

    def write(self, data: bytes) -> int:
        if self._dead:   # tarfile flushes from __del__ after a failure; that must not run the checks again
            return len(data)
        self.sha.update(data)
        self._gz.write(data)
        self.length += len(data)
        self._since += len(data)
        if self._since >= CHUNK:
            self.tick()
        return len(data)

    def flush_tick(self) -> None:
        """Run the checks once more for whatever came in since the last chunk boundary."""
        if self._since:
            self.tick()

    def tick(self) -> None:
        self._since = 0
        self._ctx.checkpoint()
        self._ctx.hook('archive_chunk', self._entry)
        free = self._ctx.mac.statfs_free(str(self._ctx.archives_path))
        remaining = max(self._total - self.length, 0)
        if free < FREE_RESERVE + remaining:
            raise DiskFull(f'{free} bytes free, {remaining} still to write')

    def close(self) -> None:
        self._gz.close()
        self._out.flush()
        fullsync(self._fd)
        self._out.close()

    def abandon(self) -> None:
        self._dead = True
        with contextlib.suppress(Exception):
            self._gz.close()
        with contextlib.suppress(Exception):
            self._out.close()


class _HashingFile:
    def __init__(self, raw: Any):
        self._raw = raw
        self.sha = hashlib.sha256()
        self.length = 0

    def read(self, n: int = -1) -> bytes:
        data = self._raw.read(n)
        self.sha.update(data)
        self.length += len(data)
        return data


class StreamReader:
    """Uncompressed view of a .gz archive that hashes both the stored bytes and the decompressed ones, so one pass
    both reads and verifies. `tick` runs after every chunk-sized read."""

    def __init__(self, path: Path, tick: Optional[Callable[[], None]] = None, limit: Optional[int] = None):
        """`limit` is the stream length the manifest promises; a longer stream (a decompression bomb) is refused
        as soon as it exceeds it."""
        self._limit = limit
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        self._raw = os.fdopen(fd, 'rb')
        self._stored = _HashingFile(self._raw)
        self._gz = gzip.GzipFile(fileobj=self._stored, mode='rb')   # type: ignore[arg-type]
        self._sha = hashlib.sha256()
        self.length = 0
        self._tick = tick
        self._since = 0

    def read(self, n: int = -1) -> bytes:
        try:
            data = self._gz.read(n)
        except (OSError, EOFError, ValueError, zlib.error) as e:   # BadGzipFile, damaged or truncated stream
            raise ArchiveCorrupt(f'{type(e).__name__}: {e}') from e
        self._sha.update(data)
        self.length += len(data)
        if self._limit is not None and self.length > self._limit:
            raise ArchiveCorrupt('the stream is longer than the manifest says')
        self._since += len(data)
        if self._tick is not None and self._since >= CHUNK:
            self._since = 0
            self._tick()
        return data

    def finish(self) -> Tuple[str, str, int, int]:
        """Read to the end. Returns (sha of the stored bytes, sha of the stream, stream length, stored length)."""
        while self.read(CHUNK):
            pass
        while self._stored.read(CHUNK):   # anything gzip left unread still counts as stored bytes
            pass
        return self._stored.sha.hexdigest(), self._sha.hexdigest(), self.length, self._stored.length

    def close(self) -> None:
        self._gz.close()
        self._raw.close()


def verify_archive_file(path: Path, expect: Mapping[str, Any], tick: Optional[Callable[[], None]] = None) -> None:
    """Decompress the whole archive and compare with the manifest values. Raises ArchiveCorrupt."""
    reader = StreamReader(path, tick, expect['stream_len'])
    try:
        sha_stored, sha_stream, length, stored = reader.finish()
    finally:
        reader.close()
    if sha_stream != expect['sha_orig'] or length != expect['stream_len']:
        raise ArchiveCorrupt('content differs from the manifest')
    if sha_stored != expect['sha_stored'] or stored != expect['stored_bytes']:
        raise ArchiveCorrupt('stored bytes differ from the manifest')


def _new_archive_id(ctx: ActionContext, entry: Entry) -> str:
    return f'{ctx.plan_id[:8]}-{entry.seq}-{secrets.token_hex(3)}'


@contextlib.contextmanager
def _part_file(ctx: ActionContext, adir_fd: int, name: str) -> Iterator[int]:
    """Create <name> exclusively; remove it again (and say so in the audit log) if the block raises."""
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=adir_fd)
    try:
        yield fd
    except Exception as e:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=adir_fd)
        audit_event(ctx, 'part.removed', name=name, reason=type(e).__name__)
        raise
    finally:
        os.close(fd)


def _finalise_archive(ctx: ActionContext, entry: Entry, adir_fd: int, archive_id: str, fmt: str, *,
                      stored_bytes: int, sha_stored: str, sha_orig: str, stream_len: int, orig_size: int,
                      mode: int, mtime_ns: int) -> Dict[str, Any]:
    """Rename .part to its final name, then write the sidecar and the database row (the row is the commit point)."""
    final = f'{archive_id}.{fmt}'
    rename_excl(adir_fd, f'{archive_id}.part', adir_fd, final)
    manifest: Dict[str, Any] = dict(
        id=archive_id, plan_id=ctx.plan_id, seq=entry.seq, path=entry.path, unit=entry.unit.value, fmt=fmt,
        orig_size=orig_size, orig_alloc=entry.alloc, mode=mode, mtime_ns=mtime_ns, sha_orig=sha_orig,
        sha_stored=sha_stored, stream_len=stream_len, stored_bytes=stored_bytes, rule_id=entry.rule_id,
        created_at=ctx.clock())
    fd = os.open(f'{archive_id}.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=adir_fd)
    try:
        write_all(fd, (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode('utf-8'))
        fullsync(fd)
    finally:
        os.close(fd)
    fullsync(adir_fd)   # the renames and the sidecar must be on disk before the original is removed
    ctx.store.add_archive(dict(
        id=archive_id, plan_id=ctx.plan_id, seq=entry.seq, path=entry.path, fmt=fmt, orig_size=orig_size,
        orig_alloc=entry.alloc, stored_bytes=stored_bytes, sha_orig=sha_orig, sha_stored=sha_stored,
        manifest=manifest, state=ARCHIVE_STATE_STORED, created_at=manifest['created_at']))
    return manifest


def discard_archive(ctx: ActionContext, archive_id: str, fmt: str, reason: str = '') -> None:
    """Remove an archive whose entry did not end up deleting anything."""
    audit_event(ctx, 'archive.discarded', archive_id=archive_id, fmt=fmt, reason=reason)
    for suffix in (fmt, 'json', 'part'):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(ctx.archives_path / f'{archive_id}.{suffix}')
    with contextlib.suppress(StewardError):
        ctx.store.set_archive_state(archive_id, 'discarded')


def _sidecar_manifest(ctx: ActionContext, entry: Entry, archive_id: str) -> Optional[Dict[str, Any]]:
    """The sidecar of an archive that has no database row, if it describes this entry and the archive verifies."""
    try:
        manifest = json.loads((ctx.archives_path / f'{archive_id}.json').read_text(encoding='utf-8'))
        fmt = manifest['fmt']
        unit = {'gz': Unit.FILE.value, 'tar.gz': Unit.BUNDLE.value}[fmt]
        ok = (manifest['id'] == archive_id and manifest['plan_id'] == entry.plan_id and manifest['seq'] == entry.seq
              and manifest['path'] == entry.path and manifest['unit'] == unit)
        if ok:
            verify_archive_file(ctx.archives_path / f'{archive_id}.{fmt}', manifest)
        return manifest if ok else None
    except (OSError, ValueError, KeyError, ArchiveCorrupt):
        return None


def settle_unregistered_archives(ctx: ActionContext, entry: Entry) -> None:
    """Archive files named for this entry without a database row: a crash between the rename and the row. One
    whose sidecar and content verify is registered (it may be the only copy of what was then removed); anything
    else is deleted. Both are audited."""
    adir = ctx.archives_path
    if not adir.is_dir():
        return
    known = {a['id'] for a in ctx.store.archives()}
    found: Dict[str, List[Path]] = {}
    for path in adir.glob(f'{ctx.plan_id[:8]}-{entry.seq}-*'):
        archive_id = path.name.partition('.')[0]
        if archive_id not in known:
            found.setdefault(archive_id, []).append(path)
    for archive_id, paths in sorted(found.items()):
        manifest = _sidecar_manifest(ctx, entry, archive_id)
        if manifest is None:
            audit_event(ctx, 'archive.unregistered_removed', archive_id=archive_id, files=sorted(p.name for p in paths))
            for path in paths:
                path.unlink()
            continue
        ctx.store.add_archive(dict(
            id=archive_id, plan_id=entry.plan_id, seq=entry.seq, path=entry.path, fmt=manifest['fmt'],
            orig_size=manifest['orig_size'], orig_alloc=manifest['orig_alloc'], stored_bytes=manifest['stored_bytes'],
            sha_orig=manifest['sha_orig'], sha_stored=manifest['sha_stored'], manifest=manifest,
            state=ARCHIVE_STATE_STORED, created_at=manifest.get('created_at', ctx.clock())))
        audit_event(ctx, 'archive.reregistered', archive_id=archive_id, path=entry.path)


def _open_archives_dir(ctx: ActionContext) -> int:
    return private_dir(ctx.archives_path)


def _verify_part(ctx: ActionContext, adir: Path, archive_id: str, sink: _Sink, stat_size: int) -> Tuple[str, int]:
    """Re-read the finished .part and compare with what was hashed on the way in. Returns (sha_stored, stored bytes)."""
    reader = StreamReader(adir / f'{archive_id}.part', ctx.checkpoint, sink.length)
    try:
        sha_stored, sha_stream, length, stored = reader.finish()
    finally:
        reader.close()
    if sha_stream != sink.sha.hexdigest() or length != sink.length:
        raise ArchiveCorrupt('the archive does not decompress to what was written')
    if stored != stat_size:
        raise ArchiveCorrupt('the archive size changed while it was verified')
    return sha_stored, stored


def archive(ctx: ActionContext, entry: Entry, parent_fd: int, name: str, st: StatInfo,
            name_taint: bool = True) -> Outcome:
    if entry.unit == Unit.BUNDLE:
        return _archive_bundle(ctx, entry, parent_fd, name, st, name_taint)
    return _archive_file(ctx, entry, parent_fd, name, st)


def _after_archive(ctx: ActionContext, entry: Entry, parent_fd: int, name: str, st: StatInfo, archive_id: str,
                   fmt: str, stored: int) -> Outcome:
    """The archive of a file exists and is verified: now remove the file, the same way a delete does."""
    try:
        outcome = delete_file(ctx, entry, parent_fd, name, st)
    except Exception:
        discard_archive(ctx, archive_id, fmt, 'the file could not be removed')
        raise
    if outcome.state == EntryState.DONE:
        return Outcome(EntryState.DONE, 'archive.stored', freed=entry.reclaim - stored, archive_id=archive_id)
    if outcome.state == EntryState.NEEDS_ATTENTION:   # the item sits in staging; its archive is its backup
        return dataclasses.replace(outcome, archive_id=archive_id)
    discard_archive(ctx, archive_id, fmt, 'the file was not removed')
    return outcome


def _archive_file(ctx: ActionContext, entry: Entry, parent_fd: int, name: str, st: StatInfo) -> Outcome:
    archive_id = _new_archive_id(ctx, entry)
    adir_fd = _open_archives_dir(ctx)
    src = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    try:
        opened = StatInfo.from_stat(os.fstat(src))
        if not file_matches(opened, entry.fp):
            return Outcome(EntryState.SKIPPED, 'skip.changed')
        try:
            with _part_file(ctx, adir_fd, f'{archive_id}.part') as part_fd:
                sink = _Sink(ctx, entry, part_fd, opened.size)
                try:
                    left = opened.size   # never read past the planned size: a file that keeps growing has changed anyway
                    while left > 0:
                        chunk = os.read(src, min(CHUNK, left))
                        if not chunk:
                            break
                        left -= len(chunk)
                        sink.write(chunk)
                        sink.flush_tick()
                    sink.close()
                except BaseException:
                    sink.abandon()
                    raise
                # Reading an appended or rewritten file would still hash fine; the stat is what notices.
                if not (file_matches(StatInfo.from_stat(os.fstat(src)), entry.fp)
                        and file_matches(stat_at(parent_fd, name), entry.fp)):
                    raise _SourceChanged()
                sha_stored, stored = _verify_part(ctx, ctx.archives_path, archive_id, sink,
                                                  os.fstat(part_fd).st_size)
        except Cancelled:
            return Outcome(EntryState.PENDING, 'rolled_back')
        except Paused:
            return Outcome(EntryState.PENDING, 'rolled_back')
        except _SourceChanged:
            return Outcome(EntryState.SKIPPED, 'skip.changed')
        except DiskFull:
            return Outcome(EntryState.FAILED, 'fail.disk_full')
        except ArchiveCorrupt as e:
            return Outcome(EntryState.FAILED, f'fail.archive_verify:{e}')
        try:
            _finalise_archive(ctx, entry, adir_fd, archive_id, 'gz', stored_bytes=stored, sha_stored=sha_stored,
                              sha_orig=sink.sha.hexdigest(), stream_len=sink.length, orig_size=opened.size,
                              mode=opened.mode & 0o7777, mtime_ns=opened.mtime_ns)
        except Exception:
            discard_archive(ctx, archive_id, 'gz', 'registering the archive failed')
            raise
    finally:
        os.close(src)
        os.close(adir_fd)
    return _after_archive(ctx, entry, parent_fd, name, st, archive_id, 'gz', stored)


def _symlink_ok(rel_dir: str, target: str) -> bool:
    """A link may only point somewhere inside the bundle: no absolute target and no way out through '..'."""
    if not target or '\0' in target or target.startswith('/'):
        return False
    return os.path.normpath(os.path.join(rel_dir, target)).split('/')[0] != '..'


def _add_tree(tar: tarfile.TarFile, root: str) -> None:
    """Add everything below `root` with relative names. Symlinks are stored as symlinks, hard links as separate
    copies; only regular files, folders and symlinks are allowed."""
    pending = [('', root)]
    while pending:
        rel, directory = pending.pop()
        with os.scandir(directory) as it:
            entries = sorted(it, key=lambda e: e.name)
        for e in entries:
            arcname = f'{rel}/{e.name}' if rel else e.name
            info = os.lstat(e.path)
            member = tarfile.TarInfo(arcname)
            member.mtime = int(info.st_mtime)
            member.mode = info.st_mode & 0o777
            if stat.S_ISDIR(info.st_mode):
                member.type = tarfile.DIRTYPE
                tar.addfile(member)
                pending.append((arcname, e.path))
            elif stat.S_ISLNK(info.st_mode):
                member.type = tarfile.SYMTYPE
                member.linkname = os.readlink(e.path)
                if not _symlink_ok(rel, member.linkname):   # restore would refuse it, so do not archive it
                    raise UnsupportedMember(arcname)
                tar.addfile(member)
            elif stat.S_ISREG(info.st_mode):
                fd = os.open(e.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as f:
                    opened = os.fstat(f.fileno())
                    if not stat.S_ISREG(opened.st_mode) or opened.st_ino != info.st_ino:
                        raise _SourceChanged()
                    member.size = opened.st_size
                    tar.addfile(member, f)
                    after = os.fstat(f.fileno())   # a member written to while it was copied is a torn copy
                    if (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
                        raise _SourceChanged()
            else:
                raise UnsupportedMember(arcname)


def _archive_bundle(ctx: ActionContext, entry: Entry, parent_fd: int, name: str, st: StatInfo,
                    name_taint: bool) -> Outcome:
    outcome, sname, summary, root = _stage_verified_bundle(ctx, entry, parent_fd, name, name_taint)
    if outcome is not None:
        return outcome
    assert summary is not None and root is not None
    archive_id = _new_archive_id(ctx, entry)
    fmt = 'tar.gz'

    def back(result: Outcome) -> Outcome:
        return _guarded_put_back(ctx, entry, parent_fd, name, sname, result)

    try:
        adir_fd = _open_archives_dir(ctx)
        try:
            try:
                with _part_file(ctx, adir_fd, f'{archive_id}.part') as part_fd:
                    sink = _Sink(ctx, entry, part_fd, summary.size)
                    try:
                        tar = tarfile.open(fileobj=sink, mode='w|', format=tarfile.PAX_FORMAT)  # type: ignore[call-overload]
                        _add_tree(tar, str(ctx.staging_path / sname))
                        tar.close()
                        sink.flush_tick()
                        # What went into the archive must be what the plan fingerprinted: walk the tree again.
                        again = stat_at(ctx.staging_fd, sname)
                        if not (dir_matches(again, entry.fp) and bundle_summary_matches(
                                walk_bundle(str(ctx.staging_path / sname), entry.fp['dev'], os.getuid(),
                                            name_taint=name_taint), again, entry.fp)):
                            raise _SourceChanged()
                        sink.close()
                    except BaseException:
                        sink.abandon()
                        raise
                    sha_stored, stored = _verify_part(ctx, ctx.archives_path, archive_id, sink,
                                                      os.fstat(part_fd).st_size)
            except (Cancelled, Paused):
                return back(Outcome(EntryState.PENDING, 'rolled_back'))
            except DiskFull:
                return back(Outcome(EntryState.FAILED, 'fail.disk_full'))
            except ArchiveCorrupt as e:
                return back(Outcome(EntryState.FAILED, f'fail.archive_verify:{e}'))
            except _SourceChanged:
                return back(Outcome(EntryState.SKIPPED, 'skip.changed'))
            except UnsupportedMember as e:
                return back(Outcome(EntryState.SKIPPED, f'skip.unsupported_member:{e}'))
            try:
                _finalise_archive(ctx, entry, adir_fd, archive_id, fmt, stored_bytes=stored, sha_stored=sha_stored,
                                  sha_orig=sink.sha.hexdigest(), stream_len=sink.length, orig_size=summary.size,
                                  mode=root.mode & 0o7777, mtime_ns=root.mtime_ns)
            except Exception:
                discard_archive(ctx, archive_id, fmt, 'registering the archive failed')
                raise
        finally:
            os.close(adir_fd)
    except Exception:
        # Anything unexpected before the archive exists: the bundle goes back, then the error is reported.
        left = put_back(ctx, entry, parent_fd, name, sname)
        if left is not None:
            return left
        raise
    ctx.hook('before_unlink', entry)
    return remove_staged_tree(ctx, entry, sname, entry.reclaim - stored, archive_id)


# trash -----------------------------------------------------------------------------------------------------

def trash(ctx: ActionContext, entry: Entry, abs_path: str, name_taint: bool = True) -> Outcome:
    """Hand the path to the system trash. It is the one action that goes by path, so the identity is checked once
    more right before the call."""
    st = StatInfo.from_stat(os.lstat(abs_path))
    if entry.unit == Unit.BUNDLE:
        ok = dir_matches(st, entry.fp)
        if ok:
            ok = bundle_summary_matches(walk_bundle(abs_path, entry.fp['dev'], os.getuid(), name_taint=name_taint),
                                        st, entry.fp)
    else:
        ok = file_matches(st, entry.fp)
    if not ok:
        return Outcome(EntryState.SKIPPED, 'skip.changed')
    ctx.mac.trash(abs_path)
    return Outcome(EntryState.DONE, 'trash.moved', freed=0)


# restore ---------------------------------------------------------------------------------------------------

def archive_file_path(ctx: ActionContext, row: Mapping[str, Any]) -> Path:
    fmt = row['fmt']
    if fmt not in ('gz', 'tar.gz'):
        raise StewardError(ErrorCode.INVALID, 'error.archive_corrupt', id=row['id'], reason=f'unknown format {fmt!r}')
    return ctx.archives_path / f'{row["id"]}.{fmt}'


def _ensure_parents(dest: Path, base: Path, uid: int) -> None:
    """Create missing folders between the base and the destination (0700). The base itself must exist."""
    missing: List[Path] = []
    cur = dest.parent
    while not os.path.lexists(cur):
        missing.append(cur)
        cur = cur.parent
    if cur != base and base not in cur.parents:
        raise StewardError(ErrorCode.INVALID, 'error.restore_parent', path=str(base))
    for directory in reversed(missing):
        with open_parent_checked(directory, base, uid) as (pfd, name):
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, 0o700, dir_fd=pfd)


def _restore_corrupt(archive_id: str, reason: str) -> StewardError:
    return StewardError(ErrorCode.INVALID, 'error.archive_corrupt', id=archive_id, reason=reason)


def restore(ctx: ActionContext, archive_id: str) -> Dict[str, Any]:
    row = ctx.store.archive(archive_id)
    if row['state'] != ARCHIVE_STATE_STORED:
        raise StewardError(ErrorCode.INVALID, 'error.archive_state', id=archive_id, state=row['state'])
    manifest = row['manifest']
    logical = manifest['path']
    dest = ctx.bases.to_abs(logical)
    base = ctx.bases.to_abs(logical.partition('/')[0])
    source = archive_file_path(ctx, row)
    if not os.path.lexists(source):
        raise StewardError(ErrorCode.NOT_FOUND, 'error.NOT_FOUND', what='archive file', id=archive_id)
    uid = os.getuid()
    _ensure_parents(dest, base, uid)
    with open_parent_checked(dest, base, uid) as (pfd, name):
        try:
            os.stat(name, dir_fd=pfd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise StewardError(ErrorCode.INVALID, 'error.restore_exists', path=logical)
        try:
            if manifest['unit'] == Unit.BUNDLE.value:
                _restore_bundle(source, manifest, row, pfd, name, str(dest.parent))
            else:
                _restore_file(source, manifest, row, pfd, name)
        except ArchiveCorrupt as e:
            raise _restore_corrupt(archive_id, str(e)) from e
    ctx.store.set_archive_state(archive_id, 'restored')
    audit_event(ctx, 'restore', archive_id=archive_id, path=logical, unit=manifest['unit'], bytes=manifest['orig_size'])
    return {'archive_id': archive_id, 'path': logical, 'unit': manifest['unit'], 'bytes': manifest['orig_size']}


def _tmp_name() -> str:
    return f'.ds-restore-{secrets.token_hex(6)}'


def _restore_file(source: Path, manifest: Mapping[str, Any], row: Mapping[str, Any], pfd: int, name: str) -> None:
    tmp = _tmp_name()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=pfd)
    try:
        reader = StreamReader(source, limit=manifest['stream_len'])
        try:
            while True:
                chunk = reader.read(CHUNK)
                if not chunk:
                    break
                write_all(fd, chunk)
            sha_stored, sha_stream, length, stored = reader.finish()
        finally:
            reader.close()
        if sha_stream != manifest['sha_orig'] or length != manifest['stream_len'] or sha_stored != manifest['sha_stored']:
            raise ArchiveCorrupt('content differs from the manifest')
        if os.fstat(fd).st_size != manifest['stream_len']:
            raise ArchiveCorrupt('the restored file is shorter than the archive says')
        os.fchmod(fd, manifest['mode'] & 0o777)
        os.utime(fd, ns=(manifest['mtime_ns'], manifest['mtime_ns']))
        fullsync(fd)
        try:
            os.link(tmp, name, src_dir_fd=pfd, dst_dir_fd=pfd, follow_symlinks=False)   # EXCL: never replaces
        except FileExistsError:
            raise StewardError(ErrorCode.INVALID, 'error.restore_exists', path=manifest['path']) from None
        fullsync(pfd)
    finally:
        os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=pfd)   # after a successful link this only drops the temporary name


class _Extractor:
    """Writes tar members into one folder, going through directory fds with O_NOFOLLOW at every step, so no
    symlink inside the archive (or created by it) can redirect a write."""

    def __init__(self, root_fd: int, root_abs: str):
        self.root_fd = root_fd
        self.root_abs = root_abs
        self.dirs: List[Tuple[Tuple[str, ...], int, int]] = []   # (components, mode, mtime_ns), applied last
        self.links: List[str] = []
        self._chain: List[Tuple[str, int]] = []                  # open folders of the current branch

    @staticmethod
    def parts(member: tarfile.TarInfo) -> Tuple[str, ...]:
        name = member.name
        if not name or '\0' in name or name.startswith('/'):
            raise _unsafe(name)
        parts = tuple(name.split('/'))
        if any(p in ('', '.', '..') for p in parts):
            raise _unsafe(name)
        return parts

    def _folder(self, parts: Tuple[str, ...]) -> int:
        """fd of the folder holding `parts`, creating missing folders (0700). Never follows a symlink."""
        keep = 0
        while keep < len(self._chain) and keep < len(parts) and self._chain[keep][0] == parts[keep]:
            keep += 1
        while len(self._chain) > keep:
            os.close(self._chain.pop()[1])
        fd = self._chain[-1][1] if self._chain else self.root_fd
        for part in parts[keep:]:
            with contextlib.suppress(FileExistsError):
                os.mkdir(part, 0o700, dir_fd=fd)
            try:
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as e:
                raise _unsafe('/'.join(parts)) from e
            self._chain.append((part, fd))
        return fd

    def close(self) -> None:
        while self._chain:
            os.close(self._chain.pop()[1])

    def add(self, member: tarfile.TarInfo, tar: tarfile.TarFile) -> None:
        parts = self.parts(member)
        mtime_ns = int(member.mtime * 1_000_000_000)
        if member.isdir():
            self._folder(parts)
            self.dirs.append((parts, member.mode & 0o777, mtime_ns))
            return
        parent = self._folder(parts[:-1])
        leaf = parts[-1]
        if member.type in (tarfile.REGTYPE, tarfile.AREGTYPE):
            try:
                fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            except FileExistsError:
                raise _unsafe(member.name) from None   # the archive names the same path twice
            try:
                source = tar.extractfile(member)
                assert source is not None
                while True:
                    chunk = source.read(CHUNK)
                    if not chunk:
                        break
                    write_all(fd, chunk)
                if os.fstat(fd).st_size != member.size:
                    raise ArchiveCorrupt(f'{member.name} is shorter than the archive says')
                os.fchmod(fd, member.mode & 0o777)
                os.utime(fd, ns=(mtime_ns, mtime_ns))
            finally:
                os.close(fd)
        elif member.issym():
            target = member.linkname
            if not _symlink_ok(os.path.dirname('/'.join(parts)), target):
                raise _unsafe(member.name)
            try:
                os.symlink(target, leaf, dir_fd=parent)
            except FileExistsError:
                raise _unsafe(member.name) from None
            os.utime(leaf, ns=(mtime_ns, mtime_ns), dir_fd=parent, follow_symlinks=False)
            self.links.append('/'.join(parts))
        else:
            raise _unsafe(member.name)   # hard links, devices, fifos, sparse files

    def finish(self) -> None:
        """Check where every symlink really leads (a chain of links can escape although each looks fine), then
        give folders their recorded modes, deepest first so a read-only folder is sealed last."""
        self.close()
        real_root = os.path.realpath(self.root_abs)
        for rel in self.links:
            resolved = os.path.realpath(os.path.join(self.root_abs, rel))
            if resolved != real_root and not resolved.startswith(real_root + '/'):
                raise _unsafe(rel)
        for parts, mode, mtime_ns in sorted(self.dirs, key=lambda d: -len(d[0])):
            fd = self.root_fd
            opened: List[int] = []
            try:
                for part in parts:
                    fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    opened.append(fd)
                os.fchmod(fd, mode)
                os.utime(fd, ns=(mtime_ns, mtime_ns))
            finally:
                for o in opened:
                    os.close(o)


def _unsafe(name: str) -> StewardError:
    return StewardError(ErrorCode.INVALID, 'error.restore_unsafe', member=name)


def _restore_bundle(source: Path, manifest: Mapping[str, Any], row: Mapping[str, Any], pfd: int, name: str,
                    parent_abs: str) -> None:
    tmp = _tmp_name()
    os.mkdir(tmp, 0o700, dir_fd=pfd)
    root_fd = os.open(tmp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=pfd)
    renamed = False
    try:
        extractor = _Extractor(root_fd, os.path.join(parent_abs, tmp))
        reader = StreamReader(source, limit=manifest['stream_len'])
        try:
            try:
                tar = tarfile.open(fileobj=reader, mode='r|')   # type: ignore[call-overload]
                for member in tar:
                    extractor.add(member, tar)
            except tarfile.TarError as e:
                raise ArchiveCorrupt(f'{type(e).__name__}: {e}') from e
            finally:
                extractor.close()
            sha_stored, sha_stream, length, stored = reader.finish()
        finally:
            reader.close()
        if sha_stream != manifest['sha_orig'] or length != manifest['stream_len'] or sha_stored != manifest['sha_stored']:
            raise ArchiveCorrupt('content differs from the manifest')
        extractor.finish()
        os.fchmod(root_fd, manifest['mode'] & 0o777)
        os.utime(root_fd, ns=(manifest['mtime_ns'], manifest['mtime_ns']))
        os.sync()   # one pass over every file written, instead of a full flush per file
        fullsync(root_fd)
        try:
            rename_excl(pfd, tmp, pfd, name)   # never replaces anything
        except FileExistsError:
            raise StewardError(ErrorCode.INVALID, 'error.restore_exists', path=manifest['path']) from None
        renamed = True
        fullsync(pfd)
    finally:
        os.close(root_fd)
        if not renamed:
            with contextlib.suppress(FileNotFoundError):
                rmtree_ours(os.path.join(parent_abs, tmp))


# purge -----------------------------------------------------------------------------------------------------

def purge_archive(ctx: ActionContext, archive_id: str) -> Dict[str, Any]:
    row = ctx.store.archive(archive_id)
    if row['state'] not in (ARCHIVE_STATE_STORED, 'restored'):
        raise StewardError(ErrorCode.INVALID, 'error.archive_state', id=archive_id, state=row['state'])
    for target in (archive_file_path(ctx, row), ctx.archives_path / f'{archive_id}.json'):
        with contextlib.suppress(FileNotFoundError):   # the goal is "gone"; a file that is already gone is fine
            os.unlink(target)
    ctx.store.set_archive_state(archive_id, 'purged')
    audit_event(ctx, 'purge', archive_id=archive_id, path=row['path'], bytes=row['stored_bytes'])
    return {'archive_id': archive_id, 'path': row['path'], 'bytes': row['stored_bytes']}
