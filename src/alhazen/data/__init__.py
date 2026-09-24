"""On-disk bookkeeping: names, paths, hashes, and the participants registry.

Deliberately the bottom of the stack and deliberately ignorant: nothing here
knows what a trial is. The queryable session mirror (`ExperimentDatabase`)
reads a session's whole vocabulary and therefore lives in `session/`.

`percents` (how a measured fraction is written beside the threshold it was
compared with) is here because display, session and analysis all need it,
and only the bottom of the stack is below all three. Like the rest of the
package, it knows nothing about what its numbers measure.

`atomic` (replacing a file whole, so a crash leaves the old file or the new
one) is here for the same reason: the registry below and a subject's
training state in ``training/`` are both records rewritten whole.
"""

from alhazen.data.manifest import add_to_manifest, verify_manifest, write_manifest
from alhazen.data.participants import ensure_participant, participants_path
from alhazen.data.paths import SessionPaths

__all__ = [
    "SessionPaths",
    "add_to_manifest",
    "ensure_participant",
    "participants_path",
    "verify_manifest",
    "write_manifest",
]
