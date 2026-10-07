# Calibration targets: pictures and pulsation

> **Status: implemented locally** on branch `feature/calibration-targets`
> (not pushed, not released). Written against alhazen 2.10.0 + main `ce9d744`.

## 1. What was asked

The owner (2026-10-06): choose between the standard calibration target, the
pictures from `sh4r11f/realtime-rdk` (`assets/calibration`, commit `0fe02e1`),
or a random selection of them; and a mode in which the target — standard or
picture — pulsates (expands and contracts) instead of standing still, as
Realtime RDK's targets did.

## 2. Requirements, as examples

- A rig that says nothing about the target calibrates exactly as before: the
  24 px black disc with a hole, drawn once per target, no new check, no
  picture loaded. (Every shipped rig; `TestTheDefaultIsWhatCalibrationAlwaysDrew`.)
- `appearance: images, images: [monkey_1, food_3]` shows monkey_1 at the first
  target, food_3 at the next, monkey_1 again at the third; one name shows that
  picture every time.
- `appearance: random_images` (optionally restricted by `images`) shows a
  random picture per target: a shuffled deck dealt without replacement, never
  the same picture twice in a row unless the pool has one, from the session
  seed's own `calibration_target` stream. The pictures shown are recorded on
  the CALIBRATION event (`targets_shown`: ordinal, position, picture, time).
- `motion: pulse` makes either kind swell and shrink about a fixed centre:
  size = still size × (min + (max − min)(1 − cos 2π·rate·t)/2), t = session
  clock since the target appeared. Defaults 1 Hz, 1.0–1.4× (Realtime RDK's).
  At 60, 144 or 240 Hz the size at a moment is the same.
- While one target is up, its picture and pulse never change; a new point (or
  a target taken down and put up again) is a new target.
- If a picture is missing, altered (SHA-256), not RGBA or the wrong size, or
  the largest target would be cut off at the outermost point, the session is
  refused when the tracker is configured (session build) and `check-rig`
  fails — never mid-calibration. Bad names, combinations and ranges are
  refused when the rig file loads.
- Point layout, acceptance (keys, auto-advance counts), gaze sampling, the
  fit, beeps and validation criteria are unchanged.

Change scenario: adding another picture set is a new manifest entry set and
files under `calibration_images/`; nothing in the backends changes.

## 3. Change map

| Where | What | Contract |
|---|---|---|
| `src/alhazen/calibration_images/` (new) | 38 PNGs byte for byte, `manifest.json` (SHA-256, size, dims, source path, git blob), README | package data (`pyproject.toml`) |
| `config/calibration_images.py` (new) | names, paths, verified bytes; only manifest names reach a file | internal |
| `config/models.py` | `CalibrationTargetConfig`, `CalibrationPulseConfig`, `EyeTrackerConfig.calibration_target` (default = standard, still), `with_calibration_target` | rig YAML schema (additive) |
| `core/rng.py` | `calibration_target` appended to `STREAMS`; `named_stream` | append-only contract (baseline updated) |
| `devices/eyetracker/calibration_targets.py` (new) | pulse, sizes, fit check, picture deck, decoding, `CalibrationTargets`, `TargetPresenter` | internal |
| `devices/eyetracker/calibration.py` | EyeLink graphics draw through the presenter; pulsating target redrawn in `get_input_key` | internal (`make_calibration_graphics` lost its unused `target_size_px`) |
| `devices/eyetracker/eyelink.py`, `viewpixx.py` | `calibration_rng=` and the optional capability `set_calibration_rng`; `prepare()` first in `configure()`; viewpixx walk redraws every frame between status refreshes when pulsing | backend constructors (additive keyword), optional capability (protocol.py) |
| `devices/eyetracker/protocol.py` | `TargetShown`; `CalibrationResult.target_style`, `.shown` | additive dataclass fields |
| `session/builder.py`, `session/eyetracker.py`, `session/checks.py` | stream to the tracker (`set_calibration_rng`, when the tracker offers it); CALIBRATION payload gains `target_style`, `targets_shown` (only from a tracker that draws one); check-rig verifies the target | event payload (additive) |
| `cli/main.py`, `modes/__init__.py` | `--calibration-target/-images/-motion` (run and test only, not with `--mouse`) | CLI (additive) |
| `cli/workspace.py`, `dashboard.py`, assets | probe records the project's offer; Launch `calibration_target`; `/calibration-picture`; Rig-section controls and preview | workspace API (additive) |

What this map decided: no refactor first; one change; the one-way decision is
shipping the private pictures in a public package (section 7).

## 4. Design

| Module | Secret it hides | Interface | Price | Callers must not rely on |
|---|---|---|---|---|
| `config.calibration_images` | where the pictures live, the manifest format | `image_names()`, `image_path(name)`, `verified_bytes(name)` | one more package-data folder | file names other than `<name>.png` |
| `CalibrationTargets` | picture decoding, deck order, the record of what was shown | `prepare(screen, area)`, `presenter(...)`, `begin_procedure()`, `shown_since_begin()`, `style` | per-tracker state across calibrations | the deck restarting on recalibration (it continues) |
| `TargetPresenter` | how a standard disc or picture is drawn, scaled, centred | `show(pos)`, `draw()`, `hide()`, `animated` | stimuli made per procedure | the stimulus objects themselves |

Both backends draw through the presenter, so the target looks the same on an
EyeLink and a TRACKPixx3. The EyeLink's Host PC still decides when and where
targets appear; pylink polls `get_input_key` continuously between them, which
is where a pulsating target gets its frames (as SR Research's animated-target
example, and Realtime RDK's graphics, do). The TRACKPixx3 walk keeps its one
eye-status read per `STATUS_REFRESH_S`; when pulsing, it polls keys without
waiting and flips between reads instead of one blocking wait, so its refresh
rhythm, auto-advance count and live-monitor report are unchanged (to within a
frame per refresh; `test_auto_advance_accepts_after_the_same_count_of_refreshes`).

Which procedures share the appearance follows the existing policy: the
calibration target is the calibration's (both backends, every recalibration,
and on an EyeLink also the Host PC's own validation and drift check on its
setup screen, which draw through the same graphics). alhazen's own validation
and drift correction (`procedures.py`) keep their 0.5 deg fixation disc, as
before; nothing about them changed.

## 5. Failure analysis

- Invalid config: refused at load with the field and the fix (models.py).
- Missing/altered/corrupt/non-RGBA picture, no Pillow: TrackerError at
  `configure()` (session build) and a failed check-rig line; never a blank or
  substituted target.
- Target larger than the room at the outermost point: same, with the largest
  calibration_area that fits.
- Path misuse: names are `[a-z][a-z0-9_]*` and must be in the manifest;
  the workspace serves only names its project's alhazen listed, from that
  folder, behind the token.
- A flag the session cannot honour (simulate, demo, movie, measure, --mouse, a
  rig with no drawing tracker, a script): refused before anything loads.
- Random order with no session stream (a tracker built outside a session):
  draws its own seed, logs it and names it in `target_style`.

## 6. Assumptions ledger

- Picture size 2.5 deg (longer side, still) by default: Realtime RDK drew
  100 px, about 2.7 deg on the lab rig. The standard target stays 24 px.
- Pulse bounds: rate 0.1–2 Hz; scale 0.5–2.0×; max − min ≥ 0.05.
- "No immediate repeat" for random pictures is an implementation choice, not
  the owner's requirement.
- A revisited point (BACKSPACE, the Host PC redoing a point) is a new target:
  it gets the next picture.

## 7. Evolution and approval

All additions are backward compatible: old rig files load unchanged, and the
CALIBRATION event of a tracker that draws no target is unchanged. The
pictures come from a private repository and their origin and licence beyond
it are not established; alhazen is public. **Pushing this branch publishes
them**, so that needs the owner's decision, separately from merging the code.
