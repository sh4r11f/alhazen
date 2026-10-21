// Tests of the standalone HTTP host (platform-web/src/host_http.jsx) against a fake fetch.
// Needs the build dependencies (esbuild compiles the JSX):
//   ALHAZEN_PLATFORM_NODE_MODULES=/tmp/apw-node/node_modules node --test platform-web/test/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.join(HERE, '..', 'src');
const modules = process.env.ALHAZEN_PLATFORM_NODE_MODULES || path.join(HERE, '..', 'node_modules');
const require = createRequire(path.join(modules, 'noop.js'));
const esbuild = require('esbuild');

function loadHost({ responses, href = 'http://127.0.0.1:8123/', secure = true }) {
  const calls = [];
  const timers = { set: 0, cleared: 0 };
  const listeners = {};
  const window = {
    location: { href, hash: '' }, isSecureContext: secure,
    setInterval: () => { timers.set++; return 1; }, clearInterval: () => { timers.cleared++; },
    setTimeout: (fn) => fn(), addEventListener: (k, fn) => { listeners[k] = fn; }, removeEventListener: (k) => { delete listeners[k]; },
  };
  async function fetch(url, init) {
    calls.push({ url, ...init });
    const key = (init.method || 'GET') + ' ' + new URL(url).pathname;
    const next = responses[key];
    if (!next) throw new Error('unexpected request ' + key);
    const r = typeof next === 'function' ? next(init) : next;
    const status = r.status || 200;
    const body = r.body === undefined ? {} : r.body;
    return { ok: status < 400, status, text: async () => JSON.stringify(body), json: async () => body, blob: async () => body };
  }
  const source = fs.readFileSync(path.join(SRC, 'host_http.jsx'), 'utf8');
  const code = esbuild.transformSync(source, { loader: 'jsx' }).code;
  const api = new Function('window', 'fetch', 'document', 'navigator', 'React', 'DARK', 'LIGHT', 'PlatformApp', 'PLATFORM_ASSETS', 'YAML',
    code + '\nreturn { createTransport, createPoller, createHttpHost, readFragmentToken };')(
    window, fetch, {}, {}, {}, {}, {}, null, {}, { parseDocument: () => null });
  return { api, calls, timers, listeners };
}

const flush = () => new Promise((r) => setImmediate(r));

test('the launch token comes from the URL fragment', () => {
  const { api } = loadHost({ responses: {} });
  assert.equal(api.readFragmentToken('#token=abc123'), 'abc123');
  assert.equal(api.readFragmentToken('#other=1&token=x%2Fy'), 'x/y');
  assert.equal(api.readFragmentToken(''), '');
});

test('local requests carry the token header and never a CSRF header', async () => {
  const { api, calls } = loadHost({ responses: { 'GET /api/session': { body: { kind: 'local' } }, 'POST /api/forks': { body: { id: 'f1' } } } });
  const t = api.createTransport({ kind: 'local', token: 'tok', getCsrf: () => 'csrf', onSignedOut: () => {} });
  await t.request('GET', 'api/session');
  await t.request('POST', 'api/forks', { json: { a: 1 } });
  assert.equal(calls[0].headers['X-Alhazen-Token'], 'tok');
  assert.equal(calls[1].headers['X-Alhazen-CSRF'], undefined);
  assert.equal(calls[1].headers['Content-Type'], 'application/json');
  assert.equal(calls[1].body, '{"a":1}');
});

test('server writes carry the CSRF header; reads do not; no token is sent', async () => {
  const { api, calls } = loadHost({ responses: { 'GET /api/forks': { body: { forks: {} } }, 'DELETE /api/forks/x': { body: {} } } });
  const t = api.createTransport({ kind: 'server', token: '', getCsrf: () => 'c-1', onSignedOut: () => {} });
  await t.request('GET', 'api/forks');
  await t.request('DELETE', 'api/forks/x');
  assert.equal(calls[0].headers['X-Alhazen-CSRF'], undefined);
  assert.equal(calls[1].headers['X-Alhazen-CSRF'], 'c-1');
  assert.equal(calls[1].headers['X-Alhazen-Token'], undefined);
  assert.equal(calls[1].credentials, 'same-origin');
});

test('an API error surfaces its message and status; 401 signals sign-out', async () => {
  let signedOut = 0;
  const { api } = loadHost({ responses: {
    'GET /api/forks': { status: 401, body: { error: { kind: 'AuthError', message: 'Not signed in.' } } },
    'POST /api/forks': { status: 409, body: { error: { kind: 'Conflict', message: 'Already there.' } } } } });
  const t = api.createTransport({ kind: 'server', token: '', getCsrf: () => 'c', onSignedOut: () => { signedOut++; } });
  await assert.rejects(t.request('GET', 'api/forks'), (e) => e.status === 401 && e.message === 'Not signed in.' && e.kind === 'AuthError');
  assert.equal(signedOut, 1);
  await assert.rejects(t.request('POST', 'api/forks', { json: {} }), (e) => e.status === 409 && e.message === 'Already there.');
  assert.equal(signedOut, 1);
});

function makeHost(responses, session = { kind: 'local', viewer: { uid: 'local', name: 'This computer' }, capabilities: { fork: true, publish: true, uploadRevision: true, signIn: false } }) {
  const env = loadHost({ responses });
  const transport = env.api.createTransport({ kind: session.kind, token: 'tok', getCsrf: () => '', onSignedOut: () => {} });
  const host = env.api.createHttpHost({ session, transport, onSignedOut: () => {}, useThemeHook: () => ({ theme: 'light' }) });
  return { ...env, host };
}

test('capabilities never claim Mana features, whatever the server says', () => {
  const { host } = makeHost({}, { kind: 'local', viewer: null, capabilities: { changeWithMana: true, machines: true, fork: true } });
  assert.equal(host.capabilities.changeWithMana, false);
  assert.equal(host.capabilities.runOnMachines, false);
  assert.equal(host.capabilities.newWithMana, false);
  assert.equal(host.capabilities.machines, false);
  assert.equal(host.capabilities.fork, true);
  assert.throws(() => host.startJob({ id: 'f' }, 'change', 'x'), /not available on this host/);
  assert.throws(() => host.newExperiment('t', 'x'), /not available on this host/);
});

test('a subscription calls back only when the response changes; the last unsubscribe stops polling', async () => {
  let version = 1;
  const { host, timers, listeners } = makeHost({ 'GET /api/forks': () => ({ body: { forks: { a: { title: 'v' + version } } } }) });
  const seen = [];
  const off = host.subscribeForks((v) => seen.push(v.a.title));
  await flush();
  await host.refresh();
  assert.deepEqual(seen, ['v1']);
  version = 2;
  await listeners.focus();
  await flush();
  assert.deepEqual(seen, ['v1', 'v2']);
  assert.equal(timers.set, 1);
  off();
  assert.equal(timers.cleared, 1);
  assert.equal(listeners.focus, undefined);
});

test('fork posts the listing coordinates and the forks list is fresh before it resolves', async () => {
  let forks = {};
  const { host, calls } = makeHost({
    'POST /api/forks': (init) => { forks = { f9: { title: 'Copy' } }; return { body: { id: 'f9', fork: { title: 'Copy' } } }; },
    'GET /api/forks': () => ({ body: { forks } }),
  });
  const seen = [];
  host.subscribeForks((v) => seen.push(Object.keys(v)));
  await flush();
  const progress = [];
  const res = await host.fork({ uid: 'pub', exp: 'exp-1' }, (m) => progress.push(m));
  assert.deepEqual(res, { forkId: 'f9', record: { title: 'Copy' } });
  assert.deepEqual(JSON.parse(calls.find((c) => c.method === 'POST').body), { publisher: 'pub', exp: 'exp-1' });
  assert.deepEqual(seen.at(-1), ['f9']);
  assert.deepEqual(progress, ['Copying files']);
});

test('fork docs: a snapshot, a missing snapshot (stale), and a copy that was just removed', async () => {
  const { host } = makeHost({
    'GET /api/forks/has/docs': { body: { docs: { methods: 'm' }, readme: null, stale: false } },
    'GET /api/forks/none/docs': { body: { docs: null, readme: 'r', stale: true } },
    'GET /api/forks/gone/docs': { status: 404, body: { error: { kind: 'NotFound', message: 'No such copy.' } } },
  });
  const got = []; const errors = [];
  host.subscribeForkDocs({ id: 'has' }, (v) => got.push(v), (e) => errors.push(e));
  host.subscribeForkDocs({ id: 'none' }, (v) => got.push(v), (e) => errors.push(e));
  host.subscribeForkDocs({ id: 'gone' }, (v) => got.push(v), (e) => errors.push(e));
  await flush();
  assert.deepEqual(got, [{ docs: { methods: 'm' } }, { stale: true }]);
  assert.deepEqual(errors, []);
});

test('a missing bundle reads as null (the app says the source is unavailable); other errors throw', async () => {
  const { host } = makeHost({
    'GET /api/bundles/L~p~e~b1/meta': { status: 404, body: { error: { kind: 'NotFound', message: 'x' } } },
    'GET /api/bundles/L~p~e~b2/meta': { status: 500, body: { error: { kind: 'Internal', message: 'boom' } } },
    'GET /api/bundles/L~p~e~b3/chunks/0': { body: [['README.md', 'hi', false]] },
  });
  assert.equal(await host.bundleMeta('L~p~e~b1'), null);
  await assert.rejects(host.bundleMeta('L~p~e~b2'), /boom/);
  assert.deepEqual(await host.bundleChunk('L~p~e~b3', 0), [['README.md', 'hi', false]]);
});

test('an upload posts the zip bytes with its message in the query', async () => {
  const { host, calls } = makeHost({ 'POST /api/forks/f%201/revisions': { body: { revision: { bundle: 'b' }, fork: {} } } });
  const file = { name: 'copy.zip' };
  const rev = await host.uploadRevision({ id: 'f 1' }, file, 'half speed & more');
  assert.deepEqual(rev, { bundle: 'b' });
  const c = calls[0];
  assert.equal(new URL(c.url).searchParams.get('message'), 'half speed & more');
  assert.equal(c.headers['Content-Type'], 'application/zip');
  assert.equal(c.body, file);
});

test('publish sends only the listing form fields; the publisher name is the server\'s business', async () => {
  const { host, calls } = makeHost({ 'POST /api/forks/f1/publish': { body: { listing: {} } } });
  await host.publish({ id: 'f1' }, { name: 'Spoofed', title: 'T', summary: 'S', category: 'memory', subjects: ['human'], hardware: {}, version: '1.0.0', license: 'MIT' });
  assert.deepEqual(Object.keys(JSON.parse(calls[0].body)).sort(), ['category', 'hardware', 'license', 'subjects', 'summary', 'title', 'version']);
});

test('the hero drawing loses only its root style attribute', async () => {
  const hero = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 4 4" style="width:100%"><g style="opacity:1"/></svg>';
  const { host } = makeHost({ 'GET /api/assets': { body: { hero, penrose: '<svg/>' } } });
  const got = [];
  host.subscribeAssets((v) => got.push(v));
  await flush();
  assert.equal(got[0].hero, '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 4 4"><g style="opacity:1"/></svg>');
  assert.equal(got[0].penrose, '<svg/>');
});

test('a server sign-in carries the page header the server requires, before any CSRF token exists', async () => {
  const { api, calls } = loadHost({ responses: { 'POST /api/login': { body: { kind: 'server', viewer: { uid: 'u1', name: 'A' }, csrf: 'c' } } } });
  const t = api.createTransport({ kind: 'server', token: '', getCsrf: () => '', onSignedOut: () => {} });
  await t.request('POST', 'api/login', { json: { username: 'a', password: 'p' } });
  assert.equal(calls[0].headers['X-Alhazen-Platform'], '1');
  assert.equal(calls[0].headers['X-Alhazen-CSRF'], undefined);
  assert.equal(calls[0].headers['X-Alhazen-Token'], undefined);
});

test('a visitor of a public catalogue reads only: no copies are fetched and no changes are offered', async () => {
  const session = { kind: 'server', viewer: null, csrf: null, publicCatalogue: true, capabilities: { signIn: true, fork: true, publish: true, uploadRevision: true, downloadRevision: true } };
  const { host, calls } = makeHost({ 'GET /api/catalogue': { body: { published: { lab: { dots: { id: 'dots' } } } } } }, session);
  assert.equal(host.viewer, null);
  for (const k of ['fork', 'publish', 'uploadRevision', 'downloadRevision']) assert.equal(host.capabilities[k], false, k);
  let forks;
  host.subscribeForks((v) => { forks = v; }, () => {});
  assert.deepEqual(forks, {});
  let listings;
  const off = host.subscribeListings((v) => { listings = v; }, (e) => { throw e; });
  await flush(); await flush();
  assert.deepEqual(Object.keys(listings), ['lab']);
  off();
  assert.equal(calls.filter((c) => new URL(c.url).pathname === '/api/forks').length, 0);
});

test('a signed-in server user keeps every capability the server grants', () => {
  const session = { kind: 'server', viewer: { uid: 'u1', name: 'Ada' }, csrf: 'c', capabilities: { signIn: true, fork: true, publish: true } };
  const { host } = makeHost({}, session);
  assert.equal(host.capabilities.fork, true);
  assert.equal(host.capabilities.publish, true);
});

test('rig configs: the owner routes of machine-rig-configs.md §4, each change refreshing the views', async () => {
  let record = { id: 'mc_0123456789abcdef', defaultRev: 1, configs: { c1a2b3c4d5e: { id: 'c1a2b3c4d5e', name: 'booth', revision: 1, state: 'synced' } } };
  const { host, calls } = makeHost({
    'GET /api/rigs': () => ({ body: { machines: [{ id: record.id, configs: {} }] } }),
    'GET /api/rigs/mc_0123456789abcdef': () => ({ body: { record } }),
    'GET /api/rigs/mc_gone00000000': { status: 404, body: { error: { kind: 'NotFound', message: 'No such machine' } } },
    'PUT /api/rigs/mc_0123456789abcdef/configs/c1a2b3c4d5e': (init) => { record = { ...record, configs: { c1a2b3c4d5e: { ...record.configs.c1a2b3c4d5e, revision: 2, state: 'pending' } } }; return { body: { record, config: record.configs.c1a2b3c4d5e } }; },
    'DELETE /api/rigs/mc_0123456789abcdef/configs/c1a2b3c4d5e': { body: { record } },
    'PUT /api/rigs/mc_0123456789abcdef/default': { body: { record } },
    'POST /api/rigs/mc_0123456789abcdef/configs/c1a2b3c4d5e/resolve': { body: { record } },
    'DELETE /api/rigs/mc_0123456789abcdef': { body: {} },
  }, { kind: 'server', viewer: { uid: 'u1', name: 'Ann' }, capabilities: { signIn: true, machineTokens: true } });
  assert.equal(host.capabilities.rigConfigs, true);
  const seen = [];
  host.subscribeRigMachine('mc_0123456789abcdef', (r) => seen.push(r.configs.c1a2b3c4d5e.state));
  const missing = [];
  host.subscribeRigMachine('mc_gone00000000', (r) => missing.push(r));
  const lists = [];
  host.subscribeRigMachines((v) => lists.push(v.machines.length));
  await flush();
  assert.deepEqual(missing, [null]);
  assert.deepEqual(lists, [1]);
  assert.deepEqual(await host.listRigMachines(), [{ id: 'mc_0123456789abcdef', configs: {} }]);
  assert.equal((await host.getRigMachine('mc_0123456789abcdef')).id, 'mc_0123456789abcdef');
  const res = await host.putRigConfig('mc_0123456789abcdef', 'c1a2b3c4d5e', { name: 'booth', text: 'a: 1\n', base: 1, basedOn: 'abc' });
  assert.equal(res.config.revision, 2);
  assert.deepEqual(seen, ['synced', 'pending']); // refreshed before putRigConfig resolved
  const put = calls.find((c) => c.method === 'PUT' && c.url.endsWith('/configs/c1a2b3c4d5e'));
  assert.deepEqual(JSON.parse(put.body), { name: 'booth', text: 'a: 1\n', base: 1, basedOn: 'abc' });
  assert.equal(put.headers['X-Alhazen-Platform'], '1');
  await host.deleteRigConfig('mc_0123456789abcdef', 'c1a2b3c4d5e', 2);
  assert.ok(calls.some((c) => c.method === 'DELETE' && c.url.endsWith('/configs/c1a2b3c4d5e?base=2')));
  await host.setRigDefault('mc_0123456789abcdef', 'c1a2b3c4d5e', 1);
  assert.deepEqual(JSON.parse(calls.filter((c) => c.method === 'PUT').at(-1).body), { config: 'c1a2b3c4d5e', base: 1 });
  await host.resolveRigConfig('mc_0123456789abcdef', 'c1a2b3c4d5e', 'machine');
  assert.deepEqual(JSON.parse(calls.filter((c) => c.method === 'POST').at(-1).body), { keep: 'machine' });
  await host.forgetRigMachine('mc_0123456789abcdef');
  assert.ok(calls.some((c) => c.method === 'DELETE' && new URL(c.url).pathname === '/api/rigs/mc_0123456789abcdef'));
  assert.equal(typeof (await host.loadYaml()).parseDocument, 'function');
});

test('rig configs: a 409 from the server reaches the editor with its message', async () => {
  const { host } = makeHost({ 'PUT /api/rigs/mc_0123456789abcdef/configs/c1': { status: 409, body: { error: { kind: 'Conflict', message: 'This rig config changed since you opened it; reopen it and make the change again' } } } });
  await assert.rejects(host.putRigConfig('mc_0123456789abcdef', 'c1', { name: 'b', text: 'a: 1', base: 0 }), (e) => e.status === 409 && /changed since you opened it/.test(e.message));
});

test('rig configs are not offered to a visitor of a public catalogue', () => {
  const { host } = makeHost({}, { kind: 'server', viewer: null, capabilities: { signIn: true } });
  assert.equal(host.capabilities.rigConfigs, false);
});
