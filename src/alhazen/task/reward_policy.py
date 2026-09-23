"""Reward as data: which outcome earns what, and how much of it.

Policy lives in config rather than in a phase or the runner because it is the
thing that changes most often and by the least code-literate route — an
experimenter shaping behavior between sessions, and later a training stage
turning ``scale`` up or down without touching the task.

An outcome absent from ``by_outcome`` earns nothing. That is deliberate: a
reward table is easier to read as "these outcomes pay" than as a list of
exceptions, and a typo'd outcome name therefore fails safe (no juice) rather
than paying out on the wrong trials.

One trial is paid by something other than its outcome: a trial a device fault
cut short (the eye tracker stopped recording mid-trial) is paid ``on_fault``,
because the subject was still working when the rig failed and the trial is
served again anyway. The session decides which trials those are
(session/runner.py); the policy only says what they pay.
"""

from __future__ import annotations

from alhazen.config.models import Model, RewardPulses


class RewardPolicy(Model):
    """What each outcome pays, and what a trial cut short by a device fault
    pays, scaled by a single dial."""

    by_outcome: dict[str, RewardPulses] = {}
    # What a trial pays when a device stopped mid-trial and ended it before
    # its outcome was decided — the eye tracker stopping recording. That is a
    # system fault, not the subject's: the trial is flagged and served again,
    # and the subject, usually still fixating or about to respond when it was
    # cut off, is paid this rather than nothing. None — the default — pays
    # nothing, so a task that does not set it is unaffected. `by_outcome` is
    # never consulted for such a trial: its ABORTED is the rig's, not a
    # result the subject earned.
    #
    # Not paid on a trial frame QA recycled for dropped frames: that subject
    # did finish the trial, and is paid for the response, as on any trial.
    on_fault: RewardPulses | None = None
    # Multiplier on the pulse count, so a training stage can thin or fatten
    # every delivery at once — `on_fault` included, since a stage that pays
    # half as much for a correct trial should not pay a fault in full. Applied
    # to n_pulses only: pulse width sets the volume per pulse and is a
    # property of the pump's calibration, not of how generous this session is.
    scale: float = 1.0

    def pulses_for(self, outcome_name: str) -> RewardPulses | None:
        """The delivery this outcome earns, scaled — or None if it earns
        nothing. A scale that rounds the count to zero returns None too: a
        zero-pulse delivery is "no reward", and saying so here keeps the
        caller from logging a delivery that never happened."""
        return self._scaled(self.by_outcome.get(outcome_name))

    def pulses_for_fault(self) -> RewardPulses | None:
        """What a trial a device fault cut short is paid: ``on_fault``,
        scaled — or None when the task sets none, or when it comes to no
        pulses at this scale."""
        return self._scaled(self.on_fault)

    def _scaled(self, pulses: RewardPulses | None) -> RewardPulses | None:
        """``pulses`` with its count multiplied by ``scale`` and rounded, or
        None when there is nothing to scale or the count rounds below one."""
        if pulses is None:
            return None
        n_pulses = int(round(pulses.n_pulses * self.scale))
        if n_pulses < 1:
            return None
        return pulses.model_copy(update={"n_pulses": n_pulses})
