"""PauseController: resolving a pause, without a runner.

The pause flow is pinned through whole sessions in test_pause_flow.py and
test_pause_menu.py (and the live monitor's side in test_live_monitor.py). These
drive the controller directly with the runner's hooks replaced by
recorders, which is enough to pin how a pause ends: resumed, by itself, or
quit, and what a menu choice that is neither reaches.
"""

from __future__ import annotations

import logging
from typing import Any

from alhazen.core.commands import Command
from alhazen.session.pause import PauseMenu
from alhazen.session.pause_control import PauseController
from alhazen.testing import FakeClock, FakeDisplay, ScriptedCommands


class Pauses:
    """A PauseController on a fake display and clock, with the runner's
    publisher, emitter and stage-command handler recorded."""

    def __init__(
        self,
        answers: list[str] | None = None,
        *,
        raw_keys: list[list[str]] | None = None,
        rest_resume_after_s: float | None = None,
    ) -> None:
        self.clock = FakeClock()
        self.display = FakeDisplay(self.clock, 1 / 60)
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.commands: list[Command] = []
        self.menus: list[PauseMenu] = []
        self.published: list[tuple[str, str | None]] = []
        # None when no keyboard is wired: an unattended run.
        on_pause = None
        if answers is not None:
            script = iter(answers)

            def on_pause(menu: PauseMenu) -> str:
                self.menus.append(menu)
                return next(script)

        self.controller = PauseController(
            display=self.display,
            clock=self.clock,
            commands=ScriptedCommands(raw_keys=raw_keys),
            wait=self.clock.advance,
            on_pause=on_pause,
            rest_resume_after_s=rest_resume_after_s,
            eyetracker=None,
            live_monitor=None,
            has_training=True,
            manual_reward=None,
            manual_reward_payload={},
            publish=lambda status, message: self.published.append((status, message)),
            last_message=lambda: None,
            emit_session_event=lambda name, payload: self.events.append((name, payload)),
            on_session_command=self.commands.append,
        )


class TestHowAPauseEnds:
    def test_an_unattended_pause_resumes_at_once_with_its_menu_drawn(self):
        pauses = Pauses()
        assert pauses.controller.handle({}, fault="REWARD FAILURE — check the pump")
        assert pauses.display.menus[-1][0].startswith("REWARD FAILURE")
        assert pauses.events == [("RESUMED", {})]

    def test_resume_goes_on(self):
        pauses = Pauses(["resume"])
        assert pauses.controller.handle({})
        assert pauses.events == [("RESUMED", {})]

    def test_quit_stops_without_a_resumed_event(self):
        pauses = Pauses(["quit"])
        assert not pauses.controller.handle({})
        assert pauses.events == []

    def test_a_rest_nobody_answers_resumes_by_itself(self):
        pauses = Pauses([], rest_resume_after_s=0.5)
        assert pauses.controller.handle({}, rest="BLOCK 1 OF 2 COMPLETE — REST")
        assert pauses.clock.now() >= 0.5
        assert pauses.events == [("RESUMED", {})]

    def test_a_key_during_a_rest_cancels_its_timeout(self):
        # SPACE is resume; anything pressed before the deadline is somebody
        # at the rig, and the rest then waits for them.
        pauses = Pauses([], raw_keys=[["space"]], rest_resume_after_s=0.5)
        assert pauses.controller.handle({}, rest="BLOCK 1 OF 2 COMPLETE — REST")
        assert pauses.clock.now() < 0.5


class TestTheMenusOtherChoices:
    def test_a_stage_key_reaches_the_runners_stage_handler_and_the_menu_stays_up(self):
        pauses = Pauses(["promote_stage", "resume"])
        assert pauses.controller.handle({})
        assert pauses.commands == [Command.PROMOTE_STAGE]
        assert len(pauses.menus) == 2

    def test_a_procedure_with_no_tracker_wired_is_said_not_run(self, caplog):
        pauses = Pauses(["calibrate", "resume"])
        with caplog.at_level(logging.WARNING, logger="alhazen.session.runner"):
            assert pauses.controller.handle({})
        assert "calibrate requested while paused, but no eye tracker is wired" in caplog.text
        assert pauses.events == [("RESUMED", {})]

    def test_an_unknown_action_is_logged_not_ignored(self, caplog):
        pauses = Pauses(["frobnicate", "resume"])
        with caplog.at_level(logging.WARNING, logger="alhazen.session.runner"):
            assert pauses.controller.handle({})
        assert "unhandled pause action 'frobnicate'" in caplog.text
