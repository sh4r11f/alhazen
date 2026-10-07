/* The page as a whole: rendering a state into panels, the saved copy that
 * never polls, and the live page's three conversations with the server —
 * the state long-poll, the camera stream and the tracker settings.
 *
 * Run: node --test "tests/js/*.test.mjs"
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { SAVED_STATE, loadLiveMonitor, settle } from './load_live_monitor.mjs';

function stateWith(panels, extra) {
  return { ...SAVED_STATE, panels: panels, ...extra };
}

const grid = (live_monitor) => live_monitor.byId('grid');
const cards = (live_monitor) => grid(live_monitor).children;

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

/* A response with only what live_monitor.js reads from one. */
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
  const live_monitor = loadLiveMonitor({
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
  return live_monitor;
}

describe('a saved page', () => {
  it('renders its state once and never polls a server', () => {
    const live_monitor = loadLiveMonitor({
      staticState: stateWith([{ title: 'accuracy', data: { form: 'stat', value: '0.8', label: 'x' } }]),
    });
    assert.equal(cards(live_monitor).length, 1);
    assert.equal(live_monitor.fetches.length, 0);
    assert.deepEqual(live_monitor.pendingTimers(), []);
  });

  it('titles each panel with a capital and no letter in front of it', () => {
    const stat = { form: 'stat', value: '1', label: 'x' };
    const live_monitor = loadLiveMonitor({
      staticState: stateWith([
        { title: 'accuracy', data: stat },
        { title: 'reaction time', data: stat },
        { title: 'P(occluder) by alignment', data: stat },
      ]),
    });
    const headings = cards(live_monitor).map((card) => card.querySelector('h2').textContent);
    /* No a, b, c in front of the titles (the owner's request, 2026-10-06). */
    assert.deepEqual(headings, ['Accuracy', 'Reaction time', 'P(occluder) by alignment']);
    assert.equal(live_monitor.document.querySelector('.panel-letter'), null);
  });

  it('draws a form it does not know as a placeholder rather than a blank card', () => {
    const live_monitor = loadLiveMonitor({ staticState: stateWith([{ title: 'x', data: { form: 'radar' } }]) });
    assert.equal(cards(live_monitor)[0].querySelector('div.empty').textContent, 'Nothing to draw');
  });

  it('loses only the table of a panel whose table cannot be built, and says why', () => {
    // A bars payload with no items list: the chart copes, the table throws.
    const live_monitor = loadLiveMonitor({ staticState: stateWith([{ title: 'x', data: { form: 'bars' } }]) });
    const card = cards(live_monitor)[0];
    assert.equal(card.querySelector('div.empty').textContent, 'No data yet');
    assert.equal(card.querySelector('details.table'), null);
    assert.match(String(live_monitor.consoleErrors[0][0]), /table view failed for a bars panel/);
  });

  it('keeps the tracker controls inert: a saved page has no session to send to', () => {
    const live_monitor = loadLiveMonitor({ staticState: stateWith([CAMERA_PANEL], { status: 'paused' }) });
    const controls = cards(live_monitor)[0].querySelectorAll('button').concat(
      cards(live_monitor)[0].querySelectorAll('input'),
    );
    assert.equal(controls.length, 3);
    controls.forEach((control) => assert.equal(control.disabled, true));
  });
});

describe('the eye-tracker panels', () => {
  it('draw a camera picture as grey pixels on a canvas', () => {
    const pixels = Buffer.from([0, 128, 255, 64]).toString('base64');
    const live_monitor = loadLiveMonitor({
      staticState: stateWith([
        { title: 'camera', data: { form: 'image', pixels: pixels, width: 2, height: 2 } },
      ]),
    });
    const canvas = cards(live_monitor)[0].querySelector('canvas.camera');
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
    const live_monitor = loadLiveMonitor({
      staticState: stateWith([
        { title: 'camera', data: { form: 'image', pixels: pixels, width: 2, height: 2 } },
      ]),
    });
    const card = cards(live_monitor)[0];
    assert.equal(card.querySelector('canvas'), null);
    assert.equal(card.querySelector('div.empty').textContent, 'Malformed image: 3 bytes for 2×2 pixels');
  });

  it('draw a circle of the expected iris size on each eye the tracker found', () => {
    const pixels = Buffer.from(new Array(8 * 4).fill(90)).toString('base64');
    const live_monitor = loadLiveMonitor({
      staticState: stateWith([{
        title: 'camera',
        data: {
          form: 'image',
          pixels: pixels,
          width: 8,
          height: 4,
          marks_eyes: true,
          eyes: [
            { eye: 'left', x: 2, y: 1.5, iris_px: 3 },
            { eye: 'right', x: 6, y: 2, iris_px: 3 },
          ],
        },
      }]),
    });
    const card = cards(live_monitor)[0];
    const svg = card.querySelector('svg.camera-eyes');
    // The drawing's units are the picture's px, so a position lands on its pixel.
    assert.equal(svg.getAttribute('viewBox'), '0 0 8 4');
    const circles = svg.querySelectorAll('circle.camera-eye');
    assert.deepEqual(
      circles.map((c) => [c.getAttribute('cx'), c.getAttribute('cy'), c.getAttribute('r')]),
      [['2', '1.5', '1.5'], ['6', '2', '1.5']],
    );
    // A cross on each centre (two lines), and which eye it is.
    assert.equal(svg.querySelectorAll('line.camera-eye').length, 4);
    assert.deepEqual(svg.querySelectorAll('text').map((t) => t.textContent), ['L', 'R']);
    // ...and the panel says what the circle is.
    assert.match(card.querySelector('.legend-slot').textContent, /Expected iris size/);
  });

  it('draw no marker and no key on a picture whose tracker does not say where the eyes are', () => {
    const pixels = Buffer.from([0, 128, 255, 64]).toString('base64');
    const live_monitor = loadLiveMonitor({
      staticState: stateWith([
        { title: 'camera', data: { form: 'image', pixels: pixels, width: 2, height: 2 } },
      ]),
    });
    const card = cards(live_monitor)[0];
    assert.equal(card.querySelector('svg.camera-eyes').children.length, 0);
    assert.equal(card.querySelector('.legend-slot').textContent, '');
  });

  it('put the no-eye alert under the image and its control, where it moves neither', () => {
    // The bug this exists for: the sentence used to be a value in the strip
    // above the image, wrapped onto more lines, and pushed the image and the
    // iris control down each time the eye was lost. It is not laid over the
    // image either: there it hid the picture the eye is being looked for in.
    const alert = 'NO EYE IN THE CAMERA IMAGE — check position, focus and LED (accept is refused)';
    const panel = (extra) => ({
      ...CAMERA_PANEL,
      data: {
        ...CAMERA_PANEL.data,
        stats: [{ label: 'eyes', value: extra.alert ? 'none' : 'both tracked' }],
        ...extra,
      },
    });
    const withAlert = loadLiveMonitor({ staticState: stateWith([panel({ alert: alert })]) });
    const without = loadLiveMonitor({ staticState: stateWith([panel({})]) });

    assert.equal(cards(withAlert)[0].querySelector('.camera-alert').textContent, alert);
    assert.equal(cards(without)[0].querySelector('.camera-alert'), null);
    // Not in the strip above the image, and not inside the picture's box.
    assert.doesNotMatch(cards(withAlert)[0].querySelector('.stats').textContent, /NO EYE/);
    assert.equal(cards(withAlert)[0].querySelector('.camera-box').querySelector('.camera-alert'), null);
    // The picture and the control come first either way, so the alert's
    // coming and going moves only what is under them.
    const outline = (live_monitor) =>
      cards(live_monitor)[0].querySelector('.plot').children.map((node) => node.className);
    assert.deepEqual(outline(without), ['camera-box', 'tracker-control', 'camera-live']);
    assert.deepEqual(
      outline(withAlert),
      ['camera-box', 'tracker-control', 'camera-alert', 'camera-live'],
    );
  });

  it('colour a verdict tile by its status', () => {
    const live_monitor = loadLiveMonitor({
      staticState: stateWith([{
        title: 'validation',
        data: { form: 'stat', value: '1.4', unit: '°', label: 'worst error', status: 'critical' },
      }]),
    });
    const tile = cards(live_monitor)[0].querySelector('div.stat-tile');
    assert.equal(tile.dataset.status, 'critical');
    assert.equal(tile.querySelector('.tile-value').textContent, '1.4 °');
  });

  it('show a running procedure\'s progress in place of the pause hint, with the controls off', () => {
    const live_monitor = loadLiveMonitor({
      staticState: stateWith([], { status: 'calibrating', message: 'Target 3 of 9' }),
    });
    assert.equal(live_monitor.byId('notice').textContent, 'Target 3 of 9');
    assert.equal(live_monitor.byId('status').dataset.state, 'calibrating');
    live_monitor.document.querySelectorAll('[data-command]').forEach((button) => {
      assert.equal(button.disabled, true, button.dataset.command);
    });
  });

  it('leaves its URL alone: it has no token in it', () => {
    const live_monitor = loadLiveMonitor();
    assert.deepEqual(live_monitor.window.history.replaced, []);
  });

  it('turn the session controls on only while paused', () => {
    const live_monitor = loadLiveMonitor({ staticState: stateWith([], { status: 'paused' }) });
    const buttons = live_monitor.document.querySelectorAll('[data-command]');
    assert.ok(buttons.length > 0);
    buttons.forEach((button) => assert.equal(button.disabled, false, button.dataset.command));
  });
});

describe('the live page', () => {
  const paused = (revision) => stateWith([CAMERA_PANEL], { status: 'paused', revision: revision });

  it('asks for the state with its token, and polls again after drawing it', async () => {
    const live_monitor = await livePage({ states: [paused(1)] });
    assert.equal(live_monitor.fetches[0].url, '/api/state?token=abc&revision=0');
    assert.equal(cards(live_monitor).length, 1);
    assert.deepEqual(live_monitor.pendingTimers(), [{ ms: 50 }]);
  });

  it('takes the token out of the address bar and still uses it', async () => {
    // A token left in the URL is on the screen, in the history and in any
    // copied link; the page keeps it in memory and in sessionStorage instead.
    const live_monitor = await livePage({ states: [paused(1)] });
    assert.equal(live_monitor.window.location.search, '');
    assert.deepEqual(live_monitor.window.history.replaced, ['/']);
    assert.equal(live_monitor.window.sessionStorage.getItem('alhazen-token'), 'abc');
    assert.equal(live_monitor.fetches[0].url, '/api/state?token=abc&revision=0');
  });

  it('keeps the rest of the URL when it removes the token', () => {
    const live_monitor = loadLiveMonitor({
      staticState: null,
      search: '?panel=eye&token=abc',
      fetch: pending,
    });
    assert.equal(live_monitor.window.location.search, '?panel=eye');
    assert.deepEqual(live_monitor.window.history.replaced, ['/?panel=eye']);
  });

  it('does not rebuild the page for a revision it has already drawn', async () => {
    // The long poll answers a timeout with the same snapshot; rebuilding it
    // would throw away the reader's hover and scroll position.
    const live_monitor = await livePage({ states: [paused(1), paused(1)] });
    const card = cards(live_monitor)[0];
    live_monitor.runTimers();
    await settle();
    assert.equal(live_monitor.fetches.filter((call) => call.url.startsWith('/api/state')).length, 2);
    assert.equal(cards(live_monitor)[0], card);
  });

  it('says it is disconnected when the server cannot be reached', async () => {
    const live_monitor = loadLiveMonitor({
      staticState: null,
      fetch: (url) => (url.startsWith('/api/state') ? Promise.reject(new Error('refused')) : pending()),
    });
    await settle();
    assert.equal(live_monitor.byId('status').textContent, 'disconnected');
    assert.equal(live_monitor.byId('status').dataset.state, 'disconnected');
  });

  it('keeps a half-typed tracker setting when the state is redrawn under it', async () => {
    const live_monitor = await livePage({ states: [paused(1), paused(2)] });
    const typing = grid(live_monitor).querySelector('input[data-setting="iris"]');
    typing.value = '17';
    typing.focus();

    live_monitor.runTimers();
    await settle();

    const redrawn = grid(live_monitor).querySelector('input[data-setting="iris"]');
    assert.notEqual(redrawn, typing, 'the panel was not rebuilt, so this proves nothing');
    assert.equal(redrawn.value, '17');
    assert.equal(live_monitor.document.activeElement, redrawn);
  });
});

describe('the live camera stream', () => {
  /* `eyes` is the X-Frame-Eyes header as the server sends it (JSON text);
   * left out, the frame has no such header. */
  const frame = (bytes, seq, eyes) => response({
    headers: {
      'X-Frame-Width': '2',
      'X-Frame-Height': '1',
      'X-Frame-Seq': String(seq),
      ...(eyes === undefined ? {} : { 'X-Frame-Eyes': eyes }),
    },
    bytes: bytes,
  });

  it('draws each frame\'s eye markers with it, and clears them when the eye is gone', async () => {
    // Three frames, each held back until the one before is drawn: an eye
    // found, then none found, then a tracker that says nothing about eyes.
    const gates = [0, 1, 2].map(() => {
      let deliver;
      const held = new Promise((resolve) => { deliver = resolve; });
      return { held: held, deliver: deliver };
    });
    const live_monitor = await livePage({
      states: [stateWith([CAMERA_PANEL], { status: 'paused' })],
      camera: gates.map((gate) => gate.held),
    });
    const svg = cards(live_monitor)[0].querySelector('svg.camera-eyes');
    const show = async (index, eyes) => {
      gates[index].deliver(frame([10, 200], index + 1, eyes));
      await settle();
      live_monitor.runFrames();
      return svg.querySelectorAll('circle.camera-eye');
    };

    const found = await show(0, '[{"eye":"right","x":1.25,"y":0.5,"iris_px":1}]');
    assert.deepEqual(
      found.map((c) => [c.getAttribute('cx'), c.getAttribute('cy'), c.getAttribute('r')]),
      [['1.25', '0.5', '0.5']],
    );
    assert.equal(svg.getAttribute('viewBox'), '0 0 2 1');
    assert.equal((await show(1, '[]')).length, 0, 'a circle outlived its eye');
    assert.equal((await show(2)).length, 0);
    assert.equal(svg.children.length, 0);
  });

  it('says under the image that a frame\'s eye markers could not be read', async () => {
    const live_monitor = await livePage({
      states: [stateWith([CAMERA_PANEL], { status: 'paused' })],
      camera: [frame([1, 2], 1, '{"eye":"left"}')],
    });
    live_monitor.runFrames();
    const line = cards(live_monitor)[0].querySelector('.camera-live');
    assert.equal(line.textContent, 'Camera stream failed: eye markers that are not a list: {"eye":"left"}');
    assert.match(String(live_monitor.consoleErrors[0][0]), /camera stream failed/);
  });

  it('paints a new frame into the canvas without rebuilding any panel', async () => {
    // The frame is held back until the page is drawn, so a rebuild it caused
    // would replace the card captured here.
    let deliver;
    const held = new Promise((resolve) => { deliver = resolve; });
    const live_monitor = await livePage({
      states: [stateWith([CAMERA_PANEL], { status: 'paused' })],
      camera: [held],
    });
    const card = cards(live_monitor)[0];
    const canvas = card.querySelector('canvas.camera');

    deliver(frame([10, 200], 7));
    await settle();
    assert.equal(live_monitor.runFrames(), 1);

    assert.equal(cards(live_monitor)[0], card, 'a frame rebuilt the panels');
    assert.deepEqual([...canvas.getContext('2d').painted.at(-1).image.data], [
      10, 10, 10, 255, 200, 200, 200, 255,
    ]);
    assert.match(card.querySelector('.camera-live').textContent, /^Live · \d+ frames\/s$/);
    // The next request asks for the frame after the one just received.
    const cameraCalls = live_monitor.fetches.filter((call) => call.url.startsWith('/api/camera'));
    assert.match(cameraCalls.at(-1).url, /&after=7&/);
  });

  it('says under the image why frames stopped, logs it, and retries', async () => {
    const live_monitor = await livePage({
      states: [stateWith([CAMERA_PANEL], { status: 'paused' })],
      camera: [response({ status: 500, text: 'tracker offline' })],
    });
    live_monitor.runFrames();

    const line = cards(live_monitor)[0].querySelector('.camera-live');
    assert.equal(line.textContent, 'Camera stream failed: the server answered 500: tracker offline');
    assert.match(String(live_monitor.consoleErrors[0][0]), /camera stream failed/);
    assert.ok(live_monitor.pendingTimers().some((timer) => timer.ms === 1000), 'no retry scheduled');
  });

  it('refuses a frame whose bytes do not match its size, and says so', async () => {
    const live_monitor = await livePage({
      states: [stateWith([CAMERA_PANEL], { status: 'paused' })],
      camera: [frame([1, 2, 3], 1)],
    });
    live_monitor.runFrames();
    const line = cards(live_monitor)[0].querySelector('.camera-live');
    assert.equal(line.textContent, 'Camera stream failed: a frame of 3 bytes for 2×1 pixels');
  });
});

describe('tracker settings', () => {
  const paused = stateWith([CAMERA_PANEL], { status: 'paused' });
  const button = (live_monitor, text) =>
    grid(live_monitor).querySelectorAll('button').find((node) => node.textContent === text);

  it('go to their own endpoint with the session token, and the notice says so', async () => {
    const sent = [];
    const live_monitor = await livePage({
      states: [paused],
      tracker: (url, init) => {
        sent.push({ url: url, init: init });
        return response({});
      },
    });
    button(live_monitor, '+').click();
    await settle();

    assert.equal(sent.length, 1);
    assert.equal(sent[0].url, '/api/tracker');
    assert.equal(sent[0].init.method, 'POST');
    assert.equal(sent[0].init.headers['X-Alhazen-Token'], 'abc');
    const body = JSON.parse(sent[0].init.body);
    assert.deepEqual([body.setting, body.value], ['iris', 21]);
    assert.equal(live_monitor.byId('notice').textContent, 'Iris size: 21 px sent to the tracker…');
  });

  it('say a refusal in the notice', async () => {
    const live_monitor = await livePage({
      states: [paused],
      tracker: () => response({ status: 409, text: 'the session is not paused' }),
    });
    button(live_monitor, '−').click(); // the minus-sign button, U+2212
    await settle();
    assert.equal(live_monitor.byId('notice').textContent, 'Iris size not changed: the session is not paused');
  });

  it('log a send that fails and say it in the notice', async () => {
    const live_monitor = await livePage({
      states: [paused],
      tracker: () => Promise.reject(new Error('offline')),
    });
    button(live_monitor, '+').click();
    await settle();
    assert.equal(live_monitor.byId('notice').textContent, 'Iris size not sent: Error: offline');
    assert.match(String(live_monitor.consoleErrors[0][0]), /tracker setting failed/);
  });

  it('refuse a value outside the setting\'s range without sending it', async () => {
    const live_monitor = await livePage({
      states: [paused],
      tracker: () => assert.fail('an out-of-range value was sent'),
    });
    const input = grid(live_monitor).querySelector('input[data-setting="iris"]');
    input.value = '51';
    input.onchange();
    await settle();
    assert.equal(live_monitor.byId('notice').textContent, 'Iris size must be a whole number from 1 to 50 px.');
  });
});
