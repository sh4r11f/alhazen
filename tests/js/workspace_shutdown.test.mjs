/* The topbar's Shut down (setUpShutdown() in workspace.js): a confirmation
 * in place, refused while a session runs, and the shut-down screen once the
 * server agreed to stop.
 *
 * Run: node --test "tests/js/*.test.mjs"
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { loadWorkspace, response, settle } from './load_workspace.mjs';

function page({ active = null, answer = () => response({ ok: true }) } = {}) {
  const handle = loadWorkspace();
  /* The state the last poll brought; only `active` matters here. */
  handle.run(`state = {projects: [], runs: [], active: ${JSON.stringify(active)}}`);
  const shutdowns = [];
  handle.server.reject = (url, init) => {
    if (url !== '/api/shutdown') return undefined;
    shutdowns.push(JSON.parse(init.body));
    return answer();
  };
  return { ...handle, shutdowns };
}

describe('Shut down', () => {
  it('asks first, in place, and only the confirmation stops the server', async () => {
    const p = page();
    assert.equal(p.byId('shutdown-confirm').hidden, true);
    p.byId('shutdown').fire('click');
    assert.equal(p.byId('shutdown-confirm').hidden, false);
    assert.equal(p.byId('shutdown').getAttribute('aria-expanded'), 'true');
    assert.equal(p.byId('shutdown-confirmed').disabled, false);
    assert.deepEqual(p.shutdowns, []);
    p.byId('shutdown-cancel').fire('click');
    assert.equal(p.byId('shutdown-confirm').hidden, true);
    assert.deepEqual(p.shutdowns, []);
  });

  it('shows the shut-down screen and stops polling once the server agreed', async () => {
    const p = page();
    p.byId('shutdown').fire('click');
    p.byId('shutdown-confirmed').fire('click');
    await settle();
    assert.equal(p.shutdowns.length, 1);
    assert.equal(p.byId('shutdown-screen').hidden, false);
    assert.equal(p.byId('shutdown-confirm').hidden, true);
    const before = p.fetches.length;
    await p.run('poll()');
    await settle();
    assert.equal(p.fetches.length, before, 'no poll after the server stopped');
  });

  it('cannot be confirmed while a session runs', async () => {
    const p = page({ active: 'run-1' });
    p.byId('shutdown').fire('click');
    assert.equal(p.byId('shutdown-confirmed').disabled, true);
    assert.match(p.byId('shutdown-detail').textContent, /session is running/);
  });

  it("shows the server's refusal and keeps the page", async () => {
    const p = page({ answer: () => response({ error: 'A session is running.' }, 409) });
    p.byId('shutdown').fire('click');
    p.byId('shutdown-confirmed').fire('click');
    await settle();
    assert.equal(p.byId('shutdown-screen').hidden, true);
    assert.equal(p.byId('shutdown-error').hidden, false);
    assert.equal(p.byId('shutdown-error').textContent, 'A session is running.');
  });
});
