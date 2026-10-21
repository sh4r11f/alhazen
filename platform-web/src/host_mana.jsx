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
// The rig config editor's YAML reader, loaded the first time an editor opens (never at app start).
const MANA_YAML_MODULE = 'https://esm.sh/yaml@2.9.1';
let manaYamlLoading = null;
function loadManaYaml() {
  if (!manaYamlLoading) manaYamlLoading = import(MANA_YAML_MODULE).catch((e) => { manaYamlLoading = null; throw e; });
  return manaYamlLoading;
}

// [2026-10-21] Who can see a listing (design.md section 5a). Public listings live in the hub
// (published/<pub>/<exp>, docs/<pub>/<exp>, bundles/<pub>/<id>). The hub is readable by everyone it
// is shared with, and Mana grants inherit downward, so a non-public listing lives in its publisher's
// own workspace instead: MANA_MINE/listings/<exp>/{listing, docs, bundles/<id>} with a read grant per
// person on that node. MANA_MINE/listed/<exp> indexes the viewer's own non-public listings (derived,
// so the app never downloads bundles to find them); MANA_MINE/shared/<pub>~<exp> are pointers saved
// from share links; the hub owner's share index tells each person what the owner shared with them.
const MANA_MINE_NAME = 'alhazen-experiment-platform-mine';
const MANA_SHARES_NAME = 'alhazen-experiment-platform-shares';
const MANA_WRITE_LIMIT = 7000000; // characters per Firebase write, under the ~8 MB ceiling
const manaMineOf = (uid) => uid + '/file-system/' + MANA_MINE_NAME;
const manaShareKey = (pub, exp) => pub + '~' + exp;
function manaParseShareKey(key) {
  const i = String(key || '').indexOf('~');
  if (i <= 0 || i === key.length - 1) return null;
  const pub = key.slice(0, i), exp = key.slice(i + 1);
  // Firebase keys cannot hold . $ # [ ] /; a key that does is not one of ours.
  return /[.$#\[\]\/]/.test(pub + exp) ? null : { pub, exp };
}
// Where each part of a listing is stored, for both kinds of location.
function manaListingPlace(kind, hub, pub, exp) {
  if (kind === 'hub') return { kind, listing: hub + '/published/' + pub + '/' + exp, docs: hub + '/docs/' + pub + '/' + exp, bundle: (id) => hub + '/bundles/' + pub + '/' + id, base: null };
  const base = manaMineOf(pub) + '/listings/' + exp;
  return { kind, listing: base + '/listing', docs: base + '/docs', bundle: (id) => base + '/bundles/' + id, base };
}
const manaPlaceKind = (access) => (accessOf({ access }).visibility === 'public' ? 'hub' : 'mine');
// The Firebase read grants that mirror access.people (null: nobody besides the publisher).
function manaReadGrants(access) {
  const a = accessOf({ access });
  if (a.visibility !== 'people') return null;
  const read = {}; for (const u of a.people) read[u] = true;
  return { read };
}
// Bundle ids a listing names: its current bundle and every recorded version.
const manaListingBundles = (rec) => [...new Set([rec && rec.bundle && rec.bundle.id, ...Object.keys((rec && rec.versions) || {})].filter(Boolean))];
// What moving a listing between the hub and the publisher's workspace copies and deletes. `others`
// are the publisher's other hub listings: a hub bundle one of them names is not deleted.
function manaMovePlan({ hub, pub, exp, rec, from, to, others }) {
  const src = manaListingPlace(from, hub, pub, exp), dst = manaListingPlace(to, hub, pub, exp);
  const bundles = manaListingBundles(rec);
  const keep = new Set();
  if (from === 'hub') for (const [k, o] of Object.entries(others || {})) if (k !== exp && k !== 'metadata' && o && typeof o === 'object') for (const b of manaListingBundles(o)) keep.add(b);
  const remap = (p) => (typeof p === 'string' && bundles.some((b) => p === src.bundle(b)) ? dst.bundle(bundles.find((b) => p === src.bundle(b))) : p);
  const versions = {};
  for (const [k, v] of Object.entries((rec && rec.versions) || {})) versions[k] = v && typeof v === 'object' && v.path ? { ...v, path: remap(v.path) } : v;
  const listing = { ...rec, bundle: { ...(rec.bundle || {}), path: dst.bundle(rec.bundle.id) }, ...(rec.versions ? { versions } : {}) };
  const deletes = from === 'hub'
    ? [src.listing, src.docs, ...bundles.filter((b) => !keep.has(b)).map(src.bundle)]
    : [src.base];
  return { src, dst, bundles, listing, deletes };
}
// The hub owner's share index patch (relative to the index node) for one listing whose access
// changed from `before` to `after`: an entry per person who can now see it, removed for the rest.
function manaShareIndexPatch(pub, exp, before, after, at) {
  const was = new Set(accessOf({ access: before }).people), now = new Set(accessOf({ access: after }).people);
  const key = manaShareKey(pub, exp), patch = {};
  for (const u of now) if (!was.has(u)) patch[u + '/' + key] = { publisher: pub, exp, at };
  for (const u of was) if (!now.has(u)) patch[u + '/' + key] = null;
  return patch;
}
// Hub listings plus the non-public listings the viewer can read (keyed pub~exp, each carrying its
// own `_base`), as one {pub: {exp: listing}} map. During a move both copies exist for a moment;
// the newer one wins.
function manaMergeListings(hubPublished, extra) {
  const out = {};
  for (const [pub, byExp] of Object.entries(hubPublished || {})) {
    if (pub === 'metadata' || !byExp || typeof byExp !== 'object') continue;
    out[pub] = { ...byExp };
  }
  for (const [key, L] of Object.entries(extra || {})) {
    const k = manaParseShareKey(key); if (!k || !L) continue;
    const have = out[k.pub] && out[k.pub][k.exp];
    if (!have || (L.updatedAt || 0) >= (have.updatedAt || 0)) out[k.pub] = { ...(out[k.pub] || {}), [k.exp]: L };
  }
  return out;
}

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

function runPrompt({ fork, forkId, jobId, kitPath, minePath, machine, rigConfig }) {
  return [
    '[Alhazen Experiment Platform · run on my machine]',
    'Experiment: ' + fork.title + ' (fork id ' + forkId + '), my copy at ' + minePath + '/forks/' + forkId + '.',
    'Goal: copy this revision to my machine, build its Python environment there from the lock file, and start the alhazen dashboard so I can run sessions.',
    '',
    '1. Load the kit in Python:\n```python\n' + kitLoader(kitPath) + '\n```',
    '2. Mana Terminal must be installed in this Mana (`/workspace/mana_terminal.py`). If it is not, call `hubkit.set_job("' + forkId + '", "' + jobId + '", {"status": "failed", "summary": "Mana Terminal is not installed. Install it from the marketplace, pair your machine, then run again."})` and stop.',
    '3. `from mana_terminal import terms` (add /workspace to sys.path). If no window is open, set the job to status "waiting" with summary "Open a terminal on your machine and type mana, then press Run again." and stop.',
    '4. `res = hubkit.run_on_machine("' + forkId + '", machine=' + (machine ? JSON.stringify(machine) : 'None') + ', psychopy=True, log=print, job_id="' + jobId + '"' + (rigConfig ? ', rig_config=' + JSON.stringify(rigConfig) : '') + ')`.' + (rigConfig ? ' rig_config is the rig config I chose (one of that machine\'s configs, by name): the dashboard opens with it chosen on its Run page. If the kit says the experiment\'s alhazen is too old to open a config by name, say so in the summary; the dashboard then opens with the machine\'s default.' : '') + ' The job id lets the dashboard report back to this job when it closes. Each step that changes my machine is approved by me in the terminal; a refusal (TermDenied) is final: set status "stopped" and do not retry. The kit checks the disk first: if it raises hubkit.CopyHasData, nothing was changed; set the job to status "waiting" with its message as the summary, ask me whether to update the files (the saved sessions stay) or run the folder as it is, and call run_on_machine again with replace=True or keep=True accordingly.',
    '5. If a step fails, read the error and the dashboard log, fix what can be fixed on the machine through the terminal (I approve each change), and retry that step. Do not edit the experiment\'s source here.',
    '6. Finish with `hubkit.set_job("' + forkId + '", "' + jobId + '", {"status": "done", "summary": "<one plain sentence>", "url": res["url"], "dir": res["dir"], "machine": <window label>})`, or status "failed" with what went wrong. Mention in the summary when res["disk"]["action"] is "unchanged" (already there), when res["library"] is not ok, and when res["dashboard"]["older"] is true (the dashboard already open runs an older alhazen than this experiment; its sessions still use the experiment\'s own).',
    'Nothing here writes to GitHub or to the shared hub.',
  ].join('\n');
}

function UserDiv() {
  const HUB = (relPathAddress('published') || '').replace(/\/published$/, '') || null;
  const KIT = relPathAddress('kit', 'hubkit', 'code');
  const MINE = userId ? manaMineOf(userId) : null;
  // The hub owner's share index (section 5a); only the owner writes it, each person reads their own entries.
  const SHARES = ownerId ? ownerId + '/file-system/' + MANA_SHARES_NAME : null;
  const lastListings = React.useRef({});
  const art = useStorageUrl('art');
  const lastPublished = React.useRef(null);
  const viewer = userId ? { uid: userId, name: (FS.auth.currentUser && (FS.auth.currentUser.displayName || (FS.auth.currentUser.email || '').split('@')[0])) || 'Unknown' } : null;

  async function ensureMine(upd, extra) {
    const meta = await get(MINE + '/metadata/type');
    const t = Date.now();
    const folder = { type: 'link', hidden: true, createdAt: t, updatedAt: t };
    if (!meta) {
      Object.assign(upd, { 'metadata': { type: 'link', hidden: true, displayName: 'Alhazen Experiment Platform: my experiments', createdAt: t, updatedAt: t },
        'forks/metadata': folder, 'bundles/metadata': folder });
    }
    // Folders added in 3.7 (listings, listed, shared) are typed the first time something goes in them.
    for (const name of extra || []) if (!meta || !(await get(MINE + '/' + name + '/metadata/type'))) upd[name + '/metadata'] = folder;
  }

  // Where a listing the app shows is stored now: the publisher's workspace when it carries _base, else the hub.
  const placeOf = (L) => manaListingPlace(L && L._base ? 'mine' : 'hub', HUB, L.uid, L.exp);
  const findListing = (pub, exp) => { const byExp = lastListings.current[pub]; const L = byExp && byExp[exp]; return L ? { ...L, uid: pub, exp } : null; };
  async function postShare(route, body) {
    const token = await FS.auth.currentUser.getIdToken();
    return fetch(API_BASE_URL + '/share/' + route, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token }, body: JSON.stringify(body) });
  }
  async function describePeople(uids) {
    const ids = [...new Set((uids || []).filter(Boolean))];
    if (!ids.length) return {};
    const res = await postShare('lookup-users', { user_ids: ids });
    if (!res.ok) throw new Error('Could not look up those accounts (' + res.status + ').');
    const out = {};
    for (const u of (await res.json()).users || []) out[u.uid] = u.email || u.displayName || u.uid;
    return out;
  }
  // A share link opens the listing in this app: /home/<hub owner>/<app>?ehv=exp&ehid=<pub>~<exp>.
  function shareLink(L) {
    const app = HUB ? HUB.slice(HUB.indexOf('/file-system/') + '/file-system/'.length).replace(/\/[^/]+$/, '') : '';
    return window.location.origin + '/home/' + ownerId + '/' + app.split('/').map(encodeURIComponent).join('/') + '?ehv=exp&ehid=' + encodeURIComponent(manaShareKey(L.uid, L.exp));
  }
  async function notifyPeople(L, uids) {
    if (!uids.length) return { sent: 0, failed: [] };
    const emails = await describePeople(uids);
    const failed = [];
    let sent = 0;
    for (const u of uids) {
      const email = emails[u];
      if (!email || !email.includes('@')) { failed.push(u); continue; }
      try {
        const res = await postShare('notify', { email, appName: L.title, role: 'viewer', shareUrl: shareLink(L), sharerName: (viewer && viewer.name) || '' });
        if (res.ok) sent++; else failed.push(email);
      } catch (e) { failed.push(email); }
    }
    return { sent, failed };
  }
  // Keep the hub owner's share index in step with one listing's people (only the owner can write it).
  async function updateShareIndex(pub, exp, before, after) {
    if (!SHARES || userId !== ownerId || pub !== userId) return;
    const patch = manaShareIndexPatch(pub, exp, before, after, Date.now());
    if (!Object.keys(patch).length) return;
    if (!(await get(SHARES + '/metadata/type'))) {
      const t = Date.now();
      await FS.update(FS.ref(FS.db, SHARES), { metadata: { type: 'link', hidden: true, displayName: 'Alhazen Experiment Platform: shared listings', permissions: { per_user_read: true }, createdAt: t, updatedAt: t } }, false);
    }
    await FS.update(FS.ref(FS.db, SHARES), patch, false);
  }
  // Copy one bundle chunk by chunk (one write each), meta last; skipped when the target is complete.
  async function moveBundle(srcPath, dstPath, required) {
    if (await get(dstPath + '/meta/complete')) return;
    const meta = await get(srcPath + '/meta');
    if (!meta) { if (required) throw new Error('This listing has no source files.'); return; }
    for (let i = 0; i < (meta.chunkCount || 0); i++) {
      await FS.update(FS.ref(FS.db, dstPath), { ['chunks/' + i]: await get(srcPath + '/chunks/' + i) }, false);
    }
    await FS.update(FS.ref(FS.db, dstPath), { meta: { ...meta, complete: true, copiedFrom: srcPath } }, false);
  }
  async function writeDocs(path, docs) {
    const at = path.lastIndexOf('/'), parent = path.slice(0, at), key = path.slice(at + 1);
    if (JSON.stringify(docs).length < MANA_WRITE_LIMIT) { await FS.update(FS.ref(FS.db, parent), { [key]: docs }, false); return; }
    for (const [k, v] of Object.entries(docs)) await FS.update(FS.ref(FS.db, path), { [k]: v }, false);
  }
  // Move a listing between the hub and the publisher's workspace (section 5a): bundles chunk by
  // chunk, then docs, then the listing last; read back; only then delete the old location.
  async function moveListing(L, to, access, t) {
    const from = L._base ? 'mine' : 'hub';
    const srcPlace = manaListingPlace(from, HUB, L.uid, L.exp);
    const rec = await get(srcPlace.listing);
    if (!rec) throw new Error('This experiment is not in the catalogue');
    const others = from === 'hub' ? (await get(HUB + '/published/' + L.uid)) || {} : {};
    const plan = manaMovePlan({ hub: HUB, pub: L.uid, exp: L.exp, rec, from, to, others });
    if (to === 'mine') {
      const upd = {}; await ensureMine(upd, ['listings', 'listed']);
      upd['listings/' + L.exp + '/metadata'] = { type: 'link', hidden: true, displayName: rec.title || L.exp, createdAt: t, updatedAt: t, ...(manaReadGrants(access) ? { permissions: manaReadGrants(access) } : {}) };
      await FS.update(FS.ref(FS.db, MINE), upd, false);
    }
    for (const b of plan.bundles) await moveBundle(plan.src.bundle(b), plan.dst.bundle(b), b === rec.bundle.id);
    const docs = await get(plan.src.docs);
    if (docs) await writeDocs(plan.dst.docs, docs);
    const listing = { ...plan.listing, access, updatedAt: t };
    if (to === 'mine') await FS.update(FS.ref(FS.db, plan.dst.base), { listing, 'metadata/permissions': manaReadGrants(access), 'metadata/updatedAt': t }, false);
    else await FS.update(FS.ref(FS.db, HUB + '/published/' + L.uid), { [L.exp]: listing }, false);
    // Read back before anything is deleted.
    const back = await get(plan.dst.listing);
    if (!back || !back.bundle || back.bundle.id !== rec.bundle.id || accessOf(back).visibility !== access.visibility) throw new Error('The moved listing did not read back as written; the old one was kept.');
    for (const b of plan.bundles) {
      const m = await get(plan.dst.bundle(b) + '/meta');
      if (!m) continue; // a version whose files were never stored
      if (!m.complete || (m.chunkCount && (await get(plan.dst.bundle(b) + '/chunks/' + (m.chunkCount - 1))) == null)) throw new Error('The moved files did not read back complete; the old listing was kept.');
    }
    if (docs && !(await get(plan.dst.docs))) throw new Error('The moved methods did not read back; the old listing was kept.');
    if (from === 'hub') await FS.update(FS.ref(FS.db, HUB), Object.fromEntries(plan.deletes.map((p) => [p.slice(HUB.length + 1), null])), false);
    else await FS.update(FS.ref(FS.db, MINE), { ['listings/' + L.exp]: null, ['listed/' + L.exp]: null }, false);
    if (to === 'mine') await FS.update(FS.ref(FS.db, MINE), { ['listed/' + L.exp]: { visibility: access.visibility, at: t } }, false);
    return listing;
  }
  async function setAccess(L, { visibility, people, notify }) {
    if (!userId || L.uid !== userId) throw new Error('Only the publisher can change who sees this');
    const next = normalizeAccess({ visibility, people }, userId);
    const before = accessOf(L);
    const t = Date.now();
    const to = manaPlaceKind(next);
    if ((L._base ? 'mine' : 'hub') !== to) await moveListing(L, to, next, t);
    else if (to === 'mine') {
      await FS.update(FS.ref(FS.db, L._base), { 'listing/access': next, 'listing/updatedAt': t, 'metadata/permissions': manaReadGrants(next), 'metadata/updatedAt': t }, false);
      await FS.update(FS.ref(FS.db, MINE), { ['listed/' + L.exp]: { visibility: next.visibility, at: t } }, false);
    } else await FS.update(FS.ref(FS.db, HUB + '/published/' + L.uid + '/' + L.exp), { access: next, updatedAt: t }, false);
    await updateShareIndex(L.uid, L.exp, before, next);
    const added = next.people.filter((u) => !before.people.includes(u));
    const notified = notify && added.length ? await notifyPeople(L, added) : { sent: 0, failed: [] };
    return { access: next, notified };
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

  async function startJob(fork, kind, request, machine, options) {
    const forkId = fork.id;
    const rigConfig = (options && options.rigConfig) || '';
    const chatId = await mana.mintChatId();
    const jobId = String(Date.now());
    const text = kind === 'run'
      ? runPrompt({ fork, forkId, jobId, kitPath: KIT, minePath: MINE, machine, rigConfig })
      : changePrompt({ fork, forkId, jobId, request, kitPath: KIT, minePath: MINE, fromScratch: kind === 'create' });
    await FS.update(FS.ref(FS.db, MINE + '/forks/' + forkId + '/jobs/' + jobId), { kind, request: request || '', chatId, status: 'running', startedAt: Date.now(), updatedAt: Date.now(), machine: machine || '', ...(rigConfig ? { rigConfig } : {}) });
    await mana.ask(chatId, text, undefined, undefined, undefined, undefined, true);
    return jobId;
  }

  // One owner change to a machine record: read it fresh, apply the rule, write only what changed.
  async function changeRig(mid, rule, cid) {
    const path = MINE + '/machines/' + rigCheckMachineId(mid);
    const before = rigFromFirebase(await get(path), mid);
    const now = Date.now();
    const after = rule(before, now);
    const patch = rigFirebasePatch(before || rigNormalize(null, mid), after, now);
    if (Object.keys(patch).length) await FS.update(FS.ref(FS.db, path), patch, false);
    return { record: after, config: cid ? after.configs[cid] || null : null };
  }

  const host = {
    kind: 'mana',
    key: HUB + '|' + MINE,
    viewer,
    capabilities: { fork: true, publish: true, changeWithMana: true, runOnMachines: true, newWithMana: true,
      uploadRevision: false, downloadRevision: false, machines: true, signIn: false, themeToggle: false, rigConfigs: !!userId,
      shareWithPeople: !!userId, notifyPeople: !!userId },
    art,
    fontStylesheet: MANA_FONT_STYLESHEET,
    extraCss: '',
    links: { terminalListing: TERMINAL_LISTING, terminalApp: `/home/${userId}/Apps/mana-terminal` },
    useTheme,
    copyText: (text) => copyText(text),

    // Hub listings, merged with the non-public listings this viewer can read: their own (MINE/listed),
    // the hub owner's share index entries for them, and the pointers they saved from share links. Each
    // of those listings is subscribed on its own; one that cannot be read (no longer shared) is dropped.
    subscribeListings(cb) {
      if (!HUB) return;
      let hub = null, hubLoaded = false, closed = false;
      const extra = {}, subs = new Map(), sources = { own: [], index: [], pointers: [] };
      const emit = () => {
        if (!hubLoaded || closed) return;
        const merged = manaMergeListings(hub, extra);
        lastListings.current = merged; lastPublished.current = hub;
        cb(merged);
      };
      const sync = () => {
        const want = new Set([...sources.own, ...sources.index, ...sources.pointers].filter((k) => manaParseShareKey(k)));
        for (const [k, off] of subs) if (!want.has(k)) { off(); subs.delete(k); delete extra[k]; }
        for (const k of want) {
          if (subs.has(k)) continue;
          const { pub, exp } = manaParseShareKey(k);
          const base = manaListingPlace('mine', HUB, pub, exp).base;
          subs.set(k, FS.onValue(FS.ref(FS.db, base + '/listing'), (snap) => {
            const v = snap.val();
            if (v && v.title && accessOf(v).visibility !== 'public') extra[k] = { ...v, _base: base }; else delete extra[k];
            emit();
          }, () => { delete extra[k]; subs.delete(k); emit(); }));
        }
        emit();
      };
      const keysOf = (v) => Object.keys(v || {}).filter((k) => k !== 'metadata');
      const offs = [FS.onValue(FS.ref(FS.db, HUB + '/published'), (snap) => { hub = snap.val(); hubLoaded = true; emit(); })];
      if (MINE) {
        offs.push(FS.onValue(FS.ref(FS.db, MINE + '/listed'), (snap) => { sources.own = keysOf(snap.val()).map((exp) => manaShareKey(userId, exp)); sync(); }, () => { sources.own = []; sync(); }));
        offs.push(FS.onValue(FS.ref(FS.db, MINE + '/shared'), (snap) => { sources.pointers = keysOf(snap.val()); sync(); }, () => { sources.pointers = []; sync(); }));
      }
      // The index exists only once the owner has shared something; before that the read is refused.
      if (SHARES && userId) offs.push(FS.onValue(FS.ref(FS.db, SHARES + '/' + userId), (snap) => { sources.index = keysOf(snap.val()); sync(); }, () => { sources.index = []; sync(); }));
      return () => { closed = true; for (const off of offs) if (off) off(); for (const off of subs.values()) off(); subs.clear(); };
    },
    // A share link to a listing this viewer has not seen yet: read it where non-public listings live,
    // and if it can be read, keep a pointer so it stays in their lists. False when it cannot be read.
    async resolveListing(pub, exp) {
      if (!MINE || !manaParseShareKey(manaShareKey(pub, exp))) return false;
      let rec = null;
      try { rec = await get(manaListingPlace('mine', HUB, pub, exp).listing); } catch (e) { return false; }
      if (!rec || !rec.title) return false;
      if (pub !== userId) {
        const upd = {}; await ensureMine(upd, ['shared']);
        upd['shared/' + manaShareKey(pub, exp)] = { publisher: pub, exp, at: Date.now() };
        await FS.update(FS.ref(FS.db, MINE), upd, false);
      }
      return true;
    },
    setAccess,
    async findPeople(query) {
      const email = String(query || '').trim().toLowerCase();
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) throw new Error('Enter their email address.');
      const res = await postShare('lookup-user', { email });
      if (res.status === 404) return [];
      if (!res.ok) throw new Error('Could not look up that address (' + res.status + ').');
      const { uid } = await res.json();
      return uid ? [{ uid, label: email }] : [];
    },
    describePeople,
    shareLink,
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
      return FS.onValue(FS.ref(FS.db, placeOf(listing).docs), (s) => cb(s.val()), onError);
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
    // [2026-10-21] Machine rig configs (machine-rig-configs.md §4): each machine's record at
    // MINE/machines/<mid> (configs under rigs/<cid>), written by the viewer's alhazen-library service
    // when a dashboard syncs, and changed here with the same rules (rigsync.js). Only what a change
    // touches is written (rigFirebasePatch), outside the undo journal like the service's writes.
    subscribeRigMachines(cb, onError) {
      if (!MINE) return;
      return FS.onValue(FS.ref(FS.db, MINE + '/machines'), (s) => {
        const all = s.val() || {};
        const machines = [], problems = [];
        for (const [mid, node] of Object.entries(all)) {
          if (mid === 'metadata' || !node || typeof node !== 'object') continue;
          // A record under a key that is not a machine id is listed as a problem, not skipped silently.
          try { machines.push(rigSummary(rigFromFirebase(node, mid))); }
          catch (e) { if (!(e instanceof RigSyncError)) throw e; problems.push(mid + ': ' + e.message); }
        }
        cb({ machines, problems });
      }, onError);
    },
    subscribeRigMachine(mid, cb, onError) {
      if (!MINE) return;
      return FS.onValue(FS.ref(FS.db, MINE + '/machines/' + rigCheckMachineId(mid)), (s) => cb(rigFromFirebase(s.val(), mid)), onError);
    },
    async listRigMachines() {
      const all = (await get(MINE + '/machines')) || {};
      return Object.entries(all).filter(([mid, n]) => mid !== 'metadata' && n && typeof n === 'object').map(([mid, n]) => rigSummary(rigFromFirebase(n, mid)));
    },
    getRigMachine: async (mid) => rigFromFirebase(await get(MINE + '/machines/' + rigCheckMachineId(mid)), mid),
    putRigConfig: (mid, cid, { name, text, base, basedOn }) => changeRig(mid, (rec, now) => rigOwnerPut(rec, mid, cid, { name, text, base, basedOn, now }), cid),
    deleteRigConfig: (mid, cid, base) => changeRig(mid, (rec, now) => rigOwnerDelete(rec, mid, cid, { base, now })),
    setRigDefault: (mid, cid, base) => changeRig(mid, (rec, now) => rigOwnerDefault(rec, mid, cid, { base, now })),
    resolveRigConfig: (mid, cid, keep) => changeRig(mid, (rec, now) => rigOwnerResolve(rec, mid, cid, { keep, now }), cid),
    async forgetRigMachine(mid) { await FS.remove(FS.ref(FS.db, MINE + '/machines/' + rigCheckMachineId(mid)), false); },
    loadYaml: loadManaYaml,

    bundleMeta: (path) => get(path + '/meta'),
    bundleChunk: async (path, i) => JSON.parse(await get(path + '/chunks/' + i) || '[]'),

    async fork(L, progress) {
      const upd = {};
      await ensureMine(upd);
      await copyBundle(L.bundle.path, MINE + '/bundles', L.bundle.id, upd, 'bundles/', progress);
      const t = Date.now();
      // Keep this version's methods with the copy, so its pages stay right after the listing moves on.
      const docsPath = HUB ? placeOf(L).docs : null;
      const hubDocs = docsPath ? await get(docsPath) : null;
      if (hubDocs && ehDocBundle(hubDocs) === L.bundle.id && !(await get(MINE + '/docs/' + L.bundle.id + '/metadata/type'))) {
        upd['docs/' + L.bundle.id] = { ...hubDocs, metadata: { type: 'entry', derived: true, source: docsPath, createdAt: t, updatedAt: t } };
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

    // Publish (or update) the listing of a copy where its access says: the hub for everyone, the
    // viewer's own workspace for only them or specific people. An existing listing whose access
    // changes is moved first, so its earlier versions go with it.
    async publish(fork, form, progress) {
      const name = form.name;
      const head = fork.head; const bid = head.id;
      let old = findListing(userId, fork.id);
      const before = old ? accessOf(old) : { visibility: 'public', people: [] };
      const access = normalizeAccess(form.access || before, userId);
      const kind = manaPlaceKind(access);
      const t0 = Date.now();
      if (old && (old._base ? 'mine' : 'hub') !== kind) {
        progress('Moving the listing');
        await moveListing(old, kind, access, t0);
        old = { ...(await get(manaListingPlace(kind, HUB, userId, fork.id).listing)), _base: kind === 'mine' ? manaListingPlace('mine', HUB, userId, fork.id).base : null };
      }
      const place = manaListingPlace(kind, HUB, userId, fork.id);
      const upd = {};
      const root = kind === 'hub' ? HUB : MINE;
      const rel = (path) => path.slice(root.length + 1);
      if (kind === 'mine') {
        await ensureMine(upd, ['listings', 'listed']);
      }
      const bundleDir = place.bundle(bid).slice(0, -(bid.length + 1));
      await copyBundle(head.path, bundleDir, bid, upd, rel(bundleDir) + '/', progress);
      const meta = await get(head.path + '/meta');
      const t = Date.now();
      const prev = old || {};
      const listing = {
        id: fork.id, title: form.title, summary: form.summary, art: fork.art || 'fixation', artImage: fork.artImage || null, featured: false, category: form.category, collection: 'own', original: fork.original || null,
        tags: [form.category, ...((fork.tags || []).filter((x) => x !== form.category))], subjects: form.subjects, hardware: form.hardware,
        platforms: ['linux', 'darwin', 'win32'], license: form.license || 'All rights reserved', publisher: { uid: userId, name },
        source: { kind: 'fork', from: fork.origin || null }, version: form.version, alhazen: fork.alhazen || '', parameterSets: fork.parameterSets || [],
        bundle: { id: bid, path: place.bundle(bid), fileCount: meta.fileCount, bytes: meta.bytes, excluded: JSON.parse(meta.excluded || '[]').length },
        access, publishedAt: prev.publishedAt || t, updatedAt: t, versions: { ...(prev.versions || {}), [bid]: { version: form.version, at: t } },
        metadata: { type: 'entry', createdAt: (prev.metadata || {}).createdAt || t, updatedAt: t },
      };
      upd[rel(place.listing)] = listing;
      if (kind === 'mine') {
        // The listing and its read grants in one update (section 5a); the node's metadata is
        // written whole when it is new, else only its grants change.
        if (!(await get(place.base + '/metadata/type'))) upd[rel(place.base) + '/metadata'] = { type: 'link', hidden: true, displayName: form.title, createdAt: t, updatedAt: t, ...(manaReadGrants(access) ? { permissions: manaReadGrants(access) } : {}) };
        else upd[rel(place.base) + '/metadata/permissions'] = manaReadGrants(access);
        upd['listed/' + fork.id] = { visibility: access.visibility, at: t };
      }
      await FS.update(FS.ref(FS.db, root), upd, false);
      await FS.update(FS.ref(FS.db, MINE + '/forks/' + fork.id), { published: { at: t, version: form.version, bundle: bid, title: form.title, visibility: access.visibility }, updatedAt: t });
      await updateShareIndex(userId, fork.id, before, access);
      const added = access.people.filter((u) => !before.people.includes(u));
      return { access, notified: form.notify && added.length ? await notifyPeople({ uid: userId, exp: fork.id, title: form.title }, added) : { sent: 0, failed: [] } };
    },

    async unpublish(fork) {
      const L = findListing(userId, fork.id);
      if (L && L._base) {
        await FS.update(FS.ref(FS.db, MINE), { ['listings/' + fork.id]: null, ['listed/' + fork.id]: null }, false);
        await updateShareIndex(userId, fork.id, accessOf(L), { visibility: 'private' });
      } else await FS.remove(FS.ref(FS.db, HUB + '/published/' + userId + '/' + fork.id));
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
