/* Shared mount for hub page tests: the page on a fake document with a fake
 * hub behind fetch (same harness as hub_page.test.mjs). */
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

import { FakeDocument } from './fake_dom.mjs';

const ASSETS = new URL('../../src/alhazen/hub/assets/', import.meta.url);
export const HTML = readFileSync(new URL('index.html', ASSETS), 'utf8');
export const CORE = readFileSync(new URL('hub_core.js', ASSETS), 'utf8');
export const APP = readFileSync(new URL('hub.js', ASSETS), 'utf8');

function buildPage(document) {
  for (const match of HTML.matchAll(/<([a-z][\w-]*)\b([^>]*)>/g)) {
    const id = /\bid="([^"]+)"/.exec(match[2]);
    if (!id) continue;
    const el = document.createElement(match[1]);
    el.setAttribute('id', id[1]);
    if (/\shidden(?=[\s>]|$)/.test(match[2])) el.hidden = true;
    document.body.appendChild(el);
  }
}

const settle = () => new Promise((resolve) => setImmediate(resolve));
export async function settleAll(n = 10) {
  for (let i = 0; i < n; i += 1) await settle();
}

/* routes: an object (or Proxy) of 'METHOD /path' -> handler(request). */
export function fakeHub(routes) {
  const calls = [];
  const fetch = async (url, init) => {
    const [path, query] = url.split('?');
    const key = init.method + ' ' + path.replace('/api/hub/v1', '');
    const request = {url, path, key, query: new URLSearchParams(query || ''), init,
      json: init.body && typeof init.body === 'string' ? JSON.parse(init.body) : null};
    calls.push(request);
    const handler = routes[key];
    const reply = handler ? await handler(request) : {status: 404, body: {error: {code: 'not_found', message: 'no route ' + key}}};
    if (reply instanceof Error) throw reply;
    return {
      ok: reply.status >= 200 && reply.status < 300, status: reply.status,
      headers: {get: (name) => (reply.headers && reply.headers[name.toLowerCase()]) || null},
      text: async () => (reply.body === undefined ? '' : JSON.stringify(reply.body)),
    };
  };
  return {fetch, calls};
}

export function storage(initial) {
  const map = new Map(Object.entries(initial || {}));
  return {getItem: (k) => (map.has(k) ? map.get(k) : null), setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k), map};
}

export async function mount({routes, path = '/', search = ''}) {
  const document = new FakeDocument();
  buildPage(document);
  const loc = {pathname: path, search, hash: ''};
  const history = {
    entries: [],
    pushState(s, t, url) { this.entries.push(['push', url]); setUrl(url); },
    replaceState(s, t, url) { this.entries.push(['replace', url]); setUrl(url); },
  };
  function setUrl(url) {
    const q = url.indexOf('?');
    loc.pathname = q < 0 ? url : url.slice(0, q);
    loc.search = q < 0 ? '' : url.slice(q);
  }
  const listeners = {};
  const win = {addEventListener: (type, fn) => { listeners[type] = fn; }, HubDocs: null};
  const hub = fakeHub(routes);
  /* Timers: [fn, ms]; tick() runs the ones due (all, or only those of `ms`). */
  const pending = [];
  const timers = {setTimeout: (fn, ms) => { pending.push([fn, ms]); return pending.length; }, clearTimeout: (id) => { pending[id - 1] = null; }};
  const session = storage();
  const local = storage();
  const context = vm.createContext({URL, URLSearchParams, AbortController, Promise, JSON, Math, Date, Error, TypeError});
  vm.runInContext(CORE + '\n' + APP + '\nthis.HubApp = HubApp; this.HubCore = HubCore;', context);
  const page = context.HubApp.mount({
    document, location: loc, history, window: win, fetch: hub.fetch, sessionStorage: session,
    localStorage: local, setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout,
    clipboard: null, scrollTo: () => {},
  });
  await page.ready;
  await settleAll();
  const main = document.getElementById('main');
  return {
    document, loc, history, hub, page, main, session, local, pending,
    text: () => main.textContent,
    find: (selector, text) => main.querySelectorAll(selector).find((el) => text === undefined || el.textContent.includes(text)),
    async click(el) { el.fire('click', {preventDefault() {}, button: 0, target: el}); await settleAll(); },
    async submit(form) { form.fire('submit', {preventDefault() {}}); await settleAll(); },
    /* Fire the timers of `ms` (the 2 s AI poll), leaving request timeouts alone. */
    async tick(ms) {
      const due = [];
      pending.forEach((t, i) => { if (t && (ms === undefined || t[1] === ms)) { due.push(t[0]); pending[i] = null; } });
      due.forEach((f) => f());
      await settleAll();
    },
    polls: (ms) => pending.filter((t) => t && t[1] === ms).length,
  };
}
