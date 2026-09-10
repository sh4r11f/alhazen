"""The per-run config snapshot: what makes a session reproducible after the
fact.

Written *before* trial 1 (the runner enforces the ordering): a session that
crashes partway still documents exactly what it was trying to run. Contents:
the fully-merged SessionConfig plus environment provenance — package
versions, BOTH git trees (the experiment's and alhazen's own), platform, and a
digest of every installed distribution so "same config, different environment"
is detectable later.

alhazen's own tree is recorded because its version number does not identify
its code between releases: `main` carries the last release's number until the
next one is cut, so a run made from a source checkout of `main` records a
version that several different trees share.
"""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

import yaml

from alhazen.config.models import SessionConfig
from alhazen.version import DISTRIBUTION, get_version


def _git_sha(cwd: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def _alhazen_git_describe(package_dir: Path) -> str:
    """What `git describe` says about alhazen's OWN source tree — and only that.

    Three answers, kept apart because collapsing them hides what this is for:

    - ``"v1.3.1"`` or ``"v1.3.1-2-gabc1234"``, either with ``-dirty`` — the
      real answer. The ``-2-`` is the point: the tree is two commits past the
      tag whose number `alhazen_version` reports.
    - ``"not a source checkout"`` — alhazen is not running from a git clone
      of itself, which is what a wheel install looks like, and it means the
      version number alone identifies the code.
    - ``"unknown"`` — git could not be run, or could not answer.

    The ownership check is not optional. A wheel installed into a virtualenv
    that lives inside another repository — an experiment's ``.venv/`` — sits
    inside that repository's work tree, and git describes it without
    complaint: tried on a scratch repo, the experiment's own tag came back as
    alhazen's. Recording someone else's commit under alhazen's name is the
    wrong-attribution bug this key exists to fix, so the tree has to prove it
    is alhazen's own: its top level must hold the pyproject that declares
    this distribution.
    """

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(package_dir), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
            # English messages whatever the machine's language, because the
            # answer below depends on reading one of git's error messages.
            env={**os.environ, "LC_ALL": "C", "LANGUAGE": "C"},
        ).stdout.strip()

    try:
        top = Path(git("rev-parse", "--show-toplevel"))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"  # no git on this machine, or it did not answer
    except subprocess.CalledProcessError as error:
        # Only git's own "not a git repository" means what the label says. Any
        # other refusal is git failing to answer: a directory that does not
        # exist, or a repository git will not open for this user ("dubious
        # ownership"). Calling those "not a source checkout" would be a
        # confident wrong answer where an honest "unknown" belongs.
        if "not a git repository" in (error.stderr or ""):
            return "not a source checkout"
        return "unknown"

    # Whose tree is it? Compared line by line with spaces removed, so a
    # dependency line such as `"alhazen-vision>=1.3"` in an experiment's own
    # pyproject can never pass for the declaration.
    try:
        lines = (top / "pyproject.toml").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    declaration = f'name="{DISTRIBUTION}"'
    if not any(line.replace(" ", "") == declaration for line in lines):
        return "not a source checkout"  # installed inside somebody else's tree

    try:
        return git("describe", "--always", "--dirty")
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        # alhazen's own tree, but git cannot describe it — a clone with no
        # commits. Calling that "not a source checkout" would be wrong.
        return "unknown"


def environment_digest() -> str:
    """A stable sha256 over every installed distribution's name and version.

    Not a lockfile — a fingerprint: two sessions with the same digest ran in
    byte-comparable environments; a differing digest says exactly when to go
    look at what changed.
    """
    lines = sorted(
        f"{dist.metadata['Name']}=={dist.version}"
        for dist in metadata.distributions()
        if dist.metadata["Name"] is not None
    )
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def build_provenance(experiment_dir: Path | None = None) -> dict[str, str]:
    """What produced this run: versions, both git trees, and the environment.

    ``alhazen_version`` comes from :func:`alhazen.version.get_version`, which
    looks up the right distribution. This module looked up ``"alhazen"``
    directly, which is the trap version.py exists to close: the name belongs
    to an unrelated project on PyPI, so the lookup either found theirs and
    stamped their version into the data, or — the case here — found nothing
    and wrote ``"unknown"`` into every snapshot alhazen has ever produced.

    ``alhazen_git_describe`` is the other half of the same question, and the
    reason a version number alone is not enough. Between releases `main`
    carries the previous release's number, so a run made from a source
    checkout records a version that does not identify the code: three
    downstream repos install alhazen by cloning `main`, and every one of them
    has run code that its version string did not describe. The describe
    string does describe it — tag, commits past the tag, and dirty.
    """
    return {
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "alhazen_version": get_version(),
        "alhazen_git_describe": _alhazen_git_describe(Path(__file__).resolve().parent),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "experiment_git_sha": _git_sha(experiment_dir or Path.cwd()),
        "environment_digest": environment_digest(),
    }


def write_snapshot(cfg: SessionConfig, path: Path, experiment_dir: Path | None = None) -> None:
    payload = {
        "config": cfg.model_dump(mode="json"),
        "provenance": build_provenance(experiment_dir),
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
