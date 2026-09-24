"""SessionRunner: the outer loop an experimenter actually starts.

Contract, in order:

1. The config snapshot is written *before anything else* — a session that
   crashes still documents what it was trying to run. A session whose
   snapshot cannot be written never started: teardown releases its devices
   and writes nothing into its run directory.
2. File logging attaches at the root logger so every module's logging lands
   in this run's ``session.log``; then the subject is registered.
3. Loop: ask the paradigm for a condition, build the trial through the
   task's ``build_trial``, open the tracker's recording segment (and close it
   in a ``finally``), run it through the engine and let its mid-trial reward
   deliveries finish, tell the scheduler how it
   went (for **every** outcome — the scheduler alone decides re-queueing),
   pay what it earned, record the measurement (for every outcome except
   PAUSED, which produced none), wait out the ITI. A trial lost to a system
   fault — dropped frames, a tracker that stopped — is re-served like any
   non-completed trial, but paid all the same and held against nobody
   (``TrialResult.lost_to_fault``).
4. However the session ends (in the loop, or in any step before it that
   set the session up), teardown attempts *every* step — a session is
   unrepeatable work, so writing the trials table must survive a display
   that fails to close, and vice versa. Step errors are logged and collected;
   the first is re-raised only if no other exception is already propagating.

This module keeps the loop and the lifecycle. Three decisions the loop
consults live beside it, built by the runner from its own arguments: when a
streak of bad trials pauses the session (session/streaks.py), what a trial
earned and paying it (session/reward_payer.py), and resolving a pause
(session/pause_control.py).
"""

from __future__ import annotations

import logging
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from alhazen.config.models import DEFAULT_MAX_CONSECUTIVE_DROPOUTS, SessionConfig
from alhazen.config.snapshot import write_snapshot
from alhazen.core.clock import Clock
from alhazen.core.commands import Command, CommandSource
from alhazen.core.engine import QuitRequested, TrialEngine, TrialResult
from alhazen.core.events import Event, EventBus
from alhazen.core.trial import (
    FAULT_DROPPED_FRAMES,
    FAULT_TRACKER_STOPPED,
    NO_FAULT,
    PAUSED,
    CircleRegion,
    Outcome,
    TrialContext,
)
from alhazen.dashboard.panels import frame_intervals_panel
from alhazen.dashboard.runtime import DashboardController, dashboard_state
from alhazen.dashboard.spec import DashboardSpec
from alhazen.data.manifest import write_manifest
from alhazen.data.participants import ensure_participant
from alhazen.data.paths import SessionPaths
from alhazen.data.percents import threshold_percent
from alhazen.devices.eyetracker import EyeTracker, HostShape
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
    pause_menu,  # noqa: F401 - re-exported: it lived here until 1.1
)
from alhazen.session.pause_control import PauseController
from alhazen.session.recorder import DataRecorder
from alhazen.session.reward_payer import RewardPayer
from alhazen.session.streaks import DropoutStreak, FailureStreak, StreakMonitor
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


# The statuses during which a camera frame is read for the dashboard: when
# the device is not busy with a trial and somebody is looking at the image.
CAMERA_STATUSES = frozenset({"paused", PROCEDURE_STATUS})


# What each system fault was, in the words the session log uses. A health
# check the engine gains later would report a reason not listed here; it is
# still named, by that reason, in the fallback where this is read.
_FAULT_CAUSES = {
    FAULT_DROPPED_FRAMES: "the display dropped more frames than frame QA allows",
    FAULT_TRACKER_STOPPED: (
        "the eye tracker stopped recording before the trial's outcome was decided"
    ),
}


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
        max_consecutive_dropouts: int | None = DEFAULT_MAX_CONSECUTIVE_DROPOUTS,
        experiment_dir: Path | None = None,
    ) -> None:
        self._cfg = cfg
        # Where the experiment's code lives, so the snapshot's
        # `experiment_git_sha` describes that repository. None falls back to
        # the working directory (config.snapshot.build_provenance), which is
        # only right when the session happens to be started from inside the
        # experiment's checkout; build_session passes the task's own folder.
        self._experiment_dir = experiment_dir
        # The subject's failure streak and the device's dropout streak: which
        # trials count toward which, and when either stops the session at the
        # pause screen (session/streaks.py, which also validates both limits).
        # max_consecutive_dropouts is the rig's number, from
        # eyetracker.max_consecutive_dropouts (session/builder.py), and on by
        # default here too, for a runner built by hand;
        # max_consecutive_failures is the task's, read off its params.
        self._streaks = StreakMonitor(max_consecutive_failures, max_consecutive_dropouts)
        # How long a rest between blocks waits for somebody before it resumes
        # by itself, or None to wait for a person however long that takes. A
        # real session leaves it None: the break is the subject's, and it ends
        # when the experimenter says so. Simulate mode sets it
        # (modes/session.py). A rehearsal on a real display has a keyboard
        # wired, so it is not "unattended" and its break used to wait for a
        # SPACE that nobody watching a dry run had a reason to press.
        if rest_resume_after_s is not None and rest_resume_after_s <= 0:
            raise ValueError(f"rest_resume_after_s must be > 0 or None, got {rest_resume_after_s}")
        # Kept here as well as handed to the pause controller below, so the
        # builder's wiring of it can be read back off the runner.
        self._rest_resume_after_s = rest_resume_after_s
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
        self._refresh_rate_hz = refresh_rate_hz
        self._task_rng = task_rng
        self._iti_s = iti_s
        # ``score`` is the experiment's derived-measure hook: task-specific
        # metrics (a saccade bias, a shift estimate) are computed here by
        # experiment code, never inside the engine.
        self._score = score
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
        # The pay rule and its delivery at the end of each trial
        # (session/reward_payer.py): what each outcome earns under
        # `reward_policy`, paid through `reward`. The runner keeps `reward`
        # itself for its lifecycle (teardown closes it).
        self._payer = RewardPayer(reward, reward_policy, display, self._emit)
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
        # The manual-reward hook the engine's key and the pause menu share,
        # kept here as well as handed to the pause controller below, so the
        # builder's wiring of it can be read back off the runner.
        self._manual_reward = manual_reward
        self._manual_reward_payload = dict(manual_reward_payload or {})
        # The pause screen, from the moment a pause is raised until the
        # experimenter resumes or quits (session/pause_control.py): the
        # keyboard loop and the dashboard loop, the menu's procedures, the
        # manual reward and the stage keys, and the RESUMED event. It acts
        # on the session through the runner's own publisher, emitter and
        # stage-command handler.
        self._pauses = PauseController(
            display=display,
            clock=clock,
            commands=commands,
            wait=self._wait,
            on_pause=on_pause,
            rest_resume_after_s=rest_resume_after_s,
            eyetracker=eyetracker,
            dashboard=dashboard,
            has_training=training is not None,
            manual_reward=manual_reward,
            manual_reward_payload=self._manual_reward_payload,
            publish=self._publish_dashboard,
            last_message=lambda: self._dashboard_message,
            emit_session_event=self._emit_session_event,
            on_session_command=self.on_session_command,
        )
        self._dashboard_revision = 0
        self._dashboard_message: str | None = None
        # A session cancelled at the instructions screen flows through the
        # same teardown a finished one does, so without this the mirror
        # recorded status="complete" for a run with zero trials —
        # indistinguishable from one that ran and produced nothing.
        self._cancelled = False

        self._trial_index = 0
        self._attempt_counts: Counter = Counter()
        # The most recent trial's context. Teardown settles its mid-trial
        # reward deliveries: a quit or a fault leaves the loop before the
        # between-trials settle, and those drops still happened.
        self._last_ctx: TrialContext | None = None

    # ------------------------------------------------------------------

    def run(self) -> None:
        # How far the start got, for the teardown in the finally below. Every
        # setup step runs inside that try, not before it: by the time run() is
        # called the builder has opened the window, the reward and sync
        # devices, connected the tracker and the spike stream, and started the
        # dashboard's child process. A setup step that failed before the try —
        # a session.log that could not be opened, a participants.tsv another
        # program held, a dashboard publish that raised — left every one of
        # them held, and no "session end" line anywhere.
        snapshot_written = False
        file_handler: logging.FileHandler | None = None
        try:
            # First, before anything else is written: a session that crashes
            # still documents what it was trying to run. Until it is on disk
            # the run directory is not a run, and teardown releases the
            # devices without writing anything into it (_teardown).
            write_snapshot(self._cfg, self._paths.snapshot_path, self._experiment_dir)
            snapshot_written = True
            # The log before the registry, so a participants.tsv that cannot
            # be written ends with a "session end: FAILED" line in this run's
            # own log rather than only on a terminal.
            file_handler = self._attach_file_logging()
            ensure_participant(self._cfg.rig.data_root, self._cfg.info.subject)

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
                    # Camera frames stream to the page on their own channel
                    # while the session is paused or a procedure runs. Wired
                    # here, once the dashboard is known to be open, so a
                    # session without one never reads a frame nobody will see.
                    self._eyetracker.camera_sink = self._send_camera_frame
                    # And the tracker settings the page sends (the iris size),
                    # applied between frames, even while a procedure runs.
                    self._eyetracker.settings_source = self._poll_tracker_settings
            self._publish_dashboard("running")

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
                self._last_ctx = ctx
                try:
                    # Inside the try, not before it: opening the recording
                    # segment can fail partway through (a tracker that starts
                    # recording and then reports no usable eye), and the
                    # finally below is what stops it again.
                    self._start_tracker_trial(ctx, attempt)
                    result = self._engine.run_trial(ctx, phases)
                    # Every mid-trial drop finishes and is counted before
                    # anything else: before the outcome's own pay, so the two
                    # pulse trains never overlap on the valve, and before the
                    # row is written, so its counts are final. Inside the
                    # tracker's recording segment, so the eye data covers the
                    # last drop's whole delivery. A no-op for a task that
                    # does not ask for reward mid-trial.
                    self._engine.settle_rewards(ctx)
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
                # The system fault this trial was lost to — the display
                # dropped frames, or the eye tracker stopped before its outcome
                # was decided — or None. Such a trial is not the subject's
                # fault: it is flagged (the row's `fault`, written by the
                # engine), served again (its outcome is non-completed, below),
                # paid anyway, and held against nobody (the failure streak
                # further down, and the training criteria).
                fault = result.lost_to_fault

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
                #
                # Paid on what the subject's response earned, not on the
                # outcome the scheduler sees. They differ on one kind of trial
                # only: one frame QA recycled into DROPPED_FRAMES because the
                # display dropped frames. That trial is still served again
                # (its measurement is discarded), but the subject did the
                # trial and was shown its feedback, and a display fault must
                # never cost them the reward — nor go unrecorded as a
                # NO_REWARD when the response did not pay. A task cannot fix
                # that by paying DROPPED_FRAMES: that would pay a recycled
                # wrong answer too.
                #
                # A trial the eye tracker cut short has no response to pay
                # for, so `fault` routes it to the task's fault reward
                # (RewardPolicy.on_fault) instead — see RewardPayer.deliver.
                reward_failed = self._payer.deliver(ctx, result.response_outcome, fault=fault)
                if fault is not None:
                    # After the pay, so the line can say what was paid.
                    self._log_fault_trial(attempt, result, fault, pay_failed=reward_failed)
                # A mid-trial drop that failed takes the same pause flow as
                # an end-of-trial failure. Held until now rather than
                # stopping the trial: the measurement was still being made,
                # and it is recorded below before a human looks at the pump.
                if ctx.record.get("n_mid_trial_reward_failures", 0) > 0:
                    reward_failed = True

                record = result.record
                if outcome.name != PAUSED.name:
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
                failure_streak = self._failure_streak(outcome, fault=fault)
                # Counted here too, for the same reason: before any branch
                # below can `continue` past it.
                dropout_streak = self._dropout_streak(outcome, result.record)

                if outcome.name == PAUSED.name or reward_failed:
                    # A reward failure goes through the same pause flow as a
                    # deliberate pause: a human has to look at the pump before
                    # the session carries on rewarding nothing. The
                    # measurement is already recorded above — a hardware fault
                    # after the fact must never discard a trial the subject
                    # actually completed.
                    fault = "REWARD FAILURE — check the pump" if reward_failed else None
                    if not self._pauses.handle(result.record, fault=fault):
                        break
                    continue  # the pause menu already gave all the time needed; skip ITI

                if dropout_streak is not None:
                    # Before the subject's failure streak: a device failing
                    # trial after trial is the rig, and the thing to look at
                    # first. The count starts again from here, so resuming on
                    # a tracker that was fixed gets a whole new run of chances
                    # and one that was not pauses again after as many.
                    heading = dropout_streak.heading()
                    self._streaks.dropout_pause_raised()
                    if not self._pauses.handle(result.record, fault=heading):
                        break
                    continue

                if failure_streak is not None:
                    # The budget written as frame QA writes it (the same rule
                    # as the log line in _failure_streak): "7.5%", never a
                    # rounded "8%".
                    fault = failure_streak.heading(
                        threshold_percent(self._frame_monitor.dropped_fraction_budget)
                    )
                    if not self._pauses.handle(result.record, fault=fault):
                        break
                    continue

                if self._iti_s > 0:
                    self._wait(self._iti_s)
        finally:
            self._teardown(file_handler, snapshot_written=snapshot_written)

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

    def _log_trial(self, attempt: int, record: dict[str, Any], outcome: Outcome) -> None:
        """One line per trial: the backbone a session log is read by."""
        detail = ""
        if record.get("abort_reason"):
            detail = f" ({record['abort_reason']})"
        elif record.get("frame_qa_reason"):
            detail = f" (was {record.get('outcome_before_frame_qa')}: {record['frame_qa_reason']})"
        elif record.get("fault", NO_FAULT) != NO_FAULT:
            # Neither an abort nor a recycle, yet a fault on the row: a device
            # stopped during the closing phase, after the measurement. The
            # engine flagged it and let the outcome stand (core/engine.py).
            detail = f" (fault {record['fault']} during its closing phase — the outcome stands)"
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
        # for the new stage. The runner's payer pays from its own reference,
        # so it has to re-read it here — otherwise the pump keeps delivering
        # the previous stage's amount while every row stamps the new scale,
        # and the data claims a reward that was never given.
        self._payer.policy = self._training.reward_policy
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

    def _log_fault_trial(
        self, attempt: int, result: TrialResult, fault: str, *, pay_failed: bool
    ) -> None:
        """The WARNING a trial lost to a system fault gets: which trial, what
        failed, what the subject was paid, and that the trial is served again.

        One line per such trial, beside the trial's own INFO line, because
        this is what an experimenter reading session.log afterwards needs in
        one place: a trial that failed through no fault of the subject's, and
        was not held against them. WARNING because the rig failed — a display
        dropping frames or a tracker dropping out, several times in a
        session, is the rig to fix before the next one.
        """
        cause = _FAULT_CAUSES.get(fault, f"a device health check failed ({fault})")
        # What the device said about it, when it said anything — the row's
        # fault_detail — so the lab can tell a pulled cable from a Host PC
        # abort from this line alone.
        detail = result.record.get("fault_detail")
        log.warning(
            "trial %d attempt %d: %s%s — a system fault, not the subject's. %s. Flagged "
            "fault=%s; the condition will be served again, and the trial is not counted "
            "against the subject.",
            self._trial_index,
            attempt,
            cause,
            f" ({detail})" if detail else "",
            self._payer.describe_fault_pay(result, fault, pay_failed),
            fault,
        )

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
        return self._pauses.handle({}, rest=f"BLOCK {done} OF {total} COMPLETE — REST")

    def _failure_streak(self, outcome: Outcome, fault: str | None) -> FailureStreak | None:
        """Count this trial toward the subject's failure streak (the rules are
        StreakMonitor.count_failure's); the streak, logged, on the trial that
        reaches the task's limit, which the caller turns into a pause.

        ``fault`` is the system fault the trial was lost to
        (``TrialResult.lost_to_fault``), or None.
        """
        streak = self._streaks.count_failure(
            outcome, fault=fault, display_failing=self._display_was_failing()
        )
        if streak is None:
            return None
        if streak.display_trials:
            log.warning(
                "%d trials in a row not completed, the last %s on trial %d, and %d of them "
                "dropped more than %s of their frames: pausing. The display was failing "
                "through this streak, and a panel missing vsyncs causes real fixation breaks "
                "— check the display before recalibrating.",
                streak.count,
                outcome.name,
                self._trial_index,
                streak.display_trials,
                # The budget exactly as configured. Whole percents wrote a
                # 7.5% budget as "8%", which the trials counted here (each over
                # 7.5%) need not have dropped.
                threshold_percent(self._frame_monitor.dropped_fraction_budget),
            )
        else:
            log.warning(
                "%d trials in a row not completed, the last %s on trial %d: pausing. The "
                "subject may not be seeing what this session is measuring — a calibration "
                "that passed but sits at the edge of the fixation window looks exactly like "
                "this.",
                streak.count,
                outcome.name,
                self._trial_index,
            )
        return streak

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

    def _dropout_streak(self, outcome: Outcome, record: dict[str, Any]) -> DropoutStreak | None:
        """Count this trial toward the device's dropout streak (the rules are
        StreakMonitor.count_dropout's); the streak, logged, on every trial from
        ``max_consecutive_dropouts`` on until its pause is raised."""
        streak = self._streaks.count_dropout(outcome, record)
        if streak is None:
            return None
        log.warning(
            "a device failed its health check (%s) on %d trials in a row, the last trial %d%s: "
            "pausing so the device can be checked. Every one of them that the fault cut short "
            "is served again.",
            streak.fault,
            streak.count,
            self._trial_index,
            f" — {streak.detail}" if streak.detail else "",
        )
        return streak

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
        return self._pauses.handle({}, fault="TRACKER NOT CALIBRATED — press C to calibrate")

    def _start_tracker_trial(self, ctx: TrialContext, attempt: int) -> None:
        """Open the tracker's recording segment and refresh its operator
        overlay, before the trial's first frame."""
        if self._tracker is None:
            return
        self._tracker.start_trial(ctx.trial_index, f"attempt {attempt}")
        self._tracker.draw_host_overlay(host_overlay_shapes(self._screen, ctx.regions))

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

    def _teardown(
        self, file_handler: logging.FileHandler | None, *, snapshot_written: bool
    ) -> None:
        """Release every device and write everything the session produced.

        ``file_handler`` is None when the session failed before session.log
        was attached; every other step still runs.

        ``snapshot_written`` is False when the session failed writing its
        config snapshot, the first file a run writes. Nothing then says what
        the run directory was for and `load_run` cannot read it, so it is not
        a run: every device is still released, but nothing is written into it
        — no data files, no manifest, no saved dashboard, no database row, and
        the tracker is not handed a destination for its recording, which
        holds no trial. A curriculum hands its task back, but the subject's
        training state is not saved for a session that never started. The
        directory stays as the build left it, so a run number whose folder is
        still empty can be used again (data/paths.py).
        """
        errors: list[Exception] = []

        def step(name: str, fn: Callable[[], None]) -> None:
            try:
                fn()
            except Exception as e:  # logged loudly + collected, never swallowed
                log.exception("teardown step %r failed", name)
                errors.append(e)

        def record_step(name: str, fn: Callable[[], None]) -> None:
            # A step that records the session — into its run directory, the
            # subject's training state or the database mirror. Only for a
            # session whose snapshot was written; see the docstring.
            if snapshot_written:
                step(name, fn)

        # First, before any other step can fail: the log's own account of how
        # the session ended is worth more than a step's failure message, and
        # a log that simply stops is what this line exists to prevent. A step
        # like every other, so if even this line cannot be written, the data
        # below still is.
        step("log.session_end", self._log_session_end)
        if not snapshot_written:
            # Said once, so an experimenter who finds the folder empty knows
            # it was left that way on purpose.
            log.warning(
                "the config snapshot was never written, so %s is not a run: the devices are "
                "released and nothing is written into it",
                self._paths.run_dir,
            )

        # Before the recorder writes: a trial cut short by a quit or a fault
        # left the loop before its between-trials settle, and its drops'
        # completions belong in events.csv like any other.
        if self._last_ctx is not None:
            last_ctx = self._last_ctx
            step("reward.settle", lambda: self._engine.settle_rewards(last_ctx))
        record_step("recorder.write", self._recorder.write)
        # Its own step, and early: a subject's place in its curriculum is
        # weeks of work, and must be written even if something later in
        # teardown fails.
        if self._training is not None:
            training = self._training
            # Wrapped rather than passed directly: save() returns the path it
            # wrote, and a teardown step returns nothing.
            record_step("training.save", lambda: (training.save(), None)[1])
            # A Task instance can outlive this session. The supervisor mutated
            # it stage by stage, so handing it back untouched is what stops a
            # second session from treating this one's last stage as its base.
            step("training.restore_base", training.restore_base)
        record_step("paradigm.summary", self._write_paradigm_summary)
        record_step("frames.save", lambda: self._frame_monitor.save(self._paths.frames_path))
        # The live analysis finishes BEFORE the final dashboard publish (so
        # the saved dashboard shows the flushed, final maps), before the
        # spike source closes (finishing drains it one last time), and
        # before the manifest is written (so what it saves is hashed).
        if self._live is not None:
            live = self._live
            record_step("live.finish", lambda: live.finish(self._paths.run_dir))
        if self._dashboard is not None:
            terminal = self._terminal_status(errors)
            dashboard = self._dashboard
            final_state: dict[str, Any] = {}

            def publish_final() -> None:
                # Complete state, not the capped one: what lands in figures/
                # is the record of the session, and it is written once.
                final_state.update(
                    self._publish_dashboard(terminal, f"Session {terminal}.", full=True)
                )

            # A step, not a bare call: building the final state asks the live
            # analysis and the eye tracker for their panels, and a device that
            # died mid-session can fail right here. Unguarded, that failure
            # skipped every step below it — the tracker's recording, the
            # manifest, closing the window.
            record_step("dashboard.publish", publish_final)
            # Saved only when the final state was built. When it was not, that
            # failure is already logged and collected, and an earlier, capped
            # state saved in its place would pose as the session's record.
            if final_state:
                record_step(
                    "dashboard.save", lambda: dashboard.save(self._paths.figures_dir, final_state)
                )
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
            # None when the snapshot was never written: released all the same,
            # with nothing delivered into a directory that is not a run.
            recording_path = (
                self._paths.run_dir / f"{self._paths.base}.edf" if snapshot_written else None
            )
            step("tracker.shutdown", lambda: tracker.shutdown(recording_path))
        if sync is not None:
            step("sync.close", sync.close)
        if reward is not None:
            step("reward.close", reward.close)
        # Close the log file before the manifest hashes it, so session.log's
        # recorded hash covers its complete contents. None when the session
        # failed before the log was attached: there is nothing to close.
        if file_handler is not None:
            handler = file_handler
            step("log.close", lambda: self._detach_file_logging(handler))
        record_step(
            "manifest.write",
            lambda: write_manifest(self._paths.run_dir, self._paths.manifest_path),
        )
        if self._database is not None:
            database = self._database
            status = self._terminal_status(errors)
            record_step(
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
