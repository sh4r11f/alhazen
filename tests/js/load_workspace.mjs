/* Load the real experiment-workspace scripts into a fake browser page and
 * hand their top-level bindings to a test.
 *
 * workspace_parameters.js, workspace_calibration.js and then workspace.js
 * run unmodified — the order
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
const CALIBRATION_JS = fileURLToPath(new URL('workspace_calibration.js', ASSETS));
const WORKSPACE_JS = fileURLToPath(new URL('workspace.js', ASSETS));
const TRAINING_JS = fileURLToPath(new URL('workspace_training.js', ASSETS));
const TRAINING_SOURCE = readFileSync(TRAINING_JS, 'utf8');
const MANAGE_JS = fileURLToPath(new URL('workspace_manage.js', ASSETS));
const RIG_JS = fileURLToPath(new URL('workspace_rig.js', ASSETS));
const RIG_SOURCE = readFileSync(RIG_JS, 'utf8');
const MANAGE_SOURCE = readFileSync(MANAGE_JS, 'utf8');
const PARAMETERS_SOURCE = readFileSync(PARAMETERS_JS, 'utf8');
const CALIBRATION_SOURCE = readFileSync(CALIBRATION_JS, 'utf8');
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
  for (const match of PAGE_HTML.matchAll(/<([a-z][\w-]*)\b([^>]*)>/g)) {
    const [opening, tag, attributes] = match;
    const id = /\bid="([^"]+)"/.exec(attributes);
    if (!id) continue;
    const element = document.createElement(tag);
    element.setAttribute('id', id[1]);
    /* An element whose content is plain text up to its own closing tag (a
     * button's label, the console's first line) starts with that text,
     * whitespace collapsed as a browser renders it. Content holding child
     * elements is not reproduced: the script rewrites those parts itself. */
    const after = PAGE_HTML.slice(match.index + opening.length);
    const plainText = new RegExp('^([^<]*)</' + tag + '>').exec(after);
    if (plainText) element.textContent = plainText[1].replace(/\s+/g, ' ').trim();
    const className = /\bclass="([^"]*)"/.exec(attributes);
    if (className) element.setAttribute('class', className[1]);
    if (/\shidden(?=[\s>]|$)/.test(attributes)) element.hidden = true;
    /* A control starts at the value and checked state its markup gives it
     * (`value="1"`, `checked`), else '' and false, as in a browser: the
     * launch request is built from these, and an `undefined` would simply
     * vanish from its JSON instead of being the `false` the server gets. */
    const value = /\bvalue="([^"]*)"/.exec(attributes);
    element.value = value ? value[1] : '';
    if (/\btype="checkbox"/.test(attributes)) {
      element.checked = /\schecked(?=[\s>]|$)/.test(attributes);
    }
    /* A link starts at the address its markup gives it (the sidebar's
     * Experiments and Rigs), which its click handler reads. */
    const href = /\bhref="([^"]*)"/.exec(attributes);
    if (href) element.href = href[1];
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
 *   configs   {text, values} by path, for /api/config (the parameter presets)
 *   rigs      a rig as it would run — {name, source, extends, values} — by a
 *             project-relative path or alhazen/<name>: /api/rig's answer
 *             (runs launched before the machine rig name them); the first
 *             one listed is also this machine's rig (sessionRig) unless
 *             `sessionRig` is set
 *   sessionRig  /api/session-rig's answer (the chosen config and what a
 *             session starts from), or null to derive it from `rigs`
 *   machineRigs /api/machine-rigs's answer (Rigs: this machine's configs)
 *   templates /api/machine-rigs/templates's answer ({templates, suggested})
 *   rigWrites (path, body) => the answer to a POST /api/machine-rigs/<action>
 *             (create, save, rename, default, delete, resolve, sync, check),
 *             or undefined to refuse it; a `{status, json}` object answers
 *             with that status
 *   rigHistory / rigVersions  /api/machine-rigs/history's answer and the
 *             texts by file for /api/machine-rigs/version
 *   schema    the task's JSON schema, for /api/schema without a task (a
 *             project whose run.py declares one task)
 *   schemas   JSON schema by task name, for /api/schema?…&task=<name> (a
 *             project with a task table); a name not listed is an error
 *   launch    (body) => the run started by POST /api/runs (default {id})
 *   posted    every POST body, in order, for a test to read back
 *   reject    (url, init) => a response to give instead, or undefined
 *
 * Every request's URL is in the handle's `fetches` (loadWorkspace), so a
 * test can check that `&task=` was, or was not, sent.
 */
/** /api/session-rig's answer for a rig as /api/rig gives it: that rig as
 *  this machine's, nothing carried, the task's events unknown. */
export function sessionRigFrom(rig) {
  return {
    machine: {
      name: rig.name, file: `C:/state/machine-rig/rig-${rig.name}.yaml`, sha256: 'sha-' + rig.name,
      values: rig.values, problem: null,
    },
    config: { id: 'c-' + rig.name, name: rig.name },
    configs: [{ id: 'c-' + rig.name, name: rig.name, default: true, problem: null }],
    values: rig.values,
    events: null,
    task_events: null,
    events_source: { kind: 'none', label: 'no earlier session of this task with this rig config' },
    dropped: [],
    reward_calibration: rig.reward_calibration ?? null,
    live_streams: rig.live_streams,
  };
}

function fakeServer() {
  const server = {
    state: { projects: [], runs: [], active: null },
    details: {},
    configs: {},
    rigs: {},
    sessionRig: null,
    machineRigs: { machine: { id: 'mc_0123456789abcdef', label: 'bench' }, default: null, configs: [],
      sync: { connected: false, state: 'not-connected', at: null, error: null, platform: null },
      migrated: null, error: null },
    templates: { templates: [], suggested: null },
    rigWrites: () => undefined,
    rigHistory: { saves: [], versions: [] },
    rigVersions: {},
    schema: {},
    schemas: {},
    launch: () => ({ id: 'launched' }),
    /* /api/manage/people's answer for every project: none registered, as
     * for an experiment whose General page was never used. */
    people: { error: null, subjects: [], experimenters: [], assigned: [], export: null,
      files: null },
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
      if (path.startsWith('/api/machine-rigs/')) {
        const answer = server.rigWrites(path, body);
        if (answer === undefined) return response({ error: 'No ' + path + ' expected' }, 400);
        if (answer && answer.status && answer.json) return response(answer.json, answer.status);
        return response(answer, path.endsWith('/create') ? 201 : 200);
      }
      throw new Error('the fake launcher has no POST route for ' + url);
    }
    if (path === '/api/state') {
      /* A project fixture that says nothing of this machine's rig configs
       * has one, the default, named after the first rig the test listed
       * (sessionRigFrom): every test written before the machine rig runs its
       * sessions on it. A test of the add-a-config card says
       * `machine_rig: null` (and so no configs); one that gives `machine_rig`
       * alone gets that rig as the one config. */
      const first = Object.values(server.rigs)[0];
      const projects = server.state.projects.map((p) => {
        let out = p;
        if (out.machine_rig === undefined && first) {
          out = { ...out, machine_rig: { id: 'c-' + first.name, name: first.name,
            file: `C:/state/machine-rig/rig-${first.name}.yaml`, sha256: 'sha-' + first.name, data_root: 'data' } };
        }
        if (out.machine_rigs === undefined) {
          const m = out.machine_rig;
          const id = m ? (m.id || 'c-' + m.name) : null;
          out = { ...out, machine_rigs: { default: id,
            configs: m ? [{ id, name: m.name, default: true, problem: null }] : [] } };
        }
        return out;
      });
      return response({ ...server.state, projects });
    }
    if (path === '/api/manage/people') return response(server.people);
    /* The training ladders' history (GET /api/training): none run yet,
     * unless the test says otherwise. */
    if (path === '/api/training') return response(server.training || { ladders: [] });
    if (path.startsWith('/api/runs/')) {
      const run = server.details[path.slice('/api/runs/'.length)];
      return run ? response(run) : response({ error: 'Unknown run' }, 404);
    }
    if (path === '/api/schema') {
      /* Like the real server: a task name picks that task's model; without
       * one the project's single task answers. A task the test did not set
       * up is refused by name rather than answered with an empty schema. */
      if (!params.has('task')) return response(server.schema);
      const schema = server.schemas[params.get('task')];
      if (!schema) return response({ error: 'Unknown task ' + params.get('task') }, 404);
      return response(schema);
    }
    if (path === '/api/session-rig') {
      server.sessionRigQueries = [...(server.sessionRigQueries || []), Object.fromEntries(params)];
      if (server.sessionRig) return response(server.sessionRig);
      const first = Object.values(server.rigs)[0];
      if (!first) return response({ error: 'This machine has no rig yet' }, 400);
      return response(sessionRigFrom(first));
    }
    if (path === '/api/machine-rigs') return response(server.machineRigs);
    if (path === '/api/machine-rigs/templates') return response(server.templates);
    if (path === '/api/machine-rigs/history') return response(server.rigHistory);
    if (path === '/api/machine-rigs/version') {
      const text = server.rigVersions[params.get('file')];
      return text === undefined ? response({ error: 'No such version' }, 404) : response({ text });
    }
    if (path === '/api/machine-rig/schema') return response(server.rigSchema || {});
    if (path === '/api/rig') {
      const rig = server.rigs[params.get('rig')];
      if (!rig) return response({ error: 'No such rig ' + params.get('rig') }, 404);
      return response(rig);
    }
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
 * options.search   location.search at load: the screen the address names
 *                  (workspace.js route()). Default '?view=run', the Run page
 *                  of the experiment used last (or the first), which is
 *                  where the tests written before the Experiments page
 *                  start; '' is the Experiments page.
 * options.storage  localStorage contents before the script runs.
 *
 * The handle: `server` (the fake launcher, see fakeServer), `fetches`
 * (every request as {url, init}), `timers` (setTimeout calls, recorded and
 * never run), `run(code)` to evaluate an expression or statement in the
 * page's scope, `byId(id)`, and `document`.
 */
export function loadWorkspace(options = {}) {
  /* The address, which pushState and replaceState change as a browser's
   * do; `entries` is the session history, for a test of Back and Forward. */
  const location = {
    hash: options.hash === undefined ? '#token=test-token' : options.hash,
    pathname: '/',
    search: options.search === undefined ? '?view=run' : options.search,
  };
  const entries = [location.pathname + location.search];
  /* Addresses left by Back, for Forward; a new address clears them. */
  const forward = [];
  const setAddress = (url) => {
    const [path, query = ''] = String(url).split('?');
    location.pathname = path || '/';
    location.search = query ? '?' + query : '';
  };
  const history = {
    entries: entries,
    replaceState(state, title, url) {
      if (url === undefined) return;
      setAddress(url);
      entries[entries.length - 1] = url;
    },
    pushState(state, title, url) {
      setAddress(url);
      entries.push(url);
      forward.length = 0;
    },
  };
  const listeners = {};
  const document = new FakeDocument();
  buildPage(document);
  const server = fakeServer();
  const fetches = [];
  const timers = [];
  const sandbox = {
    document: document,
    URLSearchParams: URLSearchParams,
    location: location,
    history: history,
    sessionStorage: fakeStorage({}),
    localStorage: fakeStorage(options.storage || {}),
    window: {
      addEventListener(type, listener) {
        (listeners[type] = listeners[type] || []).push(listener);
      },
      open: () => null,
    },
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
  vm.runInContext(CALIBRATION_SOURCE, context, { filename: CALIBRATION_JS });
  vm.runInContext(TRAINING_SOURCE, context, { filename: TRAINING_JS });
  vm.runInContext(RIG_SOURCE, context, { filename: RIG_JS });
  vm.runInContext(WORKSPACE_WITHOUT_POLL, context, { filename: WORKSPACE_JS });
  /* workspace.html loads the management script after workspace.js; it only
   * defines window.WorkspaceManage, which workspace.js looks up when used. */
  vm.runInContext(MANAGE_SOURCE, context, { filename: MANAGE_JS });
  return {
    document: document,
    server: server,
    fetches: fetches,
    timers: timers,
    run: (code) => vm.runInContext(code, context),
    location: location,
    history: history,
    /* Back to an earlier address, as the browser's Back button: the
     * address changes, then the page hears popstate. */
    back: () => {
      forward.push(entries.pop());
      setAddress(entries[entries.length - 1]);
      for (const listener of listeners.popstate || []) listener({});
    },
    /* Forward again after back(), as the browser's Forward button. */
    forward: () => {
      entries.push(forward.pop());
      setAddress(entries[entries.length - 1]);
      for (const listener of listeners.popstate || []) listener({});
    },
    byId: (id) => document.getElementById(id),
  };
}

/* Plain copies of values made inside the script's context. node:assert's
 * deepStrictEqual compares prototypes, and an object built by workspace.js
 * has that context's Object.prototype, not this one's. */
export function plain(value) {
  return structuredClone(value);
}
