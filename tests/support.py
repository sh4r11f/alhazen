"""Shared test scaffolding, built on alhazen.testing's public fakes."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from alhazen.config.models import (
    DisplayConfig,
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
from alhazen.session.builder import make_gaze_input_provider, make_tracker_health_check
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
    ) -> None:
        self.clock = FakeClock()
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
        )

    def ctx(self, trial_index: int = 1, **kwargs: Any) -> TrialContext:
        return TrialContext(
            clock=self.clock,
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
    ) -> None:
        from alhazen.data.paths import SessionPaths
        from alhazen.paradigms.base import Condition, SimpleSequence
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
        self.reward = reward
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
        self.frame_monitor = FrameMonitor(FrameQAConfig(), 1 / FRAME_S)
        # The same closures build_session derives from a tracker — reused
        # rather than re-implemented, so there stays exactly one gaze
        # coordinate conversion in the codebase.
        self.engine = TrialEngine(
            display=self.display,
            clock=self.clock,
            bus=self.bus,
            schema=self.schema,
            commands=self.commands,
            frame_monitor=self.frame_monitor,
            input_provider=(
                make_gaze_input_provider(tracker, SCREEN) if tracker is not None else None
            ),
            health_checks=((make_tracker_health_check(tracker),) if tracker is not None else ()),
            on_manual_reward=(
                (lambda: reward.deliver(RewardPulses())) if reward is not None else None
            ),
            overlay=overlay,
        )
        self.source = source or SimpleSequence(
            [Condition({"condition": "a"})],
            n_repeats=n_trials,
            rng=np.random.default_rng(0),
        )

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
            # The builder's own pause strategy, wired the same way: it is the
            # only path that exercises poll_raw_keys end to end.
            on_pause=(
                (
                    lambda menu: run_pause_menu(
                        menu,
                        lambda m: self.display.show_menu(m.title, m.render(), color=m.color),
                        self.commands.poll_raw_keys,
                        lambda s: self.clock.advance(s),
                    )
                )
                if use_pause_menu
                else None
            ),
            tracker=tracker,
            reward=reward,
            sync=sync,
            reward_policy=reward_policy,
            on_calibrate=tracker.calibrate if tracker is not None else None,
        )
