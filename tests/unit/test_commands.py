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
from alhazen.task.reward_policy import RewardPolicy
from alhazen.testing import ScriptedCommands
from alhazen.training import Curriculum, Stage, StageCriteria, TrainingState, TrainingSupervisor
from support import SessionHarness


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
    """``pause_menu`` is the runner's default pause strategy; wire it to a
    KeyboardCommands the way the builder does and check a keypress gets out."""

    @pytest.mark.parametrize(
        ("key", "choice"),
        [("space", "resume"), ("c", "calibrate"), ("q", "quit"), ("escape", "quit")],
    )
    def test_menu_resolves_a_key(self, key, choice):
        commands = KeyboardCommands(key_getter=RecordingGetter([[], [(key, {})]]))
        messages: list[str] = []

        assert pause_menu(messages.append, commands.poll_raw_keys, lambda _s: None) == choice
        assert messages and "PAUSED" in messages[0]


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
        harness = SessionHarness(tmp_path, n_trials=1)
        harness.runner._training = supervisor

        harness.runner.on_session_command(Command.PROMOTE_STAGE)
        harness.runner._apply_stage_transition()
        assert supervisor.stage.name == "real"

        harness.runner.on_session_command(Command.DEMOTE_STAGE)
        harness.runner._apply_stage_transition()
        assert supervisor.stage.name == "easy"
        # Demotion restores the stage's own parameters, not the harder ones.
        assert supervisor._task.params.hold_ms == 100.0
