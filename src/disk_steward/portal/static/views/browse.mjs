// Browse: groups with their selection state, their items with the server's decision for each, and the sticky
// summary. The browser sends ops and renders what the server answers; it never works out what qualifies.
// The address names a filter (#/browse?tier=junk&state=too_recent): the page says what it shows and how much, and
// the figures are the ones of the overview row the link came from.
import { api, ApiError } from '../api.mjs';
import { delegate, h, keepFocus, replace } from '../dom.mjs';
import { browseHref, fmtAge, fmtAgo, fmtBytes, fmtCount, pluralCount } from '../format.mjs';
import { t } from '../i18n.mjs';
import { icon } from '../icons.mjs';
import { create as createNative } from './native.mjs';
import { quickPlan, refused } from '../planflow.mjs';
import { colgroup, dotLabel, midPath, nextSort, plainTh, sortRows, sortTh } from '../tables.mjs';
import { on, state } from '../state.mjs';
import { actionLine, badge, codeBlock, emptyState, groupBadge, groupTier, loading, reportError, stateTable, toast, warningList } from '../ui.mjs';

const PAGE = 50;
const MAX_PAGE = 500;
const FILTER_KEYS = ['domain', 'tier', 'report_only', 'state', 'reason', 'days'];
const DOMAINS = ['agent', 'app-library', 'user-files', 'dev-shared'];
const STATES = ['selected', 'too_recent', 'too_recent_floor', 'other'];
const REASONS = ['credentials', 'databases', 'config', 'user_content', 'current_version', 'tainted', 'unknown', 'other'];
const STATE_KIND = { selected: 'ok', too_recent: 'warn', too_recent_floor: 'warn', excluded: 'neutral', protected: 'neutral',
  no_gain: 'neutral', unsupported_action: 'neutral', domain_off: 'neutral', unselected: 'neutral' };
const VERDICT_KIND = { likely_safe: 'ok', review: 'warn', keep: 'neutral' };

const selectable = (g) => g.tier !== 'protected' && g.actions.length > 0;
const TIER_RANK = { junk: 0, review: 1, report_only: 1, protected: 2 };
const GROUP_PAGE = 50;   // groups drawn at a time: a scan can have hundreds

export function create() {
  let root;
  let groups = [];
  let scope = { groups: 0, items: 0, alloc: 0 };
  let counts = null;
  let selection = null;       // the user's own selection (not loaded for a preview)
  let quick = null;           // the "safe to clean" selection, when the page shows what it would clean
  let advice = null;          // {byGroup, result} or {error}
  let loaded = false;
  let want = null;            // group key from the URL to open once
  let summaryOpen = false;    // narrow screens fold the summary's details away
  let flt = {};               // the filter in the address
  let preview = false;        // a state filter: how the quick selection judges the items; nothing to tick
  const open = new Map();     // group key -> {rows, total, q}
  const search = { text: '' };
  const nativePart = createNative();           // the tools' own commands, when the filter is "shown only"
  let nativeMounted = false;
  let nativeSlot = null;
  let shownGroups = GROUP_PAGE;
  let groupSort = { col: null, dir: 'asc' };     // the order the server gave until a header is clicked
  let itemSort = { col: 'size', dir: 'desc' };
  const disposers = [];
  let listEl;
  let summaryEl;

  // data ----------------------------------------------------------------------------------------------------

  async function loadAdvice() {
    advice = null;
    if (!state.settings.advisor_enabled) return;
    try {
      const { last } = await api.get('/api/advisor/last');   // the stored advice; no CLI is started for a page of badges
      const byGroup = {};
      if (last) for (const rec of last.recommendations) byGroup[rec.group] = rec;
      advice = { byGroup, result: last };
    } catch (e) {
      if (!(e instanceof ApiError)) throw e;
      advice = { error: e.message };
    }
  }

  async function loadAll() {
    if (!state.scan) {   // without a scan the endpoints answer 409; do not ask
      groups = [];
      selection = null;
      loaded = true;
      render();
      return;
    }
    const [g, sel] = await Promise.all([api.get('/api/groups', { ...flt, counts: 1 }), preview ? null : api.get('/api/spec')]);
    groups = g.groups;
    scope = g.scope;
    counts = g.counts;
    selection = sel;
    quick = null;
    if (preview && flt.state === 'selected' && flt.tier === 'junk') quick = await api.get('/api/quick', { days: flt.days });
    for (const key of [...open.keys()]) if (!groups.some((x) => x.key === key)) open.delete(key);   // gone after a rescan
    await loadAdvice();
    await Promise.all([...open.keys()].map((key) => loadItems(key, true)));
    loaded = true;
    render();
  }

  // What the server can sort by: size and age (both ways for age), the path when nothing judges the items. The rest of the
  // columns, and size ascending, sort the rows that are loaded.
  const judgedList = () => Boolean(flt.state || flt.reason);
  function serverOrder() {
    const { col, dir } = itemSort;
    if (col === 'size' && dir === 'desc') return 'alloc';
    if (col === 'changed') return dir === 'desc' ? 'newest' : 'oldest';
    if (col === 'path' && dir === 'asc' && !judgedList()) return 'path';
    return 'alloc';
  }

  async function loadItems(key, keep = false, more = false) {
    const cur = open.get(key) || { rows: [], total: 0, q: '' };
    const offset = more ? cur.rows.length : 0;
    const limit = more ? PAGE : Math.min(MAX_PAGE, Math.max(PAGE, keep ? cur.rows.length : PAGE));
    const judged = flt.state || flt.reason ? { state: flt.state, reason: flt.reason, days: flt.days } : { q: cur.q };
    const page = await api.get('/api/items', { group: key, offset, limit, order: serverOrder(), ...judged });
    cur.rows = more ? cur.rows.concat(page.rows) : page.rows;
    cur.total = page.total;
    open.set(key, cur);
  }

  async function applyOps(ops) {
    try {
      selection = await api.post('/api/spec/ops', { ops });
    } catch (e) {
      if (e instanceof ApiError) {
        reportError(e);
        selection = await api.get('/api/spec');   // show what the server holds, not what the click suggested
      } else {
        throw e;
      }
    }
    await Promise.all([...open.keys()].map((key) => loadItems(key, true)));
    keepFocus(root, render);
  }

  // what is shown -------------------------------------------------------------------------------------------

  const judgedAt = () => flt.days || (counts && counts.days);

  function describe() {
    const days = judgedAt();
    if (flt.state === 'selected') return t('portal.filter.offered', { days });
    if (flt.state === 'too_recent') return t('portal.filter.too_recent', { days });
    if (flt.state === 'too_recent_floor') return t('portal.filter.floor_held');
    if (flt.state === 'other') return t('portal.filter.other');
    if (flt.state) return flt.state.split('|').map((s) => t(`decision.${s}`)).join(' / ');
    if (flt.reason) return t('portal.filter.protected_reason', { reason: t(`cli.reason.${flt.reason}`) });
    if (flt.report_only === '1') return t('portal.filter.report_only');
    if (flt.tier) return t(`portal.tier.${flt.tier}`);
    if (flt.domain) return t(`domain.${flt.domain}`);
    return t('portal.filter.everything');
  }

  function chips() {
    const list = [];
    const add = (key, text) => list.push({ key, text });
    if (flt.domain) add('domain', t(`domain.${flt.domain}`));
    if (flt.tier) add('tier', t(`portal.tier.${flt.tier}`));
    if (flt.report_only === '1') add('report_only', t('portal.filter.report_only'));
    if (flt.report_only === '0') add('report_only', t('portal.filter.has_action'));
    if (flt.state) add('state', STATES.includes(flt.state) ? t(`portal.filter.state.${flt.state}`) : flt.state);
    if (flt.state) add('days', t('portal.filter.days', { days: judgedAt() }));
    if (flt.reason) add('reason', t(`cli.reason.${flt.reason}`));
    return list;
  }

  const without = (...keys) => Object.fromEntries(Object.entries(flt).filter(([k]) => !keys.includes(k)));
  const withFilter = (changes) => {
    const next = { ...flt, ...changes };
    if (!next.state) delete next.days;
    return browseHref(next);
  };

  function filterBar() {
    const list = chips();
    const sel = (name, value, options) => h('select', { data: { change: `filter-${name}` }, 'data-fid': `f:${name}`, 'data-testid': `filter-${name}` },
      options.map(([v, label]) => h('option', { value: v, selected: v === value, text: label })));
    const n = (c) => (c ? ` (${fmtCount(c.items)})` : '');
    const tierValue = flt.report_only === '1' ? 'report_only' : (flt.tier || '');
    return h('div', { class: 'filters', 'data-testid': 'filters' },
      h('div', { class: 'toolbar' },
        h('label', { class: 'inline' }, h('span', { class: 'muted', text: t('portal.browse.area') }),
          sel('domain', flt.domain || '', [['', t('portal.browse.all')], ...DOMAINS.map((d) => [d, t(`domain.${d}`)])])),
        h('label', { class: 'inline' }, h('span', { class: 'muted', text: t('cli.col.tier') }),
          sel('tier', tierValue, [['', t('portal.browse.all')], ...['junk', 'review', 'protected'].map((x) => [x, t(`portal.tier.${x}`)]),
            ['report_only', t('portal.filter.report_only') + n(counts && counts.report_only)]])),
        h('label', { class: 'inline' }, h('span', { class: 'muted', text: t('portal.filter.state_label') }),
          sel('state', STATES.includes(flt.state) ? flt.state : (flt.state || ''), [['', t('portal.browse.all')],
            ...STATES.map((x) => [x, t(`portal.filter.state.${x}`) + n(counts && counts.states[x])])])),
        h('label', { class: 'inline' }, h('span', { class: 'muted', text: t('portal.filter.reason_label') }),
          sel('reason', flt.reason || '', [['', t('portal.browse.all')],
            ...REASONS.filter((r) => counts && counts.reasons[r]).map((r) => [r, t(`cli.reason.${r}`) + n(counts.reasons[r])])])),
        h('input', { type: 'search', value: search.text, placeholder: t('portal.browse.filter_groups'), 'aria-label': t('portal.browse.filter_groups'),
          data: { change: 'filter-text' }, 'data-fid': 'f:text' })),
      list.length ? h('div', { class: 'chips', 'data-testid': 'chips' },
        list.map((c) => h('a', { class: 'chip', href: withFilter({ [c.key]: undefined }), 'data-chip': c.key,
          title: t('portal.filter.remove') }, c.text, icon('close', 12))),
        h('a', { class: 'chip chip-clear', href: '#/browse', 'data-testid': 'clear-filters', text: t('portal.filter.clear') })) : null);
  }

  // groups and items ----------------------------------------------------------------------------------------

  const DECISION_DOT = { selected: 'junk', unselected: 'neutral', too_recent: 'review', too_recent_floor: 'review', excluded: 'neutral',
    protected: 'protected', no_gain: 'outside', unsupported_action: 'outside', domain_off: 'outside' };

  // "Claude Code sessions (~/.claude/projects/x)" -> the name, and the path as a second line
  function splitLabel(g) {
    const m = g.label.match(/^(.*?) \(([^()]*\/[^()]*)\)$/);
    return m ? { name: m[1], where: m[2] } : { name: g.label, where: null };
  }

  const groupAgo = (g) => (state.scan && state.scan.finished_at
    ? fmtAgo(Math.max(0, (state.scan.finished_at - g.newest_ns / 1e9) / 86400)) : '-');

  function actionSelect(g) {
    if (preview) return null;
    const sel = selection.spec.groups[g.key];
    if (!sel || g.actions.length < 2) return null;
    const current = sel.action || g.default_action;
    return h('label', { class: 'inline' }, h('span', { class: 'muted', text: t('portal.browse.action') }),
      h('select', { data: { change: 'set-action', key: g.key }, 'data-fid': `act:${g.key}` },
        g.actions.map((a) => h('option', { value: a, selected: a === current, text: t(`action.${a}`) }))));
  }

  function adviceLine(g) {
    if (!advice || advice.error || !advice.byGroup[g.key]) return null;
    const rec = advice.byGroup[g.key];
    return h('p', { class: 'advice' },
      badge(t(`portal.advisor.verdict.${rec.verdict}`), VERDICT_KIND[rec.verdict]), ' ',
      h('span', { text: t('portal.browse.advice', { provider: advice.result.provider, reason: rec.reason }) }),
      advice.result.stale ? [' ', badge(t('portal.advisor.stale'), 'warn')] : null);
  }

  // the tier (a group of another tool's is that, once), and what else the page says about the group
  function tierCell(g, sel, s) {
    return [groupBadge(g), sel && s.selected === 0 ? badge(t('portal.browse.none_qualify'), 'warn') : null];
  }

  // how many qualify (with the selection) or how many items there are, and what is held back
  function countCell(g, sel, s) {
    if (preview) return h('span', { 'data-testid': 'group-match', text: pluralCount('portal.hero.items', g.match.items) });
    const matched = g.match && g.match.items !== g.items
      ? h('span', { class: 'sub', text: t('portal.filter.matched', { count: fmtCount(g.match.items), size: fmtBytes(g.match.alloc) }) }) : null;
    if (!selectable(g) || !sel) return [h('span', { text: pluralCount('portal.hero.items', g.items) }), matched];
    const extra = [];
    if (s.too_recent) extra.push(t('portal.browse.n_too_recent', { count: fmtCount(s.too_recent) }));
    if (s.floor_held) extra.push(t('portal.browse.n_floor_held', { count: fmtCount(s.floor_held) }));
    if (s.excluded) extra.push(t('portal.browse.n_excluded', { count: fmtCount(s.excluded) }));
    return [h('strong', { 'data-testid': 'group-qualify', text: t('portal.browse.qualify', { selected: fmtCount(s.selected), total: fmtCount(s.total) }) }),
      extra.length ? h('span', { class: 'sub', text: extra.join(' · ') }) : null, matched];
  }

  function itemRow(g, it) {
    const d = it.decision;
    const canToggle = !preview && selectable(g);
    let control = h('span', { class: 'lock', 'aria-hidden': 'true' });   // nothing to tick; the column stays aligned
    if (canToggle) {
      const checked = d.state === 'selected';
      const disabled = !['selected', 'unselected', 'excluded'].includes(d.state);
      control = h('input', { type: 'checkbox', checked, disabled, 'aria-label': t('portal.browse.select_item', { path: it.path }),
        data: { change: 'toggle-item', key: it.key, group: g.key }, 'data-fid': `item:${it.key}` });
    }
    const reason = !preview && !selectable(g) && g.actions.length === 0 && g.tier !== 'protected' ? t('reason.report_only') : d.text;
    const shown = preview || selectable(g) || g.tier === 'protected' ? d.state : 'unsupported_action';
    const recent = canToggle && d.state === 'too_recent';
    const files = it.unit === 'bundle' ? pluralCount('portal.browse.files', it.files) : null;
    const status = () => [dotLabel(DECISION_DOT[shown] || 'neutral', t(`decision.${shown}`)), h('span', { class: 'sub', text: reason })];
    return h('tr', { class: `item item-${shown}`, 'data-testid': 'item', 'data-state': shown, 'data-path': it.path },
      h('td', { class: 'c-check' }, control),
      h('td', { class: 'c-path' }, midPath(it.path), files ? h('span', { class: 'sub', text: files }) : null,
        it.evidence_text.map((text) => h('span', { class: 'sub evidence', text })),
        h('span', { class: 'sub meta-sm' }, [fmtBytes(it.alloc), fmtAge(it.age_days, it.age_basis)].join(' · ')),
        h('span', { class: 'sub meta-sm status-sm' }, status())),
      h('td', { class: 'c-size num hide-sm', text: fmtBytes(it.alloc) }),
      h('td', { class: 'c-changed hide-md', text: fmtAge(it.age_days, it.age_basis) }),
      h('td', { class: 'c-status hide-sm' }, status()),
      h('td', { class: 'c-action' }, recent ? h('button', { class: 'btn small', data: { action: 'include-anyway', key: it.key, group: g.key },
        'data-fid': `inc:${it.key}`, text: t('portal.browse.include_anyway') }) : null));
  }

  const ITEM_GETTERS = {
    path: (it) => it.path.toLowerCase(), size: (it) => it.alloc, changed: (it) => it.age_days,
    status: (it) => it.decision.state,
  };

  function itemsTable(g, o) {
    if (!o.rows.length) return h('p', { class: 'muted', text: t('portal.browse.no_items') });
    const server = serverOrder();
    const done = (itemSort.col === 'size' && itemSort.dir === 'desc') || (itemSort.col === 'changed') || (itemSort.col === 'path' && server === 'path');
    const rows = done ? o.rows : sortRows(o.rows, itemSort, ITEM_GETTERS);
    const th = (label, col, cls) => sortTh(label, col, itemSort, 'items', cls);
    return h('table', { class: 'data nested', 'data-testid': 'items' },
      colgroup('c-check', 'c-path', 'c-size hide-sm', 'c-changed hide-md', 'c-status hide-sm', 'c-action'),
      h('thead', {}, h('tr', {}, plainTh(t('portal.col.select'), 'c-check sr-only-th'), th(t('portal.col.path'), 'path', 'c-path'),
        th(t('portal.col.size'), 'size', 'c-size num hide-sm'), th(t('portal.col.changed'), 'changed', 'c-changed hide-md'),
        th(t('portal.col.status'), 'status', 'c-status hide-sm'), plainTh(t('portal.col.action'), 'c-action'))),
      h('tbody', {}, rows.map((it) => itemRow(g, it))));
  }

  function itemsPanel(g) {
    const o = open.get(g.key);
    const filtered = Boolean(flt.state || flt.reason);
    return h('div', { class: 'items-panel' },
      h('p', { class: 'effect-full', text: g.effect }),
      preview ? h('p', {}, h('a', { href: browseHref({ group: g.key }), text: t('portal.filter.open_group') })) : null,
      g.hint ? h('div', {}, h('p', { class: 'muted', text: t('portal.browse.hint_intro') }), codeBlock(g.hint)) : null,
      g.evidence_text.length ? h('ul', { class: 'plain muted' }, g.evidence_text.map((x) => h('li', { text: x }))) : null,
      h('div', { class: 'toolbar panel-tools' },
        filtered ? null : h('label', { class: 'search' }, h('span', { class: 'sr-only', text: t('portal.browse.search') }),
          h('input', { type: 'search', value: o.q, placeholder: t('portal.browse.search'),
            data: { change: 'search', key: g.key }, 'data-fid': `q:${g.key}` })),
        actionSelect(g)),
      itemsTable(g, o),
      h('div', { class: 'pager' },
        h('span', { class: 'muted', text: t('portal.browse.showing', { shown: fmtCount(o.rows.length), total: fmtCount(o.total) }) }),
        o.rows.length < o.total ? h('button', { class: 'btn small', data: { action: 'more', key: g.key }, 'data-fid': `more:${g.key}`,
          text: t('portal.browse.more') }) : null));
  }

  function groupBody(g) {
    const sel = preview ? null : selection.spec.groups[g.key];
    const s = preview ? null : selection.summary.groups[g.key];
    const isOpen = open.has(g.key);
    const canSelect = !preview && selectable(g);
    const { name, where } = splitLabel(g);
    const check = canSelect ? h('input', { type: 'checkbox', checked: Boolean(sel), 'aria-label': t('portal.browse.select_group', { label: g.label }),
      data: { change: 'toggle-group', key: g.key }, 'data-fid': `grp:${g.key}` })
      : h('span', { class: 'lock', 'aria-hidden': 'true' }, !preview && g.tier === 'protected' ? icon('lock', 16) : null);
    const size = fmtBytes(preview ? g.match.alloc : g.alloc);
    const metaMd = [t(`domain.${g.domain}`), groupAgo(g)].join(' · ');
    const row = h('tr', { class: 'group-row group-head' },
      h('td', { class: 'c-check' }, check),
      h('td', { class: 'c-name' },
        h('button', { class: 'group-title', 'aria-expanded': String(isOpen), data: { action: 'toggle-open', key: g.key }, 'data-fid': `open:${g.key}`, text: name }),
        where ? h('span', { class: 'sub' }, midPath(where)) : null,
        // a narrow table folds columns under the name: area and age first, then (and only then) the tier and the figures
        h('span', { class: 'sub meta-md' }, metaMd),
        h('span', { class: 'sub meta-sm' }, dotLabel(groupTier(g), t(`portal.tier.${groupTier(g)}`)), ' · ', [size, preview ? pluralCount('portal.hero.items', g.match.items)
          : (selectable(g) && sel ? t('portal.browse.qualify', { selected: fmtCount(s.selected), total: fmtCount(s.total) }) : pluralCount('portal.hero.items', g.items))].join(' · ')),
        adviceLine(g)),
      h('td', { class: 'c-tier hide-sm' }, h('span', { class: 'tags' }, tierCell(g, sel, s))),
      h('td', { class: 'c-area hide-md', text: t(`domain.${g.domain}`) }),
      h('td', { class: 'c-count hide-sm' }, countCell(g, sel, s)),
      h('td', { class: 'c-size num hide-sm', text: size }),
      h('td', { class: 'c-changed hide-md', text: groupAgo(g) }),
      h('td', { class: 'c-chev' }, h('button', { class: 'icon-btn small', 'aria-expanded': String(isOpen), 'aria-label': t('portal.col.expand'),
        data: { action: 'toggle-open', key: g.key }, 'data-fid': `chev:${g.key}` }, icon('chevron', 16))));
    return h('tbody', { class: `group-body tier-${g.tier}${isOpen ? ' open' : ''}`, id: `group-${g.key}`, 'data-testid': 'group', 'data-key': g.key },
      row, isOpen ? h('tr', { class: 'detail-row' }, h('td', { colspan: 8 }, itemsPanel(g))) : null);
  }

  function groupGetters() {
    return {
      name: (g) => g.label.toLowerCase(), tier: (g) => TIER_RANK[groupTier(g)], area: (g) => t(`domain.${g.domain}`),
      count: (g) => (preview ? g.match.items : (selection.summary.groups[g.key] ? selection.summary.groups[g.key].selected : g.items)),
      size: (g) => (preview ? g.match.alloc : g.alloc), changed: (g) => g.newest_ns,
    };
  }

  // The full list is sorted first and then cut, so a header sorts everything and not only what is drawn.
  function groupsTable(list) {
    const th = (label, col, cls) => sortTh(label, col, groupSort, 'groups', cls);
    const sorted = sortRows(list, groupSort, groupGetters());
    if (want) shownGroups = Math.max(shownGroups, sorted.findIndex((g) => g.key === want) + 1);   // a link to a group shows it
    const part = sorted.slice(0, shownGroups);
    const more = sorted.length - part.length;
    return [h('div', { class: 'table-card' }, h('table', { class: 'data sticky groups-table' },
      colgroup('c-check', 'c-name', 'c-tier hide-sm', 'c-area hide-md', 'c-count hide-sm', 'c-size hide-sm', 'c-changed hide-md', 'c-chev'),
      h('thead', {}, h('tr', {}, plainTh(t('portal.col.select'), 'c-check sr-only-th'), th(t('portal.col.name'), 'name', 'c-name'),
        th(t('cli.col.tier'), 'tier', 'c-tier hide-sm'), th(t('portal.browse.area'), 'area', 'c-area hide-md'),
        th(t('portal.col.count'), 'count', 'c-count hide-sm'), th(t('portal.col.size'), 'size', 'c-size num hide-sm'),
        th(t('portal.col.changed'), 'changed', 'c-changed hide-md'), plainTh(t('portal.col.expand'), 'c-chev sr-only-th'))),
      part.map(groupBody))),
    h('div', { class: 'pager groups-pager', 'data-testid': 'groups-pager' },
      h('span', { class: 'muted', text: t('portal.browse.showing', { shown: fmtCount(part.length), total: fmtCount(sorted.length) }) }),
      more ? h('button', { class: 'btn', data: { action: 'more-groups' }, 'data-fid': 'more-groups', 'data-testid': 'more-groups',
        text: pluralCount('portal.browse.more_groups', more) }) : null)];
  }

  function visibleGroups() {
    const text = search.text.trim().toLowerCase();
    // what can be cleaned first; within a tier the server's order (biggest first) stands
    return groups.filter((g) => !text || g.label.toLowerCase().includes(text) || g.key.toLowerCase().includes(text))
      .sort((a, b) => TIER_RANK[a.tier] - TIER_RANK[b.tier]);
  }

  // the side panel ------------------------------------------------------------------------------------------

  function summaryPanel() {
    const s = selection.summary;
    const left = Object.entries(s.scope_by_state).filter(([k]) => k !== 'selected');
    const none = s.count === 0;
    const noneReason = left.length ? t('portal.summary.none_qualify', { list: left.map(([k, v]) => `${fmtCount(v.count)} ${t(`decision.${k}`).toLowerCase()}`).join(', ') })
      : t('portal.summary.none_selected');
    return h('div', { class: 'summary-inner' },
      h('h2', { text: t('portal.summary.title') }),
      h('p', { class: 'big', 'data-testid': 'summary-count', text: pluralCount('portal.summary.selected', s.count, { size: fmtBytes(s.alloc) }) }),
      h('div', { class: 'summary-actions' },
        h('button', { class: 'btn primary', disabled: none, data: { action: 'create-plan' }, 'data-fid': 'create-plan',
          'data-testid': 'create-plan', 'aria-describedby': none ? 'create-plan-why' : null, text: t('portal.summary.create') }),
        h('button', { class: 'btn summary-toggle', 'aria-expanded': String(summaryOpen), data: { action: 'toggle-summary' }, 'data-fid': 'toggle-summary',
          'data-testid': 'summary-toggle', text: t(summaryOpen ? 'portal.summary.hide' : 'portal.summary.show') })),
      none ? h('p', { id: 'create-plan-why', class: 'muted', text: noneReason }) : null,
      h('div', { class: 'summary-more' },
        h('label', { class: 'inline filter' }, h('span', { text: t('portal.summary.filter') }),
          h('input', { type: 'number', min: '0', step: 'any', value: selection.spec.filter_min_age_days, class: 'days',
            data: { change: 'set-filter' }, 'data-fid': 'filter-days', 'data-testid': 'age-filter' }),
          h('span', { text: t('portal.summary.days') })),
        Object.entries(s.by_action).map(([action, v]) => h('p', { class: 'action-line', 'data-testid': `action-${action}`, text: actionLine(action, v) })),
        warningList(s.warnings, 'fold'),
        left.length ? h('details', { open: none }, h('summary', { text: t('portal.summary.left_out') }), stateTable(Object.fromEntries(left))) : null,
        h('div', { class: 'summary-actions' },
          h('button', { class: 'btn', data: { action: 'select-junk' }, 'data-fid': 'select-junk', 'data-testid': 'select-junk', text: t('portal.summary.select_junk') }),
          h('button', { class: 'btn', data: { action: 'clear' }, 'data-fid': 'clear', text: t('portal.summary.clear') }))));
  }

  // A preview has nothing to tick. Where the list is what "review and clean" would clean, the button is here too.
  function previewPanel() {
    return h('div', { class: 'summary-inner' },
      h('h2', { text: describe() }),
      h('p', { class: 'big', text: pluralCount('portal.summary.selected', scope.items, { size: fmtBytes(scope.alloc) }) }),
      h('p', { class: 'muted', text: t('portal.filter.preview_note') }),
      quick ? h('div', { class: 'summary-actions' },
        h('button', { class: 'btn primary', disabled: quick.summary.count === 0, data: { action: 'review-clean' }, 'data-testid': 'review-clean',
          text: t('portal.hero.button', { size: fmtBytes(quick.summary.alloc) }) })) : null,
      h('div', { class: 'summary-actions' },
        h('a', { class: 'btn', href: browseHref({}), text: t('portal.filter.choose') }),
        h('a', { class: 'btn', href: '#/overview', text: t('portal.nav.overview') })));
  }

  function render() {
    if (!loaded) return;
    if (!preview && !selection) {
      replace(root, h('div', { class: 'page-head' }, h('h1', { text: t('portal.nav.browse') })),
        emptyState({ iconName: 'browse', title: t('portal.nav.browse'), body: t('portal.browse.no_scan'),
          action: h('a', { class: 'btn primary', href: '#/overview', text: t('portal.nav.overview') }) }));
      return;
    }
    if (!root.querySelector('.browse-layout')) {
      listEl = h('div', { class: 'groups-box', 'data-testid': 'groups' });
      summaryEl = h('aside', { class: 'summary', 'data-testid': 'summary', 'aria-label': t('portal.summary.title') });
      replace(root, h('div', { class: 'page-head' }, h('h1', { text: t('portal.nav.browse') })),
        h('p', { class: 'browse-header', id: 'browse-header', 'data-testid': 'browse-header' }),
        h('div', { class: 'browse-layout' }, h('div', { class: 'browse-main' }, h('div', { id: 'browse-toolbar' }), listEl), summaryEl));
    }
    replace(root.querySelector('#browse-header'), h('strong', { text: pluralCount('portal.filter.header', scope.items, { title: describe(), size: fmtBytes(scope.alloc) }) }));
    const visible = visibleGroups();
    nativeSlot = flt.report_only === '1' ? h('div', { class: 'native-slot' }) : null;
    replace(root.querySelector('#browse-toolbar'), filterBar(), advice && advice.error ? h('p', { class: 'banner banner-warn', text: t('portal.browse.advice_error', { reason: advice.error }) }) : null, nativeSlot);
    if (nativeSlot) {
      if (nativeMounted) nativePart.attach(nativeSlot);
      else {
        nativeMounted = true;
        nativePart.mount(nativeSlot).catch(reportError);
      }
    }
    // "shown only" is the tools' own table above and nothing else: the groups behind it would list the same tools twice
    const reportOnly = flt.report_only === '1';
    replace(listEl, reportOnly ? (groups.length ? null : h('p', { class: 'muted', text: t('portal.browse.no_groups') }))
      : (visible.length ? groupsTable(visible) : h('p', { class: 'muted', text: t('portal.browse.no_groups') })));
    summaryEl.classList.toggle('expanded', summaryOpen);
    replace(summaryEl, preview ? previewPanel() : summaryPanel());
    if (want) {
      const key = want;
      want = null;
      const el = [...listEl.querySelectorAll('.group-body')].find((e) => e.dataset.key === key);
      if (el) {
        el.scrollIntoView({ block: 'start' });
        el.querySelector('.group-title').focus({ preventScroll: true });
      }
    }
  }

  // creating a plan -------------------------------------------------------------------------------------------

  async function createPlan() {
    const expected = { scan_id: selection.scan_id, spec_version: selection.spec.version, digest: selection.digest,
      resolved_at: selection.resolved_at };
    try {
      const plan = await api.post('/api/plans', { expected });
      location.hash = `#/plan/${plan.id}`;
    } catch (e) {
      await refused(e, { create: createPlan, reload: async () => { await loadAll(); return { count: selection.summary.count, alloc: selection.summary.alloc }; } });
    }
  }

  const reviewAndClean = () => quickPlan(() => quick, async () => {
    await loadAll();
    return quick ? { count: quick.summary.count, alloc: quick.summary.alloc } : { count: 0, alloc: 0 };
  });

  // events ----------------------------------------------------------------------------------------------------

  const go = (changes) => { location.hash = withFilter(changes); };

  const handlers = {
    click: {
      'toggle-open': async (el) => {
        const key = el.dataset.key;
        if (open.has(key)) open.delete(key);
        else await loadItems(key);
        keepFocus(root, render);
      },
      more: async (el) => {
        await loadItems(el.dataset.key, true, true);
        keepFocus(root, render);
      },
      'select-junk': () => applyOps([{ op: 'select_tier', tier: 'junk', on: true }]),
      clear: () => applyOps([{ op: 'clear' }]),
      'create-plan': createPlan,
      'review-clean': reviewAndClean,
      'more-groups': () => { shownGroups += GROUP_PAGE; keepFocus(root, render); },
      'toggle-summary': () => { summaryOpen = !summaryOpen; keepFocus(root, render); },
      sort: async (el) => {
        if (el.dataset.table === 'groups') {
          groupSort = nextSort(groupSort, el.dataset.col, ['size', 'count', 'changed'].includes(el.dataset.col) ? 'desc' : 'asc');
        } else {
          itemSort = nextSort(itemSort, el.dataset.col, ['size', 'changed'].includes(el.dataset.col) ? 'desc' : 'asc');
          await Promise.all([...open.keys()].map((key) => loadItems(key, true)));
        }
        keepFocus(root, render);
      },
      'include-anyway': async (el) => {
        const g = groups.find((x) => x.key === el.dataset.group);
        const sel = selection.spec.groups[g.key];
        const action = (sel && sel.action) || g.default_action;
        const key = el.dataset.key;
        await applyOps([{ op: 'toggle_item', item: key, on: true, action }]);
        const row = open.get(g.key).rows.find((r) => r.key === key);
        if (row && row.decision.state !== 'selected') {
          toast(t('portal.browse.include_refused', { path: row.path, reason: row.decision.text }), 'warn');
        }
      },
    },
    change: {
      'toggle-group': (el) => applyOps([{ op: 'select_group', group: el.dataset.key, on: el.checked }]),
      'set-action': (el) => applyOps([{ op: 'set_action', group: el.dataset.key, action: el.value }]),
      'toggle-item': (el) => applyOps([{ op: 'toggle_item', item: el.dataset.key, on: el.checked }]),
      'set-filter': (el) => {
        const days = Number(el.value);
        if (el.value === '' || !Number.isFinite(days) || days < 0) {
          toast(t('portal.summary.bad_days'), 'error');
          keepFocus(root, render);
          return null;
        }
        return applyOps([{ op: 'set_filter', min_age_days: days }]);
      },
      search: async (el) => {
        const o = open.get(el.dataset.key);
        o.q = el.value;
        await loadItems(el.dataset.key);
        keepFocus(root, render);
      },
      'filter-domain': (el) => go({ domain: el.value || undefined }),
      'filter-tier': (el) => {
        if (el.value === 'report_only') go({ tier: undefined, report_only: '1' });
        else go({ tier: el.value || undefined, report_only: undefined });
      },
      'filter-state': (el) => go(el.value ? { state: el.value, tier: flt.tier || 'junk', days: judgedAt() } : { state: undefined }),
      'filter-reason': (el) => go(el.value ? { reason: el.value, tier: 'protected' } : { reason: undefined }),
      'filter-text': (el) => { search.text = el.value; shownGroups = GROUP_PAGE; keepFocus(root, render); },
    },
  };

  return {
    async mount(el, route) {
      root = el;
      want = route.query.group || null;
      flt = Object.fromEntries(FILTER_KEYS.filter((k) => route.query[k]).map((k) => [k, route.query[k]]));
      preview = Boolean(flt.state);
      replace(root, loading());
      delegate(root, 'click', 'action', handlers.click);
      delegate(root, 'change', 'change', handlers.change);
      if (want) open.set(want, { rows: [], total: 0, q: '' });
      disposers.push(on('events', (events) => {
        if (events.some((e) => e.type === 'scan.done' || e.type.startsWith('advisor.'))) loadAll().catch(reportError);
      }));
      disposers.push(on('reset', () => loadAll().catch(reportError)));
      await loadAll();
    },
    unmount() {
      disposers.forEach((f) => f());
      if (nativeMounted) nativePart.unmount();
    },
  };
}
