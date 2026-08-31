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
    """

    tracker: EyeTracker | None = None
    response: ResponseDevice | None = None
    task: Task | None = None
    # Free-form, recorded in the run's snapshot: what this simulated subject
    # was configured to do (its blink rate, its latency spread). It ends up in
    # the data, so a rehearsal's numbers can be read months later by someone
    # who no longer remembers how the autopilot was set up.
    describe: dict[str, Any] | None = None

    def is_empty(self) -> bool:
        return self.tracker is None and self.response is None and self.task is None
