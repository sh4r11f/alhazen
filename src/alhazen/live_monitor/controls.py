"""Which operator commands the live monitor's server passes on, in which status.

The page's buttons are a view; this table is the boundary. The server checks
every POST against it before anything reaches the session's queue
(runtime.py), and the session publishes, from the same table, which buttons
it will act on right now (session/operator_commands.py), so the two can never
disagree about what a status allows.

A command is passed on only in the status the session can act on it in.
The rule is the same on every rig (it is not a rig setting):

- ``running``: what the engine acts on during a trial, exactly as it acts on
  the keyboard's keys (core/commands.py) — pause, skip the trial, a manual
  reward, quit, and the training stage keys.
- ``paused``: what the pause menu offers (session/pause.py) — resume, the
  three eye-tracker procedures, the manual reward, the stage keys and quit.

The three eye-tracker procedures (calibrate, validate, drift correct) are
paused-only: a procedure takes the subject's screen over, so the operator
pauses first and then chooses it. There is no "pause and calibrate" in one
press, from the page or from the rig's keyboard (core/engine.py refuses the
in-trial calibrate key with the same sentence, core/commands.py
``paused_only_refusal``).

Every other status passes nothing: ``calibrating`` (a procedure is running,
and nothing reads the queue until it ends), ``starting`` and the terminal
ones. Each refusal says why, in words the page shows.

Interface: ``KNOWN_COMMANDS``, ``STATUS_COMMANDS`` and ``status_refusal``.
Callers must not rely on the wording of a refusal, only on whether there is
one.
"""

from __future__ import annotations

from alhazen.core.commands import paused_only_refusal

# The status an eye-tracker procedure publishes under (session/eyetracker.py
# PROCEDURE_STATUS). Named here too rather than imported: the live monitor
# sits below the session in the layers, and the string is the wire contract.
_PROCEDURE_STATUS = "calibrating"

# The training stage keys: the same rows in both statuses.
_STAGE_COMMANDS = frozenset({"promote_stage", "demote_stage", "hold_stage"})

_RUNNING = frozenset({"pause", "skip_trial", "manual_reward", "quit"})
_PAUSED = frozenset({"resume", "calibrate", "validate", "drift_correct", "manual_reward", "quit"})
STATUS_COMMANDS: dict[str, frozenset[str]] = {
    "running": _RUNNING | _STAGE_COMMANDS,
    "paused": _PAUSED | _STAGE_COMMANDS,
}

KNOWN_COMMANDS: frozenset[str] = frozenset().union(*STATUS_COMMANDS.values())

# The eye-tracker procedures: accepted only while paused, on every rig.
PROCEDURE_COMMANDS: frozenset[str] = frozenset({"calibrate", "validate", "drift_correct"})

# How each command is named in a refusal, as its button reads.
LABELS = {
    "pause": "Pause",
    "resume": "Resume",
    "calibrate": "Calibrate",
    "validate": "Validate",
    "drift_correct": "Drift correct",
    "manual_reward": "Reward",
    "skip_trial": "Skip trial",
    "quit": "Quit",
    "promote_stage": "Promote stage",
    "demote_stage": "Demote stage",
    "hold_stage": "Hold stage",
}

_TERMINAL_REFUSAL = "the session has ended"


def status_refusal(name: str, status: str) -> str | None:
    """Why ``name`` is not passed on in ``status``, or None when it is.

    ``name`` must be one of ``KNOWN_COMMANDS``; an unknown name is a bad
    request, refused before this is asked (runtime.py), so it raises here.
    """
    if name not in KNOWN_COMMANDS:
        raise ValueError(f"unknown operator command {name!r}")
    if name in STATUS_COMMANDS.get(status, frozenset()):
        return None
    label = LABELS[name]
    if status == "running":
        if name in PROCEDURE_COMMANDS:
            return paused_only_refusal(label)
        return f"{label} is available only while the session is paused"
    if status == "paused":
        if name == "pause":
            return "the session is already paused"
        return f"{label} is available only while a trial is running"
    if status == _PROCEDURE_STATUS:
        return "an eye-tracker procedure is running; the controls return when it ends"
    if status == "starting":
        return "the session has not started yet"
    return _TERMINAL_REFUSAL
