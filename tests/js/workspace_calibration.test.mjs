/* The Rig section's calibration-target choice: the pure part
 * (workspace_calibration.js) and the controls workspace.js draws from it,
 * down to what a launch sends.
 *
 * Run: node --test "tests/js/*.test.mjs"
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';
import vm from 'node:vm';

import { loadWorkspace, plain, settle } from './load_workspace.mjs';

const ASSETS = new URL('../../src/alhazen/cli/assets/', import.meta.url);

/* The pure module on its own, as workspace_parameters.test.mjs loads its. */
function pure() {
  const context = vm.createContext({});
  vm.runInContext(readFileSync(new URL('workspace_calibration.js', ASSETS), 'utf8'), context);
  return vm.runInContext('CalibrationChoice', context);
}

const DEFAULTS = Object.freeze({
  appearance: 'standard', images: [], image_size_dva: 2.5, motion: 'still',
  pulse: { rate_hz: 1.0, min_scale: 1.0, max_scale: 1.4 },
});

const OFFER = Object.freeze({
  dir: 'C:/env/alhazen/calibration_images',
  images: ['animal_1', 'food_1', 'monkey_1', 'tree_1'],
  defaults: DEFAULTS,
});

const PROJECT = Object.freeze({
  id: 'p', name: 'demo-folder', title: 'Demo task', slug: 'demo', title_error: null,
  path: 'C:/projects/demo', python: 'python', available: true, alhazen_version: '2.11.0',
  rigs: [{ name: 'lab', source: 'experiment', path: 'configs/rig-lab.yaml', shadowed: false,
           extends: null }],
  rigs_note: null, configs: ['configs/task.yaml'], scripts: [],
  parameter_sets: [{ label: 'task', task: null, params: 'configs/task.yaml' }],
  default_parameter_set: 'task', parameter_sets_error: null,
  calibration_targets: OFFER,
});

function rigWith(eyetracker) {
  const values = {
    monitor: { width_px: 1920, height_px: 1080, refresh_rate_hz: 120, width_cm: 52.1,
               distance_cm: 57 },
    live_monitor: { enabled: false },
  };
  if (eyetracker) values.devices = { eyetracker };
  return { name: 'lab', source: 'experiment', extends: null, values };
}

async function page({ project = PROJECT, eyetracker = { backend: 'eyelink' } } = {}) {
  const app = loadWorkspace({});
  app.server.state = { projects: [project], runs: [], active: null };
  app.server.details = { launched: { id: 'launched', project: 'p', name: 'Demo task',
    mode: 'test', rig: 'configs/rig-lab.yaml', started: '2026-10-06T10:00:00', finished: null,
    status: 'running', returncode: null, command: [], cwd: '', directory: '', log: '',
    artifacts: [], monitor: null } };
  app.server.configs = { 'configs/task.yaml': { text: 'trials: 4\n', values: { trials: 4 } } };
  app.server.rigs = { 'configs/rig-lab.yaml': rigWith(eyetracker) };
  app.server.schemas = {};
  await app.run('refresh()');
  await settle();
  return app;
}

function chooseMode(app, mode) {
  app.byId('mode').value = mode;
  app.run('modeChanged()');
}

function buttons(app, id) {
  return app.byId(id).children;
}

function press(app, id, value) {
  const button = buttons(app, id).find((b) => b.dataset.value === value);
  assert.ok(button, `no ${value} button in #${id}`);
  button.fire('click');
}

function picture(app, name) {
  const button = app.byId('calibration-pictures').children.find((b) => b.title === name);
  assert.ok(button, `no picture ${name}`);
  return button;
}

async function launchBody(app) {
  app.server.posted.length = 0;
  app.byId('launch-form').fire('submit', { preventDefault() {} });
  await settle();
  await settle();
  const posts = app.server.posted.filter((p) => p.path === '/api/runs');
  assert.equal(posts.length, 1);
  return posts[0].body;
}

function fillIdentity(app) {
  app.byId('subject').value = 's01';
  app.byId('initials').value = 'HD';
}

describe('the calibration choice, pure', () => {
  const C = pure();

  it('pulses with the formula the rig draws with', () => {
    const pulse = DEFAULTS.pulse;
    assert.equal(C.pulseScale(0, pulse), 1.0);
    assert.ok(Math.abs(C.pulseScale(0.25, pulse) - 1.2) < 1e-12);
    assert.ok(Math.abs(C.pulseScale(0.5, pulse) - 1.4) < 1e-12);
    assert.ok(Math.abs(C.pulseScale(1.0, pulse) - 1.0) < 1e-12);
    assert.equal(C.pulseScale(-1, pulse), 1.0);
  });

  it('fills the rig’s setting in from the defaults', () => {
    const rig = rigWith({ backend: 'viewpixx', calibration_target: { motion: 'pulse',
      pulse: { rate_hz: 0.5 } } }).values;
    assert.ok(C.drawsTarget(rig));
    assert.deepEqual(plain(C.fromRig(rig, DEFAULTS)), {
      appearance: 'standard', images: [], image_size_dva: 2.5, motion: 'pulse',
      pulse: { rate_hz: 0.5, min_scale: 1.0, max_scale: 1.4 },
    });
    assert.ok(!C.drawsTarget(rigWith({ backend: 'mouse_sim' }).values));
    assert.ok(!C.drawsTarget(rigWith(null).values));
  });

  it('sends only what differs from the rig', () => {
    const rig = C.fromRig(rigWith({ backend: 'eyelink' }).values, DEFAULTS);
    assert.equal(C.toSend({ ...rig }, rig), null);
    assert.deepEqual(plain(C.toSend({ ...rig, motion: 'pulse' }, rig)), { motion: 'pulse' });
    assert.deepEqual(plain(C.toSend({ ...rig, appearance: 'random_images', images: [] }, rig)),
      { appearance: 'random_images' });
    assert.deepEqual(
      plain(C.toSend({ ...rig, appearance: 'images', images: ['tree_1', 'food_1'] }, rig)),
      { appearance: 'images', images: ['tree_1', 'food_1'] });
    const chosen = { ...rig, appearance: 'images', images: ['a', 'b'] };
    assert.equal(C.toSend({ ...chosen, images: ['a', 'b'] }, chosen), null);
    /* Chosen pictures have an order; a random set does not. */
    assert.deepEqual(plain(C.toSend({ ...chosen, images: ['b', 'a'] }, chosen)),
      { images: ['b', 'a'] });
    const random = { ...rig, appearance: 'random_images', images: ['a', 'b'] };
    assert.equal(C.toSend({ ...random, images: ['b', 'a'] }, random), null);
  });

  it('refuses chosen pictures with none chosen', () => {
    assert.match(C.problem({ appearance: 'images', images: [] }), /pick at least one/);
    assert.equal(C.problem({ appearance: 'random_images', images: [] }), null);
  });

  it('sizes the target as the rig does', () => {
    const monitor = { width_px: 1920, width_cm: 52.1, distance_cm: 57 };
    assert.equal(C.stillPx({ appearance: 'standard' }, monitor), 24);
    const px = C.stillPx({ appearance: 'images', image_size_dva: 2.5 }, monitor);
    assert.ok(Math.abs(px - 2.5 * (1920 / 52.1) * 57 * Math.tan(Math.PI / 180)) < 1e-9);
    assert.equal(C.stillPx({ appearance: 'images', image_size_dva: 2.5 }, {}), null);
  });
});

describe('the Rig section’s calibration target', () => {
  it('is offered for a rig whose tracker draws a target, starting from the rig’s setting',
    async () => {
      const app = await page();
      chooseMode(app, 'test');
      assert.equal(app.byId('calibration').hidden, false);
      assert.deepEqual(plain(buttons(app, 'calibration-appearance').map((b) => b.textContent)),
        ['Standard', 'Chosen pictures', 'Random pictures']);
      assert.deepEqual(plain(buttons(app, 'calibration-motion').map((b) => b.textContent)),
        ['Still', 'Pulsating']);
      const pressed = (id) => buttons(app, id).find((b) => b.getAttribute('aria-pressed') === 'true')
        .dataset.value;
      assert.equal(pressed('calibration-appearance'), 'standard');
      assert.equal(pressed('calibration-motion'), 'still');
      assert.equal(app.byId('calibration-origin').textContent, 'the rig’s setting');
      assert.equal(app.byId('calibration-pictures-block').hidden, true);
      fillIdentity(app);
      /* Nothing changed: nothing sent, and the run uses the rig's own. */
      assert.equal('calibration_target' in await launchBody(app), false);
    });

  it('sends chosen pictures in the order picked, and a pulse', async () => {
    const app = await page();
    chooseMode(app, 'test');
    fillIdentity(app);
    press(app, 'calibration-appearance', 'images');
    assert.equal(app.byId('calibration-pictures-block').hidden, false);
    /* None picked yet: the launch is held back and says why. */
    assert.equal(app.byId('launch').disabled, true);
    assert.match(app.byId('launch-note').textContent, /pick at least one picture/);
    picture(app, 'tree_1').fire('click');
    picture(app, 'monkey_1').fire('click');
    assert.equal(picture(app, 'tree_1').getAttribute('aria-pressed'), 'true');
    press(app, 'calibration-motion', 'pulse');
    assert.equal(app.byId('launch').disabled, false);
    assert.equal(app.byId('calibration-origin').textContent, 'changed for this run');
    assert.match(app.byId('calibration-help').textContent,
      /pictures tree_1, monkey_1 in turn, pulsating 1–1.4× at 1 Hz/);
    /* Each picture is fetched by name, with the token, from the launcher. */
    const img = picture(app, 'tree_1').children[0];
    assert.match(img.src, /^\/calibration-picture\?project=p&name=tree_1&token=/);
    const body = await launchBody(app);
    assert.deepEqual(plain(body.calibration_target),
      { appearance: 'images', images: ['tree_1', 'monkey_1'], motion: 'pulse' });
  });

  it('sends a random choice over all pictures when none are picked', async () => {
    const app = await page();
    chooseMode(app, 'run');
    fillIdentity(app);
    press(app, 'calibration-appearance', 'random_images');
    assert.equal(app.byId('calibration-pictures-label').textContent, 'Drawn from all pictures');
    assert.deepEqual(plain((await launchBody(app)).calibration_target),
      { appearance: 'random_images' });
  });

  it('is shown but not sent for a launch that does not calibrate', async () => {
    const app = await page();
    chooseMode(app, 'test');
    press(app, 'calibration-motion', 'pulse');
    chooseMode(app, 'simulate');
    assert.equal(app.byId('calibration').hidden, false);
    assert.ok(buttons(app, 'calibration-motion').every((b) => b.disabled));
    assert.match(app.byId('calibration-help').textContent, /Applies to run and test/);
    assert.equal('calibration_target' in await launchBody(app), false);
  });

  it('is not sent with the mouse as gaze', async () => {
    const app = await page();
    chooseMode(app, 'test');
    fillIdentity(app);
    press(app, 'calibration-motion', 'pulse');
    app.byId('mouse').checked = true;
    app.byId('mouse').fire('change');
    assert.match(app.byId('calibration-help').textContent, /Mouse as gaze replaces/);
    assert.equal('calibration_target' in await launchBody(app), false);
  });

  it('is absent for a rig whose tracker draws no target', async () => {
    for (const eyetracker of [null, { backend: 'mouse_sim' }]) {
      const app = await page({ eyetracker });
      chooseMode(app, 'test');
      assert.equal(app.byId('calibration').hidden, true);
    }
  });

  it('tells a project on an older alhazen to update rather than offering it', async () => {
    const { calibration_targets: _, ...older } = PROJECT;
    const app = await page({ project: { ...older, alhazen_version: '2.10.0' } });
    chooseMode(app, 'test');
    assert.equal(app.byId('calibration').hidden, false);
    assert.equal(app.byId('calibration-controls').hidden, true);
    assert.match(app.byId('calibration-help').textContent, /2\.10\.0.*no calibration-target choice/);
  });

  it('describes the preview as a look, not as accuracy, and in rig pixels', async () => {
    const app = await page({ eyetracker: { backend: 'viewpixx', calibration_target: {
      appearance: 'images', images: ['food_1'], motion: 'pulse' } } });
    chooseMode(app, 'test');
    const caption = app.byId('calibration-caption').textContent;
    assert.match(caption, /2\.5° ≈ 92 px on this rig · pulses 1–1\.4× at 1 Hz/);
    assert.match(caption, /not of calibration accuracy/);
    /* The rig already says so: nothing to send. */
    fillIdentity(app);
    assert.equal('calibration_target' in await launchBody(app), false);
  });
});
