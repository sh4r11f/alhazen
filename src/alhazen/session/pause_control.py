"""PauseController: the pause screen, from the moment a pause is raised until
the experimenter resumes or quits.

Internal to the session package; SessionRunner builds one from its
constructor arguments and hands it every pause: a PAUSED trial, a reward
failure, a streak, a tracker with no calibration, a rest between blocks.

The decision it hides is *how a pause is resolved*: whether anybody can
answer it at all (no keyboard wired resumes at once, dashboard or not), the
keyboard loop and the dashboard loop that wait for an answer, a rest that
resumes by itself when nobody acts, the menu choices that are not resume or
quit (eye-tracker procedures, the manual reward, the stage keys) and the
heading the menu leads with after each of them, and the RESUMED event —
with the failed validation the session went on under, when there was one.

Interface: ``handle(record, fault=..., rest=...)``, True to go on and False
when the experimenter quit. Everything it does to the world it does through
what the runner handed it: the display, the clock, the keyboard, the
dashboard, and the runner's own dashboard publisher, session-event emitter
and stage-command handler.

Callers must not rely on how many times the menu is drawn or the dashboard
published while a pause is up, nor on the order keyboard and browser input
is read in within one poll; only on what a pause ends with (RESUMED, or
False for a quit) and on the menu a procedure leaves behind.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from alhazen.core.clock import Clock
from alhazen.core.commands import Command, CommandSource
from alhazen.dashboard.runtime import DashboardController
from alhazen.devices.eyetracker.procedures import ValidationResult
from alhazen.display.backend import DisplayBackend
from alhazen.session.eyetracker import EyeTrackerMonitor
from alhazen.session.pause import PauseMenu, build_pause_menu

# The runner's logger, not this module's: these lines were always the
# runner's, session.log names the logger on every line, and a reader (or a
# test) filtering on "alhazen.session.runner" must keep finding them.
log = logging.getLogger("alhazen.session.runner")


# Menu action -> the session command it issues, for the stage rows. A stage
# moved from the browser and one moved from the keyboard both arrive here as
# the same action name, so they go through exactly the same code.
PAUSE_STAGE_COMMANDS = {
    "promote_stage": Command.PROMOTE_STAGE,
    "demote_stage": Command.DEMOTE_STAGE,
    "hold_stage": Command.HOLD_STAGE,
}

# The pause-menu actions that are eye-tracker procedures, run through the
# session's EyeTrackerMonitor. Same names as the menu rows (session/pause.py)
# and the dashboard's buttons (dashboard/runtime.py _ALLOWED_COMMANDS).
PROCEDURE_ACTIONS = ("calibrate", "validate", "drift_correct")

# While a session is paused, how often the dashboard is republished so a
# tracker with a camera shows a live image. A pause is when the experimenter
# is looking at the subject's eye; the image is the point of the tab.
CAMERA_REFRESH_S = 1.0


def _validation_shortfall(validation: ValidationResult) -> str:
    """How a validation that did not pass fell short, as a heading says it:
    over the limit, or complete only in part, or measuring nothing at all."""
    worst = validation.max_error_deg
    if worst is None:
        return "VALIDATION MEASURED NO TARGET"
    limit = f"{validation.threshold_deg:g}°"
    if worst > validation.threshold_deg:
        line = f"VALIDATION ABOVE THE {limit} LIMIT — worst {worst:.2f}°"
    else:
        line = f"VALIDATION INCOMPLETE — worst {worst:.2f}° within the {limit} limit"
    if validation.n_missed:
        line += f", {validation.n_missed} target(s) missed"
    return line


class PauseController:
    """Resolves the session's pauses. See the module docstring for what it
    hides and what callers may rely on."""

    def __init__(
        self,
        *,
        display: DisplayBackend,
        clock: Clock,
        commands: CommandSource,
        wait: Callable[[float], None],
        on_pause: Callable[[PauseMenu], str] | None,
        rest_resume_after_s: float | None,
        eyetracker: EyeTrackerMonitor | None,
        dashboard: DashboardController | None,
        has_training: bool,
        manual_reward: Callable[[], None] | None,
        manual_reward_payload: dict[str, Any],
        publish: Callable[[str, str | None], object],
        last_message: Callable[[], str | None],
        emit_session_event: Callable[[str, dict[str, Any]], None],
        on_session_command: Callable[[Command], None],
    ) -> None:
        """``on_pause`` is the blocking keyboard strategy, None for an
        unattended run. ``rest_resume_after_s`` is how long a rest waits for
        somebody before it resumes by itself (SessionRunner validates it).
        ``publish(status, message)`` is the runner's dashboard publisher and
        ``last_message()`` the line it published last; ``emit_session_event``
        and ``on_session_command`` are the runner's own."""
        self._display = display
        self._clock = clock
        self._commands = commands
        self._wait = wait
        self._on_pause = on_pause
        self._rest_resume_after_s = rest_resume_after_s
        self._eyetracker = eyetracker
        self._dashboard = dashboard
        self._has_training = has_training
        self._manual_reward = manual_reward
        self._manual_reward_payload = manual_reward_payload
        self._publish = publish
        self._last_message = last_message
        self._emit_session_event = emit_session_event
        self._on_session_command = on_session_command

    def _pause_menu(
        self,
        fault: str | None = None,
        rest: str | None = None,
        resumes_in_s: float | None = None,
        warning: str | None = None,
    ) -> PauseMenu:
        """The menu for this session, built from what is actually wired.

        Built fresh at each pause rather than once at construction, because
        what is available can change during a session: a curriculum's stage
        keys are meaningless until a curriculum is running, and a fault
        heading belongs only to the pause it describes.
        """
        return build_pause_menu(
            has_tracker=self._eyetracker is not None,
            has_reward=self._manual_reward is not None,
            has_training=self._has_training,
            has_dashboard=self._dashboard is not None,
            fault=fault,
            rest=rest,
            resumes_in_s=resumes_in_s,
            warning=warning,
        )

    def _show_pause_menu(self, menu: PauseMenu) -> None:
        self._display.show_menu(menu.title, menu.render(), color=menu.color)

    def handle(
        self, record: dict[str, Any], *, fault: str | None = None, rest: str | None = None
    ) -> bool:
        """Resolve a PAUSED trial; returns False when the experimenter chose
        to quit. With no pause strategy wired (unattended runs), resume
        immediately — blocking forever with nobody at the keyboard would
        hang a simulated session. That check comes FIRST, before the
        dashboard: whether anyone is at the rig and whether a browser is
        serving are different questions, and answering the second one first
        hung every unattended run of a rig with the dashboard turned on.

        ``fault`` makes this an involuntary pause — a reward failure, a
        tracker with no calibration — and the screen leads with what went
        wrong rather than with the word PAUSED. ``rest`` is the opposite: a
        scheduled break, headed and coloured as one.

        The menu stays up across everything except resume and quit. Pressing
        the calibrate key used to calibrate and then resume in one press,
        which meant an experimenter who wanted to calibrate AND give a reward
        had to pause twice; and after a recalibration the natural thing to
        want is a look at the menu again, not the next trial.
        """
        notice = "Paused — browser controls are enabled."
        if record.get("pause_action") == "calibrate":
            # The in-trial calibrate key: a pause that arrives with the
            # procedure already chosen. Its verdict becomes the pause notice,
            # so the browser says "calibrated …" or "NOT calibrated …" rather
            # than only that the session is paused.
            notice = self._apply_pause_action("calibrate") or notice
        elif fault is not None:
            notice = f"{fault} — browser controls are enabled."
        elif rest is not None:
            notice = f"{rest.capitalize()} — resume when the subject is ready."
        # A rest can resume by itself when nobody acts in time: a simulation's
        # break, and only with someone who could act, since an unattended run
        # below resumes at once anyway. Never a fault: a pump or a
        # calibration that failed is exactly what somebody has to look at.
        resume_after_s = (
            self._rest_resume_after_s if rest is not None and self._on_pause is not None else None
        )
        menu = self._pause_menu(fault=fault, rest=rest, resumes_in_s=resume_after_s)
        if self._on_pause is None:
            # Nobody is going to answer. `on_pause` is wired only for a
            # rendering display with a keyboard behind it (session/builder.py),
            # so None means an unattended run — and that is true whether or
            # not the rig file turned the dashboard on. A dashboard is a
            # window onto the session, not a person at it; waiting for a
            # browser click that will never come hung every unattended run of
            # a rig with `dashboard.enabled`, and a scheduled block break made
            # that every simulated run of a multi-block experiment.
            #
            # The menu is still drawn and the skipped pause still logged, at
            # WARNING: a pause that did not pause is a real difference between
            # what the session was asked to do and what it did, and the run
            # that finds out is the dry run, not the one with a subject in it.
            self._show_pause_menu(menu)
            log.warning(
                "pause with nobody to answer it (no keyboard wired — unattended run): "
                "resuming immediately. %s",
                notice,
            )
            if self._dashboard is not None:
                # Left out, a dashboard open on a dry run would sit on the
                # last state it was told about while the session ran on.
                self._publish("running", f"{notice} Unattended — resumed.")
            return self._resumed()
        if self._dashboard is not None:
            return self._handle_dashboard_pause(
                menu, notice, fault=fault, rest=rest, resume_after_s=resume_after_s
            )
        deadline: float | None = None
        if resume_after_s is not None:
            deadline = self._clock.now() + resume_after_s
        while True:
            if deadline is not None:
                # `on_pause` blocks until a key is pressed, so a pause that can
                # time out polls the keyboard here instead, until the deadline.
                timed = self._next_menu_action_before(menu, deadline)
                if timed is None:
                    return self._resumed_by_itself(resume_after_s or 0.0)
                # Somebody is there after all. From here the rest waits for
                # them, and the screen stops promising otherwise.
                action = timed
                deadline = None
                menu = self._pause_menu(fault=fault, rest=rest)
            else:
                action = self._on_pause(menu)
            if action == "quit":
                return False
            if action == "resume":
                return self._resumed()
            self._apply_pause_action(action)
            # The menu is rebuilt after every procedure, not only after one
            # that failed. A procedure that failed becomes the heading of the
            # menu that comes back, on the screen the experimenter is actually
            # facing — and a procedure that then SUCCEEDS has to take that
            # heading back down again. Without this, a red VALIDATION FAILED
            # stays up after the recalibration that fixed it, and the pause's
            # own heading (a block break's REST) never comes back.
            if action in PROCEDURE_ACTIONS:
                menu = self._menu_after_procedure(action, fault=fault, rest=rest)

    def _next_menu_action_before(self, menu: PauseMenu, deadline: float) -> str | None:
        """Draw the menu and poll the keyboard until a key picks an action or
        the deadline passes. None when it passed.

        The keyboard half of a pause that can time out. ``on_pause`` blocks
        until a key is pressed, which is right for a pause a person has to
        resolve and wrong for one that resumes by itself, so the runner polls
        the same keys, through the menu's own key mapping
        (PauseMenu.action_for_key) — the one every pause loop uses.
        """
        self._show_pause_menu(menu)
        while self._clock.now() < deadline:
            for key in self._commands.poll_raw_keys():
                action = menu.action_for_key(key)
                if action is not None:
                    return action
            self._wait(0.01)
        return None

    def _resumed_by_itself(self, after_s: float) -> bool:
        """End a rest that nobody resolved in time: say so, then resume."""
        log.info(
            "the rest between blocks resumed by itself after %g s: nothing was pressed "
            "(simulation)",
            after_s,
        )
        if self._dashboard is not None:
            self._publish("running", f"Resumed by itself after {after_s:g} s (simulation).")
        return self._resumed()

    def _apply_pause_action(self, action: str) -> str | None:
        """One non-terminal menu choice; returns the line the dashboard shows
        for it, or None when the action published its own.

        Anything unrecognised is logged rather than ignored: a key that
        silently does nothing is the fault this menu exists to prevent.
        """
        if action in PROCEDURE_ACTIONS:
            return self._run_procedure(action)
        if action == "manual_reward":
            self._manual_reward_while_paused()
            return None  # publishes its own outcome, which is more specific
        if action in PAUSE_STAGE_COMMANDS:
            self._on_session_command(PAUSE_STAGE_COMMANDS[action])
            return f"{action.replace('_', ' ')} requested."
        log.warning("unhandled pause action %r", action)
        return f"unhandled action {action!r}."

    def _run_procedure(self, action: str) -> str:
        """One eye-tracker procedure from the pause menu, and its one-line
        outcome. The monitor keeps the results and shows them on the
        dashboard's Eye tracker tab; this line is what the pause notice says.
        """
        monitor = self._eyetracker
        if monitor is None:
            log.warning("%s requested while paused, but no eye tracker is wired", action)
            return "No eye tracker is wired."
        if action == "calibrate":
            calibration = monitor.calibrate()
            line = calibration.summary()
            validation = monitor.validation
            # The validation the calibration triggered, if the rig asks for
            # one: newer than the calibration, so not a stale result.
            if validation is not None and validation.t >= calibration.t:
                line += f" · {validation.summary()}"
            return line
        if action == "validate":
            return monitor.validate().summary()
        return monitor.drift_correct().summary()

    def _menu_after_procedure(
        self, action: str, *, fault: str | None, rest: str | None
    ) -> PauseMenu:
        """The pause menu to show once a procedure has run.

        A procedure that failed heads it as a fault, and a validation that did
        not pass heads it as a warning; either replaces the pause's own
        heading while it stands. After a procedure that succeeded, the pause's
        own heading comes back: a block break's REST, or the fault that
        opened the pause.
        """
        failed = self._procedure_fault(action)
        if failed is not None:
            return self._pause_menu(fault=failed)
        warned = self._procedure_warning(action)
        if warned is not None:
            return self._pause_menu(warning=warned)
        return self._pause_menu(fault=fault, rest=rest)

    def _procedure_fault(self, action: str) -> str | None:
        """The heading the pause screen leads with after a procedure that
        failed, or None.

        The verdict already goes to the dashboard's notice line and the log.
        Neither is the screen the experimenter is looking at while they stand
        at the rig, so a calibration the tracker did not take, or a drift
        correction it refused, leads the menu that comes back. A validation
        that did not pass is a warning instead (_procedure_warning).
        """
        monitor = self._eyetracker
        if monitor is None or action not in PROCEDURE_ACTIONS:
            return None
        calibration, drift = monitor.calibration, monitor.drift
        if action == "calibrate" and calibration is not None and calibration.ok is False:
            return f"CALIBRATION FAILED — {calibration.note or 'the tracker reports none'}"
        if action == "drift_correct" and drift is not None and not drift.applied:
            return f"DRIFT CORRECTION REFUSED — {drift.note or drift.summary()}"
        return None

    def _procedure_warning(self, action: str) -> str | None:
        """The heading after a validation that did not pass, or None.

        A warning, not a fault. Whether a calibration is good enough is the
        experimenter's call: a validation a little over its limit can be the
        best a subject manages that day, and a heading that said "recalibrate
        before resuming" kept an experimenter recalibrating a subject who was
        not going to do better. So the heading says how the validation fell
        short and offers both ways on. Resuming on it is recorded (_resumed).
        """
        monitor = self._eyetracker
        if monitor is None or action not in ("calibrate", "validate"):
            return None
        validation = monitor.validation
        if validation is None or validation.accepted or validation.aborted:
            return None
        return f"{_validation_shortfall(validation)} — SPACE resumes on it, C recalibrates"

    def _resumed(self) -> bool:
        payload: dict[str, Any] = {}
        monitor = self._eyetracker
        validation = monitor.validation if monitor is not None else None
        if validation is not None and not validation.accepted and not validation.aborted:
            # The session is going on under a validation that did not pass.
            # That is the experimenter's decision to make, and it is recorded
            # where an analysis and a later reader will look: in this event,
            # with the numbers, and in the log, in words. The VALIDATION event
            # and its per-target errors were written when it ran.
            payload["on_failed_validation"] = {
                "t": validation.t,
                "mean_error_deg": validation.mean_error_deg,
                "max_error_deg": validation.max_error_deg,
                "threshold_deg": validation.threshold_deg,
                "n_missed": validation.n_missed,
            }
            log.warning(
                "resumed on a validation that did not pass, as the experimenter chose: %s",
                validation.summary(),
            )
        self._emit_session_event("RESUMED", payload)
        return True

    def _handle_dashboard_pause(
        self,
        menu: PauseMenu,
        notice: str,
        *,
        fault: str | None = None,
        rest: str | None = None,
        resume_after_s: float | None = None,
    ) -> bool:
        """Drive the local browser controls only after a keyboard pause.

        The browser is server-enforced read-only before this state is
        published. Keyboard polling remains available so closing the browser
        can never strand an experimenter in the pause screen. `notice` is the
        line the browser shows as the pause begins; `fault` and `rest` are the
        pause's own heading, kept so that a procedure run from the browser can
        put it back after replacing it.
        """
        assert self._dashboard is not None
        dashboard = self._dashboard
        # Drain and discard whatever is already queued. A command accepted in
        # the milliseconds between the browser seeing "paused" and the runner
        # resuming would otherwise sit in the queue and fire at the NEXT
        # pause — a reward delivered, or a session quit, minutes after the
        # click that asked for it and with nobody expecting it.
        stale = dashboard.poll_commands()
        if stale:
            log.info("discarding %d command(s) queued before this pause", len(stale))
        # The menu goes on the subject display here too. It did not used to,
        # so turning the dashboard on silently removed the only thing the
        # person standing at the rig could see — and the rig is where a pause
        # is usually resolved, browser or no browser.
        self._show_pause_menu(menu)
        self._publish("paused", notice)
        # A tracker with a camera gets its image refreshed through the pause,
        # so the Eye tracker tab shows the eye as it is now, not as it was
        # when the pause began.
        live_camera = self._eyetracker is not None and self._eyetracker.has_camera
        monitor = self._eyetracker
        published_at = self._clock.now()
        # A rest that can resume by itself: the same deadline as the keyboard
        # path, cancelled by the first thing anybody does.
        deadline: float | None = None
        if resume_after_s is not None:
            deadline = self._clock.now() + resume_after_s
        while True:
            # What the page asks of the tracker between clicks: a camera frame
            # whenever one is due (session/eyetracker.py CAMERA_STREAM_S), and
            # any tracker setting it sent, applied and reported in the notice.
            # The refresh further down republishes the panel's words about once
            # a second.
            if monitor is not None:
                for setting_line in monitor.service_dashboard():
                    self._publish("paused", setting_line)
                    published_at = self._clock.now()
            actions = [command.name for command in dashboard.poll_commands()]
            actions += [
                action
                for key in self._commands.poll_raw_keys()
                if (action := menu.action_for_key(key)) is not None
            ]
            if deadline is not None:
                if actions:
                    # Somebody acted, at the rig or in the browser: the rest
                    # waits for them from here, and the menu drawn after their
                    # action no longer says it will resume by itself.
                    deadline = None
                    menu = self._pause_menu(fault=fault, rest=rest)
                elif self._clock.now() >= deadline:
                    return self._resumed_by_itself(resume_after_s or 0.0)
            for index, action in enumerate(actions):
                if action == "resume":
                    self._publish("running", "Resumed.")
                    return self._resumed()
                if action == "quit":
                    self._publish("stopping", "Quit requested.")
                    return False
                message = self._apply_pause_action(action)
                # Every non-terminal action redraws the menu, because
                # _apply_pause_action may have put a calibration screen over
                # it, and a menu that vanishes after one keypress looks like
                # a session that has crashed. A procedure that failed becomes
                # the menu's heading: the browser gets the verdict as its
                # notice, but the rig's own screen must say it too.
                if action in PROCEDURE_ACTIONS:
                    # Rebuilt after every procedure, so a heading that a
                    # failure put up comes back down when a later procedure
                    # succeeds, and the pause's own heading returns with it.
                    menu = self._menu_after_procedure(action, fault=fault, rest=rest)
                self._show_pause_menu(menu)
                if action in PROCEDURE_ACTIONS:
                    # A procedure runs for seconds to minutes, and the browser
                    # keeps accepting clicks until it learns of the
                    # "calibrating" status — about 0.2 s after the first
                    # click. A double-click on Calibrate, or Validate pressed
                    # right after it, would otherwise sit in the queue and run
                    # NOW, after the procedure, with nobody expecting a second
                    # walk. Discard it, and the rest of this batch, before the
                    # buttons come back; the keys a walk polls are already
                    # consumed by the walk itself.
                    dropped = actions[index + 1 :] + [c.name for c in dashboard.poll_commands()]
                    if dropped:
                        log.info(
                            "discarding %d command(s) queued while %s ran: %s",
                            len(dropped),
                            action,
                            ", ".join(dropped),
                        )
                if message is not None:
                    # Back to "paused" whatever the action published while it
                    # ran: the buttons are live again.
                    self._publish("paused", message)
                published_at = self._clock.now()
                if action in PROCEDURE_ACTIONS:
                    break
            if live_camera and self._clock.now() - published_at >= CAMERA_REFRESH_S:
                self._publish("paused", self._last_message())
                published_at = self._clock.now()
            self._wait(0.01)

    def _manual_reward_while_paused(self) -> None:
        if self._manual_reward is None:
            self._publish("paused", "No reward device is configured.")
            return
        try:
            self._manual_reward()
        except Exception as e:
            log.exception("manual reward failed while paused")
            self._emit_session_event("REWARD_FAILED", {"manual": True, "error": str(e)})
            self._publish("paused", "Manual reward failed — check the pump.")
            return
        self._emit_session_event("REWARD", {"manual": True, **self._manual_reward_payload})
        self._publish("paused", "Manual reward delivered.")
