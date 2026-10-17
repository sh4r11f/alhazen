"""Reading an experiment from git: committed files only, at an exact commit."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from alhazen.experiment_platform import sources
from alhazen.experiment_platform.errors import SourceError


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
def collection(tmp_path: Path) -> Path:
    repo = tmp_path / "experiments"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for folder in ("classics/alpha", "contributed/someone/beta"):
        (repo / folder).mkdir(parents=True)
        (repo / folder / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.1.0"\n')
        (repo / folder / "run.py").write_text("PARAMETERS = {}\n")
    (repo / "classics" / "alpha" / "go.sh").write_text("#!/bin/sh\n")
    (repo / "classics" / "alpha" / "go.sh").chmod(0o755)
    (repo / "README.md").write_text("collection\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "first")
    return repo


def test_a_folder_is_read_relative_to_its_root(collection: Path):
    tree = sources.read_tree(collection, "main", "classics/alpha")
    assert sorted(tree.files) == ["go.sh", "pyproject.toml", "run.py"]
    assert tree.subdir == "classics/alpha" and len(tree.commit) == 40
    if os.name == "posix":
        assert tree.executable == {"go.sh"}
    assert tree.subject == "first"


def test_uncommitted_edits_never_reach_the_catalogue(collection: Path):
    (collection / "classics" / "alpha" / "run.py").write_text("EDITED\n")
    (collection / "classics" / "alpha" / "untracked.py").write_text("x\n")
    tree = sources.read_tree(collection, "main", "classics/alpha")
    assert tree.text("run.py") == "PARAMETERS = {}\n" and "untracked.py" not in tree.files


def test_an_older_commit_gives_the_older_files(collection: Path):
    first = sources.resolve_commit(collection, "main")
    (collection / "classics" / "alpha" / "run.py").write_text("PARAMETERS = {'a': 1}\n")
    git(collection, "commit", "-qam", "second")
    assert (
        sources.read_tree(collection, first, "classics/alpha").text("run.py") == "PARAMETERS = {}\n"
    )
    assert "'a'" in sources.read_tree(collection, "main", "classics/alpha").text("run.py")


def test_collection_folders_are_found(collection: Path):
    assert sources.collection_folders(collection, "main") == [
        "classics/alpha",
        "contributed/someone/beta",
    ]


def test_errors_name_the_problem_not_the_machine(collection: Path, tmp_path: Path):
    with pytest.raises(SourceError, match="no files"):
        sources.read_tree(collection, "main", "classics/missing")
    with pytest.raises(SourceError):
        sources.read_tree(collection, "no-such-branch")
    with pytest.raises(SourceError, match="not a git repository"):
        sources.read_tree(tmp_path / "nothing")


@pytest.mark.skipif(os.name != "posix", reason="symbolic links need POSIX permissions here")
def test_a_committed_symlink_is_refused(collection: Path):
    (collection / "classics" / "alpha" / "link").symlink_to("/etc/hostname")
    git(collection, "add", "-A")
    git(collection, "commit", "-qm", "link")
    with pytest.raises(SourceError, match="symbolic link"):
        sources.read_tree(collection, "main", "classics/alpha")
