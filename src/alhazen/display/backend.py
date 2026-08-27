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

    def show_message(self, text: str) -> None:
        """Present a short operator/subject message and flip. Backends with
        no visible surface log it instead."""
        ...

    def set_gamma(self, gamma: float) -> None:
        """Apply a measured gamma correction (`alhazen calibrate gamma`).

        Without one, a stimulus asking for 50% contrast gets whatever 50% of
        the panel's raw code values happens to look like — which is not 50%
        of its luminance. Backends with no visible surface record the value
        instead, so a simulated session still says what it would have used.
        """
        ...
