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
delivery on the valve. The experimenter's manual reward overrides the queue
(``QueuedReward.deliver_manual``): it cancels every drop still waiting and is
delivered once, as soon as the pulse train already on the valve finishes —
which is never cut short.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections import deque
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
    """One delivery, waiting in the worker's line or on the valve.

    ``request`` is set for a mid-trial request (reported back through the
    completion queue, drained by the session thread). ``finished`` is set for
    a synchronous delivery (manual reward, end-of-trial pay), whose caller is
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
    it, so none ever overlaps another on the valve:

    - ``submit(request)`` — a mid-trial drop, from the engine. Returns at once
      with how many deliveries are ahead of it, because an 8 s pursuit at
      120 Hz cannot absorb a 200 ms pulse train inside a frame. Its outcome
      comes back through ``completed()``. Requests that arrive while one is
      delivering wait their turn; none is dropped or merged.
    - ``deliver(pulses)`` — the end-of-trial pay. Waits behind whatever is
      queued, then for its own delivery, and re-raises a failure on the
      caller's thread — the same contract as any ``RewardDispenser``, which
      is why the runner need not know it holds this rather than the device.
      Never cancelled.
    - ``deliver_manual(pulses)`` — the experimenter's manual reward (the ``r``
      key during a trial, R in the pause menu). The same synchronous contract
      as ``deliver``, but it overrides the queue: every drop still waiting is
      cancelled (reported through ``completed()``, never delivered), and the
      manual reward is delivered once, as soon as the delivery already on the
      valve finishes. Drops asked for after it queue as usual.

    So the order at the valve is: the delivery on it finishes (it is never
    cut short — ``deliver_manual`` says why); then the oldest waiting manual
    reward; then the oldest of everything else — which, once a manual reward
    has emptied the queue of drops, is only what was asked for after it.

    Threading: the worker only calls the dispenser and puts plain-data
    completions on a thread-safe queue; a cancellation is put there by the
    thread that asked for the manual reward. Neither ever touches the event
    bus, the recorder or a trial record — the session thread drains the
    queue and emits the events (core/engine.py), so the one-writer rule the
    bus and recorder rely on holds.
    """

    def __init__(self, dispenser: RewardDispenser) -> None:
        self.dispenser = dispenser
        self._done: queue.Queue[RewardCompletion] = queue.Queue()
        # One lock guards everything the session thread and the worker share:
        # both lines below, _outstanding and _closed. One lock, not one per
        # structure, so a count taken while a job joins a line always agrees
        # with where the job was put, and a manual reward's cancellations and
        # its own place in line are one step: the worker cannot take a job,
        # or finish one, in between.
        self._lock = threading.Lock()
        # Two conditions on that one lock, each with its own kind of waiter:
        # the worker waits on _wakeup for a job to arrive (or for close()),
        # and wait_idle waits on _idle for the last delivery to finish.
        self._wakeup = threading.Condition(self._lock)
        self._idle = threading.Condition(self._lock)
        # The jobs waiting for the valve, in two lines, each first in, first
        # out. _manual holds manual rewards (deliver_manual); _queue holds the
        # mid-trial drops (submit) and the end-of-trial pay (deliver). The
        # worker empties _manual before it takes anything from _queue (_take).
        # A job leaves its line when the worker takes it, so neither line ever
        # holds the delivery that is on the valve — which is why a manual
        # reward, cancelling what is in _queue, can never cut that one short.
        self._manual: deque[_Job] = deque()
        self._queue: deque[_Job] = deque()
        # Deliveries asked for and not yet finished or cancelled: everything
        # in both lines plus the one on the valve. What a new drop reports as
        # queued_behind (all of it goes before a job joining _queue) and what
        # wait_idle waits to reach zero.
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
        """Queue a mid-trial drop; return how many deliveries are ahead of it.

        Exact when taken: everything outstanding goes before a new drop — the
        delivery on the valve, a manual reward still waiting, the drops
        queued earlier. A drop asked for while a manual reward waits for the
        valve queues behind it and is not cancelled by it: a manual reward
        cancels only what was waiting when it was asked for.
        """
        return self._enqueue(_Job(pulses=request.pulses, request=request), manual=False)

    def deliver(self, pulses: RewardPulses) -> None:
        """Deliver after everything already queued, and wait for it.

        The end-of-trial pay. It never jumps the queue and a manual reward
        never cancels it: the runner pays between trials, where no frame is
        waiting on it, and only after ``settle_rewards`` has waited for every
        drop — so it finds the queue empty anyway, and follows the trial's
        drops at the valve. It is the trial's outcome, earned and decided;
        only drops, which a manual reward replaces, are ever cancelled.
        """
        self._deliver_and_wait(pulses, manual=False)

    def deliver_manual(self, pulses: RewardPulses) -> None:
        """The experimenter's manual reward: override the queue, deliver this
        once, and wait for it.

        Every mid-trial drop still waiting for the valve is cancelled — taken
        out of the queue and reported through ``completed()`` with
        ``cancelled_by="manual"``, so each gets its own REWARD_CANCELLED and
        none vanishes — and this reward is delivered as soon as the delivery
        already on the valve finishes. Drops asked for after this call queue
        as usual, behind it: the queue builds up again on its own. The
        end-of-trial pay (``deliver``) is never cancelled; one still waiting
        is delivered after this.

        Its caller is the session thread: the ``r`` key blocks the frame it
        was pressed on until the pump is done, so that the manual REWARD is
        emitted after the delivery it records. The wait is at most the rest
        of the train already on the valve plus this one's own. Several calls
        go in the order made, each cancelling the drops waiting when it was
        made (a session makes one at a time: the key is synchronous).

        The train already on the valve is not cut short, for two reasons:

        - The dose. Pulse width is what sets the volume delivered — it is the
          pump's calibration — so a train stopped part-way delivers an amount
          nobody measured, and that drop's REWARD_DELIVERED could not say
          what the subject received.
        - The line. ``NidaqReward`` plays a finite buffered waveform, and an
          analog-output task leaves its last generated sample on the line
          when it stops; that is why ``build_reward_waveform`` always ends at
          0 V. Stopping it mid-pulse — reaching, from this thread, into a
          task the worker owns — would leave the valve open until a second
          write drove the line to 0 V, and a failure of that write would
          flood the subject.

        Interrupting would save at most one train's wait. A failure is
        re-raised here, on the caller's thread. The drops it cancelled stay
        cancelled — each is already reported — and the worker carries on
        with whatever is asked for next.
        """
        self._deliver_and_wait(pulses, manual=True)

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
        finished (``_run``), and a cancellation is reported in the same step
        that stops counting it (``_cancel_queued_drops``).
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
            # Nothing can join a line from here on (_enqueue refuses), so the
            # worker delivers what is already waiting and then stops: _take
            # returns None only once both lines are empty.
            self._closed = True
            self._wakeup.notify()
        try:
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
                    if done.cancelled_by is not None:
                        how = f"CANCELLED by a {done.cancelled_by} reward, never delivered"
                    elif done.error is None:
                        how = "delivered"
                    else:
                        how = f"FAILED — {done.error}"
                    log.error(
                        "mid-trial reward %r (frame %s) ended after the session stopped "
                        "reporting: %s",
                        done.request.reason,
                        done.request.frame,
                        how,
                    )
        finally:
            self.dispenser.close()

    def _deliver_and_wait(self, pulses: RewardPulses, *, manual: bool) -> None:
        """Put a synchronous delivery in line — a manual reward in the manual
        line, the end-of-trial pay at the end of the queue — and block until
        the worker has delivered it, re-raising its failure on this thread."""
        job = _Job(pulses=pulses, finished=threading.Event())
        self._enqueue(job, manual=manual)
        assert job.finished is not None
        # No timeout: the worker sets this whatever the dispenser does, and
        # a real dispenser bounds its own hardware wait (NidaqReward).
        job.finished.wait()
        if job.error is not None:
            raise job.error

    def _enqueue(self, job: _Job, *, manual: bool) -> int:
        """Put a job in line and wake the worker; return how many deliveries
        are ahead of it (on the valve or earlier in line).

        A manual reward first cancels every drop waiting in the queue
        (``_cancel_queued_drops``). All of it happens under the one lock, so
        the count is exact and the cancelling and the manual reward's place
        in line are one step: the worker cannot take a job, or finish one, in
        between. A drop it has not taken yet is cancelled; one it has taken
        is on the valve, and finishes.
        """
        cancelled: list[RewardRequest] = []
        with self._lock:
            if self._closed:
                raise RewardError("reward delivery requested after the reward worker was closed")
            if not self._thread.is_alive():
                # _run catches everything a delivery raises, so this means
                # the thread itself was killed. Queuing onto it would wait
                # forever for a delivery nobody makes.
                raise RewardError("the reward worker thread is no longer running")
            if manual:
                cancelled = self._cancel_queued_drops()
                # Still ahead of it: the delivery on the valve, which always
                # finishes, and any earlier manual reward — everything
                # outstanding except what is left in the queue, which can
                # only be an end-of-trial pay, and which it goes ahead of.
                ahead = self._outstanding - len(self._queue)
                self._manual.append(job)
            else:
                # Everything outstanding goes before a job joining the queue.
                ahead = self._outstanding
                self._queue.append(job)
            self._outstanding += 1
            # notify(), not notify_all(): the worker is the only thread that
            # ever waits on _wakeup.
            self._wakeup.notify()
        if cancelled:
            # Said in the log as well as in the events (REWARD_CANCELLED,
            # which the engine emits when it drains them): drops the subject
            # earned and will not receive.
            log.warning(
                "manual reward cancels %d queued mid-trial drop(s), which will not be "
                "delivered: %s",
                len(cancelled),
                ", ".join(f"{request.reason!r} (frame {request.frame})" for request in cancelled),
            )
        return ahead

    def _cancel_queued_drops(self) -> list[RewardRequest]:
        """Take every mid-trial drop out of the queue, report each as
        cancelled, and return their requests. The caller holds the lock.

        Only drops. A synchronous job in the queue — the end-of-trial pay,
        whose caller is blocked on it — is never cancelled and keeps its
        place. The delivery on the valve is in neither line, so it is never
        touched. Each cancellation is reported in the same step that stops
        counting it as outstanding, so ``wait_idle`` returning still means
        every completion is already in the completion queue.
        """
        kept: deque[_Job] = deque()
        cancelled: list[RewardRequest] = []
        for waiting in self._queue:
            if waiting.request is None:
                kept.append(waiting)
                continue
            cancelled.append(waiting.request)
            # "manual" is the only thing that cancels a drop today, and the
            # value the REWARD_CANCELLED payload carries (core/events.py).
            # Put while holding our lock, which cannot deadlock: the
            # completion queue's own lock is only ever held inside its put and
            # get, and nothing waits for our lock while holding it.
            self._done.put(RewardCompletion(request=waiting.request, cancelled_by="manual"))
        self._queue = kept
        # Never reaches zero here — the manual reward that cancelled them is
        # counted next, in the same step — so nobody in wait_idle needs waking.
        self._outstanding -= len(cancelled)
        return cancelled

    # ------------------------------------------------------------------
    # The worker thread
    # ------------------------------------------------------------------

    def _take(self) -> _Job | None:
        """The next job for the valve, or None once close() has been called
        and both lines are empty. Blocks while there is nothing to deliver.

        The manual line first, then the queue: this is the one place the
        order at the valve is decided.
        """
        with self._wakeup:
            while True:
                if self._manual:
                    return self._manual.popleft()
                if self._queue:
                    return self._queue.popleft()
                if self._closed:
                    return None
                # Releases the lock while it waits, so the session thread
                # can add to a line; re-checked on every wake-up.
                self._wakeup.wait()

    def _run(self) -> None:
        while True:
            job = self._take()
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
