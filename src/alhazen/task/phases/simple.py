"""The small phases every task ends up needing: a blank, and feedback."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from alhazen.core.trial import Outcome, PhaseAction, TrialContext

# What the fixation point turns for a good and a bad trial, in the renderer's
# signed RGB. Green and red rather than anything subtler: feedback is read
# across a room, at a glance, by someone who has just made a saccade.
SUCCESS_COLOR = (-1.0, 1.0, -1.0)
FAILURE_COLOR = (1.0, -1.0, -1.0)


class Blank:
    """Show nothing for a fixed time — an inter-stimulus gap, a mask-free
    interval, the pause before feedback. Drawn as an empty frame rather than
    skipped, so the display keeps flipping and frame QA keeps measuring."""

    name = "blank"

    def __init__(self, duration_s: float, then: Any = PhaseAction.ADVANCE) -> None:
        self._duration_s = duration_s
        self._then = then

    def on_enter(self, ctx: TrialContext) -> None:
        self._t0 = ctx.clock.now()

    def on_frame(self, ctx: TrialContext) -> str | Outcome:
        if ctx.clock.now() - self._t0 >= self._duration_s:
            return self._then
        return PhaseAction.CONTINUE


class Feedback:
    """Draw the named stimuli for a fixed time.

    Which stimulus is "correct feedback" and which is "wrong" is the task's
    choice, made in build_trial where the outcome so far is known; this phase
    just shows what it is given for as long as it is told to.
    """

    name = "feedback"

    def __init__(
        self,
        stimulus_keys: list[str],
        duration_s: float,
        then: Any = PhaseAction.ADVANCE,
        onset_event: str | None = None,
        on_show: Any = None,
    ) -> None:
        self._stimulus_keys = list(stimulus_keys)
        self._duration_s = duration_s
        self._then = then
        self._onset_event = onset_event
        self._on_show = on_show

    def on_enter(self, ctx: TrialContext) -> None:
        self._t0 = ctx.clock.now()
        if self._onset_event is not None:
            ctx.emit_on_flip(self._onset_event)
        if self._on_show is not None:
            self._on_show(ctx)

    def on_frame(self, ctx: TrialContext) -> str | Outcome:
        for key in self._stimulus_keys:
            stimulus = ctx.stimuli[key]
            stimulus.update(ctx.dt)
            stimulus.draw()
        if ctx.clock.now() - self._t0 >= self._duration_s:
            return self._then
        return PhaseAction.CONTINUE


class TrialFeedback:
    """Tell the subject how the trial went — after everything is measured.

    The fixation point turns green for a good trial and red for a bad one,
    the ``FEEDBACK`` event goes out on the flip that showed it (the session
    sounds the beep from that; a phase touches no hardware), and after
    ``duration_s`` the trial ends with the task's outcome.

    Two things are deliberately separate here, because an experiment's
    first use of this would have been wrong either way:

    - **The verdict is the task's, and it is not the outcome.** ``verdict``
      is a predicate over the record so far — an acceptance region, a
      latency bound, whatever the design calls a good trial — and its answer
      is written to the record as ``feedback``. ``then`` is the outcome, also
      the task's, and unchanged by the verdict. A saccade that missed the
      figure is still a completed, scored measurement; dropping those would
      leave a dataset of exactly the trials that agreed with the hypothesis.
      So the subject can be told a trial was not good enough while the
      scheduler is told it was complete.
    - **Feedback is never on screen while something is being measured.** A
      display whose whole premise is one ink value and one background cannot
      have a red dot on it mid-trial. This phase declares ``must_be_last``
      and the engine refuses it anywhere else, so the ordering is enforced
      rather than left to each task to get right.

    **Choose the predicate for what the subject was asked to do, not for the
    dependent measure.** The tempting default is to feed the measurement in —
    "landed inside the target" — and it is the one most likely to bias a
    result. Feedback delivered every trial is a stronger instruction than
    anything read once at the start: an experiment that measures *where* a
    saccade lands, and tells the subject "just move your eyes to it quickly,
    don't aim", would be training landing accuracy per trial while asking
    for speed, and could manufacture its own predicted effect. The honest
    verdict there is procedural — held fixation, saccaded within the window
    — which is why ``verdict`` is any predicate over the record rather than
    a built-in region test.

    The stimulus under ``stimulus_key`` must offer ``set_color``; the
    fixation point does, and the simulated stand-in records it.
    """

    name = "trial_feedback"
    must_be_last = True

    def __init__(
        self,
        verdict: Callable[[TrialContext], bool],
        then: Outcome | Callable[[TrialContext], Outcome],
        duration_s: float,
        stimulus_key: str = "fixation",
        success_color: tuple[float, float, float] = SUCCESS_COLOR,
        failure_color: tuple[float, float, float] = FAILURE_COLOR,
        record_key: str = "feedback",
        feedback_event: str = "FEEDBACK",
    ) -> None:
        if duration_s < 0:
            raise ValueError(f"feedback duration must be >= 0 s, got {duration_s}")
        self._verdict = verdict
        self._then = then
        self._duration_s = duration_s
        self._stimulus_key = stimulus_key
        self._success_color = success_color
        self._failure_color = failure_color
        self._record_key = record_key
        self._feedback_event = feedback_event

    def on_enter(self, ctx: TrialContext) -> None:
        good = bool(self._verdict(ctx))
        ctx.record[self._record_key] = "success" if good else "failure"
        stimulus = ctx.stimuli[self._stimulus_key]
        set_color = getattr(stimulus, "set_color", None)
        if set_color is None:
            raise TypeError(
                f"TrialFeedback cannot recolour stimulus {self._stimulus_key!r} "
                f"({type(stimulus).__name__}): it has no set_color(). Feedback needs the "
                f"fixation point, or a stimulus that can change colour."
            )
        set_color(self._success_color if good else self._failure_color)
        ctx.emit_on_flip(self._feedback_event, {"success": good})
        self._t0 = ctx.clock.now()

    def on_frame(self, ctx: TrialContext) -> str | Outcome:
        stimulus = ctx.stimuli[self._stimulus_key]
        stimulus.update(ctx.dt)
        stimulus.draw()
        if ctx.clock.now() - self._t0 >= self._duration_s:
            return self._then(ctx) if callable(self._then) else self._then
        return PhaseAction.CONTINUE
