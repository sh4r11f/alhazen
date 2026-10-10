"""Users' provider keys, encrypted at rest under the operator's wrapping key.

Hides: the encryption (Fernet: AES-128-CBC + HMAC-SHA256, from
``cryptography``), the binding of each ciphertext to its owner and provider,
and the only path by which a plaintext key leaves this module
(`reveal`, called by the job worker to build one provider client).

What the outside sees of a key: its provider, its last four characters
(``hint``) and when it was stored and last replaced. No route returns more,
nothing logs a key, and the audit log records only that a key was set or
removed.
"""

from __future__ import annotations

import base64
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Connection, delete, insert, select, update

from alhazen.hub.auth import audit
from alhazen.hub.context import Hub
from alhazen.hub.errors import HubError, invalid
from alhazen.hub.schema import ai_keys
from alhazen.hub.settings import AI_PROVIDERS, wrapping_key
from alhazen.hub.timefmt import iso

MIN_KEY_CHARS = 16
MAX_KEY_CHARS = 512


def disabled() -> HubError:
    return HubError(403, "ai_disabled", "AI authoring is not enabled on this hub")


def require_enabled(hub: Hub) -> None:
    if not hub.settings.ai.enabled:
        raise disabled()


def check_provider(provider: object) -> str:
    if not isinstance(provider, str) or provider not in AI_PROVIDERS:
        raise invalid(f"provider must be one of {', '.join(AI_PROVIDERS)}", "unknown_provider")
    return provider


def check_key_shape(key: object) -> str:
    """Shape only: printable ASCII without spaces, 16-512 characters. A key
    is never echoed back in an error."""
    if not isinstance(key, str):
        raise invalid("key must be a string", "invalid_key")
    key = key.strip()
    if not MIN_KEY_CHARS <= len(key) <= MAX_KEY_CHARS or not all(33 <= ord(c) <= 126 for c in key):
        raise invalid(
            f"That does not look like an API key ({MIN_KEY_CHARS}-{MAX_KEY_CHARS} printable "
            "characters, no spaces)",
            "invalid_key",
        )
    return key


def _fernet(hub: Hub) -> Fernet:
    require_enabled(hub)
    return Fernet(base64.urlsafe_b64encode(wrapping_key(hub.settings.ai.key_secret)))


def _binding(user_id: str, provider: str) -> bytes:
    return f"alhazen-hub-ai-key:v1:{user_id}:{provider}\n".encode()


def seal(hub: Hub, user_id: str, provider: str, key: str) -> str:
    return _fernet(hub).encrypt(_binding(user_id, provider) + key.encode("ascii")).decode("ascii")


def _open(hub: Hub, user_id: str, provider: str, ciphertext: str) -> str:
    try:
        plain = _fernet(hub).decrypt(ciphertext.encode("ascii"))
    except (InvalidToken, ValueError):
        raise HubError(
            409,
            "key_unreadable",
            "Your stored key can no longer be read on this hub; enter it again",
        ) from None
    prefix = _binding(user_id, provider)
    if not plain.startswith(prefix):
        raise HubError(
            409,
            "key_unreadable",
            "Your stored key can no longer be read on this hub; enter it again",
        )
    return plain[len(prefix) :].decode("ascii")


def view(row: Any) -> dict[str, Any]:
    return {
        "provider": row.provider,
        "hint": row.key_hint,
        "set": True,
        "created_at": iso(row.created_at),
        "rotated_at": iso(row.rotated_at),
    }


def list_keys(conn: Connection, user_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(ai_keys).where(ai_keys.c.user_id == user_id).order_by(ai_keys.c.provider)
    ).all()
    return [view(r) for r in rows]


def store(hub: Hub, user_id: str, provider: str, key: str) -> dict[str, Any]:
    """Store or replace one key; returns its public view."""
    ciphertext = seal(hub, user_id, provider, key)
    now = hub.clock()
    with hub.db.transaction() as conn:
        existing = conn.execute(
            select(ai_keys.c.created_at)
            .where(ai_keys.c.user_id == user_id, ai_keys.c.provider == provider)
            .with_for_update()
        ).first()
        values = {"ciphertext": ciphertext, "key_hint": key[-4:], "rotated_at": now}
        if existing is None:
            conn.execute(
                insert(ai_keys).values(user_id=user_id, provider=provider, created_at=now, **values)
            )
        else:
            conn.execute(
                update(ai_keys)
                .where(ai_keys.c.user_id == user_id, ai_keys.c.provider == provider)
                .values(**values)
            )
        audit(
            conn,
            now,
            f"user:{user_id}",
            "ai.key.replace" if existing is not None else "ai.key.set",
            f"ai-key:{provider}",
            {},
        )
        row = conn.execute(
            select(ai_keys).where(ai_keys.c.user_id == user_id, ai_keys.c.provider == provider)
        ).one()
        return view(row)


def remove(hub: Hub, user_id: str, provider: str) -> bool:
    now = hub.clock()
    with hub.db.transaction() as conn:
        removed = conn.execute(
            delete(ai_keys).where(ai_keys.c.user_id == user_id, ai_keys.c.provider == provider)
        ).rowcount
        if removed:
            audit(conn, now, f"user:{user_id}", "ai.key.remove", f"ai-key:{provider}", {})
    return bool(removed)


def has_key(conn: Connection, user_id: str, provider: str) -> bool:
    return (
        conn.execute(
            select(ai_keys.c.provider).where(
                ai_keys.c.user_id == user_id, ai_keys.c.provider == provider
            )
        ).first()
        is not None
    )


def key_required(provider: str) -> HubError:
    return HubError(
        409,
        "key_required",
        f"Add your {provider} API key before starting",
        extra={"provider": provider},
    )


def reveal(hub: Hub, user_id: str, provider: str) -> str:
    """The plaintext key, for building one provider client in the worker.
    Raises 409 key_required (removed meanwhile) or key_unreadable."""
    with hub.db.transaction() as conn:
        row = conn.execute(
            select(ai_keys.c.ciphertext).where(
                ai_keys.c.user_id == user_id, ai_keys.c.provider == provider
            )
        ).first()
    if row is None:
        raise key_required(provider)
    return _open(hub, user_id, provider, row.ciphertext)
