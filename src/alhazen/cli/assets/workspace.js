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
 * session's own live monitor — a separate loopback server the session starts —
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
 * project's discovered script (its preview.py, or alhazen's preview of the
 * stimuli it declares) joins the menu at runtime with the label the server
 * supplies; see chooseProject. */
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

/* The rule a subject's initials are held to, word for word as the command
 * line says it (alhazen.config.models.INITIALS_RULE; a Python test holds the
 * two together), so the page and a terminal refuse the same answer the same
 * way. The server checks again, with the same words, before a run starts. */
const INITIALS_RULE = 'initials must be 1 to 5 letters, such as HD';
const INITIALS_REQUIRED = 'Subject initials are required for run and test modes';

/* The modes whose session draws a new seed when it is given none, records
 * it, and prints it on its console, where the launcher reads it for the
 * history (workspace.py SEED_DRAWING_MODES). Demo and movie use seed 0 when
 * given none, the command line's own default; measure takes no seed. */
const DRAWS_SEED = ['run', 'test', 'simulate'];
/* What the seed field accepts, said when it holds anything else. */
const SEED_RULE = 'The random seed must be a whole number of 0 or more, or empty for a new one';

/* What installs PsychoPy for alhazen, word for word as the error a launch
 * without it prints (alhazen.display.psychopy_backend.PSYCHOPY_INSTALL; a
 * Python test holds the two together). The distribution is alhazen-vision:
 * a bare `alhazen` on PyPI is an unrelated project. */
const PSYCHOPY_INSTALL = 'pip install "alhazen-vision[psychopy]"';

/* The API token. The opening URL carries it in the fragment (#token=…), which
 * a browser never sends to a server, so it stays out of request logs; the
 * page keeps it for reloads and then takes it out of the address bar.
 * sessionStorage, not localStorage: the token should die with the tab, as
 * the server that issued it dies with the terminal. */
let token = new URLSearchParams(location.hash.slice(1)).get('token')
  || sessionStorage.getItem('alhazen-workspace-token')
  || '';
if (token) sessionStorage.setItem('alhazen-workspace-token', token);
/* The address keeps its query — which screen this is (route()) — and loses
 * only the fragment that carried the token. */
if (location.hash) history.replaceState(null, '', location.pathname + (location.search || ''));

/* The server's last /api/state answer: the registered projects, every run
 * (newest first) and the id of the one active run, or null. */
let state = {projects: [], runs: [], active: null};
/* Which project and which run the page is looking at. */
let selected = null;
let runId = null;
/* The parameter values being edited — null while a file is loading, and for
 * a project with no parameter file at all, whose task then runs on the
 * defaults in its code — and which editor shows them, 'fields' or 'yaml'. */
let values = null;
let editor = 'fields';
/* Which of the open experiment's pages is shown: 'general' (its notes,
 * people and rigs: #general-view), 'run' (the launch form, run output and
 * recent runs: #workspace), 'data' (#data-view, drawn by workspace_data.js)
 * or 'history' (#history-view). With no experiment open, the Experiments
 * page (#home-view). Each is an address (route()); the last page used for an
 * experiment is remembered in localStorage (VIEW_KEY) for a link that names
 * none. */
let view = 'run';
const VIEWS = {general: 'General', run: 'Run', data: 'Data', history: 'History'};
/* The sidebar's glyph for each page, drawn with the page's name. */
const VIEW_GLYPHS = {general: '◇', run: '▶', data: '▦', history: '↺'};
const VIEW_KEY = 'alhazen-workspace-view:';
/* The colour theme the reader chose: 'system' (follow the operating
 * system's light or dark setting; the default), 'light' or 'dark'. */
const THEMES = ['system', 'light', 'dark'];
const THEME_KEY = 'alhazen-workspace-theme';
/* The project open in the settings dialog, or null for "Add experiment". */
let editProject = null;
/* Epochs for the three loads a reader can re-trigger faster than they finish:
 * each load takes the next number and, after awaiting, writes its answer
 * only if no newer load has started. Without this a slow answer for the
 * previous rig, parameter file or task would land on top of the current one. */
let configEpoch = 0;
let rigEpoch = 0;
let schemaEpoch = 0;
/* The open experiment's people (GET /api/manage/people): its subjects,
 * its experimenters and the CSV copies' state; null until loaded. The Run
 * page's Subject and Experimenter menus are drawn from it, and each
 * experiment's choice in them is kept apart (identityChoice), so opening
 * another experiment never carries a subject over. */
let people = null;
let peopleEpoch = 0;
const identityChoice = {};
/* Whether the first poll has placed the page on the address's screen. */
let routed = false;
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
/* The selected task's JSON schema, read once per project (and again when
 * the reader picks another of its tasks) for the parameter dropdowns; {}
 * until it arrives or when it could not be read. */
let parameterSchema = {};
/* The task parameterSchema was asked for (null: the project's one task), so
 * choosing another Task parameters entry for the same task reloads only its
 * file. */
let schemaTask = null;
/* Whether each rig YAML the page has read turns the live monitor on, keyed
 * "<project id>:<rig path>" (two projects may both have a configs/rig.yaml).
 * The Live monitor tab reads it to say why an active run shows no monitor. */
const rigMonitor = {};
/* Each rig's display backend, as its merged YAML says it (keyed like
 * rigMonitor): 'psychopy' or 'simulated'. A rig not read yet has no entry,
 * which psychopyNeeded treats as "cannot tell yet", not as either answer. */
const rigBackend = {};
/* The rigs that say `real_data: false` — development rigs, which run mode
 * refuses before anything is written (docs/rigs.md §5) — keyed like
 * rigMonitor, holding the name the summary shows; null for a rig that
 * collects. A rig not read yet has no entry, and the footer then says
 * nothing about it: the server makes the same check at launch, so the page
 * only warns ahead of it and never blocks. */
const rigDevelopment = {};
/* The run most recently started from this page, and the run whose monitor
 * tab has already been brought up on its own: the tab is switched once, for
 * the reader who is waiting on the run they launched, and never again. */
/* The Rig section's calibration-target choice (renderCalibration): the
 * rig's own setting, the page's choice over it, the project's offer (its
 * pictures and the setting's defaults) and the monitor, for the preview's
 * size. Null while the selected rig has no tracker that draws a target. */
let calibration = null;
/* The preview's animation frame, while one is scheduled. */
let calibrationFrame = null;
let launchedRun = null;
let monitorShown = null;
/* Measure rig's checklist (workspace_measure.js): its state, the line saying
 * what a tick also changed, and the selected rig's merged settings for the
 * module's hints. */
let measureState = null;
let measureNote = '';
let measureRig = null;
const MEASURE_KEY = 'alhazen-workspace-measurements:';
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
  history.replaceState(null, '', location.pathname + (location.search || ''));
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

/** A run's heading for the history and the summary line: its mode, then the
 *  Task parameters entry it was launched from — "Simulate · Main (less
 *  trials)" — or, for a run recorded before entries had labels, the task it
 *  ran when the experiment ships several — "Simulate · mt-tuning". */
function title(run) {
  const what = run.parameter_set || run.task;
  return what ? `${label(run.mode)} · ${what}` : label(run.mode);
}

/** The tasks a project's run.py declares (`run_experiment(tasks=TASKS)`),
 *  as {name, params}. Empty for a run.py with one task the old way, when
 *  the table could not be read (see `tasks_error`), and for a project a
 *  server from before task tables lists without the field at all. */
function tasks(p) {
  return p?.tasks || [];
}

/** Labels in the order a person looks for them: alphabetical, ignoring
 *  case, with numbers in number order ("Block 2" before "Block 10"). Every
 *  menu on the form is sorted this way, at the owner's request (2026-10-06). */
function byName(a, b) {
  return a.localeCompare(b, undefined, {numeric: true, sensitivity: 'base'});
}

/** The project's Task parameters entries, {label, task, params}, sorted by
 *  label. Each one is a parameter file (or none: the task's own defaults)
 *  AND the task it runs — run.py's PARAMETERS, or the pairs the server
 *  derives from TASKS and configs/ when run.py names none
 *  (workspace.project_parameter_sets). */
function parameterSets(p) {
  return [...(p?.parameter_sets || [])].sort((a, b) => byName(a.label, b.label));
}

/** The entry the Task parameters menu shows, or undefined (no entries). */
function selectedSet() {
  return parameterSets(project()).find((s) => s.label === $('params-config').value);
}

/** The task a launch or a schema request names: the one the chosen Task
 *  parameters entry runs, for a project with a task table; null otherwise.
 *  Null, not '', so a project without tasks never sends a task the server
 *  would refuse. There is no Task menu: the parameter entry says the task,
 *  so the two cannot be chosen apart (the owner's request, 2026-10-06). */
function selectedTask() {
  if (!tasks(project()).length) return null;
  return selectedSet()?.task ?? null;
}

/** The parameter file the chosen entry starts from, '' for none. */
function selectedParams() {
  return selectedSet()?.params || '';
}

/**
 * Fill the Task parameters menu for project `p`, sorted by name, and open it
 * on `previous` when that is still an entry, else on the server's
 * `default_parameter_set` (the default task's own file). Under the menu,
 * which task the entry runs — or why run.py's task table or PARAMETERS
 * could not be read. A project with no entry at all has no menu, and the
 * editor says what runs instead (renderEditor).
 */
function presetMenu(p, previous = null) {
  const sets = parameterSets(p);
  const labels = sets.map((s) => s.label);
  const start = labels.includes(previous) ? previous : p.default_parameter_set;
  options($('params-config'), sets.map((s) => [s.label, s.label]), start);
  $('params-config').hidden = !sets.length;
  describeSet(p);
}

/** The sentence under the Task parameters menu: which task the chosen entry
 *  runs (when the experiment declares several) and with which file, and
 *  any problem reading run.py's PARAMETERS, said rather than hidden. */
function describeSet(p) {
  const set = selectedSet();
  const parts = [];
  if (p?.tasks_error) parts.push(p.tasks_error);
  else if (set?.task) {
    parts.push(`Runs the task ${set.task}`
      + (set.params ? ` with ${set.params}.` : ' on the defaults in its code.'));
  }
  if (p?.parameter_sets_error) {
    parts.push('run.py’s PARAMETERS could not be read, so the menu pairs each task with each '
      + `file instead: ${p.parameter_sets_error}`);
  }
  $('parameter-set-help').textContent = parts.join(' ');
  $('parameter-set-help').hidden = !parts.length;
}

/** What the selected experiment is called on the page: the title its
 *  pyproject declares, else its slug; a server from before titles sends
 *  neither, and the registered folder name stands in. */
function titleOf(p) {
  return p?.title || p?.name || '';
}

/** The experiment's short name, the owner part of its rigs' names
 *  (amodal-averaging/lab); the folder name from a server before slugs. */
function slugOf(p) {
  return p?.slug || p?.name || '';
}

/** Who a run was for, "sub-01 · HD", from its record; '' for a run that
 *  names no subject (a script, a movie, a simulation left to name its own,
 *  or a run recorded before the workspace kept it). */
function who(run) {
  if (!run.subject) return '';
  return run.initials ? `sub-${run.subject} · ${run.initials}` : `sub-${run.subject}`;
}

/** The seed a run's session used, for the history and the summary line:
 *  "seed 2718281828" when the record knows it — the one the launch passed,
 *  or the one the session drew and printed, which the launcher reads from
 *  its console — else "seed new" for a session that draws its own and has
 *  not said which yet (or never will: an alhazen from before the console
 *  line). '' for a launch with no seed to show: measure, a script, and demo
 *  or movie left on their default. A record from before the launcher kept
 *  the seed has it filled in from its command (workspace.py seed_argument). */
function seedText(run) {
  if (run.seed !== null && run.seed !== undefined) return `seed ${run.seed}`;
  return DRAWS_SEED.includes(run.mode) ? 'seed new' : '';
}

/**
 * The seed field as a launch sends it: `value` null when the field is empty
 * — the session then draws its own — or the whole number typed. `problem`
 * says why anything else cannot be sent, in words the reader can act on,
 * rather than letting it turn into "empty" and a new seed nobody asked for.
 * A browser hands an unreadable number field over as '' and flags it
 * (`validity.badInput`), so that flag is read as well as the text; a number
 * past 2^53 would not survive the trip through JSON and is refused too.
 */
function readSeed(field) {
  const text = field.value.trim();
  if (field.validity?.badInput) return {value: null, problem: SEED_RULE};
  if (!text) return {value: null, problem: ''};
  const value = Number(text);
  if (!/^[0-9]+$/.test(text) || !Number.isSafeInteger(value)) {
    return {value: null, problem: `${SEED_RULE}; got '${field.value}'`};
  }
  return {value: value, problem: ''};
}

/**
 * The initials as they are sent — trimmed and uppercase, as run.py records
 * them — or, as `problem`, why they cannot be: missing where the mode names a
 * real subject (`required`), or not 1 to 5 letters. Letters in any script
 * count, as they do on the command line (Python's str.isalpha).
 */
function checkInitials(text, required) {
  const value = text.trim().toUpperCase();
  if (!value) return {value: '', problem: required ? INITIALS_REQUIRED : ''};
  if (!/^\p{L}{1,5}$/u.test(value)) return {value, problem: `${INITIALS_RULE}; got '${text}'`};
  return {value, problem: ''};
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

/** Refill a <select> from groups of [value, text] pairs, each group under an
 *  <optgroup> with its heading as the label; an empty group is left out.
 *  Keeps the reader's previous choice when it is still on offer, else opens
 *  on the first option, as a browser does. */
function groupedOptions(select, groups, previous) {
  const nodes = [];
  const values = [];
  for (const [heading, items] of groups) {
    if (!items.length) continue;
    const group = node('optgroup');
    group.label = heading;
    for (const [value, text] of items) {
      const option = node('option', '', text);
      option.value = value;
      group.append(option);
      values.push(value);
    }
    nodes.push(group);
  }
  select.replaceChildren(...nodes);
  select.value = values.includes(previous) ? previous : (values[0] ?? '');
}

/* ------------------------------------------------------------------ */
/* Rigs                                                                */
/* ------------------------------------------------------------------ */

/* A project's `rigs` are {name, source, path, shadowed, extends}: its own
 * rigs (source 'experiment', path relative to the project) and then the
 * shared rigs its alhazen ships (source 'alhazen'). A rig's name is its file
 * name without rig- and .yaml — `lab` for configs/rig-lab.yaml. The page
 * shows every rig with its owner in front, <experiment slug>/<name> or
 * alhazen/<name>, which --rig takes as well as the bare name (docs/rigs.md). */

/** What the Rig menu sends for rig `r`: an experiment rig's project-relative
 *  path, or alhazen/<name> for a shared rig — the spelling that names the
 *  shared one even when the experiment has a rig of the same name. */
function rigValue(r) {
  return r.source === 'alhazen' ? `alhazen/${r.name}` : r.path;
}

/** Rig `name`'s qualified name: `alhazen/<name>` for a shared rig
 *  (`source` 'alhazen'), `<slug>/<name>` for one of experiment `slug`'s own
 *  — the one spelling of a rig the menu, its summary and the history share,
 *  and that the command line takes (alhazen.config.rigs.resolve_rig). */
function rigName(name, source, slug) {
  return `${source === 'alhazen' ? 'alhazen' : slug}/${name}`;
}

/** A rig's text in the menu: its qualified name, never its file, and
 *  nothing more, so it fits the closed menu (the shared rig it extends is in
 *  the summary under it, loadRig). One whose name two of the experiment's
 *  files share (`duplicates`) carries its file, since its name alone would
 *  not say which it is. */
function rigLabel(r, duplicates, slug) {
  const text = rigName(r.name, r.source, slug);
  return r.source === 'experiment' && duplicates.has(r.name) ? `${text} (${r.path})` : text;
}

/** Fill the Rig menu for project `p`: the experiment's own rigs, then the
 *  shared ones, as two groups, each sorted by name. A shared rig the experiment's own rig of the
 *  same name hides (`shadowed`) is left out: the experiment's is the one that
 *  name means, and the command line still reaches the shared one as
 *  alhazen/<name>. The menu opens on the laptop, at the owner's request
 *  (2026-10-02): the laptop rig is the development machine, where an
 *  experiment is written and tried (docs/rigs.md), and it has a window and
 *  no devices, so a launch nobody re-pointed cannot reach for hardware that
 *  is not there. (It used to open on the mac, the same kind of machine for
 *  someone working on a Mac, who now chooses it from the menu.)
 *  The experiment's own laptop when it has one — it shadows the shared
 *  laptop, which the menu then leaves out — else alhazen's shared laptop,
 *  else the first rig listed. */
function rigMenu(p) {
  const slug = slugOf(p);
  const rigs = p.rigs || [];
  const own = rigs.filter((r) => r.source === 'experiment');
  const shared = rigs.filter((r) => r.source === 'alhazen' && !r.shadowed);
  const seen = new Set();
  const duplicates = new Set();
  for (const r of own) {
    if (seen.has(r.name)) duplicates.add(r.name);
    seen.add(r.name);
  }
  // The experiment's own laptop is looked for first: the shared one of that
  // name is shadowed by it, so it is not in `shared` to be found anyway.
  const laptop = own.find((r) => r.name === 'laptop') || shared.find((r) => r.name === 'laptop');
  const first = laptop || own[0] || shared[0];
  // Each group sorted by name (byName), at the owner's request (2026-10-06).
  const items = (list) => list.map((r) => [rigValue(r), rigLabel(r, duplicates, slug)])
    .sort((a, b) => byName(a[1], b[1]));
  groupedOptions($('rig'), [
    ['This experiment', items(own)],
    ['Shared (alhazen)', items(shared)],
  ], first ? rigValue(first) : '');
  // Said under the menu when the registration predates shared rigs, so a
  // menu without them is explained rather than mistaken for "there are none".
  $('rig-note').textContent = p.rigs_note || '';
  $('rig-note').hidden = !p.rigs_note;
}

/**
 * Show a rig's facts under the Rig menu as a small list of labels and
 * values: [[label, [part, part, …]], …]. Each part is kept whole (it never
 * breaks inside, "30.4 cm wide"), and a line may break only between parts,
 * at their " · " separators.
 */
function rigFacts(facts) {
  const list = node('dl', 'rig-facts');
  for (const [label, parts] of facts) {
    const value = node('dd');
    parts.forEach((part, index) => {
      if (index) value.append(node('span', 'fact-separator', ' · '));
      value.append(node('span', 'fact', part));
    });
    list.append(node('dt', '', label), value);
  }
  $('rig-summary').replaceChildren(list);
}

/** A run's rig as the history shows it: its qualified name, not its file —
 *  alhazen/<name> for a shared rig, <slug>/<name> for one of experiment
 *  `slug`'s own. A run recorded before runs kept the name gets it back from
 *  its file name (rig-lab.yaml is lab), and is the experiment's unless it
 *  was launched as alhazen/<name>. */
function runRig(run, slug) {
  if (run.rig_name) return rigName(run.rig_name, run.rig_source, slug);
  const name = run.rig.split('/').pop().replace(/\.ya?ml$/, '').replace(/^rig-/, '');
  return rigName(name, run.rig.startsWith('alhazen/') ? 'alhazen' : 'experiment', slug);
}

/** Whether the selected mode takes task parameters (see ParameterChoices). */
function usesParameters() {
  return ParameterChoices.usesParameters($('mode').value, project()?.scripts || []);
}

/* ------------------------------------------------------------------ */
/* The launch form                                                     */
/* ------------------------------------------------------------------ */

/**
 * Will this launch open a PsychoPy window? true, false, or null when that
 * cannot be told yet (the rig has not been read).
 *
 * The rule, read from alhazen's modes (it is inferred there, so the page
 * only warns on it and never blocks a launch):
 *   demo, measure       always: both open alhazen's PsychoPy display
 *                       whatever the rig's display backend says
 *                       (modes/demo.py run_demo, modes/measure.py).
 *   test, run           when the rig's display backend is psychopy (the
 *                       model's default when the YAML does not say).
 *   simulate            the same, unless headless, which swaps the display
 *                       for the simulated one (modes/session.py rig_for_mode).
 *   movie, scripts      never known to: movie renders off screen, and a
 *                       script's needs are its own.
 */
function psychopyNeeded(mode, backend, headless) {
  if (mode === 'demo' || mode === 'measure') return true;
  if (!['test', 'run', 'simulate'].includes(mode)) return false;
  if (mode === 'simulate' && headless) return false;
  if (backend === undefined) return null;
  return backend === 'psychopy';
}

/**
 * The launch footer's warning when the selected launch needs PsychoPy and
 * the project's interpreter may not have it, else null.
 *
 * `psychopy_version` is what the interpreter probe found when the project
 * was registered (workspace.py INTERPRETER_PROBE): a version string, null
 * for "not importable there", and absent for a registration made before the
 * probe asked, which says nothing either way — so that case asks for a
 * re-registration rather than claiming PsychoPy is missing.
 */
function psychopyWarning(p, mode, backend, headless) {
  if (!p || psychopyNeeded(mode, backend, headless) !== true) return null;
  const opens = `${label(mode)} opens a PsychoPy window`;
  if (!('psychopy_version' in p)) {
    return `${opens}. Whether this project’s interpreter (${p.python}) has PsychoPy is `
      + 'unknown: it was registered before the dashboard checked. Re-register to check: '
      + 'Project settings → Save.';
  }
  if (p.psychopy_version !== null) return null;
  return `${opens}, and this project’s interpreter (${p.python}) has no PsychoPy, so the `
    + `run will stop with an error. Install it there with ${PSYCHOPY_INSTALL}, or choose an `
    + 'interpreter that has it in Project settings.';
}

/**
 * The launch footer's warning when a Run launch is about to be refused
 * because the selected rig is a development rig (`real_data: false`), else
 * null. `name` is the rig as the summary names it, or null/undefined for a
 * rig that collects or has not been read. The server refuses the launch
 * itself, in the session's own words and naming the rigs that do collect;
 * this says so before the click.
 */
function developmentRigWarning(mode, name) {
  if (mode !== 'run' || !name) return null;
  return `${label('run')} records real data, and ${name} is a development rig `
    + '(real_data: false): this launch will be refused before anything is written. Choose '
    + `a rig that collects real data, or ${label('test')} or ${label('simulate')} to try `
    + 'the session on this machine.';
}

/**
 * The launch button's state and the note under it. Disabled while a run is
 * active (one job at a time), while a launch is in flight, while parameters
 * are still loading (a launch then would silently use defaults for the
 * rest), when the project has no rig or its interpreter is missing, and
 * when run.py's task table could not be read: the server would refuse the
 * launch with the same message, so the page says so first.
 */
/**
 * The launch in one line, for the footer: the mode, the Task parameters
 * entry (when the mode takes parameters), the rig by its menu name, and the
 * subject and session (when the mode names one) — "Simulate · Main ·
 * alhazen/laptop · sub-s01 · ses 1". What the button will start, read before
 * pressing it; '' with no project.
 */
function launchSummary() {
  const p = project();
  if (!p) return '';
  const mode = $('mode').value;
  const parts = [label(mode)];
  if (usesParameters() && selectedSet()) parts.push(selectedSet().label);
  const rig = [...$('rig').querySelectorAll('option')]
    .find((o) => o.value === $('rig').value)?.textContent;
  if (rig) parts.push(rig);
  if (['run', 'test', 'simulate'].includes(mode)) {
    const subject = chosenSubject()?.code || $('subject').value.trim();
    if (subject) parts.push(`sub-${subject}`);
    parts.push(`ses ${$('session').value || 1}`);
  }
  if (['run', 'test', 'simulate', 'measure'].includes(mode) && chosenExperimenter()) {
    parts.push(`by ${chosenExperimenter().name}`);
  }
  return parts.join(' · ');
}

/* ------------------------------------------------------------------ */
/* Who: the Run page's Subject and Experimenter menus                  */
/* ------------------------------------------------------------------ */

/** The registered subject chosen in the Subject menu, or null. */
function chosenSubject() {
  const id = $('subject-record').value;
  return id ? (people?.subjects || []).find((s) => s.id === id) || null : null;
}

/** The experimenter chosen in the Experimenter menu, or null. */
function chosenExperimenter() {
  const id = $('experimenter').value;
  return id ? (people?.experimenters || []).find((e) => e.id === id) || null : null;
}

/** "sub-007 · HD", or "sub-007 · no initials" for a record without them. */
function subjectLabel(s) {
  return `sub-${s.code} · ${s.initials || 'no initials'}`;
}

/** An experimenter as the menus name them: their name, their initials when
 *  recorded, and the end of their id when two share a name, so two records
 *  are never shown as one person. */
function experimenterLabel(e, among) {
  const twin = among.some((o) => o.id !== e.id && o.name === e.name);
  let text = e.initials ? `${e.name} (${e.initials})` : e.name;
  if (twin) text += ` · ${e.id.slice(-4)}`;
  return text;
}

/** What Measure rig's ticked measurements need of the subject: 'required',
 *  'optional' or 'none' (MeasureChoice.subjectNeed); 'none' in other modes. */
function measureNeed() {
  if ($('mode').value !== 'measure' || !usesMeasurements()) return 'none';
  return MeasureChoice.subjectNeed(measureState);
}

/** Why a launch cannot go yet for want of who, or ''. A run or test subject
 *  chosen from the register needs the experimenter who runs it (the server
 *  asks the same); with none chosen, the typed fields must be open. A
 *  measurement of the subject in the chair needs the subject. */
function identityProblem() {
  const mode = $('mode').value;
  if (measureNeed() === 'required' && !chosenSubject() && !$('subject').value.trim()) {
    return 'A ticked measurement is of the subject in the chair: choose the subject.';
  }
  if (!['run', 'test'].includes(mode) || people?.error) return '';
  if (chosenSubject() && !chosenExperimenter()) {
    return 'Choose the experimenter who runs this session.';
  }
  if (!chosenSubject() && !$('typed-identity').open) {
    return 'Choose the subject for this session, or type an unregistered one.';
  }
  return '';
}

/** Fill one menu, opening on `chosen` when it is still on offer and on its
 *  prompt otherwise — never on a value left from another experiment. */
function fillMenu(select, items, chosen) {
  options(select, items, chosen || '');
  if (!items.some(([value]) => value === chosen)) select.value = '';
}

/** Fill the Subject and Experimenter menus from the open experiment's
 *  people: its active subjects in their registry order, and the active
 *  experimenters assigned to it, keeping this experiment's own earlier
 *  choice. With no subject registered there is nothing to choose, and the
 *  typed fields open. */
function renderIdentity() {
  const p = project();
  if (!p) return;
  const choice = identityChoice[p.id] || {};
  const subjects = (people?.subjects || []).filter((s) => s.status === 'active');
  fillMenu($('subject-record'), [
    ['', subjects.length ? 'Choose a subject…' : 'No subjects registered yet'],
    ...subjects.map((s) => [s.id, subjectLabel(s)]),
  ], choice.subject);
  const assigned = (people?.assigned || [])
    .filter((e) => e.status === 'active' && e.assignment_status === 'active');
  fillMenu($('experimenter'), [
    ['', assigned.length ? 'Choose who runs it…' : 'No experimenters assigned yet'],
    ...assigned.map((e) => [e.id, experimenterLabel(e, assigned)]),
  ], choice.experimenter);
  if (!subjects.length) $('typed-identity').open = true;
  identityChanged();
}

/** After a change of who: remember it for this experiment, lock the typed
 *  fields while a record is chosen, set what is required, and say what the
 *  session will record. */
function identityChanged() {
  const p = project();
  if (!p) return;
  const mode = $('mode').value;
  const record = chosenSubject();
  const person = chosenExperimenter();
  // Kept only once this experiment's people are loaded: before that the
  // menus are empty, and an empty choice must not replace its real one.
  if (people) {
    identityChoice[p.id] = {subject: record?.id || '', experimenter: person?.id || ''};
    sessionStorage.setItem('alhazen-workspace-identity', JSON.stringify(identityChoice));
  }
  if (record) $('typed-identity').open = false;
  $('subject').disabled = !!record;
  $('initials').disabled = !!record;
  const named = ['run', 'test'].includes(mode);
  const typedOpen = !!$('typed-identity').open;
  $('subject').required = (named || measureNeed() === 'required') && !record && typedOpen;
  $('initials').required = named && !record && typedOpen;
  $('experimenter').required = named && !!record;
  let help = '';
  if (people?.error) {
    help = `${people.error}. Type the subject below; no experimenter can be recorded.`;
  } else if (person && p.records_experimenter === false) {
    help = `${titleOf(p)}’s alhazen (${p.alhazen_version}) does not record the experimenter in `
      + 'its session folders: the workspace keeps it with the launch only. Update its alhazen '
      + 'to record it in session.json.';
  } else if (person && p.records_experimenter == null) {
    help = 'Whether this experiment’s alhazen records the experimenter is not known: open '
      + 'Project settings and save to ask it. Until then it is kept with the launch only.';
  } else if (!(people?.subjects || []).length) {
    help = 'Register subjects and experimenters on the General page to choose them here.';
  }
  $('identity-help').textContent = help;
  updateLaunch();
}

/** Load the open experiment's people for the Run page's menus. A registry
 *  that cannot be read leaves the typed fields to use, and says why. */
async function loadPeople(id) {
  const epoch = ++peopleEpoch;
  let answer;
  try {
    answer = await api(`/api/manage/people?project=${encodeURIComponent(id)}`);
  } catch (e) {
    answer = {error: e.message, subjects: [], experimenters: [], assigned: []};
  }
  if (epoch !== peopleEpoch || selected !== id) return;
  people = answer;
  renderIdentity();
}

/** The General page changed the people: redraw the menus from its answer. */
function peopleChanged(answer) {
  people = answer;
  renderIdentity();
}

function updateLaunch() {
  $('launch-summary').textContent = launchSummary();
  const p = project();
  const waitingForParameters = usesParameters() && (loadingConfig || loadingSchema);
  $('launch').disabled = !!state.active
    || !p?.rigs.length
    || launching
    || waitingForParameters
    || !p?.available
    || !!p?.tasks_error
    || !!calibrationProblem()
    || !!identityProblem()
    || (usesMeasurements() && !!MeasureChoice.problem(measureState));
  if (launching) $('launch').textContent = 'Starting…';
  else if (state.active) $('launch').textContent = 'A run is in progress';
  else $('launch').textContent = `▶ ${label($('mode').value) || 'Start run'}`;
  const mode = $('mode').value;
  const backend = p ? rigBackend[`${p.id}:${$('rig').value}`] : undefined;
  const headless = mode === 'simulate' && $('headless').checked;
  const psychopy = psychopyWarning(p, mode, backend, headless);
  const development = developmentRigWarning(
    mode, p ? rigDevelopment[`${p.id}:${$('rig').value}`] : null,
  );
  let note;
  if (p?.tasks_error) {
    // Before the active-run note: this one asks the reader to fix run.py,
    // which a finished run does not change.
    note = 'Nothing can be launched until run.py’s task table (TASKS) can be read: '
      + p.tasks_error;
  } else if (state.active) {
    note = 'One run at a time keeps the rig available to its active experiment.';
  } else if (calibrationProblem()) {
    note = calibrationProblem();
  } else if (identityProblem()) {
    note = identityProblem();
  } else if (usesMeasurements() && MeasureChoice.problem(measureState)) {
    note = MeasureChoice.problem(measureState);
  } else if (development) {
    // Before the PsychoPy warning: a launch refused for its rig never opens
    // a window, so the rig is the thing to change first.
    note = development;
  } else if (psychopy) {
    // Before the real-data reminder: a run that cannot open its window
    // records nothing, so the missing PsychoPy is the thing to fix first.
    note = psychopy;
  } else if (mode === 'run') {
    note = 'This mode records real subject data. Check the rig and subject ID before starting.';
  } else {
    note = 'Runs locally in your experiment’s Python environment.';
  }
  $('launch-note').textContent = note;
  // Styled as a warning (workspace.css .launch-warning) only while the note
  // is the development-rig or the PsychoPy one; every other note is plain
  // help text.
  $('launch-note').classList.toggle(
    'launch-warning',
    note === development || note === psychopy || note === calibrationProblem()
      || note === identityProblem(),
  );
}

/** Whether this launch picks its measurements from a list: Measure rig, on
 *  a project whose alhazen lists them (an older one runs its fixed list). */
function usesMeasurements() {
  return $('mode').value === 'measure' && Array.isArray(project()?.measurements)
    && !!window.MeasureChoice;
}

/** Draw the measurement list for the selected project, remembering the
 *  ticks per project (localStorage) so a reload keeps them. */
function renderMeasurements() {
  const p = project();
  if (!p || !usesMeasurements()) return;
  if (!measureState || measureState.project !== p.id) {
    // Kept as the keys joined by commas (a key never holds one); keys the
    // project no longer lists are dropped by create().
    const saved = (localStorage.getItem(MEASURE_KEY + p.id) || '').split(',').filter(Boolean);
    measureState = {project: p.id, ...MeasureChoice.create(p.measurements, saved)};
  }
  const help = p.measurements_error
    ? `A measurement provider failed to load, so its measurements are missing: ${p.measurements_error}`
    : '';
  $('measurements-help').classList.toggle('launch-warning', !!help);
  if (help) $('measurements-help').textContent = help;
  MeasureChoice.renderChecklist($('measurement-list'), measureState, {
    rig: measureRig,
    note: measureNote,
    onToggle: (key, on) => {
      const {state: next, note} = MeasureChoice.toggle(measureState, key, on);
      measureState = {project: p.id, ...next};
      measureNote = note;
      localStorage.setItem(MEASURE_KEY + p.id, MeasureChoice.selection(measureState).join(','));
      modeChanged();
    },
  });
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
  // parameter-file flag. Disabling the fieldset takes its inputs — the file
  // menu in its heading among them — out of form validation as well as out
  // of view; the menu is also disabled by name, so no change of markup can
  // leave it choosable for a launch that sends no parameters.
  $('params-config').disabled = !usesParameters();
  $('task-parameters').hidden = !usesParameters();
  $('task-parameters').disabled = !usesParameters();
  // Measure rig's measurements, in Task parameters' place.
  $('measurements').hidden = !usesMeasurements();
  $('measurements').disabled = !usesMeasurements();
  renderMeasurements();
  // Measuring the rig draws nothing random, so it takes no seed; nor does an
  // experiment's own script, which the launcher passes no seed to — a field
  // shown for either would promise something the launch does not do.
  const seedless = mode === 'measure' || !!script;
  $('seed-fields').hidden = seedless;
  $('seed').disabled = seedless;
  // What an empty seed field means depends on the mode, and the field says
  // so: a session draws a new seed each run and records it; demo and movie
  // use seed 0, as they do on the command line.
  if (DRAWS_SEED.includes(mode)) {
    $('seed').placeholder = 'new each run';
    $('seed-help').textContent = 'Leave the seed empty to draw a new one for every run: the '
      + 'session records the seed it drew (session.log, config_snapshot.yaml) and the history '
      + 'shows it. Type a seed only to repeat a session.';
  } else {
    $('seed').placeholder = '0';
    $('seed-help').textContent = 'Leave the seed empty for seed 0, as on the command line; '
      + 'type another for a different random draw of the stimulus.';
  }
  // Subject and session name the recorded data. A real or rehearsal session
  // must name its subject; a simulation may fall back to its own default.
  // Measure rig asks for a subject ID only when a ticked measurement is of
  // the subject in the chair (MeasureChoice.subjectNeed), and never for
  // initials or a session.
  const measureSubject = usesMeasurements() ? MeasureChoice.subjectNeed(measureState) : 'none';
  $('identity').hidden = !['run', 'test', 'simulate'].includes(mode) && measureSubject === 'none';
  for (const id of ['initials', 'session']) {
    const field = $(id).parentElement;
    if (field) field.hidden = mode === 'measure';
  }
  // Who runs it: for every session, and for Measure rig (who measured).
  $('experimenter-field').hidden = !['run', 'test', 'simulate', 'measure'].includes(mode);
  // What is required follows the mode and the choice (identityChanged): a
  // typed subject and its initials for run and test, a subject ID for a
  // measurement of the subject in the chair, an experimenter with a
  // registered subject. The initials are checked again on submit, with the
  // command line's words, by checkInitials.
  identityChanged();
  $('trials-field').hidden = !['test', 'simulate'].includes(mode);
  // Each option is shown for exactly the modes whose CLI accepts the flag.
  $('headless-field').hidden = mode !== 'simulate';
  $('mouse-field').hidden = mode !== 'test';
  $('windowed-field').hidden = !['test', 'run', 'demo', 'measure', 'simulate'].includes(mode);
  $('movie-options').hidden = mode !== 'movie';
  // The calibration target applies to run and test only (renderCalibration).
  if (calibration) renderCalibration();
  // Extra arguments are offered for every mode and script; only the words
  // change. A script's help lists the flags it declares, less the ones the
  // launcher sets itself, which are not offered for retyping. A mode's says
  // what the field is for: the runner's flags are the form's already, so
  // what goes here is a runner flag the form has no control for
  // (--curriculum) or, for an experiment that ships several tasks without
  // declaring a task table, its own --task; one with a table picks the task
  // through its Task parameters entry instead, so --task is not suggested. The placeholder
  // follows the help so it never shows a flag the help does not offer. The
  // server refuses a flag the form owns, by name (workspace.py).
  $('extra-label').textContent = script ? 'Extra script arguments' : 'Extra run.py arguments';
  if (script) {
    const managed = ['--out', '--rig', '--params', '--task-config', '--project'];
    const offered = script.flags.filter((f) => !managed.includes(f));
    $('extra-help').textContent = `Available flags: ${offered.join(', ') || 'none'}`;
    $('extra-args').placeholder = offered.length ? `e.g. ${offered[0]}` : '';
  } else if (tasks(project()).length) {
    $('extra-help').textContent = 'Passed to run.py after the launcher’s own flags: runner '
      + 'flags the form has no control for, e.g. --curriculum configs/shaping.yaml. '
      + 'The task is the one the Task parameters entry runs.';
    $('extra-args').placeholder = 'e.g. --curriculum configs/shaping.yaml';
  } else {
    $('extra-help').textContent = 'Passed to run.py after the launcher’s own flags — e.g. '
      + '--task mib-detect for an experiment that ships several tasks, or '
      + '--curriculum configs/shaping.yaml.';
    $('extra-args').placeholder = 'e.g. --task mib-detect';
  }
  updateLaunch();
}

/**
 * Switch the workspace to project `id`, on the view last used for it: name
 * it in the heading, fill the mode, task, rig and parameter-file menus, show
 * its most recent run, then load its schema, parameter file and rig in
 * parallel. Remembered in localStorage so a reload lands on the same one.
 */
async function chooseProject(id, nextView = null) {
  if (selected !== id) {
    // The people belong to the experiment being left: its menus go with
    // them until this one's are loaded (loadPeople).
    people = null;
    peopleEpoch += 1;
    fillMenu($('subject-record'), [['', 'Loading…']], '');
    fillMenu($('experimenter'), [['', 'Loading…']], '');
  }
  selected = id;
  localStorage.setItem('alhazen-workspace-project', id || '');
  // Every list belongs to the previous project: force each to redraw.
  projectSignature = '';
  historySignature = '';
  gallerySignature = '';
  const p = project();
  if (!p) return;
  // The heading: the experiment's title, then in small print its slug and
  // folder, and — loudly, under them — why a declared title is not shown.
  $('project-name').textContent = titleOf(p);
  $('project-slug').textContent = slugOf(p);
  $('project-path').textContent = p.path;
  $('title-error').textContent = p.title_error || '';
  $('title-error').hidden = !p.title_error;
  showView(nextView || rememberedView(id));
  // The modes and the experiment's scripts, sorted by name (byName), at the
  // owner's request (2026-10-06). The menu still opens where it did before
  // it was sorted: on the project's "Preview images" (the quickest look at
  // the stimulus) when it has one, else on Simulate.
  const preview = p.scripts.find((s) => s.label === 'Preview images');
  const modes = [
    ...Object.entries(MODES).map(([value, [text]]) => [value, text]),
    ...p.scripts.map((s) => [s.id, s.label]),
  ].sort((a, b) => byName(a[1], b[1]));
  options($('mode'), modes, preview ? preview.id : 'simulate');
  // Rigs by name, the experiment's own and then the shared ones (rigMenu).
  rigMenu(p);
  // The Task parameters entries by name, each pairing a file with the task
  // it runs, opening on the default task's own file (presetMenu).
  presetMenu(p);
  // "Each launch saves a parameter snapshot" is untrue without a file, and
  // with nothing to edit the Fields / Text switch goes too: only the
  // message saying what runs instead remains (renderEditor).
  $('editor-switch').hidden = !p.configs.length;
  // The server lists runs newest first, so the first match is the latest.
  runId = state.runs.find((r) => r.project === id)?.id || null;
  $('extra-args').value = '';
  $('parameter-search').value = '';
  modeChanged();
  renderState();
  // Each of these checks that the project is still selected before it
  // writes; the reader may click another project while they load.
  await Promise.all([loadSchema(id), loadConfig(), loadRig(), loadPeople(id)]);
  await refreshRun();
}

/**
 * Fetch the parameter schema for project `id` — of the selected task when
 * the project declares tasks, of its one task otherwise (the server refuses
 * a task name for a project without a table, so none is sent). Dropdowns
 * need it, but a launch must not be blocked by its absence: on failure the
 * fields render from the current values alone and a notice says why. The
 * epoch drops the answer for a task that is no longer the selected one; the
 * `selected` check, the answer for a project that is not.
 */
async function loadSchema(id) {
  const epoch = ++schemaEpoch;
  parameterSchema = {};
  loadingSchema = true;
  updateLaunch();
  $('choices-notice').hidden = true;
  let query = `project=${encodeURIComponent(id)}`;
  const task = selectedTask();
  schemaTask = task;
  if (task !== null) query += `&task=${encodeURIComponent(task)}`;
  // A stale answer must not land on the current task's schema, nor end the
  // "loading" state of a newer request that is still in flight.
  const current = () => epoch === schemaEpoch && selected === id;
  try {
    const schema = await api(`/api/schema?${query}`);
    if (!current()) return;
    parameterSchema = schema;
  } catch (e) {
    if (!current()) return;
    showChoicesError(e.message);
  } finally {
    if (current()) {
      loadingSchema = false;
      renderEditor();
      updateLaunch();
    }
  }
}

/**
 * Say why the parameter choices could not be read, readably. The server's
 * message ends with the error the task's code raised, after a whole Python
 * traceback; the notice shows that last line and what it means here, and
 * keeps the full text folded in a <details> for whoever has to fix it.
 */
function showChoicesError(message) {
  const lines = message.split('\n').map((line) => line.trim()).filter(Boolean);
  const last = lines.at(-1) || 'Unknown error';
  // What still works: with a file, its values are shown without the
  // choice lists; without one there is nothing more to say.
  const rest = project()?.configs?.length
    ? ' The fields show the file’s values as they are; use the text editor for others.'
    : '';
  const sentence = node('p', '', `${last} — the dashboard cannot read this task’s parameter `
    + `choices.${rest}`);
  const notice = $('choices-notice');
  if (lines.length > 1) {
    const details = node('details');
    details.append(node('summary', '', 'Full error'), node('pre', '', message));
    notice.replaceChildren(sentence, details);
  } else {
    notice.replaceChildren(sentence);
  }
  notice.hidden = false;
}

/**
 * The reader picked another Task parameters entry: load its file, and — when
 * it runs another task than the schema on hand is for — that task's schema
 * with it, so the editor shows the new task's parameters with the new
 * task's choices. Each load drops its answer if the reader has moved on
 * again meanwhile.
 */
async function parameterSetChanged() {
  const p = project();
  describeSet(p);
  const loads = [loadConfig()];
  if (selectedTask() !== schemaTask) loads.push(loadSchema(p.id));
  await Promise.all(loads);
}

/**
 * Read the selected rig and summarise its monitor, and say whose rig it is.
 * The server answers with the rig as it would run — merged over the shared
 * rig it extends, when it extends one (/api/rig) — so a rig whose own file
 * says nothing about the live monitor still reports the shared rig's setting.
 * The epoch drops the answer for a rig that is no longer the selected one.
 */
async function loadRig() {
  const epoch = ++rigEpoch;
  const p = project();
  const value = $('rig').value;
  $('rig-summary').textContent = value
    ? 'Reading rig…'
    : 'No rigs: none in this experiment’s configs/ (rig-<name>.yaml), and none '
      + 'shared by its alhazen.';
  if (!value) return;
  const query = `project=${encodeURIComponent(p.id)}&rig=${encodeURIComponent(value)}`;
  const data = await api(`/api/rig?${query}`);
  if (epoch !== rigEpoch) return;
  const rig = data.values;
  // Whose rig this is, by the qualified name the menu shows and the command
  // line takes, then where it comes from and the shared rig it extends.
  // (A shared rig the experiment's own hides is not in the menu, so it is
  // never summarised here: rigMenu.)
  const name = rigName(data.name, data.source, slugOf(p));
  const origin = data.source === 'alhazen'
    ? [name, 'alhazen’s shared rig']
    : [name, `this experiment’s ${value}`];
  if (data.extends) origin.push(`extends alhazen/${data.extends}`);
  const m = rig.monitor || {};
  // The live monitor is opt-in (LiveMonitorConfig.enabled defaults to false),
  // so a rig without the block, or without the key, has it off. Remembered
  // for the Live monitor tab, which cannot re-read the YAML on every poll.
  // `dashboard:` is the same section as alhazen spelled it before 1.9. It
  // stays here although alhazen 2.0 refuses it, because the rig is read by
  // the PROJECT's alhazen, not this one: a project still on 1.x (before 1.9
  // it is the only spelling there is; 1.9 and 1.10 read either) says it, its
  // session reads it, and the workspace launches it (/api/rig is the rig
  // file unvalidated; workspace.py _as_the_project_reads_it). The page must
  // agree with that session about whether a monitor is coming. A project on
  // 2.0 that still says it is refused at launch, naming `live_monitor:`.
  const monitorOn = rig.live_monitor?.enabled === true || rig.dashboard?.enabled === true;
  rigMonitor[`${p.id}:${value}`] = monitorOn;
  // The display backend, for the launch footer's PsychoPy warning. A rig
  // that does not say gets the model's default (DisplayConfig.backend).
  rigBackend[`${p.id}:${value}`] = rig.display?.backend || 'psychopy';
  // Whether real data may be collected on it. The model's default is true,
  // so only an explicit false — in the file or the shared rig it extends,
  // the answer already merged here — makes a development rig.
  const development = rig.real_data === false;
  rigDevelopment[`${p.id}:${value}`] = development ? name : null;
  // '?' rather than 'undefined' for a field the YAML leaves to its default.
  setCalibrationRig(p, rig);
  measureRig = rig;
  renderMeasurements();
  rigFacts([
    ['Screen', [`${m.width_px ?? '?'} × ${m.height_px ?? '?'} px`, `${m.refresh_rate_hz ?? '?'} Hz`]],
    ['Size', [`${m.width_cm ?? '?'} cm wide`, `${m.distance_cm ?? '?'} cm away`]],
    ['Display', [rig.display?.backend || 'default display']],
    ['Live monitor', [monitorOn ? 'on' : 'off']],
    // Shown only for a development rig: every other rig collects, as every
    // rig did before the setting existed, and a line saying so on each
    // would be noise.
    ...(development ? [['Real data', ['refused', 'a development rig (real_data: false)']]] : []),
    ['Rig', origin],
  ]);
  // The footer's warning depends on the backend just learned.
  updateLaunch();
}

/* ------------------------------------------------------------------ */
/* Calibration target                                                  */
/* ------------------------------------------------------------------ */

/**
 * Take the selected rig's calibration target as the starting choice. Only a
 * rig whose tracker draws a target (an EyeLink or a TRACKPixx3) gets the
 * controls, and only for a project whose alhazen offers the choice — one
 * from before it is told so instead of being offered flags its run.py would
 * refuse (workspace.py _check_calibration_choice says the same).
 */
function setCalibrationRig(p, rig) {
  if (!CalibrationChoice.drawsTarget(rig)) {
    calibration = null;
  } else {
    const offer = p.calibration_targets || null;
    const setting = offer ? CalibrationChoice.fromRig(rig, offer.defaults) : null;
    calibration = {
      offer,
      monitor: rig.monitor || {},
      backend: rig.devices.eyetracker.backend,
      rig: setting,
      current: setting ? structuredCloneOf(setting) : null,
    };
  }
  renderCalibration();
}

function structuredCloneOf(setting) {
  return {...setting, images: [...setting.images], pulse: {...setting.pulse}};
}

/** The page clock, in seconds, for the preview's pulse. */
function pageSeconds() {
  return (typeof performance !== 'undefined' ? performance.now() : Date.now()) / 1000;
}

/** Whether the calibration choice applies to the launch the form describes:
 *  run or test, on the rig's own tracker (not Mouse as gaze). */
function calibrationApplies() {
  const mode = $('mode').value;
  return CalibrationChoice.CALIBRATING_MODES.includes(mode)
    && !(mode === 'test' && $('mouse').checked);
}

/** The launch's `calibration_target`: what differs from the rig, or null. */
function calibrationToSend() {
  if (!calibration?.current || !calibrationApplies()) return null;
  return CalibrationChoice.toSend(calibration.current, calibration.rig);
}

/** Why the calibration choice blocks a launch, or ''. */
function calibrationProblem() {
  if (!calibration?.current || !calibrationApplies()) return '';
  return CalibrationChoice.problem(calibration.current) || '';
}

/** Two- or three-way switch: buttons with aria-pressed, as the editor's. */
function segmented(container, options, chosen, onChoose, disabled) {
  container.replaceChildren(...options.map(([value, text]) => {
    const button = node('button', '', text);
    button.type = 'button';
    button.dataset.value = value;
    button.setAttribute('aria-pressed', String(value === chosen));
    button.disabled = disabled;
    button.addEventListener('click', () => onChoose(value));
    return button;
  }));
}

function pictureUrl(p, name) {
  return `/calibration-picture?project=${encodeURIComponent(p.id)}`
    + `&name=${encodeURIComponent(name)}&token=${encodeURIComponent(token)}`;
}

/**
 * Draw the calibration-target controls from `calibration`: appearance and
 * motion switches, the picture grid when pictures are chosen, a preview of
 * the target, and what the choice means. Shown only for a rig whose tracker
 * draws a target; disabled, and said why, for a launch that does not
 * calibrate it.
 */
function renderCalibration() {
  const section = $('calibration');
  const p = project();
  stopCalibrationPreview();
  if (!calibration || !p) {
    section.hidden = true;
    updateLaunch();
    return;
  }
  section.hidden = false;
  const help = $('calibration-help');
  if (!calibration.current) {
    $('calibration-controls').hidden = true;
    $('calibration-origin').textContent = '';
    help.textContent = `${titleOf(p)}’s alhazen (${p.alhazen_version}) has no calibration-target `
      + 'choice, or was registered before the workspace asked: update its alhazen, then open '
      + 'Project settings and save.';
    updateLaunch();
    return;
  }
  $('calibration-controls').hidden = false;
  const current = calibration.current;
  const applies = calibrationApplies();
  const changed = CalibrationChoice.toSend(current, calibration.rig) !== null;
  $('calibration-origin').textContent = changed ? 'changed for this run' : 'the rig’s setting';
  segmented($('calibration-appearance'), CalibrationChoice.APPEARANCES, current.appearance,
    (value) => {
      if (value === current.appearance) return;
      current.appearance = value;
      // Each appearance starts from its own pictures: the rig's when it is
      // the rig's appearance, else none (every picture, for random ones).
      current.images = value === calibration.rig.appearance ? [...calibration.rig.images] : [];
      renderCalibration();
    }, !applies);
  segmented($('calibration-motion'), CalibrationChoice.MOTIONS, current.motion, (value) => {
    current.motion = value;
    renderCalibration();
  }, !applies);
  renderPictureGrid(p, current, applies);
  renderCalibrationPreview(p, current);
  const problem = applies ? CalibrationChoice.problem(current) : null;
  let text;
  if (!applies) {
    text = $('mode').value === 'test'
      ? 'Mouse as gaze replaces the eye tracker, so nothing is calibrated: the rig’s setting '
        + 'stands and nothing is sent.'
      : 'Applies to run and test, the modes that calibrate the rig’s eye tracker; this launch '
        + 'sends nothing.';
  } else if (problem) {
    text = problem;
  } else {
    text = `Every calibration in this run: ${CalibrationChoice.describe(current)}. The target `
      + 'layout, timing, sampling and fit are the rig’s, unchanged.';
  }
  help.textContent = text;
  help.classList.toggle('launch-warning', !!problem);
  updateLaunch();
}

/** The picture grid: every picture the project's alhazen ships, each a
 *  toggle. Chosen pictures show in the order picked (their number says
 *  which); a random set has no order, and none picked means all of them. */
function renderPictureGrid(p, current, applies) {
  const block = $('calibration-pictures-block');
  block.hidden = current.appearance === 'standard';
  if (block.hidden) return;
  const random = current.appearance === 'random_images';
  const count = current.images.length;
  $('calibration-pictures-label').textContent = random
    ? (count ? `${count} picture${count === 1 ? '' : 's'} to draw from` : 'Drawn from all pictures')
    : (count ? `${count} picture${count === 1 ? '' : 's'}, shown in this order` : 'Pick pictures');
  $('calibration-clear').hidden = !count;
  $('calibration-clear').disabled = !applies;
  const grid = $('calibration-pictures');
  grid.replaceChildren(...calibration.offer.images.map((name) => {
    const index = current.images.indexOf(name);
    const button = node('button', 'picture');
    button.type = 'button';
    button.disabled = !applies;
    button.title = name;
    button.setAttribute('aria-label', name);
    button.setAttribute('aria-pressed', String(index >= 0));
    const img = node('img');
    img.src = pictureUrl(p, name);
    img.alt = '';
    img.loading = 'lazy';
    button.append(img);
    if (index >= 0 && !random) button.append(node('span', 'picture-order', String(index + 1)));
    button.addEventListener('click', () => {
      const at = current.images.indexOf(name);
      if (at >= 0) current.images.splice(at, 1);
      else current.images.push(name);
      renderCalibration();
    });
    return button;
  }));
}

/** Whether the browser asks for less motion: the preview then stands still
 *  (the run itself pulses as configured; it is the experiment's choice). */
function reducedMotion() {
  return typeof matchMedia === 'function'
    && matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/**
 * The preview: the target on a mid-grey patch, at the rig's own pixel size
 * where it fits (scaled down, and said so, where it does not), pulsing at
 * the configured rate with the same formula the rig uses. A random choice
 * shows its pictures in turn. It shows the look and the motion, nothing
 * about how well a calibration will fit.
 */
function renderCalibrationPreview(p, current) {
  const stage = $('calibration-stage');
  const STAGE_PX = 132;
  const still = CalibrationChoice.stillPx(current, calibration.monitor);
  const largest = still === null ? null
    : still * (current.motion === 'pulse' ? current.pulse.max_scale : 1);
  const fit = largest ? Math.min(1, STAGE_PX / largest) : 1;
  const pool = current.appearance === 'standard' ? []
    : (current.images.length ? current.images : calibration.offer.images);
  const target = current.appearance === 'standard'
    ? node('span', 'standard-target')
    : node('img', 'picture-target');
  if (current.appearance !== 'standard') {
    target.alt = '';
    if (pool.length) target.src = pictureUrl(p, pool[0]);
  }
  stage.replaceChildren(target);
  const base = (still ?? 24) * fit;
  const size = (scale) => {
    target.style.width = `${base * scale}px`;
    target.style.height = `${base * scale}px`;
  };
  const motionNote = current.motion === 'pulse'
    ? ` · pulses ${current.pulse.min_scale}–${current.pulse.max_scale}× at ${current.pulse.rate_hz} Hz`
    : ' · still';
  const sizeNote = still === null
    ? 'size unknown: the rig gives no monitor geometry'
    : current.appearance === 'standard'
      ? '24 px across on the rig'
      : `${current.image_size_dva}° ≈ ${Math.round(still)} px on this rig`;
  const reduced = current.motion === 'pulse' && reducedMotion();
  $('calibration-caption').textContent = `${sizeNote}${motionNote}`
    + (fit < 1 ? ` · shown at ${Math.round(fit * 100)}%` : '')
    + (reduced ? ' · preview still (reduced motion)' : '')
    + '. A preview of the look only, not of calibration accuracy.';
  size(current.motion === 'pulse' ? current.pulse.min_scale : 1);
  const animate = !reducedMotion() && typeof requestAnimationFrame === 'function'
    && (current.motion === 'pulse' || pool.length > 1);
  if (!animate) return;
  const started = pageSeconds();
  const step = () => {
    const elapsed = pageSeconds() - started;
    if (current.motion === 'pulse') size(CalibrationChoice.pulseScale(elapsed, current.pulse));
    if (pool.length > 1) {
      // A new picture each pulse cycle (or each second and a half when
      // still), as a calibration shows a new one at each target.
      const period = current.motion === 'pulse' ? 1 / current.pulse.rate_hz : 1.5;
      const name = pool[Math.floor(elapsed / period) % pool.length];
      const url = pictureUrl(p, name);
      if (target.getAttribute('src') !== url) target.src = url;
    }
    calibrationFrame = requestAnimationFrame(step);
  };
  calibrationFrame = requestAnimationFrame(step);
}

function stopCalibrationPreview() {
  if (calibrationFrame !== null && typeof cancelAnimationFrame === 'function') {
    cancelAnimationFrame(calibrationFrame);
  }
  calibrationFrame = null;
}

/**
 * Load the selected parameter file into the editor. The launch button is
 * disabled until it lands: a run started meanwhile would use the task's
 * defaults for everything not yet loaded, silently. A project with no
 * parameter file (an empty path) loads nothing and leaves `values` null.
 */
async function loadConfig() {
  const epoch = ++configEpoch;
  const path = selectedParams();
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
    // A newly loaded file always opens in the fields editor.
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

/**
 * What the editor says when there are no values to edit, so a launch sends
 * no parameters: for a task of a table whose entry names no file (or one the
 * project lacks), that it runs on its code's defaults; for a project with
 * no file at all, the same; and when a file is chosen but its text was
 * emptied in the text editor, that the launch now sends nothing.
 */
function noValuesHint(p) {
  const set = selectedSet();
  if (set?.task && !set.params) {
    if (set.missing) {
      return `run.py names ${set.missing} as ${set.task}’s parameter file, but the project `
        + 'has no such file. A launch sends no parameters, and run.py will look for that '
        + 'file itself; choose another entry above, or fix run.py.';
    }
    return `${set.label} has no parameter file: ${set.task} runs on the defaults in its code, `
      + 'and launches without --params.';
  }
  if (!p?.configs?.length) {
    return 'No task parameter files in configs/ (task*.yaml or params*.yaml). The task runs on '
      + 'the defaults written in its code, and launches without --params.';
  }
  return 'The text editor was left empty, so a launch sends no parameters and the task runs '
    + 'on the defaults written in its code. Choose a file above to start from its values.';
}

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
  // Nothing to search while there are no values (a project without files).
  $('parameter-search').hidden = editor !== 'fields' || values === null;
  $('parameter-yaml').hidden = editor !== 'yaml';
  // "Each launch saves a parameter snapshot" holds only with a file chosen.
  $('parameters-help').hidden = !selectedParams();
  if (editor !== 'fields') return;
  const host = $('parameter-fields');
  host.replaceChildren();
  if (values === null) {
    // No values to edit: a task that runs without a file, a project with
    // no parameter file at all, or a text editor emptied and switched back
    // (while a file loads, loadConfig shows "Loading parameters…" instead).
    // Either way a launch now sends no parameters, and the reader is told
    // what runs instead (noValuesHint).
    host.append(node('p', 'help', noValuesHint(project())));
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
 * Draw what depends on the server state as a whole: welcome screen or the
 * selected experiment's heading and view, the project list (only when it
 * changed), the launch button and the history list.
 */
function renderState() {
  const p = project();
  const home = !p;
  $('home-view').hidden = !home;
  $('empty').hidden = !home || state.projects.length > 0;
  $('project-heading').hidden = home;
  $('general-view').hidden = home || view !== 'general';
  $('workspace').hidden = home || view !== 'run';
  $('data-view').hidden = home || view !== 'data';
  $('history-view').hidden = home || view !== 'history';
  $('project-count').textContent = state.projects.filter((x) => !x.archived).length;
  // The title can change under a registered experiment (its pyproject is
  // re-read on every poll), so the heading and tab follow it here too.
  if (p) nameView(p);
  else nameHome();
  const signature = JSON.stringify([state.projects, selected, view, state.active]);
  if (signature !== projectSignature) {
    projectSignature = signature;
    renderNav();
    if (home) renderHome();
  }
  updateLaunch();
  renderHistory();
}

/** The address of a screen: the Experiments page, or one page of one
 *  experiment. Other query parameters are kept as they are. */
function addressOf(id, page) {
  const query = new URLSearchParams(location.search || '');
  query.delete('project');
  query.delete('view');
  if (id) {
    query.set('project', id);
    query.set('view', page);
  } else {
    query.set('view', 'experiments');
  }
  return `${location.pathname}?${query}`;
}

/** The screen the address names: {project, view}. `?view=<page>` without a
 *  project opens the experiment used last (or the first) on that page; no
 *  view at all, or `experiments`, is the Experiments page. */
function route() {
  const query = new URLSearchParams(location.search || '');
  const page = query.get('view');
  const id = query.get('project');
  if (id) return {project: id, view: Object.hasOwn(VIEWS, page) ? page : null};
  if (page && Object.hasOwn(VIEWS, page)) {
    const remembered = localStorage.getItem('alhazen-workspace-project');
    const usable = state.projects.filter((x) => !x.archived);
    const found = usable.find((x) => x.id === remembered) || usable[0];
    return {project: found?.id || null, view: page};
  }
  return {project: null, view: null};
}

/** Show the screen `target` names: an experiment's page (opening the
 *  experiment if another was open) or the Experiments page. An address
 *  naming an experiment that is not registered lands on the Experiments
 *  page and says so. Leaving a page never stops a run. */
async function applyRoute(target) {
  if (target.project && !state.projects.some((x) => x.id === target.project)) {
    leaveExperiment();
    error('That experiment is not registered in this workspace (it may have been '
      + 'unregistered). Choose one below.');
    history.replaceState(null, '', addressOf(null));
    return;
  }
  if (!target.project) {
    leaveExperiment();
    return;
  }
  if (target.project !== selected) {
    await chooseProject(target.project, target.view);
  } else {
    showView(target.view || rememberedView(target.project));
  }
}

/** Close the open experiment's pages and show the Experiments page. */
function leaveExperiment() {
  for (const script of [window.WorkspaceData, window.WorkspaceManage]) script?.hide?.();
  selected = null;
  people = null;
  peopleEpoch += 1;
  projectSignature = '';
  renderState();
}

/** Go to a screen: a new history entry (or the current one replaced), then
 *  the screen. Unsaved edits on a management page are asked about first
 *  (WorkspaceManage.leave), so a link never silently drops them. */
async function navigate(id, page, {replace = false} = {}) {
  const leaving = window.WorkspaceManage?.leave;
  if (typeof leaving === 'function' && !(await leaving())) return;
  const url = addressOf(id, page);
  const here = location.pathname + (location.search || '');
  if (url !== here) {
    if (replace || typeof history.pushState !== 'function') history.replaceState(null, '', url);
    else history.pushState(null, '', url);
  }
  error('');
  await applyRoute(id ? {project: id, view: page} : {project: null, view: null});
}

/** A plain left click, as opposed to one that opens a new tab or window. */
function plainClick(event) {
  return !(event.metaKey || event.ctrlKey || event.shiftKey || event.button);
}

/** A sidebar or page link: an <a> with its real address (so a middle click
 *  opens it in a new tab), whose plain click navigates in place. */
function link(className, text, id, page) {
  const a = node('a', className, text);
  a.href = addressOf(id, page);
  a.addEventListener('click', (event) => {
    if (!plainClick(event)) return;
    event.preventDefault();
    guard(() => navigate(id, page))();
  });
  return a;
}

/**
 * The sidebar: Experiments (the workspace's home), marked when shown; the
 * run in progress, whichever page is open; and the open experiment's pages
 * — General, Run, Data, History — with the shown one marked.
 */
function renderNav() {
  const p = project();
  const home = $('nav-experiments');
  home.classList.toggle('selected', !p);
  home.setAttribute('aria-current', !p ? 'page' : 'false');
  const running = state.active ? state.runs.find((r) => r.id === state.active) : null;
  const owner = running ? state.projects.find((x) => x.id === running.project) : null;
  const pill = $('nav-running');
  pill.hidden = !owner;
  if (owner) {
    pill.href = addressOf(owner.id, 'run');
    pill.replaceChildren(
      node('span', 'nav-running-lamp'),
      node('span', 'nav-running-text', `Running · ${titleOf(owner)}`),
    );
  }
  $('nav-experiment').hidden = !p;
  if (!p) {
    $('experiment-nav').replaceChildren();
    return;
  }
  $('nav-experiment-title').textContent = titleOf(p);
  const activeHere = !!running && running.project === p.id;
  const items = Object.entries(VIEWS).map(([name, text]) => {
    const item = link('nav-item view-button' + (name === view ? ' selected' : ''), '', p.id, name);
    item.dataset.view = name;
    item.setAttribute('aria-current', name === view ? 'page' : 'false');
    item.append(node('span', 'nav-glyph', VIEW_GLYPHS[name]), node('span', 'nav-label', text));
    if (name === 'run' && activeHere) item.append(node('span', 'nav-lamp', ''));
    return item;
  });
  $('experiment-nav').replaceChildren(...items);
}

/** The Experiments page, drawn by workspace_manage.js; a plain message when
 *  that script is missing, so the page is never blank. */
function renderHome() {
  const manage = window.WorkspaceManage;
  if (typeof manage?.renderHome === 'function') {
    try {
      manage.renderHome($('home-list'), state, pageHelpers());
    } catch (e) {
      console.error(e);
      $('home-list').replaceChildren(node('p', 'data-unavailable',
        `The experiments could not be listed: ${e.message}`));
    }
  } else {
    $('home-list').replaceChildren(node('p', 'data-unavailable',
      'Experiment management is not available'));
  }
}

/** What the management script is handed: the page's request helper and
 *  token, its element helper and error banner, navigation, and callbacks
 *  for what it changes. */
function pageHelpers() {
  return {
    api, token, node, error, navigate, link, addressOf,
    titleOf, slugOf, date, label,
    state: () => state,
    openSettings: () => openProject(true),
    register: () => openProject(false),
    peopleChanged,
    refresh: () => guard(refresh)(),
    viewRun: (id) => {
      runId = id;
      gallerySignature = '';
      return navigate(selected, 'run');
    },
  };
}

/** The view last shown for experiment `id`, 'run' when none was; a stored
 *  value this page does not know (from a later version, or edited by hand)
 *  is reported in the console and ignored rather than shown as a blank. */
function rememberedView(id) {
  const stored = localStorage.getItem(VIEW_KEY + id);
  if (stored === null) return 'run';
  if (Object.hasOwn(VIEWS, stored)) return stored;
  console.warn(`Ignoring the unknown workspace view ${JSON.stringify(stored)} for ${id}`);
  return 'run';
}

/** Name the open experiment and its page in the breadcrumb, the heading's
 *  eyebrow and the browser tab ("Run · <title> · Alhazen"), so the
 *  browser's history list tells the pages apart. */
function nameView(p) {
  $('breadcrumb').textContent = titleOf(p);
  $('breadcrumb-view').textContent = `/ ${VIEWS[view]}`;
  $('view-eyebrow').textContent = VIEWS[view].toUpperCase();
  document.title = `${VIEWS[view]} · ${titleOf(p)} · Alhazen`;
}

/** The same for the Experiments page. */
function nameHome() {
  $('breadcrumb').textContent = 'Experiments';
  $('breadcrumb-view').textContent = '';
  document.title = 'Experiments · Alhazen';
}

/**
 * Show page `next` of the open experiment and remember it for that
 * experiment. Leaving the Data, General or History page tells its script
 * (hide) so it can stop what it is doing; entering one hands it the
 * experiment and the page's helpers (show) — every time, so switching
 * experiments while on it shows the new one's. Without the script the page
 * says so plainly instead of staying blank. Leaving the Run page stops
 * nothing: a run goes on, and its page shows it again on return.
 */
function showView(next) {
  const p = project();
  if (!p) return;
  if (!Object.hasOwn(VIEWS, next)) throw new Error(`Unknown workspace view: ${next}`);
  const data = window.WorkspaceData;
  const manage = window.WorkspaceManage;
  if (view === 'data' && next !== 'data') data?.hide?.();
  if (['general', 'history'].includes(view)) manage?.hide?.();
  view = next;
  localStorage.setItem(VIEW_KEY + p.id, next);
  nameView(p);
  $('general-view').hidden = next !== 'general';
  $('workspace').hidden = next !== 'run';
  $('data-view').hidden = next !== 'data';
  $('history-view').hidden = next !== 'history';
  if (next === 'data') {
    if (typeof data?.show === 'function') {
      data.show(p, {api, token, node, error});
    } else {
      $('data-view').replaceChildren(
        node('p', 'data-unavailable', 'Data inspection is not available'),
      );
    }
  }
  if (next === 'general' || next === 'history') {
    const target = $(`${next}-view`);
    const method = next === 'general' ? 'showGeneral' : 'showHistory';
    if (typeof manage?.[method] === 'function') {
      // A page that fails to draw says so, rather than staying on "Loading…".
      Promise.resolve()
        .then(() => manage[method](target, p, pageHelpers()))
        .catch((e) => {
          console.error(e);
          target.replaceChildren(node('p', 'data-unavailable',
            `This page could not be drawn: ${e.message}`));
        });
    } else {
      target.replaceChildren(node('p', 'data-unavailable',
        'Experiment management is not available'));
    }
  }
  // The sidebar marks the view shown: redraw it.
  projectSignature = '';
  renderState();
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
    // Who the run was for, when it names a subject: "sub-01 · HD".
    const subject = who(run) ? ` · ${who(run)}` : '';
    // And the seed it ran with, so a session can be repeated and two
    // sessions told apart: "seed 2718281828", or "seed new" while a drawn
    // one is not known yet.
    const seed = seedText(run) ? ` · ${seedText(run)}` : '';
    text.append(
      node('strong', '', title(run)),
      node(
        'small', '', `${date(run.started)} · ${runRig(run, slugOf(project()))}${subject}${seed}`,
      ),
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
 * The Live monitor tab for `run`. The session's live monitor is a separate
 * loopback server that the session process starts and that dies with it,
 * and the runner prints its URL (which carries that server's own token)
 * to the console; the launcher relays it as `run.monitor`.
 *
 * While the run is active and that URL is known, the monitor page is framed
 * at it, loaded once — a reload on every poll would restart the page under
 * the reader — and the open-in-new-tab link points at it too. In every
 * other case the frame is emptied, so a browser error page for a server
 * that no longer exists never sits in the tab, and a note says what the
 * reader is looking at instead: the saved copy, a rig with the live monitor
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
    // The file is named by the alhazen the session ran, which is the
    // project's, not this page's. alhazen 2.0 renamed it; a run recorded by
    // an older alhazen keeps the old name, and the history mixes both. So
    // both are given rather than one guessed from a version.
    setMonitorNote(
      'The live monitor closes with the session. Its final state was saved in the run’s '
      + 'data directory as ',
      node('code', '', 'figures/live_monitor.html'),
      ' (',
      node('code', '', 'figures/dashboard.html'),
      ' by an alhazen before 2.0).',
    );
  } else if (rigMonitor[`${run.project}:${run.rig}`] === false) {
    setMonitorNote(
      'This rig has ',
      node('code', '', 'live_monitor.enabled: false'),
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
    info = who(run)
      ? `${title(run)} · ${who(run)} · ${date(run.started)}`
      : `${title(run)} · ${date(run.started)}`;
    if (seedText(run)) info += ' · ' + seedText(run);
    if (run.returncode !== null) info += ' · exit ' + run.returncode;
    if (run.error) info += ' · ' + run.error;
  }
  $('run-info').textContent = info;
  // Only the active run can be stopped; while it stops, the button waits.
  $('stop').hidden = !run || run.id !== state.active;
  $('stop').disabled = run?.status === 'stopping';
  $('stop').textContent = run?.status === 'stopping' ? 'Stopping…' : 'Stop run';
  // A Measure rig run's queue, above the tabs.
  if (window.MeasureChoice) MeasureChoice.renderProgress($('measure-progress'), run?.measurement || null);
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
  // Do not replace a playing video on every poll. Defer videos until the
  // encoder closes them; an unfinished MP4 has no readable index yet.
  const visible = artifacts.filter((a) => !a.type.startsWith('video/') || !active);
  // The empty state stands in for the gallery whenever nothing is shown —
  // also while every artifact so far is a deferred video — so it follows
  // `visible`, and is set on every pass, before the early return below.
  const empty = $('gallery-empty');
  empty.hidden = visible.length > 0;
  // Its wording follows the run's situation.
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
  const signature = JSON.stringify([key, visible]);
  if (signature === gallerySignature) return;
  gallerySignature = signature;
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
  if (!routed) {
    // The first answer: show the screen the address names (route()).
    routed = true;
    await applyRoute(route());
    return;
  }
  if (selected && !project()) {
    // The open experiment was unregistered (in another tab, say).
    leaveExperiment();
    error('This experiment is no longer registered in the workspace.');
    history.replaceState(null, '', addressOf(null));
    return;
  }
  renderState();
  if (selected) await refreshRun();
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

/** Open the project dialog: to register a new experiment, or (edit) the
 *  current one's settings. The folder is fixed once registered; the
 *  interpreter is what changes. */
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
  $('remove-confirm').hidden = true;
  $('dialog-error').textContent = '';
  $('project-dialog').showModal();
}

/* ------------------------------------------------------------------ */
/* Colour theme                                                        */
/* ------------------------------------------------------------------ */

/**
 * Apply colour theme `choice` — 'system', 'light' or 'dark' — and mark its
 * button in the sidebar. 'system' removes the page's data-theme attribute,
 * so the stylesheet's prefers-color-scheme rule decides (and follows the
 * operating system when it switches); 'light' and 'dark' set it, which the
 * stylesheet obeys over the system's setting (workspace.css, tokens).
 * `remember` stores the choice in localStorage for the next visit.
 */
function setTheme(choice, remember = true) {
  if (!THEMES.includes(choice)) throw new Error(`Unknown colour theme: ${choice}`);
  const root = document.documentElement;
  if (choice === 'system') delete root.dataset.theme;
  else root.dataset.theme = choice;
  for (const name of THEMES) {
    $(`theme-${name}`).setAttribute('aria-pressed', String(name === choice));
  }
  if (remember) localStorage.setItem(THEME_KEY, choice);
}

/** The theme remembered from the last visit, 'system' when none was; a
 *  stored value this page does not know is reported in the console and
 *  replaced by the default rather than applied half-way. */
function rememberedTheme() {
  const stored = localStorage.getItem(THEME_KEY);
  if (stored === null) return 'system';
  if (THEMES.includes(stored)) return stored;
  console.warn(`Ignoring the unknown colour theme ${JSON.stringify(stored)}`);
  return 'system';
}

/* ------------------------------------------------------------------ */
/* Wiring                                                              */
/* ------------------------------------------------------------------ */

// The remembered theme, before the first poll draws anything else.
setTheme(rememberedTheme(), false);
for (const name of THEMES) {
  $(`theme-${name}`).addEventListener('click', () => setTheme(name));
}

$('add-project').addEventListener('click', () => openProject());
$('empty-add').addEventListener('click', () => openProject());
$('project-settings').addEventListener('click', () => openProject(true));
$('close-dialog').addEventListener('click', () => $('project-dialog').close());
$('close-image').addEventListener('click', () => $('image-dialog').close());
$('mode').addEventListener('change', modeChanged);
$('rig').addEventListener('change', guard(loadRig));
// Headless simulate opens no window, so the PsychoPy warning follows it.
$('headless').addEventListener('change', updateLaunch);
// Mouse as gaze takes the tracker away, and with it the calibration choice.
$('mouse').addEventListener('change', () => (calibration ? renderCalibration() : updateLaunch()));
$('calibration-clear').addEventListener('click', () => {
  if (!calibration?.current) return;
  calibration.current.images = [];
  renderCalibration();
});
$('params-config').addEventListener('change', guard(parameterSetChanged));
// Typing a subject or session changes the launch summary, nothing else.
$('subject').addEventListener('input', () => updateLaunch());
$('session').addEventListener('input', () => updateLaunch());
$('parameter-search').addEventListener('input', filterParameters);
$('fields-tab').addEventListener('click', guard(() => switchEditor('fields')));
$('yaml-tab').addEventListener('click', guard(() => switchEditor('yaml')));
$('media-tab').addEventListener('click', () => outputTab('media'));
$('console-tab').addEventListener('click', () => outputTab('console'));
$('monitor-tab').addEventListener('click', () => outputTab('monitor'));
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
    // A new experiment is registered (a folder already registered is
    // refused, by name); an existing one's settings save registers it again
    // over the same record, keeping its notes.
    const body = {path: $('project-folder').value, python: $('project-python').value};
    const p = editProject
      ? await api('/api/projects', body)
      : await api('/api/manage/register', body);
    state = await api('/api/state');
    $('project-dialog').close();
    error('');
    projectSignature = '';
    if (editProject) await chooseProject(p.id, view);
    else await navigate(p.id, 'general');
  } catch (e) {
    $('dialog-error').textContent = e.message;
  } finally {
    button.disabled = false;
  }
});

/* Unregistering is confirmed in place, saying what it does not do. */
$('remove-project').addEventListener('click', () => {
  $('remove-confirm').hidden = false;
});
$('remove-cancel').addEventListener('click', () => {
  $('remove-confirm').hidden = true;
});
$('remove-confirmed').addEventListener('click', guard(async () => {
  await api('/api/projects/remove', {id: selected});
  $('project-dialog').close();
  runId = null;
  state = await api('/api/state');
  await navigate(null, null);
}));

/* Who: the Run page's menus and the typed fields' fold. */
$('subject-record').addEventListener('change', () => identityChanged());
$('experimenter').addEventListener('change', () => identityChanged());
$('typed-identity').addEventListener('toggle', () => identityChanged());
$('initials').addEventListener('input', () => updateLaunch());

/* Back and Forward: show the screen the address now names. */
window.addEventListener('popstate', () => guard(() => applyRoute(route()))());
for (const id of ['nav-experiments', 'nav-running']) {
  $(id).addEventListener('click', (event) => {
    if (!plainClick(event)) return;
    event.preventDefault();
    const target = new URLSearchParams($(id).href.split('?')[1] || '');
    guard(() => navigate(target.get('project'), target.get('view')))();
  });
}
/* Unsaved edits on a management page: the browser asks before a reload or
 * a closed tab drops them. */
window.addEventListener('beforeunload', (event) => {
  if (window.WorkspaceManage?.dirty?.()) {
    event.preventDefault();
    event.returnValue = '';
  }
});
/* Each experiment's earlier choice of who, for this tab. */
try {
  Object.assign(identityChoice,
    JSON.parse(sessionStorage.getItem('alhazen-workspace-identity') || '{}'));
} catch (e) {
  console.warn('Ignoring an unreadable saved choice of subject and experimenter', e);
}

$('launch-form').addEventListener('submit', guard(async (event) => {
  event.preventDefault();
  // The button is disabled in these states, but Enter in a field submits too.
  if (launching || (usesParameters() && (loadingConfig || loadingSchema)) || state.active) return;
  if (project().tasks_error) return;
  const mode = $('mode').value;
  // Who: a registered subject and experimenter, by their record ids (the
  // server takes the subject's ID and initials from the record), or a typed
  // subject. The initials, for the modes that show them: refused here in
  // the command line's own words before anything is sent (the server checks
  // again).
  const identity = ['run', 'test', 'simulate'].includes(mode);
  // A measurement of the subject in the chair names its subject too.
  const named = identity || measureNeed() !== 'none';
  const record = named ? chosenSubject() : null;
  const runner = ['run', 'test', 'simulate', 'measure'].includes(mode)
    ? chosenExperimenter()
    : null;
  if (identityProblem()) {
    error(identityProblem());
    return;
  }
  const initials = identity && !record
    ? checkInitials($('initials').value, ['run', 'test'].includes(mode))
    : {value: '', problem: ''};
  if (initials.problem) {
    error(initials.problem);
    return;
  }
  // The seed, for the modes that take one: empty is null, and the session
  // draws its own. Measure and the experiment's scripts take none, so they
  // send none, whatever the hidden or unused field holds.
  const isScript = project().scripts.some((s) => s.id === mode);
  const seed = mode === 'measure' || isScript
    ? {value: null, problem: ''}
    : readSeed($('seed'));
  if (seed.problem) {
    error(seed.problem);
    return;
  }
  launching = true;
  updateLaunch();
  error('');
  try {
    const request = {
      project: selected,
      mode,
      // The task the run is for, when the experiment declares several: the
      // one its Task parameters entry runs. A script takes none — the
      // server refuses one — so it is null there.
      task: isScript ? null : selectedTask(),
      // The Task parameters entry, by its label, for the history: the run's
      // task and folder keep the task's own name. None for a launch that
      // takes no parameters.
      parameter_set: !isScript && usesParameters() ? (selectedSet()?.label ?? null) : null,
      rig: $('rig').value,
      subject: record ? '' : $('subject').value,
      initials: initials.value,
      session: Number($('session').value),
      // null for an empty field (Number('') was 0, so every launch used to
      // send seed 0); see readSeed.
      seed: seed.value,
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
      // For every mode and script alike; the server splits and checks them.
      extra_args: $('extra-args').value,
    };
    // What differs from the rig's calibration target, for run and test;
    // sent only when something does — without it the run uses the rig's
    // own (renderCalibration).
    // Measure rig's ticked measurements, in run order; the server checks them.
    if (usesMeasurements()) request.measurements = MeasureChoice.selection(measureState);
    const calibrationTarget = calibrationToSend();
    if (calibrationTarget) request.calibration_target = calibrationTarget;
    // The records chosen, sent only when chosen: a launch without them is
    // the typed one every earlier client sent.
    if (record) request.subject_record = record.id;
    if (runner) request.experimenter = runner.id;
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
