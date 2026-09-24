/* The page as a whole: rendering a state into panels, the saved copy that
 * never polls, and the live page's three conversations with the server —
 * the state long-poll, the camera stream and the tracker settings.
 *
 * Run: node --test "tests/js/*.test.mjs"
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { SAVED_STATE, loadDashboard, settle } from './load_dashboard.mjs';

function stateWith(panels, extra) {
  return { ...SAVED_STATE, panels: panels, ...extra };
}

const grid = (dashboard) => dashboard.byId('grid');
const cards = (dashboard) => grid(dashboard).children;

/* A camera panel as the session's eye-tracker monitor sends it: the picture
 * streams on its own channel, and one tracker setting can be changed. */
const CAMERA_PANEL = {
  title: 'eye camera',
  section: 'Eye tracker',
  data: {
    form: 'image',
    stream: true,
    controls: [
      { setting: 'iris', label: 'Iris size', unit: 'px', min: 1, max: 50, step: 1, value: 20 },
    ],
  },
};

/* A response with only what dashboard.js reads from one. */
function response({ status = 200, json, text = '', headers = {}, bytes }) {
  return {
    ok: status >= 200 && status < 300,
    status: status,
    json: async () => json,
    text: async () => text,
    headers: new Map(Object.entries(headers)),
    arrayBuffer: async () => new Uint8Array(bytes || []).buffer,
  };
}

/* Never answers: parks a long-poll loop so it asks for nothing more. */
const pending = () => new Promise(() => {});

/**
 * The live page, answering /api/state with `states` in turn (the last one
 * repeats), /api/camera with `camera` in turn (then never again), and
 * /api/tracker with `tracker`.
 */
async function livePage({ states, camera = [], tracker }) {
  const stateQueue = [...states];
  const cameraQueue = [...camera];
  const dashboard = loadDashboard({
    staticState: null,
    search: '?token=abc',
    fetch: (url, init) => {
      if (url.startsWith('/api/state')) {
        const next = stateQueue.length > 1 ? stateQueue.shift() : stateQueue[0];
        return response({ json: next });
      }
      if (url.startsWith('/api/camera')) return cameraQueue.length ? cameraQueue.shift() : pending();
      if (url.startsWith('/api/tracker')) return tracker(url, init);
      throw new Error('unexpected fetch ' + url);
    },
  });
  await settle();
  return dashboard;
}

describe('a saved page', () => {
  it('renders its state once and never polls a server', () => {
    const dashboard = loadDashboard({
      staticState: stateWith([{ title: 'accuracy', data: { form: 'stat', value: '0.8', label: 'x' } }]),
    });
    assert.equal(cards(dashboard).length, 1);
    assert.equal(dashboard.fetches.length, 0);
    assert.deepEqual(dashboard.pendingTimers(), []);
  });

  it('letters the panels a, b, c in order and starts each title with a capital', () => {
    const stat = { form: 'stat', value: '1', label: 'x' };
    const dashboard = loadDashboard({
      staticState: stateWith([
        { title: 'accuracy', data: stat },
        { title: 'reaction time', data: stat },
        { title: 'P(occluder) by alignment', data: stat },
      ]),
    });
    const headings = cards(dashboard).map((card) => card.querySelector('h2').textContent);
    assert.deepEqual(headings, ['aAccuracy', 'bReaction time', 'cP(occluder) by alignment']);
  });

  it('draws a form it does not know as a placeholder rather than a blank card', () => {
    const dashboard = loadDashboard({ staticState: stateWith([{ title: 'x', data: { form: 'radar' } }]) });
    assert.equal(cards(dashboard)[0].querySelector('div.empty').textContent, 'Nothing to draw');
  });

  it('loses only the table of a panel whose table cannot be built, and says why', () => {
    // A bars payload with no items list: the chart copes, the table throws.
    const dashboard = loadDashboard({ staticState: stateWith([{ title: 'x', data: { form: 'bars' } }]) });
    const card = cards(dashboard)[0];
    assert.equal(card.querySelector('div.empty').textContent, 'No data yet');
    assert.equal(card.querySelector('details.table'), null);
    assert.match(String(dashboard.consoleErrors[0][0]), /table view failed for a bars panel/);
  });

  it('keeps the tracker controls inert: a saved page has no session to send to', () => {
    const dashboard = loadDashboard({ staticState: stateWith([CAMERA_PANEL], { status: 'paused' }) });
    const controls = cards(dashboard)[0].querySelectorAll('button').concat(
      cards(dashboard)[0].querySelectorAll('input'),
    );
    assert.equal(controls.length, 3);
    controls.forEach((control) => assert.equal(control.disabled, true));
  });
});

describe('the eye-tracker panels', () => {
  it('draw a camera picture as grey pixels on a canvas', () => {
    const pixels = Buffer.from([0, 128, 255, 64]).toString('base64');
    const dashboard = loadDashboard({
      staticState: stateWith([
        { title: 'camera', data: { form: 'image', pixels: pixels, width: 2, height: 2 } },
      ]),
    });
    const canvas = cards(dashboard)[0].querySelector('canvas.camera');
    assert.ok(canvas, 'no camera canvas drawn');
    assert.deepEqual([canvas.width, canvas.height], [2, 2]);
    const painted = canvas.getContext('2d').painted;
    assert.equal(painted.length, 1);
    assert.deepEqual(
      [...painted[0].image.data],
      [0, 0, 0, 255, 128, 128, 128, 255, 255, 255, 255, 255, 64, 64, 64, 255],
    );
  });

  it('say a picture whose bytes do not fill its size is malformed, instead of drawing it', () => {
    const pixels = Buffer.from([1, 2, 3]).toString('base64');
    const dashboard = loadDashboard({
      staticState: stateWith([
        { title: 'camera', data: { form: 'image', pixels: pixels, width: 2, height: 2 } },
      ]),
    });
    const card = cards(dashboard)[0];
    assert.equal(card.querySelector('canvas'), null);
    assert.equal(card.querySelector('div.empty').textContent, 'Malformed image: 3 bytes for 2×2 pixels');
  });

  it('colour a verdict tile by its status', () => {
    const dashboard = loadDashboard({
      staticState: stateWith([{
        title: 'validation',
        data: { form: 'stat', value: '1.4', unit: '°', label: 'worst error', status: 'critical' },
      }]),
    });
    const tile = cards(dashboard)[0].querySelector('div.stat-tile');
    assert.equal(tile.dataset.status, 'critical');
    assert.equal(tile.querySelector('.tile-value').textContent, '1.4 °');
  });

  it('show a running procedure\'s progress in place of the pause hint, with the controls off', () => {
    const dashboard = loadDashboard({
      staticState: stateWith([], { status: 'calibrating', message: 'Target 3 of 9' }),
    });
    assert.equal(dashboard.byId('notice').textContent, 'Target 3 of 9');
    assert.equal(dashboard.byId('status').dataset.state, 'calibrating');
    dashboard.document.querySelectorAll('[data-command]').forEach((button) => {
      assert.equal(button.disabled, true, button.dataset.command);
    });
  });

  it('turn the session controls on only while paused', () => {
    const dashboard = loadDashboard({ staticState: stateWith([], { status: 'paused' }) });
    const buttons = dashboard.document.querySelectorAll('[data-command]');
    assert.ok(buttons.length > 0);
    buttons.forEach((button) => assert.equal(button.disabled, false, button.dataset.command));
  });
});

describe('the live page', () => {
  const paused = (revision) => stateWith([CAMERA_PANEL], { status: 'paused', revision: revision });

  it('asks for the state with its token, and polls again after drawing it', async () => {
    const dashboard = await livePage({ states: [paused(1)] });
    assert.equal(dashboard.fetches[0].url, '/api/state?token=abc&revision=0');
    assert.equal(cards(dashboard).length, 1);
    assert.deepEqual(dashboard.pendingTimers(), [{ ms: 50 }]);
  });

  it('does not rebuild the page for a revision it has already drawn', async () => {
    // The long poll answers a timeout with the same snapshot; rebuilding it
    // would throw away the reader's hover and scroll position.
    const dashboard = await livePage({ states: [paused(1), paused(1)] });
    const card = cards(dashboard)[0];
    dashboard.runTimers();
    await settle();
    assert.equal(dashboard.fetches.filter((call) => call.url.startsWith('/api/state')).length, 2);
    assert.equal(cards(dashboard)[0], card);
  });

  it('says it is disconnected when the server cannot be reached', async () => {
    const dashboard = loadDashboard({
      staticState: null,
      fetch: (url) => (url.startsWith('/api/state') ? Promise.reject(new Error('refused')) : pending()),
    });
    await settle();
    assert.equal(dashboard.byId('status').textContent, 'disconnected');
    assert.equal(dashboard.byId('status').dataset.state, 'disconnected');
  });

  it('keeps a half-typed tracker setting when the state is redrawn under it', async () => {
    const dashboard = await livePage({ states: [paused(1), paused(2)] });
    const typing = grid(dashboard).querySelector('input[data-setting="iris"]');
    typing.value = '17';
    typing.focus();

    dashboard.runTimers();
    await settle();

    const redrawn = grid(dashboard).querySelector('input[data-setting="iris"]');
    assert.notEqual(redrawn, typing, 'the panel was not rebuilt, so this proves nothing');
    assert.equal(redrawn.value, '17');
    assert.equal(dashboard.document.activeElement, redrawn);
  });
});

describe('the live camera stream', () => {
  const frame = (bytes, seq) => response({
    headers: { 'X-Frame-Width': '2', 'X-Frame-Height': '1', 'X-Frame-Seq': String(seq) },
    bytes: bytes,
  });

  it('paints a new frame into the canvas without rebuilding any panel', async () => {
    // The frame is held back until the page is drawn, so a rebuild it caused
    // would replace the card captured here.
    let deliver;
    const held = new Promise((resolve) => { deliver = resolve; });
    const dashboard = await livePage({
      states: [stateWith([CAMERA_PANEL], { status: 'paused' })],
      camera: [held],
    });
    const card = cards(dashboard)[0];
    const canvas = card.querySelector('canvas.camera');

    deliver(frame([10, 200], 7));
    await settle();
    assert.equal(dashboard.runFrames(), 1);

    assert.equal(cards(dashboard)[0], card, 'a frame rebuilt the panels');
    assert.deepEqual([...canvas.getContext('2d').painted.at(-1).image.data], [
      10, 10, 10, 255, 200, 200, 200, 255,
    ]);
    assert.match(card.querySelector('.camera-live').textContent, /^Live · \d+ frames\/s$/);
    // The next request asks for the frame after the one just received.
    const cameraCalls = dashboard.fetches.filter((call) => call.url.startsWith('/api/camera'));
    assert.match(cameraCalls.at(-1).url, /&after=7&/);
  });

  it('says under the image why frames stopped, logs it, and retries', async () => {
    const dashboard = await livePage({
      states: [stateWith([CAMERA_PANEL], { status: 'paused' })],
      camera: [response({ status: 500, text: 'tracker offline' })],
    });
    dashboard.runFrames();

    const line = cards(dashboard)[0].querySelector('.camera-live');
    assert.equal(line.textContent, 'Camera stream failed: the server answered 500: tracker offline');
    assert.match(String(dashboard.consoleErrors[0][0]), /camera stream failed/);
    assert.ok(dashboard.pendingTimers().some((timer) => timer.ms === 1000), 'no retry scheduled');
  });

  it('refuses a frame whose bytes do not match its size, and says so', async () => {
    const dashboard = await livePage({
      states: [stateWith([CAMERA_PANEL], { status: 'paused' })],
      camera: [frame([1, 2, 3], 1)],
    });
    dashboard.runFrames();
    const line = cards(dashboard)[0].querySelector('.camera-live');
    assert.equal(line.textContent, 'Camera stream failed: a frame of 3 bytes for 2×1 pixels');
  });
});

describe('tracker settings', () => {
  const paused = stateWith([CAMERA_PANEL], { status: 'paused' });
  const button = (dashboard, text) =>
    grid(dashboard).querySelectorAll('button').find((node) => node.textContent === text);

  it('go to their own endpoint with the session token, and the notice says so', async () => {
    const sent = [];
    const dashboard = await livePage({
      states: [paused],
      tracker: (url, init) => {
        sent.push({ url: url, init: init });
        return response({});
      },
    });
    button(dashboard, '+').click();
    await settle();

    assert.equal(sent.length, 1);
    assert.equal(sent[0].url, '/api/tracker');
    assert.equal(sent[0].init.method, 'POST');
    assert.equal(sent[0].init.headers['X-Alhazen-Token'], 'abc');
    const body = JSON.parse(sent[0].init.body);
    assert.deepEqual([body.setting, body.value], ['iris', 21]);
    assert.equal(dashboard.byId('notice').textContent, 'Iris size: 21 px sent to the tracker…');
  });

  it('say a refusal in the notice', async () => {
    const dashboard = await livePage({
      states: [paused],
      tracker: () => response({ status: 409, text: 'the session is not paused' }),
    });
    button(dashboard, '−').click(); // the minus-sign button, U+2212
    await settle();
    assert.equal(dashboard.byId('notice').textContent, 'Iris size not changed: the session is not paused');
  });

  it('log a send that fails and say it in the notice', async () => {
    const dashboard = await livePage({
      states: [paused],
      tracker: () => Promise.reject(new Error('offline')),
    });
    button(dashboard, '+').click();
    await settle();
    assert.equal(dashboard.byId('notice').textContent, 'Iris size not sent: Error: offline');
    assert.match(String(dashboard.consoleErrors[0][0]), /tracker setting failed/);
  });

  it('refuse a value outside the setting\'s range without sending it', async () => {
    const dashboard = await livePage({
      states: [paused],
      tracker: () => assert.fail('an out-of-range value was sent'),
    });
    const input = grid(dashboard).querySelector('input[data-setting="iris"]');
    input.value = '51';
    input.onchange();
    await settle();
    assert.equal(dashboard.byId('notice').textContent, 'Iris size must be a whole number from 1 to 50 px.');
  });
});
