/* Create with AI wired to the AI authoring API (ai-authoring CONTRACT.md),
 * against the contract stub in ai_stub.mjs: the status gate, Provider &
 * key, Generate and the polled plan job, the plan, Generate source and its
 * validation report, acceptance into a private version, every error code
 * with its next step, drafts under My experiments, the AI-assisted badge
 * and Library Remove. */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import vm from 'node:vm';

import { mount, CORE } from './hub_harness.mjs';
import { aiHub, PLAN, REPORT_OK } from './ai_stub.mjs';

const SERVER_CONFIG = {status: 200, body: {role: 'server', api_version: 1, registration_mode: 'invite', limits: {}}};
const ALICE = {id: 'u1', username: 'alice', display_name: 'Alice A'};
const KEY = 'sk-' + 'q'.repeat(28) + 'WXYZ';
const PROMPT = 'Saccade adaptation in monkeys with an intrasaccadic back-step of two degrees.';

function item(id, over) {
  return Object.assign({
    experiment: {id, title: 'Exp ' + id, summary: '', owner: {id: 'u2', username: 'bob', display_name: 'Bob B'}, published_version_id: 'pv-' + id, tags: [], created_at: '2026-10-01T00:00:00Z'},
    version: {id: 'pv-' + id, experiment_id: id, version: '1.2.0', sha256: 'ab'.repeat(32), size: 10, created_at: '2026-10-02T00:00:00Z', manifest: {hardware: {}}},
  }, over || {});
}

function base(extra) {
  return Object.assign({
    'GET /config': () => SERVER_CONFIG,
    'GET /auth/me': () => ({status: 200, body: {user: ALICE, csrf_token: 'c1'}}),
    'GET /catalog': () => ({status: 200, body: {items: [item('b'), item('c')], next_offset: null}}),
  }, extra || {});
}

async function open(search, options, extra) {
  const ai = aiHub(base(extra), options);
  const p = await mount({routes: ai.routes, search: search || '?view=create'});
  return {p, ai};
}

const keyInput = (p) => p.main.querySelectorAll('input').find((i) => i.getAttribute('type') === 'password');
const posts = (p, key) => p.hub.calls.filter((c) => c.key === key);
const noKeyAnywhere = (p) => {
  for (const store of [p.session, p.local]) for (const v of store.map.values()) assert.ok(!String(v).includes('sk-'), 'a key in browser storage');
};

async function describe(p) {
  const text = p.main.querySelector('textarea');
  text.value = PROMPT;
  text.fire('input', {});
}

/* ---- core helpers ---------------------------------------------------- */

function core() {
  const context = vm.createContext({URL, URLSearchParams, AbortController, setTimeout, clearTimeout, TypeError});
  vm.runInContext(CORE + '\nthis.HubCore = HubCore;', context);
  return context.HubCore;
}

test('core: AI error codes never read as the hub being offline', () => {
  const C = core();
  for (const [status, code] of [[502, 'provider_error'], [504, 'provider_timeout'], [402, 'provider_quota'], [422, 'generation_invalid'], [409, 'key_required'], [403, 'ai_disabled']]) {
    const e = C.errorFromResponse(status, {error: {code, message: 'm'}}, null);
    assert.equal(e.kind, 'ai', code);
    assert.equal(C.aiErrorCode(e), code);
  }
  assert.equal(C.errorFromResponse(402, null, null).kind, 'ai');
  assert.equal(C.aiErrorCode(C.errorFromResponse(429, {error: {code: 'rate_limited', message: 'x'}}, '30')), 'rate_limited');
  assert.equal(C.aiErrorCode(C.errorFromResponse(409, {error: {code: 'conflict', message: 'x'}}, null)), 'conflict');
  assert.equal(C.errorFromResponse(504, null, null).kind, 'unavailable', 'a gateway timeout without an AI code is still the hub');
  for (const code of ['ai_disabled', 'key_required', 'provider_quota', 'generation_invalid', 'rate_limited', 'provider_error', 'provider_timeout', 'cancelled', 'conflict']) {
    const a = C.aiAdvice(code, 'OpenAI');
    assert.ok(a.title, code);
  }
  assert.equal(C.aiAdvice('key_required', 'OpenAI').action, 'key');
  assert.equal(C.aiAdvice('provider_quota', 'OpenAI').action, 'key');
  assert.equal(C.aiAdvice('generation_invalid', 'OpenAI').action, 'retry');
});

test('core: a contract Plan becomes the plan page, nothing invented', () => {
  const C = core();
  const v = C.aiPlanView(PLAN);
  assert.equal(v.title, PLAN.title);
  assert.equal(v.slug, 'saccade-adaptation-in-two-monkeys');
  assert.deepEqual([...v.design.map((r) => [...r])], [['Subject', 'Monkey'], ['Hardware', 'Display, Eye tracker, Reward line'], ['Tasks', 'adapt']]);
  assert.deepEqual([...v.parameters[0]], ['step_dva', '10', 'dva', 'Primary target step (5-15)']);
  assert.deepEqual([...v.parameters[1]], ['backstep_dva', '2', 'dva', 'Intrasaccadic step']);
  assert.deepEqual(v.timeline.map((t) => [t.label, t.ms, t.time]), [['Fixate', 500, '500 ms'], ['Saccade', null, 'Until an event'], ['ITI', null, 'Set by a parameter']]);
  assert.deepEqual([...v.stimuli.heads], ['Element', 'Size', 'Notes']);
  assert.deepEqual([...v.measures.rows[0]], ['Gain', 'ratio', 'Amplitude / target step']);
  const empty = C.aiPlanView({title: 'T'});
  assert.equal(empty.parameters.length, 0);
  assert.equal(empty.design.length, 0);
  assert.equal(empty.timeline.length, 0);
  assert.deepEqual([...C.aiPlanView({title: 'x', stimuli: ['a dot']}).stimuli.rows[0]], ['a dot']);
});

test('core: validation reports, disclosure, durations and the draft address', () => {
  const C = core();
  const ok = C.aiValidation(REPORT_OK);
  assert.equal(ok.ok, true);
  assert.equal(ok.files.length, 3);
  assert.equal(ok.bytes, 6900);
  assert.equal(ok.passed, 3);
  const bad = C.aiValidation({files: {'run.py': 10}, report: {checks: [{name: 'a', status: 'passed'}], errors: ['manifest missing']}});
  assert.equal(bad.ok, false);
  assert.equal(bad.failed, 1);
  assert.equal(bad.files[0].size, 10);
  assert.equal(C.aiValidation({report: {ok: false, checks: []}}).ok, false);
  assert.equal(C.aiValidation(null).ok, true);
  assert.match(C.aiDisclosed({files: ['a', 'b'], bytes: 2048}), /2 source files \(2(\.0)? KiB\)/);
  assert.equal(C.aiDisclosed(null), '');
  assert.equal(C.phaseDuration('1.5 s').ms, 1500);
  assert.equal(C.phaseDuration(' 250 ').ms, 250);
  assert.equal(C.aiJob({status: 'failed', error_code: 'provider_timeout'}).code, 'provider_timeout');
  assert.equal(C.aiJob({status: 'running'}).active, true);
  assert.equal(C.formatRoute({view: 'create', step: 'plan', draft: 'd1'}), '?view=create&step=plan&draft=d1');
  assert.equal(C.parseRoute('view=create&draft=../x').draft, undefined);
  assert.equal(C.formatRoute({view: 'create', fork: 'e1', version: 'v2'}), '?view=create&fork=e1&version=v2');
});

/* ---- status gate ------------------------------------------------------ */

test('AI disabled: the key step says so, Generate sends nothing, the example plan is labelled', async () => {
  const {p} = await open('?view=create', {enabled: false});
  assert.match(p.text(), /AI authoring is not enabled on this hub/);
  const form = p.main.querySelector('form');
  assert.equal(p.find('button', 'Generate plan').disabled, true);
  assert.equal(keyInput(p), undefined, 'no key field when AI is off');
  await describe(p);
  const calls = p.hub.calls.length;
  await p.submit(form);
  assert.equal(p.hub.calls.length, calls, 'no request of any kind');
  await p.click(p.find('a', 'See an example plan'));
  assert.equal(p.loc.search, '?view=create&step=plan');
  assert.match(p.text(), /Example plan/);
  assert.match(p.text(), /not generated from your description/);
  assert.equal(p.find('button', 'Create private draft version').disabled, true);
  assert.match(p.text(), /Parameters11/);
});

test('a hub without the AI routes (404) is treated as AI disabled; a failing status shows the error with Retry', async () => {
  const p = await mount({routes: base(), search: '?view=create'});
  assert.match(p.text(), /AI authoring is not enabled on this hub/);
  const {p: q} = await open('?view=create', {fail: {'GET /ai/status': {status: 500, code: 'server', message: 'boom'}}});
  assert.match(q.text(), /boom/);
  assert.ok(q.find('button', 'Try again'));
});

test('AI enabled with no draft: the plan address goes back to the form, never to the example', async () => {
  const {p} = await open('?view=create&step=plan');
  assert.equal(p.loc.search, '?view=create');
  assert.doesNotMatch(p.text(), /Example plan/);
});

/* ---- provider & key ------------------------------------------------------ */

test('Provider & key: Save sends one PUT, clears the field, shows the hint; Replace and Remove', async () => {
  const {p, ai} = await open();
  const models = p.main.querySelector('select[name="model"]');
  assert.deepEqual(models.querySelectorAll('option').map((o) => o.textContent), ['gpt-test', 'gpt-big']);
  assert.match(p.text(), /No OpenAI key saved/);
  keyInput(p).value = KEY;
  await p.click(p.find('button', 'Save key'));
  const put = posts(p, 'PUT /ai/keys/openai');
  assert.equal(put.length, 1);
  assert.deepEqual(put[0].json, {key: KEY});
  assert.match(p.text(), /OpenAI key saved, ending WXYZ/);
  assert.equal(keyInput(p), undefined, 'the field is gone once saved');
  assert.doesNotMatch(p.main.textContent, new RegExp(KEY));
  noKeyAnywhere(p);
  await p.click(p.find('button', 'Replace'));
  assert.equal(keyInput(p).value, '');
  assert.match(p.text(), /replaces the saved one/);
  await p.click(p.find('button', 'Cancel'));
  await p.click(p.find('button', 'Remove\u2026'));
  assert.match(p.text(), /Remove the saved OpenAI key/);
  await p.click(p.find('button', 'Remove key'));
  assert.equal(posts(p, 'DELETE /ai/keys/openai').length, 1);
  assert.equal(ai.state.keys.length, 0);
  assert.match(p.text(), /No OpenAI key saved/);
  /* another provider shows its own state and models */
  const provider = p.main.querySelector('select[name="provider"]');
  provider.value = 'anthropic';
  provider.fire('change', {});
  assert.match(p.text(), /No Anthropic key saved/);
  assert.deepEqual(p.main.querySelector('select[name="model"]').querySelectorAll('option').map((o) => o.textContent), ['claude-test']);
});

test('Provider & key: a refused key clears the field and says why', async () => {
  const {p} = await open();
  keyInput(p).value = 'short';
  await p.click(p.find('button', 'Save key'));
  assert.equal(keyInput(p).value, '');
  assert.match(p.text(), /does not look like an API key/);
  assert.match(p.text(), /The hub refused the request/);
});

/* ---- generate → plan → source → accept --------------------------------- */

test('the whole flow: generate, poll the plan job, plan, generate source, report, accept, success', async () => {
  const {p, ai} = await open('?view=create', {keys: [{provider: 'openai', hint: 'abcd', rotated_at: '2026-10-08T00:00:00Z'}]});
  assert.match(p.text(), /ending abcd/);
  await describe(p);
  await p.submit(p.main.querySelector('form'));
  const post = posts(p, 'POST /ai/drafts');
  assert.equal(post.length, 1);
  assert.deepEqual(post[0].json, {prompt: PROMPT, start_from: null, provider: 'openai', model: 'gpt-test'});
  assert.equal(p.loc.search, '?view=create&step=plan&draft=d1');
  /* planning: quiet progress, the description, a 2 s poll */
  assert.match(p.text(), /Writing the plan/);
  assert.match(p.text(), new RegExp(PROMPT.slice(0, 30)));
  assert.equal(p.polls(2000), 1);
  assert.ok(p.find('button', 'Cancel'));
  await p.tick(2000);  // queued -> running
  assert.equal(p.polls(2000), 1, 'still polling');
  await p.tick(2000);  // running -> done: the page reloads the draft
  assert.equal(p.polls(2000), 0);
  assert.match(p.main.querySelector('h1').textContent, /Saccade adaptation in two monkeys/);
  assert.match(p.text(), /Plan ready/);
  assert.match(p.text(), /backstep_dva/);
  assert.match(p.text(), /Until an event/);
  assert.match(p.text(), /OpenAI · gpt-test, your key/);
  assert.match(p.text(), /Description, authoring context/);
  assert.doesNotMatch(p.text(), /Example plan/);
  /* source */
  await p.click(p.find('button', 'Generate source'));
  assert.equal(posts(p, 'POST /ai/drafts/d1/generate').length, 1);
  assert.match(p.text(), /Writing the source/);
  await p.tick(2000);
  await p.tick(2000);
  assert.match(p.text(), /Generated source/);
  assert.match(p.text(), /3 files · 6\.7 KiB · 3 checks passed/);
  assert.match(p.text(), /src\/adapt\/task\.py/);
  assert.match(p.text(), /Python syntaxPassed/);
  const form = p.main.querySelectorAll('form').find((f) => f.className.includes('ix-accept'));
  const inputs = form.querySelectorAll('input');
  assert.deepEqual(inputs.map((i) => i.value), [PLAN.title, PLAN.summary, 'MIT']);
  await p.submit(form);
  assert.deepEqual(ai.state.accepted, [{title: PLAN.title, summary: PLAN.summary, license: 'MIT'}]);
  assert.match(p.text(), /Saved as a private version in My experiments/);
  assert.equal(p.find('a', 'Open in My experiments').getAttribute('href'), '/?view=mine&id=xe1');
  assert.equal(p.find('a', 'Download').getAttribute('href'), '/api/hub/v1/experiments/xe1/versions/xv1/download');
});

test('Generate checks the form first: a sentence of description, a listing to fork; nothing is sent', async () => {
  const {p} = await open('?view=create', {keys: [{provider: 'openai', hint: 'abcd'}]});
  const form = p.main.querySelector('form');
  await p.submit(form);
  assert.match(p.text(), /Describe the experiment in a sentence or two first/);
  await describe(p);
  const radios = form.querySelectorAll('input').filter((i) => i.getAttribute('type') === 'radio');
  radios[0].checked = false;
  radios[1].checked = true;
  radios[1].fire('change', {});
  await p.submit(form);
  assert.match(p.text(), /Choose the listing to fork/);
  assert.equal(posts(p, 'POST /ai/drafts').length, 0);
});

test('Generate saves a typed key first, then starts the draft; with no key at all it asks for one and sends nothing', async () => {
  const {p} = await open();
  await describe(p);
  const form = p.main.querySelector('form');
  await p.submit(form);
  assert.equal(posts(p, 'POST /ai/drafts').length, 0);
  assert.match(p.text(), /No key saved for OpenAI/);
  assert.match(p.text(), /Save one in Provider & key/);
  keyInput(p).value = KEY;
  await p.submit(form);
  assert.equal(posts(p, 'PUT /ai/keys/openai').length, 1);
  assert.equal(posts(p, 'POST /ai/drafts').length, 1);
  assert.equal(p.loc.search, '?view=create&step=plan&draft=d1');
  noKeyAnywhere(p);
});

test('Fork with AI passes start_from: the catalogue version, or the private version named in the address', async () => {
  const keys = [{provider: 'openai', hint: 'abcd'}];
  let {p} = await open('?view=create&fork=b', {keys});
  assert.equal(p.main.querySelector('select[name="from"]').value, 'b');
  await describe(p);
  await p.submit(p.main.querySelector('form'));
  assert.deepEqual(posts(p, 'POST /ai/drafts')[0].json.start_from, {experiment_id: 'b', version_id: 'pv-b'});
  ({p} = await open('?view=create&fork=mine1&version=v7', {keys}));
  await describe(p);
  await p.submit(p.main.querySelector('form'));
  assert.deepEqual(posts(p, 'POST /ai/drafts')[0].json.start_from, {experiment_id: 'mine1', version_id: 'v7'});
});

test('the experiment page passes a private version to Fork with AI, not the public one', async () => {
  const experiment = {id: 'e1', title: 'Own', owner: ALICE, published_version_id: 'v1', created_at: '2026-10-01T00:00:00Z'};
  const v1 = {id: 'v1', version: '1.0.0', sha256: 'ab'.repeat(32), size: 1, created_at: '2026-10-01T00:00:00Z', manifest: {}};
  const v2 = {id: 'v2', version: '1.1.0', sha256: 'cd'.repeat(32), size: 1, created_at: '2026-10-05T00:00:00Z', manifest: {ai_assisted: true}};
  const routes = {
    'GET /experiments/e1': () => ({status: 200, body: {experiment, versions: [v1, v2]}}),
    'GET /experiments/e1/versions/v2/documentation': () => ({status: 200, body: {documentation: null}}),
    'GET /experiments/e1/versions/v1/documentation': () => ({status: 200, body: {documentation: null}}),
  };
  const {p} = await open('?view=experiment&id=e1&version=v2', {}, routes);
  assert.equal(p.find('a', 'Fork with AI').getAttribute('href'), '/?view=create&fork=e1&version=v2');
  assert.ok(p.find('span', 'AI-assisted draft'), 'the AI-assisted badge on the release');
  const {p: q} = await open('?view=experiment&id=e1&version=v1', {}, routes);
  assert.equal(q.find('a', 'Fork with AI').getAttribute('href'), '/?view=create&fork=e1');
  assert.equal(q.main.querySelectorAll('span').filter((s) => s.className === 'chip chip-ai').length, 0, 'no badge on a hand-made version');
});

/* ---- errors, each with its next step ---------------------------------- */

test('POST /ai/drafts refusals: quota, rate limit, timeout, provider error, disabled; never the offline banner', async () => {
  const keys = [{provider: 'openai', hint: 'abcd'}];
  const cases = [
    [{status: 402, code: 'provider_quota'}, /OpenAI says this key is out of credit/, /Add credit with OpenAI/],
    [{status: 429, code: 'rate_limited', message: 'At most 3 running jobs.'}, /Too many AI jobs/, /At most 3 running jobs/],
    [{status: 504, code: 'provider_timeout'}, /OpenAI did not answer in time/, /Try again in a moment/],
    [{status: 502, code: 'provider_error'}, /OpenAI returned an error/, /Try again in a moment/],
    [{status: 403, code: 'ai_disabled'}, /AI authoring is not enabled on this hub/, /./],
    [{status: 409, code: 'key_required'}, /No key saved for OpenAI/, /Save one in Provider & key/],
  ];
  for (const [fail, title, next] of cases) {
    const {p} = await open('?view=create', {keys, fail: {'POST /ai/drafts': fail}});
    await describe(p);
    await p.submit(p.main.querySelector('form'));
    assert.match(p.text(), title, fail.code);
    assert.match(p.text(), next, fail.code);
    assert.equal(p.loc.search, '?view=create', 'stays on the form');
    assert.equal(p.find('button', 'Generate plan').disabled, false, 'can try again');
    assert.doesNotMatch(p.document.getElementById('banner').textContent, /not reachable/, fail.code);
    assert.equal(p.main.querySelector('textarea').value, PROMPT, 'the description is kept');
  }
});

test('a failed plan job: the error and its next step in the rail; Back to the description keeps the text', async () => {
  for (const [code, title, extra] of [['provider_quota', /out of credit/, 'Open Provider & key'], ['provider_timeout', /did not answer in time/, null], ['provider_error', /returned an error/, null]]) {
    const {p} = await open('?view=create', {keys: [{provider: 'openai', hint: 'abcd'}], planOutcome: code});
    await describe(p);
    await p.submit(p.main.querySelector('form'));
    await p.tick(2000);
    await p.tick(2000);
    const rail = p.main.querySelector('aside');
    assert.match(rail.textContent, title, code);
    assert.match(rail.textContent, /Plan failed/);
    assert.match(rail.textContent, new RegExp('Provider said: ' + code));
    assert.equal(Boolean(p.find('a', 'Open Provider & key')), Boolean(extra), code);
    p.page; // eslint quiet
    await p.click(p.find('a', 'Back to the description'));
    assert.equal(p.main.querySelector('textarea').value, PROMPT);
  }
});

test('generation_invalid: the validator report with the failed check, and Generate source again', async () => {
  const {p} = await open('?view=create', {keys: [{provider: 'openai', hint: 'abcd'}], sourceOutcome: 'invalid'});
  await describe(p);
  await p.submit(p.main.querySelector('form'));
  await p.tick(2000);
  await p.tick(2000);
  await p.click(p.find('button', 'Generate source'));
  await p.tick(2000);
  await p.tick(2000);
  assert.match(p.text(), /The generated output failed validation/);
  assert.match(p.text(), /1 check passed, 1 failed/);
  assert.match(p.text(), /Python syntaxFailed/);
  assert.match(p.text(), /line 3: invalid syntax/);
  assert.equal(p.main.querySelectorAll('form').filter((f) => f.className.includes('ix-accept')).length, 0, 'no accept on an invalid bundle');
  assert.ok(p.find('button', 'Generate source'));
});

test('cancel a running job; discard a draft; an accept conflict offers Reload', async () => {
  const keys = [{provider: 'openai', hint: 'abcd'}];
  let {p} = await open('?view=create', {keys});
  await describe(p);
  await p.submit(p.main.querySelector('form'));
  await p.click(p.find('button', 'Cancel'));
  assert.equal(posts(p, 'POST /ai/jobs/j2/cancel').length, 1);
  assert.match(p.main.querySelector('aside').textContent, /Cancelled/);
  assert.equal(p.polls(2000), 0, 'no polling after cancel');
  await p.click(p.find('button', 'Discard\u2026'));
  await p.click(p.find('button', 'Discard draft'));
  assert.equal(posts(p, 'DELETE /ai/drafts/d1').length, 1);
  assert.match(p.text(), /This draft was discarded/);

  ({p} = await open('?view=create', {keys, fail: {'POST /ai/drafts/d1/accept': {status: 409, code: 'conflict', message: 'Already accepted.'}}}));
  await describe(p);
  await p.submit(p.main.querySelector('form'));
  await p.tick(2000); await p.tick(2000);
  await p.click(p.find('button', 'Generate source'));
  await p.tick(2000); await p.tick(2000);
  await p.submit(p.main.querySelectorAll('form').find((f) => f.className.includes('ix-accept')));
  assert.match(p.text(), /This draft changed on the hub/);
  assert.match(p.text(), /Already accepted/);
  assert.ok(p.find('button', 'Reload'));
});

test('a draft address that is not yours or gone shows the error with Retry', async () => {
  const {p} = await open('?view=create&step=plan&draft=nope');
  assert.match(p.text(), /not.found/i);
  assert.ok(p.find('button', 'Try again'));
});

/* ---- my experiments, library ------------------------------------------- */

test('My experiments: a Drafts section with status, Open and Discard; accepted drafts open their experiment', async () => {
  const extra = {'GET /experiments': () => ({status: 200, body: {items: [{experiment: {id: 'xe9', title: 'Saved', created_at: '2026-10-09T00:00:00Z'}}]}})};
  const {p, ai} = await open('?view=mine', {}, extra);
  assert.equal(p.main.querySelector('section.ix-drafts').hidden, true, 'hidden while there are none');
  ai.state.drafts.set('d5', {id: 'd5', status: 'planned', prompt: 'A long description '.repeat(10), plan: PLAN, updated_at: '2026-10-09T11:00:00Z'});
  ai.state.drafts.set('d6', {id: 'd6', status: 'accepted', experiment_id: 'xe9', prompt: 'x', updated_at: '2026-10-09T12:00:00Z'});
  ai.state.drafts.set('d7', {id: 'd7', status: 'discarded', prompt: 'gone'});
  const {p: q} = await (async () => ({p: await mount({routes: ai.routes, search: '?view=mine'})}))();
  const block = q.main.querySelector('section.ix-drafts');
  assert.equal(block.hidden, false);
  const rows = block.querySelectorAll('tr').filter((r) => r.getAttribute('data-draft'));
  assert.deepEqual(rows.map((r) => r.getAttribute('data-draft')), ['d6', 'd5']);
  assert.match(rows[1].textContent, /Saccade adaptation in two monkeys.*Plan ready/);
  assert.equal(q.find('a', 'Open experiment').getAttribute('href'), '/?view=mine&id=xe9');
  assert.equal(rows[1].querySelectorAll('a').find((a) => a.textContent === 'Open').getAttribute('href'), '/?view=create&step=plan&draft=d5');
  await q.click(rows[1].querySelectorAll('button').find((b) => b.textContent === 'Discard\u2026'));
  await q.click(q.find('button', 'Discard draft'));
  assert.equal(ai.state.drafts.get('d5').status, 'discarded');
  assert.match(q.text(), /Draft discarded/);
  /* no AI on the hub: no section, no error */
  const plain = await mount({routes: base(extra), search: '?view=mine'});
  assert.equal(plain.main.querySelector('section.ix-drafts').hidden, true);
  assert.doesNotMatch(plain.text(), /no route/);
});

test('My experiments: versions whose manifest says ai_assisted carry the AI-assisted draft badge', async () => {
  const experiment = {id: 'xe1', title: 'Saved', owner: ALICE, created_at: '2026-10-09T00:00:00Z'};
  const versions = [
    {id: 'xv1', version: '0.1.0', sha256: 'ab'.repeat(32), size: 9, created_at: '2026-10-09T00:00:00Z', manifest: {ai_assisted: true}},
    {id: 'xv0', version: '0.0.1', sha256: 'cd'.repeat(32), size: 9, created_at: '2026-10-08T00:00:00Z', manifest: {}},
  ];
  const {p} = await open('?view=mine&id=xe1', {}, {'GET /experiments/xe1': () => ({status: 200, body: {experiment, versions}})});
  const rows = p.main.querySelectorAll('tr').filter((r) => r.querySelectorAll('td').length);
  assert.match(rows[0].textContent, /AI-assisted draft/);
  assert.doesNotMatch(rows[1].textContent, /AI-assisted draft/);
});

test('Library: Remove asks first, then DELETE /library/{id} and the list reloads', async () => {
  const lib = [{experiment: {id: 'e1', title: 'Fixation demo', owner: {display_name: 'Bob'}, tags: []}, version: {id: 'v1', version: '1.0.0', sha256: 'ab'.repeat(32)}}];
  const {p, ai} = await open('?view=library', {library: lib}, {'GET /experiments/e1': () => ({status: 200, body: {experiment: {id: 'e1', published_version_id: 'v1'}, versions: []}})});
  const remove = p.find('button', 'Remove\u2026');
  assert.equal(remove.disabled, false);
  await p.click(remove);
  assert.match(p.text(), /Remove Fixation demo from your library\?/);
  await p.click(p.find('button', 'Keep it'));
  assert.equal(p.hub.calls.filter((c) => c.init.method === 'DELETE').length, 0);
  await p.click(p.find('button', 'Remove\u2026'));
  await p.click(p.find('button', 'Remove from library'));
  assert.equal(posts(p, 'DELETE /library/e1').length, 1);
  assert.equal(ai.state.library.length, 0);
  assert.match(p.text(), /Removed Fixation demo from your library/);
  assert.match(p.text(), /Your library is empty/);
});
