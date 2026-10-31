// Insights: where the disk space is, how it changed, what the cleanups achieved and what is left to do. One request
// (/api/insights), one window control above every chart, and every chart has a table with the same numbers.
import { api, ApiError } from '../api.mjs';
import { destroyAll, legend, lineArea, rowBars, sparkline, stackedBar100, stackedColumns } from '../charts.mjs';
import { h, replace } from '../dom.mjs';
import { browseHref, fmtBytes, fmtDate, fmtDuration, fmtPct, fmtTime, pluralCount } from '../format.mjs';
import { has, t } from '../i18n.mjs';
import { icon } from '../icons.mjs';
import { timeLabel } from '../scale.mjs';
import { on, setJobFromResponse, state } from '../state.mjs';
import { copyButton, emptyState, isReportOnly, loading, pickTab, reportError, subTabs, tabPanel } from '../ui.mjs';

const WINDOWS = [7, 30, 90, 0];
const DEFAULT_WINDOW = 30;
const STORE = 'disk-steward-insights-window';
const DRAW_ORDER = ['junk', 'review', 'report_only', 'protected', 'outside', 'free'];   // left to right in the bar
const STACK_ORDER = ['protected', 'report_only', 'review', 'junk'];                      // bottom to top in the columns
const SEVERITY_ICON = { act: 'cleanup', warn: 'alert', review: 'browse', tip: 'advisor' };

const LINKS = {
  junk: '#/overview',
  review: browseHref({ tier: 'review', report_only: '0' }),   // the rows the overview calls "needs your decision": the bar and Browse then show one number
  protected: browseHref({ tier: 'protected' }),
  report_only: browseHref({ report_only: '1' }),
};

function savedWindow() {
  try {
    const saved = localStorage.getItem(STORE);
    return saved !== null && WINDOWS.includes(Number(saved)) ? Number(saved) : DEFAULT_WINDOW;
  } catch (e) {
    return DEFAULT_WINDOW;   // storage is blocked: the default window
  }
}

const safeHref = (href) => (typeof href === 'string' && href.startsWith('#/') ? href : '#/overview');
const domainLabel = (d) => (has(`domain.${d}`) ? t(`domain.${d}`) : d);

export function create() {
  let root;
  let data = null;
  let win = DEFAULT_WINDOW;
  let tab = null;                // the sub-tab under the KPI row
  const views = new Map();       // chart id -> 'chart' | 'table', kept across redraws
  let reportOnly = { scan: null, keys: new Set() };
  const charts = [];
  const disposers = [];

  // data ----------------------------------------------------------------------------------------------------

  async function load() {
    if (data) root.dataset.busy = '1';   // the frame stays, dimmed, while the numbers come
    try {
      data = await api.get('/api/insights', { window: win });
      await loadReportOnly();
    } finally {
      delete root.dataset.busy;
    }
    render();
  }

  // The groups that belong to another tool: a growth row is labelled by its tier, and these are not "needs your decision".
  // Asked once per scan.
  async function loadReportOnly() {
    const scan = data.environment.last_scan;
    if (!scan) { reportOnly = { scan: null, keys: new Set() }; return; }
    if (reportOnly.scan === scan.id) return;
    try {
      const { groups } = await api.get('/api/groups');
      reportOnly = { scan: scan.id, keys: new Set(groups.filter(isReportOnly).map((g) => g.key)) };
    } catch (e) {
      if (!(e instanceof ApiError && e.code === 'NO_SCAN')) throw e;   // the scan vanished since the answer above
      reportOnly = { scan: null, keys: new Set() };
    }
  }

  async function setWindow(w) {
    if (w === win) return;
    win = w;
    try {
      localStorage.setItem(STORE, String(w));
    } catch (e) {
      // storage is blocked: the choice lasts for this visit
    }
    history.replaceState(null, '', `#/insights?window=${w}&tab=${tab}`);   // the address names the window without a new page load
    await load();
  }

  const label = (key) => {
    const found = data.composition.find((c) => c.key === key);
    return found ? found.label : key;
  };
  const tracked = (el) => {
    charts.push(el);
    return el;
  };

  // building blocks -----------------------------------------------------------------------------------------

  function dataTable(columns, rows, testid) {
    return h('div', { class: 'table-wrap nowrap', 'data-testid': testid }, h('table', {},
      h('thead', {}, h('tr', {}, columns.map((c) => h('th', { class: c.num ? 'num' : null, text: c.label })))),
      h('tbody', {}, rows.map((r) => h('tr', { 'data-testid': r.testid, 'data-id': r.id, 'data-kind': r.kind },
        r.cells.map((cell, i) => {
          const body = r.href ? h('a', { class: 'figure', href: r.href, tabindex: i ? '-1' : null }, cell) : cell;
          return h('td', { class: columns[i].num ? 'num' : columns[i].wrap ? 'wrap' : null }, body);
        }))))));
  }

  // The Chart | Table switch of one chart: the surface the chart sits on, the switch, and the table that takes its place.
  // both: the table is the record itself (cleanups), so it sits under the chart and there is nothing to switch.
  function viewSwitch(id, chartEl, tableEl, both = false) {
    const mode = views.get(id) || 'chart';
    const surface = h('div', { class: 'chart-surface', 'data-testid': `${id}-surface` }, chartEl);
    const seg = (name) => {
      const b = h('button', { 'aria-pressed': String(mode === name), 'data-testid': `${id}-show-${name}`, text: t(`portal.insights.view_${name}`) });
      b.addEventListener('click', () => {
        views.set(id, name);
        surface.hidden = name === 'table';
        tableEl.hidden = name === 'chart';
        for (const other of toggle.querySelectorAll('button')) other.setAttribute('aria-pressed', String(other === b));
      });
      return b;
    };
    const toggle = both ? null : h('div', { class: 'seg-ctl', role: 'group', 'aria-label': t('portal.insights.view_label') }, seg('chart'), seg('table'));
    if (!both) {
      surface.hidden = mode === 'table';
      tableEl.hidden = mode === 'chart';
    }
    return { surface, toggle };
  }

  // A section with a heading, a Chart | Table switch and the same numbers either way. extra: more blocks in the same card.
  function section(id, title, sub, chartEl, tableEl, note, both = false, extra = null) {
    const { surface, toggle } = viewSwitch(id, chartEl, tableEl, both);
    return h('section', { class: 'card section', 'data-testid': `section-${id}` },
      h('div', { class: 'section-head' }, h('div', { class: 'section-title' }, h('h2', { text: title }), sub ? h('p', { text: sub }) : null), toggle),
      surface, tableEl, note || null, extra);
  }

  // header and KPI row --------------------------------------------------------------------------------------

  function header() {
    const group = h('div', { class: 'seg-ctl', role: 'group', 'aria-label': t('portal.insights.window_label'), 'data-testid': 'window' },
      WINDOWS.map((w) => {
        const b = h('button', { 'aria-pressed': String(w === win), 'data-testid': `window-${w}`,
          text: w === 0 ? t('portal.insights.window_all') : t('portal.insights.window_days', { days: w }) });
        b.addEventListener('click', () => setWindow(w).catch(reportError));
        return b;
      }));
    return h('div', { class: 'ins-head' },
      h('div', {}, h('h1', { text: t('portal.nav.insights') }),
        h('p', { class: 'lede', text: t('portal.insights.lede') }),
        h('p', { class: 'chart-note', 'data-testid': 'generated', text: t('portal.insights.updated', { when: fmtTime(data.generated_at) }) })),
      group);
  }

  const vsText = () => (data.window_days === 0 ? t('portal.insights.vs_start') : t('portal.insights.vs_days', { days: data.window_days }));

  function delta(d, goodWhenUp) {
    if (d === null || d === undefined) return h('span', { class: 'stat-note', text: t('portal.insights.no_baseline') });
    const tone = goodWhenUp === null || d === 0 ? 'flat' : (d > 0) === goodWhenUp ? 'good' : 'bad';
    return h('span', { class: `stat-delta ${tone}`, 'data-delta': d },
      h('span', { 'aria-hidden': 'true', text: d > 0 ? '▲' : d < 0 ? '▼' : '–' }), h('span', { text: fmtBytes(d, true) }),
      h('span', { class: 'vs', text: vsText() }));
  }

  function stat(id, labelText, valueNodes, middle, foot) {
    return h('article', { class: 'stat', 'data-testid': `kpi-${id}` },
      h('div', { class: 'stat-label', text: labelText }),
      h('div', { class: 'stat-value', 'data-testid': `kpi-${id}-value` }, valueNodes),
      middle, h('div', { class: 'stat-foot' }, foot));
  }

  function kpis() {
    const k = data.kpis;
    const series = (s) => sparkline(s.series.map((p) => p.v));
    const safe = k.safe_to_clean;
    const growth = k.growth_per_day;
    return h('div', { class: 'kpis', 'data-testid': 'kpis' },
      stat('free', t('portal.insights.kpi.free'), h('span', { 'data-bytes': k.free.value, text: fmtBytes(k.free.value) }),
        delta(k.free.delta, true), [series(k.free)]),
      stat('scanned', t('portal.insights.kpi.scanned'),
        k.scanned.value === null ? h('span', { text: '—' }) : h('span', { 'data-bytes': k.scanned.value, text: fmtBytes(k.scanned.value) }),
        delta(k.scanned.delta, null), [series(k.scanned)]),
      stat('freed', t('portal.insights.kpi.freed'), h('span', { 'data-bytes': k.freed_total.value, text: fmtBytes(k.freed_total.value) }),
        h('span', { class: 'stat-note', 'data-testid': 'kpi-freed-note',
          text: pluralCount('portal.insights.kpi.cleanups', k.freed_total.count) + (k.freed_total.since ? ` · ${t('portal.insights.kpi.since', { when: fmtDate(k.freed_total.since) })}` : '') }), []),
      stat('safe', t('portal.insights.kpi.safe'), h('span', { 'data-bytes': safe.bytes, text: fmtBytes(safe.bytes) }),
        h('span', { class: 'stat-note', text: pluralCount('portal.insights.kpi.safe_items', safe.items, { days: safe.days }) }),
        h('a', { class: 'btn small primary', href: '#/overview', 'data-testid': 'kpi-safe-button' }, icon('cleanup', 14), t('portal.insights.kpi.safe_button'))),
      stat('growth', t('portal.insights.kpi.growth'),
        growth === null || growth === undefined ? h('span', { text: '—' })
          : [h('span', { 'data-bytes': growth, text: fmtBytes(growth, true) }), h('small', { text: t('portal.insights.per_day_unit') })],
        h('span', { class: 'stat-note', text: t(growth === null || growth === undefined ? 'portal.insights.kpi.growth_none' : 'portal.insights.kpi.growth_hint') }), []));
  }

  // composition ---------------------------------------------------------------------------------------------

  const SCANNED = ['junk', 'review', 'report_only', 'protected'];

  function composition() {
    const total = data.composition.reduce((a, c) => a + c.bytes, 0);
    const known = DRAW_ORDER.map((key) => data.composition.find((c) => c.key === key)).filter(Boolean);
    const segments = known.map((c) => ({
      key: c.key, label: c.label, value: c.bytes, href: LINKS[c.key] || null,
      hint: c.key === 'outside' ? t('portal.insights.comp.outside_hint') : null,
      tip: { title: c.label, rows: [{ cls: `s-${c.key}`, label: t('portal.insights.comp.size'), value: fmtBytes(c.bytes) },
        { cls: `s-${c.key}`, label: t('portal.insights.comp.share'), value: fmtPct(total ? c.bytes / total : 0) }] },
    }));
    const chart = tracked(stackedBar100({ segments, valueText: fmtBytes,
      ariaLabel: t('portal.insights.comp.title') }));
    const table = dataTable([{ label: t('portal.insights.col.category') }, { label: t('portal.insights.col.size'), num: true }, { label: t('portal.insights.col.share'), num: true }],
      known.map((c) => ({ testid: `comp-row-${c.key}`, id: c.key, cells: [c.label, fmtBytes(c.bytes), fmtPct(total ? c.bytes / total : 0)] })), 'comp-table');
    // what "not scanned" is, and where the biggest of it can be measured
    const caption = h('p', { class: 'chart-note', 'data-testid': 'comp-note' }, t('portal.insights.comp.not_scanned_note'), ' ',
      h('a', { href: '#/overview?apps=1', 'data-testid': 'comp-apps-link', text: t('portal.insights.comp.apps_link') }));
    return section('comp', t('portal.insights.comp.title'), t('portal.insights.comp.sub'), chart, table, caption, false, scannedBlock());
  }

  // The same four tiers as shares of what the scan covered: next to the whole disk they would be slivers.
  function scannedBlock() {
    const parts = SCANNED.map((key) => data.composition.find((c) => c.key === key)).filter(Boolean);
    const total = parts.reduce((a, c) => a + c.bytes, 0);
    if (!total) return null;
    const segments = parts.map((c) => ({
      key: c.key, label: c.label, value: c.bytes, href: LINKS[c.key],
      tip: { title: c.label, rows: [{ cls: `s-${c.key}`, label: t('portal.insights.comp.size'), value: fmtBytes(c.bytes) },
        { cls: `s-${c.key}`, label: t('portal.insights.scanned.share'), value: fmtPct(c.bytes / total) }] },
    }));
    const chart = tracked(stackedBar100({ segments, valueText: fmtBytes, prefix: 'scanned-', ariaLabel: t('portal.insights.scanned.title') }));
    const table = dataTable([{ label: t('portal.insights.col.category') }, { label: t('portal.insights.col.size'), num: true }, { label: t('portal.insights.scanned.share_col'), num: true }],
      parts.map((c) => ({ testid: `scanned-row-${c.key}`, id: c.key, cells: [c.label, fmtBytes(c.bytes), fmtPct(c.bytes / total)] })), 'scanned-table');
    const { surface, toggle } = viewSwitch('scanned', chart, table);
    return h('div', { class: 'subpanel', 'data-testid': 'section-scanned' },
      h('div', { class: 'section-head sub' }, h('div', { class: 'section-title' }, h('h3', { text: t('portal.insights.scanned.title') }),
        h('p', { text: t('portal.insights.scanned.sub', { size: fmtBytes(total) }) })), toggle),
      surface, table);
  }

  // space over time -----------------------------------------------------------------------------------------

  function scanTip(sc) {
    return { title: t('portal.insights.space.scan_title', { id: sc.id, when: fmtTime(sc.t) }) + (sc.backfilled ? ` · ${t('portal.insights.space.backfilled_tag')}` : ''),
      rows: [{ cls: 's-single', label: t('portal.insights.space.used'), value: fmtBytes(sc.used) },
        { cls: 's-single', label: t('portal.insights.space.free'), value: fmtBytes(sc.free) },
        { cls: 's-single', label: t('portal.insights.space.scanned'), value: fmtBytes(sc.scanned) }] };
  }

  const cleanupTime = (c) => (c.finished_at === null || c.finished_at === undefined ? c.created_at : c.finished_at);

  // a cleanup is a plan that was run, or a tool's own command that was run from the portal
  const isNative = (c) => c.kind === 'native';
  const cleanupId = (c) => c.plan_id || `native:${c.rule_id}:${c.finished_at}`;

  function cleanupTip(c) {
    return { title: t('portal.insights.space.cleanup_title', { when: fmtTime(cleanupTime(c)) }),
      rows: isNative(c)
        ? [{ cls: 's-freed', label: t('portal.insights.col.command'), value: c.command },
          { cls: 's-freed', label: t('portal.insights.col.freed'), value: fmtBytes(c.freed) }]
        : [{ cls: 's-freed', label: t('portal.insights.col.freed'), value: fmtBytes(c.freed) },
          { cls: 's-freed', label: t('portal.insights.col.items'), value: String(c.items_done) },
          { cls: 's-freed', label: t('portal.insights.col.skipped'), value: String(c.items_skipped) }] };
  }

  function space() {
    const { scans, cleanups } = data.history;
    if (!scans.length) {   // scans exist, but none inside this window
      return [h('section', { class: 'card section', 'data-testid': 'section-space' },
        h('div', { class: 'section-head' }, h('div', { class: 'section-title' }, h('h2', { text: t('portal.insights.space.title') }))),
        h('p', { class: 'muted', 'data-testid': 'window-empty', text: t('portal.insights.window_empty') }))];
    }
    const points = scans.map((sc) => ({ t: sc.t, v: sc.used, id: sc.id, tip: scanTip(sc) }));
    const events = cleanups.map((c) => ({ t: cleanupTime(c), id: cleanupId(c), badge: t('portal.insights.space.badge', { size: fmtBytes(c.freed) }),
      href: isNative(c) ? null : `#/plan/${c.plan_id}`, tip: cleanupTip(c) }));
    const line = tracked(lineArea({ points, events, ariaLabel: t('portal.insights.space.title') }));
    const span = scans.length ? scans[scans.length - 1].t - scans[0].t : 0;
    const columns = scans.map((sc) => ({ id: sc.id, label: timeLabel(sc.t, Math.max(span, 1)),
      values: { junk: sc.junk, review: sc.review, protected: sc.protected, report_only: sc.report_only },
      total: sc.junk + sc.review + sc.protected + sc.report_only,
      tip: { title: t('portal.insights.space.scan_title', { id: sc.id, when: fmtTime(sc.t) }) + (sc.backfilled ? ` · ${t('portal.insights.space.backfilled_tag')}` : ''),
        rows: [...STACK_ORDER].reverse().map((key) => ({ cls: `s-${key}`, label: label(key), value: fmtBytes(sc[key]) })) } }));
    const stack = tracked(stackedColumns({ columns, series: STACK_ORDER.map((key) => ({ key, label: label(key) })), labelLast: true,
      valueText: fmtBytes, testid: 'mix-col', ariaLabel: t('portal.insights.mix.title') }));
    const backfilled = scans.some((sc) => sc.backfilled);
    const lineTable = dataTable([{ label: t('portal.insights.col.scan') }, { label: t('portal.insights.col.time') }, { label: t('portal.insights.space.used'), num: true },
      { label: t('portal.insights.space.free'), num: true }, { label: t('portal.insights.space.scanned'), num: true }],
    scans.map((sc) => ({ testid: 'scan-row', id: sc.id, cells: [`#${sc.id}`, fmtTime(sc.t), fmtBytes(sc.used), fmtBytes(sc.free), fmtBytes(sc.scanned)] })), 'space-table');
    const mixTable = dataTable([{ label: t('portal.insights.col.scan') }, ...STACK_ORDER.slice().reverse().map((key) => ({ label: label(key), num: true }))],
      scans.map((sc) => ({ id: sc.id, cells: [`#${sc.id}`, ...STACK_ORDER.slice().reverse().map((key) => fmtBytes(sc[key]))] })), 'mix-table');
    const noteEl = backfilled ? h('p', { class: 'chart-note', 'data-testid': 'backfilled-note', text: t('portal.insights.mix.backfilled') }) : null;
    const mix = h('div', {}, stack, legend(['junk', 'review', 'report_only', 'protected'].map((key) => ({ key, label: label(key) }))));
    // two charts would share one switch if they shared a card: each gets its own
    return [
      section('space', t('portal.insights.space.title'), t('portal.insights.space.sub'), line, lineTable,
        events.length ? h('p', { class: 'chart-note', text: t('portal.insights.space.events_note') }) : null),
      section('mix', t('portal.insights.mix.title'), t('portal.insights.mix.sub'), mix, mixTable, noteEl),
    ];
  }

  // growth ----------------------------------------------------------------------------------------------------

  function growth() {
    const { up, down } = data.growth;
    const row = (g, sign) => {
      const value = sign * Math.abs(g.delta);
      const perDay = g.per_day === null || g.per_day === undefined ? '' : ` · ${t('portal.insights.growth.per_day', { size: fmtBytes(Math.abs(g.per_day)) })}`;
      return { key: g.group, label: g.label, sub: `${domainLabel(g.domain)} · ${t(`portal.tier.${reportOnly.keys.has(g.group) ? 'report_only' : g.tier}`)}`, value, valueText: `${fmtBytes(value, true)}${perDay}`,
        cls: sign > 0 ? 's-up' : 's-down', href: browseHref({ group: g.group }),
        tip: { title: g.label, rows: [{ cls: sign > 0 ? 's-up' : 's-down', label: t('portal.insights.growth.change'), value: fmtBytes(value, true) },
          { cls: sign > 0 ? 's-up' : 's-down', label: t('portal.insights.growth.now'), value: fmtBytes(g.now) },
          { cls: sign > 0 ? 's-up' : 's-down', label: t('portal.insights.growth.before'), value: fmtBytes(g.before) }] } };
    };
    const rows = [...up.map((g) => row(g, 1)), ...down.map((g) => row(g, -1))];
    if (!rows.length) {
      return h('section', { class: 'card section', 'data-testid': 'section-growth' },
        h('div', { class: 'section-head' }, h('div', { class: 'section-title' }, h('h2', { text: t('portal.insights.growth.title') }))),
        h('p', { class: 'muted', 'data-testid': 'growth-empty', text: t('portal.insights.growth.empty') }));
    }
    const listed = [];
    if (up.length) listed.push({ heading: t('portal.insights.growth.up'), cls: 's-up' }, ...rows.slice(0, up.length));
    if (down.length) listed.push({ heading: t('portal.insights.growth.down'), cls: 's-down' }, ...rows.slice(up.length));
    const chart = tracked(rowBars({ rows: listed, diverging: true, testid: 'growth-row', ariaLabel: t('portal.insights.growth.title') }));
    const table = dataTable([{ label: t('cli.col.group'), wrap: true }, { label: t('portal.insights.growth.change'), num: true }, { label: t('portal.insights.growth.per_day_col'), num: true },
      { label: t('portal.insights.growth.now'), num: true }, { label: t('portal.insights.growth.before'), num: true }],
    rows.map((r, i) => {
      const g = (i < up.length ? up[i] : down[i - up.length]);
      return { href: r.href, id: g.group, cells: [r.label, fmtBytes(r.value, true), g.per_day === null || g.per_day === undefined ? '—' : fmtBytes(g.per_day, true),
        fmtBytes(g.now), fmtBytes(g.before)] };
    }), 'growth-table');
    return section('growth', t('portal.insights.growth.title'), t('portal.insights.growth.sub'), chart, table);
  }

  // cleanup results -------------------------------------------------------------------------------------------

  function results() {
    const { cleanups } = data.history;
    if (!cleanups.length) {
      return emptyState({ iconName: 'cleanup', title: t('portal.insights.results.empty_title'), body: t('portal.insights.results.empty_body'), testid: 'results-empty',
        action: h('a', { class: 'btn primary', href: '#/overview', text: t('portal.insights.results.empty_action') }) });
    }
    const span = Math.max(cleanups.length > 1 ? cleanupTime(cleanups[cleanups.length - 1]) - cleanupTime(cleanups[0]) : 0, 1);
    const columns = cleanups.map((c) => ({ id: cleanupId(c), label: timeLabel(cleanupTime(c), span), values: { freed: c.freed }, total: c.freed, tip: cleanupTip(c) }));
    const chart = tracked(stackedColumns({ columns, series: [{ key: 'freed', cls: 's-freed', label: t('portal.insights.col.freed') }], capLabels: true, valueText: fmtBytes,
      testid: 'freed-col', ariaLabel: t('portal.insights.results.title') }));
    const dash = '—';
    const table = dataTable([{ label: t('portal.insights.col.date') }, { label: t('portal.insights.col.items'), num: true }, { label: t('portal.insights.col.removed'), num: true },
      { label: t('portal.insights.col.freed'), num: true }, { label: t('portal.insights.col.skipped'), num: true }],
    cleanups.slice().reverse().map((c) => (isNative(c)
      ? { testid: 'cleanup-row', id: cleanupId(c), kind: 'native', cells: [[fmtTime(cleanupTime(c)), h('span', { class: 'cmd-pill', title: c.command }, icon('terminal', 14), h('code', { text: c.command }))],
        dash, dash, fmtBytes(c.freed), dash] }
      : { testid: 'cleanup-row', id: cleanupId(c), kind: 'plan', href: `#/plan/${c.plan_id}`,
        cells: [fmtTime(cleanupTime(c)), String(c.items_done), fmtBytes(c.removed), fmtBytes(c.freed), String(c.items_skipped)] })), 'results-table');
    return section('results', t('portal.insights.results.title'), t('portal.insights.results.sub'), chart, table, null, true);
  }

  // agents ----------------------------------------------------------------------------------------------------

  function agents() {
    const list = data.agents.slice().sort((a, b) => b.bytes - a.bytes);
    if (!list.length) {
      return h('section', { class: 'card section', 'data-testid': 'section-agents' },
        h('div', { class: 'section-head' }, h('div', { class: 'section-title' }, h('h2', { text: t('portal.insights.agents.title') }))),
        h('p', { class: 'muted', text: t('portal.insights.agents.empty') }));
    }
    const arrow = (d) => (d > 0 ? '▲' : d < 0 ? '▼' : '–');
    const rows = list.map((a) => ({ key: a.provider, label: a.label, value: a.bytes, cls: 's-agent', href: browseHref({ domain: 'agent' }),
      valueText: a.delta === null || a.delta === undefined ? fmtBytes(a.bytes) : `${fmtBytes(a.bytes)} · ${arrow(a.delta)} ${fmtBytes(a.delta, true)}`,
      tip: { title: a.label, rows: [{ cls: 's-agent', label: t('portal.insights.col.size'), value: fmtBytes(a.bytes) },
        ...(a.delta === null || a.delta === undefined ? [] : [{ cls: 's-agent', label: t('portal.insights.growth.change'), value: fmtBytes(a.delta, true) }])] } }));
    const chart = tracked(rowBars({ rows, testid: 'agent-row', ariaLabel: t('portal.insights.agents.title') }));
    const table = dataTable([{ label: t('portal.insights.col.agent') }, { label: t('portal.insights.col.size'), num: true }, { label: t('portal.insights.growth.change'), num: true }],
      list.map((a) => ({ href: browseHref({ domain: 'agent' }), id: a.provider,
        cells: [a.label, fmtBytes(a.bytes), a.delta === null || a.delta === undefined ? '—' : fmtBytes(a.delta, true)] })), 'agents-table');
    return section('agents', t('portal.insights.agents.title'), t('portal.insights.agents.sub'), chart, table);
  }

  // todos -----------------------------------------------------------------------------------------------------

  function todos() {
    const head = h('div', { class: 'section-head' }, h('div', { class: 'section-title' }, h('h2', { text: t('portal.insights.todo.title') }),
      h('p', { text: t('portal.insights.todo.sub') })));
    if (!data.todos.length) {
      return h('section', { class: 'card section', 'data-testid': 'section-todos' }, head, h('p', { class: 'muted', 'data-testid': 'todo-empty', text: t('portal.insights.todo.empty') }));
    }
    const sev = (s) => h('span', { class: `sev sev-${s}` }, icon(SEVERITY_ICON[s] || 'info', 14), t(`insights.severity.${s}`));
    return h('section', { class: 'card section', 'data-testid': 'section-todos' }, head,
      h('ul', { class: 'todo-list' }, data.todos.map((todo) => h('li', { class: `todo todo-${todo.severity}`, 'data-testid': 'todo', 'data-id': todo.id, 'data-severity': todo.severity },
        sev(todo.severity),
        h('div', { class: 'todo-main' }, h('h3', { text: todo.title }), h('p', { text: todo.detail }),
          todo.bytes === null || todo.bytes === undefined ? null : h('span', { class: 'todo-size', 'data-bytes': todo.bytes, text: fmtBytes(todo.bytes) })),
        h('div', { class: 'todo-actions' }, todo.runnable
          // a tool that can run its own command is run from the table that lists it, not from a terminal
          ? h('a', { class: 'btn small primary', href: '#/browse?report_only=1', 'data-testid': 'todo-run' }, icon('play', 14), t('portal.native.run'))
          : todo.action
            ? h('a', { class: `btn small${todo.severity === 'act' ? ' primary' : ''}`, href: safeHref(todo.action.href), 'data-testid': 'todo-action' }, todo.action.label, icon('arrow', 14)) : null),
        todo.command ? h('div', { class: 'codeline' }, h('code', { text: todo.command }), todo.runnable ? null : copyButton(todo.command)) : null))));
  }

  // environment -----------------------------------------------------------------------------------------------

  function environment() {
    const e = data.environment;
    const dash = '—';
    const row = (key, ...value) => h('div', { class: 'env-row', 'data-testid': `env-${key}` }, h('dt', { text: t(`portal.insights.env.${key}`) }), h('dd', {}, value));
    const cli = (name) => {
      const v = e.clis[name];
      const failed = !v && e.clis_errors && e.clis_errors[name];
      return h('span', { class: `badge ${v ? 'badge-ok' : ''}`, 'data-cli': name, title: failed ? e.clis_errors[name] : null },
        v ? icon('check', 13) : failed ? icon('alert', 13) : null,
        v ? `${name} ${v}` : `${name} · ${t(failed ? 'portal.insights.env.cli_error' : 'portal.insights.env.cli_missing')}`);
    };
    const sched = e.schedule;
    const day = sched && sched.installed && sched.weekday !== null && sched.weekday !== undefined
      ? new Date(2024, 0, 7 + sched.weekday).toLocaleDateString(state.locale, { weekday: 'long' }) : null;
    const scan = e.last_scan;
    const vol = e.volume;
    const areas = (e.areas || []).map(domainLabel).join(', ');
    return h('section', { class: 'env', 'data-testid': 'section-env' },
      h('div', { class: 'section-head' }, h('div', { class: 'section-title' }, h('h2', { text: t('portal.insights.env.title') }))),
      h('dl', { class: 'env-grid' },
        row('macos', e.macos || dash),
        row('chip', e.arch || dash),
        row('python', e.python || dash),
        row('version', e.tool_version || dash),
        row('disk', vol && vol.total !== null && vol.total !== undefined
          ? t('portal.insights.env.disk_value', { total: fmtBytes(vol.total), used: fmtBytes(vol.used), free: fmtBytes(vol.free) }) : dash),
        row('snapshots', e.snapshots === null || e.snapshots === undefined ? [dash, e.snapshots_error ? h('small', { text: e.snapshots_error }) : null] : String(e.snapshots)),
        row('clis', h('span', { class: 'pills' }, ['codex', 'claude', 'ollama'].map(cli))),
        row('advisor', h('span', { class: `badge ${e.advisor_enabled ? 'badge-ok' : ''}`, text: t(e.advisor_enabled ? 'portal.insights.env.on' : 'portal.insights.env.off') })),
        row('schedule', sched && sched.installed
          ? (day ? t('portal.insights.env.schedule_on', { day, hour: String(sched.hour).padStart(2, '0') }) : t('portal.insights.env.on'))
          : t('portal.insights.env.schedule_off')),
        row('areas', areas || dash),
        row('last_scan', scan
          ? [`#${scan.id} · ${fmtTime(scan.finished_at)}`,
            h('small', { text: [t('portal.insights.env.took', { duration: fmtDuration(scan.duration_s) }), pluralCount('portal.insights.env.items', scan.items),
              pluralCount('portal.insights.env.privacy', scan.privacy_skipped), pluralCount('portal.insights.env.errors', scan.errors)].join(' · ') })]
          : dash),
        row('data_dir', h('code', { text: e.data_dir }), h('small', { text: fmtBytes(e.data_dir_bytes) }))));
  }

  // page ------------------------------------------------------------------------------------------------------

  function render() {
    destroyAll(charts);
    const hasScans = data.environment.last_scan !== null && data.environment.last_scan !== undefined;
    const empty = emptyState({ iconName: 'insights', title: t('portal.insights.empty_title'), body: t('portal.insights.empty_body'), testid: 'insights-empty',
      action: h('button', { class: 'btn primary', 'data-testid': 'insights-scan', text: t('portal.shell.scan') }) });
    empty.querySelector('button').addEventListener('click', () => api.post('/api/scan', {}).then((r) => setJobFromResponse(r.job)).catch(reportError));
    const tabs = [
      ...(hasScans ? [['composition', 'portal.tab.ins_composition'], ['trends', 'portal.tab.ins_trends'], ['results', 'portal.tab.ins_results'], ['agents', 'portal.tab.ins_agents']] : []),
      ['todos', 'portal.insights.todo.title'], ['env', 'portal.insights.env.title'],
    ].map(([key, label]) => ({ key, label: t(label), count: key === 'todos' ? data.todos.length : 0 }));   // the count: to-dos are not missed behind a tab
    tab = pickTab(tabs, tab);
    const part = { composition, trends: () => [...space(), growth()], results, agents, todos, env: environment }[tab];
    replace(root, header(), h('div', { class: 'ins-body' },
      hasScans ? kpis() : empty, subTabs(tabs, tab, (key) => { tab = key; render(); }), tabPanel(tab, part())));
  }

  return {
    async mount(el, route) {
      root = el;
      const asked = route.query.window === undefined ? null : Number(route.query.window);
      win = asked !== null && WINDOWS.includes(asked) ? asked : savedWindow();
      tab = route.query.tab || null;
      replace(root, loading());
      disposers.push(on('events', (events) => {
        if (events.some((e) => e.type === 'scan.done' || e.type === 'run.finished')) load().catch(reportError);
      }));
      await load();
    },
    unmount() {
      disposers.forEach((f) => f());
      destroyAll(charts);
    },
  };
}
