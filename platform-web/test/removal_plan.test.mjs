// Tests of the library removal plan (platform-web/src/60_helpers.js): what removing a copy from
// the library takes with it. Both hosts delete exactly what the plan names.
//   node --test platform-web/test/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(path.join(HERE, '..', 'src', '60_helpers.js'), 'utf8');
const { removalPlan } = new Function('window', 'ehFormat', source + '\nreturn { removalPlan };')({}, () => '');

const copy = (id, bundles, extra = {}) => ({
  id, title: 'Dots ' + id, head: { id: bundles[bundles.length - 1] },
  revisions: Object.fromEntries(bundles.map((b, i) => [String(i + 1), { bundle: b }])),
  origin: { publisherUid: 'lab', exp: 'dots', bundle: bundles[0] }, ...extra,
});

test('a copy alone in the library takes its revisions and methods snapshot with it', () => {
  const a = copy('a', ['dots-1111111111', 'a-2222222222'], { jobs: { 1: { status: 'done' } } });
  const plan = removalPlan(a, [a]);
  assert.deepEqual(plan.bundles.sort(), ['a-2222222222', 'dots-1111111111']);
  assert.equal(plan.docsKey, 'dots-1111111111');
  assert.equal(plan.working, false);
  assert.match(plan.removed.join('\n'), /2 revisions/);
  assert.match(plan.removed.join('\n'), /request and run record/);
  assert.match(plan.removed.join('\n'), /saved methods/);
  assert.match(plan.kept.join('\n'), /session data/);
});

test('what another copy names stays', () => {
  const a = copy('a', ['dots-1111111111', 'a-2222222222']);
  const b = copy('b', ['dots-1111111111']);
  const plan = removalPlan(a, [a, b]);
  assert.deepEqual(plan.bundles, ['a-2222222222']);
  assert.equal(plan.docsKey, null);
  assert.match(plan.kept.join('\n'), /another copy in your library uses them/);
});

test('a documentation-only refresh gives the copy its own snapshot key', () => {
  const a = copy('a', ['dots-1111111111']); a.origin.docsKey = 'dots-1111111111.d2';
  const b = copy('b', ['dots-1111111111']);
  assert.equal(removalPlan(a, [a, b]).docsKey, 'dots-1111111111.d2');
  assert.deepEqual(removalPlan(a, [a, b]).bundles, []);
});

test('a published copy keeps its listing, a copy at work says so, a scratch copy has no snapshot', () => {
  const a = copy('a', ['a-2222222222'], { published: { version: '1.0' }, jobs: { 9: { status: 'running' } } });
  const plan = removalPlan(a, [a]);
  assert.match(plan.kept.join('\n'), /marketplace listing: it stays/);
  assert.equal(plan.working, true);
  const s = { id: 's', title: 'New', origin: { scratch: true }, head: null };
  assert.equal(removalPlan(s, [s]).docsKey, null);
  assert.deepEqual(removalPlan(s, [s]).bundles, []);
});
