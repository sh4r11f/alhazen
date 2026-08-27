"""The small phases every task ends up needing: a blank, and feedback."""

from __future__ import annotations

from typing import Any

from alhazen.core.trial import Outcome, PhaseAction, TrialContext


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
