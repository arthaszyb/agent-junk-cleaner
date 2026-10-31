// Restorable archives, a section of the Cleanup page: the compressed backups the Archive action leaves behind. Restore
// never overwrites; the server's refusal is shown as is.
import { api, ApiError } from '../api.mjs';
import { delegate, h, replace } from '../dom.mjs';
import { fmtBytes, fmtTime } from '../format.mjs';
import { t } from '../i18n.mjs';
import { icon } from '../icons.mjs';
import { colgroup, dotLabel, midPath, plainTh } from '../tables.mjs';
import { on } from '../state.mjs';
import { confirmDialog, emptyState, reportError } from '../ui.mjs';

const STATE_DOT = { stored: 'free', restored: 'report_only', purged: 'outside', discarded: 'outside' };

export function create() {
  let root;
  let rows = null;
  let notice = null;      // {kind, text} of the last restore or purge
  const disposers = [];

  async function load() {
    rows = (await api.get('/api/archives')).archives.slice().reverse();
    render();
  }

  function table() {
    return h('div', { class: 'table-card plain' }, h('table', { class: 'data archives-table', 'data-testid': 'archives' },
      colgroup('c-path', 'c-size hide-sm', 'c-size hide-sm', 'c-changed hide-md', 'c-status', 'c-action'),
      h('thead', {}, h('tr', {}, plainTh(t('portal.col.path'), 'c-path'), plainTh(t('portal.col.size'), 'c-size num hide-sm'),
        plainTh(t('portal.archives.col_stored'), 'c-size num hide-sm'), plainTh(t('portal.archives.col_created'), 'c-changed hide-md'),
        plainTh(t('portal.col.status'), 'c-status'), plainTh(t('portal.col.action'), 'c-action'))),
      h('tbody', {}, rows.map((a) => h('tr', { class: 'archive', 'data-testid': 'archive', 'data-path': a.path, 'data-state': a.state },
        h('td', { class: 'c-path' }, midPath(a.path),
          h('span', { class: 'sub meta-sm' }, [fmtBytes(a.orig_size), fmtBytes(a.stored_bytes), fmtTime(a.created_at)].join(' · '))),
        h('td', { class: 'c-size num hide-sm', text: fmtBytes(a.orig_size) }),
        h('td', { class: 'c-size num hide-sm', text: fmtBytes(a.stored_bytes) }),
        h('td', { class: 'c-changed hide-md', text: fmtTime(a.created_at) }),
        h('td', { class: 'c-status' }, dotLabel(STATE_DOT[a.state] || 'neutral', t(`cli.archive_state.${a.state}`))),
        h('td', { class: 'c-action' }, h('div', { class: 'row-actions' },
          a.state === 'stored' ? h('button', { class: 'btn small', data: { action: 'restore', id: a.id }, 'data-testid': 'restore', text: t('portal.archives.restore') }) : null,
          a.state === 'stored' || a.state === 'restored' ? h('button', { class: 'btn small danger', data: { action: 'purge', id: a.id, path: a.path }, 'data-testid': 'purge' },
            icon('trash', 14), t('portal.archives.purge')) : null)))))));
  }

  function render() {
    if (!rows) return;
    replace(root,
      h('div', { class: 'section-head' }, h('div', { class: 'section-title' }, h('h2', { id: 'archives-title', tabindex: '-1', text: t('portal.archives.section_title') }),
        h('p', { text: t('portal.archives.explain') }))),
      notice ? h('div', { class: `banner banner-${notice.kind}`, role: 'alert', 'data-testid': 'archive-notice', text: notice.text }) : null,
      rows.length ? table() : emptyState({ iconName: 'archive', title: t('portal.archives.empty_title'), body: t('portal.archives.empty_body'), testid: 'archives-empty' }));
  }

  const handlers = {
    restore: async (el) => {
      try {
        const r = await api.post(`/api/archives/${el.dataset.id}/restore`, {});
        notice = { kind: 'ok', text: t('cli.archives.restored', { path: r.path, size: fmtBytes(r.bytes) }) };
      } catch (e) {
        if (!(e instanceof ApiError)) throw e;
        notice = { kind: 'danger', text: e.message };
      }
      await load();
    },
    purge: async (el) => {
      const ok = await confirmDialog({ title: t('portal.archives.purge_title'),
        body: h('p', { text: t('portal.archives.purge_body', { id: el.dataset.id, path: el.dataset.path }) }),
        confirm: t('portal.archives.purge'), danger: true });
      if (!ok) return;
      try {
        const r = await api.post(`/api/archives/${el.dataset.id}/purge`, {});
        notice = { kind: 'ok', text: t('cli.archives.purged', { id: r.archive_id, size: fmtBytes(r.bytes) }) };
      } catch (e) {
        if (!(e instanceof ApiError)) throw e;
        notice = { kind: 'danger', text: e.message };
      }
      await load();
    },
  };

  return {
    async mount(el) {
      root = el;
      delegate(root, 'click', 'action', handlers);
      disposers.push(on('events', (events) => {
        if (events.some((e) => e.type === 'run.finished' || e.type.startsWith('archive.'))) load().catch(reportError);
      }));
      await load();
    },
    unmount() { disposers.forEach((f) => f()); },
  };
}
