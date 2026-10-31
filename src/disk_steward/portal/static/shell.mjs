// The frame around the views: the sidebar (navigation, scan, theme, language and the disk meter), which becomes a top
// bar with a drawer on a narrow screen. The views never touch any of it.
import { api } from './api.mjs';
import { h, replace } from './dom.mjs';
import { fmtAgo, fmtBytes } from './format.mjs';
import { t } from './i18n.mjs';
import { icon } from './icons.mjs';
import { diskLevel } from './scale.mjs';
import { on, setJobFromResponse, state } from './state.mjs';
import { reportError } from './ui.mjs';

const NAV = [['overview', 'overview'], ['insights', 'insights'], ['browse', 'browse'], ['run', 'cleanup'], ['advisor', 'advisor'], ['settings', 'settings']];
const THEME_KEY = 'disk-steward-theme';   // theme-init.js reads the same key before the first paint
const THEMES = [['system', 'monitor'], ['light', 'sun'], ['dark', 'moon']];
const LOCALES = [['en', 'EN', 'English'], ['zh-CN', '中文', '简体中文']];
const NARROW = '(max-width: 960px)';
const FOCUSABLE = 'a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])';

let bar;
let panel;
let menuButton;
let theme = 'system';

// theme --------------------------------------------------------------------------------------------------------

function savedTheme() {
  try {
    const v = localStorage.getItem(THEME_KEY);
    return v === 'light' || v === 'dark' ? v : 'system';
  } catch (e) {
    return 'system';   // storage is blocked: follow the system
  }
}

function applyTheme(mode) {
  theme = mode;
  if (mode === 'system') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme', mode);
}

function nextTheme() {
  const mode = THEMES[(THEMES.findIndex(([m]) => m === theme) + 1) % THEMES.length][0];
  applyTheme(mode);
  try {
    if (mode === 'system') localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, mode);
  } catch (e) {
    // storage is blocked: the choice lasts until the page is reloaded
  }
  renderTools();
}

// drawer -------------------------------------------------------------------------------------------------------

const narrow = () => window.matchMedia(NARROW).matches;
const isOpen = () => bar.dataset.drawer === 'open';

function openDrawer() {
  bar.dataset.drawer = 'open';
  menuButton.setAttribute('aria-expanded', 'true');
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-modal', 'true');
  document.getElementById('main').inert = true;
  (panel.querySelector('#nav a[aria-current="page"]') || panel.querySelector(FOCUSABLE)).focus();
}

function closeDrawer(refocus = true) {
  if (!isOpen()) return;
  delete bar.dataset.drawer;
  menuButton.setAttribute('aria-expanded', 'false');
  panel.removeAttribute('role');
  panel.removeAttribute('aria-modal');
  document.getElementById('main').inert = false;
  if (refocus) menuButton.focus();
}

function onKeydown(ev) {
  if (!isOpen()) return;
  if (ev.key === 'Escape') {
    ev.preventDefault();
    closeDrawer();
    return;
  }
  if (ev.key !== 'Tab') return;
  const items = [...panel.querySelectorAll(FOCUSABLE)].filter((el) => el.offsetParent !== null);
  const first = items[0];
  const last = items[items.length - 1];
  if (ev.shiftKey && (document.activeElement === first || !panel.contains(document.activeElement))) {
    ev.preventDefault();
    last.focus();
  } else if (!ev.shiftKey && (document.activeElement === last || !panel.contains(document.activeElement))) {
    ev.preventDefault();
    first.focus();
  }
}

// navigation and status ---------------------------------------------------------------------------------------

export function renderNav(route) {
  replace(document.getElementById('nav'), NAV.map(([name, iconName]) =>
    h('a', { href: `#/${name}`, 'aria-current': name === route.name ? 'page' : null, 'data-testid': `nav-${name}` },
      icon(iconName, 18), h('span', { text: t(`portal.nav.${name}`) }))));   // a plan page belongs to no tab
}

export function renderStatus() {
  const j = state.job;
  const make = () => {
    if (state.offline) return h('span', { class: 'status status-warn', role: 'status' }, icon('alert', 14), t('portal.status.offline'));
    if (j && j.status === 'running') {
      return j.kind === 'scan'
        ? h('a', { class: 'status', href: '#/overview' }, icon('scan', 14), t('portal.status.scanning'))
        : h('a', { class: 'status', href: '#/run' }, icon('cleanup', 14), t('portal.status.running', { done: j.counts.done, total: j.counts.total }));
    }
    if (j && j.status === 'paused') return h('a', { class: 'status status-warn', href: '#/run' }, icon('alert', 14), t('portal.status.paused'));
    return null;
  };
  const slot = document.getElementById('status');
  if (slot) replace(slot, make());
  replace(document.getElementById('status-top'), make());
}

// disk meter --------------------------------------------------------------------------------------------------

function disk() {
  const s = state.scan;
  if (!s || s.vol_total === null || s.vol_total === undefined || s.vol_free === null || s.vol_free === undefined) return null;
  const level = diskLevel(s.vol_total - s.vol_free, s.vol_total);
  const meter = h('span', { class: 'meter', 'data-level': level, role: 'img',
    'aria-label': t('portal.shell.disk_free', { free: fmtBytes(s.vol_free), total: fmtBytes(s.vol_total) }) }, h('span', { class: 'meter-fill' }));
  meter.firstChild.style.width = `${Math.round(((s.vol_total - s.vol_free) / s.vol_total) * 100)}%`;   // CSSOM: allowed by the CSP, unlike a style attribute
  return { s, level, meter };
}

const levelNote = (level) => (level === 'ok' ? null : h('span', { class: 'level', 'data-level': level },
  icon('alert', 13), t(level === 'crit' ? 'portal.shell.disk_crit' : 'portal.shell.disk_warn')));

const lastScan = (s, testid) => (s.finished_at ? h('span', { class: 'side-note', 'data-testid': testid,
  text: t('portal.shell.last_scan', { ago: fmtAgo((Date.now() / 1000 - s.finished_at) / 86400) }) }) : null);

// a card at the foot of the sidebar (of the drawer on a narrow screen)
function renderDisk() {
  const d = disk();
  replace(document.getElementById('drawer-foot'), h('div', { class: 'side-card', 'data-testid': 'side-disk' }, d
    ? [h('div', { class: 'side-line' }, h('span', { text: t('portal.shell.free') }), h('strong', { text: fmtBytes(d.s.vol_free) })), d.meter,
      h('div', { class: 'side-line' }, h('span', { class: 'side-note', text: t('portal.shell.of_total', { total: fmtBytes(d.s.vol_total) }) }), levelNote(d.level)),
      lastScan(d.s, 'side-last-scan')]
    : h('div', { class: 'side-line' }, icon('disk', 16), t('portal.shell.disk_unknown'))));
}

// tools: status, language, theme, scan ---------------------------------------------------------------------------

async function scan() {
  const r = await api.post('/api/scan', {});
  setJobFromResponse(r.job);
}

export function renderTools() {
  const tools = document.getElementById('top-tools');
  const kept = tools.contains(document.activeElement) ? document.activeElement.dataset.testid : null;   // a job event rebuilds the tools: keep the keyboard where it was
  const j = state.job;
  const scanning = j && j.kind === 'scan' && j.status === 'running';
  const busy = j && (j.status === 'running' || j.status === 'paused');
  const scanButton = h('button', { class: 'btn', 'data-testid': 'top-scan', disabled: busy },
    icon('scan', 16), t(scanning ? 'portal.status.scanning' : state.scan ? 'portal.shell.rescan' : 'portal.shell.scan'));
  scanButton.addEventListener('click', () => scan().catch(reportError));
  const label = `${t('portal.shell.theme')}: ${t(`portal.shell.theme_${theme}`)}`;
  const themeButton = h('button', { class: 'round-btn', 'aria-label': label, title: label, 'data-testid': 'theme-toggle', 'data-mode': theme },
    icon(THEMES.find(([m]) => m === theme)[1], 18));
  themeButton.addEventListener('click', nextTheme);
  const langGroup = h('div', { class: 'seg-ctl compact', role: 'group', 'aria-label': t('portal.shell.language'), 'data-testid': 'lang-toggle' },
    LOCALES.map(([code, short, name]) => {
      const b = h('button', { 'aria-pressed': String(code === state.locale), lang: code, title: name, 'aria-label': name, 'data-testid': `lang-${code}`, text: short });
      b.addEventListener('click', async () => {
        if (code === state.locale) return;
        try {
          await api.put('/api/settings', { locale: code });
          location.reload();   // every text on the page and from the server is in the old language
        } catch (e) {
          reportError(e);
        }
      });
      return b;
    }));
  replace(tools, h('span', { id: 'status' }), langGroup, themeButton, scanButton);
  renderStatus();
  const again = kept && tools.querySelector(`[data-testid="${kept}"]`);
  if (again && !again.disabled) again.focus({ preventScroll: true });
}

function renderAll() {
  renderTools();
  renderDisk();
}

// start --------------------------------------------------------------------------------------------------------

export function initShell() {
  bar = document.getElementById('topbar');
  panel = document.getElementById('top-panel');
  menuButton = document.getElementById('menu-button');
  applyTheme(savedTheme());
  panel.setAttribute('aria-label', t('portal.shell.main_nav'));
  document.getElementById('nav').setAttribute('aria-label', t('portal.shell.main_nav'));
  replace(menuButton, icon('menu', 20));
  menuButton.setAttribute('aria-label', t('portal.shell.menu'));
  const close = document.getElementById('side-close');
  replace(close, icon('close', 20));
  close.setAttribute('aria-label', t('portal.shell.close_menu'));
  const skip = h('button', { class: 'skip-link', text: t('portal.shell.skip') });
  skip.addEventListener('click', () => document.getElementById('main').focus());
  document.body.prepend(skip);
  menuButton.addEventListener('click', () => (isOpen() ? closeDrawer() : openDrawer()));
  close.addEventListener('click', () => closeDrawer());
  document.getElementById('scrim').addEventListener('click', () => closeDrawer());
  document.getElementById('nav').addEventListener('click', (ev) => {
    if (ev.target instanceof Element && ev.target.closest('a') && narrow()) closeDrawer(false);
  });
  document.addEventListener('keydown', onKeydown);
  window.matchMedia(NARROW).addEventListener('change', (ev) => { if (!ev.matches) closeDrawer(false); });
  window.addEventListener('hashchange', () => closeDrawer(false));
  on('job', renderAll);
  on('connection', renderStatus);
  renderAll();
}
