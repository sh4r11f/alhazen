/* Alhazen experiment workspace — the launcher page's behaviour.
 *
 * The page is served by alhazen/cli/dashboard.py and talks to it over a
 * small JSON API (/api/state, /api/runs, /api/config, …). Everything shown
 * is a rendering of the server's state: the browser keeps no truth of its
 * own beyond which project and run are selected and what the reader has
 * typed, so a reload — or a second tab — shows the same workspace.
 *
 * How it stays current: poll() asks for /api/state every 1.5 s and redraws.
 * Redraws are cheap because each list (projects, history, gallery) keeps a
 * JSON "signature" of what it last drew and skips the DOM work when nothing
 * moved; a playing video must not be replaced under the reader on every poll.
 *
 * The Run output card has three tabs: Media (the run's images and finished
 * movies), Console (its output tail) and Live monitor, which frames the
 * session's own dashboard — a separate loopback server the session starts —
 * while the run is active (see renderMonitor).
 *
 * Companion: workspace_parameters.js (ParameterChoices) holds the schema
 * lookups that decide which parameter fields become dropdowns. It is loaded
 * first by workspace.html and tested on its own.
 */
'use strict';

/** The one DOM lookup this script uses: every control has an id. */
const $ = (id) => document.getElementById(id);

/* The built-in modes as [button label, help sentence], in menu order. A
 * project's discovered scripts (preview.py, movie.py) join the menu at
 * runtime with labels the server supplies; see chooseProject. */
const MODES = {
  simulate: ['Simulate', 'Run the complete session with a simulated participant.'],
  demo: ['Demo', 'View the stimulus on the rig’s display. Screenshots you save appear here.'],
  movie: ['Record movies', 'Render stimulus clips without opening a display.'],
  test: ['Test session', 'Rehearse with a person, using fewer trials and rehearsal data.'],
  run: [
    'Run experiment',
    'Collect a real session using the selected rig and its data directory.',
  ],
  measure: ['Measure rig', 'Check the physical display, response keys and eye tracker.'],
};

/* The API token. The opening URL carries it in the fragment (#token=…), which
 * a browser never sends to a server, so it stays out of request logs; the
 * page keeps it for reloads and then takes it out of the address bar.
 * sessionStorage, not localStorage: the token should die with the tab, as
 * the server that issued it dies with the terminal. */
let token = new URLSearchParams(location.hash.slice(1)).get('token')
  || sessionStorage.getItem('alhazen-workspace-token')
  || '';
if (token) sessionStorage.setItem('alhazen-workspace-token', token);
if (location.hash) history.replaceState(null, '', location.pathname);

/* The server's last /api/state answer: the registered projects, every run
 * (newest first) and the id of the one active run, or null. */
let state = {projects: [], runs: [], active: null};
/* Which project and which run the page is looking at. */
let selected = null;
let runId = null;
/* The parameter values being edited (null means "Task defaults": nothing to
 * edit) and which editor shows them, 'fields' or 'yaml'. */
let values = null;
let editor = 'fields';
/* The project open in the settings dialog, or null for "Add experiment". */
let editProject = null;
/* Epochs for the two loads a reader can re-trigger faster than they finish:
 * each load takes the next number and, after awaiting, writes its answer
 * only if no newer load has started. Without this a slow answer for the
 * previous rig or preset would land on top of the current one. */
let configEpoch = 0;
let rigEpoch = 0;
/* JSON signatures of what each list last drew (see the file comment). */
let gallerySignature = '';
let historySignature = '';
let projectSignature = '';
/* In-flight flags. The first three gate the launch button; connectionError
 * remembers that the banner shows a connection failure to clear later. */
let launching = false;
let loadingConfig = false;
let connectionError = false;
let loadingSchema = false;
/* The task's JSON schema, read once per project for the parameter dropdowns;
 * {} until it arrives or when it could not be read. */
let parameterSchema = {};
/* Whether each rig YAML the page has read turns the live dashboard on, keyed
 * "<project id>:<rig path>" (two projects may both have a configs/rig.yaml).
 * The Live monitor tab reads it to say why an active run shows no monitor. */
const rigMonitor = {};
/* The run most recently started from this page, and the run whose monitor
 * tab has already been brought up on its own: the tab is switched once, for
 * the reader who is waiting on the run they launched, and never again. */
let launchedRun = null;
let monitorShown = null;
/* The URL loaded in the monitor frame, '' when it shows about:blank. The
 * frame is (re)loaded only when this changes, never on a poll. */
let framedMonitor = '';

/* A restarted launcher issues a new token and reopens the same tab with it in
 * the fragment: take it, hide it, and redraw everything that embeds it. */
window.addEventListener('hashchange', () => {
  const fresh = new URLSearchParams(location.hash.slice(1)).get('token');
  if (!fresh) return;
  token = fresh;
  sessionStorage.setItem('alhazen-workspace-token', token);
  history.replaceState(null, '', location.pathname);
  // Media URLs carry the token, so the gallery must be rebuilt.
  gallerySignature = '';
  guard(refresh)();
});

/* ------------------------------------------------------------------ */
/* Small helpers                                                       */
/* ------------------------------------------------------------------ */

/** Create an element with a class and text. Text goes in as text, never as
 *  markup: paths, log lines and error messages come from the experiment. */
function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}

/** Show a message in the page's error banner; an empty message hides it. */
function error(message) {
  $('error').textContent = message;
  $('error').hidden = !message;
}

/**
 * One request to the launcher's JSON API: a GET without a body, a POST with
 * one. The token travels in a header so it never appears in a URL. A non-2xx
 * answer becomes an Error carrying the server's own message, which every
 * caller shows through error() — nothing fails quietly.
 */
async function api(path, body) {
  const headers = {'X-Alhazen-Token': token};
  const init = {method: 'GET', headers};
  if (body !== undefined) {
    init.method = 'POST';
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const response = await fetch(path, init);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

/** Wrap an async handler so a rejection lands in the error banner instead
 *  of an unhandled-promise message in a console nobody has open. */
function guard(fn) {
  return async (...args) => {
    try {
      await fn(...args);
    } catch (e) {
      error(e.message);
    }
  };
}

/** The selected project's record, or undefined while none is selected. */
function project() {
  return state.projects.find((p) => p.id === selected);
}

/** A mode's display name: built-in, a discovered script's label, or the id
 *  itself for a run whose script has since disappeared. */
function label(mode) {
  return MODES[mode]?.[0] || project()?.scripts.find((s) => s.id === mode)?.label || mode;
}

/** A short local timestamp for history rows and the run summary. */
function date(value) {
  return new Date(value).toLocaleString([], {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

/** Refill a <select> from [value, text] pairs, keeping the reader's previous
 *  choice when it is still on offer. */
function options(select, items, previous) {
  const nodes = items.map(([value, text]) => {
    const option = node('option', '', text);
    option.value = value;
    return option;
  });
  select.replaceChildren(...nodes);
  if (items.some(([value]) => value === previous)) select.value = previous;
}

/** Whether the selected mode takes task parameters (see ParameterChoices). */
function usesParameters() {
  return ParameterChoices.usesParameters($('mode').value, project()?.scripts || []);
}

/* ------------------------------------------------------------------ */
/* The launch form                                                     */
/* ------------------------------------------------------------------ */

/**
 * The launch button's state and the note under it. Disabled while a run is
 * active (one job at a time), while a launch is in flight, while parameters
 * are still loading (a launch then would silently use defaults for the
 * rest), and when the project has no rig or its interpreter is missing.
 */
function updateLaunch() {
  const p = project();
  const waitingForParameters = usesParameters() && (loadingConfig || loadingSchema);
  $('launch').disabled = !!state.active
    || !p?.rigs.length
    || launching
    || waitingForParameters
    || !p?.available;
  if (launching) $('launch').textContent = 'Starting…';
  else if (state.active) $('launch').textContent = 'A run is in progress';
  else $('launch').textContent = `▶ ${label($('mode').value) || 'Start run'}`;
  let note;
  if (state.active) {
    note = 'One run at a time keeps the rig available to its active experiment.';
  } else if ($('mode').value === 'run') {
    note = 'This mode records real subject data. Check the rig and subject ID before starting.';
  } else {
    note = 'Runs locally in your experiment’s Python environment.';
  }
  $('launch-note').textContent = note;
}

/**
 * Show the controls that apply to the selected mode and hide the rest. A
 * hidden control is also disabled where the browser would otherwise still
 * validate it: a required field the reader cannot see would block submit.
 */
function modeChanged() {
  const mode = $('mode').value;
  const script = project()?.scripts.find((s) => s.id === mode);
  $('mode-help').textContent = MODES[mode]?.[1]
    || 'Run the experiment’s own script and collect its images and movies.';
  // Task parameters: not for measuring the rig, nor for a script without a
  // parameter-file flag. Disabling the fieldset takes its inputs out of
  // form validation as well as out of view.
  $('params-preset-field').hidden = !usesParameters();
  $('params-config').disabled = !usesParameters();
  $('task-parameters').hidden = !usesParameters();
  $('task-parameters').disabled = !usesParameters();
  // Measuring the rig draws nothing random, so it takes no seed.
  $('seed-fields').hidden = mode === 'measure';
  $('seed').disabled = mode === 'measure';
  // Subject and session name the recorded data. A real or rehearsal session
  // must name its subject; a simulation may fall back to its own default.
  $('identity').hidden = !['run', 'test', 'simulate'].includes(mode);
  $('subject').required = ['run', 'test'].includes(mode);
  $('trials-field').hidden = !['test', 'simulate'].includes(mode);
  // Each option is shown for exactly the modes whose CLI accepts the flag.
  $('headless-field').hidden = mode !== 'simulate';
  $('mouse-field').hidden = mode !== 'test';
  $('windowed-field').hidden = !['test', 'run', 'demo', 'measure', 'simulate'].includes(mode);
  $('movie-options').hidden = mode !== 'movie';
  $('script-options').hidden = !script;
  // The flags the launcher sets itself are not offered for retyping.
  const managed = ['--out', '--rig', '--params', '--task-config'];
  let flags = '';
  if (script) {
    const offered = script.flags.filter((f) => !managed.includes(f));
    flags = `Available flags: ${offered.join(', ') || 'none'}`;
  }
  $('script-help').textContent = flags;
  updateLaunch();
}

/**
 * Switch the workspace to project `id`: fill the mode, rig and preset menus,
 * show its most recent run, then load its schema, preset and rig in
 * parallel. Remembered in localStorage so a reload lands on the same one.
 */
async function chooseProject(id) {
  selected = id;
  localStorage.setItem('alhazen-workspace-project', id || '');
  // Every list belongs to the previous project: force each to redraw.
  projectSignature = '';
  historySignature = '';
  gallerySignature = '';
  const p = project();
  if (!p) return;
  $('project-name').textContent = p.name;
  $('project-path').textContent = p.path;
  $('breadcrumb').textContent = p.name;
  // Menu order: a project's "Preview images" script first (the quickest look
  // at the stimulus), the built-in modes, then its other scripts.
  const previews = p.scripts.filter((s) => s.label === 'Preview images');
  const others = p.scripts.filter((s) => s.label !== 'Preview images');
  options($('mode'), [
    ...previews.map((s) => [s.id, s.label]),
    ...Object.entries(MODES).map(([value, [text]]) => [value, text]),
    ...others.map((s) => [s.id, s.label]),
  ]);
  // Rigs are shown by file name. rig-mac.yaml — the scaffold's development
  // rig: a window, no devices — is the safe first choice when present.
  const defaultRig = p.rigs.find((r) => r.endsWith('rig-mac.yaml')) || p.rigs[0];
  options($('rig'), p.rigs.map((r) => [r, r.split('/').pop()]), defaultRig);
  // Presets: "Task defaults" (no file) first, then the configs found; a plain
  // task.yaml is the usual starting point.
  const defaultPreset = p.configs.find((path) => path.endsWith('/task.yaml')) || '';
  options($('params-config'), [
    ['', 'Task defaults'],
    ...p.configs.map((path) => [path, path.split('/').pop()]),
  ], defaultPreset);
  // The server lists runs newest first, so the first match is the latest.
  runId = state.runs.find((r) => r.project === id)?.id || null;
  $('script-args').value = '';
  $('parameter-search').value = '';
  modeChanged();
  renderState();
  // Each of these checks that the project is still selected before it
  // writes; the reader may click another project while they load.
  await Promise.all([loadSchema(id), loadConfig(), loadRig()]);
  await refreshRun();
}

/**
 * Fetch the task's parameter schema for project `id`. Dropdowns need it,
 * but a launch must not be blocked by its absence: on failure the fields
 * render from the current values alone and a notice says why.
 */
async function loadSchema(id) {
  parameterSchema = {};
  loadingSchema = true;
  updateLaunch();
  $('choices-notice').hidden = true;
  try {
    const schema = await api(`/api/schema?project=${encodeURIComponent(id)}`);
    if (selected !== id) return;
    parameterSchema = schema;
  } catch (e) {
    if (selected !== id) return;
    $('choices-notice').textContent = 'Could not load model choices. '
      + `Showing current values; use YAML for other values. ${e.message}`;
    $('choices-notice').hidden = false;
  } finally {
    if (selected === id) {
      loadingSchema = false;
      renderEditor();
      updateLaunch();
    }
  }
}

/**
 * Read the selected rig's YAML and summarise its monitor. The epoch drops
 * the answer for a rig that is no longer the selected one.
 */
async function loadRig() {
  const epoch = ++rigEpoch;
  const p = project();
  const path = $('rig').value;
  $('rig-summary').textContent = path
    ? 'Reading rig…'
    : 'No rig YAML found in configs/. Add a rig to this experiment.';
  if (!path) return;
  const query = `project=${encodeURIComponent(p.id)}&path=${encodeURIComponent(path)}`;
  const data = await api(`/api/config?${query}`);
  if (epoch !== rigEpoch) return;
  const rig = data.values;
  const m = rig.monitor || {};
  // The live dashboard is opt-in (DashboardConfig.enabled defaults to false),
  // so a rig without the block, or without the key, has it off. Remembered
  // for the Live monitor tab, which cannot re-read the YAML on every poll.
  const monitorOn = rig.dashboard?.enabled === true;
  rigMonitor[`${p.id}:${path}`] = monitorOn;
  // '?' rather than 'undefined' for a field the YAML leaves to its default.
  $('rig-summary').textContent =
    `${m.width_px ?? '?'} × ${m.height_px ?? '?'} px · ${m.refresh_rate_hz ?? '?'} Hz`
    + ` · ${rig.display?.backend || 'default display'}\n`
    + `${m.width_cm ?? '?'} cm wide · ${m.distance_cm ?? '?'} cm viewing distance`
    + ` · live monitor: ${monitorOn ? 'on' : 'off'}`;
}

/**
 * Load the selected parameter preset into the editor. The launch button is
 * disabled until it lands: a run started meanwhile would use the task's
 * defaults for everything not yet loaded, silently. "Task defaults" (an
 * empty path) loads nothing and leaves `values` null.
 */
async function loadConfig() {
  const epoch = ++configEpoch;
  const path = $('params-config').value;
  const p = project();
  loadingConfig = true;
  updateLaunch();
  values = null;
  $('parameter-yaml').value = '';
  $('parameter-fields').replaceChildren(node('p', 'help', 'Loading parameters…'));
  try {
    if (path) {
      const query = `project=${encodeURIComponent(p.id)}&path=${encodeURIComponent(path)}`;
      const data = await api(`/api/config?${query}`);
      if (epoch !== configEpoch) return;
      values = data.values;
      $('parameter-yaml').value = data.text;
    }
    // A newly loaded preset always opens in the fields editor.
    editor = 'fields';
    renderEditor();
    loadingConfig = false;
  } finally {
    // A failed config load must not enable a launch with unintended defaults.
    if (epoch === configEpoch) updateLaunch();
  }
}

/* ------------------------------------------------------------------ */
/* The parameter editor                                                */
/* ------------------------------------------------------------------ */

/** Write one edited value into `values` at `path` (an array of keys). */
function setValue(path, value) {
  let parent = values;
  for (const key of path.slice(0, -1)) parent = parent[key];
  parent[path.at(-1)] = value;
}

/**
 * Draw the parameter editor: the two tab buttons' pressed state, which of
 * the fields list and the text area is visible, and — for the fields
 * editor — one row per leaf value in `values`, with nested mappings
 * flattened into "group / key" labels. Every row writes straight into
 * `values` on change, so a launch sends exactly what is on screen.
 */
function renderEditor() {
  $('fields-tab').setAttribute('aria-pressed', String(editor === 'fields'));
  $('yaml-tab').setAttribute('aria-pressed', String(editor === 'yaml'));
  $('parameter-fields').hidden = editor !== 'fields';
  $('parameter-search').hidden = editor !== 'fields';
  $('parameter-yaml').hidden = editor !== 'yaml';
  if (editor !== 'fields') return;
  const host = $('parameter-fields');
  host.replaceChildren();
  if (values === null) {
    const hint = 'Using the task’s own defaults. Choose a parameter preset to edit its values.';
    host.append(node('p', 'help', hint));
    return;
  }
  // Controls are numbered param-0, param-1, … in document order, and each
  // label points at its control by that id.
  let number = 0;
  function addFields(object, prefix = []) {
    for (const [key, value] of Object.entries(object)) {
      const path = [...prefix, key];
      // A nested mapping is a group: recurse, and its rows carry the group
      // path in small print. Arrays are values (edited as JSON), not groups.
      if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
        addFields(value, path);
        continue;
      }
      const row = node('div', 'parameter-row');
      // The search box filters rows on this lower-cased dotted path.
      row.dataset.key = path.join('.').toLowerCase();
      const lab = node('label', '', key.replaceAll('_', ' '));
      lab.htmlFor = `param-${number++}`;
      if (prefix.length) lab.append(node('small', '', prefix.join(' / ')));
      const selection = ParameterChoices.describe(parameterSchema, path, value);
      if (selection) {
        if (selection.multiple) {
          // A list of strings: a <details> dropdown of checkboxes, its
          // summary listing the current selection.
          const dropdown = node('details', 'parameter-multiselect');
          const summary = node('summary', '', value.join(', ') || 'Choose values…');
          summary.id = lab.htmlFor;
          summary.setAttribute('aria-label', path.join(' / '));
          const choices = node('div', 'parameter-options');
          for (const choice of selection.choices) {
            const option = node('label');
            const check = node('input');
            check.type = 'checkbox';
            check.checked = value.includes(choice);
            check.addEventListener('change', () => {
              // Retain existing order: switching one level must not reorder the others.
              const current = path.reduce((v, k) => v[k], values);
              const next = check.checked
                ? [...current, choice]
                : current.filter((v) => v !== choice);
              setValue(path, next);
              summary.textContent = next.join(', ') || 'Choose values…';
            });
            option.append(check, node('span', '', choice));
            choices.append(option);
          }
          dropdown.append(summary, choices);
          row.append(lab, dropdown);
        } else {
          // One string with a known set of values: a <select>.
          const select = node('select');
          select.id = lab.htmlFor;
          options(select, selection.choices.map((v) => [v, v || '(empty)']), value);
          select.addEventListener('change', () => setValue(path, select.value));
          row.append(lab, select);
        }
        host.append(row);
        continue;
      }
      // Everything else: a checkbox for a boolean, a number input for a
      // number, a textarea holding JSON for an array or null, a text input
      // for any other string.
      const complex = Array.isArray(value) || value === null;
      const input = node(complex ? 'textarea' : 'input');
      input.id = lab.htmlFor;
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
      input.addEventListener('input', () => {
        // Parse back to the value's original type. Text that does not parse
        // marks the field invalid — the form then refuses to submit and says
        // why — and leaves `values` as it was.
        input.setCustomValidity('');
        try {
          let next;
          if (typeof value === 'boolean') next = input.checked;
          else if (typeof value === 'number') next = Number(input.value);
          else if (complex) next = JSON.parse(input.value);
          else next = input.value;
          if (typeof value === 'number' && (!input.value || !Number.isFinite(next))) {
            throw new Error('Enter a finite number');
          }
          if (Array.isArray(value) && !Array.isArray(next)) {
            throw new Error('Enter a JSON array, e.g. ["left", "right"]');
          }
          setValue(path, next);
        } catch (e) {
          input.setCustomValidity(e.message);
        }
      });
      row.append(lab, input);
      host.append(row);
    }
  }
  addFields(values);
  filterParameters();
}

/** Hide the rows whose dotted path does not contain the search text. A
 *  space in the query stands for an underscore, as the labels show them. */
function filterParameters() {
  const query = $('parameter-search').value.toLowerCase().replaceAll(' ', '_');
  for (const row of $('parameter-fields').children) {
    if (row.dataset.key) row.hidden = !row.dataset.key.includes(query);
  }
}

/**
 * Move between the fields editor and the text editor. Fields → text writes
 * the current values out as JSON (which is valid YAML), after the form's
 * own validation so a half-typed number is not serialised. Text → fields
 * asks the server to parse the text, so the reader sees the same YAML
 * errors a launch would raise rather than a browser-side approximation.
 */
async function switchEditor(next) {
  if (editor === next) return;
  if (next === 'yaml') {
    if (!$('launch-form').reportValidity()) return;
    $('parameter-yaml').value = values === null ? '' : JSON.stringify(values, null, 2);
  } else {
    const text = $('parameter-yaml').value;
    values = text.trim() ? (await api('/api/parameters', {text})).values : null;
  }
  editor = next;
  renderEditor();
}

/* ------------------------------------------------------------------ */
/* Projects, history and the run output                                */
/* ------------------------------------------------------------------ */

/**
 * Draw what depends on the server state as a whole: welcome screen or
 * workspace, the project list (only when it changed), the launch button
 * and the history list.
 */
function renderState() {
  $('empty').hidden = !!project();
  $('workspace').hidden = !project();
  $('project-count').textContent = state.projects.length;
  const signature = JSON.stringify([state.projects, selected]);
  if (signature !== projectSignature) {
    projectSignature = signature;
    const buttons = state.projects.map((p) => {
      const button = node('button', 'project-button' + (p.id === selected ? ' selected' : ''));
      button.append(node('span', 'project-icon', '◈'), node('span', '', p.name));
      button.setAttribute('aria-current', p.id === selected ? 'page' : 'false');
      button.addEventListener('click', guard(() => chooseProject(p.id)));
      return button;
    });
    $('projects').replaceChildren(...buttons);
  }
  updateLaunch();
  renderHistory();
}

/**
 * The selected project's runs, newest first as the server lists them, with
 * the selected run highlighted. Redrawn only when the list or the selection
 * changed, so a poll does not rebuild the rows under the pointer.
 */
function renderHistory() {
  const runs = state.runs.filter((r) => r.project === selected);
  const signature = JSON.stringify([runs, runId]);
  if (signature === historySignature) return;
  historySignature = signature;
  $('history-count').textContent = `${runs.length} RUN${runs.length === 1 ? '' : 'S'}`;
  const rows = runs.map((run) => {
    const button = node('button', 'history-row' + (run.id === runId ? ' selected' : ''));
    const text = node('span', 'history-text');
    text.append(
      node('strong', '', label(run.mode)),
      node('small', '', `${date(run.started)} · ${run.rig.split('/').pop()}`),
    );
    // A movie run gets a play glyph; every other mode opens a display.
    const icon = node('span', 'history-icon', run.mode === 'movie' ? '▷' : '↗');
    button.append(icon, text, node('span', `status ${run.status}`, run.status.toUpperCase()));
    button.addEventListener('click', guard(async () => {
      runId = run.id;
      gallerySignature = '';
      renderHistory();
      await refreshRun();
    }));
    return button;
  });
  $('history').replaceChildren(...rows);
  if (!runs.length) {
    const hint = 'Your first run starts a history. Configurations, logs and media stay together.';
    $('history').append(node('p', 'history-empty', hint));
  }
}

/** Show one output panel — 'media', 'console' or 'monitor' — and mark its tab. */
function outputTab(tab) {
  for (const name of ['media', 'console', 'monitor']) {
    $(`${name}-panel`).hidden = tab !== name;
    $(`${name}-tab`).classList.toggle('selected', tab === name);
  }
}

/** Fill the Live monitor tab's note. Strings become spans so a <code> part
 *  (a YAML key the reader should copy) can sit between them. */
function setMonitorNote(...parts) {
  const nodes = parts.map((part) => (typeof part === 'string' ? node('span', '', part) : part));
  $('monitor-note').replaceChildren(...nodes);
}

/**
 * The Live monitor tab for `run`. The session's dashboard is a separate
 * loopback server that the session process starts and that dies with it,
 * and the runner prints its URL (which carries that server's own token)
 * to the console; the launcher relays it as `run.monitor`.
 *
 * While the run is active and that URL is known, the monitor page is framed
 * at it, loaded once — a reload on every poll would restart the page under
 * the reader — and the open-in-new-tab link points at it too. In every
 * other case the frame is emptied, so a browser error page for a server
 * that no longer exists never sits in the tab, and a note says what the
 * reader is looking at instead: the saved copy, a rig with the dashboard
 * off, or a session that has not opened its monitor yet.
 */
function renderMonitor(run, active) {
  const frame = $('monitor-frame');
  const note = $('monitor-note');
  const url = run && active ? run.monitor : null;
  $('monitor').hidden = !url;
  if (url) {
    $('monitor').href = url;
    if (framedMonitor !== url) {
      frame.src = url;
      framedMonitor = url;
    }
    frame.hidden = false;
    note.hidden = true;
    // Bring the tab up once, for the run the reader started from this page:
    // they are waiting for exactly this. A run picked from the history, or a
    // reader who has moved to another tab since, keeps the tab they chose.
    if (run.id === launchedRun && monitorShown !== run.id) {
      monitorShown = run.id;
      outputTab('monitor');
    }
    return;
  }
  if (framedMonitor) {
    frame.src = 'about:blank';
    framedMonitor = '';
  }
  frame.hidden = true;
  note.hidden = false;
  if (!run) {
    setMonitorNote('Start a run to watch its live monitor here.');
  } else if (!active) {
    setMonitorNote(
      'The live monitor closes with the session. Its final state was saved in the run’s '
      + 'data directory as ',
      node('code', '', 'figures/dashboard.html'),
      '.',
    );
  } else if (rigMonitor[`${run.project}:${run.rig}`] === false) {
    setMonitorNote(
      'This rig has ',
      node('code', '', 'dashboard.enabled: false'),
      '; set it to true in the rig YAML to watch the session here.',
    );
  } else {
    // The rig has it on, or this page has not read that rig's YAML.
    setMonitorNote('Waiting for the session to open its monitor…');
  }
}

/**
 * Fetch the selected run and draw it: status badge, summary line, stop
 * button, live-monitor link, console tail, the command that started it and
 * the media gallery. Called on every poll, so the parts the reader interacts
 * with (the console's scroll position, a playing video) are touched only
 * when their content changed.
 */
async function refreshRun() {
  const key = runId;
  const run = key ? await api('/api/runs/' + key) : null;
  // The reader picked another run while this one was loading.
  if (key !== runId) return;
  // "Active": its process is alive, so its monitor may be up and its movies
  // may still be being written.
  const active = run ? ['running', 'stopping'].includes(run.status) : false;
  $('run-status').textContent = run ? run.status.toUpperCase() : 'READY';
  $('run-status').className = 'status ' + (run?.status || '');
  let info = '';
  if (run) {
    info = `${label(run.mode)} · ${date(run.started)}`;
    if (run.returncode !== null) info += ' · exit ' + run.returncode;
    if (run.error) info += ' · ' + run.error;
  }
  $('run-info').textContent = info;
  // Only the active run can be stopped; while it stops, the button waits.
  $('stop').hidden = !run || run.id !== state.active;
  $('stop').disabled = run?.status === 'stopping';
  $('stop').textContent = run?.status === 'stopping' ? 'Stopping…' : 'Stop run';
  // The Live monitor tab and its open-in-new-tab link.
  renderMonitor(run, active);
  // Console: follow the tail only if the reader was already at the bottom,
  // so scrolling up to read an earlier line is not undone by the next poll.
  const consoleEl = $('console');
  const nearBottom = consoleEl.scrollHeight - consoleEl.scrollTop - consoleEl.clientHeight < 70;
  let log;
  if (run) log = run.log || run.error || 'Waiting for output…';
  else log = 'Output will appear when a run starts.';
  if (consoleEl.textContent !== log) {
    consoleEl.textContent = log;
    if (nearBottom) consoleEl.scrollTop = consoleEl.scrollHeight;
  }
  // The exact argument list, quoted only where a reader would need it to
  // paste the command into a shell.
  let command = '';
  if (run) {
    const quoted = run.command.map((s) => (/[\s"']/.test(s) ? JSON.stringify(s) : s));
    command = `${run.cwd}\n\n${quoted.join(' ')}\n\nSaved in ${run.directory}`;
  }
  $('command').textContent = command;
  const artifacts = run?.artifacts || [];
  $('media-count').textContent = artifacts.length;
  $('gallery-empty').hidden = artifacts.length > 0;
  // The empty state's wording follows the run's situation.
  const empty = $('gallery-empty');
  let heading;
  let detail;
  if (!run) {
    heading = 'A closer look at your experiment.';
    detail = 'Preview images and recorded movies appear here. '
      + 'Choose a mode and start a run to see the results.';
  } else if (active) {
    heading = 'Your experiment is working.';
    detail = 'Images appear as they are written. Movies are available when recording '
      + 'finishes. Follow progress in the console.';
  } else if (run.status === 'failed') {
    heading = 'This run needs attention.';
    detail = 'Open the console for the error and the command that produced it.';
  } else {
    heading = 'No images or movies in this run.';
    detail = 'Session modes write their data to the rig’s data directory. '
      + 'Use a preview script or movie mode to generate media.';
  }
  empty.querySelector('h3').textContent = heading;
  empty.querySelector('p').textContent = detail;
  // Do not replace a playing video on every poll. Defer videos until the
  // encoder closes them; an unfinished MP4 has no readable index yet.
  const visible = artifacts.filter((a) => !a.type.startsWith('video/') || !active);
  const signature = JSON.stringify([key, visible]);
  if (signature === gallerySignature) return;
  gallerySignature = signature;
  $('gallery-empty').hidden = visible.length > 0;
  const cards = visible.map((artifact) => {
    // Each path segment is encoded on its own, so '/' stays a separator and
    // a '#' or '?' inside a file name does not end the URL. The token rides
    // in the query because an <img> cannot send a header; `v` (the file's
    // mtime) defeats the cache when a file is rewritten under the same name.
    const segments = artifact.path.split('/').map(encodeURIComponent).join('/');
    const url = `/media/${key}/${segments}`
      + `?token=${encodeURIComponent(token)}&v=${artifact.modified}`;
    const video = artifact.type.startsWith('video/');
    const figure = node('figure', 'media-card');
    const media = node(video ? 'video' : 'img');
    media.src = url;
    if (video) {
      media.controls = true;
      // Metadata only — the duration and first frame — not the whole file.
      media.preload = 'metadata';
      media.playsInline = true;
    } else {
      media.alt = artifact.path;
      media.loading = 'lazy';
      // A click or Enter opens the image at full size in the image dialog.
      media.tabIndex = 0;
      const show = () => {
        $('large-image').src = url;
        $('large-image').alt = artifact.path;
        $('large-caption').textContent = artifact.path;
        $('image-dialog').showModal();
      };
      media.addEventListener('click', show);
      media.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') show();
      });
    }
    const caption = node('figcaption');
    const title = node('span', '', artifact.path);
    const size = artifact.size > 1048576
      ? `${(artifact.size / 1048576).toFixed(1)} MB`
      : `${Math.ceil(artifact.size / 1024)} KB`;
    title.append(node('small', 'media-size', size));
    const download = node('a', '', '↓ Save');
    download.href = url;
    download.download = artifact.path.split('/').pop();
    caption.append(title, download);
    figure.append(media, caption);
    return figure;
  });
  $('gallery').replaceChildren(...cards);
}

/* ------------------------------------------------------------------ */
/* Polling and the dialogs                                             */
/* ------------------------------------------------------------------ */

/**
 * One poll: fetch the state, clear an earlier connection error, and either
 * pick a project (the remembered one, else the first) when none is shown
 * yet, or redraw the current one and its run.
 */
async function refresh() {
  state = await api('/api/state');
  $('connection').textContent = 'Connected to localhost';
  if (connectionError) {
    error('');
    connectionError = false;
  }
  if (!project() && state.projects.length) {
    const remembered = localStorage.getItem('alhazen-workspace-project');
    const found = state.projects.find((p) => p.id === remembered);
    await chooseProject(found?.id || state.projects[0].id);
  } else {
    renderState();
    await refreshRun();
  }
}

/** The 1.5 s polling loop. A failed poll shows the error and keeps trying:
 *  the launcher may only be restarting. */
async function poll() {
  try {
    await refresh();
  } catch (e) {
    $('connection').textContent = 'Connection unavailable';
    connectionError = true;
    error(e.message);
  }
  setTimeout(poll, 1500);
}

/** Open the project dialog: for a new experiment, or (edit) the current
 *  one's settings. The folder is fixed once registered; the interpreter is
 *  what changes. */
function openProject(edit = false) {
  editProject = edit ? project() : null;
  $('dialog-title').textContent = edit ? 'Project settings' : 'Add an experiment';
  $('project-folder').value = editProject?.path || '';
  $('project-folder').readOnly = edit;
  $('project-python').value = editProject?.python || '';
  $('project-runtime').textContent = editProject
    ? `Current interpreter: ${editProject.python}`
    : '';
  $('remove-project').hidden = !edit;
  $('dialog-error').textContent = '';
  $('project-dialog').showModal();
}

/* ------------------------------------------------------------------ */
/* Wiring                                                              */
/* ------------------------------------------------------------------ */

$('add-project').addEventListener('click', () => openProject());
$('empty-add').addEventListener('click', () => openProject());
$('project-settings').addEventListener('click', () => openProject(true));
$('close-dialog').addEventListener('click', () => $('project-dialog').close());
$('close-image').addEventListener('click', () => $('image-dialog').close());
$('mode').addEventListener('change', modeChanged);
$('rig').addEventListener('change', guard(loadRig));
$('params-config').addEventListener('change', guard(loadConfig));
$('parameter-search').addEventListener('input', filterParameters);
$('fields-tab').addEventListener('click', guard(() => switchEditor('fields')));
$('yaml-tab').addEventListener('click', guard(() => switchEditor('yaml')));
$('media-tab').addEventListener('click', () => outputTab('media'));
$('console-tab').addEventListener('click', () => outputTab('console'));
$('stop').addEventListener('click', guard(async () => {
  await api('/api/stop', {id: runId});
  await refresh();
}));

/* Not guard(): a failure here belongs in the dialog, next to the fields. */
$('project-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    const p = await api('/api/projects', {
      path: $('project-folder').value,
      python: $('project-python').value,
    });
    state = await api('/api/state');
    $('project-dialog').close();
    error('');
    await chooseProject(p.id);
  } catch (e) {
    $('dialog-error').textContent = e.message;
  } finally {
    button.disabled = false;
  }
});

$('remove-project').addEventListener('click', guard(async () => {
  await api('/api/projects/remove', {id: selected});
  $('project-dialog').close();
  selected = null;
  runId = null;
  await refresh();
}));

$('launch-form').addEventListener('submit', guard(async (event) => {
  event.preventDefault();
  // The button is disabled in these states, but Enter in a field submits too.
  if (launching || (usesParameters() && (loadingConfig || loadingSchema)) || state.active) return;
  launching = true;
  updateLaunch();
  error('');
  const mode = $('mode').value;
  try {
    const isScript = project().scripts.some((s) => s.id === mode);
    const request = {
      project: selected,
      mode,
      rig: $('rig').value,
      subject: $('subject').value,
      session: Number($('session').value),
      seed: Number($('seed').value),
      trials: Number($('trials').value),
      // Options are sent only for the mode that shows them: a hidden checkbox
      // keeps its state across mode changes and must not leak into a launch.
      headless: mode === 'simulate' && $('headless').checked,
      mouse: mode === 'test' && $('mouse').checked,
      windowed: !['movie'].includes(mode) && !isScript && $('windowed').checked,
      scale: Number($('scale').value),
      sheet: $('sheet').checked,
      columns: $('columns').value ? Number($('columns').value) : null,
      clips: $('clips').value.split(',').map((s) => s.trim()).filter(Boolean),
      script_args: MODES[mode] ? '' : $('script-args').value,
    };
    // Parameters travel as the editor shows them: the raw text from the text
    // editor (the server parses and validates it) or the edited values from
    // the fields. Modes that take no parameters send neither.
    if (usesParameters() && editor === 'yaml' && $('parameter-yaml').value.trim()) {
      request.parameters_yaml = $('parameter-yaml').value;
    } else if (usesParameters() && editor === 'fields' && values !== null) {
      request.parameters = values;
    }
    const run = await api('/api/runs', request);
    runId = run.id;
    // Remembered so the Live monitor tab comes up on its own when this run's
    // monitor appears (renderMonitor); until then the media tab shows.
    launchedRun = run.id;
    gallerySignature = '';
    outputTab('media');
    await refresh();
  } finally {
    launching = false;
    updateLaunch();
  }
}));

poll();
