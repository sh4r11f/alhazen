"""Run the minimal fixation example against the simulated display.

    python examples/minimal_fixation/run.py [--data-root DIR] [--seed N]

Produces a real run directory (trials/events/frames CSVs, config snapshot,
manifest, session log) with no renderer or hardware installed.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from task import FixationParams, MinimalFixationTask

from alhazen import build_session
from alhazen.config.experiment import find_experiment
from alhazen.config.loader import load_model, load_rig
from alhazen.modes.session import next_run

HERE = Path(__file__).parent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rig", default=None, help="rig YAML (in this directory)")
    parser.add_argument("--data-root", default=None, help="override the rig config's data_root")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--auto", action="store_true", help="run a visible automated demo")
    args = parser.parse_args()

    rig_path = HERE / (args.rig or ("rig-psychopy.yaml" if args.auto else "rig-sim.yaml"))
    rig = load_rig(rig_path)
    if args.data_root is not None:
        rig = rig.model_copy(update={"data_root": Path(args.data_root)})
    params = load_model(HERE / "task.yaml", FixationParams)

    runner = build_session(
        rig=rig,
        subject="demo",
        session=1,
        run=_next_run(rig.data_root),
        task=MinimalFixationTask(params),
        seed=args.seed,
        iti=params.iti,
        windowed=True,
        sources={"rig": str(rig_path), "task": str(HERE / "task.yaml")},
        simulated_frame_period_s=0.0,  # unpaced: finish in milliseconds
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
    version = find_experiment(MinimalFixationTask).version
    return next_run(data_root, "demo", 1, experiment_version=version)


if __name__ == "__main__":
    main()
