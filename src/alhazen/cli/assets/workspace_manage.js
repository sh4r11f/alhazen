/* Alhazen experiment workspace — the management pages: Experiments (the
 * workspace's home), an experiment's General page and its History page.
 *
 * Served beside workspace.js, which owns the shell (sidebar, addresses,
 * polling) and calls this script through window.WorkspaceManage:
 *
 *   renderHome(container, state, helpers)     the Experiments page
 *   showGeneral(container, project, helpers)  General: notes, people, rigs
 *   showHistory(container, project, helpers)  History: launches and sessions
 *   hide()                                    the page was left
 *   leave() -> Promise<boolean>               may the page be left? (asks
 *                                              in place when edits are unsaved)
 *   dirty() -> boolean                        are edits unsaved?
 *
 * Everything shown comes from the server (cli/workspace_manage.py) and goes
 * into the page as text, never as markup: names, notes and log lines are
 * typed by people and written by experiments. Every write names the record
 * revision or file hash it was made against; a 409 answer means someone
 * else changed it first, and the page offers to reload rather than
 * overwriting.
 */
'use strict';

(function () {
  /* The page's helpers from workspace.js (api, node, error, navigate, …). */
  let h = null;
  /* What the open page is about, so a slow answer for a page that has since
   * been left is dropped. */
  let current = {page: null, project: null, epoch: 0};
  /* The forms with unsaved edits, by name. */
  const unsaved = new Set();
  /* The Experiments page's search text, kept across redraws. */
  let homeQuery = '';
  /* History's filter text. */
  let historyQuery = '';
  /* The sessions checked for upload on the History page, "<root>:<run>". */
  const uploadChecked = new Set();
  /* Which Open on the History page is the latest: a session's details
   * arrive after a request, so one opened earlier but answered later must
   * not replace the details opened since. */
  let opened = 0;

  /** An element with a class and text, as workspace.js's node(). */
  function el(tag, className, text) {
    return h.node(tag, className, text);
  }

  let fieldCount = 0;
  /** A labelled field: <label> then the control, wrapped for layout. */
  function field(labelText, control, hint) {
    const box = el('div', 'm-field');
    if (!control.id) control.id = `m-field-${++fieldCount}`;
    const label = el('label', '', labelText);
    label.htmlFor = control.id;
    box.append(label, control);
    if (hint) box.append(el('p', 'm-hint', hint));
    return box;
  }

  function input(value, attributes = {}) {
    const box = el('input');
    box.value = value ?? '';
    for (const [k, v] of Object.entries(attributes)) box.setAttribute(k, v);
    return box;
  }

  /** A <select> of [value, text] pairs, opening on `value`. */
  function select(items, value) {
    const box = el('select');
    for (const [v, text] of items) {
      const o = el('option', '', text);
      o.value = v;
      box.append(o);
    }
    box.value = value ?? '';
    return box;
  }

  function textarea(value, rows = 3) {
    const box = el('textarea');
    box.value = value ?? '';
    box.rows = rows;
    return box;
  }

  function button(text, className, onClick) {
    const b = el('button', className || 'quiet', text);
    b.type = 'button';
    if (onClick) b.addEventListener('click', onClick);
    return b;
  }

  /** A panel of a page: a mono eyebrow, a title, an aside, then content. */
  function panel(eyebrow, title, aside) {
    const box = el('section', 'm-panel');
    const head = el('div', 'm-panel-head');
    const titles = el('div');
    titles.append(el('p', 'm-eyebrow', eyebrow), el('h2', 'm-title', title));
    head.append(titles);
    if (aside) head.append(aside);
    box.append(head);
    return box;
  }

  /** An inline message: 'error', 'ok' or 'note', with an optional action. */
  function message(kind, text, action) {
    const box = el('div', `m-message m-${kind}`);
    box.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    box.append(el('span', '', text));
    if (action) box.append(action);
    return box;
  }

  /** Run a write; on failure put the reason in `where` (with a Reload button
   *  when someone else changed the record first), never in an alert.
   *  Returns the answer, or null when it failed. */
  async function attempt(where, write, reload) {
    where.replaceChildren();
    try {
      return await write();
    } catch (e) {
      const conflict = /changed (elsewhere|on disk|since)/.test(e.message);
      where.replaceChildren(message('error', e.message,
        conflict && reload ? button('Reload', 'quiet', reload) : null));
      return null;
    }
  }

  /** Track a form's unsaved state from its inputs. */
  function watch(form, name) {
    form.addEventListener('input', () => unsaved.add(name));
  }

  function clean(name) {
    unsaved.delete(name);
  }

  /** A table cell for a row's buttons, which go in its `box`: a flex box
   *  inside the cell, since a flex cell would leave the table's layout. */
  function actionCell() {
    const cell = el('td', 'm-actions-cell');
    cell.box = el('div', 'm-row-actions');
    cell.append(cell.box);
    return cell;
  }

  function lamp(kind, text) {
    const box = el('span', `m-lamp m-lamp-${kind}`);
    box.append(el('span', 'm-lamp-dot'), el('span', '', text));
    return box;
  }

  function when(stamp) {
    return stamp ? h.date(stamp) : '—';
  }

  /* ---------------------------------------------------------------- */
  /* Experiments                                                       */
  /* ---------------------------------------------------------------- */

  /** The latest launch of each experiment, from the workspace's state. */
  function lastLaunches(state) {
    const latest = {};
    for (const run of state.runs) {
      if (!latest[run.project] || run.started > latest[run.project].started) {
        latest[run.project] = run;
      }
    }
    return latest;
  }

  function environment(p) {
    const python = (p.python_version || '').split(' ')[0] || '?';
    const parts = [`Python ${python}`, `alhazen ${p.alhazen_version || '?'}`];
    if (p.psychopy_version === null) parts.push('no PsychoPy');
    else if (p.psychopy_version) parts.push(`PsychoPy ${p.psychopy_version}`);
    return parts.join(' · ');
  }

  function experimentRow(p, latest, state) {
    const row = el('article', 'm-exp' + (p.archived ? ' m-exp-archived' : ''));
    const main = el('div', 'm-exp-main');
    const name = h.link('m-exp-name', h.titleOf(p), p.id, 'general');
    const facts = el('p', 'm-exp-path');
    facts.append(el('span', 'm-mono', h.slugOf(p)), el('span', '', p.path));
    main.append(name, facts);
    if (p.meta?.description) main.append(el('p', 'm-exp-description', p.meta.description));
    const env = el('dl', 'm-exp-facts');
    const add = (term, value, title) => {
      const dd = el('dd', '', value);
      if (title) dd.title = title;
      env.append(el('dt', '', term), dd);
    };
    add('Protocol', p.version ? `v${p.version}` : 'unknown', p.version_error || '');
    add('Environment', environment(p), p.python);
    add('Last launch', latest ? `${h.label(latest.mode)} · ${when(latest.started)} · `
      + latest.status : 'none from this workspace');
    const running = !!state.active
      && state.runs.find((r) => r.id === state.active)?.project === p.id;
    const status = el('div', 'm-exp-status');
    if (running) status.append(lamp('run', 'Running'));
    else if (!p.available) status.append(lamp('bad', 'run.py missing'));
    else status.append(lamp('ok', 'Available'));
    if (p.tasks_error || p.title_error) status.append(lamp('warn', 'Needs attention'));
    const actions = el('div', 'm-exp-actions');
    actions.append(
      h.link('m-action', 'General', p.id, 'general'),
      h.link('m-action', 'History', p.id, 'history'),
      h.link('m-action m-action-primary', 'Run ▸', p.id, 'run'),
    );
    if (p.archived) {
      actions.append(button('Restore', 'quiet', async () => {
        try {
          await h.api('/api/manage/archive', {project: p.id, archived: false});
          h.refresh();
        } catch (e) {
          h.error(e.message);
        }
      }));
    }
    row.append(status, main, env, actions);
    return row;
  }

  function renderHome(container, state, helpers) {
    h = helpers;
    const projects = state.projects;
    const latest = lastLaunches(state);
    const toolbar = el('div', 'm-toolbar');
    const search = input(homeQuery, {type: 'search', placeholder: 'Search by name, folder or '
      + 'protocol…', 'aria-label': 'Search experiments'});
    search.className = 'm-search';
    toolbar.append(search, el('span', 'm-count', `${projects.filter((p) => !p.archived).length} `
      + `registered · ${projects.filter((p) => p.archived).length} archived`));
    const list = el('div', 'm-exp-list');
    const archived = el('details', 'm-archive');
    const draw = () => {
      const query = homeQuery.trim().toLowerCase();
      const matches = (p) => !query || [h.titleOf(p), h.slugOf(p), p.path, p.version || '',
        p.meta?.description || ''].some((t) => t.toLowerCase().includes(query));
      const active = projects.filter((p) => !p.archived && matches(p));
      const old = projects.filter((p) => p.archived && matches(p));
      list.hidden = !active.length && !projects.some((p) => !p.archived);
      list.replaceChildren(...active.map((p) => experimentRow(p, latest[p.id], state)));
      if (!active.length && projects.some((p) => !p.archived)) {
        list.append(el('p', 'm-empty', 'No registered experiment matches the search.'));
      }
      archived.hidden = !old.length;
      archived.replaceChildren(el('summary', '', `Archived (${old.length})`),
        el('p', 'm-hint', 'Archived experiments keep their folders, data, people and history; '
          + 'they are left out of the sidebar until restored.'),
        ...old.map((p) => experimentRow(p, latest[p.id], state)));
    };
    search.addEventListener('input', () => {
      homeQuery = search.value;
      draw();
    });
    draw();
    container.replaceChildren(...(projects.length ? [toolbar, list, archived] : []));
  }

  /* ---------------------------------------------------------------- */
  /* General                                                           */
  /* ---------------------------------------------------------------- */

  async function fetchPeople(p) {
    try {
      return await h.api(`/api/manage/people?project=${encodeURIComponent(p.id)}`);
    } catch (e) {
      return {error: e.message, subjects: [], experimenters: [], assigned: []};
    }
  }

  async function showGeneral(container, p, helpers) {
    h = helpers;
    const epoch = current.epoch + 1;
    current = {page: 'general', project: p.id, epoch};
    unsaved.clear();
    container.replaceChildren(el('p', 'm-loading', 'Loading…'));
    const people = await fetchPeople(p);
    if (epoch !== current.epoch) return;
    drawGeneral(container, p, people);
  }

  function drawGeneral(container, p, people) {
    const guardBar = el('div', 'm-leave');
    guardBar.id = 'm-leave';
    guardBar.hidden = true;
    container.replaceChildren(
      guardBar,
      experimentPanel(p),
      subjectsPanel(container, p, people),
      experimentersPanel(container, p, people),
      machinePanel(p),
    );
  }

  /** Reload the people and redraw General, telling the Run page too. */
  async function reloadGeneral(container, p) {
    const people = await fetchPeople(p);
    h.peopleChanged(people);
    if (current.page === 'general' && current.project === p.id) {
      unsaved.clear();
      drawGeneral(container, p, people);
    }
  }

  function experimentPanel(p) {
    const actions = el('div', 'm-panel-actions');
    const confirmBox = el('div', 'inline-confirm');
    confirmBox.hidden = true;
    const note = el('div');
    async function archive(value) {
      const answer = await attempt(note, () => h.api('/api/manage/archive',
        {project: p.id, archived: value}));
      if (answer) {
        confirmBox.hidden = true;
        h.refresh();
        if (value) h.navigate(null, null);
      }
    }
    function confirmArchive() {
      if (p.archived) {
        archive(false);
        return;
      }
      confirmBox.hidden = false;
      const row = el('div', 'inline-confirm-actions');
      row.append(button('Keep it', 'quiet', () => { confirmBox.hidden = true; }),
        button('Archive', 'danger', () => archive(true)));
      confirmBox.replaceChildren(
        el('p', '', 'Archiving keeps the experiment registered with its folder, data, people '
          + 'and history; it only leaves the sidebar and moves to the archive on the '
          + 'Experiments page.'),
        row,
      );
    }
    actions.append(button(p.archived ? 'Restore' : 'Archive…', 'quiet', confirmArchive));
    const box = panel('EXPERIMENT', 'About this experiment', actions);
    const facts = el('dl', 'm-facts');
    const add = (term, value) => facts.append(el('dt', '', term), el('dd', '', value));
    add('Folder', p.path);
    add('Short name', h.slugOf(p));
    add('Protocol version', p.version ? `v${p.version}` : `unknown — ${p.version_error}`);
    add('Interpreter', p.python);
    add('Environment', environment(p));
    add('Experimenter in session.json', p.records_experimenter === true ? 'recorded'
      : p.records_experimenter === false
        ? `not by this alhazen (${p.alhazen_version}); kept with each launch only`
        : 'unknown: open Project settings and save');
    add('Registered', p.registered ? when(p.registered) : 'before the workspace kept the date');
    const form = el('form', 'm-form m-form-wide');
    const description = input(p.meta?.description || '', {maxlength: '4000'});
    const notes = textarea(p.meta?.notes || '', 4);
    const save = button('Save notes', 'primary');
    save.type = 'submit';
    const formActions = el('div', 'm-form-actions');
    formActions.append(save);
    form.append(field('Description', description, 'One line, shown on the Experiments page.'),
      field('Notes', notes), formActions);
    watch(form, 'experiment');
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const answer = await attempt(note, () => h.api('/api/manage/meta', {
        project: p.id, fields: {description: description.value, notes: notes.value}}));
      if (answer) {
        clean('experiment');
        note.replaceChildren(message('ok', 'Saved.'));
        h.refresh();
      }
    });
    box.append(facts, confirmBox, form, note);
    return box;
  }

  /* -- subjects --------------------------------------------------------------- */

  function extraEditor(pairs) {
    const box = el('div', 'm-extra');
    const rows = el('div', 'm-extra-rows');
    const addRow = (name, value) => {
      const row = el('div', 'm-extra-row');
      const key = input(name ?? '', {'aria-label': 'Column', placeholder: 'column'});
      const missing = value === null;
      const cell = input(missing ? '' : value ?? '', {'aria-label': 'Value',
        placeholder: missing ? '(missing)' : 'value'});
      cell.dataset.missing = missing ? '1' : '';
      cell.addEventListener('input', () => { cell.dataset.missing = ''; });
      const remove = button('×', 'quiet m-icon', () => row.remove());
      remove.setAttribute('aria-label', 'Remove column');
      row.append(key, cell, remove);
      rows.append(row);
    };
    for (const [name, value] of pairs || []) addRow(name, value);
    box.append(el('p', 'm-label', 'Other columns'), rows,
      button('＋ Column', 'quiet m-small', () => addRow('', '')));
    box.read = () => [...rows.children].map((row) => {
      const [key, cell] = row.querySelectorAll('input');
      return [key.value.trim(), cell.dataset.missing === '1' && !cell.value ? null : cell.value];
    }).filter(([key]) => key);
    return box;
  }

  function subjectForm(container, p, s, onDone) {
    const form = el('form', 'm-form');
    const code = input(s?.code || '', {required: '', maxlength: '32', autocomplete: 'off',
      placeholder: 'e.g. 01'});
    const initials = input(s?.initials || '', {maxlength: '5', autocomplete: 'off',
      placeholder: 'e.g. HD'});
    // Age in years and sex from a fixed list; both optional, both editable
    // after sessions (each session keeps the values it ran with).
    const age = input(s?.age ?? '', {inputmode: 'decimal', maxlength: '5', autocomplete: 'off',
      placeholder: 'e.g. 27'});
    const sex = select([['', 'Not recorded'], ...h.sexes], s?.sex || '');
    const notes = textarea(s?.notes || '', 2);
    const extra = extraEditor(s?.extra || []);
    if (s?.used) {
      code.readOnly = true;
      if (s.initials) initials.readOnly = true;
    }
    const where = el('div');
    const save = button(s ? 'Save subject' : 'Add subject', 'primary');
    save.type = 'submit';
    const actions = el('div', 'm-form-actions');
    actions.append(button('Cancel', 'quiet', () => { clean('subject'); onDone(false); }), save);
    const row = el('div', 'm-row');
    row.append(
      field('Subject ID', code, s?.used ? 'Fixed: sessions are filed under it.'
        : 'Letters and digits, kept as typed (007 stays 007).'),
      field('Initials', initials, s?.used && s.initials ? 'Fixed: recorded with its sessions.'
        : 'Checked against participants.tsv at every session.'),
      field('Age (years)', age, s?.age_recorded ? `Entered ${s.age_recorded}; a new age is `
        + 'dated today.' : '0 to 120, whole or one decimal. Recorded with each session.'),
      field('Sex', sex, 'Recorded with each session. Prefer not to say is an answer; '
        + 'Not recorded is none.'),
      field('Notes', notes),
    );
    form.append(row, extra, where, actions);
    watch(form, 'subject');
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const fields = {code: code.value, initials: initials.value || null,
        age: age.value.trim() || null, sex: sex.value || null,
        notes: notes.value || null, extra: extra.read()};
      const answer = await attempt(where, () => (s
        ? h.api('/api/manage/people/subject-update', {project: p.id, id: s.id,
          revision: s.revision, fields})
        : h.api('/api/manage/people/subject-add', {project: p.id, fields})),
      () => reloadGeneral(container, p));
      if (answer) {
        clean('subject');
        h.peopleChanged(answer.people);
        onDone(true);
      }
    });
    return form;
  }

  function exportState(people, container, p) {
    const box = el('div', 'm-export');
    const ex = people.export;
    if (!ex || !ex.pending) return box;
    const where = el('div');
    box.append(message('error', 'The records are saved, but the CSV copies are behind '
      + `(revision ${ex.exported_revision} of ${ex.revision})${ex.error ? `: ${ex.error}` : '.'}`,
    button('Write CSV copies again', 'quiet', async () => {
      const answer = await attempt(where, () => h.api('/api/manage/people/export',
        {project: p.id}));
      if (answer) reloadGeneral(container, p);
    })), where);
    return box;
  }

  function subjectsPanel(container, p, people) {
    const aside = el('div', 'm-panel-actions');
    const box = panel('PEOPLE', 'Subjects', aside);
    if (people.error) {
      box.append(message('error', people.error));
      return box;
    }
    const all = people.subjects;
    let showArchived = false;
    const slot = el('div');
    const table = el('div', 'm-table-wrap');
    const tools = el('div', 'm-subtools');
    aside.append(button('＋ Add subject', 'primary', () => {
      slot.replaceChildren(subjectForm(container, p, null, (saved) => {
        slot.replaceChildren();
        if (saved) reloadGeneral(container, p);
      }));
    }));
    const archivedToggle = el('label', 'check');
    const toggle = el('input');
    toggle.type = 'checkbox';
    toggle.addEventListener('change', () => { showArchived = toggle.checked; draw(); });
    archivedToggle.append(toggle, el('span', '', `Show archived (${all.filter(
      (s) => s.status === 'archived').length})`));
    const importer = el('div');
    tools.append(
      button('Import participants.tsv…', 'quiet', () => previewParticipants(importer, container,
        p)),
      button('Read back edited CSV…', 'quiet', () => previewCsv(importer, container, p,
        'subjects')),
      archivedToggle,
    );
    const files = el('p', 'm-files');
    if (people.files) {
      files.append(el('span', 'm-label', 'CSV copy '), el('code', '', people.files.subjects));
    }
    function draw() {
      const shown = all.filter((s) => showArchived || s.status === 'active');
      if (!shown.length) {
        table.replaceChildren(el('p', 'm-empty', all.length
          ? 'Every subject is archived.'
          : 'No subjects yet. Add one, or import the data folders’ participants.tsv.'));
        return;
      }
      const t = el('table', 'm-table');
      const head = el('tr');
      for (const c of ['ID', 'Initials', 'Age', 'Sex', 'Other columns', 'Notes', 'From',
        'Status', '']) {
        head.append(el('th', '', c));
      }
      t.append(head);
      for (const s of shown) {
        const tr = el('tr', s.status === 'archived' ? 'm-archived' : '');
        tr.append(
          el('td', 'm-mono', `sub-${s.code}`),
          el('td', 'm-mono', s.initials || '—'),
          el('td', 'm-mono', s.age ?? '—'),
          el('td', s.sex ? 'm-nowrap' : 'm-mono', s.sex ? h.sexLabel(s.sex) : '—'),
          el('td', 'm-cell-extra', s.extra.map(([k, v]) => `${k}: ${v === null ? '∅' : v}`)
            .join(' · ') || '—'),
          el('td', 'm-cell-notes', s.notes || ''),
          el('td', 'm-cell-from', s.sources.length
            ? s.sources.map((x) => `${x.kind} participants.tsv, line ${x.line}`).join('; ')
            : 'added here'),
        );
        const state = el('td');
        state.append(lamp(s.status === 'active' ? 'ok' : 'off', s.status));
        if (s.used) state.append(el('span', 'm-tag', 'has sessions'));
        const acts = actionCell();
        const where = el('div');
        acts.box.append(
          button('Edit', 'quiet m-small', () => {
            slot.replaceChildren(subjectForm(container, p, s, (saved) => {
              slot.replaceChildren();
              if (saved) reloadGeneral(container, p);
            }));
          }),
          button(s.status === 'active' ? 'Archive' : 'Restore', 'quiet m-small', async () => {
            const answer = await attempt(where, () => h.api(
              '/api/manage/people/subject-status', {project: p.id, id: s.id,
                revision: s.revision, status: s.status === 'active' ? 'archived' : 'active'}),
            () => reloadGeneral(container, p));
            if (answer) reloadGeneral(container, p);
          }),
          where,
        );
        tr.append(state, acts);
        t.append(tr);
      }
      table.replaceChildren(t);
    }
    draw();
    box.append(exportState(people, container, p), tools, importer, slot, table, files);
    return box;
  }

  async function previewParticipants(where, container, p) {
    where.replaceChildren(el('p', 'm-loading', 'Reading participants.tsv…'));
    let plan;
    try {
      plan = await h.api(`/api/manage/participants-plan?project=${encodeURIComponent(p.id)}`);
    } catch (e) {
      where.replaceChildren(message('error', e.message));
      return;
    }
    const box = el('div', 'm-preview');
    box.append(el('h3', 'm-preview-title', 'Import from participants.tsv — preview'),
      el('p', 'm-hint', 'Nothing has changed yet. The files are read, never written; IDs, '
        + 'initials, every other column and the row order are kept. A subject recorded with '
        + 'other initials is never merged.'));
    const files = el('ul', 'm-preview-files');
    for (const f of plan.files) {
      files.append(el('li', '', `${f.kind}: ${f.path} — ${f.error || `${f.rows} rows`}`));
    }
    if (!plan.files.length) {
      files.append(el('li', '', 'This experiment’s rigs name no data folder that exists yet.'));
    }
    box.append(files);
    const names = {new: 'new subject', link: 'link to existing', fill: 'fill missing',
      same: 'already imported', conflict: 'conflict — left alone', error: 'cannot be read'};
    if (plan.rows.length) {
      const t = el('table', 'm-table');
      const head = el('tr');
      for (const c of ['Line', 'ID', 'Initials', 'Age', 'Sex', 'Other columns', 'Will']) {
        head.append(el('th', '', c));
      }
      t.append(head);
      for (const r of plan.rows) {
        const tr = el('tr', `m-plan-${r.action}`);
        tr.append(el('td', 'm-mono', `${r.kind}:${r.line}`),
          el('td', 'm-mono', r.code ? `sub-${r.code}` : '?'),
          el('td', 'm-mono', r.initials || '—'),
          el('td', 'm-mono', r.age ?? '—'),
          el('td', r.sex ? 'm-nowrap' : 'm-mono', r.sex ? h.sexLabel(r.sex) : '—'),
          el('td', 'm-cell-extra', (r.extra || []).map(([k, v]) => `${k}: ${v ?? '∅'}`)
            .join(' · ')),
          el('td', '', names[r.action]
            + (r.action === 'fill' && r.fills?.length ? ` ${r.fills.join(', ')}` : '')
            + (r.reason ? ` — ${r.reason}` : '')));
        t.append(tr);
      }
      const wrap = el('div', 'm-table-wrap');
      wrap.append(t);
      box.append(wrap);
    }
    const result = el('div');
    const actions = el('div', 'm-form-actions');
    const apply = button(plan.changes ? `Import ${plan.changes} change(s)` : 'Nothing to import',
      'primary', async () => {
        const answer = await attempt(result, () => h.api(
          '/api/manage/people/participants-apply', {project: p.id, digest: plan.digest}));
        if (answer) {
          h.peopleChanged(answer.people);
          if (current.page === 'general') drawGeneral(container, p, answer.people);
          const done = message('ok', `Imported ${answer.record.applied} row(s).`
            + (answer.record.backup ? ` Backup first: ${answer.record.backup}` : ''));
          container.querySelector('.m-subtools')?.after?.(done);
        }
      });
    apply.disabled = !plan.changes;
    actions.append(button('Close', 'quiet', () => where.replaceChildren()), apply);
    box.append(result, actions);
    where.replaceChildren(box);
  }

  async function previewCsv(where, container, p, kind) {
    where.replaceChildren(el('p', 'm-loading', 'Reading the CSV copy…'));
    let plan;
    try {
      plan = await h.api(`/api/manage/csv-plan?project=${encodeURIComponent(p.id)}&kind=${kind}`);
    } catch (e) {
      where.replaceChildren(message('error', e.message));
      return;
    }
    const box = el('div', 'm-preview');
    box.append(el('h3', 'm-preview-title', `Read back ${kind}.csv — preview`),
      el('p', 'm-hint', `${plan.path}. Rows edited from an older copy are conflicts; rows `
        + 'deleted in the file are not deleted here; rows without a record_id are new.'));
    const list = el('ul', 'm-preview-files');
    for (const c of plan.changes.filter((x) => x.action !== 'unchanged')) {
      let what;
      if (c.action === 'update') {
        what = `update ${c.record_id}: ${Object.keys(c.fields).join(', ')}`
          + (c.status ? `${Object.keys(c.fields).length ? ', ' : ''}status → ${c.status}` : '');
      } else if (c.action === 'add') {
        what = 'add a new record';
      } else {
        what = `${c.action}: ${c.reason}`;
      }
      list.append(el('li', `m-plan-${c.action}`, `line ${c.line}: ${what}`));
    }
    if (!list.children.length) list.append(el('li', '', 'The file matches the records.'));
    if (plan.absent.length) {
      list.append(el('li', '', `${plan.absent.length} record(s) not in the file: left as `
        + 'they are.'));
    }
    const result = el('div');
    const todo = (plan.counts.add || 0) + (plan.counts.update || 0);
    const blocked = (plan.counts.error || 0) + (plan.counts.conflict || 0);
    const apply = button(blocked ? 'Fix the file first' : todo ? `Apply ${todo} change(s)`
      : 'Nothing to apply', 'primary', async () => {
      const answer = await attempt(result, () => h.api('/api/manage/people/csv-apply',
        {project: p.id, kind, digest: plan.digest}));
      if (answer) {
        h.peopleChanged(answer.people);
        if (current.page === 'general') drawGeneral(container, p, answer.people);
      }
    });
    apply.disabled = !todo || !!blocked;
    const actions = el('div', 'm-form-actions');
    actions.append(button('Close', 'quiet', () => where.replaceChildren()), apply);
    box.append(list, result, actions);
    where.replaceChildren(box);
  }

  /* -- experimenters ----------------------------------------------------------- */

  function experimenterForm(container, p, e, onDone) {
    const form = el('form', 'm-form');
    const name = input(e?.name || '', {required: '', maxlength: '120', autocomplete: 'off'});
    const initials = input(e?.initials || '', {maxlength: '5', autocomplete: 'off'});
    const notes = textarea(e?.notes || '', 2);
    const where = el('div');
    const save = button(e ? 'Save experimenter' : 'Add experimenter', 'primary');
    save.type = 'submit';
    const row = el('div', 'm-row');
    row.append(field('Name', name), field('Initials', initials, 'Optional.'),
      field('Notes', notes));
    const actions = el('div', 'm-form-actions');
    actions.append(button('Cancel', 'quiet', () => { clean('experimenter'); onDone(false); }),
      save);
    form.append(row, where, actions);
    watch(form, 'experimenter');
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const fields = {name: name.value, initials: initials.value || null,
        notes: notes.value || null};
      const answer = await attempt(where, () => (e
        ? h.api('/api/manage/people/experimenter-update', {project: p.id, id: e.id,
          revision: e.revision, fields})
        : h.api('/api/manage/people/experimenter-add', {project: p.id, fields, assign: true})),
      () => reloadGeneral(container, p));
      if (answer) {
        clean('experimenter');
        h.peopleChanged(answer.people);
        onDone(true);
      }
    });
    return form;
  }

  function experimentersPanel(container, p, people) {
    const aside = el('div', 'm-panel-actions');
    const box = panel('PEOPLE', 'Experimenters', aside);
    if (people.error) return box;
    const slot = el('div');
    aside.append(button('＋ New experimenter', 'primary', () => {
      slot.replaceChildren(experimenterForm(container, p, null, (saved) => {
        slot.replaceChildren();
        if (saved) reloadGeneral(container, p);
      }));
    }));
    const rows = people.assigned.filter((a) => a.assignment_status === 'active');
    const mine = new Set(rows.map((a) => a.id));
    const others = people.experimenters.filter((e) => !mine.has(e.id) && e.status === 'active');
    const where = el('div');
    const picker = el('div', 'm-subtools');
    if (others.length) {
      const select = el('select');
      select.setAttribute('aria-label', 'An experimenter of another experiment');
      const prompt = el('option', '', 'Add an existing experimenter…');
      prompt.value = '';
      select.append(prompt);
      for (const e of others) {
        const o = el('option', '', `${e.name}${e.initials ? ` (${e.initials})` : ''} · `
          + e.id.slice(-4));
        o.value = e.id;
        select.append(o);
      }
      picker.append(select, button('Add to this experiment', 'quiet', async () => {
        if (!select.value) return;
        const answer = await attempt(where, () => h.api('/api/manage/people/assign',
          {project: p.id, experimenter: select.value}));
        if (answer) reloadGeneral(container, p);
      }));
    }
    const wrap = el('div', 'm-table-wrap');
    if (!rows.length) {
      wrap.append(el('p', 'm-empty', 'No experimenters for this experiment yet. The Run page '
        + 'asks for one with every registered subject.'));
    } else {
      const t = el('table', 'm-table');
      const head = el('tr');
      for (const c of ['Name', 'Initials', 'Notes', 'Status', '']) head.append(el('th', '', c));
      t.append(head);
      for (const e of rows) {
        const tr = el('tr');
        const cellWhere = el('div');
        const acts = actionCell();
        acts.box.append(
          button('Edit', 'quiet m-small', () => {
            slot.replaceChildren(experimenterForm(container, p, e, (saved) => {
              slot.replaceChildren();
              if (saved) reloadGeneral(container, p);
            }));
          }),
          button('Remove from experiment', 'quiet m-small', async () => {
            const answer = await attempt(cellWhere, () => h.api('/api/manage/people/unassign',
              {project: p.id, experimenter: e.id}));
            if (answer) reloadGeneral(container, p);
          }),
          button(e.status === 'active' ? 'Archive' : 'Restore', 'quiet m-small', async () => {
            const answer = await attempt(cellWhere, () => h.api(
              '/api/manage/people/experimenter-status', {project: p.id, id: e.id,
                revision: e.revision, status: e.status === 'active' ? 'archived' : 'active'}),
            () => reloadGeneral(container, p));
            if (answer) reloadGeneral(container, p);
          }),
          cellWhere,
        );
        const state = el('td');
        state.append(lamp(e.status === 'active' ? 'ok' : 'off', e.status));
        tr.append(el('td', '', e.name), el('td', 'm-mono', e.initials || '—'),
          el('td', 'm-cell-notes', e.notes || ''), state, acts);
        t.append(tr);
      }
      wrap.append(t);
    }
    const files = el('p', 'm-files');
    if (people.files) {
      files.append(el('span', 'm-label', 'CSV copies '), el('code', '', people.files.assigned),
        el('span', '', ' · '), el('code', '', people.files.experimenters));
    }
    const tools = el('div', 'm-subtools');
    const importer = el('div');
    tools.append(button('Read back edited experimenters.csv…', 'quiet', () => previewCsv(importer,
      container, p, 'experimenters')));
    box.append(picker, where, slot, wrap, tools, importer, files);
    return box;
  }

  /* -- this machine's rig configs ------------------------------------------------- */

  /* A config to bring into view (and open) once the panel has drawn: the
   * Run page's "Edit" names the config it runs on (focusRig). */
  let pendingRig = null;

  /** Whether an answer failed because the file changed since it was opened. */
  function stale(e) {
    return e?.status === 409 || /changed (elsewhere|on disk|since)/.test(e?.message || '');
  }

  /** This machine's rig configs (docs/design/machine-rig-configs.md): every
   *  config with its default mark, whether it loads, its sync state and last
   *  save, and the ways to add, edit, rename, make default, delete and see
   *  the history of one; the platform connection and Sync now. Shared and
   *  experiment rigs appear only as a starting point in Add a config. */
  function machinePanel(p) {
    const aside = el('div', 'm-panel-actions');
    const box = panel('THIS MACHINE', 'This machine’s rig configs', aside);
    box.id = 'm-machine';
    box.append(el('p', 'm-hint', 'Every session this workspace launches, of every experiment, runs '
      + 'on one of these configs, chosen on the Run page (the default first). A session can change '
      + 'its config for itself there; change a config here to change it for every later session.'));
    const body = el('div', 'mrc');
    box.append(body);
    let rigs = null;
    const draw = (answer, notice = null) => {
      rigs = answer;
      aside.replaceChildren();
      const sync = button('Sync now', 'quiet', async () => {
        sync.disabled = true;
        sync.textContent = 'Syncing…';
        try {
          draw(await h.api('/api/machine-rigs/sync', {}));
          h.refresh();
        } catch (e) {
          drawNotice(message('error', e.message));
          sync.disabled = false;
          sync.textContent = 'Sync now';
        }
      });
      sync.hidden = !answer.sync?.connected;
      const add = button('Add a config', 'primary', () => openCreate());
      aside.append(sync, add);
      body.replaceChildren();
      const notices = el('div', 'mrc-notices');
      body.append(connectionLine(answer), notices);
      const drawNotice = (node) => notices.replaceChildren(node);
      if (notice) notices.append(notice);
      if (answer.error) body.append(message('error', answer.error));
      const migrated = migrationNote(answer);
      if (migrated) body.append(migrated);
      const create = el('div', 'mrc-create');
      body.append(create);
      const openCreate = () => {
        if (!window.RigEditor) {
          create.replaceChildren(message('note', 'The rig editor did not load; reload the page.'));
          return;
        }
        window.RigEditor.createForm(create, {
          node: h.node, api: h.api, configs: answer.configs || [], first: !(answer.configs || []).length,
          onCancel: () => create.replaceChildren(),
          onDone: async (next) => {
            const made = (next.configs || []).find((c) => c.id === next.created);
            draw(next, message('ok', made ? `Added ${made.name}. It is on the Run page’s list now.`
              : 'Added the config.'));
            h.refresh();
          },
        });
      };
      const list = el('div', 'mrc-list');
      for (const config of answer.configs || []) list.append(configRow(config, answer, p, draw));
      if (!(answer.configs || []).length) {
        body.append(message('note', 'This machine has no rig config yet. Add one to launch sessions.'));
        openCreate();
      } else {
        body.append(list);
      }
      if (pendingRig) {
        const wanted = pendingRig;
        pendingRig = null;
        const row = list.querySelector?.(`[data-config="${wanted}"]`)
          || (list.children || []).find?.((r) => r.dataset?.config === wanted);
        if (row) {
          row.scrollIntoView?.({block: 'start'});
          row.openEditor?.();
        }
      }
    };
    (async () => {
      body.replaceChildren(el('p', 'm-loading', 'Reading this machine’s rig configs…'));
      try {
        draw(await h.api('/api/machine-rigs'));
      } catch (e) {
        body.replaceChildren(message('error', e.message));
      }
    })();
    return box;
  }

  /** The machine and its platform connection, in one line. */
  function connectionLine(rigs) {
    const line = el('div', 'mrc-connection');
    const state = rigs.sync?.state;
    const kind = state === 'synced' ? 'ok' : (state === 'offline' || state === 'error' ? 'warn' : 'idle');
    const word = 'This machine';
    // The machine by name, its lamp beside it; the sentence says the state.
    const machine = el('span', 'mrc-connection-machine');
    const light = el('span', `m-lamp m-lamp-${kind}`);
    light.append(el('span', 'm-lamp-dot'));
    machine.append(light, el('strong', '', rigs.machine?.label || word));
    line.append(machine);
    const text = el('span', 'mrc-connection-text');
    text.textContent = window.RigEditor.connectionText(rigs.sync, when);
    line.append(text);
    return line;
  }

  /** "Your rig from 3.5.0 is now the config <name>", until dismissed. */
  function migrationNote(rigs) {
    const migrated = rigs.migrated;
    if (!migrated) return null;
    const key = `alhazen-mrc-migrated:${rigs.machine?.id || ''}`;
    try {
      if (localStorage.getItem(key)) return null;
    } catch (e) {
      console.warn(`Could not read ${key}: ${e.message}`);
    }
    const config = (rigs.configs || []).find((c) => c.id === migrated.config);
    const box = message('note', `Your rig from ${migrated.from || '3.5.0'} is now the config `
      + `${config?.name || migrated.config}${config?.default ? ', the default' : ''}. Nothing in its `
      + 'file was moved or rewritten.');
    box.append(button('Dismiss', 'quiet m-small', () => {
      try {
        localStorage.setItem(key, '1');
      } catch (e) {
        console.warn(`Could not remember ${key}: ${e.message}`);
      }
      box.remove();
    }));
    return box;
  }

  /** A config's settings in a few lines: what a session on it starts from. */
  function configFacts(config, p) {
    const v = config.values || {};
    const m = v.monitor || {};
    const devices = v.devices || {};
    const facts = el('dl', 'm-facts mrc-facts');
    const add = (term, value) => facts.append(el('dt', '', term), el('dd', '', value ?? '—'));
    add('Screen', `${m.width_px ?? '?'} × ${m.height_px ?? '?'} px · ${m.refresh_rate_hz ?? '?'} Hz · `
      + `${m.width_cm ?? '?'} cm wide, ${m.distance_cm ?? '?'} cm away`);
    const parts = [`display ${v.display?.backend || 'psychopy'}`,
      `eye tracker ${devices.eyetracker?.backend || 'none'}`,
      `reward ${devices.reward ? devices.reward.backend : 'none'}`,
      `sync ${devices.sync ? devices.sync.backend : 'none'}`];
    add('Devices', parts.join(' · '));
    add('Real data', v.real_data === false ? 'refused: a development rig (real_data: false)' : 'collected');
    const root = v.data_root;
    add('Data folder', root ? `${root}${/^([A-Za-z]:|\/|~)/.test(root) ? '' : ` (inside each experiment’s folder: ${p.path}/${root})`}` : 'none');
    const last = (config.saves || []).at(-1);
    if (last) {
      add('Last saved', `${when(last.at)} from the ${last.by}${last.changes?.length ? ` (${last.changes.join(', ')})` : ''}`);
    } else if (config.created) {
      add('Added', `${when(config.created)}${config.origin?.label ? ` from ${config.origin.label}` : ''}`);
    }
    facts.append(el('dt', '', 'File'), el('dd', 'm-mono m-cell-path', config.file || '—'));
    return facts;
  }

  /** One config: name, default mark, whether it loads, its sync state, its
   *  facts, any refused platform edit or conflict, and its actions, with
   *  room under them for the editor, Rename, Delete or History. */
  function configRow(config, rigs, p, redraw) {
    const R = window.RigEditor;
    const row = el('article', 'mrc-config' + (config.default ? ' mrc-default' : ''));
    row.dataset.config = config.id;
    row.id = `m-rig-${config.id}`;
    const head = el('div', 'mrc-config-head');
    const title = el('div', 'mrc-config-title');
    title.append(el('h3', 'mrc-config-name', config.name));
    if (config.default) title.append(el('span', 'mrc-default-mark', 'Default'));
    const states = el('div', 'mrc-config-states');
    states.append(config.problem ? lamp('bad', 'Does not load') : lamp('ok', 'Ready to run'));
    states.append(lamp(R.syncLamp(config), R.syncText(config)));
    head.append(title, states);
    row.append(head);
    if (config.problem) {
      const box = message('error', `It does not load, so the Run page lists it but cannot launch `
        + `it: ${R.problemText(config.problem)}.`);
      if (R.problemText(config.problem) !== config.problem.trim()) {
        const fold = el('details', 'mrc-fold');
        fold.append(el('summary', '', 'The full message'), el('pre', 'm-pre', config.problem));
        box.append(fold);
      }
      row.append(box);
    }
    row.append(configFacts(config, p));
    if (config.legacy && (rigs.configs || []).length > 1) {
      row.append(el('p', 'm-hint', 'A 3.5.0 dashboard on this state folder still runs this config, '
        + 'the one it knows as this machine’s rig.'));
    }
    const done = async (answer, text) => {
      redraw(answer, text ? message('ok', text) : null);
      h.refresh();
    };
    const sync = config.sync || {};
    if (sync.state === 'pending') {
      row.append(message('note', 'The platform has a newer version of this config; this machine '
        + 'takes it at the next sync, if it loads.'));
    }
    if (sync.state === 'rejected' && sync.rejected) row.append(rejectedBlock(config));
    if (sync.state === 'conflict' && sync.conflict) row.append(conflictBlock(config, done));
    const work = el('div', 'mrc-config-work');
    const actions = el('div', 'm-row-actions mrc-config-actions');
    const open = (fn) => () => fn(work);
    row.openEditor = open((where) => configEditor(where, config, done, redraw));
    const edit = button('Edit', 'quiet mrc-edit', row.openEditor);
    actions.append(edit,
      button('Rename', 'quiet', open((where) => renameForm(where, config, rigs, done))),
      button('History', 'quiet', open((where) => historyView(where, config))));
    if (!config.default) {
      actions.append(button('Make default', 'quiet', async () => {
        const answer = await attempt(work, () => h.api('/api/machine-rigs/default', {id: config.id}));
        if (answer) done(answer, `${config.name} is the default now: the Run page opens on it.`);
      }));
    }
    const remove = button('Delete', 'danger', open((where) => deleteConfirm(where, config, done)));
    if (config.default) {
      remove.disabled = true;
      remove.title = 'The default config cannot be deleted: make another one the default first.';
    }
    actions.append(remove);
    row.append(actions, work);
    return row;
  }

  /** A platform edit this machine refused, with its reason and its text. */
  function rejectedBlock(config) {
    const r = config.sync.rejected;
    const box = message('error', `An edit made on the platform${r.revision ? ` (revision ${r.revision})` : ''} `
      + `was refused here: ${r.reason || 'no reason given'}. This machine still runs its own version; `
      + 'edit the config again on either side to settle it.');
    if (r.text) {
      const fold = el('details', 'mrc-fold');
      fold.append(el('summary', '', 'The refused version'), el('pre', 'm-pre', r.text));
      box.append(fold);
    }
    return box;
  }

  /** Both versions of a config changed on both sides, side by side, with
   *  the way to keep one (POST /api/machine-rigs/resolve). */
  function conflictBlock(config, done) {
    const c = config.sync.conflict;
    // §3 names the platform's side platform_text/…; the index keeps it as
    // text/revision/sha256 (§2), which an older answer may pass on as it is.
    const platformText = c.platform_text ?? c.text ?? '';
    const platformRevision = c.platform_revision ?? c.revision ?? '?';
    const box = el('div', 'mrc-conflict');
    box.setAttribute('role', 'group');
    box.setAttribute('aria-label', `Sync conflict for ${config.name}`);
    box.append(el('p', 'mrc-conflict-lead', 'This config was changed here and on the platform since '
      + 'they last agreed. Nothing was overwritten: keep one of the two versions.'));
    const columns = el('div', 'mrc-conflict-columns');
    // Each version line by line, a line the other version does not have
    // marked, so the difference is found without reading both files.
    const lines = (text) => String(text || '').replace(/\n$/, '').split('\n');
    const column = (heading, sub, text, other) => {
      const col = el('div', 'mrc-conflict-column');
      col.append(el('h4', 'mrc-conflict-title', heading));
      if (sub) col.append(el('p', 'm-hint', sub));
      const theirs = new Set(lines(other));
      const pre = el('pre', 'm-pre mrc-conflict-text');
      let differing = 0;
      for (const line of lines(text)) {
        const only = !theirs.has(line);
        if (only) differing++;
        pre.append(el(only ? 'mark' : 'span', only ? 'mrc-only' : '', `${line}\n`));
      }
      col.append(pre);
      col.append(el('p', 'm-hint', differing
        ? `${differing} line${differing === 1 ? '' : 's'} not in the other version, marked.`
        : 'Nothing here that the other version lacks.'));
      return col;
    };
    const by = c.by ? ` by the ${c.by}` : '';
    columns.append(
      column('This machine’s version', 'What this machine runs now.', config.text, platformText),
      column('The platform’s version', `Revision ${platformRevision}${by}${c.at ? `, ${when(c.at)}` : ''}.`,
        platformText, config.text),
    );
    const result = el('div');
    const actions = el('div', 'm-form-actions');
    const keep = (which, text) => button(text, which === 'machine' ? 'primary' : 'quiet', async () => {
      const answer = await attempt(result, () => h.api('/api/machine-rigs/resolve', {id: config.id, keep: which}));
      if (answer) done(answer, which === 'machine' ? `Kept this machine’s ${config.name}.` : `Kept the platform’s ${config.name}.`);
    });
    actions.append(keep('machine', 'Keep this machine’s'), keep('platform', 'Keep the platform’s'));
    box.append(columns, result, actions);
    return box;
  }

  /** Edit a config: the settings by section (the saved config the baseline,
   *  each change marked), or the file as YAML with Check. Saving the form
   *  sends values (the file is written anew, its comments dropped: said
   *  before saving); saving YAML sends the text. Both name the hash read, and
   *  a file changed since is said plainly, with Reload. */
  function configEditor(where, config, done, redraw) {
    const R = window.RigEditor;
    const name = `rig-${config.id}`;
    const box = el('div', 'm-preview mrc-editor');
    const head = el('div', 'mrc-editor-head');
    head.append(el('h3', 'm-preview-title', `Edit ${config.name}`));
    const tabs = el('div', 'mrc-tabs');
    tabs.setAttribute('role', 'group');
    tabs.setAttribute('aria-label', 'Edit as');
    const formTab = button('Settings', 'quiet mrc-tab');
    const yamlTab = button('YAML', 'quiet mrc-tab');
    tabs.append(formTab, yamlTab);
    head.append(tabs);
    const result = el('div');
    // The settings form.
    const formView = el('div', 'mrc-view');
    const draft = {machine: {values: config.values || {}}, current: R.clone(config.values || {})};
    const search = input('', {type: 'search', placeholder: 'Search settings…', 'aria-label': `Filter ${config.name}’s settings`});
    search.className = 'mrc-search';
    const fields = el('div', 'mrc-fields');
    const count = el('span', 'm-count');
    const comments = /(^|\s)#/m.test(config.text || '');
    // RigConfig's schema gives the settings their menus; typed until it is read.
    let schema = null;
    const drawFields = (options = {}) => {
      const changed = R.changes(draft.machine.values, draft.current).length;
      count.textContent = changed ? `${changed} changed` : 'as saved';
      saveForm.disabled = !changed;
      if (changed) unsaved.add(name);
      if (options.quiet) return;
      R.renderFields(fields, draft, {
        node: h.node, schema, query: search.value,
        baseline: 'the saved value', fixed: [], onChange: drawFields,
      });
    };
    const formBar = el('div', 'mrc-editor-bar');
    formBar.append(search, count);
    const saveForm = button('Save settings', 'primary');
    const formActions = el('div', 'm-form-actions');
    formActions.append(button('Close', 'quiet', () => close()), saveForm);
    formView.append(formBar, fields);
    if (comments) {
      formView.append(el('p', 'm-hint mrc-comments', 'Saving from this form writes the file anew, so '
        + 'the comments in it are dropped. Edit it as YAML to keep them.'));
    }
    formView.append(formActions);
    search.addEventListener('input', () => drawFields());
    // The YAML.
    const yamlView = el('div', 'mrc-view');
    const text = textarea(config.text, 18);
    text.className = 'm-code';
    text.spellcheck = false;
    text.setAttribute('aria-label', `${config.name} as YAML`);
    text.addEventListener('input', () => unsaved.add(name));
    const yamlActions = el('div', 'm-form-actions');
    const saveYaml = button('Save YAML', 'primary');
    yamlActions.append(button('Close', 'quiet', () => close()),
      button('Check', 'quiet', async () => {
        const answer = await attempt(result, () => h.api('/api/machine-rigs/check', {id: config.id, text: text.value}));
        if (answer) {
          result.replaceChildren(message('ok', answer.changes?.length
            ? `Valid. Changes: ${answer.changes.map((c) => c.path ?? c).join(', ')}.`
            : 'Valid, and the same as the saved config.'));
        }
      }),
      saveYaml);
    yamlView.append(el('p', 'm-hint', `${config.file || `rig-${config.name}.yaml`}: one complete rig, `
      + 'with no extends and no event names (a session assigns its task’s events to lines on the Run '
      + 'page).'), text, yamlActions);
    const close = () => {
      clean(name);
      where.replaceChildren();
    };
    const save = async (body, what) => {
      result.replaceChildren();
      try {
        const answer = await h.api('/api/machine-rigs/save', {id: config.id, sha256: config.sha256, ...body});
        clean(name);
        where.replaceChildren();
        const saved = answer.saved?.length ? ` (${answer.saved.join(', ')})` : '';
        done(answer, `Saved ${config.name}${what === 'values' ? saved : ''}. Sessions launched on it from now on start from it.`);
      } catch (e) {
        if (stale(e)) {
          result.replaceChildren(message('error', `${config.name} changed since you opened it (here, in `
            + 'another tab or by a sync), so this was not saved. Reload to see the new version; '
            + 'copy anything you want to keep first.', button('Reload', 'quiet', async () => {
            clean(name);
            try {
              redraw(await h.api('/api/machine-rigs'));
            } catch (err) {
              result.replaceChildren(message('error', err.message));
            }
          })));
        } else {
          result.replaceChildren(message('error', e.message));
        }
      }
    };
    saveForm.addEventListener('click', () => save({values: draft.current}, 'values'));
    saveYaml.addEventListener('click', () => save({text: text.value}, 'text'));
    let showing = null;
    const show = (which) => {
      if (showing === which) return;
      const left = showing;
      showing = which;
      formView.hidden = which !== 'form';
      yamlView.hidden = which !== 'yaml';
      formTab.setAttribute('aria-pressed', String(which === 'form'));
      yamlTab.setAttribute('aria-pressed', String(which === 'yaml'));
      result.replaceChildren();
      const pending = left === 'form' ? R.changes(draft.machine.values, draft.current).length
        : (left === 'yaml' ? text.value !== config.text : false);
      if (pending) {
        result.append(message('note', `Your edits in ${left === 'form' ? 'Settings' : 'YAML'} stay there; `
          + 'Save saves only the view it is in.'));
      }
    };
    formTab.addEventListener('click', () => show('form'));
    yamlTab.addEventListener('click', () => show('yaml'));
    box.append(head, formView, yamlView, result);
    where.replaceChildren(box);
    drawFields();
    // A config whose file does not read has no settings to show: YAML only.
    const readable = !!config.values;
    formTab.hidden = !readable;
    if (!readable) {
      tabs.hidden = true;
      show('yaml');
      return;
    }
    show('form');
    if (typeof h.rigSchema === 'function') {
      h.rigSchema().then((answer) => {
        if (!answer || !box.isConnected) return;
        schema = answer;
        drawFields();
      }, (e) => console.warn(`RigConfig's schema could not be read: ${e.message}`));
    }
  }

  /** Rename a config (its file and companions with it). */
  function renameForm(where, config, rigs, done) {
    const R = window.RigEditor;
    const form = el('form', 'm-form');
    const name = input(config.name, {maxlength: '64', autocomplete: 'off', required: ''});
    form.append(field('New name', name, `rig-${config.name}.yaml, its gamma and reward files `
      + 'are renamed with it, and PsychoPy’s monitor of the same name follows. Sessions already run '
      + 'keep the old name in their records.'));
    const result = el('div');
    const actions = el('div', 'm-form-actions');
    const save = button('Rename', 'primary');
    save.type = 'submit';
    actions.append(button('Cancel', 'quiet', () => { clean(`rename-${config.id}`); where.replaceChildren(); }), save);
    form.append(result, actions);
    watch(form, `rename-${config.id}`);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const chosen = name.value.trim();
      const taken = (rigs.configs || []).filter((c) => c.id !== config.id).map((c) => c.name);
      const problem = chosen === config.name ? 'That is its name already.' : R.nameProblem(chosen, taken);
      if (problem) {
        result.replaceChildren(message('error', problem));
        return;
      }
      const answer = await attempt(result, () => h.api('/api/machine-rigs/rename',
        {id: config.id, name: chosen, sha256: config.sha256}));
      if (answer) {
        clean(`rename-${config.id}`);
        done(answer, `Renamed ${config.name} to ${chosen}.`);
      }
    });
    where.replaceChildren(form);
    name.focus?.();
  }

  /** Delete a config, asked in place: its folder moves to trash/. */
  function deleteConfirm(where, config, done) {
    const box = el('div', 'inline-confirm');
    const synced = config.sync?.platform_revision != null;
    box.append(el('p', '', `Delete ${config.name}? Its folder moves to trash/ in this dashboard’s state `
      + 'folder, so nothing is destroyed, and sessions already run keep their own copies of it.'
      + (synced ? ' The platform drops it at the next sync.' : '')));
    const result = el('div');
    const actions = el('div', 'inline-confirm-actions');
    actions.append(button('Keep it', 'quiet', () => where.replaceChildren()),
      button(`Delete ${config.name}`, 'danger', async () => {
        const answer = await attempt(result, () => h.api('/api/machine-rigs/delete',
          {id: config.id, sha256: config.sha256}));
        if (answer) done(answer, `Deleted ${config.name}; its folder is in trash/.`);
      }));
    box.append(result, actions);
    where.replaceChildren(box);
  }

  /** A config's saves and earlier versions; any version's text on View. */
  async function historyView(where, config) {
    where.replaceChildren(el('p', 'm-loading', 'Reading the history…'));
    let history;
    try {
      history = await h.api(`/api/machine-rigs/history?id=${encodeURIComponent(config.id)}`);
    } catch (e) {
      where.replaceChildren(message('error', e.message));
      return;
    }
    const box = el('div', 'm-preview mrc-history');
    const head = el('div', 'mrc-editor-head');
    head.append(el('h3', 'm-preview-title', `${config.name}: history`),
      button('Close', 'quiet', () => where.replaceChildren()));
    box.append(head);
    // Newest first, whatever order the answer lists them in.
    const newest = (a, b) => String(b.at || '').localeCompare(String(a.at || ''));
    const saves = [...(history.saves || [])].sort(newest);
    if (saves.length) {
      const wrap = el('div', 'm-table-wrap');
      const table = el('table', 'm-table');
      const tr = el('tr');
      for (const th of ['Saved', 'From', 'What changed']) tr.append(el('th', '', th));
      table.append(tr);
      for (const s of saves) {
        const r = el('tr');
        r.append(el('td', '', when(s.at)), el('td', '', s.by || '—'),
          el('td', '', s.changes?.length ? s.changes.join(', ') : '—'));
        table.append(r);
      }
      wrap.append(table);
      box.append(wrap);
    } else {
      box.append(el('p', 'm-unknown', 'No saves recorded yet.'));
    }
    const viewer = el('div');
    const versions = [...(history.versions || [])].sort(newest);
    if (versions.length) {
      box.append(el('h4', 'mrc-subtitle', 'Earlier versions'));
      const list = el('ul', 'mrc-versions');
      for (const v of versions) {
        const li = el('li');
        li.append(el('span', '', when(v.at)), el('span', 'm-size', ` ${v.size} B`),
          button('View', 'quiet m-small', async () => {
            try {
              const answer = await h.api(`/api/machine-rigs/version?id=${encodeURIComponent(config.id)}`
                + `&file=${encodeURIComponent(v.file)}`);
              viewer.replaceChildren(el('h4', 'mrc-subtitle', `As it was on ${when(v.at)}`),
                el('pre', 'm-pre', answer.text));
            } catch (e) {
              viewer.replaceChildren(message('error', e.message));
            }
          }));
        list.append(li);
      }
      box.append(list);
    } else {
      box.append(el('p', 'm-unknown', 'No earlier version is kept yet.'));
    }
    box.append(viewer);
    where.replaceChildren(box);
  }

  /** What a session changed of this machine's rig, one line per setting. */
  function rigChangesText(changes) {
    const shown = (changes || []).filter((c) => !c.path.startsWith('devices.sync.event_lines')
      && !c.path.startsWith('display.photodiode.events'));
    if (!shown.length) return 'nothing';
    const value = (v) => (v === undefined ? 'not set' : (typeof v === 'string' ? v : JSON.stringify(v)));
    return shown.map((c) => `${c.path}: ${value(c.absent === 'machine' ? undefined : c.machine)} → `
      + `${value(c.absent === 'session' ? undefined : c.session)}`).join('; ');
  }

  /** A session's event lines in one line: "FIX_ON → Dev1/port0/line0, …". */
  function eventLinesText(events) {
    const lines = Object.entries(events?.event_lines || {}).map(([e, l]) => `${e} → ${l}`);
    const diode = (events?.photodiode_events || []).length
      ? [`photodiode: ${events.photodiode_events.join(', ')}`] : [];
    return [...lines, ...diode].join(', ') || 'none';
  }

  /* ---------------------------------------------------------------- */
  /* History                                                           */
  /* ---------------------------------------------------------------- */

  async function showHistory(container, p, helpers) {
    h = helpers;
    const epoch = current.epoch + 1;
    current = {page: 'history', project: p.id, epoch};
    container.replaceChildren(el('p', 'm-loading', 'Reading the sessions…'));
    let history;
    try {
      history = await h.api(`/api/manage/history?project=${encodeURIComponent(p.id)}`);
    } catch (e) {
      if (epoch === current.epoch) container.replaceChildren(message('error', e.message));
      return;
    }
    if (epoch !== current.epoch) return;
    // The training ladders' summary (workspace_training.js), when the
    // experiment registers one: read beside the sessions, never instead.
    let training = null;
    if ((p.ladders || []).length && window.TrainingLadder) {
      try {
        training = await h.api(`/api/training?project=${encodeURIComponent(p.id)}`);
      } catch (e) {
        training = {ladders: [], error: e.message};
      }
      if (epoch !== current.epoch) return;
    }
    drawHistory(container, p, history, training);
  }

  function experimenterText(x) {
    if (!x || x.recorded === false || !x.name) return 'not recorded';
    return x.name;
  }

  /**
   * Begin showing something in the History page's detail box (#history-detail,
   * above the two lists): put `first` there (the details, or a loading line)
   * and bring the box to the top of the window, because Open is pressed far
   * down a list and the details appear out of sight. Called once per Open
   * (and per link inside the details that opens another), never by a poll or
   * the filter, so the page moves only when the reader asked. The page
   * scrolls as a whole and the sidebar in its own column, which is not an
   * ancestor of the box, so scrollIntoView moves the page and leaves the
   * sidebar where it was. Returns a ticket for finish().
   */
  function begin(detail, first) {
    opened += 1;
    detail.replaceChildren(first);
    // A jump, not an animation: from the end of a long list a smooth scroll
    // takes seconds, and the reader asked to see the details now.
    detail.scrollIntoView({block: 'start', behavior: 'auto'});
    return {ticket: opened, epoch: current.epoch};
  }

  /** Finish what begin() started, unless another Open (or leaving the page)
   *  came since: put `content` in the box (null when begin() already put the
   *  details there) and give the box keyboard focus without scrolling again,
   *  so Tab continues from the details. False when it was stale. */
  function finish(detail, started, content) {
    if (started.ticket !== opened || started.epoch !== current.epoch) return false;
    if (content !== null) detail.replaceChildren(content);
    detail.tabIndex = -1;
    detail.focus({preventScroll: true});
    return true;
  }

  function drawHistory(container, p, history, training = null) {
    const toolbar = el('div', 'm-toolbar');
    const search = input(historyQuery, {type: 'search', placeholder: 'Filter by subject, '
      + 'experimenter, task, rig or date…', 'aria-label': 'Filter sessions'});
    search.className = 'm-search';
    toolbar.append(search, button('Refresh', 'quiet', () => showHistory(container, p, h)));
    const sessionsBox = panel('DATA FOLDERS', 'Sessions', el('span', 'm-count',
      `${history.sessions.length}`));
    const launchesBox = panel('THIS WORKSPACE', 'Launches', el('span', 'm-count',
      `${history.launches.length}`));
    const detail = el('div', 'm-detail');
    detail.id = 'history-detail';
    const notes = el('div');
    for (const root of history.missing) {
      notes.append(message('note', `The ${root.kind} data folder ${root.path} does not exist yet `
        + `(${root.rigs.join(', ')}).`));
    }
    for (const problem of history.problems) notes.append(message('note', problem));
    const sessionTable = el('div', 'm-table-wrap');
    const launchTable = el('div', 'm-table-wrap');
    // Upload to the archive: the checked sessions, or every session listed
    // (workspace_upload.js draws the bar and runs the upload).
    const uploadBar = el('div', 'm-upload');
    const grouped = (sessions) => {
      const byRoot = new Map();
      for (const s of sessions) {
        if (!byRoot.has(s.root)) byRoot.set(s.root, []);
        byRoot.get(s.root).push(s.id);
      }
      return [...byRoot].map(([root, runs]) => ({root, runs}));
    };
    let batch = null;
    const selection = () => {
      const known = new Set(history.sessions.map((s) => `${s.root}:${s.id}`));
      for (const key of [...uploadChecked]) if (!known.has(key)) uploadChecked.delete(key);
      const checked = history.sessions.filter((s) => uploadChecked.has(`${s.root}:${s.id}`));
      return {
        count: checked.length,
        groups: grouped(checked),
        // Everything in every data folder of the experiment, sessions or not.
        all: history.roots.map((r) => ({root: r.id, all: true})),
        allLabel: history.roots.length === 1 ? 'the whole data folder'
          : `all ${history.roots.length} data folders`,
        // When an upload ends, read the history again: the Archive column
        // shows the receipts it wrote.
        after: () => showHistory(container, p, h),
      };
    };
    const changed = () => batch?.draw();
    const draw = () => {
      const q = historyQuery.trim().toLowerCase();
      const hit = (...texts) => !q || texts.some((t) => String(t ?? '').toLowerCase()
        .includes(q));
      const sessions = history.sessions.filter((s) => hit(s.subject, s.initials, s.task, s.rig,
        s.date, s.mode, experimenterText(s.experimenter), s.id));
      changed();
      sessionTable.replaceChildren(sessions.length ? sessionRows(sessions, history, p, detail,
        changed)
        : el('p', 'm-empty', history.sessions.length ? 'No session matches the filter.'
          : 'No session folders in this experiment’s data folders yet.'));
      const launches = history.launches.filter((l) => hit(l.subject, l.initials, l.task,
        l.parameter_set, l.rig, l.started, l.mode, l.status, experimenterText(l.experimenter)));
      launchTable.replaceChildren(launches.length ? launchRows(launches, history, p, detail)
        : el('p', 'm-empty', history.launches.length ? 'No launch matches the filter.'
          : 'Nothing launched from this workspace yet.'));
    };
    search.addEventListener('input', () => { historyQuery = search.value; draw(); });
    draw();
    if (window.ArchiveUpload && history.sessions.length) {
      batch = ArchiveUpload.mountBatch(uploadBar, {api: h.api, node: h.node, project: p.id},
        selection);
      sessionsBox.append(uploadBar);
    }
    sessionsBox.append(sessionTable);
    launchesBox.append(launchTable);
    const trainingBox = training && window.TrainingLadder
      ? TrainingLadder.historyPanel(training, {node: h.node, panel})
      : null;
    container.replaceChildren(toolbar, notes, detail,
      ...(trainingBox ? [trainingBox] : []), sessionsBox, launchesBox);
  }

  function sessionRows(sessions, history, p, detail, changed) {
    const t = el('table', 'm-table m-history');
    const head = el('tr');
    // The first column checks every listed session at once.
    const all = el('input');
    all.type = 'checkbox';
    all.setAttribute('aria-label', 'Select every listed session for upload');
    const keyOf = (s) => `${s.root}:${s.id}`;
    all.checked = sessions.every((s) => uploadChecked.has(keyOf(s)));
    const ticks = [];
    all.addEventListener('change', () => {
      for (const s of sessions) {
        if (all.checked) uploadChecked.add(keyOf(s)); else uploadChecked.delete(keyOf(s));
      }
      for (const tick of ticks) tick.checked = all.checked;
      changed();
    });
    const allCell = el('th', 'm-check');
    allCell.append(all);
    head.append(allCell);
    for (const c of ['Date', 'Subject', 'Experimenter', 'Task', 'Rig', 'Mode', 'Trials',
      'Folder', 'Archive', '']) {
      head.append(el('th', '', c));
    }
    t.append(head);
    for (const s of sessions) {
      const tr = el('tr');
      const tick = el('input');
      tick.type = 'checkbox';
      tick.checked = uploadChecked.has(keyOf(s));
      tick.setAttribute('aria-label', `Select ${s.id} for upload`);
      tick.addEventListener('change', () => {
        if (tick.checked) uploadChecked.add(keyOf(s)); else uploadChecked.delete(keyOf(s));
        all.checked = sessions.every((x) => uploadChecked.has(keyOf(x)));
        changed();
      });
      ticks.push(tick);
      const tickCell = el('td', 'm-check');
      tickCell.append(tick);
      const archive = el('td');
      if (window.ArchiveUpload) archive.append(ArchiveUpload.stateCell({node: h.node}, s.upload));
      tr.append(tickCell);
      const acts = actionCell();
      acts.box.append(button('Open', 'quiet m-small', () => sessionDetail(detail, s, history, p)));
      const known = s.experimenter.recorded && s.experimenter.name;
      tr.append(
        el('td', 'm-mono', s.date || '—'),
        el('td', 'm-mono', `sub-${s.subject}${s.initials ? ` · ${s.initials}` : ''}`
          + ` · ses ${s.session}`),
        el('td', known ? '' : 'm-unknown', experimenterText(s.experimenter)),
        el('td', '', s.task || '—'),
        el('td', 'm-mono', s.rig_name || s.rig || '—'),
        el('td', '', s.mode ? h.label(s.mode) : `? (${s.root_kind})`),
        el('td', 'm-mono', s.trials === null ? '—'
          : `${s.trials_counted === 'lines' ? '≈' : ''}${s.trials}`),
        el('td', 'm-mono m-cell-path', `${s.root_kind} · ${s.id}`),
        archive,
        acts,
      );
      t.append(tr);
    }
    return t;
  }

  function launchRows(launches, history, p, detail) {
    const t = el('table', 'm-table m-history');
    const head = el('tr');
    for (const c of ['Started', 'What', 'Subject', 'Experimenter', 'Rig', 'Status', '']) {
      head.append(el('th', '', c));
    }
    t.append(head);
    for (const l of launches) {
      const tr = el('tr');
      const stage = l.training
        ? `stage ${l.training.stage}${l.training.rehearse ? ' (rehearsal)' : ''}` : null;
      const what = stage || l.parameter_set || l.task
        ? `${h.label(l.mode)} · ${stage || l.parameter_set || l.task}` : h.label(l.mode);
      const subject = l.subject ? `sub-${l.subject}${l.initials ? ` · ${l.initials}` : ''}` : '—';
      const status = el('td');
      let kind = 'off';
      if (l.active) kind = 'run';
      else if (l.status === 'completed') kind = 'ok';
      else if (['failed', 'killed', 'interrupted'].includes(l.status)) kind = 'bad';
      status.append(lamp(kind, l.status));
      const acts = actionCell();
      acts.box.append(button('Open', 'quiet m-small', () => launchDetail(detail, l, history, p)));
      tr.append(el('td', 'm-mono', when(l.started)), el('td', '', what),
        el('td', 'm-mono', subject),
        el('td', l.experimenter ? '' : 'm-unknown', experimenterText(l.experimenter)),
        el('td', 'm-mono', l.rig_name || l.rig || '—'), status, acts);
      t.append(tr);
    }
    return t;
  }

  function textViewer(title, answer) {
    const box = el('div', 'm-text');
    let note = '';
    if (answer.truncated) note = answer.tail ? ' (last part)' : ' (first part)';
    box.append(el('p', 'm-label', `${title}${note}`));
    const pre = el('pre', 'm-pre', answer.text);
    pre.tabIndex = 0;
    box.append(pre);
    return box;
  }

  function downloadLink(p, s, name) {
    const a = el('a', 'm-file', name);
    const query = new URLSearchParams({project: p.id, root: s.root, run: s.id, name,
      token: h.token});
    a.href = `/data/download?${query}`;
    a.setAttribute('download', name.split('/').pop());
    a.rel = 'noreferrer';
    return a;
  }

  async function sessionDetail(detail, s, history, p) {
    const started = begin(detail, el('p', 'm-loading', 'Opening the session…'));
    const where = `project=${encodeURIComponent(p.id)}&root=${s.root}`
      + `&run=${encodeURIComponent(s.id)}`;
    let run;
    try {
      run = await h.api(`/api/data/run?${where}`);
    } catch (e) {
      finish(detail, started, message('error', e.message));
      return;
    }
    const box = panel('SESSION', `sub-${s.subject} · ses ${s.session} · run ${s.run}`,
      button('Close', 'quiet', () => detail.replaceChildren()));
    const facts = el('dl', 'm-facts');
    const add = (term, value) => facts.append(el('dt', '', term), el('dd', '', value ?? '—'));
    add('Folder', run.path);
    add('Date', s.date);
    add('Task', s.task);
    add('Mode', s.mode ? h.label(s.mode) : `not recorded (${s.root_kind} folder)`);
    const sessionConfig = s.rig_config ?? s.rig_origin?.rig_config ?? s.rig_origin?.config ?? null;
    add('Rig', sessionConfig ? `${s.rig_name || s.rig} (this machine’s rig config)`
      : (s.rig_origin ? `${s.rig} (this machine’s rig)` : s.rig));
    if (s.rig_origin) {
      add('Rig changes', rigChangesText(s.rig_origin.changes));
      add('Event lines', eventLinesText(s.rig_origin.events));
    }
    add('Experimenter', experimenterText(s.experimenter));
    add('Age, sex', s.demographics?.recorded
      ? (h.demographicsText(s.demographics) || 'not recorded (session.json says none)')
      : 'not recorded (session.json from before the fields)');
    add('Launched', s.launch ? 'from this workspace'
      : 'not from this workspace, or before it kept launch records');
    box.append(facts);
    for (const problem of run.problems) box.append(message('note', problem));
    // What the session paid through the reward line (a monkey's), trial by
    // trial and in total (workspace_juice.js); nothing for one that paid none.
    if (run.juice && run.juice.form === 'juice' && window.JuiceChart) {
      box.append(JuiceChart.render(run.juice, {node: h.node}));
    } else if (run.juice && run.juice.form === 'empty') {
      box.append(el('p', 'm-unknown', 'The reward line was open; no juice was delivered.'));
    }
    const actions = el('div', 'm-subtools');
    const viewer = el('div');
    if (run.page) {
      actions.append(button('Open saved live monitor ↗', 'primary', async () => {
        try {
          const answer = await h.api(`/api/data/page?${where}`
            + `&name=${encodeURIComponent(run.page)}`);
          // noopener: the saved page gets neither this tab nor its storage.
          window.open(answer.url, '_blank', 'noopener');
        } catch (e) {
          viewer.replaceChildren(message('error', e.message));
        }
      }));
    } else {
      actions.append(el('span', 'm-unknown', 'No saved live monitor page in this folder.'));
    }
    for (const name of run.texts) {
      actions.append(button(name, 'quiet', async () => {
        try {
          const answer = await h.api(`/api/data/text?${where}&name=${encodeURIComponent(name)}`);
          viewer.replaceChildren(textViewer(name, answer));
        } catch (e) {
          viewer.replaceChildren(message('error', e.message));
        }
      }));
    }
    if (s.launch) {
      actions.append(button('Its launch', 'quiet', () => {
        const launch = history.launches.find((l) => l.id === s.launch);
        if (launch) launchDetail(detail, launch, history, p);
      }));
    }
    const files = el('details', 'm-files-list');
    files.append(el('summary', '', `Files (${run.files.length}${run.files_capped ? '+' : ''})`));
    const list = el('ul');
    for (const f of run.files) {
      const li = el('li');
      li.append(downloadLink(p, s, f.name), el('span', 'm-size', ` ${f.size} B`));
      list.append(li);
    }
    files.append(list);
    box.append(actions, viewer, files);
    finish(detail, started, box);
  }

  function launchDetail(detail, l, history, p) {
    const box = panel('LAUNCH', `${h.label(l.mode)} · ${when(l.started)}`,
      button('Close', 'quiet', () => detail.replaceChildren()));
    const facts = el('dl', 'm-facts');
    const add = (term, value) => facts.append(el('dt', '', term), el('dd', '', value ?? '—'));
    add('Status', l.status + (l.active ? ' (in progress)' : ''));
    add('Task', l.parameter_set ? `${l.parameter_set}${l.task ? ` (${l.task})` : ''}` : l.task);
    if (l.rig_config) add('Rig', `${l.rig_name || l.rig} (this machine’s rig config)`);
    else add('Rig', l.rig_source === 'machine' ? `${l.rig} (this machine’s rig)` : l.rig);
    if (l.rig_source === 'machine') {
      add('Rig changes', rigChangesText(l.rig_changes));
      if (l.rig_events_source?.label) add('Event lines from', l.rig_events_source.label);
    }
    let subject = null;
    if (l.subject) {
      subject = `sub-${l.subject}${l.initials ? ` · ${l.initials}` : ''}`
        + (l.identity?.subject?.record_id ? ' (registered)' : ' (typed)');
    }
    add('Subject', subject);
    const ageSex = h.demographicsText(l.identity?.subject);
    if (l.identity?.subject) {
      add('Age, sex', ageSex
        ? ageSex + (l.demographics_recorded_in === 'session.json' ? ' — also in session.json'
          : ' — kept with the launch only')
        : 'not recorded');
    }
    let who = experimenterText(l.experimenter);
    if (l.experimenter) {
      who += l.experimenter_recorded_in === 'session.json' ? ' — also in session.json'
        : ' — kept with the launch only';
    }
    add('Experimenter', who);
    add('Seed', l.seed);
    add('Finished', l.finished ? when(l.finished) : null);
    box.append(facts);
    const viewer = el('div');
    const actions = el('div', 'm-subtools');
    for (const name of l.files) {
      actions.append(button(name, 'quiet', async () => {
        try {
          const answer = await h.api(`/api/manage/launch-text?project=${encodeURIComponent(p.id)}`
            + `&run=${l.id}&name=${encodeURIComponent(name)}`);
          viewer.replaceChildren(textViewer(name, answer));
        } catch (e) {
          viewer.replaceChildren(message('error', e.message));
        }
      }));
    }
    actions.append(button('Show on the Run page', 'quiet', () => h.viewRun(l.id)));
    if (l.session_folder) {
      actions.append(button('Its session folder', 'quiet', () => {
        const s = history.sessions.find((x) => x.root === l.session_folder.root
          && x.id === l.session_folder.run);
        if (s) sessionDetail(detail, s, history, p);
      }));
    } else {
      actions.append(el('span', 'm-unknown', 'No session folder identified for this launch.'));
    }
    box.append(actions, viewer);
    finish(detail, begin(detail, box), null);
  }

  /* ---------------------------------------------------------------- */
  /* Leaving                                                           */
  /* ---------------------------------------------------------------- */

  function hide() {
    current = {page: null, project: null, epoch: current.epoch + 1};
    unsaved.clear();
  }

  /** May the page be left? Yes at once with nothing unsaved; otherwise the
   *  bar at the top of the page asks, and the answer resolves. */
  function leave() {
    if (!unsaved.size) return Promise.resolve(true);
    const bar = document.getElementById('m-leave');
    if (!bar) return Promise.resolve(true);
    return new Promise((resolve) => {
      bar.hidden = false;
      const stay = button('Stay', 'quiet', () => { bar.hidden = true; resolve(false); });
      const discard = button('Discard and leave', 'danger', () => {
        bar.hidden = true;
        unsaved.clear();
        resolve(true);
      });
      bar.replaceChildren(el('span', '', 'You have unsaved changes on this page.'), stay,
        discard);
      stay.focus?.();
    });
  }

  window.WorkspaceManage = {
    renderHome, showGeneral, showHistory, hide, leave,
    dirty: () => unsaved.size > 0,
    /* Bring this machine's rig config `id` into view, its editor open, the
     * next time the General page draws (the Run page's Edit). */
    focusRig: (id) => { pendingRig = id; },
  };
}());
