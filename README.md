# alhazen

A framework for building and running vision science experiments.

One tested core provides the trial engine, the device layer, the
configuration and reproducibility machinery, and the session data management.
Each experiment is a thin package supplying its own stimuli, phases, configs
and analysis.

Open the experiment workspace with `alhazen dashboard`. Add downstream
project folders, select a rig, edit parameters, and launch previews, movies
or any of the six experiment modes. Images, playable movies, logs and run
history live in the browser. See [the workspace guide](docs/workspace.md).

```bash
pip install alhazen-vision
alhazen new my_experiment && cd my_experiment
pip install -e ".[dev]"
pytest                                                    # no display needed
python run.py --task my-experiment --mode simulate --rig lab --headless
```

`--task my-experiment` names the scaffold's one task: every session names its
task, and from alhazen 3.0 a command without `--task` is refused.
`--rig lab` is the scaffold's `configs/rig-lab.yaml`: a rig is named by its
file (`rig-<name>.yaml`), found in the experiment's `configs/` first and then
among the rigs alhazen ships for every experiment to share, and a path works
as well. `alhazen rigs` lists them ([Rigs](docs/rigs.md)).

That last command runs a complete session on the rig's own config, with
nobody in the chair and no window open, and writes a real run directory —
trials, events and frame timings, a config snapshot, a session log and a
hashed manifest — on a laptop with no rig attached. Every mode runs on every
rig file: the mode decides what to do with the machine, not the file. The one
exception is real data: run mode refuses a development rig (`real_data:
false`, as the shared laptop says) before anything is written.

Every run is also mirrored into `data/experiment.sqlite3`, where subjects,
sessions, trials, displayed frames, gaze/responses, artifacts and aligned
device channels can be queried together.

**Documentation:** <https://sh4r11f.github.io/alhazen/> · **Concepts:**
[docs/architecture.md](docs/architecture.md) · **Contributing:**
[CONTRIBUTING.md](CONTRIBUTING.md)

Working on alhazen itself: install [uv](https://docs.astral.sh/uv/), then
`uv sync` in the clone builds the locked development environment (Python
3.11, `uv.lock`) and `uv run pytest` runs the suite; CONTRIBUTING.md has
every gate.

## Experiment Hub (development branch)

The optional Experiment Hub adds accounts, a public catalogue of pinned source releases,
private libraries, downloads to a rig, and opt-in private session uploads. Experiments
still run locally, using the existing rig, calibration, reward, training and recording
workflow. The default `alhazen dashboard` remains the local workspace; the hub reading
and connection surface is available with `alhazen dashboard --hub`.

This branch is a development build, not a released or deployed service. Pilot registration
is invite-gated. Published code does not publish collected data, and downloaded code is
not executed until its exact release is explicitly trusted. Local files are kept after
upload; a verified primary copy is not an independent backup.

Experiment descriptions and task guides are versioned with their source: authored Methods,
parameter meanings and defaults, stimulus schematics, timelines, outcomes and event notes.
The source-checked fixation scaffold is an example, not a catalogue of validated paper
replications. Native bring-your-own-key AI authoring is a future phase, not an enabled feature.

- [Design and assumptions](docs/hub/design.md)
- [Service and operator guide](docs/hub/server.md)
- [Rig and offline workflow](docs/hub/rig.md)
- [Scientific documentation format](docs/hub/documentation.md)
- [Source package safety and recovery](docs/hub/packages.md)
- [Real HTTP integration test](docs/hub/integration-test.md)
- [Verification and deployment boundaries](docs/hub/verification.md)

## What it gives an experiment

- **A frame loop that is honest about time.** Visual events are stamped by
  the flip that showed them, on one clock; dropped frames are detected and
  acted on. A photodiode patch marks the exact frame an event was shown,
  which makes those timestamps auditable rather than merely claimed.
- **Hardware behind protocols.** EyeLink and VPixx TRACKPixx3 eye trackers
  (one word in the rig config picks between them), NI-DAQ reward and TTL
  sync, subject keyboards and recording systems, each with a simulated twin — so the whole
  test suite runs with none of them installed, and `alhazen check-rig`
  exercises the real ones before a subject arrives.
- **A phase library and five schedulers.** Fixation, hold, saccade, response,
  adjustment and frame-timeline phases; constant stimuli, staircases, QUEST+,
  adjustment and blocks. An experiment composes them rather than writing a
  trial loop.
- **Training curricula as data.** Named stages that override task parameters,
  with promotion criteria and per-subject state that persists between
  sessions.
- **Analysis that reads a session's own configuration.** TTL clock alignment
  with a stored artifact, photodiode-measured display latency, and
  `alhazen report`.
- **Live spikes, behind a device seam.** A `SpikeSource` device reads the
  running SpikeGLX acquisition and turns it into threshold-crossing spikes
  on the session clock, and a `Task.live_analysis` hook lets an experiment
  compute on them between trials and put its own panels on the live monitor —
  with a simulated backend whose ground-truth receptive fields let the
  whole pipeline run, and be tested, with no hardware
  ([docs/live-spikes.md](docs/live-spikes.md)). The
  [rf-mapping](https://github.com/sh4r11f/rf-mapping) experiment is built
  on exactly this.
- **A live monitor in the browser between trials.** Outcome, response, reaction-time,
  landing and reward plots update after every measurement; controls unlock only
  after a keyboard pause so the browser cannot steal focus during an active trial.
- **Scenes from [illusion-studio](https://github.com/sh4r11f/illusion-studio)**,
  rendered unchanged inside a trial.

## What it refuses to do

- Credit fixation it cannot verify — an unverifiable gaze sample is outside
  every region.
- Count a trial that produced no measurement; its condition is served again.
- Overwrite a run's data.
- Fail quietly. A missing SDK, a config typo, a mismatched refresh rate, a
  sync line that will not pulse: each is a typed error naming what to fix.

## License

MIT.
