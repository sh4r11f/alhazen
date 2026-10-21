// Standalone host adapter: the platform served by `alhazen platform local` (one user, a per-launch
// token from the URL fragment) or `alhazen platform serve` (accounts, a session cookie and a CSRF
// header). It speaks the JSON API of design.md Appendix A and nothing else: no Mana, no Firebase,
// nothing fetched from another origin. Data is fetched on mount, after this host's own writes, when
// the window regains focus and every 30 s; an unchanged response does not re-render.
const POLL_MS = 30000;
const THEME_KEY = 'alhazen-platform-theme';

class PlatformRequestError extends Error {
  constructor(message, status, kind) { super(message); this.name = 'PlatformRequestError'; this.status = status; this.kind = kind; }
}

// Clipboard for the standalone page. The reader components call `copyText` directly (Mana injects
// its own); both answer 'failed' when nothing was copied.
async function copyText(text) {
  const value = String(text == null ? '' : text);
  try {
    if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(value); return 'copied'; }
  } catch (e) {
    // The async clipboard can be refused (permissions, focus); the selection fallback below still works.
    console.warn('Clipboard API refused the copy; falling back to a selection copy.', e);
  }
  const area = document.createElement('textarea');
  area.value = value; area.setAttribute('readonly', ''); area.style.position = 'fixed'; area.style.opacity = '0';
  document.body.appendChild(area); area.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } finally { document.body.removeChild(area); }
  return ok ? 'copied' : 'failed';
}

function readFragmentToken(hash) {
  const params = new URLSearchParams(String(hash || '').replace(/^#/, ''));
  return params.get('token') || '';
}

function platformBase() {
  return new URL('.', window.location.href);
}

// The transport: every request goes through here, so authentication headers, error decoding and
// the signed-out signal live in one place.
function createTransport({ kind, token, getCsrf, onSignedOut }) {
  const base = platformBase();
  const url = (path) => new URL(path.replace(/^\//, ''), base).toString();
  async function request(method, path, { json, body, contentType, accept = 'json' } = {}) {
    const headers = {};
    if (kind === 'local' && token) headers['X-Alhazen-Token'] = token;
    if (method !== 'GET' && kind === 'server' && getCsrf()) headers['X-Alhazen-CSRF'] = getCsrf();
    // The server refuses a sign-in (and any change) that does not come from its own page;
    // a cross-site form cannot set this header without a preflight the server never grants.
    if (method !== 'GET' && kind === 'server') headers['X-Alhazen-Platform'] = '1';
    let payload = body;
    if (json !== undefined) { headers['Content-Type'] = 'application/json'; payload = JSON.stringify(json); }
    else if (contentType) headers['Content-Type'] = contentType;
    const res = await fetch(url(path), { method, headers, body: payload, credentials: 'same-origin', cache: 'no-store' });
    if (!res.ok) {
      let message = 'The server answered ' + res.status + '.'; let errKind = '';
      const text = await res.text();
      try { const parsed = JSON.parse(text); if (parsed && parsed.error) { message = parsed.error.message || message; errKind = parsed.error.kind || ''; } }
      catch (e) { if (text && text.length < 300) message = text; }
      if (res.status === 401 && onSignedOut) onSignedOut();
      throw new PlatformRequestError(message, res.status, errKind);
    }
    if (accept === 'blob') return res.blob();
    if (accept === 'text') return res.text();
    return res.json();
  }
  return { request, url };
}

// A subscription is a fetch the host repeats; `refresh()` re-runs every live one and resolves when
// all have answered, so a view opened after a write already sees it.
const UNCHANGED = Symbol('unchanged');

function createPoller() {
  const live = new Set();
  let timer = null;
  const onFocus = () => { refresh(); };
  function start() {
    if (timer) return;
    timer = window.setInterval(() => { refresh(); }, POLL_MS);
    window.addEventListener('focus', onFocus);
  }
  function stop() {
    if (!timer) return;
    window.clearInterval(timer); timer = null;
    window.removeEventListener('focus', onFocus);
  }
  function subscribe(load, cb, onError) {
    const entry = { last: undefined, active: true };
    entry.run = async () => {
      try {
        const value = await load();
        if (value === UNCHANGED) return;
        const text = JSON.stringify(value === undefined ? null : value);
        if (!entry.active || text === entry.last) return;
        entry.last = text; await cb(value);
      } catch (e) {
        if (!entry.active) return;
        entry.last = undefined;
        if (onError) onError(e); else console.error('Alhazen Experiment Platform: could not refresh', e);
      }
    };
    live.add(entry); start(); entry.run();
    return () => { entry.active = false; live.delete(entry); if (!live.size) stop(); };
  }
  function refresh() { return Promise.all([...live].map((entry) => entry.run())); }
  return { subscribe, refresh };
}

function useStandaloneTheme() {
  const media = typeof window.matchMedia === 'function' ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  const stored = () => { try { const v = window.localStorage.getItem(THEME_KEY); return v === 'dark' || v === 'light' ? v : ''; } catch (e) { console.warn('The saved theme choice could not be read; following the system theme.', e); return ''; } };
  const [choice, setChoice] = React.useState(stored);
  const [system, setSystem] = React.useState(media && media.matches ? 'dark' : 'light');
  React.useEffect(() => {
    if (!media) return;
    const on = (e) => setSystem(e.matches ? 'dark' : 'light');
    media.addEventListener('change', on);
    return () => media.removeEventListener('change', on);
  }, []);
  const theme = choice || system;
  React.useEffect(() => {
    const T = theme === 'dark' ? DARK : LIGHT;
    document.documentElement.style.colorScheme = theme;
    document.body.style.background = T.paper;
  }, [theme]);
  const toggleTheme = () => {
    const next = theme === 'dark' ? 'light' : 'dark';
    setChoice(next);
    try { window.localStorage.setItem(THEME_KEY, next); } catch (e) { console.warn('The theme choice could not be saved for the next visit.', e); }
  };
  return { theme, toggleTheme };
}

const enc = encodeURIComponent;
let httpHostSerial = 0;

function createHttpHost({ session, transport, onSignedOut, onSignIn, useThemeHook }) {
  const poller = createPoller();
  const { request } = transport;
  const unavailable = (what) => () => { throw new Error(what + ' is not available on this host.'); };
  // A visitor of a public catalogue: a host with accounts and nobody signed in.
  const visitor = !session.viewer && !!(session.capabilities || {}).signIn;
  const host = {
    kind: session.kind,
    key: 'http:' + (++httpHostSerial),
    viewer: session.viewer || null,
    // A visitor of a public catalogue reads only: every change needs an account (the server
    // refuses them too; these flags keep the page from offering them).
    capabilities: { ...(session.capabilities || {}), ...(visitor ? { fork: false, publish: false, uploadRevision: false, downloadRevision: false, machineTokens: false } : {}), changeWithMana: false, runOnMachines: false, newWithMana: false, machines: false, themeToggle: true,
      // Machine rig configs (machine-rig-configs.md §4, owner routes): the signed-in person's machines.
      rigConfigs: !visitor && !!(session.capabilities || {}).rigConfigs },
    art: { url: transport.url('api/art/'), encodePath: (p) => String(p).split('/').map(enc).join('/'), error: null },
    fontStylesheet: null,
    inlineStyles: false,
    extraCss: '',
    links: {},
    useTheme: useThemeHook,
    copyText,

    subscribeListings: (cb, onError) => poller.subscribe(async () => (await request('GET', 'api/catalogue')).published || {}, cb, onError),
    // The hero drawing carries a style attribute; under the server's CSP (no inline styles) it would be
    // refused, so it is dropped here and platform.css sizes the drawing instead (.eh-art svg).
    subscribeAssets: (cb, onError) => poller.subscribe(async () => {
      const a = await request('GET', 'api/assets');
      return a && typeof a.hero === 'string' ? { ...a, hero: a.hero.replace(/^(<svg\b[^>]*?)\s+style="[^"]*"/, '$1') } : a;
    }, cb, onError),
    subscribeForks: (cb, onError) => (!visitor ? poller.subscribe(async () => (await request('GET', 'api/forks')).forks || {}, cb, onError) : (cb({}), () => undefined)),
    subscribeMachines: () => undefined,
    // Machine tokens (design.md section 8a): the signed-in person's rigs and what they reported.
    subscribeMachineTokens: (cb, onError) => (!visitor && (session.capabilities || {}).machineTokens
      ? poller.subscribe(() => request('GET', 'api/machines'), cb, onError) : (cb(null), () => undefined)),
    async createMachineToken(name) { const res = await request('POST', 'api/machines', { json: { name } }); await poller.refresh(); return res; },
    async revokeMachineToken(id) { await request('DELETE', 'api/machines/' + enc(id)); await poller.refresh(); },
    platformOrigin: platformBase().toString().replace(/\/$/, ''),

    // Machine rig configs: the records the person's dashboards sync (GET /api/rigs, /api/rigs/<mid>)
    // and the owner's changes, which the server applies with rigsync's rules. Each change re-runs
    // the live subscriptions before it resolves, so the page shows the new revision at once.
    listRigMachines: async () => (await request('GET', 'api/rigs')).machines || [],
    getRigMachine: async (mid) => (await request('GET', 'api/rigs/' + enc(mid))).record || null,
    subscribeRigMachines: (cb, onError) => poller.subscribe(async () => ({ machines: (await request('GET', 'api/rigs')).machines || [] }), cb, onError),
    subscribeRigMachine: (mid, cb, onError) => poller.subscribe(async () => {
      try { return (await request('GET', 'api/rigs/' + enc(mid))).record || null; }
      catch (e) { if (e.status === 404) return null; throw e; }
    }, cb, onError),
    async putRigConfig(mid, cid, { name, text, base, basedOn }) {
      const res = await request('PUT', 'api/rigs/' + enc(mid) + '/configs/' + enc(cid), { json: { name, text, base, basedOn: basedOn || null } });
      await poller.refresh(); return res;
    },
    async deleteRigConfig(mid, cid, base) { const res = await request('DELETE', 'api/rigs/' + enc(mid) + '/configs/' + enc(cid) + '?base=' + enc(base)); await poller.refresh(); return res; },
    async setRigDefault(mid, cid, base) { const res = await request('PUT', 'api/rigs/' + enc(mid) + '/default', { json: { config: cid, base } }); await poller.refresh(); return res; },
    async resolveRigConfig(mid, cid, keep) { const res = await request('POST', 'api/rigs/' + enc(mid) + '/configs/' + enc(cid) + '/resolve', { json: { keep } }); await poller.refresh(); return res; },
    async forgetRigMachine(mid) { await request('DELETE', 'api/rigs/' + enc(mid)); await poller.refresh(); },
    // Bundled with the standalone page (entry_standalone.jsx); nothing is fetched from elsewhere.
    loadYaml: async () => YAML,
    subscribeDocs: (listing, cb, onError) => poller.subscribe(() => request('GET', 'api/catalogue/' + enc(listing.uid) + '/' + enc(listing.exp) + '/docs'), cb, onError),
    subscribeForkDocs: (fork, cb, onError) => poller.subscribe(async () => {
      let res;
      try { res = await request('GET', 'api/forks/' + enc(fork.id) + '/docs'); }
      catch (e) { if (e.status === 404) return UNCHANGED; throw e; } // the copy was just removed; its page is closing
      return res && res.docs ? { docs: res.docs } : { stale: true };
    }, cb, onError),
    async bundleMeta(handle) {
      try { return await request('GET', 'api/bundles/' + enc(handle) + '/meta'); }
      catch (e) { if (e.status === 404) return null; throw e; }
    },
    bundleChunk: (handle, i) => request('GET', 'api/bundles/' + enc(handle) + '/chunks/' + i),
    archiveUrl: (handle) => transport.url('api/bundles/' + enc(handle) + '/archive'),
    async downloadArchive(handle, filename) {
      const blob = await request('GET', 'api/bundles/' + enc(handle) + '/archive', { accept: 'blob' });
      const href = URL.createObjectURL(blob);
      const a = document.createElement('a'); a.href = href; a.download = filename; document.body.appendChild(a); a.click(); a.remove();
      window.setTimeout(() => URL.revokeObjectURL(href), 60000);
    },

    async fork(listing, progress) {
      progress('Copying files');
      const res = await request('POST', 'api/forks', { json: { publisher: listing.uid, exp: listing.exp } });
      await poller.refresh();
      return { forkId: res.id, record: res.fork };
    },
    async publish(fork, form) {
      const { title, summary, category, subjects, hardware, version, license } = form;
      await request('POST', 'api/forks/' + enc(fork.id) + '/publish', { json: { title, summary, category, subjects, hardware, version, license } });
      await poller.refresh();
    },
    async unpublish(fork) { await request('POST', 'api/forks/' + enc(fork.id) + '/unpublish', { json: {} }); await poller.refresh(); },
    async removeFork(fork) { await request('DELETE', 'api/forks/' + enc(fork.id)); await poller.refresh(); },
    async uploadRevision(fork, file, message) {
      const res = await request('POST', 'api/forks/' + enc(fork.id) + '/revisions?message=' + enc(message), { body: file, contentType: 'application/zip' });
      await poller.refresh();
      return res.revision;
    },
    startJob: unavailable('Changing a copy with Mana'),
    newExperiment: unavailable('Starting an experiment with Mana'),
    showChat: () => undefined,
    openTerminalApp: () => undefined,
    async signOut() { await request('POST', 'api/logout', { json: {} }); onSignedOut(); },
    signIn: () => (onSignIn ? onSignIn() : undefined),
    refresh: () => poller.refresh(),
  };
  return host;
}

function StandalonePage({ theme, children }) {
  const T = theme === 'dark' ? DARK : LIGHT;
  const vars = {}; for (const [k, v] of Object.entries(T)) vars['--' + k] = v;
  return <div className="eh w-full" style={{ ...vars, colorScheme: theme === 'dark' ? 'dark' : 'light' }}>{children}</div>;
}

function SignInCard({ theme, onSignedIn, transport, onCancel }) {
  const [username, setUsername] = React.useState('');
  const [password, setPassword] = React.useState('');
  const [error, setError] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  async function submit(e) {
    e.preventDefault();
    if (!username.trim() || !password) return;
    setBusy(true); setError('');
    try { const session = await transport.request('POST', 'api/login', { json: { username: username.trim(), password } }); setPassword(''); onSignedIn(session); }
    catch (err) { setError(err.status === 401 || err.status === 403 ? 'The username or password is not right.' : (err.message || String(err))); }
    finally { setBusy(false); }
  }
  return <StandalonePage theme={theme}>
    <main className="eh-signin">
      <form className="eh-signin-card" onSubmit={submit} aria-labelledby="eh-signin-title">
        <div className="eh-brand" aria-hidden="true"><span dangerouslySetInnerHTML={{ __html: PLATFORM_ASSETS.penrose || '' }} style={{ display: 'flex' }} /><span className="eh-word">Alhazen</span><span className="eh-sub">Experiment Platform</span></div>
        <h1 id="eh-signin-title">Sign in</h1>
        <p>Use the account your platform administrator gave you.</p>
        <div className="field"><label htmlFor="eh-signin-user">Username</label><input id="eh-signin-user" autoComplete="username" autoFocus value={username} onChange={(e) => setUsername(e.target.value)} /></div>
        <div className="field"><label htmlFor="eh-signin-pass">Password</label><input id="eh-signin-pass" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} /></div>
        {error ? <p className="eh-signin-error" role="alert">{error}</p> : null}
        <button className="btn btn-primary" type="submit" disabled={busy || !username.trim() || !password}>{busy ? 'Signing in…' : 'Sign in'}</button>
        {onCancel ? <button className="btn btn-quiet" type="button" onClick={onCancel}>Back to the catalogue</button> : null}
      </form>
    </main>
  </StandalonePage>;
}

function StandaloneMessage({ theme, title, text }) {
  return <StandalonePage theme={theme}><main className="eh-signin"><div className="eh-signin-card" role="alert"><h1>{title}</h1><p>{text}</p></div></main></StandalonePage>;
}

function StandaloneRoot() {
  const themeApi = useStandaloneTheme();
  const tokenRef = React.useRef(readFragmentToken(window.location.hash));
  const [session, setSession] = React.useState(null);
  const [failure, setFailure] = React.useState(null);
  const sessionRef = React.useRef(null);
  sessionRef.current = session;
  const signedOut = React.useCallback(() => {
    setSession((s) => (s && s.kind === 'server' ? { ...s, viewer: null, csrf: null } : s));
  }, []);
  const transport = React.useMemo(() => createTransport({
    kind: session ? session.kind : (tokenRef.current ? 'local' : 'server'),
    token: tokenRef.current,
    getCsrf: () => (sessionRef.current && sessionRef.current.csrf) || '',
    onSignedOut: signedOut,
  }), [session && session.kind, signedOut]);
  React.useEffect(() => {
    let off = false;
    transport.request('GET', 'api/session')
      .then((s) => { if (!off) setSession(s); })
      .catch((e) => { if (!off) setFailure(e); });
    return () => { off = true; };
  }, []);
  // A public catalogue (the server's public_catalogue) can be read without an account; the
  // sign-in card then opens on request (Sign in) instead of in front of everything.
  const [wantSignIn, setWantSignIn] = React.useState(false);
  const readable = !!session && (!!session.viewer || !(session.capabilities || {}).signIn || !!session.publicCatalogue);
  const host = React.useMemo(() => (readable
    ? createHttpHost({ session, transport, onSignedOut: signedOut, onSignIn: () => setWantSignIn(true), useThemeHook: useStandaloneTheme }) : null),
  [readable, session && session.kind, session && session.viewer && session.viewer.uid, session && session.csrf, transport]);
  if (failure) {
    const local = !!tokenRef.current || failure.status === 401;
    return <StandaloneMessage theme={themeApi.theme} title="The platform could not be opened"
      text={local ? 'Open the link printed by alhazen platform local; it carries this launch’s access token. (' + (failure.message || failure) + ')' : (failure.message || String(failure))} />;
  }
  if (!session) return <StandalonePage theme={themeApi.theme}><main className="eh-signin"><p className="eh-muted">Loading…</p></main></StandalonePage>;
  if (!host || (wantSignIn && !session.viewer)) {
    return <SignInCard theme={themeApi.theme} transport={transport} onSignedIn={(s) => { setWantSignIn(false); setSession(s); }}
      onCancel={host ? () => setWantSignIn(false) : null} />;
  }
  return <PlatformApp host={host} />;
}
