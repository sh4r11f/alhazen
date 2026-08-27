"""QUEST+: pick the stimulus that will teach you the most about the observer.

Instead of sampling a fixed grid, QUEST+ (Watson, 2017) keeps a posterior over
the psychometric function's parameters and, each trial, presents the intensity
whose expected outcome entropy is lowest — the one whose answer, whichever it
turns out to be, leaves the least uncertainty behind. That converges on a
threshold in a fraction of the trials a fixed grid needs.

Implemented here in numpy rather than delegating to a renderer's staircase
class: this is arithmetic over a parameter grid, and a scheduler that dragged
in the display stack would make adaptive experiments impossible to run — or
test — on a machine with no renderer installed.

What counts as a "success" is the experiment's business, not this module's.
``score`` maps a finished trial to True/False, so a task can titrate accuracy
(``outcome.success``, the default), or a magnitude crossing a criterion, or
anything else it can compute from the result.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Literal

import numpy as np
import pandas as pd

from alhazen.core.engine import TrialResult
from alhazen.paradigms.base import Condition

StimScale = Literal["linear", "log10", "dB"]


def weibull(
    intensity: np.ndarray | float,
    threshold: np.ndarray | float,
    slope: np.ndarray | float,
    lower_asymptote: np.ndarray | float,
    lapse_rate: np.ndarray | float,
    scale: StimScale = "linear",
) -> np.ndarray:
    """Probability of a success at ``intensity``, for one parameter set.

    Three intensity scales, because "twice the contrast" and "twice the
    decibels" are different questions: ``linear`` compares intensity to
    threshold as a ratio, ``log10`` and ``dB`` as a difference (the latter in
    20·log10 units).

    The asymptotes are the conventional ones: performance runs from
    ``lower_asymptote`` at zero intensity — the guess rate, which is 0.5 for a
    two-alternative task and near 0 for detection — up to ``1 - lapse_rate``,
    the ceiling an attentive observer still falls short of.
    """
    intensity = np.asarray(intensity, dtype=float)
    threshold = np.asarray(threshold, dtype=float)
    if scale == "linear":
        # A ratio scale has no meaning at or below zero threshold; treated as
        # "infinitely easy" rather than allowed to produce a silent nan.
        with np.errstate(divide="ignore", invalid="ignore"):
            safe = np.where(threshold > 0, threshold, 1.0)
            ratio = np.where(threshold > 0, intensity / safe, np.inf)
        exponent = -np.power(np.clip(ratio, 0.0, None), slope)
    elif scale == "log10":
        exponent = -np.power(10.0, slope * (intensity - threshold))
    else:  # "dB"
        exponent = -np.power(10.0, slope * (intensity - threshold) / 20.0)
    return lower_asymptote + (1.0 - lower_asymptote - lapse_rate) * (1.0 - np.exp(exponent))


class QuestPlusEstimator:
    """The Bayesian half: a posterior over (threshold, slope, lower asymptote,
    lapse rate), and the next intensity to present.

    Kept separate from the scheduler so the statistics can be tested directly —
    simulate an observer with a known threshold, feed responses, and check the
    estimate converges — with no session, no trials, and no rig.
    """

    def __init__(
        self,
        intensities: Sequence[float],
        thresholds: Sequence[float],
        slopes: Sequence[float] = (3.5,),
        lower_asymptotes: Sequence[float] = (0.05,),
        lapse_rates: Sequence[float] = (0.02,),
        scale: StimScale = "linear",
    ) -> None:
        if len(intensities) == 0 or len(thresholds) == 0:
            raise ValueError("QUEST+ needs a non-empty intensity grid and threshold grid")
        # Sorted grids: the same config content must produce the same session
        # whatever order its file listed the values in.
        self.intensities = np.array(sorted(intensities), dtype=float)
        grids = np.meshgrid(
            np.array(sorted(thresholds), dtype=float),
            np.array(sorted(slopes), dtype=float),
            np.array(sorted(lower_asymptotes), dtype=float),
            np.array(sorted(lapse_rates), dtype=float),
            indexing="ij",
        )
        self.param_names = ("threshold", "slope", "lower_asymptote", "lapse_rate")
        # Flattened to one axis: the posterior is over parameter *combinations*,
        # and every operation below is "for each combination", never per-axis.
        self._params = np.stack([g.ravel() for g in grids], axis=1)
        self._log_posterior = np.full(len(self._params), -np.log(len(self._params)))

        # p(success | intensity, params) for the whole grid, computed once:
        # it never changes, only the posterior over params does.
        self._p_success = np.empty((len(self.intensities), len(self._params)))
        for i, intensity in enumerate(self.intensities):
            self._p_success[i] = weibull(
                intensity,
                self._params[:, 0],
                self._params[:, 1],
                self._params[:, 2],
                self._params[:, 3],
                scale=scale,
            )
        # Clipped away from 0 and 1 so a single surprising response can never
        # take a parameter combination's likelihood to exactly zero — from
        # which no amount of later evidence could recover it.
        np.clip(self._p_success, 1e-9, 1 - 1e-9, out=self._p_success)
        self.n_responses = 0

    @property
    def posterior(self) -> np.ndarray:
        return np.exp(self._log_posterior)

    def next_intensity(self) -> float:
        """The intensity whose expected posterior entropy is lowest.

        For each candidate: work out how likely each answer is under the
        current posterior, what the posterior would become after each answer,
        and how uncertain that leaves things. Present the candidate with the
        lowest expected uncertainty.
        """
        posterior = self.posterior
        p_success = self._p_success @ posterior  # marginal p(success) per intensity
        expected_entropy = np.empty(len(self.intensities))
        for i in range(len(self.intensities)):
            entropy = 0.0
            for outcome_p, likelihood in (
                (p_success[i], self._p_success[i]),
                (1.0 - p_success[i], 1.0 - self._p_success[i]),
            ):
                if outcome_p <= 0:
                    continue
                updated = likelihood * posterior
                updated = updated / updated.sum()
                entropy += outcome_p * _entropy(updated)
            expected_entropy[i] = entropy
        # argmin ties break toward the lower intensity, deterministically —
        # a random tie-break would make the same seed produce different
        # sessions on different numpy versions.
        return float(self.intensities[int(np.argmin(expected_entropy))])

    def add_response(self, intensity: float, success: bool) -> None:
        """Fold one answer into the posterior."""
        index = int(np.argmin(np.abs(self.intensities - intensity)))
        likelihood = self._p_success[index] if success else 1.0 - self._p_success[index]
        # Updated in log space: over dozens of trials a product of small
        # likelihoods underflows to zero, taking the whole posterior with it.
        self._log_posterior = self._log_posterior + np.log(likelihood)
        self._log_posterior -= _log_sum_exp(self._log_posterior)
        self.n_responses += 1

    def estimate(self) -> dict[str, float]:
        """Posterior mean of each parameter — the estimate QUEST+ reports."""
        posterior = self.posterior
        return {
            name: float(np.sum(self._params[:, i] * posterior))
            for i, name in enumerate(self.param_names)
        }

    def entropy(self) -> float:
        return _entropy(self.posterior)


def _entropy(posterior: np.ndarray) -> float:
    nonzero = posterior[posterior > 0]
    return float(-np.sum(nonzero * np.log(nonzero)))


def _log_sum_exp(values: np.ndarray) -> float:
    peak = float(np.max(values))
    return peak + float(np.log(np.sum(np.exp(values - peak))))


def _success_from_outcome(result: TrialResult) -> bool:
    return bool(result.outcome.success)


class QuestPlus:
    """The scheduler: one estimator per interleaved level, served round-robin.

    Two rules, both about not corrupting the posterior:

    - an attempt that produced no measurement never reaches ``add_response``.
      Scoring a fixation break as a failure would drag the threshold estimate
      toward wherever the subject stopped cooperating, and nothing downstream
      could tell that apart from a real failure;
    - the intensity that attempt was already committed to is re-served
      unchanged, rather than drawing a fresh one. Drawing again would leave
      the first intensity in the record with no answer against it.
    """

    def __init__(
        self,
        parameter: str,
        intensities: Sequence[float],
        thresholds: Sequence[float],
        slopes: Sequence[float] = (3.5,),
        lower_asymptotes: Sequence[float] = (0.05,),
        lapse_rates: Sequence[float] = (0.02,),
        n_trials: int = 60,
        scale: StimScale = "linear",
        interleave_by: str | None = None,
        interleave_levels: Sequence[Any] = (),
        fixed: dict[str, Any] | None = None,
        score: Callable[[TrialResult], bool] | None = None,
    ) -> None:
        if n_trials < 1:
            raise ValueError(f"n_trials must be >= 1, got {n_trials}")
        if interleave_by is not None and not interleave_levels:
            raise ValueError("interleave_by needs the levels to interleave over")
        self._parameter = parameter
        self._interleave_by = interleave_by
        self._n_trials = n_trials
        self._fixed = dict(fixed or {})
        self._score = score or _success_from_outcome

        levels: list[Any] = sorted(interleave_levels) if interleave_by is not None else [None]
        self._entries = [
            {
                "level": level,
                "estimator": QuestPlusEstimator(
                    intensities, thresholds, slopes, lower_asymptotes, lapse_rates, scale
                ),
                "served": None,
                "pending_retry": False,
            }
            for level in levels
        ]
        self._cursor = 0

    def next(self) -> Condition | None:
        # One pass over the entries at most, so this always terminates and
        # returns None once every staircase has had its trials.
        for _ in range(len(self._entries)):
            entry = self._entries[self._cursor]
            # Advanced unconditionally, whatever this call ends up doing:
            # that is what keeps the staircases interleaved instead of
            # hammering whichever one needs a retry.
            self._cursor = (self._cursor + 1) % len(self._entries)
            estimator: QuestPlusEstimator = entry["estimator"]
            if estimator.n_responses >= self._n_trials:
                continue
            if entry["pending_retry"]:
                return entry["served"]
            params: dict[str, Any] = {
                **self._fixed,
                self._parameter: estimator.next_intensity(),
            }
            if self._interleave_by is not None:
                params[self._interleave_by] = entry["level"]
            condition = Condition(params)
            entry["served"] = condition
            return condition
        return None

    def record(self, condition: Condition, result: TrialResult) -> None:
        entry = self._entry_for(condition)
        if not result.outcome.completed:
            entry["pending_retry"] = True
            return
        entry["pending_retry"] = False
        estimator: QuestPlusEstimator = entry["estimator"]
        estimator.add_response(condition.params[self._parameter], self._score(result))

    def _entry_for(self, condition: Condition) -> dict:
        if self._interleave_by is None:
            return self._entries[0]
        level = condition.params.get(self._interleave_by)
        for entry in self._entries:
            if entry["level"] == level:
                return entry
        raise ValueError(
            f"record() got a condition with {self._interleave_by}={level!r}, which matches "
            f"none of this scheduler's levels ({[e['level'] for e in self._entries]!r})"
        )

    def summary(self) -> pd.DataFrame:
        rows = []
        for entry in self._entries:
            estimator: QuestPlusEstimator = entry["estimator"]
            rows.append(
                {
                    "interleave_value": entry["level"],
                    **estimator.estimate(),
                    "n_trials": estimator.n_responses,
                    "posterior_entropy": estimator.entropy(),
                }
            )
        return pd.DataFrame(rows)
