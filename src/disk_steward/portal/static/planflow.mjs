// What to do when the server refuses to make a plan from the numbers the user saw. Shared by Browse and the overview.
import { api, ApiError } from './api.mjs';
import { h } from './dom.mjs';
import { fmtBytes, fmtCount, pluralCount } from './format.mjs';
import { t } from './i18n.mjs';
import { modal, notesBlock, reportError, stateTable } from './ui.mjs';

const changeList = (title, rows) => rows.length ? h('div', {}, h('h3', { text: title }),
  h('ul', { class: 'plain changes' }, rows.map((r) => h('li', { text: t('portal.plan.change_row', {
    path: r.path, action: t(`action.${r.action}`), size: fmtBytes(r.alloc) }) })))) : null;

const lower = (state) => t(`decision.${state}`).toLowerCase();

// ctx.reload(): refresh what the caller shows and return its summary {count, alloc}; ctx.create(): ask again.
export async function refused(e, ctx) {
  if (!(e instanceof ApiError)) throw e;
  if (e.code === 'DIGEST_MISMATCH') {
    const summary = await ctx.reload();
    const d = e.details;
    const known = d.added_count !== null && d.added_count !== undefined;
    const answer = await modal({
      title: t('portal.plan.changed_title'),
      body: [h('p', { text: e.message }),
        known ? [changeList(t('portal.plan.added', { count: fmtCount(d.added_count) }), d.added),
          changeList(t('portal.plan.removed', { count: fmtCount(d.removed_count) }), d.removed)]
          : h('p', { class: 'muted', text: t('portal.plan.changes_unknown') }),
        h('p', { class: 'big', text: pluralCount('portal.plan.now', summary.count, { size: fmtBytes(summary.alloc) }) })],
      buttons: [{ label: t('portal.common.cancel'), value: 'no' },
        { label: pluralCount('portal.plan.create_new', summary.count, { size: fmtBytes(summary.alloc) }), value: 'yes', primary: true,
          disabled: summary.count === 0 }] });
    if (answer === 'yes') await ctx.create();
    return;
  }
  if (e.code === 'EMPTY_PLAN') {
    const list = Object.entries(e.details.breakdown).filter(([k]) => k !== 'selected')
      .map(([k, v]) => `${fmtCount(v.count)} ${lower(k)}`).join(', ');
    await modal({ title: t('portal.plan.empty_title'),
      body: [h('p', { text: e.message }), h('p', { class: 'big', text: t('portal.plan.empty_breakdown', { list: list || '-' }) }), stateTable(e.details.breakdown)],
      buttons: [{ label: t('portal.common.close'), value: 'ok', primary: true }] });
    return;
  }
  if (e.messageKey === 'error.spec_stale') {
    await modal({ title: t('portal.notes.title'),
      body: [h('p', { text: e.message }), e.details.notes ? notesBlock(e.details.notes, e.details.labels || {}) : null],
      buttons: [{ label: t('portal.common.close'), value: 'ok', primary: true }] });
    await ctx.reload();
    return;
  }
  reportError(e);
}

// "Review and clean": the plan for exactly the quick selection the page shows. getQuick() returns the selection the
// page displays now; reload() refreshes it (after a refusal) and returns its summary {count, alloc}.
export async function quickPlan(getQuick, reload) {
  const attempt = async () => {
    const q = getQuick();
    const expected = { scan_id: q.scan_id, spec_version: q.spec.version, digest: q.digest, resolved_at: q.resolved_at };
    try {
      const plan = await api.post('/api/quick/plan', { days: q.days, expected });
      location.hash = `#/plan/${plan.id}`;
    } catch (e) {
      await refused(e, { create: attempt, reload });
    }
  };
  await attempt();
}
