"""Shared test scaffolding, built on alhazen.testing's public fakes."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from alhazen.config.models import (
    DEFAULT_MAX_CONSECUTIVE_DROPOUTS,
    DisplayConfig,
    EyeTrackerConfig,
    FrameQAConfig,
    MonitorConfig,
    RewardPulses,
    RigConfig,
    SessionConfig,
    SessionInfo,
)
from alhazen.core.commands import CommandSource
from alhazen.core.engine import TrialEngine
from alhazen.core.events import EventBus, EventSchema
from alhazen.core.trial import InputFrame, Outcome, PhaseAction, TrialContext
from alhazen.devices.eyetracker import EyeTracker, TrackerMessageSubscriber
from alhazen.devices.reward import RewardDispenser
from alhazen.devices.sync import SyncOutput, make_sync_subscriber
from alhazen.display.frames import FrameMonitor
from alhazen.display.screen import Screen
from alhazen.session.builder import (
    make_input_provider,
    make_manual_reward,
    make_tracker_health_check,
)
from alhazen.testing import EventCollector, FakeClock, FakeDisplay, ScriptedCommands

SCREEN = Screen(width_px=1920, height_px=1080, px_per_deg=40.0)
FRAME_S = 1 / 60

MONITOR = MonitorConfig(
    width_px=1920,
    height_px=1080,
    width_cm=60.0,
    distance_cm=60.0,
    refresh_rate_hz=60.0,
    fullscreen=False,
)


def load_example_task(example_dir: Path):
    """Import an example's ``task.py`` under a name unique to its directory.

    Every example ships a module called ``task``, so a plain ``import task``
    would hand the second test whichever one the first test happened to load
    (``sys.modules`` caches by name). Loading from the file path, under a
    directory-derived name, keeps the examples independent.
    """
    name = f"example_{example_dir.name}_task"
    spec = importlib.util.spec_from_file_location(name, example_dir / "task.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class TickingClock(FakeClock):
    """A FakeClock that also moves forward on every read, by ``tick_s``.

    FakeClock holds still between flips, so a timestamp read right after a
    flip and one read later in the same frame come out as the same number,
    and no test can tell which of the two an event carries. A real clock
    moves while code runs; this one moves on each ``now()``, the smallest
    step that makes the difference visible. ``advance`` still moves it by any
    amount, which is how a test stands in for a subscriber that takes time.
    """

    def __init__(self, tick_s: float = 1e-6) -> None:
        super().__init__()
        self.tick_s = tick_s

    def now(self) -> float:
        # The value returned is the time before this read's own tick, so the
        # first read after a flip sees the flip exactly.
        t = super().now()
        self.advance(self.tick_s)
        return t


def record_flips(flips: dict[int, float]) -> Callable[[int, int, float, InputFrame], None]:
    """An ``on_frame_input`` hook that keeps each frame's flip time under its
    frame index: the engine's own stamp of that flip, and the time the
    database's ``frames`` and ``frame_inputs`` tables carry for the frame."""

    def note(trial_index: int, frame_index: int, t: float, inputs: InputFrame) -> None:
        flips[frame_index] = t

    return note


def make_session_config(data_root, task_params: dict[str, Any] | None = None) -> SessionConfig:
    return SessionConfig(
        rig=RigConfig(
            monitor=MONITOR, display=DisplayConfig(backend="simulated"), data_root=data_root
        ),
        info=SessionInfo(subject="t01", session=1, run=1, task_name="test-task", seed=7),
        task_params=task_params or {},
        sources={"rig": "<inline>", "task": "<inline>"},
    )


class EngineHarness:
    """A TrialEngine wired entirely to fakes, with simulated time: every flip
    advances the clock by exactly one frame period."""

    def __init__(
        self,
        commands: CommandSource | None = None,
        frame_qa: FrameQAConfig | None = None,
        input_provider: Callable[[], InputFrame] | None = None,
        health_checks: tuple[Callable[[], str | None], ...] = (),
        declared_events: tuple[str, ...] = ("FIX_ON", "STIM_ON"),
        on_manual_reward: Callable[[], None] | None = None,
        overlay: Callable[[TrialContext], None] | None = None,
        reward_requests: Any = None,
        clock: FakeClock | None = None,
        on_frame_input: Callable[[int, int, float, InputFrame], None] | None = None,
    ) -> None:
        # Accepting a clock lets a test run the engine on a TickingClock,
        # which moves between reads as a real one does.
        self.clock = clock if clock is not None else FakeClock()
        self.display = FakeDisplay(self.clock, FRAME_S)
        self.bus = EventBus()
        self.collector = EventCollector()
        self.bus.subscribe(self.collector)
        self.schema = EventSchema(declared_events)
        self.commands = commands or ScriptedCommands()
        self.frame_monitor = FrameMonitor(frame_qa, 1 / FRAME_S) if frame_qa is not None else None
        self.engine = TrialEngine(
            display=self.display,
            clock=self.clock,
            bus=self.bus,
            schema=self.schema,
            commands=self.commands,
            frame_monitor=self.frame_monitor,
            input_provider=input_provider,
            health_checks=health_checks,
            on_manual_reward=on_manual_reward,
            overlay=overlay,
            reward_requests=reward_requests,
            on_frame_input=on_frame_input,
        )

    def ctx(self, trial_index: int = 1, **kwargs: Any) -> TrialContext:
        return TrialContext(
            # The harness's own clock unless a test hands the context another
            # one — which only a test of the one-clock rule does.
            clock=kwargs.pop("clock", self.clock),
            screen=SCREEN,
            rng=np.random.default_rng(0),
            trial_index=trial_index,
            params={"condition": "test"},
            **kwargs,
        )


class RunForFrames:
    """A phase that CONTINUEs for n frames, then returns its terminal value
    (an Outcome, or PhaseAction.ADVANCE)."""

    name = "run_for_frames"

    def __init__(self, n_frames: int, then: Any, emit_on_enter: str | None = None) -> None:
        self._remaining = n_frames
        self._then = then
        self._emit = emit_on_enter
        self.frames_seen: list[float] = []

    def on_enter(self, ctx: TrialContext) -> None:
        if self._emit is not None:
            ctx.emit_on_flip(self._emit)

    def on_frame(self, ctx: TrialContext) -> Any:
        self.frames_seen.append(ctx.dt)
        if self._remaining <= 0:
            return self._then
        self._remaining -= 1
        return PhaseAction.CONTINUE


class RequestRewardOnFrames(RunForFrames):
    """RunForFrames that asks for a mid-trial drop on the listed frames of
    the phase (0 = its first on_frame call) — a pursuit phase paying while
    gaze stays in its window, reduced to the part the engine sees."""

    name = "request_reward_on_frames"

    def __init__(
        self,
        n_frames: int,
        then: Any,
        on_frames: tuple[int, ...],
        pulses: RewardPulses | None = None,
        reason: str = "hold",
    ) -> None:
        super().__init__(n_frames, then)
        self._on_frames = set(on_frames)
        self._pulses = pulses or RewardPulses(n_pulses=1, pulse_ms=50, inter_pulse_ms=0)
        self._reason = reason
        self._frame = 0

    def on_frame(self, ctx: TrialContext) -> Any:
        if self._frame in self._on_frames:
            ctx.request_reward(self._pulses, self._reason)
        self._frame += 1
        return super().on_frame(ctx)


COMPLETED = Outcome("COMPLETED", completed=True, success=True)
FAILED = Outcome("FAILED", completed=False)


class SessionHarness:
    """A full SessionRunner wired to fakes: simulated time, tmp data root,
    a SimpleSequence source, and a default 2-frame COMPLETED trial."""

    def __init__(
        self,
        tmp_path,
        n_trials: int = 2,
        commands: CommandSource | None = None,
        build_trial: Callable | None = None,
        score: Callable | None = None,
        declared_events: tuple[str, ...] = ("FIX_ON",),
        tracker: EyeTracker | None = None,
        reward: RewardDispenser | None = None,
        sync: SyncOutput | None = None,
        event_lines: dict[str, str] | None = None,
        overlay: Callable | None = None,
        clock: FakeClock | None = None,
        reward_policy: Any = None,
        source: Any = None,
        declared_outcomes: Any = None,
        use_pause_menu: bool = False,
        live_monitor: Any = None,
        mid_trial_reward: bool = False,
        # The seams below are SessionRunner's own constructor parameters,
        # passed straight through. A test states what it needs here instead
        # of assigning the runner's private attributes after construction,
        # so the suite keeps testing the runner through the same door
        # build_session uses, whatever the runner does with them inside.
        on_pause: Callable | None = None,
        eyetracker: Any = None,
        training: Any = None,
        instructions: str | None = None,
        await_start: Callable[[], bool] | None = None,
        database: Any = None,
        manual_reward: Callable[[], None] | None = None,
        manual_reward_payload: dict[str, Any] | None = None,
        pause_menu_reward: bool = False,
        max_consecutive_failures: int | None = None,
        max_consecutive_dropouts: int | None = DEFAULT_MAX_CONSECUTIVE_DROPOUTS,
        rest_resume_after_s: float | None = None,
        frame_qa: FrameQAConfig | None = None,
    ) -> None:
        """``on_pause`` is the pause strategy, and wins over ``use_pause_menu``.
        ``eyetracker`` replaces the monitor the harness builds from ``tracker``
        (a stub standing in for EyeTrackerMonitor). ``pause_menu_reward``
        hands the pause menu the engine's own manual-reward hook, as
        build_session wires it; ``manual_reward`` hands it a hook of the
        test's own instead. ``frame_qa`` configures the one FrameMonitor the
        engine and the runner share (the default config otherwise)."""
        if pause_menu_reward and manual_reward is not None:
            raise ValueError("pass pause_menu_reward or manual_reward, not both")
        from alhazen.data.paths import SessionPaths
        from alhazen.devices.reward import QueuedReward
        from alhazen.paradigms.base import Condition, SimpleSequence
        from alhazen.session.eyetracker import EyeTrackerMonitor
        from alhazen.session.pause import run_pause_menu
        from alhazen.session.recorder import DataRecorder
        from alhazen.session.runner import SessionRunner
        from alhazen.task.plan import TrialPlan

        # Accepting a clock lets a test build a device (a scripted tracker)
        # against the same simulated time the session runs on.
        self.clock = clock if clock is not None else FakeClock()
        self.display = FakeDisplay(self.clock, FRAME_S)
        self.cfg = make_session_config(tmp_path)
        self.paths = SessionPaths.create(tmp_path, "t01", 1, 1, "test-task", "20260826")
        self.bus = EventBus()
        # Subscription order mirrors the builder's: tracker messages, sync
        # pulses, then the recorder.
        self.tracker = tracker
        # The device itself, whatever the session delivers through: a test
        # asserts on what reached the valve.
        self.reward = reward
        # A mid-trial-reward session wraps its dispenser the way the builder
        # does, and every delivery — requests, manual key, end-of-trial pay —
        # goes through the wrapper.
        self.queued_reward = (
            QueuedReward(reward) if mid_trial_reward and reward is not None else None
        )
        session_reward = self.queued_reward if self.queued_reward is not None else reward
        self.sync = sync
        if tracker is not None:
            self.bus.subscribe(TrackerMessageSubscriber(tracker))
        if sync is not None:
            self.bus.subscribe(make_sync_subscriber(sync, event_lines or {}))
        self.recorder = DataRecorder(self.paths.trials_path, self.paths.events_path)
        self.bus.subscribe(self.recorder.on_event)
        self.collector = EventCollector()
        self.bus.subscribe(self.collector)
        self.schema = EventSchema(declared_events)
        self.commands = commands or ScriptedCommands()
        # One monitor, handed to both the engine and the runner, as the
        # builder does: the engine marks the trials, the runner reads its
        # verdicts (the failure streak's display heading) and saves it.
        self.frame_monitor = FrameMonitor(
            frame_qa if frame_qa is not None else FrameQAConfig(), 1 / FRAME_S
        )
        # The session's eye-tracker monitor, built the way the builder builds
        # it: it owns the drift correction the input provider applies and the
        # procedures the pause menu's C/V/D keys run. Validation after a
        # calibration is off so a test that presses C gets one calibration,
        # not a calibration plus a validation walk.
        # A test's own stand-in monitor, when it passes one, replaces it.
        self.eyetracker = eyetracker
        if self.eyetracker is None and tracker is not None:
            self.eyetracker = EyeTrackerMonitor(
                tracker,
                self.display,
                SCREEN,
                self.clock,
                EyeTrackerConfig(backend="scripted", validate_after_calibration=False),
                poll_keys=self.commands.poll_raw_keys,
            )
        # The same closures build_session derives from a tracker and a
        # reward device — reused rather than re-implemented, so there stays
        # exactly one gaze coordinate conversion in the codebase, and one
        # routing of the manual reward (ahead of the queue through a
        # QueuedReward, straight to the device otherwise).
        manual_hook = make_manual_reward(session_reward, RewardPulses())
        self.engine = TrialEngine(
            display=self.display,
            clock=self.clock,
            bus=self.bus,
            schema=self.schema,
            commands=self.commands,
            frame_monitor=self.frame_monitor,
            input_provider=(
                make_input_provider(SCREEN, tracker=tracker, correction=self.eyetracker.correction)
                if tracker is not None and self.eyetracker is not None
                else None
            ),
            health_checks=((make_tracker_health_check(tracker),) if tracker is not None else ()),
            on_manual_reward=manual_hook,
            overlay=overlay,
            # Commands the engine has no opinion about (the training stage
            # keys) reach the runner, as build_session wires them. A closure
            # over self.runner, which is built below: the engine calls it
            # only while the runner is running.
            on_session_command=lambda command: self.runner.on_session_command(command),
            reward_requests=self.queued_reward,
        )
        self.source = source or SimpleSequence(
            [Condition({"condition": "a"})],
            n_repeats=n_trials,
            rng=np.random.default_rng(0),
        )

        def pause_menu(menu):
            """The builder's own pause strategy, wired the same way: it is the
            only path that exercises poll_raw_keys end to end."""
            return run_pause_menu(
                menu,
                lambda m: self.display.show_menu(m.title, m.render(), color=m.color),
                self.commands.poll_raw_keys,
                lambda s: self.clock.advance(s),
            )

        # A test's own on_pause wins; use_pause_menu asks for the builder's;
        # neither is an unattended run, which resumes every pause at once.
        if on_pause is None and use_pause_menu:
            on_pause = pause_menu

        def default_build_trial(setup):
            return TrialPlan(phases=[RunForFrames(2, COMPLETED, emit_on_enter="FIX_ON")])

        self.runner = SessionRunner(
            cfg=self.cfg,
            paths=self.paths,
            display=self.display,
            screen=SCREEN,
            clock=self.clock,
            bus=self.bus,
            engine=self.engine,
            source=self.source,
            build_trial=build_trial or default_build_trial,
            recorder=self.recorder,
            frame_monitor=self.frame_monitor,
            commands=self.commands,
            refresh_rate_hz=1 / FRAME_S,
            task_rng=np.random.default_rng(1),
            iti_s=0.0,
            score=score,
            wait=lambda s: self.clock.advance(s),
            on_pause=on_pause,
            tracker=tracker,
            reward=session_reward,
            sync=sync,
            reward_policy=reward_policy,
            eyetracker=self.eyetracker,
            live_monitor=live_monitor,
            training=training,
            instructions=instructions,
            await_start=await_start,
            database=database,
            # The pause menu's reward: the engine's own hook (one routing, as
            # build_session wires it), or the test's, or none at all.
            manual_reward=manual_hook if pause_menu_reward else manual_reward,
            manual_reward_payload=manual_reward_payload,
            max_consecutive_failures=max_consecutive_failures,
            max_consecutive_dropouts=max_consecutive_dropouts,
            rest_resume_after_s=rest_resume_after_s,
        )
