// Shared client state and the server-event loop. Views subscribe with on() and must call the returned function.
import { api, ApiError } from './api.mjs';

export const state = { locale: 'en', version: 0, job: null, scan: null, settings: null, recovery: null, log: [],
  offline: false };
const handlers = {};
const LOG_LIMIT = 300;
const JOB_POLL_MS = 1500;
let stopped = false;

export function on(type, fn) {
  (handlers[type] ||= new Set()).add(fn);
  return () => handlers[type].delete(fn);
}

function emit(type, data) {
  for (const fn of [...(handlers[type] || [])]) fn(data);
}

function apply(s) {
  state.locale = s.locale;
  state.settings = s.settings;
  state.scan = s.scan;
  state.recovery = s.recovery;
  setJob(s.job);
}

function setJob(job) {
  const before = state.job;
  state.job = job;
  emit('job', { job, before });
}

export async function loadState(initial = false) {
  const s = await api.get('/api/state');
  if (initial) state.version = s.version;   // later calls must not skip events the poll has not delivered yet
  apply(s);
  return s;
}

export async function refreshJob() {
  setJob((await api.get('/api/job')).job);
  return state.job;
}

export function setJobFromResponse(job) { setJob(job); }

function after(events) {
  const types = events.map((e) => e.type);
  if (types.some((x) => x === 'scan.done' || x === 'scan.failed' || x === 'run.finished' || x === 'recover.done' || x === 'data_dir.recreated' || x === 'native.done' || x === 'native.failed')) {
    return loadState();
  }
  if (types.some((x) => x.startsWith('run.') || x.startsWith('scan.') || x.startsWith('native.'))) return refreshJob();
  return null;
}

async function loop() {
  while (!stopped) {
    try {
      const r = await api.get('/api/events', { since: state.version, timeout: 25 });
      if (state.offline) { state.offline = false; emit('connection', false); }
      state.version = r.version;
      if (r.reset) {
        await loadState();
        emit('reset');
      }
      for (const ev of r.events) {
        if (ev.type === 'entry.done') {
          state.log.push(ev);
          if (state.log.length > LOG_LIMIT) state.log.shift();
        }
      }
      if (r.events.length) {
        await after(r.events);
        emit('events', r.events);
        for (const ev of r.events) if (ev.type === 'data_dir.recreated') emit('data_dir_recreated', ev.data);
      }
    } catch (e) {
      if (stopped || (e instanceof ApiError && e.status === 401)) return;
      if (!state.offline) { state.offline = true; emit('connection', true); }
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
  }
}

export function startEvents() {
  window.addEventListener('pagehide', () => { stopped = true; });
  loop();
  // An event can be missed (the server keeps the last 500); while something runs, look at the job anyway.
  // A lost connection is reported by the event loop, so a failed look here needs no second report.
  setInterval(() => {
    if (state.job && (state.job.status === 'running' || state.job.status === 'paused')) refreshJob().catch(() => {});
  }, JOB_POLL_MS);
}
