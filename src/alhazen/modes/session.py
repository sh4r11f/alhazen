"""run, test and simulate: one code path, three sets of arguments.

The whole value of a rehearsal is that it rehearses *this* session. A test
mode that built its session differently — its own wiring, its own scheduler,
its own trial builder — would be a second implementation of the experiment,
and the day it drifted from the first is the day it stopped being a
rehearsal. So all three modes go through ``build_session`` with the same task
and the same rig, and differ in exactly three ways:

============  ===============  ==================  ====================
mode          trial counts     who is in the chair  where data lands
============  ===============  ==================  ====================
``run``       as configured    a subject            the rig's data_root
``test``      reduced          a subject            the rehearsal root
``simulate``  reduced          the task's autopilot the rehearsal root
============  ===============  ==================  ====================

Everything else — the phases, the stimuli, the scheduler, the recorder, the
snapshot, the analysis that reads it afterwards — is the same code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alhazen.config.models import RigConfig
from alhazen.errors import ConfigError
from alhazen.modes import Mode
from alhazen.modes.rehearsal import Reduction, rehearsal_root, shrink_params
from alhazen.modes.simulation import Simulation
from alhazen.session.runner import SessionRunner
from alhazen.task.task import Task


@dataclass
class ModeSession:
    """A built session, plus what the mode did to it.

    Returned unrun so the caller decides when to start — and so a test can
    check what a mode *would* do without sitting through a session.
    """

    mode: Mode
    runner: SessionRunner
    data_root: Path
    run: int
    reductions: list[Reduction] = field(default_factory=list)
    simulation: Simulation | None = None
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        """What is about to happen, for the experimenter to read before it
        does. Every line is something they might want to stop and change."""
        lines = [f"mode: {self.mode.value} — {self.mode.summary}"]
        if not self.mode.writes_real_data:
            lines.append(f"data: {self.data_root}  (NOT the rig's data root)")
        else:
            lines.append(f"data: {self.data_root}")
        for reduction in self.reductions:
            lines.append(f"reduced: {reduction}")
        if self.mode is Mode.TEST and not self.reductions:
            # Said out loud: otherwise an experimenter who asked for a short
            # run and got a full-length one has no way to know why.
            lines.append(
                "reduced: nothing — this experiment already specifies one "
                "repetition per condition, so a test run is a full-length one"
            )
        if self.simulation is not None and self.simulation.describe:
            for key, value in self.simulation.describe.items():
                lines.append(f"autopilot: {key}={value}")
        lines.extend(self.notes)
        return "\n".join(lines)


def next_run(data_root: Path | str, subject: str, session: int) -> int:
    """The next unused run number for this subject and session.

    The run directories ARE the record, so they are what is counted: a
    counter file that disagreed with them is what would eventually overwrite
    a session's data. Both experiment packages had grown their own identical
    copy of this before it lived here.
    """
    session_dir = Path(data_root) / f"sub-{subject}" / f"ses-{session:03d}"
    if not session_dir.exists():
        return 1
    taken = []
    for path in session_dir.glob("run-*"):
        try:
            taken.append(int(path.name.split("_")[0].split("-")[1]))
        except (IndexError, ValueError):
            continue
    return max(taken, default=0) + 1


def _real_devices(rig: RigConfig) -> list[str]:
    """The rig's devices that drive real hardware.

    ``mouse_sim`` and ``scripted`` trackers are not hardware, and neither is
    a simulated pump; a laptop rig configured with them is exactly what a
    simulated session is for.
    """
    simulated = {"simulated", "mouse_sim", "scripted", "none"}
    found = []
    for name in ("eyetracker", "reward", "sync", "recording"):
        device = getattr(rig.devices, name)
        if device is not None and device.backend not in simulated:
            found.append(f"{name} ({device.backend})")
    return found


def build_mode_session(
    mode: Mode,
    *,
    rig: RigConfig,
    task: Task,
    subject: str,
    session: int,
    run: int | None = None,
    seed: int | None = None,
    n_per_condition: int = 1,
    max_adaptive_trials: int = 10,
    windowed: bool = False,
    sources: dict[str, str] | None = None,
    instructions: str | None = None,
    curriculum: Any = None,
    dashboard: bool | None = None,
    open_dashboard: bool | None = None,
    build_session: Callable[..., SessionRunner] | None = None,
    **extra: Any,
) -> ModeSession:
    """Wire one session in the given mode.

    ``build_session`` is injectable only so tests can watch what this passes
    down without opening a window; production always gets the real one.
    """
    if not mode.runs_trials:
        raise ValueError(f"{mode.value} does not run trials — see alhazen.modes.{mode.value}")
    if build_session is None:
        from alhazen.session.builder import build_session as _real_build_session

        build_session = _real_build_session

    params = task.params
    reductions: list[Reduction] = []
    simulation: Simulation | None = None
    notes: list[str] = []

    if mode is not Mode.RUN:
        params, reductions = shrink_params(
            params,
            n_per_condition=n_per_condition,
            max_adaptive_trials=max_adaptive_trials,
        )
        # The task is rebuilt around the reduced params rather than mutated:
        # a Task validates its params in __init__, so going through the
        # constructor is what proves the reduced design is still runnable.
        if reductions:
            task = type(task)(params)

    if mode is Mode.SIMULATE:
        hardware = _real_devices(rig)
        if hardware:
            raise ConfigError(
                f"simulate mode refuses this rig: it configures real hardware "
                f"({', '.join(hardware)}). Driving a real rig with an invented subject "
                f"writes a run directory full of numbers that look exactly like a session "
                f"and are not one. Point --rig at a simulated config."
            )
        simulation = task.simulation(seed if seed is not None else 0)
        if simulation is None or simulation.is_empty():
            raise ConfigError(
                f"simulate mode needs {type(task).__name__}.simulation() to return the "
                f"stand-ins for a subject (see alhazen.modes.simulation.Simulation); it "
                f"returned nothing. A gaze-contingent task with no simulated gaze ends "
                f"every trial NO_FIXATION, which is re-served, so the session never ends."
            )
        if simulation.task is not None:
            task = simulation.task

    data_root = rig.data_root if mode.writes_real_data else rehearsal_root(rig.data_root)
    if data_root != rig.data_root:
        rig = rig.model_copy(update={"data_root": data_root})
    run_number = run if run is not None else next_run(data_root, subject, session)

    runner = build_session(
        rig=rig,
        subject=subject,
        session=session,
        run=run_number,
        task=task,
        curriculum=curriculum,
        seed=seed,
        iti=getattr(params, "iti", None),
        windowed=windowed,
        sources=sources,
        instructions=instructions,
        dashboard=dashboard,
        open_dashboard=open_dashboard,
        tracker=simulation.tracker if simulation else None,
        response=simulation.response if simulation else None,
        # A simulated session has nobody to press SPACE at the instructions.
        auto_start=mode is Mode.SIMULATE,
        **extra,
    )
    return ModeSession(
        mode=mode,
        runner=runner,
        data_root=data_root,
        run=run_number,
        reductions=reductions,
        simulation=simulation,
        notes=notes,
    )
