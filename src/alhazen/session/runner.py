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
from collections.abc import Callable, Sequence
from typing import Any

import numpy as np

from alhazen.config.models import SessionConfig
from alhazen.config.snapshot import write_snapshot
from alhazen.core.clock import Clock
from alhazen.core.commands import Command, CommandSource
from alhazen.core.engine import QuitRequested, TrialEngine
from alhazen.core.events import Event, EventBus
from alhazen.core.trial import CircleRegion, TrialContext
from alhazen.dashboard.panels import frame_intervals_panel
from alhazen.dashboard.runtime import DashboardController, dashboard_state
from alhazen.dashboard.spec import DashboardSpec
from alhazen.data.manifest import write_manifest
from alhazen.data.participants import ensure_participant
from alhazen.data.paths import SessionPaths
from alhazen.devices.eyetracker import EyeTracker, HostShape
from alhazen.devices.eyetracker.procedures import ValidationResult
from alhazen.devices.eyetracker.protocol import CameraFrame
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


def _validation_shortfall(validation: ValidationResult) -> str:
    """How a validation that did not pass fell short, as a heading says it:
    over the limit, or complete only in part, or measuring nothing at all."""
    worst = validation.max_error_deg
    if worst is None:
        return "VALIDATION MEASURED NO TARGET"
    limit = f"{validation.threshold_deg:g}°"
    if worst > validation.threshold_deg:
        line = f"VALIDATION ABOVE THE {limit} LIMIT — worst {worst:.2f}°"
    else:
        line = f"VALIDATION INCOMPLETE — worst {worst:.2f}° within the {limit} limit"
    if validation.n_missed:
        line += f", {validation.n_missed} target(s) missed"
    return line


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
        setup_notes: Sequence[str] = (),
        max_consecutive_failures: int | None = None,
        rest_resume_after_s: float | None = None,
    ) -> None:
        self._cfg = cfg
        # How many non-completed trials in a row stop the session at the pause
        # screen. None never pauses. What counts as too many is the task's to
        # say (the builder reads it off the task's params), because a
        # fixation-break rate that is routine for one design is a subject who
        # cannot see the stimulus in another. A session that completed none
        # of 33 trials — every one a fixation break, with the eye sitting just
        # outside the window on a calibration that passed — ran to its end
        # with nothing on screen or in the log saying so. This is that line.
        if max_consecutive_failures is not None and max_consecutive_failures < 1:
            raise ValueError(
                f"max_consecutive_failures must be >= 1 or None, got {max_consecutive_failures}"
            )
        self._max_consecutive_failures = max_consecutive_failures
        # How long a rest between blocks waits for somebody before it resumes
        # by itself, or None to wait for a person however long that takes. A
        # real session leaves it None: the break is the subject's, and it ends
        # when the experimenter says so. Simulate mode sets it
        # (modes/session.py). A rehearsal on a real display has a keyboard
        # wired, so it is not "unattended" and its break used to wait for a
        # SPACE that nobody watching a dry run had a reason to press.
        if rest_resume_after_s is not None and rest_resume_after_s <= 0:
            raise ValueError(f"rest_resume_after_s must be > 0 or None, got {rest_resume_after_s}")
        self._rest_resume_after_s = rest_resume_after_s
        self._failures_in_a_row = 0
        # How many of the trials counted in the current streak dropped more of
        # their frames than frame QA allows, and that number as it stood when
        # the last streak pause was raised (the running count is reset by then).
        self._failure_streak_display_trials = 0
        self._paused_streak_display_trials = 0
        # What was decided about this session before it started — a mode's
        # reductions and stood-down devices (modes/session.py describe()),
        # in the experimenter's words. Logged right after "session start",
        # so the run directory itself records what was live and what was
        # reduced; printing them to a terminal left no trace in the run. A
        # public attribute because the mode learns them after the builder
        # has already returned the runner.
        self.setup_notes: list[str] = list(setup_notes)
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
        log.info("devices: %s", self._devices_line())
        for note in self.setup_notes:
            log.info("setup: %s", note)
        if self._dashboard is not None:
            log.info("live dashboard: %s", self._dashboard.url)
            if self._eyetracker is not None:
                # Camera frames stream to the page on their own channel while
                # the session is paused or a procedure runs. Wired here, once
                # the dashboard is known to be open, so a session without one
                # never reads a frame nobody will see.
                self._eyetracker.camera_sink = self._send_camera_frame
                # And the tracker settings the page sends (the iris size),
                # applied between frames, even while a procedure runs.
                self._eyetracker.settings_source = self._poll_tracker_settings
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
            if not self._require_tracker_calibration():
                return
            while True:
                condition = self._source.next()
                if condition is None:
                    break  # the scheduler's definition of "session done"
                # A scheduler that knows its block boundaries (BlockPlan)
                # leaves a break here when one has just ended; the session
                # takes it now, before this block's first trial is built.
                take_break = getattr(self._source, "take_block_break", None)
                pending = take_break() if take_break is not None else None
                if pending is not None and not self._block_break(*pending):
                    break

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
                self._log_trial(attempt, result.record, outcome)

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

                # Counted here, before the pause branches, so that a trial
                # the subject completed clears the count even when its reward
                # pump failed. The reward branch below `continue`s, and used
                # to carry the counter past a completed trial untouched: two
                # fixation breaks, a completed trial with a dead pump, one
                # more fixation break and the screen said three in a row.
                too_many_failures = self._too_many_failures_in_a_row(outcome)

                if outcome.name == "PAUSED" or reward_failed:
                    # A reward failure goes through the same pause flow as a
                    # deliberate pause: a human has to look at the pump before
                    # the session carries on rewarding nothing. The
                    # measurement is already recorded above — a hardware fault
                    # after the fact must never discard a trial the subject
                    # actually completed.
                    fault = "REWARD FAILURE — check the pump" if reward_failed else None
                    if not self._handle_pause(result.record, fault=fault):
                        break
                    continue  # the pause menu already gave all the time needed; skip ITI

                if too_many_failures:
                    fault = self._failure_streak_heading(outcome)
                    if not self._handle_pause(result.record, fault=fault):
                        break
                    continue

                if self._iti_s > 0:
                    self._wait(self._iti_s)
        finally:
            self._teardown(file_handler)

    # ------------------------------------------------------------------
    # The session log's structure
    # ------------------------------------------------------------------

    def _devices_line(self) -> str:
        """Which backend drives each device this session, in one line.

        The snapshot holds the same facts, but a log that says "eyetracker
        viewpixx" on its third line is the one a reader opens first when a
        run looks wrong.
        """
        devices = self._cfg.rig.devices
        parts = [f"display {self._cfg.rig.display.backend}"]
        for name in ("eyetracker", "reward", "sync", "recording", "spikes"):
            device = getattr(devices, name, None)
            backend = getattr(device, "backend", None) if device is not None else None
            parts.append(f"{name} {backend if backend is not None else 'none'}")
        return ", ".join(parts)

    def _log_trial(self, attempt: int, record: dict[str, Any], outcome: Any) -> None:
        """One line per trial: the backbone a session log is read by."""
        detail = ""
        if record.get("abort_reason"):
            detail = f" ({record['abort_reason']})"
        elif record.get("frame_qa_reason"):
            detail = f" (was {record.get('outcome_before_frame_qa')}: {record['frame_qa_reason']})"
        log.info(
            "trial %d attempt %d: %s%s%s",
            self._trial_index,
            attempt,
            outcome.name,
            "" if outcome.completed else " — not completed, condition re-served",
            detail,
        )

    def _log_session_end(self) -> None:
        """The last line the session writes about itself, so a log that stops
        mid-trial can be told from one that ended: how it ended, how many
        trials were served, and how their rows came out."""
        rows = self._recorder.trials
        counts = Counter(str(row.get("outcome")) for row in rows)
        outcomes = ", ".join(f"{name} {count}" for name, count in sorted(counts.items()))
        exc = sys.exc_info()[1]
        if exc is not None:
            log.error(
                "session end: FAILED on trial %d after %d rows (%s) — %s: %s",
                self._trial_index,
                len(rows),
                outcomes or "no rows",
                type(exc).__name__,
                exc,
            )
            return
        status = "cancelled" if self._cancelled else "complete"
        log.info(
            "session end: %s — %d trials served, %d rows recorded (%s)",
            status,
            self._trial_index,
            len(rows),
            outcomes or "no rows",
        )

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

    def _block_break(self, done: int, total: int) -> bool:
        """The rest between blocks: the pause screen, headed with how far
        the session has got, until the experimenter resumes. Returns False
        when they quit instead.

        Its own heading and colour, because the pause menu now also leads
        with faults, and a subject looking at the screen during a break must
        not be looking at the thing that appears when a calibration dies.
        The block count is the heading because "how much longer" is the one
        question a break gets asked.
        """
        log.info("block %d of %d complete: taking the break", done, total)
        self._emit_session_event(
            "PAUSED", {"reason": "block_break", "blocks_done": done, "blocks_total": total}
        )
        return self._handle_pause({}, rest=f"BLOCK {done} OF {total} COMPLETE — REST")

    def _too_many_failures_in_a_row(self, outcome: Any) -> bool:
        """Count the subject's failed trials back to back; True on the one that
        reaches the task's limit, which the caller turns into a pause.

        What counts is what the SUBJECT did, trial by trial:

        - A completed trial ends the streak.
        - ``DROPPED_FRAMES`` ends it too. The engine only recycles a trial the
          subject completed (core/engine.py): the display failed, not the eye,
          and the row keeps what the subject did as ``outcome_before_frame_qa``.
          Recycles used to be skipped over instead, neither counted nor ending
          anything, and that let a failing display join separate runs of
          failures into one. A rehearsal whose completed trials were all
          recycled paused on "6 trials in a row" for fixation breaks and missed
          saccades that those completed trials had separated, and told the
          operator to check a calibration while the panel dropped half its
          frames. Frame QA counts recycles on its own and stops the run with
          the display's message.
        - ``PAUSED`` neither counts nor ends it: the experimenter stopped the
          trial, and that says nothing about the subject. The count restarts
          after the pause this raises, so a subject still not fixating gets a
          whole new run of chances rather than a pause every trial.
        - Every other outcome that did not complete counts.

        Beside the count it keeps how many of the counted trials dropped more
        of their frames than frame QA's budget, so the pause can say when the
        display was failing through the streak. A panel missing vsyncs can
        cause real fixation breaks, and what must not happen is sending the
        experimenter to recalibrate while it does.
        """
        limit = self._max_consecutive_failures
        if limit is None or outcome.name == "PAUSED":
            return False
        if outcome.completed or outcome.name == "DROPPED_FRAMES":
            self._failures_in_a_row = 0
            self._failure_streak_display_trials = 0
            return False
        self._failures_in_a_row += 1
        if self._display_was_failing():
            self._failure_streak_display_trials += 1
        if self._failures_in_a_row < limit:
            return False

        display_trials = self._failure_streak_display_trials
        if display_trials:
            log.warning(
                "%d trials in a row not completed, the last %s on trial %d, and %d of them "
                "dropped more than %.0f%% of their frames: pausing. The display was failing "
                "through this streak, and a panel missing vsyncs causes real fixation breaks "
                "— check the display before recalibrating.",
                self._failures_in_a_row,
                outcome.name,
                self._trial_index,
                display_trials,
                self._frame_monitor.dropped_fraction_budget * 100,
            )
        else:
            log.warning(
                "%d trials in a row not completed, the last %s on trial %d: pausing. The "
                "subject may not be seeing what this session is measuring — a calibration "
                "that passed but sits at the edge of the fixation window looks exactly like "
                "this.",
                self._failures_in_a_row,
                outcome.name,
                self._trial_index,
            )
        self._paused_streak_display_trials = display_trials
        self._failures_in_a_row = 0
        self._failure_streak_display_trials = 0
        return True

    def _display_was_failing(self) -> bool:
        """Did the trial that just ended drop more of its frames than frame QA's
        budget allows?

        False on a simulated display: its frame times measure how accurately
        the host can wait, not a panel, which is also why the session builder
        stands frame QA down there (session/builder.py).
        """
        if getattr(self._display, "kind", None) == "simulated":
            return False
        frames = self._frame_monitor.last_trial
        return (
            frames is not None
            and frames.dropped_fraction > self._frame_monitor.dropped_fraction_budget
        )

    def _failure_streak_heading(self, outcome: Any) -> str:
        """What the pause screen leads with when the failure streak stops the
        session: the display first when it was failing through the streak,
        the subject-side checks otherwise."""
        limit = self._max_consecutive_failures
        display_trials = self._paused_streak_display_trials
        if display_trials:
            return (
                f"{limit} TRIALS FAILED IN A ROW — last {outcome.name}, and {display_trials} "
                f"of them dropped over {self._frame_monitor.dropped_fraction_budget:.0%} of "
                f"their frames; check the display before recalibrating"
            )
        return (
            f"{limit} TRIALS FAILED IN A ROW — last {outcome.name}; check the calibration "
            f"(V), the subject, and the stimulus before resuming"
        )

    def _require_tracker_calibration(self) -> bool:
        """Before trial 1: a tracker that can say it holds no calibration
        stops the session at the pause screen, with that reason, until the
        experimenter has calibrated (or chosen to go on). Returns False
        when they quit instead.

        Only trackers with the optional ``calibration_state`` capability
        (the TRACKPixx3) are asked; the EyeLink's Host PC owns its own
        calibration and the stand-ins have none. Gaze from an uncalibrated
        device is not a position, and a session that ran on it would look
        like a session where the subject never fixated — every trial a
        fixation break, nothing in the record saying why.
        """
        state = getattr(self._tracker, "calibration_state", None)
        if state is None or state():
            return True
        log.warning(
            "the eye tracker reports NO calibration before trial 1; pausing until one is "
            "done (C on the pause screen, or the dashboard's Calibrate button)"
        )
        return self._handle_pause({}, fault="TRACKER NOT CALIBRATED — press C to calibrate")

    def _start_tracker_trial(self, ctx: TrialContext, attempt: int) -> None:
        """Open the tracker's recording segment and refresh its operator
        overlay, before the trial's first frame."""
        if self._tracker is None:
            return
        self._tracker.start_trial(ctx.trial_index, f"attempt {attempt}")
        self._tracker.draw_host_overlay(host_overlay_shapes(self._screen, ctx.regions))

    def _pause_menu(
        self,
        fault: str | None = None,
        rest: str | None = None,
        resumes_in_s: float | None = None,
        warning: str | None = None,
    ) -> PauseMenu:
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
            rest=rest,
            resumes_in_s=resumes_in_s,
            warning=warning,
        )

    def _show_pause_menu(self, menu: PauseMenu) -> None:
        self._display.show_menu(menu.title, menu.render(), color=menu.color)

    def _handle_pause(
        self, record: dict[str, Any], *, fault: str | None = None, rest: str | None = None
    ) -> bool:
        """Resolve a PAUSED trial; returns False when the experimenter chose
        to quit. With no pause strategy wired (unattended runs), resume
        immediately — blocking forever with nobody at the keyboard would
        hang a simulated session. That check comes FIRST, before the
        dashboard: whether anyone is at the rig and whether a browser is
        serving are different questions, and answering the second one first
        hung every unattended run of a rig with the dashboard turned on.

        ``fault`` makes this an involuntary pause — a reward failure, a
        tracker with no calibration — and the screen leads with what went
        wrong rather than with the word PAUSED. ``rest`` is the opposite: a
        scheduled break, headed and coloured as one.

        The menu stays up across everything except resume and quit. Pressing
        the calibrate key used to calibrate and then resume in one press,
        which meant an experimenter who wanted to calibrate AND give a reward
        had to pause twice; and after a recalibration the natural thing to
        want is a look at the menu again, not the next trial.
        """
        notice = "Paused — browser controls are enabled."
        if record.get("pause_action") == "calibrate":
            # The in-trial calibrate key: a pause that arrives with the
            # procedure already chosen. Its verdict becomes the pause notice,
            # so the browser says "calibrated …" or "NOT calibrated …" rather
            # than only that the session is paused.
            notice = self._apply_pause_action("calibrate") or notice
        elif fault is not None:
            notice = f"{fault} — browser controls are enabled."
        elif rest is not None:
            notice = f"{rest.capitalize()} — resume when the subject is ready."
        # A rest can resume by itself when nobody acts in time: a simulation's
        # break, and only with someone who could act, since an unattended run
        # below resumes at once anyway. Never a fault: a pump or a
        # calibration that failed is exactly what somebody has to look at.
        resume_after_s = (
            self._rest_resume_after_s if rest is not None and self._on_pause is not None else None
        )
        menu = self._pause_menu(fault=fault, rest=rest, resumes_in_s=resume_after_s)
        if self._on_pause is None:
            # Nobody is going to answer. `on_pause` is wired only for a
            # rendering display with a keyboard behind it (session/builder.py),
            # so None means an unattended run — and that is true whether or
            # not the rig file turned the dashboard on. A dashboard is a
            # window onto the session, not a person at it; waiting for a
            # browser click that will never come hung every unattended run of
            # a rig with `dashboard.enabled`, and a scheduled block break made
            # that every simulated run of a multi-block experiment.
            #
            # The menu is still drawn and the skipped pause still logged, at
            # WARNING: a pause that did not pause is a real difference between
            # what the session was asked to do and what it did, and the run
            # that finds out is the dry run, not the one with a subject in it.
            self._show_pause_menu(menu)
            log.warning(
                "pause with nobody to answer it (no keyboard wired — unattended run): "
                "resuming immediately. %s",
                notice,
            )
            if self._dashboard is not None:
                # Left out, a dashboard open on a dry run would sit on the
                # last state it was told about while the session ran on.
                self._publish_dashboard("running", f"{notice} Unattended — resumed.")
            return self._resumed()
        if self._dashboard is not None:
            return self._handle_dashboard_pause(
                menu, notice, fault=fault, rest=rest, resume_after_s=resume_after_s
            )
        deadline: float | None = None
        if resume_after_s is not None:
            deadline = self._clock.now() + resume_after_s
        while True:
            if deadline is not None:
                # `on_pause` blocks until a key is pressed, so a pause that can
                # time out polls the keyboard here instead, until the deadline.
                timed = self._next_menu_action_before(menu, deadline)
                if timed is None:
                    return self._resumed_by_itself(resume_after_s or 0.0)
                # Somebody is there after all. From here the rest waits for
                # them, and the screen stops promising otherwise.
                action = timed
                deadline = None
                menu = self._pause_menu(fault=fault, rest=rest)
            else:
                action = self._on_pause(menu)
            if action == "quit":
                return False
            if action == "resume":
                return self._resumed()
            self._apply_pause_action(action)
            # The menu is rebuilt after every procedure, not only after one
            # that failed. A procedure that failed becomes the heading of the
            # menu that comes back, on the screen the experimenter is actually
            # facing — and a procedure that then SUCCEEDS has to take that
            # heading back down again. Without this, a red VALIDATION FAILED
            # stays up after the recalibration that fixed it, and the pause's
            # own heading (a block break's REST) never comes back.
            if action in PROCEDURE_ACTIONS:
                menu = self._menu_after_procedure(action, fault=fault, rest=rest)

    def _next_menu_action_before(self, menu: PauseMenu, deadline: float) -> str | None:
        """Draw the menu and poll the keyboard until a key picks an action or
        the deadline passes. None when it passed.

        The keyboard half of a pause that can time out. ``on_pause`` blocks
        until a key is pressed, which is right for a pause a person has to
        resolve and wrong for one that resumes by itself, so the runner polls
        the same keys, through the same row mapping the dashboard path uses.
        """
        self._show_pause_menu(menu)
        keys = menu.actions()
        while self._clock.now() < deadline:
            for key in self._commands.poll_raw_keys():
                action = _menu_action(keys, key)
                if action is not None:
                    return action
            self._wait(0.01)
        return None

    def _resumed_by_itself(self, after_s: float) -> bool:
        """End a rest that nobody resolved in time: say so, then resume."""
        log.info(
            "the rest between blocks resumed by itself after %g s: nothing was pressed "
            "(simulation)",
            after_s,
        )
        if self._dashboard is not None:
            self._publish_dashboard(
                "running", f"Resumed by itself after {after_s:g} s (simulation)."
            )
        return self._resumed()

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

    def _menu_after_procedure(
        self, action: str, *, fault: str | None, rest: str | None
    ) -> PauseMenu:
        """The pause menu to show once a procedure has run.

        A procedure that failed heads it as a fault, and a validation that did
        not pass heads it as a warning; either replaces the pause's own
        heading while it stands. After a procedure that succeeded, the pause's
        own heading comes back: a block break's REST, or the fault that
        opened the pause.
        """
        failed = self._procedure_fault(action)
        if failed is not None:
            return self._pause_menu(fault=failed)
        warned = self._procedure_warning(action)
        if warned is not None:
            return self._pause_menu(warning=warned)
        return self._pause_menu(fault=fault, rest=rest)

    def _procedure_fault(self, action: str) -> str | None:
        """The heading the pause screen leads with after a procedure that
        failed, or None.

        The verdict already goes to the dashboard's notice line and the log.
        Neither is the screen the experimenter is looking at while they stand
        at the rig, so a calibration the tracker did not take, or a drift
        correction it refused, leads the menu that comes back. A validation
        that did not pass is a warning instead (_procedure_warning).
        """
        monitor = self._eyetracker
        if monitor is None or action not in PROCEDURE_ACTIONS:
            return None
        calibration, drift = monitor.calibration, monitor.drift
        if action == "calibrate" and calibration is not None and calibration.ok is False:
            return f"CALIBRATION FAILED — {calibration.note or 'the tracker reports none'}"
        if action == "drift_correct" and drift is not None and not drift.applied:
            return f"DRIFT CORRECTION REFUSED — {drift.note or drift.summary()}"
        return None

    def _procedure_warning(self, action: str) -> str | None:
        """The heading after a validation that did not pass, or None.

        A warning, not a fault. Whether a calibration is good enough is the
        experimenter's call: a validation a little over its limit can be the
        best a subject manages that day, and a heading that said "recalibrate
        before resuming" kept an experimenter recalibrating a subject who was
        not going to do better. So the heading says how the validation fell
        short and offers both ways on. Resuming on it is recorded (_resumed).
        """
        monitor = self._eyetracker
        if monitor is None or action not in ("calibrate", "validate"):
            return None
        validation = monitor.validation
        if validation is None or validation.accepted or validation.aborted:
            return None
        return f"{_validation_shortfall(validation)} — SPACE resumes on it, C recalibrates"

    def _resumed(self) -> bool:
        payload: dict[str, Any] = {}
        monitor = self._eyetracker
        validation = monitor.validation if monitor is not None else None
        if validation is not None and not validation.accepted and not validation.aborted:
            # The session is going on under a validation that did not pass.
            # That is the experimenter's decision to make, and it is recorded
            # where an analysis and a later reader will look: in this event,
            # with the numbers, and in the log, in words. The VALIDATION event
            # and its per-target errors were written when it ran.
            payload["on_failed_validation"] = {
                "t": validation.t,
                "mean_error_deg": validation.mean_error_deg,
                "max_error_deg": validation.max_error_deg,
                "threshold_deg": validation.threshold_deg,
                "n_missed": validation.n_missed,
            }
            log.warning(
                "resumed on a validation that did not pass, as the experimenter chose: %s",
                validation.summary(),
            )
        self._bus.emit(
            Event(
                name="RESUMED", t=self._clock.now(), trial_index=self._trial_index, payload=payload
            )
        )
        return True

    def _handle_dashboard_pause(
        self,
        menu: PauseMenu,
        notice: str,
        *,
        fault: str | None = None,
        rest: str | None = None,
        resume_after_s: float | None = None,
    ) -> bool:
        """Drive the local browser controls only after a keyboard pause.

        The browser is server-enforced read-only before this state is
        published. Keyboard polling remains available so closing the browser
        can never strand an experimenter in the pause screen. `notice` is the
        line the browser shows as the pause begins; `fault` and `rest` are the
        pause's own heading, kept so that a procedure run from the browser can
        put it back after replacing it.
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
        self._publish_dashboard("paused", notice)
        keys = menu.actions()
        # A tracker with a camera gets its image refreshed through the pause,
        # so the Eye tracker tab shows the eye as it is now, not as it was
        # when the pause began.
        live_camera = self._eyetracker is not None and self._eyetracker.has_camera
        monitor = self._eyetracker
        published_at = self._clock.now()
        # A rest that can resume by itself: the same deadline as the keyboard
        # path, cancelled by the first thing anybody does.
        deadline: float | None = None
        if resume_after_s is not None:
            deadline = self._clock.now() + resume_after_s
        while True:
            # What the page asks of the tracker between clicks: a camera frame
            # whenever one is due (session/eyetracker.py CAMERA_STREAM_S), and
            # any tracker setting it sent, applied and reported in the notice.
            # The refresh further down republishes the panel's words about once
            # a second.
            if monitor is not None:
                for setting_line in monitor.service_dashboard():
                    self._publish_dashboard("paused", setting_line)
                    published_at = self._clock.now()
            actions = [command.name for command in dashboard.poll_commands()]
            actions += [
                action
                for key in self._commands.poll_raw_keys()
                if (action := _menu_action(keys, key)) is not None
            ]
            if deadline is not None:
                if actions:
                    # Somebody acted, at the rig or in the browser: the rest
                    # waits for them from here, and the menu drawn after their
                    # action no longer says it will resume by itself.
                    deadline = None
                    menu = self._pause_menu(fault=fault, rest=rest)
                elif self._clock.now() >= deadline:
                    return self._resumed_by_itself(resume_after_s or 0.0)
            for index, action in enumerate(actions):
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
                # a session that has crashed. A procedure that failed becomes
                # the menu's heading: the browser gets the verdict as its
                # notice, but the rig's own screen must say it too.
                if action in PROCEDURE_ACTIONS:
                    # Rebuilt after every procedure, so a heading that a
                    # failure put up comes back down when a later procedure
                    # succeeds, and the pause's own heading returns with it.
                    menu = self._menu_after_procedure(action, fault=fault, rest=rest)
                self._show_pause_menu(menu)
                if action in PROCEDURE_ACTIONS:
                    # A procedure runs for seconds to minutes, and the browser
                    # keeps accepting clicks until it learns of the
                    # "calibrating" status — about 0.2 s after the first
                    # click. A double-click on Calibrate, or Validate pressed
                    # right after it, would otherwise sit in the queue and run
                    # NOW, after the procedure, with nobody expecting a second
                    # walk. Discard it, and the rest of this batch, before the
                    # buttons come back; the keys a walk polls are already
                    # consumed by the walk itself.
                    dropped = actions[index + 1 :] + [c.name for c in dashboard.poll_commands()]
                    if dropped:
                        log.info(
                            "discarding %d command(s) queued while %s ran: %s",
                            len(dropped),
                            action,
                            ", ".join(dropped),
                        )
                if message is not None:
                    # Back to "paused" whatever the action published while it
                    # ran: the buttons are live again.
                    self._publish_dashboard("paused", message)
                published_at = self._clock.now()
                if action in PROCEDURE_ACTIONS:
                    break
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
        extra_panels: list[dict[str, Any]] = [self._frame_timing_panel()]
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

    def _poll_tracker_settings(self) -> list[tuple[str, object]]:
        """The tracker settings the page sent, for the monitor to apply."""
        return self._dashboard.poll_settings() if self._dashboard is not None else []

    def _send_camera_frame(self, frame: CameraFrame) -> None:
        """The monitor's streamed camera frames, onto the dashboard's camera channel."""
        if self._dashboard is not None:
            self._dashboard.publish_camera(frame.pixels, frame.t)

    def _frame_timing_panel(self) -> dict[str, Any]:
        """The frame-interval histogram, from the monitor's own record: the
        one panel whose data is the frame log rather than the trials."""
        monitor = self._frame_monitor
        return frame_intervals_panel(
            monitor.intervals_s(),
            monitor.expected_s,
            monitor.threshold_s,
            n_dropped=monitor.n_dropped,
        )

    # ------------------------------------------------------------------

    def _attach_file_logging(self) -> logging.FileHandler:
        # UTF-8 by name, not the platform default: on Windows that default
        # is cp1252, and every line with a dash or a degree sign in it came
        # back from the rig's own logs as mojibake.
        handler = logging.FileHandler(self._paths.log_path, encoding="utf-8")
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
        # First, before any step can fail: the log's own account of how the
        # session ended is worth more than a step's failure message, and a
        # log that simply stops is what this line exists to prevent.
        self._log_session_end()

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
