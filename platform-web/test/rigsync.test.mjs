// The Mana host's rig config rules (platform-web/src/rigsync.js) against the Python module they port
// (alhazen.experiment_platform.rigsync): rigsync_cases.json holds owner operations and the record or
// refusal Python gave for each; regenerate it with gen_rigsync_cases.py after changing the rules.
//   node --test platform-web/test/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(path.join(HERE, '..', 'src', 'rigsync.js'), 'utf8');
const R = new Function(source + '\nreturn { RigSyncError, rigSha256, rigNormalize, rigOwnerPut, rigOwnerDelete, rigOwnerDefault, '
  + 'rigOwnerResolve, rigSummary, rigFromFirebase, rigFirebasePatch, rigStripNulls };')();
const data = JSON.parse(fs.readFileSync(path.join(HERE, 'rigsync_cases.json'), 'utf8'));
const expand = (v) => (v && typeof v === 'object' ? v.repeat.repeat(v.times) : v);

function run(op, rec, mid, cid, args) {
  if (op === 'put') return R.rigOwnerPut(rec, mid, cid, { ...args, text: expand(args.text) });
  if (op === 'delete') return R.rigOwnerDelete(rec, mid, cid, args);
  if (op === 'default') return R.rigOwnerDefault(rec, mid, cid, args);
  return R.rigOwnerResolve(rec, mid, cid, args);
}

test('sha256 of UTF-8 text matches Python', () => {
  for (const [text, digest] of Object.entries(data.sha256)) assert.equal(R.rigSha256(text), digest);
});

for (const c of data.cases) {
  test('same results as rigsync.py: ' + c.title, () => {
    let rec = c.start;
    c.steps.forEach((step, i) => {
      const where = c.title + ' step ' + (i + 1) + ' (' + step.op + ' ' + step.cid + ')';
      if (step.error) {
        assert.throws(() => run(step.op, rec, c.mid, step.cid, step.args), (e) => {
          assert.ok(e instanceof R.RigSyncError, where + ': ' + e);
          assert.equal(e.kind, step.error.kind, where);
          assert.equal(e.message, step.error.message, where);
          return true;
        }, where);
        return;
      }
      const after = run(step.op, rec, c.mid, step.cid, step.args);
      assert.deepEqual(after, step.record, where);
      assert.deepEqual(R.rigSummary(after), step.summary, where);
      rec = after;
    });
  });
}

test('the rules never change the record they are given', () => {
  const c = data.cases[0];
  const before = JSON.stringify(c.start);
  R.rigOwnerPut(c.start, c.mid, 'c1a2b3c4d5e', { ...c.steps[0].args });
  assert.equal(JSON.stringify(c.start), before);
});

test('a record read back from Firebase (no nulls, configs under rigs) normalizes to the same record', () => {
  const rec = data.cases[0].steps[2].record;
  const { configs, ...rest } = rec;
  const node = R.rigStripNulls({ ...rest, rigs: configs, metadata: { type: 'entry', createdAt: 1, updatedAt: 2 } });
  assert.equal(JSON.stringify(node).includes('null'), false);
  const back = R.rigFromFirebase(node, rec.id);
  // Every field of the record and of each config is back; nulls nested deeper (running.problem)
  // stay absent, as with the Python normalize, so readers use (x || {}).field.
  assert.deepEqual(Object.keys(back).sort(), Object.keys(rec).sort());
  for (const cid of Object.keys(rec.configs)) assert.deepEqual(Object.keys(back.configs[cid]).sort(), Object.keys(rec.configs[cid]).sort());
  assert.deepEqual(R.rigStripNulls(back), R.rigStripNulls(rec));
  assert.equal(R.rigFromFirebase(null, rec.id), null);
});

test('the Firebase patch writes only what changed', () => {
  const c = data.cases[2]; // default and delete
  const start = c.start, afterDefault = c.steps[0].record, afterDelete = c.steps[2].record;
  assert.deepEqual(R.rigFirebasePatch(start, afterDefault, 9), { default: 'c2b3c4d5e6f', defaultRev: 2, defaultBy: 'platform', 'metadata/updatedAt': 9 });
  const patch = R.rigFirebasePatch(afterDefault, afterDelete, 10);
  assert.deepEqual(Object.keys(patch).sort(), ['metadata/updatedAt', 'rigs/c1a2b3c4d5e']);
  assert.equal(patch['rigs/c1a2b3c4d5e'].deleted, true);
  assert.equal('rejection' in patch['rigs/c1a2b3c4d5e'], false);
  const dropped = data.cases[3]; // never ran: dropped at once
  assert.deepEqual(R.rigFirebasePatch(dropped.start, dropped.steps[0].record, 11), { 'rigs/c2b3c4d5e6f': null, 'metadata/updatedAt': 11 });
  assert.deepEqual(R.rigFirebasePatch(start, start, 12), {});
});
