/* Experiment Hub: the page's pure part (no DOM, no network of its own).
 *
 * The hub page (index.html, hub.js) is one static page served by two hosts:
 * the central hub service (role "server", cookie sessions + CSRF) and the
 * rig's loopback dashboard (role "rig", the workspace token, a stored bearer
 * credential the browser never sees). This file holds everything about that
 * page that can be decided without a document: the address of each screen,
 * the API client and how it reads errors, input checks, and the words and
 * numbers the views show. hub.js draws; this decides.
 *
 * Rules the tests hold (tests/js/hub_core.test.mjs):
 * - A screen is its address. parseRoute(formatRoute(r)) is r for every valid
 *   route, unknown views and malformed ids fall back to the landing page, and
 *   no route can name another origin (safeNext, sameOriginPath).
 * - The client sends exactly one credential kind per role: the workspace
 *   token header on a rig, the CSRF header on central writes; never both,
 *   never a bearer token. Every failure becomes a HubError with a `kind` the
 *   views branch on; a network failure is "offline", not a crash.
 * - Server text is data: nothing here produces markup.
 *
 * Interface: HubCore = {VIEWS, parseRoute, formatRoute, safeNext,
 *   sameOriginPath, HubError, errorFromResponse, createApi, validateHubUrl,
 *   validatePython, validatePackageMetadata, parseList, parseTags,
 *   formatBytes, formatDate, shortHash, hardwareList, platformsText,
 *   trialColumns, cellText, jobState, uploadTotals, firstHttpsUrl,
 *   readToken, seg, nextOffsetLabel}
 */
'use strict';

const HubCore = (() => {
  const API_PREFIX = '/api/hub/v1';

  /* The screens, by `view` in the address. `rig` exists only on a rig; the
   * page sends a central reader who types it to the landing page. */
  const VIEWS = ['home', 'catalog', 'experiment', 'guide', 'signin', 'register', 'library', 'mine', 'data', 'rig'];
  /* Screens that need a signed-in account: a signed-out reader is sent to
   * sign in, and back here afterwards. */
  const PRIVATE_VIEWS = ['library', 'mine', 'data'];
  /* Identifiers the server issues (numbers, UUIDs, slugs): anything else in
   * the address is dropped rather than sent to the server. */
  const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
  const MAX_QUERY = 200;
  const MAX_OFFSET = 1000000;

  /* Which parameters each view keeps, and how each is checked. */
  const PARAMS = {
    home: [],
    catalog: ['q', 'offset'],
    experiment: ['id', 'version', 'tab', 'task'],
    guide: ['section'],
    signin: ['next'],
    register: [],
    library: [],
    mine: ['id', 'new'],
    data: ['experiment', 'subject', 'mode', 'offset', 'session', 'toffset'],
    rig: ['tab', 'project', 'root', 'run', 'job'],
  };
  const RIG_TABS = ['connection', 'installed', 'upload'];
  /* An experiment's reading views (design: Overview / Methods / Tasks &
   * parameters / Versions). */
  const EXPERIMENT_TABS = ['overview', 'methods', 'tasks', 'versions'];
  /* A task or guide-section key inside a documentation descriptor. */
  const DOC_KEY = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

  function cleanText(value) {
    if (typeof value !== 'string') return '';
    return value.replace(/[\u0000-\u001f\u007f]/g, ' ').trim().slice(0, MAX_QUERY);
  }

  function cleanParam(name, raw) {
    if (raw === null || raw === undefined) return undefined;
    switch (name) {
      case 'id': case 'version': case 'experiment': case 'session': case 'project':
      case 'root': case 'job':
        return ID.test(raw) ? raw : undefined;
      case 'run':
        /* A DataView run id: a relative POSIX path of plain segments. */
        return /^[A-Za-z0-9._-]+(\/[A-Za-z0-9._-]+){0,5}$/.test(raw) && !/(^|\/)\.\.?(\/|$)/.test(raw)
          ? raw : undefined;
      case 'offset': case 'toffset': {
        if (!/^\d{1,7}$/.test(raw)) return undefined;
        const n = Number(raw);
        return n > 0 && n <= MAX_OFFSET ? n : undefined;
      }
      case 'q': case 'subject': case 'mode': {
        const text = cleanText(raw);
        return text || undefined;
      }
      case 'next':
        return safeNext(raw) || undefined;
      case 'new':
        return raw === '1' ? '1' : undefined;
      case 'tab':
        return RIG_TABS.includes(raw) || EXPERIMENT_TABS.includes(raw) ? raw : undefined;
      case 'task': case 'section':
        return DOC_KEY.test(raw) ? raw : undefined;
      default:
        return undefined;
    }
  }

  function tabFits(view, name, value) {
    if (name !== 'tab') return true;
    return view === 'rig' ? RIG_TABS.includes(value) : EXPERIMENT_TABS.includes(value);
  }

  /** The screen an address names: {view, ...params}. Unknown views and
   *  parameters that fail their check are dropped, never guessed. */
  function parseRoute(search) {
    const query = new URLSearchParams(typeof search === 'string' ? search : '');
    let view = query.get('view') || 'home';
    if (!VIEWS.includes(view)) view = 'home';
    const route = {view};
    for (const name of PARAMS[view]) {
      const value = cleanParam(name, query.get(name));
      if (value !== undefined && tabFits(view, name, value)) route[name] = value;
    }
    return route;
  }

  /** The address (a search string, '' for the landing page) of a route.
   *  Parameters are written in a fixed order, so equal routes are equal
   *  strings, which is how the page tells a real navigation from a repaint. */
  function formatRoute(route) {
    const view = route && VIEWS.includes(route.view) ? route.view : 'home';
    const query = new URLSearchParams();
    if (view !== 'home') query.set('view', view);
    for (const name of PARAMS[view]) {
      const value = cleanParam(name, route[name] === undefined ? undefined : String(route[name]));
      if (value !== undefined && tabFits(view, name, value)) query.set(name, String(value));
    }
    const text = query.toString();
    return text ? '?' + text : '';
  }

  /** A `next` address after signing in: only one of this page's own search
   *  strings, re-formatted; never a URL, a path or another origin. */
  function safeNext(raw) {
    if (typeof raw !== 'string' || !raw.startsWith('?') || raw.length > 600) return '';
    const route = parseRoute(raw.slice(1));
    if (route.view === 'signin' || route.view === 'register') return '';
    return formatRoute(route) || '?view=home';
  }

  /** A link the server handed back for this origin (an installed project's
   *  workspace address): a path that starts with one '/', or null. */
  function sameOriginPath(value) {
    if (typeof value !== 'string' || value.length > 2000) return null;
    if (!value.startsWith('/') || value.startsWith('//') || value.includes('\\')) return null;
    if (/[\u0000-\u001f\u007f\s]/.test(value)) return null;
    return value;
  }

  /* -- errors ------------------------------------------------------------ */

  /** One failure, with the `kind` the views decide on:
   *  offline, timeout, aborted, unauthorized, forbidden, not_found, conflict,
   *  too_large, invalid, rate_limited, not_configured, unavailable, server,
   *  bad_response, and on a rig run_active, preview_stale,
   *  auth_context_changed. `message` is the server's words when it sent any. */
  class HubError extends Error {
    constructor(kind, message, extra) {
      super(message);
      this.name = 'HubError';
      this.kind = kind;
      this.status = (extra && extra.status) || 0;
      this.code = (extra && extra.code) || '';
      this.retryAfter = (extra && extra.retryAfter) || 0;
      /* The parsed error body, for the one caller that reads more than the
       * message (an unauthenticated GET /auth/me may carry a CSRF token). */
      this.body = (extra && extra.body) || null;
    }
  }

  const KIND_BY_STATUS = {
    400: 'invalid', 401: 'unauthorized', 403: 'forbidden', 404: 'not_found', 409: 'conflict',
    413: 'too_large', 422: 'invalid', 429: 'rate_limited', 502: 'unavailable',
    503: 'unavailable', 504: 'unavailable',
  };
  const DEFAULT_MESSAGE = {
    invalid: 'The request was refused as invalid.',
    unauthorized: 'Sign in to continue.',
    forbidden: 'This account may not do that.',
    not_found: 'Not found. It may have been removed, or it is not yours to see.',
    conflict: 'That conflicts with something that already exists.',
    too_large: 'That is larger than the hub accepts.',
    rate_limited: 'Too many attempts. Wait a moment and try again.',
    unavailable: 'The hub is not reachable right now.',
    not_configured: 'This rig is not connected to a hub yet.',
    run_active: 'A session is running on this rig. Hub work waits until it ends, so it cannot disturb timing.',
    preview_stale: 'The files, recipient or release changed since the preview. Preview again before uploading.',
    auth_context_changed: 'The signed-in account or hub changed, so this transfer is paused. It only continues for the account it was approved for.',
    server: 'The hub failed to answer this request.',
    bad_response: 'The hub sent an answer this page cannot read.',
  };

  /* The server's message from any of the error shapes in use: the v1
   * contract's {error:{code,message}}, the dashboard's {error:"text"}, and
   * a framework's {detail: "text" | [{msg}]}. */
  function messageOf(body) {
    if (!body || typeof body !== 'object') return {code: '', message: ''};
    const error = body.error;
    if (error && typeof error === 'object') {
      return {code: String(error.code || ''), message: String(error.message || '')};
    }
    if (typeof error === 'string') return {code: '', message: error};
    if (typeof body.detail === 'string') return {code: '', message: body.detail};
    if (Array.isArray(body.detail)) {
      const parts = body.detail.map((d) => (d && typeof d === 'object' ? d.msg : d)).filter(Boolean);
      return {code: 'invalid', message: parts.map(String).join('; ')};
    }
    return {code: '', message: ''};
  }

  /** The HubError for a response that was not OK, from its status, headers
   *  and (already parsed, or null) body. */
  function errorFromResponse(status, body, retryAfterHeader) {
    const {code, message} = messageOf(body);
    let kind = KIND_BY_STATUS[status] || (status >= 500 ? 'server' : 'invalid');
    /* The rig adapter's codes (rig-contract): no hub saved yet, the hub not
     * answering, heavy work refused while a session runs, a consent preview
     * that no longer matches, or a job bound to another account or hub. */
    if (code === 'not_configured' || code === 'not_connected') kind = 'not_configured';
    else if (code === 'hub_unreachable' || code === 'hub_redirect') kind = 'unavailable';
    else if (code === 'run_active') kind = 'run_active';
    else if (code === 'preview_stale') kind = 'preview_stale';
    else if (code === 'auth_context_changed') kind = 'auth_context_changed';
    else if (code === 'unauthenticated') kind = 'unauthorized';
    const retryAfter = /^\d{1,6}$/.test(String(retryAfterHeader || '')) ? Number(retryAfterHeader) : 0;
    let text = message || DEFAULT_MESSAGE[kind];
    if (kind === 'rate_limited' && retryAfter) text += ` Try again in ${retryAfter} s.`;
    return new HubError(kind, text.slice(0, 600), {status, code, retryAfter, body});
  }

  /* -- the API client ------------------------------------------------------ */

  /** A client for /api/hub/v1 on this origin.
   *
   *  options: {fetch, role: 'server'|'rig', token: () => string,
   *            csrf: () => string, timeoutMs, setTimeout, clearTimeout}
   *  request(method, path, {query, json, body, contentType, signal,
   *          timeoutMs, headers}) -> Promise<parsed JSON | null>
   *  url(path, query) -> the address of a GET (for links), with the rig's
   *          token in the query, as the dashboard accepts for downloads.
   *
   *  `path` is fixed by the caller and joined to the one prefix; ids are
   *  encoded by the caller with encodeURIComponent (seg below). */
  function createApi(options) {
    const doFetch = options.fetch;
    const role = options.role === 'rig' ? 'rig' : 'server';
    const timers = {
      set: options.setTimeout || setTimeout,
      clear: options.clearTimeout || clearTimeout,
    };
    const defaultTimeout = options.timeoutMs || 20000;

    function address(path, query, prefix) {
      if (typeof path !== 'string' || !path.startsWith('/') || path.includes('//') || path.includes('..')) {
        throw new Error('hub API paths are fixed strings beginning with one "/": ' + JSON.stringify(path));
      }
      const q = new URLSearchParams();
      for (const [name, value] of Object.entries(query || {})) {
        if (value === undefined || value === null || value === '') continue;
        q.set(name, String(value));
      }
      const text = q.toString();
      return (prefix === undefined ? API_PREFIX : prefix) + path + (text ? '?' + text : '');
    }

    async function request(method, path, opts) {
      const o = opts || {};
      /* A bad path is the caller's bug: thrown here, before the network
       * try block, so it is never reported as "offline". */
      const target = address(path, o.query, o.prefix);
      const headers = Object.assign({Accept: 'application/json'}, o.headers || {});
      if (role === 'rig') {
        const token = options.token ? options.token() : '';
        if (token) headers['X-Alhazen-Token'] = token;
      } else if (method !== 'GET' && method !== 'HEAD') {
        const csrf = options.csrf ? options.csrf() : '';
        if (csrf) headers['X-CSRF-Token'] = csrf;
      }
      let body;
      if (o.json !== undefined) {
        headers['Content-Type'] = 'application/json';
        body = JSON.stringify(o.json);
      } else if (o.body !== undefined) {
        headers['Content-Type'] = o.contentType || 'application/octet-stream';
        body = o.body;
      }
      const controller = new AbortController();
      const outer = o.signal;
      let timedOut = false;
      const onAbort = () => controller.abort();
      if (outer) {
        if (outer.aborted) controller.abort();
        else outer.addEventListener('abort', onAbort);
      }
      const limit = o.timeoutMs === 0 ? 0 : (o.timeoutMs || defaultTimeout);
      const timer = limit ? timers.set(() => { timedOut = true; controller.abort(); }, limit) : null;
      let response;
      try {
        response = await doFetch(target, {
          method, headers, body, credentials: 'same-origin', redirect: 'error',
          cache: 'no-store', signal: controller.signal,
        });
      } catch (exc) {
        if (timedOut) throw new HubError('timeout', 'The hub took too long to answer.');
        if (outer && outer.aborted) throw new HubError('aborted', 'Cancelled.');
        throw new HubError('offline', role === 'rig'
          ? 'The rig dashboard is not answering. Is alhazen dashboard still running?'
          : 'The hub cannot be reached. Check your connection and try again.');
      } finally {
        if (timer !== null) timers.clear(timer);
        if (outer) outer.removeEventListener('abort', onAbort);
      }
      let parsed = null;
      let text;
      try {
        text = await response.text();
      } catch (exc) {
        /* The connection dropped while the body was arriving: never read
         * as an empty success. */
        throw new HubError('offline', 'The connection to the hub broke while it was answering. Try again.', {status: response.status});
      }
      if (text) {
        try {
          parsed = JSON.parse(text);
        } catch (exc) {
          if (response.ok) throw new HubError('bad_response', DEFAULT_MESSAGE.bad_response, {status: response.status});
          parsed = null;
        }
      }
      if (!response.ok) {
        const retry = response.headers && response.headers.get ? response.headers.get('Retry-After') : '';
        throw errorFromResponse(response.status, parsed, retry);
      }
      return parsed;
    }

    function url(path, query, prefix) {
      const q = Object.assign({}, query || {});
      if (role === 'rig' && options.token && options.token()) q.token = options.token();
      return address(path, q, prefix);
    }

    return {role, request, url, prefix: API_PREFIX};
  }

  /** One path segment for an id the server issued. */
  function seg(value) {
    return encodeURIComponent(String(value));
  }

  /** The workspace token on a rig: the fragment (#token=…) first, then the
   *  tab's stored copy, the workspace's own sessionStorage key. Returns
   *  {token, fromFragment}. */
  function readToken(hash, stored) {
    const fresh = new URLSearchParams(String(hash || '').replace(/^#/, '')).get('token');
    if (fresh && /^[A-Za-z0-9_-]{16,256}$/.test(fresh)) return {token: fresh, fromFragment: true};
    if (stored && /^[A-Za-z0-9_-]{16,256}$/.test(stored)) return {token: stored, fromFragment: false};
    return {token: '', fromFragment: false};
  }

  /* -- input checks -------------------------------------------------------- */

  const LOOPBACK = ['127.0.0.1', 'localhost', '[::1]'];

  /** A hub base URL an operator typed: HTTPS, or HTTP to this computer for
   *  local development. No user:password, query or fragment.
   *  -> {ok: true, url} | {ok: false, reason} */
  function validateHubUrl(raw) {
    /* -> also {loopbackHttp: true} when it is plain http to this computer,
     * which the rig must be told to allow explicitly (allow_http_loopback). */
    const text = typeof raw === 'string' ? raw.trim() : '';
    if (!text) return {ok: false, reason: 'Enter the hub address, for example https://hub.example.org.'};
    if (text.length > 500) return {ok: false, reason: 'That address is too long.'};
    let parsed;
    try {
      parsed = new URL(text);
    } catch (exc) {
      if (!(exc instanceof TypeError)) throw exc;  // URL() reports a malformed address as TypeError
      return {ok: false, reason: 'That is not a web address. Include https://.'};
    }
    const loopback = LOOPBACK.includes(parsed.hostname);
    if (parsed.protocol !== 'https:' && !(parsed.protocol === 'http:' && loopback)) {
      return {ok: false, reason: 'Use https://. Plain http:// is accepted only for a hub on this computer.'};
    }
    if (parsed.username || parsed.password) {
      return {ok: false, reason: 'Do not put a user name or password in the address; sign in after connecting.'};
    }
    if (parsed.search || parsed.hash || /[?#]/.test(text)) return {ok: false, reason: 'Leave out ? and # parts; enter only the base address.'};
    if (/%/.test(text)) return {ok: false, reason: 'Encoded characters (%) are not accepted in the hub address.'};
    const path = parsed.pathname.replace(/\/+$/, '');
    return {ok: true, url: parsed.protocol + '//' + parsed.host + path, loopbackHttp: parsed.protocol === 'http:'};
  }

  /** The Python interpreter for an installed experiment: one explicit path
   *  or command on one line. The rig checks that it runs. */
  function validatePython(raw) {
    const text = typeof raw === 'string' ? raw.trim() : '';
    if (!text) return {ok: false, reason: 'Choose the Python interpreter this experiment will run with.'};
    if (text.length > 1024) return {ok: false, reason: 'That interpreter path is too long.'};
    if (/[\u0000-\u001f\u007f]/.test(text)) return {ok: false, reason: 'The interpreter must be one line.'};
    return {ok: true, value: text};
  }

  /* The hub's password rule (contract M1): 12 to 1024 characters. The
   * server enforces it; the page says it before a round trip. */
  const PASSWORD_MIN = 12;
  const PASSWORD_MAX = 1024;

  function validatePassword(password, repeat) {
    const text = typeof password === 'string' ? password : '';
    if (text.length < PASSWORD_MIN) return {ok: false, reason: `Use at least ${PASSWORD_MIN} characters.`};
    if (text.length > PASSWORD_MAX) return {ok: false, reason: `Use at most ${PASSWORD_MAX} characters.`};
    if (repeat !== undefined && repeat !== text) return {ok: false, reason: 'The two passwords differ.'};
    return {ok: true};
  }

  const SLUG = /^[a-z0-9][a-z0-9-]{0,63}$/;
  const SEMVER = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;

  /** The package metadata an author edits before packing:
   *  -> {ok, errors: {field: reason}}. Mirrors the manifest rules in the
   *  contract; the rig and the server check again and their word is final. */
  function validatePackageMetadata(meta) {
    const errors = {};
    const m = meta || {};
    if (!SLUG.test(String(m.name || ''))) errors.name = 'Lower-case letters, digits and hyphens, starting with a letter or digit.';
    if (!SEMVER.test(String(m.version || ''))) errors.version = 'A version like 1.0.0.';
    if (!String(m.title || '').trim()) errors.title = 'Give the experiment a title.';
    if (!String(m.license || '').trim()) errors.license = 'Name the licence (for example MIT or CC-BY-4.0).';
    return {ok: Object.keys(errors).length === 0, errors};
  }

  /** One entry per non-empty line (citations). */
  function parseList(text) {
    return String(text || '').split(/\r?\n/).map((s) => s.trim()).filter(Boolean).slice(0, 100);
  }

  /** Comma-separated tags, trimmed, lower-cased, without repeats. */
  function parseTags(text) {
    const seen = [];
    for (const raw of String(text || '').split(',')) {
      const tag = raw.trim().toLowerCase().slice(0, 40);
      if (tag && !seen.includes(tag)) seen.push(tag);
    }
    return seen.slice(0, 20);
  }

  /* -- words and numbers ------------------------------------------------------ */

  /** Bytes as people read them (1 decimal above 1 kB, binary units). */
  function formatBytes(n) {
    const value = Number(n);
    if (!Number.isFinite(value) || value < 0) return '—';
    if (value < 1024) return `${value} B`;
    const units = ['KiB', 'MiB', 'GiB', 'TiB'];
    let v = value / 1024;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
  }

  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  /** An ISO time as "9 Oct 2026, 14:03" in the reader's time zone ('' for
   *  none); a string the page cannot read is shown as given. */
  function formatDate(value, withTime) {
    if (value === null || value === undefined || value === '') return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    const day = `${date.getDate()} ${MONTHS[date.getMonth()]} ${date.getFullYear()}`;
    if (withTime === false) return day;
    const hh = String(date.getHours()).padStart(2, '0');
    const mm = String(date.getMinutes()).padStart(2, '0');
    return `${day}, ${hh}:${mm}`;
  }

  /** The first 12 hex digits of a digest, for display beside the full one. */
  function shortHash(sha) {
    const text = String(sha || '');
    return /^[0-9a-f]{12,}$/i.test(text) ? text.slice(0, 12) : text;
  }

  const HARDWARE = [
    ['display', 'Display', 'A calibrated stimulus display'],
    ['eye_tracker', 'Eye tracker', 'An eye tracker supported by alhazen'],
    ['reward', 'Reward line', 'A reward device (monkey sessions only)'],
  ];

  /** What a manifest says the experiment needs, one row per device kind:
   *  [{key, label, detail, required: true|false|null}] (null: not declared). */
  function hardwareList(manifest) {
    const hw = manifest && typeof manifest.hardware === 'object' && manifest.hardware ? manifest.hardware : {};
    return HARDWARE.map(([key, label, detail]) => ({
      key, label, detail,
      required: typeof hw[key] === 'boolean' ? hw[key] : null,
    }));
  }

  const PLATFORM_NAMES = {linux: 'Linux', darwin: 'macOS', win32: 'Windows'};

  function platformsText(manifest) {
    const list = manifest && Array.isArray(manifest.platforms) ? manifest.platforms : [];
    if (!list.length) return 'Not declared';
    return list.map((p) => PLATFORM_NAMES[p] || String(p)).join(', ');
  }

  /** The columns of a page of trial rows: keys in first-seen order, capped,
   *  so one odd row cannot make the table unreadably wide. */
  function trialColumns(rows, cap) {
    const limit = cap || 40;
    const columns = [];
    for (const row of rows || []) {
      if (!row || typeof row !== 'object') continue;
      for (const key of Object.keys(row)) {
        if (!columns.includes(key)) columns.push(key);
        if (columns.length >= limit) return columns;
      }
    }
    return columns;
  }

  /** One table cell as text: numbers and words as they are, objects as
   *  compact JSON, missing values blank. Long values are cut. */
  function cellText(value) {
    if (value === null || value === undefined) return '';
    const text = typeof value === 'object' ? JSON.stringify(value) : String(value);
    return text.length > 200 ? text.slice(0, 199) + '…' : text;
  }

  /* Rig job states (rig-contract): queued, waiting (a session is running),
   * hashing, uploading, completing, completed, paused (resumable), failed,
   * cancelled. Terminal ones stop the page's polling. */
  const TERMINAL = ['completed', 'failed', 'cancelled'];
  const ACTIVE = ['hashing', 'uploading', 'completing'];
  const JOB_WORDS = {
    queued: 'Queued', waiting: 'Waiting for the running session to end', hashing: 'Checking files',
    uploading: 'Uploading', completing: 'Hub verifying', completed: 'Received and verified by the hub',
    paused: 'Paused', failed: 'Failed', cancelled: 'Cancelled',
  };

  /** A rig upload job as the page shows it:
   *  {status, terminal, ok, fraction (0..1 or null), done, total, error}. */
  function jobState(job) {
    const j = job || {};
    const status = String(j.status || 'queued').toLowerCase();
    const p = j.progress && typeof j.progress === 'object' ? j.progress : j;
    const done = Number(p.bytes_done);
    const total = Number(p.bytes_total);
    const fraction = Number.isFinite(done) && Number.isFinite(total) && total > 0
      ? Math.min(1, Math.max(0, done / total)) : null;
    const terminal = TERMINAL.includes(status);
    const ok = status === 'completed';
    let error = '';
    let retryable = false;
    if (j.error) {
      error = typeof j.error === 'object' ? String(j.error.message || j.error.code || '') : String(j.error);
      retryable = typeof j.error === 'object' && j.error.retryable === true;
    }
    return {
      status, terminal, ok, fraction, retryable,
      running: ACTIVE.includes(status),
      word: JOB_WORDS[status] || status,
      canResume: status === 'paused' || (status === 'failed' && retryable),
      canCancel: !terminal,
      done: Number.isFinite(done) ? done : null,
      total: Number.isFinite(total) ? total : null,
      error,
    };
  }

  /** Files and bytes of an upload or package preview. */
  function uploadTotals(files, declaredTotal) {
    const list = Array.isArray(files) ? files : [];
    let bytes = 0;
    for (const f of list) bytes += Number(f && f.size) || 0;
    const total = Number(declaredTotal);
    return {count: list.length, bytes: Number.isFinite(total) && total >= 0 ? total : bytes};
  }

  /** The first https:// address inside a citation, or '' — offered as a
   *  separate link; the citation itself is always shown as text. */
  function firstHttpsUrl(text) {
    const match = /https:\/\/[^\s<>"']+/.exec(String(text || ''));
    if (!match) return '';
    const candidate = match[0].replace(/[.,;:)\]]+$/, '');
    try {
      const parsed = new URL(candidate);
      return parsed.protocol === 'https:' && !parsed.username && !parsed.password ? parsed.href : '';
    } catch (exc) {
      if (!(exc instanceof TypeError)) throw exc;
      return '';  // not an address after all: the citation stays text only
    }
  }

  /** "Showing 51–100" for a page of a list. */
  function nextOffsetLabel(offset, count) {
    const start = (Number(offset) || 0) + 1;
    return count ? `Showing ${start}–${start + count - 1}` : '';
  }

  return {
    API_PREFIX, VIEWS, PRIVATE_VIEWS, RIG_TABS, EXPERIMENT_TABS, parseRoute, formatRoute, safeNext, sameOriginPath,
    HubError, errorFromResponse, createApi, seg, readToken, validateHubUrl, validatePython, validatePassword,
    validatePackageMetadata, parseList, parseTags, formatBytes, formatDate, shortHash,
    hardwareList, platformsText, trialColumns, cellText, jobState, uploadTotals, firstHttpsUrl,
    nextOffsetLabel,
  };
})();

if (typeof window !== 'undefined') window.HubCore = HubCore;
