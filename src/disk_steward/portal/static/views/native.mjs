// Tools that manage their own space (the report-only groups): what each holds, the command that cleans it, and a button
// that runs exactly that command with its output live. A tool that cannot run says why and offers the command to copy.
import { api, ApiError } from '../api.mjs';
import { delegate, h, replace } from '../dom.mjs';
import { fmtBytes } from '../format.mjs';
import { t } from '../i18n.mjs';
import { icon } from '../icons.mjs';
import { on, setJobFromResponse, state } from '../state.mjs';
import { colgroup, dotLabel, nextSort, plainTh, sortRows, sortTh } from '../tables.mjs';
import { copyButton, modal, reportError } from '../ui.mjs';

const SHOWN_LINES = 12;

const running = () => state.job && state.job.kind === 'native' && state.job.status === 'running';
const isRow = (job, rule, target) => job.rule_id === rule && (job.target || null) === (target || null);
const firstSentence = (text) => (text || '').split(/(?<=\.)\s/)[0];
const sizeOf = (x) => (typeof x.bytes === 'number' ? fmtBytes(x.bytes) : String(x.size));

export function create() {
  let root = null;
  let tools = null;           // null until the first answer
  let effects = {};           // group key -> what the tool's space is, in one line
  let sort = { col: 'size', dir: 'desc' };
  let result = null;          // what the last run did, until the next one starts
  let lines = [];             // the output of the run that is going on
  let logEl = null;
  const disposers = [];

  // data ----------------------------------------------------------------------------------------------------

  async function load() {
    tools = (await api.get('/api/native')).tools.filter((x) => x.bytes > 0);
    if (tools.length && !Object.keys(effects).length) {
      const groups = (await api.get('/api/groups')).groups;
      effects = Object.fromEntries(groups.map((g) => [g.key, firstSentence(g.effect)]));
    }
    render();
  }

  // rendering -----------------------------------------------------------------------------------------------

  const command = (text, title) => h('span', { class: 'cmd-pill', title: title || text }, icon('terminal', 14), h('code', { text }));

  // the tool runs, then the numbers are measured again (a rescan can not be cancelled from here)
  function progress(job) {
    const rescanning = job.phase === 'rescan';
    const label = t(rescanning ? 'portal.native.rescanning' : 'portal.native.running');
    return h('div', { class: 'native-progress', role: 'status', 'data-testid': 'native-running' },
      h('progress', { 'aria-label': label }), h('span', { class: 'muted', text: label }),
      rescanning ? null : h('button', { class: 'btn small', data: { action: 'cancel' }, 'data-testid': 'native-cancel', text: t('portal.common.cancel') }));
  }

  function actionCell(tool, target) {
    const job = state.job;
    const here = running() && isRow(job, tool.rule_id, target && target.name);
    if (here) return progress(job);
    const cmd = target ? target.command : tool.command;
    if (tool.runnable && (target || !tool.targets)) {
      return h('button', { class: 'btn small', disabled: running(), data: { action: 'run', rule: tool.rule_id, target: target ? target.name : null },
        'data-testid': target ? 'native-delete-model' : 'native-run' },
      icon(target ? 'trash' : 'play', 14), t(target ? 'portal.native.delete_model' : 'portal.native.run'));
    }
    if (tool.targets) return null;   // the models carry the buttons
    return h('div', { class: 'row-actions' }, tool.reason ? h('span', { class: 'sub reason', 'data-testid': 'native-reason', text: tool.reason }) : null,
      cmd ? copyButton(cmd) : null);
  }

  function toolRows(tool) {
    const rows = [h('tr', { class: 'native-row', 'data-testid': 'native-row', 'data-rule': tool.rule_id },
      h('td', { class: 'c-name' }, h('span', { class: 'row-title', text: tool.label }),
        tool.tool ? h('span', { class: 'sub', title: tool.tool, text: [tool.tool.split('/').pop(), tool.tool_version].filter(Boolean).join(' ') }) : null,
        h('span', { class: 'sub meta-sm', text: fmtBytes(tool.bytes) }),
        h('span', { class: 'sub meta-sm' }, tool.command ? command(tool.command) : null)),
      h('td', { class: 'c-size num hide-sm', text: fmtBytes(tool.bytes) }),
      h('td', { class: 'c-cmd hide-sm' }, tool.command ? command(tool.command) : null),
      h('td', { class: 'c-action' }, actionCell(tool, null)))];
    for (const target of tool.targets || []) {
      rows.push(h('tr', { class: 'native-row native-target', 'data-testid': 'native-target', 'data-rule': tool.rule_id, 'data-target': target.name },
        h('td', { class: 'c-name' }, h('span', { class: 'row-title target-name', text: target.name }),
          h('span', { class: 'sub meta-sm', text: sizeOf(target) }),
          h('span', { class: 'sub meta-sm' }, command(target.command))),
        h('td', { class: 'c-size num hide-sm', text: sizeOf(target) }),
        h('td', { class: 'c-cmd hide-sm' }, command(target.command)),
        h('td', { class: 'c-action' }, actionCell(tool, target))));
    }
    const job = state.job;
    if (running() && tools.some((x) => x.rule_id === job.rule_id) && job.rule_id === tool.rule_id) {
      rows.push(h('tr', { class: 'native-live', 'data-testid': 'native-live' }, h('td', { colspan: 4 },
        h('div', { class: 'live-head' }, dotLabel('report_only', t('portal.native.output')), command(job.command || '')),
        logEl = h('pre', { class: 'native-log', tabindex: '0', 'data-testid': 'native-log', 'aria-live': 'off' }))));
      paintLog();
    }
    return rows;
  }

  function resultBanner() {
    if (!result) return null;
    const bad = result.status && result.status !== 'ok';
    return h('div', { class: `banner banner-${bad ? 'warn' : 'ok'}`, role: 'status', 'data-testid': 'native-result' },
      h('div', {}, bad
        ? t('portal.native.failed', { command: result.command || '', reason: result.error ? result.error.text : t('portal.native.exit_code', { code: String(result.exit_code) }) })
        : t('portal.native.done', { command: result.command || '', freed: fmtBytes(result.freed || 0), before: fmtBytes(result.before || 0), after: fmtBytes(result.after || 0) })),
      result.output_tail && result.output_tail.length ? h('details', { class: 'fold' }, h('summary', { text: t('portal.native.output') }),
        h('pre', { class: 'native-log', text: result.output_tail.join('\n') })) : null,
      h('button', { class: 'btn small', data: { action: 'dismiss' }, text: t('portal.common.dismiss') }));
  }

  function commandOf(rule, target) {
    const tool = (tools || []).find((x) => x.rule_id === rule);
    if (!tool) return '';
    const model = target && (tool.targets || []).find((x) => x.name === target);
    return model ? model.command : tool.command;
  }

  const GETTERS = { tool: (x) => x.label.toLowerCase(), size: (x) => x.bytes };

  function render() {
    if (!root || tools === null) return;
    if (!tools.length && !result) { replace(root); return; }
    const th = (label, col, cls) => sortTh(label, col, sort, 'native', cls);
    replace(root, h('section', { class: 'card native-card', 'data-testid': 'native' },
      h('div', { class: 'section-head' }, h('div', { class: 'section-title' }, h('h2', { text: t('portal.native.title') }), h('p', { text: t('portal.native.sub') }))),
      resultBanner(),
      tools.length ? h('div', { class: 'table-card' }, h('table', { class: 'data native-table' },
        colgroup('c-name', 'c-size hide-sm', 'c-cmd hide-sm', 'c-action'),
        h('thead', {}, h('tr', {}, th(t('portal.col.tool'), 'tool', 'c-name'), th(t('portal.col.size'), 'size', 'c-size num hide-sm'),
          plainTh(t('portal.col.command'), 'c-cmd hide-sm'), plainTh(t('portal.col.action'), 'c-action'))),
        h('tbody', {}, sortRows(tools, sort, GETTERS).flatMap(toolRows)))) : null));
  }

  function paintLog() {
    if (!logEl) return;
    const shown = lines.slice(-SHOWN_LINES);
    logEl.textContent = shown.length ? shown.join('\n') : t('portal.native.waiting');
    logEl.scrollTop = logEl.scrollHeight;   // the newest line stays in view
  }

  // actions -------------------------------------------------------------------------------------------------

  async function run(el) {
    const tool = tools.find((x) => x.rule_id === el.dataset.rule);
    const target = tool.targets && el.dataset.target ? tool.targets.find((x) => x.name === el.dataset.target) : null;
    const text = target ? target.command : tool.command;
    const size = target ? sizeOf(target) : fmtBytes(tool.bytes);
    const what = target ? t('portal.native.what_model', { name: target.name }) : (effects[tool.group] || tool.label);
    const answer = await modal({
      title: t(target ? 'portal.native.confirm_model' : 'portal.native.confirm_title', { name: target ? target.name : tool.label }),
      body: [h('p', { class: 'confirm-what', text: what }),
        h('dl', { class: 'confirm-facts' },
          h('div', {}, h('dt', { text: t('portal.native.command') }), h('dd', {}, h('code', { class: 'confirm-command', 'data-testid': 'native-confirm-command', text: text }))),
          h('div', {}, h('dt', { text: t('portal.native.size_now') }), h('dd', { 'data-testid': 'native-confirm-size', text: size }))),
        h('p', { class: 'muted', text: t('portal.native.confirm_note') })],
      buttons: [{ label: t('portal.common.cancel'), value: 'no' },
        { label: t('portal.native.run'), value: 'yes', primary: true, solid: true, testid: 'native-confirm-yes' }] });
    if (answer !== 'yes') return;
    result = null;
    lines = [];
    try {
      const r = await api.post('/api/native/run', { rule_id: tool.rule_id, target: target ? target.name : undefined, command: text });
      setJobFromResponse(r.job);
    } catch (e) {
      if (!(e instanceof ApiError)) throw e;
      reportError(e);
      await load();   // what could run a moment ago may not now
    }
    render();
  }

  const handlers = {
    run,
    cancel: async () => { setJobFromResponse((await api.post('/api/job/cancel', {})).job); },
    dismiss: () => { result = null; render(); },
    sort: (el) => { sort = nextSort(sort, el.dataset.col, el.dataset.col === 'size' ? 'desc' : 'asc'); render(); },
  };

  function onEvents(events) {
    let reload = false;
    for (const ev of events) {
      if (ev.type === 'native.output') {
        // the job already holds the last lines (it is read before the events are handed on): one source, no doubles
        if (state.job && state.job.kind === 'native' && Array.isArray(state.job.output)) lines = state.job.output.slice();
        else lines.push(ev.data.line);
        paintLog();
      } else if (ev.type === 'native.done') {
        result = { ...ev.data, status: ev.data.exit_code === 0 ? 'ok' : 'failed', command: ev.data.command || commandOf(ev.data.rule_id, ev.data.target) };
        reload = true;
      } else if (ev.type === 'native.failed') {
        result = { command: ev.data.command || (ev.data.details && ev.data.details.command) || '', status: 'error', exit_code: null, error: { text: ev.data.message || ev.data.code } };
        reload = true;
      } else if (ev.type === 'scan.done') {
        reload = true;
      }
    }
    if (reload) load().catch(reportError);
  }

  return {
    async mount(el) {
      root = el;
      delegate(root, 'click', 'action', handlers);
      lines = state.job && state.job.kind === 'native' && Array.isArray(state.job.output) ? state.job.output.slice() : [];
      disposers.push(on('events', onEvents));
      disposers.push(on('job', () => {
        if (state.job && state.job.kind === 'native' && state.job.status === 'running' && Array.isArray(state.job.output)) {
          lines = state.job.output.slice();   // the events carry the first lines only; the job keeps the last ones
        }
        if (tools !== null) render();
      }));
      await load();
    },
    // the host drew a new page around the section: draw the section again into its new place
    attach(el) {
      root = el;
      delegate(root, 'click', 'action', handlers);
      render();
    },
    unmount() { disposers.forEach((f) => f()); },
  };
}
