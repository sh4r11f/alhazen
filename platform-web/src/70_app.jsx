// The Alhazen Experiment Platform web app. It reaches data, identity and actions only through
// `host` (design.md section 7): the Mana adapter (host_mana.jsx) or the standalone HTTP adapter
// (host_http.jsx). Capabilities decide which controls exist; text never promises what a host lacks.
function PlatformApp({ host }) {
  const { theme, toggleTheme } = host.useTheme();
  const T = theme === 'dark' ? DARK : LIGHT;
  const vars = {}; for (const [k, v] of Object.entries(T)) vars['--' + k] = v;
  const caps = host.capabilities || {};
  const viewerUid = host.viewer ? host.viewer.uid : null;
  const art = host.art;

  const [route, setRoute] = React.useState(readRoute);
  const [published, setPublished] = React.useState(null);
  const [assets, setAssets] = React.useState({});
  const [forks, setForks] = React.useState(null);
  const [termData, setTermData] = React.useState(undefined);
  const [dialog, setDialog] = React.useState(null);
  const [flash, setFlash] = React.useState(null);
  const [busy, setBusy] = React.useState('');
  const [bf, setBf] = React.useState({ q: '', cat: '', hw: [], who: [], sort: 'newest', coll: '', vis: '' });
  const [docs, setDocs] = React.useState({});
  const [metas, setMetas] = React.useState({});
  const [fileView, setFileView] = React.useState(null);
  const [draft, setDraft] = React.useState('');
  const [upload, setUpload] = React.useState({ file: null, message: '' });
  // Machine tokens (standalone hosts): {machines, events} from /api/machines, and the token just
  // made (shown once, never stored).
  const [machineTokens, setMachineTokens] = React.useState(undefined);
  const [madeToken, setMadeToken] = React.useState(null);
  const [machineName, setMachineName] = React.useState('');
  const [revoking, setRevoking] = React.useState('');
  // Who can see a listing (design.md section 5a): labels of the people a publisher shared with, and
  // share links being resolved (listings the host could not list yet: 'pending' | 'found' | 'missing').
  const [peopleNames, setPeopleNames] = React.useState({});
  const [resolved, setResolved] = React.useState({});

  React.useEffect(() => { const on = () => setRoute(readRoute()); window.addEventListener('popstate', on); return () => window.removeEventListener('popstate', on); }, []);
  React.useEffect(() => host.subscribeListings((v) => setPublished(v || {})), [host.key]);
  React.useEffect(() => host.subscribeAssets((v) => setAssets(v || {})), [host.key]);
  React.useEffect(() => host.subscribeForks((v) => setForks(v || {}), () => setForks({})), [host.key]);
  React.useEffect(() => { if (!caps.machines) return; return host.subscribeMachines((v) => setTermData(v), () => setTermData(null)); }, []);
  React.useEffect(() => { if (!caps.machineTokens || !host.subscribeMachineTokens) return; return host.subscribeMachineTokens((v) => setMachineTokens(v || { machines: [], events: [] }), () => setMachineTokens(null)); }, [host.key]);

  const go = (r, replace) => { const next = { v: r.v || 'home', id: r.id || '', tab: r.tab || '', task:r.task || '', set:r.set || '' }; writeRoute(next, replace); setRoute(next); };

  const listings = React.useMemo(() => {
    const out = [];
    for (const [uid, byExp] of Object.entries(published || {})) {
      if (!byExp || typeof byExp !== 'object' || uid === 'metadata') continue;
      for (const [exp, L] of Object.entries(byExp)) if (L && L.title && exp !== 'metadata') out.push({ ...L, key: uid + '~' + exp, uid, exp });
    }
    return out.sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
  }, [published]);
  const forkList = React.useMemo(() => Object.entries(forks || {}).filter(([k, v]) => k !== 'metadata' && v && v.title).map(([id, f]) => ({ ...f, id })).sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0)), [forks]);
  const forkedFrom = React.useMemo(() => { const m = {}; for (const f of forkList) if (f.origin) m[f.origin.publisherUid + '~' + f.origin.exp] = f.id; return m; }, [forkList]);

  const machines = React.useMemo(() => {
    if (!termData) return { installed: termData !== null && termData !== undefined, devices: [], sessions: [] };
    const devices = Object.entries(termData.devices || {}).map(([id, d]) => ({ id, ...d }));
    const sessions = Object.entries(termData.sessions || {}).map(([id, s]) => ({ id, ...s }));
    return { installed: true, devices, sessions };
  }, [termData]);

  // ------------------------------------------------------------ actions
  async function forkListing(L, andThen) {
    if (!host.viewer) return;
    try {
      setBusy('Copying files');
      const { forkId, record } = await host.fork(L, setBusy);
      setBusy('');
      setFlash({ kind: 'ok', text: L.title + ' is in My experiments.' });
      go({ v: 'fork', id: forkId });
      if (andThen) await andThen(forkId, record);
    } catch (e) { setBusy(''); setFlash({ kind: 'bad', text: 'Could not fork: ' + (e.message || e) }); }
  }

  const startJob = (forkId, fork, kind, request, machine, options) => host.startJob({ ...fork, id: forkId }, kind, request, machine, options);
  const rigFlash = (kind, text) => setFlash({ kind, text });
  // A rig config editor (or the new-config form) is its own screen of Machines: ?ehv=machines&ehid=<machine>&ehtab=<config|new>.
  const rigEditor = () => (caps.rigConfigs && route.id ? <RigConfigEditor key={route.id + ':' + route.tab} host={host} mid={route.id} cid={route.tab || 'new'} go={go} onFlash={rigFlash} homeLabel={caps.machines ? 'My machines' : 'Machines'} /> : null);

  async function newExperiment(title, request) {
    const forkId = await host.newExperiment(title, request);
    go({ v: 'fork', id: forkId, tab: 'changes' });
  }

  async function publishFork(fork, form) {
    try {
      setBusy('Publishing');
      const name = form.name;
      const res = await host.publish(fork, form, setBusy);
      setBusy(''); setDialog(null);
      const a = form.access || { visibility: 'public', people: [] };
      setFlash({ kind: 'ok', text: a.visibility === 'public' ? form.title + ' is on the marketplace under ' + name + '.' : form.title + ' is listed under ' + name + ': ' + accessDoneText(a, res) });
    } catch (e) { setBusy(''); setFlash({ kind: 'bad', text: 'Could not publish: ' + (e.message || e) }); }
  }

  async function unpublish(fork, listedAs) {
    try { await host.unpublish(fork); }
    catch (e) { setDialog(null); setFlash({ kind: 'bad', text: 'Could not remove the listing: ' + (e.message || e) }); return; }
    setDialog(null); setFlash({ kind: 'ok', text: fork.title + (listedAs && listedAs !== 'public' ? '’s listing was removed.' : ' was taken off the marketplace.') + ' Your copy is unchanged.' });
  }

  // Remove a copy from the library (My experiments). The host deletes the copy with its revisions
  // and job records, plus the docs snapshot when no other copy uses it; nothing on a machine's disk.
  async function removeFork(fork) {
    try {
      setBusy('Removing');
      await host.removeFork(fork);
      setBusy(''); setDialog(null);
      if (route.v === 'fork') go({ v: 'mine' }, true);
      setFlash({ kind: 'ok', text: fork.title + ' was removed from your library.' });
    } catch (e) { setBusy(''); setDialog(null); setFlash({ kind: 'bad', text: 'Could not remove ' + fork.title + ': ' + (e.message || e) }); }
  }

  // Standalone hosts: a changed copy comes back as a zip and becomes a new revision.
  async function uploadRevision(fork) {
    const file = upload.file; const message = upload.message.trim();
    if (!file || !message) return;
    try {
      setBusy('Uploading');
      await host.uploadRevision(fork, file, message);
      setBusy(''); setUpload({ file: null, message: '' });
      setFlash({ kind: 'ok', text: 'Saved a new revision of ' + fork.title + '.' });
    } catch (e) { setBusy(''); setFlash({ kind: 'bad', text: 'Could not upload: ' + (e.message || e) }); }
  }

  async function downloadRevision(fork) {
    try { await host.downloadArchive(fork.head.path, fork.id + '.zip'); }
    catch (e) { setFlash({ kind: 'bad', text: 'Could not download: ' + (e.message || e) }); }
  }

  const showChat = (chatId) => host.showChat(chatId);

  // ------------------------------------------------------------ who can see a listing (design.md section 5a)
  const ownListing = (forkId) => (viewerUid ? listings.find((x) => x.key === viewerUid + '~' + forkId) || null : null);
  async function labelPeople(uids) {
    const missing = (uids || []).filter((u) => !peopleNames[u]);
    if (!missing.length || !host.describePeople) return;
    try { const got = await host.describePeople(missing); setPeopleNames((p) => ({ ...p, ...got })); }
    catch (e) { console.warn('Alhazen Experiment Platform: could not look up the people a listing is shared with; their account ids are shown instead.', e); }
  }
  // The dialog's working copy of a listing's access: people carry the label the dialog shows.
  function accessDraft(L) {
    const a = L ? accessOf(L) : { visibility: 'public', people: [] };
    const names = { ...peopleNames, ...((L && L.access && L.access.names) || {}) };
    if (L && a.people.some((u) => !names[u])) labelPeople(a.people);
    return { visibility: a.visibility, people: a.people.map((uid) => ({ uid, label: names[uid] || '' })), query: '', results: null, finding: false, findError: '', notify: true, initial: a.people };
  }
  const setAccessDraft = (patch) => setDialog((prev) => (prev && prev.access ? { ...prev, error: '', access: { ...prev.access, ...(typeof patch === 'function' ? patch(prev.access) : patch) } } : prev));
  function accessDraftValue(a, publisherUid) {
    return normalizeAccess({ visibility: a.visibility, people: a.people.map((p) => p.uid) }, publisherUid);
  }
  function accessDoneText(next, res) {
    const n = next.people.length;
    let text = next.visibility === 'public' ? 'everyone can see it.' : next.visibility === 'private' ? 'only you can see it.' : 'you and ' + n + (n === 1 ? ' person' : ' people') + ' can see it.';
    const sent = (res && res.notified && res.notified.sent) || 0, failed = (res && res.notified && res.notified.failed) || [];
    if (sent) text += ' Emailed ' + sent + (sent === 1 ? ' person' : ' people') + ' a link.';
    if (failed.length) text += ' Could not email ' + failed.join(', ') + '.';
    return text;
  }
  async function saveAccess(d) {
    let next;
    try { next = accessDraftValue(d.access, d.L.uid); } catch (e) { setDialog({ ...d, error: e.message }); return; }
    try {
      setBusy('Saving');
      const res = await host.setAccess(d.L, { ...next, notify: !!(caps.notifyPeople && d.access.notify) });
      setPeopleNames((p) => ({ ...p, ...Object.fromEntries(d.access.people.filter((x) => x.label).map((x) => [x.uid, x.label])) }));
      setBusy(''); setDialog(null);
      setFlash({ kind: 'ok', text: d.L.title + ': ' + accessDoneText(next, res) });
    } catch (e) { setBusy(''); setDialog({ ...d, error: e.message || String(e) }); }
  }
  const openAccess = (L) => setDialog({ kind: 'access', L, access: accessDraft(L), error: '' });
  // A share link to a listing the host has not listed yet (Mana keeps non-public listings outside
  // the hub): ask the host once whether this viewer can read it.
  React.useEffect(() => {
    if (route.v !== 'exp' || published === null || !route.id || !host.resolveListing) return;
    if (listings.some((x) => x.key === route.id) || resolved[route.id]) return;
    const k = route.id, i = k.indexOf('~');
    if (i <= 0) return;
    setResolved((p) => ({ ...p, [k]: 'pending' }));
    host.resolveListing(k.slice(0, i), k.slice(i + 1)).then((ok) => setResolved((p) => ({ ...p, [k]: ok ? 'found' : 'missing' })),
      (e) => { console.warn('Alhazen Experiment Platform: could not open the shared listing ' + k, e); setResolved((p) => ({ ...p, [k]: 'missing' })); });
  }, [route.v, route.id, published === null, listings.length]);
  // The publisher's own listing: show who it is shared with by name.
  const routeListing = route.v === 'exp' ? listings.find((x) => x.key === route.id) : null;
  const routePeople = routeListing && accessCanManage(routeListing, viewerUid) && !(routeListing.access && routeListing.access.names) ? accessOf(routeListing).people.join(',') : '';
  React.useEffect(() => { if (routePeople) labelPeople(routePeople.split(',')); }, [routePeople]);

  // The "Who can see it" fields of the Access and publish dialogs.
  function accessFields(a, publisherUid) {
    const choices = ACCESS_CHOICES.filter(([k]) => k !== 'people' || caps.shareWithPeople || a.visibility === 'people');
    const explain = { private: 'Only you. It is listed in My experiments, where you can run it.', people: 'You and the people you add. Nobody else can find it.', public: 'Everyone who can open this platform can see it and add it to their library.' };
    const byEmail = !!caps.notifyPeople;
    const find = async () => {
      const q = a.query.trim(); if (!q || a.finding) return;
      setAccessDraft({ finding: true, findError: '', results: null });
      try {
        const found = (await host.findPeople(q)).filter((p) => p.uid !== publisherUid);
        setAccessDraft({ finding: false, results: found, findError: found.length ? '' : byEmail ? 'No Mana account uses that address.' : 'Nobody matches.' });
      } catch (e) { setAccessDraft({ finding: false, findError: e.message || String(e) }); }
    };
    const add = (p) => setAccessDraft((cur) => ({ people: cur.people.some((x) => x.uid === p.uid) ? cur.people : [...cur.people, p], query: byEmail ? '' : cur.query, results: byEmail ? null : cur.results, findError: '' }));
    const newcomers = a.people.filter((p) => !(a.initial || []).includes(p.uid));
    return <fieldset className="eh-access">
      <legend>Who can see it</legend>
      {choices.map(([k, label]) => <label key={k} className="eh-access-choice"><input type="radio" name="eh-access" checked={a.visibility === k} onChange={() => setAccessDraft({ visibility: k })} /><span><b>{label}</b><small>{explain[k]}</small></span></label>)}
      {a.visibility === 'people' ? <div className="eh-access-people">
        <div className="field"><label htmlFor="eh-access-q">{byEmail ? 'Add a person by email' : 'Add a person'}</label>
          <div className="eh-access-find"><input id="eh-access-q" type={byEmail ? 'email' : 'text'} value={a.query} placeholder={byEmail ? 'name@example.org' : 'Name or username'} autoComplete="off"
            onChange={(e) => setAccessDraft({ query: e.target.value, findError: '' })} onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); find(); } }} />
            <button className="btn btn-line" type="button" disabled={!a.query.trim() || a.finding} onClick={find}>{a.finding ? 'Looking…' : 'Find'}</button></div></div>
        {a.findError ? <p className="eh-access-error" role="alert">{a.findError}</p> : null}
        {a.results && a.results.length ? <ul className="eh-access-results" aria-label="Matching people">{a.results.map((p) => { const on = a.people.some((x) => x.uid === p.uid); return <li key={p.uid}><span>{p.label}</span><button className="btn btn-line btn-small" type="button" disabled={on} onClick={() => add(p)}>{on ? 'Added' : 'Add'}</button></li>; })}</ul> : null}
        {a.people.length ? <ul className="eh-access-list" aria-label="People who can see it">{a.people.map((p) => <li key={p.uid}><span>{p.label || peopleNames[p.uid] || p.uid}</span><button className="btn btn-quiet btn-small" type="button" aria-label={'Remove ' + (p.label || peopleNames[p.uid] || p.uid)} onClick={() => setAccessDraft((cur) => ({ people: cur.people.filter((x) => x.uid !== p.uid) }))}>Remove</button></li>)}</ul>
          : <p className="eh-access-none">Nobody added yet.</p>}
        {byEmail && newcomers.length ? <label className="check"><input type="checkbox" checked={!!a.notify} onChange={() => setAccessDraft((cur) => ({ notify: !cur.notify }))} />{newcomers.length === 1 ? 'Email them a link' : 'Email the ' + newcomers.length + ' new people a link'}</label> : null}
      </div> : null}
    </fieldset>;
  }

  // ------------------------------------------------------------ pieces
  function card(L, featured) {
    const owned = forkedFrom[L.key];
    return (
      <button key={L.key} className={'card' + (featured ? ' card-featured' : '')} onClick={() => go({ v: 'exp', id: L.key })}>
        <div className="card-art"><EHExperimentLogo item={L} wide={featured}/></div>
        <div className="card-body">
          <div className="card-title">{L.title}</div>
          {L.original && L.original.citation ? <p className="card-orig">After {origShort(L)}</p> : null}
          {L.methodSummary || L.summary ? <p className="card-summary">{L.methodSummary || L.summary}</p> : null}
          <p className="card-by">{(L.publisher || {}).name || 'Unknown'}</p>
        </div>
        <div className="card-foot">
          {hwKeys(L).map((k) => <span key={k} className="need" title={(HW.find((x) => x[0] === k) || [])[1]}><Glyph k={k} />{featured || k === 'display' ? (HW.find((x) => x[0] === k) || [])[1] : null}</span>)}
          <span>{asList(L.subjects).map((s) => s === 'human' ? 'Human' : 'Monkey').join(' · ')}</span>
          <span className="sp" />
          {accessBadge(L, viewerUid) ? <span className="tag-access">{accessBadge(L, viewerUid)}</span> : null}
          {L.status ? <span className={'tag-status st-' + L.status} title={(STATUS[L.status] || [])[1]}>{(STATUS[L.status] || [L.status])[0]}</span> : null}
          {owned ? <span className="owned"><Glyph k="check" />Forked</span> : <span>{L.version ? 'v' + L.version : ''}</span>}
        </div>
      </button>
    );
  }

  function Flash() {
    if (!flash && !busy) return null;
    const kind = busy ? 'st-idle' : flash.kind === 'ok' ? 'st-ok' : 'st-bad';
    return <div className={'flash ' + kind} role="status"><span>{busy ? busy + '…' : flash.text}</span>{busy ? null : <button className="btn btn-quiet btn-small" onClick={() => setFlash(null)}>Dismiss</button>}</div>;
  }

  // ------------------------------------------------------------ views
  function Home() {
    // Home's sections show what everyone sees; listings shared with the viewer get their own section.
    const open = listings.filter((L) => accessGroup(L, viewerUid) === 'public');
    const shared = listings.filter((L) => accessGroup(L, viewerUid) === 'with-you');
    const featured = open.filter((L) => L.featured).slice(0, 2);
    return (
      <>
        <section className="eh-hero">
          <div>
            <h1>Run Experiments Now</h1>
            <p className="eh-lede">{caps.changeWithMana ? 'Alhazen experiments you can fork, change with Mana and run on your own rig.' : 'Alhazen experiments you can fork, change and run on your own rig.'}</p>
            <div className="eh-actions">
              <button className="btn btn-primary" onClick={() => go({ v: 'browse' })}>Browse experiments</button>
              <button className="btn btn-line" onClick={() => go({ v: 'mine' })}>My experiments</button>
            </div>
          </div>
          <div className="eh-art" dangerouslySetInnerHTML={{ __html: assets.hero || '' }} />
        </section>
        {shared.length ? <section className="eh-section"><div className="eh-sechead"><h2>Shared with you</h2>{shared.length > 3 ? <button className="btn btn-quiet" onClick={() => { setBf({ ...bf, vis: 'with-you' }); go({ v: 'browse' }); }}>View all</button> : null}</div>
          <div className="eh-grid">{shared.slice(0, 3).map((L) => card(L))}</div></section> : null}
        {featured.length ? <section className="eh-section"><div className="eh-sechead"><h2>Featured</h2></div><div className="eh-featured">{featured.map((L) => card(L, true))}</div></section> : null}
        {open.some((L) => collOf(L) === 'classics') ? <section className="eh-section"><div className="eh-sechead"><h2>Classics</h2><button className="btn btn-quiet" onClick={() => { setBf({ ...bf, coll: 'classics' }); go({ v: 'browse' }); }}>View all</button></div>
          <p className="eh-muted" style={{ margin: '-6px 0 14px', fontSize: 14 }}>Landmark experiments rebuilt from their original methods sections. Each says whether it follows the original protocol or adapts it, and lists every value the paper did not state.</p>
          <div className="eh-grid">{open.filter((L) => collOf(L) === 'classics').sort((a, b) => ((a.original || {}).year || 0) - ((b.original || {}).year || 0)).slice(0, 6).map((L) => card(L))}</div></section> : null}
        <section className="eh-section">
          <div className="eh-sechead"><h2>Latest</h2><button className="btn btn-quiet" onClick={() => go({ v: 'browse' })}>View all</button></div>
          {published === null ? <p className="eh-muted">Loading…</p> : <div className="eh-grid">{open.slice(0, 6).map((L) => card(L))}</div>}
        </section>
        <section className="eh-section">
          <div className="eh-sechead"><h2>How it works</h2></div>
          <div className="eh-steps">
            <div className="eh-step"><b>1</b><h3>Fork</h3><p>Copy an experiment into My experiments. Your copy is yours to change.</p></div>
            {caps.changeWithMana ? <div className="eh-step"><b>2</b><h3>Change</h3><p>Describe what should be different; Mana edits the code, runs its tests and saves a revision.</p></div>
              : <div className="eh-step"><b>2</b><h3>Change</h3><p>Download a revision as a zip, change it on your computer and upload the changed copy. Each upload is a new revision.</p></div>}
            {caps.runOnMachines ? <div className="eh-step"><b>3</b><h3>Run</h3><p>Type <span className="cmd">mana</span> in a terminal on your rig. Mana copies the experiment there, builds its environment and opens the dashboard.</p></div>
              : <div className="eh-step"><b>3</b><h3>Run</h3><p>Unpack a revision on your rig and run the two commands under Run. The alhazen dashboard opens there; session data stays on the rig.</p></div>}
          </div>
        </section>
      </>
    );
  }

  function Browse() {
    const { q, cat, hw, who, sort, coll, vis } = bf;
    const setColl = (v) => setBf({ ...bf, coll: v }), setVis = (v) => setBf({ ...bf, vis: v });
    const setQ = (v) => setBf({ ...bf, q: v }), setCat = (v) => setBf({ ...bf, cat: v }), setSort = (v) => setBf({ ...bf, sort: v });
    const setHw = (v) => setBf({ ...bf, hw: v }), setWho = (v) => setBf({ ...bf, who: v });
    const toggle = (list, set, k) => set(list.includes(k) ? list.filter((x) => x !== k) : [...list, k]);
    const match = (L, skip) => {
      if (skip !== 'coll' && coll && collOf(L) !== coll) return false;
      if (skip !== 'vis' && vis && accessGroup(L, viewerUid) !== vis) return false;
      if (skip !== 'cat' && cat && !asList(L.tags).includes(cat)) return false;
      if (skip !== 'hw' && hw.length && !hw.some((k) => hwKeys(L).includes(k))) return false;
      if (skip !== 'who' && who.length && !who.some((k) => asList(L.subjects).includes(k))) return false;
      if (q) { const hay = (L.title + ' ' + (L.summary || '') + ' ' + ((L.publisher || {}).name || '') + ' ' + ((L.original || {}).citation || '')).toLowerCase(); if (!q.toLowerCase().split(/\s+/).every((w) => hay.includes(w))) return false; }
      return true;
    };
    let list = listings.filter((L) => match(L));
    if (sort === 'name') list = [...list].sort((a, b) => a.title.localeCompare(b.title));
    if (sort === 'year') list = [...list].sort((a, b) => ((a.original || {}).year || 9999) - ((b.original || {}).year || 9999));
    const catCount = (k) => listings.filter((L) => match(L, 'cat') && asList(L.tags).includes(k)).length;
    return (
      <div className="eh-browse">
        <aside className="eh-filters" aria-label="Filters">
          <div><h3>Category</h3><div className="eh-chips">
            <button className="chip" aria-pressed={!cat} onClick={() => setCat('')}><span>All</span><span>{listings.filter((L) => match(L, 'cat')).length}</span></button>
            {CATEGORIES.map(([k, label]) => <button key={k} className="chip" aria-pressed={cat === k} onClick={() => setCat(cat === k ? '' : k)}><span>{label}</span><span>{catCount(k)}</span></button>)}
          </div></div>
          {listings.some((L) => accessGroup(L, viewerUid) !== 'public') || vis ? <div><h3>Visible to</h3><div className="eh-chips">
            <button className="chip" aria-pressed={!vis} onClick={() => setVis('')}><span>All</span><span>{listings.filter((L) => match(L, 'vis')).length}</span></button>
            {VISIBLE_TO.map(([k, label]) => { const n = listings.filter((L) => match(L, 'vis') && accessGroup(L, viewerUid) === k).length; return n || vis === k ? <button key={k} className="chip" aria-pressed={vis === k} onClick={() => setVis(vis === k ? '' : k)}><span>{label}</span><span>{n}</span></button> : null; })}
          </div></div> : null}
          <div><h3>Hardware</h3>{HW.map(([k, label]) => <label key={k} className="check"><input type="checkbox" checked={hw.includes(k)} onChange={() => toggle(hw, setHw, k)} />{label}</label>)}</div>
          <div><h3>Subjects</h3>{WHO.map(([k, label]) => <label key={k} className="check"><input type="checkbox" checked={who.includes(k)} onChange={() => toggle(who, setWho, k)} />{label}</label>)}</div>
        </aside>
        <div>
          <div className="eh-toolbar">
            <input className="eh-search" placeholder="Search experiments" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search experiments" />
            <select className="eh-select" value={sort} onChange={(e) => setSort(e.target.value)} aria-label="Sort"><option value="newest">Newest</option><option value="name">Name</option><option value="year">Year of the original</option></select>
          </div>
          <div className="eh-colls" role="group" aria-label="Collection">
            <button className="chip" aria-pressed={!coll} onClick={() => setColl('')}><span>All</span><span>{listings.filter((L) => match(L, 'coll')).length}</span></button>
            {COLLECTIONS.map(([k, label]) => { const n = listings.filter((L) => match(L, 'coll') && collOf(L) === k).length; return n || coll === k ? <button key={k} className="chip" aria-pressed={coll === k} onClick={() => setColl(coll === k ? '' : k)}><span>{label}</span><span>{n}</span></button> : null; })}
          </div>
          <p className="eh-muted" style={{ margin: '0 0 12px', fontSize: 14 }}>{list.length === listings.length ? `${list.length} experiment${list.length === 1 ? '' : 's'}` : `${list.length} of ${listings.length} experiments`}</p>
          {list.length ? <div className="eh-grid">{list.map((L) => card(L))}</div> : <div className="empty"><h3>No experiments match</h3><p>Clear a filter or the search.</p></div>}
        </div>
      </div>
    );
  }

  const currentListing = listings.find(x => x.key === route.id);
  React.useEffect(() => {
    if (route.v !== 'exp' || !currentListing || currentListing.source?.kind !== 'repo') return;
    const key = currentListing.key;
    return host.subscribeDocs(currentListing, record => {
      setDocs(previous => ({...previous,[key]:record || {readme:'This version has no published Methods document.'}}));
    }, error => setDocs(previous => ({...previous,[key]:{__loadError:error.message || String(error)}})));
  },[host.key,route.v,route.id,currentListing?.source?.kind]);

  // [2026-10-16] Methods for a private copy: the docs snapshot of the version it was forked from,
  // saved beside the copy (MINE/docs/<origin bundle>). Older copies are backfilled from the hub's docs
  // when those still describe the same bundle; otherwise the copy's README stands in.
  const currentFork = route.v === 'fork' && forks ? forks[route.id] || null : null;
  const forkOriginBundle = (currentFork && currentFork.origin && !currentFork.origin.scratch && currentFork.origin.bundle) || '';
  const forkHeadId = (currentFork && currentFork.head && currentFork.head.id) || '';
  React.useEffect(() => {
    if (route.v !== 'fork' || !viewerUid || !currentFork) return;
    const f = currentFork;
    const key = 'forkdoc:' + (forkOriginBundle || 'head:' + (forkHeadId || route.id));
    let off = false;
    const readmeOnly = async (extra) => {
      const readme = f.head ? (await readFile(f.head.path, 'README.md')) || '' : '';
      if (!off) setDocs((p) => ({ ...p, [key]: { readme: readme || 'This experiment has no Methods document yet.', ...extra } }));
    };
    if (!forkOriginBundle) { readmeOnly({}).catch((e) => { if (!off) setDocs((p) => ({ ...p, [key]: { __loadError: e.message || String(e) } })); }); return () => { off = true; }; }
    // The host delivers {docs} (the snapshot saved with the copy) or {stale: true} (no snapshot of
    // that version exists any more; the copy's README stands in). Backfilling is the host's business.
    const unsub = host.subscribeForkDocs({ ...f, id: route.id }, async (result) => {
      const v = result && result.docs;
      if (v) { if (!off) setDocs((p) => ({ ...p, [key]: { ...v, __commit: ehDocCommit(v) } })); return; }
      if (result && result.stale) await readmeOnly({ __stale: true });
    }, (e) => { if (!off) setDocs((p) => ({ ...p, [key]: { __loadError: e.message || String(e) } })); });
    return () => { off = true; if (unsub) unsub(); };
  }, [route.v, route.id, viewerUid, host.key, forkOriginBundle, forkHeadId, !!currentFork]);

  // ------------------------------------------------------------ lazy data
  const chunkCache = React.useRef(new Map());
  async function bundleMeta(path) {
    if (metas[path]) return metas[path];
    try {
      const m = await host.bundleMeta(path);
      if (!m) throw new Error('The source bundle is not available for this version.');
      setMetas(p => ({...p,[path]:m})); return m;
    } catch (error) {
      const failed = {__error:error.message || String(error)};
      setMetas(p => ({...p,[path]:failed})); return failed;
    }
  }
  async function readFile(bundlePath, file) {
    const meta = await bundleMeta(bundlePath); if (!meta) return null; if (meta.__error) throw new Error(meta.__error);
    for (let i = 0; i < (meta.chunkCount || 0); i++) {
      const key = bundlePath + '#' + i;
      if (!chunkCache.current.has(key)) chunkCache.current.set(key, await host.bundleChunk(bundlePath, i));
      const hit = chunkCache.current.get(key).find((f) => f[0] === file);
      if (hit) return hit[3] === 'b64' ? { b64: hit[1] } : hit[1];
    }
    return null;
  }
  async function openFile(bundlePath, file) {
    setFileView({ bundle: bundlePath, path: file, text: null });
    const got = await readFile(bundlePath, file);
    if (got && typeof got === 'object') setFileView({ bundle: bundlePath, path: file, text: got.b64, binary: true });
    else setFileView({ bundle: bundlePath, path: file, text: got === null ? '(not found)' : got });
  }
  React.useEffect(() => {
    let off = false;
    (async () => {
      if (route.v === 'exp') {
        const L = listings.find((x) => x.key === route.id); if (!L) return;
        if (!docs[L.key] && L.source?.kind !== 'repo') {
          // Only listings without a documentation record reach here (repo listings subscribe above).
          const readme = await readFile(L.bundle.path, 'README.md') || '';
          if (!off) setDocs((p) => ({ ...p, [L.key]: { readme } }));
        }
        if (route.tab === 'files') bundleMeta(L.bundle.path);
      } else if (route.v === 'fork' && forks) {
        const f = forks[route.id]; if (!f || !f.head) return;
        if (route.tab === 'files') bundleMeta(f.head.path);
      }
    })();
    return () => { off = true; };
  }, [route.v, route.id, route.tab, listings.length, forks && route.id && forks[route.id] && forks[route.id].head && forks[route.id].head.id]);

  function filesTab(bundlePath) {
    const meta = metas[bundlePath];
    if (!meta) return <p className="eh-muted">Loading files…</p>;
    if (meta.__error) return <div className="w-full eh-doc-error" role="alert"><p>{meta.__error}</p></div>;
    const files = JSON.parse(meta.files || '[]'); const excluded = JSON.parse(meta.excluded || '[]');
    const groups = {};
    for (const [p, b] of files) { const top = p.includes('/') ? p.split('/')[0] + '/' : ''; (groups[top] = groups[top] || []).push([p, b]); }
    const cur = fileView && fileView.bundle === bundlePath ? fileView : null;
    return (
      <>
        <p className="eh-muted" style={{ margin: '0 0 14px', fontSize: 14 }}>{meta.fileCount} files, {kb(meta.bytes)}{meta.binaries ? `, including ${meta.binaries} binary file${meta.binaries === 1 ? '' : 's'} (pictures, stimulus banks)` : ''}{excluded.length ? `. ${excluded.length} large binary files stay in the source repository and are not part of this copy.` : '.'}</p>
        <div className="eh-files">
          <nav className="eh-tree" aria-label="Files">
            {Object.keys(groups).sort().map((g) => (
              <div key={g || '_'}>
                <div className="dir">{g || 'Top level'}</div>
                {groups[g].map(([p, b]) => <button key={p} aria-current={cur && cur.path === p} onClick={() => openFile(bundlePath, p)}><span className="mono">{g ? p.slice(g.length) : p}</span><span className="eh-muted">{kb(b)}</span></button>)}
              </div>
            ))}
          </nav>
          <div className="eh-code">
            <header><span className="mono">{cur ? cur.path : 'No file open'}</span>{cur && cur.text && !cur.binary ? <button className="btn btn-quiet btn-small" onClick={async () => { const r = await host.copyText(cur.text); setFlash({ kind: r === 'failed' ? 'bad' : 'ok', text: r === 'failed' ? 'Could not copy.' : 'Copied ' + cur.path }); }}>Copy</button> : null}</header>
            {cur && cur.binary ? (/\.(png|jpe?g|gif|webp)$/i.test(cur.path) ? <img className="file-img" alt={cur.path} src={'data:image/' + (/\.png$/i.test(cur.path) ? 'png' : /\.gif$/i.test(cur.path) ? 'gif' : /\.webp$/i.test(cur.path) ? 'webp' : 'jpeg') + ';base64,' + cur.text} /> : <pre>Binary file; it is part of the copy you fork.</pre>) : <pre>{cur ? (cur.text === null ? 'Loading…' : cur.text) : 'Choose a file on the left.'}</pre>}
          </div>
        </div>
      </>
    );
  }

  function tabs(list, active, onPick) {
    return <div className="eh-tabs" role="tablist">{list.map(([k, label]) => <button key={k} role="tab" aria-selected={active === k} onClick={() => onPick(k)}>{label}</button>)}</div>;
  }

  function tasksTab(d) {
    const tasks = JSON.parse((d && d.tasks) || '[]');
    if (!tasks.length) return <p className="eh-muted">No task documentation in this listing.</p>;
    return tasks.map((t) => {
      const groups = {}; for (const p of t.parameters || []) (groups[p.group || 'Other'] = groups[p.group || 'Other'] || []).push(p);
      return (
        <div key={t.id} className="task">
          <div className="task-head"><h3>{t.title}</h3><p>{t.summary}</p></div>
          {(t.outcomes || []).length ? <details><summary>Outcomes <span>{t.outcomes.length}</span></summary>
            <table className="ptable"><tbody>{t.outcomes.map((o) => <tr key={o.name}><td className="mono">{o.name}</td><td className="meaning">{o.meaning}</td><td>{o.success ? 'Success' : o.completed ? 'Counted' : 'Served again'}</td></tr>)}</tbody></table></details> : null}
          {(t.parameters || []).length ? <details><summary>Parameters <span>{t.parameters.length}</span></summary>
            <table className="ptable"><thead><tr><th>Parameter</th><th>Default</th><th>Meaning</th></tr></thead><tbody>
              {Object.entries(groups).map(([g, ps]) => [<tr key={'g' + g} className="group"><td colSpan="3">{g}</td></tr>,
                ...ps.map((p) => <tr key={p.name}><td>{p.label || p.name}<div className="mono eh-muted">{p.name}</div></td><td className="mono">{p.default === undefined ? '' : (Array.isArray(p.default) ? p.default.join(', ') : String(p.default))}{p.unit ? ' ' + p.unit : ''}</td><td className="meaning">{p.meaning}</td></tr>)])}
            </tbody></table></details> : null}
        </div>
      );
    });
  }

  function replication(L, d) {
    const ass = JSON.parse((d && d.assumed) || '[]'), dev = JSON.parse((d && d.deviations) || '[]');
    const st = STATUS[L.status] || [L.status, ''];
    const tbl = (rows, cols) => <table><thead><tr>{cols.map(([k, l]) => <th key={k}>{l}</th>)}</tr></thead><tbody>{rows.map((r, i) => <tr key={i}>{cols.map(([k]) => <td key={k}>{String(r[k] == null ? '' : r[k])}</td>)}</tr>)}</tbody></table>;
    return (
      <div className="repl">
        <h3>{L.status ? st[0] : 'Replication'}{L.original && L.original.citation ? <span className="eh-muted" style={{ fontWeight: 500 }}> · of {origShort(L)}</span> : null}</h3>
        {L.statusReason ? <p>{L.statusReason}</p> : st[1] ? <p>{st[1]}</p> : null}
        {L.original && L.original.citation ? <p>{L.original.citation}{L.original.experiments ? ' — ' + L.original.experiments : ''}{L.original.url ? <> · <a href={L.original.url} target="_blank" rel="noreferrer">paper</a></> : null}</p> : null}
        <p>Tested in software (unit tests and a simulated session). No participant data has been recorded with this version.</p>
        <details><summary>Deviations from the original ({dev.length})</summary>{dev.length ? tbl(dev, [['item', 'Item'], ['original', 'Original'], ['here', 'Here'], ['reason', 'Reason']]) : <p>None.</p>}</details>
        <details><summary>Assumed, not in the paper ({ass.length})</summary><p>Values and rules the paper does not state, chosen here. They are not the paper's.</p>{ass.length ? tbl(ass, [['item', 'Item'], ['value', 'Value chosen'], ['reason', 'Reason']]) : <p>None: the paper states everything this version uses.</p>}</details>
      </div>
    );
  }

  function Exp() {
    const L = listings.find(x => x.key === route.id);
    const resolving = resolved[route.id] === 'pending' || resolved[route.id] === 'found';
    if (!L) return published === null || resolving ? <p className="eh-muted">Loading…</p> : <div className="empty"><h3>This experiment is not in the catalogue</h3><p>It may have been taken off, or it is not shared with you. <button className="btn btn-quiet" onClick={() => go({v:'browse'})}>Browse experiments</button></p></div>;
    const d = docs[L.key];
    const mine = forkedFrom[L.key];
    const actions = mine ? <><button className="btn btn-primary" onClick={() => go({v:'fork',id:mine})}>Open my copy</button><button className="btn btn-line" disabled={!!busy} onClick={() => forkListing(L)}>Fork again</button></> : <>{caps.signIn && !viewerUid ? <button className="btn btn-primary" onClick={() => host.signIn()}>Sign in to add to library</button> : <button className="btn btn-primary" disabled={!!busy || !viewerUid} onClick={() => forkListing(L)}>Add to library</button>}{caps.changeWithMana ? <button className="btn btn-line" disabled={!!busy || !viewerUid} onClick={() => setDialog({kind:'forkai',L,text:''})}>Fork with AI</button> : null}</>;
    const onFile = async file => {
      go({v:'exp',id:L.key,tab:'files'});
      try { await openFile(L.bundle.path,file); }
      catch (error) { setFileView({bundle:L.bundle.path,path:file,text:'Could not load this file: '+(error.message || error)}); }
    };
    // Who can see it: the publisher sees it for every listing and can change it; anyone else sees it
    // only when the listing was shared with them.
    const manage = accessCanManage(L, viewerUid) && !!host.setAccess;
    const accessRow = manage || accessBadge(L, viewerUid) ? <div className="eh-access-row"><dt>Visible to</dt><dd><span>{accessSummary(L, viewerUid, { ...peopleNames, ...((L.access && L.access.names) || {}) })}</span>{manage ? <button className="eh-inline-link" disabled={!!busy} onClick={() => openAccess(L)}>Change</button> : null}</dd></div> : null;
    return <EHDocumentationBoundary key={L.key+':'+(d?.metadata?.updatedAt || d?.__loadError || 'loading')}><EHExperimentPage L={L} d={d} route={route} art={art} actions={actions} renderTabs={tabs} renderFiles={filesTab} renderReplication={replication} go={go} onFile={onFile} accessBadge={accessBadge(L, viewerUid)} accessRow={accessRow} /></EHDocumentationBoundary>;
  }

  function statusChip(st) {
    const m = { running: ['st-warn', 'Working'], done: ['st-ok', 'Done'], waiting: ['st-warn', 'Needs you'], failed: ['st-bad', 'Failed'], stopped: ['st-idle', 'Stopped'] }[st] || ['st-idle', st || '—'];
    return <span className={'status ' + m[0]}>{m[1]}</span>;
  }

  function machinesPanel(compact) {
    const online = machines.sessions;
    return (
      <div className="panel">
        <h3>Your machines</h3>
        {!machines.installed ? <p>Mana Terminal is not installed in your Mana. It is what lets Mana copy an experiment to your rig and start it there. <a href={host.links.terminalListing}>Get Mana Terminal</a>, pair the rig once, then type <span className="cmd">mana</span> in a terminal on it.</p> : <>
          <p>{online.length ? `${online.length} terminal window${online.length === 1 ? '' : 's'} open. Runs go to the newest one unless you pick another.` : <>No terminal window is open. On the rig, open a terminal and type <span className="cmd">mana</span>.</>} <button className="btn btn-quiet btn-small" style={{ padding: 0, height: 'auto' }} onClick={() => { setDialog(null); go({ v: 'machines' }); }}>My machines</button></p>
          {!compact ? <div style={{ marginTop: 12 }}>
            {machines.devices.map((dv) => { const n = online.filter((s) => s.device === dv.id).length; return (
              <div key={dv.id} className="term-line"><span className="dotlamp" style={{ background: n ? 'var(--ok-ink)' : 'var(--line-strong)' }} /><span style={{ fontWeight: 700 }}>{dv.label || dv.host}</span><span className="eh-muted">{dv.os}</span><span style={{ flex: 1 }} /><span className="eh-muted">{n ? `${n} window${n === 1 ? '' : 's'} open` : 'Seen ' + ago(dv.lastSeen)}</span></div>); })}
            {!machines.devices.length ? <p>No machine is paired yet. Open <a href={host.links.terminalApp}>Mana Terminal</a> to pair one.</p> : null}
          </div> : null}
        </>}
      </div>
    );
  }

  // How a published copy's listing is shown on its row: on the marketplace, or who it is shared with.
  function publishedChip(f) {
    const L = ownListing(f.id);
    const g = L ? accessGroup(L, viewerUid) : (f.published && f.published.visibility) === 'private' ? 'private' : (f.published && f.published.visibility) === 'people' ? 'by-you' : 'public';
    return g === 'public' ? <span className="status st-ok">On the marketplace</span> : <span className="tag-access">{g === 'private' ? 'Listed, only you' : 'Listed, shared'}</span>;
  }

  function listingRow(L, sub) {
    const badge = accessBadge(L, viewerUid);
    return <div key={L.key} className="row eh-mine-row">
      <button className="eh-row-open" onClick={() => go({ v: 'exp', id: L.key })}>
        <div className="row-art"><EHExperimentLogo item={L}/></div>
        <div className="row-main"><div className="row-title">{L.title}</div><div className="row-sub">{sub}</div></div>
        {badge ? <span className="tag-access">{badge}</span> : null}
      </button>
    </div>;
  }

  function Mine() {
    // Above the copies: the viewer's listings that are not on the marketplace and listings shared
    // with them; below, a collapsed list of their listings on the marketplace (design.md section 5a).
    const sec = librarySections(listings, viewerUid);
    const names = { ...peopleNames };
    for (const L of sec.notOnMarketplace) Object.assign(names, (L.access && L.access.names) || {});
    const facts = (L, first) => [first, L.version ? 'v' + L.version : '', L.updatedAt ? 'updated ' + ago(L.updatedAt) : ''].filter(Boolean).join(' · ');
    const hasListings = sec.notOnMarketplace.length || sec.sharedWithYou.length || sec.onMarketplace.length;
    return (
      <>
        <div className="eh-sechead" style={{ marginBottom: 6 }}><h1 style={{ fontSize: 34, fontWeight: 800 }}>My experiments</h1>
          {caps.newWithMana ? <button className="btn btn-primary" disabled={!viewerUid} onClick={() => setDialog({ kind: 'new', title: '', text: '' })}>New experiment</button> : null}</div>
        <p className="eh-muted" style={{ margin: '0 0 24px' }}>{hasListings ? 'Your listings, listings shared with you, and your copies. Only you can see and change your copies; publish one to list it for everyone, for people you choose, or only for you.' : 'Your copies. Only you can see and change them; publish one to list it for everyone, for people you choose, or only for you.'}</p>
        {sec.notOnMarketplace.length ? <section className="eh-mine-section"><h2>Not on the marketplace</h2>
          <p className="eh-muted">Your listings that only you, or the people you chose, can see. Add one to your library to change it or run it.</p>
          <div className="list">{sec.notOnMarketplace.map((L) => listingRow(L, facts(L, accessSummary(L, viewerUid, names))))}</div></section> : null}
        {sec.sharedWithYou.length ? <section className="eh-mine-section"><h2>Shared with you</h2>
          <div className="list">{sec.sharedWithYou.map((L) => listingRow(L, facts(L, 'From ' + ((L.publisher || {}).name || 'Unknown'))))}</div></section> : null}
        <section className="eh-mine-section">{hasListings ? <h2>Your copies</h2> : null}
          {forks === null ? <p className="eh-muted">Loading…</p> : forkList.length ? <div className="list">
            {forkList.map((f) => {
              const jobs = Object.values(f.jobs || {}).sort((a, b) => (b.startedAt || 0) - (a.startedAt || 0));
              const live = jobs.find((j) => j.status === 'running' || j.status === 'waiting');
              return (
                <div key={f.id} className="row eh-mine-row">
                  <button className="eh-row-open" onClick={() => go({ v: 'fork', id: f.id })}>
                    <div className="row-art"><EHExperimentLogo item={f}/></div>
                    <div className="row-main"><div className="row-title">{f.title}</div>
                      <div className="row-sub">{f.origin && f.origin.scratch ? 'Started from scratch' : `From ${f.origin.title}${f.origin.publisherName ? ' by ' + f.origin.publisherName : ''}`} · {Object.keys(f.revisions || {}).length} revision{Object.keys(f.revisions || {}).length === 1 ? '' : 's'} · changed {ago(f.updatedAt)}</div></div>
                    {f.published ? publishedChip(f) : null}
                    {live ? statusChip(live.status) : null}
                  </button>
                  <button className="btn btn-quiet btn-small eh-row-remove" aria-label={'Remove ' + f.title + ' from your library'} onClick={() => setDialog({ kind: 'remove', f })}>Remove</button>
                </div>);
            })}
          </div> : (caps.signIn && !viewerUid ? <div className="empty"><h3>Sign in to keep experiments</h3><p>Your copies are private to your account.</p><div className="eh-actions" style={{ justifyContent: 'center', marginTop: 14 }}><button className="btn btn-primary" onClick={() => host.signIn()}>Sign in</button></div></div> : <div className="empty"><h3>Nothing here yet</h3><p>{caps.newWithMana ? 'Fork an experiment from Browse, or start a new one.' : 'Fork an experiment from Browse.'}</p><div className="eh-actions" style={{ justifyContent: 'center', marginTop: 14 }}><button className="btn btn-line" onClick={() => go({ v: 'browse' })}>Browse experiments</button></div></div>)}
        </section>
        {sec.onMarketplace.length ? <details className="eh-mine-section eh-mine-public"><summary>On the marketplace <span>{sec.onMarketplace.length}</span></summary>
          <div className="list">{sec.onMarketplace.map((L) => listingRow(L, facts(L, 'Everyone')))}</div></details> : null}
      </>
    );
  }

  // [2026-10-16] My machines: its own top-level page (moved off My experiments).
  const openTerminalApp = () => host.openTerminalApp();
  function Machines() {
    const online = machines.sessions;
    const sessionLabel = (id) => { const s = online.find((x) => x.id === id); if (!s) return id; const dv = machines.devices.find((x) => x.id === s.device) || {}; return dv.label || dv.host || s.device; };
    const runs = [];
    for (const f of forkList) for (const [id, j] of Object.entries(f.jobs || {})) if (j && j.kind === 'run') runs.push({ ...j, id, fork: f });
    runs.sort((a, b) => (b.startedAt || 0) - (a.startedAt || 0));
    const devices = machines.devices.slice().sort((a, b) => (b.lastSeen || 0) - (a.lastSeen || 0));
    return (
      <>
        <div className="eh-sechead eh-machines-head" style={{ marginBottom: 6 }}><h1 style={{ fontSize: 34, fontWeight: 800 }}>My machines</h1>
          {machines.installed ? <button className="btn btn-line" onClick={openTerminalApp}>Pair a machine</button> : null}</div>
        <p className="eh-muted" style={{ margin: '0 0 24px' }}>The computers paired with Mana Terminal. Run on my machine copies an experiment to one of them, builds its environment and opens the alhazen dashboard there. Session data stays on the machine.</p>
        {termData === undefined ? <p className="eh-muted">Loading…</p>
          : !machines.installed ? <div className="empty"><h3>Mana Terminal is not installed</h3><p>It is what lets Mana copy an experiment to your rig and start it there. Install it, pair the rig once, then type <span className="cmd">mana</span> in a terminal on it.</p><div className="eh-actions" style={{ justifyContent: 'center', marginTop: 14 }}><a className="btn btn-primary" href={host.links.terminalListing}>Get Mana Terminal</a></div></div>
          : devices.length ? <div className="list">
            {devices.map((dv) => {
              const wins = online.filter((s) => s.device === dv.id).sort((a, b) => (b.startedAt || 0) - (a.startedAt || 0));
              return (
                <div key={dv.id} className="row eh-machine">
                  <span className="dotlamp" style={{ background: wins.length ? 'var(--ok-ink)' : 'var(--line-strong)' }} />
                  <div className="row-main"><div className="row-title">{dv.label || dv.host}</div>
                    <div className="row-sub">{[dv.os, dv.user ? 'user ' + dv.user : '', dv.pairedAt ? 'paired ' + ago(dv.pairedAt) : ''].filter(Boolean).join(' · ')}</div>
                    {wins.map((s) => <div key={s.id} className="row-sub eh-machine-win"><span className="mono">{s.shell || 'shell'}</span> · <span className="mono">{s.cwd || s.tty || s.id}</span>{s.startedAt ? ' · opened ' + ago(s.startedAt) : ''}</div>)}
                  </div>
                  {wins.length ? <span className="status st-ok">{wins.length} window{wins.length === 1 ? '' : 's'} open</span> : <span className="status st-idle">{dv.lastSeen ? 'Seen ' + ago(dv.lastSeen) : 'Offline'}</span>}
                </div>);
            })}
          </div>
          : <div className="empty"><h3>No machine is paired yet</h3><p>Pair a rig in Mana Terminal, then type <span className="cmd">mana</span> in a terminal on it.</p><div className="eh-actions" style={{ justifyContent: 'center', marginTop: 14 }}><button className="btn btn-primary" onClick={openTerminalApp}>Pair a machine</button></div></div>}
        {machines.installed && devices.length ? <p className="eh-muted" style={{ margin: '12px 0 0', fontSize: 14 }}>{online.length ? `Runs go to the newest open window unless you pick another.` : <>No terminal window is open. On the rig, open a terminal and type <span className="cmd">mana</span>.</>}</p> : null}
        {caps.rigConfigs ? <RigConfigsSection host={host} go={go} onFlash={rigFlash} /> : null}
        <div className="eh-sechead" style={{ marginTop: 36 }}><h2 style={{ fontSize: 20, fontWeight: 800, margin: 0 }}>Runs</h2></div>
        {forks === null ? <p className="eh-muted">Loading…</p> : runs.length ? <div className="list">
          {runs.slice(0, 30).map((j) => (
            <div key={j.fork.id + '~' + j.id} className="row eh-run-row">
              <div className="row-main"><div className="row-title">{j.fork.title} <span className="eh-muted" style={{ fontWeight: 500 }}>on {j.machine ? sessionLabel(j.machine) : 'my machine'}{j.rigConfig ? ' with ' + j.rigConfig : ''}</span></div>
                <div className="row-sub">{j.summary || (j.status === 'running' ? 'Mana is working on it.' : '')}{j.url ? <> · Dashboard: <span className="mono">{j.url.split('#')[0]}</span></> : null}</div>
                <div className="row-sub">Started {ago(j.startedAt)}{j.updatedAt && j.status !== 'running' ? ' · updated ' + ago(j.updatedAt) : ''}</div></div>
              {statusChip(j.status)}
              <button className="btn btn-line btn-small" onClick={() => go({ v: 'fork', id: j.fork.id, tab: 'run' })}>Open</button>
              {j.chatId ? <button className="btn btn-quiet btn-small" onClick={() => showChat(j.chatId)}>Chat</button> : null}
            </div>))}
        </div> : <p className="eh-muted">No runs yet. Open a copy in My experiments and press Run on my machine.</p>}
      </>
    );
  }

  function Fork() {
    const f = forks && forks[route.id] ? { ...forks[route.id], id: route.id } : null;
    if (!f) return forks === null ? <p className="eh-muted">Loading…</p> : <div className="empty"><h3>This copy is not in My experiments</h3><p><button className="btn btn-quiet" onClick={() => go({ v: 'mine' })}>My experiments</button></p></div>;
    const jobs = Object.entries(f.jobs || {}).map(([id, j]) => ({ ...j, id })).sort((a, b) => (b.startedAt || 0) - (a.startedAt || 0));
    const revs = Object.values(f.revisions || {}).sort((a, b) => (b.at || 0) - (a.at || 0));
    const runs = jobs.filter((j) => j.kind === 'run');
    const changes = jobs.filter((j) => j.kind !== 'run');
    const lastRun = runs[0];
    const head = f.head;
    // The listing published from this copy, when there is one (on the marketplace or shared).
    const myListing = f.published ? ownListing(f.id) : null;
    const listedAs = myListing ? accessGroup(myListing, viewerUid) : f.published && f.published.visibility === 'private' ? 'private' : f.published && f.published.visibility === 'people' ? 'by-you' : 'public';
    const send = async () => { const text = draft.trim(); if (!text) return; setDraft(''); setBusy('Asking Mana'); try { await startJob(f.id, f, 'change', text); setBusy(''); go({ v: 'fork', id: f.id, tab: 'changes' }, true); } catch (e) { setBusy(''); setFlash({ kind: 'bad', text: 'Could not start: ' + (e.message || e) }); } };
    const jobRow = (j) => (
      <div key={j.id} className="row">
        <div className="row-main"><div className="row-title">{j.kind === 'run' ? 'Run on ' + (j.machine || 'my machine') + (j.rigConfig ? ' with ' + j.rigConfig : '') : j.kind === 'create' ? 'Build the experiment' : j.request}</div>
          <div className="row-sub">{j.summary || (j.status === 'running' ? 'Mana is working on it.' : '')}{j.url ? <> · Dashboard on the rig: <span className="mono">{j.url.split('#')[0]}</span></> : null}{j.dir ? <> · Folder <span className="mono">{j.dir}</span></> : null}</div>
          <div className="row-sub">Started {ago(j.startedAt)}{j.updatedAt && j.status !== 'running' ? ' · updated ' + ago(j.updatedAt) : ''}</div></div>
        {statusChip(j.status)}
        <button className="btn btn-line btn-small" onClick={() => showChat(j.chatId)}>Open chat</button>
      </div>);
    // [2026-10-16] A private copy shows the same Overview / Methods / Tasks & parameters / Versions as a
    // listing (from the methods snapshot of the version it was forked from), plus Changes, Run and Files.
    const O = f.origin && !f.origin.scratch ? listings.find((x) => x.key === f.origin.publisherUid + '~' + f.origin.exp) || null : null;
    const originBundle = (f.origin && !f.origin.scratch && f.origin.bundle) || '';
    const d = docs['forkdoc:' + (originBundle || 'head:' + ((head && head.id) || f.id))];
    const sameBundle = !!(O && O.bundle && head && O.bundle.id === head.id);
    const changed = !!(head && originBundle && head.id !== originBundle);
    const newer = !!(O && O.bundle && originBundle && O.bundle.id !== originBundle);
    const L = { ...(O || {}), key: f.id, uid: viewerUid, exp: (f.origin && f.origin.exp) || f.id,
      title: f.title, summary: f.summary || (O && O.summary) || '', art: f.art || (O && O.art), artImage: f.artImage || (O && O.artImage),
      tags: f.tags || (O && O.tags) || [], subjects: f.subjects || (O && O.subjects) || [], hardware: f.hardware || (O && O.hardware) || {},
      alhazen: f.alhazen || (O && O.alhazen) || '', original: f.original || (O && O.original) || null, status: f.originStatus || (O && O.status) || null,
      parameterSets: f.parameterSets || (O && O.parameterSets) || [], version: (f.origin && f.origin.version) || '', updatedAt: f.updatedAt,
      methodSummary: (O && !newer && O.methodSummary) || f.methodSummary || '',
      bundle: { ...(sameBundle ? O.bundle : {}), id: originBundle || (head && head.id) || '', path: head ? head.path : '' },
      source: { ...((O && O.source) || { kind: 'fork' }), commit: (d && d.__commit) || ((O && !newer && O.source) || {}).commit || (f.origin && f.origin.commit) || '' },
      publisher: (O && O.publisher) || { name: (f.origin && f.origin.publisherName) || 'You' }, versions: null };
    const nav = (next, extra = {}, replace = false) => go({ v: 'fork', id: f.id, tab: next === 'overview' ? '' : next, ...extra }, replace);
    const onFile = async (file) => {
      nav('files');
      if (!head) return;
      try { await openFile(head.path, file); }
      catch (error) { setFileView({ bundle: head.path, path: file, text: 'Could not load this file: ' + (error.message || error) }); }
    };
    const crumb = <div className="eh-crumb"><button onClick={() => go({ v: 'mine' })}>My experiments</button><span>/</span><span>{f.title}</span></div>;
    const sheetHead = <div className="eh-store-sheet-head"><span>Your copy · <strong>{revs.length} revision{revs.length === 1 ? '' : 's'}</strong></span><small>{ago(f.updatedAt)}</small></div>;
    const actions = <>
      {caps.runOnMachines ? <button className="btn btn-primary" disabled={!head || !!busy} onClick={() => setDialog({ kind: 'run', f, machine: '' })}>Run on my machine</button>
        : <button className="btn btn-primary" disabled={!head || !!busy} onClick={() => downloadRevision(f)}>Download this revision (.zip)</button>}
            {caps.publish ? <button className="btn btn-line" disabled={!head} onClick={() => { const name = (host.viewer && host.viewer.name) || 'Unknown'; setDialog({ kind: 'share', f, access: accessDraft(myListing), error: '', form: { name, title: f.published ? f.published.title : f.title, summary: f.summary || '', category: (asList(f.tags).find((t) => CATEGORIES.some((c) => c[0] === t))) || 'eye-movements', subjects: asList(f.subjects), hardware: f.hardware || {}, version: f.published ? '' : ((f.origin && f.origin.version) ? f.origin.version + '-1' : '0.1.0'), license: 'All rights reserved' } }); }}>{f.published ? 'Update listing' : 'Publish'}</button> : null}
      {myListing ? <button className="btn btn-quiet btn-small" onClick={() => go({ v: 'exp', id: myListing.key })}>View your listing</button> : null}
      {!caps.runOnMachines ? <button className="btn btn-line" onClick={() => nav('run')}>How to run it</button> : null}
      {O ? <button className="btn btn-quiet btn-small" onClick={() => go({ v: 'exp', id: O.key })}>View the original listing</button> : null}
    </>;
    const originLine = f.origin && f.origin.scratch ? 'Started from scratch.' : <>Forked from <b>{f.origin.title}</b>{f.origin.version ? ' v' + f.origin.version : ''}{f.origin.commit ? <> (<span className="mono">{f.origin.commit}</span>)</> : null}.</>;
    const scopeNote = changed || newer || (d && d.__stale) ? <p className="eh-source-note eh-fork-scope">{originLine}{changed ? ' Methods, tasks and parameters describe that version; changes made to your copy are under Changes, and Files shows your current revision.' : ''}{newer ? <> The original is now at {O.version ? 'v' + O.version : 'a newer version'} on the marketplace.</> : null}{d && d.__stale ? ' The methods of that version were not saved with your copy, so its README is shown instead.' : ''}</p> : null;
    const overviewExtra = <section className="eh-reference-section eh-fork-copy">
      <h2>Your copy</h2>
      <p className="eh-muted" style={{ margin: '0 0 14px' }}>{originLine}{f.published ? <> {listedAs === 'public' ? 'On the marketplace' : listedAs === 'private' ? 'Listed for you only' : 'Listed for the people you chose'} as v{f.published.version}.</> : null}</p>
      {changes.filter((j) => j.status === 'running' || j.status === 'waiting').length ? <div className="list" style={{ marginBottom: 20 }}>{changes.filter((j) => j.status === 'running' || j.status === 'waiting').map(jobRow)}</div> : null}
      <div className="eh-two" style={{ marginBottom: 24 }}>
            <div className="panel"><h3>Current revision</h3>
              <dl className="kv"><dt>Saved</dt><dd>{revs[0] ? ago(revs[0].at) : '—'}</dd><dt>Message</dt><dd>{revs[0] ? revs[0].message : 'Not built yet'}</dd>{head ? <><dt>Revision</dt><dd className="mono">{head.id}</dd></> : null}<dt>Alhazen</dt><dd>{f.alhazen || '—'}</dd></dl></div>
            {caps.runOnMachines ? <div className="panel"><h3>Last run</h3>
              {lastRun ? <dl className="kv"><dt>Status</dt><dd>{statusChip(lastRun.status)}</dd><dt>Started</dt><dd>{ago(lastRun.startedAt)}</dd>{lastRun.dir ? <><dt>Folder</dt><dd className="mono">{lastRun.dir}</dd></> : null}{lastRun.closedAt ? <><dt>Dashboard</dt><dd>Closed {ago(lastRun.closedAt)}{typeof lastRun.sessionsRecorded === 'number' ? ' · ' + lastRun.sessionsRecorded + ' session' + (lastRun.sessionsRecorded === 1 ? '' : 's') : ''}</dd></> : null}</dl> : <p>Not run yet. Run copies this revision to your rig and opens the alhazen dashboard there.</p>}</div>
              : <div className="panel"><h3>Running it</h3><p>Experiments run on your rig, not here. Download this revision, unpack it there and run the two commands under Run.</p></div>}
          </div>
      {caps.changeWithMana ? <div className="eh-actions"><button className="btn btn-line" disabled={!head} onClick={() => nav('changes')}>Change with Mana</button><button className="btn btn-line" onClick={() => nav('run')}>Runs</button></div>
        : <div className="eh-actions"><button className="btn btn-line" disabled={!head} onClick={() => nav('changes')}>Changes</button><button className="btn btn-line" onClick={() => nav('run')}>Run</button></div>}
          <div style={{ marginTop: 32, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {f.published ? <button className="btn btn-line btn-small" onClick={() => setDialog({ kind: 'unpublish', f, listedAs })}>{listedAs === 'public' ? 'Take off the marketplace' : 'Remove the listing'}</button> : null}
            <button className="btn btn-quiet btn-small" onClick={() => setDialog({ kind: 'remove', f })}>Remove from library</button>
          </div>
    </section>;
    const versionsView = <section className="eh-reference-versions"><h2>Revisions of your copy</h2>
      <div className="eh-table-wrap"><table className="eh-release-table"><thead><tr><th>Revision</th><th>By</th><th>Saved</th><th>Bundle</th></tr></thead>
        <tbody>{revs.map((r) => <tr key={r.at} className={head && r.bundle === head.id ? 'is-current' : ''}><td>{r.message}{head && r.bundle === head.id ? ' (current)' : ''}</td><td>{r.by === 'mana' ? 'Mana' : 'You'}</td><td>{r.at ? new Date(r.at).toLocaleDateString() : ''}</td><td className="mono">{r.bundle}</td></tr>)}</tbody></table></div>
      <p className="eh-source-note">{originLine}{newer ? <> The original listing has moved on to {O.version ? 'v' + O.version : 'a newer version'}; your copy does not change by itself.</> : O ? ' That is still the original listing’s current version.' : ''}</p>
      <button className="eh-inline-link" onClick={() => nav('details')}>Replication and source details</button></section>;
    const renderExtra = (t) => t === 'changes' ? <>
          {caps.changeWithMana ? <div className="panel" style={{ marginBottom: 20 }}>
            <h3>Change with Mana</h3>
            <p>Describe what should be different. Mana edits your copy, runs its tests and saves a new revision. Nothing reaches GitHub or the marketplace.</p>
            <div className="field"><textarea aria-label="What should change" placeholder="For example: add a block of 40 trials with the cylinder at half speed, and make the pilot file use it." value={draft} onChange={(e) => setDraft(e.target.value)} onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') send(); }} /></div>
            <div className="eh-actions" style={{ marginTop: 12, justifyContent: 'flex-end' }}><button className="btn btn-primary" disabled={!draft.trim() || !!busy || !head} onClick={send}>Ask</button></div>
          </div> : <div className="panel eh-upload" style={{ marginBottom: 20 }}>
            <h3>Change this copy</h3>
            <p>Download this revision, change it on your computer, then upload the changed folder as a .zip. The upload becomes a new revision; earlier revisions stay. Nothing reaches GitHub or the marketplace.</p>
            <div className="eh-actions" style={{ margin: '12px 0 18px' }}><button className="btn btn-line" disabled={!head || !!busy} onClick={() => downloadRevision(f)}>Download this revision (.zip)</button></div>
            <div className="field"><label htmlFor="eh-up-file">Changed copy (.zip)</label><input id="eh-up-file" type="file" accept=".zip,application/zip" onChange={(e) => setUpload({ ...upload, file: (e.target.files && e.target.files[0]) || null })} /></div>
            <div className="field"><label htmlFor="eh-up-msg">What changed</label><input id="eh-up-msg" value={upload.message} placeholder="For example: half-speed cylinder block in the pilot file" onChange={(e) => setUpload({ ...upload, message: e.target.value })} /></div>
            <div className="eh-actions" style={{ marginTop: 12, justifyContent: 'flex-end' }}><button className="btn btn-primary" disabled={!upload.file || !upload.message.trim() || !!busy} onClick={() => uploadRevision(f)}>Upload a changed copy</button></div>
          </div>}
          {changes.length ? <><h2 style={{ fontSize: 18, fontWeight: 800, margin: '0 0 12px' }}>Requests</h2><div className="list" style={{ marginBottom: 28 }}>{changes.map(jobRow)}</div></> : null}
          <h2 style={{ fontSize: 18, fontWeight: 800, margin: '0 0 12px' }}>Revisions</h2>
          <div className="list">{revs.map((r) => <div key={r.at} className="row"><div className="row-main"><div className="row-title" style={{ fontWeight: 600 }}>{r.message}</div><div className="row-sub">{ago(r.at)} · {r.by === 'mana' ? 'Mana' : 'You'} · <span className="mono">{r.bundle}</span></div>{r.stat ? <pre className="mono eh-muted" style={{ margin: '8px 0 0', whiteSpace: 'pre-wrap', fontSize: 12 }}>{r.stat.split('\n').slice(-6).join('\n')}</pre> : null}</div></div>)}</div>
    </> : t === 'run' ? <>
          {caps.runOnMachines ? <>
          <div style={{ marginBottom: 20 }}>{machinesPanel(true)}</div>
          <div className="panel" style={{ marginBottom: 20 }}><h3>What Run does</h3>
            <p>Mana copies this revision to <span className="cmd">~/alhazen-experiments/{slug(f.title)}</span> on the rig, builds its Python environment with uv from the experiment's lock file (PsychoPy included), and starts the alhazen dashboard there, with the rig config you choose, where you pick the subject and task and run sessions. Your terminal asks you before each step that changes the machine. Data stays on the rig.</p>
            <div className="eh-actions" style={{ marginTop: 14 }}><button className="btn btn-primary" disabled={!head || !!busy} onClick={() => setDialog({ kind: 'run', f, machine: '' })}>Run on my machine</button></div></div>
          {runs.length ? <div className="list">{runs.map(jobRow)}</div> : null}
          </> : <>{caps.machineTokens ? <div className="panel" style={{ marginBottom: 20 }}><h3>From the rig's dashboard</h3>
            <p>Connect the rig's alhazen dashboard to your library once: make a token for it under <button className="eh-inline-link" onClick={() => go({ v: 'machines' })}>Machines</button> and run the command it shows on the rig. The dashboard then lists {f.title} with your other copies, downloads it when you run it, builds its environment, and tells this platform when it closes.</p>
            {caps.rigConfigs ? <RigConfigsBrief host={host} /> : null}</div> : null}
          <div className="panel eh-run-steps"><h3>{caps.machineTokens ? 'Or by hand' : 'Run it on a rig'}</h3>
            <p>The platform does not run experiments. On the rig (with <a href="https://docs.astral.sh/uv/" target="_blank" rel="noreferrer">uv</a> installed):</p>
            <ol className="eh-steps-list">
              <li><button className="eh-inline-link" disabled={!head} onClick={() => downloadRevision(f)}>Download this revision (.zip)</button> and unpack it. It unpacks into a folder named <span className="cmd">{f.id}</span>.</li>
              <li>In a terminal, in that folder, build the environment from the experiment's lock file (PsychoPy included):<pre className="eh-cmd">{'uv sync --locked --extra psychopy'}</pre></li>
              <li>Start the alhazen dashboard, where you choose the rig, subject and task and run sessions:<pre className="eh-cmd">{'uv run alhazen dashboard --project .'}</pre></li>
            </ol>
            <p>Session data stays on the rig. To change the experiment, edit the unpacked folder and upload it under Changes.</p>
            <div className="eh-actions" style={{ marginTop: 12 }}><button className="btn btn-quiet btn-small" onClick={async () => { const r = await host.copyText('cd ' + f.id + '\nuv sync --locked --extra psychopy\nuv run alhazen dashboard --project .'); setFlash({ kind: r === 'failed' ? 'bad' : 'ok', text: r === 'failed' ? 'Could not copy.' : 'Copied the commands.' }); }}>Copy the commands</button></div>
          </div></>}
    </> : null;
    return <EHDocumentationBoundary key={f.id + ':' + (d ? (d.metadata && d.metadata.updatedAt) || d.__loadError || 'ready' : 'loading')}><EHExperimentPage L={L} d={d} route={route} art={art} actions={actions} renderTabs={tabs} renderFiles={(p) => (head ? filesTab(p) : <p className="eh-muted">No files yet: Mana is still building this experiment.</p>)} renderReplication={replication} go={go} onFile={onFile}
      nav={nav} crumb={crumb} sheetHead={sheetHead} extraTabs={[['changes', 'Changes'], ['run', 'Run'], ['files', 'Files']]} renderExtra={renderExtra} overviewExtra={overviewExtra} scopeNote={scopeNote} versionsView={versionsView} /></EHDocumentationBoundary>;
  }

  // The device label of the terminal window a run goes to (the chosen one, else the newest), which
  // names the dashboard machine whose rig configs the Run dialog offers.
  function runDeviceLabel(sessionId) {
    const list = machines.sessions.slice().sort((x, y) => (y.startedAt || 0) - (x.startedAt || 0));
    const s = sessionId ? machines.sessions.find((x) => x.id === sessionId) : list[0];
    if (!s) return '';
    const dv = machines.devices.find((x) => x.id === s.device) || {};
    return dv.label || dv.host || '';
  }

  function Dialog() {
    const d = dialog; const close = () => setDialog(null);
    let body = null;
    if (d.kind === 'forkai') body = <>
      <h2>Fork with AI</h2><p>{d.L.title} is copied to My experiments, then Mana changes your copy as you describe.</p>
      <div className="field"><label htmlFor="eh-ai">What should change?</label><textarea id="eh-ai" autoFocus value={d.text} onChange={(e) => setDialog({ ...d, text: e.target.value })} /></div>
      <div className="eh-actions"><button className="btn btn-line" onClick={close}>Cancel</button><button className="btn btn-primary" disabled={!d.text.trim() || !!busy} onClick={async () => { const text = d.text.trim(); close(); await forkListing(d.L, async (forkId, rec) => { await startJob(forkId, rec, 'change', text); go({ v: 'fork', id: forkId, tab: 'changes' }, true); }); }}>Fork and ask</button></div></>;
    else if (d.kind === 'new') body = <>
      <h2>New experiment</h2><p>Mana scaffolds an alhazen experiment in My experiments and builds what you describe.</p>
      <div className="field"><label htmlFor="eh-nt">Name</label><input id="eh-nt" autoFocus value={d.title} onChange={(e) => setDialog({ ...d, title: e.target.value })} /></div>
      <div className="field"><label htmlFor="eh-nd">What should it do?</label><textarea id="eh-nd" value={d.text} onChange={(e) => setDialog({ ...d, text: e.target.value })} placeholder="Task, stimulus, trial structure, what is measured, human or monkey, hardware." /></div>
      <div className="eh-actions"><button className="btn btn-line" onClick={close}>Cancel</button><button className="btn btn-primary" disabled={!d.title.trim() || !d.text.trim()} onClick={async () => { const t = d.title.trim(), x = d.text.trim(); close(); try { await newExperiment(t, x); } catch (e) { setFlash({ kind: 'bad', text: 'Could not start: ' + (e.message || e) }); } }}>Create</button></div></>;
    else if (d.kind === 'run') body = <>
      <h2>Run on my machine</h2><p>Mana copies {d.f.title} to the rig, builds its environment and opens the alhazen dashboard. Approve each step in the terminal.</p>
      {machines.sessions.length > 1 ? <div className="field"><label htmlFor="eh-m">Terminal window</label><select id="eh-m" value={d.machine} onChange={(e) => setDialog({ ...d, machine: e.target.value })}><option value="">Newest window</option>{machines.sessions.map((s) => { const dv = machines.devices.find((x) => x.id === s.device) || {}; return <option key={s.id} value={s.id}>{(dv.label || s.device) + ' · ' + (s.tty || s.id)}</option>; })}</select></div> : null}
      {caps.rigConfigs ? <RigConfigPicker host={host} deviceLabel={runDeviceLabel(d.machine)} value={d.rigConfig || ''} onChange={(v) => setDialog({ ...d, rigConfig: v })} /> : null}
      <div style={{ marginTop: 14 }}>{machinesPanel(true)}</div>
      <div className="eh-actions"><button className="btn btn-line" onClick={close}>Cancel</button><button className="btn btn-primary" disabled={!!busy} onClick={async () => { close(); try { await startJob(d.f.id, d.f, 'run', '', d.machine, { rigConfig: d.rigConfig ? d.rigConfig.slice(d.rigConfig.indexOf('|') + 1) : '' }); go({ v: 'fork', id: d.f.id, tab: 'run' }, true); } catch (e) { setFlash({ kind: 'bad', text: 'Could not start: ' + (e.message || e) }); } }}>Run</button></div></>;
    else if (d.kind === 'share') { const fm = d.form; const set = (k, v) => setDialog({ ...d, form: { ...fm, [k]: v } });
      const togg = (k) => set('subjects', fm.subjects.includes(k) ? fm.subjects.filter((x) => x !== k) : [...fm.subjects, k]);
      const a = d.access;
      const publish = () => {
        let access;
        try { access = accessDraftValue(a, viewerUid); } catch (e) { setDialog({ ...d, error: e.message }); return; }
        publishFork(d.f, { ...fm, title: fm.title.trim(), version: fm.version.trim(), access, notify: !!(caps.notifyPeople && a.notify) });
      };
      body = <>
        <h2>{d.f.published ? 'Update listing' : 'Publish ' + d.f.title}</h2><p>{a.visibility === 'public' ? 'Everyone who can open this platform will see the listing and can fork this revision.' : a.visibility === 'people' ? 'Only you and the people you add will see the listing and can fork this revision.' : 'Only you will see the listing. You can share it later.'} It carries your name.</p>
        {accessFields(a, viewerUid)}
        <dl className="kv"><dt>Appears as</dt><dd>{fm.name}</dd></dl>
        <div className="field"><label htmlFor="eh-st">Title</label><input id="eh-st" value={fm.title} onChange={(e) => set('title', e.target.value)} /></div>
        <div className="field"><label htmlFor="eh-ss">Summary</label><textarea id="eh-ss" style={{ minHeight: 90 }} value={fm.summary} onChange={(e) => set('summary', e.target.value)} /></div>
        <div className="eh-two"><div className="field"><label htmlFor="eh-sc">Category</label><select id="eh-sc" value={fm.category} onChange={(e) => set('category', e.target.value)}>{CATEGORIES.map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select></div>
          <div className="field"><label htmlFor="eh-sv">Version</label><input id="eh-sv" value={fm.version} onChange={(e) => set('version', e.target.value)} /></div></div>
        <div className="eh-two"><div className="field"><label>Subjects</label>{WHO.map(([k, l]) => <label key={k} className="check"><input type="checkbox" checked={fm.subjects.includes(k)} onChange={() => togg(k)} />{l}</label>)}</div>
          <div className="field"><label>Hardware</label>{[['eye_tracker', 'Eye tracker'], ['reward', 'Reward line']].map(([k, l]) => <label key={k} className="check"><input type="checkbox" checked={!!fm.hardware[k]} onChange={() => set('hardware', { ...fm.hardware, [k]: !fm.hardware[k] })} />{l}</label>)}</div></div>
        <div className="field"><label htmlFor="eh-sl">Licence</label><input id="eh-sl" value={fm.license} onChange={(e) => set('license', e.target.value)} /></div>
        {d.error ? <p className="eh-access-error" role="alert">{d.error}</p> : null}
        <div className="eh-actions"><button className="btn btn-line" onClick={close}>Cancel</button><button className="btn btn-primary" disabled={!fm.title.trim() || !fm.version.trim() || !!busy} onClick={publish}>{d.f.published ? 'Update' : 'Publish'}</button></div></>; }
    else if (d.kind === 'unpublish') body = d.listedAs && d.listedAs !== 'public' ? <>
      <h2>Remove the listing?</h2><p>Nobody you shared it with can open it any more. Copies they already made stay theirs, and your copy is unchanged.</p>
      <div className="eh-actions"><button className="btn btn-line" onClick={close}>Cancel</button><button className="btn btn-primary" onClick={() => unpublish(d.f, d.listedAs)}>Remove the listing</button></div></> : <>
      <h2>Take off the marketplace?</h2><p>The listing disappears from Browse. Copies others already forked stay theirs, and your copy is unchanged.</p>
      <div className="eh-actions"><button className="btn btn-line" onClick={close}>Cancel</button><button className="btn btn-primary" onClick={() => unpublish(d.f, 'public')}>Take off</button></div></>;
    else if (d.kind === 'access') { const a = d.access; const before = accessOf(d.L).visibility;
      body = <>
      <h2>Who can see {d.L.title}</h2>
      <p>{a.visibility === before ? 'Copies people already made stay theirs whatever you choose.' : a.visibility === 'public' ? 'It will appear in Browse for everyone. Copies people already made stay theirs.' : 'People who can no longer see it keep the copies they already made.'}</p>
      {accessFields(a, d.L.uid)}
      {d.error ? <p className="eh-access-error" role="alert">{d.error}</p> : null}
      <div className="eh-actions">
        {a.visibility === 'people' && host.shareLink && before === 'people' ? <button className="btn btn-quiet eh-access-copy" onClick={async () => { const r = await host.copyText(host.shareLink(d.L)); setFlash({ kind: r === 'failed' ? 'bad' : 'ok', text: r === 'failed' ? 'Could not copy.' : 'Copied the link to ' + d.L.title + '.' }); }}>Copy link</button> : null}
        <button className="btn btn-line" onClick={close}>Cancel</button><button className="btn btn-primary" disabled={!!busy} onClick={() => saveAccess(d)}>Save</button></div></>; }
    else if (d.kind === 'remove') { const plan = removalPlan(d.f, forkList);
      body = <>
      <h2>Remove {d.f.title} from your library?</h2>
      <div className="eh-remove-plan">
        <h3>Removed</h3>
        <ul>{plan.removed.map((x) => <li key={x}>{x}</li>)}</ul>
        <h3>Not removed</h3>
        <ul>{plan.kept.map((x) => <li key={x}>{x}</li>)}</ul>
      </div>
      {plan.working ? <p className="eh-remove-warn">Mana is still working on this copy. Removing it does not stop that request.</p> : null}
      <div className="eh-actions"><button className="btn btn-line" onClick={close}>Cancel</button><button className="btn btn-primary" disabled={!!busy} onClick={() => removeFork(d.f)}>Remove from library</button></div></>; }
    return <div className="eh-scrim" onMouseDown={(e) => { if (e.target === e.currentTarget) close(); }}><div className="eh-dialog" role="dialog" aria-modal="true">{body}</div></div>;
  }

  // ------------------------------------------------------------ Machines (standalone hosts)
  // A machine token lets one rig's alhazen dashboard read this person's library and say when it
  // closes (design.md section 8a). Shown once when made; revoking it cuts that rig off.
  function MachineTokens() {
    const data = machineTokens;
    const origin = host.platformOrigin || '';
    const command = madeToken ? 'alhazen library connect ' + origin + ' --token ' + madeToken.token + (madeToken.machine.name ? ' --machine "' + madeToken.machine.name.replace(/"/g, '') + '"' : '') : '';
    const make = async (e) => {
      e.preventDefault();
      const name = machineName.trim(); if (!name) return;
      setBusy('Making a token');
      try { const res = await host.createMachineToken(name); setMadeToken(res); setMachineName(''); }
      catch (err) { setFlash({ kind: 'bad', text: 'Could not make a token: ' + (err.message || err) }); }
      finally { setBusy(''); }
    };
    const revoke = async (m) => {
      setBusy('Revoking');
      try { await host.revokeMachineToken(m.id); setRevoking(''); setFlash({ kind: 'ok', text: m.name + ' can no longer read your library.' }); }
      catch (err) { setFlash({ kind: 'bad', text: 'Could not revoke: ' + (err.message || err) }); }
      finally { setBusy(''); }
    };
    const copy = async (text, what) => { const r = await host.copyText(text); setFlash({ kind: r === 'failed' ? 'bad' : 'ok', text: r === 'failed' ? 'Could not copy.' : 'Copied the ' + what + '.' }); };
    const sessionsLine = (ev) => { const n = (ev.sessions || []).length; return n ? n + ' session' + (n === 1 ? '' : 's') : 'no sessions'; };
    return (
      <>
        <div className="eh-sechead" style={{ marginBottom: 6 }}><h1 style={{ fontSize: 34, fontWeight: 800 }}>Machines</h1></div>
        <p className="eh-muted" style={{ margin: '0 0 24px', maxWidth: 720 }}>A machine token lets the alhazen dashboard on one rig list your library, download a copy when you run it, and tell this platform when it closes. Make one for each machine; revoke it when the machine is no longer yours to use.</p>
        <form className="panel eh-token-form" style={{ marginBottom: 20 }} onSubmit={make}>
          <h3>Connect a machine</h3>
          <div className="eh-token-row">
            <div className="field" style={{ flex: 1, minWidth: 220 }}><label htmlFor="eh-machine-name">Machine name</label><input id="eh-machine-name" value={machineName} maxLength={60} placeholder="Booth 2 rig" onChange={(e) => setMachineName(e.target.value)} /></div>
            <button className="btn btn-primary" type="submit" disabled={!machineName.trim() || !!busy}>Make a token</button>
          </div>
        </form>
        {madeToken ? <div className="panel eh-token-made" style={{ marginBottom: 20 }} role="status">
          <h3>Token for {madeToken.machine.name}</h3>
          <p>Copy it now: it is not shown again. On that machine, where alhazen is installed, run:</p>
          <pre className="eh-cmd">{command}</pre>
          <div className="eh-actions" style={{ marginTop: 12 }}>
            <button className="btn btn-line btn-small" onClick={() => copy(command, 'command')}>Copy the command</button>
            <button className="btn btn-quiet btn-small" onClick={() => copy(madeToken.token, 'token')}>Copy the token only</button>
            <button className="btn btn-quiet btn-small" onClick={() => setMadeToken(null)}>Done</button>
          </div>
          <p className="eh-muted" style={{ marginTop: 12 }}>Or paste the address and the token into the Library section of the dashboard's Experiments page.</p>
        </div> : null}
        <h2 style={{ fontSize: 18, fontWeight: 800, margin: '8px 0 12px' }}>Connected machines</h2>
        {data === undefined ? <p className="eh-muted">Loading…</p> : data === null ? <p className="eh-muted">The machines could not be read.</p>
          : data.machines.length ? <div className="list" style={{ marginBottom: 28 }}>{data.machines.map((m) => (
            <div key={m.id} className="row eh-machine">
              <div className="row-main"><div className="row-title">{m.name}</div>
                <div className="row-sub">{['made ' + ago(m.created), m.lastUsed ? 'last used ' + ago(m.lastUsed) : 'not used yet'].join(' · ')}</div></div>
              {revoking === m.id ? <div className="eh-actions"><span className="eh-muted">Revoke this token?</span><button className="btn btn-line btn-small" onClick={() => setRevoking('')}>Keep</button><button className="btn btn-primary btn-small eh-revoke" disabled={!!busy} onClick={() => revoke(m)}>Revoke</button></div>
                : <button className="btn btn-quiet btn-small" onClick={() => setRevoking(m.id)}>Revoke</button>}
            </div>))}</div>
          : <p className="eh-muted" style={{ marginBottom: 28 }}>No machine is connected yet.</p>}
        {caps.rigConfigs ? <RigConfigsSection host={host} go={go} onFlash={rigFlash} /> : null}
        <h2 style={{ fontSize: 18, fontWeight: 800, margin: '0 0 12px' }}>Recent activity</h2>
        {data && data.events && data.events.length ? <div className="list">{data.events.map((ev) => (
          <div key={ev.id} className="row">
            <div className="row-main"><div className="row-title" style={{ fontWeight: 650 }}>{ev.machine || 'A machine'} closed its dashboard</div>
              <div className="row-sub">{[ago(ev.at), sessionsLine(ev), ev.alhazen ? 'alhazen ' + ev.alhazen : ''].filter(Boolean).join(' · ')}</div>
              {(ev.sessions || []).length ? <ul className="eh-event-sessions">{ev.sessions.map((x, i) => <li key={i}>{[x.experiment, x.task, x.status, Object.entries(x.outcomes || {}).map(([k, n]) => k + ' ' + n).join(', ')].filter(Boolean).join(' · ')}</li>)}</ul> : null}
            </div>
          </div>))}</div> : <p className="eh-muted">Nothing yet. A dashboard reports here when it closes.</p>}
      </>
    );
  }

  const machinesView = () => rigEditor() || (caps.machines ? Machines() : caps.machineTokens ? MachineTokens() : Home());
  const view = route.v === 'browse' ? Browse() : route.v === 'exp' ? Exp() : route.v === 'mine' ? Mine() : route.v === 'machines' ? machinesView() : route.v === 'fork' ? Fork() : Home();
  const navCur = route.v === 'exp' ? 'browse' : route.v === 'fork' ? 'mine' : route.v;
  return (
    <div className="eh w-full" data-eh-view={route.v} style={{ ...vars, colorScheme: theme === 'dark' ? 'dark' : 'light' }} onKeyDown={(e) => { if (e.key === 'Escape' && dialog) setDialog(null); }}>
      {host.fontStylesheet ? <link rel="stylesheet" href={host.fontStylesheet} /> : null}
      {host.inlineStyles === false ? null : <style>{CSS + GUIDE_CSS + (host.extraCss || '')}</style>}
      <header className="eh-top"><div className="eh-wrap">
        <button className="eh-brand" onClick={() => go({ v: 'home' })} aria-label="Alhazen Experiment Platform, home">
          <span dangerouslySetInnerHTML={{ __html: assets.penrose || '' }} style={{ display: 'flex' }} />
          <span className="eh-word">Alhazen</span><span className="eh-sub">Experiment Platform</span>
        </button>
        <nav className="eh-nav" aria-label="Hub">
          {[['home', 'Home'], ['browse', 'Browse'], ['mine', 'My experiments'], ['machines', caps.machines ? 'My machines' : 'Machines']].filter(([k]) => k !== 'machines' || caps.machines || (caps.machineTokens && viewerUid)).map(([k, label]) => <button key={k} aria-current={navCur === k ? 'page' : undefined} onClick={() => go({ v: k })}>{label}{k === 'mine' && forkList.length ? <span className="eh-count">{forkList.length}</span> : null}{k === 'machines' && machines.sessions.length ? <span className="eh-count" title="Terminal windows open">{machines.sessions.length}</span> : null}</button>)}
        </nav>
        {caps.themeToggle || caps.signIn ? <div className="eh-host-tools">
          {caps.signIn && host.viewer ? <span className="eh-viewer">{host.viewer.name}</span> : null}
          {caps.signIn && host.viewer ? <button className="btn btn-quiet btn-small" onClick={() => host.signOut()}>Sign out</button> : null}
          {caps.signIn && !host.viewer ? <button className="btn btn-quiet btn-small" onClick={() => host.signIn()}>Sign in</button> : null}
          {caps.themeToggle ? <button className="btn btn-quiet btn-small eh-theme-toggle" aria-label={theme === 'dark' ? 'Use the light theme' : 'Use the dark theme'} onClick={toggleTheme}>{theme === 'dark' ? 'Light' : 'Dark'}</button> : null}
        </div> : null}
      </div></header>
      <main className="eh-main"><div className="eh-wrap">{Flash()}{view}</div></main>
      <footer className="eh-foot"><div className="eh-wrap"><span style={{ fontWeight: 800, color: 'var(--ink)' }}>Alhazen</span><span>{caps.runOnMachines ? 'Experiments run on your own machine through Mana Terminal; session data stays there.' : 'Experiments run on your own rig; session data stays there.'}</span></div></footer>
      {dialog ? Dialog() : null}
    </div>
  );
}
