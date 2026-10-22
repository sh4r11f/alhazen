// Formatting, routing and documentation helpers shared by every host.
const hwKeys = (L) => { const h = L.hardware || {}; const k = []; for (const key of ['eye_tracker', 'reward', 'microstimulation', 'recording', 'audio']) if (h[key]) k.push(key); return k.length ? k : ['display']; };
const collOf = (L) => L.collection || (L.source && L.source.kind === 'fork' ? 'own' : 'own');
const origShort = (L) => { const o = L.original || {}; return o.short ? o.short + (o.year ? ' (' + o.year + ')' : '') : (o.citation || ''); };
const asList = (v) => Array.isArray(v) ? v : (v && typeof v === 'object' ? Object.values(v) : []);
const slug = (t) => String(t || 'experiment').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40) || 'experiment';
const ago = (ms) => {
  if (!ms) return ''; const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 60) return 'just now'; if (s < 3600) return Math.round(s / 60) + ' min ago'; if (s < 86400) return Math.round(s / 3600) + ' h ago';
  const d = Math.round(s / 86400); return d < 30 ? d + ' d ago' : new Date(ms).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
};
// What removing a copy from the library takes with it (design.md section 5): the copy, its
// revisions and job records, the docs snapshot of the version it was forked from when no other
// copy uses that snapshot, and private revision files no other copy names. Hosts delete exactly
// these; nothing on a machine's disk, and no published listing, is touched.
const forkDocsKey = (f) => { const o = (f && f.origin) || {}; return o.scratch ? null : (o.docsKey || o.bundle || null); };
const forkBundles = (f) => { const out = new Set(); for (const r of Object.values((f && f.revisions) || {})) if (r && r.bundle) out.add(r.bundle); const h = f && f.head; if (h && h.id) out.add(h.id); return out; };
function removalPlan(f, all) {
  const others = (all || []).filter((o) => o && o.id !== f.id);
  const key = forkDocsKey(f);
  const docsShared = !key || others.some((o) => forkDocsKey(o) === key);
  const kept = new Set(); for (const o of others) for (const b of forkBundles(o)) kept.add(b);
  const bundles = [...forkBundles(f)].filter((b) => !kept.has(b));
  const nRev = Object.keys(f.revisions || {}).length;
  const jobs = Object.values(f.jobs || {});
  const nJobs = jobs.length;
  const removed = ['Your private copy of ' + f.title + (nRev ? ', with its ' + (nRev === 1 ? 'revision' : nRev + ' revisions') : '')];
  if (nJobs) removed.push('Its ' + (nJobs === 1 ? 'request and run record' : nJobs + ' request and run records'));
  if (key && !docsShared) removed.push('The saved methods of the version you copied');
  const kept_ = ['Folders already copied to your machines, and the session data in them'];
  if (key && docsShared) kept_.push('The saved methods of the version you copied: another copy in your library uses them');
  if (f.published) kept_.push('Your marketplace listing: it stays until you take it off');
  else kept_.push('The original listing on the marketplace');
  return { removed, kept: kept_, bundles, docsKey: key && !docsShared ? key : null, working: jobs.some((j) => j && (j.status === 'running' || j.status === 'waiting')) };
}
// Who can see a listing (design.md section 5a). A listing without `access` (written before 3.7) is
// public. The publisher manages it; anyone else who can see a non-public listing had it shared.
const ACCESS_CHOICES = [['private', 'Only you'], ['people', 'Specific people'], ['public', 'Everyone']];
const ACCESS_MAX_PEOPLE = 200;
function accessOf(L) {
  const a = (L && L.access) || {};
  const visibility = a.visibility === 'private' || a.visibility === 'people' ? a.visibility : 'public';
  return { visibility, people: visibility === 'people' ? [...new Set(asList(a.people).filter((u) => typeof u === 'string' && u))] : [] };
}
const accessCanManage = (L, viewerUid) => !!L && (L.canManage === true || (!!viewerUid && L.uid === viewerUid));
// The grouping used by Browse's "Visible to" filter and My experiments.
function accessGroup(L, viewerUid) {
  const v = accessOf(L).visibility;
  if (v === 'public') return 'public';
  if (!accessCanManage(L, viewerUid)) return 'with-you';
  return v === 'private' ? 'private' : 'by-you';
}
const VISIBLE_TO = [['public', 'Everyone'], ['with-you', 'Shared with you'], ['by-you', 'Shared by you'], ['private', 'Only you']];
// The badge on cards and listing headers; none for public listings.
function accessBadge(L, viewerUid) {
  const g = accessGroup(L, viewerUid);
  return g === 'public' ? null : g === 'private' ? 'Only you' : g === 'by-you' ? 'Shared' : 'Shared with you';
}
// One plain line saying who can see a listing. `names` maps uid to a label (publisher only).
function accessSummary(L, viewerUid, names) {
  const a = accessOf(L);
  if (a.visibility === 'public') return 'Everyone';
  if (!accessCanManage(L, viewerUid)) return 'Shared with you by ' + ((L.publisher || {}).name || 'its publisher');
  if (a.visibility === 'private') return 'Only you';
  const n = a.people.length;
  const shown = a.people.map((u) => (names || {})[u]).filter(Boolean);
  return 'You and ' + n + (n === 1 ? ' person' : ' people') + (shown.length ? ': ' + shown.slice(0, 3).join(', ') + (n > 3 ? ' and ' + (n - 3) + ' more' : '') : '');
}
// The rules of PlatformService.set_access, for hosts that apply them in the browser (Mana) and for
// the dialog, so it can say what is wrong before saving. Returns {visibility, people}; throws an
// Error carrying the contract's message.
function normalizeAccess(input, publisherUid) {
  const visibility = input && input.visibility;
  if (!ACCESS_CHOICES.some(([k]) => k === visibility)) throw new Error('Choose who can see it: Only you, Specific people or Everyone.');
  if (visibility !== 'people') return { visibility, people: [] };
  const people = [...new Set(asList(input.people).map((u) => String(u || '').trim()).filter((u) => u && u !== publisherUid))];
  if (!people.length) throw new Error('Add at least one person, or choose Only you');
  if (people.length > ACCESS_MAX_PEOPLE) throw new Error('A listing can be shared with at most ' + ACCESS_MAX_PEOPLE + ' people.');
  return { visibility, people };
}
// My experiments: the viewer's listings that are not on the marketplace, listings shared with the
// viewer, and the viewer's public listings (newest first, as `listings` is ordered).
function librarySections(listings, viewerUid) {
  const out = { notOnMarketplace: [], sharedWithYou: [], onMarketplace: [] };
  for (const L of listings || []) {
    const g = accessGroup(L, viewerUid);
    if (g === 'with-you') out.sharedWithYou.push(L);
    else if (!accessCanManage(L, viewerUid)) continue;
    else if (g === 'public') out.onMarketplace.push(L);
    else out.notOnMarketplace.push(L);
  }
  return out;
}
const kb = (b) => b > 1e6 ? (b / 1e6).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1e3)) + ' KB';

// Dashboards on machines (Mana, design.md section 8). Which machine a run, dashboard or shutdown job is
// on: its machineLabel; else job.machine as an open terminal window mapped to that window's machine (as
// the Runs list names it); else job.machine taken as a label. Labels compare trimmed and case-blind, the
// rule rig configs use (65_rigs.jsx rigLabelKey).
const machineLabelKey = (s) => String(s || '').trim().toLowerCase();
function jobMachineLabel(job, sessions, devices) {
  if (job && job.machineLabel) return job.machineLabel;
  const m = (job && job.machine) || '';
  const s = (sessions || []).find((x) => x.id === m);
  if (s) { const dv = (devices || []).find((d) => d.id === s.device) || {}; return dv.label || dv.host || s.device || m; }
  return m;
}
// What a run or dashboard job says about its dashboard: 'starting' (Mana is still opening it), 'open'
// (opened and not reported closed: the last known state, the machine may have stopped it unreported),
// 'closed' (it reported closing, or was shut down from the platform), or null (it never opened one).
function dashJobState(job) {
  if (!job || (job.kind !== 'run' && job.kind !== 'dashboard')) return null;
  if (job.closedAt) return 'closed';
  if (job.status === 'running') return 'starting';
  if (job.status === 'done' && job.url) return 'open';
  return null;
}
// A machine's dashboard from the newest job on it that says something about one, with a running
// shutdown as 'stopping': {state: 'open'|'closed'|'starting'|'stopping', job, at} or null.
function machineDashboard(jobs, label, sessions, devices) {
  const key = machineLabelKey(label);
  if (!key) return null;
  let best = null;
  for (const job of jobs || []) {
    if (!job || machineLabelKey(jobMachineLabel(job, sessions, devices)) !== key) continue;
    const state = job.kind === 'shutdown' ? (job.status === 'running' ? 'stopping' : null) : dashJobState(job);
    if (state && (!best || (job.startedAt || 0) > (best.job.startedAt || 0))) best = { state, job };
  }
  if (!best) return null;
  const j = best.job;
  return { ...best, at: best.state === 'closed' ? j.closedAt : best.state === 'open' ? (j.updatedAt || j.startedAt) : j.startedAt };
}
// A job whose step is held by Mana Terminal for the person's yes (hubkit marks it as
// waiting: {for: 'terminal', machine, what, since}); null otherwise, and once the job has ended.
function terminalWait(job) {
  const w = job && job.waiting;
  if (!w || w.for !== 'terminal') return null;
  if (job.status && job.status !== 'running' && job.status !== 'queued') return null;
  return { machine: w.machine || 'your machine', what: w.what || '' };
}
// The chip of a job that opened a dashboard (it has a url): Open until it is reported closed, then
// Closed. Null for other jobs, which keep their status chip.
function dashJobChip(job) {
  if (!job || !job.url || (job.kind !== 'run' && job.kind !== 'dashboard')) return null;
  if (job.closedAt) return { tone: 'st-idle', words: 'Closed' };
  if (job.status === 'done') return { tone: 'st-ok', words: 'Open' };
  return null;
}

// The address of a screen: ?ehv=&ehid=&ehtab=&ehtask=&ehset=, plus ehdev for the Configure window of
// a paired machine (65_rigs.jsx rigWindowRoute). Other parameters (Mana's chatId) are kept.
function readRoute() {
  const q = new URLSearchParams(window.location.search);
  return { v: q.get('ehv') || 'home', id: q.get('ehid') || '', tab: q.get('ehtab') || '', task: q.get('ehtask') || '', set: q.get('ehset') || '', dev: q.get('ehdev') || '' };
}
function writeRoute(r, replace, state) {
  const u = new URL(window.location.href);
  for (const [k, v] of [['ehv', r.v === 'home' ? '' : r.v], ['ehid', r.id], ['ehtab', r.tab], ['ehtask', r.task], ['ehset', r.set], ['ehdev', r.dev]]) { if (v) u.searchParams.set(k, v); else u.searchParams.delete(k); }
  window.history[replace ? 'replaceState' : 'pushState'](state || {}, '', u.toString());
}

function fillTokens(md, tasks) {
  const pmap = {}, tmap = {};
  for (const t of tasks || []) { tmap[t.id] = t.title; for (const p of t.parameters || []) pmap[t.id + '/' + p.name] = p; }
  return String(md || '').replace(/\[\[param:([^\]]+)\]\]/g, (_, key) => {
    const p = pmap[key]; if (!p) return '`' + key.split('/').pop() + '`';
    const v = p.default === undefined || p.default === null ? null : ehFormat(p.default, p.unit);
    return v === undefined || v === null ? '`' + p.name + '`' : '**' + v + '**';
  }).replace(/\[\[task:([^\]]+)\]\]/g, (_, id) => '**' + (tmap[id] || id) + '**').replace(/^## Provenance[\s\S]*$/m, '');
}
