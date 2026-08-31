"""Measurement mode: is this rig telling the truth?

Every number an experiment reports rests on three claims the rig makes about
itself, and none of them is checked by anything else:

- the display refreshes at the rate the config names, and a flip lands when
  it says it does;
- a degree of visual angle is the size the config's geometry implies;
- a keypress arrives soon enough, and consistently enough, that a reaction
  time means something;
- the eye tracker's gaze position is where the eye actually is.

Each is measured here, on the real rig, through the same code a session uses.
What comes out is a report — printed, and written next to the rig config —
that says what was measured and, where there is a right answer, whether this
machine gives it.

**On honesty about what is measured.** Response latency measured by flipping
a marker and waiting for a key includes the display's own latency, the
subject's reaction time and the input path, and no amount of arithmetic
separates them without hardware alhazen does not have. So this measures the
two things it *can* isolate and says exactly what each one is: the loop's own
polling lag (pure software, no human involved, and the only part alhazen
controls), and the full flip-to-key distribution (which is dominated by the
human). Where a photodiode is configured, the marker flips the patch too, so
the recording holds the ground truth for the display half and the report says
how to read it back. A single number claiming to be "the" key latency would
be a number nobody could act on.

The statistics are plain functions with no hardware in sight and are unit
tested directly; the loops that touch a display, a keyboard or a tracker are
thin wrappers around them.
"""

from __future__ import annotations

import json
import math
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alhazen.config.models import RigConfig
from alhazen.display.screen import Screen

# How many flips to time the display over. Two seconds at 60 Hz: long enough
# for a dropped frame to show up, short enough that nobody walks away.
DEFAULT_FLIPS = 120
# How many keypresses to ask for. Ten is enough to see a spread without
# turning a rig check into a psychophysics session.
DEFAULT_PRESSES = 10
# Where accuracy targets go, in degrees: centre plus the four corners of a
# square. Five points is what fits in the time an experimenter will give it.
DEFAULT_ACCURACY_TARGETS_DVA = ((0.0, 0.0), (-8.0, 8.0), (8.0, 8.0), (-8.0, -8.0), (8.0, -8.0))


@dataclass(frozen=True)
class Measurement:
    """One thing measured.

    ``ok`` is None for a measurement with no right answer — a latency
    distribution is a fact about the rig, not a pass or a fail, and reporting
    it as "OK" would invite it to be skimmed past.
    """

    name: str
    summary: str
    ok: bool | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        mark = {True: "OK  ", False: "FAIL", None: "--  "}[self.ok]
        return f"{mark} {self.name}: {self.summary}"


@dataclass
class MeasurementReport:
    rig_path: str
    measurements: list[Measurement] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """False only if something with a right answer gave the wrong one."""
        return all(m.ok for m in self.measurements if m.ok is not None)

    def render(self) -> str:
        lines = [f"rig measurements — {self.rig_path}", ""]
        lines += [m.render() for m in self.measurements]
        for m in self.measurements:
            if m.detail.get("notes"):
                lines += ["", f"{m.name}:"] + [f"  {n}" for n in m.detail["notes"]]
        return "\n".join(lines)

    def save(self, path: Path | str) -> Path:
        """Write the whole thing as JSON, beside the rig config.

        JSON as well as the printed text because these numbers are worth
        comparing over time: a panel that has drifted, or a tracker that is
        worse than it was last month, is only visible against the last
        measurement.
        """
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "rig": self.rig_path,
                    "ok": self.ok,
                    "measurements": [
                        {"name": m.name, "ok": m.ok, "summary": m.summary, **m.detail}
                        for m in self.measurements
                    ],
                },
                indent=2,
                default=str,
            )
        )
        return out


# ----------------------------------------------------------------------
# The statistics. No hardware here on purpose — this is the part that can be
# wrong in a way nobody notices, so it is the part that is tested directly.
# ----------------------------------------------------------------------


def summarise(values: Sequence[float]) -> dict[str, float]:
    """Median and spread of a sample, in the units it came in.

    Median and inter-quartile range rather than mean and SD: every
    distribution here is bounded below and has a long right tail (a frame
    that was late, a press that was slow), and a mean reports the tail as if
    it were the typical case.
    """
    if not values:
        raise ValueError("nothing to summarise")
    ordered = sorted(values)
    if len(ordered) == 1:
        return {"n": 1, "median": ordered[0], "iqr": 0.0, "min": ordered[0], "max": ordered[0]}
    quartiles = statistics.quantiles(ordered, n=4, method="inclusive")
    return {
        "n": len(ordered),
        "median": statistics.median(ordered),
        "iqr": quartiles[2] - quartiles[0],
        "min": ordered[0],
        "max": ordered[-1],
    }


def frame_timing(intervals: Sequence[float], nominal_hz: float, tolerance: float = 0.5) -> dict:
    """What a run of flip-to-flip intervals says about the display.

    A frame counts as dropped when its interval exceeds the expected one by
    more than ``tolerance`` — the same rule ``display.frames.FrameMonitor``
    applies during a session, so a rig that measures clean here and drops
    frames in a session is telling you something about the experiment's own
    per-frame work rather than about the panel.
    """
    if not intervals:
        raise ValueError("no intervals to judge")
    expected = 1.0 / nominal_hz
    dropped = [i for i in intervals if i > expected * (1.0 + tolerance)]
    stats = summarise(intervals)
    return {
        **stats,
        "measured_hz": 1.0 / stats["median"],
        "nominal_hz": nominal_hz,
        "expected_interval_s": expected,
        "n_dropped": len(dropped),
        "dropped_fraction": len(dropped) / len(intervals),
    }


def accuracy(
    targets: Sequence[tuple[float, float]],
    gaze: Sequence[tuple[float, float]],
    screen: Screen,
) -> dict:
    """Per-target gaze error in degrees, given centred-pixel coordinates.

    This is a *validation*, not a calibration: the tracker has already been
    calibrated, and what is asked here is how far off it still is. That
    number is the one that decides whether a 2-degree fixation window is
    generous or impossible on this rig today.
    """
    if len(targets) != len(gaze):
        raise ValueError(f"{len(targets)} targets but {len(gaze)} gaze samples")
    if not targets:
        raise ValueError("no targets to check")
    errors = [
        screen.px2deg(math.dist(target, sample))
        for target, sample in zip(targets, gaze, strict=True)
    ]
    return {
        **summarise(errors),
        "per_target_dva": [
            {"target_px": list(t), "gaze_px": list(g), "error_dva": e}
            for t, g, e in zip(targets, gaze, errors, strict=True)
        ],
    }


def judge_refresh(timing: dict, tolerance_hz: float = 1.0) -> tuple[bool, str]:
    """Does the panel refresh at the rate the config claims?

    This one has a right answer, because every duration an experiment
    specifies in frames is converted through that number.
    """
    off_by = abs(timing["measured_hz"] - timing["nominal_hz"])
    ok = off_by <= tolerance_hz and timing["n_dropped"] == 0
    summary = (
        f"{timing['measured_hz']:.2f} Hz measured against {timing['nominal_hz']:g} configured, "
        f"{timing['n_dropped']} of {timing['n']} frames late"
    )
    return ok, summary


# ----------------------------------------------------------------------
# The procedures. Thin: each one drives real hardware and hands the numbers
# to the functions above.
# ----------------------------------------------------------------------


def measure_display(rig: RigConfig, display: Any, n_flips: int = DEFAULT_FLIPS) -> Measurement:
    """Time a run of flips on the real display.

    Timed here rather than taken from ``measure_refresh_rate`` because that
    reports a rate and this needs the intervals: a panel that averages 60 Hz
    while dropping one frame in twenty is a panel an experiment must know
    about, and an average hides it.
    """
    import time

    intervals = []
    display.flip()
    last = time.perf_counter()
    for _ in range(n_flips):
        display.flip()
        now = time.perf_counter()
        intervals.append(now - last)
        last = now

    timing = frame_timing(intervals, rig.monitor.refresh_rate_hz)
    ok, summary = judge_refresh(timing)
    notes = []
    if timing["n_dropped"]:
        notes.append(
            "Late frames on an idle display are a machine problem, not an experiment "
            "problem: close other applications, check the video mode and the cable, "
            "and re-run before blaming a session's frame QA."
        )
    return Measurement("display timing", summary, ok, {**timing, "notes": notes})


def measure_geometry(rig: RigConfig, size_dva: float = 10.0) -> Measurement:
    """What a known angular size should measure on the glass.

    Reported rather than judged: only a tape measure can settle it, and the
    report is what tells whoever is holding one what they should be reading.
    ``alhazen calibrate ruler`` draws the bar to hold it against.
    """
    from alhazen.cli.calibrate import ruler_report

    screen = Screen.from_monitor(rig.monitor)
    return Measurement(
        "display geometry",
        f"{screen.px_per_deg:.2f} px per degree — hold a tape to "
        f"`alhazen calibrate ruler --rig ...` to confirm",
        None,
        {
            "px_per_deg": screen.px_per_deg,
            "width_px": rig.monitor.width_px,
            "width_cm": rig.monitor.width_cm,
            "distance_cm": rig.monitor.distance_cm,
            "notes": ruler_report(rig, size_dva).splitlines(),
        },
    )


def measure_key_latency(
    display: Any,
    wait_for_key: Callable[[], tuple[str, float, float]],
    n_presses: int = DEFAULT_PRESSES,
    show: Callable[[str], None] | None = None,
    now: Callable[[], float] = time.perf_counter,
) -> Measurement:
    """Flip a marker, wait for a key, and time the gap — ``n_presses`` times.

    ``wait_for_key`` returns ``(key, arrived_at, noticed_at)``: when the
    windowing toolkit stamped the key, and when this loop saw it. Injected so
    the procedure can be tested without a keyboard, and so a rig with an
    exotic input device can supply its own.

    ``now`` stamps the flip, and **must be the same clock ``wait_for_key``
    stamps against** — the latency is one minus the other, so two clocks give
    a difference between epochs rather than a latency. It is a parameter
    rather than a hardcoded ``perf_counter`` precisely to make that coupling
    visible: ``_psychopy_key_waiter`` pins psychopy's clock to perf_counter's
    epoch for the same reason, and a test that injects one must inject both.

    Two numbers come out, and they answer different questions:

    - **poll lag** (noticed minus arrived) is alhazen's own contribution. No
      human is in it. If it is large, the frame loop is doing too much
      between polls and every reaction time in every experiment on this rig
      is inflated by it.
    - **press latency** (arrived minus flip) is the whole path: the panel's
      own latency, the person, and the input hardware. It is reported as a
      distribution because it is dominated by the person, and a single
      number would be read as if it were not.
    """
    lags, latencies = [], []
    for index in range(n_presses):
        if show is not None:
            show(f"press any key   ({index + 1} of {n_presses})")
        display.flip()
        flipped = now()
        _, arrived, noticed = wait_for_key()
        latencies.append(arrived - flipped)
        lags.append(noticed - arrived)

    lag = summarise(lags)
    latency = summarise(latencies)
    return Measurement(
        "response keys",
        f"poll lag {lag['median'] * 1000:.1f} ms (IQR {lag['iqr'] * 1000:.1f}); "
        f"flip-to-key {latency['median'] * 1000:.0f} ms (IQR {latency['iqr'] * 1000:.0f})",
        None,
        {
            "poll_lag_s": lag,
            "press_latency_s": latency,
            "notes": [
                "Poll lag is alhazen's own: the gap between the toolkit stamping a key "
                "and the session loop noticing it. It is the only part of the number "
                "below that alhazen controls, and it should be well under a frame.",
                "Flip-to-key includes the panel's latency, the person's reaction time "
                "and the input hardware, and nothing here separates them. Read it as a "
                "distribution: what matters is that it is stable, not what it is.",
            ],
        },
    )


def measure_tracker_accuracy(
    tracker: Any,
    screen: Screen,
    present_target: Callable[[tuple[float, float]], tuple[float, float]],
    targets: Sequence[tuple[float, float]] = DEFAULT_ACCURACY_TARGETS_DVA,
) -> Measurement:
    """Show targets at known positions and measure how far the gaze lands off.

    ``present_target`` draws one target at a centred-pixel position, waits for
    the eye to settle, and returns the gaze it measured — injected because
    "waits for the eye to settle" is a judgement each rig's operator makes
    differently, and because it is what lets this be tested without a tracker.

    A validation, not a calibration: it says how wrong the tracker still is
    after being calibrated, which is the number that decides whether a
    2-degree fixation window is generous or impossible today.
    """
    positions = [(screen.deg2px(x), screen.deg2px(y)) for x, y in targets]
    samples = [present_target(position) for position in positions]
    result = accuracy(positions, samples, screen)
    worst = result["max"]
    # Half a degree is the number SR Research quote for a well-calibrated
    # EyeLink; a degree is where a small fixation window starts refusing
    # trials a subject is actually making.
    ok = worst <= 1.0
    return Measurement(
        "eye tracker accuracy",
        f"median {result['median']:.2f} dva off, worst {worst:.2f} dva over {result['n']} targets",
        ok,
        {
            **result,
            "notes": (
                []
                if ok
                else [
                    "Worse than 1 degree: a fixation window narrower than that will "
                    "refuse trials the subject is making. Recalibrate before the "
                    "session, and check the head support and the camera focus."
                ]
            ),
        },
    )


# ----------------------------------------------------------------------
# The driver: open the rig's real display and run the procedures against it.
# ----------------------------------------------------------------------

# The measurements, in the order they run. Ordered by what an experimenter
# should stop for: a display that drops frames makes every later number
# meaningless, so it goes first, and the tracker — the slowest, and the one
# needing a person in the chair — goes last.
MEASUREMENTS = ("display", "geometry", "keys", "tracker")


def run_measurements(
    rig: RigConfig,
    rig_path: str,
    *,
    windowed: bool = False,
    n_flips: int = DEFAULT_FLIPS,
    n_presses: int = DEFAULT_PRESSES,
    skip: Sequence[str] = (),
    echo: Callable[[str], None] = print,
) -> MeasurementReport:
    """Measure this rig, through the display a session would open.

    The window comes from ``PsychoPyDisplay`` rather than from
    ``visual.Window``, so what is measured is what a session gets — including
    the framebuffer check. A measurement mode that opened its own window
    would be measuring a different display from the one the experiment uses,
    which is precisely the failure it exists to catch.

    Every measurement runs even after one fails: whoever came to check a rig
    wants the whole picture from one visit, not to fix one thing, re-run, and
    only then discover the second.
    """
    unknown = sorted(set(skip) - set(MEASUREMENTS))
    if unknown:
        raise ValueError(
            f"nothing to skip called {', '.join(unknown)}; "
            f"the measurements are {', '.join(MEASUREMENTS)}"
        )

    from alhazen.display.psychopy_backend import PsychoPyDisplay

    report = MeasurementReport(rig_path=rig_path)
    screen = Screen.from_monitor(rig.monitor)
    display = PsychoPyDisplay(rig.monitor, windowed=windowed)
    display.open()
    try:
        if "display" not in skip:
            echo(f"timing {n_flips} flips...")
            report.measurements.append(measure_display(rig, display, n_flips))

        if "geometry" not in skip:
            report.measurements.append(measure_geometry(rig))

        if "keys" not in skip:
            echo(f"press any key {n_presses} times, when asked...")
            report.measurements.append(
                measure_key_latency(
                    display,
                    _psychopy_key_waiter(),
                    n_presses,
                    show=display.show_message,
                )
            )

        if "tracker" not in skip:
            if rig.devices.eyetracker is None:
                # Said out loud rather than skipped silently: "no tracker
                # measurement" and "no tracker on this rig" look identical in
                # a report, and only one of them is fine.
                report.measurements.append(
                    Measurement(
                        "eye tracker accuracy",
                        "no eye tracker configured on this rig — nothing to measure",
                        None,
                    )
                )
            else:
                report.measurements.append(_measure_tracker_on_rig(rig, display, screen, echo))
    finally:
        display.close()
    return report


def _psychopy_key_waiter() -> Callable[[], tuple[str, float, float]]:
    """A ``wait_for_key`` for the real keyboard.

    Returns ``(key, arrived_at, noticed_at)``. The two times are what let the
    caller separate alhazen's own polling lag from everything else: psychopy
    stamps the key when the toolkit received it, and ``perf_counter`` here is
    when this loop got round to looking.

    Both clocks must be the same one, so the stamp comes from a psychopy
    ``Clock`` constructed against ``perf_counter``'s epoch — psychopy's
    ``getKeys(timeStamped=clock)`` reports seconds on whatever clock it is
    handed, and mixing two epochs would produce a "lag" of however far apart
    they happened to start.
    """
    import time

    from psychopy import core, event

    clock = core.Clock()
    # Pin the psychopy clock's zero to a perf_counter reading, so the two
    # timelines can be added rather than compared blindly.
    origin = time.perf_counter() - clock.getTime()

    def wait() -> tuple[str, float, float]:
        event.clearEvents()
        while True:
            pressed = event.getKeys(timeStamped=clock)
            if pressed:
                key, stamp = pressed[0]
                return key, origin + stamp, time.perf_counter()
            time.sleep(0.001)

    return wait


def _measure_tracker_on_rig(
    rig: RigConfig, display: Any, screen: Screen, echo: Callable[[str], None]
) -> Measurement:
    """Connect the rig's tracker, show the targets, and read the gaze back.

    The operator advances each target by pressing a key once the subject is
    steady on it, because "steady" is a judgement a person makes and a timer
    does not: a fixed dwell would sample a blink or a mid-saccade as readily
    as a fixation, and report the tracker as bad when the timing was.
    """
    from psychopy import event, visual

    from alhazen.core.clock import MonotonicClock
    from alhazen.devices.eyetracker import make_tracker

    assert rig.devices.eyetracker is not None  # the caller checked; this narrows the type
    clock = MonotonicClock()
    # make_tracker builds the same backend a session would, and needs the same
    # four things a session hands it — the display included, because a real
    # tracker draws its calibration through the session's own window.
    tracker = make_tracker(rig.devices.eyetracker, display, screen, clock)
    tracker.connect()
    tracker.configure(screen, clock)
    try:
        tracker.start_trial(0, "accuracy check")
        dot = visual.Circle(
            display.window,
            radius=max(4.0, screen.deg2px(0.2) / 2.0),
            fillColor="white",
            lineColor="white",
            units="pix",
        )

        def present(position: tuple[float, float]) -> tuple[float, float]:
            """Draw one target, wait for the operator, return where gaze was.

            ``get_gaze`` returns None when the tracker has no eye — a blink,
            or the subject looking away — and asking again is the only sane
            answer: substituting a default would report perfect accuracy at a
            point that was never measured, and raising would throw away the
            targets already collected. So it says what happened and waits.
            """
            while True:
                dot.pos = position
                dot.draw()
                display.flip()
                event.clearEvents()
                # Blocks until the operator says the eye is on the target.
                event.waitKeys()
                sample = tracker.get_gaze()
                if sample is not None:
                    return screen.screen_to_centered(sample.gx, sample.gy)
                echo("  no eye at that moment — look at the dot and press again")

        echo("look at each dot; press a key when the eye is steady on it")
        return measure_tracker_accuracy(tracker, screen, present)
    finally:
        tracker.stop_trial()
        # shutdown(), not close(): it is what the EyeTracker protocol declares,
        # and None says this measurement keeps no native eye recording.
        tracker.shutdown(None)
