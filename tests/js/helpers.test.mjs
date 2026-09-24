/* The renderer's pure helpers: scales, tick labels, number formats, units,
 * labels and colours. Each test states a behaviour a comment in dashboard.js
 * promises, and checks it by calling the real function — so a rename passes
 * and a wrong answer fails, the reverse of a test that searches the source.
 *
 * Run: node --test "tests/js/*.test.mjs"
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { loadDashboard, plain } from './load_dashboard.mjs';

/* One saved page for the whole file: nothing here touches the page, and
 * none of these functions keeps state between calls. */
const dashboard = loadDashboard();
const fn = (name) => {
  const found = dashboard.get(name);
  assert.equal(typeof found, 'function', name + ' is not a function in dashboard.js');
  return found;
};

const MINUS = '−';

/* Is `step` 1, 2, 2.5 or 5 times a power of ten — a step a reader can count
 * in? (10 times a power of ten is 1 times the next one.) */
function isNiceStep(step) {
  const magnitude = Math.pow(10, Math.floor(Math.log10(step)));
  const mantissa = Number((step / magnitude).toFixed(6));
  return [1, 2, 2.5, 5].includes(mantissa);
}

describe('niceTicks', () => {
  const niceTicks = (...args) => plain(fn('niceTicks')(...args));

  it('steps in 1, 2, 2.5 or 5 times a power of ten', () => {
    assert.deepEqual(niceTicks(0, 1, 4), [0, 0.25, 0.5, 0.75, 1]);
    assert.deepEqual(niceTicks(0, 100, 5), [0, 20, 40, 60, 80, 100]);
    assert.deepEqual(niceTicks(-10, 10, 4), [-10, -5, 0, 5, 10]);
  });

  it('reaches both ends of the range when they fall on a step', () => {
    // The upper tick is the one a loop bound gets wrong: an axis from 0 to
    // 100 whose last label is 80 reads as if the data stopped there.
    for (const [lo, hi, target] of [[0, 1, 4], [0, 100, 5], [-10, 10, 4], [0, 16, 8]]) {
      const ticks = niceTicks(lo, hi, target);
      assert.equal(ticks[0], lo, `first tick of [${lo}, ${hi}]`);
      assert.equal(ticks[ticks.length - 1], hi, `last tick of [${lo}, ${hi}]`);
    }
  });

  it('covers any range with evenly spaced nice ticks inside it, within a step of each end', () => {
    const cases = [
      [0, 1, 4], [0, 100, 5], [-3.7, 12.2, 5], [0.001, 0.0093, 4], [250, 1250, 4],
      [-1, -0.2, 4], [0, 7.3, 4], [1e-4, 5e-4, 3], [3, 3.1, 4], [-500, 500, 7], [12, 13, 3],
    ];
    for (const [lo, hi, target] of cases) {
      const ticks = niceTicks(lo, hi, target);
      const label = `niceTicks(${lo}, ${hi}, ${target}) = ${JSON.stringify(ticks)}`;
      assert.ok(ticks.length >= 2, label);
      const step = ticks[1] - ticks[0];
      assert.ok(isNiceStep(step), label + ': step ' + step + ' is not nice');
      ticks.forEach((tick, i) => {
        if (i > 0) assert.ok(Math.abs(tick - ticks[i - 1] - step) < step * 1e-6, label + ': uneven');
        assert.ok(tick >= lo - step * 1e-9 && tick <= hi + step * 1e-9, label + ': outside');
        assert.ok(Math.abs(tick / step - Math.round(tick / step)) < 1e-6, label + ': off the step');
      });
      assert.ok(ticks[0] - lo < step, label + ': starts more than a step in');
      assert.ok(hi - ticks[ticks.length - 1] < step, label + ': stops more than a step short');
    }
  });

  it('carries no floating-point dust into a tick value', () => {
    // 0.1 + 0.2 is 0.30000000000000004 in binary; the axis must say 0.3.
    assert.deepEqual(niceTicks(0, 0.3, 3), [0, 0.1, 0.2, 0.3]);
  });

  it('writes zero as 0, never as -0', () => {
    const zero = niceTicks(-10, 10, 4)[2];
    assert.ok(Object.is(zero, 0), 'expected +0, got ' + zero);
  });

  it('uses whole-number steps on an integer axis: there is no trial 7.5', () => {
    const ticks = niceTicks(1, 4, 8, true);
    assert.deepEqual(ticks, [1, 2, 3, 4]);
    for (const [lo, hi, target] of [[0, 3, 10], [1, 79, 4], [0, 16, 4], [0, 500, 6]]) {
      niceTicks(lo, hi, target, true).forEach((tick) => assert.ok(Number.isInteger(tick), `${tick}`));
    }
  });

  it('stops at the last tick inside the range rather than past it', () => {
    // The histogram depends on this and extends the ticks itself: "niceTicks(0, 16)
    // stops at 15".
    assert.deepEqual(niceTicks(0, 16, 4, true), [0, 5, 10, 15]);
  });

  it('gives a single tick for an empty or reversed range instead of looping', () => {
    assert.deepEqual(niceTicks(3, 3, 4), [3]);
    assert.deepEqual(niceTicks(5, 2, 4), [5]);
  });
});

describe('niceDomain', () => {
  const niceDomain = (...args) => plain(fn('niceDomain')(...args));

  it('starts and ends the axis on a labelled tick outside the data', () => {
    const domain = niceDomain(0.12, 0.93, 4);
    assert.deepEqual(domain, { lo: 0, hi: 1, ticks: [0, 0.25, 0.5, 0.75, 1] });
  });

  it('always contains the data and ends on its outermost ticks', () => {
    for (const [lo, hi] of [[0.12, 0.93], [-3.7, 12.2], [250, 1210], [-0.8, -0.05], [3, 3.1]]) {
      const domain = niceDomain(lo, hi, 4);
      assert.ok(domain.lo <= lo && domain.hi >= hi, JSON.stringify(domain));
      assert.equal(domain.ticks[0], domain.lo);
      assert.equal(domain.ticks[domain.ticks.length - 1], domain.hi);
    }
  });

  it('does not push an end that already sits on a tick a step further', () => {
    assert.deepEqual(niceDomain(0, 100, 5).hi, 100);
    assert.deepEqual(niceDomain(0, 1, 4).lo, 0);
  });

  it('keeps a baseline at zero', () => {
    assert.equal(niceDomain(0, 7.3, 4).lo, 0);
    assert.equal(niceDomain(-7.3, 0, 4).hi, 0);
  });

  it('runs a trial axis in whole trials: 0 to 80, not 1 to 79', () => {
    const domain = niceDomain(1, 79, 4, true);
    assert.deepEqual([domain.lo, domain.hi], [0, 80]);
    domain.ticks.forEach((tick) => assert.ok(Number.isInteger(tick), `${tick}`));
  });

  it('opens a single value into a range around it', () => {
    const domain = niceDomain(5, 5, 4);
    assert.ok(domain.lo < 5 && domain.hi > 5, JSON.stringify(domain));
    assert.ok(domain.ticks.includes(5), JSON.stringify(domain));
  });
});

describe('decimalsFor', () => {
  const decimalsFor = fn('decimalsFor');

  it('writes a quarter step with two decimals, so 0.25 never shows as 0.3', () => {
    assert.equal(decimalsFor(0.25), 2);
    assert.equal(decimalsFor(-0.25), 2);
  });

  it('uses exactly as many decimals as the step needs', () => {
    assert.equal(decimalsFor(1), 0);
    assert.equal(decimalsFor(20), 0);
    assert.equal(decimalsFor(0.5), 1);
    assert.equal(decimalsFor(2.5), 1);
    assert.equal(decimalsFor(0.1), 1);
    assert.equal(decimalsFor(0.05), 2);
    assert.equal(decimalsFor(0.0025), 4);
  });

  it('gives 0 for a step that is zero or not a number', () => {
    assert.equal(decimalsFor(0), 0);
    assert.equal(decimalsFor(NaN), 0);
    assert.equal(decimalsFor(Infinity), 0);
  });
});

describe('tickText', () => {
  const tickText = fn('tickText');
  const labels = (ticks) => ticks.map((tick) => tickText(tick, ticks));

  it('labels every tick of an axis with the decimals its step implies', () => {
    assert.deepEqual(labels([0, 0.25, 0.5, 0.75, 1]), ['0.00', '0.25', '0.50', '0.75', '1.00']);
    assert.deepEqual(labels([0, 20, 40]), ['0', '20', '40']);
  });

  it('never prints floating-point dust', () => {
    assert.equal(tickText(0.1 + 0.2, [0, 0.1, 0.2, 0.3]), '0.3');
  });

  it('sets a negative tick with a true minus sign', () => {
    assert.equal(tickText(-5, [-10, -5, 0]), MINUS + '5');
  });

  it('groups a large tick and drops its decimals', () => {
    // Digit grouping follows the reader's locale, so only its shape is pinned.
    assert.match(tickText(20000, [0, 10000, 20000]), /^20\D?000$/);
    assert.match(tickText(-20000, [-20000, 0]), new RegExp('^' + MINUS + '20\\D?000$'));
  });

  it('labels a lone tick by its own size', () => {
    assert.equal(tickText(3, [3]), '3');
    assert.equal(tickText(0.5, [0.5]), '0.5');
  });
});

describe('fmt', () => {
  const fmt = fn('fmt');

  it('gives each magnitude the decimals it deserves', () => {
    assert.equal(fmt(123.4), '123');
    assert.equal(fmt(12.34), '12.3');
    assert.equal(fmt(1.234), '1.23');
    assert.equal(fmt(0.01234), '0.0123');
    assert.equal(fmt(0.1234), '0.123');
    assert.equal(fmt(0), '0');
  });

  it('groups thousands without decimals', () => {
    assert.match(fmt(1234.5), /^1\D?235$/);
  });

  it('sets a negative value with a true minus sign', () => {
    assert.equal(fmt(-12.34), MINUS + '12.3');
    assert.match(fmt(-1234.5), new RegExp('^' + MINUS + '1\\D?235$'));
  });

  it('shows a dash for a value that is not a finite number', () => {
    assert.equal(fmt(NaN), '—');
    assert.equal(fmt(Infinity), '—');
    assert.equal(fmt(-Infinity), '—');
  });
});

describe('minus', () => {
  const minus = fn('minus');

  it('replaces the hyphen in front of a number with U+2212', () => {
    assert.equal(minus('-3'), MINUS + '3');
    assert.equal(minus('-0.5'), MINUS + '0.5');
    assert.equal(minus(-2), MINUS + '2');
  });

  it('leaves every other hyphen alone', () => {
    assert.equal(minus('3'), '3');
    assert.equal(minus('-x'), '-x');
    assert.equal(minus('a-1'), 'a-1');
    assert.equal(minus('1-2'), '1-2');
  });
});

describe('unitOf and withUnit', () => {
  const unitOf = fn('unitOf');
  const withUnit = fn('withUnit');

  it('takes the short unit symbol an axis title ends with', () => {
    assert.equal(unitOf('Saccade latency (ms)'), 'ms');
    assert.equal(unitOf('Landing error (°)'), '°');
    assert.equal(unitOf('RT (ms)  '), 'ms');
  });

  it('does not treat a long parenthetical as a unit', () => {
    assert.equal(unitOf('Occluder landing (proportion)'), '');
  });

  it('finds no unit where the title has none at its end', () => {
    assert.equal(unitOf('Accuracy'), '');
    assert.equal(unitOf('RT (ms) by side'), '');
    assert.equal(unitOf(null), '');
    assert.equal(unitOf(undefined), '');
  });

  it('sets a unit as print does: a space before ms, none before ° or %', () => {
    assert.equal(withUnit('199', 'ms'), '199 ms');
    assert.equal(withUnit('0.78', '°'), '0.78°');
    assert.equal(withUnit('12', '%'), '12%');
    assert.equal(withUnit('5', ''), '5');
  });
});

describe('sentenceStart', () => {
  const sentenceStart = fn('sentenceStart');

  it('capitalises the first letter and changes nothing else', () => {
    assert.equal(sentenceStart('p(occluder) by alignment'), 'P(occluder) by alignment');
    assert.equal(sentenceStart('saccade RT'), 'Saccade RT');
    assert.equal(sentenceStart('Already capitalised'), 'Already capitalised');
  });

  it('gives an empty string for a missing title', () => {
    assert.equal(sentenceStart(''), '');
    assert.equal(sentenceStart(null), '');
    assert.equal(sentenceStart(undefined), '');
  });
});

describe('panelLetter', () => {
  const panelLetter = fn('panelLetter');

  it('runs a to z, then aa, ab, …', () => {
    assert.equal(panelLetter(0), 'a');
    assert.equal(panelLetter(25), 'z');
    assert.equal(panelLetter(26), 'aa');
    assert.equal(panelLetter(27), 'ab');
    assert.equal(panelLetter(52), 'ba');
    assert.equal(panelLetter(701), 'zz');
    assert.equal(panelLetter(702), 'aaa');
  });

  it('never gives two panels the same letter', () => {
    const letters = Array.from({ length: 800 }, (_, i) => panelLetter(i));
    assert.equal(new Set(letters).size, letters.length);
  });
});

describe('ellipsize', () => {
  // The fake canvas measures every character as 6 px wide.
  const ellipsize = fn('ellipsize');

  it('leaves a label that fits alone', () => {
    assert.equal(ellipsize('Correct', 60), 'Correct');
    assert.equal(ellipsize('Correct', 42), 'Correct');
  });

  it('cuts a label that does not fit to the longest prefix that does, with an ellipsis', () => {
    const cut = ellipsize('BROKE_FIXATION', 60);
    assert.equal(cut, 'BROKE_FIX…');
    assert.ok(cut.length * 6 <= 60);
  });

  it('keeps at least one character, so a label never vanishes', () => {
    assert.equal(ellipsize('BROKE', 1), 'B…');
  });
});

describe('series colours', () => {
  const slotColor = fn('slotColor');
  const rampColor = fn('rampColor');
  const seriesColor = fn('seriesColor');

  it('counts categorical slots from 1 and wraps after the third', () => {
    assert.equal(slotColor(1), 'var(--series-1)');
    assert.equal(slotColor(2), 'var(--series-2)');
    assert.equal(slotColor(3), 'var(--series-3)');
    assert.equal(slotColor(4), 'var(--series-1)');
    assert.equal(slotColor(5), 'var(--series-2)');
  });

  it('treats a missing slot as the first', () => {
    assert.equal(slotColor(undefined), 'var(--series-1)');
    assert.equal(slotColor(0), 'var(--series-1)');
  });

  it('maps ordered steps onto the five-step ramp, clamped at both ends', () => {
    assert.equal(rampColor(0), 'var(--ramp-1)');
    assert.equal(rampColor(4), 'var(--ramp-5)');
    assert.equal(rampColor(-1), 'var(--ramp-1)');
    assert.equal(rampColor(9), 'var(--ramp-5)');
    assert.equal(rampColor(undefined), 'var(--ramp-1)');
  });

  it('colours a muted series grey, an ordered one by ramp and any other by slot', () => {
    assert.equal(seriesColor({ muted: true, slot: 2 }), 'var(--muted)');
    assert.equal(seriesColor({ muted: true, ramp: 2 }), 'var(--muted)');
    assert.equal(seriesColor({ ramp: 2 }), 'var(--ramp-3)');
    assert.equal(seriesColor({ ramp: 0, slot: 2 }), 'var(--ramp-1)');
    assert.equal(seriesColor({ slot: 2 }), 'var(--series-2)');
  });
});

describe('heatmap colours', () => {
  const heatColor = fn('heatColor');

  it('reads the five ramp colours from the theme as RGB triples', () => {
    // dashboard.css's light theme: --ramp-1 is #7fb3da, --ramp-5 is #0a3a5c.
    const stops = plain(fn('heatStops')());
    assert.equal(stops.length, 5);
    assert.deepEqual(stops[0], [0x7f, 0xb3, 0xda]);
    assert.deepEqual(stops[4], [0x0a, 0x3a, 0x5c]);
  });

  it('interpolates linearly between neighbouring stops', () => {
    const stops = [[0, 0, 0], [100, 200, 250]];
    assert.equal(heatColor(stops, 0), 'rgb(0,0,0)');
    assert.equal(heatColor(stops, 1), 'rgb(100,200,250)');
    assert.equal(heatColor(stops, 0.5), 'rgb(50,100,125)');
  });

  it('lands exactly on a stop at its share of the scale', () => {
    const stops = [[0, 0, 0], [10, 10, 10], [20, 20, 20], [30, 30, 30], [40, 40, 40]];
    assert.equal(heatColor(stops, 0.25), 'rgb(10,10,10)');
    assert.equal(heatColor(stops, 1), 'rgb(40,40,40)');
  });

  it('clamps a value outside 0 to 1 to the ends of the scale', () => {
    const stops = [[0, 0, 0], [100, 200, 250]];
    assert.equal(heatColor(stops, -1), 'rgb(0,0,0)');
    assert.equal(heatColor(stops, 2), 'rgb(100,200,250)');
  });
});

describe('valueLabel', () => {
  const valueLabel = fn('valueLabel');

  it('gives a count with its share in whole percent', () => {
    assert.equal(valueLabel({ value: 12, share: 0.5 }), '12 (50%)');
    assert.equal(valueLabel({ value: 2, share: 0.1 }), '2 (10%)');
  });

  it('keeps one decimal for a share under 10%, so a small share is not rounded away', () => {
    assert.equal(valueLabel({ value: 3, share: 0.034 }), '3 (3.4%)');
  });

  it('groups a large count', () => {
    assert.match(valueLabel({ value: 1234, share: 0.25 }), /^1\D?234 \(25%\)$/);
  });
});

describe('shown', () => {
  const shown = fn('shown');

  it('prefers the display form the session sent', () => {
    assert.equal(shown({ label: 'FIX_BREAK', display_label: 'Fix break' }, 'label'), 'Fix break');
  });

  it('falls back to the raw value for a payload saved before display forms existed', () => {
    assert.equal(shown({ label: 'FIX_BREAK' }, 'label'), 'FIX_BREAK');
    assert.equal(shown({ label: 'FIX_BREAK', display_label: '' }, 'label'), 'FIX_BREAK');
    assert.equal(shown({ label: 'FIX_BREAK', display_label: null }, 'label'), 'FIX_BREAK');
  });

  it('gives an empty string for a missing object', () => {
    assert.equal(shown(null, 'label'), '');
  });
});

describe('plot geometry', () => {
  const plotHeight = fn('plotHeight');
  const chartHeight = fn('chartHeight');
  const PAD = plain(dashboard.get('PAD'));

  it('scales the plot height with width, between 210 and 340 px', () => {
    assert.equal(plotHeight(100), 210);
    assert.equal(plotHeight(500), 250);
    assert.equal(plotHeight(2000), 340);
  });

  it('adds the tick row and axis title to the plot for the whole drawing', () => {
    assert.equal(chartHeight(500), PAD.top + 250 + PAD.bottom);
  });
});

describe('tableRows', () => {
  const tableRows = (data) => plain(fn('tableRows')(data));

  it('lists each dot with its interval, or a dash where none was measured', () => {
    const table = tableRows({
      form: 'dots',
      error_label: 'Mean ± s.e.m.',
      groups: [
        { label: 'left', display_label: 'Left', mean: 301.4, sem: 12.25, n: 12 },
        { label: 'right', mean: 290, sem: null, n: 1 },
        { label: 'up', mean: 0.625, low: 0.512, high: 0.713, n: 30 },
      ],
    });
    assert.deepEqual(table.head, ['Group', 'Mean', 'Mean ± s.e.m.', 'n']);
    assert.deepEqual(table.rows, [
      ['Left', '301', '± 12.3', '12'],
      ['right', '290', '—', '1'],
      ['up', '0.625', '0.512 – 0.713', '30'],
    ]);
  });

  it('merges line series onto one sorted x column, blank where a series has no point', () => {
    const table = tableRows({
      form: 'line',
      x_label: 'Trial',
      series: [
        { name: 'running', points: [[2, 0.375], [1, 0.125]] },
        { name: 'raw', display_name: 'Raw', points: [[3, 1.5]] },
      ],
    });
    assert.deepEqual(table.head, ['Trial', 'running', 'Raw']);
    assert.deepEqual(table.rows, [['1.00', '0.125', ''], ['2.00', '0.375', ''], ['3.00', '', '1.50']]);
  });

  it('reads a heatmap top row first, as the map is drawn', () => {
    const table = tableRows({
      form: 'heatmap',
      x_label: 'x',
      y_label: 'y',
      x_edges: [0, 2],
      y_edges: [0, 2, 4],
      maps: [{ name: 'rate', matrix: [[5], [null]] }],
    });
    assert.deepEqual(table.rows, [['1.00', '3.00', '', ''], ['1.00', '1.00', '', '5.00']]);
  });

  it('has no table for a picture', () => {
    assert.equal(fn('tableRows')({ form: 'image' }), null);
  });
});
