"""Human and monkey sessions: who is in the chair decides whether the juice
line opens, and what pays on it (task/subject_kind.py).

The promises tested here, each against a session actually built and run:

- a human session never opens the rig's reward line, whatever the rig has
  and whatever the task class pays: no NI-DAQ task is created, no trial is
  paid, and the experimenter's ``r`` key has nothing to call;
- a monkey session pays exactly the outcomes its params file's ``reward``
  block names, on exactly those trials, as the pulse train it configures,
  played out on the rig file's device and channel at the hardware rate;
- a monkey session is refused where it could not be paid (run mode on a rig
  with no line), rehearsals stand a simulated line in, and it shows no
  instruction screen;
- both kinds say who they were on every row, in session.log and in
  session.json; params that declare neither keep the old rule.

The NI-DAQ is a fake ``nidaqmx`` module standing in for the driver: it
records every task the real ``NidaqReward`` opens, so these tests exercise
the same code path the lab rig runs, down to the buffer written to ao0.
"""

from __future__ import annotations

import csv
import json
import sys
import types
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from pydantic import ValidationError

from alhazen import (
    Condition,
    RewardPolicy,
    RewardPulses,
    RigConfig,
    SubjectKind,
    SubjectParams,
    Task,
    TrialPlan,
    TrialSetup,
    outcomes,
)
from alhazen.config.models import DevicesConfig, DisplayConfig, Duration, RewardHwConfig
from alhazen.config.reward_calibration import (
    load_reward_calibration,
    record_reward_calibration,
    reward_calibration_path,
    ul_per_pulse,
)
from alhazen.core.events import EventSchema
from alhazen.devices.reward import SAMPLE_RATE_HZ, SimulatedReward, build_reward_waveform
from alhazen.errors import ConfigError
from alhazen.modes import Mode
from alhazen.modes.session import build_mode_session
from alhazen.modes.simulation import Simulation
from alhazen.paradigms.base import SimpleSequence
from alhazen.session.builder import build_session
from alhazen.task.subject_kind import opens_reward_line, reward_for, subject_kind_of
from support import MONITOR, RunForFrames

# Two completed outcomes, so no trial is served again and the trial list is
# exactly the plan: HIT is what a monkey is paid for, MISS is not.
OUTCOMES = outcomes(
    HIT=dict(completed=True, success=True),
    MISS=dict(completed=True, success=False),
)
PLAN = ["HIT", "MISS", "HIT", "HIT", "MISS", "MISS", "HIT"]
JUICE = RewardPulses(n_pulses=2, pulse_ms=200, inter_pulse_ms=200)


class Params(SubjectParams):
    n_trials: int = len(PLAN)


class OutcomeTask(Task):
    """Ends each trial as the outcome its condition names, in PLAN order."""

    name = "outcome-task"
    events = EventSchema(())
    outcomes = OUTCOMES
    params_model = Params

    def make_source(self, params, rng):
        conditions = [Condition({"plan_index": i, "result": r}) for i, r in enumerate(PLAN)]
        return SimpleSequence(conditions, n_repeats=1, rng=rng, shuffle=False)

    def build_trial(self, setup: TrialSetup) -> TrialPlan:
        result = setup.condition.params["result"]
        return TrialPlan(phases=[RunForFrames(1, self.outcomes[result])])

    def simulation(self, seed: int) -> Simulation:
        return Simulation(tracker=object(), describe={"seed": seed})


class ReadingTask(OutcomeTask):
    """A task with an instruction screen for its subject."""

    name = "reading-task"

    def instructions(self) -> str | None:
        return "Look at the dot."


class PayingClassTask(OutcomeTask):
    """A task whose class pays HIT: the shape mbri had before 2.12, where
    the policy lived in code and paid on any rig with a line."""

    name = "paying-class-task"
    reward = RewardPolicy(by_outcome={"HIT": JUICE})


def human() -> Params:
    return Params(subject_kind="human")


def monkey(**reward: Any) -> Params:
    policy = reward or {"by_outcome": {"HIT": JUICE.model_dump()}}
    return Params(subject_kind="monkey", reward=policy)


# ----------------------------------------------------------------------
# The fake NI-DAQ driver
# ----------------------------------------------------------------------


class FakeDaq:
    """Every analog-output task the code under test opened, in order."""

    def __init__(self) -> None:
        self.tasks: list[FakeAoTask] = []


class FakeAoTask:
    def __init__(self, daq: FakeDaq) -> None:
        self.channels: list[dict[str, Any]] = []
        self.clock: dict[str, Any] = {}
        self.written: list[float] | None = None
        self.auto_start: bool | None = None
        self.waited = False
        self.stopped = False
        self.closed = False
        outer = self

        class _Channels:
            def add_ao_voltage_chan(self, name, min_val, max_val, units):
                outer.channels.append(
                    {"name": name, "min_val": min_val, "max_val": max_val, "units": units}
                )

        class _Timing:
            def cfg_samp_clk_timing(self, rate, sample_mode, samps_per_chan):
                outer.clock = {"rate": rate, "mode": sample_mode, "samples": samps_per_chan}

        self.ao_channels = _Channels()
        self.timing = _Timing()
        daq.tasks.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def write(self, data, auto_start=False):
        self.written = list(data)
        self.auto_start = auto_start

    def wait_until_done(self, timeout):
        self.waited = True

    def stop(self):
        self.stopped = True


@pytest.fixture
def daq(monkeypatch) -> FakeDaq:
    """Install a fake ``nidaqmx`` that records what NidaqReward does."""
    record = FakeDaq()
    module = types.ModuleType("nidaqmx")
    constants = types.ModuleType("nidaqmx.constants")
    constants.AcquisitionType = types.SimpleNamespace(FINITE="FINITE")  # type: ignore[attr-defined]
    constants.VoltageUnits = types.SimpleNamespace(VOLTS="VOLTS")  # type: ignore[attr-defined]

    class DaqError(Exception):
        pass

    module.Task = lambda: FakeAoTask(record)  # type: ignore[attr-defined]
    module.DaqError = DaqError  # type: ignore[attr-defined]
    module.constants = constants  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nidaqmx", module)
    monkeypatch.setitem(sys.modules, "nidaqmx.constants", constants)
    return record


LAB_LINE = RewardHwConfig(backend="nidaq", device="Dev1", channel="ao0", voltage=5.0)


def rig(tmp_path: Path, reward: RewardHwConfig | None) -> RigConfig:
    return RigConfig(
        monitor=MONITOR,
        display=DisplayConfig(backend="simulated"),
        devices=DevicesConfig(reward=reward),
        data_root=tmp_path,
    )


def build(tmp_path: Path, task: Task, reward_line: RewardHwConfig | None = LAB_LINE, **kw):
    return build_session(
        rig=rig(tmp_path, reward_line),
        subject="t01",
        session=1,
        run=1,
        task=task,
        seed=1,
        iti=Duration(ms=0),
        simulated_frame_period_s=0.0,
        date_yyyymmdd="20261008",
        **kw,
    )


def rows(tmp_path: Path) -> list[dict[str, str]]:
    with next(tmp_path.rglob("*_trials.csv")).open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def events(tmp_path: Path, name: str) -> list[dict[str, str]]:
    with next(tmp_path.rglob("*_events.csv")).open(encoding="utf-8", newline="") as f:
        return [row for row in csv.DictReader(f) if row["event"] == name]


def card(tmp_path: Path) -> dict[str, Any]:
    return json.loads(next(tmp_path.rglob("session.json")).read_text(encoding="utf-8"))


# ----------------------------------------------------------------------
# The params declaration
# ----------------------------------------------------------------------


class TestDeclaration:
    def test_a_human_file_with_a_reward_block_is_refused(self):
        with pytest.raises(ValidationError, match="human session never pays"):
            Params(subject_kind="human", reward={"by_outcome": {"HIT": JUICE.model_dump()}})

    def test_a_monkey_file_without_a_reward_block_is_refused(self):
        with pytest.raises(ValidationError, match="no reward block"):
            Params(subject_kind="monkey")

    def test_a_monkey_file_that_pays_nothing_is_refused(self):
        with pytest.raises(ValidationError, match="pays nothing"):
            Params(subject_kind="monkey", reward={"by_outcome": {}})

    def test_a_scale_that_rounds_every_entry_away_is_refused(self):
        with pytest.raises(ValidationError, match="single pulse"):
            Params(
                subject_kind="monkey",
                reward={"by_outcome": {"HIT": {"n_pulses": 1}}, "scale": 0.2},
            )

    def test_a_reward_block_without_a_kind_is_refused(self):
        with pytest.raises(ValidationError, match="no subject_kind"):
            Params(reward={"by_outcome": {"HIT": JUICE.model_dump()}})

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(ValidationError):
            Params(subject_kind="macaque")
        with pytest.raises(ConfigError, match="not one of human, monkey"):
            subject_kind_of({"subject_kind": "macaque"})

    def test_the_kind_reads_the_same_from_the_model_and_its_dump(self):
        params = monkey()
        assert subject_kind_of(params) is SubjectKind.MONKEY
        assert subject_kind_of(params.model_dump(mode="json")) is SubjectKind.MONKEY
        assert subject_kind_of(Params()) is None
        assert subject_kind_of({}) is None

    def test_only_a_human_closes_the_line(self):
        assert opens_reward_line(human()) is False
        assert opens_reward_line(monkey()) is True
        assert opens_reward_line(Params()) is True


class TestPolicy:
    def test_a_human_task_pays_nothing_even_when_its_class_pays(self):
        assert PayingClassTask(human()).reward is None

    def test_a_monkey_task_pays_its_params_block_not_its_class(self):
        block = {"by_outcome": {"MISS": {"n_pulses": 1, "pulse_ms": 50, "inter_pulse_ms": 0}}}
        task = PayingClassTask(monkey(**block))
        assert task.reward is not None
        assert set(task.reward.by_outcome) == {"MISS"}

    def test_undeclared_params_keep_the_class_policy(self):
        assert PayingClassTask(Params()).reward is PayingClassTask.reward
        assert OutcomeTask(Params()).reward is None

    def test_a_misspelt_outcome_is_refused_when_the_task_is_built(self):
        with pytest.raises(ConfigError, match="HTI.*does not declare"):
            OutcomeTask(monkey(by_outcome={"HTI": JUICE.model_dump()}))

    def test_reward_for_is_the_same_rule_outside_a_task(self):
        names = OUTCOMES.names
        assert reward_for(PayingClassTask.reward, human(), names) is None
        assert reward_for(None, monkey(), names) == monkey().reward

    def test_a_human_task_that_asks_for_mid_trial_drops_is_refused(self):
        class Pursuit(OutcomeTask):
            name = "pursuit"
            mid_trial_reward = True

        with pytest.raises(ConfigError, match="mid_trial_reward = True.*subject_kind: human"):
            Pursuit(human())


# ----------------------------------------------------------------------
# Human sessions
# ----------------------------------------------------------------------


class TestHumanSession:
    def test_the_lab_line_is_never_opened_and_nothing_is_paid(self, tmp_path, daq):
        runner = build(tmp_path, PayingClassTask(human()))
        # Nothing to deliver through, and nothing behind the `r` key.
        assert runner._reward is None
        assert runner._engine._on_manual_reward is None
        runner.run()

        assert daq.tasks == []  # no NI-DAQ task was ever created
        written = rows(tmp_path)
        assert [row["outcome"] for row in written] == PLAN
        assert all(row["subject_kind"] == "human" for row in written)
        assert all(row.get("rewarded", "") == "" for row in written)
        assert events(tmp_path, "REWARD") == []
        assert events(tmp_path, "NO_REWARD") == []

    def test_session_json_and_log_say_the_line_was_closed(self, tmp_path, daq):
        build(tmp_path, OutcomeTask(human())).run()
        log_text = next(tmp_path.rglob("*session.log")).read_text(encoding="utf-8")
        assert "subject kind: human" in log_text
        assert "reward nidaq (closed: human subject)" in log_text
        record = card(tmp_path)
        assert record["subject_kind"] == "human"
        assert record["reward"] == {
            "line_open": False,
            "backend": None,
            "line": None,
            "voltage": None,
            "policy": None,
            "volumes": None,
        }

    def test_a_dispenser_handed_in_is_refused(self, tmp_path):
        with pytest.raises(ConfigError, match="human.*handed a reward dispenser"):
            build(tmp_path, OutcomeTask(human()), reward=SimulatedReward())
        assert not list(tmp_path.rglob("session.json"))


# ----------------------------------------------------------------------
# Monkey sessions
# ----------------------------------------------------------------------


class TestMonkeySession:
    def test_juice_goes_out_on_exactly_the_paying_trials(self, tmp_path, daq):
        build(tmp_path, OutcomeTask(monkey())).run()

        written = rows(tmp_path)
        assert [row["outcome"] for row in written] == PLAN
        assert all(row["subject_kind"] == "monkey" for row in written)
        paid = [row["trial_index"] for row in written if row["outcome"] == "HIT"]
        assert [row["trial_index"] for row in written if row["rewarded"] == "True"] == paid
        # One analog-output task per paying trial, and none for a miss.
        assert len(daq.tasks) == PLAN.count("HIT")
        assert [e["trial_index"] for e in events(tmp_path, "REWARD")] == paid
        assert len(events(tmp_path, "NO_REWARD")) == PLAN.count("MISS")

    def test_each_delivery_is_the_configured_train_on_the_rig_line(self, tmp_path, daq):
        pulses = RewardPulses(n_pulses=3, pulse_ms=120, inter_pulse_ms=80)
        line = RewardHwConfig(backend="nidaq", device="Dev2", channel="ao1", voltage=4.5)
        task = OutcomeTask(monkey(by_outcome={"HIT": pulses.model_dump()}))
        build(tmp_path, task, reward_line=line).run()

        assert daq.tasks, "no delivery reached the fake NI-DAQ"
        for ao in daq.tasks:
            assert ao.channels == [
                {"name": "Dev2/ao1", "min_val": 0.0, "max_val": 10.0, "units": "VOLTS"}
            ]
            assert ao.clock["rate"] == SAMPLE_RATE_HZ == 1000
            assert ao.clock["mode"] == "FINITE"
            samples = np.asarray(ao.written)
            assert ao.clock["samples"] == len(samples)
            assert ao.auto_start is True and ao.waited and ao.stopped and ao.closed
            # The train, sample by sample at 1 kHz: 120 ms at 4.5 V, 80 ms at
            # 0 V, three times; ends at 0 V so the valve closes.
            np.testing.assert_array_equal(samples, build_reward_waveform(4.5, pulses))
            on = samples > 0
            edges = np.flatnonzero(np.diff(on.astype(int)))
            widths_ms = np.diff(np.concatenate([[-1], edges]))[0::2]
            assert list(widths_ms) == [120, 120, 120]
            assert int(on.sum()) == 3 * 120
            assert len(samples) == 3 * (120 + 80)
            assert samples[-1] == 0.0

    def test_the_default_train_matches_realtime_rdk_give_reward(self, tmp_path, daq):
        # realtime-rdk's give_reward (mib_staircase.py, 2025-09): Dev1/ao0,
        # 5 V, 1 kHz finite buffer, 2 pulses of 200 ms with 200 ms between.
        build(tmp_path, OutcomeTask(monkey())).run()
        reference = np.tile(np.concatenate([np.full(200, 5.0), np.zeros(200)]), 2)
        assert daq.tasks
        for ao in daq.tasks:
            assert ao.channels[0]["name"] == "Dev1/ao0"
            np.testing.assert_array_equal(np.asarray(ao.written), reference)

    def test_session_json_records_the_line_and_the_policy(self, tmp_path, daq):
        build(tmp_path, OutcomeTask(monkey())).run()
        record = card(tmp_path)
        assert record["subject_kind"] == "monkey"
        assert record["reward"]["line_open"] is True
        assert record["reward"]["backend"] == "nidaq"
        assert record["reward"]["line"] == "Dev1/ao0"
        assert record["reward"]["voltage"] == 5.0
        assert record["reward"]["policy"]["by_outcome"] == {"HIT": JUICE.model_dump()}

    def test_a_monkey_session_has_no_instruction_screen(self, tmp_path, daq):
        assert build(tmp_path / "h", ReadingTask(human()))._instructions == "Look at the dot."
        assert build(tmp_path / "m", ReadingTask(monkey()))._instructions is None
        given = build(tmp_path / "c", ReadingTask(monkey()), instructions="Read me")
        assert given._instructions is None

    def test_run_on_a_rig_with_no_line_is_refused_before_anything_is_written(self, tmp_path):
        with pytest.raises(ConfigError, match="subject_kind: monkey.*rig has none"):
            build(tmp_path, OutcomeTask(monkey()), reward_line=None)
        assert not list(tmp_path.rglob("session.json"))


# ----------------------------------------------------------------------
# What each mode does with the line
# ----------------------------------------------------------------------


class Spy:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None
        self.runner = types.SimpleNamespace(setup_notes=[])

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self.runner


def mode_build(tmp_path: Path, mode: Mode, task: Task, reward_line: RewardHwConfig | None):
    spy = Spy()
    build_mode_session(
        mode,
        rig=RigConfig(
            monitor=MONITOR,
            data_root=tmp_path / "data",
            devices=DevicesConfig(reward=reward_line),
            display=DisplayConfig(),
        ),
        task=task,
        subject="t01",
        session=1,
        build_session=spy,
    )
    assert spy.kwargs is not None
    return spy.kwargs["rig"], spy.runner.setup_notes


class TestModes:
    @pytest.mark.parametrize("mode", [Mode.TEST, Mode.SIMULATE])
    def test_a_rehearsal_stands_a_simulated_line_in_for_a_monkey(self, tmp_path, mode):
        rig_used, notes = mode_build(tmp_path, mode, OutcomeTask(monkey()), None)
        assert rig_used.devices.reward == RewardHwConfig(backend="simulated")
        assert any("subject_kind is monkey" in note for note in notes)

    def test_run_mode_does_not_stand_one_in(self, tmp_path):
        rig_used, _ = mode_build(tmp_path, Mode.RUN, OutcomeTask(monkey()), None)
        assert rig_used.devices.reward is None  # build_session refuses it

    @pytest.mark.parametrize("mode", [Mode.RUN, Mode.TEST, Mode.SIMULATE])
    def test_a_human_session_says_the_line_stays_closed(self, tmp_path, mode):
        _, notes = mode_build(tmp_path, mode, OutcomeTask(human()), LAB_LINE)
        assert any("reward: closed" in note for note in notes)

    def test_a_human_rehearsal_on_a_bare_rig_gets_no_stand_in(self, tmp_path):
        rig_used, _ = mode_build(tmp_path, Mode.TEST, OutcomeTask(human()), None)
        assert rig_used.devices.reward is None


class TestUndeclared:
    """Params that do not say keep the rule of 2.11 and before."""

    def test_a_paying_class_still_pays_on_a_rig_with_a_line(self, tmp_path, daq):
        build(tmp_path, PayingClassTask(Params())).run()
        assert len(daq.tasks) == PLAN.count("HIT")
        # No new column: a run of undeclared params writes what it always did.
        assert all("subject_kind" not in row for row in rows(tmp_path))
        assert card(tmp_path)["subject_kind"] is None


# ----------------------------------------------------------------------
# A small reward on a device fault, settable to none
# ----------------------------------------------------------------------


class TestFaultReward:
    def test_on_fault_pays_its_pulses_and_zero_pulses_pays_nothing(self):
        one = monkey(by_outcome={"HIT": JUICE.model_dump()}, on_fault={"n_pulses": 1})
        assert one.reward is not None
        assert one.reward.pulses_for_fault() == RewardPulses(n_pulses=1)
        none = monkey(by_outcome={"HIT": JUICE.model_dump()}, on_fault={"n_pulses": 0})
        assert none.reward is not None and none.reward.pulses_for_fault() is None


# ----------------------------------------------------------------------
# Reward in µL, from the rig's measured calibration
# ----------------------------------------------------------------------


def rig_file(tmp_path: Path, reward: RewardHwConfig | None = LAB_LINE) -> Path:
    path = tmp_path / "rig-lab.yaml"
    line = (
        f"  reward: {{backend: {reward.backend}, device: {reward.device}, "
        f"channel: {reward.channel}, voltage: {reward.voltage}}}\n"
        if reward is not None
        else ""
    )
    path.write_text(
        "monitor: {width_px: 1920, height_px: 1080, width_cm: 53.0, distance_cm: 57.0,"
        " refresh_rate_hz: 60.0}\n"
        "display: {backend: simulated}\n"
        f"devices:\n{line}"
        f"data_root: {tmp_path / 'data'}\n",
        encoding="utf-8",
    )
    return path


def build_on_file(path: Path, task: Task):
    return build_session(
        rig=path,
        subject="t01",
        session=1,
        run=1,
        task=task,
        seed=1,
        iti=Duration(ms=0),
        simulated_frame_period_s=0.0,
        date_yyyymmdd="20261008",
    )


def measured(path: Path, *, pulse_ms=200, ul=120.0, line="Dev1/ao0", voltage=5.0):
    return record_reward_calibration(
        path,
        {
            "pulse_ms": pulse_ms,
            "ul_per_pulse": ul,
            "line": line,
            "voltage": voltage,
            "measured_at": "2026-10-08T12:00:00+00:00",
        },
    )


class TestCalibrationFile:
    def test_a_new_measurement_replaces_a_width_and_keeps_the_old_one(self, tmp_path):
        path = rig_file(tmp_path)
        measured(path, ul=100.0)
        measured(path, ul=120.0)
        measured(path, pulse_ms=50, ul=20.0)
        stored = load_reward_calibration(path)
        assert stored is not None
        assert stored["widths"][200]["ul_per_pulse"] == 120.0
        assert stored["widths"][200]["previous"][0]["ul_per_pulse"] == 100.0
        assert stored["widths"][50]["ul_per_pulse"] == 20.0
        assert reward_calibration_path(path).name == "rig-lab.reward.yaml"

    def test_only_the_same_width_line_and_voltage_count(self, tmp_path):
        path = rig_file(tmp_path)
        measured(path)
        stored = load_reward_calibration(path)
        assert ul_per_pulse(stored, pulse_ms=200, line="Dev1/ao0", voltage=5.0)
        assert ul_per_pulse(stored, pulse_ms=150, line="Dev1/ao0", voltage=5.0) is None
        assert ul_per_pulse(stored, pulse_ms=200, line="Dev1/ao1", voltage=5.0) is None
        assert ul_per_pulse(stored, pulse_ms=200, line="Dev1/ao0", voltage=4.0) is None
        assert load_reward_calibration(tmp_path / "rig-none.yaml") is None


class TestRewardInMicrolitres:
    def asking(self, ul: float = 250.0) -> Params:
        return monkey(by_outcome={"HIT": {"volume_ul": ul, "pulse_ms": 200}})

    def test_a_volume_becomes_pulses_from_the_measured_width(self, tmp_path, daq):
        path = rig_file(tmp_path)
        measured(path, ul=120.0)
        runner = build_on_file(path, OutcomeTask(self.asking(250.0)))
        policy = runner._payer.policy
        assert policy is not None
        assert policy.by_outcome["HIT"].n_pulses == 2  # 250 / 120, rounded
        runner.run()
        assert len(daq.tasks) == PLAN.count("HIT")
        assert all(sum(np.asarray(ao.written) > 0) == 2 * 200 for ao in daq.tasks)
        volumes = card(tmp_path)["reward"]["volumes"]["HIT"]
        assert volumes["n_pulses"] == 2 and volumes["ul_per_pulse"] == 120.0
        assert volumes["ul"] == 240.0 and volumes["volume_ul_asked"] == 250.0

    def test_a_volume_with_no_measurement_is_refused(self, tmp_path):
        with pytest.raises(ConfigError, match="no measured µL per 200 ms pulse"):
            build_on_file(rig_file(tmp_path), OutcomeTask(self.asking()))

    def test_a_measurement_on_another_line_is_not_used(self, tmp_path):
        path = rig_file(tmp_path)
        measured(path, line="Dev2/ao0")
        with pytest.raises(ConfigError, match="no measured"):
            build_on_file(path, OutcomeTask(self.asking()))

    def test_pulses_given_as_a_count_show_their_measured_volume(self, tmp_path, daq):
        path = rig_file(tmp_path)
        measured(path, ul=120.0)
        build_on_file(path, OutcomeTask(monkey())).run()
        volumes = card(tmp_path)["reward"]["volumes"]["HIT"]
        assert volumes == {
            "n_pulses": 2,
            "pulse_ms": 200,
            "volume_ul_asked": None,
            "ul_per_pulse": 120.0,
            "ul": 240.0,
            "measured_at": "2026-10-08T12:00:00+00:00",
        }

    def test_with_nothing_measured_the_record_claims_no_volume(self, tmp_path, daq):
        build_on_file(rig_file(tmp_path), OutcomeTask(monkey())).run()
        volumes = card(tmp_path)["reward"]["volumes"]["HIT"]
        assert volumes["ul_per_pulse"] is None and volumes["ul"] is None
