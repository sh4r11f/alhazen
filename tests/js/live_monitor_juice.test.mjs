/* The juice panel (live_monitor/juice.py's "juice" form): stacked bars per
 * paid trial by what they were paid for, the cumulative total on its own axis,
 * the unit in both axis titles, and a legend that tells on_fault apart.
 *
 * Run: node --test "tests/js/*.test.mjs"
 */

import assert from 'node:assert/strict';
import { beforeEach, describe, it } from 'node:test';

import { hostFor, loadLiveMonitor } from './load_live_monitor.mjs';

let live_monitor;
beforeEach(() => {
  live_monitor = loadLiveMonitor();
});

const DATA = {
  form: 'juice', unit: 'µL', x_label: 'trial', y_label: 'Per trial (µL)',
  y2_label: 'Cumulative (µL)',
  kinds: [
    { key: 'outcome', name: "paid for the trial's outcome" },
    { key: 'fault', name: 'paid for a trial a device cut short (on_fault)' },
    { key: 'manual', name: 'manual reward (r)' },
    { key: 'mid_trial', name: 'mid-trial drop' },
  ],
  trials: [
    { x: 1, outcome: 25, fault: 0, manual: 0, mid_trial: 0 },
    { x: 3, outcome: 0, fault: 12.5, manual: 0, mid_trial: 0 },
    { x: 4, outcome: 25, fault: 0, manual: 25, mid_trial: 0 },
  ],
  cumulative: [[0, 0], [1, 25], [3, 37.5], [4, 87.5], [6, 87.5]],
  failures: [5],
  total: 87.5,
  stats: [{ label: 'total', value: '87.5 µL' }],
};

describe('drawJuice', () => {
  it('stacks each trial by what it paid for and draws the total as a step', () => {
    const host = hostFor(live_monitor, 520);
    const legendHost = hostFor(live_monitor);
    live_monitor.get('drawJuice')(legendHost, host, DATA);
    const svg = host.querySelector('svg');
    const bars = svg.querySelectorAll('rect').filter((r) => r.getAttribute('data-kind'));
    assert.deepEqual(bars.map((b) => b.getAttribute('data-kind')), ['outcome', 'fault', 'outcome', 'manual']);
    // on_fault is its own colour, never the outcome's; manual is an outline.
    const style = (kind) => bars.find((b) => b.getAttribute('data-kind') === kind).getAttribute('style');
    assert.notEqual(style('fault'), style('outcome'));
    assert.match(style('manual'), /stroke:/);
    const total = svg.querySelectorAll('path').find((p) => p.classList.contains('juice-total'));
    assert.ok(total && /H/.test(total.getAttribute('d')), 'the cumulative total is a step line');
    const titles = svg.querySelectorAll('text').map((t) => t.textContent);
    assert.ok(titles.includes('Per trial (µL)') && titles.includes('Cumulative (µL)'));
    const legend = legendHost.querySelector('div.legend');
    const names = legend.children.filter((i) => !i.classList.contains('legend-title'))
      .map((i) => i.children[1].textContent);
    assert.deepEqual(names, [DATA.kinds[0].name, DATA.kinds[1].name, DATA.kinds[2].name, 'cumulative']);
  });

  it('reads as a table with the running total', () => {
    const table = live_monitor.get('tableRows')(DATA);
    assert.equal(table.head[table.head.length - 1], 'Cumulative (µL)');
    assert.deepEqual(Array.from(table.rows[2]).slice(-1), ['87.5']);
  });

  it('says so when nothing was delivered', () => {
    const host = hostFor(live_monitor, 400);
    live_monitor.get('drawJuice')(hostFor(live_monitor), host, { ...DATA, trials: [], failures: [] });
    assert.match(host.textContent, /No juice delivered yet/);
  });
});
