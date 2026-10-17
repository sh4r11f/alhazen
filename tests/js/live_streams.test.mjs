/* The live monitor's stream switches (Live streams): the gaze traces and the
 * eye camera, which the operator can turn off on a slow machine. Off must
 * stop the page asking for the stream, not only hide it.
 *
 * Run: node --test "tests/js/*.test.mjs"
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { SAVED_STATE, loadLiveMonitor, settle } from './load_live_monitor.mjs';

function response({ status = 200, json, text = '', headers = {} }) {
  return {
    ok: status >= 200 && status < 300,
    status: status,
    json: async () => json,
    text: async () => text,
    headers: new Map(Object.entries(headers)),
    arrayBuffer: async () => new ArrayBuffer(0),
  };
}

/* A long poll that is never answered, but ends when the page aborts it, as a
 * browser's fetch does. `open` counts the ones still waiting. */
function abortablePoll(open, name) {
  return (init) => new Promise((resolve, reject) => {
    open[name] = (open[name] || 0) + 1;
    const signal = init && init.signal;
    if (signal) {
      signal.addEventListener('abort', () => {
        open[name] -= 1;
        reject(new Error('aborted'));
      });
    }
  });
}

const VIEW = {
  trial_index: 3,
  attempt: 1,
  t: 50,
  screen: { width_dva: 40, height_dva: 30 },
  source: 'task',
  shapes: [],
  undrawn: [],
  condition: {},
  gaze: true,
  camera: { live: false, has_frames: true, reason: 'Read while paused.' },
};

/** The live page against a fake server holding the switches in `held`. */
async function livePage({ held = { gaze_traces: true, camera: true }, refuse = null, states = null } = {}) {
  const open = {};
  const posts = [];
  const stateQueue = states ? [...states] : [{ ...SAVED_STATE, revision: 1, status: 'running' }];
  let trialSent = false;
  const page = loadLiveMonitor({
    staticState: null,
    search: '?token=abc',
    fetch: (url, init) => {
      if (url.startsWith('/api/state')) {
        return response({ json: stateQueue.length > 1 ? stateQueue.shift() : stateQueue[0] });
      }
      if (url.startsWith('/api/streams')) return response({ json: { ...held } });
      if (url === '/api/stream') {
        const body = JSON.parse(init.body);
        posts.push(body);
        if (refuse) return response({ status: 400, text: refuse });
        held[body.stream] = body.on;
        return response({ json: { ...held } });
      }
      if (url.startsWith('/api/trial')) {
        if (trialSent) return abortablePoll(open, 'trial')(init);
        trialSent = true;
        return response({ json: VIEW, headers: { 'X-Trial-Seq': '1' } });
      }
      if (url.startsWith('/api/gaze')) return abortablePoll(open, 'gaze')(init);
      if (url.startsWith('/api/camera')) return abortablePoll(open, 'camera')(init);
      throw new Error('unexpected fetch ' + url);
    },
  });
  await settle();
  await settle();
  const asked = (prefix) => page.fetches.filter((call) => call.url.startsWith(prefix)).length;
  return { page, open, posts, held, asked };
}

const button = (page, name) => page.byId('stream-' + name);

describe('the live stream switches', () => {
  it('asks the server how they stand and polls only the streams that are on', async () => {
    const { page, open, asked } = await livePage({ held: { gaze_traces: false, camera: true } });
    assert.equal(asked('/api/streams'), 1);
    assert.equal(asked('/api/gaze'), 0, 'gaze traces are off: never asked for');
    assert.equal(open.camera, 1, 'the camera is on: one open long poll');
    assert.equal(button(page, 'gaze_traces').getAttribute('aria-checked'), 'false');
    assert.equal(button(page, 'camera').getAttribute('aria-checked'), 'true');
    page.runFrames();
    assert.equal(page.byId('trial-status').textContent, 'Gaze traces are off.');
  });

  it('switching gaze traces off sends the switch and ends the open gaze request', async () => {
    const { page, open, posts, asked } = await livePage();
    assert.equal(open.gaze, 1);
    button(page, 'gaze_traces').onclick();
    await settle();
    await settle();
    assert.deepEqual(posts, [{ stream: 'gaze_traces', on: false }]);
    assert.equal(open.gaze, 0, 'the open request was aborted');
    const before = asked('/api/gaze');
    page.runTimers();
    await settle();
    assert.equal(asked('/api/gaze'), before, 'no new gaze request while off');
    assert.equal(button(page, 'gaze_traces').getAttribute('aria-checked'), 'false');
    assert.equal(page.consoleErrors.length, 0, 'switching off is not a failure');

    button(page, 'gaze_traces').onclick();
    await settle();
    await settle();
    assert.deepEqual(posts.at(-1), { stream: 'gaze_traces', on: true });
    assert.equal(open.gaze, 1, 'switched back on: the gaze loop runs again');
  });

  it('switching the eye camera off stops its poll and says so in the camera box', async () => {
    const { page, open } = await livePage();
    button(page, 'camera').onclick();
    await settle();
    await settle();
    assert.equal(open.camera, 0);
    assert.equal(page.byId('trial-camera').textContent, 'Eye camera is off.');
    assert.equal(page.byId('trial-camera').parentElement.dataset.camera, 'none');
    assert.equal(page.get('cameraRateText')(), 'Eye camera is off.');
  });

  it('says a refused switch under the switches and keeps the stream as it was', async () => {
    const { page, open } = await livePage({ refuse: 'unknown stream' });
    button(page, 'camera').onclick();
    await settle();
    await settle();
    assert.equal(page.byId('streams-note').textContent, 'Eye camera not switched: unknown stream');
    assert.equal(button(page, 'camera').getAttribute('aria-checked'), 'true');
    assert.equal(open.camera, 1, 'still polling');
    assert.match(String(page.consoleErrors[0][0]), /stream switch failed/);
  });

  it('follows a switch made on another page when the next state arrives', async () => {
    const states = [
      { ...SAVED_STATE, revision: 1, status: 'running' },
      { ...SAVED_STATE, revision: 2, status: 'running' },
    ];
    const { page, open, held } = await livePage({ states });
    assert.equal(open.camera, 1);
    held.camera = false; // another tab, or the dashboard's frame, switched it
    page.runTimers();
    await settle();
    await settle();
    assert.equal(open.camera, 0);
    assert.equal(button(page, 'camera').getAttribute('aria-checked'), 'false');
  });

  it('greys out the gaze switch on a rig with no eye tracker', async () => {
    const { page } = await livePage();
    page.get('trialView').view = { ...VIEW, gaze: false, camera: { live: false, has_frames: false, reason: 'x' } };
    page.get('renderStreamSwitches')();
    assert.equal(button(page, 'gaze_traces').disabled, true);
    assert.equal(button(page, 'camera').disabled, true);
  });

  it('hides the switches on the saved copy, which has no session to switch', () => {
    const page = loadLiveMonitor();
    assert.equal(page.byId('streams').hasAttribute('hidden'), true);
    assert.equal(page.fetches.length, 0);
  });
});
