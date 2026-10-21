// Machine rig configs in the web app (docs/design/machine-rig-configs.md): each machine's named rig
// configs as its dashboard last synced them, an editor (a form by sections from the machine's rig
// schema, and the YAML), creating, renaming, the default, deleting, conflicts and refusals, plus the
// rig config choice for a run. Everything goes through `host` (listRigMachines, subscribeRigMachines,
// subscribeRigMachine, putRigConfig, deleteRigConfig, setRigDefault, resolveRigConfig,
// forgetRigMachine, loadYaml); the machine validates every change and is what runs.

// ------------------------------------------------------------------ pure helpers (tested in node)
const RIG_SECTIONS = [['monitor', 'Monitor'], ['display', 'Display'], ['eyetracker', 'Eye tracker'], ['reward', 'Reward'],
  ['sync', 'Sync'], ['devices', 'Other devices'], ['live_monitor', 'Live monitor'], ['data', 'Data'], ['other', 'Other']];
// Event lines belong to an experiment and its task, set per session; a machine config names none.
const RIG_EVENT_PATHS = ['devices.sync.event_lines', 'display.photodiode.events'];
const rigIsPlainObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

// The dashboard's sectionOf (cli/assets/workspace_rig.js): the heading a setting is shown under.
function rigSectionOf(path) {
  const [first, second] = path;
  if (first === 'monitor' || first === 'display') return first;
  if (first === 'devices') return ['eyetracker', 'reward', 'sync'].includes(second) ? second : 'devices';
  if (first === 'live_monitor' || first === 'dashboard') return 'live_monitor';
  if (['data_root', 'database', 'real_data'].includes(first)) return 'data';
  return 'other';
}

// How many steps of a path its heading already says (Monitor: monitor.width_px shows width_px).
function rigSectionDepth(path) {
  const section = rigSectionOf(path);
  if (['eyetracker', 'reward', 'sync', 'devices'].includes(section)) return Math.min(2, path.length - 1);
  if (section === 'data' || section === 'other') return 0;
  return 1;
}

function rigIsEventPath(path) {
  const dotted = path.join('.');
  return RIG_EVENT_PATHS.some((p) => dotted === p || dotted.startsWith(p + '.'));
}

// Each setting of `values` as {path, value}: sections are walked; a list or an empty section is a value.
function rigLeaves(values, prefix = []) {
  const out = [];
  for (const [key, value] of Object.entries(values || {})) {
    const path = [...prefix, key];
    if (rigIsPlainObject(value) && Object.keys(value).length) out.push(...rigLeaves(value, path));
    else out.push({ path, value });
  }
  return out;
}

// The event lines a config names (paths that carry a value).
function rigEventPathsIn(values) {
  return rigLeaves(values).filter((l) => rigIsEventPath(l.path) && l.value !== null && !(Array.isArray(l.value) && !l.value.length)
    && !(rigIsPlainObject(l.value) && !Object.keys(l.value).length)).map((l) => l.path.join('.'));
}

// The JSON schema node for one setting: $ref followed into $defs, a nullable anyOf reduced to its
// other branch (nullable: true), properties and additionalProperties walked. Null when unknown.
function rigSchemaDeref(schema, node) {
  let n = node; let guard = 0;
  while (n && n.$ref && guard++ < 20) {
    const m = /^#\/(\$defs|definitions)\/(.+)$/.exec(n.$ref);
    const target = m && schema[m[1]] ? schema[m[1]][m[2]] : null;
    n = target ? { ...target, ...Object.fromEntries(Object.entries(n).filter(([k]) => k !== '$ref')) } : null;
  }
  if (n && Array.isArray(n.allOf) && n.allOf.length === 1) n = { ...rigSchemaDeref(schema, n.allOf[0]), ...n, allOf: undefined };
  return n;
}

function rigSchemaBranch(schema, node) {
  const n = rigSchemaDeref(schema, node);
  if (!n) return { node: null, nullable: false };
  const branches = n.anyOf || n.oneOf;
  if (Array.isArray(branches)) {
    const real = branches.map((b) => rigSchemaDeref(schema, b)).filter((b) => b && b.type !== 'null');
    const nullable = real.length < branches.length;
    if (real.length === 1) return { node: { ...real[0], default: n.default, title: n.title || real[0].title, description: n.description || real[0].description }, nullable };
    return { node: { ...n, anyOf: real }, nullable };
  }
  return { node: n, nullable: n.type === 'null' };
}

function rigSchemaAt(schema, path) {
  if (!rigIsPlainObject(schema)) return null;
  let node = schema;
  for (const key of path) {
    const { node: n } = rigSchemaBranch(schema, node);
    if (!n) return null;
    if (n.properties && n.properties[key]) node = n.properties[key];
    else if (rigIsPlainObject(n.additionalProperties)) node = n.additionalProperties;
    else return null;
  }
  return rigSchemaBranch(schema, node);
}

// Which control a setting takes: a choice for an enum, a checkbox for a boolean, a number field,
// a text field, or JSON for a list, a mapping, null and anything the schema does not describe.
function rigFieldKind(schema, path, value) {
  const found = rigSchemaAt(schema, path);
  const node = found && found.node;
  const nullable = !!(found && found.nullable);
  if (node && Array.isArray(node.enum) && node.enum.every((x) => typeof x === 'string' || typeof x === 'number')) return { kind: 'select', options: node.enum, nullable };
  const type = node && node.type;
  if (type === 'boolean' || (!type && typeof value === 'boolean')) return { kind: 'bool', nullable };
  if (type === 'integer' || (!type && Number.isInteger(value))) return { kind: 'integer', nullable };
  if (type === 'number' || (!type && typeof value === 'number')) return { kind: 'number', nullable };
  if (type === 'string' || (!type && typeof value === 'string')) return { kind: 'text', nullable };
  return { kind: 'json', nullable };
}

// YAML through the `yaml` package (host.loadYaml()). Parsed as YAML 1.1, as the rig's PyYAML reads
// it; edits go into the parsed document, so comments and layout around them are kept.
const RIG_YAML_PARSE = { version: '1.1', prettyErrors: true };
const RIG_YAML_DUMP = { lineWidth: 0, flowCollectionPadding: false };

function rigParse(Y, text) {
  try {
    const doc = Y.parseDocument(String(text || ''), RIG_YAML_PARSE);
    if (doc.errors && doc.errors.length) return { doc: null, values: null, error: doc.errors[0].message.split('\n')[0] };
    const values = doc.toJS();
    if (values !== null && values !== undefined && !rigIsPlainObject(values)) return { doc: null, values: null, error: 'A rig config is a mapping of settings (monitor:, display:, …).' };
    return { doc, values: values || {}, error: null };
  } catch (e) {
    return { doc: null, values: null, error: String((e && e.message) || e).split('\n')[0] };
  }
}

// The text with `path` set to `value` (null removes nothing; it writes null). A scalar keeps its node,
// so a comment on its line stays.
function rigSetValue(Y, text, path, value) {
  const { doc, error } = rigParse(Y, text);
  if (!doc) throw new Error(error || 'The YAML does not parse');
  const node = doc.getIn(path, true);
  const scalarValue = value === null || ['string', 'number', 'boolean'].includes(typeof value);
  if (node && Y.isScalar(node) && scalarValue) node.value = value;
  else {
    const flat = Array.isArray(value) && value.every((x) => x === null || typeof x !== 'object');
    doc.setIn(path, doc.createNode(value, { flow: flat }));
  }
  return doc.toString(RIG_YAML_DUMP);
}

// The text without the given paths (a template's event lines).
function rigDeletePaths(Y, text, paths) {
  const { doc, error } = rigParse(Y, text);
  if (!doc) throw new Error(error || 'The YAML does not parse');
  for (const p of paths) doc.deleteIn(p.split('.'));
  return doc.toString(RIG_YAML_DUMP);
}

const rigStable = (v) => JSON.stringify(v, (k, x) => (rigIsPlainObject(x) ? Object.fromEntries(Object.keys(x).sort().map((key) => [key, x[key]])) : x));

// What changed between two parsed configs, setting by setting: [{path, from, to}] (undefined = not set).
function rigChanges(before, after) {
  const a = new Map(rigLeaves(before).map((l) => [l.path.join('.'), l.value]));
  const b = new Map(rigLeaves(after).map((l) => [l.path.join('.'), l.value]));
  const out = [];
  for (const key of new Set([...a.keys(), ...b.keys()])) if (rigStable(a.get(key)) !== rigStable(b.get(key))) out.push({ path: key, from: a.get(key), to: b.get(key) });
  return out;
}

function rigShowValue(v) {
  if (v === undefined) return 'not set';
  if (typeof v === 'string') return v;
  return JSON.stringify(v);
}

// One config's state, in words, from the platform's record (a full config or a summary row).
function rigConfigState(c, machineLabel) {
  const who = machineLabel || 'the machine';
  if (c.deleted) return { tone: 'warn', words: 'Being deleted', detail: 'Waiting for ' + who + ' to delete it' };
  if (c.conflict || c.hasConflict) return { tone: 'bad', words: 'Conflict', detail: 'Changed on both sides since they last agreed; keep one of the two' };
  if (c.state === 'pending') return { tone: 'warn', words: 'Waiting for the machine', detail: 'Waiting for ' + who + ' to take it' };
  if (c.state === 'rejected') return { tone: 'bad', words: 'Refused by the machine', detail: (c.rejection && c.rejection.reason) || 'refused' };
  return { tone: 'ok', words: 'Synced', detail: '' };
}

const rigProblemOf = (c) => (c.running && c.running.problem) || c.problem || null;

// A machine's configs that are not being deleted: the default first, then by name.
function rigLiveConfigs(record) {
  return Object.values((record && record.configs) || {}).filter((c) => c && !c.deleted)
    .sort((a, b) => (a.id === record.default ? -1 : b.id === record.default ? 1 : String(a.name).localeCompare(String(b.name))));
}

// The machine's templates ({list, error}): JSON text it sent at its last sync. Text that is not a JSON
// list is reported to the person (error), not passed over.
function rigTemplatesOf(record) {
  if (!record || typeof record.templates !== 'string') return { list: [], error: null };
  let list;
  try { list = JSON.parse(record.templates); }
  catch (e) { if (!(e instanceof SyntaxError)) throw e; return { list: [], error: 'The templates ' + rigMachineName(record) + ' sent are not JSON (' + e.message + ').' }; }
  if (!Array.isArray(list)) return { list: [], error: 'The templates ' + rigMachineName(record) + ' sent are not a list.' };
  return { list: list.filter((t) => t && typeof t.text === 'string'), error: null };
}

// The machine's rig schema ({schema, error}); without one the form chooses controls by value.
function rigSchemaOf(record) {
  if (!record || typeof record.rigSchema !== 'string') return { schema: null, error: null };
  try { return { schema: JSON.parse(record.rigSchema), error: null }; }
  catch (e) { if (!(e instanceof SyntaxError)) throw e; return { schema: null, error: 'The rig schema ' + rigMachineName(record) + ' sent is not JSON (' + e.message + '), so each setting is edited by the kind of value it has.' }; }
}

function rigNewId() {
  const bytes = new Uint8Array(5);
  (window.crypto || globalThis.crypto).getRandomValues(bytes);
  return 'c' + Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

function rigMachineName(m) { return (m && (m.label || m.id)) || 'this machine'; }

// The Run dialog's choices: the configs of the machine whose label matches the terminal window's
// device, else every machine's configs grouped by machine. Values are config names (what the
// dashboard's --rig-config takes); a config the machine has not taken or reports a problem with is
// listed but not selectable.
function rigPickerGroups(machines, deviceLabel) {
  const norm = (s) => String(s || '').trim().toLowerCase();
  const list = (machines || []).filter((m) => m && Object.keys(m.configs || {}).length);
  const matched = deviceLabel ? list.filter((m) => norm(m.label) === norm(deviceLabel)) : [];
  const use = matched.length ? matched : list;
  return {
    matched: matched.length > 0,
    groups: use.map((m) => ({
      mid: m.id, label: rigMachineName(m),
      configs: rigLiveConfigs(m).map((c) => {
        const problem = rigProblemOf(c);
        const waiting = c.state === 'pending' && !c.runningSha256 && !(c.running && c.running.sha256);
        return { value: m.id + '|' + c.name, name: c.name, isDefault: c.id === m.default,
          disabled: !!problem || waiting, note: problem ? 'the machine reports a problem' : waiting ? 'not on the machine yet' : '' };
      }),
    })).filter((g) => g.configs.length),
  };
}

function rigAgo(ms) {
  if (!ms) return '';
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + ' min ago';
  if (s < 86400) return Math.floor(s / 3600) + ' h ago';
  return Math.floor(s / 86400) + ' d ago';
}

function rigWhen(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d.getTime()) ? String(iso) : d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

// ------------------------------------------------------------------ components
function useRigYaml(host) {
  const [state, setState] = React.useState({ Y: null, error: null });
  React.useEffect(() => {
    let off = false;
    Promise.resolve().then(() => host.loadYaml()).then((Y) => { if (!off) setState({ Y: Y && Y.default && !Y.parseDocument ? Y.default : Y, error: null }); })
      .catch((e) => { if (!off) setState({ Y: null, error: (e && e.message) || String(e) }); });
    return () => { off = true; };
  }, [host.key]);
  return state;
}

function useRigMachines(host) {
  const [data, setData] = React.useState(undefined);
  React.useEffect(() => {
    if (!host.subscribeRigMachines) return undefined;
    return host.subscribeRigMachines((v) => setData(v || { machines: [] }), (e) => setData({ error: (e && e.message) || String(e) }));
  }, [host.key]);
  return data;
}

function RigStatus({ c, label }) {
  const st = rigConfigState(c, label);
  return <span className={'status st-' + st.tone}>{st.words}</span>;
}

// The list on Machines / My machines: every machine that synced, with its configs.
function RigConfigsSection({ host, go, onFlash }) {
  const data = useRigMachines(host);
  const [forgetting, setForgetting] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  const machines = (data && data.machines ? data.machines : []).slice().sort((a, b) => (b.seenAt || 0) - (a.seenAt || 0));
  async function forget(m) {
    setBusy(true);
    try { await host.forgetRigMachine(m.id); setForgetting(''); onFlash('ok', rigMachineName(m) + ' was removed here; it comes back at its next sync.'); }
    catch (e) { onFlash('bad', 'Could not forget ' + rigMachineName(m) + ': ' + ((e && e.message) || e)); }
    finally { setBusy(false); }
  }
  return <section className="eh-rigs" aria-labelledby="eh-rigs-title">
    <h2 id="eh-rigs-title" className="eh-rigs-title">Rig configs</h2>
    <p className="eh-muted eh-rigs-lede">The rig settings each machine runs its sessions with. Change them here or on the machine's dashboard; the two stay in step, and the machine checks every change before it uses it.</p>
    {data === undefined ? <p className="eh-muted">Loading…</p>
      : data.error ? <p className="eh-rig-error" role="alert">The rig configs could not be read: {data.error}</p>
      : null}
    {data && data.problems && data.problems.length ? <p className="eh-rig-error" role="alert">{data.problems.length === 1 ? 'One machine record' : data.problems.length + ' machine records'} could not be read: {data.problems.join('; ')}</p> : null}
    {data === undefined || data.error ? null
      : !machines.length ? <div className="empty"><h3>No machine has synced its rig configs yet</h3><p>A machine appears here once its alhazen dashboard (3.6 or newer) is connected to this library.</p></div>
      : machines.map((m) => {
        const configs = rigLiveConfigs(m);
        return <div key={m.id} className="eh-rig-machine">
          <div className="eh-rig-mhead">
            <div className="eh-rig-mtitle"><h3>{rigMachineName(m)}</h3>
              <p>{[m.alhazen ? 'alhazen ' + m.alhazen : '', m.seenAt ? 'synced ' + rigAgo(m.seenAt) : 'not synced yet'].filter(Boolean).join(' · ')}</p></div>
            {forgetting === m.id
              ? <div className="eh-actions eh-rig-confirm"><span>Forget {rigMachineName(m)}? Its configs stay on the machine.</span><button className="btn btn-line btn-small" onClick={() => setForgetting('')}>Keep</button><button className="btn btn-primary btn-small" disabled={busy} onClick={() => forget(m)}>Forget</button></div>
              : <div className="eh-actions"><button className="btn btn-line btn-small" onClick={() => go({ v: 'machines', id: m.id, tab: 'new' })}>Add a config</button><button className="btn btn-quiet btn-small" onClick={() => setForgetting(m.id)}>Forget</button></div>}
          </div>
          <div className="list">
            {configs.length ? configs.map((c) => {
              const st = rigConfigState(c, rigMachineName(m)); const problem = rigProblemOf(c);
              return <button key={c.id} className="row rowbtn eh-rig-row" onClick={() => go({ v: 'machines', id: m.id, tab: c.id })}>
                <div className="row-main"><div className="row-title">{c.name}{c.id === m.default ? <span className="eh-rig-default">Default</span> : null}</div>
                  {st.detail ? <div className="row-sub">{st.detail}</div> : null}
                  {problem ? <div className="row-sub eh-rig-problem">The machine reports: {problem}</div> : null}</div>
                <RigStatus c={c} label={rigMachineName(m)} />
              </button>;
            }) : <div className="row"><div className="row-main"><div className="row-sub">No configs on this machine.</div></div></div>}
          </div>
        </div>;
      })}
  </section>;
}

// One setting in the form.
function RigField({ id, path, value, schema, disabled, onSet }) {
  const kind = rigFieldKind(schema, path, value);
  const [raw, setRaw] = React.useState(null); // text being typed in a number or JSON field
  const [bad, setBad] = React.useState('');
  React.useEffect(() => { setRaw(null); setBad(''); }, [rigStable(value)]);
  const shown = raw !== null ? raw : kind.kind === 'json' ? JSON.stringify(value) : value === null || value === undefined ? '' : String(value);
  function commitTyped(text) {
    if (kind.kind === 'json') {
      let value;
      try { value = JSON.parse(text); } catch (e) { if (!(e instanceof SyntaxError)) throw e; setBad('Not valid JSON: ' + e.message); return; }
      onSet(value); setBad(''); setRaw(null);
      return;
    }
    const t = text.trim();
    if (!t) { if (kind.nullable) { onSet(null); setBad(''); setRaw(null); } else setBad('Needs a value'); return; }
    const n = Number(t);
    if (!Number.isFinite(n) || (kind.kind === 'integer' && !Number.isInteger(n))) { setBad(kind.kind === 'integer' ? 'A whole number' : 'A number'); return; }
    onSet(n); setBad(''); setRaw(null);
  }
  let control;
  if (kind.kind === 'bool') control = <label className="check eh-rig-check"><input id={id} type="checkbox" checked={value === true} disabled={disabled} onChange={(e) => onSet(e.target.checked)} />{value === true ? 'Yes' : value === false ? 'No' : 'Not set'}</label>;
  else if (kind.kind === 'select') control = <select id={id} value={value === null || value === undefined ? '' : String(value)} disabled={disabled}
    onChange={(e) => { const v = e.target.value; const opt = kind.options.find((o) => String(o) === v); onSet(v === '' ? null : opt === undefined ? v : opt); }}>
    {kind.nullable || value === null || value === undefined ? <option value="">Not set</option> : null}
    {kind.options.map((o) => <option key={String(o)} value={String(o)}>{String(o)}</option>)}
    {value !== null && value !== undefined && !kind.options.some((o) => String(o) === String(value)) ? <option value={String(value)}>{String(value)} (not a known choice)</option> : null}
  </select>;
  else if (kind.kind === 'text') control = <input id={id} type="text" value={shown} disabled={disabled} spellCheck={false} onChange={(e) => onSet(e.target.value)} />;
  else control = <input id={id} type="text" inputMode={kind.kind === 'json' ? 'text' : 'decimal'} className={kind.kind === 'json' ? 'mono' : ''} value={shown} disabled={disabled} spellCheck={false}
    aria-invalid={bad ? 'true' : undefined} onChange={(e) => setRaw(e.target.value)} onBlur={(e) => { if (raw !== null) commitTyped(e.target.value); }}
    onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); commitTyped(e.currentTarget.value); } }} />;
  return <>{control}{bad ? <small className="eh-rig-bad" role="alert">{bad}</small> : null}</>;
}

function RigForm({ values, schema, disabled, onSet }) {
  const rows = rigLeaves(values).filter((l) => !rigIsEventPath(l.path));
  const buckets = new Map(RIG_SECTIONS.map(([key]) => [key, []]));
  for (const row of rows) buckets.get(rigSectionOf(row.path)).push(row);
  const sections = RIG_SECTIONS.filter(([key]) => buckets.get(key).length);
  if (!sections.length) return <p className="eh-muted">This config sets nothing yet. Add settings in the YAML view.</p>;
  return <div className="eh-rig-form">{sections.map(([key, label]) => (
    <section key={key} className="eh-rig-section" aria-labelledby={'eh-rig-sec-' + key}>
      <h3 id={'eh-rig-sec-' + key}>{label}</h3>
      {buckets.get(key).map(({ path, value }) => {
        const id = 'eh-rig-f-' + path.join('-');
        const dotted = path.join('.');
        return <div key={dotted} className="eh-rig-field">
          <label htmlFor={id}>{path.slice(rigSectionDepth(path)).join('.') || dotted}</label>
          <div className="eh-rig-control">
            <RigField id={id} path={path} value={value} schema={schema} disabled={disabled} onSet={(v) => onSet(path, v)} />
            {dotted === 'real_data' ? <small>Decides whether Run mode may record subjects with this config. A rehearsal or development config sets it to No.</small> : null}
          </div>
        </div>;
      })}
    </section>))}</div>;
}

// Creating a config on one machine: a name and a starting point (a copy of one of its configs or
// one of the rigs its dashboard offers as templates).
function RigNewConfig({ host, record, go, onFlash, homeLabel = 'Machines' }) {
  const { Y, error: yamlError } = useRigYaml(host);
  const label = rigMachineName(record);
  const configs = rigLiveConfigs(record);
  const { list: templates, error: templatesError } = rigTemplatesOf(record);
  const [name, setName] = React.useState('');
  const [from, setFrom] = React.useState(configs.length ? 'c:' + configs[0].id : templates.length ? 't:0' : '');
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState('');
  const source = from.startsWith('c:') ? configs.find((c) => 'c:' + c.id === from) : from.startsWith('t:') ? templates[Number(from.slice(2))] : null;
  let text = '', problem = '';
  if (source && Y) {
    const parsed = rigParse(Y, source.text);
    if (parsed.error) problem = 'This starting point does not parse: ' + parsed.error;
    else if (parsed.values.extends) problem = 'This rig extends another rig (extends: ' + parsed.values.extends + '); a machine config is complete on its own. Choose another starting point.';
    else {
      const events = rigEventPathsIn(parsed.values);
      try { text = events.length ? rigDeletePaths(Y, source.text, events) : source.text; } catch (e) { problem = String(e.message || e); }
    }
  }
  const nameTaken = configs.some((c) => c.name === name.trim());
  async function create(e) {
    e.preventDefault();
    if (!text || !name.trim() || nameTaken) return;
    setBusy(true); setErr('');
    const cid = rigNewId();
    try {
      await host.putRigConfig(record.id, cid, { name: name.trim(), text, base: 0, basedOn: null });
      onFlash('ok', name.trim() + ' was added. Waiting for ' + label + ' to take it.');
      go({ v: 'machines', id: record.id, tab: cid }, true);
    } catch (error) { setErr((error && error.message) || String(error)); }
    finally { setBusy(false); }
  }
  return <form className="eh-rig-editor eh-rig-new" onSubmit={create}>
    <nav className="eh-crumb" aria-label="Location"><button type="button" onClick={() => go({ v: 'machines' })}>{homeLabel}</button><span>/</span><span>{label}</span></nav>
    <h1 className="eh-rig-h1">Add a rig config to {label}</h1>
    <div className="field"><label htmlFor="eh-rig-new-name">Name</label>
      <input id="eh-rig-new-name" autoFocus value={name} maxLength={64} spellCheck={false} placeholder="monkey-daq" onChange={(e) => setName(e.target.value)} aria-invalid={nameTaken ? 'true' : undefined} />
      <small>{nameTaken ? label + ' already has a config with this name.' : 'Letters, digits, dots, dashes and underscores. The rig file on the machine is named after it.'}</small></div>
    <div className="field"><label htmlFor="eh-rig-new-from">Start from</label>
      {configs.length || templates.length ? <select id="eh-rig-new-from" value={from} onChange={(e) => setFrom(e.target.value)}>
        {configs.length ? <optgroup label={'A copy of a config on ' + label}>{configs.map((c) => <option key={c.id} value={'c:' + c.id}>{c.name}</option>)}</optgroup> : null}
        {[['shared', 'alhazen rigs'], ['experiment', 'Experiment rigs']].map(([kind, heading]) => {
          const items = templates.map((t, i) => [t, i]).filter(([t]) => (t.kind || 'shared') === kind);
          return items.length ? <optgroup key={kind} label={heading}>{items.map(([t, i]) => <option key={i} value={'t:' + i}>{t.label || t.name}</option>)}</optgroup> : null;
        })}
      </select> : <p className="eh-muted">{label} sent no configs or templates to start from.</p>}
      {source && source.summary ? <small>{source.summary}{typeof source.real_data === 'boolean' ? (source.real_data ? ' Records real data.' : ' Does not record real data.') : ''}</small> : null}</div>
    {yamlError ? <p className="eh-rig-error" role="alert">The YAML reader could not be loaded: {yamlError}</p> : null}
    {templatesError ? <p className="eh-rig-error" role="alert">{templatesError}</p> : null}
    {problem ? <p className="eh-rig-error" role="alert">{problem}</p> : null}
    {err ? <p className="eh-rig-error" role="alert">{err}</p> : null}
    <div className="eh-actions eh-rig-actions"><button className="btn btn-primary" type="submit" disabled={busy || !text || !name.trim() || nameTaken}>{busy ? 'Adding…' : 'Add the config'}</button>
      <button className="btn btn-line" type="button" onClick={() => go({ v: 'machines' })}>Cancel</button></div>
  </form>;
}

// The editor for one config.
function RigConfigEditor({ host, mid, cid, go, onFlash, homeLabel = 'Machines' }) {
  const [record, setRecord] = React.useState(undefined);
  const [loadError, setLoadError] = React.useState('');
  React.useEffect(() => host.subscribeRigMachine(mid, (r) => { setRecord(r || null); setLoadError(''); }, (e) => { setLoadError((e && e.message) || String(e)); }), [host.key, mid]);
  const { Y, error: yamlError } = useRigYaml(host);
  const config = record && record.configs ? record.configs[cid] : null;
  const label = rigMachineName(record);
  const { schema, error: schemaError } = React.useMemo(() => rigSchemaOf(record), [record && record.rigSchemaSha, record && record.rigSchema && record.rigSchema.length]);
  // The draft: what the editor started from (revision, text, name) and what it holds now.
  const [draft, setDraft] = React.useState(null);
  const [view, setView] = React.useState('form');
  const [busy, setBusy] = React.useState('');
  const [err, setErr] = React.useState('');
  const [confirmDelete, setConfirmDelete] = React.useState(false);
  const dirty = !!(draft && config && (draft.text !== draft.startText || draft.name.trim() !== draft.startName));
  React.useEffect(() => {
    if (!config || config.deleted) return;
    if (!draft || (!dirty && (draft.base !== config.revision || draft.startText !== config.text))) {
      setDraft({ base: config.revision, startText: config.text, startName: config.name, startSha: config.sha256, text: config.text, name: config.name });
    }
  }, [config && config.revision, config && config.text, config && config.deleted]);

  const back = () => go({ v: 'machines' });
  if (loadError && record === undefined) return <div className="eh-rig-editor"><p className="eh-rig-error" role="alert">This machine could not be read: {loadError}</p><button className="btn btn-line" onClick={back}>Back to {homeLabel}</button></div>;
  if (record === undefined) return <div className="eh-rig-editor"><p className="eh-muted">Loading…</p></div>;
  if (!record) return <div className="eh-rig-editor"><div className="empty"><h3>This machine is not on the platform</h3><p>It appears once its dashboard syncs with this library.</p></div><div className="eh-actions eh-rig-actions"><button className="btn btn-line" onClick={back}>Back to {homeLabel}</button></div></div>;
  if (cid === 'new') return <RigNewConfig host={host} record={record} go={go} onFlash={onFlash} homeLabel={homeLabel} />;
  if (!config || config.deleted) return <div className="eh-rig-editor"><div className="empty"><h3>{config ? 'This config is being deleted' : 'This config is not on the platform'}</h3><p>{config ? 'Waiting for ' + label + ' to delete it.' : 'It was deleted, or the machine has not synced it.'}</p></div><div className="eh-actions eh-rig-actions"><button className="btn btn-line" onClick={back}>Back to {homeLabel}</button></div></div>;
  if (!draft) return <div className="eh-rig-editor"><p className="eh-muted">Loading…</p></div>;

  const st = rigConfigState(config, label);
  const problem = rigProblemOf(config);
  const isDefault = record.default === cid;
  const stale = draft.base !== config.revision;
  const parsed = Y ? rigParse(Y, draft.text) : null;
  const startParsed = Y ? rigParse(Y, draft.startText) : null;
  const changes = parsed && startParsed && parsed.values && startParsed.values ? rigChanges(startParsed.values, parsed.values) : [];
  const events = parsed && parsed.values ? rigEventPathsIn(parsed.values) : [];
  const extendsRig = parsed && parsed.values && parsed.values.extends;
  const nameProblem = !draft.name.trim() ? 'A config needs a name.' : !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(draft.name.trim()) || draft.name.trim().length > 64
    ? 'Letters, digits, dots, dashes and underscores, starting with a letter or digit.'
    : rigLiveConfigs(record).some((c) => c.id !== cid && c.name === draft.name.trim()) ? label + ' already has a config with this name.' : '';
  const blocked = !!(parsed && parsed.error) || events.length > 0 || !!extendsRig || !!nameProblem || !draft.text.trim();

  const run = async (what, fn, done) => {
    setBusy(what); setErr('');
    try { await fn(); if (done) onFlash('ok', done); } catch (e) { setErr((e && e.message) || String(e)); } finally { setBusy(''); }
  };
  const save = () => run('Saving', async () => {
    const res = await host.putRigConfig(mid, cid, { name: draft.name.trim(), text: draft.text, base: draft.base, basedOn: draft.startSha });
    const saved = res && res.config;
    // The editor now starts from what was saved, so the new revision is not taken for someone else's.
    if (saved) setDraft({ base: saved.revision, startText: saved.text, startName: saved.name, startSha: saved.sha256, text: saved.text, name: saved.name });
  }, 'Saved. Waiting for ' + label + ' to take it.');
  const discard = () => setDraft({ ...draft, base: config.revision, startText: config.text, startName: config.name, startSha: config.sha256, text: config.text, name: config.name });
  const setValue = (path, value) => {
    try { setDraft((d) => ({ ...d, text: rigSetValue(Y, d.text, path, value) })); }
    catch (e) { setErr((e && e.message) || String(e)); }
  };
  const cal = config.calibration || {};
  const running = config.running || null;

  return <div className="eh-rig-editor">
    <nav className="eh-crumb" aria-label="Location"><button onClick={back}>{homeLabel}</button><span>/</span><span>{label}</span></nav>
    <div className="eh-rig-titlerow">
      <h1 className="eh-rig-h1">{config.name}{isDefault ? <span className="eh-rig-default">Default</span> : null}</h1>
      <span className={'status st-' + st.tone}>{st.words}</span>
    </div>
    <p className="eh-muted eh-rig-statusline">{[config.state === 'rejected' || config.conflict ? '' : st.detail, config.updatedAt ? 'Last changed ' + (config.updatedBy === 'platform' ? 'here ' : config.updatedBy === 'machine' ? 'on ' + label + ' ' : '') + rigAgo(config.updatedAt) : ''].filter(Boolean).join(' · ')}</p>
    {problem ? <p className="eh-rig-error" role="alert">{label} reports a problem with the version it runs: {problem}</p> : null}

    {config.conflict ? <div className="panel eh-rig-panel eh-rig-conflict">
      <h3>Changed on both sides</h3>
      <p>{label} changed this config while a change made here was waiting. Nothing was overwritten: keep one of the two versions.</p>
      <RigDiffTable Y={Y} leftText={config.conflict.text} rightText={config.text} leftLabel={'On ' + label} rightLabel="Here" />
      <details className="eh-rig-running"><summary>Both versions in full</summary>
        <div className="eh-rig-versions">
          <div><h4>On {label}</h4><pre className="mono">{config.conflict.text}</pre></div>
          <div><h4>Here</h4><pre className="mono">{config.text}</pre></div>
        </div>
      </details>
      <div className="eh-actions eh-rig-actions">
        <button className="btn btn-line" disabled={!!busy} onClick={() => run('Keeping', () => host.resolveRigConfig(mid, cid, 'machine'), 'Kept the version on ' + label + '.')}>Keep the machine’s</button>
        <button className="btn btn-line" disabled={!!busy} onClick={() => run('Keeping', () => host.resolveRigConfig(mid, cid, 'platform'), 'Kept the version here. Waiting for ' + label + ' to take it.')}>Keep the platform’s</button>
      </div>
    </div> : null}

    {config.state === 'rejected' && !config.conflict ? <div className="panel eh-rig-panel">
      <h3>{label} refused the last change</h3>
      <p className="eh-rig-reason">{(config.rejection && config.rejection.reason) || 'No reason was given.'}</p>
      <p>It keeps running its earlier version until a change it accepts arrives. The refused change differs from it in:</p>
      {running ? <RigDiffTable Y={Y} leftText={running.text} rightText={config.text} leftLabel={'What ' + label + ' runs'} rightLabel="The refused change" /> : null}
      {running ? <details className="eh-rig-running"><summary>The version {label} runs, in full</summary><pre className="mono">{running.text}</pre></details> : null}
      {running ? <div className="eh-actions eh-rig-actions"><button className="btn btn-line btn-small" onClick={() => setDraft({ ...draft, text: running.text })}>Edit from the running version</button></div> : null}
    </div> : null}

    {stale ? <div className="panel eh-rig-panel" role="status"><h3>This config changed since you opened it</h3>
      <p>Saving now is refused so that the newer version is not overwritten. Load it, then make your change again.</p>
      <div className="eh-actions eh-rig-actions"><button className="btn btn-line btn-small" onClick={discard}>Load the newer version (drops your changes)</button></div></div> : null}

    <div className="field eh-rig-name"><label htmlFor="eh-rig-name">Name</label>
      <input id="eh-rig-name" value={draft.name} maxLength={64} spellCheck={false} onChange={(e) => setDraft({ ...draft, name: e.target.value })} aria-invalid={nameProblem ? 'true' : undefined} />
      {nameProblem ? <small className="eh-rig-bad">{nameProblem}</small> : draft.name.trim() !== draft.startName ? <small>Renaming renames the rig file on {label}; earlier sessions keep the old name.</small> : null}</div>

    <div className="eh-settings-nav eh-rig-views" role="tablist" aria-label="Edit as">
      {[['form', 'Settings'], ['yaml', 'YAML']].map(([k, l]) => <button key={k} role="tab" aria-selected={view === k} aria-current={view === k ? 'page' : undefined} onClick={() => setView(k)}>{l}</button>)}
    </div>
    {yamlError ? <p className="eh-rig-error" role="alert">The YAML reader could not be loaded ({yamlError}); edit the text in the YAML view.</p> : null}
    {schemaError && view === 'form' ? <p className="eh-rig-error" role="alert">{schemaError}</p> : null}
    {view === 'form' && !yamlError ? (!parsed ? <p className="eh-muted">Loading…</p>
      : parsed.error ? <p className="eh-rig-error" role="alert">The YAML does not parse: {parsed.error}. Fix it in the YAML view.</p>
      : <RigForm values={parsed.values} schema={schema} disabled={!!busy} onSet={setValue} />)
      : <div className="field eh-rig-yamlfield"><label htmlFor="eh-rig-yaml">{config.name}.yaml</label>
        <textarea id="eh-rig-yaml" className="mono eh-rig-yaml" spellCheck={false} value={draft.text} onChange={(e) => setDraft({ ...draft, text: e.target.value })} />
        {parsed && parsed.error ? <small className="eh-rig-bad" role="alert">Does not parse: {parsed.error}</small> : null}</div>}
    {events.length ? <p className="eh-rig-error" role="alert">This config names event lines ({events.join(', ')}). A machine's config carries none and the machine refuses it: event lines are set per experiment on the dashboard's Run page. Remove them in the YAML view.</p> : null}
    {extendsRig ? <p className="eh-rig-error" role="alert">This config extends another rig (extends: {String(extendsRig)}). A machine's config is complete on its own; the machine refuses it.</p> : null}

    {dirty && (changes.length || draft.name.trim() !== draft.startName) ? <div className="eh-rig-changes"><h3>Changes to save</h3><ul>
      {draft.name.trim() !== draft.startName ? <li><span className="mono">name</span> {draft.startName} → {draft.name.trim()}</li> : null}
      {changes.map((c) => <li key={c.path}><span className="mono">{c.path}</span> {rigShowValue(c.from)} → {rigShowValue(c.to)}</li>)}
    </ul></div> : null}
    {err ? <p className="eh-rig-error" role="alert">{err}</p> : null}

    <div className="eh-actions eh-rig-actions eh-rig-savebar">
      <button className="btn btn-primary" disabled={!dirty || blocked || stale || !!busy} onClick={save}>{busy === 'Saving' ? 'Saving…' : 'Save'}</button>
      <button className="btn btn-line" disabled={!dirty || !!busy} onClick={discard}>Discard changes</button>
      <span className="eh-rig-spacer" />
      {!isDefault ? <button className="btn btn-line" disabled={!!busy || config.state === 'pending' && !running} onClick={() => run('Default', () => host.setRigDefault(mid, cid, record.defaultRev), config.name + ' is the default on ' + label + ' from its next sync.')}>Make default</button> : null}
      {confirmDelete
        ? <span className="eh-actions eh-rig-confirm"><span>Delete {config.name}? {running ? label + ' moves it to its trash at its next sync.' : 'It is not on ' + label + ' yet, so it goes at once.'}</span><button className="btn btn-line btn-small" onClick={() => setConfirmDelete(false)}>Keep</button>
          <button className="btn btn-primary btn-small" disabled={!!busy} onClick={() => run('Deleting', async () => { await host.deleteRigConfig(mid, cid, config.revision); back(); }, running ? config.name + ' will be deleted on ' + label + ' at its next sync.' : config.name + ' was deleted.')}>Delete</button></span>
        : <button className="btn btn-quiet" disabled={isDefault || !!busy} title={isDefault ? 'Make another config the default first' : undefined} onClick={() => setConfirmDelete(true)}>Delete</button>}
    </div>
    {isDefault ? <p className="eh-muted eh-rig-note">The default config cannot be deleted; make another one the default first.</p> : null}

    <div className="eh-rig-facts">
      <h3>Calibration on {label}</h3>
      <dl className="kv">
        <dt>Gamma</dt><dd>{cal.gamma && cal.gamma.at ? 'measured ' + rigWhen(cal.gamma.at) : 'not measured'}</dd>
        <dt>Juice per pulse</dt><dd>{cal.reward && cal.reward.at ? 'measured ' + rigWhen(cal.reward.at) + (cal.reward.summary ? ' · ' + cal.reward.summary : '') : 'not measured'}</dd>
      </dl>
      <p className="eh-muted">Measured with Measure rig on the machine and kept there with this config.</p>
    </div>
  </div>;
}

// The settings two versions of a config differ in, side by side (a conflict, a refused change).
function RigDiffTable({ Y, leftText, rightText, leftLabel, rightLabel }) {
  if (!Y) return null;
  const left = rigParse(Y, leftText), right = rigParse(Y, rightText);
  if (left.error || right.error) return <p className="eh-muted">{left.error ? leftLabel : rightLabel} does not parse as YAML; compare the full versions below.</p>;
  const rows = rigChanges(left.values, right.values);
  if (!rows.length) return <p className="eh-muted">The two versions have the same settings; they differ only in comments or layout.</p>;
  return <div className="eh-rig-difftable"><table>
    <thead><tr><th scope="col">Setting</th><th scope="col">{leftLabel}</th><th scope="col">{rightLabel}</th></tr></thead>
    <tbody>{rows.map((r) => <tr key={r.path}><td className="mono">{r.path}</td><td>{rigShowValue(r.from)}</td><td>{rigShowValue(r.to)}</td></tr>)}</tbody>
  </table></div>;
}

// The Mana Run dialog's rig config choice.
function RigConfigPicker({ host, deviceLabel, value, onChange }) {
  const data = useRigMachines(host);
  if (!data || data.error || !(data.machines || []).length) return null;
  const { matched, groups } = rigPickerGroups(data.machines, deviceLabel);
  if (!groups.length) return null;
  const one = matched && groups.length === 1;
  const options = (g) => g.configs.map((c) => <option key={c.value} value={c.value} disabled={c.disabled}>{c.name}{c.isDefault ? ' (default)' : ''}{c.note ? ' — ' + c.note : ''}</option>);
  return <div className="field"><label htmlFor="eh-rig-pick">Rig config</label>
    <select id="eh-rig-pick" value={value || ''} onChange={(e) => onChange(e.target.value)}>
      <option value="">The machine’s default</option>
      {one ? options(groups[0]) : groups.map((g) => <optgroup key={g.mid} label={g.label}>{options(g)}</optgroup>)}
    </select>
    <small>{one ? 'The configs of ' + groups[0].label + '. The dashboard opens with this one chosen on its Run page.' : 'The dashboard opens with this one chosen on its Run page.'}</small></div>;
}

// The standalone Run tab: the person's machines and configs, and how a dashboard opens one.
function RigConfigsBrief({ host }) {
  const data = useRigMachines(host);
  const machines = data && data.machines ? data.machines.filter((m) => Object.keys(m.configs || {}).length) : [];
  if (!machines.length) return null;
  return <div className="eh-rig-brief">
    <dl className="kv">{machines.map((m) => <React.Fragment key={m.id}><dt>{rigMachineName(m)}</dt><dd>{rigLiveConfigs(m).map((c) => c.name + (c.id === m.default ? ' (default)' : '')).join(', ') || 'no configs'}</dd></React.Fragment>)}</dl>
    <p>The dashboard opens a config with <span className="cmd">alhazen dashboard --rig-config NAME</span>, or choose it on its Run page.</p>
  </div>;
}
