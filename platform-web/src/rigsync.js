// Machine rig configs, the owner's side of the rules (docs/design/machine-rig-configs.md §4), for the
// Mana host. A JavaScript port of alhazen.experiment_platform.rigsync: the same inputs give the same
// records and the same refusals (platform-web/test/rigsync.test.mjs replays cases generated from the
// Python module). The standalone hosts apply the Python rules on the server; the Mana host writes the
// viewer's own Firebase record, so the web app applies them here. Pure: no Firebase, no clock.
const RIG_SCHEMA = 1;
const RIG_MAX_TEXT = 256 * 1024;
const RIG_MAX_CONFIGS = 64;
const RIG_MAX_NAME = 64;
const RIG_MACHINE_ID = /^[A-Za-z0-9_-]{6,64}$/;
const RIG_CONFIG_ID = /^[A-Za-z0-9_-]{2,40}$/;
const RIG_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
const RIG_STATES = ['synced', 'pending', 'rejected'];

class RigSyncError extends Error {
  // kind: 'invalid' (400), 'conflict' (409), 'missing' (404), as in the Python module.
  constructor(message, kind = 'invalid') { super(message); this.name = 'RigSyncError'; this.kind = kind; }
}

// SHA-256 of a string's UTF-8 bytes, as hex: rigsync.sha256_text. Synchronous, so the rules stay
// plain functions in every host (crypto.subtle is asynchronous and missing on insecure origins).
const RIG_K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2]);

function rigUtf8(text) { return new TextEncoder().encode(String(text)); }

function rigSha256(text) {
  const data = rigUtf8(text);
  const bitLen = data.length * 8;
  const padded = new Uint8Array(((data.length + 9 + 63) >> 6) << 6);
  padded.set(data); padded[data.length] = 0x80;
  const view = new DataView(padded.buffer);
  view.setUint32(padded.length - 8, Math.floor(bitLen / 0x100000000));
  view.setUint32(padded.length - 4, bitLen >>> 0);
  const h = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
  const w = new Uint32Array(64);
  const rot = (x, n) => (x >>> n) | (x << (32 - n));
  for (let off = 0; off < padded.length; off += 64) {
    for (let i = 0; i < 16; i++) w[i] = view.getUint32(off + i * 4);
    for (let i = 16; i < 64; i++) {
      const s0 = rot(w[i - 15], 7) ^ rot(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = rot(w[i - 2], 17) ^ rot(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }
    let [a, b, c, d, e, f, g, k] = h;
    for (let i = 0; i < 64; i++) {
      const t1 = (k + (rot(e, 6) ^ rot(e, 11) ^ rot(e, 25)) + ((e & f) ^ (~e & g)) + RIG_K[i] + w[i]) >>> 0;
      const t2 = ((rot(a, 2) ^ rot(a, 13) ^ rot(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) >>> 0;
      k = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b; b = a; a = (t1 + t2) >>> 0;
    }
    h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e; h[5] += f; h[6] += g; h[7] += k;
  }
  return Array.from(h, (x) => x.toString(16).padStart(8, '0')).join('');
}

const rigClone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));
const rigIsObject = (v) => !!v && typeof v === 'object' && !Array.isArray(v);

function rigCheckMachineId(mid) {
  if (typeof mid !== 'string' || !RIG_MACHINE_ID.test(mid)) throw new RigSyncError("A machine id is 6 to 64 letters, digits, '_' or '-'");
  return mid;
}
function rigCheckConfigId(cid) {
  if (typeof cid !== 'string' || !RIG_CONFIG_ID.test(cid)) throw new RigSyncError("A rig config id is 2 to 40 letters, digits, '_' or '-'");
  return cid;
}
function rigCheckName(name) {
  if (typeof name !== 'string' || name.length > RIG_MAX_NAME || !RIG_NAME.test(name.trim())) {
    throw new RigSyncError("A rig config's name is letters, digits, '.', '_' or '-', starting with a letter "
      + "or digit, at most 64 characters (it becomes the rig's file name, rig-<name>.yaml)");
  }
  return name.trim();
}
function rigCheckText(text) {
  if (typeof text !== 'string' || !text.trim()) throw new RigSyncError("A rig config's text is empty");
  if (rigUtf8(text).length > RIG_MAX_TEXT) throw new RigSyncError('A rig config must be at most 256 KiB');
  return text;
}
function rigWhole(value, what) {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 0) throw new RigSyncError(what + ' must be a whole number of at least 0');
  return value;
}

function rigNewRecord(mid) {
  return { schema: RIG_SCHEMA, id: rigCheckMachineId(mid), label: null, alhazen: null, seenAt: null, token: null,
    default: null, defaultRev: 0, defaultBy: null, rigSchema: null, rigSchemaSha: null, templates: null, configs: {} };
}

function rigBlankConfig(cid, name) {
  return { id: cid, name, text: '', sha256: null, revision: 0, state: 'synced', deleted: false, updatedAt: null,
    updatedBy: null, basedOn: null, running: null, rejection: null, conflict: null, calibration: null };
}

// rigsync.normalize: every field of the contract present, as a fresh copy. Firebase drops empty
// values ({}, [], null), so records read back from it have fields missing; this restores them.
function rigNormalize(record, mid) {
  const out = rigNewRecord(mid);
  if (rigIsObject(record)) {
    for (const key of Object.keys(out)) if (key !== 'configs' && key in record && record[key] !== undefined) out[key] = rigClone(record[key]);
    for (const [cid, config] of Object.entries(record.configs || {})) {
      if (!rigIsObject(config)) continue;
      const full = rigBlankConfig(String(cid), String(config.name || cid));
      for (const key of Object.keys(full)) if (key in config && config[key] !== undefined) full[key] = rigClone(config[key]);
      full.id = String(cid);
      full.deleted = !!full.deleted;
      if (!RIG_STATES.includes(full.state)) full.state = 'synced';
      out.configs[String(cid)] = full;
    }
  }
  out.id = mid;
  if (typeof out.defaultRev !== 'number' || !Number.isInteger(out.defaultRev)) out.defaultRev = 0;
  return out;
}

function rigLive(record) {
  const out = {};
  for (const [cid, c] of Object.entries(record.configs)) if (!c.deleted) out[cid] = c;
  return out;
}

function rigNameTaken(record, name, cid) {
  return Object.entries(rigLive(record)).some(([other, c]) => c.name === name && other !== cid);
}

function rigConfigOf(rec, cid) {
  const config = rec.configs[rigCheckConfigId(cid)];
  if (!config || config.deleted) throw new RigSyncError('This machine has no such rig config', 'missing');
  return config;
}

function rigNeedRecord(record) {
  if (record === null || record === undefined) throw new RigSyncError('This machine has not synced with the platform yet', 'missing');
}

// rigsync.owner_put: a config created or changed on the platform, a new pending revision for the
// machine to take; refused ('conflict') when `base` is not the current revision.
function rigOwnerPut(record, mid, cid, { name, text, base, basedOn, now }) {
  rigNeedRecord(record);
  const rec = rigNormalize(record, mid);
  rigCheckConfigId(cid);
  name = rigCheckName(name);
  text = rigCheckText(text);
  base = rigWhole(base, 'base');
  let current = rec.configs[cid];
  if (current && current.deleted) throw new RigSyncError('This rig config is being deleted', 'conflict');
  const revision = current ? current.revision : 0;
  if (base !== revision) throw new RigSyncError('This rig config changed since you opened it; reopen it and make the change again', 'conflict');
  if (rigNameTaken(rec, name, cid)) throw new RigSyncError("This machine already has a rig config named '" + name + "'");
  if (!current) {
    if (Object.keys(rigLive(rec)).length >= RIG_MAX_CONFIGS) throw new RigSyncError('A machine keeps at most ' + RIG_MAX_CONFIGS + ' rig configs');
    current = rec.configs[cid] = rigBlankConfig(cid, name);
  }
  Object.assign(current, {
    name, text, sha256: rigSha256(text), revision: revision + 1, state: 'pending', updatedAt: now, updatedBy: 'platform',
    basedOn: typeof basedOn === 'string' && basedOn ? basedOn : (current.sha256 === undefined ? null : current.sha256), rejection: null,
  });
  return rec;
}

// rigsync.owner_delete: pending until the machine removes it; a config the machine never ran is
// dropped at once; the default config is refused.
function rigOwnerDelete(record, mid, cid, { base, now }) {
  rigNeedRecord(record);
  const rec = rigNormalize(record, mid);
  const current = rigConfigOf(rec, cid);
  if (rigWhole(base, 'base') !== current.revision) throw new RigSyncError('This rig config changed since you opened it; reopen it before deleting it', 'conflict');
  if (rec.default === cid) throw new RigSyncError("This is the machine's default rig config: make another the default first");
  if (current.running === null) { delete rec.configs[cid]; return rec; }
  Object.assign(current, { deleted: true, state: 'pending', revision: current.revision + 1, updatedAt: now, updatedBy: 'platform', rejection: null });
  return rec;
}

function rigOwnerDefault(record, mid, cid, { base }) {
  rigNeedRecord(record);
  const rec = rigNormalize(record, mid);
  rigConfigOf(rec, cid);
  if (rigWhole(base, 'base') !== rec.defaultRev) throw new RigSyncError('The default changed since you opened this page; reload it', 'conflict');
  if (rec.default !== cid) Object.assign(rec, { default: cid, defaultRev: rec.defaultRev + 1, defaultBy: 'platform' });
  return rec;
}

// rigsync.owner_resolve: keep 'machine' (its version becomes the platform's) or 'platform'; either
// way a new pending revision whose basedOn is the machine's version, so the machine takes it.
function rigOwnerResolve(record, mid, cid, { keep, now }) {
  rigNeedRecord(record);
  const rec = rigNormalize(record, mid);
  const current = rigConfigOf(rec, cid);
  const conflict = current.conflict;
  if (!conflict) throw new RigSyncError('This rig config has no conflict to settle', 'conflict');
  if (keep !== 'machine' && keep !== 'platform') throw new RigSyncError('Keep "machine" or "platform"');
  const text = keep === 'machine' ? conflict.text : current.text;
  Object.assign(current, { text, sha256: rigSha256(text), revision: current.revision + 1, state: 'pending', updatedAt: now,
    updatedBy: 'platform', basedOn: conflict.sha256, conflict: null, rejection: null });
  return rec;
}

// rigsync.summary: a record for a list, without schema, templates or config texts.
function rigSummary(record) {
  const out = {};
  for (const [k, v] of Object.entries(record)) if (!['rigSchema', 'templates', 'configs'].includes(k)) out[k] = v;
  out.hasSchema = !!record.rigSchema;
  out.configs = {};
  for (const [cid, c] of Object.entries(record.configs || {})) {
    const lite = {};
    for (const [k, v] of Object.entries(c)) if (!['text', 'running', 'conflict'].includes(k)) lite[k] = v;
    lite.runningSha256 = (c.running || {}).sha256 === undefined ? null : (c.running || {}).sha256;
    lite.problem = (c.running || {}).problem === undefined ? null : (c.running || {}).problem;
    lite.hasConflict = !!c.conflict;
    out.configs[cid] = lite;
  }
  return out;
}

// Firebase layout of a machine record (the Mana host): configs live under `rigs/<cid>`, beside a
// `metadata` entry; Firebase stores no nulls, so writes leave them out and reads normalize.
function rigFromFirebase(node, mid) {
  if (!rigIsObject(node)) return null;
  const { rigs, metadata, ...rest } = node;
  return rigNormalize({ ...rest, configs: rigs || {} }, mid);
}

function rigStripNulls(value) {
  if (Array.isArray(value)) return value.map(rigStripNulls);
  if (!rigIsObject(value)) return value;
  const out = {};
  for (const [k, v] of Object.entries(value)) if (v !== null && v !== undefined) out[k] = rigStripNulls(v);
  return out;
}

// The multi-location update that turns record `before` into `after` (both normalized), for
// FS.update on the machine node: only the configs and top-level fields that changed, so a
// concurrent sync of another config is not overwritten.
function rigFirebasePatch(before, after, now) {
  const patch = {};
  for (const key of ['default', 'defaultRev', 'defaultBy']) if (JSON.stringify(before[key]) !== JSON.stringify(after[key])) patch[key] = after[key];
  const ids = new Set([...Object.keys(before.configs), ...Object.keys(after.configs)]);
  for (const cid of ids) {
    const a = before.configs[cid], b = after.configs[cid];
    if (JSON.stringify(a) === JSON.stringify(b)) continue;
    patch['rigs/' + cid] = b ? rigStripNulls(b) : null;
  }
  if (Object.keys(patch).length) patch['metadata/updatedAt'] = now;
  return patch;
}
