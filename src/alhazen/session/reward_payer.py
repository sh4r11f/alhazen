"""RewardPayer: what a finished trial earned, and paying it at the pump.

Internal to the session package; SessionRunner builds one from its
``reward`` and ``reward_policy`` arguments.

The decision it hides is *the pay rule*: which ``RewardPolicy`` entry a
trial is paid from (its response outcome, or the task's fault reward for a
trial a device cut short), when a trial that earned nothing gets a
NO_REWARD event, and what a pump failure does — plus the words the session
log uses for what a fault trial was paid. ``earned`` is the one place the
rule lives: ``deliver`` pays it, and ``describe_fault_pay`` reports it.

Interface: ``deliver(ctx, outcome, fault)`` once per trial, between trials
(True when the hardware failed, which the caller turns into a pause);
``describe_fault_pay(result, fault, pay_failed)`` for a fault trial's log
line; ``policy``, the attribute the runner rebinds on every stage
transition. The device, the display and the event emitter are the runner's,
handed in; this class owns none of their lifecycles.

Callers must not rely on how a delivery is attempted (one ``deliver`` call
on the device today) nor on which events it emits beyond what the session's
event schema promises (REWARD / NO_REWARD / REWARD_FAILED). The experimenter's
manual reward is not paid here: it is a pause-menu action with its own
events, and goes through the hook the builder wires.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from alhazen.config.models import RewardPulses
from alhazen.core.engine import TrialResult
from alhazen.core.trial import TrialContext
from alhazen.devices.reward import RewardDispenser
from alhazen.display.backend import DisplayBackend
from alhazen.session.streaks import cut_short_by_device
from alhazen.task.reward_policy import RewardPolicy

# The runner's logger, not this module's: these lines were always the
# runner's, session.log names the logger on every line, and a reader (or a
# test) filtering on "alhazen.session.runner" must keep finding them.
log = logging.getLogger("alhazen.session.runner")


def earned_mid_trial(record: dict[str, Any]) -> bool:
    """Did a phase ask for a mid-trial drop this trial? Delivered, failed, or
    cancelled by the experimenter's manual reward — whichever, the trial
    earned it, so NO_REWARD ("earned nothing") would be false."""
    return (
        record.get("n_mid_trial_rewards", 0)
        + record.get("n_mid_trial_reward_failures", 0)
        + record.get("n_mid_trial_rewards_cancelled", 0)
    ) > 0


class RewardPayer:
    """Pays each trial what it earned at its end. See the module docstring
    for what it hides and what callers may rely on."""

    def __init__(
        self,
        device: RewardDispenser | None,
        policy: RewardPolicy | None,
        display: DisplayBackend,
        emit: Callable[[TrialContext, str, dict[str, Any]], None],
    ) -> None:
        """``emit`` is the runner's between-trials emitter: stamped now and
        mirrored into the trial's record, as the engine does mid-trial."""
        self._device = device
        # What each outcome earns. None (or no reward device) means nothing
        # pays at the end of a trial: what is left is the experimenter's
        # manual key and, for a task that declares mid_trial_reward, the drops
        # its phases ask for during the trial. Public because every stage
        # transition rebinds it (SessionRunner._apply_stage_transition).
        self.policy = policy
        self._display = display
        self._emit = emit

    def earned(self, outcome: Any, fault: str | None) -> RewardPulses | None:
        """What this trial earned at its end, scaled — a ``RewardPulses`` — or
        None for nothing. The one place the pay rule lives: ``deliver`` pays
        it, and a fault trial's log line reports it.

        A trial the eye tracker cut short is paid the task's fault reward,
        never its outcome's entry: its ABORTED is the rig's, not something
        the subject earned. Every other trial — a dropped-frames trial
        included, since ``outcome`` is then the response it replaced — is
        paid by its outcome.
        """
        assert self.policy is not None
        if cut_short_by_device(fault):
            return self.policy.pulses_for_fault()
        return self.policy.pulses_for(outcome.name)

    def deliver(self, ctx: TrialContext, outcome: Any, fault: str | None = None) -> bool:
        """Pay out what this outcome earned. Returns True if the hardware
        failed, which the caller turns into a pause.

        ``outcome`` is the subject's response outcome
        (``TrialResult.response_outcome``), which on a trial frame QA recycled
        is the one it replaced — so the REWARD / NO_REWARD / REWARD_FAILED
        payloads name what was paid for, and the row's
        ``outcome_before_frame_qa`` says the same.

        ``fault`` is the system fault the trial was lost to
        (``TrialResult.lost_to_fault``), or None. A trial the eye tracker cut
        short is paid ``RewardPolicy.on_fault`` (``earned``), and its REWARD /
        REWARD_FAILED payloads carry ``fault`` beside ``outcome``: that key
        is how events.csv tells a fault reward from a reward for a response.
        A dropped-frames trial pays for its response and its payloads are
        the ones any response gets.

        The one deliberate catch in the session's trial path. Everywhere else
        a device fault aborts loudly, but here the trial's measurement already
        exists and is about to be written: letting a pump failure propagate
        would throw away a completed trial's data to report a problem with the
        juice line. So it is recorded, marked in the event stream, shown on
        screen, and handed to a human — loudly, but without losing the trial.
        A fault reward that fails takes the same path.
        """
        if self.policy is None or self._device is None or outcome.name == "PAUSED":
            return False
        pulses = self.earned(outcome, fault)
        # What the delivery was for, as every event about it says.
        paid_for: dict[str, Any] = {"outcome": outcome.name}
        if cut_short_by_device(fault):
            paid_for["fault"] = fault
        if pulses is None:
            # A completed trial that earned nothing is a fact the subject
            # experienced. Marked with its own event rather than left as the
            # absence of REWARD, which is indistinguishable from a REWARD that
            # failed to be written. "Nothing" includes the trial itself: one
            # whose phases asked for mid-trial drops earned those, so it gets
            # no NO_REWARD even when its outcome pays nothing at the end.
            # A trial the tracker cut short is not completed, so a task with
            # no on_fault writes nothing here — its fault line in the log says
            # that nothing was paid, and why.
            if outcome.completed and not earned_mid_trial(ctx.record):
                self._emit(ctx, "NO_REWARD", {"outcome": outcome.name})
            return False
        try:
            self._device.deliver(pulses)
        except Exception:
            log.exception("reward delivery failed on trial %d", ctx.trial_index)
            # False unless a mid-trial drop already arrived: `rewarded` says
            # whether any juice reached the subject this trial.
            ctx.record.setdefault("rewarded", False)
            self._emit(ctx, "REWARD_FAILED", paid_for)
            self._display.show_message("REWARD FAILURE — check the pump")
            return True
        ctx.record["rewarded"] = True
        self._emit(
            ctx,
            "REWARD",
            {"manual": False, **paid_for, "pulses": pulses.model_dump(mode="json")},
        )
        return False

    def describe_fault_pay(self, result: TrialResult, fault: str, pay_failed: bool) -> str:
        """What a fault trial was paid at its end, in words, for its log line.

        Reports the decision ``deliver`` made (it asks the same ``earned``),
        and says why when nothing was paid — above all when the task sets no
        ``on_fault``, which is a choice the experimenter may not know they
        made.
        """
        record = result.record
        # Drops a mid-trial-reward task delivered before the fault stay paid,
        # and stay on the row (n_mid_trial_rewards); said here so the line
        # accounts for everything the subject got.
        drops = record.get("n_mid_trial_rewards", 0)
        before = ""
        if drops:
            before = f"; {drops} mid-trial drop(s) delivered before the fault stay counted"
        policy, device = self.policy, self._device
        if policy is None or device is None:
            missing = "reward policy" if policy is None else "reward device"
            return f"Nothing paid at the trial's end: this session has no {missing}{before}"
        outcome = result.response_outcome
        pulses = self.earned(outcome, fault)
        if cut_short_by_device(fault):
            if policy.on_fault is None:
                return (
                    f"The task sets no fault reward (RewardPolicy.on_fault), so nothing was "
                    f"paid for it{before}"
                )
            if pulses is None:
                return (
                    f"The task's fault reward (RewardPolicy.on_fault) comes to no pulses at "
                    f"reward scale {policy.scale:g}, so nothing was paid{before}"
                )
            paid = f"Paid the task's fault reward (RewardPolicy.on_fault), {pulses}"
        else:
            if pulses is None:
                return (
                    f"Paid for the subject's response as on any trial: {outcome.name} pays "
                    f"nothing{before}"
                )
            paid = f"Paid for the subject's response, {outcome.name}, {pulses}"
        delivered = " — the delivery FAILED at the pump" if pay_failed else ", delivered"
        return f"{paid}{delivered}{before}"
