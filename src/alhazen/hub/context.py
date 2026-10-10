"""What every hub service operation runs against: settings, database,
artifact store, clock and the process-local admission gates.

The gates bound work in THIS process only (docs/hub/server.md "Process
model"). The pilot runs one process, so they are the service's bounds;
correctness never depends on them, because every cross-request invariant
(offsets, seals, quotas, single-use invites) is enforced in the database.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from alhazen.hub.database import Database
from alhazen.hub.errors import HubError
from alhazen.hub.settings import HubSettings
from alhazen.hub.storage import ArtifactStore

Clock = Callable[[], int]


def system_clock() -> int:
    """Milliseconds since the Unix epoch."""
    return int(time.time() * 1000)


def new_id() -> str:
    """An opaque random identifier (128 bits, 32 hex characters)."""
    return secrets.token_hex(16)


class Gate:
    """A bounded admission gate: take a slot or refuse with 429 at once.

    Refusing rather than queueing keeps a burst of uploads or exports from
    occupying the request thread pool that sign-in and catalogue reads share.
    """

    def __init__(self, name: str, slots: int, wait_seconds: float = 0.0) -> None:
        self.name = name
        self._slots = threading.BoundedSemaphore(slots)
        self._wait = wait_seconds

    @contextmanager
    def slot(self) -> Iterator[None]:
        if self._wait:
            acquired = self._slots.acquire(timeout=self._wait)
        else:
            acquired = self._slots.acquire(blocking=False)
        if not acquired:
            raise HubError(
                429,
                "server_busy",
                f"The hub is at its limit of concurrent {self.name}; retry shortly",
                headers={"Retry-After": "5"},
            )
        try:
            yield
        finally:
            self._slots.release()


class OwnerGate:
    """At most ``per_owner`` slots held by one owner at once (auth-review
    finding 2): checked BEFORE the shared `Gate`, so a single account can
    never hold every transfer slot, however slowly it sends. Refuses with
    429 at once, never queues."""

    def __init__(self, name: str, per_owner: int) -> None:
        self.name = name
        self.per_owner = per_owner
        self._held: dict[str, int] = {}
        self._lock = threading.Lock()

    def held(self, owner: str) -> int:
        with self._lock:
            return self._held.get(owner, 0)

    def try_acquire(self, owner: str) -> bool:
        """Take one of the owner's slots if one is free; never waits."""
        with self._lock:
            count = self._held.get(owner, 0)
            if count >= self.per_owner:
                return False
            self._held[owner] = count + 1
            return True

    def release(self, owner: str) -> None:
        with self._lock:
            left = self._held.get(owner, 1) - 1
            if left > 0:
                self._held[owner] = left
            else:
                self._held.pop(owner, None)

    def refusal(self, owner: str) -> HubError:
        return HubError(
            429,
            "owner_transfer_limit",
            f"You already have {self.held(owner)} {self.name} running, the most one account may "
            "run at once; retry when one finishes",
            headers={"Retry-After": "5"},
        )

    @contextmanager
    def slot(self, owner: str) -> Iterator[None]:
        """Take a slot or refuse at once (the app waits briefly first; see
        app._upload_slots)."""
        if not self.try_acquire(owner):
            raise self.refusal(owner)
        try:
            yield
        finally:
            self.release(owner)


@dataclass
class Hub:
    settings: HubSettings
    db: Database
    store: ArtifactStore
    clock: Clock = system_clock
    transfers: Gate = field(init=False)
    exports: Gate = field(init=False)
    hashes: Gate = field(init=False)
    owner_transfers: OwnerGate = field(init=False)

    def __post_init__(self) -> None:
        limits = self.settings.limits
        self.transfers = Gate("transfers", limits.max_concurrent_transfers)
        self.owner_transfers = OwnerGate("uploads", limits.max_transfers_per_owner)
        self.exports = Gate("exports", limits.max_concurrent_exports)
        # A sign-in waits briefly for one of the few hashing slots instead of
        # failing on a momentary overlap; the per-minute admission count
        # (auth.py) is what bounds a flood.
        self.hashes = Gate("sign-ins", self.settings.auth.max_concurrent_hashes, wait_seconds=10.0)
