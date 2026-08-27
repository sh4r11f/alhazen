"""DataRecorder: accumulates trials and events in memory, writes at teardown.

Two tables, two truths:

- ``trials.csv`` — one row per trial *attempt* that produced a record.
  ``trial_index`` counts every attempt and never resets, so values are not
  guaranteed consecutive (paused/quit attempts consume an index but write no
  row).
- ``events.csv`` — one row per bus event, written even for attempts that
  never got a trials row. The two tables therefore do not join one-to-one on
  ``trial_index``, and that is documented behavior, not a bug.

Column ordering in trials.csv is deterministic: identity columns first,
everything else alphabetically, ``t_*`` timestamps last; a column appears
only if some row actually populated it.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from alhazen.core.events import Event

_LEADING = ("trial_index", "attempt", "outcome", "completed", "success", "abort_reason")


def ordered_trial_columns(rows: list[dict[str, Any]]) -> list[str]:
    present: set[str] = set()
    for row in rows:
        present.update(k for k, v in row.items() if v is not None)
    leading = [c for c in _LEADING if c in present]
    trailing = sorted(c for c in present if c.startswith("t_"))
    middle = sorted(present - set(leading) - set(trailing))
    return leading + middle + trailing


class DataRecorder:
    def __init__(self, trials_path: Path, events_path: Path) -> None:
        self._trials_path = trials_path
        self._events_path = events_path
        self._trials: list[dict[str, Any]] = []
        self._events: list[dict[str, Any]] = []

    @property
    def trials(self) -> list[dict[str, Any]]:
        return list(self._trials)

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self._events)

    def add_trial(self, record: dict[str, Any]) -> None:
        self._trials.append(dict(record))

    def on_event(self, event: Event) -> None:
        """EventBus subscriber. Payload is JSON-encoded so a consumer parses
        it with ``json.loads`` instead of eyeballing a Python repr."""
        self._events.append(
            {
                "trial_index": event.trial_index,
                "event": event.name,
                "t": event.t,
                "payload_json": json.dumps(event.payload, sort_keys=True),
            }
        )

    def write(self) -> None:
        """Write both tables. Called from the runner's teardown, which
        guarantees this is attempted even when other teardown steps fail."""
        columns = ordered_trial_columns(self._trials)
        with self._trials_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in self._trials:
                writer.writerow({k: v for k, v in row.items() if v is not None})
        with self._events_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["trial_index", "event", "t", "payload_json"])
            writer.writeheader()
            writer.writerows(self._events)
