// Settings. Fields that shape what a scan finds are locked while a cleanup runs; the server enforces it.
import { api } from '../api.mjs';
import { delegate, h, replace } from '../dom.mjs';
import { t } from '../i18n.mjs';
import { loadState, on, state } from '../state.mjs';
import { loading, pickTab, subTabs, tabPanel, toast } from '../ui.mjs';

const MIB = 1024 * 1024;
const DOMAINS = ['agent', 'app-library', 'user-files', 'dev-shared'];
const NUMBERS = ['default_min_age_days', 'idle_days', 'installer_age_days', 'project_idle_days'];
const SIZES = ['large_file_min_bytes', 'idle_file_min_bytes'];
const CLASSIFYING = ['domains', 'user_roots', 'idle_days', 'installer_age_days', 'project_idle_days', ...SIZES];

export function create() {
  let root;
  let current = null;
  let live = [];
  let tab = null;   // the panels are hidden, not redrawn, so an unsaved edit survives a tab change
  const disposers = [];

  async function load() {
    const r = await api.get('/api/settings');
    current = r.settings;
    live = r.live_fields;
    render();
  }

  const running = () => state.job && (state.job.status === 'running' || state.job.status === 'paused');
  const locked = (field) => running() && !live.includes(field);

  function number(name, value, label, hint) {
    return h('label', { class: 'field' }, h('span', { text: label }),
      h('input', { type: 'number', min: '0', step: 'any', value, name, disabled: locked(name.replace('_mib', '')), 'data-testid': `set-${name}` }),
      hint ? h('span', { class: 'muted', text: hint }) : null);
  }

  function renderTabs() {
    const tabs = [['general', 'portal.tab.set_general'], ['scope', 'portal.tab.set_scope'], ['thresholds', 'portal.settings.thresholds']]
      .map(([key, label]) => ({ key, label: t(label) }));
    tab = pickTab(tabs, tab);
    replace(root.querySelector('.settings-tabs'), subTabs(tabs, tab, (key) => { tab = key; renderTabs(); }));
    for (const x of tabs) root.querySelector(`#tabpanel-${x.key}`).hidden = x.key !== tab;
  }

  function render() {
    if (!current) return;
    const lockNote = running() ? h('div', { class: 'banner banner-info', text: t('portal.settings.locked') }) : null;
    const language = h('fieldset', {},
      h('legend', { text: t('portal.settings.language') }),
      h('select', { name: 'locale', 'data-testid': 'set-locale' }, [['en', 'English'], ['zh-CN', '简体中文']].map(([v, label]) =>
        h('option', { value: v, selected: v === (current.locale || state.locale), text: label }))));
    replace(root, h('div', { class: 'page-head' }, h('h1', { text: t('portal.nav.settings') })), lockNote, h('div', { class: 'settings-tabs' }),
      h('form', { class: 'card settings', 'data-submit': 'save', 'data-testid': 'settings-form' },
        tabPanel('general', language,
          h('fieldset', {},
            h('legend', { text: t('portal.nav.advisor') }),
            h('label', { class: 'inline' }, h('input', { type: 'checkbox', class: 'switch', name: 'advisor_enabled', checked: current.advisor_enabled, 'data-testid': 'set-advisor' }),
              h('span', { text: t('portal.settings.advisor') })))),
        tabPanel('scope', h('fieldset', { disabled: locked('domains') },
          h('legend', { text: t('portal.settings.areas') }),
          DOMAINS.map((d) => h('div', { class: 'check' },
            h('label', { class: 'inline' }, h('input', { type: 'checkbox', class: 'switch', name: `domain:${d}`, checked: current.domains.includes(d), 'data-testid': `set-domain-${d}` }),
              h('span', { text: t(`domain.${d}`) })),
            d === 'user-files' ? h('p', { class: 'muted', text: t('portal.settings.user_files_note') }) : null))),
          h('fieldset', { disabled: locked('user_roots') },
            h('legend', { text: t('portal.settings.roots') }),
            h('p', { class: 'muted', text: t('portal.settings.roots_hint') }),
            h('textarea', { name: 'user_roots', rows: '4', text: current.user_roots.join('\n'), 'data-testid': 'set-user_roots' }))),
        tabPanel('thresholds', h('fieldset', {},
          h('legend', { text: t('portal.settings.thresholds') }),
          number('default_min_age_days', current.default_min_age_days, t('portal.settings.default_age')),
          number('large_file_min_bytes_mib', current.large_file_min_bytes / MIB, t('portal.settings.large_file')),
          number('idle_file_min_bytes_mib', current.idle_file_min_bytes / MIB, t('portal.settings.idle_file')),
          number('idle_days', current.idle_days, t('portal.settings.idle_days')),
          number('installer_age_days', current.installer_age_days, t('portal.settings.installer_days')),
          number('project_idle_days', current.project_idle_days, t('portal.settings.project_days')))),
        h('button', { class: 'btn primary', type: 'submit', 'data-testid': 'save-settings', text: t('portal.common.save') })));
    renderTabs();
  }

  async function save(form) {
    const f = new FormData(form);
    const patch = {};
    const num = (name) => Number(f.get(name));
    // a field the form disabled is not in FormData; it was not edited
    for (const name of NUMBERS) if (f.has(name) && num(name) !== current[name]) patch[name] = num(name);
    for (const name of SIZES) {
      if (!f.has(`${name}_mib`)) continue;
      const bytes = Math.round(num(`${name}_mib`) * MIB);
      if (bytes !== current[name]) patch[name] = bytes;
    }
    if (!locked('domains')) {
      const domains = DOMAINS.filter((d) => f.get(`domain:${d}`) !== null);
      if (domains.join() !== current.domains.slice().sort((a, b) => DOMAINS.indexOf(a) - DOMAINS.indexOf(b)).join()) patch.domains = domains;
    }
    if (f.has('user_roots')) {
      const roots = String(f.get('user_roots')).split('\n').map((x) => x.trim()).filter(Boolean);
      if (roots.join('\n') !== current.user_roots.join('\n')) patch.user_roots = roots;
    }
    const locale = String(f.get('locale'));
    if (locale !== (current.locale || state.locale)) patch.locale = locale;
    const advisor = f.get('advisor_enabled') !== null;
    if (Object.keys(patch).length) await api.put('/api/settings', patch);
    if (advisor !== current.advisor_enabled) await api.post('/api/advisor/enable', { enabled: advisor });
    if (!Object.keys(patch).length && advisor === current.advisor_enabled) {
      toast(t('portal.settings.unchanged'));
      return;
    }
    if (patch.locale) { location.reload(); return; }   // every text on the page and from the server is in the old language
    await Promise.all([load(), loadState()]);
    toast(t('portal.settings.saved'), 'ok');
    if (CLASSIFYING.some((k) => k in patch)) toast(t('portal.settings.rescan_hint'));
  }

  return {
    async mount(el, route) {
      root = el;
      tab = route.query.tab || null;
      replace(root, loading());
      // a bad number on a hidden tab would block Save without a word: show the tab that holds it
      root.addEventListener('invalid', (ev) => {
        const panel = ev.target.closest('[role="tabpanel"]');
        if (panel && panel.hidden) { tab = panel.id.replace('tabpanel-', ''); renderTabs(); }
      }, true);
      delegate(root, 'submit', 'submit', { save: (form, ev) => { ev.preventDefault(); return save(form); } });
      disposers.push(on('job', () => { if (current) render(); }));
      await load();
    },
    unmount() { disposers.forEach((f) => f()); },
  };
}
