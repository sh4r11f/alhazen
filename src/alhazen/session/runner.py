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
from alhazen.devices.sync import SyncOutput
from alhazen.display.backend import DisplayBackend
from alhazen.display.frames import FrameMonitor
from alhazen.display.screen import Screen
from alhazen.paradigms.base import Condition, TrialSource
from alhazen.session.database import ExperimentDatabase, FrameInputBuffer
from alhazen.session.recorder import DataRecorder

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


def pause_menu(
    show_message: Callable[[str], None],
    raw_keys: Callable[[], list[str]],
    wait: Callable[[float], None],
) -> str:
    """Block until the experimenter chooses; returns "resume" | "calibrate" |
    "quit". The short wait keeps this from spinning a core while idle."""
    show_message("PAUSED — space: resume | c: calibrate | q/escape: quit")
    while True:
        for key in raw_keys():
            if key == "space":
                return "resume"
            if key == "c":
                return "calibrate"
            if key in ("q", "escape"):
                return "quit"
        wait(0.01)


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
        on_pause: Callable[[], str] | None = None,
        on_calibrate: Callable[[], None] | None = None,
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
        self._on_calibrate = on_calibrate
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
                    if not self._handle_pause(result.record):
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

    def _handle_pause(self, record: dict[str, Any]) -> bool:
        """Resolve a PAUSED trial; returns False when the experimenter chose
        to quit. With no pause strategy wired (unattended runs), resume
        immediately — blocking forever with nobody at the keyboard would
        hang a simulated session."""
        if record.get("pause_action") == "calibrate" and self._on_calibrate is not None:
            self._on_calibrate()
        if self._dashboard is not None:
            return self._handle_dashboard_pause()
        choice = self._on_pause() if self._on_pause is not None else "resume"
        if choice == "quit":
            return False
        if choice == "calibrate" and self._on_calibrate is not None:
            self._on_calibrate()
        self._bus.emit(
            Event(name="RESUMED", t=self._clock.now(), trial_index=self._trial_index, payload={})
        )
        return True

    def _handle_dashboard_pause(self) -> bool:
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
        self._publish_dashboard("paused", "Paused — browser controls are enabled.")
        while True:
            actions = [command.name for command in dashboard.poll_commands()]
            for key in self._commands.poll_raw_keys():
                if key == "space":
                    actions.append("resume")
                elif key == "c":
                    actions.append("calibrate")
                elif key in ("q", "escape"):
                    actions.append("quit")
                elif key == "r":
                    actions.append("manual_reward")
            for action in actions:
                if action == "resume":
                    self._bus.emit(
                        Event(
                            name="RESUMED",
                            t=self._clock.now(),
                            trial_index=self._trial_index,
                            payload={},
                        )
                    )
                    self._publish_dashboard("running", "Resumed.")
                    return True
                if action == "quit":
                    self._publish_dashboard("stopping", "Quit requested.")
                    return False
                if action == "calibrate":
                    if self._on_calibrate is None:
                        self._publish_dashboard("paused", "No eye tracker is configured.")
                    else:
                        self._on_calibrate()
                        self._publish_dashboard("paused", "Calibration complete.")
                elif action == "manual_reward":
                    self._manual_reward_while_paused()
                elif action in {"promote_stage", "demote_stage", "hold_stage"}:
                    mapping = {
                        "promote_stage": Command.PROMOTE_STAGE,
                        "demote_stage": Command.DEMOTE_STAGE,
                        "hold_stage": Command.HOLD_STAGE,
                    }
                    self.on_session_command(mapping[action])
                    self._publish_dashboard("paused", f"{action.replace('_', ' ')} requested.")
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
