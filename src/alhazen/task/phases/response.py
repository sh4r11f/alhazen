"""Phases driven by the subject's hands: key responses and knob adjustment."""

from __future__ import annotations

from collections.abc import Callable

from alhazen.core.trial import Outcome, PhaseAction, TrialContext


def _draw(ctx: TrialContext, keys: list[str]) -> None:
    for key in keys:
        stimulus = ctx.stimuli[key]
        stimulus.update(ctx.dt)
        stimulus.draw()


class ResponseWindow:
    """An n-AFC key response, with a deadline.

    ``keys`` maps a key name to the outcome pressing it produces, which is how
    a task says "left arrow means CORRECT on this trial and WRONG on the next"
    without the phase knowing anything about the design. The chosen key and the
    reaction time both land in the record.

    Reaction time runs from the flip that showed the response window's own
    onset event, for the same reason as everywhere else: that flip is when the
    subject could first have seen anything to respond to.

    Keys pressed before that flip are not responses, and are ignored rather
    than scored: a bound key is only read as a choice once every key in the
    frame's batch was pressed after the cue was on screen (see
    ``_reference_time``). Ignored, not recorded as an anticipation, because
    nothing in the library records one yet — ``StimulusResponse`` likewise
    just waits for its onset stamp.

    With ``onset_event=None`` there is no cue flip to wait for: the window
    opens when the phase is entered, keys count from its first frame, and the
    reaction time runs from ``on_enter``. A key read on that first frame may
    have been pressed before the phase began, during the frame before it; a
    task that drops the onset event has chosen that.
    """

    name = "response_window"

    def __init__(
        self,
        keys: dict[str, Outcome],
        timeout_s: float = 2.0,
        on_timeout: Outcome | None = None,
        stimulus_keys: list[str] | None = None,
        onset_event: str | None = "RESPONSE_CUE",
        response_event: str | None = "RESPONSE",
        rt_record_key: str = "rt_ms",
        key_record_key: str = "response_key",
    ) -> None:
        if not keys:
            raise ValueError("ResponseWindow needs at least one key mapped to an outcome")
        if on_timeout is None:
            raise ValueError("ResponseWindow needs an on_timeout outcome")
        self._keys = dict(keys)
        self._timeout_s = timeout_s
        self._on_timeout = on_timeout
        self._stimulus_keys = list(stimulus_keys or [])
        self._onset_event = onset_event
        self._response_event = response_event
        self._rt_record_key = rt_record_key
        self._key_record_key = key_record_key

    def on_enter(self, ctx: TrialContext) -> None:
        self._t0 = ctx.clock.now()
        # Whether the frame before this one already saw the onset flip's
        # stamp. Reset on every entry, so a phase object reused across trials
        # never carries one trial's cue into the next.
        self._onset_seen_last_frame = False
        if self._onset_event is not None:
            ctx.emit_on_flip(self._onset_event)

    def _reference_time(self, ctx: TrialContext) -> float | None:
        """The time this frame's keys are timed from, or None while they
        cannot be credited to the cue. Called exactly once per frame, since
        it notes whether this frame has seen the onset stamp.

        A frame's keys are everything pressed since the *previous* frame's
        read (``ResponseDevice.poll`` reports each press once). So a
        batch is all post-cue only if that previous read came after the cue's
        flip — that is, if the previous frame already saw the
        ``t_<onset_event>`` stamp the engine writes right after the flip.
        That rules out two frames.
        """
        if self._onset_event is None:
            return self._t0
        onset_t = ctx.record.get(f"t_{self._onset_event.lower()}")
        if onset_t is None:
            # The first frame: the cue has been drawn but not yet flipped, so
            # these keys were pressed with nothing on screen to answer — the
            # same wait StimulusResponse makes for its onset stamp.
            return None
        if not self._onset_seen_last_frame:
            # The first frame to see the stamp. Its keys were pressed between
            # the first frame's read and the cue's flip, while the cue was
            # waiting to be shown: the pre-cue queue arriving one frame late,
            # which would otherwise score with a reaction time of zero. The
            # only post-cue presses lost with it are those made in the
            # engine's bookkeeping just after the flip — far under any real
            # reaction time.
            self._onset_seen_last_frame = True
            return None
        return float(onset_t)

    def on_frame(self, ctx: TrialContext) -> str | Outcome:
        _draw(ctx, self._stimulus_keys)
        now = ctx.clock.now()
        reference_t = self._reference_time(ctx)
        # Before the cue, bound keys are dropped with the rest of the batch.
        # The poll that read them reported them for the last time, so they
        # cannot turn up on a later frame as a response either.
        if reference_t is not None:
            for key in ctx.inputs.keys:
                outcome = self._keys.get(key)
                if outcome is None:
                    # A key the task did not bind is not a response. Ignored
                    # rather than counted as a wrong answer: the subject's
                    # hand slipping onto an unbound key is not a decision.
                    continue
                ctx.record[self._key_record_key] = key
                ctx.record[self._rt_record_key] = (now - reference_t) * 1000.0
                if self._response_event is not None:
                    ctx.emit_on_flip(self._response_event)
                return outcome
        if now - self._t0 >= self._timeout_s:
            return self._on_timeout
        return PhaseAction.CONTINUE


class AdjustmentLoop:
    """The subject turns a knob until the stimulus looks right, then commits.

    ``adjust`` is the task's own callback — it receives the context and this
    frame's wheel movement and does whatever "turning the knob" means for that
    stimulus (a contrast, an orientation, a position). The phase owns the loop,
    the committing, the timeout and the record; the task owns the meaning.
    """

    name = "adjustment_loop"

    def __init__(
        self,
        adjust: Callable[[TrialContext, float], None],
        value: Callable[[TrialContext], float],
        commit_key: str = "space",
        timeout_s: float | None = None,
        on_commit: Outcome | None = None,
        on_timeout: Outcome | None = None,
        stimulus_keys: list[str] | None = None,
        value_record_key: str = "adjusted_value",
        commit_event: str | None = "RESPONSE",
    ) -> None:
        if on_commit is None:
            raise ValueError("AdjustmentLoop needs an on_commit outcome")
        if timeout_s is not None and on_timeout is None:
            raise ValueError("a timeout needs an on_timeout outcome")
        self._adjust = adjust
        self._value = value
        self._commit_key = commit_key
        self._timeout_s = timeout_s
        self._on_commit = on_commit
        self._on_timeout = on_timeout
        self._stimulus_keys = list(stimulus_keys or [])
        self._value_record_key = value_record_key
        self._commit_event = commit_event

    def on_enter(self, ctx: TrialContext) -> None:
        self._t0 = ctx.clock.now()
        self._turns = 0

    def on_frame(self, ctx: TrialContext) -> str | Outcome:
        if ctx.inputs.wheel:
            self._adjust(ctx, ctx.inputs.wheel)
            self._turns += 1
        _draw(ctx, self._stimulus_keys)
        if self._commit_key in ctx.inputs.keys:
            # Recorded at commit, from the task's own accessor: the setting the
            # subject settled on IS the measurement here.
            ctx.record[self._value_record_key] = self._value(ctx)
            ctx.record["adjustment_turns"] = self._turns
            ctx.record["adjustment_s"] = ctx.clock.now() - self._t0
            if self._commit_event is not None:
                ctx.emit_on_flip(self._commit_event)
            return self._on_commit
        if self._timeout_s is not None and ctx.clock.now() - self._t0 >= self._timeout_s:
            # The setting at timeout is still recorded — the subject was
            # somewhere when they ran out of time, and that is data even
            # though the outcome says they never committed.
            ctx.record[self._value_record_key] = self._value(ctx)
            ctx.record["adjustment_turns"] = self._turns
            assert self._on_timeout is not None
            return self._on_timeout
        return PhaseAction.CONTINUE
