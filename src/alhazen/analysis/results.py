"""Results bundles: an output directory that says how it was made.

An analysis that writes a CSV and nothing else is an analysis nobody can
reproduce — not because the code is gone, but because which inputs, which
parameters and which version produced *that particular file* is gone. A
bundle is the same outputs plus a manifest recording exactly that.

Deliberately small: a directory, some tables, one manifest. Nothing here
decides what an analysis computes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alhazen.data.manifest import sha256_file
from alhazen.version import get_version

log = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.json"
SCHEMA_VERSION = 1


@dataclass
class ResultsBundle:
    """An output directory being built.

    An ``out_dir`` that already holds files is reused, not refused: a report
    re-run into its own ``analysis/`` directory is the normal case for the
    experiments built on this (mbri's reports do exactly that, and nest one
    report's directory inside another's). But reuse is not silent. The files
    found on opening are logged as a warning, and whichever of them this
    bundle does not rewrite are listed under ``preexisting`` in the manifest,
    so an earlier run's leftover output cannot pass for this run's.
    """

    out_dir: Path
    parameters: dict[str, Any] = field(default_factory=dict)
    inputs: list[dict[str, Any]] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    # Relative (forward-slash) paths of the files out_dir held when the
    # bundle opened, previous manifest excluded. Set in __post_init__.
    found_on_open: list[str] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.out_dir = Path(self.out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        # The previous manifest is left out: this bundle's replaces it, so it
        # is not a leftover anyone could mistake for an output.
        self.found_on_open = [
            path.relative_to(self.out_dir).as_posix()
            for path in sorted(self.out_dir.rglob("*"))
            if path.is_file() and path != self.out_dir / MANIFEST_NAME
        ]
        if self.found_on_open:
            log.warning(
                "results directory %s already holds %d file(s) from an earlier run: %s. "
                "Any this bundle does not rewrite will be listed as 'preexisting' in its "
                "manifest; use a fresh directory to keep one run's outputs alone.",
                self.out_dir,
                len(self.found_on_open),
                ", ".join(self.found_on_open[:10])
                + (" ..." if len(self.found_on_open) > 10 else ""),
            )

    def add_input(self, path: Path | str, role: str = "input") -> None:
        """Record an input file and its hash.

        The hash is what makes the bundle answerable later: "was this made
        from the run I think it was" is otherwise a question about
        filenames, which get renamed.
        """
        path = Path(path)
        entry: dict[str, Any] = {"role": role, "path": str(path)}
        if path.is_file():
            entry["sha256"] = sha256_file(path)
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
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        else:
            # An empty result is a result; writing nothing would be
            # indistinguishable from the analysis never running.
            path.write_text("", encoding="utf-8")
        self.outputs.append(name)
        return path

    def write_manifest(self) -> Path:
        """Close the bundle. Everything above, plus the version that made it.

        ``preexisting`` is what was in ``out_dir`` before this bundle opened
        and is not among its ``outputs`` — computed here rather than on
        opening, because callers may add an output (a figure) to ``outputs``
        directly after the bundle is made.
        """
        path = self.out_dir / MANIFEST_NAME
        written = set(self.outputs)
        preexisting = [name for name in self.found_on_open if name not in written]
        path.write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "written": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "alhazen_version": get_version(),
                    "parameters": self.parameters,
                    "inputs": self.inputs,
                    "outputs": self.outputs,
                    "preexisting": preexisting,
                },
                indent=2,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        log.info("results bundle written to %s (%d outputs)", self.out_dir, len(self.outputs))
        return path
