"""Helpers for the M3 tests: an App over a FakeHome holding the M2 fixture tree, and a scan helper without App."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from disk_steward import scanner
from disk_steward.catalog import load_catalog
from disk_steward.classify import Classifier
from disk_steward.detectors import DetectorContext, compute_facts
from disk_steward.macos import FakeMacOS
from disk_steward.model import Action, AgeBasis, Domain, Group, Item, Kind, Tier, Unit
from disk_steward.service import App
from disk_steward.settings import Settings
from disk_steward.store import Store

NOW = 1_800_000_000.0
DAY = 86400.0
USAGE = (500 * 10 ** 9, 200 * 10 ** 9)
FREE = 300 * 10 ** 9


def make_mac(**kw) -> FakeMacOS:
    kw.setdefault('usage', USAGE)
    kw.setdefault('free', FREE)
    return FakeMacOS(**kw)


def make_app(fh, mac: Optional[FakeMacOS] = None, settings: Optional[Settings] = None, locale: str = 'en',
             **kw) -> App:
    return App(home=fh.home, data_dir=fh.data_dir, mac=mac or make_mac(), clock=fh.clock, locale=locale,
               settings=settings or Settings(domains=('agent',)), bases=fh.bases, **kw)


def scan_store(fh, store: Store, mac: Optional[FakeMacOS] = None, settings: Optional[Settings] = None, **scan_kw):
    """Scan the fixture without an App. Returns (scan_id, catalog, settings, mac). `scan_kw` goes to scanner.scan
    (prune=False keeps the previous scan's rows, as App.scan does while it migrates selections)."""
    settings = settings or Settings(domains=('agent',))
    catalog = load_catalog(settings)
    mac = mac or make_mac()
    facts, _ = compute_facts(DetectorContext(fh.bases, mac, settings), catalog)
    classifier = Classifier(catalog, facts, fh.bases, settings)
    scan_id = scanner.scan(store, classifier, catalog, fh.bases, mac, settings, settings.domains, **scan_kw)
    return scan_id, catalog, settings, mac


def ns_ago(days):
    return int((NOW - days * DAY) * 1e9)


def mk_item(key='k1', age=20.0, group='g', **kw):
    base = dict(scan_id=1, key=key, path=f'~/x/{key}', rule_id='r', group_key=group, tier=Tier.JUNK,
                domain=Domain.AGENT, kind=Kind.CACHE, unit=Unit.FILE, dev=1, ino=1, size=100, alloc=4096,
                reclaim=4096, files=1, mtime_ns=ns_ago(age), birth_ns=ns_ago(age), newest_ns=ns_ago(age),
                last_used_ns=None, nlink=1, uid=501, flags=0)
    base.update(kw)
    return Item(**base)


def mk_group(key='g', **kw):
    base = dict(scan_id=1, key=key, rule_id='r', provider='codex', domain=Domain.AGENT, tier=Tier.JUNK,
                kind=Kind.CACHE, unit=Unit.FILE, label_args={}, items=1, alloc=4096, reclaim=4096, newest_ns=1,
                oldest_ns=1, actions=(Action.DELETE, Action.ARCHIVE), default_action=Action.DELETE,
                min_age_floor_days=0.0, default_min_age_days=None, age_basis=AgeBasis.CHANGED)
    base.update(kw)
    return Group(**base)


def mk_store(tmp, catalog_hash='c', settings_hash='s', tainted=False, domains=('agent', 'app-library')):
    """One scan with a junk, a review, a protected, a report-only and an app-library junk group.
    `tainted` adds two items to the junk group that decide() must still refuse: one carrying a taint and one
    of the protected tier (the scanner puts those in protected groups; a plan must not depend on that)."""
    store = Store(Path(tmp) / 's.db')
    sid = store.begin_scan(catalog_hash, 'f', settings_hash, list(domains))
    groups = [
        mk_group('g.junk', scan_id=sid, actions=(Action.DELETE, Action.TRASH), min_age_floor_days=1.0),
        mk_group('g.review', scan_id=sid, tier=Tier.REVIEW, actions=(Action.ARCHIVE, Action.DELETE),
                 default_action=Action.ARCHIVE),
        mk_group('g.prot', scan_id=sid, tier=Tier.PROTECTED, actions=(), default_action=None),
        mk_group('g.report', scan_id=sid, tier=Tier.REVIEW, actions=(), default_action=None, hint='do it by hand'),
        mk_group('g.app', scan_id=sid, domain=Domain.APP_LIBRARY, actions=(Action.DELETE,)),
    ]
    spec = [('g.junk', 'j16', 16), ('g.junk', 'j9', 9), ('g.junk', 'j0', 0.5), ('g.junk', 'jhl', 30),
            ('g.review', 'r40', 40), ('g.review', 'r20', 20), ('g.prot', 'p1', 40), ('g.report', 'rep', 40),
            ('g.app', 'a30', 30)]
    if tainted:
        spec += [('g.junk', 'jt', 30), ('g.junk', 'jp', 30)]
    items = []
    for g, name, age in spec:
        kw = {}
        if g == 'g.prot':
            kw = dict(tier=Tier.PROTECTED)
        if g == 'g.app':
            kw = dict(domain=Domain.APP_LIBRARY)
        if name == 'jhl':
            kw = dict(nlink=2)
        if name == 'jt':
            kw = dict(taint='sqlite')
        if name == 'jp':
            kw = dict(tier=Tier.PROTECTED)
        size = 1000 * (len(name) + 1)
        items.append(mk_item(name, age, g, scan_id=sid, alloc=size, reclaim=0 if name == 'jhl' else size, **kw))
    store.add_groups(sid, groups)
    store.add_items(sid, items)
    store.finish_scan(sid, 'done', 10 ** 9, 5 * 10 ** 8, sum(i.alloc for i in items))
    return store, sid
