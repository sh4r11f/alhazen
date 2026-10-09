/* The hub page's pure part (src/alhazen/hub/assets/hub_core.js): screen
 * addresses, the API client's credentials and error kinds, input checks and
 * the words the views show. No DOM and no network: fetch is a fake. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const ASSETS = new URL('../../src/alhazen/hub/assets/', import.meta.url);

function load() {
  /* URL comes from Node, so its TypeError must be the one the script sees:
   * one realm, as in a browser. */
  const context = vm.createContext({URL, URLSearchParams, AbortController, setTimeout, clearTimeout, TypeError});
  vm.runInContext(readFileSync(new URL('hub_core.js', ASSETS), 'utf8') + '\nthis.C = HubCore;', context);
  return context.C;
}
const C = load();

/* A fetch that records each call and answers from `answer(url, init)`. */
function fakeFetch(answer) {
  const calls = [];
  const fetch = async (url, init) => {
    calls.push({url, init});
    const reply = await answer(url, init);
    if (reply instanceof Error) throw reply;
    const headers = new Map(Object.entries(reply.headers || {}));
    return {
      ok: reply.status >= 200 && reply.status < 300,
      status: reply.status,
      headers: {get: (name) => headers.get(name) || null},
      text: async () => (reply.raw !== undefined ? reply.raw : reply.body === undefined ? '' : JSON.stringify(reply.body)),
    };
  };
  return {fetch, calls};
}

test('a route survives formatting and parsing, with parameters in a fixed order', () => {
  const routes = [
    {view: 'home'},
    {view: 'catalog', q: 'saccade averaging', offset: 40},
    {view: 'experiment', id: 'e1', version: 'v-2', tab: 'tasks', task: 'fixation-demo'},
    {view: 'data', experiment: 'e1', subject: 'S01', mode: 'run', offset: 20, session: 's9', toffset: 50},
    {view: 'rig', tab: 'upload', project: 'p1', root: 'r1', run: 'v1/sub-01/ses-1/run-1', job: 'j1'},
    {view: 'guide', section: 'modes'},
  ];
  for (const route of routes) {
    assert.deepEqual({...C.parseRoute(C.formatRoute(route).slice(1))}, route);
  }
  assert.equal(C.formatRoute({view: 'home'}), '');
  assert.equal(C.formatRoute({offset: 3, q: 'x', view: 'catalog'}), C.formatRoute({view: 'catalog', q: 'x', offset: 3}));
});

test('unknown views, malformed ids and parameters of another view are dropped', () => {
  assert.deepEqual({...C.parseRoute('view=admin')}, {view: 'home'});
  assert.deepEqual({...C.parseRoute('view=experiment&id=../../etc')}, {view: 'experiment'});
  assert.deepEqual({...C.parseRoute('view=experiment&id=a%2Fb')}, {view: 'experiment'});
  assert.deepEqual({...C.parseRoute('view=catalog&offset=-5')}, {view: 'catalog'});
  assert.deepEqual({...C.parseRoute('view=catalog&offset=0')}, {view: 'catalog'});
  // A rig tab is not an experiment tab and the reverse.
  assert.deepEqual({...C.parseRoute('view=experiment&id=e&tab=upload')}, {view: 'experiment', id: 'e'});
  assert.deepEqual({...C.parseRoute('view=rig&tab=methods')}, {view: 'rig'});
  assert.deepEqual({...C.parseRoute('view=rig&run=a/../b')}, {view: 'rig'});
  assert.deepEqual({...C.parseRoute('view=library&id=e1')}, {view: 'library'});
});

test('a sign-in "next" address can only be one of this page\'s own screens', () => {
  assert.equal(C.safeNext('?view=library'), '?view=library');
  assert.equal(C.safeNext('https://evil.example/?view=library'), '');
  assert.equal(C.safeNext('//evil.example'), '');
  assert.equal(C.safeNext('?view=signin'), '');
  assert.equal(C.safeNext('?view=nonsense'), '?view=home');
  assert.deepEqual({...C.parseRoute('view=signin&next=' + encodeURIComponent('https://x.example'))}, {view: 'signin'});
});

test('only same-origin paths from the server become links', () => {
  assert.equal(C.sameOriginPath('/?project=p1'), '/?project=p1');
  for (const bad of ['//evil.example/x', 'https://evil.example', 'javascript:alert(1)', '/\\evil', '/a b', '', null]) {
    assert.equal(C.sameOriginPath(bad), null, String(bad));
  }
});

test('every error body shape becomes a HubError with a kind and the server words', () => {
  let e = C.errorFromResponse(404, {error: {code: 'not_found', message: 'No such experiment'}}, null);
  assert.equal(e.kind, 'not_found');
  assert.equal(e.message, 'No such experiment');
  e = C.errorFromResponse(403, {error: 'Cross-origin requests are not allowed'}, null);
  assert.equal(e.kind, 'forbidden');
  assert.equal(e.message, 'Cross-origin requests are not allowed');
  e = C.errorFromResponse(422, {detail: [{msg: 'field required'}, {msg: 'too short'}]}, null);
  assert.equal(e.kind, 'invalid');
  assert.equal(e.message, 'field required; too short');
  e = C.errorFromResponse(429, null, '30');
  assert.equal(e.kind, 'rate_limited');
  assert.equal(e.retryAfter, 30);
  assert.match(e.message, /30 s/);
  e = C.errorFromResponse(500, null, null);
  assert.equal(e.kind, 'server');
  assert.ok(e.message.length > 0);
});

test('the rig adapter\'s error codes map to the kinds the page branches on', () => {
  const kinds = {
    not_connected: [409, 'not_configured'], unauthenticated: [401, 'unauthorized'],
    hub_unreachable: [502, 'unavailable'], hub_redirect: [502, 'unavailable'], run_active: [409, 'run_active'],
    preview_stale: [409, 'preview_stale'], auth_context_changed: [409, 'auth_context_changed'], conflict: [409, 'conflict'],
  };
  for (const [code, [status, kind]] of Object.entries(kinds)) {
    assert.equal(C.errorFromResponse(status, {error: {code, message: 'm'}}, null).kind, kind, code);
  }
});

test('a rig client sends the workspace token on every request and never CSRF', async () => {
  const {fetch, calls} = fakeFetch(() => ({status: 200, body: {ok: true}}));
  const api = C.createApi({fetch, role: 'rig', token: () => 'T'.repeat(20), csrf: () => 'never'});
  await api.request('GET', '/local/status');
  await api.request('POST', '/auth/login', {json: {username: 'a', password: 'b'}});
  for (const call of calls) {
    assert.equal(call.init.headers['X-Alhazen-Token'], 'T'.repeat(20));
    assert.equal(call.init.headers['X-CSRF-Token'], undefined);
    assert.equal(call.init.headers.Authorization, undefined);
    assert.equal(call.init.redirect, 'error');
    assert.equal(call.init.credentials, 'same-origin');
  }
  assert.equal(calls[0].url, '/api/hub/v1/local/status');
  assert.equal(calls[1].init.headers['Content-Type'], 'application/json');
  assert.equal(calls[1].init.body, JSON.stringify({username: 'a', password: 'b'}));
});

test('a central client sends CSRF on writes only, and no token', async () => {
  const {fetch, calls} = fakeFetch(() => ({status: 200, body: {}}));
  const api = C.createApi({fetch, role: 'server', token: () => 'T'.repeat(20), csrf: () => 'c5rf'});
  await api.request('GET', '/library');
  await api.request('POST', '/library', {json: {experiment_id: 'e', version_id: 'v'}});
  await api.request('PATCH', '/experiments/e', {json: {}});
  assert.equal(calls[0].init.headers['X-CSRF-Token'], undefined);
  assert.equal(calls[1].init.headers['X-CSRF-Token'], 'c5rf');
  assert.equal(calls[2].init.headers['X-CSRF-Token'], 'c5rf');
  for (const call of calls) assert.equal(call.init.headers['X-Alhazen-Token'], undefined);
});

test('query values are encoded and empty ones left out; paths are fixed strings', async () => {
  const {fetch, calls} = fakeFetch(() => ({status: 200, body: {items: []}}));
  const api = C.createApi({fetch, role: 'server'});
  await api.request('GET', '/catalog', {query: {query: 'a&b=c', limit: 20, offset: 0, empty: '', none: undefined}});
  assert.equal(calls[0].url, '/api/hub/v1/catalog?query=a%26b%3Dc&limit=20&offset=0');
  await assert.rejects(() => api.request('GET', 'https://evil.example/x'), /fixed strings/);
  await assert.rejects(() => api.request('GET', '/a/../b'), /fixed strings/);
});

test('download links carry the rig token in the query, central ones do not', () => {
  const rig = C.createApi({fetch: null, role: 'rig', token: () => 'abcdefghijklmnopqrstu'});
  const central = C.createApi({fetch: null, role: 'server', token: () => 'abcdefghijklmnopqrstu'});
  assert.equal(rig.url('/data/sessions/s1/export', {format: 'csv'}),
    '/api/hub/v1/data/sessions/s1/export?format=csv&token=abcdefghijklmnopqrstu');
  assert.equal(central.url('/data/sessions/s1/export', {format: 'csv'}), '/api/hub/v1/data/sessions/s1/export?format=csv');
});

test('a network failure is "offline", a slow hub "timeout", a non-JSON success "bad_response"', async () => {
  let api = C.createApi({fetch: fakeFetch(() => new TypeError('Failed to fetch')).fetch, role: 'server'});
  await assert.rejects(() => api.request('GET', '/catalog'), (e) => e.kind === 'offline');

  let fire = null;
  const never = (url, init) => new Promise((resolve, reject) => {
    init.signal.addEventListener('abort', () => reject(new Error('aborted')));
  });
  api = C.createApi({fetch: never, role: 'server', setTimeout: (fn) => { fire = fn; return 1; }, clearTimeout: () => {}});
  const pending = api.request('GET', '/catalog');
  fire();
  await assert.rejects(() => pending, (e) => e.kind === 'timeout');

  const controller = new AbortController();
  api = C.createApi({fetch: never, role: 'server', setTimeout: () => 1, clearTimeout: () => {}});
  const cancelled = api.request('GET', '/catalog', {signal: controller.signal});
  controller.abort();
  await assert.rejects(() => cancelled, (e) => e.kind === 'aborted');

  api = C.createApi({fetch: fakeFetch(() => ({status: 200, raw: '<html>'})).fetch, role: 'server'});
  await assert.rejects(() => api.request('GET', '/catalog'), (e) => e.kind === 'bad_response');
});

test('an error keeps its parsed body (an unauthenticated /auth/me may carry a CSRF token)', async () => {
  const api = C.createApi({fetch: fakeFetch(() => ({status: 401, body: {error: {code: 'unauthenticated', message: 'x'}, csrf_token: 'pre'}})).fetch, role: 'server'});
  await assert.rejects(() => api.request('GET', '/auth/me'), (e) => e.kind === 'unauthorized' && e.body.csrf_token === 'pre');
});

test('hub addresses: HTTPS, or HTTP only to this computer; no userinfo, query, fragment or escapes', () => {
  assert.deepEqual({...C.validateHubUrl(' https://hub.example.org/ ')}, {ok: true, url: 'https://hub.example.org', loopbackHttp: false});
  assert.deepEqual({...C.validateHubUrl('https://hub.example.org/base/')}, {ok: true, url: 'https://hub.example.org/base', loopbackHttp: false});
  assert.equal(C.validateHubUrl('http://127.0.0.1:8765').loopbackHttp, true);
  assert.equal(C.validateHubUrl('http://localhost:8765').ok, true);
  for (const bad of ['', 'hub.example.org', 'http://hub.example.org', 'ftp://hub.example.org',
    'https://user:pw@hub.example.org', 'https://hub.example.org/?a=1', 'https://hub.example.org/#x',
    'https://hub.example.org/%2e%2e', 'javascript:alert(1)']) {
    assert.equal(C.validateHubUrl(bad).ok, false, bad);
  }
});

test('passwords: 12 to 1024 characters, and the repeat must match', () => {
  assert.equal(C.validatePassword('short').ok, false);
  assert.equal(C.validatePassword('x'.repeat(12)).ok, true);
  assert.equal(C.validatePassword('x'.repeat(1025)).ok, false);
  assert.equal(C.validatePassword('x'.repeat(12), 'y'.repeat(12)).ok, false);
});

test('interpreter and package manifest checks name the field at fault', () => {
  assert.equal(C.validatePython('  ').ok, false);
  assert.equal(C.validatePython('/opt/venv/bin/python\n--evil').ok, false);
  assert.equal(C.validatePython(' /opt/venv/bin/python ').value, '/opt/venv/bin/python');
  const good = {name: 'fixation-demo', version: '1.0.0', title: 'Fixation', license: 'MIT'};
  assert.equal(C.validatePackageMetadata(good).ok, true);
  const bad = C.validatePackageMetadata({name: 'Fixation Demo', version: '1.0', title: '', license: ''});
  assert.deepEqual(Object.keys(bad.errors).sort(), ['license', 'name', 'title', 'version']);
  assert.equal(C.validatePackageMetadata({...good, version: '01.0.0'}).ok, false);
});

test('lists and tags are parsed as typed', () => {
  assert.deepEqual([...C.parseList('a\n\n b \r\nc')], ['a', 'b', 'c']);
  assert.deepEqual([...C.parseTags('Vision, saccades, vision ,, EYE')], ['vision', 'saccades', 'eye']);
});

test('numbers and dates as shown', () => {
  assert.equal(C.formatBytes(0), '0 B');
  assert.equal(C.formatBytes(1536), '1.5 KiB');
  assert.equal(C.formatBytes(256 * 1024 * 1024), '256 MiB');
  assert.equal(C.formatBytes(-1), '—');
  assert.equal(C.formatBytes('x'), '—');
  assert.equal(C.formatDate(''), '');
  assert.equal(C.formatDate('not a date'), 'not a date');
  assert.match(C.formatDate('2026-10-09T12:00:00Z', false), /^\d{1,2} Oct 2026$/);
  assert.equal(C.shortHash('a'.repeat(64)), 'a'.repeat(12));
});

test('hardware rows say needed, not needed or not declared; platforms in words', () => {
  const rows = C.hardwareList({hardware: {display: true, eye_tracker: false}});
  assert.deepEqual(JSON.parse(JSON.stringify(rows.map((r) => [r.key, r.required]))), [['display', true], ['eye_tracker', false], ['reward', null]]);
  assert.equal(C.platformsText({platforms: ['linux', 'darwin', 'win32']}), 'Linux, macOS, Windows');
  assert.equal(C.platformsText({}), 'Not declared');
});

test('trial columns keep first-seen order and a cap; cells are text', () => {
  assert.deepEqual([...C.trialColumns([{a: 1, b: 2}, {c: 3, a: 4}])], ['a', 'b', 'c']);
  const wide = Object.fromEntries(Array.from({length: 60}, (_, i) => ['k' + i, i]));
  assert.equal(C.trialColumns([wide]).length, 40);
  assert.equal(C.cellText(null), '');
  assert.equal(C.cellText({x: 1}), '{"x":1}');
  assert.equal(C.cellText('y'.repeat(300)).length, 200);
});

test('job states: words, terminal states, resume only when it can resume', () => {
  let s = C.jobState({status: 'uploading', bytes_done: 50, bytes_total: 200});
  assert.equal(s.fraction, 0.25);
  assert.equal(s.running, true);
  assert.equal(s.terminal, false);
  s = C.jobState({status: 'completed', bytes_done: 200, bytes_total: 200});
  assert.equal(s.ok, true);
  assert.equal(s.terminal, true);
  assert.equal(s.canCancel, false);
  s = C.jobState({status: 'paused', error: {code: 'auth_context_changed', message: 'other account', retryable: false}});
  assert.equal(s.canResume, false, 'resume cannot help while another account is signed in');
  assert.equal(s.error, 'other account');
  assert.equal(C.jobState({status: 'paused', error: {code: 'interrupted', retryable: true}}).canResume, true);
  assert.equal(C.jobState({status: 'paused'}).canResume, true);
  assert.equal(C.jobState({status: 'paused', error: {code: 'local_changed', retryable: false}}).needsPreview, true);
  s = C.jobState({status: 'failed', error: {code: 'x', message: 'disk', retryable: false}});
  assert.equal(s.canResume, false);
  assert.equal(C.jobState({status: 'failed', error: {retryable: true}}).canResume, true);
  assert.equal(C.jobState({status: 'waiting'}).word, 'Waiting for the running session to end');
  assert.equal(C.jobState({status: 'queued', bytes_total: 0}).fraction, null);
});

test('only https links are offered from citations', () => {
  assert.equal(C.firstHttpsUrl('Doe 2020, https://doi.org/10.1/x.'), 'https://doi.org/10.1/x');
  assert.equal(C.firstHttpsUrl('javascript:alert(1) http://x.example'), '');
  assert.equal(C.firstHttpsUrl('no link'), '');
});

test('the rig token comes from the fragment first, then the tab', () => {
  const t = 'A'.repeat(43);
  assert.deepEqual({...C.readToken('#token=' + t, 'B'.repeat(43))}, {token: t, fromFragment: true});
  assert.deepEqual({...C.readToken('', 'B'.repeat(43))}, {token: 'B'.repeat(43), fromFragment: false});
  assert.deepEqual({...C.readToken('#token=<script>', '')}, {token: '', fromFragment: false});
});

test('a body that breaks off mid-answer is a failure, never an empty success', async () => {
  const fetch = async () => ({ok: true, status: 200, headers: {get: () => null}, text: async () => { throw new TypeError('network error'); }});
  const api = C.createApi({fetch, role: 'server'});
  await assert.rejects(() => api.request('POST', '/library', {json: {}}), (e) => e.kind === 'offline');
});

test('trial index state: nested server shape, older flat fields, and what each allows', () => {
  let s = C.indexState({index: {status: 'failed', rows: 0, error: 'budget'}});
  assert.equal(s.failed, true);
  assert.equal(s.canRebuild, true);
  assert.equal(s.ready, false);
  assert.equal(s.error, 'budget');
  s = C.indexState({index: {status: 'indexed', rows: 12, error: null}});
  assert.equal(s.ready, true);
  assert.equal(s.rows, 12);
  assert.equal(s.canRebuild, false);
  assert.equal(C.indexState({index: {status: 'pending', rows: 0, error: null}}).busy, true);
  assert.equal(C.indexState({index: {status: 'indexing'}}).busy, true);
  assert.equal(C.indexState({index: {status: 'none', rows: 0}}).word, 'No trial table in this session');
  s = C.indexState({index_status: 'failed', index_error: 'flat', index_rows: 0});
  assert.equal(s.failed, true);
  assert.equal(s.error, 'flat');
  s = C.indexState({});
  assert.equal(s.status, '');
  assert.equal(s.ready, false);
  assert.equal(s.canRebuild, false);
  assert.equal(s.rows, null);
});

test('a trial row is read from the server\'s {ordinal, source_path, values}; flat rows still work', () => {
  const row = C.trialRow({ordinal: 3, source_path: 'trials.csv', values: {trial_index: '4', outcome: 'FIXATED'}});
  assert.equal(row.ordinal, 3);
  assert.equal(row.source, 'trials.csv');
  assert.equal(row.values.outcome, 'FIXATED');
  const flat = C.trialRow({trial: 1, rt: 0.3});
  assert.equal(flat.ordinal, null);
  assert.equal(flat.values.rt, 0.3);
  assert.equal(C.trialRow(null).ordinal, null);
});
