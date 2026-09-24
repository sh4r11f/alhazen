"""build_session: the one place a runnable session is wired together.

Everything an experiment supplies is explicit here — its params model, its
event schema, its trial builder, its scheduler factory — and everything
alhazen owns (display selection, refresh measurement, seed streams, paths,
recorder, devices, engine) is assembled around it. This is also the only
place a display backend or a device backend is selected by its config name.

Devices reach the engine only as narrow hooks derived here: gaze becomes an
input provider, "is it still recording" becomes a health check, the reward
dispenser becomes the manual-reward callback (and, for a task that asks for
reward mid-trial, the engine's reward-request sink), and tracker messages and sync
pulses become bus subscribers. That is what keeps every layer below this one
free of hardware.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel

from alhazen.config.gamma import gamma_path, load_gamma
from alhazen.config.loader import build_session_config, load_rig
from alhazen.config.models import (
    DEFAULT_MAX_CONSECUTIVE_DROPOUTS,
    Duration,
    EyeTrackerConfig,
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
from alhazen.core.trial import FAULT_TRACKER_STOPPED, HealthFault, InputFrame, TrialContext
from alhazen.dashboard.runtime import DashboardController
from alhazen.dashboard.spec import DashboardSpec
from alhazen.data.paths import SessionPaths
from alhazen.devices.eyetracker import EyeTracker, TrackerMessageSubscriber, make_tracker
from alhazen.devices.eyetracker.messages import MessageMap
from alhazen.devices.eyetracker.procedures import GazeCorrection
from alhazen.devices.recording import make_recording
from alhazen.devices.response import ResponseDevice, SubjectKeyboard
from alhazen.devices.reward import QueuedReward, RewardDispenser, make_reward
from alhazen.devices.spikes import SpikeSource, make_spikes
from alhazen.devices.sync import SyncOutput, make_sync, make_sync_subscriber
from alhazen.display.backend import DisplayBackend
from alhazen.display.frames import FrameMonitor
from alhazen.display.psychopy_backend import PsychoPyDisplay
from alhazen.display.screen import Screen
from alhazen.display.simulated import SimulatedDisplay
from alhazen.errors import ConfigError
from alhazen.paradigms.base import TrialSource
from alhazen.session.database import ExperimentDatabase, FrameInputBuffer
from alhazen.session.eyetracker import EyeTrackerMonitor
from alhazen.session.feedback import FeedbackSounder
from alhazen.session.pause import PauseMenu, run_pause_menu
from alhazen.session.recorder import DataRecorder
from alhazen.session.runner import SessionRunner
from alhazen.stimuli.photodiode import make_photodiode
from alhazen.task.live import LiveAnalysis, LiveWiring
from alhazen.task.plan import BuildTrial
from alhazen.task.task import Task, task_instructions
from alhazen.training.stages import Curriculum
from alhazen.training.state import TrainingState
from alhazen.training.supervisor import TrainingSupervisor

log = logging.getLogger(__name__)

MakeSource = Callable[[BaseModel, np.random.Generator], TrialSource]


def make_input_provider(
    screen: Screen,
    tracker: EyeTracker | None = None,
    response: ResponseDevice | None = None,
    correction: GazeCorrection | None = None,
) -> Callable[[], InputFrame] | None:
    """The engine's per-frame input snapshot: where the subject is looking and
    what their hands did, assembled in one place.

    This closure is the ONE place gaze changes coordinate frame: trackers
    report screen px with y growing down, phases read centered px with y
    growing up. A second conversion site anywhere else is how a task ends up
    silently mirrored about the horizontal midline.

    ``correction`` is the session's drift correction (session/eyetracker.py),
    applied after the conversion, in the centered px it was measured in. It
    is consulted on every frame rather than copied, so a correction applied
    at a pause takes effect on the next trial's first frame.

    ``None`` gaze passes straight through as ``None``: an unverifiable
    position stays unverifiable (the blink rule), never a guess. The sample's
    time rides along as ``InputFrame.gaze_t``, unconverted. Returns None
    when the rig has no input devices at all, so the engine keeps its own
    empty-frame default.
    """
    if tracker is None and response is None:
        return None

    def provide() -> InputFrame:
        gaze = None
        gaze_t = None
        if tracker is not None:
            sample = tracker.get_gaze()
            if sample is not None:
                gaze = screen.screen_to_centered(sample.gx, sample.gy)
                if correction is not None:
                    gaze = correction.apply(gaze)
                # The sample's own time, passed through untouched: it is
                # already on the session clock (GazeSample's contract), and it
                # is the only way a phase can tell a new sample from the
                # previous one repeated. Set only beside a position, so a
                # blink carries no time either.
                gaze_t = sample.t
        hands = response.poll() if response is not None else None
        return InputFrame(
            gaze=gaze,
            keys=hands.keys if hands is not None else (),
            wheel=hands.wheel if hands is not None else 0.0,
            gaze_t=gaze_t,
        )

    return provide


def make_gaze_input_provider(tracker: EyeTracker, screen: Screen) -> Callable[[], InputFrame]:
    """Gaze only — the shape a test that has a tracker and nothing else wants."""
    provider = make_input_provider(screen, tracker=tracker)
    assert provider is not None
    return provider


def make_tracker_health_check(tracker: EyeTracker) -> Callable[[], HealthFault | None]:
    """Abort a trial the moment the tracker's recording dies.

    A trial that runs on while its tracker has dropped out produces a record
    that looks like a normal trial but has no eye data behind it — worse than
    an abort, because nothing in the data says so. The reason,
    ``FAULT_TRACKER_STOPPED``, lands in the trial record as ``abort_reason``
    and as the row's ``fault``: a tracker that stops is a system fault, not
    the subject's (core/trial.py). What the tracker said about it lands as
    ``fault_detail``. During the trial's closing phase, after the
    measurement, the engine flags it without aborting.

    Two questions, asked every frame, in this order:

    1. ``is_recording()`` — is a recording segment open at all? A flag the
       backend keeps between ``start_trial`` and ``stop_trial``; no device
       call. On its own it cannot see a recording that died mid-trial, which
       is why there is a second question.
    2. ``recording_fault()`` — is the open recording still delivering? An
       optional capability (devices/eyetracker/protocol.py) of the backends
       with a real stream: the EyeLink and the TRACKPixx3 watch their newest
       sample's age, and ask their device *why* only once it has gone stale,
       so a healthy frame costs no round trip to the device. A tracker
       without it (the stand-ins, an experiment's own fake) is judged by the
       first question alone, as before.
    """
    probe = getattr(tracker, "recording_fault", None)

    def check() -> HealthFault | None:
        if not tracker.is_recording():
            return HealthFault(
                FAULT_TRACKER_STOPPED,
                "the tracker reports no recording open (is_recording() is False)",
            )
        detail = probe() if probe is not None else None
        return None if detail is None else HealthFault(FAULT_TRACKER_STOPPED, detail)

    return check


def make_manual_reward(
    reward: RewardDispenser | None, pulses: RewardPulses
) -> Callable[[], None] | None:
    """The experimenter's manual reward: the hook behind the ``r`` key during
    a trial and R in the pause menu (keyboard or dashboard). None when the
    rig has no dispenser.

    Through a ``QueuedReward`` — a task that asks for reward mid-trial — it
    overrides the queue (``QueuedReward.deliver_manual``): every drop still
    waiting is cancelled, each with its own REWARD_CANCELLED, and the manual
    reward is delivered once, as soon as the train already on the valve
    finishes. The key blocks the frame it was pressed on until the pump is
    done, so that wait is at most that train plus its own. The end-of-trial
    pay does not come through here — the runner calls ``deliver`` — so it is
    never cancelled and takes its turn as before.

    Any other dispenser is the device itself, called on the session thread
    exactly as before.

    One closure serves the engine and the runner's pause menu, and the test
    harness reuses it, so there is one routing to get right.
    """
    if reward is None:
        return None
    if isinstance(reward, QueuedReward):
        return lambda: reward.deliver_manual(pulses)
    return lambda: reward.deliver(pulses)


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


def _release_on_abort(what: str, release: Callable[..., object], *args: object) -> None:
    """One release in a failed build's cleanup: attempted, and logged rather
    than raised when it fails.

    The build's own error is already propagating when this runs, and it is
    the one that says what went wrong. A release that raised would replace
    it: ExitStack chains the two, but the one a caller catches and the CLI
    reports would be the release's. An unguarded ``dashboard.stop()`` did
    exactly that. ExitStack runs every later release either way; catching
    here is what keeps the build's error on top. Logged with its traceback,
    so the failed release is not lost either.
    """
    try:
        release(*args)
    except Exception:
        log.exception("could not %s while aborting the build", what)


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
    max_consecutive_failures: int | None = None,
    rest_resume_after_s: float | None = None,
    score: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    reward_pulses: RewardPulses | None = None,
    tracker_messages: MessageMap | None = None,
    tracker: EyeTracker | None = None,
    response: ResponseDevice | None = None,
    reward: RewardDispenser | None = None,
    sync: SyncOutput | None = None,
    spikes: SpikeSource | None = None,
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
    name, params, events, trial builder, scheduler, score, reward policy, the
    subject's instructions — comes from it. The explicit parameters still work
    and still win when both are given, which is what a test overriding one
    piece of a real task needs. For ``instructions`` that includes an empty
    string: ``instructions=""`` shows no instruction screen whatever the task
    declares.

    ``tracker``/``reward``/``sync`` likewise override what the rig config
    would have built, so a simulated session can be driven by a scripted gaze
    trace through this same function rather than through a hand-wired copy of
    it — a copy is how an experiment's tests end up exercising different
    wiring from the sessions they are meant to rehearse.

    A device handed in is the session's to release from then on, like one
    the rig config built: the runner's teardown releases it when the session
    ends, and a build that fails releases it before raising.
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
    mid_trial_reward = task.mid_trial_reward if task is not None else False
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

    # What the subject reads before trial one: the caller's text when it
    # passed one (run.py's `instructions=`, an example's instructions.md),
    # otherwise whatever the task declares (Task.instructions). Asked here,
    # after the curriculum block, because a stage may have rebuilt the params
    # the text is allowed to depend on; and before the run directory is
    # created, so a task whose instructions file is missing fails without
    # leaving an empty run behind.
    if instructions is None and task is not None:
        instructions = task_instructions(task)

    # Refused here, before a run directory exists or a window opens, rather
    # than at the first drop: a task that pays during the trial on a rig with
    # nothing to pay with would run a subject through trials it believes are
    # rewarded. Simulate and test modes substitute a simulated dispenser
    # (modes/session.py), so a rehearsal on a laptop still builds.
    if mid_trial_reward and reward is None and rig_cfg.devices.reward is None:
        raise ConfigError(
            f"task {task_name!r} declares mid_trial_reward = True, but the rig has no "
            f"reward dispenser (devices.reward). Add one to the rig config, or rehearse with "
            f"--mode simulate or --mode test, which stand in a simulated one."
        )

    # Paths first: refusing to overwrite an existing run must fail before a
    # window ever opens or a device is touched.
    paths = SessionPaths.create(rig_cfg.data_root, subject, session, run, task_name, date_yyyymmdd)

    # Everything from here to the end of the build runs inside this guard, and
    # each thing the build acquires registers its release on `on_failure` as
    # soon as it is held: the dashboard's CHILD PROCESS, the window, the
    # tracker's link, the sync lines' NI-DAQ tasks, the reward dispenser and
    # any worker thread wrapped around it, the spike source's connection. A
    # tracker that will not connect, a refresh rate that disagrees with the
    # config, an event name the rig maps but the task never declares, a
    # scheduler that raises: each of those used to leave some of these behind,
    # and the next session found the port taken, the NI lines reserved or the
    # window still up.
    #
    # On a failure the releases run in reverse order, each one attempted even
    # when another fails, and the build's own error is the one that propagates
    # (_release_on_abort). A Ctrl-C mid-build releases them too. A build that
    # succeeds drops them unrun (`pop_all` at the end): from then on the
    # runner's teardown owns every one.
    with ExitStack() as on_failure:
        dashboard_controller = (
            DashboardController(port=rig_cfg.dashboard.port, auto_open=rig_cfg.dashboard.auto_open)
            if rig_cfg.dashboard.enabled
            else None
        )
        if dashboard_controller is not None:
            # Registered before start(), which spawns the child: stop() does
            # nothing to a controller whose child never started, and a start()
            # that spawned it and then failed is covered without relying on
            # start() to clean up after itself.
            on_failure.callback(
                _release_on_abort, "stop the dashboard server", dashboard_controller.stop
            )
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

        # Registered before open(): closing a display that never opened does
        # nothing, and PsychoPy's open() creates the window before it checks
        # the framebuffer against the rig config, so a refusal there leaves a
        # window up that only this release closes.
        on_failure.callback(_release_on_abort, "close the display", display.close)
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
        spikes_cfg = rig_cfg.devices.spikes
        if spikes_cfg is not None and spikes_cfg.backend == "simulated":
            # The simulated spike source fires to a stimulus event; a name
            # this experiment never declares would mean a simulation that
            # silently never spikes above baseline.
            validate_event_names(
                [spikes_cfg.sim_respond_to], event_schema, "the rig's spikes.sim_respond_to"
            )

        # Devices come from the rig config unless the caller hands one in. The
        # override exists for sessions a rig config cannot describe: a scripted
        # gaze trace replaying through a full session, which is how an experiment
        # package tests its own task end to end with no tracker attached. The rig
        # config itself still refuses test-only backends — what is allowed here
        # is passing a real object, not naming a fake one in YAML.
        #
        # Each device's release is registered as soon as the build holds it,
        # whether the rig config built it or the caller handed it in: the
        # runner's teardown releases either, so a failed build does too. Each
        # registration follows its own construction at once, not the last
        # device's, because NidaqSync reserves its NI lines in its constructor
        # and the step after it can fail. The tracker's waits for connect()
        # (below).
        devices = rig_cfg.devices
        if tracker is None and devices.eyetracker is not None:
            tracker = make_tracker(devices.eyetracker, display, screen, clock)
        if reward is None and devices.reward is not None:
            reward = make_reward(devices.reward)
        if reward is not None:

            def close_reward() -> None:
                # Whatever `reward` names when this runs, on purpose: a task
                # that asks for reward mid-trial rebinds it below to the
                # QueuedReward wrapped around this device, whose close() stops
                # the worker and then closes the device. Registering the
                # device and the wrapper apart would close the device twice.
                # (Never None by then; the check is for the type checker.)
                if reward is not None:
                    reward.close()

            on_failure.callback(_release_on_abort, "close the reward dispenser", close_reward)
        if sync is None and devices.sync is not None:
            sync = make_sync(devices.sync)
        if sync is not None:
            on_failure.callback(_release_on_abort, "close the sync output", sync.close)
        # The recorder is annotated once, before trial 1: a run directory should
        # say which external recording it belongs to even if the session then
        # crashes, and the manifest hashes that pointer along with everything
        # else the run produced.
        if rig_cfg.devices.recording is not None:
            make_recording(rig_cfg.devices.recording).annotate_session(info, paths.run_dir)
        if spikes is None and devices.spikes is not None:
            spikes = make_spikes(devices.spikes)
        if spikes is not None:
            # The source holds a connection and a background thread once
            # connect()/start() below have run. Registered before them because
            # the build's failure path has always closed it whether or not it
            # had connected, so every SpikeSource's close() already has to be
            # harmless on one that never did. (The tracker's has no such
            # history; its release waits for connect().)
            on_failure.callback(_release_on_abort, "close the spike source", spikes.close)
        # The subject's own keyboard and wheel exist only where there is a real
        # window to focus. A simulated session has nobody at the keys, and a
        # scripted test supplies its inputs directly.
        if response is None and display.kind != "simulated":
            response = SubjectKeyboard(window=display.window)
        eyetracker: EyeTrackerMonitor | None = None
        if tracker is not None:
            # Connect at build time, alongside opening the display and measuring
            # the refresh rate: a rig fault must surface before the snapshot is
            # written and before a subject is sitting in the chair.
            tracker.connect()
            # Registered once connect() has returned, not before: the protocol
            # does not promise shutdown() is safe on a tracker that never
            # connected, and a tracker handed in may be any implementation. A
            # connect() that fails is the backend's own to clean up.
            # configure() is covered: the TRACKPixx3 starts its recording and
            # reader thread there, and shutdown() stops both. None: a session
            # that never began has no recording to retrieve (the protocol's
            # "no recording wanted" call, as check-rig makes).
            on_failure.callback(
                _release_on_abort, "shut down the eye tracker", tracker.shutdown, None
            )
            tracker.configure(screen, clock)
            # Calibration is deliberately NOT automatic. It blocks on an
            # experimenter at the rig, so it stays an explicit action — the
            # calibrate key, the pause menu, or the dashboard — run through
            # the session's monitor, which also validates, drift-corrects and
            # shows the results. A caller-supplied tracker with no config of
            # its own gets the procedures' defaults. The procedures read their
            # keys from the same source as the pause menu.
            eyetracker = EyeTrackerMonitor(
                tracker,
                display,
                screen,
                clock,
                (
                    devices.eyetracker
                    if devices.eyetracker is not None
                    else EyeTrackerConfig(backend="scripted")
                ),
                poll_keys=commands.poll_raw_keys,
            )
        if spikes is not None:
            # Same rule as the tracker: a SpikeGLX host that is unreachable,
            # or reachable but not acquiring, must refuse the session now.
            # The clock arrives through configure() so the source stamps its
            # spikes on the ONE session clock, like every other device.
            spikes.connect()
            spikes.configure(clock)
            spikes.start()

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
        # The beep that goes with trial feedback. A subscriber like the sync
        # lines, because the phase that shows feedback touches no hardware;
        # it emits FEEDBACK and this is what hears it.
        if rig_cfg.display.feedback_beeps:
            bus.subscribe(FeedbackSounder(display))
        # After the recorder, deliberately: these two only take notes (the
        # simulated spike source reacting to a stimulus event, a live
        # analysis logging a flash), and the hardware paths and the record
        # must already have seen the event they annotate.
        if spikes is not None and hasattr(spikes, "on_event"):
            bus.subscribe(spikes.on_event)
        live: LiveAnalysis | None = (
            task.live_analysis(LiveWiring(spikes=spikes, screen=screen, clock=clock))
            if task is not None
            else None
        )
        if live is not None and hasattr(live, "on_event"):
            bus.subscribe(live.on_event)

        # Frame QA judges a panel, and a simulated display has no panel. Its
        # flip times measure how accurately the host can wait between them,
        # which on a loaded machine — a CI box, a laptop compiling something
        # else — is not the rate the rig file asks for. Judging that and then
        # marking, recycling or aborting trials for the answer stops a dry run
        # for a reason that has nothing to do with the experiment.
        #
        # Exactly the argument `SimulatedDisplay.measure_refresh_rate` makes
        # for reporting its paced rate rather than a stopwatch's
        # (display/simulated.py). The intervals are still recorded, and still
        # reach frames.csv and the dashboard's timing panel: the policy that
        # records and does not act is `log`.
        frame_qa = rig_cfg.display.frame_qa
        if display.kind == "simulated" and frame_qa.policy != "log":
            log.info(
                "frame QA policy %r is not applied on a simulated display (there is no panel "
                "to judge): the intervals are recorded, no trial is marked or recycled",
                frame_qa.policy,
            )
            frame_qa = frame_qa.model_copy(update={"policy": "log"})
        frame_monitor = FrameMonitor(frame_qa, refresh_hz)
        frame_inputs = FrameInputBuffer()

        overlay: Callable[[TrialContext], None] | None = None
        if photodiode_cfg is not None:
            patch = make_photodiode(display, screen, photodiode_cfg)
            # The patch reads only the names queued for the upcoming flip, so it
            # marks exactly the frame whose flip carries the event.
            overlay = lambda ctx: patch.draw(  # noqa: E731 - a closure over locals
                name for name, _ in ctx.pending_flip_events
            )

        # A task that asks for reward mid-trial gets its dispenser wrapped in
        # a worker thread, and from here on EVERY delivery goes through that
        # wrapper — the task's requests, the manual key, the end-of-trial pay
        # — so no two ever overlap on the valve. Any other task keeps the
        # device itself: all its deliveries already run on the session thread.
        #
        # The worker needs no release of its own: rebinding `reward` makes the
        # dispenser's release above (close_reward) close this wrapper instead.
        queued_reward: QueuedReward | None = None
        if mid_trial_reward:
            assert reward is not None  # refused above when the rig has none
            queued_reward = QueuedReward(reward)
            reward = queued_reward

        manual_pulses = reward_pulses if reward_pulses is not None else RewardPulses()
        # One hook for the engine's `r` key and the runner's pause menu.
        # Through the wrapper it cancels the queued drops and goes next,
        # while the runner's end-of-trial pay calls deliver(), is never
        # cancelled, and takes its turn (make_manual_reward).
        on_manual_reward = make_manual_reward(reward, manual_pulses)

        engine = TrialEngine(
            display=display,
            clock=clock,
            bus=bus,
            schema=event_schema,
            commands=commands,
            frame_monitor=frame_monitor,
            input_provider=make_input_provider(
                screen,
                tracker=tracker,
                response=response,
                correction=eyetracker.correction if eyetracker is not None else None,
            ),
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
            reward_requests=queued_reward,
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
            # Read off the task's params by name, the way `iti` is by the
            # modes: a limit on failed trials in a row is the experiment's
            # number, and belongs in its task config next to the trial
            # counts, not in a rig file or in code.
            # How long a rest between blocks waits before it resumes by itself;
            # None waits for a person. Set by simulate mode (modes/session.py).
            rest_resume_after_s=rest_resume_after_s,
            max_consecutive_failures=(
                max_consecutive_failures
                if max_consecutive_failures is not None
                else getattr(task_params, "max_consecutive_failures", None)
            ),
            # The rig's number, unlike the one above: how often a tracker may
            # drop out in a row is about the tracker, not the task. A caller-
            # supplied tracker on a rig with no tracker config gets the default.
            max_consecutive_dropouts=(
                devices.eyetracker.max_consecutive_dropouts
                if devices.eyetracker is not None
                else DEFAULT_MAX_CONSECUTIVE_DROPOUTS
            ),
            score=score,
            on_pause=on_pause,
            eyetracker=eyetracker,
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
            await_start=_start_gate(instructions, display.kind, auto_start),
            dashboard=dashboard_controller,
            dashboard_spec=dashboard_spec,
            manual_reward=on_manual_reward,
            manual_reward_payload={"pulses": manual_pulses.model_dump(mode="json")},
            spikes=spikes,
            live=live,
        )
        # Built. Every release registered above now belongs to the runner's
        # teardown, so they are dropped here without running.
        on_failure.pop_all()
    return runner


def _start_gate(
    instructions: str | None, display_kind: str, auto_start: bool
) -> Callable[[], bool] | None:
    """What stands between the instruction screen and trial one.

    - Nothing, when there is no text to read, or when the display has no
      keyboard behind it (a simulated one): the runner shows the text, which
      the simulated display logs, and starts at once.
    - The two-second auto-start, on a real display in an unattended session
      (``auto_start``: simulate mode, an example's ``--auto``). It never waits
      for a key, because nobody is there to press one — which is why an
      instruction screen can be shown in simulate mode at all.
    - SPACE (start) or ESC (cancel), on a real display with somebody in the
      chair.

    A function of its own so the rule can be pinned without a renderer: the
    two gates it chooses between need PsychoPy, the choice does not.
    """
    if not instructions or display_kind != "psychopy":
        return None
    return _psychopy_auto_start if auto_start else _psychopy_await_start


def _psychopy_await_start() -> bool:
    """Wait at a subject instruction screen; False means cancel cleanly."""
    from psychopy import event

    keys = event.waitKeys(keyList=["space", "escape"])
    return bool(keys and keys[0] == "space")


def _psychopy_auto_start() -> bool:
    """Leave instructions visible briefly without requiring an operator."""
    time.sleep(2.0)
    return True
