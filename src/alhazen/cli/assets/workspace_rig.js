/* Alhazen experiment workspace — this machine's rig, on the Run and General pages.
 *
 * A dashboard workspace is one machine, and every session it launches runs on
 * that machine's rig (alhazen/cli/machine_rig.py). The Run page's Rig section
 * starts from it, lets the person change it for THIS session only — the same
 * way they change the task parameters: settings in sections, each changed
 * value marked and resettable — and assigns the task's events to sync lines.
 * "Save to this machine's rig" makes a session's changes the machine's.
 *
 *   RigEditor.changes(machine, current)        what the session changed
 *   RigEditor.carryChanges(previous, next)     keep them across a task change
 *   RigEditor.eventRows(values, all, own)      the Event lines table's rows
 *   RigEditor.setLine / setDiode               edit one event's assignment
 *   RigEditor.renderFields(host, rig, opts)    the settings, in sections
 *   RigEditor.renderEvents(host, rig, opts)    the Event lines block
 *   RigEditor.setupCard(host, status, opts)    "set up this machine's rig"
 *
 * `rig` is the Run page's session rig: {machine: {name, values, sha256, ...},
 * prefill, current, events, taskEvents, source, dropped}. The pure helpers
 * touch no DOM, so tests/js/workspace_rig.test.mjs loads them on their own;
 * the renderers take the page's `node(tag, class, text)`.
 */
'use strict';

const RigEditor = (() => {
  const EVENT_LINES = ['devices', 'sync', 'event_lines'];
  const PHOTODIODE_EVENTS = ['display', 'photodiode', 'events'];
  const EDF = ['devices', 'eyetracker', 'edf_host_filename'];
  const ACCURACY = ['devices', 'eyetracker', 'accuracy_max_deg'];
  /* The settings the Event lines block edits (each experiment's own), which
   * the fields leave out; real_data, which only the machine's rig decides. */
  const EVENT_PATHS = [EVENT_LINES.join('.'), PHOTODIODE_EVENTS.join('.')];
  const MACHINE_ONLY = ['real_data'];
  /* The headings, in the order the fields show them. */
  const SECTIONS = [
    ['monitor', 'Monitor'], ['display', 'Display'], ['eyetracker', 'Eye tracker'],
    ['reward', 'Reward'], ['sync', 'Sync'], ['devices', 'Other devices'],
    ['live_monitor', 'Live monitor'], ['data', 'Data'], ['other', 'Other'],
  ];

  const isObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
  const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));
  /* Two values the same, whatever order a mapping's keys came in. */
  function stable(v) {
    if (Array.isArray(v)) return v.map(stable);
    if (isObject(v)) return Object.fromEntries(Object.keys(v).sort().map((k) => [k, stable(v[k])]));
    return v;
  }
  const same = (a, b) => JSON.stringify(stable(a)) === JSON.stringify(stable(b));

  function get(values, path) {
    let current = values;
    for (const key of path) {
      if (!isObject(current) || !Object.hasOwn(current, key)) return {found: false};
      current = current[key];
    }
    return {found: true, value: current};
  }

  /* Set `path` where its parent section exists; false, and nothing set, when
   * it does not (an event line is no use on a machine with no sync device). */
  function set(values, path, value) {
    const parent = get(values, path.slice(0, -1));
    if (!parent.found || !isObject(parent.value)) return false;
    parent.value[path.at(-1)] = value;
    return true;
  }

  function remove(values, path) {
    const parent = get(values, path.slice(0, -1));
    if (parent.found && isObject(parent.value)) delete parent.value[path.at(-1)];
  }

  /** Every setting `current` has differently from `machine`, as
   *  {path, machine, session, absent}: sections compared key by key, a list
   *  or a value whole — exactly as the server records them (rig_changes). */
  function changes(machine, current, prefix = []) {
    const out = [];
    if (isObject(machine) && isObject(current)) {
      const keys = [...Object.keys(machine), ...Object.keys(current).filter((k) => !Object.hasOwn(machine, k))];
      for (const key of keys) {
        const path = [...prefix, key];
        if (!Object.hasOwn(current, key)) out.push({path: path.join('.'), machine: machine[key], absent: 'session'});
        else if (!Object.hasOwn(machine, key)) out.push({path: path.join('.'), session: current[key], absent: 'machine'});
        else out.push(...changes(machine[key], current[key], path));
      }
      return out;
    }
    if (!same(machine, current)) out.push({path: prefix.join('.'), machine, session: current});
    return out;
  }

  const isEventPath = (dotted) => EVENT_PATHS.some((p) => dotted === p || dotted.startsWith(p + '.'));

  /** The session's own changes to the machine's settings, event lines apart. */
  function settingChanges(rig) {
    return changes(rig.machine.values, rig.current).filter((c) => !isEventPath(c.path));
  }

  /** Put `current`'s value at `path` back to the machine's (or remove it). */
  function reset(rig, path) {
    const found = get(rig.machine.values, path);
    if (found.found) {
      if (!set(rig.current, path, clone(found.value))) {
        // A whole section the session removed: put the section back.
        set(rig.current, path.slice(0, 1), clone(get(rig.machine.values, path.slice(0, 1)).value));
      }
    } else remove(rig.current, path);
  }

  /** Every setting back to the machine's; the event lines to where they started. */
  function resetAll(rig) {
    const next = clone(rig.machine.values);
    for (const path of [EVENT_LINES, PHOTODIODE_EVENTS, EDF, ACCURACY]) {
      const started = get(rig.prefill, path);
      if (started.found) set(next, path, clone(started.value));
      else remove(next, path);
    }
    rig.current = next;
  }

  /** Keep the person's own setting changes when the Rig section is read again
   *  for another task or mode (`next` is the fresh answer, `previous` the old
   *  rig): the events start over for the new task, the settings do not. */
  function carryChanges(previous, next) {
    for (const change of settingChanges(previous)) {
      const path = change.path.split('.');
      if (change.absent === 'session') remove(next.current, path);
      else if (!set(next.current, path, clone(change.session))) {
        // Its section is new to the machine too: carry the whole section.
        set(next.current, path.slice(0, 1), clone(get(previous.current, path.slice(0, 1)).value));
      }
    }
    return next;
  }

  /** The events to list in the Event lines table: the task's own first, then
   *  the ones every task has, then any the rig names that the task does not
   *  (shown so they can be cleared), each with its line and photodiode tick. */
  function eventRows(values, all, own) {
    const lines = get(values, EVENT_LINES).value || {};
    const diode = get(values, PHOTODIODE_EVENTS).value || [];
    const known = new Set(all || []);
    const mine = new Set(own || (all ? [] : Object.keys(lines)));
    const names = [...new Set([...(own || []), ...(all || []), ...Object.keys(lines), ...diode])];
    const kind = (name) => {
      if (all && !known.has(name)) return 'undeclared';
      if (mine.has(name)) return 'task';
      return own ? 'common' : 'task';
    };
    const order = {undeclared: 0, task: 1, common: 2};
    return names
      .map((name) => ({name, line: lines[name] || '', diode: diode.includes(name), kind: kind(name)}))
      .sort((a, b) => order[a.kind] - order[b.kind] || a.name.localeCompare(b.name));
  }

  /** Assign `event` to `line` (empty: to none). An empty assignment is taken
   *  out rather than kept as {}: the machine has none, and none is no change. */
  function setLine(values, event, line) {
    const sync = get(values, ['devices', 'sync']);
    if (!sync.found || !isObject(sync.value)) return false;
    const lines = {...(sync.value.event_lines || {})};
    if (line.trim()) lines[event] = line.trim();
    else delete lines[event];
    if (Object.keys(lines).length) sync.value.event_lines = lines;
    else delete sync.value.event_lines;
    return true;
  }

  function setDiode(values, event, on) {
    const diode = get(values, ['display', 'photodiode']);
    if (!diode.found || !isObject(diode.value)) return false;
    const events = (diode.value.events || []).filter((e) => e !== event);
    if (on) events.push(event);
    if (events.length) diode.value.events = events;
    else delete diode.value.events;
    return true;
  }

  /** Lines to suggest for a sync backend: those already named, then the
   *  device's own. */
  function suggestedLines(values, rows) {
    const backend = get(values, ['devices', 'sync', 'backend']).value;
    const named = rows.map((r) => r.line).filter(Boolean);
    let device = [];
    if (backend === 'nidaq' || backend === 'simulated') {
      device = Array.from({length: 8}, (_, i) => `Dev1/port0/line${i}`);
    } else if (backend === 'datapixx') {
      device = Array.from({length: 24}, (_, i) => `DATAPIXX3/dout${i}`);
    }
    return [...new Set([...named, ...device])];
  }

  /** The heading a setting's row goes under. */
  function sectionOf(path) {
    const [first, second] = path;
    if (first === 'monitor' || first === 'display') return first;
    if (first === 'devices') return ['eyetracker', 'reward', 'sync'].includes(second) ? second : 'devices';
    if (first === 'live_monitor' || first === 'dashboard') return 'live_monitor';
    if (['data_root', 'database', 'real_data'].includes(first)) return 'data';
    return 'other';
  }

  /** Each leaf setting of `values` as {path, value}: sections are walked, a
   *  list or an empty section is a value. */
  function leaves(values, prefix = []) {
    const out = [];
    for (const [key, value] of Object.entries(values || {})) {
      const path = [...prefix, key];
      if (isObject(value) && Object.keys(value).length) out.push(...leaves(value, path));
      else out.push({path, value});
    }
    return out;
  }

  /** The text a value is shown as in a change list. */
  function show(value) {
    if (value === undefined) return 'not set';
    if (typeof value === 'string') return value;
    return JSON.stringify(value);
  }

  /* ---------------------------------------------------------------- */
  /* The settings, in sections                                         */
  /* ---------------------------------------------------------------- */

  /**
   * Draw the session's settings into `host`: one row per setting of the
   * machine's rig (and any the session added), under the Monitor, Display,
   * Eye tracker, … headings, each with the control its value takes. A row
   * the session changed is marked and has a reset button giving the
   * machine's value. Options: node, schema (RigConfig's JSON schema, for
   * choices), query (the search text), disabled, onChange().
   */
  function renderFields(host, rig, {node, schema = null, query = '', disabled = false, onChange}) {
    host.replaceChildren();
    const changed = new Map(settingChanges(rig).map((c) => [c.path, c]));
    const rows = leaves(rig.current).filter((l) => !isEventPath(l.path.join('.')));
    // A setting the session removed is still listed, so it can be put back.
    for (const c of changed.values()) {
      if (c.absent === 'session') rows.push({path: c.path.split('.'), value: undefined});
    }
    const buckets = new Map(SECTIONS.map(([key]) => [key, []]));
    let number = 0;
    const needle = query.trim().toLowerCase().replaceAll(' ', '_');
    for (const {path, value} of rows) {
      const dotted = path.join('.');
      if (needle && !dotted.toLowerCase().includes(needle)) continue;
      const change = changed.get(dotted)
        || [...changed.values()].find((c) => dotted.startsWith(c.path + '.'));
      const row = node('div', 'parameter-row rig-row' + (change ? ' changed' : ''));
      row.dataset.key = dotted.toLowerCase();
      const id = `rig-${number++}`;
      const lab = node('label', '', path.at(-1).replaceAll('_', ' '));
      lab.htmlFor = id;
      if (path.length > 1) lab.append(node('small', '', path.slice(0, -1).join(' / ')));
      const control = controlFor(path, value, id, {node, schema, rig, disabled, onChange});
      const cell = node('div', 'rig-value');
      cell.append(control);
      if (change) {
        const undo = node('button', 'rig-reset', '↺');
        undo.type = 'button';
        const machineValue = change.absent === 'machine' ? 'not set' : show(get(rig.machine.values, path).value ?? change.machine);
        undo.title = `Back to this machine’s value: ${machineValue}`;
        undo.setAttribute('aria-label', `Reset ${path.join(' ')} to this machine’s value, ${machineValue}`);
        undo.disabled = disabled;
        undo.addEventListener('click', () => {
          reset(rig, change.path.split('.'));
          onChange();
        });
        cell.append(undo);
      }
      row.append(lab, cell);
      buckets.get(sectionOf(path)).push(row);
    }
    for (const [key, label] of SECTIONS) {
      const list = buckets.get(key);
      if (!list.length) continue;
      const section = node('section', 'parameter-section');
      const heading = node('h4', 'parameter-section-title');
      heading.id = `rig-section-${key}`;
      section.setAttribute('aria-labelledby', heading.id);
      const marked = list.filter((r) => r.classList.contains('changed')).length;
      heading.append(node('span', 'parameter-section-name', label), node('span', 'parameter-section-count', String(list.length)));
      if (marked) heading.append(node('span', 'rig-section-changed', `${marked} changed`));
      section.append(heading, ...list);
      host.append(section);
    }
    if (!host.children.length) host.append(node('p', 'help', needle ? 'No setting matches.' : 'This rig has no settings.'));
  }

  /* The control for one setting: a choice for a value the schema lists, a
   * checkbox for a boolean, a number field, a text field, or JSON for a list
   * or null. real_data is the machine's alone: shown, never editable here. */
  function controlFor(path, value, id, {node, schema, rig, disabled, onChange}) {
    const dotted = path.join('.');
    if (value === undefined) {
      const span = node('span', 'rig-removed', 'removed for this session');
      span.id = id;
      return span;
    }
    if (MACHINE_ONLY.includes(dotted)) {
      const span = node('span', 'rig-fixed', `${value} (this machine’s)`);
      span.id = id;
      span.title = 'Whether real data may be collected is decided in this machine’s rig, not per session.';
      return span;
    }
    const choice = typeof value === 'string' && typeof ParameterChoices !== 'undefined' && schema
      ? ParameterChoices.describe(schema, path, value) : null;
    const write = (next) => {
      set(rig.current, path, next);
      onChange();
    };
    if (choice && !choice.multiple && choice.choices.length > 1) {
      const select = node('select');
      select.id = id;
      for (const option of choice.choices) {
        const o = node('option', '', option || '(empty)');
        o.value = option;
        select.append(o);
      }
      select.value = value;
      select.disabled = disabled;
      select.addEventListener('change', () => write(select.value));
      return select;
    }
    const complex = Array.isArray(value) || value === null || isObject(value);
    const input = node(complex ? 'textarea' : 'input');
    input.id = id;
    input.disabled = disabled;
    if (typeof value === 'boolean') {
      input.type = 'checkbox';
      input.checked = value;
    } else if (typeof value === 'number') {
      input.type = 'number';
      input.step = 'any';
      input.value = value;
      input.required = true;
    } else {
      input.value = complex ? JSON.stringify(value) : String(value);
    }
    input.addEventListener(typeof value === 'boolean' ? 'change' : 'input', () => {
      input.setCustomValidity?.('');
      try {
        let next;
        if (typeof value === 'boolean') next = input.checked;
        else if (typeof value === 'number') {
          next = Number(input.value);
          if (!input.value || !Number.isFinite(next)) throw new Error('Enter a finite number');
        } else if (complex) next = JSON.parse(input.value);
        else next = input.value;
        set(rig.current, path, next);
      } catch (e) {
        input.setCustomValidity?.(e.message);
        return;
      }
      onChange({quiet: true});
    });
    // Redraw (and re-mark) once the edit is done, not on every keystroke.
    input.addEventListener('change', () => onChange());
    return input;
  }

  /* ---------------------------------------------------------------- */
  /* Event lines                                                       */
  /* ---------------------------------------------------------------- */

  /**
   * Draw the Event lines block: where the assignment came from, then one row
   * per event — the line it goes out on, and whether it flashes the
   * photodiode — the task's own events first, the ones every task has folded
   * away. Then the two other settings each experiment adds on a tracker: the
   * EDF file name and a calibration limit. Options: node, disabled, onChange.
   */
  function renderEvents(host, rig, {node, disabled = false, onChange}) {
    host.replaceChildren();
    const values = rig.current;
    const hasSync = isObject(get(values, ['devices', 'sync']).value);
    const hasDiode = isObject(get(values, ['display', 'photodiode']).value);
    const tracker = get(values, ['devices', 'eyetracker']).value;
    const rows = eventRows(values, rig.events, rig.taskEvents);
    const head = node('div', 'rig-events-head');
    head.append(node('h4', 'rig-subtitle', 'Event lines'));
    const assigned = rows.filter((r) => r.line).length;
    const flashed = rows.filter((r) => r.diode).length;
    let summary;
    if (!hasSync && !hasDiode) summary = 'This machine has no sync device or photodiode, so no event is marked.';
    else if (!hasSync) summary = flashed ? `${flashed} event${flashed === 1 ? '' : 's'} flash the photodiode; no sync device.` : 'No sync device; no event flashes the photodiode.';
    else summary = assigned
      ? `${assigned} event${assigned === 1 ? '' : 's'} go out on a line; every other event pulses nothing.`
      : 'No event goes out on a line.';
    head.append(node('p', 'rig-events-summary', summary));
    host.append(head);
    if (rig.source?.label) host.append(node('p', 'help rig-events-source', `Started ${rig.source.label}.`));
    for (const note of rig.dropped || []) host.append(node('p', 'help rig-events-dropped', `Left out: ${note}.`));
    if (hasSync || hasDiode) {
      const lines = suggestedLines(values, rows);
      const listId = 'rig-line-choices';
      const datalist = node('datalist');
      datalist.id = listId;
      for (const line of lines) {
        const o = node('option');
        o.value = line;
        datalist.append(o);
      }
      const table = (list) => {
        const t = node('table', 'rig-event-table');
        const tr = node('tr');
        tr.append(node('th', '', 'Event'));
        if (hasSync) tr.append(node('th', '', 'Line'));
        if (hasDiode) tr.append(node('th', '', 'Photodiode'));
        t.append(tr);
        for (const row of list) {
          const r = node('tr', row.kind === 'undeclared' ? 'rig-event-undeclared' : (row.line || row.diode ? 'rig-event-on' : ''));
          const name = node('td', 'rig-event-name', row.name);
          if (row.kind === 'undeclared') name.append(node('span', 'rig-event-flag', ' not an event of this task'));
          r.append(name);
          if (hasSync) {
            const cell = node('td');
            const input = node('input');
            input.value = row.line;
            input.placeholder = 'none';
            input.setAttribute('list', listId);
            input.setAttribute('aria-label', `Line for ${row.name}`);
            input.disabled = disabled;
            input.addEventListener('change', () => {
              setLine(rig.current, row.name, input.value);
              onChange();
            });
            cell.append(input);
            r.append(cell);
          }
          if (hasDiode) {
            const cell = node('td');
            const check = node('input');
            check.type = 'checkbox';
            check.checked = row.diode;
            check.disabled = disabled;
            check.setAttribute('aria-label', `${row.name} flashes the photodiode`);
            check.addEventListener('change', () => {
              setDiode(rig.current, row.name, check.checked);
              onChange();
            });
            cell.append(check);
            r.append(cell);
          }
          t.append(r);
        }
        return t;
      };
      const main = rows.filter((r) => r.kind !== 'common' || r.line || r.diode);
      const common = rows.filter((r) => r.kind === 'common' && !r.line && !r.diode);
      host.append(datalist);
      if (main.length) host.append(table(main));
      if (common.length) {
        const fold = node('details', 'rig-event-more');
        fold.append(node('summary', '', `Events every task has (${common.length})`), table(common));
        host.append(fold);
      }
      if (!rows.length) host.append(node('p', 'help', 'The task’s events could not be read; the session checks the names itself.'));
    }
    if (isObject(tracker)) {
      const extra = node('div', 'rig-tracker-extra');
      const field = (label, path, placeholder, numeric) => {
        const wrap = node('label', 'rig-inline-field');
        wrap.append(node('span', '', label));
        const input = node('input');
        input.type = numeric ? 'number' : 'text';
        if (numeric) input.step = 'any';
        const found = get(values, path);
        input.value = found.found && found.value != null ? found.value : '';
        input.placeholder = placeholder;
        input.disabled = disabled;
        input.addEventListener('change', () => {
          const text = input.value.trim();
          if (!text) remove(rig.current, path);
          else set(rig.current, path, numeric ? Number(text) : text);
          onChange();
        });
        wrap.append(input);
        return wrap;
      };
      if (tracker.backend === 'eyelink') extra.append(field('EDF file name', EDF, 'alhazen.EDF', false));
      extra.append(field('Calibration limit (deg)', ACCURACY, '1.0', true));
      host.append(extra);
    }
  }

  /* ---------------------------------------------------------------- */
  /* Setting the machine up                                            */
  /* ---------------------------------------------------------------- */

  /**
   * The card shown until this machine has a rig: its name and the rig it
   * starts from — a registered experiment's rig or one of alhazen's shared
   * rigs, with a line on each — then Set up. `status` is /api/machine-rig's
   * answer before set-up ({templates, suggested}). Options: node, api,
   * onDone(status).
   */
  function setupCard(host, status, {node, api, onDone}) {
    host.replaceChildren();
    const card = node('div', 'rig-setup');
    card.append(node('h4', 'rig-subtitle', 'Set up this machine’s rig'));
    card.append(node('p', 'help', 'Every session on this machine runs on one rig. Choose the rig it '
      + 'starts from and name the machine; it is written out as one complete file you can edit '
      + 'later, and a session can still change it for itself.'));
    if (status.error) card.append(node('p', 'rig-problem', status.error));
    const templates = (status.templates || []).filter((t) => !t.error);
    const form = node('form', 'rig-setup-form');
    const from = node('select');
    from.id = 'rig-setup-from';
    const groups = [
      ['Registered experiments’ rigs', templates.filter((t) => t.kind === 'experiment')],
      ['alhazen’s shared rigs', templates.filter((t) => t.kind === 'shared')],
    ];
    for (const [label, list] of groups) {
      if (!list.length) continue;
      const group = node('optgroup');
      group.label = label;
      for (const t of list) {
        const o = node('option', '', t.label);
        o.value = t.id;
        group.append(o);
      }
      from.append(group);
    }
    const initial = status.suggested || templates[0]?.id || '';
    from.value = initial;
    const name = node('input');
    name.id = 'rig-setup-name';
    name.required = true;
    name.maxLength = 64;
    name.pattern = '[A-Za-z0-9][A-Za-z0-9._-]*';
    const facts = node('p', 'help rig-setup-facts');
    const describe = () => {
      const t = templates.find((x) => x.id === from.value);
      if (!t) {
        facts.textContent = '';
        return;
      }
      if (!name.dataset.typed) name.value = t.name;
      const parts = [t.summary];
      parts.push(t.real_data ? 'collects real data' : 'development rig (run mode refused)');
      if (t.extends) parts.push(`written out over alhazen/${t.extends}`);
      if (t.events?.length) parts.push(`its event lines (${t.events.join(', ')}) are left out and chosen per session`);
      facts.textContent = parts.join(' · ');
    };
    name.addEventListener('input', () => { name.dataset.typed = '1'; });
    from.addEventListener('change', describe);
    const row = (label, control) => {
      const wrap = node('div', 'rig-setup-row');
      const lab = node('label', '', label);
      lab.htmlFor = control.id;
      wrap.append(lab, control);
      return wrap;
    };
    const result = node('p', 'rig-problem');
    result.hidden = true;
    const submit = node('button', 'primary', 'Set up this machine’s rig');
    submit.type = 'submit';
    form.append(row('Start from', from), facts, row('Machine name', name), result, submit);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      submit.disabled = true;
      result.hidden = true;
      try {
        const answer = await api('/api/machine-rig/setup', {name: name.value.trim(), template: from.value});
        await onDone(answer);
      } catch (e) {
        result.textContent = e.message;
        result.hidden = false;
      } finally {
        submit.disabled = false;
      }
    });
    if (!templates.length) {
      card.append(node('p', 'rig-problem', 'No rig to start from: register an experiment, or run '
        + '`alhazen rig init` in a terminal.'));
    } else {
      card.append(form);
      describe();
    }
    host.append(card);
  }

  return {
    EVENT_LINES, PHOTODIODE_EVENTS, EDF, ACCURACY, SECTIONS,
    clone, same, get, set, remove, changes, settingChanges, reset, resetAll, carryChanges,
    eventRows, setLine, setDiode, suggestedLines, sectionOf, leaves, show,
    renderFields, renderEvents, setupCard,
  };
})();

// Also on window, for the scripts that look modules up there (workspace_manage.js).
if (typeof window !== 'undefined') window.RigEditor = RigEditor;
