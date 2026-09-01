# Example modes

Interactive examples wait on their instruction screen for the participant to
press Space. Add `--auto` to show the same real PsychoPy task while an
automated participant supplies gaze or key responses, completes the trials,
and saves an ordinary run directory without operator input.

```bash
python examples/minimal_fixation/run.py --auto
python examples/gaze_fixation/run.py --auto
python examples/saccade_to_target/run.py --auto
python examples/scene_stimulus/run.py --auto
python examples/staircase_detection/run.py --auto
python examples/shaping_curriculum/run.py --auto
```

`rf_mapping` is mode-driven rather than `--auto`-driven — it adopts the
RF-mapping template (docs/rf-mapping.md), and its most instructive run is

```bash
python examples/rf_mapping/run.py --mode simulate --rig rig-demo.yaml
```

which opens the live dashboard and shows receptive-field heat maps being
recovered from a simulated spike source's ground-truth fields, with no
hardware and no renderer anywhere.

Use `--data-root DIR` to choose where the data is written. Automated mode
shows the instructions for two seconds and then starts by itself. The usual
experimenter controls remain active, so Ctrl+C still ends a run cleanly.

The monitor geometry and refresh rate in each real-display rig YAML are
examples. Set them to the actual display before treating timing or visual
angle as experimental measurements.
