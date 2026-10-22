// Tests of the Mana host's storage for who can see a listing (platform-web/src/host_mana.jsx,
// design.md section 5a) against an in-memory Firebase that refuses what Firebase refuses:
// overlapping paths in one update, invalid keys, writes over ~8 MB, and reads without a grant.
//   ALHAZEN_PLATFORM_NODE_MODULES=/tmp/apw-node/node_modules node --test 'platform-web/test/*.test.mjs'
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.join(HERE, '..', 'src');
const modules = process.env.ALHAZEN_PLATFORM_NODE_MODULES || path.join(HERE, '..', 'node_modules');
const require = createRequire(path.join(modules, 'noop.js'));
const esbuild = require('esbuild');

const OWNER = 'sharif';
const HUB = OWNER + '/file-system/Apps/alhazen-experiment-platform/hub';
const MINE = (u) => u + '/file-system/alhazen-experiment-platform-mine';
const SHARES = OWNER + '/file-system/alhazen-experiment-platform-shares';
const EMAILS = { bob: 'bob@lab.org', carol: 'carol@lab.org', sharif: 'sharif@lab.org' };

const code = esbuild.transformSync(
  fs.readFileSync(path.join(SRC, '60_helpers.js'), 'utf8') + '\n' + fs.readFileSync(path.join(SRC, 'host_mana.jsx'), 'utf8'), { loader: 'jsx' }).code;
const NAMES = ['manaParseShareKey', 'manaListingPlace', 'manaMovePlan', 'manaShareIndexPatch', 'manaMergeListings', 'manaReadGrants', 'MANA_WRITE_LIMIT'];

// ------------------------------------------------------------------ the in-memory Firebase
function makeDb(initial) {
  const root = JSON.parse(JSON.stringify(initial));
  const listeners = new Set();
  const objects = new Map(); // Firebase Storage: '<owner>/<path>' -> text
  const writes = [];
  let failRead = null; // (path) => Error | null: a read that fails for another reason than permissions
  const split = (p) => p.split('/').filter(Boolean);
  const at = (p) => { let c = root; for (const k of split(p)) { if (c == null || typeof c !== 'object') return null; c = c[k]; } return c === undefined ? null : c; };
  const prune = (o) => { if (!o || typeof o !== 'object') return o; for (const k of Object.keys(o)) { o[k] = prune(o[k]); if (o[k] === null || (typeof o[k] === 'object' && !Object.keys(o[k]).length)) delete o[k]; } return o; };
  const put = (p, v) => {
    const parts = split(p); let c = root;
    for (const k of parts.slice(0, -1)) { if (c[k] == null || typeof c[k] !== 'object') c[k] = {}; c = c[k]; }
    if (v === null || v === undefined) delete c[parts.at(-1)]; else c[parts.at(-1)] = JSON.parse(JSON.stringify(v));
    prune(root);
  };
  const badKey = (k) => !k || /[.$#\[\]]/.test(k);
  const checkValue = (v, where) => { if (v && typeof v === 'object') for (const [k, x] of Object.entries(v)) { if (badKey(k)) throw new Error('invalid key ' + k + ' at ' + where); checkValue(x, where + '/' + k); } };
  function canRead(p, uid) {
    const s = split(p);
    if (s[1] === 'file-system' && s[2] === 'alhazen-experiment-platform-mine' && s[0] !== uid) {
      if (s[3] !== 'listings' || !s[4]) return false;
      return at(s.slice(0, 5).join('/') + '/metadata/permissions/read/' + uid) === true;
    }
    if (p.startsWith(SHARES) && uid !== OWNER) return s[3] === uid && at(SHARES + '/metadata/permissions/per_user_read') === true;
    return true;
  }
  const denied = () => Object.assign(new Error('permission_denied'), { code: 'PERMISSION_DENIED' });
  const fire = () => { for (const l of [...listeners]) if (listeners.has(l)) { if (canRead(l.path, l.uid)) l.cb({ val: () => at(l.path) }); else { listeners.delete(l); if (l.err) l.err(denied()); } } };
  function fsFor(uid) {
    return {
      db: {}, ref: (_db, p) => ({ path: p }),
      // Storage: each person reads and writes only below their own uid (as Mana's rules do).
      storage: {}, storageRef: (_s, p) => ({ path: p }),
      uploadBytes: async (r, blob) => { if (r.path.split('/')[0] !== uid) throw denied(); objects.set(r.path, await blob.text()); return { ref: r, metadata: {} }; },
      getDownloadURL: async (r) => { if (r.path.split('/')[0] !== uid) throw denied(); if (!objects.has(r.path)) throw new Error('storage/object-not-found'); return 'https://storage/' + r.path; },
      get: async (r) => { const broken = failRead && failRead(r.path); if (broken) throw broken; if (!canRead(r.path, uid)) throw denied(); return { val: () => JSON.parse(JSON.stringify(at(r.path))) }; },
      onValue: (r, cb, err) => { const l = { path: r.path, cb, err, uid }; if (!canRead(r.path, uid)) { if (err) err(denied()); return () => {}; } listeners.add(l); cb({ val: () => at(r.path) }); return () => listeners.delete(l); },
      update: async (r, obj) => {
        const keys = Object.keys(obj);
        for (const a of keys) {
          for (const seg of split(a)) if (badKey(seg)) throw new Error('invalid key ' + a);
          for (const b of keys) if (a !== b && (b + '/').startsWith(a + '/')) throw new Error('overlapping paths ' + a + ' and ' + b);
          checkValue(obj[a], a);
          if (!(r.path + '/' + a).startsWith(uid + '/') && !(r.path + '/' + a).startsWith(HUB)) throw denied();
        }
        const size = JSON.stringify(obj).length;
        if (size > 8e6) throw new Error('write too large: ' + size);
        writes.push({ uid, path: r.path, keys, size });
        for (const a of keys) put(r.path + '/' + a, obj[a]);
        fire();
      },
      remove: async (r) => { put(r.path, null); writes.push({ uid, path: r.path, keys: [null], size: 0 }); fire(); },
      auth: { currentUser: { uid, displayName: uid, email: EMAILS[uid], getIdToken: async () => 'token-' + uid } },
    };
  }
  return { root, at, fsFor, writes, listeners, objects, setFailRead: (fn) => { failRead = fn; } };
}

const BUNDLE_PREFIX = 'app-storage/Apps/alhazen-experiment-platform/hub/bundles';
// The hub's `bundles` storage grant (useStorageUrl): only the owner's page writes through it here.
function grantFor(db) {
  const key = (rel) => OWNER + '/' + BUNDLE_PREFIX + '/' + rel;
  return {
    url: 'https://grant/', encodePath: (n) => n.split('/').map(encodeURIComponent).join('/'),
    stat: async (rel) => { const v = db.objects.get(key(rel)); if (v == null) throw new Error('404'); return { size: new Blob([v]).size }; },
    upload: async (rel, blob) => { if (db.objects.has(key(rel))) throw new Error('exists'); db.objects.set(key(rel), await blob.text()); },
    remove: async (rel) => { db.objects.delete(key(rel)); },
  };
}

function hostFor(db, uid, mails) {
  const GRANT = OWNER + '/' + BUNDLE_PREFIX + '/';
  const fetch = async (url, init = {}) => {
    if (url.startsWith('https://storage/') || url.startsWith('https://grant/')) {
      const key = url.startsWith('https://grant/') ? GRANT + decodeURIComponent(url.slice('https://grant/'.length)) : url.slice('https://storage/'.length);
      const text = db.objects.get(key);
      return { ok: text != null, status: text != null ? 200 : 404, text: async () => text };
    }
    const body = JSON.parse(init.body || '{}');
    const route = new URL(url).pathname;
    const reply = (status, json) => ({ ok: status < 400, status, json: async () => json, text: async () => JSON.stringify(json) });
    if (route === '/api/share/lookup-users') return reply(200, { users: body.user_ids.filter((u) => EMAILS[u]).map((u) => ({ uid: u, email: EMAILS[u], displayName: null })) });
    if (route === '/api/share/lookup-user') { const u = Object.keys(EMAILS).find((k) => EMAILS[k] === body.email); return u ? reply(200, { uid: u }) : reply(404, { detail: 'user not found' }); }
    if (route === '/api/share/notify') { mails.push(body); return reply(200, { ok: true }); }
    throw new Error('unexpected request ' + route);
  };
  const React = { useRef: (v) => ({ current: v }), createElement: (_c, props) => props };
  const window = { location: { origin: 'https://www.mana.space', href: 'https://www.mana.space/home/' + OWNER + '/Apps/alhazen-experiment-platform' } };
  const api = new Function('React', 'FS', 'mana', 'useStorageUrl', 'relPathAddress', 'userId', 'ownerId', 'openChat', 'copyText', 'useTheme', 'API_BASE_URL', 'fetch', 'window', 'PlatformApp', 'ehDocBundle', 'ehLogoKind',
    code + '\nreturn { UserDiv, ' + NAMES.join(', ') + ' };')(
    React, db.fsFor(uid), {}, (name) => (name === 'bundles' ? grantFor(db) : {}), (...s) => [HUB, ...s].join('/'), uid, OWNER, () => {}, async () => 'copied', () => ({ theme: 'light' }), 'https://server/api', fetch, window, function PlatformApp() {}, (d) => d && d.bundle, () => 'fixation');
  return { host: api.UserDiv().host, api };
}

const bundle = (n) => ({ meta: { chunkCount: n, fileCount: n, bytes: 10 * n, excluded: '[]', files: '[]', complete: true }, chunks: Object.fromEntries(Array.from({ length: n }, (_, i) => [i, JSON.stringify([['f' + i + '.txt', 'x'.repeat(10), 10, 'utf8']])])) });
function seed() {
  return {
    sharif: { 'file-system': { Apps: { 'alhazen-experiment-platform': { hub: {
      published: { sharif: {
        kde: { title: 'kde vergence', publisher: { uid: 'sharif', name: 'Sharif' }, bundle: { id: 'kde-2', path: HUB + '/bundles/sharif/kde-2' }, versions: { 'kde-1': { version: '1.0', at: 1 }, 'kde-2': { version: '1.1', at: 2 } }, updatedAt: 10, source: { kind: 'repo' } },
        mbri: { title: 'mbri', publisher: { uid: 'sharif', name: 'Sharif' }, bundle: { id: 'kde-1', path: HUB + '/bundles/sharif/kde-1' }, versions: { 'kde-1': { version: '0.1', at: 1 } }, updatedAt: 9 },
      } },
      docs: { sharif: { kde: { readme: '# kde', bundle: 'kde-2' } } },
      bundles: { sharif: { 'kde-1': bundle(2), 'kde-2': bundle(3) } },
    } } } } },
  };
}
const flush = () => new Promise((r) => setImmediate(r));
function watch(host) {
  const state = { listings: null };
  const off = host.subscribeListings((v) => { state.listings = v; });
  return { state, off };
}

test('pure parts: share keys, places, grants, the share index patch, merging', () => {
  const { api } = hostFor(makeDb(seed()), OWNER, []);
  assert.deepEqual(api.manaParseShareKey('sharif~kde'), { pub: 'sharif', exp: 'kde' });
  assert.equal(api.manaParseShareKey('sharif~'), null);
  assert.equal(api.manaParseShareKey('a.b~c'), null);
  assert.equal(api.manaListingPlace('mine', HUB, 'bob', 'x').bundle('b1'), MINE('bob') + '/listings/x/bundles/b1');
  assert.equal(api.manaListingPlace('hub', HUB, 'bob', 'x').docs, HUB + '/docs/bob/x');
  assert.deepEqual(api.manaReadGrants({ visibility: 'people', people: ['a', 'b'] }), { read: { a: true, b: true } });
  assert.equal(api.manaReadGrants({ visibility: 'private' }), null);
  assert.deepEqual(api.manaShareIndexPatch('sharif', 'kde', { visibility: 'people', people: ['a', 'b'] }, { visibility: 'people', people: ['b', 'c'] }, 5),
    { 'c/sharif~kde': { publisher: 'sharif', exp: 'kde', at: 5 }, 'a/sharif~kde': null });
  const merged = api.manaMergeListings({ sharif: { kde: { title: 'old', updatedAt: 1 } }, metadata: { type: 'link' } }, { 'sharif~kde': { title: 'new', updatedAt: 2 }, 'eve~x': { title: 'x', updatedAt: 1 } });
  assert.equal(merged.sharif.kde.title, 'new');
  assert.equal(merged.eve.x.title, 'x');
  assert.equal(merged.metadata, undefined);
});

test('the move plan keeps hub bundles another listing names and remaps paths into the new place', () => {
  const { api } = hostFor(makeDb(seed()), OWNER, []);
  const s = seed().sharif['file-system'].Apps['alhazen-experiment-platform'].hub.published.sharif;
  const plan = api.manaMovePlan({ hub: HUB, pub: 'sharif', exp: 'kde', rec: s.kde, from: 'hub', to: 'mine', others: s });
  assert.deepEqual(plan.bundles, ['kde-2', 'kde-1']);
  assert.equal(plan.listing.bundle.path, MINE('sharif') + '/listings/kde/bundles/kde-2');
  assert.deepEqual(plan.deletes, [HUB + '/published/sharif/kde', HUB + '/docs/sharif/kde', HUB + '/bundles/sharif/kde-2']);
  const back = api.manaMovePlan({ hub: HUB, pub: 'sharif', exp: 'kde', rec: plan.listing, from: 'mine', to: 'hub', others: {} });
  assert.deepEqual(back.deletes, [MINE('sharif') + '/listings/kde']);
  assert.equal(back.listing.bundle.path, HUB + '/bundles/sharif/kde-2');
});

test('sharing a public listing with people moves it out of the hub, grants each person, indexes and emails them', async () => {
  const db = makeDb(seed()); const mails = [];
  const { host } = hostFor(db, OWNER, mails);
  const w = watch(host); await flush();
  const L = { ...w.state.listings.sharif.kde, uid: 'sharif', exp: 'kde' };
  const res = await host.setAccess(L, { visibility: 'people', people: ['bob', 'sharif', 'bob'], notify: true });
  assert.deepEqual(res.access, { visibility: 'people', people: ['bob'] });
  // Out of the hub (the bundle mbri still names stays); into Sharif's workspace with a grant for Bob.
  assert.equal(db.at(HUB + '/published/sharif/kde'), null);
  assert.equal(db.at(HUB + '/docs/sharif/kde'), null);
  assert.equal(db.at(HUB + '/bundles/sharif/kde-2'), null);
  assert.ok(db.at(HUB + '/bundles/sharif/kde-1'));
  const base = MINE('sharif') + '/listings/kde';
  assert.deepEqual(db.at(base + '/listing/access'), { visibility: 'people', people: ['bob'] });
  assert.equal(db.at(base + '/listing/bundle/path'), base + '/bundles/kde-2');
  assert.deepEqual(db.at(base + '/metadata/permissions'), { read: { bob: true } });
  assert.equal(db.at(base + '/metadata/hidden'), true);
  assert.equal(db.at(base + '/bundles/kde-2/meta/chunkCount'), 3);
  // Files went to Sharif's own Storage, not the database; only the meta is in the tree.
  assert.deepEqual(db.at(base + '/bundles/kde-2/meta/store'), { v: 1, owner: 'sharif', path: 'alhazen-experiment-platform-mine/bundles/kde-2' });
  assert.equal(db.at(base + '/bundles/kde-2/chunks'), null);
  assert.equal(db.objects.get('sharif/alhazen-experiment-platform-mine/bundles/kde-1/1.json'), db.at(HUB + '/bundles/sharif/kde-1/chunks/1'));
  assert.equal(db.at(base + '/docs/readme'), '# kde');
  assert.equal(db.at(MINE('sharif') + '/listed/kde/visibility'), 'people');
  assert.deepEqual(db.at(SHARES + '/metadata/permissions'), { per_user_read: true });
  assert.equal(db.at(SHARES + '/bob/sharif~kde/exp'), 'kde');
  assert.deepEqual(mails.map((m) => [m.email, m.role, m.appName]), [['bob@lab.org', 'viewer', 'kde vergence']]);
  assert.equal(mails[0].shareUrl, 'https://www.mana.space/home/sharif/Apps/alhazen-experiment-platform?ehv=exp&ehid=sharif~kde');
  // Bundles were copied one chunk per write, all well under the write limit.
  assert.ok(db.writes.every((x) => x.size < 7e6));
  // The owner still sees it in the merged catalogue, now from its own place.
  await flush();
  assert.equal(w.state.listings.sharif.kde._base, base);
  assert.equal(w.state.listings.sharif.mbri.title, 'mbri');
  w.off();
});

test('a person it is shared with finds it through the index, reads its docs and bundle; someone else cannot', async () => {
  const db = makeDb(seed());
  const owner = hostFor(db, OWNER, []).host;
  const w0 = watch(owner); await flush();
  await owner.setAccess({ ...w0.state.listings.sharif.kde, uid: 'sharif', exp: 'kde' }, { visibility: 'people', people: ['bob'] });
  w0.off();
  const bob = hostFor(db, 'bob', []).host;
  const wb = watch(bob); await flush();
  const L = { ...wb.state.listings.sharif.kde, uid: 'sharif', exp: 'kde' };
  assert.equal(L.title, 'kde vergence');
  let docs = null; const docErrors = []; bob.subscribeDocs(L, (v) => { docs = v; }, (e) => docErrors.push(e));
  assert.equal(docs.readme, '# kde');
  assert.equal((await bob.bundleMeta(L.bundle.path)).chunkCount, 3);
  const carol = hostFor(db, 'carol', []).host;
  const wc = watch(carol); await flush();
  assert.equal(wc.state.listings.sharif.kde, undefined);
  assert.equal(await carol.resolveListing('sharif', 'kde'), false);
  // Only a refused read means "not shared with you"; any other failure reaches the page.
  db.setFailRead((q) => (q.endsWith('/listings/kde/listing') ? new Error('network down') : null));
  await assert.rejects(hostFor(db, 'carol', []).host.resolveListing('sharif', 'kde'), /network down/);
  db.setFailRead(null);
  // Narrowing it to Only you takes it from Bob's list at once.
  await owner.setAccess({ ...L, _base: MINE('sharif') + '/listings/kde' }, { visibility: 'private', people: [] });
  await flush();
  assert.equal(wb.state.listings.sharif && wb.state.listings.sharif.kde, undefined);
  assert.equal(db.at(SHARES + '/bob'), null);
  assert.equal(db.at(MINE('sharif') + '/listings/kde/metadata/permissions'), null);
  assert.equal(docErrors.length, 1); // Bob's open page loses the docs it can no longer read
  wb.off(); wc.off();
});

test('a share link from another publisher becomes a pointer in the viewer’s own library', async () => {
  const db = makeDb(seed());
  db.root.eve = { 'file-system': { 'alhazen-experiment-platform-mine': { listings: { dots: {
    listing: { title: 'Dots', publisher: { uid: 'eve', name: 'Eve' }, bundle: { id: 'd1', path: MINE('eve') + '/listings/dots/bundles/d1' }, access: { visibility: 'people', people: ['bob'] }, updatedAt: 3 },
    metadata: { type: 'link', hidden: true, permissions: { read: { bob: true } } } } } } } };
  const bob = hostFor(db, 'bob', []).host;
  const w = watch(bob); await flush();
  assert.equal(w.state.listings.eve, undefined);
  assert.equal(await bob.resolveListing('eve', 'dots'), true);
  await flush();
  assert.equal(w.state.listings.eve.dots.title, 'Dots');
  assert.equal(db.at(MINE('bob') + '/shared/eve~dots/publisher'), 'eve');
  assert.equal(db.at(MINE('bob') + '/shared/metadata/hidden'), true);
  w.off();
});

test('making a shared listing public moves it back to the hub and clears its grants and index entries', async () => {
  const db = makeDb(seed());
  const { host } = hostFor(db, OWNER, []);
  const w = watch(host); await flush();
  await host.setAccess({ ...w.state.listings.sharif.kde, uid: 'sharif', exp: 'kde' }, { visibility: 'people', people: ['bob', 'carol'] });
  await flush();
  await host.setAccess({ ...w.state.listings.sharif.kde, uid: 'sharif', exp: 'kde' }, { visibility: 'public' });
  await flush();
  assert.deepEqual(db.at(HUB + '/published/sharif/kde/access'), { visibility: 'public' }); // Firebase keeps no empty list
  assert.equal(db.at(HUB + '/published/sharif/kde/bundle/path'), HUB + '/bundles/sharif/kde-2');
  assert.equal(db.at(HUB + '/bundles/sharif/kde-2/meta/chunkCount'), 3);
  // Back in the hub, its files are under the hub's public bundles grant.
  assert.equal(db.at(HUB + '/bundles/sharif/kde-2/meta/store/path'), BUNDLE_PREFIX + '/sharif/kde-2');
  assert.ok(db.objects.get('sharif/' + BUNDLE_PREFIX + '/sharif/kde-2/2.json'));
  assert.equal(db.at(HUB + '/bundles/sharif/kde-2/chunks'), null);
  assert.equal(db.at(HUB + '/docs/sharif/kde/readme'), '# kde');
  assert.equal(db.at(MINE('sharif') + '/listings/kde'), null);
  assert.equal(db.at(MINE('sharif') + '/listed/kde'), null);
  assert.equal(db.at(SHARES + '/bob'), null);
  assert.equal(db.at(SHARES + '/carol'), null);
  assert.equal(w.state.listings.sharif.kde._base, undefined);
  w.off();
});

test('only the publisher changes access, and empty people are refused before anything is written', async () => {
  const db = makeDb(seed());
  const bob = hostFor(db, 'bob', []).host;
  await assert.rejects(bob.setAccess({ uid: 'sharif', exp: 'kde', title: 'kde' }, { visibility: 'private' }), /Only the publisher can change who sees this/);
  const owner = hostFor(db, OWNER, []).host;
  const n = db.writes.length;
  await assert.rejects(owner.setAccess({ uid: 'sharif', exp: 'kde', title: 'kde' }, { visibility: 'people', people: ['sharif'] }), /Add at least one person, or choose Only you/);
  assert.equal(db.writes.length, n);
});

test('publishing a copy for Only you writes it to the publisher’s workspace; updating it for everyone moves it to the hub', async () => {
  const db = makeDb(seed());
  db.root.bob = { 'file-system': { 'alhazen-experiment-platform-mine': { metadata: { type: 'link', hidden: true },
    forks: { 'my-dots': { title: 'My dots', head: { id: 'b1', path: MINE('bob') + '/bundles/b1' }, origin: { scratch: true } } }, bundles: { b1: bundle(2) } } } };
  const { host } = hostFor(db, 'bob', []);
  const w = watch(host); await flush();
  const fork = { id: 'my-dots', ...db.at(MINE('bob') + '/forks/my-dots') };
  const form = { name: 'Bob', title: 'My dots', summary: 's', category: 'motion', subjects: ['human'], hardware: {}, version: '0.1.0', license: 'MIT' };
  await host.publish(fork, { ...form, access: { visibility: 'private', people: [] } }, () => {});
  const base = MINE('bob') + '/listings/my-dots';
  assert.equal(db.at(base + '/listing/access/visibility'), 'private');
  assert.equal(db.at(base + '/listing/bundle/path'), base + '/bundles/b1');
  assert.equal(db.at(base + '/metadata/permissions'), null);
  assert.equal(db.at(base + '/bundles/b1/meta/chunkCount'), 2);
  assert.equal(db.at(base + '/bundles/b1/meta/store/owner'), 'bob');
  assert.ok(db.objects.get('bob/alhazen-experiment-platform-mine/bundles/b1/1.json'));
  assert.equal(db.at(HUB + '/published/bob'), null);
  assert.equal(db.at(MINE('bob') + '/forks/my-dots/published/visibility'), 'private');
  await flush();
  assert.equal(w.state.listings.bob['my-dots']._base, base);
  await host.publish(fork, { ...form, version: '0.2.0', access: { visibility: 'public' } }, () => {});
  assert.equal(db.at(base), null);
  assert.equal(db.at(HUB + '/published/bob/my-dots/version'), '0.2.0');
  assert.deepEqual(Object.keys(db.at(HUB + '/published/bob/my-dots/versions')).sort(), ['b1']);
  assert.equal(db.at(HUB + '/published/bob/my-dots/access/visibility'), 'public');
  await host.unpublish(fork);
  assert.equal(db.at(HUB + '/published/bob/my-dots'), null);
  w.off();
});

test('bundle files: a hub bundle in Storage is read through the grant, a copy gets its own files, and nothing writes chunks to the database', async () => {
  const db = makeDb(seed());
  // kde-2 lives in Storage under the hub's grant (as after the 2026-10-21 migration); kde-1 is still a legacy database bundle.
  const legacy = db.at(HUB + '/bundles/sharif/kde-2');
  for (let i = 0; i < 3; i++) db.objects.set('sharif/' + BUNDLE_PREFIX + '/sharif/kde-2/' + i + '.json', legacy.chunks[i]);
  db.root.sharif['file-system'].Apps['alhazen-experiment-platform'].hub.bundles.sharif['kde-2'] = { meta: { ...legacy.meta, store: { v: 1, owner: 'sharif', path: BUNDLE_PREFIX + '/sharif/kde-2' } } };
  const bob = hostFor(db, 'bob', []).host;
  const w = watch(bob); await flush();
  const L = { ...w.state.listings.sharif.kde, uid: 'sharif', exp: 'kde' };
  assert.equal((await bob.bundleMeta(L.bundle.path)).chunkCount, 3);
  assert.deepEqual(await bob.bundleChunk(L.bundle.path, 2), JSON.parse(legacy.chunks[2]));
  assert.deepEqual(await bob.bundleChunk(HUB + '/bundles/sharif/kde-1', 1), JSON.parse(db.at(HUB + '/bundles/sharif/kde-1/chunks/1')));
  const before = db.writes.length;
  const { record } = await bob.fork(L, () => {});
  const meta = db.at(MINE('bob') + '/bundles/kde-2/meta');
  assert.deepEqual(meta.store, { v: 1, owner: 'bob', path: 'alhazen-experiment-platform-mine/bundles/kde-2' });
  assert.equal(meta.complete, true);
  assert.equal(db.at(MINE('bob') + '/bundles/kde-2/chunks'), null);
  assert.equal(db.objects.get('bob/alhazen-experiment-platform-mine/bundles/kde-2/1.json'), legacy.chunks[1]);
  assert.deepEqual(await bob.bundleChunk(record.head.path, 0), JSON.parse(legacy.chunks[0]));
  // No write since the fork started carried a chunk.
  assert.ok(db.writes.slice(before).every((x) => !JSON.stringify(x).includes('/chunks/') && !JSON.stringify(x).includes('"chunks')));
  w.off();
});

test('bundle files: moving a Storage bundle out of the hub copies it to the publisher’s Storage and removes the hub files; a recipient is told plainly it cannot open them', async () => {
  const db = makeDb(seed());
  const legacy = db.at(HUB + '/bundles/sharif/kde-2');
  for (let i = 0; i < 3; i++) db.objects.set('sharif/' + BUNDLE_PREFIX + '/sharif/kde-2/' + i + '.json', legacy.chunks[i]);
  db.root.sharif['file-system'].Apps['alhazen-experiment-platform'].hub.bundles.sharif['kde-2'] = { meta: { ...legacy.meta, store: { v: 1, owner: 'sharif', path: BUNDLE_PREFIX + '/sharif/kde-2' } } };
  const owner = hostFor(db, OWNER, []).host;
  const w = watch(owner); await flush();
  await owner.setAccess({ ...w.state.listings.sharif.kde, uid: 'sharif', exp: 'kde' }, { visibility: 'people', people: ['bob'] });
  const base = MINE('sharif') + '/listings/kde';
  assert.equal(db.at(base + '/bundles/kde-2/meta/store/path'), 'alhazen-experiment-platform-mine/bundles/kde-2');
  assert.equal(db.objects.get('sharif/alhazen-experiment-platform-mine/bundles/kde-2/2.json'), legacy.chunks[2]);
  assert.equal(db.objects.has('sharif/' + BUNDLE_PREFIX + '/sharif/kde-2/0.json'), false);
  assert.deepEqual(await owner.bundleChunk(base + '/bundles/kde-2', 1), JSON.parse(legacy.chunks[1]));
  const bob = hostFor(db, 'bob', []).host;
  await assert.rejects(bob.bundleChunk(base + '/bundles/kde-2', 0), /only the publisher can open them/);
  w.off();
});
