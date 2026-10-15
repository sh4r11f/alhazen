"""What `hub pack` suggests for an experiment folder: the reward line and the
documentation descriptor (found while importing amodal-averaging)."""

from __future__ import annotations

from pathlib import Path

import pytest

from alhazen.hub import cli
from alhazen.hub.source import suggest_metadata


def _experiment(root: Path, params: dict[str, str]) -> Path:
    (root / "configs").mkdir(parents=True)
    (root / "run.py").write_text("TASKS = {}\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.2.0"\n', encoding="utf-8"
    )
    for name, text in params.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def test_human_only_experiment_needs_no_reward_line(tmp_path):
    root = _experiment(tmp_path / "e", {"configs/task.yaml": "subject_kind: human\n"})
    assert suggest_metadata(root)["hardware"]["reward"] is False


def test_monkey_params_suggest_the_reward_line(tmp_path):
    root = _experiment(
        tmp_path / "e",
        {
            "configs/task.yaml": "subject_kind: human\n",
            "configs/task-monkey.yaml": (
                "# a monkey file\nsubject_kind: monkey  # pays\nreward: {}\n"
            ),
        },
    )
    assert suggest_metadata(root)["hardware"]["reward"] is True


def test_reward_guess_reads_only_packed_files_and_ignores_legacy(tmp_path):
    root = _experiment(
        tmp_path / "e",
        {
            "configs/task.yaml": "subject_kind: human\n",
            "configs/task-monkey.yaml": "subject_kind: monkey\n",
            "configs/legacy/old.yaml": "subject_kind: monkey\n",
        },
    )
    assert suggest_metadata(root, ["run.py", "configs/task.yaml"])["hardware"]["reward"] is False
    assert (
        suggest_metadata(root, ["configs/task.yaml", "configs/legacy/old.yaml"])["hardware"][
            "reward"
        ]
        is False
    )
    assert suggest_metadata(root, ["configs/task-monkey.yaml"])["hardware"]["reward"] is True


def test_a_comment_or_another_key_is_not_a_monkey_declaration(tmp_path):
    root = _experiment(
        tmp_path / "e",
        {"configs/task.yaml": "# subject_kind: monkey\nnot_subject_kind: monkey\n"},
    )
    assert suggest_metadata(root)["hardware"]["reward"] is False


def test_documentation_descriptor_is_suggested_only_when_packed(tmp_path):
    root = _experiment(tmp_path / "e", {"docs/experiment.json": "{}"})
    assert suggest_metadata(root)["documentation"] == "docs/experiment.json"
    assert suggest_metadata(root, ["run.py", "docs/experiment.json"])["documentation"] == (
        "docs/experiment.json"
    )
    assert "documentation" not in suggest_metadata(root, ["run.py"])
    bare = _experiment(tmp_path / "bare", {})
    assert "documentation" not in suggest_metadata(bare)


def test_parse_hardware():
    assert cli.parse_hardware("eye_tracker, reward") == {
        "display": False,
        "eye_tracker": True,
        "reward": True,
    }
    assert cli.parse_hardware("none") == {"display": False, "eye_tracker": False, "reward": False}
    with pytest.raises(ValueError, match="Unknown hardware"):
        cli.parse_hardware("eyetracker")
