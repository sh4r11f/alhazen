"""alhazen's own measurement jobs, the rig's devices for them, and the operator.

Each job here is a thin procedure: it opens what it needs through the run's
:class:`~alhazen.modes.measure_jobs.Devices`, asks the operator for what only
a person can read, and hands the numbers to a tested function
(:mod:`alhazen.modes.measure`, :mod:`alhazen.modes.measure_stats`,
:func:`alhazen.config.gamma.fit_gamma`). Where a claim cannot be measured on
this rig the job says so — *unavailable*, with what would make it available —
rather than reporting a number it did not measure.

The groups, as the dashboard shows them, and what each can and cannot claim
(docs/measure-rig.md has the full metrology notes):

- **Monitor** — refresh and frame timing (measured on the real window);
  viewing distance and size (a tape measure, declared and measured kept
  apart); luminance and gamma (photometer readings in cd/m², fitted, never
  applied); colour (no colorimeter integration: unavailable).
- **Input** — keyboard poll lag and flip-to-key time (human included, and
  said so); mouse pointer travel per centimetre at two speeds (pointer gain
  with any OS acceleration, not sensor DPI).
- **Eye tracker** — calibration (the tracker's own procedure and verdict),
  then accuracy, precision and gain on validation samples taken after it.
- **Reward** — read-only driver/device/channel check; juice per pulse from a
  balance reading, after explicit arming at the rig.
- **Neural** — a bounded read-only look at the configured acquisition: a
  SpikeGLX imec stream's rate, channels and raw noise in ADC counts, or a
  sorted-spike stream's announcements. Never starts, stops or reconfigures it.
"""

from __future__ import annotations

import csv
import logging
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from typing import Any

from alhazen.config.gamma import fit_gamma, gamma_path, load_gamma, read_measurements
from alhazen.config.models import RewardPulses, RigConfig
from alhazen.display.ruler import draw_ruler_on
from alhazen.display.screen import Screen
from alhazen.modes import measure_stats as stats
from alhazen.modes.measure import (
    DEFAULT_ACCURACY_TARGETS_DVA,
    DEFAULT_FLIPS,
    DEFAULT_PRESSES,
    RULER_DVA,
    Measurement,
    calibration_verdict,
    measure_display,
    measure_key_latency,
)
from alhazen.modes.measure_jobs import (
    Devices,
    JobContext,
    JobUnavailable,
    MeasurementJob,
    OperatorCancelled,
)

log = logging.getLogger(__name__)

# Reward volume limits. A calibration collects enough to weigh well (a few
# hundred µL at least) without ever holding a valve open long enough to
# flood a rig: these bound the plan before anything is armed.
MAX_PULSES = 500
MAX_PULSE_MS = 1000
MAX_TOTAL_OPEN_S = 60.0

# How long the accuracy job samples gaze once the operator says the eye is
# on a target, and its bounds.
DEFAULT_SAMPLE_MS = 500.0

# The neural stream's listening window, in seconds, and its bounds.
DEFAULT_LISTEN_S = 2.0
NOISE_CHANNELS = 16
NOISE_BLOCK_S = 0.5

STAND_IN_TRACKERS = {"mouse_sim", "scripted"}


# ----------------------------------------------------------------------
# Availability: asked before anything opens
# ----------------------------------------------------------------------


def _needs_window(rig: RigConfig, _inputs: Mapping[str, str]) -> str | None:
    if rig.display.backend == "simulated":
        return "this rig's display is simulated: there is no panel to measure"
    return None


def _needs_real_tracker(rig: RigConfig, inputs: Mapping[str, str]) -> str | None:
    tracker = rig.devices.eyetracker
    if tracker is None:
        return "no eye tracker configured on this rig"
    if tracker.backend in STAND_IN_TRACKERS:
        return (
            f"the rig's tracker is the {tracker.backend} stand-in, which has no calibration, "
            "accuracy or precision to measure"
        )
    return _needs_window(rig, inputs)


def _needs_real_reward(rig: RigConfig, _inputs: Mapping[str, str]) -> str | None:
    reward = rig.devices.reward
    if reward is None:
        return "no reward device configured on this rig"
    if reward.backend == "simulated":
        return "the rig's reward is simulated: it opens no valve and delivers no liquid"
    return None


def _colour_unavailable(_rig: RigConfig, _inputs: Mapping[str, str]) -> str | None:
    return (
        "not supported: alhazen has no colorimeter integration and no colour (xyY) "
        "calibration model. Luminance and gamma are measured by monitor.luminance; a "
        "colour characterisation needs a colorimeter and its own software"
    )


def _neural_unavailable(rig: RigConfig, _inputs: Mapping[str, str]) -> str | None:
    spikes = rig.devices.spikes
    if spikes is None:
        return (
            "no neural acquisition configured: to check a Neuropixels probe, set "
            "devices.spikes with backend: spikeglx (host, port, stream: imec0) — the "
            "SpikeGLX command server, read only. Open Ephys is not supported by alhazen; "
            "a sorted-spike stream is backend: sorted_stream"
        )
    if spikes.backend == "simulated":
        return "the rig's spike source is simulated: there is no acquisition to read"
    return None


# ----------------------------------------------------------------------
# The jobs
# ----------------------------------------------------------------------


def _refresh(ctx: JobContext) -> Measurement:
    n_flips = (
        int(ctx.number("flips", "flips to time", unit="flips", low=30, high=3600))
        if ctx.input("flips")
        else DEFAULT_FLIPS
    )
    return measure_display(ctx.rig, ctx.devices.get("display"), n_flips, echo=ctx.echo)


def _geometry(ctx: JobContext) -> Measurement:
    monitor = ctx.rig.monitor
    screen = ctx.screen
    bar_px = screen.deg2px(RULER_DVA)
    distance = ctx.number(
        "distance_cm",
        "Measure the viewing distance: from the eye (at the chin rest) to the screen centre",
        unit="cm",
        low=10.0,
        high=400.0,
    )
    if ctx.input("bar_cm") is None:
        ctx.operator.tell(
            f"A {RULER_DVA:g}° bar is drawn next. Hold the tape against it, then press any key."
        )
        draw_ruler_on(ctx.devices.get("display"), ctx.rig, RULER_DVA)
    bar_cm = ctx.number(
        "bar_cm",
        f"Length of the {RULER_DVA:g}° bar between its ticks",
        unit="cm",
        low=0.1,
        high=300.0,
    )
    result = stats.geometry_check(
        width_px=monitor.width_px,
        declared_width_cm=monitor.width_cm,
        declared_distance_cm=monitor.distance_cm,
        bar_px=bar_px,
        measured_bar_cm=bar_cm,
        measured_distance_cm=distance,
    )
    error = result["px_per_deg_error"]
    notes = [
        "Declared values are the rig file's; measured ones are the tape's. Nothing here "
        "changes the rig file: if they disagree, edit monitor.width_cm / monitor.distance_cm.",
    ]
    if not result["ok"]:
        notes.append(
            f"Every stimulus size on this rig is off by {100 * error:+.1f}% until the rig file "
            "is corrected."
        )
    return Measurement(
        "viewing geometry",
        f"{result['measured']['px_per_deg']:.2f} px/deg measured against "
        f"{result['declared']['px_per_deg']:.2f} declared ({100 * error:+.1f}%); "
        f"distance {distance:g} cm (declared {monitor.distance_cm:g})",
        bool(result["ok"]),
        {**result, "notes": notes},
    )


def _luminance(ctx: JobContext) -> Measurement:
    instrument = (
        ctx.input("instrument")
        or "not named (give --measure-input monitor.luminance.instrument=...)"
    )
    readings = ctx.input("readings")
    if readings is not None:
        levels, luminances = read_measurements(readings)
        source = f"imported from {readings}"
    else:
        n_levels = int(ctx.input("levels") or 9)
        if not 3 <= n_levels <= 33:
            raise ValueError("monitor.luminance.levels must be between 3 and 33")
        display = ctx.devices.get("display")
        values = [i / (n_levels - 1) for i in range(n_levels)]
        measured = []
        for index, level in enumerate(values):
            show_patch(display, level)
            measured.append(
                ctx.number(
                    f"level_{index}",
                    f"Photometer reading of the patch at level {level:.3f} "
                    f"({index + 1} of {n_levels})",
                    unit="cd/m²",
                    low=0.0,
                    high=100000.0,
                )
            )
        import numpy as np

        levels, luminances = np.asarray(values), np.asarray(measured)
        source = "read by the operator from the photometer"
    table = stats.luminance_summary(list(levels), list(luminances))
    fit = fit_gamma(levels, luminances)
    ctx.output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = ctx.output_dir / "luminance.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["level", "luminance"])
        writer.writerows(zip(table["levels"], table["luminance_cd_m2"], strict=True))
    stored = load_gamma(ctx.rig_path)
    notes = [
        f"Readings {source}; instrument: {instrument}. Units: cd/m² as entered.",
        "Nothing was applied. To use this fit, run it explicitly: "
        f"alhazen calibrate gamma --rig {ctx.rig_path} --measurements {csv_path} — it writes "
        f"{gamma_path(ctx.rig_path).name}; keep the current file to roll back.",
    ]
    if stored is not None:
        notes.append(
            f"The gamma in use now is {stored['gamma']:.3f} ({gamma_path(ctx.rig_path).name})."
        )
    if not table["monotonic"]:
        notes.append("The readings do not rise with level: check the meter was on the patch.")
    return Measurement(
        "luminance and gamma",
        f"gamma {fit['gamma']:.3f}, {table['min_cd_m2']:g}–{table['max_cd_m2']:g} cd/m² over "
        f"{fit['n_measurements']} levels (fitted, not applied)",
        None,
        {
            **table,
            "fit": fit,
            "instrument": instrument,
            "source": source,
            "readings_csv": str(csv_path),
            "stored_gamma": stored,
            "notes": notes,
        },
    )


def show_patch(display: Any, level: float) -> None:
    """Fill the window with one grey level (0 black, 1 white) for a
    photometer, through the window's own colour pipeline."""
    from psychopy import visual

    grey = 2.0 * level - 1.0
    rect = visual.Rect(
        display.window,
        width=display.window.size[0],
        height=display.window.size[1],
        units="pix",
        fillColor=(grey, grey, grey),
        lineColor=None,
    )
    rect.draw()
    display.flip()


def _keys(ctx: JobContext) -> Measurement:
    from alhazen.modes.measure import _psychopy_key_waiter

    display = ctx.devices.get("display")
    presses = int(ctx.input("presses") or DEFAULT_PRESSES)
    if not 3 <= presses <= 200:
        raise ValueError("input.keys.presses must be between 3 and 200")
    ctx.set_waiting(f"press any key, {presses} times, when asked")
    try:
        return measure_key_latency(
            display, _psychopy_key_waiter(), presses, show=display.show_message
        )
    finally:
        ctx.set_waiting(None)


def _mouse(ctx: JobContext) -> Measurement:
    distance = ctx.number(
        "distance_cm",
        "Distance to slide the mouse along a ruler each pass",
        unit="cm",
        low=1.0,
        high=50.0,
    )
    passes = int(ctx.input("passes") or 3)
    drag = mouse_dragger(ctx.devices.get("display"))
    slow: list[float] = []
    fast: list[float] = []
    for speed, bucket in (("slowly", slow), ("quickly", fast)):
        for index in range(passes):
            ctx.set_waiting(f"slide the mouse {distance:g} cm {speed} ({index + 1} of {passes})")
            try:
                bucket.append(
                    drag(
                        f"Hold the left button, slide the mouse {distance:g} cm {speed} "
                        f"along the ruler, release. ({index + 1} of {passes}; ESC stops)"
                    )
                )
            finally:
                ctx.set_waiting(None)
    result = stats.pointer_gain(slow, fast, distance)
    notes = [
        "This is pointer travel per centimetre: the sensor's resolution times the operating "
        "system's pointer gain and acceleration. It is not the sensor's DPI, which needs the "
        "device's raw counts.",
    ]
    if result["acceleration_suspected"]:
        notes.append(
            f"Fast passes moved {result['fast_over_slow']:.2f}× as far as slow ones: pointer "
            "acceleration is on, so a mouse response's pixel distance depends on its speed."
        )
    return Measurement(
        "mouse pointer gain",
        f"{result['slow']['mean_px_per_cm']:.1f} px/cm slow, "
        f"{result['fast']['mean_px_per_cm']:.1f} px/cm fast (×{result['fast_over_slow']:.2f})",
        None,
        {**result, "slow_px": slow, "fast_px": fast, "notes": notes},
    )


def mouse_dragger(display: Any) -> Callable[[str], float]:
    """A drag reader on the real window: shows the prompt, waits for the
    left button to go down and come up, returns the pointer's horizontal
    travel in px. ESC raises OperatorCancelled."""
    from psychopy import event

    mouse = event.Mouse(win=display.window)

    def drag(prompt: str) -> float:
        display.show_message(prompt)
        event.clearEvents()
        while not mouse.getPressed()[0]:
            if "escape" in event.getKeys(keyList=["escape"]):
                raise OperatorCancelled("the operator stopped the mouse passes")
            time.sleep(0.002)
        start = mouse.getPos()
        while mouse.getPressed()[0]:
            time.sleep(0.002)
        end = mouse.getPos()
        return float(abs(end[0] - start[0]))

    return drag


def _reward_connection(ctx: JobContext) -> Measurement:
    cfg = ctx.rig.devices.reward
    assert cfg is not None  # unavailable() refused a rig without one
    try:
        import nidaqmx.system
    except ImportError as error:
        return Measurement(
            "reward connection",
            "the NI-DAQmx Python package is not installed: this rig cannot open its valve",
            False,
            {
                "backend": cfg.backend,
                "error": str(error),
                "notes": ["Install alhazen's [nidaq] extra and NI-DAQmx on the rig."],
            },
        )
    system = nidaqmx.system.System.local()
    try:
        version = system.driver_version
        driver = f"{version.major_version}.{version.minor_version}.{version.update_version}"
        devices = {device.name: device for device in system.devices}
    except Exception as error:  # the driver library itself is what is being checked
        return Measurement(
            "reward connection",
            f"the NI-DAQmx driver did not answer: {type(error).__name__}: {error}",
            False,
            {"backend": cfg.backend, "error": str(error)},
        )
    channel = f"{cfg.device}/{cfg.channel}"
    found = cfg.device in devices
    channels = list(devices[cfg.device].ao_physical_chans.channel_names) if found else []
    ok = found and channel in channels
    detail = {
        "backend": cfg.backend,
        "driver": driver,
        "devices": sorted(devices),
        "device": cfg.device,
        "product": getattr(devices.get(cfg.device), "product_type", None),
        "channel": channel,
        "ao_channels": channels,
        "voltage": cfg.voltage,
        "notes": [
            "Read only: no task was created and no line was written. Whether the valve "
            "opens, and how much it delivers, is reward.volume's question."
        ],
    }
    if not found:
        listed = ", ".join(sorted(devices)) or "none"
        summary = f"{cfg.device} is not among the devices the driver lists ({listed})"
    elif not ok:
        summary = f"{cfg.device} has no analog output {cfg.channel}"
    else:
        summary = f"NI-DAQmx {driver}: {channel} present on {detail['product'] or cfg.device}"
    return Measurement("reward connection", summary, ok, detail)


def _reward_volume(ctx: JobContext) -> Measurement:
    n_pulses = int(
        ctx.number("pulses", "How many pulses to deliver", unit="pulses", low=1, high=MAX_PULSES)
    )
    pulse_ms = int(ctx.number("pulse_ms", "Pulse width", unit="ms", low=1, high=MAX_PULSE_MS))
    gap_ms = int(ctx.number("inter_pulse_ms", "Gap between pulses", unit="ms", low=50, high=5000))
    open_s = n_pulses * pulse_ms / 1000.0
    if open_s > MAX_TOTAL_OPEN_S:
        raise ValueError(
            f"{n_pulses} × {pulse_ms} ms holds the valve open {open_s:g} s in total; the limit is "
            f"{MAX_TOTAL_OPEN_S:g} s — use fewer or shorter pulses"
        )
    pulses = RewardPulses(n_pulses=n_pulses, pulse_ms=pulse_ms, inter_pulse_ms=gap_ms)
    plan = {
        "n_pulses": n_pulses,
        "pulse_ms": pulse_ms,
        "inter_pulse_ms": gap_ms,
        "total_open_s": open_s,
        "train_s": (n_pulses * (pulse_ms + gap_ms)) / 1000.0,
    }
    armed = ctx.confirm(
        "arm",
        f"ARM THE VALVE? {n_pulses} pulses × {pulse_ms} ms (valve open {open_s:g} s in total, "
        f"train {plan['train_s']:g} s). A tared collection container must be under the spout. "
        "Y delivers once; N or ESC cancels.",
    )
    if not armed:
        raise OperatorCancelled("not armed: no pulse was sent")
    dispenser = ctx.devices.get("reward")
    started = time.perf_counter()
    try:
        dispenser.deliver(pulses)
    except Exception as error:
        # Uncertain: some pulses may have run. Never retried here — a second
        # train into the same container would make both readings wrong.
        return Measurement(
            "reward volume",
            f"delivery did not complete ({type(error).__name__}: {error}); NOT retried — empty "
            "the container before measuring again",
            False,
            {"plan": plan, "delivered": "uncertain", "error": str(error)},
        )
    commanded_s = time.perf_counter() - started
    net = ctx.number(
        "net_mass_g", "Net mass collected (tared balance)", unit="g", low=0.0001, high=1000.0
    )
    density = ctx.number(
        "density_g_per_ml",
        "Density of the liquid (declared, e.g. 1.00 for water)",
        unit="g/mL",
        low=0.5,
        high=2.0,
    )
    result = stats.reward_volume(
        net_mass_g=net, density_g_per_ml=density, n_pulses=n_pulses, pulse_ms=pulse_ms
    )
    return Measurement(
        "reward volume",
        f"{result['ul_per_pulse']:.1f} µL per {pulse_ms} ms pulse "
        f"({result['volume_ul']:.0f} µL over {n_pulses} pulses)",
        None,
        {
            **result,
            "plan": plan,
            "delivered": "commanded once",
            "command_returned_after_s": round(commanded_s, 3),
            "notes": [
                "Volume comes from the balance, not from the command: a returned command proves "
                "the train was played out, not that liquid flowed.",
                f"Density {density:g} g/mL is the operator's declaration, recorded with "
                "the result.",
            ],
        },
    )


def _neural(ctx: JobContext) -> Measurement:
    cfg = ctx.rig.devices.spikes
    assert cfg is not None
    if cfg.backend == "sorted_stream":
        from alhazen.session.checks import check_spikes

        result = check_spikes(ctx.rig)
        return Measurement(
            "neural stream",
            f"sorted-spike stream: {result.detail}",
            result.ok,
            {
                **result.evidence,
                "stream_type": "sorted units over ZeroMQ (not raw IMEC samples)",
                "notes": [
                    "A sorted stream carries unit spike times, not raw AP/LF samples: "
                    "no sample rate, channel noise or gain can be read from it."
                ],
            },
        )
    listen_s = float(ctx.input("listen_s") or DEFAULT_LISTEN_S)
    if not 0.5 <= listen_s <= 10.0:
        raise ValueError("neural.stream.listen_s must be between 0.5 and 10")
    from alhazen.devices.spikes import parse_stream

    js, ip = parse_stream(cfg.stream)
    connection = ctx.devices.get("spikes")
    version = connection.version()
    if not connection.is_running():
        raise JobUnavailable(
            f"SpikeGLX at {cfg.host}:{cfg.port} answers (version {version}) but no acquisition "
            "is running; this check reads a running stream and never starts one"
        )
    rate = connection.sample_rate(js, ip)
    counts = connection.acq_channel_counts(js, ip)
    first, t0 = connection.sample_count(js, ip), time.monotonic()
    time.sleep(listen_s)
    last, t1 = connection.sample_count(js, ip), time.monotonic()
    flow = stats.stream_rate(first, last, t1 - t0, rate)
    detail: dict[str, Any] = {
        "software": f"SpikeGLX {version}",
        "stream": cfg.stream,
        "stream_type": "imec (raw AP/LF/SY)" if cfg.stream.startswith("imec") else cfg.stream,
        "channel_counts": counts,
        "channel_kinds": ["AP", "LF", "SY"] if cfg.stream.startswith("imec") else None,
        **flow,
    }
    noise = None
    n_neural = counts[0] if counts else 0
    if flow["advancing"] and n_neural:
        channels = list(range(min(NOISE_CHANNELS, n_neural)))
        want = max(2, int(rate * NOISE_BLOCK_S))
        head, block = connection.fetch(js, ip, max(0, last - want), want, channels)
        noise = stats.channel_noise(block)
        detail["noise"] = {**noise, "channels": channels, "first_sample": head}
    ok = flow["advancing"] and abs(flow["rate_error"]) <= 0.02
    detail["notes"] = [
        "Read only: the acquisition was not started, stopped or reconfigured; nothing was "
        "written or uploaded.",
        "Noise is RMS in int16 ADC counts: converting to µV needs the probe's gain, which "
        "this check does not read.",
    ]
    noise_line = (
        f", median RMS {noise['rms_counts_median']:.1f} counts on {noise['n_channels']} AP channels"
        if noise
        else ""
    )
    return Measurement(
        "neural stream",
        f"{cfg.stream} at {flow['observed_hz']:.0f} Hz observed / {rate:g} reported, "
        f"channels {counts}{noise_line}",
        ok,
        detail,
    )


def _tracker_calibration(ctx: JobContext) -> Measurement:
    tracker = ctx.devices.get("tracker")
    result = tracker.calibrate()
    proceed, line = calibration_verdict(result)
    detail: dict[str, Any] = {"calibration": line}
    if result is not None:
        detail.update(
            layout=result.layout,
            n_targets=result.n_targets,
            eye=result.eye,
            advance=result.advance,
            aborted=result.aborted,
            note=result.note,
            target_style=result.target_style,
            targets=[
                {
                    "target_px": list(t.target_px),
                    "left_error_deg": t.left_error_deg,
                    "right_error_deg": t.right_error_deg,
                }
                for t in result.targets
            ],
        )
    if result is not None and result.aborted:
        raise OperatorCancelled(f"calibration aborted: {line}")
    ok = None if result is None or result.ok is None else bool(result.ok and proceed)
    return Measurement("eye tracker calibration", line, ok, detail)


def _tracker_accuracy(ctx: JobContext) -> Measurement:
    tracker = ctx.devices.get("tracker")
    display = ctx.devices.get("display")
    window_ms = float(ctx.input("window_ms") or DEFAULT_SAMPLE_MS)
    if not 100.0 <= window_ms <= 3000.0:
        raise ValueError("tracker.accuracy.window_ms must be between 100 and 3000")
    screen = ctx.screen
    targets = [(screen.deg2px(x), screen.deg2px(y)) for x, y in DEFAULT_ACCURACY_TARGETS_DVA]
    collect = gaze_collector(tracker, display, screen)
    samples = []
    tracker.start_trial(0, "accuracy check")
    try:
        for index, position in enumerate(targets):
            ctx.set_waiting(
                f"subject on target {index + 1} of {len(targets)}: press a key when steady"
            )
            try:
                samples.append(collect(position, window_ms / 1000.0, ctx.echo))
            finally:
                ctx.set_waiting(None)
    finally:
        tracker.stop_trial()
    quality = stats.gaze_quality(targets, samples, screen)
    calibration = ctx.results.get("tracker.calibration")
    ok = quality["accuracy_max_dva"] <= 1.0
    gain = quality["gain"]
    return Measurement(
        "eye tracker accuracy",
        f"accuracy {quality['accuracy_mean_dva']:.2f}° mean, "
        f"{quality['accuracy_max_dva']:.2f}° worst; "
        f"precision {quality['precision_rms_s2s_median_dva'] or float('nan'):.3f}° RMS-S2S; "
        f"gain x {gain['x'] if gain['x'] is not None else float('nan'):.2f}, "
        f"y {gain['y'] if gain['y'] is not None else float('nan'):.2f}",
        ok,
        {
            **quality,
            "window_ms": window_ms,
            "samples_px": samples,
            "calibration": calibration.summary if calibration is not None else None,
            "notes": [
                "Validation on fresh fixations after the calibration, never the calibration's "
                "own samples. Bias and accuracy say how far off; precision (RMS-S2S, as polled "
                "here, not at the device's own rate) says how noisy; gain says whether the "
                "calibration stretches or compresses the screen.",
                *(
                    []
                    if ok
                    else [
                        "Worse than 1° somewhere: recalibrate before a session with a "
                        "small fixation window."
                    ]
                ),
            ],
        },
    )


def gaze_collector(
    tracker: Any, display: Any, screen: Screen
) -> Callable[..., list[tuple[float, float]]]:
    """On the real window: draw the dot, wait for the operator's key, then
    poll gaze once per flip for the window. A window with no valid sample
    (a blink, a look away) asks again rather than inventing one."""
    from psychopy import event, visual

    dot = visual.Circle(
        display.window,
        radius=max(4.0, screen.deg2px(0.2) / 2.0),
        fillColor="white",
        lineColor="white",
        units="pix",
    )

    def collect(
        position: tuple[float, float], seconds: float, echo: Callable[[str], None]
    ) -> list[tuple[float, float]]:
        while True:
            dot.pos = position
            dot.draw()
            display.flip()
            event.clearEvents()
            keys = event.waitKeys()
            if keys and keys[0] == "escape":
                raise OperatorCancelled("the operator stopped the accuracy check")
            points = []
            end = time.perf_counter() + seconds
            while time.perf_counter() < end:
                dot.draw()
                display.flip()
                sample = tracker.get_gaze()
                if sample is not None:
                    points.append(screen.screen_to_centered(sample.gx, sample.gy))
            if points:
                return points
            echo("  no gaze position during the window — look at the dot and press again")

    return collect


def builtin_jobs() -> list[MeasurementJob]:
    """alhazen's own measurements, in the order they run.

    The order puts what needs nobody first (timing, read-only device
    checks), then what needs a person at the keys or a tape, then what
    needs an instrument or a container, and the tracker last: it needs the
    subject in the chair.
    """
    window = frozenset({"display"})
    return [
        MeasurementJob(
            "monitor.refresh",
            "Monitor",
            "Refresh rate & frame timing",
            "Times flips on the real window: rate against the rig file, late frames.",
            10,
            _refresh,
            needs=window,
            unavailable=_needs_window,
            inputs=("flips (count)",),
        ),
        MeasurementJob(
            "neural.stream",
            "Neural recording",
            "Neuropixels / imec stream",
            "Read-only look at the configured acquisition: rate, channels, raw noise.",
            20,
            _neural,
            needs=frozenset({"spikes"}),
            unavailable=_neural_unavailable,
            inputs=("listen_s (s)",),
        ),
        MeasurementJob(
            "reward.connection",
            "Reward",
            "Reward connection",
            "Read-only: driver, device and output channel are present. Sends nothing.",
            30,
            _reward_connection,
            unavailable=_needs_real_reward,
        ),
        MeasurementJob(
            "input.keys",
            "Keyboard & mouse",
            "Key timing & response time",
            "Poll lag (software) and flip-to-key time (person included), kept apart.",
            40,
            _keys,
            needs=frozenset({"display", "keyboard"}),
            unavailable=_needs_window,
            inputs=("presses (count)",),
            subject="optional",
        ),
        MeasurementJob(
            "input.mouse",
            "Keyboard & mouse",
            "Mouse pointer gain",
            "Pointer px per cm of mouse travel, slow and fast (shows OS acceleration).",
            45,
            _mouse,
            needs=frozenset({"display", "mouse", "operator"}),
            unavailable=_needs_window,
            inputs=("distance_cm (cm)", "passes (count)"),
        ),
        MeasurementJob(
            "monitor.geometry",
            "Monitor",
            "Viewing distance & size",
            "Tape-measured distance and ruler bar against the rig file's geometry.",
            50,
            _geometry,
            needs=frozenset({"display", "operator"}),
            unavailable=_needs_window,
            inputs=("distance_cm (cm)", "bar_cm (cm)"),
        ),
        MeasurementJob(
            "monitor.luminance",
            "Monitor",
            "Luminance & gamma",
            "Photometer readings (cd/m²) of grey levels, fitted; never applied.",
            60,
            _luminance,
            needs=frozenset({"display", "operator"}),
            unavailable=_needs_window,
            inputs=("readings (CSV path)", "levels (count)", "instrument (text)"),
        ),
        MeasurementJob(
            "monitor.colour",
            "Monitor",
            "Colour (xyY)",
            "Needs a colorimeter integration alhazen does not have.",
            65,
            _unsupported,
            unavailable=_colour_unavailable,
        ),
        MeasurementJob(
            "reward.volume",
            "Reward",
            "Juice per pulse",
            "Delivers one armed pulse train into a container; volume from the balance.",
            70,
            _reward_volume,
            needs=frozenset({"reward", "operator", "display"}),
            unavailable=_needs_real_reward,
            inputs=(
                "pulses (count)",
                "pulse_ms (ms)",
                "inter_pulse_ms (ms)",
                "net_mass_g (g)",
                "density_g_per_ml (g/mL)",
            ),
        ),
        MeasurementJob(
            "tracker.calibration",
            "Eye tracker",
            "Calibration",
            "The tracker's own calibration procedure and its verdict.",
            80,
            _tracker_calibration,
            needs=frozenset({"tracker", "display"}),
            unavailable=_needs_real_tracker,
            subject="required",
        ),
        MeasurementJob(
            "tracker.accuracy",
            "Eye tracker",
            "Accuracy, precision & gain",
            "Fresh fixations on 5 targets after calibrating: bias, accuracy, RMS-S2S, gain.",
            85,
            _tracker_accuracy,
            needs=frozenset({"tracker", "display", "keyboard"}),
            requires=("tracker.calibration",),
            unavailable=_needs_real_tracker,
            inputs=("window_ms (ms)",),
            subject="required",
        ),
    ]


def _unsupported(ctx: JobContext) -> Measurement:  # never reached: unavailable() refuses first
    raise JobUnavailable(_colour_unavailable(ctx.rig, ctx.inputs) or "not supported")


# ----------------------------------------------------------------------
# The rig's devices, opened as a session would open them
# ----------------------------------------------------------------------


def rig_devices(rig: RigConfig, *, windowed: bool = False) -> Devices:
    """Factories for a real measurement run. Each registers its release on
    the run's stack as soon as it holds the device."""

    def display(stack: ExitStack) -> Any:
        from alhazen.display.psychopy_backend import PsychoPyDisplay

        window = PsychoPyDisplay(rig.monitor, windowed=windowed)
        stack.callback(window.close)
        window.open()
        return window

    def tracker(stack: ExitStack) -> Any:
        from alhazen.core.clock import MonotonicClock
        from alhazen.devices.eyetracker import make_tracker

        assert rig.devices.eyetracker is not None
        window = devices.get("display")
        screen = Screen.from_monitor(rig.monitor)
        clock = MonotonicClock()
        device = make_tracker(rig.devices.eyetracker, window, screen, clock)
        device.connect()
        # shutdown(None): measure mode keeps no native eye recording — unless
        # a job ended the tracker itself to keep one (JobContext
        # .keep_tracker_recording), which hands it back first.
        stack.callback(
            lambda: None if devices.was_handed_back("tracker") else device.shutdown(None)
        )
        device.configure(screen, clock)
        return device

    def reward(stack: ExitStack) -> Any:
        from alhazen.devices.reward import make_reward

        assert rig.devices.reward is not None
        dispenser = make_reward(rig.devices.reward)
        stack.callback(dispenser.close)
        return dispenser

    def spikes(stack: ExitStack) -> Any:
        from alhazen.devices.spikes import spikeglx_connection

        assert rig.devices.spikes is not None
        connection = spikeglx_connection(rig.devices.spikes)
        stack.callback(connection.close)
        return connection

    def window_input(stack: ExitStack) -> Any:
        return devices.get("display")

    devices = Devices(
        {
            "display": display,
            "tracker": tracker,
            "reward": reward,
            "spikes": spikes,
            "keyboard": window_input,
            "mouse": window_input,
            "operator": window_input,
        }
    )
    return devices


class WindowOperator:
    """The operator, through the rig's own window and keyboard.

    Every prompt starts from an empty key buffer, so a key pressed earlier
    (the one that launched the run, the last measurement's) can never answer
    it. Numbers are typed (digits, '.', '-'), BACKSPACE edits, ENTER accepts,
    ESC cancels the job. ``get_keys`` and ``sleep`` are injected for tests.
    """

    def __init__(
        self,
        devices: Devices,
        get_keys: Callable[[], list[str]] | None = None,
        clear: Callable[[], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._devices = devices
        self._get_keys = get_keys
        self._clear = clear
        self._sleep = sleep

    def _keys(self) -> list[str]:
        if self._get_keys is not None:
            return self._get_keys()
        from psychopy import event

        return list(event.getKeys())

    def _clear_keys(self) -> None:
        if self._clear is not None:
            self._clear()
            return
        from psychopy import event

        event.clearEvents()

    def _show(self, text: str) -> None:
        self._devices.get("display").show_message(text)

    def tell(self, prompt: str) -> None:
        self._show(prompt)

    def ask_number(self, key: str, prompt: str, *, unit: str, low: float, high: float) -> float:
        typed, problem = "", ""
        self._clear_keys()
        while True:
            self._show(
                f"{prompt}\n\n> {typed}_ {unit}\n\n{problem}"
                f"Type the number ({low:g} to {high:g}), ENTER to accept, "
                "ESC to stop this measurement."
            )
            for name in self._keys():
                if name == "escape":
                    raise OperatorCancelled(f"stopped at the prompt for {key}")
                if name in {"return", "enter", "num_enter"}:
                    try:
                        value = float(typed)
                    except ValueError:
                        problem = f"'{typed}' is not a number. "
                        continue
                    if low <= value <= high:
                        return value
                    problem = f"{value:g} is outside {low:g} to {high:g}. "
                elif name == "backspace":
                    typed = typed[:-1]
                elif name in {"period", "num_decimal"}:
                    typed += "."
                elif name in {"minus", "num_subtract"}:
                    typed += "-"
                elif name.startswith("num_") and name[4:].isdigit():
                    typed += name[4:]
                elif len(name) == 1 and name.isdigit():
                    typed += name
            self._sleep(0.01)

    def confirm(self, key: str, prompt: str) -> bool:
        self._clear_keys()
        while True:
            self._show(f"{prompt}\n\nY: yes    N or ESC: no")
            for name in self._keys():
                if name == "y":
                    return True
                if name in {"n", "escape"}:
                    return False
            self._sleep(0.01)
