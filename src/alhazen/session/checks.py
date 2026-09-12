"""check_rig: the pre-session ritual — run it before the subject arrives.

Every check constructs the same backend objects a real session would build
(devices/*.make_*), so a clean result actually predicts a working session
rather than exercising a parallel code path that can drift from it. What it
deliberately does not do is open the subject display: a window is the one
thing that cannot be checked without becoming a session.

Every check runs, always, even after one fails: whoever came to check the
whole rig wants the complete picture from one invocation, not to fix one
problem, re-run, and only then discover a second.

Each result also carries ``evidence``: what that device actually *did*, in
numbers — the pulse width that was commanded and the one that was measured,
every sync line by name, what the recorder returned, the lag the sorter is
running at. ``ok`` answers "may the session start"; the evidence is what
makes today's checkout comparable with last week's, and it is the half that
cannot be reconstructed from scrollback. :mod:`alhazen.session.checkout`
writes it down; nothing here decides a pass on it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alhazen.config.models import RewardPulses, RigConfig
from alhazen.core.clock import MonotonicClock
from alhazen.devices.eyetracker import make_tracker
from alhazen.devices.recording import make_recording
from alhazen.devices.reward import make_reward
from alhazen.devices.spikes import (
    UNITS_GRACE_MS,
    UNITS_REANNOUNCE_PERIOD_MS,
    make_spikes,
)
from alhazen.devices.sync import SyncOutput, make_sync
from alhazen.display import monitors as monitor_registry
from alhazen.display.screen import Screen
from alhazen.errors import AlhazenError, DisplayError, SpikeSourceError

log = logging.getLogger(__name__)

# One short, deliberately audible pulse: enough for whoever is standing at
# the rig to hear the valve, short enough to waste nothing.
CHECK_PULSE = RewardPulses(n_pulses=1, pulse_ms=50, inter_pulse_ms=0)


@dataclass(frozen=True)
class CheckResult:
    """One device's verdict, and what it did to earn it.

    ``evidence`` is JSON-serializable and per-device: keys mean whatever that
    device's check measured, and it is written whether the check passed or
    failed — a failure's evidence is the more useful of the two, because it
    says how far the device got before it stopped.
    """

    name: str
    ok: bool
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


def check_rig(rig: RigConfig, pulse: bool = False) -> list[CheckResult]:
    """Check every configured device plus the data root.

    With ``pulse=True`` the reward and sync checks fire real hardware once —
    construction alone only proves the SDK imports, which is not the same as
    a pump that is plugged in. Simulated backends say so in their detail.

    A rig config that no session could ever run (a test-only backend named in
    the YAML) raises ConfigError rather than returning a failed check: that is
    a broken config, not a broken rig, and it is the same error the session
    builder would raise.
    """
    return [
        _check_config(rig),
        _check_monitor(rig),
        _check_data_root(rig),
        _check_eyetracker(rig),
        _check_reward(rig, pulse),
        _check_sync(rig, pulse),
        _check_recording(rig),
        _check_spikes(rig),
    ]


def _check_config(rig: RigConfig) -> CheckResult:
    # Reaching this function at all means the YAML parsed and validated.
    configured = [
        name
        for name, cfg in (
            ("eyetracker", rig.devices.eyetracker),
            ("reward", rig.devices.reward),
            ("sync", rig.devices.sync),
            ("recording", rig.devices.recording),
            ("spikes", rig.devices.spikes),
        )
        if cfg is not None
    ]
    return CheckResult(
        "config",
        True,
        f"valid — {rig.display.backend} display, devices: "
        f"{', '.join(configured) if configured else 'none configured'}",
        {
            "display_backend": rig.display.backend,
            "configured": configured,
            "data_root": str(rig.data_root),
        },
    )


def _check_monitor(rig: RigConfig) -> CheckResult:
    """Does PsychoPy know this rig's monitor, and does it still agree with it?

    The window itself cannot be checked without becoming a session, but its
    monitor registration can — and a registration that has drifted from the
    rig config is exactly what stops the window from opening at all, half an
    hour later, with a subject already in the chair.
    """
    evidence: dict[str, Any] = {
        "name": rig.monitor.name,
        "display_backend": rig.display.backend,
        "width_px": rig.monitor.width_px,
        "height_px": rig.monitor.height_px,
        "width_cm": rig.monitor.width_cm,
        "distance_cm": rig.monitor.distance_cm,
        "refresh_rate_hz": rig.monitor.refresh_rate_hz,
    }
    if rig.display.backend != "psychopy":
        return CheckResult(
            "monitor",
            True,
            f"no psychopy registration needed ({rig.display.backend} display)",
            {**evidence, "registered": None},
        )
    try:
        registration = monitor_registry.lookup(rig.monitor.name)
    except DisplayError as e:
        # A rig config that asks for the psychopy backend on a machine without
        # psychopy cannot run a session at all, so this is a rig fault, not a
        # missing niceness.
        return CheckResult("monitor", False, str(e), {**evidence, "error": str(e)})

    evidence["registered"] = registration.registered
    if not registration.registered:
        # Not a failure: sessions run unregistered, using the config's own
        # geometry. They just have no stored calibration to inherit, which is
        # worth saying rather than passing silently.
        return CheckResult(
            "monitor",
            True,
            f"{rig.monitor.name!r} is not registered with psychopy — sessions will use this "
            f"config's geometry and no stored calibration "
            f"(alhazen monitor register --rig <yaml> to add it)",
            evidence,
        )
    drift = monitor_registry.differences(rig.monitor, registration)
    evidence["differences"] = list(drift)
    if drift:
        return CheckResult(
            "monitor",
            False,
            f"{rig.monitor.name!r} disagrees with this config ({'; '.join(drift)}) — "
            f"re-register it with `alhazen monitor register --rig <yaml>`",
            evidence,
        )
    gamma = monitor_registry.format_gamma(registration.gamma)
    return CheckResult(
        "monitor",
        True,
        f"{rig.monitor.name!r} registered with psychopy, gamma {gamma}",
        {**evidence, "gamma": gamma},
    )


def _check_data_root(rig: RigConfig) -> CheckResult:
    """Prove the data root is writable now, rather than at teardown — when
    the session's only copy of its data is still in memory."""
    root = rig.data_root
    probe = root / ".alhazen-write-check"
    evidence: dict[str, Any] = {"path": str(root), "probe": probe.name}
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return CheckResult(
            "data_root", False, f"{root} is not writable: {e}", {**evidence, "error": str(e)}
        )
    return CheckResult("data_root", True, f"{root} is writable", {**evidence, "written": True})


def _check_eyetracker(rig: RigConfig) -> CheckResult:
    cfg = rig.devices.eyetracker
    if cfg is None:
        return CheckResult("eyetracker", True, "not configured on this rig", {"configured": False})
    evidence: dict[str, Any] = {
        "configured": True,
        "backend": cfg.backend,
        "host_ip": cfg.host_ip if cfg.backend == "eyelink" else None,
        "simulated": cfg.backend == "mouse_sim",
    }
    if cfg.backend == "mouse_sim":
        # Never constructed here: it needs a real window to read the mouse
        # from, and check-rig must not open one. There is no hardware behind
        # it to verify anyway.
        return CheckResult(
            "eyetracker",
            True,
            "mouse_sim — no hardware to check (simulated)",
            {**evidence, "connected": None, "connect_ms": None},
        )

    # display=None is safe: connect() and shutdown() — the only methods
    # called here — never touch the window; only configure() does, and
    # calibration graphics need a real session.
    tracker = make_tracker(cfg, None, Screen.from_monitor(rig.monitor), MonotonicClock())
    started = time.perf_counter()
    try:
        tracker.connect()
        # How long the tracker took to answer, which is the tracker's actual
        # response and not a restatement of "it did not raise": a link that
        # connects in 4 s today and 40 ms last week is a network or a host
        # that has changed, and only a written number shows it.
        evidence["connect_ms"] = round(1000.0 * (time.perf_counter() - started), 1)
        evidence["connected"] = True
        # Release the device again: this is a smoke test, not a session, and
        # nothing should be left connected behind it. No destination — no
        # trial ran, so there is no recording to hand back.
        tracker.shutdown(None)
    except AlhazenError as e:
        # Only alhazen's own device errors are a rig fault; anything else is
        # a bug here and keeps its traceback.
        evidence["connected"] = False
        evidence["connect_ms"] = round(1000.0 * (time.perf_counter() - started), 1)
        return CheckResult("eyetracker", False, str(e), {**evidence, "error": str(e)})
    # Named by what the experimenter would have to go and check: an EyeLink
    # is reached over the network, so the IP is the useful half of the
    # message; a TRACKPixx3 is inside the display chassis and has no address
    # to get wrong, so printing one would be noise at best and misleading at
    # worst.
    where = f" at {cfg.host_ip}" if cfg.backend == "eyelink" else ""
    # The console line is unchanged: how long the link took to answer is a
    # number to compare between checkouts, not something to read out loud at
    # the rig. It goes in the record.
    return CheckResult("eyetracker", True, f"{cfg.backend}{where} responded", evidence)


def _check_reward(rig: RigConfig, pulse: bool) -> CheckResult:
    cfg = rig.devices.reward
    if cfg is None:
        return CheckResult("reward", True, "not configured on this rig", {"configured": False})
    simulated = " (simulated)" if cfg.backend == "simulated" else ""
    evidence: dict[str, Any] = {
        "configured": True,
        "backend": cfg.backend,
        "device": cfg.device,
        "channel": cfg.channel,
        "voltage": cfg.voltage,
        "simulated": cfg.backend == "simulated",
        "pulsed": pulse,
        "n_pulses": CHECK_PULSE.n_pulses if pulse else 0,
        "commanded_ms": float(CHECK_PULSE.pulse_ms) if pulse else None,
        "measured_ms": None,
    }
    try:
        reward = make_reward(cfg)
        if pulse:
            # Wall-clock across the delivery call. On the NI-DAQ backend that
            # call writes the buffer and blocks until the device says it has
            # played out, so the number is the pulse the hardware ran — which
            # is what sets the volume the subject got. It is NOT an
            # independent measurement of the valve: a measured 50 ms with a
            # disconnected solenoid still reads 50 ms. On a simulated backend
            # nothing is played out at all, so it reads ~0 and says so.
            started = time.perf_counter()
            reward.deliver(CHECK_PULSE)
            evidence["measured_ms"] = round(1000.0 * (time.perf_counter() - started), 1)
        reward.close()
    except AlhazenError as e:
        return CheckResult("reward", False, str(e), {**evidence, "error": str(e)})
    fired = f", fired one {CHECK_PULSE.pulse_ms} ms pulse" if pulse else ""
    return CheckResult(
        "reward", True, f"{cfg.backend} on {cfg.device}/{cfg.channel}{fired}{simulated}", evidence
    )


def _check_sync(rig: RigConfig, pulse: bool) -> CheckResult:
    cfg = rig.devices.sync
    if cfg is None:
        return CheckResult("sync", True, "not configured on this rig", {"configured": False})
    simulated = " (simulated)" if cfg.backend in ("simulated", "none") else ""
    # "none" wires nothing at all, so there is nothing to pulse either.
    lines = sorted(set(cfg.event_lines.values())) if cfg.backend != "none" else []
    # Every line by name, with the events mapped onto it: the record has to be
    # checkable against the wiring diagram line by line, and a count of three
    # cannot be. Events are listed because a line is only as good as what the
    # session will actually send down it.
    events_by_line: dict[str, list[str]] = {line: [] for line in lines}
    for event, line in sorted(cfg.event_lines.items()):
        if line in events_by_line:
            events_by_line[line].append(event)
    per_line: list[dict[str, Any]] = [
        {
            "line": line,
            "events": events_by_line[line],
            "pulsed": False,
            "commanded_ms": float(cfg.pulse_ms),
            "measured_ms": None,
        }
        for line in lines
    ]
    evidence: dict[str, Any] = {
        "configured": True,
        "backend": cfg.backend,
        "simulated": cfg.backend in ("simulated", "none"),
        "pulse_requested": pulse,
        "lines": per_line,
    }
    sync: SyncOutput | None = None
    try:
        sync = make_sync(cfg)
        if pulse:
            for entry in per_line:
                started = time.perf_counter()
                sync.pulse(entry["line"])
                # The NI-DAQ backend holds the line high for cfg.pulse_ms with
                # a sleep, so measured-vs-commanded here is how much the host
                # overshot — a line measured at 8 ms against a commanded 2 ms
                # is a pulse a recorder may read as a different event.
                entry["measured_ms"] = round(1000.0 * (time.perf_counter() - started), 1)
                entry["pulsed"] = True
    except AlhazenError as e:
        return CheckResult("sync", False, str(e), {**evidence, "error": str(e)})
    finally:
        # A real sync backend holds its digital-output tasks open for its
        # whole life; leaking them from a short CLI invocation would block
        # the very session that is about to start.
        if sync is not None:
            sync.close()
    fired = f", pulsed {len(lines)}" if pulse else f", {len(lines)}"
    return CheckResult("sync", True, f"{cfg.backend}{fired} line(s){simulated}", evidence)


def _check_recording(rig: RigConfig) -> CheckResult:
    """Is the recorder where the rig config says it is?

    The failure this catches is the one that actually happens: an acquisition
    host's share that did not mount, discovered after a session rather than
    before it.
    """
    cfg = rig.devices.recording
    if cfg is None:
        return CheckResult("recording", True, "not configured on this rig", {"configured": False})
    simulated = " (simulated)" if cfg.backend == "simulated" else ""
    data_dir = Path(cfg.data_dir)
    evidence: dict[str, Any] = {
        "configured": True,
        "backend": cfg.backend,
        "data_dir": str(cfg.data_dir),
        "run_glob": cfg.run_glob,
        "exists": data_dir.exists(),
        # What check() handed back, verbatim: None is the recorder saying
        # nothing is wrong, and a sentence is the recorder saying what is.
        "returned": None,
    }
    try:
        problem = make_recording(cfg).check()
    except AlhazenError as e:
        return CheckResult("recording", False, str(e), {**evidence, "error": str(e)})
    evidence["returned"] = problem
    if problem is not None:
        return CheckResult("recording", False, problem, evidence)
    return CheckResult("recording", True, f"{cfg.backend} at {cfg.data_dir}{simulated}", evidence)


def _check_spikes(rig: RigConfig) -> CheckResult:
    """Can the live spike stream actually be opened?

    Connects the same way a session would — server reachable, an
    acquisition running, the stream present, the channel list valid — and
    closes again without starting the fetch thread. The failure this
    catches is SpikeGLX left un-started (or its command server disabled),
    discovered here rather than with the subject in the chair.

    The ``sorted_stream`` backend needs more than a connect, because a
    ZeroMQ SUB socket connects to an endpoint nobody is publishing on and
    reports success. So that backend is checked by listening (below), which
    is the only thing that can tell a running sorter from a dead one.
    """
    cfg = rig.devices.spikes
    if cfg is None:
        return CheckResult("spikes", True, "not configured on this rig", {"configured": False})
    evidence: dict[str, Any] = {
        "configured": True,
        "backend": cfg.backend,
        "address": cfg.address if cfg.backend == "sorted_stream" else None,
        "simulated": cfg.backend == "simulated",
        "connected": False,
        "units": None,
        "lag_ms": None,
        "dropped_messages": None,
        "listened_s": None,
        "publishing": None,
        "units_announced": None,
        "reported": None,
    }
    source = make_spikes(cfg)
    try:
        source.connect()
        evidence["connected"] = True
        detail = (
            _listen_for_units(source, cfg, evidence)
            if cfg.backend == "sorted_stream"
            else source.describe()
        )
        evidence["reported"] = detail
    except AlhazenError as e:
        # One path for every way this can go wrong, including a stream that
        # is publishing but unusable. A listen that returned its error as a
        # detail string would report it under an OK, which is the exact
        # false clean bill of health this backend's check exists to refuse.
        return CheckResult("spikes", False, str(e), {**evidence, "error": str(e)})
    finally:
        # Nothing must be left holding the command-server connection or the
        # socket: the session that is about to start needs both.
        source.close()
    simulated = " (simulated)" if cfg.backend == "simulated" else ""
    return CheckResult("spikes", True, f"{detail}{simulated}", evidence)


def _listen_for_units(source: Any, cfg: Any, evidence: dict[str, Any]) -> str:
    """Wait for the sorter to announce ``units``, then report the lag.

    check-rig is *by definition* a late joiner: the sorter has been
    publishing since long before anyone ran a check. So this waits for a
    re-announcement, not for a first announcement — the contract requires
    one at least every ``UNITS_REANNOUNCE_PERIOD_MS`` precisely so that this
    wait terminates on a healthy rig (docs/live-spikes.md). Timed messages
    arriving first are held by the source, not refused; if they were
    refused, a FAIL here would be the normal outcome on a working sorter.

    Polls synchronously rather than starting the background thread, for the
    same reason every other check avoids one: a check that leaves a thread
    running has changed the rig it was asked to inspect. The wait is the
    configured heartbeat timeout, since a stream that has not said anything
    within its own silence budget is by definition not publishing.

    The covered-until lag is the number the phase-2 bring-up gate is about:
    it is how far behind real time the sorter's output is, and therefore how
    long a consumer will wait for a window to close.

    Raises ``SpikeSourceError`` for every failure, including nothing
    arriving at all, so the caller has one path to report rather than a
    string it has to inspect. ``evidence`` is filled in as the listen
    proceeds, so a raise still leaves behind how long it listened and whether
    anything was publishing — which is the whole difference between the two
    failures, written down instead of only said.
    """
    clock = MonotonicClock()
    source.configure(clock)
    # Two budgets, because there are two failures. Silence is judged against
    # the stream's own silence budget. A stream that is talking but has not
    # announced itself is judged against the announcement grace instead,
    # which has nothing to do with heartbeats and must not be shortened by a
    # rig that tightened them.
    silence_budget_s = max(cfg.heartbeat_timeout_ms / 1000.0, 0.5)
    units_budget_s = max(silence_budget_s, UNITS_GRACE_MS / 1000.0)
    started = time.monotonic()
    budget_s = silence_budget_s
    while time.monotonic() < started + budget_s:
        source.poll_once()
        if source.n_channels:
            break
        if source.awaiting_units:
            budget_s = units_budget_s
        time.sleep(0.02)
    evidence["listened_s"] = round(time.monotonic() - started, 2)
    # Two separate facts, and the pair is what names the fault: something on
    # the endpoint at all, and a timebase from it.
    evidence["publishing"] = bool(source.n_channels or source.awaiting_units)
    evidence["units_announced"] = bool(source.n_channels)
    if not source.n_channels:
        if source.awaiting_units:
            # The distinction worth making: something IS publishing, so the
            # endpoint and the sorter are alive; what is missing is the one
            # message carrying the sample rate. Saying "nothing is
            # publishing" here would send the experimenter to restart a
            # sorter that is running fine.
            raise SpikeSourceError(
                f"the sorted stream at {cfg.address} is publishing, but the sorter never "
                f"re-announced units within {units_budget_s:g} s. check-rig joins a stream "
                f"already in progress, so the contract requires a 'units' message at least "
                f"every {UNITS_REANNOUNCE_PERIOD_MS / 1000.0:g} s (docs/live-spikes.md); "
                f"without one the sample rate and timebase are unrecoverable for any late "
                f"subscriber, and every spike would land at the same instant"
            )
        raise SpikeSourceError(
            f"no units message from the sorted stream at {cfg.address} within "
            f"{silence_budget_s:g} s — is the real-time sorter running and publishing?"
        )
    # One more poll so a heartbeat that arrived just after the units message
    # has a chance to set coverage; without it the lag reads as unknown on a
    # stream that is working perfectly well.
    covered = source.drain().covered_until
    if covered is None:
        source.poll_once()
        covered = source.drain().covered_until
    lag = "lag unknown (no timed message yet)"
    if covered is not None:
        evidence["lag_ms"] = round(1000.0 * (clock.now() - covered), 1)
        lag = f"lag {evidence['lag_ms']:.0f} ms"
    evidence["units"] = source.n_channels
    evidence["dropped_messages"] = source.dropped_messages
    return f"{source.describe()}, {lag}"


def format_result(result: CheckResult) -> str:
    return f"{'OK  ' if result.ok else 'FAIL'} {result.name}: {result.detail}"


__all__ = ["CheckResult", "check_rig", "format_result"]
