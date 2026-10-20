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
 *
 * The page keeps asking while no library is connected, so a connection made
 * while it is open (alhazen library connect, the form below, or Mana's kit
 * writing library.json) shows up without a reload; the server reads the list
 * in the background the first time it sees a connection and says so
 * (`refreshing`), and the page asks often until that read is done. Without a
 * connection, the Experiments page offers the form: the platform's address
 * and a machine token from its Machines page (POST /api/library/connect).
 */
'use strict';

(function () {
  const POLL_BUSY_MS = 1500;
  const POLL_IDLE_MS = 20000;
  // No library yet: ask now and then whether one was connected meanwhile.
  const POLL_UNCONNECTED_MS = 5000;
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
  let connectOpen = false;      // the connection form is shown
  let connecting = false;
  let connectError = '';

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

  /** How long until the page asks again (ms), from the last answer. */
  function nextPoll(answer) {
    if (!answer || !answer.configured) return POLL_UNCONNECTED_MS;
    const busy = answer.refreshing || answer.items.some((x) => x.job && x.job.status === 'running');
    return busy ? POLL_BUSY_MS : POLL_IDLE_MS;
  }

  /** The list just arrived (a background read finished, a connection
   * appeared): copies on disk may have been registered meanwhile. */
  function arrived(before, after) {
    if (!after || !after.configured) return false;
    if (!before || !before.configured) return !after.refreshing && after.fetched != null;
    return !!before.refreshing && !after.refreshing;
  }

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
    timer = setTimeout(() => {
      const previous = data;
      const before = new Set((data && data.items || []).filter((x) => x.job && x.job.status === 'running').map((x) => x.id));
      h.api('/api/library').then((next) => {
        data = next;
        const finished = data.items.filter((x) => before.has(x.id) && x.job && x.job.status !== 'running');
        draw();
        schedule();
        if (arrived(previous, data)) h.refresh();
        if (finished.length) whenFinished(finished);
      }).catch((e) => { h.error(e.message); schedule(); });
    }, nextPoll(data));
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

  async function connect(form) {
    const url = form.querySelector('[name="url"]').value.trim();
    const token = form.querySelector('[name="token"]').value.trim();
    const label = form.querySelector('[name="label"]').value.trim();
    if (!url) { connectError = 'Give the platform’s address.'; draw(); return; }
    connecting = true;
    connectError = '';
    draw();
    try {
      data = await h.api('/api/library/connect', {url, token, label});
      connectOpen = false;
      formEl = null;
      h.refresh();
    } catch (e) {
      connectError = e.message;
    } finally {
      connecting = false;
    }
    signature = '';
    draw();
    schedule();
  }

  /* The connection form: the platform's address and a machine token. One
   * element while it is open, so a redraw (a poll, a download's progress)
   * never clears what is being typed; only its message and button change. */
  let formEl = null;
  function connectForm() {
    if (!formEl) formEl = buildForm();
    const message = formEl.querySelector('.library-connect-message');
    message.textContent = connectError;
    message.hidden = !connectError;
    const go = formEl.querySelector('button[type="submit"]');
    go.textContent = connecting ? 'Connecting…' : 'Connect';
    go.disabled = connecting;
    return formEl;
  }

  function closeForm() { connectOpen = false; connectError = ''; formEl = null; draw(); }

  function buildForm() {
    const form = h.node('form', 'library-connect');
    form.noValidate = true;
    const field = (name, label, type, hint, placeholder) => {
      const wrap = h.node('label', 'library-field');
      const input = h.node('input', '');
      input.name = name;
      input.type = type;
      input.autocomplete = 'off';
      input.spellcheck = false;
      if (placeholder) input.placeholder = placeholder;
      wrap.append(h.node('span', 'library-field-label', label), input);
      if (hint) wrap.append(h.node('span', 'library-field-hint', hint));
      return wrap;
    };
    form.append(
      field('url', 'Platform address', 'url', '', 'https://platform.example.org'),
      field('token', 'Machine token', 'password', 'Make one on the platform’s Machines page. A local platform’s link with #token= needs none.', ''),
      field('label', 'Name on this page', 'text', '', 'Library'),
    );
    const message = h.node('p', 'library-error library-connect-message', '');
    message.setAttribute('role', 'alert');
    message.hidden = true;
    form.append(message);
    const actions = h.node('div', 'library-connect-actions');
    const go = h.node('button', 'primary', 'Connect');
    go.type = 'submit';
    const cancel = h.node('button', '', 'Cancel');
    cancel.type = 'button';
    cancel.addEventListener('click', () => closeForm());
    actions.append(go, cancel);
    form.append(actions);
    form.addEventListener('submit', (e) => { e.preventDefault(); connect(form); });
    return form;
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

  function drawUnconnected(home) {
    const head = h.node('div', 'library-head');
    const words = h.node('div', 'library-head-text');
    words.append(
      h.node('h2', '', 'Library'),
      h.node('p', 'library-source', 'Connect this dashboard to your library on an Experiment Platform to list your copies here and download one when you run it.'),
    );
    head.append(words);
    if (!connectOpen) {
      const open = h.node('button', '', 'Connect a library');
      open.type = 'button';
      open.addEventListener('click', () => {
        connectOpen = true;
        draw();
        home.querySelector('input[name="url"]')?.focus();
      });
      head.append(open);
    }
    home.replaceChildren(head, ...(connectOpen ? [connectForm()] : []));
  }

  function drawHome() {
    const home = $('library-home');
    if (!home) return;
    if (!data) { home.hidden = true; home.replaceChildren(); return; }
    home.hidden = false;
    if (!data.configured) { drawUnconnected(home); return; }
    const head = h.node('div', 'library-head');
    const words = h.node('div', 'library-head-text');
    words.append(h.node('h2', '', 'Library'));
    if (data.source) words.append(h.node('p', 'library-source', `Your copies on ${data.source.label}. Downloads go to ${data.source.copies}.`));
    const busy = refreshing || data.refreshing;
    const again = h.node('button', '', busy ? 'Refreshing…' : 'Refresh');
    again.type = 'button';
    again.disabled = busy;
    again.addEventListener('click', () => refresh());
    const tools = h.node('div', 'library-head-actions');
    const other = h.node('button', 'quiet', connectOpen ? 'Close' : 'Connect another');
    other.type = 'button';
    other.addEventListener('click', () => { if (connectOpen) closeForm(); else { connectOpen = true; draw(); } });
    tools.append(other, again);
    head.append(words, tools);
    const parts = [head];
    if (connectOpen) parts.push(connectForm());
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
    const next = JSON.stringify([data, [...kept], refreshing, connectOpen, connecting, connectError]);
    if (next === signature) return;
    // Keep the reader's place: a redraw replaces the buttons.
    const focused = document.activeElement?.closest?.('[data-copy]')?.dataset.copy;
    // A field of the connection form keeps its focus and caret across a redraw.
    const typing = formEl && formEl.contains(document.activeElement) ? document.activeElement : null;
    const caret = typing && typeof typing.selectionStart === 'number' ? [typing.selectionStart, typing.selectionEnd] : null;
    signature = next;
    drawNav();
    drawHome();
    if (focused) document.querySelector(`#library-home [data-copy="${CSS.escape(focused)}"] button`)?.focus({preventScroll: true});
    if (typing && document.activeElement !== typing) {
      typing.focus({preventScroll: true});
      if (caret) { try { typing.setSelectionRange(...caret); } catch (e) { /* not a text field */ } }
    }
  }

  window.WorkspaceLibrary = {
    /** Begin: read the library, ask the platform once, keep both places drawn. */
    start(helpers) {
      h = helpers;
      // A connection read before (the server keeps its list) is asked again
      // once; a new one the server is already reading in the background.
      load().then(() => { if (data && data.configured && !data.refreshing) refresh(); }).catch((e) => { h.error(e.message); schedule(); });
    },
    /** workspace.js redrew the sidebar or the Experiments page: draw ours again. */
    redraw() { signature = ''; if (h && data) draw(); },
    /** The connected library's name, or null (the Shut down panel says it is told). */
    connected() { return data && data.configured && data.source ? data.source.label : null; },
    /* For tests. */
    _stateOf: stateOf,
    _actionOf: actionOf,
    _nextPoll: nextPoll,
    _arrived: arrived,
  };
  // workspace.js ran first (deferred scripts run in order): start with its helpers.
  if (typeof pageHelpers === 'function') window.WorkspaceLibrary.start(pageHelpers());
})();
