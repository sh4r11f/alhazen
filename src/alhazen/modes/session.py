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

- ``run`` drives the rig exactly as written — and refuses a development rig,
  one whose settings say ``real_data: false``, before anything is written.
- ``test`` puts a person in the chair. On a rig with no tracker — a laptop —
  their mouse cursor stands in for gaze; ``--mouse`` asks for that on a rig
  whose tracker is switched off.
- ``simulate`` puts nobody in the chair, so it drives no hardware: the task's
  autopilot replaces the tracker, the pump and the sync lines are recorded
  rather than fired, and ``--headless`` takes the window away as well.

Every substitution is a line in ``describe()``, printed before trial one.

What the subject reads before trial one is the task's own
(``Task.instructions``) in all three modes, shown by ``build_session``. Run
mode alone also checks that the task has *said* — text, or None on purpose —
and warns when it has not (:func:`build_mode_session`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alhazen.config.experiment import Experiment, session_experiment
from alhazen.config.models import EyeTrackerConfig, RewardHwConfig, RigConfig
from alhazen.config.rigs import RigRef
from alhazen.core.rng import resolve_seed
from alhazen.data import naming
from alhazen.data.paths import session_dir
from alhazen.errors import ConfigError
from alhazen.modes import Mode, flag_refusal, real_data_refusal
from alhazen.modes.rehearsal import Reduction, rehearsal_root, shrink_params
from alhazen.modes.simulation import Simulation
from alhazen.session.runner import SessionRunner
from alhazen.task.subject_kind import SubjectKind, subject_kind_of
from alhazen.task.task import Task, declares_instructions

log = logging.getLogger(__name__)


@dataclass
class ModeSession:
    """A built session, plus what the mode did to it.

    Returned unrun so the caller decides when to start — and so a test can
    check what a mode *would* do without sitting through a session.
    """

    mode: Mode
    runner: SessionRunner
    # The UNversioned root: the rig's data_root, or its rehearsal sibling.
    # The run itself sits under `v<version>/` inside it (see `experiment`);
    # participants.tsv and the experiment database sit here, at the top.
    data_root: Path
    run: int
    reductions: list[Reduction] = field(default_factory=list)
    simulation: Simulation | None = None
    notes: list[str] = field(default_factory=list)
    # The experiment and version the run is filed under. None only for a
    # ModeSession built by hand; build_mode_session always sets it.
    experiment: Experiment | None = None

    def describe(self) -> str:
        """What is about to happen, for the experimenter to read before it
        does. Every line is something they might want to stop and change."""
        lines = [f"mode: {self.mode.value} — {self.mode.summary}"]
        if not self.mode.writes_real_data:
            lines.append(f"data: {self.data_root}  (NOT the rig's data root)")
        else:
            lines.append(f"data: {self.data_root}")
        if self.experiment is not None:
            # Which version folder the run goes in, and where that number came
            # from: a version nobody bumped files a changed protocol with the
            # old one's data, and this is the last moment to notice.
            lines.append(
                f"experiment: {self.experiment.name} {self.experiment.version} — filed under "
                f"{naming.version_dirname(self.experiment.version)}/ (version from "
                f"{self.experiment.version_source})"
            )
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


def next_run(data_root: Path | str, subject: str, session: int, *, experiment_version: str) -> int:
    """The next unused run number for this subject and session, within one
    version of the experiment.

    The run directories ARE the record, so they are what is counted: a
    counter file that disagreed with them is what would eventually overwrite
    a session's data. Both experiment packages had grown their own identical
    copy of this before it lived here, and so had the CLI.

    Counted inside the version's folder (``v<version>/sub-.../ses-...``),
    because that is where the run will be made: a new version starts its
    sessions at run 1, and runs recorded before alhazen 2.0 (directly under
    the data root) are in no version's folder and never counted.
    ``experiment_version`` is required for the same reason `SessionPaths`
    requires it — a default would count one folder and write another.

    One past the HIGHEST number taken, not the first gap: filling a gap would
    reuse a number an experimenter's notes may already refer to. The folder
    is `data.paths.session_dir` and the names are read by `alhazen.data.naming`,
    the one definition of the layout, so what is counted here is what
    `SessionPaths` creates.
    """
    folder = session_dir(data_root, experiment_version, subject, session)
    if not folder.exists():
        return 1
    taken = [naming.parse_run_dirname(path.name) for path in folder.glob("run-*")]
    # None is a folder that only looks like a run ("run-notes"): not a number.
    return max((run for run in taken if run is not None), default=0) + 1


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
    live_monitor = rig.live_monitor

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
            # No window, and no browser either: the live monitor still serves
            # its page, but whoever started this over ssh has no browser to
            # open it in, and CI has nobody to look.
            notes.append(
                "display: none (--headless) — no window opens, and the live monitor "
                "does not open a browser"
            )
            display = display.model_copy(update={"backend": "simulated"})
            live_monitor = live_monitor.model_copy(update={"auto_open": False})

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

    if (
        devices is not rig.devices
        or display is not rig.display
        or live_monitor is not rig.live_monitor
    ):
        rig = rig.model_copy(
            update={"devices": devices, "display": display, "live_monitor": live_monitor}
        )
    return rig, notes


def _named_rig(sources: dict[str, str] | None) -> RigRef | None:
    """The rig the caller's ``sources`` name — the file, its name and whose it
    is, as the command line records them — for a refusal to name; None when
    they do not name it (a rig built in code, a caller passing no sources)."""
    sources = sources or {}
    path, name, source = sources.get("rig"), sources.get("rig_name"), sources.get("rig_source")
    if path is None or name is None or source not in ("experiment", "alhazen"):
        return None
    return RigRef(name, Path(path), "alhazen" if source == "alhazen" else "experiment")


def _stand_in_reward(mode: Mode, rig: RigConfig, task: Task, notes: list[str]) -> RigConfig:
    """What this mode does with the reward line, said as a note.

    A human session never opens it (task/subject_kind.py): said here, so the
    session's setup lines show it on every rig that has one. A task that pays
    through the line — one that asks for reward mid-trial, or a monkey
    session — is refused by ``build_session`` on a rig with no dispenser,
    because a real session would run a subject through trials it believes
    are paid. A rehearsal pays nobody, so test and simulate modes stand a
    simulated dispenser in — every delivery is still decided, logged and
    recorded — and say so with a note, like every other substitution. Run
    mode is left alone: there the refusal is the point.
    """
    kind = subject_kind_of(task.params)
    if kind is SubjectKind.HUMAN:
        if rig.devices.reward is not None:
            notes.append(
                "reward: closed — subject_kind is human, so the rig's reward line is never "
                "opened and no trial or key pays"
            )
        return rig
    if mode is Mode.RUN or rig.devices.reward is not None:
        return rig
    if task.mid_trial_reward:
        notes.append(
            "reward: simulated — this task asks for reward mid-trial and the rig has no "
            "dispenser, so drops are logged, not pumped"
        )
    elif kind is SubjectKind.MONKEY:
        notes.append(
            "reward: simulated — subject_kind is monkey and the rig has no dispenser, so "
            "deliveries are logged, not pumped"
        )
    else:
        return rig
    devices = rig.devices.model_copy(update={"reward": RewardHwConfig(backend="simulated")})
    return rig.model_copy(update={"devices": devices})


def undeclared_instructions_warning(task_class: type) -> str:
    """The WARNING a run-mode session logs for a task that never said what its
    subject reads, naming the two ways to say it.

    A function so the wording lives in one place: the tests, the docs and the
    session all mean this text.
    """
    name = task_class.__name__
    return (
        f"{name} does not declare instructions(), so this run shows the subject "
        f"nothing before trial one. Implement {name}.instructions(self) -> str | None: "
        f"return the text the subject should read, or return None to declare that "
        f"this task has none (an animal subject, say), which also silences this warning."
    )


# How long simulate mode's break between blocks waits for somebody before it
# resumes by itself. A rehearsal on a real display has a keyboard wired, so
# the break used to wait for a SPACE that nobody watching a dry run had a
# reason to press, and the run sat on the rest screen. Ten seconds is long
# enough to read the screen, or to press a key and keep the pause, and short
# enough that a rehearsal finishes by itself.
SIMULATION_REST_RESUME_S = 10.0


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
    live_monitor: bool | None = None,
    open_live_monitor: bool | None = None,
    headless: bool = False,
    mouse: bool = False,
    build_session: Callable[..., SessionRunner] | None = None,
    experiment_version: str | None = None,
    experiment_name: str | None = None,
    initials: str | None = None,
    **extra: Any,
) -> ModeSession:
    """Wire one session in the given mode.

    ``headless`` and ``mouse`` are the two flags that override the machine
    (see :func:`alhazen.modes.flag_refusal`); a mode that cannot honour one
    raises ``ConfigError`` before anything is wired. So does run mode on a
    development rig, one whose settings say ``real_data: false``
    (:func:`alhazen.modes.real_data_refusal`, docs/rigs.md §5), naming the
    rig ``sources`` names when it names one.

    The run is filed under its experiment's version — the one the
    ``pyproject.toml`` above ``task``'s class declares, unless
    ``experiment_version`` (and ``experiment_name``) say otherwise — and
    numbered within that version's folder. It is found once, from the task
    class this was handed, and passed down to the builder as it is.

    ``initials`` are the subject's, passed to the builder to be recorded and
    checked against the registry (``build_session``). The command line makes
    them required for ``run`` and ``test``, the modes that name a real
    subject; here, as for every caller in code, None records none.

    ``instructions`` is the caller's own text for the instruction screen
    (``run_experiment``'s ``instructions=``) and wins over the task's; None
    leaves it to ``Task.instructions``, which ``build_session`` asks.

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
    # Then run mode on a development rig, before anything is found, numbered,
    # built or written. The command line refuses it earlier still, before the
    # params hook (alhazen.cli.main); this is the same rule for code that
    # starts a run-mode session itself, which no command line stands in
    # front of. docs/rigs.md §5.
    refusal = real_data_refusal(
        mode,
        rig,
        _named_rig(sources),
        instead=lambda: [
            "Start the session on a rig that collects real data, or rehearse it on this one "
            "in test or simulate mode."
        ],
    )
    if refusal is not None:
        raise ConfigError(refusal)
    rig = _stand_in_reward(mode, rig, task, notes)

    # The experiment the run is filed under, found from the task class the
    # caller handed in — before a rehearsal rebuilds the task around reduced
    # params or a simulation swaps in its own, whose class may be defined
    # somewhere else — and before the run is numbered, which counts inside
    # that version's folder. A project with no version stops here, with the
    # file to fix named (config/experiment.py).
    experiment = session_experiment(
        type(task), task.name, version=experiment_version, name=experiment_name
    )

    # A run-mode session is the one a subject actually sits through, and a
    # task that never said what that subject reads — neither text nor a
    # deliberate None — would start trial one with no instruction screen and
    # nothing anywhere saying so. The WARNING names the method to write; the
    # note puts the same fact in the lines printed before trial one and in
    # the run's session.log, which outlive the terminal. Only run mode (a
    # pilot is run mode with a shorter params file): test mode rehearses
    # whatever the task declares, and simulate has nobody to read anything.
    # A caller that passed its own text (run.py's `instructions=`) has
    # answered for the task, so there is nothing to warn about.
    if mode is Mode.RUN and instructions is None and not declares_instructions(type(task)):
        log.warning(undeclared_instructions_warning(type(task)))
        notes.append(
            f"instructions: none — {type(task).__name__} does not declare instructions(), "
            f"so the subject is shown nothing before trial one"
        )

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

    # The session's seed, drawn here when none was given rather than left to
    # the builder: the simulated subject below is the first thing in a
    # session that draws at random, and it has to draw from the SAME seed the
    # session records. It used to be handed 0 whenever no --seed was given,
    # so every unseeded rehearsal — and every one the workspace starts — had
    # the same subject making the same latencies, landings and lapses, and
    # the printed "--seed N repeats it" repeated the session but not the
    # subject. resolve_seed keeps a given seed as it is.
    seed = resolve_seed(seed)

    if mode is Mode.SIMULATE:
        simulation = task.simulation(seed)
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
    run_number = (
        run
        if run is not None
        else next_run(data_root, subject, session, experiment_version=experiment.version)
    )

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
        live_monitor=live_monitor,
        open_live_monitor=open_live_monitor,
        tracker=simulation.tracker if simulation else None,
        response=simulation.response if simulation else None,
        # The rig's own probe has already been stood down by rig_for_mode;
        # this is the simulated brain that runs in its place, and None
        # leaves the rig's spike source (a `simulated` one, or none) alone.
        spikes=simulation.spikes if simulation else None,
        # A simulated session has nobody to press SPACE at the instructions.
        auto_start=mode is Mode.SIMULATE,
        rest_resume_after_s=SIMULATION_REST_RESUME_S if mode is Mode.SIMULATE else None,
        # The experiment found above, handed down as it is, so the folder the
        # run was numbered in is the folder it is made in; and the mode, for
        # the run's session.json.
        experiment=experiment,
        mode=mode.value,
        initials=initials,
        **extra,
    )
    built = ModeSession(
        mode=mode,
        runner=runner,
        data_root=data_root,
        run=run_number,
        reductions=reductions,
        simulation=simulation,
        notes=notes,
        experiment=experiment,
    )
    # The same lines the experimenter reads before trial one go into the
    # session log after "session start": the run directory has to say for
    # itself what was reduced and which devices were stood down, and a
    # terminal is not part of the run directory.
    runner.setup_notes = built.describe().splitlines()
    return built
