"""The session clock: one monotonic timebase for everything.

Every timestamp this package records — events, flips, responses, log lines —
comes from one injected `Clock`, so any two recorded times are directly
comparable with no epoch or unit conversion. Device-native clocks (an eye
tracker's, a neural recorder's) are never mixed in online; they are aligned
offline through shared sync events.
"""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Seconds on a monotonic timebase. The zero point is arbitrary but
        fixed for the life of the session."""
        ...


class MonotonicClock:
    """Wall implementation: ``perf_counter`` re-zeroed at construction, so
    session times start near zero and stay readable in logs."""

    def __init__(self) -> None:
        self._t0 = time.perf_counter()

    def now(self) -> float:
        return time.perf_counter() - self._t0
