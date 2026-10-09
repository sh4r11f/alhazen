/* The hub page (src/alhazen/hub/assets/index.html, hub.js) on a fake
 * document with a fake hub behind fetch: what each role sends, what each
 * screen shows, and the rules that keep it honest (no markup from the server,
 * stale answers dropped, consent bound to a preview, nothing reported done
 * that the server did not confirm). Real browsers and the real server are
 * the coordinator's integration checks, not these. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

import { FakeDocument } from './fake_dom.mjs';

const ASSETS = new URL('../../src/alhazen/hub/assets/', import.meta.url);
const HTML = readFileSync(new URL('index.html', ASSETS), 'utf8');
const CSS = readFileSync(new URL('hub.css', ASSETS), 'utf8');
const CORE = readFileSync(new URL('hub_core.js', ASSETS), 'utf8');
const APP = readFileSync(new URL('hub.js', ASSETS), 'utf8');
const TOKEN = 'tok_' + 'x'.repeat(40);

/* Every element of index.html that has an id, flat under <body>. */
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

function settle() {
  return new Promise((resolve) => setImmediate(resolve));
}
async function settleAll(n = 8) {
  for (let i = 0; i < n; i += 1) await settle();
}

/* A fake hub: routes are 'METHOD /path' (no query) -> handler(request) that
 * returns {status, body} or a Promise of one (or an Error for offline). */
function fakeHub(routes) {
  const calls = [];
  const fetch = async (url, init) => {
    const [path, query] = url.split('?');
    const key = init.method + ' ' + path.replace('/api/hub/v1', '');
    const request = {url, path, query: new URLSearchParams(query || ''), init,
      json: init.body && typeof init.body === 'string' ? JSON.parse(init.body) : null};
    calls.push(request);
    const handler = routes[key];
    const reply = handler ? await handler(request) : {status: 404, body: {error: {code: 'not_found', message: 'no route ' + key}}};
    if (reply instanceof Error) throw reply;
    return {
      ok: reply.status >= 200 && reply.status < 300, status: reply.status,
      headers: {get: () => null},
      text: async () => (reply.body === undefined ? '' : JSON.stringify(reply.body)),
    };
  };
  return {fetch, calls};
}

function storage(initial) {
  const map = new Map(Object.entries(initial || {}));
  return {getItem: (k) => (map.has(k) ? map.get(k) : null), setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k), map};
}

/* Mount the page. Returns the page and helpers. */
async function mount({routes, path = '/', search = '', hash = '', docs = null}) {
  const document = new FakeDocument();
  buildPage(document);
  const loc = {pathname: path, search, hash};
  const history = {
    entries: [],
    pushState(s, t, url) { this.entries.push(['push', url]); setUrl(url); },
    replaceState(s, t, url) { this.entries.push(['replace', url]); setUrl(url); },
  };
  function setUrl(url) {
    const q = url.indexOf('?');
    loc.pathname = q < 0 ? url : url.slice(0, q);
    loc.search = q < 0 ? '' : url.slice(q);
    loc.hash = '';
  }
  const listeners = {};
  const win = {addEventListener: (type, fn) => { listeners[type] = fn; }, HubDocs: docs};
  const hub = fakeHub(routes);
  const pending = [];
  const timers = {setTimeout: (fn) => { pending.push(fn); return pending.length; }, clearTimeout: (id) => { pending[id - 1] = null; }};
  const session = storage();
  const context = vm.createContext({URL, URLSearchParams, AbortController, Promise, JSON, Math, Date, Error, TypeError});
  vm.runInContext(CORE + '\n' + APP + '\nthis.HubApp = HubApp; this.HubCore = HubCore;', context);
  const page = context.HubApp.mount({
    document, location: loc, history, window: win, fetch: hub.fetch, sessionStorage: session,
    localStorage: storage(), setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout,
    clipboard: null, scrollTo: () => {},
  });
  await page.ready;
  await settleAll();
  const main = document.getElementById('main');
  return {
    document, loc, history, hub, page, main, session, pending, win,
    text: () => main.textContent,
    find: (selector, text) => main.querySelectorAll(selector).find((el) => text === undefined || el.textContent.includes(text)),
    async click(el) { el.fire('click', {preventDefault() {}, button: 0, target: el}); await settleAll(); },
    async submit(form) { form.fire('submit', {preventDefault() {}}); await settleAll(); },
    async back(url) { setUrl(url); listeners.popstate(); await settleAll(); },
    async tick() { const fns = pending.splice(0).filter(Boolean); fns.forEach((f) => f()); await settleAll(); },
  };
}

const SERVER_CONFIG = {status: 200, body: {role: 'server', api_version: 1, registration_mode: 'invite', limits: {}}};
const RIG_CONFIG = {status: 200, body: {role: 'rig', api_version: 1, rig: {base_url: 'https://hub.example.org', connected: true, signed_in: true, workspace_url: '/'}, server: {}, server_error: null}};
const ALICE = {id: 'u1', username: 'alice', display_name: 'Alice A'};
const signedOut = () => ({status: 401, body: {error: {code: 'unauthenticated', message: 'Sign in'}}});

function release(over) {
  return Object.assign({
    id: 'v1', experiment_id: 'e1', version: '1.2.0', sha256: 'ab'.repeat(32), size: 2048,
    created_at: '2026-10-09T10:00:00Z',
    manifest: {name: 'fix', version: '1.2.0', entrypoint: 'run.py', python_min: '3.10', alhazen_min: '2.13.0',
      platforms: ['linux'], hardware: {display: true, eye_tracker: true, reward: false}, license: 'MIT', files: [{path: 'run.py'}]},
  }, over || {});
}
function experiment(over) {
  return Object.assign({id: 'e1', title: 'Fixation demo', summary: 'Hold gaze.', description: 'Para one.\n\nPara two.',
    owner: {id: 'u2', username: 'bob', display_name: 'Bob B'}, license: 'MIT', citations: ['Doe 2020 https://doi.org/10.1/x'],
    tags: ['fixation'], published_version_id: 'v1', created_at: '2026-10-01T00:00:00Z'}, over || {});
}

function inputsOf(root) {
  return root.querySelectorAll('input');
}

/* ---------------------------------------------------------------------- */

test('index.html loads only its own files, with no inline script or style', () => {
  assert.doesNotMatch(HTML, /<script(?![^>]*\bsrc=)[^>]*>/);
  assert.doesNotMatch(HTML, /\sstyle=/);
  assert.doesNotMatch(HTML, /<style/);
  for (const name of ['hub.css', 'hub_core.js', 'hub.js', 'hub_docs.js', 'hub_docs.css', 'icon.svg']) {
    assert.match(HTML, new RegExp('"/hub/assets/' + name.replace('.', '\\.') + '"'), name);
  }
  assert.ok(HTML.indexOf('hub_core.js') < HTML.indexOf('hub_docs.js') && HTML.indexOf('hub_docs.js') < HTML.indexOf('/hub.js'));
  for (const src of HTML.matchAll(/(?:src|href)="(https?:)?\/\//g)) assert.fail('external reference ' + src[0]);
  for (const id of APP.matchAll(/\$\('([\w-]+)'\)/g)) assert.match(HTML, new RegExp('id="' + id[1] + '"'), id[1]);
});

test('the scripts never parse markup or evaluate text', () => {
  for (const [name, source] of [['hub.js', APP], ['hub_core.js', CORE]]) {
    assert.doesNotMatch(source, /innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function|setAttribute\('style'/, name);
    assert.doesNotMatch(source, /localStorage\.setItem\([^)]*(token|csrf|password)/i, name);
  }
});

test('the two dark palettes are identical and rules name tokens, not colours', () => {
  const block = (marker) => {
    const start = CSS.indexOf(marker);
    const open = CSS.indexOf('{', CSS.indexOf(marker === 'DARK-AUTO' ? ':root:not' : ':root[data-theme=dark]', start));
    return CSS.slice(open + 1, CSS.indexOf('}', open)).replace(/\s+/g, ' ').trim();
  };
  assert.equal(block('DARK-AUTO'), block('DARK-EXPLICIT'));
  const hd = (marker, selector) => {
    const open = CSS.indexOf('{', CSS.indexOf(selector, CSS.indexOf(marker)));
    return CSS.slice(open + 1, CSS.indexOf('}', open)).replace(/\s+/g, ' ').trim();
  };
  const hdAuto = hd('HD-DARK-AUTO', ':root:not([data-theme=light]) .hd-doc');
  assert.equal(hdAuto, hd('HD-DARK-EXPLICIT', ':root[data-theme=dark] .hd-doc'));
  for (const name of ['ink', 'muted', 'faint', 'paper', 'soft', 'rule', 'accent', 'accent-soft', 'fail', 'abort', 'ok']) {
    assert.match(hdAuto, new RegExp('--hd-' + name + ': var\\(--'), name);
  }
  const firstRule = CSS.indexOf('/* ---- base ---- */');
  assert.doesNotMatch(CSS.slice(firstRule), /#[0-9a-fA-F]{3,8}\b|rgb\(|hsl\(/);
});

test('central visitor: the landing page says what is real, with an honest empty catalogue', async () => {
  const p = await mount({routes: {
    'GET /config': () => SERVER_CONFIG, 'GET /auth/me': signedOut,
    'GET /catalog': () => ({status: 200, body: {items: [], next_offset: null}}),
  }});
  assert.match(p.text(), /Nothing is published yet/);
  assert.match(p.text(), /Experiment.*Rig.*Data/s);
  const nav = p.document.getElementById('primary-nav').textContent;
  assert.match(nav, /Catalogue/);
  assert.doesNotMatch(nav, /This rig/);
  assert.match(p.document.getElementById('account').textContent, /Sign in.*Register/);
  assert.match(p.document.getElementById('role-badge').textContent, /Hub/);
  assert.equal(p.hub.calls.find((c) => c.path.endsWith('/catalog')).query.get('limit'), '6');
});

test('server text is shown as text: a hostile title creates no elements', async () => {
  const evil = '<img src=x onerror=alert(1)>';
  const p = await mount({routes: {
    'GET /config': () => SERVER_CONFIG, 'GET /auth/me': signedOut,
    'GET /catalog': () => ({status: 200, body: {items: [{experiment: experiment({title: evil, summary: evil}), version: release()}], next_offset: null}}),
  }, search: '?view=catalog'});
  assert.ok(p.text().includes(evil));
  assert.equal(p.main.querySelectorAll('img').length, 0);
  assert.equal(p.main.querySelectorAll('script').length, 0);
});

test('a private screen sends a visitor to sign in, then back where they were going', async () => {
  const p = await mount({routes: {
    'GET /config': () => SERVER_CONFIG, 'GET /auth/me': signedOut,
    'POST /auth/login': () => ({status: 200, body: {user: ALICE, csrf_token: 'c1'}}),
    'GET /library': () => ({status: 200, body: {items: [], next_offset: null}}),
  }, search: '?view=library'});
  assert.equal(p.loc.search, '?view=signin&next=%3Fview%3Dlibrary');
  const form = p.main.querySelector('form');
  const [user, password] = inputsOf(form);
  user.value = 'alice';
  password.value = 'correct horse battery';
  await p.submit(form);
  const login = p.hub.calls.find((c) => c.path.endsWith('/auth/login'));
  assert.deepEqual(login.json, {username: 'alice', password: 'correct horse battery'});
  assert.equal(password.value, '');
  assert.equal(p.loc.search, '?view=library');
  assert.match(p.text(), /Your library is empty/);
  assert.match(p.document.getElementById('account').textContent, /Alice A/);
  // Later central writes carry the CSRF token the login returned.
  assert.equal(p.page.state.csrf, 'c1');
});

test('a wrong password says so without naming which part was wrong', async () => {
  const p = await mount({routes: {
    'GET /config': () => SERVER_CONFIG, 'GET /auth/me': signedOut,
    'POST /auth/login': () => ({status: 401, body: {error: {code: 'invalid_credentials', message: 'x'}}}),
  }, search: '?view=signin'});
  const form = p.main.querySelector('form');
  const [user, password] = inputsOf(form);
  user.value = 'alice';
  password.value = 'nope nope nope';
  await p.submit(form);
  assert.match(p.text(), /do not match an account/);
  assert.equal(p.loc.search, '?view=signin');
});

test('registration checks the password rule before asking the hub', async () => {
  const p = await mount({routes: {
    'GET /config': () => SERVER_CONFIG, 'GET /auth/me': signedOut,
    'POST /auth/register': () => ({status: 200, body: {user: ALICE}}),
  }, search: '?view=register'});
  const form = p.main.querySelector('form');
  const [user, display, pw, repeat, invite] = inputsOf(form);
  user.value = 'alice'; display.value = 'Alice A'; invite.value = 'INV';
  pw.value = 'short'; repeat.value = 'short';
  await p.submit(form);
  assert.equal(p.hub.calls.filter((c) => c.path.endsWith('/auth/register')).length, 0);
  assert.match(p.text(), /at least 12/);
  pw.value = 'x'.repeat(14); repeat.value = 'x'.repeat(14);
  await p.submit(form);
  assert.equal(p.hub.calls.filter((c) => c.path.endsWith('/auth/register')).length, 1);
  assert.equal(p.loc.search, '?view=signin');
  assert.match(p.text(), /Account created for @alice/);
  assert.equal(inputsOf(p.main.querySelector('form'))[0].value, 'alice');
});

test('rig: the token is read from the fragment, kept in the tab and sent on every request', async () => {
  const p = await mount({path: '/hub', hash: '#token=' + TOKEN, routes: {
    'GET /config': () => RIG_CONFIG,
    'GET /auth/me': () => ({status: 200, body: {user: ALICE, csrf_token: null}}),
    'GET /local/status': () => ({status: 200, body: {state: 'signed_in', base_url: 'https://hub.example.org', user: ALICE, installed: [], jobs: [], run_active: false, interpreters: []}}),
    'GET /catalog': () => ({status: 200, body: {items: [], next_offset: null}}),
  }});
  assert.equal(p.session.getItem('alhazen-workspace-token'), TOKEN);
  assert.deepEqual(p.history.entries[0], ['replace', '/hub']);
  assert.ok(p.hub.calls.length >= 4);
  for (const call of p.hub.calls) {
    assert.equal(call.init.headers['X-Alhazen-Token'], TOKEN);
    assert.equal(call.init.headers['X-CSRF-Token'], undefined);
  }
  assert.match(p.document.getElementById('primary-nav').textContent, /This rig/);
  assert.match(p.document.getElementById('account').textContent, /Operator.*Alice A.*@alice/);
  assert.match(p.document.getElementById('role-badge').textContent, /Rig.*hub\.example\.org/);
  assert.match(p.text(), /Operator/);
});

test('rig without its token says how to open the page instead of failing silently', async () => {
  const p = await mount({path: '/hub', routes: {
    'GET /config': () => ({status: 403, body: {error: 'Open the dashboard using the URL printed by alhazen dashboard'}}),
  }});
  assert.match(p.text(), /alhazen dashboard --hub/);
});

test('Back and Forward redraw the screen and drop answers for the screen left behind', async () => {
  let releaseSlow;
  const p = await mount({routes: {
    'GET /config': () => SERVER_CONFIG, 'GET /auth/me': signedOut,
    'GET /catalog': (req) => (req.query.get('query') === 'slow'
      ? new Promise((resolve) => { releaseSlow = () => resolve({status: 200, body: {items: [{experiment: experiment({title: 'STALE'}), version: release()}]}}); })
      : {status: 200, body: {items: [], next_offset: null}}),
  }, search: '?view=catalog&q=slow'});
  await p.click(p.document.getElementById('brand'));
  assert.equal(p.loc.search, '');
  releaseSlow();
  await settleAll();
  assert.doesNotMatch(p.text(), /STALE/);
  assert.match(p.text(), /Share the experiment/);
  assert.equal(p.document.activeElement.getAttribute('data-heading'), '');
  await p.back('/?view=catalog');
  assert.match(p.text(), /Published experiments/);
});

test('experiment page: release sheet, citations as text with an https link, download only on central', async () => {
  const p = await mount({routes: {
    'GET /config': () => SERVER_CONFIG, 'GET /auth/me': signedOut,
    'GET /experiments/e1': () => ({status: 200, body: {experiment: experiment(), versions: [release()]}}),
  }, search: '?view=experiment&id=e1'});
  const t = p.text();
  assert.match(t, /Fixation demo/);
  assert.match(t, /Public release/);
  assert.ok(t.includes('ab'.repeat(32)), 'the full digest is shown');
  assert.match(t, /Eye tracker.*needed/);
  assert.match(t, /Reward line.*not needed/);
  assert.match(t, /never runs experiments/);
  const download = p.find('a', 'Download the release archive');
  assert.equal(download.getAttribute('href'), '/api/hub/v1/experiments/e1/versions/v1/download');
  const cite = p.find('a', 'Open source');
  assert.equal(cite.getAttribute('href'), 'https://doi.org/10.1/x');
  assert.equal(cite.getAttribute('rel'), 'noopener noreferrer');
  assert.equal(p.find('h3', 'Install on this rig'), undefined);
});

test('Methods and Tasks views ask for this exact version\'s documentation and use the renderer', async () => {
  const scratch = new FakeDocument();
  const text = (value) => { const el = scratch.createElement('div'); el.textContent = value; return el; };
  const docs = {
    renderMethods: (d) => text(d ? 'METHODS' : 'NO METHODS'),
    renderTaskGuide: (d, id) => text('TASK ' + id),
    renderGlobalGuide: () => text('GUIDE'), renderMissing: (kind) => text('MISSING ' + kind),
  };
  const documentation = {tasks: [{id: 'fix', title: 'Fixation'}, {id: 'sac', title: 'Saccade'}]};
  const p = await mount({docs, search: '?view=experiment&id=e1&version=v0&tab=methods', routes: {
    'GET /config': () => SERVER_CONFIG, 'GET /auth/me': signedOut,
    'GET /experiments/e1': () => ({status: 200, body: {experiment: experiment(), versions: [release(), release({id: 'v0', version: '1.0.0', created_at: '2026-01-01T00:00:00Z'})]}}),
    'GET /experiments/e1/versions/v0/documentation': () => ({status: 200, body: {documentation: null}}),
    'GET /experiments/e1/versions/v1/documentation': () => ({status: 200, body: {documentation}}),
  }});
  assert.ok(p.hub.calls.some((c) => c.path.endsWith('/versions/v0/documentation')));
  assert.match(p.text(), /NO METHODS/);
  assert.match(p.text(), /Documentation of v1\.0\.0/);
  await p.back('/?view=experiment&id=e1&tab=tasks&task=sac');
  assert.ok(p.hub.calls.some((c) => c.path.endsWith('/versions/v1/documentation')));
  assert.match(p.text(), /TASK sac/);
  assert.match(p.text(), /Fixation.*Saccade/s);
  assert.equal(p.find('a', 'Saccade').getAttribute('aria-current'), 'page');
});

test('without the documentation viewer the page says so instead of drawing nothing', async () => {
  const p = await mount({routes: {
    'GET /config': () => SERVER_CONFIG, 'GET /auth/me': signedOut,
    'GET /guide': () => ({status: 200, body: {guide: {modes: []}}}),
  }, search: '?view=guide'});
  assert.match(p.text(), /documentation viewer \(hub_docs\.js\) is not available/);
});

test('the Guide accepts the guide wrapped or bare', async () => {
  const got = [];
  for (const body of [{guide: {modes: ['run']}}, {modes: ['run']}]) {
    const docs = {renderMethods: () => null, renderGlobalGuide: (g) => { got.push(g); const d = new FakeDocument(); const el = d.createElement('p'); el.textContent = 'GUIDE'; return el; }};
    const p = await mount({routes: {'GET /config': () => SERVER_CONFIG, 'GET /auth/me': signedOut, 'GET /guide': () => ({status: 200, body})}, docs, search: '?view=guide'});
    assert.match(p.text(), /GUIDE/);
  }
  assert.deepEqual(JSON.parse(JSON.stringify(got)), [{modes: ['run']}, {modes: ['run']}]);
});

function rigRoutes(over) {
  return Object.assign({
    'GET /config': () => RIG_CONFIG,
    'GET /auth/me': () => ({status: 200, body: {user: ALICE, csrf_token: null}}),
    'GET /local/status': () => ({status: 200, body: {state: 'signed_in', base_url: 'https://hub.example.org', user: ALICE,
      installed: [], jobs: [], run_active: false, interpreters: [{path: '/opt/lab/bin/python', label: 'lab'}]}}),
    'GET /library': () => ({status: 200, body: {items: [], next_offset: null}}),
  }, over || {});
}

test('install needs an interpreter and trust in this exact release, then shows the workspace link', async () => {
  let installed = false;
  const p = await mount({path: '/hub', hash: '#token=' + TOKEN, search: '?view=experiment&id=e1', routes: rigRoutes({
    'GET /experiments/e1': () => ({status: 200, body: {experiment: experiment(), versions: [release()]}}),
    'GET /local/status': () => ({status: 200, body: {state: 'signed_in', base_url: 'https://hub.example.org', user: ALICE, jobs: [], run_active: false, interpreters: [],
      trust_statement: 'Trusted code runs as your operating-system user; a virtual environment is not a sandbox.',
      installed: installed ? [{sha256: release().sha256, experiment_id: 'e1', version_id: 'v1', version: '1.2.0', status: 'registered', workspace_url: '/?project=p9', error: null}] : []}}),
    'POST /local/install': () => { installed = true; return {status: 201, body: {install: {sha256: release().sha256, version: '1.2.0', status: 'registered', workspace_url: '/?project=p9', error: null}}}; },
  })});
  assert.match(p.text(), /Trusted code runs as your operating-system user; a virtual environment is not a sandbox\./);
  const form = p.find('form', 'Python interpreter');
  await p.submit(form);
  assert.equal(p.hub.calls.filter((c) => c.path.endsWith('/local/install')).length, 0);
  assert.match(p.text(), /Choose the Python interpreter/);
  form.querySelector('input').value = '/opt/lab/bin/python';
  await p.submit(form);
  assert.equal(p.hub.calls.filter((c) => c.path.endsWith('/local/install')).length, 0);
  assert.match(p.text(), /trust this exact release/);
  form.querySelectorAll('input').find((i) => i.getAttribute('type') === 'checkbox').checked = true;
  await p.submit(form);
  const call = p.hub.calls.find((c) => c.path.endsWith('/local/install'));
  assert.deepEqual(call.json, {experiment_id: 'e1', version_id: 'v1', sha256: release().sha256, trust_code: true, python: '/opt/lab/bin/python'});
  assert.equal(p.find('a', 'Open it in the workspace').getAttribute('href'), '/?project=p9');
});

test('an install the rig refuses is shown in its words, not as installed', async () => {
  const p = await mount({path: '/hub', hash: '#token=' + TOKEN, search: '?view=experiment&id=e1', routes: rigRoutes({
    'GET /experiments/e1': () => ({status: 200, body: {experiment: experiment(), versions: [release()]}}),
    'POST /local/install': () => ({status: 400, body: {error: {code: 'invalid_request', message: 'alhazen is not installed in /usr/bin/python3'}}}),
  })});
  const form = p.find('form', 'Python interpreter');
  form.querySelector('input').value = '/usr/bin/python3';
  form.querySelectorAll('input').find((i) => i.getAttribute('type') === 'checkbox').checked = true;
  await p.submit(form);
  assert.match(p.text(), /alhazen is not installed in \/usr\/bin\/python3/);
  assert.doesNotMatch(p.text(), /Installed and registered/);
});

const SESSION = {root_id: 'r1', root_kind: 'data', root_name: 'data', run_id: 'v1/sub-01/ses-1/run-1', subject: '01', session: 1, run: 1, task: 'fix', complete: true, active: false, job: null};
const PREVIEW = {
  preview_id: 'pv1', manifest_digest: 'cd'.repeat(32), files: [{path: 'session.json', size: 100}, {path: 'trials.csv', size: 900}],
  total_bytes: 1000, file_count: 2, recipient: {base_url: 'https://hub.example.org', user: ALICE},
  experiment_id: 'e1', version_id: 'v1', metadata: {subject_code: 'S01', mode: 'run', rig_alias: 'lab', started_at: '2026-10-09T09:00:00Z'},
  privacy: {fields: ['subject_code', 'age', 'sex'], warning: 'Contains participant information.'},
  install: {sha256: 'ab'.repeat(32), title: 'Fixation demo', version: '1.2.0'},
};

test('upload consent names recipient, account, release and manifest, and binds the preview id', async () => {
  let job = {id: 'j1', status: 'uploading', bytes_done: 250, bytes_total: 1000, files_done: 0, files_total: 2, run_id: SESSION.run_id, error: null};
  const p = await mount({path: '/hub', hash: '#token=' + TOKEN, search: '?view=rig&tab=upload&project=p1&root=r1&run=' + encodeURIComponent(SESSION.run_id), routes: rigRoutes({
    'GET /local/projects': () => ({status: 200, body: {items: [{id: 'p1', title: 'Fixation demo'}]}}),
    'GET /local/sessions': () => ({status: 200, body: {items: [SESSION], problems: []}}),
    'POST /local/upload-preview': () => ({status: 200, body: PREVIEW}),
    'POST /local/upload': () => ({status: 202, body: {job}}),
    'GET /local/jobs/j1': () => ({status: 200, body: {job}}),
  })});
  const t = p.text();
  assert.match(t, /https:\/\/hub\.example\.org/);
  assert.match(t, /Alice A \(@alice\)/);
  assert.match(t, /Fixation demo v1\.2\.0/);
  assert.ok(t.includes('cd'.repeat(32)));
  assert.match(t, /Contains participant information/);
  assert.match(t, /subject_code, age, sex/);
  assert.match(t, /2 files/);
  const form = p.find('form', 'Upload privately');
  await p.submit(form);
  assert.equal(p.hub.calls.filter((c) => c.path.endsWith('/local/upload')).length, 0);
  form.querySelectorAll('input').find((i) => i.getAttribute('type') === 'checkbox').checked = true;
  await p.submit(form);
  const call = p.hub.calls.find((c) => c.path.endsWith('/local/upload'));
  assert.deepEqual(call.json, {project_id: 'p1', root_id: 'r1', run_id: SESSION.run_id, experiment_id: 'e1', version_id: 'v1', preview_id: 'pv1', consent: true});
  assert.equal(p.loc.search, '?view=rig&tab=upload&job=j1');
  assert.match(p.text(), /Uploading/);
  assert.equal(p.main.querySelector('[role="progressbar"]').getAttribute('aria-valuenow'), '25');
  job = Object.assign({}, job, {status: 'completed', bytes_done: 1000, session_id: 's1'});
  await p.tick();
  assert.match(p.text(), /verified every file/);
  assert.equal(p.find('a', 'Open the uploaded session').getAttribute('href'), '/hub?view=data&session=s1');
  const polls = p.hub.calls.filter((c) => c.path.endsWith('/local/jobs/j1')).length;
  await p.tick();
  assert.equal(p.hub.calls.filter((c) => c.path.endsWith('/local/jobs/j1')).length, polls, 'a finished job is not polled again');
});

test('a stale preview is previewed again instead of uploading under old consent', async () => {
  let previews = 0;
  const p = await mount({path: '/hub', hash: '#token=' + TOKEN, search: '?view=rig&tab=upload&project=p1&root=r1&run=' + encodeURIComponent(SESSION.run_id), routes: rigRoutes({
    'GET /local/projects': () => ({status: 200, body: {items: [{id: 'p1', title: 'Fixation demo'}]}}),
    'GET /local/sessions': () => ({status: 200, body: {items: [SESSION], problems: []}}),
    'POST /local/upload-preview': () => { previews += 1; return {status: 200, body: Object.assign({}, PREVIEW, {preview_id: 'pv' + previews})}; },
    'POST /local/upload': () => ({status: 409, body: {error: {code: 'preview_stale', message: 'Files changed since the preview.'}}}),
  })});
  const form = p.find('form', 'Upload privately');
  form.querySelectorAll('input').find((i) => i.getAttribute('type') === 'checkbox').checked = true;
  await p.submit(form);
  assert.equal(previews, 2);
  const fresh = p.find('form', 'Upload privately');
  assert.ok(!fresh.querySelectorAll('input').find((i) => i.getAttribute('type') === 'checkbox').checked, 'new consent required');
  assert.notEqual(fresh, form, 'the consent form was drawn again from the new preview');
  assert.doesNotMatch(p.loc.search, /job=/);
});

test('a transfer paused for another account cannot be resumed here; an interrupted one can', async () => {
  let job = {id: 'j2', status: 'paused', bytes_done: 10, bytes_total: 100, project_id: 'p1', root_id: 'r1', run_id: SESSION.run_id,
    error: {code: 'auth_context_changed', message: 'Signed in as someone else.', retryable: false}};
  const p = await mount({path: '/hub', hash: '#token=' + TOKEN, search: '?view=rig&tab=upload&job=j2', routes: rigRoutes({
    'GET /local/jobs/j2': () => ({status: 200, body: {job}}),
    'POST /local/jobs/j2/resume': () => { job = Object.assign({}, job, {status: 'uploading', error: null}); return {status: 200, body: {job}}; },
  })});
  assert.match(p.text(), /Signed in as someone else/);
  assert.match(p.text(), /approved for/);
  assert.equal(p.find('button', 'Resume'), undefined);
  job = Object.assign({}, job, {error: {code: 'interrupted', message: 'Connection lost.', retryable: true}});
  await p.tick();
  await p.click(p.find('button', 'Resume'));
  assert.ok(p.hub.calls.some((c) => c.path.endsWith('/local/jobs/j2/resume')));
  assert.match(p.text(), /Uploading/);
  job = Object.assign({}, job, {status: 'failed', error: {code: 'local_changed', message: 'Files changed after the preview.', retryable: false}});
  await p.tick();
  assert.equal(p.find('a', 'Preview this session again').getAttribute('href'),
    '/hub?view=rig&tab=upload&project=p1&root=r1&run=' + encodeURIComponent(SESSION.run_id));
});

test('publishing needs both acknowledgements and a confirmation, then sends exactly them', async () => {
  const mine = experiment({owner: ALICE, published_version_id: null});
  const p = await mount({search: '?view=mine&id=e1', routes: {
    'GET /config': () => SERVER_CONFIG,
    'GET /auth/me': () => ({status: 200, body: {user: ALICE, csrf_token: 'c1'}}),
    'GET /experiments/e1': () => ({status: 200, body: {experiment: mine, versions: [release()]}}),
    'POST /experiments/e1/publish': () => ({status: 200, body: {experiment: Object.assign({}, mine, {published_version_id: 'v1'})}}),
  }});
  const form = p.find('form', 'Version to publish');
  await p.submit(form);
  assert.match(p.text(), /Tick both confirmations/);
  for (const box of form.querySelectorAll('input').filter((i) => i.getAttribute('type') === 'checkbox')) box.checked = true;
  await p.submit(form);
  assert.equal(p.hub.calls.filter((c) => c.path.endsWith('/publish')).length, 0, 'the first press only asks');
  assert.match(p.text(), /Publish v1\.2\.0 .* publicly\?/);
  await p.click(p.find('button', 'Publish now'));
  const call = p.hub.calls.find((c) => c.path.endsWith('/publish'));
  assert.deepEqual(call.json, {version_id: 'v1', license_ack: true, data_excluded_ack: true});
  assert.equal(call.init.headers['X-CSRF-Token'], 'c1');
});

test('a session that ends mid-use signs the page out and says so', async () => {
  const p = await mount({search: '?view=library', routes: {
    'GET /config': () => SERVER_CONFIG,
    'GET /auth/me': () => ({status: 200, body: {user: ALICE, csrf_token: 'c1'}}),
    'GET /library': () => ({status: 401, body: {error: {code: 'unauthenticated', message: 'Session expired'}}}),
  }});
  assert.equal(p.page.state.user, null);
  assert.match(p.document.getElementById('banner').textContent, /session ended/);
  assert.match(p.text(), /Sign in again/);
  assert.match(p.document.getElementById('account').textContent, /Sign in/);
});

test('an unreachable hub on a rig says local work is unaffected and offers Retry', async () => {
  const p = await mount({path: '/hub', hash: '#token=' + TOKEN, search: '?view=catalog', routes: rigRoutes({
    'GET /catalog': () => ({status: 502, body: {error: {code: 'hub_unreachable', message: 'The hub did not answer.'}}}),
  })});
  const banner = p.document.getElementById('banner');
  assert.equal(banner.hidden, false);
  assert.match(banner.textContent, /data on this rig are unaffected/);
  assert.ok(banner.querySelector('button'));
  assert.match(p.text(), /The hub did not answer/);
});

/* Shapes as the server writes them (hub/uploads.py session_row_view and
 * receipt, hub/data.py session_detail, app.py trials). */
function sessionView(over) {
  return Object.assign({
    id: 's1', experiment_id: 'e1', version_id: 'v1', client_session_id: 'c1', status: 'committed',
    metadata: {subject_code: 'S01', mode: 'run', rig_alias: 'lab', started_at: '2026-10-09T09:00:00Z'},
    created_at: '2026-10-09T09:30:00Z', completed_at: '2026-10-09T09:31:00Z', manifest_sha256: 'ef'.repeat(32),
    total_bytes: 1010, file_count: 2, index: {status: 'indexed', rows: 1, error: null}, experiment_title: 'Fixation demo',
  }, over || {});
}
function sessionDetail(view) {
  return {session: view, receipt: {id: view.id, status: view.status, manifest_sha256: view.manifest_sha256,
    durability: "verified on the hub's primary storage; not an independent backup", index: view.index},
  artifacts: [{path: 'session.json', size: 10, sha256: '34'.repeat(32)}, {path: 'trials.csv', size: 1000, sha256: '12'.repeat(32)}],
  columns: view.index.status === 'indexed' ? ['trial', 'rt'] : []};
}
const ALICE_ROUTES = {
  'GET /config': () => SERVER_CONFIG,
  'GET /auth/me': () => ({status: 200, body: {user: ALICE, csrf_token: 'c1'}}),
  'GET /experiments': () => ({status: 200, body: {items: []}}),
  'GET /library': () => ({status: 200, body: {items: []}}),
};

test('data: filters become the address, sessions link to their detail, exports are authorised links', async () => {
  const p = await mount({search: '?view=data', routes: Object.assign({}, ALICE_ROUTES, {
    'GET /data/sessions': (req) => ({status: 200, body: {items: req.query.get('subject_code') === 'S01' ? [sessionView()] : [], next_offset: null}}),
    'GET /data/sessions/s1': () => ({status: 200, body: sessionDetail(sessionView())}),
    'GET /data/sessions/s1/trials': () => ({status: 200, body: {items: [{rt: 0.31, trial: 1}], next_offset: null, columns: ['trial', 'rt'], index: {status: 'indexed', rows: 1, error: null}}}),
  })});
  assert.match(p.text(), /No sessions uploaded yet/);
  const form = p.main.querySelector('form');
  form.querySelectorAll('input')[0].value = 'S01';
  await p.submit(form);
  assert.equal(p.loc.search, '?view=data&subject=S01');
  assert.match(p.text(), /Fixation demo/, 'the server\'s experiment_title labels the row');
  assert.match(p.text(), /trial rows indexed/);
  await p.click(p.main.querySelector('td').querySelector('a'));
  assert.equal(p.loc.search, '?view=data&subject=S01&session=s1');
  const t = p.text();
  assert.match(t, /not an independent backup/);
  assert.match(t, /1 trial rows indexed/);
  assert.match(t, /0\.31/);
  const headers = p.main.querySelectorAll('th').map((th) => th.textContent);
  assert.ok(headers.indexOf('trial') < headers.indexOf('rt'), 'columns in the server\'s declared order');
  assert.equal(p.find('a', 'CSV').getAttribute('href'), '/api/hub/v1/data/sessions/s1/export?format=csv');
  assert.equal(p.find('a', 'trials.csv').getAttribute('href'), '/api/hub/v1/data/sessions/s1/files?path=trials.csv');
  assert.equal(p.find('button', 'Rebuild trial index'), undefined, 'no rebuild offered for a ready index');
});

test('a failed trial index keeps files readable and offers Rebuild; queued, then ready after polling', async () => {
  let current = sessionView({index: {status: 'failed', rows: 0, error: 'more than 100000 trial rows (the index budget)'}});
  let reindexCalls = 0;
  const p = await mount({search: '?view=data&session=s1', routes: Object.assign({}, ALICE_ROUTES, {
    'GET /data/sessions/s1': () => ({status: 200, body: sessionDetail(current)}),
    'POST /data/sessions/s1/reindex': () => {
      reindexCalls += 1;
      current = sessionView({index: {status: 'pending', rows: 0, error: null}});
      return {status: 202, body: sessionDetail(current)};
    },
    'GET /data/sessions/s1/trials': () => ({status: 200, body: {items: [{trial: 1, rt: 0.5}], next_offset: null, columns: ['trial', 'rt'], index: current.index}}),
  })});
  let t = p.text();
  assert.match(t, /the index budget/);
  assert.match(t, /raw files are kept/);
  assert.equal(p.find('a', 'trials.csv').getAttribute('href'), '/api/hub/v1/data/sessions/s1/files?path=trials.csv');
  assert.equal(p.find('a', 'CSV'), undefined, 'no export while the index is not ready');
  assert.equal(p.hub.calls.filter((c) => c.path.endsWith('/trials')).length, 0);
  await p.click(p.find('button', 'Rebuild trial index'));
  assert.equal(reindexCalls, 1);
  const post = p.hub.calls.find((c) => c.path.endsWith('/reindex'));
  assert.equal(post.init.method, 'POST');
  assert.equal(post.init.headers['X-CSRF-Token'], 'c1');
  t = p.text();
  assert.match(t, /queued for rebuilding/);
  assert.doesNotMatch(t, /0\.5/, 'no rows before the server says indexed');
  assert.equal(p.find('button', 'Rebuild trial index'), undefined);
  // The hub is still working: the page checks again and keeps waiting.
  current = sessionView({index: {status: 'indexing', rows: 0, error: null}});
  await p.tick();
  assert.match(p.text(), /being rebuilt/);
  current = sessionView({index: {status: 'indexed', rows: 1, error: null}});
  await p.tick();
  t = p.text();
  assert.match(t, /1 trial rows indexed/);
  assert.match(t, /0\.5/);
  assert.equal(p.find('a', 'CSV').getAttribute('href'), '/api/hub/v1/data/sessions/s1/export?format=csv');
  const polls = p.hub.calls.filter((c) => c.path === '/api/hub/v1/data/sessions/s1').length;
  await p.tick();
  assert.equal(p.hub.calls.filter((c) => c.path === '/api/hub/v1/data/sessions/s1').length, polls, 'polling stops once ready');
});

test('a rebuild that fails again, or is refused, is said plainly and can be retried', async () => {
  let current = sessionView({index: {status: 'failed', rows: 0, error: 'the trial table could not be indexed'}});
  let refuse = true;
  const p = await mount({search: '?view=data&session=s1', routes: Object.assign({}, ALICE_ROUTES, {
    'GET /data/sessions/s1': () => ({status: 200, body: sessionDetail(current)}),
    'POST /data/sessions/s1/reindex': () => {
      if (refuse) return {status: 429, body: {error: {code: 'rate_limited', message: 'Too many rebuilds; try later.'}}};
      current = sessionView({index: {status: 'pending', rows: 0, error: null}});
      return {status: 202, body: sessionDetail(current)};
    },
  })});
  await p.click(p.find('button', 'Rebuild trial index'));
  assert.match(p.text(), /The rebuild was not started: Too many rebuilds; try later\./);
  const button = p.find('button', 'Rebuild trial index');
  assert.ok(!button.disabled, 'the button is usable again');
  refuse = false;
  await p.click(button);
  assert.match(p.text(), /queued for rebuilding/);
  current = sessionView({index: {status: 'failed', rows: 0, error: 'still unreadable'}});
  await p.tick();
  assert.match(p.text(), /still unreadable/);
  assert.ok(p.find('button', 'Rebuild trial index'), 'Rebuild offered again after a second failure');
  assert.doesNotMatch(p.text(), /trial rows indexed/);
});

test('rig connection: plain http to this computer is sent with its explicit allowance', async () => {
  const p = await mount({path: '/hub', hash: '#token=' + TOKEN, search: '?view=rig&tab=connection', routes: rigRoutes({
    'GET /local/status': () => ({status: 200, body: {state: 'not_configured', base_url: null, user: null, installed: [], jobs: [], run_active: false, interpreters: []}}),
    'GET /auth/me': () => ({status: 409, body: {error: {code: 'not_connected', message: 'No hub'}}}),
    'POST /local/connect': () => ({status: 200, body: {state: 'signed_out', base_url: 'http://127.0.0.1:8765', user: null, installed: [], jobs: []}}),
  })});
  const form = p.find('form', 'Hub address');
  const url = form.querySelector('input');
  url.value = 'https://user:pw@hub.example.org';
  await p.submit(form);
  assert.equal(p.hub.calls.filter((c) => c.path.endsWith('/local/connect')).length, 0);
  url.value = 'http://127.0.0.1:8765';
  await p.submit(form);
  const call = p.hub.calls.find((c) => c.path.endsWith('/local/connect'));
  assert.deepEqual(call.json, {url: 'http://127.0.0.1:8765', allow_http_loopback: true});
  assert.match(p.text(), /Connected to http:\/\/127\.0\.0\.1:8765/);
});

test('rig sign-out that could not reach the hub says the sign-in stays valid there', async () => {
  const p = await mount({path: '/hub', hash: '#token=' + TOKEN, routes: rigRoutes({
    'GET /catalog': () => ({status: 200, body: {items: []}}),
    'POST /auth/logout': () => ({status: 200, body: {ok: true, revoked: false, paused_jobs: 1}}),
  })});
  await p.click(p.document.getElementById('account').querySelector('button'));
  assert.match(p.text(), /could not be reached to revoke/);
  assert.match(p.text(), /1 upload was paused/);
});

test('focus: a new screen focuses its heading once it has loaded; a pager keeps focus on the pager', async () => {
  const items = Array.from({length: 20}, (_, i) => ({experiment: experiment({id: 'e' + i, title: 'Exp ' + i}), version: release({id: 'v' + i})}));
  const p = await mount({search: '?view=catalog', routes: {
    'GET /config': () => SERVER_CONFIG, 'GET /auth/me': signedOut,
    'GET /catalog': (req) => ({status: 200, body: {items: req.query.get('offset') === '20' ? items.slice(0, 3) : items, next_offset: req.query.get('offset') === '20' ? null : 20}}),
    'GET /experiments/e1': () => ({status: 200, body: {experiment: experiment(), versions: [release()]}}),
  }});
  await p.click(p.find('a', 'Next'));
  assert.equal(p.loc.search, '?view=catalog&offset=20');
  assert.equal(p.document.activeElement.textContent, 'Previous', 'focus stays on the pager');
  await p.back('/?view=catalog');
  await p.click(p.find('a', 'Exp 1'));
  assert.equal(p.loc.search, '?view=experiment&id=e1&version=v1');
  const active = p.document.activeElement;
  assert.equal(active.getAttribute('data-heading'), '');
  assert.equal(active.textContent, 'Fixation demo');
});

test('a session of a project not installed from the hub is recorded against a release the operator picks', async () => {
  const previews = [];
  const p = await mount({path: '/hub', hash: '#token=' + TOKEN, search: '?view=rig&tab=upload&project=p1&root=r1&run=' + encodeURIComponent(SESSION.run_id), routes: rigRoutes({
    'GET /local/projects': () => ({status: 200, body: {items: [{id: 'p1', title: 'Local study'}]}}),
    'GET /local/sessions': () => ({status: 200, body: {items: [SESSION], problems: []}}),
    'GET /library': () => ({status: 200, body: {items: [{experiment: experiment(), version: release()}]}}),
    'POST /local/upload-preview': (req) => {
      previews.push(req.json);
      return req.json.experiment_id
        ? {status: 200, body: Object.assign({}, PREVIEW, {install: null})}
        : {status: 400, body: {error: {code: 'invalid_request', message: 'This project has no hub release.'}}};
    },
  })});
  assert.match(p.text(), /This project has no hub release/);
  await p.click(p.find('button', 'Use this release'));
  assert.deepEqual(previews[1], {project_id: 'p1', root_id: 'r1', run_id: SESSION.run_id, experiment_id: 'e1', version_id: 'v1'});
  assert.match(p.text(), /recorded against Fixation demo v1\.2\.0/);
});
