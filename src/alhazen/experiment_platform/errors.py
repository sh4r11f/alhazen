"""The platform's errors. Each message is safe to show to the person using the platform:
it names a catalogue path, an experiment file inside a bundle or a request field, never a
path on the machine serving the platform."""

from __future__ import annotations

from alhazen.errors import AlhazenError


class PlatformError(AlhazenError):
    """Base class for every error the Experiment Platform raises deliberately."""


class BundleError(PlatformError):
    """A bundle (an experiment's files at one version) is malformed, unsafe or corrupt:
    a path escapes its root, a file is too large, an entry is duplicated, or the files do
    not hash to the bundle's recorded digest."""


class SourceError(PlatformError):
    """An experiment's source could not be read at the requested version (no such
    repository, ref or folder, or git failed)."""


class StoreError(PlatformError):
    """The catalogue store is unusable or a write would break one of its invariants
    (a version or bundle already exists with different content, a corrupt record)."""


class NotFound(PlatformError):
    """The record does not exist, or the principal may not know that it exists. Private
    records of another user are reported as missing, never as forbidden."""


class Forbidden(PlatformError):
    """The principal is known but may not perform this action (for example, writing as
    an anonymous visitor)."""


class Conflict(PlatformError):
    """The request contradicts the current state (for example, publishing a copy that
    has no revision yet)."""


class InvalidRequest(PlatformError):
    """A request field is missing, malformed or out of bounds."""


class AuthError(PlatformError):
    """Sign-in failed or the session is no longer valid. The message never says which of
    the user name or the password was wrong."""
