"""Run the gaze-contingent fixation example.

    python examples/gaze_fixation/run.py                      # mouse as gaze (needs [psychopy])
    python examples/gaze_fixation/run.py --rig rig-sim.yaml   # headless, no gaze at all

With ``rig-mouse.yaml`` the mouse cursor is the subject's eye: move it into
the fixation window to acquire, keep it there to complete the trial. With
``rig-sim.yaml`` there is no tracker, so every trial times out — the same
task, unchanged, on a machine with nothing attached.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from task import GazeFixationParams, GazeFixationTask

from alhazen import RewardPulses, build_session
from alhazen.config.experiment import find_experiment
from alhazen.config.loader import load_model, load_rig
from alhazen.devices.automated import AutomatedGazeTracker
from alhazen.modes.session import next_run

HERE = Path(__file__).parent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rig", default="rig-mouse.yaml", help="rig YAML (in this directory)")
    parser.add_argument("--data-root", default=None, help="override the rig config's data_root")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--auto", action="store_true", help="run a visible automated demo")
    args = parser.parse_args()

    rig_path = HERE / args.rig
    rig = load_rig(rig_path)
    if args.data_root is not None:
        rig = rig.model_copy(update={"data_root": Path(args.data_root)})
    params = load_model(HERE / "task.yaml", GazeFixationParams)

    runner = build_session(
        rig=rig,
        subject="demo",
        session=1,
        run=_next_run(rig.data_root),
        task=GazeFixationTask(params),
        tracker=AutomatedGazeTracker() if args.auto else None,
        seed=args.seed,
        iti=params.iti,
        # What the experimenter's manual-reward key delivers.
        reward_pulses=RewardPulses(n_pulses=1, pulse_ms=100, inter_pulse_ms=0),
        windowed=False,
        sources={"rig": str(rig_path), "task": str(HERE / "task.yaml")},
        simulated_frame_period_s=0.0,  # unpaced when simulated: finish immediately
        instructions=(HERE / "instructions.md").read_text(),
        auto_start=args.auto,
    )
    runner.run()
    print(f"session complete — data under {rig.data_root.resolve()}")


def _next_run(data_root: Path) -> int:
    """First unused run number, so repeated invocations never trip the
    overwrite refusal. Counted inside the experiment's version folder
    (``data/v<version>/sub-demo/ses-001/``), which is where build_session
    files the run: the version is the one the pyproject.toml above the task
    declares — for an example shipped with alhazen, alhazen's own."""
    version = find_experiment(GazeFixationTask).version
    return next_run(data_root, "demo", 1, experiment_version=version)


if __name__ == "__main__":
    main()
