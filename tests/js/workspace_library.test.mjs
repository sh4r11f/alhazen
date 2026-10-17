// The library's sidebar and Experiments-page states (workspace_library.js):
// what each copy says and which single action it offers.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(path.join(HERE, '../../src/alhazen/cli/assets/workspace_library.js'), 'utf8');
const window = {};
new Function('window', 'document', 'CSS', source)(window, {}, {escape: (s) => s});
const lib = window.WorkspaceLibrary;

const item = (state, extra = {}) => ({
  id: 'dots-1', title: 'Dots', downloadable: true, project: null, job: null,
  disk: {state, path: '/x/dots-1', marker: state === 'absent' ? null : {bundle: 'b1'}, data: {sessions: 0, folders: []}, environment: false},
  ...extra,
});

test('a copy that is not on this machine offers Download', () => {
  assert.equal(lib._stateOf(item('absent')).words, 'Not downloaded');
  assert.equal(lib._actionOf(item('absent')), 'Download');
});

test('a registered current copy offers nothing more; an unregistered one offers Set up', () => {
  assert.equal(lib._actionOf(item('current', {project: 'p1'})), null);
  assert.equal(lib._stateOf(item('current', {project: 'p1'})).words, 'On this machine');
  assert.equal(lib._actionOf(item('current')), 'Set up');
});

test('another revision on disk offers Update; a folder without a marker is checked first', () => {
  assert.equal(lib._actionOf(item('outdated')), 'Update');
  assert.equal(lib._actionOf(item('unknown')), 'Check and set up');
});

test('a running download offers nothing; a conflict asks; a copy without a revision cannot download', () => {
  const busy = item('absent', {job: {status: 'running', stage: 'building'}});
  assert.equal(lib._actionOf(busy), null);
  assert.equal(lib._stateOf(busy).words, 'Building the environment…');
  assert.equal(lib._stateOf(item('outdated', {job: {status: 'conflict'}})).words, 'Needs your answer');
  assert.equal(lib._actionOf(item('absent', {downloadable: false})), null);
});
