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

## Unreleased

### Added

- **VPixx TRACKPixx3 eye tracker** — `eyetracker.backend: viewpixx` selects it
  in place of `eyelink`, and nothing else in a config or a task changes.
  Gaze arrives on the session clock in the same frame as every other backend
  (the device's centered, y-up px are converted in the backend), blinks are
  recognised from the device's own `±9000` report, and `eyetracker.eye`
  picks which eye of a binocular tracker a sample carries (`left`, `right`
  or `average`, the last requiring both eyes tracked). Calibration draws its
  target grid — `HV5`, `HV9` or `HV13` over `calibration_area` — in the
  session's own window, since the device has no Host PC to run it.
  `alhazen check-rig` opens and releases it like any other tracker.
- **Backend-specific config fields are checked against the backend** — an
  `eyetracker` block that sets `host_ip` on a `viewpixx` rig, or `eye` on an
  `eyelink` one, is now a load-time error instead of a value that silently
  does nothing.

### Changed

- **`EyeTracker.shutdown()`'s argument is the run's recording path, not
  specifically an EDF.** Only the directory and the base name are a promise;
  the suffix belongs to the backend, and a ViewPixx run directory holds
  `<base>_gaze.csv` (VPixx's own sample format) and `<base>_gaze-messages.csv`
  (each tracker message stamped on both the device and session clocks — the
  alignment an EDF carries internally) rather than an `.edf`. The parameter
  is now positional-only, so an experiment package implementing this protocol
  may name it after whatever it records.

## 1.0.0

The first stable release. The run-directory layout, `RESERVED_EVENTS` and
`core.rng.STREAMS` are compatibility contracts from here on: they change only
in a major version, with a documented migration.

### Added

- **Live dashboard** — an isolated local browser process updates standard and
  task-declared plots after every trial, saves a self-contained final view,
  and exposes authenticated controls only after a keyboard pause.
- **Experiment database** — every run is mirrored into a per-experiment
  SQLite database with frame-level gaze/response inputs, artifacts, sparse
  device samples, compressed dense ephys streams, and cross-device frame
  queries. Configured by `database:` on the rig (`enabled`,
  `artifact_max_bytes`).
- **Automated mode** — an example's `run.py --auto` wires an
  `AutomatedGazeTracker` and `AutomatedResponse` and passes
  `build_session(auto_start=True)`, driving a real task through the real
  engine with a scripted participant, so an unattended machine can run a
  *visible* demonstration of an experiment.
- **Task framework** — one `Task` subclass per experiment declares its
  params, events, outcomes and reward policy; `build_session(task=...)` reads
  them all.
- **Phase library** — `AcquireFixation`, `HoldFixation`, `StimulusResponse`,
  `LandingCheck`, `ResponseWindow`, `AdjustmentLoop`, `FrameSequence`,
  `Blank`, `Feedback`.
- **Schedulers** — constant stimuli, up-down staircases (single and
  interleaved), QUEST+, adjustment, and `BlockPlan` over any of them.
- **Devices** — EyeLink / mouse-sim / scripted eye trackers, NI-DAQ reward and
  TTL sync, a subject keyboard, and recording-system pointers, each behind a
  protocol with a simulated twin.
- **Training curricula** — stages that override task parameters, ramps,
  promotion and demotion criteria, and per-subject state that persists
  between sessions.
- **Analysis** — readers for SpikeGLX, Kilosort and EyeLink ASC; TTL clock
  alignment with a stored artifact; photodiode-measured display latency;
  `alhazen report`.
- **Scenes** — illusion-studio scene JSON rendered inside trials, with a
  bit-exact expression language and a headless renderer.
- **CLI** — `new`, `run`, `validate`, `check-rig`, `calibrate`, `report`.

### Changed

- **`trials.csv` gains a `completed` column** (additive; existing columns and
  their meanings are unchanged). The engine stamps the outcome's own
  `completed` flag on every row. Incomplete outcomes always wrote rows, but
  the flag lived only inside the `TRIAL_END` event payload, so every reader
  downstream had to guess completion from the outcome *name* — which it
  cannot do, because outcome names belong to the experiment. `alhazen
  report`'s `completed_rate` and the dashboard's `completed_only` filter both
  read the column; the report falls back to the old heuristic for runs
  written before it existed.
- **`load_run` returns pandas DataFrames** with real dtypes for `trials`,
  `events` and `frames`, instead of lists of string dicts. `row["success"] ==
  "False"` being truthy was a live footgun for every consumer.
- **A scene declares its canvas in the format's own top-level `width` and
  `height`.** alhazen had invented a `canvas: {width, height}` block; scenes
  written against it still load, with a warning. The scene is letterboxed onto
  the screen and the scale factor is recorded on the stimulus.
- **`alhazen calibrate ruler` draws the bar** on a real display, instead of
  only printing what it should measure. `build_session` applies a stored gamma
  fit when the display opens.
- **The scaffolded `rig-lab.yaml` loads.** It emitted `devices:` followed only
  by comments — YAML for `devices: null`, which fails validation — so the
  first config a new user ran on their rig was invalid.
- **`EyeTracker.configure` takes the session clock** (`configure(screen,
  clock)`). Every backend gets it at the same seam, so none can quietly stamp
  gaze from a second clock.
- **`ExperimentDatabase` moved to `alhazen.session.database`.** It is still
  exported from `alhazen`; what changed is that `alhazen.data` is pure disk
  again and the import layering points one way.
- **The database's `run_id` carries the run's date**, and `artifacts` gains a
  `sha256` column with a nullable `content`. The schema is unreleased, so
  there is no migration.
- **`dashboard.max_rows`** caps how many trials and events each between-trial
  update carries; the state saved at teardown is still complete.
- **`RESERVED_EVENTS` gains `NO_REWARD`** (append-only, as the contract
  allows). The runner emits it when a session has a reward policy and a
  completed outcome pays nothing.
- **The dashboard's plots are rebuilt.** Each quantity now gets the mark that
  answers the question asked of it: reward is a cumulative curve over trials
  with failed deliveries marked (a session total is a number, and is shown as
  one), performance is a running proportion with a 95% Wilson band, outcomes
  and responses are horizontal bars with counts and shares, reaction times are
  Freedman–Diaconis bins on a robust axis with the median marked, landings are
  drawn at equal aspect, and group means carry standard-error bars and their
  n. Axes carry units read off the column name, every chart has a table view,
  and the page follows the reader's light/dark setting with a validated
  palette in both.
- **Panel data is computed in Python** (`alhazen.dashboard.panels`), over the
  whole session rather than the `max_rows` echo, and thinned to at most 180
  points per series. The page renders; it no longer analyses. Two new panel
  kinds, `performance` and `stat`, and two new panel fields, `unit` and `agg`.
  `DashboardPanel` and `DashboardSpec` are otherwise unchanged.
- **The dashboard page's source is three files** under
  `alhazen/dashboard/assets/`, inlined when the page is served or saved,
  instead of one minified string literal. `runtime._INDEX_HTML` is gone;
  `runtime.page_html(static_state)` replaces it.
- **Dashboard panels are measured after they are laid out.** Every chart was
  drawn while its card was still detached from the document, where
  `clientWidth` is 0, so all of them fell back to one hard-coded width — too
  small inside a wide card and overflowing a narrow one. Panels are now built,
  attached, and then drawn, and a resize redraws the plots without rebuilding
  the cards. Charts are laid out two to a row (one on a narrow window) and
  their height follows their width.
- **New `vectors` panel kind**, with `origin_x`/`origin_y` on `DashboardPanel`:
  every response as a displacement from where the eye started, all trials on
  one origin, on a polar grid of amplitude rings. It ships as a default panel
  ("Landing relative to fixation"); pointed at the target columns instead, the
  same panel is an endpoint-error plot.
- **An experiment database written by an older schema is refused with a
  message that names the file and the fix**, instead of opening cleanly and
  failing at the first insert with `table runs has no column named date`.
  `SCHEMA_VERSION` is 2 (it was never bumped when `runs.date` and
  `artifacts.sha256` were added, and the version row was seeded with a
  hard-coded 1). A shape change without a version bump is caught too. Nothing
  is migrated and nothing is lost: the database is a mirror, and the run
  directories and each subject's `training_state.yaml` are the record — move
  or delete the file and it is rebuilt from the next session onward.
- **`StimulusResponse` records where the saccade started.** On the frame gaze
  leaves the window it writes `<depart_region>_x_dva`/`_y_dva` from the last
  sample verifiably inside it. A displacement measured from an assumed origin
  is an assumption: the eye sits where the subject's fixation and the
  calibration put it, near the fixation point and not on it. A trial whose
  origin was never verified is left without the columns rather than given the
  screen centre.
- **`LandingCheck` records `endpoint_error_dva`**, the distance from the
  region's centre to where gaze arrived. The coordinates alone cannot be
  averaged across a condition that moves the target — a task with left and
  right targets averages its endpoints to roughly zero and reports perfect
  aim.
- **Dashboard panels know the experiment's conditions.** The runner collects
  the condition factors from the conditions the paradigm served and passes
  them through: the spatial panels are coloured by the first factor, and each
  of the first two factors earns an `Accuracy by <factor>` and a `Landing
  error by <factor>` panel. Numeric levels take one hue light-to-dark
  (they are ordered); named levels take separate hues; levels past the
  validated set fold into one grey series and the panel says how many.
- **New `grouped_rate` panel kind** — proportion correct per condition level
  with a 95% Wilson interval — and `color_by`/`style` on `DashboardPanel`.
  `grouped_mean` draws as dots ± SEM by default or as bars with
  `style="bars"`. `DashboardSpec.resolved_panels` is now a method taking the
  session's condition fields, not a property.
- **A landing plot's mean marker is per condition level.** One mean over every
  point in a task with left and right targets lands between the two clusters,
  on the fixation point, where nothing landed — and it was drawn there.
- **The dashboard is restyled for reading, not for browsing.** Okabe-Ito
  categorical colours (the colour-vision-deficiency-safe set scientific
  figures use; the same three hexes clear every gate in both themes under the
  all-pairs test), a validated single-hue ramp per theme for ordered condition
  levels, one neutral grotesque at journal sizes, and a printed figure's
  chrome: two spines, outward tick marks, and no gridlines.
- **Panels are grouped and filterable.** `DashboardPanel` gains `section`,
  defaulting from the kind to Session / Behaviour / Gaze / Conditions. The
  page grew a sidebar that lists the groups with counts and shows one at a
  time; the session controls and the theme toggle moved into it. The choice is
  remembered per browser.
- **Panels in a row are the same size.** Every chart on a page shares one
  drawing box, bar charts spread through it instead of sizing to their own
  content, and each card reserves the same room for a legend and a caveat
  line — so a row reads as a plate of figures rather than a ragged pile.
- **The run manifest records forward-slash paths on every platform.** A run
  written on Windows recorded `figures\dashboard.html`, which no other machine
  could verify — and a manifest that only reads on the OS that wrote it is not
  a record.
- **`figures/dashboard.html` and `dashboard_state.json` are written as UTF-8**
  rather than in the platform's preferred encoding. The page declares that
  charset and carries µ, Δ and — in its own labels; on Windows the write
  failed outright.
- **A simulated display reports the rate it paces at.** It timed its own
  `time.sleep` calls, so on a loaded machine a 60 Hz simulation measured 20 Hz
  and every session refused to start. A deliberately mismatched
  `frame_period_s` still disagrees with the nominal rate and still fails; what
  is gone is the dependence on how accurately the host can sleep.
- **The dashboard's start timeout is 20 seconds, not 5.** It exists to catch a
  server that will never start, not to race a cold interpreter: a spawned
  child re-imports pydantic before it can bind.
- **The dashboard server binds without a reverse DNS lookup.**
  `HTTPServer.server_bind` calls `socket.getfqdn("127.0.0.1")` to fill in a
  `server_name` this server never reads; on a machine with a slow or absent
  resolver — a rig with no network, most of all — that blocked for tens of
  seconds, and the session failed to start with a timeout that blamed the
  server. The failure message now also says whether the child is still
  running or what code it exited with.
