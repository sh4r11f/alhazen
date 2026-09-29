/* The Data view's plotting maths and drawing (workspace_plot.js).
 *
 * The maths is checked against numbers worked out by hand: a mean and its
 * standard error, a proportion from True/False cells, histogram bins and
 * counts, axis ticks. The drawing is checked in the fake DOM for what a
 * reader relies on: axes with labelled ticks, one mark per point, error bars,
 * a legend for groups, and refusals said in words.
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

import { FakeDocument } from './fake_dom.mjs';

const SOURCE = readFileSync(
  new URL('../../src/alhazen/cli/assets/workspace_plot.js', import.meta.url), 'utf8');

function load() {
  const context = vm.createContext({});
  vm.runInContext(SOURCE + '\nglobalThis.WorkspacePlot = WorkspacePlot;', context);
  return context.WorkspacePlot;
}
const P = load();
/* Values made in the script's context have its prototypes; compare copies. */
const plain = (value) => JSON.parse(JSON.stringify(value));
const close = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-9, `${actual} != ${expected}`);

test('cells read as numbers, booleans, empty or text', () => {
  assert.equal(P.parseCell('3.5'), 3.5);
  assert.equal(P.parseCell(' -2e3 '), -2000);
  assert.equal(P.parseCell('True'), 1);
  assert.equal(P.parseCell('false'), 0);
  assert.equal(P.parseCell(''), null);
  assert.equal(P.parseCell('  '), null);
  assert.ok(Number.isNaN(P.parseCell('HIT')));
  // Not measurements, though Number() would take them.
  assert.ok(Number.isNaN(P.parseCell('0x10')));
  assert.ok(Number.isNaN(P.parseCell('Infinity')));
});

test('a column is numeric only when every non-empty cell is', () => {
  assert.deepEqual(plain(P.columnType(['1', '', '2.5'])), { type: 'numeric', binary: false });
  assert.deepEqual(plain(P.columnType(['True', 'False', ''])), { type: 'numeric', binary: true });
  assert.deepEqual(plain(P.columnType(['1', 'x'])), { type: 'text', binary: false });
  assert.deepEqual(plain(P.columnType(['', ''])), { type: 'empty', binary: false });
});

test('mean and SEM use the sample standard deviation', () => {
  // 1..4: mean 2.5, sample variance 5/3, SEM sqrt(5/3/4).
  const stats = P.meanSem([1, 2, 3, 4]);
  close(stats.mean, 2.5);
  close(stats.sem, Math.sqrt(5 / 3 / 4));
  assert.equal(stats.n, 4);
  // One value has no spread to estimate.
  assert.deepEqual(plain(P.meanSem([7])), { mean: 7, sem: null, n: 1 });
  assert.deepEqual(plain(P.meanSem([])), { mean: null, sem: null, n: 0 });
});

test('ticks are round, cover the data and do not drift', () => {
  assert.deepEqual(plain(P.niceTicks(0, 1)), [0, 0.2, 0.4, 0.6, 0.8, 1]);
  assert.deepEqual(plain(P.niceTicks(0.13, 0.87)), [0, 0.2, 0.4, 0.6, 0.8, 1]);
  assert.deepEqual(plain(P.niceTicks(-7, 112)), [-50, 0, 50, 100, 150]);
  // 0.1 + 0.05 * 3 is 0.25000000000000006 when accumulated.
  assert.deepEqual(plain(P.niceTicks(0.1, 0.3)), [0.1, 0.15, 0.2, 0.25, 0.3]);
  // A single value still gets an axis around it.
  const flat = P.niceTicks(3, 3);
  assert.ok(flat[0] < 3 && flat[flat.length - 1] > 3);
  // Counts: whole steps only.
  assert.deepEqual(plain(P.niceTicks(0, 2, 5, true)), [0, 1, 2]);
  assert.equal(P.formatTick(0.25, 0.05), '0.25');
  assert.equal(P.formatTick(100, 50), '100');
});

test('histogram bins follow Sturges, rounded to a nice width', () => {
  // n = 8: ceil(log2 8) + 1 = 4 bins over 1..10 → width 2.25 → 2.5.
  const h = P.histogram([1, 2, 2, 3, 3, 3, 4, 10]);
  assert.deepEqual(plain(h.edges), [0, 2.5, 5, 7.5, 10]);
  // A value on an inner edge counts above it; the maximum in the last bin.
  assert.deepEqual(plain(h.counts), [3, 4, 0, 1]);
  assert.equal(h.counts.reduce((a, b) => a + b), 8);
  // Given edges (a group's share of a pooled histogram).
  assert.deepEqual(plain(P.histogram([0, 5, 9.9], [0, 5, 10]).counts), [1, 2]);
  assert.deepEqual(plain(P.histogram([4, 4, 4])), { edges: [3.5, 4.5], counts: [3] });
  assert.deepEqual(plain(P.histogram([])), { edges: [], counts: [] });
});

const TABLE = {
  columns: ['cond', 'success', 'rt_ms', 'hemifield'],
  rows: [
    ['near', 'True', '300', '-1'],
    ['near', 'False', '320', '1'],
    ['far', 'True', '250', '-1'],
    ['far', 'True', '', '1'],
  ],
};

test('mean ± SEM of a True/False column is a proportion per x', () => {
  const { figure } = P.build('mean', TABLE, 'cond', 'success', '');
  assert.equal(figure.yLabel, 'success (proportion)');
  assert.deepEqual(plain(figure.x.categories), ['far', 'near']);
  const points = plain(figure.series[0].points);
  assert.deepEqual(points.map((p) => [p.x, p.y, p.n]), [['far', 1, 2], ['near', 0.5, 2]]);
  close(points[1].sem, 0.5); // values 1, 0: sd 0.7071, / sqrt 2
  // A proportion's axis always spans 0 to 1.
  assert.equal(figure.y.ticks[0], 0);
  assert.equal(figure.y.ticks[figure.y.ticks.length - 1], 1);
});

test('groups are separate series; empty y rows are counted and said', () => {
  const { figure } = P.build('mean', TABLE, 'cond', 'rt_ms', 'hemifield');
  assert.deepEqual(plain(figure.series.map((s) => s.name)), ['-1', '1']);
  assert.deepEqual(plain(figure.series.map((s) => s.color)), [P.PALETTE[0], P.PALETTE[1]]);
  assert.match(figure.notes[0], /1 of 4 rows have an empty y/);
  assert.equal(figure.legend, true);
});

test('a numeric x is a linear axis, sorted and joined by a line', () => {
  const { figure } = P.build('mean', TABLE, 'rt_ms', 'success', '');
  assert.equal(figure.x.type, 'linear');
  assert.equal(figure.connect, true);
  assert.deepEqual(plain(figure.series[0].points.map((p) => p.x)), [250, 300, 320]);
});

test('refusals are sentences, never an empty figure', () => {
  assert.match(P.build('mean', TABLE, 'rt_ms', 'cond', '').error, /cond holds text, so it cannot be y/);
  assert.match(P.build('hist-x', TABLE, 'cond', 'rt_ms', '').error, /cond is not numeric/);
  assert.match(P.build('mean', { columns: TABLE.columns, rows: [] }, 'cond', 'success', '').error,
    /no rows/);
  const many = { columns: ['g', 'y'], rows: [...'abcdef'].map((g) => [g, '1']) };
  assert.match(P.build('scatter', many, 'y', 'y', 'g').error, /6 values; at most 5/);
  const wide = { columns: ['x', 'y'], rows: Array.from({ length: 31 }, (_, i) => [`c${i}`, '1']) };
  assert.match(P.build('mean', wide, 'x', 'y', '').error, /31 different text values/);
});

test('histograms of y or of x share bins across groups', () => {
  const { figure } = P.build('hist-y', TABLE, 'cond', 'rt_ms', 'cond');
  assert.equal(figure.kind, 'histogram');
  assert.equal(figure.xLabel, 'rt_ms');
  assert.deepEqual(plain(figure.series.map((s) => s.counts.reduce((a, b) => a + b))), [1, 2]);
  assert.equal(P.build('hist-x', TABLE, 'rt_ms', '', '').figure.xLabel, 'rt_ms');
});

test('the drawing has axes, ticks, labels, marks, error bars and a legend', () => {
  const document = new FakeDocument();
  const { figure } = P.build('mean', TABLE, 'cond', 'success', 'hemifield');
  const svg = P.render(figure, document);
  assert.equal(svg.localName, 'svg');
  assert.equal(svg.querySelectorAll('line.plot-grid').length, figure.y.ticks.length);
  const ticks = svg.querySelectorAll('text.plot-tick').map((t) => t.textContent);
  assert.ok(ticks.includes('far') && ticks.includes('near') && ticks.includes('0.4'));
  const labels = svg.querySelectorAll('text.plot-label').map((t) => t.textContent);
  assert.deepEqual(labels, ['cond', 'success (proportion)']);
  // Four points (2 groups × 2 conditions), each with a hover title.
  assert.equal(svg.querySelector('g.plot-marks').children.length, 4);
  assert.ok(svg.querySelectorAll('title').some((t) => /n = 1/.test(t.textContent)));
  const legend = svg.querySelectorAll('text.plot-legend-text').map((t) => t.textContent);
  assert.deepEqual(legend, ['-1', '1']);
  // Marker shapes differ by group, so identity is not colour alone.
  assert.equal(svg.querySelectorAll('circle.plot-mark').length, 2 + 1);
  assert.equal(svg.querySelectorAll('rect.plot-mark').length, 2 + 1);
});

test('one histogram is bars; several are outlines', () => {
  const document = new FakeDocument();
  const single = P.render(P.build('hist-y', TABLE, '', 'rt_ms', '').figure, document);
  assert.ok(single.querySelectorAll('rect.plot-bar').length >= 1);
  const grouped = P.render(P.build('hist-y', TABLE, '', 'rt_ms', 'cond').figure, document);
  assert.equal(grouped.querySelectorAll('rect.plot-bar').length, 0);
  assert.equal(grouped.querySelectorAll('path.plot-line').length, 2);
});

test('a scatter over the point limit says how much is drawn', () => {
  const rows = Array.from({ length: P.MAX_POINTS + 5 }, (_, i) => [String(i), String(i % 7)]);
  const { figure } = P.build('scatter', { columns: ['x', 'y'], rows }, 'x', 'y', '');
  assert.equal(figure.series[0].points.length, P.MAX_POINTS);
  assert.match(figure.notes[0], new RegExp(`first ${P.MAX_POINTS} of ${P.MAX_POINTS + 5}`));
});
