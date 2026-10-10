"""The hub service's one error type and its wire shape.

Every refusal the service makes is a `HubError`: an HTTP status, a stable
machine-readable ``code`` clients branch on, and a ``message`` written for a
person. Messages never carry filesystem paths, SQL or exception text from a
dependency; the app's last-resort handler turns anything else into a generic
``internal`` error after logging it server-side.
"""

from __future__ import annotations

from typing import Any


class HubError(Exception):
    """A refusal with a status, a stable code and a human message.

    ``extra`` adds documented, non-sensitive fields to the error object (for
    example ``received`` on an upload offset mismatch, so a client can resume
    without a second request). ``headers`` are sent with the response
    (``Retry-After`` on a rate limit).
    """

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        extra: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = dict(extra or {})
        self.headers = dict(headers or {})

    def body(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, **self.extra}}


def invalid(message: str, code: str = "invalid_request") -> HubError:
    return HubError(400, code, message)


def not_found(what: str = "Not found") -> HubError:
    # One answer for "does not exist" and "exists but is not yours": a
    # different status would let anyone probe for other users' identifiers.
    return HubError(404, "not_found", what)


def conflict(code: str, message: str, **extra: Any) -> HubError:
    return HubError(409, code, message, extra=extra)


def too_large(message: str, code: str = "payload_too_large") -> HubError:
    return HubError(413, code, message)
