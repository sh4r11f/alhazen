"""`alhazen new`, `alhazen run` and `alhazen calibrate`."""

from __future__ import annotations

import csv
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from alhazen._scaffold import scaffold, task_class_name
from alhazen.cli.calibrate import (
    fit_gamma,
    gamma_path,
    read_measurements,
    ruler_report,
    write_gamma,
)
from alhazen.cli.main import _next_run, main
from alhazen.cli.tasks import load_task_class
from alhazen.config.models import DisplayConfig, RigConfig
from alhazen.errors import ConfigError
from support import MONITOR


def import_scaffolded_task(root: Path, package: str):
    """Import a scaffolded package's ``task.py`` as a real module."""
    import importlib.util

    name = f"scaffolded_{package}_task"
    spec = importlib.util.spec_from_file_location(name, root / "src" / package / "task.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def scrubbed_env(python_path: list, home) -> dict[str, str]:
    """A near-empty environment for a subprocess, portable enough to boot.

    "Near-empty" is the point: these tests prove a scaffolded package works
    from its own files rather than from whatever the developer happens to
    have exported. A handful of variables still have to survive the scrub —
    Windows cannot seed hash randomisation without ``SystemRoot``, and a child
    that dies there reports a fatal interpreter error that says nothing about
    the test that started it.
    """
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        # `os.pathsep`, not ":" — the separator is ";" on Windows, and a
        # PYTHONPATH joined with the wrong one names one directory nobody has.
        "PYTHONPATH": os.pathsep.join(str(part) for part in python_path if str(part)),
        "HOME": str(home),
    }
    for name in ("SystemRoot", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP"):
        if name in os.environ:
            environment[name] = os.environ[name]
    return environment


class TestScaffold:
    def test_it_writes_a_package_that_looks_like_one(self, tmp_path):
        root = scaffold("saccade_bias", tmp_path)
        expected = {
            "pyproject.toml",
            "README.md",
            "run.py",
            ".gitignore",
            "src/saccade_bias/__init__.py",
            "src/saccade_bias/task.py",
            "configs/rig-sim.yaml",
            "configs/rig-view.yaml",
            "configs/rig-auto.yaml",
            "configs/rig-mouse.yaml",
            "configs/rig-mac.yaml",
            "configs/rig-lab.yaml",
            "configs/task.yaml",
            "tests/test_task.py",
        }
        # Posix separators both sides: the expected set is written with
        # forward slashes, and `str()` on a Windows path is not.
        written = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
        assert expected <= written

    def test_the_placeholders_are_all_filled_and_the_files_are_python(self, tmp_path):
        root = scaffold("saccade_bias", tmp_path)
        for path in root.rglob("*.py"):
            text = path.read_text()
            # A leftover $placeholder would be a syntax error a user meets
            # instead of a working example.
            assert "$" not in text, f"{path.name} still holds a placeholder"
            # And what renders must at least parse — run.py is only executed
            # by the slow acceptance test, which is deselected while
            # iterating, so a syntax error would otherwise hide there.
            compile(text, str(path), "exec")

    def test_every_rendered_config_loads_through_its_real_loader(self, tmp_path):
        """Not "the file exists" — "the file works". The scaffolded
        `rig-lab.yaml` shipped for a whole phase with `devices:` followed only
        by comments, which is YAML for `devices: null`, which the non-Optional
        field rejects. A test that only checked existence could not see it."""
        from alhazen.config.loader import load_params, load_rig

        root = scaffold("saccade_bias", tmp_path)
        # Imported as a module rather than exec'd into a bare namespace: the
        # params model has forward references pydantic can only resolve
        # against a real module's globals.
        module = import_scaffolded_task(root, "saccade_bias")
        params_model = getattr(module, task_class_name("saccade_bias")).params_model

        for name in (
            "rig-sim.yaml",
            "rig-view.yaml",
            "rig-auto.yaml",
            "rig-mouse.yaml",
            "rig-mac.yaml",
            "rig-lab.yaml",
        ):
            rig = load_rig(root / "configs" / name)
            assert rig.monitor.width_px > 0, name
        load_params(root / "configs" / "task.yaml", params_model)

    def test_the_dev_rigs_keep_their_data_out_of_the_subject_tree(self, tmp_path):
        """One rig file per purpose, and the purposes that rehearse — view,
        auto, mouse, mac — all write under data/dev. A dev rig pointing at
        the rig's own data root is how a simulated subject ends up in the
        same table as a real one."""
        from pathlib import PurePath

        from alhazen.config.loader import load_rig

        root = scaffold("saccade_bias", tmp_path)
        for name in ("rig-view.yaml", "rig-auto.yaml", "rig-mouse.yaml", "rig-mac.yaml"):
            rig = load_rig(root / "configs" / name)
            assert rig.data_root == PurePath("data/dev"), name
        for name in ("rig-sim.yaml", "rig-lab.yaml"):
            assert load_rig(root / "configs" / name).data_root == PurePath("data"), name

    def test_the_template_task_answers_every_advertised_mode(self, tmp_path):
        """`alhazen new`'s closing message, run.py's docstring and the rig
        headers all print `--mode demo`, `--mode movie` and `--mode simulate`
        commands verbatim. The template task must answer them: a scaffold
        whose own printed commands exit with 'implement X to use this' is a
        worse first impression than one that prints fewer commands."""
        import numpy as np

        from alhazen.display.screen import Screen
        from alhazen.modes.movie import MovieSetup
        from alhazen.task.task import Task as BaseTask

        root = scaffold("saccade_bias", tmp_path)
        module = import_scaffolded_task(root, "saccade_bias")
        task_class = getattr(module, task_class_name("saccade_bias"))
        task = task_class(task_class.params_model())

        # simulate: a subject in the chair, not the base class's None.
        simulation = task.simulation(seed=0)
        assert simulation is not None and not simulation.is_empty()

        # movie: real frames of the rig's screen, one per flip of the hold.
        setup = MovieSetup(
            screen=Screen(width_px=32, height_px=24, px_per_deg=8.0),
            hz=30.0,
            params=task.params,
            rng=np.random.default_rng(0),
        )
        clips = task.movie_clips(setup)
        frames = list(clips[0].frames())
        assert frames and frames[0].shape == (24, 32)
        assert len(frames) == round(task.params.hold_duration.seconds(30.0) * 30.0)

        # demo: needs a real window to build its stimulus, so what can be
        # checked headless is that the hook is genuinely overridden.
        assert type(task).demo_views is not BaseTask.demo_views

    def test_the_generated_task_is_importable_and_declares_itself(self, tmp_path):
        root = scaffold("saccade_bias", tmp_path)
        namespace: dict = {}
        source = (root / "src" / "saccade_bias" / "task.py").read_text()
        exec(compile(source, "task.py", "exec"), namespace)  # noqa: S102 - the file we just wrote
        task_class = namespace[task_class_name("saccade_bias")]
        assert task_class.name == "saccade-bias"
        assert task_class.params_model is not None
        # And it builds a trial from library phases, which is the example
        # the scaffold is meant to set.
        assert hasattr(task_class, "build_trial")

    def test_the_entry_point_names_the_task(self, tmp_path):
        root = scaffold("saccade_bias", tmp_path)
        pyproject = (root / "pyproject.toml").read_text()
        assert 'saccade-bias = "saccade_bias.task:SaccadeBiasTask"' in pyproject

    def test_a_name_that_is_not_a_package_is_refused(self, tmp_path):
        with pytest.raises(ConfigError, match="cannot be a package name"):
            scaffold("My Task", tmp_path)
        with pytest.raises(ConfigError, match="cannot be a package name"):
            scaffold("2fast", tmp_path)

    def test_hyphens_become_underscores_in_the_package(self, tmp_path):
        root = scaffold("saccade-bias", tmp_path)
        assert (root / "src" / "saccade_bias" / "task.py").exists()

    def test_it_will_not_overwrite_someones_work(self, tmp_path):
        root = scaffold("demo", tmp_path)
        (root / "notes.md").write_text("weeks of work")
        with pytest.raises(ConfigError, match="already exists and is not empty"):
            scaffold("demo", tmp_path)
        scaffold("demo", tmp_path, force=True)  # unless told to
        assert (root / "notes.md").exists()


class TestScaffoldCli:
    def test_it_reports_what_to_do_next(self, tmp_path, capsys):
        assert main(["new", "demo_task", "--into", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        assert "created" in out and "pytest" in out and "run.py" in out

    def test_a_bad_name_exits_nonzero(self, tmp_path, capsys):
        assert main(["new", "Bad Name", "--into", str(tmp_path)]) == 1
        assert "CANNOT SCAFFOLD" in capsys.readouterr().err


@pytest.mark.slow
class TestScaffoldedPackageWorks:
    """The acceptance claim: scaffold, INSTALL, test, run — with nothing else.

    Installed, not merely on the path (spec 8.1). Injecting `src/` into
    `PYTHONPATH` — which is what this used to do — exercises none of the
    packaging: not that the wheel carries the scaffold's own files, not that
    `[project.entry-points."alhazen.tasks"]` registers, not that a fresh
    environment can resolve the dependency on alhazen. Those are exactly the
    things that break between a working checkout and a rig.
    """

    def install(self, root: Path, tmp_path: Path) -> dict[str, str]:
        """`pip --target` both packages into one directory, and hand back the
        environment that finds them.

        A --target install runs the real build backend and writes real
        dist-info, so entry points are registered the way an installed
        package registers them. It is minutes faster than a venv and asks
        nothing of the network beyond what is already in this environment.
        """
        target = tmp_path / "site"
        alhazen_root = Path(__file__).parents[2]
        install = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                "--no-build-isolation",
                "--target",
                str(target),
                str(alhazen_root),
                str(root),
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if install.returncode != 0:
            pytest.skip(f"pip could not install into a target directory:\n{install.stderr}")
        # The target holds only the two packages; alhazen's own dependencies
        # come from the environment running the tests.
        return scrubbed_env([target, os.environ.get("PYTHONPATH", "")], tmp_path)

    def test_it_installs_registers_tests_and_runs(self, tmp_path):
        root = scaffold("acceptance_demo", tmp_path)
        environment = self.install(root, tmp_path)

        # The entry point is registered by the INSTALL, which is the claim
        # `alhazen run --task` rests on.
        listed = subprocess.run(
            [sys.executable, "-m", "alhazen.cli.main", "run", "--list"],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env=environment,
            timeout=300,
        )
        assert listed.returncode == 0, listed.stdout + listed.stderr
        assert "acceptance-demo" in listed.stdout

        tests = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(root / "tests")],
            capture_output=True,
            text=True,
            cwd=root,
            env=environment,
            timeout=600,
        )
        assert tests.returncode == 0, tests.stdout + tests.stderr

        session = subprocess.run(
            [
                sys.executable,
                "-m",
                "alhazen.cli.main",
                "run",
                "--task",
                "acceptance-demo",
                "--rig",
                str(root / "configs" / "rig-sim.yaml"),
                "--params",
                str(root / "configs" / "task.yaml"),
                "--sub",
                "s01",
                "--ses",
                "1",
            ],
            capture_output=True,
            text=True,
            cwd=root,
            env=environment,
            timeout=600,
        )
        assert session.returncode == 0, session.stdout + session.stderr
        run_dir = next((root / "data").glob("sub-s01/ses-001/run-*"))
        with next(run_dir.glob("*_trials.csv")).open() as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == 10  # the template's paradigm block
        assert all(row["outcome"] in {"FIXATED", "NO_FIXATION", "FIX_BREAK"} for row in rows)

    def test_the_bundled_runner_works_without_installing_anything(self, tmp_path):
        """`run.py` exists so a session can be started before the package is
        installed at all; that promise is on the template's own README."""
        root = scaffold("bundled_demo", tmp_path)
        environment = scrubbed_env([root / "src", Path(__file__).parents[2] / "src"], tmp_path)

        session = subprocess.run(
            [
                sys.executable,
                str(root / "run.py"),
                "--rig",
                str(root / "configs" / "rig-sim.yaml"),
                "--sub",
                "s01",
                "--ses",
                "1",
            ],
            capture_output=True,
            text=True,
            cwd=root,
            env=environment,
            timeout=600,
        )

        assert session.returncode == 0, session.stdout + session.stderr
        assert next((root / "data").glob("sub-s01/ses-001/run-*")).is_dir()

        # The OTHER advertised headless modes work from the same tree — the
        # scaffold's own printed commands must not be the first thing a new
        # user sees fail. Simulate names its subject and reduces its own
        # trial counts; movie needs no window at all.
        simulate = subprocess.run(
            [
                sys.executable,
                str(root / "run.py"),
                "--mode",
                "simulate",
                "--rig",
                str(root / "configs" / "rig-sim.yaml"),
                "--seed",
                "1",
            ],
            capture_output=True,
            text=True,
            cwd=root,
            env=environment,
            timeout=600,
        )
        assert simulate.returncode == 0, simulate.stdout + simulate.stderr
        assert next((root / "data-rehearsal").glob("sub-sim/**/run-*"), None) is not None

        recorded = subprocess.run(
            [
                sys.executable,
                str(root / "run.py"),
                "--mode",
                "movie",
                "--rig",
                str(root / "configs" / "rig-sim.yaml"),
                "--out",
                str(root / "movies"),
            ],
            capture_output=True,
            text=True,
            cwd=root,
            env=environment,
            timeout=600,
        )
        assert recorded.returncode == 0, recorded.stdout + recorded.stderr
        assert (root / "movies" / "fixation-hold.mp4").exists()


class TestNextRunNumber:
    """`_next_run` decides where a session's data goes. Nothing tested it."""

    def session_dir(self, tmp_path, *runs: str) -> Path:
        directory = tmp_path / "sub-s01" / "ses-001"
        directory.mkdir(parents=True)
        for name in runs:
            (directory / name).mkdir()
        return tmp_path

    def test_an_empty_data_root_starts_at_one(self, tmp_path):
        assert _next_run(tmp_path, "s01", 1) == 1

    def test_a_session_with_no_runs_yet_starts_at_one(self, tmp_path):
        root = self.session_dir(tmp_path)
        assert _next_run(root, "s01", 1) == 1

    def test_it_counts_the_directories_that_exist(self, tmp_path):
        root = self.session_dir(tmp_path, "run-01_task-demo", "run-02_task-demo")
        assert _next_run(root, "s01", 1) == 3

    def test_a_gap_does_not_reuse_a_number(self, tmp_path):
        """One past the HIGHEST, not the first hole. Filling a gap would
        write into a numbering an experimenter's notes already refer to."""
        root = self.session_dir(tmp_path, "run-01_task-demo", "run-04_task-demo")
        assert _next_run(root, "s01", 1) == 5

    def test_a_directory_that_is_not_a_run_is_ignored(self, tmp_path):
        root = self.session_dir(tmp_path, "run-01_task-demo", "run-notes", "run-")
        assert _next_run(root, "s01", 1) == 2

    def test_each_session_numbers_independently(self, tmp_path):
        root = self.session_dir(tmp_path, "run-01_task-demo", "run-02_task-demo")
        assert _next_run(root, "s01", 2) == 1
        assert _next_run(root, "s02", 1) == 1


class TestRunCommand:
    """The happy path through `alhazen run`, and the prompts it falls back to."""

    def registered(self, monkeypatch, tmp_path):
        """Register the scaffolded task the way an installed package would."""
        root = scaffold("run_demo", tmp_path / "pkg")
        module = import_scaffolded_task(root, "run_demo")
        task_class = getattr(module, task_class_name("run_demo"))
        monkeypatch.setattr(
            "alhazen.cli.tasks.installed_tasks",
            lambda: {"run-demo": SimpleNamespace(name="run-demo", load=lambda: task_class)},
        )
        rig = tmp_path / "rig.yaml"
        rig.write_text(
            yaml.safe_dump(
                {
                    "monitor": MONITOR.model_dump(),
                    "display": {"backend": "simulated"},
                    "data_root": str(tmp_path / "data"),
                }
            )
        )
        # A one-trial, few-frame version of the template's own config: the
        # point here is the command's plumbing, not the template's timing.
        self.params = tmp_path / "task.yaml"
        self.params.write_text(
            yaml.safe_dump(
                {
                    "acquire_timeout": {"frames": 2},
                    "hold_duration": {"frames": 1},
                    "iti": {"ms": 0},
                    "paradigm": {"kind": "sequence", "n_per_condition": 1},
                }
            )
        )
        return rig

    def run_args(self, rig, *extra: str) -> list[str]:
        return [
            "run",
            "--task",
            "run-demo",
            "--rig",
            str(rig),
            "--params",
            str(self.params),
            *extra,
        ]

    def test_a_simulated_session_runs_through_the_entry_point(self, monkeypatch, tmp_path, capsys):
        rig = self.registered(monkeypatch, tmp_path)

        code = main(self.run_args(rig, "--sub", "s01", "--ses", "1"))

        assert code == 0
        out = capsys.readouterr().out
        assert "sub-s01 ses-001 run-01" in out
        assert "session complete" in out
        run_dir = next((tmp_path / "data").glob("sub-s01/ses-001/run-*"))
        with next(run_dir.glob("*_trials.csv")).open() as handle:
            assert list(csv.DictReader(handle))

    def test_the_subject_and_session_are_prompted_when_omitted(self, monkeypatch, tmp_path, capsys):
        rig = self.registered(monkeypatch, tmp_path)
        answers = iter(["s02", "3"])
        monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
        # The prompt only fires at a terminal; this test stands in for one.
        monkeypatch.setattr("sys.stdin", type("Tty", (), {"isatty": lambda self: True})())

        assert main(self.run_args(rig)) == 0

        assert "sub-s02 ses-003 run-01" in capsys.readouterr().out

    def test_the_run_number_defaults_to_the_next_free_one(self, monkeypatch, tmp_path, capsys):
        rig = self.registered(monkeypatch, tmp_path)
        main(self.run_args(rig, "--sub", "s01", "--ses", "1"))
        capsys.readouterr()

        main(self.run_args(rig, "--sub", "s01", "--ses", "1"))

        assert "run-02" in capsys.readouterr().out

    def test_an_unknown_task_exits_nonzero_and_lists_what_is_installed(self, tmp_path, capsys):
        rig = tmp_path / "rig.yaml"
        rig.write_text(
            yaml.safe_dump(
                {
                    "monitor": MONITOR.model_dump(),
                    "display": {"backend": "simulated"},
                    "data_root": str(tmp_path / "data"),
                }
            )
        )

        assert main(["run", "--task", "nope", "--rig", str(rig), "--sub", "s", "--ses", "1"]) == 1

        assert "Installed tasks" in capsys.readouterr().err

    def test_a_missing_rig_file_exits_nonzero(self, monkeypatch, tmp_path, capsys):
        self.registered(monkeypatch, tmp_path)

        code = main(
            [
                "run",
                "--task",
                "run-demo",
                "--rig",
                str(tmp_path / "nope.yaml"),
                "--sub",
                "s",
                "--ses",
                "1",
            ]
        )

        assert code == 1
        assert "INVALID" in capsys.readouterr().err


class TestGammaReachesTheDisplay:
    """`calibrate gamma` wrote a file that nothing ever read. A measurement
    made and not applied is worse than none: it costs an afternoon with a
    photometer and leaves every "50% contrast" at 50% of code value."""

    def rig_file(self, tmp_path) -> Path:
        path = tmp_path / "rig-sim.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "monitor": MONITOR.model_dump(),
                    "display": {"backend": "simulated"},
                    "data_root": str(tmp_path / "data"),
                }
            )
        )
        return path

    def build(self, tmp_path, rig_path):
        from alhazen.core.events import EventSchema
        from alhazen.paradigms.base import Condition, SimpleSequence
        from alhazen.session.builder import build_session
        from alhazen.task.plan import TrialPlan
        from support import COMPLETED, RunForFrames

        return build_session(
            rig=rig_path,
            subject="s01",
            session=1,
            run=1,
            task_name="gamma-demo",
            task_params=DisplayConfig(backend="simulated"),
            event_schema=EventSchema(()),
            build_trial=lambda setup: TrialPlan(phases=[RunForFrames(0, COMPLETED)]),
            make_source=lambda params, rng: SimpleSequence([Condition({})], n_repeats=1, rng=rng),
            simulated_frame_period_s=0.0,
            date_yyyymmdd="20260826",
        )

    def test_a_stored_fit_is_applied_when_the_display_opens(self, tmp_path):
        rig_path = self.rig_file(tmp_path)
        write_gamma(rig_path, {"gamma": 2.27, "min_luminance": 0.1, "max_luminance": 100.0})

        runner = self.build(tmp_path, rig_path)

        assert runner._display.gamma == pytest.approx(2.27)

    def test_no_stored_fit_leaves_the_display_alone(self, tmp_path):
        runner = self.build(tmp_path, self.rig_file(tmp_path))

        assert runner._display.gamma is None

    def test_the_calibrate_command_writes_where_the_builder_looks(self, tmp_path):
        rig_path = self.rig_file(tmp_path)
        measurements = tmp_path / "readings.csv"
        levels = np.linspace(0.0, 1.0, 12)
        rows = ["level,luminance"]
        rows += [f"{level},{0.1 + 99.9 * level**2.2}" for level in levels]
        measurements.write_text("\n".join(rows) + "\n")

        assert (
            main(
                ["calibrate", "gamma", "--rig", str(rig_path), "--measurements", str(measurements)]
            )
            == 0
        )

        runner = self.build(tmp_path, rig_path)
        assert runner._display.gamma == pytest.approx(2.2, abs=0.05)

    def test_the_public_fake_satisfies_the_display_protocol(self):
        """A fake missing a protocol method makes the session that calls it
        untestable — which is how this went unnoticed."""
        from alhazen.display.backend import DisplayBackend
        from alhazen.testing import FakeClock, FakeDisplay

        display = FakeDisplay(FakeClock())
        assert isinstance(display, DisplayBackend)
        display.set_gamma(1.8)
        assert display.gamma == 1.8


class TestTaskDiscovery:
    def test_an_unknown_task_lists_what_is_installed(self):
        # On a rig, "no such task" with nothing else is a dead end, and the
        # answer is almost always that the package was never installed here.
        with pytest.raises(ConfigError, match="Installed tasks"):
            load_task_class("no-such-task")

    def test_run_without_a_task_says_what_it_needs(self, capsys):
        assert main(["run", "--rig", "x.yaml"]) == 2
        assert "--task" in capsys.readouterr().err

    def test_listing_tasks_never_fails(self, capsys):
        assert main(["run", "--list"]) == 0


class TestCalibrateRuler:
    def rig(self, tmp_path) -> RigConfig:
        return RigConfig(
            monitor=MONITOR, display=DisplayConfig(backend="simulated"), data_root=tmp_path
        )

    def test_it_reports_a_measurable_length(self, tmp_path):
        report = ruler_report(self.rig(tmp_path), size_dva=10.0)
        assert "cm on the panel" in report
        assert "px per degree" in report

    def test_the_two_ways_of_computing_it_agree_at_small_angles(self, tmp_path):
        # The linear model and exact trigonometry must not disagree
        # meaningfully over the range experiments use — if they did, the
        # ruler would be checking the wrong thing.
        report = ruler_report(self.rig(tmp_path), size_dva=10.0)
        linear = float(report.split("cm on the panel")[0].split("\n")[-1].strip())
        exact = float(report.split("cm by exact trigonometry")[0].split("\n")[-1].strip())
        assert abs(linear - exact) / exact < 0.01


class TestCalibrateGamma:
    def measurements(self, tmp_path, gamma=2.2, n=12) -> Path:
        path = tmp_path / "photometer.csv"
        levels = np.linspace(0.0, 1.0, n)
        luminance = 0.5 + 99.5 * levels**gamma
        with path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["level", "luminance"])
            writer.writerows(zip(levels, luminance, strict=True))
        return path

    def test_a_planted_gamma_is_recovered(self, tmp_path):
        levels, luminances = read_measurements(self.measurements(tmp_path, gamma=2.2))
        fit = fit_gamma(levels, luminances)
        assert fit["gamma"] == pytest.approx(2.2, abs=0.05)
        assert fit["residual_rms"] < 0.01

    def test_levels_given_in_0_255_work_too(self, tmp_path):
        path = tmp_path / "photometer.csv"
        levels = np.linspace(0, 255, 10)
        luminance = 0.5 + 99.5 * (levels / 255) ** 2.0
        with path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["level", "luminance"])
            writer.writerows(zip(levels, luminance, strict=True))
        levels_read, luminances = read_measurements(path)
        assert levels_read.max() <= 1.0
        assert fit_gamma(levels_read, luminances)["gamma"] == pytest.approx(2.0, abs=0.05)

    def test_a_file_without_the_columns_says_which(self, tmp_path):
        path = tmp_path / "wrong.csv"
        path.write_text("brightness,cd\n1,2\n3,4\n5,6\n")
        with pytest.raises(ConfigError, match="'level' and 'luminance'"):
            read_measurements(path)

    def test_too_few_measurements_is_refused(self, tmp_path):
        path = tmp_path / "sparse.csv"
        path.write_text("level,luminance\n0,1\n1,100\n")
        with pytest.raises(ConfigError, match="at least 3"):
            read_measurements(path)

    def test_a_flat_response_says_to_check_the_meter(self, tmp_path):
        with pytest.raises(ConfigError, match="do not increase"):
            fit_gamma(np.linspace(0, 1, 5), np.full(5, 10.0))

    def test_the_fit_is_stored_beside_its_own_rig(self, tmp_path):
        # A gamma table belongs to one physical monitor; following the wrong
        # one is worse than having none.
        rig_path = tmp_path / "rig-lab.yaml"
        rig_path.write_text("monitor: {}\n")
        fit = {
            "gamma": 2.2,
            "min_luminance": 0.5,
            "max_luminance": 100.0,
            "n_measurements": 12,
            "residual_rms": 0.001,
        }
        written = write_gamma(rig_path, fit)
        assert written == gamma_path(rig_path) == tmp_path / "rig-lab_gamma.yaml"
        assert yaml.safe_load(written.read_text())["gamma"] == 2.2


class TestCalibrateCli:
    def rig_file(self, tmp_path) -> Path:
        path = tmp_path / "rig.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "monitor": {
                        "width_px": 1920,
                        "height_px": 1080,
                        "width_cm": 60,
                        "distance_cm": 60,
                        "refresh_rate_hz": 60,
                    },
                    "display": {"backend": "simulated"},
                    "data_root": str(tmp_path / "data"),
                }
            )
        )
        return path

    def test_ruler_prints_a_length(self, tmp_path, capsys):
        assert main(["calibrate", "ruler", "--rig", str(self.rig_file(tmp_path))]) == 0
        assert "cm on the panel" in capsys.readouterr().out

    def test_gamma_fits_and_writes(self, tmp_path, capsys):
        rig_path = self.rig_file(tmp_path)
        measurements = TestCalibrateGamma().measurements(tmp_path)
        assert (
            main(
                ["calibrate", "gamma", "--rig", str(rig_path), "--measurements", str(measurements)]
            )
            == 0
        )
        out = capsys.readouterr().out
        assert "gamma 2.2" in out
        assert gamma_path(rig_path).exists()

    def test_a_missing_measurements_file_exits_nonzero(self, tmp_path, capsys):
        assert (
            main(
                [
                    "calibrate",
                    "gamma",
                    "--rig",
                    str(self.rig_file(tmp_path)),
                    "--measurements",
                    str(tmp_path / "nope.csv"),
                ]
            )
            == 1
        )
        assert "CANNOT CALIBRATE" in capsys.readouterr().err


class TestGammaOnTheDisplay:
    def test_a_simulated_display_records_what_it_would_have_applied(self):
        from alhazen.display.simulated import SimulatedDisplay

        display = SimulatedDisplay(60.0, frame_period_s=0.0)
        assert display.gamma is None  # "none measured" is not "gamma 1.0"
        display.set_gamma(2.2)
        assert display.gamma == 2.2
