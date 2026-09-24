"""Neural-signal arithmetic shared by the live path and the analysis path.

Everything in this package is pure numpy over plain arrays: no sockets, no
vendor SDK, no clock, no device. That is what lets the same threshold
detector and the same receptive-field accumulator serve two masters that the
layering keeps apart — ``alhazen.devices.spikes`` runs them live during a
session, and ``alhazen.analysis`` runs them again over the recorded files —
without either importing the other.

- :mod:`alhazen.neural.detect` — band-limited threshold crossing detection
  over a chunked int16 stream, with the carry-over state that keeps a spike
  on a chunk boundary from being lost.
- :mod:`alhazen.neural.rfmap` — the probe grid and the per-channel
  spike-count accumulator that turn (flash, spike) pairs into a rate map.
- :mod:`alhazen.neural.timebase` — placing a recording stream's sample
  indices on the session clock, live, with an honest statement of the
  jitter that mapping carries.
"""

from alhazen.neural.detect import SpikeDetector as SpikeDetector
from alhazen.neural.rfmap import ProbeGrid, RFAccumulator
from alhazen.neural.timebase import StreamTimebase as StreamTimebase

# `__all__` holds only the names docs/reference.md lists as public (a test in
# tests/unit/test_docs_snippets.py holds it to that). The `X as X` imports
# above are internal: they stay importable from here, for code that already
# imports them this way, but are not exported.
__all__ = ["ProbeGrid", "RFAccumulator"]
