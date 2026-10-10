/* A fetch stub of the AI authoring API (ai-authoring CONTRACT.md) for the
 * hub page tests: an in-memory hub with keys, drafts, plan/source jobs and
 * acceptance into a private version. Jobs advance one state per poll
 * (queued -> running -> done | failed). Behaviour knobs:
 *   enabled        GET /ai/status enabled (default true)
 *   keys           initial [{provider, hint, rotated_at}]
 *   planOutcome    'done' | an error code (job fails with it)
 *   sourceOutcome  'done' | 'invalid' (validation fails) | an error code
 *   fail           {'METHOD /path': {status, code, message}} one-shot HTTP errors
 */
export const PLAN = {
  title: 'Saccade adaptation in two monkeys',
  summary: 'Intrasaccadic back-step of 2 dva; pre, adaptation and post blocks.',
  paradigm: 'Double-step saccade adaptation.',
  subject_kind: 'monkey',
  stimuli: [{name: 'Target', size: '0.3 dva', notes: 'Red dot'}],
  measures: [{name: 'Gain', unit: 'ratio', definition: 'Amplitude / target step'}],
  parameters: [
    {name: 'step_dva', meaning: 'Primary target step', unit: 'dva', default: 10, constraints: '5-15'},
    {name: 'backstep_dva', meaning: 'Intrasaccadic step', unit: 'dva', default: 2, constraints: null},
  ],
  timeline: [{phase: 'Fixate', duration: 500, note: 'Window 2 dva'}, {phase: 'Saccade', duration: 'event-driven', note: 'To the target'}, {phase: 'ITI', duration: 'parameterized', note: ''}],
  tasks: [{name: 'adapt', description: 'Adaptation trials'}],
  tests: [{name: 'Schedule', how: '500 trials in three blocks'}],
  hardware: {display: true, eye_tracker: true, reward: true},
  notes: 'Reward on landing.',
};
export const REPORT_OK = {
  files: [{path: 'run.py', size: 1200}, {path: 'src/adapt/task.py', size: 5400}, {path: 'configs/task.yaml', size: 300}],
  manifest: {name: 'saccade-adaptation', version: '0.1.0', license: 'MIT', ai_assisted: true},
  report: {ok: true, checks: [{name: 'Package rules', ok: true}, {name: 'Python syntax', ok: true}, {name: 'Documentation schema', ok: true}]},
};
export const REPORT_BAD = {
  files: [{path: 'run.py', size: 1200}],
  report: {ok: false, checks: [{name: 'Package rules', ok: true}, {name: 'Python syntax', ok: false, message: 'src/adapt/task.py line 3: invalid syntax'}]},
};

export function aiHub(base, options) {
  const o = Object.assign({enabled: true, keys: [], planOutcome: 'done', sourceOutcome: 'done', fail: {}}, options || {});
  const s = {keys: o.keys.slice(), drafts: new Map(), jobs: new Map(), library: (o.library || []).slice(), n: 0, accepted: [], sentKeys: []};
  const err = (status, code, message) => ({status, body: {error: {code, message: message || code}}});
  const draftOut = (d) => ({draft: Object.assign({}, d, {plan: undefined}), plan: d.plan || null,
    jobs: [...s.jobs.values()].filter((j) => j.draft_id === d.id)});
  function newJob(kind, d) {
    s.n += 1;
    const job = {id: 'j' + s.n, kind, status: 'queued', draft_id: d.id, provider: d.provider, model: d.model || 'gpt-test',
      error_code: null, error_message: null, result: null, created_at: '2026-10-09T10:0' + s.n + ':00Z',
      disclosed: {files: d.start_version_id ? ['run.py', 'task.py'] : [], bytes: 2048}};
    s.jobs.set(job.id, job);
    return job;
  }
  function advance(job) {
    if (job.status === 'queued') { job.status = 'running'; return; }
    if (job.status !== 'running') return;
    const d = s.drafts.get(job.draft_id);
    const outcome = job.kind === 'plan' ? o.planOutcome : o.sourceOutcome;
    if (outcome === 'done') {
      job.status = 'done';
      if (job.kind === 'plan') { d.plan = o.plan || PLAN; d.status = 'planned'; } else { job.result = REPORT_OK; d.status = 'generated'; }
    } else if (outcome === 'invalid') {
      job.status = 'failed'; job.error_code = 'generation_invalid'; job.error_message = 'The model output failed validation after one repair round.';
      job.result = REPORT_BAD;
    } else {
      job.status = 'failed'; job.error_code = outcome; job.error_message = 'Provider said: ' + outcome;
    }
  }
  const routes = Object.assign({}, base);
  function match(key) {
    if (o.fail[key]) {
      const f = o.fail[key];
      delete o.fail[key];
      return () => err(f.status, f.code, f.message);
    }
    if (key === 'GET /ai/status') {
      return () => ({status: 200, body: {enabled: o.enabled, providers: [
        {id: 'openai', name: 'OpenAI', models: ['gpt-test', 'gpt-big'], default_model: 'gpt-test'},
        {id: 'anthropic', name: 'Anthropic', models: ['claude-test'], default_model: 'claude-test'}], keys: s.keys}});
    }
    let m;
    if ((m = /^PUT \/ai\/keys\/([a-z]+)$/.exec(key))) {
      return (req) => {
        const value = req.json && req.json.key;
        s.sentKeys.push(value);
        if (typeof value !== 'string' || value.length < 8) return err(400, 'invalid', 'That does not look like an API key.');
        const saved = {provider: m[1], hint: value.slice(-4), rotated_at: '2026-10-09T12:00:00Z'};
        s.keys = s.keys.filter((k) => k.provider !== m[1]).concat([saved]);
        return {status: 200, body: saved};
      };
    }
    if ((m = /^DELETE \/ai\/keys\/([a-z]+)$/.exec(key))) {
      return () => { s.keys = s.keys.filter((k) => k.provider !== m[1]); return {status: 204}; };
    }
    if (key === 'POST /ai/drafts') {
      return (req) => {
        const b = req.json || {};
        if (!o.enabled) return err(403, 'ai_disabled');
        if (!s.keys.some((k) => k.provider === b.provider)) return err(409, 'key_required', 'No key saved for ' + b.provider + '.');
        if (!b.prompt || b.prompt.length > 8000) return err(400, 'invalid', 'prompt');
        s.n += 1;
        const d = {id: 'd' + s.n, experiment_id: 'xe' + s.n, prompt: b.prompt, provider: b.provider, model: b.model,
          start_experiment_id: b.start_from ? b.start_from.experiment_id : null, start_version_id: b.start_from ? b.start_from.version_id : null,
          status: 'planning', created_at: '2026-10-09T10:00:00Z', updated_at: '2026-10-09T10:00:00Z'};
        s.drafts.set(d.id, d);
        const job = newJob('plan', d);
        d.plan_job_id = job.id;
        return {status: 202, body: {draft: d, job}};
      };
    }
    if (key === 'GET /ai/drafts') return () => ({status: 200, body: {items: [...s.drafts.values()]}});
    if ((m = /^GET \/ai\/drafts\/([\w-]+)$/.exec(key))) {
      return () => (s.drafts.has(m[1]) ? {status: 200, body: draftOut(s.drafts.get(m[1]))} : err(404, 'not_found'));
    }
    if ((m = /^DELETE \/ai\/drafts\/([\w-]+)$/.exec(key))) {
      return () => { const d = s.drafts.get(m[1]); if (!d) return err(404, 'not_found'); d.status = 'discarded'; return {status: 204}; };
    }
    if ((m = /^POST \/ai\/drafts\/([\w-]+)\/generate$/.exec(key))) {
      return () => {
        const d = s.drafts.get(m[1]);
        if (!d || !d.plan) return err(409, 'conflict', 'No accepted plan.');
        const job = newJob('source', d);
        d.source_job_id = job.id;
        return {status: 202, body: {job}};
      };
    }
    if ((m = /^POST \/ai\/drafts\/([\w-]+)\/accept$/.exec(key))) {
      return (req) => {
        const d = s.drafts.get(m[1]);
        if (!d || d.status !== 'generated') return err(409, 'conflict', 'Validation failed or already accepted.');
        s.accepted.push(req.json);
        d.status = 'accepted';
        d.version_id = 'xv1';
        return {status: 200, body: {experiment: {id: d.experiment_id, title: req.json.title}, version: {id: 'xv1', version: '0.1.0', manifest: REPORT_OK.manifest}}};
      };
    }
    if ((m = /^GET \/ai\/jobs\/([\w-]+)$/.exec(key))) {
      return () => { const j = s.jobs.get(m[1]); if (!j) return err(404, 'not_found'); advance(j); return {status: 200, body: {job: j}}; };
    }
    if ((m = /^POST \/ai\/jobs\/([\w-]+)\/cancel$/.exec(key))) {
      return () => { const j = s.jobs.get(m[1]); j.status = 'cancelled'; const d = s.drafts.get(j.draft_id); if (j.kind === 'plan') d.status = 'planning'; return {status: 200, body: {job: j}}; };
    }
    if ((m = /^DELETE \/library\/([\w-]+)$/.exec(key))) {
      return () => { s.library = s.library.filter((i) => i.experiment.id !== m[1]); return {status: 204}; };
    }
    if (key === 'GET /library') return () => ({status: 200, body: {items: s.library, next_offset: null}});
    return routes[key];
  }
  const proxy = new Proxy(routes, {get: (target, key) => (typeof key === 'string' ? match(key) : undefined)});
  return {routes: proxy, state: s, options: o};
}
