"""Reward as data: which outcome earns what, and how much of it.

Policy lives in config rather than in a phase or the runner because it is the
thing that changes most often and by the least code-literate route — an
experimenter shaping behavior between sessions, and later a training stage
turning ``scale`` up or down without touching the task.

An outcome absent from ``by_outcome`` earns nothing. That is deliberate: a
reward table is easier to read as "these outcomes pay" than as a list of
exceptions, and a typo'd outcome name therefore fails safe (no juice) rather
than paying out on the wrong trials.
"""

from __future__ import annotations

from alhazen.config.models import Model, RewardPulses


class RewardPolicy(Model):
    """What each outcome pays, scaled by a single dial."""

    by_outcome: dict[str, RewardPulses] = {}
    # Multiplier on the pulse count, so a training stage can thin or fatten
    # every delivery at once. Applied to n_pulses only: pulse width sets the
    # volume per pulse and is a property of the pump's calibration, not of
    # how generous this session is.
    scale: float = 1.0

    def pulses_for(self, outcome_name: str) -> RewardPulses | None:
        """The delivery this outcome earns, scaled — or None if it earns
        nothing. A scale that rounds the count to zero returns None too: a
        zero-pulse delivery is "no reward", and saying so here keeps the
        caller from logging a delivery that never happened."""
        pulses = self.by_outcome.get(outcome_name)
        if pulses is None:
            return None
        n_pulses = int(round(pulses.n_pulses * self.scale))
        if n_pulses < 1:
            return None
        return pulses.model_copy(update={"n_pulses": n_pulses})
