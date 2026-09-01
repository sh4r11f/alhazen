"""Run the RF-mapping example, in any of the six modes.

    python run.py --mode simulate --rig rig-demo.yaml   # watch the live map fill in
    python run.py --mode simulate --rig rig-sim.yaml    # the same, headless
    python run.py --mode demo --rig <a real rig>        # look at the grid and the flashes
    python run.py --rig <the lab rig> --sub m01 --ses 1 # the real thing

``rig-demo.yaml`` pairs a simulated display and a simulated spike source
with the live dashboard: the browser page shows the receptive-field heat
maps being recovered from the simulated channels' ground-truth fields while
the autopilot fixates — the entire live pipeline, with no hardware anywhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

from task import ArrayV4MapTask

from alhazen.cli.modes import run_experiment

HERE = Path(__file__).parent

raise SystemExit(
    run_experiment(
        task_class=ArrayV4MapTask,
        default_rig=HERE / "rig-sim.yaml",
        default_params=HERE / "task.yaml",
        instructions=lambda: (HERE / "instructions.md").read_text(),
        argv=sys.argv[1:],
    )
)
