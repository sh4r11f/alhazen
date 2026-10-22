// A machine's dashboard on My machines (platform-web/src/60_helpers.js): which machine a run, dashboard
// or shutdown job is on, what the newest such job says (starting, open, closed, stopping), and the
// Open / Closed chip of a job that opened a dashboard.
//   ALHAZEN_PLATFORM_NODE_MODULES=/tmp/apw-node/node_modules node --test platform-web/test/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const H = new Function('window', 'URL', 'URLSearchParams', fs.readFileSync(path.join(HERE, '..', 'src', '60_helpers.js'), 'utf8')
  + '\nreturn { jobMachineLabel, dashJobState, machineDashboard, dashJobChip, terminalWait };')({}, URL, URLSearchParams);

const devices = [{ id: 'd_rb', label: 'RazerBlade', host: 'razer' }, { id: 'd_mac', host: 'Sharifs-MacBook-Pro.local' }];
const sessions = [{ id: 's_rb', device: 'd_rb' }, { id: 's_mac', device: 'd_mac' }];

test('a job is on the machine its label names, else the one its terminal window belongs to', () => {
  assert.equal(H.jobMachineLabel({ machineLabel: 'RazerBlade', machine: 's_mac' }, sessions, devices), 'RazerBlade');
  assert.equal(H.jobMachineLabel({ machine: 's_rb' }, sessions, devices), 'RazerBlade');
  assert.equal(H.jobMachineLabel({ machine: 's_mac' }, sessions, devices), 'Sharifs-MacBook-Pro.local');
  assert.equal(H.jobMachineLabel({ machine: 'Wallach' }, sessions, devices), 'Wallach');
  assert.equal(H.jobMachineLabel({ machine: 's_gone' }, sessions, devices), 's_gone');
  assert.equal(H.jobMachineLabel({}, sessions, devices), '');
});

test('what a job says about its dashboard', () => {
  assert.equal(H.dashJobState({ kind: 'dashboard', status: 'running' }), 'starting');
  assert.equal(H.dashJobState({ kind: 'run', status: 'done', url: 'http://127.0.0.1:1/' }), 'open');
  assert.equal(H.dashJobState({ kind: 'run', status: 'done', url: 'http://127.0.0.1:1/', closedAt: 5 }), 'closed');
  assert.equal(H.dashJobState({ kind: 'run', status: 'done' }), null); // finished without a dashboard
  assert.equal(H.dashJobState({ kind: 'run', status: 'failed', url: 'x' }), null);
  assert.equal(H.dashJobState({ kind: 'change', status: 'done', url: 'x' }), null);
  assert.equal(H.dashJobState({ kind: 'shutdown', status: 'done' }), null);
});

test('a machine row reads the newest job on it; a running shutdown is stopping', () => {
  const jobs = [
    { kind: 'dashboard', machineLabel: 'RazerBlade', status: 'done', url: 'u1', startedAt: 10, updatedAt: 12 },
    { kind: 'run', machine: 's_mac', status: 'done', url: 'u2', startedAt: 20, updatedAt: 25 },
    { kind: 'run', machine: 'razerblade ', status: 'failed', startedAt: 30 },
  ];
  assert.deepEqual(H.machineDashboard(jobs, 'RazerBlade', sessions, devices), { state: 'open', job: jobs[0], at: 12 });
  assert.equal(H.machineDashboard(jobs, 'Sharifs-MacBook-Pro.local', sessions, devices).state, 'open');
  assert.equal(H.machineDashboard(jobs, 'Wallach', sessions, devices), null);
  assert.equal(H.machineDashboard(jobs, '', sessions, devices), null);
  const closed = [...jobs, { kind: 'dashboard', machineLabel: 'RazerBlade', status: 'done', url: 'u1', startedAt: 10, closedAt: 40 }];
  closed.splice(0, 1);
  assert.deepEqual(H.machineDashboard(closed, 'RazerBlade', sessions, devices).at, 40);
  assert.equal(H.machineDashboard(closed, 'RazerBlade', sessions, devices).state, 'closed');
  const stopping = [...jobs, { kind: 'shutdown', machine: 's_rb', machineLabel: 'RazerBlade', status: 'running', startedAt: 50 }];
  assert.equal(H.machineDashboard(stopping, 'RazerBlade', sessions, devices).state, 'stopping');
  // A finished shutdown says nothing itself: the jobs it closed do.
  const done = [...jobs, { kind: 'shutdown', machineLabel: 'RazerBlade', status: 'waiting', startedAt: 50 }];
  assert.equal(H.machineDashboard(done, 'RazerBlade', sessions, devices).state, 'open');
  const starting = [...jobs, { kind: 'run', machineLabel: 'RazerBlade', status: 'running', startedAt: 60 }];
  assert.deepEqual(H.machineDashboard(starting, 'RazerBlade', sessions, devices).state, 'starting');
});

test('a job that opened a dashboard is Open until it is reported closed', () => {
  assert.deepEqual(H.dashJobChip({ kind: 'run', status: 'done', url: 'u' }), { tone: 'st-ok', words: 'Open' });
  assert.deepEqual(H.dashJobChip({ kind: 'dashboard', status: 'done', url: 'u', closedAt: 3 }), { tone: 'st-idle', words: 'Closed' });
  assert.equal(H.dashJobChip({ kind: 'run', status: 'done' }), null);
  assert.equal(H.dashJobChip({ kind: 'run', status: 'failed', url: 'u' }), null);
  assert.equal(H.dashJobChip({ kind: 'shutdown', status: 'done', url: 'u' }), null);
});

test('a job held by Mana Terminal for a yes says so until it ends', () => {
  const w = { for: 'terminal', machine: 'RazerBlade', what: 'Shut down the alhazen dashboard', since: 1 };
  assert.deepEqual(H.terminalWait({ kind: 'shutdown', status: 'running', waiting: w }), { machine: 'RazerBlade', what: 'Shut down the alhazen dashboard' });
  assert.deepEqual(H.terminalWait({ kind: 'run', status: 'running', waiting: { for: 'terminal' } }), { machine: 'your machine', what: '' });
  assert.equal(H.terminalWait({ kind: 'shutdown', status: 'done', waiting: w }), null);
  assert.equal(H.terminalWait({ kind: 'run', status: 'running', waiting: null }), null);
  assert.equal(H.terminalWait({ kind: 'run', status: 'running', waiting: { for: 'other' } }), null);
});
