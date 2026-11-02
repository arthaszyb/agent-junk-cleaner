// Pieces shared by every table of the portal: sortable headers, sorting, a path that shortens in the middle and the
// dot that gives a status its colour (the words are always next to it).
import { h } from './dom.mjs';
import { icon } from './icons.mjs';

export const nextSort = (state, col, firstDir = 'asc') => (state.col === col
  ? { col, dir: state.dir === 'asc' ? 'desc' : 'asc' } : { col, dir: firstDir });

// A column header that sorts: a real button inside the th, and aria-sort on the th for assistive technology.
export function sortTh(label, col, state, table, className = '') {
  const active = state.col === col;
  const arrow = active ? (state.dir === 'asc' ? 'arrow-up' : 'arrow-down') : 'sort';
  return h('th', { scope: 'col', class: className || null, 'aria-sort': active ? (state.dir === 'asc' ? 'ascending' : 'descending') : 'none' },
    h('button', { class: `th-sort${active ? ' active' : ''}`, data: { action: 'sort', table, col }, 'data-fid': `sort:${table}:${col}`, 'data-testid': `sort-${table}-${col}` },
      label, h('span', { class: 'sort-arrow', 'aria-hidden': 'true' }, icon(arrow, 12))));
}

export function plainTh(label, className = '') {
  return h('th', { scope: 'col', class: className || null }, label);
}

// getters: {column: row -> string | number}. No column (or none known): the order the server gave.
export function sortRows(rows, state, getters) {
  const get = getters[state.col];
  if (!get) return rows;
  const dir = state.dir === 'asc' ? 1 : -1;
  return rows.slice().sort((a, b) => {
    const x = get(a);
    const y = get(b);
    const order = typeof x === 'string' ? x.localeCompare(y) : (x < y ? -1 : x > y ? 1 : 0);
    return order * dir;
  });
}

// "…Support/Claude/Cache/blob1": the start gives way to an ellipsis, the end (the part that tells
// paths apart) never does. The whole path is the tooltip.
export function midPath(path) {
  // One text node, cut at its start by CSS so the end stays in view. Two boxes side by side were copied with a line
  // break between them, and a pasted "~/Library⏎/Caches/x" runs as `rm -rf ~/Library` first.
  return h('span', { class: 'path path-mid', title: path }, h('span', { dir: 'ltr', text: path }));
}

// kind: an entity (junk, review, report_only, protected, outside, free) or neutral.
export const dotLabel = (kind, text) => h('span', { class: 'dotted' }, h('span', { class: `dot dot-${kind}`, 'aria-hidden': 'true' }), h('span', { text }));

// The columns of a table, named once, so a narrow container can collapse them (col.hide-md, col.hide-sm) while a row that
// spans the table keeps a colspan that is still true.
export const colgroup = (...classes) => h('colgroup', {}, classes.map((c) => h('col', { class: c })));
