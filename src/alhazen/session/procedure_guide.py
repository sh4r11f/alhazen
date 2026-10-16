"""The live monitor's guide to a running eye-tracker procedure: what the
operator reads while the subject's screen shows nothing but targets.

Since round 4 the calibration, validation and drift-correction screens carry
no text (user decision, 2026-10-15), so the operator's instructions travel to
the live monitor instead. They used to arrive as one status string
(``"calibrating: target 2 of 5 · eyes: both tracked"``) that only a person
could read. This module turns the ``(stage, detail)`` progress reports the
trackers and procedures already make into a structured record the page draws
as a panel — a heading, the instruction, the eye line, the step — and turns
each procedure's result into how the panel ends::

    tracker / procedures ── hook(stage, detail) ──▶ progressed()
    EyeTrackerMonitor    ── result ───────────────▶ ended_calibration() …
                                                        │
                       state["procedure"] ◀── as_state() ┘   (null when none)

Pure functions over frozen records: nothing here reads a device, a clock or
the live monitor. ``EyeTrackerMonitor`` (session/eyetracker.py) holds the
current record and decides when it is published and when it is dropped.

The reports it reads, as the trackers make them:

=====================  =====================================================
stage                  detail
=====================  =====================================================
``calibration guide``  ``[<eye line> · ]press SPACE to start, …`` (TRACKPixx3,
                       mouse/sim) or ``press SPACE to open the Host PC setup,
                       ESC to skip`` (EyeLink)
``calibrating``        ``target i of n · <eye line>`` (TRACKPixx3, mouse/sim)
                       or ``on the Host PC's setup screen`` (EyeLink)
``camera image``       ``<Host PC caption> · shown on the live monitor``
                       (EyeLink, while its camera image is open)
``validating``         ``target i of n``
``drift correcting``   ``target 1 of 1``
``calibration failed`` ``the tracker reports no calibration`` (TRACKPixx3)
=====================  =====================================================

Any other stage is shown generically: its detail is the instruction.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal

from alhazen.devices.eyetracker.procedures import DriftResult, ValidationResult
from alhazen.devices.eyetracker.protocol import CalibrationResult

Kind = Literal["calibration", "validation", "drift_correction"]
# "done" is never published: a procedure that succeeds takes the panel down
# (the record becomes None), so the page has only these three to draw.
Outcome = Literal["running", "failed", "aborted"]

HEADINGS: dict[str, str] = {
    "calibration": "Calibration",
    "validation": "Validation",
    "drift_correction": "Drift correction",
}

# While a target is up, what the operator does depends on how the walk
# advances (``EyeTrackerConfig.calibration_advance``, or "auto" on a
# simulated display): in manual mode SPACE accepts the target (the
# TRACKPixx3 refuses it while no eye is in the image); in auto mode the walk
# takes the target once the gaze holds still.
TARGET_INSTRUCTIONS: dict[str, str] = {
    "manual": "Press SPACE when the subject is looking at the target",
    "auto": "Keep the subject looking at the target; it moves on by itself",
}

# What to check after a procedure that did not succeed, when its result's
# own note does not say (a note that does — "… — check the camera sees the
# eyes …" — is used instead: split_check). The keys named are the pause
# menu's (session/pause.py) and the startup request's.
FAILED_CHECKS: dict[str, str] = {
    "calibration": "Check the eye tracker and the subject's position, then calibrate again (C)",
    "validation": "Calibrate again (C), validate again (V), or go on with this calibration",
    "drift_correction": "Drift-correct again (D), or calibrate again (C)",
}
ABORTED_CHECKS: dict[str, str] = {
    "calibration": "Calibrate again (C) when ready",
    "validation": "Validate again (V) when ready",
    "drift_correction": "Drift-correct again (D) when ready",
}

# The drift correction's note for an ESC (procedures.drift_correct): a
# DriftResult has no ``aborted`` flag of its own.
DRIFT_ABORTED_NOTE = "aborted"

_TARGET = re.compile(r"^target (\d+) of (\d+)$")
# A note's "what to check" clause: "… — check …" or "…; check …".
_CHECK = re.compile(r"\s*(?:—|;)\s*(?=check\b)", re.IGNORECASE)
# The stages whose detail completes a sentence ("calibrating on the Host
# PC's setup screen"); any other stage's detail stands on its own.
_VERB_STAGES = frozenset({"calibrating", "validating", "drift correcting"})


def sentence(text: str) -> str:
    """``text`` with its first letter capitalised and no trailing full stop:
    the trackers write lower-case clauses for a status line."""
    text = text.strip().rstrip(".")
    return text[:1].upper() + text[1:]


def split_check(note: str) -> tuple[str, str | None]:
    """A result note as (what went wrong, what to check), splitting off a
    trailing "— check …" clause when the note has one."""
    parts = _CHECK.split(note, maxsplit=1)
    if len(parts) == 2 and parts[0].strip():
        return sentence(parts[0]), sentence(parts[1])
    return sentence(note), None


def _shouting(part: str) -> bool:
    """A status part opening in capitals is the tracker raising its voice —
    the TRACKPixx3's "NO EYE IN THE CAMERA IMAGE — check …" — and is shown
    as an alert rather than a plain eye line."""
    first = part.split(" ", 1)[0]
    return len(first) > 1 and first.isalpha() and first.isupper()


@dataclass(frozen=True)
class ProcedureGuide:
    """What the live monitor's procedure panel shows, as of ``t``.

    ``stage`` is the tracker's own word for where the procedure is;
    ``instruction`` the large line (what the operator does now);
    ``status_line`` the tracker's status (the eye line), ``status_alert``
    True when that line says something is wrong; ``step``/``n`` the target
    on screen, when the procedure walks targets; ``failure``/``check`` set
    once the procedure has failed or been stopped.
    """

    kind: Kind
    stage: str
    instruction: str
    t: float
    status_line: str | None = None
    status_alert: bool = False
    step: int | None = None
    n: int | None = None
    outcome: Outcome = "running"
    failure: str | None = None
    check: str | None = None

    @property
    def heading(self) -> str:
        return HEADINGS[self.kind]

    def as_state(self) -> dict[str, Any]:
        """The record as it travels in ``state["procedure"]``."""
        return {**asdict(self), "heading": self.heading}


def started(kind: Kind, t: float) -> ProcedureGuide:
    """A procedure that has just begun and reported nothing yet."""
    return ProcedureGuide(kind=kind, stage="starting", instruction="Starting", t=t)


def progressed(
    guide: ProcedureGuide, stage: str, detail: str, *, advance: str, t: float
) -> ProcedureGuide:
    """``guide`` after one progress report ``(stage, detail)``.

    The detail is read as `` · ``-separated parts: ``target i of n`` is the
    step, a part starting with "press" is the instruction, the rest is the
    tracker's status (the eye line). ``advance`` ("manual" or "auto") picks
    the instruction while a target is up and no key was named.
    """
    parts = [part.strip() for part in detail.split(" · ") if part.strip()]
    step: int | None = None
    n: int | None = None
    press: str | None = None
    rest: list[str] = []
    for part in parts:
        target = _TARGET.match(part)
        if target is not None and step is None:
            step, n = int(target.group(1)), int(target.group(2))
        elif press is None and part.lower().startswith("press "):
            press = part
        else:
            rest.append(part)
    outcome: Outcome = "running"
    failure: str | None = None
    if stage == "calibration failed":
        # The TRACKPixx3 says so a moment before its result: shown as the
        # failure at once; the result (ended_calibration) adds what to check.
        outcome, failure, instruction, rest = "failed", sentence(detail), "", []
    elif press is not None:
        instruction = sentence(press)
    elif step is not None:
        instruction = TARGET_INSTRUCTIONS.get(advance, TARGET_INSTRUCTIONS["manual"])
    elif stage == "camera image" and rest:
        # The EyeLink's camera image, open on the Host PC's setup screen and
        # mirrored to the live monitor: its caption is what is happening.
        instruction, rest = f"Camera image: {rest[0]}", rest[1:]
    elif detail.strip() == "starting":
        instruction, rest = "Starting", []
    elif stage in _VERB_STAGES and detail.strip():
        instruction, rest = sentence(f"{stage} {detail}"), []
    else:
        instruction, rest = sentence(detail or stage), []
    return replace(
        guide,
        stage=stage,
        instruction=instruction,
        t=t,
        status_line=" · ".join(
            sentence(part) if not part.startswith("eyes: ") else part for part in rest
        )
        or None,
        status_alert=any(_shouting(part) for part in rest),
        step=step,
        n=n,
        outcome=outcome,
        failure=failure,
        check=None,
    )


def _stopped(guide: ProcedureGuide, outcome: Outcome, note: str, t: float) -> ProcedureGuide:
    """``guide`` ended as failed or aborted, with the note as the failure and
    the note's own check, or the kind's usual one."""
    if outcome == "aborted":
        failure = sentence(note) if note else "Stopped before the end"
        check: str | None = ABORTED_CHECKS[guide.kind]
    else:
        failure, check = split_check(note) if note else ("Did not succeed", None)
        check = check or FAILED_CHECKS[guide.kind]
    return replace(guide, outcome=outcome, failure=failure, check=check, instruction="", t=t)


def ended_calibration(guide: ProcedureGuide, result: CalibrationResult) -> ProcedureGuide | None:
    """The panel after a calibration: gone when it took (or its outcome is
    unknown, as on the stand-ins), kept with the failure otherwise."""
    if result.aborted:
        return _stopped(guide, "aborted", result.note, result.t)
    if result.ok is False:
        return _stopped(
            guide, "failed", result.note or "the tracker reports no calibration", result.t
        )
    return None


def ended_validation(guide: ProcedureGuide, result: ValidationResult) -> ProcedureGuide | None:
    """The panel after a validation: gone when it passed, kept otherwise."""
    if result.aborted:
        return _stopped(guide, "aborted", result.summary(), result.t)
    if result.accepted:
        return None
    return _stopped(guide, "failed", result.summary(), result.t)


def ended_drift(guide: ProcedureGuide, result: DriftResult) -> ProcedureGuide | None:
    """The panel after a drift correction: gone when it was applied, kept
    otherwise (aborted with ESC, no gaze, an offset too large to be drift)."""
    if result.applied:
        return None
    if result.note == DRIFT_ABORTED_NOTE:
        return _stopped(guide, "aborted", "drift correction stopped (ESC)", result.t)
    return _stopped(guide, "failed", result.summary(), result.t)


def failed_with_error(guide: ProcedureGuide, error: BaseException, t: float) -> ProcedureGuide:
    """The panel after a procedure that raised instead of returning a
    result: the error is the failure, so the panel never stays "running"."""
    return _stopped(guide, "failed", f"stopped with an error: {error}", t)
