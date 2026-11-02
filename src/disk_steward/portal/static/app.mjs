// Entry point: signing in, hash routing and the global handlers (copy buttons). The frame around the views is shell.mjs.
import { api, ApiError, clearToken, loadToken, saveToken, setUnauthorizedHandler } from './api.mjs';
import { h, replace, setErrorHandler } from './dom.mjs';
import { load as loadMessages, t } from './i18n.mjs';
import { initShell, renderNav } from './shell.mjs';
import { loadState, on, startEvents } from './state.mjs';
import { reportError, toast } from './ui.mjs';
import { create as advisor } from './views/advisor.mjs';
import { create as browse } from './views/browse.mjs';
import { create as insights } from './views/insights.mjs';
import { create as overview } from './views/overview.mjs';
import { create as plan } from './views/plan.mjs';
import { create as run } from './views/run.mjs';
import { create as settings } from './views/settings.mjs';

const VIEWS = { overview, insights, browse, plan, run, advisor, settings };

let current = null;
let navigation = 0;

function parseRoute() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [path, query = ''] = raw.split('?');
  const [name, ...params] = path.split('/');
  if (name === 'archives') {   // the archives are a tab of the Cleanup page; an old address lands there
    history.replaceState(null, '', '#/run?tab=archives');
    return { name: 'run', params: [], query: { tab: 'archives' } };
  }
  return { name: VIEWS[name] ? name : 'overview', params: params.map(decodeURIComponent), query: Object.fromEntries(new URLSearchParams(query)) };
}

async function show() {
  const token = ++navigation;
  const route = parseRoute();
  if (current) current.unmount();
  const container = h('div', { class: `view view-${route.name}` });
  replace(document.getElementById('main'), container);   // a fresh container drops the old view's delegated listeners
  renderNav(route);
  document.title = `${t(`portal.nav.${route.name}`)} · Disk Steward`;
  current = VIEWS[route.name]();
  try {
    await current.mount(container, route);
  } catch (e) {
    if (token !== navigation) return;
    if (!(e instanceof ApiError)) throw e;
    replace(container, h('div', { class: 'banner banner-danger', role: 'alert', text: e.message }),
      h('button', { class: 'btn', data: { retry: '1' }, text: t('portal.common.retry') }));
    container.querySelector('[data-retry]').addEventListener('click', () => show().catch(reportError));
  }
}

async function copy(text) {
  await navigator.clipboard.writeText(text);
  toast(t('portal.common.copied'), 'ok');
}

// The locked page says how to get a new link. The token is dropped: it is no good any more.
function lock() {
  clearToken();
  location.replace('/static/locked.html');
}

// /launch?code=… carries a one-time code: trade it for a token, then take the code out of the address bar.
// Returns false when the page is locked (and is on its way to the locked page), 'unsaved' when the browser would
// not store the token.
async function signIn() {
  const code = new URLSearchParams(location.search).get('code');
  if (code === null) return loadToken() ? true : lock();
  let session;
  try {
    session = await api.post('/api/session', { code });
  } catch (e) {
    if (e instanceof ApiError && e.status === 403) return lock();
    throw e;
  }
  const saved = saveToken(session.token);
  history.replaceState(null, '', `/${location.hash}`);
  return saved || 'unsaved';
}

async function boot() {
  setErrorHandler(reportError);
  setUnauthorizedHandler(lock);   // 401: this server run does not know the token (it was restarted)
  const signedIn = await signIn();
  if (!signedIn) return;
  const s = await loadState(true);
  await loadMessages(s.locale);
  if (signedIn === 'unsaved') toast(t('portal.common.no_storage'), 'warn');
  document.addEventListener('click', (ev) => {
    const el = ev.target instanceof Element ? ev.target.closest('[data-copy]') : null;
    if (el) copy(el.dataset.copy).catch(reportError);
  });
  initShell();
  window.addEventListener('hashchange', () => show().catch(reportError));
  on('data_dir_recreated', (data) => {   // the data folder was deleted under the running portal: what it showed is gone
    toast(data.message, 'warn', 30000);
    show().catch(reportError);
  });
  startEvents();
  await show().catch(reportError);
}

boot().catch((e) => {
  console.error(e);
  document.getElementById('main').textContent = `Disk Steward could not start: ${e.message}`;
});
