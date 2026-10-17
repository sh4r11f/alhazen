"""Writing a phase's measurement onto the trial's record without losing one.

``ctx.record`` is a plain dict that becomes the trial's row in trials.csv,
so a phase that writes a column another writer already filled replaces that
value, and nothing says so. That is what happened to every experiment that
ran two ``HoldFixation`` phases in one trial: both wrote ``hold_duration_s``,
the second won, and the trials file never held the jittered foreperiod the
subject actually waited.

Two rules close it, and a phase that writes through this module keeps both:

- **A column name is a plain identifier, and never one of the framework's
  own** (:func:`column_name`, checked when the phase is built). The
  framework's columns (``core.trial.TRIAL_RECORD_COLUMNS``) are mostly
  written *after* the phases run — ``outcome``, ``completed``,
  ``feedback`` — so a phase that wrote one would lose its value to the
  engine later in the trial, where no check at write time could see it.
- **A column already on the record is never overwritten in silence**
  (:func:`record_once`). The record is built fresh for every attempt
  (``session/runner.py``), phases run once each and in order, and such a
  phase writes each of its columns once. So a name already on the record
  when the phase writes it was put there earlier in the same attempt: by
  another phase (most often a second instance of the same one), or by the
  trial's own condition or ``build_trial``. Whichever it was, its value is
  about to be lost, so the write is refused. From 2.6 until 3.0 it was a
  ``FutureWarning`` and the value was replaced; 3.0 removed that.
"""

from __future__ import annotations

import re
from typing import Any

from alhazen.core.trial import TRIAL_RECORD_COLUMNS, TrialContext

# What a column name may be: an ASCII identifier. It becomes a CSV header, a
# database column and, in every analysis, a pandas attribute, and a name with
# a space, a dot or a leading digit is a column some of those cannot reach.
_COLUMN_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def column_name(phase: str, argument: str, name: object) -> str:
    """``name`` if it can be a record column this phase owns; else ValueError.

    ``phase`` and ``argument`` are the class and the constructor argument the
    name came from, for the message. Called by the constructor, so a bad name
    fails where it was written, not on the first trial with a subject in the
    chair.
    """
    if not isinstance(name, str) or not _COLUMN_NAME.fullmatch(name):
        raise ValueError(
            f"{phase}({argument}={name!r}) is not a column name: it must be a non-empty "
            "identifier — letters, digits and underscores, not starting with a digit — because "
            "it becomes a column in trials.csv"
        )
    _refuse_framework_column(phase, argument, name, name)
    return name


def column_prefix(phase: str, argument: str, prefix: object, suffixes: tuple[str, ...]) -> str:
    """``prefix`` if every ``<prefix>_<suffix>`` column it makes can be one
    this phase owns; else ValueError.

    For a phase that writes several columns under one prefix, as
    ``LandingSample(record_prefix=...)`` always has. The prefix is checked
    as a name, and each column it makes against the framework's own.
    """
    if not isinstance(prefix, str) or not _COLUMN_NAME.fullmatch(prefix):
        raise ValueError(
            f"{phase}({argument}={prefix!r}) is not a column prefix: it must be a non-empty "
            "identifier — letters, digits and underscores, not starting with a digit — because "
            f"it begins the names of columns in trials.csv "
            f"({', '.join('<prefix>_' + suffix for suffix in suffixes)})"
        )
    for suffix in suffixes:
        _refuse_framework_column(phase, argument, prefix, f"{prefix}_{suffix}")
    return prefix


def _refuse_framework_column(phase: str, argument: str, given: str, column: str) -> None:
    """ValueError when ``column`` — the name ``given`` as ``argument``, or
    one it makes — is a column alhazen writes itself."""
    if column in TRIAL_RECORD_COLUMNS:
        # Most of these are written after every phase has run, so the
        # phase's value would be replaced later in the trial, out of sight
        # of record_once's check.
        made = "" if column == given else f" makes the column {column!r}, which"
        raise ValueError(
            f"{phase}({argument}={given!r}){made} names one of the columns alhazen itself "
            "writes on every trial (core.trial.TRIAL_RECORD_COLUMNS): the framework's value "
            "would replace the phase's, or the phase's the framework's. Choose another name"
        )


def record_once(ctx: TrialContext, key: str, value: Any, *, phase: str, argument: str) -> None:
    """Write ``value`` to ``ctx.record[key]``; ValueError when ``key`` is
    already there.

    ``phase`` and ``argument`` name the class writing and the constructor
    argument that renames its column, so the refusal says exactly what to
    change. Nothing is written when it refuses: the value already on the
    record stays, and the error propagates out of the engine and stops the
    session, because carrying on would save a row that silently lacks one of
    the two values.

    Removed in 3.0 (docs/versioning.md §4): from 2.6 this wrote the value
    anyway with a ``FutureWarning``, since refusing a trial that ran was a
    MAJOR change. The message is the warning's, with the replacement first:
    the experiment's own line is not on the stack (``build_trial`` returned
    long before the phase runs), so the text names the argument to change.
    """
    if key in ctx.record:
        raise ValueError(
            f"{phase} cannot write its {key!r} column: the trial's record already holds "
            f"{key!r}. An earlier phase of this trial — most likely another {phase} — or the "
            f"trial's condition or build_trial wrote it first, and writing it again would "
            f"lose that value from trials.csv. Give each {phase} in the trial a different "
            f"{argument}"
        )
    ctx.record[key] = value
