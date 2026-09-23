"""Reward delivery: a pure waveform builder plus simulated and NI-DAQ backends.

The rig drives a juice/water solenoid from an analog-output line on its
NI-DAQ. :func:`build_reward_waveform` turns a pulse spec into the exact
sample buffer written to that line — pure, so the timing that controls how
much the subject receives is testable on any machine — and the backends only
play it out.

Reward *policy* (which outcome earns what) is not here: it lives in
``alhazen.task.reward_policy``, so a backend never decides what a trial
earned — it only plays out the pulse train it is handed.

:class:`QueuedReward` wraps a backend for a task that asks for reward while a
trial runs: it serialises every delivery onto one worker thread, so a
mid-trial drop never blocks the frame loop and never overlaps another
delivery on the valve.
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np

from alhazen.config.models import RewardHwConfig, RewardPulses
from alhazen.core.trial import RewardCompletion, RewardRequest
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


# How long a wait for the reward worker goes before it says so in the log.
# Not a timeout — a pump train has no business being cut short, and NidaqReward
# bounds its own hardware wait — but a session sitting silently on a stuck
# delivery must at least show up in the log as exactly that.
_WAIT_WARNING_S = 5.0


@dataclass
class _Job:
    """One delivery on the worker's queue.

    ``request`` is set for a mid-trial request (reported back through the
    completion queue, drained by the session thread). ``finished`` is set for
    a synchronous delivery (manual key, end-of-trial pay), whose caller is
    blocked on it and gets any error re-raised on its own thread.
    """

    pulses: RewardPulses
    request: RewardRequest | None = None
    finished: threading.Event | None = None
    error: BaseException | None = None


class QueuedReward:
    """One dispenser, one valve, one delivery at a time — on a worker thread.

    Wraps the rig's dispenser for a task that asks for reward mid-trial
    (``Task.mid_trial_reward``). Every delivery the session makes goes through
    it, in the order asked for, so none ever overlaps another on the valve:

    - ``submit(request)`` — a mid-trial drop, from the engine. Returns at once
      with how many deliveries are ahead of it, because an 8 s pursuit at
      120 Hz cannot absorb a 200 ms pulse train inside a frame. Its outcome
      comes back through ``completed()``. Requests that arrive while one is
      delivering wait their turn; none is dropped or merged.
    - ``deliver(pulses)`` — the manual key and the end-of-trial pay. Waits
      behind whatever is queued, then for its own delivery, and re-raises a
      failure on the caller's thread — the same contract as any
      ``RewardDispenser``, which is why the runner and the manual-reward hook
      need not know they hold this rather than the device itself.

    Threading: the worker only calls the dispenser and puts plain-data
    completions on a thread-safe queue. It never touches the event bus, the
    recorder or a trial record — the session thread drains the queue and
    emits the events (core/engine.py), so the one-writer rule the bus and
    recorder rely on holds.
    """

    def __init__(self, dispenser: RewardDispenser) -> None:
        self.dispenser = dispenser
        self._jobs: queue.Queue[_Job | None] = queue.Queue()
        self._done: queue.Queue[RewardCompletion] = queue.Queue()
        # Guards _outstanding and _closed. _outstanding counts deliveries
        # submitted and not yet finished (queued or on the valve), which is
        # both the queued_behind a new request reports and what wait_idle
        # waits to reach zero.
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._outstanding = 0
        self._closed = False
        # A daemon, so a process exiting after a crash is never held open by
        # a pump call that does not return. close() — a teardown step — is
        # what normally stops it, after the queue has been delivered.
        self._thread = threading.Thread(target=self._run, name="alhazen-reward", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------
    # The session thread's side
    # ------------------------------------------------------------------

    def submit(self, request: RewardRequest) -> int:
        """Queue a mid-trial drop; return how many deliveries are ahead of it."""
        return self._enqueue(_Job(pulses=request.pulses, request=request))

    def deliver(self, pulses: RewardPulses) -> None:
        """Deliver now, after everything already queued, and wait for it."""
        job = _Job(pulses=pulses, finished=threading.Event())
        self._enqueue(job)
        assert job.finished is not None
        # No timeout: the worker sets this whatever the dispenser does, and
        # a real dispenser bounds its own hardware wait (NidaqReward).
        job.finished.wait()
        if job.error is not None:
            raise job.error

    def completed(self) -> list[RewardCompletion]:
        """Every mid-trial completion reported since the last call, oldest
        first. Never blocks."""
        done: list[RewardCompletion] = []
        while True:
            try:
                done.append(self._done.get_nowait())
            except queue.Empty:
                return done

    def wait_idle(self) -> None:
        """Block until nothing is queued or on the valve.

        Once this returns, every mid-trial completion is already in the
        completion queue: the worker reports a request *before* it counts it
        finished (``_run``).
        """
        with self._idle:
            while self._outstanding:
                if not self._idle.wait(timeout=_WAIT_WARNING_S):
                    log.warning(
                        "still waiting for %d reward delivery(ies) to finish", self._outstanding
                    )

    def close(self) -> None:
        """Deliver what is still queued, stop the worker, close the dispenser.

        Queued drops are delivered rather than discarded: each was earned and
        already recorded as commanded. The dispenser is closed even when
        stopping the worker fails, so the device is released either way.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            # After every queued job, because nothing can be enqueued once
            # _closed is set: the worker finishes the queue, then stops.
            self._jobs.put(None)
            while self._thread.is_alive():
                self._thread.join(timeout=_WAIT_WARNING_S)
                if self._thread.is_alive():
                    log.warning("still waiting for the reward worker to finish its queue")
            unreported = self.completed()
            if unreported:
                # The session settles every trial before this runs, so these
                # can only come from a path that skipped it. Their outcomes
                # never reached events.csv; the log is the last place to say.
                for done in unreported:
                    log.error(
                        "mid-trial reward %r (frame %s) finished after the session stopped "
                        "reporting: %s",
                        done.request.reason,
                        done.request.frame,
                        "delivered" if done.error is None else f"FAILED — {done.error}",
                    )
        finally:
            self.dispenser.close()

    def _enqueue(self, job: _Job) -> int:
        with self._lock:
            if self._closed:
                raise RewardError("reward delivery requested after the reward worker was closed")
            if not self._thread.is_alive():
                # _run catches everything a delivery raises, so this means
                # the thread itself was killed. Queuing onto it would wait
                # forever for a delivery nobody makes.
                raise RewardError("the reward worker thread is no longer running")
            ahead = self._outstanding
            self._outstanding += 1
            # Put under the lock, so the order jobs reach the valve is the
            # order their `ahead` counts were taken in.
            self._jobs.put(job)
        return ahead

    # ------------------------------------------------------------------
    # The worker thread
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            error: BaseException | None = None
            try:
                self.dispenser.deliver(job.pulses)
            except BaseException as e:  # reported, never allowed to kill the worker
                # BaseException, not Exception: a worker that died here would
                # leave _outstanding above zero for ever, and the next
                # wait_idle — between trials — would never return.
                log.exception("reward delivery of %s failed", job.pulses)
                error = e
            if job.request is not None:
                # Reported BEFORE it counts as finished, so wait_idle
                # returning guarantees the completion is already queued.
                message = None if error is None else f"{type(error).__name__}: {error}"
                self._done.put(RewardCompletion(request=job.request, error=message))
                self._finish_one()
            else:
                # Counted finished BEFORE the waiting caller is released, so
                # a request it submits next does not see this delivery as
                # still ahead of it.
                job.error = error
                self._finish_one()
                assert job.finished is not None
                job.finished.set()

    def _finish_one(self) -> None:
        with self._idle:
            self._outstanding -= 1
            self._idle.notify_all()


def make_reward(cfg: RewardHwConfig) -> RewardDispenser:
    """Construct the dispenser a rig config names. Shared by session build and
    ``check-rig`` so a clean check exercises the real constructor."""
    if cfg.backend == "nidaq":
        return NidaqReward(cfg)
    return SimulatedReward()
