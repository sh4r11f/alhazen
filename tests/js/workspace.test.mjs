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
 * A page showing `project` (PROJECT by default) with `run` selected (or no
 * run), after one refresh: the project chosen, its rig and preset loaded,
 * the run drawn. `hash` is the opening URL's fragment, which carries the
 * token; `dashboardEnabled` is the rig's setting (null: no dashboard block).
 */
async function pageWith({ run = null, dashboardEnabled = true, hash, project = PROJECT } = {}) {
  const app = loadWorkspace({ hash: hash });
  app.server.state = {
    projects: [project],
    runs: run ? [summary(run)] : [],
    active: run && ACTIVE.includes(run.status) ? run.id : null,
  };
  app.server.details = run ? { [run.id]: run } : {};
  /* The run the fake server starts on POST /api/runs (id 'launched'), so the
   * page's refresh after a launch finds it as a real launcher's would. */
  app.server.details.launched = runDetail({ id: 'launched', log: '' });
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

describe('the media gallery', () => {
  it('builds media URLs that carry the token and encode each path segment', async () => {
    const image = { path: 'sub dir/im age#1.png', size: 2048, modified: 123, type: 'image/png' };
    const app = await pageWith({
      run: runDetail({ status: 'completed', returncode: 0, artifacts: [image] }),
      /* A token with a '/' in it, decoded from the fragment as 'tok/en'. */
      hash: '#token=tok%2Fen',
    });
    const cards = app.byId('gallery').children;
    assert.equal(cards.length, 1);
    const [media, caption] = cards[0].children;
    assert.equal(media.localName, 'img');
    /* The '/' between segments stays a separator; the space and '#' inside a
     * name are escaped, and so is the token — its '/' would otherwise begin
     * a new path segment and the server would look for another run. */
    assert.equal(media.src, '/media/r1/sub%20dir/im%20age%231.png?token=tok%2Fen&v=123');
    assert.equal(media.alt, image.path);
    const download = caption.querySelector('a');
    assert.equal(download.href, media.src);
    assert.equal(download.download, 'im age#1.png');
    assert.match(caption.textContent, /2 KB/);
    assert.equal(app.byId('media-count').textContent, '1');
    assert.equal(app.byId('gallery-empty').hidden, true);
  });

  it('defers a video while the run is active and shows it once the run has ended', async () => {
    const image = { path: 'frame.png', size: 10, modified: 1, type: 'image/png' };
    const video = { path: 'clip.mp4', size: 5 * 1048576, modified: 2, type: 'video/mp4' };
    const app = await pageWith({ run: runDetail({ artifacts: [image, video] }) });
    const gallery = app.byId('gallery');
    const shown = () => gallery.children.map((card) => card.children[0].localName);
    assert.deepEqual(shown(), ['img']);
    /* The count still says what the run has produced so far. */
    assert.equal(app.byId('media-count').textContent, '2');

    await redraw(app, runDetail({ status: 'completed', returncode: 0, artifacts: [image, video] }));
    assert.deepEqual(shown(), ['img', 'video']);
    const player = gallery.children[1].children[0];
    assert.equal(player.src, '/media/r1/clip.mp4?token=test-token&v=2');
    assert.equal(player.controls, true);
    assert.equal(player.preload, 'metadata');
    assert.match(gallery.children[1].textContent, /5\.0 MB/);
  });

  it('keeps the empty state visible while the only artifact is a deferred video', async () => {
    const video = { path: 'clip.mp4', size: 5, modified: 2, type: 'video/mp4' };
    const app = await pageWith({ run: runDetail({ artifacts: [video] }) });
    const empty = app.byId('gallery-empty');
    assert.equal(app.byId('gallery').children.length, 0);
    assert.equal(empty.hidden, false);
    assert.match(empty.textContent, /your experiment is working/i);
    /* And still on the next poll, when nothing in the gallery has changed. */
    await redraw(app, runDetail({ artifacts: [video], log: 'more output\n' }));
    assert.equal(empty.hidden, false);
  });
});

describe('the run history', () => {
  const older = runDetail({
    id: 'r1', mode: 'movie', status: 'completed', returncode: 0, started: '2026-09-25T09:00:00',
  });
  const newer = runDetail({
    id: 'r2', mode: 'simulate', status: 'failed', returncode: 1, started: '2026-09-25T10:00:00',
  });

  /** PROJECT with two finished runs, listed newest first as the server does. */
  async function historyPage() {
    const app = loadWorkspace();
    app.server.state = {
      projects: [PROJECT], runs: [summary(newer), summary(older)], active: null,
    };
    app.server.details = { r1: older, r2: newer };
    app.server.configs = {
      'configs/rig-mac.yaml': rig(true),
      'configs/task.yaml': { text: 'trials: 4\n', values: { trials: 4 } },
    };
    await app.run('refresh()');
    await settle();
    return app;
  }

  it('lists the runs with the selected one marked and a status badge on each', async () => {
    const app = await historyPage();
    const rows = app.byId('history').children;
    assert.equal(app.byId('history-count').textContent, '2 RUNS');
    assert.equal(rows.length, 2);
    /* The newest run is the one shown on arrival, so its row is selected. */
    assert.equal(rows[0].classList.contains('selected'), true);
    assert.equal(rows[1].classList.contains('selected'), false);
    const badge = (row) => row.querySelector('.status');
    assert.equal(badge(rows[0]).textContent, 'FAILED');
    assert.equal(badge(rows[0]).className, 'status failed');
    assert.equal(badge(rows[1]).textContent, 'COMPLETED');
    assert.equal(badge(rows[1]).className, 'status completed');
    assert.equal(rows[0].querySelector('strong').textContent, 'Simulate');
    assert.equal(rows[1].querySelector('strong').textContent, 'Record movies');
    assert.match(rows[1].textContent, /rig-mac\.yaml/);
    assert.equal(app.byId('run-status').textContent, 'FAILED');
    assert.equal(app.byId('run-status').className, 'status failed');
    assert.match(app.byId('run-info').textContent, /exit 1/);
  });

  it('selects the clicked run and redraws its output', async () => {
    const app = await historyPage();
    app.byId('history').children[1].fire('click');
    await settle();
    assert.equal(app.run('runId'), 'r1');
    const rows = app.byId('history').children;
    assert.equal(rows[0].classList.contains('selected'), false);
    assert.equal(rows[1].classList.contains('selected'), true);
    assert.ok(app.fetches.some((f) => f.url === '/api/runs/r1'));
    assert.equal(app.byId('run-status').textContent, 'COMPLETED');
    assert.match(app.byId('run-info').textContent, /Record movies/);
    assert.match(app.byId('run-info').textContent, /exit 0/);
  });
});

describe('launching a run', () => {
  /** The one POST /api/runs the page made, as the server received it. */
  function launched(app) {
    const posts = app.server.posted.filter((p) => p.path === '/api/runs');
    assert.equal(posts.length, 1);
    return posts[0].body;
  }

  /** Pick a mode as the reader would, so the form shows that mode's fields. */
  function chooseMode(app, mode) {
    app.byId('mode').value = mode;
    app.run('modeChanged()');
  }

  it('sends a simulate run: identity, seed, trials, its own flags and the edited parameters',
    async () => {
      const app = await pageWith();
      chooseMode(app, 'simulate');
      app.byId('subject').value = 's01';
      app.byId('session').value = '2';
      app.byId('seed').value = '7';
      app.byId('trials').value = '3';
      app.byId('scale').value = '0.5';
      /* Hidden for simulate: whatever they hold must not reach the server. */
      app.byId('mouse').checked = true;
      app.byId('script-args').value = '--ignored';
      await launch(app);
      assert.deepEqual(launched(app), {
        project: 'p', mode: 'simulate', rig: 'configs/rig-mac.yaml', subject: 's01', session: 2,
        seed: 7, trials: 3,
        /* headless and windowed start checked in the markup. */
        headless: true, mouse: false, windowed: true,
        scale: 0.5, sheet: false, columns: null, clips: [], script_args: '',
        parameters: { trials: 4 },
      });
      /* The token travels in a header, as JSON, and the page moved on to the
       * new run. */
      const post = app.fetches.find((f) => f.url === '/api/runs');
      assert.equal(post.init.method, 'POST');
      assert.equal(post.init.headers['X-Alhazen-Token'], 'test-token');
      assert.equal(post.init.headers['Content-Type'], 'application/json');
      assert.equal(app.run('runId'), 'launched');
    });

  it('sends a movie run with its scale, contact-sheet, column and clip options', async () => {
    const app = await pageWith();
    chooseMode(app, 'movie');
    app.byId('scale').value = '0.25';
    app.byId('sheet').checked = true;
    app.byId('columns').value = '3';
    app.byId('clips').value = ' fixation, target ,, ';
    await launch(app);
    const body = launched(app);
    assert.equal(body.mode, 'movie');
    assert.equal(body.scale, 0.25);
    assert.equal(body.sheet, true);
    assert.equal(body.columns, 3);
    assert.deepEqual(body.clips, ['fixation', 'target']);
    /* headless is simulate's, and a movie renders without a display. */
    assert.equal(body.headless, false);
    assert.equal(body.windowed, false);
    assert.deepEqual(body.parameters, { trials: 4 });
  });

  it('sends a discovered script its arguments, and no task parameters when it takes none',
    async () => {
      const script = {
        id: 'preview', label: 'Preview images', flags: ['--out', '--sheet'], params_flag: null,
      };
      const app = await pageWith({ project: { ...PROJECT, scripts: [script] } });
      chooseMode(app, 'preview');
      assert.equal(app.byId('script-options').hidden, false);
      /* --out is the launcher's own flag and is not offered for retyping. */
      assert.equal(app.byId('script-help').textContent, 'Available flags: --sheet');
      assert.equal(app.byId('task-parameters').hidden, true);
      app.byId('script-args').value = '--sheet';
      await launch(app);
      const body = launched(app);
      assert.equal(body.mode, 'preview');
      assert.equal(body.script_args, '--sheet');
      assert.equal(body.windowed, false);
      assert.equal('parameters' in body, false);
      assert.equal('parameters_yaml' in body, false);
    });
});

describe('the parameter text editor', () => {
  it('is labelled for what it shows: the values as JSON, which is also YAML', async () => {
    const app = await pageWith();
    assert.equal(app.byId('yaml-tab').textContent.trim(), 'Text (YAML or JSON)');
    /* Fields → text writes the edited values out as JSON. */
    app.run("$('launch-form').reportValidity = () => true");
    app.byId('yaml-tab').fire('click');
    await settle();
    assert.equal(app.run('editor'), 'yaml');
    assert.equal(app.byId('parameter-yaml').hidden, false);
    assert.equal(app.byId('parameter-fields').hidden, true);
    assert.equal(app.byId('parameter-yaml').value, JSON.stringify({ trials: 4 }, null, 2));
    assert.equal(app.byId('yaml-tab').getAttribute('aria-pressed'), 'true');
  });

  it('sends the text as written for the server to parse, and reads it back the same way',
    async () => {
      const app = await pageWith();
      app.byId('mode').value = 'simulate';
      app.run('modeChanged()');
      app.run("$('launch-form').reportValidity = () => true");
      app.byId('yaml-tab').fire('click');
      await settle();
      /* The reader types YAML that is not JSON; the page does not parse it. */
      app.byId('parameter-yaml').value = 'trials: 9\n';
      await launch(app);
      const body = app.server.posted.find((p) => p.path === '/api/runs').body;
      assert.equal(body.parameters_yaml, 'trials: 9\n');
      assert.equal('parameters' in body, false);
      /* Text → fields asks the server, whose YAML errors are the ones a launch
       * would raise (the fake accepts JSON, a subset of YAML). */
      app.byId('parameter-yaml').value = '{"trials": 12}';
      app.byId('fields-tab').fire('click');
      await settle();
      const parsed = app.server.posted.find((p) => p.path === '/api/parameters');
      assert.equal(parsed.body.text, '{"trials": 12}');
      assert.deepEqual(plain(app.run('values')), { trials: 12 });
      assert.equal(app.run('editor'), 'fields');
    });
});

describe('a request the launcher refuses', () => {
  it('shows the server’s message in the banner and gives the launch button back', async () => {
    const app = await pageWith();
    app.byId('mode').value = 'simulate';
    app.run('modeChanged()');
    app.server.reject = (url, init) =>
      (url === '/api/runs' && init.method === 'POST'
        ? response({ error: 'Rig file is missing' }, 400)
        : undefined);
    await launch(app);
    const banner = app.byId('error');
    assert.equal(banner.hidden, false);
    assert.equal(banner.textContent, 'Rig file is missing');
    assert.equal(app.run('launching'), false);
    assert.equal(app.byId('launch').disabled, false);
    assert.equal(app.run('runId'), null);
    /* A refusal without a message still says what happened. */
    app.server.reject = (url) => (url === '/api/runs' ? response({}, 503) : undefined);
    await launch(app);
    assert.equal(banner.textContent, 'Request failed (503)');
  });

  it('reports a failed poll, keeps polling, and clears the banner once the server is back',
    async () => {
      const app = await pageWith();
      app.server.reject = (url) =>
        (url === '/api/state' ? response({ error: 'gone' }, 500) : undefined);
      await app.run('poll()');
      await settle();
      assert.equal(app.byId('connection').textContent, 'Connection unavailable');
      assert.equal(app.byId('error').hidden, false);
      assert.equal(app.byId('error').textContent, 'gone');
      assert.deepEqual(plain(app.timers.map((timer) => timer.ms)), [1500]);

      app.server.reject = () => undefined;
      await app.run('poll()');
      await settle();
      assert.equal(app.byId('error').hidden, true);
      assert.equal(app.byId('connection').textContent, 'Connected to localhost');
    });
});
