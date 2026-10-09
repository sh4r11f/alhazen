"""The rig's hub connection and sign-in, kept in the workspace's state folder.

Secret it hides: where and how the configured hub address, the bearer token
and this rig's random identity are stored. Files live under
``<workspace>/hub/`` (outside every experiment's source), are replaced
atomically, and on POSIX are readable only by the owning user (directory
0700, files 0600).

What callers must NOT rely on: protection from software running as the same
OS user. Trusted experiment code started from this workspace runs as that
user and can read these files; owner-only permissions guard against other
accounts on the machine, not against code the operator chose to run. On
Windows the files inherit the user profile's ACLs.

The token is never returned to the browser: :meth:`RigState.public` is the
only shape that leaves this module towards the page.
"""

from __future__ import annotations

import functools
import json
import os
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alhazen.hub.client import HubClient, HubError

CONNECTION_FILE = "connection.json"
CREDENTIAL_FILE = "credential.json"
RIG_FILE = "rig.json"


@dataclass(frozen=True)
class Connection:
    """The one hub this rig talks to: its canonical base URL."""

    base: str
    allow_http_loopback: bool = False


@dataclass(frozen=True)
class Credential:
    """A bearer issued by ``base`` for ``user`` (``{id, username, display_name}``)."""

    base: str
    token: str
    user: dict[str, Any]
    expires_at: str | None = None

    @property
    def user_id(self) -> str:
        return str(self.user.get("id", ""))


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        os.chmod(path, 0o700)


def write_private(path: Path, text: str) -> None:
    """Replace ``path`` with ``text``, created owner-only (0600) before any
    byte is written, flushed to disk, then renamed over the target."""
    temporary = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    private = functools.partial(os.open, mode=0o600)
    try:
        with open(temporary, "x", encoding="utf-8", opener=private) as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError(f"The hub settings file {path.name} cannot be read: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"The hub settings file {path.name} is not an object")
    return value


def _expired(expires_at: str | None) -> bool:
    """Whether an ISO-8601 UTC expiry has passed. An unparseable expiry is
    treated as expired: the next request would be refused anyway."""
    if not expires_at:
        return False
    from datetime import datetime, timezone

    try:
        moment = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp() <= time.time()


class RigState:
    """Hub connection, credential and rig identity for one workspace.

    Every change bumps :attr:`epoch`, which upload workers compare before
    each request: a sign-out or a new hub fences work that started before it.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        _private_dir(directory)
        self._lock = threading.RLock()
        self.epoch = 0

    # -- connection -----------------------------------------------------------

    def connection(self) -> Connection | None:
        with self._lock:
            record = _read(self.directory / CONNECTION_FILE)
        if not record or not isinstance(record.get("base"), str):
            return None
        return Connection(record["base"], bool(record.get("allow_http_loopback", False)))

    def save_connection(self, connection: Connection) -> None:
        """Save a new hub address. A different address forgets the sign-in:
        a token belongs to the hub that issued it."""
        with self._lock:
            current = self.connection()
            if current is None or current.base != connection.base:
                self.clear_credential()
            write_private(
                self.directory / CONNECTION_FILE,
                json.dumps(
                    {
                        "base": connection.base,
                        "allow_http_loopback": connection.allow_http_loopback,
                    },
                    indent=2,
                ),
            )
            self.epoch += 1

    def clear_connection(self) -> None:
        with self._lock:
            self.clear_credential()
            (self.directory / CONNECTION_FILE).unlink(missing_ok=True)
            self.epoch += 1

    # -- credential -------------------------------------------------------------

    def credential(self) -> Credential | None:
        """The stored sign-in, if it belongs to the configured hub and has not
        expired; otherwise None (and an expired or orphaned one is removed)."""
        with self._lock:
            record = _read(self.directory / CREDENTIAL_FILE)
            if record is None:
                return None
            connection = self.connection()
            try:
                credential = Credential(
                    base=str(record["base"]),
                    token=str(record["token"]),
                    user=dict(record["user"]),
                    expires_at=record.get("expires_at"),
                )
            except (KeyError, TypeError, ValueError):
                self.clear_credential()
                return None
            if (
                connection is None
                or credential.base != connection.base
                or not credential.token
                or not credential.user_id
                or _expired(credential.expires_at)
            ):
                self.clear_credential()
                return None
            return credential

    def save_credential(self, credential: Credential) -> None:
        with self._lock:
            write_private(
                self.directory / CREDENTIAL_FILE,
                json.dumps(
                    {
                        "base": credential.base,
                        "token": credential.token,
                        "user": credential.user,
                        "expires_at": credential.expires_at,
                    },
                    indent=2,
                ),
            )
            self.epoch += 1

    def clear_credential(self, expected: Credential | None = None) -> bool:
        """Forget the stored sign-in; with ``expected``, only if it is still
        exactly that one (base, user, token): a request refused with an old
        bearer must never sign out an account that signed in since.
        True when something was removed."""
        with self._lock:
            path = self.directory / CREDENTIAL_FILE
            if expected is not None:
                record = _read(path)
                if record is None or (
                    record.get("base"),
                    str((record.get("user") or {}).get("id", "")),
                    record.get("token"),
                ) != (expected.base, expected.user_id, expected.token):
                    return False
            existed = path.exists()
            path.unlink(missing_ok=True)
            if existed:
                self.epoch += 1
            return existed

    # -- this rig -------------------------------------------------------------------

    def rig_id(self) -> str:
        """A random id made once per workspace: part of every session's
        upload identity, so two rigs with the same folder layout never
        collide on the hub."""
        with self._lock:
            record = _read(self.directory / RIG_FILE)
            if record and isinstance(record.get("rig_id"), str) and record["rig_id"]:
                return str(record["rig_id"])
            rig_id = secrets.token_hex(16)
            write_private(self.directory / RIG_FILE, json.dumps({"rig_id": rig_id}, indent=2))
            return rig_id

    # -- what the page may see ---------------------------------------------------------

    def public(self) -> dict[str, Any]:
        """Connection and operator, never the token."""
        connection = self.connection()
        credential = self.credential()
        if connection is None:
            state = "not_configured"
        elif credential is None:
            state = "signed_out"
        else:
            state = "signed_in"
        return {
            "state": state,
            "base_url": connection.base if connection else None,
            "user": dict(credential.user) if credential else None,
            "expires_at": credential.expires_at if credential else None,
        }


def public_user(user: Any) -> dict[str, str]:
    """The user shape that is stored and shown: ``{id, username, display_name}``."""
    user = user if isinstance(user, dict) else {}
    return {k: str(user.get(k, "")) for k in ("id", "username", "display_name")}


def sign_in(client: HubClient, username: str, password: str) -> Credential:
    """Exchange a password for a bearer at ``client``'s hub (``/auth/token``).
    The password is sent once, in the body, and kept nowhere."""
    answer = client.json(
        "POST",
        "/auth/token",
        json_body={"username": username, "password": password},
        authenticated=False,
    )
    token = answer.get("access_token") if isinstance(answer, dict) else None
    user = public_user(answer.get("user") if isinstance(answer, dict) else None)
    if not isinstance(token, str) or not token or not user["id"]:
        raise HubError(502, "hub_bad_response", "The hub's sign-in answer is incomplete")
    expires = answer.get("expires_at")
    return Credential(client.base, token, user, expires if isinstance(expires, str) else None)
