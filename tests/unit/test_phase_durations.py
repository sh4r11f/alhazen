"""A phase of d seconds is on screen for d, to the nearest frame (architecture §2.3).

Every timed phase used to show one frame more than its duration — two more for
StimulusResponse and ResponseWindow — because the engine flipped the frame on
which a phase, having drawn, found its time up. These tests run every library
phase that has a duration, a timeout or a dwell through the real TrialEngine,
log every draw with the flip that showed it, and count. They pin:

- the count at 60, 120, 144 and 165 Hz, for durations that are and are not
  a whole number of frames, rounded to the nearest frame and half up;
- the mechanism: the engine does not flip a frame a phase ended undrawn, the
  next phase draws it with the same inputs, and events still carry the flip
  that showed them;
- what happens to a dropped frame, a measured refresh rate a hair off the
  nominal one, and a clock whose flips jitter;
- ``TrialContext.time_up`` and ``end_undrawn`` on their own, including
  every refusal.
"""

from __future__ import annotations

import math
import warnings
from typing import Any

import numpy as np
import pytest

from alhazen.config.models import Duration, FrameQAConfig, RewardPulses, whole_frames
from alhazen.core.engine import TrialEngine
from alhazen.core.events import EventBus, EventSchema
from alhazen.core.trial import (
    CircleRegion,
    InputFrame,
    Outcome,
    PhaseAction,
    RewardCompletion,
    RewardRequest,
    TrialContext,
)
from alhazen.display.frames import FrameMonitor
from alhazen.display.screen import Screen
from alhazen.display.simulated import SimulatedDisplay
from alhazen.task.phases import (
    AcquireFixation,
    AdjustmentLoop,
    Blank,
    Feedback,
    HoldFixation,
    LandingCheck,
    LandingSample,
    ResponseWindow,
    StimulusResponse,
    TrialFeedback,
)
from alhazen.testing import EventCollector, FakeClock, FakeDisplay, ScriptedCommands, ScriptedInputs

SCREEN = Screen(width_px=1920, height_px=1080, px_per_deg=40.0)
FIX = CircleRegion((0.0, 0.0), 40.0)
TARGET = CircleRegion((400.0, 0.0), 60.0)
IN_FIX = InputFrame(gaze=(0.0, 0.0))
AWAY = InputFrame(gaze=(900.0, 900.0))
ON_TARGET = InputFrame(gaze=(400.0, 0.0))
BLINK = InputFrame(gaze=None)

DONE = Outcome("DONE", completed=True, success=True)
TIMED_OUT = Outcome("TIMED_OUT", completed=True, success=False)
BROKE = Outcome("BROKE", completed=False)
HIT = Outcome("HIT", completed=True, success=True)
MISS = Outcome("MISS", completed=True, success=False)

EVENTS = (
    "FIX_ON",
    "FIX_ACQUIRED",
    "HOLD_ON",
    "STIM_ON",
    "RESPONSE_ONSET",
    "LANDED",
    "RESPONSE_CUE",
    "RESPONSE",
    "NEXT_ON",
)

RATES = (60.0, 120.0, 144.0, 165.0)

# Frames each duration is on screen for, worked out by hand: d x hz rounded
# to the nearest whole frame, a value exactly half-way going up. The rows mix
# exact multiples (600 ms at 120 Hz is 72.0 frames) with durations that are
# not (600 ms at 144 Hz is 86.4), and include one exact half (500 ms at
# 165 Hz is 82.5, so 83). Before the fix every one of these was one frame
# longer, or two.
EXPECTED = {
    0.600: {60.0: 36, 120.0: 72, 144.0: 86, 165.0: 99},
    0.500: {60.0: 30, 120.0: 60, 144.0: 72, 165.0: 83},
    0.250: {60.0: 15, 120.0: 30, 144.0: 36, 165.0: 41},
    0.150: {60.0: 9, 120.0: 18, 144.0: 22, 165.0: 25},
    0.100: {60.0: 6, 120.0: 12, 144.0: 14, 165.0: 17},
}
CASES = [(d, hz, n) for d, by_rate in EXPECTED.items() for hz, n in by_rate.items()]


# ----------------------------------------------------------------------
# A rig that logs every draw with the flip that showed it
# ----------------------------------------------------------------------


class Logged:
    """A stimulus that notes, on every draw, the index of the flip that will
    show it: the display's flip count so far. The engine flips once per
    shown frame, so within one trial that index is also the frame index the
    engine hands ``on_frame_input``."""

    def __init__(self, rig: Rig, key: str) -> None:
        self._rig, self._key = rig, key
        self.colors: list[Any] = []

    def update(self, dt: float) -> None:
        return None

    def draw(self) -> None:
        self._rig.draws.append((self._rig.display.flip_count, self._key))

    def set_color(self, rgb: Any) -> None:
        # TrialFeedback recolours its stimulus; kept so a test can see it.
        self.colors.append(rgb)


class Next:
    """The phase after the one under test: draws ``next`` once and ends the
    trial. Its first frame is the frame the phase under test hands over on."""

    name = "next"

    def __init__(self, onset_event: str | None = None) -> None:
        self._onset_event = onset_event

    def on_enter(self, ctx: TrialContext) -> None:
        if self._onset_event is not None:
            ctx.emit_on_flip(self._onset_event)

    def on_frame(self, ctx: TrialContext) -> Any:
        ctx.stimuli["next"].draw()
        return DONE


class ShowOnce:
    """Draws ``key`` on one frame and advances: a previous phase, so the
    phase under test is entered after a flip the test can point at."""

    name = "show_once"

    def __init__(self, key: str = "cue") -> None:
        self._key = key

    def on_enter(self, ctx: TrialContext) -> None:
        return None

    def on_frame(self, ctx: TrialContext) -> Any:
        ctx.stimuli[self._key].draw()
        return PhaseAction.ADVANCE


class JitteryDisplay(FakeDisplay):
    """A FakeDisplay whose flip stamps scatter around the vsync grid, the way
    a real panel's do: flip k lands at its grid time plus a seeded offset of
    up to ``jitter_frames`` of a frame either way. The offsets do not add up
    — the panel's vsync keeps time; only the stamps wobble."""

    def __init__(self, clock: FakeClock, frame_period_s: float, jitter_frames: float, seed: int):
        super().__init__(clock, frame_period_s)
        self._rng = np.random.default_rng(seed)
        self._jitter = jitter_frames
        self._grid = clock.now()

    def flip(self, clear: bool = True) -> None:
        self._grid += self.frame_period_s
        offset = self.frame_period_s * self._rng.uniform(-self._jitter, self._jitter)
        self._clock.advance(self._grid + offset - self._clock.now())
        self.flip_count += 1


class Rig:
    """One trial through the real TrialEngine at ``hz``, every draw logged.

    ``monitor_hz`` wires a FrameMonitor at that (measured) rate, as a session
    does; without it the engine takes the frame period from the display.
    ``late`` maps a flip index to how many frame periods late that flip is:
    the overlay, which runs just before every flip, sets it.
    """

    def __init__(
        self,
        hz: float,
        inputs: list[InputFrame] | Any = None,
        *,
        monitor_hz: float | None = None,
        display: Any = None,
        late: dict[int, float] | None = None,
        reward_requests: Any = None,
        health_checks: tuple = (),
        commands: Any = None,
    ) -> None:
        self.clock = FakeClock()
        self.display = (
            display(self.clock) if display is not None else FakeDisplay(self.clock, 1.0 / hz)
        )
        self.draws: list[tuple[int, str]] = []
        self.flips: dict[int, float] = {}
        self.frame_inputs: dict[int, InputFrame] = {}
        self.collector = EventCollector()
        bus = EventBus()
        bus.subscribe(self.collector)
        self._late = dict(late or {})
        provider = inputs if callable(inputs) else ScriptedInputs(inputs or [IN_FIX])

        def note(trial_index: int, frame_index: int, t: float, frame: InputFrame) -> None:
            # The engine's own stamp of each flip, and the inputs it logged
            # for that frame (what the database's frame tables receive).
            self.flips[frame_index] = t
            self.frame_inputs[frame_index] = frame

        def overlay(ctx: TrialContext) -> None:
            late_by = self._late.get(self.display.flip_count)
            if late_by is not None:
                self.display.next_flip_extra = late_by * self.display.frame_period_s

        self.engine = TrialEngine(
            display=self.display,
            clock=self.clock,
            bus=bus,
            schema=EventSchema(EVENTS),
            commands=commands or ScriptedCommands(),
            frame_monitor=(
                FrameMonitor(FrameQAConfig(), monitor_hz) if monitor_hz is not None else None
            ),
            input_provider=provider,
            on_frame_input=note,
            overlay=overlay,
            reward_requests=reward_requests,
            health_checks=health_checks,
        )
        self.stimuli = {
            key: Logged(self, key) for key in ("fixation", "target", "cue", "next", "fb")
        }

    def run(self, phases: list, record: dict | None = None, seed: int = 0) -> Any:
        ctx = TrialContext(
            clock=self.clock,
            screen=SCREEN,
            rng=np.random.default_rng(seed),
            trial_index=1,
            params={},
            stimuli=self.stimuli,
            regions={"fixation": FIX, "target": TARGET},
            record=dict(record or {}),
        )
        self.ctx = ctx
        return self.engine.run_trial(ctx, phases)

    def frames_of(self, key: str) -> list[int]:
        """The flip indices on which ``key`` was drawn, in order."""
        return sorted({flip for flip, drawn in self.draws if drawn == key})

    def event_t(self, name: str) -> float:
        (event,) = [e for e in self.collector.events if e.name == name]
        return event.t


def contiguous(frames: list[int]) -> bool:
    return frames == list(range(frames[0], frames[0] + len(frames))) if frames else True


# ----------------------------------------------------------------------
# Every library phase with a duration, a timeout or a dwell
# ----------------------------------------------------------------------


def moving_gaze(rig_box: dict) -> Any:
    """An eye that never settles: a new sample every frame, a degree further
    right each time (60 deg/s at 60 Hz and more above it), stamped with the
    clock — so a saccade-offset LandingSample can only end at its cap."""
    state = {"x": 0.0}

    def provide() -> InputFrame:
        state["x"] += 40.0
        return InputFrame(gaze=(state["x"], -300.0), gaze_t=rig_box["rig"].clock.now())

    return provide


# Each entry builds the phases of a trial, names the stimulus whose frames
# are counted, and the gaze it runs with. The phase under test draws only
# that stimulus, so its frames on screen are the flips that showed it.
PHASES: dict[str, Any] = {
    "HoldFixation": lambda d: (
        [HoldFixation(duration_s=d, on_break=BROKE), Next()],
        "fixation",
        [IN_FIX],
    ),
    "StimulusResponse timeout": lambda d: (
        [StimulusResponse("target", timeout_s=d, on_timeout=TIMED_OUT)],
        "target",
        [IN_FIX],
    ),
    "AcquireFixation timeout": lambda d: (
        [AcquireFixation(timeout_s=d, on_timeout=TIMED_OUT)],
        "fixation",
        [AWAY],
    ),
    "LandingCheck timeout": lambda d: (
        [LandingCheck("target", timeout_s=d, on_hit=HIT, on_miss=MISS, stimulus_keys=["fixation"])],
        "fixation",
        [IN_FIX],
    ),
    "LandingSample dwell": lambda d: (
        [
            LandingSample(
                "target",
                on_hit=HIT,
                on_miss=MISS,
                dwell_s=d,
                onset_event=None,
                stimulus_keys=["fixation"],
            )
        ],
        "fixation",
        [IN_FIX],
    ),
    "ResponseWindow timeout": lambda d: (
        [
            ResponseWindow(
                {"left": HIT}, timeout_s=d, on_timeout=TIMED_OUT, stimulus_keys=["fixation"]
            )
        ],
        "fixation",
        [IN_FIX],
    ),
    "ResponseWindow timeout, no onset event": lambda d: (
        [
            ResponseWindow(
                {"left": HIT},
                timeout_s=d,
                on_timeout=TIMED_OUT,
                stimulus_keys=["fixation"],
                onset_event=None,
            )
        ],
        "fixation",
        [IN_FIX],
    ),
    "AdjustmentLoop timeout": lambda d: (
        [
            AdjustmentLoop(
                lambda ctx, wheel: None,
                lambda ctx: 0.5,
                timeout_s=d,
                on_commit=HIT,
                on_timeout=TIMED_OUT,
                stimulus_keys=["fixation"],
            )
        ],
        "fixation",
        [IN_FIX],
    ),
    "Feedback": lambda d: (
        [Feedback(["fixation"], duration_s=d), Next()],
        "fixation",
        [IN_FIX],
    ),
    "TrialFeedback": lambda d: (
        [TrialFeedback(lambda ctx: True, then=DONE, duration_s=d, stimulus_key="fixation")],
        "fixation",
        [IN_FIX],
    ),
}


class TestEveryTimedPhaseIsOnScreenForItsDuration:
    @pytest.mark.parametrize("name", sorted(PHASES))
    @pytest.mark.parametrize(("d", "hz", "expected"), CASES)
    def test_frames_on_screen(self, name: str, d: float, hz: float, expected: int) -> None:
        phases, key, inputs = PHASES[name](d)
        rig = Rig(hz, inputs)
        rig.run(phases)
        frames = rig.frames_of(key)
        assert len(frames) == expected, (name, d, hz, frames)
        assert contiguous(frames)
        # What follows appears exactly `expected` frames after the phase's
        # first: the next phase's first frame, or the empty frame that takes
        # the stimulus off — a real flip, stamped.
        after = frames[0] + expected
        assert after in rig.flips
        assert rig.flips[after] - rig.flips[frames[0]] == pytest.approx(expected / hz)
        assert (after, key) not in rig.draws

    @pytest.mark.parametrize(("d", "hz", "expected"), CASES)
    def test_blank_shows_nothing_for_its_duration(self, d: float, hz: float, expected: int) -> None:
        rig = Rig(hz)
        rig.run([ShowOnce("cue"), Blank(d), Next()])
        (cue,) = rig.frames_of("cue")
        (after,) = rig.frames_of("next")
        # The flips between the frame before and the frame after are the
        # blank's: drawn with nothing, on screen for the duration.
        assert after - cue - 1 == expected
        assert rig.flips[after] - rig.flips[cue] == pytest.approx((expected + 1) / hz)

    @pytest.mark.parametrize(("d", "hz", "expected"), CASES)
    def test_landing_sample_capped_by_max_wait(self, d: float, hz: float, expected: int) -> None:
        box: dict = {}
        rig = Rig(hz, moving_gaze(box))
        box["rig"] = rig
        result = rig.run(
            [
                LandingSample(
                    "target",
                    on_hit=HIT,
                    on_miss=MISS,
                    settle_speed_dva_per_s=5.0,
                    max_wait_s=d,
                    onset_event=None,
                    stimulus_keys=["fixation"],
                )
            ]
        )
        assert result.record["endpoint_settled"] is False
        assert len(rig.frames_of("fixation")) == expected

    @pytest.mark.parametrize("hz", RATES)
    @pytest.mark.parametrize("seed", range(6))
    def test_hold_fixation_jitter_lasts_the_drawn_duration(self, hz: float, seed: int) -> None:
        """The jitter is drawn per trial; whatever is drawn is what is shown,
        rounded to the nearest frame — never one more."""
        rig = Rig(hz)
        result = rig.run(
            [HoldFixation(duration_s=0.4, jitter_s=0.1, on_break=BROKE), Next()], seed=seed
        )
        drawn = result.record["hold_duration_s"]
        assert 0.3 <= drawn <= 0.5
        assert len(rig.frames_of("fixation")) == math.floor(drawn * hz + 0.5)

    @pytest.mark.parametrize(("d", "hz", "expected"), CASES)
    def test_landing_sample_dwell_runs_from_saccade_onset(
        self, d: float, hz: float, expected: int
    ) -> None:
        """Timed from RESPONSE_ONSET, the flip just before its own first frame:
        on screen for the dwell, and the landing judged on the sample read
        `dwell` after onset — the same sample as before the fix."""
        rig = Rig(hz, [IN_FIX, IN_FIX, IN_FIX, ON_TARGET])
        result = rig.run(
            [
                StimulusResponse("target", timeout_s=2.0, on_timeout=TIMED_OUT),
                LandingSample(
                    "target", on_hit=HIT, on_miss=MISS, dwell_s=d, stimulus_keys=["fixation"]
                ),
            ]
        )
        assert result.outcome is HIT
        frames = rig.frames_of("fixation")
        assert len(frames) == expected
        onset = rig.event_t("RESPONSE_ONSET")
        assert onset == rig.flips[frames[0] - 1]
        assert result.record["endpoint_latency_ms"] == pytest.approx(expected / hz * 1000)
        # LANDED was queued on the frame the dwell ran out, which was not
        # drawn; it is stamped on the flip that took the stimulus off.
        assert rig.event_t("LANDED") == rig.flips[frames[0] + expected]

    @pytest.mark.parametrize(("d", "hz", "expected"), CASES)
    def test_acquire_fixation_blinks_for_its_period(
        self, d: float, hz: float, expected: int
    ) -> None:
        """Each half of the blink cycle is on screen for the period."""
        rig = Rig(hz, [AWAY])
        rig.run([AcquireFixation(timeout_s=5 * d, on_timeout=TIMED_OUT, blink_period_s=d)])
        frames = rig.frames_of("fixation")
        first_run = [f for f in frames if f < frames[0] + expected]
        assert first_run == list(range(frames[0], frames[0] + expected))
        assert frames[expected] == frames[0] + 2 * expected  # the dark half, then on again


class TestTheOldRuleIsGone:
    """The regression: each of these was one frame longer (or two) before."""

    def test_a_600_ms_hold_at_120_hz_is_72_frames_not_73(self) -> None:
        rig = Rig(120.0)
        rig.run([HoldFixation(duration_s=0.600, on_break=BROKE), Next()])
        assert len(rig.frames_of("fixation")) == 72
        (next_frame,) = rig.frames_of("next")
        assert next_frame == 72

    def test_a_500_ms_response_timeout_at_120_hz_is_60_frames_not_62(self) -> None:
        rig = Rig(120.0)
        result = rig.run([StimulusResponse("target", timeout_s=0.500, on_timeout=TIMED_OUT)])
        assert result.outcome is TIMED_OUT
        assert len(rig.frames_of("target")) == 60

    def test_a_600_ms_hold_at_165_hz_is_99_frames_not_100(self) -> None:
        rig = Rig(165.0)
        rig.run([HoldFixation(duration_s=0.600, on_break=BROKE), Next()])
        assert len(rig.frames_of("fixation")) == 99


# ----------------------------------------------------------------------
# Events still carry the flip that showed them
# ----------------------------------------------------------------------


class TestEventsLandOnTheFlipsThatShowedThem:
    @pytest.mark.parametrize("hz", RATES)
    def test_entry_events_and_stimulus_off_are_flips(self, hz: float) -> None:
        rig = Rig(hz)
        rig.run(
            [
                HoldFixation(duration_s=0.25, on_break=BROKE, onset_event="HOLD_ON"),
                StimulusResponse("target", timeout_s=0.15, on_timeout=TIMED_OUT),
            ]
        )
        hold, target = rig.frames_of("fixation"), rig.frames_of("target")
        n_hold, n_target = EXPECTED[0.250][hz], EXPECTED[0.150][hz]
        assert (len(hold), len(target)) == (n_hold, n_target)
        # Each entry event carries the stamp of the flip that first showed
        # its phase — the hold's on the hold's first frame, the stimulus's
        # on the frame the hold handed over (which the hold never drew).
        assert rig.event_t("HOLD_ON") == rig.flips[hold[0]]
        assert rig.event_t("STIM_ON") == rig.flips[target[0]]
        assert target[0] == hold[-1] + 1
        assert rig.event_t("STIM_ON") - rig.event_t("HOLD_ON") == pytest.approx(n_hold / hz)
        # The stimulus goes off on a flip of its own, n_target frames on.
        assert rig.flips[target[-1] + 1] - rig.event_t("STIM_ON") == pytest.approx(n_target / hz)

    def test_a_phase_with_no_frames_is_never_drawn_and_its_event_is_the_next_flip(self) -> None:
        """A zero duration: ended before its first frame, so nothing of it is
        ever shown, and its entry event is stamped on the flip that shows the
        next phase — the moment it both began and ended on screen."""
        rig = Rig(120.0)
        rig.run(
            [
                HoldFixation(duration_s=0.0, on_break=BROKE, onset_event="HOLD_ON"),
                Next(onset_event="NEXT_ON"),
            ]
        )
        assert rig.frames_of("fixation") == []
        assert rig.frames_of("next") == [0]
        assert rig.event_t("HOLD_ON") == rig.event_t("NEXT_ON") == rig.flips[0]


# ----------------------------------------------------------------------
# The engine's side: a frame ended undrawn is not flipped
# ----------------------------------------------------------------------


class CountingInputs:
    """An input provider that counts its calls and serves scripted keys."""

    def __init__(self, keys_on_call: dict[int, tuple[str, ...]] | None = None) -> None:
        self.calls = 0
        self._keys = dict(keys_on_call or {})

    def __call__(self) -> InputFrame:
        frame = InputFrame(gaze=(0.0, 0.0), keys=self._keys.get(self.calls, ()))
        self.calls += 1
        return frame


class CountingCommands(ScriptedCommands):
    def __init__(self) -> None:
        super().__init__()
        self.polls = 0

    def poll(self):
        self.polls += 1
        return super().poll()


class SeesKeys:
    """Records the keys it sees on each frame, and ends after its first."""

    name = "sees_keys"

    def __init__(self) -> None:
        self.seen: list[tuple[str, ...]] = []

    def on_enter(self, ctx: TrialContext) -> None:
        return None

    def on_frame(self, ctx: TrialContext) -> Any:
        self.seen.append(ctx.inputs.keys)
        ctx.stimuli["next"].draw()
        return DONE


class TestTheEngineDoesNotFlipAFrameEndedUndrawn:
    def test_one_flip_per_frame_shown(self) -> None:
        rig = Rig(60.0)
        rig.run([HoldFixation(duration_s=3 / 60, on_break=BROKE), Next()])
        # Three hold frames, the next phase's one, the blank: no flip for the
        # frame on which the hold found its time up.
        assert rig.frames_of("fixation") == [0, 1, 2]
        assert rig.frames_of("next") == [3]
        assert rig.display.flip_count == 5

    def test_the_next_phase_gets_the_same_frame_and_its_inputs_once(self) -> None:
        """Commands, health checks and inputs are taken once per frame: the
        frame the hold ended on is the next phase's first, so a key in it
        reaches that phase — and the database's frame log — exactly once."""
        inputs = CountingInputs(keys_on_call={3: ("x",)})
        commands = CountingCommands()
        checks = {"n": 0}

        def check():
            checks["n"] += 1
            return

        rig = Rig(60.0, inputs, commands=commands, health_checks=(check,))
        sees = SeesKeys()
        rig.run([HoldFixation(duration_s=3 / 60, on_break=BROKE), sees])
        # Four frames shown (three hold, one next): four of each, not five.
        assert inputs.calls == commands.polls == checks["n"] == 4
        assert sees.seen == [("x",)]
        assert [rig.frame_inputs[i].keys for i in sorted(rig.frame_inputs)] == [(), (), (), ("x",)]

    def test_feedback_follows_a_timed_out_stimulus_with_no_blank_frame(self) -> None:
        """An Outcome ended undrawn hands its frame to the closing phase."""
        rig = Rig(120.0)
        result = rig.run(
            [
                StimulusResponse("target", timeout_s=0.1, on_timeout=TIMED_OUT),
                TrialFeedback(lambda ctx: True, then=DONE, duration_s=0.05, stimulus_key="fb"),
            ]
        )
        assert result.outcome is TIMED_OUT
        target, feedback = rig.frames_of("target"), rig.frames_of("fb")
        assert len(target) == 12
        assert feedback[0] == target[-1] + 1
        assert len(feedback) == 6

    def test_with_no_closing_phase_the_stimulus_goes_off_on_an_empty_frame(self) -> None:
        rig = Rig(120.0)
        rig.run([StimulusResponse("target", timeout_s=0.1, on_timeout=TIMED_OUT)])
        # Twelve frames of the stimulus, one empty frame (stamped, logged)
        # that takes it off, then the engine's blank.
        assert rig.frames_of("target") == list(range(12))
        assert sorted(rig.flips) == list(range(13))
        assert rig.display.flip_count == 14

    def test_a_reward_requested_on_an_undrawn_frame_goes_with_the_next_flip(self) -> None:
        sink = FakeRewardSink()
        rig = Rig(60.0, reward_requests=sink)
        rig.run([PayAtTimeUp(duration_s=2 / 60), Next()])
        (request,) = sink.submitted
        # Requested on frame 2, which the phase did not draw; commanded with
        # the flip that showed the next phase's first frame.
        assert request.frame == 2
        (reward,) = [e for e in rig.collector.events if e.name == "REWARD"]
        assert reward.t == rig.flips[2]

    def test_ending_undrawn_with_advance_off_the_end_is_still_refused(self) -> None:
        rig = Rig(60.0)
        with pytest.raises(RuntimeError, match="must end the trial with an Outcome"):
            rig.run([HoldFixation(duration_s=2 / 60, on_break=BROKE)])


class FakeRewardSink:
    def __init__(self) -> None:
        self.submitted: list[RewardRequest] = []

    def submit(self, request: RewardRequest) -> int:
        self.submitted.append(request)
        return 0

    def completed(self) -> list[RewardCompletion]:
        return []

    def wait_idle(self) -> None:
        return None


class PayAtTimeUp:
    """A custom timed phase that asks for a drop on the frame its time runs
    out — the frame it does not draw."""

    name = "pay_at_time_up"

    def __init__(self, duration_s: float) -> None:
        self._duration_s = duration_s

    def on_enter(self, ctx: TrialContext) -> None:
        self._t0 = ctx.clock.now()

    def on_frame(self, ctx: TrialContext) -> Any:
        if ctx.time_up(self._t0, self._duration_s):
            ctx.request_reward(RewardPulses(n_pulses=1, pulse_ms=20), "end_bonus")
            return ctx.end_undrawn(PhaseAction.ADVANCE)
        ctx.stimuli["fixation"].draw()
        return PhaseAction.CONTINUE


class Misbehaves:
    """Calls end_undrawn and then returns something else."""

    name = "misbehaves"

    def __init__(self, returns: Any) -> None:
        self._returns = returns

    def on_enter(self, ctx: TrialContext) -> None:
        return None

    def on_frame(self, ctx: TrialContext) -> Any:
        ctx.end_undrawn(PhaseAction.ADVANCE)
        return self._returns


class TestEndUndrawnIsLoudAboutMisuse:
    @pytest.mark.parametrize("returns", [PhaseAction.CONTINUE, DONE])
    def test_returning_something_else_is_refused_naming_the_phase(self, returns: Any) -> None:
        rig = Rig(60.0)
        with pytest.raises(TypeError, match="misbehaves.*end_undrawn"):
            rig.run([Misbehaves(returns), Next()])

    @pytest.mark.parametrize("then", [PhaseAction.CONTINUE, "ADVANCED", None, 3])
    def test_it_takes_only_advance_or_an_outcome(self, then: Any) -> None:
        ctx = bare_context()
        with pytest.raises(TypeError, match="end_undrawn"):
            ctx.end_undrawn(then)

    def test_it_returns_what_it_was_given(self) -> None:
        ctx = bare_context()
        assert ctx.end_undrawn(PhaseAction.ADVANCE) == PhaseAction.ADVANCE
        assert ctx.end_undrawn(DONE) is DONE


# ----------------------------------------------------------------------
# Dropped frames, a measured rate, a jittery clock, the simulated display
# ----------------------------------------------------------------------


class TestAPhaseEndsByTimeNotByFrameCount:
    def test_a_dropped_frame_inside_a_phase_costs_it_a_frame_not_its_end(self) -> None:
        # The hold's sixth frame stays up for two periods: the hold shows 11
        # frames, and the stimulus still appears 100 ms after its first.
        rig = Rig(120.0, late={6: 1.0})
        rig.run(
            [
                ShowOnce("cue"),
                HoldFixation(duration_s=0.1, on_break=BROKE, onset_event="HOLD_ON"),
                StimulusResponse("target", timeout_s=0.05, on_timeout=TIMED_OUT),
            ]
        )
        assert len(rig.frames_of("fixation")) == 11
        assert rig.event_t("STIM_ON") - rig.event_t("HOLD_ON") == pytest.approx(0.1)

    def test_a_late_first_flip_is_absorbed_and_the_schedule_kept(self) -> None:
        # The hold's own first flip is a frame late: the hold is timed from
        # the flip before it, so it gives that frame back and the stimulus
        # appears when it would have without the drop.
        rig = Rig(120.0, late={1: 1.0})
        rig.run(
            [
                ShowOnce("cue"),
                HoldFixation(duration_s=0.1, on_break=BROKE, onset_event="HOLD_ON"),
                StimulusResponse("target", timeout_s=0.05, on_timeout=TIMED_OUT),
            ]
        )
        (cue,) = rig.frames_of("cue")
        assert len(rig.frames_of("fixation")) == 11
        assert rig.event_t("STIM_ON") - rig.flips[cue] == pytest.approx(13 / 120)

    @pytest.mark.parametrize(("late_by", "expected"), [(0.4, 12), (0.6, 11)])
    def test_half_a_frame_late_is_the_line(self, late_by: float, expected: int) -> None:
        """A flip less than half a frame late is still one frame; more is
        two — the line frame QA's default tolerance draws."""
        rig = Rig(120.0, late={5: late_by})
        rig.run([HoldFixation(duration_s=0.1, on_break=BROKE), Next()])
        assert len(rig.frames_of("fixation")) == expected

    @pytest.mark.parametrize("measured_hz", [119.99, 120.01, 119.95, 120.05])
    def test_exact_multiples_survive_a_measured_rate_off_nominal(self, measured_hz: float) -> None:
        """The engine times with the measured refresh rate, never exactly the
        nominal one. 600 ms is 72.006 frames at 120.01 Hz: the nearest frame
        is 72, where "the next frame" would make it 73."""
        rig = Rig(120.0, monitor_hz=measured_hz)
        rig.run([HoldFixation(duration_s=0.600, on_break=BROKE), Next()])
        assert len(rig.frames_of("fixation")) == 72

    @pytest.mark.parametrize("seed", range(8))
    @pytest.mark.parametrize(("d", "hz", "expected"), [(0.6, 120.0, 72), (0.5, 144.0, 72)])
    def test_exact_multiples_survive_jittery_flip_stamps(
        self, seed: int, d: float, hz: float, expected: int
    ) -> None:
        rig = Rig(
            hz,
            display=lambda clock: JitteryDisplay(clock, 1.0 / hz, jitter_frames=0.2, seed=seed),
        )
        rig.run([HoldFixation(duration_s=d, on_break=BROKE), Next()])
        assert len(rig.frames_of("fixation")) == expected

    @pytest.mark.parametrize(("d", "hz", "expected"), CASES)
    def test_on_the_simulated_display_in_simulated_time(
        self, d: float, hz: float, expected: int
    ) -> None:
        """A SimulatedDisplay handed the clock's advance moves it one frame per
        flip, and reports its frame period to an engine with no monitor."""
        rig = Rig(hz, display=lambda clock: SimulatedDisplay(hz, advance=clock.advance))
        rig.run([HoldFixation(duration_s=d, on_break=BROKE), Next()])
        assert len(rig.frames_of("fixation")) == expected


class TestWhatAPhaseStillSeesOnItsLastFrame:
    def test_a_blink_read_on_the_time_up_frame_is_a_break(self) -> None:
        rig = Rig(60.0, [IN_FIX, IN_FIX, IN_FIX, IN_FIX, BLINK])
        result = rig.run([HoldFixation(duration_s=4 / 60, on_break=BROKE), Next()])
        assert result.outcome is BROKE

    def test_a_departure_read_on_the_time_up_frame_is_a_response(self) -> None:
        # 12 frames at 120 Hz; the departure is read at the start of the
        # 13th frame — the one on which the time is up — and counts.
        rig = Rig(120.0, [IN_FIX] * 12 + [AWAY])
        result = rig.run([StimulusResponse("target", timeout_s=0.1, on_timeout=TIMED_OUT), Next()])
        assert result.outcome is DONE
        assert result.record["rt_ms"] == pytest.approx(11 / 120 * 1000)

    def test_a_departure_read_after_the_stimulus_has_gone_is_a_timeout(self) -> None:
        rig = Rig(120.0, [IN_FIX] * 13 + [AWAY])
        result = rig.run([StimulusResponse("target", timeout_s=0.1, on_timeout=TIMED_OUT), Next()])
        assert result.outcome is TIMED_OUT

    def test_a_key_read_on_the_time_up_frame_is_a_response(self) -> None:
        inputs = [IN_FIX] * 12 + [InputFrame(gaze=(0.0, 0.0), keys=("left",))]
        rig = Rig(120.0, inputs)
        result = rig.run(
            [
                ResponseWindow(
                    {"left": HIT}, timeout_s=0.1, on_timeout=TIMED_OUT, stimulus_keys=["fixation"]
                )
            ]
        )
        assert result.outcome is HIT

    def test_a_commit_read_on_the_time_up_frame_counts(self) -> None:
        inputs = [IN_FIX] * 12 + [InputFrame(gaze=(0.0, 0.0), keys=("space",))]
        rig = Rig(120.0, inputs)
        result = rig.run(
            [
                AdjustmentLoop(
                    lambda ctx, wheel: None,
                    lambda ctx: 0.5,
                    timeout_s=0.1,
                    on_commit=HIT,
                    on_timeout=TIMED_OUT,
                    stimulus_keys=["fixation"],
                )
            ]
        )
        assert result.outcome is HIT


# ----------------------------------------------------------------------
# Where the frame period comes from
# ----------------------------------------------------------------------


class NoPeriodDisplay:
    """A display that reports no frame period — a backend written before
    there was one. All the engine needs of it is ``flip``."""

    kind = "simulated"
    window = None

    def __init__(self, clock: FakeClock, frame_period_s: float) -> None:
        self._clock, self._period = clock, frame_period_s
        self.flip_count = 0

    def flip(self, clear: bool = True) -> None:
        self._clock.advance(self._period)
        self.flip_count += 1


class TestTheFramePeriod:
    def test_the_engine_sets_it_from_the_frame_monitor(self) -> None:
        rig = Rig(120.0, monitor_hz=119.98)
        rig.run([Next()])
        assert rig.ctx.frame_period_s == pytest.approx(1 / 119.98)

    def test_without_a_monitor_it_is_the_displays(self) -> None:
        rig = Rig(144.0)
        rig.run([Next()])
        assert rig.ctx.frame_period_s == pytest.approx(1 / 144)

    def test_the_simulated_display_reports_the_frame_it_moves_by(self) -> None:
        assert SimulatedDisplay(120.0, frame_period_s=0.01).frame_period_s == 0.01
        # Unpaced: the nominal frame, the one simulated time moves by.
        assert SimulatedDisplay(120.0, frame_period_s=0.0).frame_period_s == pytest.approx(1 / 120)
        assert SimulatedDisplay(165.0).frame_period_s == pytest.approx(1 / 165)

    def test_with_neither_a_phase_falls_back_to_dt_and_says_so(self) -> None:
        rig = Rig(60.0, display=lambda clock: NoPeriodDisplay(clock, 1 / 60))
        with pytest.warns(DeprecationWarning, match="frame_period_s.*3.0"):
            rig.run([HoldFixation(duration_s=3 / 60, on_break=BROKE), Next()])
        assert rig.ctx.frame_period_s is None
        assert len(rig.frames_of("fixation")) == 3


# ----------------------------------------------------------------------
# time_up on its own
# ----------------------------------------------------------------------


def bare_context(frame_period_s: float | None = 1 / 120, clock: FakeClock | None = None):
    ctx = TrialContext(
        clock=clock or FakeClock(),
        screen=SCREEN,
        rng=np.random.default_rng(0),
        trial_index=1,
        params={},
    )
    ctx.frame_period_s = frame_period_s
    return ctx


def after_frames(n: int, hz: float, duration_s: float) -> bool:
    """time_up after n frames of 1/hz, added one at a time as flips add them."""
    clock = FakeClock()
    ctx = bare_context(1 / hz, clock)
    since = clock.now()
    for _ in range(n):
        clock.advance(1 / hz)
    return ctx.time_up(since, duration_s)


class TestTimeUp:
    @pytest.mark.parametrize(("d", "hz", "expected"), CASES)
    def test_it_is_up_after_the_rounded_frames_and_not_one_before(
        self, d: float, hz: float, expected: int
    ) -> None:
        assert not after_frames(expected - 1, hz, d)
        assert after_frames(expected, hz, d)

    def test_a_zero_duration_is_up_at_once(self) -> None:
        assert after_frames(0, 120.0, 0.0)

    def test_less_than_half_a_frame_is_no_frame_at_all(self) -> None:
        assert after_frames(0, 120.0, 0.4 / 120)
        assert not after_frames(0, 120.0, 0.6 / 120)

    def test_it_reads_the_contexts_clock(self) -> None:
        clock = FakeClock(start=10.0)
        ctx = bare_context(0.01, clock)
        assert not ctx.time_up(10.0, 0.05)
        clock.advance(0.05)
        assert ctx.time_up(10.0, 0.05)

    @pytest.mark.parametrize("bad", [-0.001, float("nan"), float("inf"), -float("inf")])
    def test_a_duration_that_is_not_a_finite_time_is_refused(self, bad: float) -> None:
        with pytest.raises(ValueError, match="duration"):
            bare_context().time_up(0.0, bad)

    def test_a_start_in_the_future_is_refused(self) -> None:
        with pytest.raises(ValueError, match="since"):
            bare_context().time_up(1.0, 0.1)

    @pytest.mark.parametrize("bad", [0.0, -1 / 60, float("nan"), float("inf")])
    def test_a_frame_period_that_is_not_one_is_refused(self, bad: float) -> None:
        with pytest.raises(ValueError, match="frame_period_s"):
            bare_context(bad).time_up(0.0, 0.1)

    def test_with_no_frame_period_it_uses_dt_and_warns(self) -> None:
        clock = FakeClock()
        ctx = bare_context(None, clock)
        ctx.dt = 1 / 120
        clock.advance(12 / 120)
        with pytest.warns(DeprecationWarning, match="removed in 3.0"):
            assert ctx.time_up(0.0, 0.1)

    def test_with_a_frame_period_it_does_not_warn(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert after_frames(12, 120.0, 0.1)


# ----------------------------------------------------------------------
# One rounding: Duration.n_frames agrees with what a phase shows
# ----------------------------------------------------------------------


class TestOneRounding:
    def test_exactly_half_way_rounds_up(self) -> None:
        # Rounding half to even gave 82 frames for 500 ms at 165 Hz and 116
        # for 700 ms: one short, one long. Half up is never short.
        assert Duration(ms=500).n_frames(165.0) == 83
        assert Duration(ms=700).n_frames(165.0) == 116
        assert Duration(ms=100).n_frames(165.0) == 17
        assert Duration(ms=37.5).n_frames(120.0) == 5

    def test_nearest_otherwise(self) -> None:
        assert Duration(ms=600).n_frames(120.0) == 72
        assert Duration(ms=600).n_frames(120.01) == 72
        assert Duration(ms=600).n_frames(144.0) == 86
        assert Duration(ms=150).n_frames(144.0) == 22
        assert Duration(frames=7).n_frames(144.0) == 7

    def test_whole_frames_refuses_what_is_not_a_duration(self) -> None:
        for bad in (-0.1, float("nan"), float("inf")):
            with pytest.raises(ValueError):
                whole_frames(bad, 1 / 120)
        with pytest.raises(ValueError):
            whole_frames(0.1, 0.0)

    @pytest.mark.parametrize("hz", RATES)
    @pytest.mark.parametrize("ms", [16.0, 37.5, 100.0, 150.0, 250.0, 300.0, 500.0, 700.0])
    def test_a_phase_lasts_what_duration_says(self, hz: float, ms: float) -> None:
        duration = Duration(ms=ms)
        rig = Rig(hz)
        rig.run([HoldFixation(duration_s=duration.seconds(hz), on_break=BROKE), Next()])
        assert len(rig.frames_of("fixation")) == duration.n_frames(hz)


class TestHoldFixationRefusesAJitterWiderThanItsDuration:
    def test_refused_at_construction(self) -> None:
        # A draw below zero would be a hold with no foreperiod at all on
        # some trials; that is caught where the numbers are written.
        with pytest.raises(ValueError, match="jitter_s"):
            HoldFixation(duration_s=0.1, jitter_s=0.2, on_break=BROKE)

    def test_a_jitter_equal_to_the_duration_is_allowed(self) -> None:
        HoldFixation(duration_s=0.1, jitter_s=0.1, on_break=BROKE)
