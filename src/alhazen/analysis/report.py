"""The session report: what happened, and whether the data is sound.

Run after a session, and again after the neural files exist. It answers the
questions someone actually asks about a run — how many trials, of what
outcomes, how many frames were dropped, does the manifest still check out,
and (with a recording) do the two clocks line up — and writes the answers
beside the data as ``report.yaml``.

Written as a *report* rather than a check that passes or fails silently:
every number is printed, including the ones that are fine, because "0 dropped
frames" is a fact worth having in the record and not merely the absence of a
warning.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from alhazen.analysis.io import spikeglx
from alhazen.analysis.io.session import RunData, load_run
from alhazen.analysis.photodiode import PhotodiodeReport, measure_from_recording
from alhazen.analysis.sync import AlignmentFit, align_run, event_bit_map
from alhazen.data.manifest import write_manifest
from alhazen.errors import AlhazenError

log = logging.getLogger(__name__)

REPORT_FILENAME = "report.yaml"


@dataclass
class SessionReport:
    """Everything the report knows, before it is printed or written."""

    run_dir: Path
    identity: dict[str, Any]
    trials: dict[str, Any]
    frames: dict[str, Any]
    manifest_problems: list[str]
    alignment: AlignmentFit | None = None
    photodiode: PhotodiodeReport | None = None
    # Events emitted vs pulses recorded, for EVERY line the rig mapped — not
    # only the one the alignment happened to fit on. A line that was wired
    # but never pulsed is a rig fault, and it is invisible in a report that
    # only describes the busiest line.
    sync_lines: dict[str, dict[str, Any]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether anything was found that should stop this run being used."""
        return not self.manifest_problems and not self.problems

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "run_dir": str(self.run_dir),
            "identity": self.identity,
            "trials": self.trials,
            "frames": self.frames,
            "manifest_problems": self.manifest_problems,
            "problems": self.problems,
            "ok": self.ok,
        }
        if self.alignment is not None:
            data["alignment"] = self.alignment.to_dict()
        if self.photodiode is not None:
            data["photodiode"] = self.photodiode.to_dict()
        if self.sync_lines:
            data["sync_lines"] = self.sync_lines
        return data

    def render(self) -> str:
        """The human-readable form, which is what most people will read."""
        lines = [f"run: {self.run_dir}"]
        identity = self.identity
        lines.append(
            f"  subject {identity.get('subject')}  session {identity.get('session')}  "
            f"run {identity.get('run')}  task {identity.get('task')}  "
            f"seed {identity.get('seed')}"
        )
        lines.append(
            f"  trials: {self.trials['n_rows']} rows, "
            f"{self.trials['completed']} completed "
            f"({self.trials['completed_rate']:.0%} of attempts)"
        )
        for outcome, count in sorted(self.trials["outcomes"].items()):
            lines.append(f"    {outcome}: {count}")
        frames = self.frames
        lines.append(
            f"  frames: {frames['n_frames']} recorded, {frames['n_dropped']} dropped "
            f"({frames['dropped_rate']:.2%}), worst interval "
            f"{frames['worst_interval_ms']:.1f} ms"
        )
        by_trial = frames.get("dropped_by_trial") or {}
        if by_trial:
            worst = sorted(by_trial.items(), key=lambda item: (-item[1], item[0]))
            listed = ", ".join(f"trial {index}: {count}" for index, count in worst[:10])
            more = "" if len(worst) <= 10 else f", and {len(worst) - 10} more"
            lines.append(f"    dropped in {len(by_trial)} trial(s) — {listed}{more}")
        if self.manifest_problems:
            lines.append(f"  manifest: FAILED — {len(self.manifest_problems)} problem(s)")
            lines.extend(f"    {problem}" for problem in self.manifest_problems)
        else:
            lines.append("  manifest: verified")
        if self.alignment is not None:
            fit = self.alignment
            lines.append(
                f"  alignment on {fit.event}: {fit.n_matched}/{fit.n_behavior} events "
                f"matched ({fit.n_extra_pulses} extra pulses), drift {fit.drift_ppm:.0f} ppm, "
                f"residual {fit.residual_rms_ms:.3f} ms rms / {fit.residual_max_ms:.3f} ms max"
            )
        for event, counts in sorted(self.sync_lines.items()):
            pulses = counts.get("n_pulses")
            recorded = "unreadable" if pulses is None else str(pulses)
            lines.append(
                f"  sync {event} on {counts['line']}: {counts['n_events']} events emitted, "
                f"{recorded} pulses recorded"
            )
        if self.photodiode is not None:
            diode = self.photodiode
            lines.append(
                f"  display latency: {diode.median_latency_ms:.1f} ms median "
                f"(± {diode.jitter_ms:.1f} ms), from {diode.n_matched} of {diode.n_events} "
                f"marked events"
            )
        for problem in self.problems:
            lines.append(f"  problem: {problem}")
        return "\n".join(lines)

    def save(self, run_dir: Path | None = None) -> Path:
        """Write ``report.yaml`` into the run directory and re-hash the run.

        The rewrite is not optional bookkeeping. `verify_manifest` reports
        any unlisted file as a problem, so a report saved without one would
        make the *next* report on the same run come back not-ok — the tool
        would break the thing it exists to check.
        """
        directory = Path(run_dir or self.run_dir)
        path = directory / REPORT_FILENAME
        path.write_text(yaml.safe_dump(self.to_dict(), sort_keys=False))
        write_manifest(directory, directory / "manifest.yaml")
        return path


def build_report(
    run_dir: Path | str,
    neural_run_dir: Path | str | None = None,
    analog_channel: int = 0,
) -> SessionReport:
    """Read a run and describe it.

    A missing or failed alignment is recorded as a problem rather than
    raised: the rest of the report is still worth having, and a caller that
    needs the run to be aligned checks ``ok``.
    """
    run = load_run(run_dir)
    report = SessionReport(
        run_dir=Path(run_dir),
        identity=_identity(run),
        trials=_trials(run),
        frames=_frames(run),
        manifest_problems=list(run.manifest_problems),
    )

    if neural_run_dir is not None:
        report.sync_lines = _sync_line_counts(run, neural_run_dir)
        try:
            report.alignment = align_run(run, neural_run_dir)
            report.alignment.save(run.run_dir)
        except AlhazenError as error:
            # Refusing to align is a finding, not a crash: the report should
            # still say how the session itself went.
            report.problems.append(f"alignment refused: {error}")

        if report.alignment is not None and run.photodiode_events:
            try:
                report.photodiode = measure_from_recording(
                    run, neural_run_dir, report.alignment, analog_channel=analog_channel
                )
            except AlhazenError as error:
                report.problems.append(f"photodiode reconciliation failed: {error}")

    return report


def _sync_line_counts(run: RunData, neural_run_dir: Path | str) -> dict[str, dict[str, Any]]:
    """Events emitted against pulses recorded, line by line.

    Every mapped line, not just the one the alignment fits on: a line that
    was wired but never pulsed — a loose BNC, a mistyped device name — is a
    rig fault the busiest line's fit cannot reveal. A line that cannot be
    read records its own reason instead of taking the report down with it.
    """
    lines = run.sync_event_lines
    if not lines:
        return {}
    try:
        bits = event_bit_map(lines)
        files = spikeglx.find_run_files(neural_run_dir)
    except AlhazenError as error:
        log.warning("cannot count sync pulses: %s", error)
        return {}
    counts: dict[str, dict[str, Any]] = {}
    for event, bit in sorted(bits.items()):
        entry: dict[str, Any] = {
            "line": lines[event],
            "bit": bit,
            "n_events": len(run.event_times(event)),
        }
        try:
            pulses = spikeglx.digital_word_edges(
                files["bin_path"], files["meta_path"], bit_index=bit, edge="rising"
            )
            entry["n_pulses"] = len(pulses)
        except AlhazenError as error:
            entry["n_pulses"] = None
            entry["problem"] = str(error)
        counts[event] = entry
    return counts


def _as_bool(column: pd.Series) -> pd.Series:
    """A CSV boolean column as real booleans, with missing read as False.

    Written out rather than `astype(bool)`: a column that carries blanks
    arrives as object dtype holding a mix of True, False and NaN, and NaN is
    truthy under `bool()`.
    """
    return column.map(lambda value: value is True or str(value).strip().lower() == "true")


def _identity(run: RunData) -> dict[str, Any]:
    info = run.config.get("info", {})
    return {
        "subject": info.get("subject"),
        "session": info.get("session"),
        "run": info.get("run"),
        "task": info.get("task_name"),
        "seed": info.get("seed"),
        "alhazen_version": run.snapshot.get("provenance", {}).get("alhazen_version"),
    }


def _trials(run: RunData) -> dict[str, Any]:
    counts = run.outcome_counts()
    n_rows = len(run.trials)
    trials = run.trials
    if "completed" in trials:
        # The engine stamps the outcome's own ``completed`` flag on every row.
        completed = int(_as_bool(trials["completed"]).sum())
    else:
        # Runs recorded before that column existed. The fallback is a
        # heuristic and known to be wrong for an experiment whose incomplete
        # outcomes have their own names (a broken fixation is an incomplete
        # trial and writes a row): it can only recognise the two reserved
        # ones. Nothing better is available from an old table, since outcome
        # names belong to the experiment and this layer cannot import them.
        completed = sum(
            count for outcome, count in counts.items() if outcome not in ("PAUSED", "ABORTED")
        )
    return {
        "n_rows": n_rows,
        "completed": completed,
        "completed_rate": (completed / n_rows) if n_rows else 0.0,
        "outcomes": counts,
    }


def _frames(run: RunData) -> dict[str, Any]:
    frames = run.frames
    n_frames = len(frames)
    if n_frames == 0:
        return {
            "n_frames": 0,
            "n_dropped": 0,
            "dropped_rate": 0.0,
            "worst_interval_ms": 0.0,
            "dropped_by_trial": {},
        }
    intervals = pd.to_numeric(frames.get("interval_s"), errors="coerce").dropna()
    dropped_flags = frames.get("dropped")
    dropped_mask = (
        _as_bool(dropped_flags)
        if dropped_flags is not None
        else pd.Series(False, index=frames.index)
    )
    return {
        "n_frames": n_frames,
        "n_dropped": int(dropped_mask.sum()),
        "dropped_rate": float(dropped_mask.sum() / n_frames),
        "worst_interval_ms": float(intervals.max() * 1000.0) if len(intervals) else 0.0,
        "dropped_by_trial": _dropped_by_trial(run, frames, dropped_mask),
    }


def _dropped_by_trial(run: RunData, frames: Any, dropped_mask: Any) -> dict[int, int]:
    """Dropped frames per trial, from both places that count them.

    The frame log and the trials table are independent records of the same
    thing — the monitor counts flips, the row carries what the engine saw —
    and a session where they disagree has a bug in one of them. Reporting the
    larger of the two per trial means neither can hide a drop.
    """
    per_trial: dict[int, int] = {}
    if "trial_index" in frames:
        counts = frames.loc[dropped_mask, "trial_index"].value_counts()
        for index, count in counts.items():
            per_trial[int(index)] = int(count)
    trials = run.trials
    if "n_dropped_frames" in trials and "trial_index" in trials:
        rows = trials[["trial_index", "n_dropped_frames"]].dropna()
        for _, row in rows.iterrows():
            index, count = int(row["trial_index"]), int(row["n_dropped_frames"])
            per_trial[index] = max(per_trial.get(index, 0), count)
    return {index: per_trial[index] for index in sorted(per_trial) if per_trial[index]}
