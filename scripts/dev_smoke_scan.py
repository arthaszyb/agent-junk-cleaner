#!/usr/bin/env python3
"""Dev-only smoke test (not packaged): scan the REAL home's roots, read-only.

Everything it writes goes to a temporary data directory that is removed on exit. Run:
    python3 scripts/dev_smoke_scan.py [--domains agent,app-library,user-files,dev-shared]
"""
from __future__ import annotations

import argparse
import collections
import resource
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from disk_steward.catalog import load_catalog  # noqa: E402
from disk_steward.classify import Classifier  # noqa: E402
from disk_steward.detectors import DetectorContext, compute_facts  # noqa: E402
from disk_steward.macos import RealMacOS  # noqa: E402
from disk_steward.model import Domain  # noqa: E402
from disk_steward.paths import Bases  # noqa: E402
from disk_steward.scanner import scan  # noqa: E402
from disk_steward.settings import Settings  # noqa: E402
from disk_steward.store import Store  # noqa: E402

GIB = 1024 ** 3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--domains', default='agent', help='comma-separated: ' + ', '.join(d.value for d in Domain))
    domains = tuple(d for d in parser.parse_args().domains.split(',') if d)
    bases = Bases.detect()
    mac = RealMacOS()
    settings = Settings(domains=domains)
    catalog = load_catalog(settings)
    # Not under $TMPDIR: app-library scans that folder, and the database would be scanning itself.
    with tempfile.TemporaryDirectory(prefix='ds-smoke-', dir='/tmp') as tmp:
        store = Store(Path(tmp) / 'steward.db')
        t0 = time.monotonic()
        facts, facts_hash = compute_facts(DetectorContext(bases, mac, settings), catalog)
        t_facts = time.monotonic() - t0
        classifier = Classifier(catalog, facts, bases, settings)
        t1 = time.monotonic()
        scan_id = scan(store, classifier, catalog, bases, mac, settings, domains)
        elapsed = time.monotonic() - t1
        info = store.scan(scan_id)
        roots = store.scan_roots(scan_id)
        files = sum(r['files'] for r in roots)
        print(f'domains: {",".join(domains)}')
        print(f'facts: {t_facts:.2f}s  scan: {elapsed:.2f}s  files: {files}  '
              f'attributed: {info["attributed"] / GIB:.2f} GiB  status: {info["status"]}  '
              f'peak RSS: {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 2 ** 20} MiB')
        print(f'detector facts: ' + ', '.join(f'{k}={len(str(v))}B' for k, v in sorted(facts.items())))
        print('\nroots:')
        for r in sorted(roots, key=lambda r: -r['alloc']):
            print(f'  {r["alloc"] / GIB:7.2f} GiB {r["files"]:8d} files  {r["status"]:16s} {r["path"]}')
        print('\ntop 30 groups by alloc:')
        print(f'  {"GiB":>7} {"items":>6}  {"tier":9s} {"rule":28s} group')
        for g in sorted(store.groups(scan_id), key=lambda g: -g.alloc)[:30]:
            print(f'  {g.alloc / GIB:7.2f} {g.items:6d}  {g.tier.value:9s} {g.rule_id:28s} {g.key}')
        print('\ntier totals per domain (GiB / items):')
        totals = collections.defaultdict(lambda: [0, 0])
        for g in store.groups(scan_id):
            totals[(g.domain.value, g.tier.value)][0] += g.alloc
            totals[(g.domain.value, g.tier.value)][1] += g.items
        for (domain, tier), (alloc, items) in sorted(totals.items()):
            print(f'  {domain:12s} {tier:10s} {alloc / GIB:8.2f} {items:8d}')
        print('\nscan_errors by code:')
        errors = store.scan_errors(scan_id)
        counts = collections.Counter(e['error'] for e in errors)
        for code, n in sorted(counts.items()):
            print(f'  {code}: {n}')
        if not counts:
            print('  none')
        for e in errors:
            if e['error'] != 'skipped_privacy':   # privacy skips are by design; anything else needs a look
                print(f'  ! {e["error"]}: {e["path"]}')
        store.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
