"""The live monitor's buttons as the session takes them (session/operator_commands.py)
and the command source that merges them into a running trial (core/commands.py
MergedCommands).

Two layers: OperatorCommands on its own, against a queue that hands over the
commands a test scripts, and whole sessions (SessionHarness, wired the way
build_session wires a rig with the live monitor on) showing that a Pause
clicked in the page ends a running trial through the same engine branch as P
at the rig, and that every command is acknowledged once, in the record and as
an OPERATOR_COMMAND event.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alhazen.core.commands import Command, MergedCommands
from alhazen.devices.reward import SimulatedReward
from alhazen.live_monitor.controls import KNOWN_COMMANDS, STATUS_COMMANDS, status_refusal
from alhazen.live_monitor.runtime import LiveMonitorCommand
from alhazen.session.operator_commands import (
    RECENT,
    Capabilities,
    OperatorCommands,
)
from alhazen.testing import FakeClock, ScriptedCommands
from support import SessionHarness


class ScriptedQueue:
    """The live monitor's command queue: each poll hands over the next batch
    of (name, status it was accepted in), then nothing. Acknowledgement
    records are kept as the page would read them."""

    url = "http://127.0.0.1:0/"

    def __init__(self, batches: list[list[tuple[str, str]]] | None = None) -> None:
        self.batches = [list(batch) for batch in (batches or [])]
        self.polls = 0
        self.ack_records: list[list[dict]] = []
        self.states: list[dict] = []
        self._ids = 0

    def poll_commands(self) -> list[LiveMonitorCommand]:
        self.polls += 1
        batch = self.batches.pop(0) if self.batches else []
        commands = []
        for name, status in batch:
            self._ids += 1
            commands.append(LiveMonitorCommand(f"r{self._ids}", name, status=status))
        return commands

    def publish_commands(self, records: list[dict]) -> None:
        self.ack_records.append(records)

    # What the runner needs of a live monitor besides the queue.
    def publish(self, state: dict) -> None:
        self.states.append(state)

    def publish_camera(self, pixels, t, eyes=None) -> None:
        raise AssertionError("no camera here")

    def poll_settings(self) -> list:
        return []

    def save(self, figures_dir: Path, state: dict) -> None:
        figures_dir.mkdir(parents=True, exist_ok=True)

    def stop(self) -> None:
        pass


def bound(queue: ScriptedQueue, caps: Capabilities | None = None):
    operator = OperatorCommands(queue)
    events: list[tuple[str, dict]] = []
    operator.bind(
        emit=lambda name, payload: events.append((name, payload)),
        capabilities=caps or Capabilities(tracker=True, reward=True),
        clock=FakeClock(),
    )
    return operator, events


class TestTheAllowSets:
    def test_every_known_command_is_allowed_somewhere_and_refused_with_a_reason_elsewhere(self):
        for name in KNOWN_COMMANDS:
            allowed = [status for status, names in STATUS_COMMANDS.items() if name in names]
            assert allowed, name
            for status in ("running", "paused", "calibrating", "starting", "complete"):
                refusal = status_refusal(name, status)
                assert (refusal is None) == (status in allowed), (name, status, refusal)

    def test_the_eye_tracker_procedures_are_paused_only_and_say_so(self):
        """The user's rule (2026-10-14), the same on every rig: Calibrate,
        Validate and Drift correct only while the task is paused; every other
        command also works during a running trial."""
        from alhazen.live_monitor.controls import PROCEDURE_COMMANDS

        assert {"calibrate", "validate", "drift_correct"} == PROCEDURE_COMMANDS
        assert not PROCEDURE_COMMANDS & STATUS_COMMANDS["running"]
        assert STATUS_COMMANDS["paused"] >= PROCEDURE_COMMANDS
        assert {"pause", "skip_trial", "manual_reward", "quit"} <= STATUS_COMMANDS["running"]
        assert status_refusal("calibrate", "running") == (
            "Calibrate works only while the task is paused; pause it first"
        )

    def test_the_rig_keys_are_judged_by_the_same_table(self):
        """Every command a rig key can send during a trial has its name in the
        controls table, and the only one the running allow-set refuses is
        calibrate: the keyboard and the page share one rule."""
        from alhazen.core.commands import DEFAULT_KEYMAP
        from alhazen.session.operator_commands import KEYBOARD_COMMAND_NAMES

        names = {KEYBOARD_COMMAND_NAMES[command] for command in DEFAULT_KEYMAP.values()}
        assert names <= KNOWN_COMMANDS
        assert names - STATUS_COMMANDS["running"] == {"calibrate"}
        # No key starts validate or drift correction during a trial: they are
        # pause-menu rows only (read while paused, session/pause.py).
        assert not names & {"validate", "drift_correct"}

    def test_a_refused_rig_key_is_recorded_like_a_page_command(self):
        queue = ScriptedQueue()
        operator, events = bound(queue)
        note = status_refusal("calibrate", "running")
        assert note is not None
        operator.record_keyboard("calibrate", "keyboard-1", "refused", note)
        assert events == [
            (
                "OPERATOR_COMMAND",
                {
                    "name": "calibrate",
                    "request_id": "keyboard-1",
                    "source": "keyboard",
                    "result": "refused",
                    "note": note,
                },
            )
        ]
        (record,) = queue.ack_records[-1]
        assert (record["source"], record["result"]) == ("keyboard", "refused")

    def test_an_unknown_name_is_a_bug_not_a_refusal(self):
        with pytest.raises(ValueError, match="unknown operator command"):
            status_refusal("recalibrate", "paused")


class TestControls:
    def test_running_lists_what_a_trial_accepts_with_the_rig_keys(self):
        operator, _ = bound(ScriptedQueue())
        controls = operator.controls("running")
        assert controls["shown"] == [
            "pause",
            "resume",
            "calibrate",
            "validate",
            "drift_correct",
            "manual_reward",
            "skip_trial",
            "quit",
        ]
        assert controls["available"] == [
            "pause",
            "manual_reward",
            "skip_trial",
            "quit",
        ]
        # The eye-tracker procedures are paused-only on every rig.
        assert set(controls["refused"]) == {"resume", "calibrate", "validate", "drift_correct"}
        for name, label in (
            ("calibrate", "Calibrate"),
            ("validate", "Validate"),
            ("drift_correct", "Drift correct"),
        ):
            assert controls["refused"][name] == (
                f"{label} works only while the task is paused; pause it first"
            )
        assert controls["keys"] == {
            "pause": "P",
            "resume": "SPACE",
            "calibrate": "C",
            "validate": "V",
            "drift_correct": "D",
            "manual_reward": "R",
            "skip_trial": "ESC",
            "quit": "CTRL-C",
        }

    def test_the_page_tests_draw_from_what_the_session_publishes(self):
        """The JS button tests read tests/fixtures/live_monitor_controls.json;
        it must be the session's own output, or they test a different table."""
        import json

        fixture = json.loads(
            (Path(__file__).parents[1] / "fixtures" / "live_monitor_controls.json").read_text(
                encoding="utf-8"
            )
        )
        operator, _ = bound(ScriptedQueue())
        assert fixture["running"] == operator.controls("running")
        assert fixture["paused"] == operator.controls("paused")

    def test_paused_lists_the_pause_menu_and_quit_is_q(self):
        operator, _ = bound(ScriptedQueue())
        controls = operator.controls("paused")
        assert controls["available"] == [
            "resume",
            "calibrate",
            "validate",
            "drift_correct",
            "manual_reward",
            "quit",
        ]
        assert controls["refused"]["pause"] == "the session is already paused"
        assert controls["keys"]["quit"] == "Q"

    def test_a_missing_device_is_a_reason_not_a_silent_button(self):
        operator, _ = bound(ScriptedQueue(), Capabilities(tracker=False, reward=False))
        controls = operator.controls("paused")
        assert controls["refused"]["calibrate"] == "this session has no eye tracker"
        assert controls["refused"]["manual_reward"] == "this session has no reward line"
        assert "calibrate" in controls["shown"]

    def test_an_unattended_run_does_not_offer_a_pause(self):
        operator, _ = bound(ScriptedQueue(), Capabilities(attended=False))
        controls = operator.controls("running")
        assert "pause" not in controls["available"]
        assert "unattended" in controls["refused"]["pause"]
        assert "skip_trial" in controls["available"]

    def test_the_stage_keys_appear_only_with_a_curriculum(self):
        operator, _ = bound(ScriptedQueue(), Capabilities(training=True))
        controls = operator.controls("running")
        assert controls["shown"][-4:] == ["promote_stage", "demote_stage", "hold_stage", "quit"]
        assert controls["keys"]["promote_stage"] == "]"
        assert controls["keys"]["hold_stage"] == "H"


class TestTakingCommands:
    def test_a_running_command_goes_to_the_trial_acknowledged_as_applied(self):
        queue = ScriptedQueue([[("pause", "running")]])
        operator, events = bound(queue)
        assert operator.take_for_trial() is Command.PAUSE
        ((name, payload),) = events
        assert name == "OPERATOR_COMMAND"
        assert payload["name"] == "pause" and payload["result"] == "applied"
        assert payload["source"] == "live_monitor" and payload["request_id"] == "r1"
        assert queue.ack_records[-1][0]["result"] == "applied"

    def test_one_command_per_frame_the_next_waits(self):
        queue = ScriptedQueue([[("manual_reward", "running"), ("skip_trial", "running")]])
        operator, _ = bound(queue)
        assert operator.take_for_trial() is Command.MANUAL_REWARD
        assert operator.take_for_trial() is Command.SKIP_TRIAL
        assert operator.take_for_trial() is None

    def test_a_command_from_a_pause_never_fires_in_a_trial(self):
        """A Resume accepted while paused, arriving after the keyboard
        resumed: discarded, and the running command behind it still taken."""
        queue = ScriptedQueue([[("resume", "paused"), ("quit", "paused"), ("pause", "running")]])
        operator, events = bound(queue)
        assert operator.take_for_trial() is Command.PAUSE
        assert [(p["name"], p["result"]) for _, p in events] == [
            ("resume", "discarded"),
            ("quit", "discarded"),
            ("pause", "applied"),
        ]

    def test_a_command_the_session_cannot_act_on_is_refused(self):
        queue = ScriptedQueue([[("manual_reward", "running")]])
        operator, events = bound(queue, Capabilities(reward=False))
        assert operator.take_for_trial() is None
        assert events[0][1]["result"] == "refused"
        assert events[0][1]["note"] == "this session has no reward line"

    def test_a_procedure_that_reaches_a_trial_is_refused_never_acted_on(self):
        """The server refuses Calibrate/Validate/Drift correct while a trial
        runs. One that reaches the session stamped "running" anyway (a server
        older than the table) is acknowledged refused with the reason, logged
        as OPERATOR_COMMAND, and the command behind it is still taken."""
        queue = ScriptedQueue(
            [[("calibrate", "running"), ("validate", "running"), ("pause", "running")]]
        )
        operator, events = bound(queue)
        assert operator.take_for_trial() is Command.PAUSE
        assert [(p["name"], p["result"], p["note"]) for _, p in events] == [
            (
                "calibrate",
                "refused",
                "Calibrate works only while the task is paused; pause it first",
            ),
            ("validate", "refused", "Validate works only while the task is paused; pause it first"),
            ("pause", "applied", "the trial ends here and the session pauses"),
        ]
        assert all(name == "OPERATOR_COMMAND" for name, _ in events)
        assert [r["result"] for r in queue.ack_records[-1]] == ["refused", "refused", "applied"]

    def test_a_command_the_pause_cannot_take_is_refused_not_handed_on(self):
        queue = ScriptedQueue([[("skip_trial", "paused"), ("calibrate", "paused")]])
        operator, events = bound(queue)
        assert [c.name for c in operator.take_for_pause()] == ["calibrate"]
        assert [(p["name"], p["result"]) for _, p in events] == [("skip_trial", "refused")]

    def test_the_pause_takes_only_what_was_sent_to_a_pause(self):
        queue = ScriptedQueue([[("manual_reward", "running"), ("resume", "paused")]])
        operator, events = bound(queue)
        taken = operator.take_for_pause()
        assert [c.name for c in taken] == ["resume"]
        # The pause acknowledges what it takes once it has acted on it; the
        # one it did not take is discarded now.
        assert [(p["name"], p["result"]) for _, p in events] == [("manual_reward", "discarded")]

    def test_discarding_acknowledges_each_command(self):
        queue = ScriptedQueue([[("pause", "running"), ("manual_reward", "running")]])
        operator, events = bound(queue)
        dropped = operator.discard_pending("queued before this pause began; not acted on")
        assert [c.name for c in dropped] == ["pause", "manual_reward"]
        assert {p["result"] for _, p in events} == {"discarded"}

    def test_the_record_keeps_the_newest_and_says_when(self):
        queue = ScriptedQueue([[("skip_trial", "running")] for _ in range(RECENT + 5)])
        operator, _ = bound(queue)
        for _ in range(RECENT + 5):
            operator.take_for_trial()
        recent = operator.recent()
        assert len(recent) == RECENT
        assert recent[-1]["request_id"] == f"r{RECENT + 5}"
        assert set(recent[0]) == {"request_id", "name", "source", "result", "note", "t"}

    def test_acknowledging_before_the_runner_bound_it_is_a_wiring_bug(self):
        operator = OperatorCommands(ScriptedQueue([[("pause", "running")]]))
        with pytest.raises(RuntimeError, match="before bind"):
            operator.take_for_trial()


class TestMergedCommands:
    def test_the_keyboard_first_and_the_remote_only_on_a_quiet_frame(self):
        asked: list[int] = []

        def remote() -> Command | None:
            asked.append(1)
            return Command.SKIP_TRIAL

        merged = MergedCommands(ScriptedCommands([[Command.PAUSE], []]), remote)
        assert merged.poll() == [Command.PAUSE]
        assert asked == []
        assert merged.poll() == [Command.SKIP_TRIAL]
        assert asked == [1]

    def test_raw_keys_are_the_keyboards(self):
        merged = MergedCommands(ScriptedCommands(raw_keys=[["space"]]), lambda: None)
        assert merged.poll_raw_keys() == ["space"]


def _names(harness) -> list[str]:
    return [event.name for event in harness.collector.events]


def _acks(harness) -> list[tuple[str, str]]:
    return [
        (event.payload["name"], event.payload["result"])
        for event in harness.collector.events
        if event.name == "OPERATOR_COMMAND"
    ]


class TestThroughASession:
    """Whole sessions, wired as build_session wires a rig with the live
    monitor on: the page's queue merged into the engine's commands."""

    def test_a_pause_from_the_page_pauses_like_p_and_a_resume_from_the_page_resumes(
        self, tmp_path: Path
    ):
        # First frame of trial 1: a Pause clicked while running. The pause
        # drains (empty), then a Resume clicked while paused.
        queue = ScriptedQueue([[("pause", "running")], [], [("resume", "paused")]])
        harness = SessionHarness(
            tmp_path / "page",
            n_trials=1,
            live_monitor=queue,
            on_pause=lambda menu: "resume",
            merge_live_monitor=True,
        )
        harness.runner.run()

        # The same session with P pressed at the rig instead.
        keyboard = SessionHarness(
            tmp_path / "keys",
            n_trials=1,
            commands=ScriptedCommands([[Command.PAUSE]]),
            live_monitor=ScriptedQueue([[], [("resume", "paused")]]),
            on_pause=lambda menu: "resume",
            merge_live_monitor=True,
        )
        keyboard.runner.run()

        page_names = [n for n in _names(harness) if n != "OPERATOR_COMMAND"]
        key_names = [n for n in _names(keyboard) if n != "OPERATOR_COMMAND"]
        assert page_names == key_names
        assert "PAUSED" in page_names and "RESUMED" in page_names
        # The click is on the record before what it caused, and acknowledged.
        names = _names(harness)
        assert names.index("OPERATOR_COMMAND") < names.index("PAUSED")
        assert _acks(harness) == [("pause", "applied"), ("resume", "applied")]
        (paused,) = [e for e in harness.collector.events if e.name == "PAUSED"]
        assert paused.payload == {}
        # The paused trial wrote no row; its condition was served again.
        assert [row["trial_index"] for row in harness.recorder.trials] == [2]
        statuses = [state["status"] for state in queue.states]
        assert "paused" in statuses and statuses[-1] == "complete"
        # Every state carries the acknowledgements and the controls.
        assert [c["name"] for c in queue.states[-1]["commands"]] == ["pause", "resume"]
        paused_state = next(s for s in queue.states if s["status"] == "paused")
        assert "resume" in paused_state["controls"]["available"]

    def test_calibrate_from_the_page_during_a_trial_neither_pauses_nor_calibrates(
        self, tmp_path: Path
    ):
        queue = ScriptedQueue([[("calibrate", "running")]])
        harness = SessionHarness(
            tmp_path,
            n_trials=1,
            live_monitor=queue,
            on_pause=lambda menu: "resume",
            merge_live_monitor=True,
        )
        harness.runner.run()
        assert "PAUSED" not in _names(harness)
        assert _acks(harness) == [("calibrate", "refused")]

    def test_calibrate_key_during_a_trial_shows_on_the_page(self, tmp_path: Path):
        """C at the rig during a trial: refused, the trial completes, and the
        refusal is in the record the page lists under its buttons."""
        queue = ScriptedQueue()
        harness = SessionHarness(
            tmp_path,
            n_trials=1,
            commands=ScriptedCommands([[Command.CALIBRATE]]),
            live_monitor=queue,
            on_pause=lambda menu: "resume",
            merge_live_monitor=True,
        )
        harness.runner.run()
        assert "PAUSED" not in _names(harness)
        assert [row["trial_index"] for row in harness.recorder.trials] == [1]
        (record,) = queue.states[-1]["commands"]
        assert (record["name"], record["source"], record["result"]) == (
            "calibrate",
            "keyboard",
            "refused",
        )
        assert record["request_id"] == "keyboard-1"
        assert [row["trial_index"] for row in harness.recorder.trials] == [1]

    def test_skip_from_the_page_abandons_the_trial(self, tmp_path: Path):
        queue = ScriptedQueue([[("skip_trial", "running")]])
        harness = SessionHarness(
            tmp_path,
            n_trials=1,
            live_monitor=queue,
            on_pause=lambda m: "resume",
            merge_live_monitor=True,
        )
        harness.runner.run()
        first = harness.recorder.trials[0]
        assert first["outcome"] == "ABORTED" and first["abort_reason"] == "skipped_by_user"
        assert _acks(harness) == [("skip_trial", "applied")]

    def test_a_reward_from_the_page_is_delivered_mid_trial(self, tmp_path: Path):
        reward = SimulatedReward()
        queue = ScriptedQueue([[("manual_reward", "running")]])
        harness = SessionHarness(
            tmp_path,
            n_trials=1,
            reward=reward,
            pause_menu_reward=True,
            live_monitor=queue,
            on_pause=lambda m: "resume",
            merge_live_monitor=True,
        )
        harness.runner.run()
        assert len(reward.deliveries) == 1
        rewards = [e for e in harness.collector.events if e.name == "REWARD"]
        assert rewards and rewards[0].payload["manual"] is True
        assert _acks(harness) == [("manual_reward", "applied")]

    def test_a_reward_clicked_as_the_trial_ended_is_discarded_at_the_pause(self, tmp_path: Path):
        """Pause and Reward clicked together while running: the pause takes
        the session, and the Reward — meant for a trial — is not delivered
        into the pause, but acknowledged as discarded."""
        reward = SimulatedReward()
        queue = ScriptedQueue(
            [[("pause", "running"), ("manual_reward", "running")], [], [("resume", "paused")]]
        )
        harness = SessionHarness(
            tmp_path,
            n_trials=1,
            reward=reward,
            pause_menu_reward=True,
            live_monitor=queue,
            on_pause=lambda m: "resume",
            merge_live_monitor=True,
        )
        harness.runner.run()
        assert reward.deliveries == []
        assert _acks(harness) == [
            ("pause", "applied"),
            ("manual_reward", "discarded"),
            ("resume", "applied"),
        ]

    def test_quit_from_the_page_ends_the_session(self, tmp_path: Path):
        queue = ScriptedQueue([[("quit", "running")]])
        harness = SessionHarness(
            tmp_path,
            n_trials=3,
            live_monitor=queue,
            on_pause=lambda m: "resume",
            merge_live_monitor=True,
        )
        harness.runner.run()
        assert harness.recorder.trials == []
        assert _acks(harness) == [("quit", "applied")]
        assert queue.states[-1]["status"] in ("complete", "cancelled")

    def test_a_reward_click_on_a_rig_without_a_pump_is_refused_while_paused(self, tmp_path: Path):
        queue = ScriptedQueue([[], [("manual_reward", "paused")], [("resume", "paused")]])
        harness = SessionHarness(
            tmp_path,
            n_trials=1,
            commands=ScriptedCommands([[Command.PAUSE]]),
            live_monitor=queue,
            on_pause=lambda m: "resume",
            merge_live_monitor=True,
        )
        harness.runner.run()
        assert _acks(harness) == [("manual_reward", "refused"), ("resume", "applied")]
