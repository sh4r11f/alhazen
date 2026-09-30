"""PauseController: the pause screen, from the moment a pause is raised until
the experimenter resumes or quits.

Internal to the session package; SessionRunner builds one from its
constructor arguments and hands it every pause: a PAUSED trial, a reward
failure, a streak, a tracker with no calibration, a rest between blocks.

The decision it hides is *how a pause is resolved*: whether anybody can
answer it at all (no keyboard wired resumes at once, live monitor or not), the
keyboard loop and the live monitor loop that wait for an answer, a rest that
resumes by itself when nobody acts, the menu choices that are not resume or
quit (eye-tracker procedures, the manual reward, the stage keys) and the
heading the menu leads with after each of them, a resume that owes a
validation of the eye tracker first (a block break under
``validate_after_break``), and the RESUMED event — with the failed
validation the session went on under, when there was one.

Interface: ``handle(record, fault=..., rest=..., validate_before_resuming=...)``,
True to go on and False when the experimenter quit. Everything it does to the
world it does through what the runner handed it: the display, the clock, the
keyboard, the live monitor, and the runner's own live monitor publisher,
session-event emitter and stage-command handler.

A resume that owes a validation, as it runs::

    SPACE / Resume ──▶ validation owed? ──no──▶ RESUMED
                             │ yes
                             ▼
                  the V key's validation (_run_procedure)
                  ├─ passed ─────────────────▶ RESUMED
                  ├─ did not pass ──▶ amber heading; SPACE ──▶ RESUMED
                  │                   (on_failed_validation)
                  └─ ESC ───────────▶ the pause's own menu; SPACE validates again

Callers must not rely on how many times the menu is drawn or the live monitor
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
from alhazen.devices.eyetracker.procedures import Advance, ValidationResult
from alhazen.display.backend import DisplayBackend
from alhazen.live_monitor.runtime import LiveMonitorController
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
# and the live monitor's buttons (live monitor/runtime.py _ALLOWED_COMMANDS).
PROCEDURE_ACTIONS = ("calibrate", "validate", "drift_correct")

# While a session is paused, how often the live monitor is republished so a
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


class _ValidationDue:
    """A pause whose resume must see the eye tracker's calibration validated
    first — a block break under ``BlockConfig.validate_after_break`` — and
    whether that validation is still owed.

    Owed until the monitor holds a validation measured during this pause
    that the experimenter did not abandon: the one a resume runs, or one run
    from the menu (V, or the validation that follows a calibration),
    whatever its verdict. A validation that did not pass is the
    experimenter's call — its heading says SPACE resumes on it — and resuming
    on it is recorded (``PauseController._resumed``). A calibration that
    takes clears the monitor's validation (``EyeTrackerMonitor.calibrate``),
    so the pause owes a new one, measured against the new model.

    Told apart by identity, not by time: the result the monitor held when the
    pause began is the one that does not count, whatever clock stamped it.
    """

    def __init__(self, monitor: EyeTrackerMonitor) -> None:
        self._monitor = monitor
        self._held_before = monitor.validation

    @property
    def owed(self) -> bool:
        latest = self._monitor.validation
        return latest is None or latest is self._held_before or latest.aborted

    @property
    def passed(self) -> bool:
        """Whether the latest validation passed; asked right after one ran."""
        latest = self._monitor.validation
        return latest is not None and latest.accepted


def _owes_validation(due: _ValidationDue | None) -> bool:
    """Whether a pause's resume still owes a validation: False for every
    pause that asked for none."""
    return due is not None and due.owed


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
        live_monitor: LiveMonitorController | None,
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
        ``publish(status, message)`` is the runner's live monitor publisher and
        ``last_message()`` the line it published last; ``emit_session_event``
        and ``on_session_command`` are the runner's own."""
        self._display = display
        self._clock = clock
        self._commands = commands
        self._wait = wait
        self._on_pause = on_pause
        self._rest_resume_after_s = rest_resume_after_s
        self._eyetracker = eyetracker
        self._live_monitor = live_monitor
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
        due: _ValidationDue | None = None,
    ) -> PauseMenu:
        """The menu for this session, built from what is actually wired.

        Built fresh at each pause rather than once at construction, because
        what is available can change during a session: a curriculum's stage
        keys are meaningless until a curriculum is running, and a fault
        heading belongs only to the pause it describes. ``due`` is the pause's
        owed validation, if it has one: while it is owed, the SPACE row says
        that resuming validates first.
        """
        return build_pause_menu(
            has_tracker=self._eyetracker is not None,
            has_reward=self._manual_reward is not None,
            has_training=self._has_training,
            has_live_monitor=self._live_monitor is not None,
            fault=fault,
            rest=rest,
            resumes_in_s=resumes_in_s,
            warning=warning,
            resume_validates=_owes_validation(due),
        )

    def _show_pause_menu(self, menu: PauseMenu) -> None:
        self._display.show_menu(menu.title, menu.render(), color=menu.color)

    def handle(
        self,
        record: dict[str, Any],
        *,
        fault: str | None = None,
        rest: str | None = None,
        validate_before_resuming: bool = False,
    ) -> bool:
        """Resolve a PAUSED trial; returns False when the experimenter chose
        to quit. With no pause strategy wired (unattended runs), resume
        immediately — blocking forever with nobody at the keyboard would
        hang a simulated session. That check comes FIRST, before the
        live_monitor: whether anyone is at the rig and whether a browser is
        serving are different questions, and answering the second one first
        hung every unattended run of a rig with the live monitor turned on.

        ``fault`` makes this an involuntary pause — a reward failure, a
        tracker with no calibration — and the screen leads with what went
        wrong rather than with the word PAUSED. ``rest`` is the opposite: a
        scheduled break, headed and coloured as one.

        ``validate_before_resuming`` makes resuming validate the eye
        tracker's calibration first: a block break under
        ``BlockConfig.validate_after_break``. It is the V key's validation,
        run when the experimenter resumes (or a simulation's rest resumes by
        itself). One that passes resumes; one that does not brings the menu
        back headed by how it fell short, and SPACE then resumes on it; one
        abandoned with ESC brings the pause's own menu back, and the next
        SPACE validates again. A validation already run from this pause's
        menu counts (``_ValidationDue``). Nothing can answer a failed one in
        an unattended run or when a rest ended by itself, so the session then
        resumes on it at once, and the log says so.

        The menu stays up across everything except resume and quit. Pressing
        the calibrate key used to calibrate and then resume in one press,
        which meant an experimenter who wanted to calibrate AND give a reward
        had to pause twice; and after a recalibration the natural thing to
        want is a look at the menu again, not the next trial.
        """
        due: _ValidationDue | None = None
        if validate_before_resuming:
            if self._eyetracker is None:
                # SessionRunner refuses to build such a session, so reaching
                # this is a caller's bug. Resuming without the validation
                # would be the silent skip the option exists to end.
                raise ValueError(
                    "validate_before_resuming needs an eye tracker to validate, and none is wired"
                )
            due = _ValidationDue(self._eyetracker)
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
        if _owes_validation(due):
            # Said where the browser's reader sees it too: what follows
            # Resume is a walk of targets, not the next trial.
            notice += " Resuming validates the eye tracker's calibration first."
        # A rest can resume by itself when nobody acts in time: a simulation's
        # break, and only with someone who could act, since an unattended run
        # below resumes at once anyway. Never a fault: a pump or a
        # calibration that failed is exactly what somebody has to look at.
        resume_after_s = (
            self._rest_resume_after_s if rest is not None and self._on_pause is not None else None
        )
        menu = self._pause_menu(fault=fault, rest=rest, resumes_in_s=resume_after_s, due=due)
        if self._on_pause is None:
            # Nobody is going to answer. `on_pause` is wired only for a
            # rendering display with a keyboard behind it (session/builder.py),
            # so None means an unattended run — and that is true whether or
            # not the rig file turned the live monitor on. A live monitor is a
            # window onto the session, not a person at it; waiting for a
            # browser click that will never come hung every unattended run of
            # a rig with `live_monitor.enabled`, and a scheduled block break made
            # that every simulated run of a multi-block experiment.
            #
            # The menu is still drawn and the skipped pause still logged, at
            # WARNING: a pause that did not pause is a real difference between
            # what the session was asked to do and what it did, and the run
            # that finds out is the dry run, not the one with a subject in it.
            self._show_pause_menu(menu)
            owed = _owes_validation(due)
            log.warning(
                "pause with nobody to answer it (no keyboard wired — unattended run): "
                "resuming immediately%s. %s",
                ", once the calibration it asks for is validated" if owed else "",
                notice,
            )
            if owed:
                # Run all the same, and on the record like any other: an
                # unattended run is how a design's validation is rehearsed.
                # Whatever it finds, nobody is here to decide on it, so the
                # session resumes on it; _resumed says so in the log.
                self._validate_before_resuming()
            if self._live_monitor is not None:
                # Left out, a live monitor open on a dry run would sit on the
                # last state it was told about while the session ran on.
                self._publish("running", f"{notice} Unattended — resumed.")
            return self._resumed(answered=False)
        if self._live_monitor is not None:
            return self._handle_live_monitor_pause(
                menu, notice, fault=fault, rest=rest, resume_after_s=resume_after_s, due=due
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
                    return self._resumed_by_itself(resume_after_s or 0.0, due)
                # Somebody is there after all. From here the rest waits for
                # them, and the screen stops promising otherwise.
                action = timed
                deadline = None
                menu = self._pause_menu(fault=fault, rest=rest, due=due)
            else:
                action = self._on_pause(menu)
            if action == "quit":
                return False
            if action == "resume":
                if not _owes_validation(due):
                    return self._resumed()
                assert due is not None  # owed implies a pause that asked for one
                self._validate_before_resuming()
                if due.passed:
                    return self._resumed()
                # Did not pass, or abandoned with ESC: the menu comes back as
                # it does after V — headed by how the validation fell short
                # (SPACE then resumes on it), or with the pause's own heading
                # after an ESC, when the next SPACE validates again.
                menu = self._menu_after_procedure("validate", fault=fault, rest=rest, due=due)
                continue
            self._apply_pause_action(action)
            # The menu is rebuilt after every procedure, not only after one
            # that failed. A procedure that failed becomes the heading of the
            # menu that comes back, on the screen the experimenter is actually
            # facing — and a procedure that then SUCCEEDS has to take that
            # heading back down again. Without this, a red VALIDATION FAILED
            # stays up after the recalibration that fixed it, and the pause's
            # own heading (a block break's REST) never comes back.
            if action in PROCEDURE_ACTIONS:
                menu = self._menu_after_procedure(action, fault=fault, rest=rest, due=due)

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

    def _resumed_by_itself(self, after_s: float, due: _ValidationDue | None) -> bool:
        """End a rest that nobody resolved in time: say so, run the validation
        it owes if it owes one, then resume.

        Nobody answered the rest, so nobody is there to answer a validation
        that fails either. It goes on the record like any other, and the
        session resumes on it at once (``_resumed`` says so in the log)
        rather than waiting under a heading nobody will read, which in a
        simulation would be a session that never ends.
        """
        log.info(
            "the rest between blocks resumed by itself after %g s: nothing was pressed "
            "(simulation)",
            after_s,
        )
        outcome = ""
        if _owes_validation(due):
            outcome = f" {self._validate_before_resuming()}."
        if self._live_monitor is not None:
            self._publish(
                "running", f"Resumed by itself after {after_s:g} s (simulation).{outcome}"
            )
        return self._resumed(answered=False)

    def _validate_before_resuming(self) -> str:
        """The validation a resume owes: the pause menu's V, run through the
        same procedure (``_run_procedure``), so it goes where every
        validation goes — the VALIDATION event, the live monitor's
        Validation panel, the log. Returns its one-line outcome.

        It advances from target to target by itself ("auto") whenever nobody
        may be at the keyboard to accept one: an unattended run, or a
        simulation, whose rest resumes by itself. A walk there that waited
        for SPACE at every target (the rig's setting, and the default) would
        never end. Otherwise it advances as the rig says, exactly as V does.
        """
        log.info("validating the eye tracker's calibration before resuming (validate_after_break)")
        unattended = self._on_pause is None or self._rest_resume_after_s is not None
        return self._run_procedure("validate", advance="auto" if unattended else None)

    def _apply_pause_action(self, action: str) -> str | None:
        """One non-terminal menu choice; returns the line the live monitor shows
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

    def _run_procedure(self, action: str, advance: Advance | None = None) -> str:
        """One eye-tracker procedure from the pause menu, and its one-line
        outcome. The monitor keeps the results and shows them on the
        live monitor's Eye tracker tab; this line is what the pause notice says.

        ``advance`` is for the validation a resume runs by itself
        (``_validate_before_resuming``); a key pressed on the menu leaves it
        None, and the walk advances as the rig says.
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
            # The keyword only when there is one to give, so V's own call is
            # exactly the one it always was — which a stand-in monitor that
            # predates the keyword (a test's) still answers.
            if advance is None:
                return monitor.validate().summary()
            return monitor.validate(advance=advance).summary()
        return monitor.drift_correct().summary()

    def _menu_after_procedure(
        self,
        action: str,
        *,
        fault: str | None,
        rest: str | None,
        due: _ValidationDue | None = None,
    ) -> PauseMenu:
        """The pause menu to show once a procedure has run.

        A procedure that failed heads it as a fault, and a validation that did
        not pass heads it as a warning; either replaces the pause's own
        heading while it stands. After a procedure that succeeded, the pause's
        own heading comes back: a block break's REST, or the fault that
        opened the pause. ``due`` is the pause's owed validation, which the
        SPACE row describes while it is still owed.
        """
        failed = self._procedure_fault(action)
        if failed is not None:
            return self._pause_menu(fault=failed, due=due)
        warned = self._procedure_warning(action)
        if warned is not None:
            return self._pause_menu(warning=warned, due=due)
        return self._pause_menu(fault=fault, rest=rest, due=due)

    def _procedure_fault(self, action: str) -> str | None:
        """The heading the pause screen leads with after a procedure that
        failed, or None.

        The verdict already goes to the live monitor's notice line and the log.
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

    def _resumed(self, *, answered: bool = True) -> bool:
        """Emit RESUMED and go on. ``answered`` says whether somebody resumed
        it (a key, the browser) rather than the session with nobody there —
        an unattended run, or a rest that ended by itself — which is what the
        log line about a failed validation has to say truthfully."""
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
            # Nobody chose it when nobody was there: an unattended run, or a
            # rest that resumed by itself, goes on under a failed validation
            # because nothing can answer it, and the log must not claim a
            # decision nobody made.
            chosen = (
                "as the experimenter chose"
                if answered
                else "with nobody there to decide (an unattended run, or a rest that ended "
                "by itself)"
            )
            log.warning(
                "resumed on a validation that did not pass, %s: %s", chosen, validation.summary()
            )
        self._emit_session_event("RESUMED", payload)
        return True

    def _handle_live_monitor_pause(
        self,
        menu: PauseMenu,
        notice: str,
        *,
        fault: str | None = None,
        rest: str | None = None,
        resume_after_s: float | None = None,
        due: _ValidationDue | None = None,
    ) -> bool:
        """Drive the local browser controls only after a keyboard pause.

        The browser is server-enforced read-only before this state is
        published. Keyboard polling remains available so closing the browser
        can never strand an experimenter in the pause screen. `notice` is the
        line the browser shows as the pause begins; `fault` and `rest` are the
        pause's own heading, kept so that a procedure run from the browser can
        put it back after replacing it. `due` is the validation the pause's
        resume owes, if any (see `handle`).
        """
        assert self._live_monitor is not None
        live_monitor = self._live_monitor
        # Drain and discard whatever is already queued. A command accepted in
        # the milliseconds between the browser seeing "paused" and the runner
        # resuming would otherwise sit in the queue and fire at the NEXT
        # pause — a reward delivered, or a session quit, minutes after the
        # click that asked for it and with nobody expecting it.
        stale = live_monitor.poll_commands()
        if stale:
            log.info("discarding %d command(s) queued before this pause", len(stale))
        # The menu goes on the subject display here too. It did not used to,
        # so turning the live monitor on silently removed the only thing the
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
                for setting_line in monitor.service_live_monitor():
                    self._publish("paused", setting_line)
                    published_at = self._clock.now()
            actions = [command.name for command in live_monitor.poll_commands()]
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
                    menu = self._pause_menu(fault=fault, rest=rest, due=due)
                elif self._clock.now() >= deadline:
                    return self._resumed_by_itself(resume_after_s or 0.0, due)
            for index, action in enumerate(actions):
                if action == "resume" and not _owes_validation(due):
                    self._publish("running", "Resumed.")
                    return self._resumed()
                if action == "quit":
                    self._publish("stopping", "Quit requested.")
                    return False
                # The procedure this action runs, if any: its own name, or,
                # for a resume that owes a validation, the V key's validation,
                # run first and handled below exactly as V's would be.
                procedure = action
                message: str | None
                if action == "resume":
                    assert due is not None  # owed implies a pause that asked for one
                    message = self._validate_before_resuming()
                    if due.passed:
                        self._publish("running", f"{message} — resumed.")
                        return self._resumed()
                    procedure = "validate"
                else:
                    message = self._apply_pause_action(action)
                # Every non-terminal action redraws the menu, because
                # _apply_pause_action may have put a calibration screen over
                # it, and a menu that vanishes after one keypress looks like
                # a session that has crashed. A procedure that failed becomes
                # the menu's heading: the browser gets the verdict as its
                # notice, but the rig's own screen must say it too.
                if procedure in PROCEDURE_ACTIONS:
                    # Rebuilt after every procedure, so a heading that a
                    # failure put up comes back down when a later procedure
                    # succeeds, and the pause's own heading returns with it.
                    menu = self._menu_after_procedure(procedure, fault=fault, rest=rest, due=due)
                self._show_pause_menu(menu)
                if procedure in PROCEDURE_ACTIONS:
                    # A procedure runs for seconds to minutes, and the browser
                    # keeps accepting clicks until it learns of the
                    # "calibrating" status — about 0.2 s after the first
                    # click. A double-click on Calibrate, or Validate pressed
                    # right after it, would otherwise sit in the queue and run
                    # NOW, after the procedure, with nobody expecting a second
                    # walk. Discard it, and the rest of this batch, before the
                    # buttons come back; the keys a walk polls are already
                    # consumed by the walk itself.
                    dropped = actions[index + 1 :] + [c.name for c in live_monitor.poll_commands()]
                    # A second Resume in a double-click lands here too, when
                    # the validation the first one ran did not pass: it must
                    # not resume on that result before anyone has read it.
                    if dropped:
                        log.info(
                            "discarding %d command(s) queued while %s ran: %s",
                            len(dropped),
                            procedure,
                            ", ".join(dropped),
                        )
                if message is not None:
                    # Back to "paused" whatever the action published while it
                    # ran: the buttons are live again.
                    self._publish("paused", message)
                published_at = self._clock.now()
                if procedure in PROCEDURE_ACTIONS:
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
