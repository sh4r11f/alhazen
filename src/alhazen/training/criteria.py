"""Deciding when a subject is ready to move on.

A criterion is a named metric over a sliding window of recent trials, and a
threshold. The metrics here are the three every shaping protocol ends up
using; an experiment registers its own for anything else.

The window holds *attempts*, not measurements: "how often does this subject
actually complete a trial" is the first thing a shaping protocol asks, and it
is unanswerable from completed trials alone.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from alhazen.errors import ConfigError
from alhazen.training.stages import StageCriteria

log = logging.getLogger(__name__)

# One window entry per trial attempt. The keys the built-in metrics read are
# written by the runner (training/supervisor.py); an experiment's own metric
# sees the whole trial record and can read anything in it.
TrialSummary = dict[str, Any]
MetricFn = Callable[[list[TrialSummary]], float]

_METRICS: dict[str, MetricFn] = {}


def register_metric(name: str, fn: MetricFn) -> None:
    """Add a metric a curriculum can name in its criteria.

    Registering over an existing name is refused: two metrics answering to
    one name would make a curriculum mean different things depending on
    import order.
    """
    if name in _METRICS:
        raise ConfigError(
            f"metric {name!r} is already registered — pick another name rather than "
            f"changing what an existing curriculum's criteria mean"
        )
    _METRICS[name] = fn


def metric_names() -> list[str]:
    return sorted(_METRICS)


def evaluate_metric(name: str, window: list[TrialSummary]) -> float:
    if name not in _METRICS:
        raise ConfigError(
            f"criterion names metric {name!r}, which is not registered (known: {metric_names()})"
        )
    return _METRICS[name](window)


# ---------------------------------------------------------------------------
# The built-in metrics
# ---------------------------------------------------------------------------


def completed_rate(window: list[TrialSummary]) -> float:
    """Fraction of attempts that produced a measurement.

    The engagement measure: a subject that breaks fixation on nine trials in
    ten is not ready for a harder one, however well it does on the tenth.
    """
    if not window:
        return 0.0
    return sum(1 for trial in window if trial.get("completed")) / len(window)


def success_rate(window: list[TrialSummary]) -> float:
    """Fraction of *completed* trials that succeeded.

    Denominator is completed trials, not all attempts: a broken fixation is
    not a wrong answer, and counting it as one would make the accuracy of an
    unengaged subject look like poor discrimination.
    """
    measured = [trial for trial in window if trial.get("completed")]
    if not measured:
        return 0.0
    return sum(1 for trial in measured if trial.get("success")) / len(measured)


def mean_rt_ms(window: list[TrialSummary]) -> float:
    """Mean reaction time over trials that recorded one.

    Returns NaN when nothing in the window has an RT, so a criterion on a
    task that records none never accidentally reads as "fast enough".
    """
    times = [
        float(trial["rt_ms"])
        for trial in window
        if trial.get("rt_ms") is not None and trial.get("completed")
    ]
    if not times:
        return float("nan")
    return sum(times) / len(times)


register_metric("completed_rate", completed_rate)
register_metric("success_rate", success_rate)
register_metric("mean_rt_ms", mean_rt_ms)


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def decide(criteria: StageCriteria, window: list[TrialSummary]) -> str | None:
    """``"promote"``, ``"demote"``, or None — what this window justifies.

    Demotion is checked first, and deliberately: a subject that has fallen
    apart should be moved back even if some other metric happens to read well
    at the same moment. Nothing is decided until the window holds at least
    ``min_trials`` attempts, so an early run of luck cannot promote anyone.
    """
    recent = window[-criteria.window :]
    if len(recent) < criteria.min_trials:
        return None

    for name, threshold in sorted(criteria.demote_when.items()):
        value = evaluate_metric(name, recent)
        # NaN compares false against everything, so a metric with no data
        # never triggers a demotion by accident.
        if value <= threshold:
            log.info(
                "demotion criterion met: %s = %.3f <= %.3f over %d trials",
                name,
                value,
                threshold,
                len(recent),
            )
            return "demote"

    if not criteria.promote_when:
        return None
    for name, threshold in sorted(criteria.promote_when.items()):
        if not evaluate_metric(name, recent) >= threshold:
            return None
    log.info(
        "promotion criteria met over %d trials: %s",
        len(recent),
        ", ".join(f"{name} >= {value}" for name, value in sorted(criteria.promote_when.items())),
    )
    return "promote"
