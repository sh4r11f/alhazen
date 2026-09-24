"""On-disk bookkeeping: names, paths, hashes, and the participants registry.

Deliberately the bottom of the stack and deliberately ignorant: nothing here
knows what a trial is. The queryable session mirror (`ExperimentDatabase`)
reads a session's whole vocabulary and therefore lives in `session/`.

`percents` (how a measured fraction is written beside the threshold it was
compared with) is here because display, session and analysis all need it,
and only the bottom of the stack is below all three. Like the rest of the
package, it knows nothing about what its numbers measure.
"""

from alhazen.data.manifest import add_to_manifest, verify_manifest, write_manifest
from alhazen.data.participants import ensure_participant as ensure_participant
from alhazen.data.participants import participants_path as participants_path
from alhazen.data.paths import SessionPaths as SessionPaths

# `__all__` holds only the names docs/reference.md lists as public (a test in
# tests/unit/test_docs_snippets.py holds it to that). The `X as X` imports
# above are internal: they stay importable from here, for code that already
# imports them this way, but are not exported.
__all__ = [
    "add_to_manifest",
    "verify_manifest",
    "write_manifest",
]
