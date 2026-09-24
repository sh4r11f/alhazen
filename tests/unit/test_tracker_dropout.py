"""Dropout detection end to end: a session on a real tracker backend whose
recording dies mid-trial.

PR #54 made a trial whose eye tracker stopped recording a system fault —
``ABORTED`` with ``fault: tracker_stopped``, the task's ``on_fault`` paid,
served again, held against nobody — but only the scripted tracker could ever
trigger it: the real backends' ``is_recording()`` was a flag they set
themselves. These sessions run the real EyeLink backend against a simulated
Host PC, and the real TRACKPixx3 backend against a simulated DATAPixx3
(fake_sdk.py), through the session's own runner and the builder's own health
check, on simulated time. Nothing sleeps.

What they pin, beyond #54's handling being reached at all:

- the row's ``fault_detail`` and the WARNING line say which kind of stop it
  was — a Host PC that stopped, a cable pulled, a device gone;
- a blinking subject is never a dropout;
- a tracker that drops out on ``max_consecutive_dropouts`` trials in a row
  stops the session at the pause screen, headed with what it said;
- a tracker that is gone for good ends the session at the next trial's
  start, loudly and in the rig's words — after the lost trial's row is safe.
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from typing import Any

import pytest

from alhazen.config.models import EyeTrackerConfig, RewardPulses
from alhazen.core.trial import (
    FAULT_TRACKER_STOPPED,
    NO_FAULT,
    Outcome,
    PhaseAction,
    lost_to_fault,
)
from alhazen.devices.eyetracker import EyeLinkTracker, ViewPixxTracker
from alhazen.devices.reward import SimulatedReward
from alhazen.errors import TrackerError
from alhazen.session.pause import FAULT_COLOR
from alhazen.session.runner import SessionRunner
from alhazen.stimuli.base import NullStimulus
from alhazen.task.phases import TrialFeedback
from alhazen.task.plan import TrialPlan
from alhazen.task.reward_policy import RewardPolicy
from alhazen.testing import FakeClock
from fake_sdk import (
    ABORT_EXPT,
    MISSING_DATA,
    FakeEyeLinkHost,
    FakeTrackPixx,
    install_fake_pylink,
    install_fake_pypixxlib,
)
from support import FRAME_S, SCREEN, RunForFrames, SessionHarness

CORRECT = Outcome("CORRECT", completed=True, success=True)
# What a correct response earns, and — deliberately different, so a test can
# tell which reached the valve — what a trial the tracker cut short is paid.
PAID = RewardPulses(n_pulses=2, pulse_ms=100, inter_pulse_ms=50)
FAULT_PAY = RewardPulses(n_pulses=1, pulse_ms=60, inter_pulse_ms=0)
POLICY = RewardPolicy(by_outcome={"CORRECT": PAID}, on_fault=FAULT_PAY)

# A plan entry builds one attempt's phases against the session's device.
Entry = Callable[[Any], list[Any]]


class RigFails(RunForFrames):
    """A measuring phase during which the rig fails, on the frame the test
    picks: ``fail`` runs then — the Host PC stops, a cable is pulled, the
    DATAPixx3 goes away. Test-only: a real phase never touches a device.
    The session time it failed at is kept, to time the detection by."""

    name = "rig_fails"

    def __init__(self, fail: Callable[[], None], on_frame: int = 3) -> None:
        # Long enough that only the dropout can end it.
        super().__init__(60, CORRECT)
        self._fail = fail
        self._on_frame = on_frame
        self._frame = 0
        self.failed_at: float | None = None

    def on_frame(self, ctx):
        if self._frame == self._on_frame:
            self._fail()
            self.failed_at = ctx.clock.now()
        self._frame += 1
        return super().on_frame(ctx)


class StopsDuringFeedback(TrialFeedback):
    """TrialFeedback during which the rig fails: after the measurement."""

    def __init__(self, fail: Callable[[], None]) -> None:
        super().__init__(verdict=lambda ctx: True, then=CORRECT, duration_s=12 * FRAME_S)
        self._fail = fail

    def on_enter(self, ctx):
        super().on_enter(ctx)
        self._fail()


def clean(device) -> list[Any]:
    """A trial the tracker records through: CORRECT after a few frames."""
    return [RunForFrames(4, CORRECT)]


def run(
    tmp_path, tracker, device, plan: list[Entry], *, n_trials: int = 1, clock: FakeClock
) -> SessionHarness:
    """A session of one condition on ``tracker``, serving ``plan`` one entry
    per attempt, until ``n_trials`` have completed. The runner is left to
    the caller to run, so a test can expect it to raise."""
    served = iter(plan)
    phases_by_attempt: list[list[Any]] = []

    def build(setup):
        phases = next(served)(device)
        phases_by_attempt.append(phases)
        return TrialPlan(phases=phases, stimuli={"fixation": NullStimulus("fixation")})

    harness = SessionHarness(
        tmp_path,
        n_trials=n_trials,
        build_trial=build,
        tracker=tracker,
        clock=clock,
        reward=SimulatedReward(),
        reward_policy=POLICY,
    )
    harness.phases_by_attempt = phases_by_attempt  # type: ignore[attr-defined]
    return harness


def rows(harness) -> list[dict[str, str]]:
    with harness.paths.trials_path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def log_lines(harness, level: str = "WARNING") -> list[str]:
    lines = harness.paths.log_path.read_text(encoding="utf-8").splitlines()
    return [line for line in lines if f" {level} " in line]


# ---------------------------------------------------------------------------
# The EyeLink
# ---------------------------------------------------------------------------


@pytest.fixture
def eyelink(monkeypatch) -> tuple[EyeLinkTracker, FakeEyeLinkHost, FakeClock]:
    """A connected EyeLink backend, the simulated Host PC behind it, and the
    clock both run on. Connected here because the builder is what connects a
    tracker in a real session, and these sessions are wired by hand."""
    clock = FakeClock()
    sdk = install_fake_pylink(monkeypatch, clock)
    tracker = EyeLinkTracker(EyeTrackerConfig(backend="eyelink"), None, SCREEN, clock)
    tracker.connect()
    (host,) = sdk.hosts
    return tracker, host, clock


def host_stops(code: int | None = None) -> Entry:
    return lambda host: [RigFails(host.host_stop if code is None else lambda: host.host_stop(code))]


def cable_pulled() -> Entry:
    return lambda host: [RigFails(host.pull_cable)]


def link_dies() -> Entry:
    return lambda host: [RigFails(host.link_down)]


class TestAnEyeLinkThatStopsRecording:
    def test_a_host_pc_stop_mid_trial_is_a_system_fault(self, tmp_path, eyelink):
        tracker, host, clock = eyelink
        harness = run(tmp_path, tracker, host, [host_stops(ABORT_EXPT), clean], clock=clock)
        harness.runner.run()

        first, second = rows(harness)
        # #54's handling, reached by a real backend: aborted, flagged, served
        # again, the fault reward paid.
        assert (first["outcome"], first["abort_reason"], first["fault"]) == (
            "ABORTED",
            FAULT_TRACKER_STOPPED,
            FAULT_TRACKER_STOPPED,
        )
        assert lost_to_fault(first["outcome"], first) == FAULT_TRACKER_STOPPED
        assert (second["attempt"], second["outcome"], second["fault"]) == ("2", "CORRECT", NO_FAULT)
        assert harness.reward.deliveries == [FAULT_PAY, PAID]
        # What the tracker said: the samples stopped, and the Host PC says
        # its operator aborted — on the row, and in the WARNING line.
        detail = first["fault_detail"]
        assert detail.startswith("no new sample from the EyeLink for ")
        assert "isRecording 3, ABORT_EXPT" in detail
        (line,) = [line for line in log_lines(harness) if "a system fault" in line]
        assert detail in line

    def test_it_is_caught_within_the_limit_of_the_stop(self, tmp_path, eyelink):
        tracker, host, clock = eyelink
        harness = run(tmp_path, tracker, host, [host_stops(), clean], clock=clock)
        harness.runner.run()

        failing = harness.phases_by_attempt[0][0]
        first = rows(harness)[0]
        # The trial ended no later than the 50 ms limit after the stop, plus
        # the frames the engine needs to see it and blank the screen.
        assert failing.failed_at is not None
        assert float(first["t_trial_end"]) - failing.failed_at <= 0.050 + 3 * FRAME_S

    def test_a_pulled_cable_is_told_from_a_host_pc_stop(self, tmp_path, eyelink):
        tracker, host, clock = eyelink
        harness = run(tmp_path, tracker, host, [cable_pulled(), clean], clock=clock)
        harness.runner.run()

        detail = rows(harness)[0]["fault_detail"]
        assert "still reports recording (isRecording 0)" in detail
        assert "check the link cable" in detail

    def test_a_blinking_subject_is_not_a_dropout(self, tmp_path, eyelink):
        tracker, host, clock = eyelink
        host.gaze = (MISSING_DATA, MISSING_DATA)  # a second of blinks
        harness = run(
            tmp_path, tracker, host, [lambda host: [RunForFrames(60, CORRECT)]], clock=clock
        )
        harness.runner.run()

        (row,) = rows(harness)
        assert (row["outcome"], row["fault"]) == ("CORRECT", NO_FAULT)

    def test_a_stop_during_feedback_flags_the_row_with_the_trackers_words(self, tmp_path, eyelink):
        tracker, host, clock = eyelink
        plan = [
            lambda host: [RunForFrames(2, PhaseAction.ADVANCE), StopsDuringFeedback(host.host_stop)]
        ]
        harness = run(tmp_path, tracker, host, plan, clock=clock)
        harness.runner.run()

        (row,) = rows(harness)
        # After the measurement: the outcome stands and is paid as a response.
        assert (row["outcome"], row["fault"]) == ("CORRECT", FAULT_TRACKER_STOPPED)
        assert row.get("abort_reason", "") == ""
        assert "isRecording -1, TRIAL_ERROR" in row["fault_detail"]
        assert harness.reward.deliveries == [PAID]


class TestDropoutsInARow:
    """Each dropout is served again; a tracker that keeps dropping out would
    serve the same trial into a dead tracker forever, paying its fault reward
    every time. At max_consecutive_dropouts in a row the session pauses."""

    def headings(self, harness) -> list[tuple[str, tuple[float, float, float]]]:
        return [
            (title, color)
            for title, _body, color in harness.display.menus
            if title.startswith("THE EYE TRACKER DROPPED OUT")
        ]

    def test_three_in_a_row_pause_the_session_with_what_the_tracker_said(self, tmp_path, eyelink):
        tracker, host, clock = eyelink
        plan = [cable_pulled(), cable_pulled(), cable_pulled(), clean]
        harness = run(tmp_path, tracker, host, plan, clock=clock)
        harness.runner.run()

        ((title, color),) = self.headings(harness)
        assert title.startswith("THE EYE TRACKER DROPPED OUT ON 3 TRIALS IN A ROW — last: ")
        assert "no new sample from the EyeLink for" in title
        assert "still reports recording (isRecording 0)" in title
        assert "Check the tracker and its connection" in title
        assert color == FAULT_COLOR
        assert any(
            "a device failed its health check (tracker_stopped) on 3 trials in a row" in line
            for line in log_lines(harness)
        )
        assert [row["outcome"] for row in rows(harness)] == ["ABORTED"] * 3 + ["CORRECT"]

    def test_two_in_a_row_do_not(self, tmp_path, eyelink):
        tracker, host, clock = eyelink
        harness = run(tmp_path, tracker, host, [cable_pulled(), cable_pulled(), clean], clock=clock)
        harness.runner.run()
        assert self.headings(harness) == []

    def test_a_trial_the_tracker_recorded_through_restarts_the_count(self, tmp_path, eyelink):
        tracker, host, clock = eyelink
        plan = [cable_pulled(), cable_pulled(), clean, cable_pulled(), cable_pulled(), clean]
        harness = run(tmp_path, tracker, host, plan, n_trials=2, clock=clock)
        harness.runner.run()
        assert self.headings(harness) == []

    def test_the_count_starts_again_after_its_pause(self, tmp_path, eyelink):
        tracker, host, clock = eyelink
        plan = [cable_pulled()] * 6 + [clean]
        harness = run(tmp_path, tracker, host, plan, clock=clock)
        harness.runner.run()
        assert len(self.headings(harness)) == 2

    def test_the_limit_is_the_rigs_and_none_never_pauses(self, tmp_path, eyelink):
        tracker, host, clock = eyelink
        harness = run(tmp_path, tracker, host, [cable_pulled()] * 3 + [clean], clock=clock)
        harness.runner._max_consecutive_dropouts = None
        harness.runner.run()
        assert self.headings(harness) == []

    def test_a_limit_below_one_is_refused(self, tmp_path):
        harness = SessionHarness(tmp_path)
        runner = harness.runner
        with pytest.raises(ValueError, match="max_consecutive_dropouts must be >= 1"):
            SessionRunner(
                cfg=runner._cfg,
                paths=runner._paths,
                display=runner._display,
                screen=runner._screen,
                clock=runner._clock,
                bus=runner._bus,
                engine=runner._engine,
                source=runner._source,
                build_trial=runner._build_trial,
                recorder=runner._recorder,
                frame_monitor=runner._frame_monitor,
                commands=runner._commands,
                refresh_rate_hz=60.0,
                task_rng=runner._task_rng,
                max_consecutive_dropouts=0,
            )


class TestAnEyeLinkThatIsGone:
    """The link dies mid-trial and stays dead. The trial is aborted and its
    row kept; the stop and the messages after it are logged, not raised;
    and the next trial's start ends the session, in the rig's words."""

    def test_the_next_start_ends_the_session_loudly_with_the_lost_trial_kept(
        self, tmp_path, eyelink
    ):
        tracker, host, clock = eyelink
        harness = run(tmp_path, tracker, host, [link_dies(), clean], clock=clock)
        with pytest.raises(TrackerError) as excinfo:
            harness.runner.run()

        message = str(excinfo.value)
        assert message.startswith("EyeLink could not start recording at trial 2: the link failed")
        assert "the Host PC at 100.1.1.1" in message
        assert "The previous trial's recording had already been lost" in message
        # The trial the dropout cost is on disk, with what the tracker said.
        (row,) = rows(harness)
        assert (row["outcome"], row["fault"]) == ("ABORTED", FAULT_TRACKER_STOPPED)
        assert "did not answer isRecording() (link terminated)" in row["fault_detail"]
        # Everything that failed on the way is in the log, none of it raised.
        warnings = "\n".join(log_lines(harness))
        assert "stopRecording() failed after this trial's dropout" in warnings
        assert "was not written into the EDF" in warnings
        assert any("session end: FAILED" in line for line in log_lines(harness, "ERROR"))


# ---------------------------------------------------------------------------
# The TRACKPixx3
# ---------------------------------------------------------------------------


@pytest.fixture
def trackpixx(monkeypatch) -> tuple[ViewPixxTracker, FakeTrackPixx, FakeClock]:
    """A connected and configured TRACKPixx3 backend with no reader thread
    (the session's frames read for it, so time is simulated), the simulated
    device behind it, and the clock."""
    clock = FakeClock()
    device = install_fake_pypixxlib(monkeypatch)
    tracker = ViewPixxTracker(
        EyeTrackerConfig(backend="viewpixx"), None, SCREEN, clock, background_gaze=False
    )
    tracker.connect()
    tracker.configure(SCREEN, clock)
    return tracker, device, clock


class TestATrackPixx3ThatStopsRecording:
    def test_the_device_stopping_its_recording_mid_trial_is_a_system_fault(
        self, tmp_path, trackpixx
    ):
        tracker, device, clock = trackpixx
        # Another program switches the device's sampling off mid-trial. The
        # live gaze report carries on; the recording does not.
        plan = [lambda device: [RigFails(device.libdpx.TPxDisableFreeRun)], clean]
        harness = run(tmp_path, tracker, device, plan, clock=clock)
        harness.runner.run()

        first, second = rows(harness)
        assert (first["outcome"], first["fault"]) == ("ABORTED", FAULT_TRACKER_STOPPED)
        assert first["fault_detail"] == (
            "the TRACKPixx3 stopped recording samples into the session's buffer: "
            "free-run sampling is off"
        )
        assert harness.reward.deliveries == [FAULT_PAY, PAID]
        # The next trial found the ring re-armed and recorded through.
        assert (second["outcome"], second["fault"]) == ("CORRECT", NO_FAULT)
        assert device.libdpx.freerun

    def test_a_device_that_went_away_ends_the_session_loudly(self, tmp_path, trackpixx):
        tracker, device, clock = trackpixx
        plan = [lambda device: [RigFails(device.libdpx.unplug)], clean]
        harness = run(tmp_path, tracker, device, plan, clock=clock)
        with pytest.raises(TrackerError, match="TRACKPixx3 is not answering at trial 2"):
            harness.runner.run()

        (row,) = rows(harness)
        assert (row["outcome"], row["fault"]) == ("ABORTED", FAULT_TRACKER_STOPPED)
        assert "did not answer a register read" in row["fault_detail"]
        warnings = "\n".join(log_lines(harness))
        assert "left out of the message record" in warnings
        errors = "\n".join(log_lines(harness, "ERROR"))
        assert "could not be saved after this trial's dropout" in errors
