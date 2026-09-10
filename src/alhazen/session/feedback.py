"""The sound that goes with trial feedback.

``TrialFeedback`` (task/phases/simple.py) recolours the fixation point and
emits ``FEEDBACK`` on the flip that showed it. It does not beep, because a
phase touches nothing but the trial context — no hardware, no window — and a
sound card is hardware. So the beep is a bus subscriber, wired by the session
builder the way the sync lines and the tracker messages are: the phase says
what happened, the session decides what the rig does about it.

Two tones, generated rather than shipped: a high one for a good trial and a
low one for a bad one, the same pair the EyeLink calibration screen uses for
"done" and "error", so a subject who has heard one set has heard the other.
"""

from __future__ import annotations

import logging
from typing import Any

from alhazen.core.events import Event

log = logging.getLogger(__name__)

SUCCESS_TONE_HZ = 1200.0
FAILURE_TONE_HZ = 400.0
BEEP_SECONDS = 0.1


class FeedbackSounder:
    """Plays the feedback tone for every ``FEEDBACK`` event.

    On a psychopy display the two sounds are built once, up front, so the
    first trial's beep does not pay the cost of constructing one inside the
    frame loop. Audio that fails is logged once and then silent: a beep is a
    convenience, not data, and a rig with no working audio device still runs
    the session. ``played`` keeps what was asked for, which is what a
    simulated session and the tests can see.
    """

    def __init__(self, display: Any) -> None:
        self._display = display
        self._sounds: dict[float, Any] = {}
        self._audio_failed = False
        self.played: list[bool] = []
        if getattr(display, "kind", None) == "psychopy":
            for tone in (SUCCESS_TONE_HZ, FAILURE_TONE_HZ):
                self._sound(tone)

    def __call__(self, event: Event) -> None:
        if event.name != "FEEDBACK":
            return
        success = bool(event.payload.get("success"))
        self.played.append(success)
        if getattr(self._display, "kind", None) != "psychopy":
            return
        sound = self._sound(SUCCESS_TONE_HZ if success else FAILURE_TONE_HZ)
        if sound is None:
            return
        try:
            sound.play()
        except Exception as e:  # the audio backend's own exception types vary
            self._audio_failed = True
            log.warning("feedback tone could not be played; no further beeps this session: %s", e)

    def _sound(self, tone_hz: float) -> Any:
        if self._audio_failed:
            return None
        if tone_hz not in self._sounds:
            try:
                from psychopy import sound

                self._sounds[tone_hz] = sound.Sound(tone_hz, secs=BEEP_SECONDS)
            except Exception as e:
                self._audio_failed = True
                log.warning(
                    "feedback tones are off: psychopy's sound backend could not build one "
                    "(%s). The session runs without them.",
                    e,
                )
                return None
        return self._sounds[tone_hz]
