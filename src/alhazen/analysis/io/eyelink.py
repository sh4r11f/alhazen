"""Reading EyeLink recordings: the ASC text form, and getting there from EDF.

The tracker writes a binary ``.edf`` on its Host PC. SR Research's
``edf2asc`` converts it to text; nothing else can read the binary, and
alhazen does not try. Two things this module does:

- find ``edf2asc`` and convert, with an error that says what to install if it
  is missing rather than a bare "command not found";
- parse the resulting text into samples and messages.

A ``.asc`` sitting beside the ``.edf`` is used as-is. Re-converting a file
someone already converted wastes minutes per session and can silently differ
if their converter had different flags.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from alhazen.errors import DataError

log = logging.getLogger(__name__)

# EyeLink writes MISSING_DATA coordinates for a blink; the ASC form writes
# a dot. Either way it is "no eye", not a position at the origin.
MISSING_MARKERS = {".", "-32768", "-32768.0"}


@dataclass
class EyeLinkRecording:
    """One recording's samples and messages, on the tracker's own clock."""

    times_ms: np.ndarray  # tracker clock, milliseconds
    gaze_x: np.ndarray  # screen px; NaN where the eye was not seen
    gaze_y: np.ndarray
    pupil: np.ndarray
    messages: list[tuple[float, str]]  # (tracker ms, text)

    @property
    def n_samples(self) -> int:
        return int(self.times_ms.size)

    def message_times(self, text: str) -> list[float]:
        """Tracker-clock times of every message with exactly this text."""
        return [time for time, message in self.messages if message == text]

    def messages_starting(self, prefix: str) -> list[tuple[float, str]]:
        """Every message beginning with ``prefix`` — how ``TRIALID 7`` and
        friends are found without hardcoding the number."""
        return [(time, message) for time, message in self.messages if message.startswith(prefix)]


def ensure_asc(edf_path: Path | str, force: bool = False) -> Path:
    """The ASC form of an EDF, converting only if needed.

    Raises with the actual remedy when the converter is missing: ``edf2asc``
    ships with SR Research's EyeLink Developer's Kit, and nobody guesses that
    from "No such file or directory".
    """
    edf_path = Path(edf_path)
    asc_path = edf_path.with_suffix(".asc")
    if asc_path.exists() and not force:
        log.info("using the existing %s rather than re-converting", asc_path.name)
        return asc_path
    if not edf_path.exists():
        raise DataError(f"EDF not found: {edf_path}")

    converter = shutil.which("edf2asc")
    if converter is None:
        raise DataError(
            f"edf2asc is not on PATH, so {edf_path.name} cannot be converted. It ships "
            f"with SR Research's EyeLink Developer's Kit (the same install that provides "
            f"pylink). If someone has already converted this recording, put the .asc "
            f"beside the .edf and it will be used as it is."
        )
    log.info("converting %s with %s", edf_path.name, converter)
    result = subprocess.run(
        [converter, "-y", str(edf_path)], capture_output=True, text=True, check=False
    )
    if result.returncode != 0 or not asc_path.exists():
        raise DataError(
            f"edf2asc failed on {edf_path.name} (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return asc_path


def read_asc(asc_path: Path | str) -> EyeLinkRecording:
    """Parse an ASC file into samples and messages.

    ASC is line-oriented: ``MSG <time> <text>`` for a message, and a sample
    line starting with a number. Anything else — the header, the tracker's
    own fixation and saccade parses, blink markers — is skipped here, because
    the events this framework aligns on are the ones IT sent, not the
    tracker's own segmentation of the eye trace.
    """
    asc_path = Path(asc_path)
    if not asc_path.exists():
        raise DataError(f"ASC file not found: {asc_path}")

    times: list[float] = []
    xs: list[float] = []
    ys: list[float] = []
    pupils: list[float] = []
    messages: list[tuple[float, str]] = []

    for line in asc_path.read_text(errors="replace").splitlines():
        if line.startswith("MSG"):
            parts = line.split(maxsplit=2)
            if len(parts) >= 3:
                try:
                    messages.append((float(parts[1]), parts[2].strip()))
                except ValueError:
                    # A malformed MSG line is skipped rather than fatal: an
                    # ASC often ends mid-line if the recording was stopped
                    # abruptly, and one unusable line is not a reason to
                    # refuse a session's eye data.
                    log.debug("skipping unparseable message line: %r", line[:80])
            continue
        fields = line.split()
        if not fields or not fields[0][0].isdigit():
            continue
        try:
            time_ms = float(fields[0])
        except ValueError:
            continue
        # A monocular sample line is: time, x, y, pupil, [flags]
        if len(fields) < 4:
            continue
        times.append(time_ms)
        xs.append(_coordinate(fields[1]))
        ys.append(_coordinate(fields[2]))
        pupils.append(_coordinate(fields[3]))

    log.info("%s: %d samples, %d messages", asc_path.name, len(times), len(messages))
    return EyeLinkRecording(
        times_ms=np.asarray(times, dtype=float),
        gaze_x=np.asarray(xs, dtype=float),
        gaze_y=np.asarray(ys, dtype=float),
        pupil=np.asarray(pupils, dtype=float),
        messages=messages,
    )


def _coordinate(field: str) -> float:
    """One sample field as a number, with "no eye" as NaN.

    NaN rather than 0.0 or the raw sentinel: every downstream mean, min and
    plot then treats a blink as missing, which it is — a zero would be a
    position at the screen's centre, and a -32768 a position off the planet.
    """
    if field in MISSING_MARKERS:
        return float("nan")
    try:
        return float(field)
    except ValueError:
        return float("nan")
