// Mana host adapter: the Alhazen Experiment Platform as a Mana user_div.
// Mana injects React, FS, mana, useStorageUrl, relPathAddress, userId, ownerId, openChat, copyText,
// useTheme, ReactMarkdown and remarkGfm; this file maps them onto the host interface PlatformApp uses
// (design.md section 7). Storage layout (unchanged from the approved Mana app):
// Data beneath the app node: published/<uid>/<exp> listings, bundles/<uid>/<id> sources,
// docs/<uid>/<exp>, kit/hubkit (agent toolkit), assets (hero, mark).
// Each viewer's own forks: <viewer>/file-system/alhazen-experiment-platform-mine (forks, bundles, docs).
const TERMINAL_LISTING = '/home/JGs3HVBXEJdU5xyEXc9WwkGT05y2/shared/LtTbJMTjunRRHrceu2JpyYCZoo82/mana-terminal';
const MANA_FONT_STYLESHEET = 'https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&display=swap';
const get = async (path) => (await FS.get(FS.ref(FS.db, path))).val();

function kitLoader(kitPath) {
  return 'from firebase_helper import firebase\nimport types\nhubkit = types.ModuleType("hubkit")\nexec(firebase.read("' + kitPath + '"), hubkit.__dict__)';
}

function changePrompt({ fork, forkId, jobId, request, kitPath, minePath, fromScratch }) {
  return [
    fromScratch ? '[Alhazen Experiment Platform · new experiment]' : '[Alhazen Experiment Platform · change request]',
    (fromScratch ? 'New experiment' : 'Experiment') + ': ' + fork.title + ' (fork id ' + forkId + '), my copy at ' + minePath + '/forks/' + forkId + '.',
    'Request: ' + request,
    '',
    'How to work (Alhazen Experiment Platform kit; read its docstring once):',
    '1. Load the kit in Python:\n```python\n' + kitLoader(kitPath) + '\n```',
    fromScratch
      ? '2. Scaffold it in /workspace/experiment-hub/forks/' + forkId + ' with alhazen\'s own generator: `UV_CACHE_DIR=/tmp/uv-cache uvx --from git+https://github.com/sh4r11f/alhazen@v2.16.0 alhazen new <package-name> --into /workspace/experiment-hub/forks/' + forkId + '` (move the generated package so pyproject.toml sits at that folder\'s top level), then pin alhazen-vision by git tag in [tool.uv.sources] like the hub\'s experiments do and run `uv lock`. Then build what the request describes.'
      : '2. `d = hubkit.checkout("' + forkId + '")` gives a git working copy. It is an alhazen experiment: read README.md, run.py, configs/ and docs/ before changing anything.',
    '3. Make the change. Keep the experiment\'s conventions and leave behaviour that was not asked about alone. Keep identifiable paths, hostnames and user names out of the source.',
    '4. Check it in a throwaway environment: `UV_CACHE_DIR=/tmp/uv-cache UV_PROJECT_ENVIRONMENT=/tmp/ehub-env/' + forkId + ' uv sync --locked` (run `uv lock` first if you changed dependencies), then run the tests that cover what changed with `uv run pytest -q <files>`.',
    '5. Save a revision: `hubkit.save("' + forkId + '", "<one-line message>", by="mana", chat_id=<get_current_chat_id() from sandbox_runtime_context>)`.',
    '6. Report to the app: `hubkit.set_job("' + forkId + '", "' + jobId + '", {"status": "done" (or "failed"), "summary": "<two plain sentences>", "revision": <bundle id>})`. If you need a decision from me first, set status "waiting" with the question as the summary and ask it in your reply.',
    'Nothing here writes to GitHub or to the shared hub.',
  ].join('\n');
}

function runPrompt({ fork, forkId, jobId, kitPath, minePath, machine }) {
  return [
    '[Alhazen Experiment Platform · run on my machine]',
    'Experiment: ' + fork.title + ' (fork id ' + forkId + '), my copy at ' + minePath + '/forks/' + forkId + '.',
    'Goal: copy this revision to my machine, build its Python environment there from the lock file, and start the alhazen dashboard so I can run sessions.',
    '',
    '1. Load the kit in Python:\n```python\n' + kitLoader(kitPath) + '\n```',
    '2. Mana Terminal must be installed in this Mana (`/workspace/mana_terminal.py`). If it is not, call `hubkit.set_job("' + forkId + '", "' + jobId + '", {"status": "failed", "summary": "Mana Terminal is not installed. Install it from the marketplace, pair your machine, then run again."})` and stop.',
    '3. `from mana_terminal import terms` (add /workspace to sys.path). If no window is open, set the job to status "waiting" with summary "Open a terminal on your machine and type mana, then press Run again." and stop.',
    '4. `res = hubkit.run_on_machine("' + forkId + '", machine=' + (machine ? JSON.stringify(machine) : 'None') + ', psychopy=True, log=print, job_id="' + jobId + '")`. The job id lets the dashboard report back to this job when it closes. Each step that changes my machine is approved by me in the terminal; a refusal (TermDenied) is final: set status "stopped" and do not retry. The kit checks the disk first: if it raises hubkit.CopyHasData, nothing was changed; set the job to status "waiting" with its message as the summary, ask me whether to update the files (the saved sessions stay) or run the folder as it is, and call run_on_machine again with replace=True or keep=True accordingly.',
    '5. If a step fails, read the error and the dashboard log, fix what can be fixed on the machine through the terminal (I approve each change), and retry that step. Do not edit the experiment\'s source here.',
    '6. Finish with `hubkit.set_job("' + forkId + '", "' + jobId + '", {"status": "done", "summary": "<one plain sentence>", "url": res["url"], "dir": res["dir"], "machine": <window label>})`, or status "failed" with what went wrong. Mention in the summary when res["disk"]["action"] is "unchanged" (already there), when res["library"] is not ok, and when res["dashboard"]["older"] is true (the dashboard already open runs an older alhazen than this experiment; its sessions still use the experiment\'s own).',
    'Nothing here writes to GitHub or to the shared hub.',
  ].join('\n');
}

function UserDiv() {
  const HUB = (relPathAddress('published') || '').replace(/\/published$/, '') || null;
  const KIT = relPathAddress('kit', 'hubkit', 'code');
  const MINE = userId ? `${userId}/file-system/alhazen-experiment-platform-mine` : null;
  const art = useStorageUrl('art');
  const lastPublished = React.useRef(null);
  const viewer = userId ? { uid: userId, name: (FS.auth.currentUser && (FS.auth.currentUser.displayName || (FS.auth.currentUser.email || '').split('@')[0])) || 'Unknown' } : null;

  async function ensureMine(upd) {
    const meta = await get(MINE + '/metadata/type');
    if (!meta) {
      const t = Date.now();
      Object.assign(upd, { 'metadata': { type: 'link', hidden: true, displayName: 'Alhazen Experiment Platform: my experiments', createdAt: t, updatedAt: t },
        'forks/metadata': { type: 'link', hidden: true, createdAt: t, updatedAt: t }, 'bundles/metadata': { type: 'link', hidden: true, createdAt: t, updatedAt: t } });
    }
  }

  async function copyBundle(srcPath, destBase, bid, upd, prefix, progress) {
    if (await get(destBase + '/' + bid + '/meta/complete')) return;
    const meta = await get(srcPath + '/meta');
    if (!meta) throw new Error('This listing has no source files.');
    for (let i = 0; i < (meta.chunkCount || 0); i++) {
      progress(`Copying files (${i + 1} of ${meta.chunkCount})`);
      upd[prefix + bid + '/chunks/' + i] = await get(srcPath + '/chunks/' + i);
    }
    upd[prefix + bid + '/meta'] = { ...meta, complete: true, copiedFrom: srcPath, createdAt: Date.now() };
  }

  async function startJob(fork, kind, request, machine) {
    const forkId = fork.id;
    const chatId = await mana.mintChatId();
    const jobId = String(Date.now());
    const text = kind === 'run'
      ? runPrompt({ fork, forkId, jobId, kitPath: KIT, minePath: MINE, machine })
      : changePrompt({ fork, forkId, jobId, request, kitPath: KIT, minePath: MINE, fromScratch: kind === 'create' });
    await FS.update(FS.ref(FS.db, MINE + '/forks/' + forkId + '/jobs/' + jobId), { kind, request: request || '', chatId, status: 'running', startedAt: Date.now(), updatedAt: Date.now(), machine: machine || '' });
    await mana.ask(chatId, text, undefined, undefined, undefined, undefined, true);
    return jobId;
  }

  const host = {
    kind: 'mana',
    key: HUB + '|' + MINE,
    viewer,
    capabilities: { fork: true, publish: true, changeWithMana: true, runOnMachines: true, newWithMana: true,
      uploadRevision: false, downloadRevision: false, machines: true, signIn: false, themeToggle: false },
    art,
    fontStylesheet: MANA_FONT_STYLESHEET,
    extraCss: '',
    links: { terminalListing: TERMINAL_LISTING, terminalApp: `/home/${userId}/Apps/mana-terminal` },
    useTheme,
    copyText: (text) => copyText(text),

    subscribeListings(cb) {
      if (!HUB) return;
      return FS.onValue(FS.ref(FS.db, HUB + '/published'), (s) => { const v = s.val(); lastPublished.current = v; cb(v); });
    },
    subscribeAssets(cb) {
      if (!HUB) return;
      return FS.onValue(FS.ref(FS.db, HUB + '/assets'), (s) => cb(s.val()));
    },
    subscribeForks(cb, onError) {
      if (!MINE) return;
      return FS.onValue(FS.ref(FS.db, MINE + '/forks'), (s) => cb(s.val()), onError);
    },
    subscribeMachines(cb, onError) {
      if (!userId) return;
      return FS.onValue(FS.ref(FS.db, `${userId}/file-system/Apps/mana-terminal/app/data`), (s) => cb(s.val()), onError);
    },
    subscribeDocs(listing, cb, onError) {
      if (!HUB) return;
      return FS.onValue(FS.ref(FS.db, HUB + '/docs/' + listing.uid + '/' + listing.exp), (s) => cb(s.val()), onError);
    },
    // [2026-10-16] Methods for a private copy: the docs snapshot of the version it was forked from,
    // saved beside the copy (MINE/docs/<origin bundle>). Older copies are backfilled from the hub's docs
    // when those still describe the same bundle; otherwise the copy's README stands in ({stale: true}).
    subscribeForkDocs(fork, cb, onError) {
      if (!MINE) return;
      const originBundle = fork.origin.bundle;
      let tried = false;
      return FS.onValue(FS.ref(FS.db, MINE + '/docs/' + originBundle), async (snap) => {
        const v = snap.val();
        if (v) { cb({ docs: v }); return; }
        if (tried) return; tried = true;
        try {
          const hub = HUB ? await get(HUB + '/docs/' + fork.origin.publisherUid + '/' + fork.origin.exp) : null;
          if (hub && ehDocBundle(hub) === originBundle) {
            const t = Date.now();
            await FS.update(FS.ref(FS.db, MINE + '/docs'), { [originBundle]: { ...hub, metadata: { type: 'entry', derived: true, source: HUB + '/docs/' + fork.origin.publisherUid + '/' + fork.origin.exp, createdAt: t, updatedAt: t } }, 'metadata/type': 'link', 'metadata/hidden': true }, false);
            return;
          }
          await cb({ stale: true });
        } catch (e) { onError(e); }
      }, onError);
    },
    bundleMeta: (path) => get(path + '/meta'),
    bundleChunk: async (path, i) => JSON.parse(await get(path + '/chunks/' + i) || '[]'),

    async fork(L, progress) {
      const upd = {};
      await ensureMine(upd);
      await copyBundle(L.bundle.path, MINE + '/bundles', L.bundle.id, upd, 'bundles/', progress);
      const t = Date.now();
      // Keep this version's methods with the copy, so its pages stay right after the listing moves on.
      const hubDocs = HUB ? await get(HUB + '/docs/' + L.uid + '/' + L.exp) : null;
      if (hubDocs && ehDocBundle(hubDocs) === L.bundle.id && !(await get(MINE + '/docs/' + L.bundle.id + '/metadata/type'))) {
        upd['docs/' + L.bundle.id] = { ...hubDocs, metadata: { type: 'entry', derived: true, source: HUB + '/docs/' + L.uid + '/' + L.exp, createdAt: t, updatedAt: t } };
        upd['docs/metadata/type'] = 'link'; upd['docs/metadata/hidden'] = true;
      }
      const forkId = slug(L.exp) + '-' + t.toString(36);
      const version = L.version ? 'v' + L.version : '';
      upd['forks/' + forkId] = {
        title: L.title, summary: L.summary || '', art: ehLogoKind(L), artImage: L.artImage || null, tags: L.tags || [], subjects: L.subjects || [], hardware: L.hardware || {},
        original: L.original || null, originStatus: L.status || null, methodPreview:L.methodPreview || null, methodPreviewBundle:L.bundle.id, methodDiagram:L.methodDiagram || null, methodDiagramBundle:L.bundle.id,
        parameterSets: L.parameterSets || [], alhazen: L.alhazen || '',
        origin: { publisherUid: L.uid, publisherName: (L.publisher || {}).name || '', exp: L.exp, title: L.title, bundle: L.bundle.id, version: L.version || '', commit: ((L.source || {}).commit || '').slice(0, 7) },
        head: { id: L.bundle.id, path: MINE + '/bundles/' + L.bundle.id },
        revisions: { [t]: { bundle: L.bundle.id, message: 'Forked from ' + L.title + (version ? ' ' + version : ''), by: 'user', at: t } },
        createdAt: t, updatedAt: t, metadata: { type: 'entry', createdAt: t, updatedAt: t },
      };
      await FS.update(FS.ref(FS.db, MINE), upd, false);
      return { forkId, record: upd['forks/' + forkId] };
    },

    startJob,

    async newExperiment(title, request) {
      const upd = {}; await ensureMine(upd);
      const t = Date.now(); const forkId = slug(title) + '-' + t.toString(36);
      const rec = { title, summary: request.slice(0, 280), art: 'fixation', tags: [], subjects: [], hardware: {}, origin: { scratch: true },
        head: null, createdAt: t, updatedAt: t, metadata: { type: 'entry', createdAt: t, updatedAt: t } };
      upd['forks/' + forkId] = rec;
      await FS.update(FS.ref(FS.db, MINE), upd, false);
      await startJob({ ...rec, id: forkId }, 'create', request);
      return forkId;
    },

    async publish(fork, form, progress) {
      const name = form.name;
      const head = fork.head; const bid = head.id;
      const upd = {};
      await copyBundle(head.path, HUB + '/bundles/' + userId, bid, upd, 'bundles/' + userId + '/', progress);
      const meta = await get(head.path + '/meta');
      const t = Date.now();
      const published = lastPublished.current;
      const old = (published && published[userId] && published[userId][fork.id]) || {};
      upd['published/' + userId + '/' + fork.id] = {
        id: fork.id, title: form.title, summary: form.summary, art: fork.art || 'fixation', artImage: fork.artImage || null, featured: false, category: form.category, collection: 'own', original: fork.original || null,
        tags: [form.category, ...((fork.tags || []).filter((x) => x !== form.category))], subjects: form.subjects, hardware: form.hardware,
        platforms: ['linux', 'darwin', 'win32'], license: form.license || 'All rights reserved', publisher: { uid: userId, name },
        source: { kind: 'fork', from: fork.origin || null }, version: form.version, alhazen: fork.alhazen || '', parameterSets: fork.parameterSets || [],
        bundle: { id: bid, path: HUB + '/bundles/' + userId + '/' + bid, fileCount: meta.fileCount, bytes: meta.bytes, excluded: JSON.parse(meta.excluded || '[]').length },
        publishedAt: old.publishedAt || t, updatedAt: t, versions: { ...(old.versions || {}), [bid]: { version: form.version, at: t } },
        metadata: { type: 'entry', createdAt: (old.metadata || {}).createdAt || t, updatedAt: t },
      };
      await FS.update(FS.ref(FS.db, HUB), upd, false);
      await FS.update(FS.ref(FS.db, MINE + '/forks/' + fork.id), { published: { at: t, version: form.version, bundle: bid, title: form.title }, updatedAt: t });
    },

    async unpublish(fork) {
      await FS.remove(FS.ref(FS.db, HUB + '/published/' + userId + '/' + fork.id));
      await FS.update(FS.ref(FS.db, MINE + '/forks/' + fork.id), { published: null, updatedAt: Date.now() });
    },

    // Remove a copy from the library in one atomic update of the viewer's own workspace: the copy
    // (revisions and job records live inside it), its revision files no other copy names, and the
    // docs snapshot of its origin version when no other copy uses it (removalPlan). Read fresh, so
    // a copy made in another tab a moment ago keeps what it names. Published listings keep their
    // own files under hub/bundles; nothing on a machine is touched.
    async removeFork(fork) {
      const forks = (await get(MINE + '/forks')) || {};
      if (!forks[fork.id]) return;
      const all = Object.entries(forks).filter(([k, v]) => k !== 'metadata' && v && typeof v === 'object').map(([id, v]) => ({ ...v, id }));
      const plan = removalPlan({ ...forks[fork.id], id: fork.id }, all);
      const upd = { ['forks/' + fork.id]: null };
      for (const b of plan.bundles) upd['bundles/' + b] = null;
      if (plan.docsKey) upd['docs/' + plan.docsKey] = null;
      await FS.update(FS.ref(FS.db, MINE), upd);
    },

    // [2026-10-15] Switch chats through the shell's own route event (same fix as Agent Hub). Pushing ?chatId=
    // plus popstate no longer works: the shell rewrites chatId back to the chat that is already open.
    showChat(chatId) {
      if (!chatId) return;
      try {
        if (window.__manaNativeRouteReady) {
          window.dispatchEvent(new CustomEvent('notification-navigate', { detail: { chatId } }));
          return;
        }
        const u = new URL(window.location.href); u.searchParams.set('chatId', chatId);
        window.history.pushState({}, '', u.toString()); window.dispatchEvent(new PopStateEvent('popstate'));
        if (typeof openChat === 'function') openChat();
      } catch (e) {}
    },

    // [2026-10-16] My machines links to the Mana Terminal app for pairing.
    openTerminalApp() {
      window.history.pushState({}, '', `/home/${userId}/Apps/mana-terminal`);
      window.dispatchEvent(new PopStateEvent('popstate'));
    },
  };
  return <PlatformApp host={host} />;
}
