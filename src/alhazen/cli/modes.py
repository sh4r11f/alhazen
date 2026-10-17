"""One entry point for an experiment package's own ``run.py``.

Every experiment ships a ``run.py`` so it can be started without installing
anything, and before this existed every one of them grew the same 180 lines:
an argument parser, a next-run-number counter, a rehearsal path, a guard
against autopilotting a real rig. Two experiments had already written it
twice, identically, including the same off-by-one in the run counter.

What is genuinely per-experiment is which task class to run and which rig to
start on when the command line names none, so those are the arguments, and
everything else is shared with ``alhazen run``. Literally shared: both go
through ``add_mode_arguments`` and the same dispatch, because two entry points
that drifted apart would mean a flag that behaves one way at the rig and
another way in a script.

The subject's wording, the params file and the params hook used to be
arguments here too, which is why ``alhazen run`` — handed only the task class
— could have none of them. A task now declares all three itself
(``Task.instructions``, ``Task.default_params``, ``Task.params_hook``), so
both entry points get them. The arguments remain for a ``run.py`` written
before that, and when given they take precedence over the task's own.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NoReturn

from alhazen.modes import Mode


def run_experiment(
    *,
    task_class: type | None = None,
    default_rig: Path | str,
    default_params: Path | str | None = None,
    instructions: Callable[[], str] | None = None,
    argv: list[str] | None = None,
    description: str | None = None,
    params_hook: Callable[[Any, argparse.Namespace], Any] | None = None,
    tasks: Mapping[str, tuple[type, Path | str | None]] | None = None,
    ladders: Mapping[str, Path | str] | None = None,
    **removed: NoReturn,
) -> int:
    """Parse ``argv`` and run this experiment in the mode it names.

    ``task_class`` and ``default_rig`` are all a ``run.py`` needs.
    ``default_rig`` is what ``--rig`` means when the command line gives none,
    and takes what ``--rig`` takes: a rig's name (``"mac"`` — the
    experiment's own ``configs/rig-mac.yaml``, else alhazen's shared mac;
    ``"alhazen/mac"`` for the shared one always) or a path to a rig file. A
    name is looked up in the experiment this task belongs to, wherever the
    command is typed (``alhazen.config.rigs``). Run mode refuses a
    development rig (``real_data: false``) — the laptop most run.py files
    name here — before anything is written, saying no ``--rig`` was given
    and which rigs do collect (docs/rigs.md §5). The three
    optional arguments below are each something the task can declare for
    itself, and a task that does has it applied by ``alhazen run --task`` as
    well as here. **Each one, when given, takes precedence over the task's
    own** for the sessions this ``run.py`` starts; ``alhazen run`` keeps
    using the task's.

    ``default_params`` is the params file used when ``--params`` is not
    given, in place of ``Task.default_params``. ``--params`` still wins over
    it.

    ``instructions`` is the subject's wording, in place of
    ``Task.instructions``. It is a callable rather than a string so an
    experiment that reads its wording from a file pays for the read only when
    this is called, and fails at that point with its own error rather than at
    import.

    ``params_hook(params, args)`` derives parameters from how the session was
    started, in place of ``Task.params_hook`` — it replaces the task's hook,
    and the two are never chained. A task receives only its params and the
    scheduler's generator (``Task.make_source``), so one whose scheduler must
    know *which subject and which session it is* — an adaptive design
    carrying state across sessions is the general case — has no other route
    from the command line to its own code. It runs after the subject and
    session are settled (flags, prompt, or simulate's own), and whatever it
    returns is re-validated through the task's own params model, so a hook
    that returns something the task cannot express fails here rather than
    mid-session.

    ``tasks`` declares an experiment that ships several tasks, in place of
    ``task_class``: a mapping from each task's name — what ``--task`` takes
    — to ``(TaskClass, default_params)``, the params file that task runs
    with when ``--params`` is not given (None: the task's own defaults).
    Exactly one of ``task_class`` and ``tasks`` is given, and
    ``default_params`` goes with ``task_class`` only: with several tasks each
    carries its own. The experiment workspace (``alhazen dashboard``) reads
    the same table out of ``run.py`` — write it as a module-level dict
    literal, ``TASKS = {"name": (TaskClass, "configs/params.yaml"), ...}``,
    and pass ``tasks=TASKS`` — to offer the tasks in its Task menu, so the
    two never disagree about which tasks there are.

    **Every session names its task.** ``--task`` is on the parser in both
    forms: its choices are the table's keys, or, with ``task_class``, the
    one task's ``name`` alone. It is required in every mode but measure,
    which checks the machine and runs no task, and training, where the stage
    names its task. A command that leaves it out is refused as a usage error
    (exit status 2) naming the ``--task`` to pass, before anything loads.
    From 2.5 to 2.x such a command still ran the first task declared (or
    ``default_task``'s, or the one task) with a ``FutureWarning``; 3.0
    removed that.

    ``ladders`` registers the experiment's training ladders
    (alhazen.training.ladder), for ``--mode training --stage <id>``: a
    mapping from each ladder's label — what ``--ladder`` and the workspace's
    Training stage list show — to its YAML file. Write it, like ``TASKS``, as
    a module-level dict literal, ``LADDERS = {"Pursuit (monkey)": HERE /
    "configs" / "training-pursuit.yaml"}``, beside ``PARAMETERS``, and pass
    ``ladders=LADDERS``: the workspace reads the same table out of run.py.
    With ``--stage`` the stage names the task (``--task`` may be left out)
    and its params file (``--params`` is refused).

    ``default_task`` was removed in 3.0 (deprecated since 2.5): it chose
    what a command without ``--task`` ran, and there is no such command any
    more. A ``run.py`` that still passes it is refused with a TypeError
    saying so; delete the argument and name the task with ``--task``. Any
    other keyword this function does not take is the TypeError Python would
    give.
    """
    from alhazen.cli.main import _run_session, add_mode_arguments
    from alhazen.task.task import default_params_path

    # Removed arguments, caught by name so the refusal says what to do
    # rather than only that the keyword is unexpected. `**removed` is typed
    # NoReturn so a type checker still flags any keyword passed to it, as it
    # would with no catch-all at all.
    for keyword in removed:
        if keyword == "default_task":
            raise TypeError(
                "run_experiment() no longer takes default_task= (deprecated since alhazen 2.5, "
                "removed in 3.0): every command names its task with --task, so there is no "
                "default to choose. Delete default_task= from run.py and add --task <name> to "
                "every command that started that task without it"
            )
        raise TypeError(f"run_experiment() got an unexpected keyword argument {keyword!r}")

    # One task or several, never neither and never both: a run.py that says
    # both has two answers to "which task", and the one it did not mean would
    # run without a word.
    if tasks is None:
        if task_class is None:
            raise TypeError("run_experiment takes task_class= (one task) or tasks= (several)")
        # The one task's own name: what its run folders are named after
        # (run-NN_task-<name>), so it is the one spelling --task can take.
        one = getattr(task_class, "name", task_class.__name__)
        names = [one]
        prog = f"run.py ({one})"
        description = description or task_class.__doc__
        task_help = f"the task to run, {one} — required in every mode but measure and training"

    else:
        if task_class is not None:
            raise TypeError("run_experiment takes either task_class= or tasks=, not both")
        if default_params is not None:
            raise TypeError(
                "with tasks=, each task names its own params file in the table; "
                "default_params= is for task_class="
            )
        if not tasks:
            raise ValueError("tasks= declares no task; name at least one")
        names = list(tasks)
        prog = f"run.py ({' | '.join(names)})"
        description = description or (
            f"Tasks: {', '.join(names)}. --task names the one to run, and is required."
        )
        task_help = (
            "which of this experiment's tasks to run — required in every mode but measure "
            "and training"
        )
    # Measure mode runs no task, and training without --stage is refused by
    # the dispatch, so neither needs one named; each is handed the first task
    # declared (the one task) only so the session can find the experiment's
    # folder, and every task in the table belongs to the same experiment.
    unnamed = names[0]

    parser = argparse.ArgumentParser(prog=prog, description=description)
    add_mode_arguments(parser)
    # The choices ARE the declared names, so a misspelt task is refused by
    # argparse with the real names listed, before anything loads. No default:
    # a command that names no task is told so below, not quietly given one.
    parser.add_argument("--task", choices=names, default=None, help=task_help)
    # run.py's params file becomes --params's default, which is exactly what
    # gives it precedence over the task's own (the dispatch asks the task only
    # when --params is still None) and keeps an explicit --params above both.
    parser.set_defaults(params=str(default_params) if default_params else None)
    args = parser.parse_args(argv)
    given = list(sys.argv[1:] if argv is None else argv)
    # Which flags were TYPED, for a training stage (alhazen.cli.main
    # _settle_training_stage): --params has run.py's file as its default, so
    # only the command line can say whether one was given.
    args.params_explicit = any(arg == "--params" or arg.startswith("--params=") for arg in given)
    args.task_explicit = args.task is not None
    args.ladders = dict(ladders or {})
    if tasks is not None:
        args.tasks_table = dict(tasks)
    else:
        assert task_class is not None  # one of the two, checked above
        args.tasks_table = {
            getattr(task_class, "name", task_class.__name__): (
                task_class,
                default_params if default_params is not None else default_params_path(task_class),
            )
        }
    stage_task = None
    if args.stage and args.task is None:
        # The stage names its task (one of TASKS, or a training task by
        # import path): no --task needed, and no warning for its absence. A
        # ladder or stage that cannot be found is said here.
        from alhazen.errors import ConfigError
        from alhazen.training.ladder import find_ladder, load_ladder

        try:
            _, ladder_path = find_ladder(args.ladder or "", args.ladders)
            ladder = load_ladder(ladder_path)
            stage = ladder.stage(args.stage)
        except ConfigError as exc:
            print(f"CANNOT RUN: {exc}", file=sys.stderr)
            return 2
        stage_task = str(stage.task or ladder.task)
        if ":" not in stage_task and stage_task not in names:
            print(
                f"CANNOT RUN: stage {args.stage!r} runs task {stage_task!r}, which is not one of "
                f"this run.py's tasks ({', '.join(names)}) nor an import path",
                file=sys.stderr,
            )
            return 2
    # The default rig goes in as typed — a name or a path — and is resolved by
    # the dispatch exactly as a --rig typed on the command line would be. It
    # is filled in after parsing rather than as the flag's default so the
    # dispatch can tell the two apart: run mode refused on a development rig
    # says "no --rig was given" first when that is what happened, because a
    # forgotten --rig on the laptop every run.py starts on is the usual way
    # to get there (docs/rigs.md §5).
    args.rig_defaulted = args.rig is None
    if args.rig is None:
        args.rig = str(default_rig)
    # The command line as it was parsed, for the run folder to record
    # (session.json's `command`): this process's program — run.py, made
    # relative to the experiment when the run is recorded — and then the
    # arguments this parser was given, which are sys.argv's unless the
    # caller passed its own.
    args.invocation = [sys.argv[0], *(sys.argv[1:] if argv is None else argv)]
    if (
        args.task is None
        and stage_task is None
        and args.mode not in (Mode.MEASURE.value, Mode.TRAINING.value)
    ):
        # Every session names its task: refused as a usage error, the way
        # argparse refuses a misspelt one, before anything is loaded or
        # written. Removed in 3.0 (docs/versioning.md §4): from 2.5 this ran
        # the first task declared, with a FutureWarning.
        if len(names) == 1:
            which = f"pass --task {names[0]}"
        else:
            which = f"pass --task with one of {', '.join(names)}"
        parser.error(
            f"every session names its task; {which} (alhazen 3.0 no longer runs a default "
            "task when --task is left out)"
        )
    if stage_task is not None:
        # The training stage decides the task and its params
        # (alhazen.cli.main._settle_training_stage resolves it).
        args.task = stage_task
        task_class = None
    else:
        args.task = unnamed if args.task is None else args.task
    if tasks is not None and stage_task is None:
        # The chosen task's params file stands in for --params exactly as
        # default_params does for one task; an explicit --params still wins.
        task_class, task_params = tasks[args.task]
        if args.params is None and task_params is not None:
            args.params = str(task_params)

    # Resolved here rather than inside the dispatch: this is run.py's own
    # override, and the dispatch knows only the task. None leaves the
    # instruction screen to the task (Task.instructions), which the session
    # builder asks — the same path `alhazen run` takes.
    args.instructions = instructions() if instructions is not None else None
    return _run_session(args, task_class=task_class, params_hook=params_hook)
