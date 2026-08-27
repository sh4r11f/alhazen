"""The examples run end to end, and a full gaze-contingent trial state
machine is expressible with library phases alone.

That last one is the probe that matters for anyone adopting the framework.
If a four-phase saccade trial — acquire, hold, respond, land — and its seven
outcomes compose from ``alhazen.task.phases`` with no phase written by hand,
then bringing an existing experiment across is a matter of its stimuli and
its analysis, not its trial logic.
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
import pytest

from alhazen import CircleRegion, Condition, Duration, InputFrame, build_session
from alhazen.config.models import DisplayConfig, RigConfig
from alhazen.core.events import EventSchema
from alhazen.core.trial import Outcome, outcomes
from alhazen.task import phases
from alhazen.testing import FakeStimulus, ScriptedInputs
from support import FRAME_S, MONITOR, EngineHarness, load_example_task

EXAMPLES = Path(__file__).parents[2] / "examples"


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open() as f:
        return list(csv.DictReader(f))


def sim_rig(tmp_path) -> RigConfig:
    return RigConfig(
        monitor=MONITOR, display=DisplayConfig(backend="simulated"), data_root=tmp_path
    )


class TestMinimalFixationExample:
    def test_runs_as_a_task(self, tmp_path):
        task_module = load_example_task(EXAMPLES / "minimal_fixation")
        params = task_module.FixationParams(fixation_duration=Duration(frames=2))
        runner = build_session(
            rig=sim_rig(tmp_path),
            subject="demo",
            session=1,
            run=1,
            task=task_module.MinimalFixationTask(params),
            seed=1,
            simulated_frame_period_s=0.0,
            date_yyyymmdd="20260826",
        )
        runner.run()
        rows = read_rows(next(tmp_path.rglob("*_trials.csv")))
        assert len(rows) == 3  # the params' paradigm block: one condition, three times
        assert all(row["outcome"] == "COMPLETED" for row in rows)


class TestSceneStimulusExample:
    """The scene example, run headless end to end.

    Its scene is a drifting Gabor from illusion-studio; nothing else in the
    suite runs it. Loading a scene proves the JSON parses — this proves the
    frames come out, which is where the renderer's bugs live."""

    def run_session(self, tmp_path, **overrides):
        from alhazen.core.clock import MonotonicClock
        from alhazen.devices.eyetracker import GazeSample, ScriptedTracker

        task_module = load_example_task(EXAMPLES / "scene_stimulus")
        params = task_module.SceneParams(
            stimulus_duration=Duration(frames=3),
            orientations_deg=[0.0, 90.0],
            **overrides,
        )
        # A gaze parked at the screen's centre from t=0. Without one, fixation
        # is never acquired, the trial's FIX_BREAK outcome is incomplete, the
        # scheduler re-queues it, and the session never ends.
        centre = GazeSample(gx=MONITOR.width_px / 2, gy=MONITOR.height_px / 2, t=0.0)
        runner = build_session(
            rig=sim_rig(tmp_path),
            subject="demo",
            session=1,
            run=1,
            task=task_module.SceneTask(params),
            tracker=ScriptedTracker([(0.0, centre)], MonotonicClock()),
            seed=1,
            simulated_frame_period_s=0.0,
            date_yyyymmdd="20260826",
        )
        return task_module, runner

    def test_the_example_runs_and_records_its_orientations(self, tmp_path):
        _module, runner = self.run_session(tmp_path)
        runner.run()

        rows = read_rows(next(tmp_path.rglob("*_trials.csv")))
        assert rows, "the scene example produced no trials"
        assert {row["orientation_deg"] for row in rows} == {"0.0", "90.0"}

    def test_the_scene_actually_drew_something_that_moved(self, tmp_path):
        """A stimulus that renders a blank frame every time would pass every
        other assertion here."""
        from alhazen.scenes import SceneStimulus

        _module, runner = self.run_session(tmp_path)
        drawn: list[SceneStimulus] = []
        # Wrapped on the runner rather than on the class: the runner already
        # holds the task's bound method, so patching the class afterwards
        # would change nothing.
        build = runner._build_trial

        def capture(setup):
            plan = build(setup)
            drawn.append(plan.stimuli["scene"])
            return plan

        runner._build_trial = capture
        runner.run()

        assert drawn, "no trial was built"
        frames = [frame for stimulus in drawn for frame in stimulus.frames]
        assert frames, "the scene stimulus never drew"
        # A Gabor on a grey background: something is brighter and something
        # darker than the mid-grey it sits on.
        assert max(frame.max() for frame in frames) > 160
        assert min(frame.min() for frame in frames) < 100
        # And its phase advanced: the first and last frame of a trial differ.
        moving = [s for s in drawn if len(s.frames) > 1]
        assert moving and any(not np.array_equal(s.frames[0], s.frames[-1]) for s in moving)

    def test_the_scene_is_letterboxed_onto_the_rigs_screen(self, tmp_path):
        from alhazen.display.screen import Screen
        from alhazen.scenes import SceneStimulus, load_scene

        scene = load_scene(EXAMPLES / "scene_stimulus" / "drifting_gabor.json")
        screen = Screen.from_monitor(MONITOR)

        class Simulated:
            kind = "simulated"

        stimulus = SceneStimulus(Simulated(), screen, scene)

        # The scene declares 400x400 in the format's own top-level fields.
        assert (stimulus._width, stimulus._height) == (400, 400)
        assert stimulus.scale == pytest.approx(screen.height_px / 400)


class TestSaccadeExample:
    """The saccade example, driven by scripted gaze through the real engine."""

    def build(self, tmp_path, inputs, seed=1):
        task_module = load_example_task(EXAMPLES / "saccade_to_target")
        params = task_module.SaccadeParams(
            acquire_timeout=Duration(frames=6),
            fixation_hold=Duration(frames=2),
            fixation_jitter=Duration(ms=0),
            response_timeout=Duration(frames=6),
            landing_timeout=Duration(frames=6),
        )
        task = task_module.SaccadeTask(params)
        harness = EngineHarness(
            input_provider=ScriptedInputs(inputs),
            declared_events=tuple(sorted(task.events.declared)),
        )
        setup = _setup(tmp_path, task, harness, seed)
        plan = task.build_trial(setup)
        ctx = harness.ctx(stimuli=plan.stimuli, regions=plan.regions, record=dict(plan.record))
        return task, harness, ctx, plan

    def test_a_saccade_to_the_target_is_correct(self, tmp_path):
        # Fixate, hold, then move to the right-hand target.
        on_fix = InputFrame(gaze=(0.0, 0.0))
        on_target = InputFrame(gaze=(320.0, 0.0))  # 8 dva at 40 px/dva
        task, harness, ctx, plan = self.build(tmp_path, [on_fix, on_fix, on_fix, on_fix, on_target])
        result = harness.engine.run_trial(ctx, plan.phases)
        assert result.outcome.name == "CORRECT"
        assert result.record["endpoint_in_target"] is True
        scored = task.score(result.record)
        assert scored["gain"] == pytest.approx(1.0, abs=0.01)

    def test_a_saccade_somewhere_else_still_records_its_endpoint(self, tmp_path):
        on_fix = InputFrame(gaze=(0.0, 0.0))
        astray = InputFrame(gaze=(160.0, 0.0))  # halfway there
        task, harness, ctx, plan = self.build(tmp_path, [on_fix, on_fix, on_fix, on_fix, astray])
        result = harness.engine.run_trial(ctx, plan.phases)
        assert result.outcome.name == "MISSED_TARGET"
        assert task.score(result.record)["gain"] == pytest.approx(0.5, abs=0.01)

    def test_never_fixating_ends_before_any_stimulus(self, tmp_path):
        away = InputFrame(gaze=(900.0, 500.0))
        task, harness, ctx, plan = self.build(tmp_path, [away])
        result = harness.engine.run_trial(ctx, plan.phases)
        assert result.outcome.name == "FIX_NOT_ACQUIRED"
        assert "STIM_ON" not in harness.collector.names()

    def test_a_blink_during_the_hold_breaks_the_trial(self, tmp_path):
        on_fix = InputFrame(gaze=(0.0, 0.0))
        task, harness, ctx, plan = self.build(tmp_path, [on_fix, on_fix, InputFrame(gaze=None)])
        result = harness.engine.run_trial(ctx, plan.phases)
        assert result.outcome.name == "FIX_BREAK"


class TestStaircaseExample:
    def test_scripted_answers_drive_the_staircases(self, tmp_path):
        task_module = load_example_task(EXAMPLES / "staircase_detection")
        params = task_module.DetectionParams(
            stimulus_duration=Duration(frames=1), response_timeout=Duration(frames=4)
        )
        task = task_module.DetectionTask(params)
        rng = np.random.default_rng(0)
        source = task.make_source(params, rng)

        # Answer every trial correctly: both staircases should march down
        # toward the floor and finish on their reversal counts.
        served = 0
        while (condition := source.next()) is not None and served < 200:
            served += 1
            source.record(condition, _result(_CORRECT))
        summary = source.summary()
        assert list(summary["staircase"]) == ["easy", "hard"]
        assert (summary["n_trials"] > 0).all()

    def test_the_titrated_contrast_reaches_the_drawn_stimulus(self, tmp_path):
        """The staircase's whole job is to move this number. It was recorded
        on the row and read by nothing that drew — a decorative adaptive
        variable, in the file users copy as their template."""
        task_module = load_example_task(EXAMPLES / "staircase_detection")
        task = task_module.DetectionTask(task_module.DetectionParams())
        harness = EngineHarness(declared_events=("STIM_ON", "RESPONSE_CUE", "RESPONSE"))

        setup = _setup(
            tmp_path,
            task,
            harness,
            seed=3,
            condition=Condition({"contrast": 0.17, "start": "easy"}),
        )
        plan = task.build_trial(setup)

        assert plan.stimuli["target"].contrast == pytest.approx(0.17)
        assert plan.record["contrast"] == pytest.approx(0.17)

    def test_the_stimulus_follows_the_staircase_down(self, tmp_path):
        task_module = load_example_task(EXAMPLES / "staircase_detection")
        task = task_module.DetectionTask(task_module.DetectionParams())
        harness = EngineHarness(declared_events=("STIM_ON", "RESPONSE_CUE", "RESPONSE"))

        drawn = []
        for value in (0.5, 0.4, 0.3):
            setup = _setup(
                tmp_path,
                task,
                harness,
                seed=3,
                condition=Condition({"contrast": value, "start": "easy"}),
            )
            drawn.append(task.build_trial(setup).stimuli["target"].contrast)

        assert drawn == pytest.approx([0.5, 0.4, 0.3])

    def test_the_task_runs_through_the_engine_with_scripted_keys(self, tmp_path):
        task_module = load_example_task(EXAMPLES / "staircase_detection")
        params = task_module.DetectionParams(
            stimulus_duration=Duration(frames=1), response_timeout=Duration(frames=6)
        )
        task = task_module.DetectionTask(params)
        harness = EngineHarness(
            input_provider=ScriptedInputs([InputFrame(), InputFrame(), InputFrame(keys=("left",))]),
            declared_events=("STIM_ON", "RESPONSE_CUE", "RESPONSE"),
        )
        setup = _setup(
            tmp_path,
            task,
            harness,
            seed=3,
            condition=Condition({"contrast": 0.5, "side": "right", "start": "easy"}),
        )
        plan = task.build_trial(setup)
        ctx = harness.ctx(stimuli=plan.stimuli, regions=plan.regions, record=dict(plan.record))
        result = harness.engine.run_trial(ctx, plan.phases)
        expected = "CORRECT" if plan.record["correct_key"] == "left" else "WRONG"
        assert result.outcome.name == expected
        assert result.record["response_key"] == "left"


class TestFullTrialStateMachineIsExpressible:
    """A complete gaze-contingent trial, composed from library phases only,
    with its own outcome set."""

    OUTCOMES = outcomes(
        CORRECT=dict(completed=True, success=True),
        MISSED_TARGET=dict(completed=True, success=False),
        FIX_NOT_ACQUIRED=dict(completed=False),
        FIX_BREAK=dict(completed=False),
        NO_SACCADE=dict(completed=False),
    )
    EVENTS = EventSchema(
        ("FIX_ON", "FIX_ACQUIRED", "NOISE_ON", "STIM_ON", "SACCADE_ONSET", "LANDED")
    )

    def trial(self):
        """The four phases, in order, with nothing hand-written."""
        return [
            phases.AcquireFixation(
                hold_s=2 * FRAME_S,
                timeout_s=8 * FRAME_S,
                on_timeout=self.OUTCOMES["FIX_NOT_ACQUIRED"],
                blink_period_s=2 * FRAME_S,
            ),
            phases.HoldFixation(
                duration_s=2 * FRAME_S,
                jitter_s=FRAME_S,
                on_break=self.OUTCOMES["FIX_BREAK"],
                concurrent=["noise"],
                onset_event="NOISE_ON",
            ),
            phases.StimulusResponse(
                stimulus_key="stimulus",
                depart_region="fixation",
                timeout_s=8 * FRAME_S,
                on_timeout=self.OUTCOMES["NO_SACCADE"],
                response_event="SACCADE_ONSET",
                rt_record_key="saccade_rt_ms",
            ),
            phases.LandingCheck(
                region="target",
                timeout_s=8 * FRAME_S,
                on_hit=self.OUTCOMES["CORRECT"],
                on_miss=self.OUTCOMES["MISSED_TARGET"],
                stimulus_keys=["stimulus"],
            ),
        ]

    def run(self, inputs):
        harness = EngineHarness(
            input_provider=ScriptedInputs(inputs),
            declared_events=tuple(sorted(self.EVENTS.declared)),
        )
        ctx = harness.ctx(
            stimuli={
                "fixation": FakeStimulus("fixation"),
                "noise": FakeStimulus("noise"),
                "stimulus": FakeStimulus("stimulus"),
            },
            regions={
                "fixation": CircleRegion((0.0, 0.0), 40.0),
                "target": CircleRegion((400.0, 0.0), 80.0),
            },
        )
        return harness, harness.engine.run_trial(ctx, self.trial())

    def test_every_outcome_is_reachable(self):
        fix = InputFrame(gaze=(0.0, 0.0))
        away = InputFrame(gaze=(900.0, 900.0))
        target = InputFrame(gaze=(400.0, 0.0))
        astray = InputFrame(gaze=(-400.0, 0.0))

        # Eight fixation frames covers the acquisition hold plus the longest
        # jittered foreperiod; the scripted inputs then switch for good.
        cases = {
            # Fixate, hold, saccade to the target.
            "CORRECT": [fix] * 8 + [target],
            # Fixate, hold, saccade somewhere else entirely.
            "MISSED_TARGET": [fix] * 8 + [astray],
            # Never look at the fixation point.
            "FIX_NOT_ACQUIRED": [away],
            # Acquire, then blink out during the hold.
            "FIX_BREAK": [fix, fix, fix, InputFrame(gaze=None)],
            # Acquire, hold, then never leave the window.
            "NO_SACCADE": [fix],
        }
        for expected, inputs in cases.items():
            _, result = self.run(inputs)
            assert result.outcome.name == expected, f"{expected} was not reachable"

    def test_the_correct_trial_records_every_measurement(self):
        fix = InputFrame(gaze=(0.0, 0.0))
        # Two fixation frames past the jittered foreperiod, so the departure
        # is detected some frames after stimulus onset rather than on the
        # first one — where the honest reaction time is exactly 0, the eye
        # having already been elsewhere when the stimulus appeared.
        harness, result = self.run([fix] * 10 + [InputFrame(gaze=(400.0, 0.0))])
        record = result.record
        assert record["saccade_rt_ms"] > 0
        assert record["endpoint_in_target"] is True
        assert record["endpoint_x_dva"] == pytest.approx(10.0)
        names = harness.collector.names()
        for event in ("FIX_ON", "FIX_ACQUIRED", "NOISE_ON", "STIM_ON", "SACCADE_ONSET", "LANDED"):
            assert event in names


_CORRECT = Outcome("CORRECT", completed=True, success=True)


def _result(outcome: Outcome):
    from alhazen.core.engine import TrialResult

    return TrialResult(outcome=outcome, record={})


def _setup(tmp_path, task, harness, seed, condition=None):
    """A TrialSetup for calling build_trial outside a session."""
    from alhazen.task.plan import TrialSetup
    from support import make_session_config

    return TrialSetup(
        cfg=make_session_config(tmp_path),
        screen=harness.ctx().screen,
        display=harness.display,
        rng=np.random.default_rng(seed),
        refresh_rate_hz=1 / FRAME_S,
        trial_index=1,
        attempt=1,
        condition=condition or Condition({"side": "right", "start": "easy"}),
    )
