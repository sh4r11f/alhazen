"""ProcedureControl: the live monitor's hold on a running eye-tracker procedure.

An eye-tracker procedure (a calibration, a validation, a drift correction)
takes the subject's screen over and walks its targets, reading the
experimenter's keys between frames. Before this, nothing on the live monitor
could reach it: the page refused every button while a procedure ran, and its
Trial view went on showing the last trial. This is what lets the page take
part, for as long as one procedure runs:

- **the walk's keys from the page.** Resume, Back and Pause on the page are
  SPACE, BACKSPACE and the walk's stop key at the rig. The session offers them
  to the page when the procedure starts (``OperatorCommands.offer``), the walk
  reads them between its own key reads — a tracker that draws its own walk
  through ``set_command_source`` (devices/eyetracker/protocol.py), the shared
  validation and drift-correction walks through the keys merged into their key
  source (``walk_keys``) — and each is acknowledged as it is taken. A
  calibration a tracker runs elsewhere (the EyeLink's, on its setup screen)
  reads none of them, and the page says so instead of offering buttons that
  would do nothing.
- **the Trial view's picture.** The procedure's targets in dva, the one on the
  subject's screen now highlighted, published on the Trial view's channel at
  the start, at every new target and at the end (``progress``). The camera
  streams through all of it (session/eyetracker.py), and the Trial view shows
  it as live.
- **gaze, where a calibrated one exists.** A validation and a drift
  correction measure gaze through the current calibration; their samples are
  sent to the Trial view as a trial's are (live_monitor/gaze.py), so the
  operator sees the eye land on each target. A calibration's walk has no model
  to read gaze through yet, and the picture says so.

Interface: ``begin``, ``end``, ``progress``, ``walk_source``, ``walk_keys``,
``gaze_tracker``. Callers must not rely on how many pictures a procedure
publishes, only on one at its start, one per new target and one at its end.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection
from typing import Any

from alhazen.config.models import SELF_DRIVEN_CALIBRATION_TYPES, EyeTrackerConfig
from alhazen.core.clock import Clock
from alhazen.core.trial import InputFrame
from alhazen.devices.eyetracker.procedures import (
    ABORT_KEY,
    ACCEPT_KEYS,
    REDO_KEY,
    GazeCorrection,
    validation_targets,
)
from alhazen.display.schematic import Circle
from alhazen.display.screen import Screen
from alhazen.live_monitor.gaze import GazeFeed, GazeSink, GazeSwitch
from alhazen.session.operator_commands import Offer, OperatorCommands
from alhazen.session.procedure_guide import Kind

log = logging.getLogger(__name__)

# The status a procedure publishes under (session/eyetracker.py PROCEDURE_STATUS).
STATUS = "calibrating"

TITLES: dict[str, str] = {
    "calibration": "Calibration",
    "validation": "Validation",
    "drift_correction": "Drift correction",
}

# Each page command as the walk reads it: the key that means the same at the
# rig. The shared walks (procedures.py) stop on ESC; a tracker's own walk
# maps the names to its own keys (set_command_source).
WALK_KEYS = {"resume": ACCEPT_KEYS[0], "back": REDO_KEY, "pause": ABORT_KEY}

# What each acknowledgement says, as the walk will act on it.
_NOTES = {
    "resume": "SPACE for the walk: starts it at the guide, or accepts the target on screen",
    "back": "BACKSPACE for the walk: back one target",
    "pause": "the walk stops; the previous calibration and gaze model are kept",
}
# The key each page command stands for at the rig, as the page prints it.
_PAGE_KEYS = {"resume": "space", "back": "backspace", "pause": "escape"}

# The radius the targets are marked with, in dva: a position marker, not the
# target's size (the note under the picture says so).
MARKER_DVA = 0.35

CameraState = Callable[[], dict[str, Any]]
ViewSink = Callable[[dict[str, Any]], object]


class _GazeTee:
    """A tracker that also hands every gaze position it reports to the Trial
    view: what a validation or drift correction reads, read once.

    Everything but ``get_gaze`` is the tracker's own (``__getattr__``), so a
    procedure that starts and stops a recording segment, or asks for an
    optional capability with ``hasattr``, meets exactly the tracker."""

    def __init__(
        self,
        tracker: Any,
        feed: GazeFeed,
        screen: Screen,
        correction: GazeCorrection | None,
        trial_index: int,
        clock: Clock,
    ) -> None:
        self._tracker = tracker
        self._feed = feed
        self._screen = screen
        self._correction = correction
        self._trial_index = trial_index
        self._clock = clock
        self._frame = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tracker, name)

    def get_gaze(self) -> Any:
        sample = self._tracker.get_gaze()
        gaze: tuple[float, float] | None = None
        gaze_t: float | None = None
        if sample is not None:
            # The conversion the walk itself makes (procedures.py gaze_now),
            # so the page shows what the measurement used.
            gaze = self._screen.screen_to_centered(sample.gx, sample.gy)
            if self._correction is not None:
                gaze = self._correction.apply(gaze)
            gaze_t = sample.t
        self._frame += 1
        self._feed.note(
            self._trial_index, self._frame, self._clock.now(), InputFrame(gaze=gaze, gaze_t=gaze_t)
        )
        return sample


class ProcedureControl:
    """See the module docstring. Built by the runner when a live monitor is
    open, and handed to the session's EyeTrackerMonitor."""

    def __init__(
        self,
        *,
        operator: OperatorCommands,
        screen: Screen,
        clock: Clock,
        publish_view: ViewSink | None,
        camera: CameraState,
        trial_index: Callable[[], int],
        gaze_sink: GazeSink | None = None,
        gaze_switch: GazeSwitch | None = None,
    ) -> None:
        self._operator = operator
        self._screen = screen
        self._clock = clock
        self._publish_view = publish_view
        self._camera = camera
        self._trial_index = trial_index
        self._gaze_sink = gaze_sink
        self._gaze_switch = gaze_switch
        self._kind: str | None = None
        self._id = 0.0
        self._positions: list[tuple[float, float]] = []
        self._gaze = False
        self._reads_page = False
        self._last: tuple[Any, ...] | None = None

    # ------------------------------------------------------------------
    # The procedure's start and end
    # ------------------------------------------------------------------

    def begin(self, kind: Kind, tracker: Any, cfg: EyeTrackerConfig) -> None:
        """A procedure starts: offer the page its keys (when its walk reads
        them) and publish its picture. Called before its first publish."""
        self._kind = kind
        self._id = self._clock.now()
        self._last = None
        if kind == "calibration":
            # Only a walk alhazen draws reads the page, and only its targets
            # are known here: the EyeLink lays its own out on its Host PC.
            self._reads_page = hasattr(tracker, "set_command_source")
            self._positions = _calibration_positions(cfg, self._screen) if self._reads_page else []
            self._gaze = False
        elif kind == "validation":
            self._reads_page = True
            self._positions = list(validation_targets(cfg, self._screen))
            self._gaze = self._gaze_sink is not None
        else:
            self._reads_page = True
            self._positions = [(0.0, 0.0)]
            self._gaze = self._gaze_sink is not None
        title = TITLES[kind].lower()
        if self._reads_page:
            notes = dict(_NOTES)
            keys = dict(_PAGE_KEYS)
            if kind == "calibration":
                keys["pause"] = "p"
            self._operator.offer(Offer(STATUS, notes, keys, f"the {title}"))
        else:
            self._operator.offer(
                Offer(
                    STATUS,
                    {},
                    {},
                    f"the {title}",
                    none_because=(
                        "this tracker runs its calibration on its own setup screen, which does "
                        "not read the live monitor; use the keys at the rig"
                    ),
                )
            )
        self.progress(None, None, "starting")

    def end(self, verdict: str) -> None:
        """The outermost procedure has ended: the page's keys are withdrawn
        (anything still sent to it is discarded and acknowledged so), and the
        picture says how it ended."""
        if self._kind is None:
            return
        self._operator.withdraw(f"the {TITLES[self._kind].lower()} had ended; not acted on")
        self._publish(None, None, verdict or "ended", ended=True)
        self._kind = None

    def progress(self, step: int | None, n: int | None, line: str) -> None:
        """A report from the running procedure: its picture is republished
        when the target or the line changed."""
        if self._kind is None:
            return
        self._publish(step, n, line)

    # ------------------------------------------------------------------
    # The page's keys, for the walks
    # ------------------------------------------------------------------

    def walk_source(self, accepted: Collection[str]) -> list[str]:
        """For a tracker's own walk (``set_command_source``): the page
        commands it can act on now, by name ("resume", "back", "pause"),
        oldest first, each acknowledged as it is taken."""
        if self._kind is None or not self._reads_page:
            return []
        return self._operator.take_offered(accepted)

    def walk_keys(self) -> list[str]:
        """For the shared walks (procedures.py): the page's commands as the
        keys they stand for, to merge with the keyboard's."""
        return [WALK_KEYS[name] for name in self.walk_source(tuple(WALK_KEYS))]

    def gaze_tracker(self, tracker: Any, correction: GazeCorrection | None) -> Any:
        """The tracker a validation or drift correction should read: one that
        also feeds the Trial view, or the tracker itself when there is no gaze
        channel."""
        if not self._gaze or self._gaze_sink is None:
            return tracker
        feed = GazeFeed(self._gaze_sink, self._screen.px_per_deg, enabled=self._gaze_switch)
        return _GazeTee(tracker, feed, self._screen, correction, self._trial_index(), self._clock)

    # ------------------------------------------------------------------
    # The Trial view's picture
    # ------------------------------------------------------------------

    def _publish(self, step: int | None, n: int | None, line: str, *, ended: bool = False) -> None:
        if self._publish_view is None or self._kind is None:
            return
        key = (self._kind, step, line, ended)
        if key == self._last:
            return
        self._last = key
        try:
            self._publish_view(self.view(step, n, line, ended=ended))
        except ValueError as error:
            # A picture that cannot be built (a target off any finite
            # screen) must never stop a calibration; said once per publish.
            log.warning("Trial view: the %s picture was not drawn: %s", self._kind, error)

    def view(
        self, step: int | None, n: int | None, line: str, *, ended: bool = False
    ) -> dict[str, Any]:
        """The Trial view's payload for the running procedure."""
        assert self._kind is not None
        deg = self._screen.px2deg
        shapes = []
        current = step - 1 if step is not None and not ended else None
        for index, (x, y) in enumerate(self._positions):
            on = index == current
            shapes.append(
                Circle(
                    (deg(x), deg(y)),
                    MARKER_DVA,
                    name="Target on screen" if on else "Target",
                    role="stimulus" if on else "annotation",
                ).payload()
            )
        return {
            "trial_index": self._trial_index(),
            "attempt": 0,
            "t": self._clock.now(),
            "screen": {
                "width_dva": deg(self._screen.width_px),
                "height_dva": deg(self._screen.height_px),
            },
            "source": "procedure",
            "shapes": shapes,
            "undrawn": [],
            "condition": {},
            "procedure": {
                "id": self._id,
                "kind": self._kind,
                "title": TITLES[self._kind],
                "step": step if not ended else None,
                "n": n if n is not None else (len(self._positions) or None),
                "line": line,
                "ended": ended,
                "targets_known": bool(self._positions),
                "reads_page": self._reads_page,
            },
            "gaze": self._gaze,
            "camera": self._camera(),
        }


def _calibration_positions(cfg: EyeTrackerConfig, screen: Screen) -> list[tuple[float, float]]:
    """The calibration walk's targets in its order, centered px — the layout
    the TRACKPixx3 and the mouse stand-in walk (viewpixx.calibration_targets),
    or none for a layout alhazen cannot lay out."""
    if cfg.calibration_type not in SELF_DRIVEN_CALIBRATION_TYPES:
        return []
    from alhazen.devices.eyetracker.viewpixx import calibration_targets

    return list(calibration_targets(cfg.calibration_type, screen, cfg.calibration_area))
