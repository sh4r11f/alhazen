/* The charts, drawn into the fake page: what marks, labels and legends each
 * one puts on screen, and what a figure export writes to disk. Each test
 * loads a fresh page, so no drawing leaks into the next.
 *
 * Run: node --test "tests/js/*.test.mjs"
 */

import assert from 'node:assert/strict';
import { beforeEach, describe, it } from 'node:test';

import { SVG_NS } from './fake_dom.mjs';
import { hostFor, loadLiveMonitor, settle } from './load_live_monitor.mjs';

let live_monitor;
beforeEach(() => {
  live_monitor = loadLiveMonitor();
});

const draw = (name) => live_monitor.get(name);
const numberAttr = (element, name) => Number(element.getAttribute(name));

/* The painted marks of a chart, without the invisible hover targets laid over
 * them (class "hit"). */
const painted = (svg, tag) => svg.querySelectorAll(tag).filter((node) => !node.classList.contains('hit'));

/* The colour a marker was filled with. marker() draws a surface-coloured ring
 * under each dot; the dot is the circle that is not the ring. */
function markerColours(svg) {
  return svg.querySelectorAll('circle')
    .map((circle) => circle.getAttribute('style'))
    .filter((style) => style !== 'fill:var(--surface)')
    .map((style) => style.replace(/^fill:/, ''));
}

/* The legend as the reader sees it: each entry's name, swatch shape and the
 * colour its swatch was painted with. */
function legendOf(legendHost) {
  const legend = legendHost.querySelector('div.legend');
  if (!legend) return null;
  return legend.children
    .filter((item) => !item.classList.contains('legend-title'))
    .map((item) => {
      const swatch = item.querySelector('i');
      return {
        name: item.children[1].textContent,
        shape: swatch.className,
        colour: swatch.style.background || swatch.style.color || swatch.style.borderColor,
      };
    });
}

describe('drawFrame', () => {
  /* Ticks that stop short of the plotting box on both axes, so a spine drawn
   * to the box's edge and one drawn to the last tick come out different. */
  const box = { x0: 50, x1: 350, y0: 10, y1: 210 };
  const scales = { xScale: (v) => 50 + v * 10, yScale: (v) => 210 - v * 150 };

  function frame(xTicks, yTicks) {
    const svg = live_monitor.document.createElementNS(SVG_NS, 'svg');
    draw('drawFrame')(svg, box, { ...scales, xTicks: xTicks, yTicks: yTicks });
    return svg;
  }

  it('ends each spine on its outermost tick, not at the edge of the plotting box', () => {
    const spines = frame([0, 10, 20], [0, 0.5, 1]).querySelectorAll('line.spine');
    const vertical = spines.find((line) => line.getAttribute('x1') === line.getAttribute('x2'));
    const horizontal = spines.find((line) => line.getAttribute('y1') === line.getAttribute('y2'));
    // y ticks 0..1 sit at 210..60 px; x ticks 0..20 at 50..250 px.
    assert.deepEqual([numberAttr(vertical, 'y1'), numberAttr(vertical, 'y2')], [60, 210]);
    assert.deepEqual([numberAttr(horizontal, 'x1'), numberAttr(horizontal, 'x2')], [50, 250]);
  });

  it('keeps the full box length for an axis with no labelled extent', () => {
    // A grouped panel's category axis has no ticks to trim the spine to.
    const spines = frame([], [0, 0.5, 1]).querySelectorAll('line.spine');
    const horizontal = spines.find((line) => line.getAttribute('y1') === line.getAttribute('y2'));
    assert.deepEqual([numberAttr(horizontal, 'x1'), numberAttr(horizontal, 'x2')], [50, 350]);
  });

  it('labels each tick with the decimals its step implies', () => {
    const labels = frame([0, 10, 20], [0, 0.5, 1]).querySelectorAll('text.tick-text')
      .map((text) => text.textContent);
    assert.deepEqual(labels, ['0.0', '0.5', '1.0', '0', '10', '20']);
  });

  it('draws outward tick marks and two spines, and no gridlines', () => {
    const svg = frame([0, 10, 20], [0, 0.5, 1]);
    const lines = svg.querySelectorAll('line');
    assert.equal(lines.length, 3 + 3 + 2);
    svg.querySelectorAll('line.tick-mark').forEach((tick) => {
      const length = Math.hypot(
        numberAttr(tick, 'x2') - numberAttr(tick, 'x1'),
        numberAttr(tick, 'y2') - numberAttr(tick, 'y1'),
      );
      assert.equal(length, 5);
      // Outward: left of the y spine, or below the x spine.
      assert.ok(numberAttr(tick, 'x1') < box.x0 || numberAttr(tick, 'y2') > box.y1);
    });
  });
});

describe('drawBars', () => {
  const outcomes = {
    form: 'bars',
    total: 20,
    value_label: 'Trials',
    items: [
      { label: 'CORRECT', display_label: 'Correct', value: 12, share: 0.6 },
      { label: 'FIX_BREAK', display_label: 'Fix break', value: 6, share: 0.3 },
      { label: 'NO_RESPONSE', display_label: 'No response', value: 2, share: 0.1 },
    ],
  };

  function bars(data, width) {
    const host = hostFor(live_monitor, width);
    const legendHost = hostFor(live_monitor);
    draw('drawBars')(legendHost, host, data);
    return { host: host, legendHost: legendHost, svg: host.querySelector('svg') };
  }

  it('draws one bar per category, its length in proportion to its count', () => {
    const { svg } = bars(outcomes);
    const widths = painted(svg, 'rect').map((rect) => numberAttr(rect, 'width'));
    assert.equal(widths.length, 3);
    assert.ok(Math.abs(widths[0] / widths[1] - 2) < 1e-9, String(widths));
    assert.ok(Math.abs(widths[0] / widths[2] - 6) < 1e-9, String(widths));
  });

  it('paints every bar in the one colour, since the categories have no order', () => {
    const { svg } = bars(outcomes);
    const fills = painted(svg, 'rect').map((rect) => rect.getAttribute('style'));
    assert.deepEqual(fills, Array(3).fill('fill:var(--series-1)'));
  });

  it('labels each row with its display name and its count and share', () => {
    const { svg } = bars(outcomes);
    const texts = svg.querySelectorAll('text').map((text) => text.textContent);
    assert.deepEqual(texts, ['Correct', '12 (60%)', 'Fix break', '6 (30%)', 'No response', '2 (10%)']);
  });

  it('shortens a name wider than its column and keeps it whole in the hover readout', () => {
    const long = {
      ...outcomes,
      items: [{ label: 'A_VERY_LONG_OUTCOME_NAME', value: 3, share: 1 }],
    };
    // A 200 px panel gives labels at most 42% of its width: 84 px, minus 10.
    const { host, svg } = bars(long, 200);
    const label = svg.querySelectorAll('text')[0].textContent;
    assert.ok(label.endsWith('…'), label);
    assert.ok(label.length * 6 <= 74, label);

    svg.querySelector('rect.hit').fire('pointermove', { clientX: 20, clientY: 20 });
    assert.equal(host.querySelector('.tip-head').textContent, 'A_VERY_LONG_OUTCOME_NAME');
  });

  it('adds no legend: one series is named by the panel title', () => {
    const { legendHost } = bars(outcomes);
    assert.equal(legendHost.children.length, 0);
  });

  it('says there is no data yet rather than drawing an empty frame', () => {
    const { host } = bars({ ...outcomes, items: [] });
    assert.equal(host.querySelector('svg'), null);
    assert.equal(host.querySelector('div.empty').textContent, 'No data yet');
  });
});

describe('drawDots', () => {
  /* Two factors on one axis, two levels each: how a panel of condition means
   * arrives when a task declares several factors. */
  function twoFactors(overrides) {
    return {
      form: 'dots',
      style: 'dots',
      x_label: 'condition',
      y_label: 'RT (ms)',
      error_label: 'Mean ± s.e.m.',
      groups: [
        { series: 'side', display_series: 'Side', label: 'left', mean: 300, sem: 10, n: 12 },
        { series: 'side', display_series: 'Side', label: 'right', mean: 320, sem: 12, n: 11 },
        { series: 'contrast', label: 'low', mean: 340, sem: 9, n: 10 },
        { series: 'contrast', label: 'high', mean: 290, sem: 8, n: 13 },
      ],
      ...overrides,
    };
  }

  function dots(data) {
    const host = hostFor(live_monitor);
    const legendHost = hostFor(live_monitor);
    draw('drawDots')(legendHost, host, data);
    return { host: host, legendHost: legendHost, svg: host.querySelector('svg') };
  }

  it('gives each factor on a shared axis its own colour, counting slots from 1', () => {
    // Passing the 0-based index gave the first two factors slot 1 both, and
    // they were drawn in the same blue.
    const { svg, legendHost } = dots(twoFactors());
    assert.deepEqual(markerColours(svg), [
      'var(--series-1)', 'var(--series-1)', 'var(--series-2)', 'var(--series-2)',
    ]);
    assert.deepEqual(legendOf(legendHost).slice(0, 2), [
      { name: 'Side', shape: 'line', colour: 'var(--series-1)' },
      { name: 'contrast', shape: 'line', colour: 'var(--series-2)' },
    ]);
  });

  it('fills the bars of a bar-style panel by factor too, with black error bars', () => {
    const { svg } = dots(twoFactors({ style: 'bars' }));
    const fills = painted(svg, 'rect').map((rect) => rect.getAttribute('style'));
    assert.deepEqual(fills, [
      'fill:var(--series-1)', 'fill:var(--series-1)', 'fill:var(--series-2)', 'fill:var(--series-2)',
    ]);
    svg.querySelectorAll('path').forEach((whisker) => {
      assert.match(whisker.getAttribute('style'), /^stroke:var\(--axis\);/);
    });
  });

  it('keeps a panel grouped by one factor in the one colour, with no factor legend', () => {
    const one = twoFactors();
    one.groups = one.groups.slice(0, 2);
    const { svg, legendHost } = dots(one);
    assert.deepEqual(markerColours(svg), ['var(--series-1)', 'var(--series-1)']);
    assert.deepEqual(legendOf(legendHost).map((entry) => entry.shape), ['whisker']);
  });

  it('defines its error bar in the legend whenever one is drawn, even for one series', () => {
    // A journal will not print an error bar the figure does not define.
    const one = twoFactors();
    one.groups = one.groups.slice(0, 2);
    const { svg, legendHost } = dots(one);
    assert.equal(svg.querySelectorAll('path').length, 2);
    assert.deepEqual(legendOf(legendHost), [
      { name: 'Mean ± s.e.m.', shape: 'whisker', colour: 'var(--ink-2)' },
    ]);
  });

  it('defines no error bar when none is drawn', () => {
    const one = twoFactors();
    one.groups = one.groups.slice(0, 2).map((group) => ({ ...group, sem: null }));
    const { svg, legendHost } = dots(one);
    assert.equal(svg.querySelectorAll('path').length, 0);
    assert.equal(legendOf(legendHost), null);
  });

  it('writes each group\'s sample size under its label, with an italic n', () => {
    const { svg } = dots(twoFactors());
    const count = svg.querySelectorAll('text').find((text) => text.textContent === 'n = 12');
    assert.ok(count, 'no "n = 12" label');
    assert.equal(count.children[0].getAttribute('font-style'), 'italic');
    assert.equal(count.children[0].textContent, 'n');
  });
});

describe('figure export', () => {
  const outcomes = {
    form: 'bars',
    total: 20,
    items: [
      { label: 'CORRECT', value: 12, share: 0.6 },
      { label: 'FIX_BREAK', value: 6, share: 0.3 },
      { label: 'NO_RESPONSE', value: 2, share: 0.1 },
    ],
  };

  /* A built panel as exportFigure receives it: its data, title and letter. */
  function panel(letter) {
    const letterNode = live_monitor.document.createElement('span');
    letterNode.textContent = letter;
    return { data: outcomes, letter: letterNode, title: 'Saccade landings' };
  }

  const root = () => live_monitor.document.documentElement;

  it('saves a single-column SVG 89 mm wide, drawn at 400 px across', async () => {
    draw('exportFigure')(panel('b'), 'svg', 'single');
    assert.equal(live_monitor.downloads.length, 1);
    const { filename, blob } = live_monitor.downloads[0];
    assert.equal(filename, 'b-saccade-landings-89mm.svg');
    const markup = await blob.text();
    assert.match(markup, /^<\?xml version="1\.0" encoding="UTF-8"\?>\n<svg /);
    const svg = /^[^\n]*\n(<svg [^>]*>)/.exec(markup)[1];
    assert.match(svg, / width="89mm"/);
    assert.match(svg, / viewBox="0 0 400 /);
  });

  it('saves a double-column SVG 183 mm wide at the same drawing scale', async () => {
    draw('exportFigure')(panel('c'), 'svg', 'double');
    const { filename, blob } = live_monitor.downloads[0];
    assert.equal(filename, 'c-saccade-landings-183mm.svg');
    const svg = /^[^\n]*\n(<svg [^>]*>)/.exec(await blob.text())[1];
    // 183 mm at 400 px per 89 mm is 822 px: text prints the same size.
    assert.match(svg, / width="183mm"/);
    assert.match(svg, / viewBox="0 0 822 /);
  });

  it('leaves the screen-only hover targets out of the file', async () => {
    draw('exportFigure')(panel('b'), 'svg', 'single');
    const markup = await live_monitor.downloads[0].blob.text();
    // The white ground and three bars; the three row hit targets are gone.
    assert.equal(markup.match(/<rect /g).length, 1 + 3);
    assert.doesNotMatch(markup, /class="hit"|data-screen-only/);
  });

  it('rasterises a PNG at 600 dpi', async () => {
    draw('exportFigure')(panel('b'), 'png', 'single');
    await settle();
    assert.equal(live_monitor.downloads.length, 1);
    const { filename, blob } = live_monitor.downloads[0];
    assert.equal(filename, 'b-saccade-landings-89mm-600dpi.png');
    // 89 mm is 3.504 inches; at 600 dpi that is 2102 pixels across.
    assert.equal(JSON.parse(await blob.text()).width, 2102);
  });

  it('draws in the light theme and figure proportions, then puts the page back', () => {
    root().setAttribute('data-theme', 'dark');
    const during = {};
    const drawBars = live_monitor.get('DRAW').bars;
    live_monitor.get('DRAW').bars = (legendHost, host, data) => {
      during.theme = root().getAttribute('data-theme');
      during.exportMode = live_monitor.get('exportMode');
      drawBars(legendHost, host, data);
    };

    draw('exportFigure')(panel('b'), 'svg', 'single');

    assert.deepEqual(during, { theme: 'light', exportMode: true });
    assert.equal(root().getAttribute('data-theme'), 'dark');
    assert.equal(live_monitor.get('exportMode'), false);
    assert.equal(live_monitor.document.querySelector('.export-stage'), null);
  });

  it('puts the page back and says so when the drawing fails', () => {
    // No data-theme is the "auto" theme, and it must come back as no attribute.
    assert.equal(root().getAttribute('data-theme'), null);
    live_monitor.get('DRAW').bars = () => {
      throw new Error('boom');
    };

    draw('exportFigure')(panel('b'), 'svg', 'single');

    assert.equal(root().getAttribute('data-theme'), null);
    assert.equal(live_monitor.get('exportMode'), false);
    assert.equal(live_monitor.document.querySelector('.export-stage'), null);
    assert.deepEqual(live_monitor.alerts, ['Figure export failed: boom']);
    assert.equal(live_monitor.consoleErrors.length, 1);
    assert.equal(live_monitor.downloads.length, 0);
  });
});
