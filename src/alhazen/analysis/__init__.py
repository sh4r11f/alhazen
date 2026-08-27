"""Reading a session back: its own files, and the devices' files beside it.

The rule this layer lives by: **analysis reads a session's own configuration,
never a hand-typed copy of it.** A notebook that re-declares which sync line
carried which event is a notebook that will eventually be wrong about a
session it was not written for, and nothing will say so.

So everything here starts from a run directory: the config snapshot in it is
where the line map, the geometry and the seed come from.

Layering: analysis imports ``core``, ``config``, ``data`` and
``display.screen`` — the same geometry and configuration the experiment used —
and never ``session``, ``devices``, ``stimuli`` or ``task``. That is enforced
by the import contract, not by convention.
"""

from alhazen.analysis.io.session import RunData, load_run
from alhazen.analysis.photodiode import PhotodiodeReport
from alhazen.analysis.report import SessionReport, build_report
from alhazen.analysis.results import ResultsBundle
from alhazen.analysis.sync import AlignmentFit, align_run, fit_alignment

__all__ = [
    "AlignmentFit",
    "PhotodiodeReport",
    "ResultsBundle",
    "RunData",
    "SessionReport",
    "align_run",
    "build_report",
    "fit_alignment",
    "load_run",
]
