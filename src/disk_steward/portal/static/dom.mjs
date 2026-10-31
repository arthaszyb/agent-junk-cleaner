// DOM building with createElement and textContent only: no string from the server or the user reaches innerHTML.

const PROPS = new Set(['checked', 'disabled', 'value', 'indeterminate', 'selected', 'open', 'hidden', 'max']);

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (tag === 'button') el.type = 'button';
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'text') el.textContent = value;
    else if (key === 'data') {
      for (const [name, v] of Object.entries(value)) if (v !== undefined && v !== null) el.dataset[name] = String(v);
    } else if (PROPS.has(key)) el[key] = value;
    else el.setAttribute(key, value === true ? '' : String(value));
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

// One replaceChildren call, so the page never lays out an empty container (which would reset the scroll position).
export function replace(el, ...children) {
  const frag = document.createDocumentFragment();
  append(frag, children);
  el.replaceChildren(frag);
  return el;
}

let errorHandler = (e) => { throw e; };
export function setErrorHandler(fn) { errorHandler = fn; }

// One listener per event type on a root; elements opt in with data-<attr>="handlerName".
// A handler may be async: its failure goes to the error handler instead of vanishing.
export function delegate(root, type, attr, handlers) {
  root.addEventListener(type, (ev) => {
    const el = ev.target instanceof Element ? ev.target.closest(`[data-${attr}]`) : null;
    if (!el || !root.contains(el)) return;
    const fn = handlers[el.dataset[attr]];
    if (!fn) return;
    try {
      const out = fn(el, ev);
      if (out && typeof out.catch === 'function') out.catch(errorHandler);
    } catch (e) {
      errorHandler(e);
    }
  });
}

// Re-render while keeping the keyboard focus on "the same" control (marked with data-fid).
export function keepFocus(root, render) {
  const active = document.activeElement;
  const fid = active && root.contains(active) ? active.dataset.fid : null;
  render();
  if (fid) {
    const again = [...root.querySelectorAll('[data-fid]')].find((e) => e.dataset.fid === fid);
    if (again && !again.disabled) again.focus({ preventScroll: true });
  }
}
