"""Reward delivery: a pure waveform builder plus simulated and NI-DAQ backends.

The rig drives a juice/water solenoid from an analog-output line on its
NI-DAQ. :func:`build_reward_waveform` turns a pulse spec into the exact
sample buffer written to that line — pure, so the timing that controls how
much the subject receives is testable on any machine — and the backends only
play it out.

Reward *policy* (which outcome earns what) is not here: it lives in
``alhazen.task.reward_policy``, so a backend never decides what a trial
earned — it only plays out the pulse train it is handed.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

import numpy as np

from alhazen.config.models import RewardHwConfig, RewardPulses
from alhazen.errors import RewardError

log = logging.getLogger(__name__)

# The DAQ plays the buffer out on its own hardware clock at this rate, so
# pulse widths are exact regardless of Python-side scheduling jitter — and
# pulse width is exactly what sets the delivered volume.
SAMPLE_RATE_HZ = 1000


@runtime_checkable
class RewardDispenser(Protocol):
    def deliver(self, pulses: RewardPulses) -> None: ...

    def close(self) -> None: ...


def build_reward_waveform(
    voltage: float, pulses: RewardPulses, rate_hz: int = SAMPLE_RATE_HZ
) -> np.ndarray:
    """The analog-output buffer for one reward delivery, one sample per
    ``1/rate_hz`` seconds.

    Always ends at 0 V. A finite NI analog-output task *latches* its last
    written sample on the physical line after the task ends, so a waveform
    ending high (``inter_pulse_ms=0``, whose final sample belongs to the
    pulse) would leave the valve open indefinitely once delivery is "done".
    """
    if pulses.pulse_ms < 1 or pulses.n_pulses < 1:
        raise ValueError(
            f"cannot build a reward waveform from {pulses!r}: n_pulses and pulse_ms must be "
            f">= 1 (a zero-length or zero-count delivery never opens the valve)"
        )
    on = np.full(round(pulses.pulse_ms * rate_hz / 1000), float(voltage))
    off = np.zeros(round(pulses.inter_pulse_ms * rate_hz / 1000))
    waveform = np.concatenate([np.concatenate([on, off]) for _ in range(pulses.n_pulses)])
    if waveform[-1] != 0.0:
        waveform = np.append(waveform, 0.0)
    return waveform


class SimulatedReward:
    """Records deliveries instead of touching hardware — dev machines, tests,
    and any dry run that must exercise trial timing without rewarding."""

    def __init__(self) -> None:
        self.deliveries: list[RewardPulses] = []

    def deliver(self, pulses: RewardPulses) -> None:
        self.deliveries.append(pulses)
        # INFO, not DEBUG: a reward is a session-meaningful thing that whoever
        # is watching the console during a dry run wants to see by default.
        log.info("simulated reward: %s", pulses)

    def close(self) -> None:
        return  # nothing was opened


class NidaqReward:
    """Delivers reward as a finite buffered analog-output waveform."""

    def __init__(self, cfg: RewardHwConfig) -> None:
        try:
            # Lazy: nidaqmx is a rig-only dependency, and importing it at
            # module level would break `import alhazen` everywhere else.
            import nidaqmx  # noqa: F401  (imported only to prove the SDK is present)
        except ImportError as e:
            # Loud at construction, never a silent no-op backend: a session
            # that runs to completion without ever rewarding the subject
            # produces unusable behavior and nobody notices until afterwards.
            raise RewardError(
                "nidaqmx is not installed — install alhazen's [nidaq] extra on the rig, or "
                "use reward backend 'simulated'"
            ) from e
        self._cfg = cfg

    def deliver(self, pulses: RewardPulses) -> None:
        if pulses.n_pulses == 0 or pulses.pulse_ms == 0:
            # A deliberately empty spec (a training stage that has not earned
            # juice yet) is a no-op, not an error — but it is logged, because
            # "the pump never fired" must never be silent.
            log.info("reward delivery skipped: %s delivers nothing", pulses)
            return

        import nidaqmx
        from nidaqmx.constants import AcquisitionType, VoltageUnits

        waveform = build_reward_waveform(self._cfg.voltage, pulses)
        channel = f"{self._cfg.device}/{self._cfg.channel}"
        try:
            with nidaqmx.Task() as task:
                task.ao_channels.add_ao_voltage_chan(
                    channel, min_val=0.0, max_val=10.0, units=VoltageUnits.VOLTS
                )
                task.timing.cfg_samp_clk_timing(
                    rate=SAMPLE_RATE_HZ,
                    sample_mode=AcquisitionType.FINITE,
                    samps_per_chan=len(waveform),
                )
                task.write(waveform.tolist(), auto_start=True)
                task.wait_until_done(timeout=10.0 + len(waveform) / SAMPLE_RATE_HZ)
                task.stop()
        except nidaqmx.DaqError as e:
            # Chained so the nidaqmx traceback survives, while callers still
            # only need one alhazen exception type to catch.
            raise RewardError(f"reward output failed on {channel}: {e}") from e

    def close(self) -> None:
        # Each delivery opens and closes its own task inside a `with`, so
        # there is nothing held between deliveries. Defined for symmetry with
        # NidaqSync, which does hold its tasks.
        return


def make_reward(cfg: RewardHwConfig) -> RewardDispenser:
    """Construct the dispenser a rig config names. Shared by session build and
    ``check-rig`` so a clean check exercises the real constructor."""
    if cfg.backend == "nidaq":
        return NidaqReward(cfg)
    return SimulatedReward()
