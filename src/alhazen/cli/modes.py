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
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


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
    default_task: str | None = None,
) -> int:
    """Parse ``argv`` and run this experiment in the mode it names.

    ``task_class`` and ``default_rig`` are all a ``run.py`` needs. The three
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
    ``--task`` then joins the parser with those names as its choices, and
    ``default_task`` is the one run without it, else the first declared.
    Exactly one of ``task_class`` and ``tasks`` is given, and
    ``default_params`` goes with ``task_class`` only: with several tasks each
    carries its own. The experiment workspace (``alhazen dashboard``) reads
    the same table out of ``run.py`` — write it as a module-level dict
    literal, ``TASKS = {"name": (TaskClass, "configs/params.yaml"), ...}``,
    and pass ``tasks=TASKS`` — to offer the tasks in its Task menu, so the
    two never disagree about which tasks there are.
    """
    from alhazen.cli.main import _run_session, add_mode_arguments

    # One task or several, never neither and never both: a run.py that says
    # both has two answers to "which task", and the one it did not mean would
    # run without a word.
    if tasks is None:
        if task_class is None:
            raise TypeError("run_experiment takes task_class= (one task) or tasks= (several)")
        names: list[str] = []
        prog = f"run.py ({getattr(task_class, 'name', task_class.__name__)})"
        description = description or task_class.__doc__
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
        default_task = names[0] if default_task is None else default_task
        if default_task not in tasks:
            raise ValueError(
                f"default_task {default_task!r} is not one of the declared tasks: "
                f"{', '.join(names)}"
            )
        prog = f"run.py ({' | '.join(names)})"
        description = description or (
            f"Tasks: {', '.join(names)}. --task chooses one; without it, {default_task}."
        )

    parser = argparse.ArgumentParser(prog=prog, description=description)
    add_mode_arguments(parser)
    if tasks is not None:
        # The choices ARE the table's keys, so a misspelt task is refused by
        # argparse with the real names listed, before anything loads.
        parser.add_argument(
            "--task",
            choices=names,
            default=default_task,
            help=f"which of this experiment's tasks to run (default: {default_task})",
        )
    # run.py's params file becomes --params's default, which is exactly what
    # gives it precedence over the task's own (the dispatch asks the task only
    # when --params is still None) and keeps an explicit --params above both.
    parser.set_defaults(
        rig=str(default_rig), params=str(default_params) if default_params else None
    )
    args = parser.parse_args(argv)
    if tasks is not None:
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
