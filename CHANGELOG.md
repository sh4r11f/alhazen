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
