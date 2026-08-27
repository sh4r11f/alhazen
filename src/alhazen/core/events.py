"""Typed task events and the synchronous event bus.

Events are *names*, not a closed enum: the framework
reserves lifecycle names, and an experiment declares its own in an
`EventSchema`. Declaration — rather than free strings at emit time — is what
lets a rig config's sync-line map and an analysis pipeline be validated
against the full event vocabulary before a session ever runs, and what turns
an emit-time typo into a loud error instead of a silently absent TTL pulse.

Subscriber errors deliberately propagate out of ``emit``: a broken sync line
or a failing recorder must abort loudly, never drop marks silently.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

# Lifecycle events the framework itself emits. PAUSED/RESUMED are session
# control marks; REWARD covers both task-earned and manual deliveries
# (payload distinguishes them); REWARD_FAILED marks a delivery the hardware
# refused — its own event, not a REWARD with a flag, because the two mean
# opposite things to anything reading the record afterwards.
RESERVED_EVENTS = frozenset(
    {
        "TRIAL_START",
        "TRIAL_END",
        "REWARD",
        "REWARD_FAILED",
        "PAUSED",
        "RESUMED",
        # A curriculum moved the subject between stages. Reserved rather than
        # experiment-declared because the framework emits it, and because an
        # analysis of training data needs one name for it across every task.
        "STAGE_CHANGED",
        # A completed trial that earned nothing, on a session that has a
        # reward policy. Its own event rather than the absence of REWARD:
        # "no juice" is a fact about the trial that a subject experienced and
        # an analysis wants to count, and inferring it from a missing event is
        # indistinguishable from an event that failed to be written.
        "NO_REWARD",
    }
)

_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class EventSchema:
    """The full event vocabulary of one experiment: reserved + declared."""

    def __init__(self, declared: tuple[str, ...] | list[str] = ()) -> None:
        for name in declared:
            if not _NAME_RE.match(name):
                raise ValueError(
                    f"event name {name!r} must be UPPER_SNAKE_CASE (it becomes a column "
                    f"value, a tracker message, and a sync-line key)"
                )
            if name in RESERVED_EVENTS:
                raise ValueError(f"event name {name!r} is reserved by alhazen")
        if len(set(declared)) != len(tuple(declared)):
            raise ValueError("duplicate event names declared")
        self.declared = frozenset(declared)
        self.all_names = self.declared | RESERVED_EVENTS

    def validate(self, name: str) -> str:
        if name not in self.all_names:
            raise ValueError(
                f"event {name!r} was never declared — declare it in the task's EventSchema "
                f"(declared: {sorted(self.declared)})"
            )
        return name


@dataclass(frozen=True)
class Event:
    name: str
    t: float  # seconds on the session clock
    trial_index: int
    payload: dict = field(default_factory=dict)


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[Callable[[Event], None]] = []

    def subscribe(self, fn: Callable[[Event], None]) -> None:
        self._subscribers.append(fn)

    def emit(self, event: Event) -> None:
        # No try/except: a subscriber that raises aborts the trial loudly.
        for fn in self._subscribers:
            fn(event)
