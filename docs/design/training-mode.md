# Training mode: a monkey's ladder to the experiment

Status: implemented on `feature/training-mode-494977` (local), for the
release after 2.12.0. Requested 2026-10-08: "a monkey must be trained up to
the final task through a ladder of smaller tasks … alhazen should expose
another mode for training, and each experiment implements its own smaller
training tasks."

## 1. What it is

A **ladder** is the ordered stages an animal climbs toward an experiment's
final task. Each **stage** is a small task variant — a fixation hold, a
saccade to a single dot, the dot moving a little, … the experiment's own
trial — with its own parameters and its own **success outcome**, the only
outcome it pays. A **training session** runs exactly one stage, the one the
operator chose.

```
python run.py --mode training --stage saccade --rig lab --sub m01 --ses 4 --initials MK
python run.py --mode simulate --stage saccade --headless      # rehearse it
```

## 2. Decisions

| Question | Decision | Why |
|---|---|---|
| A new mode or a flag? | `Mode.TRAINING`, plus `--stage` on test and simulate to rehearse a stage | The operator thinks "today we train", not "run with a flag"; rehearsals stay one code path with the real thing, as simulate/test/run are. |
| Devices | Driven exactly as run mode drives them (`Mode.drives_subject`); a development rig (`real_data: false`) is refused | A monkey is in the chair and juice is paid. |
| Who | The stage's params must declare `subject_kind: monkey`; anything else is refused when the stage resolves | Training is a monkey's; a human session never pays (2.12's rule). |
| What pays | The stage's reward policy is rebuilt to pay the stage's `success` alone (delivery from the stage, else the file's entry for that outcome, else its first), plus `on_fault` (the file's unless the stage says) | An intermediate stage that also paid the final success would train the wrong thing. |
| Where data goes | `<data_root>-training/<ladder>/<stage>/` (rehearsals: `<data_root>-training-rehearsal/…`), the experiment's layout inside | Never mixed with experiment sessions; a sibling, like the rehearsal root, so analysis never finds it by accident, and every run reader still reads it. |
| Record | session.json `mode: training` and `training: {ladder, stage, stage_number, stage_count, task, params_file, overrides, success, reward, criterion}`; rows `training_ladder`, `training_stage`; a setup line in session.log | The data say which rung they came from without a lab notebook. |
| Advancement | The operator's, every session. A stage may declare a `criterion` (`StageCriteria`: window, min_trials, promote_when/demote_when); it is evaluated over the subject's latest training trials at that stage and **shown** (advance / stay / go back / too few trials), never acted on | The request: "advancement is the operator's decision". No state file: nothing remembers a stage for a subject. |
| Where a ladder lives | A YAML file per ladder (`configs/training-<name>.yaml`), registered in run.py's `LADDERS` dict literal beside `PARAMETERS` | Data, reviewable, readable by the workspace without running run.py (like `PARAMETERS`). |
| Stage params | `task` (one of TASKS, or a training task class by import path `package.module:Class`) + `params` file (relative to the ladder) + dotted `overrides`, through `training.stages.apply_stage` | Reuses the curriculum's override path, so a stage that asks for something the task cannot express fails at load naming the stage and the path. |
| A training task in TASKS? | No: named by import path from the ladder. A training task's params model is usually the experiment's plus a `training` block, so it accepts every experiment file — an experiment's tooling that asks "which task is this file for?" (amodal-averaging's) would find two answers. Off TASKS it is also off every menu. | |
| Relation to `Curriculum` | Separate. A curriculum moves a subject between stages of one task automatically within a session; a ladder's stages may be different tasks and the move is a person's. Both remain. | |

## 3. Pieces

- `alhazen.training.ladder`: `Ladder`, `LadderStage`, `StageReward`,
  `load_ladder`, `find_ladder` (label, name or path), `resolve_stage`
  (→ `ResolvedStage`: task class, validated params, record), `training_root`.
- `alhazen.training.history`: `stage_tally` (finished trials, successes;
  paused and fault-lost trials not counted), `recommendation`,
  `ladder_history` (per stage: sessions with success rates, per subject the
  criterion's verdict), read from the run folders only.
- `Mode.TRAINING`, `Mode.drives_subject`; `build_mode_session(training=…)`;
  CLI `--ladder`, `--stage` (`--params` refused beside a stage; the stage
  names the task, so `--task` may be left out); `--estimate-duration` reads
  the stage; capability `training-mode`.
- Live monitor: the header names the stage ("training stage 2/5 Saccade to
  the dot · LANDED 42/50 (84%)").
- Workspace: Mode menu entry Training (only for a project whose run.py
  registers a ladder and whose alhazen can run one); a Training stage panel
  in Task parameters' place (the ladder drawn as a ladder — numbered marks
  on one rail, the chosen stage lit — each stage with what it pays, its
  overrides, its criterion, and the chosen subject's sessions, finished
  trials, success rate and recommendation); a Rehearse switch (simulate,
  headless, training rehearsal folder); the duration estimate; History's
  Training summary (per stage, every session's success rate); each stage
  folder a data root on the Data page. `GET /api/training`.

## 4. A ladder file

```yaml
name: pursuit                       # folder name: lowercase, digits, hyphens
title: Pursuit training
task: kde_vergence.tasks.training:KdePursuitTrainingTask  # by import path
params: task-pursuit-monkey.yaml    # default params file, beside this file
stages:
  - id: saccade
    title: Saccade to the red dot
    description: The dot alone, still; land on it.
    overrides: {training.background: none, training.pursuit_fraction: 0.0}
    success: LANDED
    reward: {success: {n_pulses: 1, pulse_ms: 200, inter_pulse_ms: 200}}
    criterion: {window: 100, min_trials: 50, promote_when: {success_rate: 0.8}}
  - id: final
    title: The real pursuit trial
    task: kde-vergence-pursuit
    success: PURSUED
```

## 5. Not in this change

Automatic promotion; per-subject remembered stage; ramps inside a stage
(a curriculum does that, and can still be used with `--curriculum`); a
stage of a task that needs a probe or a report.
