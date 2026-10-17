"""Refresh from real git history: a new commit becomes a new immutable version with
documentation bound to exactly that source, earlier versions and copies never change,
and ``check`` is a usable gate."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from alhazen.cli.main import main as alhazen_main
from alhazen.experiment_platform import refresh
from alhazen.experiment_platform.service import PlatformService, Principal
from alhazen.experiment_platform.store import FileStore

SCAFFOLD = (
    Path(__file__).resolve().parents[1] / "fixtures" / "platform" / "documentation" / "scaffold"
)
LAB = {"uid": "lab", "name": "The Lab"}
ADA = Principal("uada0000001", "Ada")


def git(repo: Path, *args: str) -> str:
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="T",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="T",
        GIT_COMMITTER_EMAIL="t@example.com",
    )
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env
    ).stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "fixation-demo"
    shutil.copytree(SCAFFOLD, root)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "fixation-demo"\nversion = "0.1.0"\n\n'
        '[tool.uv.sources]\nalhazen-vision = { git = "https://example.invalid/alhazen", '
        'tag = "v0.0.1" }\n',
        encoding="utf-8",
    )
    (root / "run.py").write_text(
        'PARAMETERS = {\n    "main": "configs/task.yaml",\n}\n', encoding="utf-8"
    )
    (root / "README.md").write_text("# Fixation demo\n\nLook at the point.\n", encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "first version")
    return root


@pytest.fixture()
def store(tmp_path: Path) -> FileStore:
    return FileStore(tmp_path / "store", create=True)


def spec(repo: Path, **kw) -> refresh.SourceSpec:  # type: ignore[no-untyped-def]
    return refresh.SourceSpec(repo=repo, ref="main", repo_label="lab/fixation-demo", **kw)


def test_a_first_refresh_publishes_a_version_with_current_documentation(
    store: FileStore, repo: Path
):
    [result] = refresh.refresh(store, [spec(repo)], LAB)
    assert result.status == "created" and result.docs_status == "current"
    listing = store.listing("lab", "fixation-demo")
    assert listing["source"]["commit"] == git(repo, "rev-parse", "HEAD").strip()
    assert listing["title"].startswith("Fixation demo") and listing["parameterSets"] == ["main"]
    version = store.version("lab", "fixation-demo", listing["bundle"]["id"])
    guide = json.loads(version["docs"]["guide"])
    params = json.loads(version["docs"]["parameterGuide"])
    assert guide["bundleId"] == listing["bundle"]["id"]
    assert params["sourceCommit"] == listing["source"]["commit"]
    assert "defaults inherited from alhazen are not listed" in " ".join(params["warnings"])


def test_an_unchanged_source_writes_nothing(store: FileStore, repo: Path):
    refresh.refresh(store, [spec(repo)], LAB)
    before = sorted(
        p.relative_to(store.root).as_posix() for p in store.root.rglob("*") if p.is_file()
    )
    [again] = refresh.refresh(store, [spec(repo)], LAB)
    after = sorted(
        p.relative_to(store.root).as_posix() for p in store.root.rglob("*") if p.is_file()
    )
    assert again.status == "unchanged" and before == after


def test_a_new_commit_is_a_new_version_and_copies_keep_theirs(store: FileStore, repo: Path):
    refresh.refresh(store, [spec(repo)], LAB)
    first = store.listing("lab", "fixation-demo")
    service = PlatformService(store)
    fork_id, fork = service.fork(ADA, "lab", "fixation-demo")
    snapshot = service.fork_docs(ADA, fork_id)["docs"]

    # The documented default no longer matches the params file: the validator refuses the
    # reader document for the new version instead of drawing a stale one.
    config = repo / "configs" / "task.yaml"
    config.write_text(config.read_text(encoding="utf-8").replace("0.3", "0.5", 1), encoding="utf-8")
    git(repo, "commit", "-qam", "bigger point")
    [result] = refresh.refresh(store, [spec(repo)], LAB)
    second = store.listing("lab", "fixation-demo")

    assert result.status == "created" and second["bundle"]["id"] != first["bundle"]["id"]
    assert result.docs_status == "partial"
    assert any("task guides could not be resolved" in w for w in result.warnings)
    new_docs = store.version("lab", "fixation-demo", second["bundle"]["id"])["docs"]
    assert "guide" not in new_docs  # never the previous version's guide with a new label
    assert json.loads(new_docs["parameterGuide"])["sourceCommit"] == second["source"]["commit"]
    # the earlier version, its documentation and the copy made from it are untouched
    assert (
        json.loads(store.version("lab", "fixation-demo", first["bundle"]["id"])["docs"]["guide"])[
            "bundleId"
        ]
        == first["bundle"]["id"]
    )
    assert service.forks(ADA)[fork_id]["head"]["id"] == fork["head"]["id"] == first["bundle"]["id"]
    assert service.fork_docs(ADA, fork_id)["docs"] == snapshot
    assert set(second["versions"]) == {first["bundle"]["id"], second["bundle"]["id"]}


def test_an_overlay_for_another_version_is_ignored_with_a_warning(store: FileStore, repo: Path):
    overlay = {"bundleId": "fixation-demo-0000000000", "tasks": []}
    [result] = refresh.refresh(store, [spec(repo, overlay=overlay)], LAB)
    assert result.docs_status == "current"
    assert any("fixation-demo-0000000000" in w for w in result.warnings)


def test_check_is_a_gate(store: FileStore, repo: Path):
    refresh.refresh(store, [spec(repo)], LAB)
    assert refresh.check(store, [spec(repo)]) == []
    (repo / "README.md").write_text("# Fixation demo\n\nChanged.\n", encoding="utf-8")
    git(repo, "commit", "-qam", "readme")
    problems = refresh.check(store, [spec(repo)])
    assert [p.problem for p in problems] == [problems[0].problem] and "refresh it" in problems[
        0
    ].problem
    # a listing whose stored documentation is bound to another version is stale
    listing = store.listing("lab", "fixation-demo")
    store.put_version(
        "lab",
        "other",
        "other-0000000000",
        {"listing": listing, "docs": {"guide": json.dumps({"bundleId": "zzz-0000000000"})}},
    )
    store.put_listing("lab", "other", {**listing, "versionKey": "other-0000000000"})
    assert any(p.where == "lab/other" and "stale" in p.problem for p in refresh.check(store))


def test_the_command_line_imports_and_checks(tmp_path: Path, repo: Path, capsys):
    store_dir = tmp_path / "cli-store"
    assert (
        alhazen_main(
            [
                "platform",
                "import",
                str(repo),
                "--ref",
                "main",
                "--store",
                str(store_dir),
                "--publisher-name",
                "Me",
            ]
        )
        == 0
    )
    assert "created" in capsys.readouterr().out
    assert alhazen_main(["platform", "check", "--store", str(store_dir)]) == 0
    forks_before = FileStore(store_dir).forks("local")
    assert forks_before == {}
    service = PlatformService(FileStore(store_dir))
    fork_id, _ = service.fork(Principal("local", "Me"), "local", "fixation-demo")
    dest = tmp_path / "checkout"
    assert (
        alhazen_main(["platform", "checkout", fork_id, str(dest), "--store", str(store_dir)]) == 0
    )
    (dest / "configs" / "task.yaml").write_text("fix_size_dva: 0.4\n", encoding="utf-8")
    assert (
        alhazen_main(
            ["platform", "save", fork_id, str(dest), "-m", "smaller", "--store", str(store_dir)]
        )
        == 0
    )
    fork = FileStore(store_dir).fork("local", fork_id)
    assert len(fork["revisions"]) == 2 and fork["head"]["id"] != fork["origin"]["bundle"]
    assert (
        alhazen_main(["platform", "import", str(tmp_path / "nothing"), "--store", str(store_dir)])
        == 1
    )
