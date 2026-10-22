/* This machine's rig configs on the Rigs page (?view=rigs, workspace_manage.js
 * showRigs, docs/design/machine-rig-configs.md §3), which belongs to no
 * experiment: every config with
 * its default mark, whether it loads and its sync state in words; Add, Edit
 * (settings or YAML), Rename, Make default, Delete asked in place, History;
 * a sync conflict shown side by side with Keep this machine's / Keep the
 * platform's, a refused platform edit with its reason. The real scripts run
 * on the fake page (load_workspace.mjs) against the fake launcher. */
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { loadWorkspace, plain, settle } from './load_workspace.mjs';

const PROJECT = Object.freeze({
  id: 'p', name: 'demo-folder', title: 'Demo task', slug: 'demo', title_error: null,
  path: 'C:/projects/demo', python: 'python', available: true,
  rigs: [], rigs_note: null, configs: ['configs/task.yaml'], scripts: [],
  parameter_sets: [{ label: 'task', task: null, params: 'configs/task.yaml' }],
  default_parameter_set: 'task', parameter_sets_error: null,
  machine_rig: { id: 'c-booth', name: 'booth', sha256: 'sha-booth', data_root: 'data' },
  machine_rigs: { default: 'c-booth', configs: [{ id: 'c-booth', name: 'booth', default: true, problem: null }] },
});

const VALUES = {
  monitor: { width_px: 1920, height_px: 1080, refresh_rate_hz: 120, width_cm: 52, distance_cm: 57 },
  devices: { eyetracker: { backend: 'eyelink' } },
  data_root: 'data',
  real_data: true,
};

function config(overrides = {}) {
  return {
    id: 'c-booth', name: 'booth', default: true, file: 'C:/state/machine-rig/rig-booth.yaml', folder: '.',
    text: '# the booth\nmonitor:\n  width_px: 1920\n', sha256: 'sha-booth', values: JSON.parse(JSON.stringify(VALUES)),
    problem: null, revision: 3, created: '2026-10-01T10:00:00', origin: null,
    saves: [{ at: '2026-10-20T09:00:00', by: 'dashboard', sha256: 'sha-booth', changes: ['monitor.distance_cm'] }],
    reward_calibration: null, gamma: null, legacy: true,
    sync: { state: 'synced', platform_revision: 7, rejected: null, conflict: null },
    ...overrides,
  };
}

function rigs(configs, overrides = {}) {
  return {
    machine: { id: 'mc_0123456789abcdef', label: 'bench' },
    default: configs.find((c) => c.default)?.id ?? null,
    configs,
    sync: { connected: true, state: 'synced', at: '2026-10-21T05:00:00', error: null, platform: 'Mana' },
    migrated: null, error: null,
    ...overrides,
  };
}

/** The Rigs page drawn with `answer` as /api/machine-rigs (PROJECT registered). */
async function rigsPage(answer, setup = () => {}) {
  const app = loadWorkspace({ search: '?view=rigs' });
  app.server.state = { projects: [PROJECT], runs: [], active: null };
  app.server.machineRigs = answer;
  app.server.configs = { 'configs/task.yaml': { text: 'trials: 4\n', values: { trials: 4 } } };
  app.server.rigs = { booth: { name: 'booth', source: 'machine', extends: null, values: VALUES } };
  setup(app);
  await app.run('refresh()');
  await settle();
  await settle();
  const panel = app.document.querySelectorAll('section').find((s) => s.getAttribute('id') === 'm-machine');
  assert.ok(panel, 'the rig configs panel is drawn');
  return { app, panel };
}

const rowOf = (panel, id) => panel.querySelectorAll('article').find((a) => a.dataset.config === id);
/* Sync now and Add a config sit in the page's heading, beside "Rigs". */
const actionIn = (app, text) => app.byId('rigs-view').querySelectorAll('button').find((b) => b.textContent === text);
const buttonIn = (where, text) => where.querySelectorAll('button').find((b) => b.textContent === text);

describe('this machine’s rig configs on the Rigs page', () => {
  it('lists every config with its default mark, whether it loads, its sync state and last save', async () => {
    const { panel } = await rigsPage(rigs([
      config(),
      config({ id: 'c-bench', name: 'bench', default: false, legacy: false, sync: { state: 'local', platform_revision: 2 } }),
      config({ id: 'c-bad', name: 'bad', default: false, legacy: false, problem: 'monitor.width_px: must be positive',
        sync: { state: 'not-synced', platform_revision: null } }),
    ]));
    const booth = rowOf(panel, 'c-booth');
    assert.match(booth.textContent, /booth.*Default/);
    assert.match(booth.textContent, /Ready to run/);
    assert.match(booth.textContent, /Synced/);
    assert.match(booth.textContent, /from the dashboard \(monitor\.distance_cm\)/);
    assert.match(booth.textContent, /A 3\.5\.0 dashboard on this state folder still runs this config/);
    assert.equal(buttonIn(booth, 'Delete').disabled, true);
    assert.equal(buttonIn(booth, 'Make default'), undefined);
    assert.match(rowOf(panel, 'c-bench').textContent, /Changed here, not sent yet/);
    const bad = rowOf(panel, 'c-bad');
    assert.match(bad.textContent, /Does not load/);
    assert.match(bad.textContent, /monitor\.width_px: must be positive/);
    assert.match(bad.textContent, /Only on this machine/);
    assert.match(panel.querySelector('.mrc-connection').textContent, /^benchSynced with the platform \(Mana\) at/);
    /* No experiment is open: a relative data folder is each experiment's own. */
    assert.match(booth.textContent, /Data folderdata, inside each experiment’s own folder/);
    assert.doesNotMatch(panel.textContent, /C:\/projects\/demo/);
  });

  it('makes a config the default, and tells the Run page', async () => {
    const { app, panel } = await rigsPage(rigs([config(), config({ id: 'c-bench', name: 'bench', default: false, legacy: false })]));
    app.server.rigWrites = (path, body) => (path === '/api/machine-rigs/default'
      ? rigs([config({ default: false }), config({ id: 'c-bench', name: 'bench', default: true, legacy: false })]) : undefined);
    buttonIn(rowOf(panel, 'c-bench'), 'Make default').fire('click');
    await settle();
    assert.deepEqual(plain(app.server.posted.at(-1)), { path: '/api/machine-rigs/default', body: { id: 'c-bench' } });
    assert.match(rowOf(panel, 'c-bench').textContent, /Default/);
    assert.match(panel.textContent, /bench is the default now/);
  });

  it('renames a config, refusing a name another config has', async () => {
    const { app, panel } = await rigsPage(rigs([config(), config({ id: 'c-bench', name: 'bench', default: false, legacy: false })]));
    const row = rowOf(panel, 'c-bench');
    buttonIn(row, 'Rename').fire('click');
    const form = row.querySelector('form');
    const name = form.querySelector('input');
    name.value = 'booth';
    form.fire('submit', { preventDefault() {} });
    await settle();
    assert.match(form.textContent, /already has a config named booth/);
    app.server.rigWrites = (path, body) => (path === '/api/machine-rigs/rename'
      ? rigs([config(), config({ id: 'c-bench', name: body.name, default: false, legacy: false })]) : undefined);
    name.value = 'bench-2';
    form.fire('submit', { preventDefault() {} });
    await settle();
    assert.deepEqual(plain(app.server.posted.at(-1).body), { id: 'c-bench', name: 'bench-2', sha256: 'sha-booth' });
    assert.match(panel.textContent, /Renamed bench to bench-2\./);
  });

  it('deletes a config only after asking in place, saying where its folder goes', async () => {
    const { app, panel } = await rigsPage(rigs([config(), config({ id: 'c-bench', name: 'bench', default: false, legacy: false })]));
    const row = rowOf(panel, 'c-bench');
    buttonIn(row, 'Delete').fire('click');
    assert.equal(app.server.posted.length, 0);
    assert.match(row.textContent, /Its folder moves to trash\/ in this dashboard’s state folder/);
    assert.match(row.textContent, /sessions already run keep their own copies of it/);
    app.server.rigWrites = (path) => (path === '/api/machine-rigs/delete' ? rigs([config()]) : undefined);
    buttonIn(row, 'Delete bench').fire('click');
    await settle();
    assert.deepEqual(plain(app.server.posted.at(-1)), { path: '/api/machine-rigs/delete', body: { id: 'c-bench', sha256: 'sha-booth' } });
    assert.equal(rowOf(panel, 'c-bench'), undefined);
    assert.match(panel.textContent, /Deleted bench; its folder is in trash\//);
  });

  it('edits a config by its settings, saying the comments go, and sends the values', async () => {
    const { app, panel } = await rigsPage(rigs([config()]));
    const row = rowOf(panel, 'c-booth');
    buttonIn(row, 'Edit').fire('click');
    assert.match(row.textContent, /the comments in it are dropped/);
    const distance = row.querySelectorAll('.parameter-row').find((r) => r.dataset.key === 'monitor.distance_cm');
    const input = distance.querySelector('input');
    input.value = '60';
    input.fire('input');
    input.fire('change');
    /* real_data is the config's to edit here, unlike on the Run page. */
    const real = row.querySelectorAll('.parameter-row').find((r) => r.dataset.key === 'real_data');
    assert.equal(real.querySelector('input').type, 'checkbox');
    app.server.rigWrites = (path, body) => (path === '/api/machine-rigs/save'
      ? { ...rigs([config({ sha256: 'sha-2', values: body.values })]), saved: ['C:/state/machine-rig/rig-booth.yaml'] } : undefined);
    buttonIn(row, 'Save settings').fire('click');
    await settle();
    const post = app.server.posted.at(-1);
    assert.equal(post.path, '/api/machine-rigs/save');
    assert.equal(post.body.id, 'c-booth');
    assert.equal(post.body.sha256, 'sha-booth');
    assert.equal(post.body.values.monitor.distance_cm, 60);
    assert.equal(post.body.text, undefined);
    assert.match(panel.textContent, /Saved booth/);
  });

  it('edits a config as YAML, checks it, and says plainly when the file changed since it was opened', async () => {
    const { app, panel } = await rigsPage(rigs([config()]));
    const row = rowOf(panel, 'c-booth');
    buttonIn(row, 'Edit').fire('click');
    buttonIn(row, 'YAML').fire('click');
    const text = row.querySelector('textarea');
    assert.match(text.value, /# the booth/);
    text.value = 'monitor:\n  width_px: 1280\n';
    text.fire('input');
    app.server.rigWrites = (path, body) => {
      if (path === '/api/machine-rigs/check') return { ok: true, changes: [{ path: 'monitor.width_px' }] };
      if (path === '/api/machine-rigs/save') return { status: 409, json: { error: 'rig-booth.yaml changed since it was read' } };
      return undefined;
    };
    buttonIn(row, 'Check').fire('click');
    await settle();
    assert.match(row.textContent, /Valid\. Changes: monitor\.width_px\./);
    buttonIn(row, 'Save YAML').fire('click');
    await settle();
    assert.deepEqual(plain(app.server.posted.at(-1).body), { id: 'c-booth', sha256: 'sha-booth', text: 'monitor:\n  width_px: 1280\n' });
    assert.match(row.textContent, /booth changed since you opened it/);
    assert.ok(buttonIn(row, 'Reload'));
    assert.equal(app.run('window.WorkspaceManage.dirty()'), true);
  });

  it('shows a conflict side by side and keeps the version chosen', async () => {
    const conflicted = config({ sync: { state: 'conflict', platform_revision: 7, rejected: null,
      conflict: { platform_text: 'monitor:\n  width_px: 2560\n', platform_sha256: 'sha-p', platform_revision: 9, at: '2026-10-21T04:00:00', by: 'platform' } } });
    const { app, panel } = await rigsPage(rigs([conflicted]));
    const row = rowOf(panel, 'c-booth');
    assert.match(row.textContent, /Conflict/);
    assert.match(row.textContent, /changed here and on the platform since they last agreed/);
    assert.match(row.textContent, /This machine’s version/);
    assert.match(row.textContent, /The platform’s version/);
    assert.match(row.textContent, /width_px: 2560/);
    /* The lines that differ are marked, in each version. */
    assert.deepEqual(row.querySelectorAll('mark').map((m) => m.textContent), ['# the booth\n', '  width_px: 1920\n', '  width_px: 2560\n']);
    app.server.rigWrites = (path) => (path === '/api/machine-rigs/resolve' ? rigs([config()]) : undefined);
    buttonIn(row, 'Keep the platform’s').fire('click');
    await settle();
    assert.deepEqual(plain(app.server.posted.at(-1)), { path: '/api/machine-rigs/resolve', body: { id: 'c-booth', keep: 'platform' } });
    assert.match(panel.textContent, /Kept the platform’s booth/);
  });

  it('says a refused platform edit, its reason and that this machine runs its own version', async () => {
    const { panel } = await rigsPage(rigs([config({ sync: { state: 'rejected', platform_revision: 8,
      rejected: { revision: 8, reason: 'monitor.width_px: must be positive', text: 'monitor:\n  width_px: -1\n' }, conflict: null } })]));
    const row = rowOf(panel, 'c-booth');
    assert.match(row.textContent, /Platform edit refused: monitor\.width_px: must be positive/);
    assert.match(row.textContent, /This machine still runs its own version/);
    assert.match(row.textContent, /width_px: -1/);
  });

  it('adds a config from a template, the only place shared rigs appear', async () => {
    const { app, panel } = await rigsPage(rigs([config()]), (a) => {
      a.server.templates = { suggested: null, templates: [
        { id: 'alhazen/lab', label: 'alhazen/lab', kind: 'shared', name: 'lab', summary: '1920 × 1080 px', real_data: true, events: [] },
      ] };
    });
    assert.doesNotMatch(panel.textContent, /alhazen\/lab/);
    actionIn(app, 'Add a config').fire('click');
    await settle();
    const select = panel.querySelector('select');
    assert.deepEqual(select.children.map((g) => g.label), ['This machine’s configs', 'alhazen’s shared rigs']);
    select.value = 'template:alhazen/lab';
    select.fire('change');
    app.server.rigWrites = (path, body) => (path === '/api/machine-rigs/create'
      ? { ...rigs([config(), config({ id: 'c-lab', name: body.name, default: false, legacy: false })]), created: 'c-lab' } : undefined);
    panel.querySelector('form').fire('submit', { preventDefault() {} });
    await settle();
    assert.deepEqual(plain(app.server.posted.at(-1).body), { name: 'lab', template: 'alhazen/lab' });
    assert.ok(rowOf(panel, 'c-lab'));
    assert.match(panel.textContent, /Added lab\./);
  });

  it('says how the machine is connected, and Sync now asks for a sync', async () => {
    const offline = await rigsPage(rigs([config({ sync: { state: 'local', platform_revision: 7 } })],
      { sync: { connected: true, state: 'offline', at: null, error: 'connection refused', platform: 'Mana' } }));
    assert.match(offline.panel.textContent, /could not be reached: connection refused/);
    offline.app.server.rigWrites = (path) => (path === '/api/machine-rigs/sync' ? rigs([config()]) : undefined);
    actionIn(offline.app, 'Sync now').fire('click');
    await settle();
    assert.equal(offline.app.server.posted.at(-1).path, '/api/machine-rigs/sync');
    assert.match(offline.panel.textContent, /Synced with the platform \(Mana\)/);
    const alone = await rigsPage(rigs([config()], { sync: { connected: false, state: 'not-connected', at: null, error: null, platform: null } }));
    assert.match(alone.panel.textContent, /Not connected to a platform/);
    assert.equal(actionIn(alone.app, 'Sync now').hidden, true);
  });

  it('says once that the 3.5.0 rig became a config', async () => {
    const { app, panel } = await rigsPage(rigs([config()], { migrated: { from: '3.5.0', config: 'c-booth' } }));
    assert.match(panel.textContent, /Your rig from 3\.5\.0 is now the config booth, the default/);
    buttonIn(panel, 'Dismiss').fire('click');
    assert.equal(app.run("localStorage.getItem('alhazen-mrc-migrated:mc_0123456789abcdef')"), '1');
  });

  it('lists a config’s saves and shows an earlier version', async () => {
    const { panel } = await rigsPage(rigs([config()]), (a) => {
      a.server.rigHistory = { saves: [{ at: '2026-10-20T09:00:00', by: 'platform', sha256: 's', changes: ['monitor.width_px'] }],
        versions: [{ file: 'rig-booth.20261019.yaml', at: '2026-10-19T09:00:00', size: 120 }] };
      a.server.rigVersions = { 'rig-booth.20261019.yaml': 'monitor:\n  width_px: 1024\n' };
    });
    const row = rowOf(panel, 'c-booth');
    buttonIn(row, 'History').fire('click');
    await settle();
    assert.match(row.textContent, /monitor\.width_px/);
    buttonIn(row, 'View').fire('click');
    await settle();
    assert.match(row.textContent, /width_px: 1024/);
  });

  it('opens the config the Run page’s Edit names', async () => {
    const { panel } = await rigsPage(rigs([config(), config({ id: 'c-bench', name: 'bench', default: false, legacy: false })]), (a) => {
      a.run("window.WorkspaceManage.focusRig('c-bench')");
    });
    assert.match(rowOf(panel, 'c-bench').textContent, /Edit bench/);
    assert.doesNotMatch(rowOf(panel, 'c-booth').textContent, /Edit booth/);
  });
});

describe('the Rigs page in the workspace', () => {
  const OTHER = Object.freeze({ ...PROJECT, id: 'q', title: 'Other task', slug: 'other', path: 'C:/projects/other' });

  /** The page at `search` with PROJECT and OTHER registered and two configs. */
  async function pageAt(search) {
    const app = loadWorkspace({ search });
    app.server.state = { projects: [PROJECT, OTHER], runs: [], active: null };
    app.server.machineRigs = rigs([config(), config({ id: 'c-bench', name: 'bench', default: false, legacy: false })]);
    app.server.configs = { 'configs/task.yaml': { text: 'trials: 4\n', values: { trials: 4 } } };
    app.server.rigs = { booth: { name: 'booth', source: 'machine', extends: null, values: VALUES } };
    await app.run('refresh()');
    await settle();
    await settle();
    return app;
  }
  const machinePanel = (app) => app.document.querySelectorAll('section').find((s) => s.getAttribute('id') === 'm-machine');
  const click = async (element) => {
    element.fire('click', { preventDefault() {}, button: 0 });
    await settle();
    await settle();
  };

  it('is its own page in the sidebar, beside Experiments, with no experiment open', async () => {
    const app = await pageAt('?view=rigs');
    assert.equal(app.byId('nav-rigs').href, '/?view=rigs');
    assert.equal(app.byId('nav-rigs').getAttribute('aria-current'), 'page');
    assert.equal(app.byId('nav-experiments').getAttribute('aria-current'), 'false');
    assert.equal(app.byId('rigs-view').hidden, false);
    assert.equal(app.byId('home-view').hidden, true);
    assert.equal(app.byId('project-heading').hidden, true);
    assert.equal(app.byId('workspace').hidden, true);
    assert.equal(app.byId('general-view').hidden, true);
    assert.equal(app.byId('breadcrumb').textContent, 'Rigs');
    assert.equal(app.document.title, 'Rigs · Alhazen');
    assert.equal(app.byId('rigs-view').querySelector('h1').textContent, 'Rigs');
    /* Both experiments stay in the sidebar, neither of them open. */
    assert.equal(app.byId('experiment-nav').querySelector('.current'), null);
    assert.equal(app.byId('experiment-nav').querySelectorAll('.nav-group').length, 2);
    assert.ok(rowOf(machinePanel(app), 'c-booth'));
    assert.ok(rowOf(machinePanel(app), 'c-bench'));
    /* A poll does not draw the page again (an open editor would be lost). */
    buttonIn(rowOf(machinePanel(app), 'c-bench'), 'Edit').fire('click');
    await app.run('refresh()');
    await settle();
    assert.match(rowOf(machinePanel(app), 'c-bench').textContent, /Edit bench/);
  });

  it('is reached from the sidebar, and Back and Forward move between it and the page before', async () => {
    const app = await pageAt('?project=p&view=run');
    assert.equal(app.byId('workspace').hidden, false);
    await click(app.byId('nav-rigs'));
    assert.equal(app.location.search, '?view=rigs');
    assert.equal(app.byId('rigs-view').hidden, false);
    assert.equal(app.byId('workspace').hidden, true);
    assert.equal(app.byId('project-heading').hidden, true);
    assert.equal(app.byId('nav-rigs').getAttribute('aria-current'), 'page');
    app.back();
    await settle();
    await settle();
    assert.equal(app.byId('workspace').hidden, false);
    assert.equal(app.byId('rigs-view').hidden, true);
    assert.equal(app.byId('nav-rigs').getAttribute('aria-current'), 'false');
    app.forward();
    await settle();
    await settle();
    assert.equal(app.byId('rigs-view').hidden, false);
    assert.ok(rowOf(machinePanel(app), 'c-booth'));
    /* The Experiments link leaves it for the Experiments page. */
    await click(app.byId('nav-experiments'));
    assert.equal(app.location.search, '?view=experiments');
    assert.equal(app.byId('rigs-view').hidden, true);
    assert.equal(app.byId('home-view').hidden, false);
  });

  it('asks before leaving unsaved edits to a config', async () => {
    const app = await pageAt('?view=rigs');
    const row = rowOf(machinePanel(app), 'c-booth');
    buttonIn(row, 'Edit').fire('click');
    buttonIn(row, 'YAML').fire('click');
    const text = row.querySelector('textarea');
    text.value = 'monitor:\n  width_px: 1280\n';
    text.fire('input');
    app.byId('nav-experiments').fire('click', { preventDefault() {}, button: 0 });
    await settle();
    const bar = app.byId('m-rigs-leave');
    assert.equal(bar.hidden, false);
    assert.match(bar.textContent, /unsaved changes/);
    assert.equal(app.location.search, '?view=rigs');
    buttonIn(bar, 'Stay').fire('click');
    await settle();
    assert.equal(app.location.search, '?view=rigs');
    assert.equal(app.byId('rigs-view').hidden, false);
  });

  it('is no longer part of an experiment’s General page, which points to it in one line', async () => {
    const app = await pageAt('?project=p&view=general');
    assert.equal(app.byId('general-view').hidden, false);
    assert.equal(machinePanel(app), undefined);
    assert.doesNotMatch(app.byId('general-view').textContent, /THIS MACHINE|Add a config|Sync now/);
    const pointer = app.byId('m-rigs-pointer');
    assert.match(pointer.textContent, /This machine’s rig configs are shared by every experiment: Rigs\./);
    const to = pointer.querySelector('a');
    assert.equal(to.href, '/?view=rigs');
    await click(to);
    assert.equal(app.location.search, '?view=rigs');
    assert.equal(app.byId('general-view').hidden, true);
    assert.ok(rowOf(machinePanel(app), 'c-booth'));
  });

  it('opens the Run page’s config on the Rigs page from Edit this config, its editor open', async () => {
    const app = await pageAt('?project=p&view=run');
    const edit = app.byId('rig-machine-edit');
    assert.equal(edit.hidden, false);
    await click(edit);
    assert.equal(app.location.search, '?view=rigs');
    assert.equal(app.byId('rigs-view').hidden, false);
    assert.match(rowOf(machinePanel(app), 'c-booth').textContent, /Edit booth/);
    assert.doesNotMatch(rowOf(machinePanel(app), 'c-bench').textContent, /Edit bench/);
  });

  it('sends the Run page’s notes about a config to the Rigs page', async () => {
    const app = loadWorkspace({ search: '?project=p&view=run' });
    app.server.state = { projects: [{ ...PROJECT, machine_rig: null, machine_rigs: { default: 'c-broken',
      configs: [{ id: 'c-broken', name: 'broken', default: true, problem: 'bad monitor' }] } }], runs: [], active: null };
    app.server.configs = { 'configs/task.yaml': { text: 'trials: 4\n', values: { trials: 4 } } };
    await app.run('refresh()');
    await settle();
    assert.match(app.byId('launch-note').textContent, /fix one on the Rigs page to launch/);
    assert.match(app.byId('rig-config-help').textContent, /Fix it on the Rigs page\./);
    assert.doesNotMatch(app.byId('workspace').textContent, /General page/);
  });

  it('offers every registered experiment’s rigs, by experiment, and the shared rigs in Add a config', async () => {
    const app = await pageAt('?view=rigs');
    app.server.templates = { suggested: null, templates: [
      { id: 'alhazen/lab', label: 'alhazen/lab', kind: 'shared', name: 'lab', summary: '', real_data: true, events: [] },
      { id: 'p:configs/rig-mac.yaml', label: 'demo/mac', kind: 'experiment', name: 'mac', project: 'p', summary: '', real_data: false, events: [] },
      { id: 'q:configs/rig-booth.yaml', label: 'other/booth', kind: 'experiment', name: 'booth', project: 'q', summary: '', real_data: true, events: [] },
    ] };
    actionIn(app, 'Add a config').fire('click');
    await settle();
    const select = machinePanel(app).querySelector('select');
    assert.deepEqual(select.children.map((g) => [g.label, g.children.map((o) => o.textContent)]), [
      ['This machine’s configs', ['booth', 'bench']],
      ['Registered experiments’ rigs', ['demo/mac', 'other/booth']],
      ['alhazen’s shared rigs', ['alhazen/lab']],
    ]);
  });
});
