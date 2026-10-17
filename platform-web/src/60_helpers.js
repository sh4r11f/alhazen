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
const kb = (b) => b > 1e6 ? (b / 1e6).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1e3)) + ' KB';

function readRoute() {
  const q = new URLSearchParams(window.location.search);
  return { v: q.get('ehv') || 'home', id: q.get('ehid') || '', tab: q.get('ehtab') || '', task: q.get('ehtask') || '', set: q.get('ehset') || '' };
}
function writeRoute(r, replace) {
  const u = new URL(window.location.href);
  for (const [k, v] of [['ehv', r.v === 'home' ? '' : r.v], ['ehid', r.id], ['ehtab', r.tab], ['ehtask',r.task], ['ehset',r.set]]) { if (v) u.searchParams.set(k, v); else u.searchParams.delete(k); }
  window.history[replace ? 'replaceState' : 'pushState']({}, '', u.toString());
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
