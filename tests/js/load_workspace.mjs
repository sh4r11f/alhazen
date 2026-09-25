/* Load the real experiment-workspace scripts into a fake browser page and
 * hand their top-level bindings to a test.
 *
 * workspace_parameters.js and then workspace.js run unmodified — the order
 * workspace.html loads them — in one node:vm context, except that the
 * trailing `poll();` is cut so the page does not start its 1.5 s polling
 * loop on its own. A test drives refresh(), refreshRun() and the form's
 * handlers itself, and so knows exactly which requests were made and when.
 *
 *     workspace.html ──ids──▶ fake page (fake_dom.mjs)
 *     workspace_parameters.js + workspace.js ─run in─▶ vm context ─run()─▶ test
 *     fetch ──▶ fakeServer (the launcher's routes, below) or the test's stub
 *
 * The fake server answers the launcher's JSON API from plain objects the
 * test fills in (`server.state`, `server.details`, `server.configs`, …), so
 * a test reads like the scenario it checks — "a running run with one image
 * and a monitor URL" — rather than like a list of fetch replies. Anything it
 * is not told about is an error naming the URL, never an empty answer.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

import { FakeDocument } from './fake_dom.mjs';

const ASSETS = new URL('../../src/alhazen/cli/assets/', import.meta.url);
const PAGE_HTML = readFileSync(new URL('workspace.html', ASSETS), 'utf8');
const PARAMETERS_JS = fileURLToPath(new URL('workspace_parameters.js', ASSETS));
const WORKSPACE_JS = fileURLToPath(new URL('workspace.js', ASSETS));
const PARAMETERS_SOURCE = readFileSync(PARAMETERS_JS, 'utf8');
const WORKSPACE_SOURCE = readFileSync(WORKSPACE_JS, 'utf8');

/* The script must end by starting its poll loop; the tests cut exactly that
 * line and fail loudly if it has moved, rather than running a page that
 * polls behind their back. */
const POLL_CALL = /\npoll\(\);\s*$/;
if (!POLL_CALL.test(WORKSPACE_SOURCE)) {
  throw new Error('workspace.js no longer ends with `poll();`; update tests/js/load_workspace.mjs');
}
const WORKSPACE_WITHOUT_POLL = WORKSPACE_SOURCE.replace(POLL_CALL, '\n');

/** Let every promise the script has started run to completion; the fake
 *  server answers on the microtask queue, so one setImmediate suffices. */
export function settle() {
  return new Promise((resolve) => setImmediate(resolve));
}

/* The page's elements the script looks up: every element in workspace.html
 * with an id, flat under <body> — the script never walks the nesting except
 * into #gallery-empty, whose <h3> and <p> it rewrites, so those two children
 * are the one piece of structure reproduced. Each keeps the `class` and
 * `hidden` it starts with in the HTML (the selected tab, the panels that
 * begin hidden), since the script toggles those rather than setting them.
 * Read from the real file so a renamed id breaks these tests instead of
 * drifting from them. */
function buildPage(document) {
  for (const [, tag, attributes] of PAGE_HTML.matchAll(/<([a-z][\w-]*)\b([^>]*)>/g)) {
    const id = /\bid="([^"]+)"/.exec(attributes);
    if (!id) continue;
    const element = document.createElement(tag);
    element.setAttribute('id', id[1]);
    const className = /\bclass="([^"]*)"/.exec(attributes);
    if (className) element.setAttribute('class', className[1]);
    if (/\shidden(?=[\s>]|$)/.test(attributes)) element.hidden = true;
    /* Form controls read back '' when untouched, as in a browser. */
    element.value = '';
    document.body.appendChild(element);
    if (id[1] === 'gallery-empty') {
      element.appendChild(document.createElement('h3'));
      element.appendChild(document.createElement('p'));
    }
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

/* A response with only what workspace.js reads from one: `ok`, `status`
 * and the JSON body (read even on an error, for its `error` message). The
 * body is a JSON round trip of the object given, as it would be over the
 * wire: the page must never share an object with the fake server, or a test
 * that edits `server.state` would edit the page's own `state` underneath it. */
export function response(json, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status: status,
    json: async () => JSON.parse(JSON.stringify(json)),
  };
}

/**
 * The launcher's API over plain objects. A test sets the fields and the
 * routes below answer from them:
 *
 *   state     what /api/state returns: {projects, runs, active}
 *   details   run detail by id, for /api/runs/<id>
 *   configs   {text, values} by path, for /api/config (the rig and presets)
 *   schema    the task's JSON schema, for /api/schema
 *   launch    (body) => the run started by POST /api/runs (default {id})
 *   posted    every POST body, in order, for a test to read back
 *   reject    (url, init) => a response to give instead, or undefined
 */
function fakeServer() {
  const server = {
    state: { projects: [], runs: [], active: null },
    details: {},
    configs: {},
    schema: {},
    launch: () => ({ id: 'launched' }),
    posted: [],
    reject: () => undefined,
  };
  server.handle = (url, init) => {
    const refused = server.reject(url, init);
    if (refused) return refused;
    const [path, query = ''] = url.split('?');
    const params = new URLSearchParams(query);
    if (init && init.method === 'POST') {
      const body = JSON.parse(init.body);
      server.posted.push({ path: path, body: body });
      if (path === '/api/runs') return response(server.launch(body));
      if (path === '/api/stop') return response({});
      /* The real server parses YAML; JSON is valid YAML, and enough here. */
      if (path === '/api/parameters') return response({ values: JSON.parse(body.text) });
      if (path === '/api/projects') return response({ id: 'p' });
      if (path === '/api/projects/remove') return response({});
      throw new Error('the fake launcher has no POST route for ' + url);
    }
    if (path === '/api/state') return response(server.state);
    if (path.startsWith('/api/runs/')) {
      const run = server.details[path.slice('/api/runs/'.length)];
      return run ? response(run) : response({ error: 'Unknown run' }, 404);
    }
    if (path === '/api/schema') return response(server.schema);
    if (path === '/api/config') {
      const config = server.configs[params.get('path')];
      if (!config) return response({ error: 'No such config ' + params.get('path') }, 404);
      return response(config);
    }
    throw new Error('the fake launcher has no GET route for ' + url);
  };
  return server;
}

/**
 * Run the workspace scripts in a fresh fake page and return a handle on it.
 *
 * options.hash     location.hash at load, e.g. '#token=abc' (the token is
 *                  read from there; default '#token=test-token').
 * options.storage  localStorage contents before the script runs.
 *
 * The handle: `server` (the fake launcher, see fakeServer), `fetches`
 * (every request as {url, init}), `timers` (setTimeout calls, recorded and
 * never run), `run(code)` to evaluate an expression or statement in the
 * page's scope, `byId(id)`, and `document`.
 */
export function loadWorkspace(options = {}) {
  const document = new FakeDocument();
  buildPage(document);
  const server = fakeServer();
  const fetches = [];
  const timers = [];
  const sandbox = {
    document: document,
    URLSearchParams: URLSearchParams,
    location: {
      hash: options.hash === undefined ? '#token=test-token' : options.hash,
      pathname: '/',
    },
    history: { replaceState() {} },
    sessionStorage: fakeStorage({}),
    localStorage: fakeStorage(options.storage || {}),
    window: { addEventListener() {} },
    console: console,
    setTimeout: (callback, ms) => {
      timers.push({ callback: callback, ms: ms });
      return timers.length;
    },
    fetch: (url, init) => {
      fetches.push({ url: String(url), init: init });
      return Promise.resolve(server.handle(String(url), init));
    },
  };
  const context = vm.createContext(sandbox);
  /* Any error at load propagates: a script that cannot start fails every
   * test, with the asset's own line numbers in the stack. */
  vm.runInContext(PARAMETERS_SOURCE, context, { filename: PARAMETERS_JS });
  vm.runInContext(WORKSPACE_WITHOUT_POLL, context, { filename: WORKSPACE_JS });
  return {
    document: document,
    server: server,
    fetches: fetches,
    timers: timers,
    run: (code) => vm.runInContext(code, context),
    byId: (id) => document.getElementById(id),
  };
}

/* Plain copies of values made inside the script's context. node:assert's
 * deepStrictEqual compares prototypes, and an object built by workspace.js
 * has that context's Object.prototype, not this one's. */
export function plain(value) {
  return structuredClone(value);
}
