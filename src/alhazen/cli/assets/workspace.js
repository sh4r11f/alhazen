'use strict';
const $ = (id) => document.getElementById(id);
const MODES = {
  simulate: ['Simulate', 'Run the complete session with a simulated participant.'],
  demo: ['Demo', 'View the stimulus on the rig’s display. Screenshots you save appear here.'],
  movie: ['Record movies', 'Render stimulus clips without opening a display.'],
  test: ['Test session', 'Rehearse with a person, using fewer trials and rehearsal data.'],
  run: ['Run experiment', 'Collect a real session using the selected rig and its data directory.'],
  measure: ['Measure rig', 'Check the physical display, response keys and eye tracker.'],
};
let token = new URLSearchParams(location.hash.slice(1)).get('token') || sessionStorage.getItem('alhazen-workspace-token') || '';
if (token) sessionStorage.setItem('alhazen-workspace-token', token);
if (location.hash) history.replaceState(null, '', location.pathname);
let state = {projects: [], runs: [], active: null};
let selected = null, runId = null, values = null, editor = 'fields', editProject = null;
let configEpoch = 0, rigEpoch = 0, gallerySignature = '', historySignature = '', projectSignature = '';
let launching = false, loadingConfig = false, connectionError = false, loadingSchema = false;
let parameterSchema = {};
window.addEventListener('hashchange', () => {
  const fresh = new URLSearchParams(location.hash.slice(1)).get('token');
  if (!fresh) return;
  token = fresh;
  sessionStorage.setItem('alhazen-workspace-token', token);
  history.replaceState(null, '', location.pathname);
  gallerySignature = '';
  guard(refresh)();
});

function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}
function error(message) { $('error').textContent = message; $('error').hidden = !message; }
async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: {'X-Alhazen-Token': token, ...(body === undefined ? {} : {'Content-Type': 'application/json'})},
    ...(body === undefined ? {} : {body: JSON.stringify(body)}),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}
function guard(fn) { return async (...args) => { try { await fn(...args); } catch (e) { error(e.message); } }; }
function project() { return state.projects.find((p) => p.id === selected); }
function label(mode) { return MODES[mode]?.[0] || project()?.scripts.find((s) => s.id === mode)?.label || mode; }
function date(value) { return new Date(value).toLocaleString([], {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}); }
function options(select, items, previous) {
  select.replaceChildren(...items.map(([value, text]) => { const el = node('option', '', text); el.value = value; return el; }));
  if (items.some(([v]) => v === previous)) select.value = previous;
}
function usesParameters() { return ParameterChoices.usesParameters($('mode').value, project()?.scripts || []); }
function updateLaunch() {
  $('launch').disabled = !!state.active || !project()?.rigs.length || launching || (usesParameters() && (loadingConfig || loadingSchema)) || !project()?.available;
  $('launch').textContent = launching ? 'Starting…' : state.active ? 'A run is in progress' : `▶ ${label($('mode').value) || 'Start run'}`;
  $('launch-note').textContent = state.active ? 'One run at a time keeps the rig available to its active experiment.' : $('mode').value === 'run' ? 'This mode records real subject data. Check the rig and subject ID before starting.' : 'Runs locally in your experiment’s Python environment.';
}
function modeChanged() {
  const mode = $('mode').value;
  const script = project()?.scripts.find((s) => s.id === mode);
  $('mode-help').textContent = MODES[mode]?.[1] || 'Run the experiment’s own script and collect its images and movies.';
  $('params-preset-field').hidden = !usesParameters();
  $('params-config').disabled = !usesParameters();
  $('task-parameters').hidden = !usesParameters();
  $('task-parameters').disabled = !usesParameters();
  $('seed-fields').hidden = mode === 'measure';
  $('seed').disabled = mode === 'measure';
  $('identity').hidden = !['run', 'test', 'simulate'].includes(mode);
  $('subject').required = ['run', 'test'].includes(mode);
  $('trials-field').hidden = !['test', 'simulate'].includes(mode);
  $('headless-field').hidden = mode !== 'simulate';
  $('mouse-field').hidden = mode !== 'test';
  $('windowed-field').hidden = !['test', 'run', 'demo', 'measure', 'simulate'].includes(mode);
  $('movie-options').hidden = mode !== 'movie';
  $('script-options').hidden = !script;
  $('script-help').textContent = script ? `Available flags: ${script.flags.filter((f) => !['--out','--rig','--params','--task-config'].includes(f)).join(', ') || 'none'}` : '';
  updateLaunch();
}
async function chooseProject(id) {
  selected = id;
  localStorage.setItem('alhazen-workspace-project', id || '');
  projectSignature = ''; historySignature = ''; gallerySignature = '';
  const p = project();
  if (!p) return;
  $('project-name').textContent = p.name;
  $('project-path').textContent = p.path;
  $('breadcrumb').textContent = p.name;
  options($('mode'), [...p.scripts.filter((s) => s.label === 'Preview images').map((s) => [s.id, s.label]), ...Object.entries(MODES).map(([v, [t]]) => [v,t]), ...p.scripts.filter((s) => s.label !== 'Preview images').map((s) => [s.id, s.label])]);
  options($('rig'), p.rigs.map((r) => [r, r.split('/').pop()]), p.rigs.find((r) => r.endsWith('rig-mac.yaml')) || p.rigs[0]);
  options($('params-config'), [['', 'Task defaults'], ...p.configs.map((p) => [p, p.split('/').pop()])], p.configs.find((p) => p.endsWith('/task.yaml')) || '');
  runId = state.runs.find((r) => r.project === id)?.id || null;
  $('script-args').value = '';
  $('parameter-search').value = '';
  modeChanged(); renderState();
  await Promise.all([loadSchema(id), loadConfig(), loadRig()]);
  await refreshRun();
}
async function loadSchema(id) {
  parameterSchema = {}; loadingSchema = true; updateLaunch();
  $('choices-notice').hidden = true;
  try {
    const schema = await api(`/api/schema?project=${encodeURIComponent(id)}`);
    if (selected !== id) return;
    parameterSchema = schema;
  } catch (e) {
    if (selected !== id) return;
    $('choices-notice').textContent = `Could not load model choices. Showing current values; use YAML for other values. ${e.message}`;
    $('choices-notice').hidden = false;
  } finally {
    if (selected === id) { loadingSchema = false; renderEditor(); updateLaunch(); }
  }
}
async function loadRig() {
  const epoch = ++rigEpoch, p = project(), path = $('rig').value;
  $('rig-summary').textContent = path ? 'Reading rig…' : 'No rig YAML found in configs/. Add a rig to this experiment.';
  if (!path) return;
  const data = await api(`/api/config?project=${encodeURIComponent(p.id)}&path=${encodeURIComponent(path)}`);
  if (epoch !== rigEpoch) return;
  const rig = data.values, m = rig.monitor || {};
  $('rig-summary').textContent = `${m.width_px ?? '?'} × ${m.height_px ?? '?'} px · ${m.refresh_rate_hz ?? '?'} Hz · ${rig.display?.backend || 'default display'}\n${m.width_cm ?? '?'} cm wide · ${m.distance_cm ?? '?'} cm viewing distance`;
}
async function loadConfig() {
  const epoch = ++configEpoch, path = $('params-config').value, p = project();
  loadingConfig = true; updateLaunch();
  values = null;
  $('parameter-yaml').value = '';
  $('parameter-fields').replaceChildren(node('p','help','Loading parameters…'));
  try {
    if (path) {
      const data = await api(`/api/config?project=${encodeURIComponent(p.id)}&path=${encodeURIComponent(path)}`);
      if (epoch !== configEpoch) return;
      values = data.values;
      $('parameter-yaml').value = data.text;
    }
    editor = 'fields'; renderEditor();
    loadingConfig = false;
  } finally {
    // A failed config load must not enable a launch with unintended defaults.
    if (epoch === configEpoch) updateLaunch();
  }
}
function setValue(path, value) {
  let parent = values;
  for (const key of path.slice(0, -1)) parent = parent[key];
  parent[path.at(-1)] = value;
}
function renderEditor() {
  $('fields-tab').setAttribute('aria-pressed', String(editor === 'fields'));
  $('yaml-tab').setAttribute('aria-pressed', String(editor === 'yaml'));
  $('parameter-fields').hidden = editor !== 'fields';
  $('parameter-search').hidden = editor !== 'fields';
  $('parameter-yaml').hidden = editor !== 'yaml';
  if (editor !== 'fields') return;
  const host = $('parameter-fields'); host.replaceChildren();
  if (values === null) { host.append(node('p','help','Using the task’s own defaults. Choose a parameter preset to edit its values.')); return; }
  let number = 0;
  function addFields(object, prefix = []) {
    for (const [key, value] of Object.entries(object)) {
      const path = [...prefix, key];
      if (value !== null && typeof value === 'object' && !Array.isArray(value)) { addFields(value, path); continue; }
      const row = node('div','parameter-row'); row.dataset.key = path.join('.').toLowerCase();
      const lab = node('label','',key.replaceAll('_',' ')); lab.htmlFor = `param-${number++}`;
      if (prefix.length) lab.append(node('small','',prefix.join(' / ')));
      const selection = ParameterChoices.describe(parameterSchema, path, value);
      if (selection) {
        if (selection.multiple) {
          const dropdown = node('details', 'parameter-multiselect');
          const summary = node('summary', '', value.join(', ') || 'Choose values…');
          summary.id = lab.htmlFor; summary.setAttribute('aria-label', path.join(' / '));
          const choices = node('div', 'parameter-options');
          for (const choice of selection.choices) {
            const option = node('label'); const check = node('input');
            check.type = 'checkbox'; check.checked = value.includes(choice);
            check.addEventListener('change', () => {
              // Retain existing order: switching one level must not reorder the others.
              const current = path.reduce((v, k) => v[k], values);
              const next = check.checked ? [...current, choice] : current.filter((v) => v !== choice);
              setValue(path, next); summary.textContent = next.join(', ') || 'Choose values…';
            });
            option.append(check, node('span', '', choice)); choices.append(option);
          }
          dropdown.append(summary, choices); row.append(lab, dropdown);
        } else {
          const select = node('select'); select.id = lab.htmlFor;
          options(select, selection.choices.map((v) => [v, v || '(empty)']), value);
          select.addEventListener('change', () => setValue(path, select.value));
          row.append(lab, select);
        }
        host.append(row); continue;
      }
      const complex = Array.isArray(value) || value === null;
      const input = node(complex ? 'textarea' : 'input'); input.id = lab.htmlFor;
      if (typeof value === 'boolean') { input.type = 'checkbox'; input.checked = value; }
      else if (typeof value === 'number') { input.type = 'number'; input.step = 'any'; input.value = value; input.required = true; }
      else input.value = complex ? JSON.stringify(value) : String(value);
      input.addEventListener('input', () => {
        input.setCustomValidity('');
        try {
          let next = typeof value === 'boolean' ? input.checked : typeof value === 'number' ? Number(input.value) : complex ? JSON.parse(input.value) : input.value;
          if (typeof value === 'number' && (!input.value || !Number.isFinite(next))) throw new Error('Enter a finite number');
          if (Array.isArray(value) && !Array.isArray(next)) throw new Error('Enter a JSON array, e.g. ["left", "right"]');
          setValue(path, next);
        } catch (e) { input.setCustomValidity(e.message); }
      });
      row.append(lab, input); host.append(row);
    }
  }
  addFields(values); filterParameters();
}
function filterParameters() {
  const query = $('parameter-search').value.toLowerCase().replaceAll(' ', '_');
  for (const row of $('parameter-fields').children) if (row.dataset.key) row.hidden = !row.dataset.key.includes(query);
}
async function switchEditor(next) {
  if (editor === next) return;
  if (next === 'yaml') {
    if (!$('launch-form').reportValidity()) return;
    $('parameter-yaml').value = values === null ? '' : JSON.stringify(values, null, 2);
  } else {
    values = $('parameter-yaml').value.trim() ? (await api('/api/parameters', {text: $('parameter-yaml').value})).values : null;
  }
  editor = next; renderEditor();
}
function renderState() {
  $('empty').hidden = !!project(); $('workspace').hidden = !project();
  $('project-count').textContent = state.projects.length;
  const signature = JSON.stringify([state.projects, selected]);
  if (signature !== projectSignature) {
    projectSignature = signature;
    $('projects').replaceChildren(...state.projects.map((p) => {
      const button = node('button', 'project-button' + (p.id === selected ? ' selected' : ''));
      button.append(node('span','project-icon','◈'), node('span','',p.name));
      button.setAttribute('aria-current', p.id === selected ? 'page' : 'false');
      button.addEventListener('click',guard(() => chooseProject(p.id))); return button;
    }));
  }
  updateLaunch(); renderHistory();
}
function renderHistory() {
  const runs = state.runs.filter((r) => r.project === selected);
  const signature = JSON.stringify([runs, runId]);
  if (signature === historySignature) return;
  historySignature = signature;
  $('history-count').textContent = `${runs.length} RUN${runs.length === 1 ? '' : 'S'}`;
  $('history').replaceChildren(...runs.map((run) => {
    const button = node('button', 'history-row' + (run.id === runId ? ' selected' : ''));
    const text = node('span','history-text');
    text.append(node('strong','',label(run.mode)), node('small','',`${date(run.started)} · ${run.rig.split('/').pop()}`));
    button.append(node('span','history-icon',run.mode === 'movie' ? '▷' : '↗'), text, node('span',`status ${run.status}`,run.status.toUpperCase()));
    button.addEventListener('click',guard(async () => { runId = run.id; gallerySignature = ''; renderHistory(); await refreshRun(); }));
    return button;
  }));
  if (!runs.length) $('history').append(node('p','history-empty','Your first run starts a history. Configurations, logs and media stay together.'));
}
function outputTab(tab) {
  $('media-panel').hidden = tab !== 'media'; $('console-panel').hidden = tab !== 'console';
  $('media-tab').classList.toggle('selected', tab === 'media'); $('console-tab').classList.toggle('selected', tab === 'console');
}
async function refreshRun() {
  const key = runId;
  const run = key ? await api('/api/runs/' + key) : null;
  if (key !== runId) return;
  $('run-status').textContent = run ? run.status.toUpperCase() : 'READY';
  $('run-status').className = 'status ' + (run?.status || '');
  $('run-info').textContent = run ? `${label(run.mode)} · ${date(run.started)}${run.returncode !== null ? ' · exit ' + run.returncode : ''}${run.error ? ' · ' + run.error : ''}` : '';
  $('stop').hidden = !run || run.id !== state.active;
  $('stop').disabled = run?.status === 'stopping';
  $('stop').textContent = run?.status === 'stopping' ? 'Stopping…' : 'Stop run';
  $('monitor').hidden = !run?.monitor;
  if (run?.monitor) $('monitor').href = run.monitor;
  const consoleEl = $('console'), nearBottom = consoleEl.scrollHeight - consoleEl.scrollTop - consoleEl.clientHeight < 70;
  const log = run?.log || run?.error || (run ? 'Waiting for output…' : 'Output will appear when a run starts.');
  if (consoleEl.textContent !== log) { consoleEl.textContent = log; if (nearBottom) consoleEl.scrollTop = consoleEl.scrollHeight; }
  $('command').textContent = run ? `${run.cwd}\n\n${run.command.map((s) => /[\s"']/.test(s) ? JSON.stringify(s) : s).join(' ')}\n\nSaved in ${run.directory}` : '';
  const artifacts = run?.artifacts || [];
  $('media-count').textContent = artifacts.length;
  $('gallery-empty').hidden = artifacts.length > 0;
  const empty = $('gallery-empty');
  empty.querySelector('h3').textContent = run ? ['running','stopping'].includes(run.status) ? 'Your experiment is working.' : run.status === 'failed' ? 'This run needs attention.' : 'No images or movies in this run.' : 'A closer look at your experiment.';
  empty.querySelector('p').textContent = run ? ['running','stopping'].includes(run.status) ? 'Images appear as they are written. Movies are available when recording finishes. Follow progress in the console.' : run.status === 'failed' ? 'Open the console for the error and the command that produced it.' : 'Session modes write their data to the rig’s data directory. Use a preview script or movie mode to generate media.' : 'Preview images and recorded movies appear here. Choose a mode and start a run to see the results.';
  // Do not replace a playing video on every poll. Defer videos until the
  // encoder closes them; an unfinished MP4 has no readable index yet.
  const visible = artifacts.filter((a) => !a.type.startsWith('video/') || !['running','stopping'].includes(run.status));
  const signature = JSON.stringify([key, visible]);
  if (signature === gallerySignature) return;
  gallerySignature = signature;
  $('gallery-empty').hidden = visible.length > 0;
  $('gallery').replaceChildren(...visible.map((artifact) => {
    const url = `/media/${key}/${artifact.path.split('/').map(encodeURIComponent).join('/')}?token=${encodeURIComponent(token)}&v=${artifact.modified}`;
    const video = artifact.type.startsWith('video/');
    const figure = node('figure','media-card'), media = node(video ? 'video' : 'img');
    media.src = url;
    if (video) { media.controls = true; media.preload = 'metadata'; media.playsInline = true; }
    else { media.alt = artifact.path; media.loading = 'lazy'; media.tabIndex = 0;
      const show = () => { $('large-image').src = url; $('large-image').alt = artifact.path; $('large-caption').textContent = artifact.path; $('image-dialog').showModal(); };
      media.addEventListener('click',show); media.addEventListener('keydown',(e) => { if (e.key === 'Enter') show(); });
    }
    const caption = node('figcaption'), title = node('span','',artifact.path);
    title.append(node('small','media-size',artifact.size > 1048576 ? `${(artifact.size / 1048576).toFixed(1)} MB` : `${Math.ceil(artifact.size / 1024)} KB`));
    const download = node('a','','↓ Save'); download.href = url; download.download = artifact.path.split('/').pop();
    caption.append(title, download); figure.append(media,caption); return figure;
  }));
}
async function refresh() {
  state = await api('/api/state');
  $('connection').textContent = 'Connected to localhost';
  if (connectionError) { error(''); connectionError = false; }
  if (!project() && state.projects.length) await chooseProject(state.projects.find((p) => p.id === localStorage.getItem('alhazen-workspace-project'))?.id || state.projects[0].id);
  else { renderState(); await refreshRun(); }
}
async function poll() {
  try { await refresh(); } catch (e) { $('connection').textContent = 'Connection unavailable'; connectionError = true; error(e.message); }
  setTimeout(poll, 1500);
}
function openProject(edit = false) {
  editProject = edit ? project() : null;
  $('dialog-title').textContent = edit ? 'Project settings' : 'Add an experiment';
  $('project-folder').value = editProject?.path || '';
  $('project-folder').readOnly = edit;
  $('project-python').value = editProject?.python || '';
  $('project-runtime').textContent = editProject ? `Current interpreter: ${editProject.python}` : '';
  $('remove-project').hidden = !edit;
  $('dialog-error').textContent = '';
  $('project-dialog').showModal();
}
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
$('stop').addEventListener('click', guard(async () => { await api('/api/stop', {id: runId}); await refresh(); }));
$('project-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = event.submitter; button.disabled = true;
  try {
    const p = await api('/api/projects', {path: $('project-folder').value, python: $('project-python').value});
    state = await api('/api/state'); $('project-dialog').close(); error(''); await chooseProject(p.id);
  } catch (e) { $('dialog-error').textContent = e.message; } finally { button.disabled = false; }
});
$('remove-project').addEventListener('click', guard(async () => {
  await api('/api/projects/remove', {id: selected}); $('project-dialog').close();
  selected = null; runId = null; await refresh();
}));
$('launch-form').addEventListener('submit', guard(async (event) => {
  event.preventDefault(); if (launching || (usesParameters() && (loadingConfig || loadingSchema)) || state.active) return;
  launching = true; updateLaunch(); error('');
  const mode = $('mode').value;
  try {
    const request = {project: selected, mode, rig: $('rig').value,
      subject: $('subject').value, session: Number($('session').value), seed: Number($('seed').value), trials: Number($('trials').value),
      headless: mode === 'simulate' && $('headless').checked, mouse: mode === 'test' && $('mouse').checked,
      windowed: !['movie'].includes(mode) && !project().scripts.some((s) => s.id === mode) && $('windowed').checked,
      scale: Number($('scale').value), sheet: $('sheet').checked, columns: $('columns').value ? Number($('columns').value) : null,
      clips: $('clips').value.split(',').map((s) => s.trim()).filter(Boolean), script_args: MODES[mode] ? '' : $('script-args').value};
    if (usesParameters() && editor === 'yaml' && $('parameter-yaml').value.trim()) request.parameters_yaml = $('parameter-yaml').value;
    else if (usesParameters() && editor === 'fields' && values !== null) request.parameters = values;
    const run = await api('/api/runs', request); runId = run.id; gallerySignature = ''; outputTab('media'); await refresh();
  } finally { launching = false; updateLaunch(); }
}));
poll();
