"""Experimenter command source: what a real keyboard actually reports.

Every other test in the suite injects a scripted key getter, and that is
exactly where three rig-only failures hid: the getter seam decides *which*
keys the psychopy queue is asked for, and a scripted getter never sees that
question. These tests capture the argument the seam passes.
"""

from __future__ import annotations

import pytest

from alhazen.config.models import RewardPulses
from alhazen.core.commands import DEFAULT_KEYMAP, Command, KeyboardCommands, NullCommands
from alhazen.session.runner import pause_menu
from alhazen.task.plan import TrialPlan
from alhazen.task.reward_policy import RewardPolicy
from alhazen.testing import ScriptedCommands
from alhazen.training import Curriculum, Stage, StageCriteria, TrainingState, TrainingSupervisor
from support import COMPLETED, RunForFrames, SessionHarness


class RecordingGetter:
    """A getter shaped like the production one: it is *given* the key filter
    and returns scripted batches. Capturing that argument is the whole point —
    a getter that ignores it cannot see a starved keyList."""

    def __init__(self, batches: list[list[tuple[str, dict[str, bool]]]] | None = None) -> None:
        self.batches = list(batches or [])
        self.calls: list[list[str] | None] = []

    def __call__(self, names: list[str] | None) -> list[tuple[str, dict[str, bool]]]:
        self.calls.append(names)
        return self.batches.pop(0) if self.batches else []


class TestKeyFilter:
    def test_poll_asks_only_for_the_mapped_keys(self):
        """During a trial the subject's response keys share the psychopy
        queue, so poll() must not drain anything it does not bind."""
        getter = RecordingGetter()

        KeyboardCommands(key_getter=getter).poll()

        assert getter.calls == [sorted({name.split("+")[-1] for name in DEFAULT_KEYMAP})]

    def test_poll_raw_keys_asks_for_every_key(self):
        """The pause menu waits for space/q — neither is in the command map.
        Filtering its read to the map's keys makes a paused session
        unresumable from the keyboard."""
        getter = RecordingGetter()

        KeyboardCommands(key_getter=getter).poll_raw_keys()

        assert getter.calls == [None]

    def test_space_reaches_the_pause_menu(self):
        commands = KeyboardCommands(key_getter=RecordingGetter([[("space", {})]]))

        assert commands.poll_raw_keys() == ["space"]

    def test_pyglet_bracket_names_promote_and_demote(self):
        """PsychoPy/pyglet report the bracket keys as bracketright and
        bracketleft; the literal ']' and '[' never arrive from a keyboard."""
        promote = KeyboardCommands(key_getter=RecordingGetter([[("bracketright", {})]]))
        demote = KeyboardCommands(key_getter=RecordingGetter([[("bracketleft", {})]]))

        assert promote.poll() == [Command.PROMOTE_STAGE]
        assert demote.poll() == [Command.DEMOTE_STAGE]

    def test_the_pyglet_names_are_in_the_key_filter(self):
        """Mapping them is not enough — they also have to be asked for."""
        getter = RecordingGetter()

        KeyboardCommands(key_getter=getter).poll()

        assert {"bracketright", "bracketleft"} <= set(getter.calls[0] or [])

    def test_literal_bracket_names_still_map(self):
        """Kept alongside the pyglet names: harmless, and scripted callers
        already use them."""
        commands = KeyboardCommands(key_getter=RecordingGetter([[("]", {}), ("[", {})]]))

        assert commands.poll() == [Command.PROMOTE_STAGE, Command.DEMOTE_STAGE]

    def test_modifier_combination_still_maps(self):
        commands = KeyboardCommands(key_getter=RecordingGetter([[("c", {"ctrl": True})]]))

        assert commands.poll() == [Command.QUIT]

    def test_unmapped_keys_are_ignored_by_poll(self):
        commands = KeyboardCommands(key_getter=RecordingGetter([[("space", {}), ("p", {})]]))

        assert commands.poll() == [Command.PAUSE]

    def test_null_source_is_silent(self):
        null = NullCommands()

        assert null.poll() == [] and null.poll_raw_keys() == []


class TestPauseMenuThroughTheRealSource:
    """The deprecated ``pause_menu`` seam, wired to a KeyboardCommands the way
    the builder used to. Kept because the function is public API until 1.2:
    an experiment package still calling it must keep getting its keys back.

    The live path is tested in test_pause_menu.py and test_pause_flow.py.
    """

    @pytest.mark.filterwarnings("ignore::DeprecationWarning")
    @pytest.mark.parametrize(
        ("key", "choice"),
        [("space", "resume"), ("c", "calibrate"), ("q", "quit"), ("escape", "quit")],
    )
    def test_menu_resolves_a_key(self, key, choice):
        commands = KeyboardCommands(key_getter=RecordingGetter([[], [(key, {})]]))
        messages: list[str] = []

        assert pause_menu(messages.append, commands.poll_raw_keys, lambda _s: None) == choice
        assert messages and "PAUSED" in messages[0]

    @pytest.mark.filterwarnings("ignore::DeprecationWarning")
    def test_a_display_keeps_the_menu_rows_on_their_own_lines(self):
        """The menu's rows are unindented, one key per line — exactly what a
        display's prose reflow would join into a paragraph. A show_message
        that takes ``reflow`` is asked to keep the breaks."""
        from alhazen.testing import FakeClock, FakeDisplay

        display = FakeDisplay(FakeClock())
        commands = KeyboardCommands(key_getter=RecordingGetter([[("space", {})]]))

        assert pause_menu(display.show_message, commands.poll_raw_keys, lambda _s: None) == (
            "resume"
        )
        [(text, reflow)] = display.message_calls
        assert reflow is False
        assert text.startswith("PAUSED")

    @pytest.mark.filterwarnings("ignore::DeprecationWarning")
    def test_a_callable_without_reflow_is_called_with_the_text_alone(self):
        """A plain one-argument callable, the seam's original contract, must
        not be handed a keyword it cannot take."""
        shown: list[str] = []

        def show(text):
            shown.append(text)

        commands = KeyboardCommands(key_getter=RecordingGetter([[("q", {})]]))
        assert pause_menu(show, commands.poll_raw_keys, lambda _s: None) == "quit"
        assert shown and "PAUSED" in shown[0]


class TestSessionResumesFromTheKeyboard:
    def test_a_paused_session_resumes_through_the_raw_key_path(self, tmp_path):
        """End to end: a PAUSE command ends trial 1, and the pause menu reads
        'space' back through the same command source to carry on."""
        commands = ScriptedCommands(batches=[[Command.PAUSE]], raw_keys=[[], ["space"]])
        harness = SessionHarness(tmp_path, n_trials=2, commands=commands, use_pause_menu=True)

        harness.runner.run()

        names = [event.name for event in harness.collector.events]
        assert "PAUSED" in names and "RESUMED" in names
        # The paused trial wrote no row; the two planned trials still ran.
        assert [row["trial_index"] for row in harness.recorder.trials] == [2, 3]


class StageTask:
    """A stand-in for a Task: the supervisor needs only params and reward."""

    def __init__(self) -> None:
        from alhazen.config.models import Model

        class Params(Model):
            hold_ms: float = 500.0

        self.params = Params()
        self.reward = RewardPolicy(by_outcome={"COMPLETED": RewardPulses(n_pulses=1)})


class TestStageCommandsMoveTheSubject:
    def supervisor(self, tmp_path) -> TrainingSupervisor:
        never = StageCriteria(window=100, min_trials=100)
        return TrainingSupervisor(
            curriculum=Curriculum(
                stages=[
                    Stage(name="easy", overrides={"hold_ms": 100.0}, criteria=never),
                    Stage(name="real", criteria=never),
                ]
            ),
            state=TrainingState(stage="easy"),
            task=StageTask(),
            data_root=tmp_path,
            subject="t01",
            session_id="ses-001_run-01",
        )

    def test_promote_then_demote_both_reach_the_curriculum(self, tmp_path):
        """The demote path had no coverage anywhere: neither key can fire on
        a real rig today, so nothing noticed."""
        supervisor = self.supervisor(tmp_path)
        # Through a session, as an experimenter presses them: PROMOTE during
        # trial 1 and DEMOTE during trial 2, each applied between trials.
        # Each trial is RunForFrames(2) — three polls — so the fourth poll is
        # trial 2's first frame.
        commands = ScriptedCommands([[Command.PROMOTE_STAGE], [], [], [Command.DEMOTE_STAGE]])
        stage_at_build: list[str] = []
        hold_ms_at_build: list[float] = []

        def build(setup):
            # The stage each trial is built at, and the task's parameters
            # then: what the transition before it left the subject on.
            # (Teardown hands the task back at its base parameters, so the
            # stage's own have to be read while the session runs.)
            stage_at_build.append(supervisor.stage.name)
            hold_ms_at_build.append(supervisor._task.params.hold_ms)
            return TrialPlan(phases=[RunForFrames(2, COMPLETED)])

        harness = SessionHarness(
            tmp_path, n_trials=3, commands=commands, build_trial=build, training=supervisor
        )
        harness.runner.run()

        # Promoted after trial 1: trial 2 ran at "real".
        assert stage_at_build[1] == "real"
        # Demoted after trial 2: trial 3, and the session's end, back at "easy".
        assert stage_at_build[2] == "easy"
        assert supervisor.stage.name == "easy"
        changes = [e.payload for e in harness.collector.events if e.name == "STAGE_CHANGED"]
        assert [(c["from"], c["to"]) for c in changes] == [("easy", "real"), ("real", "easy")]
        # Demotion restores the stage's own parameters, not the harder ones.
        assert hold_ms_at_build[2] == 100.0
