"""The duration estimate a launch shows before it starts.

alhazen.task.duration (the task's vocabulary and the schedule drain),
alhazen.modes.estimate (what is counted, and what is said instead of
counted), alhazen.cli.duration (run.py --estimate-duration answers from the
same command line a launch uses, writing nothing) and
alhazen.cli.workspace_estimate (the workspace asks and caches).
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pytest

from alhazen import Condition, Duration, Model, Task, outcomes
from alhazen.cli.modes import run_experiment
from alhazen.config.loader import load_rig
from alhazen.core.events import EventSchema
from alhazen.modes import Mode
from alhazen.modes import estimate as est
from alhazen.paradigms.base import SimpleSequence
from alhazen.paradigms.blocks import BlockPlan
from alhazen.paradigms.config import BlockConfig, QuestConfig, SchedulerConfig, StaircaseConfig
from alhazen.paradigms.constant import ConstantStimuli
from alhazen.task import duration as timing

RIG = Path(__file__).parents[2] / "examples/minimal_fixation/rig-sim.yaml"


# ----------------------------------------------------------------------
# Tasks the estimate is asked about. build_trial raises: an estimate that
# built a trial (stimuli, a window) would fail these tests.
# ----------------------------------------------------------------------


class TimedParams(Model):
    hold: Duration = Duration(ms=500)
    jitter: Duration = Duration(ms=100)
    flash: Duration = Duration(frames=6)
    response_timeout: Duration = Duration(ms=800)
    iti: Duration = Duration(ms=250)
    levels: tuple[str, ...] = ("a", "b")
    paradigm: SchedulerConfig = SchedulerConfig(
        kind="constant", n_per_condition=3, blocks=BlockConfig(n_blocks=2)
    )


class TimedTask(Task):
    name = "timed"
    events = EventSchema(("GO",))
    outcomes = outcomes(DONE=dict(completed=True, success=True))
    params_model = TimedParams
    timed_conditions: ClassVar[list[dict[str, Any]]] = []

    def conditions(self, rng: np.random.Generator) -> list[Condition]:
        return [
            Condition({"level": level, "side": side})
            for level in self.params.levels
            for side in (-1, 1)
        ]

    def build_trial(self, setup: Any) -> Any:
        raise AssertionError("the estimate must not build a trial")

    def trial_timing(self, condition: Condition, refresh_rate_hz: float) -> timing.TrialTiming:
        p = self.params
        type(self).timed_conditions.append(dict(condition.params))
        return timing.TrialTiming(
            [
                timing.jittered(
                    "hold", p.hold.seconds(refresh_rate_hz), p.jitter.seconds(refresh_rate_hz)
                ),
                timing.fixed("flash", p.flash.seconds(refresh_rate_hz)),
                timing.wait("response", p.response_timeout.seconds(refresh_rate_hz)),
            ]
        )


class UntimedTask(TimedTask):
    name = "untimed"

    def trial_timing(self, condition: Condition, refresh_rate_hz: float) -> None:
        return None


class OwnSourceTask(TimedTask):
    name = "own-source"
    built: ClassVar[int] = 0

    def make_source(self, params: Any, rng: np.random.Generator) -> Any:
        type(self).built += 1
        raise AssertionError("an estimate must not build a task's own scheduler")


def rig(tmp_path: Path, extra: str = "", refresh: float = 60.0) -> Path:
    text = RIG.read_text(encoding="utf-8").replace(
        "refresh_rate_hz: 60.0", f"refresh_rate_hz: {refresh}"
    )
    text = text.replace("data_root: data", f"data_root: {tmp_path / 'data'}")
    path = tmp_path / f"rig-{int(refresh)}.yaml"
    path.write_text(text + extra, encoding="utf-8")
    return path


# ----------------------------------------------------------------------
# Spans and timings
# ----------------------------------------------------------------------


class TestSpans:
    def test_helpers_and_their_bounds(self):
        assert timing.fixed("x", 0.3).expected_s == 0.3
        j = timing.jittered("x", 0.6, 0.15)
        assert (j.min_s, j.max_s, j.expected_s) == (pytest.approx(0.45), 0.75, 0.6)
        assert timing.jittered("x", 0.6, 0.0).kind == "fixed"
        w = timing.wait("x", 2.0)
        assert (w.min_s, w.max_s, w.expected_s) == (0.0, 2.0, None)
        assert timing.wait("x", 5.0, minimum_s=0.2).min_s == 0.2
        u = timing.uniform("x", 4.0, 8.0)
        assert u.expected_s == 6.0
        b = timing.bounded("x", 0.0, 0.3, "why")
        assert (b.kind, b.expected_s) == ("range", None)

    @pytest.mark.parametrize(
        "args",
        [
            ("x", "fixed", -1.0, 1.0, 0.5),
            ("x", "wait", 2.0, 1.0, None),
            ("x", "fixed", 0, 1, 2),
            ("x", "nope", 0, 1, None),
            ("x", "fixed", 0, math.inf, None),
        ],
    )
    def test_invalid_spans_are_refused(self, args):
        with pytest.raises(ValueError):
            timing.Span(*args)

    def test_timing_totals(self):
        t = timing.TrialTiming([timing.jittered("a", 1.0, 0.5), timing.wait("b", 2.0)])
        assert t.min_s == 0.5
        assert t.max_s == 3.5
        assert t.expected_s is None
        assert t.fixed_s == 1.0
        assert t.open_s == 2.0
        unbounded = timing.TrialTiming([timing.bounded("c", 1.0, None, "no cap")])
        assert unbounded.max_s is None and unbounded.open_s is None


# ----------------------------------------------------------------------
# Schedules: the session's own scheduler, drained
# ----------------------------------------------------------------------


class TestDrain:
    def test_block_restricted_factors_are_counted_as_served(self):
        """One level per block, as amodal-averaging serves its motions: not
        the full factorial repeated in every block."""
        rng = np.random.default_rng(1)
        sources = [
            ConstantStimuli({"motion": [m], "side": [-1, 1]}, n_per_condition=2, rng=rng)
            for m in ("static", "moving", "static", "moving")
        ]
        schedule = timing.drain(BlockPlan(sources, rng=rng, breaks=True))
        assert schedule.n_trials == 16
        assert [len(b) for b in schedule.blocks] == [4, 4, 4, 4]
        assert schedule.breaks == 3
        assert schedule.n_cells == 4
        assert {c.params["motion"] for c in schedule.blocks[0]} == {"static"}

    def test_blocks_without_breaks_are_still_blocks(self):
        rng = np.random.default_rng(2)
        sources = [SimpleSequence([Condition({"x": 1})], n_repeats=3, rng=rng) for _ in range(2)]
        schedule = timing.drain(BlockPlan(sources, rng=rng, breaks=False))
        assert (len(schedule.blocks), schedule.breaks, schedule.n_trials) == (2, 0, 6)

    def test_a_validation_after_each_break_is_carried(self):
        rng = np.random.default_rng(3)
        sources = [SimpleSequence([Condition({})], rng=rng) for _ in range(2)]
        plan = BlockPlan(sources, rng=rng, breaks=True, validate_after_break=True)
        assert timing.drain(plan).validate_after_break is True

    def test_only_fixed_plans_are_drained(self):
        class Adaptive:
            def next(self):  # pragma: no cover - never asked
                raise AssertionError

        with pytest.raises(TypeError):
            timing.drain(Adaptive())

    def test_the_default_schedule_is_the_tasks_own_and_uses_only_the_given_rng(self):
        task = TimedTask(TimedParams())
        state = np.random.get_state()[1].copy()
        schedule = task.duration_schedule(task.params, timing.scratch_rng())
        assert isinstance(schedule, timing.PlannedSchedule)
        # 2 levels x 2 sides x 3 per condition, in each of 2 blocks.
        assert schedule.n_trials == 24
        assert (len(schedule.blocks), schedule.breaks) == (2, 1)
        assert (np.random.get_state()[1] == state).all()

    def test_a_task_with_its_own_scheduler_is_not_built(self):
        OwnSourceTask.built = 0
        task = OwnSourceTask(TimedParams())
        schedule = task.duration_schedule(task.params, timing.scratch_rng())
        assert isinstance(schedule, timing.UnknownSchedule)
        assert "make_source" in schedule.reason
        assert OwnSourceTask.built == 0


class TestAdaptiveBounds:
    conditions: ClassVar[list[Condition]] = [Condition({"side": s}) for s in (-1, 1)]

    def cfg(self, **stair):
        return SchedulerConfig(
            kind="staircase", staircase=StaircaseConfig(parameter="c", start=1, step=0.1, **stair)
        )

    def test_trials_only_is_exact(self):
        b = timing.adaptive_bounds(self.cfg(n_trials=40), self.conditions)
        assert (b.min_trials, b.max_trials) == (40, 40)

    def test_reversals_and_trials_whichever_first(self):
        b = timing.adaptive_bounds(self.cfg(n_trials=40, n_reversals=8), self.conditions)
        assert (b.min_trials, b.max_trials) == (8, 40)
        assert "whichever comes first" in b.stopping_rule

    def test_reversals_only_has_no_upper_bound(self):
        b = timing.adaptive_bounds(self.cfg(n_reversals=8), self.conditions)
        assert (b.min_trials, b.max_trials) == (8, None)

    def test_interleaved_estimators_multiply(self):
        b = timing.adaptive_bounds(self.cfg(n_trials=30, interleave_by="side"), self.conditions)
        assert (b.min_trials, b.max_trials) == (60, 60)
        quest = SchedulerConfig(
            kind="questplus",
            quest=QuestConfig(
                parameter="c",
                intensities=[0.1, 0.2],
                thresholds=[0.1, 0.2],
                n_trials=25,
                interleave_by="side",
            ),
        )
        q = timing.adaptive_bounds(quest, self.conditions)
        assert (q.min_trials, q.max_trials) == (50, 50)

    def test_breaks_follow_the_blocks_the_trials_fill(self):
        cfg = SchedulerConfig(
            kind="staircase",
            staircase=StaircaseConfig(parameter="c", start=1, step=0.1, n_trials=40, n_reversals=8),
            blocks=BlockConfig(n_blocks=4, trials_per_block=10),
        )
        b = timing.adaptive_bounds(cfg, self.conditions)
        assert (b.min_breaks, b.max_breaks) == (0, 3)


# ----------------------------------------------------------------------
# The estimate
# ----------------------------------------------------------------------


class TestEstimateTrials:
    def test_counts_spans_iti_and_the_refresh_it_was_planned_on(self, tmp_path):
        task = TimedTask(TimedParams())
        answer = est.estimate_trials(
            Mode.RUN, task, task.params, load_rig(rig(tmp_path)), rig_name="sim"
        )
        assert answer["status"] == "ok"
        assert answer["counts"]["trials"] == 24
        assert answer["counts"]["breaks"] == 1
        # Per trial: hold 0.5 + flash 6 frames at 60 Hz (0.1) + ITI 0.25, and
        # a response wait of 0 to 0.8 s.
        assert answer["seconds"]["low"] == pytest.approx(24 * 0.85)
        assert answer["seconds"]["high"] == pytest.approx(24 * (0.85 + 0.8))
        assert answer["seconds"]["expected"] is None
        assert answer["seconds"]["bound_min"] == pytest.approx(24 * 0.75)
        assert (
            "60 Hz" in answer["basis"]["refresh"] and "not measured" in answer["basis"]["refresh"]
        )
        assert any("1 rest break" in m for m in answer["manual"])
        assert answer["plus"] == "plus 1 manual break"
        assert any("re-served" in e for e in answer["excluded"])

    def test_frames_follow_the_rigs_refresh_rate(self, tmp_path):
        task = TimedTask(TimedParams())
        at_60 = est.estimate_trials(
            Mode.RUN, task, task.params, load_rig(rig(tmp_path, refresh=60.0))
        )
        at_120 = est.estimate_trials(
            Mode.RUN, task, task.params, load_rig(rig(tmp_path, refresh=120.0))
        )
        # 6 frames: 0.1 s at 60 Hz, 0.05 s at 120 Hz; nothing else changes.
        assert at_60["seconds"]["low"] - at_120["seconds"]["low"] == pytest.approx(24 * 0.05)

    def test_each_distinct_condition_is_timed_once_with_what_build_trial_sees(self, tmp_path):
        TimedTask.timed_conditions = []
        task = TimedTask(TimedParams())
        est.estimate_trials(Mode.RUN, task, task.params, load_rig(rig(tmp_path)))
        # Four cells in each of two blocks, each carrying its block number.
        assert len(TimedTask.timed_conditions) == 8
        assert {c["block"] for c in TimedTask.timed_conditions} == {1, 2}

    def test_test_mode_counts_the_reduced_session(self, tmp_path):
        task = TimedTask(TimedParams())
        answer = est.estimate_trials(
            Mode.TEST, task, task.params, load_rig(rig(tmp_path)), trials_per_condition=1
        )
        assert answer["counts"]["trials"] == 8
        assert answer["basis"]["reductions"] == ["paradigm.n_per_condition: 3 -> 1"]

    def test_simulate_includes_its_unattended_rests_and_says_whose_time_it_is(self, tmp_path):
        task = TimedTask(TimedParams())
        answer = est.estimate_trials(
            Mode.SIMULATE, task, task.params, load_rig(rig(tmp_path)), headless=True
        )
        assert answer["seconds"]["low"] == pytest.approx(8 * 0.85 + 10.0)
        assert answer["manual"] == []
        assert any("Simulated session time" in a for a in answer["assumptions"])

    def test_a_real_tracker_puts_calibration_beside_the_number(self, tmp_path):
        loaded = load_rig(rig(tmp_path, "\ndevices:\n  eyetracker:\n    backend: eyelink\n"))
        # A rig with a window: test mode's mouse needs one to move over.
        tracked = loaded.model_copy(
            update={"display": loaded.display.model_copy(update={"backend": "psychopy"})}
        )
        task = TimedTask(TimedParams())
        answer = est.estimate_trials(Mode.RUN, task, task.params, tracked)
        assert answer["plus"] == "plus calibration and 1 manual break"
        mouse = est.estimate_trials(Mode.TEST, task, task.params, tracked, mouse=True)
        assert "calibration" not in mouse["plus"]

    def test_a_task_that_does_not_time_its_trials_is_counted_not_timed(self, tmp_path):
        task = UntimedTask(TimedParams())
        answer = est.estimate_trials(Mode.RUN, task, task.params, load_rig(rig(tmp_path)))
        assert answer["status"] == "partial"
        assert answer["counts"]["trials"] == 24
        assert "seconds" not in answer
        assert "trial_timing" in answer["reason"]

    def test_an_unknown_schedule_is_said_not_guessed(self, tmp_path):
        task = OwnSourceTask(TimedParams())
        answer = est.estimate_trials(Mode.RUN, task, task.params, load_rig(rig(tmp_path)))
        assert (answer["status"], answer["headline"]) == ("unknown", "No reliable estimate")

    def test_adaptive_plans_give_a_range_from_the_stopping_rule(self, tmp_path):
        params = TimedParams(
            paradigm=SchedulerConfig(
                kind="staircase",
                staircase=StaircaseConfig(
                    parameter="c", start=1, step=0.1, n_trials=40, n_reversals=8
                ),
            )
        )
        task = TimedTask(params)
        answer = est.estimate_trials(Mode.RUN, task, params, load_rig(rig(tmp_path)))
        assert (answer["counts"]["trials"], answer["counts"]["trials_max"]) == (8, 40)
        assert answer["seconds"]["low"] == pytest.approx(8 * 0.85)
        assert answer["seconds"]["high"] == pytest.approx(40 * 1.65)
        assert any("Stopping rule" in a for a in answer["assumptions"])


class TestWords:
    @pytest.mark.parametrize(
        ("low", "high", "expected", "text"),
        [
            (2044.8, 3484.8, None, "34–59 min"),
            (62, 62, 62, "≈ 62 s"),
            (100, None, None, "at least 2 min"),
            (18, 120, None, "18 s – 2 min"),
            (5400, 7300, None, "1 h 30 min – 2 h 02 min"),
        ],
    )
    def test_range_text(self, low, high, expected, text):
        assert est.range_text(low, high, expected) == text


class TestOtherModes:
    def test_demo_is_open_ended_and_movie_is_not_guessed(self):
        assert est.open_ended(Mode.DEMO)["headline"] == "Open-ended, until stopped"
        assert est.movie()["status"] == "unknown"

    def test_measure_times_the_sampling_and_names_the_operators_parts(self, tmp_path):
        from alhazen.modes.measure_jobs import installed_jobs

        jobs = installed_jobs()
        answer = est.estimate_measure(
            load_rig(rig(tmp_path, refresh=120.0)), jobs, ["monitor.refresh"], {}
        )
        # The simulated display has no panel to measure: said, at 0 s.
        assert answer["jobs"][0]["note"].startswith("unavailable")
        tracked = load_rig(rig(tmp_path, "\ndevices:\n  eyetracker:\n    backend: eyelink\n"))
        real = tracked.model_copy(
            update={"display": tracked.display.model_copy(update={"backend": "psychopy"})}
        )
        answer = est.estimate_measure(
            real,
            jobs,
            ["monitor.refresh", "tracker.calibration", "tracker.accuracy"],
            {"monitor.refresh.flips": "600"},
        )
        refresh, calibration, accuracy = answer["jobs"]
        assert refresh["timed_s"] == pytest.approx(600 / 60.0)
        assert calibration["timed_s"] == 0.0 and "calibration" in calibration["operator"]
        assert accuracy["timed_s"] == pytest.approx(5 * 0.5)
        assert answer["headline"] == "≈ 13 s of timed sampling"  # 12.5 s
        assert answer["plus"] == "plus operator-guided steps"
        assert answer["seconds"]["high"] is None

    def test_measure_without_a_selection_is_not_timed(self, tmp_path):
        answer = est.estimate_measure(load_rig(rig(tmp_path)), {}, [], {})
        assert answer["status"] == "unknown"


# ----------------------------------------------------------------------
# run.py --estimate-duration
# ----------------------------------------------------------------------


def ask(capsys, tmp_path, *argv: str, task_class: type = TimedTask) -> tuple[int, dict]:
    path = tmp_path / "rig-60.yaml"
    default = path if path.exists() else rig(tmp_path)
    code = run_experiment(
        task_class=task_class, default_rig=default, argv=[*argv, "--estimate-duration"]
    )
    out = capsys.readouterr().out.strip().splitlines()
    return code, json.loads(out[-1])


class TestCommandLine:
    def test_the_same_argv_answers_with_json_and_writes_nothing(
        self, capsys, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        params = tmp_path / "params.yaml"
        params.write_text("paradigm: {kind: constant, n_per_condition: 1, blocks: {n_blocks: 3}}\n")
        rig(tmp_path)
        before = sorted(p.name for p in tmp_path.iterdir())
        code, answer = ask(
            capsys, tmp_path, "--mode", "run", "--task", "timed", "--params", str(params)
        )
        assert code == 0
        assert answer["counts"]["trials"] == 12
        assert answer["counts"]["breaks"] == 2
        assert answer["basis"]["params"] == str(params)
        # No run folder, no data root, nothing asked for (no --sub given).
        assert sorted(p.name for p in tmp_path.iterdir()) == before
        assert not (tmp_path / "data").exists()

    def test_modes(self, capsys, tmp_path):
        assert (
            ask(capsys, tmp_path, "--mode", "demo", "--task", "timed")[1]["status"] == "open-ended"
        )
        assert ask(capsys, tmp_path, "--mode", "movie", "--task", "timed")[1]["status"] == "unknown"
        code, sim = ask(
            capsys, tmp_path, "--mode", "simulate", "--task", "timed", "--trials-per-condition", "2"
        )
        assert sim["counts"]["trials"] == 16

    def test_a_launch_the_run_would_refuse_is_refused_in_its_words(self, capsys, tmp_path):
        code, answer = ask(capsys, tmp_path, "--mode", "test", "--task", "timed", "--headless")
        assert (code, answer["status"]) == (0, "refused")
        assert "headless" in answer["reason"]

    def test_invalid_params_are_an_error_with_the_models_words(self, capsys, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("paradigm: {n_per_condition: 0}\n")
        code, answer = ask(
            capsys, tmp_path, "--mode", "run", "--task", "timed", "--params", str(bad)
        )
        assert (code, answer["status"]) == (1, "error")
        assert "n_per_condition" in answer["reason"]

    def test_the_capability_is_advertised(self):
        from alhazen.cli.capabilities import CAPABILITIES

        assert "duration-estimate" in CAPABILITIES


# ----------------------------------------------------------------------
# The workspace: asked of the project's interpreter, cached by its inputs
# ----------------------------------------------------------------------


@pytest.fixture
def estimator(tmp_path, monkeypatch):
    from alhazen.cli import workspace as workspace_module
    from alhazen.cli.workspace import Workspace
    from alhazen.cli.workspace_estimate import DurationEstimator

    capabilities = {"value": ["duration-estimate", "experimenter"]}
    monkeypatch.setattr(
        workspace_module,
        "probe_interpreter",
        lambda python, path: {
            "alhazen_version": "2.11.0",
            "python_version": "stub",
            "shared_rigs": [],
            "capabilities": capabilities["value"],
        },
    )
    root = tmp_path / "experiment"
    (root / "configs").mkdir(parents=True)
    (root / "configs/rig-sim.yaml").write_bytes(RIG.read_bytes())
    # A run.py the workspace reads without running (project_tasks): one
    # task in a table, so no schema read starts a child here.
    (root / "run.py").write_text(
        "from alhazen.cli.modes import run_experiment\n"
        "TASKS = {'timed': (object, None)}\n"
        "run_experiment(tasks=TASKS, default_rig='configs/rig-sim.yaml')\n"
    )
    space = Workspace(tmp_path / "state")
    space.add(str(root), sys.executable)
    asked: list[list[str]] = []
    estimator = DurationEstimator(space)

    def fake_ask(project, command, text, form="the parameters on the form"):
        asked.append([*command, text or ""])
        return {"schema": 1, "status": "ok", "headline": f"{len(asked)} min"}

    monkeypatch.setattr(estimator, "_ask", fake_ask)
    yield estimator, space, asked, capabilities, root
    space.close()


def request(space, **kw):
    from alhazen.cli.workspace_estimate import EstimateRequest

    kw.setdefault("task", "timed")
    return EstimateRequest(project=space.projects[0]["id"], rig="configs/rig-sim.yaml", **kw)


class TestWorkspaceEstimates:
    def test_answers_are_cached_by_what_decides_them(self, estimator):
        est_, space, asked, _, root = estimator
        first = est_.estimate(request(space, mode="run", parameters_yaml="a: 1\n"))
        again = est_.estimate(request(space, mode="run", parameters_yaml="a: 1\n"))
        assert (first["cached"], again["cached"], len(asked)) == (False, True, 1)
        est_.estimate(request(space, mode="run", parameters_yaml="a: 2\n"))
        est_.estimate(request(space, mode="test", parameters_yaml="a: 1\n", trials=2))
        assert len(asked) == 3
        assert "--trials-per-condition" in asked[-1] and "2" in asked[-1]
        # An edit to the project's source is a new answer.
        (root / "run.py").write_text((root / "run.py").read_text() + "# edited\n")
        est_.estimate(request(space, mode="run", parameters_yaml="a: 1\n"))
        assert len(asked) == 4

    def test_an_alhazen_that_cannot_estimate_is_unavailable_not_asked(self, estimator, tmp_path):
        est_, space, asked, capabilities, root = estimator
        # A registration made under an older alhazen, and one made before
        # the probe asked what an alhazen can do.
        for recorded, words in ((["experimenter"], "cannot estimate"), (None, "registered before")):
            space.projects[0]["capabilities"] = recorded
            answer = est_.estimate(request(space, mode="run"))
            assert answer["status"] == "unavailable"
            assert answer["headline"] == "Estimate unavailable for this environment"
            assert words in answer["reason"]
        assert asked == []

    def test_refusals_come_before_a_child(self, estimator):
        est_, space, asked, *_ = estimator
        assert est_.estimate(request(space, mode="test", headless=True))["status"] == "refused"
        with pytest.raises(ValueError, match="set from the dashboard"):
            est_.estimate(request(space, mode="run", extra_args="--seed 5"))
        with pytest.raises(ValueError, match="set from the dashboard"):
            est_.estimate(request(space, mode="run", extra_args="--estimate-duration"))
        with pytest.raises(ValueError, match="Measure rig does not use task parameters"):
            est_.estimate(request(space, mode="measure", parameters_yaml="a: 1"))
        assert asked == []

    def test_the_command_is_the_launchs_with_the_flag_last(self, estimator):
        est_, space, asked, *_ = estimator
        est_.estimate(
            request(
                space, mode="simulate", headless=True, trials=3, extra_args="--curriculum c.yaml"
            )
        )
        command = asked[0]
        assert command[0].endswith("run.py")
        assert command[1:3] == ["--mode", "simulate"]
        assert "--headless" in command and "--curriculum" in command


class TestRealChild:
    """One real child: run.py in this interpreter, the edited params in a
    temporary file that the answer never names and that is gone afterwards."""

    def test_end_to_end(self, tmp_path, monkeypatch):
        from alhazen.cli import workspace as workspace_module
        from alhazen.cli.workspace import Workspace
        from alhazen.cli.workspace_estimate import DurationEstimator, EstimateRequest

        monkeypatch.setattr(
            workspace_module,
            "probe_interpreter",
            lambda python, path: {
                "alhazen_version": "2.11.0",
                "python_version": "stub",
                "shared_rigs": [],
                "capabilities": ["duration-estimate"],
            },
        )
        root = tmp_path / "exp"
        (root / "configs").mkdir(parents=True)
        (root / "configs/rig-sim.yaml").write_text(rig(tmp_path).read_text())
        (root / "timed_task.py").write_text(
            "from tests.unit.test_duration_estimate import TimedTask\n"
        )
        (root / "run.py").write_text(
            f"import sys\nsys.path.insert(0, {str(Path(__file__).parents[2])!r})\n"
            + "from alhazen.cli.modes import run_experiment\n"
            "from timed_task import TimedTask\n"
            "TASKS = {'timed': (TimedTask, None)}\n"
            "if __name__ == '__main__':\n"
            "    rig = 'configs/rig-sim.yaml'\n"
            "    raise SystemExit(run_experiment(tasks=TASKS, default_rig=rig))\n"
        )
        space = Workspace(tmp_path / "state")
        space.add(str(root), sys.executable)
        estimator = DurationEstimator(space)
        made: list[str] = []
        import tempfile

        original = tempfile.TemporaryDirectory

        def watched(*a, **k):
            scratch = original(*a, **k)
            made.append(scratch.name)
            return scratch

        monkeypatch.setattr(tempfile, "TemporaryDirectory", watched)
        answer = estimator.estimate(
            EstimateRequest(
                project=space.projects[0]["id"],
                mode="run",
                task="timed",
                rig="configs/rig-sim.yaml",
                parameters_yaml="paradigm: {kind: constant, n_per_condition: 2}\n",
            )
        )
        space.close()
        assert answer["status"] == "ok", answer
        assert answer["counts"]["trials"] == 8
        assert answer["basis"]["params"] == "the parameters on the form"
        assert made and not any(Path(m).exists() for m in made)
        assert not (root / "data").exists() and not (tmp_path / "data").exists()
        assert not list((tmp_path / "state" / "runs").iterdir())
