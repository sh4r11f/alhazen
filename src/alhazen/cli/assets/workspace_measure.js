/* Measure rig's checklist and its progress: a module of its own.
 *
 * The pure part (MeasureChoice.*: order, prerequisites, what the rig lacks,
 * what a launch sends, what the queue says) has no DOM, and
 * tests/js/workspace_measure.test.mjs loads it on its own. The two renderers
 * (renderChecklist, renderProgress) draw into a container they are handed and
 * touch nothing else on the page, so any shell can mount them: today's Run
 * experiment form, or a later Run page.
 *
 * The Python side is the authority. The catalog comes from the project's own
 * alhazen (workspace.py _measurement_offer: key, group, title, description,
 * order, needs, requires, provider, inputs, subject); the server refuses a
 * selection the child would refuse (check_measurements); the child decides
 * whether each job can really run on the rig. The hints here only say ahead
 * of time what the rig file already tells.
 *
 * Interface for a shell:
 *   MeasureChoice.create(catalog)            -> state
 *   MeasureChoice.toggle(state, key, on)     -> {state, note}
 *   MeasureChoice.selection(state)           -> keys in run order (what to send)
 *   MeasureChoice.problem(state)             -> why it cannot launch, or ''
 *   MeasureChoice.subjectNeed(state)         -> 'required' | 'optional' | 'none'
 *   MeasureChoice.rigHint(entry, rig)        -> what the rig file says is missing, or ''
 *   MeasureChoice.renderChecklist(el, state, {rig, onToggle, document})
 *   MeasureChoice.renderProgress(el, status, {document})
 */
'use strict';

const MeasureChoice = (() => {
  /* Job states the child writes (alhazen.modes.measure_jobs), with the word
   * and lamp each gets. Only passed and measured are results. */
  const STATES = {
    queued: ['Queued', 'idle'],
    running: ['Running', 'run'],
    waiting: ['Waiting for operator', 'wait'],
    passed: ['Passed', 'ok'],
    measured: ['Measured', 'ok'],
    failed: ['Failed', 'bad'],
    error: ['Error', 'bad'],
    unavailable: ['Unavailable', 'off'],
    cancelled: ['Cancelled', 'off'],
    blocked: ['Not run', 'off'],
  };
  const RESULTS = ['passed', 'measured'];
  const STAND_IN_TRACKERS = ['mouse_sim', 'scripted'];

  function byOrder(a, b) {
    return a.order - b.order || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0);
  }

  function create(catalog, selected = []) {
    const entries = [...(catalog || [])].sort(byOrder);
    const keys = new Set(entries.map((e) => e.key));
    return {entries, selected: new Set(selected.filter((k) => keys.has(k)))};
  }

  function entry(state, key) {
    return state.entries.find((e) => e.key === key);
  }

  /* Ticking a job ticks what it requires; unticking one unticks what
   * requires it. Either way the note says what else changed, so nothing
   * changes behind the reader's back. */
  function toggle(state, key, on) {
    const selected = new Set(state.selected);
    const changed = [];
    if (on) {
      const visit = (k) => {
        if (selected.has(k)) return;
        selected.add(k);
        if (k !== key) changed.push(k);
        (entry(state, k)?.requires || []).forEach(visit);
      };
      visit(key);
    } else {
      const drop = (k) => {
        if (!selected.has(k)) return;
        selected.delete(k);
        if (k !== key) changed.push(k);
        state.entries.filter((e) => (e.requires || []).includes(k)).forEach((e) => drop(e.key));
      };
      drop(key);
    }
    const titles = changed.map((k) => entry(state, k)?.title || k);
    let note = '';
    if (titles.length) note = (on ? 'Also selected, needed first: ' : 'Also cleared, it needs this: ')
      + titles.join(', ');
    return {state: {entries: state.entries, selected}, note};
  }

  function selection(state) {
    return state.entries.filter((e) => state.selected.has(e.key)).map((e) => e.key);
  }

  function problem(state) {
    if (!state.entries.length) return 'This experiment’s alhazen lists no measurements.';
    if (!state.selected.size) return 'Choose at least one measurement.';
    for (const key of state.selected) {
      const missing = (entry(state, key)?.requires || []).filter((k) => !state.selected.has(k));
      if (missing.length) return `${entry(state, key).title} needs ${missing.join(', ')}.`;
    }
    return '';
  }

  function subjectNeed(state) {
    const needs = selection(state).map((k) => entry(state, k).subject || 'none');
    if (needs.includes('required')) return 'required';
    if (needs.includes('optional')) return 'optional';
    return 'none';
  }

  /* What the rig file already says this job will not find (mirrors each
   * job's own unavailable(); the child has the last word). '' = nothing. */
  function rigHint(job, rig) {
    if (!rig) return '';
    const devices = rig.devices || {};
    const needs = job.needs || [];
    if (job.key === 'monitor.colour') return 'No colorimeter support';
    if (needs.includes('display') && rig.display?.backend === 'simulated') {
      return 'Display is simulated';
    }
    if (job.key.startsWith('tracker.') || needs.includes('tracker')) {
      const tracker = devices.eyetracker?.backend;
      if (!tracker) return 'No eye tracker on this rig';
      if (STAND_IN_TRACKERS.includes(tracker)) return `Tracker is the ${tracker} stand-in`;
    }
    if (job.key.startsWith('reward.')) {
      const reward = devices.reward?.backend;
      if (!reward) return 'No reward device on this rig';
      if (reward === 'simulated') return 'Reward is simulated';
    }
    if (job.key === 'neural.stream') {
      const spikes = devices.spikes?.backend;
      if (!spikes) return 'No acquisition configured (devices.spikes)';
      if (spikes === 'simulated') return 'Spike source is simulated';
    }
    return '';
  }

  function groups(state) {
    const out = [];
    for (const e of state.entries) {
      let group = out.find((g) => g.name === e.group);
      if (!group) out.push(group = {name: e.group, entries: []});
      group.entries.push(e);
    }
    return out;
  }

  function stateWord(name) {
    return STATES[name] || [name, 'idle'];
  }

  /* The queue in a few numbers, for a heading. */
  function summary(status) {
    if (!status) return '';
    const jobs = status.jobs || [];
    const results = jobs.filter((j) => RESULTS.includes(j.state)).length;
    const failed = jobs.filter((j) => ['failed', 'error'].includes(j.state)).length;
    const parts = [`${status.done ?? 0} of ${status.total ?? jobs.length} done`];
    if (results) parts.push(`${results} with a result`);
    if (failed) parts.push(`${failed} failed`);
    if (status.stopped) parts.push('stopped');
    return parts.join(' · ');
  }

  /* -------------------------------------------------------------- */
  /* Renderers: each draws into the container it is handed only.     */
  /* -------------------------------------------------------------- */

  function el(doc, tag, className, text) {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function renderChecklist(container, state, {rig = null, onToggle = () => {}, note = '',
    document: doc = globalThis.document} = {}) {
    container.replaceChildren();
    const order = selection(state);
    for (const group of groups(state)) {
      const block = el(doc, 'div', 'mx-group');
      block.append(el(doc, 'h4', 'mx-group-title', group.name));
      for (const job of group.entries) {
        const id = `mx-${job.key.replace(/[^a-z0-9-]/g, '-')}`;
        const row = el(doc, 'label', 'mx-item');
        row.htmlFor = id;
        const box = el(doc, 'input');
        box.type = 'checkbox';
        box.id = id;
        box.value = job.key;
        box.checked = state.selected.has(job.key);
        box.addEventListener('change', () => onToggle(job.key, box.checked));
        const text = el(doc, 'span', 'mx-text');
        const title = el(doc, 'span', 'mx-title');
        const position = order.indexOf(job.key);
        if (position >= 0) {
          const step = el(doc, 'span', 'mx-step', String(position + 1));
          step.setAttribute('aria-label', `runs ${position + 1}`);
          title.append(step);
        }
        title.append(el(doc, 'span', '', job.title));
        text.append(title, el(doc, 'span', 'mx-desc', job.description || ''));
        const labels = [];
        if (job.subject === 'required') labels.push(['mx-tag', 'subject']);
        if ((job.needs || []).includes('operator')) labels.push(['mx-tag', 'operator input']);
        if (job.provider && job.provider !== 'alhazen') labels.push(['mx-tag', job.provider]);
        const hint = rigHint(job, rig);
        if (hint) {
          row.classList.add('mx-unavailable');
          labels.push(['mx-tag mx-tag-warn', hint]);
        }
        if (labels.length) {
          const tags = el(doc, 'span', 'mx-tags');
          tags.append(...labels.map(([cls, word]) => el(doc, 'span', cls, word)));
          text.append(tags);
        }
        row.append(box, text);
        block.append(row);
      }
      container.append(block);
    }
    const foot = el(doc, 'p', 'mx-order');
    foot.textContent = order.length
      ? `Runs in this order: ${order.map((k) => state.entries.find((e) => e.key === k).title).join(' → ')}`
      : 'Nothing selected: tick the measurements to run, one after another.';
    container.append(foot);
    if (note) container.append(el(doc, 'p', 'mx-note', note));
  }

  function renderProgress(container, status, {document: doc = globalThis.document} = {}) {
    container.replaceChildren();
    container.hidden = !status;
    if (!status) return;
    const head = el(doc, 'div', 'mx-progress-head');
    head.append(el(doc, 'h3', 'mx-progress-title', 'Measurements'),
      el(doc, 'span', 'mx-progress-count', summary(status)));
    const bar = el(doc, 'div', 'mx-bar');
    const fill = el(doc, 'span', 'mx-bar-fill');
    const total = status.total || (status.jobs || []).length || 1;
    fill.style.width = `${Math.round(100 * (status.done || 0) / total)}%`;
    bar.append(fill);
    const list = el(doc, 'ol', 'mx-queue');
    for (const job of status.jobs || []) {
      const [word, lamp] = stateWord(job.state);
      const item = el(doc, 'li', `mx-job mx-${lamp}`);
      if (job.key === status.current) item.classList.add('mx-current');
      const top = el(doc, 'div', 'mx-job-top');
      top.append(el(doc, 'span', 'mx-job-title', job.title), el(doc, 'span', `mx-state mx-${lamp}`, word));
      item.append(top);
      if (job.waiting_for) item.append(el(doc, 'p', 'mx-wait', `At the rig: ${job.waiting_for}`));
      if (job.summary) item.append(el(doc, 'p', 'mx-summary', job.summary));
      list.append(item);
    }
    container.append(head, bar, list);
    if (status.report) container.append(el(doc, 'p', 'mx-report', `Report: ${status.report}`));
  }

  return {
    STATES, create, toggle, selection, problem, subjectNeed, rigHint, groups, summary,
    stateWord, renderChecklist, renderProgress,
  };
})();

if (typeof window !== 'undefined') window.MeasureChoice = MeasureChoice;
