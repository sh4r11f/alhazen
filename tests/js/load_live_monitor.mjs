/* Load the real live_monitor.js into a fake browser page and hand its top-level
 * functions and constants to a test.
 *
 * The script is run unmodified, the way index.html runs it: first a script
 * that defines STATIC_STATE, then live_monitor.js itself, in one shared global
 * scope (a node:vm context). Everything it declares at top level — functions,
 * `const`s, `let`s — is then reachable by name through `get()`.
 *
 *     index.html ──ids, data-command buttons──▶ fake page (fake_dom.mjs)
 *     live_monitor.css ──:root colours──────────▶ getComputedStyle
 *     STATIC_STATE + live_monitor.js ─runs in──▶ vm context ─get(name)─▶ test
 *
 * Nothing asynchronous happens behind a test's back: timers and animation
 * frames are recorded, not run, until the test calls runTimers()/runFrames();
 * fetch answers only through a stub the test passes in. A saved page (a state
 * given) renders once at load and polls nothing, exactly as the copy in
 * figures/ does.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

import { FakeDocument, serialize } from './fake_dom.mjs';

const ASSETS = new URL('../../src/alhazen/live_monitor/assets/', import.meta.url);
export const LIVE_MONITOR_JS = fileURLToPath(new URL('live_monitor.js', ASSETS));
const SOURCE = readFileSync(LIVE_MONITOR_JS, 'utf8');
const PAGE_HTML = readFileSync(new URL('index.html', ASSETS), 'utf8');
const STYLESHEET = readFileSync(new URL('live_monitor.css', ASSETS), 'utf8');

/* The light theme's custom properties, read from the stylesheet's first
 * :root block, so a test that resolves a theme colour (the heatmap's ramp, the
 * figure export) gets the colour the page really has. */
const ROOT_BLOCK = /:root\s*\{([^}]*)\}/.exec(STYLESHEET);
if (!ROOT_BLOCK) throw new Error('live_monitor.css has no :root block to read the light theme from');
export const LIGHT_THEME = Object.fromEntries(
  [...ROOT_BLOCK[1].matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]),
);

/** The smallest state render() accepts: a finished session with no panels. */
export const SAVED_STATE = Object.freeze({
  revision: 1, status: 'complete', identity: {}, n_trials: 0, n_events: 0, panels: [],
});

/** Let every promise the script has started run to completion. The stubs
 *  below resolve on the microtask queue, which empties before the next
 *  macrotask, so one setImmediate is enough. */
export function settle() {
  return new Promise((resolve) => setImmediate(resolve));
}

/* The page's elements the script looks up: everything in index.html with an
 * id, and the command buttons. Created flat under <body> — the script never
 * walks the page's nesting — and read from the real file so a renamed id
 * breaks the tests instead of drifting from them. */
function buildPage(document) {
  for (const [, tag, attributes] of PAGE_HTML.matchAll(/<([a-z][\w-]*)\b([^>]*)>/g)) {
    const id = /\bid="([^"]+)"/.exec(attributes);
    const command = /\bdata-command="([^"]+)"/.exec(attributes);
    if (!id && !command) continue;
    const element = document.createElement(tag);
    if (id) element.setAttribute('id', id[1]);
    if (command) element.setAttribute('data-command', command[1]);
    document.body.appendChild(element);
  }
}

/* localStorage/sessionStorage over a Map, with the browser's null for a
 * missing key. */
function fakeStorage(initial) {
  const items = new Map(Object.entries(initial));
  return {
    getItem: (key) => (items.has(key) ? items.get(key) : null),
    setItem: (key, value) => { items.set(key, String(value)); },
    removeItem: (key) => { items.delete(key); },
  };
}

/**
 * Run live_monitor.js in a fresh fake page and return a handle on it.
 *
 * options.staticState  the state a saved page carries (default SAVED_STATE);
 *                      `null` loads the live page, which polls at once and
 *                      so needs options.fetch.
 * options.fetch        (url, init) => response or promise of one; every call
 *                      is also recorded in `fetches`. Without it any fetch
 *                      throws, naming the URL.
 * options.search       location.search, e.g. '?token=abc'; the page may
 *                      rewrite it (window.location, window.history.replaced).
 * options.storage      localStorage contents before the script runs.
 */
export function loadLiveMonitor(options = {}) {
  const staticState = options.staticState === undefined ? SAVED_STATE : options.staticState;
  if (staticState === null && !options.fetch) {
    throw new Error('the live page (staticState: null) polls the server as it loads; ' +
      'pass options.fetch to answer it');
  }

  const document = new FakeDocument();
  buildPage(document);

  const clock = { now: 0 };
  const timers = new Map();
  const frames = new Map();
  let handles = 0;
  let requests = 0;
  const fetches = [];
  const alerts = [];
  const consoleErrors = [];
  const downloads = [];
  const objectUrls = new Map();

  /* A download is an <a download> clicked with an object URL (saveBlob),
   * recorded as it happens: the script revokes the URL afterwards. */
  document.onElementClick = (element) => {
    if (element.localName === 'a' && element.download) {
      downloads.push({ filename: element.download, blob: objectUrls.get(element.href) });
    }
  };

  /* An image "loads" on the next microtask, whatever its URL: the figure
   * export draws a data: URL onto a canvas, and the canvas is faked too. */
  class FakeImage {
    set src(url) {
      this.url = url;
      Promise.resolve().then(() => { if (this.onload) this.onload(); });
    }

    get src() {
      return this.url;
    }
  }

  /* The address bar: where the page was loaded from, and a history whose
   * replaceState rewrites it the way a browser does (same page, new URL, no
   * reload), keeping each call for a test to look at. */
  const location = { pathname: '/', search: options.search || '', hash: '' };
  const history = {
    state: null,
    replaced: [],
    replaceState(state, unused, url) {
      history.replaced.push(String(url));
      const parsed = new URL(String(url), 'http://127.0.0.1' + location.pathname);
      history.state = state;
      location.pathname = parsed.pathname;
      location.search = parsed.search;
      location.hash = parsed.hash;
    },
  };

  const sandbox = {
    document: document,
    location: location,
    history: history,
    URLSearchParams: URLSearchParams,
    localStorage: fakeStorage(options.storage || {}),
    sessionStorage: fakeStorage({}),
    /* The page root answers with the light theme's custom properties; any
     * other element has no computed style worth reporting. */
    getComputedStyle: (element) => ({
      getPropertyValue: (name) =>
        (element === document.documentElement && name in LIGHT_THEME ? LIGHT_THEME[name] : ''),
    }),
    matchMedia: (query) => ({ matches: false, media: query, addEventListener() {} }),
    ResizeObserver: class {
      observe() {}
      disconnect() {}
    },
    requestAnimationFrame: (callback) => {
      handles += 1;
      frames.set(handles, callback);
      return handles;
    },
    cancelAnimationFrame: (handle) => { frames.delete(handle); },
    setTimeout: (callback, ms) => {
      handles += 1;
      timers.set(handles, { callback: callback, ms: ms });
      return handles;
    },
    clearTimeout: (handle) => { timers.delete(handle); },
    performance: { now: () => clock.now },
    fetch: (url, init) => {
      fetches.push({ url: String(url), init: init });
      if (!options.fetch) {
        throw new Error('live_monitor.js fetched ' + url + ' but this test passed no fetch stub');
      }
      return Promise.resolve(options.fetch(String(url), init));
    },
    /* Errors are the script's way of saying something failed; they are kept
     * for the test to assert on rather than printed as noise. */
    console: {
      log: (...args) => console.log(...args),
      warn: (...args) => consoleErrors.push(args),
      error: (...args) => consoleErrors.push(args),
    },
    atob: atob,
    Blob: Blob,
    crypto: { randomUUID: () => 'request-' + (requests += 1) },
    XMLSerializer: class {
      serializeToString(element) {
        return serialize(element);
      }
    },
    URL: {
      createObjectURL: (blob) => {
        const url = 'blob:fake/' + (objectUrls.size + 1);
        objectUrls.set(url, blob);
        return url;
      },
      revokeObjectURL: (url) => { objectUrls.delete(url); },
    },
    Image: FakeImage,
    alert: (message) => { alerts.push(String(message)); },
    confirm: () => false,
  };
  sandbox.window = sandbox;

  const context = vm.createContext(sandbox);
  /* index.html's own first script, which the saved copy fills in. */
  vm.runInContext('const STATIC_STATE = ' + JSON.stringify(staticState) + ';', context, {
    filename: 'index.html',
  });
  /* Any error at load propagates: a script that cannot start fails every
   * test, with live_monitor.js's own line numbers in the stack. */
  vm.runInContext(SOURCE, context, { filename: LIVE_MONITOR_JS });

  return {
    document: document,
    window: sandbox,
    clock: clock,
    fetches: fetches,
    alerts: alerts,
    consoleErrors: consoleErrors,
    downloads: downloads,
    /** A top-level binding of live_monitor.js (or any expression) by name. */
    get: (name) => vm.runInContext(name, context),
    byId: (id) => document.getElementById(id),
    /** Pending timers as [{ms}], for asserting what was scheduled. */
    pendingTimers: () => [...timers.values()].map((timer) => ({ ms: timer.ms })),
    pendingFrames: () => frames.size,
    /** Run every timer scheduled so far (not ones they schedule in turn). */
    runTimers() {
      const due = [...timers.values()];
      timers.clear();
      due.forEach((timer) => timer.callback());
      return due.length;
    },
    /** Run every animation frame requested so far. */
    runFrames() {
      const due = [...frames.values()];
      frames.clear();
      due.forEach((callback) => callback(clock.now));
      return due.length;
    },
  };
}

/** A fresh element to draw into, `width` CSS pixels wide as if on the page. */
export function hostFor(live_monitor, width) {
  const host = live_monitor.document.createElement('div');
  host.clientWidth = width || 400;
  live_monitor.document.body.appendChild(host);
  return host;
}

/* Plain copies of values made inside the script's context. node:assert's
 * deepStrictEqual compares prototypes, and an array built by live_monitor.js has
 * that context's Array.prototype, not this one's. */
export function plain(value) {
  return structuredClone(value);
}
