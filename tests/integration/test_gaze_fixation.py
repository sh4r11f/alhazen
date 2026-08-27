"""Acceptance: the gaze_fixation example, driven end to end by scripted
gaze on a machine with no tracker, no renderer and no DAQ.

Everything here runs the example's own task module — the same phases a
mouse_sim or EyeLink rig would run — with a ScriptedTracker standing in for
the eye and simulated reward/sync standing in for the DAQ.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from alhazen import Duration
from alhazen.core.commands import Command
from alhazen.devices.eyetracker import GazeSample, ScriptedTracker
from alhazen.devices.reward import SimulatedReward
from alhazen.devices.sync import SimulatedSync
from alhazen.paradigms.config import SchedulerConfig
from alhazen.testing import FakeClock, ScriptedCommands
from support import FRAME_S, SessionHarness, load_example_task

EXAMPLE_DIR = Path(__file__).parents[2] / "examples" / "gaze_fixation"

# Screen px, origin top-left, y down — the frame a tracker reports in. The
# centre of the harness's 1920x1080 screen is inside the task's fixation
# window; the other point is far outside it.
CENTRE = (960.0, 540.0)
OFF_TARGET = (100.0, 100.0)

LINES = {"TRIAL_START": "Dev1/port0/line0", "FIX_ACQUIRED": "Dev1/port0/line2"}


def gaze_at(t: float, position: tuple[float, float]) -> tuple[float, GazeSample]:
    return (t, GazeSample(gx=position[0], gy=position[1], t=t))


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open() as f:
        return list(csv.DictReader(f))


class DropoutTracker(ScriptedTracker):
    """A tracker whose link goes down mid-trial: it stops recording on the
    n-th gaze poll, with no error and no warning — exactly the failure the
    engine's health check exists to catch.

    Exactly once (``==``, not ``>=``): an aborted trial is re-served, and a
    tracker that dropped out on every attempt would abort the same condition
    forever.
    """

    def __init__(self, samples, clock, drop_after: int) -> None:
        super().__init__(samples, clock)
        self._drop_after = drop_after
        self._polls = 0

    def get_gaze(self):
        self._polls += 1
        if self._polls == self._drop_after:
            self.stop_recording()
        return super().get_gaze()


@pytest.fixture
def scripted_session(tmp_path):
    """Build a full session running the example's phases against a scripted
    gaze trajectory. Durations are in frames so the whole trial is exact."""

    def make(script, *, n_trials=1, commands=None, tracker_class=ScriptedTracker, **tracker_kw):
        task = load_example_task(EXAMPLE_DIR)
        params = task.GazeFixationParams(
            acquire_timeout=Duration(frames=10), hold_duration=Duration(frames=3)
        )
        # One clock for the session and the tracker, so gaze and frames
        # advance together and no wall-clock time passes at all.
        clock = FakeClock()
        tracker = tracker_class(script, clock, **tracker_kw)
        harness = SessionHarness(
            tmp_path,
            n_trials=n_trials,
            commands=commands,
            build_trial=task.GazeFixationTask(params).build_trial,
            declared_events=("FIX_ON", "FIX_ACQUIRED"),
            tracker=tracker,
            reward=SimulatedReward(),
            sync=SimulatedSync(LINES),
            event_lines=LINES,
            clock=clock,
        )
        return harness, task

    return make


class TestGazeDrivesTheTrial:
    def test_gaze_entering_the_window_completes_the_trial(self, scripted_session):
        # Off target at first, fixating from the third frame on.
        harness, _ = scripted_session([gaze_at(0.0, OFF_TARGET), gaze_at(2 * FRAME_S, CENTRE)])
        harness.runner.run()

        rows = read_rows(harness.paths.trials_path)
        assert [r["outcome"] for r in rows] == ["FIXATED"]
        assert rows[0]["success"] == "True"
        assert float(rows[0]["acquire_latency_s"]) > 0
        assert "FIX_ACQUIRED" in harness.collector.names()

    def test_gaze_that_never_arrives_times_out(self, scripted_session):
        harness, _ = scripted_session([gaze_at(0.0, OFF_TARGET)])
        harness.runner.run()

        rows = read_rows(harness.paths.trials_path)
        assert [r["outcome"] for r in rows] == ["NO_FIXATION"]
        assert "FIX_ACQUIRED" not in harness.collector.names()

    def test_unverifiable_gaze_is_outside_the_window(self, scripted_session):
        # The blink rule, end to end: gaze acquires, then goes dark. A blink
        # is never credited as continued fixation, so the hold breaks — and
        # a broken trial is re-served, which is why the second attempt (gaze
        # still dark) times out rather than completing.
        harness, _ = scripted_session([gaze_at(0.0, CENTRE), (2 * FRAME_S, None)])
        harness.runner.run()

        rows = read_rows(harness.paths.trials_path)
        assert [r["outcome"] for r in rows] == ["FIX_BREAK", "NO_FIXATION"]


class TestDeviceIntegration:
    def test_tracker_stopping_mid_trial_aborts_the_trial(self, scripted_session):
        harness, _ = scripted_session(
            [gaze_at(0.0, CENTRE)], tracker_class=DropoutTracker, drop_after=2
        )
        harness.runner.run()

        rows = read_rows(harness.paths.trials_path)
        # A trial that ran on without eye data would look like a normal trial
        # with nothing in the record to say otherwise. The aborted attempt is
        # re-served and, with the link back, completes.
        assert [r["outcome"] for r in rows] == ["ABORTED", "FIXATED"]
        assert rows[0]["abort_reason"] == "tracker_stopped"

    def test_messages_events_and_sync_pulses_agree(self, scripted_session):
        harness, _ = scripted_session([gaze_at(0.0, CENTRE)])
        harness.runner.run()

        recorded = [row["event"] for row in read_rows(harness.paths.events_path)]
        # One EDF mark per row of events.csv, so the eye trace can be lined
        # up with the table afterwards.
        assert harness.tracker.sent_messages == [name.lower() for name in recorded]
        # And exactly the mapped events reached a physical line.
        assert harness.sync.pulses == [LINES[name] for name in recorded if name in LINES]

    def test_tracker_lifecycle_is_per_trial_and_guaranteed(self, scripted_session):
        harness, _ = scripted_session([gaze_at(0.0, CENTRE)], n_trials=2)
        harness.runner.run()

        assert [index for index, _ in harness.tracker.trials_started] == [1, 2]
        assert not harness.tracker.is_recording()  # stopped, however the last trial ended
        # The operator overlay is refreshed per trial: a fixation cross plus
        # one box for the task's own fixation window.
        assert len(harness.tracker.overlays) == 2
        assert [shape.kind for shape in harness.tracker.overlays[0]] == ["cross", "box"]

    def test_teardown_retrieves_the_recording_into_the_run_directory(self, scripted_session):
        harness, _ = scripted_session([gaze_at(0.0, CENTRE)])
        harness.runner.run()
        assert harness.tracker.shutdowns == [harness.paths.run_dir / f"{harness.paths.base}.edf"]

    def test_manual_reward_reaches_the_dispenser(self, scripted_session):
        harness, _ = scripted_session(
            [gaze_at(0.0, CENTRE)], commands=ScriptedCommands([[Command.MANUAL_REWARD]])
        )
        harness.runner.run()
        assert len(harness.reward.deliveries) == 1
        assert "REWARD" in harness.collector.names()


@pytest.mark.display
def test_mouse_sim_drives_the_same_task_on_a_real_window(tmp_path):
    """The rig path: a real PsychoPy window, gaze from the mouse cursor.

    Excluded from the default suite (needs the [psychopy] extra and a
    screen). It is the counterpart of the scripted tests above: same task,
    same phases, same data files — only the source of gaze differs, which is
    the whole point of putting the tracker behind a protocol. Nobody moves
    the mouse during the run, so the trial ends however the cursor happens to
    sit; what is asserted is that the gaze path was live end to end.
    """
    from alhazen import RewardPulses, build_session
    from alhazen.config.loader import load_rig
    from alhazen.devices.eyetracker import MouseSimTracker

    task = load_example_task(EXAMPLE_DIR)
    rig = load_rig(EXAMPLE_DIR / "rig-mouse.yaml").model_copy(update={"data_root": tmp_path})
    params = task.GazeFixationParams(
        acquire_timeout=Duration(ms=500),
        hold_duration=Duration(ms=200),
        paradigm=SchedulerConfig(n_per_condition=1),
    )
    runner = build_session(
        rig=rig,
        subject="demo",
        session=1,
        run=1,
        task=task.GazeFixationTask(params),
        seed=1,
        iti=Duration(ms=0),
        reward_pulses=RewardPulses(n_pulses=1, pulse_ms=50, inter_pulse_ms=0),
        windowed=True,
        date_yyyymmdd="20260826",
    )
    assert isinstance(runner._tracker, MouseSimTracker)
    runner.run()

    run_dir = tmp_path / "sub-demo" / "ses-001" / "run-01_task-gaze-fixation"
    rows = read_rows(next(run_dir.glob("*_trials.csv")))
    assert len(rows) >= 1
    assert rows[-1]["outcome"] in {"FIXATED", "NO_FIXATION", "FIX_BREAK"}
