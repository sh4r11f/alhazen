"""The service's one background worker.

Runs, in order, on start and then periodically: seal reconciliation (finish
abandoned seals, report missing or orphaned artifacts), expiry of stale
unfinished uploads, pruning of old sign-in throttle records and ended
sign-in sessions, and the trial
index queue. One thread means at most one index job at a time (review gate
M4). Its latest report and any failure are exposed through /readyz, so a
broken step is visible rather than only logged.

Process model: one worker per server process. Claims and leases in the
database keep two processes from indexing or sealing the same session, but
the supported pilot runs a single process (docs/hub/server.md).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from alhazen.hub.auth import housekeeping as auth_housekeeping
from alhazen.hub.context import Hub
from alhazen.hub.trials import claim_next, index_session
from alhazen.hub.uploads import expire_stale, reconcile

log = logging.getLogger(__name__)

RECONCILE_EVERY_MS = 10 * 60 * 1000
HOUSEKEEP_EVERY_MS = 60 * 60 * 1000
POLL_SECONDS = 30.0


class Maintenance:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.report: dict[str, Any] | None = None
        self.last_error: str | None = None
        self._last_reconcile = -RECONCILE_EVERY_MS
        self._last_housekeep = -HOUSEKEEP_EVERY_MS
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._loop, name="alhazen-hub-maintenance", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._wake.wait(POLL_SECONDS)
            self._wake.clear()

    def run_once(self, *, force_reconcile: bool = False) -> None:
        """One pass. Each step's failure is recorded and does not stop the others."""
        with self._lock:
            now = self.hub.clock()
            errors = []
            if force_reconcile or now - self._last_reconcile >= RECONCILE_EVERY_MS:
                try:
                    self.report = reconcile(self.hub)
                    self._last_reconcile = now
                except Exception as exc:  # noqa: BLE001 - recorded for /readyz and logged
                    log.exception("hub reconciliation failed")
                    errors.append(f"reconciliation: {type(exc).__name__}")
            if now - self._last_housekeep >= HOUSEKEEP_EVERY_MS:
                try:
                    expire_stale(self.hub)
                    auth_housekeeping(self.hub)
                    self._last_housekeep = now
                except Exception as exc:  # noqa: BLE001 - recorded for /readyz and logged
                    log.exception("hub housekeeping failed")
                    errors.append(f"housekeeping: {type(exc).__name__}")
            try:
                self.drain_index()
            except Exception as exc:  # noqa: BLE001 - recorded for /readyz and logged
                log.exception("hub trial indexing failed")
                errors.append(f"indexing: {type(exc).__name__}")
            self.last_error = "; ".join(errors) or None

    def drain_index(self, session_id: str | None = None) -> int:
        """Index every due session (or only ``session_id``); return how many."""
        done = 0
        while not self._stop.is_set():
            claimed = claim_next(self.hub, session_id)
            if claimed is None:
                return done
            index_session(self.hub, claimed)
            done += 1
            if session_id is not None:
                return done
        return done
