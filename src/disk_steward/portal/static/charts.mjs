// Vanilla SVG charts: stackedBar100, lineArea, stackedColumns, rowBars (plain and diverging), sparkline and legend.
// Colours come from CSS (classes s-<entity>, tokens in styles.css), never from this file, so light and dark share one
// drawing. Every chart redraws when its box changes width, every mark has a tooltip on hover and on keyboard focus, and
// no text is ever drawn into a place it does not fit: it is left out and the legend, tooltip and table carry it.
import { h, replace } from './dom.mjs';
import { fmtPct as pctText } from './format.mjs';
import { byteTicks, diskLevel, linear, nearest, roundedRect, segmentLayout, timeLabel, timeTicks } from './scale.mjs';
import { s } from './svg.mjs';

const GAP = 2;          // surface between touching marks
const BAR_MAX = 24;     // thickest bar or column
const END = 4;          // rounded data end
const FIRST_PAINT = 720;

let measure = null;
let family = '';

// Rendered width of a text, so a label is only placed where it fits.
export function textWidth(text, size = 12, weight = 400) {
  if (!measure) {
    measure = document.createElement('canvas').getContext('2d');
    family = getComputedStyle(document.body).fontFamily;
  }
  measure.font = `${weight} ${size}px ${family}`;
  return measure.measureText(text).width;
}

// Room for the tick labels of a y axis, so the widest of them is never cut at the edge of the chart.
function axisRoom(axis) {
  return Math.ceil(Math.max(...axis.ticks.map((tick) => textWidth(tick.label)))) + 18;
}

// A box that draws itself at its own width, and again whenever that changes.
function responsive(draw, className = 'chart-host') {
  const host = h('div', { class: className });
  let last = 0;
  const paint = (w) => {
    last = w;
    replace(host, draw(w));
  };
  const ro = new ResizeObserver((entries) => {
    const w = Math.floor(entries[0].contentRect.width);
    if (w > 0 && w !== last) paint(w);
  });
  paint(FIRST_PAINT);
  ro.observe(host);
  host.destroy = () => ro.disconnect();
  return host;
}

export function destroyAll(list) {
  for (const el of list.splice(0)) if (el.destroy) el.destroy();
}

// tooltip ----------------------------------------------------------------------------------------------------

function makeTip(box) {
  const el = h('div', { class: 'chart-tip', role: 'tooltip', hidden: true });
  box.append(el);
  return {
    el,
    show(x, y, tip) {
      replace(el, h('div', { class: 'tip-title', text: tip.title }), (tip.rows || []).map((row) =>
        h('div', { class: 'tip-row' }, h('span', { class: `tip-key ${row.cls || ''}` }), h('span', { class: 'tip-label', text: row.label }),
          h('strong', { class: 'tip-value', text: row.value }))));
      el.hidden = false;
      const width = el.offsetWidth;
      const height = el.offsetHeight;
      const room = box.clientWidth;
      let left = x + 14;
      if (left + width > room) left = x - width - 14;
      left = Math.max(0, Math.min(left, room - width));
      let top = y - height - 14;
      if (top < 0) top = y + 18;
      el.style.left = `${Math.round(left)}px`;
      el.style.top = `${Math.round(top)}px`;
    },
    hide() { el.hidden = true; },
  };
}

// Hover and keyboard focus show the same tooltip. `tipFor()` is read when it opens.
function bindTip(el, box, tip, tipFor, anchor, hooks = {}) {
  const at = (x, y) => {
    if (hooks.open) hooks.open();
    tip.show(x, y, tipFor());
  };
  const close = () => {
    tip.hide();
    if (hooks.close) hooks.close();
  };
  el.addEventListener('pointermove', (ev) => {
    const b = box.getBoundingClientRect();
    at(ev.clientX - b.left, ev.clientY - b.top);
  });
  el.addEventListener('pointerleave', close);
  el.addEventListener('focus', () => {
    const b = box.getBoundingClientRect();
    const r = (anchor ? anchor() : el).getBoundingClientRect();
    at(r.left + r.width / 2 - b.left, r.top - b.top);
  });
  el.addEventListener('blur', close);
}

// One tab stop for a row of marks; the arrow keys, Home and End move along it.
function roving(items, start = items.length - 1) {
  items.forEach((el, i) => el.setAttribute('tabindex', i === start ? '0' : '-1'));
  items.forEach((el, i) => el.addEventListener('keydown', (ev) => {
    const to = ev.key === 'ArrowRight' || ev.key === 'ArrowDown' ? i + 1 : ev.key === 'ArrowLeft' || ev.key === 'ArrowUp' ? i - 1
      : ev.key === 'Home' ? 0 : ev.key === 'End' ? items.length - 1 : null;
    if (to === null || to < 0 || to >= items.length) return;
    ev.preventDefault();
    items.forEach((x, j) => x.setAttribute('tabindex', j === to ? '0' : '-1'));
    items[to].focus();
  }));
}

// legend ----------------------------------------------------------------------------------------------------

// items: [{key, label}]. A chart of one series has no legend: its title names it.
export function legend(items) {
  return h('ul', { class: 'legend', 'data-testid': 'legend' }, items.map((it) =>
    h('li', { class: 'legend-item', 'data-key': it.key }, h('span', { class: `swatch s-${it.key}`, 'aria-hidden': 'true' }), h('span', { text: it.label }))));
}

// stackedBar100 ----------------------------------------------------------------------------------------------

// segments: [{key, label, value, href?, hint?, tip}] in drawing order. Returns the bar and its legend, which names every
// segment, links where a link exists and carries the value of the ones too narrow to label.
export function stackedBar100({ segments, ariaLabel, valueText, prefix = '' }) {
  const total = segments.reduce((a, x) => a + Math.max(0, x.value), 0);
  const box = h('div', { class: 'chart-box' });
  const tip = makeTip(box);
  const bar = responsive((w) => {
    const height = BAR_MAX;
    const lay = segmentLayout(segments.map((x) => x.value), w, GAP);
    const seen = lay.map((l, i) => i).filter((i) => lay[i].w >= 1.5);   // a sliver cannot carry the rounded end of the bar
    const first = seen[0];
    const last = seen[seen.length - 1];
    const groups = segments.map((seg, i) => {
      const { x, w: sw } = lay[i];
      if (sw <= 0) return null;
      const radii = { tl: i === first ? END + 2 : 0, bl: i === first ? END + 2 : 0, tr: i === last ? END + 2 : 0, br: i === last ? END + 2 : 0 };
      const share = seg.value / total;
      const candidates = [`${seg.label} ${pctText(share)}`, seg.label, pctText(share)];
      const fit = candidates.find((text) => textWidth(text, 12, 600) + 16 <= sw);
      const hitW = Math.max(sw, 12);
      const g = s('g', { class: `mark seg-g s-${seg.key}`, role: 'img', tabindex: 0, 'aria-label': `${seg.label}: ${valueText(seg.value)} (${pctText(share)})`,
        'data-testid': `${prefix}seg-${seg.key}`, 'data-key': seg.key, 'data-bytes': seg.value },
      s('path', { class: 'seg', d: roundedRect(x, 0, sw, height, radii) }),
      fit ? s('text', { class: `seg-label on-${seg.key}`, x: x + sw / 2, y: height / 2 + 4, 'text-anchor': 'middle', text: fit }) : null,
      s('rect', { class: 'hit', x: x + sw / 2 - hitW / 2, y: -4, width: hitW, height: height + 8, fill: 'transparent' }));
      bindTip(g, box, tip, () => seg.tip, null);
      return g;
    });
    return s('svg', { class: 'chart-svg', width: w, height, viewBox: `0 0 ${w} ${height}`, role: 'group', 'aria-label': ariaLabel }, groups);
  });
  box.append(bar);
  const items = segments.map((seg) => {
    const share = total ? seg.value / total : 0;
    const inner = [h('span', { class: `swatch s-${seg.key}`, 'aria-hidden': 'true' }), h('span', { class: 'legend-label', text: seg.label }),
      h('span', { class: 'legend-value', text: valueText(seg.value) }), h('span', { class: 'legend-share', text: pctText(share) })];
    const attrs = { class: 'legend-item', 'data-testid': `${prefix}legend-${seg.key}`, 'data-key': seg.key };
    if (seg.href) return h('li', {}, h('a', { ...attrs, href: seg.href }, inner));
    if (seg.hint) {
      const id = `${prefix}hint-${seg.key}`;
      return h('li', {}, h('span', { ...attrs, tabindex: '0', 'aria-describedby': id }, inner, h('span', { class: 'hint', id, role: 'tooltip', text: seg.hint })));
    }
    return h('li', {}, h('span', attrs, inner));
  });
  const wrap = h('div', { class: 'stacked-bar' }, box, h('ul', { class: 'legend legend-values' }, items));
  wrap.destroy = () => bar.destroy();
  return wrap;
}

// lineArea ---------------------------------------------------------------------------------------------------

// points: [{t, v, id, tip}] ascending in t. events: [{t, badge, href?, tip}] drawn as hairlines on the time axis, each
// with a small badge on top; badges that would overlap stagger onto a second row and, failing that, lose their text.
export function lineArea({ points, events = [], ariaLabel }) {
  const box = h('div', { class: 'chart-box' });
  const tip = makeTip(box);
  const host = responsive((w) => {
    const compact = w < 520;
    const height = compact ? 230 : 280;
    const innerPad = 14;
    const times = points.map((p) => p.t).concat(events.map((e) => e.t));
    const t0 = Math.min(...times);
    const t1 = Math.max(...times);
    const vals = points.map((p) => p.v);
    const lo = Math.min(...vals);
    const hi = Math.max(...vals);
    const pad = (hi - lo) * 0.18 || hi * 0.02 || 1;
    const yAxis = byteTicks(Math.max(0, lo - pad), hi + pad, { target: compact ? 4 : 5 });
    const m = { l: axisRoom(yAxis), r: 18, t: events.length ? 38 : 16, b: 30 };   // the ticks decide how wide the left margin is
    const x = linear(t0, t1, m.l + innerPad, w - m.r - innerPad);
    const y = linear(yAxis.min, yAxis.max, height - m.b, m.t);
    const out = [];
    for (const tick of yAxis.ticks) {
      const ty = y(tick.value);
      out.push(s('line', { class: 'grid', x1: m.l, x2: w - m.r, y1: ty, y2: ty }));
      out.push(s('text', { class: 'tick', x: m.l - 10, y: ty + 4, 'text-anchor': 'end', text: tick.label }));
    }
    out.push(s('line', { class: 'axis', x1: m.l, x2: w - m.r, y1: height - m.b, y2: height - m.b }));
    const span = t1 - t0;
    const xt = timeTicks(t0, t1, compact ? 3 : 6).ticks;
    xt.forEach((tick, i) => {
      const label = timeLabel(tick, Math.max(span, 1));
      const half = textWidth(label) / 2;
      const cx = Math.max(half, Math.min(w - half, x(tick)));
      if (i && Math.abs(cx - x(xt[i - 1])) < half * 2 + 8) return;   // never print two labels over each other
      out.push(s('text', { class: 'tick', x: cx, y: height - m.b + 18, 'text-anchor': 'middle', text: label }));
    });
    const xs = points.map((p) => x(p.t));
    const ys = points.map((p) => y(p.v));
    if (points.length > 1) {
      const line = xs.map((px, i) => `${i ? 'L' : 'M'}${px.toFixed(1)} ${ys[i].toFixed(1)}`).join('');
      out.push(s('path', { class: 'area s-single', d: `${line}L${xs[xs.length - 1].toFixed(1)} ${height - m.b}L${xs[0].toFixed(1)} ${height - m.b}Z` }));
      out.push(s('path', { class: 'line s-single', d: line }));
    }
    // cleanup markers: a hairline, and a badge above it
    const rowsRight = [-Infinity, -Infinity];
    const eventMarks = events.map((ev) => {
      const ex = x(ev.t);
      const label = ev.badge;
      const bw = textWidth(label, 11, 600) + 14;
      const left = Math.max(2, Math.min(w - bw - 2, ex - bw / 2));
      let row = rowsRight.findIndex((edge) => left > edge + 4);
      const labelled = row !== -1;
      if (labelled) rowsRight[row] = left + bw;
      else row = 0;
      const by = 4 + row * 20;
      const body = labelled
        ? [s('rect', { class: 'badge-bg', x: left, y: by, width: bw, height: 18, rx: 9 }),
          s('text', { class: 'badge-text', x: left + bw / 2, y: by + 13, 'text-anchor': 'middle', text: label })]
        : [s('circle', { class: 'badge-dot', cx: ex, cy: 10, r: 4 })];
      const attrs = { class: 'mark event', 'data-testid': 'cleanup-marker', 'data-id': ev.id, 'aria-label': `${ev.badge}: ${ev.tip.title}` };
      const g = ev.href ? s('a', { ...attrs, href: ev.href }, body) : s('g', { ...attrs, role: 'img', tabindex: 0 }, body);
      bindTip(g, box, tip, () => ev.tip, null);
      return [s('line', { class: 'event-line', x1: ex, x2: ex, y1: by + 18, y2: height - m.b }), g];
    });
    out.push(eventMarks.map((pair) => pair[0]));
    const cross = s('line', { class: 'crosshair', x1: 0, x2: 0, y1: m.t, y2: height - m.b, hidden: true });
    out.push(cross);
    const markers = points.map((p, i) => {
      const dot = s('circle', { class: 'marker s-single', cx: xs[i], cy: ys[i], r: 5, role: 'img', 'aria-label': `${p.tip.title}: ${p.tip.rows.map((r) => `${r.label} ${r.value}`).join(', ')}`,
        'data-testid': 'used-marker', 'data-id': p.id, 'data-t': p.t, 'data-v': p.v });
      return dot;
    });
    const show = (i) => {
      cross.removeAttribute('hidden');
      cross.setAttribute('x1', xs[i]);
      cross.setAttribute('x2', xs[i]);
      markers.forEach((dot, j) => dot.setAttribute('r', j === i ? 6.5 : 5));
      tip.show(xs[i], ys[i], points[i].tip);
    };
    const hide = () => {
      cross.setAttribute('hidden', '');
      markers.forEach((dot) => dot.setAttribute('r', 5));
      tip.hide();
    };
    const plot = s('rect', { class: 'plot-hit', x: m.l, y: m.t - 8, width: w - m.l - m.r, height: height - m.t - m.b + 8, fill: 'transparent' });
    plot.addEventListener('pointermove', (ev) => show(nearest(xs, ev.clientX - box.getBoundingClientRect().left)));
    plot.addEventListener('pointerleave', hide);
    out.push(plot);
    markers.forEach((dot, i) => {
      dot.addEventListener('pointermove', () => show(i));   // the dot sits above the plot's own hit area
      dot.addEventListener('focus', () => show(i));
      dot.addEventListener('blur', hide);
    });
    roving(markers);
    out.push(markers);
    out.push(eventMarks.map((pair) => pair[1]));
    return s('svg', { class: 'chart-svg', width: w, height, viewBox: `0 0 ${w} ${height}`, role: 'group', 'aria-label': ariaLabel }, out);
  });
  box.append(host);
  box.destroy = () => host.destroy();
  return box;
}

// stackedColumns -----------------------------------------------------------------------------------------------

// columns: [{id, label, values: {key: bytes}, total, tip, href?}]; series: [{key, label}] from the bottom up. One series
// is a plain column chart. labelLast writes the series names beside the last column; capLabels the value above each.
export function stackedColumns({ columns, series, ariaLabel, labelLast = false, capLabels = false, valueText, testid = 'col' }) {
  const box = h('div', { class: 'chart-box' });
  const tip = makeTip(box);
  const host = responsive((w) => {
    const compact = w < 520;
    const height = compact ? 220 : 260;
    const lastValues = columns.length ? columns[columns.length - 1].values : {};
    const nameRoom = labelLast ? Math.max(...series.filter((x) => lastValues[x.key] > 0).map((x) => textWidth(x.label)), 0) + 20 : 0;
    const maxTotal = Math.max(...columns.map((c) => c.total), 1);
    const yAxis = byteTicks(0, maxTotal, { zero: true, target: compact ? 4 : 5 });
    const m = { l: axisRoom(yAxis), r: Math.max(16, nameRoom), t: capLabels ? 24 : 12, b: 30 };
    const y = linear(0, yAxis.max, height - m.b, m.t);
    const band = (w - m.l - m.r) / columns.length;
    const colW = Math.max(3, Math.min(BAR_MAX, band - 4));
    const cx = (i) => m.l + band * (i + 0.5);
    const out = [];
    for (const tick of yAxis.ticks) {
      const ty = y(tick.value);
      out.push(s('line', { class: 'grid', x1: m.l, x2: w - m.r, y1: ty, y2: ty }));
      out.push(s('text', { class: 'tick', x: m.l - 10, y: ty + 4, 'text-anchor': 'end', text: tick.label }));
    }
    out.push(s('line', { class: 'axis', x1: m.l, x2: w - m.r, y1: height - m.b, y2: height - m.b }));
    const widest = Math.max(...columns.map((c) => textWidth(c.label)), 1);
    const step = Math.max(1, Math.ceil(columns.length / Math.max(1, Math.floor((w - m.l - m.r) / (widest + 14)))));
    columns.forEach((c, i) => {
      if ((columns.length - 1 - i) % step === 0) out.push(s('text', { class: 'tick', x: cx(i), y: height - m.b + 18, 'text-anchor': 'middle', text: c.label }));
    });
    const marks = [];
    const labels = [];
    let lastY = Infinity;
    const maxIndex = columns.findIndex((c) => c.total === maxTotal);
    columns.forEach((c, i) => {
      const live = series.filter((x) => c.values[x.key] > 0);
      let base = height - m.b;   // pixel of the bottom of the next segment
      const parts = live.map((x, k) => {
        const hgt = (c.values[x.key] / yAxis.max) * (height - m.b - m.t);
        const y1 = base - hgt;
        const topmost = k === live.length - 1;
        const gapped = k === 0 ? base : base - GAP;
        const segH = Math.max(1, gapped - y1);
        const d = roundedRect(cx(i) - colW / 2, gapped - segH, colW, segH, topmost ? { tl: END, tr: END } : {});
        const centre = (gapped + y1) / 2;
        base = y1;
        return { x, d, centre };
      });
      const g = s('g', { class: 'mark col-g', role: 'img', tabindex: 0, 'data-testid': testid, 'data-id': c.id, 'data-total': c.total,
        'aria-label': `${c.tip.title}: ${c.tip.rows.map((r) => `${r.label} ${r.value}`).join(', ')}` },
      parts.map((p) => s('path', { class: `seg ${p.x.cls || `s-${p.x.key}`}`, d: p.d, 'data-key': p.x.key })),
      s('rect', { class: 'hit', x: cx(i) - band / 2, y: m.t - 6, width: band, height: height - m.t - m.b + 6, fill: 'transparent' }));
      const anchor = () => g.querySelector('.seg') || g;
      bindTip(g, box, tip, () => c.tip, anchor);
      marks.push(g);
      if (capLabels && c.total > 0) {
        const text = valueText(c.total);
        const room = textWidth(text, 11, 600);
        if (band >= room + 10 || i === maxIndex) {
          const lx = Math.max(room / 2, Math.min(w - room / 2, cx(i)));
          labels.push(s('text', { class: 'cap-label', x: lx, y: y(c.total) - 6, 'text-anchor': 'middle', text }));
        }
      }
      if (labelLast && i === columns.length - 1) {
        [...parts].reverse().forEach((p) => {
          if (Math.abs(p.centre - lastY) < 15) return;   // a label that would touch the one above is left to the legend
          lastY = p.centre;
          labels.push(s('text', { class: 'direct-label', x: cx(i) + colW / 2 + 8, y: p.centre + 4, text: p.x.label }));
        });
      }
    });
    roving(marks);
    out.push(marks, labels);
    return s('svg', { class: 'chart-svg', width: w, height, viewBox: `0 0 ${w} ${height}`, role: 'group', 'aria-label': ariaLabel }, out);
  });
  box.append(host);
  box.destroy = () => host.destroy();
  return box;
}

// rowBars ------------------------------------------------------------------------------------------------------

// rows: [{key, label, sub?, value, valueText, cls, href?, tip}] or {heading}. diverging: bars grow right (positive) and left
// (negative) from one neutral zero line; otherwise from the left edge. Labels and values are HTML text in text tokens, so
// they wrap instead of clipping; only the bar itself is SVG.
export function rowBars({ rows, diverging = false, ariaLabel, testid = 'row' }) {
  const box = h('div', { class: 'chart-box rowbars-box' });
  const tip = makeTip(box);
  const biggest = Math.max(...rows.filter((r) => !r.heading).map((r) => Math.abs(r.value)), 1);
  const tracks = [];
  const items = rows.map((row) => {
    if (row.heading) return h('li', { class: 'rb-head' }, row.cls ? h('span', { class: `swatch ${row.cls}`, 'aria-hidden': 'true' }) : null, row.heading);
    const track = h('span', { class: 'rb-track' });
    tracks.push({ row, track });
    const body = [h('span', { class: 'rb-label' }, h('span', { class: 'rb-name', text: row.label }), row.sub ? h('small', { class: 'rb-sub', text: row.sub }) : null),
      track, h('span', { class: 'rb-value', text: row.valueText })];
    const attrs = { class: 'rb-row', 'data-testid': testid, 'data-key': row.key, 'data-value': row.value, 'aria-label': `${row.label}: ${row.valueText}` };
    const el = row.href ? h('a', { ...attrs, href: row.href }, body) : h('div', { ...attrs, tabindex: '0', role: 'group' }, body);
    bindTip(el, box, tip, () => row.tip, null);
    return h('li', {}, el);
  });
  const list = h('ul', { class: 'rowbars', 'aria-label': ariaLabel }, items);
  box.append(list);
  let last = 0;
  const paint = (w) => {
    last = w;
    for (const { row, track } of tracks) {
      const height = 28;
      const thick = 14;
      const mid = diverging ? w / 2 : 0;
      const room = diverging ? w / 2 - 6 : w - 6;
      const len = row.value === 0 ? 0 : Math.max(2, (Math.abs(row.value) / biggest) * room);
      const up = row.value >= 0;
      const x = diverging && !up ? mid - len : mid;
      const radii = diverging ? (up ? { tr: END, br: END } : { tl: END, bl: END }) : { tr: END, br: END };
      replace(track, s('svg', { class: 'chart-svg', width: w, height, viewBox: `0 0 ${w} ${height}`, 'aria-hidden': 'true' },
        len ? s('path', { class: `seg ${row.cls}`, d: roundedRect(x, (height - thick) / 2, len, thick, radii) }) : null,
        s('line', { class: 'axis zero', x1: mid + 0.5, x2: mid + 0.5, y1: 2, y2: height - 2 })));
    }
  };
  const ro = new ResizeObserver(() => {
    const first = tracks[0];
    if (!first) return;
    const w = Math.floor(first.track.clientWidth);
    if (w > 0 && w !== last) paint(w);
  });
  paint(240);
  ro.observe(list);
  box.destroy = () => ro.disconnect();
  return box;
}

// sparkline ------------------------------------------------------------------------------------------------------

// A trend in a small box: a muted 2px line and the latest point as the accent dot. Decorative; the tile has the numbers.
export function sparkline(values, { width = 104, height = 32 } = {}) {
  if (values.length < 2) return null;
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const pad = 6;
  const x = linear(0, values.length - 1, pad, width - pad);
  const y = linear(lo, hi === lo ? lo + 1 : hi, height - pad, pad);
  const pts = values.map((v, i) => [x(i), hi === lo ? height / 2 : y(v)]);
  const d = pts.map(([px, py], i) => `${i ? 'L' : 'M'}${px.toFixed(1)} ${py.toFixed(1)}`).join('');
  const [lx, ly] = pts[pts.length - 1];
  return s('svg', { class: 'spark', width, height, viewBox: `0 0 ${width} ${height}`, 'aria-hidden': 'true' },
    s('path', { class: 'spark-line', d }), s('circle', { class: 'spark-dot', cx: lx, cy: ly, r: 4 }));
}

// diskRing -------------------------------------------------------------------------------------------------------

// The used share of a volume as an arc and the free figure in the middle. The arc is the accent ink; how full the disk
// is gets its words (and a small status icon) from the caller, never a colour on the arc.
export function diskRing({ used, total, value, caption, ariaLabel, size = 176 }) {
  const stroke = 14;
  const r = (size - stroke) / 2;
  const c = size / 2;
  const circumference = 2 * Math.PI * r;
  const share = Math.max(0, Math.min(1, used / total));
  return s('svg', { class: 'ring-svg', viewBox: `0 0 ${size} ${size}`, width: size, height: size, role: 'img', 'aria-label': ariaLabel,
    'data-level': diskLevel(used, total), 'data-testid': 'disk-ring' },
  s('circle', { class: 'ring-track', cx: c, cy: c, r }),
  s('circle', { class: 'ring-arc', cx: c, cy: c, r, 'stroke-dasharray': `${(circumference * share).toFixed(1)} ${circumference.toFixed(1)}`,
    transform: `rotate(-90 ${c} ${c})` }),
  s('text', { class: 'ring-value', x: c, y: c + 2, 'text-anchor': 'middle', text: value }),
  s('text', { class: 'ring-caption', x: c, y: c + 22, 'text-anchor': 'middle', text: caption }));
}
