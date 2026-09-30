from __future__ import annotations

import yaml

from alhazen.cli.main import main


def rig_file(tmp_path, valid=True, devices=None):
    path = tmp_path / "rig.yaml"
    body = {
        "monitor": {
            "width_px": 800,
            "height_px": 600,
            "width_cm": 40,
            "distance_cm": 57,
            "refresh_rate_hz": 120,
        },
        "display": {"backend": "simulated"},
        "data_root": str(tmp_path / "data"),
    }
    if devices is not None:
        body["devices"] = devices
    if not valid:
        body["monitor"].pop("width_px")
    path.write_text(yaml.safe_dump(body))
    return path


def test_validate_ok(tmp_path, capsys):
    assert main(["validate", "--rig", str(rig_file(tmp_path))]) == 0
    assert "OK" in capsys.readouterr().out


def test_validate_invalid(tmp_path, capsys):
    assert main(["validate", "--rig", str(rig_file(tmp_path, valid=False))]) == 1
    assert "INVALID" in capsys.readouterr().err


def test_no_command_prints_help(capsys):
    assert main([]) == 0
    assert "alhazen" in capsys.readouterr().out


def test_check_rig_reports_each_check_and_succeeds(tmp_path, capsys):
    rig = rig_file(
        tmp_path,
        devices={
            "reward": {"backend": "simulated"},
            "sync": {"backend": "simulated", "event_lines": {"TRIAL_START": "Dev1/line0"}},
        },
    )
    assert main(["check-rig", "--rig", str(rig)]) == 0
    out = capsys.readouterr().out
    for name in ("config", "data_root", "eyetracker", "reward", "sync"):
        assert f"OK   {name}:" in out
    # The one thing check-rig cannot verify is said out loud, so a clean run
    # is never mistaken for "the display works too".
    assert "display: untested" in out


def test_check_rig_pulse_fires_hardware(tmp_path, capsys):
    rig = rig_file(tmp_path, devices={"reward": {"backend": "simulated"}})
    assert main(["check-rig", "--rig", str(rig), "--pulse"]) == 0
    assert "fired one 50 ms pulse" in capsys.readouterr().out


def test_check_rig_exits_nonzero_on_a_failed_check(tmp_path, capsys):
    (tmp_path / "data").write_text("not a directory")
    assert main(["check-rig", "--rig", str(rig_file(tmp_path))]) == 1
    assert "FAIL data_root:" in capsys.readouterr().out


def test_check_rig_rejects_a_test_only_backend(tmp_path, capsys):
    rig = rig_file(tmp_path, devices={"eyetracker": {"backend": "scripted"}})
    assert main(["check-rig", "--rig", str(rig)]) == 1
    assert "INVALID" in capsys.readouterr().err


def test_python_dash_m_alhazen_is_the_same_command_line(tmp_path):
    """`python -m alhazen …` runs the same main() as the `alhazen` script.

    It is the way to start a long-running command (the dashboard) on Windows
    without holding `alhazen.exe` open, which would make the next reinstall of
    alhazen fail part-way (alhazen/__main__.py). Run as a real child process,
    because what is under test is that the package is runnable with -m at all:
    the exit status and the output are main()'s own.
    """
    import os
    import subprocess
    import sys

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    version = subprocess.run(
        [sys.executable, "-m", "alhazen", "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
    )
    assert version.returncode == 0, version.stderr
    assert version.stdout.startswith("alhazen ")

    checked = subprocess.run(
        [sys.executable, "-m", "alhazen", "validate", "--rig", str(rig_file(tmp_path))],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
    )
    assert checked.returncode == 0, checked.stderr
    assert "OK" in checked.stdout

    refused = subprocess.run(
        [sys.executable, "-m", "alhazen", "validate", "--rig", str(rig_file(tmp_path, False))],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
    )
    # main()'s nonzero status reaches the shell unchanged.
    assert refused.returncode == 1
    assert "INVALID" in refused.stderr
