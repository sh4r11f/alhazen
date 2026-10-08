"""Upload receipts: what was copied where, kept with the session.

A receipt is written for every session an upload attempted, verified or
not, to ``<data folder>/uploads/<run id>/<UTC time>.json``: beside the
session, in its own data folder, but never inside the session folder
itself — that folder is append-only by its manifest (`verify_manifest`
reports any unlisted file), and a receipt is not part of what the session
recorded. ``uploads/`` is not a run folder by name, so `find_runs` never
lists it. Receipts are only ever added; the newest is the session's state.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RECEIPTS_DIR = "uploads"
RECEIPT_SCHEMA = 1
# What a receipt's status means, from best to worst. Only "verified" says
# every file is at the destination with the same content.
STATUSES = ("verified", "conflict", "incomplete", "cancelled", "failed")


def _folder(root: Path, run_id: str) -> Path:
    # run_id was checked by the caller (workspace_data._run_folder): a run
    # folder's shape, inside the data folder.
    return root / RECEIPTS_DIR / run_id


def write_receipt(root: Path, run_id: str, receipt: dict[str, Any]) -> Path:
    """Add one receipt for ``run_id`` and return its file; never replaces one."""
    if receipt.get("status") not in STATUSES:
        raise ValueError(f"Unknown receipt status {receipt.get('status')!r}")
    folder = _folder(root, run_id)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = folder / f"{stamp}.json"
    record = {"schema_version": RECEIPT_SCHEMA, "run": run_id, **receipt}
    with path.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, indent=2, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    return path


def receipts(root: Path, run_id: str) -> list[dict[str, Any]]:
    """Every receipt of ``run_id``, newest first. One that cannot be read is
    listed as ``{"file", "error"}``, not skipped."""
    folder = _folder(root, run_id)
    if not folder.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.json"), reverse=True):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as exc:
            found.append({"file": path.name, "error": f"cannot be read: {exc}"})
            continue
        found.append({"file": path.name, **value})
    return found


def latest(root: Path, run_id: str) -> dict[str, Any] | None:
    """The session's upload state for a list: its newest receipt, summed up.
    None when it was never uploaded."""
    found = receipts(root, run_id)
    if not found:
        return None
    newest = found[0]
    if "error" in newest:
        return {"status": "unreadable", "file": newest["file"], "error": newest["error"]}
    destination = newest.get("destination") or {}
    return {
        "status": newest.get("status"),
        "finished": newest.get("finished"),
        "destination": destination.get("path"),
        "files": len(newest.get("files") or []),
        "attempts": len(found),
        "ever_verified": any(r.get("status") == "verified" for r in found),
    }
