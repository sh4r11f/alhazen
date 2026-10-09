/* Alhazen experiment workspace — the Data view: saved sessions, read.
 *
 * The page opens this view for one experiment through a two-call contract
 * (the navigation lives in workspace.js):
 *
 *   WorkspaceData.show(projectRecord, {api, token, node, error})
 *   WorkspaceData.hide()
 *
 * `projectRecord` is the experiment's /api/state entry; `api(path)` the
 * page's JSON fetcher (it adds the token header and throws the server's
 * message); `token` the API token, used only in <img src> URLs, which cannot
 * carry a header; `node(tag, className, text)` the page's element helper;
 * `error(msg)` its banner, which this view does not call: it says every
 * failure in the card it belongs to instead. The view fills #data-view by
 * creating elements and setting textContent — never innerHTML — because
 * everything shown (paths, subject ids, CSV cells, log lines) comes from
 * files on disk.
 *
 * Top to bottom the view holds four cards, each loaded from one route of
 * workspace_data.py (GET /api/data/<route>):
 *
 *   Data folder  roots  the folders the project's rigs write to (a picker)
 *   Runs         runs   every run in the folder: filters, a checkbox each
 *   Run          run    one run: its session.json card, files, records
 *                text   (a viewer), figures, the saved live monitor (page)
 *   Table        table  the CSV of the chosen run(s), pooled, sortable
 *
 * Every failure is shown in the card it belongs to, in words: a folder that
 * vanished, a CSV that cannot be parsed. A card is never left empty in a way
 * that could be read as "no data".
 *
 * Quick plots of the table (a Plot card under it, drawn by workspace_plot.js)
 * were taken out after alhazen 2.1.1; that release has their code.
 *
 * Extension point — experiment figures (NOT implemented yet). An experiment
 * will later declare its own analysis figures; the plan is a fifth card,
 * "Experiment figures", filled from a new route that runs the experiment's
 * figure functions in its own interpreter and returns images. This file
 * would then only list and show those images, the way the Run card shows
 * figures/ today (see docs/workspace.md §Data, "Experiment figures").
 */
'use strict';

const WorkspaceData = (() => {
  /* How many table rows are put in the DOM at once. The table holds up to
   * the server's cap (50 000); drawing them all would freeze the page, and
   * sorting and filtering are how the rest is reached. Said under the table. */
  const SHOWN_ROWS = 500;
  const TABLE_KINDS = ['trials', 'events', 'frames', 'paradigm'];

  /* What show() was given, and the view's state. `epoch` counts show()
   * calls and folder changes: an answer that arrives after the reader has
   * moved on belongs to a view that no longer exists and is dropped.
   * `numeric` says, per column of the loaded table, whether it sorts as
   * numbers (numericColumn below). */
  let ctx = null;
  let project = null;
  let epoch = 0;
  let state = null;

  function fresh() {
    return {
      roots: [], root: null, runs: [], checked: new Set(), filters: {version: '', subject: '', task: ''},
      detail: null, table: null, numeric: {}, kind: 'trials', sort: {column: -1, direction: 1},
      filter: '',
    };
  }

  const container = () => document.getElementById('data-view');
  const byName = (name) => container().querySelector(`[data-part="${name}"]`);
  const enc = encodeURIComponent;
  const node = (tag, className, text) => ctx.node(tag, className, text);

  /** A card: a titled section whose body is `data-part=<name>`. */
  function card(name, title) {
    const section = node('section', 'card data-card');
    section.setAttribute('data-card', name);
    const heading = node('div', 'card-heading');
    heading.appendChild(node('h2', '', title));
    section.appendChild(heading);
    const body = node('div', 'data-body');
    body.setAttribute('data-part', name);
    section.appendChild(body);
    return section;
  }

  /** A failure, in words, where it happened. */
  function problem(parent, message) {
    const p = node('p', 'data-error', message);
    p.setAttribute('role', 'alert');
    parent.appendChild(p);
    return p;
  }

  function note(parent, message) {
    return parent.appendChild(node('p', 'data-note', message));
  }

  /** A "working on it" line, removed by the answer or by the failure. */
  function loading(parent, message) {
    return parent.appendChild(node('p', 'data-note data-loading', message));
  }

  /** A <select> of [value, label] pairs, the value set after the options
   *  exist (a browser ignores a value no option has). */
  function select(options, value, onchange, label) {
    const element = node('select', 'data-select');
    if (label) element.setAttribute('aria-label', label);
    for (const [optionValue, text, disabled] of options) {
      const option = node('option', '', text);
      option.value = optionValue;
      if (disabled) option.disabled = true;
      element.appendChild(option);
    }
    element.value = value;
    element.onchange = () => onchange(element.value);
    return element;
  }

  function button(text, onclick, className = '') {
    const element = node('button', className, text);
    element.type = 'button';
    element.onclick = onclick;
    return element;
  }

  /** Run an async step for the current view only, showing any failure in
   *  `where` (a card body) rather than letting it vanish. */
  async function step(where, fn) {
    const mine = epoch;
    try {
      await fn(() => mine === epoch);
    } catch (e) {
      if (mine !== epoch) return;
      // The "Loading…" line would otherwise stay above the failure.
      where.querySelectorAll('.data-loading').forEach((element) => element.remove());
      problem(where, e.message);
    }
  }

  /* ---------------------------------------------------------------- */
  /* Show / hide                                                       */
  /* ---------------------------------------------------------------- */

  function show(projectRecord, context) {
    ctx = context;
    project = projectRecord;
    epoch += 1;
    state = fresh();
    const root = container();
    root.replaceChildren();
    root.hidden = false;
    // No heading of its own: the page's heading above both views already
    // names the experiment and says "Data" (workspace.js), and a second one
    // here repeated it. One line says what this view is.
    root.appendChild(node('p', 'help data-intro',
      'Saved sessions from this experiment’s data folders. Read only: nothing here changes a '
      + 'session. Upload copies sessions to the archive and keeps a receipt beside them.'));
    for (const [name, title] of [['roots', 'Data folder'], ['runs', 'Runs'], ['run', 'Run'],
      ['table', 'Table']]) {
      const section = card(name, title);
      if (name !== 'roots' && name !== 'runs') section.hidden = true;
      root.appendChild(section);
    }
    return loadRoots();
  }

  function hide() {
    epoch += 1;
    const root = container();
    if (root) {
      root.hidden = true;
      root.replaceChildren();
    }
  }

  const cardOf = (name) => container().querySelector(`[data-card="${name}"]`);

  /* ---------------------------------------------------------------- */
  /* Data folders                                                      */
  /* ---------------------------------------------------------------- */

  /** A data folder's text in the picker: its name and what kind of data it
   *  holds. The rigs that write there can be many and long
   *  (`amodal-averaging/lab-rehearsal`), so they go on the line under the
   *  picker (rootDetail), not into an option the menu would cut off. */
  function rootLabel(root) {
    const kind = {
      rehearsal: 'rehearsal (test, simulate)',
      training: 'training stage',
      'training-rehearsal': 'training stage rehearsal',
    }[root.kind] || 'real (run)';
    return `${root.name} — ${kind}`;
  }

  /** The line under the picker: the folder's path and the rigs writing there. */
  function rootDetail(root) {
    return `${root.path} · written by ${root.rigs.join(', ')}`;
  }

  function loadRoots() {
    const body = byName('roots');
    body.replaceChildren(loading(body, 'Reading the rigs…'));
    return step(body, async (current) => {
      const answer = await ctx.api(`/api/data/roots?project=${enc(project.id)}`);
      if (!current()) return;
      state.roots = answer.roots;
      body.replaceChildren();
      for (const message of answer.problems) problem(body, message);
      if (!answer.roots.length) {
        note(body, 'None of this experiment’s data folders exists yet: no session has been ' +
          'saved with its rigs. Run one, then come back.');
      } else {
        const options = answer.roots.map((r) => [r.id, rootLabel(r)]);
        const picker = select(options, answer.roots[0].id, (id) => chooseRoot(id), 'Data folder');
        picker.setAttribute('data-role', 'root');
        body.appendChild(picker);
        body.appendChild(node('p', 'path data-root-path'));
      }
      if (answer.missing.length) {
        // Folded away: useful to check a rig's data_root, noise otherwise.
        // By path: two missing folders may share a name (data/ of two rigs).
        const count = answer.missing.length;
        const more = node('details', 'data-more');
        more.appendChild(node('summary', '',
          `${count} more data folder${count === 1 ? '' : 's'} not created yet`));
        const list = node('ul', 'data-missing');
        for (const r of answer.missing) {
          list.appendChild(node('li', '', `${r.path} (${r.kind}; rigs ${r.rigs.join(', ')})`));
        }
        more.appendChild(list);
        body.appendChild(more);
      }
      if (answer.roots.length) await chooseRoot(answer.roots[0].id);
    });
  }

  async function chooseRoot(id) {
    epoch += 1;
    const root = state.roots.find((r) => r.id === id);
    state = {...fresh(), roots: state.roots, root};
    const path = container().querySelector('.data-root-path');
    if (path) path.textContent = rootDetail(root);
    for (const name of ['run', 'table']) cardOf(name).hidden = true;
    await loadRuns();
  }

  /* ---------------------------------------------------------------- */
  /* Runs                                                              */
  /* ---------------------------------------------------------------- */

  function loadRuns() {
    const body = byName('runs');
    body.replaceChildren(loading(body, 'Listing runs…'));
    return step(body, async (current) => {
      const answer = await ctx.api(
        `/api/data/runs?project=${enc(project.id)}&root=${enc(state.root.id)}`);
      if (!current()) return;
      state.runs = answer.runs;
      body.replaceChildren();
      for (const message of answer.problems) problem(body, message);
      drawRuns();
    });
  }

  const RUN_COLUMNS = [
    ['version', 'Version', (r) => r.version ?? 'pre-2.0'],
    ['subject', 'Subject', (r) => (r.initials ? `${r.subject} · ${r.initials}` : r.subject)],
    ['session', 'Session', (r) => String(r.session)],
    ['run', 'Run', (r) => String(r.run)],
    ['task', 'Task', (r) => r.task ?? '—'],
    ['mode', 'Mode', (r) => r.mode ?? '—'],
    ['date', 'Date', (r) => r.date ?? '—'],
    // Without report.yaml the count is the trials file's lines, marked ≈
    // (a quoted cell with a line break would add one); see drawRuns' title.
    ['trials', 'Trials', (r) => (r.trials === null ? '—' : (r.trials_counted === 'lines' ? `≈${r.trials}` : String(r.trials)))],
    ['rig', 'Rig', (r) => r.rig ?? '—'],
  ];

  function visibleRuns() {
    const f = state.filters;
    return state.runs.filter((r) =>
      (!f.version || (r.version ?? 'pre-2.0') === f.version) &&
      (!f.subject || r.subject === f.subject) &&
      (!f.task || (r.task ?? '—') === f.task));
  }

  function drawRuns() {
    const body = byName('runs');
    body.querySelectorAll('[data-runs]').forEach((element) => element.remove());
    const holder = node('div');
    holder.setAttribute('data-runs', '');
    body.appendChild(holder);
    if (!state.runs.length) {
      note(holder, `No runs in ${state.root.path} yet.`);
      return;
    }
    // Filters: one menu per identifying field, from the values present.
    const filters = node('div', 'data-toolbar');
    for (const [key, label, valueOf] of [
      ['version', 'Version', (r) => r.version ?? 'pre-2.0'],
      ['subject', 'Subject', (r) => r.subject],
      ['task', 'Task', (r) => r.task ?? '—'],
    ]) {
      const values = [...new Set(state.runs.map(valueOf))].sort();
      const menu = select([['', `Every ${label.toLowerCase()}`], ...values.map((v) => [v, v])],
        state.filters[key], (value) => { state.filters[key] = value; drawRuns(); }, label);
      menu.setAttribute('data-filter', key);
      filters.appendChild(menu);
    }
    const shown = visibleRuns();
    filters.appendChild(node('span', 'step', `${shown.length} OF ${state.runs.length} RUNS`));
    holder.appendChild(filters);

    const wrap = node('div', 'data-scroll data-runs-scroll');
    const table = node('table', 'data-table');
    const head = node('tr');
    head.appendChild(node('th', 'data-check', ''));
    for (const [, title] of RUN_COLUMNS) head.appendChild(node('th', '', title));
    table.appendChild(head);
    for (const run of shown) {
      const row = node('tr', state.detail?.id === run.id ? 'selected' : '');
      row.setAttribute('data-run', run.id);
      const tick = node('input');
      tick.type = 'checkbox';
      tick.checked = state.checked.has(run.id);
      tick.setAttribute('aria-label', `Pool ${run.id}`);
      tick.onchange = () => {
        if (tick.checked) state.checked.add(run.id); else state.checked.delete(run.id);
        drawPoolBar();
        state.upload?.draw();
      };
      const box = node('td', 'data-check');
      box.appendChild(tick);
      row.appendChild(box);
      for (const [key, , valueOf] of RUN_COLUMNS) {
        const cell = row.appendChild(node('td', '', valueOf(run)));
        if (key === 'trials' && run.trials_counted === 'lines') {
          cell.setAttribute('title', 'counted lines of the trials file (this run has no report.yaml)');
        }
      }
      if (run.problems.length) {
        const flag = node('td', 'data-flag', '⚠');
        flag.setAttribute('title', run.problems.join('\n'));
        row.appendChild(flag);
      }
      row.onclick = (event) => {
        if (event?.target?.type === 'checkbox') return;
        openRun(run.id);
      };
      table.appendChild(row);
    }
    wrap.appendChild(table);
    holder.appendChild(wrap);
    // Problems of listed runs, said under the table as well as flagged.
    for (const run of shown) {
      for (const message of run.problems) problem(holder, `${run.id}: ${message}`);
    }
    holder.appendChild(node('div', 'data-toolbar data-pool'));
    drawPoolBar();
    // Upload to the archive: the checked runs, or every run the filters show
    // (workspace_upload.js draws the bar and runs the upload).
    if (window.ArchiveUpload) {
      const bar = node('div', 'data-upload');
      holder.appendChild(bar);
      const rootId = state.root.id;
      state.upload = ArchiveUpload.mountBatch(bar, {api: ctx.api, node, project: project.id}, () => ({
        count: state.checked.size,
        groups: state.checked.size ? [{root: rootId, runs: [...state.checked]}] : [],
        // The whole folder: every session and every other file in it.
        all: [{root: rootId, all: true}],
        allLabel: `all of ${state.root.name}`,
      }));
    }
  }

  /** The bar under the run table: which table to load for the checked runs. */
  function drawPoolBar() {
    const bar = byName('runs').querySelector('.data-pool');
    if (!bar) return;
    bar.replaceChildren();
    const count = state.checked.size;
    bar.appendChild(select(TABLE_KINDS.map((k) => [k, `${k} table`]), state.kind,
      (value) => { state.kind = value; }, 'Table to load'));
    const load = button(count > 1 ? `Load and pool ${count} checked runs`
      : count === 1 ? 'Load the checked run' : 'Check runs to pool their tables',
    () => loadTable([...state.checked]), 'primary');
    load.disabled = count === 0;
    load.setAttribute('data-role', 'pool');
    bar.appendChild(load);
  }

  /* ---------------------------------------------------------------- */
  /* One run                                                           */
  /* ---------------------------------------------------------------- */

  function sizeText(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  /** session.json as a person reads it: labelled lines, not raw JSON. */
  function cardLines(detail) {
    const c = detail.card;
    if (!c) {
      return [
        ['Layout', detail.layout === 'pre-2.0' ? 'Recorded before alhazen 2.0 (no session.json)' : 'No session.json'],
        ['Subject', detail.subject], ['Session', detail.session], ['Run', detail.run],
        ['Task', detail.task ?? '—'], ['Date', detail.date ?? '—'], ['Rig', detail.rig ?? '—'],
      ];
    }
    const exp = c.experiment || {};
    const rig = c.rig || {};
    const subject = c.subject || {};
    return [
      ['Experiment', `${exp.name ?? '?'} ${exp.version ?? ''}${exp.git ? ` (git ${exp.git})` : ''}`],
      ['Task', c.task ?? '—'],
      ['Mode', c.mode ?? '—'],
      ['Subject', subject.initials ? `${subject.id} · ${subject.initials}` : String(subject.id ?? '—')],
      ['Session / run', `${c.session} / ${c.run}`],
      ['Seed', String(c.seed ?? '—')],
      ['Recorded', c.created ?? c.date ?? '—'],
      ['Rig', `${rig.name ?? '—'}${rig.source ? ` (${rig.source})` : ''}`],
      ['Params file', c.params_file ?? 'task defaults'],
      ['alhazen', c.alhazen?.version ?? '—'],
    ];
  }

  function openRun(id) {
    const section = cardOf('run');
    section.hidden = false;
    section.querySelector('h2').textContent = `Run · ${id}`;
    const body = byName('run');
    body.replaceChildren(loading(body, 'Reading the run…'));
    byName('runs').querySelectorAll('tr[data-run]').forEach((row) => {
      row.classList.toggle('selected', row.getAttribute('data-run') === id);
    });
    return step(body, async (current) => {
      const detail = await ctx.api(`/api/data/run?project=${enc(project.id)}` +
        `&root=${enc(state.root.id)}&run=${enc(id)}`);
      if (!current()) return;
      state.detail = detail;
      body.replaceChildren();
      drawRun(body, detail);
    });
  }

  function drawRun(body, detail) {
    for (const message of detail.problems) problem(body, message);
    if (detail.card_error) problem(body, detail.card_error);
    const columns = node('div', 'data-run-grid');
    const summary = node('dl', 'data-card-summary');
    for (const [key, value] of cardLines(detail)) {
      summary.appendChild(node('dt', '', key));
      summary.appendChild(node('dd', '', String(value)));
    }
    columns.appendChild(summary);

    const files = node('div', 'data-files');
    files.appendChild(node('h3', '', `Files (${detail.files.length}${detail.files_capped ? '+' : ''})`));
    const list = node('ul', 'data-file-list');
    for (const file of detail.files) {
      const item = node('li');
      item.appendChild(node('span', 'data-file-name', file.name));
      item.appendChild(node('span', 'data-file-size', sizeText(file.size)));
      list.appendChild(item);
    }
    files.appendChild(list);
    columns.appendChild(files);
    body.appendChild(columns);

    // Actions: load this run's table, open the saved monitor.
    const actions = node('div', 'data-toolbar');
    if (detail.tables.length) {
      for (const table of detail.tables) {
        const load = button(`Load ${table.kind} table`, () => {
          state.kind = table.kind;
          return loadTable([detail.id]);
        });
        load.setAttribute('data-load', table.kind);
        actions.appendChild(load);
      }
    } else {
      actions.appendChild(node('span', 'data-note', 'This run has no CSV tables (it did not record trials).'));
    }
    if (detail.page) {
      const open = button('Open saved live monitor ↗', () => openPage(detail, open));
      open.setAttribute('data-role', 'monitor');
      actions.appendChild(open);
    }
    body.appendChild(actions);

    // The text records, one button each, into one viewer.
    if (detail.texts.length) {
      body.appendChild(node('h3', '', 'Records'));
      const records = node('div', 'data-toolbar data-records');
      const viewer = node('pre', 'data-viewer');
      viewer.hidden = true;
      const status = node('p', 'data-note');
      for (const name of detail.texts) {
        const show = button(name, () => viewText(detail, name, viewer, status, records));
        show.setAttribute('data-text', name);
        records.appendChild(show);
      }
      body.appendChild(records);
      body.appendChild(status);
      body.appendChild(viewer);
    }

    if (detail.images.length) {
      body.appendChild(node('h3', '', `Figures (${detail.images.length})`));
      const gallery = node('div', 'data-figures');
      for (const name of detail.images) {
        const figure = node('figure');
        const img = node('img');
        img.src = `/data/file?project=${enc(project.id)}&root=${enc(state.root.id)}` +
          `&run=${enc(detail.id)}&name=${enc(name)}&token=${enc(ctx.token)}`;
        img.alt = name;
        img.onerror = () => figure.appendChild(node('figcaption', 'data-error', `${name} could not be shown`));
        figure.appendChild(img);
        figure.appendChild(node('figcaption', '', name));
        gallery.appendChild(figure);
      }
      body.appendChild(gallery);
    }
  }

  function viewText(detail, name, viewer, status, records) {
    records.querySelectorAll('button').forEach((b) => {
      b.setAttribute('aria-pressed', String(b.getAttribute('data-text') === name));
    });
    status.textContent = `Reading ${name}…`;
    status.className = 'data-note';
    return step(status.parentNode, async (current) => {
      try {
        const answer = await ctx.api(`/api/data/text?project=${enc(project.id)}` +
          `&root=${enc(state.root.id)}&run=${enc(detail.id)}&name=${enc(name)}`);
        if (!current()) return;
        let text = answer.text;
        // JSON reads better indented. A file that is not valid JSON is
        // shown as written — and said to be broken, under the viewer.
        let broken = '';
        if (name.endsWith('.json') && !answer.truncated) {
          try {
            text = JSON.stringify(JSON.parse(text), null, 2);
          } catch (e) {
            // Only a parse error is the file's fault; anything else is a bug.
            if (!(e instanceof SyntaxError)) throw e;
            broken = ` — not valid JSON (${e.message}); shown as written`;
          }
        }
        viewer.textContent = text;
        viewer.hidden = false;
        status.textContent = answer.truncated
          ? (answer.tail ? `The last ${sizeText(answer.text.length)} of ${name} (${sizeText(answer.size)}).`
            : `The first ${sizeText(answer.text.length)} of ${name} (${sizeText(answer.size)}).`)
          : `${name} · ${sizeText(answer.size)}${broken}`;
        if (broken) status.className = 'data-error';
      } catch (e) {
        status.textContent = `${name} cannot be shown: ${e.message}`;
        status.className = 'data-error';
      }
    });
  }

  /** Open the saved monitor page in a new tab through a short-lived link
   *  (the API token never goes in a tab's address). `noopener`, so the new
   *  tab gets nothing of this one — not its sessionStorage, where the
   *  token is kept. */
  async function openPage(detail, control) {
    try {
      const answer = await ctx.api(`/api/data/page?project=${enc(project.id)}` +
        `&root=${enc(state.root.id)}&run=${enc(detail.id)}&name=${enc(detail.page)}`);
      window.open(answer.url, '_blank', 'noopener');
    } catch (e) {
      problem(control.parentNode, `The saved monitor cannot be opened: ${e.message}`);
    }
  }

  /* ---------------------------------------------------------------- */
  /* Table                                                             */
  /* ---------------------------------------------------------------- */

  /* The table's cells arrive as the CSV's own text (workspace_data.py sends
   * no types), so the view decides what a column holds, for sorting: a
   * numeric column sorts by value, any other as text. */

  /** A cell as a number, or null when it is empty, or NaN when it is text.
   *  "True"/"False" (how the trials file writes a boolean) read as 1/0. */
  function parseCell(text) {
    const value = String(text ?? '').trim();
    if (value === '') return null;
    if (value === 'True' || value === 'true') return 1;
    if (value === 'False' || value === 'false') return 0;
    // Number('') is 0 and Number(' ') too, so emptiness is decided above;
    // '0x10' and 'Infinity' are not measurements, though Number() takes them.
    if (!/^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(value)) return NaN;
    return Number(value);
  }

  /** Whether a column sorts as numbers: every non-empty cell is one, and
   *  there is at least one (an all-empty column has nothing to compare). */
  function numericColumn(cells) {
    let seen = false;
    for (const cell of cells) {
      const value = parseCell(cell);
      if (value === null) continue;
      if (Number.isNaN(value)) return false;
      seen = true;
    }
    return seen;
  }

  function loadTable(runIds) {
    const section = cardOf('table');
    section.hidden = false;
    const body = byName('table');
    body.replaceChildren(loading(body, `Loading the ${state.kind} table of ${runIds.length} run(s)…`));
    return step(body, async (current) => {
      const answer = await ctx.api(`/api/data/table?project=${enc(project.id)}` +
        `&root=${enc(state.root.id)}&runs=${enc(runIds.join(','))}&kind=${enc(state.kind)}`);
      if (!current()) return;
      state.table = answer;
      state.sort = {column: -1, direction: 1};
      state.filter = '';
      // Which columns sort as numbers, decided once per load rather than
      // on every click of a header.
      state.numeric = {};
      answer.columns.forEach((name, i) => {
        state.numeric[name] = numericColumn(answer.rows.map((r) => r[i]));
      });
      body.replaceChildren();
      drawTable();
    });
  }

  /** The rows the filter keeps, in the chosen order. */
  function tableRows() {
    const table = state.table;
    const needle = state.filter.trim().toLowerCase();
    let rows = needle
      ? table.rows.filter((r) => r.some((cell) => cell.toLowerCase().includes(needle)))
      : table.rows.slice();
    const {column, direction} = state.sort;
    if (column >= 0) {
      const numeric = state.numeric[table.columns[column]];
      rows = rows.sort((a, b) => {
        if (numeric) {
          const x = parseCell(a[column]);
          const y = parseCell(b[column]);
          // Empty cells last, whichever the direction.
          if (x === null || y === null) return (x === null) - (y === null);
          return (x - y) * direction;
        }
        return a[column].localeCompare(b[column], undefined, {numeric: true}) * direction;
      });
    }
    return rows;
  }

  function drawTable() {
    const body = byName('table');
    const table = state.table;
    body.replaceChildren();
    for (const message of table.problems) problem(body, message);
    if (table.capped) {
      problem(body, `Only the first ${table.rows.length.toLocaleString('en')} of ` +
        `${table.total.toLocaleString('en')} rows were loaded (the limit is ` +
        `${table.limit.toLocaleString('en')}). Filters and sorting see those rows only.`);
    }
    const bar = node('div', 'data-toolbar');
    bar.appendChild(select(TABLE_KINDS.map((k) => [k, `${k} table`]), state.kind, (value) => {
      state.kind = value;
      loadTable(table.files.map((f) => f.run));
    }, 'Table'));
    const search = node('input', 'data-search');
    search.type = 'search';
    search.placeholder = 'Filter rows (any cell contains…)';
    search.value = state.filter;
    search.setAttribute('aria-label', 'Filter rows');
    search.oninput = () => { state.filter = search.value; drawRows(); };
    bar.appendChild(search);
    bar.appendChild(node('span', 'step data-count'));
    body.appendChild(bar);
    // Which file each row came from, and for a capped table how much of
    // each was loaded (none, for a pooled run after the cap).
    const loadedText = (f) => (f.loaded < f.rows
      ? `, ${f.loaded ? f.loaded.toLocaleString('en') : 'none'} loaded` : '');
    const sources = node('ul', 'data-missing');
    for (const f of table.files) {
      sources.appendChild(node('li', f.loaded < f.rows ? 'data-cut' : '',
        `${f.run}: ${f.name} (${f.rows.toLocaleString('en')} row${f.rows === 1 ? '' : 's'}${loadedText(f)})`));
    }
    body.appendChild(sources);
    const wrap = node('div', 'data-scroll data-rows-scroll');
    wrap.appendChild(node('table', 'data-table data-rows'));
    body.appendChild(wrap);
    body.appendChild(node('p', 'data-note data-shown'));
    drawRows();
  }

  function drawRows() {
    const body = byName('table');
    const table = state.table;
    const rows = tableRows();
    body.querySelector('.data-count').textContent =
      `${rows.length.toLocaleString('en')} OF ${table.rows.length.toLocaleString('en')} ROWS`;
    const element = body.querySelector('.data-rows');
    element.replaceChildren();
    const head = node('tr');
    table.columns.forEach((name, i) => {
      const arrow = state.sort.column === i ? (state.sort.direction > 0 ? ' ▲' : ' ▼') : '';
      const th = node('th', table.added.includes(name) ? 'data-added' : '', name + arrow);
      th.setAttribute('data-column', String(i));
      th.setAttribute('title', `Sort by ${name}`);
      th.onclick = () => {
        state.sort = state.sort.column === i
          ? {column: i, direction: -state.sort.direction} : {column: i, direction: 1};
        drawRows();
      };
      head.appendChild(th);
    });
    element.appendChild(head);
    for (const cells of rows.slice(0, SHOWN_ROWS)) {
      const tr = node('tr');
      for (const cell of cells) tr.appendChild(node('td', '', cell));
      element.appendChild(tr);
    }
    const shown = body.querySelector('.data-shown');
    shown.textContent = rows.length > SHOWN_ROWS
      ? `The first ${SHOWN_ROWS} of these ${rows.length.toLocaleString('en')} rows are shown; ` +
        'sort or filter to bring others up.'
      : (rows.length ? '' : 'No row matches the filter.');
  }

  return {show, hide, SHOWN_ROWS};
})();

// Classic scripts share one global scope, but a `const` is not a property of
// `window`; the page's navigation reaches the view through window.WorkspaceData.
if (typeof window !== 'undefined') window.WorkspaceData = WorkspaceData;
