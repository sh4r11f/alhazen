# Getting started

From nothing to a running session. No rig, no eye tracker, and no renderer
needed — everything here works on a laptop.

## Install

```bash
pip install alhazen
```

That is the hardware-free core: enough to build an experiment, run complete
simulated sessions, and analyse what they produce. Two extras exist for when
you need them:

```bash
pip install "alhazen[psychopy]"   # a real window, on the rig or for development
pip install "alhazen[nidaq]"      # reward and TTL sync hardware (Windows rigs)
```

Neither eye tracker's SDK is on PyPI. `pylink` ships with SR Research's
EyeLink Developer's Kit — and the PyPI package of that name is something else
entirely — while `pypixxlib` ships with VPixx's Software Tools installer.
alhazen will tell you which one is missing, and where it comes from, if you
try to use a tracker without it.

## Scaffold an experiment

```bash
alhazen new saccade_bias
cd saccade_bias
pip install -e ".[dev]"
```

You now have a working experiment package:

```
saccade_bias/
├── src/saccade_bias/task.py   the experiment: params, events, outcomes, one trial
├── configs/task.yaml          the task's parameters
├── configs/rig-sim.yaml       a laptop: no window, no devices
├── configs/rig-lab.yaml       the rig: fill in your monitor, uncomment your devices
├── tests/test_task.py         tests on a fake clock, no display needed
└── run.py
```

## Run its tests

```bash
pytest
```

Three tests, on a fake clock with scripted gaze: the trial completes when the
subject looks at the point, times out when they never do, and — the one worth
reading — treats a blink during the hold as a break rather than as continued
fixation.

## Run a session

```bash
python run.py --rig configs/rig-sim.yaml --sub s01 --ses 1
```

That is a complete session. Look at what it wrote:

```
data/sub-s01/ses-001/run-01_task-saccade-bias/
├── sub-s01_..._trials.csv     one row per trial that produced a measurement
├── sub-s01_..._events.csv     every event, timestamped by the flip that showed it
├── sub-s01_..._frames.csv     every frame interval, and which were dropped
├── config_snapshot.yaml       exactly what ran: config, seed, versions, git SHA
├── session.log
└── manifest.yaml              a hash of every file above
```

The snapshot is the important one. It is written *before the first trial*, so
a session that crashes still documents what it was trying to do, and every
later analysis reads the rig's configuration out of it rather than being told.

## Read the run back

```bash
alhazen report --run data/sub-s01/ses-001/run-01_task-saccade-bias
```

Trial counts by outcome, frame-drop statistics, and a manifest check. Exits
non-zero if anything is wrong, so it can gate a pipeline.

## Change the experiment

Open `src/saccade_bias/task.py`. Three things to try:

1. **Change a duration.** `configs/task.yaml`'s `hold_duration: {ms: 500}`
   can also be `{frames: 30}` — frame-denominated durations are exact, and
   both resolve once against the display's *measured* refresh rate.
2. **Add a phase.** `alhazen.task.phases` has nine; the trial is just a list.
3. **Change the scheduling.** `paradigm: {kind: staircase, ...}` in the task
   config turns the same trials into an adaptive staircase, with no code
   change.

## When you get to the rig

```bash
alhazen monitor register --rig configs/rig-lab.yaml  # tell PsychoPy about the panel
alhazen calibrate ruler --rig configs/rig-lab.yaml   # is the geometry right?
alhazen check-rig --rig configs/rig-lab.yaml --pulse # is everything wired?
alhazen run --task saccade-bias --rig configs/rig-lab.yaml --sub s01 --ses 1
```

`monitor register` writes the rig's monitor into PsychoPy's own monitor
database — the one Monitor Center edits, under `~/.psychopy3/monitors` — using
the name in `monitor.name`. Do it once per rig, and again whenever you change
the geometry or measure a new gamma. From then on the panel is visible to
every PsychoPy tool on that machine, sessions inherit whatever calibration is
stored on it, and `check-rig` tells you if the config and the registration
have drifted apart. `monitor list` shows what PsychoPy knows; `monitor show
--rig <yaml>` compares one rig against it and exits non-zero if they disagree.

A machine driving two panels needs two names: rigs left on the default share
one registration and overwrite each other.

`calibrate ruler` opens the rig's own display and draws a bar of the size you
asked for, with ticks at each end and the length it *should* measure printed
underneath. Hold a tape against it; press any key to close. If the tape
disagrees, `monitor.width_cm` or `monitor.distance_cm` is wrong, and every
stimulus size on that rig is scaled by the same error until you fix it. On a
simulated display there is nothing to measure, so it prints the report and
returns.

`calibrate gamma --rig <yaml> --measurements <csv>` fits the panel's luminance
response and writes it to `<rig>_gamma.yaml`, beside the config it belongs to.
Every session built from that config applies it when the display opens.

`check-rig` constructs the same device objects a session would, so a clean
check actually predicts a working session. `--pulse` fires one reward pulse
and one pulse per sync line, so you can hear the pump and see the lines on a
scope before an animal is in the chair.
