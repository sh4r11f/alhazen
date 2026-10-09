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
        const link = rigLink();
        const lamp = link.state === 'connected' ? 'ok' : (link.state === 'unreachable' ? 'err' : 'idle');
        el.replaceChildren(h('span', {class: 'lamp lamp-' + lamp, 'aria-hidden': 'true'}),
          h('span', {class: 'role-word'}, 'Rig'), h('span', {class: 'role-detail'}, link.where));
        el.setAttribute('title', 'This page is served by this rig\u2019s dashboard. ' + link.sentence);
      } else if (state.role === 'server') {
        el.replaceChildren(h('span', {class: 'lamp lamp-ok', 'aria-hidden': 'true'}),
          h('span', {class: 'role-word'}, 'Hub'), h('span', {class: 'role-detail'}, 'catalogue and accounts'));
        el.setAttribute('title', 'The central hub. It never runs experiments or rig hardware.');
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
          h('span', {class: 'who-label'}, state.role === 'rig' ? 'Operator' : 'Signed in'),
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
      const title = node.querySelector('[data-heading]');
      doc.title = (title ? title.textContent + ' · ' : '') + 'Alhazen Experiment Hub';
      settleFocus(false);
    }

    function flashNode() {
      const f = state.flash;
      state.flash = null;
      if (!f) return null;
      return h('p', {class: 'note note-' + (f.tone || 'info'), role: 'status'}, f.text);
    }

    function screenShell(eyebrow, title, lede) {
      const section = h('section', {class: 'screen'});
      const head = h('header', {class: 'screen-head'},
        eyebrow ? h('p', {class: 'eyebrow'}, eyebrow) : null,
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
          settleFocus(true);
        },
        fail(error, retry) {
          el.setAttribute('aria-busy', 'false');
          el.replaceChildren(errorBox(error, retry));
          settleFocus(true);
        },
      };
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

    function select(options, value, attrs) {
      const el = h('select', Object.assign({id: nextId('s'), class: 'input select'}, attrs));
      for (const [v, label] of options) {
        const opt = h('option', {value: v}, label);
        if (String(v) === String(value)) opt.selected = true;
        el.appendChild(opt);
      }
      el.value = value === undefined || value === null ? (options[0] ? options[0][0] : '') : String(value);
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
      const manifest = (version && version.manifest) || {};
      const card = h('article', {class: 'listing'});
      const head = h('header', {class: 'listing-head'},
        h('h3', {class: 'listing-title'},
          link({view: 'experiment', id: experiment.id, version: version ? version.id : undefined}, experiment.title || 'Untitled experiment')),
        version ? h('span', {class: 'chip chip-version mono'}, versionLabel(version)) : null);
      card.appendChild(head);
      const by = ownerLine(experiment);
      if (by) card.appendChild(by);
      if (experiment.summary) card.appendChild(h('p', {class: 'listing-summary'}, experiment.summary));
      const meta = h('div', {class: 'listing-meta'},
        hardwareLamps(manifest, true),
        h('span', {class: 'meta-item'}, h('span', {class: 'meta-key'}, 'Licence'),
          h('span', {class: 'meta-val'}, experiment.license || manifest.license || 'not stated')),
        manifest.platforms ? h('span', {class: 'meta-item'}, h('span', {class: 'meta-key'}, 'Runs on'),
          h('span', {class: 'meta-val'}, C.platformsText(manifest))) : null);
      card.appendChild(meta);
      if (extra) card.appendChild(extra);
      return card;
    }

    /* ---- home ------------------------------------------------------------ */

    function pathGlyph(kind) {
      const box = svg('svg', {viewBox: '0 0 48 48', width: '40', height: '40', class: 'path-glyph', focusable: 'false', 'aria-hidden': 'true'});
      const line = {fill: 'none', stroke: 'currentColor', 'stroke-width': '2', 'stroke-linecap': 'round', 'stroke-linejoin': 'round'};
      if (kind === 'experiment') {
        box.appendChild(svg('path', Object.assign({d: 'M24 5l16 9v20l-16 9-16-9V14z'}, line)));
        box.appendChild(svg('path', Object.assign({d: 'M8 14l16 9 16-9M24 23v20'}, line)));
      } else if (kind === 'rig') {
        box.appendChild(svg('rect', Object.assign({x: '6', y: '8', width: '36', height: '24', rx: '3'}, line)));
        box.appendChild(svg('path', Object.assign({d: 'M18 40h12M24 32v8'}, line)));
        box.appendChild(svg('circle', Object.assign({cx: '24', cy: '20', r: '5'}, line)));
        box.appendChild(svg('circle', {cx: '24', cy: '20', r: '1.8', fill: 'currentColor'}));
      } else {
        box.appendChild(svg('rect', Object.assign({x: '7', y: '8', width: '34', height: '32', rx: '3'}, line)));
        box.appendChild(svg('path', Object.assign({d: 'M7 17h34M7 26h34M7 35h34M19 8v32'}, line)));
      }
      return box;
    }

    /* The page's one signature: the path a study takes, as three stations on
     * a signal rail. The words are the hub's actual guarantees. */
    function signalPath() {
      const stages = [
        ['experiment', '01', 'Experiment',
          'Published as an immutable release: manifest, licence, citations and a SHA-256 for every file.'],
        ['rig', '02', 'Rig',
          'Installed only after you trust its code, run with your own interpreter, calibration and rig file. The hub never runs it.'],
        ['data', '03', 'Data',
          'Sessions stay on the rig. You preview one and opt in; the hub verifies every file before it counts as received.'],
      ];
      const list = h('ol', {class: 'signal', 'aria-label': 'How an experiment travels'});
      for (const [kind, n, name, text] of stages) {
        list.appendChild(h('li', {class: 'station station-' + kind},
          h('span', {class: 'station-node'}, pathGlyph(kind)),
          h('span', {class: 'station-num mono'}, n),
          h('span', {class: 'station-name'}, name),
          h('span', {class: 'station-text'}, text)));
      }
      return h('figure', {class: 'signal-figure'}, h('span', {class: 'signal-rail', 'aria-hidden': 'true'},
        h('span', {class: 'signal-pulse'})), list);
    }

    function screenHome(ctx) {
      const section = h('section', {class: 'screen screen-home'});
      const actions = h('div', {class: 'actions'});
      actions.appendChild(link({view: 'catalog'}, 'Browse the catalogue', {class: 'btn btn-primary'}));
      if (state.user) {
        actions.appendChild(link({view: 'library'}, 'Your library', {class: 'btn btn-line'}));
        actions.appendChild(link({view: 'mine'}, 'My experiments', {class: 'btn btn-line'}));
      } else {
        actions.appendChild(link({view: 'signin'}, 'Sign in', {class: 'btn btn-line'}));
        actions.appendChild(link({view: 'register'}, 'Register with an invite', {class: 'btn btn-quiet'}));
      }
      actions.appendChild(link({view: 'guide'}, 'How alhazen runs a session', {class: 'btn btn-quiet'}));
      section.appendChild(h('div', {class: 'hero'},
        h('p', {class: 'eyebrow mono'}, 'Alhazen · Experiment Hub'),
        h('h1', {class: 'hero-title', tabindex: '-1', 'data-heading': ''}, 'Share the experiment. Keep the rig and the data yours.'),
        h('p', {class: 'lede'}, 'A catalogue of vision-science experiments as pinned, checksummed code. '
          + 'Rigs install the exact release they trust and run it locally; collected sessions leave a rig only when you choose.'),
        actions));
      const flash = flashNode();
      if (flash) section.appendChild(flash);
      section.appendChild(signalPath());
      if (state.role === 'rig') section.appendChild(rigStrip());

      const recent = region('the latest releases');
      section.appendChild(h('section', {class: 'block'},
        h('div', {class: 'block-head'},
          h('h2', {class: 'block-title'}, 'Recently published'),
          link({view: 'catalog'}, 'Whole catalogue', {class: 'block-link'})),
        recent.el));
      (async () => {
        const got = await screenRequest(ctx.epoch, 'GET', '/catalog', {query: {limit: 6}});
        if (!got) return;
        if (!got.ok) return recent.fail(got.error, retryCurrent);
        const items = (got.value && got.value.items) || [];
        if (!items.length) {
          return recent.fill(emptyState('Nothing is published yet.',
            'Releases appear here only after their author publishes one explicitly, with a licence and a check that no participant data is inside. '
            + 'Private experiments and every collected session stay out of the catalogue.',
            state.user ? link({view: 'mine'}, 'Publish one of yours', {class: 'btn btn-line'}) : null));
        }
        recent.fill(h('div', {class: 'listing-grid'}, items.map((item) => listing(item))));
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
        'Every entry is one pinned release its author chose to publish. Search titles, summaries, tags and authors.');
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
        doc.title = (experiment.title || 'Experiment') + ' · Alhazen Experiment Hub';
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
        ['Files', Array.isArray(m.files) ? String(m.files.length) : null, {mono: true}],
        ['Archive', C.formatBytes(version.size), {mono: true}],
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
        h('p', {class: 'muted small'}, 'This hub stores and lists code; it never runs experiments or touches rig hardware. '
          + 'To run this one, open the hub page on a rig computer (alhazen dashboard --hub), sign in and install it from your library.'));
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
        h('p', null, 'Trusting it lets alhazen import and run it as your operating-system user, with your access to files, '
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

    function screenSignin(ctx) {
      const r = ctx.route;
      const flash = state.flash;
      const section = screenShell(state.role === 'rig' ? 'This rig' : 'Account', 'Sign in',
        state.role === 'rig'
          ? 'Sign in to the hub this rig is connected to. The rig keeps the sign-in in a private file; this page never sees it.'
          : 'Sign in to keep a library, upload experiments and see the sessions you collected.');
      if (state.user) {
        section.appendChild(h('p', {class: 'note note-info'}, 'You are signed in as ' + (state.user.display_name || state.user.username) + '.'));
        section.appendChild(link({view: 'home'}, 'Go to the start page', {class: 'btn btn-line'}));
        return section;
      }
      if (state.role === 'rig' && state.local && state.local.state === 'not_configured') {
        section.appendChild(h('div', {class: 'callout callout-info'},
          h('p', null, 'Connect this rig to a hub first; then sign in to it.'),
          link({view: 'rig', tab: 'connection'}, 'Connect this rig', {class: 'btn btn-primary'})));
        return section;
      }
      const username = input({type: 'text', name: 'username', autocomplete: 'username', autocapitalize: 'none',
        spellcheck: 'false', required: true, maxlength: '64', value: (flash && flash.username) || '', 'data-focus': 'signin-user'});
      const password = input({type: 'password', name: 'password', autocomplete: 'current-password', required: true, maxlength: '1024'});
      const status = statusLine();
      const button = h('button', {type: 'submit', class: 'btn btn-primary'}, 'Sign in');
      const form = h('form', {class: 'form form-narrow'}, field('Username', username), field('Password', password), button, status.el,
        h('p', {class: 'muted small'}, 'No account? ', link({view: 'register'}, 'Register with an invite code')));
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
      section.appendChild(form);
      return section;
    }

    function screenRegister() {
      const section = screenShell('Account', 'Register',
        'Registration needs an invite code from the hub\u2019s operator. No e-mail is sent; you sign in straight after.');
      if (state.user) {
        section.appendChild(h('p', {class: 'note note-info'}, 'You are already signed in as ' + (state.user.display_name || state.user.username) + '.'));
        return section;
      }
      if (state.role === 'rig' && state.local && state.local.state === 'not_configured') {
        section.appendChild(h('div', {class: 'callout callout-info'},
          h('p', null, 'Connect this rig to a hub first; the account is created there.'),
          link({view: 'rig', tab: 'connection'}, 'Connect this rig', {class: 'btn btn-primary'})));
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
        field('Password', password, '12 to 1024 characters.'),
        field('Repeat the password', repeat),
        field('Invite code', invite),
        button, status.el);
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
      section.appendChild(form);
      return section;
    }

    /* ---- library -------------------------------------------------------- */

    function screenLibrary(ctx) {
      const section = screenShell('Library', 'Your library',
        'Releases you pinned. A pin stays on its version until you choose another; nothing upgrades on its own.');
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
      row.appendChild(h('span', {class: 'mono small', title: String(version.sha256 || '')}, 'SHA-256 ' + C.shortHash(version.sha256)));
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
        'Everything here is private until you publish a release. Collected data is never part of an experiment.');
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
      const status = s.index_status || '';
      if (!status) return '';
      if (status === 'indexed' || status === 'complete' || status === 'completed') return 'trials indexed';
      if (status === 'failed' || status === 'error') return 'trial index failed';
      return 'trials ' + status;
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
        for (const item of await libraryItems(false)) {
          if (item.experiment && item.experiment.id) names.set(item.experiment.id, item.experiment.title || item.experiment.id);
        }
      } catch (exc) { noteFailure(exc); }
      return names;
    }

    function screenData(ctx) {
      if (ctx.route.session) return screenSession(ctx);
      const r = ctx.route;
      const section = screenShell('Data', 'Your sessions',
        'Sessions you uploaded and the hub verified. Only you can see them; an experiment\u2019s author has no access to your data.');
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
      const offset = r.offset || 0;
      (async () => {
        const [got, names] = await Promise.all([
          screenRequest(ctx.epoch, 'GET', '/data/sessions', {query: {
            experiment_id: r.experiment, subject_code: r.subject, mode: r.mode, limit: PAGE, offset}}),
          experimentNames(),
        ]);
        if (!got || ctx.epoch !== state.epoch) return;
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
            h('td', null, names.get(s.experiment_id) || String(s.experiment_id || '')),
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
        const base = '/data/sessions/' + C.seg(s.id || r.session);
        const trials = region('trials');
        area.fill(
          h('header', {class: 'screen-head'},
            h('p', {class: 'eyebrow mono'}, link(back, 'Your sessions'), h('span', {'aria-hidden': 'true'}, ' / '), h('span', null, String(s.id || r.session))),
            h('h1', {class: 'screen-title', tabindex: '-1', 'data-heading': ''}, 'Session ' + (m.subject_code ? m.subject_code + ' \u00b7 ' : '') + (sessionWhen(s) || '')),
            h('p', {class: 'lede'}, 'Raw files as the rig recorded them, verified on arrival. Trial rows below are derived from them and can be rebuilt.')),
          h('div', {class: 'exp-grid'},
            h('div', {class: 'exp-reading'},
              h('h2', {class: 'block-title'}, 'Trials'),
              s.index_status === 'failed' || s.index_status === 'error'
                ? h('p', {class: 'note note-warn'}, 'The trial table could not be indexed' + (s.index_error ? ': ' + (s.index_error.message || s.index_error) : '') + '. The raw files are kept and can be downloaded below.')
                : null,
              trials.el,
              h('h2', {class: 'block-title'}, 'Files'),
              artifactTable(base, artifacts)),
            h('aside', {class: 'sheet'},
              h('h2', {class: 'sheet-title'}, 'Session'),
              spec([
                ['Status', s.status],
                ['Subject', m.subject_code, {mono: true}],
                ['Mode', m.mode],
                ['Rig', m.rig_alias],
                ['Started', C.formatDate(m.started_at)],
                ['Received', C.formatDate(s.completed_at)],
                ['Release', s.version_id, {mono: true}],
                ['Manifest SHA-256', digest(s.manifest_sha256 || (receipt && receipt.manifest_sha256))],
              ]),
              h('p', {class: 'muted small'}, 'The receipt means the hub\u2019s primary storage verified every file. It is not a backup; keep the rig\u2019s originals.'),
              h('div', {class: 'sheet-block'},
                h('h3', {class: 'sub-title'}, 'Export the trial table'),
                h('div', {class: 'actions'},
                  h('a', {class: 'btn btn-line', download: '', href: state.api.url(base + '/export', {format: 'csv'})}, 'CSV'),
                  h('a', {class: 'btn btn-line', download: '', href: state.api.url(base + '/export', {format: 'json'})}, 'JSON'))))));
        loadTrials(ctx, base, trials, back, s.id || r.session);
      })();
      return section;
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

    async function loadTrials(ctx, base, trials, back, sessionId) {
      const r = ctx.route;
      const offset = r.toffset || 0;
      const got = await screenRequest(ctx.epoch, 'GET', base + '/trials', {query: {limit: PAGE, offset}});
      if (!got) return;
      if (!got.ok) return trials.fail(got.error, retryCurrent);
      const rows = (got.value && got.value.items) || [];
      if (!rows.length) {
        return trials.fill(h('p', {class: 'muted'}, offset ? 'No more trials.' : 'No trial rows: this session has no supported trials.csv, or indexing has not finished.'));
      }
      const columns = C.trialColumns(rows);
      const table = h('table', {class: 'table table-dense'},
        h('caption', {class: 'visually-hidden'}, 'Trials'),
        h('thead', null, h('tr', null, ...columns.map((c) => h('th', {scope: 'col', class: 'mono'}, c)))));
      const tbody = h('tbody');
      for (const row of rows) tbody.appendChild(h('tr', null, ...columns.map((c) => h('td', {class: 'mono'}, C.cellText(row[c])))));
      table.appendChild(tbody);
      trials.fill(h('div', {class: 'table-wrap table-scroll', tabindex: '0', role: 'region', 'aria-label': 'Trial rows'}, table),
        pager(offset, rows.length, got.value.next_offset,
          (o) => Object.assign({}, back, {session: sessionId, toffset: o}), 'trials'));
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
          h('td', null, word),
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
          h('thead', null, h('tr', null, ...['Session', 'Folder', 'Task', 'State', ''].map((t) => h('th', {scope: 'col'}, t)))));
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
            h('td', null, s.root_name || s.root_kind || ''),
            h('td', null, s.task || ''),
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
        if (release) Object.assign(body, release);
        try {
          const preview = await api('POST', '/local/upload-preview', {json: body, timeoutMs: 120000});
          if (ctx.epoch !== state.epoch) return;
          previewArea.replaceChildren(consentForm(ctx, project, session, preview, status, runPreview));
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
          if (experiment_id && version_id) onPick({experiment_id, version_id});
        });
        wrap.replaceChildren(field('Record against', choice), button, status.el);
      }).catch((exc) => { noteFailure(exc); wrap.replaceChildren(errorBox(exc)); });
      return wrap;
    }

    /* The consent: who receives it (hub and account), which release it is
     * recorded against, exactly which files (and their manifest digest),
     * what personal information they can hold. It names this preview_id;
     * a change to any of these needs a new preview (rig-contract B2). */
    function consentForm(ctx, project, session, preview, status, again) {
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
        : String(p.experiment_id || '') + ' / ' + String(p.version_id || '');
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
        if (job.error && job.error.code === 'auth_context_changed') {
          detail.appendChild(h('p', {class: 'muted small'}, 'It continues only when the account it was approved for is signed in to the same hub.'));
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
