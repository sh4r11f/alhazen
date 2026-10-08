/* The experiment workspace page: what it draws for a run and what it sends
 * when a run is launched. workspace_parameters.test.mjs covers the parameter
 * editor; this file covers the rest of workspace.js.
 *
 * Run: node --test "tests/js/*.test.mjs"
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';

import { loadWorkspace, plain, response, settle } from './load_workspace.mjs';

/* One of a project's `rigs` as the launcher describes it (Workspace.describe):
 * an experiment rig by default, with its project-relative path. */
function rigEntry(name, overrides = {}) {
  return {
    name: name, source: 'experiment', path: `configs/rig-${name}.yaml`,
    shadowed: false, extends: null, ...overrides,
  };
}

/* A shared rig as the launcher lists it: the absolute file the project's
 * alhazen ships. */
function sharedEntry(name, overrides = {}) {
  return rigEntry(name, {
    source: 'alhazen', path: `C:/env/alhazen/rigs/rig-${name}.yaml`, ...overrides,
  });
}

/* One registered project with one rig, one parameter file and no scripts.
 * `name` is the folder the registry recorded; `title` and `slug` are what
 * its pyproject.toml says ([tool.alhazen] title, [project] name). */
const PROJECT = Object.freeze({
  id: 'p', name: 'demo-folder', title: 'Demo task', slug: 'demo', title_error: null,
  path: 'C:/projects/demo', python: 'python', available: true,
  rigs: [rigEntry('mac')], rigs_note: null, configs: ['configs/task.yaml'], scripts: [],
  /* The Task parameters menu as Workspace.describe derives it for a run.py
   * with one task and no PARAMETERS: each file by its short name. */
  parameter_sets: [{ label: 'task', task: null, params: 'configs/task.yaml' }],
  default_parameter_set: 'task', parameter_sets_error: null,
});

/* A rig as /api/rig returns it: its name, whose it is, the shared rig it
 * extends and its settings, merged. `null` leaves the dashboard block out, as
 * a rig written before the dashboard existed would; the model's default is
 * then off. (Not `undefined`: pageWith's destructuring default would turn
 * that into true.) */
function rig(dashboardEnabled, oldKey = false, about = {}) {
  const values = {
    monitor: {
      width_px: 1920, height_px: 1080, refresh_rate_hz: 60, width_cm: 52, distance_cm: 57,
    },
  };
  /* `oldKey`: the section under its pre-1.9 name `dashboard:`, as a project
   * whose own alhazen is older than 2.0 may still spell it. */
  if (dashboardEnabled !== null) {
    values[oldKey ? 'dashboard' : 'live_monitor'] = { enabled: dashboardEnabled };
  }
  return { name: 'mac', source: 'experiment', extends: null, values: values, ...about };
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
 * `configs` adds presets beyond task.yaml (by path, as /api/config answers),
 * `rigs` rigs beyond the project's mac (by menu value, as /api/rig answers)
 * and `schemas` the per-task schemas of a project with a task table.
 * `storage` is localStorage as the page finds it when it loads, `search` the
 * address's query (default: the Run page of the experiment used last) and
 * `people` the experiment's registered subjects and experimenters.
 */
async function pageWith({
  run = null, dashboardEnabled = true, oldKey = false, hash, project = PROJECT,
  configs = {}, rigs = {}, schemas = {}, storage, search, people,
} = {}) {
  const app = loadWorkspace({ hash: hash, storage: storage, search: search });
  if (people) app.server.people = people;
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
    'configs/task.yaml': { text: 'trials: 4\n', values: { trials: 4 } },
    ...configs,
  };
  app.server.rigs = { 'configs/rig-mac.yaml': rig(dashboardEnabled, oldKey), ...rigs };
  app.server.schemas = schemas;
  await app.run('refresh()');
  await settle();
  return app;
}

/** The rig summary under the Rig menu as {label: value}: a list of labels
 *  and values since the owner asked for one (it was lines of monospace
 *  text), read the way a person reads it. */
function facts(app) {
  const list = app.byId('rig-summary').children[0];
  const found = {};
  for (let i = 0; i < list.children.length; i += 2) {
    found[list.children[i].textContent] = list.children[i + 1].textContent;
  }
  return found;
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

/** The URLs of every /api/schema request the page made, in order. */
function schemaRequests(app) {
  return app.fetches.map((f) => f.url).filter((url) => url.startsWith('/api/schema'));
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
    /* The saved page under the name alhazen 2.0 writes, and the name a run
     * recorded by an older alhazen has: the history holds both kinds. */
    assert.match(note.textContent, /figures\/live_monitor\.html \(figures\/dashboard\.html by an alhazen before 2\.0\)/);
    /* Nor is a link to a dead server offered. */
    assert.equal(app.byId('monitor').hidden, true);
  });

  it('opens when its tab is clicked, like the Media and Console tabs', async () => {
    const app = await pageWith();
    app.byId('monitor-tab').fire('click');
    assert.equal(app.byId('monitor-panel').hidden, false);
    assert.equal(app.byId('media-panel').hidden, true);
    assert.equal(app.byId('monitor-tab').classList.contains('selected'), true);
    app.byId('media-tab').fire('click');
    assert.equal(app.byId('monitor-panel').hidden, true);
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
    assert.match(note.textContent, /live_monitor\.enabled: false/);
    assert.match(note.textContent, /rig YAML/);
    assert.equal(facts(app)['Live monitor'], 'off');
  });

  it('treats a rig without a dashboard block as off, the model default', async () => {
    const app = await pageWith({ run: runDetail({ monitor: null }), dashboardEnabled: null });
    assert.match(app.byId('monitor-note').textContent, /live_monitor\.enabled: false/);
    assert.equal(facts(app)['Live monitor'], 'off');
  });

  it('waits for the monitor while the rig has the dashboard on', async () => {
    const app = await pageWith({ run: runDetail({ monitor: null }), dashboardEnabled: true });
    assert.match(app.byId('monitor-note').textContent, /waiting for the session/i);
    assert.equal(facts(app)['Live monitor'], 'on');
  });

  it('reads the pre-1.9 `dashboard:` rig section as the monitor setting', async () => {
    /* A project still on alhazen 1.x (1.8 knows only this spelling; 1.9 and
     * 1.10 read either): its session will bring a monitor, so the page must
     * wait for it rather than declare the rig has it off. alhazen 2.0 refuses
     * the section, but the rig is read by the project's alhazen, not the
     * workspace's, so this stays for as long as such projects are launched. */
    const on = await pageWith({ run: runDetail({ monitor: null }), dashboardEnabled: true, oldKey: true });
    assert.match(on.byId('monitor-note').textContent, /waiting for the session/i);
    assert.equal(facts(on)['Live monitor'], 'on');
    const off = await pageWith({ run: runDetail({ monitor: null }), dashboardEnabled: false, oldKey: true });
    assert.equal(facts(off)['Live monitor'], 'off');
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
      'configs/task.yaml': { text: 'trials: 4\n', values: { trials: 4 } },
    };
    app.server.rigs = { 'configs/rig-mac.yaml': rig(true) };
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
    /* The rig by its qualified name, not its file: this record predates
     * runs keeping the name, so the page takes it from rig-mac.yaml, and
     * the experiment's slug is its owner (was the bare "mac" before the
     * owner asked for qualified names). */
    assert.match(rows[1].textContent, / · demo\/mac/);
    assert.doesNotMatch(rows[1].textContent, /rig-mac|\.yaml/);
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
  it('sends a simulate run: identity, seed, trials, its own flags and the edited parameters',
    async () => {
      const app = await pageWith();
      chooseMode(app, 'simulate');
      app.byId('subject').value = 's01';
      app.byId('session').value = '2';
      app.byId('seed').value = '7';
      app.byId('trials').value = '3';
      app.byId('scale').value = '0.5';
      /* Hidden for simulate: whatever it holds must not reach the server. */
      app.byId('mouse').checked = true;
      await launch(app);
      assert.deepEqual(launched(app), {
        /* task is null: this project's run.py declares one task the old way. */
        project: 'p', mode: 'simulate', task: null, rig: 'configs/rig-mac.yaml', subject: 's01',
        /* Initials are optional in simulate; left blank, they are sent blank. */
        initials: '',
        session: 2, seed: 7, trials: 3,
        /* headless and windowed start checked in the markup. */
        headless: true, mouse: false, windowed: true,
        scale: 0.5, sheet: false, columns: null, clips: [], extra_args: '',
        /* The Task parameters entry it was launched from, for the history. */
        parameter_set: 'task',
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
      /* The field is the script's here, not run.py's, and says so. */
      assert.equal(app.byId('extra-label').textContent, 'Extra script arguments');
      /* --out is the launcher's own flag and is not offered for retyping. */
      assert.equal(app.byId('extra-help').textContent, 'Available flags: --sheet');
      assert.equal(app.byId('task-parameters').hidden, true);
      app.byId('extra-args').value = '--sheet';
      await launch(app);
      const body = launched(app);
      assert.equal(body.mode, 'preview');
      assert.equal(body.extra_args, '--sheet');
      assert.equal(body.windowed, false);
      assert.equal('parameters' in body, false);
      assert.equal('parameters_yaml' in body, false);
    });

  it('offers extra run.py arguments for a mode too, and sends what was typed', async () => {
    /* An experiment that ships several tasks needs its own --task before
     * run_experiment sees the rest; the field used to be a script's alone. */
    const app = await pageWith();
    chooseMode(app, 'simulate');
    assert.equal(app.byId('extra-label').textContent, 'Extra run.py arguments');
    const help = app.byId('extra-help').textContent;
    assert.match(help, /after the launcher’s own flags/);
    assert.match(help, /--task mib-detect/);
    assert.match(help, /--curriculum configs\/shaping\.yaml/);
    app.byId('subject').value = 's01';
    app.byId('extra-args').value = '--task mib-detect';
    await launch(app);
    const body = launched(app);
    assert.equal(body.mode, 'simulate');
    assert.equal(body.extra_args, '--task mib-detect');
    /* Switching to another project starts the field empty again: the
     * arguments were that experiment's. */
    app.run("$('extra-args').value = '--task other'");
    await app.run("chooseProject('p')");
    await settle();
    assert.equal(app.byId('extra-args').value, '');
  });
});

describe('choosing a Task parameters entry', () => {
  /* An experiment whose run.py declares two tasks and names its Task
   * parameters menu (PARAMETERS): each entry is a file AND the task it runs,
   * so there is no Task menu (the owner's request, 2026-10-06). Listed out
   * of order, as run.py may; the menu sorts them by name. */
  const TASKED = Object.freeze({
    ...PROJECT,
    tasks: [
      { name: 'mt-tuning', params: 'configs/task-tuning.yaml' },
      { name: 'mib-search', params: 'configs/task-search-rdk.yaml' },
    ],
    default_task: 'mib-search',
    configs: ['configs/task.yaml', 'configs/task-tuning.yaml', 'configs/task-search-rdk.yaml'],
    parameter_sets: [
      { label: 'Tuning', task: 'mt-tuning', params: 'configs/task-tuning.yaml' },
      { label: 'Search (RDK)', task: 'mib-search', params: 'configs/task-search-rdk.yaml' },
      { label: 'Search (base)', task: 'mib-search', params: 'configs/task.yaml' },
    ],
    default_parameter_set: 'Search (RDK)',
  });
  /* Each file and each task's model. The files hold one string each and the
   * schemas give that string a choice list, so the fields editor shows a
   * dropdown whose options say which task's model it was drawn from. */
  const CONFIGS = {
    'configs/task-tuning.yaml': { text: 'speed: fast\n', values: { speed: 'fast' } },
    'configs/task-search-rdk.yaml': { text: 'motion: moving\n', values: { motion: 'moving' } },
    'configs/task.yaml': { text: 'motion: static\n', values: { motion: 'static' } },
  };
  const SCHEMAS = {
    'mt-tuning': { properties: { speed: { enum: ['slow', 'fast'], default: 'slow' } } },
    'mib-search': { properties: { motion: { enum: ['static', 'moving'], default: 'static' } } },
  };

  /** A page on TASKED, its files and schemas served. */
  function taskedPage(options = {}) {
    return pageWith({ project: TASKED, configs: CONFIGS, schemas: SCHEMAS, ...options });
  }

  /** Choose a Task parameters entry as the reader would; let the loads finish. */
  async function chooseSet(app, label) {
    app.byId('params-config').value = label;
    app.byId('params-config').fire('change');
    await settle();
  }

  it('has no Task menu, and asks for the one schema of a project without a task table',
    async () => {
      const app = await pageWith();
      assert.equal(app.byId('task'), null);
      assert.equal(app.byId('task-field'), null);
      /* No `task` in the query: the server refuses one for such a project. */
      assert.deepEqual(schemaRequests(app), ['/api/schema?project=p']);
      assert.equal(app.byId('params-config').value, 'task');
      /* No task to name under the menu. */
      assert.equal(app.byId('parameter-set-help').hidden, true);
    });

  it('lists the entries by name, opens on the default task’s own file and asks that task’s '
    + 'schema', async () => {
    const app = await taskedPage();
    const select = app.byId('params-config');
    assert.deepEqual(select.children.map((o) => o.value),
      ['Search (base)', 'Search (RDK)', 'Tuning']);
    assert.deepEqual(select.children.map((o) => o.textContent),
      ['Search (base)', 'Search (RDK)', 'Tuning']);
    assert.equal(select.value, 'Search (RDK)');
    assert.equal(app.byId('parameter-set-help').textContent,
      'Runs the task mib-search with configs/task-search-rdk.yaml.');
    assert.deepEqual(schemaRequests(app), ['/api/schema?project=p&task=mib-search']);
    /* The editor shows that file's values with that model's choices. */
    const field = app.byId('param-0');
    assert.equal(field.localName, 'select');
    assert.deepEqual(field.children.map((o) => o.value), ['static', 'moving']);
    assert.equal(field.value, 'moving');
    assert.equal(app.byId('launch').disabled, false);
  });

  it('reloads the file, schema and fields when an entry for another task is chosen',
    async () => {
      const app = await taskedPage();
      await chooseSet(app, 'Tuning');
      assert.deepEqual(schemaRequests(app), [
        '/api/schema?project=p&task=mib-search',
        '/api/schema?project=p&task=mt-tuning',
      ]);
      /* The fields are the new task's: its file's value, its model's choices. */
      const field = app.byId('param-0');
      assert.equal(field.localName, 'select');
      assert.deepEqual(field.children.map((o) => o.value), ['slow', 'fast']);
      assert.equal(field.value, 'fast');
      assert.deepEqual(plain(app.run('values')), { speed: 'fast' });
      assert.equal(app.byId('parameter-set-help').textContent,
        'Runs the task mt-tuning with configs/task-tuning.yaml.');
      assert.equal(app.byId('launch').disabled, false);
    });

  it('reloads only the file when another entry for the same task is chosen', async () => {
    const app = await taskedPage();
    await chooseSet(app, 'Search (base)');
    assert.deepEqual(schemaRequests(app), ['/api/schema?project=p&task=mib-search']);
    assert.deepEqual(plain(app.run('values')), { motion: 'static' });
  });

  it('runs an entry with no file on its task’s defaults, and one naming a missing file says so',
    async () => {
      const project = {
        ...TASKED,
        parameter_sets: [
          { label: 'Check', task: 'mt-tuning', params: null },
          { label: 'Search (RDK)', task: 'mib-search', params: null, missing: 'configs/gone.yaml' },
        ],
        default_parameter_set: 'Search (RDK)',
      };
      const app = await taskedPage({ project: project });
      assert.equal(app.run('values'), null);
      assert.match(app.byId('parameter-fields').textContent, /configs\/gone\.yaml/);
      assert.equal(app.byId('parameter-set-help').textContent,
        'Runs the task mib-search on the defaults in its code.');
      await chooseSet(app, 'Check');
      assert.equal(app.run('values'), null);
      assert.equal(
        app.byId('parameter-fields').textContent,
        'Check has no parameter file: mt-tuning runs on the defaults in its code, and launches '
        + 'without --params.',
      );
      assert.equal(app.byId('parameters-help').hidden, true);
      assert.equal(app.fetches.filter((f) => f.url.startsWith('/api/config')).length, 0);
      chooseMode(app, 'movie');
      await launch(app);
      assert.equal(launched(app).task, 'mt-tuning');
      assert.equal('parameters' in launched(app), false);
    });

  it('sends the chosen entry’s task and label with a built-in mode, and neither with a script',
    async () => {
      const script = {
        id: 'preview', label: 'Preview images', flags: ['--out', '--sheet'], params_flag: null,
      };
      const app = await taskedPage({ project: { ...TASKED, scripts: [script] } });
      await chooseSet(app, 'Tuning');
      chooseMode(app, 'simulate');
      /* The help does not suggest --task: the entry names the task. */
      const help = app.byId('extra-help').textContent;
      assert.doesNotMatch(help, /--task/);
      assert.match(help, /--curriculum configs\/shaping\.yaml/);
      assert.match(help, /Task parameters entry/);
      assert.doesNotMatch(app.byId('extra-args').placeholder, /--task/);
      app.byId('subject').value = 's01';
      await launch(app);
      const body = launched(app);
      assert.equal(body.mode, 'simulate');
      assert.equal(body.task, 'mt-tuning');
      assert.equal(body.parameter_set, 'Tuning');
      assert.deepEqual(body.parameters, { speed: 'fast' });

      /* A script launch: the server refuses a task on a script, so none is
       * sent, and no entry either. */
      app.server.posted.length = 0;
      chooseMode(app, 'preview');
      await launch(app);
      assert.equal(launched(app).mode, 'preview');
      assert.equal(launched(app).task, null);
      assert.equal(launched(app).parameter_set, null);
    });

  it('takes the declared stimuli out of the task’s hands: no parameters, no task', async () => {
    /* What the server describes for an experiment that declares its stimuli
     * ([tool.alhazen] stimuli): alhazen's own Preview images. */
    const declared = {
      id: 'alhazen.preview', label: 'Preview images', module: 'alhazen',
      flags: ['--out', '--project', '--rig'], params_flag: null, rig_flag: true,
      task_free: true, error: null,
    };
    const app = await taskedPage({ project: { ...TASKED, scripts: [declared] } });
    /* The menu is sorted by name now, and still opens on Preview images. */
    assert.equal(app.byId('mode').value, 'alhazen.preview');
    chooseMode(app, 'alhazen.preview');
    assert.equal(app.byId('task-parameters').hidden, true);
    /* The flags the launcher sets are not offered for retyping. */
    assert.equal(app.byId('extra-help').textContent, 'Available flags: none');
    await launch(app);
    const body = launched(app);
    assert.equal(body.mode, 'alhazen.preview');
    assert.equal(body.task, null);
    assert.equal(body.parameters ?? null, null);
    assert.equal(body.parameters_yaml ?? null, null);
    /* Back on a mode that runs a task, its parameters are offered again. */
    chooseMode(app, 'simulate');
    assert.equal(app.byId('task-parameters').hidden, false);
  });

  it('names the entry after the mode in the history, and the task for an older record',
    async () => {
      const run = runDetail({
        task: 'mt-tuning', parameter_set: 'Tuning', status: 'completed', returncode: 0,
      });
      const app = await taskedPage({ run: run });
      const row = app.byId('history').children[0];
      assert.equal(row.querySelector('strong').textContent, 'Simulate · Tuning');
      assert.match(app.byId('run-info').textContent, /^Simulate · Tuning · /);
      /* A run recorded before entries had labels keeps its task's name. */
      const older = runDetail({ task: 'mt-tuning', status: 'completed', returncode: 0 });
      const before = await taskedPage({ run: older });
      assert.equal(before.byId('history').children[0].querySelector('strong').textContent,
        'Simulate · mt-tuning');
    });

  it('shows why the task table could not be read and offers no launch', async () => {
    const message = 'TASKS in run.py is not a dict literal';
    const broken = { ...PROJECT, tasks: [], default_task: null, tasks_error: message };
    const app = await pageWith({ project: broken });
    assert.equal(app.byId('parameter-set-help').hidden, false);
    assert.equal(app.byId('parameter-set-help').textContent, message);
    assert.equal(app.byId('launch').disabled, true);
    assert.match(app.byId('launch-note').textContent, /run\.py/);
    assert.match(app.byId('launch-note').textContent, new RegExp(message));
    /* No task table, so no task in the schema request either. */
    assert.deepEqual(schemaRequests(app), ['/api/schema?project=p']);
    /* Enter in a field submits past the disabled button; nothing is posted. */
    chooseMode(app, 'simulate');
    assert.equal(app.byId('launch').disabled, true);
    await launch(app);
    assert.equal(app.server.posted.filter((p) => p.path === '/api/runs').length, 0);
  });

  it('says when run.py’s PARAMETERS could not be read, and still offers the derived entries',
    async () => {
      const project = {
        ...TASKED,
        parameter_sets_error: "run.py's PARAMETERS['Main'] names a task that is not one of TASKS",
      };
      const app = await taskedPage({ project: project });
      assert.match(app.byId('parameter-set-help').textContent,
        /PARAMETERS could not be read.*names a task that is not one of TASKS/);
      assert.equal(app.byId('launch').disabled, false);
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

describe('the subject’s initials', () => {
  it('are required where the subject is: run and test, not simulate', async () => {
    const app = await pageWith();
    for (const [mode, required] of [['run', true], ['test', true], ['simulate', false]]) {
      chooseMode(app, mode);
      assert.equal(app.byId('initials').required, required, mode);
      assert.equal(app.byId('identity').hidden, false, mode);
    }
    /* A mode that names no subject hides the whole row. */
    chooseMode(app, 'movie');
    assert.equal(app.byId('identity').hidden, true);
    assert.equal(app.byId('initials').required, false);
  });

  it('are sent trimmed and uppercase, as run.py records them', async () => {
    const app = await pageWith();
    chooseMode(app, 'run');
    app.byId('subject').value = 's01';
    app.byId('initials').value = ' hd ';
    await launch(app);
    assert.equal(launched(app).mode, 'run');
    assert.equal(launched(app).initials, 'HD');
  });

  it('that break the rule are refused in the command line’s words, and nothing is sent',
    async () => {
      const app = await pageWith();
      chooseMode(app, 'test');
      app.byId('subject').value = 's01';
      app.byId('initials').value = 'H1';
      await launch(app);
      assert.equal(app.server.posted.filter((p) => p.path === '/api/runs').length, 0);
      assert.equal(app.byId('error').hidden, false);
      assert.equal(
        app.byId('error').textContent, "initials must be 1 to 5 letters, such as HD; got 'H1'",
      );
      /* The reader can fix the field and try again. */
      assert.equal(app.run('launching'), false);
      assert.equal(app.byId('launch').disabled, false);
    });

  it('left out of a run are asked for, and nothing is sent', async () => {
    const app = await pageWith();
    chooseMode(app, 'run');
    app.byId('subject').value = 's01';
    await launch(app);
    assert.equal(app.server.posted.filter((p) => p.path === '/api/runs').length, 0);
    assert.equal(
      app.byId('error').textContent, 'Subject initials are required for run and test modes',
    );
  });

  it('are not checked or sent for a mode that names no subject', async () => {
    const app = await pageWith();
    chooseMode(app, 'run');
    app.byId('initials').value = '12345';  /* left over from another mode */
    chooseMode(app, 'movie');
    await launch(app);
    assert.equal(launched(app).mode, 'movie');
    assert.equal(launched(app).initials, '');
  });

  it('show who a run was for in the history and the run summary', async () => {
    const run = runDetail({
      mode: 'run', subject: 's01', session: 1, initials: 'HD', status: 'completed',
      returncode: 0,
    });
    const app = await pageWith({ run: run });
    const row = app.byId('history').children[0];
    /* The seed follows who (2.3.0: the history shows the seed each session
     * ran with; this run passed none, and its console has not said which it
     * drew). */
    assert.match(
      row.querySelector('small').textContent, / · demo\/mac · sub-s01 · HD · seed new$/,
    );
    assert.match(app.byId('run-info').textContent, /^Run experiment · sub-s01 · HD · /);
  });

  it('are left out of a run that names no subject', async () => {
    const app = await pageWith({ run: runDetail({ status: 'completed', returncode: 0 }) });
    assert.doesNotMatch(app.byId('history').children[0].textContent, /sub-/);
    assert.doesNotMatch(app.byId('run-info').textContent, /sub-/);
  });
});

describe('the random seed', () => {
  /* The field used to start at 0 and every launch sent --seed 0, so every
   * session started from the workspace drew the same trial order, jitters
   * and block order. The command line draws a fresh seed when given none;
   * the workspace now does what it does. */

  it('starts empty, says a new seed is drawn each run, and sends none', async () => {
    const app = await pageWith();
    chooseMode(app, 'simulate');
    assert.equal(app.byId('seed').value, '');
    assert.equal(app.byId('seed').placeholder, 'new each run');
    assert.match(app.byId('seed-help').textContent, /draw a new one for every run/);
    assert.match(app.byId('seed-help').textContent, /records the seed it drew/);
    await launch(app);
    assert.equal(launched(app).seed, null);
  });

  it('sends a typed seed, to repeat a session', async () => {
    const app = await pageWith();
    chooseMode(app, 'run');
    app.byId('subject').value = 's01';
    app.byId('initials').value = 'HD';
    app.byId('seed').value = ' 2718281828 ';
    await launch(app);
    assert.equal(launched(app).seed, 2718281828);
  });

  it('refuses anything but a whole number of 0 or more, and sends nothing', async () => {
    for (const typed of ['1.5', '-3', '1e3', '99999999999999999999']) {
      const app = await pageWith();
      chooseMode(app, 'simulate');
      app.byId('seed').value = typed;
      await launch(app);
      assert.equal(app.server.posted.filter((p) => p.path === '/api/runs').length, 0, typed);
      assert.equal(
        app.byId('error').textContent,
        'The random seed must be a whole number of 0 or more, or empty for a new one; '
          + `got '${typed}'`,
        typed,
      );
      assert.equal(app.run('launching'), false);
    }
  });

  it('refuses what a browser could not read as a number, rather than drawing a new seed',
    async () => {
      /* A number field holding text it cannot read reports '' and flags it;
       * read as empty, it would launch a new seed nobody asked for. */
      const app = await pageWith();
      chooseMode(app, 'simulate');
      app.byId('seed').value = '';
      app.byId('seed').validity = { badInput: true };
      await launch(app);
      assert.equal(app.server.posted.filter((p) => p.path === '/api/runs').length, 0);
      assert.equal(
        app.byId('error').textContent,
        'The random seed must be a whole number of 0 or more, or empty for a new one',
      );
    });

  it('says demo and movie use seed 0 when it is empty', async () => {
    const app = await pageWith();
    for (const mode of ['demo', 'movie']) {
      chooseMode(app, mode);
      assert.equal(app.byId('seed-fields').hidden, false, mode);
      assert.equal(app.byId('seed').placeholder, '0', mode);
      assert.match(app.byId('seed-help').textContent, /empty for seed 0/, mode);
    }
    await launch(app);
    assert.equal(launched(app).seed, null);
  });

  it('is hidden and sent as none for measure and for a script', async () => {
    const script = {
      id: 'preview', label: 'Preview images', flags: ['--out'], params_flag: null,
    };
    for (const mode of ['measure', 'preview']) {
      const app = await pageWith({ project: { ...PROJECT, scripts: [script] } });
      chooseMode(app, 'simulate');
      app.byId('seed').value = '5';  /* typed for another mode, then left */
      chooseMode(app, mode);
      assert.equal(app.byId('seed-fields').hidden, true, mode);
      assert.equal(app.byId('seed').disabled, true, mode);
      await launch(app);
      assert.equal(launched(app).seed, null, mode);
    }
  });

  it('is shown in the history and the run summary, or "new" until a session says it',
    async () => {
      const drawn = runDetail({ id: 'r3', seed: 2718281828, started: '2026-09-25T11:00:00' });
      const waiting = runDetail({ id: 'r2', seed: null, started: '2026-09-25T10:30:00' });
      /* A movie on its default seed: nothing drawn, nothing to show. */
      const movie = runDetail({
        id: 'r1', mode: 'movie', seed: null, status: 'completed', returncode: 0,
        started: '2026-09-25T10:00:00',
      });
      const app = loadWorkspace();
      app.server.state = {
        projects: [PROJECT], runs: [drawn, waiting, movie].map(summary), active: 'r3',
      };
      app.server.details = { r1: movie, r2: waiting, r3: drawn };
      app.server.configs = { 'configs/task.yaml': { text: 'trials: 4\n', values: { trials: 4 } } };
      app.server.rigs = { 'configs/rig-mac.yaml': rig(true) };
      await app.run('refresh()');
      await settle();
      const rows = app.byId('history').children.map((row) => row.querySelector('small'));
      assert.match(rows[0].textContent, / · seed 2718281828$/);
      assert.match(rows[1].textContent, / · seed new$/);
      assert.doesNotMatch(rows[2].textContent, /seed/);
      assert.match(app.byId('run-info').textContent, /^Simulate · .* · seed 2718281828$/);
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

describe('the Rig menu', () => {
  /* An experiment like amodal-averaging after it moved to shared rigs: its
   * own lab extends alhazen's lab (and so hides it from `--rig lab`), its own
   * laptop is a whole file, and its alhazen ships lab, mac and vpixx. */
  const RIGGED = Object.freeze({
    ...PROJECT,
    rigs: [
      rigEntry('lab', { extends: 'lab' }),
      rigEntry('laptop'),
      sharedEntry('lab', { shadowed: true }),
      sharedEntry('mac'),
      sharedEntry('vpixx'),
    ],
  });
  /* What /api/rig answers for each of them: the merged rig. */
  const RIGS = {
    'configs/rig-lab.yaml': rig(true, false, { name: 'lab', extends: 'lab' }),
    'configs/rig-laptop.yaml': rig(false, false, { name: 'laptop' }),
    'alhazen/lab': rig(true, false, { name: 'lab', source: 'alhazen' }),
    'alhazen/mac': rig(false, false, { name: 'mac', source: 'alhazen' }),
    'alhazen/vpixx': rig(true, false, { name: 'vpixx', source: 'alhazen' }),
  };

  /** The menu as groups: [label, [[value, text], ...]], in order. */
  function menu(app) {
    return app.byId('rig').children.map((group) => [
      group.label, group.children.map((option) => [option.value, option.textContent]),
    ]);
  }

  /** Choose rig `value` as the reader would, and let the summary load. */
  async function chooseRig(app, value) {
    app.byId('rig').value = value;
    app.byId('rig').fire('change');
    await settle();
  }

  it('names each rig with its owner, never its file, in a group for the experiment and one '
    + 'for alhazen, leaving out the shared rig the experiment hides', async () => {
    const app = await pageWith({ project: RIGGED, rigs: RIGS });
    /* Changed at the owner's request: every rig is <owner>/<name> (the
     * experiment's slug for its own, alhazen for a shared one), and the
     * shared lab — hidden from `--rig lab` by the experiment's own lab — is
     * no longer offered at all (it used to be "alhazen/lab (hidden by this
     * experiment’s lab)"). The shared rig an experiment rig extends is said
     * in the summary, not the menu, whose options it made too long. */
    assert.deepEqual(plain(menu(app)), [
      ['This experiment', [
        ['configs/rig-lab.yaml', 'demo/lab'],
        ['configs/rig-laptop.yaml', 'demo/laptop'],
      ]],
      ['Shared (alhazen)', [
        ['alhazen/mac', 'alhazen/mac'],
        ['alhazen/vpixx', 'alhazen/vpixx'],
      ]],
    ]);
    for (const [, options] of menu(app)) {
      for (const [, text] of options) assert.doesNotMatch(text, /rig-|\.ya?ml/);
    }
    /* Left out without a note (the owner's call): only a registration that
     * predates shared rigs is explained under the menu. */
    assert.equal(app.byId('rig-note').hidden, true);
  });

  it('leaves out every shared rig the experiment hides, and the group they were in',
    async () => {
      const app = await pageWith({
        project: {
          ...RIGGED,
          rigs: [...RIGGED.rigs, rigEntry('vpixx'), rigEntry('mac')].map((r) => (
            r.source === 'alhazen' ? { ...r, shadowed: true } : r)),
        },
        rigs: RIGS,
      });
      assert.deepEqual(plain(menu(app).map(([label]) => label)), ['This experiment']);
      assert.equal(app.byId('rig-note').hidden, true);
    });

  it('opens on the experiment’s own laptop, else the shared laptop, else the first rig',
    async () => {
      /* Changed at the owner's request (2026-10-02): the menu used to open
       * on the experiment's own mac, else the shared mac. It opens on the
       * laptop now, and a mac is no longer preferred to any other rig. */
      const withoutOwnLaptop = RIGGED.rigs.filter((r) => r.name !== 'laptop');
      /* The experiment's own laptop, which hides alhazen's laptop of the same
       * name: the shared one is left out of the menu, and is not chosen. */
      const own = await pageWith({
        project: { ...RIGGED, rigs: [...RIGGED.rigs, sharedEntry('laptop', { shadowed: true })] },
        rigs: RIGS,
      });
      assert.equal(own.byId('rig').value, 'configs/rig-laptop.yaml');
      assert.ok(!menu(own).flatMap(([, options]) => options).some(([v]) => v === 'alhazen/laptop'));
      /* No laptop of its own: alhazen's shared laptop, though the shared mac
       * and the experiment's own rigs are listed before it. */
      const shared = await pageWith({
        project: { ...RIGGED, rigs: [...withoutOwnLaptop, sharedEntry('laptop')] },
        rigs: { ...RIGS, 'alhazen/laptop': rig(false, false, { name: 'laptop', source: 'alhazen' }) },
      });
      assert.equal(shared.byId('rig').value, 'alhazen/laptop');
      /* No laptop at all: the first rig listed — the experiment's lab, not
       * the shared mac the menu used to prefer. */
      const neither = await pageWith({
        project: { ...RIGGED, rigs: withoutOwnLaptop }, rigs: RIGS,
      });
      assert.equal(neither.byId('rig').value, 'configs/rig-lab.yaml');
      /* And with only shared rigs, the first of them. */
      const onlyShared = await pageWith({
        project: { ...RIGGED, rigs: [sharedEntry('vpixx'), sharedEntry('mac')] }, rigs: RIGS,
      });
      assert.equal(onlyShared.byId('rig').value, 'alhazen/vpixx');
    });

  it('leaves out a group with no rigs in it', async () => {
    const app = await pageWith();
    assert.deepEqual(plain(menu(app).map(([label]) => label)), ['This experiment']);
  });

  it('shows the file beside a name two of the experiment’s rigs share', async () => {
    const app = await pageWith({
      project: {
        ...PROJECT,
        rigs: [rigEntry('lab'), rigEntry('lab', { path: 'configs/old/rig-lab.yaml' })],
      },
      rigs: { 'configs/rig-lab.yaml': rig(true, false, { name: 'lab' }) },
    });
    /* Sorted by name like every rig (2026-10-06), so the file decides. */
    assert.deepEqual(plain(menu(app)[0][1].map(([, text]) => text)), [
      'demo/lab (configs/old/rig-lab.yaml)', 'demo/lab (configs/rig-lab.yaml)',
    ]);
  });

  it('sorts each group by name, and still opens on the laptop', async () => {
    const app = await pageWith({
      project: {
        ...PROJECT,
        rigs: [
          rigEntry('vpixx'), rigEntry('lab-rehearsal'), rigEntry('lab'),
          sharedEntry('mac'), sharedEntry('laptop'), sharedEntry('lab', { shadowed: true }),
        ],
      },
      rigs: { 'alhazen/laptop': rig(true, false, { name: 'laptop', source: 'alhazen' }) },
    });
    const groups = menu(app);
    assert.deepEqual(plain(groups.map(([heading, items]) => [heading, items.map(([, t]) => t)])), [
      ['This experiment', ['demo/lab', 'demo/lab-rehearsal', 'demo/vpixx']],
      ['Shared (alhazen)', ['alhazen/laptop', 'alhazen/mac']],
    ]);
    assert.equal(app.byId('rig').value, 'alhazen/laptop');
  });

  it('summarises a shared rig from the merged answer, and says whose it is', async () => {
    const app = await pageWith({ project: RIGGED, rigs: RIGS });
    /* The shared mac, which has the live monitor off, chosen from the menu:
     * the page used to open on it, and now opens on the experiment's own
     * laptop (changed at the owner's request, 2026-10-02). The summary
     * names it as the menu does (changed at the owner's request from
     * "alhazen’s shared rig mac"). */
    await chooseRig(app, 'alhazen/mac');
    assert.ok(app.fetches.some((f) => f.url === '/api/rig?project=p&rig=alhazen%2Fmac'));
    /* Every fact under its label (changed at the owner's request from three
     * lines of monospace text that wrapped mid-phrase). */
    assert.deepEqual(plain(facts(app)), {
      Screen: '1920 × 1080 px · 60 Hz',
      Size: '52 cm wide · 57 cm away',
      Display: 'default display',
      'Live monitor': 'off',
      Rig: 'alhazen/mac · alhazen’s shared rig',
    });
    /* A value is made of parts that never break inside; a line may break only
     * at the separators between them. */
    const screen = app.byId('rig-summary').children[0].children[1];
    assert.deepEqual(
      plain(screen.children.map((part) => [part.className, part.textContent])),
      [['fact', '1920 × 1080 px'], ['fact-separator', ' · '], ['fact', '60 Hz']],
    );
    /* Another shared rig: the summary follows. (The shadowed lab this test
     * used to pick is no longer in the menu to pick.) */
    await chooseRig(app, 'alhazen/vpixx');
    assert.equal(facts(app)['Live monitor'], 'on');
    assert.equal(facts(app).Rig, 'alhazen/vpixx · alhazen’s shared rig');
  });

  it('summarises a rig that extends a shared one as merged, naming the shared rig', async () => {
    const app = await pageWith({ project: RIGGED, rigs: RIGS });
    await chooseRig(app, 'configs/rig-lab.yaml');
    /* The experiment's file says nothing about the live monitor; the merged
     * answer does, and the Live monitor tab reads the same fact. */
    assert.equal(facts(app)['Live monitor'], 'on');
    /* Named as in the menu, then its file and the shared rig it builds on
     * (changed at the owner's request from "this experiment’s
     * configs/rig-lab.yaml, extending alhazen’s shared lab"; the "extends"
     * moved here from the menu's option text). */
    assert.equal(
      facts(app).Rig, 'demo/lab · this experiment’s configs/rig-lab.yaml · extends alhazen/lab',
    );
    assert.equal(app.run("rigMonitor['p:configs/rig-lab.yaml']"), true);
  });

  it('launches a shared rig by its alhazen/ name, and an experiment rig by its path',
    async () => {
      const app = await pageWith({ project: RIGGED, rigs: RIGS });
      chooseMode(app, 'movie');
      /* vpixx, not the shadowed lab this used to pick: that one is no longer
       * in the menu. The value sent is unchanged by the new labels. */
      await chooseRig(app, 'alhazen/vpixx');
      await launch(app);
      assert.equal(launched(app).rig, 'alhazen/vpixx');

      const own = await pageWith({ project: RIGGED, rigs: RIGS });
      chooseMode(own, 'movie');
      await chooseRig(own, 'configs/rig-lab.yaml');
      await launch(own);
      assert.equal(launched(own).rig, 'configs/rig-lab.yaml');
    });

  it('says why a project registered before shared rigs offers none', async () => {
    const note = 'This project was registered before the workspace listed alhazen’s shared rigs';
    const app = await pageWith({ project: { ...PROJECT, rigs_note: note } });
    assert.equal(app.byId('rig-note').hidden, false);
    assert.equal(app.byId('rig-note').textContent, note);
    const current = await pageWith();
    assert.equal(current.byId('rig-note').hidden, true);
  });

  it('shows a shared-rig run in the history by its alhazen/ name', async () => {
    const run = runDetail({
      status: 'completed', returncode: 0, rig: 'alhazen/lab', rig_name: 'lab',
      rig_source: 'alhazen',
    });
    const app = await pageWith({ project: RIGGED, rigs: RIGS, run: run });
    const row = app.byId('history').children[0];
    /* The rig's name, then the seed (2.3.0; a simulation that has not said
     * which seed it drew). */
    assert.match(row.querySelector('small').textContent, / · alhazen\/lab · seed new$/);
    const mine = await pageWith({
      project: RIGGED, rigs: RIGS,
      run: runDetail({
        status: 'completed', returncode: 0, rig: 'configs/rig-lab.yaml', rig_name: 'lab',
        rig_source: 'experiment',
      }),
    });
    /* The experiment's own lab, owner first (was the bare "lab"); the seed
     * after it, as above. */
    assert.match(
      mine.byId('history').children[0].querySelector('small').textContent,
      / · demo\/lab · seed new$/,
    );
  });
});

/** The sidebar's group for experiment `id`: its disclosure button and its
 *  pages (renderNav in workspace.js). */
function navGroup(app, id) {
  return app.byId('experiment-nav').children.find((g) => g.dataset.project === id);
}

/** Experiment `id`'s four page links in the sidebar. */
function pageLinks(app, id) {
  return navGroup(app, id).querySelector('.experiment-nav').children;
}

/** The title of the sidebar group marked as the open experiment's. */
function sidebarTitle(app) {
  return app.byId('experiment-nav').querySelector('.current')
    .querySelector('.nav-group-title').textContent;
}

describe('the experiment’s name', () => {
  it('shows the title in the sidebar, the heading, the breadcrumb and the browser tab',
    async () => {
      const app = await pageWith();
      /* The sidebar names the open experiment above its pages. */
      assert.equal(sidebarTitle(app), 'Demo task');
      assert.equal(app.byId('project-name').textContent, 'Demo task');
      assert.equal(app.byId('breadcrumb').textContent, 'Demo task');
      /* The tab names the page too, so Back's list tells pages apart. */
      assert.equal(app.document.title, 'Run · Demo task · Alhazen');
      /* The slug stays visible in small print, beside the folder. */
      assert.equal(app.byId('project-slug').textContent, 'demo');
      assert.equal(app.byId('project-path').textContent, 'C:/projects/demo');
      assert.equal(app.byId('title-error').hidden, true);
      assert.equal(app.byId('project-heading').hidden, false);
    });

  it('says loudly why a declared title is not shown, and carries on under the slug',
    async () => {
      const message = 'pyproject.toml: [tool.alhazen] title must be a non-empty string';
      const app = await pageWith({
        project: { ...PROJECT, title: 'demo', title_error: message },
      });
      assert.equal(app.byId('project-name').textContent, 'demo');
      assert.equal(app.byId('title-error').hidden, false);
      assert.equal(app.byId('title-error').textContent, message);
      assert.equal(app.byId('launch').disabled, false);
    });

  it('falls back to the registered folder name from a server that sends no title', async () => {
    const { title, slug, title_error: error, ...old } = PROJECT;
    assert.ok(title && slug && error === null);
    const app = await pageWith({ project: old });
    assert.equal(app.byId('project-name').textContent, 'demo-folder');
    assert.equal(app.document.title, 'Run · demo-folder · Alhazen');
    assert.equal(app.byId('rig').children[0].children[0].textContent, 'demo-folder/mac');
  });

  it('follows a title changed while the page is open', async () => {
    const app = await pageWith();
    app.server.state.projects = [{ ...PROJECT, title: 'Renamed' }];
    await app.run('refresh()');
    await settle();
    assert.equal(app.byId('breadcrumb').textContent, 'Renamed');
    assert.equal(app.document.title, 'Run · Renamed · Alhazen');
    assert.match(sidebarTitle(app), /Renamed/);
  });
});

describe('the launch form', () => {
  it('reads Configure a run → Rig → Task parameters → launch, with no step label', () => {
    /* The markup itself: the ids workspace.js finds are flat in the fake
     * page, so their nesting and order are read from the file. */
    const html = readFileSync(
      new URL('../../src/alhazen/cli/assets/workspace.html', import.meta.url), 'utf8',
    );
    const at = (text) => {
      const index = html.indexOf(text);
      assert.notEqual(index, -1, text);
      return index;
    };
    assert.doesNotMatch(html, /01 — SETUP/);
    const order = [
      'id="mode"', 'id="identity"', 'id="seed-fields"', 'id="movie-options"',
      'id="extra-args"', 'id="rig-section"', 'id="rig"', 'id="rig-summary"', 'id="rig-note"',
      'id="task-parameters"', 'id="params-config"', 'id="parameter-set-help"', 'id="fields-tab"',
      'id="parameter-search"',
      'id="parameter-fields"', 'class="launch-footer"',
    ].map(at);
    assert.deepEqual(order, [...order].sort((a, b) => a - b));
    /* The file menu is inside the Task parameters fieldset, so hiding or
     * disabling the fieldset takes it along; its heading is its label. */
    const fieldset = html.slice(at('id="task-parameters"'), html.indexOf('</fieldset>'));
    assert.match(fieldset, /<label for="params-config">Task parameters<\/label>/);
    assert.match(fieldset, /id="params-config"/);
    assert.doesNotMatch(html, /Parameter preset/);
    /* No Task menu: the Task parameters entry names the task (2026-10-06). */
    assert.doesNotMatch(html, /id="task"|id="task-field"/);
    /* The Data view's slot, right after the workspace view. */
    assert.match(html, /<div id="data-view" hidden><\/div>/);
    assert.ok(at('id="data-view"') > at('id="workspace"'));
  });
});

describe('the launch summary', () => {
  it('says in one line what the button will start, and follows the form', async () => {
    const app = await pageWith();
    chooseMode(app, 'simulate');
    assert.equal(app.byId('launch-summary').textContent, 'Simulate · task · demo/mac · ses 1');
    app.byId('subject').value = 's01';
    app.byId('subject').fire('input');
    app.byId('session').value = '2';
    app.byId('session').fire('input');
    assert.equal(app.byId('launch-summary').textContent,
      'Simulate · task · demo/mac · sub-s01 · ses 2');
    /* Measure takes no parameters and names no subject. */
    chooseMode(app, 'measure');
    assert.equal(app.byId('launch-summary').textContent, 'Measure rig · demo/mac');
  });
});

describe('the Mode menu', () => {
  it('lists the modes and the experiment’s scripts by name, opening on Simulate', async () => {
    const script = { id: 'movie_script', label: 'Contact sheet', flags: [], params_flag: null };
    const app = await pageWith({ project: { ...PROJECT, scripts: [script] } });
    const texts = app.byId('mode').children.map((o) => o.textContent);
    assert.deepEqual(plain(texts), [
      'Contact sheet', 'Demo', 'Measure rig', 'Record movies', 'Run experiment', 'Simulate',
      'Test session',
    ]);
    /* Where the menu opened before it was sorted: Simulate, with no preview. */
    assert.equal(app.byId('mode').value, 'simulate');
  });
});

describe('the Task parameters menu', () => {
  /** The menu's entries as [value, text]. */
  function entries(app) {
    return app.byId('params-config').children.map((o) => [o.value, o.textContent]);
  }

  /* A run.py with one task and four files, as the server derives its menu
   * (each file by its short name; workspace.project_parameter_sets, whose
   * Python tests cover the names). */
  const FOUR = Object.freeze({
    ...PROJECT,
    configs: [
      'configs/presets/task-x.yaml', 'configs/params-fast.yml', 'configs/task-pilot.yaml',
      'configs/task.yaml',
    ],
    parameter_sets: [
      { label: 'presets/x', task: null, params: 'configs/presets/task-x.yaml' },
      { label: 'task', task: null, params: 'configs/task.yaml' },
      { label: 'fast', task: null, params: 'configs/params-fast.yml' },
      { label: 'Pilot 10', task: null, params: 'configs/task-pilot.yaml' },
    ],
    default_parameter_set: 'task',
  });

  it('lists the server’s entries sorted by name, ignoring case', async () => {
    const app = await pageWith({ project: FOUR });
    assert.deepEqual(plain(entries(app)), [
      ['fast', 'fast'],
      ['Pilot 10', 'Pilot 10'],
      ['presets/x', 'presets/x'],
      ['task', 'task'],
    ]);
    assert.equal(app.byId('params-config').hidden, false);
    assert.equal(app.byId('parameter-search').hidden, false);
    assert.equal(app.byId('parameters-help').hidden, false);
    assert.equal(app.byId('editor-switch').hidden, false);
  });

  it('sorts numbers in number order: Block 2 before Block 10', async () => {
    const app = await pageWith({
      project: {
        ...PROJECT,
        parameter_sets: [
          { label: 'Block 10', task: null, params: 'configs/task.yaml' },
          { label: 'Block 2', task: null, params: 'configs/task.yaml' },
        ],
        default_parameter_set: 'Block 10',
      },
    });
    assert.deepEqual(plain(entries(app)).map(([value]) => value), ['Block 2', 'Block 10']);
    assert.equal(app.byId('params-config').value, 'Block 10');
  });

  it('opens on the server’s default entry, and sends its file’s parameters', async () => {
    const pilot = { text: 'trials: 2\n', values: { trials: 2 } };
    const withTask = await pageWith({ project: FOUR, configs: { 'configs/task-pilot.yaml': pilot } });
    assert.equal(withTask.byId('params-config').value, 'task');
    const without = await pageWith({
      project: {
        ...PROJECT,
        configs: ['configs/task-pilot.yaml'],
        parameter_sets: [{ label: 'pilot', task: null, params: 'configs/task-pilot.yaml' }],
        default_parameter_set: 'pilot',
      },
      configs: { 'configs/task-pilot.yaml': pilot },
    });
    assert.equal(without.byId('params-config').value, 'pilot');
    assert.deepEqual(plain(without.run('values')), { trials: 2 });
    chooseMode(without, 'movie');
    await launch(without);
    assert.deepEqual(launched(without).parameters, { trials: 2 });
    assert.equal(launched(without).parameter_set, 'pilot');
  });

  it('says so when the text editor is emptied, since a launch then sends no parameters',
    async () => {
      const app = await pageWith();
      app.run("$('launch-form').reportValidity = () => true");
      app.byId('yaml-tab').fire('click');
      await settle();
      app.byId('parameter-yaml').value = '  ';
      app.byId('fields-tab').fire('click');
      await settle();
      assert.equal(app.run('values'), null);
      assert.match(app.byId('parameter-fields').textContent, /text editor was left empty/);
      chooseMode(app, 'movie');
      await launch(app);
      assert.equal('parameters' in launched(app), false);
    });

  it('says a project without parameter files runs on its code’s defaults, and sends none',
    async () => {
      const app = await pageWith({
        project: { ...PROJECT, configs: [], parameter_sets: [], default_parameter_set: null },
      });
      /* No menu to choose from, and no request for a file. */
      assert.equal(app.byId('params-config').hidden, true);
      assert.equal(app.byId('params-config').children.length, 0);
      assert.equal(app.fetches.some((f) => f.url.startsWith('/api/config')), false);
      const fields = app.byId('parameter-fields').textContent;
      assert.match(fields, /No task parameter files in configs\//);
      assert.match(fields, /defaults written in its code/);
      assert.match(fields, /without --params/);
      /* Nothing to search or switch, and no snapshot is promised: only the
       * message remains. */
      assert.equal(app.byId('parameter-search').hidden, true);
      assert.equal(app.byId('parameters-help').hidden, true);
      assert.equal(app.byId('editor-switch').hidden, true);
      assert.equal(app.byId('launch').disabled, false);
      chooseMode(app, 'movie');
      await launch(app);
      const body = launched(app);
      assert.equal('parameters' in body, false);
      assert.equal('parameters_yaml' in body, false);
    });
});

describe('the sidebar: Experiments, and the open experiment’s pages', () => {
  const OTHER = Object.freeze({ ...PROJECT, id: 'q', title: 'Other task', slug: 'other' });

  /** A fake workspace_data.js: records every call it gets. */
  function fakeData(app) {
    app.run(`window.WorkspaceData = {
      calls: [],
      show(project, helpers) {
        this.calls.push(['show', project.id, Object.keys(helpers).sort().join(',')]);
      },
      hide() { this.calls.push(['hide']); },
    }`);
    return () => plain(app.run('window.WorkspaceData.calls'));
  }

  /** Experiment `id`'s pages in the sidebar as [view, text, current]. */
  function pages(app, id = 'p') {
    return pageLinks(app, id).map((a) => [
      a.dataset.view, a.querySelector('.nav-label').textContent, a.getAttribute('aria-current'),
    ]);
  }

  /** Click experiment `id`'s sidebar link for page `name`, as a plain left
   *  click. */
  async function clickView(app, name, id = 'p') {
    pageLinks(app, id).find((a) => a.dataset.view === name)
      .fire('click', { preventDefault() {}, button: 0 });
    await settle();
    await settle();
  }

  it('lists the open experiment’s four pages, Run shown, each a real address', async () => {
    const app = await pageWith();
    assert.deepEqual(plain(pages(app)), [
      ['general', 'General', 'false'], ['run', 'Run', 'page'], ['data', 'Data', 'false'],
      ['history', 'History', 'false'],
    ]);
    const links = pageLinks(app, 'p').map((a) => a.href);
    assert.deepEqual(plain(links), [
      '/?project=p&view=general', '/?project=p&view=run', '/?project=p&view=data',
      '/?project=p&view=history',
    ]);
    assert.equal(app.byId('nav-experiment').hidden, false);
    assert.equal(app.byId('nav-experiments').getAttribute('aria-current'), 'false');
    assert.equal(app.byId('workspace').hidden, false);
    assert.equal(app.byId('breadcrumb-view').textContent, '/ Run');
  });

  it('goes to Data and Back again, telling workspace_data.js each time', async () => {
    const app = await pageWith();
    const calls = fakeData(app);
    await clickView(app, 'data');
    assert.equal(app.location.search, '?project=p&view=data');
    assert.equal(app.byId('workspace').hidden, true);
    assert.equal(app.byId('data-view').hidden, false);
    assert.equal(app.byId('project-heading').hidden, false);
    assert.equal(app.byId('breadcrumb-view').textContent, '/ Data');
    assert.equal(app.byId('view-eyebrow').textContent, 'DATA');
    assert.deepEqual(plain(pages(app)).map(([, , current]) => current),
      ['false', 'false', 'page', 'false']);
    assert.deepEqual(calls(), [['show', 'p', 'api,error,node,token']]);
    /* A poll keeps the view as it is and does not show it again. */
    await app.run('refresh()');
    await settle();
    assert.equal(calls().length, 1);
    /* The browser's Back: the address changes, then the page follows it. */
    app.back();
    await settle();
    await settle();
    assert.equal(app.byId('workspace').hidden, false);
    assert.equal(app.byId('data-view').hidden, true);
    assert.equal(app.byId('view-eyebrow').textContent, 'RUN');
    assert.deepEqual(calls(), [['show', 'p', 'api,error,node,token'], ['hide']]);
  });

  it('says plainly when the data view’s script is not loaded', async () => {
    const app = await pageWith();
    await clickView(app, 'data');
    assert.equal(app.byId('data-view').hidden, false);
    assert.equal(app.byId('data-view').textContent, 'Data inspection is not available');
  });

  it('hands General and History to workspace_manage.js', async () => {
    const app = await pageWith();
    app.run(`window.WorkspaceManage = {
      calls: [],
      showGeneral(target, project) { this.calls.push(['general', target.id, project.id]); },
      showHistory(target, project) { this.calls.push(['history', target.id, project.id]); },
      hide() { this.calls.push(['hide']); },
      leave: async () => true,
    }`);
    await clickView(app, 'general');
    assert.equal(app.byId('general-view').hidden, false);
    assert.equal(app.byId('workspace').hidden, true);
    await clickView(app, 'history');
    assert.equal(app.byId('history-view').hidden, false);
    assert.equal(app.byId('general-view').hidden, true);
    assert.deepEqual(plain(app.run('window.WorkspaceManage.calls')), [
      ['general', 'general-view', 'p'], ['hide'], ['history', 'history-view', 'p'],
    ]);
  });

  it('stays put when unsaved edits are kept', async () => {
    const app = await pageWith();
    app.run('window.WorkspaceManage = { leave: async () => false, hide() {} }');
    await clickView(app, 'data');
    assert.equal(app.location.search, '?view=run');
    assert.equal(app.byId('workspace').hidden, false);
  });

  it('opens the page an address names, and the remembered one when it names none',
    async () => {
      const data = await pageWith({ search: '?project=p&view=data' });
      assert.equal(data.byId('data-view').hidden, false);
      assert.equal(data.byId('workspace').hidden, true);
      const remembered = await pageWith({
        search: '?project=p', storage: { 'alhazen-workspace-view:p': 'data' },
      });
      assert.equal(remembered.byId('data-view').hidden, false);
      const unknown = await pageWith({
        search: '?project=p', storage: { 'alhazen-workspace-view:p': 'charts' },
      });
      assert.equal(unknown.byId('workspace').hidden, false);
    });

  it('keeps each experiment’s page when switching experiments', async () => {
    const app = await pageWith();
    app.server.state.projects = [PROJECT, OTHER];
    await app.run('refresh()');
    await settle();
    const calls = fakeData(app);
    await clickView(app, 'data');
    assert.equal(app.run("localStorage.getItem('alhazen-workspace-view:p')"), 'data');
    await app.run("navigate('q', 'run')");
    await settle();
    assert.equal(sidebarTitle(app), 'Other task');
    assert.equal(app.byId('workspace').hidden, false);
    assert.deepEqual(calls(), [['show', 'p', 'api,error,node,token'], ['hide']]);
    app.back();
    await settle();
    await settle();
    assert.equal(sidebarTitle(app), 'Demo task');
    assert.equal(app.byId('data-view').hidden, false);
  });
});

describe('the sidebar: every experiment, each expanding to its pages', () => {
  const OTHER = Object.freeze({ ...PROJECT, id: 'q', title: 'Other task', slug: 'other' });
  const SHELVED = Object.freeze({ ...PROJECT, id: 'r', title: 'Shelved', archived: true });
  const NAV_OPEN = 'alhazen-workspace-nav-open';

  /** A page on `search` with PROJECT, OTHER and SHELVED registered. */
  async function threeExperiments(options = {}) {
    const app = await pageWith({ search: '?project=p&view=run', ...options });
    app.server.state.projects = [PROJECT, OTHER, SHELVED];
    await app.run('refresh()');
    await settle();
    return app;
  }

  /** Experiment `id`'s disclosure button. */
  const toggle = (app, id) => navGroup(app, id).querySelector('.nav-group-toggle');
  /** Whether experiment `id`'s pages show: [aria-expanded, pages hidden]. */
  const shown = (app, id) => [
    toggle(app, id).getAttribute('aria-expanded'),
    navGroup(app, id).querySelector('.experiment-nav').hidden,
  ];
  /** The links marked as the page shown, as [experiment, view]. */
  const marked = (app) => app.byId('experiment-nav').children.flatMap((g) => pageLinks(
    app, g.dataset.project).filter((a) => a.getAttribute('aria-current') === 'page')
    .map((a) => [g.dataset.project, a.dataset.view]));
  const stored = (app) => JSON.parse(app.run(`localStorage.getItem('${NAV_OPEN}')`));

  it('lists every experiment that is not archived, the open one expanded', async () => {
    const app = await threeExperiments();
    const groups = app.byId('experiment-nav').children;
    assert.deepEqual(plain(groups.map((g) => g.dataset.project)), ['p', 'q']);
    assert.deepEqual(plain(groups.map((g) => g.querySelector('.nav-group-title').textContent)),
      ['Demo task', 'Other task']);
    assert.deepEqual(plain(shown(app, 'p')), ['true', false]);
    assert.deepEqual(plain(shown(app, 'q')), ['false', true]);
    assert.equal(navGroup(app, 'p').classList.contains('current'), true);
    assert.equal(navGroup(app, 'q').classList.contains('current'), false);
    assert.deepEqual(plain(marked(app)), [['p', 'run']]);
    /* The button names what it shows; the other experiment's pages are real
     * addresses even while collapsed. */
    assert.equal(toggle(app, 'q').getAttribute('aria-controls'),
      navGroup(app, 'q').querySelector('.experiment-nav').id);
    assert.deepEqual(plain(pageLinks(app, 'q').map((a) => a.href)), [
      '/?project=q&view=general', '/?project=q&view=run', '/?project=q&view=data',
      '/?project=q&view=history',
    ]);
  });

  it('expands and collapses another experiment without leaving the page or stopping a run',
    async () => {
      const app = await threeExperiments({ run: runDetail({ status: 'running' }) });
      const before = app.fetches.length;
      toggle(app, 'q').fire('click');
      assert.deepEqual(plain(shown(app, 'q')), ['true', false]);
      assert.equal(app.location.search, '?project=p&view=run');
      assert.equal(app.history.entries.length, 1);
      assert.equal(app.byId('workspace').hidden, false);
      assert.deepEqual(plain(marked(app)), [['p', 'run']]);
      assert.equal(app.fetches.length, before);
      assert.equal(app.server.posted.length, 0);
      assert.deepEqual(stored(app), ['p', 'q']);
      /* The run's lamp is on its own experiment's Run, not on the other. */
      assert.ok(toggle(app, 'p').querySelector('.nav-lamp'));
      assert.equal(toggle(app, 'q').querySelector('.nav-lamp'), null);
      toggle(app, 'q').fire('click');
      assert.deepEqual(plain(shown(app, 'q')), ['false', true]);
      assert.deepEqual(stored(app), ['p']);
    });

  it('keeps the reader’s expanded groups across a reload, and a collapsed open one',
    async () => {
      const app = await threeExperiments({ storage: { [NAV_OPEN]: '["q"]' } });
      /* The open experiment is expanded on opening; q as remembered. */
      assert.deepEqual(plain(shown(app, 'p')), ['true', false]);
      assert.deepEqual(plain(shown(app, 'q')), ['true', false]);
      /* Collapsing the open experiment holds through redraws. */
      toggle(app, 'p').fire('click');
      app.server.state.projects = [PROJECT, { ...OTHER, title: 'Other task 2' }, SHELVED];
      await app.run('refresh()');
      await settle();
      assert.deepEqual(plain(shown(app, 'p')), ['false', true]);
      assert.deepEqual(stored(app), ['q']);
    });

  it('opens the experiment a link goes to, and marks pages through Back and Forward',
    async () => {
      const app = await threeExperiments();
      toggle(app, 'q').fire('click');
      pageLinks(app, 'q').find((a) => a.dataset.view === 'data')
        .fire('click', { preventDefault() {}, button: 0 });
      await settle();
      await settle();
      assert.equal(app.location.search, '?project=q&view=data');
      assert.deepEqual(plain(marked(app)), [['q', 'data']]);
      assert.equal(navGroup(app, 'q').classList.contains('current'), true);
      /* p stays listed and expanded beside it. */
      assert.deepEqual(plain(shown(app, 'p')), ['true', false]);
      app.back();
      await settle();
      await settle();
      assert.deepEqual(plain(marked(app)), [['p', 'run']]);
      app.forward();
      await settle();
      await settle();
      assert.equal(app.location.search, '?project=q&view=data');
      assert.deepEqual(plain(marked(app)), [['q', 'data']]);
    });

  it('adds a newly registered experiment collapsed and leaves out one archived',
    async () => {
      const app = await threeExperiments();
      const NEW = { ...PROJECT, id: 'n', title: 'New task' };
      app.server.state.projects = [PROJECT, OTHER, SHELVED, NEW];
      await app.run('refresh()');
      await settle();
      assert.deepEqual(plain(app.byId('experiment-nav').children.map((g) => g.dataset.project)),
        ['p', 'q', 'n']);
      assert.deepEqual(plain(shown(app, 'n')), ['false', true]);
      toggle(app, 'q').fire('click');
      app.server.state.projects = [PROJECT, { ...OTHER, archived: true }, SHELVED, NEW];
      await app.run('refresh()');
      await settle();
      assert.deepEqual(plain(app.byId('experiment-nav').children.map((g) => g.dataset.project)),
        ['p', 'n']);
      /* Its expansion is kept, for when it is restored. */
      assert.deepEqual(stored(app), ['p', 'q']);
    });

  it('gives keyboard focus back to the sidebar control a redraw replaced', async () => {
    const app = await threeExperiments();
    toggle(app, 'q').focus();
    app.server.state.projects = [PROJECT, { ...OTHER, title: 'Renamed' }, SHELVED];
    await app.run('refresh()');
    await settle();
    assert.equal(app.document.activeElement, toggle(app, 'q'));
    assert.match(app.document.activeElement.textContent, /Renamed/);
  });

  it('treats an unreadable stored state as nothing expanded but the open experiment',
    async () => {
      const app = await threeExperiments({ storage: { [NAV_OPEN]: '{not json' } });
      assert.deepEqual(plain(shown(app, 'p')), ['true', false]);
      assert.deepEqual(plain(shown(app, 'q')), ['false', true]);
    });
});

describe('History: Open brings the details into view', () => {
  const HISTORY = {
    missing: [], problems: [],
    sessions: [{
      id: 'sub-01_ses-01_run-01', root: 'data', root_kind: 'data', subject: '01',
      initials: 'AB', session: '01', run: '01', date: '2026-10-01', task: 'main',
      rig: 'mac', mode: 'run', trials: 576, trials_counted: 'rows',
      experimenter: { recorded: true, name: 'Sharif' }, launch: 'l1',
    }],
    launches: [{
      id: 'l1', mode: 'simulate', parameter_set: 'Main', task: 'main', subject: '01',
      initials: 'AB', experimenter: null, rig: 'mac', started: '2026-10-01T10:00:00',
      finished: '2026-10-01T10:30:00', status: 'completed', active: false, seed: 7,
      files: [], session_folder: { root: 'data', run: 'sub-01_ses-01_run-01' },
    }],
  };
  const RUN = { path: 'data/sub-01_ses-01_run-01', problems: [], page: null, texts: [],
    files: [], files_capped: false };

  /** The History page of PROJECT, with the session's details answered by
   *  `runAnswer` (a response, or a promise of one). */
  async function historyPage(runAnswer = response(RUN)) {
    const app = await pageWith({ search: '?project=p&view=history' });
    app.server.reject = (url) => {
      if (url.startsWith('/api/manage/history')) return response(HISTORY);
      if (url.startsWith('/api/data/run')) return runAnswer;
      return undefined;
    };
    await app.run("navigate('p', 'history', {replace: true})");
    await settle();
    await settle();
    return app;
  }

  const detail = (app) => app.byId('history-view').querySelector('.m-detail');
  /** The Open button of the sessions (0) or launches (1) table. */
  const open = (app, table) => app.byId('history-view')
    .querySelectorAll('table')[table].querySelector('button');
  const scrolls = (app) => app.document.scrolledIntoView;

  it('scrolls the page to a session’s details once, and focuses them', async () => {
    const app = await historyPage();
    assert.equal(scrolls(app).length, 0);
    open(app, 0).fire('click');
    await settle();
    assert.equal(scrolls(app).length, 1);
    assert.equal(scrolls(app)[0].element, detail(app));
    assert.deepEqual(plain(scrolls(app)[0].options), { block: 'start', behavior: 'auto' });
    assert.match(detail(app).textContent, /SESSION/);
    assert.equal(app.document.activeElement, detail(app));
    assert.deepEqual(plain(app.document.focused.at(-1).options), { preventScroll: true });
    /* A poll and the filter redraw nothing that moves the page. */
    await app.run('refresh()');
    await settle();
    const search = app.byId('history-view').querySelector('input');
    search.value = 'sub';
    search.fire('input');
    assert.equal(scrolls(app).length, 1);
  });

  it('scrolls to a launch’s details once, and again for each Open', async () => {
    const app = await historyPage();
    open(app, 1).fire('click');
    assert.equal(scrolls(app).length, 1);
    assert.equal(scrolls(app)[0].element, detail(app));
    assert.match(detail(app).textContent, /LAUNCH/);
    assert.equal(app.document.activeElement, detail(app));
    open(app, 1).fire('click');
    assert.equal(scrolls(app).length, 2);
  });

  it('shows a session that cannot be read as an error, in view', async () => {
    const app = await historyPage(response({ error: 'No such folder' }, 404));
    open(app, 0).fire('click');
    await settle();
    assert.equal(scrolls(app).length, 1);
    assert.match(detail(app).textContent, /No such folder/);
    assert.equal(app.document.activeElement, detail(app));
  });

  it('keeps the latest Open when an earlier session answers late', async () => {
    let answer;
    const late = new Promise((resolve) => { answer = resolve; });
    const app = await historyPage(late);
    open(app, 0).fire('click');
    await settle();
    assert.match(detail(app).textContent, /Opening the session/);
    open(app, 1).fire('click');
    answer(response(RUN));
    await settle();
    await settle();
    assert.match(detail(app).textContent, /LAUNCH/);
    assert.equal(scrolls(app).length, 2);
  });
});

describe('who: the Run page’s Subject and Experimenter', () => {
  const SUBJECT = { id: 's_1', experiment_id: 'p', code: '007', initials: 'HD', notes: null,
    extra: [], status: 'active', position: 1, revision: 1, sources: [], used: false };
  const ARCHIVED = { ...SUBJECT, id: 's_2', code: '008', status: 'archived' };
  const PERSON = { id: 'e_1a2b', name: '<img src=x onerror=alert(1)>', initials: 'ZL',
    notes: null, status: 'active', revision: 1 };
  const PEOPLE = {
    error: null, subjects: [SUBJECT, ARCHIVED], experimenters: [PERSON],
    assigned: [{ ...PERSON, assignment_status: 'active', assigned: '2026-10-07' }],
    export: null, files: null,
  };

  const texts = (select) => select.children.map((o) => o.textContent);

  it('offers the active registered subjects and assigned experimenters, as text', async () => {
    const app = await pageWith({ people: PEOPLE });
    chooseMode(app, 'run');
    assert.deepEqual(plain(texts(app.byId('subject-record'))),
      ['Choose a subject…', 'sub-007 · HD']);
    assert.deepEqual(plain(texts(app.byId('experimenter'))),
      ['Choose who runs it…', '<img src=x onerror=alert(1)> (ZL)']);
    /* A name is text: no element was made from it (the one <img> is the
     * page's own image dialog). */
    assert.deepEqual(plain(app.document.querySelectorAll('img').map((i) => i.id)),
      ['large-image']);
    assert.equal(app.byId('typed-identity').open, false);
  });

  it('asks for the subject, then the experimenter, before a run can start', async () => {
    const app = await pageWith({ people: PEOPLE });
    chooseMode(app, 'run');
    assert.equal(app.byId('launch').disabled, true);
    assert.match(app.byId('launch-note').textContent, /Choose the subject/);
    app.byId('subject-record').value = 's_1';
    app.byId('subject-record').fire('change');
    assert.equal(app.byId('launch').disabled, true);
    assert.match(app.byId('launch-note').textContent, /Choose the experimenter/);
    assert.equal(app.byId('subject').disabled, true);
    app.byId('experimenter').value = 'e_1a2b';
    app.byId('experimenter').fire('change');
    assert.equal(app.byId('launch').disabled, false);
    assert.match(app.byId('launch-summary').textContent, /sub-007 · ses 1 · by <img/);
    await launch(app);
    const body = launched(app);
    assert.equal(body.subject_record, 's_1');
    assert.equal(body.experimenter, 'e_1a2b');
    assert.equal(body.subject, '');
    assert.equal(body.initials, '');
  });

  it('still takes a typed subject, with an optional experimenter', async () => {
    const app = await pageWith({ people: PEOPLE });
    chooseMode(app, 'test');
    app.byId('typed-identity').open = true;
    app.byId('typed-identity').fire('toggle');
    app.byId('subject').value = 's99';
    app.byId('initials').value = 'ab';
    await launch(app);
    const body = launched(app);
    assert.equal(body.subject, 's99');
    assert.equal(body.initials, 'AB');
    assert.equal('subject_record' in body, false);
    assert.equal('experimenter' in body, false);
  });

  it('sends no one for a mode that names no one', async () => {
    const app = await pageWith({ people: PEOPLE });
    chooseMode(app, 'run');
    app.byId('subject-record').value = 's_1';
    app.byId('subject-record').fire('change');
    app.byId('experimenter').value = 'e_1a2b';
    app.byId('experimenter').fire('change');
    chooseMode(app, 'movie');
    assert.equal(app.byId('experimenter-field').hidden, true);
    await launch(app);
    const body = launched(app);
    assert.equal('subject_record' in body, false);
    assert.equal('experimenter' in body, false);
  });

  it('keeps each experiment’s choice apart', async () => {
    const OTHER = { ...PROJECT, id: 'q', title: 'Other task' };
    const app = await pageWith({ people: PEOPLE });
    app.server.state.projects = [PROJECT, OTHER];
    await app.run('refresh()');
    await settle();
    chooseMode(app, 'run');
    app.byId('subject-record').value = 's_1';
    app.byId('subject-record').fire('change');
    app.server.people = { ...PEOPLE, subjects: [], assigned: [] };
    await app.run("navigate('q', 'run')");
    await settle();
    await settle();
    assert.equal(app.byId('subject-record').value, '');
    app.server.people = PEOPLE;
    await app.run("navigate('p', 'run')");
    await settle();
    await settle();
    assert.equal(app.byId('subject-record').value, 's_1');
  });

  it('says when the experiment’s alhazen will not record the experimenter', async () => {
    const app = await pageWith({
      people: PEOPLE,
      project: { ...PROJECT, records_experimenter: false, alhazen_version: '2.10.0' },
    });
    chooseMode(app, 'test');
    app.byId('experimenter').value = 'e_1a2b';
    app.byId('experimenter').fire('change');
    assert.match(app.byId('identity-help').textContent,
      /\(2\.10\.0\) does not record the experimenter/);
  });
});

describe('the Experiments page', () => {
  const OTHER = Object.freeze({
    ...PROJECT, id: 'q', title: 'Other task', slug: 'other', path: 'C:/projects/other',
    archived: true,
  });

  it('is where an address without an experiment lands', async () => {
    const app = await pageWith({ search: '' });
    assert.equal(app.byId('home-view').hidden, false);
    assert.equal(app.byId('project-heading').hidden, true);
    assert.equal(app.byId('workspace').hidden, true);
    /* The experiments stay in the sidebar, none of their pages marked. */
    assert.equal(app.byId('nav-experiment').hidden, false);
    assert.equal(app.byId('experiment-nav').querySelector('.current'), null);
    assert.equal(app.byId('experiment-nav').querySelectorAll('a[aria-current="page"]').length, 0);
    assert.equal(app.byId('nav-experiments').getAttribute('aria-current'), 'page');
    assert.equal(app.document.title, 'Experiments · Alhazen');
    /* Drawn by workspace_manage.js: one row per registered experiment. */
    const rows = app.byId('home-list').querySelectorAll('.m-exp');
    assert.equal(rows.length, 1);
    assert.match(rows[0].textContent, /Demo task/);
    assert.match(rows[0].textContent, /C:\/projects\/demo/);
  });

  it('lists archived experiments apart and leaves them out of the count', async () => {
    const app = await pageWith({ search: '' });
    app.server.state.projects = [PROJECT, OTHER];
    await app.run('refresh()');
    await settle();
    const archive = app.byId('home-list').querySelector('.m-archive');
    assert.equal(archive.hidden, false);
    assert.match(archive.textContent, /Other task/);
    assert.equal(app.byId('project-count').textContent, '1');
  });

  it('opens an experiment from its row, as a new address', async () => {
    const app = await pageWith({ search: '' });
    const run = app.byId('home-list').querySelector('a.m-action-primary');
    assert.equal(run.href, '/?project=p&view=run');
    run.fire('click', { preventDefault() {}, button: 0 });
    await settle();
    await settle();
    assert.equal(app.location.search, '?project=p&view=run');
    assert.equal(app.byId('workspace').hidden, false);
    assert.equal(app.byId('home-view').hidden, true);
  });

  it('says so when an address names an experiment that is not registered', async () => {
    const app = await pageWith({ search: '?project=gone&view=run' });
    assert.equal(app.byId('home-view').hidden, false);
    assert.match(app.byId('error').textContent, /not registered/);
    assert.equal(app.location.search, '?view=experiments');
  });

  it('shows a run in progress from every page, with a way back to it', async () => {
    const run = runDetail({ status: 'running' });
    const app = await pageWith({ run: run, search: '' });
    assert.equal(app.byId('nav-running').hidden, false);
    assert.match(app.byId('nav-running').textContent, /Running · Demo task/);
    assert.equal(app.byId('nav-running').href, '/?project=p&view=run');
    /* Leaving the Run page stopped nothing: no POST was made. */
    assert.equal(app.server.posted.length, 0);
  });
});

describe('the colour theme', () => {
  /** The page's data-theme attribute: null means "follow the system". */
  const theme = (app) => app.document.documentElement.getAttribute('data-theme');
  const pressed = (app) => ['system', 'light', 'dark'].map(
    (name) => app.byId(`theme-${name}`).getAttribute('aria-pressed'),
  );

  it('follows the system until the reader chooses, and remembers the choice', async () => {
    const app = await pageWith();
    assert.equal(theme(app), null);
    assert.deepEqual(pressed(app), ['true', 'false', 'false']);
    app.byId('theme-dark').fire('click');
    assert.equal(theme(app), 'dark');
    assert.deepEqual(pressed(app), ['false', 'false', 'true']);
    assert.equal(app.run("localStorage.getItem('alhazen-workspace-theme')"), 'dark');
    app.byId('theme-light').fire('click');
    assert.equal(theme(app), 'light');
    app.byId('theme-system').fire('click');
    assert.equal(theme(app), null);
    assert.equal(app.run("localStorage.getItem('alhazen-workspace-theme')"), 'system');
  });

  it('applies the remembered theme when the page loads', async () => {
    const app = await pageWith({ storage: { 'alhazen-workspace-theme': 'dark' } });
    assert.equal(theme(app), 'dark');
    assert.deepEqual(pressed(app), ['false', 'false', 'true']);
  });

  it('ignores a remembered theme it does not know', async () => {
    const app = await pageWith({ storage: { 'alhazen-workspace-theme': 'sepia' } });
    assert.equal(theme(app), null);
    assert.deepEqual(pressed(app), ['true', 'false', 'false']);
  });

  it('keeps the two copies of the dark palette in the stylesheet identical', () => {
    /* The dark colours are written twice — for "Auto" when the system is
     * dark, and for an explicit Dark — because CSS cannot share one block
     * between a media query and a plain selector. A colour changed in one
     * copy only would make Auto and Dark differ; this catches it. */
    const css = readFileSync(
      new URL('../../src/alhazen/cli/assets/workspace.css', import.meta.url), 'utf8',
    );
    const block = (marker) => {
      const start = css.indexOf(marker);
      assert.notEqual(start, -1, marker);
      const open = css.indexOf('{', start + marker.length);
      return css.slice(open + 1, css.indexOf('}', open)).trim();
    };
    const system = block(':root:not([data-theme=light])');
    const chosen = block(':root[data-theme=dark]');
    assert.match(system, /--paper:/);
    assert.equal(system, chosen);
  });

  it('keeps every text and control colour readable in both palettes', () => {
    /* WCAG AA: text needs a contrast ratio of 4.5:1 against what it is drawn
     * on; a control's edge (a field's border, the focus ring) and the logo's
     * bricks 3:1. The dark palette is the owner's VS Code theme, and where
     * the theme itself falls short (white on its blue button is 2.8:1) the
     * stylesheet departs from it; this pins that it did. Each pair below is
     * one the stylesheet really draws (text token on background token). */
    const css = readFileSync(
      new URL('../../src/alhazen/cli/assets/workspace.css', import.meta.url), 'utf8',
    );
    /** The tokens of the block that starts at `marker`, as {name: '#rrggbb'}. */
    const palette = (marker) => {
      const start = css.indexOf(marker);
      assert.notEqual(start, -1, marker);
      // The block's own brace: the search starts at the marker, which may end in it.
      const open = css.indexOf('{', start);
      const text = css.slice(open + 1, css.indexOf('}', open));
      return Object.fromEntries(
        [...text.matchAll(/--([\w-]+):\s*(#[0-9a-fA-F]{6})\b/g)].map((m) => [m[1], m[2]]),
      );
    };
    /** Relative luminance of an sRGB colour (WCAG 2 definition). */
    const luminance = (hex) => {
      const [r, g, b] = [1, 3, 5].map((i) => {
        const c = parseInt(hex.slice(i, i + 2), 16) / 255;
        return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    };
    const ratio = (a, b) => {
      const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
      return (hi + 0.05) / (lo + 0.05);
    };
    const TEXT = [
      ['ink', ['paper', 'surface', 'surface-2', 'surface-3', 'field', 'hover-bg']],
      ['muted', ['paper', 'surface', 'surface-2', 'surface-3', 'field']],
      ['accent', ['paper', 'surface', 'accent-soft']],
      ['on-accent', ['accent', 'accent-hover']],
      ['selected-ink', ['selected-bg']],
      ['run-ink', ['run-bg']],
      ['ok-ink', ['ok-bg', 'surface']],
      ['bad-ink', ['bad-bg', 'surface', 'paper']],
      ['console-ink', ['console-bg']],
      /* The sidebar's rail (2026-10-06): its own text on its own grounds,
       * and the signal path's step numbers on their nodes. */
      ['rail-ink', ['rail', 'rail-2', 'rail-3']],
      ['rail-muted', ['rail', 'rail-2']],
      ['node-ink', ['node']],
      ['on-accent', ['accent']],
    ];
    const EDGES = [
      ['field-border', ['field', 'surface']],
      ['focus', ['paper', 'surface']],
      ['logo-brick', ['logo-ground']],
      ['logo-brick', ['rail']],
    ];
    const light = palette(':root {');
    const dark = palette(':root[data-theme=dark]');
    // Light mode keeps its own colours; the dark one is the VS Code theme's.
    assert.equal(dark.paper.toLowerCase(), '#080808');
    assert.equal(dark['selected-ink'].toLowerCase(), '#f3c900');
    const low = [];
    for (const [name, colours] of [['light', light], ['dark', dark]]) {
      for (const [pairs, need] of [[TEXT, 4.5], [EDGES, 3]]) {
        for (const [fg, backgrounds] of pairs) {
          for (const bg of backgrounds) {
            // A token missing from a palette is a failure, not a skip.
            assert.ok(colours[fg] && colours[bg], `${name}: --${fg} or --${bg} is not a #rrggbb token`);
            const r = ratio(colours[fg], colours[bg]);
            if (r < need) low.push(`${name} --${fg} on --${bg}: ${r.toFixed(2)} < ${need}`);
          }
        }
      }
    }
    assert.deepEqual(low, []);
  });
});

describe('the logo', () => {
  it('is the same drawing in the sidebar and in the favicon', () => {
    /* The sidebar draws it inline (so it follows the page's theme) and the
     * browser tab loads favicon.svg (an image, which cannot read the page's
     * CSS): two copies of one drawing, which must not drift apart. */
    const read = (name) => readFileSync(
      new URL(`../../src/alhazen/cli/assets/${name}`, import.meta.url), 'utf8',
    );
    const drawing = (text) => {
      const start = text.indexOf('<!-- LOGO-START');
      const end = text.indexOf('<!-- LOGO-END -->');
      assert.ok(start !== -1 && end > start);
      return text.slice(start, end).replace(/\s+/g, ' ');
    };
    // The frame, the letter and the layering are one drawing. The bricks
    // (the <pattern>s before LOGO-START) are sized per file: see the next test.
    const inline = drawing(read('workspace.html'));
    assert.equal(drawing(read('favicon.svg')), inline);
    assert.match(inline, /url\(#logo-bricks-turned\)/);
    assert.match(read('workspace.html'), /<link rel="icon" href="\/favicon.svg" type="image\/svg\+xml">/);
  });

  it('uses 4:1 bricks, finer in the sidebar than in the favicon', () => {
    /* The sidebar draws the logo at 56 px with bricks 4 units wide (one
     * pixel each); a browser tab draws the favicon at 16-32 px, where those
     * would blur into grey, so it uses bricks 7 wide. In both the ground is
     * horizontal L×w bricks, the figure the same turned upright and shifted
     * by half a brick width both ways. */
    const read = (name) => readFileSync(
      new URL(`../../src/alhazen/cli/assets/${name}`, import.meta.url), 'utf8',
    );
    const bricks = (text) => {
      const ground = text.match(/<pattern id="logo-bricks" width="([\d.]+)" height="([\d.]+)"/);
      const turned = text.match(
        /<pattern id="logo-bricks-turned" width="([\d.]+)" height="([\d.]+)"\s+patternUnits="userSpaceOnUse" patternTransform="translate\(([\d.]+) ([\d.]+)\)"/,
      );
      assert.ok(ground && turned, 'both brick patterns are present');
      const [gw, gh] = ground.slice(1, 3).map(Number);
      const [tw, th, dx, dy] = turned.slice(1, 5).map(Number);
      // A tile is two bricks: ground 2L × 2w, figure 2w × 2L.
      const w = gh / 2;
      assert.equal(gw / 2, 4 * w, 'ground bricks are 4:1');
      assert.deepEqual([tw, th], [gh, gw], 'the figure is the ground turned 90 degrees');
      assert.deepEqual([dx, dy], [w / 2, w / 2], 'shifted by half a brick width');
      return w;
    };
    assert.equal(bricks(read('workspace.html')), 4);
    assert.equal(bricks(read('favicon.svg')), 7);
  });

  it('paints the upright bricks inside a drawn A, with nothing on top of it', () => {
    /* The owner: the vertical bricks are painted IN the A, not an A on top
     * of the figure. So the letter is a path (the favicon cannot load the
     * page's font, and a path looks the same everywhere) that clips the
     * turned bricks, and there is no disc, <text> or solid letter left. */
    const html = readFileSync(
      new URL('../../src/alhazen/cli/assets/workspace.html', import.meta.url), 'utf8',
    );
    const logo = html.slice(html.indexOf('<!-- LOGO-START'), html.indexOf('<!-- LOGO-END -->'));
    const markup = logo.replace(/<!--[\s\S]*?-->/g, '');
    assert.match(markup, /<clipPath id="logo-letter">\s*<path d="M[^"]+Z"\/>/);
    // The turned bricks are drawn only inside the letter's clip.
    assert.match(markup, /<g clip-path="url\(#logo-letter\)">[^]*?fill="url\(#logo-bricks-turned\)"[^]*?<\/g>/);
    assert.doesNotMatch(markup, /<text|<circle|stroke/);
  });
});

describe('a task table with a task that has no parameter file', () => {
  /* kde-vergence's menu (its run.py's PARAMETERS): the check task runs on no
   * file, the other two on their own files, each with a shorter twin. */
  const KDE = Object.freeze({
    ...PROJECT,
    tasks: [
      { name: 'kde-vergence-check', params: null },
      { name: 'kde-vergence-pursuit', params: 'configs/task-pursuit.yaml' },
      { name: 'kde-vergence-report', params: 'configs/task-report.yaml' },
    ],
    default_task: 'kde-vergence-check',
    configs: [
      'configs/task-pursuit-pilot.yaml', 'configs/task-pursuit.yaml',
      'configs/task-report-pilot.yaml', 'configs/task-report.yaml',
    ],
    parameter_sets: [
      { label: 'Vergence check', task: 'kde-vergence-check', params: null },
      { label: 'Pursuit', task: 'kde-vergence-pursuit', params: 'configs/task-pursuit.yaml' },
      {
        label: 'Pursuit (less trials)', task: 'kde-vergence-pursuit',
        params: 'configs/task-pursuit-pilot.yaml',
      },
      { label: 'Report', task: 'kde-vergence-report', params: 'configs/task-report.yaml' },
      {
        label: 'Report (less trials)', task: 'kde-vergence-report',
        params: 'configs/task-report-pilot.yaml',
      },
    ],
    default_parameter_set: 'Vergence check',
  });
  const CONFIGS = {
    'configs/task-pursuit-pilot.yaml': { text: 'speed: 1\n', values: { speed: 1 } },
    'configs/task-pursuit.yaml': { text: 'speed: 2\n', values: { speed: 2 } },
    'configs/task-report-pilot.yaml': { text: 'gap: 3\n', values: { gap: 3 } },
    'configs/task-report.yaml': { text: 'gap: 4\n', values: { gap: 4 } },
  };
  const SCHEMAS = {
    'kde-vergence-check': {}, 'kde-vergence-pursuit': {}, 'kde-vergence-report': {},
  };

  async function kdePage() {
    const app = await pageWith({ project: KDE, configs: CONFIGS, schemas: SCHEMAS });
    chooseMode(app, 'movie');
    return app;
  }

  async function chooseSet(app, label) {
    app.byId('params-config').value = label;
    app.byId('params-config').fire('change');
    await settle();
  }

  /** Launch, and return what was posted; clears earlier posts first. */
  async function launchBody(app) {
    app.server.posted.length = 0;
    await launch(app);
    return launched(app);
  }

  it('opens on the check, with no file, and launches it without parameters', async () => {
    const app = await kdePage();
    const menu = app.byId('params-config');
    assert.equal(menu.value, 'Vergence check');
    assert.deepEqual(plain(menu.children.map((o) => o.value)), [
      'Pursuit', 'Pursuit (less trials)', 'Report', 'Report (less trials)', 'Vergence check',
    ]);
    assert.match(app.byId('parameter-fields').textContent,
      /^Vergence check has no parameter file: kde-vergence-check runs on the defaults/);
    assert.equal(app.byId('parameters-help').hidden, true);
    const body = await launchBody(app);
    assert.equal(body.task, 'kde-vergence-check');
    assert.equal(body.parameter_set, 'Vergence check');
    assert.equal('parameters' in body, false);
    assert.equal('parameters_yaml' in body, false);
  });

  it('pairs every entry with its own task, back and forth, so no file reaches another task',
    async () => {
      const app = await kdePage();
      await chooseSet(app, 'Pursuit');
      assert.deepEqual(plain(app.run('values')), { speed: 2 });
      assert.equal(app.byId('parameters-help').hidden, false);
      let body = await launchBody(app);
      assert.equal(body.task, 'kde-vergence-pursuit');
      assert.deepEqual(body.parameters, { speed: 2 });

      await chooseSet(app, 'Vergence check');
      assert.equal(app.run('values'), null);
      body = await launchBody(app);
      assert.equal(body.task, 'kde-vergence-check');
      assert.equal('parameters' in body, false);

      await chooseSet(app, 'Report (less trials)');
      body = await launchBody(app);
      assert.equal(body.task, 'kde-vergence-report');
      assert.deepEqual(body.parameters, { gap: 3 });

      await chooseSet(app, 'Pursuit (less trials)');
      body = await launchBody(app);
      assert.equal(body.task, 'kde-vergence-pursuit');
      assert.deepEqual(body.parameters, { speed: 1 });
    });
});

describe('a task whose parameter choices cannot be read', () => {
  const TRACEBACK = 'Cannot read task parameter choices: Traceback (most recent call last):\n'
    + '  File "workspace_schema.py", line 86, in task_schema\n'
    + '    cls = _value(keywords["task_class"], namespace, "task_class")\n'
    + "ValueError: run.py's task_class=task_class is not a module-level name\n";

  it('says so in one sentence and folds the traceback away', async () => {
    const app = loadWorkspace();
    app.server.state = { projects: [PROJECT], runs: [], active: null };
    app.server.configs = { 'configs/task.yaml': { text: 'trials: 4\n', values: { trials: 4 } } };
    app.server.rigs = { 'configs/rig-mac.yaml': rig(true) };
    app.server.reject = (url) => (
      url.startsWith('/api/schema') ? response({ error: TRACEBACK }, 400) : undefined);
    await app.run('refresh()');
    await settle();
    const notice = app.byId('choices-notice');
    assert.equal(notice.hidden, false);
    const [sentence, details] = notice.children;
    assert.equal(
      sentence.textContent,
      "ValueError: run.py's task_class=task_class is not a module-level name — the dashboard "
      + 'cannot read this task’s parameter choices. The fields show the file’s values as they '
      + 'are; use the text editor for others.',
    );
    assert.doesNotMatch(sentence.textContent, /Traceback/);
    assert.equal(details.localName, 'details');
    assert.equal(details.open, false);
    assert.equal(details.querySelector('pre').textContent, TRACEBACK);
    /* Not a reason to block a launch: the file's values are still there. */
    assert.deepEqual(plain(app.run('values')), { trials: 4 });
    assert.equal(app.byId('launch').disabled, false);
    /* A later read that works clears it. */
    app.server.reject = () => undefined;
    await app.run("loadSchema('p')");
    await settle();
    assert.equal(notice.hidden, true);
  });

  it('shows a one-line error as it is, with nothing to fold', async () => {
    const app = await pageWith();
    app.run("showChoicesError('Reading the task’s parameter choices timed out')");
    const notice = app.byId('choices-notice');
    assert.equal(notice.children.length, 1);
    assert.match(notice.textContent, /^Reading the task’s parameter choices timed out — /);
  });

  it('does not speak of a file’s values when the project has no file', async () => {
    const app = await pageWith({ project: { ...PROJECT, configs: [] } });
    app.run("showChoicesError('ValueError: bad')");
    assert.equal(
      app.byId('choices-notice').textContent,
      'ValueError: bad — the dashboard cannot read this task’s parameter choices.',
    );
  });
});

describe('the PsychoPy warning in the launch footer', () => {
  /* The owner's first Demo from the dashboard ran with an interpreter that
   * had alhazen but no PsychoPy and ended in a traceback. The page now says
   * so before the launch, from what the interpreter probe recorded. It warns
   * and never blocks: which launches need PsychoPy is inferred. */
  const WITHOUT = { ...PROJECT, python: 'C:/envs/plain/python.exe', psychopy_version: null };
  const WITH = { ...PROJECT, psychopy_version: '2026.2.4' };
  const SIMULATED_RIG = rig(true, false);
  SIMULATED_RIG.values.display = { backend: 'simulated' };

  function note(app) {
    return app.byId('launch-note');
  }

  it('names the interpreter and the fix for a demo it cannot open', async () => {
    const app = await pageWith({ project: WITHOUT });
    chooseMode(app, 'demo');
    const text = note(app).textContent;
    assert.match(text, /^Demo opens a PsychoPy window/);
    assert.match(text, /C:\/envs\/plain\/python\.exe/);
    assert.match(text, /pip install "alhazen-vision\[psychopy\]"/);
    assert.match(text, /Project settings/);
    assert.equal(note(app).classList.contains('launch-warning'), true);
    /* A warning, not a refusal. */
    assert.equal(app.byId('launch').disabled, false);
  });

  it('says nothing when the interpreter has PsychoPy', async () => {
    const app = await pageWith({ project: WITH });
    for (const mode of ['demo', 'measure', 'test', 'run', 'simulate']) {
      chooseMode(app, mode);
      assert.doesNotMatch(note(app).textContent, /PsychoPy/, mode);
      assert.equal(note(app).classList.contains('launch-warning'), false, mode);
    }
  });

  it('asks for a re-registration when the record predates the check', async () => {
    /* PROJECT has no psychopy_version: registered before the probe asked,
     * which is unknown, not "not installed". */
    const app = await pageWith();
    chooseMode(app, 'measure');
    const text = note(app).textContent;
    assert.match(text, /^Measure rig opens a PsychoPy window/);
    assert.match(text, /unknown/);
    assert.match(text, /Re-register to check/);
    assert.doesNotMatch(text, /has no PsychoPy/);
  });

  it('follows the rig’s display backend for sessions', async () => {
    const app = await pageWith({ project: WITHOUT });
    /* A simulation with a window; headless is the next test's. */
    app.byId('headless').checked = false;
    /* The mac rig says no backend: the model's default, psychopy. */
    for (const mode of ['test', 'run', 'simulate']) {
      chooseMode(app, mode);
      assert.match(note(app).textContent, /opens a PsychoPy window/, mode);
    }
    const simulated = await pageWith({
      project: WITHOUT, rigs: { 'configs/rig-mac.yaml': SIMULATED_RIG },
    });
    simulated.byId('headless').checked = false;
    for (const mode of ['test', 'run', 'simulate']) {
      chooseMode(simulated, mode);
      assert.doesNotMatch(note(simulated).textContent, /PsychoPy/, mode);
    }
    /* Demo draws through PsychoPy whatever the rig's backend says. */
    chooseMode(simulated, 'demo');
    assert.match(note(simulated).textContent, /opens a PsychoPy window/);
  });

  it('drops the warning for a headless simulation, and for movies', async () => {
    const app = await pageWith({ project: WITHOUT });
    chooseMode(app, 'simulate');
    /* Headless is the form's default for simulate: no window, no warning. */
    assert.equal(app.byId('headless').checked, true);
    assert.doesNotMatch(note(app).textContent, /PsychoPy/);
    app.byId('headless').checked = false;
    app.byId('headless').fire('change');
    assert.match(note(app).textContent, /PsychoPy/);
    app.byId('headless').checked = true;
    app.byId('headless').fire('change');
    assert.doesNotMatch(note(app).textContent, /PsychoPy/);
    assert.equal(note(app).classList.contains('launch-warning'), false);
    chooseMode(app, 'movie');
    assert.doesNotMatch(note(app).textContent, /PsychoPy/);
  });

  it('gives way to an active run’s note, which is about what can launch at all', async () => {
    const run = runDetail({ status: 'running' });
    const app = await pageWith({ project: WITHOUT, run: run });
    chooseMode(app, 'demo');
    assert.match(note(app).textContent, /One run at a time/);
    assert.equal(note(app).classList.contains('launch-warning'), false);
  });
});

describe('a development rig in the launch form', () => {
  /* docs/rigs.md §5: run mode refuses a rig whose settings say
   * real_data: false — the laptop the Rig menu opens on. The page says so
   * under the rig and above the launch, and shows the server's refusal when
   * a launch is sent anyway; it warns and never blocks, because the server
   * makes the check. PsychoPy is present, so its warning stays out of it. */
  const LAPTOP = Object.freeze({
    ...PROJECT, psychopy_version: '2026.2.4', rigs: [sharedEntry('lab'), sharedEntry('laptop')],
  });
  const DEVELOPMENT = rig(true, false, { name: 'laptop', source: 'alhazen' });
  DEVELOPMENT.values.real_data = false;
  const RIGS = {
    'alhazen/laptop': DEVELOPMENT,
    'alhazen/lab': rig(true, false, { name: 'lab', source: 'alhazen' }),
  };

  async function chooseRig(app, value) {
    app.byId('rig').value = value;
    app.byId('rig').fire('change');
    await settle();
  }

  function note(app) {
    return app.byId('launch-note');
  }

  it('says under the rig that real data is refused there, and only for such a rig', async () => {
    const app = await pageWith({ project: LAPTOP, rigs: RIGS });
    assert.equal(app.byId('rig').value, 'alhazen/laptop');
    assert.equal(facts(app)['Real data'], 'refused · a development rig (real_data: false)');
    await chooseRig(app, 'alhazen/lab');
    assert.equal('Real data' in facts(app), false);
  });

  it('warns in Run mode before the launch, names the rig and the way on, and still launches',
    async () => {
      const app = await pageWith({ project: LAPTOP, rigs: RIGS });
      chooseMode(app, 'run');
      assert.equal(
        note(app).textContent,
        'Run experiment records real data, and alhazen/laptop is a development rig '
          + '(real_data: false): this launch will be refused before anything is written. '
          + 'Choose a rig that collects real data, or Test session or Simulate to try the '
          + 'session on this machine.',
      );
      assert.equal(note(app).classList.contains('launch-warning'), true);
      assert.equal(app.byId('launch').disabled, false);
    });

  it('says nothing of it in the modes that run there', async () => {
    const app = await pageWith({ project: LAPTOP, rigs: RIGS });
    for (const mode of ['test', 'simulate', 'demo', 'movie', 'measure']) {
      chooseMode(app, mode);
      assert.doesNotMatch(note(app).textContent, /development rig/, mode);
      assert.equal(note(app).classList.contains('launch-warning'), false, mode);
    }
  });

  it('gives a collecting rig in Run mode the usual reminder', async () => {
    const app = await pageWith({ project: LAPTOP, rigs: RIGS });
    await chooseRig(app, 'alhazen/lab');
    chooseMode(app, 'run');
    assert.match(note(app).textContent, /^This mode records real subject data/);
    assert.equal(note(app).classList.contains('launch-warning'), false);
  });

  it('shows the server’s refusal in the error banner', async () => {
    const app = await pageWith({ project: LAPTOP, rigs: RIGS });
    const refusal = 'run mode records real data, and alhazen/laptop (alhazen’s shared rig) is '
      + 'a development rig: its settings say `real_data: false`. Nothing was started and '
      + 'nothing was written.';
    app.server.launch = () => { throw new Error(refusal); };
    chooseMode(app, 'run');
    app.byId('subject').value = '01';
    app.byId('initials').value = 'HD';
    await launch(app);
    assert.equal(app.byId('error').hidden, false);
    assert.equal(app.byId('error').textContent, refusal);
    assert.equal(app.run('launching'), false);
  });
});

describe('the Rig summary\'s Reward line', () => {
  const line = { backend: 'nidaq', device: 'Dev1', channel: 'ao0', voltage: 5 };

  it('shows each width measured on the rig\'s own line and voltage, in µL', () => {
    const app = loadWorkspace();
    const fact = plain(app.run(`rewardFact(${JSON.stringify(line)}, ${JSON.stringify({
      200: { ul_per_pulse: 118.46, line: 'Dev1/ao0', voltage: 5, measured_at: '2026-10-08T12:00:00+00:00' },
      100: { ul_per_pulse: 40, line: 'Dev2/ao0', voltage: 5, measured_at: null },
    })})`));
    assert.deepEqual(fact, [['Reward', ['Dev1/ao0 at 5 V', '118.5 µL per 200 ms pulse (2026-10-08)']]]);
  });

  it('claims no volume when nothing was measured, and says nothing without a line', () => {
    const app = loadWorkspace();
    assert.deepEqual(plain(app.run(`rewardFact(${JSON.stringify(line)}, null)`)),
      [['Reward', ['Dev1/ao0 at 5 V', 'volume not measured (Measure rig, Reward)']]]);
    assert.deepEqual(plain(app.run('rewardFact(undefined, null)')), []);
  });
});
