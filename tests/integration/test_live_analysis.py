"""Acceptance: the live-analysis seam, end to end through the real wiring.

The RF-mapping experiment that motivated these seams lives in its own repo
now, so this is the framework's own proof that they hold together: a toy
task whose live analysis consumes the rig's simulated spike source, built
by ``build_mode_session`` and run through the real builder, runner and
engine. It pins the four promises the seam makes:

- the builder wires the rig-config spike source into ``Task.live_analysis``
  and subscribes both the simulated source and the analysis to the bus
  (the spike counts prove the source heard the stimulus event);
- the runner drives ``on_trial`` between trials;
- the analysis's panels reach the dashboard state, after the spec's own;
- ``finish`` runs in teardown before the manifest is written, so the saved
  artifact is covered — ``load_run`` verifying is the proof.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from alhazen.analysis.io.session import load_run
from alhazen.config.models import (
    DevicesConfig,
    DisplayConfig,
    Model,
    MonitorConfig,
    RigConfig,
    SpikeSourceConfig,
)
from alhazen.core.events import EventSchema
from alhazen.core.trial import Outcome, PhaseAction, TrialContext, outcomes
from alhazen.modes import Mode
from alhazen.modes.session import build_mode_session
from alhazen.paradigms.config import SchedulerConfig
from alhazen.task.live import LiveWiring
from alhazen.task.plan import TrialPlan, TrialSetup
from alhazen.task.task import Task

# The simulated source's ground truth: channel 0's field sits ON the ping
# position, channel 1's far away — so channel 0 must out-fire channel 1,
# which is what proves the events reached the source through the bus.
PING_AT = (1.0, -1.0)
FAR_AWAY = (8.0, 8.0)


class PingParams(Model):
    paradigm: SchedulerConfig = SchedulerConfig(n_per_condition=2)


class EmitPing:
    """Queue one PING carrying a position, hold a few frames, end the trial."""

    name = "emit_ping"

    def __init__(self, on_done: Outcome) -> None:
        self._on_done = on_done
        self._frames = 0

    def on_enter(self, ctx: TrialContext) -> None:
        self._frames = 0
        ctx.emit_on_flip("PING", {"x_dva": PING_AT[0], "y_dva": PING_AT[1]})

    def on_frame(self, ctx: TrialContext) -> str | Outcome:
        self._frames += 1
        # Six frames (~100 ms): long enough that this trial's simulated
        # spikes are due by the time the runner drains between trials.
        if self._frames >= 6:
            return self._on_done
        return PhaseAction.CONTINUE


class CountingLive:
    """The smallest honest LiveAnalysis: count events and spikes, panel the
    total, save the tally."""

    def __init__(self, wiring: LiveWiring) -> None:
        self.spikes = wiring.spikes
        self.n_events = 0
        self.per_channel = (
            np.zeros(self.spikes.n_channels, dtype=int) if self.spikes is not None else None
        )
        self.trials_seen = 0

    def on_event(self, event: Any) -> None:
        if event.name == "PING":
            self.n_events += 1

    def on_trial(self, record: dict[str, Any]) -> None:
        self.trials_seen += 1
        self._drain()

    def _drain(self) -> None:
        if self.spikes is None:
            return
        batch = self.spikes.drain()
        assert self.per_channel is not None
        np.add.at(self.per_channel, batch.channels, 1)

    def panels(self) -> list[dict[str, Any]]:
        total = 0 if self.per_channel is None else int(self.per_channel.sum())
        return [
            {
                "title": "Spikes heard",
                "section": "Live",
                "data": {
                    "form": "stat",
                    "value": f"{total}",
                    "unit": "",
                    "label": "spikes",
                    "secondary": f"{self.n_events} pings",
                },
            }
        ]

    def finish(self, run_dir: Path) -> None:
        self._drain()
        counts = [] if self.per_channel is None else self.per_channel.tolist()
        (run_dir / "live_counts.json").write_text(
            json.dumps({"pings": self.n_events, "per_channel": counts})
        )


class PingTask(Task):
    name = "live-ping"
    events = EventSchema(("PING",))
    outcomes = outcomes(DONE=dict(completed=True))
    params_model = PingParams

    # Simulate mode may rebuild the task around reduced params
    # (modes/session.py), so the instance the caller constructed is not
    # necessarily the one the builder wires. The class keeps every
    # instance; the test asserts on the one that actually ran.
    instances: list[PingTask] = []

    def __init__(self, params: Model) -> None:
        super().__init__(params)
        self.live: CountingLive | None = None
        PingTask.instances.append(self)

    def build_trial(self, setup: TrialSetup) -> TrialPlan:
        return TrialPlan(phases=[EmitPing(self.outcomes["DONE"])])

    def live_analysis(self, wiring: LiveWiring) -> CountingLive:
        self.live = CountingLive(wiring)
        return self.live

    def simulation(self, seed: int) -> Any:
        from alhazen.devices.automated import AutomatedGazeTracker
        from alhazen.modes.simulation import Simulation

        # Nothing here is gaze-contingent; the tracker only makes the
        # simulation non-empty, which is simulate mode's entry fee.
        return Simulation(tracker=AutomatedGazeTracker(), describe={"seed": seed})


def sim_rig(tmp_path: Path) -> RigConfig:
    return RigConfig(
        monitor=MonitorConfig(
            width_px=1920,
            height_px=1080,
            width_cm=60.0,
            distance_cm=60.0,
            refresh_rate_hz=60.0,
            fullscreen=False,
        ),
        display=DisplayConfig(backend="simulated"),
        # Dashboard on (browser suppressed): the point is that the live
        # panels travel through the real publish path into the saved state.
        dashboard={"enabled": True, "auto_open": False},
        devices=DevicesConfig(
            spikes=SpikeSourceConfig(
                backend="simulated",
                sim_channels=2,
                sim_rf_centers_dva=(PING_AT, FAR_AWAY),
                sim_rf_sigma_dva=1.0,
                sim_baseline_hz=0.0,
                sim_peak_hz=300.0,
                sim_latency_ms=0.0,
                sim_duration_ms=20.0,
                sim_respond_to="PING",
            )
        ),
        data_root=tmp_path / "data",
    )


def test_live_analysis_seam_end_to_end(tmp_path):
    PingTask.instances.clear()
    built = build_mode_session(
        Mode.SIMULATE,
        rig=sim_rig(tmp_path),
        task=PingTask(PingParams()),
        subject="sim",
        session=1,
        seed=9,
        # Two trials really means two: simulate's reduction lowers counts
        # only past this ceiling, so the design above runs as written.
        n_per_condition=2,
    )
    built.runner.run()

    ran = PingTask.instances[-1]
    assert ran.live is not None
    live = ran.live
    # The runner drove the analysis once per completed trial, and the bus
    # delivered every flip-stamped PING to it.
    assert live.trials_seen == 2
    assert live.n_events == 2

    run_dir = next((built.data_root / "sub-sim" / "ses-001").glob("run-01_*"))

    # finish() ran before the manifest was written: the artifact exists AND
    # the manifest verifies with it present.
    run = load_run(run_dir)
    assert run.manifest_problems == []
    saved = json.loads((run_dir / "live_counts.json").read_text())
    assert saved["pings"] == 2
    # Channel 0's ground-truth field sits on the ping; channel 1's is 11
    # dva away with zero baseline. Spikes on 0 and none on 1 is the proof
    # the simulated source heard the session's own events through the bus.
    assert saved["per_channel"][0] > 0
    assert saved["per_channel"][1] == 0

    # The live panel travelled through the real dashboard publish into the
    # saved state, after the spec's own panels, under its own section.
    state = json.loads((run_dir / "figures" / "dashboard_state.json").read_text())
    live_panels = [p for p in state["panels"] if p.get("section") == "Live"]
    assert len(live_panels) == 1
    assert live_panels[0]["title"] == "Spikes heard"
    assert live_panels[0]["data"]["form"] == "stat"
    assert live_panels[0]["data"]["secondary"] == "2 pings"


# ----------------------------------------------------------------------
# The task-supplied spike source
# ----------------------------------------------------------------------


class TaskSuppliedTask(PingTask):
    """A task whose simulated subject brings its own brain.

    The case this covers is the one a rig YAML cannot: a simulated source
    whose response depends on what the trial was asking. The rig file can
    only name a backend and its constants, so an experiment whose simulated
    neurons have to answer *this* stimulus has to construct them itself and
    hand them to simulate mode.
    """

    name = "live-ping-own-spikes"

    def simulation(self, seed: int) -> Any:
        from alhazen.devices.automated import AutomatedGazeTracker
        from alhazen.devices.spikes import SimulatedSpikeSource
        from alhazen.modes.simulation import Simulation

        return Simulation(
            tracker=AutomatedGazeTracker(),
            spikes=SimulatedSpikeSource(
                SpikeSourceConfig(
                    backend="simulated",
                    sim_channels=2,
                    # Deliberately the mirror of the rig's: channel 1 is the
                    # one on the ping here. If the rig's source were used
                    # instead, channel 0 would fire and the assertion below
                    # would fail rather than pass for the wrong reason.
                    sim_rf_centers_dva=(FAR_AWAY, PING_AT),
                    sim_rf_sigma_dva=1.0,
                    sim_baseline_hz=0.0,
                    sim_peak_hz=300.0,
                    sim_latency_ms=0.0,
                    sim_duration_ms=20.0,
                    sim_respond_to="PING",
                )
            ),
            describe={"seed": seed},
        )


def test_a_task_supplied_spike_source_reaches_the_live_analysis(tmp_path):
    PingTask.instances.clear()
    built = build_mode_session(
        Mode.SIMULATE,
        rig=sim_rig(tmp_path),  # its own simulated source has the mirrored fields
        task=TaskSuppliedTask(PingParams()),
        subject="sim",
        session=1,
        seed=9,
        n_per_condition=2,
    )
    built.runner.run()

    ran = PingTask.instances[-1]
    assert ran.live is not None
    assert ran.live.spikes is built.simulation.spikes

    run_dir = next((built.data_root / "sub-sim" / "ses-001").glob("run-01_*"))
    saved = json.loads((run_dir / "live_counts.json").read_text())
    # Channel 1 is the one whose field sits on the ping in the task's own
    # source. Spikes there and none on channel 0 proves the task's source
    # ran, was subscribed to the bus, and beat the rig's.
    assert saved["per_channel"][1] > 0
    assert saved["per_channel"][0] == 0
