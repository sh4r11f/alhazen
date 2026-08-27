"""Acceptance: a subject shaped through three stages, across two sessions,
with its place persisted between them."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
import yaml

from alhazen import build_session
from alhazen.config.loader import load_model, load_rig
from alhazen.core.clock import MonotonicClock
from alhazen.session.builder import make_gaze_input_provider
from alhazen.training import Curriculum, Ramp, Stage, StageCriteria, TrainingState
from support import load_example_task

EXAMPLE_DIR = Path(__file__).parents[2] / "examples" / "shaping_curriculum"


# The shipped curriculum is written in the timescale a real subject works on
# (a 1.5 s acquisition window, holds of tenths of a second), so a session of
# sixty trials takes tens of seconds of wall clock. These tests run the same
# protocol — same overrides, same ramp, same criteria shape — with the
# durations expressed in frames and the criteria's windows shortened, so the
# machinery is exercised in a couple of seconds. The shipped file itself is
# checked separately, by reading it.
FAST_CURRICULUM = Curriculum(
    stages=[
        Stage(
            name="any-look",
            overrides={"fix_window_dva": 8.0, "hold_duration": {"frames": 1}},
            reward_scale=2.0,
            criteria=StageCriteria(window=8, min_trials=4, promote_when={"completed_rate": 0.8}),
        ),
        Stage(
            name="tighten",
            overrides={"hold_duration": {"frames": 2}},
            reward_scale=1.5,
            ramps=[Ramp(param="fix_window_dva", start=8.0, end=3.0, over_completed_trials=8)],
            criteria=StageCriteria(
                window=8,
                min_trials=4,
                promote_when={"success_rate": 0.75},
                demote_when={"completed_rate": 0.3},
            ),
        ),
        Stage(name="real-task", criteria=StageCriteria(window=50, min_trials=50)),
    ]
)

FAST_PARAMS = {
    "hold_duration": {"frames": 2},
    "acquire_timeout": {"frames": 10},
    "iti": {"ms": 0},
}


def run_session(tmp_path: Path, trials: int, run: int) -> None:
    """One session of the shaping example, with its scripted subject."""
    import sys

    sys.path.insert(0, str(EXAMPLE_DIR))
    try:
        from run import ImprovingSubject  # the example's own stand-in subject
    finally:
        sys.path.remove(str(EXAMPLE_DIR))

    task_module = load_example_task(EXAMPLE_DIR)
    rig = load_rig(EXAMPLE_DIR / "rig-sim.yaml").model_copy(update={"data_root": tmp_path})
    params = load_model(EXAMPLE_DIR / "task.yaml", task_module.ShapingParams)
    params = task_module.ShapingParams.model_validate({**params.model_dump(), **FAST_PARAMS})
    params = params.model_copy(
        update={"paradigm": params.paradigm.model_copy(update={"n_per_condition": trials})}
    )
    curriculum = FAST_CURRICULUM

    runner = build_session(
        rig=rig,
        subject="m01",
        session=1,
        run=run,
        task=task_module.ShapingTask(params),
        curriculum=curriculum,
        seed=1,
        iti=params.iti,
        simulated_frame_period_s=0.0,
        date_yyyymmdd="20260826",
    )
    # Improving over eight trials rather than twenty, matching the shortened
    # criteria windows: the subject still has to earn each promotion.
    subject = ImprovingSubject(runner._screen, MonotonicClock())
    subject._start_error_px = runner._screen.deg2px(5.0)
    runner._tracker = subject
    runner._engine._input_provider = make_gaze_input_provider(subject, runner._screen)
    runner._engine._health_checks = ()
    runner._bus.subscribe(subject.on_event)
    runner.run()


def trials_of(run_dir: Path) -> list[dict[str, str]]:
    with next(run_dir.glob("*_trials.csv")).open() as f:
        return list(csv.DictReader(f))


class TestShapingAcrossSessions:
    def test_the_subject_walks_up_the_curriculum(self, tmp_path):
        run_session(tmp_path, trials=60, run=1)
        rows = trials_of(next(tmp_path.glob("sub-m01/ses-001/run-01*")))
        stages = [row["stage"] for row in rows]
        # It starts where the curriculum starts, and reaches the real task.
        assert stages[0] == "any-look"
        assert stages[-1] == "real-task"
        # Every stage was actually run at, in order — no stage skipped.
        seen: list[str] = []
        for stage in stages:
            if not seen or seen[-1] != stage:
                seen.append(stage)
        assert seen == ["any-look", "tighten", "real-task"]

    def test_every_row_says_how_hard_the_task_was(self, tmp_path):
        # The claim that makes training data analysable stand-alone.
        run_session(tmp_path, trials=60, run=1)
        rows = trials_of(next(tmp_path.glob("sub-m01/ses-001/run-01*")))
        assert all(row["stage"] for row in rows)
        assert all(row["reward_scale"] for row in rows)
        ramped = [row["ramp_fix_window_dva"] for row in rows if row["ramp_fix_window_dva"]]
        # The ramping stage's window shrinks as the subject works.
        assert ramped == sorted(ramped, reverse=True)
        assert float(ramped[0]) > float(ramped[-1])

    def test_a_second_session_carries_on_where_the_first_stopped(self, tmp_path):
        run_session(tmp_path, trials=60, run=1)
        first = yaml.safe_load(TrainingState.path_for(tmp_path, "m01").read_text())

        run_session(tmp_path, trials=10, run=2)
        second_rows = trials_of(next(tmp_path.glob("sub-m01/ses-001/run-02*")))
        # The new session opens at the stage the old one ended on, rather
        # than starting the subject over.
        assert second_rows[0]["stage"] == first["stage"]

        after = yaml.safe_load(TrainingState.path_for(tmp_path, "m01").read_text())
        assert (
            after["completed_by_stage"][first["stage"]]
            > (first["completed_by_stage"][first["stage"]])
        )
        # And the transition history is appended to, not replaced.
        assert len(after["history"]) >= len(first["history"])

    def test_transitions_are_in_the_event_stream(self, tmp_path):
        run_session(tmp_path, trials=60, run=1)
        events_path = next((tmp_path / "sub-m01" / "ses-001").glob("run-01*/*_events.csv"))
        with events_path.open() as f:
            names = [row["event"] for row in csv.DictReader(f)]
        assert names.count("STAGE_CHANGED") == 2  # two promotions

    def test_the_shipped_curriculum_is_the_protocol_it_documents(self):
        """The example's own curriculum.yaml, read rather than run.

        The tests above use a shortened copy so they finish in seconds; this
        one checks the file an experimenter would actually edit."""
        curriculum = load_model(EXAMPLE_DIR / "curriculum.yaml", Curriculum)
        assert [stage.name for stage in curriculum.stages] == [
            "any-look",
            "tighten",
            "real-task",
        ]
        # Stage 1 pays double and asks only for engagement.
        assert curriculum.stages[0].reward_scale == 2.0
        assert curriculum.stages[0].criteria.promote_when == {"completed_rate": 0.8}
        # Stage 2 shrinks the window as the subject works, and can send it
        # back if it falls apart.
        (ramp,) = curriculum.stages[1].ramps
        assert (ramp.param, ramp.start, ramp.end) == ("fix_window_dva", 8.0, 3.0)
        assert curriculum.stages[1].criteria.demote_when == {"completed_rate": 0.3}
        # Stage 3 is the task's own parameters, untouched.
        assert curriculum.stages[2].overrides == {}

    def test_a_stage_the_task_cannot_express_fails_at_build(self, tmp_path):
        from alhazen.errors import ConfigError
        from alhazen.training import Stage

        task_module = load_example_task(EXAMPLE_DIR)
        rig = load_rig(EXAMPLE_DIR / "rig-sim.yaml").model_copy(update={"data_root": tmp_path})
        params = load_model(EXAMPLE_DIR / "task.yaml", task_module.ShapingParams)
        broken = Curriculum(stages=[Stage(name="typo", overrides={"fix_window_deg": 5.0})])
        with pytest.raises(ConfigError, match="typo.*fix_window_deg"):
            build_session(
                rig=rig,
                subject="m01",
                session=1,
                run=1,
                task=task_module.ShapingTask(params),
                curriculum=broken,
                seed=1,
                date_yyyymmdd="20260826",
            )
