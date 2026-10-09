/* Experiment Hub: scientific documentation views (Methods, task guides, the
 * alhazen Guide).
 *
 * The data comes resolved and validated from alhazen.hub.documentation
 * (read_documentation for one package version, global_guide for alhazen
 * itself); this file only draws it. What it hides is how that data becomes
 * readable, accessible DOM without ever becoming markup:
 *
 * - Every string is text: elements are built with createElement /
 *   createElementNS and filled with text nodes, never innerHTML. Markdown is a
 *   small subset parsed here into a tree (headings, paragraphs, lists, block
 *   quotes, code, simple tables, emphasis, inline code, https links and
 *   [[param:...]] / [[task:...]] references); raw HTML in it stays visible text.
 *   Images are not fetched: an image reference reads as its alt text.
 * - Figures are SVG drawn from the descriptor's closed grammar, with
 *   presentation attributes only (the page's CSP has no inline styles). A
 *   timeline draws a phase to scale only when its duration is known; a phase
 *   that waits on the subject or happens only sometimes is drawn hatched with
 *   break marks and labelled with its rule, never given a fake length.
 * - Nothing here routes or fetches: the parent mounts what these functions
 *   return, and task links come from options.taskHref. Parameter mentions are
 *   buttons that scroll to the parameter's row (the rig page owns the URL
 *   fragment, so no hash is ever written).
 *
 * Interface: HubDocs = {SCHEMA_VERSION, renderMethods, renderTaskGuide,
 *   renderTaskIndex, renderGlobalGuide, renderMissing, renderMarkdown,
 *   timelineSvg, diagramSvg, parseMarkdown, parseInline, layoutTimeline,
 *   layoutDiagram, formatValue, resolveParameterReference}
 * options = {document, taskHref(taskId) -> string|null, headingLevel (2),
 *   idPrefix ('hd-')}.
 */
'use strict';

const HubDocs = (() => {
  const SCHEMA_VERSION = 1;
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const MAX_MARKDOWN_CHARS = 600000;
  const MAX_INLINE_DEPTH = 4;
  let figureCounter = 0;

  /* ------------------------------------------------------------------ */
  /* Construction helpers                                                */
  /* ------------------------------------------------------------------ */
  function context(options) {
    const opts = options || {};
    const doc = opts.document || (typeof document !== 'undefined' ? document : null);
    if (!doc) throw new Error('HubDocs needs options.document outside a browser');
    const level = Number.isInteger(opts.headingLevel) ? Math.min(Math.max(opts.headingLevel, 1), 5) : 2;
    const prefix = typeof opts.idPrefix === 'string' && /^[a-z][a-z0-9-]{0,20}$/.test(opts.idPrefix)
      ? opts.idPrefix : 'hd-';
    const taskHref = typeof opts.taskHref === 'function' ? opts.taskHref : () => null;
    return { doc, level, prefix, taskHref, root: null };
  }

  function el(c, tag, className, text) {
    const node = c.doc.createElement(tag);
    if (className) node.setAttribute('class', className);
    if (text !== undefined && text !== null && text !== '') node.appendChild(c.doc.createTextNode(String(text)));
    return node;
  }

  function textNode(c, text) {
    return c.doc.createTextNode(String(text));
  }

  function svg(c, tag, attributes, text) {
    const node = c.doc.createElementNS(SVG_NS, tag);
    for (const [name, value] of Object.entries(attributes || {})) {
      if (value !== undefined && value !== null) node.setAttribute(name, String(value));
    }
    if (text !== undefined && text !== null && text !== '') node.appendChild(c.doc.createTextNode(String(text)));
    return node;
  }

  function heading(c, offset, text, id) {
    const level = Math.min(c.level + offset, 6);
    const node = el(c, 'h' + level, 'hd-h hd-h' + Math.min(offset + 1, 4), text);
    if (id) node.setAttribute('id', id);
    return node;
  }

  function safeId(value) {
    return String(value).toLowerCase().replace(/[^a-z0-9_-]+/g, '-').slice(0, 80);
  }

  function nextFigureId(c) {
    figureCounter += 1;
    return c.prefix + 'fig' + figureCounter;
  }

  /* An internal jump: focus and scroll to an element inside the rendered
   * view. Buttons rather than links, so the URL is never touched. */
  function jumpButton(c, label, targetSelector, className) {
    const button = el(c, 'button', className || 'hd-jump', label);
    button.setAttribute('type', 'button');
    button.addEventListener('click', () => {
      const root = c.root;
      const target = root && root.querySelector(targetSelector);
      if (!target) return;
      if (typeof target.scrollIntoView === 'function') target.scrollIntoView({ block: 'center', behavior: 'smooth' });
      if (typeof target.focus === 'function') target.focus({ preventScroll: true });
      if (target.classList) {
        target.classList.add('hd-flash');
        setTimeout(() => target.classList.remove('hd-flash'), 1600);
      }
    });
    return button;
  }

  function taskLink(c, task, text) {
    const href = c.taskHref(task.id);
    if (typeof href === 'string' && href.startsWith('/') && !href.startsWith('//')) {
      const link = el(c, 'a', 'hd-task-link', text || task.title);
      link.setAttribute('href', href);
      return link;
    }
    return el(c, 'span', 'hd-task-link hd-task-link--plain', text || task.title);
  }

  /* ------------------------------------------------------------------ */
  /* Values (same rule as documentation._format_value)                   */
  /* ------------------------------------------------------------------ */
  function formatNumber(value) {
    if (Number.isInteger(value) && Math.abs(value) < 1e15) return String(value);
    if (Number.isFinite(value) && value === Math.round(value) && Math.abs(value) < 1e15) return String(Math.round(value));
    return String(Number(value.toPrecision(6)));
  }

  function formatValue(value, unit) {
    if (typeof value === 'boolean') return value ? 'true' : 'false';
    if (typeof value === 'number') return formatNumber(value) + (unit ? ' ' + unit : '');
    if (typeof value === 'string') return value;
    if (value === null || value === undefined) return 'none';
    if (Array.isArray(value)) return '[' + value.map((item) => formatValue(item, null)).join(', ') + ']';
    if (typeof value === 'object') {
      const keys = Object.keys(value);
      if (keys.length === 1 && keys[0] === 'ms' && typeof value.ms === 'number') return formatNumber(value.ms) + ' ms';
      if (keys.length === 1 && keys[0] === 'frames' && typeof value.frames === 'number') return formatNumber(value.frames) + ' frames';
      return keys.map((key) => key + ': ' + formatValue(value[key], null)).join(', ');
    }
    return String(value);
  }

  /* ------------------------------------------------------------------ */
  /* Markdown subset -> tree (pure)                                      */
  /* ------------------------------------------------------------------ */
  const SAFE_LINK = /^https?:\/\/[^\s<>"'`]+$/i;

  function parseInline(text, depth) {
    const level = depth || 0;
    const tokens = [];
    let buffer = '';
    let i = 0;
    const flush = () => { if (buffer) { tokens.push({ type: 'text', text: buffer }); buffer = ''; } };
    const source = String(text);
    while (i < source.length) {
      const rest = source.slice(i);
      let match;
      if (rest[0] === '\\' && rest.length > 1 && /[\\`*_[\]()!#>|-]/.test(rest[1])) {
        buffer += rest[1]; i += 2; continue;
      }
      if ((match = /^\[\[([a-z]+):([^\]\s]{1,80})\]\]/.exec(rest))) {
        flush(); tokens.push({ type: 'ref', kind: match[1], target: match[2] }); i += match[0].length; continue;
      }
      if ((match = /^`([^`]+)`/.exec(rest))) {
        flush(); tokens.push({ type: 'code', text: match[1] }); i += match[0].length; continue;
      }
      if ((match = /^!\[([^\]]*)\]\(([^)\s]*)\)/.exec(rest))) {
        flush(); tokens.push({ type: 'image', alt: match[1] }); i += match[0].length; continue;
      }
      if ((match = /^\[([^\]]+)\]\(([^)\s]+)\)/.exec(rest))) {
        flush();
        const children = level < MAX_INLINE_DEPTH ? parseInline(match[1], level + 1) : [{ type: 'text', text: match[1] }];
        if (SAFE_LINK.test(match[2])) tokens.push({ type: 'link', href: match[2], children });
        else tokens.push({ type: 'text', text: match[0] });
        i += match[0].length; continue;
      }
      if (level < MAX_INLINE_DEPTH && (match = /^(\*\*|__)(?=\S)([\s\S]+?\S)\1/.exec(rest))) {
        flush(); tokens.push({ type: 'strong', children: parseInline(match[2], level + 1) }); i += match[0].length; continue;
      }
      if (level < MAX_INLINE_DEPTH && (match = /^(\*|_)(?=\S)([\s\S]+?\S)\1(?![*_\w])/.exec(rest))
          && (match[1] === '*' || !/\w/.test(source[i - 1] || ''))) {
        flush(); tokens.push({ type: 'em', children: parseInline(match[2], level + 1) }); i += match[0].length; continue;
      }
      buffer += rest[0]; i += 1;
    }
    flush();
    return tokens;
  }

  function splitRow(line) {
    let row = line.trim();
    if (row.startsWith('|')) row = row.slice(1);
    if (row.endsWith('|') && !row.endsWith('\\|')) row = row.slice(0, -1);
    return row.split(/(?<!\\)\|/).map((cell) => cell.trim().replace(/\\\|/g, '|'));
  }

  function parseMarkdown(text) {
    const source = String(text || '').replace(/\r\n?/g, '\n').slice(0, MAX_MARKDOWN_CHARS);
    const lines = source.split('\n');
    const blocks = [];
    let i = 0;
    const isBlank = (line) => /^\s*$/.test(line);
    const startsBlock = (line) => /^(#{1,6})\s|^```|^\s*([-*+]|\d{1,3}[.)])\s+|^>\s?|^(\*\s*){3,}$|^(-\s*){3,}$/.test(line);
    while (i < lines.length) {
      const line = lines[i];
      let match;
      if (isBlank(line)) { i += 1; continue; }
      if ((match = /^(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line))) {
        blocks.push({ type: 'heading', level: match[1].length, inline: parseInline(match[2]) });
        i += 1; continue;
      }
      if (/^```/.test(line)) {
        const body = [];
        i += 1;
        while (i < lines.length && !/^```/.test(lines[i])) { body.push(lines[i]); i += 1; }
        i += 1;
        blocks.push({ type: 'code', text: body.join('\n') });
        continue;
      }
      if (/^(\*\s*){3,}$|^(-\s*){3,}$|^(_\s*){3,}$/.test(line.trim())) {
        blocks.push({ type: 'rule' }); i += 1; continue;
      }
      if (/^>\s?/.test(line)) {
        const body = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) { body.push(lines[i].replace(/^>\s?/, '')); i += 1; }
        blocks.push({ type: 'quote', inline: parseInline(body.join(' ')) });
        continue;
      }
      if ((match = /^\s*([-*+]|\d{1,3}[.)])\s+/.exec(line))) {
        const ordered = /\d/.test(match[1]);
        const items = [];
        while (i < lines.length) {
          const item = /^\s*([-*+]|\d{1,3}[.)])\s+(.*)$/.exec(lines[i]);
          if (item && /\d/.test(item[1]) === ordered) { items.push(item[2]); i += 1; continue; }
          if (!item && !isBlank(lines[i]) && /^\s{2,}\S/.test(lines[i]) && items.length) {
            items[items.length - 1] += ' ' + lines[i].trim(); i += 1; continue;
          }
          break;
        }
        blocks.push({ type: 'list', ordered, items: items.map((item) => parseInline(item)) });
        continue;
      }
      if (line.includes('|') && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(lines[i + 1])) {
        const header = splitRow(line);
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].includes('|') && !isBlank(lines[i])) {
          const cells = splitRow(lines[i]).slice(0, header.length);
          while (cells.length < header.length) cells.push('');
          rows.push(cells.map((cell) => parseInline(cell)));
          i += 1;
        }
        blocks.push({ type: 'table', header: header.map((cell) => parseInline(cell)), rows });
        continue;
      }
      const body = [line];
      i += 1;
      while (i < lines.length && !isBlank(lines[i]) && !startsBlock(lines[i])) { body.push(lines[i]); i += 1; }
      blocks.push({ type: 'paragraph', inline: parseInline(body.map((part) => part.trim()).join(' ')) });
    }
    return blocks;
  }

  /* ``task-id/name`` names one task's parameter; a bare name is the current
   * task's, or (in the methods) the one task that has it. Same rule as
   * documentation._resolve_parameter_reference. */
  function resolveParameterReference(target, task, tasks) {
    const list = Array.isArray(tasks) ? tasks : [];
    const find = (candidate, name) => (candidate.parameters || []).find((p) => p.name === name) || null;
    if (target.includes('/')) {
      const [taskId, name] = [target.slice(0, target.indexOf('/')), target.slice(target.indexOf('/') + 1)];
      const owner = list.find((candidate) => candidate.id === taskId);
      const parameter = owner && find(owner, name);
      return parameter ? { task: owner, parameter } : null;
    }
    if (task) {
      const parameter = find(task, target);
      return parameter ? { task, parameter } : null;
    }
    const owners = list.filter((candidate) => find(candidate, target));
    return owners.length === 1 ? { task: owners[0], parameter: find(owners[0], target) } : null;
  }

  /* ------------------------------------------------------------------ */
  /* Markdown tree -> DOM                                                */
  /* ------------------------------------------------------------------ */
  function paramSelector(c, task, name) {
    return '[data-hd-param="' + safeId(task.id) + '/' + name.replace(/[^A-Za-z0-9_.]/g, '') + '"]';
  }

  function renderInline(c, tokens, scope, parent) {
    for (const token of tokens) {
      if (token.type === 'text') parent.appendChild(textNode(c, token.text));
      else if (token.type === 'code') parent.appendChild(el(c, 'code', 'hd-code', token.text));
      else if (token.type === 'strong' || token.type === 'em') {
        const node = el(c, token.type, null);
        renderInline(c, token.children, scope, node);
        parent.appendChild(node);
      } else if (token.type === 'link') {
        const link = el(c, 'a', 'hd-link');
        link.setAttribute('href', token.href);
        link.setAttribute('rel', 'noopener noreferrer external');
        link.setAttribute('target', '_blank');
        renderInline(c, token.children, scope, link);
        parent.appendChild(link);
      } else if (token.type === 'image') {
        parent.appendChild(el(c, 'span', 'hd-image-omitted', '[image not shown' + (token.alt ? ': ' + token.alt : '') + ']'));
      } else if (token.type === 'ref') {
        parent.appendChild(renderReference(c, token, scope));
      }
    }
  }

  function renderReference(c, token, scope) {
    const tasks = (scope.doc && scope.doc.tasks) || [];
    if (token.kind === 'task') {
      const task = tasks.find((candidate) => candidate.id === token.target);
      return task ? taskLink(c, task) : el(c, 'span', 'hd-ref-broken', token.target);
    }
    if (token.kind === 'param') {
      const found = resolveParameterReference(token.target, scope.task, tasks);
      if (!found) return el(c, 'span', 'hd-ref-broken', token.target);
      const value = found.parameter.has_default ? found.parameter.default_text : 'no default';
      const label = value;
      if (scope.task && found.task.id === scope.task.id) {
        const button = jumpButton(c, label, paramSelector(c, found.task, found.parameter.name), 'hd-ref hd-ref-param');
        button.setAttribute('title', found.parameter.label + ' (' + found.parameter.name + ')');
        button.setAttribute('aria-label', found.parameter.label + ': ' + value + '. Show in the parameter table');
        return button;
      }
      const wrapper = el(c, 'span', 'hd-ref hd-ref-param hd-ref-param--other');
      wrapper.appendChild(el(c, 'span', 'hd-ref-value', label));
      wrapper.setAttribute('title', found.parameter.label + ' (' + found.task.id + ' / ' + found.parameter.name + ')');
      return wrapper;
    }
    return el(c, 'span', 'hd-ref-broken', token.kind + ':' + token.target);
  }

  function renderMarkdownInto(c, text, scope, container) {
    const blocks = parseMarkdown(text);
    /* The shallowest heading the authors wrote sits one level under the
     * view's own title, whether they started at # or at ##. */
    const top = Math.min(7, ...blocks.filter((b) => b.type === 'heading').map((b) => b.level));
    for (const block of blocks) {
      let node;
      if (block.type === 'heading') {
        const depth = block.level - top + 1;
        node = el(c, 'h' + Math.min(c.level + depth, 6), 'hd-md-h hd-md-h' + Math.min(depth, 4));
        renderInline(c, block.inline, scope, node);
      } else if (block.type === 'paragraph') {
        node = el(c, 'p', null);
        renderInline(c, block.inline, scope, node);
      } else if (block.type === 'quote') {
        node = el(c, 'blockquote', 'hd-quote');
        const p = el(c, 'p', null);
        renderInline(c, block.inline, scope, p);
        node.appendChild(p);
      } else if (block.type === 'code') {
        node = el(c, 'pre', 'hd-pre');
        node.appendChild(el(c, 'code', null, block.text));
      } else if (block.type === 'rule') {
        node = el(c, 'hr', 'hd-rule');
      } else if (block.type === 'list') {
        node = el(c, block.ordered ? 'ol' : 'ul', 'hd-list');
        for (const item of block.items) {
          const li = el(c, 'li', null);
          renderInline(c, item, scope, li);
          node.appendChild(li);
        }
      } else if (block.type === 'table') {
        node = el(c, 'div', 'hd-table-wrap');
        const table = el(c, 'table', 'hd-table hd-md-table');
        const head = el(c, 'thead', null);
        const headRow = el(c, 'tr', null);
        for (const cell of block.header) {
          const th = el(c, 'th', null);
          th.setAttribute('scope', 'col');
          renderInline(c, cell, scope, th);
          headRow.appendChild(th);
        }
        head.appendChild(headRow);
        table.appendChild(head);
        const body = el(c, 'tbody', null);
        for (const row of block.rows) {
          const tr = el(c, 'tr', null);
          for (const cell of row) {
            const td = el(c, 'td', null);
            renderInline(c, cell, scope, td);
            tr.appendChild(td);
          }
          body.appendChild(tr);
        }
        table.appendChild(body);
        node.appendChild(table);
      }
      if (node) container.appendChild(node);
    }
    return container;
  }

  function renderMarkdown(text, options, scope) {
    const c = context(options);
    const container = el(c, 'div', 'hd-prose');
    c.root = container;
    return renderMarkdownInto(c, text, scope || {}, container);
  }

  /* ------------------------------------------------------------------ */
  /* Timeline geometry (pure)                                            */
  /* ------------------------------------------------------------------ */
  const TL = {
    pad: 16, phaseMin: 72, unscaled: 140, maxPxPerMs: 0.32, scaledBudget: 520,
    labelTop: 18, barY: 58, barH: 24, trackH: 30, eventH: 18, branchH: 24, gap: 34, endW: 112,
  };
  const NICE_MS = [10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000];

  function segmentWidth(timing, pxPerMs) {
    if (timing && timing.scaled && timing.ms === 0) return { width: 0, kind: 'instant', clamped: false };
    if (timing && timing.scaled && typeof timing.ms === 'number') {
      const natural = timing.ms * pxPerMs;
      return { width: Math.max(natural, TL.phaseMin), kind: 'scaled', clamped: natural < TL.phaseMin };
    }
    return { width: TL.unscaled, kind: 'unscaled', clamped: false };
  }

  function layoutTimeline(timeline) {
    const phases = (timeline && timeline.phases) || [];
    const between = timeline && timeline.between_trials;
    const timed = phases.map((p) => p.timing).concat(between ? [between.timing] : []);
    const scaledMs = timed.filter((t) => t && t.scaled && t.ms > 0).reduce((sum, t) => sum + t.ms, 0);
    const pxPerMs = scaledMs > 0 ? Math.min(TL.maxPxPerMs, TL.scaledBudget / scaledMs) : 0;
    let x = TL.pad;
    const segments = phases.map((phase) => {
      const size = segmentWidth(phase.timing, pxPerMs);
      const segment = {
        id: phase.id, label: phase.label, text: phase.timing.text, x, width: size.width,
        kind: size.kind, clamped: size.clamped, conditional: phase.timing.kind === 'conditional',
        timingKind: phase.timing.kind,
      };
      x += size.width;
      return segment;
    });
    const trialEnd = x;
    const end = timeline && timeline.end ? { x: trialEnd, outcome: timeline.end.outcome, label: timeline.end.label } : null;
    x += end ? TL.endW : 24;
    let betweenSegment = null;
    if (between) {
      x += TL.gap;
      const size = segmentWidth(between.timing, pxPerMs);
      const width = size.kind === 'instant' ? TL.phaseMin : size.width;
      betweenSegment = { label: between.label, text: between.timing.text, x, width, kind: size.kind === 'instant' ? 'scaled' : size.kind, clamped: size.clamped };
      x += width;
    }
    const width = Math.ceil(x + TL.pad);
    const byId = new Map(segments.map((s) => [s.id, s]));
    let y = TL.barY + TL.barH + 14;
    const tracks = ((timeline && timeline.tracks) || []).map((track) => {
      const from = byId.get(track.from);
      const to = byId.get(track.to);
      const row = { label: track.label, role: track.role, x: from.x, width: Math.max(to.x + to.width - from.x, 2), y };
      y += TL.trackH;
      return row;
    });
    /* Event labels: one row per collision, so two events at one moment
     * stack instead of overprinting. */
    const marks = [];
    for (const segment of segments) {
      const phase = phases.find((p) => p.id === segment.id);
      for (const name of phase.start_events || []) marks.push({ name, x: segment.x, at: 'start', phase: segment.id });
      for (const name of phase.end_events || []) marks.push({ name, x: segment.x + segment.width, at: 'end', phase: segment.id });
    }
    const rowsEnd = [];
    const events = marks.map((mark) => {
      const span = mark.name.length * 6.6 + 12;
      let row = 0;
      while (rowsEnd[row] !== undefined && rowsEnd[row] > mark.x - 4) row += 1;
      rowsEnd[row] = mark.x + span;
      return { ...mark, row };
    });
    const eventTop = y + 6;
    y = eventTop + (events.length ? (Math.max(...events.map((e) => e.row)) + 1) * TL.eventH + 8 : 0);
    const branches = ((timeline && timeline.branches) || []).map((branch) => {
      const origin = byId.get(branch.from);
      const row = branch.from === '*'
        ? { ...branch, anyPhase: true, x: TL.pad, x1: trialEnd, y }
        : { ...branch, anyPhase: false, x: origin.x + origin.width / 2, x1: null, y };
      y += TL.branchH;
      return row;
    });
    const unclamped = segments.concat(betweenSegment ? [betweenSegment] : []).some((s) => s.kind === 'scaled' && !s.clamped);
    let scaleBar = null;
    if (pxPerMs > 0 && unclamped) {
      const ms = NICE_MS.find((candidate) => candidate * pxPerMs >= 48) || NICE_MS[NICE_MS.length - 1];
      scaleBar = { ms, px: ms * pxPerMs, x: TL.pad, y: y + 14 };
      y += 30;
    }
    return {
      width, height: Math.ceil(y + TL.pad), pxPerMs, segments, end, between: betweenSegment,
      tracks, events, eventTop, branches, scaleBar, trialEnd,
      anyClamped: segments.some((s) => s.clamped) || Boolean(betweenSegment && betweenSegment.clamped),
      anyUnscaled: segments.some((s) => s.kind === 'unscaled') || Boolean(betweenSegment && betweenSegment.kind === 'unscaled'),
    };
  }

  function timelineDescription(task) {
    const timeline = task.timeline;
    const parts = timeline.phases.map((phase) => phase.label + ': ' + phase.timing.text + '.');
    if (timeline.end) parts.push('Then the trial ends as ' + timeline.end.outcome + '.');
    for (const branch of timeline.branches) {
      parts.push((branch.from === '*' ? 'In any phase' : 'During ' + phaseLabel(timeline, branch.from))
        + ', if ' + branch.when + ': ' + branch.outcome + (branch.effect ? ' (' + branch.effect + ')' : '') + '.');
    }
    if (timeline.between_trials) parts.push(timeline.between_trials.label + ': ' + timeline.between_trials.timing.text + '.');
    return parts.join(' ');
  }

  function phaseLabel(timeline, id) {
    const phase = timeline.phases.find((p) => p.id === id);
    return phase ? phase.label : id;
  }

  function timelineSvg(task, options) {
    const c = context(options);
    const layout = layoutTimeline(task.timeline);
    const id = nextFigureId(c);
    const root = svg(c, 'svg', {
      class: 'hd-svg hd-timeline-svg', viewBox: '0 0 ' + layout.width + ' ' + layout.height,
      width: layout.width, height: layout.height, role: 'img',
      'aria-labelledby': id + '-title ' + id + '-desc', focusable: 'false',
    });
    root.appendChild(svg(c, 'title', { id: id + '-title' }, 'Trial timeline: ' + task.title));
    root.appendChild(svg(c, 'desc', { id: id + '-desc' }, timelineDescription(task)));
    const defs = svg(c, 'defs');
    const hatch = svg(c, 'pattern', { id: id + '-hatch', width: 7, height: 7, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' });
    hatch.appendChild(svg(c, 'rect', { width: 7, height: 7, class: 'hd-tl-hatch-bg' }));
    hatch.appendChild(svg(c, 'line', { x1: 0, y1: 0, x2: 0, y2: 7, class: 'hd-tl-hatch-line' }));
    defs.appendChild(hatch);
    const arrow = svg(c, 'marker', { id: id + '-arrow', viewBox: '0 0 8 8', refX: 7, refY: 4, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse' });
    arrow.appendChild(svg(c, 'path', { d: 'M0,0 L8,4 L0,8 z', class: 'hd-tl-arrowhead' }));
    defs.appendChild(arrow);
    root.appendChild(defs);

    const drawSegment = (segment, group) => {
      const g = svg(c, 'g', { class: 'hd-tl-phase hd-tl-' + segment.kind + (segment.conditional ? ' hd-tl-conditional' : '') });
      if (segment.kind === 'instant') {
        g.appendChild(svg(c, 'line', { x1: segment.x, y1: TL.barY - 4, x2: segment.x, y2: TL.barY + TL.barH + 4, class: 'hd-tl-instant' }));
      } else {
        g.appendChild(svg(c, 'rect', {
          x: segment.x, y: TL.barY, width: segment.width, height: TL.barH,
          class: 'hd-tl-bar', fill: segment.kind === 'unscaled' ? 'url(#' + id + '-hatch)' : null,
        }));
        if (segment.kind === 'unscaled') {
          /* Break marks: this length is not a duration. */
          for (const bx of [segment.x + 10, segment.x + segment.width - 18]) {
            g.appendChild(svg(c, 'path', {
              d: 'M' + bx + ',' + (TL.barY - 5) + ' l4,' + (TL.barH + 10) + ' M' + (bx + 6) + ',' + (TL.barY - 5) + ' l4,' + (TL.barH + 10),
              class: 'hd-tl-break',
            }));
          }
        }
        if (segment.clamped) {
          g.appendChild(svg(c, 'text', { x: segment.x + segment.width - 4, y: TL.barY + TL.barH - 7, 'text-anchor': 'end', class: 'hd-tl-note' }, 'not to scale'));
        }
      }
      const labelX = segment.x + (segment.kind === 'instant' ? 0 : 6);
      g.appendChild(svg(c, 'text', { x: labelX, y: TL.labelTop, class: 'hd-tl-label' }, segment.label));
      g.appendChild(svg(c, 'text', { x: labelX, y: TL.labelTop + 17, class: 'hd-tl-time' }, shortTiming(segment)));
      group.appendChild(g);
    };
    const phases = svg(c, 'g', { class: 'hd-tl-phases' });
    layout.segments.forEach((segment) => drawSegment(segment, phases));
    if (layout.between) {
      const gapX = layout.between.x - TL.gap / 2;
      phases.appendChild(svg(c, 'path', { d: 'M' + (gapX - 5) + ',' + (TL.barY + 2) + ' l4,' + (TL.barH - 4) + ' M' + (gapX + 1) + ',' + (TL.barY + 2) + ' l4,' + (TL.barH - 4), class: 'hd-tl-gapmark' }));
      phases.appendChild(svg(c, 'text', { x: gapX, y: TL.barY + TL.barH + 14, 'text-anchor': 'middle', class: 'hd-tl-note' }, 'after the trial'));
      drawSegment({ ...layout.between, id: 'between', conditional: false }, phases);
    }
    root.appendChild(phases);

    /* Trial boundary and the outcome it ends with. */
    root.appendChild(svg(c, 'line', { x1: layout.trialEnd, y1: TL.barY - 10, x2: layout.trialEnd, y2: layout.height - TL.pad, class: 'hd-tl-boundary' }));
    if (layout.end) {
      root.appendChild(svg(c, 'line', { x1: layout.trialEnd + 4, y1: TL.barY + TL.barH / 2, x2: layout.trialEnd + 26, y2: TL.barY + TL.barH / 2, class: 'hd-tl-endarrow', 'marker-end': 'url(#' + id + '-arrow)' }));
      root.appendChild(svg(c, 'text', { x: layout.trialEnd + 30, y: TL.barY + TL.barH / 2 + 4, class: 'hd-tl-outcome hd-tl-outcome--end' }, layout.end.outcome));
    }

    const tracks = svg(c, 'g', { class: 'hd-tl-tracks' });
    for (const track of layout.tracks) {
      tracks.appendChild(svg(c, 'rect', { x: track.x, y: track.y + 12, width: track.width, height: 7, rx: 1.5, class: 'hd-tl-track hd-tl-track--' + track.role }));
      tracks.appendChild(svg(c, 'text', { x: track.x + 2, y: track.y + 8, class: 'hd-tl-track-label' }, track.label));
    }
    root.appendChild(tracks);

    const events = svg(c, 'g', { class: 'hd-tl-events' });
    for (const event of layout.events) {
      const y = layout.eventTop + event.row * TL.eventH;
      events.appendChild(svg(c, 'line', { x1: event.x, y1: TL.barY + TL.barH, x2: event.x, y2: y + 12, class: 'hd-tl-event-tick' }));
      events.appendChild(svg(c, 'text', { x: event.x + 4, y: y + 12, class: 'hd-tl-event' }, event.name));
    }
    root.appendChild(events);

    const branches = svg(c, 'g', { class: 'hd-tl-branches' });
    for (const branch of layout.branches) {
      if (branch.anyPhase) {
        branches.appendChild(svg(c, 'line', { x1: branch.x, y1: branch.y + 8, x2: branch.x1, y2: branch.y + 8, class: 'hd-tl-abort-span' }));
        branches.appendChild(svg(c, 'text', { x: branch.x1 + 8, y: branch.y + 12, class: 'hd-tl-outcome hd-tl-outcome--' + branch.kind }, branch.outcome + ' (any phase)'));
      } else {
        branches.appendChild(svg(c, 'path', { d: 'M' + branch.x + ',' + (TL.barY + TL.barH) + ' V' + (branch.y + 8) + ' h14', class: 'hd-tl-branch hd-tl-branch--' + branch.kind, 'marker-end': 'url(#' + id + '-arrow)' }));
        branches.appendChild(svg(c, 'text', { x: branch.x + 20, y: branch.y + 12, class: 'hd-tl-outcome hd-tl-outcome--' + branch.kind }, branch.outcome));
      }
    }
    root.appendChild(branches);

    if (layout.scaleBar) {
      const bar = layout.scaleBar;
      const g = svg(c, 'g', { class: 'hd-tl-scale' });
      g.appendChild(svg(c, 'path', { d: 'M' + bar.x + ',' + (bar.y - 4) + ' v4 h' + bar.px + ' v-4', class: 'hd-tl-scalebar' }));
      g.appendChild(svg(c, 'text', { x: bar.x + bar.px + 6, y: bar.y + 3, class: 'hd-tl-note' }, formatNumber(bar.ms) + ' ms'));
      root.appendChild(g);
    }
    return root;
  }

  function shortTiming(segment) {
    if (segment.kind === 'instant') return 'instant';
    if (segment.timingKind === 'event') return 'waits on an event';
    if (segment.conditional) return 'only sometimes';
    return segment.text;
  }

  /* ------------------------------------------------------------------ */
  /* Stimulus schematic (pure geometry, then SVG)                        */
  /* ------------------------------------------------------------------ */
  const DG = { maxWidth: 480, maxHeight: 360, pad: 14 };
  const NICE_DVA = [0.1, 0.2, 0.25, 0.5, 1, 2, 2.5, 5, 10, 20, 50];

  function layoutDiagram(diagram) {
    const ppu = Math.min(DG.maxWidth / diagram.width, DG.maxHeight / diagram.height);
    const w = diagram.width * ppu;
    const h = diagram.height * ppu;
    const toX = (x) => DG.pad + (x + diagram.width / 2) * ppu;
    const toY = (y) => DG.pad + (diagram.height / 2 - y) * ppu;
    const unit = NICE_DVA.find((candidate) => candidate * ppu >= 40) || NICE_DVA[NICE_DVA.length - 1];
    let callout = 0;
    const callouts = [];
    for (const element of diagram.elements) {
      if (!element.label) continue;
      callout += 1;
      const anchor = anchorOf(element);
      const ax = Math.min(Math.max(toX(anchor[0]) + 10, DG.pad + 10), DG.pad + w - 10);
      const ay = Math.min(Math.max(toY(anchor[1]) - 10, DG.pad + 10), DG.pad + h - 10);
      callouts.push({ number: callout, element, x: ax, y: ay, px: toX(anchor[2]), py: toY(anchor[3]) });
    }
    return {
      ppu, width: Math.ceil(w + DG.pad * 2), height: Math.ceil(h + DG.pad * 2 + 26), canvas: { x: DG.pad, y: DG.pad, w, h },
      toX, toY, callouts, scaleBar: unit * ppu <= w * 0.6 ? { units: unit, px: unit * ppu } : null,
    };
  }

  /* A point just outside the element (for its numbered callout) and the
   * point on it the leader touches: [calloutX, calloutY, touchX, touchY]. */
  function anchorOf(element) {
    const diag = Math.SQRT1_2;
    switch (element.type) {
      case 'circle': return [element.cx + element.r * diag + 0.08 * element.r, element.cy + element.r * diag, element.cx + element.r * diag, element.cy + element.r * diag];
      case 'ellipse': return [element.cx + element.rx * diag, element.cy + element.ry * diag, element.cx + element.rx * diag, element.cy + element.ry * diag];
      case 'rect': case 'screen': return [element.cx + element.width / 2, element.cy + element.height / 2, element.cx + element.width / 2, element.cy + element.height / 2];
      case 'dot_field': case 'grating': return [element.cx + element.radius * diag, element.cy + element.radius * diag, element.cx + element.radius * diag, element.cy + element.radius * diag];
      case 'text': return [element.x, element.y, element.x, element.y];
      default: return [(element.x1 + element.x2) / 2, (element.y1 + element.y2) / 2, (element.x1 + element.x2) / 2, (element.y1 + element.y2) / 2];
    }
  }

  function grey(luminance) {
    const v = Math.round(Math.min(Math.max(luminance, 0), 1) * 255);
    return 'rgb(' + v + ',' + v + ',' + v + ')';
  }

  /* Deterministic dot positions in a disc (mulberry32): the same seed draws
   * the same field everywhere. */
  function dotPositions(count, seed) {
    let state = seed >>> 0;
    const random = () => {
      state = (state + 0x6d2b79f5) >>> 0;
      let t = state;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
    const points = [];
    for (let i = 0; i < count; i += 1) {
      const r = Math.sqrt(random());
      const angle = random() * Math.PI * 2;
      points.push([r * Math.cos(angle), r * Math.sin(angle)]);
    }
    return points;
  }

  function elementDescription(element, unit) {
    const v = (value) => formatNumber(value) + ' ' + unit;
    const at = (x, y) => ' at (' + formatNumber(x) + ', ' + formatNumber(y) + ')';
    switch (element.type) {
      case 'circle': return (element.dashed ? 'dashed ' : '') + 'circle, radius ' + v(element.r) + at(element.cx, element.cy);
      case 'ellipse': return 'ellipse, radii ' + v(element.rx) + ' by ' + v(element.ry) + at(element.cx, element.cy);
      case 'rect': return 'rectangle, ' + v(element.width) + ' by ' + v(element.height) + at(element.cx, element.cy);
      case 'screen': return 'display outline, ' + v(element.width) + ' by ' + v(element.height);
      case 'dot_field': return element.count + ' dots in a disc of radius ' + v(element.radius) + at(element.cx, element.cy);
      case 'grating': return 'grating, ' + formatNumber(element.cycles) + ' cycles across a disc of radius ' + v(element.radius) + ', orientation ' + formatNumber(element.orientation) + '°';
      case 'text': return 'text "' + element.text + '"';
      case 'dimension': return 'measurement' + (element.text ? ' of ' + element.text : '') + (element.value_text ? ': ' + element.value_text : '');
      case 'arrow': return 'arrow';
      default: return 'line';
    }
  }

  function diagramSvg(task, options) {
    const c = context(options);
    const diagram = task.diagram;
    const layout = layoutDiagram(diagram);
    const id = nextFigureId(c);
    const background = diagram.background === null || diagram.background === undefined ? null : diagram.background;
    const darkInk = background === null || background >= 0.45;
    const inkClass = darkInk ? 'hd-ink-dark' : 'hd-ink-light';
    const root = svg(c, 'svg', {
      class: 'hd-svg hd-diagram-svg ' + inkClass, viewBox: '0 0 ' + layout.width + ' ' + layout.height,
      width: layout.width, height: layout.height, role: 'img', 'aria-labelledby': id + '-title ' + id + '-desc', focusable: 'false',
    });
    root.appendChild(svg(c, 'title', { id: id + '-title' }, 'Stimulus schematic: ' + task.title));
    const described = diagram.elements.map((element) => (element.label ? element.label + ': ' : '') + elementDescription(element, diagram.unit));
    root.appendChild(svg(c, 'desc', { id: id + '-desc' }, 'Drawn to scale in ' + diagram.unit + ', ' + formatNumber(diagram.width) + ' by ' + formatNumber(diagram.height) + '. ' + described.join('; ') + '.'));
    const defs = svg(c, 'defs');
    const clipId = id + '-canvas';
    const clip = svg(c, 'clipPath', { id: clipId });
    clip.appendChild(svg(c, 'rect', { x: layout.canvas.x, y: layout.canvas.y, width: layout.canvas.w, height: layout.canvas.h }));
    defs.appendChild(clip);
    const arrow = svg(c, 'marker', { id: id + '-arrow', viewBox: '0 0 8 8', refX: 7, refY: 4, markerWidth: 6, markerHeight: 6, orient: 'auto-start-reverse' });
    arrow.appendChild(svg(c, 'path', { d: 'M0,0 L8,4 L0,8 z', class: 'hd-dg-arrowhead' }));
    defs.appendChild(arrow);
    root.appendChild(defs);
    root.appendChild(svg(c, 'rect', {
      x: layout.canvas.x, y: layout.canvas.y, width: layout.canvas.w, height: layout.canvas.h,
      class: 'hd-dg-panel', fill: background === null ? null : grey(background),
    }));
    const scene = svg(c, 'g', { 'clip-path': 'url(#' + clipId + ')' });
    const { toX, toY, ppu } = layout;
    diagram.elements.forEach((element, index) => {
      const cls = 'hd-dg hd-dg-' + element.type + ' hd-role-' + element.role + (element.dashed ? ' hd-dashed' : '');
      const fill = element.luminance === null || element.luminance === undefined ? null : grey(element.luminance);
      const rotate = (angle, cx, cy) => (angle ? 'rotate(' + (-angle) + ' ' + toX(cx) + ' ' + toY(cy) + ')' : null);
      let node = null;
      if (element.type === 'circle') {
        node = svg(c, 'circle', { cx: toX(element.cx), cy: toY(element.cy), r: element.r * ppu, class: cls, fill });
      } else if (element.type === 'ellipse') {
        node = svg(c, 'ellipse', { cx: toX(element.cx), cy: toY(element.cy), rx: element.rx * ppu, ry: element.ry * ppu, class: cls, fill, transform: rotate(element.rotation, element.cx, element.cy) });
      } else if (element.type === 'rect' || element.type === 'screen') {
        node = svg(c, 'rect', {
          x: toX(element.cx - element.width / 2), y: toY(element.cy + element.height / 2), width: element.width * ppu, height: element.height * ppu,
          class: cls, fill: element.type === 'rect' ? fill : null, transform: element.type === 'rect' ? rotate(element.rotation, element.cx, element.cy) : null,
        });
      } else if (element.type === 'line' || element.type === 'arrow') {
        node = svg(c, 'line', { x1: toX(element.x1), y1: toY(element.y1), x2: toX(element.x2), y2: toY(element.y2), class: cls, 'marker-end': element.type === 'arrow' ? 'url(#' + id + '-arrow)' : null });
      } else if (element.type === 'dot_field') {
        node = svg(c, 'g', { class: cls });
        for (const [dx, dy] of dotPositions(element.count, element.seed)) {
          node.appendChild(svg(c, 'circle', { cx: toX(element.cx + dx * element.radius), cy: toY(element.cy + dy * element.radius), r: Math.max(element.dot_radius * ppu, 0.8), fill: fill || null, class: 'hd-dg-dot' }));
        }
      } else if (element.type === 'grating') {
        node = gratingNode(c, element, layout, id + '-g' + index, cls);
      } else if (element.type === 'text') {
        node = svg(c, 'text', { x: toX(element.x), y: toY(element.y), class: 'hd-dg-text', 'text-anchor': 'middle' }, element.text);
      } else if (element.type === 'dimension') {
        node = dimensionNode(c, element, layout);
      }
      if (node) scene.appendChild(node);
    });
    root.appendChild(scene);
    const callouts = svg(c, 'g', { class: 'hd-dg-callouts', 'aria-hidden': 'true' });
    for (const callout of layout.callouts) {
      callouts.appendChild(svg(c, 'line', { x1: callout.px, y1: callout.py, x2: callout.x, y2: callout.y, class: 'hd-dg-leader' }));
      callouts.appendChild(svg(c, 'circle', { cx: callout.x, cy: callout.y, r: 10, class: 'hd-dg-badge' }));
      callouts.appendChild(svg(c, 'text', { x: callout.x, y: callout.y + 4.5, 'text-anchor': 'middle', class: 'hd-dg-badge-text' }, String(callout.number)));
    }
    root.appendChild(callouts);
    if (layout.scaleBar) {
      const y = layout.canvas.y + layout.canvas.h + 16;
      const x = layout.canvas.x;
      root.appendChild(svg(c, 'path', { d: 'M' + x + ',' + (y - 4) + ' v4 h' + layout.scaleBar.px + ' v-4', class: 'hd-dg-scalebar' }));
      root.appendChild(svg(c, 'text', { x: x + layout.scaleBar.px + 6, y: y + 3, class: 'hd-dg-scaletext' }, formatNumber(layout.scaleBar.units) + ' ' + diagram.unit));
    }
    return root;
  }

  function gratingNode(c, element, layout, clipId, cls) {
    const { toX, toY, ppu } = layout;
    const group = svg(c, 'g', { class: cls });
    const clip = svg(c, 'clipPath', { id: clipId });
    clip.appendChild(svg(c, 'circle', { cx: toX(element.cx), cy: toY(element.cy), r: element.radius * ppu }));
    group.appendChild(clip);
    const stripes = svg(c, 'g', { 'clip-path': 'url(#' + clipId + ')', transform: 'rotate(' + (-element.orientation) + ' ' + toX(element.cx) + ' ' + toY(element.cy) + ')' });
    const diameter = element.radius * 2 * ppu;
    const period = diameter / element.cycles;
    const light = grey(0.5 + element.contrast / 2);
    const dark = grey(0.5 - element.contrast / 2);
    const left = toX(element.cx) - element.radius * ppu;
    const top = toY(element.cy) - element.radius * ppu;
    stripes.appendChild(svg(c, 'rect', { x: left, y: top, width: diameter, height: diameter, fill: dark }));
    for (let k = 0; k < Math.ceil(element.cycles); k += 1) {
      stripes.appendChild(svg(c, 'rect', { x: left + k * period, y: top, width: period / 2, height: diameter, fill: light }));
    }
    group.appendChild(stripes);
    group.appendChild(svg(c, 'circle', { cx: toX(element.cx), cy: toY(element.cy), r: element.radius * ppu, class: 'hd-dg-outline' }));
    return group;
  }

  function dimensionNode(c, element, layout) {
    const { toX, toY } = layout;
    const x1 = toX(element.x1); const y1 = toY(element.y1); const x2 = toX(element.x2); const y2 = toY(element.y2);
    const length = Math.hypot(x2 - x1, y2 - y1) || 1;
    const nx = -(y2 - y1) / length * 5; const ny = (x2 - x1) / length * 5;
    const group = svg(c, 'g', { class: 'hd-dg-dimension' });
    group.appendChild(svg(c, 'line', { x1, y1, x2, y2, class: 'hd-dg-dimline' }));
    group.appendChild(svg(c, 'line', { x1: x1 - nx, y1: y1 - ny, x2: x1 + nx, y2: y1 + ny, class: 'hd-dg-dimline' }));
    group.appendChild(svg(c, 'line', { x1: x2 - nx, y1: y2 - ny, x2: x2 + nx, y2: y2 + ny, class: 'hd-dg-dimline' }));
    const label = element.value_text || element.text;
    if (label) {
      group.appendChild(svg(c, 'text', { x: (x1 + x2) / 2, y: (y1 + y2) / 2 - 7, 'text-anchor': 'middle', class: 'hd-dg-dimtext' }, label));
    }
    return group;
  }

  /* ------------------------------------------------------------------ */
  /* Views                                                               */
  /* ------------------------------------------------------------------ */
  function shortHash(value) {
    return typeof value === 'string' ? value.slice(0, 12) : '';
  }

  function provenance(c, doc, path) {
    const source = doc.source || {};
    const where = [path, source.package && source.version ? source.package + ' ' + source.version : source.package]
      .filter(Boolean).join(' in ');
    const line = el(c, 'p', 'hd-provenance', 'From ' + where + '. Written by the experiment\'s authors and versioned with its code.');
    return line;
  }

  function header(c, eyebrow, title, summary) {
    const head = el(c, 'header', 'hd-head');
    head.appendChild(el(c, 'p', 'hd-eyebrow', eyebrow));
    head.appendChild(heading(c, 0, title));
    if (summary) head.appendChild(el(c, 'p', 'hd-summary', summary));
    return head;
  }

  function section(c, key, title) {
    const node = el(c, 'section', 'hd-section hd-section--' + key);
    const id = c.prefix + safeId(key);
    node.setAttribute('aria-labelledby', id);
    node.setAttribute('tabindex', '-1');
    node.setAttribute('data-hd-section', key);
    node.appendChild(heading(c, 1, title, id));
    return node;
  }

  function pageNav(c, entries) {
    const nav = el(c, 'nav', 'hd-pagenav');
    nav.setAttribute('aria-label', 'On this page');
    const list = el(c, 'ul', 'hd-pagenav-list');
    for (const [key, label] of entries) {
      const li = el(c, 'li', null);
      li.appendChild(jumpButton(c, label, '[data-hd-section="' + key + '"]', 'hd-jump hd-pagenav-item'));
      list.appendChild(li);
    }
    nav.appendChild(list);
    return nav;
  }

  function renderMissing(kind, options) {
    const c = context(options);
    const messages = {
      methods: ['No methods for this version', 'The authors did not include documentation in this package version. Read its source and parameter files before running it; nothing here has been filled in for them.'],
      task: ['No documentation for this task', 'This package version does not document this task. Its parameters and timing are only in its source and parameter files.'],
      guide: ['The guide is not available', 'The guide comes with Alhazen itself; this hub could not provide it.'],
    };
    const [title, body] = messages[kind] || messages.methods;
    const box = el(c, 'div', 'hd-doc hd-missing');
    box.setAttribute('role', 'status');
    box.appendChild(heading(c, 0, title));
    box.appendChild(el(c, 'p', null, body));
    return box;
  }

  function renderTaskIndex(doc, options, currentId) {
    const c = context(options);
    const nav = el(c, 'nav', 'hd-task-index');
    nav.setAttribute('aria-label', 'Tasks');
    const list = el(c, 'ul', 'hd-task-list');
    for (const task of (doc && doc.tasks) || []) {
      const li = el(c, 'li', 'hd-task-item');
      const link = taskLink(c, task, task.title);
      if (task.id === currentId) link.setAttribute('aria-current', 'page');
      li.appendChild(link);
      const facts = [task.parameters.length + (task.parameters.length === 1 ? ' parameter' : ' parameters')];
      if (task.timeline) facts.push('timeline');
      if (task.diagram) facts.push('stimulus figure');
      li.appendChild(el(c, 'span', 'hd-task-facts', facts.join(' · ')));
      list.appendChild(li);
    }
    nav.appendChild(list);
    return nav;
  }

  function renderMethods(doc, options) {
    if (!doc) return renderMissing('methods', options);
    const c = context(options);
    const view = el(c, 'article', 'hd-doc hd-methods');
    c.root = view;
    const version = doc.source && doc.source.version ? ' · version ' + doc.source.version : '';
    view.appendChild(header(c, 'Methods' + version, doc.title, doc.summary));
    if (doc.methods) {
      view.appendChild(renderMarkdownInto(c, doc.methods.markdown, { doc, task: null }, el(c, 'div', 'hd-prose')));
      view.appendChild(provenance(c, doc, doc.methods.path));
    } else {
      view.appendChild(el(c, 'p', 'hd-empty', 'This version documents its tasks but has no methods text.'));
    }
    if (doc.tasks.length) {
      const tasks = section(c, 'tasks', 'Tasks');
      tasks.appendChild(renderTaskIndex(doc, options));
      view.appendChild(tasks);
    }
    if (doc.references.length) {
      const refs = section(c, 'references', 'References');
      const list = el(c, 'ol', 'hd-references');
      for (const reference of doc.references) list.appendChild(el(c, 'li', null, reference));
      refs.appendChild(list);
      view.appendChild(refs);
    }
    return view;
  }

  function constraintText(parameter) {
    const constraint = parameter.constraints || {};
    const parts = [];
    const unit = parameter.type === 'duration' ? 'ms' : parameter.unit;
    if (constraint.min !== undefined && constraint.max !== undefined) parts.push(formatValue(constraint.min, null) + ' to ' + formatValue(constraint.max, unit));
    else if (constraint.min !== undefined) parts.push('≥ ' + formatValue(constraint.min, unit));
    else if (constraint.max !== undefined) parts.push('≤ ' + formatValue(constraint.max, unit));
    if (constraint.choices) parts.push('one of ' + constraint.choices.map((choice) => formatValue(choice, null)).join(', '));
    if (constraint.note) parts.push(constraint.note);
    return parts.join('. ');
  }

  function sourceText(parameter) {
    const source = parameter.source || {};
    if (source.status === 'matched') return 'Checked against ' + source.file + (source.key !== parameter.name ? ' (' + source.key + ')' : '');
    if (source.status === 'read') return 'Read from ' + source.file;
    return source.model ? 'Declared; see ' + source.model : 'Declared by the authors; not checked against a file';
  }

  function cell(c, tag, className, label, content) {
    const node = el(c, tag, className);
    if (label) node.setAttribute('data-label', label);
    if (typeof content === 'string') node.appendChild(textNode(c, content));
    else if (content) node.appendChild(content);
    return node;
  }

  function parameterTable(c, task) {
    const wrap = el(c, 'div', 'hd-table-wrap');
    const table = el(c, 'table', 'hd-table hd-params');
    const caption = el(c, 'caption', 'hd-sr', 'Parameters of ' + task.title + ': name, default, constraints and meaning');
    table.appendChild(caption);
    const head = el(c, 'thead', null);
    const row = el(c, 'tr', null);
    for (const label of ['Parameter', 'Default', 'Constraints', 'Meaning']) {
      const th = el(c, 'th', null, label);
      th.setAttribute('scope', 'col');
      row.appendChild(th);
    }
    head.appendChild(row);
    table.appendChild(head);
    const groups = [];
    for (const parameter of task.parameters) {
      const name = parameter.group || 'Other';
      let group = groups.find((g) => g.name === name);
      if (!group) { group = { name, items: [] }; groups.push(group); }
      group.items.push(parameter);
    }
    for (const group of groups) {
      const body = el(c, 'tbody', 'hd-param-group');
      if (groups.length > 1) {
        const groupRow = el(c, 'tr', 'hd-group-row');
        const th = el(c, 'th', null, group.name);
        th.setAttribute('colspan', '4');
        th.setAttribute('scope', 'colgroup');
        groupRow.appendChild(th);
        body.appendChild(groupRow);
      }
      for (const parameter of group.items) {
        const tr = el(c, 'tr', 'hd-param');
        tr.setAttribute('data-hd-param', safeId(task.id) + '/' + parameter.name.replace(/[^A-Za-z0-9_.]/g, ''));
        tr.setAttribute('tabindex', '-1');
        const nameCell = el(c, 'th', 'hd-param-name');
        nameCell.setAttribute('scope', 'row');
        nameCell.appendChild(el(c, 'span', 'hd-param-label', parameter.label));
        nameCell.appendChild(el(c, 'code', 'hd-param-key', parameter.name));
        tr.appendChild(nameCell);
        tr.appendChild(cell(c, 'td', 'hd-param-default', 'Default',
          parameter.has_default ? el(c, 'span', 'hd-value', parameter.default_text) : el(c, 'span', 'hd-value hd-value--none', 'no default')));
        tr.appendChild(cell(c, 'td', 'hd-param-constraints', 'Constraints', constraintText(parameter) || '—'));
        const meaning = cell(c, 'td', 'hd-param-meaning', 'Meaning', null);
        meaning.appendChild(el(c, 'p', null, parameter.meaning));
        if (parameter.interactions) meaning.appendChild(el(c, 'p', 'hd-interactions', parameter.interactions));
        meaning.appendChild(el(c, 'p', 'hd-source hd-source--' + (parameter.source && parameter.source.status), sourceText(parameter)));
        tr.appendChild(meaning);
        body.appendChild(tr);
      }
      table.appendChild(body);
    }
    wrap.appendChild(table);
    return wrap;
  }

  function simpleTable(c, captionText, columns, rows, className) {
    const wrap = el(c, 'div', 'hd-table-wrap');
    const table = el(c, 'table', 'hd-table ' + className);
    table.appendChild(el(c, 'caption', 'hd-sr', captionText));
    const head = el(c, 'thead', null);
    const headRow = el(c, 'tr', null);
    for (const column of columns) {
      const th = el(c, 'th', null, column);
      th.setAttribute('scope', 'col');
      headRow.appendChild(th);
    }
    head.appendChild(headRow);
    table.appendChild(head);
    const body = el(c, 'tbody', null);
    for (const values of rows) {
      const tr = el(c, 'tr', null);
      values.forEach((value, index) => {
        if (index === 0) {
          const th = el(c, 'th', null);
          th.setAttribute('scope', 'row');
          th.appendChild(el(c, 'code', 'hd-code', value));
          tr.appendChild(th);
        } else {
          tr.appendChild(cell(c, 'td', null, columns[index], value));
        }
      });
      body.appendChild(tr);
    }
    table.appendChild(body);
    wrap.appendChild(table);
    return wrap;
  }

  function figure(c, number, kind, title, caption, graphic, notes, wide) {
    const node = el(c, 'figure', 'hd-figure hd-figure--' + kind);
    const canvas = el(c, 'div', 'hd-figure-canvas' + (wide ? ' hd-figure-canvas--wide' : ''));
    if (wide) {
      canvas.setAttribute('tabindex', '0');
      canvas.setAttribute('role', 'group');
      canvas.setAttribute('aria-label', title + ' (scrolls sideways on narrow screens)');
    }
    canvas.appendChild(graphic);
    node.appendChild(canvas);
    if (wide) node.appendChild(el(c, 'p', 'hd-scroll-note', 'Swipe or scroll sideways to see the full-size figure.'));
    const figcaption = el(c, 'figcaption', 'hd-figcaption');
    figcaption.appendChild(el(c, 'strong', 'hd-fig-number', 'Figure ' + number + '. ' + title + '. '));
    if (caption) figcaption.appendChild(textNode(c, caption + ' '));
    for (const note of notes) figcaption.appendChild(el(c, 'span', 'hd-fig-note', note + ' '));
    node.appendChild(figcaption);
    return node;
  }

  function timelineNotes(task) {
    const layout = layoutTimeline(task.timeline);
    const notes = [];
    if (layout.anyUnscaled) notes.push('Hatched segments with break marks wait on the subject or depend on the refresh rate; their width is not a duration.');
    if (layout.anyClamped) notes.push('Segments marked “not to scale” were widened to fit their label.');
    if (layout.scaleBar) notes.push('Solid segments are drawn to the scale bar at the documented defaults.');
    return notes;
  }

  function phaseList(c, task) {
    const timeline = task.timeline;
    const list = el(c, 'ol', 'hd-phase-list');
    for (const phase of timeline.phases) {
      const li = el(c, 'li', 'hd-phase hd-phase--' + phase.timing.kind);
      li.appendChild(el(c, 'span', 'hd-phase-label', phase.label));
      li.appendChild(el(c, 'span', 'hd-phase-time', phase.timing.text));
      const events = [];
      if (phase.start_events.length) events.push('starts with ' + phase.start_events.join(', '));
      if (phase.end_events.length) events.push('ends with ' + phase.end_events.join(', '));
      if (events.length) li.appendChild(el(c, 'span', 'hd-phase-events', events.join('; ')));
      if (phase.note) li.appendChild(el(c, 'span', 'hd-phase-note', phase.note));
      list.appendChild(li);
    }
    const wrap = el(c, 'div', 'hd-phase-text');
    wrap.appendChild(list);
    const outcomes = el(c, 'ul', 'hd-branch-list');
    if (timeline.end) {
      outcomes.appendChild(el(c, 'li', 'hd-branch hd-branch--end', (timeline.end.label || 'Trial ends') + ': ' + timeline.end.outcome));
    }
    for (const branch of timeline.branches) {
      const li = el(c, 'li', 'hd-branch hd-branch--' + branch.kind);
      li.appendChild(el(c, 'code', 'hd-code', branch.outcome));
      li.appendChild(textNode(c, ' — ' + (branch.from === '*' ? 'any phase' : phaseLabel(timeline, branch.from)) + ', if ' + branch.when + (branch.effect ? '; ' + branch.effect : '')));
      outcomes.appendChild(li);
    }
    if (timeline.between_trials) {
      outcomes.appendChild(el(c, 'li', 'hd-branch hd-branch--between', 'Between trials: ' + timeline.between_trials.label + ', ' + timeline.between_trials.timing.text));
    }
    wrap.appendChild(outcomes);
    return wrap;
  }

  function diagramLegend(c, task) {
    const layout = layoutDiagram(task.diagram);
    if (!layout.callouts.length) return null;
    const list = el(c, 'ol', 'hd-legend');
    for (const callout of layout.callouts) {
      const li = el(c, 'li', 'hd-legend-item');
      li.setAttribute('value', String(callout.number));
      li.appendChild(el(c, 'span', 'hd-legend-label', callout.element.label));
      li.appendChild(el(c, 'span', 'hd-legend-detail', elementDescription(callout.element, task.diagram.unit)));
      list.appendChild(li);
    }
    return list;
  }

  function renderTaskGuide(doc, taskId, options) {
    if (!doc) return renderMissing('methods', options);
    const task = (doc.tasks || []).find((candidate) => candidate.id === taskId);
    if (!task) return renderMissing('task', options);
    const c = context(options);
    const view = el(c, 'article', 'hd-doc hd-task');
    c.root = view;
    const version = doc.source && doc.source.version ? ' · version ' + doc.source.version : '';
    view.appendChild(header(c, 'Task ' + task.id + version, task.title, task.summary));
    if (doc.tasks.length > 1) view.appendChild(renderTaskIndex(doc, options, task.id));
    const entries = [];
    if (task.description) entries.push(['description', 'Description']);
    if (task.diagram) entries.push(['stimulus', 'Stimulus']);
    if (task.timeline) entries.push(['timeline', 'Timeline']);
    entries.push(['parameters', 'Parameters']);
    if (task.outcomes.length) entries.push(['outcomes', 'Outcomes']);
    if (task.events.length) entries.push(['events', 'Events']);
    view.appendChild(pageNav(c, entries));
    if (task.description) {
      const part = section(c, 'description', 'Description');
      part.appendChild(renderMarkdownInto(c, task.description.markdown, { doc, task }, el(c, 'div', 'hd-prose')));
      view.appendChild(part);
    }
    let figureNumber = 0;
    if (task.diagram) {
      figureNumber += 1;
      const part = section(c, 'stimulus', 'Stimulus');
      const notes = ['To scale in ' + task.diagram.unit + ' at the documented defaults; an explanation of the design, not a measurement of any rig.'];
      // Keep scientific labels at their authored size; a phone scrolls the drawing instead of reducing text to 7 px.
      part.appendChild(figure(c, figureNumber, 'diagram', 'Stimulus', task.diagram.caption, diagramSvg(task, options), notes, true));
      const legend = diagramLegend(c, task);
      if (legend) part.appendChild(legend);
      view.appendChild(part);
    }
    if (task.timeline) {
      figureNumber += 1;
      const part = section(c, 'timeline', 'Trial timeline');
      part.appendChild(figure(c, figureNumber, 'timeline', 'Trial timeline', task.timeline.caption, timelineSvg(task, options), timelineNotes(task), true));
      part.appendChild(phaseList(c, task));
      view.appendChild(part);
    }
    const params = section(c, 'parameters', 'Parameters');
    if (task.parameters.length) params.appendChild(parameterTable(c, task));
    else params.appendChild(el(c, 'p', 'hd-empty', 'This task documents no parameters.'));
    if (task.parameters_file) {
      params.appendChild(el(c, 'p', 'hd-source-note', 'Defaults are those in ' + task.parameters_file + ' of this version.'));
    }
    if (task.parameters_undocumented && task.parameters_undocumented.length) {
      params.appendChild(el(c, 'p', 'hd-undocumented', 'Also set in ' + task.parameters_file + ' but not described here: ' + task.parameters_undocumented.join(', ') + '.'));
    }
    view.appendChild(params);
    if (task.outcomes.length) {
      const part = section(c, 'outcomes', 'Outcomes');
      part.appendChild(simpleTable(c, 'Outcomes of ' + task.title, ['Outcome', 'Counts as a trial', 'Success', 'Meaning'],
        task.outcomes.map((o) => [o.name, o.completed ? 'yes' : 'no, served again', o.success === null || o.success === undefined ? '—' : (o.success ? 'yes' : 'no'), o.meaning]), 'hd-outcomes'));
      view.appendChild(part);
    }
    if (task.events.length) {
      const part = section(c, 'events', 'Events');
      part.appendChild(simpleTable(c, 'Events of ' + task.title, ['Event', 'Meaning'], task.events.map((e) => [e.name, e.meaning]), 'hd-events'));
      view.appendChild(part);
    }
    if (doc.source) view.appendChild(provenance(c, doc, doc.source.descriptor));
    return view;
  }

  function badge(c, text, on) {
    return el(c, 'span', 'hd-badge ' + (on ? 'hd-badge--on' : 'hd-badge--off'), text);
  }

  function renderGlobalGuide(guide, options) {
    if (!guide || !Array.isArray(guide.modes)) return renderMissing('guide', options);
    const c = context(options);
    const view = el(c, 'article', 'hd-doc hd-guide');
    c.root = view;
    view.appendChild(header(c, 'Guide · alhazen ' + (guide.alhazen_version || ''), guide.title || 'Alhazen guide', guide.intro));
    const sections = guide.sections || [];
    view.appendChild(pageNav(c, [['modes', 'Modes'], ...sections.map((s) => [s.id, s.title])]));
    const modes = section(c, 'modes', 'Modes');
    const list = el(c, 'ul', 'hd-modes');
    for (const mode of guide.modes) {
      const item = el(c, 'li', 'hd-mode');
      const top = el(c, 'div', 'hd-mode-head');
      top.appendChild(el(c, 'code', 'hd-mode-name', mode.id));
      top.appendChild(el(c, 'span', 'hd-mode-summary', mode.summary));
      item.appendChild(top);
      const facts = el(c, 'div', 'hd-mode-facts');
      facts.appendChild(badge(c, mode.runs_trials ? 'runs trials' : 'no trials', mode.runs_trials));
      if (mode.id === 'simulate') facts.appendChild(badge(c, 'simulated subject', false));
      else if (mode.id === 'test') facts.appendChild(badge(c, 'subject rehearsal', false));
      else if (mode.drives_subject) facts.appendChild(badge(c, 'real subject', true));
      const dataLabel = mode.id === 'training' ? 'separate training records'
        : mode.writes_real_data ? 'experiment records'
        : (mode.id === 'test' || mode.id === 'simulate') ? 'separate rehearsal records'
        : mode.runs_trials ? 'see output below' : 'no trial records';
      facts.appendChild(badge(c, dataLabel, mode.writes_real_data || mode.id === 'training'));
      if (mode.refuses_development_rig) facts.appendChild(badge(c, 'refuses development rigs', true));
      const flags = [];
      if (mode.accepts.headless) flags.push('--headless');
      if (mode.accepts.mouse) flags.push('--mouse');
      if (mode.accepts.calibration_target) flags.push('calibration target');
      if (flags.length) facts.appendChild(el(c, 'span', 'hd-mode-flags', 'accepts ' + flags.join(', ')));
      item.appendChild(facts);
      item.appendChild(el(c, 'p', 'hd-mode-data', 'Output: ' + mode.data));
      list.appendChild(item);
    }
    modes.appendChild(list);
    view.appendChild(modes);
    for (const part of sections) {
      const node = section(c, part.id, part.title);
      const items = el(c, 'div', 'hd-guide-items');
      for (const item of part.items || []) {
        const block = el(c, 'div', 'hd-guide-item');
        block.appendChild(heading(c, 2, item.title));
        block.appendChild(el(c, 'p', null, item.text));
        if (item.values && item.values.length) {
          const values = el(c, 'p', 'hd-guide-values');
          item.values.forEach((value) => values.appendChild(el(c, 'code', 'hd-chip', value)));
          block.appendChild(values);
        }
        if (item.sources && item.sources.length) {
          block.appendChild(el(c, 'p', 'hd-guide-source', 'Source: ' + item.sources.join(', ')));
        }
        items.appendChild(block);
      }
      node.appendChild(items);
      view.appendChild(node);
    }
    return view;
  }

  return {
    SCHEMA_VERSION,
    renderMethods,
    renderTaskGuide,
    renderTaskIndex: (doc, options) => renderTaskIndex(doc, options, null),
    renderGlobalGuide,
    renderMissing,
    renderMarkdown,
    timelineSvg,
    diagramSvg,
    parseMarkdown,
    parseInline,
    layoutTimeline,
    layoutDiagram,
    formatValue,
    resolveParameterReference,
  };
})();

if (typeof window !== 'undefined') window.HubDocs = HubDocs;
