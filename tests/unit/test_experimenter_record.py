"""Who ran a session: --experimenter and --experimenter-id, recorded in
session.json and session.log (session.identity.Experimenter)."""

from __future__ import annotations

import json

import pytest
from tests.unit import test_builder, test_cli_modes

from alhazen.session.identity import Experimenter

# The helpers of the tests these extend, by module so pytest does not collect
# those classes here a second time.
Built = test_builder.TestTheExperimentVersionFilesTheRun
rig_file = test_cli_modes.rig_file


class TestParse:
    def test_name_and_id(self):
        assert Experimenter.parse("  Zoë Lee ", "e_1a2b") == Experimenter("Zoë Lee", "e_1a2b")
        assert Experimenter.parse("Ana") == Experimenter("Ana", None)
        assert Experimenter.parse(None, None) is None

    @pytest.mark.parametrize(
        ("name", "record_id", "words"),
        [
            (None, "e_1", "needs --experimenter"),
            ("", None, "1 to 120"),
            ("x" * 121, None, "1 to 120"),
            ("two\nlines", None, "one line"),
            ("Ana", "../x", "--experimenter-id must be"),
            ("Ana", "a b", "--experimenter-id must be"),
        ],
    )
    def test_refusals(self, name, record_id, words):
        with pytest.raises(ValueError, match=words):
            Experimenter.parse(name, record_id)


class TestRecorded:
    def test_session_json_and_log_name_the_experimenter(self, tmp_path, monkeypatch):
        repo, task = Built.experiment_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        Built.session(
            tmp_path, task, rig=repo / "rig-sim.yaml", experimenter=Experimenter("Zoë", "e_9")
        ).run()
        (run_dir,) = (p.parent for p in (tmp_path / "data").rglob("session.json"))
        card = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
        assert card["experimenter"] == {"id": "e_9", "name": "Zoë"}
        log = next(run_dir.glob("*session.log")).read_text(encoding="utf-8")
        assert "experimenter: Zoë (e_9)" in log

    def test_without_one_the_card_says_not_recorded(self, tmp_path, monkeypatch):
        repo, task = Built.experiment_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        Built.session(tmp_path, task, rig=repo / "rig-sim.yaml").run()
        (run_dir,) = (p.parent for p in (tmp_path / "data").rglob("session.json"))
        card = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
        assert card["experimenter"] is None
        log = next(run_dir.glob("*session.log")).read_text(encoding="utf-8")
        assert "experimenter:" not in log


class TestCommandLine:
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
            task_class=test_cli_modes.TestInitials.task_class(),
            default_rig=rig_file(tmp_path),
            argv=argv,
        )
        return code, seen

    def test_the_flags_reach_the_session(self, tmp_path, monkeypatch):
        argv = [
            "--mode",
            "simulate",
            "--headless",
            "--experimenter",
            "Zoë Lee",
            "--experimenter-id",
            "e_1a2b",
        ]
        code, seen = self.start(tmp_path, monkeypatch, argv)
        assert code == 1  # the test's stop, after the flags were read
        assert seen["experimenter"] == Experimenter("Zoë Lee", "e_1a2b")

    def test_without_the_flags_nothing_is_passed(self, tmp_path, monkeypatch):
        code, seen = self.start(
            tmp_path, monkeypatch, ["--mode", "simulate", "--task", "initials-check", "--headless"]
        )
        assert code == 1 and "experimenter" not in seen

    def test_an_id_without_a_name_is_a_usage_error(self, tmp_path, monkeypatch, capsys):
        code, seen = self.start(
            tmp_path, monkeypatch, ["--mode", "simulate", "--experimenter-id", "e_1"]
        )
        assert code == 2 and seen == {}
        assert "needs --experimenter" in capsys.readouterr().err
