// The icon set: hand-written 24px stroke icons, drawn in currentColor so they follow the text token around them.
import { s } from './svg.mjs';

const p = (d) => ['path', { d }];
const c = (cx, cy, r) => ['circle', { cx, cy, r }];
const r = (x, y, width, height, rx) => ['rect', { x, y, width, height, rx }];

const SHAPES = {
  overview: [r(3.5, 3.5, 7, 9, 2), r(13.5, 3.5, 7, 5, 2), r(3.5, 15.5, 7, 5, 2), r(13.5, 11.5, 7, 9, 2)],
  insights: [p('M3.5 20.5h17'), p('M7.5 17v-5'), p('M12 17V7.5'), p('M16.5 17V4.5')],
  browse: [p('M3.5 7.5a2 2 0 0 1 2-2h3.6l2 2.2h7.4a2 2 0 0 1 2 2v7.8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z')],
  cleanup: [p('M19.5 3.5l-6.3 6.3'), p('M9.6 9.6l4.8 4.8-1.3 1.3c-1.4 1.4-3.6 2.4-6.6 2.8-.4-3 .6-5.3 2-6.7z'), p('M8.2 15.4l-1.1 2.2')],
  archive: [r(3.5, 4.5, 17, 4.5, 1.5), p('M5 9v9a1.5 1.5 0 0 0 1.5 1.5h11A1.5 1.5 0 0 0 19 18V9'), p('M10 13h4')],
  advisor: [p('M10.5 3.5l1.7 4.8 4.8 1.7-4.8 1.7-1.7 4.8-1.7-4.8L4 10l4.8-1.7z'), p('M18.5 14.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z')],
  settings: [p('M4 7h9'), p('M17 7h3'), c(15, 7, 2), p('M4 17h3'), p('M11 17h9'), c(9, 17, 2)],
  scan: [p('M20 11a8 8 0 0 0-14.4-4.2L4 8.5'), p('M4 4v4.5h4.5'), p('M4 13a8 8 0 0 0 14.4 4.2L20 15.5'), p('M20 20v-4.5h-4.5')],
  check: [p('M5 12.5l4.5 4.5L19 7.5')],
  alert: [p('M10.3 4.9L2.9 18a2 2 0 0 0 1.7 3h14.8a2 2 0 0 0 1.7-3L13.7 4.9a2 2 0 0 0-3.4 0z'), p('M12 9.5v4'), p('M12 17h.01')],
  info: [c(12, 12, 9), p('M12 11v5'), p('M12 7.8h.01')],
  lock: [r(5, 10.5, 14, 10, 2.5), p('M8 10.5V8a4 4 0 0 1 8 0v2.5')],
  external: [p('M14 4h6v6'), p('M20 4l-9 9'), p('M18 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10')],
  copy: [r(8.5, 8.5, 11, 11, 2.5), p('M15.5 8.5v-2a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2')],
  chevron: [p('M6 9.5l6 6 6-6')],
  sun: [c(12, 12, 4), p('M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6L7 7M17 17l1.4 1.4M5.6 18.4L7 17M17 7l1.4-1.4')],
  moon: [p('M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z')],
  monitor: [r(3.5, 4.5, 17, 12, 2.5), p('M9 20h6'), p('M12 16.5V20')],
  menu: [p('M4 7h16'), p('M4 12h16'), p('M4 17h16')],
  close: [p('M6 6l12 12'), p('M18 6L6 18')],
  disk: [r(3.5, 5, 17, 14, 3), c(12, 12, 3.2), p('M7 8.5h.01')],
  arrow: [p('M5 12h14'), p('M13 6l6 6-6 6')],
  'arrow-up': [p('M12 19V6'), p('M6 11l6-6 6 6')],
  'arrow-down': [p('M12 5v13'), p('M6 13l6 6 6-6')],
  sort: [p('M8 9l4-4 4 4'), p('M8 15l4 4 4-4')],
  play: [p('M8 5.5v13l11-6.5z')],
  stop: [r(6.5, 6.5, 11, 11, 2)],
  trash: [p('M4 7h16'), p('M9.5 7V4.5h5V7'), p('M6.5 7l.8 12a1.5 1.5 0 0 0 1.5 1.4h6.4a1.5 1.5 0 0 0 1.5-1.4l.8-12'), p('M10 11v5'), p('M14 11v5')],
  terminal: [r(3.5, 4.5, 17, 15, 3), p('M7.5 10l3 2.5-3 2.5'), p('M13 15.5h3.5')],
};

export function icon(name, size = 20) {
  const shapes = SHAPES[name];
  if (!shapes) throw new Error(`no icon named ${name}`);
  return s('svg', { class: `icon icon-${name}`, viewBox: '0 0 24 24', width: size, height: size, fill: 'none', stroke: 'currentColor',
    'stroke-width': 1.75, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true', focusable: 'false' },
  shapes.map(([tag, attrs]) => s(tag, attrs)));
}

export const ICON_NAMES = Object.keys(SHAPES);
