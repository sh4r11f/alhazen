/* Alhazen experiment workspace — the Training panel (a training ladder's
 * stages on the Run page) and the History page's training summary.
 *
 * A ladder (alhazen.training.ladder) is the stages a monkey climbs toward an
 * experiment's final task. In Training mode the Run page shows it as a
 * ladder: the stages in order, each with what it pays, its criterion when it
 * declares one, and how the chosen subject has done there (sessions, finished
 * trials, success rate, from GET /api/training). The operator chooses the
 * stage; a criterion is only ever shown as a recommendation, never acted on.
 *
 *   TrainingLadder.mount(box, {node, onChange})  the Run page's panel
 *     .render(project, subjectCode)              draw the ladder(s)
 *     .setHistory(answer)                        GET /api/training's answer
 *     .selection() -> {ladder, ladderName, label, stage, rehearse} | null
 *     .problem() -> '' | why a launch cannot go yet
 *   TrainingLadder.historyPanel(answer, {node, panel}) -> element | null
 *
 * Everything shown is text: ladder files are written by people.
 */
'use strict';

const TrainingLadder = (() => {
  const STORE = 'alhazen-workspace-training:';

  /* What a recommendation verdict looks like: the lamp's state and its word. */
  const VERDICTS = {
    advance: ['ok', 'Criterion met — consider the next stage'],
    'go back': ['bad', 'Criterion says go back a stage'],
    stay: ['run', 'Not met yet — stay'],
    'too few trials': ['none', 'Too few trials to judge'],
  };

  function percent(x) {
    return x === null || x === undefined ? '—' : `${Math.round(100 * x)}%`;
  }

  /* A delivery in words: "2 × 200 ms". */
  function pulses(p) {
    if (!p) return null;
    if (p.volume_ul) return `${p.volume_ul} µL in ${p.pulse_ms ?? 200} ms pulses`;
    const n = p.n_pulses ?? 2;
    if (n === 0) return 'nothing';
    return `${n} × ${p.pulse_ms ?? 200} ms`;
  }

  /* A criterion in words: "success ≥ 80% over the last 100 (from 50)". */
  function criterionText(c) {
    if (!c) return null;
    const names = {success_rate: 'success', completed_rate: 'completed', mean_rt_ms: 'mean RT'};
    const say = (entries, op) => Object.entries(entries || {}).map(([k, v]) => {
      const value = k.endsWith('_rate') ? `${Math.round(100 * v)}%` : `${v}`;
      return `${names[k] || k} ${op} ${value}`;
    });
    const parts = [...say(c.promote_when, '≥')];
    const back = say(c.demote_when, '≤');
    const over = `over the last ${c.window} trials (judged from ${c.min_trials})`;
    let text = parts.length ? `${parts.join(' and ')} ${over}` : `Judged ${over}`;
    if (back.length) text += `; back if ${back.join(' or ')}`;
    return text;
  }

  /* The panel's last choice for a project, kept as "ladder\tstage\trehearse"
   * (a tab never occurs in a label or an id), so a reload keeps it. */
  function remembered(projectId) {
    const text = localStorage.getItem(STORE + projectId);
    if (!text) return null;
    const [ladder, stage, rehearse] = text.split('\t');
    return {ladder: ladder || null, stage: stage || null, rehearse: rehearse === '1'};
  }

  function remember(projectId, value) {
    localStorage.setItem(STORE + projectId,
      [value.ladder || '', value.stage || '', value.rehearse ? '1' : ''].join('\t'));
  }

  function mount(box, {node, onChange}) {
    let project = null;
    let subject = '';
    let history = null;
    let choice = {ladder: null, stage: null, rehearse: false};

    const ladders = () => project?.ladders || [];
    const chosenLadder = () => ladders().find((l) => l.label === choice.ladder) || ladders()[0];
    const stageHistory = (ladder, stageId) => {
      const found = (history?.ladders || []).find((l) => l.label === ladder?.label);
      return found?.stages?.find((s) => s.id === stageId) || null;
    };

    function changed() {
      if (project) remember(project.id, choice);
      draw();
      if (onChange) onChange();
    }

    function rung(ladder, stage) {
      const selected = choice.stage === stage.id;
      const item = node('li', 'tl-rung');
      item.dataset.selected = selected ? 'true' : 'false';
      const button = node('button', 'tl-rung-button');
      button.type = 'button';
      button.setAttribute('role', 'radio');
      button.setAttribute('aria-checked', selected ? 'true' : 'false');
      button.dataset.stage = stage.id;
      button.addEventListener('click', () => { choice.stage = stage.id; changed(); });
      button.addEventListener('keydown', (event) => {
        const order = ladder.stages.map((s) => s.id);
        const at = order.indexOf(stage.id);
        let next = null;
        if (event.key === 'ArrowDown' || event.key === 'ArrowRight') next = order[at + 1];
        if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') next = order[at - 1];
        if (event.key === 'Home') next = order[0];
        if (event.key === 'End') next = order[order.length - 1];
        if (next) {
          event.preventDefault();
          choice.stage = next;
          changed();
          box.querySelector(`[data-stage="${CSS.escape(next)}"]`)?.focus();
        }
      });
      button.tabIndex = selected || (!choice.stage && stage.number === 1) ? 0 : -1;

      const mark = node('span', 'tl-mark', String(stage.number));
      mark.setAttribute('aria-hidden', 'true');
      const body = node('span', 'tl-body');
      const head = node('span', 'tl-head');
      head.append(node('span', 'tl-title', stage.title), node('span', 'tl-id', stage.id));
      body.append(head);
      if (stage.description) body.append(node('span', 'tl-desc', stage.description));
      const facts = node('span', 'tl-facts');
      const fact = (k, v) => {
        const f = node('span', 'tl-fact');
        f.append(node('span', 'tl-k', k), node('span', 'tl-v', v));
        facts.append(f);
      };
      fact('pays', `${stage.success}${stage.reward?.success ? ` · ${pulses(stage.reward.success)}` : ''}`);
      // A training task named by import path shows its class name.
      fact('task', String(stage.task || '').includes(':') ? stage.task.split(':').pop() : stage.task);
      if (stage.params) fact('from', stage.params);
      // What the stage changes: in full on the chosen stage, counted on the
      // others, so the ladder stays readable as a whole.
      const overrides = Object.keys(stage.overrides || {});
      if (overrides.length) {
        fact('changes', selected || overrides.length <= 2
          ? overrides.join(', ')
          : `${overrides.length} settings`);
      }
      body.append(facts);
      if (stage.criterion) {
        body.append(node('span', 'tl-criterion', `Criterion: ${criterionText(stage.criterion)}`));
      }

      // How the chosen subject has done here, and what the criterion says.
      const record = node('span', 'tl-record');
      const seen = stageHistory(ladder, stage.id);
      const mine = subject ? seen?.subjects?.[subject] : null;
      if (!history) {
        record.append(node('span', 'tl-muted', 'reading sessions…'));
      } else if (!subject) {
        record.append(node('span', 'tl-muted',
          seen?.training_sessions ? `${seen.training_sessions} sessions, all subjects` : 'no sessions'));
      } else if (!mine) {
        record.append(node('span', 'tl-muted', `no sessions for sub-${subject}`));
      } else {
        const rate = mine.finished ? mine.successes / mine.finished : null;
        record.append(node('span', 'tl-rate', percent(rate)));
        const meter = node('span', 'tl-meter');
        const fill = node('span', 'tl-fill');
        fill.style.width = `${rate === null ? 0 : Math.round(100 * rate)}%`;
        meter.append(fill);
        record.append(meter);
        record.append(node('span', 'tl-counts',
          `${mine.successes}/${mine.finished} · ${mine.sessions} session${mine.sessions === 1 ? '' : 's'}`));
        // The criterion's own numbers over its window, when it judges others
        // than the success rate (a fixation stage judges completed trials).
        const metrics = Object.entries(mine.recommendation?.metrics || {})
          .filter(([name, value]) => name !== 'success_rate' && value !== null)
          .map(([name, value]) => `${name.replace('_rate', '')} ${name.endsWith('_rate') ? percent(value) : value}`);
        if (metrics.length) record.append(node('span', 'tl-counts', `last ${mine.recommendation.trials}: ${metrics.join(' · ')}`));
        if (mine.recommendation) {
          const [state, words] = VERDICTS[mine.recommendation.verdict] || ['none', mine.recommendation.verdict];
          const lamp = node('span', 'tl-verdict', words);
          lamp.dataset.state = state;
          record.append(lamp);
        }
      }
      button.append(mark, body, record);
      item.append(button);
      return item;
    }

    function draw() {
      box.replaceChildren();
      if (!project) return;
      if (project.ladders_error) {
        box.append(node('p', 'help launch-warning', `A ladder could not be read: ${project.ladders_error}`));
      }
      const all = ladders();
      if (!all.length) {
        box.append(node('p', 'help', 'This experiment’s run.py registers no training ladder '
          + '(LADDERS beside PARAMETERS).'));
        return;
      }
      const ladder = chosenLadder();
      if (all.length > 1) {
        const pick = node('div', 'segmented tl-ladders');
        pick.setAttribute('role', 'group');
        pick.setAttribute('aria-label', 'Training ladder');
        for (const l of all) {
          const b = node('button', '', l.label);
          b.type = 'button';
          b.setAttribute('aria-pressed', l.label === ladder.label ? 'true' : 'false');
          b.addEventListener('click', () => {
            choice = {ladder: l.label, stage: null, rehearse: choice.rehearse};
            changed();
          });
          pick.append(b);
        }
        box.append(pick);
      }
      const intro = node('p', 'help tl-intro');
      intro.textContent = `${ladder.title}${ladder.description ? ` — ${ladder.description}` : ''}`;
      box.append(intro);
      const list = node('ol', 'tl-ladder');
      list.setAttribute('role', 'radiogroup');
      list.setAttribute('aria-label', `${ladder.label}: stages, in order`);
      for (const stage of ladder.stages) list.append(rung(ladder, stage));
      box.append(list);
      const rehearse = node('label', 'check tl-rehearse');
      const tick = node('input');
      tick.type = 'checkbox';
      tick.id = 'training-rehearse';
      tick.checked = !!choice.rehearse;
      tick.addEventListener('change', () => { choice.rehearse = tick.checked; changed(); });
      rehearse.append(tick, node('span', '', ' Rehearse this stage with a simulated monkey '
        + '(simulate mode, no window, filed under the training rehearsal folder)'));
      box.append(rehearse);
      box.append(node('p', 'help tl-note', 'You choose the stage for every session. A stage’s '
        + 'criterion is shown as a recommendation; nothing moves a subject by itself. Each stage '
        + 'pays its own success alone, and its sessions are filed under '
        + '<data folder>-training/<ladder>/<stage>/, apart from the experiment’s data.'));
    }

    return {
      render(next, subjectCode) {
        const switched = next?.id !== project?.id;
        project = next;
        subject = subjectCode || '';
        if (switched) {
          choice = (project ? remembered(project.id) : null)
            || {ladder: null, stage: null, rehearse: false};
          history = null;
        }
        const ladder = chosenLadder();
        if (ladder && !ladder.stages.some((s) => s.id === choice.stage)) choice.stage = null;
        if (ladder) choice.ladder = ladder.label;
        draw();
      },
      setHistory(answer) {
        history = answer;
        draw();
      },
      selection() {
        const ladder = chosenLadder();
        if (!ladder) return null;
        const stage = ladder.stages.find((s) => s.id === choice.stage) || null;
        return {
          ladder: ladder.label,
          ladderName: ladder.name,
          label: ladder.label,
          stage,
          rehearse: !!choice.rehearse,
        };
      },
      problem() {
        if (!ladders().length) return 'This experiment registers no training ladder.';
        if (!this.selection()?.stage) return 'Choose the training stage to run.';
        return '';
      },
    };
  }

  /* The History page's training summary: per ladder, per stage, the sessions
   * run at it with their success rates, and per subject the criterion's
   * recommendation. Null when the experiment has no ladder. */
  function historyPanel(answer, {node, panel}) {
    const ladders = answer?.ladders || [];
    if (!ladders.length) return null;
    const total = ladders.reduce((n, l) => n + (l.stages || [])
      .reduce((m, s) => m + (s.sessions?.length || 0), 0), 0);
    const box = panel('TRAINING LADDERS', 'Training', node('span', 'm-count', String(total)));
    if (answer.error) box.append(node('p', 'help launch-warning', answer.error));
    for (const ladder of ladders) {
      const section = node('section', 'tl-history');
      section.append(node('h4', 'tl-history-title', ladder.title || ladder.label));
      if (ladder.error) {
        section.append(node('p', 'help launch-warning', ladder.error));
        box.append(section);
        continue;
      }
      const list = node('ol', 'tl-history-stages');
      for (const stage of ladder.stages) {
        const item = node('li', 'tl-history-stage');
        const head = node('div', 'tl-history-head');
        head.append(node('span', 'tl-mark', String(stage.number)),
          node('span', 'tl-title', stage.title),
          node('span', 'tl-k', `pays ${stage.success}`),
          node('span', 'tl-counts', stage.training_sessions
            ? `${stage.training_sessions} session${stage.training_sessions === 1 ? '' : 's'} · `
              + `${stage.successes}/${stage.finished} · ${percent(stage.success_rate)}`
            : 'no training sessions'));
        if (stage.rehearsal_sessions) {
          head.append(node('span', 'tl-muted', `+ ${stage.rehearsal_sessions} rehearsal`));
        }
        item.append(head);
        for (const [code, mine] of Object.entries(stage.subjects || {})) {
          if (!mine.recommendation) continue;
          const [state, words] = VERDICTS[mine.recommendation.verdict] || ['none', mine.recommendation.verdict];
          const lamp = node('span', 'tl-verdict', `sub-${code}: ${words}`);
          lamp.dataset.state = state;
          item.append(lamp);
        }
        if (stage.sessions?.length) {
          const wrap = node('div', 'm-table-wrap');
          const table = node('table', 'm-table tl-sessions');
          const head2 = node('tr');
          for (const c of ['Date', 'Subject', 'Session', 'Kind', 'Finished', 'Success']) {
            head2.append(node('th', '', c));
          }
          table.append(head2);
          for (const s of [...stage.sessions].reverse()) {
            const row = node('tr');
            row.dataset.kind = s.kind;
            row.append(node('td', 'tl-mono', s.date || ''),
              node('td', '', `sub-${s.subject}`),
              node('td', 'tl-mono', `ses-${String(s.session).padStart(3, '0')} run-${String(s.run).padStart(2, '0')}`),
              node('td', '', s.kind === 'rehearsal' ? 'rehearsal' : 'training'),
              node('td', 'tl-mono', `${s.successes}/${s.finished}`));
            const cell = node('td', 'tl-success');
            const inner = node('span', 'tl-success-inner');
            const meter = node('span', 'tl-meter');
            const fill = node('span', 'tl-fill');
            fill.style.width = `${s.success_rate === null ? 0 : Math.round(100 * s.success_rate)}%`;
            meter.append(fill);
            inner.append(node('span', 'tl-rate', percent(s.success_rate)), meter);
            cell.append(inner);
            row.append(cell);
            table.append(row);
          }
          wrap.append(table);
          item.append(wrap);
        }
        list.append(item);
      }
      section.append(list);
      box.append(section);
    }
    return box;
  }

  return {mount, historyPanel, criterionText, pulses};
})();

if (typeof window !== 'undefined') window.TrainingLadder = TrainingLadder;
