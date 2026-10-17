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

  it('sizes words on the picture with the picture: the desktop size on a wide one, smaller on a phone', () => {
    const trialLabelSize = loadLiveMonitor().get('trialLabelSize');
    assert.equal(trialLabelSize(1200), 12);
    assert.equal(trialLabelSize(480), 12);
    assert.equal(trialLabelSize(400), 11);
    assert.equal(trialLabelSize(320), 10);
    assert.equal(trialLabelSize(200), 10, 'never under the minimum');
  });

  describe('a label that would not fit', () => {
    const live_monitor = loadLiveMonitor();
    const fitTrialLabel = live_monitor.get('fitTrialLabel');
    const trialFit = live_monitor.get('trialFit');
    // The fake canvas measures 6 px a character whatever the size.
    const CHAR = 6;
    const KDE = 'Target path: cue 1.54 s after onset, pursuit over by +5.0 s';

    it('stays as it was, one line where the task put it, when it fits', () => {
      const fit = trialFit(40, 30, 800, 1000);
      const label = fitTrialLabel('strip slides', 400, 200, fit);
      assert.deepEqual(plain(label.lines), ['strip slides']);
      assert.deepEqual([label.x, label.y, label.fontSize, label.shortened], [400, 200, 12, false]);
    });

    it('wraps onto two whole lines on a phone-sized picture, each inside the picture', () => {
      const fit = trialFit(40, 22.5, 300, 1000);
      const label = fitTrialLabel(KDE, 150, 140, fit);
      assert.equal(label.fontSize, 10);
      assert.equal(label.lines.length, 2);
      assert.equal(label.lines.join(' '), KDE, 'nothing lost');
      assert.equal(label.shortened, false);
      // Split where the longer line is shortest, not a long line and a stray word.
      const [a, b] = label.lines.map((line) => line.length);
      assert.ok(Math.abs(a - b) <= 6, label.lines.join(' | '));
      const room = 300 - 2 * 6;
      label.lines.forEach((line) => assert.ok(line.length * CHAR <= room, line));
      // Two lines a line height apart, centred on the label's point.
      assert.equal(label.lineHeight, 12.5);
      assert.equal(label.y, 140 - label.lineHeight / 2);
    });

    it('ends in an ellipsis past two lines, and keeps the whole text for the hover', () => {
      const fit = trialFit(40, 22.5, 300, 1000);
      const long = KDE + ', then a second sentence that no phone has room for at all';
      const label = fitTrialLabel(long, 150, 100, fit);
      assert.equal(label.lines.length, 2);
      assert.ok(label.lines[1].endsWith('…'), label.lines[1]);
      assert.equal(label.shortened, true);
      label.lines.forEach((line) => assert.ok(line.length * CHAR <= 300 - 12, line));
    });

    it('moves in from an edge so its widest line is not cut by the border', () => {
      const fit = trialFit(40, 30, 800, 1000);
      const label = fitTrialLabel('a label at the edge', 790, 595, fit);
      const half = ('a label at the edge'.length * CHAR) / 2;
      assert.equal(label.x, 800 - 6 - half);
      assert.equal(label.y, 600 - 6 - label.lineHeight / 2);
    });

    it('sizes for the picture as shown when the page shows it smaller than drawn', () => {
      const fit = { ...trialFit(40, 22.5, 600, 1000), unit: 2 };
      // Drawn 600 wide, shown 300 wide: 10 px on screen is 20 drawing units.
      const label = fitTrialLabel(KDE, 300, 200, fit);
      assert.equal(label.fontSize, 20);
      assert.equal(label.lines.length, 2);
      label.lines.forEach((line) => assert.ok(line.length * CHAR <= 300 - 12, line));
    });
  });

  it('draws a wrapped label as lines, and a shortened one with its whole text on hover', () => {
    const KDE = 'Target path: cue 1.54 s after onset, pursuit over by +5.0 s';
    const phone = (text) => {
      const live_monitor = loadLiveMonitor();
      live_monitor.byId('trial-stage').clientWidth = 300;
      const tv = live_monitor.get('trialView');
      tv.view = { ...VIEW, screen: { width_dva: 40, height_dva: 22.5 },
        shapes: [{ kind: 'label', role: 'annotation', name: 'path', at: [0, -5], text: text }] };
      live_monitor.get('drawTrialStage')();
      return live_monitor.byId('trial-stage').querySelector('text.trial-shape');
    };
    let text = phone(KDE);
    assert.equal(text.getAttribute('style'), 'font-size: 10px');
    assert.equal(text.querySelectorAll('tspan').map((t) => t.textContent).join(' '), KDE);
    assert.equal(text.querySelector('title').textContent, 'path', 'the name, as before');
    text = phone(KDE + ', and far too much more for two lines on a phone screen');
    assert.ok(text.querySelectorAll('tspan')[1].textContent.endsWith('…'));
    assert.equal(text.querySelector('title').textContent,
      'path: ' + KDE + ', and far too much more for two lines on a phone screen');
  });

  describe('the scale bar keeps clear of the words on the picture', () => {
    const KDE = 'Path: 1.77–6.93 s after onset (latest end)';
    const stage = (view, width) => {
      const live_monitor = loadLiveMonitor();
      live_monitor.byId('trial-stage').clientWidth = width;
      live_monitor.get('trialView').view = view;
      live_monitor.get('drawTrialStage')();
      const svg = live_monitor.byId('trial-stage').querySelector('svg');
      const line = svg.querySelector('line.trial-scale');
      const text = svg.querySelector('text.trial-scale-text');
      const label = svg.querySelector('text.trial-shape');
      return { live_monitor, line, text, label, fit: live_monitor.get('trialView').fit };
    };
    const hits = (a, b) => a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3];
    const barBox = (line, fit) => {
      const x0 = Number(line.getAttribute('x1'));
      const y0 = Number(line.getAttribute('y1'));
      return [x0, y0 - 6 - 11, x0 + Math.max(5 * fit.scale, 2 * 6), y0 + 2];
    };

    it('stays at the bottom left, exactly where it was, when no label is there', () => {
      const { line, text, fit } = stage(VIEW, 800);
      assert.equal(fit.scaleBar.corner, 'bottom-left');
      assert.deepEqual([line.getAttribute('x1'), line.getAttribute('y1')].map(Number), [12, fit.height - 12]);
      assert.equal(Number(line.getAttribute('x2')), 12 + 5 * fit.scale);
      assert.deepEqual([text.getAttribute('x'), text.getAttribute('y')].map(Number), [12, fit.height - 18]);
      assert.equal(text.textContent, '5°');
    });

    it('moves to another corner when a label sits on it, and the label keeps its place', () => {
      // A phone-sized picture with the label low and to the left, over the bar's corner.
      const view = { ...VIEW, screen: { width_dva: 40, height_dva: 22.5 },
        shapes: [{ kind: 'label', role: 'annotation', name: 'path', at: [-14, -10.5], text: KDE }] };
      const { live_monitor, line, label, fit } = stage(view, 300);
      assert.notEqual(fit.scaleBar.corner, 'bottom-left');
      const fitTrialLabel = live_monitor.get('fitTrialLabel');
      const [x, y] = live_monitor.get('trialMap')(fit, -14, -10.5);
      const laid = fitTrialLabel(KDE, x, y, fit);
      assert.equal(Number(label.getAttribute('x')), laid.x, 'the label is not moved for the bar');
      const box = live_monitor.get('trialLabelBox')(laid);
      assert.equal(hits(barBox(line, fit), box), false, JSON.stringify([barBox(line, fit), box]));
    });

    it('prefers a free corner clear of the content, else the first free one, else stays put', () => {
      const live_monitor = loadLiveMonitor();
      const place = live_monitor.get('trialScalePlace');
      const fit = live_monitor.get('trialFit')(40, 30, 800, 1000);
      const bottomLeft = [0, 560, 200, 600];
      const bottomRight = [600, 560, 800, 600];
      const topLeft = [0, 0, 200, 40];
      assert.equal(place(fit, [], []).corner, 'bottom-left');
      assert.equal(place(fit, [bottomLeft], []).corner, 'bottom-right');
      // A stimulus at the bottom right: the top left is free of both.
      assert.equal(place(fit, [bottomLeft], [bottomRight]).corner, 'top-left');
      // Content everywhere: the first corner clear of labels.
      assert.equal(place(fit, [bottomLeft], [[0, 0, 800, 600]]).corner, 'bottom-right');
      // Labels in every corner: the bar stays where it always was.
      assert.equal(place(fit, [bottomLeft, bottomRight, topLeft, [600, 0, 800, 40]], []).corner, 'bottom-left');
      // Content alone never moves it.
      assert.equal(place(fit, [], [bottomLeft]).corner, 'bottom-left');
    });
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
        // The stream switches (Live streams): both on, as a session starts by default.
        if (url.startsWith('/api/streams')) return response({ json: { gaze_traces: true, camera: true } });
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


/* 2026-10-17: the Trial view shows a running eye-tracker procedure (its
 * targets, the one on screen, the camera live, gaze where a model exists) and
 * says plainly when the pause menu is up. */
const PROCEDURE_VIEW = {
  trial_index: 3,
  attempt: 0,
  t: 50,
  screen: { width_dva: 40, height_dva: 30 },
  source: 'procedure',
  shapes: [
    { kind: 'circle', role: 'stimulus', name: 'Target on screen', center: [0, 0], radius: 0.35 },
    { kind: 'circle', role: 'annotation', name: 'Target', center: [10, 0], radius: 0.35 },
    { kind: 'circle', role: 'annotation', name: 'Target', center: [-10, 0], radius: 0.35 },
  ],
  undrawn: [],
  condition: {},
  procedure: { id: 49, kind: 'validation', title: 'Validation', step: 1, n: 3, line: 'validating: target 1 of 3',
    ended: false, targets_known: true, reads_page: true },
  gaze: true,
  camera: { live: true, has_frames: true, reason: 'Live while the procedure runs.' },
};

describe('a procedure in the Trial view', () => {
  function drawnProcedure(view, status = 'calibrating') {
    const live_monitor = loadLiveMonitor();
    live_monitor.get(`state = { status: '${status}', message: 'Calibrating.' }`);
    live_monitor.get('trialView').view = view;
    live_monitor.get('drawTrialStage')();
    return live_monitor;
  }

  it('names the procedure and the target on screen, and keys the targets', () => {
    const live_monitor = drawnProcedure(PROCEDURE_VIEW);
    assert.equal(live_monitor.byId('trial-title').textContent, 'Validation');
    assert.equal(live_monitor.byId('trial-meta').textContent, 'Target 1 of 3');
    const legend = live_monitor.byId('trial-legend').children.map((item) => item.textContent);
    assert.deepEqual(legend, ['Gaze', 'Trail', 'Target on screen', 'Other targets']);
    assert.match(live_monitor.byId('trial-note').textContent, /filled one is on the subject’s screen/);
  });

  it('says a calibration shows no gaze, and a walk run elsewhere has its keys at the rig', () => {
    const calibration = { ...PROCEDURE_VIEW, gaze: false, shapes: [],
      procedure: { ...PROCEDURE_VIEW.procedure, kind: 'calibration', title: 'Calibration', step: null,
        line: 'calibrating: on the Host PC', targets_known: false, reads_page: false } };
    const live_monitor = drawnProcedure(calibration);
    assert.equal(live_monitor.byId('trial-meta').textContent, 'Starting');
    const note = live_monitor.byId('trial-note').textContent;
    assert.match(note, /lays out its calibration targets itself/);
    assert.match(note, /keys are at the rig/);
    assert.equal(live_monitor.byId('trial-status').textContent,
      'No gaze during a calibration: the gaze model is what it fits.');
  });

  it('says the guide waits for Resume', () => {
    const guide = { ...PROCEDURE_VIEW, procedure: { ...PROCEDURE_VIEW.procedure, step: null,
      line: 'calibration guide: eyes: both · press SPACE to start' } };
    assert.equal(drawnProcedure(guide).byId('trial-meta').textContent, 'Waiting to start: Resume starts the walk');
  });

  it('shows the gaze live while it measures, not "paused"', () => {
    const live_monitor = drawnProcedure(PROCEDURE_VIEW);
    const tv = live_monitor.get('trialView');
    tv.gazeTrial = 3;
    tv.samples = [[1.0, 0.2, 0.1], [1.1, 0.3, -0.1]];
    live_monitor.get('paintGaze')();
    assert.equal(live_monitor.byId('trial-status').textContent, 'Gaze 0.3°, −0.1°');
  });

  it('starts a fresh trail when a procedure begins, and keeps it through its targets', () => {
    const live_monitor = loadLiveMonitor();
    const tv = live_monitor.get('trialView');
    const take = live_monitor.get('takeProcedureView');
    tv.gazeTrial = 3;
    tv.samples = [[0.5, 1, 1]];
    take({ ...VIEW, trial_index: 3 }, PROCEDURE_VIEW);
    assert.equal(tv.samples.length, 0);
    tv.samples = [[1.0, 0, 0]];
    take(PROCEDURE_VIEW, { ...PROCEDURE_VIEW, procedure: { ...PROCEDURE_VIEW.procedure, step: 2 } });
    assert.equal(tv.samples.length, 1, 'the next target keeps the trail');
  });

  it('says live during the procedure for the camera', () => {
    const live_monitor = loadLiveMonitor();
    live_monitor.get("state = { status: 'calibrating' }");
    live_monitor.get('cameraArrivals').push(1);
    assert.equal(live_monitor.get('cameraAgeText')({ reason: '' }, { t: 1 }, { t: 2 }, true),
      'Live during the procedure.');
  });
});

describe('the Trial view banner', () => {
  it('says the pause menu is up, a screen is waiting, or the session is quitting', () => {
    const trialBanner = loadLiveMonitor().get('trialBanner');
    assert.equal(trialBanner({ status: 'paused', message: 'Paused — browser controls are enabled.' }),
      'Paused: the pause menu is on the subject’s screen. Browser controls are enabled.');
    assert.equal(trialBanner({ status: 'waiting', message: 'Resume starts.' }),
      'Waiting before the first trial. Resume starts.');
    assert.equal(trialBanner({ status: 'running', message: 'x' }), '');
    assert.equal(trialBanner(null), '');
  });

  it('is shown over the picture only while it has something to say', () => {
    const live_monitor = loadLiveMonitor();
    live_monitor.get("state = { status: 'paused', message: '' }");
    live_monitor.get('paintTrialBanner')();
    const banner = live_monitor.byId('trial-banner');
    assert.equal(banner.hasAttribute('hidden'), false);
    assert.equal(banner.dataset.status, 'paused');
    live_monitor.get("state = { status: 'running' }");
    live_monitor.get('paintTrialBanner')();
    assert.equal(banner.hasAttribute('hidden'), true);
  });
});
