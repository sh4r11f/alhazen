/* Alhazen experiment workspace — the juice a session delivered, drawn on the
 * History page's session details (GET /api/data/run's `juice`, the live
 * monitor's own "juice" form from live_monitor/juice.py): one bar per paid
 * trial, stacked by what it was paid for, and the cumulative total as a step
 * line on its own right-hand axis, in the unit the payload says (µL with a
 * rig calibration, pulses without).
 *
 *   JuiceChart.render(data, {node}) -> element
 */
'use strict';

const JuiceChart = (() => {
  const SVG = 'http://www.w3.org/2000/svg';
  const COLORS = {
    outcome: 'var(--accent)',
    fault: 'var(--juice-fault)',
    manual: 'var(--muted)',
    mid_trial: 'var(--ok-ink)',
  };

  function svg(tag, attrs, parent) {
    const el = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, String(v));
    if (parent) parent.appendChild(el);
    return el;
  }

  function ticks(hi, count, integer) {
    if (!(hi > 0)) return [0, 1];
    const raw = hi / count;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const n = raw / mag;
    let step = (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * mag;
    if (integer) step = Math.max(1, Math.round(step));
    const out = [];
    for (let v = 0; v <= hi + step * 1e-9; v += step) out.push(Number(v.toFixed(6)));
    if (out[out.length - 1] < hi) out.push(Number((out[out.length - 1] + step).toFixed(6)));
    return out;
  }

  function fmt(v) {
    return Math.abs(v) >= 100 ? String(Math.round(v)) : String(Number(v.toFixed(1)));
  }

  function render(data, {node}) {
    const box = node('section', 'juice');
    box.append(node('h4', 'juice-title', 'Juice delivered'));
    const stats = node('div', 'juice-stats');
    for (const stat of data.stats || []) {
      const item = node('span', 'juice-stat');
      if (stat.status) item.dataset.status = stat.status;
      item.append(node('span', 'juice-k', stat.label), node('span', 'juice-v', stat.value));
      stats.append(item);
    }
    box.append(stats);
    const trials = data.trials || [];
    const cumulative = data.cumulative || [];
    const kinds = (data.kinds || []).map((k) => k.key);
    const W = 560;
    const H = 220;
    const pad = {l: 46, r: 52, t: 10, b: 38};
    const xs = cumulative.map((p) => p[0]).concat(trials.map((t) => t.x));
    const x0 = Math.min(...xs, 0) - 1;
    const x1 = Math.max(...xs, x0 + 1) + 1;
    const integer = data.unit !== 'µL';
    const perTicks = ticks(Math.max(0, ...trials.map((t) => kinds.reduce((s, k) => s + (t[k] || 0), 0))), 3, integer);
    const cumTicks = ticks(data.total || 0, 4, integer);
    const X = (v) => pad.l + ((v - x0) / (x1 - x0)) * (W - pad.l - pad.r);
    const Y = (v) => H - pad.b - (v / perTicks[perTicks.length - 1]) * (H - pad.t - pad.b);
    const Y2 = (v) => H - pad.b - (v / cumTicks[cumTicks.length - 1]) * (H - pad.t - pad.b);
    const chart = svg('svg', {viewBox: `0 0 ${W} ${H}`, class: 'juice-chart', role: 'img',
      'aria-label': `Juice delivered per trial and cumulative, in ${data.unit}`});
    svg('line', {x1: pad.l, x2: pad.l, y1: pad.t, y2: H - pad.b, class: 'juice-axis'}, chart);
    svg('line', {x1: W - pad.r, x2: W - pad.r, y1: pad.t, y2: H - pad.b, class: 'juice-axis'}, chart);
    svg('line', {x1: pad.l, x2: W - pad.r, y1: H - pad.b, y2: H - pad.b, class: 'juice-axis'}, chart);
    for (const v of perTicks) {
      svg('text', {x: pad.l - 6, y: Y(v) + 4, class: 'juice-tick', 'text-anchor': 'end'}, chart).textContent = fmt(v);
    }
    for (const v of ticks(x1, 5, true).filter((v) => v >= x0 && v <= x1)) {
      svg('line', {x1: X(v), x2: X(v), y1: H - pad.b, y2: H - pad.b + 4, class: 'juice-axis'}, chart);
      svg('text', {x: X(v), y: H - pad.b + 15, class: 'juice-tick', 'text-anchor': 'middle'}, chart)
        .textContent = fmt(v);
    }
    for (const v of cumTicks) {
      svg('text', {x: W - pad.r + 6, y: Y2(v) + 4, class: 'juice-tick'}, chart).textContent = fmt(v);
    }
    svg('text', {x: (pad.l + W - pad.r) / 2, y: H - 6, class: 'juice-label', 'text-anchor': 'middle'}, chart)
      .textContent = data.x_label || 'trial';
    svg('text', {x: 12, y: (H - pad.b + pad.t) / 2, class: 'juice-label', 'text-anchor': 'middle',
      transform: `rotate(-90 12 ${(H - pad.b + pad.t) / 2})`}, chart).textContent = data.y_label;
    svg('text', {x: W - 8, y: (H - pad.b + pad.t) / 2, class: 'juice-label', 'text-anchor': 'middle',
      transform: `rotate(90 ${W - 8} ${(H - pad.b + pad.t) / 2})`}, chart).textContent = data.y2_label;
    const barW = Math.max(1.5, Math.min(12, ((W - pad.l - pad.r) / Math.max(1, x1 - x0)) * 0.7));
    for (const t of trials) {
      let base = 0;
      for (const k of kinds) {
        const v = t[k] || 0;
        if (!v) continue;
        const top = Y(base + v);
        const rect = svg('rect', {x: X(t.x) - barW / 2, y: top, width: barW,
          height: Math.max(0.5, Y(base) - top), 'data-kind': k}, chart);
        rect.style.cssText = k === 'manual'
          ? `fill:var(--surface);stroke:${COLORS[k]};stroke-width:1.2` : `fill:${COLORS[k]}`;
        svg('title', {}, rect).textContent = `trial ${t.x}: ${fmt(v)} ${data.unit}`;
        base += v;
      }
    }
    if (cumulative.length) {
      let d = '';
      cumulative.forEach((p, i) => { d += i ? `H${X(p[0])}V${Y2(p[1])}` : `M${X(p[0])},${Y2(p[1])}`; });
      svg('path', {d, class: 'juice-total'}, chart);
    }
    for (const f of data.failures || []) {
      svg('line', {x1: X(f), x2: X(f), y1: pad.t, y2: H - pad.b, class: 'juice-failed'}, chart);
    }
    // Scrolls sideways on a narrow pane rather than shrinking its text.
    const scroll = node('div', 'juice-scroll');
    scroll.append(chart);
    box.append(scroll);
    const legend = node('div', 'juice-legend');
    for (const kind of data.kinds || []) {
      if (!trials.some((t) => t[kind.key])) continue;
      const item = node('span', 'juice-key');
      const sw = node('i', kind.key === 'manual' ? 'juice-swatch juice-outline' : 'juice-swatch');
      sw.style.cssText = kind.key === 'manual' ? `border-color:${COLORS[kind.key]}` : `background:${COLORS[kind.key]}`;
      item.append(sw, node('span', '', kind.name));
      legend.append(item);
    }
    const total = node('span', 'juice-key');
    total.append(node('i', 'juice-swatch juice-line'), node('span', '', 'cumulative'));
    legend.append(total);
    box.append(legend);
    if (data.note) box.append(node('p', 'help', data.note));
    return box;
  }

  return {render};
})();

if (typeof window !== 'undefined') window.JuiceChart = JuiceChart;
