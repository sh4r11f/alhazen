/* hub_docs.js: the documentation renderer, run in Node against fake_dom.
 *
 * The data is the real resolved output of alhazen.hub.documentation for the
 * scaffold example (tests/hub/fixtures/documentation/resolved/, kept current
 * by tests/hub/test_documentation.py), so these tests draw what the server
 * would serve. Held here:
 * - untrusted text never becomes markup or script, links are https only and
 *   images are never fetched;
 * - a phase that waits on the subject is never given a length: its width does
 *   not change with its timeout, while known durations scale;
 * - figures are labelled for assistive technology and carry no inline styles;
 * - parameter mentions jump to their row without touching the URL;
 * - the guide shows alhazen's modes in order, with their sources.
 */

import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

import { FakeDocument } from './fake_dom.mjs';

const ASSET = new URL('../../src/alhazen/hub/assets/hub_docs.js', import.meta.url);
const SOURCE = readFileSync(ASSET, 'utf8');
const RESOLVED = new URL('../hub/fixtures/documentation/resolved/', import.meta.url);
const SCAFFOLD = JSON.parse(readFileSync(new URL('scaffold.json', RESOLVED), 'utf8'));
const GUIDE = JSON.parse(readFileSync(new URL('guide.json', RESOLVED), 'utf8'));

/* fake_dom has no text nodes; a browser does, and the renderer uses them. A
 * text node here is an element named #text holding only its own text. */
class DocsDocument extends FakeDocument {
  createTextNode(text) {
    const node = this.createElement('#text');
    node.textContent = text;
    return node;
  }
}

function load() {
  const sandbox = { setTimeout: () => 0, console };
  vm.createContext(sandbox);
  vm.runInContext(SOURCE, sandbox, { filename: 'hub_docs.js' });
  return vm.runInContext('HubDocs', sandbox);
}

const HubDocs = load();
const clone = (value) => JSON.parse(JSON.stringify(value));

function options(extra) {
  return { document: new DocsDocument(), taskHref: (id) => '/hub?view=experiment&task=' + id, ...extra };
}

function all(node) {
  const out = [];
  const walk = (current) => { out.push(current); current.children.forEach(walk); };
  walk(node);
  return out;
}

function byTag(node, tag) {
  return all(node).filter((child) => child.localName === tag);
}

function assertNoInlineBehaviour(node) {
  for (const element of all(node)) {
    for (const [name] of element.attributes) {
      assert.notEqual(name, 'style', element.localName + ' has a style attribute');
      assert.ok(!/^on/i.test(name), element.localName + ' has an event attribute ' + name);
      if (name === 'href') assert.ok(!/^\s*javascript:/i.test(element.getAttribute(name)));
    }
  }
}

test('the source never builds markup, evaluates code, fetches or touches the URL', () => {
  const code = SOURCE.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  for (const forbidden of ['innerHTML', 'outerHTML', 'insertAdjacentHTML', 'eval(', 'new Function', 'fetch(',
    'XMLHttpRequest', '.style', 'location', 'localStorage', 'sessionStorage', 'document.write']) {
    assert.ok(!code.includes(forbidden), 'hub_docs.js uses ' + forbidden);
  }
});

test('Markdown: blocks parse, raw HTML stays text, images are not fetched', () => {
  const blocks = HubDocs.parseMarkdown('# Title\n\nOne\ntwo **bold** and `code`.\n\n- a\n- b\n\n1. x\n2. y\n\n```\n<b>raw</b>\n```\n\n> quoted\n\n| A | B |\n|---|---|\n| 1 | 2 |\n');
  assert.deepEqual(Array.from(blocks, (b) => b.type), ['heading', 'paragraph', 'list', 'list', 'code', 'quote', 'table']);
  assert.equal(blocks[1].inline[0].text, 'One two ');
  assert.equal(blocks[4].text, '<b>raw</b>');
  assert.equal(blocks[6].rows[0].length, 2);

  const view = HubDocs.renderMarkdown(
    '<script>alert(1)</script> ![x](https://evil.example/p.png) [bad](javascript:alert(1)) [good](https://example.org/a)',
    options(),
  );
  assert.equal(byTag(view, 'script').length, 0);
  assert.equal(byTag(view, 'img').length, 0);
  assert.ok(view.textContent.includes('<script>alert(1)</script>'));
  assert.ok(view.textContent.includes('[image not shown: x]'));
  const links = byTag(view, 'a');
  assert.equal(links.length, 1);
  assert.equal(links[0].getAttribute('href'), 'https://example.org/a');
  assert.match(links[0].getAttribute('rel'), /noopener/);
  assertNoInlineBehaviour(view);
});

test('values read the same as documentation._format_value', () => {
  assert.equal(HubDocs.formatValue({ ms: 500 }, 'ms'), '500 ms');
  assert.equal(HubDocs.formatValue({ frames: 30 }, null), '30 frames');
  assert.equal(HubDocs.formatValue(0.3, 'dva'), '0.3 dva');
  assert.equal(HubDocs.formatValue(2.0, 'dva'), '2 dva');
  assert.equal(HubDocs.formatValue(true, null), 'true');
  assert.equal(HubDocs.formatValue({ kind: 'sequence', n_per_condition: 10 }, null), 'kind: sequence, n_per_condition: 10');
  // The resolved fixture's text was produced by Python; the browser agrees.
  for (const parameter of SCAFFOLD.tasks[0].parameters) {
    if (parameter.has_default) assert.equal(HubDocs.formatValue(parameter.default, parameter.unit), parameter.default_text);
  }
});

test('parameter references resolve by the same rule as the server', () => {
  const tasks = SCAFFOLD.tasks;
  assert.equal(HubDocs.resolveParameterReference('iti', tasks[0], tasks).parameter.name, 'iti');
  assert.equal(HubDocs.resolveParameterReference('fixation-demo/iti', null, tasks).parameter.name, 'iti');
  assert.equal(HubDocs.resolveParameterReference('iti', null, tasks).task.id, 'fixation-demo');
  assert.equal(HubDocs.resolveParameterReference('nope', null, tasks), null);
  const twice = [tasks[0], { ...tasks[0], id: 'other' }];
  assert.equal(HubDocs.resolveParameterReference('iti', null, twice), null, 'ambiguous in the methods');
});

test('a phase that waits on the subject has no length; known durations scale', () => {
  const timeline = clone(SCAFFOLD.tasks[0].timeline);
  const base = HubDocs.layoutTimeline(timeline);
  const [acquire, hold] = base.segments;
  assert.equal(acquire.kind, 'unscaled');
  timeline.phases[0].timing.max_ms = 10000;
  assert.equal(HubDocs.layoutTimeline(timeline).segments[0].width, acquire.width, 'timeout does not stretch it');

  assert.equal(hold.kind, 'scaled');
  assert.ok(Math.abs(hold.width - 500 * base.pxPerMs) < 1e-9);
  assert.ok(base.scaleBar && Math.abs(base.scaleBar.px - base.scaleBar.ms * base.pxPerMs) < 1e-9);
  assert.ok(base.anyUnscaled);

  const twice = clone(SCAFFOLD.tasks[0].timeline);
  twice.phases.push({ ...clone(twice.phases[1]), id: 'hold2', timing: { ...twice.phases[1].timing, ms: 1000, text: '1000 ms' } });
  const longer = HubDocs.layoutTimeline(twice);
  assert.ok(Math.abs(longer.segments[2].width / longer.segments[1].width - 2) < 1e-9);

  const tiny = clone(SCAFFOLD.tasks[0].timeline);
  tiny.phases[1].timing = { kind: 'fixed', ms: 8, text: '8 ms', scaled: true };
  tiny.between_trials = null;
  const small = HubDocs.layoutTimeline(tiny);
  assert.equal(small.segments[1].clamped, true);
  assert.equal(small.anyClamped, true);
  assert.equal(small.scaleBar, null, 'no scale bar when nothing is drawn to scale');
});

test('the timeline figure is labelled, honest about waits and free of inline styles', () => {
  const task = SCAFFOLD.tasks[0];
  const figure = HubDocs.timelineSvg(task, options());
  assert.equal(figure.getAttribute('role'), 'img');
  const title = byTag(figure, 'title')[0];
  const desc = byTag(figure, 'desc')[0];
  assert.match(title.textContent, /Fixation hold/);
  assert.match(desc.textContent, /until gaze is inside the window, at most 2000 ms/);
  assert.match(desc.textContent, /FIX_BREAK/);
  const labels = byTag(figure, 'text').map((t) => t.textContent);
  for (const expected of ['Acquire fixation', 'waits on an event', 'Hold fixation', '500 ms', 'FIXATED', 'NO_FIXATION', 'FIX_BREAK', 'ABORTED (any phase)', 'FIX_ON', 'FIX_ACQUIRED', 'Inter-trial interval']) {
    assert.ok(labels.includes(expected), 'missing ' + expected);
  }
  const hatched = byTag(figure, 'rect').filter((r) => (r.getAttribute('fill') || '').startsWith('url(#'));
  assert.equal(hatched.length, 1, 'only the acquisition is hatched');
  for (const element of all(figure)) {
    for (const [name, value] of element.attributes) {
      if (['x', 'y', 'x1', 'x2', 'y1', 'y2', 'width', 'height', 'r', 'cx', 'cy'].includes(name)) {
        assert.ok(Number.isFinite(Number(value)), name + '=' + value);
      }
    }
  }
  assertNoInlineBehaviour(figure);
});

test('labels of a short phase beside the next stack instead of overprinting (mbri "Block marker")', () => {
  const timeline = clone(SCAFFOLD.tasks[0].timeline);
  timeline.phases.unshift({ id: 'marker', label: 'Block marker',
    timing: { kind: 'conditional', ms: 0, text: '0 ms', scaled: true }, start_events: [], end_events: [] });
  const layout = HubDocs.layoutTimeline(timeline);
  const [marker, acquire] = layout.segments;
  assert.equal(marker.kind, 'instant');
  assert.notEqual(marker.labelRow, acquire.labelRow, 'the two labels are in different tiers');
  const rows = new Map();
  for (const s of layout.segments.concat(layout.between ? [layout.between] : [])) {
    for (const other of rows.get(s.labelRow) || []) {
      assert.ok(s.labelX >= other.labelEnd || other.labelX >= s.labelEnd, s.label + ' overlaps ' + other.label);
    }
    rows.set(s.labelRow, (rows.get(s.labelRow) || []).concat([s]));
  }
  const plain = HubDocs.layoutTimeline(clone(SCAFFOLD.tasks[0].timeline));
  assert.equal(plain.labelRows, 1, 'a timeline that fits keeps one tier');
  assert.equal(layout.labelShift, (layout.labelRows - 1) * 36);
  const figure = HubDocs.timelineSvg({ ...SCAFFOLD.tasks[0], timeline }, options());
  const texts = byTag(figure, 'text');
  const label = (name) => texts.find((t) => t.textContent === name);
  assert.notEqual(label('Block marker').getAttribute('y'), label('Acquire fixation').getAttribute('y'));
  const body = all(figure).find((e) => (e.getAttribute('class') || '') === 'hd-tl-body');
  assert.equal(body.getAttribute('transform'), 'translate(0,' + layout.labelShift + ')');
});

test('the stimulus figure draws the source geometry to scale', () => {
  const task = SCAFFOLD.tasks[0];
  const figure = HubDocs.diagramSvg(task, options());
  const layout = HubDocs.layoutDiagram(task.diagram);
  const circles = byTag(figure, 'circle').filter((c) => / hd-role-/.test(' ' + (c.getAttribute('class') || '')));
  const [window, point] = circles;
  assert.ok(Math.abs(Number(window.getAttribute('r')) - 2 * layout.ppu) < 1e-9);
  assert.ok(Math.abs(Number(point.getAttribute('r')) - 0.15 * layout.ppu) < 1e-9);
  assert.match(window.getAttribute('class'), /hd-dashed/);
  assert.equal(point.getAttribute('fill'), 'rgb(255,255,255)');
  const panel = byTag(figure, 'rect').find((r) => r.getAttribute('class') === 'hd-dg-panel');
  assert.equal(panel.getAttribute('fill'), 'rgb(128,128,128)');
  assert.match(byTag(figure, 'desc')[0].textContent, /Gaze window: dashed circle, radius 2 dva/);
  assert.ok(byTag(figure, 'text').some((t) => t.textContent === '2 dva'));
  assert.ok(layout.scaleBar);
  assertNoInlineBehaviour(figure);
});

test('the task guide holds every section, and a parameter mention jumps to its row', () => {
  const opts = options();
  const view = HubDocs.renderTaskGuide(SCAFFOLD, 'fixation-demo', opts);
  opts.document.body.appendChild(view);
  const sections = view.querySelectorAll('section').map((s) => s.getAttribute('data-hd-section'));
  assert.deepEqual(Array.from(sections), ['description', 'stimulus', 'timeline', 'parameters', 'outcomes', 'events']);
  const rows = view.querySelectorAll('tr.hd-param');
  assert.equal(rows.length, SCAFFOLD.tasks[0].parameters.length);
  assert.equal(view.querySelectorAll('figure').length, 2);
  assert.match(view.textContent, /Figure 1\. Stimulus\./);
  assert.match(view.textContent, /Figure 2\. Trial timeline\./);
  assert.match(view.textContent, /Declared; see alhazen\.paradigms\.config:SchedulerConfig/);

  const mention = view.querySelectorAll('button.hd-ref-param').find((b) => b.textContent === '2000 ms');
  assert.ok(mention, 'the description mentions acquire_timeout');
  mention.fire('click', { target: mention });
  const target = view.querySelector('[data-hd-param="fixation-demo/acquire_timeout"]');
  assert.ok(target);
  assert.equal(opts.document.scrolledIntoView.at(-1).element, target);
  assert.equal(opts.document.focused.at(-1).element, target);
  assert.equal(target.getAttribute('tabindex'), '-1');
  assertNoInlineBehaviour(view);
});

test('task links come from the parent and must stay on this origin', () => {
  for (const href of ['https://evil.example/x', '//evil.example/x', 'javascript:alert(1)', null]) {
    const view = HubDocs.renderMethods(SCAFFOLD, options({ taskHref: () => href }));
    assert.equal(byTag(view, 'a').filter((a) => a.getAttribute('class') === 'hd-task-link').length, 0, String(href));
  }
  const view = HubDocs.renderMethods(SCAFFOLD, options());
  const links = byTag(view, 'a').filter((a) => (a.getAttribute('class') || '').startsWith('hd-task-link'));
  assert.ok(links.length >= 2, 'the [[task:]] mention and the task index');
  assert.ok(links.every((a) => a.getAttribute('href') === '/hub?view=experiment&task=fixation-demo'));
});

test('methods render the authors text, references and provenance', () => {
  const view = HubDocs.renderMethods(SCAFFOLD, options());
  assert.match(view.textContent, /Methods · version 0\.1\.0/);
  assert.match(view.textContent, /From docs\/methods\.md in fixation-demo 0\.1\.0/);
  assert.ok(byTag(view, 'h3').some((h) => h.textContent === 'Stimuli'));
  // Methods mention parameters of a task shown elsewhere: values, not jumps.
  assert.ok(view.querySelectorAll('span.hd-ref-param--other').length > 0);
  assert.equal(view.querySelectorAll('ol').filter((o) => o.getAttribute('class') === 'hd-references').length, 1);
});

test('missing documentation is said plainly, never filled in', () => {
  const methods = HubDocs.renderMethods(null, options());
  assert.equal(methods.getAttribute('role'), 'status');
  assert.match(methods.textContent, /No methods for this version/);
  const task = HubDocs.renderTaskGuide(SCAFFOLD, 'no-such-task', options());
  assert.match(task.textContent, /No documentation for this task/);
  assert.match(HubDocs.renderGlobalGuide(null, options()).textContent, /not available/);
});

test('the guide shows every mode in order, with its sources', () => {
  const view = HubDocs.renderGlobalGuide(GUIDE, options());
  const names = view.querySelectorAll('code.hd-mode-name').map((n) => n.textContent);
  assert.deepEqual(Array.from(names), GUIDE.modes.map((m) => m.id));
  assert.deepEqual(Array.from(names), ['measure', 'demo', 'movie', 'simulate', 'test', 'run', 'training']);
  const sections = view.querySelectorAll('section').map((s) => s.getAttribute('data-hd-section'));
  assert.deepEqual(Array.from(sections), ['modes', ...GUIDE.sections.map((s) => s.id)]);
  assert.match(view.textContent, /Source: alhazen\.core\.engine:TrialEngine/);
  assert.ok(!/api key|provider key/i.test(view.textContent));
  assertNoInlineBehaviour(view);
});


test('stimulus schematics keep their authored label size in a focusable narrow-pane scroller', () => {
  const view = HubDocs.renderTaskGuide(SCAFFOLD, 'fixation-demo', options());
  const figure = view.querySelector('figure.hd-figure--diagram');
  const canvas = figure.querySelector('.hd-figure-canvas');
  assert.match(canvas.getAttribute('class'), /hd-figure-canvas--wide/);
  assert.equal(canvas.getAttribute('tabindex'), '0');
  assert.equal(canvas.getAttribute('role'), 'group');
  assert.match(canvas.getAttribute('aria-label'), /Stimulus.*scrolls sideways/);
  const drawing = canvas.querySelector('svg');
  assert.ok(Number(drawing.getAttribute('width')) >= 400);
  assert.match(figure.querySelector('.hd-scroll-note').textContent, /Swipe or scroll sideways/);
});


test('mode labels do not call training or rehearsal records unreal', () => {
  const view = HubDocs.renderGlobalGuide(GUIDE, options());
  const modes = new Map(view.querySelectorAll('li.hd-mode').map((item) => [item.querySelector('code.hd-mode-name').textContent, item.textContent]));
  assert.match(modes.get('training'), /real subject.*separate training records/);
  assert.match(modes.get('test'), /subject rehearsal.*separate rehearsal records/);
  assert.match(modes.get('simulate'), /simulated subject.*separate rehearsal records/);
  assert.match(modes.get('run'), /real subject.*experiment records/);
  assert.ok(!view.textContent.includes('no real data'));
  assert.ok(!view.textContent.includes('no subject driven'));
});
