"""The display seam: everything above talks to this
protocol, never to a renderer directly. PsychoPy implements it first; a
leaner GL or native presentation core can slot in later without touching
experiment code.

Timestamping contract: ``flip()`` blocks until the buffer swap and returns
nothing — the *engine* stamps the session clock immediately after, so there
is exactly one clock and one stamping site (photodiode reconciliation, phase
2, measures what that stamp misses)."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DisplayBackend(Protocol):
    # Short backend name ("psychopy", "simulated", ...) — used for logging
    # and for the rare seam where stimulus construction must branch.
    kind: str

    # The native window object stimuli draw into (a psychopy Window for the
    # psychopy backend; a recording stub for simulated/fake ones). Typed Any
    # because its concrete type is exactly what this protocol hides.
    window: Any

    def open(self) -> None: ...

    def close(self) -> None: ...

    def flip(self, clear: bool = True) -> None:
        """Swap buffers; block until the swap. The frame's photons change
        here and nowhere else."""
        ...

    def measure_refresh_rate(self, n_flips: int) -> float:
        """Measured Hz over n warm-up flips. Frame math uses this, never the
        nominal rate (config.resolve_refresh checks they agree)."""
        ...

    def show_message(self, text: str, *, reflow: bool = True) -> None:
        """Present a short operator/subject message and flip. Backends with
        no visible surface log it instead.

        ``reflow`` (the default) treats the text as prose: a single newline
        inside a paragraph becomes a space and a blank line separates
        paragraphs, with indented lines and list items keeping their breaks
        (``alhazen.display.text.reflow`` has the exact rule). Without it,
        hard-wrapped instructions are wrapped a second time at the display's
        own measure. ``reflow=False`` keeps every line break exactly as given,
        for text laid out line by line. Every built-in backend accepts the
        argument, and one with no visible surface records it, so a caller's
        choice is never lost on the way to the screen.

        A backend written before ``reflow`` existed takes the text alone and
        keeps working: framework code never passes ``reflow=`` to a backend
        it did not build (a message whose lines must stay apart is written as
        paragraphs instead), and the deprecated ``pause_menu`` seam checks the
        signature before it does.
        """
        ...

    def show_menu(self, title: str, body: str, *, color: tuple[float, float, float]) -> None:
        """Present a modal menu — a heading over a block of key/action rows —
        and flip.

        Separate from ``show_message`` because it is a different thing on
        screen and must look like one: a message is the session talking to the
        subject, a menu is the session stopped and waiting for the
        experimenter. ``color`` is what carries that distinction across a
        room, so it is required rather than defaulted.

        The body's key column is aligned with spaces, so a backend that draws
        it must use a monospace face or the alignment is lost. Backends with
        no visible surface record it instead.
        """
        ...

    def set_gamma(self, gamma: float) -> None:
        """Apply a measured gamma correction (`alhazen calibrate gamma`).

        Without one, a stimulus asking for 50% contrast gets whatever 50% of
        the panel's raw code values happens to look like — which is not 50%
        of its luminance. Backends with no visible surface record the value
        instead, so a simulated session still says what it would have used.
        """
        ...
