// Pure helpers for the charts: scales, rounded ticks, time ticks, segment layout and path geometry.
// No DOM and no imports, so a test can load this file in node.

const BYTE_UNITS = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];

export function linear(d0, d1, r0, r1) {
  const span = d1 - d0;
  return (v) => (span === 0 ? (r0 + r1) / 2 : r0 + ((v - d0) / span) * (r1 - r0));
}

// 1, 2, 5 times a power of ten: the step that gives about `target` intervals over `span`.
export function niceStep(span, target = 5) {
  if (!(span > 0)) return 1;
  const raw = span / target;
  const pow = 10 ** Math.floor(Math.log10(raw));
  const f = raw / pow;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * pow;
}

export function niceTicks(min, max, target = 5) {
  if (max === min) {
    const pad = Math.abs(min) * 0.05 || 1;
    min -= pad;
    max += pad;
  }
  const step = niceStep(max - min, target);
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + step / 2; v += step) ticks.push(Math.round(v / step) * step);
  return { ticks, min: lo, max: hi, step };
}

function trim(n) {
  return String(Number(n.toFixed(2)));
}

// Axis ticks of a byte scale in binary units: "0", "10 GiB", "20 GiB". The unit follows the largest tick.
// zero: true starts the axis at 0 (columns); false lets a line sit on a narrower, rounded range.
export function byteTicks(min, max, { target = 5, zero = false } = {}) {
  const top = Math.max(Math.abs(min), Math.abs(max), 1);
  const k = Math.min(BYTE_UNITS.length - 1, Math.max(0, Math.floor(Math.log(top) / Math.log(1024))));
  const factor = 1024 ** k;
  const lo = zero ? Math.min(0, min / factor) : min / factor;
  const hi = max / factor;
  const nice = niceTicks(zero ? Math.min(lo, 0) : lo, zero && hi <= 0 ? 1 : hi, target);
  const unit = BYTE_UNITS[k];
  return {
    unit, factor, min: nice.min * factor, max: nice.max * factor,
    ticks: nice.ticks.map((v) => ({ value: v * factor, label: v === 0 ? '0' : `${trim(v)} ${unit}` })),
  };
}

const MIN = 60;
const HOUR = 3600;
const DAY = 86400;
const TIME_STEPS = [MIN, 5 * MIN, 10 * MIN, 15 * MIN, 30 * MIN, HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY, 7 * DAY, 14 * DAY,
  30 * DAY, 90 * DAY, 180 * DAY, 365 * DAY];

// Ticks of an epoch-seconds axis at round local times. Returns {ticks: [epoch s], step}.
export function timeTicks(t0, t1, target = 5) {
  const span = Math.max(t1 - t0, 1);
  const want = span / target;
  const step = TIME_STEPS.find((x) => x >= want) || TIME_STEPS[TIME_STEPS.length - 1];
  const ticks = [];
  const d = new Date(t0 * 1000);
  let first;
  if (step >= DAY) {
    d.setHours(0, 0, 0, 0);
    first = d.getTime() / 1000;
  } else {
    first = Math.floor(t0 / step) * step;
  }
  for (let t = first; t <= t1 + 1; t += step) {
    if (t >= t0 - 1) ticks.push(t);
    if (ticks.length > 24) break;
  }
  return { ticks, step };
}

const pad = (n) => String(n).padStart(2, '0');

// "10-28" for day steps, "10-28 14:00" for a few days, "14:05" inside a day.
export function timeLabel(ts, spanSeconds) {
  const d = new Date(ts * 1000);
  const day = `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const clock = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (spanSeconds <= 2 * DAY) return spanSeconds <= 6 * HOUR ? clock : `${day} ${clock}`;
  return day;
}

// Segment widths of a 100% bar: proportional to the values, `gap` pixels of surface between neighbours, and the
// widths plus the gaps add up to exactly `width`. Zero values take no room and no gap.
export function segmentLayout(values, width, gap = 2) {
  const live = values.filter((v) => v > 0).length;
  const total = values.reduce((a, v) => a + (v > 0 ? v : 0), 0);
  const room = Math.max(0, width - gap * Math.max(0, live - 1));
  let x = 0;
  return values.map((v) => {
    if (!(v > 0) || total === 0) return { x, w: 0 };
    const w = (v / total) * room;
    const out = { x, w };
    x += w + gap;
    return out;
  });
}

// A rectangle path with a radius per corner ({tl, tr, br, bl}); a radius never exceeds half the shorter side.
export function roundedRect(x, y, w, h, radii = {}) {
  const limit = Math.max(0, Math.min(w, h) / 2);
  const k = (name) => Math.min(radii[name] || 0, limit);
  const tl = k('tl');
  const tr = k('tr');
  const br = k('br');
  const bl = k('bl');
  const f = (n) => Number(n.toFixed(2));
  return [`M${f(x + tl)} ${f(y)}`, `H${f(x + w - tr)}`, tr ? `A${tr} ${tr} 0 0 1 ${f(x + w)} ${f(y + tr)}` : '',
    `V${f(y + h - br)}`, br ? `A${br} ${br} 0 0 1 ${f(x + w - br)} ${f(y + h)}` : '', `H${f(x + bl)}`,
    bl ? `A${bl} ${bl} 0 0 1 ${f(x)} ${f(y + h - bl)}` : '', `V${f(y + tl)}`, tl ? `A${tl} ${tl} 0 0 1 ${f(x + tl)} ${f(y)}` : '', 'Z']
    .filter(Boolean).join('');
}

// Index of the value nearest to x in an ascending list of pixel positions.
export function nearest(xs, x) {
  let best = 0;
  for (let i = 1; i < xs.length; i += 1) if (Math.abs(xs[i] - x) < Math.abs(xs[best] - x)) best = i;
  return best;
}

// How full a volume is, as the status it deserves: 'ok', 'warn' from 80% used, 'crit' from 90%.
export function diskLevel(used, total) {
  const share = total > 0 ? used / total : 0;
  return share >= 0.9 ? 'crit' : share >= 0.8 ? 'warn' : 'ok';
}
