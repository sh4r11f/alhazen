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


__all__ = ["MODE_SUMMARIES", "Mode"]
