"""What makes a rehearsal a rehearsal: fewer trials, and its own data root.

Test mode runs the whole experiment — every phase, every condition, the block
breaks, the instructions, the analysis afterwards — with the trial counts
turned down, so an experimenter can sit through it once before a subject does.
Simulation mode runs the same reduced session with nobody in the chair.

Two things have to be true of both, and they are the two things in this
module:

1. **The reduction is announced, never assumed.** Every number it changes is
   returned, and the caller prints them. A mode that quietly redesigned the
   experiment would be worse than no mode at all — the numbers in the config
   snapshot would no longer be the numbers that were run, and the snapshot is
   the record of what happened.
2. **The data goes somewhere else.** A rehearsal writes real files that look
   exactly like a session's, because that is the point: you want to run the
   analysis over them. Which is also exactly why they must not sit in the
   directory the analysis globs for real subjects.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from alhazen.errors import ConfigError
from alhazen.paradigms.config import SchedulerConfig

# What a rehearsal root is called, appended to the real one's directory name.
# A sibling rather than a subdirectory of ``data_root``: an analysis that
# globs ``data_root/sub-*`` finds nothing of a rehearsal either way, but a
# sibling is also obvious in a file listing, and a directory nobody can see is
# a directory somebody eventually analyses by accident.
REHEARSAL_SUFFIX = "-rehearsal"


@dataclass(frozen=True)
class Reduction:
    """One number test mode turned down, and what it was."""

    where: str  # dotted path to the field, e.g. "saccade_paradigm.n_per_condition"
    was: int
    now: int

    def __str__(self) -> str:
        return f"{self.where}: {self.was} -> {self.now}"


def rehearsal_root(data_root: Path | str) -> Path:
    """Where a rehearsal's data goes, given the rig's real data root."""
    root = Path(data_root)
    return root.parent / f"{root.name}{REHEARSAL_SUFFIX}"


def _scheduler_paths(model: BaseModel, prefix: str = "") -> Iterator[str]:
    """Every SchedulerConfig in a params model, as a dotted path.

    Found by TYPE rather than by field name, because experiments do not agree
    on the name: one task calls it ``paradigm``, another declares
    ``saccade_paradigm`` and ``pursuit_paradigm`` and schedules them into
    separate blocks. A reduction that looked for ``paradigm`` would silently
    do nothing to the second — and silently running the full 252-trial
    session when you asked for a rehearsal is the worst outcome available.

    Sequences are walked too, so ``list[SchedulerConfig]`` is found; the path
    then carries the index.
    """
    for name, value in model:
        path = f"{prefix}{name}"
        if isinstance(value, SchedulerConfig):
            yield path
        elif isinstance(value, BaseModel):
            yield from _scheduler_paths(value, f"{path}.")
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                if isinstance(item, SchedulerConfig):
                    yield f"{path}.{index}"
                elif isinstance(item, BaseModel):
                    yield from _scheduler_paths(item, f"{path}.{index}.")


def _dig(data: Any, path: str) -> Any:
    """Follow a dotted path into nested dicts/lists from ``model_dump()``."""
    for part in path.split("."):
        data = data[int(part)] if part.isdigit() else data[part]
    return data


def shrink_params(
    params: BaseModel,
    *,
    n_per_condition: int = 1,
    max_adaptive_trials: int = 10,
) -> tuple[BaseModel, list[Reduction]]:
    """A params model with its trial counts turned down, and what changed.

    What it reduces, and only this:

    - every ``SchedulerConfig.n_per_condition``, to ``n_per_condition``;
    - a staircase's ``n_trials`` and ``n_reversals``, and a QUEST+ block's
      ``n_trials``, to ``max_adaptive_trials`` — an adaptive run's length is
      not a repetition count, so the first rule does not reach it.

    What it deliberately leaves alone is **block structure**. Blocks are part
    of what a rehearsal is for (the break is where a subject stops
    concentrating, and where the experimenter has to do something), they cost
    almost nothing once each block holds one trial per cell, and a task may
    constrain its own block count in ways this cannot know — one of the two
    experiments alhazen was built for requires the block count to be a
    multiple of its motion levels, and would refuse a reduced one.

    Nothing is lowered past what it already is: asking for 3 repetitions of a
    design that specifies 1 must not turn a rehearsal into a longer session
    than the experiment.

    The result is re-validated through the model, so a reduction that breaks
    the task's own rules raises ConfigError here — with the model's own
    complaint in it — rather than producing params that fail later, halfway
    into the session it was supposed to rehearse.
    """
    if n_per_condition < 1:
        raise ValueError("n_per_condition must be >= 1")
    if max_adaptive_trials < 1:
        raise ValueError("max_adaptive_trials must be >= 1")

    data = params.model_dump()
    reductions: list[Reduction] = []

    def lower(path: str, field: str, ceiling: int) -> None:
        """Turn one number down, if it is above the ceiling, and record it."""
        block = _dig(data, path)
        current = block.get(field)
        if current is None or current <= ceiling:
            return
        block[field] = ceiling
        reductions.append(Reduction(f"{path}.{field}", current, ceiling))

    for path in _scheduler_paths(params):
        lower(path, "n_per_condition", n_per_condition)
        scheduler = _dig(data, path)
        if scheduler.get("staircase") is not None:
            lower(f"{path}.staircase", "n_trials", max_adaptive_trials)
            lower(f"{path}.staircase", "n_reversals", max_adaptive_trials)
        if scheduler.get("quest") is not None:
            lower(f"{path}.quest", "n_trials", max_adaptive_trials)

    if not reductions:
        # Not an error: a design that already specifies one repetition per
        # cell is a legitimate thing to rehearse. But it is worth the caller
        # saying so, rather than the experimenter wondering why the "short"
        # run is taking an hour.
        return params, []

    try:
        return type(params).model_validate(data), reductions
    except ValidationError as e:
        raise ConfigError(
            f"a rehearsal with {n_per_condition} repetition(s) per condition is not a valid "
            f"{type(params).__name__}: the experiment constrains the numbers this reduces.\n"
            f"  tried: {', '.join(str(r) for r in reductions)}\n"
            f"  {e}"
        ) from e
