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


class _Terminal:
    """A stdin that says it is a terminal, so the prompts are allowed."""

    def isatty(self):
        return True


class TestTheSessionNumberPrompt:
    """`int(input(...))` turned a typo at the prompt into a raw ValueError
    traceback, with the rig config loaded and an animal waiting. A bad
    answer is now said to be bad, in the CLI's own words, and asked again."""

    def settle(self, monkeypatch, answers):
        import argparse
        import builtins
        import sys

        from alhazen.cli.main import _settle_subject_and_session
        from alhazen.modes import Mode

        asked = []
        replies = iter(answers)

        def fake_input(prompt):
            asked.append(prompt)
            return next(replies)

        monkeypatch.setattr(sys, "stdin", _Terminal())
        monkeypatch.setattr(builtins, "input", fake_input)
        # Initials given: test mode asks for them too since 2.0, and these
        # tests are about the session number's prompt.
        args = argparse.Namespace(sub="s01", ses=None, initials="HD")
        refused = _settle_subject_and_session(args, Mode.TEST)
        return refused, args, asked

    def test_a_typo_is_named_and_asked_again(self, monkeypatch, capsys):
        refused, args, asked = self.settle(monkeypatch, ["1a", "3"])

        assert refused is None
        assert args.ses == 3
        assert asked == ["session number: ", "session number: "]
        err = capsys.readouterr().err
        assert "INVALID" in err and "'1a'" in err

    def test_a_number_below_one_is_asked_again(self, monkeypatch, capsys):
        """Sessions are numbered from 1 (SessionInfo refuses 0) — caught at
        the prompt rather than later, as a validation error from the builder."""
        refused, args, _ = self.settle(monkeypatch, ["0", "", "2"])

        assert refused is None
        assert args.ses == 2
        assert capsys.readouterr().err.count("INVALID") == 2

    def test_a_good_answer_is_taken_first_time(self, monkeypatch, capsys):
        refused, args, asked = self.settle(monkeypatch, [" 12 "])

        assert (refused, args.ses, len(asked)) == (None, 12, 1)
        assert capsys.readouterr().err == ""


class TestABadCurriculumIsInvalid:
    def test_a_missing_curriculum_file_is_reported_not_raised(self, tmp_path, capsys):
        """`--curriculum` was loaded outside the ConfigError handling, so a
        misspelled path printed a traceback where every other bad config
        file prints `INVALID:` and exits 1."""
        from alhazen.cli.modes import run_experiment
        from alhazen.config.models import Model
        from alhazen.core.events import EventSchema
        from alhazen.core.trial import outcomes as make_outcomes
        from alhazen.task.task import Task

        class CurriculumParams(Model):
            pass

        class CurriculumTask(Task):
            name = "curriculum-check"
            events = EventSchema(())
            outcomes = make_outcomes(DONE=dict(completed=True, success=True))
            params_model = CurriculumParams

        missing = tmp_path / "no-such-curriculum.yaml"
        code = run_experiment(
            task_class=CurriculumTask,
            default_rig=rig_file(tmp_path),
            argv=["--mode", "simulate", "--headless", "--curriculum", str(missing)],
        )

        assert code == 1
        err = capsys.readouterr().err
        assert "INVALID: " in err
        assert str(missing) in err


class TestARefusalFromWhatIsOnDiskIsReported:
    def test_a_database_from_before_2_0_stops_the_session_with_its_path(self, tmp_path, capsys):
        """The builder refuses an experiment database it cannot write before
        the session starts (alhazen 2.0 moved it to schema 3). That refusal is
        a DataError, which the CLI used to let through as a traceback; it is
        reported like a bad config, with the file and the fix."""
        import sqlite3

        from alhazen.cli.modes import run_experiment
        from alhazen.config.models import Model
        from alhazen.core.events import EventSchema
        from alhazen.core.trial import outcomes as make_outcomes
        from alhazen.task.task import Task

        class DiskParams(Model):
            pass

        class DiskTask(Task):
            name = "disk-check"
            events = EventSchema(())
            outcomes = make_outcomes(DONE=dict(completed=True, success=True))
            params_model = DiskParams

        # A database from a NEWER alhazen: still refused (one from an older
        # schema is moved aside since 2.0.1, so it no longer exercises this).
        from alhazen.session.database import SCHEMA_VERSION

        (tmp_path / "data").mkdir()
        database = tmp_path / "data" / "experiment.sqlite3"
        with sqlite3.connect(database) as db:
            db.execute("CREATE TABLE schema_info (version INTEGER NOT NULL)")
            db.execute("INSERT INTO schema_info(version) VALUES (?)", (SCHEMA_VERSION + 1,))

        code = run_experiment(
            task_class=DiskTask,
            default_rig=rig_file(tmp_path),
            argv=["--mode", "run", "--sub", "01", "--ses", "1", "--initials", "HD"],
        )

        assert code == 1
        err = capsys.readouterr().err
        assert err.startswith("CANNOT RUN: ")
        assert str(database) in err and f"schema version {SCHEMA_VERSION + 1}" in err
        # Refused before a run folder was made.
        assert not list((tmp_path / "data").glob("v*"))


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
            argv=["--mode", "test", "--sub", "t01", "--ses", "4", "--initials", "TT", *argv_extra],
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


class TestInitials:
    """`--initials` (alhazen 2.0): the subject's initials, 1 to 5 letters,
    recorded uppercase. Required by `run` and `test` — prompted for like
    `--sub` and `--ses`, refused without a terminal — and optional elsewhere,
    but held to their rule wherever they are given."""

    @staticmethod
    def task_class():
        from alhazen.config.models import Model
        from alhazen.core.events import EventSchema
        from alhazen.core.trial import outcomes as make_outcomes
        from alhazen.task.task import Task

        class InitialsParams(Model):
            pass

        class InitialsTask(Task):
            name = "initials-check"
            events = EventSchema(())
            outcomes = make_outcomes(DONE=dict(completed=True, success=True))
            params_model = InitialsParams

        return InitialsTask

    def start(self, tmp_path, argv, monkeypatch=None):
        """run.py's entry point up to the builder, which is stopped there:
        what matters is what the dispatch hands it."""
        from alhazen.cli.modes import run_experiment

        seen: dict = {}
        if monkeypatch is not None:
            import alhazen.modes.session as session_module
            from alhazen.errors import ConfigError

            def stop(mode, **kwargs):
                seen.update(kwargs, mode=mode)
                raise ConfigError("stopped by the test")

            monkeypatch.setattr(session_module, "build_mode_session", stop)
        code = run_experiment(
            task_class=self.task_class(), default_rig=rig_file(tmp_path), argv=argv
        )
        return code, seen

    @pytest.mark.parametrize("mode", ["run", "test"])
    def test_they_reach_the_session_uppercase(self, tmp_path, monkeypatch, mode):
        argv = ["--mode", mode, "--sub", "01", "--ses", "1", "--initials", "hd"]
        code, seen = self.start(tmp_path, argv, monkeypatch)

        assert code == 1  # the stop above
        assert seen["initials"] == "HD"

    @pytest.mark.parametrize("mode", ["run", "test", "simulate", "demo", "measure"])
    def test_ones_that_break_the_rule_are_refused_in_every_mode(self, tmp_path, capsys, mode):
        argv = ["--mode", mode, "--sub", "01", "--ses", "1", "--initials", "H1"]
        code, _ = self.start(tmp_path, argv)

        assert code == 2
        err = capsys.readouterr().err
        assert "INVALID: initials must be 1 to 5 letters, such as HD; got 'H1'" in err
        assert not (tmp_path / "data").exists() and not (tmp_path / "data-rehearsal").exists()

    @pytest.mark.parametrize("mode", ["run", "test"])
    def test_missing_ones_are_refused_without_a_terminal(self, tmp_path, capsys, mode):
        code, _ = self.start(tmp_path, ["--mode", mode, "--sub", "01", "--ses", "1"])

        assert code == 2
        err = capsys.readouterr().err
        assert "--initials required: stdin is not a terminal" in err

    def test_simulate_needs_none(self, tmp_path, monkeypatch):
        code, seen = self.start(tmp_path, ["--mode", "simulate", "--headless"], monkeypatch)

        assert code == 1  # reached the builder
        assert seen["initials"] is None and seen["subject"] == "sim"

    def test_they_are_prompted_for_and_asked_again_until_they_are_letters(
        self, monkeypatch, capsys
    ):
        import argparse
        import builtins
        import sys

        from alhazen.cli.main import _settle_subject_and_session
        from alhazen.modes import Mode

        asked = []
        replies = iter(["01", "2", "H.D.", "", "hd"])

        def fake_input(prompt):
            asked.append(prompt)
            return next(replies)

        monkeypatch.setattr(sys, "stdin", _Terminal())
        monkeypatch.setattr(builtins, "input", fake_input)
        args = argparse.Namespace(sub=None, ses=None, initials=None)

        assert _settle_subject_and_session(args, Mode.RUN) is None

        assert (args.sub, args.ses, args.initials) == ("01", 2, "HD")
        assert asked[2:] == ["subject initials: "] * 3
        assert capsys.readouterr().err.count("INVALID: initials must be 1 to 5 letters") == 2


class TestTheCommandIsHandedDown:
    """run.py and `alhazen run` hand the command line they parsed to the
    session, which records it in session.json and the snapshot
    (session/identity.py, `recorded_command`)."""

    def start(self, tmp_path, monkeypatch, argv):
        seen: dict = {}
        import alhazen.modes.session as session_module
        from alhazen.cli.modes import run_experiment
        from alhazen.errors import ConfigError

        def stop(mode, **kwargs):
            seen.update(kwargs)
            raise ConfigError("stopped by the test")

        monkeypatch.setattr(session_module, "build_mode_session", stop)
        code = run_experiment(
            task_class=TestInitials.task_class(), default_rig=rig_file(tmp_path), argv=argv
        )
        assert code == 1  # the stop above
        return seen

    def test_run_py_hands_down_its_program_and_the_arguments_it_parsed(self, tmp_path, monkeypatch):
        import sys

        argv = ["--mode", "simulate", "--headless", "--seed", "3"]
        seen = self.start(tmp_path, monkeypatch, argv)
        assert seen["command"] == [sys.argv[0], *argv]

    def test_with_no_argv_given_it_is_the_processs_own(self, tmp_path, monkeypatch):
        import sys

        started = [str(tmp_path / "run.py"), "--mode", "simulate", "--headless"]
        monkeypatch.setattr(sys, "argv", started)
        seen = self.start(tmp_path, monkeypatch, None)
        assert seen["command"] == started

    def test_alhazen_run_hands_down_its_own_name_and_arguments(self, monkeypatch):
        import importlib

        # The module, not the `main` function alhazen.cli re-exports under
        # the same name.
        cli_main = importlib.import_module("alhazen.cli.main")
        seen: dict = {}

        def handler(args, parser):
            seen["invocation"] = args.invocation
            return 0

        monkeypatch.setitem(cli_main._COMMANDS, "run", handler)
        argv = ["run", "--mode", "simulate", "--task", "some-task", "--rig", "laptop"]

        assert main(argv) == 0
        assert seen["invocation"] == ["alhazen", *argv]
