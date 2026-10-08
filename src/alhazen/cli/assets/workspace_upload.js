/* Alhazen experiment workspace — Upload to the archive.
 *
 * One module, three mounts, one upload at a time (the server's rule too):
 *
 *   ArchiveUpload.mountSession(container, ctx, launchId)
 *       the Run page's card for the session a finished launch saved: the
 *       destination, its upload state, Preview and Upload to the archive.
 *   ArchiveUpload.mountBatch(container, ctx, selection)
 *       the bar under a list of sessions (History, Data): upload the
 *       checked sessions or all of them. `selection()` answers
 *       {groups: [{root, runs?|all}], count, label}.
 *   ArchiveUpload.settings(container, ctx)
 *       the settings form, opened from either.
 *
 * ctx = {api, node, project}: the page's JSON fetcher (adds the token),
 * element helper and the experiment's id. Everything shown comes from
 * files and settings on disk, so text is set with textContent only.
 *
 * The routes are workspace_upload.py's (/api/upload/...). While an upload
 * runs, the module polls its job once a second and redraws every mounted
 * view; nothing polls when none is running.
 *
 * The archive's name on the page ("Upload to <name>") is the settings'
 * `label`, typed in on this computer; the code ships only "archive".
 */
'use strict';

(function () {
  const POLL_MS = 1000;
  const ACTIVE = ['starting', 'copying', 'verifying'];
  const LAMPS = {
    verified: ['ok', 'VERIFIED'],
    conflict: ['bad', 'DIFFERS THERE'],
    incomplete: ['bad', 'INCOMPLETE'],
    failed: ['bad', 'UPLOAD FAILED'],
    cancelled: ['off', 'UPLOAD STOPPED'],
    unreadable: ['bad', 'RECEIPT UNREADABLE'],
  };
  let job = null;
  let timer = null;
  let lastPhase = null;
  /* The archive's name from the settings (`label`), once read. */
  let label = 'archive';
  let labelRead = null;
  const views = new Set();

  function el(ctx, tag, className, text) {
    return ctx.node(tag, className, text);
  }

  function button(ctx, text, className, onClick) {
    const b = el(ctx, 'button', className, text);
    b.type = 'button';
    b.addEventListener('click', onClick);
    return b;
  }

  function size(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    if (bytes < 1024 ** 3) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
    return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
  }

  /** A status lamp: a dot and a word, the dashboard's status language. */
  function lamp(ctx, kind, text) {
    const box = el(ctx, 'span', `up-lamp up-lamp-${kind}`);
    box.append(el(ctx, 'span', 'up-lamp-dot'), el(ctx, 'span', '', text));
    return box;
  }

  function stateLamp(ctx, upload) {
    if (!upload) return lamp(ctx, 'off', 'NOT UPLOADED');
    const [kind, text] = LAMPS[upload.status] || ['off', String(upload.status).toUpperCase()];
    return lamp(ctx, kind, text);
  }

  function message(ctx, kind, text) {
    const box = el(ctx, 'div', `up-message up-${kind}`);
    box.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    box.textContent = text;
    return box;
  }

  /** Read the archive's name once (again after the settings are saved). */
  function readLabel(ctx, again = false) {
    if (!labelRead || again) {
      labelRead = ctx.api('/api/upload/settings').then((answer) => {
        label = answer.settings.label || 'archive';
        redraw();
      }).catch(() => {});
    }
    return labelRead;
  }

  /* -- the job, shared by every mounted view -------------------------- */

  function redraw() {
    for (const view of views) {
      if (!view.container.isConnected) views.delete(view);
      else view.draw();
    }
  }

  async function poll(ctx) {
    clearTimeout(timer);
    timer = null;
    try {
      job = (await ctx.api('/api/upload/job')).job;
    } catch (e) {
      job = job ? {...job, pollError: e.message} : null;
    }
    const finished = lastPhase && ACTIVE.includes(lastPhase) && !running();
    lastPhase = job?.phase || null;
    redraw();
    // An upload that just ended wrote receipts: the views that show a
    // session's state read it again.
    if (finished) for (const view of views) view.refresh?.();
    if (running()) timer = setTimeout(() => poll(ctx), POLL_MS);
  }

  function running() {
    return job && ACTIVE.includes(job.phase);
  }

  /** The progress line of the running (or last) upload, for any view. */
  function progressBlock(ctx, keys) {
    if (!job || job.project !== ctx.project) return null;
    const mine = !keys || job.groups.some((g) => g.runs.some((r) => keys.has(`${g.root}:${r}`)));
    if (!mine) return null;
    const box = el(ctx, 'div', 'up-progress');
    const p = job.progress || {};
    const fraction = p.bytes_total ? p.bytes_done / p.bytes_total : 0;
    const phase = {
      starting: 'Checking what is already on the archive…',
      copying: `Copying ${size(p.bytes_done || 0)} of ${size(p.bytes_total || 0)}`,
      verifying: 'Verifying every file by content…',
      done: 'Upload finished',
      failed: 'Upload failed',
      cancelled: 'Upload stopped — the next upload resumes it',
    }[job.phase] || job.phase;
    const head = el(ctx, 'div', 'up-progress-head');
    head.append(el(ctx, 'span', 'up-progress-phase', phase));
    if (running()) {
      head.append(button(ctx, 'Stop', 'quiet up-stop', async () => {
        try {
          await ctx.api('/api/upload/cancel', {});
        } catch (e) {
          box.append(message(ctx, 'error', e.message));
        }
      }));
    }
    const bar = el(ctx, 'div', 'up-bar');
    bar.setAttribute('role', 'progressbar');
    bar.setAttribute('aria-valuemin', '0');
    bar.setAttribute('aria-valuemax', '100');
    const percent = Math.round((job.phase === 'done' ? 1 : fraction) * 100);
    bar.setAttribute('aria-valuenow', String(percent));
    const fill = el(ctx, 'div', 'up-bar-fill');
    fill.style.width = `${percent}%`;
    bar.append(fill);
    box.append(head, bar);
    if (job.error) box.append(message(ctx, 'error', job.error));
    if (job.pollError) box.append(message(ctx, 'error', job.pollError));
    const results = Object.values(job.results || {});
    if (results.length && !running()) {
      const count = (s) => results.filter((r) => r.status === s).length;
      const parts = [];
      if (count('verified')) parts.push(`${count('verified')} verified`);
      if (count('conflict')) parts.push(`${count('conflict')} with files that differ on the archive`);
      if (count('incomplete')) parts.push(`${count('incomplete')} incomplete`);
      if (count('failed')) parts.push(`${count('failed')} failed`);
      box.append(el(ctx, 'p', 'up-note', `${parts.join(' · ')}. A receipt is kept with each session.`));
    }
    for (const reason of job.skipped || []) box.append(message(ctx, 'note', `Skipped ${reason}`));
    return box;
  }

  /* -- connection and settings --------------------------------------------- */

  async function connection(ctx, where) {
    where.replaceChildren(el(ctx, 'p', 'up-note', 'Checking the connection…'));
    let answer;
    try {
      answer = await ctx.api('/api/upload/check');
    } catch (e) {
      answer = {ok: false, message: e.message};
    }
    where.replaceChildren();
    if (answer.ok) {
      where.append(lamp(ctx, 'ok', answer.message));
      return true;
    }
    where.append(message(ctx, 'error', answer.message));
    if (answer.command) {
      const row = el(ctx, 'div', 'up-command');
      const code = el(ctx, 'code', '', answer.command);
      row.append(code, button(ctx, 'Copy', 'quiet', async () => {
        try {
          await navigator.clipboard.writeText(answer.command);
        } catch (e) {
          // Clipboard refused (an http page in some browsers): select the
          // text instead so the operator can copy it by hand.
          const range = document.createRange();
          range.selectNodeContents(code);
          window.getSelection().removeAllRanges();
          window.getSelection().addRange(range);
        }
      }), button(ctx, 'Check again', 'quiet', () => connection(ctx, where)));
      where.append(row);
    }
    return false;
  }

  async function settings(container, ctx, onSaved) {
    container.replaceChildren(el(ctx, 'p', 'up-note', 'Reading the upload settings…'));
    let current;
    try {
      current = await ctx.api('/api/upload/settings');
    } catch (e) {
      container.replaceChildren(message(ctx, 'error', e.message));
      return;
    }
    const s = current.settings;
    const form = el(ctx, 'form', 'up-settings');
    const fields = {};
    const field = (key, label, hint, attrs = {}) => {
      const box = el(ctx, 'div', 'up-field');
      const input = el(ctx, 'input');
      input.id = `up-${key}`;
      input.value = s[key] ?? '';
      for (const [k, v] of Object.entries(attrs)) input.setAttribute(k, v);
      const l = el(ctx, 'label', '', label);
      l.htmlFor = input.id;
      box.append(l, input);
      if (hint) box.append(el(ctx, 'p', 'up-hint', hint));
      fields[key] = input;
      return box;
    };
    const kind = el(ctx, 'div', 'segmented up-kind');
    kind.setAttribute('role', 'group');
    kind.setAttribute('aria-label', 'Upload to');
    let transport = s.transport;
    const sshBox = el(ctx, 'div', 'up-fields');
    const localBox = el(ctx, 'div', 'up-fields');
    const choose = (value) => {
      transport = value;
      for (const b of kind.children) b.setAttribute('aria-pressed', String(b.dataset.value === value));
      sshBox.hidden = value !== 'ssh';
      localBox.hidden = value !== 'local';
    };
    for (const [value, text] of [['ssh', 'Remote host over SSH'],
      ['local', 'A folder on this computer']]) {
      const b = button(ctx, text, '', () => choose(value));
      b.dataset.value = value;
      kind.append(b);
    }
    sshBox.append(
      field('user', 'Login user', 'The account the SSH connection logs in as.',
        {autocomplete: 'off', spellcheck: 'false', placeholder: 'e.g. alice'}),
      field('host', 'Remote host', 'The host that can write to the archive.',
        {spellcheck: 'false', placeholder: 'e.g. archive.example.org'}),
      field('base_path', 'Remote base path', 'Each experiment gets a folder of its own name here.',
        {spellcheck: 'false', placeholder: '/path/to/remote/data'}),
      field('control_path', 'Connection socket', `Where the connection you open once (password
        and any second factor) is kept, so uploads never ask again. Empty: SSH keys or
        Kerberos instead.`, {spellcheck: 'false'}),
    );
    localBox.append(field('local_path', 'Folder', 'A mounted share or a backup disk.',
      {spellcheck: 'false', placeholder: '/path/to/backup/data'}));
    const name = field('label', 'Name on this page', 'What the buttons call the archive: '
      + '“Upload to <name>”.', {spellcheck: 'false', placeholder: 'archive'});
    choose(transport);
    const status = el(ctx, 'div');
    const save = el(ctx, 'button', 'primary', 'Save settings');
    save.type = 'submit';
    const actions = el(ctx, 'div', 'up-actions');
    actions.append(save);
    form.append(el(ctx, 'p', 'up-eyebrow', 'UPLOAD SETTINGS · THIS COMPUTER'), name, kind,
      sshBox, localBox, status, actions);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const body = {...s, transport};
      for (const [key, input] of Object.entries(fields)) body[key] = input.value;
      try {
        await ctx.api('/api/upload/settings', {settings: body});
      } catch (e) {
        status.replaceChildren(message(ctx, 'error', e.message));
        return;
      }
      status.replaceChildren(message(ctx, 'ok', 'Saved on this computer.'));
      readLabel(ctx, true);
      if (onSaved) onSaved();
    });
    container.replaceChildren(form);
  }

  /* -- preview and start ------------------------------------------------- */

  function previewTable(ctx, preview) {
    const box = el(ctx, 'div', 'up-preview');
    for (const group of preview.groups) {
      box.append(el(ctx, 'p', 'up-dest', group.destination));
      const table = el(ctx, 'table', 'up-table');
      const head = el(ctx, 'tr');
      for (const c of ['Session', 'To copy', 'Already there', 'Differs there', 'State']) {
        head.append(el(ctx, 'th', '', c));
      }
      table.append(head);
      for (const s of group.sessions) {
        const tr = el(ctx, 'tr');
        const state = el(ctx, 'td');
        state.append(s.complete ? stateLamp(ctx, s.upload) : lamp(ctx, 'bad', 'NO MANIFEST'));
        tr.append(
          el(ctx, 'td', 'up-mono', s.run),
          el(ctx, 'td', 'up-mono', s.new ? `${s.new} · ${size(s.new_bytes)}` : '—'),
          el(ctx, 'td', 'up-mono', s.present ? String(s.present) : '—'),
          el(ctx, 'td', s.conflict ? 'up-mono up-bad' : 'up-mono',
            s.conflict ? `${s.conflict}: ${s.conflicts.join(', ')}` : '—'),
          state,
        );
        table.append(tr);
      }
      box.append(table);
    }
    for (const reason of preview.skipped) box.append(message(ctx, 'note', `Skipped ${reason}`));
    return box;
  }

  function totals(preview) {
    let files = 0;
    let bytes = 0;
    let conflicts = 0;
    let incomplete = 0;
    for (const g of preview.groups) {
      for (const s of g.sessions) {
        files += s.new;
        bytes += s.new_bytes;
        conflicts += s.conflict;
        if (!s.complete) incomplete += 1;
      }
    }
    return {files, bytes, conflicts, incomplete};
  }

  /** The preview, then a confirm. `where` holds it; `selection` the request. */
  async function previewThenConfirm(ctx, where, selection, onStarted) {
    where.replaceChildren(el(ctx, 'p', 'up-note', 'Comparing with the archive (dry run)…'));
    const ready = el(ctx, 'div');
    let preview;
    try {
      preview = await ctx.api('/api/upload/preview', {project: ctx.project, selection});
    } catch (e) {
      where.replaceChildren(message(ctx, 'error', e.message), ready);
      connection(ctx, ready);
      return;
    }
    const t = totals(preview);
    const box = el(ctx, 'div', 'up-confirm');
    box.append(previewTable(ctx, preview));
    let summary = t.files
      ? `${t.files} file${t.files === 1 ? '' : 's'} (${size(t.bytes)}) to copy.`
      : 'Everything is already there; uploading again only verifies it.';
    if (t.conflicts) {
      summary += ` ${t.conflicts} file${t.conflicts === 1 ? '' : 's'} differ on the archive and `
        + 'will be left as they are (never overwritten).';
    }
    box.append(el(ctx, 'p', 'up-summary', summary));
    let includeIncomplete = null;
    if (t.incomplete) {
      const label = el(ctx, 'label', 'up-check');
      includeIncomplete = el(ctx, 'input');
      includeIncomplete.type = 'checkbox';
      label.append(includeIncomplete, el(ctx, 'span', '',
        `Also upload ${t.incomplete} session${t.incomplete === 1 ? '' : 's'} without a manifest `
        + '(it never finished, or is still being written)'));
      box.append(label);
    }
    const status = el(ctx, 'div');
    const actions = el(ctx, 'div', 'up-actions');
    const go = button(ctx, '⇪ Upload now', 'primary', async () => {
      go.disabled = true;
      try {
        await ctx.api('/api/upload/start', {
          project: ctx.project, selection,
          include_incomplete: !!includeIncomplete?.checked,
        });
      } catch (e) {
        go.disabled = false;
        status.replaceChildren(message(ctx, 'error', e.message));
        return;
      }
      where.replaceChildren();
      if (onStarted) onStarted();
      poll(ctx);
    });
    actions.append(go, button(ctx, 'Cancel', 'quiet', () => where.replaceChildren()));
    box.append(status, actions);
    where.replaceChildren(box);
    go.focus({preventScroll: true});
  }

  /* -- the Run page: one session ----------------------------------------- */

  function mountSession(container, ctx, launchId) {
    const view = {container, ctx, session: undefined, launchId};
    const panel = el(ctx, 'div', 'up-panel');
    container.replaceChildren(panel);
    let loaded = 0;
    view.draw = () => {
      const s = view.session;
      container.hidden = !s;
      if (!s) return;
      const keys = new Set([`${s.root}:${s.run}`]);
      const head = el(ctx, 'div', 'up-head');
      const titles = el(ctx, 'div');
      titles.append(
        el(ctx, 'p', 'up-eyebrow', s.root_kind === 'real' ? `ARCHIVE · ${label.toUpperCase()}`
          : `ARCHIVE · ${label.toUpperCase()} · REHEARSAL DATA`),
        el(ctx, 'h2', 'up-title', 'Upload this session'),
      );
      head.append(titles, stateLamp(ctx, s.upload));
      const where = el(ctx, 'p', 'up-dest');
      where.append(el(ctx, 'span', 'up-run', s.run));
      where.append(el(ctx, 'span', 'up-arrow', ' → '),
        el(ctx, 'span', '', view.destination || 'the archive'));
      const actions = el(ctx, 'div', 'up-actions');
      const busy = running();
      const upload = button(ctx, busy ? 'An upload is running' : `⇪ Upload to ${label}`,
        'primary up-go', () => previewThenConfirm(ctx, view.box, [{root: s.root, runs: [s.run]}],
          () => refresh()));
      upload.disabled = busy;
      actions.append(upload,
        button(ctx, 'Settings', 'quiet', () => settings(view.box, ctx, () => refresh())));
      const notes = el(ctx, 'div');
      if (!s.complete) {
        notes.append(message(ctx, 'note', 'This session has no manifest yet: it did not finish '
          + 'teardown. You can still upload it; the preview asks.'));
      }
      if (s.upload?.finished) {
        notes.append(el(ctx, 'p', 'up-note', `Last upload ${new Date(s.upload.finished)
          .toLocaleString()} · ${s.upload.attempts} receipt${s.upload.attempts === 1 ? '' : 's'}`));
      }
      view.box = view.box || el(ctx, 'div', 'up-box');
      panel.replaceChildren(head, where, actions, notes,
        progressBlock(ctx, keys) || el(ctx, 'span'), view.box);
    };
    const refresh = async () => {
      const ticket = ++loaded;
      let answer;
      try {
        answer = await ctx.api(`/api/upload/launch-session?project=${encodeURIComponent(ctx.project)}`
          + `&launch=${encodeURIComponent(launchId)}`);
      } catch (e) {
        if (ticket !== loaded) return;
        container.hidden = false;
        panel.replaceChildren(message(ctx, 'error', e.message));
        return;
      }
      if (ticket !== loaded) return;
      view.session = answer.session;
      view.destination = answer.session?.destination
        || answer.session?.destination_problem || 'the archive';
      view.draw();
    };
    view.refresh = refresh;
    views.add(view);
    readLabel(ctx);
    refresh();
    poll(ctx);
    return view;
  }

  /* -- History and Data: several sessions ---------------------------------- */

  function mountBatch(container, ctx, selection) {
    const view = {container, ctx};
    const box = el(ctx, 'div', 'up-box');
    view.draw = () => {
      const chosen = selection();
      const bar = el(ctx, 'div', 'up-batch');
      bar.append(el(ctx, 'span', 'up-eyebrow', `UPLOAD TO ${label.toUpperCase()}`));
      const busy = running();
      const some = button(ctx, chosen.count ? `⇪ Upload ${chosen.count} selected`
        : 'Select sessions to upload', 'primary', () => previewThenConfirm(ctx, box,
        chosen.groups));
      some.disabled = busy || !chosen.count;
      const all = button(ctx, `Upload all${chosen.allLabel ? ` ${chosen.allLabel}` : ''}`, 'quiet',
        () => previewThenConfirm(ctx, box, chosen.all));
      all.disabled = busy || !chosen.all?.length;
      bar.append(some, all, button(ctx, 'Settings', 'quiet', () => settings(box, ctx)));
      const parts = [bar];
      const progress = progressBlock(ctx, null);
      if (progress) parts.push(progress);
      parts.push(box);
      container.replaceChildren(...parts);
    };
    view.refresh = () => selection().after?.();
    views.add(view);
    readLabel(ctx);
    view.draw();
    poll(ctx);
    return view;
  }

  /** History's state cell for a session: its newest receipt as a lamp. */
  function stateCell(ctx, upload) {
    return stateLamp(ctx, upload);
  }

  window.ArchiveUpload = {mountSession, mountBatch, settings, stateCell, poll};
}());
