"""One entry point for an experiment package's own ``run.py``.

Every experiment ships a ``run.py`` so it can be started without installing
anything, and before this existed every one of them grew the same 180 lines:
an argument parser, a next-run-number counter, a rehearsal path, a guard
against autopilotting a real rig. Two experiments had already written it
twice, identically, including the same off-by-one in the run counter.

What is genuinely per-experiment is two things — which task class to run, and
where the subject-facing wording comes from — so those are the arguments, and
everything else is shared with ``alhazen run``. Literally shared: both go
through ``add_mode_arguments`` and the same dispatch, because two entry points
that drifted apart would mean a flag that behaves one way at the rig and
another way in a script.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Any


def run_experiment(
    *,
    task_class: type,
    default_rig: Path | str,
    default_params: Path | str | None = None,
    instructions: Callable[[], str] | None = None,
    argv: list[str] | None = None,
    description: str | None = None,
    params_hook: Callable[[Any, argparse.Namespace], Any] | None = None,
) -> int:
    """Parse ``argv`` and run this experiment in the mode it names.

    ``instructions`` is a callable rather than a string so an experiment that
    reads its wording from a file — which both of alhazen's do, so that what a
    subject hears cannot drift from what was reviewed — pays for the read only
    when a session is actually going to show it, and fails at that point with
    its own error rather than at import.

    ``params_hook(params, args)`` is the one place an experiment may derive
    its parameters from how it was invoked. A task receives only its params
    and the scheduler's generator (``Task.make_source``), so one whose
    scheduler must know *which subject and which session it is* — an
    adaptive design carrying state across sessions is the general case — has
    no other route from the command line to its own code. Whatever it
    returns is re-validated through the task's own params model, so a hook
    that returns something the task cannot express fails here rather than
    mid-session.
    """
    from alhazen.cli.main import _run_session, add_mode_arguments

    parser = argparse.ArgumentParser(
        prog=f"run.py ({getattr(task_class, 'name', task_class.__name__)})",
        description=description or task_class.__doc__,
    )
    add_mode_arguments(parser)
    parser.set_defaults(
        rig=str(default_rig), params=str(default_params) if default_params else None
    )
    args = parser.parse_args(argv)

    # Resolved here rather than inside the dispatch: an experiment's wording
    # is its own business, and the dispatch has no way to find it.
    args.instructions = instructions() if instructions is not None else None
    return _run_session(args, task_class=task_class, params_hook=params_hook)
