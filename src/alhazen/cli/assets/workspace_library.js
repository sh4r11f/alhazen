/* Alhazen experiment workspace — the library: the owner's copies on a
 * platform (an Experiment Platform host, or Mana), downloaded on demand.
 *
 * Served beside workspace.js (dashboard.py; GET /api/library, POST
 * /api/library/refresh and /api/library/download, workspace_library.py).
 * It loads after workspace.js (both deferred, in order) and starts itself
 * with that page's helpers (pageHelpers()); it then
 * draws two places it owns:
 *
 *   #library-nav   the sidebar, below the registered experiments: every copy
 *                  in the library that is not registered here, with its state
 *                  (Not downloaded, On this machine, Downloading…) and its one
 *                  action;
 *   #library-home  the Experiments page: the whole library with each copy's
 *                  disk state, saved sessions and actions, and the question a
 *                  download asks when a folder already holds other files and
 *                  data (Update the files, keep the data / Keep as it is).
 *
 * A registered copy is an ordinary experiment in the sidebar; the library
 * only adds what is not here yet. Nothing is downloaded without a click.
 */
'use strict';

(function () {
  const POLL_BUSY_MS = 1500;
  const POLL_IDLE_MS = 20000;
  const STAGE_WORDS = {
    checking: 'Checking this machine',
    fetching: 'Downloading the files',
    writing: 'Writing the files',
    building: 'Building the environment',
    registering: 'Registering',
  };

  let h = null;          // the page's helpers (pageHelpers() in workspace.js)
  let data = null;       // the last /api/library answer
  let timer = null;
  let signature = '';
  const asked = new Set();      // copies whose download this tab started
  const kept = new Set();       // conflicts the reader chose to leave as they are
  let refreshing = false;

  function $(id) { return document.getElementById(id); }

  function stateOf(item) {
    const job = item.job;
    if (job && job.status === 'running') return {key: 'busy', words: (STAGE_WORDS[job.stage] || 'Working') + '…'};
    if (job && job.status === 'conflict' && !kept.has(item.id)) return {key: 'ask', words: 'Needs your answer'};
    if (job && job.status === 'failed') return {key: 'bad', words: 'Download failed'};
    if (!item.downloadable) return {key: 'idle', words: 'No revision yet'};
    const disk = item.disk;
    if (disk.state === 'absent') return {key: 'idle', words: 'Not downloaded'};
    if (disk.state === 'current') return {key: 'ok', words: item.project ? 'On this machine' : 'On this machine, not set up'};
    if (disk.state === 'outdated') return {key: 'warn', words: 'Newer revision in the library'};
    return {key: 'warn', words: 'Folder of unknown version'};
  }

  function actionOf(item) {
    const job = item.job;
    if (!item.downloadable || (job && job.status === 'running')) return null;
    const disk = item.disk;
    if (disk.state === 'absent') return 'Download';
    if (disk.state === 'current') return item.project ? null : 'Set up';
    if (disk.state === 'outdated') return 'Update';
    return 'Check and set up';
  }

  function sessions(n) { return `${n} saved session${n === 1 ? '' : 's'}`; }

  async function load() {
    data = await h.api('/api/library');
    draw();
    schedule();
  }

  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      data = await h.api('/api/library/refresh', {});
      // A copy on disk with data may have been registered just now.
      h.refresh();
    } catch (e) {
      // The page keeps the last list and says why it is old.
      data = await h.api('/api/library');
      if (!data.error) data.error = e.message;
    } finally {
      refreshing = false;
    }
    draw();
    schedule();
  }

  function schedule() {
    clearTimeout(timer);
    if (!data || !data.configured) return;
    const busy = data.items.some((x) => x.job && x.job.status === 'running');
    timer = setTimeout(() => {
      const before = new Set(data.items.filter((x) => x.job && x.job.status === 'running').map((x) => x.id));
      h.api('/api/library').then((next) => {
        data = next;
        const finished = data.items.filter((x) => before.has(x.id) && x.job && x.job.status !== 'running');
        draw();
        schedule();
        if (finished.length) whenFinished(finished);
      }).catch((e) => { h.error(e.message); schedule(); });
    }, busy ? POLL_BUSY_MS : POLL_IDLE_MS);
  }

  /* A download this tab asked for that finished: show the new experiment's
   * Run page once the workspace lists it. */
  async function whenFinished(items) {
    h.refresh();
    for (const item of items) {
      if (item.job.status === 'done' && asked.has(item.id) && item.project) {
        asked.delete(item.id);
        await h.navigate(item.project, 'run');
      }
    }
  }

  async function download(item, replace) {
    h.error('');
    kept.delete(item.id);
    asked.add(item.id);
    try {
      await h.api('/api/library/download', {id: item.id, replace: !!replace});
    } catch (e) {
      asked.delete(item.id);
      h.error(e.message);
    }
    data = await h.api('/api/library');
    draw();
    schedule();
  }

  /* ---------------------------------------------------------------- sidebar */
  function navItem(item) {
    const st = stateOf(item);
    const group = h.node('div', 'nav-group library-nav-item');
    group.dataset.copy = item.id;
    const row = h.node('div', 'library-nav-row');
    const text = h.node('div', 'library-nav-text');
    text.append(h.node('span', 'library-nav-title', item.title), h.node('span', `library-nav-state ls-${st.key}`, st.words));
    row.append(text);
    const act = actionOf(item);
    if (act) {
      const b = h.node('button', 'library-nav-action', act === 'Download' ? 'Download' : act === 'Update' ? 'Update' : 'Set up');
      b.type = 'button';
      b.setAttribute('aria-label', `${act}: ${item.title}`);
      b.addEventListener('click', () => {
        if (item.disk.state !== 'absent' && item.disk.data.sessions) {
          // A folder with data: the Experiments page asks first.
          showOnHome(item.id);
        } else {
          download(item, false);
        }
      });
      row.append(b);
    } else if (st.key === 'ask' || st.key === 'bad') {
      const b = h.node('button', 'library-nav-action', 'Details');
      b.type = 'button';
      b.addEventListener('click', () => showOnHome(item.id));
      row.append(b);
    }
    group.append(row);
    return group;
  }

  async function showOnHome(id) {
    await h.navigate(null, null);
    const row = document.querySelector(`#library-home [data-copy="${CSS.escape(id)}"]`);
    if (row) { row.scrollIntoView({block: 'center'}); row.querySelector('button')?.focus(); }
  }

  function drawNav() {
    const nav = $('library-nav');
    if (!nav) return;
    const items = data && data.configured ? data.items.filter((x) => !x.project) : [];
    const parts = [];
    if (data && data.configured && (items.length || data.error)) {
      const head = h.node('div', 'library-nav-head');
      head.append(h.node('span', 'library-nav-label', `Library · ${data.source ? data.source.label : ''}`));
      parts.push(head);
      if (data.error) parts.push(h.node('p', 'library-nav-error', 'Not reachable now'));
      for (const item of items) parts.push(navItem(item));
    }
    nav.replaceChildren(...parts);
    if (parts.length) $('nav-experiment').hidden = false;
  }

  /* ---------------------------------------------------------------- Experiments page */
  function homeRow(item) {
    const st = stateOf(item);
    const row = h.node('div', `library-row ls-row-${st.key}`);
    row.dataset.copy = item.id;
    const main = h.node('div', 'library-main');
    const title = h.node('div', 'library-title', item.title);
    const sub = [];
    if (item.from && item.from !== item.title) sub.push(`From ${item.from}${item.by ? ' by ' + item.by : ''}`);
    else if (item.by) sub.push(`By ${item.by}`);
    if (item.revision) sub.push(`revision ${item.revision}`);
    main.append(title, h.node('div', 'library-sub', sub.join(' · ')));
    const disk = item.disk;
    const facts = [];
    if (disk.state !== 'absent') facts.push(disk.path);
    if (disk.data.sessions) facts.push(sessions(disk.data.sessions) + (disk.data.folders.length ? ` in ${disk.data.folders.join(', ')}` : ''));
    if (disk.state === 'outdated' && disk.marker) facts.push(`folder holds revision ${disk.marker.bundle}`);
    if (facts.length) main.append(h.node('div', 'library-disk', facts.join(' · ')));
    const job = item.job;
    if (job && job.status === 'done' && job.message && disk.state === 'current') main.append(h.node('div', 'library-note ls-ok', job.message));
    if (job && job.status === 'failed') main.append(h.node('div', 'library-note ls-bad', job.message));
    const side = h.node('div', 'library-side');
    side.append(h.node('span', `library-state ls-${st.key}`, st.words));
    const actions = h.node('div', 'library-actions');
    if (item.project) actions.append(h.link('library-open', 'Open', item.project, 'run'));
    const act = actionOf(item);
    if (act && !(job && job.status === 'conflict' && !kept.has(item.id))) {
      const b = h.node('button', act === 'Download' || act === 'Set up' ? 'primary library-go' : 'library-go', act);
      b.type = 'button';
      b.addEventListener('click', () => {
        if (disk.state !== 'absent' && disk.data.sessions && disk.state !== 'current') ask(row, item);
        else download(item, false);
      });
      actions.append(b);
    }
    side.append(actions);
    row.append(main, side);
    if (job && job.status === 'conflict' && !kept.has(item.id)) row.append(question(item, job.message));
    return row;
  }

  /* The question a folder with data asks before its files change. */
  function question(item, message) {
    const box = h.node('div', 'library-question');
    box.setAttribute('role', 'group');
    box.setAttribute('aria-label', `Update ${item.title}?`);
    const disk = item.disk;
    const n = disk.data.sessions;
    box.append(
      h.node('p', 'library-question-text', message || `${disk.path} holds ${sessions(n)}.`),
      h.node('p', 'library-question-detail', 'Updating writes this revision’s files over the old ones and deletes nothing: the saved sessions, the environment and any file you added stay. Keeping it leaves the folder exactly as it is.'),
    );
    const buttons = h.node('div', 'library-question-actions');
    const go = h.node('button', 'primary', 'Update the files, keep the data');
    go.type = 'button';
    go.addEventListener('click', () => download(item, true));
    const keep = h.node('button', '', 'Keep as it is');
    keep.type = 'button';
    keep.addEventListener('click', () => { kept.add(item.id); draw(); });
    buttons.append(go, keep);
    box.append(buttons);
    return box;
  }

  function ask(row, item) {
    if (row.querySelector('.library-question')) return;
    const box = question(item, `${item.disk.path} holds ${item.disk.state === 'outdated' ? 'revision ' + item.disk.marker.bundle : 'files this library did not write'} and ${sessions(item.disk.data.sessions)}.`);
    row.append(box);
    box.querySelector('button').focus();
  }

  function drawHome() {
    const home = $('library-home');
    if (!home) return;
    if (!data || !data.configured) { home.hidden = true; home.replaceChildren(); return; }
    home.hidden = false;
    const head = h.node('div', 'library-head');
    const words = h.node('div', 'library-head-text');
    words.append(h.node('h2', '', 'Library'));
    if (data.source) words.append(h.node('p', 'library-source', `Your copies on ${data.source.label}. Downloads go to ${data.source.copies}.`));
    const again = h.node('button', '', refreshing ? 'Refreshing…' : 'Refresh');
    again.type = 'button';
    again.disabled = refreshing;
    again.addEventListener('click', () => refresh());
    head.append(words, again);
    const parts = [head];
    if (data.error) parts.push(h.node('p', 'library-error', data.error));
    if (!data.items.length && !data.error) parts.push(h.node('p', 'library-empty', data.fetched ? 'Your library is empty. Add experiments to it on the platform.' : 'Loading the library…'));
    if (data.items.length) {
      const list = h.node('div', 'library-list');
      list.append(...data.items.map(homeRow));
      parts.push(list);
    }
    home.replaceChildren(...parts);
  }

  function draw() {
    const next = JSON.stringify([data, [...kept], refreshing]);
    if (next === signature) return;
    // Keep the reader's place: a redraw replaces the buttons.
    const focused = document.activeElement?.closest?.('[data-copy]')?.dataset.copy;
    signature = next;
    drawNav();
    drawHome();
    if (focused) document.querySelector(`#library-home [data-copy="${CSS.escape(focused)}"] button`)?.focus({preventScroll: true});
  }

  window.WorkspaceLibrary = {
    /** Begin: read the library, ask the platform once, keep both places drawn. */
    start(helpers) {
      h = helpers;
      load().then(() => { if (data && data.configured) refresh(); }).catch((e) => h.error(e.message));
    },
    /** workspace.js redrew the sidebar or the Experiments page: draw ours again. */
    redraw() { signature = ''; if (h && data) draw(); },
    /* For tests. */
    _stateOf: stateOf,
    _actionOf: actionOf,
  };
  // workspace.js ran first (deferred scripts run in order): start with its helpers.
  if (typeof pageHelpers === 'function') window.WorkspaceLibrary.start(pageHelpers());
})();
