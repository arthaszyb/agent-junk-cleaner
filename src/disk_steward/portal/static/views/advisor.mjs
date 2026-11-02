// AI advisor: off by default, shows exactly what would be sent before anything leaves, and its answer only
// annotates groups. It never changes the selection.
import { api, ApiError } from '../api.mjs';
import { delegate, h, replace } from '../dom.mjs';
import { fmtBytes, fmtTime } from '../format.mjs';
import { t } from '../i18n.mjs';
import { loadState, on } from '../state.mjs';
import { badge, copyTextButton, loading, pickTab, reportError, subTabs, tabPanel } from '../ui.mjs';

const VERDICT_KIND = { likely_safe: 'ok', review: 'warn', keep: 'neutral' };
const PROVIDERS = ['codex', 'claude'];

export function create() {
  let root;
  let status = null;
  let unavailable = null;
  let preview = null;
  let exported = null;       // {text}
  let waiting = null;        // provider name while a run is in flight
  let failure = null;
  let importError = null;
  let tab = null;            // 'ask' or 'results'; a new answer opens 'results'
  const disposers = [];

  async function load() {
    try {
      status = await api.get('/api/advisor');
      unavailable = null;
    } catch (e) {
      if (!(e instanceof ApiError) || e.status !== 503) throw e;
      status = null;
      unavailable = e.message;
    }
    render();
  }

  function providerBox(name) {
    const p = status.providers[name];
    const ready = p && p.available === true && preview && !waiting;
    // available: true and false are answers of a probe; null means no probe ran (the advisor is off, or nothing cached)
    const note = p && p.available === true ? t('portal.advisor.version', { version: p.version || '?' })
      : p && p.available === false ? t('portal.advisor.unavailable_provider', { reason: p.reason || '' })
        : t('portal.advisor.not_checked');
    return h('div', { class: 'provider' },
      h('button', { class: 'btn primary', disabled: !ready, data: { action: 'run', provider: name }, 'data-testid': `ask-${name}`,
        text: t(`portal.advisor.ask_${name}`) }),
      h('span', { class: 'muted', 'data-testid': `provider-${name}`, text: note }));
  }

  function previewBox() {
    if (!preview) return null;
    const c = preview.coverage;
    return h('div', { 'data-testid': 'preview' },
      preview.notice_key ? h('p', { class: 'banner banner-info', 'data-testid': 'preview-notice', text: t(preview.notice_key) }) : null,
      h('p', { text: t('portal.advisor.coverage', { included: c.groups_included, total: c.groups_total,
        size: fmtBytes(c.alloc_included), all: fmtBytes(c.alloc_total) }) }),
      h('p', { class: 'muted', text: t('portal.advisor.payload_size', { size: fmtBytes(preview.bytes), expires: fmtTime(preview.expires_at) }) }),
      h('pre', { class: 'payload', tabindex: '0', 'data-testid': 'payload', text: JSON.stringify(preview.payload, null, 2) }));
  }

  function manualBox() {
    return h('div', { class: 'manual' },
      h('h3', { text: t('portal.advisor.manual') }),
      h('p', { class: 'muted', text: t('portal.advisor.manual_hint') }),
      h('button', { class: 'btn', disabled: !preview, data: { action: 'export' }, text: t('portal.advisor.show_prompt') }),
      exported ? h('div', {}, h('textarea', { readonly: true, rows: '8', 'aria-label': t('portal.advisor.show_prompt'), text: exported.text }), copyTextButton(exported.text)) : null,
      h('label', { class: 'block' }, h('span', { text: t('portal.advisor.paste') }),
        h('textarea', { rows: '6', id: 'reply', 'data-testid': 'reply' })),
      importError ? h('p', { class: 'banner banner-danger', text: importError }) : null,
      h('button', { class: 'btn', disabled: !preview, data: { action: 'import' }, text: t('portal.advisor.import') }));
  }

  function resultBox() {
    const r = status.last;
    if (!r) return h('p', { class: 'muted', text: t('portal.advisor.no_result') });
    return h('div', { 'data-testid': 'advice' },
      h('p', {}, t('portal.advisor.result_head', { provider: r.provider, when: fmtTime(r.created_at) }), ' ',
        r.stale ? badge(t('portal.advisor.stale'), 'warn') : null),
      h('p', { text: r.summary }),
      (r.traces || []).map((path) => h('p', { class: 'muted', 'data-testid': 'trace', text: t('advisor.cli.trace', { provider: r.provider, path }) })),
      h('ul', { class: 'plain recs' }, r.recommendations.map((rec) => h('li', { class: 'rec', 'data-testid': 'rec' },
        badge(t(`portal.advisor.verdict.${rec.verdict}`), VERDICT_KIND[rec.verdict]), ' ',
        h('strong', { text: rec.label }), h('div', { text: rec.reason }),
        h('a', { href: `#/browse?group=${encodeURIComponent(rec.group)}`, text: t('portal.advisor.go_to_group') })))),
      h('p', { class: 'muted', text: t('portal.advisor.never_selects') }));
  }

  function render() {
    if (unavailable) {
      replace(root, h('div', { class: 'page-head' }, h('h1', { text: t('portal.nav.advisor') })),
        h('div', { class: 'banner banner-warn', 'data-testid': 'advisor-unavailable', text: unavailable }));
      return;
    }
    if (!status) return;
    const tabs = [{ key: 'ask', label: t('portal.tab.adv_ask') },
      { key: 'results', label: t('portal.advisor.results'), count: status.last ? status.last.recommendations.length : 0 }];
    tab = pickTab(tabs, tab);
    const ask = () => [
      h('section', { class: 'card' },
        h('p', { text: t('portal.advisor.explain') }),
        h('p', { class: 'muted', text: t('portal.advisor.privacy') }),
        h('label', { class: 'inline' },
          h('input', { type: 'checkbox', class: 'switch', checked: status.enabled, data: { change: 'enable' }, 'data-testid': 'advisor-enabled' }),
          h('span', { text: t('portal.advisor.enable') }))),
      status.enabled ? h('section', { class: 'card' },
        h('h2', { text: t('portal.advisor.step_preview') }),
        h('button', { class: 'btn', data: { action: 'preview' }, 'data-testid': 'preview-button', text: t('portal.advisor.preview') }),
        previewBox(),
        h('h2', { text: t('portal.advisor.step_ask') }),
        h('div', { class: 'providers' }, PROVIDERS.map(providerBox)),
        waiting ? h('p', { role: 'status', 'data-testid': 'advisor-waiting', text: t('portal.advisor.waiting', { provider: waiting }) }) : null,
        failure ? h('div', { class: 'banner banner-danger', role: 'alert', text: failure }) : null,
        manualBox()) : h('div', { class: 'banner banner-info', text: t('portal.advisor.disabled') })];
    replace(root,
      h('div', { class: 'page-head' }, h('h1', { text: t('portal.nav.advisor') })),
      subTabs(tabs, tab, (key) => { tab = key; render(); }),
      tabPanel(tab, tab === 'ask' ? ask() : h('section', { class: 'card' }, h('h2', { text: t('portal.advisor.results') }), resultBox())));
  }

  async function setEnabled(enabled) {
    status = await api.post('/api/advisor/enable', { enabled });
    if (!enabled) { preview = null; exported = null; }
    await loadState();
    render();
  }

  const handlers = {
    click: {
      preview: async () => {
        preview = await api.post('/api/advisor/preview', {});
        exported = null;
        render();
      },
      run: async (el) => {
        failure = null;
        waiting = el.dataset.provider;
        render();
        try {
          await api.post('/api/advisor/run', { provider: waiting, request_id: preview.request_id });
        } catch (e) {
          waiting = null;
          render();
          throw e;
        }
      },
      export: async () => {
        exported = await api.post('/api/advisor/export', { request_id: preview.request_id });
        render();
      },
      import: async () => {
        importError = null;
        try {
          await api.post('/api/advisor/import', { request_id: preview.request_id, text: root.querySelector('#reply').value });
        } catch (e) {
          if (!(e instanceof ApiError)) throw e;
          importError = e.message;
          render();
          return;
        }
        tab = 'results';
        await load();
      },
    },
    change: { enable: (el) => setEnabled(el.checked) },
  };

  return {
    async mount(el, route) {
      root = el;
      tab = route.query.tab || null;
      replace(root, loading());
      delegate(root, 'click', 'action', handlers.click);
      delegate(root, 'change', 'change', handlers.change);
      disposers.push(on('events', (events) => {
        for (const ev of events) {
          if (ev.type === 'advisor.done') { waiting = null; failure = null; tab = 'results'; load().catch(reportError); }
          if (ev.type === 'advisor.failed') {
            waiting = null;
            failure = ev.data.message || ev.data.error || t('portal.advisor.failed');
            render();
          }
        }
      }));
      await load();
    },
    unmount() { disposers.forEach((f) => f()); },
  };
}
