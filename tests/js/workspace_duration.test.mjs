/* The duration estimate beside the Start button (workspace_duration.js): its
 * pure part, and the controller's timing rules on the fake page. The numbers
 * themselves are the server's (modes/estimate.py, tests/unit/test_estimate.py);
 * these hold the page to never showing one for a form it does not describe. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

import { FakeDocument } from './fake_dom.mjs';

const ASSETS = new URL('../../src/alhazen/cli/assets/', import.meta.url);

function load() {
  const context = vm.createContext({AbortController, JSON, Math, Promise});
  vm.runInContext(
    readFileSync(new URL('workspace_duration.js', ASSETS), 'utf8') + '\nthis.DE = DurationEstimate;',
    context,
  );
  return context.DE;
}

/* Timers the test advances by hand. */
function fakeTimers() {
  let next = 1;
  const pending = new Map();
  return {
    setTimeout(fn, ms) { const id = next++; pending.set(id, fn); return id; },
    clearTimeout(id) { pending.delete(id); },
    fire() { const fns = [...pending.values()]; pending.clear(); fns.forEach((f) => f()); },
    get count() { return pending.size; },
  };
}

const MAIN = {
  project: 'p1', mode: 'run', task: 'amodal-averaging', parameter_set: 'Main', rig: 'configs/rig-lab.yaml',
  trials: 1, headless: false, mouse: false, extra_args: '', parameters_yaml: 'paradigm: {}',
};
const ANSWER = {
  schema: 1, status: 'ok', headline: '34–59 min', plus: 'plus calibration and 5 manual breaks',
  counts: {kind: 'planned', trials: 576, trials_max: 576, blocks: 6, breaks: 5},
  per_trial: {iti_s: 0.5, min_s: 3.2, max_s: 6.4, spans: [
    {label: 'fixation acquired', kind: 'wait', min_s: 0, max_s: 2, expected_s: null, basis: ''},
    {label: 'landing dwell', kind: 'fixed', min_s: 0.25, max_s: 0.25, expected_s: 0.25, basis: ''},
  ]},
  manual: ['eye-tracker calibration before trial 1 (requested at start-up)'],
  excluded: ['re-served trials: …'], assumptions: [],
  basis: {refresh: 'frame durations at 120 Hz', params: 'configs/task.yaml', reductions: []},
};

const flush = () => new Promise((r) => setImmediate(r));

function setup(DE, draft) {
  const doc = new FakeDocument();
  const container = doc.createElement('div');
  const timers = fakeTimers();
  const asked = [];
  let form = draft;
  const ctl = DE.mount(container, {
    document: doc,
    timers,
    debounceMs: 300,
    read: () => form,
    ask: (d, signal) => new Promise((resolve, reject) => {
      asked.push({draft: d, signal, resolve, reject});
      signal.addEventListener('abort', () => reject(new Error('aborted')));
    }),
  });
  return {doc, container, timers, asked, ctl, set: (d) => { form = d; }};
}

test('a signature ignores key order and fields that do not decide the duration', () => {
  const DE = load();
  assert.equal(DE.signature({...MAIN, subject: 's01'}), DE.signature({...MAIN}));
  assert.equal(
    DE.signature({...MAIN, parameters: {b: 1, a: 2}}),
    DE.signature({...MAIN, parameters: {a: 2, b: 1}}),
  );
  assert.notEqual(DE.signature(MAIN), DE.signature({...MAIN, rig: 'alhazen/laptop'}));
  assert.notEqual(DE.signature(MAIN), DE.signature({...MAIN, parameters_yaml: 'x: 1'}));
  assert.equal(DE.signature(null), '');
  assert.equal(DE.signature({waiting: 'loading'}), '');
});

test('describe: the line, the counts and every section of the disclosure', () => {
  const DE = load();
  const view = DE.describe(ANSWER);
  assert.equal(view.state, 'ok');
  assert.equal(view.value, '34–59 min');
  assert.equal(view.counts, '576 trials · 6 blocks');
  assert.equal(view.plus, 'plus calibration and 5 manual breaks');
  const headings = Array.from(view.details, (d) => d.heading);
  assert.deepEqual(headings, ['One trial', 'Not included: up to a person', 'Not included', 'Basis']);
  assert.match(view.details[0].lines[0], /fixation acquired: up to 2 s/);
  assert.match(view.details[0].lines[1], /landing dwell: 0.25 s/);
});

test('describe: adaptive bounds, open-ended and unavailable answers say so', () => {
  const DE = load();
  const adaptive = DE.describe({...ANSWER, counts: {kind: 'adaptive', trials: 304, trials_max: 375}});
  assert.equal(adaptive.counts, '304–375 trials');
  assert.ok(adaptive.details.some((d) => /stopping rule/.test(d.lines[0])));
  const open = DE.describe({status: 'open-ended', headline: 'Open-ended, until stopped', reason: 'Demo…'});
  assert.equal(open.state, 'open');
  assert.equal(open.value, 'Open-ended, until stopped');
  const old = DE.describe({status: 'unavailable', headline: 'Estimate unavailable for this environment', reason: 'old'});
  assert.equal(old.state, 'unavailable');
  assert.equal(old.details[0].heading, 'Why');
});

test('a change clears the number at once, and asks once after the edits settle', async () => {
  const DE = load();
  const s = setup(DE, MAIN);
  s.ctl.update();
  const root = s.container.children[0];
  assert.equal(root.dataset.state, 'pending');
  assert.match(root.textContent, /Estimating/);
  assert.equal(s.asked.length, 0);
  // Two more edits before the debounce fires: still one request, for the last.
  s.set({...MAIN, parameters_yaml: 'a: 1'});
  s.ctl.update();
  s.set({...MAIN, parameters_yaml: 'a: 2'});
  s.ctl.update();
  assert.equal(s.timers.count, 1);
  s.timers.fire();
  assert.equal(s.asked.length, 1);
  assert.equal(s.asked[0].draft.parameters_yaml, 'a: 2');
  s.asked[0].resolve(ANSWER);
  await flush();
  assert.equal(root.dataset.state, 'ok');
  assert.match(root.textContent, /34–59 min/);
  // The same form again asks nothing (polls call update every 1.5 s).
  s.ctl.update();
  assert.equal(s.timers.count, 0);
  assert.equal(root.dataset.state, 'ok');
});

test("the previous task's number is never shown for a new form, and a late answer is dropped", async () => {
  const DE = load();
  const s = setup(DE, MAIN);
  s.ctl.update();
  s.timers.fire();
  const first = s.asked[0];
  // The person switches task while the first answer is on its way.
  s.set({...MAIN, task: 'other', parameter_set: 'Pilot'});
  s.ctl.update();
  const root = s.container.children[0];
  assert.equal(root.dataset.state, 'pending');
  assert.ok(first.signal.aborted, 'the overtaken request is aborted');
  first.resolve(ANSWER);  // arrives anyway
  await flush();
  assert.equal(root.dataset.state, 'pending', 'an overtaken answer is not painted');
  assert.doesNotMatch(root.textContent, /34–59/);
  s.timers.fire();
  s.asked[1].resolve({...ANSWER, headline: '17–30 min'});
  await flush();
  assert.match(root.textContent, /17–30 min/);
});

test('a form that cannot be sent yet says why, and an error is shown as no estimate', async () => {
  const DE = load();
  const s = setup(DE, {waiting: 'Waiting for the task parameters…'});
  s.ctl.update();
  const root = s.container.children[0];
  assert.equal(root.dataset.state, 'pending');
  assert.match(root.textContent, /Waiting for the task parameters/);
  assert.equal(s.timers.count, 0);
  s.set(MAIN);
  s.ctl.update();
  s.timers.fire();
  s.asked[0].reject(new Error('invalid config: n_per_condition must be >= 1'));
  await flush();
  assert.equal(root.dataset.state, 'error');
  assert.match(root.textContent, /No estimate/);
  assert.match(root.textContent, /n_per_condition/);
  // Back to no project: nothing at all.
  s.set(null);
  s.ctl.update();
  assert.equal(root.dataset.state, 'none');
  assert.equal(root.textContent.includes('No estimate'), false);
});

test('refresh asks again for the same form', async () => {
  const DE = load();
  const s = setup(DE, MAIN);
  s.ctl.update();
  s.timers.fire();
  s.asked[0].resolve(ANSWER);
  await flush();
  s.ctl.refresh();
  assert.equal(s.container.children[0].dataset.state, 'pending');
  s.timers.fire();
  assert.equal(s.asked.length, 2);
});
