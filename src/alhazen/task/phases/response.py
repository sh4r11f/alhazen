"""Phases driven by the subject's hands: key responses and knob adjustment."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from alhazen.core.trial import Outcome, PhaseAction, TrialContext
from alhazen.task.phases._draw import draw_stimuli
from alhazen.task.phases._record import column_prefix, record_once


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

    ``timeout_s`` is how long the cue is on screen when no key comes: the
    phase's frames on screen are ``timeout_s`` to the nearest frame
    (``ctx.time_up``, counted from the flip before the cue's first frame),
    and the trial then ends as ``on_timeout``. Keys are read at the start of
    each frame and checked before the time, so the last key counted is one
    read at the start of the cue's last frame on screen — pressed up to one
    frame before ``timeout_s`` after the cue appeared. Before 2.5 the cue
    stayed up two frames past the timeout, and a key read at the timeout
    still counted.

    With ``onset_event=None`` there is no cue flip to wait for: the window
    opens when the phase is entered, keys count from its first frame, and the
    reaction time runs from ``on_enter``. A key read on that first frame may
    have been pressed before the phase began, during the frame before it; a
    task that drops the onset event has chosen that. The deadline is the
    same either way: the phase is on screen for ``timeout_s``.
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
                # A response ends the trial on this frame, which is shown,
                # as it always was.
                draw_stimuli(ctx, self._stimulus_keys)
                ctx.record[self._key_record_key] = key
                ctx.record[self._rt_record_key] = (now - reference_t) * 1000.0
                if self._response_event is not None:
                    ctx.emit_on_flip(self._response_event)
                return outcome
        # The deadline, after the keys (a key read on the frame it runs out
        # on still counts) and before drawing (that frame is not shown).
        # Counted from on_enter — right after the flip before the cue's first
        # frame — whether or not there is an onset event: the cue's own
        # stamp is the flip of that first frame, and counting from it would
        # keep the cue up one frame longer.
        if ctx.time_up(self._t0, self._timeout_s):
            return ctx.end_undrawn(self._on_timeout)
        draw_stimuli(ctx, self._stimulus_keys)
        return PhaseAction.CONTINUE


class AdjustmentLoop:
    """The subject turns a knob until the stimulus looks right, then commits.

    ``adjust`` is the task's own callback — it receives the context and this
    frame's wheel movement and does whatever "turning the knob" means for that
    stimulus (a contrast, an orientation, a position). The phase owns the loop,
    the committing, the timeout and the record; the task owns the meaning.

    ``timeout_s`` is how long the stimulus is on screen without a commit, to
    the nearest frame (``ctx.time_up``). The inputs read on the frame the
    time runs out on were made before it, so they still count: that frame's
    wheel movement is applied and its commit key honoured, and only then is
    the time asked. The setting recorded at a timeout therefore includes the
    last turn, whose effect the subject did not get to see.

    Records the setting under ``value_record_key`` (default
    ``adjusted_value``) at a commit or a timeout, and, under
    ``record_prefix`` (default ``adjustment``), ``<prefix>_turns`` — the
    frames the wheel moved on — at either, and ``<prefix>_s``, the time to
    the commit, at a commit only. A trial with two adjustments gives each its
    own ``value_record_key`` and ``record_prefix``, for the reason
    ``HoldFixation`` gives: any of the three columns the record already
    holds is still overwritten, with a ``FutureWarning`` naming it (since
    2.6), and 3.0 refuses it. A prefix that is not a plain identifier, or
    that makes one of the columns alhazen writes itself, is refused here.
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
        *,
        record_prefix: str = "adjustment",
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
        # Not checked as a column name, unlike record_prefix: it has been
        # accepted unchecked since it was added, and refusing a name that
        # works today is a MAJOR change. Its writes are guarded all the same.
        self._value_record_key = value_record_key
        self._commit_event = commit_event
        self._prefix = column_prefix(
            "AdjustmentLoop", "record_prefix", record_prefix, ("turns", "s")
        )

    def on_enter(self, ctx: TrialContext) -> None:
        self._t0 = ctx.clock.now()
        self._turns = 0

    def _record(self, ctx: TrialContext, key: str, value: Any, argument: str) -> None:
        """One of this phase's columns, never over another writer's value in
        silence (``HoldFixation`` says why)."""
        record_once(ctx, key, value, phase="AdjustmentLoop", argument=argument)

    def on_frame(self, ctx: TrialContext) -> str | Outcome:
        if ctx.inputs.wheel:
            self._adjust(ctx, ctx.inputs.wheel)
            self._turns += 1
        if self._commit_key in ctx.inputs.keys:
            # A commit ends the trial on this frame, which is shown, as it
            # always was.
            draw_stimuli(ctx, self._stimulus_keys)
            # Recorded at commit, from the task's own accessor: the setting the
            # subject settled on IS the measurement here.
            self._record(ctx, self._value_record_key, self._value(ctx), "value_record_key")
            self._record(ctx, f"{self._prefix}_turns", self._turns, "record_prefix")
            self._record(ctx, f"{self._prefix}_s", ctx.clock.now() - self._t0, "record_prefix")
            if self._commit_event is not None:
                ctx.emit_on_flip(self._commit_event)
            return self._on_commit
        # The time after the inputs and before drawing: the frame it runs out
        # on is not shown, so the stimulus is up for timeout_s.
        if self._timeout_s is not None and ctx.time_up(self._t0, self._timeout_s):
            # The setting at timeout is still recorded — the subject was
            # somewhere when they ran out of time, and that is data even
            # though the outcome says they never committed.
            self._record(ctx, self._value_record_key, self._value(ctx), "value_record_key")
            self._record(ctx, f"{self._prefix}_turns", self._turns, "record_prefix")
            assert self._on_timeout is not None
            return ctx.end_undrawn(self._on_timeout)
        draw_stimuli(ctx, self._stimulus_keys)
        return PhaseAction.CONTINUE
