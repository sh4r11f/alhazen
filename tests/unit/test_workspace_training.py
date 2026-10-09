"""The experiment workspace's Training mode (cli/workspace.py): the ladders
read out of run.py, the command a Training launch builds (and a rehearsal of
a stage), what it refuses, what the run record keeps, and the training
history read back from the stage folders.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import pytest
import yaml

from alhazen.cli import workspace as workspace_module
from alhazen.cli.main import add_mode_arguments
from alhazen.cli.workspace import Launch, Workspace

RIG = Path(__file__).parents[2] / "examples/minimal_fixation/rig-sim.yaml"

RUN_PY = """import json, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
TASKS = {"shape-task": (object, HERE / "configs" / "task.yaml")}
PARAMETERS = {"Main": ("shape-task", HERE / "configs" / "task.yaml")}
LADDERS = {"Shaping (monkey)": HERE / "configs" / "training-shaping.yaml"}
print(json.dumps(sys.argv[1:]), flush=True)
if __name__ == "__nothing__":
    run_experiment(tasks=TASKS, ladders=LADDERS, default_rig="sim")
"""

LADDER = {
    "name": "shaping",
    "title": "Shaping",
    "description": "Hold, then look.",
    "task": "shape-task",
    "params": "task-monkey.yaml",
    "stages": [
        {"id": "hold", "title": "Hold", "success": "HELD"},
        {
            "id": "look",
            "title": "Look at the dot",
            "success": "LANDED",
            "overrides": {"gap": 2},
            "criterion": {"window": 4, "min_trials": 2, "promote_when": {"success_rate": 0.5}},
        },
    ],
}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(
        workspace_module,
        "probe_interpreter",
        lambda python, path: {
            "alhazen_version": "2.13.0",
            "python_version": "stub",
            "shared_rigs": [],
            "capabilities": ["duration-estimate", "training-mode"],
        },
    )
    root = tmp_path / "experiment"
    (root / "configs").mkdir(parents=True)
    (root / "configs/rig-sim.yaml").write_bytes(RIG.read_bytes())
    (root / "configs/rig-laptop.yaml").write_text(
        RIG.read_text(encoding="utf-8") + "real_data: false\n", encoding="utf-8"
    )
    (root / "configs/task.yaml").write_text("speed: 3\n", encoding="utf-8")
    (root / "configs/task-monkey.yaml").write_text("speed: 3\n", encoding="utf-8")
    (root / "configs/training-shaping.yaml").write_text(yaml.safe_dump(LADDER), encoding="utf-8")
    (root / "run.py").write_text(RUN_PY, encoding="utf-8")
    space = Workspace(tmp_path / "state")
    space.add(str(root), sys.executable)
    yield space
    space.close()


def launch(workspace, **overrides) -> Launch:
    return Launch(
        **{
            "project": workspace.projects[0]["id"],
            "mode": "training",
            "rig": "configs/rig-sim.yaml",
            "subject": "m01",
            "initials": "MK",
            "ladder": "Shaping (monkey)",
            "stage": "look",
            **overrides,
        }
    )


def parsed(command: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_mode_arguments(parser)
    return parser.parse_args(command[3:])


class TestDescribe:
    def test_the_ladders_are_read_out_of_run_py(self, workspace):
        described = workspace.describe(workspace.projects[0]["id"])
        assert described["trains"] is True
        assert described["ladders_error"] is None
        [ladder] = described["ladders"]
        assert ladder["label"] == "Shaping (monkey)"
        assert ladder["name"] == "shaping"
        assert ladder["file"] == "configs/training-shaping.yaml"
        assert [s["id"] for s in ladder["stages"]] == ["hold", "look"]
        look = ladder["stages"][1]
        assert look["number"] == 2 and look["success"] == "LANDED"
        assert look["task"] == "shape-task" and look["params"] == "task-monkey.yaml"
        assert look["criterion"]["promote_when"] == {"success_rate": 0.5}

    def test_a_ladder_that_cannot_be_read_is_said(self, workspace):
        root = Path(workspace.projects[0]["path"])
        (root / "configs/training-shaping.yaml").write_text("name: Bad Name\n", encoding="utf-8")
        described = workspace.describe(workspace.projects[0]["id"])
        assert described["ladders"] == []
        assert "LADDERS['Shaping (monkey)']" in described["ladders_error"]

    def test_a_stage_naming_another_task_is_said(self, workspace):
        root = Path(workspace.projects[0]["path"])
        ladder = {**LADDER, "task": "nope"}
        (root / "configs/training-shaping.yaml").write_text(yaml.safe_dump(ladder), "utf-8")
        described = workspace.describe(workspace.projects[0]["id"])
        assert "not one of TASKS" in described["ladders_error"]


class TestCommand:
    def test_training_sends_the_ladder_and_stage_and_no_params_or_task(self, workspace):
        command = workspace._command(launch(workspace), workspace.directory / "job")
        args = parsed(command)
        assert args.mode == "training"
        assert args.ladder == "shaping" and args.stage == "look"
        assert args.params is None and "--task" not in command
        assert args.sub == "m01" and args.initials == "MK"
        assert not args.headless

    def test_a_rehearsal_is_a_headless_simulation_of_the_stage(self, workspace):
        command = workspace._command(
            launch(workspace, rehearse=True, trials=2), workspace.directory / "job"
        )
        args = parsed(command)
        assert args.mode == "simulate" and args.headless
        assert args.stage == "look" and args.trials_per_condition == 2

    @pytest.mark.parametrize(
        ("change", "words"),
        [
            ({"stage": "pursue"}, "Choose a stage of Shaping"),
            ({"stage": None}, "Choose a stage"),
            ({"ladder": "other"}, "Choose a training ladder"),
            ({"parameters": {"speed": 1}}, "names its own parameters"),
            ({"mode": "run"}, "for Training"),
            ({"rig": "configs/rig-laptop.yaml"}, "development rig"),
            ({"subject": ""}, "subject ID is required for training"),
            ({"initials": ""}, "initials are required for training"),
        ],
    )
    def test_refusals_before_anything_is_written(self, workspace, change, words):
        with pytest.raises(ValueError, match=words):
            workspace._command(launch(workspace, **change), workspace.directory / "job")

    def test_a_rehearsal_may_use_a_development_rig(self, workspace):
        command = workspace._command(
            launch(workspace, rig="configs/rig-laptop.yaml", rehearse=True),
            workspace.directory / "job",
        )
        assert parsed(command).mode == "simulate"

    def test_an_alhazen_that_cannot_train_is_refused(self, workspace):
        workspace.projects[0]["capabilities"] = ["duration-estimate"]
        with pytest.raises(ValueError, match="cannot run training stages"):
            workspace._command(launch(workspace), workspace.directory / "job")


class TestRunRecord:
    def test_the_run_keeps_the_stage(self, workspace):
        run = workspace.start(launch(workspace))
        workspace.worker.join(timeout=10)
        assert run["mode"] == "training"
        assert run["training"] == {"ladder": "shaping", "stage": "look", "rehearse": False}
        assert run["task"] == "shape-task"
        assert run["parameter_set"] is None
        launched = json.loads(
            (workspace.directory / "runs" / run["id"] / "launch.json").read_text("utf-8")
        )
        assert launched["training"]["stage"] == "look"


def write_stage_run(root: Path, stage: str, outcomes: list[str], session: int = 1) -> Path:
    folder = (
        root
        / "data-training"
        / "shaping"
        / stage
        / "v1.0.0"
        / "sub-m01"
        / f"ses-{session:03d}"
        / "run-01_task-shape-task"
    )
    folder.mkdir(parents=True)
    (folder / "session.json").write_text(
        json.dumps(
            {
                "mode": "training",
                "date": "20261008",
                "created": f"2026-10-0{session}T10",
                "training": {"ladder": "shaping", "stage": stage, "success": "LANDED"},
            }
        ),
        encoding="utf-8",
    )
    with (folder / "sub-m01_ses-001_trials.csv").open("w", encoding="utf-8", newline="") as h:
        writer = csv.DictWriter(h, fieldnames=["trial_index", "outcome", "completed"])
        writer.writeheader()
        for index, outcome in enumerate(outcomes):
            writer.writerow(
                {"trial_index": index, "outcome": outcome, "completed": outcome != "FIX_BREAK"}
            )
    return folder


class TestHistory:
    def test_stage_sessions_and_recommendations_are_read_back(self, workspace):
        root = Path(workspace.projects[0]["path"])
        write_stage_run(root, "look", ["LANDED", "MISS", "FIX_BREAK", "LANDED"])
        answer = workspace.training(workspace.projects[0]["id"])
        [ladder] = answer["ladders"]
        assert ladder["label"] == "Shaping (monkey)"
        look = next(s for s in ladder["stages"] if s["id"] == "look")
        assert look["training_sessions"] == 1
        assert (look["successes"], look["finished"]) == (2, 3)
        assert look["subjects"]["m01"]["recommendation"]["verdict"] == "advance"
        hold = next(s for s in ladder["stages"] if s["id"] == "hold")
        assert hold["sessions"] == []

    def test_stage_folders_are_data_roots_of_their_own(self, workspace):
        from alhazen.cli.workspace_data import data_roots

        root = Path(workspace.projects[0]["path"])
        write_stage_run(root, "look", ["LANDED"])
        existing, _missing, _ = data_roots(workspace.describe(workspace.projects[0]["id"]))
        training = [r for r in existing if r.kind == "training"]
        assert [r.as_json()["name"] for r in training] == ["shaping/look"]


class TestEstimate:
    def test_the_estimate_asks_about_the_stage(self, workspace, monkeypatch):
        from alhazen.cli.workspace_estimate import DurationEstimator, EstimateRequest

        estimator = DurationEstimator(workspace)
        asked = []
        monkeypatch.setattr(
            estimator,
            "_ask",
            lambda project, command, text, form="": asked.append(command) or {"status": "ok"},
        )
        project = workspace.projects[0]["id"]
        estimator.estimate(
            EstimateRequest(
                project=project,
                mode="training",
                rig="configs/rig-sim.yaml",
                ladder="Shaping (monkey)",
                stage="look",
            )
        )
        estimator.estimate(
            EstimateRequest(
                project=project,
                mode="training",
                rig="configs/rig-sim.yaml",
                ladder="Shaping (monkey)",
                stage="look",
                rehearse=True,
                trials=2,
            )
        )
        training, rehearsal = asked
        assert training[1:3] == ["--mode", "training"]
        assert training[training.index("--stage") + 1] == "look"
        assert "--task" not in training
        assert rehearsal[1:3] == ["--mode", "simulate"] and "--headless" in rehearsal
        assert rehearsal[rehearsal.index("--trials-per-condition") + 1] == "2"

    def test_no_stage_yet_is_no_estimate_yet(self, workspace):
        from alhazen.cli.workspace_estimate import DurationEstimator, EstimateRequest

        answer = DurationEstimator(workspace).estimate(
            EstimateRequest(
                project=workspace.projects[0]["id"],
                mode="training",
                rig="configs/rig-sim.yaml",
                ladder="Shaping (monkey)",
            )
        )
        assert answer["status"] == "unknown" and "Choose the training stage" in answer["reason"]


class TestJuiceInHistory:
    def test_a_paying_sessions_details_carry_its_juice(self, workspace):
        from alhazen.cli.workspace_data import DataView, data_roots

        root = Path(workspace.projects[0]["path"])
        folder = write_stage_run(root, "look", ["LANDED", "MISS"])
        card = json.loads((folder / "session.json").read_text(encoding="utf-8"))
        card["reward"] = {"line_open": True, "delivered": {"ul_per_pulse": {"150": 7.5}}}
        (folder / "session.json").write_text(json.dumps(card), encoding="utf-8")
        with (folder / "sub-m01_ses-001_events.csv").open("w", encoding="utf-8", newline="") as h:
            writer = csv.DictWriter(h, fieldnames=["trial_index", "event", "t", "payload_json"])
            writer.writeheader()
            pulses = {"n_pulses": 1, "pulse_ms": 150, "inter_pulse_ms": 200}
            writer.writerow(
                {
                    "trial_index": 0,
                    "event": "REWARD",
                    "t": 1.0,
                    "payload_json": json.dumps(
                        {"manual": False, "outcome": "LANDED", "pulses": pulses}
                    ),
                }
            )
            writer.writerow(
                {
                    "trial_index": 1,
                    "event": "REWARD",
                    "t": 2.0,
                    "payload_json": json.dumps(
                        {
                            "manual": False,
                            "outcome": "ABORTED",
                            "fault": "tracker_stopped",
                            "pulses": pulses,
                        }
                    ),
                }
            )
        existing, _, _ = data_roots(workspace.describe(workspace.projects[0]["id"]))
        stage = next(r for r in existing if r.kind == "training")
        run_id = folder.relative_to(stage.path).as_posix()
        detail = DataView(workspace).run(workspace.projects[0]["id"], stage.id, run_id)
        juice = detail["juice"]
        assert juice["form"] == "juice" and juice["unit"] == "µL"
        assert juice["total"] == 15.0
        assert {t["x"]: (t["outcome"], t["fault"]) for t in juice["trials"]} == {
            0: (7.5, 0.0),
            1: (0.0, 7.5),
        }

    def test_a_human_sessions_details_carry_none(self, workspace):
        from alhazen.cli.workspace_data import DataView, data_roots

        root = Path(workspace.projects[0]["path"])
        folder = write_stage_run(root, "hold", ["HELD"])
        existing, _, _ = data_roots(workspace.describe(workspace.projects[0]["id"]))
        stage = next(r for r in existing if r.kind == "training")
        detail = DataView(workspace).run(
            workspace.projects[0]["id"], stage.id, folder.relative_to(stage.path).as_posix()
        )
        assert detail["juice"] is None
