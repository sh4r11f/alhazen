# Changelog

Notable changes, newest first. This project follows [semantic
versioning](https://semver.org): the public API is everything exported from
`alhazen` and everything documented in the module reference. Three things are
also compatibility contracts, because they live on disk and outlast any one
version:

- **`core.rng.STREAMS`** is append-only. Removing or reordering a stream
  changes what every seed produces.
- **`RESERVED_EVENTS`** may gain names, never lose them; an analysis reads
  them out of old data.
- **The run-directory layout** (file names, column meanings, the manifest and
  snapshot formats) changes only in a major version, with a documented
  migration.

## 1.0.0

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
