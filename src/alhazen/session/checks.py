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
from dataclasses import dataclass

from alhazen.config.models import RewardPulses, RigConfig
from alhazen.core.clock import MonotonicClock
from alhazen.devices.eyetracker import make_tracker
from alhazen.devices.recording import make_recording
from alhazen.devices.reward import make_reward
from alhazen.devices.sync import SyncOutput, make_sync
from alhazen.display.screen import Screen
from alhazen.errors import AlhazenError

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
        _check_data_root(rig),
        _check_eyetracker(rig),
        _check_reward(rig, pulse),
        _check_sync(rig, pulse),
        _check_recording(rig),
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
        )
        if cfg is not None
    ]
    return CheckResult(
        "config",
        True,
        f"valid — {rig.display.backend} display, devices: "
        f"{', '.join(configured) if configured else 'none configured'}",
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


def format_result(result: CheckResult) -> str:
    return f"{'OK  ' if result.ok else 'FAIL'} {result.name}: {result.detail}"


__all__ = ["CheckResult", "check_rig", "format_result"]
