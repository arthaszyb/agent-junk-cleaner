// Cleanup tab: the run that is going on (progress, pause / resume / cancel, excluding what is still pending, the live
// log) or, when nothing runs, the draft that is waiting and the last cleanups with their results.
import { api, ApiError } from '../api.mjs';
import { delegate, h, keepFocus, replace } from '../dom.mjs';
import { fmtBytes, fmtCount, fmtTime, pluralCount } from '../format.mjs';
import { t } from '../i18n.mjs';
import { on, refreshJob, setJobFromResponse, state } from '../state.mjs';
import { create as archives } from './archives.mjs';
import { colgroup, dotLabel, midPath, plainTh } from '../tables.mjs';
import { badge, confirmDialog, emptyState, loading, pickTab, reportError, subTabs, tabPanel, toast } from '../ui.mjs';

const STATE_DOT = { done: 'free', skipped: 'review', failed: 'junk', needs_attention: 'junk', cancelled: 'outside', excluded: 'neutral', pending: 'neutral' };
const JOB_KIND = { running: 'info', paused: 'warn', done: 'ok', error: 'danger', idle: 'neutral' };
const SHOWN_LOG = 200;
const PAGE = 100;
const EXCLUDE_ATTEMPTS = 3;

export function create() {
  let root;
  let pending = null;       // {rows, total} of the pending entries, while a run is active
  let pendingPlan = null;
  let plans = null;         // {active, draft, finished} for the idle page
  const checked = new Set();
  const disposers = [];
  let logEl = null;
  let panelEl = null;
  let mainEl = null;
  const archivesPart = archives();
  let archivesEl = null;
  let tabsEl = null;
  let tab = null;   // 'current' or 'archives'; both panels stay mounted, one is hidden

  const job = () => state.job;
  const active = (j) => j && j.kind === 'run' && (j.status === 'running' || j.status === 'paused');

  async function loadPending() {
    const j = job();
    if (!active(j)) { pending = null; return; }
    pendingPlan = j.plan_id;
    const page = await api.get(`/api/plans/${j.plan_id}/entries`, { state: 'pending', limit: PAGE });
    pending = { rows: page.rows, total: page.total };
    for (const seq of [...checked]) if (!page.rows.some((r) => r.seq === seq)) checked.delete(seq);
  }

  async function loadPlans() {
    plans = await api.get('/api/plans');
  }

  // rendering -----------------------------------------------------------------------------------------------

  function logRow(ev) {
    const d = ev.data;
    return h('li', { class: `log-row log-${d.state}`, 'data-testid': 'log-row', 'data-state': d.state, 'data-path': d.path },
      dotLabel(STATE_DOT[d.state] || 'neutral', t(`entry.${d.state}`)), ' ',
      h('span', { class: 'path', text: d.path }),
      d.reason_text ? h('span', { class: 'muted', text: ` — ${d.reason_text}` }) : null,
      d.freed > 0 ? h('span', { class: 'muted', text: ` (${t('portal.run.freed', { size: fmtBytes(d.freed) })})` }) : null);
  }

  function renderLog() {
    if (!logEl) return;
    const events = state.log.slice(-SHOWN_LOG).reverse();
    replace(logEl, events.length ? events.map(logRow) : h('li', { class: 'muted', text: t('portal.run.log_empty') }));
  }

  function controls(j) {
    const run = j.status === 'running';
    return h('div', { class: 'run-controls' },
      run ? h('button', { class: 'btn', disabled: j.pause_requested, data: { action: 'pause' }, 'data-fid': 'pause', 'data-testid': 'pause',
        text: t(j.pause_requested ? 'portal.run.pausing' : 'portal.run.pause') }) : null,
      j.status === 'paused' ? h('button', { class: 'btn primary', data: { action: 'resume' }, 'data-fid': 'resume', 'data-testid': 'resume', text: t('portal.run.resume') }) : null,
      h('button', { class: 'btn danger', data: { action: 'cancel' }, 'data-fid': 'cancel', 'data-testid': 'cancel', text: t('portal.run.cancel') }));
  }

  function pendingPanel(j) {
    if (!active(j) || j.phase !== 'run' || !pending) return null;
    return h('section', { class: 'card', 'data-testid': 'pending-panel' },
      h('h2', { text: t('portal.run.remaining', { total: pending.total }) }),
      h('p', { class: 'muted', text: t('portal.run.remaining_hint') }),
      h('div', { class: 'table-card' }, h('table', { class: 'data nested' },
        colgroup('c-check', 'c-path', 'c-act hide-sm', 'c-size hide-sm'),
        h('thead', {}, h('tr', {}, plainTh(t('portal.col.select'), 'c-check sr-only-th'), plainTh(t('portal.col.path'), 'c-path'),
          plainTh(t('portal.col.action'), 'c-act hide-sm'), plainTh(t('portal.col.size'), 'c-size num hide-sm'))),
        h('tbody', {}, pending.rows.map((e) => h('tr', { class: 'item entry', 'data-testid': 'pending-entry', 'data-path': e.path },
          h('td', { class: 'c-check' }, h('input', { type: 'checkbox', checked: checked.has(e.seq), 'aria-label': t('portal.plan.exclude_entry', { path: e.path }),
            data: { change: 'tick', seq: e.seq }, 'data-fid': `pe:${e.seq}` })),
          h('td', { class: 'c-path' }, midPath(e.path), h('span', { class: 'sub meta-sm', text: `${t(`action.${e.action}`)} · ${fmtBytes(e.alloc)}` })),
          h('td', { class: 'c-act hide-sm', text: t(`action.${e.action}`) }),
          h('td', { class: 'c-size num hide-sm', text: fmtBytes(e.alloc) })))))),
      h('div', { class: 'pager' },
        h('button', { class: 'btn', data: { action: 'refresh-pending' }, 'data-fid': 'refresh-pending', text: t('portal.run.refresh') }),
        h('button', { class: 'btn', disabled: checked.size === 0, data: { action: 'exclude-checked' }, 'data-fid': 'exclude-checked',
          'data-testid': 'exclude-checked', text: t('portal.plan.exclude_checked', { count: checked.size }) }),
        h('button', { class: 'btn', disabled: pending.total === 0, data: { action: 'exclude-all' }, 'data-fid': 'exclude-all',
          'data-testid': 'exclude-all', text: t('portal.run.exclude_all', { count: pending.total }) })));
  }

  // nothing is running: what is waiting, and what was done lately -------------------------------------------

  function finishedRow(p) {
    const r = p.result;
    const line = r ? pluralCount('portal.cleanup.result_line', r.counts.done || 0, { freed: fmtBytes(r.freed) })
      : pluralCount('portal.cleanup.result_plain', p.entries.by_state.done || 0);
    return h('tr', { 'data-testid': 'finished-plan', 'data-plan': p.id },
      h('td', { class: 'c-name' }, h('a', { href: `#/plan/${p.id}`, text: t('portal.cleanup.finished_at', { when: fmtTime(p.created_at) }) }),
        h('span', { class: 'sub meta-sm', text: line })),
      h('td', { class: 'c-count hide-sm', text: line }),
      h('td', { class: 'c-status' }, dotLabel(STATE_DOT[p.state] || 'neutral', t(`plan.${p.state}`))));
  }

  function idleBox() {
    if (!plans) return loading();
    const out = [];
    const live = plans.active;
    if (live) {
      out.push(h('section', { class: 'card', 'data-testid': 'cleanup-paused' },
        h('h2', { text: t('portal.cleanup.paused_title') }),
        h('p', { text: pluralCount('portal.cleanup.paused_body', live.pending.count, { size: fmtBytes(live.pending.alloc) }) }),
        h('a', { class: 'btn primary', href: `#/plan/${live.id}`, text: t('portal.cleanup.continue') })));
    }
    const draft = plans.draft;
    if (draft && !live) {
      out.push(h('section', { class: 'card', 'data-testid': 'cleanup-draft' },
        h('h2', { text: t('portal.cleanup.draft_title') }),
        h('p', { class: 'big', text: pluralCount('portal.plan.head', draft.pending.count, { size: fmtBytes(draft.pending.alloc) }) }),
        h('p', { class: 'muted', text: t('portal.cleanup.draft_made', { when: fmtTime(draft.created_at) }) }),
        h('div', { class: 'summary-actions' },
          h('a', { class: 'btn primary', href: `#/plan/${draft.id}`, 'data-testid': 'draft-continue', text: t('portal.cleanup.continue') }),
          h('button', { class: 'btn', data: { action: 'discard', plan: draft.id }, 'data-testid': 'draft-discard', text: t('portal.cleanup.discard') }))));
    }
    if (plans.finished.length) {
      out.push(h('section', { class: 'card', 'data-testid': 'cleanup-finished' },
        h('h2', { text: t('portal.cleanup.recent') }),
        h('div', { class: 'table-card plain' }, h('table', { class: 'data' },
          colgroup('c-name', 'c-count hide-sm', 'c-status'),
          h('thead', {}, h('tr', {}, plainTh(t('portal.col.name'), 'c-name'), plainTh(t('portal.col.result'), 'c-count hide-sm'), plainTh(t('portal.col.status'), 'c-status'))),
          h('tbody', {}, plans.finished.map(finishedRow))))));
    }
    if (!out.length) {
      out.push(emptyState({ iconName: 'cleanup', title: t('portal.nav.run'), body: t('portal.cleanup.none'), testid: 'cleanup-empty',
        action: h('a', { class: 'btn primary', href: '#/overview', text: t('portal.nav.overview') }) }));
    }
    return out;
  }

  function renderMain() {
    const j = job();
    if (!j || j.kind !== 'run' || !(active(j) || j.status === 'error')) {   // a finished run is read on its plan's page
      replace(mainEl, idleBox());
      return;
    }
    const c = j.counts;
    const busy = active(j);
    replace(mainEl,
      h('section', { class: 'card', 'data-testid': 'run-status' },
        h('div', { class: 'toolbar' }, h('h2', {}, h('a', { href: `#/plan/${j.plan_id}`, text: t('portal.plan.title', { id: j.plan_id }) })),
          badge(t(`portal.run.status.${j.status}`), JOB_KIND[j.status])),
        j.phase === 'rescan' ? h('p', { 'data-testid': 'rescanning', text: t('portal.run.rescanning') }) : null,
        h('progress', { value: c.done, max: c.total || 1, 'aria-label': pluralCount('portal.run.progress', c.total, { done: fmtCount(c.done), size: fmtBytes(c.bytes) }) }),
        h('p', { 'data-testid': 'run-progress', text: pluralCount('portal.run.progress', c.total, { done: fmtCount(c.done), size: fmtBytes(c.bytes) }) }),
        busy && j.current ? h('p', { class: 'muted' }, t('portal.run.current'), ' ', h('code', { text: j.current })) : null,
        busy && j.phase === 'run' ? controls(j) : null,
        j.status === 'error' ? h('div', { class: 'banner banner-danger', text: `${j.error_text || ''} ${j.error || ''}`.trim() }) : null),
      pendingPanel(j),
      h('section', { class: 'card' }, h('h2', { text: t('portal.run.log') }), logEl = h('ol', { class: 'log', 'data-testid': 'log' })));
    renderLog();
  }

  // actions -------------------------------------------------------------------------------------------------

  async function control(action) {
    setJobFromResponse((await api.post(`/api/job/${action}`, {})).job);
  }

  async function excludeSeqs(seqs) {
    for (let attempt = 1; ; attempt += 1) {
      const plan = await api.get(`/api/plans/${pendingPlan}`);   // the revision the exclusion must name
      try {
        await api.post(`/api/plans/${pendingPlan}/exclude`, { seqs, revision: plan.revision });
        return;
      } catch (e) {
        if (!(e instanceof ApiError) || !['REVISION_MISMATCH', 'IN_PROGRESS'].includes(e.code) || attempt >= EXCLUDE_ATTEMPTS) throw e;
      }
    }
  }

  async function allPendingSeqs() {
    const seqs = [];
    for (let offset = 0; ; offset += 1000) {
      const page = await api.get(`/api/plans/${pendingPlan}/entries`, { state: 'pending', offset, limit: 1000 });
      seqs.push(...page.rows.map((r) => r.seq));
      if (offset + 1000 >= page.total) return seqs;
    }
  }

  async function afterExclude(count) {
    checked.clear();
    toast(pluralCount('portal.run.excluded', count));
    await loadPending();
    renderMain();
  }

  const handlers = {
    click: {
      pause: () => control('pause'),
      resume: () => control('resume'),
      cancel: async () => {
        if (await confirmDialog({ title: t('portal.run.cancel_title'), body: h('p', { text: t('portal.run.cancel_body') }),
          confirm: t('portal.run.cancel_confirm'), danger: true })) await control('cancel');
      },
      discard: async (el) => {
        await api.post(`/api/plans/${el.dataset.plan}/discard`, {});
        await loadPlans();
        renderMain();
      },
      'refresh-pending': async () => { await loadPending(); keepFocus(root, renderMain); },
      'exclude-checked': async () => {
        const seqs = [...checked];
        await excludeSeqs(seqs);
        await afterExclude(seqs.length);
      },
      'exclude-all': async () => {
        const seqs = await allPendingSeqs();
        await excludeSeqs(seqs);
        await afterExclude(seqs.length);
      },
    },
    change: {
      tick: (el) => {
        const seq = Number(el.dataset.seq);
        if (el.checked) checked.add(seq);
        else checked.delete(seq);
        keepFocus(root, renderMain);
      },
    },
  };

  function renderTabs() {
    const tabs = [['current', 'run_current'], ['archives', 'run_archives']].map(([key, label]) => ({ key, label: t(`portal.tab.${label}`) }));
    replace(tabsEl, subTabs(tabs, tab, (key) => { tab = key; renderTabs(); }));
    for (const x of tabs) document.getElementById(`tabpanel-${x.key}`).hidden = x.key !== tab;
  }

  async function seedLog() {
    if (state.log.length) return;
    // A page opened mid-run has missed the earlier entries; the server keeps the latest events.
    const r = await api.get('/api/events', { since: 0, timeout: 0 });
    for (const ev of r.events) if (ev.type === 'entry.done') state.log.push(ev);
  }

  return {
    async mount(el, route) {
      root = el;
      archivesEl = h('section', { class: 'card archives-card', 'data-testid': 'archives-section' });
      tabsEl = h('div', {});
      tab = pickTab([{ key: 'current' }, { key: 'archives' }], route.query.tab);
      replace(root, h('div', { class: 'page-head' }, h('h1', { text: t('portal.nav.run') })), tabsEl,
        tabPanel('current', mainEl = h('div', {}, loading())), tabPanel('archives', archivesEl));
      renderTabs();
      delegate(root, 'click', 'action', handlers.click);
      delegate(root, 'change', 'change', handlers.change);
      disposers.push(on('job', ({ job: j, before }) => {
        const changed = !before || before.status !== j.status || before.plan_id !== j.plan_id;
        if (active(before) && j.kind === 'run' && j.status === 'done') {   // the result is read on the plan's own page
          location.hash = `#/plan/${j.plan_id}`;
          return;
        }
        if (changed) loadPending().then(() => (active(j) ? null : loadPlans())).then(() => keepFocus(root, renderMain)).catch(reportError);
        else keepFocus(root, renderMain);
      }));
      disposers.push(on('events', (events) => {
        if (events.some((e) => e.type === 'entry.done')) renderLog();
      }));
      await refreshJob();
      await seedLog();
      await loadPending();
      if (!active(job())) await loadPlans();
      renderMain();
      await archivesPart.mount(archivesEl);
      if (tab === 'archives') {   // #/archives lands here
        archivesEl.querySelector('#archives-title').focus({ preventScroll: true });
      }
    },
    unmount() {
      disposers.forEach((f) => f());
      archivesPart.unmount();
    },
  };
}
