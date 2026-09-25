/* The experiment workspace page: what it draws for a run and what it sends
 * when a run is launched. workspace_parameters.test.mjs covers the parameter
 * editor; this file covers the rest of workspace.js.
 *
 * Run: node --test "tests/js/*.test.mjs"
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { loadWorkspace, plain, response, settle } from './load_workspace.mjs';

/* One registered project with one rig, one preset and no scripts. */
const PROJECT = Object.freeze({
  id: 'p', name: 'Demo task', path: 'C:/projects/demo', python: 'python', available: true,
  rigs: ['configs/rig-mac.yaml'], configs: ['configs/task.yaml'], scripts: [],
});

/* A rig YAML as /api/config returns it: parsed values and the source text.
 * `null` leaves the dashboard block out, as a rig written before the
 * dashboard existed would; the model's default is then off. (Not
 * `undefined`: pageWith's destructuring default would turn that into true.) */
function rig(dashboardEnabled) {
  const values = {
    monitor: {
      width_px: 1920, height_px: 1080, refresh_rate_hz: 60, width_cm: 52, distance_cm: 57,
    },
  };
  if (dashboardEnabled !== null) values.dashboard = { enabled: dashboardEnabled };
  return { text: '# rig', values: values };
}

/* A run as /api/runs/<id> returns it, running by default. */
function runDetail(overrides) {
  return {
    id: 'r1', project: 'p', name: 'Demo task', mode: 'simulate', rig: 'configs/rig-mac.yaml',
    started: '2026-09-25T10:00:00', finished: null, status: 'running', returncode: null,
    command: ['python', 'run.py', '--mode', 'simulate'], cwd: 'C:/projects/demo',
    directory: 'C:/state/runs/r1', log: 'starting\n', artifacts: [], monitor: null,
    ...overrides,
  };
}

/* The same run as /api/state lists it (no log, artifacts or monitor). */
function summary(detail) {
  const { log, artifacts, monitor, ...rest } = detail;
  return rest;
}

const ACTIVE = ['running', 'stopping'];

/**
 * A page showing PROJECT with `run` selected (or no run), after one refresh:
 * the project chosen, its rig and preset loaded, the run drawn.
 */
async function pageWith({ run = null, dashboardEnabled = true } = {}) {
  const app = loadWorkspace();
  app.server.state = {
    projects: [PROJECT],
    runs: run ? [summary(run)] : [],
    active: run && ACTIVE.includes(run.status) ? run.id : null,
  };
  app.server.details = run ? { [run.id]: run } : {};
  app.server.configs = {
    'configs/rig-mac.yaml': rig(dashboardEnabled),
    'configs/task.yaml': { text: 'trials: 4\n', values: { trials: 4 } },
  };
  await app.run('refresh()');
  await settle();
  return app;
}

/** Update the selected run on the fake server and redraw it. */
async function redraw(app, run) {
  app.server.details[run.id] = run;
  app.server.state.runs = [summary(run)];
  app.server.state.active = ACTIVE.includes(run.status) ? run.id : null;
  await app.run('refresh()');
  await settle();
}

/** Submit the launch form as a click on Start would. */
async function launch(app) {
  app.byId('launch-form').fire('submit', { preventDefault() {} });
  await settle();
  await settle();
}

const MONITOR_URL = 'http://127.0.0.1:41234/?token=monitor-secret';

describe('the Live monitor tab', () => {
  it('frames the monitor while the run is active and opens the tab once for a run launched here',
    async () => {
      const app = await pageWith();
      const frame = app.byId('monitor-frame');
      const note = app.byId('monitor-note');
      /* No run yet: nothing to frame, and the tab says so. */
      assert.equal(frame.hidden, true);
      assert.match(note.textContent, /start a run/i);

      /* The run is launched from this page; its monitor has not opened yet. */
      const running = runDetail({ monitor: null });
      app.server.launch = () => ({ id: running.id });
      app.server.details[running.id] = running;
      app.server.state.runs = [summary(running)];
      app.server.state.active = running.id;
      await launch(app);
      assert.equal(app.byId('media-tab').classList.contains('selected'), true);
      assert.equal(frame.hidden, true);
      assert.match(note.textContent, /waiting for the session to open its monitor/i);
      assert.equal(app.byId('monitor').hidden, true);

      /* The runner prints its URL: the frame loads it and the tab comes up. */
      await redraw(app, runDetail({ monitor: MONITOR_URL }));
      assert.equal(frame.hidden, false);
      assert.equal(frame.src, MONITOR_URL);
      assert.equal(note.hidden, true);
      assert.equal(app.byId('monitor-tab').classList.contains('selected'), true);
      assert.equal(app.byId('monitor-panel').hidden, false);
      assert.equal(app.byId('media-panel').hidden, true);
      assert.equal(app.byId('monitor').hidden, false);
      assert.equal(app.byId('monitor').href, MONITOR_URL);

      /* The reader moves to the console; the next poll must neither switch
       * back nor reload the frame (a reload would restart the monitor page). */
      app.byId('console-tab').fire('click');
      const writes = [];
      let src = frame.src;
      Object.defineProperty(frame, 'src', {
        get: () => src,
        set: (value) => { writes.push(value); src = value; },
      });
      await redraw(app, runDetail({ monitor: MONITOR_URL, log: 'trial 1\n' }));
      assert.equal(app.byId('console-tab').classList.contains('selected'), true);
      assert.equal(app.byId('monitor-tab').classList.contains('selected'), false);
      assert.deepEqual(writes, []);
    });

  it('empties the frame and points at the saved copy once the run has ended', async () => {
    const app = await pageWith({ run: runDetail({ monitor: MONITOR_URL }) });
    const frame = app.byId('monitor-frame');
    assert.equal(frame.src, MONITOR_URL);

    await redraw(app, runDetail({ monitor: MONITOR_URL, status: 'completed', returncode: 0 }));
    /* The monitor's server is gone with the session: the frame must not be
     * left to show a browser error page. */
    assert.equal(frame.hidden, true);
    assert.equal(frame.src, 'about:blank');
    const note = app.byId('monitor-note');
    assert.equal(note.hidden, false);
    assert.match(note.textContent, /closes with the session/i);
    assert.match(note.textContent, /figures\/dashboard\.html/);
    /* Nor is a link to a dead server offered. */
    assert.equal(app.byId('monitor').hidden, true);
  });

  it('does not open the tab by itself for a run picked from the history', async () => {
    const app = await pageWith({ run: runDetail({ monitor: MONITOR_URL }) });
    assert.equal(app.byId('monitor-frame').src, MONITOR_URL);
    assert.equal(app.byId('media-tab').classList.contains('selected'), true);
    assert.equal(app.byId('monitor-tab').classList.contains('selected'), false);
  });

  it('says the rig has the dashboard off while an active run shows no monitor', async () => {
    const app = await pageWith({ run: runDetail({ monitor: null }), dashboardEnabled: false });
    const note = app.byId('monitor-note');
    assert.equal(app.byId('monitor-frame').hidden, true);
    assert.match(note.textContent, /dashboard\.enabled: false/);
    assert.match(note.textContent, /rig YAML/);
    assert.match(app.byId('rig-summary').textContent, /live monitor: off/);
  });

  it('treats a rig without a dashboard block as off, the model default', async () => {
    const app = await pageWith({ run: runDetail({ monitor: null }), dashboardEnabled: null });
    assert.match(app.byId('monitor-note').textContent, /dashboard\.enabled: false/);
    assert.match(app.byId('rig-summary').textContent, /live monitor: off/);
  });

  it('waits for the monitor while the rig has the dashboard on', async () => {
    const app = await pageWith({ run: runDetail({ monitor: null }), dashboardEnabled: true });
    assert.match(app.byId('monitor-note').textContent, /waiting for the session/i);
    assert.match(app.byId('rig-summary').textContent, /live monitor: on/);
  });
});
