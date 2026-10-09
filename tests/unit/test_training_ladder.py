"""Training mode: a stage of a training ladder, run for a monkey, paid on the
stage's success alone, filed apart from the experiment (training/ladder.py,
training/history.py, modes/session.py).

The promises tested here, against ladders written to disk and sessions
actually built and run:

- a ladder is data, refused at load when it cannot mean one thing (ids,
  order, a stage without a task, a criterion that judges nothing);
- a stage resolves to the task's own params with its overrides, re-validated
  through the task's model, and a reward policy that pays the stage's success
  and nothing else (plus the device-fault reward, kept or replaced);
- training mode refuses a human, a stage it cannot find, a development rig,
  and a session without a stage; run mode refuses a stage;
- a training session pays juice on exactly the trials that ended in the
  stage's success — through the fake NI-DAQ driver, the real NidaqReward
  code path — records the stage in session.json and on every row, and its
  data land under ``<data_root>-training/<ladder>/<stage>/`` and nowhere in
  the experiment's data root; a rehearsal of a stage goes to the training
  root's rehearsal sibling, reduced;
- the history reads success rates per stage back from those folders, and a
  stage's criterion only ever comes out as a recommendation.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from tests.unit.test_subject_kind import FakeDaq, daq  # noqa: F401  (the fake NI-DAQ fixture)

from alhazen import (
    Condition,
    RigConfig,
    SubjectParams,
    Task,
    TrialPlan,
    TrialSetup,
    outcomes,
)
from alhazen.config.models import DevicesConfig, DisplayConfig, RewardHwConfig
from alhazen.core.events import EventSchema
from alhazen.errors import ConfigError
from alhazen.modes import Mode, real_data_refusal
from alhazen.modes.session import build_mode_session
from alhazen.modes.simulation import Simulation
from alhazen.paradigms.base import SimpleSequence
from alhazen.training.history import ladder_history, recommendation, stage_tally
from alhazen.training.ladder import find_ladder, load_ladder, resolve_stage, training_root
from alhazen.training.stages import StageCriteria
from support import MONITOR, RunForFrames

OUTCOMES = outcomes(
    HELD=dict(completed=True, success=True),
    LANDED=dict(completed=True, success=True),
    MISS=dict(completed=True, success=False),
    FIX_BREAK=dict(completed=False),
)
JUICE = {"n_pulses": 2, "pulse_ms": 200, "inter_pulse_ms": 200}


class Params(SubjectParams):
    # The outcome each trial ends with, in order: what a stage changes.
    results: tuple[str, ...] = ("HELD", "MISS", "HELD")
    gap_dva: float = 1.0


class LadderTask(Task):
    name = "ladder-task"
    events = EventSchema(())
    outcomes = OUTCOMES
    params_model = Params

    def make_source(self, params, rng):
        conditions = [Condition({"i": i, "result": r}) for i, r in enumerate(params.results)]
        return SimpleSequence(conditions, n_repeats=1, rng=rng, shuffle=False)

    def build_trial(self, setup: TrialSetup) -> TrialPlan:
        return TrialPlan(phases=[RunForFrames(1, self.outcomes[setup.condition.params["result"]])])

    def simulation(self, seed: int) -> Simulation:
        # Trials end by themselves; the stand-in is the task as it is.
        return Simulation(task=type(self)(self.params), describe={"seed": seed})


class OtherTask(LadderTask):
    name = "other-task"


TASKS = {"ladder-task": (LadderTask, None), "other-task": (OtherTask, None)}
STAGE2_PLAN = ["LANDED", "MISS", "LANDED", "HELD", "MISS", "LANDED"]


def write_params(folder: Path, name: str, **values: Any) -> Path:
    data = {"subject_kind": "monkey", "reward": {"by_outcome": {"HELD": JUICE}}, **values}
    path = folder / name
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def write_ladder(folder: Path, **change: Any) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    write_params(
        folder,
        "task-monkey.yaml",
        reward={
            "by_outcome": {"HELD": JUICE},
            "on_fault": {"n_pulses": 1, "pulse_ms": 200, "inter_pulse_ms": 200},
        },
    )
    (folder / "task-human.yaml").write_text("subject_kind: human\n", encoding="utf-8")
    ladder: dict[str, Any] = {
        "name": "shaping",
        "title": "Shaping",
        "task": "ladder-task",
        "params": "task-monkey.yaml",
        "stages": [
            {"id": "hold", "title": "Hold fixation", "success": "HELD"},
            {
                "id": "saccade",
                "title": "Saccade to the dot",
                "success": "LANDED",
                "overrides": {"results": STAGE2_PLAN, "gap_dva": 2.5},
                "reward": {"success": {"n_pulses": 1, "pulse_ms": 150, "inter_pulse_ms": 200}},
                "criterion": {"window": 4, "min_trials": 4, "promote_when": {"success_rate": 0.5}},
            },
            {"id": "final", "title": "The real trial", "task": "other-task", "success": "HELD"},
        ],
    }
    ladder.update(change)
    path = folder / "training-shaping.yaml"
    path.write_text(yaml.safe_dump(ladder, sort_keys=False), encoding="utf-8")
    return path


LAB_LINE = RewardHwConfig(backend="nidaq", device="Dev1", channel="ao0", voltage=5.0)


def lab_rig(tmp_path: Path, *, real_data: bool = True) -> RigConfig:
    return RigConfig(
        monitor=MONITOR,
        display=DisplayConfig(backend="simulated"),
        devices=DevicesConfig(reward=LAB_LINE),
        data_root=tmp_path / "data",
        real_data=real_data,
    )


def run_stage(tmp_path: Path, stage: str, mode: Mode = Mode.TRAINING, **kw: Any):
    resolved = resolve_stage(write_ladder(tmp_path / "configs"), stage, TASKS)
    built = build_mode_session(
        mode,
        rig=lab_rig(tmp_path),
        task=resolved.task_class(resolved.params),
        subject="m01",
        session=kw.pop("session", 1),
        seed=1,
        experiment_version="1.0.0",
        experiment_name="ladder-experiment",
        training=resolved,
        live_monitor=False,
        **kw,
    )
    built.runner.run()
    return built


def rows_of(folder: Path) -> list[dict[str, str]]:
    with next(folder.rglob("*_trials.csv")).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


# ----------------------------------------------------------------------
# The ladder file
# ----------------------------------------------------------------------


class TestLadderFile:
    def test_a_ladder_loads_in_order(self, tmp_path):
        ladder = load_ladder(write_ladder(tmp_path))
        assert [s.id for s in ladder.stages] == ["hold", "saccade", "final"]
        assert ladder.index_of("final") == 2

    @pytest.mark.parametrize(
        ("change", "words"),
        [
            ({"name": "Has Spaces"}, "ladder name"),
            ({"stages": []}, "no stages"),
            (
                {
                    "stages": [
                        {"id": "a", "title": "A", "success": "HELD"},
                        {"id": "a", "title": "B", "success": "HELD"},
                    ]
                },
                "repeats stage id",
            ),
            ({"task": None}, "name no task"),
            (
                {
                    "stages": [
                        {
                            "id": "a",
                            "title": "A",
                            "success": "HELD",
                            "criterion": {"window": 5, "min_trials": 5},
                        }
                    ]
                },
                "judges",
            ),
            ({"stages": [{"id": "a/b", "title": "A", "success": "HELD"}]}, "stage id"),
        ],
    )
    def test_a_ladder_that_cannot_mean_one_thing_is_refused(self, tmp_path, change, words):
        with pytest.raises(ConfigError, match=words):
            load_ladder(write_ladder(tmp_path, **change))

    def test_an_unknown_stage_lists_the_stages(self, tmp_path):
        ladder = load_ladder(write_ladder(tmp_path))
        with pytest.raises(ConfigError, match="hold, saccade, final"):
            ladder.stage("pursue")

    def test_find_ladder_by_label_name_path_or_the_only_one(self, tmp_path):
        path = write_ladder(tmp_path)
        registered = {"Shaping (monkey)": path}
        assert find_ladder("", registered) == ("Shaping (monkey)", path)
        assert find_ladder("Shaping (monkey)", registered) == ("Shaping (monkey)", path)
        assert find_ladder("shaping", registered) == ("Shaping (monkey)", path)
        assert find_ladder(str(path), {}) == (None, path)
        with pytest.raises(ConfigError, match="registers none"):
            find_ladder("", {})
        with pytest.raises(ConfigError, match="several"):
            find_ladder("", {"a": path, "b": path})
        with pytest.raises(ConfigError, match="not a registered ladder"):
            find_ladder("nope", registered)


# ----------------------------------------------------------------------
# Resolving a stage
# ----------------------------------------------------------------------


class TestResolve:
    def test_overrides_reach_the_params_through_the_task_model(self, tmp_path):
        resolved = resolve_stage(write_ladder(tmp_path), "saccade", TASKS)
        assert resolved.params.results == tuple(STAGE2_PLAN)
        assert resolved.params.gap_dva == 2.5
        assert resolved.task_name == "ladder-task"
        assert resolved.params_file == (tmp_path / "task-monkey.yaml").resolve()

    def test_the_stage_pays_its_success_and_nothing_else(self, tmp_path):
        resolved = resolve_stage(write_ladder(tmp_path), "saccade", TASKS)
        policy = resolved.params.reward
        # The params file paid HELD; this stage pays LANDED alone, at its
        # own delivery, and keeps the file's device-fault reward.
        assert set(policy.by_outcome) == {"LANDED"}
        assert policy.by_outcome["LANDED"].n_pulses == 1
        assert policy.by_outcome["LANDED"].pulse_ms == 150
        assert policy.on_fault is not None and policy.on_fault.n_pulses == 1
        task = resolved.task_class(resolved.params)
        assert task.reward.pulses_for("HELD") is None
        assert task.reward.pulses_for("LANDED") is not None

    def test_a_stage_without_its_own_delivery_takes_the_files(self, tmp_path):
        resolved = resolve_stage(write_ladder(tmp_path), "hold", TASKS)
        assert resolved.params.reward.by_outcome["HELD"].model_dump(exclude={"volume_ul"}) == JUICE

    def test_on_fault_can_be_replaced_or_turned_off(self, tmp_path):
        stages = [
            {
                "id": "a",
                "title": "A",
                "success": "LANDED",
                "reward": {"on_fault": None},
            },
            {
                "id": "b",
                "title": "B",
                "success": "LANDED",
                "reward": {"on_fault": {"n_pulses": 0}},
            },
        ]
        path = write_ladder(tmp_path, stages=stages)
        assert resolve_stage(path, "a", TASKS).params.reward.on_fault is None
        assert resolve_stage(path, "b", TASKS).params.reward.pulses_for_fault() is None

    def test_another_task_per_stage(self, tmp_path):
        resolved = resolve_stage(write_ladder(tmp_path), "final", TASKS)
        assert resolved.task_class is OtherTask

    def test_a_training_task_by_import_path_stays_out_of_tasks(self, tmp_path):
        spec = "tests.unit.test_training_ladder:OtherTask"
        path = write_ladder(
            tmp_path, stages=[{"id": "x", "title": "X", "success": "HELD", "task": spec}]
        )
        resolved = resolve_stage(path, "x", {})
        assert resolved.task_class.__name__ == "OtherTask"
        assert resolved.task_name == "other-task"
        bad = write_ladder(
            tmp_path / "bad",
            stages=[{"id": "x", "title": "X", "success": "HELD", "task": "json:dumps"}],
        )
        with pytest.raises(ConfigError, match="not an alhazen Task class"):
            resolve_stage(bad, "x", {})

    def test_a_human_stage_is_refused(self, tmp_path):
        path = write_ladder(
            tmp_path,
            stages=[{"id": "h", "title": "H", "success": "HELD", "params": "task-human.yaml"}],
        )
        with pytest.raises(ConfigError, match="training is for a monkey.*human"):
            resolve_stage(path, "h", TASKS)

    def test_undeclared_params_are_refused_too(self, tmp_path):
        (tmp_path / "blank.yaml").write_text("gap_dva: 1.0\n", encoding="utf-8")
        path = write_ladder(
            tmp_path, stages=[{"id": "u", "title": "U", "success": "HELD", "params": "blank.yaml"}]
        )
        with pytest.raises(ConfigError, match="declare subject_kind nothing"):
            resolve_stage(path, "u", TASKS)

    @pytest.mark.parametrize(
        ("stage", "words"),
        [
            ({"success": "PURSUED"}, "does not declare"),
            ({"success": "FIX_BREAK"}, "not a completed outcome"),
            ({"success": "HELD", "overrides": {"nope": 1}}, "stage 'x' overrides 'nope'"),
            ({"success": "HELD", "overrides": {"gap_dva": "wide"}}, "rejects"),
            ({"success": "HELD", "task": "missing-task"}, "not one of this experiment's tasks"),
            ({"success": "HELD", "params": "absent.yaml"}, "not a file"),
        ],
    )
    def test_a_stage_that_cannot_run_is_refused_naming_it(self, tmp_path, stage, words):
        path = write_ladder(tmp_path, stages=[{"id": "x", "title": "X", **stage}])
        with pytest.raises(ConfigError, match=words):
            resolve_stage(path, "x", TASKS)

    def test_the_record_says_what_ran(self, tmp_path):
        record = resolve_stage(write_ladder(tmp_path), "saccade", TASKS).record()
        assert record["ladder"] == "shaping"
        assert record["stage"] == "saccade"
        assert (record["stage_number"], record["stage_count"]) == (2, 3)
        assert record["success"] == "LANDED"
        assert set(record["reward"]["by_outcome"]) == {"LANDED"}
        assert record["overrides"]["gap_dva"] == 2.5
        assert record["criterion"]["promote_when"] == {"success_rate": 0.5}


# ----------------------------------------------------------------------
# The mode's rules
# ----------------------------------------------------------------------


class TestModeRules:
    def test_training_drives_the_rig_as_run_does_but_files_elsewhere(self):
        assert Mode.TRAINING.runs_trials
        assert Mode.TRAINING.drives_subject and Mode.RUN.drives_subject
        assert not Mode.TRAINING.writes_real_data
        assert not Mode.SIMULATE.drives_subject

    def test_a_development_rig_is_refused(self, tmp_path):
        assert real_data_refusal(Mode.TRAINING, lab_rig(tmp_path, real_data=False)) is not None
        assert real_data_refusal(Mode.TRAINING, lab_rig(tmp_path)) is None

    def test_training_without_a_stage_is_refused(self, tmp_path):
        resolved = resolve_stage(write_ladder(tmp_path / "c"), "hold", TASKS)
        with pytest.raises(ConfigError, match="--stage"):
            build_mode_session(
                Mode.TRAINING,
                rig=lab_rig(tmp_path),
                task=LadderTask(resolved.params),
                subject="m01",
                session=1,
                experiment_version="1.0.0",
            )
        assert not (tmp_path / "data").exists()

    def test_run_mode_refuses_a_stage(self, tmp_path):
        resolved = resolve_stage(write_ladder(tmp_path / "c"), "hold", TASKS)
        with pytest.raises(ConfigError, match="run mode"):
            build_mode_session(
                Mode.RUN,
                rig=lab_rig(tmp_path),
                task=LadderTask(resolved.params),
                subject="m01",
                session=1,
                experiment_version="1.0.0",
                training=resolved,
            )

    def test_training_roots_are_siblings_of_the_data_root(self, tmp_path):
        root = tmp_path / "data"
        assert training_root(root, "shaping", "hold", rehearsal=False) == (
            tmp_path / "data-training" / "shaping" / "hold"
        )
        assert training_root(root, "shaping", "hold", rehearsal=True) == (
            tmp_path / "data-training-rehearsal" / "shaping" / "hold"
        )


# ----------------------------------------------------------------------
# A training session, end to end
# ----------------------------------------------------------------------


class TestTrainingSession:
    def test_juice_goes_out_on_exactly_the_stage_success(self, tmp_path, daq):  # noqa: F811
        built = run_stage(tmp_path, "saccade")
        rows = rows_of(built.data_root)
        assert [row["outcome"] for row in rows][:3] == ["LANDED", "MISS", "LANDED"]
        landed = [row["trial_index"] for row in rows if row["outcome"] == "LANDED"]
        rewarded = [row["trial_index"] for row in rows if row["rewarded"] == "True"]
        # HELD is the params file's paying outcome and the final task's
        # success; at this stage it pays nothing.
        assert any(row["outcome"] == "HELD" for row in rows)
        assert rewarded == landed
        assert len(daq.tasks) == len(landed)
        for ao in daq.tasks:
            assert ao.channels[0]["name"] == "Dev1/ao0"
            # The stage's own delivery: one 150 ms pulse.
            assert sum(1 for v in ao.written if v > 0) == 150

    def test_data_land_under_the_stage_root_and_never_the_experiments(self, tmp_path, daq):  # noqa: F811
        built = run_stage(tmp_path, "saccade")
        assert built.data_root == tmp_path / "data-training" / "shaping" / "saccade"
        assert list(built.data_root.rglob("*_trials.csv"))
        assert not (tmp_path / "data").exists()
        assert not (tmp_path / "data-rehearsal").exists()

    def test_session_json_and_rows_say_which_stage(self, tmp_path, daq):  # noqa: F811
        built = run_stage(tmp_path, "saccade")
        card = json.loads(next(built.data_root.rglob("session.json")).read_text(encoding="utf-8"))
        assert card["mode"] == "training"
        assert card["subject_kind"] == "monkey"
        assert card["training"]["ladder"] == "shaping"
        assert card["training"]["stage"] == "saccade"
        assert card["training"]["success"] == "LANDED"
        assert card["training"]["params_file"] == "task-monkey.yaml"
        assert card["reward"]["policy"]["by_outcome"].keys() == {"LANDED"}
        rows = rows_of(built.data_root)
        assert {(r["training_ladder"], r["training_stage"]) for r in rows} == {
            ("shaping", "saccade")
        }
        log = next(built.data_root.rglob("*session.log")).read_text(encoding="utf-8")
        assert "training: ladder shaping — stage 2 of 3, saccade" in log

    def test_full_length_in_training_mode(self, tmp_path, daq):  # noqa: F811
        built = run_stage(tmp_path, "saccade")
        assert built.reductions == []
        assert len(rows_of(built.data_root)) >= len(STAGE2_PLAN)

    def test_a_rehearsal_of_a_stage_goes_to_the_rehearsal_sibling(self, tmp_path):
        built = run_stage(tmp_path, "saccade", mode=Mode.SIMULATE)
        assert built.data_root == tmp_path / "data-training-rehearsal" / "shaping" / "saccade"
        card = json.loads(next(built.data_root.rglob("session.json")).read_text(encoding="utf-8"))
        assert card["mode"] == "simulate" and card["training"]["stage"] == "saccade"
        assert not (tmp_path / "data").exists()
        assert not (tmp_path / "data-training").exists()


# ----------------------------------------------------------------------
# The history, and the criterion as a recommendation
# ----------------------------------------------------------------------


class TestHistory:
    def test_tally_counts_the_stage_success_among_finished_trials(self):
        rows = [
            {"outcome": "LANDED", "completed": "True"},
            {"outcome": "MISS", "completed": "True"},
            {"outcome": "FIX_BREAK", "completed": "False"},
            {"outcome": "PAUSED", "completed": "False"},
            {
                "outcome": "ABORTED",
                "completed": "False",
                "fault": "tracker_stopped",
                "abort_reason": "tracker_stopped",
            },
        ]
        tally = stage_tally(rows, "LANDED")
        assert tally == {
            "attempts": 3,
            "finished": 2,
            "successes": 1,
            "faults": 2,
            "success_rate": 0.5,
        }

    def test_recommendations(self):
        criterion = StageCriteria(window=4, min_trials=4, promote_when={"success_rate": 0.75})
        good = [{"completed": True, "success": True}] * 4
        mixed = [{"completed": True, "success": i % 2 == 0} for i in range(4)]
        assert recommendation(criterion, good[:2])["verdict"] == "too few trials"
        assert recommendation(criterion, good)["verdict"] == "advance"
        assert recommendation(criterion, mixed)["verdict"] == "stay"
        back = StageCriteria(
            window=4,
            min_trials=4,
            demote_when={"completed_rate": 0.5},
            promote_when={"success_rate": 0.75},
        )
        broken = [{"completed": False, "success": False}] * 4
        assert recommendation(back, broken)["verdict"] == "go back"

    def test_ladder_history_reads_the_sessions_back(self, tmp_path, daq):  # noqa: F811
        run_stage(tmp_path, "saccade", session=1)
        run_stage(tmp_path, "saccade", session=2)
        run_stage(tmp_path, "saccade", mode=Mode.SIMULATE)
        history = ladder_history(
            [tmp_path / "data"], load_ladder(tmp_path / "configs" / "training-shaping.yaml")
        )
        stages = {stage["id"]: stage for stage in history["stages"]}
        saccade = stages["saccade"]
        assert saccade["training_sessions"] == 2
        assert saccade["rehearsal_sessions"] == 1
        assert stages["hold"]["sessions"] == []
        first = next(s for s in saccade["sessions"] if s["kind"] == "training")
        # LANDED on 3 of the plan's 6 finished trials.
        assert (first["successes"], first["finished"]) == (3, 6)
        assert first["success_rate"] == pytest.approx(0.5)
        # Over the subject's last 4 training trials (L, H, M, L): 2 of 4.
        verdict = saccade["subjects"]["m01"]["recommendation"]
        assert verdict["verdict"] == "advance"
        assert verdict["metrics"]["success_rate"] == pytest.approx(0.5)


# ----------------------------------------------------------------------
# The command line: run.py's run_experiment(..., ladders=LADDERS)
# ----------------------------------------------------------------------


def lab_rig_file(tmp_path: Path, *, real_data: bool = True) -> Path:
    path = tmp_path / "rig-lab.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "monitor": MONITOR.model_dump(mode="json"),
                "display": {"backend": "simulated"},
                "devices": {"reward": LAB_LINE.model_dump(mode="json")},
                "live_monitor": {"enabled": False, "auto_open": False},
                "data_root": str(tmp_path / "data"),
                "real_data": real_data,
            }
        ),
        encoding="utf-8",
    )
    return path


def run_py(tmp_path: Path, *argv: str, capsys=None) -> int:
    from alhazen.cli.modes import run_experiment

    ladder = write_ladder(tmp_path / "configs")
    return run_experiment(
        tasks=TASKS,
        ladders={"Shaping (monkey)": ladder},
        default_rig=str(lab_rig_file(tmp_path)),
        argv=list(argv),
    )


class TestCommandLine:
    def test_a_stage_runs_from_run_py_without_task_or_params(self, tmp_path, daq, recwarn):  # noqa: F811
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
        root = tmp_path / "data-training" / "shaping" / "saccade"
        card = json.loads(next(root.rglob("session.json")).read_text(encoding="utf-8"))
        assert card["mode"] == "training" and card["training"]["stage"] == "saccade"
        assert card["task"] == "ladder-task"
        rows = rows_of(root)
        assert [r["trial_index"] for r in rows if r["rewarded"] == "True"] == [
            r["trial_index"] for r in rows if r["outcome"] == "LANDED"
        ]
        assert not (tmp_path / "data").exists()
        # The stage named the task: no warning about a command without --task.
        assert not [w for w in recwarn if issubclass(w.category, FutureWarning)]

    @pytest.mark.parametrize(
        ("argv", "words"),
        [
            (["--mode", "training"], "--stage"),
            (["--mode", "run", "--stage", "hold"], "does not take one"),
            (["--mode", "demo", "--stage", "hold"], "does not take one"),
            (["--mode", "training", "--stage", "hold", "--params", "x.yaml"], "--params"),
            (["--mode", "training", "--stage", "nope"], "has no stage 'nope'"),
            (["--mode", "training", "--stage", "hold", "--ladder", "other"], "not a registered"),
            (
                ["--mode", "training", "--stage", "final", "--task", "ladder-task"],
                "disagrees with stage",
            ),
        ],
    )
    def test_refusals_are_usage_errors_before_anything_is_written(
        self, tmp_path, capsys, argv, words
    ):
        code = run_py(tmp_path, *argv, "--sub", "m01", "--ses", "1", "--initials", "MK")
        assert code == 2
        assert words in capsys.readouterr().err
        assert not (tmp_path / "data-training").exists()

    def test_a_development_rig_is_refused(self, tmp_path, capsys):
        from alhazen.cli.modes import run_experiment

        ladder = write_ladder(tmp_path / "configs")
        code = run_experiment(
            tasks=TASKS,
            ladders={"Shaping (monkey)": ladder},
            default_rig=str(lab_rig_file(tmp_path, real_data=False)),
            argv=[
                "--mode",
                "training",
                "--stage",
                "hold",
                "--sub",
                "m01",
                "--ses",
                "1",
                "--initials",
                "MK",
            ],
        )
        assert code == 2
        assert "drives a real subject" in capsys.readouterr().err
        assert not (tmp_path / "data-training").exists()

    def test_the_estimate_reads_the_stage(self, tmp_path, capsys):
        code = run_py(tmp_path, "--mode", "training", "--stage", "saccade", "--estimate-duration")
        out = capsys.readouterr().out.strip().splitlines()[-1]
        answer = json.loads(out)
        assert code == 0, answer
        assert answer["mode"] == "training"
        assert answer["status"] in {"ok", "partial", "unknown"}

    def test_a_rehearsal_of_a_stage_from_run_py(self, tmp_path, recwarn):
        code = run_py(tmp_path, "--mode", "simulate", "--stage", "saccade", "--headless")
        assert code == 0
        # The stage names the task: no warning about a command without --task.
        assert not [w for w in recwarn if issubclass(w.category, FutureWarning)]
        assert list(
            (tmp_path / "data-training-rehearsal" / "shaping" / "saccade").rglob("session.json")
        )
        assert not (tmp_path / "data-training").exists()
