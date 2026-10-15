/* The live monitor's Trial view: the running trial's screen in degrees, the
 * gaze and its trail over it, the camera box beside it, and the two long
 * polls that feed it (/api/trial, /api/gaze).
 *
 * Run: node --test "tests/js/*.test.mjs"
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { SAVED_STATE, loadLiveMonitor, settle } from './load_live_monitor.mjs';

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
const pending = () => new Promise(() => {});
/* Values made in the page's realm, as plain values of this one, so a deep
 * comparison compares contents and not which realm built the array. */
const plain = (value) => JSON.parse(JSON.stringify(value));

/* A task's own picture on a 40 × 30 dva screen: a fixation point, its window,
 * a target window and an approximate bar. */
const VIEW = {
  trial_index: 4,
  attempt: 2,
  t: 100,
  screen: { width_dva: 40, height_dva: 30 },
  source: 'task',
  shapes: [
    { kind: 'circle', role: 'stimulus', name: 'fixation', center: [0, 0], radius: 0.25 },
    { kind: 'circle', role: 'region', name: 'Fixation window', center: [0, 0], radius: 2 },
    { kind: 'circle', role: 'region', name: 'Target window', center: [10, -5], radius: 3 },
    { kind: 'rect', role: 'stimulus', name: 'bar', center: [-10, 5], size: [4, 2], angle_deg: 90, approximate: true },
  ],
  undrawn: [],
  condition: { side: 'right', contrast: 0.5, hemifield: -1 },
  gaze: true,
  camera: { live: false, has_frames: false, reason: 'The mouse stand-in for an eye tracker has no camera.' },
};

describe('trial view math', () => {
  it('measures the gaze rate over the last second of samples, gaps left out', () => {
    const gazeRate = loadLiveMonitor().get('gazeRate');
    const samples = [];
    for (let i = 0; i <= 120; i += 1) samples.push([i / 60, 0, 0]);
    samples.push([2.01, null, null]);
    assert.equal(gazeRate(samples), 60);
    assert.equal(gazeRate(samples.slice(0, 10)), null, 'under a quarter second');
    assert.equal(gazeRate([]), null);
  });

  const live_monitor = loadLiveMonitor();
  const trialFit = live_monitor.get('trialFit');
  const trialMap = live_monitor.get('trialMap');
  const trailSegments = live_monitor.get('trailSegments');

  it('fits the screen in the box by its tighter side and keeps its aspect', () => {
    const wide = trialFit(40, 30, 800, 1000);
    assert.equal(wide.scale, 20);
    assert.deepEqual([wide.width, wide.height], [800, 600]);
    const short = trialFit(40, 30, 800, 300);
    assert.equal(short.scale, 10);
    assert.deepEqual([short.width, short.height], [400, 300]);
  });

  it('maps centred dva with y up to drawing px with y down', () => {
    const fit = trialFit(40, 30, 800, 1000);
    assert.deepEqual(plain(trialMap(fit, 0, 0)), [400, 300]);
    assert.deepEqual(plain(trialMap(fit, -20, 15)), [0, 0]);
    assert.deepEqual(plain(trialMap(fit, 20, -15)), [800, 600]);
    assert.deepEqual(plain(trialMap(fit, 10, 5)), [600, 200]);
  });

  it('keeps the last seconds of the trail and breaks it at every gap', () => {
    const samples = [
      [0.0, 0, 0], [0.5, 1, 0], [1.0, 2, 0], [1.2, null, null], [1.4, 3, 1], [1.6, 4, 1],
    ];
    const all = trailSegments(samples, 0);
    assert.deepEqual(plain(all.segments), [[[0, 0], [1, 0], [2, 0]], [[3, 1], [4, 1]]]);
    assert.deepEqual(plain(all.current), [4, 1]);
    const recent = trailSegments(samples, 1);
    assert.deepEqual(plain(recent.segments), [[[2, 0]], [[3, 1], [4, 1]]]);
  });

  it('shows no current point when the newest sample is a gap', () => {
    const { segments, current } = trailSegments([[0, 1, 1], [0.1, null, null]], 1);
    assert.equal(current, null);
    assert.deepEqual(plain(segments), [[[1, 1]]]);
    assert.deepEqual(plain(trailSegments([], 1)), { segments: [], current: null });
  });

  it('starts a fresh trail at a new trial and drops a late sample from an older one', () => {
    const takeGaze = live_monitor.get('takeGaze');
    const tv = live_monitor.get('trialView');
    takeGaze({ seq: 2, missed: 0, samples: [[3, 1.0, 0, 0], [3, 1.1, 1, 0]] });
    assert.equal(tv.gazeTrial, 3);
    assert.equal(tv.samples.length, 2);
    takeGaze({ seq: 4, missed: 1, samples: [[4, 2.0, 5, 5], [3, 1.2, 9, 9]] });
    assert.equal(tv.gazeTrial, 4);
    assert.deepEqual(plain(tv.samples), [[2.0, 5, 5]]);
    assert.equal(tv.gazeSeq, 4);
    assert.equal(tv.missed, 1);
  });
});

describe('the trial picture', () => {
  function drawn(view, samples) {
    const live_monitor = loadLiveMonitor();
    const tv = live_monitor.get('trialView');
    tv.view = view;
    if (samples) {
      tv.gazeTrial = view.trial_index;
      tv.samples = samples;
    }
    live_monitor.get('drawTrialStage')();
    return live_monitor;
  }

  it('draws every shape where its dva say, regions as outlines over stimuli', () => {
    const live_monitor = drawn(VIEW);
    const svg = live_monitor.byId('trial-stage').querySelector('svg');
    const fit = live_monitor.get('trialView').fit;
    const circles = svg.querySelectorAll('circle').filter((c) => c.classList.contains('trial-shape'));
    // Stimuli are drawn first, regions after: a window is never under a disc.
    assert.deepEqual(circles.map((c) => c.getAttribute('class')),
      ['trial-shape role-stimulus', 'trial-shape role-region', 'trial-shape role-region']);
    const target = circles[2];
    assert.equal(Number(target.getAttribute('cx')), (10 + 20) * fit.scale);
    assert.equal(Number(target.getAttribute('cy')), (15 + 5) * fit.scale);
    assert.equal(Number(target.getAttribute('r')), 3 * fit.scale);
    // A rect turned 90° stands upright: 2 wide, 4 tall.
    const bar = svg.querySelectorAll('polygon')[0];
    assert.match(bar.getAttribute('class'), /approximate/);
    const points = bar.getAttribute('points').split(' ').map((p) => p.split(',').map(Number));
    const xs = points.map((p) => p[0]);
    const ys = points.map((p) => p[1]);
    assert.ok(Math.abs(Math.max(...xs) - Math.min(...xs) - 2 * fit.scale) < 1e-6);
    assert.ok(Math.abs(Math.max(...ys) - Math.min(...ys) - 4 * fit.scale) < 1e-6);
  });

  it('says what the trial is and keys the picture; a task picture needs no note', () => {
    const live_monitor = drawn(VIEW);
    assert.equal(live_monitor.byId('trial-title').textContent, 'Trial 4');
    assert.equal(live_monitor.byId('trial-meta').textContent, 'attempt 2 · side right · contrast 0.5 · hemifield −1');
    const legend = live_monitor.byId('trial-legend').children.map((item) => item.textContent);
    assert.deepEqual(legend, ['Gaze', 'Trail', 'Stimulus', 'Gaze window', 'Approximate outline']);
    assert.equal(live_monitor.byId('trial-note').textContent, '');
  });

  it('names each shape on hover exactly as the task gave it', () => {
    const feedback = { kind: 'polygon', role: 'region', name: 'feedback', points: [[0, 0], [4, 0], [0, 4]] };
    const live_monitor = drawn({ ...VIEW, shapes: [...VIEW.shapes, feedback] });
    const titles = live_monitor.byId('trial-stage').querySelector('svg').querySelectorAll('title')
      .map((title) => title.textContent);
    // alhazen names its own windows "… window"; a task's region keeps its own name.
    assert.deepEqual(titles.sort(), ['Fixation window', 'Target window', 'bar (approximate)', 'feedback', 'fixation'].sort());
  });

  it('keys outlines and paths drawn as annotation; words on the picture need no key', () => {
    const annotations = [
      { kind: 'polygon', role: 'annotation', name: 'surface (hidden)', points: [[0, 0], [2, 0], [0, 2]] },
      { kind: 'line', role: 'annotation', name: 'path', start: [0, 0], end: [3, 0] },
      { kind: 'label', role: 'annotation', name: 'motion', at: [0, 5], text: 'strip slides' },
    ];
    let live_monitor = drawn({ ...VIEW, shapes: [...VIEW.shapes, ...annotations] });
    let items = live_monitor.byId('trial-legend').children;
    assert.deepEqual(items.map((item) => item.textContent),
      ['Gaze', 'Trail', 'Stimulus', 'Outline or path', 'Gaze window', 'Approximate outline']);
    // The swatch is styled like the drawn annotation (a thin solid outline).
    assert.equal(items[3].children[0].getAttribute('class'), 'annotation');
    live_monitor = drawn({ ...VIEW, shapes: [...VIEW.shapes, annotations[2]] });
    items = live_monitor.byId('trial-legend').children;
    assert.ok(!items.map((item) => item.textContent).includes('Outline or path'), 'a label alone has no key');
  });

  it('a task with no picture shows its gaze windows alone and says so once', () => {
    const regionsOnly = {
      ...VIEW,
      source: 'regions',
      shapes: VIEW.shapes.filter((shape) => shape.role === 'region'),
    };
    const live_monitor = drawn(regionsOnly);
    const stage = live_monitor.byId('trial-stage');
    assert.equal(stage.querySelectorAll('.trial-shape.role-region').length, 2);
    assert.equal(stage.querySelectorAll('.trial-shape.role-stimulus').length, 0);
    const legend = live_monitor.byId('trial-legend').children.map((item) => item.textContent);
    assert.deepEqual(legend, ['Gaze', 'Trail', 'Gaze window']);
    assert.equal(live_monitor.byId('trial-note').textContent,
      'This task defines no picture of its stimuli, so only its gaze windows are shown.');
  });

  it('a regions-only trial with no windows, and a window that could not be drawn, say so', () => {
    const empty = drawn({ ...VIEW, source: 'regions', shapes: [] });
    assert.equal(empty.byId('trial-note').textContent,
      'This task defines no picture of its stimuli, and this trial has no gaze windows to show.');
    const bad = drawn({ ...VIEW, source: 'regions', shapes: VIEW.shapes.slice(1, 2), undrawn: ['target (window: radius must be positive)'] });
    assert.equal(bad.byId('trial-note').textContent,
      'This task defines no picture of its stimuli, so only its gaze windows are shown. ' +
      'Gaze window not drawn: target (window: radius must be positive).');
  });

  it('draws the trail and the current gaze over the picture', () => {
    const live_monitor = drawn(VIEW, [[1.0, 0, 0], [1.1, 1, 0], [1.2, 2, 1]]);
    const layer = live_monitor.byId('trial-stage').querySelector('g.trial-gaze-layer');
    const fit = live_monitor.get('trialView').fit;
    const line = layer.querySelector('polyline');
    assert.ok(line, 'a trail');
    const gaze = layer.querySelector('circle');
    assert.equal(Number(gaze.getAttribute('cx')), 22 * fit.scale);
    assert.equal(Number(gaze.getAttribute('cy')), 14 * fit.scale);
  });

  it('hides the point during a blink and says so', () => {
    const live_monitor = drawn(VIEW, [[1.0, 0, 0], [1.1, null, null]]);
    live_monitor.get("state = { status: 'running' }");
    live_monitor.get('paintGaze')();
    const layer = live_monitor.byId('trial-stage').querySelector('g.trial-gaze-layer');
    assert.equal(layer.querySelector('circle'), null);
    assert.equal(live_monitor.byId('trial-status').textContent,
      'No gaze sample: a blink, or the eye is lost.');
  });

  it('says paused, with or without a trail, whatever the gaze clock says', () => {
    const live_monitor = drawn(VIEW, [[1.0, 0, 0], [1.1, 1, 1]]);
    live_monitor.get("state = { status: 'paused' }");
    live_monitor.get('paintGaze')();
    assert.equal(live_monitor.byId('trial-status').textContent,
      'Paused. The trail is this trial’s, up to the pause.');
    live_monitor.get('trialView').samples = [];
    live_monitor.get('paintGaze')();
    assert.equal(live_monitor.byId('trial-status').textContent, 'Paused.');
  });

  it('draws no gaze from another trial over this trial’s picture', () => {
    const live_monitor = loadLiveMonitor();
    const tv = live_monitor.get('trialView');
    tv.view = VIEW;
    tv.gazeTrial = VIEW.trial_index + 1;
    tv.samples = [[1.0, 0, 0], [1.1, 1, 1]];
    live_monitor.get('drawTrialStage')();
    const layer = live_monitor.byId('trial-stage').querySelector('g.trial-gaze-layer');
    assert.equal(layer.children.length, 0);
  });

  it('says how long before the trial the last real camera frame was taken', () => {
    const cameraAgeText = loadLiveMonitor().get('cameraAgeText');
    const camera = { reason: 'Reason.' };
    assert.equal(cameraAgeText(camera, { t: 58 }, { t: 100 }, false),
      'Last frame, taken 42 s before this trial (paused or calibrating). Reason.');
    assert.equal(cameraAgeText(camera, { t: 0 }, { t: 600 }, false),
      'Last frame, taken 10 min before this trial (paused or calibrating). Reason.');
  });
});

describe('the live page', () => {
  async function livePage({ trial, gaze, camera = [] }) {
    const trials = [...trial];
    const gazes = [...gaze];
    const cameras = [...camera];
    const live_monitor = loadLiveMonitor({
      staticState: null,
      search: '?token=abc',
      fetch: (url) => {
        if (url.startsWith('/api/state')) {
          return response({ json: { ...SAVED_STATE, revision: 1, status: 'running' } });
        }
        if (url.startsWith('/api/trial')) return trials.length ? trials.shift() : pending();
        if (url.startsWith('/api/gaze')) return gazes.length ? gazes.shift() : pending();
        if (url.startsWith('/api/camera')) return cameras.length ? cameras.shift() : pending();
        throw new Error('unexpected fetch ' + url);
      },
    });
    await settle();
    await settle();
    return live_monitor;
  }

  it('lists Trial in the sidebar and shows the view above the panels', async () => {
    const live_monitor = await livePage({ trial: [], gaze: [] });
    const rows = live_monitor.byId('sections').children.map((li) => li.textContent);
    assert.equal(rows[1], 'Trial');
    assert.equal(live_monitor.byId('trial-view').hasAttribute('hidden'), false);
  });

  it('asks for the picture and the gaze with the token, and draws both', async () => {
    const live_monitor = await livePage({
      trial: [response({ json: VIEW, headers: { 'X-Trial-Seq': '3' } })],
      gaze: [response({ json: { seq: 2, missed: 0, samples: [[4, 1.0, 1, 1], [4, 1.02, 2, 1]] } })],
    });
    const urls = live_monitor.fetches.map((f) => f.url);
    assert.ok(urls.includes('/api/trial?token=abc&after=0&wait_ms=2000'));
    assert.ok(urls.includes('/api/gaze?token=abc&after=0&wait_ms=2000'));
    assert.ok(urls.includes('/api/trial?token=abc&after=3&wait_ms=2000'), 'asks for the next trial');
    assert.ok(urls.includes('/api/gaze?token=abc&after=2&wait_ms=2000'), 'asks for newer gaze');
    live_monitor.runFrames();
    const layer = live_monitor.byId('trial-stage').querySelector('g.trial-gaze-layer');
    assert.ok(layer.querySelector('circle'), 'the current gaze');
    // Two samples 20 ms apart are no span to measure a rate over yet.
    assert.equal(live_monitor.byId('trial-status').textContent, 'Gaze 2.0°, 1.0°');
  });

  it('shows the tracker’s reason in place of a camera image, never a picture', async () => {
    const live_monitor = await livePage({
      trial: [response({ json: VIEW, headers: { 'X-Trial-Seq': '1' } })],
      gaze: [],
    });
    const box = live_monitor.byId('trial-camera');
    assert.equal(box.querySelector('canvas'), null);
    assert.equal(live_monitor.byId('trial-camera-note').textContent, VIEW.camera.reason);
    /* Marked for its layout: a tracker with no camera gets one line on a
     * narrow page, not an empty picture. */
    assert.equal(box.parentElement.dataset.camera, 'none');
  });

  it('shows the last real frame and its age when the tracker has a camera', async () => {
    const withCamera = { ...VIEW, camera: { live: false, has_frames: true, reason: 'Read while paused.' } };
    const live_monitor = await livePage({
      trial: [response({ json: withCamera, headers: { 'X-Trial-Seq': '1' } })],
      gaze: [],
      camera: [response({
        bytes: [1, 2, 3, 4, 5, 6],
        headers: { 'X-Frame-Width': '3', 'X-Frame-Height': '2', 'X-Frame-Seq': '1', 'X-Frame-Time': '70.000' },
      })],
    });
    await settle();
    const canvas = live_monitor.byId('trial-camera').querySelector('canvas');
    assert.ok(canvas, 'the frame is drawn');
    assert.equal(live_monitor.byId('trial-camera').parentElement.dataset.camera, 'frame');
    assert.equal(live_monitor.byId('trial-camera-note').textContent,
      'Last frame, taken 30 s before this trial (paused or calibrating). Read while paused.');
  });

  it('says a failing stream in the status line and the console, and retries', async () => {
    const live_monitor = await livePage({
      trial: [response({ status: 500, text: 'boom' })],
      gaze: [],
    });
    assert.match(String(live_monitor.consoleErrors[0][0]), /trial stream failed/);
    live_monitor.runFrames();
    assert.match(live_monitor.byId('trial-status').textContent, /^Trial stream failed: the server answered 500: boom$/);
    assert.ok(live_monitor.pendingTimers().some((t) => t.ms === 1000));
  });

  it('keeps the view hidden on the saved copy, which has no trial running', () => {
    const live_monitor = loadLiveMonitor();
    assert.equal(live_monitor.byId('trial-view').hasAttribute('hidden'), true);
    const rows = live_monitor.byId('sections').children.map((li) => li.textContent);
    assert.ok(!rows.includes('Trial'));
  });
});
