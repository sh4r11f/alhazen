"""The upload protocol's shared identities, stdlib only (rig and hub use it).

Secret it hides: the exact canonical forms both sides must agree on —

* ``manifest_sha256``: SHA-256 of the UTF-8 JSON (``sort_keys=True``,
  separators ``(",", ":")``, ``ensure_ascii=False``) of
  ``{experiment_id, version_id, files, metadata}``, with ``files`` the
  ``{path, size, sha256}`` records sorted by path and ``metadata`` always
  holding the four keys ``subject_code, mode, rig_alias, started_at`` (a
  missing value is ``null``). ``client_session_id`` and consent are not
  hashed (docs/hub/api-contract.md, "Receipt identity").
* what a receipt must say before a client may call an upload complete
  (:func:`receipt_problems`).

Importing this module imports nothing from the hub service, so the rig can
use it on a plain install.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

METADATA_KEYS = ("subject_code", "mode", "rig_alias", "started_at")
# The hub's per-field limits (characters); a value over its limit, or with a
# control character, is refused by the hub, so a client sends null instead.
METADATA_LIMITS = {"subject_code": 64, "mode": 32, "rig_alias": 64, "started_at": 40}
COMMITTED = "committed"


def canonical_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """All four metadata keys, in the hub's form: missing values are None."""
    source = metadata or {}
    return {key: source.get(key) for key in METADATA_KEYS}


def usable_metadata(values: Mapping[str, Any]) -> dict[str, str | None]:
    """``values`` reduced to what the hub accepts: strings within the limits
    and free of control characters, else None. The original values stay in
    the uploaded files; this is only the searchable summary."""
    out: dict[str, str | None] = {}
    for key, limit in METADATA_LIMITS.items():
        value = values.get(key)
        ok = isinstance(value, str) and len(value) <= limit and all(ord(c) >= 32 for c in value)
        out[key] = value if ok else None
    return out


def canonical_files(files: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """``{path, size, sha256}`` records sorted by path."""
    rows = [{"path": f["path"], "size": f["size"], "sha256": f["sha256"]} for f in files]
    return sorted(rows, key=lambda row: str(row["path"]))


def manifest_sha256(
    experiment_id: str,
    version_id: str,
    files: Iterable[Mapping[str, Any]],
    metadata: Mapping[str, Any] | None,
) -> str:
    """The upload's canonical identity (module docstring)."""
    canonical = {
        "experiment_id": experiment_id,
        "version_id": version_id,
        "files": canonical_files(files),
        "metadata": canonical_metadata(metadata),
    }
    data = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def receipt_problems(
    receipt: Any,
    *,
    session_id: str,
    client_session_id: str,
    experiment_id: str,
    version_id: str,
    files: Iterable[Mapping[str, Any]],
    metadata: Mapping[str, Any] | None,
) -> list[str]:
    """Why ``receipt`` does not certify exactly the upload the client bound;
    empty when it does. Checks the status, the session and client-session
    ids, the release, the file count, the byte total and ``manifest_sha256``."""
    if not isinstance(receipt, Mapping):
        return ["the receipt is not an object"]
    rows = canonical_files(files)
    expected: dict[str, Any] = {
        "status": COMMITTED,
        "id": session_id,
        "client_session_id": client_session_id,
        "experiment_id": experiment_id,
        "version_id": version_id,
        "file_count": len(rows),
        "total_bytes": sum(int(row["size"]) for row in rows),
        "manifest_sha256": manifest_sha256(experiment_id, version_id, rows, metadata),
    }
    problems = []
    for key, want in expected.items():
        got = receipt.get(key)
        if isinstance(want, int) and (isinstance(got, bool) or not isinstance(got, int)):
            problems.append(f"{key}: expected {want}, the receipt says {got!r}")
        elif got != want:
            problems.append(f"{key}: expected {want!r}, the receipt says {got!r}")
    return problems
