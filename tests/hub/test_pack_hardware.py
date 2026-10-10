"""The hub pack metadata suggestion reads hardware needs from the
experiment's own configuration (found importing sh4r11f/mbri: every package
said no eye tracker and no reward)."""

from __future__ import annotations

from pathlib import Path

from alhazen.hub.source import declared_hardware, suggest_metadata


def _write(root: Path, name: str, text: str) -> None:
    path = root / "configs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_no_configs_suggests_display_only(tmp_path: Path) -> None:
    assert declared_hardware(tmp_path) == {"display": True, "eye_tracker": False, "reward": False}


def test_monkey_params_and_tracker_rig(tmp_path: Path) -> None:
    _write(tmp_path, "task.yaml", "subject_kind: human\n")
    _write(
        tmp_path, "task-monkey.yaml", "subject_kind: monkey\nreward:\n  success: {n_pulses: 1}\n"
    )
    _write(
        tmp_path,
        "rig-lab.yaml",
        "extends: lab\ndevices:\n  eyetracker:\n    calibration_type: HV5\n",
    )
    hardware = suggest_metadata(tmp_path)["hardware"]
    assert hardware == {"display": True, "eye_tracker": True, "reward": True}


def test_reward_block_alone_counts(tmp_path: Path) -> None:
    _write(tmp_path, "sub/task.yaml", "reward:\n  success: {n_pulses: 2}\n")
    assert declared_hardware(tmp_path)["reward"] is True


def test_human_only_and_stand_in_tracker_need_neither(tmp_path: Path) -> None:
    _write(tmp_path, "task.yaml", "subject_kind: human\nreward: null\n")
    _write(tmp_path, "rig-rehearsal.yaml", "devices:\n  eyetracker:\n    backend: mouse_sim\n")
    _write(tmp_path, "rig-other.yaml", "devices:\n  sync: {backend: simulated}\n")
    assert declared_hardware(tmp_path) == {"display": True, "eye_tracker": False, "reward": False}


def test_unreadable_files_are_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "broken.yaml", "a: [unclosed\n")
    _write(tmp_path, "list.yaml", "- 1\n- 2\n")
    (tmp_path / "configs" / "bad.yml").write_bytes(b"\xff\xfe\x00")
    assert declared_hardware(tmp_path)["reward"] is False
