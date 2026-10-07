"""What this alhazen's command line can be asked to record, by name.

The experiment workspace launches each registered experiment with that
experiment's own alhazen, which may be older or newer than the workspace's.
Its interpreter probe (``alhazen.cli.workspace.INTERPRETER_PROBE``) imports
this module, when it exists, and records ``CAPABILITIES`` with the project, so
the workspace sends a flag only to an alhazen that understands it and says,
before a launch, what an older one will not record.

- ``experimenter``: ``--experimenter`` and ``--experimenter-id``, recorded
  in session.json and session.log.

Names are only ever added. A module without the file is an alhazen from
before any of them.
"""

from __future__ import annotations

CAPABILITIES: frozenset[str] = frozenset({"experimenter"})
