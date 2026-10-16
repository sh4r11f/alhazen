/* The live monitor's procedure guide: the panel that says what the operator
 * does during a calibration, validation or drift correction, drawn from the
 * session's state.procedure (session/procedure_guide.py). It appears when the
 * session publishes a procedure, follows it, says what failed and what to
 * check, and goes when the session stops publishing one.
 *
 * The dashboard's Live monitor tab frames this same page (an iframe on the
 * session's own server), so there is no second code path to test: the
 * framing is checked here only as far as the panel being first in <main>,
 * which is what keeps it on screen in the dashboard's column.
 *
 * Run: node --test "tests/js/*.test.mjs"
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';

import { SAVED_STATE, loadLiveMonitor, settle } from './load_live_monitor.mjs';

function response(json) {
  return {
    ok: true, status: 200, json: async () => json, text: async () => '',
    headers: new Map(), arrayBuffer: async () => new ArrayBuffer(0),
  };
}
const pending = () => new Promise(() => {});

/* What the session publishes, in the shape procedure_guide.py writes. */
const GUIDE = {
  kind: 'calibration', heading: 'Calibration', stage: 'calibration guide',
  instruction: 'Press SPACE to start, P or ESC to go back to the pause menu',
  status_line: 'eyes: both tracked', status_alert: false, step: null, n: null,
  outcome: 'running', failure: null, check: null, t: 12.5,
};
const TARGET = {
  ...GUIDE, stage: 'calibrating', step: 2, n: 5,
  instruction: 'Press SPACE when the subject is looking at the target',
};
const FAILED = {
  ...GUIDE, stage: 'calibration failed', instruction: '', step: null, n: null, status_line: null,
  outcome: 'failed', failure: 'The device reports NO calibration after the walk',
  check: 'Check the camera sees the eyes (position, focus, LED), then calibrate again with C',
};

const state = (revision, status, procedure, message) => ({
  ...SAVED_STATE, revision, status, procedure, message: message || '',
});

/** The live page fed `states` one per poll of /api/state (the last repeats). */
async function livePage(states) {
  const queue = [...states];
  const page = loadLiveMonitor({
    staticState: null,
    search: '?token=abc',
    fetch: (url) => {
      if (url.startsWith('/api/state')) return response(queue.length > 1 ? queue.shift() : queue[0]);
      return pending();
    },
  });
  await settle();
  return page;
}

/** The next state: run the poll timer and let its fetch finish. */
async function next(page) {
  page.runTimers();
  await settle();
}

const shown = (element) => !element.hasAttribute('hidden');
const keys = (element) => element.querySelectorAll('kbd').map((key) => key.textContent);

describe('the procedure guide', () => {
  it('appears when a procedure starts, with its heading, instruction and eye line', async () => {
    const page = await livePage([state(1, 'calibrating', GUIDE, 'calibration guide: …')]);
    assert.ok(shown(page.byId('procedure')));
    assert.equal(page.byId('procedure-heading').textContent, 'Calibration');
    assert.equal(page.byId('procedure-instruction').textContent,
      'Press Space to start, P or Esc to go back to the pause menu');
    assert.deepEqual(keys(page.byId('procedure-instruction')), ['Space', 'P', 'Esc']);
    assert.equal(page.byId('procedure-status').textContent, 'eyes: both tracked');
    assert.equal(page.byId('procedure-status').dataset.alert, 'false');
    /* No target is up at the guide: no step and no marks. */
    assert.equal(page.byId('procedure-step').textContent, '');
    assert.ok(!shown(page.byId('procedure-track')));
    assert.ok(!shown(page.byId('procedure-failure')));
    /* The line under the buttons does not repeat it small. */
    assert.equal(page.byId('notice').textContent, '');
  });

  it('follows the targets, one mark each, and then goes when the procedure ends', async () => {
    const page = await livePage([
      state(1, 'calibrating', GUIDE),
      state(2, 'calibrating', TARGET),
      state(3, 'paused', null, 'calibration OK: HV5 (5 targets)'),
    ]);
    await next(page);
    assert.equal(page.byId('procedure-step').textContent, 'Target 2 of 5');
    const marks = page.byId('procedure-track').querySelectorAll('li');
    assert.deepEqual(marks.map((mark) => mark.dataset.state), ['done', 'current', 'todo', 'todo', 'todo']);
    assert.equal(page.byId('procedure-instruction').textContent,
      'Press Space when the subject is looking at the target');
    await next(page);
    assert.ok(!shown(page.byId('procedure')));
    assert.equal(page.byId('notice').textContent, 'calibration OK: HV5 (5 targets)');
  });

  it('says what failed and what to check, and keeps saying it while the session holds it', async () => {
    const page = await livePage([
      state(1, 'calibrating', FAILED),
      state(2, 'paused', FAILED, 'NOT calibrated: …'),
    ]);
    const panel = page.byId('procedure');
    assert.ok(shown(panel));
    assert.equal(panel.dataset.outcome, 'failed');
    assert.equal(page.byId('procedure-heading').textContent, 'Calibration failed');
    assert.ok(shown(page.byId('procedure-failure')));
    assert.equal(page.byId('procedure-failure-text').textContent,
      'The device reports NO calibration after the walk');
    assert.equal(page.byId('procedure-check').textContent,
      'Check the camera sees the eyes (position, focus, LED), then calibrate again with C');
    assert.deepEqual(keys(page.byId('procedure-check')), ['C']);
    /* "NO" is a word, not a key; the failure text has no keys drawn at all. */
    assert.deepEqual(keys(page.byId('procedure-failure-text')), []);
    assert.ok(!shown(page.byId('procedure-instruction')));
    assert.ok(!shown(page.byId('procedure-status')));
    await next(page);
    assert.ok(shown(panel), 'the failure stays up while paused');
    assert.equal(page.byId('notice').textContent, '');
  });

  it('calls a stopped procedure stopped', async () => {
    const page = await livePage([state(1, 'paused', {
      ...FAILED, kind: 'validation', heading: 'Validation', outcome: 'aborted',
      failure: 'Validation aborted after 2 target(s)', check: 'Validate again with V when ready',
      step: 2, n: 5,
    })]);
    assert.equal(page.byId('procedure-heading').textContent, 'Validation stopped');
    /* Where it stopped is said; the marks are for a walk still running. */
    assert.equal(page.byId('procedure-step').textContent, 'Target 2 of 5');
    assert.ok(!shown(page.byId('procedure-track')));
    assert.equal(page.byId('procedure').dataset.outcome, 'aborted');
    assert.deepEqual(keys(page.byId('procedure-check')), ['V']);
  });

  it('marks the tracker\'s no-eye sentence as an alert', async () => {
    const page = await livePage([state(1, 'calibrating', {
      ...TARGET, status_line: 'NO EYE IN THE CAMERA IMAGE — check position, focus and LED', status_alert: true,
    })]);
    assert.equal(page.byId('procedure-status').dataset.alert, 'true');
  });

  it('draws no marks for a walk too long for a row, but still says the step', async () => {
    const page = await livePage([state(1, 'calibrating', { ...TARGET, step: 7, n: 60 })]);
    assert.equal(page.byId('procedure-step').textContent, 'Target 7 of 60');
    assert.ok(!shown(page.byId('procedure-track')));
  });

  it('never shows in the saved copy, whatever its state carries', () => {
    const page = loadLiveMonitor({ staticState: { ...SAVED_STATE, procedure: FAILED } });
    assert.ok(!shown(page.byId('procedure')));
  });

  it('comes first in the page, above the trial view and the panels', () => {
    const html = readFileSync(new URL('../../src/alhazen/live_monitor/assets/index.html', import.meta.url), 'utf8');
    const main = html.slice(html.indexOf('<main>'));
    const first = /<(section|div)\b[^>]*\bid="([^"]+)"/.exec(main);
    assert.equal(first[2], 'procedure');
  });
});
