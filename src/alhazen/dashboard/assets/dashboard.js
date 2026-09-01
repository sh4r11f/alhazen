/* Alhazen live dashboard — the renderer.
 *
 * This file draws; it does not analyse. Every count, bin edge, mean, error
 * bar and running proportion arrives precomputed in `panel.data` from
 * alhazen/dashboard/panels.py, which is where those statistics are tested.
 * What lives here is the part that has to be here: scales, axes, marks,
 * hover, and the table view that makes every plotted value readable as text.
 *
 * Drawing conventions, applied by every chart below:
 *   - 1 SVG unit = 1 CSS pixel (width/height attributes, no viewBox), so an
 *     11px label is 11px in a narrow panel and in a wide one.
 *   - spines and outward tick marks, no gridlines: a printed figure's
 *     conventions, so the ink in a plot is the data.
 *   - 2px lines, markers of at least r=4 carrying a 2px ring in the surface
 *     colour, bars capped at 24px thick with a 2px gap between neighbours.
 *   - labels wear text tokens, never a series colour; identity comes from the
 *     coloured mark beside the text.
 *   - values are direct-labelled selectively (endpoints and extremes) and are
 *     always reachable in full from the hover readout and the table view.
 */

'use strict';

const SVG = 'http://www.w3.org/2000/svg';

/* Plot geometry. `bottom` has room for a tick row and an axis title; the card
 * grows to contain it rather than clipping it into a nested scrollbar. */
const PAD = { top: 16, right: 22, bottom: 52, left: 52 };

/**
 * One plot height for every panel on the page.
 *
 * Deliberately not per-chart: panels sit two to a row, and a row whose plots
 * begin and end on the same lines reads as a figure plate rather than as a
 * pile. It scales with the width they share, so the drawing area keeps a
 * readable aspect on any screen without the panels drifting out of step.
 */
function plotHeight(width) {
  return Math.max(210, Math.min(340, Math.round(width * 0.5)));
}

/* Spatial plots are square by nature — both axes carry the same quantity —
 * but they take the same box as everything else and centre inside it: a card
 * taller than its neighbour costs more than a little air does. */
const squarePlotHeight = plotHeight;

/** The height of the whole drawing, ticks and axis title included. Every
 *  chart uses it, including the ones with no axis to label, so two panels
 *  side by side end on the same line rather than nearly so. */
function chartHeight(width) {
  return PAD.top + plotHeight(width) + PAD.bottom;
}

const SANS = '"Helvetica Neue", Helvetica, Arial, "Liberation Sans", system-ui, sans-serif';
const TICK_FONT = '11.5px ' + SANS;
const LABEL_FONT = '12px ' + SANS;

/* ------------------------------------------------------------------ */
/* Small helpers                                                       */
/* ------------------------------------------------------------------ */

const byId = (id) => document.getElementById(id);

/** Create an SVG element and set attributes in one call. */
function svgEl(name, attrs, parent) {
  const node = document.createElementNS(SVG, name);
  for (const key in attrs) {
    if (attrs[key] !== null && attrs[key] !== undefined) node.setAttribute(key, attrs[key]);
  }
  if (parent) parent.appendChild(node);
  return node;
}

/** Create an HTML element; `text` is inserted as text, never as markup —
 *  outcome names and response keys are experiment-authored strings. */
function htmlEl(name, className, text, parent) {
  const node = document.createElement(name);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (parent) parent.appendChild(node);
  return node;
}

/** A CSS custom property reference, so marks follow the theme with no
 *  re-render when the reader flips it. */
const slotColor = (slot) => 'var(--series-' + (((slot || 1) - 1) % 3 + 1) + ')';

/** One hue, light to dark, for condition levels that have an order — 0.05
 *  really is less than 0.4, and the reader should see that in the colour. */
const rampColor = (step) => 'var(--ramp-' + (Math.min(4, Math.max(0, step || 0)) + 1) + ')';

/**
 * The colour of one drawn series. `ramp` means an ordered level, `slot` an
 * unordered one, and `muted` the folded tail — grey, because "several levels
 * at once" is not a level and must not look like one.
 */
function seriesColor(series) {
  if (series.muted) return 'var(--muted)';
  return series.ramp === undefined ? slotColor(series.slot) : rampColor(series.ramp);
}

/** As many decimals as a number deserves — the same rule the Python side
 *  uses for the KPI strip, so the two never disagree on screen. */
function fmt(value) {
  if (!isFinite(value)) return '—';
  const magnitude = Math.abs(value);
  if (magnitude >= 1000) return value.toLocaleString(undefined, { maximumFractionDigits: 0 });
  if (magnitude >= 100) return value.toFixed(0);
  if (magnitude >= 10) return value.toFixed(1);
  if (magnitude >= 1) return value.toFixed(2);
  if (magnitude === 0) return '0';
  return value.toPrecision(3);
}

/* Text width without laying anything out: bar charts need to know how wide a
 * label column must be *before* they can choose the plot width. */
const measurer = document.createElement('canvas').getContext('2d');
function textWidth(text, font) {
  measurer.font = font || TICK_FONT;
  return measurer.measureText(String(text)).width;
}

/**
 * Shorten a label until it fits, with an ellipsis. A category name wider than
 * its column would otherwise be cropped by the SVG's own edge, which eats the
 * first or last characters and is worse than saying nothing. The full name
 * stays in the hover readout and the table view.
 */
function ellipsize(text, maxWidth, font) {
  const full = String(text);
  if (textWidth(full, font) <= maxWidth) return full;
  let cut = full.length;
  while (cut > 1 && textWidth(full.slice(0, cut) + '…', font) > maxWidth) cut -= 1;
  return full.slice(0, cut) + '…';
}

/**
 * Tick values a human reads: multiples of 1, 2, 2.5 or 5 times a power of ten
 * covering [lo, hi]. `integer` forces a whole-number step, which is what a
 * trial axis needs — there is no trial 7.5.
 */
function niceTicks(lo, hi, target, integer) {
  if (!(hi > lo)) return [lo];
  const raw = (hi - lo) / Math.max(1, target);
  const magnitude = Math.pow(10, Math.floor(Math.log10(raw)));
  const normalised = raw / magnitude;
  let step = (normalised <= 1 ? 1 : normalised <= 2 ? 2 : normalised <= 2.5 ? 2.5 : normalised <= 5 ? 5 : 10) * magnitude;
  if (integer) step = Math.max(1, Math.round(step));
  const ticks = [];
  const first = Math.ceil(lo / step - 1e-9) * step;
  for (let value = first; value <= hi + step * 1e-9; value += step) {
    ticks.push(Math.abs(value) < step * 1e-9 ? 0 : Number(value.toFixed(10)));
  }
  return ticks;
}

/**
 * How many decimals it takes to write `step` exactly. Deriving this from
 * log10 is off by one on the quarter steps a 0–1 axis is full of: it renders
 * 0.25 as "0.3", so an axis reads 0.0 / 0.3 / 0.5 / 0.8 / 1.0.
 */
function decimalsFor(step) {
  const magnitude = Math.abs(step);
  if (!isFinite(magnitude) || magnitude === 0) return 0;
  for (let places = 0; places <= 6; places += 1) {
    if (Math.abs(Number(magnitude.toFixed(places)) - magnitude) < magnitude * 1e-9) return places;
  }
  return 6;
}

/** Tick text with the decimals the step implies — never 0.30000000000000004. */
function tickText(value, ticks) {
  const step = ticks.length > 1 ? Math.abs(ticks[1] - ticks[0]) : Math.abs(value) || 1;
  if (Math.abs(value) >= 10000) return value.toLocaleString(undefined, { maximumFractionDigits: 0 });
  return value.toFixed(decimalsFor(step));
}

/* ------------------------------------------------------------------ */
/* Shared chart chrome                                                 */
/* ------------------------------------------------------------------ */

/**
 * Draw the frame every rectangular chart shares: horizontal gridlines at the
 * y ticks, a baseline, tick labels, and the two axis titles. Returns nothing;
 * the caller already holds the scales.
 */
function drawFrame(svg, box, opts) {
  const { x0, x1, y0, y1 } = box;
  /* Two spines and outward tick marks — the reading conventions of a printed
   * figure. No gridlines: they are a screen habit that fills a plot with ink
   * that is not data, and every value one would have carried is on a tick, on
   * a direct label, in the hover readout and in the table view. */
  (opts.yTicks || []).forEach((value) => {
    const y = opts.yScale(value);
    svgEl('line', { x1: x0 - 5, x2: x0, y1: y, y2: y, class: 'tick-mark' }, svg);
    svgEl('text', {
      x: x0 - 10, y: y + 4, class: 'tick-text', 'text-anchor': 'end',
    }, svg).textContent = tickText(value, opts.yTicks);
  });
  (opts.xTicks || []).forEach((value) => {
    const x = opts.xScale(value);
    svgEl('line', { x1: x, x2: x, y1: y1, y2: y1 + 5, class: 'tick-mark' }, svg);
    svgEl('text', {
      x: x, y: y1 + 19, class: 'tick-text', 'text-anchor': 'middle',
    }, svg).textContent = tickText(value, opts.xTicks);
  });
  svgEl('line', { x1: x0, x2: x0, y1: y0, y2: y1, class: 'spine' }, svg);
  svgEl('line', { x1: x0, x2: x1, y1: y1, y2: y1, class: 'spine' }, svg);
  if (opts.xLabel) {
    svgEl('text', {
      x: (x0 + x1) / 2, y: y1 + 40, class: 'axis-text', 'text-anchor': 'middle',
    }, svg).textContent = opts.xLabel;
  }
  if (opts.yLabel) {
    const y = (y0 + y1) / 2;
    svgEl('text', {
      x: 13, y: y, class: 'axis-text', 'text-anchor': 'middle',
      transform: 'rotate(-90 13 ' + y + ')',
    }, svg).textContent = opts.yLabel;
  }
}

/** A marker that stays legible wherever it lands: the ring is surface-coloured
 *  so overlapping points separate without a stroke drawn around the data. */
function marker(svg, x, y, color, radius) {
  svgEl('circle', {
    cx: x, cy: y, r: (radius || 4) + 1,
    style: 'fill:var(--surface)',
  }, svg);
  svgEl('circle', { cx: x, cy: y, r: radius || 4, style: 'fill:' + color }, svg);
}

/**
 * A text node that stays readable wherever it lands. `paint-order: stroke`
 * draws a surface-coloured halo behind the glyphs, which is what keeps a ring
 * label legible inside a dense cloud of points — the alternative is a label
 * placed where there happens to be no data today.
 */
function haloText(svg, x, y, text, anchor) {
  const node = svgEl('text', {
    x: x, y: y, class: 'tick-text', 'text-anchor': anchor || 'middle',
    style: 'paint-order:stroke;stroke:var(--surface);stroke-width:3;stroke-linejoin:round',
  }, svg);
  node.textContent = text;
  return node;
}

/** Left margin wide enough for the widest y tick label plus its axis title. */
function leftPad(ticks) {
  let widest = 0;
  ticks.forEach((value) => { widest = Math.max(widest, textWidth(tickText(value, ticks))); });
  return Math.max(PAD.left, widest + 34);
}

/* ------------------------------------------------------------------ */
/* Hover readout                                                       */
/* ------------------------------------------------------------------ */

/** One tooltip per panel, created on demand and reused. */
function tipFor(host) {
  let tip = host.querySelector('.tip');
  if (!tip) tip = htmlEl('div', 'tip', null, host);
  return tip;
}

/**
 * Show a readout. `rows` is [{name, value, color}] — value first and bold,
 * because the reader already knows which series they are pointing at.
 */
function showTip(host, x, y, head, rows) {
  const tip = tipFor(host);
  tip.replaceChildren();
  if (head) htmlEl('div', 'tip-head', head, tip);
  rows.forEach((row) => {
    const line = htmlEl('div', 'tip-row', null, tip);
    if (row.color) htmlEl('i', 'tip-key', null, line).style.background = row.color;
    htmlEl('span', 'tip-val', row.value, line);
    if (row.name) htmlEl('span', 'tip-name', row.name, line);
  });
  tip.dataset.open = '1';
  /* Flip the tooltip to the other side of the pointer near the right edge, so
   * it is never clipped by the card. */
  const width = tip.offsetWidth || 120;
  const left = x + 14 + width > host.clientWidth ? x - width - 14 : x + 14;
  tip.style.left = Math.max(0, left) + 'px';
  tip.style.top = Math.max(0, y - 12) + 'px';
}

function hideTip(host) {
  const tip = host.querySelector('.tip');
  if (tip) tip.dataset.open = '0';
}

/* ------------------------------------------------------------------ */
/* Legend                                                              */
/* ------------------------------------------------------------------ */

/** A legend whenever two or more things are drawn — identity is never left to
 *  colour-matching alone. One series needs none: the title already names it. */
function drawLegend(parent, entries) {
  if (entries.length < 2) return;
  const legend = htmlEl('div', 'legend', null, parent);
  entries.forEach((entry) => {
    const item = htmlEl('span', null, null, legend);
    const swatch = htmlEl('i', entry.shape || 'line', null, item);
    if (entry.shape === 'ring') swatch.style.borderColor = entry.color;
    else swatch.style.background = entry.color;
    if (entry.shape === 'box') swatch.style.opacity = '0.28';
    htmlEl('span', null, entry.name, item);
  });
}

/* ------------------------------------------------------------------ */
/* Charts                                                              */
/* ------------------------------------------------------------------ */

/**
 * Lines over a trial axis: the cumulative reward curve, running performance,
 * and any `series` panel. Handles an optional confidence band, step
 * interpolation (a cumulative total jumps at a delivery, it does not drift
 * between them), and event marks such as a failed delivery.
 */
function drawLineChart(legendHost, host, data) {
  const series = (data.series || []).filter((s) => s.points && s.points.length);
  if (!series.length) return drawEmpty(host, 'No data yet');

  const width = host.clientWidth || 380;
  const points = series.flatMap((s) => s.points);
  let xLo = Math.min(...points.map((p) => p[0]));
  let xHi = Math.max(...points.map((p) => p[0]));
  if (xHi === xLo) { xLo -= 0.5; xHi += 0.5; }

  let yLo;
  let yHi;
  if (data.y_domain) {
    [yLo, yHi] = data.y_domain;
  } else {
    const values = points.map((p) => p[1])
      .concat((data.band ? data.band.points : []).flatMap((p) => [p[1], p[2]]));
    yLo = Math.min(...values);
    yHi = Math.max(...values);
    if (yHi === yLo) { yLo -= 0.5; yHi += 0.5; }
    const margin = (yHi - yLo) * 0.08;
    yLo -= margin;
    yHi += margin;
  }

  const yTicks = niceTicks(yLo, yHi, 4);
  const left = leftPad(yTicks);
  const plotH = plotHeight(width);
  const box = { x0: left, x1: width - PAD.right, y0: PAD.top, y1: PAD.top + plotH };
  const xScale = (v) => box.x0 + (v - xLo) / (xHi - xLo) * (box.x1 - box.x0);
  const yScale = (v) => box.y1 - (v - yLo) / (yHi - yLo) * (box.y1 - box.y0);
  const xTicks = niceTicks(xLo, xHi, Math.max(2, Math.floor((box.x1 - box.x0) / 78)), true);

  const svg = svgEl('svg', { width: width, height: chartHeight(width) }, host);
  drawFrame(svg, box, {
    xScale, yScale, xTicks, yTicks, xLabel: data.x_label, yLabel: data.y_label,
  });

  /* The interval first, so the line and its markers sit on top of it. */
  if (data.band && data.band.points.length > 1) {
    const upper = data.band.points.map((p) => xScale(p[0]) + ',' + yScale(p[2]));
    const lower = data.band.points.slice().reverse().map((p) => xScale(p[0]) + ',' + yScale(p[1]));
    svgEl('polygon', {
      points: upper.concat(lower).join(' '),
      style: 'fill:' + slotColor(data.band.slot) + ';fill-opacity:0.12',
    }, svg);
  }

  series.forEach((one) => {
    const color = slotColor(one.slot);
    const pixels = one.points.map((p) => [xScale(p[0]), yScale(p[1])]);
    if (one.line !== false) {
      let path = 'M' + pixels[0][0] + ',' + pixels[0][1];
      for (let i = 1; i < pixels.length; i += 1) {
        /* A step carries the previous level across to the new x before it
         * rises, so the curve never draws reward the subject did not get. */
        if (one.step) path += 'L' + pixels[i][0] + ',' + pixels[i - 1][1];
        path += 'L' + pixels[i][0] + ',' + pixels[i][1];
      }
      svgEl('path', {
        d: path,
        style: 'fill:none;stroke:' + color + ';stroke-width:2;stroke-linejoin:round;stroke-linecap:round',
      }, svg);
    }
    if (one.marker) {
      /* Raw per-trial values: small, semi-transparent, no ring — with a
       * hundred of them on screen the rings would merge into a white field. */
      pixels.forEach((p) => {
        svgEl('circle', {
          cx: p[0], cy: p[1], r: 2.6, style: 'fill:' + color + ';fill-opacity:0.55',
        }, svg);
      });
    }
    /* One direct label per series, at its end — where the reader's eye
     * already is, and where it cannot collide with the data. */
    const last = pixels[pixels.length - 1];
    marker(svg, last[0], last[1], color, 3.5);
    one._labelY = last[1];
  });

  /* Skip an end label that would sit on top of another one; the legend and the
   * hover readout still carry that series. */
  const placed = [];
  series.forEach((one) => {
    const y = one._labelY;
    if (placed.some((other) => Math.abs(other - y) < 13)) return;
    placed.push(y);
    const value = one.points[one.points.length - 1][1];
    const text = svgEl('text', {
      x: box.x1 - 2, y: y - 8, class: 'value-text', 'text-anchor': 'end',
    }, svg);
    text.textContent = fmt(value);
  });

  /* Failed deliveries: a status mark, with its meaning in the legend and the
   * hover readout — never colour alone. */
  (data.marks || []).forEach((mark) => {
    const x = xScale(mark.x);
    const y = yScale(mark.y);
    svgEl('circle', { cx: x, cy: y, r: 5.5, style: 'fill:var(--surface)' }, svg);
    svgEl('path', {
      d: 'M' + (x - 3.6) + ',' + (y - 3.6) + 'L' + (x + 3.6) + ',' + (y + 3.6) +
         'M' + (x + 3.6) + ',' + (y - 3.6) + 'L' + (x - 3.6) + ',' + (y + 3.6),
      style: 'stroke:var(--critical);stroke-width:1.8;stroke-linecap:round',
    }, svg);
  });

  const entries = series.map((one) => ({ name: one.name, color: slotColor(one.slot) }));
  if (data.band) entries.push({ name: data.band.name || '95% CI', color: slotColor(data.band.slot), shape: 'box' });
  if ((data.marks || []).length) entries.push({ name: 'delivery failed', color: 'var(--critical)', shape: 'dot' });
  drawLegend(legendHost, entries);

  /* Crosshair: the reader aims at a trial, never at a 2px line. */
  const hairline = svgEl('line', {
    x1: 0, x2: 0, y1: box.y0, y2: box.y1, class: 'hairline', opacity: 0,
  }, svg);
  const hit = svgEl('rect', {
    x: box.x0, y: box.y0, width: Math.max(1, box.x1 - box.x0), height: box.y1 - box.y0, class: 'hit',
  }, svg);
  const onMove = (event) => {
    const rect = svg.getBoundingClientRect();
    const px = event.clientX - rect.left;
    const primary = series[0];
    let best = 0;
    let bestGap = Infinity;
    primary.points.forEach((p, i) => {
      const gap = Math.abs(xScale(p[0]) - px);
      if (gap < bestGap) { bestGap = gap; best = i; }
    });
    const at = primary.points[best][0];
    hairline.setAttribute('x1', xScale(at));
    hairline.setAttribute('x2', xScale(at));
    hairline.setAttribute('opacity', 1);
    const rows = series.map((one) => {
      const near = one.points.reduce((a, b) => (Math.abs(b[0] - at) < Math.abs(a[0] - at) ? b : a));
      return { name: one.name, value: fmt(near[1]), color: slotColor(one.slot) };
    });
    showTip(host, xScale(at) , event.clientY - rect.top, (data.x_label || 'x') + ' ' + fmt(at), rows);
  };
  hit.addEventListener('pointermove', onMove);
  hit.addEventListener('pointerleave', () => {
    hairline.setAttribute('opacity', 0);
    hideTip(host);
  });
}

/**
 * Counts of a nominal column. Horizontal, because outcome and response names
 * are words: `BROKE_FIXATION` under a vertical bar either overlaps its
 * neighbour or gets rotated, and both are worse than reading it across.
 */
function valueLabel(item) {
  return item.value.toLocaleString() + ' · ' + (item.share * 100).toFixed(item.share < 0.1 ? 1 : 0) + '%';
}

function drawBars(legendHost, host, data) {
  const items = data.items || [];
  if (!items.length) return drawEmpty(host, 'No data yet');

  const width = host.clientWidth || 380;
  /* The same box every other panel gets, with the bars spread through it and
   * centred. A bar chart sized to its own content leaves its card as a strip
   * of plot above a field of nothing, and breaks the row it sits in. */
  const height = chartHeight(width);
  const band = Math.min(64, (height - PAD.bottom) / items.length);
  const top = (height - band * items.length) / 2;
  const thickness = Math.min(24, band - 14);
  const labelFont = LABEL_FONT;
  let labelWidth = 0;
  let valueWidth = 0;
  items.forEach((item) => {
    labelWidth = Math.max(labelWidth, textWidth(item.label, labelFont));
    valueWidth = Math.max(valueWidth, textWidth(valueLabel(item), labelFont));
  });
  /* A few pixels of slack: the canvas metric and the SVG's own layout can
   * disagree by a hair, and losing the trailing "%" to that is not a
   * rounding error the reader should have to notice. */
  valueWidth += 10;
  const labelCap = width * 0.42;
  labelWidth = Math.min(labelWidth + 10, labelCap);
  const x0 = labelWidth;
  const x1 = width - valueWidth - 12;
  const max = Math.max(...items.map((item) => item.value), 1);
  const scale = (v) => Math.max(0, (v / max) * Math.max(8, x1 - x0));

  const svg = svgEl('svg', { width: width, height: chartHeight(width) }, host);
  /* One hue for every bar: these categories have no order, so colouring them
   * by size would spend the identity channel re-encoding bar length. */
  const color = slotColor(1);

  items.forEach((item, index) => {
    const y = top + index * band + (band - thickness) / 2;
    const length = scale(item.value);
    const radius = Math.min(4, length);
    /* Rounded at the data end, square at the baseline. */
    svgEl('path', {
      d: 'M' + x0 + ',' + y +
         'H' + (x0 + length - radius) + 'Q' + (x0 + length) + ',' + y + ' ' + (x0 + length) + ',' + (y + radius) +
         'V' + (y + thickness - radius) + 'Q' + (x0 + length) + ',' + (y + thickness) + ' ' + (x0 + length - radius) + ',' + (y + thickness) +
         'H' + x0 + 'Z',
      style: 'fill:' + color,
    }, svg);
    svgEl('text', {
      x: x0 - 8, y: y + thickness / 2 + 4, class: 'value-text', 'text-anchor': 'end',
    }, svg).textContent = ellipsize(item.label, labelCap - 10, labelFont);
    svgEl('text', {
      x: x0 + length + 8, y: y + thickness / 2 + 4, class: 'value-text',
    }, svg).textContent = valueLabel(item);

    /* The hit target is the whole row, not the painted bar. */
    const hit = svgEl('rect', {
      x: 0, y: top + index * band, width: width, height: band, class: 'hit',
    }, svg);
    hit.addEventListener('pointermove', (event) => {
      const rect = svg.getBoundingClientRect();
      showTip(host, event.clientX - rect.left, event.clientY - rect.top, item.label, [
        { name: data.value_label || '', value: item.value.toLocaleString(), color: color },
        { name: 'of ' + data.total.toLocaleString(), value: (item.share * 100).toFixed(1) + '%' },
      ]);
    });
    hit.addEventListener('pointerleave', () => hideTip(host));
  });
  svgEl('line', {
    x1: x0, x2: x0, y1: top, y2: top + band * items.length, class: 'spine',
  }, svg);
}

/** A distribution. Bin edges arrive already chosen (Freedman–Diaconis, rounded
 *  to readable numbers); this draws them and marks the median. */
function drawHistogram(legendHost, host, data) {
  const bins = data.bins || [];
  if (!bins.length) return drawEmpty(host, 'No data yet');

  const width = host.clientWidth || 380;
  const xLo = bins[0].x0;
  const xHi = bins[bins.length - 1].x1;
  const yHi = Math.max(...bins.map((b) => b.count), 1);
  const yTicks = niceTicks(0, yHi, 4, true);
  /* The tick range IS this chart's domain, so it has to reach the tallest
   * bar: `niceTicks(0, 16)` stops at 15, and a 16-count bar would be drawn
   * above the top gridline and through the panel's title. */
  const step = yTicks.length > 1 ? yTicks[1] - yTicks[0] : yHi;
  while (yTicks[yTicks.length - 1] < yHi) yTicks.push(yTicks[yTicks.length - 1] + step);
  const left = leftPad(yTicks);
  const plotH = plotHeight(width);
  const box = { x0: left, x1: width - PAD.right, y0: PAD.top + 8, y1: PAD.top + plotH };
  const xScale = (v) => box.x0 + (v - xLo) / (xHi - xLo || 1) * (box.x1 - box.x0);
  const yScale = (v) => box.y1 - (v / (yTicks[yTicks.length - 1] || 1)) * (box.y1 - box.y0);
  const xTicks = niceTicks(xLo, xHi, Math.max(2, Math.floor((box.x1 - box.x0) / 74)));

  const svg = svgEl('svg', { width: width, height: chartHeight(width) }, host);
  drawFrame(svg, box, { xScale, yScale, xTicks, yTicks, xLabel: data.x_label, yLabel: data.y_label });

  const color = slotColor(1);
  bins.forEach((bin) => {
    if (!bin.count) return;
    /* A 2px gap in the surface colour separates neighbours — never a stroke
     * around the bar, which would add ink that is not data. */
    const x = xScale(bin.x0) + 1;
    const w = Math.max(1, xScale(bin.x1) - xScale(bin.x0) - 2);
    const y = yScale(bin.count);
    const h = box.y1 - y;
    const radius = Math.min(4, w / 2, h);
    svgEl('path', {
      d: 'M' + x + ',' + box.y1 + 'V' + (y + radius) +
         'Q' + x + ',' + y + ' ' + (x + radius) + ',' + y +
         'H' + (x + w - radius) + 'Q' + (x + w) + ',' + y + ' ' + (x + w) + ',' + (y + radius) +
         'V' + box.y1 + 'Z',
      style: 'fill:' + color,
    }, svg);
    const hit = svgEl('rect', { x: x - 1, y: box.y0, width: w + 2, height: box.y1 - box.y0, class: 'hit' }, svg);
    hit.addEventListener('pointermove', (event) => {
      const rect = svg.getBoundingClientRect();
      showTip(host, event.clientX - rect.left, event.clientY - rect.top,
        fmt(bin.x0) + ' – ' + fmt(bin.x1), [{ name: data.y_label, value: bin.count.toLocaleString(), color: color }]);
    });
    hit.addEventListener('pointerleave', () => hideTip(host));
  });

  /* The median, direct-labelled: the one number a reader takes off a
   * reaction-time histogram without measuring it. */
  if (isFinite(data.median)) {
    const x = xScale(data.median);
    svgEl('line', { x1: x, x2: x, y1: box.y0 - 2, y2: box.y1, class: 'rule' }, svg);
    const label = 'median ' + fmt(data.median);
    /* Above the bars, never across them; flipped inward where the median sits
     * near the right edge, so the text is never clipped by the card. */
    const flip = x + textWidth(label, TICK_FONT) + 8 > box.x1;
    svgEl('text', {
      x: flip ? x - 5 : x + 5, y: box.y0 - 4, class: 'value-text',
      'text-anchor': flip ? 'end' : 'start',
    }, svg).textContent = label;
  }
}

/**
 * A response in two spatial dimensions. Equal aspect is the whole point: a
 * degree right must be the same length on screen as a degree up, or the shape
 * of the scatter is a lie about where the subject looked.
 */
function drawScatter(legendHost, host, data) {
  const series = (data.series || []).filter((one) => one.points && one.points.length);
  if (!series.length) return drawEmpty(host, 'No data yet');
  const points = series.flatMap((one) => one.points);

  const width = host.clientWidth || 380;
  const all = points.concat(data.targets || []);
  let xLo = Math.min(...all.map((p) => p[0]));
  let xHi = Math.max(...all.map((p) => p[0]));
  let yLo = Math.min(...all.map((p) => p[1]));
  let yHi = Math.max(...all.map((p) => p[1]));
  const padX = (xHi - xLo) * 0.12 || 1;
  const padY = (yHi - yLo) * 0.12 || 1;
  xLo -= padX; xHi += padX; yLo -= padY; yHi += padY;

  const yTicks0 = niceTicks(yLo, yHi, 4);
  const left = leftPad(yTicks0);
  const plotH = squarePlotHeight(width);
  const box = { x0: left, x1: width - PAD.right, y0: PAD.top, y1: PAD.top + plotH };

  if (data.equal_aspect) {
    /* Grow the axis with the finer scale until both carry the same units per
     * pixel, keeping each range centred on its own data. */
    const perPixelX = (xHi - xLo) / Math.max(1, box.x1 - box.x0);
    const perPixelY = (yHi - yLo) / Math.max(1, box.y1 - box.y0);
    const unit = Math.max(perPixelX, perPixelY);
    const spanX = unit * (box.x1 - box.x0);
    const spanY = unit * (box.y1 - box.y0);
    const midX = (xLo + xHi) / 2;
    const midY = (yLo + yHi) / 2;
    xLo = midX - spanX / 2; xHi = midX + spanX / 2;
    yLo = midY - spanY / 2; yHi = midY + spanY / 2;
  }

  const xScale = (v) => box.x0 + (v - xLo) / (xHi - xLo) * (box.x1 - box.x0);
  const yScale = (v) => box.y1 - (v - yLo) / (yHi - yLo) * (box.y1 - box.y0);
  const yTicks = niceTicks(yLo, yHi, 4);
  const xTicks = niceTicks(xLo, xHi, Math.max(2, Math.floor((box.x1 - box.x0) / 74)));

  const svg = svgEl('svg', { width: width, height: chartHeight(width) }, host);
  drawFrame(svg, box, {
    xScale, yScale, xTicks, yTicks, xLabel: data.x_label, yLabel: data.y_label,
  });

  /* Screen centre, where a fixation point sits — a spatial plot without its
   * origin marked makes the reader count gridlines. */
  if (xLo < 0 && xHi > 0) svgEl('line', { x1: xScale(0), x2: xScale(0), y1: box.y0, y2: box.y1, class: 'rule' }, svg);
  if (yLo < 0 && yHi > 0) svgEl('line', { x1: box.x0, x2: box.x1, y1: yScale(0), y2: yScale(0), class: 'rule' }, svg);

  series.forEach((one) => {
    const color = seriesColor(one);
    one.points.forEach((point) => marker(svg, xScale(point[0]), yScale(point[1]), color, 4));
  });

  /* Drawn after the cloud, not under it: an open ring hides no data, and
   * beneath a dense cluster it may as well not be there. */
  (data.targets || []).forEach((target) => {
    const cx = xScale(target[0]);
    const cy = yScale(target[1]);
    svgEl('circle', {
      cx: cx, cy: cy, r: 9, style: 'fill:none;stroke:var(--surface);stroke-width:3',
    }, svg);
    svgEl('circle', {
      cx: cx, cy: cy, r: 9, style: 'fill:none;stroke:var(--ink-2);stroke-width:1.5',
    }, svg);
    svgEl('path', {
      d: 'M' + (cx - 3) + ',' + cy + 'H' + (cx + 3) + 'M' + cx + ',' + (cy - 3) + 'V' + (cy + 3),
      style: 'stroke:var(--ink-2);stroke-width:1.5',
    }, svg);
  });

  /* One mean per level, in that level's own colour. A single mean over every
   * point lands between a left cluster and a right one — where nothing did.
   * Shape and outline, not hue, are what separate a summary from a datum. */
  series.forEach((one) => {
    if (!one.centroid || !isFinite(one.centroid[0])) return;
    const mx = xScale(one.centroid[0]);
    const my = yScale(one.centroid[1]);
    const diamond = 'M' + mx + ',' + (my - 7) + 'L' + (mx + 7) + ',' + my +
                    'L' + mx + ',' + (my + 7) + 'L' + (mx - 7) + ',' + my + 'Z';
    svgEl('path', { d: diamond, style: 'fill:var(--surface);stroke:var(--surface);stroke-width:4' }, svg);
    svgEl('path', {
      d: diamond,
      style: 'fill:' + seriesColor(one) + ';stroke:var(--ink);stroke-width:1.5',
    }, svg);
  });

  drawLegend(legendHost, [
    ...series.map((one) => ({
      name: one.name || 'landing', color: seriesColor(one), shape: 'dot',
    })),
    /* Only when one was drawn: a legend entry for a mark that is not on the
     * plot sends the reader looking for it. */
    ...(series.some((one) => one.centroid)
      ? [{ name: 'mean landing', color: 'var(--ink-2)', shape: 'diamond' }]
      : []),
    ...((data.targets || []).length ? [{ name: 'target', color: 'var(--ink-2)', shape: 'ring' }] : []),
  ]);

  /* Nearest-point hover: a 8px dot is a pinpoint nobody hits reliably, so the
   * pointer only has to be closest. */
  const hit = svgEl('rect', {
    x: box.x0, y: box.y0, width: Math.max(1, box.x1 - box.x0), height: box.y1 - box.y0, class: 'hit',
  }, svg);
  hit.addEventListener('pointermove', (event) => {
    const rect = svg.getBoundingClientRect();
    const px = event.clientX - rect.left;
    const py = event.clientY - rect.top;
    let best = null;
    let bestGap = Infinity;
    series.forEach((one) => {
      one.points.forEach((point) => {
        const gap = Math.hypot(xScale(point[0]) - px, yScale(point[1]) - py);
        if (gap < bestGap) { bestGap = gap; best = { point: point, series: one }; }
      });
    });
    if (!best || bestGap > 40) return hideTip(host);
    const rows = [
      { name: data.x_label, value: fmt(best.point[0]), color: seriesColor(best.series) },
      { name: data.y_label, value: fmt(best.point[1]) },
    ];
    if (best.series.name) rows.push({ name: data.color_label || '', value: best.series.name });
    showTip(host, xScale(best.point[0]), yScale(best.point[1]), 'landing', rows);
  });
  hit.addEventListener('pointerleave', () => hideTip(host));
}

/**
 * Every response as a displacement from where the eye started — all trials on
 * one origin. The grid is polar (rings of constant amplitude) because that is
 * what this plot's two questions are: how far, and which way.
 */
function drawVectors(legendHost, host, data) {
  const series = (data.series || []).filter((one) => one.points && one.points.length);
  if (!series.length) return drawEmpty(host, 'No data yet');

  const width = host.clientWidth || 380;
  const plotH = squarePlotHeight(width);
  const box = { x0: PAD.left, x1: width - PAD.right, y0: PAD.top, y1: PAD.top + plotH };
  const cx = (box.x0 + box.x1) / 2;
  const cy = (box.y0 + box.y1) / 2;
  /* One radius in pixels for both axes: a degree left has to be a degree's
   * worth of screen wherever it points, or the direction is a lie. */
  const rPx = Math.min((box.x1 - box.x0) / 2, (box.y1 - box.y0) / 2);
  const scale = rPx / (data.radius || 1);

  const svg = svgEl('svg', { width: width, height: chartHeight(width) }, host);

  (data.rings || []).forEach((ring) => {
    svgEl('circle', {
      cx: cx, cy: cy, r: ring * scale, class: 'tick-mark', fill: 'none', 'stroke-opacity': 0.55,
    }, svg);
    /* Labelled once, on the horizontal, so the reader can put a number on an
     * amplitude without counting rings — with a halo, because that horizontal
     * is exactly where a leftward or rightward saccade puts its endpoints. */
    haloText(svg, cx + ring * scale, cy - 4, tickText(ring, data.rings));
  });
  svgEl('line', { x1: cx - rPx, x2: cx + rPx, y1: cy, y2: cy, class: 'spine' }, svg);
  svgEl('line', { x1: cx, x2: cx, y1: cy - rPx, y2: cy + rPx, class: 'spine' }, svg);

  /* The vectors themselves, faint: three hundred of them at full weight are a
   * solid wedge, and the endpoint cloud on top is what carries the detail. */
  series.forEach((one) => {
    const color = seriesColor(one);
    one.points.forEach((point) => {
      svgEl('line', {
        x1: cx, y1: cy, x2: cx + point[0] * scale, y2: cy - point[1] * scale,
        style: 'stroke:' + color + ';stroke-width:1;stroke-opacity:0.22',
      }, svg);
    });
  });
  series.forEach((one) => {
    const color = seriesColor(one);
    one.points.forEach((point) => {
      marker(svg, cx + point[0] * scale, cy - point[1] * scale, color, 3.5);
    });
  });
  drawLegend(legendHost, series.map((one) => ({
    name: one.name || 'saccade', color: seriesColor(one), shape: 'dot',
  })));

  /* The origin, marked and named: a displacement plot without its zero is a
   * cloud of numbers nobody can anchor. */
  svgEl('path', {
    d: 'M' + (cx - 5) + ',' + cy + 'H' + (cx + 5) + 'M' + cx + ',' + (cy - 5) + 'V' + (cy + 5),
    style: 'stroke:var(--ink-2);stroke-width:1.6',
  }, svg);
  haloText(svg, cx + 8, cy + 14, data.origin_label || 'origin', 'start');

  if (data.x_label) {
    svgEl('text', {
      x: cx, y: box.y1 + 26, class: 'axis-text', 'text-anchor': 'middle',
    }, svg).textContent = data.x_label;
  }
  if (data.y_label) {
    svgEl('text', {
      x: 11, y: cy, class: 'axis-text', 'text-anchor': 'middle',
      transform: 'rotate(-90 11 ' + cy + ')',
    }, svg).textContent = data.y_label;
  }

  const hit = svgEl('rect', {
    x: box.x0, y: box.y0, width: Math.max(1, box.x1 - box.x0), height: box.y1 - box.y0, class: 'hit',
  }, svg);
  hit.addEventListener('pointermove', (event) => {
    const rect = svg.getBoundingClientRect();
    const px = event.clientX - rect.left;
    const py = event.clientY - rect.top;
    let best = null;
    let bestGap = Infinity;
    series.forEach((one) => {
      one.points.forEach((point) => {
        const gap = Math.hypot(cx + point[0] * scale - px, cy - point[1] * scale - py);
        if (gap < bestGap) { bestGap = gap; best = { point: point, series: one }; }
      });
    });
    if (!best || bestGap > 40) return hideTip(host);
    const [dx, dy] = best.point;
    /* Direction in degrees anticlockwise from the rightward horizontal — the
     * convention a saccade's direction is quoted in. */
    const angle = (Math.atan2(dy, dx) * 180) / Math.PI;
    const rows = [
      { name: 'amplitude', value: fmt(Math.hypot(dx, dy)), color: seriesColor(best.series) },
      { name: 'direction', value: fmt(angle) + '°' },
    ];
    if (best.series.name) rows.push({ name: data.color_label || '', value: best.series.name });
    showTip(host, cx + dx * scale, cy - dy * scale, 'saccade', rows);
  });
  hit.addEventListener('pointerleave', () => hideTip(host));
}


/** Group means with standard-error bars — the comparison a condition column
 *  invites, drawn so the reader can see how well each mean is pinned down. */
function drawDots(legendHost, host, data) {
  const groups = data.groups || [];
  if (!groups.length) return drawEmpty(host, 'No data yet');

  const width = host.clientWidth || 380;
  const bars = data.style === 'bars';
  /* An interval may be given as absolute bounds (a Wilson interval is not
   * symmetric about its estimate) or as one ± value. */
  const lowOf = (g) => (g.low === undefined ? g.mean - (g.sem || 0) : g.low);
  const highOf = (g) => (g.high === undefined ? g.mean + (g.sem || 0) : g.high);
  let yLo;
  let yHi;
  if (data.y_domain) {
    [yLo, yHi] = data.y_domain;
  } else {
    yLo = Math.min(...groups.map(lowOf));
    yHi = Math.max(...groups.map(highOf));
    /* A bar's length is the quantity, so it has to start at zero — a bar
     * chart cropped to its own range overstates every difference on it. */
    if (bars) { yLo = Math.min(0, yLo); yHi = Math.max(0, yHi); }
    if (yHi === yLo) { yLo -= 0.5; yHi += 0.5; }
    const margin = (yHi - yLo) * 0.14;
    /* Headroom goes above the data, never past the baseline: padding below a
     * bar chart's zero lifts every bar off the axis it is measured from. */
    yLo = bars && yLo >= 0 ? 0 : yLo - margin;
    yHi = bars && yHi <= 0 ? 0 : yHi + margin;
  }

  const yTicks = niceTicks(yLo, yHi, 4);
  const left = leftPad(yTicks);
  const plotH = plotHeight(width);
  const box = { x0: left, x1: width - PAD.right, y0: PAD.top, y1: PAD.top + plotH };
  const step = (box.x1 - box.x0) / groups.length;
  const xAt = (index) => box.x0 + step * (index + 0.5);
  const yScale = (v) => box.y1 - (v - yLo) / (yHi - yLo) * (box.y1 - box.y0);

  const svg = svgEl('svg', { width: width, height: chartHeight(width) }, host);
  drawFrame(svg, box, { xScale: xAt, yScale, xTicks: [], yTicks, yLabel: data.y_label });
  if (yLo < 0 && yHi > 0) {
    /* Bars on both sides of zero need the baseline drawn, or a short negative
     * bar reads as a short positive one. */
    svgEl('line', {
      x1: box.x0, x2: box.x1, y1: yScale(0), y2: yScale(0), class: 'spine',
    }, svg);
  }

  /* One colour per factor. A panel that groups by a single factor keeps the
   * one colour it always had; a panel showing several on a shared axis needs
   * them told apart, and the legend is the only thing that says which is
   * which — the x labels are level names and several factors can share one. */
  const series = [];
  groups.forEach((g) => { if (g.series && !series.includes(g.series)) series.push(g.series); });
  const colorOf = (g) => (series.length > 1 ? slotColor(series.indexOf(g.series)) : slotColor(1));
  drawLegend(legendHost, series.map((name) => ({ name: name, color: slotColor(series.indexOf(name)) })));
  const zero = yScale(Math.min(Math.max(0, yLo), yHi));
  const thickness = Math.min(24, Math.max(6, step - 26));
  groups.forEach((group, index) => {
    const x = xAt(index);
    const y = yScale(group.mean);
    if (bars) {
      /* Rounded at the data end, square at the baseline — and the rounding
       * flips with the sign, so a negative mean reads as a bar hanging from
       * zero rather than one standing on it. */
      const top = Math.min(y, zero);
      const height = Math.abs(zero - y);
      const radius = Math.min(4, thickness / 2, height);
      const up = y <= zero;
      svgEl('path', {
        d: up
          ? 'M' + (x - thickness / 2) + ',' + zero + 'V' + (top + radius) +
            'Q' + (x - thickness / 2) + ',' + top + ' ' + (x - thickness / 2 + radius) + ',' + top +
            'H' + (x + thickness / 2 - radius) +
            'Q' + (x + thickness / 2) + ',' + top + ' ' + (x + thickness / 2) + ',' + (top + radius) +
            'V' + zero + 'Z'
          : 'M' + (x - thickness / 2) + ',' + zero + 'V' + (y - radius) +
            'Q' + (x - thickness / 2) + ',' + y + ' ' + (x - thickness / 2 + radius) + ',' + y +
            'H' + (x + thickness / 2 - radius) +
            'Q' + (x + thickness / 2) + ',' + y + ' ' + (x + thickness / 2) + ',' + (y - radius) +
            'V' + zero + 'Z',
        style: 'fill:' + colorOf(group),
      }, svg);
    }
    const top = yScale(highOf(group));
    const bottom = yScale(lowOf(group));
    if (Math.abs(bottom - top) > 0.5) {
      svgEl('path', {
        d: 'M' + x + ',' + top + 'V' + bottom + 'M' + (x - 4) + ',' + top + 'H' + (x + 4) +
           'M' + (x - 4) + ',' + bottom + 'H' + (x + 4),
        style: 'stroke:' + (bars ? 'var(--ink-2)' : colorOf(group)) + ';stroke-width:1.5',
      }, svg);
    }
    if (!bars) marker(svg, x, y, colorOf(group), 4.5);
    svgEl('text', {
      x: x, y: box.y1 + 15, class: 'tick-text', 'text-anchor': 'middle',
    }, svg).textContent = group.label;
    svgEl('text', {
      x: x, y: box.y1 + 27, class: 'tick-text', 'text-anchor': 'middle', opacity: 0.75,
    }, svg).textContent = 'n=' + group.n;

    const hit = svgEl('rect', { x: x - step / 2, y: box.y0, width: step, height: box.y1 - box.y0, class: 'hit' }, svg);
    hit.addEventListener('pointermove', (event) => {
      const rect = svg.getBoundingClientRect();
      const rows = [{ name: 'mean', value: fmt(group.mean), color: colorOf(group) }];
      rows.push({
        name: data.error_label || 'interval',
        value: fmt(lowOf(group)) + ' – ' + fmt(highOf(group)),
      });
      rows.push({ name: 'trials', value: String(group.n) });
      const title = series.length > 1 ? group.series + ': ' + group.label : group.label;
      showTip(host, event.clientX - rect.left, event.clientY - rect.top, title, rows);
    });
    hit.addEventListener('pointerleave', () => hideTip(host));
  });

  if (data.x_label) {
    svgEl('text', {
      x: (box.x0 + box.x1) / 2, y: box.y1 + 42, class: 'axis-text', 'text-anchor': 'middle',
    }, svg).textContent = data.x_label;
  }
}

/* ------------------------------------------------------------------ */
/* Heatmap — receptive-field maps and any other cell-gridded quantity  */
/* ------------------------------------------------------------------ */

/**
 * The sequential scale, built from the theme's own ordinal ramp so it obeys
 * the theme like every other mark: --ramp-1 is "least" and --ramp-5 "most"
 * in both light and dark. Interpolated here because a heatmap needs a
 * continuous scale and CSS custom properties cannot be mixed in SVG fills.
 * Resolved per draw, and the theme toggle repaints the panels, so a flipped
 * theme never leaves stale colours behind.
 */
function heatStops() {
  const style = getComputedStyle(document.documentElement);
  return [1, 2, 3, 4, 5].map((i) => {
    const hex = style.getPropertyValue('--ramp-' + i).trim().replace('#', '');
    return [0, 2, 4].map((at) => parseInt(hex.slice(at, at + 2), 16));
  });
}

function heatColor(stops, v) {
  const x = Math.max(0, Math.min(1, v)) * (stops.length - 1);
  const i = Math.min(stops.length - 2, Math.floor(x));
  const f = x - i;
  const c = stops[i].map((a, k) => Math.round(a + (stops[i + 1][k] - a) * f));
  return 'rgb(' + c.join(',') + ')';
}

/**
 * One or many cell maps on a shared colour scale — a receptive-field panel.
 *
 * `data.maps` is [{name, matrix, centroid?}] with matrix[row][col], row 0
 * the BOTTOM row (y ascending, matching y_edges — the same y-up convention
 * every spatial panel keeps); a null cell means "not measured yet" and is
 * drawn muted, never as zero. All maps share x_edges/y_edges (data units,
 * e.g. dva), `vmax`, and one colourbar. Cells keep the data's aspect ratio:
 * a degree up must be the same length as a degree across, or the map's
 * shape lies about the field's.
 */
function drawHeatmap(legendHost, host, data) {
  const maps = (data.maps || []).filter((m) => m.matrix && m.matrix.length);
  if (!maps.length) return drawEmpty(host, 'No map yet');
  const xEdges = data.x_edges || [];
  const yEdges = data.y_edges || [];
  const rows = maps[0].matrix.length;
  const cols = maps[0].matrix[0].length;
  if (xEdges.length !== cols + 1 || yEdges.length !== rows + 1) {
    return drawEmpty(host, 'Malformed map: edges do not match the matrix');
  }

  const width = host.clientWidth || 380;
  const height = chartHeight(width);
  const svg = svgEl('svg', { width: width, height: height }, host);
  const stops = heatStops();
  const vmax = Math.max(data.vmax || 0, 1e-9);

  /* The colourbar takes a fixed strip at the bottom; the maps tile the rest
   * in a near-square grid, each with a small title above it. */
  const barH = 34;
  const tileCols = maps.length === 1 ? 1 : (width >= 560 && maps.length > 4 ? 3 : 2);
  const tileRows = Math.ceil(maps.length / tileCols);
  const tileW = (width - 8) / tileCols;
  const tileH = (height - barH - 6) / tileRows;
  const titleH = 15;

  const spanX = xEdges[cols] - xEdges[0];
  const spanY = yEdges[rows] - yEdges[0];

  maps.forEach((one, index) => {
    const tx = 4 + (index % tileCols) * tileW;
    const ty = (Math.floor(index / tileCols)) * tileH;
    /* Fit the map into the tile at the data's own aspect, centred. */
    const availW = tileW - 10;
    const availH = tileH - titleH - 8;
    const scale = Math.min(availW / spanX, availH / spanY);
    const mapW = spanX * scale;
    const mapH = spanY * scale;
    const x0 = tx + (tileW - mapW) / 2;
    const y1 = ty + titleH + (availH - mapH) / 2 + mapH;
    const xScale = (v) => x0 + (v - xEdges[0]) * scale;
    const yScale = (v) => y1 - (v - yEdges[0]) * scale;

    svgEl('text', {
      x: tx + tileW / 2, y: ty + 11, class: 'tick-text', 'text-anchor': 'middle',
    }, svg).textContent = one.name || '';

    for (let r = 0; r < rows; r += 1) {
      for (let c = 0; c < cols; c += 1) {
        const value = one.matrix[r][c];
        const cellX = xScale(xEdges[c]);
        const cellY = yScale(yEdges[r + 1]);
        const cell = svgEl('rect', {
          x: cellX,
          y: cellY,
          width: Math.max(0.5, xScale(xEdges[c + 1]) - cellX),
          height: Math.max(0.5, yScale(yEdges[r]) - cellY),
          style: value === null
            ? 'fill:var(--muted);fill-opacity:0.12'
            : 'fill:' + heatColor(stops, value / vmax),
        }, svg);
        cell.classList.add('hit');
        const head = fmt((xEdges[c] + xEdges[c + 1]) / 2) + ', ' +
                     fmt((yEdges[r] + yEdges[r + 1]) / 2) + ' ' + (data.x_label ? 'dva' : '');
        cell.addEventListener('pointermove', (event) => {
          const rect = svg.getBoundingClientRect();
          const readout = [{
            name: data.value_label || 'value',
            value: value === null ? 'not probed yet' : fmt(value),
            color: value === null ? 'var(--muted)' : heatColor(stops, value / vmax),
          }];
          if (data.flashes && data.flashes[r]) {
            readout.push({ name: 'flashes', value: String(data.flashes[r][c]) });
          }
          if (one.name) readout.push({ name: 'map', value: one.name });
          showTip(host, event.clientX - rect.left, event.clientY - rect.top, head, readout);
        });
        cell.addEventListener('pointerleave', () => hideTip(host));
      }
    }

    /* The origin, when it is inside the mapped extent: a spatial map with
     * its fixation point unmarked makes the reader count cells. */
    if (xEdges[0] < 0 && xEdges[cols] > 0) {
      svgEl('line', {
        x1: xScale(0), x2: xScale(0), y1: yScale(yEdges[rows]), y2: yScale(yEdges[0]),
        class: 'rule',
      }, svg);
    }
    if (yEdges[0] < 0 && yEdges[rows] > 0) {
      svgEl('line', {
        x1: xScale(xEdges[0]), x2: xScale(xEdges[cols]), y1: yScale(0), y2: yScale(0),
        class: 'rule',
      }, svg);
    }

    /* The map's estimated centre, same summary mark as the scatter's mean. */
    if (one.centroid && isFinite(one.centroid[0])) {
      const mx = xScale(one.centroid[0]);
      const my = yScale(one.centroid[1]);
      const diamond = 'M' + mx + ',' + (my - 5) + 'L' + (mx + 5) + ',' + my +
                      'L' + mx + ',' + (my + 5) + 'L' + (mx - 5) + ',' + my + 'Z';
      svgEl('path', {
        d: diamond,
        style: 'fill:var(--surface);stroke:var(--ink);stroke-width:1.4',
      }, svg);
    }
  });

  /* One colourbar for every map — a shared scale is the whole point of
   * small multiples, and each map carrying its own would quietly break it. */
  const gradientId = 'heat-' + Math.random().toString(36).slice(2, 8);
  const defs = svgEl('defs', {}, svg);
  const gradient = svgEl('linearGradient', { id: gradientId, x1: 0, x2: 1, y1: 0, y2: 0 }, defs);
  stops.forEach((stop, index) => {
    svgEl('stop', {
      offset: (100 * index / (stops.length - 1)) + '%',
      'stop-color': 'rgb(' + stop.join(',') + ')',
    }, gradient);
  });
  const barY = height - barH + 6;
  const barX0 = Math.max(60, width * 0.25);
  const barX1 = width - Math.max(60, width * 0.25);
  svgEl('rect', {
    x: barX0, y: barY, width: Math.max(10, barX1 - barX0), height: 8, rx: 3,
    style: 'fill:url(#' + gradientId + ')',
  }, svg);
  svgEl('text', {
    x: barX0 - 6, y: barY + 8, class: 'tick-text', 'text-anchor': 'end',
  }, svg).textContent = '0';
  svgEl('text', {
    x: barX1 + 6, y: barY + 8, class: 'tick-text',
  }, svg).textContent = fmt(data.vmax || 0);
  svgEl('text', {
    x: (barX0 + barX1) / 2, y: barY + 22, class: 'axis-text', 'text-anchor': 'middle',
  }, svg).textContent = (data.value_label || '') +
    (data.x_label ? ' · ' + data.x_label + ' / ' + (data.y_label || '') : '');

  if (maps.some((one) => one.centroid)) {
    drawLegend(legendHost, [
      { name: 'unprobed cell', color: 'var(--muted)', shape: 'box' },
      { name: 'estimated centre', color: 'var(--ink-2)', shape: 'diamond' },
    ]);
  }
}

/** One number is one number. A single scalar drawn as a one-bar bar chart
 *  tells the reader nothing the digits don't, and spends a panel doing it. */
function drawStat(legendHost, host, data) {
  const tile = htmlEl('div', 'stat-tile', null, host);
  tile.style.height = chartHeight(host.clientWidth || 380) + 'px';
  htmlEl('div', 'tile-value', data.value + (data.unit ? ' ' + data.unit : ''), tile);
  htmlEl('div', 'tile-label', data.label, tile);
  if (data.secondary) htmlEl('div', 'tile-secondary', data.secondary, tile);
}

function drawEmpty(host, message) {
  /* The same height as a drawn plot, so a panel with nothing to show yet does
   * not pull its row out of alignment. */
  const box = htmlEl('div', 'empty', message || 'No data yet', host);
  box.style.height = chartHeight(host.clientWidth || 380) + 'px';
}

/* ------------------------------------------------------------------ */
/* Table view — every plotted value, readable as text                  */
/* ------------------------------------------------------------------ */

function tableRows(data) {
  switch (data.form) {
    case 'bars':
      return { head: ['category', data.value_label || 'count', 'share'],
        rows: data.items.map((i) => [i.label, i.value.toLocaleString(), (i.share * 100).toFixed(1) + '%']) };
    case 'histogram':
      return { head: ['bin', data.y_label || 'count'],
        rows: data.bins.map((b) => [fmt(b.x0) + ' – ' + fmt(b.x1), String(b.count)]) };
    case 'line': {
      const series = data.series || [];
      const xs = [];
      series.forEach((s) => s.points.forEach((p) => { if (!xs.includes(p[0])) xs.push(p[0]); }));
      xs.sort((a, b) => a - b);
      return { head: [data.x_label || 'x'].concat(series.map((s) => s.name)),
        rows: xs.map((x) => [fmt(x)].concat(series.map((s) => {
          const hit = s.points.find((p) => p[0] === x);
          return hit ? fmt(hit[1]) : '';
        }))) };
    }
    case 'scatter': {
      const named = (data.series || []).some((s) => s.name);
      return { head: [...(named ? [data.color_label || 'series'] : []), data.x_label || 'x', data.y_label || 'y'],
        rows: (data.series || []).flatMap((s) => s.points.map((p) => [
          ...(named ? [s.name] : []), fmt(p[0]), fmt(p[1]),
        ])) };
    }
    case 'vectors': {
      const named = (data.series || []).some((s) => s.name);
      return { head: [...(named ? [data.color_label || 'series'] : []), 'amplitude', 'direction (°)'],
        rows: (data.series || []).flatMap((s) => s.points.map((p) => [
          ...(named ? [s.name] : []),
          fmt(Math.hypot(p[0], p[1])),
          fmt((Math.atan2(p[1], p[0]) * 180) / Math.PI),
        ])) };
    }
    case 'dots':
      return { head: [data.x_label || 'group', 'mean', data.error_label || 'interval', 'n'],
        rows: (data.groups || []).map((g) => [
          g.label,
          fmt(g.mean),
          g.low === undefined
            ? (g.sem === null ? '—' : '± ' + fmt(g.sem))
            : fmt(g.low) + ' – ' + fmt(g.high),
          String(g.n),
        ]) };
    case 'stat':
      return { head: [data.label || 'value', ''], rows: [[data.value + (data.unit ? ' ' + data.unit : ''), data.secondary || '']] };
    case 'heatmap': {
      /* One row per cell, top row first (the reading order of the drawn
       * map), a column per map — every plotted rate readable as text. */
      const maps = data.maps || [];
      if (!maps.length) return null;
      const rows = [];
      for (let r = maps[0].matrix.length - 1; r >= 0; r -= 1) {
        for (let c = 0; c < maps[0].matrix[r].length; c += 1) {
          rows.push([
            fmt((data.x_edges[c] + data.x_edges[c + 1]) / 2),
            fmt((data.y_edges[r] + data.y_edges[r + 1]) / 2),
            data.flashes && data.flashes[r] ? String(data.flashes[r][c]) : '',
            ...maps.map((m) => (m.matrix[r][c] === null ? '' : fmt(m.matrix[r][c]))),
          ]);
        }
      }
      return {
        head: [data.x_label || 'x', data.y_label || 'y', 'flashes', ...maps.map((m) => m.name)],
        rows: rows,
      };
    }
    default:
      return null;
  }
}

function drawTable(card, data, index, open) {
  let content = null;
  try {
    content = tableRows(data);
  } catch (error) {
    /* A payload the table view has not learned to read yet costs its own
     * table, never the whole page: this runs while the panels are being
     * built, so throwing here leaves the dashboard blank mid-session. */
    console.error('table view failed for a ' + data.form + ' panel', error);
    return;
  }
  if (!content || !content.rows.length) return;
  const details = htmlEl('details', 'table', null, card);
  details.open = open;
  details.dataset.panel = String(index);
  htmlEl('summary', null, 'Table (' + content.rows.length + ' rows)', details);
  const scroll = htmlEl('div', 'table-scroll', null, details);
  const table = htmlEl('table', null, null, scroll);
  const head = htmlEl('tr', null, null, htmlEl('thead', null, null, table));
  content.head.forEach((cell) => htmlEl('th', null, cell, head));
  const body = htmlEl('tbody', null, null, table);
  content.rows.forEach((row) => {
    const tr = htmlEl('tr', null, null, body);
    row.forEach((cell) => htmlEl('td', null, cell, tr));
  });
}

/* ------------------------------------------------------------------ */
/* Panels and page                                                     */
/* ------------------------------------------------------------------ */

const DRAW = {
  line: drawLineChart,
  bars: drawBars,
  histogram: drawHistogram,
  scatter: drawScatter,
  vectors: drawVectors,
  dots: drawDots,
  stat: drawStat,
  heatmap: drawHeatmap,
};

/**
 * Build one panel's frame — everything except the drawing.
 *
 * Building and drawing are two passes for a reason that is easy to miss and
 * impossible to un-see: an element that is not in the document has a
 * `clientWidth` of 0. Drawing while the card was still detached meant every
 * chart fell back to a hard-coded width — too small inside a wide card, and
 * spilling out of a narrow one.
 */
function buildPanel(panel, index, openTables) {
  const card = htmlEl('section', 'panel');
  htmlEl('h2', null, panel.title, card);
  const data = panel.data || { form: 'empty', message: 'No data yet' };

  if (data.stats && data.stats.length) {
    const strip = htmlEl('div', 'stats', null, card);
    data.stats.forEach((stat) => {
      const cell = htmlEl('div', null, null, strip);
      htmlEl('span', 'stat-label', stat.label, cell);
      const value = htmlEl('span', 'stat-value', stat.value, cell);
      if (stat.status) value.dataset.status = stat.status;
    });
  }

  /* The legend gets its own slot between the plot and the note, so drawing
   * later cannot append it below the table view. */
  const host = htmlEl('div', 'plot', null, card);
  const legendHost = htmlEl('div', 'legend-slot', null, card);
  htmlEl('p', 'note', data.note || '', card);
  drawTable(card, data, index, openTables.has(index));
  return {
    card: card,
    host: host,
    legendHost: legendHost,
    data: data,
    section: panel.section || 'Other',
  };
}

/** Draw (or redraw) one built panel, now that it has a real width. */
function paintPanel(entry) {
  entry.host.replaceChildren();
  entry.legendHost.replaceChildren();
  const data = entry.data;
  if (data.form === 'empty' || !DRAW[data.form]) {
    drawEmpty(entry.host, data.message || 'Nothing to draw');
    return;
  }
  DRAW[data.form](entry.legendHost, entry.host, data);
}

let state = null;
let painted = -1;
let panels = [];

/* Which group of panels is on screen. Panels are read in groups — how the
 * session is going, what the subject did, where it looked, how the conditions
 * compare — and a dashboard that shows all of them at once is a page to
 * scroll rather than a thing to watch. */
let section = 'all';
try { section = localStorage.getItem('alhazen-section') || 'all'; } catch (e) { /* private mode */ }

function renderSections(entries) {
  const counts = new Map();
  entries.forEach((entry) => {
    const name = entry.section;
    counts.set(name, (counts.get(name) || 0) + 1);
  });
  /* A remembered group that this session has no panels for would leave the
   * grid empty with no way back except the sidebar the reader is looking at. */
  if (section !== 'all' && !counts.has(section)) section = 'all';

  const list = byId('sections');
  list.replaceChildren();
  const rows = [['all', 'All panels', entries.length], ...[...counts].map((c) => [c[0], c[0], c[1]])];
  rows.forEach(([key, label, count]) => {
    const item = htmlEl('li', null, null, list);
    const button = htmlEl('button', null, null, item);
    button.type = 'button';
    button.setAttribute('aria-current', String(key === section));
    htmlEl('span', null, label, button);
    htmlEl('span', 'count', count, button);
    button.onclick = () => {
      section = key;
      try { localStorage.setItem('alhazen-section', key); } catch (e) { /* private mode */ }
      render();
    };
  });
}

function render() {
  if (!state) return;
  const identity = state.identity || {};
  byId('identity').textContent = [
    identity.task_name,
    identity.subject && 'sub-' + identity.subject,
    identity.session && 'ses-' + String(identity.session).padStart(3, '0'),
    identity.run && 'run-' + String(identity.run).padStart(2, '0'),
  ].filter(Boolean).join(' · ');

  const status = byId('status');
  status.textContent = state.status;
  status.dataset.state = state.status;
  byId('counts').textContent = (state.n_trials || 0).toLocaleString() + ' trials · ' +
    (state.n_events || 0).toLocaleString() + ' events';

  const paused = state.status === 'paused';
  document.querySelectorAll('[data-command]').forEach((button) => { button.disabled = !paused; });
  byId('notice').textContent = paused
    ? (state.message || 'Paused — controls are enabled.')
    : 'Press P on the experimenter keyboard to pause.';

  /* Which table views the reader had open, so a new trial does not close
   * one they were reading. */
  const openTables = new Set();
  document.querySelectorAll('details.table[open]').forEach((node) => {
    openTables.add(Number(node.dataset.panel));
  });
  /* Where the reader had scrolled to. Rebuilding the grid empties the
   * document for an instant, and a browser laying out an empty document
   * clamps the scroll position to zero — so without this the page jumps back
   * to the top on every completed trial, which at one trial every few seconds
   * makes a panel halfway down impossible to read. Captured here and restored
   * below, in the same synchronous block, so the intermediate state is never
   * painted. */
  const scroller = document.scrollingElement || document.documentElement;
  const scrollTop = scroller.scrollTop;

  panels = (state.panels || []).map((panel, index) => buildPanel(panel, index, openTables));
  renderSections(panels);
  const shown = panels.filter((entry) => section === 'all' || entry.section === section);
  byId('grid').replaceChildren(...shown.map((entry) => entry.card));
  /* Attached now, so every panel can be measured before it is drawn. Only the
   * shown ones: an element that is not in the document has no width, and a
   * chart drawn against that width would be drawn wrong. */
  panels = shown;
  panels.forEach(paintPanel);

  /* After painting, not before: the panels have their real heights only once
   * they are drawn, and restoring against a half-laid-out document would
   * clamp to a maximum that is about to grow. */
  if (scroller.scrollTop !== scrollTop) scroller.scrollTop = scrollTop;
}

/* Charts are sized in pixels, so a resized window is a redraw — of the plots
 * only, which leaves the reader's open table views alone. One frame of
 * debounce keeps a drag from redrawing every panel per pointer event. */
let resizeHandle = 0;
new ResizeObserver(() => {
  cancelAnimationFrame(resizeHandle);
  resizeHandle = requestAnimationFrame(() => panels.forEach(paintPanel));
}).observe(document.body);

/* ------------------------------------------------------------------ */
/* Theme                                                               */
/* ------------------------------------------------------------------ */

const THEMES = ['auto', 'light', 'dark'];
function applyTheme(name) {
  if (name === 'auto') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme', name);
  byId('theme').textContent = 'Theme: ' + name;
  try { localStorage.setItem('alhazen-theme', name); } catch (e) { /* private mode */ }
  /* Most marks follow the theme through CSS variables and need nothing;
   * the heatmap interpolates its scale from resolved colours, so a theme
   * flip is a repaint. Empty before the first render, so this is safe at
   * startup. */
  panels.forEach(paintPanel);
}
let theme = 'auto';
try { theme = localStorage.getItem('alhazen-theme') || 'auto'; } catch (e) { /* private mode */ }
applyTheme(THEMES.includes(theme) ? theme : 'auto');
byId('theme').onclick = () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  applyTheme(theme);
};
/* In "auto" the OS can flip the scheme underneath the page; the heatmap's
 * interpolated colours need the same repaint the toggle gets. */
window.matchMedia('(prefers-color-scheme: dark)')
  .addEventListener('change', () => panels.forEach(paintPanel));

/* ------------------------------------------------------------------ */
/* Commands and polling                                                */
/* ------------------------------------------------------------------ */

let token = new URLSearchParams(location.search).get('token') || sessionStorage.getItem('alhazen-token');
if (token) sessionStorage.setItem('alhazen-token', token);

async function command(name) {
  if (name === 'quit' && !confirm('Quit this session?')) return;
  try {
    const response = await fetch('/api/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Alhazen-Token': token },
      body: JSON.stringify({ name: name, request_id: crypto.randomUUID() }),
    });
    byId('notice').textContent = response.ok ? 'Command accepted.' : await response.text();
  } catch (error) {
    byId('notice').textContent = 'Command failed: ' + error;
  }
}
document.querySelectorAll('[data-command]').forEach((button) => {
  button.onclick = () => command(button.dataset.command);
});

async function poll() {
  try {
    const response = await fetch('/api/state?token=' + encodeURIComponent(token) +
      '&revision=' + ((state && state.revision) || 0));
    if (response.ok) {
      const next = await response.json();
      /* The server long-polls and returns the same snapshot on timeout;
       * repainting an unchanged revision would only throw away the reader's
       * hover and scroll position. */
      if (next.revision !== painted) {
        state = next;
        painted = next.revision;
        render();
      }
    }
  } catch (error) {
    const status = byId('status');
    status.textContent = 'disconnected';
    status.dataset.state = 'disconnected';
  }
  setTimeout(poll, 50);
}

if (STATIC_STATE !== null) {
  /* The saved copy in figures/: one render, no server to poll. */
  state = STATIC_STATE;
  painted = state.revision;
  render();
} else {
  poll();
}
