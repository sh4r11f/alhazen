"""Reading an experiment's source at an exact version, from git, without running it.

The catalogue is built from git objects, never from a work tree: ``git ls-tree`` names the
files at a commit and ``git cat-file`` reads them, so uncommitted edits, ignored files and
local data can never leak into a published version, and the version a listing names is
the version its files came from. Nothing here checks anything out or executes anything.

An experiment is either a whole repository (``subdir=""``) or one folder of a collection
repository: ``classics/<name>`` or ``contributed/<author>/<name>``, each a complete
alhazen project with its own pyproject.toml.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from alhazen.experiment_platform.errors import SourceError
from alhazen.experiment_platform.safety import safe_relative

# A source tree larger than this is not an experiment; refusing early keeps a mistaken
# path (a home folder, a data repository) from being read into memory.
MAX_SOURCE_BYTES = 256 * 1024 * 1024


def _git(repo: Path, *args: str) -> bytes:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("GIT_TRACE") and k != "GIT_CURL_VERBOSE"
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        out = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, check=False, env=env, timeout=300
        )
    except FileNotFoundError as error:
        raise SourceError("git is not installed or not on PATH") from error
    except subprocess.TimeoutExpired as error:
        raise SourceError(f"git {args[0]} took too long") from error
    if out.returncode != 0:
        message = out.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise SourceError(f"git {args[0]} failed: {message[-1] if message else 'no message'}")
    return out.stdout


@dataclass(frozen=True)
class SourceTree:
    """The files of one experiment at one commit. ``files`` maps a path relative to the
    experiment root to its bytes; ``executable`` holds the paths with the executable bit."""

    repo: Path
    ref: str
    commit: str
    subdir: str
    files: dict[str, bytes]
    executable: frozenset[str] = field(default_factory=frozenset)
    commit_date: str = ""
    subject: str = ""
    folder_commit: str = ""

    def text(self, path: str) -> str:
        raw = self.files.get(path)
        return raw.decode("utf-8", errors="replace") if raw is not None else ""

    def blobs(self) -> list[tuple[str, bytes, int]]:
        return [(p, b, 1 if p in self.executable else 0) for p, b in sorted(self.files.items())]


def resolve_commit(repo: Path, ref: str) -> str:
    """The full commit id ``ref`` names in ``repo``."""
    repo = Path(repo)
    if not (repo / ".git").exists() and not (repo / "HEAD").exists():
        raise SourceError(f"{repo.name!r} is not a git repository")
    return _git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}").decode().strip()


def read_tree(repo: Path, ref: str = "HEAD", subdir: str = "") -> SourceTree:
    """Every file under ``subdir`` (or the whole repository) at ``ref``, read from git.
    Submodules and symbolic links are refused rather than followed."""
    repo = Path(repo)
    commit = resolve_commit(repo, ref)
    sub = subdir.strip("/")
    if sub:
        safe_relative(sub)
    listing = _git(repo, "ls-tree", "-r", "-z", "-l", commit, "--", (sub + "/") if sub else ".")
    files: dict[str, bytes] = {}
    executable: set[str] = set()
    total = 0
    for record in listing.decode("utf-8").split("\0"):
        if not record:
            continue
        head, path = record.split("\t", 1)
        mode, kind, sha, size = head.split()
        rel = path[len(sub) + 1 :] if sub else path
        if kind == "commit":
            raise SourceError(f"{rel!r} is a git submodule; submodules are not supported")
        if kind != "blob":
            continue
        if mode == "120000":
            raise SourceError(f"{rel!r} is a symbolic link; links are not supported in experiments")
        total += int(size)
        if total > MAX_SOURCE_BYTES:
            raise SourceError("The source is larger than an experiment can be")
        files[safe_relative(rel)] = _git(repo, "cat-file", "blob", sha)
        if mode == "100755":
            executable.add(rel)
    if not files:
        where = f"folder {sub!r}" if sub else "repository"
        raise SourceError(f"The {where} has no files at {commit[:7]}")
    date = _git(repo, "log", "-1", "--format=%cI", commit).decode().strip()
    path_args = ["--", sub] if sub else []
    subject = _git(repo, "log", "-1", "--format=%s", commit, *path_args).decode().strip()
    folder_commit = (
        _git(repo, "log", "-1", "--format=%H", commit, *path_args).decode().strip() or commit
    )
    return SourceTree(
        repo, ref, commit, sub, files, frozenset(executable), date, subject, folder_commit
    )


def collection_folders(repo: Path, ref: str = "HEAD") -> list[str]:
    """The experiment folders of a collection repository at ``ref``: every
    ``classics/<name>`` and ``contributed/<author>/<name>`` holding a pyproject.toml."""
    commit = resolve_commit(Path(repo), ref)
    names = (
        _git(Path(repo), "ls-tree", "-r", "--name-only", "-z", commit).decode("utf-8").split("\0")
    )
    found = set()
    for name in names:
        parts = name.split("/")
        if parts[0] == "classics" and len(parts) == 3 and parts[2] == "pyproject.toml":
            found.add("/".join(parts[:2]))
        elif parts[0] == "contributed" and len(parts) == 4 and parts[3] == "pyproject.toml":
            found.add("/".join(parts[:3]))
    return sorted(found)


def is_pushed(repo: Path, commit: str) -> bool:
    """Whether any remote-tracking branch contains ``commit`` (False with no remote)."""
    return bool(_git(Path(repo), "branch", "-r", "--contains", commit).strip())
