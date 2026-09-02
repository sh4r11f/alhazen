"""check_rig: the pre-session ritual — run it before the subject arrives.

Every check constructs the same backend objects a real session would build
(devices/*.make_*), so a clean result actually predicts a working session
rather than exercising a parallel code path that can drift from it. What it
deliberately does not do is open the subject display: a window is the one
thing that cannot be checked without becoming a session.

Every check runs, always, even after one fails: whoever came to check the
whole rig wants the complete picture from one invocation, not to fix one
problem, re-run, and only then discover a second.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from alhazen.config.models import RewardPulses, RigConfig
from alhazen.core.clock import MonotonicClock
from alhazen.devices.eyetracker import make_tracker
from alhazen.devices.recording import make_recording
from alhazen.devices.reward import make_reward
from alhazen.devices.spikes import make_spikes
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
    name: str
    ok: bool
    detail: str


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
    )


def _check_monitor(rig: RigConfig) -> CheckResult:
    """Does PsychoPy know this rig's monitor, and does it still agree with it?

    The window itself cannot be checked without becoming a session, but its
    monitor registration can — and a registration that has drifted from the
    rig config is exactly what stops the window from opening at all, half an
    hour later, with a subject already in the chair.
    """
    if rig.display.backend != "psychopy":
        return CheckResult(
            "monitor", True, f"no psychopy registration needed ({rig.display.backend} display)"
        )
    try:
        registration = monitor_registry.lookup(rig.monitor.name)
    except DisplayError as e:
        # A rig config that asks for the psychopy backend on a machine without
        # psychopy cannot run a session at all, so this is a rig fault, not a
        # missing niceness.
        return CheckResult("monitor", False, str(e))

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
        )
    drift = monitor_registry.differences(rig.monitor, registration)
    if drift:
        return CheckResult(
            "monitor",
            False,
            f"{rig.monitor.name!r} disagrees with this config ({'; '.join(drift)}) — "
            f"re-register it with `alhazen monitor register --rig <yaml>`",
        )
    gamma = monitor_registry.format_gamma(registration.gamma)
    return CheckResult(
        "monitor", True, f"{rig.monitor.name!r} registered with psychopy, gamma {gamma}"
    )


def _check_data_root(rig: RigConfig) -> CheckResult:
    """Prove the data root is writable now, rather than at teardown — when
    the session's only copy of its data is still in memory."""
    root = rig.data_root
    probe = root / ".alhazen-write-check"
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        return CheckResult("data_root", False, f"{root} is not writable: {e}")
    return CheckResult("data_root", True, f"{root} is writable")


def _check_eyetracker(rig: RigConfig) -> CheckResult:
    cfg = rig.devices.eyetracker
    if cfg is None:
        return CheckResult("eyetracker", True, "not configured on this rig")
    if cfg.backend == "mouse_sim":
        # Never constructed here: it needs a real window to read the mouse
        # from, and check-rig must not open one. There is no hardware behind
        # it to verify anyway.
        return CheckResult("eyetracker", True, "mouse_sim — no hardware to check (simulated)")

    # display=None is safe: connect() and shutdown() — the only methods
    # called here — never touch the window; only configure() does, and
    # calibration graphics need a real session.
    tracker = make_tracker(cfg, None, Screen.from_monitor(rig.monitor), MonotonicClock())
    try:
        tracker.connect()
        # Release the device again: this is a smoke test, not a session, and
        # nothing should be left connected behind it. No destination — no
        # trial ran, so there is no recording to hand back.
        tracker.shutdown(None)
    except AlhazenError as e:
        # Only alhazen's own device errors are a rig fault; anything else is
        # a bug here and keeps its traceback.
        return CheckResult("eyetracker", False, str(e))
    # Named by what the experimenter would have to go and check: an EyeLink
    # is reached over the network, so the IP is the useful half of the
    # message; a TRACKPixx3 is inside the display chassis and has no address
    # to get wrong, so printing one would be noise at best and misleading at
    # worst.
    where = f" at {cfg.host_ip}" if cfg.backend == "eyelink" else ""
    return CheckResult("eyetracker", True, f"{cfg.backend}{where} responded")


def _check_reward(rig: RigConfig, pulse: bool) -> CheckResult:
    cfg = rig.devices.reward
    if cfg is None:
        return CheckResult("reward", True, "not configured on this rig")
    simulated = " (simulated)" if cfg.backend == "simulated" else ""
    try:
        reward = make_reward(cfg)
        if pulse:
            reward.deliver(CHECK_PULSE)
        reward.close()
    except AlhazenError as e:
        return CheckResult("reward", False, str(e))
    fired = f", fired one {CHECK_PULSE.pulse_ms} ms pulse" if pulse else ""
    return CheckResult(
        "reward", True, f"{cfg.backend} on {cfg.device}/{cfg.channel}{fired}{simulated}"
    )


def _check_sync(rig: RigConfig, pulse: bool) -> CheckResult:
    cfg = rig.devices.sync
    if cfg is None:
        return CheckResult("sync", True, "not configured on this rig")
    simulated = " (simulated)" if cfg.backend in ("simulated", "none") else ""
    # "none" wires nothing at all, so there is nothing to pulse either.
    lines = sorted(set(cfg.event_lines.values())) if cfg.backend != "none" else []
    sync: SyncOutput | None = None
    try:
        sync = make_sync(cfg)
        if pulse:
            for line in lines:
                sync.pulse(line)
    except AlhazenError as e:
        return CheckResult("sync", False, str(e))
    finally:
        # A real sync backend holds its digital-output tasks open for its
        # whole life; leaking them from a short CLI invocation would block
        # the very session that is about to start.
        if sync is not None:
            sync.close()
    fired = f", pulsed {len(lines)}" if pulse else f", {len(lines)}"
    return CheckResult("sync", True, f"{cfg.backend}{fired} line(s){simulated}")


def _check_recording(rig: RigConfig) -> CheckResult:
    """Is the recorder where the rig config says it is?

    The failure this catches is the one that actually happens: an acquisition
    host's share that did not mount, discovered after a session rather than
    before it.
    """
    cfg = rig.devices.recording
    if cfg is None:
        return CheckResult("recording", True, "not configured on this rig")
    simulated = " (simulated)" if cfg.backend == "simulated" else ""
    try:
        problem = make_recording(cfg).check()
    except AlhazenError as e:
        return CheckResult("recording", False, str(e))
    if problem is not None:
        return CheckResult("recording", False, problem)
    return CheckResult("recording", True, f"{cfg.backend} at {cfg.data_dir}{simulated}")


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
        return CheckResult("spikes", True, "not configured on this rig")
    source = make_spikes(cfg)
    try:
        source.connect()
        detail = (
            _listen_for_units(source, cfg) if cfg.backend == "sorted_stream" else source.describe()
        )
    except AlhazenError as e:
        # One path for every way this can go wrong, including a stream that
        # is publishing but unusable. A listen that returned its error as a
        # detail string would report it under an OK, which is the exact
        # false clean bill of health this backend's check exists to refuse.
        return CheckResult("spikes", False, str(e))
    finally:
        # Nothing must be left holding the command-server connection or the
        # socket: the session that is about to start needs both.
        source.close()
    simulated = " (simulated)" if cfg.backend == "simulated" else ""
    return CheckResult("spikes", True, f"{detail}{simulated}")


def _listen_for_units(source: Any, cfg: Any) -> str:
    """Wait for the sorter's first ``units`` message, then report the lag.

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
    string it has to inspect.
    """
    clock = MonotonicClock()
    source.configure(clock)
    budget_s = max(cfg.heartbeat_timeout_ms / 1000.0, 0.5)
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        source.poll_once()
        if source.n_channels:
            break
        time.sleep(0.02)
    if not source.n_channels:
        raise SpikeSourceError(
            f"no units message from the sorted stream at {cfg.address} within "
            f"{budget_s:g} s — is the real-time sorter running and publishing?"
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
        lag = f"lag {1000.0 * (clock.now() - covered):.0f} ms"
    return f"{source.describe()}, {lag}"


def format_result(result: CheckResult) -> str:
    return f"{'OK  ' if result.ok else 'FAIL'} {result.name}: {result.detail}"


__all__ = ["CheckResult", "check_rig", "format_result"]
