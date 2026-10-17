"""Validation of the names and paths that cross the platform's trust boundaries.

Everything that becomes a file name in a store or a path inside a bundle passes through
here first: identifiers are a closed character set, and bundle paths are relative POSIX
paths that cannot leave their root on any operating system.
"""

from __future__ import annotations

import re
from typing import Any

from alhazen.experiment_platform.errors import BundleError, InvalidRequest

# Experiment ids, fork ids, publisher ids and bundle ids: lower-case, digits and . _ -,
# starting with a letter or digit. 80 characters is far above any real id and short
# enough to be a file name everywhere.
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
# Windows reserved device names cannot be file names even with an extension.
_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
MAX_PATH_CHARS = 400


def check_id(value: Any, kind: str) -> str:
    """``value`` if it is a well-formed identifier, else InvalidRequest naming ``kind``."""
    if not isinstance(value, str) or not _ID.match(value) or value in (".", "..") or ".." in value:
        raise InvalidRequest(
            f"{kind} {value!r} is not a valid identifier (a-z, 0-9, '.', '_', '-')"
        )
    if value.split(".")[0] in _RESERVED:
        raise InvalidRequest(f"{kind} {value!r} is a reserved name")
    return value


def is_id(value: Any) -> bool:
    try:
        check_id(value, "id")
    except InvalidRequest:
        return False
    return True


def safe_relative(path: Any) -> str:
    """A bundle path, checked: relative, forward slashes, no empty, ``.`` or ``..``
    segment, no drive letter, no control characters, not inside ``.git``. Returns it
    unchanged; raises BundleError naming the path otherwise."""
    if not isinstance(path, str) or not path:
        raise BundleError("A file path in the bundle is empty or not text")
    shown = path if len(path) <= 120 else path[:117] + "..."
    if len(path) > MAX_PATH_CHARS:
        raise BundleError(f"File path {shown!r} is too long")
    if "\\" in path or path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise BundleError(f"File path {shown!r} must be relative with forward slashes")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
        raise BundleError(f"File path {shown!r} contains a control character")
    parts = path.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise BundleError(f"File path {shown!r} has an empty, '.' or '..' segment")
        if part.endswith((" ", ".")) and part not in (".",):
            raise BundleError(f"File path {shown!r} has a segment ending in a space or dot")
        if part.split(".")[0].lower() in _RESERVED:
            raise BundleError(f"File path {shown!r} uses a reserved device name")
        if any(ch in part for ch in '<>:"|?*'):
            raise BundleError(f"File path {shown!r} contains a character no file system accepts")
    if parts[0] == ".git":
        raise BundleError(f"File path {shown!r} is inside .git")
    return path
