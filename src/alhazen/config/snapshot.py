"""The per-run config snapshot: what makes a session reproducible after the
fact.

Written *before* trial 1 (the runner enforces the ordering): a session that
crashes partway still documents exactly what it was trying to run. Contents:
the fully-merged SessionConfig plus environment provenance — package
versions, git SHA, platform, and a digest of every installed distribution so
"same config, different environment" is detectable later.
"""

from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

import yaml

from alhazen.config.models import SessionConfig


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
    try:
        alhazen_version = metadata.version("alhazen")
    except metadata.PackageNotFoundError:
        alhazen_version = "unknown"
    return {
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "alhazen_version": alhazen_version,
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
    path.write_text(yaml.safe_dump(payload, sort_keys=False))
