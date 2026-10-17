/* The operator controls: one function draws every command button into every
 * [data-controls-host] from the session's `controls` block, sends a click
 * once with a request id, and follows it to the session's acknowledgement.
 *
 * Run: node --test "tests/js/*.test.mjs"
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';

import { SAVED_STATE, loadLiveMonitor, settle } from './load_live_monitor.mjs';

/* The controls blocks a session publishes, read from the fixture the Python
 * suite checks against session/operator_commands.py, so the buttons are tested
 * on the session's own table. */
const CONTROLS = JSON.parse(readFileSync(
  new URL('../fixtures/live_monitor_controls.json', import.meta.url), 'utf8'));
const RUNNING_CONTROLS = CONTROLS.running;
const PAUSED_CONTROLS = CONTROLS.paused;

const running = (extra) => ({
  ...SAVED_STATE, revision: 1, status: 'running', controls: RUNNING_CONTROLS, commands: [],
  ...extra,
});

function response({ status = 200, json, text = '' }) {
  return {
    ok: status >= 200 && status < 300,
    status: status,
    json: async () => json,
    text: async () => text,
    headers: new Map(),
    arrayBuffer: async () => new ArrayBuffer(0),
  };
}
const pending = () => new Promise(() => {});

/** The live page on one state, with /api/command and /api/commands answered
 *  by the given functions (each call recorded in `fetches`). */
async function livePage({ state, command, acknowledgements }) {
  const acks = [...(acknowledgements || [])];
  const live_monitor = loadLiveMonitor({
    staticState: null,
    search: '?token=abc',
    fetch: (url, init) => {
      if (url.startsWith('/api/state')) return response({ json: state });
      if (url.startsWith('/api/camera')) return pending();
      // The stream switches (Live streams): both on, as a session starts by default.
      if (url.startsWith('/api/streams')) return response({ json: { gaze_traces: true, camera: true } });
      if (url.startsWith('/api/command?') || url === '/api/command') return command(init);
      if (url.startsWith('/api/commands')) return acks.length ? acks.shift() : pending();
      throw new Error('unexpected fetch ' + url);
    },
  });
  await settle();
  return live_monitor;
}

const buttons = (root) => root.querySelectorAll('button[data-command]');
const button = (root, name) => root.querySelector('button[data-command="' + name + '"]');
const posts = (live_monitor) => live_monitor.fetches.filter((call) => call.url === '/api/command');

describe('the operator controls', () => {
  it('draw the session\'s buttons, with their keys, on where the session accepts them', async () => {
    const live_monitor = await livePage({ state: running(), command: () => response({ status: 202 }) });
    const sidebar = live_monitor.byId('controls');
    assert.deepEqual(buttons(sidebar).map((b) => b.dataset.command), RUNNING_CONTROLS.shown);
    assert.equal(button(sidebar, 'pause').querySelector('kbd').textContent, 'P');
    /* Named keys are printed as on the key, in sentence case (the session
     * sends ESC, SPACE, CTRL-C); a single letter stays as it is. */
    assert.equal(button(sidebar, 'skip_trial').querySelector('kbd').textContent, 'Esc');
    assert.equal(button(sidebar, 'quit').querySelector('kbd').textContent, 'Ctrl-C');
    assert.equal(button(sidebar, 'pause').getAttribute('aria-disabled'), 'false');
    assert.equal(button(sidebar, 'resume').getAttribute('aria-disabled'), 'true');
    assert.equal(button(sidebar, 'resume').title, RUNNING_CONTROLS.refused.resume);
  });

  it('keep Calibrate, Validate and Drift correct off during a trial, in every host, and on while paused', async () => {
    const procedures = ['calibrate', 'validate', 'drift_correct'];
    const others = ['pause', 'manual_reward', 'skip_trial', 'quit'];
    const live_monitor = await livePage({ state: running(), command: () => response({ status: 202 }) });
    const trialHost = live_monitor.byId('trial-controls');
    for (const host of [live_monitor.byId('controls'), trialHost]) {
      procedures.forEach((name) => {
        assert.equal(button(host, name).getAttribute('aria-disabled'), 'true', name);
        assert.equal(button(host, name).title, RUNNING_CONTROLS.refused[name]);
        assert.match(button(host, name).title, /works only while the task is paused/);
      });
      others.forEach((name) => assert.equal(button(host, name).getAttribute('aria-disabled'), 'false', name));
    }
    button(trialHost, 'calibrate').onclick();
    await settle();
    assert.equal(posts(live_monitor).length, 0, 'an off button sends nothing');
    assert.equal(trialHost.querySelector('.control-feedback').textContent,
      'Calibrate works only while the task is paused; pause it first.');

    const paused = await livePage({
      state: running({ status: 'paused', controls: PAUSED_CONTROLS }),
      command: () => response({ status: 202 }),
    });
    for (const host of [paused.byId('controls'), paused.byId('trial-controls')]) {
      procedures.forEach((name) => assert.equal(button(host, name).getAttribute('aria-disabled'), 'false', name));
    }
  });

  it('list a rig key the session refused, with why, under the buttons', async () => {
    const note = RUNNING_CONTROLS.refused.calibrate;
    const live_monitor = await livePage({
      state: running({ commands: [{ request_id: 'keyboard-1', name: 'calibrate', source: 'keyboard',
        result: 'refused', note, t: 3 }] }),
      command: () => response({ status: 202 }),
    });
    for (const host of [live_monitor.byId('controls'), live_monitor.byId('trial-controls')]) {
      const item = host.querySelector('.command-log').querySelector('li');
      assert.equal(item.dataset.result, 'refused');
      assert.equal(item.querySelector('.command-name').textContent, 'Calibrate (key at the rig)');
      assert.equal(item.querySelector('.command-note').textContent, 'refused · ' + note);
    }
  });

  it('fill every controls host on the page, not only the sidebar', async () => {
    const live_monitor = await livePage({ state: running(), command: () => response({ status: 202 }) });
    const host = live_monitor.document.createElement('div');
    host.setAttribute('data-controls-host', '');
    live_monitor.document.body.appendChild(host);
    live_monitor.get('renderControls')();
    assert.deepEqual(buttons(host).map((b) => b.dataset.command), RUNNING_CONTROLS.shown);
  });

  it('say why a button is off when it is clicked, and send nothing', async () => {
    const live_monitor = await livePage({ state: running(), command: () => response({ status: 202 }) });
    button(live_monitor.byId('controls'), 'validate').onclick();
    await settle();
    assert.equal(posts(live_monitor).length, 0);
    assert.match(live_monitor.byId('controls').querySelector('.control-feedback').textContent,
      /^Validate works only while the task is paused; pause it first\.$/);
  });

  it('keep the focus on an off button after a click on it says why, in its own host only', async () => {
    const live_monitor = await livePage({ state: running(), command: () => response({ status: 202 }) });
    const host = live_monitor.byId('trial-controls');
    button(host, 'calibrate').focus();
    button(host, 'calibrate').onclick();
    await settle();
    const again = button(host, 'calibrate');
    assert.equal(live_monitor.document.activeElement, again, 'the redrawn Calibrate has the focus');
    assert.equal(again.getAttribute('aria-disabled'), 'true');
    assert.notEqual(live_monitor.document.activeElement, button(live_monitor.byId('controls'), 'calibrate'));
    assert.equal(posts(live_monitor).length, 0);
  });

  it('send a click once, show it pending, and follow it to its acknowledgement', async () => {
    let answerAck;
    const live_monitor = await livePage({
      state: running(),
      command: () => response({ status: 202 }),
      acknowledgements: [new Promise((resolve) => { answerAck = resolve; })],
    });
    button(live_monitor.byId('controls'), 'pause').onclick();
    await settle();
    const [post] = posts(live_monitor);
    assert.deepEqual(JSON.parse(post.init.body), { name: 'pause', request_id: 'request-1' });
    assert.equal(post.init.headers['X-Alhazen-Token'], 'abc');
    const sidebar = live_monitor.byId('controls');
    assert.equal(button(sidebar, 'pause').dataset.pending, 'true');
    assert.equal(button(sidebar, 'pause').getAttribute('aria-disabled'), 'true');
    assert.match(sidebar.querySelector('.command-log').textContent, /sent, waiting for the task/);
    assert.ok(live_monitor.fetches.some((call) => call.url ===
      '/api/commands?token=abc&after=0&wait_ms=2000'));

    answerAck(response({ json: { seq: 1, commands: [{
      request_id: 'request-1', name: 'pause', result: 'applied',
      note: 'the trial ends here and the session pauses', t: 3.2,
    }] } }));
    await settle();
    await settle();
    assert.equal(button(sidebar, 'pause').dataset.pending, undefined);
    const entry = sidebar.querySelector('.command-log').querySelector('li');
    assert.equal(entry.dataset.result, 'applied');
    assert.match(entry.textContent, /Pause.*applied · the trial ends here/);
  });

  it('take an acknowledgement from a state publish as well', async () => {
    const live_monitor = await livePage({ state: running(), command: () => response({ status: 202 }) });
    button(live_monitor.byId('controls'), 'manual_reward').onclick();
    await settle();
    live_monitor.get('state').commands = [{
      request_id: 'request-1', name: 'manual_reward', result: 'discarded',
      note: 'queued before this pause began; not acted on', t: 1,
    }];
    live_monitor.get('render')();
    const sidebar = live_monitor.byId('controls');
    assert.equal(live_monitor.get('pendingCommands').size, 0);
    assert.equal(sidebar.querySelector('.command-log').querySelector('li').dataset.result, 'discarded');
  });

  it('keep a newer acknowledgement when an older state publish is read after it', async () => {
    const live_monitor = await livePage({ state: running(), command: () => response({ status: 202 }) });
    const take = live_monitor.get('takeAcknowledgements');
    const reward = { request_id: 'b', name: 'manual_reward', result: 'applied', note: 'one reward delivered', t: 2 };
    const pause = { request_id: 'a', name: 'pause', result: 'applied', note: 'paused', t: 1 };
    take([pause, reward]);   // the acknowledgement channel, newest
    take([pause]);           // a state published just before the reward's acknowledgement
    assert.equal(live_monitor.get('acknowledgements').map((r) => r.request_id).join(','), 'a,b');
  });

  it('show the server\'s refusal and do not leave the button pending', async () => {
    const live_monitor = await livePage({
      state: running(),
      command: () => response({ status: 409, text: 'the session is already paused' }),
    });
    button(live_monitor.byId('controls'), 'pause').onclick();
    await settle();
    await settle();
    const sidebar = live_monitor.byId('controls');
    assert.equal(button(sidebar, 'pause').dataset.pending, undefined);
    assert.match(sidebar.querySelector('.control-feedback').textContent,
      /Pause: the session is already paused/);
  });

  it('ask before quitting, in the page, and send quit only on the confirmation', async () => {
    const live_monitor = await livePage({ state: running(), command: () => response({ status: 202 }) });
    const sidebar = () => live_monitor.byId('controls');
    button(sidebar(), 'quit').onclick();
    await settle();
    assert.equal(posts(live_monitor).length, 0);
    assert.match(sidebar().querySelector('.quit-confirm').textContent, /End the session\?/);
    sidebar().querySelector('[data-confirm="cancel"]').onclick();
    assert.equal(sidebar().querySelector('.quit-confirm'), null);
    assert.ok(button(sidebar(), 'quit'));

    button(sidebar(), 'quit').onclick();
    sidebar().querySelector('[data-confirm="quit"]').onclick();
    await settle();
    assert.deepEqual(posts(live_monitor).map((call) => JSON.parse(call.init.body).name), ['quit']);
  });

  it('let go of a command the session ended before taking', async () => {
    const live_monitor = await livePage({ state: running(), command: () => response({ status: 202 }) });
    button(live_monitor.byId('controls'), 'skip_trial').onclick();
    await settle();
    Object.assign(live_monitor.get('state'), { status: 'complete', controls: {
      ...RUNNING_CONTROLS, available: [], refused: {} } });
    live_monitor.get('render')();
    assert.equal(live_monitor.get('pendingCommands').size, 0);
    assert.match(live_monitor.byId('controls').querySelector('.control-feedback').textContent,
      /ended before it took: Skip trial/);
  });
});
