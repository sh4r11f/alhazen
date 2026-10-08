"""How long a session will take, said before it starts: the task's half.

The experiment workspace shows an estimate beside its Start button
(``alhazen.modes.estimate`` puts it together). Two things in it belong to the
experiment, and this module is their vocabulary:

- **what one trial lasts**, phase by phase (``Task.trial_timing``, a
  ``TrialTiming`` of ``Span`` values). A task builds its phases from its
  params inside ``build_trial``, which needs a window to make stimuli with, so
  the phases cannot be asked; the task says what they last instead, from the
  same params fields its ``build_trial`` reads.
- **which trials a session serves** (``Task.duration_schedule``). The default
  asks the task's own scheduler, built on a throwaway generator, exactly as a
  session builds it: a queue-based plan (sequence, constant, adjustment, and
  alhazen's ``BlockPlan`` over them) is drained to the end, so block-restricted
  factors, per-block queues and the breaks between blocks are counted as the
  session would serve them; an adaptive kind (staircase, QUEST+) gives the
  bounds its stopping rule allows. A task whose ``make_source`` is its own is
  never built here (it may load or move saved state): it says what it serves
  by overriding ``duration_schedule``, or the estimate says it cannot tell.

Every span carries the range its value can take and, where the configuration
alone decides it, its expected value. A wait on the subject (fixation
acquisition, a response window) has a cap and no expected value: a configured
timeout is the longest it can last, never how long it usually takes. Nothing
here touches a display, a device, a file or the session's generators.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from alhazen.core.engine import TrialResult
from alhazen.core.trial import Outcome
from alhazen.paradigms.adjustment import AdjustmentTrials
from alhazen.paradigms.base import Condition, SimpleSequence
from alhazen.paradigms.blocks import BlockPlan
from alhazen.paradigms.config import ADAPTIVE_KINDS, SchedulerConfig, _levels
from alhazen.paradigms.constant import ConstantStimuli

# What a drained plan may hold before it is refused: far above any session a
# person sits through (a 10 000-trial plan at one second a trial is close to
# three hours), and low enough that a plan that never ends cannot hang the
# page that asked.
MAX_PLANNED_TRIALS = 100_000

# The kinds of span. "fixed" and "jittered" are decided by the configuration;
# "wait" waits on the subject up to a cap; "range" is bounded by the design
# with no expected value the configuration decides.
SPAN_KINDS = ("fixed", "jittered", "wait", "range")


@dataclass(frozen=True)
class Span:
    """One stretch of a trial: its label, its bounds and, when the config
    decides it, its expected value. ``max_s`` None is unbounded."""

    label: str
    kind: str
    min_s: float
    max_s: float | None
    expected_s: float | None
    basis: str = ""

    def __post_init__(self) -> None:
        if self.kind not in SPAN_KINDS:
            raise ValueError(f"span kind must be one of {SPAN_KINDS}, got {self.kind!r}")
        values = [self.min_s] + [v for v in (self.max_s, self.expected_s) if v is not None]
        if not all(math.isfinite(v) and v >= 0 for v in values):
            raise ValueError(f"span {self.label!r}: durations are finite seconds >= 0")
        if self.max_s is not None and self.max_s < self.min_s:
            raise ValueError(f"span {self.label!r}: max_s {self.max_s} < min_s {self.min_s}")
        if self.expected_s is not None and not (
            self.min_s <= self.expected_s <= (self.max_s if self.max_s is not None else math.inf)
        ):
            raise ValueError(f"span {self.label!r}: expected_s lies outside its bounds")

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "kind": self.kind,
            "min_s": self.min_s,
            "max_s": self.max_s,
            "expected_s": self.expected_s,
            "basis": self.basis,
        }


def fixed(label: str, seconds: float, basis: str = "") -> Span:
    """A stretch that lasts exactly ``seconds`` (to the frame)."""
    return Span(label, "fixed", seconds, seconds, seconds, basis)


def jittered(label: str, center_s: float, jitter_s: float, basis: str = "") -> Span:
    """A stretch drawn uniformly from ``center_s ± jitter_s``: expected the centre."""
    if jitter_s == 0:
        return fixed(label, center_s, basis)
    return Span(
        label,
        "jittered",
        center_s - jitter_s,
        center_s + jitter_s,
        center_s,
        basis,
    )


def uniform(label: str, low_s: float, high_s: float, basis: str = "") -> Span:
    """A stretch drawn uniformly between two bounds: expected their mean."""
    if high_s == low_s:
        return fixed(label, low_s, basis)
    return Span(
        label,
        "jittered",
        low_s,
        high_s,
        (low_s + high_s) / 2,
        basis,
    )


def wait(label: str, cap_s: float, minimum_s: float = 0.0, basis: str = "") -> Span:
    """A stretch that waits on the subject, up to ``cap_s``. No expected
    value: the cap is the longest it can last, not how long it usually does."""
    return Span(label, "wait", minimum_s, cap_s, None, basis)


def bounded(label: str, min_s: float, max_s: float | None, basis: str) -> Span:
    """A stretch the design bounds without deciding its value (say, a cue
    that may move later to find a target): its bounds and why."""
    return Span(label, "range", min_s, max_s, None, basis)


@dataclass(frozen=True)
class TrialTiming:
    """One completed trial, from its first phase to its last, as spans in
    the order they run. The inter-trial interval is the session's, not the
    trial's, and is added by the estimate from the params' ``iti``."""

    spans: tuple[Span, ...]

    def __init__(self, spans: Any) -> None:
        object.__setattr__(self, "spans", tuple(spans))
        if not all(isinstance(s, Span) for s in self.spans):
            raise TypeError("TrialTiming takes Span values")

    @property
    def min_s(self) -> float:
        return sum(s.min_s for s in self.spans)

    @property
    def max_s(self) -> float | None:
        if any(s.max_s is None for s in self.spans):
            return None
        return sum(s.max_s for s in self.spans if s.max_s is not None)

    @property
    def expected_s(self) -> float | None:
        if any(s.expected_s is None for s in self.spans):
            return None
        return sum(s.expected_s for s in self.spans if s.expected_s is not None)

    @property
    def fixed_s(self) -> float:
        """The part the configuration decides: the expected values of the
        fixed and jittered spans, and the minimum of every other."""
        return sum(s.expected_s if s.expected_s is not None else s.min_s for s in self.spans)

    @property
    def open_s(self) -> float | None:
        """How much longer the spans without an expected value (waits on the
        subject, design-bounded stretches) can make the trial than
        ``fixed_s``: the sum of their caps less their minimums. None when one
        has no cap."""
        open_spans = [s for s in self.spans if s.expected_s is None]
        if any(s.max_s is None for s in open_spans):
            return None
        return sum(s.max_s - s.min_s for s in open_spans if s.max_s is not None)


@dataclass(frozen=True)
class PlannedSchedule:
    """A fixed plan: the conditions each block serves, in no particular
    order (the counts are what matter), and the rests between blocks."""

    blocks: tuple[tuple[Condition, ...], ...]
    breaks: int
    validate_after_break: bool = False
    note: str = ""
    # How many distinct cells it serves, its block number aside.
    n_cells: int | None = None

    @property
    def n_trials(self) -> int:
        return sum(len(block) for block in self.blocks)

    def conditions(self) -> list[Condition]:
        return [condition for block in self.blocks for condition in block]


@dataclass(frozen=True)
class AdaptiveSchedule:
    """A plan whose length its stopping rule decides: the bounds on its
    completed trials (``max_trials`` None: no bound), the rule in words, the
    conditions whose trial timing stands for its trials, and the bounds on
    its rests between blocks."""

    min_trials: int
    max_trials: int | None
    stopping_rule: str
    conditions: tuple[Condition, ...] = (Condition({}),)
    min_breaks: int = 0
    max_breaks: int | None = 0
    validate_after_break: bool = False
    note: str = ""

    def __post_init__(self) -> None:
        if self.min_trials < 0 or (
            self.max_trials is not None and self.max_trials < self.min_trials
        ):
            raise ValueError("AdaptiveSchedule needs 0 <= min_trials <= max_trials")
        if not self.conditions:
            raise ValueError("AdaptiveSchedule needs at least one condition to time")


@dataclass(frozen=True)
class UnknownSchedule:
    """Why the trials a session serves cannot be told before it runs."""

    reason: str


Schedule = PlannedSchedule | AdaptiveSchedule | UnknownSchedule

# The one outcome the drain records: a completed trial, so every planned
# trial leaves its queue exactly once — the plan as written, before any
# re-served attempt.
_COMPLETED = Outcome("ESTIMATE_COMPLETED", completed=True, success=True)


def _queue_based(source: Any) -> bool:
    """Whether ``source`` is one of alhazen's fixed-plan schedulers, which
    the drain may run to the end without side effects."""
    if isinstance(source, SimpleSequence | ConstantStimuli | AdjustmentTrials):
        return True
    if isinstance(source, BlockPlan):
        return all(_queue_based(s) for s in source._sources)
    return False


def drain(source: Any) -> PlannedSchedule:
    """Serve a fixed plan to its end, every trial completing, and say what
    it served: the conditions of each block and the rests between them, as
    a session's runner meets them (a break is taken after the ``next()`` that
    leaves it pending). The source is used up; build it for this alone."""
    if not _queue_based(source):
        raise TypeError(f"{type(source).__name__} is not a fixed plan this can drain")
    blocks: list[list[Condition]] = []
    cells: set[str] = set()
    current_block: int | None = None
    breaks = 0
    served = 0
    while True:
        condition = source.next()
        if condition is None:
            break
        # Which block served it: a BlockPlan's own index (the one it stamps
        # into the condition), else the one block a bare queue is.
        block = source._block if isinstance(source, BlockPlan) else 0
        if block != current_block:
            blocks.append([])
            current_block = block
        if isinstance(source, BlockPlan) and source.take_block_break() is not None:
            breaks += 1
        served += 1
        if served > MAX_PLANNED_TRIALS:
            raise ValueError(f"the plan serves more than {MAX_PLANNED_TRIALS} trials")
        blocks[-1].append(condition)
        # The cell as the inner scheduler holds it, without the block number
        # a BlockPlan stamps on what it serves.
        inner = source._served[1] if isinstance(source, BlockPlan) and source._served else condition
        cells.add(repr(sorted(inner.params.items(), key=lambda kv: kv[0])))
        source.record(condition, TrialResult(outcome=_COMPLETED, record={}))
    return PlannedSchedule(
        blocks=tuple(tuple(b) for b in blocks),
        n_cells=len(cells),
        breaks=breaks,
        validate_after_break=bool(getattr(source, "validate_after_break", False)),
    )


def adaptive_bounds(cfg: SchedulerConfig, conditions: list[Condition]) -> AdaptiveSchedule:
    """The bounds an adaptive ``SchedulerConfig`` puts on its completed
    trials, from its stopping rule, and on the rests between its blocks.

    A staircase stops at ``n_reversals`` reversals or ``n_trials`` trials,
    whichever comes first: ``n_trials`` alone is exact, both give
    ``[min(n_reversals, n_trials), n_trials]``, ``n_reversals`` alone has no
    upper bound (each reversal takes at least one trial, so ``n_reversals``
    is a lower bound). QUEST+ stops at exactly ``n_trials`` per level.
    Interleaved estimators multiply both bounds by their number of levels.
    """
    if cfg.kind not in ADAPTIVE_KINDS:
        raise ValueError(f"{cfg.kind!r} is not an adaptive kind")
    if cfg.kind == "staircase":
        assert cfg.staircase is not None
        stair = cfg.staircase
        n_est = len(_levels(conditions, stair.interleave_by)) if stair.interleave_by else 1
        if stair.n_trials is not None and stair.n_reversals is not None:
            low, high = min(stair.n_reversals, stair.n_trials), stair.n_trials
            rule = (
                f"each of {n_est} staircase(s) stops at {stair.n_reversals} reversals or "
                f"{stair.n_trials} trials, whichever comes first"
            )
        elif stair.n_trials is not None:
            low = high = stair.n_trials
            rule = f"each of {n_est} staircase(s) runs {stair.n_trials} trials"
        else:
            assert stair.n_reversals is not None
            low, high = stair.n_reversals, None
            rule = (
                f"each of {n_est} staircase(s) stops after {stair.n_reversals} reversals: "
                "no upper bound on its trials"
            )
    else:
        assert cfg.quest is not None
        n_est = len(_levels(conditions, cfg.quest.interleave_by)) if cfg.quest.interleave_by else 1
        low = high = cfg.quest.n_trials
        rule = f"QUEST+ runs {cfg.quest.n_trials} trials for each of {n_est} level(s)"
    min_trials = low * n_est
    max_trials = None if high is None else high * n_est
    min_breaks = 0
    max_breaks: int | None = 0
    validate = False
    if cfg.blocks is not None and cfg.blocks.breaks and cfg.blocks.trials_per_block:
        per = cfg.blocks.trials_per_block
        n = cfg.blocks.n_blocks
        # Blocks end on completed trials; a block that serves nothing after
        # the estimator finishes is no block a subject sits.
        min_breaks = max(0, min(n, math.ceil(min_trials / per)) - 1)
        max_breaks = None if max_trials is None else max(0, min(n, math.ceil(max_trials / per)) - 1)
        validate = cfg.blocks.validate_after_break
    return AdaptiveSchedule(
        min_trials=min_trials,
        max_trials=max_trials,
        stopping_rule=rule,
        conditions=tuple(conditions) or (Condition({}),),
        min_breaks=min_breaks,
        max_breaks=max_breaks,
        validate_after_break=validate,
    )


def default_schedule(task: Any, params: Any, rng: np.random.Generator) -> Schedule:
    """``Task.duration_schedule``'s default: the task's own scheduler, asked.

    Only alhazen's own ``make_source`` is ever built here — it builds a
    scheduler from the params and does nothing else — and only on ``rng``,
    a generator made for this: the session's generators are not drawn from.
    A task with its own ``make_source`` is not built, because nothing says
    what building it does (one loads a subject's saved search state).
    """
    from alhazen.task.task import Task

    if type(task).make_source is not Task.make_source:
        return UnknownSchedule(
            f"{type(task).__name__} builds its own scheduler (make_source) and does not say "
            "which trials it serves (duration_schedule)"
        )
    paradigm = getattr(params, task.paradigm_field, None) or SchedulerConfig()
    if not isinstance(paradigm, SchedulerConfig):
        return UnknownSchedule(
            f"{type(task).__name__}.params.{task.paradigm_field} is not a SchedulerConfig"
        )
    if paradigm.kind in ADAPTIVE_KINDS:
        return adaptive_bounds(paradigm, task.conditions(rng))
    return drain(task.make_source(params, rng))


def scratch_rng() -> np.random.Generator:
    """A generator for building a schedule to count, seeded so two estimates
    of one configuration agree. Never one of a session's own streams."""
    return np.random.default_rng(0)


__all__ = [
    "AdaptiveSchedule",
    "PlannedSchedule",
    "Schedule",
    "Span",
    "TrialTiming",
    "UnknownSchedule",
    "adaptive_bounds",
    "bounded",
    "default_schedule",
    "drain",
    "fixed",
    "jittered",
    "scratch_rng",
    "uniform",
    "wait",
]
