/* The heatmap panel: receptive-field maps, and any other quantity on a grid
 * of cells.
 *
 * The first block pins backward compatibility. fixtures/heatmap_2_7_0.json
 * holds what the 2.7.0 renderer drew for payloads in that release's wire
 * form — the SVG, the legend, hover readouts and the table — recorded once
 * from that release. A heatmap that gives none of the newer fields must keep
 * drawing exactly that, so an experiment's maps (rf-mapping's) do not change
 * under it on an upgrade.
 *
 * Run: node --test "tests/js/*.test.mjs"
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';

import { serialize } from './fake_dom.mjs';
import { SAVED_STATE, hostFor, loadLiveMonitor } from './load_live_monitor.mjs';

const RECORDED = JSON.parse(
  readFileSync(new URL('fixtures/heatmap_2_7_0.json', import.meta.url), 'utf8'),
).cases;

/* The colourbar's gradient gets a random id on every draw (so two heatmaps
 * on one page never share one); the recording writes it as heat-ID. */
const normalise = (markup) => markup.replace(/heat-[a-z0-9]*/g, 'heat-ID');

/** Draw one payload on a fresh page, `width` pixels wide, and hand back what
 *  a reader would see: the SVG, the legend, and a way to hover a cell. */
function drawn(payload, width) {
  const live_monitor = loadLiveMonitor();
  const host = hostFor(live_monitor, width);
  const legendHost = hostFor(live_monitor);
  live_monitor.get('drawHeatmap')(legendHost, host, structuredClone(payload));
  const svg = host.querySelector('svg');
  return {
    live_monitor: live_monitor,
    host: host,
    legendHost: legendHost,
    svg: svg,
    /** Hover cell `index` (in drawing order) and return the readout's markup. */
    hover(index) {
      svg.querySelectorAll('rect.hit')[index].fire('pointermove', { clientX: 30, clientY: 40 });
      return serialize(host.querySelector('.tip'));
    },
  };
}

describe('a heatmap in the 2.7.0 form draws as 2.7.0 drew it', () => {
  const unchanged = [
    'rf-mapping, three maps',
    'rf-mapping, five maps in three columns',
    'a unit-square slice with no axis labels',
  ];

  for (const name of unchanged) {
    const recorded = RECORDED[name];

    it(name + ': the same drawing, element for element', () => {
      const { svg } = drawn(recorded.payload, recorded.width);
      assert.equal(normalise(serialize(svg)), recorded.svg);
    });

    it(name + ': the same legend', () => {
      const { legendHost } = drawn(recorded.payload, recorded.width);
      assert.deepEqual(legendHost.children.map(serialize), recorded.legend);
    });

    it(name + ': the same hover readouts, "dva" included where 2.7.0 wrote it', () => {
      const page = drawn(recorded.payload, recorded.width);
      for (const { cell, tip } of recorded.tips) assert.equal(page.hover(cell), tip, 'cell ' + cell);
    });

    it(name + ': the same table', () => {
      const live_monitor = loadLiveMonitor();
      const table = structuredClone(live_monitor.get('tableRows')(structuredClone(recorded.payload)));
      assert.deepEqual(table, recorded.table);
    });
  }

  it('a slice with values below its colour range keeps every cell where and as it was', () => {
    // The colourbar may now say that values fell outside it; the cells
    // themselves — position, size and colour — must not move.
    const recorded = RECORDED['a unit-square slice with values below 0'];
    const { svg } = drawn(recorded.payload, recorded.width);
    const cells = svg.querySelectorAll('rect.hit').map(serialize);
    const recordedCells = recorded.svg.match(/<rect [^>]*class="hit"><\/rect>/g);
    assert.equal(cells.length, 16);
    assert.deepEqual(cells, recordedCells);
  });

  it('a slice with values below its colour range keeps its hover readouts and table', () => {
    const recorded = RECORDED['a unit-square slice with values below 0'];
    const page = drawn(recorded.payload, recorded.width);
    // Cell 15 is inside the range: its readout is exactly as recorded.
    assert.equal(page.hover(15), recorded.tips.find((tip) => tip.cell === 15).tip);
    const table = structuredClone(page.live_monitor.get('tableRows')(structuredClone(recorded.payload)));
    assert.deepEqual(table, recorded.table);
  });
});

/* ------------------------------------------------------------------ */
/* Axes                                                                */
/* ------------------------------------------------------------------ */

/* n + 1 edges from lo to hi in equal ratios: the cells of a log axis. */
const geometric = (lo, hi, n) => Array.from({ length: n + 1 }, (_, i) => lo * Math.pow(hi / lo, i / n));

/* A posterior slice as mbri sends it in the new form: real edges on two log
 * axes, a unit for each, and a colour range. Values rise to the right. */
function searchSlice(overrides) {
  return {
    form: 'heatmap',
    maps: [{ name: 'posterior mean', matrix: [[0.32, 0.36, 0.40], [0.34, 0.38, 0.42], [0.36, 0.40, 0.44]] }],
    x_edges: [4, 8, 16, 32],
    y_edges: geometric(0.2, 3, 3),
    x_scale: 'log',
    y_scale: 'log',
    x_unit: 'dva/s',
    y_unit: 'dots/dva²',
    x_label: 'Speed (dva/s)',
    y_label: 'Dot density (dots/dva²)',
    value_label: 'Balanced accuracy',
    vmin: 0.3,
    vmax: 0.45,
    ...overrides,
  };
}

/* A receptive-field plate that opts in to axes: degrees on both axes. */
function rfWithAxes(overrides) {
  return {
    ...structuredClone(RECORDED['rf-mapping, three maps'].payload),
    x_scale: 'linear',
    y_scale: 'linear',
    x_unit: '°',
    y_unit: '°',
    ...overrides,
  };
}

const numberAttr = (element, name) => Number(element.getAttribute(name));

/** The drawn extent of a heatmap's first map: its left, right, top and
 *  bottom in pixels, read off its outermost cells. */
function mapBox(svg, cellsPerMap) {
  const all = svg.querySelectorAll('rect.hit');
  const cells = all.slice(0, cellsPerMap || all.length);
  const xs = cells.flatMap((cell) => [numberAttr(cell, 'x'), numberAttr(cell, 'x') + numberAttr(cell, 'width')]);
  const ys = cells.flatMap((cell) => [numberAttr(cell, 'y'), numberAttr(cell, 'y') + numberAttr(cell, 'height')]);
  return { left: Math.min(...xs), right: Math.max(...xs), top: Math.min(...ys), bottom: Math.max(...ys) };
}

/** The x-axis ticks (marks hanging below a map) and y-axis ticks (marks left
 *  of it), each as [{at, text}] with the label drawn at that tick, in
 *  drawing order. */
function ticksOf(svg) {
  const marks = svg.querySelectorAll('line.tick-mark');
  const labels = svg.querySelectorAll('text.tick-text');
  const x = [];
  const y = [];
  marks.forEach((mark) => {
    if (mark.getAttribute('x1') === mark.getAttribute('x2')) {
      const at = numberAttr(mark, 'x1');
      const label = labels.find((text) =>
        numberAttr(text, 'x') === at && numberAttr(text, 'y') === numberAttr(mark, 'y1') + 16);
      x.push({ at: at, text: label.textContent });
    } else {
      const at = numberAttr(mark, 'y1');
      const label = labels.find((text) =>
        numberAttr(text, 'y') === at + 4 && numberAttr(text, 'x') === numberAttr(mark, 'x1') - 4);
      y.push({ at: at, text: label.textContent });
    }
  });
  return { x: x, y: y };
}

/** Parse a tick label back into its number: '1,500' is 1500, '−3' is −3. */
const valueOf = (text) => Number(text.replace(/,/g, '').replace('−', '-'));

/* The label heatTickText writes for a value, for comparing an end's label. */
const heatTickText = (value) => loadLiveMonitor().get('heatTickText')(value);

describe('logTicks', () => {
  const logTicks = (...args) => structuredClone(loadLiveMonitor().get('logTicks')(...args));

  it('puts round values on a log axis: 1, 2 and 5 of each decade where they fit', () => {
    assert.deepEqual(logTicks(4, 32, 5), [5, 10, 20]);
    assert.deepEqual(logTicks(0.2, 3, 5), [0.2, 0.5, 1, 2]);
    assert.deepEqual(logTicks(50, 1500, 5), [50, 100, 200, 500, 1000]);
  });

  it('offers every digit of a decade when the axis has room for them', () => {
    assert.deepEqual(logTicks(4, 32, 9), [4, 5, 6, 7, 8, 9, 10, 20, 30]);
  });

  it('thins whole decades over a wide range, counting from the first decade on the axis', () => {
    assert.deepEqual(logTicks(0.001, 1000, 5), [0.001, 0.1, 10, 1000]);
    assert.deepEqual(logTicks(1, 1e9, 4), [1, 1e3, 1e6, 1e9]);
  });

  it('takes the linear steps on an axis spanning less than about a factor of two', () => {
    const live_monitor = loadLiveMonitor();
    assert.deepEqual(
      structuredClone(live_monitor.get('logTicks')(4.2, 4.8, 4)),
      structuredClone(live_monitor.get('niceTicks')(4.2, 4.8, 4)),
    );
  });

  it('never offers more values than asked for, nor one outside the range', () => {
    const cases = [[4, 32, 5], [0.2, 3, 3], [50, 1500, 2], [0.001, 1000, 3], [7, 30, 4], [1, 1e9, 2]];
    for (const [lo, hi, target] of cases) {
      const ticks = logTicks(lo, hi, target);
      const label = `logTicks(${lo}, ${hi}, ${target}) = ${JSON.stringify(ticks)}`;
      assert.ok(ticks.length <= target, label);
      assert.ok(ticks.every((value) => value >= lo && value <= hi), label);
    }
  });
});

describe('heatTickText', () => {
  it('writes a value with up to three significant digits and no padding zeros', () => {
    assert.deepEqual(
      [4, 32, 0.2, 10.5, 0.775, 123.4, 0].map(heatTickText),
      ['4', '32', '0.2', '10.5', '0.775', '123', '0'],
    );
  });

  it('writes the floating-point dust of a computed edge as the value meant', () => {
    assert.equal(heatTickText(31.999999999999996), '32');
    assert.equal(heatTickText(0.30000000000000004), '0.3');
  });

  it('groups thousands and sets a true minus sign, as every other number on the page', () => {
    assert.equal(heatTickText(1500), '1,500');
    assert.equal(heatTickText(999.6), '1,000');
    assert.equal(heatTickText(-10.5), '−10.5');
  });
});

describe('a heatmap that gives its axes a scale', () => {
  it('labels both ends of a log axis and round values between, each where its value sits', () => {
    const { svg } = drawn(searchSlice(), 700);
    const box = mapBox(svg);
    const { x, y } = ticksOf(svg);
    assert.deepEqual(x.map((tick) => tick.text), ['4', '5', '10', '20', '32']);
    assert.deepEqual(y.map((tick) => tick.text), ['0.2', '0.5', '1', '2', '3']);
    // On a log axis a value v sits at log10(v), so 4 to 8 is as long as 16 to 32.
    const across = (v) =>
      box.left + ((Math.log10(v) - Math.log10(4)) / Math.log10(32 / 4)) * (box.right - box.left);
    const up = (v) =>
      box.bottom - ((Math.log10(v) - Math.log10(0.2)) / Math.log10(3 / 0.2)) * (box.bottom - box.top);
    x.forEach((tick) => assert.ok(Math.abs(tick.at - across(valueOf(tick.text))) < 1e-9, tick.text));
    y.forEach((tick) => assert.ok(Math.abs(tick.at - up(valueOf(tick.text))) < 1e-9, tick.text));
  });

  it('puts a tick at a cell edge exactly on that edge', () => {
    // Cells need not be equal ratios on a log axis. With edges at 10 and
    // 20, the round-value ticks there must meet the cells' boundaries.
    const { svg } = drawn(searchSlice({ x_edges: [4, 10, 20, 32] }), 700);
    const { x } = ticksOf(svg);
    const cellEdges = svg.querySelectorAll('rect.hit').map((cell) => numberAttr(cell, 'x'));
    for (const text of ['10', '20']) {
      const tick = x.find((t) => t.text === text);
      assert.ok(tick, 'no tick labelled ' + text + ' in ' + JSON.stringify(x.map((t) => t.text)));
      assert.ok(cellEdges.some((edge) => Math.abs(edge - tick.at) < 1e-9), text);
    }
  });

  it('places linear ticks at round steps, with the origin where 0 is labelled', () => {
    const { svg } = drawn(rfWithAxes(), 400);
    const { x, y } = ticksOf(svg);
    // Three maps, each with its own axes; the first map's come first.
    assert.deepEqual(x.slice(0, 3).map((tick) => tick.text), ['−3', '0', '3']);
    const zero = x.find((tick) => tick.text === '0');
    const rule = svg.querySelectorAll('line.rule').find((line) => line.getAttribute('x1') === line.getAttribute('x2'));
    assert.equal(numberAttr(rule, 'x1'), zero.at);
    assert.ok(y.some((tick) => tick.text === '0'));
  });

  it('never lets two tick labels touch, and never drops an end', () => {
    const payloads = [searchSlice(), searchSlice({ x_edges: geometric(50, 1500, 3) }), rfWithAxes()];
    for (const width of [300, 380, 520, 900]) {
      for (const payload of payloads) {
        const { svg } = drawn(payload, width);
        const all = ticksOf(svg).x;
        // The first map's axis: its ticks run left to right, so it ends
        // where the next map's begins again further left.
        const end = all.findIndex((tick, i) => i > 0 && tick.at < all[i - 1].at);
        const first = end < 0 ? all : all.slice(0, end);
        const label = width + ' px: ' + JSON.stringify(first.map((tick) => tick.text));
        for (let i = 1; i < first.length; i += 1) {
          const gap = first[i].at - first[i - 1].at;
          const room = ((first[i].text.length + first[i - 1].text.length) * 6) / 2;
          assert.ok(gap >= room + 6, label);
        }
        assert.equal(first[0].text, heatTickText(payload.x_edges[0]), label);
        assert.equal(first[first.length - 1].text, heatTickText(payload.x_edges[payload.x_edges.length - 1]), label);
      }
    }
  });

  it('draws the map square when the axes share no length, at the data\'s aspect when they do', () => {
    const square = (box) => Math.abs((box.right - box.left) - (box.bottom - box.top)) < 1e-9;
    assert.ok(square(mapBox(drawn(searchSlice(), 700).svg)), 'log by log');
    assert.ok(square(mapBox(drawn(searchSlice({ x_scale: 'linear', y_scale: 'linear' }), 700).svg)),
      'linear, but dva/s by dots/dva²');
    // Degrees by degrees: 6 across, 4.5 up, and a degree the same length both ways.
    const rf = mapBox(drawn(rfWithAxes(), 700).svg, 12);
    assert.ok(Math.abs((rf.right - rf.left) / (rf.bottom - rf.top) - 6 / 4.5) < 1e-9);
  });

  it('titles the axes once, and the colourbar with the value alone', () => {
    const { svg } = drawn(searchSlice(), 700);
    const titles = svg.querySelectorAll('text.axis-text');
    assert.deepEqual(titles.map((text) => text.textContent),
      ['Speed (dva/s)', 'Dot density (dots/dva²)', 'Balanced accuracy']);
    assert.match(titles[1].getAttribute('transform'), /^rotate\(-90 /);
  });

  it('draws no axes for a payload that gives units but no scale', () => {
    const payload = searchSlice({ x_scale: undefined, y_scale: undefined, x_edges: [0, 1, 2, 3], y_edges: [0, 1, 2, 3] });
    const { svg } = drawn(payload, 700);
    assert.equal(svg.querySelectorAll('line.tick-mark').length, 0);
    assert.equal(svg.querySelectorAll('line.spine').length, 0);
  });
});

describe('the hover readout and the table give the cells\' real positions and units', () => {
  it('reads a log cell out at its geometric centre, with each axis\'s unit', () => {
    const page = drawn(searchSlice(), 700);
    // The bottom-left cell spans 4–8 dva/s: its middle on the drawn axis is
    // √(4·8) = 5.66, not 6.
    const density = Math.sqrt(0.2 * geometric(0.2, 3, 3)[1]);
    assert.match(page.hover(0),
      new RegExp('<div class="tip-head">5\\.66 dva/s, ' + density.toFixed(3) + ' dots/dva²</div>'));
  });

  it('writes no unit for an axis that gives none, and never "dva" once the axes are named', () => {
    const page = drawn(searchSlice({ x_unit: undefined, y_unit: undefined }), 700);
    assert.match(page.hover(0), /<div class="tip-head">5\.66, 0\.\d+<\/div>/);
  });

  it('writes a degree sign against its number, as print does', () => {
    const page = drawn(rfWithAxes(), 400);
    assert.match(page.hover(0), /<div class="tip-head">−2\.25°, −0\.75°<\/div>/);
  });

  it('gives the table the same positions as the hover, and its heads the units once', () => {
    const live_monitor = loadLiveMonitor();
    const table = structuredClone(live_monitor.get('tableRows')(searchSlice()));
    assert.deepEqual(table.head, ['Speed (dva/s)', 'Dot density (dots/dva²)', 'Flashes', 'posterior mean']);
    // The last three rows are the bottom row, left to right: centres √32,
    // √128 and √512 dva/s.
    assert.deepEqual(table.rows.slice(-3).map((row) => row[0]), ['5.66', '11.3', '22.6']);
    const untitled = structuredClone(
      live_monitor.get('tableRows')(searchSlice({ x_label: undefined, y_label: 'Density' })));
    assert.deepEqual(untitled.head.slice(0, 2), ['x (dva/s)', 'Density (dots/dva²)']);
  });
});

/* ------------------------------------------------------------------ */
/* The colour range                                                    */
/* ------------------------------------------------------------------ */

describe('the colour range', () => {
  /* The fill of each cell, in drawing order (row 0 first). */
  const fills = (svg) => svg.querySelectorAll('rect.hit').map((cell) => cell.getAttribute('style').replace(/^fill:/, ''));

  it('spreads the whole ramp over vmin to vmax', () => {
    const page = drawn(searchSlice(), 700);
    const stops = page.live_monitor.get('heatStops')();
    const heatColor = page.live_monitor.get('heatColor');
    // Cells hold 0.32 … 0.44 on a range of 0.30 to 0.45.
    const expected = [0.32, 0.36, 0.40, 0.34, 0.38, 0.42, 0.36, 0.40, 0.44]
      .map((value) => heatColor(stops, (value - 0.3) / (0.45 - 0.3)));
    assert.deepEqual(fills(page.svg), expected);
  });

  it('labels the colourbar\'s ends with the range', () => {
    const { svg } = drawn(searchSlice(), 700);
    const labels = svg.querySelectorAll('text.tick-text').slice(-2).map((text) => text.textContent);
    assert.deepEqual(labels, ['0.3', '0.45']);
  });

  it('marks values past either end on the colourbar, in the legend and in the hover', () => {
    const payload = searchSlice({
      maps: [{ name: 'posterior mean', matrix: [[0.12, 0.25, 0.40], [0.34, 0.38, 0.52], [0.36, 0.40, 0.44]] }],
    });
    const page = drawn(payload, 700);
    const stops = page.live_monitor.get('heatStops')();
    const low = 'rgb(' + stops[0].join(',') + ')';
    const high = 'rgb(' + stops[4].join(',') + ')';
    // Drawn in the colour of the end they passed...
    assert.equal(fills(page.svg)[0], low);
    assert.equal(fills(page.svg)[5], high);
    // ...with an arrow-head at each end of the colourbar, in that colour...
    const caps = page.svg.querySelectorAll('path.out-of-range').map((path) => path.getAttribute('style'));
    assert.deepEqual(caps, ['fill:' + low, 'fill:' + high]);
    // ...the legend saying how many, and how far...
    const legend = page.legendHost.querySelector('div.legend');
    assert.deepEqual(
      legend.children.map((item) => [item.querySelector('i').className, item.children[1].textContent]),
      [
        ['below', '2 cells below the colour range (lowest 0.12)'],
        ['above', '1 cell above the colour range (highest 0.52)'],
      ],
    );
    // ...and the hover saying so for the cell itself.
    assert.match(page.hover(0),
      /<span class="tip-val">0\.12<\/span>.*<span class="tip-val">below the colour range<\/span><span class="tip-name">drawn as 0\.3<\/span>/);
    assert.match(page.hover(5),
      /<span class="tip-val">above the colour range<\/span><span class="tip-name">drawn as 0\.45<\/span>/);
    // An in-range cell's readout says nothing of the kind.
    assert.doesNotMatch(page.hover(4), /colour range/);
  });

  it('moves each end\'s number out past its arrow-head', () => {
    const inside = drawn(searchSlice(), 700).svg.querySelectorAll('text.tick-text').slice(-2);
    const payload = searchSlice({ maps: [{ name: 'm', matrix: [[0.1, 0.4, 0.4], [0.4, 0.4, 0.4], [0.4, 0.4, 0.9]] }] });
    const outside = drawn(payload, 700).svg.querySelectorAll('text.tick-text').slice(-2);
    assert.equal(numberAttr(outside[0], 'x'), numberAttr(inside[0], 'x') - 7);
    assert.equal(numberAttr(outside[1], 'x'), numberAttr(inside[1], 'x') + 7);
  });

  it('keeps the true value in the table', () => {
    const payload = searchSlice({ maps: [{ name: 'm', matrix: [[0.12, 0.4, 0.4], [0.4, 0.4, 0.4], [0.4, 0.4, 0.4]] }] });
    const table = structuredClone(loadLiveMonitor().get('tableRows')(payload));
    // Top row first in the table, so row 0 of the matrix is the last three.
    assert.equal(table.rows[6][3], '0.12');
  });

  it('marks a 2.7-style map whose values fall below 0, the start of its scale', () => {
    // Before, these cells were drawn in the colour of 0 and the page said nothing.
    const recorded = RECORDED['a unit-square slice with values below 0'];
    const page = drawn(recorded.payload, recorded.width);
    assert.equal(page.svg.querySelectorAll('path.out-of-range').length, 1);
    assert.equal(page.legendHost.querySelector('div.legend').textContent,
      '3 cells below the colour range (lowest −0.12)');
    assert.match(page.hover(0), /below the colour range/);
  });

  it('draws no mark and no legend line when every value is inside the range', () => {
    const page = drawn(searchSlice(), 700);
    assert.equal(page.svg.querySelectorAll('path.out-of-range').length, 0);
    assert.equal(page.legendHost.children.length, 0);
  });
});

describe('a malformed heatmap says what is wrong instead of drawing a wrong map', () => {
  const message = (payload) => drawn(payload, 500).host.querySelector('div.empty').textContent;

  it('refuses a scale it does not know', () => {
    assert.equal(message(searchSlice({ x_scale: 'Log' })),
      'Malformed map: x_scale must be "linear" or "log", not "Log"');
  });

  it('refuses one scale without the other', () => {
    assert.equal(message(searchSlice({ y_scale: undefined })), 'Malformed map: give both x_scale and y_scale');
  });

  it('refuses a log axis that reaches 0', () => {
    assert.equal(
      message(searchSlice({ y_edges: [0, 1, 2, 3] })),
      'Malformed map: a log axis needs every edge above 0 (y_edges starts at 0)',
    );
  });

  it('refuses a colour range the wrong way round', () => {
    assert.equal(message(searchSlice({ vmin: 0.5, vmax: 0.4 })), 'Malformed map: vmin must be below vmax');
  });
});

describe('a heatmap\'s figure export', () => {
  it('keeps the axes and the out-of-range legend in the saved SVG', async () => {
    const live_monitor = loadLiveMonitor();
    const letter = live_monitor.document.createElement('span');
    letter.textContent = 'c';
    const data = searchSlice({ maps: [{ name: 'm', matrix: [[0.12, 0.4, 0.4], [0.4, 0.4, 0.4], [0.4, 0.4, 0.4]] }] });
    live_monitor.get('exportFigure')({ data: data, letter: letter, title: 'Posterior' }, 'svg', 'single');
    const markup = await live_monitor.downloads[0].blob.text();
    const wanted = ['>4<', '>32<', '>0.2<', '>3<', '>Speed (dva/s)<', '>Dot density (dots/dva²)<',
      '>1 cell below the colour range (lowest 0.12)<'];
    for (const text of wanted) assert.ok(markup.includes(text), text + ' missing from the exported figure');
    assert.doesNotMatch(markup, /class="hit"|data-screen-only/);
  });

  it('keeps every cell of every map, which are its marks as well as its hover targets', async () => {
    // Up to 2.7 the export dropped each cell as a hover target, and saved
    // an empty frame with a colourbar under it.
    const live_monitor = loadLiveMonitor();
    const letter = live_monitor.document.createElement('span');
    const data = structuredClone(RECORDED['rf-mapping, three maps'].payload);
    live_monitor.get('exportFigure')({ data: data, letter: letter, title: 'Receptive fields' }, 'svg', 'single');
    const markup = await live_monitor.downloads[0].blob.text();
    // The white ground, three maps of 3 x 4 cells, the colourbar, and the
    // legend's "unprobed cell" swatch.
    assert.equal(markup.match(/<rect /g).length, 1 + 3 * 12 + 1 + 1);
    assert.doesNotMatch(markup, /class="hit"|data-screen-only/);
  });
});

describe('a heatmap the session could not send as drawn', () => {
  /* What live_monitor_state() publishes, during a session, in place of a
   * heatmap that failed its check: an error card with the check's message. */
  const MESSAGE = 'Malformed map: heatmap colour range is empty: vmin and vmax are both 0.4; ' +
    'vmin must be below vmax';
  const savedPage = () => loadLiveMonitor({
    staticState: {
      ...SAVED_STATE,
      panels: [{ title: 'Posterior slice', section: 'Search', data: { form: 'error', message: MESSAGE } }],
    },
  });

  it('shows the problem in the panel\'s place, in the status red, live and saved alike', () => {
    const live_monitor = savedPage();
    const card = live_monitor.document.querySelector('section.panel');
    assert.equal(card.querySelector('h2').textContent, 'aPosterior slice');
    const box = card.querySelector('div.empty');
    assert.equal(box.textContent, MESSAGE);
    assert.equal(box.getAttribute('data-status'), 'critical');
    assert.equal(card.querySelector('svg'), null);
    assert.deepEqual(live_monitor.consoleErrors, []);
  });

  it('offers no table and no figure export for a panel with nothing drawn', () => {
    const card = savedPage().document.querySelector('section.panel');
    assert.equal(card.querySelector('details.table'), null);
    assert.equal(card.querySelector('div.figure-export'), null);
  });
});
