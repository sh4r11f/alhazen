"""What a simulated run puts in the subject's chair.

Both experiments alhazen was built for needed one, and neither could use
``devices.automated.AutomatedGazeTracker``: that one looks at the same pixel
on every trial, which proves the machinery turns over and gives the analysis
nothing to work on. Each wrote its own — one substituting a tracker, the other
substituting a tracker, a response device *and* the task, because its
autopilot has to know which way the answer should go.

So the seam is a small record of what to substitute, rather than a base class
with behaviour in it. Where a simulated subject looks and how it answers is
the experiment's own question, and the two experiments' answers have almost
nothing in common; what they do have in common is the wiring, and that is
what this holds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import cycle: Task imports this module
    from alhazen.devices.eyetracker import EyeTracker
    from alhazen.devices.response import ResponseDevice
    from alhazen.devices.spikes import SpikeSource
    from alhazen.task.task import Task


@dataclass(frozen=True)
class Simulation:
    """The stand-ins for one simulated session.

    Every field is optional and ``None`` means "leave the rig's own". A task
    whose trials are gaze-contingent supplies a ``tracker``; one whose subject
    also presses keys supplies a ``response``; one whose simulated answers
    depend on what the trial was asking supplies a ``task`` as well — a
    subclass that knows the right answer, which is the only way an autopilot
    can be scored rather than just counted.

    ``spikes`` is the same idea one layer in: an experiment whose objective
    is computed from neural activity cannot rehearse itself with gaze alone,
    and simulated neurons that respond to *this* trial's stimulus are
    something only the experiment can build. The rig's own probe still
    stands down in simulate mode; this is what runs in its place.
    """

    tracker: EyeTracker | None = None
    response: ResponseDevice | None = None
    task: Task | None = None
    spikes: SpikeSource | None = None
    # Free-form, recorded in the run's snapshot: what this simulated subject
    # was configured to do (its blink rate, its latency spread). It ends up in
    # the data, so a rehearsal's numbers can be read months later by someone
    # who no longer remembers how the autopilot was set up.
    describe: dict[str, Any] | None = None

    def is_empty(self) -> bool:
        """Whether the task supplied no stand-in at all.

        The question simulate mode asks, and it is about the task having
        implemented ``simulation()`` rather than about the subject being
        complete: a partial subject is the experiment's business, a missing
        one is a mode that cannot run.
        """
        return (
            self.tracker is None
            and self.response is None
            and self.task is None
            and self.spikes is None
        )


# ----------------------------------------------------------------------
# The choice made at launch: which devices to stand in for, and who plays
# the subject (docs/design/simulate-as-a-choice.md)
# ----------------------------------------------------------------------

# What ``--simulate`` can name, in the order a session's notes list them.
# Each is a kind of device a rig file configures under ``devices``; the
# display is not one of them (``--headless`` takes the window away).
SIMULATABLE = ("tracker", "reward", "sync", "recording", "spikes")

# ``--simulate all``: every one of the above.
ALL = "all"

# Who supplies gaze (and answers) when the eye tracker is simulated. There is
# no default on purpose: watching a task play itself and trying it by hand
# are different things to want, and a guess would hand over the wrong one.
GAZE_AUTOPILOT = "autopilot"
GAZE_MOUSE = "mouse"
GAZE_SOURCES = (GAZE_AUTOPILOT, GAZE_MOUSE)


@dataclass(frozen=True)
class SimulateChoice:
    """Which devices one launch stands in for, and who plays the subject.

    ``devices`` holds names from `SIMULATABLE`, never ``"all"`` (`parse`
    spells that out). ``gaze`` is `GAZE_AUTOPILOT` or `GAZE_MOUSE` when the
    tracker is among them and None otherwise — `parse` refuses every other
    combination, so code that holds one of these never has to check.

    Nothing is simulated that is not named here: a device that fails to
    connect is refused, in every mode, and never quietly replaced.
    """

    devices: frozenset[str] = frozenset()
    gaze: str | None = None

    def __bool__(self) -> bool:
        """Whether anything is simulated at all — which is what makes a
        launch a rehearsal, whatever its mode."""
        return bool(self.devices)

    @property
    def autopilot(self) -> bool:
        """Whether the task's own autopilot plays the subject: nobody is in
        the chair, so nobody is asked for a subject id, shown instructions
        to start from, or waited for at a break."""
        return self.gaze == GAZE_AUTOPILOT

    def names(self) -> list[str]:
        """The simulated devices in `SIMULATABLE` order, as session.json
        records them."""
        return [name for name in SIMULATABLE if name in self.devices]

    @classmethod
    def parse(cls, simulate: str | None, gaze: str | None) -> SimulateChoice:
        """``--simulate`` and ``--gaze`` as typed, checked. Raises ValueError
        with the words for the person who typed them.

        ``simulate`` is comma-separated names from `SIMULATABLE`, or ``all``;
        None or empty simulates nothing. ``gaze`` is required exactly when
        the tracker is simulated.
        """
        names = [name.strip().lower() for name in (simulate or "").split(",")]
        names = [name for name in names if name]
        unknown = sorted(set(names) - set(SIMULATABLE) - {ALL})
        if unknown:
            raise ValueError(
                f"--simulate {simulate!r}: {', '.join(unknown)} is not something a rig "
                f"simulates. Choose from {', '.join(SIMULATABLE)}, or {ALL}."
            )
        devices = frozenset(SIMULATABLE if ALL in names else names)
        if gaze is not None and gaze not in GAZE_SOURCES:
            raise ValueError(f"--gaze {gaze!r}: choose {' or '.join(GAZE_SOURCES)}")
        if "tracker" in devices and gaze is None:
            raise ValueError(
                "--simulate names the eye tracker, so say who supplies gaze: "
                f"--gaze {GAZE_AUTOPILOT} (the task plays itself, nobody in the chair) or "
                f"--gaze {GAZE_MOUSE} (you play it, with the mouse cursor as your eye)"
            )
        if gaze is not None and "tracker" not in devices:
            raise ValueError(
                f"--gaze {gaze} chooses who stands in for the eye tracker, and this command "
                "does not simulate it: add --simulate tracker (or --simulate all)"
            )
        return cls(devices=devices, gaze=gaze)


# Simulating nothing: every device is the rig's own.
NOTHING_SIMULATED = SimulateChoice()
