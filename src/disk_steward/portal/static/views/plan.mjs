// The plan page: the one screen where a cleanup list is reviewed, started and (afterwards) read as a result.
// The numbers in the header and on the button are always those of the entries that would run right now.
import { api, ApiError } from '../api.mjs';
import { delegate, h, keepFocus, replace } from '../dom.mjs';
import { fmtAge, fmtBytes, fmtCount, fmtTime, pluralCount } from '../format.mjs';
import { t } from '../i18n.mjs';
import { on, setJobFromResponse } from '../state.mjs';
import { icon } from '../icons.mjs';
import { colgroup, dotLabel, midPath, nextSort, plainTh, sortRows, sortTh } from '../tables.mjs';
import { actionLine, badge, loading, modal, reportError, toast, warningList } from '../ui.mjs';

const PAGE = 50;
const RUNNABLE = ['draft', 'paused'];
const FINISHED = ['done', 'cancelled', 'failed'];
const ENTRY_DOT = { pending: 'neutral', in_progress: 'report_only', done: 'free', skipped: 'review', failed: 'junk', excluded: 'neutral',
  cancelled: 'outside', needs_attention: 'junk' };

export function create() {
  let root;
  let id;
  let plan = null;
  let groups = [];
  let pre = null;            // preflight; null until it has answered
  let preError = null;
  let flat = { rows: [], total: 0 };   // the entries of a plan that is no longer a draft, by state
  let stateFilter = '';
  let entrySort = { col: null, dir: 'asc' };   // the order of the plan until a header is clicked
  const open = new Map();    // group key -> {rows, total}
  const disposers = [];

  const draftLike = () => RUNNABLE.includes(plan.state);

  // data ----------------------------------------------------------------------------------------------------

  async function loadPlan() {
    plan = await api.get(`/api/plans/${id}`);
    groups = plan.entries.total ? (await api.get(`/api/plans/${id}/groups`)).groups : [];
    for (const key of [...open.keys()]) {
      if (!groups.some((g) => g.group === key)) open.delete(key);
      else await loadGroup(key, true);
    }
    if (!draftLike() && plan.entries.total) await loadFlat();
  }

  async function loadGroup(key, keep = false, more = false) {
    const cur = open.get(key) || { rows: [], total: 0 };
    const offset = more ? cur.rows.length : 0;
    const limit = more ? PAGE : Math.max(PAGE, keep ? cur.rows.length : PAGE);
    const page = await api.get(`/api/plans/${id}/entries`, { group: key, offset, limit });
    cur.rows = more ? cur.rows.concat(page.rows) : page.rows;
    cur.total = page.total;
    open.set(key, cur);
  }

  async function loadFlat(more = false) {
    const offset = more ? flat.rows.length : 0;
    const page = await api.get(`/api/plans/${id}/entries`, { state: stateFilter, offset, limit: more ? PAGE : Math.max(PAGE, flat.rows.length) });
    flat = { rows: more ? flat.rows.concat(page.rows) : page.rows, total: page.total };
  }

  async function loadPre() {
    pre = null;
    preError = null;
    if (!draftLike()) { render(); return; }
    render();
    try {
      pre = await api.get(`/api/preflight/${id}`);
    } catch (e) {
      if (!(e instanceof ApiError)) throw e;
      preError = e.message;
    }
    render();
  }

  async function reload() {
    await loadPlan();
    render();
    await loadPre();
  }

  // rendering -----------------------------------------------------------------------------------------------

  function counts() {
    return draftLike() ? plan.pending : plan.totals;
  }

  function whyNotRunnable() {
    const out = [];
    if (!draftLike()) return out;
    if (!pre) return out;
    if (pre.expired) out.push(t('cli.apply.expired'));
    if (!pre.hashes_ok) {
      const what = Object.entries(pre.hashes).filter(([, ok]) => !ok).map(([n]) => n).join(', ');
      out.push(t('cli.apply.hashes_changed', { what }));
    }
    if (pre.pending === 0) out.push(t('portal.plan.nothing_pending'));
    return out;
  }

  function preflightBox() {
    if (preError) return h('div', { class: 'banner banner-warn', text: t('portal.plan.preflight_failed', { reason: preError }) });
    if (!pre) return h('p', { class: 'muted', 'data-testid': 'preflight-loading', text: t('portal.plan.preflight_loading') });
    const lines = pre.blocker_groups;
    return h('div', { 'data-testid': 'preflight' },
      lines.length
        ? h('div', { class: 'blockers', 'data-testid': 'blockers' }, h('h3', { text: t('portal.plan.blockers') }),
          h('ul', { class: 'plain' }, lines.map((b) => h('li', { class: `blocker blocker-${b.kind}`, 'data-kind': b.kind, text: b.text }))),
          h('button', { class: 'btn small', data: { action: 'recheck' }, 'data-fid': 'recheck', 'data-testid': 'recheck', text: t('portal.plan.recheck') }))
        : h('p', { class: 'muted', text: t('portal.plan.no_blockers') }),
      warningList(pre.warnings));
  }

  // entries -------------------------------------------------------------------------------------------------

  function entryRow(e) {
    const editable = e.state === 'pending' || (e.state === 'excluded' && plan.state === 'draft');
    const tickable = e.state === 'pending' || e.state === 'excluded';
    const status = () => [dotLabel(ENTRY_DOT[e.state] || 'neutral', t(`entry.${e.state}`)), h('span', { class: 'sub', text: e.reason_text })];
    return h('tr', { class: `item entry entry-${e.state}`, 'data-testid': 'entry', 'data-path': e.path, 'data-seq': e.seq, 'data-state': e.state },
      h('td', { class: 'c-check' }, tickable
        ? h('input', { type: 'checkbox', checked: e.state === 'pending', disabled: !editable, 'aria-label': t('portal.plan.include_entry', { path: e.path }),
          data: { change: 'entry-toggle', seq: e.seq, group: e.group_key }, 'data-fid': `ex:${e.seq}` })
        : h('span', { class: 'lock', 'aria-hidden': 'true' })),
      h('td', { class: 'c-path' }, midPath(e.path),
        h('span', { class: 'sub meta-sm' }, [t(`action.${e.action}`), fmtBytes(e.alloc), fmtAge(e.age_days, e.age_basis)].join(' · ')),
        h('span', { class: 'sub meta-sm status-sm' }, status())),
      h('td', { class: 'c-act hide-sm', text: t(`action.${e.action}`) }),
      h('td', { class: 'c-size num hide-sm', text: fmtBytes(e.alloc) }),
      h('td', { class: 'c-changed hide-md', text: fmtAge(e.age_days, e.age_basis) }),
      h('td', { class: 'c-status hide-sm' }, status()));
  }

  const ENTRY_GETTERS = { path: (e) => e.path.toLowerCase(), action: (e) => e.action, size: (e) => e.alloc, changed: (e) => e.age_days, status: (e) => e.state };

  // path, action, size, change, status: one table for the entries of a group and for a finished plan alike
  function entriesTable(rows) {
    const th = (label, col, cls) => sortTh(label, col, entrySort, 'entries', cls);
    return h('table', { class: 'data nested', 'data-testid': 'entries' },
      colgroup('c-check', 'c-path', 'c-act hide-sm', 'c-size hide-sm', 'c-changed hide-md', 'c-status hide-sm'),
      h('thead', {}, h('tr', {}, plainTh(t('portal.col.select'), 'c-check sr-only-th'), th(t('portal.col.path'), 'path', 'c-path'),
        th(t('portal.col.action'), 'action', 'c-act hide-sm'), th(t('portal.col.size'), 'size', 'c-size num hide-sm'),
        th(t('portal.col.changed'), 'changed', 'c-changed hide-md'), th(t('portal.col.status'), 'status', 'c-status hide-sm'))),
      h('tbody', {}, sortRows(rows, entrySort, ENTRY_GETTERS).map(entryRow)));
  }

  function groupBody(g) {
    const isOpen = open.has(g.group);
    const included = g.pending.count;
    const left = g.by_state.excluded || 0;
    const all = included > 0 && left === 0;
    const partial = included > 0 && left > 0;
    const canInclude = plan.state === 'draft';
    const toggleable = included > 0 || (left > 0 && canInclude);
    const stats = all ? pluralCount('portal.plan.group_all', included, { size: fmtBytes(g.pending.alloc) })
      : partial ? t('portal.plan.group_partial', { count: fmtCount(included), total: fmtCount(g.entries), size: fmtBytes(g.pending.alloc) })
        : t('portal.plan.group_none', { total: fmtCount(g.entries) });
    const o = open.get(g.group);
    return h('tbody', { class: `group-body plan-group${isOpen ? ' open' : ''}`, 'data-testid': 'plan-group', 'data-key': g.group },
      h('tr', { class: 'group-row group-head' },
        h('td', { class: 'c-check' }, h('input', { type: 'checkbox', checked: all, indeterminate: partial, disabled: !toggleable,
          'aria-label': t('portal.plan.group_toggle', { label: g.label }), data: { change: 'group-toggle', key: g.group, all: all ? '1' : '0' },
          'data-fid': `pg:${g.group}` })),
        h('td', { class: 'c-name' }, h('button', { class: 'group-title', 'aria-expanded': String(isOpen), data: { action: 'group-open', key: g.group },
          'data-fid': `po:${g.group}`, text: g.label }),
        h('span', { class: 'sub meta-sm' }, stats)),
        h('td', { class: 'c-count hide-sm', 'data-testid': 'plan-group-stats', text: stats }),
        h('td', { class: 'c-size num hide-sm', text: fmtBytes(g.alloc) }),
        h('td', { class: 'c-chev' }, h('button', { class: 'icon-btn small', 'aria-expanded': String(isOpen), 'aria-label': t('portal.col.expand'),
          data: { action: 'group-open', key: g.group }, 'data-fid': `pc:${g.group}` }, icon('chevron', 16)))),
      isOpen ? h('tr', { class: 'detail-row' }, h('td', { colspan: 5 }, h('div', { class: 'items-panel' },
        h('p', { class: 'effect-full', text: g.effect }),
        entriesTable(o.rows),
        h('div', { class: 'pager' },
          h('span', { class: 'muted', text: t('portal.browse.showing', { shown: fmtCount(o.rows.length), total: fmtCount(o.total) }) }),
          o.rows.length < o.total ? h('button', { class: 'btn small', data: { action: 'group-more', key: g.group }, 'data-fid': `pm:${g.group}`,
            text: t('portal.browse.more') }) : null)))) : null);
  }

  function groupsTable() {
    return h('div', { class: 'table-card' }, h('table', { class: 'data sticky groups-table plan-table' },
      colgroup('c-check', 'c-name', 'c-count hide-sm', 'c-size hide-sm', 'c-chev'),
      h('thead', {}, h('tr', {}, plainTh(t('portal.col.select'), 'c-check sr-only-th'), plainTh(t('portal.col.name'), 'c-name'),
        plainTh(t('portal.col.included'), 'c-count hide-sm'), plainTh(t('portal.col.size'), 'c-size num hide-sm'),
        plainTh(t('portal.col.expand'), 'c-chev sr-only-th'))),
      groups.map(groupBody)));
  }

  // the result of a run, in place -----------------------------------------------------------------------------

  function resultBox() {
    const r = plan.result;
    const by = plan.entries.by_state;
    const done = r ? (r.counts.done || 0) : (by.done || 0);
    const skipped = r ? (r.counts.skipped || 0) : (by.skipped || 0);
    const kind = plan.state === 'cancelled' ? 'cancelled' : plan.state === 'failed' ? 'failed' : 'done';
    const other = (name) => (r ? (r.counts[name] || 0) : (by[name] || 0));
    return h('section', { class: 'card result', 'data-testid': 'result' },
      h('h2', { text: t(`portal.result.title_${kind}`) }),
      h('p', { class: 'big', 'data-testid': 'result-cleaned', text: r
        ? pluralCount('portal.result.cleaned', done, { size: fmtBytes(r.removed_alloc), freed: fmtBytes(r.freed) })
        : pluralCount('portal.result.cleaned_plain', done) }),
      skipped ? h('details', { class: 'fold', 'data-testid': 'result-skipped' },
        h('summary', { text: pluralCount('portal.result.skipped', skipped) }),
        r ? h('ul', { class: 'plain' }, r.skipped.map((x) => h('li', { text: `${x.text}: ${fmtCount(x.count)}` }))) : null) : null,
      other('excluded') ? h('p', { class: 'muted', text: pluralCount('portal.result.left_out', other('excluded')) }) : null,
      other('failed') ? h('p', { class: 'banner banner-danger', text: pluralCount('portal.result.failed', other('failed')) }) : null,
      other('cancelled') ? h('p', { class: 'muted', text: pluralCount('portal.result.cancelled', other('cancelled')) }) : null,
      other('needs_attention') ? h('p', { class: 'banner banner-warn', text: pluralCount('portal.run.attention', other('needs_attention')) }) : null,
      r && r.moved_to_trash ? h('p', { text: t('portal.result.trashed', { size: fmtBytes(r.moved_to_trash) }) }) : null,
      r ? h('p', { 'data-testid': 'result-freed', text: t('portal.result.freed', { measured: fmtBytes(r.measured_delta, true), estimated: fmtBytes(r.estimated_delta, true) }) }) : null,
      r ? warningList(r.warnings) : null,
      r && r.rescan && r.rescan.error ? h('div', { class: 'banner banner-warn', text: t('portal.run.rescan_failed', { error: r.rescan.error }) }) : null,
      h('div', { class: 'summary-actions' },
        h('a', { class: 'btn primary', href: '#/overview', 'data-testid': 'result-back', text: t('portal.result.back') }),
        skipped ? h('button', { class: 'btn', data: { action: 'view-skipped' }, 'data-testid': 'view-skipped', text: t('portal.result.view_skipped') }) : null));
  }

  // Finished items lose their per-item rows after a week; the totals and the audit log keep the record.
  const compactedNote = () => h('p', { class: 'note', 'data-testid': 'compacted-note' }, icon('info', 16), h('span', { text: t('plan.compacted_note') }));

  function flatBox() {
    const states = Object.entries(plan.entries.by_state);
    return h('section', { class: 'card', 'data-testid': 'flat' },
      h('div', { class: 'toolbar' }, h('h2', { text: t('portal.plan.entries', { total: fmtCount(plan.entries.total) }) }),
        h('label', { class: 'inline' }, h('span', { class: 'muted', text: t('portal.plan.show') }),
          h('select', { data: { change: 'filter' }, 'data-fid': 'filter', 'data-testid': 'state-filter' },
            [['', t('portal.browse.all')], ...states.map(([s, n]) => [s, `${t(`entry.${s}`)} (${fmtCount(n)})`])]
              .map(([v, label]) => h('option', { value: v, selected: v === stateFilter, text: label }))))),
      plan.compacted_at ? compactedNote() : null,
      plan.compacted_at && !flat.rows.length ? null : [
        h('div', { class: 'table-card' }, entriesTable(flat.rows)),
        h('div', { class: 'pager' },
          h('span', { class: 'muted', text: t('portal.browse.showing', { shown: fmtCount(flat.rows.length), total: fmtCount(flat.total) }) }),
          flat.rows.length < flat.total ? h('button', { class: 'btn small', data: { action: 'flat-more' }, 'data-fid': 'flat-more', text: t('portal.browse.more') }) : null)]);
  }

  function render() {
    if (!plan) return;
    const reasons = whyNotRunnable();
    const ready = draftLike() && pre !== null && pre.ready && reasons.length === 0;
    const c = counts();
    const finished = FINISHED.includes(plan.state);
    replace(root,
      h('div', { class: 'page-head' },
        h('h1', { 'data-testid': 'plan-head', text: pluralCount('portal.plan.head', c.count, { size: fmtBytes(c.alloc) }) }),
        badge(t(`plan.${plan.state}`), plan.state === 'draft' ? 'info' : 'neutral')),
      h('p', { class: 'muted', text: t('portal.plan.times', { created: fmtTime(plan.created_at), expires: fmtTime(plan.expires_at), scan: plan.scan_id, revision: plan.revision }) }),
      plan.state === 'invalid' ? h('div', { class: 'banner banner-warn', 'data-testid': 'plan-invalid', text: t(`portal.plan.invalid_${plan.error || 'superseded'}`) }) : null,
      plan.error && plan.state !== 'invalid' ? h('div', { class: 'banner banner-warn', text: t('portal.plan.error', { error: plan.error }) }) : null,
      finished ? resultBox() : null,
      plan.state === 'running' ? h('div', { class: 'banner banner-info' }, t('portal.plan.running'), ' ', h('a', { href: '#/run', text: t('portal.plan.open_run') })) : null,
      draftLike() ? h('section', { class: 'card' },
        h('div', { 'data-testid': 'plan-totals' }, Object.entries(plan.pending.by_action).map(([action, v]) =>
          h('p', { class: 'action-line', text: actionLine(action, v) }))),
        warningList(plan.totals.warnings), preflightBox()) : null,
      draftLike() && groups.length ? h('section', { 'data-testid': 'plan-groups' }, groupsTable()) : null,
      draftLike() ? h('div', { class: 'action-bar run-card', 'data-testid': 'action-bar' },
        h('span', { class: 'bar-figure muted', text: t('portal.plan.bar_hint') }),
        plan.state === 'draft' ? h('button', { class: 'btn', data: { action: 'discard' }, 'data-testid': 'discard', text: t('portal.cleanup.discard') }) : null,
        plan.state === 'paused' ? h('a', { class: 'btn', href: '#/run', text: t('portal.plan.open_run') }) : null,
        h('button', { class: 'btn primary solid big', disabled: !ready, data: { action: 'run' }, 'data-fid': 'run', 'data-testid': 'confirm-run',
          'aria-describedby': reasons.length ? 'run-why' : null,
          text: pluralCount('portal.plan.confirm_run', plan.pending.count, { size: fmtBytes(plan.pending.alloc) }) }),
        h('ul', { id: 'run-why', class: 'plain muted' }, reasons.map((x) => h('li', { text: x })))) : null,
      !draftLike() && plan.entries.total ? flatBox() : null);
  }

  // actions -------------------------------------------------------------------------------------------------

  async function change(path, body) {
    try {
      await api.post(`/api/plans/${id}/${path}`, { ...body, revision: plan.revision });
    } catch (e) {
      if (!(e instanceof ApiError) || !['REVISION_MISMATCH', 'IN_PROGRESS'].includes(e.code)) throw e;
      toast(e.message, 'error');   // the plan moved on under the user's eyes: show the fresh state and let them choose again
    }
    await reload();
  }

  // What the user is about to do, in the words of what can come back: deleted for good, Trash, Archives.
  async function confirmRun() {
    const kinds = Object.entries(plan.pending.by_action).filter(([, v]) => v.count > 0);
    const permanent = kinds.some(([action]) => action === 'delete');
    const answer = await modal({
      title: pluralCount(kinds.length === 1 && permanent ? 'portal.confirm.title_delete' : 'portal.confirm.title_clean', plan.pending.count,
        { size: fmtBytes(plan.pending.alloc) }),
      body: [h('ul', { class: 'confirm-lines', 'data-testid': 'confirm-lines' }, kinds.map(([action, v]) =>
        h('li', { class: action === 'delete' ? 'perm' : 'safe', 'data-action': action },
          icon(action === 'delete' ? 'alert' : 'check', 18), h('span', { text: pluralCount(`portal.confirm.${action}`, v.count, { size: fmtBytes(v.alloc) }) })))),
      h('p', { class: 'muted', text: t('portal.confirm.note') })],
      buttons: [{ label: t('portal.common.cancel'), value: 'no' },
        { label: t(permanent ? 'portal.confirm.go_delete' : 'portal.confirm.go'), value: 'yes', primary: !permanent, solid: !permanent, fill: permanent, testid: 'confirm-run-yes' }] });
    return answer === 'yes';
  }

  async function run() {
    if (!(await confirmRun())) return;
    try {
      const r = await api.post(`/api/plans/${id}/run`, { revision: plan.revision, entries_digest: plan.entries_digest });
      setJobFromResponse(r.job);
      location.hash = '#/run';
    } catch (e) {
      if (!(e instanceof ApiError)) throw e;
      if (['REVISION_MISMATCH', 'PLAN_EXPIRED', 'RULES_CHANGED'].includes(e.code)) {
        await modal({ title: t('portal.plan.cannot_run'), body: h('p', { text: e.message }),
          buttons: [{ label: t('portal.common.close'), value: 'ok', primary: true }] });
        await reload();
        return;
      }
      throw e;
    }
  }

  const handlers = {
    click: {
      run,
      sort: (el) => {
        entrySort = nextSort(entrySort, el.dataset.col, ['size', 'changed'].includes(el.dataset.col) ? 'desc' : 'asc');
        keepFocus(root, render);
      },
      recheck: loadPre,
      discard: async () => {
        await api.post(`/api/plans/${id}/discard`, {});
        location.hash = '#/overview';
      },
      'group-open': async (el) => {
        const key = el.dataset.key;
        if (open.has(key)) open.delete(key);
        else await loadGroup(key);
        keepFocus(root, render);
      },
      'group-more': async (el) => {
        await loadGroup(el.dataset.key, true, true);
        keepFocus(root, render);
      },
      'flat-more': async () => {
        await loadFlat(true);
        keepFocus(root, render);
      },
      'view-skipped': async () => {
        stateFilter = 'skipped';
        await loadFlat();
        render();
        root.querySelector('[data-testid="flat"]').scrollIntoView({ block: 'start' });
      },
    },
    change: {
      'group-toggle': (el) => {
        // everything included -> leave the group out; otherwise put it back (a plan that has started cannot take entries back)
        const putBack = el.dataset.all !== '1' && plan.state === 'draft';
        return change(putBack ? 'include' : 'exclude', { group: el.dataset.key });
      },
      'entry-toggle': (el) => change(el.checked ? 'include' : 'exclude', { seqs: [Number(el.dataset.seq)] }),
      filter: async (el) => {
        stateFilter = el.value;
        flat = { rows: [], total: 0 };
        await loadFlat();
        keepFocus(root, render);
      },
    },
  };

  return {
    async mount(el, route) {
      root = el;
      id = route.params[0];
      replace(root, loading());
      delegate(root, 'click', 'action', handlers.click);
      delegate(root, 'change', 'change', handlers.change);
      disposers.push(on('job', ({ job, before }) => {
        if (job.plan_id === id && before && before.status !== job.status) reload().catch(reportError);
      }));
      await loadPlan();
      render();
      await loadPre();
    },
    unmount() { disposers.forEach((f) => f()); },
  };
}
