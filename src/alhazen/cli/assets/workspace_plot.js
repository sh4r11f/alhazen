/* Alhazen experiment workspace — quick plots for the Data view.
 *
 * Two halves, kept apart so the maths is tested without a page:
 *
 *   build(kind, table, x, y, group)  pure: text cells in, a figure model out
 *                                    ({error} when the choice cannot be drawn)
 *   render(figure, document)         the model as an <svg> element
 *
 * The table's cells arrive as the CSV's own text (workspace_data.py sends
 * no types), so this file decides what a column is: `parseCell` reads a
 * number, "True"/"False" (how the trials file writes a boolean) as 1/0, and
 * anything else as text. A column is numeric when every non-empty cell is.
 * A numeric column whose values are all 0 or 1 is a proportion: its mean is
 * the fraction of 1s, and the axis says "proportion".
 *
 * Colours. Axes, grid and text use CSS classes that workspace_data.css
 * colours from the page's variables, so they follow the light/dark theme.
 * Series use a fixed Okabe-Ito order (the colour-vision-deficiency-safe set
 * the live monitor uses too), readable on light and dark surfaces, and each
 * series also gets its own marker shape and a legend entry, so identity is
 * never carried by colour alone. More groups than colours is refused with a
 * reason rather than drawn with repeated colours.
 *
 * No dependencies and no network: the page's CSP allows scripts from itself
 * only.
 */
'use strict';

const WorkspacePlot = (() => {
  const SVG_NS = 'http://www.w3.org/2000/svg';
  /* Okabe-Ito, in the live monitor's order (blue, vermillion, green), then
   * reddish purple and orange. Assigned in this order, never cycled. */
  const PALETTE = ['#0072b2', '#d55e00', '#009e73', '#cc79a7', '#e69f00'];
  const MARKERS = ['circle', 'square', 'triangle', 'diamond', 'cross'];
  /* A categorical x with more values than this is unreadable as a band. */
  const MAX_CATEGORIES = 30;
  /* Scatter points drawn at most; past it the figure says how many were. */
  const MAX_POINTS = 20000;
  /* The drawing's own coordinate system; the <svg> scales to its box. */
  const WIDTH = 720;
  const HEIGHT = 420;
  const MARGIN = {left: 70, right: 20, top: 20, bottom: 64};

  /** A cell as a number, or null when empty, or NaN when it is text. */
  function parseCell(text) {
    const value = String(text ?? '').trim();
    if (value === '') return null;
    if (value === 'True' || value === 'true') return 1;
    if (value === 'False' || value === 'false') return 0;
    // Number('') is 0 and Number(' ') too, so emptiness is decided above;
    // '0x10' and 'Infinity' are not measurements.
    if (!/^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(value)) return NaN;
    return Number(value);
  }

  /** What a column holds: 'empty', 'numeric' (with `binary` when every
   *  value is 0 or 1) or 'text'. */
  function columnType(cells) {
    let seen = 0;
    let binary = true;
    for (const cell of cells) {
      const value = parseCell(cell);
      if (value === null) continue;
      if (Number.isNaN(value)) return {type: 'text', binary: false};
      seen += 1;
      if (value !== 0 && value !== 1) binary = false;
    }
    return seen ? {type: 'numeric', binary} : {type: 'empty', binary: false};
  }

  /** 1, 2 or 5 times a power of ten, the smallest not below `raw`; with
   *  `fine`, 2.5 too (a histogram's bin width, which would otherwise often
   *  double and halve the number of bins asked for). */
  function niceStep(raw, fine = false) {
    if (!(raw > 0) || !Number.isFinite(raw)) return 1;
    const power = 10 ** Math.floor(Math.log10(raw));
    const fraction = raw / power;
    const choices = fine ? [1, 2, 2.5, 5, 10] : [1, 2, 5, 10];
    return choices.find((nice) => fraction <= nice + 1e-9) * power;
  }

  /** Round tick values covering [min, max], about `target` of them. The
   *  domain is widened to the outer ticks, so data never sits past an axis
   *  end. A zero-width range gets a little room either side. `integer`
   *  keeps the step at 1 or more (for counts). */
  function niceTicks(min, max, target = 5, integer = false) {
    if (!Number.isFinite(min) || !Number.isFinite(max)) return [0, 1];
    if (min === max) {
      const pad = min === 0 ? 1 : Math.abs(min) * 0.1;
      return niceTicks(min - pad, max + pad, target, integer);
    }
    // Counts are whole numbers: a histogram's axis never says 0.5.
    const step = integer ? Math.max(1, niceStep((max - min) / target))
      : niceStep((max - min) / target);
    const first = Math.floor(min / step) * step;
    const ticks = [];
    // Counted in steps, not accumulated, so 0.1 + 0.2 never drifts into
    // 0.30000000000000004 on an axis label.
    for (let i = 0; first + i * step <= max + step * 1e-9 || ticks.length < 2; i++) {
      ticks.push(Number((first + i * step).toPrecision(12)));
    }
    if (ticks[ticks.length - 1] < max) ticks.push(Number((first + ticks.length * step).toPrecision(12)));
    return ticks;
  }

  /** A tick label with as many decimals as the step needs. */
  function formatTick(value, step) {
    const decimals = Math.max(0, Math.min(10, -Math.floor(Math.log10(step) + 1e-9)));
    return value.toFixed(decimals);
  }

  /** Mean, standard error of the mean and count of some numbers. SEM is
   *  the sample SD (n − 1) over √n; null for a single value, which has no
   *  spread to estimate. */
  function meanSem(values) {
    const n = values.length;
    if (!n) return {mean: null, sem: null, n: 0};
    const mean = values.reduce((a, b) => a + b, 0) / n;
    if (n < 2) return {mean, sem: null, n};
    const variance = values.reduce((a, b) => a + (b - mean) ** 2, 0) / (n - 1);
    return {mean, sem: Math.sqrt(variance / n), n};
  }

  /** Histogram edges and counts. Sturges' number of bins (⌈log2 n⌉ + 1),
   *  rounded to a nice width and aligned to it; `edges` has one more entry
   *  than `counts`. A value on an inner edge counts in the bin above it;
   *  the maximum counts in the last bin. */
  function histogram(values, edgesGiven) {
    const finite = values.filter(Number.isFinite);
    if (!finite.length) return {edges: [], counts: []};
    let edges = edgesGiven;
    if (!edges) {
      const min = Math.min(...finite);
      const max = Math.max(...finite);
      if (min === max) {
        edges = [min - 0.5, min + 0.5];
      } else {
        const bins = Math.ceil(Math.log2(finite.length)) + 1;
        const step = niceStep((max - min) / bins, true);
        const start = Math.floor(min / step) * step;
        edges = [start];
        // The maximum may sit on the last edge; it counts in the last bin.
        while (edges[edges.length - 1] < max) {
          edges.push(Number((start + edges.length * step).toPrecision(12)));
        }
      }
    }
    const counts = new Array(edges.length - 1).fill(0);
    for (const value of finite) {
      let bin = edges.findIndex((edge, i) => i < edges.length - 1 && value < edges[i + 1]);
      if (bin === -1) bin = value === edges[edges.length - 1] ? counts.length - 1 : -1;
      if (bin >= 0 && value >= edges[0]) counts[bin] += 1;
    }
    return {edges, counts};
  }

  /** Compare two category labels: numbers by value, text naturally. */
  function compareLabels(a, b) {
    return String(a).localeCompare(String(b), undefined, {numeric: true});
  }

  /** The rows' groups in order, each with its colour and marker, or an
   *  error when there are more than the palette can tell apart. */
  function groupsOf(rows, groupIndex) {
    if (groupIndex < 0) return {names: [''], of: () => ''};
    const names = [...new Set(rows.map((r) => r[groupIndex]))].sort(compareLabels);
    if (names.length > PALETTE.length) {
      return {
        error: `The group-by column has ${names.length} values; at most ${PALETTE.length} ` +
          'can be told apart. Filter the table first, or group by another column.',
      };
    }
    return {names, of: (row) => row[groupIndex]};
  }

  function styleOf(index) {
    return {color: PALETTE[index], marker: MARKERS[index]};
  }

  /**
   * Turn a table and the reader's choices into a figure model.
   *
   * table   {columns: [...], rows: [[text, ...], ...]} — the rows the view
   *         shows (already filtered)
   * kind    'mean' (mean ± SEM per x value), 'scatter', 'hist-y', 'hist-x'
   * x, y    column names; group: a column name or '' for none
   *
   * Returns {figure} or {error}: every refusal is a sentence for the page.
   */
  function build(kind, table, x, y, group) {
    const index = (name) => table.columns.indexOf(name);
    const xi = index(x);
    const yi = index(y);
    const gi = group ? index(group) : -1;
    const histogramOf = kind === 'hist-x' ? x : kind === 'hist-y' ? y : null;
    if (group && gi < 0) return {error: `No column ${group} in this table.`};
    const rows = table.rows;
    if (!rows.length) return {error: 'The table has no rows to plot (check the filter).'};
    const groups = groupsOf(rows, gi);
    if (groups.error) return {error: groups.error};

    if (histogramOf !== null) {
      const hi = index(histogramOf);
      if (hi < 0) return {error: `Choose a column for the histogram's ${kind === 'hist-x' ? 'x' : 'y'}.`};
      const type = columnType(rows.map((r) => r[hi]));
      if (type.type !== 'numeric') {
        return {error: `${histogramOf} is not numeric, so it has no histogram; count it by ` +
          'choosing it as x of a mean ± SEM plot instead.'};
      }
      const all = rows.map((r) => parseCell(r[hi])).filter((v) => v !== null);
      const {edges} = histogram(all);
      const series = groups.names.map((name, i) => {
        const values = rows.filter((r) => groups.of(r) === name)
          .map((r) => parseCell(r[hi])).filter((v) => v !== null);
        return {name, ...styleOf(i), counts: histogram(values, edges).counts, n: values.length};
      });
      const top = Math.max(...series.flatMap((s) => s.counts));
      return {figure: {
        kind: 'histogram', xLabel: histogramOf, yLabel: 'count',
        x: {type: 'linear', ticks: niceTicks(edges[0], edges[edges.length - 1])},
        y: {ticks: niceTicks(0, top, 5, true)}, edges, series, legend: gi >= 0, notes: [],
      }};
    }

    if (xi < 0) return {error: 'Choose a column for x.'};
    if (yi < 0) return {error: 'Choose a column for y.'};
    const yType = columnType(rows.map((r) => r[yi]));
    if (yType.type !== 'numeric') {
      return {error: `${y} holds text, so it cannot be y. Text columns can be x or the group-by; ` +
        'y must be numbers (or True/False, plotted as a proportion).'};
    }
    const xType = columnType(rows.map((r) => r[xi]));
    const numericX = xType.type === 'numeric';
    const notes = [];
    let categories = null;
    if (!numericX) {
      categories = [...new Set(rows.map((r) => r[xi]))].sort(compareLabels);
      if (categories.length > MAX_CATEGORIES) {
        return {error: `${x} has ${categories.length} different text values; at most ` +
          `${MAX_CATEGORIES} fit on an axis. Choose a numeric x, or filter the table.`};
      }
    }
    // Rows with an empty y (or, for a numeric x, an empty x) have nothing
    // to place; they are counted and said, never dropped silently.
    const usable = rows.filter((r) => parseCell(r[yi]) !== null &&
      (!numericX || parseCell(r[xi]) !== null));
    if (usable.length < rows.length) {
      notes.push(`${rows.length - usable.length} of ${rows.length} rows have an empty ` +
        `${numericX ? 'x or y' : 'y'} and are left out.`);
    }
    if (!usable.length) return {error: `No row has a value for ${y}.`};
    const xOf = (r) => (numericX ? parseCell(r[xi]) : r[xi]);
    const yLabel = kind === 'mean' ? (yType.binary ? `${y} (proportion)` : `${y} (mean ± SEM)`) : y;

    let series;
    if (kind === 'mean') {
      series = groups.names.map((name, i) => {
        const buckets = new Map();
        for (const r of usable) {
          if (groups.of(r) !== name) continue;
          const key = xOf(r);
          if (!buckets.has(key)) buckets.set(key, []);
          buckets.get(key).push(parseCell(r[yi]));
        }
        const keys = [...buckets.keys()].sort(numericX ? (a, b) => a - b : compareLabels);
        const points = keys.map((key) => {
          const stats = meanSem(buckets.get(key));
          return {x: key, y: stats.mean, sem: stats.sem, n: stats.n};
        });
        return {name, ...styleOf(i), points};
      });
    } else if (kind === 'scatter') {
      let drawn = 0;
      series = groups.names.map((name, i) => {
        const points = [];
        for (const r of usable) {
          if (groups.of(r) !== name) continue;
          if (drawn >= MAX_POINTS) break;
          points.push({x: xOf(r), y: parseCell(r[yi])});
          drawn += 1;
        }
        return {name, ...styleOf(i), points};
      });
      if (usable.length > drawn) notes.push(`The first ${drawn} of ${usable.length} points are drawn.`);
    } else {
      return {error: `Unknown plot kind ${kind}.`};
    }
    const ys = series.flatMap((s) => s.points.flatMap((p) =>
      p.sem === null || p.sem === undefined ? [p.y] : [p.y - p.sem, p.y + p.sem]));
    const yTicks = yType.binary && kind === 'mean'
      ? niceTicks(Math.min(0, ...ys), Math.max(1, ...ys))
      : niceTicks(Math.min(...ys), Math.max(...ys));
    const xAxis = numericX
      ? {type: 'linear', ticks: niceTicks(...extent(series.flatMap((s) => s.points.map((p) => p.x))))}
      : {type: 'band', categories};
    return {figure: {
      kind, xLabel: x, yLabel, x: xAxis, y: {ticks: yTicks}, series, legend: gi >= 0, notes,
      connect: kind === 'mean' && numericX,
    }};
  }

  function extent(values) {
    return [Math.min(...values), Math.max(...values)];
  }

  /* ---------------------------------------------------------------- */
  /* Rendering                                                         */
  /* ---------------------------------------------------------------- */

  function el(document, tag, attributes, text) {
    const element = document.createElementNS(SVG_NS, tag);
    for (const [name, value] of Object.entries(attributes || {})) {
      element.setAttribute(name, String(value));
    }
    if (text !== undefined) element.textContent = text;
    return element;
  }

  /** A label cut to `max` characters with an ellipsis (the whole goes in
   *  a <title> where it matters). */
  function shorten(text, max) {
    const value = String(text);
    return value.length > max ? value.slice(0, max - 1) + '…' : value;
  }

  /** A marker of `shape` centred on (x, y), in `color`. */
  function marker(document, shape, x, y, color, size = 4.5) {
    const s = size;
    const common = {fill: color, stroke: color, class: 'plot-mark'};
    if (shape === 'square') {
      return el(document, 'rect', {...common, x: x - s, y: y - s, width: 2 * s, height: 2 * s});
    }
    if (shape === 'triangle') {
      return el(document, 'path', {...common, d: `M${x},${y - s * 1.2}L${x + s * 1.1},${y + s * 0.9}L${x - s * 1.1},${y + s * 0.9}Z`});
    }
    if (shape === 'diamond') {
      return el(document, 'path', {...common, d: `M${x},${y - s * 1.3}L${x + s * 1.3},${y}L${x},${y + s * 1.3}L${x - s * 1.3},${y}Z`});
    }
    if (shape === 'cross') {
      return el(document, 'path', {
        fill: 'none', stroke: color, 'stroke-width': 2.2, class: 'plot-mark',
        d: `M${x - s},${y - s}L${x + s},${y + s}M${x - s},${y + s}L${x + s},${y - s}`,
      });
    }
    return el(document, 'circle', {...common, cx: x, cy: y, r: s});
  }

  /**
   * The figure as an <svg>: grid, axes with ticks and labels, the marks,
   * the legend, each mark carrying a <title> the browser shows on hover.
   */
  function render(figure, document) {
    const svg = el(document, 'svg', {
      xmlns: SVG_NS, viewBox: `0 0 ${WIDTH} ${HEIGHT}`, class: 'plot', role: 'img',
      'aria-label': `${figure.yLabel} by ${figure.xLabel}`,
    });
    svg.appendChild(el(document, 'rect', {class: 'plot-bg', x: 0, y: 0, width: WIDTH, height: HEIGHT}));
    const left = MARGIN.left;
    // With a legend, the plot area stops short of it: a legend inside the
    // axes would sit on top of whatever data falls in its corner.
    const LEGEND_WIDTH = 150;
    const right = WIDTH - MARGIN.right - (figure.legend ? LEGEND_WIDTH + 10 : 0);
    const top = MARGIN.top;
    const bottom = HEIGHT - MARGIN.bottom;
    const yTicks = figure.y.ticks;
    const y0 = yTicks[0];
    const y1 = yTicks[yTicks.length - 1];
    const sy = (v) => bottom - ((v - y0) / (y1 - y0)) * (bottom - top);
    let sx;
    let xTickList;
    let band = 0;
    if (figure.x.type === 'band') {
      const n = figure.x.categories.length;
      band = (right - left) / n;
      const where = new Map(figure.x.categories.map((c, i) => [c, left + band * (i + 0.5)]));
      sx = (v) => where.get(v);
      xTickList = figure.x.categories.map((c) => ({value: c, at: where.get(c), label: c}));
    } else {
      const ticks = figure.x.ticks;
      const x0 = ticks[0];
      const x1 = ticks[ticks.length - 1];
      sx = (v) => left + ((v - x0) / (x1 - x0)) * (right - left);
      const step = ticks.length > 1 ? ticks[1] - ticks[0] : 1;
      xTickList = ticks.map((t) => ({value: t, at: sx(t), label: formatTick(t, step)}));
    }
    // Grid and y axis.
    const yStep = yTicks.length > 1 ? yTicks[1] - yTicks[0] : 1;
    for (const tick of yTicks) {
      svg.appendChild(el(document, 'line', {class: 'plot-grid', x1: left, x2: right, y1: sy(tick), y2: sy(tick)}));
      svg.appendChild(el(document, 'text', {class: 'plot-tick', x: left - 8, y: sy(tick) + 4, 'text-anchor': 'end'},
        formatTick(tick, yStep)));
    }
    svg.appendChild(el(document, 'line', {class: 'plot-axis', x1: left, x2: left, y1: top, y2: bottom}));
    svg.appendChild(el(document, 'line', {class: 'plot-axis', x1: left, x2: right, y1: bottom, y2: bottom}));
    // x ticks; long category labels are cut, with the whole in a <title>.
    const slanted = figure.x.type === 'band' && xTickList.length > 8;
    for (const tick of xTickList) {
      svg.appendChild(el(document, 'line', {class: 'plot-axis', x1: tick.at, x2: tick.at, y1: bottom, y2: bottom + 5}));
      const label = String(tick.label);
      const short = shorten(label, 14);
      const text = el(document, 'text', slanted
        ? {class: 'plot-tick', x: tick.at, y: bottom + 16, 'text-anchor': 'end',
          transform: `rotate(-35 ${tick.at} ${bottom + 16})`}
        : {class: 'plot-tick', x: tick.at, y: bottom + 19, 'text-anchor': 'middle'}, short);
      if (short !== label) text.appendChild(el(document, 'title', {}, label));
      svg.appendChild(text);
    }
    svg.appendChild(el(document, 'text', {class: 'plot-label', x: (left + right) / 2, y: HEIGHT - 10, 'text-anchor': 'middle'},
      figure.xLabel));
    svg.appendChild(el(document, 'text', {class: 'plot-label', x: 16, y: (top + bottom) / 2, 'text-anchor': 'middle',
      transform: `rotate(-90 16 ${(top + bottom) / 2})`}, figure.yLabel));

    const marks = el(document, 'g', {class: 'plot-marks'});
    svg.appendChild(marks);
    if (figure.kind === 'histogram') {
      const edges = figure.edges;
      const bars = figure.series.length === 1;
      figure.series.forEach((series) => {
        if (bars) {
          series.counts.forEach((count, i) => {
            if (!count) return;
            const x0 = sx(edges[i]) + 1;
            const x1 = sx(edges[i + 1]) - 1;
            const rect = el(document, 'rect', {
              class: 'plot-bar', fill: series.color, x: x0, y: sy(count),
              width: Math.max(1, x1 - x0), height: Math.max(0, bottom - sy(count)),
            });
            rect.appendChild(el(document, 'title', {}, `${edges[i]} to ${edges[i + 1]}: ${count}`));
            marks.appendChild(rect);
          });
        } else {
          // Several groups overlaid as step outlines, so none hides another.
          let d = `M${sx(edges[0])},${bottom}`;
          series.counts.forEach((count, i) => {
            d += `L${sx(edges[i])},${sy(count)}L${sx(edges[i + 1])},${sy(count)}`;
          });
          d += `L${sx(edges[edges.length - 1])},${bottom}`;
          const path = el(document, 'path', {class: 'plot-line', d, fill: 'none', stroke: series.color, 'stroke-width': 2});
          path.appendChild(el(document, 'title', {}, `${series.name}: ${series.n} values`));
          marks.appendChild(path);
        }
      });
    } else {
      const count = figure.series.length;
      figure.series.forEach((series, index) => {
        // Groups side by side within a category, so error bars do not overlap.
        const dodge = figure.x.type === 'band' && count > 1
          ? (index - (count - 1) / 2) * Math.min(12, band / (count + 1)) : 0;
        const place = (p) => [sx(p.x) + dodge, sy(p.y)];
        if (figure.connect && series.points.length > 1) {
          const d = series.points.map((p, i) => `${i ? 'L' : 'M'}${place(p).join(',')}`).join('');
          marks.appendChild(el(document, 'path', {class: 'plot-line', d, fill: 'none', stroke: series.color, 'stroke-width': 2}));
        }
        for (const p of series.points) {
          const [cx, cy] = place(p);
          const group = el(document, 'g', {});
          if (p.sem !== null && p.sem !== undefined) {
            const hi = sy(p.y + p.sem);
            const lo = sy(p.y - p.sem);
            group.appendChild(el(document, 'path', {
              class: 'plot-errorbar', stroke: series.color, 'stroke-width': 1.5, fill: 'none',
              d: `M${cx},${hi}L${cx},${lo}M${cx - 4},${hi}L${cx + 4},${hi}M${cx - 4},${lo}L${cx + 4},${lo}`,
            }));
          }
          group.appendChild(marker(document, series.marker, cx, cy, series.color, figure.kind === 'scatter' ? 3 : 4.5));
          const who = series.name !== '' ? `${series.name} · ` : '';
          const what = figure.kind === 'mean'
            ? `${p.y.toPrecision(4)}${p.sem !== null ? ` ± ${p.sem.toPrecision(3)}` : ''} (n = ${p.n})`
            : String(p.y);
          group.appendChild(el(document, 'title', {}, `${who}${figure.xLabel} ${p.x}: ${what}`));
          marks.appendChild(group);
        }
      });
    }
    if (figure.legend) {
      const legend = el(document, 'g', {class: 'plot-legend'});
      // Right of the axes, with a panel so it reads as one block.
      const x0 = right + 14;
      legend.appendChild(el(document, 'rect', {
        class: 'plot-legend-bg', x: x0, y: top - 6, width: LEGEND_WIDTH,
        height: 18 * figure.series.length + 6,
      }));
      let at = top + 6;
      for (const series of figure.series) {
        const itemBox = el(document, 'g', {});
        itemBox.appendChild(marker(document, series.marker, x0 + 12, at, series.color));
        itemBox.appendChild(el(document, 'text', {class: 'plot-legend-text', x: x0 + 24, y: at + 4},
          shorten(series.name === '' ? '(empty)' : series.name, 16)));
        legend.appendChild(itemBox);
        at += 18;
      }
      svg.appendChild(legend);
    }
    return svg;
  }

  return {
    PALETTE, MAX_CATEGORIES, MAX_POINTS,
    parseCell, columnType, niceStep, niceTicks, formatTick, meanSem, histogram, build, render,
  };
})();
