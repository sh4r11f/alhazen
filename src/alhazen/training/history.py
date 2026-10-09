"""How a subject is doing at each stage of a training ladder, from the data.

The operator decides when a monkey moves up a rung (training/ladder.py), and
decides from this: per stage, every session run at it, each with how often
the stage's success outcome came up among the trials the subject finished,
and — where the stage declares a criterion — what that criterion says about
the subject's most recent trials at the stage, as a recommendation.

Everything is read back from the run folders the sessions wrote (session.json
and the trials table), never from a state file of its own: the data are the
record, so nothing here can disagree with them. A trial the subject did not
make — paused, or lost to a system fault (dropped frames, a tracker that
stopped: ``core.trial.lost_to_fault``) — is not counted, as the criteria
window of a curriculum does not count one (training/criteria.py).
"""

from __future__ import annotations

import csv
import json
import logging
import math
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from alhazen.core.trial import PAUSED, lost_to_fault
from alhazen.data.paths import find_runs
from alhazen.training.criteria import evaluate_metric
from alhazen.training.ladder import Ladder, training_root
from alhazen.training.stages import StageCriteria

log = logging.getLogger(__name__)

_TRUE = {"true", "1", "yes"}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value) and not (isinstance(value, float) and math.isnan(value))
    return str(value).strip().lower() in _TRUE


def _counted(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The subject's own attempts: not paused, not lost to a system fault."""
    kept = []
    for row in rows:
        outcome = str(row.get("outcome") or "")
        if not outcome or outcome == PAUSED.name:
            continue
        if lost_to_fault(outcome, row) is not None:
            continue
        kept.append(row)
    return kept


def stage_tally(rows: Iterable[Mapping[str, Any]], success: str) -> dict[str, Any]:
    """Attempts, finished trials, successes and the success rate.

    ``success_rate`` is successes over FINISHED trials (``completed``), as
    ``training.criteria.success_rate`` is: a broken fixation is not a failed
    saccade. None when nothing was finished yet.
    """
    rows = list(rows)
    counted = _counted(rows)
    finished = [row for row in counted if _truthy(row.get("completed"))]
    successes = sum(1 for row in finished if row.get("outcome") == success)
    return {
        "attempts": len(counted),
        "finished": len(finished),
        "successes": successes,
        "faults": len(rows) - len(counted),
        "success_rate": (successes / len(finished)) if finished else None,
    }


def stage_window(rows: Iterable[Mapping[str, Any]], success: str, stage: str) -> list[dict]:
    """Criteria-window entries (training.criteria) for these rows, in order,
    with ``success`` meaning the STAGE's success outcome."""
    window = []
    for row in _counted(rows):
        completed = _truthy(row.get("completed"))
        window.append(
            {
                "outcome": row.get("outcome"),
                "completed": completed,
                "success": completed and row.get("outcome") == success,
                "rt_ms": None,
                "stage": stage,
            }
        )
    return window


def recommendation(criterion: StageCriteria, window: list[dict]) -> dict[str, Any]:
    """What ``criterion`` says about the most recent ``criterion.window``
    attempts: ``advance``, ``go back``, ``stay`` (judged, not met) or
    ``too few trials``. Never acted on: it is shown to the operator.
    """
    recent = window[-criterion.window :]
    metrics: dict[str, float | None] = {}
    for name in sorted({*criterion.promote_when, *criterion.demote_when}):
        value = evaluate_metric(name, recent) if recent else float("nan")
        metrics[name] = None if math.isnan(value) else round(value, 4)
    if len(recent) < criterion.min_trials:
        verdict = "too few trials"
    elif any(
        metrics.get(name) is not None and metrics[name] <= limit  # type: ignore[operator]
        for name, limit in criterion.demote_when.items()
    ):
        verdict = "go back"
    elif criterion.promote_when and all(
        metrics.get(name) is not None and metrics[name] >= limit  # type: ignore[operator]
        for name, limit in criterion.promote_when.items()
    ):
        verdict = "advance"
    else:
        verdict = "stay"
    return {
        "verdict": verdict,
        "metrics": metrics,
        "trials": len(recent),
        "window": criterion.window,
        "min_trials": criterion.min_trials,
        "promote_when": dict(criterion.promote_when),
        "demote_when": dict(criterion.demote_when),
    }


def read_trials(run_dir: Path) -> list[dict[str, str]]:
    """The run's trials table as rows of strings; [] when it has none yet."""
    tables = sorted(run_dir.glob("*_trials.csv"))
    if not tables:
        return []
    try:
        with tables[0].open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        log.warning("cannot read %s: %s", tables[0], error)
        return []


def _session_card(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "session.json"
    try:
        card = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return card if isinstance(card, dict) else {}


def stage_sessions(root: Path, success: str, *, kind: str) -> list[dict[str, Any]]:
    """Every session under one stage's root, oldest first, with its tally."""
    sessions = []
    for found in find_runs(root):
        card = _session_card(found.path)
        recorded = card.get("training")
        stage: dict[str, Any] = recorded if isinstance(recorded, dict) else {}
        paid = str(stage.get("success") or success)
        rows = read_trials(found.path)
        sessions.append(
            {
                "kind": kind,
                "subject": found.subject,
                "session": found.session,
                "run": found.run,
                "task": found.task,
                "version": found.experiment_version,
                "mode": card.get("mode"),
                "date": card.get("date"),
                "created": card.get("created"),
                "success": paid,
                "path": str(found.path),
                **stage_tally(rows, paid),
            }
        )
    sessions.sort(key=lambda s: (str(s.get("created") or s.get("date") or ""), s["path"]))
    return sessions


def ladder_history(
    data_roots: Iterable[Path | str], ladder: Ladder, *, include_rehearsals: bool = True
) -> dict[str, Any]:
    """Per stage, in ladder order: its sessions (training and, optionally,
    rehearsals) under every given rig data root, totals, and per subject the
    criterion's recommendation over that subject's latest training trials.
    """
    roots = list(dict.fromkeys(Path(root) for root in data_roots))
    stages = []
    for index, stage in enumerate(ladder.stages):
        sessions: list[dict[str, Any]] = []
        for root in roots:
            sessions += stage_sessions(
                training_root(root, ladder.name, stage.id, rehearsal=False),
                stage.success,
                kind="training",
            )
            if include_rehearsals:
                sessions += stage_sessions(
                    training_root(root, ladder.name, stage.id, rehearsal=True),
                    stage.success,
                    kind="rehearsal",
                )
        sessions.sort(key=lambda s: (str(s.get("created") or s.get("date") or ""), s["path"]))
        trained = [s for s in sessions if s["kind"] == "training"]
        by_subject: dict[str, Any] = {}
        for subject in sorted({s["subject"] for s in trained}):
            mine = [s for s in trained if s["subject"] == subject]
            entry: dict[str, Any] = {
                "sessions": len(mine),
                "finished": sum(s["finished"] for s in mine),
                "successes": sum(s["successes"] for s in mine),
                "last": mine[-1]["created"] or mine[-1]["date"],
            }
            if stage.criterion is not None:
                window: list[dict] = []
                for s in mine:
                    window += stage_window(read_trials(Path(s["path"])), s["success"], stage.id)
                entry["recommendation"] = recommendation(stage.criterion, window)
            by_subject[subject] = entry
        finished = sum(s["finished"] for s in trained)
        successes = sum(s["successes"] for s in trained)
        stages.append(
            {
                "id": stage.id,
                "number": index + 1,
                "title": stage.title,
                "success": stage.success,
                "criterion": (
                    stage.criterion.model_dump(mode="json") if stage.criterion is not None else None
                ),
                "sessions": sessions,
                "training_sessions": len(trained),
                "rehearsal_sessions": len(sessions) - len(trained),
                "trials": sum(s["attempts"] for s in trained),
                "finished": finished,
                "successes": successes,
                "success_rate": (successes / finished) if finished else None,
                "subjects": by_subject,
            }
        )
    return {"ladder": ladder.name, "title": ladder.title, "stages": stages}
