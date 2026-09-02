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

"Who is in the chair" is also what decides which of the rig's devices are
driven, and that is why the same rig file serves all three. A rig file
describes the machine; the mode decides what to do with it (``rig_for_mode``):

- ``run`` drives the rig exactly as written.
- ``test`` puts a person in the chair. On a rig with no tracker — a laptop —
  their mouse cursor stands in for gaze; ``--mouse`` asks for that on a rig
  whose tracker is switched off.
- ``simulate`` puts nobody in the chair, so it drives no hardware: the task's
  autopilot replaces the tracker, the pump and the sync lines are recorded
  rather than fired, and ``--headless`` takes the window away as well.

Every substitution is a line in ``describe()``, printed before trial one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alhazen.config.models import EyeTrackerConfig, RigConfig
from alhazen.errors import ConfigError
from alhazen.modes import Mode, flag_refusal
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
            # run and got a full-length one has no way to know why. Worded
            # for both ways this happens — a design already at one repetition
            # per cell, and a task that schedules its own trials (the RF
            # templates schedule per probe) with no SchedulerConfig to find.
            lines.append(
                "reduced: nothing — these parameters carry no reducible trial "
                "counts, so this run is full-length (pass --params with a "
                "smaller design to rehearse less)"
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


# Backends that are already stand-ins. Simulate mode leaves these alone: there
# is nothing to switch off, and the note it would print would be noise.
_STAND_INS = {"simulated", "mouse_sim", "scripted", "none"}


def rig_for_mode(
    mode: Mode, rig: RigConfig, *, headless: bool = False, mouse: bool = False
) -> tuple[RigConfig, list[str]]:
    """The rig as ``mode`` will drive it, and one line per thing it changed.

    Pure — the rig file is never rewritten, and the copy handed back is what
    ``build_session`` gets — so a test can ask what a mode would do to a rig
    without opening a window. The lines go into ``ModeSession.notes`` and are
    printed by ``describe()``, because every one of them is a device the
    experimenter configured and is not getting.
    """
    refusal = flag_refusal(mode, headless=headless, mouse=mouse)
    if refusal is not None:
        raise ConfigError(refusal)
    notes: list[str] = []
    devices = rig.devices
    display = rig.display
    dashboard = rig.dashboard

    if mode is Mode.SIMULATE:
        # Nobody is in the chair, so nothing that acts on a subject or reads
        # one is driven. Each stand-in is the same one a purely simulated rig
        # would have configured; what changes is that the rig file no longer
        # has to say so, because the mode already knows.
        if devices.eyetracker is not None and devices.eyetracker.backend not in _STAND_INS:
            notes.append(
                f"eyetracker: {devices.eyetracker.backend} stands down — "
                f"the task's autopilot supplies gaze"
            )
            devices = devices.model_copy(update={"eyetracker": None})
        if devices.reward is not None and devices.reward.backend not in _STAND_INS:
            notes.append(
                f"reward: {devices.reward.backend} stands down — deliveries are logged, not pumped"
            )
            devices = devices.model_copy(
                update={"reward": devices.reward.model_copy(update={"backend": "simulated"})}
            )
        if devices.sync is not None and devices.sync.backend not in _STAND_INS:
            notes.append(f"sync: {devices.sync.backend} stands down — pulses are logged, not fired")
            devices = devices.model_copy(
                update={"sync": devices.sync.model_copy(update={"backend": "simulated"})}
            )
        if devices.recording is not None and devices.recording.backend not in _STAND_INS:
            notes.append(
                f"recording: {devices.recording.backend} stands down — "
                f"the run is marked as having no recording attached"
            )
            devices = devices.model_copy(
                update={"recording": devices.recording.model_copy(update={"backend": "simulated"})}
            )
        # Spikes are dropped rather than simulated: a simulated spike source
        # needs a receptive field and a stimulus event to fire on, which only
        # an RF task declares. A task that wants simulated spikes configures
        # them in its rig as `simulated`, and that passes through untouched.
        if devices.spikes is not None and devices.spikes.backend not in _STAND_INS:
            notes.append(
                f"spikes: {devices.spikes.backend} stands down — no spike source in a "
                f"simulated session"
            )
            devices = devices.model_copy(update={"spikes": None})
        if headless:
            # No window, and no browser either: the dashboard still serves
            # its page, but whoever started this over ssh has no browser to
            # open it in, and CI has nobody to look.
            notes.append(
                "display: none (--headless) — no window opens, and the dashboard "
                "does not open a browser"
            )
            display = display.model_copy(update={"backend": "simulated"})
            dashboard = dashboard.model_copy(update={"auto_open": False})

    elif mode is Mode.TEST:
        # A person is in the chair. Their gaze has to come from somewhere,
        # and on a machine with no tracker the mouse cursor is that
        # somewhere — which needs a window to move the cursor over.
        if mouse and display.backend == "simulated":
            raise ConfigError(
                "--mouse needs a window for the cursor to move over, and this rig's "
                "display is simulated. Point --rig at a machine with a screen."
            )
        if mouse:
            was = (
                f"{devices.eyetracker.backend} switched off (--mouse)"
                if devices.eyetracker is not None
                else "this rig has none (--mouse)"
            )
            notes.append(f"eyetracker: the mouse cursor stands in for gaze — {was}")
            devices = devices.model_copy(
                update={"eyetracker": EyeTrackerConfig(backend="mouse_sim")}
            )
        elif devices.eyetracker is None:
            if display.backend == "simulated":
                notes.append(
                    "eyetracker: none — this rig has no tracker and no window for a "
                    "mouse cursor, so gaze is blank"
                )
            else:
                notes.append("eyetracker: the mouse cursor stands in for gaze — this rig has none")
                devices = devices.model_copy(
                    update={"eyetracker": EyeTrackerConfig(backend="mouse_sim")}
                )

    if devices is not rig.devices or display is not rig.display or dashboard is not rig.dashboard:
        rig = rig.model_copy(
            update={"devices": devices, "display": display, "dashboard": dashboard}
        )
    return rig, notes


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
    headless: bool = False,
    mouse: bool = False,
    build_session: Callable[..., SessionRunner] | None = None,
    **extra: Any,
) -> ModeSession:
    """Wire one session in the given mode.

    ``headless`` and ``mouse`` are the two flags that override the machine
    (see :func:`alhazen.modes.flag_refusal`); a mode that cannot honour one
    raises ``ConfigError`` before anything is wired.

    ``build_session`` is injectable only so tests can watch what this passes
    down without opening a window; production always gets the real one.
    """
    if not mode.runs_trials:
        raise ValueError(f"{mode.value} does not run trials — see alhazen.modes.{mode.value}")
    if build_session is None:
        from alhazen.session.builder import build_session as _real_build_session

        build_session = _real_build_session

    # The rig as this mode drives it — real hardware stood down for simulate,
    # the mouse standing in for a missing tracker in test — decided before
    # anything else, so a flag the mode refuses is refused first.
    rig, notes = rig_for_mode(mode, rig, headless=headless, mouse=mouse)

    params = task.params
    reductions: list[Reduction] = []
    simulation: Simulation | None = None

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
