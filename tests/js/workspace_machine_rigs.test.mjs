/* This machine's rig configs on the General page (workspace_manage.js
 * machinePanel, docs/design/machine-rig-configs.md §3): every config with
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

/** The General page drawn for PROJECT, with `answer` as /api/machine-rigs. */
async function general(answer, setup = () => {}) {
  const app = loadWorkspace({ search: '?project=p&view=general' });
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
const buttonIn = (where, text) => where.querySelectorAll('button').find((b) => b.textContent === text);

describe('this machine’s rig configs on the General page', () => {
  it('lists every config with its default mark, whether it loads, its sync state and last save', async () => {
    const { panel } = await general(rigs([
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
    assert.match(panel.textContent, /bench\. Synced with the platform \(Mana\) at/);
  });

  it('makes a config the default, and tells the Run page', async () => {
    const { app, panel } = await general(rigs([config(), config({ id: 'c-bench', name: 'bench', default: false, legacy: false })]));
    app.server.rigWrites = (path, body) => (path === '/api/machine-rigs/default'
      ? rigs([config({ default: false }), config({ id: 'c-bench', name: 'bench', default: true, legacy: false })]) : undefined);
    buttonIn(rowOf(panel, 'c-bench'), 'Make default').fire('click');
    await settle();
    assert.deepEqual(plain(app.server.posted.at(-1)), { path: '/api/machine-rigs/default', body: { id: 'c-bench' } });
    assert.match(rowOf(panel, 'c-bench').textContent, /Default/);
    assert.match(panel.textContent, /bench is the default now/);
  });

  it('renames a config, refusing a name another config has', async () => {
    const { app, panel } = await general(rigs([config(), config({ id: 'c-bench', name: 'bench', default: false, legacy: false })]));
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
    const { app, panel } = await general(rigs([config(), config({ id: 'c-bench', name: 'bench', default: false, legacy: false })]));
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
    const { app, panel } = await general(rigs([config()]));
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
    const { app, panel } = await general(rigs([config()]));
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
    const { app, panel } = await general(rigs([conflicted]));
    const row = rowOf(panel, 'c-booth');
    assert.match(row.textContent, /Conflict/);
    assert.match(row.textContent, /changed here and on the platform since they last agreed/);
    assert.match(row.textContent, /This machine’s version/);
    assert.match(row.textContent, /The platform’s version/);
    assert.match(row.textContent, /width_px: 2560/);
    app.server.rigWrites = (path) => (path === '/api/machine-rigs/resolve' ? rigs([config()]) : undefined);
    buttonIn(row, 'Keep the platform’s').fire('click');
    await settle();
    assert.deepEqual(plain(app.server.posted.at(-1)), { path: '/api/machine-rigs/resolve', body: { id: 'c-booth', keep: 'platform' } });
    assert.match(panel.textContent, /Kept the platform’s booth/);
  });

  it('says a refused platform edit, its reason and that this machine runs its own version', async () => {
    const { panel } = await general(rigs([config({ sync: { state: 'rejected', platform_revision: 8,
      rejected: { revision: 8, reason: 'monitor.width_px: must be positive', text: 'monitor:\n  width_px: -1\n' }, conflict: null } })]));
    const row = rowOf(panel, 'c-booth');
    assert.match(row.textContent, /Platform edit refused: monitor\.width_px: must be positive/);
    assert.match(row.textContent, /This machine still runs its own version/);
    assert.match(row.textContent, /width_px: -1/);
  });

  it('adds a config from a template, the only place shared rigs appear', async () => {
    const { app, panel } = await general(rigs([config()]), (a) => {
      a.server.templates = { suggested: null, templates: [
        { id: 'alhazen/lab', label: 'alhazen/lab', kind: 'shared', name: 'lab', summary: '1920 × 1080 px', real_data: true, events: [] },
      ] };
    });
    assert.doesNotMatch(panel.textContent, /alhazen\/lab/);
    buttonIn(panel, 'Add a config').fire('click');
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
    const offline = await general(rigs([config({ sync: { state: 'local', platform_revision: 7 } })],
      { sync: { connected: true, state: 'offline', at: null, error: 'connection refused', platform: 'Mana' } }));
    assert.match(offline.panel.textContent, /could not be reached: connection refused/);
    offline.app.server.rigWrites = (path) => (path === '/api/machine-rigs/sync' ? rigs([config()]) : undefined);
    buttonIn(offline.panel, 'Sync now').fire('click');
    await settle();
    assert.equal(offline.app.server.posted.at(-1).path, '/api/machine-rigs/sync');
    assert.match(offline.panel.textContent, /Synced with the platform \(Mana\)/);
    const alone = await general(rigs([config()], { sync: { connected: false, state: 'not-connected', at: null, error: null, platform: null } }));
    assert.match(alone.panel.textContent, /Not connected to a platform/);
    assert.equal(buttonIn(alone.panel, 'Sync now').hidden, true);
  });

  it('says once that the 3.5.0 rig became a config', async () => {
    const { app, panel } = await general(rigs([config()], { migrated: { from: '3.5.0', config: 'c-booth' } }));
    assert.match(panel.textContent, /Your rig from 3\.5\.0 is now the config booth, the default/);
    buttonIn(panel, 'Dismiss').fire('click');
    assert.equal(app.run("localStorage.getItem('alhazen-mrc-migrated:mc_0123456789abcdef')"), '1');
  });

  it('lists a config’s saves and shows an earlier version', async () => {
    const { panel } = await general(rigs([config()]), (a) => {
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
    const { panel } = await general(rigs([config(), config({ id: 'c-bench', name: 'bench', default: false, legacy: false })]), (a) => {
      a.run("window.WorkspaceManage.focusRig('c-bench')");
    });
    assert.match(rowOf(panel, 'c-bench').textContent, /Edit bench/);
    assert.doesNotMatch(rowOf(panel, 'c-booth').textContent, /Edit booth/);
  });
});
