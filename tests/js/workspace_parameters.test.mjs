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
  const sandbox = {document, URLSearchParams, location:{hash:''}, sessionStorage:{getItem:()=>null}, window:{addEventListener(){}}, console};
  const context = vm.createContext(sandbox);
  vm.runInContext(readFileSync(new URL('workspace_parameters.js', assets),'utf8'),context);
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
  assert.equal(app.byId('params-preset-field').hidden,true);
  assert.equal(app.byId('launch').disabled,false);
  assert.equal(app.run('usesParameters()'),false);
  app.byId('mode').value = 'movie'; app.run('modeChanged()');
  assert.equal(app.byId('task-parameters').hidden,false);
  assert.equal(app.byId('task-parameters').disabled,false);
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
