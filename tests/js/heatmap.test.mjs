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
import { hostFor, loadLiveMonitor } from './load_live_monitor.mjs';

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
