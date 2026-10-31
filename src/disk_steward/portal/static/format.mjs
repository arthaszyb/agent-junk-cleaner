// Number, size, time and age formatting. Sizes match the CLI (binary units, two decimals).
import { t } from './i18n.mjs';

function fixed2(v) {
  // The CLI rounds half to even on exact ties (1.125 -> 1.12); toFixed would round them up.
  const scaled = v * 100;
  const floor = Math.floor(scaled);
  if (scaled - floor === 0.5) return ((floor % 2 === 0 ? floor : floor + 1) / 100).toFixed(2);
  return v.toFixed(2);
}

export function fmtBytes(n, signed = false) {
  const sign = n < 0 ? '-' : (signed && n > 0 ? '+' : '');
  const abs = Math.abs(n);
  if (abs < 1024) return `${sign}${abs} B`;
  let value = abs;
  for (const unit of ['KiB', 'MiB', 'GiB']) {
    value /= 1024;
    if (value < 1024 || unit === 'GiB') return `${sign}${fixed2(value)} ${unit}`;
  }
  throw new Error('unreachable');
}

const pad = (n) => String(n).padStart(2, '0');

export function fmtTime(ts) {
  if (ts === null || ts === undefined) return '-';
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// 2026-10-29
export function fmtDate(ts) {
  if (ts === null || ts === undefined) return '-';
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// "3.4 s", "42 s", "3 m 12 s": the unit letters read the same in every language.
export function fmtDuration(seconds) {
  if (seconds < 10) return `${seconds.toFixed(1)} s`;
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const whole = Math.round(seconds);
  return `${Math.floor(whole / 60)} m ${whole % 60} s`;
}

// 0.347 -> "35%", 0.0123 -> "1.2%", 0.00002 -> "<0.1%"
export function fmtPct(share) {
  if (share > 0 && share < 0.001) return '<0.1%';
  return `${(share * 100).toFixed(share > 0 && share < 0.1 ? 1 : 0)}%`;
}

export function plural(key, n, args = {}) {
  return t(`${key}_${n === 1 ? 'one' : 'other'}`, { count: n, ...args });
}

// "16 days ago". `days` comes from the server; this only words it.
export function fmtAgo(days) {
  if (days < 1 / 24) return t('portal.ago.now');
  if (days < 1) return plural('portal.ago.hours', Math.floor(days * 24));
  if (days < 60) return plural('portal.ago.days', Math.floor(days));
  if (days < 730) return plural('portal.ago.months', Math.floor(days / 30));
  return plural('portal.ago.years', Math.floor(days / 365));
}

export function fmtAge(days, basis) {
  return t(basis === 'last_used' ? 'portal.age.last_used' : 'portal.age.changed', { ago: fmtAgo(days) });
}

export function fmtDays(days) { return String(Number(days)); }

// 33937 -> "33,937". Counts are written the same way in every language.
export function fmtCount(n) { return Number(n).toLocaleString('en-US'); }

// plural() with the count written with thousands separators.
export function pluralCount(key, n, args = {}) {
  return t(`${key}_${n === 1 ? 'one' : 'other'}`, { ...args, count: fmtCount(n) });
}

// The Browse address of a filter ({tier, state, days, ...}) or of one group.
export function browseHref(filter) {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(filter)) if (v !== undefined && v !== null && v !== '') params.set(k, String(v));
  const text = params.toString();
  return `#/browse${text ? `?${text}` : ''}`;
}
