// Message catalog of the server (all namespaces merged) and {name} interpolation.
import { api } from './api.mjs';

let messages = {};
const reported = new Set();

export async function load(locale) {
  messages = await api.get(`/api/i18n/${locale}`);
  document.documentElement.lang = locale;
}

export function has(key) { return Object.prototype.hasOwnProperty.call(messages, key); }

export function t(key, args) {
  const text = messages[key];
  if (text === undefined) {
    if (!reported.has(key)) {
      reported.add(key);
      console.error(`missing i18n key: ${key}`);
    }
    return key;
  }
  if (!args) return text;
  return text.replace(/\{(\w+)\}/g, (_, name) => {
    if (!(name in args)) throw new Error(`i18n ${key}: no value for {${name}}`);
    return String(args[name]);
  });
}
