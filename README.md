# alhazen

A framework for building and running vision science experiments.

One tested core provides the trial engine, the device layer, the
configuration and reproducibility machinery, and the session data management.
Each experiment is a thin package supplying its own stimuli, phases, configs
and analysis.

```bash
pip install alhazen-vision
alhazen new my_experiment && cd my_experiment
pip install -e ".[dev]"
pytest                                                    # no display needed
python run.py --rig configs/rig-sim.yaml --sub s01 --ses 1
```

That last command runs a complete session and writes a real run directory —
trials, events and frame timings, a config snapshot, a session log and a
hashed manifest — on a laptop with no rig attached.

Every run is also mirrored into `data/experiment.sqlite3`, where subjects,
sessions, trials, displayed frames, gaze/responses, artifacts and aligned
device channels can be queried together.

**Documentation:** <https://sh4r11f.github.io/alhazen/> · **Concepts:**
[docs/architecture.md](docs/architecture.md) · **Contributing:**
[CONTRIBUTING.md](CONTRIBUTING.md)

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
- **A live browser dashboard between trials.** Outcome, response, reaction-time,
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
