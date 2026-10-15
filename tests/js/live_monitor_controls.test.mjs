/* The operator controls: one function draws every command button into every
 * [data-controls-host] from the session's `controls` block, sends a click
 * once with a request id, and follows it to the session's acknowledgement.
 *
 * Run: node --test "tests/js/*.test.mjs"
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { SAVED_STATE, loadLiveMonitor, settle } from './load_live_monitor.mjs';

/* The controls block a running session publishes (session/operator_commands.py). */
const RUNNING_CONTROLS = {
  shown: ['pause', 'resume', 'calibrate', 'validate', 'drift_correct', 'manual_reward',
    'skip_trial', 'quit'],
  available: ['pause', 'calibrate', 'manual_reward', 'skip_trial', 'quit'],
  refused: {
    resume: 'Resume is available only while the session is paused',
    validate: 'Validate is available only while the session is paused',
    drift_correct: 'Drift correct is available only while the session is paused',
  },
  keys: { pause: 'P', resume: 'SPACE', calibrate: 'C', validate: 'V', drift_correct: 'D',
    manual_reward: 'R', skip_trial: 'ESC', quit: 'CTRL-C' },
  labels: { pause: 'Pause', resume: 'Resume', calibrate: 'Calibrate', validate: 'Validate',
    drift_correct: 'Drift correct', manual_reward: 'Reward', skip_trial: 'Skip trial',
    quit: 'Quit' },
};

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
    assert.equal(button(sidebar, 'skip_trial').querySelector('kbd').textContent, 'ESC');
    assert.equal(button(sidebar, 'pause').getAttribute('aria-disabled'), 'false');
    assert.equal(button(sidebar, 'resume').getAttribute('aria-disabled'), 'true');
    assert.equal(button(sidebar, 'resume').title, RUNNING_CONTROLS.refused.resume);
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
      /^Validate is available only while the session is paused\.$/);
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
