"""StreakMonitor: when a run of bad trials stops the session at the pause screen.

Internal to the session package; SessionRunner builds one from its
``max_consecutive_failures`` and ``max_consecutive_dropouts`` arguments.

The decision it hides is *which trials count toward which streak*. There
are two streaks, and they count different things on purpose: the subject's
failure streak (non-completed trials, never a trial a device cut short) and
the device's dropout streak (trials a health check failed on, and nothing
else). Every rule about what counts, what ends a streak and what neither
counts nor ends one lives here, with the counters it moves.

It makes decisions only. It does no I/O — no logging, no events, no screen,
no clock — so every rule can be tested by feeding it outcomes, without a
runner. The caller reads the facts it needs off the trial (the outcome, the
fault it was lost to, whether the display was failing, the row) and turns a
returned streak into the WARNING and the pause.

Interface: ``count_failure`` and ``count_dropout``, called once per trial
that was not paused before it ran, each returning the streak that has
reached its limit (``FailureStreak`` / ``DropoutStreak``, which word the
pause heading) or None; ``dropout_pause_raised`` once the caller has raised
the dropout pause.

Callers must not rely on the counters themselves (they are private and are
reset at points this module chooses), nor on a failure streak being
reported more than once: it resets as it is reported. A dropout streak, by
contrast, is reported on every trial from its limit on until the caller
says its pause was raised.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from alhazen.core.trial import (
    DROPPED_FRAMES,
    FAULT_DROPPED_FRAMES,
    FAULT_TRACKER_STOPPED,
    NO_FAULT,
    PAUSED,
    Outcome,
)


def cut_short_by_device(fault: str | None) -> bool:
    """Was this trial lost to a device that stopped mid-trial — the eye
    tracker — rather than to frame QA?

    The two faults a trial can be lost to are handled differently, because
    they cost the subject different things. A dropped-frames trial ran to its
    end: the subject responded, is paid for that response, and a completed
    trial ends a failure streak. A trial a device cut short usually ended
    before any response existed: it is paid ``RewardPolicy.on_fault``, and it
    says nothing about the subject either way.
    """
    return fault is not None and fault != FAULT_DROPPED_FRAMES


def device_fault(record: dict[str, Any]) -> str | None:
    """The fault a device health check reported on this trial, from its row,
    or None.

    Wider than being lost to one: a tracker that stopped during the closing
    phase flags the row without costing the trial its outcome, and is still a
    tracker that stopped. Every row's ``fault`` is one of "none", frame QA's
    "dropped_frames", or a failed health check's reason — so anything but the
    first two is a device's.
    """
    fault = record.get("fault", NO_FAULT)
    if fault in (None, "", NO_FAULT, FAULT_DROPPED_FRAMES):
        return None
    return str(fault)


# What the pause a run of dropouts raises leads with, per health-check reason.
# A check added later reports a reason not listed here; its heading names it.
_DROPOUT_HEADINGS = {FAULT_TRACKER_STOPPED: "THE EYE TRACKER DROPPED OUT"}


@dataclass(frozen=True)
class FailureStreak:
    """The subject's failure streak, as it stood when it reached its limit.

    ``count`` is the number of trials in a row (the limit); ``last_outcome``
    the name of the outcome that reached it; ``display_trials`` how many of
    the counted trials dropped more of their frames than frame QA allows.
    """

    count: int
    last_outcome: str
    display_trials: int

    def heading(self, budget: str) -> str:
        """What the pause screen leads with when the failure streak stops the
        session: the display first when it was failing through the streak,
        the subject-side checks otherwise.

        ``budget`` is frame QA's dropped-frame budget as frame QA writes it
        (``threshold_percent``): "7.5%", never a rounded "8%".
        """
        if self.display_trials:
            return (
                f"{self.count} TRIALS FAILED IN A ROW — last {self.last_outcome}, and "
                f"{self.display_trials} of them dropped over {budget} of their frames; check "
                f"the display before recalibrating"
            )
        return (
            f"{self.count} TRIALS FAILED IN A ROW — last {self.last_outcome}; check the "
            f"calibration (V), the subject, and the stimulus before resuming"
        )


@dataclass(frozen=True)
class DropoutStreak:
    """The device's dropout streak, at or past its limit: how many trials in
    a row, and the last dropout's health-check reason and detail."""

    count: int
    fault: str
    detail: str | None

    def heading(self) -> str:
        """What the pause screen leads with when a run of dropouts stops the
        session: what dropped out and how often, then what the device said
        the last time — the words that tell a pulled cable from a Host PC that
        stopped recording — and where to look."""
        what = _DROPOUT_HEADINGS.get(self.fault, f"A DEVICE FAILED ITS HEALTH CHECK ({self.fault})")
        said = f"last: {self.detail}. " if self.detail else ""
        return (
            f"{what} ON {self.count} TRIALS IN A ROW — {said}Check the tracker "
            f"and its connection to this machine before resuming; those trials are served again"
        )


class StreakMonitor:
    """Counts the subject's failure streak and the device's dropout streak,
    trial by trial, and says when either has reached its limit. See the
    module docstring for what it hides and what callers may rely on."""

    def __init__(
        self, max_consecutive_failures: int | None, max_consecutive_dropouts: int | None
    ) -> None:
        # How many trials in a row a device may fail its health check on (the
        # eye tracker dropping out) before the session stops at the pause
        # screen, headed with what the device said; None never pauses. Each of
        # those trials is served again, so without this a tracker that dies at
        # the start of every recording turns the session into a loop: the
        # same trial served into a dead tracker, its fault reward paid each
        # time, and nothing on the rig's screen but trials that never finish.
        # The rig's number, from eyetracker.max_consecutive_dropouts
        # (session/builder.py); on by default in SessionRunner too, for a
        # runner built by hand.
        if max_consecutive_dropouts is not None and max_consecutive_dropouts < 1:
            raise ValueError(
                f"max_consecutive_dropouts must be >= 1 or None, got {max_consecutive_dropouts}"
            )
        self._max_consecutive_dropouts = max_consecutive_dropouts
        self._dropouts_in_a_row = 0
        # How many non-completed trials in a row stop the session at the pause
        # screen. None never pauses. What counts as too many is the task's to
        # say (the builder reads it off the task's params), because a
        # fixation-break rate that is routine for one design is a subject who
        # cannot see the stimulus in another. A session that completed none
        # of 33 trials — every one a fixation break, with the eye sitting just
        # outside the window on a calibration that passed — ran to its end
        # with nothing on screen or in the log saying so. This is that line.
        if max_consecutive_failures is not None and max_consecutive_failures < 1:
            raise ValueError(
                f"max_consecutive_failures must be >= 1 or None, got {max_consecutive_failures}"
            )
        self._max_consecutive_failures = max_consecutive_failures
        self._failures_in_a_row = 0
        # How many of the trials counted in the current streak dropped more of
        # their frames than frame QA allows.
        self._failure_streak_display_trials = 0

    def count_failure(
        self, outcome: Outcome, *, fault: str | None, display_failing: bool
    ) -> FailureStreak | None:
        """Count the subject's failed trials back to back; the streak on the
        trial that reaches the task's limit, which the caller turns into a
        pause, and None on every other.

        ``fault`` is the system fault the trial was lost to
        (``TrialResult.lost_to_fault``), or None. ``display_failing`` says
        whether the trial dropped more of its frames than frame QA's budget
        allows (False on a simulated display, whose frame times measure the
        host rather than a panel).

        What counts is what the SUBJECT did, trial by trial:

        - A completed trial ends the streak.
        - ``DROPPED_FRAMES`` ends it too. The engine only recycles a trial the
          subject completed (core/engine.py): the display failed, not the eye,
          and the row keeps what the subject did as ``outcome_before_frame_qa``.
          Recycles used to be skipped over instead, neither counted nor ending
          anything, and that let a failing display join separate runs of
          failures into one. A rehearsal whose completed trials were all
          recycled paused on "6 trials in a row" for fixation breaks and missed
          saccades that those completed trials had separated, and told the
          operator to check a calibration while the panel dropped half its
          frames. Frame QA counts recycles on its own and stops the run with
          the display's message. Ending the streak never counts against the
          subject, so a dropped-frames trial keeps ending it even though it is
          a system fault.
        - ``PAUSED`` neither counts nor ends it: the experimenter stopped the
          trial, and that says nothing about the subject. The count restarts
          after the pause this raises, so a subject still not fixating gets a
          whole new run of chances rather than a pause every trial.
        - A trial the eye tracker cut short (``ABORTED``, lost to
          ``tracker_stopped``) neither counts nor ends it, like ``PAUSED``,
          and for the same reason: the rig stopped the trial, usually before
          the subject had responded, so it says nothing about the subject in
          either direction. Counted, a tracker dropping out between fixation
          breaks would send the operator to recalibrate a subject for the
          tracker's fault; ending the streak, it would hide a subject who was
          breaking fixation on every trial the tracker let finish.
        - Every other outcome that did not complete counts — the
          experimenter's skip included, as it always has.

        Beside the count it keeps how many of the counted trials dropped more
        of their frames than frame QA's budget, so the pause can say when the
        display was failing through the streak. A panel missing vsyncs can
        cause real fixation breaks, and what must not happen is sending the
        experimenter to recalibrate while it does.
        """
        limit = self._max_consecutive_failures
        if limit is None or outcome.name == PAUSED.name or cut_short_by_device(fault):
            return None
        if outcome.completed or outcome.name == DROPPED_FRAMES.name:
            self._failures_in_a_row = 0
            self._failure_streak_display_trials = 0
            return None
        self._failures_in_a_row += 1
        if display_failing:
            self._failure_streak_display_trials += 1
        if self._failures_in_a_row < limit:
            return None
        streak = FailureStreak(
            count=self._failures_in_a_row,
            last_outcome=outcome.name,
            display_trials=self._failure_streak_display_trials,
        )
        self._failures_in_a_row = 0
        self._failure_streak_display_trials = 0
        return streak

    def count_dropout(self, outcome: Outcome, record: dict[str, Any]) -> DropoutStreak | None:
        """Count the trials a device failed its health check on, back to back;
        the streak once the count has reached ``max_consecutive_dropouts``,
        which the caller turns into a pause (and reports through
        ``dropout_pause_raised``), and None otherwise.

        - A trial whose row names a device's fault (``device_fault``) counts:
          the tracker dropped out on it, whether that cost the trial its
          outcome or only flagged its closing phase.
        - ``PAUSED`` neither counts nor ends the run, as in the subject's
          failure streak: the experimenter stopped that trial, and it says
          nothing about the device either way.
        - Every other trial ends it: the tracker recorded that one through.

        Separate from the subject's failure streak on purpose. That one leaves
        a tracker-stopped trial out (it says nothing about the subject); this
        one is nothing but those trials, and its pause sends the experimenter
        to the tracker rather than to the calibration.

        The count is left standing when the limit is reached, and reset only
        when the pause is actually raised: a reward failure on the same trial
        takes the pause screen first (the pump), and the dropouts are then
        still owed their own pause if the next trial drops out as well.
        """
        limit = self._max_consecutive_dropouts
        if limit is None or outcome.name == PAUSED.name:
            return None
        fault = device_fault(record)
        if fault is None:
            self._dropouts_in_a_row = 0
            return None
        self._dropouts_in_a_row += 1
        if self._dropouts_in_a_row < limit:
            return None
        # The last dropout's reason and detail, for the pause heading.
        return DropoutStreak(
            count=self._dropouts_in_a_row, fault=fault, detail=record.get("fault_detail")
        )

    def dropout_pause_raised(self) -> None:
        """The caller raised the dropout streak's pause: the count starts
        again from here, so resuming on a tracker that was fixed gets a whole
        new run of chances and one that was not pauses again after as many."""
        self._dropouts_in_a_row = 0
