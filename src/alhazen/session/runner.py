"""SessionRunner: the outer loop an experimenter actually starts.

Contract, in order:

1. The config snapshot is written *before anything else* — a session that
   crashes still documents what it was trying to run.
2. File logging attaches at the root logger so every module's logging lands
   in this run's ``session.log``.
3. Loop: ask the paradigm for a condition, build the trial through the
   task's ``build_trial``, open the tracker's recording segment (and close it
   in a ``finally``), run it through the engine, tell the scheduler how it
   went (for **every** outcome — the scheduler alone decides re-queueing),
   record the measurement (for every outcome except PAUSED, which produced
   none), wait out the ITI.
4. However the loop ends, teardown attempts *every* step — a session is
   unrepeatable work, so writing the trials table must survive a display
   that fails to close, and vice versa. Step errors are logged and collected;
   the first is re-raised only if no other exception is already propagating.
"""

from __future__ import annotations

import logging
import sys
import time
from collections import Counter
from collections.abc import Callable
from typing import Any

import numpy as np

from alhazen.config.models import SessionConfig
from alhazen.config.snapshot import write_snapshot
from alhazen.core.clock import Clock
from alhazen.core.commands import Command, CommandSource
from alhazen.core.engine import QuitRequested, TrialEngine
from alhazen.core.events import Event, EventBus
from alhazen.core.trial import CircleRegion, TrialContext
from alhazen.dashboard.runtime import DashboardController, dashboard_state
from alhazen.dashboard.spec import DashboardSpec
from alhazen.data.manifest import write_manifest
from alhazen.data.participants import ensure_participant
from alhazen.data.paths import SessionPaths
from alhazen.devices.eyetracker import EyeTracker, HostShape
from alhazen.devices.reward import RewardDispenser
from alhazen.devices.spikes import SpikeSource
from alhazen.devices.sync import SyncOutput
from alhazen.display.backend import DisplayBackend
from alhazen.display.frames import FrameMonitor
from alhazen.display.screen import Screen
from alhazen.paradigms.base import Condition, TrialSource
from alhazen.session.database import ExperimentDatabase, FrameInputBuffer
from alhazen.session.eyetracker import PROCEDURE_STATUS, EyeTrackerMonitor
from alhazen.session.pause import (
    PauseMenu,
    build_pause_menu,
    pause_menu,  # noqa: F401 - re-exported: it lived here until 1.1
)
from alhazen.session.recorder import DataRecorder
from alhazen.task.live import LiveAnalysis

# Re-exported through this module as well as its own: experiment code and
# tests written before the task layer existed import them from here.
from alhazen.task.plan import BuildTrial, TrialSetup
from alhazen.task.reward_policy import RewardPolicy
from alhazen.training.supervisor import TrainingSupervisor

log = logging.getLogger(__name__)


def host_overlay_shapes(screen: Screen, regions: dict[str, CircleRegion]) -> list[HostShape]:
    """The trial's fixation cross and region boxes, in screen px, for the
    tracker's operator display.

    Lives here rather than in the engine because it is a *session* courtesy
    to whoever is watching the rig, not part of running a trial. The min/max
    normalization matters: centered y grows up and screen y grows down, so
    "the top corner" swaps sides in the conversion, and a box handed over
    with x1 > x2 simply does not draw.
    """

    def box(center: tuple[float, float], radius: float) -> HostShape:
        cx, cy = center
        ax, ay = screen.centered_to_screen(cx - radius, cy + radius)
        bx, by = screen.centered_to_screen(cx + radius, cy - radius)
        return HostShape(
            kind="box",
            x1=round(min(ax, bx)),
            y1=round(min(ay, by)),
            x2=round(max(ax, bx)),
            y2=round(max(ay, by)),
        )

    fx, fy = screen.centered_to_screen(0.0, 0.0)
    shapes = [HostShape(kind="cross", x1=round(fx), y1=round(fy))]
    shapes.extend(box(region.center, region.radius) for region in regions.values())
    return shapes


# Menu action -> the session command it issues. Shared by the keyboard and
# dashboard pause paths so a stage moved from the browser and one moved from
# the keyboard go through exactly the same code.
def _menu_action(actions: dict[str, str], key: str) -> str | None:
    """The action a raw key name selects on the menu, or None.

    The menu prints one row for "Q or ESC", so its key text is not a key name;
    the two real names are mapped here. Everything else matches a row's key
    case-insensitively, which is what lets a rebound key work without the
    pause screen and the keyboard drifting apart.
    """
    if key.lower() in ("q", "escape"):
        return actions.get("Q or ESC")
    if key.lower() == "space":
        return actions.get("SPACE")
    for row_key, action in actions.items():
        if row_key.lower() == key.lower():
            return action
    return None


PAUSE_STAGE_COMMANDS = {
    "promote_stage": Command.PROMOTE_STAGE,
    "demote_stage": Command.DEMOTE_STAGE,
    "hold_stage": Command.HOLD_STAGE,
}

# The pause-menu actions that are eye-tracker procedures, run through the
# session's EyeTrackerMonitor. Same names as the menu rows (session/pause.py)
# and the dashboard's buttons (dashboard/runtime.py _ALLOWED_COMMANDS).
PROCEDURE_ACTIONS = ("calibrate", "validate", "drift_correct")

# While a session is paused, how often the dashboard is republished so a
# tracker with a camera shows a live image. A pause is when the experimenter
# is looking at the subject's eye; the image is the point of the tab.
CAMERA_REFRESH_S = 1.0

# The statuses during which a camera frame is read for the dashboard: when
# the device is not busy with a trial and somebody is looking at the image.
CAMERA_STATUSES = frozenset({"paused", PROCEDURE_STATUS})


class SessionRunner:
    def __init__(
        self,
        cfg: SessionConfig,
        paths: SessionPaths,
        display: DisplayBackend,
        screen: Screen,
        clock: Clock,
        bus: EventBus,
        engine: TrialEngine,
        source: TrialSource,
        build_trial: BuildTrial,
        recorder: DataRecorder,
        frame_monitor: FrameMonitor,
        commands: CommandSource,
        refresh_rate_hz: float,
        task_rng: np.random.Generator,
        iti_s: float = 0.0,
        score: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        on_pause: Callable[[PauseMenu], str] | None = None,
        eyetracker: EyeTrackerMonitor | None = None,
        wait: Callable[[float], None] | None = None,
        tracker: EyeTracker | None = None,
        reward: RewardDispenser | None = None,
        sync: SyncOutput | None = None,
        reward_policy: RewardPolicy | None = None,
        training: TrainingSupervisor | None = None,
        instructions: str | None = None,
        await_start: Callable[[], bool] | None = None,
        database: ExperimentDatabase | None = None,
        frame_inputs: FrameInputBuffer | None = None,
        dashboard: DashboardController | None = None,
        dashboard_spec: DashboardSpec | None = None,
        manual_reward: Callable[[], None] | None = None,
        manual_reward_payload: dict[str, Any] | None = None,
        spikes: SpikeSource | None = None,
        live: LiveAnalysis | None = None,
    ) -> None:
        self._cfg = cfg
        self._paths = paths
        self._display = display
        self._screen = screen
        self._clock = clock
        self._bus = bus
        self._engine = engine
        self._source = source
        self._build_trial = build_trial
        self._recorder = recorder
        self._frame_monitor = frame_monitor
        self._commands = commands
        self._refresh_rate_hz = refresh_rate_hz
        self._task_rng = task_rng
        self._iti_s = iti_s
        # ``score`` is the experiment's derived-measure hook: task-specific
        # metrics (a saccade bias, a shift estimate) are computed here by
        # experiment code, never inside the engine.
        self._score = score
        self._on_pause = on_pause
        # The tracker's procedures and their results (session/eyetracker.py).
        # It reports through the runner: its progress lines go out as
        # dashboard publishes, and its results as session events, both of
        # which are the runner's to send.
        self._eyetracker = eyetracker
        if eyetracker is not None:
            eyetracker.publisher = self._publish_dashboard
            eyetracker.emit = self._emit_session_event
        self._wait = wait if wait is not None else time.sleep
        # Devices are owned here, not by the engine: the engine sees only the
        # narrow hooks the builder derived from them (gaze inputs, health
        # checks, the manual-reward callback). What is left for the runner is
        # their *lifecycle* — per-trial recording segments and teardown.
        self._tracker = tracker
        self._reward = reward
        self._sync = sync
        # What each outcome earns. None (or no reward device) means the only
        # live reward path is the experimenter's manual key.
        self._reward_policy = reward_policy
        # The curriculum, if this session runs under one. It owns the task's
        # current parameters; the runner only asks it what to stamp on a
        # record, tells it how each trial went, and applies its transitions
        # between trials.
        self._training = training
        self._instructions = instructions
        self._await_start = await_start
        self._database = database
        self._frame_inputs = frame_inputs or FrameInputBuffer()
        self._dashboard = dashboard
        self._dashboard_spec = dashboard_spec or DashboardSpec()
        # The live spike stream and the analysis consuming it. The runner
        # owns their *lifecycle* only — the analysis is driven between
        # trials, and both are released in teardown — exactly as it owns the
        # tracker's; what they compute is the experiment's business.
        self._spikes = spikes
        self._live = live
        # Insertion-ordered, so the first factor a task names is the one the
        # spatial panels take their colours from.
        self._condition_fields: list[str] = []
        self._manual_reward = manual_reward
        self._manual_reward_payload = dict(manual_reward_payload or {})
        self._dashboard_revision = 0
        self._dashboard_message: str | None = None
        # A session cancelled at the instructions screen flows through the
        # same teardown a finished one does, so without this the mirror
        # recorded status="complete" for a run with zero trials —
        # indistinguishable from one that ran and produced nothing.
        self._cancelled = False

        self._trial_index = 0
        self._attempt_counts: Counter = Counter()

    # ------------------------------------------------------------------

    def run(self) -> None:
        write_snapshot(self._cfg, self._paths.snapshot_path)
        ensure_participant(self._cfg.rig.data_root, self._cfg.info.subject)
        file_handler = self._attach_file_logging()

        log.info(
            "session start: subject %s, ses %d, run %d, task %s, seed %d",
            self._cfg.info.subject,
            self._cfg.info.session,
            self._cfg.info.run,
            self._cfg.info.task_name,
            self._cfg.info.seed,
        )
        if self._dashboard is not None:
            log.info("live dashboard: %s", self._dashboard.url)
        self._publish_dashboard("running")
        try:
            if self._instructions:
                self._display.show_message(self._instructions)
                # A simulated session has no subject and therefore no start
                # callback.  It records the message and proceeds immediately.
                if self._await_start is not None and not self._await_start():
                    log.info("session cancelled from instructions screen")
                    self._cancelled = True
                    return
            while True:
                condition = self._source.next()
                if condition is None:
                    break  # the scheduler's definition of "session done"

                # Attempts are keyed by condition identity so a re-served
                # condition increments the same counter, never restarts it.
                self._attempt_counts[condition.key()] += 1
                attempt = self._attempt_counts[condition.key()]
                # The factors this experiment actually varies, learned from
                # the conditions served rather than declared in advance. The
                # dashboard colours and groups its plots by them, so a task
                # gets condition-aware monitoring without saying anything.
                for name in condition.params:
                    if name not in self._condition_fields:
                        self._condition_fields.append(name)
                self._trial_index += 1

                ctx, phases = self._assemble_trial(condition, attempt)
                try:
                    # Inside the try, not before it: opening the recording
                    # segment can fail partway through (a tracker that starts
                    # recording and then reports no usable eye), and the
                    # finally below is what stops it again.
                    self._start_tracker_trial(ctx, attempt)
                    result = self._engine.run_trial(ctx, phases)
                except QuitRequested:
                    log.info("session terminated by experimenter on trial %d", self._trial_index)
                    break
                finally:
                    # Guaranteed, however the trial ended (outcome, quit, a
                    # bug, a hardware fault): a tracker left believing it is
                    # still recording writes the next trial's samples into
                    # this trial's segment.
                    if self._tracker is not None:
                        self._tracker.stop_trial()

                outcome = result.outcome

                # Two different questions, two different gates. The SCHEDULER
                # holds the plan and must hear about every outcome — the
                # condition was already popped by next(), and record() alone
                # decides whether it goes back (a paused trial's condition
                # must not silently vanish from the plan). The RECORDER holds
                # measurements — a paused trial produced none, so it writes
                # no row (its events are already in the events table).
                self._source.record(condition, result)

                # Reward before recording, so record["rewarded"] states what
                # actually happened at the pump rather than what was owed.
                reward_failed = self._deliver_reward(ctx, outcome)

                record = result.record
                if outcome.name != "PAUSED":
                    if self._score is not None:
                        record = self._score(record)
                    self._recorder.add_trial(record)
                    # The live analysis runs between trials, after the row is
                    # written and before the dashboard publish — so the
                    # panels it contributes to that publish already include
                    # this trial. It sees the SCORED record, like training.
                    if self._live is not None:
                        self._live.on_trial(record)
                    self._publish_dashboard("running")

                # Training hears about the trial after the row is written,
                # and moves the subject only here — between trials, never
                # inside one. It sees the SCORED record, which is the one that
                # was written: a task's derived measures (an rt_ms computed in
                # `score`) are exactly what a criterion would want to gate on,
                # and they exist nowhere else.
                if self._training is not None:
                    self._training.observe(outcome, record)
                    if not self._apply_stage_transition():
                        break

                if outcome.name == "PAUSED" or reward_failed:
                    # A reward failure goes through the same pause flow as a
                    # deliberate pause: a human has to look at the pump before
                    # the session carries on rewarding nothing. The
                    # measurement is already recorded above — a hardware fault
                    # after the fact must never discard a trial the subject
                    # actually completed.
                    if not self._handle_pause(result.record, reward_failed=reward_failed):
                        break
                    continue  # the pause menu already gave all the time needed; skip ITI

                if self._iti_s > 0:
                    self._wait(self._iti_s)
        finally:
            self._teardown(file_handler)

    # ------------------------------------------------------------------

    def _assemble_trial(self, condition: Condition, attempt: int) -> tuple[TrialContext, list]:
        setup = TrialSetup(
            cfg=self._cfg,
            screen=self._screen,
            display=self._display,
            rng=self._task_rng,
            refresh_rate_hz=self._refresh_rate_hz,
            trial_index=self._trial_index,
            attempt=attempt,
            condition=condition,
        )
        plan = self._build_trial(setup)
        record = {
            "trial_index": self._trial_index,
            "attempt": attempt,
            # Stage and ramp values first, so a task that records a column of
            # the same name wins — the task's own measurement is never
            # shadowed by bookkeeping.
            **(self._training.stamp() if self._training is not None else {}),
            **condition.params,
            **plan.record,
        }
        ctx = TrialContext(
            clock=self._clock,
            screen=self._screen,
            rng=self._task_rng,
            trial_index=self._trial_index,
            params=dict(condition.params),
            stimuli=plan.stimuli,
            regions=plan.regions,
            record=record,
        )
        return ctx, plan.phases

    def _apply_stage_transition(self) -> bool:
        """Move the subject if the curriculum says so. Returns False when the
        session should stop (a finished curriculum that asked to stop)."""
        assert self._training is not None
        change = self._training.transition()
        if change is not None:
            # Emitted like any other event, so a stage change lands in
            # events.csv and — where a rig maps it — on a sync line.
            self._emit_session_event(
                "STAGE_CHANGED",
                {"from": change.from_stage, "to": change.to_stage, "reason": change.reason},
            )
            self._display.show_message(f"stage: {change.to_stage}")
        # Every transition rebinds the task's reward policy to a copy scaled
        # for the new stage. The runner pays from its own reference, so it has
        # to re-read it here — otherwise the pump keeps delivering the
        # previous stage's amount while every row stamps the new scale, and
        # the data claims a reward that was never given.
        self._reward_policy = self._training.reward_policy
        if self._training.complete and self._training.stop_when_complete:
            log.info("curriculum complete; ending the session")
            return False
        return True

    def on_session_command(self, command: Any) -> None:
        """Commands the engine handed on. Today: the training stage keys.

        Queued rather than applied: this arrives mid-trial, and the
        transition happens between trials like every other one.
        """
        if self._training is None:
            log.info("ignoring %s: this session has no curriculum", command)
            return
        if command is Command.PROMOTE_STAGE:
            self._training.request(+1)
        elif command is Command.DEMOTE_STAGE:
            self._training.request(-1)
        elif command is Command.HOLD_STAGE:
            held = self._training.toggle_hold()
            self._display.show_message(
                "stage transitions held" if held else "stage transitions resumed"
            )

    def _deliver_reward(self, ctx: TrialContext, outcome: Any) -> bool:
        """Pay out what this outcome earned. Returns True if the hardware
        failed, which the caller turns into a pause.

        The one deliberate catch in this file. Everywhere else a device fault
        aborts loudly, but here the trial's measurement already exists and is
        about to be written: letting a pump failure propagate would throw away
        a completed trial's data to report a problem with the juice line. So
        it is recorded, marked in the event stream, shown on screen, and
        handed to a human — loudly, but without losing the trial.
        """
        if self._reward_policy is None or self._reward is None or outcome.name == "PAUSED":
            return False
        pulses = self._reward_policy.pulses_for(outcome.name)
        if pulses is None:
            # A completed trial that earned nothing is a fact the subject
            # experienced. Marked with its own event rather than left as the
            # absence of REWARD, which is indistinguishable from a REWARD that
            # failed to be written.
            if outcome.completed:
                self._emit(ctx, "NO_REWARD", {"outcome": outcome.name})
            return False
        try:
            self._reward.deliver(pulses)
        except Exception:
            log.exception("reward delivery failed on trial %d", self._trial_index)
            ctx.record["rewarded"] = False
            self._emit(ctx, "REWARD_FAILED", {"outcome": outcome.name})
            self._display.show_message("REWARD FAILURE — check the pump")
            return True
        ctx.record["rewarded"] = True
        self._emit(
            ctx,
            "REWARD",
            {"manual": False, "outcome": outcome.name, "pulses": pulses.model_dump(mode="json")},
        )
        return False

    def _emit_session_event(self, name: str, payload: dict[str, Any]) -> None:
        """An event between trials, with no trial record to mirror it into."""
        self._bus.emit(
            Event(
                name=name,
                t=self._clock.now(),
                trial_index=self._trial_index,
                payload=payload,
            )
        )

    def _emit(self, ctx: TrialContext, name: str, payload: dict[str, Any]) -> None:
        """Emit a between-trials event. Stamped now and mirrored into the
        record exactly as the engine does mid-trial, so the two sources of
        events are indistinguishable downstream."""
        t = self._clock.now()
        ctx.record[f"t_{name.lower()}"] = t
        self._bus.emit(Event(name=name, t=t, trial_index=self._trial_index, payload=payload))

    def _start_tracker_trial(self, ctx: TrialContext, attempt: int) -> None:
        """Open the tracker's recording segment and refresh its operator
        overlay, before the trial's first frame."""
        if self._tracker is None:
            return
        self._tracker.start_trial(ctx.trial_index, f"attempt {attempt}")
        self._tracker.draw_host_overlay(host_overlay_shapes(self._screen, ctx.regions))

    def _pause_menu(self, fault: str | None = None) -> PauseMenu:
        """The menu for this session, built from what is actually wired.

        Built fresh at each pause rather than once at construction, because
        what is available can change during a session: a curriculum's stage
        keys are meaningless until a curriculum is running, and a fault
        heading belongs only to the pause it describes.
        """
        return build_pause_menu(
            has_tracker=self._eyetracker is not None,
            has_reward=self._manual_reward is not None,
            has_training=self._training is not None,
            has_dashboard=self._dashboard is not None,
            fault=fault,
        )

    def _show_pause_menu(self, menu: PauseMenu) -> None:
        self._display.show_menu(menu.title, menu.render(), color=menu.color)

    def _handle_pause(self, record: dict[str, Any], reward_failed: bool = False) -> bool:
        """Resolve a PAUSED trial; returns False when the experimenter chose
        to quit. With no pause strategy wired (unattended runs), resume
        immediately — blocking forever with nobody at the keyboard would
        hang a simulated session.

        The menu stays up across everything except resume and quit. Pressing
        the calibrate key used to calibrate and then resume in one press,
        which meant an experimenter who wanted to calibrate AND give a reward
        had to pause twice; and after a recalibration the natural thing to
        want is a look at the menu again, not the next trial.
        """
        if record.get("pause_action") == "calibrate":
            # The in-trial calibrate key: a pause that arrives with the
            # procedure already chosen.
            self._apply_pause_action("calibrate")
        # A reward failure is not a pause anybody asked for, so the screen
        # leads with what went wrong rather than with the word PAUSED.
        menu = self._pause_menu(fault="REWARD FAILURE — check the pump" if reward_failed else None)
        if self._dashboard is not None:
            return self._handle_dashboard_pause(menu)
        if self._on_pause is None:
            # Unattended. Still shown, so a simulated session's log records
            # that it stopped and why.
            self._show_pause_menu(menu)
            return self._resumed()
        while True:
            action = self._on_pause(menu)
            if action == "quit":
                return False
            if action == "resume":
                return self._resumed()
            self._apply_pause_action(action)

    def _apply_pause_action(self, action: str) -> str | None:
        """One non-terminal menu choice; returns the line the dashboard shows
        for it, or None when the action published its own.

        Anything unrecognised is logged rather than ignored: a key that
        silently does nothing is the fault this menu exists to prevent.
        """
        if action in PROCEDURE_ACTIONS:
            return self._run_procedure(action)
        if action == "manual_reward":
            self._manual_reward_while_paused()
            return None  # publishes its own outcome, which is more specific
        if action in PAUSE_STAGE_COMMANDS:
            self.on_session_command(PAUSE_STAGE_COMMANDS[action])
            return f"{action.replace('_', ' ')} requested."
        log.warning("unhandled pause action %r", action)
        return f"unhandled action {action!r}."

    def _run_procedure(self, action: str) -> str:
        """One eye-tracker procedure from the pause menu, and its one-line
        outcome. The monitor keeps the results and shows them on the
        dashboard's Eye tracker tab; this line is what the pause notice says.
        """
        monitor = self._eyetracker
        if monitor is None:
            log.warning("%s requested while paused, but no eye tracker is wired", action)
            return "No eye tracker is wired."
        if action == "calibrate":
            calibration = monitor.calibrate()
            line = calibration.summary()
            validation = monitor.validation
            # The validation the calibration triggered, if the rig asks for
            # one: newer than the calibration, so not a stale result.
            if validation is not None and validation.t >= calibration.t:
                line += f" · {validation.summary()}"
            return line
        if action == "validate":
            return monitor.validate().summary()
        return monitor.drift_correct().summary()

    def _resumed(self) -> bool:
        self._bus.emit(
            Event(name="RESUMED", t=self._clock.now(), trial_index=self._trial_index, payload={})
        )
        return True

    def _handle_dashboard_pause(self, menu: PauseMenu) -> bool:
        """Drive the local browser controls only after a keyboard pause.

        The browser is server-enforced read-only before this state is
        published. Keyboard polling remains available so closing the browser
        can never strand an experimenter in the pause screen.
        """
        assert self._dashboard is not None
        dashboard = self._dashboard
        # Drain and discard whatever is already queued. A command accepted in
        # the milliseconds between the browser seeing "paused" and the runner
        # resuming would otherwise sit in the queue and fire at the NEXT
        # pause — a reward delivered, or a session quit, minutes after the
        # click that asked for it and with nobody expecting it.
        stale = dashboard.poll_commands()
        if stale:
            log.info("discarding %d command(s) queued before this pause", len(stale))
        # The menu goes on the subject display here too. It did not used to,
        # so turning the dashboard on silently removed the only thing the
        # person standing at the rig could see — and the rig is where a pause
        # is usually resolved, browser or no browser.
        self._show_pause_menu(menu)
        self._publish_dashboard("paused", "Paused — browser controls are enabled.")
        keys = menu.actions()
        # A tracker with a camera gets its image refreshed through the pause,
        # so the Eye tracker tab shows the eye as it is now, not as it was
        # when the pause began.
        live_camera = self._eyetracker is not None and self._eyetracker.has_camera
        published_at = self._clock.now()
        while True:
            actions = [command.name for command in dashboard.poll_commands()]
            actions += [
                action
                for key in self._commands.poll_raw_keys()
                if (action := _menu_action(keys, key)) is not None
            ]
            for action in actions:
                if action == "resume":
                    self._publish_dashboard("running", "Resumed.")
                    return self._resumed()
                if action == "quit":
                    self._publish_dashboard("stopping", "Quit requested.")
                    return False
                message = self._apply_pause_action(action)
                # Every non-terminal action redraws the menu, because
                # _apply_pause_action may have put a calibration screen over
                # it, and a menu that vanishes after one keypress looks like
                # a session that has crashed.
                self._show_pause_menu(menu)
                if message is not None:
                    # Back to "paused" whatever the action published while it
                    # ran: the buttons are live again.
                    self._publish_dashboard("paused", message)
                published_at = self._clock.now()
            if live_camera and self._clock.now() - published_at >= CAMERA_REFRESH_S:
                self._publish_dashboard("paused", self._dashboard_message)
                published_at = self._clock.now()
            self._wait(0.01)

    def _manual_reward_while_paused(self) -> None:
        if self._manual_reward is None:
            self._publish_dashboard("paused", "No reward device is configured.")
            return
        try:
            self._manual_reward()
        except Exception as e:
            log.exception("manual reward failed while paused")
            self._emit_session_event("REWARD_FAILED", {"manual": True, "error": str(e)})
            self._publish_dashboard("paused", "Manual reward failed — check the pump.")
            return
        self._emit_session_event("REWARD", {"manual": True, **self._manual_reward_payload})
        self._publish_dashboard("paused", "Manual reward delivered.")

    def _publish_dashboard(
        self, status: str, message: str | None = None, full: bool = False
    ) -> dict[str, Any]:
        """Push one snapshot to the browser.

        Between trials the snapshot carries only the most recent
        ``dashboard.max_rows`` trials and events. Sending the whole history
        after every trial makes publishing cost grow with the square of the
        session's length, and a long session spends that time between trials
        where a subject is waiting. ``full=True`` — used once, at teardown —
        builds the complete state that gets written to disk.
        """
        if self._dashboard is None:
            return {}
        self._dashboard_revision += 1
        # The panels no trial record produces: the live analysis's, then the
        # eye tracker's. A camera frame is read only while the device is
        # between trials and somebody is looking (paused, or a procedure
        # running), and the pixels stay out of the copy written to disk.
        extra_panels: list[dict[str, Any]] = []
        if self._live is not None:
            extra_panels += self._live.panels()
        if self._eyetracker is not None:
            extra_panels += self._eyetracker.panels(
                camera=status in CAMERA_STATUSES and not full, image=not full
            )
        state = dashboard_state(
            revision=self._dashboard_revision,
            status=status,
            identity={
                "subject": self._cfg.info.subject,
                "session": self._cfg.info.session,
                "run": self._cfg.info.run,
                "task_name": self._cfg.info.task_name,
            },
            trials=self._recorder.trials,
            events=self._recorder.events,
            spec=self._dashboard_spec,
            condition_fields=self._condition_fields,
            training=self._training.stamp() if self._training is not None else None,
            message=message,
            max_rows=None if full else self._cfg.rig.dashboard.max_rows,
            extra_panels=extra_panels,
        )
        self._dashboard_message = message
        self._dashboard.publish(state)
        return state

    # ------------------------------------------------------------------

    def _attach_file_logging(self) -> logging.FileHandler:
        handler = logging.FileHandler(self._paths.log_path)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root = logging.getLogger()
        root.addHandler(handler)
        # The handler's level filters what IT writes; the root LOGGER's level
        # is checked first and defaults to WARNING, which would swallow every
        # info line before it reached the file. Raise it only if it is less
        # permissive than INFO — never lower an experimenter's DEBUG setting.
        if root.level == logging.NOTSET or root.level > logging.INFO:
            root.setLevel(logging.INFO)
        return handler

    def _teardown(self, file_handler: logging.FileHandler) -> None:
        errors: list[Exception] = []

        def step(name: str, fn: Callable[[], None]) -> None:
            try:
                fn()
            except Exception as e:  # logged loudly + collected, never swallowed
                log.exception("teardown step %r failed", name)
                errors.append(e)

        step("recorder.write", self._recorder.write)
        # Its own step, and early: a subject's place in its curriculum is
        # weeks of work, and must be written even if something later in
        # teardown fails.
        if self._training is not None:
            training = self._training
            # Wrapped rather than passed directly: save() returns the path it
            # wrote, and a teardown step returns nothing.
            step("training.save", lambda: (training.save(), None)[1])
            # A Task instance can outlive this session. The supervisor mutated
            # it stage by stage, so handing it back untouched is what stops a
            # second session from treating this one's last stage as its base.
            step("training.restore_base", training.restore_base)
        step("paradigm.summary", self._write_paradigm_summary)
        step("frames.save", lambda: self._frame_monitor.save(self._paths.frames_path))
        # The live analysis finishes BEFORE the final dashboard publish (so
        # the saved dashboard shows the flushed, final maps), before the
        # spike source closes (finishing drains it one last time), and
        # before the manifest is written (so what it saves is hashed).
        if self._live is not None:
            live = self._live
            step("live.finish", lambda: live.finish(self._paths.run_dir))
        if self._dashboard is not None:
            terminal = self._terminal_status(errors)
            # Complete state, not the capped one: what lands in figures/ is
            # the record of the session, and it is written once.
            final_state = self._publish_dashboard(terminal, f"Session {terminal}.", full=True)
            dashboard = self._dashboard
            step("dashboard.save", lambda: dashboard.save(self._paths.figures_dir, final_state))
            step("dashboard.stop", dashboard.stop)
        # Devices release BEFORE the manifest is written: the tracker's
        # recording is retrieved into this run's directory during shutdown,
        # and a manifest written first would not cover the very file the
        # session exists to produce.
        tracker, sync, reward = self._tracker, self._sync, self._reward
        if self._spikes is not None:
            step("spikes.close", self._spikes.close)
        if tracker is not None:
            # This run's directory and base name, carrying the EyeLink's
            # historical .edf suffix. Only the directory and the stem are a
            # promise: a backend whose native recording is not an EDF replaces
            # the suffix and may write more than one file (the viewpixx
            # backend writes samples and their clock alignment separately).
            recording_path = self._paths.run_dir / f"{self._paths.base}.edf"
            step("tracker.shutdown", lambda: tracker.shutdown(recording_path))
        if sync is not None:
            step("sync.close", sync.close)
        if reward is not None:
            step("reward.close", reward.close)
        # Close the log file before the manifest hashes it, so session.log's
        # recorded hash covers its complete contents.
        step("log.close", lambda: self._detach_file_logging(file_handler))
        step(
            "manifest.write",
            lambda: write_manifest(self._paths.run_dir, self._paths.manifest_path),
        )
        if self._database is not None:
            database = self._database
            status = self._terminal_status(errors)
            step(
                "database.write",
                lambda: (
                    database.write_run(
                        self._cfg,
                        self._paths,
                        trials=self._recorder.trials,
                        events=self._recorder.events,
                        frames=self._frame_monitor.records,
                        frame_inputs=self._frame_inputs.records,
                        status=status,
                    ),
                    None,
                )[1],
            )
        step("display.close", self._display.close)

        # Re-raise the first teardown error only when nothing else is already
        # propagating — a teardown failure must never mask the exception that
        # actually ended the session.
        if errors and sys.exc_info()[0] is None:
            raise errors[0]

    def _terminal_status(self, errors: list[Exception]) -> str:
        """How this session ended, in one word, for the mirror and the saved
        dashboard. "cancelled" is its own answer: a run abandoned before the
        first trial is not a run that completed with no data."""
        if sys.exc_info()[0] is not None or errors:
            return "failed"
        return "cancelled" if self._cancelled else "complete"

    def _write_paradigm_summary(self) -> None:
        """Write the scheduler's end-of-session state, if it has one — an
        adaptive fit or the per-cell counts that say whether the session
        ended balanced. A scheduler with nothing to say writes no file, and
        an absent file means exactly that."""
        summary = self._source.summary()
        if summary is None:
            return
        summary.to_csv(self._paths.paradigm_path, index=False)

    @staticmethod
    def _detach_file_logging(handler: logging.FileHandler) -> None:
        logging.getLogger().removeHandler(handler)
        handler.close()
