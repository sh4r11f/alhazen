"""Reading a ViewPixx (TRACKPixx3) run's eye data, and putting it on the
session clock.

This module exists because a ViewPixx run has no EDF. The EyeLink writes its
messages *into* the sample stream, so an EDF carries its own alignment between
task time and sample time, and :mod:`alhazen.analysis.io.eyelink` can hand
back both together. Nothing can be written into the TRACKPixx3's stream, so
the backend records the alignment beside it instead: every task event is
stamped on the device clock and the session clock at the same moment, in
``<base>_gaze-messages.csv`` (devices/eyetracker/viewpixx.py).

So the alignment here is a **fit**, not a lookup — and a fit that is not tight
is not an alignment. :func:`fit_clock` reports its residual and refuses one
that is worse than a sample period: a drifting or nonlinear residual means the
two clocks were not measuring the same time, and every latency and every
landing time downstream would be quietly wrong.

**The column names are the device's, captured from a real recording.** The
header a TRACKPixx3 writes is ``REAL_HEADER`` below, verbatim: the values are
", "-separated, so every name after the first carries a leading space and the
first a tab, and ``Right Fixaion`` is VPixx's own typo. ``DEFAULT_COLUMNS``
maps this module's names onto those, and the match is case- and
separator-insensitive, so the whitespace never matters — but the *words* do:
an earlier guess (``Time``, ``LeftEyeX``) passed every test written against
fixtures that used the guessed names and could not open a real file. The
fixture in ``tests/fixtures/trackpixx3/`` is the real header, for that reason.

**Which way is up has now been measured.** The backend hands the device its
calibration targets in centered px with y up (the frame PsychoPy draws in),
and the device's polynomial maps raw eye vectors onto the frame the targets
were given in — so ``Left/Right Screen X/Y`` should be centered px, y up.
That is no longer only an argument. A calibrated accuracy check on the rig
(``--mode measure``, 2026-09-09) reports each target's position and the gaze
measured at it, and those gaze numbers *are* the device's own output: the
backend converts device→screen with ``Screen.centered_to_screen`` and the
check converts back with ``screen_to_centered``, which are exact inverses. At
a target on the screen's centre the device returned (1, 32) px; at targets
±293 px from it, (-293, 305), (299, 301), (-279, -274) and (300, -270), for
errors of 0.26 to 0.87 degrees. A top-left, y-down device would have answered
the centre target with roughly (960, 540) and every error would have been
tens of degrees.

``gaze_frame`` stays, because a recording calibrated by some other tool — one
that handed the device top-left-origin targets — is in the other frame, and
that is a fact about the recording rather than about the hardware. Reading
the wrong one is the quietest mistake available here: every position shifts
by half a panel while staying a tight, plausible-looking cluster, so nothing
downstream raises and every landing is wrong. :func:`read_run` therefore
checks how much of a run's tracked gaze falls outside the panel it was
recorded on, and refuses a run where most of it does.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
import yaml

from alhazen.config.models import MonitorConfig
from alhazen.devices.eyetracker.viewpixx import is_tracking_lost
from alhazen.display.screen import Screen
from alhazen.errors import DataError

log = logging.getLogger(__name__)

# One real TRACKPixx3 header, exactly as the device wrote it (pypixxlib 1.9.2,
# ``TPxSaveToCSV``). Kept so DEFAULT_COLUMNS can be resolved against the
# genuine article in a test, and so the synthetic fixtures write what the
# device writes rather than what the reader would like.
REAL_HEADER: tuple[str, ...] = (
    "Timestamp",
    "\tLeft Screen X",
    " Left Screen Y",
    " Left Pupil Diameter",
    " Right Screen X",
    " Right Screen Y",
    " Right Pupil Diameter",
    " Digital In",
    " Left Blink",
    " Right Blink",
    " Digital Out",
    " Left Fixation",
    " Right Fixaion",
    " Left Eye Saccade",
    " Right Eye Saccade",
    " MessageCode",
    " Left X Vector",
    " Left Y Vector",
    " Right X Vector",
    " Right Y Vector",
    " SoftState",
    " SoftStateStart",
)

# How this module names the columns it needs, mapped to the device's own
# names. Overridable per call (``columns=``) for a firmware that renames one;
# the mapping fails loudly, naming both sides, when a name is not found.
DEFAULT_COLUMNS: dict[str, str] = {
    "device_time_s": "Timestamp",
    "left_x_px": "Left Screen X",
    "left_y_px": "Left Screen Y",
    "right_x_px": "Right Screen X",
    "right_y_px": "Right Screen Y",
}

# Read when the file has them, left out of the samples when it does not: the
# device writes them, but a recording made by another tool may not, and
# neither the clock fit nor a position depends on them.
OPTIONAL_COLUMNS: dict[str, str] = {
    "left_blink": "Left Blink",
    "right_blink": "Right Blink",
    "left_pupil": "Left Pupil Diameter",
    "right_pupil": "Right Pupil Diameter",
}

MESSAGE_COLUMNS = ("device_time_s", "session_time_s", "message")

# How much of a run's tracked gaze may sit outside the panel before the frame
# it was read in is in doubt. A subject looks away and a calibration
# extrapolates a little past the edges, so a few percent is ordinary; a
# quarter of a run is not, and reading the wrong frame puts most of a run
# there — offset by half a panel, and no less tightly clustered for it.
OFF_PANEL_WARN = 0.02
OFF_PANEL_REFUSE = 0.25

# The frame the device's ``Screen X/Y`` columns are in. ``centered_y_up`` is
# what a TRACKPixx3 calibrated by this backend reports, measured on the rig
# (module docstring); ``screen_y_down`` is the frame a GazeSample carries, for
# a recording calibrated by another tool that handed the device
# top-left-origin targets.
GazeFrame = Literal["centered_y_up", "screen_y_down"]


@dataclass(frozen=True)
class ClockFit:
    """The affine map from the device's clock to the session's.

    ``max_residual_s`` is the headline number: it is how far the worst
    alignment mark sits from the fitted line, and therefore the honest error
    bar on every time this module produces.
    """

    slope: float
    intercept: float
    max_residual_s: float
    n_marks: int

    def to_session(self, device_time_s: np.ndarray | float) -> np.ndarray:
        return self.slope * np.asarray(device_time_s, dtype=float) + self.intercept


class RecordingViews:
    """The views every ViewPixx recording offers over its samples table.

    A plain mixin rather than a dataclass base, deliberately: the recordings
    below declare their own fields, and inheriting them from here would fix
    their order for anyone who ever constructed one positionally. What is
    shared is behaviour, not layout.
    """

    samples: pd.DataFrame
    messages: pd.DataFrame

    @property
    def sample_rate_hz(self) -> float:
        """The device's rate, from its own timestamps."""
        steps = np.diff(self.samples["t_device"].to_numpy(dtype=float))
        return float(1.0 / np.median(steps[steps > 0]))

    def between(self, t0: float, t1: float) -> pd.DataFrame:
        """The samples with ``t0 <= t_session < t1``."""
        t = self.samples["t_session"]
        return self.samples[(t >= t0) & (t < t1)]

    def trial_spans(self) -> pd.DataFrame:
        """One row per trial segment the backend opened: ``trial_index``,
        ``status`` (the ``attempt N`` the backend wrote), ``t_start`` and
        ``t_end`` on the session clock.

        A segment starts at its ``TRIAL <index> <status>`` mark and ends at
        the ``trial_end`` mark that follows it — or, for a trial the session
        left without one (a quit, a crash), at the next trial's start, or at
        the last sample. Every trial the tracker was told about gets a row,
        including re-served attempts, so a trial index can appear twice.
        """
        # Built as parallel lists: the open trial's end is patched in place
        # when its close is found, and None marks a segment still open.
        indices: list[int] = []
        statuses: list[str] = []
        starts: list[float] = []
        ends: list[float | None] = []
        for _, row in self.messages.iterrows():
            text = str(row["message"])
            t = float(row["session_time_s"])
            if text.startswith("TRIAL "):
                parts = text.split(maxsplit=2)
                if ends and ends[-1] is None:
                    ends[-1] = t  # the previous trial never closed: it ends here
                indices.append(int(parts[1]))
                statuses.append(parts[2] if len(parts) > 2 else "")
                starts.append(t)
                ends.append(None)
            elif text == "trial_end" and ends and ends[-1] is None:
                ends[-1] = t
        if ends and ends[-1] is None:
            ends[-1] = float(self.samples["t_session"].iloc[-1]) if len(self.samples) else np.nan
        return pd.DataFrame(
            {"trial_index": indices, "status": statuses, "t_start": starts, "t_end": ends},
            columns=["trial_index", "status", "t_start", "t_end"],
        )


@dataclass(frozen=True)
class GazeRecording(RecordingViews):
    """One run's eye data for a single eye, ready to analyse.

    ``samples`` columns: ``t_session`` (seconds, the same clock as every event
    and flip in the run), ``t_device`` (the device's own clock, kept for
    cross-checking), ``x_dva``/``y_dva`` (degrees, relative to the screen
    centre, y up) and ``tracked`` (False where the device reported no eye or
    flagged a blink); ``pupil`` when the file carried a pupil diameter for the
    eye read. Lost samples are kept as rows with NaN positions rather than
    dropped: a blink is a *gap at a known time*, and deleting it would let a
    velocity differentiator interpolate straight across it and invent a
    saccade.
    """

    samples: pd.DataFrame
    messages: pd.DataFrame
    fit: ClockFit
    screen: Screen
    eye: str
    gaze_frame: GazeFrame


@dataclass(frozen=True)
class BinocularRecording(RecordingViews):
    """One run's eye data with **both** eyes kept, from
    :func:`read_run_binocular`.

    ``samples`` carries the same ``t_session`` and ``t_device`` as the
    monocular form, then each eye's own columns: ``left_x_dva``,
    ``left_y_dva``, ``left_tracked``, ``left_pupil`` and the four ``right_``
    equivalents. Degrees from the screen centre with y up, exactly as
    :class:`GazeRecording` uses them, so a reader who knows one knows the
    other.

    **Two tracked flags, never one.** A single flag meaning "both eyes" would
    be a different predicate wearing the same name, and it would hide the case
    it is most important to see: one eye lost while the other is tracked. That
    case is neither hypothetical nor cheap. Measured on 500 samples with the
    left eye lost for 50 of them, encoding loss *only* as NaN leaves a version
    estimate (the mean of the two eyes) undefined for those 50 as well,
    because ``(finite + nan) / 2`` is nan — so the surviving eye's answer to
    "where was the subject looking" is discarded silently. A lost eye's
    position is NaN here, which makes naive arithmetic fail loudly rather than
    use a stale value; the boolean is what makes the loss *addressable*, so a
    consumer can tell one eye lost from both without recomputing finiteness
    for itself.

    **Vergence is not a column**, on purpose. It is ``left_x_dva -
    right_x_dva`` (positive for convergence, which follows from the frame) and
    computing it is one line — but its absolute value carries the subject's
    tonic vergence and both eyes' calibration offsets, so it means nothing
    until it is baseline-subtracted against a window the experiment defines. A
    column here would invite somebody to plot it raw.
    """

    samples: pd.DataFrame
    messages: pd.DataFrame
    fit: ClockFit
    screen: Screen
    gaze_frame: GazeFrame


@dataclass(frozen=True)
class _Loaded:
    """A recording's file-level facts, before any eye is chosen.

    Everything here is shared by both readers, and it is the half that has
    been checked against a file a device really wrote: the header mapping,
    the clock fit, the device's own timestamps.
    """

    frame: pd.DataFrame
    mapping: dict[str, str]
    optional: dict[str, str]
    messages: pd.DataFrame
    messages_path: Path
    screen: Screen
    fit: ClockFit
    device_t: np.ndarray
    samples_path: Path


def _load_run(
    run_dir: str | Path, columns: dict[str, str] | None, max_residual_s: float | None
) -> _Loaded:
    """Open a run directory and do everything that does not depend on which
    eye is being read."""
    run_dir = Path(run_dir)
    samples_path = _one_file(run_dir, "*_gaze.csv")
    messages_path = _one_file(run_dir, "*_gaze-messages.csv")

    messages = _read_messages(messages_path)
    screen = _screen_from_snapshot(run_dir)

    frame = pd.read_csv(samples_path)
    mapping = _resolve_columns(frame, {**DEFAULT_COLUMNS, **(columns or {})}, samples_path)
    optional = _resolve_columns(frame, OPTIONAL_COLUMNS, samples_path, required=False)

    # Sample period, needed both to set the residual tolerance and to report
    # the rate. Taken from the device's own timestamps rather than assumed:
    # the TRACKPixx3 runs at whatever rate VPixx's tools left it at.
    device_t = frame[mapping["device_time_s"]].to_numpy(dtype=float)
    period_s = _sample_period_s(device_t, samples_path)
    fit = fit_clock(messages, tolerance_s=max_residual_s or period_s)
    return _Loaded(
        frame=frame,
        mapping=mapping,
        optional=optional,
        messages=messages,
        messages_path=messages_path,
        screen=screen,
        fit=fit,
        device_t=device_t,
        samples_path=samples_path,
    )


def _eye_in_dva(
    loaded: _Loaded, eye: str, gaze_frame: GazeFrame, check_bounds: bool
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray | None]:
    """One eye as degrees from the screen centre: (x, y, tracked, pupil).

    The frame conversion and the bounds check live here so that both readers
    get them, and get them identically — a binocular run read in the wrong
    frame is wrong in exactly the way a monocular one is.
    """
    x_px, y_px, tracked, pupil = _select_eye(loaded.frame, loaded.mapping, loaded.optional, eye)
    screen = loaded.screen
    if gaze_frame == "screen_y_down":
        x_px = x_px - screen.width_px / 2.0
        y_px = screen.height_px / 2.0 - y_px
    elif gaze_frame != "centered_y_up":
        raise DataError(
            f"gaze_frame must be 'centered_y_up' or 'screen_y_down', got {gaze_frame!r}"
        )
    # Unconditional: an empty recording is worth a word whatever the caller
    # thinks about the panel's edges.
    _warn_if_nothing_tracked(tracked, loaded.samples_path, eye)
    if check_bounds:
        _check_on_panel(x_px, y_px, tracked, screen, gaze_frame, loaded.samples_path, eye)
    return (
        np.where(tracked, x_px / screen.px_per_deg, np.nan),
        np.where(tracked, y_px / screen.px_per_deg, np.nan),
        tracked,
        None if pupil is None else np.where(tracked, pupil, np.nan),
    )


def _samples_or_raise(data: dict[str, np.ndarray], samples_path: Path) -> pd.DataFrame:
    """The samples table, refused if the clock fit left it out of order."""
    samples = pd.DataFrame(data)
    if not samples["t_session"].is_monotonic_increasing:
        raise DataError(
            f"{samples_path} timestamps are not increasing after the clock fit; the "
            f"file is out of order or two recordings were concatenated"
        )
    return samples


def read_run(
    run_dir: str | Path,
    eye: str | None = None,
    columns: dict[str, str] | None = None,
    max_residual_s: float | None = None,
    gaze_frame: GazeFrame = "centered_y_up",
    check_bounds: bool = True,
) -> GazeRecording:
    """Read a run directory's ViewPixx eye data onto the session clock.

    ``eye`` defaults to whatever the session actually recorded, which the
    backend wrote into the message stream every trial as ``EYE_USED <eye>``.
    Reading it from the data rather than from a config is deliberate: the
    config may have been edited since. ``average`` is the mean of the two
    eyes where both were tracked, and a gap where either was not — the same
    rule the live backend applies (devices/eyetracker/viewpixx.py
    ``select_eye``), so online and offline never disagree about a sample.

    **This returns one eye.** The device always records both, and for most
    experiments one of them is the measurement; for a binocular one it is not
    a reduced version of the measurement but none of it — vergence is the
    difference between the eyes, and ``average`` is not vergence either. Use
    :func:`read_run_binocular` for that.

    ``check_bounds`` refuses a run whose tracked gaze mostly falls outside the
    panel, which is what reading the wrong ``gaze_frame`` looks like. Pass
    False for a recording that genuinely sits off-panel — and only once that
    is established, because the failure it guards against produces data that
    looks entirely reasonable.
    """
    loaded = _load_run(run_dir, columns, max_residual_s)
    eye = eye or _eye_used(loaded.messages, loaded.messages_path)
    if eye not in ("left", "right", "average"):
        raise DataError(f"eye must be 'left', 'right' or 'average', got {eye!r}")

    x_dva, y_dva, tracked, pupil = _eye_in_dva(loaded, eye, gaze_frame, check_bounds)
    data = {
        "t_session": loaded.fit.to_session(loaded.device_t),
        "t_device": loaded.device_t,
        "x_dva": x_dva,
        "y_dva": y_dva,
        "tracked": tracked,
    }
    if pupil is not None:
        data["pupil"] = pupil
    return GazeRecording(
        samples=_samples_or_raise(data, loaded.samples_path),
        messages=loaded.messages,
        fit=loaded.fit,
        screen=loaded.screen,
        eye=eye,
        gaze_frame=gaze_frame,
    )


def read_run_binocular(
    run_dir: str | Path,
    columns: dict[str, str] | None = None,
    max_residual_s: float | None = None,
    gaze_frame: GazeFrame = "centered_y_up",
    check_bounds: bool = True,
) -> BinocularRecording:
    """Read a run keeping **both** eyes, for an experiment whose measurement
    is the relation between them.

    Vergence is ``left_x_dva - right_x_dva`` and version is their mean;
    neither survives the reduction :func:`read_run` performs, which is why
    this is a separate entry point rather than an argument to that one. It
    reads the file once and selects each eye from it, so the header mapping,
    the clock fit, the blink rule and the bounds check are literally the same
    code the monocular reader is tested through.

    There is no ``eye`` argument and no ``EYE_USED`` requirement: that mark
    records which eye the *session* read online, for its fixation windows and
    its drift correction, and the device recorded both regardless. A run whose
    online eye was the left one is still a perfectly good binocular recording.

    See :class:`BinocularRecording` for the columns, for why there are two
    tracked flags, and for why vergence is not one of them.

    Moving from two ``read_run`` calls to one of these is mechanical except
    for one rename: the monocular ``pupil`` column is ``left_pupil`` and
    ``right_pupil`` here, since a single unprefixed name would have had to
    pick an eye.
    """
    loaded = _load_run(run_dir, columns, max_residual_s)
    data: dict[str, np.ndarray] = {
        "t_session": loaded.fit.to_session(loaded.device_t),
        "t_device": loaded.device_t,
    }
    for eye in ("left", "right"):
        x_dva, y_dva, tracked, pupil = _eye_in_dva(loaded, eye, gaze_frame, check_bounds)
        data[f"{eye}_x_dva"] = x_dva
        data[f"{eye}_y_dva"] = y_dva
        data[f"{eye}_tracked"] = tracked
        if pupil is not None:
            data[f"{eye}_pupil"] = pupil
    return BinocularRecording(
        samples=_samples_or_raise(data, loaded.samples_path),
        messages=loaded.messages,
        fit=loaded.fit,
        screen=loaded.screen,
        gaze_frame=gaze_frame,
    )


def fit_clock(messages: pd.DataFrame, tolerance_s: float) -> ClockFit:
    """Least-squares device→session map, refused if it is not tight.

    Two marks would fit any line exactly and prove nothing, so at least three
    are required. The residual check is the real content: it is what tells the
    difference between two clocks that ran together and two that merely
    started together.
    """
    if len(messages) < 3:
        raise DataError(
            f"the clock fit needs at least 3 alignment marks and this run has "
            f"{len(messages)}. Without them the two clocks cannot be checked against "
            f"each other, only assumed equal."
        )
    device = messages["device_time_s"].to_numpy(dtype=float)
    session = messages["session_time_s"].to_numpy(dtype=float)
    slope, intercept = np.polyfit(device, session, 1)
    residuals = session - (slope * device + intercept)
    worst = float(np.max(np.abs(residuals)))
    if worst > tolerance_s:
        raise DataError(
            f"the device and session clocks do not fit a straight line: the worst of "
            f"{len(messages)} alignment marks is {worst * 1000:.2f} ms off the fit, past "
            f"the {tolerance_s * 1000:.2f} ms tolerance. Every latency in this run would "
            f"inherit that error. Check whether the session was paused or the device "
            f"re-clocked mid-run before analysing it."
        )
    return ClockFit(
        slope=float(slope), intercept=float(intercept), max_residual_s=worst, n_marks=len(messages)
    )


def event_times(messages: pd.DataFrame, event: str) -> pd.DataFrame:
    """Session times of one task event, one row per occurrence, with the trial
    it fell in.

    Events reach the tracker as their own name lowercased (``STIM_ON`` becomes
    ``stim_on``; devices/eyetracker/messages.py), and the backend opens each
    trial's segment with ``TRIAL <index> <status>``. Pairing the two is what
    assigns an event to a trial, and it is done here rather than by counting,
    so a trial that produced no such event simply has no row instead of
    shifting every later one by a place.
    """
    wanted = event.lower()
    trial_index = -1
    rows = []
    for _, row in messages.iterrows():
        text = str(row["message"])
        if text.startswith("TRIAL "):
            trial_index = int(text.split()[1])
        elif text == wanted:
            rows.append({"trial_index": trial_index, "t_session": float(row["session_time_s"])})
    return pd.DataFrame(rows, columns=["trial_index", "t_session"])


# ----------------------------------------------------------------------
# Loading helpers — each one fails loudly and says what to do about it
# ----------------------------------------------------------------------


def _one_file(run_dir: Path, pattern: str) -> Path:
    matches = sorted(run_dir.glob(pattern))
    if not matches:
        raise DataError(
            f"no file matching {pattern!r} in {run_dir}. A ViewPixx run writes "
            f"<base>_gaze.csv and <base>_gaze-messages.csv at teardown; if they are "
            f"missing, the session did not shut the tracker down cleanly."
        )
    if len(matches) > 1:
        raise DataError(
            f"{len(matches)} files match {pattern!r} in {run_dir}: "
            f"{', '.join(p.name for p in matches)}. A run directory holds one recording."
        )
    return matches[0]


def _read_messages(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    missing = [name for name in MESSAGE_COLUMNS if name not in frame.columns]
    if missing:
        raise DataError(
            f"{path} is missing {', '.join(missing)}; expected the header "
            f"{','.join(MESSAGE_COLUMNS)} written by alhazen's viewpixx backend"
        )
    return frame


def _eye_used(messages: pd.DataFrame, path: Path) -> str:
    """Which eye the session recorded, from the data rather than from a config."""
    marks = {
        str(text).split()[1] for text in messages["message"] if str(text).startswith("EYE_USED ")
    }
    if not marks:
        raise DataError(
            f"{path} contains no EYE_USED mark, so which eye these samples came from is "
            f"unknown. Pass eye= explicitly only if you can establish it another way."
        )
    if len(marks) > 1:
        raise DataError(
            f"{path} says the recorded eye changed mid-session ({', '.join(sorted(marks))}). "
            f"Split the run before analysing it; a single gaze table cannot mix two eyes."
        )
    return marks.pop()


def _normalise(name: str) -> str:
    """Strip everything but letters and digits, and lowercase the rest."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _resolve_columns(
    frame: pd.DataFrame, mapping: dict[str, str], path: Path, *, required: bool = True
) -> dict[str, str]:
    """Map this reader's names onto the file's actual headers.

    Matched on the normalised name rather than exactly, because VPixx owns
    this layout and the difference between ``Left Screen X``, ``\\tLeft
    Screen X`` and ``left_screen_x`` is a naming style, not a different
    measurement. Anything the normalisation cannot reach is an error naming
    both what was wanted and what the file actually holds — never a guess
    and never a silent skip — unless ``required`` is False, in which case
    the unmatched names are simply left out of the result.
    """
    available = {_normalise(column): column for column in frame.columns}
    resolved, missing = {}, {}
    for ours, theirs in mapping.items():
        actual = available.get(_normalise(theirs))
        if actual is None:
            missing[ours] = theirs
        else:
            resolved[ours] = actual
    if missing and required:
        raise DataError(
            f"{path} does not have the columns this reader expects: "
            + ", ".join(f"{theirs!r} (for {ours})" for ours, theirs in sorted(missing.items()))
            + f". The file has {', '.join(map(repr, frame.columns))}. VPixx owns this "
            f"layout, so confirm it against a recording and pass columns= for a name that "
            f"differs — do not guess."
        )
    return resolved


def _select_eye(
    frame: pd.DataFrame, mapping: dict[str, str], optional: dict[str, str], eye: str
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray | None]:
    """The eye's positions, its tracked mask, and its pupil if recorded.

    The device reports a lost eye as data — +/-9000 px or NaN — not as an
    absent sample, and separately flags blinks. Read as a position, 9000 px
    is a couple of hundred degrees off screen, and any downstream mean or
    velocity that touched it would be nonsense. The lost rule is imported
    rather than re-implemented so the online and offline definitions of a
    blink cannot diverge.
    """

    def one(side: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray | None]:
        x = frame[mapping[f"{side}_x_px"]].to_numpy(dtype=float)
        y = frame[mapping[f"{side}_y_px"]].to_numpy(dtype=float)
        lost = np.array([is_tracking_lost(a, b) for a, b in zip(x, y, strict=True)], dtype=bool)
        blink_column = optional.get(f"{side}_blink")
        if blink_column is not None:
            # The device's own blink flag: 1 during a blink. NaN (a file
            # without the flag, or an empty cell) is not a blink.
            blink = frame[blink_column].to_numpy(dtype=float)
            lost |= np.nan_to_num(blink, nan=0.0) == 1.0
        pupil_column = optional.get(f"{side}_pupil")
        pupil = frame[pupil_column].to_numpy(dtype=float) if pupil_column is not None else None
        return x, y, ~lost, pupil

    if eye in ("left", "right"):
        return one(eye)
    lx, ly, ltracked, lpupil = one("left")
    rx, ry, rtracked, rpupil = one("right")
    tracked = ltracked & rtracked
    pupil = None if lpupil is None or rpupil is None else (lpupil + rpupil) / 2.0
    return (lx + rx) / 2.0, (ly + ry) / 2.0, tracked, pupil


def _warn_if_nothing_tracked(tracked: np.ndarray, path: Path, eye: str) -> None:
    """Say so when a whole recording holds no usable gaze for this eye.

    Nearly always a device that was holding no calibration, and a reader who
    gets back a table of NaN deserves to be told why rather than left to work
    it out. Its own function, and called whether or not the bounds check is,
    because the two are different concerns: ``check_bounds=False`` means "I
    know this run legitimately goes off the panel", which is precisely the
    caller a vergence experiment is — and it must not also mean "do not tell
    me the recording is empty". It did, briefly, and that caller noticed.
    """
    if tracked.any():
        return
    log.warning(
        "no %s-eye sample in %s is tracked: every position is the device's lost "
        "sentinel or is flagged as a blink. On a TRACKPixx3 that is what a recording "
        "made with no calibration on the device looks like.",
        eye,
        path.name,
    )


def _check_on_panel(
    x_px: np.ndarray,
    y_px: np.ndarray,
    tracked: np.ndarray,
    screen: Screen,
    gaze_frame: GazeFrame,
    path: Path,
    eye: str,
) -> None:
    """Refuse a run whose tracked gaze does not lie on the panel it was
    recorded on, because that is what the wrong ``gaze_frame`` looks like.

    Positions arrive here already in centred px. Read in the wrong frame they
    are all displaced by half a panel — and displaced *together*, so the
    cluster stays as tight as ever, the clock fit stays as good as ever, and
    nothing else in this module has any reason to complain. That is why this
    check exists: not because off-panel gaze is impossible, but because a
    silently plausible answer is worse than a loud one.

    A run with no tracked samples at all says nothing about the frame, so it
    passes here; :func:`_warn_if_nothing_tracked` is what says so, and it is
    deliberately not this function's job — see there.
    """
    if not tracked.any():
        return
    x, y = x_px[tracked], y_px[tracked]
    off = (np.abs(x) > screen.width_px / 2.0) | (np.abs(y) > screen.height_px / 2.0)
    fraction = float(off.mean())
    if fraction <= OFF_PANEL_WARN:
        return
    other = "screen_y_down" if gaze_frame == "centered_y_up" else "centered_y_up"
    where = (
        f"{fraction:.0%} of the tracked {eye}-eye samples in {path.name} lie outside the "
        f"{screen.width_px}x{screen.height_px} px panel they were recorded on"
    )
    if fraction < OFF_PANEL_REFUSE:
        log.warning(
            "%s, read as %r. That is high enough to be worth a look: a subject who "
            "spent the run looking away, or a calibration extrapolating past the "
            "edges.",
            where,
            gaze_frame,
        )
        return
    raise DataError(
        f"{where}, read as gaze_frame={gaze_frame!r}. Almost certainly the frame: "
        f"the other one, {other!r}, moves every position by half a panel, and the "
        f"wrong one leaves the cluster looking perfectly tidy in the wrong place. "
        f"Read it as {other!r} if that is what the recording is, or pass "
        f"check_bounds=False once you have established that this gaze really was "
        f"off the panel."
    )


def _sample_period_s(device_t: np.ndarray, path: Path) -> float:
    if device_t.size < 2:
        raise DataError(f"{path} holds fewer than two samples")
    steps = np.diff(device_t)
    period = float(np.median(steps[steps > 0])) if np.any(steps > 0) else 0.0
    if period <= 0:
        raise DataError(
            f"{path} has no increasing timestamps, so its sample rate cannot be "
            f"established. Check that the device's own writer produced this file."
        )
    return period


def _screen_from_snapshot(run_dir: Path) -> Screen:
    """The run's own monitor geometry, so px2deg is the exact inverse of the
    deg2px that placed the stimulus.

    Read from the run directory rather than from a config on disk today: that
    is not necessarily the config that ran the session, and every position in
    degrees depends on this number.
    """
    path = run_dir / "config_snapshot.yaml"
    if not path.exists():
        raise DataError(
            f"{path} is missing, so the monitor geometry this run used is unknown and "
            f"pixels cannot be converted to degrees. Do not substitute a current config: "
            f"it may not be the one that ran."
        )
    snapshot = yaml.safe_load(path.read_text(encoding="utf-8"))
    try:
        monitor = snapshot["config"]["rig"]["monitor"]
    except (KeyError, TypeError) as e:
        raise DataError(f"{path} has no config.rig.monitor block: {e}") from e
    return Screen.from_monitor(MonitorConfig(**monitor))
