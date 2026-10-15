"""Launcher tests use real child processes and HTTP, without a renderer or rig."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
from http.client import HTTPConnection
from pathlib import Path
from xml.etree import ElementTree

import pytest
import yaml

import alhazen
from alhazen.cli import workspace as workspace_module
from alhazen.cli.dashboard import DashboardServer, Handler, workspace_lock
from alhazen.cli.main import add_mode_arguments
from alhazen.cli.workspace import (
    MODE_FLAGS,
    STOP_GRACE_S,
    Launch,
    Workspace,
    no_browser_flag,
    parse_parameters,
    path_inside,
    script_actions,
)
from alhazen.config.calibration_images import IMAGE_DIR, image_names, image_path
from alhazen.config.models import INITIALS_RULE
from alhazen.modes import Mode

RIG = Path(__file__).parents[2] / "examples/minimal_fixation/rig-sim.yaml"
# The real probe, kept from before the fixture stubs it, for the tests of the
# probe itself.
REAL_PROBE = workspace_module.probe_interpreter


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    # Registering an experiment imports alhazen in the project's interpreter,
    # which costs seconds per test; TestInterpreters runs the real probe.
    monkeypatch.setattr(
        workspace_module,
        "probe_interpreter",
        # A version the launcher can read (it picks a flag's spelling by it,
        # no_browser_flag); the interpreter is never really probed here. No
        # shared rigs: tests/unit/test_workspace_rigs.py covers those.
        lambda python, path: {
            "alhazen_version": "1.9.0",
            "python_version": "stub",
            "shared_rigs": [],
        },
    )
    root = tmp_path / "experiment with spaces"
    (root / "configs/rigs").mkdir(parents=True)
    (root / "configs/rig-sim.yaml").write_bytes(RIG.read_bytes())
    # A rig in a subdirectory: its relative path has a separator, which is
    # where Windows and POSIX records used to differ.
    (root / "configs/rigs/rig-lab.yaml").write_bytes(RIG.read_bytes())
    (root / "configs/task.yaml").write_text("speed: 3\nduration: {ms: 100}\n")
    (root / "run.py").write_text(
        "import json, sys\nfrom pathlib import Path\n"
        "print(json.dumps(sys.argv[1:]), flush=True)\n"
        "if '--out' in sys.argv:\n"
        "    out = Path(sys.argv[sys.argv.index('--out') + 1])\n"
        "    (out / 'clip.mp4').write_bytes(b'0123456789')\n"
    )
    space = Workspace(tmp_path / "state")
    space.add(str(root), sys.executable)
    yield space
    space.close()


def request_for(workspace, **overrides):
    return Launch(
        **{
            "project": workspace.projects[0]["id"],
            "mode": "movie",
            "rig": "configs/rig-sim.yaml",
            **overrides,
        }
    )


def calibration_offer() -> dict:
    """What the probe records for a project whose alhazen offers the
    calibration-target choice: this alhazen's own pictures and defaults."""
    from alhazen.config.calibration_images import IMAGE_DIR, image_names
    from alhazen.config.models import CalibrationTargetConfig

    return {
        "dir": str(IMAGE_DIR.resolve()),
        "images": list(image_names()),
        "defaults": CalibrationTargetConfig().model_dump(mode="json"),
    }


def eyelink_rig(root: Path) -> Path:
    """configs/rig-eyelink.yaml: the example rig with an EyeLink, a tracker
    that draws a calibration target (never connected here)."""
    text = RIG.read_text(encoding="utf-8")
    assert "devices:" not in text
    path = root / "configs/rig-eyelink.yaml"
    path.write_text(text + "\ndevices:\n  eyetracker:\n    backend: eyelink\n", encoding="utf-8")
    return path


def finish(workspace, run):
    workspace.worker.join(timeout=10)
    assert not workspace.worker.is_alive()
    return workspace.detail(run["id"])


class TestProjects:
    def test_registry_discovery_and_roundtrip(self, workspace):
        p = workspace.describe(workspace.projects[0]["id"])
        # By name, in name order; each path posix on every OS, so a registry
        # or run record written on a Windows rig reads the same on a Mac — and
        # CI is green on both.
        assert p["rigs"] == [
            {
                "name": "lab",
                "source": "experiment",
                "shadowed": False,
                "extends": None,
                "path": "configs/rigs/rig-lab.yaml",
            },
            {
                "name": "sim",
                "source": "experiment",
                "shadowed": False,
                "extends": None,
                "path": "configs/rig-sim.yaml",
            },
        ]
        assert p["rigs_note"] is None
        assert p["configs"] == ["configs/task.yaml"]
        paths = [rig["path"] for rig in p["rigs"]] + p["configs"]
        assert not any("\\" in path for path in paths)
        assert workspace.config(p["id"], p["configs"][0])["values"]["speed"] == 3
        assert "monitor" in workspace.rig(p["id"], p["rigs"][0]["path"])["values"]
        workspace.add(p["path"], sys.executable)
        assert len(workspace.projects) == 1
        restored = Workspace(workspace.directory)
        assert restored.state()["projects"] == workspace.state()["projects"]
        workspace.remove(p["id"])
        assert workspace.state()["projects"] == []

    def test_describe_names_the_experiment_by_its_title_and_slug(self, workspace):
        key = workspace.projects[0]["id"]
        root = Path(workspace.projects[0]["path"])
        # No pyproject.toml: the folder names it, and nothing is wrong.
        described = workspace.describe(key)
        assert (described["title"], described["slug"], described["title_error"]) == (
            "experiment with spaces",
            "experiment with spaces",
            None,
        )
        # A declared title is shown; the [project] name is the slug. Read on
        # every describe, so an edit shows on the next poll.
        (root / "pyproject.toml").write_text(
            '[project]\nname = "amodal-averaging"\n[tool.alhazen]\ntitle = "Amodal averaging"\n',
            encoding="utf-8",
        )
        described = workspace.describe(key)
        assert (described["title"], described["slug"], described["title_error"]) == (
            "Amodal averaging",
            "amodal-averaging",
            None,
        )
        # The registry's own name is untouched: run records made before
        # titles existed carry it.
        assert described["name"] == "experiment with spaces"

    def test_a_title_that_cannot_be_used_is_reported_and_the_workspace_still_works(self, workspace):
        key = workspace.projects[0]["id"]
        root = Path(workspace.projects[0]["path"])
        (root / "pyproject.toml").write_text(
            '[project]\nname = "demo"\n[tool.alhazen]\ntitle = 42\n', encoding="utf-8"
        )
        described = workspace.describe(key)
        assert (described["title"], described["slug"]) == ("demo", "demo")
        assert "title must be a non-empty string" in described["title_error"]
        # The state the page polls still lists it, with the reason.
        assert workspace.state()["projects"][0]["title_error"] == described["title_error"]

    def test_invalid_paths_and_interpreter(self, workspace, tmp_path):
        with pytest.raises(ValueError, match="No run.py"):
            workspace.add(str(tmp_path))
        with pytest.raises(ValueError, match="interpreter does not exist"):
            workspace.add(workspace.projects[0]["path"], str(tmp_path / "missing"))
        with pytest.raises(ValueError, match="not registered"):
            workspace.describe("missing")
        with pytest.raises(ValueError, match="YAML"):
            workspace.config(workspace.projects[0]["id"], "run.py")

    def test_script_discovery_does_not_import(self, workspace):
        root = Path(workspace.projects[0]["path"])
        package = root / "src/my_experiment"
        package.mkdir(parents=True)
        (package / "preview.py").write_text(
            "raise RuntimeError('do not import')\n"
            "parser.add_argument('--out')\nparser.add_argument('--task-config')\n"
            "parser.add_argument('--rig')\nif __name__ == '__main__': main()\n"
        )
        actions = script_actions(root)
        assert len(actions) == 1
        assert actions[0]["module"] == "my_experiment.preview"
        assert actions[0]["params_flag"] == "--task-config"
        # The experiment's own script may read the task's file, so it keeps
        # the Task menu.
        assert actions[0]["task_free"] is False
        args = workspace._command(
            request_for(workspace, mode=actions[0]["id"], parameters={"speed": 4}),
            workspace.directory / "job",
        )
        assert "-m" in args and "--task-config" in args and "--rig" in args
        # The refusal names the flag, in its `--flag=value` spelling too.
        with pytest.raises(ValueError, match=r"--out is set from the dashboard controls"):
            workspace._command(
                request_for(workspace, mode=actions[0]["id"], extra_args="--out=/tmp/elsewhere"),
                workspace.directory,
            )

    def test_a_movie_script_is_not_offered(self, workspace):
        """Movies are the Record movies mode's alone. A movie.py with a command
        line, which used to be listed beside the mode as "Movie script", gets
        no button, with or without declared stimuli."""
        root = Path(workspace.projects[0]["path"])
        package = root / "src/my_experiment"
        package.mkdir(parents=True)
        (package / "movie.py").write_text(
            "parser.add_argument('--out')\nparser.add_argument('--task-config')\n"
            "parser.add_argument('--rig')\nif __name__ == '__main__': main()\n"
        )
        assert script_actions(root) == []
        declare_stimuli(root)
        assert [a["id"] for a in script_actions(root)] == ["alhazen.preview"]

    def test_a_preview_that_does_not_parse_is_reported_not_hidden(self, workspace, caplog):
        """A preview.py with a syntax error gets no button — but silently, the
        missing button reads as "not a generator" instead of "broken". The
        warning names the file and the error, and the other scripts are still
        offered."""
        root = Path(workspace.projects[0]["path"])
        broken = root / "src/broken/preview.py"
        broken.parent.mkdir(parents=True)
        broken.write_text("parser.add_argument('--out'\nif __name__ == '__main__': main(\n")
        good = root / "src/good/preview.py"
        good.parent.mkdir(parents=True)
        good.write_text("parser.add_argument('--out')\nif __name__ == '__main__': main()\n")
        with caplog.at_level("WARNING", logger="alhazen.cli.workspace"):
            actions = script_actions(root)
        assert [a["module"] for a in actions] == ["good.preview"]
        assert len(caplog.records) == 1 and caplog.records[0].levelname == "WARNING"
        # The line and wording of a SyntaxError vary by Python version; the
        # warning must carry whatever this interpreter says about this file.
        with pytest.raises(SyntaxError) as error:
            ast.parse(broken.read_text())
        assert str(broken) in caplog.text
        assert f"line {error.value.lineno}: {error.value.msg}" in caplog.text

    def test_declared_stimuli_get_alhazens_preview_instead_of_the_experiments_own(self, workspace):
        """An experiment that declares its stimuli gets one Preview images,
        alhazen's, which draws them with no task and no parameter file. Its
        own preview.py, which would read a parameter file, is not offered
        beside it."""
        root = Path(workspace.projects[0]["path"])
        declare_stimuli(root)
        package = root / "src/my_experiment"
        package.mkdir(parents=True)
        (package / "preview.py").write_text(
            "parser.add_argument('--out')\nparser.add_argument('--task-config')\n"
            "if __name__ == '__main__': main()\n"
        )
        actions = script_actions(root)
        assert [a["id"] for a in actions] == ["alhazen.preview"]
        preview = actions[0]
        assert preview["label"] == "Preview images"
        assert (preview["params_flag"], preview["task_free"], preview["error"]) == (
            None,
            True,
            None,
        )

    def test_the_preview_command_names_no_task_and_no_parameter_file(self, workspace):
        root = Path(workspace.projects[0]["path"])
        declare_stimuli(root)
        job = workspace.directory / "job"
        args = workspace._command(request_for(workspace, mode="alhazen.preview"), job)
        assert args[2:] == [
            "-m",
            "alhazen",
            "preview",
            "--project",
            str(root),
            "--rig",
            str(root / "configs/rig-sim.yaml"),
            "--out",
            str(job / "media"),
        ]
        with pytest.raises(ValueError, match="takes no parameter file"):
            workspace._command(
                request_for(workspace, mode="alhazen.preview", parameters={"speed": 4}), job
            )
        # What the launcher sets may not be contradicted from the extras.
        with pytest.raises(ValueError, match=r"--project is set from the dashboard controls"):
            workspace._command(
                request_for(workspace, mode="alhazen.preview", extra_args="--project=/elsewhere"),
                job,
            )

    def test_a_declaration_that_cannot_be_used_is_offered_and_refused_with_its_reason(
        self, workspace
    ):
        """The button stays, so the reader learns why on launching it: a
        missing button would say "nothing declared" instead."""
        root = Path(workspace.projects[0]["path"])
        (root / "pyproject.toml").write_text(
            '[project]\nname = "demo"\nversion = "0.1.0"\n[tool.alhazen]\nstimuli = 42\n',
            encoding="utf-8",
        )
        (action,) = script_actions(root)
        assert action["id"] == "alhazen.preview"
        assert "package.module:function" in action["error"]
        with pytest.raises(ValueError, match="package.module:function"):
            workspace._command(
                request_for(workspace, mode="alhazen.preview"), workspace.directory / "job"
            )


def declare_stimuli(root: Path) -> None:
    """Give the workspace fixture's experiment a pyproject.toml that declares
    its stimuli, and the package that draws them: one grey dot, one degree
    across at the rig's scale."""
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n\n'
        '[tool.alhazen]\nstimuli = "declared_demo.stimulus_set:stimulus_images"\n',
        encoding="utf-8",
    )
    package = root / "src/declared_demo"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "stimulus_set.py").write_text(
        "import numpy as np\n"
        "from alhazen.stimuli import StimulusImage\n\n"
        "def stimulus_images(screen):\n"
        "    size = int(round(screen.px_per_deg))\n"
        "    return [StimulusImage('dot', np.full((size, size), 0.5), 'a grey square')]\n",
        encoding="utf-8",
    )


class TestLaunches:
    def test_preview_images_draws_the_declared_stimuli_in_a_real_process(self, workspace):
        """The whole path: the page's request, the project's interpreter
        running `python -m alhazen preview`, and the images in the run's media
        folder, where the gallery shows them."""
        root = Path(workspace.projects[0]["path"])
        declare_stimuli(root)
        run = finish(workspace, workspace.start(request_for(workspace, mode="alhazen.preview")))
        assert run["status"] == "completed", run["log"]
        assert run["returncode"] == 0
        # The gallery lists the images; the index is beside them on disk.
        assert [a["path"] for a in run["artifacts"]] == ["dot.png"]
        media = Path(run["directory"]) / "media"
        assert (media / "README.md").is_file()
        assert (media / "dot.png").read_bytes().startswith(b"\x89PNG")
        # No parameter file was written for it: it takes none.
        assert not (Path(run["directory"]) / "params.yaml").exists()

    def test_real_process_media_snapshot_logs_and_history(self, workspace):
        original = Path(workspace.projects[0]["path"]) / "configs/task.yaml"
        content = original.read_bytes()
        run = finish(
            workspace,
            workspace.start(
                request_for(workspace, parameters={"speed": 7, "duration": {"ms": 150}})
            ),
        )
        assert run["status"] == "completed" and run["returncode"] == 0
        assert workspace.active is None
        assert run["artifacts"][0]["path"] == "clip.mp4"
        # Relative paths in the record and the gallery are posix on every OS.
        assert run["rig"] == "configs/rig-sim.yaml"
        assert (run["rig_name"], run["rig_source"]) == ("sim", "experiment")
        frames = Path(run["directory"]) / "media/frames"
        frames.mkdir()
        (frames / "first.png").write_bytes(b"png")
        listed = [a["path"] for a in workspace.detail(run["id"])["artifacts"]]
        assert listed == ["clip.mp4", "frames/first.png"]
        assert '--mode", "movie"' in run["log"]
        assert yaml.safe_load((Path(run["directory"]) / "params.yaml").read_text())["speed"] == 7
        assert (Path(run["directory"]) / "rig.yaml").read_bytes() == RIG.read_bytes()
        # A whole rig file is copied as it is, and alone.
        assert not (Path(run["directory"]) / "rig-source.yaml").exists()
        assert original.read_bytes() == content
        restored = Workspace(workspace.directory)
        assert restored.detail(run["id"])["status"] == "completed"
        run2 = finish(workspace, workspace.start(request_for(workspace)))
        assert run2["directory"] != run["directory"]
        assert Path(run["directory"], "media/clip.mp4").exists()

    @pytest.mark.parametrize("mode", ["simulate", "test", "run", "demo", "measure", "movie"])
    def test_standard_modes(self, workspace, mode):
        req = request_for(
            workspace,
            mode=mode,
            subject="s01",
            initials="hd",
            seed=42,
            parameters=None if mode == "measure" else {"speed": 2},
            headless=mode == "simulate",
            mouse=mode == "test",
            sheet=True,
            columns=2,
            clips=["one"],
        )
        cmd = workspace._command(req, workspace.directory / "job")
        assert cmd[:2] == [sys.executable, "-u"]
        assert cmd[cmd.index("--mode") + 1] == mode
        assert cmd[cmd.index("--seed") + 1] == "42"
        assert ("--params" in cmd) == (mode != "measure")
        if mode == "movie":
            assert "--sheet" in cmd and "--clip" in cmd and "--columns" in cmd
        if mode == "demo":
            assert "--screenshots" in cmd
        if mode == "simulate":
            assert "--headless" in cmd
        if mode == "test":
            assert "--mouse" in cmd
        # The subject's initials, uppercase as run.py records them, for the
        # modes that run trials — and only for those.
        if Mode(mode).runs_trials:
            assert cmd[cmd.index("--initials") + 1] == "HD"
        else:
            assert "--initials" not in cmd

    @pytest.mark.parametrize(
        "args, message",
        [
            ({"mode": "run"}, "subject ID"),
            ({"mode": "movie", "headless": True}, "without a window only with"),
            ({"mode": "run", "mouse": True}, "only test"),
            ({"mode": "missing"}, "Unknown experiment"),
            ({"rig": "../outside.yaml"}, "inside"),
            ({"rig": "missing.yaml"}, "existing rig"),
            # Movie mode: --out is the launcher's, so the run record's media
            # directory cannot be contradicted from the extra arguments.
            ({"extra_args": "--out elsewhere"}, "--out is set from the dashboard controls"),
            ({"parameters": {}, "parameters_yaml": "speed: 2"}, "either"),
            ({"parameters_yaml": "[1,2]"}, "mapping"),
            ({"mode": "measure", "parameters": {"speed": 2}}, "does not use task parameters"),
        ],
    )
    def test_invalid_launches_fail_before_creating_a_run(self, workspace, args, message):
        with pytest.raises(ValueError, match=message):
            workspace.start(request_for(workspace, **args))
        assert workspace.runs == {}

    def test_nonzero_exit_and_spawn_failure_are_visible(self, workspace):
        root = Path(workspace.projects[0]["path"])
        (root / "run.py").write_text("raise RuntimeError('bad parameter combination')")
        run = finish(workspace, workspace.start(request_for(workspace)))
        assert run["status"] == "failed" and "bad parameter combination" in run["log"]
        workspace.projects[0]["python"] = str(root / "missing-python")
        failed = workspace.start(request_for(workspace))
        assert failed["status"] == "failed" and failed["error"]
        assert workspace.active is None

    def test_stop_and_overlapping_runs(self, workspace, monkeypatch):
        started, stopped = threading.Event(), threading.Event()

        class Process:
            pid = 123

            def wait(self, timeout=None):
                started.set()
                assert stopped.wait(timeout=5)
                return -2

            def poll(self):
                return -2 if stopped.is_set() else None

        monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Process())
        monkeypatch.setattr(workspace, "_signal", lambda process, force: stopped.set())
        run = workspace.start(request_for(workspace))
        assert started.wait(timeout=5)
        with pytest.raises(ValueError, match="Another run"):
            workspace.start(request_for(workspace))
        with pytest.raises(ValueError, match="Stop this experiment"):
            workspace.remove(run["project"])
        workspace.stop(run["id"])
        assert finish(workspace, run)["status"] == "cancelled"
        with pytest.raises(ValueError, match="no longer active"):
            workspace.stop(run["id"])

    def test_a_forced_kill_is_recorded_honestly(self, workspace, monkeypatch):
        """A run that ignores the interrupt for the whole grace period is
        killed — and the history must say so, because a killed session has
        no trials file and no manifest, and "cancelled" would read as clean.
        """
        started, exited = threading.Event(), threading.Event()
        graces, signals = [], []

        class Process:
            pid = 123

            def wait(self, timeout=None):
                started.set()
                if timeout is not None:
                    # The child never reacts to the interrupt: the grace runs out.
                    graces.append(timeout)
                    raise subprocess.TimeoutExpired("run.py", timeout)
                assert exited.wait(timeout=5)
                return -9

            def poll(self):
                return -9 if exited.is_set() else None

        def send(process, force):
            signals.append(force)
            if force:
                exited.set()

        monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Process())
        monkeypatch.setattr(workspace, "_signal", send)
        run = workspace.start(request_for(workspace))
        assert started.wait(timeout=5)
        workspace.stop(run["id"])
        detail = finish(workspace, run)
        # docs/workspace.md promises thirty seconds; an EDF transfer plus
        # manifest hashing does not fit in the ten it used to be.
        assert STOP_GRACE_S == 30 and graces == [STOP_GRACE_S]
        assert signals == [False, True]
        assert detail["status"] == "killed" and detail["stopped"] == "forced"
        assert f"killed after {STOP_GRACE_S} s" in detail["log"]
        assert "may be incomplete" in detail["log"]
        record = json.loads((Path(detail["directory"]) / "run.json").read_text())
        assert record["status"] == "killed" and record["stopped"] == "forced"

    def test_interrupted_history_and_log_tail(self, workspace):
        run = finish(workspace, workspace.start(request_for(workspace)))
        directory = Path(run["directory"])
        record = json.loads((directory / "run.json").read_text())
        record["status"] = "running"
        (directory / "run.json").write_text(json.dumps(record))
        # The contract is the line the CLI prints before trial one
        # (cli/main.py _trial_session: "dashboard: <url>", pinned in
        # test_task_hooks.py). The runner's own "live monitor:" line goes
        # only to session.log, never to the console, so it is not what a
        # launched run's console holds. The last URL in the tail wins.
        (directory / "console.log").write_text(
            "x" * 80000 + "\nparams: configs/task.yaml\n"
            "running demo: sub-s01 ses-001 run-01\n"
            "dashboard: http://127.0.0.1:1111/?token=stale\n"
            "dashboard: http://127.0.0.1:1234/?token=abc_-123\n"
        )
        restored = Workspace(workspace.directory)
        detail = restored.detail(run["id"])
        assert detail["status"] == "interrupted"
        assert len(detail["log"]) == 65536
        assert detail["monitor"] == "http://127.0.0.1:1234/?token=abc_-123"

    def test_nested_media_paths_use_forward_slashes(self, workspace):
        # The page splits artifact paths on "/" to build media URLs. A Windows
        # backslash would be percent-encoded into one segment instead.
        run = finish(workspace, workspace.start(request_for(workspace)))
        nested = Path(run["directory"]) / "media" / "frames" / "first.png"
        nested.parent.mkdir()
        nested.write_bytes(b"png")
        paths = [a["path"] for a in workspace.detail(run["id"])["artifacts"]]
        assert paths == ["clip.mp4", "frames/first.png"]


class TestCommandContract:
    """The launcher hand-builds run.py's flags; ``add_mode_arguments`` is the
    parser that has to accept them. A flag renamed in cli/main.py would
    otherwise break every launch silently: the child would exit on a usage
    error and the run would just read "failed". So every command the launcher
    can build is parsed with the runner's own parser — strictly (parse_args,
    not parse_known_args), so a flag the runner no longer knows is a failure.

    The extra arguments are not the runner's to parse: an experiment's
    `--task` is stripped by its own run.py before run_experiment sees argv.
    They ride at the very end of the command, so the contract is the part
    before them, and the parse stops exactly where they begin.
    """

    # Training names a ladder stage instead of parameters: its command is held
    # to the same parser in tests/unit/test_workspace_training.py.
    @pytest.mark.parametrize(
        "mode, sheet",
        [(m.value, False) for m in Mode if m is not Mode.TRAINING] + [("movie", True)],
    )
    def test_every_mode_command_parses_with_the_runner_parser(self, workspace, mode, sheet):
        request = request_for(
            workspace,
            mode=mode,
            subject="s01",
            initials="HD",
            session=2,
            seed=3,
            trials=4,
            parameters=None if mode == "measure" else {"speed": 2},
            headless=mode == "simulate",
            mouse=mode == "test",
            windowed=True,
            scale=0.25,
            sheet=sheet,
            columns=2 if sheet else None,
            clips=["one", "two"] if mode == "movie" else [],
            extra_args="--task mib-detect",
        )
        command = workspace._command(request, workspace.directory / "job")
        # The extras are last, after every flag of the launcher's own — a
        # movie's repeated --clip included — so run.py finds them where a
        # typed command would put them.
        extras = shlex.split(request.extra_args)
        assert command[-len(extras) :] == extras
        parser = argparse.ArgumentParser()
        add_mode_arguments(parser)
        # After <python> -u run.py, before the extras.
        args = parser.parse_args(command[3 : -len(extras)])
        assert args.mode == mode and args.seed == 3 and args.windowed
        assert args.rig.endswith("rig-sim.yaml") and args.no_live_monitor_browser
        assert (args.params is not None) == (mode != "measure")
        if Mode(mode).runs_trials:
            assert args.sub == "s01" and args.ses == 2 and args.initials == "HD"
        if mode in {"test", "simulate"}:
            assert args.trials_per_condition == 4
        assert args.headless == (mode == "simulate") and args.mouse == (mode == "test")
        if mode == "demo":
            assert args.screenshots.endswith("media")
        if mode == "movie":
            assert args.out.endswith("media") and args.scale == 0.25
            assert args.clip == ["one", "two"]
            assert (args.sheet is not None) == sheet and args.columns == (2 if sheet else None)

    def test_the_reserved_flags_are_exactly_the_ones_the_launcher_emits(
        self, workspace, monkeypatch
    ):
        """MODE_FLAGS is the refusal rule for the extra arguments, so it must
        be neither wider nor narrower than what _mode_command emits: a flag
        added there without joining the set could be contradicted from the
        text field; one dropped there but kept in the set would refuse an
        argument the form no longer owns. Every option on, across the six
        modes, is every flag the launcher can produce — for a project on the
        current alhazen and for one on a pre-1.9 alhazen, which is told
        `--no-dashboard-browser` because that is the spelling it knows."""
        emitted: set[str] = set()
        for version in ("1.8.0", "1.9.0"):
            # Training names a ladder stage, not parameters: its flags are
            # added below, from a project that registers a ladder.
            for mode in (m for m in Mode if m is not Mode.TRAINING):
                request = request_for(
                    workspace,
                    mode=mode.value,
                    subject="s01",
                    initials="HD",
                    # A typed seed: since 2.3.0 an empty field sends no
                    # --seed, so "every option on" has to include one.
                    seed=5,
                    parameters=None if mode is Mode.MEASURE else {"speed": 2},
                    headless=mode is Mode.SIMULATE,
                    mouse=mode is Mode.TEST,
                    windowed=True,
                    sheet=True,
                    columns=2,
                    clips=["one"],
                )
                monkeypatch.setitem(workspace.project(request.project), "alhazen_version", version)
                command = workspace._command(request, workspace.directory / "job")
                emitted.update(token for token in command if token.startswith("--"))
        # The calibration-target flags: emitted for run and test on a rig whose
        # tracker draws a target, by a project whose alhazen offers the choice.
        project = workspace.project(workspace.projects[0]["id"])
        monkeypatch.setitem(project, "calibration_targets", calibration_offer())
        eyelink_rig(Path(project["path"]))
        request = request_for(
            workspace,
            mode="test",
            subject="s01",
            initials="HD",
            rig="configs/rig-eyelink.yaml",
            calibration_target={
                "appearance": "images",
                "images": ["monkey_1", "food_3"],
                "motion": "pulse",
            },
        )
        command = workspace._command(request, workspace.directory / "job")
        emitted.update(token for token in command if token.startswith("--"))
        # The measurement flags: emitted for Measure rig by a project whose
        # alhazen lists its measurements.
        monkeypatch.setitem(
            project,
            "measurements",
            [
                {
                    "key": "monitor.refresh",
                    "group": "Monitor",
                    "title": "Refresh",
                    "order": 1,
                    "requires": [],
                    "subject": "none",
                }
            ],
        )
        request = request_for(workspace, mode="measure", measurements=["monitor.refresh"])
        command = workspace._command(request, workspace.directory / "job")
        emitted.update(token for token in command if token.startswith("--"))
        # The experimenter: emitted for a session whose project's alhazen
        # records one, from the people registry's record.
        request = request_for(workspace, mode="test", subject="s01", initials="HD")
        command = workspace._command(
            request, workspace.directory / "job", experimenter={"name": "Ana", "record_id": "e_1"}
        )
        emitted.update(token for token in command if token.startswith("--"))
        # The subject's age and sex: emitted for a session whose project's
        # alhazen records them, from the record or the typed fields.
        command = workspace._command(
            request, workspace.directory / "job", demographics={"age": "27", "sex": "female"}
        )
        emitted.update(token for token in command if token.startswith("--"))
        # The training flags: emitted for a Training launch by a project whose
        # run.py registers a ladder (tests/unit/test_workspace_training.py).
        emitted.update({"--ladder", "--stage"})
        assert emitted == MODE_FLAGS


class TestTheSeed:
    """The seed field defaulted to 0 and every launch sent --seed 0, so every
    session started from the workspace had the same trial order, jitters and
    (in amodal-averaging) block order. An empty field now sends no --seed, and
    the session draws a fresh seed and records it, as the command line does;
    a typed seed is still sent, to repeat a session. The run record keeps the
    seed each session used — the one passed, or the one it drew, read from its
    console — for the history to show."""

    @staticmethod
    def session_request(workspace, **overrides):
        return request_for(
            workspace, mode="simulate", subject="s01", parameters={"speed": 2}, **overrides
        )

    @staticmethod
    def record(run) -> dict:
        return json.loads((Path(run["directory"]) / "run.json").read_text(encoding="utf-8"))

    def test_an_empty_field_passes_no_seed_and_the_session_draws_its_own(self, workspace):
        request = self.session_request(workspace)
        assert request.seed is None
        command = workspace._command(request, workspace.directory / "job")
        assert "--seed" not in command
        # The runner's own parser then reads no seed, and the build draws one
        # (core.rng.resolve_seed), exactly as for a command typed without it.
        parser = argparse.ArgumentParser()
        add_mode_arguments(parser)
        assert parser.parse_args(command[3:]).seed is None

    def test_a_typed_seed_is_passed_zero_included(self, workspace):
        # 0 is a seed like any other once typed; only an empty field is none.
        command = workspace._command(self.session_request(workspace, seed=0), workspace.directory)
        assert command[command.index("--seed") + 1] == "0"

    def test_a_negative_seed_is_refused(self, workspace):
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="greater than or equal to 0"):
            self.session_request(workspace, seed=-1)

    def test_the_record_keeps_the_seed_passed(self, workspace):
        run = finish(workspace, workspace.start(self.session_request(workspace, seed=7)))
        assert run["seed"] == 7 and self.record(run)["seed"] == 7

    def test_a_drawn_seed_is_read_from_the_console_and_kept(self, workspace):
        from alhazen.cli.main import _seed_line

        # A run.py that prints what the command line prints before trial one.
        root = Path(workspace.projects[0]["path"])
        line = _seed_line(2718281828, drawn=True)
        (root / "run.py").write_text(f"print({line!r}, flush=True)\n", encoding="utf-8")

        run = finish(workspace, workspace.start(self.session_request(workspace)))

        assert run["command"].count("--seed") == 0
        assert run["seed"] == 2718281828
        # Kept in the record, so the history shows it without the console.
        assert self.record(run)["seed"] == 2718281828

    def test_while_the_session_runs_it_shows_as_soon_as_it_is_printed(self, workspace, monkeypatch):
        started, release = threading.Event(), threading.Event()

        class Process:
            pid = 123

            def wait(self, timeout=None):
                started.set()
                assert release.wait(timeout=5)
                return 0

            def poll(self):
                return 0 if release.is_set() else None

        monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Process())
        run = workspace.start(self.session_request(workspace))
        assert started.wait(timeout=5)
        console = Path(run["directory"]) / "console.log"
        assert workspace.state()["runs"][0]["seed"] is None  # not printed yet

        console.write_text("mode: simulate\nseed: 31 (drawn for this run; --seed 31 repeats it)\n")
        (listed,) = workspace.state()["runs"]
        assert listed["seed"] == 31
        assert workspace.detail(run["id"])["seed"] == 31
        # The record is written once, when the run ends, not on every poll.
        assert self.record(run)["seed"] is None

        release.set()
        assert self.record(finish(workspace, run))["seed"] == 31

    def test_a_session_that_never_says_stays_unknown(self, workspace):
        # The fixture's run.py prints only its argv: an alhazen from before
        # the seed line. The page shows that as "new".
        run = finish(workspace, workspace.start(self.session_request(workspace)))
        assert run["seed"] is None

    def test_a_mode_that_draws_no_seed_is_never_read_for_one(self, workspace):
        # A movie takes seed 0 when given none; whatever its console prints,
        # it drew nothing.
        root = Path(workspace.projects[0]["path"])
        (root / "run.py").write_text("print('seed: 99 (drawn for this run)')\n", encoding="utf-8")
        run = finish(workspace, workspace.start(request_for(workspace)))
        assert run["mode"] == "movie" and run["seed"] is None

    def test_a_record_from_before_the_field_reads_its_command(self, workspace):
        # 2.2 wrote no seed field, and always passed --seed: 0 unless typed.
        run = finish(workspace, workspace.start(self.session_request(workspace, seed=0)))
        path = Path(run["directory"]) / "run.json"
        record = self.record(run)
        del record["seed"]
        path.write_text(json.dumps(record), encoding="utf-8")

        restored = Workspace(workspace.directory)

        assert restored.detail(run["id"])["seed"] == 0
        assert restored.state()["runs"][0]["seed"] == 0

    def test_an_interrupted_session_keeps_the_seed_it_printed(self, workspace):
        run = finish(workspace, workspace.start(self.session_request(workspace)))
        path = Path(run["directory"]) / "run.json"
        record = self.record(run)
        # As a server that died mid-run leaves it: still "running", no seed.
        record.update(status="running", seed=None)
        path.write_text(json.dumps(record), encoding="utf-8")
        (Path(run["directory"]) / "console.log").write_text(
            "seed: 42 (drawn for this run; --seed 42 repeats it)\n", encoding="utf-8"
        )

        restored = Workspace(workspace.directory)

        detail = restored.detail(run["id"])
        assert (detail["status"], detail["seed"]) == ("interrupted", 42)
        assert json.loads(path.read_text(encoding="utf-8"))["seed"] == 42


class TestTheSeedLine:
    """The contract between the two ends: the line cli/main.py prints before
    trial one, and what the workspace reads from a launched run's console."""

    @pytest.mark.parametrize("seed, drawn", [(2718281828, True), (0, False), (7, True)])
    def test_the_workspace_reads_the_line_the_command_line_prints(self, tmp_path, seed, drawn):
        from alhazen.cli.main import _seed_line
        from alhazen.cli.workspace import console_seed

        console = tmp_path / "console.log"
        # Among the lines printed before it, on a Windows console's line ends.
        console.write_bytes(
            (
                "mode: simulate — the whole session\r\nautopilot: seed=5\r\n"
                f"running demo: sub-s01 ses-001 run-01\r\n{_seed_line(seed, drawn=drawn)}\r\n"
            ).encode()
        )
        assert console_seed(console) == seed

    def test_only_a_line_that_starts_with_it_counts(self, tmp_path):
        from alhazen.cli.workspace import console_seed

        console = tmp_path / "console.log"
        console.write_text("an experiment's own note: seed: 5\n", encoding="utf-8")
        assert console_seed(console) is None

    def test_no_console_no_seed(self, tmp_path):
        from alhazen.cli.workspace import console_seed

        assert console_seed(tmp_path / "missing.log") is None

    def test_only_the_start_of_a_console_is_searched(self, tmp_path):
        # The line comes before trial one; a console that never printed it is
        # not read whole on every poll.
        from alhazen.cli.workspace import SEED_SEARCH_BYTES, console_seed

        console = tmp_path / "console.log"
        console.write_text("x" * SEED_SEARCH_BYTES + "\nseed: 5 (drawn)\n", encoding="utf-8")
        assert console_seed(console) is None

    def test_a_recorded_command_is_read_for_the_seed_it_passed(self):
        from alhazen.cli.workspace import seed_argument

        assert seed_argument(["python", "run.py", "--mode", "run", "--seed", "0"]) == 0
        assert seed_argument(["python", "run.py", "--mode", "run"]) is None
        # A value that is not a seed is not read as one.
        assert seed_argument(["python", "-m", "pkg.preview", "--seed", "abc"]) is None


class TestTheNoBrowserFlag:
    """The launcher tells a session not to open its own browser tab (the page
    embeds the monitor). The flag was renamed in alhazen 1.9, and the child
    runs the project's alhazen, not the workspace's — so the spelling follows
    the version registration recorded."""

    @pytest.mark.parametrize("version", ["1.9.0", "1.10.2", "2.0.0", "1.9.0rc1"])
    def test_a_project_on_a_recent_alhazen_gets_the_current_spelling(self, version):
        assert no_browser_flag(version) == "--no-live-monitor-browser"

    @pytest.mark.parametrize("version", ["1.8.0", "1.7.0", "0.9.0"])
    def test_a_project_on_an_older_alhazen_gets_the_spelling_it_knows(self, version):
        assert no_browser_flag(version) == "--no-dashboard-browser"

    @pytest.mark.parametrize("version", [None, "", "unknown"])
    def test_a_record_with_no_readable_version_is_refused_not_guessed(self, version):
        # Guessing would launch a child that dies on argparse in its console;
        # re-registering re-probes the interpreter and records the version.
        with pytest.raises(ValueError, match="register it again"):
            no_browser_flag(version)


class TestInitials:
    """The subject's initials (alhazen 2.0), beside the subject ID: required
    for run and test, held to the command line's rule in its words, passed
    as --initials, and recorded on the run for the history."""

    @pytest.mark.parametrize("mode", ["run", "test"])
    def test_run_and_test_need_them_before_a_run_is_made(self, workspace, mode):
        request = request_for(workspace, mode=mode, subject="s01", parameters={"speed": 2})
        with pytest.raises(ValueError, match="Subject initials are required for run and test"):
            workspace.start(request)
        assert workspace.runs == {}

    def test_ones_that_break_the_rule_are_refused_in_the_command_lines_words(self, workspace):
        request = request_for(
            workspace, mode="run", subject="s01", initials="H.D.", parameters={"speed": 2}
        )
        with pytest.raises(ValueError) as refused:
            workspace.start(request)
        assert str(refused.value) == f"{INITIALS_RULE}; got 'H.D.'"
        assert workspace.runs == {}

    def test_simulate_needs_none_and_passes_none(self, workspace):
        request = request_for(workspace, mode="simulate", subject="s01", parameters={"speed": 2})
        command = workspace._command(request, workspace.directory / "job")
        assert "--initials" not in command

    @pytest.mark.parametrize("mode", ["demo", "movie", "measure"])
    def test_a_mode_without_a_subject_ignores_what_the_form_holds(self, workspace, mode):
        # The page hides the field there; a stale value must neither refuse
        # the launch nor reach run.py.
        request = request_for(workspace, mode=mode, initials="1234567")
        command = workspace._command(request, workspace.directory / "job")
        assert "--initials" not in command

    def test_the_run_record_says_who_the_run_was_for(self, workspace):
        run = finish(
            workspace,
            workspace.start(
                request_for(
                    workspace,
                    mode="run",
                    subject="s01",
                    initials=" hd ",
                    session=3,
                    parameters={"speed": 2},
                )
            ),
        )
        assert (run["subject"], run["session"], run["initials"]) == ("s01", 3, "HD")
        assert run["command"][run["command"].index("--initials") + 1] == "HD"
        # And the record on disk, which the history is read from after a restart.
        restored = Workspace(workspace.directory).detail(run["id"])
        assert (restored["subject"], restored["initials"]) == ("s01", "HD")

    def test_a_launch_that_names_no_subject_records_none(self, workspace):
        run = finish(workspace, workspace.start(request_for(workspace)))  # a movie
        assert "subject" not in run and "initials" not in run

    def test_the_page_refuses_in_the_same_words(self):
        # workspace.js checks the field before sending; its sentence must be
        # the one the command line and this server use.
        script = (Path(workspace_module.__file__).parent / "assets" / "workspace.js").read_text(
            encoding="utf-8"
        )
        assert f"const INITIALS_RULE = '{INITIALS_RULE}';" in script
        assert "'Subject initials are required for run and test modes'" in script


class TestExtraArguments:
    """Free-form run.py arguments ride at the end of a mode's command, exactly
    as they do for a standalone script. Without them an experiment that ships
    several tasks cannot be launched at all: its run.py needs `--task <name>`
    (and exits with a usage error without it) before it hands the rest to
    run_experiment. What the extras may not do is contradict the form, whose
    settings are what the run record and its history show.
    """

    def test_a_modes_extras_follow_the_launchers_flags(self, workspace):
        request = request_for(
            workspace, mode="simulate", subject="s01", extra_args="--task mib-detect"
        )
        command = workspace._command(request, workspace.directory / "job")
        assert command[3:5] == ["--mode", "simulate"]
        assert command[-2:] == ["--task", "mib-detect"]

    def test_the_runners_flags_the_form_lacks_pass_through(self, workspace):
        """--curriculum and --run are add_mode_arguments' own, and the launcher
        never sets them; a shaping curriculum is exactly what the field is for."""
        request = request_for(
            workspace,
            mode="run",
            subject="s01",
            initials="HD",
            extra_args="--curriculum configs/shaping.yaml --run 3",
        )
        command = workspace._command(request, workspace.directory / "job")
        assert command[-4:] == ["--curriculum", "configs/shaping.yaml", "--run", "3"]

    @pytest.mark.parametrize(
        "extra, flag",
        [
            ("--seed 5", "--seed"),
            ("--sub=x", "--sub"),
            ("--task mib-detect --headless", "--headless"),
            ("--mode run", "--mode"),
            ("--initials XY", "--initials"),
        ],
    )
    def test_a_flag_the_form_sets_is_refused_by_name(self, workspace, extra, flag):
        with pytest.raises(ValueError, match=re.escape(flag) + " is set from the dashboard"):
            workspace._command(
                request_for(workspace, mode="simulate", subject="s01", extra_args=extra),
                workspace.directory / "job",
            )
        assert workspace.runs == {}

    def test_a_quoting_error_names_the_field(self, workspace):
        """shlex's own message is "No closing quotation": true, and silent about
        which of the form's fields it means."""
        with pytest.raises(ValueError, match="extra arguments.*No closing quotation"):
            workspace._command(
                request_for(workspace, mode="simulate", extra_args='--task "mib'),
                workspace.directory / "job",
            )

    def test_a_launched_run_receives_and_records_its_extras(self, workspace):
        """End to end: the child's argv ends with the extras (the stub run.py
        prints its argv), and the run record's command shows them, so the
        history says which task a run was."""
        run = finish(
            workspace,
            workspace.start(request_for(workspace, extra_args="--task mib-detect --run 2")),
        )
        assert run["status"] == "completed"
        assert run["command"][-4:] == ["--task", "mib-detect", "--run", "2"]
        assert json.loads(run["log"].splitlines()[0])[-4:] == ["--task", "mib-detect", "--run", "2"]


class TestBoundaries:
    def test_only_one_server_can_recover_and_use_a_workspace(self, tmp_path):
        with (
            workspace_lock(tmp_path),
            pytest.raises(ValueError, match="already has this workspace"),
            workspace_lock(tmp_path),
        ):
            pytest.fail("two servers acquired the same workspace")
        with workspace_lock(tmp_path):
            pass

    @pytest.mark.parametrize(
        "content", ["[]", "speed: [", "speed: .nan", "day: 2026-01-01", "x: &x [*x]", "1: value"]
    )
    def test_parameter_errors_are_explicit(self, content):
        with pytest.raises(ValueError):
            parse_parameters(content)

    def test_traversal_and_symlink_media(self, workspace, tmp_path):
        with pytest.raises(ValueError, match="inside"):
            path_inside(tmp_path, "../elsewhere")
        run = finish(workspace, workspace.start(request_for(workspace)))
        secret = tmp_path / "secret.png"
        secret.write_bytes(b"private")
        root = Path(run["directory"]) / "media"
        try:
            (root / "escape.png").symlink_to(secret)
        except OSError as exc:
            # Windows refuses symlinks to an account without the privilege
            # (ERROR_PRIVILEGE_NOT_HELD, 1314) unless Developer Mode is on. CI
            # runners have it, so the escape check runs there; skipping on
            # the platform alone would have hidden this test from Windows
            # entirely. Any other error is a real one.
            if getattr(exc, "winerror", None) != 1314:
                raise
            pytest.skip("symlink creation needs a privilege this account lacks")
        assert [a["path"] for a in workspace.detail(run["id"])["artifacts"]] == ["clip.mp4"]
        with pytest.raises(ValueError, match="inside"):
            path_inside(root, "escape.png")


@pytest.fixture
def http(workspace):
    server = DashboardServer(workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(path, body=None, headers=None, method=None):
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        merged = {"X-Alhazen-Token": server.token, **(headers or {})}
        if body is not None:
            merged["Content-Type"] = "application/json"
        conn.request(
            method or ("POST" if body is not None else "GET"),
            path,
            json.dumps(body) if body is not None else None,
            merged,
        )
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    yield request, server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


class TestHTTP:
    def test_page_state_and_yaml(self, http, workspace):
        call, _ = http
        status, headers, page = call("/")
        assert status == 200 and b"Configure a run" in page
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        # The page frames the live session monitor, which runs on another
        # loopback port; nothing else may be framed.
        assert "frame-src http://127.0.0.1:*" in headers["Content-Security-Policy"]
        assert call("/workspace.js")[0] == 200
        assert call("/workspace.css")[0] == 200
        assert json.loads(call("/api/state")[2])["projects"][0]["name"] == "experiment with spaces"
        assert json.loads(call("/api/parameters", {"text": "speed: 2"})[2])["values"] == {
            "speed": 2
        }
        assert call("/api/parameters", {"text": 2})[0] == 400
        assert call("/no-such-file")[0] == 404
        key = workspace.projects[0]["id"]
        assert call(f"/api/config?project={key}&path=configs/task.yaml")[0] == 200
        assert call(f"/api/config?project={key}&path=../../secret.yaml")[0] == 400

    def test_the_page_font_and_favicon_are_served_from_the_package(self, http):
        """The workspace ships its own font (the page may load nothing from
        outside, and a rig may have no internet) and its logo as the icon."""
        call, _ = http
        status, headers, font = call("/fonts/Manrope-latin.woff2")
        assert status == 200 and headers["Content-Type"] == "font/woff2"
        assert font[:4] == b"wOF2"
        # The CSP names fonts explicitly: from this server only.
        assert "font-src 'self'" in headers["Content-Security-Policy"]
        status, headers, icon = call("/favicon.svg")
        assert status == 200 and headers["Content-Type"] == "image/svg+xml"
        assert icon.startswith(b"<svg")
        # An image must be well-formed XML, or the browser shows no icon at
        # all and says nothing (a "--" inside an XML comment is enough). The
        # mark is the Penrose A as two paths (ink, face), not text.
        root = ElementTree.fromstring(icon)
        svg = "{http://www.w3.org/2000/svg}"
        assert [p.get("class") for p in root.iter(f"{svg}path")] == ["mark-ink", "mark-face"]
        assert root.find(f".//{svg}text") is None
        # The sidebar's page icons are served the same way.
        for name in ("experiments", "general", "run", "data", "history"):
            status, headers, body = call(f"/icon-{name}.svg")
            assert status == 200 and headers["Content-Type"] == "image/svg+xml"
            ElementTree.fromstring(body)
        # Only the named files: nothing else under assets/ by URL.
        assert call("/fonts/OFL.txt")[0] == 404
        assert call("/fonts/../workspace.css")[0] in {400, 404}

    def test_the_font_and_its_licence_are_package_data(self):
        """A file left out of package-data installs fine and then 404s, and
        the OFL requires the licence to travel with the font."""
        try:
            import tomllib
        except ModuleNotFoundError:  # 3.10
            import tomli as tomllib
        root = Path(__file__).parents[2]
        config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        patterns = config["tool"]["setuptools"]["package-data"]["alhazen"]
        # setuptools globs like pathlib: `*` does not cross a `/`, so the fonts
        # folder needs a pattern of its own (fnmatch would wrongly say
        # cli/assets/* covers it).
        package = root / "src" / "alhazen"
        fonts = package / "cli" / "assets" / "fonts"
        assert sorted(p.name for p in fonts.iterdir()) == ["Manrope-latin.woff2", "OFL.txt"]
        for path in fonts.iterdir():
            relative = path.relative_to(package)
            assert any(relative.match(pattern) for pattern in patterns), relative
        assert "SIL Open Font License" in (fonts / "OFL.txt").read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "headers",
        [
            {"X-Alhazen-Token": "wrong"},
            {"Origin": "https://other.example"},
            {"Host": "attacker.example"},
        ],
    )
    def test_auth_host_and_origin(self, http, headers):
        call, _ = http
        assert call("/api/state", headers=headers)[0] == 403
        assert call("/api/runs", {}, headers=headers)[0] == 403

    def test_encoded_api_paths_cannot_bypass_authentication(self, http):
        call, _ = http
        assert call("/%61pi/state", headers={"X-Alhazen-Token": ""})[0] == 403
        assert call("/%61pi/runs", {}, headers={"X-Alhazen-Token": ""})[0] == 403

    def test_invalid_requests(self, http):
        call, _ = http
        assert call("/api/state?a=1&a=2")[0] == 400
        assert call("/api/runs", {"mode": "movie"})[0] == 400
        assert call("/api/projects", {"path": []})[0] == 400
        assert call("/api/projects", [1, 2])[0] == 400
        assert call("/api/stop", headers={"Content-Length": "-1"}, method="POST")[0] == 413
        assert call("/api/runs/missing")[0] == 400

    def test_launch_and_video_ranges(self, http, workspace):
        call, server = http
        status, _, body = call("/api/runs", request_for(workspace).model_dump())
        assert status == 201
        run = finish(workspace, json.loads(body))
        url = f"/media/{run['id']}/clip.mp4?token={server.token}"
        status, headers, data = call(url, headers={"Range": "bytes=2-5"})
        assert status == 206 and data == b"2345"
        assert headers["Content-Range"] == "bytes 2-5/10"
        assert headers["Content-Type"] == "video/mp4"
        assert call(url, headers={"Range": "bytes=-3"})[2] == b"789"
        assert call(url, headers={"Range": "bytes=6-"})[2] == b"6789"
        assert call(url, headers={"Range": "bytes=100-"})[0] == 416
        assert call(url, headers={"Range": "invalid"})[0] == 416
        assert call(url)[2] == b"0123456789"
        assert call(f"/media/{run['id']}/../params.yaml")[0] == 400
        assert call(f"/media/{run['id']}/missing.png")[0] == 404
        assert call("/media/missing/clip.mp4")[0] == 404

    def test_a_wrong_host_says_which_url_to_open(self, http):
        """The likely cause is someone typing localhost:PORT; the refusal has
        to name the URL that works, not just say "Invalid host"."""
        call, server = http
        status, _, body = call("/api/state", headers={"Host": "attacker.example"})
        assert status == 403
        assert f"http://127.0.0.1:{server.server_port}" in json.loads(body)["error"]

    def test_a_stalled_client_cannot_hold_a_handler_forever(self, http, monkeypatch):
        """A body shorter than its Content-Length used to block rfile.read for
        as long as the client stayed connected, so one stalled tab could pin
        a handler thread for good. The handler's socket timeout ends the read
        and answers 408 with the reason; the class-level value is the contract,
        shortened here only so the test is quick."""
        assert Handler.timeout == 30
        call, server = http
        monkeypatch.setattr(Handler, "timeout", 0.5)
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=10) as sock:
            sock.sendall(
                f"POST /api/parameters HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n"
                f"X-Alhazen-Token: {server.token}\r\nContent-Type: application/json\r\n"
                'Content-Length: 100\r\n\r\n{"text":'.encode()
            )
            # Headers and JSON body arrive in separate segments; the server
            # closes the connection after the response, so read to EOF.
            response = b"".join(iter(lambda: sock.recv(65536), b""))
        assert response.split(b"\r\n")[0].endswith(b" 408 Request Timeout"), response
        assert b"did not arrive" in response
        # And the server is still serving.
        assert call("/api/state")[0] == 200


class TestAClientThatWentAway:
    """A tab closed mid-response makes the next write fail. On Windows that is
    ConnectionAbortedError (WinError 10053), which the handlers used not to
    catch: it fell into the OSError branch, which wrote a 400 to the dead
    socket, raised again, and socketserver printed the traceback into the
    owner's dashboard console. Now nothing more is written and nothing is
    printed, for either method, and whether the failed write was the answer
    or a refusal."""

    @pytest.fixture
    def dead_socket(self, http, monkeypatch):
        """Every write of a JSON answer fails as a write to an aborted socket
        does; the writes attempted and the errors that escaped a handler are
        recorded."""
        call, server = http
        writes, escaped = [], []

        def write(self, payload, status=200):
            writes.append(status)
            raise ConnectionAbortedError(10053, "An established connection was aborted")

        monkeypatch.setattr(Handler, "_json", write)
        # socketserver's hook for an exception out of a handler: what printed
        # the traceback. Recorded instead, so the test sees it.
        monkeypatch.setattr(
            server, "handle_error", lambda request, address: escaped.append(sys.exc_info()[1])
        )

        def send(raw):
            with socket.create_connection(("127.0.0.1", server.server_port), timeout=10) as sock:
                sock.sendall(raw.replace(b"PORT", str(server.server_port).encode()))
                # The server closes without answering; read to EOF.
                return b"".join(iter(lambda: sock.recv(65536), b""))

        token = server.token.encode()
        return send, token, writes, escaped

    @pytest.mark.parametrize(
        ("request_line", "body", "first_status"),
        [
            # The answer's own write fails.
            (b"GET /api/state", b"", 200),
            # A refusal's write fails (an unknown run is a ValueError: 400).
            (b"GET /api/runs/no-such-run", b"", 400),
            (b"POST /api/parameters", b'{"text": "speed: 2"}', 200),
            (b"POST /api/parameters", b'{"text": 2}', 400),
        ],
    )
    def test_nothing_more_is_written_and_nothing_escapes(
        self, dead_socket, request_line, body, first_status
    ):
        send, token, writes, escaped = dead_socket
        raw = (
            request_line
            + b" HTTP/1.1\r\nHost: 127.0.0.1:PORT\r\nX-Alhazen-Token: "
            + token
            + b"\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
        )
        assert send(raw) == b""
        # One attempt: the failed write was not followed by a refusal
        # written to the same dead socket.
        assert writes == [first_status]
        assert escaped == []


class TestInterpreters:
    def test_children_see_the_project_first_and_nothing_of_the_launcher(
        self, workspace, monkeypatch
    ):
        """From an installed wheel the launcher's root is its whole
        site-packages; put first on the *project's* interpreter's path it
        shadowed the project's pinned alhazen and every package beside it.
        Both children — the launch and the schema probe — must get the
        project's src/, its root, then whatever PYTHONPATH was inherited, and
        nothing else."""
        monkeypatch.setenv("PYTHONPATH", "INHERITED")
        root = workspace.projects[0]["path"]
        expected = os.pathsep.join([str(Path(root) / "src"), root, "INHERITED"])
        launcher_root = str(Path(workspace_module.__file__).resolve().parents[2])
        seen = {}

        class Process:
            pid = 123

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

        def popen(command, **kwargs):
            seen["launch"] = kwargs["env"]["PYTHONPATH"]
            return Process()

        def run(command, **kwargs):
            seen["schema"] = kwargs["env"]["PYTHONPATH"]
            return subprocess.CompletedProcess(command, 0, stdout='{"properties": {}}', stderr="")

        monkeypatch.setattr(subprocess, "Popen", popen)
        monkeypatch.setattr(subprocess, "run", run)
        finish(workspace, workspace.start(request_for(workspace)))
        workspace.schema(workspace.projects[0]["id"])
        assert seen == {"launch": expected, "schema": expected}
        assert launcher_root not in expected

    def test_an_inherited_launcher_checkout_does_not_reach_another_interpreter(
        self, tmp_path, monkeypatch
    ):
        """A dashboard started from a source checkout with PYTHONPATH=<checkout>/src
        passed that entry on, so an experiment's own venv ran the launcher's
        alhazen instead of its pinned one (found importing amodal-averaging into
        the hub). Dropped for another interpreter; kept for the launcher's own;
        every other inherited entry kept, in order."""
        launcher = str(workspace_module._launcher_root())
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join(["FIRST", launcher, "LAST"]))
        root = tmp_path / "exp"
        other = tmp_path / "venv" / "bin" / "python"
        other.parent.mkdir(parents=True)
        other.write_text("", encoding="utf-8")
        base = [str(root / "src"), str(root)]
        env = workspace_module._child_env({"path": str(root), "python": str(other)})
        assert env["PYTHONPATH"].split(os.pathsep) == [*base, "FIRST", "LAST"]
        env = workspace_module._child_env({"path": str(root), "python": sys.executable})
        assert env["PYTHONPATH"].split(os.pathsep) == [*base, "FIRST", launcher, "LAST"]
        # No interpreter recorded (an old registry entry): treated as another one.
        env = workspace_module._child_env({"path": str(root)})
        assert launcher not in env["PYTHONPATH"].split(os.pathsep)

    def test_a_virtual_environments_python_is_another_interpreter(self, tmp_path, monkeypatch):
        """A virtual environment's python is a symlink to the base interpreter:
        still another interpreter, with its own site-packages, so it loses
        the launcher's entry like any other. Its own test: making the symlink
        needs a privilege many Windows accounts lack, and as part of the test
        above it took that test's other cases down with it."""
        launcher = str(workspace_module._launcher_root())
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join(["FIRST", launcher, "LAST"]))
        root = tmp_path / "exp"
        venv_python = tmp_path / "venv2" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        try:
            venv_python.symlink_to(Path(sys.executable).resolve())
        except OSError as exc:
            # ERROR_PRIVILEGE_NOT_HELD (1314), as in
            # test_traversal_and_symlink_media; CI runners have the privilege,
            # so this runs there. Any other error is a real one.
            if getattr(exc, "winerror", None) != 1314:
                raise
            pytest.skip("symlink creation needs a privilege this account lacks")
        env = workspace_module._child_env({"path": str(root), "python": str(venv_python)})
        assert env["PYTHONPATH"].split(os.pathsep) == [
            str(root / "src"),
            str(root),
            "FIRST",
            "LAST",
        ]

    def test_another_interpreter_loses_exactly_the_launcher_entry(self, tmp_path, monkeypatch):
        """A dashboard started from a source checkout (PYTHONPATH=src) put
        that src/ on every project interpreter's path, so a hub install whose
        own env pins alhazen ran the launcher's alhazen while the probe
        reported the env's version (found importing kde-vergence). Another
        interpreter loses exactly that entry and keeps the rest; the
        launcher's own interpreter keeps it, since it imports alhazen there.

        Integration (fix/import-round): the kde-vergence and amodal-averaging
        fixes disagreed on a project with no recorded interpreter; the
        amodal-averaging rule is kept (an unknown interpreter is treated as
        another one, so it never silently runs the launcher's alhazen)."""
        launcher_root = str(Path(workspace_module.__file__).resolve().parents[2])
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join(["BEFORE", launcher_root, "AFTER"]))
        root = tmp_path / "project"
        other = str(tmp_path / "venv" / "bin" / "python")
        env = workspace_module._child_env({"path": str(root), "python": other})
        assert env["PYTHONPATH"].split(os.pathsep) == [
            str(root / "src"),
            str(root),
            "BEFORE",
            "AFTER",
        ]
        own = workspace_module._child_env({"path": str(root), "python": sys.executable})
        assert launcher_root in own["PYTHONPATH"].split(os.pathsep)
        unnamed = workspace_module._child_env({"path": str(root)})
        assert launcher_root not in unnamed["PYTHONPATH"].split(os.pathsep)
        assert unnamed["PYTHONPATH"].split(os.pathsep)[-2:] == ["BEFORE", "AFTER"]

    def test_the_probe_sees_the_environment_the_launch_gets(self, tmp_path, monkeypatch):
        launcher = str(workspace_module._launcher_root())
        monkeypatch.setenv("PYTHONPATH", launcher)
        seen = {}

        def run(command, **kwargs):
            seen["path"] = kwargs["env"]["PYTHONPATH"]
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="no alhazen")

        monkeypatch.setattr(subprocess, "run", run)
        other = tmp_path / "python"
        with pytest.raises(ValueError):
            workspace_module.probe_interpreter(str(other), str(tmp_path))
        assert launcher not in seen["path"].split(os.pathsep)

    def test_registration_records_which_alhazen_the_interpreter_has(self, workspace, monkeypatch):
        monkeypatch.setattr(workspace_module, "probe_interpreter", REAL_PROBE)
        project = workspace.add(workspace.projects[0]["path"], sys.executable)
        assert project["alhazen_version"] == alhazen.__version__
        assert project["python_version"] == sys.version
        # The shared rigs THIS interpreter's alhazen ships, by absolute file:
        # the ones the Rig menu offers and a launch merges `extends` over.
        from alhazen.config.rigs import shared_rig_files

        assert project["shared_rigs"] == [
            {"name": name, "path": str(path.resolve())} for name, path in shared_rig_files().items()
        ]
        restored = Workspace(workspace.directory)
        assert restored.projects[0]["alhazen_version"] == alhazen.__version__

    def test_registration_records_whether_the_interpreter_has_psychopy(
        self, workspace, monkeypatch
    ):
        """The page warns before a launch that would open a PsychoPy window
        with an interpreter that has none. The probe looks the package up
        without importing it; this interpreter's truth is the expectation,
        so the test holds whether or not PsychoPy is installed here."""
        import importlib.metadata
        import importlib.util

        monkeypatch.setattr(workspace_module, "probe_interpreter", REAL_PROBE)
        project = workspace.add(workspace.projects[0]["path"], sys.executable)
        expected = (
            None
            if importlib.util.find_spec("psychopy") is None
            else importlib.metadata.version("psychopy")
        )
        assert project["psychopy_version"] == expected
        assert Workspace(workspace.directory).projects[0]["psychopy_version"] == expected

    def test_a_psychopy_is_found_by_its_installed_version_without_being_imported(
        self, workspace, monkeypatch
    ):
        """A psychopy package on the child's path, with the metadata pip
        writes beside it: its version is read from that metadata. The
        package raises if imported, so a probe that imported it would fail
        the registration."""
        monkeypatch.setattr(workspace_module, "probe_interpreter", REAL_PROBE)
        root = Path(workspace.projects[0]["path"])
        # The project's src/ is first on the child's path (_child_env).
        (root / "src/psychopy").mkdir(parents=True)
        (root / "src/psychopy/__init__.py").write_text(
            "raise RuntimeError('the probe must not import psychopy')\n"
        )
        (root / "src/psychopy-2099.1.0.dist-info").mkdir()
        (root / "src/psychopy-2099.1.0.dist-info/METADATA").write_text(
            "Metadata-Version: 2.1\nName: psychopy\nVersion: 2099.1.0\n"
        )
        project = workspace.add(str(root), sys.executable)
        assert project["psychopy_version"] == "2099.1.0"

    def test_an_interpreter_without_alhazen_is_refused_at_registration(
        self, workspace, monkeypatch
    ):
        """The wrong env used to register fine and die at the first launch's
        `import alhazen`. The refusal names the interpreter and the package to
        install, and leaves the registry as it was."""
        monkeypatch.setattr(workspace_module, "probe_interpreter", REAL_PROBE)
        root = Path(workspace.projects[0]["path"])
        # Shadow alhazen on the child's path with one that cannot import: the
        # project's own src/ comes first, so this is what the child sees.
        (root / "src/alhazen").mkdir(parents=True)
        (root / "src/alhazen/__init__.py").write_text("raise ImportError('not installed here')\n")
        before = [dict(p) for p in workspace.projects]
        with pytest.raises(ValueError, match=re.escape(sys.executable)) as refused:
            workspace.add(str(root), sys.executable)
        assert "alhazen-vision" in str(refused.value)
        assert "not installed here" in str(refused.value)
        assert workspace.projects == before

    def test_a_file_that_is_not_an_interpreter_is_refused(self, workspace, monkeypatch, tmp_path):
        monkeypatch.setattr(workspace_module, "probe_interpreter", REAL_PROBE)
        bogus = tmp_path / "not-python.txt"
        bogus.write_text("not an executable")
        with pytest.raises(ValueError, match="Cannot run the Python interpreter"):
            workspace.add(workspace.projects[0]["path"], str(bogus))


class TestParameterSchema:
    def test_schema_uses_project_interpreter_without_launching(self, workspace):
        root = Path(workspace.projects[0]["path"])
        (root / "run.py").write_text(
            "from pydantic import BaseModel\nfrom typing import Literal\n"
            "class Params(BaseModel):\n"
            '    motion: Literal["static", "moving"] = "static"\n'
            '    stimuli: tuple[str, ...] = ("bars", "kanizsa")\n'
            "class ExampleTask:\n    params_model = Params\n"
            'if __name__ == "__main__":\n'
            '    raise RuntimeError("must not launch")\n'
            "    run_experiment(task_class=ExampleTask)\n",
            encoding="utf-8",
        )
        schema = workspace.schema(workspace.projects[0]["id"])
        assert schema["properties"]["motion"]["enum"] == ["static", "moving"]
        assert schema["properties"]["stimuli"]["default"] == ["bars", "kanizsa"]
        assert not workspace.runs

    def test_missing_schema_is_an_explicit_error(self, workspace):
        with pytest.raises(ValueError, match="Cannot read task parameter choices"):
            workspace.schema(workspace.projects[0]["id"])

    def test_schema_is_cached_until_run_py_or_the_interpreter_changes(self, workspace, monkeypatch):
        """Reading the schema imports the task in a child interpreter — seconds
        each time — and the UI asks for it on every project switch. One read
        per (run.py, interpreter) is enough; editing run.py or choosing another
        interpreter must read again, because either can change the choices."""
        spawned = []

        def run(command, **kwargs):
            spawned.append(command[0])
            schema = {"properties": {"read": len(spawned)}}
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(schema), stderr="")

        monkeypatch.setattr(subprocess, "run", run)
        key = workspace.projects[0]["id"]
        assert workspace.schema(key)["properties"]["read"] == 1
        assert workspace.schema(key)["properties"]["read"] == 1 and len(spawned) == 1
        run_py = Path(workspace.projects[0]["path"]) / "run.py"
        stat = run_py.stat()
        os.utime(run_py, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        assert workspace.schema(key)["properties"]["read"] == 2
        other = str(Path(sys.executable).with_name("other-python"))
        workspace.projects[0]["python"] = other
        assert workspace.schema(key)["properties"]["read"] == 3 and spawned[-1] == other


class TestRecovery:
    def test_a_corrupt_registry_or_run_record_names_the_file(self, workspace):
        """A raw JSONDecodeError ("Expecting value: line 1 column 1") does not
        say which of the workspace's files it means; the person at the rig
        needs the path to fix or move."""
        registry = workspace.directory / "projects.json"
        registry.write_text("{not json")
        with pytest.raises(ValueError, match=re.escape(str(registry))):
            Workspace(workspace.directory)
        registry.write_text("[]")
        record = workspace.directory / "runs/broken/run.json"
        record.parent.mkdir()
        record.write_text("{not json")
        with pytest.raises(ValueError, match=re.escape(str(record))):
            Workspace(workspace.directory)
        record.write_text('{"id": "broken"}')
        with pytest.raises(ValueError, match=re.escape(str(record))):
            Workspace(workspace.directory)


class TestCalibrationTargetChoice:
    """The Rig section's calibration-target choice, as the launcher takes it:
    flags for run.py only when the project's alhazen offers the choice, for a
    rig whose tracker draws a target, in run or test; the pictures served to
    the page by manifest name only, never by a path the request spells."""

    @staticmethod
    def offered(workspace, monkeypatch):
        project = workspace.project(workspace.projects[0]["id"])
        monkeypatch.setitem(project, "calibration_targets", calibration_offer())
        eyelink_rig(Path(project["path"]))
        return project

    @staticmethod
    def launch(workspace, mode="test", **choice):
        return request_for(
            workspace,
            mode=mode,
            subject="s01",
            initials="HD",
            rig="configs/rig-eyelink.yaml",
            calibration_target=choice,
        )

    def test_the_choice_becomes_run_py_flags_after_the_launchers_own(self, workspace, monkeypatch):
        self.offered(workspace, monkeypatch)
        request = self.launch(
            workspace, appearance="random_images", images=["food_1", "food_2"], motion="pulse"
        )
        command = workspace._command(request, workspace.directory / "job")
        assert command[-6:] == [
            "--calibration-target",
            "random_images",
            "--calibration-images",
            "food_1,food_2",
            "--calibration-motion",
            "pulse",
        ]
        parser = argparse.ArgumentParser()
        add_mode_arguments(parser)
        args = parser.parse_args(command[3:])
        assert (args.calibration_target, args.calibration_images, args.calibration_motion) == (
            "random_images",
            "food_1,food_2",
            "pulse",
        )

    def test_no_choice_sends_no_flag(self, workspace, monkeypatch):
        self.offered(workspace, monkeypatch)
        request = request_for(
            workspace, mode="test", subject="s01", initials="HD", rig="configs/rig-eyelink.yaml"
        )
        command = workspace._command(request, workspace.directory / "job")
        assert not any(token.startswith("--calibration") for token in command)

    def test_a_project_on_an_alhazen_without_the_choice_is_told_to_update(self, workspace):
        eyelink_rig(Path(workspace.projects[0]["path"]))
        with pytest.raises(ValueError, match="has no calibration-target choice"):
            workspace._command(self.launch(workspace, motion="pulse"), workspace.directory)

    @pytest.mark.parametrize(
        "mode, choice, words",
        [
            ("simulate", {"motion": "pulse"}, "only run and test calibrate"),
            ("demo", {"motion": "pulse"}, "only run and test calibrate"),
            ("test", {"appearance": "images"}, "needs the pictures to show"),
            ("test", {"appearance": "images", "images": ["giraffe_9"]}, "giraffe_9: not among"),
        ],
    )
    def test_a_choice_the_launch_cannot_honour_is_refused_first(
        self, workspace, monkeypatch, mode, choice, words
    ):
        self.offered(workspace, monkeypatch)
        with pytest.raises(ValueError, match=words):
            workspace._command(self.launch(workspace, mode=mode, **choice), workspace.directory)

    def test_not_with_the_mouse_as_gaze(self, workspace, monkeypatch):
        self.offered(workspace, monkeypatch)
        request = self.launch(workspace, motion="pulse").model_copy(update={"mouse": True})
        with pytest.raises(ValueError, match="replaces the rig's eye tracker"):
            workspace._command(request, workspace.directory)

    def test_a_rig_whose_tracker_draws_no_target_refuses_it(self, workspace, monkeypatch):
        project = self.offered(workspace, monkeypatch)
        request = request_for(
            workspace,
            mode="test",
            subject="s01",
            initials="HD",
            calibration_target={"motion": "pulse"},
        )
        assert project["calibration_targets"]
        with pytest.raises(ValueError, match="this rig has no eye tracker"):
            workspace._command(request, workspace.directory)

    def test_a_script_never_takes_it(self, workspace, monkeypatch):
        self.offered(workspace, monkeypatch)
        request = self.launch(workspace, mode="preview-stimuli", motion="pulse")
        with pytest.raises(ValueError, match="a script never calibrates"):
            workspace._command(request, workspace.directory)

    def test_the_flags_cannot_be_typed_in_the_extra_arguments(self, workspace, monkeypatch):
        self.offered(workspace, monkeypatch)
        request = request_for(
            workspace,
            mode="test",
            subject="s01",
            initials="HD",
            rig="configs/rig-eyelink.yaml",
            extra_args="--calibration-motion=pulse",
        )
        with pytest.raises(ValueError, match="--calibration-motion is set from the dashboard"):
            workspace._command(request, workspace.directory)

    def test_pictures_are_served_by_listed_name_only(self, http, workspace, monkeypatch):
        call, server = http
        self.offered(workspace, monkeypatch)
        key = workspace.projects[0]["id"]
        status, headers, body = call(f"/calibration-picture?project={key}&name=monkey_1")
        assert status == 200 and headers["Content-Type"] == "image/png"
        assert body == image_path("monkey_1").read_bytes()
        for bad in ("../manifest", "monkey_1.png", "README", "giraffe_9", "%2e%2e%2fREADME"):
            assert call(f"/calibration-picture?project={key}&name={bad}")[0] == 404, bad
        # Like every other request, it needs the token.
        assert (
            call(
                f"/calibration-picture?project={key}&name=monkey_1",
                headers={"X-Alhazen-Token": "x"},
            )[0]
            == 403
        )
        assert call("/workspace_calibration.js")[0] == 200

    def test_a_project_without_the_offer_serves_no_pictures(self, http, workspace):
        call, _ = http
        key = workspace.projects[0]["id"]
        assert call(f"/calibration-picture?project={key}&name=monkey_1")[0] == 404

    def test_the_real_probe_reports_the_offer_of_the_projects_alhazen(self, tmp_path):
        root = tmp_path / "experiment"
        root.mkdir()
        report = REAL_PROBE(sys.executable, str(root))
        offer = report["calibration_targets"]
        assert offer["images"] == list(image_names())
        assert Path(offer["dir"]) == IMAGE_DIR.resolve()
        assert offer["defaults"]["appearance"] == "standard"
        assert offer["defaults"]["pulse"] == {"rate_hz": 1.0, "min_scale": 1.0, "max_scale": 1.4}

    def test_an_older_probe_answer_records_no_offer(self):
        from alhazen.cli.workspace import _calibration_offer

        assert _calibration_offer(None) is None
        with pytest.raises(ValueError, match="unexpected calibration picture names"):
            _calibration_offer({"dir": "/x", "images": ["../etc"], "defaults": {}})
