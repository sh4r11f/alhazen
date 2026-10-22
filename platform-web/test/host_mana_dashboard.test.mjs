// Tests of the Mana host's Open dashboard (My machines), reusing the fake Firebase of host_mana_access.test.mjs.
// Original header of the copied fakes:
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
  return { root, at, fsFor, writes, listeners, setFailRead: (fn) => { failRead = fn; } };
}

function hostFor(db, uid, mails) {
  const fetch = async (url, init) => {
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
    React, db.fsFor(uid), mails.mana || {}, () => ({}), (...s) => [HUB, ...s].join('/'), uid, OWNER, () => {}, async () => 'copied', () => ({ theme: 'light' }), 'https://server/api', fetch, window, function PlatformApp() {}, (d) => d && d.bundle, () => 'fixation');
  return { host: api.UserDiv().host, api };
}


const flush = () => new Promise((r) => setImmediate(r));

test('Open dashboard writes a running dashboard job and asks one chat with the kit and the job', async () => {
  const db = makeDb({ sharif: { 'file-system': { Apps: { 'alhazen-experiment-platform': { hub: { kit: { hubkit: { code: '# kit' } } } } } } } });
  const asks = [];
  const mails = []; mails.mana = { mintChatId: async () => 'chat-1', ask: async (...a) => { asks.push(a); } };
  const { host } = hostFor(db, 'sharif', mails);
  assert.equal(host.capabilities.openDashboard, true);
  const seen = [];
  host.subscribeDashboards((v) => seen.push(v));
  const jobId = await host.openDashboard('win-7', 'RazerBlade');
  const job = db.at(MINE('sharif') + '/dashboards/' + jobId);
  assert.equal(job.kind, 'dashboard'); assert.equal(job.status, 'running'); assert.equal(job.chatId, 'chat-1');
  assert.equal(job.machine, 'win-7'); assert.equal(job.machineLabel, 'RazerBlade');
  assert.equal(db.at(MINE('sharif') + '/dashboards/metadata/type'), 'link');
  assert.equal(asks.length, 1);
  const [chat, text, , , , , notify] = asks[0];
  assert.equal(chat, 'chat-1'); assert.equal(notify, true);
  assert.match(text, /hubkit\.open_dashboard\(machine="win-7"/);
  assert.match(text, new RegExp('job_id="' + jobId + '"'));
  assert.match(text, /set_dashboard_job/);
  assert.ok(!/--project/.test(text));
  await flush();
  assert.ok(seen.at(-1) && seen.at(-1)[jobId], 'subscribers see the job');
});
