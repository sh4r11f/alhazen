/* The launch's duration estimate, beside the Start button: a module of its own.
 *
 * The Python side is the authority. POST /api/estimate (cli/workspace_estimate.py)
 * asks the project's own alhazen, through the run.py a launch would start
 * (--estimate-duration), how long the launch the form describes would take,
 * and answers with the numbers and the words (modes/estimate.py). This module
 * only asks at the right moments and shows the answer: it never computes a
 * duration itself.
 *
 * Its rules, which the tests hold (tests/js/workspace_duration.test.mjs):
 * - A change to anything that decides the duration (DurationEstimate.signature
 *   of the shell's draft) clears the number at once and shows "Estimating…":
 *   the previous task's number is never shown as if it were current.
 * - Edits in quick succession are asked about once, after they settle
 *   (debounceMs); a request overtaken by a newer one is aborted, and an
 *   answer that arrives for a draft that is no longer the form's is dropped.
 * - A draft the shell cannot send yet (null: parameters still loading, no
 *   project) shows why there is no estimate, not an old one.
 *
 * Interface for a shell:
 *   DurationEstimate.signature(draft)              -> string ('' for null)
 *   DurationEstimate.describe(answer)              -> {state, value, counts, plus, details}
 *   DurationEstimate.mount(container, {ask, read, debounceMs, document, timers})
 *       -> {update(), refresh(), dispose(), current()}
 *     ask(draft, signal) -> Promise<answer>   (POST /api/estimate)
 *     read()             -> the draft, or {waiting: 'why'} / null
 */
'use strict';

const DurationEstimate = (() => {
  /* The draft's fields that decide a duration, in a fixed order, so two
   * drafts that differ only in key order are one signature. */
  const FIELDS = [
    'project', 'mode', 'task', 'parameter_set', 'rig', 'trials', 'headless', 'mouse',
    'extra_args', 'parameters_yaml', 'parameters', 'measurements', 'calibration_target',
  ];

  function stable(value) {
    if (Array.isArray(value)) return value.map(stable);
    if (value && typeof value === 'object') {
      const out = {};
      Object.keys(value).sort().forEach((k) => { out[k] = stable(value[k]); });
      return out;
    }
    return value;
  }

  function signature(draft) {
    if (!draft || draft.waiting) return '';
    return JSON.stringify(FIELDS.map((f) => [f, stable(draft[f] ?? null)]));
  }

  /* Seconds as a span's bounds read: 0.25 s, 2 s, 1.5–2.5 s. */
  function seconds(x) {
    if (x === null || x === undefined) return '…';
    const r = Math.round(x * 100) / 100;
    return `${r}`;
  }

  function spanText(span) {
    if (span.expected_s !== null && span.min_s === span.max_s) return `${seconds(span.min_s)} s`;
    const range = `${seconds(span.min_s)}–${span.max_s === null ? 'no limit' : seconds(span.max_s)} s`;
    if (span.expected_s !== null) return `${seconds(span.expected_s)} s (${range})`;
    if (span.kind === 'wait') {
      const from = span.min_s > 0 ? `${seconds(span.min_s)}–` : 'up to ';
      return `${from}${seconds(span.max_s)} s, waiting on the subject`;
    }
    return range;
  }

  function countsText(counts) {
    if (!counts) return '';
    const t = counts.trials;
    const hi = counts.trials_max;
    let text;
    if (hi === t) text = `${t} trial${t === 1 ? '' : 's'}`;
    else if (hi === null || hi === undefined) text = `${t}+ trials`;
    else text = `${t}–${hi} trials`;
    if (counts.blocks) text += ` · ${counts.blocks} block${counts.blocks === 1 ? '' : 's'}`;
    return text;
  }

  const STATES = {
    ok: 'ok', partial: 'partial', unknown: 'unknown', 'open-ended': 'open',
    unavailable: 'unavailable', refused: 'refused', error: 'error',
  };

  /* An answer as the page shows it: what goes in the line, and the sections
   * of the disclosure (each a heading and its lines). */
  function describe(answer) {
    if (!answer) return {state: 'none', value: '', counts: '', plus: '', details: []};
    const state = STATES[answer.status] || 'error';
    const details = [];
    const per = answer.per_trial;
    if (per && per.spans) {
      details.push({
        heading: 'One trial',
        lines: per.spans.map((s) => `${s.label}: ${spanText(s)}${s.basis ? ` — ${s.basis}` : ''}`),
      });
    } else if (per) {
      details.push({
        heading: 'One trial',
        lines: [`${seconds(per.min_s)}–${per.max_s === null ? 'no limit' : seconds(per.max_s)} s, by condition`],
      });
    }
    if (answer.jobs) {
      details.push({
        heading: 'Measurements',
        lines: answer.jobs.map((j) => {
          const timed = j.timed_s === null ? 'not declared' : `${seconds(j.timed_s)} s timed`;
          const who = j.operator ? `; ${j.operator}` : '';
          return `${j.title}: ${j.note || timed}${who}`;
        }),
      });
    }
    const counts = answer.counts;
    if (counts && counts.kind === 'adaptive') {
      details.push({heading: 'Trials', lines: [countsText(counts) + ', set by the stopping rule']});
    }
    if (answer.manual && answer.manual.length) {
      details.push({heading: 'Not included: up to a person', lines: answer.manual});
    }
    if (answer.excluded && answer.excluded.length) {
      details.push({heading: 'Not included', lines: answer.excluded});
    }
    const basis = [];
    if (answer.basis?.refresh) basis.push(answer.basis.refresh);
    if (answer.basis?.params) basis.push(`params: ${answer.basis.params}`);
    (answer.basis?.reductions || []).forEach((r) => basis.push(`reduced for this mode: ${r}`));
    (answer.assumptions || []).forEach((a) => basis.push(a));
    if (basis.length) details.push({heading: 'Basis', lines: basis});
    if (answer.reason && state !== 'ok') details.unshift({heading: 'Why', lines: [answer.reason]});
    return {
      state,
      value: answer.headline || '',
      counts: countsText(counts),
      plus: answer.plus || '',
      details,
    };
  }

  function mount(container, options) {
    const doc = options.document || container.ownerDocument;
    // Called through wrappers: the browser's own setTimeout refuses to be
    // called as a method of another object ("Illegal invocation").
    const timers = options.timers || {
      setTimeout: (fn, ms) => setTimeout(fn, ms),
      clearTimeout: (id) => clearTimeout(id),
    };
    const debounceMs = options.debounceMs ?? 350;
    let shown = null;      // signature of the answer on screen ('' none)
    let wanted = '';       // signature of the form now
    let timer = null;
    let inflight = null;   // {signature, controller}
    let answer = null;

    const node = (tag, cls, text) => {
      const el = doc.createElement(tag);
      if (cls) el.className = cls;
      if (text !== undefined) el.textContent = text;
      return el;
    };
    const root = node('section', 'duration');
    root.setAttribute('aria-live', 'polite');
    root.setAttribute('aria-label', 'Estimated session duration');
    const row = node('div', 'duration-row');
    const eyebrow = node('span', 'duration-eyebrow', 'Duration');
    const value = node('span', 'duration-value');
    const counts = node('span', 'duration-counts');
    row.append(eyebrow, value, counts);
    const plus = node('p', 'duration-plus');
    const more = node('details', 'duration-details');
    const summary = node('summary', '', 'How this is worked out');
    const body = node('div', 'duration-body');
    more.append(summary, body);
    root.append(row, plus, more);
    container.replaceChildren(root);

    function paint(view) {
      root.dataset.state = view.state;
      value.textContent = view.value;
      counts.textContent = view.counts;
      counts.hidden = !view.counts;
      plus.textContent = view.plus;
      plus.hidden = !view.plus;
      body.replaceChildren(...view.details.map((section) => {
        const block = node('div', 'duration-section');
        block.append(node('h4', '', section.heading));
        const list = node('ul');
        section.lines.forEach((line) => list.append(node('li', '', line)));
        block.append(list);
        return block;
      }));
      more.hidden = !view.details.length;
    }

    function pending(text) {
      paint({state: 'pending', value: text, counts: '', plus: '', details: []});
    }

    async function ask(draft, sig) {
      if (inflight) inflight.controller.abort();
      const controller = new AbortController();
      inflight = {signature: sig, controller};
      try {
        const result = await options.ask(draft, controller.signal);
        if (sig !== wanted) return;          // overtaken: the form moved on
        answer = result;
        shown = sig;
        paint(describe(result));
      } catch (e) {
        if (controller.signal.aborted || sig !== wanted) return;
        answer = null;
        shown = sig;
        paint({state: 'error', value: 'No estimate', counts: '', plus: '',
          details: [{heading: 'Why', lines: [e.message || String(e)]}]});
      } finally {
        if (inflight && inflight.controller === controller) inflight = null;
      }
    }

    /* The page's form must keep working whatever happens here: a fault in
     * the estimate is shown in its own box (and the console), never thrown
     * into the shell that called. */
    function update(force = false) {
      try {
        change(force);
      } catch (e) {
        if (timer) timers.clearTimeout(timer);
        timer = null;
        wanted = '';
        if (typeof console !== 'undefined') console.error('duration estimate:', e);
        paint({state: 'error', value: 'No estimate', counts: '', plus: '',
          details: [{heading: 'Why', lines: [`The page could not ask: ${e.message || e}`]}]});
      }
    }

    function change(force) {
      const draft = options.read();
      if (!draft || draft.waiting) {
        wanted = '';
        if (timer) timers.clearTimeout(timer);
        timer = null;
        if (inflight) inflight.controller.abort();
        inflight = null;
        answer = null;
        shown = null;
        if (draft && draft.waiting) pending(draft.waiting);
        else paint({state: 'none', value: '', counts: '', plus: '', details: []});
        return;
      }
      const sig = signature(draft);
      if (!force && sig === wanted) return;  // nothing that decides it changed
      wanted = sig;
      // Cleared at once: the number on screen belongs to another form.
      answer = null;
      shown = null;
      pending('Estimating…');
      if (timer) timers.clearTimeout(timer);
      if (inflight && inflight.signature !== sig) {
        inflight.controller.abort();
        inflight = null;
      }
      timer = timers.setTimeout(() => {
        timer = null;
        ask(draft, sig);
      }, debounceMs);
    }

    function dispose() {
      if (timer) timers.clearTimeout(timer);
      if (inflight) inflight.controller.abort();
      container.replaceChildren();
    }

    paint({state: 'none', value: '', counts: '', plus: '', details: []});
    return {
      update: () => update(false),
      refresh: () => update(true),
      dispose,
      current: () => ({signature: shown, answer}),
    };
  }

  return {signature, describe, mount, spanText, countsText};
})();

if (typeof window !== 'undefined') window.DurationEstimate = DurationEstimate;
