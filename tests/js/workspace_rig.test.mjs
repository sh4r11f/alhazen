/* This machine's rig on the Run page: the pure part of workspace_rig.js —
 * what a session changed, keeping those changes across a task change, the
 * Event lines rows and their edits, which config the Run page opens on and
 * the sync states in words — and its Add a config form on the fake page.
 * The server records the same changes (machine_rig.rig_changes) and checks
 * the event names again; these tests hold the page to the same rules. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

import { FakeDocument } from './fake_dom.mjs';

const ASSETS = new URL('../../src/alhazen/cli/assets/', import.meta.url);

function load() {
  const context = vm.createContext({});
  vm.runInContext(readFileSync(new URL('workspace_rig.js', ASSETS), 'utf8') + '\nthis.RigEditor = RigEditor;', context);
  return context.RigEditor;
}
const plain = (v) => JSON.parse(JSON.stringify(v));
const R = load();

const MACHINE = {
  monitor: { width_px: 1920, distance_cm: 57 },
  display: { photodiode: { corner: 'br' } },
  devices: { sync: { backend: 'nidaq' } },
  real_data: true,
};
function rig() {
  return { machine: { name: 'lab', values: R.clone(MACHINE) }, prefill: R.clone(MACHINE), current: R.clone(MACHINE) };
}

test('changes are listed setting by setting, as the server records them', () => {
  const current = R.clone(MACHINE);
  current.monitor.distance_cm = 60;
  delete current.real_data;
  current.data_root = 'data';
  assert.deepEqual(plain(R.changes(MACHINE, current)), [
    { path: 'monitor.distance_cm', machine: 57, session: 60 },
    { path: 'real_data', machine: true, absent: 'session' },
    { path: 'data_root', session: 'data', absent: 'machine' },
  ]);
});

test('event lines are not setting changes, and one change is reset', () => {
  const r = rig();
  R.setLine(r.current, 'FIX_ON', 'Dev1/port0/line0');
  r.current.monitor.distance_cm = 61;
  assert.deepEqual(plain(R.settingChanges(r)).map((c) => c.path), ['monitor.distance_cm']);
  R.reset(r, ['monitor', 'distance_cm']);
  assert.equal(r.current.monitor.distance_cm, 57);
  assert.equal(r.current.devices.sync.event_lines.FIX_ON, 'Dev1/port0/line0');
});

test('an empty assignment is taken out, not kept as an empty mapping', () => {
  const r = rig();
  R.setLine(r.current, 'FIX_ON', 'Dev1/port0/line0');
  R.setLine(r.current, 'FIX_ON', '  ');
  assert.equal('event_lines' in r.current.devices.sync, false);
  R.setDiode(r.current, 'FIX_ON', true);
  assert.deepEqual(plain(r.current.display.photodiode.events), ['FIX_ON']);
  R.setDiode(r.current, 'FIX_ON', false);
  assert.equal('events' in r.current.display.photodiode, false);
  /* No sync device on the machine: no line to assign, nothing invented. */
  const bare = { monitor: {} };
  assert.equal(R.setLine(bare, 'FIX_ON', 'x'), false);
  assert.deepEqual(plain(bare), { monitor: {} });
});

test('rows list the task’s own events first, then those every task has, an undeclared one on top', () => {
  const values = R.clone(MACHINE);
  values.devices.sync.event_lines = { STIM_ON: 'Dev1/port0/line1', TRIAL_START: 'Dev1/port0/line0' };
  const rows = plain(R.eventRows(values, ['FIX_ON', 'PAUSED', 'TRIAL_START'], ['FIX_ON', 'TRIAL_START']));
  assert.deepEqual(rows.map((r) => [r.name, r.kind, r.line]), [
    ['STIM_ON', 'undeclared', 'Dev1/port0/line1'],
    ['FIX_ON', 'task', ''],
    ['TRIAL_START', 'task', 'Dev1/port0/line0'],
    ['PAUSED', 'common', ''],
  ]);
  /* An older alhazen says nothing of the events: the assigned ones are shown. */
  assert.deepEqual(plain(R.eventRows(values, null, null)).map((r) => r.name), ['STIM_ON', 'TRIAL_START']);
});

test('a setting change survives a fresh read for another task; the events start over', () => {
  const previous = rig();
  previous.current.monitor.distance_cm = 62;
  R.setLine(previous.current, 'FIX_ON', 'Dev1/port0/line0');
  const next = rig();
  R.setLine(next.prefill, 'GO', 'Dev1/port0/line2');
  next.current = R.clone(next.prefill);
  R.carryChanges(previous, next);
  assert.equal(next.current.monitor.distance_cm, 62);
  assert.deepEqual(plain(next.current.devices.sync.event_lines), { GO: 'Dev1/port0/line2' });
});

test('Reset all puts the settings back and the events where the session started', () => {
  const r = rig();
  R.setLine(r.prefill, 'FIX_ON', 'Dev1/port0/line0');
  r.current = R.clone(r.prefill);
  r.current.monitor.distance_cm = 70;
  R.setLine(r.current, 'FIX_ON', 'Dev1/port0/line5');
  R.resetAll(r);
  assert.equal(r.current.monitor.distance_cm, 57);
  assert.equal(r.current.devices.sync.event_lines.FIX_ON, 'Dev1/port0/line0');
});

test('rows go under the Monitor, Display, Eye tracker, … headings', () => {
  assert.equal(R.sectionOf(['monitor', 'width_px']), 'monitor');
  assert.equal(R.sectionOf(['devices', 'eyetracker', 'host_ip']), 'eyetracker');
  assert.equal(R.sectionOf(['devices', 'spikes', 'backend']), 'devices');
  assert.equal(R.sectionOf(['data_root']), 'data');
  assert.equal(R.sectionOf(['frame_budget']), 'other');
});

function fakeNode() {
  const document = new FakeDocument();
  return (tag, className, text) => {
    const el = document.createElement(tag);
    if (className) el.setAttribute('class', className);
    if (text !== undefined) el.textContent = text;
    return el;
  };
}

test('Add a config offers this machine’s configs, the experiments’ rigs and the shared ones', async () => {
  const node = fakeNode();
  const host = node('div');
  const templates = {
    suggested: 'p:configs/rig-lab.yaml',
    templates: [
      { id: 'alhazen/laptop', label: 'alhazen/laptop', kind: 'shared', name: 'laptop', summary: 's', real_data: false, events: [], error: null },
      { id: 'p:configs/rig-lab.yaml', label: 'demo/lab', kind: 'experiment', name: 'lab', summary: '1920 × 1080 px · 120 Hz · eyelink', real_data: true, extends: 'lab', events: ['FIX_ON'], error: null },
      { id: 'p:configs/rig-bad.yaml', label: 'demo/bad', kind: 'experiment', name: 'bad', error: 'invalid' },
    ],
  };
  const sent = [];
  const api = async (path, body) => {
    sent.push([path, body]);
    return path.endsWith('/templates') ? templates : { created: 'c-new', configs: [] };
  };
  let done = null;
  await R.createForm(host, { node, api, configs: [{ id: 'c-booth', name: 'booth', problem: null }],
    onDone: async (rigs) => { done = rigs; } });
  const select = host.querySelector('select');
  assert.deepEqual(select.children.map((g) => g.label),
    ['This machine’s configs', 'Registered experiments’ rigs', 'alhazen’s shared rigs']);
  assert.equal(select.value, 'template:p:configs/rig-lab.yaml');
  assert.match(host.textContent, /collects real data · written out over alhazen\/lab/);
  assert.match(host.textContent, /its event lines \(FIX_ON\) are left out and chosen per session/);
  assert.doesNotMatch(host.textContent, /demo\/bad/);
  const name = host.querySelector('input');
  assert.equal(name.value, 'lab');
  /* A copy of a config on this machine is sent as copy_of, its name suggested apart. */
  select.value = 'copy:c-booth';
  select.fire('change');
  assert.equal(name.value, 'booth-copy');
  host.querySelector('form').fire('submit', { preventDefault() {} });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(plain(sent.at(-1)), ['/api/machine-rigs/create', { name: 'booth-copy', copy_of: 'c-booth' }]);
  assert.equal(done.created, 'c-new');
});

test('Add a config refuses a name taken or malformed before asking the server', async () => {
  const node = fakeNode();
  const host = node('div');
  const sent = [];
  const api = async (path, body) => {
    sent.push(path);
    return { templates: [{ id: 'alhazen/lab', label: 'alhazen/lab', kind: 'shared', name: 'lab', summary: '', real_data: true, events: [] }] };
  };
  await R.createForm(host, { node, api, configs: [{ id: 'c1', name: 'lab' }], onDone: async () => {} });
  const select = host.querySelector('select');
  select.value = 'template:alhazen/lab';
  select.fire('change');
  const name = host.querySelector('input');
  assert.equal(name.value, 'lab-2');
  name.value = 'lab';
  name.fire('input');
  host.querySelector('form').fire('submit', { preventDefault() {} });
  assert.match(host.querySelector('.rig-problem').textContent, /already has a config named lab/);
  name.value = '-bad name';
  host.querySelector('form').fire('submit', { preventDefault() {} });
  assert.match(host.querySelector('.rig-problem').textContent, /starts with a letter or digit/);
  assert.deepEqual(sent, ['/api/machine-rigs/templates']);
});

test('the Run page opens on the named config, else the default, never one that does not load', () => {
  const choices = [
    { id: 'c1', name: 'booth', default: true, problem: null },
    { id: 'c2', name: 'bench', default: false, problem: null },
    { id: 'c3', name: 'broken', default: false, problem: 'bad' },
  ];
  assert.equal(R.pickConfig(choices, null).id, 'c1');
  assert.equal(R.pickConfig(choices, 'bench').id, 'c2');
  assert.equal(R.pickConfig(choices, 'c2').id, 'c2');
  assert.equal(R.pickConfig(choices, 'broken').id, 'c1');
  assert.equal(R.pickConfig(choices, 'gone').id, 'c1');
  assert.equal(R.pickConfig([{ ...choices[0], problem: 'x' }, choices[1]], null).id, 'c2');
  assert.equal(R.pickConfig([choices[2]], null), null);
  assert.equal(R.pickConfig([], 'booth'), null);
});

test('a config’s sync state and the connection are said in words', () => {
  assert.equal(R.syncText({ sync: { state: 'synced' } }), 'Synced');
  assert.equal(R.syncText({ sync: { state: 'local' } }), 'Changed here, not sent yet');
  assert.equal(R.syncText({ sync: { state: 'pending' } }), 'Waiting for this machine (a platform edit)');
  assert.equal(R.syncText({ sync: { state: 'rejected', rejected: { reason: 'monitor.width_px: must be positive' } } }),
    'Platform edit refused: monitor.width_px: must be positive');
  assert.equal(R.syncText({ sync: { state: 'conflict' } }), 'Conflict');
  assert.equal(R.syncText({ sync: { state: 'not-synced' } }), 'Only on this machine');
  assert.match(R.connectionText({ state: 'not-connected' }), /^Not connected to a platform/);
  assert.equal(R.connectionText({ state: 'synced', at: 'T', platform: 'Mana' }, (s) => `<${s}>`),
    'Synced with the platform (Mana) at <T>.');
  assert.match(R.connectionText({ state: 'offline', error: 'timed out' }), /could not be reached: timed out\. Changes are kept here/);
  assert.match(R.connectionText({ state: 'unsupported' }), /does not keep rig configs yet/);
});

test('the config editor’s fields can leave real_data editable and name their baseline', () => {
  const node = fakeNode();
  const host = node('div');
  const r = { machine: { values: { monitor: { width_px: 1920 }, real_data: true } }, current: { monitor: { width_px: 1280 }, real_data: true } };
  R.renderFields(host, r, { node, onChange: () => {}, baseline: 'the saved value', fixed: [] });
  const real = host.querySelectorAll('.parameter-row').find((row) => row.dataset.key === 'real_data');
  assert.equal(real.querySelector('input').type, 'checkbox');
  const width = host.querySelectorAll('.parameter-row').find((row) => row.dataset.key === 'monitor.width_px');
  assert.match(width.querySelector('.rig-reset').title, /^Back to the saved value: 1920/);
});

test('a config’s problem is said in a line, without paths, codes or links', () => {
  const problem = "invalid config in /state/machine-rig/configs/bench/rig-bench.yaml:\n1 validation error for RigConfig\nmonitor\n  Value error, width_px must be > 0 [type=value_error, input_value={'width_px': -5}, input_type=dict]\n    For further information visit https://errors.pydantic.dev/2.13/v/value_error";
  assert.equal(R.problemText(problem), 'monitor: width_px must be > 0');
  assert.equal(R.problemText('names events (FIX_ON); event lines are chosen per session'),
    'names events (FIX_ON); event lines are chosen per session');
  assert.equal(R.problemText(null), '');
});
