"""What this alhazen's command line can be asked to record, by name.

The experiment workspace launches each registered experiment with that
experiment's own alhazen, which may be older or newer than the workspace's.
Its interpreter probe (``alhazen.cli.workspace.INTERPRETER_PROBE``) imports
this module, when it exists, and records ``CAPABILITIES`` with the project, so
the workspace sends a flag only to an alhazen that understands it and says,
before a launch, what an older one will not record.

- ``experimenter``: ``--experimenter`` and ``--experimenter-id``, recorded
  in session.json and session.log.
- ``subject-demographics``: ``--age`` and ``--sex``, recorded in
  session.json and session.log (and participants.tsv for a new subject).
- ``duration-estimate``: ``--estimate-duration``, which prints how long the
  launch would take as JSON instead of running it (alhazen.cli.duration).
- ``training-mode``: ``--mode training`` with ``--ladder``/``--stage``, a
  stage of a training ladder run.py registers (``LADDERS``), filed under the
  stage's training root (alhazen.training.ladder); test and simulate take
  ``--stage`` to rehearse one.

Names are only ever added. A module without the file is an alhazen from
before any of them.
"""

from __future__ import annotations

CAPABILITIES: frozenset[str] = frozenset(
    {"duration-estimate", "experimenter", "subject-demographics", "training-mode"}
)
