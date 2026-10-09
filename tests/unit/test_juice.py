"""The juice a session delivered (live_monitor/juice.py): the live monitor's
per-trial and cumulative plot, session.json's reward.delivered, and both held
to what actually reached the pump.

- deliveries are classified by what they were paid for: the trial's outcome,
  a trial a device cut short (on_fault), a manual press, a mid-trial drop
  (counted when delivered, never when commanded or cancelled);
- amounts are in µL when the rig's reward calibration covers every pulse
  width delivered on that line at that voltage, and in pulses otherwise,
  the unit always said;
- a whole session on the fake NI-DAQ driver: the pulses the driver played
  are the plot's and session.json's totals; with a calibration beside the
  rig file they are µL, pulses times the measured µL per pulse;
- the runner publishes the panel for a session that pays, and not for one
  that does not.
"""

from __future__ import annotations

import json

import pytest
import yaml
from tests.unit.test_pause_flow import FakeLiveMonitor
from tests.unit.test_subject_kind import FakeDaq, daq  # noqa: F401  (the fake NI-DAQ fixture)
from tests.unit.test_system_faults import FAULT_PAY, PAID, clean, run_session, tracker_stops
from tests.unit.test_training_ladder import run_py, write_ladder  # noqa: F401

from alhazen.devices.reward import SimulatedReward
from alhazen.live_monitor.juice import deliveries, juice_payload, juice_totals, ul_by_width
from support import SessionHarness


def event(name: str, trial: int, **payload) -> dict:
    return {"event": name, "trial_index": trial, "payload_json": json.dumps(payload)}


TRAIN = {"n_pulses": 2, "pulse_ms": 200, "inter_pulse_ms": 200}
DROP = {"n_pulses": 1, "pulse_ms": 50, "inter_pulse_ms": 0}
EVENTS = [
    event("TRIAL_START", 1),
    event("REWARD", 1, manual=False, outcome="HIT", pulses=TRAIN),
    event("NO_REWARD", 2, outcome="MISS"),
    event(
        "REWARD",
        3,
        manual=False,
        outcome="ABORTED",
        fault="tracker_stopped",
        pulses={"n_pulses": 1, "pulse_ms": 200, "inter_pulse_ms": 200},
    ),
    event("REWARD", 4, manual=True, pulses=TRAIN),
    # A mid-trial drop: commanded (REWARD with a reason), then delivered.
    event("REWARD", 5, pulses=DROP, reason="held", frame=10),
    event("REWARD_DELIVERED", 5, pulses=DROP, reason="held", frame=10),
    # A second drop commanded and cancelled: it never reached the valve.
    event("REWARD", 5, pulses=DROP, reason="held", frame=20),
    event("REWARD_CANCELLED", 5, pulses=DROP, reason="held", frame=20, cancelled_by="manual"),
    event("REWARD_FAILED", 6, outcome="HIT"),
    event("TRIAL_START", 7),
]
CAL = {
    "schema_version": 1,
    "widths": {
        200: {
            "ul_per_pulse": 12.5,
            "line": "Dev1/ao0",
            "voltage": 5.0,
            "measured_at": "2026-10-08",
        },
        50: {"ul_per_pulse": 3.0, "line": "Dev1/ao0", "voltage": 5.0, "measured_at": "2026-10-08"},
        100: {"ul_per_pulse": 6.0, "line": "Dev2/ao1", "voltage": 5.0, "measured_at": "2026-10-08"},
    },
}


class TestTheLedger:
    def test_each_delivery_is_classified_by_what_it_paid_for(self):
        found, failed = deliveries(EVENTS)
        assert [(d["trial_index"], d["kind"]) for d in found] == [
            (1, "outcome"),
            (3, "fault"),
            (4, "manual"),
            (5, "mid_trial"),
        ]
        assert failed == [6]

    def test_the_calibration_is_used_only_for_its_own_line_and_voltage(self):
        assert ul_by_width(CAL, line="Dev1/ao0", voltage=5.0) == {200: 12.5, 50: 3.0}
        assert ul_by_width(CAL, line="Dev1/ao0", voltage=4.0) == {}
        assert ul_by_width(None, line="Dev1/ao0", voltage=5.0) == {}

    def test_totals_in_pulses_without_a_calibration(self):
        totals = juice_totals(EVENTS)
        assert totals["unit"] == "pulses"
        assert totals["volume_ul"] is None
        assert (totals["deliveries"], totals["pulses"]) == (4, 2 + 1 + 2 + 1)
        assert totals["open_ms"] == 400 + 200 + 400 + 50
        assert totals["by_kind"]["fault"] == {
            "deliveries": 1,
            "pulses": 1,
            "open_ms": 200,
            "volume_ul": None,
        }
        assert totals["failed"] == 1 and totals["trials_paid"] == 3

    def test_totals_in_ul_when_every_width_was_measured(self):
        totals = juice_totals(EVENTS, {200: 12.5, 50: 3.0})
        assert totals["unit"] == "µL"
        assert totals["volume_ul"] == pytest.approx(2 * 12.5 + 12.5 + 2 * 12.5 + 3.0)
        assert totals["by_kind"]["manual"]["volume_ul"] == pytest.approx(25.0)
        assert totals["ul_per_pulse"] == {"50": 3.0, "200": 12.5}

    def test_a_width_never_measured_falls_back_to_pulses(self):
        assert juice_totals(EVENTS, {200: 12.5})["unit"] == "pulses"

    def test_the_plot_stacks_each_trial_and_climbs_to_the_total(self):
        payload = juice_payload(EVENTS, {200: 12.5, 50: 3.0})
        assert payload["form"] == "juice" and payload["unit"] == "µL"
        assert payload["y_label"] == "Per trial (µL)" and payload["y2_label"] == "Cumulative (µL)"
        by_trial = {t["x"]: t for t in payload["trials"]}
        assert by_trial[1]["outcome"] == 25.0 and by_trial[3]["fault"] == 12.5
        assert by_trial[4]["manual"] == 25.0 and by_trial[5]["mid_trial"] == 3.0
        assert payload["cumulative"][-1] == [7, payload["total"]]
        assert payload["total"] == pytest.approx(
            juice_totals(EVENTS, {200: 12.5, 50: 3.0})["volume_ul"]
        )
        assert payload["failures"] == [6]
        assert {s["label"] for s in payload["stats"]} >= {"total", "on_fault", "manual", "failed"}

    def test_nothing_delivered_is_said_so(self):
        assert juice_payload([event("TRIAL_START", 1)])["form"] == "empty"


class TestAWholeSession:
    def test_fault_and_outcome_payouts_match_what_the_pump_was_told(self, tmp_path):
        pump = SimulatedReward()
        harness = run_session(
            tmp_path, [tracker_stops(), clean(), clean()], n_trials=2, reward=pump
        )
        totals = juice_totals(harness.runner._recorder.events)
        assert pump.deliveries == [FAULT_PAY, PAID, PAID]
        assert totals["pulses"] == sum(p.n_pulses for p in pump.deliveries)
        assert totals["open_ms"] == sum(p.n_pulses * p.pulse_ms for p in pump.deliveries)
        assert totals["by_kind"]["fault"]["pulses"] == FAULT_PAY.n_pulses
        card = json.loads(harness.paths.session_json_path.read_text(encoding="utf-8"))
        assert card["reward"]["delivered"] == totals

    def test_the_fake_ni_daq_plays_exactly_the_plotted_juice_in_ul(self, tmp_path, daq):  # noqa: F811
        rig = tmp_path / "rig-lab.yaml"
        # The rig run_py writes, and its calibration beside it: 150 ms pulses
        # (the stage's) measured at 7.5 µL each on Dev1/ao0 at 5 V.
        run_py(tmp_path, "--mode", "training", "--stage", "saccade", "--estimate-duration")
        (tmp_path / "rig-lab.reward.yaml").write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "widths": {
                        150: {
                            "ul_per_pulse": 7.5,
                            "line": "Dev1/ao0",
                            "voltage": 5.0,
                            "measured_at": "2026-10-08T10:00:00",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        assert rig.is_file()
        code = run_py(
            tmp_path,
            "--mode",
            "training",
            "--stage",
            "saccade",
            "--sub",
            "m01",
            "--ses",
            "1",
            "--initials",
            "MK",
            "--seed",
            "1",
        )
        assert code == 0
        card = json.loads(
            next((tmp_path / "data-training").rglob("session.json")).read_text(encoding="utf-8")
        )
        delivered = card["reward"]["delivered"]
        # What the driver played: high samples at 1 kHz over each 150 ms pulse.
        played = sum(sum(1 for v in ao.written if v > 0) for ao in daq.tasks) // 150
        assert delivered["pulses"] == played == len(daq.tasks)
        assert delivered["unit"] == "µL"
        assert delivered["volume_ul"] == pytest.approx(played * 7.5)
        assert delivered["open_ms"] == played * 150

    def test_without_a_calibration_the_session_says_pulses(self, tmp_path, daq):  # noqa: F811
        code = run_py(
            tmp_path,
            "--mode",
            "training",
            "--stage",
            "saccade",
            "--sub",
            "m01",
            "--ses",
            "1",
            "--initials",
            "MK",
            "--seed",
            "1",
        )
        assert code == 0
        card = json.loads(
            next((tmp_path / "data-training").rglob("session.json")).read_text(encoding="utf-8")
        )
        assert card["reward"]["delivered"]["unit"] == "pulses"
        assert card["reward"]["delivered"]["volume_ul"] is None
        assert card["reward"]["delivered"]["pulses"] == len(daq.tasks)

    def test_the_runner_publishes_the_panel_only_for_a_paying_session(self, tmp_path):
        monitor = FakeLiveMonitor()
        paying = SessionHarness(
            tmp_path / "pay",
            n_trials=1,
            reward=SimulatedReward(),
            reward_policy=__import__("alhazen").RewardPolicy(by_outcome={"COMPLETED": PAID}),
            live_monitor=monitor,
        )
        paying.runner.run()
        panels = {p["title"]: p for p in monitor.saved["panels"]}
        assert panels["Juice delivered"]["data"]["form"] == "juice"
        assert panels["Juice delivered"]["data"]["total"] == PAID.n_pulses
        quiet_monitor = FakeLiveMonitor()
        quiet = SessionHarness(tmp_path / "none", n_trials=1, live_monitor=quiet_monitor)
        quiet.runner.run()
        assert "Juice delivered" not in {p["title"] for p in quiet_monitor.saved["panels"]}


def test_a_reward_calibration_beside_a_rig_is_not_a_rig(tmp_path):
    from alhazen.config.rigs import experiment_rig_files

    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "rig-lab.yaml").write_text("extends: lab\n", encoding="utf-8")
    (configs / "rig-lab.reward.yaml").write_text("schema_version: 1\nwidths: {}\n", "utf-8")
    assert [name for name, _ in experiment_rig_files(tmp_path)] == ["lab"]
