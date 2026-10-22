// The Configure window of My machines / Machines (platform-web/src/65_rigs.jsx, 60_helpers.js):
// matching a paired machine to the rig machines its dashboard synced, the address of an open
// window, the line each machine row shows, and what the window says when nothing has synced.
//   ALHAZEN_PLATFORM_NODE_MODULES=/tmp/apw-node/node_modules node --test platform-web/test/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const modules = process.env.ALHAZEN_PLATFORM_NODE_MODULES || path.join(HERE, '..', 'node_modules');
const require = createRequire(path.join(modules, 'noop.js'));
const esbuild = require('esbuild');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const src = (name) => fs.readFileSync(path.join(HERE, '..', 'src', name), 'utf8');
const rigsCode = esbuild.transformSync(src('65_rigs.jsx'), { loader: 'jsx' }).code;
const R = new Function('React', 'window', rigsCode + '\nreturn { rigMachinesForLabel, rigMatchMachines, rigRowSummary, rigWindowRoute, '
  + 'rigWindowFromRoute, rigPickerGroups, RigMachineWindow };')(React, { crypto: globalThis.crypto });

// A window whose address and history the routing helpers read and write.
function fakeWindow(href) {
  const w = { location: new URL(href), history: { entries: [], state: null } };
  const put = (state, url) => { w.location = new URL(url); w.history.state = state; };
  w.history.pushState = (state, _t, url) => { w.history.entries.push(['push', url]); put(state, url); };
  w.history.replaceState = (state, _t, url) => { w.history.entries.push(['replace', url]); put(state, url); };
  return w;
}
function routing(win) {
  return new Function('window', 'URL', 'URLSearchParams', src('60_helpers.js') + '\nreturn { readRoute, writeRoute };')(win, URL, URLSearchParams);
}

const booth = { id: 'mc_booth2a7f3', label: 'Booth-2', alhazen: '3.7.1', seenAt: 1000, default: 'c1',
  configs: { c1: { id: 'c1', name: 'booth', state: 'synced' }, c2: { id: 'c2', name: 'booth-daq', state: 'pending', hasConflict: true } } };
const boothOwn = { id: 'mc_boothown01', label: ' booth-2 ', seenAt: 5000, default: 'c9', configs: { c9: { id: 'c9', name: 'mine', state: 'rejected', rejection: { reason: 'x' } } } };
const lab = { id: 'mc_lab3rig88', label: 'Lab-3-rig', seenAt: 3000, default: 'c3', configs: { c3: { id: 'c3', name: 'lab3', state: 'synced' } } };

test('a paired machine matches the rig machines with its label, trimmed and case-blind', () => {
  assert.deepEqual(R.rigMachinesForLabel([booth, boothOwn, lab], 'BOOTH-2').map((m) => m.id), ['mc_booth2a7f3', 'mc_boothown01']);
  assert.deepEqual(R.rigMachinesForLabel([booth, lab], ''), []);
  assert.deepEqual(R.rigMachinesForLabel([booth, lab], 'Wallach'), []);
  // The Run dialog's choices use the same matching.
  assert.equal(R.rigPickerGroups([booth, boothOwn, lab], 'booth-2').matched, true);
  assert.deepEqual(R.rigPickerGroups([booth, boothOwn, lab], 'booth-2').groups.map((g) => g.mid), ['mc_booth2a7f3', 'mc_boothown01']);
});

test('rows get their rig machines, newest sync first; the rest are listed apart, nothing is made up', () => {
  const rows = [{ key: 'd_booth2', label: 'Booth-2' }, { key: 'd_wallach', label: 'Wallach' }];
  const { rows: out, unmatched } = R.rigMatchMachines(rows, [booth, boothOwn, lab]);
  assert.deepEqual(out.map((r) => [r.key, r.rigs.map((m) => m.id)]), [['d_booth2', ['mc_boothown01', 'mc_booth2a7f3']], ['d_wallach', []]]);
  assert.deepEqual(unmatched.map((m) => m.id), ['mc_lab3rig88']);
  assert.deepEqual(R.rigMatchMachines([], [lab, booth]).unmatched.map((m) => m.id), ['mc_lab3rig88', 'mc_booth2a7f3']);
  assert.deepEqual(R.rigMatchMachines(rows, []).rows.map((r) => r.rigs.length), [0, 0]);
});

test('each row says which configs a machine has and what needs the person', () => {
  assert.deepEqual(R.rigRowSummary([booth]), { text: 'Rig configs: booth (default), booth-daq', attention: '1 conflict' });
  assert.deepEqual(R.rigRowSummary([booth, boothOwn]).attention, '1 conflict, 1 refused by the machine');
  assert.deepEqual(R.rigRowSummary([]), { text: '', attention: '' });
  assert.deepEqual(R.rigRowSummary([{ ...lab, configs: {} }]), { text: 'No rig configs', attention: '' });
  const many = { ...lab, configs: Object.fromEntries(['a', 'b', 'c', 'd', 'e'].map((n) => ['c' + n, { id: 'c' + n, name: n, state: 'synced' }])), default: 'ca' };
  assert.equal(R.rigRowSummary([many]).text, 'Rig configs: a (default), b, c and 2 more');
});

test('the open window is in the address; the full-screen editor addresses still open it', () => {
  assert.deepEqual(R.rigWindowRoute({ dev: 'd_booth2' }), { v: 'machines', dev: 'd_booth2', id: '', tab: '' });
  assert.deepEqual(R.rigWindowRoute({ dev: 'd_booth2', mid: 'mc_booth2a7f3', cid: 'c2' }), { v: 'machines', dev: 'd_booth2', id: 'mc_booth2a7f3', tab: 'c2' });
  assert.deepEqual(R.rigWindowRoute({ cid: 'c2' }), { v: 'machines', dev: '', id: '', tab: '' });
  assert.equal(R.rigWindowFromRoute({ v: 'machines', id: '', tab: '', dev: '' }), null);
  assert.equal(R.rigWindowFromRoute({ v: 'browse', id: 'x', tab: '', dev: '' }), null);
  // ?ehv=machines&ehid=<machine>&ehtab=<config|new>, as written before 3.8.
  assert.deepEqual(R.rigWindowFromRoute({ v: 'machines', id: 'mc_booth2a7f3', tab: 'new', dev: '' }), { dev: '', mid: 'mc_booth2a7f3', cid: 'new' });
  assert.deepEqual(R.rigWindowFromRoute({ v: 'machines', id: '', tab: 'c2', dev: 'd_booth2' }), { dev: 'd_booth2', mid: '', cid: '' });

  const win = fakeWindow('https://mana.space/home/u/Apps/alhazen-experiment-platform?chatId=abc&ehv=machines');
  const { readRoute, writeRoute } = routing(win);
  writeRoute(R.rigWindowRoute({ dev: 'd_booth2', mid: 'mc_booth2a7f3', cid: 'c2' }), false, { ehWin: 2 });
  assert.equal(win.location.search, '?chatId=abc&ehv=machines&ehid=mc_booth2a7f3&ehtab=c2&ehdev=d_booth2');
  assert.deepEqual(win.history.state, { ehWin: 2 });
  assert.deepEqual(R.rigWindowFromRoute(readRoute()), { dev: 'd_booth2', mid: 'mc_booth2a7f3', cid: 'c2' });
  writeRoute({ v: 'machines', id: '', tab: '', task: '', set: '', dev: '' }, true);
  assert.equal(win.location.search, '?chatId=abc&ehv=machines');
  assert.deepEqual(win.history.state, {});
  assert.equal(R.rigWindowFromRoute(readRoute()), null);
});

const noop = () => {};
const host = { key: 'k', loadYaml: async () => null, subscribeRigMachine: () => noop, forgetRigMachine: async () => { throw new Error('not expected'); } };
const render = (props) => renderToStaticMarkup(React.createElement(R.RigMachineWindow, { host, rowsLoading: false, onOpen: noop, onBack: noop, onClose: noop, onFlash: noop, flash: null, ...props }));

test('a paired machine whose dashboard never synced says so and offers what connects it', () => {
  const html = render({ win: { dev: 'd_wallach', mid: '', cid: '' }, rows: [{ key: 'd_wallach', label: 'Wallach', rigs: [] }],
    rigData: { machines: [booth] }, renderUnmatched: (row) => React.createElement('button', { className: 'open' }, 'Open dashboard on ' + row.label) });
  assert.match(html, /role="dialog"/);
  assert.match(html, /aria-modal="true"/);
  assert.match(html, /<h2 id="eh-cfg-title">Wallach<\/h2>/);
  assert.match(html, /No rig configs from Wallach yet/);
  assert.match(html, /Open dashboard on Wallach/);
  assert.doesNotMatch(html, /Add a config/);
});

test('a matched machine lists its configs with the default and their state', () => {
  const html = render({ win: { dev: 'd_booth2', mid: '', cid: '' }, rows: [{ key: 'd_booth2', label: 'Booth-2', rigs: [booth] }], rigData: { machines: [booth] } });
  assert.match(html, /Rig configs · alhazen 3\.7\.1/);
  assert.match(html, />booth<span class="eh-rig-default">Default<\/span>/);
  assert.match(html, /Conflict/);
  assert.match(html, /Add a config/);
  const two = render({ win: { dev: 'd_booth2', mid: '', cid: '' }, rows: [{ key: 'd_booth2', label: 'Booth-2', rigs: [boothOwn, booth] }], rigData: { machines: [booth, boothOwn] } });
  assert.match(two, /Booth-2 syncs from 2 dashboards/);
  assert.match(two, /Dashboard 1/);
  assert.match(two, /Dashboard 2/);
});

test('a window for a machine that is gone says so instead of an empty list', () => {
  assert.match(render({ win: { dev: 'd_gone', mid: '', cid: '' }, rows: [], rigData: { machines: [] } }), /This machine is not paired any more/);
  assert.match(render({ win: { dev: '', mid: 'mc_missing01', cid: '' }, rows: [], rigData: { machines: [booth] } }), /This machine is not on the platform/);
  assert.match(render({ win: { dev: '', mid: 'mc_lab3rig88', cid: '' }, rows: [], rigData: { machines: [lab] } }), /<h2 id="eh-cfg-title">Lab-3-rig<\/h2>/);
  assert.match(render({ win: { dev: 'd_booth2', mid: '', cid: '' }, rows: [], rowsLoading: true, rigData: { machines: [] } }), /Loading…/);
});
