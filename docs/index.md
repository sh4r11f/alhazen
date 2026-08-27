# alhazen

A framework for building and running vision science experiments.

One tested core provides the trial engine, the device layer, the
configuration and reproducibility machinery, and the session data management.
Each experiment is a thin package supplying its own stimuli, phases, configs
and analysis — and nothing else.

```python
class SaccadeTask(alhazen.Task):
    name = "saccade-to-target"
    events = EventSchema(("STIM_ON", "SACCADE_ONSET", "LANDED"))
    outcomes = outcomes(CORRECT=dict(completed=True, success=True))  # ...and the rest
    params_model = SaccadeParams
    reward = RewardPolicy(by_outcome={"CORRECT": RewardPulses(n_pulses=2)})

    def build_trial(self, setup):
        return TrialPlan(phases=[
            phases.AcquireFixation(timeout_s=2.0, on_timeout=self.outcomes["FIX_NOT_ACQUIRED"]),
            phases.HoldFixation(duration_s=0.4, jitter_s=0.1, on_break=self.outcomes["FIX_BREAK"]),
            phases.StimulusResponse("target", timeout_s=0.8, on_timeout=self.outcomes["NO_SACCADE"]),
            phases.LandingCheck("target", on_hit=..., on_miss=...),
        ], stimuli={}, regions={})
```

That is a complete experiment. The frame loop, the eye tracker, the reward
pump, the TTL sync, the scheduling, the data files and the alignment to a
neural recording all come from the framework.

## What it does

| | |
|---|---|
| **Runs trials honestly** | Events are timestamped by the flip that showed them, on one clock. Dropped frames are detected and acted on, not logged and forgotten. |
| **Puts hardware behind protocols** | Eye tracker, reward, sync, response keys and recording systems each have a real backend and a simulated twin. The whole test suite runs on a laptop with none of them installed. |
| **Never credits what it cannot verify** | An unverifiable gaze sample is outside every region. A trial that produced no measurement is served again rather than counted. |
| **Makes sessions reproducible** | One seed, named streams, a config snapshot written before trial 1, and a hashed manifest of everything the run produced. |
| **Refuses to lose data quietly** | It will not overwrite a run. Teardown attempts every step. A device fault is loud — except the one case where a completed trial's measurement would be thrown away to report it. |

## Start here

- **[Getting started](getting-started.md)** — from nothing to a running
  simulated session, in about ten minutes.
- **[Concepts](architecture.md)** — how the pieces fit, and why they are
  shaped that way.
- **[How-to](how-to.md)** — adding a stimulus, a phase, a paradigm, a device
  backend or a training stage.
- **[Scenes](scenes.md)** — stimuli designed in illusion-studio, run
  unchanged.
- **[Experiment database](database.md)** — query subjects, sessions, frames,
  gaze, responses and aligned device channels together.
