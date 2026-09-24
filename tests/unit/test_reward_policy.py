"""Reward policy: what an outcome pays, and what happens when the pump does
not."""

from __future__ import annotations

import csv

from alhazen.config.models import RewardPulses
from alhazen.core.trial import Outcome
from alhazen.devices.reward import SimulatedReward
from alhazen.errors import RewardError
from alhazen.task.plan import TrialPlan
from alhazen.task.reward_policy import RewardPolicy
from support import COMPLETED, FAILED, RunForFrames, SessionHarness

PAID = RewardPulses(n_pulses=2, pulse_ms=100, inter_pulse_ms=50)


class BrokenReward(SimulatedReward):
    """A pump that fails on demand — a blocked line, a DAQ that went away."""

    def __init__(self, fail_on: int = 1) -> None:
        super().__init__()
        self.fail_on = fail_on
        self.attempts = 0

    def deliver(self, pulses: RewardPulses) -> None:
        self.attempts += 1
        if self.attempts == self.fail_on:
            raise RewardError("solenoid did not open")
        super().deliver(pulses)


class TestPolicyTable:
    def test_only_listed_outcomes_pay(self):
        policy = RewardPolicy(by_outcome={"CORRECT": PAID})
        assert policy.pulses_for("CORRECT") == PAID
        assert policy.pulses_for("WRONG") is None

    def test_scale_multiplies_the_pulse_count_only(self):
        # Pulse width is the pump's calibration — how much juice one pulse
        # is — so a training dial must not change it.
        policy = RewardPolicy(by_outcome={"CORRECT": PAID}, scale=2.0)
        scaled = policy.pulses_for("CORRECT")
        assert scaled.n_pulses == 4
        assert scaled.pulse_ms == PAID.pulse_ms

    def test_a_scale_that_rounds_to_nothing_pays_nothing(self):
        policy = RewardPolicy(by_outcome={"CORRECT": RewardPulses(n_pulses=1)}, scale=0.1)
        assert policy.pulses_for("CORRECT") is None


class TestDeliveryInASession:
    def harness(self, tmp_path, reward, outcome=COMPLETED, **kwargs):
        return SessionHarness(
            tmp_path,
            n_trials=1,
            reward=reward,
            reward_policy=RewardPolicy(by_outcome={outcome.name: PAID}),
            build_trial=lambda setup: TrialPlan(phases=[RunForFrames(1, outcome)]),
            **kwargs,
        )

    def read_trials(self, harness):
        with harness.paths.trials_path.open() as f:
            return list(csv.DictReader(f))

    def test_a_paying_outcome_delivers_and_records_it(self, tmp_path):
        reward = SimulatedReward()
        harness = self.harness(tmp_path, reward)
        harness.runner.run()
        assert reward.deliveries == [PAID]
        rows = self.read_trials(harness)
        assert rows[0]["rewarded"] == "True"
        (event,) = [e for e in harness.collector.events if e.name == "REWARD"]
        assert event.payload == {
            "manual": False,
            "outcome": "COMPLETED",
            "pulses": PAID.model_dump(mode="json"),
        }

    def test_an_unlisted_outcome_pays_nothing_and_says_nothing(self, tmp_path):
        reward = SimulatedReward()
        harness = SessionHarness(
            tmp_path,
            n_trials=1,
            reward=reward,
            reward_policy=RewardPolicy(by_outcome={"SOMETHING_ELSE": PAID}),
            build_trial=lambda setup: TrialPlan(phases=[RunForFrames(1, COMPLETED)]),
        )
        harness.runner.run()
        assert reward.deliveries == []
        assert "REWARD" not in harness.collector.names()
        assert "rewarded" not in self.read_trials(harness)[0]

    def test_no_policy_means_no_automatic_reward(self, tmp_path):
        reward = SimulatedReward()
        harness = SessionHarness(
            tmp_path,
            n_trials=1,
            reward=reward,
            build_trial=lambda setup: TrialPlan(phases=[RunForFrames(1, COMPLETED)]),
        )
        harness.runner.run()
        assert reward.deliveries == []


class TestDeliveryFailure:
    def test_the_measurement_survives_a_failed_delivery(self, tmp_path):
        # The one deliberate catch in the runner: a pump fault after a
        # completed trial must not throw away the trial's data.
        reward = BrokenReward(fail_on=1)
        harness = SessionHarness(
            tmp_path,
            n_trials=1,
            reward=reward,
            reward_policy=RewardPolicy(by_outcome={"COMPLETED": PAID}),
            build_trial=lambda setup: TrialPlan(phases=[RunForFrames(1, COMPLETED)]),
        )
        harness.runner.run()

        with harness.paths.trials_path.open() as f:
            rows = list(csv.DictReader(f))
        assert [r["outcome"] for r in rows] == ["COMPLETED"]
        assert rows[0]["rewarded"] == "False"

    def test_failure_is_marked_in_the_event_stream_and_on_screen(self, tmp_path):
        reward = BrokenReward(fail_on=1)
        harness = SessionHarness(
            tmp_path,
            n_trials=1,
            reward=reward,
            reward_policy=RewardPolicy(by_outcome={"COMPLETED": PAID}),
            build_trial=lambda setup: TrialPlan(phases=[RunForFrames(1, COMPLETED)]),
        )
        harness.runner.run()
        names = harness.collector.names()
        assert "REWARD_FAILED" in names
        assert "REWARD" not in names  # never both: they mean opposite things
        assert any("REWARD FAILURE" in message for message in harness.display.messages)

    def test_failure_opens_the_pause_flow(self, tmp_path):
        # A human has to look at the pump before the session carries on
        # rewarding nothing.
        paused: list = []

        def on_pause(menu):
            paused.append(menu)
            return "resume"

        reward = BrokenReward(fail_on=1)
        harness = SessionHarness(
            tmp_path,
            n_trials=2,
            reward=reward,
            reward_policy=RewardPolicy(by_outcome={"COMPLETED": PAID}),
            build_trial=lambda setup: TrialPlan(phases=[RunForFrames(1, COMPLETED)]),
            on_pause=on_pause,
        )
        harness.runner.run()
        assert len(paused) == 1
        # The pause screen leads with the fault, not with the word PAUSED: a
        # pause nobody asked for has to say why it happened before it says
        # which key resumes it.
        assert "REWARD FAILURE" in paused[0].title
        # And the session went on afterwards: the second trial was rewarded.
        assert reward.deliveries == [PAID]

    def test_quitting_at_the_reward_failure_prompt_ends_the_session(self, tmp_path):
        reward = BrokenReward(fail_on=1)
        harness = SessionHarness(
            tmp_path,
            n_trials=3,
            reward=reward,
            reward_policy=RewardPolicy(by_outcome={"COMPLETED": PAID}),
            build_trial=lambda setup: TrialPlan(phases=[RunForFrames(1, COMPLETED)]),
            on_pause=lambda menu: "quit",
        )
        harness.runner.run()
        with harness.paths.trials_path.open() as f:
            assert len(list(csv.DictReader(f))) == 1


class TestNonCompletedOutcomes:
    def test_a_failed_trial_pays_nothing_even_if_listed(self, tmp_path):
        # FAILED is completed=False, so the trial produced no measurement —
        # but the policy is a table of outcome names, and if an experiment
        # lists one it means it.
        reward = SimulatedReward()
        harness = SessionHarness(
            tmp_path,
            n_trials=1,
            reward=reward,
            reward_policy=RewardPolicy(by_outcome={"FAILED": PAID}),
            build_trial=lambda setup: TrialPlan(
                phases=[RunForFrames(1, FAILED if reward.deliveries == [] else COMPLETED)]
            ),
        )
        harness.runner.run()
        assert reward.deliveries == [PAID]


class TestNoReward:
    """A completed trial that earned nothing is a fact the subject
    experienced. Marking it takes its own event: the absence of a REWARD event
    is indistinguishable from a REWARD that failed to be written, and an
    experiment whose message table has a "no reward" string needs something to
    hang it on."""

    def harness(self, tmp_path, outcome, policy=None):
        device = SimulatedReward()
        harness = SessionHarness(
            tmp_path,
            n_trials=1,
            build_trial=lambda setup: TrialPlan(phases=[RunForFrames(1, outcome)]),
            reward=device,
            reward_policy=policy or RewardPolicy(by_outcome={"COMPLETED": PAID}),
        )
        harness.runner.run()
        return harness, device

    def names(self, harness):
        return [event.name for event in harness.collector.events]

    def test_a_completed_outcome_that_pays_nothing_is_marked(self, tmp_path):
        wrong = Outcome("WRONG", completed=True, success=False)
        harness, device = self.harness(tmp_path, wrong)

        assert "NO_REWARD" in self.names(harness)
        assert "REWARD" not in self.names(harness)
        assert device.deliveries == []

    def test_a_paid_outcome_does_not_emit_it(self, tmp_path):
        harness, device = self.harness(tmp_path, COMPLETED)

        assert "REWARD" in self.names(harness)
        assert "NO_REWARD" not in self.names(harness)
        assert len(device.deliveries) == 1

    def test_an_incomplete_outcome_is_not_marked(self, tmp_path):
        """An aborted trial earned nothing because it produced nothing, which
        is a different statement from "did this response earn juice"."""
        outcomes = iter([FAILED, COMPLETED])

        def build_trial(setup):
            # Incomplete outcomes re-queue forever, so the second attempt
            # completes and lets the session end.
            return TrialPlan(phases=[RunForFrames(1, next(outcomes, COMPLETED))])

        device = SimulatedReward()
        harness = SessionHarness(
            tmp_path,
            n_trials=1,
            build_trial=build_trial,
            reward=device,
            reward_policy=RewardPolicy(by_outcome={"COMPLETED": PAID}),
        )
        harness.runner.run()

        # One NO_REWARD would mean the aborted attempt was marked; there is
        # exactly none, and the completed second attempt was paid.
        assert self.names(harness).count("NO_REWARD") == 0
        assert len(device.deliveries) == 1

    def test_a_session_with_no_policy_never_emits_it(self, tmp_path):
        wrong = Outcome("WRONG", completed=True, success=False)
        harness = SessionHarness(
            tmp_path,
            n_trials=1,
            build_trial=lambda setup: TrialPlan(phases=[RunForFrames(1, wrong)]),
        )

        harness.runner.run()

        assert "NO_REWARD" not in [event.name for event in harness.collector.events]

    def test_a_scale_that_rounds_to_zero_counts_as_no_reward(self, tmp_path):
        """A stage that thins reward until a delivery rounds to zero pulses
        has stopped paying, and the record should say so."""
        policy = RewardPolicy(by_outcome={"COMPLETED": RewardPulses(n_pulses=1)}, scale=0.0)
        harness, device = self.harness(tmp_path, COMPLETED, policy=policy)

        assert "NO_REWARD" in self.names(harness)
        assert device.deliveries == []

    def test_it_is_stamped_on_the_row_like_every_other_event(self, tmp_path):
        wrong = Outcome("WRONG", completed=True, success=False)
        harness, _device = self.harness(tmp_path, wrong)

        with harness.paths.trials_path.open() as handle:
            (row,) = list(csv.DictReader(handle))
        assert float(row["t_no_reward"]) > 0


class TestARecycledTrialIsPaidForTheResponse:
    """Frame QA's `recycle_trial` discards a trial whose display dropped too
    many frames and serves its condition again. That is a verdict on the
    data, not on the subject: feedback and reward follow what the subject
    did. Paying on the recycled outcome paid a correct response nothing and
    wrote no NO_REWARD either, right after its feedback said it was right."""

    CORRECT = Outcome("CORRECT", completed=True, success=True)
    WRONG = Outcome("WRONG", completed=True, success=False)
    FIX_BREAK = Outcome("FIX_BREAK", completed=False)

    def session(self, tmp_path, plan, reward=None):
        """A session whose trials run a measuring phase, then TrialFeedback.

        ``plan`` holds ``(slow, outcome)`` per trial served, in order; a slow
        trial overruns every frame of its measuring phase, far past frame
        QA's 10% budget. The feedback verdict is the outcome's own success,
        so what the subject is shown is exactly what their response was.
        """
        from alhazen.config.models import FrameQAConfig
        from alhazen.stimuli.base import NullStimulus
        from alhazen.task.phases import TrialFeedback
        from support import FRAME_S

        served = iter(plan)
        box = {}

        class Slow(RunForFrames):
            def on_frame(self, ctx):
                box["harness"].display.next_flip_extra = FRAME_S
                return super().on_frame(ctx)

        def build(setup):
            slow, outcome = next(served)
            measure = (Slow if slow else RunForFrames)(6, outcome)
            feedback = TrialFeedback(
                verdict=lambda ctx: outcome.success is True,
                then=outcome,
                duration_s=2 * FRAME_S,
            )
            return TrialPlan(
                phases=[measure, feedback], stimuli={"fixation": NullStimulus("fixation")}
            )

        # The session ends after this many completed trials that frame QA
        # kept; every other trial is served again.
        n_kept = sum(1 for slow, outcome in plan if outcome.completed and not slow)
        harness = SessionHarness(
            tmp_path,
            n_trials=n_kept,
            build_trial=build,
            reward=reward if reward is not None else SimulatedReward(),
            reward_policy=RewardPolicy(by_outcome={"CORRECT": PAID}),
            frame_qa=FrameQAConfig(
                policy="recycle_trial", max_dropped_fraction=0.10, max_consecutive_recycles=50
            ),
            on_pause=lambda menu: "resume",
        )
        box["harness"] = harness
        harness.runner.run()
        return harness

    def rows(self, harness):
        with harness.paths.trials_path.open() as f:
            return list(csv.DictReader(f))

    def events(self, harness, trial_index):
        return [e for e in harness.collector.events if e.trial_index == trial_index]

    def test_a_correct_response_is_paid_though_its_trial_is_served_again(self, tmp_path):
        reward = SimulatedReward()
        harness = self.session(tmp_path, [(True, self.CORRECT), (False, self.CORRECT)], reward)

        first, second = self.rows(harness)
        # Data quality follows the display: discarded and served again.
        assert first["outcome"] == "DROPPED_FRAMES"
        assert first["completed"] == "False"
        assert first["outcome_before_frame_qa"] == "CORRECT"
        assert (first["attempt"], second["attempt"]) == ("1", "2")
        assert second["outcome"] == "CORRECT"
        # What the subject was shown and paid follows the response.
        assert first["feedback"] == "success"
        assert first["rewarded"] == "True"
        assert reward.deliveries == [PAID, PAID]
        (paid,) = [e for e in self.events(harness, 1) if e.name == "REWARD"]
        assert paid.payload == {
            "manual": False,
            "outcome": "CORRECT",
            "pulses": PAID.model_dump(mode="json"),
        }
        assert "NO_REWARD" not in [e.name for e in self.events(harness, 1)]

    def test_a_wrong_response_is_marked_unrewarded_though_its_trial_is_served_again(self, tmp_path):
        reward = SimulatedReward()
        harness = self.session(tmp_path, [(True, self.WRONG), (False, self.WRONG)], reward)

        first, _second = self.rows(harness)
        assert first["outcome"] == "DROPPED_FRAMES"
        assert first["outcome_before_frame_qa"] == "WRONG"
        assert first["feedback"] == "failure"
        assert reward.deliveries == []
        names = [e.name for e in self.events(harness, 1)]
        assert "REWARD" not in names
        (declined,) = [e for e in self.events(harness, 1) if e.name == "NO_REWARD"]
        assert declined.payload == {"outcome": "WRONG"}

    def test_a_pump_failure_on_a_recycled_trial_is_a_pump_failure(self, tmp_path):
        """REWARD_FAILED and the pause flow behave as on any paid trial."""
        reward = BrokenReward(fail_on=1)
        harness = self.session(tmp_path, [(True, self.CORRECT), (False, self.CORRECT)], reward)

        first, _second = self.rows(harness)
        assert first["outcome"] == "DROPPED_FRAMES"
        assert first["rewarded"] == "False"
        names = [e.name for e in self.events(harness, 1)]
        assert "REWARD_FAILED" in names
        assert "REWARD" not in names
        assert any("REWARD FAILURE" in message for message in harness.display.messages)
        # The re-served trial was paid once the pump was back.
        assert reward.deliveries == [PAID]

    def test_an_incomplete_trial_on_a_failing_display_is_unaffected(self, tmp_path):
        """Frame QA only recycles a completed trial. A fixation break is being
        served again for its own reason, earned nothing because it produced
        nothing, and gets neither REWARD nor NO_REWARD — as on a clean display."""
        reward = SimulatedReward()
        harness = self.session(tmp_path, [(True, self.FIX_BREAK), (False, self.CORRECT)], reward)

        first, second = self.rows(harness)
        assert first["outcome"] == "FIX_BREAK"
        assert "outcome_before_frame_qa" not in first
        assert first["feedback"] == "failure"
        assert first.get("rewarded", "") == ""
        names = [e.name for e in self.events(harness, 1)]
        assert not {"REWARD", "NO_REWARD", "REWARD_FAILED"} & set(names)
        # The clean trial that followed was paid as usual.
        assert second["outcome"] == "CORRECT"
        assert reward.deliveries == [PAID]
