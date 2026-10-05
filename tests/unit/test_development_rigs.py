"""Run mode refuses a development rig before anything is written.

Every experiment's run.py starts on the shared laptop when a command names no
--rig, and run mode drives a rig exactly as written: a forgotten --rig used
to open a window, file a run under the real data root, register the subject
and get no gaze. A rig now says whether real data may be collected on it
(`real_data:`, docs/rigs.md §5), and these pin what a refusal leaves behind
(nothing at all), what it says (what to type instead), what it does not stop
(every other mode), and the one deliberate way round it (the experiment's
own rig file).
"""

from __future__ import annotations

import builtins
import importlib
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

from alhazen.cli.main import main
from alhazen.cli.modes import run_experiment
from alhazen.config.loader import load_rig
from alhazen.config.models import RigConfig
from alhazen.config.rigs import SHARED_RIG_DIR, RigRef, shared_rig_files
from alhazen.modes import REAL_DATA_DOCS, Mode, real_data_refusal
from support import MONITOR

# The command line's module itself, whose dispatch functions the tests spy on.
# Imported by name: `alhazen.cli.main` as an attribute is the `main` function.
cli_main = importlib.import_module("alhazen.cli.main")


def write_yaml(path: Path, body: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    return path


def tree(root: Path) -> dict[str, bytes | None]:
    """Every file and folder under ``root`` with each file's bytes: what
    "the data root is exactly as it was" is checked against."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


@pytest.fixture
def experiment(tmp_path, monkeypatch):
    """An experiment folder, the working directory, with a pyproject.toml,
    its own lab rig (extending the shared lab, as the lab's experiments do)
    and a booth of its own, and a task whose module lives in it — which is
    how alhazen finds the experiment's rigs from the task."""
    root = tmp_path / "rig-demo"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "rig-demo"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    write_yaml(root / "configs" / "rig-lab.yaml", {"extends": "lab"})
    write_yaml(
        root / "configs" / "rig-booth.yaml",
        {"monitor": dict(MONITOR_FIELDS), "display": {"backend": "simulated"}, "data_root": "data"},
    )
    module = root / "rig_demo_task.py"
    module.write_text(
        "from test_task_hooks import SilentTask\n\n"
        "class RigDemoTask(SilentTask):\n    name = 'rig-demo'\n",
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location("rig_demo_task", module)
    loaded = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "rig_demo_task", loaded)
    spec.loader.exec_module(loaded)
    # The shared rigs' data_root is "data", relative to where the command is
    # typed: the experiment folder, as in the lab.
    monkeypatch.chdir(root)
    return root, loaded.RigDemoTask


MONITOR_FIELDS = {
    "width_px": MONITOR.width_px,
    "height_px": MONITOR.height_px,
    "width_cm": MONITOR.width_cm,
    "distance_cm": MONITOR.distance_cm,
    "refresh_rate_hz": MONITOR.refresh_rate_hz,
}


@pytest.fixture
def nothing_else_may_happen(monkeypatch):
    """Make every later step of a session fail the test if it is reached: a
    prompt for the subject, the session builder. The params hook is the
    test's own spy (`hook`), since it is passed to run_experiment."""

    def no_prompt(prompt=""):
        raise AssertionError(f"the session asked for input: {prompt!r}")

    def no_build(**kwargs):
        raise AssertionError("build_session was reached")

    from alhazen.session import builder

    monkeypatch.setattr(builtins, "input", no_prompt)
    monkeypatch.setattr(builder, "build_session", no_build)


class Hook:
    """A params hook that records being run — the step that may load and
    save a subject's real state (an adaptive search)."""

    def __init__(self):
        self.calls = 0

    def __call__(self, params, args):
        self.calls += 1
        return params


class TestARealSessionOnADevelopmentRig:
    def test_a_forgotten_rig_on_the_shared_laptop_is_refused_before_anything(
        self, experiment, capsys, nothing_else_may_happen
    ):
        root, task = experiment
        before = tree(root)
        hook = Hook()

        code = run_experiment(
            task_class=task,
            default_rig="laptop",
            # A params file that does not exist: had the session got as far
            # as loading params, this would be INVALID and exit 1.
            default_params=str(root / "configs" / "missing-task.yaml"),
            params_hook=hook,
            argv=["--task", "rig-demo"],
        )

        assert code == 2
        err = capsys.readouterr().err
        assert err.startswith(
            "CANNOT RUN: run mode records real data, and alhazen/laptop (alhazen's shared rig) "
            "is a development rig: its settings say `real_data: false`. Nothing was started "
            "and nothing was written."
        )
        # Nothing written, nothing created — not even the data root — and
        # the params hook, which may load a subject's state, never ran.
        assert tree(root) == before
        assert not (root / "data").exists()
        assert hook.calls == 0

    def test_the_refusal_says_what_to_type_instead(
        self, experiment, capsys, nothing_else_may_happen
    ):
        root, task = experiment
        # A rig file the loader refuses is named as not considered, not
        # silently left out of the suggestion.
        broken = write_yaml(root / "configs" / "rig-broken.yaml", {"monitor": {}})

        run_experiment(task_class=task, default_rig="laptop", argv=["--task", "rig-demo"])

        lines = capsys.readouterr().err.rstrip("\n").split("\n")
        assert lines[1:] == [
            "  No --rig was given, so run.py started on its default rig.",
            # The experiment's own collecting rigs by the names --rig takes
            # (its lab, which extends the shared lab and hides it; its
            # booth, which says nothing and so collects), then the shared
            # vpixx. The laptop, the mac and the rehearsal are not offered.
            "  To record a subject, name the machine it sits at: --rig booth, --rig lab or "
            "--rig vpixx (the rigs here that collect real data). Not considered, because "
            f"they cannot be read: {broken}.",
            "  To try the session on this machine, use --mode test or --mode simulate; "
            "their data goes to the rehearsal root.",
            "  To record real data on this machine on purpose, give the experiment its own "
            "configs/rig-laptop.yaml saying `extends: laptop` and `real_data: true` "
            f"({REAL_DATA_DOCS}).",
        ]

    def test_a_rig_typed_on_the_command_line_is_not_called_forgotten(
        self, experiment, capsys, nothing_else_may_happen
    ):
        root, task = experiment
        code = run_experiment(
            task_class=task, default_rig="lab", argv=["--task", "rig-demo", "--rig", "laptop"]
        )
        assert code == 2
        err = capsys.readouterr().err
        assert "alhazen/laptop (alhazen's shared rig) is a development rig" in err
        assert "No --rig was given" not in err

    @pytest.mark.parametrize("spec", ["mac", "lab-rehearsal", "alhazen/laptop"])
    def test_every_shared_development_rig_is_refused(
        self, experiment, capsys, nothing_else_may_happen, spec
    ):
        root, task = experiment
        before = tree(root)
        code = run_experiment(task_class=task, default_rig=spec, argv=["--task", "rig-demo"])
        assert code == 2
        assert "is a development rig" in capsys.readouterr().err
        assert tree(root) == before

    def test_alhazen_run_refuses_it_the_same_way(
        self, experiment, capsys, monkeypatch, nothing_else_may_happen
    ):
        root, task = experiment
        before = tree(root)
        from alhazen.cli import tasks

        monkeypatch.setattr(tasks, "load_task_class", lambda name: task)
        code = main(
            ["run", "--task", "rig-demo", "--rig", "laptop", "--sub", "01", "--ses", "1"]
            + ["--initials", "AB"]
        )
        assert code == 2
        err = capsys.readouterr().err
        assert err.startswith("CANNOT RUN: run mode records real data, and alhazen/laptop")
        # `alhazen run` has no default rig: --rig was typed.
        assert "No --rig was given" not in err
        assert tree(root) == before

    def test_an_experiment_rig_that_extends_the_laptop_is_refused_naming_its_file(
        self, experiment, capsys, nothing_else_may_happen
    ):
        root, task = experiment
        own = write_yaml(root / "configs" / "rig-laptop.yaml", {"extends": "laptop"})
        before = tree(root)

        code = run_experiment(task_class=task, default_rig="laptop", argv=["--task", "rig-demo"])

        assert code == 2
        err = capsys.readouterr().err
        assert err.startswith(
            f"CANNOT RUN: run mode records real data, and laptop ({own}) is a development rig"
        )
        assert (
            f"To record real data on this machine on purpose, write `real_data: true` in {own}, "
            f"with a comment saying why ({REAL_DATA_DOCS})."
        ) in err
        assert tree(root) == before


class TestWhatStillRunsThere:
    @pytest.mark.parametrize(
        ("mode", "dispatch", "extra"),
        [
            ("test", "_trial_session", ["--sub", "dev", "--ses", "1", "--initials", "DEV"]),
            ("measure", "_measure_rig", []),
            ("demo", "_demo_task", []),
            ("movie", "_movie_task", []),
        ],
    )
    def test_every_mode_that_records_no_real_data_reaches_its_dispatch(
        self, experiment, monkeypatch, mode, dispatch, extra
    ):
        root, task = experiment
        reached = []
        monkeypatch.setattr(cli_main, dispatch, lambda *args: reached.append(mode) or 0)

        code = run_experiment(
            task_class=task,
            default_rig="laptop",
            argv=["--task", "rig-demo", "--mode", mode, *extra],
        )

        assert code == 0
        assert reached == [mode]

    def test_a_whole_simulation_runs_on_the_laptop_into_the_rehearsal_root(
        self, experiment, capsys
    ):
        root, task = experiment
        code = run_experiment(
            task_class=task,
            default_rig="laptop",
            # The laptop's live monitor would start a child process, no part
            # of what this checks.
            argv=["--task", "rig-demo", "--mode", "simulate", "--headless", "--no-live-monitor"],
        )
        assert code == 0, capsys.readouterr().err
        assert list((root / "data-rehearsal").rglob("config_snapshot.yaml"))
        assert not (root / "data").exists()


class TestTheDeliberateException:
    def test_the_experiments_own_laptop_that_says_so_runs_a_real_session(
        self, experiment, monkeypatch
    ):
        root, task = experiment
        write_yaml(root / "configs" / "rig-laptop.yaml", {"extends": "laptop", "real_data": True})
        seen = {}

        def spy(args, rig, task_, params, mode):
            seen.update(rig=rig, mode=mode, ref=args.rig_ref)
            return 0

        monkeypatch.setattr(cli_main, "_trial_session", spy)
        code = run_experiment(
            task_class=task,
            default_rig="laptop",
            argv=["--task", "rig-demo", "--sub", "p01", "--ses", "1", "--initials", "AB"],
        )

        assert code == 0
        assert seen["mode"] is Mode.RUN
        assert seen["rig"].real_data is True
        assert (seen["ref"].name, seen["ref"].source) == ("laptop", "experiment")


class TestTheRule:
    """`real_data_refusal` on its own: which sessions it refuses, and what
    it asks of the caller."""

    def collecting(self, tmp_path) -> RigConfig:
        return RigConfig(monitor=MONITOR, data_root=tmp_path)

    def development(self, tmp_path) -> RigConfig:
        return RigConfig(monitor=MONITOR, data_root=tmp_path, real_data=False)

    def test_only_run_mode_on_a_development_rig_is_refused(self, tmp_path):
        for mode in Mode:
            refused = real_data_refusal(mode, self.development(tmp_path)) is not None
            assert refused is (mode is Mode.RUN), mode
            assert real_data_refusal(mode, self.collecting(tmp_path)) is None, mode

    def test_the_callers_lines_are_asked_for_only_when_it_refuses(self, tmp_path):
        asked = []

        def instead():
            asked.append(True)
            return ["Choose another rig."]

        assert real_data_refusal(Mode.RUN, self.collecting(tmp_path), instead=instead) is None
        assert real_data_refusal(Mode.TEST, self.development(tmp_path), instead=instead) is None
        assert asked == []
        refusal = real_data_refusal(Mode.RUN, self.development(tmp_path), instead=instead)
        assert asked == [True]
        assert refusal.split("\n  ")[1] == "Choose another rig."

    def test_a_rig_with_no_name_is_refused_in_words_that_need_none(self, tmp_path):
        refusal = real_data_refusal(Mode.RUN, self.development(tmp_path))
        assert refusal == (
            "run mode records real data, and this rig is a development rig: its settings say "
            "`real_data: false`. Nothing was started and nothing was written.\n"
            "  To record real data on this machine on purpose, give the experiment a rig file "
            f"of its own that says `real_data: true` ({REAL_DATA_DOCS})."
        )

    def test_a_shared_rig_is_named_by_the_spelling_that_reaches_it(self, tmp_path):
        ref = RigRef("mac", SHARED_RIG_DIR / "rig-mac.yaml", "alhazen")
        refusal = real_data_refusal(Mode.RUN, load_rig(shared_rig_files()["mac"]), ref)
        assert "and alhazen/mac (alhazen's shared rig) is a development rig" in refusal
        assert "own configs/rig-mac.yaml saying `extends: mac` and `real_data: true`" in refusal
