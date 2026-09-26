/* The fake page itself (fake_dom.mjs, load_live_monitor.mjs). Every other test
 * here trusts it, so the parts a wrong answer would hide in — selector
 * matching, attribute reflection, text — and its refusals are pinned first.
 *
 * Run: node --test "tests/js/*.test.mjs"
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { FakeDocument, SVG_NS, compileSelector } from './fake_dom.mjs';
import { loadLiveMonitor } from './load_live_monitor.mjs';

describe('the fake DOM', () => {
  function page() {
    const document = new FakeDocument();
    const card = document.body.appendChild(document.createElement('details'));
    card.className = 'table wide';
    const input = card.appendChild(document.createElement('input'));
    input.dataset.setting = 'iris';
    const svg = card.appendChild(document.createElementNS(SVG_NS, 'svg'));
    const text = svg.appendChild(document.createElementNS(SVG_NS, 'text'));
    text.setAttribute('class', 'tick-text');
    return { document, card, input, svg, text };
  }

  it('matches a tag, classes, and attributes with or without a value', () => {
    const { document, card, input, text } = page();
    assert.deepEqual(document.querySelectorAll('details.table.wide'), [card]);
    assert.deepEqual(document.querySelectorAll('input[data-setting="iris"]'), [input]);
    assert.deepEqual(document.querySelectorAll("[data-setting='iris']"), [input]);
    assert.deepEqual(document.querySelectorAll('[data-setting]'), [input]);
    assert.deepEqual(document.querySelectorAll('text.tick-text'), [text]);
    assert.deepEqual(document.querySelectorAll('input[data-setting="other"]'), []);
  });

  it('refuses a selector it cannot match instead of matching nothing', () => {
    for (const selector of ['details input', 'input:focus', 'a, b', 'div > span', '']) {
      assert.throws(() => compileSelector(selector), /the fake DOM cannot match the selector/, selector);
    }
  });

  it('reflects dataset into data-* attributes, and the open flag into [open]', () => {
    const { document, card, input } = page();
    assert.equal(input.getAttribute('data-setting'), 'iris');
    input.dataset.frameRate = '30';
    assert.equal(input.getAttribute('data-frame-rate'), '30');
    assert.equal(input.dataset.missing, undefined);
    assert.deepEqual(document.querySelectorAll('details.table[open]'), []);
    card.open = true;
    assert.deepEqual(document.querySelectorAll('details.table[open]'), [card]);
  });

  it('reads text in document order, and replaces children when text is set', () => {
    const { document, svg, text } = page();
    text.textContent = 'n';
    const more = svg.appendChild(document.createElementNS(SVG_NS, 'tspan'));
    more.textContent = ' = 12';
    assert.equal(svg.textContent, 'n = 12');
    svg.textContent = 'replaced';
    assert.equal(svg.children.length, 0);
    assert.equal(svg.textContent, 'replaced');
  });

  it('keeps SVG tag names as written and puts HTML ones in capitals, as a browser does', () => {
    const { input, text } = page();
    assert.equal(text.tagName, 'text');
    assert.equal(input.tagName, 'INPUT');
  });

  it('gives only a canvas, and only a 2d context', () => {
    const document = new FakeDocument();
    assert.equal(document.createElement('canvas').getContext('2d').measureText('abc').width, 18);
    assert.throws(() => document.createElement('canvas').getContext('webgl'), /only gives a <canvas> a "2d"/);
    assert.throws(() => document.createElement('div').getContext('2d'), /only gives a <canvas>/);
  });
});

describe('loadLiveMonitor', () => {
  it('builds every element index.html gives an id or a command', () => {
    const live_monitor = loadLiveMonitor();
    for (const id of ['identity', 'counts', 'status', 'sections', 'notice', 'theme', 'grid']) {
      assert.ok(live_monitor.byId(id), 'no #' + id);
    }
    const commands = live_monitor.document.querySelectorAll('[data-command]')
      .map((button) => button.dataset.command);
    assert.ok(commands.includes('resume') && commands.includes('quit'), String(commands));
  });

  it('refuses to load the live page without a server to answer it', () => {
    assert.throws(() => loadLiveMonitor({ staticState: null }), /pass options.fetch/);
  });

  it('fails a fetch no stub answers, naming the URL', () => {
    const live_monitor = loadLiveMonitor();
    assert.throws(() => live_monitor.window.fetch('/api/anything'), /fetched \/api\/anything/);
    assert.deepEqual(live_monitor.fetches.map((call) => call.url), ['/api/anything']);
  });

  it('runs timers and frames only when the test says so', () => {
    const live_monitor = loadLiveMonitor();
    let ran = 0;
    live_monitor.window.setTimeout(() => { ran += 1; }, 10);
    live_monitor.window.requestAnimationFrame(() => { ran += 10; });
    assert.equal(ran, 0);
    assert.equal(live_monitor.runTimers(), 1);
    assert.equal(live_monitor.runFrames(), 1);
    assert.equal(ran, 11);
  });
});
