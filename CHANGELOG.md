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
