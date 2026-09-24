"""Reading a session back: its own files, and the devices' files beside it.

The rule this layer lives by: **analysis reads a session's own configuration,
never a hand-typed copy of it.** A notebook that re-declares which sync line
carried which event is a notebook that will eventually be wrong about a
session it was not written for, and nothing will say so.

So everything here starts from a run directory: the config snapshot in it is
where the line map, the geometry and the seed come from.

Layering: analysis imports ``core``, ``config``, ``data`` and
``display.screen`` — the same geometry and configuration the experiment used —
and never ``session``, ``stimuli`` or ``task``. From ``devices`` it takes only
pure decisions the live path also makes (the viewpixx blink rule), never a
device, so online and offline cannot disagree about what a sample means. The
rest is enforced by the import contract, not by convention.
"""

from alhazen.analysis.io.session import RunData, load_run
from alhazen.analysis.photodiode import PhotodiodeReport as PhotodiodeReport
from alhazen.analysis.report import SessionReport as SessionReport
from alhazen.analysis.report import build_report as build_report
from alhazen.analysis.results import ResultsBundle
from alhazen.analysis.sync import AlignmentFit, align_run, fit_alignment

# `__all__` holds only the names docs/reference.md lists as public (a test in
# tests/unit/test_docs_snippets.py holds it to that). The `X as X` imports
# above are internal: they stay importable from here, for code that already
# imports them this way, but are not exported.
__all__ = [
    "AlignmentFit",
    "ResultsBundle",
    "RunData",
    "align_run",
    "fit_alignment",
    "load_run",
]
