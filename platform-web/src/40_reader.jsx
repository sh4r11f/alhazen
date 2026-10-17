// Host adapter only. Drawing, timeline geometry, parameter references and reading layout
// come from the original Alhazen HubDocs renderer pinned in the owning project notes.
function ehDocBundle(record) {
  // Which bundle a docs record describes: the task guide's bundleId, else the presentation's.
  for (const field of ['guide', 'presentation']) {
    try { const v = ehDecode(record && record[field], null, field); if (v && v.bundleId) return v.bundleId; }
    catch (e) { console.warn('Unreadable ' + field + ' in a docs record:', e.message); }
  }
  return '';
}
function ehDocCommit(record) {
  // The full source commit a docs record was written for (parameter guide, else task guide).
  try { const g = ehDecode(record && record.parameterGuide, null, 'Parameter sets'); if (g && g.sourceCommit) return g.sourceCommit; }
  catch (e) { console.warn('Unreadable parameter guide:', e.message); }
  try { const g = ehDecode(record && record.guide, null, 'Task guide'); if (g && g.source && g.source.commit) return g.source.commit; }
  catch (e) { console.warn('Unreadable task guide:', e.message); }
  return '';
}
function ehGuideDocument(record, listing) {
  const doc = ehDecode(record?.guide, null, 'Task guide');
  if (!doc) return null;
  if (doc.bundleId && doc.bundleId !== listing.bundle?.id) return null;
  if (!Array.isArray(doc.tasks)) throw new Error('The task-guide document has no task list.');
  return doc;
}
function ehGuideTask(doc, selected, fallbackTasks) {
  return (doc?.tasks || []).find(t => t.taskKey === selected || t.id === selected) ||
    (doc?.tasks || []).find(t => t.sourceTaskId === selected) || doc?.tasks?.[0] || null;
}
function ehTaskHref(listingKey, taskKey) {
  const u = new URL(window.location.href);
  u.searchParams.set('ehv','exp');u.searchParams.set('ehid',listingKey);
  u.searchParams.set('ehtab','tasks');u.searchParams.set('ehtask',taskKey);
  u.searchParams.delete('ehset');
  return u.pathname + u.search;
}
function EHMethodThumbnail({ diagram, title, className = '' }) {
  const ref = React.useRef(null);
  const [error,setError] = React.useState('');
  const value = React.useMemo(() => {
    try { return {diagram:ehDecode(diagram,null,'Stimulus diagram')}; }
    catch (e) { return {error:e.message}; }
  },[diagram]);
  React.useEffect(() => {
    const root=ref.current;if(!root)return;
    root.replaceChildren();setError('');
    try {
      if(value.error)throw new Error(value.error);
      const d=value.diagram;
      if(!d || !Array.isArray(d.elements) || !(d.width>0) || !(d.height>0))throw new Error('No stimulus diagram is documented.');
      root.appendChild(HubDocs.diagramSvg({title:title || 'Stimulus',diagram:d},{document:window.document,idPrefix:'ehthumb-'}));
    } catch (e) { setError(e.message || 'The stimulus diagram could not be drawn.'); }
    return () => root.replaceChildren();
  },[value,title]);
  return <div className={'eh-method-thumb w-full '+className}><div ref={ref} className="hd-doc eh-thumb-canvas" aria-hidden="true" />{error ? <span className="eh-thumb-error">Diagram unavailable</span> : null}</div>;
}
function EHOriginalDocumentation({ document:guide, kind='task', taskId, listingKey, onTask, parameterSets=[], onSet }) {
  const root=React.useRef(null);
  const callbacks=React.useRef({onTask,onSet});callbacks.current={onTask,onSet};
  const prefix=React.useId().replace(/[^a-z0-9]/gi,'').slice(-12);
  const parameterSetKey=JSON.stringify(parameterSets.map(s=>[s.id,s.name]));
  const [error,setError]=React.useState('');
  const [exportStatus,setExportStatus]=React.useState('');
  React.useEffect(() => {
    const host=root.current;if(!host)return;
    host.replaceChildren();setError('');setExportStatus('');
    try {
      const options={document:window.document,headingLevel:2,idPrefix:'ehg-'+prefix+'-',taskHref:id => {
        const t=guide?.tasks?.find(x=>x.id===id);
        return ehTaskHref(listingKey,t?.taskKey || id);
      }};
      const content=kind==='methods' ? HubDocs.renderMethods(guide,options) : HubDocs.renderTaskGuide(guide,taskId,options);
      // Replication qualifications stay in the prose, not as paragraphs embedded in SVG.
      const task=guide?.tasks?.find(t=>t.id===taskId);
      if(kind==='task' && task?.guideNotes?.length){
        const box=window.document.createElement('details');box.className='eh-guide-qualifications';
        const summary=window.document.createElement('summary');summary.textContent='Diagram assumptions and qualifications';box.appendChild(summary);
        const list=window.document.createElement('ul');for(const note of task.guideNotes){const item=window.document.createElement('li');item.textContent=note;list.appendChild(item);}box.appendChild(list);content.appendChild(box);
      }
      if(kind==='task' && task){
        if(parameterSets.length){
          const section=content.querySelector('[data-hd-section="parameters"]');
          if(section){const box=window.document.createElement('div');box.className='eh-guide-sets';
            const label=window.document.createElement('span');label.textContent='Named parameter sets for this task';box.appendChild(label);
            const row=window.document.createElement('div');for(const set of parameterSets){const button=window.document.createElement('button');button.type='button';button.className='eh-inline-link';button.textContent=set.name;button.addEventListener('click',()=>callbacks.current.onSet?.(set.id));row.appendChild(button);}box.appendChild(row);section.insertBefore(box,section.children[1] || null);
          }
        }
        for(const [type,sectionName] of [['diagram','stimulus'],['timeline','timeline']]){
          const figure=content.querySelector('[data-hd-section="'+sectionName+'"] figure');if(!figure)continue;
          const tools=window.document.createElement('div');tools.className='eh-diagram-tools';
          for(const [copy,label] of [[false,'Download SVG'],[true,'Copy SVG']]){const button=window.document.createElement('button');button.type='button';button.className='eh-inline-link';button.textContent=label;button.addEventListener('click',()=>exportFigure(type,copy));tools.appendChild(button);}figure.appendChild(tools);
        }
      }
      host.appendChild(content);
      const click=e=>{
        const anchor=e.target.closest?.('a.hd-task-link');
        if(!anchor || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button!==0)return;
        const u=new URL(anchor.href,window.location.href);
        const key=u.searchParams.get('ehtask');if(!key)return;
        e.preventDefault();e.stopPropagation();callbacks.current.onTask?.(key);
      };
      const fitFigures=()=>{
        for(const canvas of host.querySelectorAll('.hd-figure-canvas--wide')){
          const svg=canvas.querySelector('svg');if(!svg)continue;
          // Preserve readable scientific labels instead of shrinking a long trial to 6px text.
          if(svg.classList.contains('hd-timeline-svg'))svg.style.minWidth=Math.round(svg.viewBox.baseVal.width*0.9)+'px';
          const note=canvas.parentElement.querySelector('.hd-scroll-note');
          if(note)note.hidden=canvas.scrollWidth<=canvas.clientWidth+1;
        }
      };
      fitFigures();const observer=new ResizeObserver(fitFigures);observer.observe(host);
      host.addEventListener('click',click);
      return ()=>{observer.disconnect();host.removeEventListener('click',click);host.replaceChildren();};
    } catch(e) { setError(e.message || 'The Alhazen task guide could not be displayed.'); }
  },[guide,kind,taskId,listingKey,prefix,parameterSetKey]);
  const exportFigure=async(type,copy=false)=>{
    let holder=null,blobUrl=null;
    try {
      setExportStatus('');
      const task=guide?.tasks?.find(t=>t.id===taskId);
      if(!task || !task[type])throw new Error('This task has no '+type+'.');
      holder=window.document.createElement('div');holder.className='hd-doc eh-export-scope';
      Object.assign(holder.style,{position:'absolute',left:'-10000px',top:'0',width:'1600px',pointerEvents:'none'});
      const light={'--hd-ink':LIGHT.ink,'--hd-muted':LIGHT.muted,'--hd-faint':LIGHT.muted,'--hd-paper':LIGHT.surface,'--hd-soft':LIGHT['surface-2'],'--hd-rule':LIGHT.line,'--hd-accent':LIGHT['accent-ink'],'--hd-accent-soft':LIGHT['accent-soft'],'--hd-fail':LIGHT['bad-ink'],'--hd-abort':LIGHT['warn-ink'],'--hd-ok':LIGHT['ok-ink']};
      for(const [k,v] of Object.entries(light))holder.style.setProperty(k,v);
      root.current.appendChild(holder);
      const svg=type==='diagram'?HubDocs.diagramSvg(task,{document:window.document,idPrefix:'eh-export-'}):HubDocs.timelineSvg(task,{document:window.document,idPrefix:'eh-export-'});
      holder.appendChild(svg);
      const clone=svg.cloneNode(true);
      const originals=[svg,...svg.querySelectorAll('*')], copies=[clone,...clone.querySelectorAll('*')];
      const props=['fill','stroke','stroke-width','stroke-dasharray','stroke-linecap','stroke-linejoin','opacity','fill-opacity','stroke-opacity','font-family','font-size','font-weight','font-style','text-anchor','dominant-baseline','marker-start','marker-end'];
      originals.forEach((node,i)=>{const computed=window.getComputedStyle(node);for(const p of props){let value=computed.getPropertyValue(p);if(!value)continue;value=value.replace(/url\(["']?[^#)]*#([^"')]+)["']?\)/g,'url(#$1)');copies[i].style.setProperty(p,value);}});
      const vb=svg.viewBox.baseVal,width=type==='diagram'?89:183;
      clone.setAttribute('xmlns','http://www.w3.org/2000/svg');clone.setAttribute('width',width+'mm');clone.setAttribute('height',(width*vb.height/vb.width).toFixed(2)+'mm');clone.style.width='';clone.style.height='';clone.style.maxWidth='';
      const bg=window.document.createElementNS('http://www.w3.org/2000/svg','rect');bg.setAttribute('width',vb.width);bg.setAttribute('height',vb.height);bg.setAttribute('fill','#fff');clone.insertBefore(bg,clone.firstChild);
      const xml=new window.XMLSerializer().serializeToString(clone);
      if(copy){const outcome=await copyText(xml);setExportStatus(outcome==='failed'?'Could not copy. Use Download SVG instead.':'Editable SVG copied.');return;}
      blobUrl=URL.createObjectURL(new Blob([xml],{type:'image/svg+xml'}));
      const link=window.document.createElement('a');link.href=blobUrl;link.download=task.id+'-'+type+'.svg';link.click();setExportStatus('SVG exported.');
    } catch(e){setExportStatus(e.message || 'Could not export this diagram.');}
    finally {if(holder)holder.remove();if(blobUrl)setTimeout(()=>URL.revokeObjectURL(blobUrl),1000);}
  };
  const chosen=guide?.tasks?.find(t=>t.id===taskId);
  return <div className="eh-original-doc w-full">

    {exportStatus ? <p className="eh-source-note" role="status">{exportStatus}</p> : null}
    {error ? <div className="w-full eh-doc-error" role="alert">{error}</div> : null}
    <div ref={root} />
  </div>;
}
function EHRestoredTaskGuide({ guide, rawTasks, parameterGuide, selectedId, listingKey, onTask, onSet, onFile }) {
  const task=ehGuideTask(guide,selectedId,rawTasks);
  if(!task)return <div className="w-full"><p className="eh-muted">This version does not yet include an Alhazen task-guide descriptor. The source methods and configuration values remain available.</p>{rawTasks.map(t=><div className="eh-guide-fallback" key={ehTaskKey(t)}><h3>{t.title}</h3><p>{t.summary}</p>{t.parameters_file ? <button className="eh-inline-link" onClick={()=>onFile(t.parameters_file)}>Read configuration</button> : null}</div>)}</div>;
  const sets=(parameterGuide?.sets || []).filter(s=>(s.taskIds || []).includes(task.sourceTaskId || task.id));
  return <div className="eh-restored-task w-full">
    {guide.tasks.length>1 ? <nav className="eh-original-task-index" aria-label="Tasks">{guide.tasks.map(t=><button key={t.id} aria-current={t.id===task.id?'page':undefined} onClick={()=>onTask(t.taskKey || t.id)}>{t.title}</button>)}</nav> : null}
    <EHOriginalDocumentation document={guide} taskId={task.id} listingKey={listingKey} onTask={onTask} parameterSets={sets} onSet={onSet}/>
  </div>;
}

function ehDecode(value, fallback, label) {
  if (value === null || value === undefined || value === '') return fallback;
  if (typeof value !== 'string') return value;
  try { return JSON.parse(value); }
  catch (error) { throw new Error((label || 'Documentation') + ' is not valid JSON: ' + error.message); }
}
function ehFormat(value, unit) {
  if (value === undefined || value === null) return 'Not specified';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'object' && !Array.isArray(value)) {
    const keys = Object.keys(value);
    if (keys.length === 1 && keys[0] === 'ms') return ehFormat(value.ms) + ' ms';
    if (keys.length === 1 && keys[0] === 'seconds') return ehFormat(value.seconds) + ' s';
    return JSON.stringify(value, null, 2);
  }
  if (Array.isArray(value)) return value.map(x => typeof x === 'object' ? JSON.stringify(x) : String(x)).join(', ') + (unit ? ' ' + unit : '');
  return String(value) + (unit ? ' ' + unit : '');
}
function ehTaskKey(task) { return task.id + (task.parameters_file ? '@' + task.parameters_file : ''); }
function ehSameTask(a, b) {
  const ai = a.taskIds || [], bi = b.taskIds || [];
  if (ai.length && bi.length) return ai.some(x => bi.includes(x));
  return !!(a.taskImplementation && b.taskImplementation && ehStable(a.taskImplementation) === ehStable(b.taskImplementation));
}
function ehStable(value) {
  if (Array.isArray(value)) return '[' + value.map(ehStable).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + ehStable(value[k])).join(',') + '}';
  return JSON.stringify(value);
}
function ehParameterLabel(p) {
  if (p.label) return p.label;
  const key = p.key || p.name || '';
  const labels = {n_pulses:'Pulse count',pulse_ms:'Pulse duration',inter_pulse_ms:'Interval between pulses',subject_kind:'Subject type'};
  const leaf = key.split('.').pop();
  const label = labels[leaf] || leaf.replace(/_/g,' ').replace(/^./,x=>x.toUpperCase());
  if (key.startsWith('reward.on_fault.')) return 'On device fault · ' + label.toLowerCase();
  if (key.startsWith('reward.by_outcome.')) return 'On ' + key.split('.')[2] + ' · ' + label.toLowerCase();
  return label;
}
function ehGroup(p) {
  const label = (p.group || 'Configuration').replace(/\s*\(§[^)]*\)/g,'').trim();
  const standard = {stimuli:'Stimuli',stimulus:'Stimulus',timing:'Timing',design:'Design',response:'Response','subject and reward':'Subject & reward','subject & reward':'Subject & reward',fixation:'Fixation',paradigm:'Paradigm'};
  return standard[label.toLowerCase()] || label.replace(/^./,x=>x.toUpperCase());
}
function ehSetValues(set) {
  return [...(set?.values || []), ...(set?.runValues || []).map(p => ({...p,group:p.group || 'Subject & reward'}))];
}
function ehDifferences(chosen, baseline) {
  if (!chosen || !baseline || !ehSameTask(chosen, baseline)) return [];
  const a = new Map(ehSetValues(chosen).map(p => [p.key, p]));
  const b = new Map(ehSetValues(baseline).map(p => [p.key, p]));
  return [...new Set([...a.keys(), ...b.keys()])].filter(key => ehStable(a.get(key)?.value) !== ehStable(b.get(key)?.value)).map(key => ({ key, label:ehParameterLabel(a.get(key) || b.get(key) || {key}), value: a.get(key)?.value, baseValue: b.get(key)?.value, display:a.get(key)?.value === undefined && key.startsWith('reward.') && chosen.subject === 'human' ? 'Not used for humans' : null, baseDisplay:b.get(key)?.value === undefined && key.startsWith('reward.') && baseline.subject === 'human' ? 'Not used for humans' : null, unit:a.get(key)?.unit || b.get(key)?.unit, assumed:a.get(key)?.assumed, meaning: a.get(key)?.meaning || a.get(key)?.comment || b.get(key)?.meaning || b.get(key)?.comment || '' }));
}
function ehMethodsData(d) {
  if (d?.__loadError) throw new Error(d.__loadError);
  return { tasks: ehDecode(d?.tasks, [], 'Tasks'), presentation: ehDecode(d?.presentation, null, 'Method figures'), parameterGuide: ehDecode(d?.parameterGuide, null, 'Parameter sets') };
}
function ehFigureFor(presentation, task) {
  const figures = presentation?.figures || [];
  if (task) return figures.find(f => (f.taskIds || []).includes(task.id) && (f.configPaths || []).includes(task.parameters_file)) || figures.find(f => (f.taskIds || []).includes(task.id)) || null;
  return figures.find(f => f.kind === 'overview') || figures[0] || null;
}
function ehSourceMatches(presentation, listing) {
  if (presentation?.bundleId && listing?.bundle?.id) return presentation.bundleId === listing.bundle.id;
  const source = presentation?.source;
  if (!source || !listing?.source) return !source;
  return !source.commit || source.commit === listing.source.commit || source.commit === listing.source.repoCommit;
}
class EHDocumentationBoundary extends React.Component {
  constructor(props) { super(props); this.state = { error: null }; }
  static getDerivedStateFromError(error) { return { error }; }
  render() { return this.state.error ? <div className="w-full eh-doc-error" role="alert"><h3>Documentation could not be displayed</h3><p>{this.state.error.message}</p><p>The source files and your copies are unchanged.</p></div> : this.props.children; }
}
function EHFigure({ figure, art, compact = false, foldCaption = false }) {
  const [large, setLarge] = React.useState(false);
  const [failure, setFailure] = React.useState('');
  const [copyState, setCopyState] = React.useState('');
  const svgPath = figure?.webPath || figure?.formats?.svg || figure?.svgPath;
  const src = art.url && svgPath ? art.url + art.encodePath(svgPath) : '';
  React.useEffect(() => { setFailure(''); setCopyState(''); setLarge(false); }, [src]);
  if (!figure) return null;
  const copySvg = async () => {
    setCopyState('Copying…');
    try {
      const response = await fetch(src);
      if (!response.ok) throw new Error('The SVG could not be read (' + response.status + ').');
      const text = await response.text();
      if (!text.includes('<svg')) throw new Error('The file returned was not an SVG.');
      const result = await copyText(text);
      setCopyState(result === 'failed' ? 'Copy failed. Use Download SVG instead.' : 'SVG copied');
    } catch (error) { setCopyState(error.message || 'Could not copy the SVG.'); }
  };
  return <figure className={'eh-method-figure' + (compact ? ' is-compact' : '')}>
    <div className="eh-figure-tools"><span>{figure.title || 'Stimuli and procedure'}</span><div>
      <button className="btn btn-quiet btn-small" aria-pressed={large} onClick={() => setLarge(x => !x)}>{large ? 'Default size' : 'Enlarge'}</button>
      <details className="eh-export"><summary>Export</summary><div className="eh-export-menu">
        {['svg', 'pdf', 'png'].map(format => { const p = figure.formats?.[format]; return p && art.url ? <a key={format} href={art.url + art.encodePath(p)} download={p.split('/').pop()} target="_blank" rel="noreferrer">Download {format.toUpperCase()}</a> : null; })}
        {src ? <button onClick={copySvg}>Copy editable SVG</button> : null}<button onClick={async () => { const result = await copyText(figure.caption || ''); setCopyState(result === 'failed' ? 'Could not copy the caption.' : 'Caption copied'); }}>Copy caption</button>
        <small>Vector SVG and PDF; PNG at 600 dpi. Figure width: 183 mm.</small>
        {copyState ? <p role="status">{copyState}</p> : null}
      </div></details>
    </div></div>
    <div className="eh-figure-swipe">Scroll the drawing horizontally →</div>
    {art.error ? <p className="eh-doc-error" role="alert">Figure storage: {art.error.message || String(art.error)}</p> : failure ? <p className="eh-doc-error" role="alert">{failure} The caption remains available below.</p> : !src ? <p className="eh-muted">Loading figure…</p> : <div className="eh-figure-scroll" tabIndex={0} role="group" aria-label="Scientific figure. Scroll horizontally on a narrow screen.">
      <img src={src} className={'eh-method-graphic' + (large ? ' full-size' : '')} alt={figure.alt || figure.title} onError={() => setFailure('The figure could not be loaded.')} loading={compact ? 'lazy' : 'eager'} />
    </div>}
    <figcaption>{foldCaption ? <details className="eh-caption-details"><summary>Figure notes and assumptions</summary><p>{figure.caption}</p></details> : figure.caption}</figcaption>
  </figure>;
}
function EHValuesTable({ values, differences = false, baselineName = '' }) {
  if (!values?.length) return <p className="eh-muted">No values are documented for this selection.</p>;
  return <div className="eh-table-wrap"><table className={'eh-param-table' + (differences ? ' is-diff' : '')}>
    <thead><tr><th>Parameter</th>{differences ? <th>{baselineName || 'Compared with'}</th> : null}<th>{differences ? 'Selected set' : 'Value'}</th><th>Meaning</th></tr></thead>
    <tbody>{values.map((p, i) => <tr key={(p.key || p.name) + '-' + i}>
      <th scope="row">{ehParameterLabel(p)}<code>{p.key || p.name}</code>{p.assumed ? <span className="eh-assumed">Assumed</span> : null}</th>
      {differences ? <td data-label="Compared with"><pre>{p.baseDisplay || ehFormat(p.baseValue, p.unit)}</pre></td> : null}
      <td data-label={differences ? 'Selected set' : 'Value'}><pre>{differences && p.display ? p.display : ehFormat('value' in p ? p.value : p.default, p.unit)}</pre></td>
      <td className="eh-param-meaning">{p.meaning || p.comment || 'Meaning not documented in this source version.'}</td>
    </tr>)}</tbody>
  </table></div>;
}
function EHDefinition() {
  return <div className="eh-task-definition"><p><strong>Task</strong> The procedure: what appears, what the subject does, and what is measured.</p><p><strong>Parameter set</strong> A named configuration of that procedure: stimulus values, timings, conditions and subject type. Several sets can run the same task.</p></div>;
}

function EHConfigurationReader({ listing, guide, tasks, selectedId, onSelect, onTask, onFile }) {
  const [baselineId, setBaselineId] = React.useState('');
  const [query, setQuery] = React.useState('');
  const sets = guide?.sets || [];
  const selected = sets.find(s => s.id === selectedId || s.configPath === selectedId) || sets[0];
  const peers = selected ? sets.filter(s => s.id !== selected.id && ehSameTask(s, selected)) : [];
  const baseline = peers.find(s => s.id === baselineId) || peers[0] || null;
  const diffs = ehDifferences(selected, baseline);
  React.useEffect(() => { setBaselineId(''); setQuery(''); }, [selected?.id]);
  if (!guide) return <div className="w-full"><h2>Parameter sets</h2><p>These are named run configurations, not additional task procedures. Detailed configuration values have not been indexed for this version.</p><ul>{asList(listing.parameterSets).map(name => <li key={name}>{name}</li>)}</ul>{tasks.filter(t => t.parameters_file).map(t => <p key={ehTaskKey(t)}><button className="btn btn-line btn-small" onClick={() => onFile(t.parameters_file)}>Read {t.parameters_file}</button></p>)}</div>;
  if (!selected) return <div className="w-full"><h2>Parameter sets</h2><p>This source version registers no parameter sets.</p></div>;
  const matchingTasks = tasks.filter(t => (selected.taskIds || []).includes(t.id));
  const exactTask = matchingTasks.find(t => t.parameters_file === selected.configPath) || matchingTasks[0];
  const values = ehSetValues(selected);
  const visible = query.trim() ? values.filter(p => [p.label, p.key, p.meaning, ehFormat(p.value)].join(' ').toLowerCase().includes(query.trim().toLowerCase())) : values;
  const groups = {};
  for (const p of visible) (groups[ehGroup(p)] ||= []).push(p);
  const firstOpenGroup = Object.keys(groups).find(name => name === 'Timing') || Object.keys(groups)[0];
  return <section className="w-full eh-config-reader">
    <div className="eh-doc-select"><label htmlFor="eh-parameter-set">Parameter set</label><select id="eh-parameter-set" value={selected.id} onChange={e => onSelect(e.target.value)}>{sets.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select></div>
    <header className="eh-reader-head"><h2>{selected.name}</h2>{selected.purpose || exactTask?.summary ? <p>{selected.purpose || exactTask.summary}</p> : null}</header>
    <dl className="eh-method-facts">
      <div><dt>Runs task</dt><dd>{exactTask ? <button className="eh-inline-link" onClick={() => onTask(exactTask)}>{exactTask.title}</button> : (selected.taskIds || []).join(', ') || 'Task mapping not documented'}</dd></div>
      <div><dt>Subjects</dt><dd>{selected.subject || 'Not specified'}</dd></div>
      <div><dt>Configuration</dt><dd>{selected.configPath ? <button className="eh-inline-link mono" onClick={() => onFile(selected.configPath)}>{selected.configPath}</button> : <><span>Model defaults</span>{selected.taskImplementation?.file ? <> · <button className="eh-inline-link" onClick={() => onFile(selected.taskImplementation.file)}>Source definition</button></> : null}</>}</dd></div>
    </dl>
    {selected.response ? <p className="eh-config-response"><strong>Response:</strong> {selected.response}</p> : null}
    {baseline ? <section className="eh-doc-section"><div className="eh-compare-head"><h3>What changes</h3><label>Compared with <select value={baseline.id} onChange={e => setBaselineId(e.target.value)}>{peers.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label></div>
      {diffs.length ? <EHValuesTable values={diffs} differences baselineName={baseline.name} /> : <p className="eh-muted">The documented values are identical. The named configuration and source path remain distinct.</p>}
      <p className="eh-source-note">Only differences between configurations of the same task are shown. Changing tasks changes the procedure, not just its values.</p>
    </section> : <p className="eh-source-note">This is the only indexed parameter set for this task. Sets for other tasks are not treated as interchangeable.</p>}
    <section className="eh-doc-section"><div className="eh-compare-head"><h3>All configuration values <span>{values.length}</span></h3><input className="eh-search" value={query} onChange={e => setQuery(e.target.value)} placeholder="Find a parameter" aria-label="Find a parameter" /></div>
      {Object.entries(groups).map(([name, ps]) => <details key={selected.id + ':' + name + ':' + !!query} className="eh-parameter-group" open={query.trim() || name === firstOpenGroup ? true : undefined}><summary><span>{name}</span><span>{ps.length} values</span></summary><EHValuesTable values={ps} /></details>)}
      {!visible.length ? <p className="eh-muted">No parameters match “{query}”.</p> : null}
    </section>
    {guide.warnings?.length ? <details className="eh-parameter-group"><summary>Source qualifications <span>{guide.warnings.length}</span></summary><ul className="eh-source-warnings">{guide.warnings.map((w,i) => <li key={i}>{typeof w === 'string' ? w : JSON.stringify(w)}</li>)}</ul></details> : null}
    <p className="eh-source-note">Values read from the source configuration and its model defaults, not estimated from the set name. <span className="mono">{(guide.sourceCommit || '').slice(0, 7)}</span></p>
  </section>;
}
function EHTaskReader({ tasks, guide, presentation, selectedId, art, onSelect, onSet, onFile }) {
  const task = tasks.find(t => ehTaskKey(t) === selectedId) || tasks.find(t => t.id === selectedId) || tasks[0];
  if (!task) return <div className="w-full"><p>No task documentation is included in this version.</p></div>;
  const sets = (guide?.sets || []).filter(s => (s.taskIds || []).includes(task.id));
  const groups = {};
  for (const p of task.parameters || []) (groups[p.group || 'Parameters'] ||= []).push(p);
  const fig = ehFigureFor(presentation, task);
  return <section className="w-full eh-task-reader">
    {tasks.length > 1 ? <div className="eh-doc-select"><label htmlFor="eh-task-procedure">Task procedure</label><select id="eh-task-procedure" value={ehTaskKey(task)} onChange={e => onSelect(e.target.value)}>{tasks.map(t => <option key={ehTaskKey(t)} value={ehTaskKey(t)}>{t.title}</option>)}</select></div> : null}
    <header className="eh-reader-head"><h2>{task.title}</h2><p>{task.summary}</p></header>
    {fig ? <EHFigure key={fig.id} figure={fig} art={art} /> : task.timeline?.phases?.length ? <section className="eh-doc-section"><h3>Trial procedure</h3><ol className="eh-phase-list">{task.timeline.phases.map((phase,i) => <li key={phase.id || i}><strong>{phase.label || phase.id}</strong><span>{phase.timing?.text || ehFormat(phase.duration || phase.timing || 'Duration is determined by the task')}{phase.note ? '. ' + phase.note : ''}</span></li>)}</ol></section> : null}
    <section className="eh-doc-section"><h3>Parameter sets for this task</h3><p className="eh-muted">Choose a configuration to see its values and how it differs from the others.</p>
      {sets.length ? <div className="eh-set-links">{sets.map(s => <button className="eh-set-link" key={s.id} onClick={() => onSet(s.id)}><span>{s.name}</span><small>{s.subject || ''}{s.subject ? ' · ' : ''}{s.configPath || 'Model defaults'}</small><span aria-hidden="true">↗</span></button>)}</div> : <p className="eh-muted">No parameter-set mapping is documented for this task.{task.parameters_file ? <> <button className="eh-inline-link" onClick={() => onFile(task.parameters_file)}>Read {task.parameters_file}</button>.</> : null}</p>}
    </section>
    {Object.keys(groups).length ? <details className="eh-parameter-group"><summary>Parameter definitions <span>{task.parameters.length}</span></summary><p className="eh-source-note">These are the documented defaults for {task.parameters_file || 'this task'}. Use a parameter set above for its actual configuration values.</p>{Object.entries(groups).map(([group,ps]) => <section key={group}><h4 className="eh-group-heading">{group}</h4><EHValuesTable values={ps} /></section>)}</details> : null}
    {(task.outcomes || []).length ? <details className="eh-parameter-group"><summary>Scoring and outcomes <span>{task.outcomes.length}</span></summary><dl className="eh-event-list">{task.outcomes.map(o => <div key={o.name}><dt>{o.name}</dt><dd>{o.meaning}<small>{o.success === true ? 'Successful trial' : o.completed ? 'Counted trial' : 'Not counted; served again'}</small></dd></div>)}</dl></details> : null}
    {(task.events || []).length ? <details className="eh-parameter-group"><summary>Recorded events <span>{task.events.length}</span></summary><dl className="eh-event-list">{task.events.map(o => <div key={o.name}><dt>{o.name}</dt><dd>{o.meaning}</dd></div>)}</dl></details> : null}
  </section>;
}


function EHExperimentPage({ L, d, route, art, actions, renderTabs, renderFiles, renderReplication, go, onFile, nav, crumb, sheetHead, extraTabs, renderExtra, overviewExtra, scopeNote, versionsView }) {
  // [2026-10-16] Also renders a private copy (My experiments): nav/crumb/sheetHead/extraTabs/renderExtra/overviewExtra/scopeNote/versionsView.
  const extraKeys=(extraTabs || []).map(([k])=>k);
  const { tasks, presentation:candidate, parameterGuide:guideCandidate }=ehMethodsData(d);
  const presentation=ehSourceMatches(candidate,L)?candidate:null;
  const guide=React.useMemo(()=>ehGuideDocument(d,L),[d?.guide,L.bundle?.id]);
  const paramsFresh=!guideCandidate?.sourceCommit || [L.source?.commit,L.source?.repoCommit].includes(guideCandidate.sourceCommit);
  const parameterGuide=paramsFresh?guideCandidate:null;
  const tab=route.tab || 'overview';
  const tabKey=tab==='sets'?'tasks':(tab==='files'&&!extraKeys.includes('files'))||tab==='details'?'versions':tab;
  const src=L.source || {};
  const firstSet=parameterGuide?.sets?.[0];
  const defaultTask=(guide?.tasks || []).find(t=>(firstSet?.taskIds || []).includes(t.sourceTaskId || t.id) && (!firstSet?.configPath || t.parameters_file===firstSet.configPath)) || guide?.tasks?.[0];
  const bannerTask=defaultTask?.diagram?defaultTask:guide?.tasks?.find(t=>t.diagram);
  const navigate=nav || ((next,extra={},replace=false)=>go({v:'exp',id:L.key,tab:next==='overview'?'':next,...extra},replace));
  const selectTask=t=>navigate('tasks',{task:typeof t==='string'?t:(t.taskKey || ehTaskKey(t))});
  const selectSet=set=>navigate('sets',{set});
  const releaseRows=Object.entries(L.versions || {}).map(([id,v])=>({id,...v})).sort((a,b)=>(b.at || 0)-(a.at || 0));
  const taskCards=guide?.tasks || tasks;
  return <article className="eh-exp-document eh-approved-experience">
    {crumb || <div className="eh-crumb"><button onClick={()=>go({v:'browse'})}>Browse</button><span>/</span><span>{L.title}</span></div>}
    <div className="eh-store-layout">
      <header className="eh-store-hero">
        <div className="eh-guide-banner"><EHExperimentLogo item={L} wide/></div>
        <div className="eh-exphead"><h1>{L.title}</h1><p className="eh-byline">by <b>{L.publisher?.name || 'Unknown'}</b>{L.original?.citation ? <> · After {origShort(L)}</> : null}</p>
          <p className="eh-reference-lede">{L.methodSummary || presentation?.summary || L.summary}</p>
          <div className="eh-reference-tags">{L.status ? <span className={'tag-status st-'+L.status}>{(STATUS[L.status] || [L.status])[0]}</span> : null}{asList(L.tags).filter(t=>CATEGORIES.some(([id])=>id===t)).slice(0,4).map(t=><span key={t}>{CATEGORIES.find(([id])=>id===t)[1]}</span>)}</div>
        </div>
      </header>
      <aside className="eh-store-sheet" aria-label="Experiment actions and requirements">
        {sheetHead || <div className="eh-store-sheet-head"><span>Version <strong>{L.version?'v'+L.version:'Unversioned'}</strong></span><small>{ago(L.updatedAt)}</small></div>}
        <div className="eh-exp-actions">{actions}</div>
        <dl className="eh-store-spec"><div><dt>Needs</dt><dd>{hwKeys(L).map(k=><span className="need" key={k}><Glyph k={k}/>{(HW.find(x=>x[0]===k) || [k,k])[1]}</span>)}</dd></div><div><dt>Subjects</dt><dd>{asList(L.subjects).map(s=>s==='human'?'Human':'Monkey').join(', ')}</dd></div><div><dt>Alhazen</dt><dd>{L.alhazen || 'Not specified'}</dd></div><div><dt>Licence</dt><dd>{L.license || 'Not specified'}</dd></div></dl>
        <div className="eh-sheet-links"><button className="eh-inline-link" onClick={()=>navigate('files')}>Browse source files</button><button className="eh-inline-link" onClick={()=>navigate('details')}>Replication and source notes</button></div>
      </aside>
      {renderTabs([['overview','Overview'],['methods','Methods'],['tasks','Tasks & parameters'],['versions','Versions'],...(extraTabs || [])],tabKey,key=>navigate(key,{},true))}
      <div className="eh-store-reading">
        {scopeNote && !extraKeys.includes(tab) && tab!=='files' && tab!=='versions' ? scopeNote : null}
        {!d && tab!=='files' && !extraKeys.includes(tab) ? <p className="eh-muted">Loading methods…</p> : null}
        {d && !paramsFresh ? <p className="eh-doc-error" role="status">The source version changed. Read its current configuration files; older parameter annotations are not shown as current.</p> : null}
        {d && tab==='overview' ? <div className="eh-overview eh-reference-overview">
          <h2>Methods</h2><p className="eh-method-intro">{presentation?.method || L.summary || tasks[0]?.summary}</p>
          <button className="eh-inline-link" onClick={()=>navigate('methods')}>Read the full Methods</button>
          <section className="eh-reference-section"><h2>Tasks</h2><div className="eh-reference-task-cards">{taskCards.map(t=><button key={t.taskKey || ehTaskKey(t)} className="eh-reference-task-card" onClick={()=>selectTask(t)}><h3>{t.title}</h3><p>{t.summary}</p><small>{t.parameters?.length || 0} documented parameters</small></button>)}</div></section>
          {L.original?.citation ? <section className="eh-reference-section eh-overview-citation"><h2>Original study</h2><p>{L.original.citation} {L.original.url ? <a href={L.original.url} target="_blank" rel="noreferrer">Open paper</a> : null}</p></section> : null}
          {(L.status==='adaptation'||L.status==='demo'||L.counts?.assumed||L.counts?.deviations) ? <p className="eh-scope-note">{L.status==='adaptation'?'This implementation adapts the original experiment. ':L.status==='demo'?'Task-structure demo, not a participant replication. ':''}<button className="eh-inline-link" onClick={()=>navigate('details')}>Assumptions and deviations</button></p> : null}
        </div> : null}
        {tab==='overview' && overviewExtra ? overviewExtra : null}
        {d && tab==='methods' ? <div className="eh-original-methods">{guide ? <EHOriginalDocumentation document={guide} kind="methods" listingKey={L.key} onTask={selectTask}/> : <div className="eh-md"><ReactMarkdown remarkPlugins={[remarkGfm]} components={{img:()=>null}}>{fillTokens(d.methods || d.readme || '',tasks)}</ReactMarkdown></div>}</div> : null}
        {d && (tab==='tasks'||tab==='sets') ? <div className="eh-original-tasks"><div className="eh-task-set-shortcut">{tab==='sets' ? <button className="eh-inline-link" onClick={()=>navigate('tasks',{},true)}>← Task guides</button> : <button className="eh-inline-link" onClick={()=>navigate('sets',{},true)}>Compare parameter sets{parameterGuide?.sets?.length?' ('+parameterGuide.sets.length+')':''}</button>}</div>
          {tab==='sets' ? <p className="eh-task-set-explanation">A task defines the procedure. A parameter set supplies its stimulus, timing and response settings.</p> : null}
          {tab==='tasks' ? <EHRestoredTaskGuide guide={guide} rawTasks={tasks} parameterGuide={parameterGuide} selectedId={route.task || defaultTask?.taskKey} listingKey={L.key} onTask={selectTask} onSet={selectSet} onFile={onFile}/> : <EHConfigurationReader listing={L} guide={parameterGuide} tasks={tasks} selectedId={route.set} onSelect={selectSet} onTask={selectTask} onFile={onFile}/>}
          {asList(L.ladders).length ? <details className="eh-parameter-group"><summary>Training ladders <span>{asList(L.ladders).length}</span></summary><p className="eh-source-note">Ordered practice stages leading to the task, separate from its parameter sets.</p><ul>{asList(L.ladders).map((x,i)=><li key={i}>{typeof x==='string'?x:x.title || x.name}</li>)}</ul></details> : null}
        </div> : null}
        {tab==='versions' && versionsView ? versionsView : null}
        {extraKeys.includes(tab) && tab!=='files' && renderExtra ? renderExtra(tab) : null}
        {tab==='versions' && !versionsView ? <section className="eh-reference-versions"><h2>Versions</h2><div className="eh-table-wrap"><table className="eh-release-table"><thead><tr><th>Version</th><th>Status</th><th>Recorded</th><th>Source</th></tr></thead><tbody>{releaseRows.map(v=><tr key={v.id} className={v.id===L.bundle.id?'is-current':''}><td>{v.version?'v'+v.version:'Unversioned'}</td><td>{v.id===L.bundle.id?'Current':'Saved'}</td><td>{v.at?new Date(v.at).toLocaleDateString():''}</td><td className="mono">{v.commit || v.id}</td></tr>)}</tbody></table></div><p className="eh-source-note">The current page describes the current source bundle. Earlier recorded bundles remain stored; these rows do not substitute older methods for the current version.</p><button className="eh-inline-link" onClick={()=>navigate('details')}>Replication and source details</button></section> : null}
        {tab==='files' ? renderFiles(L.bundle.path) : null}
        {d && tab==='details' ? <section className="eh-details-page">
          {(L.original || L.status)?renderReplication(L,d):null}
          <dl className="eh-detail-facts"><dt>Version</dt><dd>{L.version?'v'+L.version:'Not specified'}</dd><dt>Source</dt><dd>{src.repo || src.kind || 'Not specified'}{src.subdir?'/'+src.subdir:''}{src.commit?' @ '+src.commit.slice(0,7):''}{src.pushed===false?' (local version, not yet on GitHub)':''}</dd><dt>Alhazen</dt><dd>{L.alhazen || 'Not specified'}</dd><dt>Subjects</dt><dd>{asList(L.subjects).map(s=>s==='human'?'Human':'Monkey').join(', ')}</dd><dt>Hardware</dt><dd>{hwKeys(L).map(k=>(HW.find(x=>x[0]===k)||[k,k])[1]).join(', ')}</dd><dt>Files</dt><dd>{L.bundle.fileCount != null ? L.bundle.fileCount+' · '+kb(L.bundle.bytes) : 'See Files'}</dd><dt>Licence</dt><dd>{L.license || 'Not specified'}</dd><dt>Updated</dt><dd>{ago(L.updatedAt)}</dd>{L.docsource?<><dt>Methods</dt><dd>{L.docsource}</dd></>:null}</dl>
          {ehDecode(d.references,[],'References').length ? <section className="eh-doc-section"><h3>References</h3><ol>{ehDecode(d.references,[],'References').map((r,i)=><li key={i}>{typeof r==='string'?r:JSON.stringify(r)}</li>)}</ol></section> : null}
          {presentation?.figures?.length ? <details className="eh-parameter-group eh-old-exports"><summary>Earlier figure files <span>{presentation.figures.length}</span></summary><p className="eh-source-note">The previous illustrated plates are kept as an archive, not used in the method guides above.</p>{presentation.figures.map(f=><div key={f.id}><span>{f.title}</span><div>{Object.entries(f.formats || {}).map(([format,path])=>art.url?<a key={format} href={art.url+art.encodePath(path)} target="_blank" rel="noreferrer" download>{format.toUpperCase()}</a>:null)}</div></div>)}</details> : null}
          {src.kind==='fork'&&src.from?<p>Changed from {src.from.title}{src.from.publisherName?' by '+src.from.publisherName:''}.</p>:null}
        </section> : null}
      </div>
    </div>
  </article>;
}
