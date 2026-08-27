"""TrackerMessageSubscriber: task events as tracker (EDF) messages.

One bus subscriber, one message per event. The default text is the event's
own name lowercased, so a new event needs no wiring to show up in the EDF.

``message_map`` is the escape hatch that keeps legacy strings *out* of
alhazen: a ported experiment whose already-recorded EDFs are parsed by
existing analysis code (``TRIALID 7``, ``TRIAL_RESULT 3``, ...) supplies those
exact strings from its own package, and the framework never learns them.
"""

from __future__ import annotations

from collections.abc import Callable

from alhazen.core.events import Event
from alhazen.devices.eyetracker.protocol import EyeTracker

MessageMap = dict[str, "str | Callable[[Event], str]"]


class TrackerMessageSubscriber:
    """Bus subscriber writing one tracker message per event.

    Errors are not caught (invariant 6): a tracker that has stopped accepting
    messages means the EDF is losing its alignment marks, which must abort
    loudly rather than produce a session that only looks recorded.
    """

    def __init__(self, tracker: EyeTracker, message_map: MessageMap | None = None) -> None:
        self._tracker = tracker
        self._map: MessageMap = dict(message_map or {})

    def __call__(self, event: Event) -> None:
        entry = self._map.get(event.name)
        if entry is None:
            text = event.name.lower()
        elif callable(entry):
            # A callable sees the whole event, which is how a payload-dependent
            # message (a result code, a reward's manual flag) is produced.
            text = entry(event)
        else:
            text = entry
        self._tracker.send_message(text)
