"""The colours a session's panels come in, named once.

Everything alhazen draws *over* a session — instructions, a calibration guide,
the pause menu, a fault — is a bordered panel, and the border's colour is how
an experimenter reads the panel's meaning from across the room. Three colours,
three meanings, and nothing else uses them:

- **terminal green** — the session talking: a message box, whatever it says.
  Instructions to the subject, the eye tracker's calibration guide, a stage
  change — and the one-line notices, ``REWARD FAILURE — check the pump`` or
  ``Calibration FAILED`` included, because the box is the same box and the
  words carry the alarm. It reads as a terminal on purpose: monospace text
  on a near-black panel with a green outline.
- **pause orange** — the session is stopped and waiting for the experimenter
  (``session.pause``).
- **fault red** — the session is stopped because something broke
  (``session.pause``).

The pause and fault colours live with the pause menu that owns them; only the
green is here, because two layers share it: the display backend (the message
box) and the eye-tracker devices (the calibration guide), and the device layer
may import display but not the other way round.

Colours are PsychoPy ``rgb`` triples in −1..1, as every colour alhazen hands a
renderer is.
"""

from __future__ import annotations

# The outline and heading: a phosphor green, bright enough to be a line.
TERMINAL_GREEN = (-0.20, 0.90, 0.10)
# The body text: the same hue, lifted almost to white so a paragraph of it is
# comfortable to read. Bright green prose on black is the harsh edge a subject
# does not need.
TERMINAL_TEXT = (0.62, 0.96, 0.70)
# The panel behind the text: near-black with the faintest green cast, so the
# box reads as a screen of its own rather than a hole in the mid-grey session.
TERMINAL_FILL = (-0.92, -0.90, -0.92)
