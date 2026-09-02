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
