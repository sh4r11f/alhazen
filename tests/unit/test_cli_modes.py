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
