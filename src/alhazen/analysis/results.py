"""Results bundles: an output directory that says how it was made.

An analysis that writes a CSV and nothing else is an analysis nobody can
reproduce — not because the code is gone, but because which inputs, which
parameters and which version produced *that particular file* is gone. A
bundle is the same outputs plus a manifest recording exactly that.

Deliberately small: a directory, some tables, one manifest. Nothing here
decides what an analysis computes.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alhazen.version import get_version

log = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.json"
SCHEMA_VERSION = 1


@dataclass
class ResultsBundle:
    """An output directory being built."""

    out_dir: Path
    parameters: dict[str, Any] = field(default_factory=dict)
    inputs: list[dict[str, Any]] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.out_dir = Path(self.out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def add_input(self, path: Path | str, role: str = "input") -> None:
        """Record an input file and its hash.

        The hash is what makes the bundle answerable later: "was this made
        from the run I think it was" is otherwise a question about
        filenames, which get renamed.
        """
        path = Path(path)
        entry: dict[str, Any] = {"role": role, "path": str(path)}
        if path.is_file():
            entry["sha256"] = _sha256(path)
            entry["bytes"] = path.stat().st_size
        else:
            # A directory input (a recording run) is recorded by name and by
            # what it contained, since hashing gigabytes to identify it
            # would cost more than it is worth.
            entry["kind"] = "directory"
            entry["contents"] = sorted(p.name for p in path.glob("*"))[:50]
        self.inputs.append(entry)

    def write_table(self, name: str, rows: list[dict[str, Any]]) -> Path:
        """Write one output table and remember it."""
        import csv

        path = self.out_dir / name
        if rows:
            with path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        else:
            # An empty result is a result; writing nothing would be
            # indistinguishable from the analysis never running.
            path.write_text("")
        self.outputs.append(name)
        return path

    def write_manifest(self) -> Path:
        """Close the bundle. Everything above, plus the version that made it."""
        path = self.out_dir / MANIFEST_NAME
        path.write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "written": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "alhazen_version": get_version(),
                    "parameters": self.parameters,
                    "inputs": self.inputs,
                    "outputs": self.outputs,
                },
                indent=2,
                sort_keys=False,
            )
        )
        log.info("results bundle written to %s (%d outputs)", self.out_dir, len(self.outputs))
        return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
