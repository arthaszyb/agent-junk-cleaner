"""Reports: what a scan found, how it changed since the previous scan, and why more space is not offered."""
from __future__ import annotations

import collections
import time
import unicodedata
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import i18n, queries
from .catalog import Catalog, user_files
from .classify import is_protected_variant
from .macos import MacOS
from .model import Domain, Group, GroupSel, Kind, Spec, Tier
from .paths import Bases
from .selection import resolve_for
from .settings import Settings, settings_hash
from .store import Store

TOP_GROUPS = 30
TOP_APPS = 10
PRIVACY_CODE = 'skipped_privacy'
REASONS = ('credentials', 'databases', 'config', 'user_content', 'current_version', 'tainted', 'unknown', 'other')


# text helpers ----------------------------------------------------------------------------------------------

def fmt_bytes(n: int, signed: bool = False) -> str:
    """Binary units with two decimals: '0 B', '1.50 KiB', '12.00 GiB'. `signed` adds '+' to positive values."""
    sign = '-' if n < 0 else ('+' if signed and n > 0 else '')
    value = float(abs(n))
    if value < 1024:
        return f'{sign}{abs(n)} B'
    for unit in ('KiB', 'MiB', 'GiB'):
        value /= 1024
        if value < 1024 or unit == 'GiB':
            return f'{sign}{value:.2f} {unit}'
    raise AssertionError('unreachable')


def display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in text)


def table(rows: Sequence[Sequence[str]], align: str = '') -> str:
    """Plain-text table. rows[0] is the header; `align` has one 'l' or 'r' per column (default left).
    Widths count East Asian wide characters as two columns so Chinese text stays aligned."""
    if not rows:
        return ''
    cols = len(rows[0])
    align = align.ljust(cols, 'l')
    widths = [max(display_width(str(r[c])) for r in rows) for c in range(cols)]

    def line(row: Sequence[str]) -> str:
        cells = []
        for c, cell in enumerate(row):
            cell = str(cell)
            pad = ' ' * (widths[c] - display_width(cell))
            cells.append(pad + cell if align[c] == 'r' else cell + pad)
        return '  '.join(cells).rstrip()

    out = [line(rows[0]), '  '.join('-' * w for w in widths)]
    out.extend(line(r) for r in rows[1:])
    return '\n'.join(out)


def fmt_time(ts: Optional[float]) -> str:
    return '-' if ts is None else time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))


def _get(group: Any, name: str) -> Any:
    return group[name] if isinstance(group, Mapping) else getattr(group, name)


def _group_text(group: Any, locale: str, part: str) -> str:
    key = _get(group, 'key')
    args = dict(_get(group, 'label_args'))
    own = f'group.{key}.{part}'
    if own in i18n.catalog(locale):
        return i18n.t(own, locale, **args)
    text = i18n.t(f'rule.{_get(group, "rule_id")}.{part}', locale, **args)
    if part == 'label' and is_protected_variant(key):
        text = i18n.t('group.protected_suffix', locale, label=text)
    return text


def group_label(group: Any, locale: str) -> str:
    """`group.<key>.label` when the catalog has one, else `rule.<rule_id>.label` with the group's label_args.
    `group` is a Group or a mapping with key, rule_id and label_args."""
    return _group_text(group, locale, 'label')


def group_effect(group: Any, locale: str) -> str:
    return _group_text(group, locale, 'effect')


# build -----------------------------------------------------------------------------------------------------

def _previous_done(store: Store, scan_id: int) -> Optional[Dict[str, Any]]:
    for older in range(scan_id - 1, 0, -1):
        scan = store.scan(older)
        if scan['status'] == 'done':
            return scan
    return None


def _protected_reason(group: Group) -> str:
    if group.rule_id == 'unknown':
        return 'unknown'
    if is_protected_variant(group.key):
        return 'current_version'   # the version in use or the newest one, kept next to its removable siblings
    return {Kind.CREDENTIAL: 'credentials', Kind.DATABASE: 'databases', Kind.CONFIG: 'config',
            Kind.WORK_PRODUCT: 'user_content'}.get(group.kind, 'other')


def _bucket(alloc: int = 0, items: int = 0) -> Dict[str, int]:
    return {'alloc': alloc, 'items': items}


def _group_row(g: Group, growth: Optional[Mapping[str, Mapping[str, Any]]]) -> Dict[str, Any]:
    row = {'key': g.key, 'rule_id': g.rule_id, 'provider': g.provider, 'domain': g.domain.value,
           'tier': g.tier.value, 'kind': g.kind.value, 'unit': g.unit.value, 'label_args': dict(g.label_args),
           'items': g.items, 'alloc': g.alloc, 'reclaim': g.reclaim, 'newest_ns': g.newest_ns,
           'oldest_ns': g.oldest_ns, 'actions': [a.value for a in g.actions], 'hint': g.hint}
    if growth is not None:
        row.update(growth[g.key])
    return row


def cloud_synced(bases: Bases, settings: Settings) -> Dict[str, str]:
    """Logical user roots whose files live in iCloud or another cloud folder, with the reason; empty while the
    user-files area is off, because nothing there is scanned or offered."""
    if Domain.USER_FILES.value not in settings.domains:
        return {}
    return user_files.cloud_synced_roots(bases, settings.user_roots)


def cloud_roots_of(paths: Iterable[str], synced: Mapping[str, str]) -> Dict[str, str]:
    """The synced roots (from cloud_synced) that hold at least one of the logical `paths`. A path counts for its
    innermost root only, so nested roots do not warn twice about one file."""
    hit: Dict[str, str] = {}
    for path in paths:
        inside = [r for r in synced if path == r or path.startswith(r + '/')]
        if inside:
            root = max(inside, key=len)
            hit[root] = synced[root]
    return hit


def cloud_warning_lines(synced: Mapping[str, str], locale: str) -> List[str]:
    return [i18n.t(f'cli.cloud.{reason}', locale, root=root) for root, reason in sorted(synced.items())]


def build_report(store: Store, scan_id: int, catalog: Catalog, settings: Settings, now: float,
                 spec: Optional[Spec] = None, mac: Optional[MacOS] = None,
                 snapshots: Optional[Sequence[str]] = None, bases: Optional[Bases] = None) -> Dict[str, Any]:
    """Everything `report` and the overview page show, as JSON-safe data (no translated text).

    `spec` supplies the age filter for the "too recent" figure; without it settings.default_min_age_days is used.
    `snapshots` is what mac.snapshots() returned (the caller owns that query). Without `mac`, Move to Trash counts
    as unavailable, which only matters for a rule whose default action is trash. `bases` is needed to look for
    cloud-synced folders; without it `cloud_synced` stays empty."""
    scan = store.scan(scan_id)
    groups = store.groups(scan_id)
    errors = dict(sorted(collections.Counter(e['error'] for e in store.scan_errors(scan_id)).items()))
    total_alloc = sum(g.alloc for g in groups)
    previous = _previous_done(store, scan_id)
    growth = _growth(store, scan, previous, groups)
    by_group = growth['by_group']

    def totals() -> Dict[str, int]:
        return {'alloc': 0, 'reclaim': 0, 'items': 0, 'groups': 0}
    by_tier = {t.value: totals() for t in Tier}
    by_domain: Dict[str, Dict[str, int]] = {}
    for g in groups:
        for acc in (by_tier[g.tier.value], by_domain.setdefault(g.domain.value, totals())):
            acc['alloc'] += g.alloc
            acc['reclaim'] += g.reclaim
            acc['items'] += g.items
            acc['groups'] += 1

    used = None if scan['vol_total'] is None or scan['vol_free'] is None else scan['vol_total'] - scan['vol_free']
    attributed = scan['attributed']
    report: Dict[str, Any] = {
        'generated_at': now,
        'scan': {'id': scan['id'], 'started_at': scan['started_at'], 'finished_at': scan['finished_at'],
                 'domains': scan['domains'], 'items': sum(g.items for g in groups), 'groups': len(groups),
                 'rules_changed': (scan['catalog_hash'] != catalog.hash
                                   or scan['settings_hash'] != settings_hash(settings))},
        'volume': {'total': scan['vol_total'], 'used': used, 'free': scan['vol_free'], 'attributed': attributed,
                   'unattributed': None if used is None or attributed is None else used - attributed,
                   'snapshots': None if snapshots is None else list(snapshots)},
        'totals': {'alloc': total_alloc, 'reclaim': sum(g.reclaim for g in groups), 'by_tier': by_tier,
                   'by_domain': by_domain},
        'top_groups': [_group_row(g, by_group) for g in groups[:TOP_GROUPS]],
        'growth': {k: v for k, v in growth.items() if k != 'by_group'},
        'cloud_synced': {} if bases is None else cloud_synced(bases, settings),
    }
    report.update(_not_included(store, scan, groups, errors, settings, now, spec, mac))
    return report


def _growth(store: Store, scan: Mapping[str, Any], previous: Optional[Mapping[str, Any]],
            groups: Sequence[Group]) -> Dict[str, Any]:
    if previous is None:
        return {'previous_scan_id': None, 'previous_finished_at': None, 'alloc_delta': None, 'groups': [],
                'by_group': None}
    by_group: Dict[str, Dict[str, Any]] = {}
    for g in groups:
        before = next((alloc for sid, _fin, alloc, _n in store.group_history(g.key) if sid == previous['id']), None)
        by_group[g.key] = {'previous_alloc': before or 0, 'delta': g.alloc - (before or 0), 'new': before is None}
    moved = sorted((g for g in groups if by_group[g.key]['delta'] != 0),
                   key=lambda g: (-abs(by_group[g.key]['delta']), g.key))
    both = scan['attributed'] is not None and previous['attributed'] is not None
    return {'previous_scan_id': previous['id'], 'previous_finished_at': previous['finished_at'],
            'alloc_delta': scan['attributed'] - previous['attributed'] if both else None,
            'groups': [dict(_group_row(g, None), **by_group[g.key]) for g in moved[:TOP_GROUPS]],
            'by_group': by_group}


def _not_included(store: Store, scan: Mapping[str, Any], groups: Sequence[Group], errors: Mapping[str, int],
                  settings: Settings, now: float, spec: Optional[Spec], mac: Optional[MacOS]) -> Dict[str, Any]:
    filter_days = settings.default_min_age_days if spec is None else spec.filter_min_age_days
    probe = Spec(scan_id=scan['id'], filter_min_age_days=filter_days, groups={
        g.key: (spec.groups[g.key] if spec is not None and g.key in spec.groups else GroupSel())
        for g in groups if g.tier == Tier.JUNK and g.actions and g.domain.value in settings.domains})
    res = resolve_for(store, probe, now, settings.domains, False if mac is None else mac.trash_available())
    per = res.summary['groups']
    taint = queries.taint_split(store, scan['id'])
    offered = {'alloc': 0, 'reclaim': 0, 'items': 0, 'min_age_days': filter_days}
    too_recent = dict(_bucket(), min_age_days=filter_days)
    floor_held = _bucket()
    other, review = _bucket(), dict(_bucket(), groups=0)
    report_only: Dict[str, Any] = dict(_bucket(), groups=[])
    protected: Dict[str, Any] = dict(_bucket(), by_reason={})
    for g in groups:
        if g.tier == Tier.PROTECTED:
            for tainted in (False, True):
                items, alloc = taint.get((g.key, tainted), (0, 0))
                if items:
                    reason = 'tainted' if tainted else _protected_reason(g)
                    slot = protected['by_reason'].setdefault(reason, _bucket())
                    for acc in (slot, protected):
                        acc['alloc'] += alloc
                        acc['items'] += items
        elif not g.actions:
            report_only['alloc'] += g.alloc
            report_only['items'] += g.items
            report_only['groups'].append({'key': g.key, 'rule_id': g.rule_id, 'label_args': dict(g.label_args),
                                          'alloc': g.alloc, 'items': g.items, 'hint': g.hint})
        elif g.tier == Tier.REVIEW:
            review['alloc'] += g.alloc
            review['items'] += g.items
            review['groups'] += 1
        else:
            s = per[g.key]
            offered['alloc'] += s['alloc']
            offered['reclaim'] += s['reclaim']
            offered['items'] += s['selected']
            too_recent['alloc'] += s['too_recent_alloc']
            too_recent['items'] += s['too_recent']
            floor_held['alloc'] += s['floor_held_alloc']
            floor_held['items'] += s['floor_held']
            other['alloc'] += g.alloc - s['alloc'] - s['too_recent_alloc'] - s['floor_held_alloc']
            other['items'] += g.items - s['selected'] - s['too_recent'] - s['floor_held']
    return {'offered': offered,
            'not_included': {'protected': protected, 'review': review, 'report_only': report_only,
                             'too_recent': too_recent, 'floor_held': floor_held, 'other': other,
                             'privacy_skipped': errors.get(PRIVACY_CODE, 0), 'errors': dict(errors)}}


# render ----------------------------------------------------------------------------------------------------

def _error_label(code: str, locale: str) -> str:
    key = f'cli.scan_error.{code}'
    return i18n.t(key, locale) if key in i18n.catalog(locale) else code


def render_text(report: Mapping[str, Any], locale: str) -> str:
    def T(key: str, **args: Any) -> str:
        return i18n.t(key, locale, **args)

    scan, vol, totals = report['scan'], report['volume'], report['totals']
    out: List[str] = [T('cli.report.scan', id=scan['id'], finished=fmt_time(scan['finished_at']),
                        domains=', '.join(T(f'domain.{d}') for d in scan['domains']) or '-')]
    if scan['rules_changed']:
        out.append(T('cli.report.rules_changed'))
    out += ['', T('cli.report.volume')]
    if vol['total'] is not None and vol['used'] is not None:
        out.append('  ' + T('cli.report.volume_line', total=fmt_bytes(vol['total']), used=fmt_bytes(vol['used']),
                             free=fmt_bytes(vol['free'])))
        out.append('  ' + T('cli.report.volume_attributed', attributed=fmt_bytes(vol['attributed']),
                             unattributed=fmt_bytes(vol['unattributed'])))
    if vol['snapshots'] is not None:
        out.append('  ' + T('cli.report.snapshots', count=len(vol['snapshots'])))

    out += ['', T('cli.report.by_tier'), table(
        [(T('cli.col.tier'), T('cli.col.groups'), T('cli.col.items'), T('cli.col.size'), T('cli.col.reclaim'))] +
        [(T(f'tier.{t}'), str(v['groups']), str(v['items']), fmt_bytes(v['alloc']), fmt_bytes(v['reclaim']))
         for t, v in totals['by_tier'].items()], 'lrrrr')]
    out += ['', T('cli.report.by_domain'), table(
        [(T('cli.col.domain'), T('cli.col.groups'), T('cli.col.items'), T('cli.col.size'), T('cli.col.reclaim'))] +
        [(T(f'domain.{d}'), str(v['groups']), str(v['items']), fmt_bytes(v['alloc']), fmt_bytes(v['reclaim']))
         for d, v in sorted(totals['by_domain'].items())], 'lrrrr')]

    has_growth = report['growth']['previous_scan_id'] is not None
    header = [T('cli.col.tier'), T('cli.col.size'), T('cli.col.items')] + ([T('cli.col.change')] if has_growth else []) \
        + [T('cli.col.group')]
    rows = [header]
    for g in report['top_groups']:
        row = [T(f'tier.{g["tier"]}'), fmt_bytes(g['alloc']), str(g['items'])]
        if has_growth:
            row.append(T('cli.growth.new') if g['new'] else fmt_bytes(g['delta'], signed=True))
        rows.append(row + [group_label(g, locale)])
    out += ['', T('cli.report.top_groups', count=len(report['top_groups'])),
            table(rows, 'lrr' + ('r' if has_growth else '') + 'l')]

    out += [''] + _not_included_text(report, locale)
    warnings = cloud_warning_lines(report['cloud_synced'], locale)
    if warnings:
        out += [''] + [T('cli.report.warning', text=w) for w in warnings]
    return '\n'.join(out) + '\n'


def _not_included_text(report: Mapping[str, Any], locale: str) -> List[str]:
    def T(key: str, **args: Any) -> str:
        return i18n.t(key, locale, **args)

    ni, offered = report['not_included'], report['offered']
    days = offered['min_age_days']

    def row(label: str, bucket: Mapping[str, Any]) -> Tuple[str, str, str]:
        return label, str(bucket['items']), fmt_bytes(bucket['alloc'])

    # With no age filter nothing can be "too recent"; the row would only say "newer than 0 days".
    rows = [(T('cli.col.what'), T('cli.col.items'), T('cli.col.size')),
            row(T('cli.why.offered', days=days) if days else T('cli.why.offered_any'), offered)]
    if days:
        rows.append(row(T('cli.why.too_recent', days=days), ni['too_recent']))
    rows += [row(T('cli.why.floor_held'), ni['floor_held']), row(T('cli.why.review'), ni['review']),
             row(T('cli.why.report_only'), ni['report_only']), row(T('cli.why.other'), ni['other']),
             row(T('cli.why.protected'), ni['protected'])]
    for reason in REASONS:
        slot = ni['protected']['by_reason'].get(reason)
        if slot:
            rows.append(('  ' + T(f'cli.reason.{reason}'), str(slot['items']), fmt_bytes(slot['alloc'])))
    out = [T('cli.why.title'), table(rows, 'lrr')]
    for g in ni['report_only']['groups']:
        if g['hint']:
            out.append('  ' + T('cli.why.hint', label=group_label(g, locale), size=fmt_bytes(g['alloc']),
                                hint=g['hint']))
        else:
            out.append('  ' + T('cli.why.hint_none', label=group_label(g, locale), size=fmt_bytes(g['alloc'])))
    if ni['privacy_skipped']:
        out.append(T('cli.why.privacy', count=ni['privacy_skipped']))
    problems = {c: n for c, n in ni['errors'].items() if c != PRIVACY_CODE}
    if problems:
        out.append(T('cli.why.errors', details=', '.join(f'{_error_label(c, locale)} {n}'
                                                        for c, n in sorted(problems.items()))))
    return out


def render_growth(report: Mapping[str, Any], locale: str) -> str:
    def T(key: str, **args: Any) -> str:
        return i18n.t(key, locale, **args)

    growth = report['growth']
    if growth['previous_scan_id'] is None:
        return T('cli.growth.none') + '\n'
    out = [T('cli.growth.title', previous=growth['previous_scan_id'], when=fmt_time(growth['previous_finished_at']),
             current=report['scan']['id'])]
    if growth['alloc_delta'] is not None:
        out.append(T('cli.growth.total', delta=fmt_bytes(growth['alloc_delta'], signed=True)))
    if not growth['groups']:
        out.append(T('cli.growth.unchanged'))
        return '\n'.join(out) + '\n'
    rows = [(T('cli.col.change'), T('cli.col.size'), T('cli.col.group'))]
    for g in growth['groups']:
        rows.append((fmt_bytes(g['delta'], signed=True), fmt_bytes(g['alloc']),
                     group_label(g, locale) + (' ' + T('cli.growth.new_tag') if g['new'] else '')))
    return '\n'.join(out + ['', table(rows, 'rrl')]) + '\n'


def render_apps(apps: Mapping[str, Any], locale: str) -> str:
    """The `app_sizes()` result as the "largest applications" section."""
    def T(key: str, **args: Any) -> str:
        return i18n.t(key, locale, **args)

    sized = [e for e in apps['entries'] if 'bytes' in e]
    failed = [e for e in apps['entries'] if 'error' in e]
    out = [T('cli.apps.title')]
    if sized:
        shown = sized[:TOP_APPS]
        out.append(T('cli.apps.measured', when=fmt_time(apps['measured_at']), shown=len(shown), count=len(sized),
                     total=fmt_bytes(apps['total'])))
        out.append(table([(T('cli.col.size'), T('cli.col.path'))] +
                         [(fmt_bytes(e['bytes']), e['path']) for e in shown], 'rl'))
    elif not failed:
        out.append(T('cli.apps.none'))
    if failed:
        out.append(T('cli.apps.errors', count=len(failed),
                     details='; '.join(f'{e["path"]}: {e["error"]}' for e in failed[:TOP_APPS])))
    out.append(T('cli.apps.note'))
    return '\n'.join(out) + '\n'
