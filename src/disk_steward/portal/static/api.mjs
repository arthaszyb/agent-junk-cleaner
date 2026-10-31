// Thin fetch wrapper. Every call carries the session token in a header; every state-changing call also carries the
// X-Disk-Steward header the server insists on. There is no cookie: one for 127.0.0.1 would go to every server on that
// address, whatever its port.

export class ApiError extends Error {
  constructor(status, error) {
    super(error.message);
    this.status = status;
    this.code = error.code;
    this.messageKey = error.message_key;
    this.details = error.details || {};
  }
}

let onUnauthorized = () => {};
export function setUnauthorizedHandler(fn) { onUnauthorized = fn; }

// Storage is per origin, which includes the port; the port is in the key as well so that a leftover is recognisable.
const TOKEN_KEY = `disk-steward-token:${location.port}`;
let token = null;

export function loadToken() {
  try {
    token = localStorage.getItem(TOKEN_KEY);
  } catch (e) {
    token = null;   // storage is blocked: there is no token to find, the locked page says what to do
  }
  return token;
}

// false when the browser refuses to store it: the token still works until the page is reloaded.
export function saveToken(value) {
  token = value;
  try {
    localStorage.setItem(TOKEN_KEY, value);
    return true;
  } catch (e) {
    return false;
  }
}

export function clearToken() {
  token = null;
  try {
    localStorage.removeItem(TOKEN_KEY);
  } catch (e) {
    // nothing stored, nothing to remove
  }
}

async function request(method, path, { query, body, signal } = {}) {
  let url = path;
  if (query) {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null && v !== '') params.set(k, String(v));
    const text = params.toString();
    if (text) url += `?${text}`;
  }
  const init = { method, credentials: 'omit', headers: {}, signal };
  if (token) init.headers['X-Disk-Steward-Token'] = token;
  if (method !== 'GET') {
    init.headers['Content-Type'] = 'application/json';
    init.headers['X-Disk-Steward'] = '1';
    init.body = JSON.stringify(body === undefined ? {} : body);
  }
  const res = await fetch(url, init);
  const payload = await res.json();
  if (!res.ok) {
    if (res.status === 401) onUnauthorized();
    throw new ApiError(res.status, payload.error);
  }
  return payload;
}

export const api = {
  get: (path, query, signal) => request('GET', path, { query, signal }),
  post: (path, body) => request('POST', path, { body }),
  put: (path, body) => request('PUT', path, { body }),
};
