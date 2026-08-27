"""TTL sync outputs: digital pulses that make an external recording alignable.

A pulse on a digital line, recorded by whatever records the neural/physio
data, is the only thing that ties a task event to a sample index in that
recording. A pulse that never fires cannot be reconstructed afterwards — so
nothing here fails quietly.

The division of labour: this module knows about *lines*, and
:func:`make_sync_subscriber` is the single place that knows which event maps
to which line. The rig config's ``event_lines`` is the source of truth for
what reaches hardware at all; an event with no entry pulses nothing.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from alhazen.config.models import SyncHwConfig
from alhazen.core.events import Event
from alhazen.errors import SyncError

log = logging.getLogger(__name__)


@runtime_checkable
class SyncOutput(Protocol):
    def pulse(self, line: str) -> None: ...

    def close(self) -> None: ...


class SimulatedSync:
    """Records pulses instead of driving lines.

    ``pulses`` is the ordered list of lines pulsed. Their times are not
    recorded here on purpose: the event that triggered each pulse is already
    in ``events.csv``, stamped on the same session clock, so a second copy
    could only ever disagree with it.
    """

    def __init__(self, event_lines: dict[str, str]) -> None:
        self._lines = set(event_lines.values())
        self.pulses: list[str] = []

    def pulse(self, line: str) -> None:
        if line not in self._lines:
            raise SyncError(
                f"no sync line {line!r} was configured; configured lines: {sorted(self._lines)}"
            )
        self.pulses.append(line)

    def close(self) -> None:
        return  # nothing was opened


class NullSync:
    """Sync turned off: every pulse is accepted and does nothing.

    Not a SimulatedSync with an empty map. That was the old implementation,
    and its ``pulse()`` *raises* on an unconfigured line — so a perfectly
    valid rig config (keep ``event_lines``, set ``backend: none`` because
    today's session has no recording attached) built successfully and then
    died on the first mapped event of trial 1.

    Nothing is recorded either, so there is no ``pulses`` list a test or a
    reader could mistake for evidence that a line fired.
    """

    def pulse(self, line: str) -> None:
        log.debug("sync disabled; not pulsing %s", line)

    def close(self) -> None:
        return  # nothing was opened


class NidaqSync:
    """One digital-output task per configured line, pulsed on demand."""

    def __init__(self, cfg: SyncHwConfig) -> None:
        try:
            # Lazy for the same reason as the reward backend: nidaqmx is
            # rig-only, and `import alhazen` must work without it.
            import nidaqmx
            from nidaqmx.constants import LineGrouping
        except ImportError as e:
            # Loud, never a silent no-op: a neurally-recorded session with no
            # alignment pulses is discovered only when someone tries to align
            # it, by which point the data cannot be rescued.
            raise SyncError(
                "nidaqmx is not installed — install alhazen's [nidaq] extra on the rig, or "
                "use sync backend 'simulated' / 'none'"
            ) from e

        self._pulse_s = cfg.pulse_ms / 1000.0
        self._tasks: dict[str, Any] = {}
        line = ""
        try:
            for line in sorted(set(cfg.event_lines.values())):
                task = nidaqmx.Task()
                # Registered BEFORE it is configured: add_do_chan/write below
                # can raise (a typo'd line name, a line the device does not
                # have), and a task created but never recorded here could
                # never be released by close() — a leaked NI handle holds the
                # line until process exit and blocks the next session.
                self._tasks[line] = task
                task.do_channels.add_do_chan(line, line_grouping=LineGrouping.CHAN_PER_LINE)
                # Drive low at setup: a line left high at task creation looks
                # like a spurious pulse to the recording system before the
                # session has even started.
                task.write(False)
        except nidaqmx.DaqError as e:
            self.close()  # release everything opened so far, including this one
            raise SyncError(f"sync line setup failed for line {line!r}: {e}") from e

    def pulse(self, line: str) -> None:
        task = self._tasks.get(line)
        if task is None:
            raise SyncError(
                f"no sync line {line!r} was configured; configured lines: {sorted(self._tasks)}"
            )
        import nidaqmx

        try:
            task.write(True)
            time.sleep(self._pulse_s)
            task.write(False)
        except nidaqmx.DaqError as e:
            raise SyncError(f"sync pulse on line {line!r} failed: {e}") from e

    def close(self) -> None:
        """Release every task, independently and idempotently.

        Called from two places where an escaping exception would do harm:
        this class's own constructor while a SyncError is already in flight,
        and session teardown, which may be unwinding something more important.
        So a failure on one line is logged and the rest are still released.
        """
        import nidaqmx

        for line, task in self._tasks.items():
            try:
                # Zero before releasing, same rule as the reward waveform's
                # trailing zero: a task closed with its line high can latch a
                # stray pulse into the recording after the session ended.
                task.write(False)
            except nidaqmx.DaqError as e:
                log.error("sync: could not zero line %s before closing: %s", line, e)
            try:
                task.close()
            except nidaqmx.DaqError as e:
                log.error("sync: could not close the DAQ task for line %s: %s", line, e)
        self._tasks.clear()


def make_sync_subscriber(sync: SyncOutput, event_lines: dict[str, str]) -> Callable[[Event], None]:
    """Bus subscriber that pulses the line an event is mapped to.

    Unmapped events are a silent no-op: the config decides which events reach
    hardware, so "no line for STIM_ON" is a rig statement, not a mistake.
    Pulse failures are *not* caught (invariant 6) — a dead sync line means
    every later alignment mark is suspect, which must stop the session.
    """
    lines = dict(event_lines)

    def on_event(event: Event) -> None:
        line = lines.get(event.name)
        if line is not None:
            sync.pulse(line)

    return on_event


def make_sync(cfg: SyncHwConfig) -> SyncOutput:
    """Construct the sync output a rig config names.

    ``none`` really is none: a no-op that accepts any line. A rig may keep its
    ``event_lines`` map and disable sync for a session with no recording
    attached, and that config has to run.
    """
    if cfg.backend == "nidaq":
        return NidaqSync(cfg)
    if cfg.backend == "none":
        return NullSync()
    return SimulatedSync(cfg.event_lines)
