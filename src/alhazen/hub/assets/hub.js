/* Experiment Hub: the page (views and controller).
 *
 * One static page for two hosts (see hub_core.js): the central hub, where a
 * browser signs in with a cookie, and a rig's loopback dashboard, which adds
 * the rig's own screens (connect to a hub, trust-and-install a release with an
 * explicit interpreter, package a registered experiment, upload a finished
 * session). The page never decides what a user may do; every action is a
 * request the server checks, and a refusal is shown in the server's words.
 *
 * Structure. index.html holds the masthead (#brand, #role-badge, #primary-nav,
 * #account), #banner, #main and the footer (#theme-*). HubApp.mount(env)
 * draws one screen into #main per address (?view=…, HubCore.parseRoute):
 *   home        landing: the experiment → rig → data path, recent releases
 *   catalog     search the published catalogue
 *   experiment  one experiment: description, citations, licence, release
 *               spec sheet (hardware, versions, digests); add to library;
 *               install (rig) or download (central)
 *   signin, register
 *   library     the releases this account pinned
 *   mine        my experiments: create, edit, add a version (zip upload on
 *               central, package a registered project on a rig), publish
 *   data        my uploaded sessions: filters, detail, trials, exports, files
 *   rig         (rig only) connection, installed releases, session upload
 *
 * Rules (tests/js/hub_page.test.mjs):
 * - Text from the server is set with textContent; no markup is ever parsed.
 * - Each screen draw has an epoch; an answer for an older epoch is dropped and
 *   its request aborted, so Back/Forward never shows a stale screen.
 * - A navigation moves focus to the new screen's heading, unless it was an
 *   in-screen step (a pager, a filter) that names the control to keep.
 * - Nothing reports success the server did not confirm.
 *
 * env (all injectable for tests): {document, location, history, fetch,
 *   sessionStorage, localStorage, setTimeout, clearTimeout, clipboard,
 *   scrollTo, matchMedia}
 */
'use strict';

const HubApp = (() => {
  const C = HubCore;
  const SVG = 'http://www.w3.org/2000/svg';
  const TOKEN_KEY = 'alhazen-workspace-token';
  const THEME_KEY = 'alhazen-workspace-theme';
  const THEMES = ['system', 'light', 'dark'];
  const PAGE = 20;
  const POLL_MS = 1000;
  const POLL_MAX_MS = 8000;
  /* The contract's package cap; the server enforces it, the page says it
   * before sending 256 MiB for nothing. */
  const MAX_PACKAGE_BYTES = 256 * 1024 * 1024;

  function mount(env) {
    const doc = env.document;
    const loc = env.location;
    const hist = env.history;
    const timers = {set: env.setTimeout || setTimeout, clear: env.clearTimeout || clearTimeout};
    const $ = (id) => {
      const el = doc.getElementById(id);
      if (!el) throw new Error(`hub page: #${id} is missing from index.html`);
      return el;
    };

    /* ---- element helper ------------------------------------------------ */

    /** h('a', {class, href, text, on: {click}}, ...children): an element.
     *  Strings become text: alone they are the element's text, beside other
     *  children each is its own <span>. Never parses markup. */
    function h(tag, attrs, ...children) {
      const el = doc.createElement(tag);
      const a = attrs || {};
      for (const [name, value] of Object.entries(a)) {
        if (value === undefined || value === null || value === false) continue;
        if (name === 'text') continue;
        if (name === 'on') {
          for (const [type, fn] of Object.entries(value)) el.addEventListener(type, fn);
        } else if (name === 'class') {
          el.className = value;
        } else if (['value', 'checked', 'disabled', 'hidden', 'selected'].includes(name)) {
          el[name] = value;
        } else if (name === 'dataset') {
          for (const [k, v] of Object.entries(value)) el.dataset[k] = String(v);
        } else {
          el.setAttribute(name, value === true ? '' : String(value));
        }
      }
      const kids = children.flat().filter((c) => c !== null && c !== undefined && c !== false && c !== '');
      if (a.text !== undefined && a.text !== null) {
        if (kids.length) kids.unshift(String(a.text));
        else el.textContent = String(a.text);
      }
      if (kids.length === 1 && typeof kids[0] !== 'object') {
        el.textContent = String(kids[0]);
      } else {
        for (const kid of kids) el.appendChild(typeof kid === 'object' ? kid : h('span', null, String(kid)));
      }
      return el;
    }

    function svg(tag, attrs, ...children) {
      const el = doc.createElementNS(SVG, tag);
      for (const [name, value] of Object.entries(attrs || {})) el.setAttribute(name, String(value));
      for (const kid of children) if (kid) el.appendChild(kid);
      return el;
    }

    function inDocument(el) {
      let node = el;
      while (node) {
        if (node === doc.documentElement) return true;
        node = node.parentNode;
      }
      return false;
    }

    function prevent(event) {
      if (event && typeof event.preventDefault === 'function') event.preventDefault();
    }

    /* ---- state --------------------------------------------------------- */

    const state = {
      role: null,           // 'server' | 'rig'
      config: null,
      api: null,
      token: '',
      user: null,
      csrf: '',
      local: null,          // GET /local/status on a rig
      route: {view: 'home'},
      epoch: 0,
      controller: null,
      pendingFocus: null,   // data-focus key to restore after a draw, or 'heading'
      flash: null,          // one-shot message for the next screen {text, tone, username}
      library: null,        // cached GET /library items for this account (null: not loaded)
      catalog: null,        // {q, at, items}: the whole published catalogue for one search, kept a minute
      createDraft: {prompt: '', provider: 'anthropic', fork: ''},  // the Create form's text (never the key)
      polls: [],
      booted: false,
    };

    /* ---- chrome: banner, masthead, account, theme ------------------------ */

    function banner(text, tone, action) {
      const el = $('banner');
      if (!text) {
        el.hidden = true;
        el.replaceChildren();
        return;
      }
      el.className = 'banner banner-' + (tone || 'info');
      el.replaceChildren(h('span', {class: 'banner-text'}, text));
      if (action) el.appendChild(h('button', {type: 'button', class: 'btn btn-quiet', on: {click: action.run}}, action.label));
      el.hidden = false;
    }

    function link(route, label, attrs) {
      const href = (loc.pathname || '/') + (C.formatRoute(route) || '');
      const a = h('a', Object.assign({href}, attrs || {}), label);
      a.addEventListener('click', (event) => {
        if (event && (event.button > 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)) return;
        prevent(event);
        go(route, {focus: attrs && attrs['data-focus-next']});
      });
      return a;
    }

    function drawNav() {
      const nav = $('primary-nav');
      const items = [
        ['catalog', 'Marketplace'],
        ['library', 'Library'],
        ['mine', 'My experiments'],
        ['data', 'Data'],
      ];
      if (state.role === 'rig') items.push(['rig', 'This rig']);
      const list = h('ul', {class: 'nav-list'});
      for (const [view, label] of items) {
        const current = state.route.view === view
          || (view === 'catalog' && (state.route.view === 'experiment' || state.route.view === 'create'));
        const a = link({view}, label, {class: 'nav-link', 'aria-current': current ? 'page' : null});
        list.appendChild(h('li', null, a));
      }
      nav.replaceChildren(list);
    }

    function drawRoleBadge() {
      const el = $('role-badge');
      if (state.role === 'rig') {
        const hub = rigLink();
        const lamp = hub.state === 'connected' ? 'ok' : (hub.state === 'unreachable' ? 'err' : 'idle');
        el.replaceChildren(h('span', {class: 'lamp lamp-' + lamp, 'aria-hidden': 'true'}),
          h('span', {class: 'role-word'}, 'Rig'), h('span', {class: 'role-detail'}, hub.where));
        el.setAttribute('title', 'This page is served by this rig\u2019s dashboard. ' + hub.sentence);
      } else if (state.role === 'server') {
        // The central hub needs no visible badge (the masthead already says
        // Experiment Hub); assistive technology still hears which host this is.
        el.replaceChildren(h('span', {class: 'visually-hidden'}, 'Hub'));
        el.removeAttribute('title');
      } else {
        el.replaceChildren();
      }
    }

    /** Where this rig's hub link stands, from GET /config (rig.connected,
     *  checked by the dashboard) and GET /local/status (base_url). */
    function rigLink() {
      const rig = state.config && state.config.rig ? state.config.rig : {};
      const base = (state.local && state.local.base_url) || rig.base_url || '';
      if (!base) return {state: 'none', where: 'no hub connected', sentence: 'No hub connected.'};
      if (rig.connected === false) {
        return {state: 'unreachable', where: hostOf(base) + ' · unreachable', sentence: 'The hub is not answering; local work is unaffected.'};
      }
      return {state: 'connected', where: hostOf(base), sentence: 'Connected to ' + hostOf(base) + '.'};
    }

    /** The host part of a hub address for display ("hub.example.org"),
     *  or the address itself when it has none. */
    function hostOf(url) {
      const match = /^[a-z][a-z0-9+.-]*:\/\/([^/?#]+)/i.exec(String(url || ''));
      return match ? match[1] : String(url || '');
    }

    function drawAccount() {
      const el = $('account');
      if (!state.role) {
        el.replaceChildren();
        return;
      }
      const create = state.role === 'server'
        ? link({view: 'create'}, null, {class: 'btn btn-create', 'aria-current': state.route.view === 'create' ? 'page' : null})
        : null;
      if (create) create.append(sparkGlyph(), h('span', null, 'Create'), h('span', {class: 'create-tail'}, ' with AI'));
      if (state.user) {
        const who = h('span', {class: 'who'},
          h('span', {class: 'who-label'}, state.role === 'rig' ? 'Operator' : 'Signed in as'),
          h('span', {class: 'who-name'}, state.user.display_name || state.user.username),
          h('span', {class: 'who-handle'}, '@' + state.user.username));
        const out = h('button', {type: 'button', class: 'btn btn-quiet', on: {click: signOut}}, 'Sign out');
        el.replaceChildren(...[create, who, out].filter(Boolean));
      } else {
        el.replaceChildren(...[create,
          link({view: 'signin', next: currentNext()}, 'Sign in', {class: 'btn btn-quiet'}),
          link({view: 'register'}, 'Register', {class: 'btn btn-line'})].filter(Boolean));
      }
    }

    function drawChrome() {
      drawRoleBadge();
      drawNav();
      drawAccount();
    }

    function currentNext() {
      const r = state.route;
      if (r.view === 'signin' || r.view === 'register') return undefined;
      return C.formatRoute(r) || undefined;
    }

    function applyTheme(choice) {
      const root = doc.documentElement;
      if (choice === 'light' || choice === 'dark') root.dataset.theme = choice;
      else delete root.dataset.theme;
      for (const name of THEMES) {
        const button = doc.getElementById('theme-' + name);
        if (button) button.setAttribute('aria-pressed', String(name === choice));
      }
    }

    function setTheme(choice) {
      if (!THEMES.includes(choice)) return;
      applyTheme(choice);
      try {
        if (choice === 'system') env.localStorage.removeItem(THEME_KEY);
        else env.localStorage.setItem(THEME_KEY, choice);
      } catch (exc) {
        if (!isStorageRefusal(exc)) throw exc;
        banner('Your theme choice cannot be remembered in this browser; it applies until you leave.', 'info');
      }
    }

    /* A browser refuses storage (private windows, blocked site data, a full
     * quota) with these DOMException names; anything else is a bug. */
    function isStorageRefusal(exc) {
      return Boolean(exc && ['SecurityError', 'QuotaExceededError', 'NS_ERROR_DOM_QUOTA_REACHED'].includes(exc.name));
    }

    function storedTheme() {
      let value = null;
      try {
        value = env.localStorage ? env.localStorage.getItem(THEME_KEY) : null;
      } catch (exc) {
        if (!isStorageRefusal(exc)) throw exc;
        value = null;  // storage blocked: Auto, as on a first visit
      }
      return THEMES.includes(value) ? value : 'system';
    }

    /* ---- navigation -------------------------------------------------------- */

    /** Show `route`. A new address is pushed (or replaced) and drawn; focus
     *  goes to the heading unless `focus` names a control to keep. */
    function go(route, opts) {
      const o = opts || {};
      const search = C.formatRoute(route);
      const target = (loc.pathname || '/') + search;
      const now = (loc.pathname || '/') + (loc.search || '');
      if (target !== now) {
        if (o.replace) hist.replaceState(null, '', target);
        else hist.pushState(null, '', target);
      }
      state.pendingFocus = o.focus || 'heading';
      draw();
    }

    function onPopState() {
      state.pendingFocus = 'heading';
      draw();
    }

    /** Put focus where the last navigation asked, once that element exists.
     *  Called after the synchronous draw and after each async fill. */
    function settleFocus(final) {
      const key = state.pendingFocus;
      if (!key) return;
      if (key !== 'heading') {
        /* 'a|b': the first of these controls that exists (a pager's Next
         * may be gone on the last page; its Previous then takes focus). */
        for (const name of key.split('|')) {
          const el = doc.querySelector(`[data-focus="${name}"]`);
          if (el && inDocument(el) && !el.disabled) {
            el.focus({preventScroll: true});
            state.pendingFocus = null;
            return;
          }
        }
        if (!final) return;
      }
      const heading = doc.querySelector('[data-heading]');
      if (heading && inDocument(heading)) {
        heading.focus({preventScroll: true});
        if (env.scrollTo) env.scrollTo(0, 0);
      } else if (!final) {
        return;  // the heading arrives with the screen's data; wait for it
      }
      state.pendingFocus = null;
    }

    /* ---- requests ----------------------------------------------------------- */

    function api(method, path, opts) {
      return state.api.request(method, path, opts);
    }

    /** A request for the current screen: dropped (null) if the reader has
     *  moved on by the time it answers. Errors of the current screen throw. */
    async function screenRequest(epoch, method, path, opts) {
      const o = Object.assign({signal: state.controller ? state.controller.signal : undefined}, opts || {});
      try {
        const answer = await api(method, path, o);
        return epoch === state.epoch ? {ok: true, value: answer} : null;
      } catch (exc) {
        if (epoch !== state.epoch || (exc && exc.kind === 'aborted')) return null;
        noteFailure(exc);
        return {ok: false, error: exc};
      }
    }

    /** Side effects of any failure: a 401 signs the page out; an offline hub
     *  shows the banner that says local work is unaffected. */
    function noteFailure(exc) {
      if (!exc || !exc.kind) return;
      if (exc.kind === 'unauthorized' && state.user) {
        state.user = null;
        state.csrf = '';
        state.library = null;
        drawChrome();
        banner('Your session ended. Sign in again to continue.', 'warn');
      } else if (exc.kind === 'unauthorized' && state.role === 'rig' && state.local && state.local.user) {
        /* The adapter cleared its stored credential on that 401. */
        state.local = Object.assign({}, state.local, {user: null, state: 'signed_out'});
        state.user = null;
        drawChrome();
      } else if (exc.kind === 'offline' || exc.kind === 'unavailable' || exc.kind === 'timeout') {
        const local = state.role === 'rig'
          ? ' Installed experiments and the data on this rig are unaffected; uploads can resume later.'
          : '';
        banner(exc.message + local, 'warn', {label: 'Retry', run: () => { banner(''); go(state.route, {replace: true}); }});
      }
    }

    /* ---- sign-in state -------------------------------------------------------- */

    async function loadMe() {
      try {
        const me = await api('GET', '/auth/me');
        state.user = me && me.user ? me.user : null;
        if (me && me.csrf_token) state.csrf = String(me.csrf_token);
      } catch (exc) {
        state.user = null;
        if (exc.body && exc.body.csrf_token) state.csrf = String(exc.body.csrf_token);
        if (exc.kind !== 'unauthorized' && exc.kind !== 'not_configured') throw exc;
      }
    }

    async function loadLocal() {
      if (state.role !== 'rig') return;
      try {
        state.local = await api('GET', '/local/status');
      } catch (exc) {
        state.local = {state: 'error', error: exc.message};
      }
    }

    async function signOut() {
      let answer = null;
      try {
        answer = await api('POST', '/auth/logout', {json: {}});
      } catch (exc) {
        if (exc.kind !== 'unauthorized') {
          banner('Signing out failed: ' + exc.message, 'err');
          return;
        }
      }
      state.user = null;
      state.csrf = '';
      state.library = null;
      if (state.role === 'rig') await loadLocal();
      if (state.role === 'server') await loadMe().catch(noteFailure);
      drawChrome();
      banner('');
      let text = 'Signed out.';
      if (answer && answer.revoked === false) {
        text = 'Signed out on this rig. The hub could not be reached to revoke the sign-in, so it stays valid there until it expires; this rig no longer holds it.';
      }
      if (answer && Number(answer.paused_jobs) > 0) {
        text += ` ${answer.paused_jobs} upload${answer.paused_jobs === 1 ? ' was' : 's were'} paused; they resume only for the same account.`;
      }
      state.flash = {text, tone: answer && answer.revoked === false ? 'warn' : 'ok'};
      go({view: 'home'});
    }

    async function libraryItems(force) {
      if (!state.user) return [];
      if (state.library && !force) return state.library;
      const answer = await api('GET', '/library', {query: {limit: 100}});
      state.library = answer && Array.isArray(answer.items) ? answer.items : [];
      return state.library;
    }

    /* ---- startup ------------------------------------------------------------- */

    async function boot() {
      applyTheme(storedTheme());
      for (const name of THEMES) {
        const button = doc.getElementById('theme-' + name);
        if (button) button.addEventListener('click', () => setTheme(name));
      }
      let stored = '';
      try {
        stored = env.sessionStorage.getItem(TOKEN_KEY) || '';
      } catch (exc) {
        if (!isStorageRefusal(exc)) throw exc;
        stored = '';  // blocked storage: only a token in the address can be used
      }
      const found = C.readToken(loc.hash, stored);
      state.token = found.token;
      if (found.fromFragment) {
        try {
          env.sessionStorage.setItem(TOKEN_KEY, found.token);
        } catch (exc) {
          if (!isStorageRefusal(exc)) throw exc;
          banner('This browser blocks tab storage, so reloading this page will need the address alhazen dashboard printed.', 'info');
        }
        hist.replaceState(null, '', (loc.pathname || '/') + (loc.search || ''));
      }
      env.window && env.window.addEventListener && env.window.addEventListener('popstate', onPopState);
      const probe = C.createApi(apiOptions(state.token ? 'rig' : 'server'));
      try {
        state.config = await probe.request('GET', '/config');
      } catch (exc) {
        drawStartupFailure(exc);
        return;
      }
      state.role = state.config && state.config.role === 'rig' ? 'rig' : 'server';
      state.api = C.createApi(apiOptions(state.role));
      try {
        await Promise.all([loadMe(), loadLocal()]);
      } catch (exc) {
        noteFailure(exc);
      }
      state.booted = true;
      doc.documentElement.dataset.role = state.role;
      state.pendingFocus = null;
      draw();
    }

    function apiOptions(role) {
      return {
        fetch: env.fetch, role,
        token: () => state.token,
        csrf: () => state.csrf,
        setTimeout: timers.set, clearTimeout: timers.clear,
      };
    }

    function drawStartupFailure(exc) {
      const main = $('main');
      let title = 'The hub page cannot start';
      let body = exc.message;
      if (exc.kind === 'forbidden') {
        title = 'Open this page from the rig dashboard\u2019s address';
        body = 'On a rig, the hub page needs the dashboard\u2019s private address. Run '
          + '`alhazen dashboard --hub` on this computer and open the address it prints.';
      } else if (exc.kind === 'offline' || exc.kind === 'timeout' || exc.kind === 'unavailable') {
        title = 'The hub is not answering';
      }
      main.replaceChildren(h('section', {class: 'screen'},
        h('h1', {class: 'screen-title', tabindex: '-1', 'data-heading': ''}, title),
        h('p', {class: 'lede'}, body),
        h('button', {type: 'button', class: 'btn btn-primary', on: {click: () => { main.replaceChildren(); boot(); }}}, 'Try again')));
    }

    /* ---- drawing a screen --------------------------------------------------- */

    function draw() {
      if (!state.booted) return;
      state.epoch += 1;
      if (state.controller) state.controller.abort();
      state.controller = new AbortController();
      for (const id of state.polls) timers.clear(id);
      state.polls = [];
      let route = C.parseRoute(loc.search || '');
      if (route.view === 'rig' && state.role !== 'rig') route = {view: 'home'};
      if (C.PRIVATE_VIEWS.includes(route.view) && !state.user) {
        state.flash = state.flash || {text: 'Sign in to see that page.', tone: 'info'};
        const next = C.formatRoute(route);
        route = {view: 'signin', next};
        hist.replaceState(null, '', (loc.pathname || '/') + C.formatRoute(route));
      }
      state.route = route;
      drawChrome();
      const main = $('main');
      const screen = SCREENS[route.view] || SCREENS.home;
      const ctx = {epoch: state.epoch, route};
      const node = screen(ctx);
      main.replaceChildren(node);
      titleFromHeading();
      settleFocus(false);
    }

    /** The tab title names the screen by its heading, also when the heading
     *  arrives with the screen's data (an experiment, a session). */
    function titleFromHeading() {
      const heading = $('main').querySelector('[data-heading]');
      const text = heading ? heading.textContent.replace(/\s+/g, ' ').trim() : '';
      doc.title = (text ? text + ' · ' : '') + 'Alhazen Experiment Hub';
    }

    function flashNode() {
      const f = state.flash;
      state.flash = null;
      if (!f) return null;
      return h('p', {class: 'note note-' + (f.tone || 'info'), role: 'status'}, f.text);
    }

    /* The first argument names the screen's section; it is no longer drawn
     * above the heading (the navigation already says where you are). */
    function screenShell(section_, title, lede) {
      const section = h('section', {class: 'screen'});
      const head = h('header', {class: 'screen-head'},
        h('h1', {class: 'screen-title', tabindex: '-1', 'data-heading': ''}, title),
        lede ? h('p', {class: 'lede'}, lede) : null);
      section.appendChild(head);
      const flash = flashNode();
      if (flash) section.appendChild(flash);
      return section;
    }

    /** A region that shows loading, then content, an empty state or an error
     *  (with Retry), and is told which by the screen's loader. */
    function region(label, extraClass) {
      const el = h('div', {class: 'region' + (extraClass ? ' ' + extraClass : ''), 'aria-live': 'polite', 'aria-busy': 'true'},
        h('p', {class: 'loading'}, h('span', {class: 'spinner', 'aria-hidden': 'true'}), 'Loading ' + label + '\u2026'));
      return {
        el,
        fill(...nodes) {
          el.setAttribute('aria-busy', 'false');
          el.replaceChildren(...nodes.flat().filter(Boolean));
          titleFromHeading();
          settleFocus(screenSettled());
        },
        fail(error, retry) {
          el.setAttribute('aria-busy', 'false');
          el.replaceChildren(errorBox(error, retry));
          titleFromHeading();
          settleFocus(screenSettled());
        },
      };
    }

    /** Whether every region of the screen has answered: only then may a
     *  missing focus target fall back to the heading (a pager arrives with
     *  the last region, after the session's own data). */
    function screenSettled() {
      return !$('main').querySelector('[aria-busy="true"]');
    }

    function errorBox(error, retry) {
      const box = h('div', {class: 'callout callout-err', role: 'alert'});
      let text = error && error.message ? error.message : String(error);
      if (error && error.kind === 'unauthorized') {
        box.appendChild(h('p', null, 'Your session has ended.'));
        box.appendChild(link({view: 'signin', next: currentNext()}, 'Sign in again', {class: 'btn btn-primary'}));
        return box;
      }
      if (error && error.kind === 'not_configured' && state.role === 'rig') {
        box.appendChild(h('p', null, text));
        box.appendChild(link({view: 'rig', tab: 'connection'}, 'Connect this rig to a hub', {class: 'btn btn-primary'}));
        return box;
      }
      box.appendChild(h('p', null, text));
      if (retry) box.appendChild(h('button', {type: 'button', class: 'btn btn-line', on: {click: retry}}, 'Try again'));
      return box;
    }

    function emptyState(title, body, ...actions) {
      return h('div', {class: 'empty'},
        h('p', {class: 'empty-title'}, title),
        body ? h('p', {class: 'empty-body'}, body) : null,
        actions.length ? h('div', {class: 'actions'}, ...actions) : null);
    }

    function retryCurrent() {
      go(state.route, {replace: true});
    }

    /** An inline status line under a form: role=status for progress and
     *  success, role=alert for refusals. */
    function statusLine() {
      const el = h('p', {class: 'form-status', hidden: true});
      return {
        el,
        show(text, tone) {
          el.className = 'form-status form-status-' + (tone || 'info');
          el.setAttribute('role', tone === 'err' ? 'alert' : 'status');
          el.textContent = text;
          el.hidden = !text;
        },
        clear() { el.hidden = true; el.textContent = ''; },
      };
    }

    function field(label, input, hint, error) {
      const id = input.getAttribute('id');
      const parts = [h('label', {class: 'field-label', for: id}, label), input];
      if (hint) {
        const hintId = id + '-hint';
        input.setAttribute('aria-describedby', hintId);
        parts.push(h('p', {class: 'field-hint', id: hintId}, hint));
      }
      if (error) parts.push(error);
      return h('div', {class: 'field'}, ...parts);
    }

    let uid = 0;
    function nextId(prefix) {
      uid += 1;
      return `${prefix}-${uid}`;
    }

    function input(attrs) {
      return h('input', Object.assign({id: nextId('f'), class: 'input'}, attrs));
    }

    function textarea(attrs, value) {
      const el = h('textarea', Object.assign({id: nextId('f'), class: 'input textarea'}, attrs));
      el.value = value || '';
      return el;
    }

    function checkbox(label, attrs) {
      const box = h('input', Object.assign({type: 'checkbox', id: nextId('c'), class: 'check'}, attrs));
      return {box, el: h('label', {class: 'check-row', for: box.getAttribute('id')}, box, h('span', null, label))};
    }

    /** A <select> of [value, label] pairs showing `value`, or the first
     *  option when `value` is not one of them (a browser would otherwise
     *  show the first while reporting no value). */
    function select(options, value, attrs) {
      const el = h('select', Object.assign({id: nextId('s'), class: 'input select'}, attrs));
      const known = options.some(([v]) => String(v) === String(value));
      const shown = known ? String(value) : (options[0] ? String(options[0][0]) : '');
      for (const [v, label] of options) {
        const opt = h('option', {value: v}, label);
        if (String(v) === shown) opt.selected = true;
        el.appendChild(opt);
      }
      el.value = shown;
      return el;
    }

    function spec(rows) {
      const dl = h('dl', {class: 'spec'});
      for (const [term, value, opts] of rows) {
        if (value === null || value === undefined || value === '') continue;
        dl.appendChild(h('div', {class: 'spec-row'},
          h('dt', null, term),
          typeof value === 'object' ? h('dd', {class: opts && opts.mono ? 'mono' : null}, value)
            : h('dd', {class: opts && opts.mono ? 'mono' : null}, String(value))));
      }
      return dl;
    }

    function copyButton(text, label) {
      const button = h('button', {type: 'button', class: 'btn btn-quiet btn-small'}, label || 'Copy');
      button.addEventListener('click', async () => {
        if (!env.clipboard || typeof env.clipboard.writeText !== 'function') {
          button.textContent = 'No clipboard here: select the text';
        } else {
          try {
            await env.clipboard.writeText(text);
            button.textContent = 'Copied';
          } catch (exc) {
            /* The browser's refusals (no permission, insecure page); said on
             * the button. Anything else is a bug and propagates. */
            if (!exc || !['NotAllowedError', 'SecurityError'].includes(exc.name)) throw exc;
            button.textContent = 'Copy refused: select the text';
          }
        }
        timers.set(() => { button.textContent = label || 'Copy'; }, 2500);
      });
      return button;
    }

    function digest(sha) {
      if (!sha) return null;
      return h('span', {class: 'digest'},
        h('code', {class: 'mono digest-text', title: 'SHA-256 of the whole release archive'}, String(sha)),
        copyButton(String(sha), 'Copy'));
    }

    function hardwareLamps(manifest, compact) {
      const list = h('ul', {class: 'hw' + (compact ? ' hw-compact' : '')});
      for (const item of C.hardwareList(manifest)) {
        const word = item.required === true ? 'needed' : item.required === false ? 'not needed' : 'not declared';
        const tone = item.required === true ? 'need' : item.required === false ? 'off' : 'idle';
        list.appendChild(h('li', {class: 'hw-item hw-' + tone, title: item.detail + ': ' + word},
          h('span', {class: 'hw-glyph', 'aria-hidden': 'true'}, glyph(item.key)),
          h('span', {class: 'hw-label'}, item.label),
          compact ? h('span', {class: 'visually-hidden'}, word) : h('span', {class: 'hw-word'}, word)));
      }
      return list;
    }

    /* Small line icons for the device kinds, drawn as SVG (no image files,
     * nothing the CSP would refuse). */
    function glyph(kind) {
      const box = svg('svg', {viewBox: '0 0 20 20', width: '16', height: '16', class: 'glyph', focusable: 'false'});
      const stroke = {fill: 'none', stroke: 'currentColor', 'stroke-width': '1.6', 'stroke-linecap': 'round', 'stroke-linejoin': 'round'};
      if (kind === 'display') {
        box.appendChild(svg('rect', Object.assign({x: '2.5', y: '3.5', width: '15', height: '10', rx: '1.5'}, stroke)));
        box.appendChild(svg('path', Object.assign({d: 'M7 17h6M10 13.5V17'}, stroke)));
      } else if (kind === 'eye_tracker') {
        box.appendChild(svg('path', Object.assign({d: 'M1.8 10s3-5.5 8.2-5.5S18.2 10 18.2 10s-3 5.5-8.2 5.5S1.8 10 1.8 10z'}, stroke)));
        box.appendChild(svg('circle', Object.assign({cx: '10', cy: '10', r: '2.4'}, stroke)));
      } else {
        box.appendChild(svg('path', Object.assign({d: 'M10 2.8s-4.5 5.2-4.5 8.5a4.5 4.5 0 0 0 9 0C14.5 8 10 2.8 10 2.8z'}, stroke)));
      }
      return box;
    }

    function pager(offset, count, nextOffset, makeRoute, focusKey) {
      const prev = offset > 0;
      const next = nextOffset !== null && nextOffset !== undefined;
      if (!prev && !next) return null;
      const nav = h('nav', {class: 'pager', 'aria-label': 'Pages'});
      nav.appendChild(h('span', {class: 'pager-label'}, C.nextOffsetLabel(offset, count)));
      if (prev) {
        nav.appendChild(link(makeRoute(Math.max(0, offset - PAGE) || undefined), 'Previous',
          {class: 'btn btn-line', 'data-focus': focusKey + '-prev', 'data-focus-next': focusKey + '-prev|' + focusKey + '-next'}));
      }
      if (next) {
        nav.appendChild(link(makeRoute(nextOffset), 'Next',
          {class: 'btn btn-line', 'data-focus': focusKey + '-next', 'data-focus-next': focusKey + '-next|' + focusKey + '-prev'}));
      }
      return nav;
    }

    /* ======================================================================
     * Screens
     * ==================================================================== */

    /* ---- shared pieces ------------------------------------------------- */

    function versionLabel(version) {
      if (!version) return '';
      const v = version.version || (version.manifest && version.manifest.version) || '';
      return v ? 'v' + v : 'version ' + String(version.id || '');
    }

    function ownerLine(experiment) {
      const o = experiment && experiment.owner;
      if (!o) return null;
      return h('p', {class: 'byline'}, h('span', {class: 'byline-by'}, 'by'),
        h('span', {class: 'byline-name'}, o.display_name || o.username),
        h('span', {class: 'byline-handle'}, '@' + o.username));
    }

    function isOwner(experiment) {
      return Boolean(state.user && experiment && experiment.owner && experiment.owner.id === state.user.id);
    }

    function installFor(sha) {
      const list = state.local && Array.isArray(state.local.installed) ? state.local.installed : [];
      return list.find((i) => i && i.sha256 === sha) || null;
    }

    function workspaceLink(install, label) {
      const href = install ? C.sameOriginPath(install.workspace_url) : null;
      if (!href) return null;
      return h('a', {class: 'btn btn-primary', href}, label || 'Open in the workspace');
    }

    /** A catalogue or library entry: {experiment, version}. A card: the
     *  stimulus picture drawn from its tags, title (the card's link), one
     *  line, author, what it needs, version and licence. */
    function listing(item, extra, opts) {
      const o = opts || {};
      const experiment = (item && item.experiment) || {};
      const version = (item && item.version) || null;
      const manifest = (version && version.manifest) || {};
      const card = h('article', {class: 'card' + (o.featured ? ' card-featured' : '')});
      card.appendChild(h('div', {class: 'card-art', 'aria-hidden': 'true'},
        schematic(C.schematicKind(experiment.tags), experiment.id || experiment.title, o.featured ? 'wide' : 'strip')));
      const body = h('div', {class: 'card-body'});
      body.appendChild(h('h3', {class: 'card-title'},
        link({view: 'experiment', id: experiment.id, version: version ? version.id : undefined}, experiment.title || 'Untitled experiment',
          {class: 'card-link'})));
      if (experiment.summary) body.appendChild(h('p', {class: 'card-summary'}, experiment.summary));
      const o_ = experiment.owner;
      if (o_) body.appendChild(h('p', {class: 'card-by'}, o_.display_name || o_.username));
      card.appendChild(body);
      const needs = h('ul', {class: 'card-needs', 'aria-label': 'Needs'});
      const keys = item ? C.hardwareKeys(item) : [];
      for (const key of keys) {
        const label = key === 'eye_tracker' ? 'Eye tracker' : key === 'reward' ? 'Reward line' : 'Display only';
        needs.appendChild(h('li', {class: 'card-need', title: label},
          h('span', {class: 'card-need-glyph', 'aria-hidden': 'true'}, glyph(key)),
          h('span', {class: key === 'display' || o.featured ? 'card-need-word' : 'visually-hidden'}, label)));
      }
      const subjects = item ? C.subjectKeys(item) : [];
      const meta = [version && !o.library ? versionLabel(version) : '', experiment.license || manifest.license || ''].filter(Boolean).join(' \u00b7 ');
      card.appendChild(h('div', {class: 'card-foot'}, needs,
        subjects.length ? h('span', {class: 'card-subject'}, subjects.map((s) => s === 'human' ? 'Human' : 'Monkey').join(' \u00b7 ')) : null,
        h('span', {class: 'card-meta'}, meta)));
      if (o.owned) card.appendChild(h('p', {class: 'card-owned'}, checkGlyph(), h('span', null, 'In your library')));
      if (extra) card.appendChild(extra);
      return card;
    }

    /* ---- stimulus pictures ---------------------------------------------------
     * A small diagram of the stimulus a listing names in its tags (a Gabor,
     * random dots, a grid, rings, inducers, a target step, a disc, a fixation
     * point). Inline SVG, every colour a token through its class. It is an
     * index picture, not the experiment's stimulus. */

    function schematic(kind, seed, shape) {
      const wide = shape === 'wide';
      const W_ = wide ? 360 : 240;
      const H_ = wide ? 150 : 100;
      const box = svg('svg', {viewBox: `0 0 ${W_} ${H_}`, class: 'sch sch-' + kind, focusable: 'false', preserveAspectRatio: 'xMidYMid slice'});
      box.appendChild(svg('rect', {x: '0', y: '0', width: String(W_), height: String(H_), class: 'sch-bg'}));
      const cx = W_ / 2;
      const cy = H_ / 2;
      const s = H_ / 100;
      const rand = C.seededRandom(seed);
      const fix = (x, y, r) => svg('circle', {cx: f(x), cy: f(y), r: f(r || 2.6 * s), class: 'sch-accent'});
      const f = (n) => String(Math.round(n * 10) / 10);
      const gabor = (x, y, r, angle, id) => {
        const g = svg('g', {transform: `rotate(${angle} ${f(x)} ${f(y)})`});
        const period = r / 2.6;
        for (let k = -6; k <= 6; k += 1) {
          const off = k * period;
          if (Math.abs(off) > r) continue;
          const half = Math.sqrt(r * r - off * off);
          const env = Math.exp(-(off * off) / (2 * (r / 2.2) * (r / 2.2)));
          g.appendChild(svg('line', {x1: f(x + off), y1: f(y - half * 0.92), x2: f(x + off), y2: f(y + half * 0.92),
            class: 'sch-stroke', 'stroke-width': f(period * 0.5), 'stroke-linecap': 'round', opacity: f(0.12 + 0.78 * env)}));
        }
        return g;
      };
      if (kind === 'gabor') {
        box.appendChild(gabor(cx + 26 * s, cy, 34 * s, 28));
        box.appendChild(svg('circle', {cx: f(cx - 52 * s), cy: f(cy), r: f(12 * s), class: 'sch-ring'}));
        box.appendChild(fix(cx - 52 * s, cy));
      } else if (kind === 'rivalry') {
        box.appendChild(gabor(cx - 42 * s, cy, 28 * s, 45));
        box.appendChild(gabor(cx + 42 * s, cy, 28 * s, -45));
        box.appendChild(svg('rect', {x: f(cx - 76 * s), y: f(cy - 36 * s), width: f(68 * s), height: f(72 * s), rx: f(4 * s), class: 'sch-ring'}));
        box.appendChild(svg('rect', {x: f(cx + 8 * s), y: f(cy - 36 * s), width: f(68 * s), height: f(72 * s), rx: f(4 * s), class: 'sch-ring'}));
      } else if (kind === 'dots') {
        const R = 38 * s;
        box.appendChild(svg('circle', {cx: f(cx), cy: f(cy), r: f(R), class: 'sch-ring sch-dash'}));
        for (let i = 0; i < 46; i += 1) {
          const a = rand() * Math.PI * 2;
          const d = Math.sqrt(rand()) * (R - 4 * s);
          const x = cx + Math.cos(a) * d;
          const y = cy + Math.sin(a) * d;
          if (Math.hypot(x - cx, y - cy) < 6 * s) continue;
          const coherent = rand() < 0.55;
          if (coherent) {
            box.appendChild(svg('line', {x1: f(x - 6 * s), y1: f(y), x2: f(x), y2: f(y), class: 'sch-trail', 'stroke-width': f(1.6 * s), 'stroke-linecap': 'round'}));
          }
          box.appendChild(svg('circle', {cx: f(x), cy: f(y), r: f(1.9 * s), class: 'sch-ink'}));
        }
        box.appendChild(fix(cx, cy));
      } else if (kind === 'grid') {
        const cols = wide ? 7 : 5;
        const rows = 3;
        const sq = 22 * s;
        const gap = 8 * s;
        const w0 = cols * sq + (cols - 1) * gap;
        const h0 = rows * sq + (rows - 1) * gap;
        for (let r = 0; r < rows; r += 1) for (let c = 0; c < cols; c += 1) {
          box.appendChild(svg('rect', {x: f(cx - w0 / 2 + c * (sq + gap)), y: f(cy - h0 / 2 + r * (sq + gap)), width: f(sq), height: f(sq), class: 'sch-ink'}));
        }
        box.appendChild(fix(cx - w0 / 2 + 2 * (sq + gap) - gap / 2, cy - h0 / 2 + (sq + gap) - gap / 2, 2.2 * s));
      } else if (kind === 'rings') {
        const ring = (x, n, dist, rr) => {
          for (let i = 0; i < n; i += 1) {
            const a = (i / n) * Math.PI * 2;
            box.appendChild(svg('circle', {cx: f(x + Math.cos(a) * dist), cy: f(cy + Math.sin(a) * dist), r: f(rr), class: 'sch-mid'}));
          }
          box.appendChild(svg('circle', {cx: f(x), cy: f(cy), r: f(8 * s), class: 'sch-accent'}));
        };
        ring(cx - 48 * s, 6, 27 * s, 11 * s);
        ring(cx + 52 * s, 8, 15 * s, 4 * s);
      } else if (kind === 'inducers') {
        const R = 30 * s;
        const r = 10 * s;
        for (let i = 0; i < 3; i += 1) {
          const a = -Math.PI / 2 + (i / 3) * Math.PI * 2;
          const x = cx + Math.cos(a) * R;
          const y = cy + 4 * s + Math.sin(a) * R;
          const toward = Math.atan2(cy + 4 * s - y, cx - x);
          const a1 = toward - Math.PI / 6;
          const a2 = toward + Math.PI / 6;
          box.appendChild(svg('path', {class: 'sch-ink', d: `M${f(x)} ${f(y)}L${f(x + Math.cos(a2) * r)} ${f(y + Math.sin(a2) * r)}`
            + `A${f(r)} ${f(r)} 0 1 1 ${f(x + Math.cos(a1) * r)} ${f(y + Math.sin(a1) * r)}Z`}));
        }
        box.appendChild(fix(cx - 96 * s, cy));
      } else if (kind === 'step') {
        const x0 = cx - 70 * s;
        const x1 = cx + 46 * s;
        box.appendChild(fix(x0, cy, 3.2 * s));
        box.appendChild(svg('path', {d: `M${f(x0 + 10 * s)} ${f(cy)}H${f(x1 - 10 * s)}`, class: 'sch-trail sch-dash', 'stroke-width': f(1.6 * s)}));
        box.appendChild(svg('path', {d: `M${f(x1 - 16 * s)} ${f(cy - 5 * s)}L${f(x1 - 9 * s)} ${f(cy)}L${f(x1 - 16 * s)} ${f(cy + 5 * s)}`, class: 'sch-trail', 'stroke-width': f(1.6 * s), fill: 'none'}));
        box.appendChild(svg('circle', {cx: f(x1), cy: f(cy), r: f(4.5 * s), class: 'sch-ring'}));
        box.appendChild(svg('circle', {cx: f(x1 + 22 * s), cy: f(cy), r: f(4.5 * s), class: 'sch-ink'}));
        box.appendChild(svg('path', {d: `M${f(x1 + 16 * s)} ${f(cy - 12 * s)}h${f(-10 * s)}`, class: 'sch-trail', 'stroke-width': f(1.6 * s)}));
      } else if (kind === 'disc') {
        box.appendChild(svg('circle', {cx: f(cx), cy: f(cy), r: f(40 * s), class: 'sch-soft'}));
        box.appendChild(svg('circle', {cx: f(cx), cy: f(cy), r: f(26 * s), class: 'sch-mid', opacity: '0.35'}));
        box.appendChild(fix(cx, cy));
      } else {
        box.appendChild(svg('circle', {cx: f(cx), cy: f(cy), r: f(24 * s), class: 'sch-ring sch-dash'}));
        let x = cx + 9 * s;
        let y = cy - 6 * s;
        let d = `M${f(x)} ${f(y)}`;
        for (let i = 0; i < 22; i += 1) {
          x += (rand() - 0.5) * 5 * s + (cx - x) * 0.18;
          y += (rand() - 0.5) * 5 * s + (cy - y) * 0.18;
          d += `L${f(x)} ${f(y)}`;
        }
        box.appendChild(svg('path', {d, class: 'sch-trail', 'stroke-width': f(1 * s), 'stroke-linejoin': 'round'}));
        box.appendChild(svg('circle', {cx: f(cx), cy: f(cy), r: f(10 * s), class: 'sch-ring'}));
        box.appendChild(fix(cx, cy, 3 * s));
      }
      return box;
    }

    function checkGlyph() {
      const box = svg('svg', {viewBox: '0 0 20 20', width: '16', height: '16', class: 'glyph glyph-check', focusable: 'false', 'aria-hidden': 'true'});
      box.appendChild(svg('path', {d: 'M4.5 10.5l3.5 3.5 7.5-8', fill: 'none', stroke: 'currentColor', 'stroke-width': '2', 'stroke-linecap': 'round', 'stroke-linejoin': 'round'}));
      return box;
    }

    function sparkGlyph() {
      const box = svg('svg', {viewBox: '0 0 20 20', width: '16', height: '16', class: 'glyph glyph-spark', focusable: 'false', 'aria-hidden': 'true'});
      box.appendChild(svg('path', {d: 'M10 2.5l1.7 4.6 4.6 1.7-4.6 1.7L10 15.1l-1.7-4.6-4.6-1.7 4.6-1.7zM15.5 13.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z', fill: 'currentColor'}));
      return box;
    }

    function filterGlyph() {
      const box = svg('svg', {viewBox: '0 0 20 20', width: '16', height: '16', class: 'glyph', focusable: 'false', 'aria-hidden': 'true'});
      box.appendChild(svg('path', {d: 'M3 5h14M6 10h8M8.5 15h3', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.8', 'stroke-linecap': 'round'}));
      return box;
    }

    /* ---- home ------------------------------------------------------------ */

    /* Line icons for the landing page's three facts (SVG, nothing the CSP
     * would refuse). */
    function pathGlyph(kind) {
      const box = svg('svg', {viewBox: '0 0 24 24', width: '20', height: '20', class: 'path-glyph', focusable: 'false', 'aria-hidden': 'true'});
      const line = {fill: 'none', stroke: 'currentColor', 'stroke-width': '1.6', 'stroke-linecap': 'round', 'stroke-linejoin': 'round'};
      if (kind === 'experiment') {
        box.appendChild(svg('path', Object.assign({d: 'M12 2.8l8 4.6v9.2l-8 4.6-8-4.6V7.4z'}, line)));
        box.appendChild(svg('path', Object.assign({d: 'M4 7.4l8 4.6 8-4.6M12 12v9.2'}, line)));
      } else if (kind === 'rig') {
        box.appendChild(svg('rect', Object.assign({x: '3', y: '4', width: '18', height: '12.5', rx: '1.8'}, line)));
        box.appendChild(svg('path', Object.assign({d: 'M9 20.5h6M12 16.5v4'}, line)));
        box.appendChild(svg('circle', Object.assign({cx: '12', cy: '10.2', r: '2.6'}, line)));
      } else {
        box.appendChild(svg('ellipse', Object.assign({cx: '12', cy: '5.5', rx: '7.5', ry: '2.7'}, line)));
        box.appendChild(svg('path', Object.assign({d: 'M4.5 5.5v13c0 1.5 3.4 2.7 7.5 2.7s7.5-1.2 7.5-2.7v-13M4.5 12c0 1.5 3.4 2.7 7.5 2.7s7.5-1.2 7.5-2.7'}, line)));
      }
      return box;
    }

    /* The hub's three guarantees, one short line each. */
    function features() {
      const rows = [
        ['experiment', 'Experiments are pinned releases', 'Immutable, checksummed and versioned, with licence and citations.'],
        ['rig', 'Rigs run them offline', 'A rig installs only the release it trusts. The hub never runs code.'],
        ['data', 'Data leaves only on upload', 'Sessions stay on the rig until you preview and opt in.'],
      ];
      return h('ul', {class: 'features', 'aria-label': 'How the hub works'},
        ...rows.map(([kind, name, text]) => h('li', {class: 'feature'},
          h('span', {class: 'feature-icon'}, pathGlyph(kind)),
          h('h2', {class: 'feature-name'}, name),
          h('p', {class: 'feature-text'}, text))));
    }

    /** The landing page's latest releases as one table: a row each, the
     *  title its link, then version, hardware, licence and platforms. */
    function releaseTable(items, caption) {
      const heads = ['Experiment', 'Version', 'Needs', 'Licence', 'Runs on'];
      const table = h('table', {class: 'table table-list'},
        h('caption', {class: 'visually-hidden'}, caption),
        h('thead', null, h('tr', null,
          ...heads.map((t, i) => h('th', {scope: 'col', class: i === 0 ? 'col-main' : null}, t)))));
      const tbody = h('tbody');
      for (const item of items) tbody.appendChild(releaseRow(item));
      table.appendChild(tbody);
      return h('div', {class: 'table-wrap'}, table);
    }

    function releaseRow(item) {
      const experiment = (item && item.experiment) || {};
      const version = (item && item.version) || null;
      const manifest = (version && version.manifest) || {};
      const o = experiment.owner;
      const main = h('td', {class: 'col-main'},
        h('h3', {class: 'list-title'},
          link({view: 'experiment', id: experiment.id, version: version ? version.id : undefined}, experiment.title || 'Untitled experiment')),
        experiment.summary ? h('p', {class: 'list-summary'}, experiment.summary) : null,
        o ? h('p', {class: 'list-by'}, o.display_name || o.username) : null);
      return h('tr', null, main,
        h('td', {class: 'list-version'}, version ? versionLabel(version) : ''),
        h('td', null, hardwareLamps(manifest, true)),
        h('td', {class: 'list-meta'}, experiment.license || manifest.license || 'Not stated'),
        h('td', {class: 'list-meta'}, manifest.platforms ? C.platformsText(manifest) : ''));
    }

    function screenHome(ctx) {
      const section = h('section', {class: 'screen screen-home'});
      const actions = h('div', {class: 'actions'});
      actions.appendChild(link({view: 'catalog'}, 'Browse the catalogue', {class: 'btn btn-primary'}));
      if (state.user) actions.appendChild(link({view: 'library'}, 'Your library', {class: 'btn btn-line'}));
      else actions.appendChild(link({view: 'signin'}, 'Sign in', {class: 'btn btn-line'}));
      section.appendChild(h('div', {class: 'hero'},
        h('h1', {class: 'hero-title', tabindex: '-1', 'data-heading': ''}, 'Your experiment hub'),
        h('p', {class: 'lede'}, 'Pinned releases your rigs install and run offline.'),
        actions));
      const flash = flashNode();
      if (flash) section.appendChild(flash);
      if (state.role === 'rig') section.appendChild(rigStrip());

      /* The catalogue itself, live: the landing page's picture. */
      const recent = region('the latest releases', 'preview-body');
      section.appendChild(h('section', {class: 'preview', 'aria-label': 'Latest releases'},
        h('div', {class: 'preview-bar'},
          h('h2', {class: 'preview-title'}, 'Latest releases'),
          link({view: 'catalog'}, 'View all', {class: 'btn btn-quiet btn-small'})),
        recent.el));
      section.appendChild(features());
      (async () => {
        const got = await screenRequest(ctx.epoch, 'GET', '/catalog', {query: {limit: 6}});
        if (!got) return;
        if (!got.ok) return recent.fail(got.error, retryCurrent);
        const items = (got.value && got.value.items) || [];
        if (!items.length) {
          return recent.fill(emptyState('Nothing is published yet.',
            'Releases appear here once their author publishes one.',
            state.user ? link({view: 'mine'}, 'Publish one of yours', {class: 'btn btn-line'}) : null));
        }
        recent.fill(releaseTable(items, 'Latest releases'));
      })();
      return section;
    }

    function rigStrip() {
      const local = state.local || {};
      const linkState = rigLink();
      const who = local.user ? (local.user.display_name || local.user.username) + ' (@' + local.user.username + ')' : 'nobody signed in';
      const installed = Array.isArray(local.installed) ? local.installed.length : 0;
      return h('section', {class: 'rig-strip', 'aria-label': 'This rig'},
        h('div', {class: 'rig-strip-item'}, h('span', {class: 'meta-key'}, 'Hub'),
          h('span', {class: 'meta-val'}, h('span', {class: 'lamp lamp-' + (linkState.state === 'connected' ? 'ok' : linkState.state === 'unreachable' ? 'err' : 'idle'), 'aria-hidden': 'true'}),
            h('span', null, linkState.where))),
        h('div', {class: 'rig-strip-item'}, h('span', {class: 'meta-key'}, 'Operator'), h('span', {class: 'meta-val'}, who)),
        h('div', {class: 'rig-strip-item'}, h('span', {class: 'meta-key'}, 'Installed from the hub'), h('span', {class: 'meta-val mono'}, String(installed))),
        h('div', {class: 'rig-strip-actions'},
          link({view: 'rig'}, 'Manage this rig', {class: 'btn btn-line'}),
          h('a', {class: 'btn btn-quiet', href: '/'}, 'Open the workspace')));
    }

    /* ---- catalogue ----------------------------------------------------- */

    /** The whole published catalogue for one search (the server's own
     *  search), page by page, kept a minute so ticking a filter is instant. */
    async function catalogItems(ctx, q) {
      const key = q || '';
      const now = Date.now();
      if (state.catalog && state.catalog.q === key && now - state.catalog.at < 60000) return {ok: true, value: state.catalog.items};
      const items = [];
      let offset = 0;
      for (let page = 0; page < 10; page += 1) {
        const got = await screenRequest(ctx.epoch, 'GET', '/catalog', {query: {query: q, limit: 100, offset: offset || undefined}});
        if (!got) return null;
        if (!got.ok) return got;
        const batch = (got.value && got.value.items) || [];
        items.push(...batch);
        const next = got.value ? got.value.next_offset : null;
        if (next === null || next === undefined || !batch.length) break;
        offset = next;
      }
      state.catalog = {q: key, at: now, items};
      return {ok: true, value: items};
    }

    /** Whether filters should apply as they are ticked (a wide page) or wait
     *  for "Show" in the phone's sheet. */
    function wideLayout() {
      const w = env.window;
      if (!w || typeof w.matchMedia !== 'function') return true;
      return w.matchMedia('(min-width: 900px)').matches;
    }

    function catalogRoute(r, over) {
      const base = {view: 'catalog', q: r.q, cat: r.cat, hw: r.hw, who: r.who, os: r.os, lic: r.lic, sort: r.sort};
      return Object.assign(base, over || {});
    }

    function screenCatalog(ctx) {
      const r = ctx.route;
      const section = h('section', {class: 'screen screen-store'});
      const flash = flashNode();
      /* The store's head: title, the search bar, the category chips. */
      const q = input({type: 'search', name: 'q', value: r.q || '', autocomplete: 'off', maxlength: '200',
        placeholder: 'Search experiments', 'data-focus': 'catalog-q', 'aria-label': 'Search the marketplace'});
      const form = h('form', {class: 'store-search', role: 'search'},
        h('span', {class: 'store-search-icon', 'aria-hidden': 'true'}, searchGlyph()), q,
        h('button', {type: 'submit', class: 'btn btn-primary', 'data-focus': 'catalog-go'}, 'Search'));
      form.addEventListener('submit', (event) => {
        prevent(event);
        go(catalogRoute(r, {q: q.value, offset: undefined}), {focus: 'catalog-q'});
      });
      const chips = h('nav', {class: 'store-cats', 'aria-label': 'Categories'});
      section.appendChild(h('header', {class: 'store-head'},
        h('div', {class: 'store-head-row'},
          h('div', null,
            h('h1', {class: 'screen-title', tabindex: '-1', 'data-heading': ''}, 'Marketplace'),
            h('p', {class: 'lede'}, 'Experiments their authors publish as pinned releases. Add one to your library and install it on a rig.')),
          state.role === 'server' ? link({view: 'create'}, 'Create with AI', {class: 'btn btn-line store-create'}) : null),
        form, chips));
      if (flash) section.appendChild(flash);
      const layout = h('div', {class: 'store-layout'});
      const filters = h('aside', {class: 'store-filters', id: 'store-filters', 'aria-label': 'Filters'});
      const results = region('the marketplace', 'store-results');
      layout.append(filters, results.el);
      section.appendChild(layout);
      const offset = r.offset || 0;
      (async () => {
        const got = await catalogItems(ctx, r.q);
        if (!got) return;
        if (!got.ok) return results.fail(got.error, retryCurrent);
        const all = got.value;
        const owned = new Set();
        const list = C.filterCatalog(all, r);
        const filtered = Boolean(r.cat || C.activeFilters(r) || r.q);
        const parts = [];
        if (all.length) {
          drawCategories(chips, r, all);
          drawFilters(filters, r, all);
          parts.push(storeToolbar(r, all, list));
        } else {
          filters.hidden = true;
          layout.classList.add('store-layout-empty');
        }
        if (!all.length) {
          parts.push(r.q
            ? emptyState(`No published experiment matches \u201c${r.q}\u201d.`, 'Try fewer or different words.',
              link({view: 'catalog'}, 'Show everything', {class: 'btn btn-line'}))
            : emptyState('The marketplace is empty.',
              'Nothing has been published yet. An author publishes one release at a time, with a licence, after checking it holds no participant data.'));
          return results.fill(parts);
        }
        if (!list.length) {
          parts.push(emptyState('No experiment fits these filters.', 'Remove a filter or choose another category.',
            link(catalogRoute(r, {cat: undefined, hw: undefined, who: undefined, os: undefined, lic: undefined}), 'Clear filters', {class: 'btn btn-line'})));
          return results.fill(parts);
        }
        let page = list.slice(offset, offset + PAGE);
        const cards = [];
        const featured = !filtered && !offset && r.sort !== 'name' && list.length >= 6;
        if (featured) {
          /* The first page leads with the three newest, larger; the grid
           * continues from the fourth, so a page still holds PAGE listings. */
          const top = list.slice(0, 3);
          page = list.slice(3, PAGE);
          const row = h('div', {class: 'featured-grid'});
          for (const item of top) cards.push([item, row.appendChild(listing(item, null, {featured: true}))]);
          parts.push(h('section', {class: 'store-block', 'aria-label': 'Latest releases'},
            h('h2', {class: 'store-block-title'}, 'Latest releases'), row));
        }
        const grid = h('div', {class: 'card-grid'});
        for (const item of page) cards.push([item, grid.appendChild(listing(item))]);
        parts.push(h('section', {class: 'store-block', 'aria-label': filtered ? 'Results' : 'All experiments'},
          h('h2', {class: 'store-block-title'}, filtered ? 'Results' : (featured ? 'More experiments' : 'All experiments')),
          grid,
          pager(offset, featured ? PAGE : page.length, offset + PAGE < list.length ? offset + PAGE : null, (o) => catalogRoute(r, {offset: o}), 'catalog')));
        results.fill(parts);
        /* Mark what this account already has, once the library answers. */
        if (state.user) {
          libraryItems(false).then((items) => {
            if (ctx.epoch !== state.epoch) return;
            for (const i of items) if (i && i.experiment) owned.add(i.experiment.id);
            for (const [item, card] of cards) {
              if (owned.has(item.experiment && item.experiment.id) && !card.querySelector('.card-owned')) {
                card.appendChild(h('p', {class: 'card-owned'}, checkGlyph(), h('span', null, 'In your library')));
              }
            }
          }).catch((exc) => { noteFailure(exc); });
        }
      })();
      return section;
    }

    function searchGlyph() {
      const box = svg('svg', {viewBox: '0 0 20 20', width: '18', height: '18', class: 'glyph', focusable: 'false'});
      box.appendChild(svg('circle', {cx: '8.5', cy: '8.5', r: '5.5', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.8'}));
      box.appendChild(svg('path', {d: 'M12.8 12.8L17 17', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.8', 'stroke-linecap': 'round'}));
      return box;
    }

    function drawCategories(nav, r, all) {
      const counts = C.catalogFacets(all, r).cat;
      const list = h('ul', {class: 'cat-list'});
      const total = C.filterCatalog(all, Object.assign({}, r, {cat: undefined})).length;
      const all_ = link(catalogRoute(r, {cat: undefined, offset: undefined}), null,
        {class: 'cat-chip', 'aria-current': !r.cat ? 'true' : null, 'data-focus': 'cat-all', 'data-focus-next': 'cat-all'});
      all_.append(h('span', null, 'All'), h('span', {class: 'cat-count'}, String(total)));
      list.appendChild(h('li', null, all_));
      for (const [key, label] of C.CATEGORIES) {
        const a = link(catalogRoute(r, {cat: r.cat === key ? undefined : key, offset: undefined}), null,
          {class: 'cat-chip', 'aria-current': r.cat === key ? 'true' : null, 'data-focus': 'cat-' + key, 'data-focus-next': 'cat-' + key});
        a.append(h('span', null, label), h('span', {class: 'cat-count'}, String(counts[key] || 0)));
        list.appendChild(h('li', null, a));
      }
      nav.replaceChildren(list);
    }

    function storeToolbar(r, all, list) {
      const bar = h('div', {class: 'store-toolbar'});
      const n = list.length;
      bar.appendChild(h('p', {class: 'store-count', role: 'status'},
        n === all.length ? `${n} experiment${n === 1 ? '' : 's'}` : `${n} of ${all.length} experiments`));
      const pills = h('ul', {class: 'store-pills', 'aria-label': 'Active filters'});
      const names = {hw: C.FACETS.hw, who: C.FACETS.who, os: C.FACETS.os};
      for (const name of ['hw', 'who', 'os', 'lic']) {
        for (const key of C.facetList(r[name])) {
          const label = name === 'lic' ? key : ((names[name].find(([k]) => k === key) || [key, key])[1]);
          const a = link(catalogRoute(r, {[name]: C.toggleFacet(r[name], key), offset: undefined}), null,
            {class: 'pill', 'aria-label': 'Remove filter ' + label});
          a.append(h('span', null, label), h('span', {class: 'pill-x', 'aria-hidden': 'true'}, '\u00d7'));
          pills.appendChild(h('li', null, a));
        }
      }
      if (pills.children.length) bar.appendChild(pills);
      const spacer = h('span', {class: 'store-spacer'});
      bar.appendChild(spacer);
      const count = C.activeFilters(r);
      const open = h('button', {type: 'button', class: 'btn btn-line store-filter-open', 'aria-controls': 'store-filters', 'aria-expanded': 'false', 'data-focus': 'filters-open'},
        filterGlyph(), h('span', null, count ? `Filters (${count})` : 'Filters'));
      open.addEventListener('click', () => openSheet(open));
      bar.appendChild(open);
      const sortId = nextId('sort');
      const sort = select(C.SORTS.map(([key, label]) => [key, label]), r.sort || 'newest', {id: sortId, 'data-focus': 'catalog-sort'});
      sort.addEventListener('change', () => go(catalogRoute(r, {sort: sort.value, offset: undefined}), {focus: 'catalog-sort', replace: true}));
      bar.appendChild(h('div', {class: 'store-sort'}, h('label', {for: sortId, class: 'store-sort-label'}, 'Sort'), sort));
      return bar;
    }

    function openSheet(opener) {
      const sheet = doc.getElementById('store-filters');
      if (!sheet) return;
      sheet.classList.add('is-open');
      sheet.setAttribute('role', 'dialog');
      sheet.setAttribute('aria-modal', 'true');
      opener.setAttribute('aria-expanded', 'true');
      const first = sheet.querySelector('input, button');
      if (first && typeof first.focus === 'function') first.focus();
    }

    function closeSheet() {
      const sheet = doc.getElementById('store-filters');
      if (!sheet) return;
      sheet.classList.remove('is-open');
      sheet.removeAttribute('role');
      sheet.removeAttribute('aria-modal');
      const opener = $('main').querySelector('.store-filter-open');
      if (opener) {
        opener.setAttribute('aria-expanded', 'false');
        if (typeof opener.focus === 'function') opener.focus();
      }
    }

    /** The filter column (a sheet on a phone): hardware, subject, platform,
     *  licence, each choice with how many it would show. */
    function drawFilters(aside, r, all) {
      const facets = C.catalogFacets(all, r);
      let pending = Object.assign({}, r);
      const showButton = h('button', {type: 'button', class: 'btn btn-primary btn-block'});
      const paintShow = () => {
        const n = C.filterCatalog(all, pending).length;
        showButton.textContent = `Show ${n} experiment${n === 1 ? '' : 's'}`;
      };
      const groups = [
        ['hw', 'Hardware', C.FACETS.hw],
        ['who', 'Subject', C.FACETS.who],
        ['os', 'Platform', C.FACETS.os],
        ['lic', 'Licence', Object.keys(facets.lic).sort().map((k) => [k, k])],
      ];
      const head = h('div', {class: 'sheet-head'}, h('h2', {class: 'filters-title'}, 'Filters'),
        h('button', {type: 'button', class: 'btn btn-quiet btn-small sheet-close', on: {click: closeSheet}}, 'Close'));
      const body = h('div', {class: 'filters-body'});
      for (const [name, title, options] of groups) {
        if (!options.length) continue;
        const set = h('fieldset', {class: 'facet'}, h('legend', {class: 'facet-title'}, title));
        for (const [key, label] of options) {
          const id = nextId('f-' + name);
          const box = h('input', {type: 'checkbox', id, class: 'facet-box', checked: C.facetList(r[name]).includes(key),
            'data-focus': 'f-' + name + '-' + key});
          box.addEventListener('change', () => {
            pending = Object.assign({}, pending, {[name]: C.toggleFacet(pending[name], key), offset: undefined});
            if (wideLayout()) go(catalogRoute(pending), {focus: 'f-' + name + '-' + key, replace: true});
            else paintShow();
          });
          set.appendChild(h('div', {class: 'facet-row'}, box,
            h('label', {for: id, class: 'facet-label'}, h('span', null, label),
              h('span', {class: 'facet-count'}, String(facets[name][key] || 0)))));
        }
        body.appendChild(set);
      }
      const any = C.activeFilters(r);
      if (any) body.appendChild(link(catalogRoute(r, {hw: undefined, who: undefined, os: undefined, lic: undefined, offset: undefined}),
        'Clear filters', {class: 'btn btn-quiet btn-small filters-clear'}));
      showButton.addEventListener('click', () => go(catalogRoute(pending), {focus: 'filters-open', replace: true}));
      paintShow();
      const foot = h('div', {class: 'sheet-foot'}, showButton);
      aside.addEventListener('keydown', (event) => { if (event && event.key === 'Escape' && aside.classList.contains('is-open')) closeSheet(); });
      aside.replaceChildren(head, body, foot);
    }

    /* ---- one experiment ------------------------------------------------------ */

    function sortVersions(versions) {
      return (Array.isArray(versions) ? versions.slice() : []).sort((a, b) =>
        String(b.created_at || '').localeCompare(String(a.created_at || '')));
    }

    function chooseVersion(experiment, versions, wanted) {
      const list = sortVersions(versions);
      return list.find((v) => v.id === wanted)
        || list.find((v) => v.id === experiment.published_version_id)
        || list[0] || null;
    }

    function screenExperiment(ctx) {
      const r = ctx.route;
      const section = h('section', {class: 'screen screen-experiment'});
      const body = region('the experiment');
      const flash = flashNode();
      if (flash) section.appendChild(flash);
      section.appendChild(body.el);
      if (!r.id) {
        body.fill(emptyState('No experiment chosen.', null, link({view: 'catalog'}, 'Open the marketplace', {class: 'btn btn-line'})));
        return section;
      }
      (async () => {
        const got = await screenRequest(ctx.epoch, 'GET', '/experiments/' + C.seg(r.id));
        if (!got) return;
        if (!got.ok) {
          if (got.error.kind === 'not_found') {
            return body.fill(h('div', {class: 'screen-head'},
              h('h1', {class: 'screen-title', tabindex: '-1', 'data-heading': ''}, 'Experiment not found'),
              h('p', {class: 'lede'}, 'It does not exist, is not published, or is private to its author.'),
              link({view: 'catalog'}, 'Back to the marketplace', {class: 'btn btn-line'})));
          }
          return body.fail(got.error, retryCurrent);
        }
        const experiment = (got.value && got.value.experiment) || {};
        const versions = sortVersions(got.value && got.value.versions);
        const version = chooseVersion(experiment, versions, r.version);
        const tab = r.tab || 'overview';
        const main = h('div', {class: 'exp-main'},
          experimentHead(experiment, version, versions), experimentTabs(r, tab, versions.length),
          experimentTab(ctx, tab, experiment, version, versions));
        body.fill(h('nav', {class: 'crumbs', 'aria-label': 'You are here'},
          link({view: 'catalog'}, 'Marketplace'), h('span', {class: 'crumb-sep', 'aria-hidden': 'true'}, '/'),
          h('span', {'aria-current': 'page'}, experiment.title || 'Untitled experiment')),
        h('div', {class: 'exp-layout'}, main, releaseSheet(ctx, experiment, version, versions)));
      })();
      return section;
    }

    function experimentHead(experiment, version, versions) {
      const published = experiment.published_version_id;
      let status;
      if (version && published && version.id === published) status = h('span', {class: 'chip chip-public'}, 'Public release');
      else if (isOwner(experiment)) status = h('span', {class: 'chip chip-private'}, 'Private: only you can see this version');
      const cats = new Map(C.CATEGORIES);
      const tags = Array.isArray(experiment.tags) ? experiment.tags.slice(0, 12) : [];
      return h('header', {class: 'exp-hero'},
        h('div', {class: 'exp-art', 'aria-hidden': 'true'}, schematic(C.schematicKind(tags), experiment.id || experiment.title, 'wide')),
        h('div', {class: 'exp-hero-text'},
          h('h1', {class: 'screen-title', tabindex: '-1', 'data-heading': ''}, experiment.title || 'Untitled experiment'),
          ownerLine(experiment),
          experiment.summary ? h('p', {class: 'lede'}, experiment.summary) : null,
          h('div', {class: 'chips'}, status,
            ...tags.map((t) => cats.has(t)
              ? link({view: 'catalog', cat: t}, cats.get(t), {class: 'chip chip-tag chip-link'})
              : h('span', {class: 'chip chip-tag'}, t)),
            versions.length > 1 ? h('span', {class: 'chip'}, versions.length + ' versions visible to you') : null)));
    }

    function experimentTabs(r, tab, versionCount) {
      const names = [['overview', 'Overview'], ['methods', 'Methods'], ['tasks', 'Tasks & parameters'], ['versions', 'Versions']];
      const nav = h('nav', {class: 'tabs', 'aria-label': 'Experiment sections'});
      for (const [key, label] of names) {
        const text = key === 'versions' && versionCount ? `${label} (${versionCount})` : label;
        nav.appendChild(link({view: 'experiment', id: r.id, version: r.version, tab: key === 'overview' ? undefined : key}, text,
          {class: 'tab', 'aria-current': key === tab ? 'page' : null, 'data-focus': 'exp-tab-' + key, 'data-focus-next': 'exp-tab-' + key}));
      }
      return nav;
    }

    function experimentTab(ctx, tab, experiment, version, versions) {
      if (tab === 'methods' || tab === 'tasks') return documentationTab(ctx, tab, experiment, version);
      if (tab === 'versions') return versionsTab(ctx.route, experiment, versions, version);
      return overviewTab(ctx, experiment, version);
    }

    /** Overview: what it is, a Methods excerpt and the task list (from the
     *  release's own documentation, when it has some), the release details
     *  and citations. */
    function overviewTab(ctx, experiment, version) {
      const r = ctx.route;
      const reading = h('div', {class: 'exp-reading'});
      reading.appendChild(h('h2', {class: 'block-title'}, 'About'));
      reading.appendChild(experiment.description
        ? h('div', {class: 'prose'}, ...String(experiment.description).split(/\n{2,}/).map((p) => h('p', null, p)))
        : h('p', {class: 'muted'}, 'The author has not written a description.'));
      if (version) {
        const docsArea = h('div', {class: 'exp-docs', 'aria-live': 'polite', 'aria-busy': 'true'});
        reading.appendChild(docsArea);
        (async () => {
          const got = await screenRequest(ctx.epoch, 'GET',
            '/experiments/' + C.seg(experiment.id) + '/versions/' + C.seg(version.id) + '/documentation');
          if (!got) return;
          docsArea.setAttribute('aria-busy', 'false');
          const documentation = got.ok && got.value ? got.value.documentation : null;
          const methods = documentation && documentation.methods ? documentation.methods.markdown : '';
          const tasks = documentation && Array.isArray(documentation.tasks) ? documentation.tasks : [];
          const parts = [h('h2', {class: 'block-title'}, 'Methods')];
          const excerpt = C.methodsExcerpt(methods, 460);
          if (excerpt) {
            parts.push(h('div', {class: 'prose prose-excerpt'}, ...excerpt.split(/\n{2,}/).map((p) => h('p', null, p))),
              link({view: 'experiment', id: r.id, version: r.version, tab: 'methods'}, 'Read the full Methods', {class: 'more-link'}));
          } else {
            parts.push(h('p', {class: 'muted'}, got.ok ? 'This release has no Methods document.' : 'The Methods could not be read: ' + got.error.message));
          }
          parts.push(h('h2', {class: 'block-title'}, 'Tasks'));
          if (tasks.length) {
            parts.push(h('ul', {class: 'task-cards'}, ...tasks.map((t) => h('li', {class: 'task-card'},
              h('h3', {class: 'task-card-title'}, link({view: 'experiment', id: r.id, version: r.version, tab: 'tasks', task: t.id}, t.title || t.id)),
              t.summary ? h('p', {class: 'task-card-text'}, t.summary) : null,
              Array.isArray(t.parameters) ? h('p', {class: 'task-card-meta'}, t.parameters.length + ' documented parameters') : null))));
          } else {
            parts.push(h('p', {class: 'muted'}, 'This release does not document its tasks.'));
          }
          docsArea.replaceChildren(...parts);
          settleFocus(screenSettled());
        })();
      }
      if (version) {
        const m = version.manifest || {};
        reading.appendChild(h('h2', {class: 'block-title'}, 'Release details'));
        reading.appendChild(spec([
          ['Python', m.python_min ? '\u2265 ' + m.python_min : null, {mono: true}],
          ['alhazen', m.alhazen_min ? '\u2265 ' + m.alhazen_min : null, {mono: true}],
          ['Entry point', m.entrypoint, {mono: true}],
          ['Files', Array.isArray(m.files) ? String(m.files.length) : null],
          ['Archive', C.formatBytes(version.size)],
          ['Released', C.formatDate(version.created_at)],
          ['SHA-256', digest(version.sha256)],
        ]));
      }
      reading.appendChild(h('h2', {class: 'block-title'}, 'Citations'));
      const citations = Array.isArray(experiment.citations) && experiment.citations.length
        ? experiment.citations : (version && version.manifest && version.manifest.citations) || [];
      if (citations.length) {
        const ol = h('ol', {class: 'citations'});
        for (const c of citations) {
          const url = C.firstHttpsUrl(c);
          ol.appendChild(h('li', null, h('span', {class: 'citation-text'}, String(c)),
            url ? h('a', {class: 'citation-link', href: url, rel: 'noopener noreferrer', target: '_blank'}, 'Open source') : null));
        }
        reading.appendChild(ol);
      } else {
        reading.appendChild(h('p', {class: 'muted'}, 'No citations listed. Cite the release by its version and SHA-256.'));
      }
      return reading;
    }

    /** The action card beside the experiment (sticky on a wide page): the
     *  library action first, then download, the version, licence, hardware. */
    function releaseSheet(ctx, experiment, version, versions) {
      const aside = h('aside', {class: 'sheet action-card', 'aria-label': 'Release'});
      if (!version) {
        aside.appendChild(h('h2', {class: 'sheet-title'}, 'No release yet'));
        aside.appendChild(h('p', {class: 'muted'}, isOwner(experiment)
          ? 'Add a version from My experiments.' : 'There is no release you can see.'));
        if (isOwner(experiment)) aside.appendChild(link({view: 'mine', id: experiment.id}, 'Manage', {class: 'btn btn-line'}));
        return aside;
      }
      const m = version.manifest || {};
      const list = versions || [version];
      const head = h('div', {class: 'action-head'});
      if (list.length > 1) {
        const id = nextId('version');
        const pick = select(list.map((v) => [v.id, versionLabel(v) + (v.id === experiment.published_version_id ? ' (public)' : '')]), version.id, {id, 'data-focus': 'version-pick'});
        pick.addEventListener('change', () => go({view: 'experiment', id: experiment.id, version: pick.value, tab: ctx.route.tab}, {focus: 'version-pick'}));
        head.appendChild(h('div', {class: 'action-version'}, h('label', {for: id, class: 'action-key'}, 'Version'), pick));
      } else {
        head.appendChild(h('p', {class: 'action-version'}, h('span', {class: 'action-key'}, 'Version'),
          h('span', {class: 'mono action-version-value'}, versionLabel(version))));
      }
      head.appendChild(h('p', {class: 'action-date'}, 'Released ' + C.formatDate(version.created_at, false)));
      aside.appendChild(head);
      aside.appendChild(libraryAction(ctx, experiment, version));
      if (state.role === 'rig') aside.appendChild(installPanel(experiment, version));
      else aside.appendChild(downloadAction(experiment, version));
      const subjects = C.subjectKeys({experiment}).map((s) => s === 'human' ? 'Human' : 'Monkey').join(', ');
      aside.appendChild(spec([
        ['Licence', experiment.license || m.license || 'not stated'],
        ['Needs', hardwareLamps(m)],
        ['Subjects', subjects || null],
        ['Runs on', C.platformsText(m)],
        ['SHA-256', h('span', {class: 'mono', title: String(version.sha256 || '')}, C.shortHash(version.sha256) + '\u2026')],
      ]));
      if (state.role === 'server') {
        aside.appendChild(h('p', {class: 'sheet-variant'}, link({view: 'create', fork: experiment.id}, 'Start a variant with AI', {class: 'btn btn-quiet btn-small'})));
      }
      if (isOwner(experiment)) {
        aside.appendChild(h('p', {class: 'sheet-owner'}, link({view: 'mine', id: experiment.id}, 'Manage this experiment', {class: 'btn btn-quiet'})));
      }
      return aside;
    }

    function libraryAction(ctx, experiment, version) {
      const box = h('div', {class: 'sheet-block library-action'});
      if (!state.user) {
        box.appendChild(link({view: 'signin', next: currentNext()}, 'Add to library', {class: 'btn btn-primary btn-block'}));
        box.appendChild(h('p', {class: 'muted small'}, 'Sign in to add it to your library.'));
        return box;
      }
      const status = statusLine();
      const button = h('button', {type: 'button', class: 'btn btn-primary btn-block', 'data-focus': 'pin'}, 'Add to library');
      const pinned = h('div', {class: 'pin-state', hidden: true});
      box.append(pinned, button, status.el);
      const paint = (items) => {
        const entry = items.find((i) => i && i.experiment && i.experiment.id === experiment.id);
        const pinnedId = entry && entry.version ? entry.version.id : null;
        if (pinnedId === version.id) {
          pinned.replaceChildren(h('p', {class: 'owned'}, checkGlyph(), h('span', null, 'In your library')),
            h('p', {class: 'muted small'}, 'Pinned to ' + versionLabel(version) + '. Newer versions are not followed automatically.'),
            link({view: 'library'}, 'Open your library', {class: 'btn btn-line btn-block', 'data-focus': 'open-library'}));
          pinned.hidden = false;
          button.hidden = true;
        } else if (entry) {
          pinned.replaceChildren(h('p', {class: 'muted small'}, 'Your library pins ' + versionLabel(entry.version) + '.'));
          pinned.hidden = false;
          button.textContent = 'Pin ' + versionLabel(version) + ' instead';
          button.hidden = false;
        } else {
          pinned.hidden = true;
          button.textContent = 'Add to library';
          button.hidden = false;
        }
      };
      libraryItems(false).then((items) => { if (ctx.epoch === state.epoch) paint(items); })
        .catch((exc) => { if (ctx.epoch === state.epoch) { noteFailure(exc); pinned.hidden = false; pinned.textContent = 'Your library could not be read: ' + exc.message; } });
      button.addEventListener('click', async () => {
        button.disabled = true;
        status.show('Adding\u2026', 'info');
        try {
          await api('POST', '/library', {json: {experiment_id: experiment.id, version_id: version.id}});
          const items = await libraryItems(true);
          if (ctx.epoch !== state.epoch) return;
          paint(items);
          status.clear();
          const open = box.querySelector('[data-focus="open-library"]');
          if (open && typeof open.focus === 'function') open.focus();
        } catch (exc) {
          noteFailure(exc);
          status.show(exc.message, 'err');
        } finally {
          button.disabled = false;
        }
      });
      return box;
    }

    function downloadAction(experiment, version) {
      const href = state.api.url('/experiments/' + C.seg(experiment.id) + '/versions/' + C.seg(version.id) + '/download');
      return h('div', {class: 'sheet-block'},
        h('a', {class: 'btn btn-line btn-block', href, download: ''}, 'Download the release archive'),
        h('p', {class: 'muted small'}, 'The hub never runs experiments. Install it on a rig from your library '
          + '(alhazen dashboard --hub).'));
    }

    /** Trust and install a release on this rig (rig only). */
    function installPanel(experiment, version) {
      const box = h('div', {class: 'sheet-block install'});
      box.appendChild(h('h3', {class: 'sub-title'}, 'Install on this rig'));
      const local = state.local || {};
      if (local.state === 'not_configured') {
        box.append(h('p', {class: 'muted'}, 'This rig is not connected to a hub.'),
          link({view: 'rig', tab: 'connection'}, 'Connect this rig', {class: 'btn btn-line'}));
        return box;
      }
      const existing = installFor(version.sha256);
      const status = statusLine();
      const result = h('div', {class: 'install-result'});
      const showInstalled = (record) => {
        result.replaceChildren();
        if (!record) return;
        const ok = record.status === 'registered' && !record.error;
        result.appendChild(h('p', {class: 'note note-' + (ok ? 'ok' : 'warn'), role: 'status'},
          ok ? 'Installed and registered in the workspace (' + versionLabel(record) + ').'
            : 'Files installed and verified, but not registered: ' + ((record.error && record.error.message) || 'choose an interpreter below.')));
        const durability = durabilityNote(record);
        if (durability) result.appendChild(durability);
        const open = workspaceLink(record, 'Open it in the workspace');
        if (open) result.appendChild(open);
      };
      showInstalled(existing);
      box.appendChild(result);
      if (existing && existing.status === 'registered' && !existing.error) return box;
      if (!local.user) {
        box.append(h('p', {class: 'muted'}, 'Sign in on this rig to install.'),
          link({view: 'signin', next: currentNext()}, 'Sign in', {class: 'btn btn-line'}));
        return box;
      }
      box.appendChild(h('div', {class: 'callout callout-warn'},
        h('p', {class: 'callout-title'}, 'This release is Python code from its author.'),
        /* The rig's own statement when it gives one (rig-contract: trust_statement). */
        h('p', null, local.trust_statement ? String(local.trust_statement)
          : 'Trusting it lets Alhazen import and run it as your operating-system user, with your access to files, '
          + 'collected data, saved credentials and connected devices. A virtual environment is not a sandbox. Install only code whose author you trust.'),
        h('p', null, 'Installing downloads the archive, checks its SHA-256 and every file against the manifest, and extracts it into a new folder. '
          + 'Existing experiments and checkouts are not changed, and nothing is installed into the interpreter.')));
      const listId = nextId('interpreters');
      const python = input({type: 'text', name: 'python', autocomplete: 'off', spellcheck: 'false', list: listId,
        placeholder: '/path/to/venv/bin/python', 'data-focus': 'install-python'});
      const datalist = h('datalist', {id: listId});
      for (const i of Array.isArray(local.interpreters) ? local.interpreters : []) {
        if (i && i.path) datalist.appendChild(h('option', {value: i.path}, i.label || i.path));
      }
      const trust = checkbox('I trust the code of ' + (experiment.title || 'this experiment') + ' ' + versionLabel(version)
        + ', SHA-256 ' + C.shortHash(version.sha256) + '\u2026, and choose to install it.');
      const button = h('button', {type: 'submit', class: 'btn btn-primary'}, existing ? 'Register with this interpreter' : 'Install ' + versionLabel(version));
      const form = h('form', {class: 'form'},
        field('Python interpreter', python,
          'The absolute path of the interpreter that already has alhazen; the experiment will run with it. '
          + (datalist.children.length ? 'Interpreters this rig knows are suggested.' : '')),
        datalist, trust.el, button, status.el);
      form.addEventListener('submit', async (event) => {
        prevent(event);
        const py = C.validatePython(python.value);
        if (!py.ok) return status.show(py.reason, 'err');
        if (!trust.box.checked) return status.show('Tick the box to confirm you trust this exact release.', 'err');
        button.disabled = true;
        status.show('Downloading, verifying and registering\u2026 keep this page open.', 'info');
        try {
          const answer = await api('POST', '/local/install', {
            json: {experiment_id: experiment.id, version_id: version.id, sha256: version.sha256, trust_code: true, python: py.value},
            timeoutMs: 15 * 60 * 1000,
          });
          await loadLocal();
          drawChrome();
          const record = (answer && answer.install) || installFor(version.sha256);
          status.clear();
          showInstalled(record);
          if (record && record.status === 'registered' && !record.error) form.hidden = true;
        } catch (exc) {
          noteFailure(exc);
          await loadLocal();
          showInstalled(installFor(version.sha256));
          status.show(exc.message, 'err');
        } finally {
          button.disabled = false;
        }
      });
      box.appendChild(form);
      return box;
    }

    function versionsTab(r, experiment, versions, current) {
      const wrap = h('div', {class: 'block'});
      if (!versions.length) {
        wrap.appendChild(emptyState('No versions you can see.', null));
        return wrap;
      }
      const table = h('table', {class: 'table'},
        h('caption', {class: 'visually-hidden'}, 'Versions of ' + (experiment.title || 'this experiment')),
        h('thead', null, h('tr', null, ...['Version', 'Status', 'Released', 'Size', 'SHA-256'].map((t) => h('th', {scope: 'col'}, t)))));
      const tbody = h('tbody');
      for (const v of versions) {
        const published = v.id === experiment.published_version_id;
        tbody.appendChild(h('tr', {class: v === current ? 'row-current' : null},
          h('td', {class: 'mono'}, link({view: 'experiment', id: experiment.id, version: v.id}, versionLabel(v))),
          h('td', null, published ? 'Public' : 'Private'),
          h('td', null, C.formatDate(v.created_at)),
          h('td', {class: 'mono num'}, C.formatBytes(v.size)),
          h('td', {class: 'mono', title: String(v.sha256 || '')}, C.shortHash(v.sha256))));
      }
      table.appendChild(tbody);
      wrap.appendChild(h('div', {class: 'table-wrap'}, table));
      wrap.appendChild(h('p', {class: 'muted small'}, 'A release never changes after upload. Publishing one release does not publish later ones.'));
      return wrap;
    }

    /* ---- scientific documentation (renderer: hub_docs.js) ------------------- */

    /** The documentation renderer, or null when hub_docs.js did not load; the
     *  page then says so rather than drawing a half-view. */
    function docsRenderer() {
      const docs = env.HubDocs || (env.window && env.window.HubDocs) || null;
      return docs && typeof docs.renderMethods === 'function' ? docs : null;
    }

    function docsOptions(route) {
      return {
        document: doc,
        headingLevel: 2,
        idPrefix: 'hd-',
        taskHref: route.view === 'experiment'
          ? (taskId) => (loc.pathname || '/') + C.formatRoute({view: 'experiment', id: route.id, version: route.version, tab: 'tasks', task: taskId})
          : () => null,
      };
    }

    function renderDocs(kind, ...args) {
      const docs = docsRenderer();
      if (!docs) {
        return h('div', {class: 'callout callout-warn', role: 'status'},
          h('p', null, 'The documentation viewer (hub_docs.js) is not available on this server, so this section cannot be shown.'));
      }
      try {
        return docs[kind](...args);
      } catch (exc) {
        return errorBox(new C.HubError('bad_response', 'This documentation cannot be displayed: ' + exc.message));
      }
    }

    function documentationTab(ctx, tab, experiment, version) {
      const r = ctx.route;
      const wrap = h('div', {class: 'block docs-block'});
      if (!version) {
        wrap.appendChild(renderDocs('renderMissing', tab === 'methods' ? 'methods' : 'task', docsOptions(r)));
        return wrap;
      }
      wrap.appendChild(h('p', {class: 'docs-provenance mono'},
        'Documentation of ' + versionLabel(version) + ' \u00b7 SHA-256 ' + C.shortHash(version.sha256) + '\u2026'));
      const area = region('the documentation');
      wrap.appendChild(area.el);
      (async () => {
        const got = await screenRequest(ctx.epoch, 'GET',
          '/experiments/' + C.seg(experiment.id) + '/versions/' + C.seg(version.id) + '/documentation');
        if (!got) return;
        if (!got.ok) return area.fail(got.error, retryCurrent);
        const documentation = got.value && got.value.documentation ? got.value.documentation : null;
        const options = docsOptions(r);
        if (tab === 'methods') return area.fill(renderDocs('renderMethods', documentation, options));
        const tasks = documentation && Array.isArray(documentation.tasks) ? documentation.tasks : [];
        if (!tasks.length) return area.fill(renderDocs('renderTaskGuide', null, null, options));
        const chosen = tasks.find((t) => t.id === r.task) || tasks[0];
        const layout = h('div', {class: 'docs-tasks'});
        if (tasks.length > 1) {
          const nav = h('nav', {class: 'task-index', 'aria-label': 'Tasks'});
          const list = h('ul', {class: 'task-list'});
          for (const t of tasks) {
            list.appendChild(h('li', null, link({view: 'experiment', id: r.id, version: r.version, tab: 'tasks', task: t.id},
              t.title || t.id, {class: 'task-link', 'aria-current': t === chosen ? 'page' : null,
                'data-focus': 'task-' + t.id, 'data-focus-next': 'task-' + t.id})));
          }
          nav.appendChild(list);
          layout.appendChild(nav);
        }
        layout.appendChild(h('div', {class: 'task-guide'}, renderDocs('renderTaskGuide', documentation, chosen.id, options)));
        area.fill(layout);
      })();
      return wrap;
    }

    function screenGuide(ctx) {
      const section = screenShell('Guide', 'How Alhazen runs an experiment',
        'Modes, protections and what each choice records, taken from the Alhazen version this '
        + (state.role === 'rig' ? 'rig runs. Readable offline.' : 'hub runs.'));
      const area = region('the guide');
      section.appendChild(area.el);
      (async () => {
        const got = await screenRequest(ctx.epoch, 'GET', '/guide');
        if (!got) return;
        if (!got.ok) return area.fail(got.error, retryCurrent);
        const guide = got.value && got.value.guide ? got.value.guide : got.value;
        if (!guide) return area.fill(renderDocs('renderMissing', 'guide', docsOptions(ctx.route)));
        area.fill(renderDocs('renderGlobalGuide', guide, docsOptions(ctx.route)));
      })();
      return section;
    }

    /* ---- sign in, register ------------------------------------------------- */

    /** Sign-in and registration share one split card: the form on the left,
     *  on wide screens a quiet panel with what an account gives you. Returns
     *  {section, body}; screens append their form to body. */
    function authShell(title, lede) {
      const section = h('section', {class: 'screen screen-auth'});
      const body = h('div', {class: 'auth-main'},
        h('p', {class: 'auth-brand'}, brandMark(), h('span', {class: 'auth-brand-word'}, 'Alhazen')),
        h('header', {class: 'screen-head'},
          h('h1', {class: 'screen-title', tabindex: '-1', 'data-heading': ''}, title),
          lede ? h('p', {class: 'lede'}, lede) : null));
      const flash = flashNode();
      if (flash) body.appendChild(flash);
      const points = [
        ['Library', 'Pin the releases you trust.'],
        ['Rigs', 'Install them on any rig you sign in to.'],
        ['Data', 'Upload sessions privately, when you choose.'],
      ];
      const aside = h('aside', {class: 'auth-aside', 'aria-label': 'What an account gives you'},
        h('p', {class: 'auth-aside-title'}, 'One account for your experiments, rigs and data.'),
        h('ul', {class: 'auth-points'}, points.map(([name, text]) => h('li', {class: 'auth-point'},
          h('span', {class: 'auth-point-name'}, name), h('span', {class: 'auth-point-text'}, text)))));
      section.appendChild(h('div', {class: 'auth'}, body, aside));
      return {section, body};
    }

    /** The Alhazen mark (the Penrose "impossible A"): ink paths plus one
     *  accent face, coloured by CSS tokens. Built as SVG nodes. */
    const MARK_INK = 'M7.83 56.61L11.81 49.72L11.98 49.52L12.21 49.35L12.38 49.28L12.64 49.24L20.6 49.24L16.62 42.35L16.55 42.2L16.51 42.01L16.5 41.82L16.53 41.64L16.62 41.39L26.31 24.61L26.49 24.39L26.73 24.23L26.92 24.16L27.12 24.13L27.41 24.16L27.6 24.23L27.77 24.33L27.92 24.47L28.02 24.61L37.41 40.87L44.77 40.87L27.17 10.39L4.15 50.24ZM55.59 57.61L59.27 51.24L13.24 51.24L9.57 57.61ZM26.59 40.87L30.85 33.5L27.17 27.13L19.23 40.87Z';
    const MARK_FACE = 'M22.91 49.24L59.27 49.24L36.26 9.39L28.9 9.39L47.38 41.39L47.45 41.55L47.49 41.75L47.5 41.95L47.46 42.14L47.34 42.41L47.14 42.64L46.89 42.79L46.7 42.85L46.52 42.87L19.23 42.87Z';
    function brandMark() {
      const box = svg('svg', {viewBox: '0 0 64 64', width: '28', height: '28', class: 'brand-mark', focusable: 'false', 'aria-hidden': 'true'});
      box.appendChild(svg('path', {class: 'mark-ink', d: MARK_INK}));
      box.appendChild(svg('path', {class: 'mark-face', d: MARK_FACE}));
      return box;
    }

    /** A password field with a Show/Hide switch inside it. */
    function passwordField(label, pw, hint) {
      const id = pw.getAttribute('id');
      const toggle = h('button', {type: 'button', class: 'pw-toggle', 'aria-controls': id,
        'aria-pressed': 'false', 'aria-label': 'Show password'}, 'Show');
      toggle.addEventListener('click', () => {
        const reveal = pw.getAttribute('type') === 'password';
        pw.setAttribute('type', reveal ? 'text' : 'password');
        toggle.setAttribute('aria-pressed', String(reveal));
        toggle.textContent = reveal ? 'Hide' : 'Show';
      });
      const parts = [h('label', {class: 'field-label', for: id}, label), h('div', {class: 'pw'}, pw, toggle)];
      if (hint) {
        pw.setAttribute('aria-describedby', id + '-hint');
        parts.push(h('p', {class: 'field-hint', id: id + '-hint'}, hint));
      }
      return h('div', {class: 'field'}, ...parts);
    }

    function screenSignin(ctx) {
      const r = ctx.route;
      const flash = state.flash;
      const {section, body} = authShell('Sign in', state.role === 'rig'
        ? 'Use your account on the hub this rig is connected to.'
        : 'Welcome back. Use your Experiment Hub account.');
      if (state.user) {
        body.appendChild(h('p', {class: 'note note-info'}, 'You are signed in as ' + (state.user.display_name || state.user.username) + '.'));
        body.appendChild(link({view: 'home'}, 'Go to the start page', {class: 'btn btn-line'}));
        return section;
      }
      if (state.role === 'rig' && state.local && state.local.state === 'not_configured') {
        body.appendChild(h('div', {class: 'callout callout-info'},
          h('p', null, 'Connect this rig to a hub first; then sign in to it.'),
          link({view: 'rig', tab: 'connection'}, 'Connect this rig', {class: 'btn btn-primary'})));
        return section;
      }
      const username = input({type: 'text', name: 'username', autocomplete: 'username', autocapitalize: 'none',
        spellcheck: 'false', required: true, maxlength: '64', value: (flash && flash.username) || '', 'data-focus': 'signin-user'});
      const password = input({type: 'password', name: 'password', autocomplete: 'current-password', required: true, maxlength: '1024'});
      const status = statusLine();
      const button = h('button', {type: 'submit', class: 'btn btn-primary'}, 'Sign in');
      const form = h('form', {class: 'form form-narrow'}, field('Username', username), passwordField('Password', password), button, status.el,
        h('p', {class: 'auth-alt'}, 'No account? ', link({view: 'register'}, 'Register with an invite')));
      form.addEventListener('submit', async (event) => {
        prevent(event);
        if (!username.value.trim() || !password.value) return status.show('Enter your username and password.', 'err');
        button.disabled = true;
        status.show('Signing in\u2026', 'info');
        try {
          const answer = await api('POST', '/auth/login', {json: {username: username.value.trim(), password: password.value}});
          password.value = '';
          state.user = answer && answer.user ? answer.user : null;
          if (answer && answer.csrf_token) state.csrf = String(answer.csrf_token);
          state.library = null;
          if (state.role === 'rig') await loadLocal();
          if (!state.user) await loadMe();
          drawChrome();
          banner('');
          state.flash = {text: 'Signed in as ' + (state.user ? state.user.display_name || state.user.username : username.value) + '.', tone: 'ok'};
          const next = r.next ? C.parseRoute(r.next.slice(1)) : {view: 'home'};
          go(next, {replace: true});
        } catch (exc) {
          password.value = '';
          if (exc.kind === 'unauthorized') status.show('That username and password do not match an account.', 'err');
          else {
            noteFailure(exc);
            status.show(exc.message, 'err');
          }
          button.disabled = false;
        }
      });
      body.appendChild(form);
      return section;
    }

    function screenRegister() {
      const {section, body} = authShell('Create an account', state.role === 'rig'
        ? 'Create your account on the hub, then sign in on this rig.'
        : 'You need an invite code from the hub\u2019s operator.');
      if (state.user) {
        body.appendChild(h('p', {class: 'note note-info'}, 'You are already signed in as ' + (state.user.display_name || state.user.username) + '.'));
        return section;
      }
      if (state.role === 'rig') {
        body.appendChild(rigRegistration());
        return section;
      }
      const username = input({type: 'text', name: 'username', autocomplete: 'username', autocapitalize: 'none', spellcheck: 'false', required: true, maxlength: '64'});
      const display = input({type: 'text', name: 'display_name', autocomplete: 'name', required: true, maxlength: '120'});
      const password = input({type: 'password', name: 'password', autocomplete: 'new-password', required: true, maxlength: '1024'});
      const repeat = input({type: 'password', name: 'password_repeat', autocomplete: 'new-password', required: true, maxlength: '1024'});
      const invite = input({type: 'text', name: 'invite_code', autocomplete: 'off', spellcheck: 'false', required: true, maxlength: '200'});
      const status = statusLine();
      const button = h('button', {type: 'submit', class: 'btn btn-primary'}, 'Create account');
      const form = h('form', {class: 'form form-narrow'},
        field('Username', username, 'Shown publicly beside anything you publish.'),
        field('Display name', display),
        passwordField('Password', password, 'At least 12 characters.'),
        passwordField('Repeat the password', repeat),
        field('Invite code', invite),
        button, status.el,
        h('p', {class: 'auth-alt'}, 'Have an account? ', link({view: 'signin'}, 'Sign in')));
      form.addEventListener('submit', async (event) => {
        prevent(event);
        if (!username.value.trim() || !display.value.trim() || !invite.value.trim()) {
          return status.show('Fill in every field.', 'err');
        }
        const pw = C.validatePassword(password.value, repeat.value);
        if (!pw.ok) return status.show(pw.reason, 'err');
        button.disabled = true;
        status.show('Creating the account\u2026', 'info');
        try {
          const answer = await api('POST', '/auth/register', {json: {
            username: username.value.trim(), display_name: display.value.trim(),
            password: password.value, invite_code: invite.value.trim(),
          }});
          password.value = '';
          repeat.value = '';
          const name = answer && answer.user ? answer.user.username : username.value.trim();
          state.flash = {text: 'Account created for @' + name + '. Sign in to continue.', tone: 'ok', username: name};
          go({view: 'signin'}, {replace: true, focus: 'signin-user'});
        } catch (exc) {
          noteFailure(exc);
          status.show(exc.message, 'err');
          button.disabled = false;
        }
      });
      body.appendChild(form);
      return section;
    }

    /** Registration on a rig. An account is created on the hub's own page:
     *  the rig refuses to relay registration (409 register_on_hub) because
     *  it must not speak for the hub page's origin. So no form here, only a
     *  link to `<hub>/?view=register`, built from the rig's configured hub
     *  address after the same check the Connect form applies (never from an
     *  address in an error message). Without a usable address: connect first. */
    function rigRegistration() {
      const rig = state.config && state.config.rig ? state.config.rig : {};
      const configured = (state.local && state.local.base_url) || rig.base_url || '';
      const checked = configured && !(state.local && state.local.state === 'not_configured')
        ? C.validateHubUrl(configured) : {ok: false};
      if (!checked.ok) {
        return h('div', {class: 'callout callout-info'},
          h('p', {class: 'callout-title'}, 'Connect this rig first'),
          h('p', null, 'Accounts are created on the hub itself. Connect this rig to a hub, register on that hub\u2019s page, then sign in here.'),
          link({view: 'rig', tab: 'connection'}, 'Connect this rig', {class: 'btn btn-primary'}));
      }
      const href = checked.url + '/?view=register';
      return h('div', {class: 'callout callout-info'},
        h('p', {class: 'callout-title'}, 'Register on the hub'),
        h('p', null, 'Accounts are created on the hub\u2019s own page, not through this rig. Registration there needs an invite code from the hub\u2019s operator.'),
        h('div', {class: 'actions'},
          h('a', {class: 'btn btn-primary', href, target: '_blank', rel: 'noopener noreferrer'}, 'Register on ' + hostOf(checked.url)),
          h('span', {class: 'muted small mono'}, checked.url)),
        h('p', null, 'It opens in a new tab. When your account exists, come back and ',
          link({view: 'signin'}, 'sign in on this rig'), '.'));
    }

    /* ---- library -------------------------------------------------------- */

    function screenLibrary(ctx) {
      const section = h('section', {class: 'screen screen-library'});
      section.appendChild(h('header', {class: 'store-head store-head-plain'},
        h('div', {class: 'store-head-row'},
          h('div', null,
            h('h1', {class: 'screen-title', tabindex: '-1', 'data-heading': ''}, 'Your library'),
            h('p', {class: 'lede'}, 'Releases you added, each pinned to one version. Nothing upgrades on its own.')),
          link({view: 'catalog'}, 'Browse the marketplace', {class: 'btn btn-line'}))));
      const flash = flashNode();
      if (flash) section.appendChild(flash);
      const area = region('your library');
      section.appendChild(area.el);
      (async () => {
        let items;
        try {
          items = await libraryItems(true);
        } catch (exc) {
          if (ctx.epoch !== state.epoch) return;
          noteFailure(exc);
          return area.fail(exc, retryCurrent);
        }
        if (ctx.epoch !== state.epoch) return;
        if (!items.length) {
          return area.fill(emptyState('Your library is empty.',
            'Open an experiment in the marketplace and add the release you want. '
            + (state.role === 'rig' ? 'You can then install it on this rig.' : 'A rig signed in to this account can then install it.'),
            link({view: 'catalog'}, 'Browse the marketplace', {class: 'btn btn-primary'})));
        }
        area.fill(h('p', {class: 'store-count'}, `${items.length} experiment${items.length === 1 ? '' : 's'}`),
          h('div', {class: 'card-grid card-grid-library'}, items.map((item) => listing(item, libraryExtra(ctx, item), {library: true}))));
      })();
      return section;
    }

    /** A library card's own part: the pinned version (and a newer public
     *  one, when there is), Install or Download, and Remove. */
    function libraryExtra(ctx, item) {
      const experiment = item.experiment || {};
      const version = item.version || {};
      const panel = h('div', {class: 'card-library'});
      const pin = h('p', {class: 'card-pin'}, h('span', {class: 'card-pin-key'}, 'Pinned'),
        h('span', {class: 'mono'}, versionLabel(version)),
        h('span', {class: 'mono card-pin-hash', title: String(version.sha256 || '')}, 'SHA-256 ' + C.shortHash(version.sha256)));
      panel.appendChild(pin);
      const update = h('div', {class: 'card-update', hidden: true});
      panel.appendChild(update);
      const row = h('div', {class: 'listing-actions'});
      if (state.role === 'rig') {
        const record = installFor(version.sha256);
        const open = record && record.status === 'registered' && !record.error ? workspaceLink(record, 'Open in the workspace') : null;
        row.appendChild(open || link({view: 'experiment', id: experiment.id, version: version.id}, record ? 'Finish installing' : 'Install on this rig', {class: 'btn btn-primary btn-small'}));
      } else if (experiment.id && version.id) {
        row.appendChild(h('a', {class: 'btn btn-line btn-small', download: '',
          href: state.api.url('/experiments/' + C.seg(experiment.id) + '/versions/' + C.seg(version.id) + '/download')}, 'Download'));
      }
      row.appendChild(h('button', {type: 'button', class: 'btn btn-quiet btn-small', disabled: true,
        title: 'The hub cannot remove a library entry yet; it can only pin another version.'}, 'Remove'));
      panel.appendChild(row);
      /* A newer public release than the pinned one: offer it, never follow it. */
      if (experiment.id && version.id) {
        (async () => {
          const got = await screenRequest(ctx.epoch, 'GET', '/experiments/' + C.seg(experiment.id));
          if (!got || !got.ok) return;
          const detail = got.value || {};
          const published = (detail.experiment || {}).published_version_id;
          const newer = sortVersions(detail.versions).find((v) => v.id === published);
          if (!newer || newer.id === version.id) return;
          const status = statusLine();
          const button = h('button', {type: 'button', class: 'btn btn-line btn-small'}, 'Pin ' + versionLabel(newer));
          button.addEventListener('click', async () => {
            button.disabled = true;
            try {
              await api('POST', '/library', {json: {experiment_id: experiment.id, version_id: newer.id}});
              await libraryItems(true);
              if (ctx.epoch === state.epoch) retryCurrent();
            } catch (exc) {
              noteFailure(exc);
              status.show(exc.message, 'err');
              button.disabled = false;
            }
          });
          update.replaceChildren(h('span', null, versionLabel(newer) + ' is the public release.'), button, status.el);
          update.hidden = false;
        })();
      }
      return panel;
    }

    /* ---- create with AI (the next phase's flow; generation not connected) ---- */

    const EXAMPLE_PROMPTS = [
      'A two-interval contrast detection task for a Gabor at four eccentricities, with a QUEST+ threshold for each and fixation control.',
      'A saccade task where the target steps during the eye movement, to adapt saccade gain over 300 trials in a monkey, with juice for accurate landings.',
      'Ebbinghaus size matching by adjustment: large or small inducers, three gaps, keyboard responses, no eye tracker.',
    ];
    const PROVIDERS = [['anthropic', 'Anthropic'], ['openai', 'OpenAI'], ['google', 'Google'], ['openrouter', 'OpenRouter']];

    function createSteps(current) {
      const steps = [['describe', 'Describe'], ['plan', 'Review the plan'], ['draft', 'Save as a private draft']];
      const ol = h('ol', {class: 'steps', 'aria-label': 'Steps'});
      steps.forEach(([key, label], i) => {
        ol.appendChild(h('li', {class: 'step' + (key === current ? ' is-current' : ''), 'aria-current': key === current ? 'step' : null},
          h('span', {class: 'step-n'}, String(i + 1)), h('span', null, label)));
      });
      return ol;
    }

    function screenCreate(ctx) {
      const r = ctx.route;
      if (r.step === 'plan') return screenCreatePlan(ctx);
      const draft = state.createDraft;
      if (r.fork) draft.fork = r.fork;
      const section = h('section', {class: 'screen screen-create'});
      section.appendChild(h('header', {class: 'create-head'},
        h('h1', {class: 'screen-title', tabindex: '-1', 'data-heading': ''}, 'Create an experiment with AI'),
        h('p', {class: 'lede'}, 'Describe the experiment in plain language. A model drafts a plan you review, then a private draft under My experiments.'),
        createSteps('describe')));
      const flash = flashNode();
      if (flash) section.appendChild(flash);
      const promptId = nextId('prompt');
      const prompt = textarea({id: promptId, name: 'prompt', rows: '7', class: 'input textarea prompt-box', maxlength: '4000',
        placeholder: 'What should the experiment measure, in whom, with which stimuli and responses?', 'data-focus': 'create-prompt'}, draft.prompt);
      prompt.addEventListener('input', () => { draft.prompt = prompt.value; });
      const examples = h('ul', {class: 'prompt-examples', 'aria-label': 'Example descriptions'});
      for (const text of EXAMPLE_PROMPTS) {
        examples.appendChild(h('li', null, h('button', {type: 'button', class: 'example-chip', on: {click: () => {
          prompt.value = text;
          draft.prompt = text;
          prompt.focus();
        }}}, text)));
      }
      const describe = h('div', {class: 'create-card'},
        h('h2', {class: 'create-card-title'}, h('span', {class: 'create-n'}, '1'), h('span', null, 'Describe it')),
        h('label', {class: 'field-label', for: promptId}, 'Description'), prompt,
        h('p', {class: 'field-hint'}, 'Or start from an example:'), examples);
      /* Fork from a published release (or start empty). */
      const forkId = nextId('fork');
      const fork = select([['', 'Start from scratch']], '', {id: forkId, name: 'fork', 'data-focus': 'create-fork'});
      const forkCard = h('div', {class: 'fork-preview'});
      const paintFork = (items) => {
        const chosen = items.find((i) => i.experiment && i.experiment.id === draft.fork);
        forkCard.replaceChildren();
        if (!chosen) return;
        forkCard.append(h('span', {class: 'fork-art', 'aria-hidden': 'true'}, schematic(C.schematicKind(chosen.experiment.tags), chosen.experiment.id, 'strip')),
          h('span', {class: 'fork-text'}, h('span', {class: 'fork-title'}, chosen.experiment.title || 'Untitled experiment'),
            h('span', {class: 'fork-meta'}, [versionLabel(chosen.version), chosen.experiment.license].filter(Boolean).join(' \u00b7 ')
              + ' \u00b7 the draft keeps its licence and cites it')));
      };
      fork.addEventListener('change', () => { draft.fork = fork.value; paintFork(forkItems); });
      let forkItems = [];
      (async () => {
        const got = await catalogItems(ctx, '');
        if (!got || !got.ok) return;
        forkItems = got.value;
        for (const item of C.filterCatalog(forkItems, {sort: 'name'})) {
          const opt = h('option', {value: item.experiment.id}, (item.experiment.title || 'Untitled') + ' \u00b7 ' + versionLabel(item.version));
          if (item.experiment.id === draft.fork) opt.selected = true;
          fork.appendChild(opt);
        }
        fork.value = draft.fork || '';
        paintFork(forkItems);
      })();
      const forkBlock = h('div', {class: 'create-card'},
        h('h2', {class: 'create-card-title'}, h('span', {class: 'create-n'}, '2'), h('span', null, 'Fork from\u2026')),
        field('Starting point', fork, 'Optional. The plan then changes a published release instead of starting empty.'), forkCard);
      /* Provider and key: the key lives in this field only. */
      const providerId = nextId('provider');
      const provider = select(PROVIDERS, draft.provider, {id: providerId, name: 'provider'});
      provider.addEventListener('change', () => { draft.provider = provider.value; });
      const key = input({type: 'password', name: 'api-key', autocomplete: 'off', spellcheck: 'false', placeholder: 'Paste your API key', 'data-focus': 'create-key'});
      const keyBlock = h('div', {class: 'create-card'},
        h('h2', {class: 'create-card-title'}, h('span', {class: 'create-n'}, '3'), h('span', null, 'Model and key')),
        h('div', {class: 'create-pair'}, field('Provider', provider), field('API key', key, 'Your own key. It will be stored encrypted for your account and never shown again.')),
        h('p', {class: 'key-status'}, h('span', {class: 'lamp lamp-idle', 'aria-hidden': 'true'}),
          h('span', null, 'Not connected yet: this version neither saves nor sends a key.')));
      const status = statusLine();
      const generate = h('button', {type: 'submit', class: 'btn btn-primary'}, sparkGlyph(), h('span', null, 'Generate plan'));
      describe.classList.add('create-main');
      const form = h('form', {class: 'create-form'}, describe, h('div', {class: 'create-side'}, forkBlock, keyBlock),
        h('div', {class: 'create-actions'}, generate,
          link({view: 'create', step: 'plan', fork: draft.fork || undefined}, 'See an example plan', {class: 'btn btn-line'})),
        status.el);
      form.addEventListener('submit', (event) => {
        prevent(event);
        key.value = '';
        if (!prompt.value.trim()) return status.show('Describe the experiment first.', 'err');
        status.show('Generation is not connected yet, so nothing was sent and no plan was made. The key field has been cleared. '
          + '\u201cSee an example plan\u201d shows what a plan will look like.', 'info');
      });
      section.appendChild(form);
      return section;
    }

    /* A static example of a generated plan, so the screen can be judged. */
    const EXAMPLE_PLAN = {
      title: 'Contrast detection at four eccentricities',
      paradigm: 'Two-interval forced choice; a QUEST+ threshold per eccentricity, interleaved',
      subjects: 'Human',
      hardware: 'Display, eye tracker (fixation control)',
      stimuli: [
        ['Gabor target', 'Vertical, 4 c/deg, envelope \u03c3 0.3 deg, on the horizontal meridian'],
        ['Fixation point', '0.15 deg disc at the screen centre, gaze window 1.5 deg'],
        ['Interval cues', 'Two tones, 50 ms, mark the intervals'],
      ],
      measures: ['Contrast threshold at 75% correct, per eccentricity', 'Proportion correct per contrast', 'Response time', 'Fixation breaks per block'],
      parameters: [
        ['ecc_dva', '2, 4, 8, 12', 'deg', 'Target eccentricities'],
        ['sf_cpd', '4', 'c/deg', 'Carrier spatial frequency'],
        ['sigma_dva', '0.3', 'deg', 'Gaussian envelope'],
        ['interval_ms', '200', 'ms', 'Each stimulus interval'],
        ['isi_ms', '500', 'ms', 'Gap between intervals'],
        ['n_per_ecc', '80', 'trials', 'QUEST+ trials per eccentricity'],
        ['fix_window_dva', '1.5', 'deg', 'Gaze window radius'],
      ],
      timeline: [['Fixation', 500], ['Interval 1', 200], ['Gap', 500], ['Interval 2', 200], ['Response', 2000], ['ITI', 700]],
      tests: [
        ['Parameters', 'Bounds and the condition table (pytest)'],
        ['Simulate', '40 trials per eccentricity with a simulated observer; threshold recovered within 0.1 log units'],
        ['Timing', 'Frame counts for 200 ms at 60, 120 and 144 Hz'],
        ['Package', 'alhazen hub pack lists every file; no data folder'],
      ],
    };

    function screenCreatePlan(ctx) {
      const plan = EXAMPLE_PLAN;
      const draft = state.createDraft;
      if (ctx.route.fork) draft.fork = ctx.route.fork;
      const startingPoint = h('span', null, draft.fork ? 'Fork of a published release' : 'New experiment');
      if (draft.fork) {
        (async () => {
          const got = await catalogItems(ctx, '');
          if (!got || !got.ok) return;
          const item = got.value.find((i) => i.experiment && i.experiment.id === draft.fork);
          if (item) startingPoint.textContent = 'Fork of ' + (item.experiment.title || 'Untitled') + ' ' + versionLabel(item.version);
        })();
      }
      const section = h('section', {class: 'screen screen-create screen-plan'});
      section.appendChild(h('header', {class: 'create-head'},
        h('h1', {class: 'screen-title', tabindex: '-1', 'data-heading': ''}, 'Review the plan'),
        h('p', {class: 'lede'}, 'Check what the model proposes before anything is written. You can change the description and generate again.'),
        createSteps('plan')));
      section.appendChild(h('div', {class: 'callout callout-info plan-notice', role: 'note'},
        h('p', {class: 'callout-title'}, 'Example plan'),
        h('p', null, 'Generation is not connected yet, so this is a fixed example of the screen, not a plan made from your description.')));
      const stack = h('div', {class: 'plan-stack'});
      const card = (title, ...children) => h('section', {class: 'plan-card'}, h('h2', {class: 'plan-card-title'}, title), ...children);
      stack.appendChild(h('section', {class: 'plan-card plan-card-lead'},
        h('div', {class: 'plan-lead-art', 'aria-hidden': 'true'}, schematic('gabor', 'example-plan', 'wide')),
        h('div', null,
          h('p', {class: 'plan-kicker'}, 'Proposed experiment'),
          h('h2', {class: 'plan-title'}, plan.title),
          spec([['Paradigm', plan.paradigm], ['Subjects', plan.subjects], ['Hardware', plan.hardware],
            ['Starting point', startingPoint]]))));
      stack.appendChild(h('div', {class: 'plan-pair'},
        card('Stimuli', h('ul', {class: 'plan-list'},
          ...plan.stimuli.map(([name, text]) => h('li', null, h('span', {class: 'plan-term'}, name), h('span', null, text))))),
        card('Measures', h('ul', {class: 'plan-list plan-list-plain'}, ...plan.measures.map((m) => h('li', null, m))))));
      const table = h('table', {class: 'table'},
        h('caption', {class: 'visually-hidden'}, 'Parameters'),
        h('thead', null, h('tr', null, ...['Parameter', 'Default', 'Unit', 'Meaning'].map((t) => h('th', {scope: 'col'}, t)))),
        h('tbody', null, ...plan.parameters.map(([n, v, u, m]) => h('tr', null,
          h('td', {class: 'mono'}, n), h('td', {class: 'mono num'}, v), h('td', null, u), h('td', null, m)))));
      stack.appendChild(card('Parameters', h('div', {class: 'table-wrap'}, table)));
      const total = plan.timeline.reduce((n, [, ms]) => n + ms, 0);
      const bar = h('ol', {class: 'timeline', 'aria-label': 'One trial, ' + total + ' ms at most'});
      for (const [name, ms] of plan.timeline) {
        const seg = h('li', {class: 'timeline-seg' + (/Interval/.test(name) ? ' is-stim' : '')},
          h('span', {class: 'timeline-name'}, name), h('span', {class: 'timeline-ms mono'}, (name === 'Response' ? '\u2264 ' : '') + ms + ' ms'));
        seg.dataset.span = String(Math.max(1, Math.round((ms / total) * 24)));
        bar.appendChild(seg);
      }
      stack.appendChild(card('Trial timeline', bar));
      stack.appendChild(card('Tests to run before use', h('ul', {class: 'plan-tests'},
        ...plan.tests.map(([name, text]) => h('li', null, h('span', {class: 'plan-test-state'}, 'Not run'),
          h('span', {class: 'plan-term'}, name), h('span', null, text))))));
      stack.appendChild(h('section', {class: 'plan-card plan-card-dest'},
        h('h2', {class: 'plan-card-title'}, 'Where it goes'),
        h('p', null, 'A private draft under My experiments: ', h('strong', null, plan.title), ', version 0.1.0. Only you see it until you publish a release.'),
        h('div', {class: 'actions'},
          h('button', {type: 'button', class: 'btn btn-primary', disabled: true}, 'Save as private draft'),
          link({view: 'create', fork: draft.fork || undefined}, 'Edit the description', {class: 'btn btn-line'}),
          link({view: 'mine'}, 'My experiments', {class: 'btn btn-quiet'}))));
      section.appendChild(stack);
      return section;
    }

    /* ---- my experiments -------------------------------------------------- */

    function screenMine(ctx) {
      const r = ctx.route;
      if (r.new) return screenMineNew();
      if (r.id) return screenMineOne(ctx);
      const section = screenShell('Author', 'My experiments',
        'Private until you publish a release.');
      section.appendChild(h('div', {class: 'actions'}, link({view: 'mine', new: '1'}, 'New experiment', {class: 'btn btn-primary'})));
      const area = region('your experiments');
      section.appendChild(area.el);
      (async () => {
        const got = await screenRequest(ctx.epoch, 'GET', '/experiments', {query: {limit: 100}});
        if (!got) return;
        if (!got.ok) return area.fail(got.error, retryCurrent);
        const items = (got.value && got.value.items) || [];
        if (!items.length) {
          return area.fill(emptyState('You have no experiments on this hub yet.',
            'Create one, then add a version: '
            + (state.role === 'rig' ? 'package a project registered on this rig.' : 'upload a release archive, or package one from a rig.')));
        }
        const table = h('table', {class: 'table'},
          h('caption', {class: 'visually-hidden'}, 'Your experiments'),
          h('thead', null, h('tr', null, ...['Experiment', 'Listing', 'Created'].map((t) => h('th', {scope: 'col'}, t)))));
        const tbody = h('tbody');
        for (const item of items) {
          const e = item.experiment || item;
          tbody.appendChild(h('tr', null,
            h('td', null, link({view: 'mine', id: e.id}, e.title || 'Untitled')),
            h('td', null, e.published_version_id ? 'Public' : 'Private'),
            h('td', null, C.formatDate(e.created_at, false))));
        }
        table.appendChild(tbody);
        area.fill(h('div', {class: 'table-wrap'}, table));
      })();
      return section;
    }

    /** The metadata fields an author edits (new and edit share them). */
    function metadataFields(experiment) {
      const e = experiment || {};
      const f = {
        title: input({type: 'text', name: 'title', required: true, maxlength: '160', value: e.title || ''}),
        summary: input({type: 'text', name: 'summary', maxlength: '300', value: e.summary || ''}),
        description: textarea({name: 'description', rows: '6', maxlength: '20000'}, e.description || ''),
        license: input({type: 'text', name: 'license', maxlength: '120', value: e.license || '', placeholder: 'MIT, CC-BY-4.0, \u2026'}),
        citations: textarea({name: 'citations', rows: '3'}, Array.isArray(e.citations) ? e.citations.join('\n') : ''),
        tags: input({type: 'text', name: 'tags', maxlength: '400', value: Array.isArray(e.tags) ? e.tags.join(', ') : ''}),
      };
      const nodes = [
        field('Title', f.title),
        field('Summary', f.summary, 'One sentence for the catalogue.'),
        field('Description', f.description, 'Plain text; blank lines separate paragraphs. Scientific Methods belong in the package\u2019s documentation.'),
        field('Licence', f.license, 'Needed before publishing.'),
        field('Citations', f.citations, 'One per line. Addresses starting with https:// get a link.'),
        field('Tags', f.tags, 'Comma-separated.'),
      ];
      const read = () => ({
        title: f.title.value.trim(), summary: f.summary.value.trim(), description: f.description.value,
        license: f.license.value.trim(), citations: C.parseList(f.citations.value), tags: C.parseTags(f.tags.value),
      });
      return {nodes, read};
    }

    function screenMineNew() {
      const section = screenShell('Author', 'New experiment', 'It stays private. You add versions and choose what to publish later.');
      const meta = metadataFields(null);
      const status = statusLine();
      const button = h('button', {type: 'submit', class: 'btn btn-primary'}, 'Create experiment');
      const form = h('form', {class: 'form'}, ...meta.nodes, h('div', {class: 'actions'}, button,
        link({view: 'mine'}, 'Cancel', {class: 'btn btn-quiet'})), status.el);
      form.addEventListener('submit', async (event) => {
        prevent(event);
        const body = meta.read();
        if (!body.title) return status.show('Give the experiment a title.', 'err');
        button.disabled = true;
        status.show('Creating\u2026', 'info');
        try {
          const answer = await api('POST', '/experiments', {json: body});
          const created = answer && answer.experiment;
          if (!created || !created.id) throw new C.HubError('bad_response', 'The hub did not return the new experiment.');
          state.flash = {text: 'Created. Add a first version below.', tone: 'ok'};
          go({view: 'mine', id: created.id}, {replace: true});
        } catch (exc) {
          noteFailure(exc);
          status.show(exc.message, 'err');
          button.disabled = false;
        }
      });
      section.appendChild(form);
      return section;
    }

    function screenMineOne(ctx) {
      const r = ctx.route;
      const section = h('section', {class: 'screen screen-mine'});
      const flash = flashNode();
      const area = region('the experiment');
      if (flash) section.appendChild(flash);
      section.appendChild(area.el);
      (async () => {
        const got = await screenRequest(ctx.epoch, 'GET', '/experiments/' + C.seg(r.id));
        if (!got) return;
        if (!got.ok) return area.fail(got.error, retryCurrent);
        const experiment = (got.value && got.value.experiment) || {};
        const versions = sortVersions(got.value && got.value.versions);
        if (!isOwner(experiment)) {
          return area.fill(h('div', {class: 'screen-head'},
            h('h1', {class: 'screen-title', tabindex: '-1', 'data-heading': ''}, experiment.title || 'Experiment'),
            h('p', {class: 'lede'}, 'Only its author can manage this experiment.'),
            link({view: 'experiment', id: r.id}, 'Open its page', {class: 'btn btn-line'})));
        }
        area.fill(
          h('header', {class: 'screen-head'},
            h('p', {class: 'eyebrow mono'}, link({view: 'mine'}, 'My experiments'), h('span', {'aria-hidden': 'true'}, ' / '), h('span', null, 'manage')),
            h('h1', {class: 'screen-title', tabindex: '-1', 'data-heading': ''}, experiment.title || 'Untitled experiment'),
            h('div', {class: 'chips'},
              experiment.published_version_id ? h('span', {class: 'chip chip-public'}, 'Public: ' + versionLabel(versions.find((v) => v.id === experiment.published_version_id)))
                : h('span', {class: 'chip chip-private'}, 'Private'),
              link({view: 'experiment', id: experiment.id}, 'See its page', {class: 'chip chip-link'}))),
          h('div', {class: 'mine-grid'},
            h('div', {class: 'mine-main'},
              mineVersions(experiment, versions),
              state.role === 'rig' ? packagePanel(ctx, experiment) : zipPanel(experiment),
              publishPanel(experiment, versions)),
            h('div', {class: 'mine-side'}, editPanel(experiment))));
      })();
      return section;
    }

    function reloadMine(id, text) {
      state.flash = {text, tone: 'ok'};
      go({view: 'mine', id}, {replace: true});
    }

    function mineVersions(experiment, versions) {
      const block = h('section', {class: 'panel'}, h('h2', {class: 'panel-title'}, 'Versions'));
      if (!versions.length) {
        block.appendChild(h('p', {class: 'muted'}, 'No versions yet. A version is an immutable archive of the experiment\u2019s source files.'));
        return block;
      }
      block.appendChild(versionsTab({id: experiment.id}, experiment, versions, null));
      return block;
    }

    function zipPanel(experiment) {
      const block = h('section', {class: 'panel'}, h('h2', {class: 'panel-title'}, 'Add a version'),
        h('p', {class: 'muted'}, 'Upload a release archive made with Alhazen (alhazen-package.json inside). '
          + 'Its version comes from the manifest and cannot be replaced later. It stays private.'));
      const file = input({type: 'file', name: 'archive', accept: '.zip,application/zip'});
      const status = statusLine();
      const button = h('button', {type: 'submit', class: 'btn btn-primary'}, 'Upload version');
      const form = h('form', {class: 'form'}, field('Release archive (.zip)', file, 'Up to 256 MiB.'), button, status.el);
      form.addEventListener('submit', async (event) => {
        prevent(event);
        const chosen = file.files && file.files[0];
        if (!chosen) return status.show('Choose a .zip file first.', 'err');
        if (chosen.size > MAX_PACKAGE_BYTES) {
          return status.show('That archive is ' + C.formatBytes(chosen.size) + '; the hub accepts at most ' + C.formatBytes(MAX_PACKAGE_BYTES) + '.', 'err');
        }
        button.disabled = true;
        status.show('Uploading ' + C.formatBytes(chosen.size) + '\u2026 keep this page open.', 'info');
        try {
          const answer = await api('POST', '/experiments/' + C.seg(experiment.id) + '/versions',
            {body: chosen, contentType: 'application/zip', timeoutMs: 30 * 60 * 1000});
          const v = answer && answer.version;
          reloadMine(experiment.id, 'Uploaded ' + versionLabel(v) + ' (private). The hub checked every file against its manifest.');
        } catch (exc) {
          noteFailure(exc);
          status.show(exc.message, 'err');
          button.disabled = false;
        }
      });
      block.appendChild(form);
      return block;
    }

    /** Package a project registered on this rig as a new private version:
     *  pick the project, review the exact file list and metadata, confirm. */
    function packagePanel(ctx, experiment) {
      const block = h('section', {class: 'panel'}, h('h2', {class: 'panel-title'}, 'Add a version from this rig'),
        h('p', {class: 'muted'}, 'Pack a project registered in this rig\u2019s workspace. You see every file before anything leaves the rig; data folders, environments and rig files are left out.'));
      const area = h('div', {class: 'package'});
      block.appendChild(area);
      const status = statusLine();
      if (!state.local || !state.local.user) {
        area.appendChild(h('p', {class: 'muted'}, 'Sign in on this rig to package a project.'));
        return block;
      }
      area.appendChild(h('p', {class: 'loading'}, 'Loading the workspace\u2019s projects\u2026'));
      (async () => {
        let projects;
        try {
          const answer = await api('GET', '/local/projects');
          projects = ((answer && answer.items) || []).filter((p) => p && !p.archived);
        } catch (exc) {
          if (ctx.epoch !== state.epoch) return;
          noteFailure(exc);
          return area.replaceChildren(errorBox(exc));
        }
        if (ctx.epoch !== state.epoch) return;
        if (!projects.length) {
          return area.replaceChildren(h('p', {class: 'muted'}, 'No project is registered in this rig\u2019s workspace.'),
            h('a', {class: 'btn btn-line', href: '/'}, 'Open the workspace'));
        }
        const choice = select([['', 'Choose a project\u2026'], ...projects.map((p) => [p.id, (p.title || p.slug || p.id) + (p.version ? ' (' + p.version + ')' : '')])], '');
        const previewButton = h('button', {type: 'button', class: 'btn btn-line'}, 'Review files');
        const review = h('div', {class: 'package-review'});
        previewButton.addEventListener('click', async () => {
          if (!choice.value) return status.show('Choose a project.', 'err');
          previewButton.disabled = true;
          status.show('Listing the files that would be packed\u2026', 'info');
          try {
            const preview = await api('POST', '/local/package-preview', {json: {project_id: choice.value}, timeoutMs: 120000});
            status.clear();
            review.replaceChildren(packageReview(experiment, choice.value, preview, status));
          } catch (exc) {
            noteFailure(exc);
            status.show(exc.message, 'err');
          } finally {
            previewButton.disabled = false;
          }
        });
        area.replaceChildren(h('div', {class: 'inline-form'}, field('Project', choice), previewButton), review, status.el);
      })();
      return block;
    }

    function packageReview(experiment, projectId, preview, status) {
      const files = Array.isArray(preview && preview.files) ? preview.files : [];
      const meta = Object.assign({}, (preview && preview.metadata) || {});
      if (!meta.title) meta.title = experiment.title || '';
      if (!meta.license) meta.license = experiment.license || '';
      if (!meta.description) meta.description = experiment.summary || '';
      if (!Array.isArray(meta.citations) || !meta.citations.length) meta.citations = experiment.citations || [];
      const wrap = h('div', {class: 'review'});
      const boxes = [];
      const totals = h('p', {class: 'mono small'});
      const count = () => {
        let n = 0;
        let bytes = 0;
        boxes.forEach(([box, f]) => { if (box.checked) { n += 1; bytes += Number(f.size) || 0; } });
        totals.textContent = `${n} of ${files.length} files selected \u00b7 ${C.formatBytes(bytes)}`;
      };
      const list = h('ul', {class: 'file-list'});
      for (const f of files) {
        const c = checkbox(f.path, {checked: true});
        c.box.addEventListener('change', count);
        boxes.push([c.box, f]);
        list.appendChild(h('li', {class: 'file-row'}, c.el, h('span', {class: 'mono small file-size'}, C.formatBytes(f.size))));
      }
      count();
      wrap.appendChild(h('h3', {class: 'sub-title'}, 'Files to pack'));
      wrap.appendChild(totals);
      wrap.appendChild(h('div', {class: 'file-scroll'}, list));
      const excluded = Array.isArray(preview && preview.excluded) ? preview.excluded : [];
      const excludedCount = Number(preview && preview.excluded_count) || excluded.length;
      if (excludedCount) {
        const det = h('details', {class: 'excluded'}, h('summary', null, `${excludedCount} files left out automatically`));
        const ul = h('ul', {class: 'file-list'});
        for (const x of excluded.slice(0, 500)) ul.appendChild(h('li', null, h('span', {class: 'mono small'}, x.path), h('span', {class: 'muted small'}, ' \u2014 ' + (x.reason || ''))));
        det.appendChild(ul);
        wrap.appendChild(det);
      }
      wrap.appendChild(h('h3', {class: 'sub-title'}, 'Manifest'));
      const f = {
        name: input({type: 'text', value: meta.name || '', maxlength: '64', spellcheck: 'false'}),
        version: input({type: 'text', value: meta.version || '', maxlength: '32', spellcheck: 'false'}),
        title: input({type: 'text', value: meta.title || '', maxlength: '160'}),
        description: textarea({rows: '3'}, meta.description || ''),
        license: input({type: 'text', value: meta.license || '', maxlength: '120'}),
        citations: textarea({rows: '3'}, Array.isArray(meta.citations) ? meta.citations.join('\n') : ''),
      };
      const hw = meta.hardware || {};
      const hwBoxes = {
        display: checkbox('Needs a stimulus display', {checked: Boolean(hw.display)}),
        eye_tracker: checkbox('Needs an eye tracker', {checked: Boolean(hw.eye_tracker)}),
        reward: checkbox('Needs a reward line', {checked: Boolean(hw.reward)}),
      };
      const errors = {};
      const errorEl = (name) => { errors[name] = h('p', {class: 'field-error', hidden: true}); return errors[name]; };
      wrap.append(
        h('div', {class: 'field-pair'}, field('Package name', f.name, 'Lower-case slug.', errorEl('name')), field('Version', f.version, 'Like 1.0.0. Never reused.', errorEl('version'))),
        field('Title', f.title, null, errorEl('title')),
        field('Description', f.description),
        field('Licence', f.license, null, errorEl('license')),
        field('Citations', f.citations, 'One per line.'),
        h('fieldset', {class: 'fieldset'}, h('legend', null, 'Hardware'), hwBoxes.display.el, hwBoxes.eye_tracker.el, hwBoxes.reward.el),
        spec([
          ['Entry point', meta.entrypoint, {mono: true}],
          ['Python', meta.python_min ? '\u2265 ' + meta.python_min : null, {mono: true}],
          ['alhazen', meta.alhazen_min ? '\u2265 ' + meta.alhazen_min : null, {mono: true}],
          ['Runs on', C.platformsText(meta)],
        ]));
      const confirm = checkbox('I reviewed these files. They contain no participant data, credentials or rig-private settings. '
        + 'The automatic exclusions only catch known file patterns.');
      const button = h('button', {type: 'submit', class: 'btn btn-primary'}, 'Pack and upload as a private version');
      const form = h('form', {class: 'form'}, wrap, confirm.el, button);
      form.addEventListener('submit', async (event) => {
        prevent(event);
        const metadata = Object.assign({}, meta, {
          name: f.name.value.trim(), version: f.version.value.trim(), title: f.title.value.trim(),
          description: f.description.value, license: f.license.value.trim(), citations: C.parseList(f.citations.value),
          hardware: {display: hwBoxes.display.box.checked, eye_tracker: hwBoxes.eye_tracker.box.checked, reward: hwBoxes.reward.box.checked},
        });
        const check = C.validatePackageMetadata(metadata);
        for (const [name, el] of Object.entries(errors)) {
          el.textContent = check.errors[name] || '';
          el.hidden = !check.errors[name];
        }
        if (!check.ok) return status.show('Correct the manifest fields marked above.', 'err');
        const chosen = boxes.filter(([box]) => box.checked).map(([, file]) => file.path);
        if (!chosen.length) return status.show('Select at least one file.', 'err');
        if (!confirm.box.checked) return status.show('Confirm that you reviewed the files.', 'err');
        button.disabled = true;
        status.show('Packing ' + chosen.length + ' files and uploading\u2026 keep this page open.', 'info');
        try {
          const answer = await api('POST', '/local/package-upload', {
            json: {project_id: projectId, experiment_id: experiment.id, metadata, files: chosen, confirmed: true},
            timeoutMs: 30 * 60 * 1000,
          });
          reloadMine(experiment.id, 'Uploaded ' + versionLabel(answer && answer.version) + ' as a private version. Nothing was published.');
        } catch (exc) {
          noteFailure(exc);
          status.show(exc.message, 'err');
          button.disabled = false;
        }
      });
      return form;
    }

    function publishPanel(experiment, versions) {
      const block = h('section', {class: 'panel'}, h('h2', {class: 'panel-title'}, 'Publish'));
      const status = statusLine();
      const publishedId = experiment.published_version_id;
      if (publishedId) {
        const current = versions.find((v) => v.id === publishedId);
        const unpublish = h('button', {type: 'button', class: 'btn btn-line'}, 'Unpublish\u2026');
        const confirmRow = h('div', {class: 'confirm', hidden: true},
          h('p', null, 'Stop new downloads of ' + versionLabel(current) + '? Copies already on rigs are not affected.'));
        const yes = h('button', {type: 'button', class: 'btn btn-danger'}, 'Unpublish');
        const no = h('button', {type: 'button', class: 'btn btn-quiet'}, 'Keep it public');
        confirmRow.append(yes, no);
        unpublish.addEventListener('click', () => { confirmRow.hidden = false; unpublish.hidden = true; yes.focus(); });
        no.addEventListener('click', () => { confirmRow.hidden = true; unpublish.hidden = false; unpublish.focus(); });
        yes.addEventListener('click', async () => {
          yes.disabled = true;
          status.show('Unpublishing\u2026', 'info');
          try {
            await api('POST', '/experiments/' + C.seg(experiment.id) + '/unpublish', {json: {}});
            reloadMine(experiment.id, 'Unpublished. The catalogue no longer lists it and new downloads stop.');
          } catch (exc) {
            noteFailure(exc);
            status.show(exc.message, 'err');
            yes.disabled = false;
          }
        });
        block.append(h('p', null, 'Public release: ', h('span', {class: 'mono'}, versionLabel(current)),
          '. The listing shows the details as they were when you published it; edits stay private until you publish again.'),
        unpublish, confirmRow);
      }
      const candidates = versions.filter((v) => v.id !== publishedId);
      if (!candidates.length) {
        if (!publishedId) block.appendChild(h('p', {class: 'muted'}, 'Add a version before publishing.'));
        block.appendChild(status.el);
        return block;
      }
      const which = select(candidates.map((v) => [v.id, versionLabel(v) + ' \u00b7 ' + C.formatDate(v.created_at, false)]), candidates[0].id);
      const licence = experiment.license || '';
      const ackLicence = checkbox(licence ? 'Publish it under the licence \u201c' + licence + '\u201d.' : 'Publish it under the stated licence.');
      const ackData = checkbox('I checked this release\u2019s files: no participant data, credentials or rig-private configuration.');
      const go1 = h('button', {type: 'submit', class: 'btn btn-primary'}, publishedId ? 'Publish instead\u2026' : 'Publish\u2026');
      const confirmRow = h('div', {class: 'confirm', hidden: true});
      const yes = h('button', {type: 'button', class: 'btn btn-primary'}, 'Publish now');
      const no = h('button', {type: 'button', class: 'btn btn-quiet'}, 'Not yet');
      const confirmText = h('p');
      confirmRow.append(confirmText, yes, no);
      const form = h('form', {class: 'form'},
        h('p', {class: 'muted'}, 'Anyone can then read its listing and download it, and downloads cannot be recalled. Later versions stay private.'),
        field('Version to publish', which),
        licence ? null : h('p', {class: 'note note-warn'}, 'Set a licence in Details first.'),
        ackLicence.el, ackData.el, go1, confirmRow);
      form.addEventListener('submit', (event) => {
        prevent(event);
        if (!licence) return status.show('Set a licence in Details before publishing.', 'err');
        if (!ackLicence.box.checked || !ackData.box.checked) return status.show('Tick both confirmations.', 'err');
        const v = candidates.find((c) => c.id === which.value);
        confirmText.textContent = 'Publish ' + versionLabel(v) + ' (SHA-256 ' + C.shortHash(v && v.sha256) + '\u2026) publicly?';
        status.clear();
        confirmRow.hidden = false;
        go1.hidden = true;
        yes.focus();
      });
      no.addEventListener('click', () => { confirmRow.hidden = true; go1.hidden = false; go1.focus(); });
      yes.addEventListener('click', async () => {
        yes.disabled = true;
        status.show('Publishing\u2026', 'info');
        try {
          await api('POST', '/experiments/' + C.seg(experiment.id) + '/publish',
            {json: {version_id: which.value, license_ack: true, data_excluded_ack: true}});
          reloadMine(experiment.id, 'Published. It is listed in the catalogue now.');
        } catch (exc) {
          noteFailure(exc);
          status.show(exc.message, 'err');
          yes.disabled = false;
        }
      });
      block.append(form, status.el);
      return block;
    }

    function editPanel(experiment) {
      const block = h('section', {class: 'panel'}, h('h2', {class: 'panel-title'}, 'Details'));
      const meta = metadataFields(experiment);
      const status = statusLine();
      const button = h('button', {type: 'submit', class: 'btn btn-line'}, 'Save details');
      const form = h('form', {class: 'form'}, ...meta.nodes, button, status.el);
      form.addEventListener('submit', async (event) => {
        prevent(event);
        const body = meta.read();
        if (!body.title) return status.show('The title cannot be empty.', 'err');
        button.disabled = true;
        status.show('Saving\u2026', 'info');
        try {
          await api('PATCH', '/experiments/' + C.seg(experiment.id), {json: body});
          status.show(experiment.published_version_id
            ? 'Saved. The public listing keeps its published details until you publish again.' : 'Saved.', 'ok');
        } catch (exc) {
          noteFailure(exc);
          status.show(exc.message, 'err');
        } finally {
          button.disabled = false;
        }
      });
      block.appendChild(form);
      return block;
    }

    /* ---- data ---------------------------------------------------------------- */

    function sessionWhen(s) {
      const m = (s && s.metadata) || {};
      return C.formatDate(m.started_at || s.created_at);
    }

    function indexWords(s) {
      const st = C.indexState(s);
      return st.status ? st.word.toLowerCase() : '';
    }

    async function experimentNames() {
      /* Experiments a session may belong to: your own and your library's.
       * Only for labels and the filter; a failure leaves ids showing. */
      const names = new Map();
      try {
        const mine = await api('GET', '/experiments', {query: {limit: 100}});
        for (const item of (mine && mine.items) || []) {
          const e = item.experiment || item;
          if (e && e.id) names.set(e.id, e.title || e.id);
        }
      } catch (exc) { noteFailure(exc); }
      try {
        /* Fresh, not the cached copy: a release pinned elsewhere (another
         * tab, the rig) must appear in the filter. */
        for (const item of await libraryItems(true)) {
          if (item.experiment && item.experiment.id) names.set(item.experiment.id, item.experiment.title || item.experiment.id);
        }
      } catch (exc) { noteFailure(exc); }
      return names;
    }

    function screenData(ctx) {
      if (ctx.route.session) return screenSession(ctx);
      const r = ctx.route;
      const section = screenShell('Data', 'Your sessions',
        'Sessions you uploaded. Only you can see them.');
      const experimentChoice = select([['', 'All experiments']], r.experiment || '', {'data-focus': 'data-exp'});
      const subject = input({type: 'text', value: r.subject || '', maxlength: '200', autocomplete: 'off', spellcheck: 'false'});
      const mode = input({type: 'text', value: r.mode || '', maxlength: '200', autocomplete: 'off', list: 'data-modes'});
      const modes = h('datalist', {id: 'data-modes'}, ...['run', 'test', 'simulate', 'training'].map((m) => h('option', {value: m})));
      const apply = h('button', {type: 'submit', class: 'btn btn-primary', 'data-focus': 'data-apply'}, 'Filter');
      const form = h('form', {class: 'filters'}, field('Experiment', experimentChoice), field('Subject code', subject),
        field('Mode', mode), modes, h('div', {class: 'filters-actions'}, apply,
          (r.experiment || r.subject || r.mode) ? link({view: 'data'}, 'Clear', {class: 'btn btn-quiet', 'data-focus-next': 'data-apply'}) : null));
      form.addEventListener('submit', (event) => {
        prevent(event);
        go({view: 'data', experiment: experimentChoice.value || undefined, subject: subject.value, mode: mode.value}, {focus: 'data-apply'});
      });
      section.appendChild(form);
      const area = region('your sessions');
      section.appendChild(area.el);
      section.appendChild(unfinishedUploads(ctx));
      const offset = r.offset || 0;
      (async () => {
        const [got, names] = await Promise.all([
          screenRequest(ctx.epoch, 'GET', '/data/sessions', {query: {
            experiment_id: r.experiment, subject_code: r.subject, mode: r.mode, limit: PAGE, offset}}),
          experimentNames(),
        ]);
        if (!got || ctx.epoch !== state.epoch) return;
        /* The sessions themselves name their experiment (experiment_title is
         * the server's authorised label), so an experiment you collected
         * data for is offered even when it is neither yours nor pinned. */
        for (const item of (got.ok && got.value && got.value.items) || []) {
          if (item.experiment_id && !names.has(item.experiment_id)) names.set(item.experiment_id, item.experiment_title || item.experiment_id);
        }
        if (r.experiment && !names.has(r.experiment)) names.set(r.experiment, r.experiment);
        for (const [id, title] of names) {
          const opt = h('option', {value: id}, title);
          if (id === r.experiment) opt.selected = true;
          experimentChoice.appendChild(opt);
        }
        if (r.experiment) experimentChoice.value = r.experiment;
        if (!got.ok) return area.fail(got.error, retryCurrent);
        const items = (got.value && got.value.items) || [];
        if (!items.length) {
          const filtered = r.experiment || r.subject || r.mode;
          return area.fill(emptyState(filtered ? 'No session matches these filters.' : 'No sessions uploaded yet.',
            filtered ? null : 'Sessions are recorded on rigs and stay there. To upload one, open the hub page on the rig, choose This rig \u2192 Upload data, preview it and opt in.'));
        }
        const table = h('table', {class: 'table'},
          h('caption', {class: 'visually-hidden'}, 'Uploaded sessions'),
          h('thead', null, h('tr', null, ...['Started', 'Experiment', 'Subject', 'Mode', 'Rig', 'Status'].map((t) => h('th', {scope: 'col'}, t)))));
        const tbody = h('tbody');
        for (const s of items) {
          const m = s.metadata || {};
          tbody.appendChild(h('tr', null,
            h('td', null, link({view: 'data', experiment: r.experiment, subject: r.subject, mode: r.mode, offset: r.offset, session: s.id}, sessionWhen(s) || String(s.id))),
            h('td', null, s.experiment_title || names.get(s.experiment_id) || String(s.experiment_id || '')),
            h('td', {class: 'mono'}, m.subject_code || ''),
            h('td', null, m.mode || ''),
            h('td', null, m.rig_alias || ''),
            h('td', null, [s.status, indexWords(s)].filter(Boolean).join(' \u00b7 '))));
        }
        table.appendChild(tbody);
        area.fill(h('div', {class: 'table-wrap'}, table),
          pager(offset, items.length, got.value.next_offset,
            (o) => ({view: 'data', experiment: r.experiment, subject: r.subject, mode: r.mode, offset: o}), 'data'));
      })();
      return section;
    }

    /* ---- unfinished uploads ------------------------------------------------ */

    const UNFINISHED_PAGE = 20;

    /** The signed-in account's uploads the hub has not committed (GET
     *  /sessions: staging or sealing, caller-owned only). They hold quota
     *  until they finish, expire or are discarded. Discard (POST
     *  /sessions/{id}/abort) removes only the hub's partial copy; the list is
     *  always read again from the hub afterwards, never edited locally. */
    function unfinishedUploads(ctx) {
      const block = h('section', {class: 'block unfinished', 'aria-label': 'Unfinished uploads'});
      const area = region('unfinished uploads');
      const flash = h('p', {class: 'form-status', role: 'status', hidden: true, tabindex: '-1', 'data-focus': 'unfinished-status'});
      block.append(h('h2', {class: 'block-title'}, 'Unfinished uploads'), flash, area.el);
      const say = (text, tone) => {
        flash.className = 'form-status form-status-' + (tone || 'info');
        flash.setAttribute('role', tone === 'err' ? 'alert' : 'status');
        flash.textContent = text;
        flash.hidden = !text;
      };
      const load = async (focusKey) => {
        const got = await screenRequest(ctx.epoch, 'GET', '/sessions', {query: {limit: UNFINISHED_PAGE, offset: 0}});
        if (!got) return;
        if (!got.ok) return area.fail(got.error, () => { say(''); load(); });
        const items = ((got.value && got.value.items) || []).filter((u) => u && u.status !== 'committed');
        if (!items.length) {
          area.fill(h('p', {class: 'muted small'}, 'None. Every upload the hub started for this account has finished or been closed.'));
        } else {
          area.fill(h('p', {class: 'muted small'}, 'The hub keeps space reserved for each until it finishes, expires or is discarded.'),
            h('ul', {class: 'unfinished-list'}, items.map((u) => unfinishedRow(ctx, u, say, load))),
            got.value.next_offset !== null && got.value.next_offset !== undefined
              ? h('p', {class: 'muted small'}, `Showing the first ${items.length}. Discard or finish some to see the rest.`) : null);
        }
        if (focusKey) {
          state.pendingFocus = focusKey;
          settleFocus(true);
        }
      };
      load();
      return block;
    }

    function unfinishedRow(ctx, u, say, reload) {
      const st = C.uploadState(u);
      const m = u.metadata || {};
      const what = [m.subject_code ? 'Subject ' + m.subject_code : null, m.mode, m.rig_alias, C.formatDate(m.started_at || u.created_at)]
        .filter(Boolean).join(' \u00b7 ');
      const row = h('li', {class: 'unfinished-row'},
        h('div', {class: 'unfinished-main'},
          h('p', {class: 'unfinished-what'}, what || String(u.id)),
          h('p', {class: 'muted small'}, st.word + (st.received !== null ? ' \u00b7 ' + C.formatBytes(st.received) + ' of ' + C.formatBytes(st.total) + ' received' : '')
            + (u.file_count ? ' \u00b7 ' + u.file_count + ' files' : '')),
          u.error ? h('p', {class: 'note note-warn small'}, typeof u.error === 'object' ? String(u.error.message || u.error.code || '') : String(u.error)) : null,
          h('p', {class: 'mono small muted'}, 'Upload ' + u.id + (u.updated_at ? ' \u00b7 last change ' + C.formatDate(u.updated_at) : ''))));
      const actions = h('div', {class: 'unfinished-actions'});
      row.appendChild(actions);
      if (!st.canDiscard) {
        actions.appendChild(h('button', {type: 'button', class: 'btn btn-line btn-small', disabled: true,
          'aria-describedby': 'why-' + u.id}, 'Discard'));
        actions.appendChild(h('p', {class: 'muted small', id: 'why-' + u.id}, st.whyNot));
        return row;
      }
      const discard = h('button', {type: 'button', class: 'btn btn-line btn-small', 'data-focus': 'discard-' + u.id}, 'Discard\u2026');
      const confirmRow = h('div', {class: 'confirm', hidden: true},
        h('p', null, 'Discard this unfinished upload on the hub? Only the hub\u2019s partial copy is removed, which frees the space it reserves. '
          + 'The session\u2019s files on the rig stay exactly as they are, and sessions the hub has already received are never removed. '
          + 'To send it again later, start a new upload from the rig.'));
      const yes = h('button', {type: 'button', class: 'btn btn-danger'}, 'Discard the partial upload');
      const no = h('button', {type: 'button', class: 'btn btn-quiet'}, 'Keep it');
      confirmRow.append(yes, no);
      actions.append(discard, confirmRow);
      discard.addEventListener('click', () => { confirmRow.hidden = false; discard.hidden = true; yes.focus(); });
      no.addEventListener('click', () => { confirmRow.hidden = true; discard.hidden = false; discard.focus(); });
      yes.addEventListener('click', async () => {
        yes.disabled = true;
        no.disabled = true;
        yes.textContent = 'Discarding\u2026';
        try {
          const answer = await api('POST', '/sessions/' + C.seg(u.id) + '/abort', {json: {}});
          if (ctx.epoch !== state.epoch) return;
          const status = answer && (answer.status || (answer.session && answer.session.status));
          say(status === 'aborted' || status === 'expired'
            ? 'Discarded on the hub. Its reserved space is released; the rig\u2019s files are unchanged.'
            : 'The hub answered without confirming the discard (status ' + String(status || 'not given') + '). The list below is the hub\u2019s current state.',
          status === 'aborted' || status === 'expired' ? 'ok' : 'err');
          reload('unfinished-status');
        } catch (exc) {
          if (ctx.epoch !== state.epoch) return;
          noteFailure(exc);
          if (exc.status === 409 || exc.status === 410 || exc.kind === 'not_found') {
            /* It changed on the hub meanwhile (being sealed, committed,
             * expired or already closed): say so in its words and show the
             * hub's current list. */
            say(exc.message, 'err');
            reload('unfinished-status');
            return;
          }
          say('Not discarded: ' + exc.message, 'err');
          yes.disabled = false;
          no.disabled = false;
          yes.textContent = 'Discard the partial upload';
        }
      });
      return row;
    }

    function screenSession(ctx) {
      const r = ctx.route;
      const back = {view: 'data', experiment: r.experiment, subject: r.subject, mode: r.mode, offset: r.offset};
      const section = h('section', {class: 'screen screen-session'});
      const area = region('the session');
      section.appendChild(area.el);
      (async () => {
        const got = await screenRequest(ctx.epoch, 'GET', '/data/sessions/' + C.seg(r.session));
        if (!got) return;
        if (!got.ok) return area.fail(got.error, retryCurrent);
        const answer = got.value || {};
        const s = answer.session || answer;
        const m = s.metadata || {};
        const receipt = answer.receipt || s.receipt || null;
        const artifacts = answer.artifacts || s.artifacts || [];
        const sessionId = s.id || r.session;
        const base = '/data/sessions/' + C.seg(sessionId);
        const indexBox = h('div', {class: 'index-state'});
        const exportsBox = h('div', {class: 'sheet-block'});
        const trials = region('trials');
        area.fill(
          h('header', {class: 'screen-head'},
            h('p', {class: 'eyebrow mono'}, link(back, 'Your sessions'), h('span', {'aria-hidden': 'true'}, ' / '), h('span', null, String(sessionId))),
            h('h1', {class: 'screen-title', tabindex: '-1', 'data-heading': ''}, 'Session ' + (m.subject_code ? m.subject_code + ' \u00b7 ' : '') + (sessionWhen(s) || '')),
            h('p', {class: 'lede'}, 'Raw files as the rig recorded them, verified on arrival. Trial rows are derived from them and can be rebuilt.')),
          h('div', {class: 'exp-grid'},
            h('div', {class: 'exp-reading'},
              h('h2', {class: 'block-title'}, 'Trials'),
              indexBox,
              trials.el,
              h('h2', {class: 'block-title'}, 'Files'),
              artifactTable(base, artifacts)),
            h('aside', {class: 'sheet'},
              h('h2', {class: 'sheet-title'}, 'Session'),
              spec([
                ['Experiment', s.experiment_title || null],
                ['Status', s.status],
                ['Subject', m.subject_code, {mono: true}],
                ['Mode', m.mode],
                ['Rig', m.rig_alias],
                ['Started', C.formatDate(m.started_at)],
                ['Received', C.formatDate(s.completed_at)],
                ['Files', s.file_count !== undefined ? s.file_count + ' \u00b7 ' + C.formatBytes(s.total_bytes) : null, {mono: true}],
                ['Release', s.version_id, {mono: true}],
                ['Manifest SHA-256', digest(s.manifest_sha256 || (receipt && receipt.manifest_sha256))],
              ]),
              h('p', {class: 'muted small'}, receipt && receipt.durability
                ? 'Receipt: ' + receipt.durability + '. Keep the rig\u2019s originals.'
                : 'The receipt means the hub\u2019s primary storage verified every file. It is not a backup; keep the rig\u2019s originals.'),
              exportsBox)));
        showIndex(ctx, {s, base, sessionId, back, indexBox, exportsBox, trials, columns: answer.columns});
      })();
      return section;
    }

    /** The session's trial index, its exports and its rows, from the state
     *  the server reports. A failed index offers Rebuild (owner-only POST
     *  …/reindex, 202); queued and running rebuilds are polled until the
     *  server says indexed, failed or none. Nothing is shown as ready that
     *  the server has not said is ready; the files above stay downloadable. */
    function showIndex(ctx, view) {
      const {base, indexBox, exportsBox, trials} = view;
      const st = C.indexState(view.s);
      indexBox.replaceChildren();
      exportsBox.replaceChildren(h('h3', {class: 'sub-title'}, 'Export the trial table'));
      if (st.ready) {
        exportsBox.appendChild(h('div', {class: 'actions'},
          h('a', {class: 'btn btn-line', download: '', href: state.api.url(base + '/export', {format: 'csv'})}, 'CSV'),
          h('a', {class: 'btn btn-line', download: '', href: state.api.url(base + '/export', {format: 'json'})}, 'JSON')));
        if (st.rows !== null) indexBox.appendChild(h('p', {class: 'muted small'}, st.rows + ' trial rows indexed'));
        loadTrials(ctx, base, trials, view.back, view.sessionId, view.columns);
        return;
      }
      exportsBox.appendChild(h('p', {class: 'muted small'}, st.status === 'none'
        ? 'There is no trial table to export; download the original files instead.'
        : 'Exports become available once the trial index is ready. The original files can be downloaded now.'));
      if (st.status === 'none') {
        indexBox.appendChild(h('p', {class: 'muted'}, 'This session has no trial table the hub indexes. Its files are listed below.'));
        trials.fill();
        return;
      }
      if (st.busy) {
        indexBox.appendChild(h('p', {class: 'note note-info', role: 'status'}, h('span', {class: 'spinner', 'aria-hidden': 'true'}),
          st.word + '. Trial rows appear here when the hub finishes; this page checks again on its own.'));
        trials.fill();
        pollIndex(ctx, view, POLL_MS);
        return;
      }
      if (st.failed) {
        const status = statusLine();
        const button = h('button', {type: 'button', class: 'btn btn-primary', 'data-focus': 'reindex'}, 'Rebuild trial index');
        indexBox.appendChild(h('div', {class: 'callout callout-warn', role: 'alert'},
          h('p', {class: 'callout-title'}, 'The trial index could not be built.'),
          st.error ? h('p', null, st.error) : null,
          h('p', null, 'The raw files are kept and can be downloaded below. Rebuilding reads them again; it changes no file.'),
          h('div', {class: 'actions'}, button), status.el));
        button.addEventListener('click', async () => {
          button.disabled = true;
          status.show('Asking the hub to rebuild the index\u2026', 'info');
          try {
            const answer = await api('POST', base + '/reindex', {json: {}});
            if (ctx.epoch !== state.epoch) return;
            const fresh = (answer && answer.session) || null;
            view.s = fresh || Object.assign({}, view.s, {index: {status: 'pending', rows: 0, error: null}, index_status: 'pending'});
            if (answer && answer.columns) view.columns = answer.columns;
            showIndex(ctx, view);
          } catch (exc) {
            if (ctx.epoch !== state.epoch) return;
            noteFailure(exc);
            status.show('The rebuild was not started: ' + exc.message, 'err');
            button.disabled = false;
          }
        });
        trials.fill();
        return;
      }
      indexBox.appendChild(h('p', {class: 'muted'}, st.word + '.'));
      trials.fill();
    }

    function pollIndex(ctx, view, delay) {
      state.polls.push(timers.set(async () => {
        const got = await screenRequest(ctx.epoch, 'GET', '/data/sessions/' + C.seg(view.sessionId));
        if (!got) return;
        if (!got.ok) {
          view.indexBox.appendChild(h('p', {class: 'form-status form-status-err', role: 'alert'},
            'Checking the index failed: ' + got.error.message + ' Trying again.'));
          pollIndex(ctx, view, Math.min(POLL_MAX_MS, delay * 2));
          return;
        }
        const answer = got.value || {};
        const before = C.indexState(view.s).status;
        view.s = answer.session || answer;
        if (answer.columns) view.columns = answer.columns;
        const now = C.indexState(view.s);
        if (now.busy && now.status === before) {
          pollIndex(ctx, view, Math.min(POLL_MAX_MS, delay * 2));
          return;
        }
        showIndex(ctx, view);  // a new state (queued -> rebuilding -> indexed/failed/none) is drawn
      }, delay));
    }

    function artifactTable(base, artifacts) {
      if (!artifacts.length) return h('p', {class: 'muted'}, 'No file list was returned for this session.');
      const table = h('table', {class: 'table'},
        h('caption', {class: 'visually-hidden'}, 'Session files'),
        h('thead', null, h('tr', null, ...['File', 'Size', 'SHA-256'].map((t) => h('th', {scope: 'col'}, t)))));
      const tbody = h('tbody');
      for (const a of artifacts) {
        const path = typeof a === 'string' ? a : a.path;
        if (!path) continue;
        tbody.appendChild(h('tr', null,
          h('td', {class: 'mono'}, h('a', {href: state.api.url(base + '/files', {path}), download: ''}, path)),
          h('td', {class: 'mono num'}, a.size !== undefined ? C.formatBytes(a.size) : ''),
          h('td', {class: 'mono', title: String(a.sha256 || '')}, C.shortHash(a.sha256))));
      }
      table.appendChild(tbody);
      return h('div', {class: 'table-wrap'}, table);
    }

    async function loadTrials(ctx, base, trials, back, sessionId, serverColumns) {
      const r = ctx.route;
      const offset = r.toffset || 0;
      const got = await screenRequest(ctx.epoch, 'GET', base + '/trials', {query: {limit: PAGE, offset}});
      if (!got) return;
      if (!got.ok) return trials.fail(got.error, retryCurrent);
      const rows = (got.value && got.value.items) || [];
      const index = got.value && got.value.index ? C.indexState({index: got.value.index}) : null;
      if (index && !index.ready) {
        /* The index changed since the session was read (a rebuild started). */
        return trials.fill(h('p', {class: 'muted'}, index.word + (index.error ? ': ' + index.error : '') + '. Reload this page to see its current state.'));
      }
      if (!rows.length) {
        return trials.fill(h('p', {class: 'muted'}, offset ? 'No more trials.' : 'The trial index holds no rows for this session.'));
      }
      const declared = (got.value && Array.isArray(got.value.columns) && got.value.columns.length) ? got.value.columns
        : (Array.isArray(serverColumns) && serverColumns.length ? serverColumns : null);
      const table = trialTable(rows, declared);
      const single = [...new Set(rows.map((item) => C.trialRow(item).source).filter(Boolean))];
      trials.fill(single.length === 1 ? h('p', {class: 'muted small'}, 'From ', h('span', {class: 'mono'}, single[0])) : null,
        h('div', {class: 'table-wrap table-scroll', tabindex: '0', role: 'region', 'aria-label': 'Trial rows'}, table),
        pager(offset, rows.length, got.value.next_offset,
          (o) => Object.assign({}, back, {session: sessionId, toffset: o}), 'trials'));
    }

    /** A page of derived trial rows as a table. Each item is the server's
     *  {ordinal, source_path, values}: the declared columns are read from
     *  `values`; the row's position and (when a session has several trial
     *  tables) its source file are shown in their own columns. */
    function trialTable(items, declaredColumns) {
      const rows = items.map((item) => C.trialRow(item));
      const columns = declaredColumns ? declaredColumns.slice(0, 40).map(String) : C.trialColumns(rows.map((r) => r.values));
      const sources = new Set(rows.map((r) => r.source).filter(Boolean));
      const showSource = sources.size > 1;
      const head = [h('th', {scope: 'col', class: 'mono num', title: 'Row in the trial index'}, '#')];
      if (showSource) head.push(h('th', {scope: 'col', class: 'mono'}, 'source'));
      const table = h('table', {class: 'table table-dense'},
        h('caption', {class: 'visually-hidden'}, 'Trials' + (sources.size === 1 ? ' from ' + [...sources][0] : '')),
        h('thead', null, h('tr', null, ...head, ...columns.map((c) => h('th', {scope: 'col', class: 'mono'}, c)))));
      const tbody = h('tbody');
      for (const row of rows) {
        const cells = [h('td', {class: 'mono num muted'}, row.ordinal === null ? '' : String(row.ordinal + 1))];
        if (showSource) cells.push(h('td', {class: 'mono'}, row.source || ''));
        for (const c of columns) cells.push(h('td', {class: 'mono'}, C.cellText(row.values[c])));
        tbody.appendChild(h('tr', null, ...cells));
      }
      table.appendChild(tbody);
      return table;
    }

    /* ---- this rig ------------------------------------------------------------ */

    function screenRig(ctx) {
      const r = ctx.route;
      const tab = r.tab || (state.local && state.local.state === 'not_configured' ? 'connection' : (r.job || r.project ? 'upload' : 'connection'));
      const section = screenShell('This rig', 'This rig and the hub',
        'Connect to a hub, install releases you trust, and upload finished sessions when you choose. Running experiments happens in the workspace, as always.');
      const tabs = h('nav', {class: 'tabs', 'aria-label': 'Rig sections'});
      for (const [key, label] of [['connection', 'Connection'], ['installed', 'Installed'], ['upload', 'Upload data']]) {
        tabs.appendChild(link({view: 'rig', tab: key}, label,
          {class: 'tab', 'aria-current': key === tab ? 'page' : null, 'data-focus': 'rig-tab-' + key, 'data-focus-next': 'rig-tab-' + key}));
      }
      section.appendChild(tabs);
      if (state.local && state.local.run_active) {
        section.appendChild(h('p', {class: 'note note-warn', role: 'status'},
          'A session is running on this rig. Installing, packing and transfers wait until it ends so they cannot disturb its timing.'));
      }
      if (tab === 'installed') section.appendChild(rigInstalled());
      else if (tab === 'upload') section.appendChild(rigUpload(ctx));
      else section.appendChild(rigConnection(ctx));
      return section;
    }

    function rigConnection(ctx) {
      const local = state.local || {};
      const rig = (state.config && state.config.rig) || {};
      const block = h('section', {class: 'panel'});
      const linkState = rigLink();
      block.appendChild(h('h2', {class: 'panel-title'}, 'Hub connection'));
      block.appendChild(spec([
        ['Hub', h('span', null, h('span', {class: 'lamp lamp-' + (linkState.state === 'connected' ? 'ok' : linkState.state === 'unreachable' ? 'err' : 'idle'), 'aria-hidden': 'true'}),
          h('span', null, local.base_url || 'not connected'))],
        ['State', linkState.sentence],
        ['Operator', local.user ? (local.user.display_name || local.user.username) + ' (@' + local.user.username + ')' : 'nobody signed in'],
        ['Signed in until', local.user && local.expires_at ? C.formatDate(local.expires_at) : null],
        ['Hub said', state.config && state.config.server_error ? state.config.server_error.message : null],
      ]));
      const url = input({type: 'url', name: 'hub', value: local.base_url || '', autocomplete: 'url', spellcheck: 'false',
        placeholder: 'https://hub.example.org', 'data-focus': 'rig-url'});
      const status = statusLine();
      const button = h('button', {type: 'submit', class: 'btn btn-primary'}, local.base_url ? 'Save and check' : 'Connect');
      const form = h('form', {class: 'form form-narrow'},
        field('Hub address', url, 'HTTPS, or http:// for a hub on this computer during development. Changing it signs this rig out and pauses its transfers.'),
        button, status.el);
      form.addEventListener('submit', async (event) => {
        prevent(event);
        const check = C.validateHubUrl(url.value);
        if (!check.ok) return status.show(check.reason, 'err');
        button.disabled = true;
        status.show('Checking ' + check.url + '\u2026', 'info');
        try {
          state.local = await api('POST', '/local/connect', {json: check.loopbackHttp
            ? {url: check.url, allow_http_loopback: true} : {url: check.url}});
          await refreshRig();
          state.flash = {text: 'Connected to ' + check.url + '.' + (state.user ? '' : ' Sign in to use it.'), tone: 'ok'};
          go({view: 'rig', tab: 'connection'}, {replace: true});
        } catch (exc) {
          noteFailure(exc);
          status.show(exc.message, 'err');
          button.disabled = false;
        }
      });
      block.appendChild(form);
      if (local.base_url) {
        const out = h('button', {type: 'button', class: 'btn btn-quiet'}, 'Disconnect\u2026');
        const confirmRow = h('div', {class: 'confirm', hidden: true},
          h('p', null, 'Forget this hub? This rig signs out and its transfers pause. Installed experiments and all data on this rig stay.'));
        const yes = h('button', {type: 'button', class: 'btn btn-danger'}, 'Disconnect');
        const no = h('button', {type: 'button', class: 'btn btn-quiet'}, 'Cancel');
        confirmRow.append(yes, no);
        out.addEventListener('click', () => { confirmRow.hidden = false; out.hidden = true; yes.focus(); });
        no.addEventListener('click', () => { confirmRow.hidden = true; out.hidden = false; out.focus(); });
        yes.addEventListener('click', async () => {
          yes.disabled = true;
          try {
            state.local = await api('POST', '/local/disconnect', {json: {}});
            await refreshRig();
            state.flash = {text: 'Disconnected. Local experiments and data are unchanged.', tone: 'ok'};
            go({view: 'rig', tab: 'connection'}, {replace: true});
          } catch (exc) {
            noteFailure(exc);
            status.show(exc.message, 'err');
            yes.disabled = false;
          }
        });
        block.append(out, confirmRow);
      }
      if (local.base_url && !local.user) {
        block.appendChild(h('p', null, link({view: 'signin', next: currentNext()}, 'Sign in to ' + hostOf(local.base_url), {class: 'btn btn-line'})));
      }
      block.appendChild(h('p', {class: 'muted small'},
        'This page never holds a hub password or token. Signing in stores a time-limited credential in a private file of this rig\u2019s workspace.'));
      return block;
    }

    /** Config and status again after a connection change. */
    async function refreshRig() {
      try {
        state.config = await api('GET', '/config');
      } catch (exc) {
        noteFailure(exc);
      }
      await loadLocal();
      state.user = state.local && state.local.user ? state.local.user : null;
      state.library = null;
      drawChrome();
    }

    /** An install whose files the rig verified but could not confirm were
     *  flushed to disk (durable === false: the folder sync is unsupported on
     *  this system, e.g. Windows, or failed). A storage fact, not a verdict
     *  on the code. Null when durable or not reported. */
    function durabilityNote(record) {
      if (!record || record.durable !== false) return null;
      return h('p', {class: 'muted small durability'},
        'The files are installed and verified, but this computer could not confirm they were written through to disk'
        + (record.durability_note ? ' (' + String(record.durability_note) + ')' : '')
        + '. This is about storage, not about the code. After a power loss or crash, check the install again before running it.');
    }

    function rigInstalled() {
      const list = (state.local && Array.isArray(state.local.installed)) ? state.local.installed : [];
      const block = h('section', {class: 'panel'}, h('h2', {class: 'panel-title'}, 'Installed from the hub'));
      if (!list.length) {
        block.appendChild(emptyState('Nothing installed from the hub yet.',
          'Experiments you set up locally are still in the workspace. To add one from the hub, pin it in your library and install it.',
          link({view: 'library'}, 'Your library', {class: 'btn btn-line'}), h('a', {class: 'btn btn-quiet', href: '/'}, 'Open the workspace')));
        return block;
      }
      const table = h('table', {class: 'table'},
        h('caption', {class: 'visually-hidden'}, 'Installed releases'),
        h('thead', null, h('tr', null, ...['Experiment', 'Release', 'State', 'Installed', ''].map((t) => h('th', {scope: 'col'}, t)))));
      const tbody = h('tbody');
      for (const i of list) {
        let word = i.status === 'registered' ? 'Registered' : 'Files only (not registered)';
        if (i.intact === false) word = 'Files changed since install';
        if (i.error) word += ': ' + (i.error.message || i.error.code);
        tbody.appendChild(h('tr', null,
          h('td', null, link({view: 'experiment', id: i.experiment_id, version: i.version_id}, i.title || i.name || String(i.experiment_id))),
          h('td', {class: 'mono', title: String(i.sha256 || '')}, versionLabel(i) + ' \u00b7 ' + C.shortHash(i.sha256)),
          h('td', null, h('span', null, word), durabilityNote(i)),
          h('td', null, C.formatDate(i.installed_at)),
          h('td', null, workspaceLink(i, 'Open'))));
      }
      table.appendChild(tbody);
      block.appendChild(h('div', {class: 'table-wrap'}, table));
      return block;
    }

    /* Upload: project -> finished session -> preview with consent -> job. */
    function rigUpload(ctx) {
      const r = ctx.route;
      const local = state.local || {};
      const block = h('section', {class: 'panel'}, h('h2', {class: 'panel-title'}, 'Upload a finished session'));
      if (r.job) {
        block.appendChild(jobPanel(ctx, r.job));
        return block;
      }
      if (local.state === 'not_configured') {
        block.appendChild(h('p', null, link({view: 'rig', tab: 'connection'}, 'Connect this rig to a hub first', {class: 'btn btn-line'})));
        return block;
      }
      if (!local.user) {
        block.appendChild(h('p', null, link({view: 'signin', next: currentNext()}, 'Sign in to upload', {class: 'btn btn-line'})));
        return block;
      }
      block.appendChild(h('p', {class: 'muted'}, 'Nothing uploads by itself. Choose one session, review exactly what would be sent and to whom, then opt in. The files stay on this rig.'));
      const jobs = Array.isArray(local.jobs) ? local.jobs : [];
      if (jobs.length) {
        const ul = h('ul', {class: 'job-list'});
        for (const j of jobs.slice(0, 20)) {
          const js = C.jobState(j);
          ul.appendChild(h('li', null, link({view: 'rig', tab: 'upload', job: j.id}, j.run_id || j.id),
            h('span', {class: 'muted small'}, ' \u00b7 ' + js.word + (js.fraction !== null ? ' ' + Math.round(js.fraction * 100) + '%' : ''))));
        }
        block.append(h('h3', {class: 'sub-title'}, 'Transfers'), ul);
      }
      const area = region('the workspace\u2019s projects');
      block.appendChild(area.el);
      (async () => {
        const got = await screenRequest(ctx.epoch, 'GET', '/local/projects');
        if (!got) return;
        if (!got.ok) return area.fail(got.error, retryCurrent);
        const projects = ((got.value && got.value.items) || []).filter((p) => p && !p.archived);
        if (!projects.length) return area.fill(h('p', {class: 'muted'}, 'No project is registered in this rig\u2019s workspace.'));
        const choice = select([['', 'Choose a project\u2026'], ...projects.map((p) => [p.id, p.title || p.slug || p.id])], r.project || '', {'data-focus': 'up-project'});
        choice.addEventListener('change', () => go({view: 'rig', tab: 'upload', project: choice.value || undefined}, {focus: 'up-project'}));
        const parts = [field('Project', choice)];
        const project = projects.find((p) => p.id === r.project);
        if (project) parts.push(sessionPicker(ctx, project));
        area.fill(...parts);
      })();
      return block;
    }

    function sessionPicker(ctx, project) {
      const r = ctx.route;
      const wrap = h('div', {class: 'sessions'});
      const area = region('finished sessions');
      wrap.appendChild(area.el);
      (async () => {
        const got = await screenRequest(ctx.epoch, 'GET', '/local/sessions', {query: {project_id: project.id}});
        if (!got) return;
        if (!got.ok) return area.fail(got.error, retryCurrent);
        const items = (got.value && got.value.items) || [];
        const problems = (got.value && got.value.problems) || [];
        const nodes = [];
        if (problems.length) nodes.push(h('ul', {class: 'note note-warn'}, ...problems.slice(0, 10).map((p) => h('li', null, String(p)))));
        if (!items.length) {
          nodes.push(h('p', {class: 'muted'}, 'No sessions found in this project\u2019s data folders.'));
          return area.fill(...nodes);
        }
        const table = h('table', {class: 'table'},
          h('caption', {class: 'visually-hidden'}, 'Sessions on this rig'),
          h('thead', null, h('tr', null, ...['Session', 'Date', 'Task', 'Mode', 'Folder', 'State', ''].map((t) => h('th', {scope: 'col'}, t)))));
        const tbody = h('tbody');
        let chosen = null;
        for (const s of items) {
          const selected = s.root_id === r.root && s.run_id === r.run;
          if (selected) chosen = s;
          let word = s.complete ? 'finished' : 'incomplete';
          if (s.active) word = 'running now';
          if (s.job) word += ' \u00b7 ' + C.jobState(s.job).word;
          const action = s.complete && !s.active
            ? link({view: 'rig', tab: 'upload', project: project.id, root: s.root_id, run: s.run_id}, selected ? 'Chosen' : 'Choose',
              {class: 'btn btn-small ' + (selected ? 'btn-primary' : 'btn-line'), 'aria-current': selected ? 'true' : null,
                'data-focus': 'up-run-' + s.run_id, 'data-focus-next': 'up-preview'})
            : null;
          tbody.appendChild(h('tr', {class: selected ? 'row-current' : null},
            h('td', {class: 'mono'}, 'sub-' + (s.subject || '?') + ' \u00b7 ses ' + (s.session ?? '?') + ' \u00b7 run ' + (s.run ?? '?')),
            h('td', null, s.date ? C.formatDate(s.date, false) : ''),
            h('td', null, s.task || ''),
            h('td', null, s.mode || ''),
            h('td', null, s.root_name || s.root_kind || ''),
            h('td', null, word),
            h('td', null, action)));
        }
        table.appendChild(tbody);
        nodes.push(h('div', {class: 'table-wrap'}, table));
        if (chosen) nodes.push(uploadPreview(ctx, project, chosen));
        area.fill(...nodes);
      })();
      return wrap;
    }

    function uploadPreview(ctx, project, session) {
      const wrap = h('div', {class: 'consent'});
      const status = statusLine();
      const releaseArea = h('div');
      const previewArea = h('div');
      wrap.append(h('h3', {class: 'sub-title', tabindex: '-1', 'data-focus': 'up-preview'}, 'Review before uploading'), releaseArea, previewArea, status.el);
      let release = null;  // {experiment_id, version_id} chosen when the project has no install record

      const runPreview = async () => {
        previewArea.replaceChildren(h('p', {class: 'loading'}, h('span', {class: 'spinner', 'aria-hidden': 'true'}), 'Listing the session\u2019s files\u2026'));
        const body = {project_id: project.id, root_id: session.root_id, run_id: session.run_id};
        if (release) Object.assign(body, {experiment_id: release.experiment_id, version_id: release.version_id});
        try {
          const preview = await api('POST', '/local/upload-preview', {json: body, timeoutMs: 120000});
          if (ctx.epoch !== state.epoch) return;
          previewArea.replaceChildren(consentForm(ctx, project, session, preview, status, runPreview, release ? release.label : ''));
        } catch (exc) {
          if (ctx.epoch !== state.epoch) return;
          noteFailure(exc);
          if (exc.kind === 'invalid' && !release) {
            previewArea.replaceChildren(h('p', {class: 'muted'}, exc.message + ' Choose which release in your library the session should be recorded against.'));
            releaseArea.replaceChildren(releasePicker((picked) => { release = picked; runPreview(); }));
            return;
          }
          previewArea.replaceChildren(errorBox(exc, runPreview));
        }
      };
      runPreview();
      return wrap;
    }

    function releasePicker(onPick) {
      const wrap = h('div', {class: 'inline-form'});
      const status = statusLine();
      libraryItems(false).then((items) => {
        const usable = items.filter((i) => i.experiment && i.version);
        if (!usable.length) {
          wrap.replaceChildren(h('p', {class: 'muted'}, 'Your library is empty. Pin the experiment\u2019s release first.'),
            link({view: 'catalog'}, 'Open the catalogue', {class: 'btn btn-line'}));
          return;
        }
        const choice = select(usable.map((i) => [i.experiment.id + '|' + i.version.id, (i.experiment.title || i.experiment.id) + ' ' + versionLabel(i.version)]), '');
        const button = h('button', {type: 'button', class: 'btn btn-line'}, 'Use this release');
        button.addEventListener('click', () => {
          const [experiment_id, version_id] = String(choice.value).split('|');
          const item = usable.find((i) => i.experiment.id === experiment_id && i.version.id === version_id);
          if (item) onPick({experiment_id, version_id, label: (item.experiment.title || experiment_id) + ' ' + versionLabel(item.version)});
        });
        wrap.replaceChildren(field('Record against', choice), button, status.el);
      }).catch((exc) => { noteFailure(exc); wrap.replaceChildren(errorBox(exc)); });
      return wrap;
    }

    /* The consent: who receives it (hub and account), which release it is
     * recorded against, exactly which files (and their manifest digest),
     * what personal information they can hold. It names this preview_id;
     * a change to any of these needs a new preview (rig-contract B2). */
    function consentForm(ctx, project, session, preview, status, again, pickedLabel) {
      const p = preview || {};
      const recipient = p.recipient || {};
      const user = recipient.user || {};
      const install = p.install || null;
      const files = Array.isArray(p.files) ? p.files : [];
      const totals = C.uploadTotals(files, p.total_bytes);
      const fileCount = Number(p.file_count) || totals.count;
      const meta = p.metadata || {};
      const privacy = p.privacy || {};
      const releaseName = install ? (install.title || install.name || p.experiment_id) + ' ' + versionLabel(install)
        : pickedLabel || String(p.experiment_id || '') + ' / ' + String(p.version_id || '');
      const form = h('form', {class: 'form'});
      form.appendChild(spec([
        ['Recipient', h('span', null, h('span', {class: 'mono'}, recipient.base_url || '(unknown hub)'), h('span', null, ' \u00b7 account '),
          h('strong', null, (user.display_name || user.username || '?') + (user.username ? ' (@' + user.username + ')' : '')))],
        ['Release', releaseName],
        ['Release SHA-256', install && install.sha256 ? C.shortHash(install.sha256) + '\u2026' : null, {mono: true}],
        ['Subject', meta.subject_code, {mono: true}],
        ['Mode', meta.mode],
        ['Rig', meta.rig_alias],
        ['Started', C.formatDate(meta.started_at)],
        ['Files', `${fileCount} files \u00b7 ${C.formatBytes(totals.bytes)}`, {mono: true}],
        ['Session manifest SHA-256', digest(p.manifest_digest)],
      ]));
      const det = h('details', {class: 'excluded'}, h('summary', null, `Show all ${fileCount} files`));
      const ul = h('ul', {class: 'file-list'});
      for (const f of files) ul.appendChild(h('li', {class: 'file-row'}, h('span', {class: 'mono small'}, f.path), h('span', {class: 'mono small file-size'}, C.formatBytes(f.size))));
      if (files.length < fileCount) ul.appendChild(h('li', {class: 'muted small'}, `${fileCount - files.length} more not listed here; the rig sends exactly the previewed set.`));
      det.appendChild(ul);
      form.appendChild(det);
      const fields = Array.isArray(privacy.fields) ? privacy.fields : [];
      form.appendChild(h('div', {class: 'callout callout-warn'},
        h('p', {class: 'callout-title'}, 'What these files can reveal'),
        privacy.warning ? h('p', null, String(privacy.warning)) : null,
        h('p', null, 'Session folders can hold rig settings, configuration snapshots, logs and participant information such as subject codes, initials, age and sex.'),
        fields.length ? h('p', null, 'Recorded fields: ' + fields.join(', ') + '.') : null,
        h('p', null, 'Only the recipient account can read the upload; the experiment\u2019s author gets no access. Nothing on this rig is deleted.')));
      const consent = checkbox('Upload this session to ' + (user.username ? '@' + user.username : 'this account') + ' at '
        + (recipient.base_url ? hostOf(recipient.base_url) : 'this hub') + ', recorded against ' + releaseName + '.');
      const button = h('button', {type: 'submit', class: 'btn btn-primary'}, 'Upload privately');
      form.append(consent.el, h('div', {class: 'actions'}, button,
        h('button', {type: 'button', class: 'btn btn-quiet', on: {click: again}}, 'Preview again')));
      form.addEventListener('submit', async (event) => {
        prevent(event);
        if (!consent.box.checked) return status.show('Tick the box to opt in to this upload.', 'err');
        button.disabled = true;
        status.show('Starting the transfer\u2026', 'info');
        try {
          const answer = await api('POST', '/local/upload', {json: {
            project_id: project.id, root_id: session.root_id, run_id: session.run_id,
            experiment_id: p.experiment_id, version_id: p.version_id, preview_id: p.preview_id, consent: true,
          }});
          const job = (answer && answer.job) || answer;
          if (!job || !job.id) throw new C.HubError('bad_response', 'The rig did not return the transfer it started.');
          await loadLocal();
          go({view: 'rig', tab: 'upload', job: job.id});
        } catch (exc) {
          noteFailure(exc);
          if (exc.kind === 'preview_stale') {
            status.show(exc.message, 'err');
            again();
          } else {
            status.show(exc.message, 'err');
          }
          button.disabled = false;
        }
      });
      return form;
    }

    function jobPanel(ctx, jobId) {
      const wrap = h('div', {class: 'job'});
      const head = h('p', {class: 'job-state', role: 'status', 'aria-live': 'polite'}, 'Reading the transfer\u2026');
      const bar = h('div', {class: 'meter', role: 'progressbar', 'aria-label': 'Upload progress', 'aria-valuemin': '0', 'aria-valuemax': '100'});
      const fill = h('span', {class: 'meter-fill'});
      bar.appendChild(fill);
      const numbers = h('p', {class: 'mono small'});
      const detail = h('div');
      const controls = h('div', {class: 'actions'});
      const status = statusLine();
      wrap.append(h('p', {class: 'eyebrow mono'}, link({view: 'rig', tab: 'upload'}, 'All transfers'), h('span', {'aria-hidden': 'true'}, ' / '), h('span', null, jobId)),
        head, bar, numbers, detail, controls, status.el);
      let delay = POLL_MS;
      const paint = (job) => {
        const js = C.jobState(job);
        head.textContent = js.word + (js.running ? '\u2026' : '');
        head.className = 'job-state job-' + (js.ok ? 'ok' : js.status === 'failed' || js.status === 'cancelled' ? 'err' : js.status === 'paused' ? 'warn' : 'info');
        const pct = js.fraction === null ? null : Math.round(js.fraction * 100);
        if (pct === null) { bar.removeAttribute('aria-valuenow'); fill.className = 'meter-fill meter-unknown'; } else {
          bar.setAttribute('aria-valuenow', String(pct));
          fill.className = 'meter-fill meter-p' + Math.min(100, Math.max(0, Math.round(pct / 5) * 5));
        }
        const parts = [];
        if (js.done !== null && js.total !== null) parts.push(C.formatBytes(js.done) + ' of ' + C.formatBytes(js.total));
        if (job.files_total) parts.push((job.files_done || 0) + ' of ' + job.files_total + ' files');
        if (job.run_id) parts.push(job.run_id);
        numbers.textContent = parts.join(' \u00b7 ');
        detail.replaceChildren();
        if (js.error) detail.appendChild(h('p', {class: 'note note-' + (js.status === 'paused' ? 'warn' : 'err')}, js.error));
        if (job.error && ['auth_context_changed', 'signed_out', 'unauthenticated'].includes(job.error.code)) {
          detail.appendChild(h('p', {class: 'muted small'}, 'It continues only when the account it was approved for is signed in to the same hub.'));
        }
        if (js.needsPreview && job.project_id && job.root_id && job.run_id) {
          detail.appendChild(link({view: 'rig', tab: 'upload', project: job.project_id, root: job.root_id, run: job.run_id},
            'Preview this session again', {class: 'btn btn-line'}));
        }
        if (js.ok) {
          detail.appendChild(h('p', {class: 'note note-ok'}, 'The hub verified every file and recorded the session. The originals stay on this rig.'));
          if (job.session_id) detail.appendChild(link({view: 'data', session: job.session_id}, 'Open the uploaded session', {class: 'btn btn-line'}));
        }
        controls.replaceChildren();
        if (js.canResume) controls.appendChild(h('button', {type: 'button', class: 'btn btn-primary', on: {click: () => act('resume')}}, 'Resume'));
        if (js.canCancel) controls.appendChild(h('button', {type: 'button', class: 'btn btn-quiet', on: {click: () => act('cancel')}}, 'Cancel transfer'));
        return js;
      };
      const poll = async () => {
        const got = await screenRequest(ctx.epoch, 'GET', '/local/jobs/' + C.seg(jobId));
        if (!got) return;
        if (!got.ok) {
          if (got.error.kind === 'not_found') {
            head.textContent = 'No such transfer for the signed-in account.';
            return;
          }
          status.show(got.error.message, 'err');
          delay = Math.min(POLL_MAX_MS, delay * 2);
        } else {
          status.clear();
          delay = POLL_MS;
          const js = paint((got.value && got.value.job) || got.value || {});
          if (js.terminal) return;
        }
        state.polls.push(timers.set(poll, delay));
      };
      const act = async (verb) => {
        status.show(verb === 'resume' ? 'Resuming\u2026' : 'Cancelling\u2026', 'info');
        try {
          const answer = await api('POST', '/local/jobs/' + C.seg(jobId) + '/' + verb, {json: {}});
          if (ctx.epoch !== state.epoch) return;
          status.clear();
          const js = paint((answer && answer.job) || answer || {});
          if (!js.terminal) {
            for (const id of state.polls) timers.clear(id);
            state.polls = [timers.set(poll, POLL_MS)];
          }
        } catch (exc) {
          noteFailure(exc);
          status.show(exc.message, 'err');
        }
      };
      poll();
      return wrap;
    }

    const SCREENS = {
      home: screenHome, catalog: screenCatalog, experiment: screenExperiment, guide: screenGuide,
      signin: screenSignin, register: screenRegister, library: screenLibrary, mine: screenMine,
      data: screenData, rig: screenRig, create: screenCreate,
    };

    /* Links drawn by the documentation viewer (task links) are plain anchors
     * to this page; follow them without a reload. */
    function onMainClick(event) {
      if (!event || event.defaultPrevented || event.button > 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      let node = event.target;
      while (node && node.tagName !== 'A') node = node.parentNode;
      if (!node || node.hasAttribute('download') || node.getAttribute('target')) return;
      const href = node.getAttribute('href') || '';
      const path = loc.pathname || '/';
      if (!href.startsWith(path + '?') && !href.startsWith('?')) return;
      const search = href.slice(href.indexOf('?'));
      prevent(event);
      go(C.parseRoute(search.slice(1)));
    }

    $('main').addEventListener('click', onMainClick);
    /* The static brand and footer links: same-page navigation too. */
    for (const [id, route] of [['brand', {view: 'home'}], ['footer-guide', {view: 'guide'}]]) {
      const el = doc.getElementById(id);
      if (!el) continue;
      el.setAttribute('href', (loc.pathname || '/') + (C.formatRoute(route) || '?'));
      el.addEventListener('click', (event) => {
        if (!state.booted || (event && (event.button > 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey))) return;
        prevent(event);
        go(route);
      });
    }
    const ready = boot();
    return {state, ready, go, draw};
  }

  return {mount};
})();

if (typeof window !== 'undefined') {
  window.HubApp = HubApp;
  window.hubPage = HubApp.mount({
    document, location, history, window, fetch: window.fetch.bind(window),
    sessionStorage: window.sessionStorage, localStorage: window.localStorage,
    setTimeout: window.setTimeout.bind(window), clearTimeout: window.clearTimeout.bind(window),
    clipboard: navigator.clipboard, scrollTo: window.scrollTo.bind(window),
  });
}
