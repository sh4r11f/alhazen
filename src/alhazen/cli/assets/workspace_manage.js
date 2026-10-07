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
      rigsPanel(p),
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
      field('Notes', notes),
    );
    form.append(row, extra, where, actions);
    watch(form, 'subject');
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const fields = {code: code.value, initials: initials.value || null,
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
      for (const c of ['ID', 'Initials', 'Other columns', 'Notes', 'From', 'Status', '']) {
        head.append(el('th', '', c));
      }
      t.append(head);
      for (const s of shown) {
        const tr = el('tr', s.status === 'archived' ? 'm-archived' : '');
        tr.append(
          el('td', 'm-mono', `sub-${s.code}`),
          el('td', 'm-mono', s.initials || '—'),
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
    const names = {new: 'new subject', link: 'link to existing', fill: 'fill missing initials',
      same: 'already imported', conflict: 'conflict — left alone', error: 'cannot be read'};
    if (plan.rows.length) {
      const t = el('table', 'm-table');
      const head = el('tr');
      for (const c of ['Line', 'ID', 'Initials', 'Other columns', 'Will']) {
        head.append(el('th', '', c));
      }
      t.append(head);
      for (const r of plan.rows) {
        const tr = el('tr', `m-plan-${r.action}`);
        tr.append(el('td', 'm-mono', `${r.kind}:${r.line}`),
          el('td', 'm-mono', r.code ? `sub-${r.code}` : '?'),
          el('td', 'm-mono', r.initials || '—'),
          el('td', 'm-cell-extra', (r.extra || []).map(([k, v]) => `${k}: ${v ?? '∅'}`)
            .join(' · ')),
          el('td', '', names[r.action] + (r.reason ? ` — ${r.reason}` : '')));
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

  /* -- rigs ----------------------------------------------------------------------- */

  function rigsPanel(p) {
    const aside = el('div', 'm-panel-actions');
    const box = panel('MACHINES', 'Rigs', aside);
    const editor = el('div');
    aside.append(button('＋ New rig', 'primary', () => rigEditor(editor, p, null)));
    box.append(el('p', 'm-hint', 'Rigs stay YAML files: the experiment’s own in configs/, and '
      + 'the shared rigs its alhazen ships, read only. Change a shared rig by making a local rig '
      + 'that extends it. Calibration and gamma results are written by Measure rig, not here.'));
    const t = el('table', 'm-table');
    const head = el('tr');
    for (const c of ['Rig', 'Whose', 'Builds on', 'File', '']) head.append(el('th', '', c));
    t.append(head);
    for (const r of p.rigs) {
      const tr = el('tr', r.shadowed ? 'm-archived' : '');
      const acts = actionCell();
      const path = r.source === 'alhazen' ? `alhazen/${r.name}` : r.path;
      acts.box.append(button(r.source === 'alhazen' ? 'View' : 'Edit', 'quiet m-small',
        () => rigEditor(editor, p, path)));
      if (r.source === 'alhazen' && !r.shadowed) {
        acts.box.append(button('Make local', 'quiet m-small', () => rigEditor(editor, p, null,
          r.name)));
      }
      tr.append(
        el('td', 'm-mono', r.source === 'alhazen' ? `alhazen/${r.name}`
          : `${h.slugOf(p)}/${r.name}`),
        el('td', '', r.source === 'alhazen' ? (r.shadowed ? 'shared (hidden by local)'
          : 'shared') : 'experiment'),
        el('td', 'm-mono', r.error ? `cannot be read: ${r.error}` : r.extends || '—'),
        el('td', 'm-mono m-cell-path', r.source === 'alhazen' ? 'alhazen installation'
          : r.path),
        acts,
      );
      t.append(tr);
    }
    const wrap = el('div', 'm-table-wrap');
    wrap.append(t);
    if (p.rigs_note) box.append(message('note', p.rigs_note));
    box.append(wrap, editor);
    return box;
  }

  async function rigEditor(where, p, path, base = null) {
    where.replaceChildren(el('p', 'm-loading', 'Opening…'));
    let file = null;
    if (path) {
      try {
        file = await h.api(`/api/manage/rig-file?project=${encodeURIComponent(p.id)}`
          + `&path=${encodeURIComponent(path)}`);
      } catch (e) {
        where.replaceChildren(message('error', e.message));
        return;
      }
    }
    const box = el('div', 'm-preview');
    const editable = !file || file.editable;
    box.append(el('h3', 'm-preview-title', file
      ? `${file.path}${editable ? '' : ' (read only)'}` : 'New rig'));
    if (file) box.append(el('p', 'm-hint m-mono', file.file));
    const form = el('form', 'm-form m-form-wide');
    const name = input(base || '', {maxlength: '64', placeholder: 'e.g. lab2'});
    let template = 'extends: laptop\n';
    if (base) {
      template = `# A local rig for this experiment: alhazen's shared '${base}', with the lines\n`
        + `# below merged over it. Change only what differs on this machine.\nextends: ${base}\n`;
    }
    const text = textarea(file ? file.text : template, 14);
    text.className = 'm-code';
    text.spellcheck = false;
    text.readOnly = !editable;
    if (!file) {
      form.append(field('Name', name, 'Saved as configs/rig-<name>.yaml, never over an '
        + 'existing file.'));
    }
    form.append(field('YAML', text));
    const result = el('div');
    const actions = el('div', 'm-form-actions');
    actions.append(button('Close', 'quiet', () => { clean('rig'); where.replaceChildren(); }));
    if (editable) {
      actions.append(button('Check', 'quiet', async () => {
        const answer = await attempt(result, () => h.api('/api/manage/rig-check',
          {project: p.id, text: text.value, path: file?.path}));
        if (answer) {
          result.replaceChildren(message('ok', `Valid${answer.extends
            ? `, building on alhazen/${answer.extends}` : ''}.`));
        }
      }));
      const save = button('Save rig', 'primary');
      save.type = 'submit';
      actions.append(save);
    }
    form.append(result, actions);
    watch(form, 'rig');
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const body = file
        ? {project: p.id, path: file.path, text: text.value, sha256: file.sha256}
        : {project: p.id, name: name.value, text: text.value};
      const answer = await attempt(result, () => h.api('/api/manage/rig-save', body),
        () => rigEditor(where, p, file?.path || null, base));
      if (answer) {
        clean('rig');
        file = answer;
        h.refresh();
        result.replaceChildren(message('ok', `Saved ${answer.path}.`));
      }
    });
    box.append(form);
    where.replaceChildren(box);
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
    drawHistory(container, p, history);
  }

  function experimenterText(x) {
    if (!x || x.recorded === false || !x.name) return 'not recorded';
    return x.name;
  }

  function drawHistory(container, p, history) {
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
    const draw = () => {
      const q = historyQuery.trim().toLowerCase();
      const hit = (...texts) => !q || texts.some((t) => String(t ?? '').toLowerCase()
        .includes(q));
      const sessions = history.sessions.filter((s) => hit(s.subject, s.initials, s.task, s.rig,
        s.date, s.mode, experimenterText(s.experimenter), s.id));
      sessionTable.replaceChildren(sessions.length ? sessionRows(sessions, history, p, detail)
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
    sessionsBox.append(sessionTable);
    launchesBox.append(launchTable);
    container.replaceChildren(toolbar, notes, detail, sessionsBox, launchesBox);
  }

  function sessionRows(sessions, history, p, detail) {
    const t = el('table', 'm-table m-history');
    const head = el('tr');
    for (const c of ['Date', 'Subject', 'Experimenter', 'Task', 'Rig', 'Mode', 'Trials',
      'Folder', '']) {
      head.append(el('th', '', c));
    }
    t.append(head);
    for (const s of sessions) {
      const tr = el('tr');
      const acts = actionCell();
      acts.box.append(button('Open', 'quiet m-small', () => sessionDetail(detail, s, history, p)));
      const known = s.experimenter.recorded && s.experimenter.name;
      tr.append(
        el('td', 'm-mono', s.date || '—'),
        el('td', 'm-mono', `sub-${s.subject}${s.initials ? ` · ${s.initials}` : ''}`
          + ` · ses ${s.session}`),
        el('td', known ? '' : 'm-unknown', experimenterText(s.experimenter)),
        el('td', '', s.task || '—'),
        el('td', 'm-mono', s.rig || '—'),
        el('td', '', s.mode ? h.label(s.mode) : `? (${s.root_kind})`),
        el('td', 'm-mono', s.trials === null ? '—'
          : `${s.trials_counted === 'lines' ? '≈' : ''}${s.trials}`),
        el('td', 'm-mono m-cell-path', `${s.root_kind} · ${s.id}`),
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
      const what = l.parameter_set || l.task
        ? `${h.label(l.mode)} · ${l.parameter_set || l.task}` : h.label(l.mode);
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
        el('td', 'm-mono', l.rig || '—'), status, acts);
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
    detail.replaceChildren(el('p', 'm-loading', 'Opening the session…'));
    const where = `project=${encodeURIComponent(p.id)}&root=${s.root}`
      + `&run=${encodeURIComponent(s.id)}`;
    let run;
    try {
      run = await h.api(`/api/data/run?${where}`);
    } catch (e) {
      detail.replaceChildren(message('error', e.message));
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
    add('Rig', s.rig);
    add('Experimenter', experimenterText(s.experimenter));
    add('Launched', s.launch ? 'from this workspace'
      : 'not from this workspace, or before it kept launch records');
    box.append(facts);
    for (const problem of run.problems) box.append(message('note', problem));
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
    detail.replaceChildren(box);
  }

  function launchDetail(detail, l, history, p) {
    const box = panel('LAUNCH', `${h.label(l.mode)} · ${when(l.started)}`,
      button('Close', 'quiet', () => detail.replaceChildren()));
    const facts = el('dl', 'm-facts');
    const add = (term, value) => facts.append(el('dt', '', term), el('dd', '', value ?? '—'));
    add('Status', l.status + (l.active ? ' (in progress)' : ''));
    add('Task', l.parameter_set ? `${l.parameter_set}${l.task ? ` (${l.task})` : ''}` : l.task);
    add('Rig', l.rig);
    let subject = null;
    if (l.subject) {
      subject = `sub-${l.subject}${l.initials ? ` · ${l.initials}` : ''}`
        + (l.identity?.subject?.record_id ? ' (registered)' : ' (typed)');
    }
    add('Subject', subject);
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
    detail.replaceChildren(box);
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
  };
}());
