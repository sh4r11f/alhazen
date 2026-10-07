"""Measure rig as a queue of selectable jobs: the contract, the registry and the runner.

``--mode measure`` used to run one fixed list (display, geometry, keys,
tracker, ruler). A rig makes more claims than that — its viewing distance,
its luminance response, its tracker's accuracy *and* precision, how much juice
a valve pulse actually delivers, whether the neural stream is arriving — and
an experimenter checks a different handful on different days. So each check
is a :class:`MeasurementJob`, the operator picks the ones to run (``--measure``,
or the dashboard's checkboxes), and :func:`run_jobs` runs exactly those, one
after another, into one report.

What each piece hides, and what callers may rely on:

- **A job** owns one physical claim: the devices it needs (``needs``), the
  job that must have *produced a result* earlier in the same run
  (``requires``), why it cannot run on this rig (``unavailable``, asked before
  anything opens), and how hardware and operator input become a
  :class:`~alhazen.modes.measure.Measurement`.
- **The runner** owns order, devices and honesty. Jobs run in their declared
  ``order`` — a stable sequence, never the order boxes were ticked — each
  device is opened once, the first time a job needs it, and every one is
  released at the end whatever happened. A job that cannot run is
  *unavailable*, one the operator stopped is *cancelled*, one whose
  prerequisite produced nothing is *blocked*, one that raised is an *error*;
  none of these is ever reported as passed.
- **The operator** is the person at the rig. A job that needs a number only a
  person can read (a tape measure, a photometer, a balance) asks through
  :class:`Operator`; a value given in advance with ``--measure-input`` is used
  instead, and nothing ever invents one. Arming hardware is never taken from
  ``--measure-input``: it is a person's decision at the rig.

Experiment packages add jobs without alhazen importing them: a package lists
a callable returning its jobs under the ``alhazen.measurements`` entry-point
group (:func:`installed_jobs`). Keys are namespaced (``kde.bead``), and a key
already taken is refused by name.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import platform
import socket
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any, Protocol

from alhazen.config.models import RigConfig
from alhazen.data.atomic import replace_atomically
from alhazen.display.screen import Screen
from alhazen.modes.measure import Measurement

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "alhazen.measurements"

# What a job can ask for. "operator" is a person at the rig who reads an
# instrument and types a number; "display" is the rig's real window.
RESOURCES = frozenset({"display", "keyboard", "mouse", "tracker", "reward", "spikes", "operator"})

# The states a job moves through and ends in. Only PASSED and MEASURED mean
# the job produced a result; nothing else is ever shown as green.
QUEUED, RUNNING, WAITING = "queued", "running", "waiting"
PASSED, FAILED, MEASURED = "passed", "failed", "measured"
UNAVAILABLE, CANCELLED, ERROR, BLOCKED = "unavailable", "cancelled", "error", "blocked"
FINAL_STATES = frozenset({PASSED, FAILED, MEASURED, UNAVAILABLE, CANCELLED, ERROR, BLOCKED})
RESULT_STATES = frozenset({PASSED, MEASURED})

REPORT_SCHEMA = 2
# A key: namespace, dot, name; lower case, digits, dashes. It is a command-line
# value, a JSON key and a file-name fragment, so nothing else gets in.
KEY_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-.")


class OperatorCancelled(Exception):
    """The operator stopped a job at a prompt (ESC, or declined to arm):
    this job is cancelled, the run goes on to the next one."""


class JobUnavailable(Exception):
    """Raised by a job that finds, once running, that it cannot measure on
    this rig (an acquisition that is not running, a driver that is not
    there). Its message is the reason the report shows."""


class Operator(Protocol):
    """The person at the rig, as a job sees them. ``ask_number`` raises
    OperatorCancelled on ESC and keeps asking until the value is in range."""

    def ask_number(self, key: str, prompt: str, *, unit: str, low: float, high: float) -> float: ...

    def confirm(self, key: str, prompt: str) -> bool: ...

    def tell(self, prompt: str) -> None: ...


def _always_available(_rig: RigConfig, _inputs: Mapping[str, str]) -> str | None:
    return None


@dataclass(frozen=True)
class MeasurementJob:
    """One measurement the operator can tick. See the module docstring."""

    key: str
    group: str
    title: str
    description: str
    order: int
    run: Callable[[JobContext], Measurement]
    needs: frozenset[str] = frozenset()
    requires: tuple[str, ...] = ()
    unavailable: Callable[[RigConfig, Mapping[str, str]], str | None] = _always_available
    # Who supplies it: "alhazen", or the experiment package's entry point.
    provider: str = "alhazen"
    # The --measure-input names it reads, "<name> (<unit>)", for the
    # dashboard and the docs: what an operator will be asked for.
    inputs: tuple[str, ...] = ()
    # Whether it measures the person or animal in the chair: "required" (a
    # calibration, an accuracy, a vergence check — refused without a subject
    # ID, recorded with it), "optional" (a person is needed, but it is the
    # machine's timing being measured: response keys) or "none".
    subject: str = "none"

    def __post_init__(self) -> None:
        if not self.key or set(self.key) - KEY_CHARS or "." not in self.key:
            raise ValueError(
                f"measurement key {self.key!r} must be '<namespace>.<name>' in lower case "
                "letters, digits and dashes"
            )
        if self.subject not in {"required", "optional", "none"}:
            raise ValueError(f"{self.key}: subject must be required, optional or none")
        unknown = set(self.needs) - RESOURCES
        if unknown:
            raise ValueError(f"{self.key}: unknown resources {sorted(unknown)}")

    def describe(self) -> dict[str, Any]:
        """What the dashboard lists: everything but the callables."""
        return {
            "key": self.key,
            "group": self.group,
            "title": self.title,
            "description": self.description,
            "order": self.order,
            "needs": sorted(self.needs),
            "requires": list(self.requires),
            "provider": self.provider,
            "inputs": list(self.inputs),
            "subject": self.subject,
        }


# ----------------------------------------------------------------------
# The registry
# ----------------------------------------------------------------------


def installed_jobs(
    entry_points: Callable[[], Iterable[Any]] | None = None,
) -> dict[str, MeasurementJob]:
    """alhazen's own jobs plus every installed package's, by key.

    A provider that fails to load, returns something that is not a job, or
    reuses a key is an error naming it: a measurement that silently vanished
    from the list would look like a rig with nothing to check.
    """
    from alhazen.modes.measure_builtin import builtin_jobs

    jobs: dict[str, MeasurementJob] = {job.key: job for job in builtin_jobs()}
    points = (
        entry_points()
        if entry_points is not None
        else metadata.entry_points(group=ENTRY_POINT_GROUP)
    )
    for point in sorted(points, key=lambda p: p.name):
        try:
            provided = list(point.load()())
        except Exception as error:
            raise ValueError(
                f"the measurement provider {point.name!r} ({point.value}) failed to load: "
                f"{type(error).__name__}: {error}"
            ) from error
        for job in provided:
            if not isinstance(job, MeasurementJob):
                raise ValueError(
                    f"the measurement provider {point.name!r} returned {job!r}, which is not "
                    "a MeasurementJob"
                )
            if job.key in jobs:
                raise ValueError(
                    f"the measurement provider {point.name!r} registers {job.key!r}, which "
                    f"{jobs[job.key].provider} already provides"
                )
            jobs[job.key] = job
    return jobs


def catalog(jobs: Mapping[str, MeasurementJob]) -> list[dict[str, Any]]:
    """The jobs as the dashboard lists them, in run order."""
    return [job.describe() for job in sorted(jobs.values(), key=lambda j: (j.order, j.key))]


def plan(jobs: Mapping[str, MeasurementJob], selected: Sequence[str]) -> list[MeasurementJob]:
    """The selected jobs in run order, or a ValueError saying what is wrong.

    Refused: an empty selection (running everything because nothing was
    chosen would be the opposite of what was asked), a key nobody provides,
    a key given twice, and a job whose prerequisite was not selected with it
    — a tracker accuracy with no calibration in the same run would be
    measured against whatever model the device happens to hold.
    """
    if not selected:
        raise ValueError("choose at least one measurement to run")
    seen: set[str] = set()
    for key in selected:
        if key in seen:
            raise ValueError(f"the measurement {key!r} is selected twice")
        seen.add(key)
    unknown = [key for key in selected if key not in jobs]
    if unknown:
        raise ValueError(
            f"no measurement called {', '.join(unknown)}; the measurements here are "
            f"{', '.join(sorted(jobs))}"
        )
    for key in selected:
        missing = [need for need in jobs[key].requires if need not in seen]
        if missing:
            raise ValueError(
                f"{key} needs {', '.join(missing)} in the same run: select it too "
                f"({jobs[key].title} is measured against what that one establishes)"
            )
    ordered = sorted((jobs[key] for key in selected), key=lambda j: (j.order, j.key))
    position = {job.key: index for index, job in enumerate(ordered)}
    for job in ordered:
        for need in job.requires:
            if position[need] > position[job.key]:
                raise ValueError(f"{job.key} is ordered before its prerequisite {need}")
    return ordered


def parse_inputs(pairs: Sequence[str]) -> dict[str, str]:
    """``--measure-input <key>.<name>=<value>`` pairs as a dict; refused when
    malformed or repeated, by name."""
    inputs: dict[str, str] = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        name = name.strip()
        if not sep or not name or not value.strip() or "." not in name:
            raise ValueError(f"--measure-input {pair!r}: write it as <measurement>.<input>=<value>")
        if name in inputs:
            raise ValueError(f"--measure-input {name} is given twice")
        inputs[name] = value.strip()
    return inputs


# ----------------------------------------------------------------------
# What a running job sees
# ----------------------------------------------------------------------


class Devices:
    """The rig's devices for one measurement run: each opened at most once,
    the first time a job asks, and every one released by :meth:`close`.

    ``factories`` map a resource name to a callable taking the run's
    ExitStack, opening the device, registering its release on the stack and
    returning it — injected, so the runner is tested with stand-ins and a
    real run opens what a session would (``measure_builtin.rig_devices``).
    """

    def __init__(self, factories: Mapping[str, Callable[[ExitStack], Any]]) -> None:
        self._factories = dict(factories)
        self._open: dict[str, Any] = {}
        self._stack = ExitStack()
        self._handed_back: set[str] = set()
        self.released: list[str] = []

    def get(self, name: str) -> Any:
        if name in self._open:
            return self._open[name]
        if name not in self._factories:
            raise JobUnavailable(f"this rig has no {name} for a measurement to use")
        device = self._factories[name](self._stack)
        self._open[name] = device
        return device

    def is_open(self, name: str) -> bool:
        return name in self._open

    def hand_back(self, name: str) -> Any:
        """Take a device out of the run's keeping: a job that has released it
        itself (ending the tracker to keep its recording) hands it back, and
        its registered release then does nothing. Returns the device."""
        device = self._open.pop(name)
        self._handed_back.add(name)
        self.released.append(name)
        return device

    def was_handed_back(self, name: str) -> bool:
        return name in self._handed_back

    def close(self) -> None:
        """Release everything opened, in reverse order, each one attempted."""
        try:
            self._stack.close()
        finally:
            self.released = [*self.released, *self._open]
            self._open.clear()


def check_number(name: str, value: float, low: float, high: float) -> None:
    """A reading is a finite number inside its plausible range, or refused."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name}: {value!r} is not a finite number")
    if not low <= value <= high:
        raise ValueError(f"{name}: {value:g} is outside {low:g} to {high:g}")


@dataclass
class JobContext:
    """Everything one job is handed. ``provided`` records every operator
    input it used, with its unit and where it came from."""

    rig: RigConfig
    rig_path: str
    screen: Screen
    devices: Devices
    operator: Operator
    inputs: Mapping[str, str]
    echo: Callable[[str], None]
    job: MeasurementJob
    output_dir: Path
    results: Mapping[str, JobRecord]
    set_waiting: Callable[[str | None], None]
    later_needs: frozenset[str] = frozenset()
    provided: dict[str, Any] = field(default_factory=dict)

    def keep_tracker_recording(self, destination: Path) -> Path:
        """End the tracker now and leave its native recording at
        ``destination`` (the tracker's ``shutdown`` contract: its stem, the
        backend's own suffix). Only for the last job in the run that uses the
        tracker; refused otherwise, since the next one would find it closed.
        Returns the directory the recording was left in."""
        if "tracker" in self.later_needs:
            raise RuntimeError(
                f"{self.job.key} cannot keep the tracker's recording: a later measurement in "
                "this run still needs the tracker"
            )
        tracker = self.devices.hand_back("tracker")
        destination.parent.mkdir(parents=True, exist_ok=True)
        tracker.shutdown(destination)
        return destination.parent

    def input(self, name: str) -> str | None:
        """A value given with --measure-input <job key>.<name>=<value>."""
        return self.inputs.get(f"{self.job.key}.{name}")

    def number(self, name: str, prompt: str, *, unit: str, low: float, high: float) -> float:
        """A number from --measure-input, else from the operator; refused
        when not finite or outside [low, high], so a mistyped reading never
        becomes a measurement."""
        given = self.input(name)
        if given is not None:
            try:
                value = float(given)
            except ValueError as error:
                raise ValueError(
                    f"--measure-input {self.job.key}.{name}={given!r} is not a number"
                ) from error
            source = "--measure-input"
        else:
            self.set_waiting(f"{prompt} ({unit})")
            try:
                value = self.operator.ask_number(
                    f"{self.job.key}.{name}", prompt, unit=unit, low=low, high=high
                )
            finally:
                self.set_waiting(None)
            source = "operator"
        check_number(f"{self.job.key}.{name}", value, low, high)
        self.provided[name] = {"value": value, "unit": unit, "source": source}
        return value

    def confirm(self, name: str, prompt: str) -> bool:
        """An explicit yes from the operator at the rig; ESC or no is False."""
        self.set_waiting(prompt)
        try:
            answer = bool(self.operator.confirm(f"{self.job.key}.{name}", prompt))
        finally:
            self.set_waiting(None)
        self.provided[name] = {"value": answer, "source": "operator"}
        return answer


# ----------------------------------------------------------------------
# The run and its report
# ----------------------------------------------------------------------


@dataclass
class JobRecord:
    """One job's line in the report and on the dashboard."""

    key: str
    title: str
    group: str
    state: str = QUEUED
    summary: str = ""
    ok: bool | None = None
    started: str | None = None
    finished: str | None = None
    waiting_for: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    measurement_name: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "group": self.group,
            "state": self.state,
            "ok": self.ok,
            "summary": self.summary,
            "started": self.started,
            "finished": self.finished,
            "waiting_for": self.waiting_for,
            "measurement": self.measurement_name,
            "detail": self.detail,
        }


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_of(measurement: Measurement) -> str:
    """PASSED/FAILED for a measurement with a right answer, MEASURED for one
    without: a distribution is a fact about the rig, not a pass."""
    return {True: PASSED, False: FAILED, None: MEASURED}[measurement.ok]


@dataclass
class JobsReport:
    """The whole run: what was selected, what each job did, and enough
    provenance to recompute every number from the report alone."""

    rig_path: str
    selected: list[str]
    records: list[JobRecord]
    provenance: dict[str, Any]
    stopped: bool = False
    report_path: str | None = None

    @property
    def ok(self) -> bool:
        """True only when every selected job produced a result and none failed."""
        return not self.stopped and all(r.state in RESULT_STATES for r in self.records)

    def as_dict(self) -> dict[str, Any]:
        measurements = [
            {"name": r.measurement_name or r.title, "ok": r.ok, "summary": r.summary, **r.detail}
            for r in self.records
            if r.state in {PASSED, FAILED, MEASURED}
        ]
        return {
            # The schema-1 fields first, as an older reader expects them:
            # "rig", "ok", and the measurements that produced a result.
            "rig": self.rig_path,
            "ok": self.ok,
            "measurements": measurements,
            "schema": REPORT_SCHEMA,
            "selected": self.selected,
            "stopped": self.stopped,
            "jobs": [r.as_dict() for r in self.records],
            "provenance": self.provenance,
        }

    def render(self) -> str:
        marks = {
            PASSED: "OK  ",
            MEASURED: "--  ",
            FAILED: "FAIL",
            ERROR: "ERR ",
            UNAVAILABLE: "N/A ",
            CANCELLED: "STOP",
            BLOCKED: "SKIP",
        }
        lines = [f"rig measurements — {self.rig_path}", ""]
        for r in self.records:
            lines.append(f"{marks.get(r.state, '    ')} {r.title} [{r.state}]: {r.summary}")
        for r in self.records:
            notes = r.detail.get("notes") or []
            if notes:
                lines += ["", f"{r.title}:"] + [f"  {n}" for n in notes]
        return "\n".join(lines)

    def save(self, path: Path) -> Path:
        """Write the report, never over an earlier one: a rig that drifted is
        only visible by comparing two of these."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        target, index = path, 1
        while True:
            try:
                handle = target.open("x", encoding="utf-8")
                break
            except FileExistsError:
                target = path.with_name(f"{path.stem}-{index}{path.suffix}")
                index += 1
        self.report_path = str(target)
        with handle:
            handle.write(json.dumps(self.as_dict(), indent=2, default=_json_default))
        return target


def _json_default(value: Any) -> Any:
    if hasattr(value, "tolist"):
        return value.tolist()
    return str(value)


def provenance(
    rig_path: str, selected: Sequence[str], inputs: Mapping[str, str], argv: Sequence[str] | None
) -> dict[str, Any]:
    """Who measured what, with which code, against which rig file."""
    rig_file = Path(rig_path)
    digest = hashlib.sha256(rig_file.read_bytes()).hexdigest() if rig_file.is_file() else None
    from alhazen.version import dependency_version, get_version

    return {
        "alhazen": get_version(),
        "psychopy": dependency_version("psychopy"),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "host": socket.gethostname(),
        "rig_file": str(rig_file),
        "rig_sha256": digest,
        "selected": list(selected),
        "inputs": dict(inputs),
        "argv": list(argv) if argv is not None else None,
        "started": _stamp(),
        "pid": os.getpid(),
    }


class StatusFile:
    """The dashboard's view of a running measurement: the queue as JSON,
    rewritten atomically after every change. ``None`` writes nothing."""

    def __init__(self, path: Path | None) -> None:
        self._path = path

    def write(self, report: JobsReport, current: str | None) -> None:
        if self._path is None:
            return
        payload = {
            "schema": REPORT_SCHEMA,
            "current": current,
            "done": sum(r.state in FINAL_STATES for r in report.records),
            "total": len(report.records),
            "stopped": report.stopped,
            "report": report.report_path,
            "jobs": [
                {
                    **{k: v for k, v in r.as_dict().items() if k != "detail"},
                    "notes": list(r.detail.get("notes") or [])[:4],
                }
                for r in report.records
            ],
            "updated": _stamp(),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        replace_atomically(self._path, json.dumps(payload, indent=2, default=_json_default))


def run_jobs(
    rig: RigConfig,
    rig_path: str,
    ordered: Sequence[MeasurementJob],
    *,
    devices: Devices,
    operator: Operator,
    inputs: Mapping[str, str] | None = None,
    output_dir: Path,
    status: StatusFile | None = None,
    echo: Callable[[str], None] = print,
    argv: Sequence[str] | None = None,
    clock: Callable[[], float] = time.monotonic,
    on_finish: Callable[[JobsReport], None] | None = None,
    subject: str | None = None,
) -> JobsReport:
    """Run the planned jobs in order and return the report.

    Every job runs even after another fails, as the old driver did: whoever
    came to check a rig wants the whole picture from one visit. Two
    exceptions, both by design: a job whose prerequisite produced no result
    is BLOCKED (an accuracy on a calibration that failed is not a
    measurement), and a stop (Ctrl-C; the dashboard's Stop run) cancels the
    job running and every one still queued. Devices are released in every
    case, and the report keeps whatever finished. ``on_finish`` sees the
    report after the devices are released (where it is saved).
    """
    inputs = dict(inputs or {})
    status = status or StatusFile(None)
    screen = Screen.from_monitor(rig.monitor)
    selected = [job.key for job in ordered]
    records = {job.key: JobRecord(job.key, job.title, job.group) for job in ordered}
    report = JobsReport(
        rig_path=rig_path,
        selected=selected,
        records=list(records.values()),
        provenance=provenance(rig_path, selected, inputs, argv),
    )
    # The rig as it was resolved (a file that `extends` a shared rig is only
    # half of it), so every geometry and device setting a number rests on is
    # in the report itself.
    report.provenance["rig_config"] = rig.model_dump(mode="json")
    # Who was in the chair, for the measurements of a subject; the report is
    # the only place it goes (no participant record is written or read).
    report.provenance["subject"] = subject
    needing = [job.key for job in ordered if job.subject == "required"]
    if needing and not subject:
        raise ValueError(f"{', '.join(needing)} measure the subject in the chair: give --sub")
    # Unavailable before anything opens: a job that cannot run on this rig
    # is said up front, and no device is ever opened for it.
    for job in ordered:
        reason = job.unavailable(rig, inputs)
        if reason:
            records[job.key].state = UNAVAILABLE
            records[job.key].summary = reason
    status.write(report, None)
    try:
        for index, job in enumerate(ordered):
            record = records[job.key]
            if record.state == UNAVAILABLE:
                echo(f"{job.title}: unavailable — {record.summary}")
                continue
            blocked = [need for need in job.requires if records[need].state not in RESULT_STATES]
            if blocked:
                record.state = BLOCKED
                record.summary = (
                    f"not run: {', '.join(records[b].title for b in blocked)} produced no "
                    "result in this run"
                )
                status.write(report, None)
                continue

            def waiting(prompt: str | None, record: JobRecord = record) -> None:
                record.state = WAITING if prompt else RUNNING
                record.waiting_for = prompt
                status.write(report, record.key)

            later = frozenset().union(*(j.needs for j in ordered[index + 1 :]))
            ctx = JobContext(
                rig=rig,
                rig_path=rig_path,
                screen=screen,
                devices=devices,
                operator=operator,
                inputs=inputs,
                echo=echo,
                job=job,
                output_dir=output_dir,
                results=records,
                set_waiting=waiting,
                later_needs=later,
            )
            record.state, record.started = RUNNING, _stamp()
            status.write(report, job.key)
            echo(f"[{index + 1}/{len(ordered)}] {job.title}...")
            started = clock()
            try:
                measurement = job.run(ctx)
            except OperatorCancelled as stop:
                record.state = CANCELLED
                record.summary = str(stop) or "cancelled by the operator at a prompt"
            except JobUnavailable as why:
                record.state = UNAVAILABLE
                record.summary = str(why)
            except KeyboardInterrupt:
                record.state = CANCELLED
                record.summary = "the run was stopped while this was measuring"
                raise
            except Exception as error:  # one job's fault is its own result, not the run's end
                log.exception("measurement %s failed", job.key)
                record.state = ERROR
                record.summary = f"{type(error).__name__}: {error}"
            else:
                record.state = state_of(measurement)
                record.ok = measurement.ok
                record.summary = measurement.summary
                record.measurement_name = measurement.name
                record.detail = dict(measurement.detail)
            finally:
                if ctx.provided:
                    record.detail.setdefault("operator_inputs", ctx.provided)
                record.detail.setdefault("duration_s", round(clock() - started, 3))
                record.finished = _stamp()
                record.waiting_for = None
                status.write(report, None)
    except KeyboardInterrupt:
        report.stopped = True
        for record in report.records:
            if record.state in {QUEUED, RUNNING, WAITING}:
                record.state = CANCELLED
                record.summary = record.summary or "not run: the run was stopped"
        echo("stopped: the measurements that finished are kept in the report")
    finally:
        try:
            devices.close()
        finally:
            report.provenance["finished"] = _stamp()
            report.provenance["released"] = list(devices.released)
            if on_finish is not None:
                on_finish(report)
            status.write(report, None)
    return report
