// Shared widgets: toasts, modal dialogs, badges, the copy button and the "your selection changed" notes.
import { ApiError } from './api.mjs';
import { h, replace } from './dom.mjs';
import { fmtBytes, fmtCount, pluralCount } from './format.mjs';
import { t } from './i18n.mjs';
import { icon } from './icons.mjs';

const TOAST_ICON = { ok: 'check', warn: 'alert', error: 'alert', info: 'info' };

export function toast(text, kind = 'info', ms = kind === 'error' ? 12000 : 5000) {
  const box = document.getElementById('toasts');
  const el = h('div', { class: `toast toast-${kind}`, role: kind === 'error' ? 'alert' : null }, icon(TOAST_ICON[kind] || 'info', 18), h('span', { text }));
  box.append(el);
  setTimeout(() => el.remove(), ms);
}

export function reportError(e) {
  if (e instanceof ApiError) {
    toast(e.message, 'error');
  } else {
    console.error(e);
    toast(`${e.name}: ${e.message}`, 'error');
  }
}

// buttons: [{label, value, primary?, danger?, fill?, testid?}]. Resolves to the pressed button's value, null if dismissed.
// solid fills a primary button; fill is the permanent step of a confirmation: it keeps a warning icon and outline.
export function modal({ title, body, buttons }) {
  const dialog = document.getElementById('dialog');
  return new Promise((resolve) => {
    const form = h('form', { method: 'dialog', class: 'dialog-body' },
      h('h2', { text: title }),
      h('div', { class: 'dialog-content' }, body),
      h('div', { class: 'dialog-actions' }, buttons.map((b) =>
        h('button', { type: 'submit', value: b.value, class: `btn${b.primary ? ' primary' : ''}${b.solid ? ' solid' : ''}${b.danger ? ' danger' : ''}${b.fill ? ' danger-fill' : ''}`,
          disabled: b.disabled, 'data-testid': b.testid, text: b.label }))));
    replace(dialog, form);
    dialog.returnValue = '';
    dialog.addEventListener('close', () => resolve(dialog.returnValue || null), { once: true });
    dialog.showModal();
  });
}

export async function confirmDialog({ title, body, confirm, danger = false }) {
  const answer = await modal({ title, body, buttons: [
    { label: t('portal.common.cancel'), value: 'no' },
    { label: confirm, value: 'yes', primary: !danger, danger }] });
  return answer === 'yes';
}

// A status pill: the icon and the words say what the colour says.
const BADGE_ICON = { ok: 'check', warn: 'alert', danger: 'alert', info: 'info' };
export const badge = (text, kind = 'neutral') => h('span', { class: `badge badge-${kind}` },
  BADGE_ICON[kind] ? icon(BADGE_ICON[kind], 13) : null, text);

// junk, review and protected keep one grey tone, one icon and one word wherever they appear.
const TIER_ICON = { junk: 'cleanup', review: 'alert', protected: 'lock', report_only: 'terminal' };
export const tierBadge = (tier, text = t(`portal.tier.${tier}`)) => h('span', { class: `badge badge-tier s-${tier}`, 'data-tier': tier },
  h('span', { class: 'tone', 'aria-hidden': 'true' }), icon(TIER_ICON[tier], 13), text);

// A group that Disk Steward only lists (no action of its own, in the review tier) belongs to another tool: it is that,
// in blue, and not "needs your decision".
export const isReportOnly = (g) => g.tier === 'review' && Array.isArray(g.actions) && g.actions.length === 0;
export const groupTier = (g) => (isReportOnly(g) ? 'report_only' : g.tier);
export const groupBadge = (g) => tierBadge(groupTier(g));

// An empty state: an icon, what is missing and one way forward.
export function emptyState({ iconName = 'info', title, body, action, testid }) {
  return h('div', { class: 'card empty', 'data-testid': testid },
    h('span', { class: 'empty-icon' }, icon(iconName, 26)),
    h('h2', { text: title }), body ? h('p', { text: body }) : null, action || null);
}

// Sub-tabs: the parts of one page side by side, one shown at a time. The page draws the active part; the address keeps
// the choice (?tab=) without a new page load, so a reload or a shared link opens the same part.
// tabs: [{key, label, count?}]; pick(key) redraws the page with that part.
export const pickTab = (tabs, asked) => (tabs.some((x) => x.key === asked) ? asked : tabs[0].key);

export function subTabs(tabs, active, pick) {
  const list = h('div', { class: 'subtabs', role: 'tablist', 'aria-label': t('portal.tab.label'), 'data-testid': 'subtabs' },
    tabs.map((x) => h('button', { role: 'tab', id: `tab-${x.key}`, 'aria-selected': String(x.key === active), 'aria-controls': `tabpanel-${x.key}`,
      tabindex: x.key === active ? '0' : '-1', 'data-tab': x.key, 'data-testid': `tab-${x.key}` },
      h('span', { text: x.label }), x.count ? h('span', { class: 'tab-count', text: fmtCount(x.count) }) : null)));
  const choose = (key) => {
    const focused = list.contains(document.activeElement);
    const [path, query = ''] = location.hash.split('?');
    const params = new URLSearchParams(query);
    params.set('tab', key);
    history.replaceState(null, '', `${path}?${params}`);
    pick(key);
    if (focused) document.getElementById(`tab-${key}`).focus({ preventScroll: true });   // the redraw replaced the tab under the keyboard
  };
  list.addEventListener('click', (ev) => {
    const b = ev.target instanceof Element ? ev.target.closest('[role="tab"]') : null;
    if (b && b.dataset.tab !== active) choose(b.dataset.tab);
  });
  list.addEventListener('keydown', (ev) => {
    const at = tabs.findIndex((x) => x.key === active);
    const to = { ArrowRight: at + 1, ArrowLeft: at - 1, Home: 0, End: tabs.length - 1 }[ev.key];
    if (to === undefined) return;
    ev.preventDefault();
    const key = tabs[(to + tabs.length) % tabs.length].key;
    if (key !== active) choose(key);
  });
  return list;
}

export const tabPanel = (key, ...children) => h('div', { class: 'tab-panel', role: 'tabpanel', id: `tabpanel-${key}`, 'aria-labelledby': `tab-${key}` }, children);

// One exact line, for a command or a path: a line break in it would run in a terminal as two commands.
export function copyButton(text) {
  if (/[\r\n]/.test(text)) throw new Error(`copyButton copies one line, got ${JSON.stringify(text)}`);
  return h('button', { class: 'btn small', data: { copy: text } }, icon('copy', 14), t('portal.common.copy'));
}

// Text of several lines that is never run, such as the advisor prompt pasted into a chat.
export function copyTextButton(text) {
  return h('button', { class: 'btn small', data: { copy: text } }, icon('copy', 14), t('portal.common.copy'));
}

export function codeBlock(text) {
  return h('div', { class: 'codeline' }, h('code', { text }), copyButton(text));
}

const NONE_QUALIFY = 'warning.group_selected_none_qualify';

// "1 local snapshot(s)" is the server's wording; the portal says one or many properly.
const warningText = (w) => (w.key === 'warning.snapshots' ? pluralCount('portal.warning.snapshots', w.count) : w.text);

// The warnings of a selection or a plan. "This group has nothing that qualifies" belongs to the selection (fold:
// one line with the groups behind it) and means nothing for a plan, which holds only what qualified (hide).
export function warningList(warnings, groups = 'hide') {
  const real = (warnings || []).filter((w) => w.key !== NONE_QUALIFY);
  const none = groups === 'fold' ? (warnings || []).filter((w) => w.key === NONE_QUALIFY) : [];
  if (!real.length && !none.length) return null;
  return h('div', { class: 'warning-box' },
    real.length ? h('ul', { class: 'warnings' }, real.map((w) => h('li', { text: warningText(w) }))) : null,
    none.length ? h('details', { class: 'fold', 'data-testid': 'none-qualify' },
      h('summary', { text: pluralCount('portal.summary.none_qualify_groups', none.length) }),
      h('ul', {}, none.map((w) => h('li', { text: w.group_label })))) : null);
}

// Skeleton lines instead of a bare word; the status role keeps the word for screen readers.
export function loading() {
  return h('div', { class: 'skeleton', role: 'status', 'aria-label': t('portal.common.loading') }, h('i'), h('i'), h('i'), h('i'));
}

// True when a rescan changed what the selection holds; kept exclusions alone change nothing for the user.
export function notesChanged(notes) {
  return notes.dropped_groups.length > 0 || notes.include_changed.count > 0 || notes.include_missing.count > 0
    || notes.group_changes.some((c) => c.old_selected !== c.new_selected || c.old_alloc !== c.new_alloc);
}

export function notesBlock(notes, labels) {
  const label = (key) => labels[key] || key;
  const items = [];
  for (const g of notes.dropped_groups) items.push(h('li', { text: t('portal.notes.dropped_group', { group: label(g) }) }));
  for (const kind of ['include_changed', 'include_missing']) {
    const found = notes[kind];
    if (found.count) {
      items.push(h('li', {}, pluralCount(`portal.notes.${kind}`, found.count),
        h('ul', { class: 'paths' }, found.paths.map((p) => h('li', { text: p })))));
    }
  }
  if (notes.kept_excludes) items.push(h('li', { text: pluralCount('portal.notes.kept_excludes', notes.kept_excludes) }));
  for (const c of notes.group_changes) {
    if (c.old_selected === c.new_selected && c.old_alloc === c.new_alloc) continue;
    items.push(h('li', { text: t('portal.notes.group_change', {
      group: label(c.group), old: fmtCount(c.old_selected), new: fmtCount(c.new_selected),
      old_size: fmtBytes(c.old_alloc), new_size: fmtBytes(c.new_alloc) }) }));
  }
  return items.length
    ? h('ul', { class: 'notes' }, items)
    : h('p', { class: 'muted', text: t('portal.notes.unchanged') });
}

export function stateTable(breakdown) {
  const rows = Object.entries(breakdown).map(([state, v]) =>
    h('tr', {}, h('td', { text: t(`decision.${state}`) }), h('td', { class: 'num', text: fmtCount(v.count) }),
      h('td', { class: 'num', text: fmtBytes(v.alloc) })));
  return h('div', { class: 'table-wrap' }, h('table', {},
    h('thead', {}, h('tr', {}, h('th', { text: t('portal.col.decision') }), h('th', { class: 'num', text: t('portal.col.items') }),
      h('th', { class: 'num', text: t('portal.col.size') }))),
    h('tbody', {}, rows)));
}


// "removes X / frees about Y" for one action's totals ({count, alloc, frees, frees_estimate}).
export function actionLine(action, v) {
  if (action === 'delete') return t('portal.summary.delete', { count: fmtCount(v.count), size: fmtBytes(v.alloc), frees: fmtBytes(v.frees) });
  if (action === 'trash') return t('portal.summary.trash', { count: fmtCount(v.count), size: fmtBytes(v.alloc) });
  return t('portal.summary.archive', { count: fmtCount(v.count), size: fmtBytes(v.alloc), frees: fmtBytes(v.frees_estimate) });
}
