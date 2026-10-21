// The rig config helpers of the web app (platform-web/src/65_rigs.jsx): sections, the form's controls
// from a machine's rig schema (rig_schema_sample.json: RigConfig.model_json_schema() of alhazen
// 3.6.0), YAML edits that keep comments, changes, the Run dialog's choices and the state words.
// Needs the build dependencies (esbuild compiles the JSX; yaml parses):
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
const Y = require('yaml');
const code = esbuild.transformSync(fs.readFileSync(path.join(HERE, '..', 'src', '65_rigs.jsx'), 'utf8'), { loader: 'jsx' }).code;
const H = new Function('React', 'window', code + '\nreturn { rigSectionOf, rigSectionDepth, rigLeaves, rigIsEventPath, rigEventPathsIn, rigSchemaAt, '
  + 'rigFieldKind, rigParse, rigSetValue, rigDeletePaths, rigChanges, rigConfigState, rigLiveConfigs, rigTemplatesOf, rigPickerGroups, rigNewId };')(
  {}, { crypto: globalThis.crypto });
const schema = JSON.parse(fs.readFileSync(path.join(HERE, 'rig_schema_sample.json'), 'utf8'));

const RIG = `# The booth rig.
monitor:
  width_px: 1920   # the panel's pixels
  height_px: 1080
  width_cm: 53.1
  distance_cm: 57
  refresh_rate_hz: 120
devices:
  eyetracker:
    backend: eyelink
    eye: left
    led_intensity: null
  sync:
    device: Dev1
    event_lines:
      STIM_ON: Dev1/port0/line0
data_root: data
real_data: yes
`;

test('settings fall under the dashboard\'s sections, event lines apart', () => {
  const { values } = H.rigParse(Y, RIG);
  const leaves = H.rigLeaves(values).map((l) => l.path.join('.'));
  assert.ok(leaves.includes('monitor.width_px') && leaves.includes('devices.sync.event_lines.STIM_ON'));
  assert.equal(H.rigSectionOf(['monitor', 'width_px']), 'monitor');
  assert.equal(H.rigSectionOf(['devices', 'eyetracker', 'eye']), 'eyetracker');
  assert.equal(H.rigSectionOf(['devices', 'recording', 'x']), 'devices');
  assert.equal(H.rigSectionOf(['real_data']), 'data');
  assert.equal(H.rigSectionOf(['dashboard', 'port']), 'live_monitor');
  assert.equal(H.rigSectionOf(['whatever']), 'other');
  assert.equal(H.rigSectionDepth(['devices', 'eyetracker', 'eye']), 2);
  assert.equal(H.rigSectionDepth(['monitor', 'width_px']), 1);
  assert.equal(H.rigIsEventPath(['devices', 'sync', 'event_lines', 'STIM_ON']), true);
  assert.equal(H.rigIsEventPath(['display', 'photodiode', 'events']), true);
  assert.equal(H.rigIsEventPath(['devices', 'sync', 'device']), false);
  assert.deepEqual(H.rigEventPathsIn(values), ['devices.sync.event_lines.STIM_ON']);
  assert.deepEqual(H.rigEventPathsIn({ devices: { sync: { event_lines: {} } }, display: { photodiode: { events: [] } } }), []);
});

test('the schema decides each control', () => {
  const kind = (p, v) => H.rigFieldKind(schema, p.split('.'), v);
  assert.equal(kind('monitor.width_px', 1920).kind, 'integer');
  assert.equal(kind('monitor.width_cm', 53.1).kind, 'number');
  assert.equal(kind('monitor.fullscreen', true).kind, 'bool');
  assert.equal(kind('monitor.name', 'x').kind, 'text');
  assert.deepEqual(kind('devices.eyetracker.backend', 'eyelink'), { kind: 'select', options: ['eyelink', 'viewpixx', 'mouse_sim', 'scripted'], nullable: false });
  assert.deepEqual(kind('devices.eyetracker.led_intensity', null), { kind: 'integer', nullable: true });
  assert.equal(kind('real_data', true).kind, 'bool');
  assert.equal(kind('data_root', 'data').kind, 'text');
  // Not in the schema: by the value it has.
  assert.equal(kind('monitor.not_a_setting', 3).kind, 'integer');
  assert.equal(kind('devices.sync.event_lines', {}).kind, 'json');
  assert.equal(H.rigFieldKind(null, ['a'], [1, 2]).kind, 'json');
  assert.equal(H.rigFieldKind(null, ['a'], 'x').kind, 'text');
});

test('editing a setting keeps comments and the rest of the file', () => {
  let text = H.rigSetValue(Y, RIG, ['monitor', 'width_px'], 2560);
  assert.match(text, /^# The booth rig\./);
  assert.match(text, /width_px: 2560 # the panel's pixels/);
  assert.match(text, /real_data: yes/); // untouched YAML 1.1 spelling stays
  assert.doesNotMatch(text, /%YAML/);
  text = H.rigSetValue(Y, text, ['devices', 'eyetracker', 'led_intensity'], 40);
  text = H.rigSetValue(Y, text, ['monitor', 'size_hint'], [1, 2]);
  const { values } = H.rigParse(Y, text);
  assert.equal(values.devices.eyetracker.led_intensity, 40);
  assert.deepEqual(values.monitor.size_hint, [1, 2]);
  assert.match(text, /size_hint: \[1, 2\]/);
  assert.deepEqual(H.rigChanges(H.rigParse(Y, RIG).values, values).map((c) => c.path).sort(),
    ['devices.eyetracker.led_intensity', 'monitor.size_hint', 'monitor.width_px']);
});

test('YAML 1.1 as the rig reads it; errors and non-mappings are reported', () => {
  assert.equal(H.rigParse(Y, 'real_data: no\n').values.real_data, false);
  assert.match(H.rigParse(Y, 'a: [1\n').error, /Flow sequence/);
  assert.match(H.rigParse(Y, '- 1\n- 2\n').error, /mapping/);
  assert.deepEqual(H.rigParse(Y, '').values, {});
});

test('a template\'s event lines are removed when a config starts from it', () => {
  const text = H.rigDeletePaths(Y, RIG, ['devices.sync.event_lines.STIM_ON']);
  const { values } = H.rigParse(Y, text);
  assert.deepEqual(values.devices.sync.event_lines, {});
  assert.equal(values.devices.sync.device, 'Dev1');
});

test('state words for every state of a config', () => {
  assert.equal(H.rigConfigState({ state: 'synced' }, 'Booth').words, 'Synced');
  assert.deepEqual(H.rigConfigState({ state: 'pending' }, 'Booth'), { tone: 'warn', words: 'Waiting for the machine', detail: 'Waiting for Booth to take it' });
  assert.equal(H.rigConfigState({ state: 'rejected', rejection: { reason: 'monitor.width_px: not an integer' } }).detail, 'monitor.width_px: not an integer');
  assert.equal(H.rigConfigState({ state: 'pending', conflict: { text: 'x' } }).words, 'Conflict');
  assert.equal(H.rigConfigState({ state: 'synced', hasConflict: true }).words, 'Conflict');
  assert.equal(H.rigConfigState({ state: 'pending', deleted: true }, 'Booth').detail, 'Waiting for Booth to delete it');
});

const machines = [
  { id: 'mc_aaaaaaaaaaaaaaaa', label: 'Booth', default: 'c1', configs: {
    c1: { id: 'c1', name: 'eyelink', state: 'synced', runningSha256: 'x' },
    c2: { id: 'c2', name: 'keyboard', state: 'synced', problem: 'monitor.width_px missing', runningSha256: 'y' },
    c3: { id: 'c3', name: 'new-one', state: 'pending' },
    c4: { id: 'c4', name: 'gone', state: 'pending', deleted: true } } },
  { id: 'mc_bbbbbbbbbbbbbbbb', label: 'Laptop', default: 'd1', configs: { d1: { id: 'd1', name: 'mouse', state: 'synced', runningSha256: 'z' } } },
  { id: 'mc_cccccccccccccccc', label: 'Empty', default: null, configs: {} },
];

test('the Run dialog offers the configs of the window\'s machine, else all, grouped', () => {
  const one = H.rigPickerGroups(machines, ' booth ');
  assert.equal(one.matched, true);
  assert.equal(one.groups.length, 1);
  assert.deepEqual(one.groups[0].configs.map((c) => [c.name, c.isDefault, c.disabled]), [['eyelink', true, false], ['keyboard', false, true], ['new-one', false, true]]);
  assert.equal(one.groups[0].configs[0].value, 'mc_aaaaaaaaaaaaaaaa|eyelink');
  const all = H.rigPickerGroups(machines, 'Unknown PC');
  assert.equal(all.matched, false);
  assert.deepEqual(all.groups.map((g) => g.label), ['Booth', 'Laptop']);
  assert.deepEqual(H.rigPickerGroups([], 'x').groups, []);
});

test('templates and ids', () => {
  assert.deepEqual(H.rigTemplatesOf({ templates: '[{"id":"lab","text":"a: 1"},{"id":"bad"}]' }).map((t) => t.id), ['lab']);
  assert.deepEqual(H.rigTemplatesOf({ templates: 'not json' }), []);
  assert.deepEqual(H.rigTemplatesOf(null), []);
  const id = H.rigNewId();
  assert.match(id, /^c[0-9a-f]{10}$/);
  assert.notEqual(H.rigNewId(), id);
  assert.deepEqual(H.rigLiveConfigs(machines[0]).map((c) => c.name), ['eyelink', 'keyboard', 'new-one']);
});
