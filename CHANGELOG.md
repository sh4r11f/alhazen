# Changelog

Notable changes, newest first. This project follows [semantic
versioning](https://semver.org): the public API is everything exported from
`alhazen` and the names the API reference lists module by module. Three things are
also compatibility contracts, because they live on disk and outlast any one
version:

- **`core.rng.STREAMS`** is append-only. Removing or reordering a stream
  changes what every seed produces.
- **`RESERVED_EVENTS`** may gain names, never lose them; an analysis reads
  them out of old data.
- **The run-directory layout** (file names, column meanings, the manifest and
  snapshot formats) changes only in a major version, with a documented
  migration.

All three are pinned by `tests/unit/test_contracts.py` against a recorded
baseline, so breaking one fails the suite rather than a stranger's analysis a
year from now. The full policy — what a bump means, how a release is cut, and
what every on-disk schema version is — is in
[docs/versioning.md](docs/versioning.md).

Each section below is `## <version> - <YYYY-MM-DD>`, newest first, and the
newest one always matches `version` in `pyproject.toml`. `Unreleased` collects
changes that have landed on `main` but not shipped; cutting a release renames
it to the new version. `scripts/release_check.py` enforces all of that.

## Unreleased

### Added

- **`build_session(clock=...)` takes the session clock.** The builder made
  its own `MonotonicClock` with no way to pass one in, so a test (or an
  example's stand-in subject) that built its own tracker had to give it a
  second, unrelated clock, and every phase of a built session was timed by
  the host's real clock: on a loaded machine a 3-frame stimulus could end
  after one frame (#62). Now a caller creates the clock and hands the same
  one to its tracker and to `build_session`; unset, it is a `MonotonicClock`
  as before. A clock that moves only when told to (`alhazen.testing.FakeClock`)
  runs the session in simulated time: the simulated display advances it one
  frame per flip (`SimulatedDisplay(advance=...)`) and the runner's waits
  advance it instead of sleeping, so every recorded time is exact and
  repeatable. Such a clock on a real display is refused with a `ConfigError`,
  since nothing there would advance it. The scene-example and
  shaping-curriculum tests now run on one fake clock.

- **A task says what its subject reads: `Task.instructions()`.** It returns
  the text shown before trial one, or `None` to declare that the task has
  none (an animal subject); declared on a shared base class, it covers every
  task under it. Every way of starting a session shows it — `alhazen run
  --task`, an experiment's `run.py` through `run_experiment`,
  `build_mode_session` and `build_session(task=...)` — because the task is
  the one thing all of them are handed. `run` and `test` wait on it for
  SPACE; `simulate` shows it and starts by itself two seconds later (at once
  with `--headless`), as it did for a `run.py` that passed its wording. It is
  asked once, after a curriculum has set the stage's params and before the
  run directory exists; text that is not a string, or is empty, is refused
  there. `instructions=` given to `run_experiment`, `build_mode_session` or
  `build_session` still works and **takes precedence** over the task's;
  `instructions=""` turns the screen off. A task that does not override the
  method behaves exactly as before — except that `--mode run` now logs a
  WARNING naming it, and prints and records `instructions: none — …` before
  trial one, so a task that forgot is not mistaken for one that decided. See
  "What the subject reads first" in [docs/architecture.md](docs/architecture.md)
  §5.1 and [docs/modes.md](docs/modes.md).
- **A task names its own params file: `Task.default_params()`**, a
  classmethod returning a path — absolute, or relative to the file the method
  is written in. Every entry point loads it when `--params` is not given, in
  every mode. A declared file that is not there stops the session with
  `INVALID`, naming the path; the params model's defaults are never run in
  its place. `--params` takes precedence, and so does
  `run_experiment(default_params=...)` for the sessions a `run.py` starts. A
  task that declares no file runs its model's defaults, as before. The
  snapshot's `sources.task` names the file that was loaded, including the
  task's own.
- **A task derives params from the invocation: `Task.params_hook(params,
  args)`**, a classmethod with the same contract as `run_experiment`'s
  `params_hook=`: it runs between loading the params and constructing the
  task, its result is re-validated through `params_model`, and it is not
  called for a task that does not override it. `alhazen run --task` applies
  it, so a task whose scheduler needs the subject and session (an adaptive
  search carrying state across sessions) now starts from `alhazen run`.
  `run_experiment(params_hook=...)`, when given, **replaces** the task's hook
  for that `run.py`; the two are not chained. `default_params` or
  `params_hook` written as an ordinary method or as a value is refused when
  the class is defined.
- **Sessions say where their params came from before trial one**: `params:
  <file>`, or `params: the defaults of <Model> — no --params given, and <Task>
  declares no default_params()`.
- **`alhazen new` scaffolds a task that names its params file and its
  instructions.** The template task's `default_params()` returns
  `configs/task.yaml`, found from the task's own file (so the package is
  installed editable, as its README says), and its `instructions()` returns
  the subject's wording; its `run.py` passes only the task and a default
  rig. Its tests gain two: the named params file is there and loads, and the
  task tells its subject what to do.

- **Every trial row names the system fault that hit it, in a new `fault`
  column.** Two failures are the rig's, never the subject's: the display
  dropping frames (frame QA's `recycle_trial` turned the trial into
  `DROPPED_FRAMES`: `fault` is `dropped_frames`), and the eye tracker
  stopping recording (the tracker health check fired: `fault` is
  `tracker_stopped`). Every other row says `none` — a value, never an empty
  cell, so a clean trial can be selected on (`trials.fault != "none"`). The
  experimenter's skip is not a fault. The trials table lists `fault` among
  its leading columns, right after `abort_reason`, and the trial-column
  baseline in `tests/fixtures/contracts.json` gains it; nothing was removed
  or renamed. See [docs/architecture.md](docs/architecture.md) §2.2.
- **`lost_to_fault(outcome_name, record)`** in `alhazen.core`, and
  `TrialResult.lost_to_fault`: the system fault a trial's measurement was
  lost to, or None. A row names the fault that hit its trial; this says
  whether the fault cost the trial its outcome (`DROPPED_FRAMES`, or
  `ABORTED` by the health check — not the skip). It reads the row alone, so
  the same rule applies to trials.csv offline. `NO_FAULT`,
  `FAULT_DROPPED_FRAMES` and `FAULT_TRACKER_STOPPED` name the column's
  values.
- **A trial lost to a system fault is paid, and logged.** Both kinds are
  served again, as before. A dropped-frames trial is paid for the subject's
  response, as in 1.5.0. A tracker-stopped trial, usually cut off before
  any response, is paid the task's new **`RewardPolicy.on_fault`**
  (`RewardPulses`, scaled by `scale` like every delivery), and its REWARD or
  REWARD_FAILED payload carries `fault` beside `outcome`. `on_fault`
  defaults to `None`, which pays nothing, so a task that does not set it is
  paid as before. Each lost trial gets one WARNING in `session.log` naming
  the trial, the cause, what the subject was paid (or that the task sets no
  `on_fault`), and that it will be served again. A mid-trial drop delivered
  before the fault stays delivered and counted, and the line says how many.
  The experimenter's skip and a pause are never paid `on_fault`. See "System
  faults" in [docs/architecture.md](docs/architecture.md) §5.3.
- **A failed health check says what the device said, and the row keeps it
  in a new `fault_detail` column.** A device health check may now return
  **`HealthFault(reason, detail)`** (`alhazen.core`) rather than a bare
  reason: `TrialEngine` writes the reason as `fault` (and `abort_reason`) as
  before, and the detail — the device's own account, in words — as
  `fault_detail`, right after `fault` in the trials table. The fault's
  WARNING line in `session.log`, and the engine's line for a stop during the
  closing phase, carry the same words. It is only on a row whose fault a
  health check reported with a detail: a dropped-frames row keeps its account
  in `frame_qa_reason`, and when frame QA recycles a trial whose closing
  phase flagged a tracker stop, the detail leaves with the flag. Free text
  for a person; select on `fault`. A check that returns a bare reason string
  still works. The trial-column baseline in `tests/fixtures/contracts.json`
  gains `fault_detail`; nothing was removed or renamed.
- **Two rig fields for dropout detection** (`devices.eyetracker`):
  `max_sample_gap_ms` — how long the tracker may go without a new sample
  mid-trial before its recording is called dead, 50 ms on an EyeLink and
  100 ms on a TRACKPixx3 by default, written into the config as it loads so
  the snapshot records it, and refused below 20 ms (EyeLink: five samples at
  its slowest rate, 250 Hz) or 50 ms (TRACKPixx3: one slow USB read) and above
  1000 ms; and `max_consecutive_dropouts` (3). Both are refused on
  `mouse_sim`, which streams nothing that could stop.
- **A run of dropouts pauses the session.** Every tracker-stopped trial is
  served again, so a tracker that dies on every recording would loop the
  session on the same trial, paying the fault reward each time. After
  `max_consecutive_dropouts` in a row the pause screen, in red, leads with
  `THE EYE TRACKER DROPPED OUT ON 3 TRIALS IN A ROW — last: <what it said>`,
  and a WARNING says the same. A trial the tracker records through ends the
  streak; a pause neither counts nor ends it; the count starts over after its
  pause. `SessionRunner(max_consecutive_dropouts=...)`, default 3, None never
  pauses.
- **`alhazen check-rig` exercises dropout detection on the tracker itself.**
  After connecting a real EyeLink or TRACKPixx3 it records for a second
  while polling the session's own health check (nothing may be reported),
  stops the recording through the SDK behind the session's back (the
  EyeLink's `stopRecording()`, the TRACKPixx3's `TPxDisableFreeRun()`), and
  times the report: OK within the limit plus 50 ms, FAIL if it is late,
  never comes, or the check fired on normal recording. `--record` keeps the
  limit, the longest gap between samples seen while recording normally, the
  check's measured per-frame cost, the latency and the tracker's words under
  the eye tracker's `dropout` key, and the summary prints them. The
  TRACKPixx3's `shutdown(None)` now removes the test's scratch recording
  rather than leaving it in the temp folder. A manual cable-pull test and a
  rig verification checklist are in [docs/eye-tracker.md](docs/eye-tracker.md),
  "When the tracker drops out mid-trial".

- **`REWARD_CANCELLED`, a reserved event, and `n_mid_trial_rewards_cancelled`,
  a trial column**, for a task that declares `mid_trial_reward`. A drop that
  was commanded (its `REWARD` is in the record) but never delivered, because
  the experimenter's manual reward overrode the queue before it reached the
  valve, ends with `REWARD_CANCELLED` carrying its `{pulses, reason, frame}`
  and `cancelled_by: "manual"` — its own event, never a `REWARD_FAILED`, so
  it takes no pump-failure pause. Rows gain the count, 0 on a trial with
  none, so delivered (`n_mid_trial_rewards`), failed and cancelled add up to
  every drop commanded. The manual path is the new
  `QueuedReward.deliver_manual(pulses)`. `RESERVED_EVENTS` and the
  trial-column baseline in `tests/fixtures/contracts.json` gain the two
  names; nothing was removed or renamed.

### Changed

- **The public API is now exactly the names `docs/reference.md` lists.**
  Most of its entries used to document a whole module, and the policy called
  "everything in the modules on this page" public — so dashboard formatting
  helpers (`format_number`, `present`), the dashboard server's `page_html`,
  every constant of the TRACKPixx3 backend and the session's eye-tracker
  monitor were all contract, and refactoring any of them was technically a
  major bump. Every entry now carries an explicit `members:` list, drawn from
  what the experiments built on alhazen import, what `examples/`, the
  `alhazen new` template and the docs' snippets use, and what the guides tell
  a task author to call; any other name is internal. **Nothing was removed
  from the code**: every name stays importable. Some modules left the page
  entirely (among them `session.eyetracker`, `dashboard.runtime`,
  `display.monitors`, `devices.eyetracker.viewpixx`, `scenes.expr`,
  `data.naming`). Names experiments already import but the page never
  listed are now listed, and so public: `session.builder.validate_event_names`,
  the mode hooks (`alhazen.modes.*`, `cli.modes.run_experiment`), the
  analysis readers (`analysis.io.spikeglx`, `.kilosort`, `.eyelink`,
  `.viewpixx`), `display.psychopy_backend.PsychoPyDisplay`,
  `devices.spikes.SimulatedSpikeSource`, `neural.rfmap` and the feedback
  colours in `task.phases.simple`. A test fails when a listed name no longer
  exists or an entry has no explicit list.

- **A deprecated name is removed only in the next major version.**
  [docs/versioning.md](docs/versioning.md) said so in §1 (MAJOR means
  something that used to work no longer does) and the opposite in §4 and the
  API reference (one minor version of warning, then removal); §4, the
  reference and the architecture notes now agree with §1. `pause_menu` is
  therefore removed in 2.0, not 1.2, and keeps working until then. Its warning
  said "will be removed in 1.2" on every call from 1.2 through 1.5; it now
  names 2.0 and the replacements by import path,
  `alhazen.session.build_pause_menu` with `alhazen.session.run_pause_menu`,
  which the API reference now documents along with `PauseMenu`.
  `tests/unit/test_versioning.py` fails if any deprecation's `removed_in` is
  not a major version or has already been reached by `pyproject.toml`'s.

- **A queue-based paradigm's `trials_per_block` may no longer be smaller
  than one block's plan; such a config is now refused.** For `sequence`,
  `constant` and `adjustment`, every block gets its own full plan (cells ×
  `n_per_condition`), and `trials_per_block` ended the block by count. Set
  below the plan, it abandoned whatever was still queued, without a word —
  and a failed trial re-queues at the end, so the retries were cut first:
  with two cells, three presentations each and a bound of 4, one cell could
  finish a block with 1 completed trial and the other with 3. The session
  builder now raises a `ConfigError` before trial one, naming the task,
  `trials_per_block`, `n_per_condition` and how many planned trials each
  block would have dropped. A config that ran before this change can be
  refused by it; to serve the same trials, omit `trials_per_block` (a block
  ends when its plan is done) or lower `n_per_condition` so that one block's
  plan is the block length wanted. A bound equal to or above the plan is
  accepted — it never cut anything, since a block's completed count reaches
  its plan exactly as its queue empties — and the adaptive kinds
  (`staircase`, `questplus`), whose block length `trials_per_block` is, are
  unchanged.

- **Training criteria leave out a trial lost to a system fault.** A
  dropped-frames trial and a tracker-stopped trial no longer enter the
  criteria window: no metric, no `min_trials` count and no ramp sees them,
  as a paused trial was already left out. Counted, a display dropping frames
  pulled `completed_rate` down and could demote a subject for the rig's
  failure.
- **A trial the eye tracker cut short neither counts toward a failure streak
  nor ends one** (`max_consecutive_failures`), as a pause does not. It used
  to count, so a tracker dropping out between fixation breaks could send the
  operator to recalibrate the subject. A dropped-frames trial still ends the
  streak, as in 1.5.0, and the experimenter's skip still counts.
- **`by_outcome` is not consulted for a tracker-stopped trial.** Its
  `ABORTED` is the rig's, so it pays `on_fault` or nothing; an `ABORTED`
  entry in `by_outcome` now pays the experimenter's skip only.
- **`session.builder.make_tracker_health_check` returns a `HealthFault`**
  (reason `tracker_stopped`, and a detail) rather than the bare string
  `"tracker_stopped"`. Code comparing its result with that string should
  read `.reason`.
- **After a dropout, the rest of the trial no longer raises over it.** On the
  EyeLink and TRACKPixx3 backends, a device that refuses the stop at the
  trial's end, or the messages that follow the dropout (the trial's
  `TRIAL_END`, the fault reward's `REWARD`), is logged — WARNING, or ERROR for
  TRACKPixx3 samples that could not be drained — instead of raising in the
  runner's `finally` and losing the trial's row. Without a dropout, the same
  failures raise as before. A TRACKPixx3 message the device could not stamp
  is left out of `<base>_gaze-messages.csv` rather than written without a
  device time.
- **A tracker that is gone fails the next trial's start in the rig's
  words.** The EyeLink's `start_trial` raises a `TrackerError` naming the
  Host PC's address — never pylink's bare `RuntimeError` — and, after a
  dropout, what the previous trial's recording died of. The TRACKPixx3's
  `start_trial` now checks the device before a trial relies on it: it
  restarts a gaze reader that stopped once one read proves the device answers
  again, re-arms a recording it finds switched off (with a warning), and
  raises `the TRACKPixx3 is not answering at trial N` when the device does
  not answer.
- **A stuck TRACKPixx3 device call fails loudly instead of hanging the
  session.** Messages and drains wait at most 2 s for the device lock that a
  USB call to a vanished device can hold forever, then raise a `TrackerError`
  saying a device call is stuck. `get_gaze()` stops treating a report as a
  position at 100 ms, or at `max_sample_gap_ms` plus 5 ms when that is
  later, so a stalled reader is always called a dropout before it can read as
  a broken fixation.

- **The manual reward overrides the mid-trial reward queue.** In a session
  whose task declares `mid_trial_reward`, the experimenter's reward — `r`
  during a trial, R in the pause menu, the dashboard's Give reward — waited
  behind every drop still queued, holding the frame loop for all of their
  pulse trains and arriving late. It now cancels every drop still waiting
  (each ends with its own `REWARD_CANCELLED`, and one WARNING in the log
  names them), lets the train already on the valve finish, and is delivered
  once, next. Drops asked for after it queue as usual. A train on the valve
  is never cut short: a partial train is a dose nobody measured, and an
  NI-DAQ output task stopped mid-pulse leaves the valve line high. The key is
  still synchronous and its `REWARD {manual: true}` still follows the pump,
  but it now blocks for at most the train on the valve plus its own, and the
  cancellations are reported ahead of it, in the same frame. The
  end-of-trial pay is never cancelled, and a cancelled drop still counts as
  earned, so its trial gets no `NO_REWARD`. Nothing changes for a task that
  does not declare `mid_trial_reward`. See "Mid-trial reward" in
  [docs/architecture.md](docs/architecture.md) §5.3.

### Fixed

- **The offline readers name the file when its contents are wrong, and a
  results directory says what it already held.** In the ViewPixx reader, a
  run snapshot whose `config.rig.monitor` the monitor model refused raised a
  pydantic `ValidationError`, and a `TRIAL` mark with no integer index a raw
  `ValueError`/`IndexError`; both are now a `DataError` naming the file (and
  the mark), and malformed trial marks are refused when the messages file is
  read. `max_residual_s=0.0` was treated as unset and replaced by the sample
  period; it is now the tolerance used. In the SpikeGLX reader, a `.meta`
  that is not UTF-8 (a Windows path in the local code page) raised
  `UnicodeDecodeError`; it is now read with undecodable bytes replaced and a
  warning naming the affected keys — safe because every field read as a
  number is ASCII. A non-numeric or non-positive `nSavedChans` is a
  `DataError` naming the file and field (it was a `ValueError` or
  `ZeroDivisionError`), and a binary whose size differs from the meta's
  `fileSizeBytes` is refused as truncated, which catches a copy cut exactly
  at a frame boundary. `ResultsBundle` hashes inputs with the run manifest's
  own function, and reusing an `out_dir` that already holds files logs a
  warning listing them and records every one the bundle did not rewrite
  under a new `preexisting` key in `manifest.json`, so an earlier run's
  leftovers cannot pass for this run's outputs; reuse itself is still
  allowed, since reports are routinely re-run into the same directory.

- **A scene expression that failed on a frame raised a bare Python error
  naming nothing.** Only function calls turned their failures into
  `ConfigError`. An operator meeting the wrong value (`params.x - 1` with a
  string param, `10 ** 400`) raised a raw `TypeError` or `OverflowError`
  mid-frame, with no word of which scene field or expression did it, and a
  malformed number literal (`1.2.3`) escaped the tokenizer as a `ValueError`
  that the loader's check did not catch. The `background` expression was
  never checked at load at all. Now a malformed number, like a syntax error
  or an unknown name, fails at `load_scene` naming the field (the background
  included); an operator error on a frame is a `ConfigError` naming the
  layer path, the scene time, the expression, the operator and the values;
  and a non-numeric result in a numeric field says so. Dividing by zero is
  unchanged (infinity, as in the studio). See "What alhazen renders" in
  [docs/scenes.md](docs/scenes.md).

- **A session that failed while starting up left every device open.**
  `SessionRunner.run` wrote the snapshot, registered the subject, attached
  `session.log` and made the first dashboard publish before the `try` whose
  `finally` tears the session down. By then the window, the eye tracker, the
  reward and sync devices and the dashboard's process were all open, so a
  failure in any of those steps (a `session.log` that could not be opened, a
  `participants.tsv` another program held, a dashboard that could not
  publish) left them all held and wrote no "session end" line. Every setup
  step now runs inside that `try`. `session.log` is attached before the
  subject is registered, so a registry failure is in the run's own log. A
  session whose config snapshot cannot be written never started: its devices
  are released, the tracker without a destination for its recording, and
  nothing is written into its run directory, which a run without a snapshot
  could not be analysed from. The original error is raised either way. See
  [docs/architecture.md](docs/architecture.md) §10.

- **A session build that failed part-way left the window, the tracker link
  and the sync lines open.** `build_session`'s failure path released only the
  dashboard, the mid-trial reward worker and the spike source. A failure once
  the devices were up (a scheduler that raised, a refresh rate that disagreed
  with the config) left the window open, the eye tracker connected and
  `NidaqSync`'s NI-DAQ tasks reserved, which blocks the next session, and a
  dispenser not wrapped for mid-trial reward was never closed. The display was
  also constructed before the guard began, so a failure there left the
  dashboard's child process running. And the dashboard's `stop()` was the one
  unguarded release: if it raised, the reward worker and spike source were
  skipped and its error replaced the build's. Now every resource the build
  acquires registers its release as soon as it is held. A failure, or a
  Ctrl-C, releases them all in reverse order, each attempted even when
  another fails. A release that fails is logged with its traceback, and the
  build's own error is the one raised. The tracker is released with
  `shutdown(None)` once it has connected. Devices handed in (`tracker=`,
  `reward=`, `sync=`, `spikes=`) are released like the rig's own, as the
  runner's teardown already did. See
  [docs/architecture.md](docs/architecture.md) §9.

- **`experiment_git_sha` says `-dirty` when the experiment ran from
  uncommitted code.** It was `git rev-parse --short HEAD`, which names the
  commit and nothing else, so a session run from edited experiment code
  recorded a clean-looking SHA whose checkout does not reproduce it. The
  experiment's tree is now read exactly as `alhazen_git_describe` reads
  alhazen's own — `git describe --always --dirty`, through one shared
  function — so the value is `abc1234-dirty` for uncommitted changes to
  tracked files. The key keeps its name. In a repository with no annotated
  tag (every downstream experiment repo today) a clean tree records the
  same short SHA as before; a tagged one records a describe string such as
  `v2.0-3-gabc1234`, which git accepts as a revision. Every failure used to
  read `unknown`; a directory outside any git repository now reads `not a
  source checkout`, and `unknown` is kept for git being absent or not
  answering, as in `alhazen_git_describe`. Git's output is now decoded as
  UTF-8, which it is: in the Windows code page, a folder name such as `Ída`
  made reading alhazen's own tree crash, and would have done the same to
  the experiment's.

- **An EyeLink whose link was down at teardown left its run marked
  complete.** `EyeLinkTracker.shutdown()` logged a WARNING and returned, so
  nothing machine-readable said the EDF never left the Host PC: the database
  mirrored the run as `complete`, and the link was never closed. A failure
  partway through (stopping the trial, closing the EDF, the transfer) also
  skipped `close()`. With a run behind it, a link that is down, or dies
  before the transfer, now raises a `TrackerError` naming the EDF on the
  Host PC, where to copy it, and why before the next session (which opens
  its EDF under the same name); the run is recorded as `failed`. With no
  destination (`check-rig`, the accuracy measurement) a dead link loses
  nothing and is still only logged. `close()` now runs in a `finally`
  whatever failed; if it fails too, the first error is raised and close()'s
  is logged. See [docs/eye-tracker.md](docs/eye-tracker.md).

- **Events shown by one flip were stamped later than the flip, and with
  different times.** The engine read the flip's time right after `flip()`,
  then stamped each event a phase had queued with `emit_on_flip` — and each
  mid-trial `REWARD` — with the clock read again as it was emitted: after
  the frame's own bookkeeping and, for every event but the frame's first,
  after the bus's subscribers (the tracker's message, the sync pulse) had
  handled the one before it. So `events.csv` and the row's `t_<event>`
  columns ran late by that time, two events on one flip got two times, and a
  reaction time a phase measured from `t_<onset_event>` came out short by
  the same amount. Every event queued on a flip, and every mid-trial
  `REWARD`, now carries that flip's own time — the time `frames.csv` and the
  database's `frames` table record for it — and the engine reads its flips
  on its own clock, never the trial context's. Events with no flip
  (`TRIAL_START`, `TRIAL_END`, `PAUSED`, a manual `REWARD`, a drop's end) are
  stamped as they are emitted, as before. **In data recorded before this
  fix** a flip-locked event's `t` is later than its flip by the subscribers'
  run time. Its flip is the last one of its trial at or before that `t`: the
  database's `frames` table has every flip (`t_session`, by `trial_index` and
  `frame_index`), while `frames.csv` leaves out each trial's first. A
  mid-trial `REWARD` names its flip outright, as `frame`. See
  [docs/architecture.md](docs/architecture.md) §2.

- **A key pressed before the response cue was on screen was scored as the
  answer.** `ResponseWindow` accepted bound keys from its first frame, whose
  keys are read before the flip that shows its cue (`RESPONSE_CUE` by
  default), and timed such a key from the phase's entry because the cue had no
  flip time yet. So a key pressed in the frame before — the end of the
  stimulus phase, in `examples/staircase_detection` — decided the trial (and,
  in a staircase, moved it) with a reaction time of about zero. The second
  frame did the same one frame later: its keys were pressed while the cue
  waited for its flip. Keys now count only once the frame before has already
  seen the cue's `t_<onset_event>` stamp, so every key in the batch followed
  the cue on screen; earlier presses are ignored, the way `StimulusResponse`
  waits for its own onset stamp. A window built with `onset_event=None` is
  unchanged: keys count from its first frame, timed from phase entry. Runs
  recorded before this fix may hold such trials, `SubjectMode.KEYBOARD` ones
  included, with the pre-cue key as their response. `trials.csv` shows them:
  their `rt_ms` (or the window's `rt_record_key`) is under one frame period
  (16.7 ms at 60 Hz) — only the engine's own work between two clock reads —
  where a key read once the cue had been up a full frame scores at least about
  one frame period. See [docs/architecture.md](docs/architecture.md) §5.2.

- **A task's `score_trial` did not reach an up-down staircase.**
  `Task.score_trial` is how a task titrating something other than accuracy (a
  bias magnitude, a settling error) says what a success is, but
  `make_scheduler` handed it to `kind: questplus` only. `kind: staircase`,
  single or interleaved, stepped on `outcome.success` whatever the task said,
  so a task that overrode the hook titrated accuracy instead, with nothing to
  say so. `UpDownStaircase` now takes a `score` callable, as `QuestPlus` does,
  and `make_scheduler` builds every staircase with the task's, blocks or
  not. The scorer is asked about completed trials only; an attempt with no
  measurement is still served again at the same level and never scored. A
  task that does not override `score_trial` runs exactly the session it ran
  before, seed for seed. See [docs/architecture.md](docs/architecture.md)
  §5.4.

- **The same run number on a later day wrote into the earlier run's
  folder.** `SessionPaths.create` refused only this run's own trials file,
  whose name carries the date; the folder's name does not. So `--run 1` on
  Tuesday, after `--run 1` on Monday, passed the check and wrote into
  Monday's folder: over its `config_snapshot.yaml`, `manifest.yaml` and
  dashboard, appending to its `session.log`, and `load_run` then paired one
  day's trials with the other day's snapshot. A run folder that holds any
  file — a finished run, or one that crashed before writing its trials file —
  is now refused on any day, naming a few of the files; the fix is the next
  run number. An empty folder left by a build that failed before the session
  began can still be used. The CLI's automatic run numbering never reused a
  folder, so only an explicit run number was affected.

- **Saving a report or an alignment hid damage to the run.**
  `SessionReport.save` and `AlignmentFit.save` re-hashed the whole run
  directory into `manifest.yaml`. On a run with a file changed since the
  session, the report said "hash mismatch" once, then recorded the changed
  file's hash as the session's — and every report and `load_run` after it
  said "verified". Both now call the new `alhazen.data.add_to_manifest`,
  which adds or replaces the entries of the files it is given and leaves
  every other entry as the session recorded it. A run with no manifest (its
  session never finished teardown) is not given one after the fact: the file
  is still written, and a warning says it went unrecorded. `write_manifest`
  is unchanged, and stays the session's teardown step. See
  [docs/architecture.md](docs/architecture.md) §7.4.

- **A failure building the final dashboard skipped the rest of teardown.**
  The end-of-session dashboard publish was the one bare call in the runner's
  teardown. Building that state asks the eye tracker and the live analysis for
  their panels, so a device that died mid-session could raise there — and
  every later step was skipped: no tracker recording retrieved, no manifest,
  the reward device and the window left open. It is now a teardown step like
  the others (`dashboard.publish`), and so is the end-of-session log line
  (`log.session_end`). The error is still raised once teardown is done; no
  dashboard is saved when its final state could not be built.

- **A TRACKPixx3 that stopped answering at the end of a session stranded its
  recording.** `shutdown()` ran its last drain before the delivery's `try`,
  so when the device did not answer — exactly when a session ends early —
  the samples of every earlier trial stayed in the temp folder and the
  message record (held only in memory) was never written. The last drain's
  failure is now held while the recording and messages are delivered, and
  raised after as a `TrackerError` saying which samples are missing. If the
  delivery fails too, its error is the one raised and the drain's is logged.

- **A training state that could not be read was written over.** When
  `training_state.yaml` was unreadable (a typo made while editing it by hand,
  a disk problem), the session started the subject at the first stage — as
  documented, and loudly — and then its teardown saved that first stage over
  the file, destroying the only record of where the subject really was. The
  file the load failed on is now renamed to
  `training_state.unreadable-<UTC time>.yaml` before the session's own state
  is written, and the warning names it; a name already taken is never
  replaced. The save itself is now atomic (a temporary file, flushed to disk,
  then renamed over the old one), so a crash or a full disk mid-write leaves
  the previous state whole instead of a truncated file. Bytes that are not
  UTF-8 are treated as unreadable too, instead of escaping as a crash.

- **The real eye trackers never noticed a recording that died mid-trial.**
  The EyeLink's and the TRACKPixx3's `is_recording()` returned a flag they
  set at `start_trial` and cleared at `stop_trial`, so a pulled cable, a Host
  PC that stopped recording or a DATAPixx3 that lost power went unnoticed:
  the tracker-stopped handling above could only ever fire on the scripted
  tracker, and a session ran on with no eye data behind its trials. The
  session's tracker health check now also calls the backends' new
  `recording_fault()`. The EyeLink's calls a newest link sample that has not
  been replaced for `max_sample_gap_ms` a dropout. A blink is a sample saying
  "no eye", so it never counts. Only then does it ask `isRecording()` why.
  The TRACKPixx3's calls it a dropout when its gaze reader has stopped or
  stalled past the limit, or when the device, asked by the reader every half
  limit, says free-run sampling no longer feeds the session's buffer or does
  not answer. A healthy frame makes no round trip to either device. What each
  says lands in `fault_detail`, so the lab can tell a pulled cable from a
  Host PC abort: "no new sample from the EyeLink for 58 ms (limit 50 ms); the
  Host PC at 100.1.1.1 reports recording ended (isRecording 3, ABORT_EXPT):
  its operator aborted the experiment", or "… still reports recording
  (isRecording 0), so the samples stopped on their way here", or "the
  TRACKPixx3 stopped recording samples into the session's buffer: free-run
  sampling is off". See [docs/eye-tracker.md](docs/eye-tracker.md), "When the
  tracker drops out mid-trial", and [docs/architecture.md](docs/architecture.md)
  §4.3.

- **`alhazen run --task` showed the subject no instructions.** Only an
  experiment's `run.py` was ever handed the subject's wording, so a real
  session started with `alhazen run` put trial one in front of the subject
  with no instruction screen, no SPACE to wait for, and nothing saying so.
  With `Task.instructions()` implemented, both entry points show the same
  screen.
- **`alhazen run --task` ignored the experiment's params file.** With no
  `--params` it ran the params model's defaults — for one experiment 432
  trials of a 576-trial design — without a word, while the same experiment's
  `run.py` loaded its file. With `Task.default_params()` implemented, both
  load the same file.
- **`alhazen run --task` could not start a task that needs its params
  hook**, and started one that feeds results back into shared state without
  it: only `run.py` was ever handed the hook. With `Task.params_hook()`
  implemented, both apply it.
- **A params hook saw a prompted subject as `None`.** The hook ran before the
  subject and session were asked for, so with `--sub` or `--ses` left to the
  prompt a hook filing state by subject filed it under `sub-None`, silently.
  Both are now settled — from the flags, the prompt, or simulate mode's `sim`
  and `1` — before any params hook runs; the params file is still loaded and
  checked before anyone is prompted.
- **The scaffold's `run.py` claimed `alhazen run` "does the same job".** It
  did not: `run.py` was handed the params file, and `alhazen run` ran the
  params model's defaults. Every experiment scaffolded since inherited the
  claim and the gap. The template now says what is the same — the params
  file and the instructions, both declared on the task — and what is not:
  `alhazen run` needs `--rig`. An experiment scaffolded earlier fixes both by
  declaring `default_params()` and `instructions()` on its task.

- **A tracker that stops during feedback no longer throws away the trial.**
  The health check aborted even the closing phase (`TrialFeedback`), which
  measures nothing. When the closing phase was the one deciding the outcome
  (a `LandingCheck` that ADVANCEs into `TrialFeedback(then=...)`), a
  finished measurement came back `ABORTED` and was served again; when an
  earlier phase had decided it, the feedback was cut off before it was drawn
  and the row carried an `abort_reason` for a trial that was not aborted.
  Now the row is flagged `fault: tracker_stopped`, a WARNING is logged, the
  feedback runs to its end, and the trial keeps its outcome — paid,
  scheduled and counted by it.

- **The scene documentation no longer promises the same frames on every
  run.** `SceneStimulus`, `docs/scenes.md` and `docs/architecture.md` said
  that scene time never comes from a wall clock, so two runs of the same
  trial show the same frames. It never reads a wall clock, but scene time is
  the sum of the measured frame durations a phase passes to `update(dt)`, so
  it follows the flips as they actually happened. A dropped frame moves the
  scene on by the time it took, and the picture due in between is never
  drawn. Flip jitter moves it by fractions of a millisecond. Two runs show
  the same frames only if every flip took the same time. What does
  reproduce is the picture for a given scene time and `dt`. The docs now say
  so, with a worked example. Nothing about how a scene is drawn has changed,
  and a test now pins the behaviour described.

- **Frame QA's percentages no longer contradict their own verdict.** The
  recycle reason printed the dropped fraction to one decimal and the budget
  to whole percents. At the shipped 10% budget, a trial that dropped 21 of
  209 frames was recycled as "(10.0%), over the 10% budget", and a 7.5%
  budget was written as "8%": "3 of 39 frames dropped (7.7%), over the 8%
  budget". That text is the trial row's `frame_qa_reason`, the session log's
  line and `FrameQAError`'s message. The per-trial dropped-frames line and
  the failure-streak pause ("dropped over 8% of their frames") rounded the
  same way. A budget is now written as it was set ("7.5%"). A fraction gets
  as many decimals as it takes to read on the side of the budget it is
  really on: "(10.05%), over the 10% budget". A fraction clear of the budget
  keeps its one decimal ("(15.0%)"). The alignment's matched-fraction
  refusal already worked this way ("79.8% < 80%"); all of these messages now
  share one rule, and the refusal prints exactly what it did.

## 1.5.0 - 2026-09-23

### Changed

- **`show_message` reflows prose, so hard-wrapped instructions are no longer
  wrapped twice.** It drew its text with every newline kept, and the message
  box then wrapped it again at its own measure (the smaller of 80% of the
  screen's width and 34 letter heights): an `instructions.md` wrapped at 80
  columns came out ragged, with orphaned words, and taller than it needed —
  tall enough, since 1.4.4, to shrink the letters to fit. Now a single newline
  inside a paragraph becomes a space and a blank line separates paragraphs; a
  line that starts with whitespace or a list marker (`-`, `*`, `+`, `•`, `1.`,
  `1)`, then a space) keeps its break and its indentation, so an indented key
  list or a Markdown list is left as laid out. `show_message(text,
  reflow=False)` keeps every break exactly. The TRACKPixx3's
  `Calibration FAILED` notice is now two paragraphs, so reflow keeps its two
  sentences apart; the pause menu proper (`show_menu`) never reflows. **A
  display backend of your own** keeps working unchanged: it should accept
  `reflow` as a keyword-only argument defaulting to `True` to support it, and
  alhazen never passes `reflow` to a backend that does not take it (the
  deprecated `pause_menu` seam checks the signature first). Instructions
  that relied on unindented line breaks (a list of keys) should indent those
  lines.

- **A sorted-spike sorter must re-announce `units` at least every second,
  and a late subscriber no longer fails.** The sample rate rides on the
  `units` message, and announcing it once at startup made it unrecoverable
  for anyone who joined later — which every subscriber to a `PUB` socket
  does, `alhazen check-rig` always. A `spikes` or `heartbeat` arriving
  before the first `units` is now *held* and placed once the rate arrives,
  instead of being refused on the spot; only a stream that publishes for
  2000 ms without ever announcing units is a fault, and it says the sorter
  never re-announced units rather than that it sent a heartbeat first.
  `check-rig` reports that case separately from an endpoint where nothing is
  publishing at all. A sorter that announces only at startup is now
  non-conformant: see the wire contract in
  [docs/live-spikes.md](docs/live-spikes.md).
- **Dashboard figures follow a journal's conventions.** Labels are in sentence
  case. Column and outcome names are written as words (`FIX_BREAK` is "Fix
  break", `saccade_latency_ms` is "Saccade latency (ms)"), with abbreviations
  in their own case ("RT", "IQR", "s.e.m."). Degrees of visual angle are "°",
  negative numbers carry a true minus sign, the sample size is an italic *n*,
  and panels are lettered a, b, c. Axes are dark, with outward ticks, and end
  on labelled ticks. Bars have square ends and read as bars rather than stems.
  The polar grid is recessive, with its amplitudes labelled up the vertical.
  Error bars are defined in the legend ("Mean ± s.e.m.", "95% CI").
- **Panel payload text changed; raw values did not.** Prose (`x_label`,
  `y_label`, `value_label`, `error_label`, `note`, `message`, `stats[].label`
  and `stats[].value`) is rewritten in place, so a test asserting those strings
  needs the new wording. Data values keep their record form and gain a display
  twin: `items[].display_label`, `series[].display_name`,
  `groups[].display_label`, `groups[].display_series`, `band.display_name`,
  `maps[].display_name` and `display_color_label`. The eye tracker's
  validation panel titles its axes "Horizontal gaze position (°)" and
  "Vertical gaze position (°)".

- **The dashboard's camera image streams.** It used to move about once a
  second, because each frame rode inside a full dashboard update that rebuilt
  every panel. Frames now travel on their own channel, about fifteen a second
  while paused and ten a second through a TRACKPixx3 calibration, and the page
  redraws only the image, with its frame rate printed under it.

- **A validation that does not pass is a warning you can accept.** The pause
  menu used to lead with *VALIDATION FAILED … recalibrate (C) before resuming*,
  in the fault colour. It now leads, in amber, with how the validation fell
  short and both ways on: SPACE resumes on it, C recalibrates. Resuming on it
  logs a WARNING and records the validation's numbers in the RESUMED event
  (`on_failed_validation`); the VALIDATION event and the per-target errors in
  the log are written whether it passed or not, as before.

### Added

- **Mid-trial reward: a phase can ask for a juice drop while the trial
  runs.** A task that declares `mid_trial_reward = True` (next to `reward`)
  may call `ctx.request_reward(pulses, reason)` from a phase. The request only
  queues; after the next flip the engine hands it to the rig's dispenser,
  which delivers on a worker thread (`QueuedReward`), so a pulse train never
  stalls the frame loop, and emits `REWARD` stamped with that flip, carrying
  `{pulses, reason, frame}` and `queued_behind` when earlier drops were still
  delivering. The delivery's end follows as a new reserved event,
  `REWARD_DELIVERED`, or as `REWARD_FAILED` with the same `reason`; a failure
  lets the trial finish and then takes the same pause flow an end-of-trial
  failure does. Every delivery in such a session — drops, the manual key,
  the end-of-trial pay — goes through the one worker, in order, and the
  runner waits for the drops before it pays the outcome, so no two pulse
  trains overlap. Rows gain `n_mid_trial_rewards` and
  `n_mid_trial_reward_failures`, and `rewarded` is True when any juice
  reached the subject during the trial; a trial that earned drops gets no
  `NO_REWARD`. Such a task is refused at build on a rig with no dispenser;
  `--mode test` and `--mode simulate` stand a simulated one in. A request
  from a task that did not declare it raises the new `RewardRequestError`.
  `alhazen.testing.ScriptedReward` holds and fails deliveries on a test's
  say-so, for deterministic threading tests. Nothing changes for a task that
  does not declare it. See "Mid-trial reward" in
  [docs/architecture.md](docs/architecture.md).

- **`LandingSample`: a landing phase that records where the saccade ended.**
  `LandingCheck` ends on the first frame gaze is inside the target region,
  which for any usable window is mid-flight: with a 3° window a 5° saccade is
  recorded 2–3° short of where it lands, biasing every analysis that filters
  on landing error. `LandingSample` (in `alhazen.task.phases`) ignores the
  region until the movement is over and judges the last valid sample once.
  It ends after a fixed dwell from saccade onset (`dwell_s`), or at saccade
  offset (`settle_speed_dva_per_s` with a `max_wait_s` cap): the first new
  sample slower than the threshold, where repeated samples are not counted
  and a blink is never settled. Onset is the flip-stamped `RESPONSE_ONSET`
  by default (`onset_event=`), the reference position may be a callable for a
  figure that moves, and either verdict may be `PhaseAction.ADVANCE`. It
  writes the familiar `endpoint_*` columns plus `endpoint_measured` (False,
  with no position, when no valid sample arrived), `endpoint_latency_ms`,
  `endpoint_reference_x/y_dva` and, in the saccade-offset mode,
  `endpoint_settled`. `depart_region="fixation"` makes it wait for the eye to
  leave that window: a blink counts as departure under the blink rule, so a
  blink at the cue stamps the onset with the eye still at fixation, and the
  first slow sample there would otherwise end the trial as a miss at
  fixation. With it, a sample still inside the window is never the endpoint
  and never settles, and an eye that has not left by the dwell or the cap is
  recorded as not measured. **`LandingCheck` is unchanged**, and its
  docstring now warns that its endpoint is where gaze entered the window. See
  [docs/architecture.md](docs/architecture.md) §5.2.
- **`InputFrame.gaze_t`: when the gaze sample was taken.** Seconds on the
  session clock, `None` whenever `gaze` is `None`. A display frame that brings
  no new tracker sample repeats the previous position, and until now a phase
  had no way to know: a speed computed across the repeat reads as zero, so a
  rule that waits for the eye to slow down could stop mid-saccade. A repeat
  now carries the same `gaze_t`, and the gap between two new samples is their
  real spacing rather than the nominal frame period. The EyeLink backend
  keeps a sample's first-read time for as long as the link hands back the
  same sample, where it used to restamp every read with "now"; the other
  backends already did the equivalent. A fake tracker that returns its own
  sample objects needs a `t` on them, as `GazeSample` always required. See
  [docs/architecture.md](docs/architecture.md) §2.1.

- **`alhazen.display.reflow(text)`**, the rule `show_message` applies, as a
  pure function with no display behind it — for an experiment that wants to
  see its instructions as the subject will, and in place of the
  line-joining each experiment's `run.py` carried its own copy of. Edge
  cases (`\r\n` endings, runs of blank lines, whitespace at either end,
  indented lines and list items) are in its docstring and in
  [docs/architecture.md](docs/architecture.md) §10.1.
- **`message_calls` on `SimulatedDisplay` and `testing.FakeDisplay`**: every
  `show_message` call as `(text, reflow)`, exactly as given, so a test can
  pin whether a caller kept its line breaks. `messages` is unchanged.

- **`TrialFeedback(keep_drawing=...)` keeps other stimuli on screen during
  feedback.** Feedback drew only the fixation point, so whatever the last
  measuring phase showed vanished on the frame feedback started — a figure the
  subject had just saccaded to blinked off at the moment they were told
  whether they reached it. `keep_drawing=("figure",)` names stimuli that are
  updated and drawn every frame, before the fixation point so the recoloured
  point stays on top; only the fixation point changes colour. A name the
  trial has no stimulus for fails when the phase starts, naming it, and
  naming the feedback stimulus itself is refused at construction. A trial
  that ended early with a non-completed outcome (a fixation break) keeps
  nothing, since the figure may never have been shown. The default is empty,
  which is the old behaviour.
- **`alhazen check-rig --record <path>`: the checkout leaves a written
  record.** Until now a pre-session checkout existed only as scrollback in
  whichever terminal was open at the rig, so nothing about it could be
  compared with the next one — and a rig that has been degrading for a
  fortnight passes every check on the morning it finally breaks. `--record`
  writes what each device *did*, per device: the reward pulse commanded and
  measured, every sync line by name with what was sent on it and which events
  it carries, what the recorder returned, how long the tracker took to
  answer, and the sorter's measured lag, units and dropped messages — plus
  the rig file, the alhazen version and git revision, and a timestamp. JSON
  at the path you name (sorted keys, so two runs diff line by line) and a
  readable rendering at `.txt` beside it; both written on a FAIL as well as a
  PASS, because the failing record is the one that says how far each device
  got. `CheckResult` gained an `evidence` mapping, and
  `alhazen.session.checkout` has `read_record` and `differences` for holding
  one checkout against another. **Nothing about the verdict changed**: `OK`,
  `FAIL` and the exit code are still decided by the checks alone. The exact
  command to type at the rig is in
  [docs/pre-session-checkout.md](docs/pre-session-checkout.md).
- **`alhazen sim-sorter`, and a rig config that rehearses the whole
  checkout.** The sorted-spike sorter is the one device `check-rig` depends
  on that no repository here contains — it is somebody else's program, on
  somebody else's machine — which made `FAIL spikes` the one line an
  experimenter met for the first time on the morning it mattered. The new
  command publishes the `docs/live-spikes.md` wire contract over ZeroMQ, so
  `alhazen check-rig --pulse` runs end to end with no probe in anything, and
  `--fault silent|announce_once|never_units|no_seq` publishes the specific
  non-conformances the contract names, so the failures can be rehearsed too
  rather than trusted. `examples/rig-rehearsal.yaml` wires every device in
  simulation and lists exactly which lines become the lab rig. The spikes are
  Poisson noise with no receptive fields: it simulates the transport, not the
  brain, and says so on every startup. See
  [docs/pre-session-checkout.md](docs/pre-session-checkout.md).
- **Figure export.** Every chart panel saves as an SVG at 89 mm or 183 mm, or
  as a PNG at 600 dpi, redrawn in the light theme with its styles, panel
  letter and legend inside the file. See "Figures for publication" in
  [docs/dashboard.md](docs/dashboard.md).
- **`shapes` on scatter panels.** A record column of circles and rects in the
  panel's units, outlined under the points: drawn once per distinct shape, in
  the colour of the one `color_by` level that showed it, or grey when several
  did. A malformed shape raises, naming the trial and the column.
- **`cross=True` on `grouped_mean`.** One bar per combination of several
  factors' levels, each with its own *n*.
- **Iris size on the dashboard's camera panel.** The TRACKPixx3's expected
  iris size, the setting LabMaestro adjusts when an eye keeps dropping out of
  tracking, can be stepped or typed under the live camera image while the
  session is paused or during a calibration. The session reads the device
  back and shows what it holds. Each change is logged and recorded as a
  TRACKER_SETTING event, a new reserved event. `eyetracker.iris_size_px` sets
  the size when a session starts; left unset, the size the device holds is
  logged.
- **The TRACKPixx3 calibration is plotted.** The Calibration panel was a
  verdict tile. After a calibration that took, it is now a plot like the
  validation's: each target and each eye's fitted gaze on it, with each eye's
  mean and worst error, computed from the raw eye vectors the device measured
  and the polynomial it fitted, as LabMaestro plots them. The CALIBRATION
  event carries the same per-target numbers.

### Fixed

- **A recording that started late or stopped early can be aligned.**
  `fit_alignment` anchored its seed search on the very first and last
  event, so when either had no pulse — the recorder started a few minutes
  after the session, or stopped before it ended — every seed tied that event
  to another event's pulse, and the fit was refused as a different session.
  It now also tries the first and last few events, as many as the matched
  threshold lets go unmatched (101 of 500 at the default 80%, never more
  than 128). The 0.99–1.01 scale bound and the matched-fraction refusal are
  unchanged, so a different session is still refused, and both refusals now
  say how many events and pulses were compared at each end. A perfectly
  regular train missing an end pulse, where a one-trial shift fits exactly
  as well, is now refused as too evenly spaced instead of returning either
  map. A stray pulse about one trial-gap before the session could
  previously shift the whole map by one trial without complaint — every
  event still matched, and the one extra pulse was reported at the far end;
  the closer-fitting map now wins.
- **The matched-fraction refusal no longer reads "80% < 80%".** Both numbers
  were rounded to whole percents, so 399 of 500 against the 80% threshold
  seemed to contradict itself. The fraction now carries as many decimals as
  it takes to be visibly below the threshold: "79.8% < 80%".

- **A trial that frame QA recycles is paid for what the subject did.** Under
  `frame_qa.policy: recycle_trial`, a trial whose display dropped too many
  frames becomes `DROPPED_FRAMES` and is served again. Its feedback already
  showed the subject's own result, but the reward was then decided on
  `DROPPED_FRAMES`: a correct response was paid nothing, and no `NO_REWARD`
  event said so. Reward now follows the response — the outcome kept as
  `outcome_before_frame_qa` — so a correct trial is paid (`REWARD`, or
  `REWARD_FAILED` if the pump fails) and a completed wrong one gets
  `NO_REWARD`, with the event naming that outcome. The trial is still served
  again for its data. `TrialResult` gains `outcome_before_frame_qa` and
  `response_outcome`. Frame QA judges the data, never the subject.
- **A SPACE pressed during a TRACKPixx3 calibration is no longer lost.** The
  calibration screens waited for keys with PsychoPy's `waitKeys`, which empties
  the keyboard buffer each time it starts waiting. A press made while the walk
  was reading the eye status, flipping or updating the dashboard was thrown
  away, so SPACE had to be pressed again and again, and the extra presses then
  accepted the next target at once. Keys are now read without emptying the
  buffer, which is cleared once when the guide or each target appears. P, the
  session's pause key, now stops the walk and goes back to the pause menu, as
  ESC does.
- **A stray vertical line at the left edge of every line chart.** The hover
  crosshair's stylesheet `opacity` outranked the attribute that hides it.
- **A grouped panel drew its first two factors in the same colour.**
- **Several factors side by side looked like the cells of a design.** The
  panel now says they are marginal means.

## 1.4.4 - 2026-09-10

### Fixed

- **A message taller than the screen shrinks to fit, and says so.**
  `show_message` drew its box centred at the usual letter size whatever the
  text's length, so instructions that laid out taller than the window lost
  their first and last lines off the top and bottom of the screen, and nothing
  was logged. A rig's subject instructions did exactly that. The letters now
  shrink, keeping the same number of characters to a line, until the box fits
  in 95% of the screen's height, and a WARNING says by how much so the text
  can be shortened. They never shrink below 60% of their usual size. Past that
  the box is drawn from the top of the screen, so the text reads from its
  start, and the overflow is logged as an ERROR.

### Changed

- **In simulate mode, the break between blocks resumes by itself after 10
  seconds if nothing is pressed.** A simulation on a real display has a
  keyboard wired, so its break waited for a SPACE that nobody watching a dry
  run had a reason to press. The rest screen now says it will resume by
  itself. Any key other than resume or quit, at the rig or in the dashboard,
  means somebody is there, and from then on the break waits for them. A run
  with no keyboard wired still resumes at once, and fault pauses, such as a
  failed reward or validation, never time out. Real sessions and test mode are
  unchanged: their breaks end when the experimenter says so. `build_session`
  and `SessionRunner` take the wait as `rest_resume_after_s`.

## 1.4.3 - 2026-09-10

### Fixed

- **The pause screen no longer draws its heading over the menu.** The heading
  and the menu rows sat at fixed distances below the panel's top, which left
  room for one line of heading. A fault heading is a sentence, such as "6
  TRIALS FAILED IN A ROW — last NO_SACCADE; check the calibration (V), the
  subject, and the stimulus before resuming", and at heading size it wrapped
  onto second and third lines drawn straight over the first rows of the menu,
  so the screen was unreadable. Every part of the menu is now measured and
  stacked below the one above it. A heading too long for one line is drawn as
  its headline, with the instruction after its dash beneath it at reading
  size; a short one such as "BLOCK 1 OF 2 COMPLETE — REST" is still drawn
  whole. The panel grows when the menu needs more room than its usual share
  of the screen, and a menu taller than the screen says so in the log.

### Changed

- **Tagging a release no longer tries to publish to TestPyPI and PyPI.** No
  trusted publisher is registered for this repository on either index, so
  every release run failed at the upload and showed red even when the release
  was fine. `release.yml` now runs the version gate and builds and checks the
  wheel and sdist, and nothing else. `docs/versioning.md` says how to bring
  publishing back.

## 1.4.2 - 2026-09-10

### Fixed

- **Every text file alhazen reads or writes names its encoding.** Thirty-four
  reads and writes left it to the locale, which is UTF-8 on Linux and macOS
  and cp1252 on the Windows rig. There, a non-ASCII character in a config
  value loaded as different characters with no error; a non-ASCII trial or
  event field went into the CSV as cp1252, for pandas to refuse or mis-read;
  and a character cp1252 has no code for raised inside the recorder in the
  middle of a session. Files a person writes by hand — rig and params YAML,
  scene files, the photometer CSV and the participants table — are read as
  UTF-8 that tolerates the byte-order mark Windows editors add. Everything
  else is plain UTF-8, and nothing is written with a byte-order mark.

  A test now fails if any module reads or writes text without naming an
  encoding. It has to be mechanical: CI does not run on a cp1252 machine, so
  a test that writes a file and reads it back passes there either way.

  Files already on disk read the same when they are ASCII, and every run
  directory checked so far is: YAML snapshots escape non-ASCII, and the CSVs
  hold outcome names and numbers. A CSV an older version wrote with a
  non-ASCII value in it now fails loudly on read instead of being quietly
  mis-decoded.

## 1.4.1 - 2026-09-10

### Fixed

- **A recycled trial ends the failure streak, and the streak's pause names a
  failing display.** The engine only recycles a trial the subject completed,
  but the runner skipped recycles when counting failed trials in a row
  instead of letting them end the streak. On a display dropping half its
  frames, a rehearsal's completed trials were all recycled, the fixation
  breaks and missed saccades between them joined into one streak, and the
  session paused on "6 trials in a row" telling the operator to check the
  calibration. A recycle now ends the streak like the completion it was.
  When the failures that do form a streak happened on trials that dropped
  more frames than frame QA's budget, the pause says so and sends the
  operator to the display first, because a panel missing vsyncs causes real
  fixation breaks. Frame times on a simulated display are not counted as
  evidence, since they measure the host's scheduler.
- **`alhazen_git_describe` reads `unknown` when git refuses to look, not `not a
  source checkout`.** Only git's own "not a git repository" earns that label.
  A directory that does not exist, or a repository git will not open for the
  current user, is git failing to answer. A shallow clone with no tags is
  described by its commit, and a test now pins that because experiment CI
  clones alhazen that way.

## 1.4.0 - 2026-09-10

### Fixed

- **The run snapshot records alhazen's version.** Every snapshot alhazen had
  ever written said `alhazen_version: unknown`. `config/snapshot.py` looked the
  version up under the bare name `alhazen`, which belongs to an unrelated
  project on PyPI — the exact trap `alhazen/version.py` exists to close — so
  the lookup found nothing, or, on a machine with that project installed,
  found theirs. It now calls `get_version()`, and a test fails if any module
  other than `version.py` looks a distribution version up itself. An
  integration test had checked the field only for being non-empty, which
  `unknown` is, so it passed the whole time.

  **Do not trust `alhazen_version` in any snapshot written before 1.4.0.** The
  run's date, `experiment_git_sha` and `environment_digest` are what is left
  to narrow down which alhazen produced it.

### Added

- **`alhazen_git_describe`, in the snapshot's provenance and in the run
  report.** Between releases `main` carries the previous release's number, so
  a run made from a source checkout records a version that several different
  trees share. The describe string — tag, commits past it, `-dirty` —
  identifies the code. It is taken only when alhazen is running from a git
  clone of itself: an installed alhazen inside another repository's
  virtualenv would otherwise be given that repository's commit, and on a
  scratch repository it was. Otherwise it reads `not a source checkout`,
  meaning the version alone identifies the code, or `unknown` when git could
  not answer. An additive key; no existing key changed meaning.

## 1.3.1 - 2026-09-09

Documentation and one log line, cut as its own release rather than left on
`main`: three experiment repos install alhazen by cloning `main`, so anything
sitting in `Unreleased` is already running in their CI under the previous
release's number. A small release makes what they are running nameable.

### Changed

- **`read_run` says what the clock fit cost, on every read.** The worst
  residual is the error bar on every session time the reader produces — every
  latency, every event alignment — and it was computed and then discarded.
  "The fit passed" is not the same fact as "the fit was tight to a tenth of a
  millisecond", and a number nobody sees cannot be used. One INFO line with
  the residual, the mark count and how many were dropped.
- **`FrameQAConfig` says to set its thresholds from the frame log rather than
  from what the policy did.** `frames.csv` holds the intervals; a recycle
  count or a run of aborts is a decision made from them under whatever
  thresholds were in force, so tuning the thresholds from those tunes a
  number against itself.
- **`TrialFeedback` says what an acceptance region cannot distinguish.** When
  the acceptance radius plus the fixation radius reaches the target's
  eccentricity, every saccade large enough to count as leaving fixation
  already lands inside the region, so an undershoot can never go red.
- **`docs/versioning.md` says that a downstream pin follows the push to
  `main` and never leads it**, and that between releases `>=X.Y.Z` is
  satisfied by two different alhazens — the tag, and a `main` that has moved
  past it. An experiment repo that installs from a clone of `main` resolves
  its pin against `main`, so a floor raised before the release commit lands
  fails resolution with a message that reads like a broken pin, and a green
  run there is evidence about `main` rather than about any release.

## 1.3.0 - 2026-09-09

### Changed

- **A phase that declares `must_be_last` is now the trial's closing phase,
  and runs whatever the trial ended as.** The engine returned as soon as any
  phase produced an Outcome, so a trial that ended early — a fixation break,
  a saccade that never came — never reached its last phase. `TrialFeedback`
  was therefore unreachable on exactly the trials a subject most needs to
  hear about: a pilot came back with 72 completed trials all showing feedback
  and 7 failures showing none. There was no way for a task to work around it,
  because the only way to stop a procedural phase from ending the trial is to
  have it ADVANCE, which lets a broken fixation fall through into the phase
  that measures the response.

  A closing phase reads how the trial ended from `TrialContext.outcome`, and
  what it returns is discarded when the trial already had an outcome: feedback
  is shown for a fixation break, it does not turn one into a completed trial.
  It does not run on `PAUSED` or `ABORTED` — neither is a trial result, and
  telling a subject they failed a trial they were still in the middle of would
  be a lie.

- **`TrialFeedback` calls a trial that ended with a non-completed outcome a
  failure without consulting `verdict`.** The predicate judges a measurement,
  and a fixation break has none; a predicate that answers True by default
  would otherwise show a green point for a trial the subject broke.

### Added

- **`TrialContext.outcome`** — how the trial ended, set by the engine before a
  closing phase runs and `None` everywhere else.

## 1.2.2 - 2026-09-09

### Fixed

- **Frame QA no longer judges a display that has no panel.** A simulated
  display's flip times measure how accurately the host can wait between them,
  which on a loaded machine is not the rate the rig file asks for. The
  scaffolded lab rig ships `recycle_trial`, so its own acceptance run —
  `--mode simulate --headless`, the documented way to run an experiment on a
  CI box — aborted with "the display is not holding its 120 Hz refresh" when
  the machine was busy. There was no display. The policy is stood down to
  `log` at build time, with a line saying so; the intervals are still
  recorded and still reach `frames.csv` and the dashboard's timing panel.
  `SimulatedDisplay.measure_refresh_rate` has reported its paced rate for the
  same reason since it was written.

## 1.2.1 - 2026-09-09

### Fixed

- **An unattended run no longer hangs at a pause when the rig config enables
  the dashboard.** The pause asked whether a browser was serving before it
  asked whether anyone was at the rig to answer, so a rig with
  `dashboard.enabled` sat in the browser loop waiting for a click nobody was
  there to make. With the block break added in 1.2.0 that was every simulated
  run of every experiment with more than one block: 28 trials and then
  nothing. The unattended check now comes first, the browser is told the
  session carried on, and the skipped pause is logged at WARNING.
- **Frame QA counts a trial as recycled only where one is recycled.** The
  monitor made the `recycle_trial` verdict for every trial over the
  dropped-frame budget and counted it towards `max_consecutive_recycles`,
  while the engine applied it only to a COMPLETED trial. A run of fixation
  breaks on a display dropping the odd frame could therefore abort the
  session blaming the panel, with no `DROPPED_FRAMES` row in the data to
  support it. `FrameMonitor.end_trial` now takes `completed`.
- **The clock fit proves that a dropped alignment mark is a stamping delay**
  rather than asserting it. A mark is dropped only if it is late, isolated
  (both neighbours on the line) and interior; a clock that stepped during the
  first or last trial used to be inside the five percent budget and was
  quietly re-timed. The 198-mark case from the pilot is unaffected.
- **A fault heading comes back down when the procedure succeeds.** A failed
  validation put a red heading on the pause screen that nothing removed, so a
  successful recalibration left it up and a block break's REST heading never
  returned.
- **The run of failed trials counts only what the subject did.** A completed
  trial whose reward pump failed did not clear the count, and `DROPPED_FRAMES`
  — a display fault with its own counter — was counted as a subject failure.
- **`--mode measure` draws the ruler.** A key left in psychopy's buffer by an
  earlier measurement ended the ruler before its first flip: a black screen,
  and a report saying a bar was drawn.
- **An unregistered monitor is a warning, not an INFO line.** The session runs
  on the rig config's geometry with no measured gamma, which looks identical
  to one that inherited a calibration.

### Note

- **`FEEDBACK` became a reserved event name in 1.2.0.** Reserved names are
  append-only by contract, but a task that already declared `FEEDBACK` of its
  own is refused at session build from 1.2.0 on. Rename it, or use
  `TrialFeedback`, which emits it.

## 1.2.0 - 2026-09-09

### Added

- **`read_run_binocular`, for an experiment whose measurement is the relation
  between the eyes.** `read_run` reduces a recording to one eye, which is what
  most experiments want and is none of what a vergence experiment wants:
  vergence is the difference between the eyes and `average` is not vergence
  either. The new entry point reads the same file once and keeps both, as
  `left_x_dva`/`left_y_dva`/`left_tracked`/`left_pupil` and their `right_`
  equivalents, in the same degrees-from-centre the monocular reader uses. The
  header mapping, the clock fit, the blink rule and the bounds check are the
  same code, so nothing verified is re-implemented to get there.

  Two `tracked` flags rather than one, deliberately: a single flag meaning
  "both eyes" is a different predicate under the same name, and it hides the
  case that matters most — one eye lost while the other tracks. Encoding loss
  only as NaN loses that case too, since `(finite + nan) / 2` is nan, so a
  version estimate silently discards the surviving eye's answer; measured at
  50 of 500 samples on a recording where only the left eye blinked. Vergence
  is not a column, because its absolute value carries the subject's tonic
  vergence and both eyes' calibration offsets and means nothing until it is
  baseline-subtracted — a column would invite plotting it raw. Designed with
  the kde-vergence experiment, whose own adapter it replaces.
- **A session pauses after too many failed trials in a row.** A task's
  params may carry `max_consecutive_failures`, read by name the way `iti`
  is; after that many non-completed trials back to back (PAUSED excluded)
  the session stops at the pause screen with the count and the last outcome
  as its heading, and the count restarts after the pause. It is the task's
  number because what is routine for one design is a subject who cannot see
  the stimulus in another. The case behind it: a session that completed
  none of 33 trials, every one a fixation break, on a calibration that
  passed but sat at the edge of the fixation window, and ran to its end with
  nothing on screen saying so.
- **The monitor is named after the rig file, and registration reads the
  record back.** `load_rig` names an unnamed `monitor` after the file's stem
  (`rig-lab.yaml` registers as `rig-lab`), so two rig files on one machine
  never share PsychoPy's one registration and overwrite each other; a
  `monitor.name` in the file still wins. `monitor register` now looks the
  record up after writing it and refuses if PsychoPy hands back different
  numbers from the ones just given — a stale file under the same name, a
  unit converted on the way in — rather than leaving that for the next
  window to refuse.
- **Trial feedback, with the verdict kept apart from the outcome.** A new
  last phase, `TrialFeedback`, turns the fixation point green or red for a
  fixed time, writes `feedback` (`success`/`failure`) on the record, and
  emits the reserved event `FEEDBACK` on the flip that showed it; the
  session's new `FeedbackSounder` beeps from that event (a phase touches no
  hardware), switchable with `display.feedback_beeps`. The verdict is the
  task's own predicate over the record — an acceptance region, a latency
  bound — and the outcome is the task's too and unchanged by it: a saccade
  that missed is still a completed, scored measurement, and re-serving it on
  the basis of where the eye landed would bias every cell toward its own
  hypothesis. The phase declares it must be last and the engine refuses it
  anywhere else, so feedback is never on screen while something is being
  measured — for a display whose premise is one ink value and one
  background, a red dot mid-trial is a third luminance in the measurement.
  `LandingCheck` accepts `PhaseAction.ADVANCE` in place of either outcome so
  a feedback phase can follow it. The fixation point gained `set_color`, and
  the simulated stand-in records the colours it was given.
- **The session takes the break between blocks.** A block boundary was a
  log line and the experimenter's memory. Now a `BlockPlan` leaves a pending
  break when a block that served trials ends and another follows, and the
  runner takes it before the next block's first trial: the pause screen
  comes up headed `BLOCK 3 OF 6 COMPLETE — REST` — the count, because "how
  much longer" is the one question a break gets asked — in the terminal
  green the instructions use rather than the fault red, so a subject resting
  is never looking at the screen that means a calibration died. It stays up
  until SPACE, goes on the record as `PAUSED` with `reason: block_break`, and
  `blocks.breaks: false` turns it off for a design whose blocks are analysis
  structure only.
- **Docs: test versus pilot.** A section in [docs/modes.md](docs/modes.md)
  on what `--mode test` reduces and what it deliberately does not (block
  structure, with the rationale from `modes/rehearsal.py`), why a default
  config and a pilot config can land on the same trial count from opposite
  directions, and how the two compose as `--mode test --params <pilot>`.
- **`--mode measure` ends by drawing the ruler.** It used to print what a
  10-degree bar should measure and send the operator to run `alhazen
  calibrate ruler` separately; one expected the bar and did not get one. The
  bar is now the last measurement, drawn on the same window everything else
  was measured through, with the centimetres to check between its ticks in
  the report. Skippable with `--skip ruler`, since it is the one measurement
  that needs a person holding a tape.

## 1.1.0 - 2026-09-09

### Fixed

- **The TRACKPixx3's gaze report is a calibrated read, and the backend now
  says so.** With no calibration on the device, `TPxBestPolyGetEyePosition`
  returns NaN for every position whether or not the camera sees an eye, and
  the session reported it as "no eye" — the wrong diagnosis, for an afternoon
  on the rig. The backend now reads the raw eye vectors the same call hands
  back (pypixxlib's wrapper throws them away), keeps the device's calibration
  state (read at `configure()` and after every `calibrate()`), gates
  `get_gaze()` on it with one warning per uncalibrated stretch, and offers
  `gaze_status()`, which says whether it was the calibration or the eye that
  was missing. `SessionRunner` asks a tracker with that capability before
  trial 1 and pauses with `TRACKER NOT CALIBRATED` as the reason. The device
  keeps a calibration across runs, so the log now says at `configure()`
  whether the session starts on one nobody in the room made.
- **`--mode measure` measured the wrong flip, an uncalibrated tracker, and
  printed vendor chatter as if it were an error.** The key-latency prompt was
  drawn by `show_message`, which already flips, and then flipped again — so
  the prompt vanished and the latency was timed from the blank flip. The
  tracker accuracy check compared uncalibrated gaze with target positions;
  it now calibrates first through the tracker's own `calibrate()`, names the
  calibration in the report, and refuses to report an accuracy when the
  calibration was aborted or failed. And pypixxlib's "Recording data is not
  yet directly implemented" print — from two methods that work perfectly
  well — is captured and logged as expected chatter at DEBUG, with anything
  else the library prints kept at INFO.
- **`n_dropped_frames` is 0 on a clean trial, not absent.** The engine
  created the column on the first drop, so a clean trial wrote an empty cell
  that read back as NaN: a column mean overstated drops by a third,
  `astype(int)` raised, and `alhazen report` dropped clean trials from its
  own table. The counter is zeroed at trial start under every marking policy,
  and the report reads an old run's empty cell as 0.

  This does change what lands on disk — a cell that was empty is now `0` —
  and the preamble above reserves changes to column meanings for a major
  version. It is in a minor because the *meaning* is unchanged: an empty cell
  always meant no drops, every file written before this release still reads,
  and `alhazen report` reads the old empty cell as the zero it meant. The
  promise protects a stranger's year-old analysis; a change that keeps their
  files readable and their columns meaning what they meant does not break
  it. Noted here so that nobody reading the preamble literally either blocks
  the next such fix or quietly ships one without saying so.
- **`session.log` is written as UTF-8.** It used the platform default, which
  on Windows is cp1252, and every line with a dash or a degree sign came back
  from the rig as mojibake.

### Changed

- **Frame QA has a proportional policy, and an inert threshold is a config
  error.** `frame_qa.max_dropped_per_trial` was only read under `abort_run`;
  under `mark_trial` it sat in a rig file reading like a tolerance and did
  nothing, and the analysis downstream, excluding on any drop, emptied two
  thirds of a rehearsal's design cells. New policy `recycle_trial` ends a
  trial that dropped more than `max_dropped_fraction` (default 10%) of its
  frames as the reserved outcome `DROPPED_FRAMES` — `completed=False`, so the
  scheduler re-serves the condition exactly as it re-serves a fixation break
  — keeping what the outcome would have been as `outcome_before_frame_qa`
  and the reason as `frame_qa_reason`; a fraction rather than a count because
  three drops are a tenth of a 30-frame trial and nothing in a 350-frame one.
  `max_consecutive_recycles` (default 5) in a row abort the run with a
  message naming the display, so a panel that is not holding its refresh
  cannot re-serve every trial forever with a subject in the chin rest.
  Setting `max_dropped_per_trial` under any policy but `abort_run`, or
  `max_dropped_fraction` / `max_consecutive_recycles` under any but
  `recycle_trial`, is refused when the rig loads — a threshold that does
  nothing is the thing this refuses. (`DROPPED_FRAMES` joins `PAUSED` and
  `ABORTED` as a name a task cannot declare.)
- **The session log records the session, not its dropped frames.** A
  72-trial rehearsal's log was 310 lines: 308 identical per-frame warnings
  and two of anything else, and it stopped mid-trial whether the session
  crashed or ended. Per-frame drops are now DEBUG (the frame log holds every
  interval) and one WARNING per trial that dropped anything sums the trial
  up. What is INFO is the structure: `session start`, a `devices:` line, one
  `setup:` line per thing the mode decided (reductions, stood-down devices —
  `ModeSession.describe()` now goes into the log as well as the terminal,
  because a terminal is not part of the run directory), `block N of M
  starts/ends` from `BlockPlan`, every validation's per-target errors and
  every drift correction beside the calibration verdicts already there, one
  line per trial with its outcome and reason, and `session end:` with the
  status and outcome counts — or `session end: FAILED … <exception>` at
  ERROR.

- **Every mode runs on every rig.** A rig file describes a machine — its
  panel, its devices, where its data goes — and says nothing about what you
  are about to do on it; that is the mode's business. Simulate mode used to
  refuse a rig that configured real hardware, which made the file that
  described the rig the one file the rig could not rehearse on, and the
  scaffold answered by shipping a file per *purpose* (`rig-sim`, `rig-view`,
  `rig-auto`, `rig-mouse`): the machine's file with a device left out or
  swapped for a stand-in, so the machine's numbers lived in five places.

  Now the mode decides what to do with the machine, in one pure function
  (`alhazen.modes.session.rig_for_mode`), and prints what it decided before
  trial one. `simulate` stands the rig's real devices down instead of
  refusing them: the task's autopilot takes the tracker's place, `nidaq`
  reward and sync become `simulated` (deliveries and pulses logged, not
  fired), a `spikeglx` recorder becomes `simulated` and a live spike stream
  is dropped — each a line in `describe()`. `test`, on a rig with no
  tracker, takes the mouse cursor as gaze. The rig file itself is never
  touched; the substituted copy is what `build_session` gets and what the
  snapshot records. `run` drives the rig exactly as written.

  Two new flags override the machine itself, and each belongs to exactly one
  mode: `--headless` (simulate: no window opens and the dashboard does not
  open a browser — CI, ssh) and `--mouse` (test: the cursor as gaze on a rig
  whose tracker is off). Any other mode refuses them by name with the reason
  (`alhazen.modes.flag_refusal`), exit 2, before anything loads.

  `alhazen new` therefore scaffolds two rig files, one per machine:
  `rig-mac.yaml` (a development laptop: a window, no devices, `data_root:
  data`) and `rig-lab.yaml` (the rig). `rig-sim`, `rig-view`, `rig-auto` and
  `rig-mouse` are gone, and so is the `data/dev` convention — `test` and
  `simulate` redirect to the rehearsal root themselves, so no rig file needs
  a second data root for the purpose. The scaffold's `run.py` defaults to
  `rig-mac.yaml`, and its README, `alhazen new`'s closing hints and the docs
  ([docs/modes.md](docs/modes.md#every-mode-on-every-rig)) are rewritten
  around the two files. An experiment that kept a purpose rig can delete it:
  `--mode simulate --headless` on the lab rig is what `rig-sim` was for.

### Added

- **`TRIAL_RECORD_COLUMNS`, and a contract test that drives real trials.**
  The columns the framework writes onto a trial record are named once, in
  `core/trial.py`, and exported — so an analysis in another package imports
  the name instead of typing it. It has to be exported: one experiment's
  dropped-frame exclusion read `dropped_frames` where alhazen writes
  `n_dropped_frames`, matched no trial for the life of the experiment, and
  kept a green suite the whole time because its own fixture was written in
  the same wrong name. `tests/unit/test_contracts.py` now pins the names by
  running real trials through the engine and the runner and comparing what
  they produce against the tuple, in both directions, so a rename at a write
  site fails there rather than downstream. The names are in
  `tests/fixtures/contracts.json` as part of the run-layout contract, which
  already promised column meanings.

- **A ViewPixx (TRACKPixx3) reader, `analysis/io/viewpixx.py`.** Reads a
  run's `*_gaze.csv` and `*_gaze-messages.csv` onto the session clock: an
  affine device→session **fit** from the two-clock message pairs, refused
  when its worst residual exceeds a sample period; a sample table in degrees
  from the screen centre where a lost eye or the device's blink flag is a NaN
  row at its own time rather than a missing one (so a differentiator cannot
  interpolate across a blink and invent a saccade); pupil diameter when the
  file has it; `average` as the mean where both eyes are tracked, by the same
  rule the live backend applies; `event_times` and `trial_spans` from the
  messages; and `gaze_frame`, an explicit setting for which way the device's
  `Screen X/Y` point — centred px with y up for a TRACKPixx3 this backend
  calibrated, which a calibrated accuracy check on the rig has now measured
  rather than argued. Reading the wrong frame displaces every position by
  half a panel while leaving the cluster as tight as ever, so `read_run`
  refuses a run with a quarter or more of its tracked gaze off the panel
  (naming the other frame), warns above a few percent, and takes
  `check_bounds=False` for gaze that really was off the panel. Its column
  names are the device's own — `Timestamp`, `Left Screen
  X`, VPixx's `Right Fixaion` typo included — matched regardless of the
  header's tabs and spaces, and pinned by a test against
  `tests/fixtures/trackpixx3/`, the header and messages of a real recording:
  the reader that preceded it, in an experiment package, passed every test
  written against fixtures in the names it wanted and could not open a real
  file.
- **A *Frame intervals* panel on every dashboard.** A histogram of every
  flip-to-flip interval, in eighths of a frame period, built by the runner
  from its `FrameMonitor` (`dashboard.panels.frame_intervals_panel`) and
  filed under *Session*. The stats strip counts the frames **under half a
  period** and turns red if there are any: impossible on a vsync-locked
  display, so the flip is not waiting for vsync — which a headless rehearsal
  here had 338 of behind a perfect-looking median, and no dropped-frame count
  could show ([docs/dashboard.md](docs/dashboard.md#frame-timing-panel)).

- **A calibration guide, validation and drift correction, and an Eye
  tracker tab on the dashboard.** Before a calibration starts, the subject
  display shows a guide (`devices/eyetracker/guide.py`): which tracker,
  which eye the session reads, how many targets over what part of the
  screen, whether the experimenter presses SPACE for each target or the
  tracker moves on by itself (`eyetracker.calibration_advance: manual |
  auto`), the keys, and — on a TRACKPixx3 — a live line saying which eyes
  the camera sees. `calibrate()` now returns a `CalibrationResult` (`ok`,
  layout, target count, eye, advance mode, the device's own note).
  Validation (the same targets again, per-target error in degrees, passing
  when the *worst* is within `accuracy_max_deg`) and drift correction (one
  centre target, the offset applied to every gaze position from then on
  unless it exceeds `drift_max_deg`) are new, generic over the `EyeTracker`
  protocol (`devices/eyetracker/procedures.py`), so they run the same way
  on every backend and are tested on the scripted one. A validation runs by
  itself after every calibration that was not aborted and that the tracker
  did not itself call bad (`validate_after_calibration`). The pause
  screen offers them as `V` and `D` beside `C`, the dashboard as *Validate*
  and *Drift correct* buttons; while one runs the dashboard's status reads
  *calibrating* and follows the walk target by target, and a new *Eye
  tracker* group shows the camera image (TRACKPixx3, `eyetracker.
  camera_image`), the calibration verdict, the validation's targets and
  gaze on a degree grid with its errors, and the drift offset. All three go
  on the record as the reserved events `CALIBRATION`, `VALIDATION` and
  `DRIFT_CORRECTION`. The EyeLink's `calibration_type` is checked against
  the layouts its Host PC accepts when the rig loads. The session's side of
  all this is `session/eyetracker.py` (`EyeTrackerMonitor`), which
  `SessionRunner` now takes as `eyetracker=` in place of the `on_calibrate`
  callable. Documented in [docs/eye-tracker.md](docs/eye-tracker.md).

- **Instruction screens look like a terminal.** Every message the session
  puts on the subject display — the instructions, `stage: 2`, the
  calibration guide — is drawn as monospace text in pale green on a
  near-black panel with a green outline, sized to what it says and centred
  on the screen, the way the pause menu is drawn in orange and a fault in
  red (the green is `display/palette.py`, the orange and red stay with the
  menu in `session/pause.py`): the border colour alone says which of the
  three a screen is. The two faces the panels use (Noto Sans for headings,
  DejaVu Sans Mono for everything laid out in columns) are registered from
  the copies PsychoPy and matplotlib ship when the display opens, and the
  session log warns by name when a face could not be found at all — pyglet
  would otherwise substitute the system default without a word.

- **A live spike source behind the device seam** (`devices.spikes`): the
  `SpikeSource` protocol with a `spikeglx` backend — SpikeGLX's remote
  command server through the official SpikeGLX-CPP-SDK bindings, imported
  lazily and named in the error when absent — and a `simulated` sibling
  whose configured ground-truth receptive fields fire to the session's own
  stimulus events, so a live pipeline runs and is *asserted on* (known
  field in, same field out) with no hardware. A background thread fetches
  the stream, the new `alhazen.neural` package turns it into threshold
  crossings (median CAR, moving-average high-pass, −kσ against a robust
  noise estimate, chunk-boundary-safe) and places them on the session
  clock with the live estimator's error budget stated. Thread faults
  re-raise on the session's own thread at the next drain — a silently dead
  stream would read as "the neurons stopped responding", which is a
  scientific claim, not a connection status. `check-rig` covers it;
  simulate mode counts a real one as hardware and refuses it. Documented
  in [docs/live-spikes.md](docs/live-spikes.md).

- **A live-analysis seam on Task** (`Task.live_analysis(wiring)` →
  `task/live.py`): between-trials computation that consumes a device,
  contributes its own dashboard panels, and saves an artifact in teardown
  before the manifest is written — never inside the frame loop. The
  dashboard gained the matching `heatmap` wire form (small multiples on one
  shared colourbar, unmeasured cells drawn as unknown rather than zero,
  theme-following via the ordinal ramp). The first user is the
  [rf-mapping](https://github.com/sh4r11f/rf-mapping) experiment, which
  maps V1/V2/V4/MT receptive fields live on exactly these seams. (Its
  tasks briefly lived in this repo as `alhazen.task.templates` between
  releases; they moved to their own repo before ever shipping, so nothing
  released changes.)

- **Movie mode** (`--mode movie`), the sixth way to start an experiment: write
  the conditions to `.mp4` files, for a distributable demo of a stimulus a
  figure in a paper cannot carry. The task implements one hook —
  `Task.movie_clips(setup)`, returning `alhazen.modes.movie.MovieClip`s that
  yield numpy frames, one per screen flip — and the mode owns everything after
  the pixels: the encoder, `--scale`, `--clip` selection, and `--sheet`, which
  tiles every clip into one labelled movie on a common clock. Frames are cut
  against the geometry and refresh rate of the rig `--rig` names, and float
  frames outside 0..1 are refused by name rather than clipped, so a
  compositing bug cannot ship as a movie that looks merely "a bit off". The
  encoder is the new `[movie]` extra; the mode names it if it is missing.
  Because `--mode` choices come from the `Mode` enum, every experiment's
  `run.py` gains the flag by upgrading alhazen — implementing the hook is the
  experiment's only part. Grew out of `amodal-averaging`'s own movie writer,
  which carried four hundred lines of encoder plumbing no experiment should
  have to write twice.

- **One rig file per purpose.** `alhazen new` now scaffolds the full set of
  dev rigs both existing experiments had grown by hand — `rig-view.yaml`
  (demo/movie), `rig-auto.yaml` (simulate, dashboard up), `rig-mouse.yaml`
  (test, mouse cursor as gaze), `rig-mac.yaml` (a Mac dev machine, with the
  Retina device-pixels-versus-points notes) — beside the existing
  `rig-sim.yaml` and `rig-lab.yaml`. Every dev rig points `data_root` at
  `data/dev`, so a rehearsal can never land where the analysis looks for
  subjects, and each file states that its monitor numbers are a starting
  point to be measured, not a measurement. The scaffold's `run.py` is also
  rewritten onto `alhazen.cli.modes.run_experiment`, so a new experiment
  starts in any mode from day one instead of only `run`. Documented in
  [docs/modes.md](docs/modes.md#one-rig-file-per-purpose).

### Fixed

- **A TRACKPixx3 session no longer hangs after a calibration, drops frames
  on every gaze read, or accepts a calibration the device did not keep.**
  Three rig findings (2026-09-01). The per-target calibration call switches
  the device's free-run sampling off and re-points its sample ring at a small
  buffer of its own; the next per-trial drain then saved from a read pointer
  into a ring that no longer existed, and never returned — Windows killed the
  session as "not responding". Every drain now checks the ring is the one it
  was armed with and re-arms it otherwise, and `calibrate()` drains first
  and re-arms after. A gaze read is a USB round trip with a 20-40 ms tail on
  one call in five; read on the render thread it dropped ~30 frames per
  trial at 120 Hz, so the backend now reads on its own thread (one lock
  around every call into libdpx) and a frame only copies the newest report,
  discarding one older than 100 ms. The calibration screen shows whether the
  camera is fitting a pupil in each eye, refuses to accept a target while it
  is not, and checks `isDeviceCalibrated()` after the fit — a "calibrated"
  session with no eye in the image was the tracking-lost sentinel forever.
  The PsychoPy backend also claims the foreground and presents the
  instructions twice: the dashboard's browser window took focus as they were
  drawn and that frame never reached the panel.
- **The TRACKPixx3 backend brings the device up itself, and connects on a
  real rig.** pypixxlib 1.9.2's `TRACKPixx3.open()` leaves libdpx addressing
  the camera controller and then writes the video-overlay register, which is
  on the DATAPixx3 — `DPX_ERR_SETREG16_ADDR_RANGE` on every healthy rig, so
  `check-rig` and every session failed at `connect()`. The backend now does
  what VPixx's own demos do: constructs the tracker object (which opens the
  link), re-selects the DATAPixx3, hides the overlay, wakes the tracker and
  flushes the register cache, then reads libdpx's sticky error flag, which
  its free functions never raise on their own. The missing-package error and
  `docs/getting-started.md` now say where pypixxlib actually comes from: the
  Software Tools installer leaves a source archive on the rig, and that
  archive is what to `pip install` into each environment.

- **A simulated display now paces its frames on time.** `SimulatedDisplay`
  slept the whole remainder of each frame, and a sleep returns one scheduler
  tick late; on Windows that tick is 15.6 ms, so a 60 Hz simulation ran at
  31 ms a frame, every flip was flagged as dropped, and a ten-minute
  rehearsal wrote a megabyte of warnings. It now sleeps to within 2 ms of
  the deadline and polls the clock for the rest, and asks Windows for 1 ms
  ticks while it is open (`timeBeginPeriod`, released on `close()`), which
  is what Python 3.10's `time.sleep` needs to be finer than a frame.

Findings of a post-merge adversarial review of the movie-mode PR, all
verified before fixing:

- **A fresh scaffold now answers every command it prints.** The template task
  implements `demo_views`, `movie_clips` and `simulation` (the smallest
  honest version of each, to build on) — previously `alhazen new`'s own
  closing message and the rig headers printed `--mode demo/movie/simulate`
  commands that all exited with "implement X to use this". The slow
  acceptance test now runs simulate and movie on a scaffolded package.
- **Movie mode's failure modes got loud and clean.** imageio-without-ffmpeg
  (the exact partial install the extra exists to prevent) and a missing
  Pillow now raise the ConfigError naming `alhazen-vision[movie]` instead of
  raw backend tracebacks, and Pillow is pinned in the extra (`>=10.1`, which
  the caption APIs need). A clip that changes frame shape — or switches
  luminance/RGB — mid-stream is refused naming the clip instead of dying in
  the encoder or a numpy broadcast. Every error path now deletes the
  truncated `.mp4` it would otherwise leave looking like an encoder problem.
- **Sheet captions render honestly.** Ink on an RGB sheet is grey, not the
  red that Pillow makes of an integer fill on a multi-band image; fonts are
  fitted to every caption's rendered width (not the longest character
  count); a caption that cannot fit even at the smallest size is elided with
  a visible ellipsis instead of overflowing into the neighbouring panel.
- **An experiment's own `NotImplementedError`, raised from a frames
  generator mid-recording, surfaces with its traceback** instead of being
  misreported as "declares no movie clips" (exit 2) — the missing-hook case
  is now told apart by identity.
- **Prompting for `--sub`/`--ses` requires a terminal.** With stdin not a TTY
  (nohup, CI, a batch script) the missing flags are refused up front with
  exit 2, where `input()` previously blocked forever or died in a raw
  EOFError after the rig config had already loaded.
- **The one order-dependent test in the suite** (the lazy-import invariant on
  `SubjectKeyboard`) now checks its invariant in a subprocess, so it can no
  longer fail when an earlier test has legitimately loaded psychopy.

### Changed

- **The distribution is now `alhazen-vision`** (`pip install alhazen-vision`).
  The import is unchanged — still `import alhazen`, still the `alhazen`
  command — and only the name pip resolves has moved.

  `alhazen` on PyPI is an unrelated project: a cognitive-modelling framework
  from CMU, currently 1.4.1, which has held the name far longer than this one
  has existed. That was not a latent risk, it was a live bug in three places.
  `pip install alhazen` in the README and getting-started guide installed
  their package. Both experiment packages declared `dependencies =
  ["alhazen"]`, so a clean install fetched it — reproduced in a fresh venv,
  which resolved 1.4.1 and a single-module `site-packages/alhazen.py`. And
  `get_version()` looks the *distribution* up by name, so with theirs
  installed it returned **their** version number, which is then stamped into
  the manifest of every run.

  A developer machine never saw any of it, because the right package is
  already installed editable and pip leaves a satisfied requirement alone.
  Which is exactly why it survived: the only environments that meet it are
  clean ones, and until now nothing built one.

  `version.DISTRIBUTION` now names it in one place, `alhazen new` writes the
  correct dependency into every experiment it scaffolds, and the release
  workflow's post-publish smoke test installs the right package.

  Four cases in `tests/unit/test_distribution_identity.py` pin it: that
  `import alhazen` is provided by this distribution and no other, that the
  reported version is this distribution's own and not `unknown`, that no
  document tells a reader to install the bare name, and that the scaffold
  hands new experiments the right dependency — the one that propagates, since
  every future experiment inherits that line.

### Added

- **The five modes** (`alhazen.modes`, `alhazen run --mode`) — `measure`,
  `demo`, `simulate`, `test` and `run`. Every experiment needs the same five
  ways of being started, and before this each one wrote them again: two of
  alhazen's own had independently grown a stimulus viewer, an autopilot, a
  ruler check and a hand-edited config for short runs. `run`, `test` and
  `simulate` are one code path with different arguments — a rehearsal that
  went through different wiring would rehearse the wrong thing — differing
  only in the trial counts, who supplies the gaze and keypresses, and which
  directory the data lands in. See [docs/modes.md](docs/modes.md).
- **`test` mode** runs the whole experiment with the trial counts turned
  down, so it can be sat through once before a subject does. It finds every
  `SchedulerConfig` by type rather than by field name, because experiments do
  not agree on the name and a reduction that silently did nothing would run
  the full session when a short one was asked for. It prints every number it
  changed, and leaves block structure alone. Data goes to a sibling
  `<data_root>-rehearsal` directory: rehearsals write real files in real
  formats, which is the point, and is exactly why they must not land where an
  analysis looks for subjects.
- **`measure` mode** measures what a rig actually does — refresh rate and
  frame timing, geometry, response-key latency and poll lag, eye-tracker
  accuracy — and writes a timestamped report beside the rig config. It is the
  one mode that needs no task: requiring one would mean a rig could not be
  checked until an experiment was installed on it.
- **`demo` mode** shows the stimulus with no trials and no data, through the
  window a session opens rather than a hand-rolled one — so it inherits the
  framebuffer check, the registered monitor and the measured gamma. Both
  experiment packages had the hand-rolled version, which on a Retina Mac
  meant judging the stimulus at half its designed size.
- **`Task.demo_views`, `Task.demo_controls`, `Task.simulation`** — the hooks
  the new modes ask an experiment for. All optional; a mode that has no answer
  says so plainly rather than improvising something that is not the
  experiment.
- **`alhazen.cli.modes.run_experiment`** — one entry point for an experiment
  package's own `run.py`, which drops to naming its task class and where its
  subject wording comes from. It shares its flags with `alhazen run` through
  the same code.

### Changed

- **The pause screen is a menu.** It was one line naming three keys, written
  when three keys were all a session had; a session now also has a reward
  pump, a curriculum whose stage can be moved and a tracker that can be
  recalibrated, and none of them appeared on it. It is now built at each pause
  from what the session actually has wired — a rig with no pump lists no
  reward key — and the live keys are read out of the real keymap, so a rebound
  key shows its own binding. It is orange on a bordered panel, because a
  stopped session has to be distinguishable from a running one across a room;
  an involuntary pause (so far only a reward failure) is a different colour
  and leads with what went wrong.
- **The pause menu stays up until resume or quit.** Calibrating used to
  calibrate and then resume in one press, so calibrating *and* rewarding took
  two pauses.
- **The dashboard pause path draws the menu too.** It drew nothing before, so
  turning the dashboard on silently removed the only thing the person standing
  at the rig could see.
- **`DisplayBackend.show_menu`** joins the display protocol. A message is the
  session talking to the subject; a menu is the session stopped and waiting
  for the experimenter, and the colour that carries that distinction is
  required rather than defaulted.

### Deprecated

- **`session.pause_menu`** — use `build_pause_menu` with `run_pause_menu`.
  It still works and warns; removed in 1.2. The old seam cannot express the
  colour or the controls that depend on what a session has wired, because a
  `show_message` callable expresses neither.

- **`alhazen monitor`** — register a rig's monitor with PsychoPy. `monitor
  register --rig <yaml>` writes the config's geometry, and any gamma from
  `alhazen calibrate gamma`, into PsychoPy's own monitor database under the
  new `monitor.name` field; `monitor list` shows what PsychoPy knows on this
  machine; `monitor show --rig <yaml>` compares one rig against it and exits
  non-zero when they disagree. Sessions open their window against the
  registered monitor, so a calibration measured in PsychoPy's Monitor Center
  is inherited rather than ignored, and a registration that has drifted from
  the rig config is a loud error instead of a window whose deg/px model
  differs from the one placing the stimuli. `check-rig` grew a `monitor`
  check for the same comparison.
- **`MonitorConfig.name`** (default `"alhazen"`) — the name the panel is
  registered under. A machine driving more than one panel needs one name per
  rig config.

## 1.0.0 - 2026-08-27

The first release, and the point from which those three contracts hold.

### The core

- **Trial engine** — one loop per displayed frame. Visual events are stamped
  by the flip that showed them, on one session clock; dropped frames are
  detected against the measured refresh rate and handled by a configured
  policy (log, warn, mark the trial, or abort the run). Phases touch only the
  trial context, so the whole engine runs against fakes.
- **Experiment-declared vocabulary** — events, outcomes and trial structure
  belong to the experiment, not the framework. The engine interprets only
  `completed`, and derived measures come from the task's own `score` hook.
- **Task framework** — one `Task` subclass per experiment declares its params,
  events, outcomes and reward policy; `build_session(task=...)` reads them
  all. Reward policy is data: an outcome absent from the table earns nothing,
  so a typo pays out on nothing rather than on the wrong trials.
- **Phase library** — `AcquireFixation`, `HoldFixation`, `StimulusResponse`,
  `LandingCheck`, `ResponseWindow`, `AdjustmentLoop`, `FrameSequence`,
  `Blank`, `Feedback`.
- **Schedulers** — constant stimuli, up-down staircases (single and
  interleaved), QUEST+, adjustment, and `BlockPlan` over any of them. Every
  scheduler re-serves a condition whose trial did not complete.
- **Training curricula** — stages that override task parameters, ramps,
  promotion and demotion criteria, and per-subject state that persists
  between sessions.
- **Reproducibility** — one resolved seed spawned into named streams, a
  config snapshot written before trial 1, and a hashed manifest of everything
  the run produced.

### Devices

- **Eye trackers** — EyeLink and VPixx TRACKPixx3 behind one protocol.
  `eyetracker.backend` selects between them and nothing else in a config or a
  task changes: gaze reaches phases as centered px on the session clock from
  either, and an unverifiable position is `None` from either. `mouse_sim` and
  a deterministic `scripted` double round out the set.
- **TRACKPixx3 specifics** — it is always binocular, so `eyetracker.eye` picks
  which eye a sample carries (`left`, `right` or `average`, the last requiring
  both eyes tracked); blinks are recognised from the device's own `±9000`
  report; and calibration walks an `HV5`/`HV9`/`HV13` grid over
  `calibration_area` in the session's own window, since there is no Host PC to
  run it.
- **`EyeTracker.shutdown()` takes the run's recording path**, not specifically
  an EDF. Only the directory and base name are a promise; the suffix belongs
  to the backend, so a ViewPixx run directory holds `<base>_gaze.csv` and
  `<base>_gaze-messages.csv` rather than an `.edf`. The parameter is
  positional-only, so a backend may name it after whatever it records.
- **Reward and sync** — NI-DAQ reward pump and TTL sync, one digital line per
  configured event name, each with a simulated twin. A failed delivery is
  recorded and surfaced rather than discarding a completed trial's data.
- **Photodiode** — a patch that turns white on exactly the flip carrying a
  configured event, which is what makes a visual timestamp auditable rather
  than merely claimed.
- **Backend-specific config fields are checked against the backend** — an
  `eyetracker` block setting `host_ip` on a `viewpixx` rig, or `eye` on an
  `eyelink` one, is a load-time error instead of a value that silently does
  nothing.
- **`alhazen check-rig`** constructs the same backends a session would, so a
  clean check predicts a working session.

### Data and analysis

- **Run directories** — trials, events and frame timings, a config snapshot,
  a session log and a manifest. Overwriting an existing run is refused, and
  anything later written into a run directory rewrites its manifest.
- **Experiment database** — every run mirrored into a per-experiment SQLite
  file carrying frame-level gaze and responses, artifacts, sparse device
  samples, compressed dense ephys streams, and cross-device frame queries.
  Configured by `database:` on the rig. It is a mirror: the run directories
  remain the record.
- **Analysis** — readers for SpikeGLX, Kilosort and EyeLink ASC; TTL clock
  alignment stored as its own artifact; photodiode-measured display latency;
  and `alhazen report`. Analysis reads a session's own configuration rather
  than a hand-typed copy of it.
- **Results bundles** — an output directory plus a manifest recording every
  input with its hash, the parameters, and the version that produced them.

### Interfaces

- **Live dashboard** — an isolated local browser process updates standard and
  task-declared plots between trials, saves a self-contained final view, and
  unlocks controls only after a keyboard pause, so it cannot steal focus
  during a trial. Panel data is computed in Python over the whole session and
  thinned to a bounded number of points, so a snapshot costs the same on trial
  4000 as on trial 40.
- **Scenes** — illusion-studio scene JSON rendered unchanged inside trials,
  with a parsed (never `eval`'d) expression language and a headless renderer.
- **CLI** — `new`, `run`, `validate`, `check-rig`, `calibrate`, `report`.
- **Automated mode** — `run.py --auto` drives a real task through the real
  engine with a scripted participant, so an unattended machine can run a
  visible demonstration.
