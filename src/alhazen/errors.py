"""Exception vocabulary shared by every layer.

This module sits outside the layering contract (see pyproject's importlinter
config) precisely so that any layer can raise these without creating an
upward import. Every error is expected to carry an actionable message: what
failed and what the experimenter should do about it.
"""

from __future__ import annotations


class AlhazenError(Exception):
    """Base class for every error this package raises deliberately."""


class ConfigError(AlhazenError):
    """A configuration file or value is invalid. Raised at load/build time,
    naming the offending file and field — never deferred to mid-session."""


class DisplayError(AlhazenError):
    """The display backend could not open, flip, or close — including a
    missing optional renderer dependency (the message names the extra)."""


class DataError(AlhazenError):
    """A data-layer invariant would be violated — most importantly, refusing
    to overwrite an existing run's recorded data."""


class FrameQAError(AlhazenError):
    """Dropped-frame budget exceeded under the ``abort_run`` policy."""


class SessionError(AlhazenError):
    """The session runner could not be built or driven to completion."""


class TrackerError(AlhazenError):
    """An eye-tracker backend could not connect, configure, record, or hand
    back its recording — with an actionable message: what failed and what the
    experimenter should do (check a cable, fix an IP, switch backends)."""


class RewardError(AlhazenError):
    """Reward hardware could not be initialized or a delivery failed. Loud by
    design: a session that silently stops rewarding starves the subject and
    produces unusable behavior."""


class SyncError(AlhazenError):
    """A sync (TTL) line could not be opened or pulsed. Loud by design: sync
    pulses are the only thing aligning behavior to an external recording, and
    a missing pulse cannot be recovered after the session."""


class SpikeSourceError(AlhazenError):
    """A live spike stream could not be opened, read, or kept up with — a
    SpikeGLX host that is not running, a missing SDK (the message names where
    it ships), or a fetch thread that died. Loud by design: a live map fed by
    a silently dead stream would flatten out and read as "no receptive
    field", which is a scientific claim, not a connection status."""
