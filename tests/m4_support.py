"""Helpers for the M4 tests: a store, a plan row and an ActionContext over a FakeHome, plus plan-building shortcuts."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from disk_steward import actions
from disk_steward.audit import Audit
from disk_steward.fsutil import walk_bundle
from disk_steward.macos import FakeMacOS
from disk_steward.model import (Action, Entry, Plan, PlanState, StatInfo, Unit)
from disk_steward.store import Store

from m3_support import make_mac

PLAN_ID = 'abcdef0123456789abcdef01'


def file_fp(path: Path) -> Dict[str, int]:
    info = StatInfo.from_stat(os.lstat(path))
    return dict(dev=info.dev, ino=info.ino, size=info.size, mtime_ns=info.mtime_ns, birth_ns=info.birth_ns,
                nlink=info.nlink)


def bundle_fp(path: Path, name_taint: bool = False) -> Dict[str, int]:
    st = os.lstat(path)
    info = StatInfo.from_stat(st)
    summary = walk_bundle(str(path), st.st_dev, os.getuid(), name_taint=name_taint)
    return dict(dev=info.dev, ino=info.ino, files=summary.files, alloc=summary.alloc,
                newest_ns=max(summary.newest_ns, info.changed_ns))


class ActionEnv:
    """Store, plan, staging folder and context for calling actions directly, without the executor."""

    def __init__(self, case, mac: Optional[FakeMacOS] = None):
        self.case = case
        self.fh = case.fh
        self.mac = mac or make_mac()
        self.store = Store(self.fh.data_dir / 'steward.db', self.fh.clock)
        case.addCleanup(self.store.close)
        scan_id = self.store.begin_scan('c', 'f', 's', ['agent'])
        self.store.finish_scan(scan_id, 'done', 1, 1, 0)
        self.plan_id = PLAN_ID
        self.entries = []
        self.scan_id = scan_id
        self.staging_fd = actions.private_dir(self.fh.data_dir / 'staging' / PLAN_ID)
        case.addCleanup(os.close, self.staging_fd)
        self.hooks: Dict[str, Any] = {}
        self.checkpoint = lambda: None
        self.ctx = actions.ActionContext(
            store=self.store, bases=self.fh.bases, mac=self.mac, audit=Audit(self.fh.data_dir, self.fh.clock),
            clock=self.fh.clock, data_dir=self.fh.data_dir, plan_id=PLAN_ID, staging_fd=self.staging_fd,
            checkpoint=lambda: self.checkpoint(), hook=lambda n, e: self.hooks[n](e) if n in self.hooks else None)

    def entry(self, logical: str, action: Action = Action.DELETE, unit: Unit = Unit.FILE, seq: Optional[int] = None,
              rule_id: str = 'test.rule') -> Entry:
        path = self.fh.path(logical)
        fp = file_fp(path) if unit == Unit.FILE else bundle_fp(path)
        alloc = os.lstat(path).st_blocks * 512 if unit == Unit.FILE else fp['alloc']
        entry = Entry(plan_id=PLAN_ID, seq=seq or len(self.entries) + 1, item_key=f'k{len(self.entries)}',
                      path=logical, unit=unit, action=action, rule_id=rule_id, group_key='g', threshold_days=14.0,
                      alloc=alloc, reclaim=alloc, fp=fp)
        self.entries.append(entry)
        self._save_plan()
        return entry

    def _save_plan(self) -> None:
        # The plan row is needed for archives (foreign key); entries are only saved so the store knows them.
        try:
            self.store.plan(PLAN_ID)
        except Exception:
            plan = Plan(id=PLAN_ID, created_at=0.0, expires_at=1e12, scan_id=self.scan_id, catalog_hash='c',
                        facts_hash='f', settings_hash='s', spec={}, revision=1, entries_digest='d',
                        state=PlanState.DRAFT, totals={})
            self.store.save_plan(plan, [])

    def call(self, fn, entry: Entry, **kw):
        """Run an action on an entry the way the executor does: through the checked parent fd."""
        path = self.fh.path(entry.path)
        base = self.fh.bases.to_abs(entry.path.partition('/')[0])
        with actions.open_parent_checked(path, base, os.getuid()) as (pfd, name):
            st = actions.stat_at(pfd, name)
            return fn(self.ctx, entry, pfd, name, st, **kw)

    def staged(self):
        return sorted(os.listdir(self.fh.data_dir / 'staging' / PLAN_ID))

    def audit_events(self):
        import json
        path = self.fh.data_dir / 'audit' / 'audit.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def archive_files(self):
        d = self.fh.data_dir / 'archives'
        return sorted(os.listdir(d)) if d.exists() else []


def tree_snapshot(root: Path) -> Dict[str, Any]:
    """Names, kinds, modes, sizes, contents and whole-second mtimes of a tree, for before/after comparison."""
    out: Dict[str, Any] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(dirnames + filenames):
            p = Path(dirpath) / name
            info = os.lstat(p)
            rel = str(p.relative_to(root))
            if os.path.islink(p):
                out[rel] = ('link', os.readlink(p))
            elif os.path.isdir(p):
                out[rel] = ('dir', info.st_mode & 0o777, int(info.st_mtime))
            else:
                out[rel] = ('file', info.st_mode & 0o777, int(info.st_mtime), p.read_bytes())
    return out


def expected_from(app, ops: Any, session: str = 'default') -> Mapping[str, Any]:
    r = app.apply_ops(ops, session)
    return {'spec_version': r['spec']['version'], 'digest': r['digest'], 'scan_id': r['spec']['scan_id']}


def make_plan(app, ops: Any, session: str = 'default') -> Dict[str, Any]:
    """Apply selection ops and create a plan from the result."""
    return app.create_plan(session, expected_from(app, ops, session))


JUNK_ALL = [{'op': 'select_tier', 'tier': 'junk', 'on': True}, {'op': 'set_filter', 'min_age_days': 0}]
JUNK_14D = [{'op': 'select_tier', 'tier': 'junk', 'on': True}, {'op': 'set_filter', 'min_age_days': 14}]
