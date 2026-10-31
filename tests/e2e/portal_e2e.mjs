// Browser E2E for the portal. Dev only: not packaged, not part of the unittest run.
// Drives a real Chromium against scripts/dev_portal_fixture.py (a FakeHome with a fake macOS), so nothing here can
// touch the real home. Run it with tests/e2e/run.sh.
import { execFileSync, spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../..');
const python = process.env.E2E_PYTHON || 'python3';
const outDir = process.env.E2E_OUT || fs.mkdtempSync(path.join(os.tmpdir(), 'ds-e2e-'));
fs.mkdirSync(outDir, { recursive: true });

// -- finding Playwright and a Chromium build ---------------------------------------------------------------

function findPlaywright() {
  const dirs = [process.env.PLAYWRIGHT_NODE_MODULES, ...(process.env.NODE_PATH || '').split(path.delimiter)];
  const npx = path.join(os.homedir(), '.npm', '_npx');
  if (fs.existsSync(npx)) for (const d of fs.readdirSync(npx)) dirs.push(path.join(npx, d, 'node_modules'));
  for (const d of dirs) {
    if (d && fs.existsSync(path.join(d, 'playwright', 'package.json'))) return d;
  }
  return null;
}

function findChromium() {
  const root = process.env.PLAYWRIGHT_BROWSERS_PATH || path.join(os.homedir(), 'Library', 'Caches', 'ms-playwright');
  if (!fs.existsSync(root)) return null;
  const builds = fs.readdirSync(root).filter((n) => /^chromium_headless_shell-\d+$/.test(n)).sort();
  for (const b of builds.reverse()) {
    const dir = path.join(root, b);
    for (const sub of fs.readdirSync(dir)) {
      const exe = path.join(dir, sub, 'chrome-headless-shell');
      if (fs.existsSync(exe)) return exe;
    }
  }
  return null;
}

const modules = findPlaywright();
if (!modules) {
  console.log('SKIPPED: Playwright is not installed (set PLAYWRIGHT_NODE_MODULES or run `npx -y playwright@1.61.1`)');
  process.exit(0);
}
const { chromium } = createRequire(path.join(modules, 'x.js'))('playwright');
let browser;
try {
  browser = await chromium.launch();
} catch (e) {
  // The cached Chromium can be a build other than the one this Playwright wants; any headless shell will do.
  const exe = findChromium();
  if (!exe) {
    console.log(`SKIPPED: no Chromium available (${String(e.message).split('\n')[0]})`);
    process.exit(0);
  }
  browser = await chromium.launch({ executablePath: exe });
}

// -- tiny test harness --------------------------------------------------------------------------------------

const results = [];
let currentStep = '';
function ok(cond, message) {
  if (!cond) throw new Error(`${currentStep}: ${message}`);
}
function eq(actual, expected, message) {
  if (actual !== expected) throw new Error(`${currentStep}: ${message}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
}
async function step(name, fn) {
  currentStep = name;
  const started = Date.now();
  try {
    await fn();
    results.push({ name, ok: true, ms: Date.now() - started });
    console.log(`ok   ${name}`);
  } catch (e) {
    results.push({ name, ok: false, error: e.message });
    console.log(`FAIL ${name}\n     ${e.message.split('\n').join('\n     ')}`);
    for (const [label, p] of livePages) {   // what the user would have seen
      try {
        console.log(`     [${label}] was at ${p.url()}`);
        await p.screenshot({ path: path.join(outDir, `FAILED-${label}.png`) });
      } catch (err) {
        // the page is already gone
      }
    }
    throw e;
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// -- fixture process -----------------------------------------------------------------------------------------

const fixtures = [];   // every fixture started, so a failing run can still stop them all

// Fake `brew` and `ollama` on the fixture's PATH, so that "Run" never starts a real tool. They clean the fixture's own
// home (its path is written to a file once the fixture is up), print a few lines slowly, and `ollama rm` waits while a
// flag file exists, so a test can cancel it.
const toolsDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ds-e2e-tools-'));
const tools = { bin: path.join(toolsDir, 'bin'), empty: path.join(toolsDir, 'empty'), homeFile: path.join(toolsDir, 'home.txt'), slowFile: path.join(toolsDir, 'slow') };
fs.mkdirSync(tools.bin);
fs.mkdirSync(tools.empty);
fs.writeFileSync(path.join(tools.bin, 'brew'), `#!/usr/bin/env python3
import os, sys, time, shutil
a = sys.argv[1:]
if a == ['--version']:
    print('Homebrew 9.9.9'); sys.exit(0)
home = open(os.environ['DS_FAKE_HOME_FILE']).read().strip()
if a == ['cleanup', '--prune=all']:
    for i in (1, 2, 3):
        print('Removing: downloads/step%d' % i, flush=True); time.sleep(0.6)
    shutil.rmtree(os.path.join(home, 'Library/Caches/Homebrew/downloads'), ignore_errors=True)
    print('==> This operation has freed approximately 296KB of disk space.', flush=True)
    sys.exit(0)
sys.exit(2)
`);
fs.writeFileSync(path.join(tools.bin, 'ollama'), `#!/usr/bin/env python3
import os, sys, time
a = sys.argv[1:]
if a == ['--version']:
    print('ollama version is 0.9.9'); sys.exit(0)
home = open(os.environ['DS_FAKE_HOME_FILE']).read().strip()
blob = os.path.join(home, '.ollama/models/blobs/sha256-seed')
if a == ['list']:
    print('NAME            ID              SIZE      MODIFIED')
    if os.path.exists(blob):
        print('tiny:latest     abc123def456    396 KB    3 days ago')
    sys.exit(0)
if len(a) == 2 and a[0] == 'rm':
    print("deleting '%s'" % a[1], flush=True)
    while os.path.exists(os.environ['DS_SLOW_FILE']):
        time.sleep(0.2)
    if os.path.exists(blob):
        os.remove(blob)
    print("deleted '%s'" % a[1], flush=True)
    sys.exit(0)
sys.exit(2)
`);
for (const name of ['brew', 'ollama']) fs.chmodSync(path.join(tools.bin, name), 0o755);
const pythonExe = execFileSync('which', [python]).toString().trim();

// withTools: false puts a PATH with no brew and no ollama at all (the "cannot run" rows)
function startFixture(args = [], { withTools = true } = {}) {
  return new Promise((resolve, reject) => {
    const PATH = withTools ? `${tools.bin}:${process.env.PATH}` : `${tools.empty}:/usr/bin:/bin:/usr/sbin:/sbin`;
    const child = spawn(pythonExe, [path.join(repo, 'scripts', 'dev_portal_fixture.py'), ...args], { stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, PATH, DS_FAKE_HOME_FILE: tools.homeFile, DS_SLOW_FILE: tools.slowFile } });
    let buf = '';
    let err = '';
    child.stderr.on('data', (d) => { err += d; });
    child.stdout.on('data', (d) => {
      buf += d;
      const line = buf.split('\n')[0];
      if (buf.includes('\n')) {
        try {
          const info = JSON.parse(line);
          fs.writeFileSync(tools.homeFile, info.home);
          const fx = { ...info, child, base: new URL(info.url).origin, stderr: () => err };
          fixtures.push(fx);
          resolve(fx);
        } catch (e) {
          reject(new Error(`fixture printed ${line}`));
        }
      }
    });
    child.on('exit', (code) => reject(new Error(`fixture exited early (${code}): ${err}`)));
  });
}

async function stopFixture(fx) {
  fx.child.removeAllListeners('exit');
  const exited = new Promise((r) => fx.child.once('exit', r));
  fx.child.kill('SIGTERM');
  const code = await exited;
  eq(code, 0, 'the fixture shuts down cleanly on SIGTERM');
  ok(!fs.existsSync(path.join(fx.data_dir, 'portal.json')), 'portal.json is removed at shutdown');
}

// -- page helpers -------------------------------------------------------------------------------------------

const consoleErrors = [];
let expectedHttpErrors = 0;   // deliberate 4xx answers that Chromium logs as console errors

function watch(page, label) {
  page.on('console', (m) => {
    if (m.type() === 'error' || m.type() === 'warning') consoleErrors.push(`[${label}] ${m.type()}: ${m.text()}`);
  });
  page.on('pageerror', (e) => consoleErrors.push(`[${label}] pageerror: ${e.message}`));
}

const livePages = new Map();

async function newPage(context, label) {
  const page = await context.newPage();
  livePages.set(label, page);
  watch(page, label);
  await page.addInitScript(() => {
    document.addEventListener('securitypolicyviolation', (e) => console.error(`CSP violation: ${e.violatedDirective} ${e.blockedURI}`));
  });
  return page;
}

const tid = (page, id) => page.locator(`[data-testid="${id}"]`);
const groupCard = (page, key) => page.locator(`[data-testid="group"][data-key="${key}"]`);
const itemRow = (page, p) => page.locator(`[data-testid="item"][data-path="${p}"]`);
const entryRow = (page, p) => page.locator(`[data-testid="entry"][data-path="${p}"]`);

async function api(page, method, url, body) {
  return page.evaluate(async ([m, u, b]) => {
    const init = { method: m, credentials: 'omit', headers: { 'X-Disk-Steward-Token': localStorage.getItem(`disk-steward-token:${location.port}`) } };
    if (m !== 'GET') {
      init.headers['Content-Type'] = 'application/json';
      init.headers['X-Disk-Steward'] = '1';
      init.body = JSON.stringify(b || {});
    }
    const res = await fetch(u, init);
    return { status: res.status, body: await res.json() };
  }, [method, url, body]);
}

async function fmtBytes(page, n) {
  return page.evaluate(async (v) => (await import('/static/format.mjs')).fmtBytes(v), n);
}

async function go(page, base, route) {
  await page.goto(`${base}/#/${route}`);
  await page.waitForSelector('main .view');
}

// "Start cleaning" asks first. The dialog says what is deleted for good and what can come back; this answers yes.
async function startRun(page) {
  await tid(page, 'confirm-run').click();
  await page.locator('dialog[open]').waitFor();
  await tid(page, 'confirm-run-yes').click();
}

async function scan(page) {
  await page.getByTestId('scan-button').click();
  await page.waitForSelector('[data-testid="last-scan"]', { timeout: 60000 });
}

// Scan again and wait until the new scan is in and the job is over.
async function rescan(page) {
  // another scan (the one a finished tool run starts by itself) may still be coming or going: wait until the server has been
  // idle, with the same latest scan, for a second and a half, or its end would pass for ours
  await page.waitForFunction(async () => {
    const ask = async () => (await (await fetch('/api/state', { headers: { 'X-Disk-Steward-Token': localStorage.getItem(`disk-steward-token:${location.port}`) } })).json());
    const first = await ask();
    if (first.job.status === 'running') return false;
    await new Promise((r) => setTimeout(r, 1500));
    const second = await ask();
    return second.job.status !== 'running' && second.scan.id === first.scan.id;
  }, null, { timeout: 90000, polling: 500 });
  // our scan is the one whose `scan.done` comes after the click: count them
  const finished = () => page.evaluate(async () => {
    const res = await fetch('/api/events?since=0&timeout=0', { headers: { 'X-Disk-Steward-Token': localStorage.getItem(`disk-steward-token:${location.port}`) } });
    return (await res.json()).events.filter((e) => e.type === 'scan.done').length;
  });
  const before = await finished();
  await page.getByTestId('scan-button').click();
  await page.waitForFunction(async (n) => {
    const headers = { 'X-Disk-Steward-Token': localStorage.getItem(`disk-steward-token:${location.port}`) };
    const events = (await (await fetch('/api/events?since=0&timeout=0', { headers })).json()).events;
    const state = await (await fetch('/api/state', { headers })).json();
    return events.filter((e) => e.type === 'scan.done').length > n && state.job.status !== 'running';
  }, before, { timeout: 60000 });
}

const GROUP_LOGS = 'app.logs:~/Library/Logs/Acme';
const SESSIONS = 'claude.projects:~/.claude/projects/-Users-me-proj';
const rawKey = /\b(portal|rule|reason|error|cli|insights)\.[a-z_]/;

// -- scenarios ---------------------------------------------------------------------------------------------------

let failure = null;
const shots = [];
try {
  // ---------------------------------------------------------------- flow A: pause, exclude, archive, language
  const A = await startFixture(['--slow', '0.35', '--no-history']);
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const page = await newPage(context, 'A');
  const home = A.home;
  const file = (rel) => path.join(home, rel);
  let planId = '';
  let excludedInPreview = '';

  await step('launch code is traded for a token, once; no cookie; reload keeps the session', async () => {
    await page.goto(A.url);
    await page.waitForSelector('#nav a');
    await page.waitForFunction(() => !location.search && location.pathname === '/');
    ok(!page.url().includes('code='), 'the one-time code was taken out of the address bar');
    eq((await context.cookies()).length, 0, 'the portal sets no cookie');
    const token = await page.evaluate((port) => localStorage.getItem(`disk-steward-token:${port}`), new URL(A.base).port);
    ok(token && token.length >= 40, 'the token is kept in this origin\'s storage, under a key with the port');
    await page.reload();
    await page.waitForSelector('#nav a');
    // cookies of other apps on 127.0.0.1 ride along to every port: they change nothing, and an old ds_session is removed
    await context.addCookies([{ name: 'ds_session', value: 'old-session', url: A.base }, { name: 'other-app', value: '1', url: A.base }]);
    await page.reload();
    await page.waitForSelector('#nav a');
    const names = (await context.cookies()).map((c) => c.name).sort();
    eq(names.join(), 'other-app', 'the legacy cookie is removed, the other app\'s is left alone');
    // a second browser with the same one-time code, and one with nothing, get the locked page; the API refuses them
    const other = await browser.newContext();
    const locked = await other.newPage();
    await locked.goto(A.url);
    await locked.waitForURL(/locked\.html/);
    ok((await locked.locator('body').innerText()).includes('disk-steward portal --url'), 'a used code leads to the locked page');
    await locked.goto(`${A.base}/`);
    await locked.waitForURL(/locked\.html/);
    ok((await locked.locator('body').innerText()).includes('disk-steward portal --url'), '/ without a token is the locked page');
    eq((await other.request.get(`${A.base}/api/state`)).status(), 401, 'the API refuses a browser without the token');
    eq((await other.request.get(`${A.base}/api/state`, { headers: { Cookie: 'ds_session=old-session' } })).status(), 401, 'a cookie is no credential');
    await other.close();
  });

  const fmt = (n) => fmtBytes(page, n);
  const count = (n) => page.evaluate(async (v) => (await import('/static/format.mjs')).fmtCount(v), n);

  await step('overview: the first scan runs in the background; the hero says what can be cleaned and the rows agree', async () => {
    await go(page, A.base, 'overview');
    await page.getByTestId('scan-button').waitFor();
    await scan(page);
    ok((await tid(page, 'last-scan').innerText()).includes('Scan #1'), 'scan #1 is shown');
    ok(await tid(page, 'tier-junk').isVisible(), 'tier totals are shown');
    ok(await tid(page, 'volume').isVisible(), 'volume accounting is shown (the Disk & areas tab opens first)');
    ok((await tid(page, 'volume').innerText()).includes('1 local snapshot exists. Removed space may not show as free until it ages out.'), 'one snapshot is said in the singular');
    ok(!(await page.locator('main').innerText()).includes('snapshot(s)'), 'no "snapshot(s)" anywhere');
    const quick = (await api(page, 'GET', '/api/quick')).body;
    eq(quick.summary.count, 12, 'twelve items are safe to clean in the fixture');
    eq(await tid(page, 'hero-size').innerText(), await fmt(quick.summary.alloc), 'the hero shows the server size');
    eq(await tid(page, 'hero-items').innerText(), '12 items', 'and the server count');
    ok((await tid(page, 'hero-button').innerText()).includes(await fmt(quick.summary.alloc)), 'the button carries the same size');
    await tid(page, 'tab-why').click();
    ok(await tid(page, 'why').isVisible(), 'the "where the space goes" table is shown on its tab');
    const row = await tid(page, 'row-offered').innerText();
    ok(row.includes('12') && row.includes(await fmt(quick.summary.alloc)), `the "safe to clean" row says the same: ${row}`);
    ok(/more junk is newer than 14 days/.test(await tid(page, 'hero-gap').innerText()), 'one line says why there is more');
    ok(await tid(page, 'hero-review').isVisible(), 'and what needs a decision');
  });

  await step('overview: the age control updates the hero from the server', async () => {
    const before = await tid(page, 'hero-items').innerText();
    await tid(page, 'hero-days').fill('3');
    await page.waitForFunction((was) => document.querySelector('[data-testid="hero-items"]').textContent !== was, before, { timeout: 15000 });
    const at3 = (await api(page, 'GET', '/api/quick?days=3')).body;
    eq(await tid(page, 'hero-size').innerText(), await fmt(at3.summary.alloc), 'size at 3 days');
    eq(await tid(page, 'hero-items').innerText(), `${at3.summary.count} items`, 'count at 3 days');
    eq(await tid(page, 'hero-days').inputValue(), '3', 'the input keeps what was typed');
    await tid(page, 'hero-days').fill('14');
    await page.waitForFunction((was) => document.querySelector('[data-testid="hero-items"]').textContent === was, before, { timeout: 15000 });
  });

  await step('overview: every figure is a link to exactly the items behind it', async () => {
    const ov = (await api(page, 'GET', '/api/overview')).body;
    const header = (text) => page.waitForFunction((want) => document.querySelector('[data-testid="browse-header"]')?.textContent === want, text, { timeout: 15000 });
    const line = async (title, b) => `${title} · ${await count(b.items)} items · ${await fmt(b.alloc)}`;
    const back = async (tab = 'space') => { await go(page, A.base, `overview?tab=${tab}`); await tid(page, 'hero').waitFor(); };
    // a tier card
    const junk = ov.totals.by_tier.junk;
    await tid(page, 'tier-junk').click();
    await page.waitForURL(/#\/browse\?tier=junk/);
    await header(await line('Junk', junk));
    eq(await page.locator('[data-testid="group"]').count(), junk.groups, 'the tier card\'s groups are listed');
    // a row of "where the space goes"
    await back('why');
    await tid(page, 'row-too_recent').getByRole('link').first().click();
    await page.waitForURL(/state=too_recent/);
    const tooRecent = ov.not_included.too_recent;
    await header(await line('Junk newer than 14 days', tooRecent));
    ok(await tid(page, 'chips').isVisible(), 'the filter shows as chips');
    eq(await page.locator('[data-testid="group"] input[type=checkbox]').count(), 0, 'a preview has nothing to tick');
    await page.locator('.group-title').first().click();
    await page.locator('[data-testid="item"]').first().waitFor();
    ok((await page.locator('[data-testid="item"]').first().innerText()).includes('Too recent'), 'its items say why they are not offered');
    await tid(page, 'clear-filters').click();
    await page.waitForURL(/#\/browse$/);
    await page.waitForFunction(() => /^Everything the scan found/.test(document.querySelector('[data-testid="browse-header"]')?.textContent || ''));
    // the review tier and a protected reason
    await back('why');
    await tid(page, 'row-review').getByRole('link').first().click();
    await header(await line('Needs your decision', ov.not_included.review));
    await back('why');
    await tid(page, 'row-reason-databases').getByRole('link').first().click();
    await page.waitForURL(/reason=databases/);
    await header(await line('Protected: Databases', ov.not_included.protected.by_reason.databases));
    // the "safe to clean" row is what the hero button cleans
    await back('why');
    await tid(page, 'row-offered').getByRole('link').first().click();
    await page.waitForURL(/state=selected/);
    await header(await line('Safe to clean (at least 14 days old)', ov.offered));
    ok(await tid(page, 'review-clean').isEnabled(), 'and the same button is on that page');
    // a group of the largest-groups table
    await back('groups');
    const firstGroup = tid(page, 'top-group').first();
    const key = await firstGroup.getAttribute('data-key');
    await firstGroup.getByRole('link').first().click();
    await page.waitForURL(/group=/);
    await groupCard(page, key).locator('[data-testid="items"]').waitFor();
    ok(true, `the group ${key} is open`);
  });

  await step('browse: junk + 14-day filter selects the 16-day file; the 9-day file is "too recent"', async () => {
    await go(page, A.base, 'browse');
    await tid(page, 'summary-toggle').click();   // below 1320px the selection is a bar at the foot, with its details folded
    await tid(page, 'select-junk').waitFor();
    await tid(page, 'select-junk').click();
    await page.waitForFunction(() => !document.querySelector('[data-testid="summary-count"]').textContent.startsWith('0 items'));   // the panel is redrawn when the server answers: type after that
    await tid(page, 'age-filter').fill('14');
    await tid(page, 'age-filter').press('Tab');
    await page.waitForFunction(() => document.querySelector('[data-testid="age-filter"]').value === '14');
    const card = groupCard(page, GROUP_LOGS);
    await card.locator('.group-title').click();
    await itemRow(page, '~/Library/Logs/Acme/old0.log').waitFor();
    eq(await itemRow(page, '~/Library/Logs/Acme/old0.log').getAttribute('data-state'), 'selected', '16-day file state');
    ok(await itemRow(page, '~/Library/Logs/Acme/old0.log').locator('input[type=checkbox]').isChecked(), '16-day file is checked');
    const recent = itemRow(page, '~/Library/Logs/Acme/recent.log');
    eq(await recent.getAttribute('data-state'), 'too_recent', '9-day file state');
    ok(await recent.locator('input[type=checkbox]').isDisabled(), '9-day file checkbox is disabled');
    ok(!(await recent.locator('input[type=checkbox]').isChecked()), '9-day file is unchecked');
    ok((await recent.innerText()).includes('Too recent'), 'the badge says Too recent');
    ok((await recent.innerText()).includes('newer than the 14-day threshold'), "the server's reason text is shown");
    ok((await recent.getByRole('button', { name: 'Include anyway' }).count()) === 1, 'Include anyway is offered');
    ok((await card.innerText()).includes('4 of 5 qualify'), 'the group shows "N of M qualify"');
    const folded = tid(page, 'none-qualify');
    ok(/^\d+ selected groups have nothing that qualifies right now$/.test(await folded.locator('summary').innerText()), 'one line for the groups with nothing to offer');
    ok(!(await folded.locator('li').first().isVisible()), 'the list behind it is folded');
    await folded.locator('summary').click();
    ok(await folded.locator('li').first().isVisible(), 'and opens on request');
    await folded.locator('summary').click();
  });

  await step('browse: the summary equals the server, and Include anyway selects a too-recent item', async () => {
    const spec = (await api(page, 'GET', '/api/spec')).body;
    const text = await tid(page, 'summary-count').innerText();
    eq(text, `${spec.summary.count} items selected · ${await fmtBytes(page, spec.summary.alloc)}`, 'summary text equals the server summary');
    eq(spec.summary.count, 12, 'twelve entries qualify in the fixture');
    ok((await tid(page, 'action-delete').innerText()).startsWith('Delete 12:'), 'per-action line');
    await itemRow(page, '~/Library/Logs/Acme/recent.log').getByRole('button', { name: 'Include anyway' }).click();
    await page.waitForFunction(() => document.querySelector('[data-testid="summary-count"]').textContent.startsWith('13 items'));
    eq(await itemRow(page, '~/Library/Logs/Acme/recent.log').getAttribute('data-state'), 'selected', 'Include anyway selects it');
    eq((await api(page, 'GET', '/api/spec')).body.summary.count, 13, 'the server counts it');
    // Unchecking it again leaves it out of the plan; the later steps work with the original twelve.
    await itemRow(page, '~/Library/Logs/Acme/recent.log').locator('input[type=checkbox]').click();
    await page.waitForFunction(() => document.querySelector('[data-testid="summary-count"]').textContent.startsWith('12 items'));
    eq((await api(page, 'GET', '/api/spec')).body.summary.count, 12, 'back to twelve');
  });

  await step('browse: a plan from a stale view is refused with the new summary to confirm', async () => {
    const page2 = await newPage(context, 'A2');
    await page2.goto(`${A.base}/#/overview`);
    await page2.waitForSelector('#nav a');
    await api(page2, 'POST', '/api/spec/ops', { ops: [{ op: 'set_filter', min_age_days: 20 }] });
    expectedHttpErrors += 1;   // the 409 below is logged by Chromium as a console error
    await tid(page, 'create-plan').click();
    await page.waitForSelector('dialog[open]');
    const dialog = await page.locator('dialog').innerText();
    ok(dialog.includes('The selection changed'), 'the dialog says the selection changed');
    ok(dialog.includes('Create a cleanup list with the new selection'), 'the user must confirm the new summary');
    await page.getByRole('button', { name: 'Cancel' }).click();
    await page2.close();
    await tid(page, 'age-filter').fill('14');
    await tid(page, 'age-filter').press('Tab');
    await page.waitForFunction(() => document.querySelector('[data-testid="age-filter"]').value === '14');
    await page.waitForFunction(() => document.querySelector('[data-testid="summary-count"]').textContent.startsWith('12 items'));
  });

  await step('overview: the hero button opens a cleanup list with exactly the same numbers', async () => {
    await go(page, A.base, 'overview');
    await tid(page, 'hero').waitFor();
    const size = await tid(page, 'hero-size').innerText();
    const items = (await tid(page, 'hero-items').innerText()).split(' ')[0];
    await tid(page, 'hero-button').click();
    await page.waitForURL(/#\/plan\//);
    planId = page.url().split('/plan/')[1];
    await tid(page, 'plan-head').waitFor();
    eq(await tid(page, 'plan-head').innerText(), `Cleanup list · ${items} items · ${size}`, 'the header has the hero\'s numbers');
    eq(await page.locator('#nav a[aria-current]').count(), 0, 'no tab claims a plan page');
    await page.waitForSelector('[data-testid="preflight"]');
    ok(!(await tid(page, 'blockers').count()), 'nothing blocks the list when no app is running');
    eq(await tid(page, 'confirm-run').innerText(), `Start cleaning ${items} items · ${size}`, 'the button repeats them');
    ok(await tid(page, 'confirm-run').isEnabled(), 'enabled once the preflight is in');
    ok(!(await page.locator('main').innerText()).includes('nothing that qualifies'), 'no "nothing qualifies" warnings on a list');
    ok((await page.locator('.warnings').first().innerText()).includes('1 local snapshot exists.'), 'the list\'s warning says one snapshot in the singular');
    ok(!(await page.locator('main').innerText()).includes('snapshot(s)'), 'and no "snapshot(s)"');
    eq(await tid(page, 'compacted-note').count(), 0, 'a list that is not finished has no "detail was removed" note');
    const groups = (await api(page, 'GET', `/api/plans/${planId}/groups`)).body.groups;
    eq(await tid(page, 'plan-group').count(), groups.length, 'entries are grouped');
    eq(groups.reduce((n, g) => n + g.entries, 0), 12, 'every entry is in a group');
    ok((await tid(page, 'plan-totals').innerText()).includes('Delete 12:'), 'per-action totals');
  });

  await step('plan: leaving out a whole group or one entry updates header and button; putting it back restores them', async () => {
    const plan = (key) => page.locator(`[data-testid="plan-group"][data-key="${key}"]`);
    const total = (await api(page, 'GET', `/api/plans/${planId}`)).body;
    const head = (n) => page.waitForFunction((want) => document.querySelector('[data-testid="plan-head"]').textContent.startsWith(want), `Cleanup list · ${n} item`, { timeout: 15000 });
    await plan(GROUP_LOGS).locator('.group-head input[type=checkbox]').uncheck();
    await head(8);
    eq(await plan(GROUP_LOGS).getByTestId('plan-group-stats').innerText(), 'Left out (4)', 'the group says it is left out');
    ok(!(await plan(GROUP_LOGS).locator('.group-head input[type=checkbox]').isChecked()), 'its box is empty');
    await page.waitForFunction(() => /^Start cleaning 8 items/.test(document.querySelector('[data-testid="confirm-run"]')?.textContent || ''));
    const pend = (await api(page, 'GET', `/api/plans/${planId}`)).body;
    eq(pend.pending.count, 8, 'the server agrees');
    eq(await tid(page, 'confirm-run').innerText(), `Start cleaning 8 items · ${await fmt(pend.pending.alloc)}`, 'size follows too');
    await plan(GROUP_LOGS).locator('.group-head input[type=checkbox]').check();   // and back
    await head(12);
    eq(await tid(page, 'confirm-run').innerText(), `Start cleaning 12 items · ${await fmt(total.pending.alloc)}`, 'back to the start');
    await plan(GROUP_LOGS).locator('.group-head input[type=checkbox]').uncheck();
    await head(8);
    // one entry of another group
    const caches = 'app.caches:~/Library/Caches/com.acme.Tool';
    await plan(caches).locator('.group-title').click();
    excludedInPreview = '~/Library/Caches/com.acme.Tool/blob1';
    await entryRow(page, excludedInPreview).locator('input[type=checkbox]').uncheck();
    await head(7);
    eq(await plan(caches).getByTestId('plan-group-stats').innerText().then((x) => x.startsWith('1 of 2 included')), true, 'a partly included group says so');
    ok(await plan(caches).locator('.group-head input[type=checkbox]').evaluate((el) => el.indeterminate), 'and its box is half-ticked');
    ok((await entryRow(page, excludedInPreview).innerText()).includes('Excluded'), 'the entry is marked Excluded');
    eq((await entryRow(page, excludedInPreview).locator('td.c-check input').isChecked()), false, 'its own box is empty');
    const after = (await api(page, 'GET', `/api/plans/${planId}`)).body;
    eq(after.pending.count, 7, 'seven entries are pending');
    eq(after.entries_digest, total.entries_digest, 'leaving things out never changes what the list is');
  });

  let doneBeforePause = 0;
  let excludedAtPause = [];
  await step('plan: starting asks first, with the count, the size and what can be recovered; Cancel changes nothing', async () => {
    const pend = (await api(page, 'GET', `/api/plans/${planId}`)).body.pending;
    await tid(page, 'confirm-run').click();
    const dialog = page.locator('dialog[open]');
    await dialog.waitFor();
    const title = await dialog.locator('h2').innerText();
    eq(title, `Permanently delete ${pend.count} items · ${await fmt(pend.alloc)}`, 'the title carries the count and the size');
    const lines = await tid(page, 'confirm-lines').innerText();
    ok(lines.includes('cannot be undone') && lines.includes(await fmt(pend.by_action.delete.alloc)), `what is permanent is said: ${lines}`);
    eq(await tid(page, 'confirm-lines').locator('[data-action="delete"] svg').count(), 1, 'with an icon, not only words');
    ok((await tid(page, 'confirm-run-yes').innerText()).includes('Delete permanently'), 'the button says what it does');
    await page.getByRole('button', { name: 'Cancel' }).click();
    await page.waitForFunction(() => !document.querySelector('dialog[open]'));
    eq((await api(page, 'GET', `/api/plans/${planId}`)).body.state, 'draft', 'cancelling starts nothing');
    ok(await tid(page, 'confirm-run').isEnabled(), 'and the list can still be started');
  });

  await step('run: pause, exclude what is left, resume; the result is read on the same page', async () => {
    await startRun(page);
    await tid(page, 'pause').waitFor({ timeout: 15000 });
    await page.waitForFunction(() => /\b[1-9]\d* of 7 items/.test(document.querySelector('[data-testid="run-progress"]')?.textContent || ''), null, { timeout: 15000 });
    await tid(page, 'pause').click();
    await page.waitForSelector('[data-testid="resume"]', { timeout: 15000 });
    ok((await tid(page, 'run-status').innerText()).includes('Paused'), 'the run shows as paused');
    await tid(page, 'pending-panel').waitFor();
    const remaining = await tid(page, 'pending-entry').evaluateAll((els) => els.map((e) => e.dataset.path));
    ok(remaining.length >= 1 && remaining.length < 7, `some entries are pending (${remaining.length})`);
    excludedAtPause = remaining;
    doneBeforePause = 7 - remaining.length;
    await tid(page, 'exclude-all').click();
    await page.waitForFunction(() => document.querySelector('[data-testid="pending-panel"]') === null || /\(0\)/.test(document.querySelector('[data-testid="pending-panel"] h2').textContent));
    await tid(page, 'resume').click();
    await page.waitForURL(/#\/plan\//, { timeout: 30000 });   // a finished run is read where it was prepared
    await tid(page, 'result').waitFor({ timeout: 30000 });
    eq(page.url().split('/plan/')[1], planId, 'the same page');
    const plan = (await api(page, 'GET', `/api/plans/${planId}`)).body;
    eq(await tid(page, 'result-cleaned').innerText(), `Cleaned ${doneBeforePause} ${doneBeforePause === 1 ? 'item' : 'items'} · ${await fmt(plan.result.removed_alloc)} (estimated ${await fmt(plan.result.freed)})`, 'what was cleaned, and what it should free');
    ok((await tid(page, 'result-freed').innerText()).includes('Actually freed'), 'measured against estimated');
    ok((await tid(page, 'result').innerText()).includes('1 local snapshot exists.'), 'the snapshot warning is shown, in the singular');
    eq(await tid(page, 'compacted-note').count(), 0, 'a run that just finished keeps its per-item detail, so there is no note');
    eq(await tid(page, 'result-skipped').count(), 0, 'nothing was skipped');
    ok((await tid(page, 'result').innerText()).includes(`Left out by you: ${5 + remaining.length} items`), 'what the user left out is accounted for');
    eq(await tid(page, 'confirm-run').count(), 0, 'a finished list has no start button');
    eq(plan.result.counts.done, doneBeforePause, 'done count');
  });

  await step('disk: exactly the finished entries are gone; excluded ones remain', async () => {
    const entries = (await api(page, 'GET', `/api/plans/${planId}/entries?limit=1000`)).body.rows;
    eq(entries.length, 12, 'twelve entries');
    const abs = (logical) => logical.replace('~', home).replace(/^\$TMPDIR/, '').replace(/^\$CACHEDIR/, '');
    let done = 0;
    for (const e of entries) {
      const p = abs(e.path);
      if (e.state === 'done') { done += 1; ok(!fs.existsSync(p), `${e.path} was removed`); }
      else { eq(e.state, 'excluded', `${e.path} state`); ok(fs.existsSync(p), `${e.path} must still exist`); }
    }
    eq(done, doneBeforePause, 'number removed');
    for (const i of [0, 1, 2, 3]) ok(fs.existsSync(file(`Library/Logs/Acme/old${i}.log`)), 'the group left out in the list remains');
    ok(fs.existsSync(file('Library/Caches/com.acme.Tool/blob1')), 'the entry left out in the list remains');
    ok(excludedAtPause.every((e) => fs.existsSync(abs(e))), 'everything excluded during the pause remains');
    ok(fs.existsSync(file('Library/Logs/Acme/recent.log')), 'the too-recent file remains');
  });

  await step('overview after the run: the banner says what was freed; the numbers are fresh; it can be dismissed', async () => {
    await go(page, A.base, 'overview');
    await page.waitForFunction(() => /Scan #2/.test(document.querySelector('[data-testid="last-scan"]')?.textContent || ''), null, { timeout: 30000 });
    await tid(page, 'tab-changes').click();
    ok(await tid(page, 'growth').isVisible(), 'growth since the last scan is shown on the Changes tab');
    const plan = (await api(page, 'GET', `/api/plans/${planId}`)).body;
    const banner = await tid(page, 'last-cleanup').innerText();
    ok(banner.includes(`Last cleanup: freed ${await fmt(plan.result.freed)} (cleanup list ${await fmt(plan.totals.alloc)})`), `banner: ${banner}`);
    const quick = (await api(page, 'GET', '/api/quick')).body;
    eq(await tid(page, 'hero-size').innerText(), await fmt(quick.summary.alloc), 'the hero is judged on the new scan');
    ok(quick.summary.count > 0, 'what was left out is offered again');
    await tid(page, 'last-cleanup').getByRole('link', { name: 'Details' }).click();
    await page.waitForURL(new RegExp(`#/plan/${planId}`));
    await go(page, A.base, 'overview');
    await tid(page, 'last-cleanup').getByRole('button', { name: 'Dismiss' }).click();
    eq(await tid(page, 'last-cleanup').count(), 0, 'dismissed');
    await page.reload();
    await tid(page, 'hero').waitFor();
    eq(await tid(page, 'last-cleanup').count(), 0, 'and it stays dismissed');
  });

  let draftId = '';
  await step('cleanup tab: the finished run, a waiting draft that can be discarded, and a draft a scan replaces', async () => {
    eq(await page.locator('#nav a').nth(3).innerText(), 'Cleanup', 'the tab is called Cleanup');
    await go(page, A.base, 'run');
    await tid(page, 'cleanup-finished').waitFor();
    eq(await tid(page, 'finished-plan').count(), 1, 'the finished run is listed');
    ok((await tid(page, 'cleanup-finished').innerText()).includes(`Cleaned ${doneBeforePause} item`), 'with its result');
    eq(await tid(page, 'cleanup-draft').count(), 0, 'no draft');
    await tid(page, 'finished-plan').getByRole('link').click();
    await page.waitForURL(new RegExp(`#/plan/${planId}`));
    // a draft is waiting
    await go(page, A.base, 'overview');
    await tid(page, 'hero-button').click();
    await page.waitForURL(/#\/plan\//);
    draftId = page.url().split('/plan/')[1];
    await tid(page, 'plan-head').waitFor();
    await go(page, A.base, 'run');
    await tid(page, 'cleanup-draft').waitFor();
    ok((await tid(page, 'cleanup-draft').innerText()).startsWith('A cleanup list is waiting'), 'the draft is offered next');
    await tid(page, 'draft-continue').click();
    await page.waitForURL(new RegExp(`#/plan/${draftId}`));
    await go(page, A.base, 'run');
    await tid(page, 'draft-discard').click();
    await page.waitForFunction(() => !document.querySelector('[data-testid="cleanup-draft"]'));
    const dropped = (await api(page, 'GET', `/api/plans/${draftId}`)).body;
    eq([dropped.state, dropped.error, dropped.entries.total].join(), 'invalid,discarded,0', 'discarded: invalid, and its entries are gone');
    // a draft that a newer scan replaces is never offered
    await go(page, A.base, 'overview');
    await tid(page, 'hero-button').click();
    await page.waitForURL(/#\/plan\//);
    draftId = page.url().split('/plan/')[1];
    await go(page, A.base, 'overview');
    await scan(page);
    await page.waitForFunction(() => /Scan #3/.test(document.querySelector('[data-testid="last-scan"]')?.textContent || ''), null, { timeout: 30000 });
    await go(page, A.base, `plan/${draftId}`);
    await tid(page, 'plan-invalid').waitFor();
    ok((await tid(page, 'plan-invalid').innerText()).includes('A newer scan replaced this list'), 'the old draft says what happened');
    eq(await tid(page, 'confirm-run').count(), 0, 'and cannot be started');
    await go(page, A.base, 'run');
    await tid(page, 'cleanup-finished').waitFor();
    eq(await tid(page, 'cleanup-draft').count(), 0, 'a superseded draft is not the next thing to run');
    // leave a fresh draft for the screenshots
    await go(page, A.base, 'overview');
    await tid(page, 'hero-button').click();
    await page.waitForURL(/#\/plan\//);
    draftId = page.url().split('/plan/')[1];
    await tid(page, 'plan-head').waitFor();
  });

  const original = fs.readFileSync(file('.claude/projects/-Users-me-proj/a.jsonl'));
  await step('archives: the old address lands on the Archives tab of the Cleanup page, which says what archives are', async () => {
    await go(page, A.base, 'archives');
    await page.waitForURL(/#\/run\?tab=archives/);
    eq(await page.locator('#nav a[aria-current="page"]').getAttribute('data-testid'), 'nav-run', 'the Cleanup tab is the current one');
    eq(await page.locator('#nav a').count(), 6, 'there is no Archives tab');
    await tid(page, 'archives-section').waitFor();
    eq(await page.locator('#archives-title').innerText(), 'Restorable archives', 'the tab is named for what it holds');
    const text = await tid(page, 'archives-section').innerText();
    ok(text.includes('compressed copy') && text.includes('Archive action'), `one line says what an archive is: ${text.slice(0, 160)}`);
    ok(await page.evaluate(() => document.activeElement && document.activeElement.id === 'archives-title'), 'the archives heading has the focus');
    ok(await tid(page, 'archives-empty').isVisible(), 'with nothing archived yet, an empty state says when it fills up');
    ok((await tid(page, 'archives-empty').innerText()).includes('Archive action') || (await tid(page, 'archives-empty').innerText()).includes('archives items'), 'it names the Archive action');
  });

  await step('archives: archive one session file, then restore it (never over an existing file)', async () => {
    await go(page, A.base, 'browse');
    await tid(page, 'summary-count').waitFor();
    await tid(page, 'summary-toggle').click();
    await tid(page, 'summary').getByRole('button', { name: 'Clear selection' }).click();
    await page.waitForFunction(() => document.querySelector('[data-testid="summary-count"]').textContent.startsWith('0 items'));
    const card = groupCard(page, SESSIONS);
    await card.locator('.group-title').click();
    const row = itemRow(page, '~/.claude/projects/-Users-me-proj/a.jsonl');
    await row.waitFor();
    await row.locator('input[type=checkbox]').check();
    await page.waitForFunction(() => document.querySelector('[data-testid="summary-count"]').textContent.startsWith('1 item '));
    ok((await tid(page, 'action-archive').innerText()).startsWith('Archive 1:'), 'archive is the default action of a session file');
    await tid(page, 'create-plan').click();
    await page.waitForURL(/#\/plan\//);
    await page.waitForFunction(() => document.querySelector('[data-testid="confirm-run"]')?.disabled === false);
    ok((await tid(page, 'plan-totals').innerText()).includes('frees about'), 'archive frees an estimate');
    await tid(page, 'confirm-run').click();
    await page.locator('dialog[open]').waitFor();
    ok((await page.locator('dialog h2').innerText()).startsWith('Clean 1 item'), 'an archived item is not "permanently deleted"');
    ok((await tid(page, 'confirm-lines').innerText()).includes('Archives page'), 'the dialog says where it can be restored from');
    eq(await tid(page, 'confirm-run-yes').innerText(), 'Start cleaning', 'a recoverable step is the plain button');
    await tid(page, 'confirm-run-yes').click();
    await tid(page, 'result').waitFor({ timeout: 30000 });
    ok(!fs.existsSync(file('.claude/projects/-Users-me-proj/a.jsonl')), 'the archived file left its place');
    await go(page, A.base, 'archives');
    const archived = page.locator('[data-testid="archive"][data-path="~/.claude/projects/-Users-me-proj/a.jsonl"]');
    await archived.waitFor();
    ok((await archived.innerText()).includes('Stored'), 'the archive is stored');
    // something else took its place: restore must refuse and say so
    fs.writeFileSync(file('.claude/projects/-Users-me-proj/a.jsonl'), 'in the way');
    expectedHttpErrors += 1;
    await archived.getByTestId('restore').click();
    await tid(page, 'archive-notice').waitFor();
    ok((await tid(page, 'archive-notice').innerText()).includes('already exists'), "the server's refusal is shown");
    eq(fs.readFileSync(file('.claude/projects/-Users-me-proj/a.jsonl'), 'utf8'), 'in the way', 'nothing was overwritten');
    fs.unlinkSync(file('.claude/projects/-Users-me-proj/a.jsonl'));
    await page.locator('[data-testid="archive"][data-path="~/.claude/projects/-Users-me-proj/a.jsonl"]').getByTestId('restore').click();
    await page.waitForFunction(() => /Restored /.test(document.querySelector('[data-testid="archive-notice"]')?.textContent || ''));
    ok(fs.readFileSync(file('.claude/projects/-Users-me-proj/a.jsonl')).equals(original), 'the file is back, byte for byte');
  });

  await step('archives: purge asks first and removes the archive for good', async () => {
    await go(page, A.base, 'archives');
    const row = page.locator('[data-testid="archive"][data-path="~/.claude/projects/-Users-me-proj/a.jsonl"]');
    await row.getByTestId('purge').click();
    await page.waitForSelector('dialog[open]');
    ok((await page.locator('dialog').innerText()).includes('This cannot be undone'), 'the dialog warns');
    await page.getByRole('button', { name: 'Cancel' }).click();
    eq(await row.getAttribute('data-state'), 'restored', 'cancelling changes nothing');
    await row.getByTestId('purge').click();
    await page.waitForSelector('dialog[open]');
    await page.locator('dialog').getByRole('button', { name: 'Purge' }).click();
    await page.waitForFunction(() => /Purged archive/.test(document.querySelector('[data-testid="archive-notice"]')?.textContent || ''));
    eq(await page.locator('[data-testid="archive"]').first().getAttribute('data-state'), 'purged', 'the archive is purged');
    eq(await page.locator('[data-testid="restore"], [data-testid="purge"]').count(), 0, 'nothing is left to do with it');
  });

  await step('overview: applications are measured on request', async () => {
    await go(page, A.base, 'overview');
    await tid(page, 'measure-apps').click();
    await tid(page, 'apps').waitFor({ timeout: 60000 });
    const apps = await tid(page, 'apps').innerText();
    ok(apps.indexOf('Big.app') > -1 && apps.indexOf('Big.app') < apps.indexOf('Small.app'), 'the biggest app comes first');
    ok(apps.includes('3.00 GiB'), `sizes are shown: ${apps}`);
  });

  await step('advisor: off by default, shows the payload first, annotates groups, never selects', async () => {
    await go(page, A.base, 'advisor');
    ok(!(await tid(page, 'advisor-enabled').isChecked()), 'disabled by default');
    ok((await page.locator('main').innerText()).includes('It is off by default'), 'the explanation is shown');
    await tid(page, 'advisor-enabled').check();
    await tid(page, 'preview-button').click();
    await tid(page, 'payload').waitFor();
    ok((await tid(page, 'payload').innerText()).includes('"groups"'), 'the exact JSON payload is shown');
    ok((await tid(page, 'preview-notice').innerText()).includes('Only the JSON payload shown here comes from Disk Steward'), 'the notice about the programs\' own instructions is shown with the payload');
    ok((await tid(page, 'provider-claude').innerText()).includes('claude was not found on PATH'), 'a probed provider that is missing says why');
    ok((await tid(page, 'provider-codex').innerText()).includes('fake-codex 1.0'), 'a probed provider shows its version');
    ok(await tid(page, 'ask-claude').isDisabled(), 'an unavailable provider cannot be asked');
    await tid(page, 'ask-codex').click();
    await tid(page, 'advice').waitFor({ timeout: 15000 });
    eq(await tid(page, 'tab-results').getAttribute('aria-selected'), 'true', 'the answer opens the Advice tab by itself');
    ok((await tid(page, 'advice').innerText()).includes('Summary <i>from the fake advisor</i>.'), 'model text is shown as plain text');
    eq(await tid(page, 'advice').locator('i, b').count(), 0, 'no markup from the model became elements');
    ok((await tid(page, 'trace').innerText()).includes('~/.claude.json'), 'a trace the program left behind is reported');
    await tid(page, 'tab-ask').click();
    await page.getByRole('button', { name: 'Show the prompt' }).click();
    await page.waitForFunction(() => [...document.querySelectorAll('textarea')].some((t) => t.value.startsWith('PROMPT for req-')));
    await tid(page, 'reply').fill('this is not JSON');
    expectedHttpErrors += 1;
    await page.getByRole('button', { name: 'Import reply' }).click();
    await page.waitForSelector('.banner-danger');
    await tid(page, 'reply').fill('{"ok": true}');
    await page.getByRole('button', { name: 'Import reply' }).click();
    await page.waitForFunction(() => /From manual/.test(document.querySelector('[data-testid="advice"]')?.textContent || ''));
    eq(await tid(page, 'tab-results').getAttribute('aria-selected'), 'true', 'a manual import opens the Advice tab too');
    const before = (await api(page, 'GET', '/api/spec')).body.summary.count;
    await tid(page, 'rec').first().getByRole('link', { name: 'Go to group' }).click();
    await page.waitForSelector('[data-testid="group"]');
    ok((await page.locator('.advice').count()) >= 1, 'groups carry the advisor badge and reason');
    eq((await api(page, 'GET', '/api/spec')).body.summary.count, before, 'advice never changes the selection');
  });

  await step('settings: a threshold edited on one tab is saved from another, and the server holds it', async () => {
    await go(page, A.base, 'settings');
    await tid(page, 'tab-thresholds').click();
    await tid(page, 'set-idle_days').fill('90');
    await tid(page, 'tab-general').click();   // Save is below the panels: it saves the hidden one too
    await tid(page, 'save-settings').click();
    await page.waitForSelector('.toast-ok');
    eq((await api(page, 'GET', '/api/settings')).body.settings.idle_days, 90, 'idle_days');
    ok((await page.locator('.toast').allInnerTexts()).some((t) => t.includes('Scan again')), 'the user is told to scan again');
  });

  await step('sub-tabs: a click shows its panel and writes ?tab=; arrows move the focus; a reload lands on the tab; Settings keeps an unsaved edit', async () => {
    await go(page, A.base, 'overview');
    await tid(page, 'hero').waitFor();
    eq(await tid(page, 'tab-space').getAttribute('aria-selected'), 'true', 'the first tab is open by default');
    eq(await tid(page, 'tab-why').getAttribute('tabindex'), '-1', 'the others are out of the Tab order');
    eq(await page.locator('[role="tabpanel"]').count(), 1, 'one panel is in the page');
    await page.evaluate(() => {   // a tab change is no page load, no hashchange and no history entry
      window.tabMark = 1;
      window.tabHashes = 0;
      window.tabHistory = history.length;
      window.addEventListener('hashchange', () => { window.tabHashes += 1; });
    });
    await tid(page, 'tab-why').click();
    ok(await tid(page, 'why').isVisible(), 'a click shows its panel');
    eq(await page.locator('#tabpanel-why').getAttribute('aria-labelledby'), 'tab-why', 'which its tab labels');
    eq(await tid(page, 'volume').count(), 0, 'and the other panel is gone');
    ok(page.url().endsWith('#/overview?tab=why'), `the address names the tab: ${page.url()}`);
    await tid(page, 'tab-why').focus();
    await page.keyboard.press('ArrowRight');
    eq(await tid(page, 'tab-groups').getAttribute('aria-selected'), 'true', 'ArrowRight opens the next tab');
    ok(await page.evaluate(() => document.activeElement.dataset.testid === 'tab-groups'), 'and the focus follows it');
    ok(await tid(page, 'top-group').first().isVisible(), 'with its panel');
    await page.keyboard.press('ArrowLeft');
    eq(await tid(page, 'tab-why').getAttribute('aria-selected'), 'true', 'ArrowLeft goes back');
    await page.keyboard.press('End');
    eq(await tid(page, 'tab-tools').getAttribute('aria-selected'), 'true', 'End opens the last tab');
    await tid(page, 'native').waitFor();   // the tools load when their tab opens: let that finish before the page is left
    await page.keyboard.press('ArrowRight');
    eq(await tid(page, 'tab-space').getAttribute('aria-selected'), 'true', 'and ArrowRight wraps around to the first');
    ok(await page.evaluate(() => document.activeElement.dataset.testid === 'tab-space'), 'with the focus on it');
    eq(await page.evaluate(() => [window.tabMark, window.tabHashes, history.length === window.tabHistory].join()), '1,0,true', 'none of it was a page load, a hashchange or a history entry');
    // a reload, and a link with ?tab=, land on the tab
    await tid(page, 'tab-changes').click();
    await page.reload();
    await tid(page, 'hero').waitFor();
    eq(await tid(page, 'tab-changes').getAttribute('aria-selected'), 'true', 'a reload lands on the tab');
    ok(await tid(page, 'growth').isVisible(), 'with its panel');
    await go(page, A.base, 'overview?tab=nope');
    await page.locator('[data-testid="tab-space"][aria-selected="true"]').waitFor();
    ok(await tid(page, 'volume').isVisible(), 'a tab that does not exist opens the first one');
    // Cleanup keeps both panels in the page and hides one
    await go(page, A.base, 'run');
    await tid(page, 'cleanup-finished').waitFor();
    eq(await page.locator('[role="tabpanel"]').count(), 2, 'Cleanup keeps both panels');
    ok(await page.locator('#tabpanel-archives').isHidden(), 'and hides the one that is not open');
    await tid(page, 'tab-archives').click();
    ok(await tid(page, 'archives-section').isVisible(), 'a click opens the archives');
    ok(await page.locator('#tabpanel-current').isHidden(), 'and hides the cleanups');
    ok(page.url().endsWith('#/run?tab=archives'), `the address follows: ${page.url()}`);
    // Settings: the panels are hidden, not redrawn, so what was typed is still there
    await go(page, A.base, 'settings');
    await tid(page, 'tab-thresholds').click();
    await tid(page, 'set-idle_days').fill('77');
    await tid(page, 'tab-scope').click();
    ok(await tid(page, 'set-user_roots').isVisible(), 'the scan scope panel is shown');
    ok(!(await tid(page, 'set-idle_days').isVisible()), 'the thresholds are hidden');
    eq(await page.locator('[role="tabpanel"]').count(), 3, 'but every panel is in the form');
    ok(page.url().endsWith('#/settings?tab=scope'), `the address follows: ${page.url()}`);
    await tid(page, 'tab-thresholds').click();
    eq(await tid(page, 'set-idle_days').inputValue(), '77', 'an unsaved edit survives a tab switch');
  });

  await step('settings: switch to 简体中文; no raw message keys anywhere', async () => {
    await go(page, A.base, 'settings');
    await tid(page, 'set-locale').selectOption('zh-CN');
    await Promise.all([page.waitForEvent('load'), tid(page, 'save-settings').click()]);
    await page.waitForSelector('#nav a');
    eq(await page.locator('#nav a').first().innerText(), '概览', 'navigation is in Chinese');
    eq(await page.locator('#nav a').nth(3).innerText(), '清理任务', 'the cleanup tab');
    await go(page, A.base, 'overview');
    await tid(page, 'hero').waitFor();
    ok((await tid(page, 'hero').innerText()).includes('可安全清理'), 'the hero uses the term for offered junk');
    ok((await tid(page, 'volume').innerText()).includes('存在 1 个本地快照'), 'the snapshot warning in Chinese');
    ok((await tid(page, 'hero-button').innerText()).startsWith('查看并清理 '), 'and for its button');
    ok((await tid(page, 'tier-review').innerText()).includes('需要你决定'), 'review tier term');
    ok((await tid(page, 'tier-protected').innerText()).includes('受保护'), 'protected term');
    const zhRecent = (await api(page, 'GET', '/api/overview')).body.not_included.too_recent;
    await tid(page, 'tab-why').click();
    await tid(page, 'row-too_recent').getByRole('link').first().click();
    await page.waitForURL(/state=too_recent/);
    await page.waitForFunction((want) => document.querySelector('[data-testid="browse-header"]')?.textContent === want,
      `不足 14 天的垃圾 · ${await count(zhRecent.items)} 项 · ${await fmt(zhRecent.alloc)}`, { timeout: 15000 });
    const seen = [];
    // a page shows one tab at a time: every tab is a stop of its own
    for (const route of ['overview', 'overview?tab=why', 'overview?tab=groups', 'overview?tab=changes', 'overview?tab=tools', 'browse', 'run', 'run?tab=archives',
      'advisor', 'advisor?tab=results', 'settings', 'settings?tab=scope', 'settings?tab=thresholds']) {
      await go(page, A.base, route);
      await sleep(500);
      if (route === 'browse') {
        await tid(page, 'summary-toggle').click();
        await tid(page, 'select-junk').click();
        await page.locator('.group-title').first().click();
        await sleep(500);
      }
      const text = await page.locator('body').innerText();
      const hit = text.match(rawKey);
      ok(!hit, `${route}: raw key visible: ${hit && text.slice(Math.max(0, hit.index - 30), hit.index + 50)}`);
      seen.push(text.length);
    }
    ok(seen.every((n) => n > 40), 'every view rendered text');
    await go(page, A.base, 'settings');
    await tid(page, 'set-locale').selectOption('en');
    await Promise.all([page.waitForEvent('load'), tid(page, 'save-settings').click()]);
    await page.waitForSelector('#nav a');
    eq(await page.locator('#nav a').first().innerText(), 'Overview', 'and back to English');
  });

  await step('insights on real history: the scans and the cleanup of this run are on the page, and the figures are the API\'s', async () => {
    await go(page, A.base, 'insights');
    await tid(page, 'kpis').waitFor();
    const ins = (await api(page, 'GET', '/api/insights?window=30')).body;
    ok(ins.history.scans.length >= 3, `the fixture scanned ${ins.history.scans.length} times`);
    ok(ins.history.cleanups.length >= 1, 'and cleaned');
    await tid(page, 'tab-trends').click();
    eq(await page.locator('[data-testid="used-marker"]').count(), ins.history.scans.length, 'one marker per scan');
    eq(await page.locator('[data-testid="cleanup-marker"]').count(), ins.history.cleanups.length, 'one marker per cleanup');
    eq(await tid(page, 'kpi-freed-value').innerText(), await fmt(ins.kpis.freed_total.value), 'freed so far');
    await tid(page, 'tab-results').click();
    eq(await tid(page, 'cleanup-row').count(), ins.history.cleanups.length, 'a row per cleanup');
    ok((await tid(page, 'cleanup-row').first().innerText()).includes(await fmt(ins.history.cleanups[ins.history.cleanups.length - 1].freed)), 'with what it freed');
  });

  await step('screenshots of every view at 1280x860 and 390x844, dark first, then light', async () => {
    // settings changed since the last scan, and a run replaced the earlier draft: scan, then leave a fresh draft
    await go(page, A.base, 'overview');
    await tid(page, 'hero').waitFor();
    await rescan(page);
    await page.reload();
    await tid(page, 'hero').waitFor();
    await sleep(1500);   // the scan.done event of that rescan can still arrive after the reload and redraw the page
    await tid(page, 'hero-button').click();
    await page.waitForURL(/#\/plan\//);
    draftId = page.url().split('/plan/')[1];
    await tid(page, 'plan-head').waitFor();
    const views = [['overview', 'overview'], ['insights', 'insights'], ['browse', 'browse'], ['browse-filtered', 'browse?tier=junk&state=too_recent'],
      ['plan-draft', `plan/${draftId}`], ['plan-done', `plan/${planId}`], ['cleanup', 'run'], ['cleanup-archives', 'archives'],
      ['advisor', 'advisor'], ['settings', 'settings']];
    for (const scheme of ['dark', 'light']) {
      await page.emulateMedia({ colorScheme: scheme });
      for (const [w, h] of [[1280, 860], [390, 844]]) {
        await page.setViewportSize({ width: w, height: h });
        for (const [name, route] of views) {
          await go(page, A.base, route);
          if (route === 'browse') {
            await tid(page, 'summary-count').waitFor();
            if (!(await tid(page, 'select-junk').isVisible())) await tid(page, 'summary-toggle').click();   // folded below 1320px
            await tid(page, 'select-junk').click();
            await groupCard(page, GROUP_LOGS).locator('.group-title').click();
            await itemRow(page, '~/Library/Logs/Acme/recent.log').waitFor();
          }
          if (name === 'browse-filtered') {
            await tid(page, 'browse-header').waitFor();
            await page.locator('.group-title').first().click();
            await page.locator('[data-testid="item"]').first().waitFor();
          }
          if (name === 'plan-draft') {
            await tid(page, 'confirm-run').waitFor();
            await page.waitForFunction(() => document.querySelector('[data-testid="confirm-run"]').disabled === false);
            await page.locator('.group-title').first().click();
          }
          if (name === 'plan-done') await tid(page, 'result').waitFor();
          if (name === 'cleanup') await tid(page, 'cleanup-draft').waitFor();
        if (name === 'cleanup-archives') await tid(page, 'archives').waitFor();
          if (name === 'insights') await tid(page, 'kpis').waitFor();
          await sleep(700);
          const text = await page.locator('main').innerText();
          const noun = text.match(/\b1 (items|entries|files|groups|locations|exclusions)\b/);
          ok(!noun, `${name}: "${noun && noun[0]}" is not English`);
          const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
          ok(overflow <= 1, `${name} at ${w}px in ${scheme} scrolls sideways by ${overflow}px`);
          const file = `${scheme}-${w}x${h}-${name}.png`;
          await page.screenshot({ path: path.join(outDir, file), fullPage: false });
          shots.push(file);
        }
      }
    }
    await page.setViewportSize({ width: 1280, height: 860 });
    await page.emulateMedia({ colorScheme: 'light' });
    for (const scheme of ['dark', 'light']) {
      for (const [w, h] of [[1280, 860], [390, 844]]) {
        const ctx = await browser.newContext({ viewport: { width: w, height: h }, colorScheme: scheme });
        const locked = await ctx.newPage();
        await locked.goto(`${A.base}/`);
        await locked.waitForURL(/locked\.html/);
        await locked.screenshot({ path: path.join(outDir, `${scheme}-${w}x${h}-locked.png`) });
        shots.push(`${scheme}-${w}x${h}-locked`);
        await ctx.close();
      }
    }
  });

  await context.close();
  await stopFixture(A);

  // ---------------------------------------------------------------- flow B: a running app and a changed file
  const B = await startFixture(['--running', 'com.anthropic.claudefordesktop', '--no-history']);
  const ctxB = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const pb = await newPage(ctxB, 'B');
  const fileB = (rel) => path.join(B.home, rel);

  await step('blocked and changed entries: the list names the app and what changed; the result explains every skip', async () => {
    await pb.goto(B.url);
    await pb.waitForSelector('#nav a');
    await scan(pb);
    await tid(pb, 'hero-button').click();
    await pb.waitForURL(/#\/plan\//);
    await pb.waitForSelector('[data-testid="blockers"]', { timeout: 15000 });
    const alloc = await fmtBytes(pb, 3 * 4096);
    const blockers = await tid(pb, 'blockers').innerText();
    ok(blockers.includes(`Quit Claude Desktop to clean 3 more items · ${alloc}`), `blockers: ${blockers}`);
    ok(await tid(pb, 'confirm-run').isEnabled(), 'blocked entries do not stop the list; they are left alone');
    eq(await tid(pb, 'blockers').locator('[data-kind="drift"]').count(), 0, 'nothing has changed yet');
    fs.appendFileSync(fileB('Library/Logs/Acme/old1.log'), 'more');   // changed after the list was made
    await tid(pb, 'recheck').click();
    await pb.waitForFunction(() => document.querySelector('[data-kind="drift"]'), null, { timeout: 15000 });
    ok((await tid(pb, 'blockers').locator('[data-kind="drift"]').innerText()).includes(`left alone: 1 item · ${await fmtBytes(pb, 4096)}`), 'a file changed after the scan is named before the run');
    ok((await tid(pb, 'blockers').locator('[data-kind="quit_app"]').innerText()).includes('Claude Desktop'), 'and so is the app to quit');
    await startRun(pb);
    await tid(pb, 'result').waitFor({ timeout: 30000 });
    eq(await tid(pb, 'result-skipped').locator('summary').innerText(), 'Skipped 4 items', 'four items were left alone');
    await tid(pb, 'result-skipped').locator('summary').click();
    const skipped = await tid(pb, 'result-skipped').innerText();
    ok(skipped.includes('Changed since the plan was made: 1'), `skipped reasons: ${skipped}`);
    ok(skipped.includes('In use: Claude Desktop is running: 3'), `skipped reasons: ${skipped}`);
    await tid(pb, 'view-skipped').click();
    await pb.waitForFunction(() => document.querySelectorAll('[data-testid="flat"] [data-testid="entry"]').length === 4);
    eq(await pb.locator('[data-testid="flat"] [data-state="skipped"]').count(), 4, 'the skipped items are listed with their reasons');
    ok(fs.existsSync(fileB('Library/Logs/Acme/old1.log')), 'the changed file stays');
    for (const i of [0, 1, 2]) ok(fs.existsSync(fileB(`Library/Application Support/Claude/Cache/old${i}`)), 'files of the running app stay');
    for (const gone of ['Library/Logs/Acme/old0.log', 'Library/Logs/Acme/old2.log', 'Library/Logs/Acme/old3.log', 'Library/Caches/com.acme.Tool/blob1']) {
      ok(!fs.existsSync(fileB(gone)), `${gone} was removed`);
    }
    ok(fs.existsSync(fileB('Library/Logs/Acme/recent.log')), 'the too-recent file stays');
  });

  await ctxB.close();
  await stopFixture(B);

  // ---------------------------------------------------------------- flow C: forty days of history, the frame, the theme, Insights
  const C = await startFixture([]);
  const ctxC = await browser.newContext({ viewport: { width: 1280, height: 860 } });
  await ctxC.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: C.base });
  const pc = await newPage(ctxC, 'C');
  const fmtC = (n) => fmtBytes(pc, n);
  const norm = (s) => s.replace(/\s+/g, ' ').trim();
  const countC = (n) => pc.evaluate(async (v) => (await import('/static/format.mjs')).fmtCount(v), n);
  const insights = async (w = 30) => (await api(pc, 'GET', `/api/insights?window=${w}`)).body;
  const VIEWS = [['overview', 'Overview'], ['insights', 'Insights'], ['browse', 'Browse'], ['run', 'Cleanup'],
    ['advisor', 'AI advisor'], ['settings', 'Settings']];

  // colours of text, fills and borders that carry a hue (outside the small status icons, which are SVG)
  const hueOffenders = (page) => page.evaluate(() => {
    const parse = (c) => (c.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
    const bad = [];
    for (const el of document.querySelectorAll('#topbar *, main *')) {
      if (el.closest('svg') || el.matches('.tone, .dot, .swatch')) continue;   // the data's own colours: dots and swatches
      const cs = getComputedStyle(el);
      for (const prop of ['color', 'backgroundColor', 'borderTopColor']) {
        const [r, g, b] = parse(cs[prop]);
        if (r === undefined || cs[prop] === 'rgba(0, 0, 0, 0)') continue;
        if (Math.max(r, g, b) - Math.min(r, g, b) > 12) bad.push(`${el.tagName}.${el.className} ${prop} ${cs[prop]}`);
      }
    }
    return bad.slice(0, 8);
  });

  await step('history fixture: the first visit already has scans and cleanups to show', async () => {
    await pc.goto(C.url);
    await pc.waitForSelector('#nav a');
    const all = await insights(0);
    ok(all.history.scans.length >= 5, `scans in the history: ${all.history.scans.length}`);
    ok(all.history.cleanups.length >= 2, `cleanups in the history: ${all.history.cleanups.length}`);
  });

  await step('sidebar: every view is one click away, the current one is marked, and the brand, icons, title and disk card are in place', async () => {
    eq(await pc.locator('#nav a').count(), 6, 'six views: the archives are a section of Cleanup now');
    eq(await pc.locator('#nav a svg').count(), 6, 'each with a line icon');
    eq(await pc.locator('#nav [data-testid="nav-archives"]').count(), 0, 'and there is no Archives tab');
    for (const [name, label] of VIEWS) {
      await pc.locator(`#nav [data-testid="nav-${name}"]`).click();
      await pc.waitForURL(new RegExp(`#/${name}`));
      await pc.waitForSelector(`main .view-${name}`);
      eq(await pc.locator('#nav a[aria-current="page"]').getAttribute('data-testid'), `nav-${name}`, `${name} is marked`);
      eq(await pc.locator('#nav a[aria-current="page"]').innerText(), label, 'by its name');
      ok((await pc.title()).startsWith(`${label} ·`), `the title names the view: ${await pc.title()}`);
    }
    ok((await pc.evaluate(() => document.querySelector('link[rel="icon"]').href)).endsWith('/static/favicon.svg'), 'the favicon is the logo mark');
    const res = await ctxC.request.get(`${C.base}/static/favicon.svg`);
    eq(res.status(), 200, 'and it is served');
    ok(res.headers()['content-type'].startsWith('image/svg+xml'), 'as an SVG');
    ok(await pc.locator('.topbar .brand-name').first().isVisible(), 'the wordmark is shown');
    ok(await tid(pc, 'top-scan').isVisible(), 'the Rescan button is in the sidebar');
    ok(await tid(pc, 'theme-toggle').isVisible(), 'and the theme button');
    eq(await pc.locator('[data-testid="lang-toggle"] button[aria-pressed="true"]').getAttribute('data-testid'), 'lang-en', 'and the language switch');
    const side = await pc.locator('#topbar').boundingBox();
    const main = await pc.locator('#main').boundingBox();
    ok(side.x + side.width <= main.x + 1 && side.height > side.width, `the bar stands to the left of the page: ${JSON.stringify(side)} | ${main.x}`);
    ok(await pc.evaluate(() => !!document.querySelector('#topbar [data-testid="side-disk"]')), 'the disk card is in the sidebar');
    ok((await tid(pc, 'side-disk').innerText()).includes('GiB'), 'with the free space and the volume size');
    ok(await tid(pc, 'side-last-scan').isVisible(), 'and the last scan');
    eq(await pc.locator('#foot, footer').count(), 0, 'there is no footer strip under the page');
    // the wordmark and the icons stay down to the narrowest sidebar layout, and the bar becomes the top bar at 960px
    await pc.setViewportSize({ width: 961, height: 860 });
    ok(await pc.locator('.topbar .brand-name').first().isVisible(), 'the wordmark is shown at 961px');
    ok(await pc.locator('#nav a svg').first().isVisible(), 'and so are the icons');
    ok(!(await tid(pc, 'menu-button').isVisible()), 'with no menu button');
    await pc.setViewportSize({ width: 960, height: 860 });
    await pc.locator('#top-panel').waitFor({ state: 'hidden' });   // the drawer slides away when the bar collapses
    ok(await tid(pc, 'menu-button').isVisible(), 'at 960px the menu button is back');
    ok(!(await pc.locator('#nav a').first().isVisible()), 'and the links leave the bar');
    await pc.setViewportSize({ width: 1280, height: 860 });
  });

  await step('design: glass panels blur, the radii and the figures are the design\'s, and nothing in the chrome has a hue', async () => {
    await go(pc, C.base, 'overview');
    await tid(pc, 'hero').waitFor();
    const d = await pc.evaluate(() => {
      const css = (el, pseudo) => getComputedStyle(el, pseudo);
      const tile = css(document.querySelector('a.tile'));
      const hero = css(document.querySelector('[data-testid="hero-size"]'));
      const cta = css(document.querySelector('[data-testid="hero-button"]'));
      return { blur: tile.backdropFilter || tile.webkitBackdropFilter, tileRadius: tile.borderRadius, tileBorder: tile.borderTopWidth,
        barBlur: css(document.querySelector('.topbar')).backdropFilter, barRadius: css(document.querySelector('.topbar')).borderRadius,
        barLayer: css(document.querySelector('.topbar'), '::before').content,
        heroSize: parseFloat(hero.fontSize), heroWeight: Number(hero.fontWeight), font: css(document.body).fontFamily, ctaRadius: cta.borderTopLeftRadius,
        plane: css(document.documentElement).getPropertyValue('--plane').trim() };
    });
    ok(/blur\(28px\)/.test(d.blur) && /saturate\((1\.2|120%)\)/.test(d.blur), `a panel blurs what is behind it: ${d.blur}`);
    ok(/blur\(28px\)/.test(d.barBlur), `so does the sidebar: ${d.barBlur}`);
    eq(d.barLayer, 'none', 'the sidebar is the glass itself, with no pseudo-element layer behind it');
    eq(d.tileRadius, '16px', 'cards have 16px corners');
    eq(d.barRadius, '24px', 'the sidebar is a panel with 24px corners, not a pill');
    ok(Number(d.ctaRadius.replace('px', '')) > 100, 'and so is the main button');
    eq(d.tileBorder, '1px', 'with a hairline border');
    ok(d.heroSize >= 52 && d.heroSize <= 64 && d.heroWeight >= 600, `the hero figure is 52-64px semibold: ${d.heroSize}px ${d.heroWeight}`);
    ok(/^system-ui/.test(d.font) && d.font.includes('PingFang SC'), `the system font stack: ${d.font}`);
    eq(await hueOffenders(pc).then((l) => l.join('; ')), '', 'no hue on the overview in the light theme');
    eq(await pc.evaluate(() => getComputedStyle(document.body, '::before').position + getComputedStyle(document.body, '::after').position), 'fixedfixed', 'the glow and the grain are fixed layers');
  });

  await step('top bar at 390px: the menu opens a drawer, keeps the focus inside, closes with Esc and after a choice', async () => {
    await pc.setViewportSize({ width: 390, height: 844 });
    await go(pc, C.base, 'overview');
    await tid(pc, 'menu-button').waitFor();
    await pc.locator('#top-panel').waitFor({ state: 'hidden' });   // the drawer slides away when the bar collapses
    ok(!(await pc.locator('#nav a').first().isVisible()), 'the links are not on the bar');
    eq(await tid(pc, 'menu-button').getAttribute('aria-expanded'), 'false', 'collapsed');
    await tid(pc, 'menu-button').click();
    eq(await tid(pc, 'menu-button').getAttribute('aria-expanded'), 'true', 'expanded');
    await pc.locator('#top-panel').waitFor({ state: 'visible' });
    ok(await pc.evaluate(() => document.getElementById('top-panel').contains(document.activeElement)), 'the focus moved into the drawer');
    eq(await pc.evaluate(() => document.getElementById('main').inert), true, 'the page behind is inert');
    ok(await pc.evaluate(() => document.getElementById('top-panel').contains(document.querySelector('[data-testid="side-disk"]'))), 'the disk card is in the drawer');
    ok(await tid(pc, 'side-last-scan').isVisible(), 'with its last-scan line');
    for (let i = 0; i < 18; i += 1) {
      await pc.keyboard.press('Tab');
      ok(await pc.evaluate(() => document.getElementById('top-panel').contains(document.activeElement)), `Tab ${i + 1} stays inside the drawer`);
    }
    await pc.keyboard.press('Shift+Tab');
    ok(await pc.evaluate(() => document.getElementById('top-panel').contains(document.activeElement)), 'and so does Shift+Tab');
    await pc.keyboard.press('Escape');
    await pc.locator('#top-panel').waitFor({ state: 'hidden' });
    eq(await tid(pc, 'menu-button').getAttribute('aria-expanded'), 'false', 'Esc closes it');
    ok(await pc.evaluate(() => document.activeElement === document.getElementById('menu-button')), 'and gives the focus back to the button');
    for (const [name, label] of VIEWS) {
      await tid(pc, 'menu-button').click();
      await pc.locator(`#nav [data-testid="nav-${name}"]`).click();
      await pc.waitForURL(new RegExp(`#/${name}`));
      await pc.waitForSelector(`main .view-${name}`);
      await pc.locator('#top-panel').waitFor({ state: 'hidden' });
      ok((await pc.title()).startsWith(label), `${label} opens from the drawer`);
    }
    await tid(pc, 'menu-button').click();
    await tid(pc, 'scrim').click({ position: { x: 20, y: 400 } });
    await pc.locator('#top-panel').waitFor({ state: 'hidden' });
    ok(await pc.evaluate(() => !document.getElementById('main').inert), 'the scrim closes it too, and the page is live again');
    ok((await pc.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)) <= 1, 'no sideways scrolling at 390px');
    await pc.setViewportSize({ width: 1280, height: 860 });
  });

  await step('theme: the button cycles system, light and dark; the choice survives a reload; dark renders', async () => {
    await go(pc, C.base, 'overview');
    const mode = () => tid(pc, 'theme-toggle').getAttribute('data-mode');
    const token = (name) => pc.evaluate((n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim(), name);
    eq(await mode(), 'system', 'follows the system by default');
    eq(await pc.evaluate(() => document.documentElement.getAttribute('data-theme')), null, 'with no stamp');
    eq(await token('--plane'), '#eceef0', 'light is the plane of a light system');
    await tid(pc, 'theme-toggle').click();
    eq(await mode(), 'light', 'first click: light');
    eq(await pc.evaluate(() => document.documentElement.getAttribute('data-theme')), 'light', 'stamped light');
    await tid(pc, 'theme-toggle').click();
    eq(await mode(), 'dark', 'second click: dark');
    eq(await token('--plane'), '#0a0a0b', 'near black');
    eq(await token('--text'), '#f5f5f5', 'with white text');
    eq(await pc.evaluate(() => localStorage.getItem('disk-steward-theme')), 'dark', 'remembered');
    ok((await pc.evaluate(() => document.activeElement.dataset.testid)) === 'theme-toggle', 'the keyboard stays on the button');
    await pc.reload();
    await pc.waitForSelector('#nav a');
    eq(await pc.evaluate(() => document.documentElement.getAttribute('data-theme')), 'dark', 'still dark after a reload');
    eq(await mode(), 'dark', 'and the button knows');
    eq(await pc.evaluate(() => getComputedStyle(document.documentElement).colorScheme), 'dark', 'the browser is told it is a dark page');
    await tid(pc, 'hero').waitFor();
    eq(await hueOffenders(pc).then((l) => l.join('; ')), '', 'no hue on the overview in the dark theme');
    await tid(pc, 'theme-toggle').click();
    eq(await mode(), 'system', 'third click: back to the system');
    eq(await pc.evaluate(() => localStorage.getItem('disk-steward-theme')), null, 'and nothing is stored');
    await pc.emulateMedia({ colorScheme: 'dark' });
    eq(await token('--plane'), '#0a0a0b', 'a dark system gets the dark plane');
    await pc.emulateMedia({ colorScheme: 'light' });
  });

  await step('accessibility fallbacks: reduced motion stops the transitions, forced colours turn the glass opaque', async () => {
    await go(pc, C.base, 'overview');
    await tid(pc, 'hero').waitFor();
    const motion = () => pc.evaluate(() => getComputedStyle(document.querySelector('#nav a')).transitionDuration);
    await pc.emulateMedia({ reducedMotion: 'no-preference' });
    ok(parseFloat(await motion()) > 0, 'with no preference the controls ease');
    await pc.emulateMedia({ reducedMotion: 'reduce' });
    eq(parseFloat(await motion()), 0, 'asked to reduce motion they do not');
    await pc.emulateMedia({ reducedMotion: 'no-preference' });
    await pc.emulateMedia({ forcedColors: 'active' });
    const forced = await pc.evaluate(() => { const c = getComputedStyle(document.querySelector('a.tile')); return [c.backdropFilter, c.boxShadow, c.borderTopStyle]; });
    eq(forced[0], 'none', 'forced colours: no blur');
    eq(forced[1], 'none', 'no shadow');
    eq(forced[2], 'solid', 'and a real border');
    const bar = await pc.evaluate(() => { const c = getComputedStyle(document.querySelector('.topbar')); return [c.backdropFilter, c.boxShadow, c.borderTopStyle]; });
    eq(bar.join('|'), 'none|none|solid', 'the sidebar is opaque too: no blur, no shadow, a real border');
    await pc.emulateMedia({ forcedColors: 'none' });
  });

  await step('insights: the key figures are the API\'s', async () => {
    await go(pc, C.base, 'insights');
    await tid(pc, 'kpis').waitFor();
    const ins = await insights(30);
    const k = ins.kpis;
    eq(await tid(pc, 'kpi-free-value').innerText(), await fmtC(k.free.value), 'free space');
    eq(await tid(pc, 'kpi-free-value').locator('[data-bytes]').getAttribute('data-bytes'), String(k.free.value), 'with the raw bytes');
    ok((await tid(pc, 'kpi-free').innerText()).includes(await fmtC(Math.abs(k.free.delta))), 'and its change');
    ok((await tid(pc, 'kpi-free').innerText()).includes(k.free.delta < 0 ? '▼' : '▲'), 'with its direction written as a symbol');
    ok((await tid(pc, 'kpi-free').innerText()).includes('vs 30 days ago'), 'against a named period');
    ok(await tid(pc, 'kpi-free').locator('svg.spark').isVisible(), 'and a sparkline');
    eq(await tid(pc, 'kpi-scanned-value').innerText(), await fmtC(k.scanned.value), 'scan coverage');
    eq(await tid(pc, 'kpi-freed-value').innerText(), await fmtC(k.freed_total.value), 'freed so far');
    ok((await tid(pc, 'kpi-freed-note').innerText()).startsWith(`${k.freed_total.count} cleanups`), 'out of its count');
    eq(await tid(pc, 'kpi-safe-value').innerText(), await fmtC(k.safe_to_clean.bytes), 'safe to clean');
    ok((await tid(pc, 'kpi-safe').innerText()).includes(`${k.safe_to_clean.items} items`), 'with its item count');
    const growth = await tid(pc, 'kpi-growth-value').innerText();
    ok(k.growth_per_day === null ? growth === '—' : growth.startsWith(await fmtC(k.growth_per_day).then((s) => (k.growth_per_day > 0 ? `+${s}` : s))), `daily growth: ${growth}`);
    await tid(pc, 'kpi-safe-button').click();
    await pc.waitForURL(/#\/overview/);
    await tid(pc, 'hero').waitFor();
    eq(await tid(pc, 'hero-size').innerText(), await fmtC(k.safe_to_clean.bytes), 'the button leads to the hero with the same figure');
  });

  await step('insights: the disk is one 100% bar; its parts add up to the disk; each part leads to the place that lists it', async () => {
    await go(pc, C.base, 'insights');
    await tid(pc, 'section-comp').waitFor();
    await sleep(900);   // charts redraw once at their real width; read them after that
    const ins = await insights(30);
    const total = ins.environment.volume.total;
    eq(ins.composition.reduce((a, c) => a + c.bytes, 0), total, 'the API\'s parts add up to the volume');
    const keys = ins.composition.map((c) => c.key);
    for (const key of keys) {
      const part = ins.composition.find((c) => c.key === key);
      const legend = tid(pc, `legend-${key}`);
      ok((await legend.innerText()).includes(await fmtC(part.bytes)), `${key}: the legend has its size`);
      ok((await legend.innerText()).includes(part.label), `${key}: and its name`);
    }
    const drawn = await pc.locator('[data-testid^="seg-"]').evaluateAll((els) => els.map((e) => ({ key: e.dataset.key, bytes: Number(e.dataset.bytes), w: e.querySelector('path.seg').getBBox().width })));
    eq(drawn.reduce((a, d) => a + d.bytes, 0), total, 'the drawn segments carry every byte of the disk');
    const width = await pc.evaluate(() => document.querySelector('[data-testid="comp-surface"] svg').getBoundingClientRect().width);
    const gaps = 2 * (drawn.length - 1);
    ok(Math.abs(drawn.reduce((a, d) => a + d.w, 0) + gaps - width) <= 2.5, `100% of the width: ${drawn.reduce((a, d) => a + d.w, 0) + gaps} of ${width}`);
    eq(drawn.map((d) => d.key).join(), 'junk,review,report_only,protected,outside,free', 'in the fixed order, the actionable part first');
    const href = (key) => tid(pc, `legend-${key}`).getAttribute('href');
    eq(await href('junk'), '#/overview', 'junk -> the overview');
    eq(await href('review'), '#/browse?tier=review&report_only=0', 'review -> Browse, needs a decision (the rows that have an action)');
    eq(await href('protected'), '#/browse?tier=protected', 'protected -> Browse, protected');
    eq(await href('report_only'), '#/browse?report_only=1', 'shown only -> Browse, shown only');
    eq(await tid(pc, 'legend-free').evaluate((e) => e.tagName), 'SPAN', 'free space leads nowhere');
    eq(await tid(pc, 'legend-outside').evaluate((e) => e.tagName), 'SPAN', 'not scanned explains itself');
    await tid(pc, 'legend-outside').focus();
    await pc.locator('#hint-outside').waitFor({ state: 'visible' });
    ok((await pc.locator('#hint-outside').innerText()).includes('outside the scanned areas'), 'with a short note');
    // hover on a segment: a tooltip with its size and share
    await tid(pc, 'seg-outside').hover();
    await pc.locator('.chart-tip:visible').waitFor();
    ok((await pc.locator('.chart-tip:visible').innerText()).includes(await fmtC(ins.composition.find((c) => c.key === 'outside').bytes)), 'hover shows the size');
    // the links land on the matching Browse filter
    for (const [key, query, title] of [['review', /tier=review/, 'Needs your decision'], ['protected', /tier=protected/, 'Protected'], ['report_only', /report_only=1/, 'Shown only']]) {
      await go(pc, C.base, 'insights');
      await tid(pc, `legend-${key}`).click();
      await pc.waitForURL(query);
      await tid(pc, 'browse-header').waitFor();
      await tid(pc, 'chips').waitFor();
      const part = ins.composition.find((c) => c.key === key);
      const header = await tid(pc, 'browse-header').innerText();
      ok(header.includes(await fmtC(part.bytes)), `${key}: Browse totals the same ${await fmtC(part.bytes)} the bar shows: ${header}`);
      if (key !== 'report_only') ok(header.startsWith(title), `${key}: the header names the filter: ${header}`);
    }
    await go(pc, C.base, 'insights');
    await tid(pc, 'legend-junk').click();
    await pc.waitForURL(/#\/overview/);
    await tid(pc, 'hero').waitFor();
    // Table switch: the same numbers
    await go(pc, C.base, 'insights');
    await tid(pc, 'comp-show-table').click();
    ok(await tid(pc, 'comp-table').isVisible(), 'the table shows');
    ok(!(await tid(pc, 'comp-surface').isVisible()), 'and the chart steps aside');
    eq(await tid(pc, 'comp-show-table').getAttribute('aria-pressed'), 'true', 'the switch says which');
    for (const part of ins.composition) {
      const row = await tid(pc, `comp-row-${part.key}`).innerText();
      ok(row.includes(await fmtC(part.bytes)) && row.includes(part.label), `table row ${part.key}: ${row}`);
    }
    await tid(pc, 'comp-show-chart').click();
    ok(await tid(pc, 'comp-surface').isVisible(), 'and back');
  });

  await step('insights: a second 100% bar shows the scanned space on its own, with the same tones, links and numbers', async () => {
    await go(pc, C.base, 'insights');
    await tid(pc, 'section-scanned').waitFor();
    await sleep(900);   // charts redraw once at their real width; read them after that
    const ins = await insights(30);
    const TIERS = ['junk', 'review', 'report_only', 'protected'];
    const parts = TIERS.map((k) => ins.composition.find((c) => c.key === k));
    const total = parts.reduce((a, c) => a + c.bytes, 0);
    eq(total, ins.kpis.scanned.value, 'the four tiers add up to what the scan covered');
    eq(await tid(pc, 'section-scanned').locator('h3').innerText(), 'Scanned space', 'its own title');
    ok((await tid(pc, 'section-scanned').innerText()).includes(await fmtC(total)), 'which says how much the scan covered');
    const drawn = await pc.locator('[data-testid^="scanned-seg-"]').evaluateAll((els) => els.map((e) => ({ key: e.dataset.key, bytes: Number(e.dataset.bytes), w: e.querySelector('path.seg').getBBox().width })));
    eq(drawn.map((d) => d.key).join(), TIERS.join(), 'the four tiers, the actionable one first');
    eq(drawn.reduce((a, d) => a + d.bytes, 0), total, 'every scanned byte is in the bar');
    const width = await pc.evaluate(() => document.querySelector('[data-testid="scanned-surface"] svg').getBoundingClientRect().width);
    ok(Math.abs(drawn.reduce((a, d) => a + d.w, 0) + 2 * (drawn.length - 1) - width) <= 2.5, 'filling the whole width, with 2px gaps');
    ok(drawn.filter((d) => d.w >= 12).length >= 3, `next to the whole-disk bar the tiers are readable: ${drawn.map((d) => Math.round(d.w)).join(', ')}px`);
    const inside = await pc.locator('[data-testid="scanned-surface"] text.seg-label').evaluateAll((els) => els.map((e) => e.textContent));
    ok(inside.length >= 2, `direct labels where they fit: ${inside.join(' | ')}`);
    for (const part of parts) {
      const legend = tid(pc, `scanned-legend-${part.key}`);
      const text = await legend.innerText();
      ok(text.includes(await fmtC(part.bytes)) && text.includes(part.label), `${part.key}: the legend has its name and size: ${text}`);
      eq(await legend.getAttribute('href'), await tid(pc, `legend-${part.key}`).getAttribute('href'), `${part.key}: the same link as under the whole-disk bar`);
      eq(await pc.locator(`[data-testid="scanned-seg-${part.key}"] path.seg`).evaluate((e) => getComputedStyle(e).fill),
        await pc.locator(`[data-testid="seg-${part.key}"] path.seg`).evaluate((e) => getComputedStyle(e).fill), `${part.key}: the same tone in both bars`);
    }
    await tid(pc, 'scanned-seg-review').hover();
    await pc.locator('.chart-tip:visible').waitFor();
    const tip = await pc.locator('.chart-tip:visible').innerText();
    ok(tip.includes(await fmtC(ins.composition.find((c) => c.key === 'review').bytes)) && tip.includes('Share of scanned'), `hover: size and share of the scanned total: ${tip}`);
    // its own table, independent of the first bar's
    await tid(pc, 'scanned-show-table').click();
    ok(await tid(pc, 'scanned-table').isVisible(), 'its table shows');
    ok(!(await tid(pc, 'scanned-surface').isVisible()), 'and its bar steps aside');
    ok(await tid(pc, 'comp-surface').isVisible(), 'while the whole-disk bar stays');
    for (const part of parts) {
      const row = await tid(pc, `scanned-row-${part.key}`).innerText();
      ok(row.includes(await fmtC(part.bytes)) && row.includes(part.label), `table row ${part.key}: ${row}`);
      ok(row.includes(`${((part.bytes / total) * 100).toFixed(part.bytes / total < 0.1 ? 1 : 0)}%`), `${part.key}: and its share of the scanned total: ${row}`);
    }
    await tid(pc, 'scanned-show-chart').click();
    ok(await tid(pc, 'scanned-surface').isVisible(), 'and back');
    // the links of the second bar land where the first one's do
    await tid(pc, 'scanned-legend-protected').click();
    await pc.waitForURL(/tier=protected/);
    await tid(pc, 'browse-header').waitFor();
    ok((await tid(pc, 'browse-header').innerText()).includes(await fmtC(parts[3].bytes)), 'protected -> Browse with the same size');
  });

  await step('insights: a line under the whole-disk bar says what "not scanned" is and leads to the largest applications', async () => {
    await go(pc, C.base, 'insights');
    await tid(pc, 'comp-note').waitFor();
    const note = await tid(pc, 'comp-note').innerText();
    ok(note.includes('system, apps and other data outside the folders Disk Steward checks'), `the caption: ${note}`);
    const box = await tid(pc, 'comp-note').boundingBox();
    const bar = await tid(pc, 'comp-surface').boundingBox();
    const scanned = await tid(pc, 'section-scanned').boundingBox();
    ok(box.y >= bar.y + bar.height - 1 && box.y + box.height <= scanned.y + 1, 'it sits under the first bar and above the second');
    eq(await tid(pc, 'comp-apps-link').getAttribute('href'), '#/overview?apps=1', 'its link goes to the overview\'s applications panel');
    await tid(pc, 'comp-apps-link').click();
    await pc.waitForURL(/#\/overview\?apps=1/);
    await tid(pc, 'measure-apps').waitFor();
    ok(await pc.evaluate(() => document.activeElement && document.activeElement.dataset.testid === 'measure-apps'), 'the "Measure applications" button has the focus');
    ok(await tid(pc, 'measure-apps').isVisible(), 'and is on screen');
    const inView = await tid(pc, 'measure-apps').evaluate((e) => { const r = e.getBoundingClientRect(); return r.top >= 0 && r.bottom <= window.innerHeight; });
    ok(inView, 'inside the window, not below the fold');
  });

  await step('insights: used space per scan is one line with a marker per scan and a hairline per cleanup', async () => {
    await go(pc, C.base, 'insights?tab=trends');
    await tid(pc, 'section-space').waitFor();
    await sleep(900);   // the chart redraws once at its real width
    const ins = await insights(30);
    const scans = ins.history.scans;
    eq(await pc.locator('[data-testid="used-marker"]').count(), scans.length, 'one marker per scan');
    const markers = await pc.locator('[data-testid="used-marker"]').evaluateAll((els) => els.map((e) => ({ v: Number(e.dataset.v), r: Number(e.getAttribute('r')), ring: getComputedStyle(e).strokeWidth })));
    eq(markers.map((m) => m.v).join(), scans.map((s) => s.used).join(), 'in order, at the scans\' used space');
    ok(markers.every((m) => m.r * 2 >= 8 && m.ring === '2px'), `at least 8px across with a 2px surface ring: ${JSON.stringify(markers)}`);
    eq(await pc.locator('.chart-svg path.line').first().evaluate((e) => getComputedStyle(e).strokeWidth), '2px', 'the line is 2px');
    ok(Number(await pc.locator('.chart-svg path.area').first().evaluate((e) => getComputedStyle(e).fillOpacity)) <= 0.12, 'under a wash of about 10%');
    eq(await pc.locator('[data-testid="space-surface"] .legend').count(), 0, 'a single series has no legend box');
    eq(await pc.locator('[data-testid="cleanup-marker"]').count(), ins.history.cleanups.length, 'one hairline and badge per cleanup');
    eq(await pc.locator('[data-testid="space-surface"] line.event-line').count(), ins.history.cleanups.length, 'the hairlines');
    const badge = (await pc.locator('[data-testid="cleanup-marker"]').first().textContent()).trim();
    ok(badge.startsWith('Freed '), `the badge says what it freed: ${badge}`);
    eq(await pc.locator('[data-testid="cleanup-marker"]').first().getAttribute('href'), `#/plan/${ins.history.cleanups[0].plan_id}`, 'and links to the cleanup');
    // crosshair and tooltip, on hover and on the keyboard
    await pc.locator('[data-testid="used-marker"]').nth(1).hover();
    await pc.locator('.chart-tip:visible').waitFor();
    const tip = await pc.locator('.chart-tip:visible').innerText();
    ok(tip.includes(await fmtC(scans[1].used)) && tip.includes(await fmtC(scans[1].free)), `hover: used and free in the tooltip: ${tip}`);
    ok(await pc.locator('line.crosshair:not([hidden])').count() === 1, 'with a crosshair on its time');
    await pc.mouse.move(5, 5);
    await pc.locator('[data-testid="used-marker"]').last().focus();
    await pc.locator('.chart-tip:visible').waitFor();
    ok((await pc.locator('.chart-tip:visible').innerText()).includes(await fmtC(scans[scans.length - 1].used)), 'focus shows the same tooltip');
    await pc.keyboard.press('ArrowLeft');
    ok((await pc.locator('.chart-tip:visible').innerText()).includes(await fmtC(scans[scans.length - 2].used)), 'the arrow keys walk along the scans');
    // the second chart: what the scans found
    eq(await pc.locator('[data-testid="mix-col"]').count(), scans.length, 'one column per scan');
    const cols = await pc.locator('[data-testid="mix-col"]').evaluateAll((els) => els.map((e) => ({ total: Number(e.dataset.total), w: Math.max(...[...e.querySelectorAll('path.seg')].map((p) => p.getBBox().width)), keys: [...e.querySelectorAll('path.seg')].map((p) => p.dataset.key).join() })));
    ok(cols.every((c) => c.w <= 24.01), `columns are at most 24px wide: ${Math.max(...cols.map((c) => c.w))}`);
    eq(cols[cols.length - 1].total, scans[scans.length - 1].junk + scans[scans.length - 1].review + scans[scans.length - 1].protected + scans[scans.length - 1].report_only, 'a column adds up its four parts');
    ok(cols.every((c) => c.keys.split(',').every((k) => ['protected', 'report_only', 'review', 'junk'].includes(k))), 'only the four categories');
    const legendKeys = await pc.locator('[data-testid="mix-surface"] .legend .legend-item').evaluateAll((els) => els.map((e) => e.dataset.key));
    eq(legendKeys.join(), 'junk,review,report_only,protected', 'a legend names all four');
    const directLabels = await pc.locator('[data-testid="mix-surface"] text.direct-label').allInnerTexts();
    ok(directLabels.length >= 2 && directLabels.length <= 4, `direct labels on the last column only: ${directLabels.join(', ')}`);
    eq(await pc.locator('[data-testid="backfilled-note"]').count(), scans.some((s) => s.backfilled) ? 1 : 0, 'the footnote shows exactly when a scan was backfilled');
    // the same entity has the same grey everywhere
    const colJunk = await pc.locator('[data-testid="mix-col"] path.seg[data-key="junk"]').first().evaluate((e) => getComputedStyle(e).fill);
    await tid(pc, 'tab-composition').click();
    await sleep(900);   // charts redraw once at their real width; read them after that
    const barJunk = await pc.locator('[data-testid="seg-junk"] path.seg').evaluate((e) => getComputedStyle(e).fill);
    const legendJunk = await pc.locator('[data-testid="legend-junk"] .swatch').evaluate((e) => getComputedStyle(e).backgroundColor);
    eq(barJunk, colJunk, 'junk is one tone in the bar and the columns');
    eq(barJunk, legendJunk, 'and in the legend');
  });

  await step('insights: the cleanup table has a row per cleanup, each leading to its plan, and the columns say the same', async () => {
    await go(pc, C.base, 'insights?tab=results');
    await tid(pc, 'section-results').waitFor();
    await sleep(900);   // charts redraw once at their real width; read them after that
    const ins = await insights(30);
    const cleanups = ins.history.cleanups;
    eq(await tid(pc, 'cleanup-row').count(), cleanups.length, 'a row per cleanup');
    const header = await tid(pc, 'results-table').locator('th').allInnerTexts();
    eq(header.map((x) => x.toLowerCase()).join(), 'date,items,removed,actually freed,skipped', 'the columns');
    const newest = cleanups.slice().reverse();
    for (let i = 0; i < newest.length; i += 1) {
      const row = tid(pc, 'cleanup-row').nth(i);
      eq(await row.getAttribute('data-id'), newest[i].plan_id, `row ${i} is cleanup ${newest[i].plan_id}`);
      eq(await row.getByRole('link').first().getAttribute('href'), `#/plan/${newest[i].plan_id}`, 'and links to its plan');
      const cells = await row.locator('td').allInnerTexts();
      eq(cells[1], String(newest[i].items_done), 'items');
      eq(cells[2], await fmtC(newest[i].removed), 'removed');
      eq(cells[3], await fmtC(newest[i].freed), 'actually freed');
      eq(cells[4], String(newest[i].items_skipped), 'skipped');
    }
    eq(await pc.locator('[data-testid="freed-col"]').count(), cleanups.length, 'a column per cleanup');
    eq(await pc.locator('[data-testid="results-surface"] .legend').count(), 0, 'one series, no legend box');
    await tid(pc, 'cleanup-row').first().getByRole('link').first().click();
    await pc.waitForURL(new RegExp(`#/plan/${newest[0].plan_id}`));
    await tid(pc, 'result').waitFor();
    // the seeded cleanups are weeks old: their per-item rows are gone and the page says so, above where the rows would be
    const detail = (await api(pc, 'GET', `/api/plans/${newest[0].plan_id}`)).body;
    ok(detail.compacted_at !== null, 'the API says this plan was compacted');
    const catalog = (await api(pc, 'GET', '/api/i18n/en')).body;
    await tid(pc, 'compacted-note').waitFor();
    eq(norm(await tid(pc, 'compacted-note').innerText()), norm(catalog['plan.compacted_note']), 'the note is the core text');
    ok((await tid(pc, 'compacted-note').innerText()).includes('7 days'), 'naming the week');
    eq(await tid(pc, 'flat').locator('[data-testid="compacted-note"]').count(), 1, 'inside the list\'s card');
    eq(await tid(pc, 'entries').count(), 0, 'with no empty list under it');
    ok((await tid(pc, 'result').innerText()).includes(String(newest[0].items_done)), 'the result still carries its totals from the stored summary');
    eq(await tid(pc, 'compacted-note').locator('svg').count(), 1, 'with an icon');
  });

  await step('insights: the window control asks again, keeps the tab, and every section follows it', async () => {
    await go(pc, C.base, 'insights?tab=trends');
    await tid(pc, 'kpis').waitFor();
    const markersAt = () => pc.locator('[data-testid="used-marker"]').count();
    eq(await tid(pc, 'window-30').getAttribute('aria-pressed'), 'true', '30 days is the default');
    const seven = await insights(7);
    const all = await insights(0);
    ok(seven.history.scans.length < all.history.scans.length, `7 days (${seven.history.scans.length} scans) and all (${all.history.scans.length}) differ`);
    const asked = pc.waitForRequest((r) => r.url().includes('/api/insights?window=7'));
    await tid(pc, 'window-7').click();
    await asked;
    await pc.waitForFunction((n) => document.querySelectorAll('[data-testid="used-marker"]').length === n, seven.history.scans.length);
    eq(await tid(pc, 'window-7').getAttribute('aria-pressed'), 'true', '7 days is pressed');
    eq(await markersAt(), seven.history.scans.length, 'the line has the 7-day scans');
    ok((await tid(pc, 'kpi-free').innerText()).includes('vs 7 days ago'), 'the key figures name the window');
    eq(await tid(pc, 'kpi-freed-value').innerText(), await fmtC(seven.kpis.freed_total.value), 'and recompute');
    ok(pc.url().endsWith('#/insights?window=7&tab=trends'), `the address names the window and keeps the tab: ${pc.url()}`);
    eq(await tid(pc, 'tab-trends').getAttribute('aria-selected'), 'true', 'the tab stays open');
    eq(await pc.locator('[data-testid="mix-col"]').count(), seven.history.scans.length, 'the columns follow too');
    await tid(pc, 'tab-results').click();
    eq(await tid(pc, 'cleanup-row').count(), seven.history.cleanups.length, 'and the cleanup table on another tab');
    await pc.reload();
    await tid(pc, 'kpis').waitFor();
    eq(await tid(pc, 'window-7').getAttribute('aria-pressed'), 'true', 'a reload keeps the window');
    eq(await tid(pc, 'tab-results').getAttribute('aria-selected'), 'true', 'and the tab');
    await tid(pc, 'tab-trends').click();
    const askedAll = pc.waitForRequest((r) => r.url().includes('/api/insights?window=0'));
    await tid(pc, 'window-0').click();
    await askedAll;
    await pc.waitForFunction((n) => document.querySelectorAll('[data-testid="used-marker"]').length === n, all.history.scans.length);
    ok((await tid(pc, 'kpi-free').innerText()).includes('since the first scan'), 'all history: since the first scan');
    eq(await markersAt(), all.history.scans.length, 'every scan is on the line');
    eq(await tid(pc, 'kpi-scanned-value').innerText(), await fmtC(all.kpis.scanned.value), 'coverage');
    await tid(pc, 'window-30').click();
    await pc.waitForFunction((n) => document.querySelectorAll('[data-testid="used-marker"]').length === n, (await insights(30)).history.scans.length);
  });

  await step('insights: growth is a diverging bar with a zero line, labels in text, and every row leads to its group', async () => {
    await go(pc, C.base, 'insights?tab=trends');
    await tid(pc, 'section-growth').waitFor();
    await sleep(900);   // charts redraw once at their real width; read them after that
    const ins = await insights(30);
    const { up, down } = ins.growth;
    eq(await tid(pc, 'growth-row').count(), up.length + down.length, 'a row per group');
    ok(await pc.locator('[data-testid="section-growth"] line.axis.zero').count() >= 1, 'one neutral zero line');
    for (const g of [...up, ...down]) {
      const row = tid(pc, 'growth-row').and(pc.locator(`[data-key="${g.group}"]`));
      eq(await row.getAttribute('href'), `#/browse?${await pc.evaluate((key) => new URLSearchParams({ group: key }).toString(), g.group)}`, `${g.group} -> its group in Browse`);
      const text = await row.innerText();
      ok(text.includes(await fmtC(Math.abs(g.delta))), `${g.label}: the change in text: ${text}`);
      ok(text.includes(g.delta > 0 ? '+' : '-'), 'with its sign');
      const side = await row.locator('path.seg').evaluate((e) => ({ cls: e.getAttribute('class'), x: e.getBBox().x, w: e.getBBox().width, mid: e.ownerSVGElement.viewBox.baseVal.width / 2, h: e.getBBox().height }));
      ok(side.h <= 24, 'bars are at most 24px thick');
      ok(g.delta > 0 ? side.x >= side.mid - 0.5 : side.x + side.w <= side.mid + 0.5, `${g.group}: growth right of zero, shrinking left`);
      ok(g.delta > 0 ? side.cls.includes('s-up') : side.cls.includes('s-down'), 'in the pole\'s own tone');
    }
    const first = tid(pc, 'growth-row').first();
    const href = await first.getAttribute('href');
    await first.click();
    await pc.waitForURL(/#\/browse\?group=/);
    ok(decodeURIComponent(pc.url().split('group=')[1].replace(/\+/g, ' ')) === up[0].group, `${href} opens the group`);
    await tid(pc, 'group').first().waitFor();
    eq(await pc.locator(`[data-testid="group"][data-key="${up[0].group}"]`).count(), 1, 'and it is there');
  });

  await step('insights: AI agents are sorted bars that lead to Browse; the todo list leads to the screens that show the same number', async () => {
    await go(pc, C.base, 'insights?tab=agents');
    await tid(pc, 'section-agents').waitFor();
    await sleep(900);   // charts redraw once at their real width; read them after that
    const ins = await insights(30);
    const sorted = ins.agents.slice().sort((a, b) => b.bytes - a.bytes);
    eq((await tid(pc, 'agent-row').evaluateAll((els) => els.map((e) => e.dataset.key))).join(), sorted.map((a) => a.provider).join(), 'sorted by size');
    eq(await tid(pc, 'agent-row').first().getAttribute('href'), '#/browse?domain=agent', 'each row leads to the agents in Browse');
    ok((await tid(pc, 'agent-row').first().innerText()).includes(await fmtC(sorted[0].bytes)), 'with its size');
    const widths = await tid(pc, 'agent-row').evaluateAll((els) => els.map((e) => e.querySelector('path.seg').getBBox().width));
    ok(widths.every((w, i) => i === 0 || w <= widths[i - 1] + 0.01), 'the bars shrink with the size');
    // todos
    await tid(pc, 'tab-todos').click();
    ok((await tid(pc, 'tab-todos').innerText()).includes(String(ins.todos.length)), 'the tab says how many are waiting');
    eq(await tid(pc, 'todo').count(), ins.todos.length, 'every todo is listed');
    const names = (await (await ctxC.request.get(`${C.base}/api/i18n/en`, { headers: { 'X-Disk-Steward-Token': await pc.evaluate((port) => localStorage.getItem(`disk-steward-token:${port}`), new URL(C.base).port) } })).json());
    for (const todo of ins.todos) {
      const row = tid(pc, 'todo').and(pc.locator(`[data-id="${todo.id}"]`));
      eq(await row.getAttribute('data-severity'), todo.severity, `${todo.id}: severity`);
      eq((await row.locator('.sev').innerText()).trim(), names[`insights.severity.${todo.severity}`], 'the severity is a word');
      eq(await row.locator('.sev svg').count(), 1, 'beside an icon');
      ok(norm(await row.innerText()).includes(norm(todo.title)), 'with its title');
      if (todo.bytes !== null) ok((await row.innerText()).includes(await fmtC(todo.bytes)), 'and its size');
      if (todo.runnable) {
        eq(await row.locator('[data-testid="todo-action"]').count(), 0, `${todo.id}: a tool that can run itself has a Run button, not a link to read about it`);
        eq(await row.locator('[data-testid="todo-run"]').getAttribute('href'), '#/browse?report_only=1', 'which leads to the table that runs it');
        eq(await row.getByRole('button', { name: 'Copy' }).count(), 0, 'and nothing to copy and paste');
        eq(norm(await row.locator('code').innerText()), norm(todo.command), 'the command is still shown');
      } else {
        eq(await row.locator('[data-testid="todo-action"]').count(), todo.action ? 1 : 0, 'an action when there is one');
        if (todo.command) {
          eq(norm(await row.locator('code').innerText()), norm(todo.command), 'the command is shown');
          await row.getByRole('button', { name: 'Copy' }).click();
          await pc.waitForFunction(async (cmd) => (await navigator.clipboard.readText()) === cmd, todo.command);
          ok(true, 'and copied');
        }
      }
    }
    const junk = ins.todos.find((x) => x.id === 'clean_junk');
    await tid(pc, 'todo').and(pc.locator('[data-id="clean_junk"]')).locator('[data-testid="todo-action"]').click();
    await pc.waitForURL(/#\/overview/);
    await tid(pc, 'hero').waitFor();
    eq(await tid(pc, 'hero-size').innerText(), await fmtC(junk.bytes), 'the clean-up todo leads to the hero with the same bytes');
    const quick = (await api(pc, 'GET', '/api/quick')).body.summary;
    eq(await tid(pc, 'hero-items').innerText(), `${quick.count} items`, 'and it is what the hero button would clean');
    eq(await fmtC(quick.alloc), await fmtC(junk.bytes), 'the todo, the hero and the plan all say the same bytes');
    for (const todo of ins.todos.filter((x) => x.runnable)) {
      await go(pc, C.base, 'insights?tab=todos');
      await tid(pc, 'todo').and(pc.locator(`[data-id="${todo.id}"]`)).locator('[data-testid="todo-run"]').click();
      await pc.waitForURL(/#\/browse\?report_only=1/);
      const rule = todo.id.split(':')[1];
      await pc.locator(`[data-testid="native-row"][data-rule="${rule}"], [data-testid="native-target"][data-rule="${rule}"]`).first().waitFor({ timeout: 15000 });
      ok(await pc.locator(`[data-rule="${rule}"] button[data-action="run"]`).first().isEnabled(), `${todo.id}: its Run button is on that page`);
    }
    for (const todo of ins.todos.filter((x) => x.action && !x.runnable && x.id !== 'clean_junk')) {
      await go(pc, C.base, 'insights?tab=todos');
      await tid(pc, 'todo').and(pc.locator(`[data-id="${todo.id}"]`)).locator('[data-testid="todo-action"]').click();
      const landed = todo.action.href === '#/archives' ? '#/run?tab=archives' : todo.action.href;   // the archives are a tab of Cleanup
      await pc.waitForFunction((h) => decodeURIComponent(location.hash) === decodeURIComponent(h), landed);
      await pc.waitForSelector('main .view');
      ok(true, `${todo.id} opens ${todo.action.href}`);
      if (todo.action.href.startsWith('#/browse?group=')) {
        const key = decodeURIComponent(todo.action.href.split('group=')[1]);
        await pc.locator(`[data-testid="group"][data-key="${key}"]`).waitFor({ timeout: 15000 });
        eq(await pc.locator(`[data-testid="group"][data-key="${key}"]`).count(), 1, `${todo.id}: the group is on the page`);
      }
      if (todo.action.href === '#/archives') {
        await tid(pc, 'archive').first().waitFor({ timeout: 15000 });
        ok((await tid(pc, 'archive').count()) >= 1, 'the archives are listed');
      }
      if (todo.action.href === '#/settings') await tid(pc, 'settings-form').waitFor();
    }
  });

  await step('insights: every chart has a table with the same numbers', async () => {
    await go(pc, C.base, 'insights?tab=trends');
    await tid(pc, 'section-space').waitFor();
    const ins = await insights(30);
    const scans = ins.history.scans;
    await tid(pc, 'space-show-table').click();
    const rows = tid(pc, 'space-table').locator('tbody tr');
    eq(await rows.count(), scans.length, 'a row per scan');
    for (let i = 0; i < scans.length; i += 1) {
      const text = await rows.nth(i).innerText();
      ok(text.includes(await fmtC(scans[i].used)) && text.includes(await fmtC(scans[i].free)), `scan ${scans[i].id}: ${text}`);
    }
    await tid(pc, 'mix-show-table').click();
    const mixRows = tid(pc, 'mix-table').locator('tbody tr');
    eq(await mixRows.count(), scans.length, 'the same in the second chart');
    const last = (await mixRows.last().innerText());
    for (const key of ['junk', 'review', 'protected', 'report_only']) ok(last.includes(await fmtC(scans[scans.length - 1][key])), `${key} in ${last}`);
    await tid(pc, 'growth-show-table').click();
    eq(await tid(pc, 'growth-table').locator('tbody tr').count(), ins.growth.up.length + ins.growth.down.length, 'growth rows');
    for (const id of ['space', 'mix', 'growth']) {
      eq(await tid(pc, `${id}-show-table`).getAttribute('aria-pressed'), 'true', `${id}: the table is selected`);
      ok(!(await tid(pc, `${id}-surface`).isVisible()), `${id}: and the chart is not in the way`);
    }
    await tid(pc, 'tab-agents').click();
    await tid(pc, 'agents-show-table').click();
    eq(await tid(pc, 'agents-table').locator('tbody tr').count(), ins.agents.length, 'agent rows');
    ok((await tid(pc, 'agents-table').innerText()).includes(await fmtC(ins.agents[0].bytes)), 'with the sizes');
    eq(await tid(pc, 'agents-show-table').getAttribute('aria-pressed'), 'true', 'agents: the table is selected');
    ok(!(await tid(pc, 'agents-surface').isVisible()), 'agents: and the chart is not in the way');
    await tid(pc, 'tab-trends').click();
    eq(await tid(pc, 'space-show-table').getAttribute('aria-pressed'), 'true', 'a table stays selected when its tab is left and opened again');
    await pc.reload();
    await tid(pc, 'kpis').waitFor();
    ok(await tid(pc, 'space-surface').isVisible(), 'a reload starts on the charts again');
    await tid(pc, 'tab-agents').click();
    ok(await tid(pc, 'agents-surface').isVisible(), 'on every tab');
  });

  await step('insights: no label is clipped or leaves its chart, marks are thin, and the environment card has its fields', async () => {
    for (const [w, tab] of [1280, 390].flatMap((x) => ['composition', 'trends', 'agents'].map((y) => [x, y]))) {
      await pc.setViewportSize({ width: w, height: 844 });
      await go(pc, C.base, `insights?tab=${tab}`);
      await tid(pc, 'kpis').waitFor();
      await sleep(600);
      const bad = await pc.evaluate(() => {
        const out = [];
        for (const svg of document.querySelectorAll('.chart-svg')) {
          const box = svg.getBoundingClientRect();
          if (!box.width) continue;
          for (const text of svg.querySelectorAll('text')) {
            const b = text.getBoundingClientRect();
            if (b.left < box.left - 1 || b.right > box.right + 1) out.push(`${text.textContent} (${Math.round(b.left - box.left)}..${Math.round(b.right - box.left)} of ${Math.round(box.width)})`);
          }
        }
        return out.slice(0, 5);
      });
      eq(bad.join('; '), '', `no chart label is clipped at ${w}px on ${tab}`);
      ok((await pc.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)) <= 1, `no sideways scrolling at ${w}px on ${tab}`);
      const bars = await pc.evaluate(() => Math.max(0, ...[...document.querySelectorAll('[data-testid="agent-row"] path.seg, [data-testid="growth-row"] path.seg')].map((p) => p.getBBox().height)));
      ok(bars <= 24, `horizontal bars are at most 24px thick: ${bars}`);
      const hair = await pc.evaluate(() => [...document.querySelectorAll('.chart-svg line.grid')].every((l) => getComputedStyle(l).strokeWidth === '1px' && !getComputedStyle(l).strokeDasharray.match(/\d/)));
      ok(hair, 'gridlines are 1px and solid');
      if (tab === 'trends') eq(await pc.evaluate(() => document.querySelectorAll('.chart-svg line.axis:not(.zero)').length > 0), true, 'and there is one axis line per chart');
    }
    await pc.setViewportSize({ width: 1280, height: 860 });
    await go(pc, C.base, 'insights?tab=env');
    await tid(pc, 'section-env').waitFor();
    const ins = await insights(30);
    const env = ins.environment;
    const text = await tid(pc, 'section-env').innerText();
    for (const part of [env.macos, env.arch, env.python, env.tool_version, await fmtC(env.volume.total), env.data_dir, `#${env.last_scan.id}`]) {
      ok(text.includes(part), `the environment card has ${part}`);
    }
    ok(/\d(\.\d+)? (B|KiB|MiB|GiB)/.test(await tid(pc, 'env-data_dir').innerText()), 'the data folder has a size (it grows while the fixture runs, so only its shape is compared)');
    eq(await tid(pc, 'section-env').locator('.env-row').count(), 12, 'twelve fields');
    ok((await tid(pc, 'env-clis').innerText()).includes('not found') || (await tid(pc, 'env-clis').innerText()).includes('codex'), 'the tools are listed with their versions or as missing');
    ok((await tid(pc, 'env-schedule').innerText()).includes(env.schedule.installed ? 'at' : 'Not installed'), 'the weekly scan is stated');
  });

  // ---------------------------------------------------------------- tables
  await step('browse: the groups are a table with sortable headers, 44px rows, right-aligned sizes and the tier as a dot with its word', async () => {
    await go(pc, C.base, 'browse');
    await tid(pc, 'group').first().waitFor();
    const table = pc.locator('table.groups-table');
    const heads = await table.locator('thead th').evaluateAll((els) => els.map((e) => e.textContent.trim()));
    eq(heads.join('|'), 'Select|Name|Tier|Area|Qualifies|Size|Last changed|Details', 'the columns');
    // below 1320px the selection is a bar at the foot and the table has the whole width; from 1320px the selection stands beside it
    ok(await tid(pc, 'sort-groups-size').isVisible(), 'at 1280px the Size header is on show');
    ok(await tid(pc, 'summary-toggle').isVisible(), 'the selection is a bar with a Details button');
    await pc.evaluate(() => window.scrollTo(0, 0));
    const bar = await tid(pc, 'summary').boundingBox();
    ok(Math.abs(bar.y + bar.height - 860) <= 1, `the bar sticks to the foot of the screen: ${bar.y + bar.height} of 860`);
    await pc.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
    const list = await tid(pc, 'groups').boundingBox();
    const end = await tid(pc, 'summary').boundingBox();
    ok(end.y >= list.y + list.height - 1 && Math.abs(end.x - list.x) <= 1, `at the end of the page it is below the table: ${JSON.stringify(end)} under ${JSON.stringify(list)}`);
    await pc.evaluate(() => window.scrollTo(0, 0));
    await pc.setViewportSize({ width: 1400, height: 860 });
    ok(await tid(pc, 'sort-groups-size').isVisible(), 'at 1400px the Size header is still on show');
    ok(!(await tid(pc, 'summary-toggle').isVisible()), 'the selection has no Details button');
    const wide = { list: await tid(pc, 'groups').boundingBox(), side: await tid(pc, 'summary').boundingBox() };
    ok(wide.side.x >= wide.list.x + wide.list.width - 1, `and stands to the right of the table: ${JSON.stringify(wide.side)} beside ${JSON.stringify(wide.list)}`);
    await pc.setViewportSize({ width: 1280, height: 860 });
    eq(await table.locator('thead th[aria-sort]').count(), 6, 'six of them sort');
    eq((await table.locator('thead th[aria-sort]').evaluateAll((els) => els.map((e) => e.getAttribute('aria-sort')))).join(), 'none,none,none,none,none,none', 'none of them has been asked to');
    const css = await table.evaluate((tb) => {
      const th = tb.querySelector('thead th.c-name');
      const row = tb.querySelector('tbody tr.group-row');
      const size = row.querySelector('td.c-size');
      return { sticky: getComputedStyle(th).position, rowH: row.getBoundingClientRect().height, align: getComputedStyle(size).textAlign,
        nums: getComputedStyle(size).fontVariantNumeric, border: getComputedStyle(row.querySelector('td')).borderBottomWidth };
    });
    eq(css.sticky, 'sticky', 'the header stays in view');
    ok(css.rowH >= 44, `rows are at least 44px: ${css.rowH}`);
    eq(css.align, 'right', 'sizes are right-aligned');
    ok(css.nums.includes('tabular-nums'), 'and in tabular figures');
    eq(css.border, '1px', 'rows are divided by a hairline');
    const probe = (name) => pc.evaluate((n) => { const p = document.createElement('i'); p.style.color = `var(${n})`; document.body.append(p); const c = getComputedStyle(p).color; p.remove(); return c; }, name);
    const pills = await table.locator('.badge-tier').evaluateAll((els) => els.map((e) => ({ tier: e.dataset.tier, text: e.textContent.trim(), dot: getComputedStyle(e.querySelector('.tone')).backgroundColor })));
    ok(pills.length >= 3 && pills.every((p) => p.text.length > 2), 'every tier is a dot AND a word');
    for (const p of pills) eq(p.dot, await probe(`--c-${p.tier}`), `the ${p.tier} dot has the ${p.tier} colour`);
    ok(new Set(pills.map((p) => p.dot)).size >= 2, 'and different tiers have different colours');
    // sorting, both ways
    const all = (await api(pc, 'GET', '/api/groups')).body.groups;
    const keys = () => table.locator('tbody.group-body').evaluateAll((els) => els.map((e) => e.dataset.key));
    await tid(pc, 'sort-groups-size').click();
    eq(await tid(pc, 'sort-groups-size').locator('xpath=..').getAttribute('aria-sort'), 'descending', 'the first click sorts the biggest first');
    const bySize = await keys();
    const alloc = Object.fromEntries(all.map((g) => [g.key, g.alloc]));
    ok(bySize.every((k, i) => i === 0 || alloc[bySize[i - 1]] >= alloc[k]), 'in that order');
    await tid(pc, 'sort-groups-size').click();
    eq(await tid(pc, 'sort-groups-size').locator('xpath=..').getAttribute('aria-sort'), 'ascending', 'the second one reverses it');
    const reversed = await keys();
    ok(reversed.every((k, i) => i === 0 || alloc[reversed[i - 1]] <= alloc[k]), 'in that order');
    await tid(pc, 'sort-groups-name').click();
    const label = Object.fromEntries(all.map((g) => [g.key, g.label.toLowerCase()]));
    const byName = await keys();
    ok(byName.every((k, i) => i === 0 || label[byName[i - 1]].localeCompare(label[k]) <= 0), 'names sort alphabetically');
    eq(await table.locator('thead th[aria-sort="ascending"]').count() + await table.locator('thead th[aria-sort="descending"]').count(), 1, 'one column at a time carries the arrow');
    // hover
    const first = table.locator('tbody tr.group-row').first();
    const before = await first.locator('td.c-size').evaluate((e) => getComputedStyle(e).backgroundColor);
    await first.locator('td.c-size').hover();
    ok(await first.locator('td.c-size').evaluate((e, was) => getComputedStyle(e).backgroundColor !== was, before), 'a row lights up under the pointer');
    // expanding a row reveals its items as a nested table
    await pc.mouse.move(5, 5);
    await first.locator('button[aria-expanded]').last().click();
    const items = pc.locator('table[data-testid="items"]').first();
    await items.waitFor();
    eq((await items.locator('thead th').evaluateAll((els) => els.map((e) => e.textContent.trim()))).join('|'), 'Select|Path|Size|Last changed|Status|Action', 'the items table has its own columns');
    const itemRows = items.locator('tbody tr[data-testid="item"]');
    ok((await itemRows.count()) >= 1, 'with a row per item');
    const row = itemRows.first();
    eq(await row.locator('.path-mid').getAttribute('title'), await row.getAttribute('data-path'), 'the whole path is the tooltip of a shortened one');
    ok((await row.getAttribute('data-path')).endsWith(await row.locator('.path-mid .tail').innerText()), 'and the end of the path is what stays visible');
    ok((await row.locator('td.c-status .dot').count()) === 1 && (await row.locator('td.c-status').innerText()).trim().length > 3, 'the state is a dot and a word');
    ok(await row.locator('td.c-size').evaluate((e) => getComputedStyle(e).textAlign === 'right'), 'sizes are right-aligned here too');
    eq(await tid(pc, 'sort-items-size').locator('xpath=..').getAttribute('aria-sort'), 'descending', 'items start biggest first');
    await tid(pc, 'sort-items-size').click();
    await pc.waitForFunction(() => document.querySelector('[data-testid="sort-items-size"]').parentElement.getAttribute('aria-sort') === 'ascending');
    ok(true, 'and a click on Size turns that around');
    // narrow screens: the less important columns move under the name, and the page does not scroll sideways
    await pc.setViewportSize({ width: 390, height: 844 });
    await sleep(500);
    eq(await table.locator('col.c-tier').evaluate((e) => getComputedStyle(e).visibility), 'collapse', 'the tier column folds away');
    ok(await table.locator('tbody tr.group-row').first().locator('.meta-sm').first().isVisible(), 'and its figures reappear under the name');
    ok((await table.locator('tbody tr.group-row').first().locator('.meta-sm').first().innerText()).includes('KiB'), 'with the size');
    ok(await pc.locator('table[data-testid="items"] tr[data-testid="item"]').first().locator('.status-sm').isVisible(), 'an item keeps its state under its path');
    ok((await pc.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)) <= 1, 'no sideways scrolling at 390px');
    await pc.setViewportSize({ width: 1280, height: 860 });
  });

  await step('data colours follow the entity: the same colour in the bars, the columns, the legends and the dots, in light and in dark', async () => {
    const rgb = { light: { junk: 'rgb(227, 73, 72)', review: 'rgb(237, 161, 0)', report_only: 'rgb(42, 120, 214)', protected: 'rgb(154, 160, 166)', outside: 'rgb(95, 102, 112)', free: 'rgb(27, 175, 122)' },
      dark: { junk: 'rgb(208, 59, 59)', review: 'rgb(201, 133, 0)', report_only: 'rgb(57, 135, 229)', protected: 'rgb(160, 165, 173)', outside: 'rgb(80, 86, 94)', free: 'rgb(25, 158, 112)' } };
    const check = async (theme) => {
      const want = rgb[theme];
      await go(pc, C.base, 'insights');
      await tid(pc, 'kpis').waitFor();
      await sleep(900);   // the charts draw once at a guessed width and again at the real one: read them after that
      const fill = (sel) => pc.locator(sel).first().evaluate((e) => getComputedStyle(e).fill);
      const bg = (sel) => pc.locator(sel).first().evaluate((e) => getComputedStyle(e).backgroundColor);
      // never colour alone: every dot, swatch and bar segment has its word beside it (read on each tab in turn)
      const words = async () => {
        const bare = await pc.evaluate(() => [...document.querySelectorAll('.dotted, .badge-tier, .legend-item, .sev')].filter((e) => e.textContent.trim().length < 2).length);
        eq(bare, 0, `${theme}: no dot, swatch or pill stands without a word`);
        const unlabelled = await pc.evaluate(() => [...document.querySelectorAll('[data-testid^="seg-"], [data-testid^="scanned-seg-"], [data-testid="mix-col"]')].filter((e) => !(e.getAttribute('aria-label') || '').trim()).length);
        eq(unlabelled, 0, `${theme}: and every mark has a name for assistive technology`);
      };
      const open = async (key) => { await tid(pc, `tab-${key}`).click(); await sleep(900); };
      for (const key of Object.keys(want)) {
        eq(await fill(`[data-testid="seg-${key}"] path.seg`), want[key], `${theme}: ${key} in the whole-disk bar`);
        eq(await bg(`[data-testid="legend-${key}"] .swatch`), want[key], `${theme}: ${key} in its legend`);
      }
      for (const key of ['junk', 'review', 'report_only', 'protected']) {
        eq(await fill(`[data-testid="scanned-seg-${key}"] path.seg`), want[key], `${theme}: ${key} in the scanned-space bar`);
      }
      await words();
      await open('trends');
      for (const key of ['junk', 'review', 'report_only', 'protected']) {
        eq(await fill(`[data-testid="mix-col"] path.seg[data-key="${key}"]`), want[key], `${theme}: ${key} in the columns`);
        eq(await bg(`[data-testid="mix-surface"] .legend-item[data-key="${key}"] .swatch`), want[key], `${theme}: ${key} in the columns' legend`);
      }
      eq(await fill('[data-testid="growth-row"] path.seg.s-up'), want.junk, `${theme}: what grew is red`);
      eq(await fill('[data-testid="growth-row"] path.seg.s-down'), want.report_only, `${theme}: what shrank is blue, never green`);
      eq(await pc.locator('[data-testid="space-surface"] path.line').evaluate((e) => getComputedStyle(e).stroke), theme === 'light' ? 'rgb(18, 18, 18)' : 'rgb(245, 245, 245)', `${theme}: the used-space line is the neutral ink`);
      eq(await pc.locator('[data-testid="space-surface"] line.event-line').first().evaluate((e) => getComputedStyle(e).stroke), want.free, `${theme}: cleanup markers are green`);
      await words();
      await open('results');
      eq(await fill('[data-testid="freed-col"] path.seg'), want.free, `${theme}: what a cleanup freed is green`);
      await words();
      await open('agents');
      eq(await fill('[data-testid="agent-row"] path.seg'), want.report_only, `${theme}: AI agents are one blue`);
      await words();
      await open('todos');
      await words();
    };
    await check('light');
    await go(pc, C.base, 'overview');
    await tid(pc, 'tier-junk').waitFor();
    eq(await tid(pc, 'tier-junk').locator('.tone').evaluate((e) => getComputedStyle(e).backgroundColor), rgb.light.junk, 'the junk tile has a red dot');
    eq(await tid(pc, 'tier-review').locator('.tone').evaluate((e) => getComputedStyle(e).backgroundColor), rgb.light.review, 'the review tile an amber one');
    eq(await tid(pc, 'tier-protected').locator('.tone').evaluate((e) => getComputedStyle(e).backgroundColor), rgb.light.protected, 'the protected tile a grey one');
    await pc.emulateMedia({ colorScheme: 'dark' });
    await check('dark');
    await pc.emulateMedia({ colorScheme: 'light' });
  });

  await step('report-only tools are a table on Overview and on Browse: tool, size, command, action; models get a row each; empty ones are hidden', async () => {
    const tools = (await api(pc, 'GET', '/api/native')).body.tools;
    ok(tools.length >= 2, `tools with something to clean: ${tools.map((x) => x.rule_id).join(', ')}`);
    await go(pc, C.base, 'overview?tab=tools');
    await tid(pc, 'native').waitFor();
    eq(await tid(pc, 'native').locator('h2').innerText(), 'Shown only: managed by other tools', 'the title');
    eq((await tid(pc, 'native').locator('thead th').evaluateAll((els) => els.map((e) => e.textContent.trim()))).join('|'), 'Tool|Size|Command|Action', 'the columns');
    const rows = tid(pc, 'native-row').and(pc.locator('tr:not(.native-target)'));
    eq(await rows.count(), tools.filter((x) => x.bytes > 0).length, 'a row per tool that holds something');
    for (const tool of tools) {
      const row = tid(pc, 'native-row').and(pc.locator(`[data-rule="${tool.rule_id}"]:not(.native-target)`));
      ok((await row.locator('td.c-size').innerText()) === await fmtC(tool.bytes), `${tool.rule_id}: its size is the API's`);
      ok((await row.locator('.cmd-pill code').first().innerText()) === tool.command, `${tool.rule_id}: its command is in a pill`);
      if (tool.runnable && !tool.targets) ok(await row.locator('button[data-action="run"]').isEnabled(), `${tool.rule_id}: a runnable tool has a Run button`);
      for (const target of tool.targets || []) {
        const sub = tid(pc, 'native-target').and(pc.locator(`[data-target="${target.name}"]`));
        eq(await sub.locator('.cmd-pill code').first().innerText(), target.command, `${target.name}: its own command`);
        ok(await sub.locator('button[data-action="run"]').isEnabled(), `${target.name}: and its own Delete model button`);
      }
    }
    ok(!(await tid(pc, 'native').innerText()).includes('\n0 B'), 'no row with nothing in it');
    await go(pc, C.base, 'browse?report_only=1');
    await tid(pc, 'native').waitFor();
    eq(await tid(pc, 'native-row').and(pc.locator('tr:not(.native-target)')).count(), tools.filter((x) => x.bytes > 0).length, 'with the same rows');
    eq(await pc.locator('table.groups-table').count(), 0, 'and nothing under it: the groups behind it would list the same tools twice');
    eq(await tid(pc, 'group').count(), 0, 'no group rows at all');
    ok((await tid(pc, 'browse-header').innerText()).includes('Shown only'), 'the header still names the filter and counts it');
    // a command is always readable in full: the pill wraps instead of cutting it
    const readable = () => pc.locator('[data-testid="native"] .cmd-pill code').evaluateAll((els) => els.filter((e) => e.offsetParent !== null && getComputedStyle(e).visibility === 'visible').map((e) => ({
      text: e.textContent, clipped: e.scrollWidth > e.clientWidth + 1, wrap: getComputedStyle(e).whiteSpace, ellipsis: getComputedStyle(e).textOverflow === 'ellipsis' })));
    const wide = await readable();
    ok(wide.length >= 3, `commands on show: ${wide.map((c) => c.text).join(' | ')}`);
    ok(wide.every((c) => !c.clipped && c.wrap === 'normal' && !c.ellipsis), 'at 1280px none is clipped or ends in an ellipsis');
    ok(wide.some((c) => c.text === 'brew cleanup --prune=all'), 'the whole of `brew cleanup --prune=all` is there');
    await go(pc, C.base, 'browse?tier=junk');
    await tid(pc, 'group').first().waitFor();
    eq(await tid(pc, 'native').count(), 0, 'and only on the "shown only" filter');
    // sorting
    await go(pc, C.base, 'browse?report_only=1');
    await tid(pc, 'native').waitFor();
    const order = () => tid(pc, 'native-row').and(pc.locator('tr:not(.native-target)')).evaluateAll((els) => els.map((e) => e.dataset.rule));
    const bigFirst = await order();
    await tid(pc, 'sort-native-size').click();
    eq((await order()).join(), bigFirst.slice().reverse().join(), 'the Size header flips the order');
    // narrow
    await pc.setViewportSize({ width: 390, height: 844 });
    await sleep(500);
    ok((await pc.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)) <= 1, 'no sideways scrolling at 390px');
    ok(await tid(pc, 'native-row').first().locator('.meta-sm .cmd-pill').isVisible(), 'the command moves under the tool name');
    const narrow = await readable();
    ok(narrow.length >= 2 && narrow.every((c) => !c.clipped && !c.ellipsis), 'and it is still whole at 390px (it wraps)');
    ok(await tid(pc, 'native').evaluate((e) => [...e.querySelectorAll('.cmd-pill')].filter((p) => p.offsetParent !== null && getComputedStyle(p).visibility === 'visible').every((p) => p.getBoundingClientRect().right <= e.getBoundingClientRect().right + 1)), 'inside its card');
    await pc.setViewportSize({ width: 1280, height: 860 });
  });

  await step('a group that belongs to another tool wears one blue badge everywhere, and the name cell repeats nothing a column says', async () => {
    const probe = (name) => pc.evaluate((n) => { const p = document.createElement('i'); p.style.color = `var(${n})`; document.body.append(p); const c = getComputedStyle(p).color; p.remove(); return c; }, name);
    const groups = (await api(pc, 'GET', '/api/groups')).body.groups;
    const other = groups.filter((g) => g.tier === 'review' && g.actions.length === 0);
    ok(other.length >= 2, `groups of other tools: ${other.map((g) => g.key).join(', ')}`);
    await go(pc, C.base, 'browse');
    await tid(pc, 'group').first().waitFor();
    for (const g of other) {
      const body = pc.locator(`[data-testid="group"][data-key="${g.key}"]`);
      eq(await body.locator('td.c-tier .badge-tier').count(), 1, `${g.key}: one badge in the tier column`);
      eq(await body.locator('td.c-tier .badge-tier').getAttribute('data-tier'), 'report_only', 'the other-tool one');
      eq((await body.locator('td.c-tier .badge-tier').innerText()).trim(), 'Managed by its tool', 'with its words');
      eq(await body.locator('td.c-tier .badge-tier .tone').evaluate((e) => getComputedStyle(e).backgroundColor), await probe('--c-report_only'), 'in the other-tools colour (blue)');
      const text = await body.locator('tr.group-row').innerText();
      ok(!text.includes('Needs your decision') && !text.includes('Report only'), `${g.key}: never "needs your decision" or "report only" as well: ${text.replace(/\s+/g, ' ')}`);
    }
    const junk = groups.find((g) => g.tier === 'junk');
    eq(await pc.locator(`[data-testid="group"][data-key="${junk.key}"] td.c-tier .badge-tier`).getAttribute('data-tier'), 'junk', 'a junk group keeps its own badge');
    // the name cell: path, then only what the table folded away (area, age): no tier, no badge
    const first = pc.locator('[data-testid="group"] tr.group-row').first();
    eq(await first.locator('td.c-name .badge-tier').count(), 0, 'no tier badge in the name cell');
    const domains = Object.fromEntries(groups.map((g) => [g.key, g.domain]));
    const key = await pc.locator('[data-testid="group"]').first().getAttribute('data-key');
    const sub = (await first.locator('td.c-name .meta-md').innerText()).trim();
    ok(sub.startsWith((await api(pc, 'GET', '/api/i18n/en')).body[`domain.${domains[key]}`] + ' · '), `its second line is area · age: ${sub}`);
    ok(!/Junk|decision|Protected|Managed/.test(sub), 'and says nothing of the tier');
    eq(await first.locator('td.c-name .meta-sm').evaluate((e) => getComputedStyle(e).display), 'none', 'the figures line is not there while the columns are');
    // the Overview's largest groups, and Insights' growth rows say the same
    await go(pc, C.base, 'overview?tab=groups');
    await tid(pc, 'top-group').first().waitFor();
    for (const g of other) {
      const row = tid(pc, 'top-group').and(pc.locator(`[data-key="${g.key}"]`));
      if (await row.count()) {
        eq(await row.locator('.badge-tier').count(), 1, `${g.key}: one badge on the Overview`);
        eq((await row.locator('.badge-tier').innerText()).trim(), 'Managed by its tool', 'that says it');
      }
    }
    await go(pc, C.base, 'insights?tab=trends');
    await tid(pc, 'section-growth').waitFor();
    await sleep(900);   // charts redraw once at their real width; read them after that
    const growth = (await api(pc, 'GET', '/api/insights?window=30')).body.growth;
    for (const g of [...growth.up, ...growth.down].filter((x) => other.some((o) => o.key === x.group))) {
      const sub2 = await tid(pc, 'growth-row').and(pc.locator(`[data-key="${g.group}"]`)).locator('.rb-sub').innerText();
      ok(sub2.includes('Managed by its tool') && !sub2.includes('Needs your decision'), `${g.group}: Insights says the same: ${sub2}`);
    }
    // narrow: the tier moves into the line under the name, and only there
    await go(pc, C.base, 'browse');
    await tid(pc, 'group').first().waitFor();
    await pc.setViewportSize({ width: 390, height: 844 });
    await sleep(500);
    eq(await pc.locator('col.c-tier').evaluate((e) => getComputedStyle(e).visibility), 'collapse', 'the tier column is gone at 390px');
    for (const g of [junk, other[0]]) {
      const text = await pc.locator(`[data-testid="group"][data-key="${g.key}"] tr.group-row`).innerText();
      const word = g.tier === 'junk' ? 'Junk' : 'Managed by its tool';
      eq((text.split(word).length - 1), 1, `${g.key}: the tier is said once (in the second line): ${text.replace(/\s+/g, ' ')}`);
    }
    await pc.setViewportSize({ width: 1280, height: 860 });
  });

  await step('screenshots of the tables: Browse with a group open and the tools table, dark first, then light, at 1280x860 and 390x844', async () => {
    for (const scheme of ['dark', 'light']) {
      await pc.emulateMedia({ colorScheme: scheme });
      for (const [w, h] of [[1280, 860], [390, 844]]) {
        await pc.setViewportSize({ width: w, height: h });
        await go(pc, C.base, 'browse');
        await tid(pc, 'group').first().waitFor();
        await pc.locator('table.groups-table tbody.group-body').first().locator('button[aria-expanded]').last().click();
        await tid(pc, 'items').first().waitFor();
        await sleep(500);
        await pc.screenshot({ path: path.join(outDir, `${scheme}-${w}x${h}-browse-table.png`) });
        shots.push(`${scheme}-${w}x${h}-browse-table`);
        await go(pc, C.base, 'browse?report_only=1');
        await tid(pc, 'native').waitFor();
        await sleep(500);
        await tid(pc, 'native').screenshot({ path: path.join(outDir, `${scheme}-${w}x${h}-native-table.png`) });
        shots.push(`${scheme}-${w}x${h}-native-table`);
      }
    }
    await pc.emulateMedia({ colorScheme: 'light' });
    await pc.setViewportSize({ width: 1280, height: 860 });
  });

  await step('a runnable tool: Run asks first with the exact command, the folder size and what it does; Cancel starts nothing', async () => {
    await go(pc, C.base, 'browse?report_only=1');
    await tid(pc, 'native').waitFor();
    const tool = (await api(pc, 'GET', '/api/native')).body.tools.find((x) => x.rule_id === 'dev.homebrew');
    const groups = (await api(pc, 'GET', '/api/groups')).body.groups;
    const effect = groups.find((g) => g.key === tool.group).effect.split(/(?<=\.)\s/)[0];
    const jobBefore = (await api(pc, 'GET', '/api/job')).body.job;
    await tid(pc, 'native-row').and(pc.locator('[data-rule="dev.homebrew"]')).getByRole('button', { name: 'Run' }).click();
    const dialog = pc.locator('dialog[open]');
    await dialog.waitFor();
    eq(await dialog.locator('h2').innerText(), 'Run Homebrew downloads?', 'the title names the tool');
    eq(await tid(pc, 'native-confirm-command').innerText(), 'brew cleanup --prune=all', 'the exact command');
    eq(await tid(pc, 'native-confirm-size').innerText(), await fmtC(tool.bytes), 'the folder size now');
    eq(await dialog.locator('.confirm-what').innerText(), effect, 'what it does, in one line');
    ok((await dialog.innerText()).includes('exactly this command'), 'and that nothing else runs');
    eq((await dialog.getByRole('button').allInnerTexts()).join('|'), 'Cancel|Run', 'with Run and Cancel');
    await dialog.getByRole('button', { name: 'Cancel' }).click();
    await pc.waitForFunction(() => !document.querySelector('dialog[open]'));
    const jobAfter = (await api(pc, 'GET', '/api/job')).body.job;
    eq([jobAfter.kind, jobAfter.status, jobAfter.started_at].join(), [jobBefore.kind, jobBefore.status, jobBefore.started_at].join(), 'Cancel starts nothing: the job is the one it was');
    ok(jobAfter.kind !== 'native', 'in particular no tool run');
    eq(await tid(pc, 'native-running').count(), 0, 'and shows no progress');
  });

  await step('running a tool: progress and live output in the table, then what it freed, and the table follows the rescan', async () => {
    const tool = (await api(pc, 'GET', '/api/native')).body.tools.find((x) => x.rule_id === 'dev.homebrew');
    const row = tid(pc, 'native-row').and(pc.locator('[data-rule="dev.homebrew"]'));
    await row.getByRole('button', { name: 'Run' }).click();
    await pc.locator('dialog[open]').waitFor();
    await tid(pc, 'native-confirm-yes').click();
    await tid(pc, 'native-running').waitFor();
    ok(await tid(pc, 'native-cancel').isVisible(), 'a Cancel button while it runs');
    ok(await pc.locator('button[data-action="run"]').evaluateAll((els) => els.every((e) => e.disabled)), 'and no other tool can be started meanwhile');
    await tid(pc, 'native-log').waitFor();
    await pc.waitForFunction(() => /step1/.test(document.querySelector('[data-testid="native-log"]')?.textContent || ''), null, { timeout: 15000 });
    await pc.screenshot({ path: path.join(outDir, 'light-1280x860-native-running.png') });   // the page, not the element: the table redraws with every line of output
    shots.push('light-1280x860-native-running');
    const style = await tid(pc, 'native-log').evaluate((e) => ({ font: getComputedStyle(e).fontFamily, atEnd: e.scrollTop + e.clientHeight >= e.scrollHeight - 2 }));
    ok(/mono|Menlo|Consolas/i.test(style.font), `the output is monospace: ${style.font}`);
    ok(style.atEnd, 'and scrolled to its last line');
    await pc.waitForFunction(() => /step3/.test(document.querySelector('[data-testid="native-log"]')?.textContent || ''), null, { timeout: 15000 });
    await tid(pc, 'native-result').waitFor({ timeout: 30000 });
    const job = (await api(pc, 'GET', '/api/job')).body.job;
    const result = job.native;
    eq(result.exit_code, 0, 'the tool exited cleanly');
    const banner = await tid(pc, 'native-result').innerText();
    ok(banner.includes('brew cleanup --prune=all') && banner.includes(`Freed ${await fmtC(result.freed)}`), `the banner says what it freed: ${banner}`);
    ok(banner.includes(await fmtC(result.before)) && banner.includes(await fmtC(result.after)), 'from what to what');
    eq(result.before, tool.bytes, 'the "before" is the size the dialog showed');
    ok(!fs.existsSync(path.join(C.home, 'Library/Caches/Homebrew/downloads/seed.tar.gz')), 'the files are gone from disk');
    await pc.waitForFunction(() => !document.querySelector('[data-testid="native-row"][data-rule="dev.homebrew"]'), null, { timeout: 30000 });
    ok(true, 'the row is gone after the automatic rescan');
    eq((await api(pc, 'GET', '/api/native')).body.tools.some((x) => x.rule_id === 'dev.homebrew'), false, 'and so is the tool in the API');
  });

  await step('a model can be deleted by its own row; Cancel stops the tool and keeps the files; a second try deletes it', async () => {
    fs.writeFileSync(tools.slowFile, '1');   // `ollama rm` waits while this file exists
    const blob = path.join(C.home, '.ollama/models/blobs/sha256-seed');
    const target = tid(pc, 'native-target').and(pc.locator('[data-target="tiny:latest"]'));
    await target.waitFor();
    await target.getByRole('button', { name: 'Delete model' }).click();
    const dialog = pc.locator('dialog[open]');
    await dialog.waitFor();
    eq(await dialog.locator('h2').innerText(), 'Delete model tiny:latest?', 'the title names the model');
    eq(await tid(pc, 'native-confirm-command').innerText(), 'ollama rm tiny:latest', 'the exact command for this model');
    await tid(pc, 'native-confirm-yes').click();
    await tid(pc, 'native-running').waitFor();
    await pc.waitForFunction(() => /deleting 'tiny:latest'/.test(document.querySelector('[data-testid="native-log"]')?.textContent || ''), null, { timeout: 15000 });
    await tid(pc, 'native-cancel').click();
    await tid(pc, 'native-result').waitFor({ timeout: 30000 });
    const banner = await tid(pc, 'native-result').innerText();
    ok(/did not finish/.test(banner), `a cancelled run says it did not finish: ${banner}`);
    ok(fs.existsSync(blob), 'and the model is still there');
    fs.rmSync(tools.slowFile, { force: true });
    await pc.waitForFunction(() => !document.querySelector('[data-testid="native-running"]'), null, { timeout: 15000 });
    await tid(pc, 'native-target').and(pc.locator('[data-target="tiny:latest"]')).getByRole('button', { name: 'Delete model' }).click();
    await pc.locator('dialog[open]').waitFor();
    await tid(pc, 'native-confirm-yes').click();
    await pc.waitForFunction(() => /Freed/.test(document.querySelector('[data-testid="native-result"]')?.textContent || ''), null, { timeout: 30000 });
    ok(!fs.existsSync(blob), 'now the model is gone from disk');
    const left = (await api(pc, 'GET', '/api/native')).body.tools.find((x) => x.rule_id === 'ollama.models');
    ok(left.bytes < 8192, `and the Ollama row shrank to what the folder itself weighs: ${left.bytes}`);
    await pc.waitForFunction((size) => document.querySelector('[data-testid="native-row"][data-rule="ollama.models"] td.c-size')?.textContent === size, await fmtC(left.bytes), { timeout: 30000 });
    ok(true, 'which the table shows after the automatic rescan');
    await tid(pc, 'native').getByRole('button', { name: 'Dismiss' }).click();
    eq(await tid(pc, 'native-result').count(), 0, 'what happened can be dismissed');
  });

  await step('insights records tool runs: the cleanup table shows them with the command instead of a plan link', async () => {
    await go(pc, C.base, 'insights?tab=results');
    await tid(pc, 'section-results').waitFor();
    await sleep(900);   // charts redraw once at their real width; read them after that
    const ins = await insights(30);
    const natives = ins.history.cleanups.filter((c) => c.kind === 'native');
    ok(natives.length >= 2, `native runs in the history: ${natives.map((c) => c.command).join(' | ')}`);
    eq(await tid(pc, 'cleanup-row').count(), ins.history.cleanups.length, 'every cleanup has a row');
    eq(await tid(pc, 'cleanup-row').and(pc.locator('[data-kind="native"]')).count(), natives.length, 'the tool runs among them');
    for (const c of natives) {
      const row = tid(pc, 'cleanup-row').and(pc.locator('[data-kind="native"]')).filter({ hasText: c.command }).filter({ hasText: await fmtC(c.freed) }).first();
      ok(await row.isVisible(), `${c.command}: has a row with what it freed (${await fmtC(c.freed)})`);
      eq(await row.getByRole('link').count(), 0, 'with no plan to link to');
    }
    eq(await tid(pc, 'cleanup-row').and(pc.locator('[data-kind="plan"]')).first().getByRole('link').first().getAttribute('href').then((h) => h.startsWith('#/plan/')), true, 'a plan still links to its page');
    await tid(pc, 'tab-trends').click();
    eq(await pc.locator('[data-testid="cleanup-marker"]').count(), ins.history.cleanups.length, 'every one is a marker on the line');
    eq(await pc.locator('a[data-testid="cleanup-marker"]').count(), ins.history.cleanups.length - natives.length, 'only the plans are links');
    ok((await tid(pc, 'kpi-freed-note').innerText()).startsWith(`${ins.kpis.freed_total.count} cleanups`), 'and they count toward the cleanups');
    eq(ins.todos.filter((x) => x.id.startsWith('report_only')).length, 0, 'the "run it yourself" todos are gone');
  });

  await step('Browse draws 50 groups at a time: the header counts them all, a button brings more, and sorting and searching cover every group', async () => {
    for (let i = 0; i < 60; i += 1) {   // sixty more groups: a cache folder each, of different sizes
      const dir = path.join(C.home, 'Library/Caches', `com.e2e.page${String(i).padStart(2, '0')}`);
      fs.mkdirSync(dir, { recursive: true });
      fs.writeFileSync(path.join(dir, 'blob'), 'x'.repeat(3000 + i * 997));
    }
    // scan until the new folders are in: another scan (the one a finished tool run starts) may be going, then the first try is BUSY
    for (let attempt = 0; attempt < 40; attempt += 1) {
      const started = await api(pc, 'POST', '/api/scan', {});
      if (started.status !== 200 && started.status !== 423) throw new Error(`scan refused: ${JSON.stringify(started.body)}`);
      await pc.waitForFunction(async () => {
        const res = await fetch('/api/state', { headers: { 'X-Disk-Steward-Token': localStorage.getItem(`disk-steward-token:${location.port}`) } });
        return (await res.json()).job.status !== 'running';
      }, null, { timeout: 60000 });
      const seen = (await api(pc, 'GET', '/api/groups')).body.groups.some((g) => g.key.endsWith('com.e2e.page59'));
      if (seen) break;
      await sleep(500);
    }
    const { groups: all, scope } = (await api(pc, 'GET', '/api/groups')).body;
    const after = (await api(pc, 'GET', '/api/state')).body;
    const log = (await api(pc, 'GET', '/api/events?since=0&timeout=0')).body.events.slice(-14).map((e) => `${e.type}${e.data && e.data.scan_id ? '#' + e.data.scan_id : ''}`).join(' ');
    ok(all.length > 100, `groups after the rescan: ${all.length} (scan #${after.scan.id} ${after.scan.status}, job ${after.job.kind} ${after.job.status}; ${fs.readdirSync(path.join(C.home, 'Library/Caches')).length} folders on disk; events: ${log})`);
    await go(pc, C.base, 'browse');
    await tid(pc, 'group').first().waitFor();
    eq(await tid(pc, 'group').count(), 50, 'fifty rows, not all of them');
    const header = await tid(pc, 'browse-header').innerText();
    ok(header.includes(`${scope.items.toLocaleString('en-US')} items`) && header.includes(await fmtC(scope.alloc)), `the header counts the whole scan: ${header}`);
    eq((await tid(pc, 'groups-pager').locator('.muted').innerText()).trim(), `Showing 50 of ${all.length}`, 'the pager says how many there are');
    eq((await tid(pc, 'more-groups').innerText()).trim(), `Show more (${all.length - 50} more groups)`, 'and how many more a click brings');
    await tid(pc, 'more-groups').click();
    eq(await tid(pc, 'group').count(), Math.min(100, all.length), 'a click adds fifty');
    while (await tid(pc, 'more-groups').count()) await tid(pc, 'more-groups').click();
    eq(await tid(pc, 'group').count(), all.length, 'until all are there');
    eq(await tid(pc, 'more-groups').count(), 0, 'and the button goes');
    // sorting reaches groups that are not on the first page
    await go(pc, C.base, 'browse?x=1');
    await tid(pc, 'group').first().waitFor();
    eq(await tid(pc, 'group').count(), 50, 'a fresh page starts with fifty again');
    const keys = () => pc.locator('tbody[data-testid="group"]').evaluateAll((els) => els.map((e) => e.dataset.key));
    await tid(pc, 'sort-groups-name').click();
    const byLabel = all.slice().sort((a, b) => a.label.toLowerCase().localeCompare(b.label.toLowerCase())).map((g) => g.key);
    const shown = await keys();
    eq(shown.length, 50, 'still fifty rows');
    const labels = Object.fromEntries(all.map((g) => [g.key, g.label.toLowerCase()]));
    ok(shown.every((k, i) => i === 0 || labels[shown[i - 1]].localeCompare(labels[k]) <= 0), 'in alphabetical order');
    eq(labels[shown[0]], labels[byLabel[0]], 'and the first one is the first of ALL the groups, not of the first page');
    eq(labels[shown[49]], labels[byLabel[49]], 'as is the fiftieth');
    await tid(pc, 'sort-groups-size').click();
    const sizes = Object.fromEntries(all.map((g) => [g.key, g.alloc]));
    const bySize = await keys();
    eq(sizes[bySize[0]], Math.max(...all.map((g) => g.alloc)), 'by size, the first row is the biggest group of the whole scan');
    ok(bySize.every((k, i) => i === 0 || sizes[bySize[i - 1]] >= sizes[k]), 'biggest first');
    await tid(pc, 'sort-groups-size').click();
    eq(sizes[(await keys())[0]], Math.min(...all.map((g) => g.alloc)), 'and the other way round, the smallest of all');
    // searching covers every group too, and a link to a group far down the list shows that group
    await go(pc, C.base, 'browse?x=2');
    await tid(pc, 'group').first().waitFor();
    await pc.getByPlaceholder('Filter groups…').fill('com.e2e.page5');
    await pc.getByPlaceholder('Filter groups…').press('Tab');   // the filter applies on change
    await pc.waitForFunction(() => document.querySelectorAll('tbody[data-testid="group"]').length <= 12);
    const hits = all.filter((g) => g.key.toLowerCase().includes('com.e2e.page5') || g.label.toLowerCase().includes('com.e2e.page5')).length;
    eq(await tid(pc, 'group').count(), hits, `the search finds all ${hits} matches, wherever they were`);
    const far = all[all.length - 1].key;
    await go(pc, C.base, `browse?group=${encodeURIComponent(far)}`);
    await pc.locator(`[data-testid="group"][data-key="${far}"]`).waitFor({ timeout: 15000 });
    ok(await pc.locator(`[data-testid="group"][data-key="${far}"] table[data-testid="items"]`).isVisible(), 'a link to the last group opens it, although it is far below the first fifty');
    eq(await tid(pc, 'group').count() >= 50, true, 'by showing the list as far as it');
  });

  await step('insights in 简体中文: the language button switches the page, with no raw keys', async () => {
    await go(pc, C.base, 'insights');
    await tid(pc, 'kpis').waitFor();
    await Promise.all([pc.waitForEvent('load'), tid(pc, 'lang-zh-CN').click()]);
    await pc.waitForSelector('#nav a');
    await tid(pc, 'kpis').waitFor();
    eq(await pc.locator('#nav a').nth(1).innerText(), '洞察', 'the nav says 洞察');
    eq(await tid(pc, 'lang-zh-CN').getAttribute('aria-pressed'), 'true', 'and the language button knows');
    eq(await pc.locator('main h1').innerText(), '洞察', 'the title');
    eq(await tid(pc, 'window-7').innerText(), '7 天', 'the window control');
    eq(await tid(pc, 'window-0').innerText(), '全部', 'all');
    eq(await tid(pc, 'kpi-free').locator('.stat-label').innerText(), '可用空间', 'free space');
    eq(await tid(pc, 'kpi-growth').locator('.stat-label').innerText(), '日均增长', 'daily growth');
    ok((await tid(pc, 'kpi-safe-button').innerText()).includes('查看并清理'), 'the button');
    eq(await tid(pc, 'subtabs').getAttribute('aria-label'), '分区', 'the tab bar is named');
    eq(await tid(pc, 'tab-trends').innerText(), '趋势', 'a tab');
    ok((await tid(pc, 'tab-todos').innerText()).includes('待办'), 'and the one that is named by its section');
    ok((await tid(pc, 'comp-show-table').innerText()) === '表格', 'the table switch');
    ok((await pc.locator('.chart-svg').first().evaluate((e) => e.getAttribute('aria-label'))).length > 0, 'charts are named');
    ok((await tid(pc, 'kpi-free').innerText()).includes('对比 30 天前'), 'the period');
    ok((await tid(pc, 'comp-note').innerText()).includes('未扫描'), 'the caption under the first bar');
    eq(await tid(pc, 'section-scanned').locator('h3').innerText(), '扫描到的空间', 'the second bar');
    const heads = [];
    for (const key of ['composition', 'trends', 'results', 'agents', 'todos', 'env']) {
      await tid(pc, `tab-${key}`).click();
      heads.push(...await pc.locator('section h2').allInnerTexts());
      const text = await pc.locator('body').innerText();
      ok(!text.match(rawKey), `no raw key on the ${key} tab: ${(text.match(rawKey) || [''])[0]}`);
      if (key === 'trends') ok((await tid(pc, 'cleanup-marker').first().textContent()).trim().startsWith('释放 '), 'the badge on the line');
    }
    for (const h of ['磁盘去向', '空间变化', '扫描到的空间构成', '增长最快 / 缩小最多', '清理成果', 'AI 代理占用', '待办', '环境']) ok(heads.includes(h), `the section ${h} is there: ${heads.join(' | ')}`);
    const zhPlan = (await insights(30)).history.cleanups.find((c) => c.kind === 'plan').plan_id;
    await go(pc, C.base, `plan/${zhPlan}`);
    await tid(pc, 'compacted-note').waitFor();
    ok((await tid(pc, 'compacted-note').innerText()).includes('7 天'), 'the note on a compacted plan, in Chinese');
    await go(pc, C.base, 'browse');
    await tid(pc, 'group').first().waitFor();
    eq((await pc.locator('table.groups-table thead th').evaluateAll((els) => els.map((e) => e.textContent.trim()))).join('|'), '选择|名称|等级|范围|符合 / 总数|大小|最近变更|展开', 'the Browse table in Chinese');
    await go(pc, C.base, 'browse?report_only=1');
    await tid(pc, 'browse-header').waitFor();
    await tid(pc, 'native').waitFor();
    ok((await pc.locator('main').innerText()).includes('仅展示：由其他工具管理'), 'the tools table is titled in Chinese');
    eq(await tid(pc, 'group').count(), 0, 'with no group table under it');
    await go(pc, C.base, 'browse?group=ollama.models');
    await pc.locator('[data-testid="group"][data-key="ollama.models"]').waitFor({ timeout: 15000 });
    ok((await pc.locator('[data-testid="group"][data-key="ollama.models"] tr.group-row').innerText()).includes('其他工具管理'), 'and the badge of another tool\'s group is in Chinese');
    await go(pc, C.base, 'browse');
    await tid(pc, 'group').first().waitFor();
    ok(/^显示更多（还有 \d+ 个分组）$/.test((await tid(pc, 'more-groups').innerText()).trim()), 'and the button that brings more groups, in Chinese');
    await go(pc, C.base, 'run?tab=archives');
    await tid(pc, 'archives-section').waitFor();
    eq(await pc.locator('#archives-title').innerText(), '可恢复的归档', 'the archives tab in Chinese');
    ok((await tid(pc, 'archives-section').innerText()).includes('归档'), 'with its explanation');
    ok(!(await pc.locator('body').innerText()).match(rawKey), 'no raw key on the archives tab');
    await tid(pc, 'tab-current').click();
    ok(!(await pc.locator('body').innerText()).match(rawKey), 'nor on the other tab of the Cleanup page');
    await go(pc, C.base, 'insights');
    await tid(pc, 'kpis').waitFor();
    await Promise.all([pc.waitForEvent('load'), tid(pc, 'lang-en').click()]);
    await pc.waitForSelector('#nav a');
    eq(await pc.locator('#nav a').first().innerText(), 'Overview', 'and back to English');
  });

  await step('screenshots of Insights on a long history, dark first, then light, at 1280x860 and 390x844', async () => {
    for (const scheme of ['dark', 'light']) {
      await pc.emulateMedia({ colorScheme: scheme });
      for (const [w, h] of [[1280, 860], [390, 844]]) {
        await pc.setViewportSize({ width: w, height: h });
        for (const [name, route] of [['insights-history', 'insights'], ['overview-history', 'overview']]) {
          await go(pc, C.base, route);
          await tid(pc, route === 'insights' ? 'kpis' : 'hero').waitFor();
          await sleep(800);
          const file = `${scheme}-${w}x${h}-${name}.png`;
          await pc.screenshot({ path: path.join(outDir, file) });
          shots.push(file);
        }
        // the composition card on its own: the whole-disk bar, its caption and the scanned-space bar
        await go(pc, C.base, 'insights');
        await tid(pc, 'section-comp').waitFor();
    await sleep(900);   // charts redraw once at their real width; read them after that
        await sleep(600);
        await tid(pc, 'section-comp').screenshot({ path: path.join(outDir, `${scheme}-${w}x${h}-insights-composition.png`) });
        shots.push(`${scheme}-${w}x${h}-insights-composition`);
        // the full page of Insights, one picture per tab
        for (const key of ['composition', 'trends', 'results', 'agents', 'todos', 'env']) {
          await go(pc, C.base, `insights?tab=${key}`);
          await tid(pc, 'kpis').waitFor();
          await sleep(800);
          await pc.screenshot({ path: path.join(outDir, `${scheme}-${w}x${h}-insights-full-${key}.png`), fullPage: true });
          shots.push(`${scheme}-${w}x${h}-insights-full-${key}`);
        }
      }
    }
    await pc.emulateMedia({ colorScheme: 'light' });
    await pc.setViewportSize({ width: 1280, height: 860 });
  });

  await ctxC.close();
  await stopFixture(C);

  // ---------------------------------------------------------------- flow D: no brew, no ollama on the PATH
  const D = await startFixture([], { withTools: false });
  const ctxD = await browser.newContext({ viewport: { width: 1280, height: 860 } });
  await ctxD.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: D.base });
  const pd = await newPage(ctxD, 'D');

  await step('a tool that cannot run says why and offers its command to copy instead of a Run button', async () => {
    await pd.goto(D.url);
    await pd.waitForSelector('#nav a');
    await go(pd, D.base, 'browse?report_only=1');
    await tid(pd, 'native').waitFor();
    const tools = (await api(pd, 'GET', '/api/native')).body.tools;
    ok(tools.length >= 2 && tools.every((x) => !x.runnable), `nothing can run: ${tools.map((x) => `${x.rule_id}: ${x.reason}`).join(' | ')}`);
    eq(await pd.locator('[data-testid="native"] button[data-action="run"]').count(), 0, 'no Run button anywhere');
    for (const tool of tools) {
      const row = tid(pd, 'native-row').and(pd.locator(`[data-rule="${tool.rule_id}"]`));
      eq((await row.locator('[data-testid="native-reason"]').innerText()).trim(), tool.reason, `${tool.rule_id}: the reason is the API's, in words`);
      ok((await row.locator('.cmd-pill code').first().innerText()) === tool.command, `${tool.rule_id}: the command is shown`);
      await row.getByRole('button', { name: 'Copy' }).click();
      await pd.waitForFunction(async (cmd) => (await navigator.clipboard.readText()) === cmd, tool.command);
      ok(true, `${tool.rule_id}: and copied`);
    }
    await go(pd, D.base, 'insights?tab=todos');
    await tid(pd, 'section-todos').waitFor();
    const ins = (await api(pd, 'GET', '/api/insights?window=30')).body;
    for (const todo of ins.todos.filter((x) => x.id.startsWith('report_only'))) {
      eq(todo.runnable, false, `${todo.id} cannot run here`);
      const row = tid(pd, 'todo').and(pd.locator(`[data-id="${todo.id}"]`));
      eq(await row.locator('[data-testid="todo-run"]').count(), 0, 'so its todo has no Run button');
      eq(await row.getByRole('button', { name: 'Copy' }).count(), 1, 'but a Copy button');
    }
  });

  await ctxD.close();
  await stopFixture(D);

  // console errors and CSP violations over the whole run
  const unexpected = [];
  let tolerated = expectedHttpErrors;
  for (const line of consoleErrors) {
    if (tolerated > 0 && /Failed to load resource: the server responded with a status of (400|409|422)/.test(line)) { tolerated -= 1; continue; }
    unexpected.push(line);
  }
  currentStep = 'console';
  eq(tolerated, 0, 'every deliberate refusal was logged by the browser exactly once');
  eq(unexpected.length, 0, `no console errors or CSP violations:\n${unexpected.join('\n')}`);
  results.push({ name: 'no console errors or CSP violations', ok: true });
  console.log('ok   no console errors or CSP violations');
} catch (e) {
  failure = e;
}

await browser.close();
for (const fx of fixtures) {
  if (fx.child.exitCode === null) {
    fx.child.removeAllListeners('exit');
    fx.child.kill('SIGTERM');   // the fixture removes its fake home when it is stopped
    await new Promise((r) => fx.child.once('exit', r));
  }
}
console.log(`\nscreenshots: ${outDir}`);
const failed = results.filter((r) => !r.ok);
console.log(`${results.length - failed.length} of ${results.length} steps passed${failure ? ' (stopped at the first failure)' : ''}`);
if (consoleErrors.length && failure) console.log(`console messages so far:\n${consoleErrors.join('\n')}`);
process.exit(failure ? 1 : 0);
