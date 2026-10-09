/* The Training panel (workspace_training.js) and Training mode on the Run
 * page (workspace.js): the ladder drawn in order, the stage the operator
 * chooses, the recommendation shown but never acted on, and what a launch
 * sends.
 *
 * Run: node --test "tests/js/*.test.mjs"
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';
import vm from 'node:vm';

import { loadWorkspace, plain, settle } from './load_workspace.mjs';

const ASSETS = new URL('../../src/alhazen/cli/assets/', import.meta.url);

function pure() {
  const context = vm.createContext({});
  vm.runInContext(readFileSync(new URL('workspace_training.js', ASSETS), 'utf8'), context);
  return vm.runInContext('TrainingLadder', context);
}

const LADDER = Object.freeze({
  label: 'Pursuit (monkey)', file: 'configs/training-pursuit.yaml', name: 'pursuit',
  title: 'Pursuit training', description: 'From a held fixation to the real trial.',
  stages: [
    { id: 'fixate', number: 1, title: 'Fixate', description: 'Hold the point.', task: 'pursuit-training',
      params: 'task-pursuit-monkey.yaml', overrides: { 'training.mode': 'fixate' },
      success: 'FIXATION_HELD', reward: {}, criterion: null },
    { id: 'saccade', number: 2, title: 'Saccade to the dot', description: '', task: 'pursuit-training',
      params: 'task-pursuit-monkey.yaml', overrides: {}, success: 'LANDED',
      reward: { success: { n_pulses: 1, pulse_ms: 150, inter_pulse_ms: 200 } },
      criterion: { window: 100, min_trials: 50, promote_when: { success_rate: 0.8 }, demote_when: {} } },
  ],
});

const PROJECT = Object.freeze({
  id: 'p', name: 'kde', title: 'KDE vergence', slug: 'kde', title_error: null,
  path: 'C:/projects/kde', python: 'python', available: true, alhazen_version: '2.13.0',
  capabilities: ['training-mode'], trains: true, ladders: [LADDER], ladders_error: null,
  rigs: [{ name: 'lab', source: 'experiment', path: 'configs/rig-lab.yaml', shadowed: false,
           extends: null }],
  rigs_note: null, configs: ['configs/task.yaml'], scripts: [],
  parameter_sets: [{ label: 'task', task: null, params: 'configs/task.yaml' }],
  default_parameter_set: 'task', parameter_sets_error: null,
});

const HISTORY = Object.freeze({
  ladders: [{
    label: 'Pursuit (monkey)', ladder: 'pursuit', title: 'Pursuit training',
    stages: [
      { id: 'fixate', number: 1, title: 'Fixate', success: 'FIXATION_HELD', criterion: null,
        sessions: [], training_sessions: 0, rehearsal_sessions: 0, trials: 0, finished: 0,
        successes: 0, success_rate: null, subjects: {} },
      { id: 'saccade', number: 2, title: 'Saccade to the dot', success: 'LANDED',
        criterion: LADDER.stages[1].criterion,
        sessions: [{ kind: 'training', subject: 'm01', session: 3, run: 1, date: '20261008',
          created: '2026-10-08T10:00:00', success: 'LANDED', attempts: 60, finished: 50,
          successes: 42, faults: 0, success_rate: 0.84, path: 'x' }],
        training_sessions: 1, rehearsal_sessions: 0, trials: 60, finished: 50, successes: 42,
        success_rate: 0.84,
        subjects: { m01: { sessions: 1, finished: 50, successes: 42, last: '2026-10-08',
          recommendation: { verdict: 'advance', metrics: { success_rate: 0.84 }, trials: 50,
            window: 100, min_trials: 50, promote_when: { success_rate: 0.8 }, demote_when: {} } } } },
    ],
  }],
  error: null,
});

async function page({ project = PROJECT, history = HISTORY } = {}) {
  const app = loadWorkspace({});
  app.server.state = { projects: [project], runs: [], active: null };
  app.server.configs = { 'configs/task.yaml': { text: 'trials: 4\n', values: { trials: 4 } } };
  app.server.rigs = { 'configs/rig-lab.yaml': { name: 'lab', source: 'experiment', extends: null,
    values: { monitor: { width_px: 1920, height_px: 1080, refresh_rate_hz: 120, width_cm: 52,
      distance_cm: 57 }, live_monitor: { enabled: false } } } };
  app.server.schemas = {};
  app.server.training = history;
  await app.run('refresh()');
  await settle();
  await settle();
  return app;
}

function chooseMode(app, mode) {
  app.byId('mode').value = mode;
  app.run('modeChanged()');
}

function rungs(app) {
  const found = [];
  const walk = (el) => {
    for (const child of el.children || []) {
      if (child.dataset && child.dataset.stage) found.push(child);
      walk(child);
    }
  };
  walk(app.byId('training-ladder'));
  return found;
}

function text(el) {
  return el.textContent;
}

describe('the training panel, pure', () => {
  const T = pure();
  it('says a criterion in words', () => {
    assert.equal(T.criterionText(LADDER.stages[1].criterion),
      'success ≥ 80% over the last 100 trials (judged from 50)');
    assert.equal(T.criterionText({ window: 20, min_trials: 10, promote_when: {},
      demote_when: { completed_rate: 0.5 } }),
      'Judged over the last 20 trials (judged from 10); back if completed ≤ 50%');
    assert.equal(T.criterionText(null), null);
  });
  it('says a delivery in words', () => {
    assert.equal(T.pulses({ n_pulses: 1, pulse_ms: 150 }), '1 × 150 ms');
    assert.equal(T.pulses({ n_pulses: 0 }), 'nothing');
    assert.equal(T.pulses(null), null);
  });
});

describe('Training mode on the Run page', () => {
  it('is offered only to a project that registers a ladder and can run one', async () => {
    const app = await page();
    const offered = app.byId('mode').children.map((o) => o.value);
    assert.ok(offered.includes('training'));
    const older = await page({ project: { ...PROJECT, trains: false } });
    assert.ok(!older.byId('mode').children.map((o) => o.value).includes('training'));
    const none = await page({ project: { ...PROJECT, ladders: [] } });
    assert.ok(!none.byId('mode').children.map((o) => o.value).includes('training'));
  });

  it('draws the ladder in Task parameters’ place, in order', async () => {
    const app = await page();
    chooseMode(app, 'training');
    assert.equal(app.byId('training-ladder-stage').hidden, false);
    assert.equal(app.byId('task-parameters').hidden, true);
    assert.deepEqual(rungs(app).map((r) => r.dataset.stage), ['fixate', 'saccade']);
    assert.match(text(app.byId('training-ladder')), /Criterion: success ≥ 80%/);
    assert.match(text(app.byId('training-ladder')), /pays.*LANDED · 1 × 150 ms/);
  });

  it('waits for a stage, then shows the subject’s record and the recommendation', async () => {
    const app = await page();
    chooseMode(app, 'training');
    assert.equal(app.byId('launch').disabled, true);
    assert.match(text(app.byId('launch-note')), /Choose the training stage/);
    app.byId('subject').value = 'm01';
    app.byId('initials').value = 'MK';
    app.run('identityChanged()');
    rungs(app).find((r) => r.dataset.stage === 'saccade').fire('click');
    const shown = text(app.byId('training-ladder'));
    assert.match(shown, /84%/);
    assert.match(shown, /42\/50 · 1 session/);
    assert.match(shown, /Criterion met — consider the next stage/);
    // A recommendation is words, never a move: the chosen stage is still
    // the operator's.
    assert.equal(rungs(app).find((r) => r.dataset.stage === 'saccade').getAttribute('aria-checked'), 'true');
    assert.match(text(app.byId('launch-summary')), /Training · Pursuit \(monkey\) · stage 2 Saccade to the dot/);
  });

  it('launches the chosen stage, with no parameters and no task of its own', async () => {
    const app = await page();
    chooseMode(app, 'training');
    app.byId('subject').value = 'm01';
    app.byId('initials').value = 'MK';
    app.run('identityChanged()');
    rungs(app).find((r) => r.dataset.stage === 'fixate').fire('click');
    app.server.posted.length = 0;
    app.byId('launch-form').fire('submit', { preventDefault() {} });
    await settle();
    await settle();
    const body = app.server.posted.find((p) => p.path === '/api/runs').body;
    assert.equal(body.mode, 'training');
    assert.equal(body.ladder, 'Pursuit (monkey)');
    assert.equal(body.stage, 'fixate');
    assert.equal(body.rehearse, false);
    assert.equal(body.task, null);
    assert.equal(body.parameter_set, null);
    assert.ok(!('parameters' in body) && !('parameters_yaml' in body));
    assert.equal(body.subject, 'm01');
  });

  it('rehearses a stage as a simulation, with its trial count', async () => {
    const app = await page();
    chooseMode(app, 'training');
    rungs(app).find((r) => r.dataset.stage === 'fixate').fire('click');
    const tick = (function find(el) {
      for (const child of el.children || []) {
        if (child.id === 'training-rehearse') return child;
        const inner = find(child);
        if (inner) return inner;
      }
      return null;
    })(app.byId('training-ladder'));
    tick.checked = true;
    tick.fire('change');
    assert.equal(app.byId('trials-field').hidden, false);
    assert.match(text(app.byId('launch-summary')), /rehearsal/);
    assert.equal(plain(app.run('launchDraft()')).rehearse, true);
  });
});
