"""build_session: the one place a runnable session is wired together.

Everything an experiment supplies is explicit here — its params model, its
event schema, its trial builder, its scheduler factory — and everything
alhazen owns (display selection, refresh measurement, seed streams, paths,
recorder, devices, engine) is assembled around it. This is also the only
place a display backend or a device backend is selected by its config name.

Devices reach the engine only as narrow hooks derived here: gaze becomes an
input provider, "is it still recording" becomes a health check, the reward
dispenser becomes the manual-reward callback, and tracker messages and sync
pulses become bus subscribers. That is what keeps every layer below this one
free of hardware.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel

from alhazen.config.gamma import gamma_path, load_gamma
from alhazen.config.loader import build_session_config, load_rig
from alhazen.config.models import (
    Duration,
    RewardPulses,
    RigConfig,
    SessionInfo,
    resolve_refresh,
)
from alhazen.core.clock import MonotonicClock
from alhazen.core.commands import CommandSource, KeyboardCommands, NullCommands
from alhazen.core.engine import TrialEngine
from alhazen.core.events import EventBus, EventSchema
from alhazen.core.rng import resolve_seed, spawn_streams
from alhazen.core.trial import InputFrame, TrialContext
from alhazen.dashboard.runtime import DashboardController
from alhazen.dashboard.spec import DashboardSpec
from alhazen.data.paths import SessionPaths
from alhazen.devices.eyetracker import EyeTracker, TrackerMessageSubscriber, make_tracker
from alhazen.devices.eyetracker.messages import MessageMap
from alhazen.devices.recording import make_recording
from alhazen.devices.response import ResponseDevice, SubjectKeyboard
from alhazen.devices.reward import RewardDispenser, make_reward
from alhazen.devices.sync import SyncOutput, make_sync, make_sync_subscriber
from alhazen.display.backend import DisplayBackend
from alhazen.display.frames import FrameMonitor
from alhazen.display.psychopy_backend import PsychoPyDisplay
from alhazen.display.screen import Screen
from alhazen.display.simulated import SimulatedDisplay
from alhazen.errors import ConfigError
from alhazen.paradigms.base import TrialSource
from alhazen.session.database import ExperimentDatabase, FrameInputBuffer
from alhazen.session.pause import PauseMenu, run_pause_menu
from alhazen.session.recorder import DataRecorder
from alhazen.session.runner import SessionRunner
from alhazen.stimuli.photodiode import make_photodiode
from alhazen.task.plan import BuildTrial
from alhazen.task.task import Task
from alhazen.training.stages import Curriculum
from alhazen.training.state import TrainingState
from alhazen.training.supervisor import TrainingSupervisor

log = logging.getLogger(__name__)

MakeSource = Callable[[BaseModel, np.random.Generator], TrialSource]


def make_input_provider(
    screen: Screen,
    tracker: EyeTracker | None = None,
    response: ResponseDevice | None = None,
) -> Callable[[], InputFrame] | None:
    """The engine's per-frame input snapshot: where the subject is looking and
    what their hands did, assembled in one place.

    This closure is the ONE place gaze changes coordinate frame: trackers
    report screen px with y growing down, phases read centered px with y
    growing up. A second conversion site anywhere else is how a task ends up
    silently mirrored about the horizontal midline.

    ``None`` gaze passes straight through as ``None``: an unverifiable
    position stays unverifiable (the blink rule), never a guess. Returns None
    when the rig has no input devices at all, so the engine keeps its own
    empty-frame default.
    """
    if tracker is None and response is None:
        return None

    def provide() -> InputFrame:
        gaze = None
        if tracker is not None:
            sample = tracker.get_gaze()
            if sample is not None:
                gaze = screen.screen_to_centered(sample.gx, sample.gy)
        hands = response.poll() if response is not None else None
        return InputFrame(
            gaze=gaze,
            keys=hands.keys if hands is not None else (),
            wheel=hands.wheel if hands is not None else 0.0,
        )

    return provide


def make_gaze_input_provider(tracker: EyeTracker, screen: Screen) -> Callable[[], InputFrame]:
    """Gaze only — the shape a test that has a tracker and nothing else wants."""
    provider = make_input_provider(screen, tracker=tracker)
    assert provider is not None
    return provider


def make_tracker_health_check(tracker: EyeTracker) -> Callable[[], str | None]:
    """Abort a trial the moment the tracker stops recording.

    A trial that runs on while its tracker has dropped out produces a record
    that looks like a normal trial but has no eye data behind it — worse than
    an abort, because nothing in the data says so. The reason string lands in
    the trial record as ``abort_reason``.
    """
    return lambda: None if tracker.is_recording() else "tracker_stopped"


def validate_event_names(
    names: dict[str, str] | list[str], schema: EventSchema, where: str
) -> None:
    """Check config keys that name events against the experiment's schema.

    Done at build time, and loudly: an event name that no schema declares
    never fires, so a typo in a sync map or a photodiode's event list would
    otherwise show up as a silently missing TTL pulse or an unmarked frame —
    discovered, at best, during analysis of a session that cannot be re-run.
    """
    for name in names:
        if name not in schema.all_names:
            raise ConfigError(
                f"{where} names event {name!r}, which this experiment never declares. "
                f"Declared events: {sorted(schema.all_names)}"
            )


def build_session(
    *,
    rig: RigConfig | str | Path,
    subject: str,
    session: int,
    run: int,
    task: Task | None = None,
    task_name: str | None = None,
    task_params: BaseModel | None = None,
    event_schema: EventSchema | None = None,
    build_trial: BuildTrial | None = None,
    make_source: MakeSource | None = None,
    seed: int | None = None,
    iti: Duration | None = None,
    score: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    reward_pulses: RewardPulses | None = None,
    tracker_messages: MessageMap | None = None,
    tracker: EyeTracker | None = None,
    response: ResponseDevice | None = None,
    reward: RewardDispenser | None = None,
    sync: SyncOutput | None = None,
    curriculum: Curriculum | None = None,
    windowed: bool = False,
    sources: dict[str, str] | None = None,
    simulated_frame_period_s: float | None = None,
    date_yyyymmdd: str | None = None,
    instructions: str | None = None,
    auto_start: bool = False,
    dashboard: bool | None = None,
    open_dashboard: bool | None = None,
) -> SessionRunner:
    """Wire one runnable session.

    Pass ``task=`` (a Task instance) and everything the experiment declares —
    name, params, events, trial builder, scheduler, score, reward policy —
    comes from it. The explicit parameters still work and still win when both
    are given, which is what a test overriding one piece of a real task needs.

    ``tracker``/``reward``/``sync`` likewise override what the rig config
    would have built, so a simulated session can be driven by a scripted gaze
    trace through this same function rather than through a hand-wired copy of
    it — a copy is how an experiment's tests end up exercising different
    wiring from the sessions they are meant to rehearse.
    """
    rig_cfg = rig if isinstance(rig, RigConfig) else load_rig(rig)
    if dashboard is not None or open_dashboard is not None:
        dashboard_cfg = rig_cfg.dashboard.model_copy(
            update={
                **({"enabled": dashboard} if dashboard is not None else {}),
                **({"auto_open": open_dashboard} if open_dashboard is not None else {}),
            }
        )
        rig_cfg = rig_cfg.model_copy(update={"dashboard": dashboard_cfg})

    reward_policy = None
    dashboard_spec = DashboardSpec()
    training: TrainingSupervisor | None = None
    if task is not None:
        task_name = task_name if task_name is not None else task.name
        task_params = task_params if task_params is not None else task.params
        event_schema = event_schema if event_schema is not None else task.events
        build_trial = build_trial if build_trial is not None else task.build_trial
        make_source = make_source if make_source is not None else task.make_source
        score = score if score is not None else task.score
        reward_policy = task.reward
        dashboard_spec = task.dashboard or DashboardSpec()
    missing = [
        name
        for name, value in (
            ("task_name", task_name),
            ("task_params", task_params),
            ("event_schema", event_schema),
            ("build_trial", build_trial),
            ("make_source", make_source),
        )
        if value is None
    ]
    if missing:
        raise ConfigError(
            f"build_session needs {', '.join(missing)} — pass task=<Task instance>, or "
            f"pass them explicitly"
        )
    assert task_name is not None and task_params is not None and event_schema is not None
    assert build_trial is not None and make_source is not None

    resolved_seed = resolve_seed(seed)
    info = SessionInfo(
        subject=subject, session=session, run=run, task_name=task_name, seed=resolved_seed
    )

    if curriculum is not None:
        if task is None:
            raise ConfigError(
                "a curriculum overrides a task's own parameters, so build_session needs "
                "task=<Task instance> to apply it to"
            )
        # Built before the display opens: a stage whose overrides the task
        # rejects must fail here, not once a subject is in front of a window.
        training = TrainingSupervisor(
            curriculum=curriculum,
            state=TrainingState.load(
                rig_cfg.data_root, subject, default_stage=curriculum.stages[0].name
            ),
            task=task,
            data_root=rig_cfg.data_root,
            subject=subject,
            session_id=f"ses-{session:03d}_run-{run:02d}",
        )
        # The supervisor may have rebuilt the task's params and reward policy
        # for the current stage, so what the session runs is read back from
        # the task rather than from what was captured above.
        task_params = task.params
        reward_policy = task.reward

    # The snapshot is built from `cfg`, and `cfg` is built HERE — after the
    # curriculum block — because a curriculum rewrites the task's parameters
    # before trial 1. Built earlier, the snapshot would record the task
    # config's file values for a session that ran at a stage's values, which
    # is the one thing the snapshot exists to prevent.
    cfg = build_session_config(rig_cfg, info, task_params, sources or {})

    # Paths first: refusing to overwrite an existing run must fail before a
    # window ever opens or a device is touched.
    paths = SessionPaths.create(rig_cfg.data_root, subject, session, run, task_name, date_yyyymmdd)

    dashboard_controller = (
        DashboardController(port=rig_cfg.dashboard.port, auto_open=rig_cfg.dashboard.auto_open)
        if rig_cfg.dashboard.enabled
        else None
    )
    if dashboard_controller is not None:
        dashboard_controller.start()

    display: DisplayBackend
    commands: CommandSource
    on_pause: Callable[[PauseMenu], str] | None
    if rig_cfg.display.backend == "simulated":
        display = SimulatedDisplay(
            rig_cfg.monitor.refresh_rate_hz, frame_period_s=simulated_frame_period_s
        )
        commands = NullCommands()
        on_pause = None  # unattended: a pause resolves by resuming
    else:
        display = PsychoPyDisplay(rig_cfg.monitor, windowed=windowed)
        commands = KeyboardCommands()

        # The runner builds the menu (only it knows what is wired); the
        # builder supplies the two ends the runner has no business owning —
        # where the menu is drawn and where the keys come from.
        def on_pause(menu: PauseMenu) -> str:
            return run_pause_menu(
                menu,
                lambda m: display.show_menu(m.title, m.render(), color=m.color),
                commands.poll_raw_keys,
                time.sleep,
            )

    # Everything from here to the end of the build runs inside this guard, not
    # just `display.open()`. The dashboard is a CHILD PROCESS: a tracker that
    # fails to connect, a refresh rate that disagrees with the config, an
    # event name the rig maps but the task never declares — any of those left
    # a server running with nothing driving it, and the next session's port
    # already taken.
    try:
        display.open()

        # The other half of `alhazen calibrate gamma`. The fit is stored beside
        # the rig config, so it can only be found when the rig arrived as a path —
        # a caller that hand-built a RigConfig has no file for one to sit beside.
        # Without this the measurement was written and never used, and every
        # "50% contrast" on a calibrated rig was 50% of code value rather than of
        # luminance.
        if not isinstance(rig, RigConfig):
            stored_gamma = load_gamma(rig)
            if stored_gamma is not None:
                log.info(
                    "applying stored gamma %.3f from %s", stored_gamma["gamma"], gamma_path(rig)
                )
                display.set_gamma(stored_gamma["gamma"])

        # Frame math runs on the MEASURED rate; a measured rate that disagrees
        # with the rig config's nominal one is a loud error (config.models).
        measured = display.measure_refresh_rate(rig_cfg.display.warmup_flips)
        refresh_hz = resolve_refresh(
            rig_cfg.monitor.refresh_rate_hz, measured, rig_cfg.display.refresh_tolerance_hz
        )

        screen = Screen.from_monitor(rig_cfg.monitor)
        clock = MonotonicClock()

        # Config that names events can only be checked against the *experiment's*
        # vocabulary, which is why this happens here and not in the models.
        sync_cfg = rig_cfg.devices.sync
        if sync_cfg is not None:
            validate_event_names(sync_cfg.event_lines, event_schema, "the rig's sync.event_lines")
        photodiode_cfg = rig_cfg.display.photodiode
        if photodiode_cfg is not None:
            validate_event_names(
                photodiode_cfg.events, event_schema, "the rig's display.photodiode.events"
            )

        # Devices come from the rig config unless the caller hands one in. The
        # override exists for sessions a rig config cannot describe: a scripted
        # gaze trace replaying through a full session, which is how an experiment
        # package tests its own task end to end with no tracker attached. The rig
        # config itself still refuses test-only backends — what is allowed here
        # is passing a real object, not naming a fake one in YAML.
        devices = rig_cfg.devices
        config_tracker = (
            make_tracker(devices.eyetracker, display, screen, clock)
            if tracker is None and devices.eyetracker is not None
            else None
        )
        config_reward = (
            make_reward(devices.reward) if reward is None and devices.reward is not None else None
        )
        config_sync = make_sync(devices.sync) if sync is None and devices.sync is not None else None
        # The recorder is annotated once, before trial 1: a run directory should
        # say which external recording it belongs to even if the session then
        # crashes, and the manifest hashes that pointer along with everything
        # else the run produced.
        if rig_cfg.devices.recording is not None:
            make_recording(rig_cfg.devices.recording).annotate_session(info, paths.run_dir)
        tracker = tracker if tracker is not None else config_tracker
        reward = reward if reward is not None else config_reward
        sync = sync if sync is not None else config_sync
        # The subject's own keyboard and wheel exist only where there is a real
        # window to focus. A simulated session has nobody at the keys, and a
        # scripted test supplies its inputs directly.
        if response is None and display.kind != "simulated":
            response = SubjectKeyboard(window=display.window)
        if tracker is not None:
            # Connect at build time, alongside opening the display and measuring
            # the refresh rate: a rig fault must surface before the snapshot is
            # written and before a subject is sitting in the chair.
            tracker.connect()
            tracker.configure(screen, clock)
            # Calibration is deliberately NOT automatic. It blocks on an
            # experimenter at the Host PC, so it stays an explicit action — the
            # calibrate key, or the pause menu — wired below as on_calibrate.

        bus = EventBus()
        # Subscription order: tracker messages, then sync pulses, then the
        # recorder. Emission calls every subscriber for the same event and none
        # depends on another's side effects, so this ordering is not load-bearing
        # behaviorally — it is kept fixed so that the two hardware paths (which
        # can fail and abort the emit) run before the in-memory bookkeeping, and
        # so a reader diffing this against the design doc finds no unexplained
        # reordering.
        if tracker is not None:
            bus.subscribe(TrackerMessageSubscriber(tracker, tracker_messages))
        if response is not None and hasattr(response, "on_event"):
            bus.subscribe(response.on_event)
        if sync is not None and sync_cfg is not None:
            bus.subscribe(make_sync_subscriber(sync, sync_cfg.event_lines))
        recorder = DataRecorder(paths.trials_path, paths.events_path)
        bus.subscribe(recorder.on_event)

        frame_monitor = FrameMonitor(rig_cfg.display.frame_qa, refresh_hz)
        frame_inputs = FrameInputBuffer()

        overlay: Callable[[TrialContext], None] | None = None
        if photodiode_cfg is not None:
            patch = make_photodiode(display, screen, photodiode_cfg)
            # The patch reads only the names queued for the upcoming flip, so it
            # marks exactly the frame whose flip carries the event.
            overlay = lambda ctx: patch.draw(  # noqa: E731 - a closure over locals
                name for name, _ in ctx.pending_flip_events
            )

        manual_pulses = reward_pulses if reward_pulses is not None else RewardPulses()
        on_manual_reward = (lambda: reward.deliver(manual_pulses)) if reward is not None else None

        engine = TrialEngine(
            display=display,
            clock=clock,
            bus=bus,
            schema=event_schema,
            commands=commands,
            frame_monitor=frame_monitor,
            input_provider=make_input_provider(screen, tracker=tracker, response=response),
            health_checks=((make_tracker_health_check(tracker),) if tracker is not None else ()),
            on_manual_reward=on_manual_reward,
            manual_reward_payload={"pulses": manual_pulses.model_dump(mode="json")},
            overlay=overlay,
            # Commands the engine has no opinion about reach the runner, which
            # today means the training stage keys. A closure over `runner`, which
            # is built below: the engine only ever calls this while the runner is
            # running, by which time the name is bound.
            on_session_command=(lambda command: runner.on_session_command(command)),
            on_frame_input=frame_inputs.note,
        )

        streams = spawn_streams(resolved_seed)
        source = make_source(task_params, streams["scheduler"])

        runner = SessionRunner(
            cfg=cfg,
            paths=paths,
            display=display,
            screen=screen,
            clock=clock,
            bus=bus,
            engine=engine,
            source=source,
            build_trial=build_trial,
            recorder=recorder,
            frame_monitor=frame_monitor,
            commands=commands,
            refresh_rate_hz=refresh_hz,
            task_rng=streams["task"],
            iti_s=iti.seconds(refresh_hz) if iti is not None else 0.0,
            score=score,
            on_pause=on_pause,
            on_calibrate=tracker.calibrate if tracker is not None else None,
            tracker=tracker,
            reward=reward,
            sync=sync,
            reward_policy=reward_policy,
            training=training,
            # `database.enabled: false` turns the mirror off entirely; the
            # run files are the record either way.
            database=(
                ExperimentDatabase.for_data_root(rig_cfg.data_root, rig_cfg.database)
                if rig_cfg.database.enabled
                else None
            ),
            frame_inputs=frame_inputs,
            instructions=(
                f"{instructions}\n\nAUTOMATED DEMO — starting automatically..."
                if instructions and auto_start
                else instructions
            ),
            await_start=(
                _psychopy_auto_start
                if instructions and display.kind == "psychopy" and auto_start
                else _psychopy_await_start
                if instructions and display.kind == "psychopy"
                else None
            ),
            dashboard=dashboard_controller,
            dashboard_spec=dashboard_spec,
            manual_reward=on_manual_reward,
            manual_reward_payload={"pulses": manual_pulses.model_dump(mode="json")},
        )
    except Exception:
        if dashboard_controller is not None:
            dashboard_controller.stop()
        raise
    return runner


def _psychopy_await_start() -> bool:
    """Wait at a subject instruction screen; False means cancel cleanly."""
    from psychopy import event

    keys = event.waitKeys(keyList=["space", "escape"])
    return bool(keys and keys[0] == "space")


def _psychopy_auto_start() -> bool:
    """Leave instructions visible briefly without requiring an operator."""
    time.sleep(2.0)
    return True
