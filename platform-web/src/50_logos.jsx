function rng(seed) {
  let x = 2166136261;
  for (const ch of String(seed || 'alhazen')) x = Math.imul(x ^ ch.charCodeAt(0), 16777619) >>> 0;
  return () => { x = (Math.imul(x ^ (x >>> 15), 2246822507) + 0x9e3779b9) >>> 0; x ^= x >>> 13; return (x >>> 0) / 4294967296; };
}
const f1 = (n) => Math.round(n * 10) / 10;

function Schematic({ kind, seed, wide }) {
  const W = wide ? 360 : 240, H = wide ? 150 : 100, cx = W / 2, cy = H / 2, s = H / 100;
  const rand = rng(seed);
  const els = [];
  let k = 0;
  const add = (el) => els.push(React.cloneElement(el, { key: k++ }));
  const fix = (x, y, r) => add(<circle cx={f1(x)} cy={f1(y)} r={f1(r || 2.6 * s)} className="sch-accent" />);
  const gabor = (x, y, r, angle) => {
    const lines = []; const period = r / 2.6;
    for (let i = -6; i <= 6; i++) {
      const off = i * period; if (Math.abs(off) > r) continue;
      const half = Math.sqrt(r * r - off * off); const env = Math.exp(-(off * off) / (2 * (r / 2.2) * (r / 2.2)));
      lines.push(<line key={i} x1={f1(x + off)} y1={f1(y - half * .92)} x2={f1(x + off)} y2={f1(y + half * .92)} className="sch-stroke" strokeWidth={f1(period * .5)} strokeLinecap="round" opacity={f1(.12 + .78 * env)} />);
    }
    add(<g transform={`rotate(${angle} ${f1(x)} ${f1(y)})`}>{lines}</g>);
  };
  if (kind === 'pair') {
    gabor(cx - 46 * s, cy, 26 * s, 45); gabor(cx + 46 * s, cy, 26 * s, 45);
    add(<path d={`M${f1(cx - 46 * s)} ${f1(cy - 36 * s)}l${f1(-5 * s)} ${f1(-7 * s)}h${f1(10 * s)}z`} className="sch-accent" />);
    fix(cx, cy);
  } else if (kind === 'dots') {
    const R = 38 * s;
    add(<circle cx={cx} cy={cy} r={f1(R)} className="sch-ring sch-dash" />);
    for (let i = 0; i < 46; i++) {
      const a = rand() * Math.PI * 2, d = Math.sqrt(rand()) * (R - 4 * s);
      const x = cx + Math.cos(a) * d, y = cy + Math.sin(a) * d;
      if (Math.hypot(x - cx, y - cy) < 6 * s) continue;
      if (rand() < .55) add(<line x1={f1(x - 6 * s)} y1={f1(y)} x2={f1(x)} y2={f1(y)} className="sch-trail" strokeWidth={f1(1.6 * s)} strokeLinecap="round" />);
      add(<circle cx={f1(x)} cy={f1(y)} r={f1(1.9 * s)} className="sch-ink" />);
    }
    fix(cx, cy);
  } else if (kind === 'cylinder') {
    const R = (wide ? 120 : 82) * s / (wide ? 1.5 : 1), h = 30 * s;
    add(<ellipse cx={cx} cy={f1(cy - h)} rx={f1(R)} ry={f1(7 * s)} className="sch-ring" />);
    add(<ellipse cx={cx} cy={f1(cy + h)} rx={f1(R)} ry={f1(7 * s)} className="sch-ring sch-dash" />);
    for (let i = 0; i < 64; i++) {
      const th = rand() * Math.PI * 2, y = cy - h + rand() * 2 * h, x = cx + Math.sin(th) * R;
      const front = Math.cos(th) > 0, dir = front ? 1 : -1, r = (front ? 2.3 : 1.5) * s;
      add(<line x1={f1(x - dir * 5 * s * Math.abs(Math.cos(th)))} y1={f1(y)} x2={f1(x)} y2={f1(y)} className="sch-trail" strokeWidth={f1(1.3 * s)} strokeLinecap="round" opacity={front ? .9 : .45} />);
      add(<circle cx={f1(x)} cy={f1(y)} r={f1(r)} className={front ? 'sch-ink' : 'sch-mid'} opacity={front ? 1 : .55} />);
    }
    add(<circle cx={f1(cx + R)} cy={cy} r={f1(3.4 * s)} className="sch-accent" />);
    fix(cx, cy, 2.2 * s);
  } else if (kind === 'inducers') {
    const R = 30 * s, r = 10 * s;
    for (let i = 0; i < 3; i++) {
      const a = -Math.PI / 2 + (i / 3) * Math.PI * 2, x = cx + 20 * s + Math.cos(a) * R, y = cy + 4 * s + Math.sin(a) * R;
      const to = Math.atan2(cy + 4 * s - y, cx + 20 * s - x), a1 = to - Math.PI / 6, a2 = to + Math.PI / 6;
      add(<path className="sch-ink" d={`M${f1(x)} ${f1(y)}L${f1(x + Math.cos(a2) * r)} ${f1(y + Math.sin(a2) * r)}A${f1(r)} ${f1(r)} 0 1 1 ${f1(x + Math.cos(a1) * r)} ${f1(y + Math.sin(a1) * r)}Z`} />);
    }
    fix(cx - 70 * s, cy);
    add(<path d={`M${f1(cx - 60 * s)} ${f1(cy)}H${f1(cx - 8 * s)}`} className="sch-trail sch-dash" strokeWidth={f1(1.5 * s)} />);
  } else {
    add(<circle cx={cx} cy={cy} r={f1(24 * s)} className="sch-ring sch-dash" />);
    let x = cx + 9 * s, y = cy - 6 * s, d = `M${f1(x)} ${f1(y)}`;
    for (let i = 0; i < 22; i++) { x += (rand() - .5) * 5 * s + (cx - x) * .18; y += (rand() - .5) * 5 * s + (cy - y) * .18; d += `L${f1(x)} ${f1(y)}`; }
    add(<path d={d} className="sch-trail" strokeWidth={f1(s)} strokeLinejoin="round" />);
    add(<circle cx={cx} cy={cy} r={f1(10 * s)} className="sch-ring" />);
    fix(cx, cy, 3 * s);
  }
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="sch" preserveAspectRatio="xMidYMid slice" aria-hidden="true" focusable="false">
      <rect x="0" y="0" width={W} height={H} className="sch-bg" />{els}
    </svg>
  );
}


// Experiment identities are deliberately separate from source-checked method figures.
// Original Store ink/oxblood motifs are retained; classics have their own marks.
const EH_LOGO_KINDS = {
  'amodal-averaging':'inducers', 'attention-clamp':'pair', 'kde-vergence':'cylinder', 'mbri':'optimise',
  'britten-1992':'dots', 'cavanagh-1992':'tracking', 'funahashi-1989':'delay',
  'lisi-cavanagh-2015':'double-drift', 'moore-armstrong-2003':'gating', 'moore-fallah-2001':'detection',
  'posner-1980':'cue', 'raymond-shapiro-arnell-1992':'blink', 'rensink-oregan-clark-1997':'change',
  'shepard-metzler-1971':'rotation', 'sperling-1960':'iconic-memory', 'sternberg-1966':'scanning',
  'stroop-1935':'stroop', 'treisman-gelade-1980':'search'
};
function ehLogoKind(item) {
  for (const id of [item?.id,item?.exp,item?.origin?.exp,item?.source?.from?.exp]) {
    if (id && Object.prototype.hasOwnProperty.call(EH_LOGO_KINDS,id)) return EH_LOGO_KINDS[id];
  }
  return typeof item?.art==='string' && item.art ? item.art : 'fixation';
}
function EHExperimentLogo({ item, wide=false }) {
  const kind=ehLogoKind(item),seed=kind;
  if(['pair','cylinder','inducers','dots','fixation'].includes(kind) || !Object.values(EH_LOGO_KINDS).includes(kind)) {
    const mark=Schematic({kind,seed,wide});
    return <div className="eh-experiment-logo" data-logo-kind={kind}>{React.cloneElement(mark,{preserveAspectRatio:'xMidYMid meet'})}</div>;
  }
  return <div className="eh-experiment-logo" data-logo-kind={kind}><EHIdentityMark kind={kind}/></div>;
}
function EHIdentityMark({kind}) {
  const els=[];let key=0;const add=e=>els.push(React.cloneElement(e,{key:key++}));
  const ink='var(--ink)', accent='var(--accent-ink)', muted='var(--muted)', soft='var(--surface-3)', paper='var(--surface-2)';
  const line=(x1,y1,x2,y2,color=ink,width=1.6,opacity=1,dash)=>add(<line x1={x1} y1={y1} x2={x2} y2={y2} stroke={color} strokeWidth={width} strokeLinecap="round" opacity={opacity} strokeDasharray={dash}/>);
  const path=(d,color=ink,width=1.6,opacity=1,fill='none',dash)=>add(<path d={d} fill={fill} stroke={color} strokeWidth={width} strokeLinecap="round" strokeLinejoin="round" opacity={opacity} strokeDasharray={dash}/>);
  const circle=(cx,cy,r,fill=ink,stroke='none',width=1.6,opacity=1)=>add(<circle cx={cx} cy={cy} r={r} fill={fill} stroke={stroke} strokeWidth={width} opacity={opacity}/>);
  const rect=(x,y,w,h,fill='none',stroke=ink,width=1.5,rx=0,opacity=1)=>add(<rect x={x} y={y} width={w} height={h} rx={rx} fill={fill} stroke={stroke} strokeWidth={width} opacity={opacity}/>);
  const text=(x,y,value,size=18,color=ink,weight=650)=>add(<text x={x} y={y} fill={color} fontFamily="Manrope,system-ui,sans-serif" fontSize={size} fontWeight={weight} textAnchor="middle" dominantBaseline="central">{value}</text>);
  const polar=(cx,cy,r,a)=>[cx+Math.cos(a)*r,cy+Math.sin(a)*r];
  const arc=(cx,cy,r,a,b,color=ink,width=1.6,opacity=1)=>{const p=polar(cx,cy,r,a),q=polar(cx,cy,r,b);path(`M${p[0]} ${p[1]}A${r} ${r} 0 ${b-a>Math.PI?1:0} 1 ${q[0]} ${q[1]}`,color,width,opacity);};
  const gabor=(x,y,r,angle=0)=>{
    const lines=[];
    for(let i=-6;i<=6;i++){const off=i*r/5.5;if(Math.abs(off)>=r)continue;const h=Math.sqrt(r*r-off*off);lines.push(<line key={i} x1={x+off} x2={x+off} y1={y-h} y2={y+h} stroke={ink} strokeWidth={r/7.6} strokeLinecap="round" opacity={0.13+0.8*Math.exp(-Math.pow(off/(r*.53),2)/2)}/>);}
    add(<g transform={`rotate(${angle} ${x} ${y})`}>{lines}</g>);
  };
  if(kind==='stroop') {
    rect(55,17,130,66,'none','var(--line-strong)',1,3);
    text(120,48,'BLUE',34,accent,780);
    line(75,75,105,75,ink,4);line(111,75,138,75,muted,4,.48);line(144,75,166,75,accent,4);
    path('M48 31V12H70M170 88H192V69',muted,1,.5);
  } else if(kind==='search') {
    const tx=120,ty=48;
    for(let row=0;row<3;row++)for(let col=0;col<5;col++){
      const x=72+col*24,y=24+row*24,hit=row===1&&col===2;
      const c=hit?accent:ink,op=hit?1:.6;
      if(hit || (row+col)%3===0){line(x-5,y-6,x+5,y-6,c,hit?2.6:1.8,op);line(x,y-6,x,y+6,c,hit?2.6:1.8,op);}
      else {line(x-5,y-6,x+5,y+6,c,1.6,op);line(x+5,y-6,x-5,y+6,c,1.6,op);}
    }
    circle(tx,ty,15,'none',accent,1.6);line(tx+11,ty+11,tx+21,ty+21,accent,2.4);
  } else if(kind==='cue') {
    rect(45,36,27,27,'none',muted,1.2,2,.45);
    rect(168,36,27,27,accent,'none',0,2);
    line(86,50,149,50,ink,2.4);path('M136 37L150 50L136 63',ink,2.4);
    path('M164 28H202V71H164',accent,1,.5);circle(120,77,2.4,muted);
  } else if(kind==='blink') {
    line(38,52,202,52,muted,1,.3);
    const heights=[12,20,17,35,16,9,29,19,12];
    heights.forEach((h,i)=>{const x=48+i*18;const c=i===3||i===6?accent:ink;
      if(i===6){line(x,52-h/2,x,49,c,4,.25);line(x,57,x,52+h/2,c,4,.25);circle(x,52,8,'none',accent,1.4);}
      else line(x,52-h/2,x,52+h/2,c,i===3?5:2.4,i===3?1:.5);
    });
    path('M101 24Q128 7 156 24',muted,1.2,.6);path('M151 18L156 24L149 26',muted,1.2,.6);
    line(102,76,156,76,accent,1.8);circle(102,76,2,accent);circle(156,76,2,'none',accent,1.4);
  } else if(kind==='change') {
    const scene=(x,y,changed)=>{
      rect(x,y,80,53,paper,ink,1.7,3);
      path(`M${x+1} ${y+38}L${x+25} ${y+17}L${x+47} ${y+38}L${x+63} ${y+27}L${x+79} ${y+40}`,muted,1.3,.6);
      rect(x+48,y+32,17,16,changed?accent:soft,'none',0);
      path(`M${x+45} ${y+32}L${x+56.5} ${y+22}L${x+68} ${y+32}`,ink,1.5);
      circle(x+16,y+13,3,muted,'none',0,.4);
    };
    scene(43,16,false);scene(114,32,true);circle(171,69,13,'none',accent,1.5);
    line(100,76,109,76,muted,1.4);path('M106 73L110 76L106 79',muted,1.2);
  } else if(kind==='iconic-memory') {
    const glyphs=['R','7','K','2','M','4','Z','9','3','L','X','6'];
    for(let row=0;row<3;row++)for(let col=0;col<4;col++)text(88+col*23,27+row*23,glyphs[row*4+col],17,row===1?accent:ink,row===1?750:500);
    path('M73 13H63V85H73M167 13H177V85H167',muted,1.2,.5);
    path('M43 43L53 50L43 57Z',accent,0,1,accent);line(62,50,69,50,accent,2);
  } else if(kind==='scanning') {
    rect(55,25,101,49,'none',muted,1.2,3,.35);
    rect(63,18,101,49,paper,muted,1.2,3,.6);
    rect(71,11,101,49,paper,ink,1.5,3);
    text(90,35,'2',18,muted,550);text(120,35,'4',18,accent,750);text(149,35,'7',18,muted,550);
    circle(134,66,19,paper,ink,2.2);text(134,65,'4',20,accent,720);line(148,80,163,94,ink,3.2);
  } else if(kind==='delay') {
    for(let i=0;i<8;i++){const a=-Math.PI/2+i*Math.PI/4;const [x,y]=polar(120,50,35,a);circle(x,y,i===1?5:2.3,i===1?accent:ink,'none',0,i===1?1:.35);}
    arc(120,50,25,-Math.PI*.5,Math.PI*1.06,muted,1.2,.5);
    line(120,50,120,32,ink,1.7);line(120,50,132,56,ink,1.7);circle(120,50,3,accent);
    circle(144.75,25.25,10,'none',accent,1.3);path('M98 54L96 46L104 48',muted,1.2,.5);
  } else if(kind==='tracking') {
    for(let i=0;i<16;i++){const a=i*Math.PI/8,selected=i===1||i===9;
      const p=polar(120,50,22,a),q=polar(120,50,39,a);
      line(p[0],p[1],q[0],q[1],selected?accent:ink,selected?6:3.2,selected?1:.55);
    }
    circle(120,50,8,'none',ink,1.4);circle(120,50,3,accent);
    arc(120,50,44,-Math.PI*.34,Math.PI*.3,accent,1.5);path('M146 86L145 77L154 78',accent,1.5);
    arc(120,50,44,Math.PI*.68,Math.PI*1.3,muted,1.2,.4);
  } else if(kind==='double-drift') {
    // The carrier bars follow the external path; the internal-motion arrow crosses it.
    add(<g transform="rotate(28 129 50)">
      <path d="M129 7V93" fill="none" stroke={muted} strokeWidth="1.1" strokeDasharray="3 5" opacity=".45"/>
      <path d="M125 13L129 7L133 13" fill="none" stroke={muted} strokeWidth="1.2"/>
    </g>);
    gabor(129,50,29,28);
    add(<g transform="rotate(28 129 50)"><path d="M100 50H158M151 44L158 50L151 56" fill="none" stroke={accent} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></g>);
    circle(63,51,3.1,accent);circle(63,51,10,'none','var(--line-strong)',1.2);
  } else if(kind==='gating') {
    circle(80,58,24,'none',muted,1.2,.45);circle(170,58,24,'none',accent,1.3,.6);
    add(<g transform="rotate(-32 80 58)"><rect x="77" y="44" width="6" height="28" fill={muted} opacity=".45"/></g>);
    add(<g transform="rotate(-32 170 58)"><rect x="167" y="44" width="6" height="28" fill={accent}/></g>);
    path('M121 17L112 35H122L117 49L136 28H125L130 17Z',ink,0,1,ink);
    path('M138 34Q155 34 166 45',accent,1.4);path('M161 43L167 46L166 39',accent,1.4);
    line(110,61,140,61,muted,1.3,.45,'3 4');
  } else if(kind==='detection') {
    circle(73,51,3,ink);circle(73,51,12,'none','var(--line-strong)',1.2);
    rect(151,26,23,23,accent,'none',0,1);rect(151,26,11.5,23,paper,'none',0,0,.55);
    rect(180,64,13,13,muted,'none',0,1,.3);circle(162.5,37.5,23,'none',accent,1.2,.6);
    path('M91 50Q114 50 132 41',ink,1.5);path('M126 38L133 41L128 47',ink,1.5);
    arc(162.5,37.5,28,-Math.PI*.15,Math.PI*.35,muted,1,.35);
  } else if(kind==='optimise') {
    circle(77,34,19,'none',muted,1.2,.35);gabor(77,34,13,30);
    circle(167,32,19,'none',muted,1.2,.35);
    for(let i=0;i<12;i++){const a=i*2.39996,r=3+Math.sqrt(i/12)*10;circle(167+Math.cos(a)*r,32+Math.sin(a)*r,1.3,muted,'none',0,.55);}
    path('M90 50L109 67M154 48L136 65',muted,1.2,.4);
    circle(122,71,18,paper,accent,2);circle(122,71,10,'none',ink,1.2);circle(122,71,3.5,accent);
    path('M113 16Q121 10 130 16',muted,1.2,.4);path('M126 12L130 16L125 19',muted,1.2,.4);
  } else if(kind==='rotation') {
    const cube=(x,y,z,ox,oy,flip,highlight)=>{
      const p=(a,b,c)=>[ox+flip*(a-b)*9,oy+(a+b)*4.5-c*10];
      const face=(pts,fill)=>add(<polygon points={pts.map(v=>p(...v).join(',')).join(' ')} fill={fill} stroke={ink} strokeWidth=".95" strokeLinejoin="round"/>);
      face([[x,y,z+1],[x+1,y,z+1],[x+1,y+1,z+1],[x,y+1,z+1]],highlight?accent:'var(--surface-3)');
      face([[x,y+1,z],[x+1,y+1,z],[x+1,y+1,z+1],[x,y+1,z+1]],paper);
      face([[x+1,y,z],[x+1,y+1,z],[x+1,y+1,z+1],[x+1,y,z+1]],highlight?accent:'var(--line-strong)');
    };
    const cells=[[0,0,0],[1,0,0],[2,0,0],[2,1,0],[2,2,0],[2,2,1],[2,2,2]];
    cells.forEach(([x,y,z],i)=>cube(x,y,z,69,48,1,i===6));
    cells.forEach(([x,y,z],i)=>cube(x,y,z,171,48,-1,i===6));
    path('M104 24Q121 12 139 24',accent,1.6);path('M132 19L139 24L131 27',accent,1.6);
  }
  return <svg className="sch eh-identity-mark" viewBox="0 0 240 100" preserveAspectRatio="xMidYMid meet" aria-hidden="true" focusable="false"><rect width="240" height="100" fill={paper}/>{els}</svg>;
}

function Glyph({ k }) {
  if (k === 'eye_tracker') return <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4"><path d="M1.5 8s2.4-4.5 6.5-4.5S14.5 8 14.5 8 12.1 12.5 8 12.5 1.5 8 1.5 8z" /><circle cx="8" cy="8" r="2.2" /></svg>;
  if (k === 'reward') return <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4"><path d="M8 1.8C8 1.8 3.6 7 3.6 10a4.4 4.4 0 0 0 8.8 0C12.4 7 8 1.8 8 1.8z" /></svg>;
  if (k === 'microstimulation') return <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4"><path d="M9.5 1.5L4 9h4l-1.5 5.5L12 7H8z" /></svg>;
  if (k === 'recording') return <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4"><path d="M1 8h3l1.5-4 2.5 8 2-6 1 2h4" /></svg>;
  if (k === 'audio') return <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4"><path d="M2 6h3l4-3v10l-4-3H2z" /><path d="M11.5 5.5a3.5 3.5 0 0 1 0 5" /></svg>;
  if (k === 'check') return <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M3 8.5l3 3 7-7" /></svg>;
  return <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4"><rect x="1.8" y="2.5" width="12.4" height="8.5" rx="1.2" /><path d="M5.5 13.8h5M8 11v2.8" /></svg>;
}
