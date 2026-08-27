"""Reading an alhazen run directory back into memory.

A run is five files and a manifest. This turns them into one object, and —
before handing anything over — verifies the manifest. A run whose files do not
match their recorded hashes is not a run to analyse quietly: something either
truncated a file or edited it, and which of those it was matters.

The three tables come back as pandas DataFrames with real dtypes, not as lists
of string dicts. That is not a convenience: a `csv.DictReader` row hands back
``row["success"] == "False"``, and ``"False"`` is truthy, so every consumer had
to remember to compare strings and any that forgot was quietly wrong in the
direction that looks correct.

The config snapshot is included deliberately. It is where every later step
gets the sync-line map, the screen geometry and the seed, rather than from a
notebook's own idea of what the rig was doing that day.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from alhazen.data.manifest import verify_manifest
from alhazen.errors import DataError

log = logging.getLogger(__name__)


@dataclass
class RunData:
    """One run: its tables, its config, and how its manifest checked out."""

    run_dir: Path
    base: str
    trials: pd.DataFrame
    events: pd.DataFrame
    frames: pd.DataFrame
    snapshot: dict[str, Any]
    manifest_problems: list[str] = field(default_factory=list)

    # -- the bits every later step asks for ----------------------------

    @property
    def config(self) -> dict[str, Any]:
        """The merged session config, as it ran."""
        return dict(self.snapshot.get("config", {}))

    @property
    def sync_event_lines(self) -> dict[str, str]:
        """Which event was wired to which physical line, from THIS run's own
        snapshot — never from a caller's idea of the rig."""
        devices = self.config.get("rig", {}).get("devices") or {}
        sync = devices.get("sync") or {}
        return dict(sync.get("event_lines") or {})

    @property
    def photodiode_events(self) -> list[str]:
        display = self.config.get("rig", {}).get("display") or {}
        photodiode = display.get("photodiode") or {}
        return list(photodiode.get("events") or [])

    def event_times(self, name: str) -> list[float]:
        """Session-clock times of every event with this name, in order."""
        if self.events.empty or "event" not in self.events:
            return []
        matching = self.events.loc[self.events["event"] == name, "t"]
        return [float(t) for t in matching]

    def outcome_counts(self) -> dict[str, int]:
        if self.trials.empty or "outcome" not in self.trials:
            return {}
        return {str(name): int(n) for name, n in self.trials["outcome"].value_counts().items()}


def load_run(run_dir: Path | str, verify: bool = True) -> RunData:
    """Read a run directory.

    ``verify=False`` exists for the one legitimate case — inspecting a run
    that is known to be damaged — and says so at the call site. The default
    checks, and records what it found on the returned object rather than
    raising, so a caller can report the damage alongside everything else it
    was going to say.
    """
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise DataError(f"not a run directory: {run_dir}")

    trials_files = sorted(run_dir.glob("*_trials.csv"))
    if not trials_files:
        raise DataError(
            f"{run_dir} holds no *_trials.csv — it is not an alhazen run directory "
            f"(or the session never got as far as writing one)"
        )
    base = trials_files[0].name[: -len("_trials.csv")]

    snapshot_path = run_dir / "config_snapshot.yaml"
    if not snapshot_path.exists():
        # Every later step reads the rig's configuration from here; without
        # it, an analysis would have to be told what the session did, which
        # is exactly the failure mode this layer exists to prevent.
        raise DataError(
            f"{run_dir} has no config_snapshot.yaml — analysis reads a session's own "
            f"configuration, and there is none here to read"
        )

    problems: list[str] = []
    if verify:
        manifest_path = run_dir / "manifest.yaml"
        if manifest_path.exists():
            problems = verify_manifest(run_dir, manifest_path)
            if problems:
                log.error("manifest check failed for %s: %s", run_dir, problems)
        else:
            problems = ["manifest.yaml is missing"]

    return RunData(
        run_dir=run_dir,
        base=base,
        trials=_read_csv(run_dir / f"{base}_trials.csv"),
        events=_read_csv(run_dir / f"{base}_events.csv"),
        frames=_read_csv(run_dir / f"{base}_frames.csv"),
        snapshot=yaml.safe_load(snapshot_path.read_text()) or {},
        manifest_problems=problems,
    )


def _read_csv(path: Path) -> pd.DataFrame:
    """One table, typed, or an empty frame when the session never wrote it.

    A missing table is normal for a session that ended early; a caller that
    needs one checks for itself rather than being handed a crash here.

    Dtypes come from pandas' own inference, which is what makes the numeric
    columns numeric and the True/False columns boolean. An empty cell becomes
    NaN rather than ``""`` — "this trial recorded no reaction time" and "this
    trial's reaction time was the empty string" are not the same claim.
    """
    if not path.exists():
        log.warning("%s is missing; treating it as empty", path.name)
        return pd.DataFrame()
    return pd.read_csv(path)


def event_payloads(events: pd.DataFrame, name: str) -> list[dict[str, Any]]:
    """Decoded payloads of the named events.

    The recorder writes payloads as JSON so they survive a round trip; this
    is the other half of that, kept here rather than left to each caller
    guessing the encoding.
    """
    if events.empty or "event" not in events:
        return []
    payloads = []
    for _, row in events.loc[events["event"] == name].iterrows():
        raw = row.get("payload_json")
        if raw is None or (isinstance(raw, float) and pd.isna(raw)):
            raw = "{}"
        try:
            payloads.append(json.loads(str(raw)))
        except json.JSONDecodeError as error:
            raise DataError(
                f"event {name!r} at t={row.get('t')} has an unreadable payload: {error}"
            ) from error
    return payloads
