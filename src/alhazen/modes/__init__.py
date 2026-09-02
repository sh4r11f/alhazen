"""The six ways to start an experiment.

Every experiment needs the same six, and before this package each one wrote
them again: a viewer for the stimulus, an autopilot for the session, a ruler
check for the display, a hand-edited copy of the config for a short run, a
movie writer for the lab meeting, and the session itself. The six below are
the same six, written once.

    measure   is this rig telling the truth? (display, keys, tracker)
    demo      look at the stimulus, with nothing else running
    movie     write the stimulus to files, for a demo you can send
    simulate  the whole session, with nobody in the chair
    test      the whole session, with a person in it and fewer trials
    run       the experiment

Three of them — simulate, test, run — are one code path with different
arguments, because that is the property that makes a rehearsal worth
anything: a test run that went through different wiring from the real session
would rehearse the wrong thing. What differs between them is the trial
counts, who supplies the gaze and the keypresses, and which directory the
data lands in. Nothing else.

The other three are their own programs, because none of them runs trials at
all — and one of them, movie, never even opens a window.

Every mode runs on every rig. A rig file describes a machine — its panel,
its devices, where its data goes — and says nothing about what you are about
to do on it; that is the mode's business. So a mode that cannot use what the
rig has (simulate, on a rig with a real tracker) substitutes for it and says
so, rather than asking for a second rig file with the device left out. Two
flags override the machine itself: ``--headless`` (simulate with no window,
for CI and ssh) and ``--mouse`` (test with the mouse cursor as gaze, on a rig
whose tracker is off). Each is honoured by exactly one mode and refused by
name everywhere else — see :func:`flag_refusal`.
"""

from __future__ import annotations

from enum import Enum


class Mode(str, Enum):
    """How a session was started. Recorded in the run's snapshot, so data can
    never be read back without knowing which of these produced it."""

    MEASURE = "measure"
    DEMO = "demo"
    MOVIE = "movie"
    SIMULATE = "simulate"
    TEST = "test"
    RUN = "run"

    @property
    def writes_real_data(self) -> bool:
        """Whether this mode's output belongs in the rig's own data root.

        Only ``run`` does. Test and simulate write real files in real formats
        — that is what makes them useful to rehearse an analysis with — and
        that is exactly why they must not land where the analysis looks for
        subjects.
        """
        return self is Mode.RUN

    @property
    def runs_trials(self) -> bool:
        return self in (Mode.SIMULATE, Mode.TEST, Mode.RUN)

    @property
    def summary(self) -> str:
        return MODE_SUMMARIES[self]


MODE_SUMMARIES = {
    Mode.MEASURE: "measure what this rig actually does: display, response keys, eye tracker",
    Mode.DEMO: "look at the stimulus, with no trials and no data",
    Mode.MOVIE: "write the stimulus to movie files, for a demo you can send",
    Mode.SIMULATE: "the whole session, driven by a simulated subject",
    Mode.TEST: "the whole session with fewer trials, for a person to sit through once",
    Mode.RUN: "the experiment",
}

# Why each of the other five modes cannot take --headless. Spelled out per
# mode rather than as one generic refusal: "demo cannot run headless" is
# obvious, but "test cannot" is a question, and the answer is the point.
_NOT_HEADLESS = {
    Mode.MEASURE: "measure mode measures the real panel, so there has to be one",
    Mode.DEMO: "demo mode exists to look at the stimulus on a real panel",
    Mode.MOVIE: "movie mode never opens a window, so there is nothing to make headless",
    Mode.TEST: "test mode is for a person to sit through, and a person needs a window",
    Mode.RUN: "run mode records real data, and a subject needs a window",
}

# Why each of the other five modes cannot take --mouse.
_NOT_MOUSE = {
    Mode.MEASURE: "measure mode measures the rig's own tracker, and the mouse is not one",
    Mode.DEMO: "demo mode reads no gaze",
    Mode.MOVIE: "movie mode reads no gaze",
    Mode.SIMULATE: "simulate mode's gaze comes from the task's own autopilot",
    Mode.RUN: "run mode records real data, so its gaze must come from the rig's tracker",
}


def flag_refusal(mode: Mode, *, headless: bool = False, mouse: bool = False) -> str | None:
    """Why ``mode`` cannot honour the flags asked for — or None when it can.

    ``--headless`` belongs to simulate alone and ``--mouse`` to test alone.
    The check is one function, called by the command line before anything
    loads and by ``build_mode_session`` before anything is wired, so a flag
    a mode cannot honour is refused with the reason and never silently
    ignored: an experimenter who typed ``--headless`` and got a window would
    not know which of the two the data came from.
    """
    if headless and mode is not Mode.SIMULATE:
        return f"--headless: only simulate mode runs without a window — {_NOT_HEADLESS[mode]}"
    if mouse and mode is not Mode.TEST:
        return f"--mouse: only test mode takes the mouse cursor as gaze — {_NOT_MOUSE[mode]}"
    return None


__all__ = ["MODE_SUMMARIES", "Mode", "flag_refusal"]
