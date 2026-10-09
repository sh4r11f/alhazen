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
        ['catalog', 'Catalogue'],
        ['library', 'Library'],
        ['mine', 'My experiments'],
        ['data', 'Data'],
      ];
      if (state.role === 'rig') items.push(['rig', 'This rig']);
      const list = h('ul', {class: 'nav-list'});
      for (const [view, label] of items) {
        const current = state.route.view === view
          || (view === 'catalog' && state.route.view === 'experiment');
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
      if (state.user) {
        const who = h('span', {class: 'who'},
          h('span', {class: 'who-label'}, state.role === 'rig' ? 'Operator' : 'Signed in as'),
          h('span', {class: 'who-name'}, state.user.display_name || state.user.username),
          h('span', {class: 'who-handle'}, '@' + state.user.username));
        const out = h('button', {type: 'button', class: 'btn btn-quiet', on: {click: signOut}}, 'Sign out');
        el.replaceChildren(who, out);
      } else {
        el.replaceChildren(
          link({view: 'signin', next: currentNext()}, 'Sign in', {class: 'btn btn-quiet'}),
          link({view: 'register'}, 'Register', {class: 'btn btn-line'}));
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

    /** A catalogue or library entry: {experiment, version}. */
    function listing(item, extra) {
      const experiment = (item && item.experiment) || {};
      const version = (item && item.version) || null;
      const card = h('article', {class: 'listing'});
      const head = h('header', {class: 'listing-head'},
        h('h3', {class: 'listing-title'},
          link({view: 'experiment', id: experiment.id, version: version ? version.id : undefined}, experiment.title || 'Untitled experiment')),
        version ? h('span', {class: 'chip chip-version mono'}, versionLabel(version)) : null);
      card.appendChild(head);
      const by = ownerLine(experiment);
      if (by) card.appendChild(by);
      if (experiment.summary) card.appendChild(h('p', {class: 'listing-summary'}, experiment.summary));
      // Hardware, licence and platforms live on the experiment page's release
      // sheet; a card stays title, author, one line and version.
      if (extra) card.appendChild(extra);
      return card;
    }

    /* ---- home ------------------------------------------------------------ */

    /* The landing page's drawing: a lateral view of the brain, traced from a
     * single-weight line drawing (potrace) into one path, coloured by CSS
     * with currentColor. Built as SVG nodes, never parsed from markup. */
    const BRAIN_VIEWBOX = '0 0 991 931';
    const BRAIN_TRANSFORM = 'translate(0.000000,931.000000) scale(0.100000,-0.100000)';
    const BRAIN_PATH = 'M4735 8768 c-44 -5 -118 -22 -165 -37 l-85 -29 -75 26 c-162 57 -462 30 -778 -69 -85 -27 -175 -46 -272 -59 -328 -44 -482 -96 -674 -226 -43 -30 -84 -54 -90 -54 -26 0 -136 -61 -195 -108 -49 -40 -76 -54 -120 -62 -231 -46 -394 -141 -535 -314 -28 -33 -62 -59 -110 -83 -213 -107 -357 -244 -455 -433 -28 -54 -45 -74 -99 -110 -147 -101 -295 -285 -363 -449 -26 -64 -40 -83 -87 -121 -115 -93 -236 -282 -277 -435 -9 -33 -19 -112 -22 -175 l-5 -115 -46 -70 c-118 -181 -167 -342 -166 -545 0 -119 4 -145 27 -217 27 -79 27 -85 16 -175 -47 -381 110 -731 392 -874 28 -13 55 -36 63 -52 85 -165 347 -307 646 -352 273 -40 409 -36 790 25 33 6 37 3 69 -46 107 -158 351 -312 559 -351 35 -6 62 -17 62 -23 0 -24 159 -332 198 -384 95 -125 226 -200 447 -254 115 -28 165 -47 280 -104 322 -161 550 -184 777 -78 38 18 70 25 114 25 34 0 106 9 159 20 54 11 99 19 100 18 1 -2 15 -41 31 -88 48 -140 155 -287 275 -375 36 -27 52 -49 74 -102 72 -173 168 -305 359 -489 141 -138 143 -142 219 -354 30 -85 83 -229 117 -320 34 -91 92 -271 129 -400 53 -187 70 -236 85 -238 22 -5 21 3 -41 218 -60 212 -84 286 -164 500 -37 102 -90 246 -118 320 -61 166 -44 146 64 -75 83 -170 202 -489 286 -769 40 -131 45 -144 55 -134 14 14 -133 474 -227 711 -77 193 -123 283 -259 507 -119 197 -179 314 -215 421 l-23 67 36 6 c48 9 92 26 92 37 0 5 -15 5 -32 1 -253 -56 -568 169 -682 486 -33 92 -33 88 12 103 20 6 63 25 94 40 73 37 78 36 78 -5 0 -41 34 -154 54 -181 21 -27 20 -9 -4 60 -12 34 -20 84 -20 120 l0 62 58 36 c31 19 77 53 102 74 86 74 71 75 163 -14 91 -88 168 -143 267 -193 162 -82 229 -184 270 -414 38 -208 70 -280 202 -449 178 -227 247 -411 314 -844 10 -62 22 -113 26 -113 31 0 -57 464 -118 623 -52 135 -99 219 -197 350 -116 154 -153 235 -188 412 -34 176 -58 240 -120 321 -26 35 -47 64 -45 64 59 0 223 -102 339 -210 108 -101 107 -100 105 -282 -3 -219 41 -362 158 -513 25 -33 52 -75 60 -93 40 -92 87 -366 114 -672 20 -221 77 -551 100 -574 19 -19 24 6 12 62 -25 112 -52 289 -67 442 -32 315 -77 613 -105 685 -13 33 -11 33 62 -23 319 -241 795 -248 1147 -17 20 13 56 29 81 34 53 12 144 51 199 85 21 14 74 34 118 45 205 51 353 149 445 295 18 28 73 95 124 148 66 70 103 120 134 180 24 46 62 104 86 129 93 101 124 207 120 409 -2 102 -7 144 -20 174 l-17 39 23 -8 c133 -42 285 4 411 124 176 168 260 408 256 735 -1 148 -1 152 30 224 88 209 86 469 -3 689 -34 81 -36 95 -36 194 0 230 -66 432 -180 554 -23 24 -31 46 -40 110 -38 252 -169 509 -340 662 -53 48 -59 57 -65 105 -32 252 -200 548 -389 683 -50 36 -58 47 -77 105 -86 261 -270 477 -506 592 -89 43 -111 59 -137 96 -51 74 -132 163 -198 220 -101 85 -312 182 -397 182 -20 0 -47 18 -101 71 -202 194 -602 379 -994 460 -99 20 -157 21 -431 7 -11 0 -48 11 -83 26 -85 37 -206 52 -313 39 -83 -9 -85 -9 -154 23 -152 69 -356 96 -560 72z m410 -57 c244 -65 518 -301 591 -510 13 -36 21 -67 19 -69 -2 -3 -24 7 -49 22 -112 65 -130 51 -22 -17 188 -120 284 -284 315 -546 26 -214 -23 -372 -207 -666 -197 -314 -262 -498 -262 -735 0 -106 14 -210 29 -210 4 0 3 33 -1 73 -29 250 52 516 259 847 184 295 218 388 217 590 -1 233 -52 396 -168 532 -41 49 -64 87 -80 135 -56 163 -123 256 -280 388 -52 44 -95 84 -95 89 -2 20 252 0 326 -26 138 -49 297 -176 400 -320 19 -26 37 -48 40 -48 9 0 -10 46 -37 85 -50 74 -56 69 76 62 186 -10 347 -53 519 -139 144 -72 210 -128 363 -306 67 -78 104 -97 269 -138 192 -48 256 -83 378 -209 62 -63 133 -124 187 -159 158 -104 210 -192 216 -370 4 -90 2 -107 -9 -100 -8 5 -45 30 -84 55 -206 136 -457 201 -661 172 -126 -18 -102 -28 69 -28 253 0 437 -62 642 -217 263 -200 355 -373 319 -599 -27 -174 -3 -252 126 -396 121 -135 166 -233 191 -418 28 -217 -14 -459 -108 -611 -120 -195 -120 -195 -172 -415 -30 -130 -56 -189 -126 -291 -35 -51 -101 -149 -148 -218 -167 -249 -282 -308 -577 -298 -178 6 -256 -3 -413 -48 -342 -99 -565 -119 -1172 -104 -240 5 -270 0 -650 -115 -137 -42 -329 -92 -425 -112 -219 -44 -262 -61 -360 -134 -132 -100 -253 -145 -457 -170 -90 -11 -105 -10 -178 8 -44 12 -82 19 -83 18 -8 -8 47 -35 93 -47 64 -16 63 -14 32 -38 -42 -33 -107 -190 -79 -190 5 0 13 15 16 33 20 107 101 171 236 188 172 20 297 69 437 170 108 78 120 82 346 129 105 22 244 56 307 75 565 172 545 170 1030 151 347 -13 614 19 925 111 119 35 122 36 370 43 373 10 466 56 622 304 32 50 86 129 121 176 93 125 138 226 172 386 16 73 38 154 49 180 16 34 110 194 115 194 1 0 1 -44 2 -98 1 -177 45 -287 170 -437 151 -179 208 -379 170 -590 -9 -49 -19 -103 -22 -120 l-5 -30 15 27 c26 45 46 148 46 242 l0 89 29 -39 c16 -21 61 -75 101 -119 119 -132 168 -238 177 -385 16 -258 -148 -484 -385 -530 -167 -33 -324 5 -619 149 l-178 88 51 26 c28 14 80 49 115 78 35 28 99 71 141 96 114 65 230 192 216 235 -2 6 -16 -9 -30 -33 -38 -63 -102 -121 -187 -170 -41 -23 -106 -66 -145 -96 -185 -141 -351 -167 -686 -108 -85 15 -213 31 -285 34 l-130 7 140 68 c163 79 257 106 367 106 89 0 104 15 24 25 -107 14 -239 -21 -411 -107 -138 -70 -394 -152 -413 -133 -2 2 20 28 50 57 170 167 176 202 7 45 -185 -174 -301 -221 -582 -237 -87 -6 -211 -19 -275 -30 -94 -16 -156 -19 -312 -17 l-195 2 -85 -32 c-47 -17 -125 -48 -175 -68 -49 -21 -124 -45 -165 -55 -162 -40 -163 -40 -75 -36 48 3 111 14 160 30 43 14 80 24 82 23 6 -7 -163 -144 -233 -189 -295 -191 -607 -252 -861 -170 -51 16 -93 26 -93 20 0 -13 81 -47 161 -66 38 -10 69 -20 69 -23 0 -4 -37 -17 -82 -29 -73 -20 -99 -22 -213 -17 -159 6 -200 19 -423 126 -92 44 -194 87 -227 95 -190 47 -237 62 -307 98 -125 64 -196 147 -295 345 -100 197 -117 250 -117 368 1 229 108 390 560 839 328 326 643 489 1064 552 242 35 616 13 771 -46 64 -24 76 -25 59 -6 -32 40 -228 84 -407 93 -67 3 -120 9 -118 13 18 29 139 71 245 86 127 17 196 38 346 108 260 120 570 139 949 59 101 -21 160 -26 370 -33 225 -7 260 -10 345 -34 160 -43 203 -36 350 60 80 52 156 79 255 90 57 6 58 7 30 17 -63 22 -219 -23 -320 -93 -112 -77 -155 -83 -308 -44 -55 14 -144 28 -196 32 l-95 7 67 39 c137 80 275 231 367 401 108 199 217 309 378 383 69 31 55 40 -21 14 -136 -48 -264 -172 -372 -361 -154 -269 -313 -415 -510 -466 -85 -22 -130 -19 -320 20 -93 19 -181 35 -194 35 -21 0 -18 5 24 31 114 73 217 221 231 334 9 64 -2 54 -32 -31 -44 -126 -136 -237 -242 -290 -55 -27 -63 -28 -215 -31 -251 -3 -387 -30 -557 -110 -138 -65 -191 -82 -328 -103 -106 -16 -138 -25 -225 -67 -134 -64 -189 -66 -291 -9 -129 72 -260 228 -320 380 -59 148 -65 235 -35 551 11 128 6 250 -17 380 -22 120 -29 316 -15 418 37 276 232 657 418 816 26 22 45 43 41 46 -8 9 -96 -64 -165 -139 l-63 -67 0 33 c0 85 -54 182 -127 228 -71 45 -73 36 -5 -25 116 -106 134 -234 52 -374 -126 -217 -178 -390 -189 -629 l-6 -144 -44 67 c-78 117 -213 238 -333 296 -75 36 -78 96 -13 254 8 20 8 20 -13 1 -29 -25 -62 -116 -62 -170 l0 -44 -77 5 c-290 19 -508 -164 -563 -475 -24 -136 -44 -186 -94 -230 -71 -63 -127 -67 -267 -22 -209 67 -364 67 -574 -1 -137 -43 -154 -39 -267 67 -111 104 -165 128 -278 122 -140 -7 -254 -87 -333 -234 -35 -64 -54 -83 -189 -195 -361 -298 -531 -557 -555 -844 -8 -102 -13 -105 -62 -35 -188 268 -123 679 159 1005 22 26 40 48 40 51 0 10 -12 2 -52 -33 l-41 -38 6 95 c16 229 119 437 287 578 58 49 71 67 98 132 76 181 213 350 365 448 55 37 71 53 91 97 62 136 183 276 314 362 75 49 152 89 152 79 0 -2 -25 -56 -55 -121 -82 -174 -51 -160 41 18 109 213 241 347 421 429 74 33 200 68 248 68 22 0 40 9 61 31 40 43 135 104 200 128 29 11 90 45 134 75 166 112 347 172 623 207 106 13 194 31 268 54 358 112 617 136 807 74 37 -12 40 -19 13 -36 -26 -16 -61 -92 -61 -132 0 -81 58 -124 182 -134 136 -11 284 -92 263 -144 -16 -42 -17 -121 -1 -175 16 -57 45 -102 129 -204 87 -105 121 -206 98 -291 -7 -23 -45 -92 -87 -153 -153 -225 -206 -374 -208 -585 -1 -163 17 -253 89 -435 97 -246 97 -298 -5 -675 -21 -81 -34 -154 -37 -222 -7 -124 11 -143 20 -23 6 79 20 141 59 275 l20 70 19 -38 c30 -58 162 -138 187 -113 6 7 11 4 -62 40 -110 56 -140 126 -111 263 33 154 19 236 -80 493 -102 262 -91 509 33 756 127 251 311 435 495 494 50 16 38 30 -15 18 -59 -13 -172 -75 -228 -125 l-47 -43 -5 74 c-6 95 -36 156 -123 256 -155 178 -159 277 -20 494 19 29 37 66 41 82 9 38 1 29 -76 -88 l-64 -96 -29 29 c-55 55 -187 99 -298 99 -50 0 -109 51 -109 94 0 101 102 176 285 211 107 21 358 13 460 -14z m1089 -136 c370 -65 826 -271 1006 -456 66 -67 73 -71 137 -85 224 -46 426 -184 552 -377 38 -59 46 -66 111 -91 216 -85 390 -262 503 -513 49 -108 48 -110 -28 -68 -83 47 -85 29 -2 -23 321 -203 516 -512 533 -847 l6 -100 -41 73 c-80 143 -171 250 -258 305 -69 44 -62 32 36 -59 215 -202 278 -361 291 -744 9 -265 12 -282 92 -468 69 -162 77 -125 9 42 -58 143 -73 218 -69 331 l3 80 19 -35 c10 -19 45 -69 76 -110 228 -299 265 -439 189 -705 -16 -56 -30 -113 -31 -128 -1 -15 -5 -26 -8 -24 -46 27 -115 51 -155 54 l-50 3 66 -23 c112 -38 184 -102 255 -224 87 -148 106 -362 43 -486 -24 -46 -21 -71 4 -34 104 160 76 412 -68 606 -65 88 -68 113 -29 236 80 251 44 414 -151 680 -132 180 -155 226 -165 337 -5 51 -16 125 -25 163 -16 77 -20 215 -6 215 16 0 131 -131 184 -209 94 -140 168 -332 183 -477 6 -59 12 -71 57 -127 105 -130 147 -261 157 -487 5 -140 9 -161 34 -220 95 -217 99 -475 10 -695 -27 -67 -28 -77 -29 -255 -2 -164 -5 -196 -27 -281 -88 -329 -264 -518 -491 -527 -79 -4 -93 -1 -142 23 -59 28 -70 45 -31 45 51 0 181 77 245 145 108 116 146 217 145 385 -1 177 -45 277 -196 445 -96 107 -117 143 -167 273 -43 115 -83 180 -164 272 -147 167 -192 391 -124 621 45 149 52 197 51 364 0 268 -43 393 -190 561 -152 172 -161 214 -108 473 41 199 40 296 -1 368 l-16 28 6 -35 c12 -79 17 -181 10 -227 l-7 -48 -28 77 c-35 95 -70 146 -176 259 l-84 88 0 100 c0 195 -61 306 -233 421 -59 39 -123 93 -162 137 -120 132 -218 187 -410 233 -168 40 -186 51 -298 178 -224 256 -516 391 -887 409 l-125 6 -50 46 c-27 26 -68 60 -89 76 l-40 30 40 6 c76 12 222 9 308 -6z m-2737 -1819 c191 -51 419 -274 487 -477 32 -97 46 -249 35 -389 -19 -237 -22 -397 -10 -461 44 -223 204 -450 385 -547 l38 -20 -49 -7 c-182 -26 -335 -68 -495 -136 -134 -57 -154 -56 -231 19 -143 139 -176 327 -108 626 43 193 42 319 -5 430 -41 97 -56 93 -22 -6 39 -117 38 -220 -3 -410 -26 -116 -32 -167 -33 -269 l-1 -127 -33 62 c-18 33 -58 110 -89 170 -66 128 -133 202 -238 263 -103 61 -162 137 -180 236 -10 52 -28 47 -20 -5 5 -34 40 -120 72 -179 2 -3 -19 -4 -46 -1 -56 5 -121 -4 -121 -17 0 -5 42 -11 93 -13 193 -7 310 -101 437 -351 142 -279 245 -427 339 -490 14 -9 23 -18 20 -20 -2 -2 -29 -18 -59 -37 -86 -53 -187 -131 -290 -224 -92 -83 -97 -86 -144 -86 -274 0 -577 293 -663 640 -18 74 -18 74 -21 32 -2 -23 4 -67 12 -96 9 -30 16 -56 16 -59 0 -3 -24 4 -52 15 -42 16 -89 22 -219 28 -249 11 -310 45 -417 235 -59 106 -166 225 -217 245 -43 16 -39 8 21 -39 64 -50 116 -117 169 -221 50 -97 149 -197 220 -224 37 -13 94 -20 215 -26 241 -12 272 -26 352 -162 127 -219 318 -360 545 -405 l43 -8 -126 -135 c-146 -157 -158 -164 -284 -157 -107 6 -178 34 -296 116 -100 70 -170 95 -289 106 -293 26 -438 115 -554 343 -97 191 -126 215 -326 277 -111 35 -124 41 -171 89 -87 87 -119 231 -91 411 l7 50 -17 -30 c-15 -25 -18 -54 -18 -165 0 -128 1 -138 29 -193 51 -106 112 -150 265 -194 159 -45 221 -101 300 -266 55 -115 120 -195 199 -245 54 -36 216 -92 261 -92 61 0 29 -18 -60 -35 -141 -26 -203 -14 -408 77 -162 71 -513 160 -431 108 8 -5 39 -15 68 -21 l53 -12 -93 -34 c-139 -51 -147 -53 -227 -53 -60 0 -72 -2 -62 -12 25 -26 167 -14 253 20 169 69 224 66 423 -22 233 -104 331 -115 515 -62 146 43 207 30 369 -79 134 -89 192 -110 314 -110 100 0 99 3 32 -83 l-47 -60 -141 -4 c-114 -4 -162 -11 -242 -33 -206 -57 -378 -45 -647 46 -209 71 -279 75 -439 28 -134 -40 -110 -54 33 -19 131 33 205 26 398 -38 86 -28 175 -54 198 -58 136 -22 -119 -58 -408 -58 -306 0 -497 38 -689 139 -135 71 -264 219 -305 348 -10 32 -22 57 -27 57 -9 0 -5 -30 12 -84 13 -41 6 -42 -46 -12 -237 140 -377 509 -318 844 l5 33 24 -33 c13 -18 35 -42 48 -53 20 -16 27 -34 35 -100 11 -89 28 -145 43 -145 6 0 7 7 4 16 -19 50 -28 213 -17 302 36 273 136 458 372 687 97 94 273 244 297 253 4 1 5 -43 4 -98 -3 -68 0 -100 7 -100 7 0 11 24 11 63 0 78 31 202 66 267 122 224 389 268 546 90 98 -110 94 -132 -50 -276 -113 -113 -190 -209 -178 -221 3 -3 18 10 33 28 321 390 654 514 1051 388 193 -61 299 -25 368 126 13 28 26 77 30 110 15 148 56 247 136 331 128 136 305 193 465 150z m-1710 -732 l24 -6 -31 -19 c-39 -24 -37 -24 -42 11 -4 24 -1 30 10 25 7 -3 25 -8 39 -11z m1003 -2283 c0 -5 -11 -33 -25 -63 -40 -87 -58 -187 -53 -298 l5 -97 -61 13 c-194 44 -370 159 -509 332 -38 47 -37 47 61 48 61 1 124 10 212 32 69 17 139 33 155 35 60 8 215 7 215 -2z m4690 -566 c63 -9 131 -20 150 -24 l35 -9 -47 -1 c-70 -2 -267 -28 -411 -56 -150 -29 -186 -30 -318 -8 l-99 16 38 23 c98 59 430 88 652 59z m661 -64 c41 -11 140 -51 220 -90 232 -112 354 -150 479 -151 60 0 110 -23 130 -60 15 -28 -3 -34 -98 -28 -84 5 -118 17 -377 133 -245 109 -409 169 -520 192 l-40 8 55 6 c30 3 60 6 66 7 6 1 44 -7 85 -17z m-191 -21 c184 -45 312 -92 590 -218 203 -92 271 -112 374 -112 l83 0 12 -42 c19 -61 -5 -78 -105 -78 -91 0 -213 37 -393 120 -353 161 -586 218 -854 207 -206 -9 -293 -28 -565 -129 -184 -67 -277 -93 -444 -122 -266 -47 -393 -100 -506 -212 -70 -70 -73 -72 -67 -41 27 133 147 257 345 357 70 35 317 120 350 120 11 0 20 4 20 10 0 19 -185 -27 -304 -77 -249 -103 -408 -260 -435 -429 l-8 -49 -22 47 c-65 142 -20 319 118 455 92 90 129 105 280 115 69 5 157 16 196 24 38 9 88 20 110 24 26 6 76 3 140 -6 144 -20 194 -18 371 16 291 56 541 63 714 20z m-1751 -118 c-167 -108 -260 -318 -215 -490 15 -60 10 -63 -39 -21 -137 121 -116 325 48 463 40 33 62 43 130 57 118 25 128 24 76 -9z m-319 -7 c0 -3 -12 -23 -26 -44 -76 -115 -71 -284 12 -406 14 -21 23 -39 20 -42 -8 -8 -209 77 -279 118 -79 47 -195 143 -228 190 l-22 32 44 38 c103 88 191 117 362 118 64 1 117 -1 117 -4z m87 -35 c-52 -46 -80 -88 -113 -169 l-24 -56 0 55 c0 106 71 210 143 210 l40 0 -46 -40z m2023 -9 c133 -30 297 -86 447 -153 189 -83 272 -115 340 -133 65 -17 195 -20 223 -5 46 25 33 -92 -13 -121 -89 -54 -197 -37 -562 86 -613 207 -907 192 -1616 -83 -346 -134 -546 -180 -679 -155 -56 11 -59 25 -20 86 79 118 263 204 540 252 170 30 267 57 475 133 275 100 399 123 630 117 100 -3 179 -11 235 -24z m-25 -221 c118 -17 230 -49 461 -131 315 -111 466 -134 564 -86 l30 15 0 -39 c0 -102 -51 -139 -191 -139 -92 0 -140 11 -359 81 -189 60 -404 106 -559 119 -294 24 -807 -2 -1095 -56 -43 -9 -80 -13 -83 -11 -11 12 414 163 567 201 211 52 485 71 665 46z m125 -223 c80 -15 206 -45 280 -68 74 -23 178 -55 230 -71 109 -34 284 -49 341 -29 44 15 46 9 16 -42 -81 -138 -129 -144 -497 -64 -91 20 -237 50 -324 67 -112 21 -230 54 -395 109 -130 44 -272 87 -316 98 -117 26 -67 32 255 29 233 -3 282 -6 410 -29z m-670 -22 c58 -13 206 -57 330 -99 221 -74 708 -185 959 -219 85 -11 80 -16 -39 -45 -344 -84 -646 -47 -1075 133 -337 141 -440 164 -765 172 l-225 6 120 23 c102 20 203 35 395 58 65 8 194 -5 300 -29z m-287 -99 c119 -22 248 -64 442 -146 341 -143 474 -174 750 -174 205 -1 296 12 435 59 40 14 76 22 79 19 9 -9 -39 -91 -75 -128 -102 -105 -574 -147 -899 -80 -161 33 -255 68 -525 192 -311 142 -425 181 -695 237 -135 27 -137 29 -50 36 136 11 442 3 538 -15z m-538 -32 c212 -52 354 -127 561 -295 225 -183 427 -284 674 -336 105 -22 421 -25 515 -5 77 17 186 52 203 65 27 21 23 -4 -7 -42 -162 -204 -422 -296 -741 -261 -347 37 -617 160 -865 392 -282 264 -449 367 -681 422 -39 10 -83 28 -100 41 l-29 24 85 2 c47 0 108 4 135 9 62 10 171 3 250 -16z m440 -116 c44 -17 182 -77 307 -134 345 -156 506 -202 773 -219 165 -11 479 21 580 59 40 15 -112 -120 -160 -143 -91 -43 -229 -72 -375 -78 -350 -15 -682 97 -955 323 -103 85 -177 139 -267 194 -49 29 -88 54 -88 56 0 5 104 -27 185 -58z m-696 7 c186 -58 302 -132 506 -320 189 -174 291 -250 428 -318 192 -96 385 -144 617 -156 l125 -6 -50 -27 c-212 -111 -502 -78 -860 97 -237 116 -389 232 -589 451 -61 65 -155 165 -210 222 -64 65 -92 100 -76 94 14 -6 63 -22 109 -37z m69 -200 c123 -136 327 -327 409 -385 272 -190 569 -308 827 -327 80 -6 87 -8 70 -21 -89 -67 -284 -59 -532 24 -328 109 -582 307 -743 581 -150 253 -143 241 -108 208 17 -16 52 -52 77 -80z m-57 -22 c135 -249 285 -418 484 -546 195 -125 428 -208 654 -233 62 -7 63 -7 37 -21 -202 -106 -524 -43 -800 155 -205 148 -385 437 -421 675 -10 64 -4 60 46 -30z m-985 -99 c45 -8 74 -21 74 -34 0 -44 169 -379 265 -525 39 -58 -268 251 -329 332 -59 78 -136 212 -150 260 l-7 22 54 -24 c30 -13 72 -27 93 -31z m964 -39 c117 -333 395 -596 740 -701 71 -21 73 -23 34 -23 -46 -1 -199 36 -284 68 -283 107 -506 394 -527 676 -7 97 -3 95 37 -20z M3240 8258 c-36 -25 -102 -54 -190 -84 -161 -54 -228 -90 -335 -180 -97 -81 -158 -110 -337 -158 -236 -63 -337 -115 -447 -231 -67 -71 -58 -86 12 -18 117 113 186 146 504 237 151 43 197 67 294 149 101 85 160 117 304 167 132 45 185 73 229 119 42 44 30 44 -34 -1z M4372 8207 c-12 -13 -44 -34 -70 -48 -50 -26 -56 -26 -307 -18 -126 4 -224 -18 -257 -59 -11 -13 -10 -14 2 -10 106 40 201 52 332 40 167 -16 246 4 312 76 26 30 15 48 -12 19z M6410 8103 c0 -3 13 -27 29 -52 27 -42 71 -155 71 -181 0 -8 -6 -8 -20 0 -30 16 -112 29 -143 23 -36 -7 -16 -23 29 -23 137 -1 271 -142 330 -350 27 -97 25 -264 -4 -351 -41 -119 -76 -169 -289 -406 -186 -206 -224 -281 -249 -490 -24 -204 -66 -296 -222 -486 -65 -80 -77 -114 -21 -61 153 144 257 349 274 537 14 153 62 275 133 339 70 64 154 73 335 37 l108 -22 82 21 c45 12 116 31 157 43 118 35 333 33 453 -4 316 -97 525 -313 601 -622 58 -236 69 -730 21 -921 -33 -129 -89 -219 -220 -354 -66 -69 -150 -158 -185 -199 -120 -138 -274 -240 -409 -271 -33 -8 -149 -13 -310 -15 -196 -1 -276 -6 -341 -19 -228 -46 -249 -49 -385 -48 -137 0 -368 40 -325 55 8 3 78 18 155 32 277 54 464 128 559 222 64 63 50 65 -31 4 -147 -110 -470 -203 -873 -251 -92 -11 -155 -30 -346 -104 -207 -80 -446 -86 -664 -17 -110 35 -125 20 -19 -19 l62 -23 -40 -12 c-41 -12 -81 -33 -228 -119 -102 -59 -150 -73 -310 -92 -217 -26 -294 -61 -430 -198 -102 -103 -294 -350 -342 -441 -67 -128 -79 -265 -32 -374 28 -66 42 -57 18 11 -55 158 -10 306 155 513 40 50 103 129 140 178 138 178 257 249 461 273 205 24 269 44 395 125 42 26 102 61 135 76 l60 28 190 -3 c257 -4 328 8 530 91 196 80 300 88 525 42 185 -38 342 -39 530 -3 227 44 309 51 498 44 205 -8 276 -20 437 -77 150 -53 274 -60 359 -21 62 28 54 38 -15 18 -101 -30 -189 -21 -344 34 -44 16 -102 36 -130 43 l-50 14 45 12 c138 39 253 122 421 303 218 235 284 284 409 304 61 10 50 28 -12 20 -29 -3 -65 -9 -80 -12 l-27 -6 40 81 c70 141 83 226 82 516 -1 568 -92 839 -345 1040 -254 201 -531 250 -871 154 -123 -35 -182 -37 -299 -13 -58 11 -106 15 -160 11 -43 -4 -78 -4 -78 -2 0 2 43 53 97 112 201 225 255 322 272 492 8 88 29 100 186 112 136 10 234 48 304 118 60 60 50 67 -21 13 -97 -73 -149 -92 -288 -101 -85 -6 -127 -14 -146 -26 -32 -21 -29 -26 -49 72 -27 137 -72 232 -153 326 -22 25 -48 67 -57 94 -43 116 -95 213 -115 213 -6 0 -10 -3 -10 -7z M3805 7762 c-71 -65 -146 -110 -239 -145 -78 -29 -87 -30 -261 -32 -324 -4 -494 -75 -688 -288 -50 -56 -158 -167 -238 -247 -150 -149 -215 -229 -274 -334 -40 -73 -78 -193 -73 -233 4 -25 5 -24 16 17 52 189 114 281 357 530 105 107 212 221 239 253 179 212 542 323 829 253 38 -9 75 -16 83 -16 25 0 6 18 -26 25 -44 10 -37 19 28 39 111 35 259 135 293 198 18 33 6 28 -46 -20z M2102 7349 c-226 -29 -414 -142 -551 -331 -52 -72 -76 -85 -231 -119 -288 -65 -512 -233 -630 -474 -61 -126 -74 -191 -66 -337 16 -299 36 -323 33 -40 -2 225 9 281 80 401 138 231 346 373 628 427 43 9 88 20 99 26 15 8 18 7 13 -3 -38 -85 -126 -316 -123 -324 2 -5 46 78 99 185 53 110 117 224 145 261 147 193 342 292 605 306 123 6 148 16 71 27 -55 7 -87 6 -172 -5z M676 5098 c-185 -212 -171 -500 30 -639 54 -37 71 -32 24 8 -195 164 -210 388 -41 612 37 50 48 71 37 71 -3 0 -25 -24 -50 -52z';

    function brainDrawing() {
      const box = svg('svg', {viewBox: BRAIN_VIEWBOX, class: 'hero-brain', focusable: 'false', 'aria-hidden': 'true'});
      const group = svg('g', {transform: BRAIN_TRANSFORM, fill: 'currentColor', stroke: 'none'});
      group.appendChild(svg('path', {d: BRAIN_PATH}));
      box.appendChild(group);
      return box;
    }

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
        h('div', {class: 'hero-text'},
          h('h1', {class: 'hero-title', tabindex: '-1', 'data-heading': ''}, 'Vision experiments you can rerun, byte for byte.'),
          h('p', {class: 'lede'}, 'Pinned releases your rigs install and run offline.'),
          actions),
        h('div', {class: 'hero-figure'}, brainDrawing())));
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

    function screenCatalog(ctx) {
      const r = ctx.route;
      const section = screenShell('Catalogue', 'Published experiments',
        'Pinned releases, published by their authors.');
      const q = input({type: 'search', name: 'q', value: r.q || '', autocomplete: 'off', maxlength: '200', 'data-focus': 'catalog-q'});
      const form = h('form', {class: 'search', role: 'search'},
        field('Search the catalogue', q),
        h('button', {type: 'submit', class: 'btn btn-primary', 'data-focus': 'catalog-go'}, 'Search'));
      form.addEventListener('submit', (event) => {
        prevent(event);
        go({view: 'catalog', q: q.value}, {focus: 'catalog-q'});
      });
      section.appendChild(form);
      const results = region('the catalogue');
      section.appendChild(results.el);
      const offset = r.offset || 0;
      (async () => {
        const got = await screenRequest(ctx.epoch, 'GET', '/catalog', {query: {query: r.q, limit: PAGE, offset}});
        if (!got) return;
        if (!got.ok) return results.fail(got.error, retryCurrent);
        const items = (got.value && got.value.items) || [];
        if (!items.length) {
          return results.fill(r.q
            ? emptyState(`No published experiment matches \u201c${r.q}\u201d.`, 'Try fewer or different words.',
              link({view: 'catalog'}, 'Show everything', {class: 'btn btn-line'}))
            : emptyState('The catalogue is empty.',
              'Nothing has been published yet. An author publishes one release at a time, with a licence, after checking it holds no participant data.'));
        }
        results.fill(
          h('p', {class: 'count mono'}, C.nextOffsetLabel(offset, items.length)),
          h('div', {class: 'listing-grid'}, items.map((item) => listing(item))),
          pager(offset, items.length, got.value.next_offset, (o) => ({view: 'catalog', q: r.q, offset: o}), 'catalog'));
      })();
      return section;
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
        body.fill(emptyState('No experiment chosen.', null, link({view: 'catalog'}, 'Open the catalogue', {class: 'btn btn-line'})));
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
              link({view: 'catalog'}, 'Back to the catalogue', {class: 'btn btn-line'})));
          }
          return body.fail(got.error, retryCurrent);
        }
        const experiment = (got.value && got.value.experiment) || {};
        const versions = sortVersions(got.value && got.value.versions);
        const version = chooseVersion(experiment, versions, r.version);
        const tab = r.tab || 'overview';
        body.fill(experimentHead(experiment, version, versions), experimentTabs(r, tab, versions.length),
          experimentTab(ctx, tab, experiment, version, versions));
      })();
      return section;
    }

    function experimentHead(experiment, version, versions) {
      const published = experiment.published_version_id;
      let status;
      if (version && published && version.id === published) status = h('span', {class: 'chip chip-public'}, 'Public release');
      else if (isOwner(experiment)) status = h('span', {class: 'chip chip-private'}, 'Private: only you can see this version');
      return h('header', {class: 'screen-head exp-head'},
        h('p', {class: 'eyebrow mono'}, link({view: 'catalog'}, 'Catalogue'), h('span', {'aria-hidden': 'true'}, ' / '),
          h('span', null, version ? versionLabel(version) : 'no release')),
        h('h1', {class: 'screen-title', tabindex: '-1', 'data-heading': ''}, experiment.title || 'Untitled experiment'),
        ownerLine(experiment),
        experiment.summary ? h('p', {class: 'lede'}, experiment.summary) : null,
        h('div', {class: 'chips'}, status,
          ...(Array.isArray(experiment.tags) ? experiment.tags.slice(0, 12).map((t) => h('span', {class: 'chip chip-tag'}, t)) : []),
          versions.length > 1 ? h('span', {class: 'chip'}, versions.length + ' versions visible to you') : null));
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

    function overviewTab(ctx, experiment, version) {
      const grid = h('div', {class: 'exp-grid'});
      const reading = h('div', {class: 'exp-reading'});
      reading.appendChild(h('h2', {class: 'block-title'}, 'About'));
      reading.appendChild(experiment.description
        ? h('div', {class: 'prose'}, ...String(experiment.description).split(/\n{2,}/).map((p) => h('p', null, p)))
        : h('p', {class: 'muted'}, 'The author has not written a description.'));
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
      grid.appendChild(reading);
      grid.appendChild(releaseSheet(ctx, experiment, version));
      return grid;
    }

    function releaseSheet(ctx, experiment, version) {
      const aside = h('aside', {class: 'sheet', 'aria-label': 'Release'});
      if (!version) {
        aside.appendChild(h('h2', {class: 'sheet-title'}, 'No release yet'));
        aside.appendChild(h('p', {class: 'muted'}, isOwner(experiment)
          ? 'Add a version from My experiments.' : 'There is no release you can see.'));
        if (isOwner(experiment)) aside.appendChild(link({view: 'mine', id: experiment.id}, 'Manage', {class: 'btn btn-line'}));
        return aside;
      }
      const m = version.manifest || {};
      aside.appendChild(h('h2', {class: 'sheet-title'}, h('span', null, 'Release'), h('span', {class: 'mono sheet-version'}, versionLabel(version))));
      aside.appendChild(spec([
        ['Licence', experiment.license || m.license || 'not stated'],
        ['Needs', hardwareLamps(m)],
        ['Runs on', C.platformsText(m)],
        ['Python', m.python_min ? '\u2265 ' + m.python_min : null, {mono: true}],
        ['alhazen', m.alhazen_min ? '\u2265 ' + m.alhazen_min : null, {mono: true}],
        ['Entry point', m.entrypoint, {mono: true}],
        ['Files', Array.isArray(m.files) ? String(m.files.length) : null],
        ['Archive', C.formatBytes(version.size)],
        ['Released', C.formatDate(version.created_at)],
        ['SHA-256', digest(version.sha256)],
      ]));
      aside.appendChild(libraryAction(ctx, experiment, version));
      if (state.role === 'rig') aside.appendChild(installPanel(experiment, version));
      else aside.appendChild(downloadAction(experiment, version));
      if (isOwner(experiment)) {
        aside.appendChild(h('p', {class: 'sheet-owner'}, link({view: 'mine', id: experiment.id}, 'Manage this experiment', {class: 'btn btn-quiet'})));
      }
      return aside;
    }

    function libraryAction(ctx, experiment, version) {
      const box = h('div', {class: 'sheet-block'});
      if (!state.user) {
        box.appendChild(h('p', {class: 'muted'}, 'Sign in to pin this release in your library.'));
        box.appendChild(link({view: 'signin', next: currentNext()}, 'Sign in', {class: 'btn btn-line'}));
        return box;
      }
      const status = statusLine();
      const button = h('button', {type: 'button', class: 'btn btn-line', 'data-focus': 'pin'}, 'Pin ' + versionLabel(version) + ' in my library');
      const pinned = h('p', {class: 'pin-state'});
      box.append(pinned, button, status.el);
      const paint = (items) => {
        const entry = items.find((i) => i && i.experiment && i.experiment.id === experiment.id);
        const pinnedId = entry && entry.version ? entry.version.id : null;
        if (pinnedId === version.id) {
          pinned.textContent = 'In your library, pinned to ' + versionLabel(version) + '.';
          button.hidden = true;
        } else {
          pinned.textContent = entry ? 'Your library pins ' + versionLabel(entry.version) + '.' : 'Not in your library.';
          button.hidden = false;
        }
      };
      libraryItems(false).then((items) => { if (ctx.epoch === state.epoch) paint(items); })
        .catch((exc) => { if (ctx.epoch === state.epoch) { noteFailure(exc); pinned.textContent = 'Your library could not be read: ' + exc.message; } });
      button.addEventListener('click', async () => {
        button.disabled = true;
        status.show('Pinning\u2026', 'info');
        try {
          await api('POST', '/library', {json: {experiment_id: experiment.id, version_id: version.id}});
          const items = await libraryItems(true);
          if (ctx.epoch !== state.epoch) return;
          paint(items);
          status.show('Pinned ' + versionLabel(version) + '. Other versions are not followed automatically.', 'ok');
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
        h('a', {class: 'btn btn-line', href, download: ''}, 'Download the release archive'),
        h('p', {class: 'muted small'}, 'The hub never runs experiments. To run this one, install it from your library on a rig '
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
          : 'Trusting it lets alhazen import and run it as your operating-system user, with your access to files, '
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
      const section = screenShell('Guide', 'How alhazen runs an experiment',
        'Modes, protections and what each choice records, taken from the alhazen version this '
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
      const section = screenShell('Library', 'Your library',
        'Releases you pinned. Nothing upgrades on its own.');
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
            'Open an experiment in the catalogue and pin the release you want. '
            + (state.role === 'rig' ? 'You can then install it on this rig.' : 'A rig signed in to this account can then install it.'),
            link({view: 'catalog'}, 'Browse the catalogue', {class: 'btn btn-primary'})));
        }
        area.fill(h('div', {class: 'listing-grid'}, items.map((item) => listing(item, libraryExtra(item)))));
      })();
      return section;
    }

    function libraryExtra(item) {
      const experiment = item.experiment || {};
      const version = item.version || {};
      const row = h('div', {class: 'listing-actions'});
      row.appendChild(h('span', {class: 'small muted', title: String(version.sha256 || '')}, 'SHA-256 ', h('span', {class: 'mono'}, C.shortHash(version.sha256))));
      if (state.role === 'rig') {
        const record = installFor(version.sha256);
        const open = record && record.status === 'registered' && !record.error ? workspaceLink(record, 'Open in the workspace') : null;
        row.appendChild(open || link({view: 'experiment', id: experiment.id, version: version.id}, record ? 'Finish installing' : 'Install on this rig', {class: 'btn btn-line'}));
      } else if (experiment.id && version.id) {
        row.appendChild(h('a', {class: 'btn btn-line', download: '',
          href: state.api.url('/experiments/' + C.seg(experiment.id) + '/versions/' + C.seg(version.id) + '/download')}, 'Download'));
      }
      return row;
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
        h('p', {class: 'muted'}, 'Upload a release archive made with alhazen (alhazen-package.json inside). '
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
      data: screenData, rig: screenRig,
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
