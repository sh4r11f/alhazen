"""The trial vocabulary: outcomes, phases, regions, inputs, and the context
threaded through every frame.

This is the generalization at the heart of alhazen. Rather than the engine
hard-coding one experiment's phases and outcomes, the *experiment* declares
its outcomes and composes its phases, and the engine relies only on the
small contracts defined in this module.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import numpy as np

from alhazen.config.models import RewardPulses
from alhazen.core.clock import Clock
from alhazen.display.screen import Screen, within_radius
from alhazen.errors import RewardRequestError

# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """A terminal trial result, as the experiment defines it.

    ``completed`` is the one flag the framework itself interprets: a
    completed trial produced its measurement and consumes a scheduled
    repetition; a non-completed one must be re-served by the scheduler
    (paradigms/base.py). ``success`` drives feedback/reward policy and is
    None for outcomes where the notion doesn't apply.
    """

    name: str
    completed: bool
    success: bool | None = None


# Framework-reserved outcomes, produced by the engine (never by a phase):
# all are non-completed by definition. PAUSED and ABORTED ended the trial
# before its measurement existed; PAUSED additionally writes no trials row
# (the runner enforces that split; see session/runner.py). ABORTED has two
# causes, told apart by the row's ``abort_reason``: the experimenter's skip
# key (``skipped_by_user``), or a device health check that failed mid-trial
# (the check's own reason — ``tracker_stopped`` when the eye tracker stopped
# recording). DROPPED_FRAMES is different in kind: the trial ran to its own
# end, but the display dropped more frames than the rig's frame QA allows
# (display/frames.py, policy ``recycle_trial``), so what the subject saw was
# not the stimulus the config describes and the measurement is discarded. The
# trial's own outcome is kept on the record as ``outcome_before_frame_qa``.
#
# A health-check abort and DROPPED_FRAMES are SYSTEM FAULTS: the rig failed,
# not the subject. See NO_FAULT below for how a row says so.
PAUSED = Outcome("PAUSED", completed=False)
ABORTED = Outcome("ABORTED", completed=False)
DROPPED_FRAMES = Outcome("DROPPED_FRAMES", completed=False)
_RESERVED_OUTCOMES = {"PAUSED": PAUSED, "ABORTED": ABORTED, "DROPPED_FRAMES": DROPPED_FRAMES}


# The values of the ``fault`` column: which system fault — the rig failing,
# never the subject — hit a trial. The engine writes it on EVERY row, NO_FAULT
# on a trial nothing happened to, for the reason ``n_dropped_frames`` is 0
# rather than absent on a clean trial: an empty cell reads back as NaN, and
# "no fault" has to be a value a reader can select on, never the absence of
# one. The string "none" rather than an empty one for the same reason.
#
# Exactly two faults are recognised — the failures that are the rig's, never
# the subject's. What the session does about a trial lost to one is
# session/runner.py's business.
#
# - FAULT_DROPPED_FRAMES: frame QA recycled the trial into DROPPED_FRAMES.
# - FAULT_TRACKER_STOPPED: the eye tracker stopped recording. It is the
#   reason the session's tracker health check reports
#   (session/builder.py); the engine writes whatever reason a failed health
#   check gives, and that check is the only one there is.
#
# Which fault a row names and whether it cost the trial its measurement are
# two questions — see lost_to_fault.
NO_FAULT = "none"
FAULT_DROPPED_FRAMES = "dropped_frames"
FAULT_TRACKER_STOPPED = "tracker_stopped"


@dataclass(frozen=True)
class HealthFault:
    """What a device health check reports when it fails.

    Two fields for two readers. ``reason`` is the fault itself, from a fixed
    vocabulary — ``FAULT_TRACKER_STOPPED`` for the eye tracker — and is what
    the row's ``fault`` (and, when the trial is aborted, ``abort_reason``)
    says: the value an analysis selects on. ``detail`` is the device's own
    account in words — which signal fired, how long the samples had been
    stale, what the device answered when asked — and goes to the row's
    ``fault_detail`` and to the session log, for the person telling a pulled
    cable from a Host PC abort. Free text, never a value to select on: the
    wording is the backend's and may change.

    A health check may still return a bare reason string instead; the engine
    reads that as a HealthFault with nothing more said (core/engine.py).
    """

    reason: str
    detail: str | None = None


def lost_to_fault(outcome_name: str, record: Mapping[str, Any]) -> str | None:
    """The system fault that cost a trial its measurement, or None.

    A trial is lost to a fault in exactly two ways, and its row says which:

    - frame QA recycled it: the outcome is ``DROPPED_FRAMES`` (and the row's
      ``fault`` is ``"dropped_frames"``);
    - a device health check aborted it before its outcome was decided — the
      eye tracker stopped recording: the outcome is ``ABORTED`` and the row's
      ``abort_reason`` is the same reason its ``fault`` names
      (``"tracker_stopped"``). The engine writes both from the one failed
      check, which is what ties the abort to the fault.

    Everything else is None, including two rows that do name a fault:

    - the experimenter's skip is ``ABORTED`` for its own reason
      (``skipped_by_user``), even on a trial whose tracker had already
      stopped during its closing phase;
    - a trial whose tracker stopped only during its closing phase — after its
      measurement, while feedback was on screen — keeps its own outcome: the
      fault is flagged on the row, but it cost the trial nothing.

    It reads only what a trials.csv row holds, so an analysis can apply the
    same rule offline: ``lost_to_fault(row["outcome"], row)``. A row written
    before the ``fault`` column existed can only be recognised as a
    dropped-frames loss.
    """
    if outcome_name == DROPPED_FRAMES.name:
        return FAULT_DROPPED_FRAMES
    fault = record.get("fault")
    if (
        outcome_name == ABORTED.name
        and fault not in (None, "", NO_FAULT)
        and record.get("abort_reason") == fault
    ):
        return str(fault)
    return None


# The columns the framework itself writes into a trial record, as against the
# ones an experiment's own build_trial and score put there.
#
# Named here because they are a *cross-repo* contract. An analysis in another
# package reads them out of trials.csv, and when it types the name itself
# nothing tells it that it asked for `dropped_frames` while alhazen writes
# `n_dropped_frames`. That happened, in an experiment whose dropped-frame
# exclusion therefore matched no trial for its whole life, with a fixture
# written in the same wrong name keeping its suite green. Importing this is
# how a reader stops guessing.
#
# Not every column is on every row: `abort_reason` only on an abort,
# `rewarded` only where a pump is wired and a delivery was attempted, the three
# `n_mid_trial_*` counts only for a task that declares mid-trial reward, the
# two frame-QA columns only on a recycled trial, `fault_detail` only where a
# health check said what failed, `success` only where the
# outcome defines one. `fault` IS on every row. Every emitted event also
# mirrors its time as `t_<event name lowercased>`, which is a pattern rather
# than a fixed name and so is not listed.
#
# tests/unit/test_contracts.py drives real trials through the engine and the
# runner and checks the names they produce against this tuple, so a rename at
# a write site that misses this list fails there rather than downstream.
TRIAL_RECORD_COLUMNS: tuple[str, ...] = (
    "trial_index",
    "attempt",
    "outcome",
    "completed",
    "success",
    "abort_reason",
    "n_dropped_frames",
    "outcome_before_frame_qa",
    "frame_qa_reason",
    "rewarded",
    # Mid-trial reward (TrialContext.request_reward): of the drops a phase
    # asked for during this trial, how many the pump delivered, how many it
    # failed, and how many a manual reward cancelled before they started.
    # Together they are every drop commanded — each one's REWARD is in the
    # event record. Written — as 0 on a trial that asked for none — on every
    # trial of a task declaring ``mid_trial_reward``, and absent otherwise, so
    # a zero is "none this trial" and never "not a mid-trial task".
    "n_mid_trial_rewards",
    "n_mid_trial_reward_failures",
    "n_mid_trial_rewards_cancelled",
    # "success" or "failure": what the subject was told at the end of the
    # trial (task/phases TrialFeedback). Beside the outcome, never derived
    # from it, because the two are different questions — a saccade that
    # missed is still a completed, scored measurement.
    "feedback",
    # The system fault that hit the trial — "dropped_frames" or
    # "tracker_stopped" — or "none", on every row (see NO_FAULT). The one
    # column that says a trial failed, or was flagged, because the rig did;
    # whether that cost the trial its measurement is `lost_to_fault`.
    "fault",
    # What the failed health check said about the fault `fault` names, in
    # the device's words (HealthFault.detail): "no new sample from the
    # EyeLink for 57 ms (limit 50 ms); the Host PC reports recording ended
    # (isRecording 3, ABORT_EXPT) ...". Only on a row whose fault a health
    # check reported with a detail — a dropped-frames row carries its account
    # in `frame_qa_reason` instead. Read by a person, never selected on.
    "fault_detail",
)


_OUTCOME_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class OutcomeSet:
    """An experiment's declared outcomes plus the reserved ones, with
    attribute access (``outcomes.CORRECT``) for phase code."""

    def __init__(self, declared: dict[str, Outcome]) -> None:
        for name in declared:
            if name in _RESERVED_OUTCOMES:
                raise ValueError(f"outcome name {name!r} is reserved by alhazen")
            if not _OUTCOME_NAME_RE.match(name):
                raise ValueError(
                    f"outcome name {name!r} must be UPPER_SNAKE_CASE (it becomes a "
                    f"trials.csv value and an attribute on this set)"
                )
        self._by_name = {**declared, **_RESERVED_OUTCOMES}
        for name, outcome in self._by_name.items():
            setattr(self, name, outcome)

    def __getattr__(self, name: str) -> Outcome:
        # Only reached for names __init__ never set — declared outcomes are
        # real attributes. Exists so static checkers accept OUTCOMES.CORRECT.
        raise AttributeError(name)

    def __getitem__(self, name: str) -> Outcome:
        return self._by_name[name]

    def __iter__(self):
        return iter(self._by_name.values())

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._by_name)


def outcomes(**declared: dict[str, Any]) -> OutcomeSet:
    """Declare an experiment's outcomes:

    OUTCOMES = outcomes(
        CORRECT=dict(completed=True, success=True),
        FIX_BREAK=dict(completed=False),
        MISSED_TARGET=dict(completed=True, success=False),
    )
    """
    built = {}
    for name, spec in declared.items():
        unknown = set(spec) - {"completed", "success"}
        if unknown:
            raise ValueError(f"outcome {name!r}: unknown keys {sorted(unknown)}")
        if "completed" not in spec:
            raise ValueError(f"outcome {name!r} must declare completed=True/False")
        built[name] = Outcome(name=name, **spec)
    return OutcomeSet(built)


# ---------------------------------------------------------------------------
# Regions and per-frame inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CircleRegion:
    """A named screen region in centered px. The None rule is the blink rule:
    an unverifiable position (track loss, blink) is *outside every region* —
    fixation is only credited when it can actually be verified."""

    center: tuple[float, float]
    radius: float

    def contains(self, point: tuple[float, float] | None) -> bool:
        if point is None:
            return False
        return within_radius(point, self.center, self.radius)


@dataclass(frozen=True)
class InputFrame:
    """This frame's input snapshot, fetched once per frame by the engine so
    phases never touch a device.

    ``gaze`` is centered px (y-up) or None (unverifiable — the blink rule).
    ``keys`` are the subject's key presses since the last frame, and
    ``wheel`` the scroll-wheel movement over that frame (an adjustment
    task's knob). Fields only ever get appended, with defaults, so a phase
    or a test that cares about one of them is unaffected by the others.

    ``gaze_t`` is when the tracker took the sample behind ``gaze``, on the
    session clock — None whenever ``gaze`` is None. Display frames and
    tracker samples do not arrive in step: a frame that brings no new sample
    repeats the previous one, *with the same* ``gaze_t``. That is how a phase
    tells a new sample from a repeat (a speed computed across a repeat is a
    false zero), and the spacing between two new samples is the real time
    between them, not the nominal frame period.
    """

    gaze: tuple[float, float] | None = None
    keys: tuple[str, ...] = ()
    wheel: float = 0.0
    gaze_t: float | None = None


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------


class PhaseAction:
    """Per-frame instruction back to the engine. A phase's ``on_frame``
    returns CONTINUE, ADVANCE, or an Outcome that ends the whole trial."""

    CONTINUE = "CONTINUE"
    ADVANCE = "ADVANCE"


@runtime_checkable
class Phase(Protocol):
    """One step of a trial's state machine. Deliberately "dumb": a phase only
    reads/mutates the TrialContext and queues events via
    ``ctx.emit_on_flip`` — it never sees hardware, the bus, or the window.
    That separation is what makes phase logic testable with a fake clock and
    no display."""

    name: str

    def on_enter(self, ctx: TrialContext) -> None: ...

    def on_frame(self, ctx: TrialContext) -> str | Outcome: ...


# ---------------------------------------------------------------------------
# Mid-trial reward
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RewardRequest:
    """One juice drop a phase asked for while the trial runs.

    Built by ``TrialContext.request_reward`` with ``frame`` unset; the engine
    fills ``frame`` in when it hands the request to the dispenser, right
    after the flip that follows the request — the frame the drop was
    commanded on, which is what the REWARD event and the completion that
    follows it both carry.
    """

    pulses: RewardPulses
    reason: str
    frame: int | None = None

    def payload(self) -> dict[str, Any]:
        """The fields every event about this request carries, so a REWARD and
        the REWARD_DELIVERED, REWARD_FAILED or REWARD_CANCELLED that follows
        it can be matched up in events.csv by ``frame`` and ``reason``."""
        return {
            "pulses": self.pulses.model_dump(mode="json"),
            "reason": self.reason,
            "frame": self.frame,
        }


@dataclass(frozen=True)
class RewardCompletion:
    """How one handed-over request ended — one of three ways:

    - delivered: ``error`` and ``cancelled_by`` are both None;
    - failed: ``error`` is the failure's message;
    - cancelled: ``cancelled_by`` names what cancelled it before it reached
      the valve. Today that is only ``"manual"``: a manual reward overrode
      the queue (devices/reward.py, ``QueuedReward.deliver_manual``). It was
      never delivered, and it is not a failure — the pump was never asked.

    Crosses to the session thread from the reward worker (or, for a
    cancellation, from the thread that asked for the manual reward), so it is
    immutable and carries only plain data — never the exception object, whose
    traceback the worker has already logged."""

    request: RewardRequest
    error: str | None = None
    cancelled_by: str | None = None


# ---------------------------------------------------------------------------
# The context
# ---------------------------------------------------------------------------


@dataclass
class TrialContext:
    """Everything a phase may touch, built fresh per trial by the runner.

    ``record`` accumulates the trial's row for trials.csv; ``stimuli`` and
    ``regions`` are the task's named drawables and named windows; ``extras``
    is task-private scratch that never reaches the data files.
    """

    clock: Clock
    screen: Screen
    rng: np.random.Generator
    trial_index: int
    params: dict[str, Any]
    stimuli: dict[str, Any] = field(default_factory=dict)
    regions: dict[str, CircleRegion] = field(default_factory=dict)
    record: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)
    inputs: InputFrame = field(default_factory=InputFrame)
    dt: float = 1 / 60  # duration of the previously-shown frame; set by the engine
    pending_flip_events: list[tuple[str, dict]] = field(default_factory=list)
    # How the trial ended, set by the engine before it runs a closing phase
    # (one declaring ``must_be_last``) and None everywhere else. A closing
    # phase runs whatever the trial ended as — trial feedback has to be able
    # to say "that one did not count" on a fixation break — so it needs to
    # see what happened, and the record does not carry the outcome until the
    # trial is finalized, which is after every phase has run.
    outcome: Outcome | None = None
    # Mid-trial reward requests queued this frame, handed to the dispenser by
    # the engine after the next flip — the same queue-then-drain shape as
    # pending_flip_events, and for the same reason: a phase never touches
    # hardware.
    pending_reward_requests: list[RewardRequest] = field(default_factory=list)
    # Set by the engine at the start of every trial: True only when the
    # session's task declared ``mid_trial_reward`` and a dispenser is wired to
    # take the requests. False makes request_reward raise.
    accepts_reward_requests: bool = False

    def emit_on_flip(self, name: str, payload: dict | None = None) -> None:
        """Queue an event to be emitted right after the next flip, stamped
        with the flip's time — the photon-honest timestamp for anything
        visual. The engine drains this queue; phases never emit directly."""
        self.pending_flip_events.append((name, payload or {}))

    def request_reward(self, pulses: RewardPulses, reason: str) -> None:
        """Ask for a juice drop now, mid-trial — from a phase's ``on_frame``
        (or ``on_enter``).

        Only queues. After the next flip the engine hands the request to the
        session's reward dispenser, whose worker thread delivers it without
        blocking the frame loop, and emits REWARD stamped with that flip and
        carrying ``{pulses, reason, frame}``. REWARD_DELIVERED or
        REWARD_FAILED follows when the pump is done — or REWARD_CANCELLED,
        when the experimenter's manual reward overrode the queue before the
        drop reached the valve. ``reason`` is the task's own label
        ("pursuit_hold", "end_bonus") and comes back on every one of those
        events.

        Raises RewardRequestError when the task never declared
        ``mid_trial_reward = True`` — never ignored, because a task that
        believes it is paying and a subject who is not is a silent training
        failure — and for a request that could not open the valve.
        """
        if not self.accepts_reward_requests:
            raise RewardRequestError(
                f"a phase requested a mid-trial reward ({reason!r}), but this session's task "
                f"does not declare `mid_trial_reward = True`. Declare it on the Task class "
                f"(next to `reward`) so the session is built with a reward worker — and "
                f"refused on a rig that has no dispenser."
            )
        if not isinstance(pulses, RewardPulses):
            raise RewardRequestError(
                f"request_reward takes a RewardPulses, got {type(pulses).__name__} ({pulses!r})"
            )
        if pulses.n_pulses < 1 or pulses.pulse_ms < 1:
            # A zero-pulse request would be logged as a drop the valve never
            # opened for. Refused here, where the task's arithmetic went
            # wrong, rather than recorded as a REWARD that delivered nothing.
            raise RewardRequestError(
                f"request_reward({pulses!r}) delivers nothing: n_pulses and pulse_ms must be >= 1"
            )
        if not isinstance(reason, str) or not reason:
            raise RewardRequestError(
                f"request_reward needs a non-empty reason string, got {reason!r}: it is "
                f"what tells the drops apart in events.csv"
            )
        self.pending_reward_requests.append(RewardRequest(pulses=pulses, reason=reason))
