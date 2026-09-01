"""The live-analysis seam: computation that watches a session as it runs.

An experiment sometimes needs more than the dashboard's trial-record panels
— a receptive-field map accumulating over a spike stream, a running PSTH, a
tuning curve. That computation has three needs the trial machinery must not
absorb: it consumes a *device* (a spike source), it produces *dashboard
panels* of its own, and it leaves an *artifact* in the run directory. This
module is the narrow contract for all three.

The rules that keep it safe:

- **Never inside the frame loop.** ``on_event`` (optional) is a bus
  subscriber and runs mid-trial, so it may only *note* things — append to a
  list, nothing more. All real work happens in ``on_trial``, which the
  runner calls between trials, where a slow computation costs ITI rather
  than a dropped frame.
- **The builder wires it, exactly like a device** (architecture §4): the
  task's ``live_analysis`` hook receives a :class:`LiveWiring` with the
  spike source the *rig config* built. Task code never constructs hardware,
  and a rig with no ``spikes:`` entry hands over ``spikes=None`` — the
  analysis then says so on its panels instead of silently showing nothing.
- **Panels are computed here, drawn by the browser** — the same division as
  every dashboard panel (dashboard/panels.py): each entry of ``panels()``
  is a finished payload the page only renders.
- **``finish`` runs in teardown, before the manifest is written**, so
  whatever it saves into the run directory is hashed with everything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from alhazen.core.clock import Clock
from alhazen.devices.spikes import SpikeSource
from alhazen.display.screen import Screen


@dataclass(frozen=True)
class LiveWiring:
    """What the builder hands a task's live analysis: the wired devices and
    session geometry it may consume. ``spikes`` is None on a rig that
    configures no spike source — the analysis must handle that by saying
    so, not by crashing and not by staying quiet."""

    spikes: SpikeSource | None
    screen: Screen
    clock: Clock


@runtime_checkable
class LiveAnalysis(Protocol):
    """What the session runner drives. Implementations may also define
    ``on_event(event)`` — the builder subscribes it to the bus when present,
    for cheap mid-trial note-taking only."""

    def on_trial(self, record: dict[str, Any]) -> None:
        """Called between trials with the scored record of every non-PAUSED
        trial, after it was written. The place for the real work."""
        ...

    def panels(self) -> list[dict[str, Any]]:
        """Extra dashboard panels, each ``{"title", "section", "data"}`` with
        ``data`` a finished payload in the dashboard's wire shapes."""
        ...

    def finish(self, run_dir: Path) -> None:
        """Teardown: flush and save artifacts into the run directory. Runs
        before the manifest is written, so what it saves is covered."""
        ...
