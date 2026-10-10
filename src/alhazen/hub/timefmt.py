"""Timestamps on the wire: stored as epoch milliseconds, sent as ISO 8601 UTC."""

from __future__ import annotations

from datetime import datetime, timezone


def iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
