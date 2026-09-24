"""StreakMonitor: which trials count toward the subject's failure streak and
the device's dropout streak, decided without a runner.

These are the rules the runner's pause-on-a-streak behaviour rests on
(test_pause_flow.py, test_system_faults.py and test_tracker_dropout.py pin
them end to end, through whole sessions). Here each rule is fed outcomes
directly: the monitor makes decisions only, so nothing needs a display, a
clock or a tracker to exercise it.
"""

from __future__ import annotations

from typing import Any

import pytest

from alhazen.core.trial import (
    ABORTED,
    DROPPED_FRAMES,
    FAULT_DROPPED_FRAMES,
    FAULT_TRACKER_STOPPED,
    NO_FAULT,
    PAUSED,
    Outcome,
)
from alhazen.session.streaks import (
    DropoutStreak,
    FailureStreak,
    StreakMonitor,
    cut_short_by_device,
    device_fault,
)

CORRECT = Outcome("CORRECT", completed=True, success=True)
FIX_BREAK = Outcome("FIX_BREAK", completed=False)


def failures(limit: int | None) -> StreakMonitor:
    """A monitor counting only the subject's failure streak."""
    return StreakMonitor(max_consecutive_failures=limit, max_consecutive_dropouts=None)


def dropouts(limit: int | None) -> StreakMonitor:
    """A monitor counting only the device's dropout streak."""
    return StreakMonitor(max_consecutive_failures=None, max_consecutive_dropouts=limit)


def fail(
    monitor: StreakMonitor,
    outcome: Outcome = FIX_BREAK,
    *,
    fault: str | None = None,
    display_failing: bool = False,
) -> FailureStreak | None:
    return monitor.count_failure(outcome, fault=fault, display_failing=display_failing)


def row(fault: str = NO_FAULT, detail: str | None = None) -> dict[str, Any]:
    """A trial row as the engine writes it: ``fault`` on every row."""
    record: dict[str, Any] = {"fault": fault}
    if detail is not None:
        record["fault_detail"] = detail
    return record


class TestLimits:
    @pytest.mark.parametrize("name", ["max_consecutive_failures", "max_consecutive_dropouts"])
    def test_a_limit_below_one_is_refused(self, name):
        limits = {"max_consecutive_failures": None, "max_consecutive_dropouts": None, name: 0}
        with pytest.raises(ValueError, match=rf"{name} must be >= 1 or None, got 0"):
            StreakMonitor(**limits)

    def test_no_limit_never_pauses(self):
        monitor = StreakMonitor(max_consecutive_failures=None, max_consecutive_dropouts=None)
        for _ in range(50):
            assert fail(monitor) is None
            assert monitor.count_dropout(FIX_BREAK, row(FAULT_TRACKER_STOPPED)) is None


class TestFailureStreak:
    def test_n_failures_in_a_row_pause_on_the_nth(self):
        monitor = failures(3)
        assert fail(monitor) is None
        assert fail(monitor) is None
        assert fail(monitor) == FailureStreak(count=3, last_outcome="FIX_BREAK", display_trials=0)

    def test_the_count_restarts_after_the_pause_it_raises(self):
        # A subject still not fixating gets a whole new run of chances rather
        # than a pause every trial.
        monitor = failures(2)
        fail(monitor)
        assert fail(monitor) is not None
        assert fail(monitor) is None
        assert fail(monitor) is not None

    def test_a_completed_trial_ends_the_streak(self):
        monitor = failures(2)
        fail(monitor)
        assert fail(monitor, CORRECT) is None
        assert fail(monitor) is None  # one in a row again, not two

    def test_a_dropped_frames_trial_ends_it_too(self):
        # The engine only recycles a trial the subject completed.
        monitor = failures(2)
        fail(monitor)
        assert fail(monitor, DROPPED_FRAMES, fault=FAULT_DROPPED_FRAMES) is None
        assert fail(monitor) is None

    def test_a_paused_trial_neither_counts_nor_ends_it(self):
        monitor = failures(2)
        fail(monitor)
        for _ in range(5):
            assert fail(monitor, PAUSED) is None
        assert fail(monitor) is not None  # the fixation break before the pauses still counts

    def test_a_trial_the_tracker_cut_short_is_not_counted_against_the_subject(self):
        monitor = failures(2)
        fail(monitor)
        for _ in range(5):
            assert fail(monitor, ABORTED, fault=FAULT_TRACKER_STOPPED) is None
        # ...and it did not end the streak either.
        assert fail(monitor) is not None

    def test_the_experimenters_skip_counts(self):
        # ABORTED with no fault is the skip key: it has always counted.
        monitor = failures(2)
        fail(monitor, ABORTED)
        assert fail(monitor, ABORTED) == FailureStreak(
            count=2, last_outcome="ABORTED", display_trials=0
        )

    def test_it_counts_the_trials_the_display_was_failing_on(self):
        monitor = failures(3)
        fail(monitor, display_failing=True)
        fail(monitor)
        streak = fail(monitor, display_failing=True)
        assert streak is not None and streak.display_trials == 2

    def test_a_completed_trial_clears_the_display_count_with_the_streak(self):
        monitor = failures(2)
        fail(monitor, display_failing=True)
        fail(monitor, CORRECT)
        fail(monitor)
        streak = fail(monitor)
        assert streak is not None and streak.display_trials == 0


class TestFailureHeading:
    def test_it_sends_the_experimenter_to_the_subject_side_checks(self):
        heading = FailureStreak(count=3, last_outcome="FIX_BREAK", display_trials=0).heading("7.5%")
        assert heading == (
            "3 TRIALS FAILED IN A ROW — last FIX_BREAK; check the calibration (V), the "
            "subject, and the stimulus before resuming"
        )

    def test_a_failing_display_is_named_first(self):
        heading = FailureStreak(count=3, last_outcome="FIX_BREAK", display_trials=2).heading("7.5%")
        assert heading == (
            "3 TRIALS FAILED IN A ROW — last FIX_BREAK, and 2 of them dropped over 7.5% of "
            "their frames; check the display before recalibrating"
        )


class TestDropoutStreak:
    def test_n_dropouts_in_a_row_pause_on_the_nth(self):
        monitor = dropouts(3)
        assert monitor.count_dropout(ABORTED, row(FAULT_TRACKER_STOPPED)) is None
        assert monitor.count_dropout(ABORTED, row(FAULT_TRACKER_STOPPED)) is None
        streak = monitor.count_dropout(ABORTED, row(FAULT_TRACKER_STOPPED, "link lost"))
        assert streak == DropoutStreak(count=3, fault=FAULT_TRACKER_STOPPED, detail="link lost")

    def test_a_fault_in_the_closing_phase_counts(self):
        # The outcome stood, but the tracker still stopped: the row says so.
        monitor = dropouts(1)
        assert monitor.count_dropout(CORRECT, row(FAULT_TRACKER_STOPPED)) is not None

    def test_a_clean_trial_ends_the_run(self):
        monitor = dropouts(2)
        monitor.count_dropout(ABORTED, row(FAULT_TRACKER_STOPPED))
        assert monitor.count_dropout(CORRECT, row()) is None
        assert monitor.count_dropout(ABORTED, row(FAULT_TRACKER_STOPPED)) is None

    def test_dropped_frames_is_not_a_device_dropout_and_ends_the_run(self):
        monitor = dropouts(2)
        monitor.count_dropout(ABORTED, row(FAULT_TRACKER_STOPPED))
        assert monitor.count_dropout(DROPPED_FRAMES, row(FAULT_DROPPED_FRAMES)) is None
        assert monitor.count_dropout(ABORTED, row(FAULT_TRACKER_STOPPED)) is None

    def test_a_paused_trial_neither_counts_nor_ends_it(self):
        monitor = dropouts(2)
        monitor.count_dropout(ABORTED, row(FAULT_TRACKER_STOPPED))
        assert monitor.count_dropout(PAUSED, row(FAULT_TRACKER_STOPPED)) is None
        assert monitor.count_dropout(ABORTED, row(FAULT_TRACKER_STOPPED)) is not None

    def test_it_is_reported_until_its_pause_is_raised(self):
        # A reward failure on the same trial takes the pause screen first; the
        # dropouts are then still owed their own pause.
        monitor = dropouts(2)
        monitor.count_dropout(ABORTED, row(FAULT_TRACKER_STOPPED))
        assert monitor.count_dropout(ABORTED, row(FAULT_TRACKER_STOPPED)) is not None
        streak = monitor.count_dropout(ABORTED, row(FAULT_TRACKER_STOPPED))
        assert streak is not None and streak.count == 3

    def test_raising_its_pause_restarts_the_count(self):
        monitor = dropouts(2)
        monitor.count_dropout(ABORTED, row(FAULT_TRACKER_STOPPED))
        monitor.count_dropout(ABORTED, row(FAULT_TRACKER_STOPPED))
        monitor.dropout_pause_raised()
        assert monitor.count_dropout(ABORTED, row(FAULT_TRACKER_STOPPED)) is None
        assert monitor.count_dropout(ABORTED, row(FAULT_TRACKER_STOPPED)) is not None

    def test_the_two_streaks_are_counted_apart(self):
        # A tracker-stopped trial is nothing to the subject's streak and
        # everything to the device's.
        monitor = StreakMonitor(max_consecutive_failures=1, max_consecutive_dropouts=1)
        assert fail(monitor, ABORTED, fault=FAULT_TRACKER_STOPPED) is None
        assert monitor.count_dropout(ABORTED, row(FAULT_TRACKER_STOPPED)) is not None


class TestDropoutHeading:
    def test_the_tracker_is_named_with_what_it_said(self):
        heading = DropoutStreak(count=3, fault=FAULT_TRACKER_STOPPED, detail="link lost").heading()
        assert heading == (
            "THE EYE TRACKER DROPPED OUT ON 3 TRIALS IN A ROW — last: link lost. Check the "
            "tracker and its connection to this machine before resuming; those trials are "
            "served again"
        )

    def test_an_unknown_check_is_named_by_its_reason(self):
        heading = DropoutStreak(count=2, fault="pump_dry", detail=None).heading()
        assert heading.startswith("A DEVICE FAILED ITS HEALTH CHECK (pump_dry) ON 2 TRIALS")
        assert "last:" not in heading


class TestFaultClassification:
    def test_only_a_device_fault_cuts_a_trial_short(self):
        assert cut_short_by_device(FAULT_TRACKER_STOPPED)
        assert not cut_short_by_device(FAULT_DROPPED_FRAMES)
        assert not cut_short_by_device(None)

    @pytest.mark.parametrize("fault", [NO_FAULT, "", None, FAULT_DROPPED_FRAMES])
    def test_a_row_without_a_device_fault_names_none(self, fault):
        assert device_fault({"fault": fault}) is None

    def test_a_row_without_a_fault_column_names_none(self):
        assert device_fault({}) is None

    def test_a_device_fault_is_named_by_its_reason(self):
        assert device_fault(row(FAULT_TRACKER_STOPPED)) == FAULT_TRACKER_STOPPED
