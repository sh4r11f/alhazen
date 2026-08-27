"""On-disk bookkeeping: names, paths, hashes, and the participants registry.

Deliberately the bottom of the stack and deliberately ignorant: nothing here
knows what a trial is. The queryable session mirror (`ExperimentDatabase`)
reads a session's whole vocabulary and therefore lives in `session/`.
"""

from alhazen.data.manifest import verify_manifest, write_manifest
from alhazen.data.participants import ensure_participant, participants_path
from alhazen.data.paths import SessionPaths

__all__ = [
    "SessionPaths",
    "ensure_participant",
    "participants_path",
    "verify_manifest",
    "write_manifest",
]
