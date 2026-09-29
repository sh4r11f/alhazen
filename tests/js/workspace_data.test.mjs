/* The Data view (workspace_data.js) rendered in the fake DOM.
 *
 * The view is loaded on its own, as workspace.html loads it, and driven
 * through its two-call contract, WorkspaceData.show(project, ctx) and
 * hide(). `ctx.api` is a fake of workspace_data.py's routes answering from
 * plain objects (`server.*` below), so a test reads like its scenario: "a
 * folder with two runs, one of them damaged". A route a test did not set up
 * throws, naming the URL — never an empty answer.
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

import { FakeDocument } from './fake_dom.mjs';

const ASSETS = new URL('../../src/alhazen/cli/assets/', import.meta.url);
const VIEW = readFileSync(new URL('workspace_data.js', ASSETS), 'utf8');

const settle = () => new Promise((resolve) => setImmediate(resolve));
async function settled() {
  for (let i = 0; i < 6; i++) await settle();
}

const ROOTS = {
  roots: [
    { id: 'r1', path: '/exp/data', name: 'data', kind: 'real', rigs: ['lab', 'alhazen/mac'] },
    { id: 'r2', path: '/exp/data-rehearsal', name: 'data-rehearsal', kind: 'rehearsal', rigs: ['lab'] },
  ],
  missing: [{ id: 'r3', path: '/exp/bench', name: 'bench', kind: 'real', rigs: ['bench'] }],
  problems: ['Rig broken cannot be read: bad'],
};

function run(id, extra = {}) {
  const [version, sub, ses, folder] = id.split('/');
  return {
    id, version: version.slice(1), layout: '2.0', subject: sub.slice(4), initials: 'HD',
    session: Number(ses.slice(4)), run: Number(folder.slice(4, 6)), task: 'demo', mode: 'run',
    date: '2026-09-29', rig: 'lab', trials: 3, trials_counted: 'report', problems: [], ...extra,
  };
}

const RUN_A = 'v1.0.0/sub-01/ses-001/run-01_task-demo';
const RUN_B = 'v1.0.0/sub-02/ses-001/run-01_task-demo';
const RUN_C = 'v2.0.0/sub-01/ses-001/run-02_task-other';

function fakeServer() {
  const server = {
    roots: ROOTS,
    runs: { r1: { runs: [run(RUN_A), run(RUN_B, { trials: 4, trials_counted: 'lines' }),
      run(RUN_C, { task: 'other', problems: ['report.yaml cannot be read: bad'] })], problems: [] } },
    details: {},
    texts: {},
    tables: {},
    pages: {},
    calls: [],
  };
  server.api = async (url) => {
    server.calls.push(url);
    const [path, query] = url.split('?');
    const q = new URLSearchParams(query);
    const found = (value, what) => {
      if (value instanceof Error) throw value;
      if (value === undefined) throw new Error(`the fake has no ${what} for ${url}`);
      return JSON.parse(JSON.stringify(value));
    };
    if (path === '/api/data/roots') return found(server.roots, 'roots');
    if (path === '/api/data/runs') return found(server.runs[q.get('root')], 'runs');
    if (path === '/api/data/run') return found(server.details[q.get('run')], 'run');
    if (path === '/api/data/text') return found(server.texts[q.get('name')], 'text');
    if (path === '/api/data/table') return found(server.tables[`${q.get('runs')}|${q.get('kind')}`], 'table');
    if (path === '/api/data/page') return found(server.pages[q.get('run')], 'page');
    throw new Error('the fake has no route ' + url);
  };
  return server;
}

function load() {
  const document = new FakeDocument();
  const view = document.createElement('div');
  view.setAttribute('id', 'data-view');
  view.hidden = true;
  document.body.appendChild(view);
  const opened = [];
  // Only what the view uses: a global it needs and does not find here
  // fails the test with a ReferenceError instead of passing unnoticed.
  const sandbox = {
    document, console, encodeURIComponent, URLSearchParams,
    window: { open: (...args) => opened.push(args) },
  };
  const context = vm.createContext(sandbox);
  vm.runInContext(VIEW, context);
  const server = fakeServer();
  const errors = [];
  const ctx = {
    api: server.api,
    token: 'tok en',
    node: (tag, className, text) => {
      const el = document.createElement(tag);
      if (className) el.className = className;
      if (text !== undefined) el.textContent = text;
      return el;
    },
    error: (message) => errors.push(message),
  };
  const show = (project = { id: 'p1', name: 'demo', title: 'Demo experiment' }) =>
    sandbox.window.WorkspaceData.show(project, ctx);
  return { document, view, server, show, ctx, errors, opened, sandbox };
}

const cardOf = (view, name) => view.querySelector(`[data-card="${name}"]`);
const texts = (element, selector) => element.querySelectorAll(selector).map((e) => e.textContent);

test('show() fills the view: folder picker, problems, missing folders, the runs', async () => {
  const page = load();
  await page.show();
  await settled();
  assert.equal(page.view.hidden, false);
  // No heading of its own (the page's heading above names the experiment),
  // one line saying what the view is.
  assert.equal(page.view.querySelector('h1'), null);
  assert.match(page.view.querySelector('.data-intro').textContent, /Read only/);
  const roots = cardOf(page.view, 'roots');
  const picker = roots.querySelector('select');
  // The options say the folder and its kind; the writers go under the picker.
  assert.deepEqual(texts(picker, 'option'), [
    'data — real (run)',
    'data-rehearsal — rehearsal (test, simulate)',
  ]);
  assert.equal(picker.value, 'r1');
  assert.deepEqual(texts(roots, '.data-error'), ['Rig broken cannot be read: bad']);
  // Folded into a closed <details>, its summary counting them.
  const more = roots.querySelector('details');
  assert.equal(more.open, false);
  assert.equal(more.querySelector('summary').textContent, '1 more data folder not created yet');
  assert.deepEqual(texts(more.querySelector('.data-missing'), 'li'), ['/exp/bench (real; rigs bench)']);
  assert.equal(roots.querySelector('.data-root-path').textContent,
    '/exp/data · written by lab, alhazen/mac');
  // Three runs, each row with its fields; the line-counted one marked "~".
  const rows = page.view.querySelectorAll('tr[data-run]');
  assert.deepEqual(rows.map((r) => r.getAttribute('data-run')), [RUN_A, RUN_B, RUN_C]);
  assert.deepEqual(texts(rows[0], 'td').slice(1),
    ['1.0.0', '01 · HD', '1', '1', 'demo', 'run', '2026-09-29', '3', 'lab']);
  const counted = rows[1].querySelectorAll('td')[8];
  assert.equal(counted.textContent, '≈4');
  assert.match(counted.getAttribute('title'), /counted lines of the trials file/);
  assert.equal(rows[0].querySelectorAll('td')[8].getAttribute('title'), null);
  // A run's problem is flagged in its row and said under the table.
  assert.equal(rows[2].querySelector('.data-flag').getAttribute('title'), 'report.yaml cannot be read: bad');
  assert.match(texts(cardOf(page.view, 'runs'), '.data-error').join(), /v2.0.0.*report.yaml cannot be read/);
});

test('filters narrow the run list by version, subject and task', async () => {
  const page = load();
  await page.show();
  await settled();
  const runs = cardOf(page.view, 'runs');
  const filter = (key, value) => {
    const menu = runs.querySelector(`select[data-filter="${key}"]`);
    menu.value = value;
    menu.onchange();
  };
  filter('version', '1.0.0');
  assert.equal(runs.querySelectorAll('tr[data-run]').length, 2);
  filter('subject', '02');
  assert.deepEqual(runs.querySelectorAll('tr[data-run]').map((r) => r.getAttribute('data-run')), [RUN_B]);
  assert.match(runs.querySelector('.step').textContent, /1 OF 3 RUNS/);
  filter('version', '');
  filter('subject', '');
  filter('task', 'other');
  assert.deepEqual(runs.querySelectorAll('tr[data-run]').map((r) => r.getAttribute('data-run')), [RUN_C]);
});

test('a failure to list is said in the card, not left as an empty table', async () => {
  const page = load();
  page.server.runs.r1 = new Error('The data folder /exp/data no longer exists');
  await page.show();
  await settled();
  assert.deepEqual(texts(cardOf(page.view, 'runs'), '.data-error'),
    ['The data folder /exp/data no longer exists']);
  // The "Listing runs…" line goes with the failure it would contradict.
  assert.equal(cardOf(page.view, 'runs').querySelectorAll('.data-loading').length, 0);
  const again = load();
  again.server.roots = new Error('Experiment is not registered');
  await again.show();
  await settled();
  assert.deepEqual(texts(cardOf(again.view, 'roots'), '.data-error'), ['Experiment is not registered']);
});

test('no data folder yet is said as such', async () => {
  const page = load();
  page.server.roots = { roots: [], missing: ROOTS.missing, problems: [] };
  await page.show();
  await settled();
  assert.match(texts(cardOf(page.view, 'roots'), '.data-note').join(' '), /None of this experiment’s data folders exists yet/);
});

const DETAIL = {
  ...run(RUN_A), path: '/exp/data/' + RUN_A, card_error: null, files_capped: false,
  card: {
    experiment: { name: 'demo', version: '1.0.0', git: 'abc' }, task: 'demo', mode: 'run',
    subject: { id: '01', initials: 'HD' }, session: 1, run: 1, seed: 7, date: '20260929',
    created: '2026-09-29T10:00:00+00:00', rig: { name: 'lab', source: 'experiment' },
    params_file: null, alhazen: { version: '2.0.1' },
  },
  files: [{ name: 'session.json', size: 900 }, { name: 'figures/fit.png', size: 2048 }],
  texts: ['session.json', 'session.log'],
  tables: [{ kind: 'trials', name: 'x_trials.csv' }],
  images: ['figures/fit.png'],
  page: 'figures/live_monitor.html',
};

async function openedRun(page) {
  page.server.details[RUN_A] = DETAIL;
  await page.show();
  await settled();
  page.view.querySelectorAll('tr[data-run]')[0].onclick({ target: {} });
  await settled();
  return cardOf(page.view, 'run');
}

test('a run opens as a readable card, its files, records, figures and monitor', async () => {
  const page = load();
  const card = await openedRun(page);
  assert.equal(card.hidden, false);
  const summary = card.querySelector('dl');
  const pairs = Object.fromEntries(texts(summary, 'dt').map((k, i) => [k, texts(summary, 'dd')[i]]));
  assert.equal(pairs.Experiment, 'demo 1.0.0 (git abc)');
  assert.equal(pairs.Subject, '01 · HD');
  assert.equal(pairs['Params file'], 'task defaults');
  assert.deepEqual(texts(card, '.data-file-name'), ['session.json', 'figures/fit.png']);
  assert.deepEqual(texts(card, '.data-file-size'), ['900 B', '2.0 KB']);
  // The figure's URL: every part encoded, the token included (an <img>
  // cannot send a header).
  const img = card.querySelector('img');
  assert.equal(img.src, '/data/file?project=p1&root=r1&run=v1.0.0%2Fsub-01%2Fses-001%2Frun-01_task-demo' +
    '&name=figures%2Ffit.png&token=tok%20en');
  // A record, shown in the viewer, JSON indented.
  page.server.texts['session.json'] = { name: 'session.json', size: 20, truncated: false, tail: false, text: '{"run":1}' };
  await card.querySelector('button[data-text="session.json"]').onclick();
  await settled();
  assert.equal(card.querySelector('pre').textContent, '{\n  "run": 1\n}');
  assert.equal(card.querySelector('pre').hidden, false);
  // A broken JSON record is shown as written, and said to be broken.
  page.server.texts['session.json'] = { name: 'session.json', size: 5, truncated: false, tail: false, text: '{"ru' };
  await card.querySelector('button[data-text="session.json"]').onclick();
  await settled();
  assert.equal(card.querySelector('pre').textContent, '{"ru');
  assert.match(texts(card, '.data-error').join(), /session.json · 5 B — not valid JSON/);
  // The log's tail says it is one.
  page.server.texts['session.log'] = { name: 'session.log', size: 200000, truncated: true, tail: true, text: 'end\n' };
  await card.querySelector('button[data-text="session.log"]').onclick();
  await settled();
  assert.match(texts(card, '.data-note').join(' '), /The last 4 B of session.log \(195.3 KB\)/);
  // A record that cannot be read says so where it would have been.
  page.server.texts['session.log'] = new Error('The run is no longer in /exp/data');
  await card.querySelector('button[data-text="session.log"]').onclick();
  await settled();
  assert.match(texts(card, '.data-error').join(' '), /session.log cannot be shown: The run is no longer/);
  // The saved monitor opens through its ticket, in a tab with no opener.
  page.server.pages[RUN_A] = { url: '/data-page/abc' };
  await card.querySelector('button[data-role="monitor"]').onclick();
  assert.deepEqual(JSON.parse(JSON.stringify(page.opened)), [['/data-page/abc', '_blank', 'noopener']]);
});

test('a run without session.json shows what its folder says', async () => {
  const page = load();
  page.server.details[RUN_A] = { ...DETAIL, card: null, layout: 'pre-2.0', version: null, page: null };
  await page.show();
  await settled();
  page.view.querySelectorAll('tr[data-run]')[0].onclick({ target: {} });
  await settled();
  const card = cardOf(page.view, 'run');
  assert.match(card.querySelector('dd').textContent, /before alhazen 2.0/);
  assert.equal(card.querySelector('button[data-role="monitor"]'), null);
});

const TABLE = {
  kind: 'trials', added: ['run', 'subject', 'session'],
  columns: ['run', 'subject', 'session', 'cond', 'success', 'rt_ms'],
  rows: [
    [RUN_A, '01', '1', 'near', 'True', '300'],
    [RUN_A, '01', '1', 'far', 'False', '12'],
    [RUN_B, '02', '1', 'near', 'True', '250'],
  ],
  total: 3, capped: false, limit: 50000, problems: [],
  files: [{ run: RUN_A, name: 'a_trials.csv', rows: 2, loaded: 2 }, { run: RUN_B, name: 'b_trials.csv', rows: 1, loaded: 1 }],
};

async function pooled(page, table = TABLE) {
  page.server.tables[`${RUN_A},${RUN_B}|trials`] = table;
  await page.show();
  await settled();
  const runs = cardOf(page.view, 'runs');
  const ticks = runs.querySelectorAll('input');
  ticks[0].checked = true; ticks[0].onchange();
  ticks[1].checked = true; ticks[1].onchange();
  const load = runs.querySelector('button[data-role="pool"]');
  assert.equal(load.textContent, 'Load and pool 2 checked runs');
  assert.equal(load.disabled, false);
  await load.onclick();
  await settled();
  return cardOf(page.view, 'table');
}

test('checked runs pool into one table, sortable and filterable', async () => {
  const page = load();
  const card = await pooled(page);
  assert.ok(page.server.calls.some((u) => u.includes('runs=' + encodeURIComponent(`${RUN_A},${RUN_B}`))));
  const header = texts(card, 'th');
  assert.deepEqual(header, ['run', 'subject', 'session', 'cond', 'success', 'rt_ms']);
  // The added columns are marked apart from the file's own.
  assert.equal(card.querySelectorAll('th.data-added').length, 3);
  assert.match(card.querySelector('.data-count').textContent, /3 OF 3 ROWS/);
  // Sort by rt_ms: numerically (12 before 250), then descending.
  const rt = card.querySelector('th[data-column="5"]');
  rt.onclick();
  const column = () => card.querySelectorAll('tr').slice(1).map((r) => r.children[5].textContent);
  assert.deepEqual(column(), ['12', '250', '300']);
  card.querySelector('th[data-column="5"]').onclick();
  assert.deepEqual(column(), ['300', '250', '12']);
  // Filter: any cell containing the text.
  const search = card.querySelector('input');
  search.value = 'NEAR';
  search.oninput();
  assert.match(card.querySelector('.data-count').textContent, /2 OF 3 ROWS/);
  search.value = 'nothing like this';
  search.oninput();
  assert.equal(card.querySelector('.data-shown').textContent, 'No row matches the filter.');
});

test('a capped table says so', async () => {
  const page = load();
  const card = await pooled(page, {
    ...TABLE, total: 61564, capped: true, limit: 3,
    files: [{ run: RUN_A, name: 'a_frames.csv', rows: 61563, loaded: 3 }, { run: RUN_B, name: 'b_frames.csv', rows: 1, loaded: 0 }],
  });
  // How much of each file made it in: all of the first's cap, none of the second.
  assert.deepEqual(texts(card, 'li.data-cut'), [
    `${RUN_A}: a_frames.csv (61,563 rows, 3 loaded)`,
    `${RUN_B}: b_frames.csv (1 row, none loaded)`,
  ]);
  assert.match(texts(card, '.data-error').join(),
    /Only the first 3 of 61,564 rows were loaded \(the limit is 3\)\. Filters and sorting see those rows only\./);
});

/** A pooled table with one column of `cells` (named `value`) beside the
 *  three pooling columns, loaded, and a function reading that column. */
async function tableWithColumn(page, cells) {
  const card = await pooled(page, {
    ...TABLE,
    columns: ['run', 'subject', 'session', 'value'],
    rows: cells.map((cell, i) => [i % 2 ? RUN_B : RUN_A, i % 2 ? '02' : '01', '1', cell]),
    total: cells.length,
  });
  const column = () => card.querySelectorAll('tr').slice(1).map((r) => r.children[3].textContent);
  const sort = () => card.querySelector('th[data-column="3"]').onclick();
  return { column, sort };
}

test('a numeric column sorts by value, however the CSV writes its numbers', async () => {
  /* The cells are the CSV's own text. A number may carry spaces, a sign or
   * an exponent; True/False (how the trials file writes a boolean) count as
   * 1 and 0; an empty cell has no value and goes last either way. Sorted as
   * text, 0.5 would come before 0.25; by value it comes after. */
  const page = load();
  const { column, sort } = await tableWithColumn(page, ['0.5', '0.25', ' -2e3 ', 'True', 'false', '', '10', '+.75']);
  sort();
  assert.deepEqual(column(), [' -2e3 ', 'false', '0.25', '0.5', '+.75', 'True', '10', '']);
  sort();
  assert.deepEqual(column(), ['10', 'True', '+.75', '0.5', '0.25', 'false', ' -2e3 ', '']);
});

test('one text cell makes a column sort as text', async () => {
  /* HIT is text; so are 0x10 and Infinity, which JavaScript's Number()
   * would take but which are not measurements. With any of them the column
   * sorts as text (digit runs compared as numbers), so 0.5 comes before 0.25
   * and 0x10 before 1 — neither of which a numeric sort would do. */
  for (const [cells, sorted] of [
    [['0.5', '0.25', 'HIT', '10'], ['0.5', '0.25', '10', 'HIT']],
    [['2', '0x10', '1'], ['0x10', '1', '2']],
    [['0.5', 'Infinity', '0.25'], ['0.5', '0.25', 'Infinity']],
  ]) {
    const page = load();
    const { column, sort } = await tableWithColumn(page, cells);
    sort();
    assert.deepEqual(column(), sorted, cells.join(' '));
  }
});

test('the view has no plot: four cards, and a loaded table is only a table', async () => {
  /* The owner took the quick plots out after 2.1.1 ("leave the plotting out
   * for now"); this keeps a merge from bringing them back unnoticed. */
  const page = load();
  await pooled(page);
  assert.deepEqual(page.view.querySelectorAll('[data-card]').map((c) => c.getAttribute('data-card')),
    ['roots', 'runs', 'run', 'table']);
  assert.equal(page.view.querySelector('svg'), null);
  assert.ok(!texts(page.view, 'button').some((t) => /figure|plot/i.test(t)));
  assert.doesNotMatch(texts(page.view, '.data-shown').join(), /plot/i);
  // Every failure is said in its card; nothing went to the page's banner.
  assert.deepEqual(page.errors, []);
});

test('a table that cannot be read is an error in the table card', async () => {
  const page = load();
  page.server.tables[`${RUN_A}|trials`] = new Error(`${RUN_A}: x_trials.csv cannot be parsed after line 3`);
  page.server.details[RUN_A] = DETAIL;
  await page.show();
  await settled();
  page.view.querySelectorAll('tr[data-run]')[0].onclick({ target: {} });
  await settled();
  await cardOf(page.view, 'run').querySelector('button[data-load="trials"]').onclick();
  await settled();
  assert.match(texts(cardOf(page.view, 'table'), '.data-error').join(), /cannot be parsed after line 3/);
});

test('hide() empties the view, and a late answer is dropped', async () => {
  const page = load();
  let release;
  page.server.api = ((original) => async (url) => {
    if (url.startsWith('/api/data/runs')) await new Promise((resolve) => { release = resolve; });
    return original(url);
  })(page.server.api);
  page.ctx.api = page.server.api;
  const shown = page.show();
  await settled();
  page.sandbox.window.WorkspaceData.hide();
  assert.equal(page.view.hidden, true);
  assert.equal(page.view.children.length, 0);
  release();
  await shown;
  await settled();
  assert.equal(page.view.children.length, 0);
});

test('the view never writes markup: file text lands as text', async () => {
  const page = load();
  page.server.runs.r1.runs[0].task = '<img src=x onerror=alert(1)>';
  await page.show();
  await settled();
  const cell = page.view.querySelectorAll('tr[data-run]')[0].children[5];
  assert.equal(cell.textContent, '<img src=x onerror=alert(1)>');
  assert.equal(cell.children.length, 0);
  // And no code path assigns markup (the file comments may name it).
  assert.ok(!/\.(innerHTML|outerHTML)\s*=|insertAdjacentHTML/.test(VIEW));
});

test('every colour variable the Data view uses is one workspace.css defines', () => {
  // The view is coloured only through workspace.css' theme variables. One
  // that is not defined there resolves to nothing, and the browser's
  // fallback for an SVG fill is black: 2.1.0's plot background went black
  // in the light theme when --white was renamed --surface. So every var(--x)
  // in the view's stylesheet and script must be declared in the page's.
  const page = readFileSync(new URL('workspace.css', ASSETS), 'utf8');
  const defined = new Set([...page.matchAll(/^\s*(--[\w-]+)\s*:/gm)].map((m) => m[1]));
  const sheet = readFileSync(new URL('workspace_data.css', ASSETS), 'utf8');
  const used = new Set([
    ...[...sheet.matchAll(/var\((--[\w-]+)/g)].map((m) => m[1]),
    // Any the script writes into an inline style (none today).
    ...[...VIEW.matchAll(/var\((--[\w-]+)/g)].map((m) => m[1]),
  ]);
  assert.ok(used.size > 3, 'found the variables the view uses');
  const undefinedOnes = [...used].filter((name) => !defined.has(name));
  assert.deepEqual(undefinedOnes, [], `not defined in workspace.css: ${undefinedOnes.join(', ')}`);
});
