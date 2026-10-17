"""The calibration request before trial 1, and the record of past calibrations.

Why this exists: a session on a real eye tracker used to start its trials on
whatever calibration the device already held. The runner only stopped when a
tracker could say it held *none* (``calibration_state``), and

- the EyeLink cannot say — its Host PC owns the calibration — so it was never
  asked about at all;
- the TRACKPixx3 keeps its calibration across runs in the device, so a model
  fitted yesterday, for another subject, read as "calibrated".

So by default a real tracker now gets a request before trial 1: **Calibrate
now** is the default (ENTER or C); reusing the previous calibration is a
second, explicit key (R), offered only when this rig's own record says the
previous calibration fits (same tracker, layout, area and screen geometry, the
same subject, the most recent one recorded, and — where the device can say —
still held); ESC cancels the session, once a second ESC confirms it. Whatever
is chosen goes on the record as a ``CALIBRATION_CHOICE`` event.

The live monitor answers it too: while the request is up the session
publishes ``waiting``, and the page's Calibrate, Reuse calibration (offered
when R is), Accept calibration (when A is) and Quit do what those keys do
(session/operator_commands.py ``offer``). The page's Quit was confirmed on the
page, so it cancels without the second ESC. An aborted or failed calibration brings the
request back; trials never start on the old model behind the operator's back.

What alhazen knows about a previous calibration is only what it recorded
itself: every calibration a session runs that the tracker reports as taken is
appended to ``<data_root>/calibrations/<rig>.jsonl`` (:class:`CalibrationLedger`)
— when, which tracker and layout, the screen geometry, the subject, and the
per-target errors where the tracker computes them. A device holding a model
alhazen did not record (another program's) is said to be of unknown origin,
never dressed up with a quality it does not have.

Stand-ins (the mouse, a scripted replay, simulate mode's autopilot) are never
asked: there is nothing real to calibrate.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alhazen.config.models import EyeTrackerConfig, MonitorConfig
from alhazen.devices.eyetracker.protocol import CalibrationResult
from alhazen.session.operator_commands import Offer, OperatorCommands

log = logging.getLogger(__name__)

# The backends that have a real eye to calibrate.
REAL_TRACKERS = frozenset({"eyelink", "viewpixx"})
# Keys, as PsychoPy names them.
CALIBRATE_KEYS = frozenset({"return", "enter", "num_enter", "c"})
REUSE_KEY = "r"
ACCEPT_UNKNOWN_KEY = "a"
CANCEL_KEYS = frozenset({"escape", "q"})
# Keys arriving this soon after the request appears are dropped: the key that
# launched the session, or a held one, must never answer it.
ARMING_S = 0.3
TITLE = "CALIBRATE BEFORE TRIALS"
# The live monitor's commands at the request, as the keys that mean the same.
# Quit is its own: confirmed on the page, it cancels without the second ESC.
PAGE_CANCEL = "live-monitor:quit"
PAGE_KEYS = {
    "calibrate": "c",
    "reuse_calibration": REUSE_KEY,
    "accept_calibration": ACCEPT_UNKNOWN_KEY,
    "quit": PAGE_CANCEL,
}
TITLE_COLOR = (1.0, 0.81, 0.2)

# What a calibration must match to be offered for reuse.
COMPATIBILITY_FIELDS = (
    "backend",
    "host_ip",
    "layout",
    "area",
    "width_px",
    "height_px",
    "width_cm",
    "distance_cm",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def asks_for_calibration(
    *, tracker_from_rig: bool, cfg: EyeTrackerConfig | None, attended: bool
) -> bool:
    """Whether a session gets the calibration request before trial 1: only
    the rig's own real tracker (an EyeLink or a TRACKPixx3, built from the
    rig file, not one handed in), and only with somebody at the keyboard to
    answer. A stand-in, simulate's autopilot or an unattended run keeps the
    older check (the runner's ``_require_tracker_calibration``)."""
    return tracker_from_rig and cfg is not None and cfg.backend in REAL_TRACKERS and attended


class CalibrationLedger:
    """An append-only JSON-lines file of the calibrations this rig took.

    One line per calibration the tracker reported as taken; an unreadable
    line (a write cut off by a power cut) is skipped with a warning, never a
    reason to refuse a session.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def append(self, entry: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True, default=str) + "\n")

    def last(self) -> dict[str, Any] | None:
        """The most recent record, or None when there is none — or when the
        newest line cannot be read: the record before it is then not the
        latest calibration, and must not be offered as if it were."""
        if not self.path.is_file():
            return None
        lines = [
            line for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        if not lines:
            return None
        try:
            value = json.loads(lines[-1])
        except ValueError as error:
            raise LedgerUnreadable(
                f"the newest line of {self.path} cannot be read ({error})"
            ) from error
        if not isinstance(value, dict):
            raise LedgerUnreadable(f"the newest line of {self.path} is not a record")
        return value


class LedgerUnreadable(ValueError):
    """The calibration ledger's newest line is not a record."""


def setup_of(cfg: EyeTrackerConfig, monitor: MonitorConfig) -> dict[str, Any]:
    """The facts a calibration is only valid for."""
    return {
        "backend": cfg.backend,
        "host_ip": cfg.host_ip if cfg.backend == "eyelink" else None,
        "layout": cfg.calibration_type,
        "area": cfg.calibration_area,
        "width_px": monitor.width_px,
        "height_px": monitor.height_px,
        "width_cm": monitor.width_cm,
        "distance_cm": monitor.distance_cm,
    }


def ledger_entry(
    result: CalibrationResult,
    cfg: EyeTrackerConfig,
    monitor: MonitorConfig,
    *,
    subject: str | None,
    session: int | None,
    run_dir: str | None,
) -> dict[str, Any]:
    """One calibration, as the ledger keeps it: setup, outcome, who, when."""
    errors = [
        e for t in result.targets for e in (t.left_error_deg, t.right_error_deg) if e is not None
    ]
    return {
        "time": _now(),
        **setup_of(cfg, monitor),
        "subject": subject,
        "session": session,
        "run_dir": run_dir,
        "ok": result.ok,
        "eye": result.eye,
        "advance": result.advance,
        "note": result.note,
        "n_targets": result.n_targets,
        "target_style": result.target_style,
        "mean_error_deg": sum(errors) / len(errors) if errors else None,
        "max_error_deg": max(errors) if errors else None,
    }


@dataclass(frozen=True)
class PreviousCalibration:
    """What is known about the calibration a session would start on, and
    whether it may be offered for reuse."""

    held: bool | None  # the device says it holds one; None: it cannot say
    record: dict[str, Any] | None  # alhazen's latest record for this rig
    reusable: bool
    reason: str  # why reuse is (not) offered, in the operator's words

    def lines(self) -> list[str]:
        held = {
            True: "the tracker reports it holds a calibration",
            False: "the tracker reports it holds NO calibration",
            None: (
                "the tracker cannot say whether it holds a calibration "
                "(an EyeLink's is on its Host PC)"
            ),
        }[self.held]
        out = [f"Device: {held}."]
        if self.record is None:
            out.append("Last calibration recorded on this rig: none.")
        else:
            r = self.record
            quality = (
                f"mean error {r['mean_error_deg']:.2f}°, worst {r['max_error_deg']:.2f}°"
                if r.get("mean_error_deg") is not None
                else "quality: not reported by this tracker"
            )
            out.append(
                f"Last calibration recorded on this rig: {r.get('time', 'time unknown')}, "
                f"subject {r.get('subject') or 'unknown'}, {r.get('layout')} "
                f"{r.get('eye') or ''}, {quality}."
            )
        out.append(self.reason)
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "held": self.held,
            "record": self.record,
            "reusable": self.reusable,
            "reason": self.reason,
        }


def previous_calibration(
    tracker: Any,
    cfg: EyeTrackerConfig,
    monitor: MonitorConfig,
    ledger: CalibrationLedger,
    subject: str | None,
) -> PreviousCalibration:
    """Read what the device says and what the ledger recorded, and decide
    whether reuse may be offered. Reuse needs every one of: a recorded
    calibration that took, for this subject, on this exact setup, as the
    most recent one recorded on this rig; and, for a device that can say,
    that it still holds one."""
    state = getattr(tracker, "calibration_state", None)
    held = bool(state()) if state is not None else None
    try:
        record = ledger.last()
    except LedgerUnreadable as error:
        return PreviousCalibration(
            held,
            None,
            False,
            f"Reuse is not offered: {error}, so the latest calibration is unknown.",
        )
    if held is False:
        return PreviousCalibration(
            held, record, False, "Reuse is not offered: the device holds none."
        )
    if record is None:
        return PreviousCalibration(
            held,
            None,
            False,
            "Reuse is not offered: alhazen has no record of the calibration the device holds, "
            "so neither whose it is nor how good it was is known.",
        )
    setup = setup_of(cfg, monitor)
    different = [k for k in COMPATIBILITY_FIELDS if record.get(k) != setup[k]]
    if different:
        return PreviousCalibration(
            held,
            record,
            False,
            f"Reuse is not offered: the last recorded calibration was for another setup "
            f"({', '.join(different)} differ).",
        )
    if record.get("ok") is not True:
        return PreviousCalibration(
            held,
            record,
            False,
            "Reuse is not offered: the last recorded calibration did not report success.",
        )
    if not subject or record.get("subject") != subject:
        return PreviousCalibration(
            held,
            record,
            False,
            "Reuse is not offered: the last recorded calibration was for another subject.",
        )
    caveat = "" if held else " The Host PC is assumed to still hold it; alhazen cannot check."
    return PreviousCalibration(
        held,
        record,
        True,
        f"R reuses it, recorded as a reuse (not a new calibration).{caveat}",
    )


@dataclass
class StartupCalibration:
    """The request before trial 1. :meth:`request` returns True to start
    the trials, False when the operator cancelled the session."""

    monitor: Any  # EyeTrackerMonitor
    tracker: Any
    cfg: EyeTrackerConfig
    screen_monitor: MonitorConfig
    ledger: CalibrationLedger
    subject: str | None
    session: int | None
    show: Callable[[str, str], None]
    poll_keys: Callable[[], list[str]]
    # The runner's session-event emitter, set by the runner (as it sets the
    # eye-tracker monitor's) before the request can be made.
    emit: Callable[[str, dict[str, Any]], None] | None = None
    wait: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.monotonic
    choices: list[str] = field(default_factory=list)
    # Asked before ESC or Q cancels the session (session/pause.py
    # QuitConfirmation.ask); None cancels at once.
    confirm_quit: Callable[[], bool] | None = None
    # The live monitor's commands and the runner's publisher, wired by the
    # runner when a live monitor is open; None without one.
    operator: OperatorCommands | None = None
    publish: Callable[[str, str | None], object] | None = None

    def _key(self) -> str:
        """The next key pressed after the request is armed — or the live
        monitor's command, as the key it stands for (``PAGE_KEYS``)."""
        armed_at = self.now() + ARMING_S
        while True:
            if self.operator is not None:
                answers = self.operator.take_offered()
                if answers:
                    return PAGE_KEYS[answers[0]]
            keys = self.poll_keys()
            if keys and self.now() >= armed_at:
                return keys[0]
            self.wait(0.01)

    def _offer(self, previous: PreviousCalibration, unknown: bool, message: str) -> None:
        """Tell the live monitor what the request answers to now, and publish
        it under ``waiting``. Re-offered every time the request is drawn:
        reuse and accept come and go with what the last calibration did."""
        if self.operator is None:
            return
        commands = {"calibrate": "calibrating now"}
        keys = {"calibrate": "return", "quit": "escape"}
        if previous.reusable:
            commands["reuse_calibration"] = "the previous calibration is reused; trials start"
            keys["reuse_calibration"] = REUSE_KEY
        if unknown:
            commands["accept_calibration"] = (
                "the calibration just run is accepted with an unknown outcome; trials start"
            )
            keys["accept_calibration"] = ACCEPT_UNKNOWN_KEY
        commands["quit"] = "the session is cancelled; nothing more is recorded"
        self.operator.offer(Offer("waiting", commands, keys, "the calibration request"))
        if self.publish is not None:
            line = "Calibration request on the subject's screen: Calibrate"
            if previous.reusable:
                line += ", Reuse calibration"
            if unknown:
                line += ", Accept calibration"
            line += " or Quit."
            self.publish("waiting", f"{message} {line}".strip() if message else line)

    def _body(self, previous: PreviousCalibration, message: str, unknown: bool) -> str:
        lines = ["Calibrate the eye tracker before the trials start.", ""]
        if message:
            lines += [message, ""]
        lines += previous.lines()
        lines += ["", "ENTER / C   calibrate now"]
        if unknown:
            lines.append("A           accept the calibration just run (outcome unknown)")
        if previous.reusable:
            lines.append("R           reuse the previous calibration")
        lines.append("ESC         cancel the session (nothing more is recorded)")
        return "\n".join(lines)

    def request(self) -> bool:
        self.poll_keys()  # drop whatever is buffered before the request shows
        previous = previous_calibration(
            self.tracker, self.cfg, self.screen_monitor, self.ledger, self.subject
        )
        message = ""
        unknown: CalibrationResult | None = None
        try:
            return self._ask(previous, message, unknown)
        finally:
            if self.operator is not None:
                self.operator.withdraw("the calibration request had been answered; not acted on")

    def _ask(
        self,
        previous: PreviousCalibration,
        message: str,
        unknown: CalibrationResult | None,
    ) -> bool:
        while True:
            self.show(TITLE, self._body(previous, message, unknown is not None))
            self._offer(previous, unknown is not None, message)
            key = self._key()
            if key in CALIBRATE_KEYS:
                result = self.monitor.calibrate()
                self.poll_keys()
                if result.aborted:
                    message = (
                        f"The calibration was aborted ({result.note}). Trials do not start "
                        "on the previous model: calibrate again, reuse it explicitly, or cancel."
                    )
                    unknown = None
                elif result.ok is False:
                    message = f"NOT calibrated: {result.note}. Calibrate again, or cancel."
                    unknown = None
                elif result.ok is None:
                    message = (
                        f"The calibration finished, but the tracker did not report whether it "
                        f"took ({result.note}). Calibrate again, or A to accept it as unknown."
                    )
                    unknown = result
                else:
                    return self._chosen("calibrated", previous, result)
                previous = previous_calibration(
                    self.tracker, self.cfg, self.screen_monitor, self.ledger, self.subject
                )
            elif key == ACCEPT_UNKNOWN_KEY and unknown is not None:
                return self._chosen("accepted with unknown outcome", previous, unknown)
            elif key == REUSE_KEY and previous.reusable:
                return self._chosen("reused previous", previous, None)
            elif key in CANCEL_KEYS and self.confirm_quit is not None and not self.confirm_quit():
                continue  # not confirmed: the request is drawn again
            elif key in CANCEL_KEYS or key == PAGE_CANCEL:
                self._chosen("cancelled", previous, None)
                return False

    def _chosen(
        self, choice: str, previous: PreviousCalibration, result: CalibrationResult | None
    ) -> bool:
        self.choices.append(choice)
        log.info("calibration before trial 1: %s", choice)
        if self.emit is None:
            raise RuntimeError("StartupCalibration.emit is wired by the session runner")
        self.emit(
            "CALIBRATION_CHOICE",
            {
                "choice": choice,
                "previous": previous.as_dict(),
                "result": result.summary() if result is not None else None,
            },
        )
        return True
