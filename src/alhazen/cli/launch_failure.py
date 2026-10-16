"""Why a run failed, in the words the person at the Run page needs.

A launch the session refuses (``INVALID: ...``, ``CANNOT RUN: ...``) or that
dies early exits non-zero, and the dashboard used to say only "Run failed"
and point at the Console. ``launch_failure`` reads the run's console and
returns its most informative line(s), which the Run page shows beside the
Start button (workspace.js ``renderFailure``), with the Console one click away
for the full output.

The rule, in order; the diagnostic printed LAST in the console wins, because
an earlier one may have been answered (a bad prompt answer is asked again):

1. **A refusal**: a line that starts with alhazen's own refusal prefix,
   ``INVALID:`` or ``CANNOT <WORDS>:`` (``CANNOT RUN:``, ``CANNOT DEMO:``,
   ...; ``REFUSAL``), or the command line's usage error, ``<prog>: error:
   <message>`` (argparse: an unknown or malformed flag; ``USAGE``). The
   reason is the text after the prefix plus the lines that continue it
   (``_continuation``), so a validation error that lists its fields is kept
   whole; a usage error is its message alone (the usage lines before it are
   the Console's).
2. **An uncaught exception**: the last ``Traceback (most recent call last):``
   block's exception line, ``Type: message`` with the type's module path
   dropped (``alhazen.errors.ConfigError`` reads ``ConfigError``), and the
   lines that continue it. The frames are left to the Console.
3. Neither: the console's last non-empty lines (up to ``TAIL_LINES``), or,
   when it printed nothing, the exit code.

A run the workspace could not start at all (``run["error"]``, the operating
system's refusal to execute the interpreter) is reported as that error.
Every reason is capped (``MAX_LINES``, ``MAX_CHARS``) and stripped of
terminal colour codes; the Console keeps the whole output.
"""

from __future__ import annotations

import re
from typing import Any

# alhazen's refusal lines (cli/main.py and every mode prints these).
REFUSAL = re.compile(r"^(INVALID|CANNOT(?: [A-Z]+)+):[ \t]*(.*)$")
# argparse's usage error, its last line: "run.py (my-task): error: <message>".
USAGE = re.compile(r"^[^\s:][^:]*: error: (.+)$")
TRACEBACK = "Traceback (most recent call last):"
# An exception line closing a traceback: a (dotted) name, then ':' or the end.
EXCEPTION = re.compile(r"^([A-Za-z_][\w.]*)(:[ \t]?(.*))?$")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
MAX_LINES = 12
MAX_CHARS = 1600
TAIL_LINES = 3

HEADINGS = {
    "refused": "Run refused",
    "error": "Run stopped with an error",
    "exit": "Run failed",
}


def _lines(log: str) -> list[str]:
    """The console as lines: colour codes removed, and a line rewritten in
    place with carriage returns (a progress counter) read as its last state."""
    text = ANSI.sub("", log).replace("\r\n", "\n")
    return [line.rsplit("\r", 1)[-1].rstrip() for line in text.split("\n")]


def _continuation(lines: list[str], start: int) -> list[str]:
    """The lines from ``start`` that continue a message: up to a blank line,
    the next refusal or traceback, or a workspace note (``[workspace] ...``)."""
    kept: list[str] = []
    for line in lines[start:]:
        if not line.strip() or REFUSAL.match(line) or line.startswith((TRACEBACK, "[workspace]")):
            break
        kept.append(line)
    return kept


def _capped(lines: list[str]) -> str:
    text = "\n".join(lines[:MAX_LINES]).strip()
    if len(lines) > MAX_LINES or len(text) > MAX_CHARS:
        text = text[:MAX_CHARS].rstrip() + " …"
    return text


def _refusal(lines: list[str]) -> tuple[int, str] | None:
    """The last refusal or usage-error line's index and reason, or None."""
    for index in range(len(lines) - 1, -1, -1):
        match = REFUSAL.match(lines[index])
        if match:
            return index, _capped([match.group(2), *_continuation(lines, index + 1)])
        usage = USAGE.match(lines[index])
        if usage:
            return index, _capped([usage.group(1)])
    return None


def _exception(lines: list[str]) -> tuple[int, str] | None:
    """The last traceback's index and its exception line(s), or None."""
    starts = [i for i, line in enumerate(lines) if line.startswith(TRACEBACK)]
    if not starts:
        return None
    for index in range(starts[-1] + 1, len(lines)):
        line = lines[index]
        # The frames ("  File ...", their source and carets) are indented.
        if not line or line[0].isspace():
            continue
        match = EXCEPTION.match(line)
        if match is None:
            # Something else printed into the traceback's place; the frames
            # alone do not say why, so the block is not used.
            return None
        name = match.group(1).rsplit(".", 1)[-1]
        message = match.group(3) or ""
        first = f"{name}: {message}" if message else name
        return starts[-1], _capped([first, *_continuation(lines, index + 1)])
    return None


def launch_failure(log: str, run: dict[str, Any]) -> dict[str, str] | None:
    """Why the run ``run`` (a run record) failed, from its console ``log``:
    ``{"kind", "heading", "reason"}`` with ``kind`` one of "refused",
    "error" or "exit" (see the module's rule); None for a run that did not
    fail."""
    if run.get("status") != "failed":
        return None
    if run.get("error"):
        return _failure("exit", _capped(_lines(str(run["error"]))))
    lines = _lines(log)
    found = [hit for hit in (_refusal(lines), _exception(lines)) if hit is not None]
    if found:
        index, reason = max(found, key=lambda hit: hit[0])
        kind = "error" if lines[index].startswith(TRACEBACK) else "refused"
        return _failure(kind, reason)
    tail = [line for line in lines if line.strip()][-TAIL_LINES:]
    if tail:
        return _failure("exit", _capped(tail))
    code = run.get("returncode")
    said = f"It exited with code {code} and printed nothing." if code is not None else ""
    return _failure("exit", said or "It printed nothing.")


def _failure(kind: str, reason: str) -> dict[str, str]:
    return {"kind": kind, "heading": HEADINGS[kind], "reason": reason}
