"""Run the interleaved-staircase detection example.

    python examples/staircase_detection/run.py     # answer with the left/right arrow keys

This one needs a subject: a staircase only moves on trials that produced a
measurement, so a session nobody answers never advances and never ends —
which is the framework behaving correctly, not a bug to work around. The
experimenter quits with ctrl+c. The headless equivalent is the test suite,
where scripted key presses stand in for the subject.

The staircases' final state is written to the run's ``*_paradigm.csv``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from task import DetectionParams, DetectionTask

from alhazen import build_session
from alhazen.config.loader import load_model, load_rig
from alhazen.devices.automated import AutomatedResponse

HERE = Path(__file__).parent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rig", default="rig-psychopy.yaml", help="rig YAML (in this directory)")
    parser.add_argument("--data-root", default=None, help="override the rig config's data_root")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--auto", action="store_true", help="run a visible automated demo")
    args = parser.parse_args()

    rig_path = HERE / args.rig
    rig = load_rig(rig_path)
    if args.data_root is not None:
        rig = rig.model_copy(update={"data_root": Path(args.data_root)})
    params = load_model(HERE / "task.yaml", DetectionParams)

    runner = build_session(
        rig=rig,
        subject="demo",
        session=1,
        run=_next_run(rig.data_root),
        task=DetectionTask(params),
        response=AutomatedResponse() if args.auto else None,
        seed=args.seed,
        iti=params.iti,
        windowed=True,
        sources={"rig": str(rig_path), "task": str(HERE / "task.yaml")},
        simulated_frame_period_s=0.0,
        instructions=(HERE / "instructions.md").read_text(),
        auto_start=args.auto,
    )
    runner.run()
    print(f"session complete — data under {rig.data_root.resolve()}")


def _next_run(data_root: Path) -> int:
    session_dir = data_root / "sub-demo" / "ses-001"
    if not session_dir.exists():
        return 1
    taken = [int(p.name.split("_")[0].split("-")[1]) for p in session_dir.glob("run-*")]
    return max(taken, default=0) + 1


if __name__ == "__main__":
    main()
