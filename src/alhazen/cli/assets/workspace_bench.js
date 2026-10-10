/* Alhazen experiment workspace: the Run page's presentation (Composite:
 * Split's shell, Bench's Run page, Console's session clock).
 *
 * Presentation only. It adds no route, request or state of its own, and
 * reads only the page:
 *
 *   - The Run page has two panes, Setup (the form and the launch bar fixed
 *     to the window's bottom) and Output (the session summary, the run
 *     output with the live monitor, the recent runs and the upload card).
 *     The choice is kept per tab (sessionStorage). Output opens by itself
 *     when a run starts (the Stop button appears) and when a recent run is
 *     opened. The Output button's lamp mirrors #run-status.
 *   - The session clock: the selected run's elapsed time ("mm:ss", or
 *     "h:mm:ss" from an hour on) and its start, from the data-started /
 *     data-finished / data-active attributes refreshRun() in workspace.js
 *     puts on #run-info; it ticks once a second while the run is active and
 *     a finished run shows how long it took. During a run the launch bar
 *     shows the same clock in the estimate's place, with View output.
 *   - Measure rig in the sidebar: an entry in the open experiment's pages,
 *     after Run, that opens the Run page with Measure rig chosen in the Mode
 *     menu (the menu's own change event runs, exactly as if the operator had
 *     picked it). Shown only when that menu offers Measure rig.
 *
 * window.WorkspaceBench = {clock(startISO, endISO, nowMs), startedAt(iso)}
 * for tests; nothing else depends on it, and the page works without it.
 */
(function () {
  'use strict';
  const doc = document;
  const $ = (id) => doc.getElementById(id);
  const PANE_KEY = 'alhazen-workspace-run-pane';

  /* ---- The clock ---- */

  /** "mm:ss", or "h:mm:ss" from an hour on, of the time between two
   *  instants (the end, or now when there is none); '00:00' when the start
   *  is missing or unreadable. */
  function clock(startISO, endISO, nowMs) {
    const start = Date.parse(startISO || '');
    if (!Number.isFinite(start)) return '00:00';
    const end = endISO ? Date.parse(endISO) : nowMs;
    const total = Math.max(0, Math.floor(((Number.isFinite(end) ? end : nowMs) - start) / 1000));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    const two = (n) => String(n).padStart(2, '0');
    return h ? `${h}:${two(m)}:${two(s)}` : `${two(m)}:${two(s)}`;
  }

  /** The start as the reader's local time of day, or an en dash. */
  function startedAt(startISO) {
    const start = Date.parse(startISO || '');
    if (!Number.isFinite(start)) return '–';
    return new Date(start).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  }

  function setText(id, text) {
    const el = $(id);
    if (el && el.textContent !== text) el.textContent = text;
  }

  function tick() {
    const info = $('run-info');
    if (!info) return;
    const started = info.dataset.started || '';
    const active = info.dataset.active === 'true';
    const text = clock(started, info.dataset.finished || '', Date.now());
    setText('session-elapsed', text);
    setText('bar-elapsed', text);
    setText('session-started', startedAt(started));
    const summary = $('session-summary');
    if (summary) summary.dataset.active = String(active);
    const bar = $('bar-session');
    if (bar) bar.hidden = !active;
    const estimate = $('duration-estimate');
    if (estimate) estimate.hidden = active;
  }

  /* ---- The panes ---- */

  function onRunPage() {
    return !$('workspace').hidden;
  }

  function syncBar() {
    doc.body.classList.toggle('bench-launch-bar',
      onRunPage() && $('workspace').dataset.pane === 'setup');
  }

  function setPane(pane) {
    $('workspace').dataset.pane = pane;
    $('pane-setup').setAttribute('aria-pressed', String(pane === 'setup'));
    $('pane-output').setAttribute('aria-pressed', String(pane === 'output'));
    try { sessionStorage.setItem(PANE_KEY, pane); } catch (e) { /* storage off */ }
    syncBar();
  }

  function mirrorLamp() {
    const status = $('run-status');
    const kind = ['running', 'stopping', 'completed', 'failed', 'interrupted']
      .find((k) => status.classList.contains(k)) || '';
    $('pane-output-lamp').className = 'pane-lamp' + (kind ? ' ' + kind : '');
  }

  let stopShown = false;
  function watchRunStart() {
    const shown = !$('stop').hidden;
    if (shown && !stopShown) setPane('output');
    stopShown = shown;
  }

  /* ---- Measure rig in the sidebar ---- */

  function measureOffered() {
    const mode = $('mode');
    return !!mode && [...mode.options].some((o) => o.value === 'measure');
  }

  function chooseMeasure() {
    const mode = $('mode');
    if (!measureOffered() || mode.value === 'measure') return;
    mode.value = 'measure';
    mode.dispatchEvent(new Event('change', {bubbles: true}));
  }

  function openMeasure(event, run) {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    event.preventDefault();
    const go = () => { chooseMeasure(); setPane('setup'); syncMeasure(); };
    if (run.classList.contains('selected') && onRunPage()) { go(); return; }
    run.click();
    // The Run page draws its form after the route settles.
    let tries = 0;
    const wait = () => {
      if (onRunPage() && measureOffered()) go();
      else if (tries++ < 40) setTimeout(wait, 50);
    };
    wait();
  }

  function syncMeasure() {
    const group = doc.querySelector('#experiment-nav .nav-group.current');
    for (const stale of doc.querySelectorAll('#experiment-nav .nav-measure')) {
      if (!group || !group.contains(stale)) stale.remove();
    }
    if (!group) return;
    const run = group.querySelector('.nav-item[data-view="run"]');
    if (!run) return;
    let item = group.querySelector('.nav-measure');
    if (!measureOffered()) { if (item) item.remove(); return; }
    if (!item) {
      item = doc.createElement('a');
      item.className = 'nav-item view-button nav-measure';
      item.dataset.view = 'measure';
      item.dataset.navKey = 'measure';
      const glyph = doc.createElement('span');
      glyph.className = 'nav-glyph';
      glyph.setAttribute('aria-hidden', 'true');
      const label = doc.createElement('span');
      label.className = 'nav-label';
      label.textContent = 'Measure rig';
      item.append(glyph, label);
      item.addEventListener('click', (event) => openMeasure(event, run));
    }
    if (run.nextSibling !== item) run.after(item);
    item.href = run.getAttribute('href');
    const on = run.classList.contains('selected') && onRunPage() && $('mode').value === 'measure';
    item.classList.toggle('selected', on);
    item.setAttribute('aria-current', on ? 'page' : 'false');
    // While Measure rig is chosen, Run is not the page being looked at.
    if (on) {
      run.classList.remove('selected');
      run.setAttribute('aria-current', 'false');
    }
  }

  function start() {
    let stored = null;
    try { stored = sessionStorage.getItem(PANE_KEY); } catch (e) { /* storage off */ }
    setPane(stored === 'output' ? 'output' : 'setup');
    $('pane-setup').addEventListener('click', () => setPane('setup'));
    $('pane-output').addEventListener('click', () => setPane('output'));
    $('bar-output').addEventListener('click', () => setPane('output'));
    $('history').addEventListener('click', (event) => {
      if (event.target.closest('button, a')) setPane('output');
    });

    let busy = false;
    const update = () => {
      if (busy) return;
      busy = true;
      try {
        mirrorLamp();
        watchRunStart();
        syncBar();
        syncMeasure();
        tick();
      } finally {
        // Our own sidebar edits are not news: drop the records they made.
        observer.takeRecords();
        busy = false;
      }
    };
    const observer = new MutationObserver(update);
    observer.observe($('experiment-nav'), {childList: true, subtree: true});
    for (const id of ['workspace', 'stop', 'run-status', 'run-info']) {
      observer.observe($(id), {attributes: true, attributeFilter: ['hidden', 'class', 'data-started']});
    }
    $('mode').addEventListener('change', update);
    window.addEventListener('popstate', update);
    update();
    setInterval(tick, 1000);
  }

  window.WorkspaceBench = Object.freeze({clock, startedAt});
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', start);
  else start();
}());
