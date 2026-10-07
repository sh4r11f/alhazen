/* Measure rig's checklist: the pure part of workspace_measure.js, and its
 * renderers on the fake page. The server re-checks everything here
 * (workspace.py check_measurements); these tests hold the page to the same
 * rules so it never offers a launch the server would refuse. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

import { FakeDocument } from './fake_dom.mjs';

const ASSETS = new URL('../../src/alhazen/cli/assets/', import.meta.url);

function load() {
  const context = vm.createContext({});
  vm.runInContext(readFileSync(new URL('workspace_measure.js', ASSETS), 'utf8') + '\nthis.MeasureChoice = MeasureChoice;', context);
  return context.MeasureChoice;
}

const CATALOG = [
  {key: 'tracker.accuracy', group: 'Eye tracker', title: 'Accuracy', order: 85,
    requires: ['tracker.calibration'], needs: ['tracker', 'display'], subject: 'required'},
  {key: 'monitor.refresh', group: 'Monitor', title: 'Refresh', order: 10, requires: [],
    needs: ['display'], subject: 'none'},
  {key: 'tracker.calibration', group: 'Eye tracker', title: 'Calibration', order: 80,
    requires: [], needs: ['tracker', 'display'], subject: 'required'},
  {key: 'input.keys', group: 'Keyboard & mouse', title: 'Keys', order: 40, requires: [],
    needs: ['display', 'keyboard'], subject: 'optional'},
  {key: 'monitor.colour', group: 'Monitor', title: 'Colour', order: 65, requires: [], needs: []},
  {key: 'reward.volume', group: 'Reward', title: 'Juice per pulse', order: 70, requires: [],
    needs: ['reward', 'operator'], subject: 'none'},
];

test('nothing is selected at first, and that cannot launch', () => {
  const M = load();
  const state = M.create(CATALOG);
  assert.equal(M.selection(state).length, 0);
  assert.match(M.problem(state), /at least one/);
});

test('the selection is sent in run order, not tick order', () => {
  const M = load();
  let state = M.create(CATALOG);
  state = M.toggle(state, 'input.keys', true).state;
  state = M.toggle(state, 'monitor.refresh', true).state;
  assert.deepEqual([...M.selection(state)], ['monitor.refresh', 'input.keys']);
  assert.equal(M.problem(state), '');
});

test('ticking a job ticks its prerequisite, and says so', () => {
  const M = load();
  const {state, note} = M.toggle(M.create(CATALOG), 'tracker.accuracy', true);
  assert.deepEqual([...M.selection(state)], ['tracker.calibration', 'tracker.accuracy']);
  assert.match(note, /needed first: Calibration/);
  const cleared = M.toggle(state, 'tracker.calibration', false);
  assert.equal(M.selection(cleared.state).length, 0);
  assert.match(cleared.note, /Also cleared.*Accuracy/);
});

test('a remembered selection drops keys the catalog no longer has', () => {
  const M = load();
  const state = M.create(CATALOG, ['monitor.refresh', 'gone.away']);
  assert.deepEqual([...M.selection(state)], ['monitor.refresh']);
});

test('a subject is asked for only when a measurement is of the subject', () => {
  const M = load();
  let state = M.toggle(M.create(CATALOG), 'monitor.refresh', true).state;
  assert.equal(M.subjectNeed(state), 'none');
  state = M.toggle(state, 'input.keys', true).state;
  assert.equal(M.subjectNeed(state), 'optional');
  state = M.toggle(state, 'tracker.calibration', true).state;
  assert.equal(M.subjectNeed(state), 'required');
});

test('the rig file says ahead of time what a job will not find', () => {
  const M = load();
  const job = (key) => CATALOG.find((e) => e.key === key);
  const laptop = {display: {backend: 'psychopy'}, devices: {eyetracker: {backend: 'mouse_sim'}}};
  assert.match(M.rigHint(job('tracker.calibration'), laptop), /stand-in/);
  assert.match(M.rigHint(job('reward.volume'), laptop), /No reward/);
  assert.match(M.rigHint(job('reward.volume'), {devices: {reward: {backend: 'simulated'}}}), /simulated/);
  assert.match(M.rigHint(job('monitor.colour'), laptop), /colorimeter/);
  assert.equal(M.rigHint(job('monitor.refresh'), laptop), '');
  assert.match(M.rigHint(job('monitor.refresh'), {display: {backend: 'simulated'}}), /simulated/);
});

test('only passed and measured count as results in the queue line', () => {
  const M = load();
  const status = {done: 3, total: 4, jobs: [
    {state: 'passed'}, {state: 'unavailable'}, {state: 'failed'}, {state: 'waiting'}]};
  assert.equal(M.summary(status), '3 of 4 done · 1 with a result · 1 failed');
  assert.deepEqual([...M.stateWord('unavailable')], ['Unavailable', 'off']);
  assert.deepEqual([...M.stateWord('cancelled')], ['Cancelled', 'off']);
});

test('the renderers draw into the container they are handed', () => {
  const M = load();
  const document = new FakeDocument();
  const list = document.createElement('div');
  let state = M.toggle(M.create(CATALOG), 'tracker.accuracy', true).state;
  const toggled = [];
  M.renderChecklist(list, state, {document, onToggle: (k, on) => toggled.push([k, on]),
    rig: {devices: {}}});
  const text = list.textContent;
  assert.match(text, /Runs in this order: Calibration → Accuracy/);
  assert.match(text, /No eye tracker on this rig/);
  const progress = document.createElement('div');
  M.renderProgress(progress, {current: 'tracker.calibration', done: 0, total: 2, jobs: [
    {key: 'tracker.calibration', title: 'Calibration', state: 'waiting',
      waiting_for: 'subject on target 1 of 5'},
    {key: 'tracker.accuracy', title: 'Accuracy', state: 'queued'}]}, {document});
  assert.equal(progress.hidden, false);
  assert.match(progress.textContent, /At the rig: subject on target 1 of 5/);
  M.renderProgress(progress, null, {document});
  assert.equal(progress.hidden, true);
});
