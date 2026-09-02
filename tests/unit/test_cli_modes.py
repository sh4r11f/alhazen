"""The CLI's mode dispatch: what each --mode asks for, and what it refuses.

These are about the seam between the command line and the modes package, not
about what the modes do once started — that is tested in test_modes_*.py. What
can go wrong here is a mode demanding something it does not need, a mode
starting when it should have refused, or a rehearsal starting without saying
that it is one.
"""

from __future__ import annotations

import pytest
import yaml

from alhazen.cli.main import main


def rig_file(tmp_path, devices=None, backend="simulated"):
    path = tmp_path / "rig.yaml"
    body = {
        "monitor": {
            "width_px": 800,
            "height_px": 600,
            "width_cm": 40,
            "distance_cm": 57,
            "refresh_rate_hz": 120,
        },
        "display": {"backend": backend},
        "data_root": str(tmp_path / "data"),
    }
    if devices is not None:
        body["devices"] = devices
    path.write_text(yaml.safe_dump(body))
    return path


class TestWhatEachModeAsksFor:
    def test_measure_needs_no_task(self, tmp_path, capsys, monkeypatch):
        """Measure mode is about the machine, not the experiment. Demanding a
        task would mean a rig could not be checked until an experiment was
        installed on it, which is backwards: the rig comes first.

        The measurement itself is stubbed: it opens a real window, and a test
        that opens one measures the machine it runs on rather than the code.
        """
        from alhazen.modes import measure

        seen = {}

        def fake_run(rig, rig_path, **kwargs):
            seen["rig_path"] = rig_path
            return measure.MeasurementReport(rig_path=rig_path)

        monkeypatch.setattr(measure, "run_measurements", fake_run)
        rig = rig_file(tmp_path)

        assert main(["run", "--mode", "measure", "--rig", str(rig)]) == 0
        assert seen["rig_path"] == str(rig)
        # And the report landed beside the rig config, not beside the data.
        assert (tmp_path / "measurements").is_dir()

    @pytest.mark.parametrize("mode", ["run", "test", "simulate", "demo", "movie"])
    def test_every_other_mode_needs_a_task(self, tmp_path, mode, capsys):
        assert main(["run", "--mode", mode, "--rig", str(rig_file(tmp_path))]) == 2

        assert "--task" in capsys.readouterr().err

    def test_a_missing_rig_is_named(self, tmp_path, capsys):
        assert main(["run", "--mode", "run", "--task", "whatever"]) == 2

        assert "--rig" in capsys.readouterr().err


class TestTheDefault:
    def test_no_mode_flag_means_the_real_experiment(self, tmp_path, capsys):
        """The mode you get by not thinking about it must be the real one:
        a default of `test` would quietly write a session's data into the
        rehearsal directory."""
        main(["run", "--rig", str(rig_file(tmp_path))])

        # Reached the task check, i.e. --mode defaulted to something valid.
        assert "--task" in capsys.readouterr().err


class TestPromptsNeedATerminal:
    def test_missing_sub_and_ses_with_no_tty_exit_rather_than_hang(self, tmp_path, capsys):
        """Prompting is for a person at a rig. Under nohup or CI, input()
        blocks forever or dies in a raw EOFError after the rig config has
        already loaded — so with stdin not a terminal (which is what pytest's
        capture provides here) the missing flags are refused up front."""
        from alhazen.cli.modes import run_experiment
        from alhazen.config.models import Model
        from alhazen.core.events import EventSchema

        # Aliased because the class attribute is itself named `outcomes`, and
        # a class body's own assignment shadows the enclosing function's name.
        from alhazen.core.trial import outcomes as make_outcomes
        from alhazen.task.task import Task

        class PromptParams(Model):
            pass

        class PromptTask(Task):
            name = "prompt-check"
            events = EventSchema(())
            outcomes = make_outcomes(DONE=dict(completed=True, success=True))
            params_model = PromptParams

        code = run_experiment(
            task_class=PromptTask, default_rig=rig_file(tmp_path), argv=["--mode", "test"]
        )
        assert code == 2
        err = capsys.readouterr().err
        assert "--sub" in err and "--ses" in err and "terminal" in err


class TestMeasureRejectsAnUnknownSkip:
    def test_a_misspelled_measurement_is_refused(self, tmp_path, capsys):
        """An experimenter who thinks they skipped the tracker and did not
        will sit through it wondering why. Worse, one who thinks they ran it
        and did not gets a report with a hole in it."""
        code = main(
            ["run", "--mode", "measure", "--rig", str(rig_file(tmp_path)), "--skip", "trackr"]
        )

        assert code == 2
        assert "trackr" in capsys.readouterr().err


class TestParamsHook:
    """The one place an experiment may derive its parameters from how it was
    invoked.

    ``Task.make_source(params, rng)`` receives the params and the scheduler's
    generator, and nothing else. So a task whose scheduler must know which
    subject and which session it is — an adaptive design carrying state
    across sessions is the general case — has no route from the command line
    to its own code. This hook is that route, and it is applied here rather
    than in an experiment's run.py because a run.py that parsed argv and
    called build_session itself would be a second copy of the mode dispatch.
    """

    def hook_task(self):
        from alhazen.config.models import Model
        from alhazen.core.events import EventSchema
        from alhazen.core.trial import outcomes as make_outcomes
        from alhazen.task.task import Task

        class HookParams(Model):
            state_dir: str | None = None
            session: int | None = None

        class HookTask(Task):
            name = "hook-check"
            events = EventSchema(())
            outcomes = make_outcomes(DONE=dict(completed=True, success=True))
            params_model = HookParams

        return HookTask, HookParams

    def run(self, tmp_path, monkeypatch, hook, argv_extra=()):
        """Start a session far enough to see the params, then stop.

        The task is never actually run: build_session opens a window and
        wants a display. What matters is the value the dispatch constructed
        the task with, which the spy captures on the way past.
        """
        from alhazen.cli.modes import run_experiment

        HookTask, _ = self.hook_task()
        seen = {}

        def spy(args, rig, task, params, mode):
            seen["params"] = params
            seen["task"] = task
            return 0

        # Reached through sys.modules, not by attribute path: the cli
        # package re-exports the `main` FUNCTION under that name, so
        # "alhazen.cli.main" resolves to it rather than to the module.
        import sys

        monkeypatch.setattr(sys.modules["alhazen.cli.main"], "_trial_session", spy)
        code = run_experiment(
            task_class=HookTask,
            default_rig=rig_file(tmp_path),
            argv=["--mode", "test", "--sub", "t01", "--ses", "4", *argv_extra],
            params_hook=hook,
        )
        return code, seen

    def test_a_hook_reaches_the_task(self, tmp_path, monkeypatch):
        def hook(params, args):
            return params.model_copy(
                update={"state_dir": f"/data/sub-{args.sub}", "session": args.ses}
            )

        code, seen = self.run(tmp_path, monkeypatch, hook)

        assert code == 0
        assert seen["params"].state_dir == "/data/sub-t01"
        assert seen["params"].session == 4
        # The task the dispatch built is the one carrying the derived values,
        # not a second instance built from the file.
        assert seen["task"].params.session == 4

    def test_no_hook_changes_nothing(self, tmp_path, monkeypatch):
        code, seen = self.run(tmp_path, monkeypatch, None)

        assert code == 0
        assert seen["params"].state_dir is None
        assert seen["params"].session is None

    def test_a_hook_returning_something_the_task_cannot_express_is_refused(
        self, tmp_path, monkeypatch, capsys
    ):
        # Re-validated through the task's own model, so a hook that returns
        # nonsense fails here with the file open rather than mid-session.
        def hook(params, args):
            return {"session": "the fourth one"}

        code, _ = self.run(tmp_path, monkeypatch, hook)

        assert code == 1
        err = capsys.readouterr().err
        assert "INVALID" in err
        assert "session" in err

    def test_a_hook_returning_an_unknown_field_is_refused(self, tmp_path, monkeypatch, capsys):
        def hook(params, args):
            return {"stat_dir": "/data"}  # a typo for state_dir

        code, _ = self.run(tmp_path, monkeypatch, hook)

        assert code == 1
        assert "stat_dir" in capsys.readouterr().err

    def test_a_hook_that_raises_is_not_swallowed(self, tmp_path, monkeypatch):
        def hook(params, args):
            raise RuntimeError("the rig file has no data_root I can use")

        with pytest.raises(RuntimeError, match="data_root"):
            self.run(tmp_path, monkeypatch, hook)

    def test_alhazen_run_passes_no_hook(self, tmp_path, capsys):
        # The shared dispatch must behave identically when nobody supplies
        # one; this is the regression guard on the default.
        code = main(["run", "--mode", "test", "--rig", str(rig_file(tmp_path))])

        assert code == 2
        assert "--task" in capsys.readouterr().err
