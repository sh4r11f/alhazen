import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {test} from 'node:test';
import {FakeDocument} from './fake_dom.mjs';

const assets = new URL('../../src/alhazen/cli/assets/', import.meta.url);
function load() {
  const document = new FakeDocument();
  const html = readFileSync(new URL('workspace.html', assets), 'utf8');
  for (const [,tag,id] of html.matchAll(/<([a-z][\w-]*)\b[^>]*\bid="([^"]+)"/g)) {
    const el = document.createElement(tag); el.setAttribute('id',id); el.value = '';
    document.body.appendChild(el);
  }
  // localStorage: the page reads the remembered colour theme when it loads.
  const sandbox = {document, URLSearchParams, location:{hash:''}, sessionStorage:{getItem:()=>null, setItem(){}}, localStorage:{getItem:()=>null, setItem(){}}, window:{addEventListener(){}}, console};
  const context = vm.createContext(sandbox);
  vm.runInContext(readFileSync(new URL('workspace_parameters.js', assets),'utf8'),context);
  vm.runInContext(readFileSync(new URL('workspace_calibration.js', assets),'utf8'),context);
  vm.runInContext(readFileSync(new URL('workspace.js', assets),'utf8').replace(/\npoll\(\);\s*$/, ''),context);
  const run = (code) => vm.runInContext(code,context);
  run(`state.projects = [{id:'p', scripts:[], rigs:['rig.yaml'], available:true}]; selected='p';`);
  return {run, document, byId:id=>document.getElementById(id)};
}
const schema = {properties:{
  motion:{enum:['static','moving'], default:'static'},
  stimuli:{type:'array', items:{type:'string'}, default:['bars','kanizsa']},
  paradigm:{$ref:'#/$defs/Paradigm'},
}, $defs:{Paradigm:{properties:{kind:{enum:['constant','sequence']}}}}};

test('text fields use model choices, including nested schema references', () => {
  const app = load();
  app.run(`parameterSchema = ${JSON.stringify(schema)}; values = {motion:'moving', paradigm:{kind:'constant'}}; renderEditor();`);
  const motion = app.byId('param-0'), kind = app.byId('param-1');
  assert.equal(motion.localName,'select');
  assert.deepEqual(motion.children.map(o=>o.value),['static','moving']);
  assert.equal(motion.value,'moving');
  assert.deepEqual(kind.children.map(o=>o.value),['constant','sequence']);
  kind.value = 'sequence'; kind.fire('change');
  assert.equal(app.run('values.paradigm.kind'),'sequence');
});

test('multi-value dropdown keeps unselected model defaults available and preserves order', () => {
  const app = load();
  app.run(`parameterSchema = ${JSON.stringify(schema)}; values = {stimuli:['kanizsa']}; renderEditor();`);
  const checks = app.byId('parameter-fields').querySelectorAll('input');
  assert.equal(checks.length,2);
  assert.equal(checks[0].checked,false); assert.equal(checks[1].checked,true);
  checks[0].checked = true; checks[0].fire('change');
  assert.equal(app.run('JSON.stringify(values.stimuli)'), '["kanizsa","bars"]');
  checks[1].checked = false; checks[1].fire('change');
  assert.equal(app.run('JSON.stringify(values.stimuli)'), '["bars"]');
});

test('measure hides and disables parameter controls without losing edits or blocking on schema', () => {
  const app = load();
  app.run(`values={motion:'moving'}; loadingSchema=true; loadingConfig=true;`);
  app.byId('mode').value = 'measure'; app.run('modeChanged()');
  assert.equal(app.byId('task-parameters').hidden,true);
  assert.equal(app.byId('task-parameters').disabled,true);
  // The parameter-file menu, which used to sit in its own field
  // (params-preset-field, hidden here), is now in the Task parameters
  // heading inside this fieldset (nesting pinned in workspace.test.mjs, "the
  // launch form"), and is disabled with it.
  assert.equal(app.byId('params-config').disabled,true);
  assert.equal(app.byId('launch').disabled,false);
  assert.equal(app.run('usesParameters()'),false);
  app.byId('mode').value = 'movie'; app.run('modeChanged()');
  assert.equal(app.byId('task-parameters').hidden,false);
  assert.equal(app.byId('task-parameters').disabled,false);
  assert.equal(app.byId('params-config').disabled,false);
  assert.equal(app.run('values.motion'),'moving');
});

test('scripts without parameter-file support hide parameters too', () => {
  const app = load();
  app.run(`state.projects[0].scripts=[{id:'preview',flags:[],params_flag:null}];`);
  app.byId('mode').value = 'preview'; app.run('modeChanged()');
  assert.equal(app.byId('task-parameters').hidden,true);
});

test('keyboard strings use dropdowns and numeric arrays retain their numeric editor', () => {
  const app = load();
  app.run(`values={flip_key:'space',color:[0.5,1,1]}; renderEditor();`);
  assert.equal(app.byId('param-0').localName,'select');
  assert.ok(app.byId('param-0').children.some(o=>o.value==='return'));
  assert.equal(app.byId('param-1').localName,'textarea');
});

/* ---- Sections: Stimulus, Timing, Session, Other (ParameterSections) ---- */

const realNames = JSON.parse(readFileSync(
  new URL('./fixtures/param_sections_real_names.json', import.meta.url), 'utf8'));

function sections() {
  const context = vm.createContext({console});
  vm.runInContext(readFileSync(new URL('workspace_parameters.js', assets), 'utf8')
    + ';globalThis.PS = ParameterSections;', context);
  return context.PS;
}

test('inference places the four experiments\u2019 real field names (no schema declarations)', () => {
  const PS = sections();
  for (const [repo, names] of Object.entries(realNames)) {
    for (const [dotted, expected] of Object.entries(names)) {
      const got = PS.of({}, dotted.split('.'));
      assert.equal(got.section, expected, `${repo}: ${dotted}`);
      assert.equal(got.how, 'inferred');
    }
  }
});

test('a rate is not a time: _dva_s and _rev_s are the stimulus, _s alone is timing', () => {
  const PS = sections();
  assert.equal(PS.infer(['speed_dva_s']).section, 'stimulus');
  assert.equal(PS.infer(['pursuit_speed_rev_s']).section, 'stimulus');
  assert.equal(PS.infer(['onset_jitter_s']).section, 'timing');
  // A one-word name `s` is no unit.
  assert.equal(PS.infer(['s']).section, 'other');
});

test('a group stays under one heading, judged by its own name', () => {
  const PS = sections();
  // The leaf alone would be timing; the group is the reward, a session matter.
  assert.equal(PS.infer(['reward', 'by_outcome', 'CORRECT', 'pulse_ms']).section, 'session');
  assert.equal(PS.infer(['paradigm', 'blocks', 'validate_after_break']).section, 'session');
});

const declaredSchema = {
  properties: {
    // The field's own declaration.
    hemifields: {type: 'array', 'x-alhazen-section': 'stimulus'},
    // A Duration: its model declares Timing (as alhazen's does), and an
    // optional field wraps it in anyOf.
    iti: {anyOf: [{$ref: '#/$defs/Duration'}, {type: 'null'}]},
    // The field's own declaration beats its model's.
    lapse_rt: {$ref: '#/$defs/Duration', 'x-alhazen-section': 'other'},
    // A nested model declaring for its fields; one field overrides it.
    search: {$ref: '#/$defs/Search'},
    // A dict of models: the model under additionalProperties declares.
    candidates: {type: 'object', additionalProperties: {$ref: '#/$defs/Candidate'}},
    // A name a newer alhazen might write: read as no declaration.
    window_dva: {type: 'number', 'x-alhazen-section': 'response'},
    // Declared wrong by inference's lights: the declaration still wins.
    fix_window_dva: {type: 'number', 'x-alhazen-section': 'session'},
  },
  $defs: {
    Duration: {'x-alhazen-section': 'timing', properties: {ms: {}, frames: {}}},
    Search: {'x-alhazen-section': 'session', properties: {
      n_init: {type: 'integer'},
      speed_dva_s: {type: 'number', 'x-alhazen-section': 'stimulus'},
    }},
    Candidate: {'x-alhazen-section': 'stimulus', properties: {speed: {}, envelope: {}}},
  },
};

test('declarations win: the field\u2019s own, then the nearest enclosing field or model', () => {
  const PS = sections();
  const of = (dotted) => PS.of(declaredSchema, dotted.split('.'));
  assert.deepEqual({...of('hemifields')}, {section: 'stimulus', how: 'declared'});
  assert.deepEqual({...of('iti.ms')}, {section: 'timing', how: 'inherited'});
  assert.deepEqual({...of('lapse_rt.ms')}, {section: 'other', how: 'inherited'});
  assert.deepEqual({...of('search.n_init')}, {section: 'session', how: 'inherited'});
  assert.deepEqual({...of('search.speed_dva_s')}, {section: 'stimulus', how: 'declared'});
  assert.deepEqual({...of('candidates.incumbent.envelope')}, {section: 'stimulus', how: 'inherited'});
  assert.deepEqual({...of('fix_window_dva')}, {section: 'session', how: 'declared'});
  // An unknown name is no declaration: inference decides (a window is Other).
  const unknown = of('window_dva');
  assert.equal(unknown.section, 'other');
  assert.equal(unknown.how, 'inferred');
  // A field the schema does not know (a file with an extra key) is inferred.
  assert.equal(of('extra_size_dva').section, 'stimulus');
});

function editorRows(app) {
  const host = app.byId('parameter-fields');
  return host.children.map((section) => ({
    section: section.dataset.section,
    hidden: !!section.hidden,
    heading: section.children[0].textContent,
    keys: section.children.slice(1).filter((r) => !r.hidden).map((r) => r.dataset.key),
  }));
}

test('the editor groups rows under Stimulus, Timing, Session, Other in that order, each with its count', () => {
  const app = load();
  const values = {
    fix_window_dva: 2, iti: {ms: 500}, eccentricity_dva: 10, subject_kind: 'human',
    paradigm: {kind: 'constant', n_per_condition: 8}, dot_color: [1, 1, 1],
  };
  app.run(`parameterSchema = {}; values = ${JSON.stringify(values)}; renderEditor();`);
  assert.deepEqual(editorRows(app), [
    {section: 'stimulus', hidden: false, heading: 'Stimulus2', keys: ['eccentricity_dva', 'dot_color']},
    {section: 'timing', hidden: false, heading: 'Timing1', keys: ['iti.ms']},
    {section: 'session', hidden: false, heading: 'Session3',
      keys: ['subject_kind', 'paradigm.kind', 'paradigm.n_per_condition']},
    {section: 'other', hidden: false, heading: 'Other1', keys: ['fix_window_dva']},
  ]);
  // Controls keep the file's numbering, wherever their row went: param-0 is
  // the file's first value, now under Other; each label still names it.
  assert.equal(String(app.byId('param-0').value), '2');
  const label = app.byId('parameter-fields').querySelectorAll('label')
    .find((l) => l.htmlFor === 'param-0');
  assert.match(label.textContent, /fix window dva/);
});

test('a heading with no rows is left out, and an all-Other file shows only Other', () => {
  const app = load();
  app.run(`parameterSchema = {}; values = {g: 0.03, gammas: [0.5, 1]}; renderEditor();`);
  assert.deepEqual(editorRows(app).map((s) => s.section), ['other']);
});

test('search filters rows, hides emptied headings and counts what is shown', () => {
  const app = load();
  const values = {eccentricity_dva: 10, dot_size_dva: 0.2, iti: {ms: 500}, n_repeats: 4};
  app.run(`parameterSchema = {}; values = ${JSON.stringify(values)}; renderEditor();`);
  app.byId('parameter-search').value = 'dot';
  app.run('filterParameters()');
  const shown = editorRows(app);
  assert.deepEqual(shown.map((s) => [s.section, s.hidden, s.keys]), [
    ['stimulus', false, ['dot_size_dva']],
    ['timing', true, []],
    ['session', true, []],
  ]);
  assert.equal(shown[0].heading, 'Stimulus1');
  app.byId('parameter-search').value = '';
  app.run('filterParameters()');
  assert.deepEqual(editorRows(app).map((s) => [s.hidden, s.heading]),
    [[false, 'Stimulus2'], [false, 'Timing1'], [false, 'Session1']]);
});

test('declared sections from the task schema place the rows', () => {
  const app = load();
  app.run(`parameterSchema = ${JSON.stringify(declaredSchema)};
    values = {fix_window_dva: 2, hemifields: ['left'], search: {n_init: 4}}; renderEditor();`);
  assert.deepEqual(editorRows(app).map((s) => [s.section, s.keys]), [
    ['stimulus', ['hemifields']],
    ['session', ['fix_window_dva', 'search.n_init']],
  ]);
});
