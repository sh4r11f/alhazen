"""The rig's HTTP client for one fixed, operator-chosen hub.

Secret it hides: how a request reaches the hub — URL canonicalisation, the
bearer header, redirect refusal, timeouts and how a remote failure becomes a
typed :class:`HubError`. Callers name API routes by segments
(:func:`api_path`), never by a URL, so no caller can point a credential at
another host or path.

Standard library only (urllib): the rig adapter must work on a plain alhazen
install with no hub extra.

What this does NOT promise: that the hub is who it says it is beyond TLS
certificate verification of the configured host, or that a request that
timed out did not take effect (every write the rig repeats is idempotent on
the server by contract: chunk replay, session init by client id, complete).
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from http.client import HTTPResponse
from pathlib import Path
from typing import IO, Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

API_PREFIX = "/api/hub/v1"
# A JSON answer larger than this is not a hub answer: refuse rather than
# buffer it. Listing pages are bounded (limit <= 100) and far smaller.
MAX_JSON_BYTES = 16 * 1024 * 1024
# How much of an error body is read to find its {error:{code,message}}.
MAX_ERROR_BYTES = 64 * 1024
DEFAULT_TIMEOUT_S = 20.0
LOOPBACK_NAMES = {"localhost"}


class HubError(Exception):
    """A request the hub refused, or one that never got an answer.

    ``status`` is the HTTP status to report to the local page: the hub's own
    for a refusal, 502 for no answer or a redirect. ``code`` is the hub's
    machine code (``unauthenticated``, ``conflict``, ...) or one of this
    module's: ``hub_unreachable``, ``hub_redirect``, ``hub_bad_response``.
    """

    def __init__(
        self, status: int, code: str, message: str, *, retry_after: str | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        """Whether repeating the same idempotent request may succeed later."""
        return self.status in (0, 408, 429, 502, 503, 504) or self.code == "hub_unreachable"


def _is_loopback(host: str) -> bool:
    if host.lower() in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def canonical_base(url: str, *, allow_http_loopback: bool = False) -> str:
    """The one canonical form of a hub's base URL, or ValueError.

    ``https://host[:port][/prefix]``; no user name or password, query,
    fragment, backslash, control character or dot segment. ``http`` only for a
    loopback host and only when the caller explicitly allows local
    development. A trailing slash and a trailing ``/api/hub/v1`` are dropped,
    so the same hub always has the same identity (the credential and every
    upload job are bound to it).
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Enter the hub's address, such as https://hub.example.org")
    text = url.strip()
    if len(text) > 2048 or any(ord(ch) < 33 or ord(ch) == 127 for ch in text) or "\\" in text:
        raise ValueError("The hub address may not contain spaces, control characters or '\\'")
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http"):
        raise ValueError("The hub address must start with https://")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValueError("The hub address may not contain a user name or password")
    if parts.query or parts.fragment or text.endswith(("?", "#")):
        raise ValueError("The hub address may not contain a query or a fragment")
    host = parts.hostname
    if not host:
        raise ValueError("The hub address has no host name")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("The hub address has an invalid port") from exc
    if scheme == "http":
        if not _is_loopback(host):
            raise ValueError(
                "Only https:// hubs are allowed; http:// is for a hub on this computer "
                "(127.0.0.1) during development"
            )
        if not allow_http_loopback:
            raise ValueError(
                "http:// is only for local development: allow it explicitly for a hub on "
                "this computer"
            )
    path = parts.path.rstrip("/")
    if "%" in path:
        raise ValueError("The hub address's path may not contain percent-escapes")
    segments = [s for s in path.split("/") if s]
    if any(s in (".", "..") for s in segments) or "//" in path:
        raise ValueError("The hub address's path may not contain empty or dot segments")
    path = "/" + "/".join(segments) if segments else ""
    if path.endswith(API_PREFIX):
        path = path[: -len(API_PREFIX)]
    host_text = f"[{host}]" if ":" in host else host.lower()
    netloc = host_text if port is None else f"{host_text}:{port}"
    return urlunsplit((scheme, netloc, path, "", ""))


def api_path(*segments: str) -> str:
    """``/<segment>/<segment>`` under the API prefix, each segment escaped.

    Segments are names this module's callers chose or ids checked by them;
    escaping is the second line, so an id can never add a path level."""
    out = []
    for segment in segments:
        if not isinstance(segment, str) or segment in ("", ".", ".."):
            raise ValueError("Invalid API path segment")
        out.append(quote(segment, safe=""))
    return "/" + "/".join(out)


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect is answered as the error it is here: following one would
    send the bearer (or a password) to an address nobody approved."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        return None


def build_opener() -> urllib.request.OpenerDirector:
    """urllib with certificate verification and no redirects. Proxies from
    the environment still apply (a rig behind an institutional proxy)."""
    context = ssl.create_default_context()
    return urllib.request.build_opener(
        _RefuseRedirects(), urllib.request.HTTPSHandler(context=context)
    )


def _error_from(status: int, body: bytes, retry_after: str | None) -> HubError:
    code, message = "hub_error", f"The hub answered HTTP {status}"
    try:
        payload = json.loads(body.decode("utf-8")) if body else None
    except (UnicodeDecodeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or code)[:64]
            message = str(error.get("message") or message)[:2000]
        elif isinstance(error, str):
            message = error[:2000]
        elif "detail" in payload:
            message = str(payload["detail"])[:2000]
    if status == 401 and code == "hub_error":
        code = "unauthenticated"
    return HubError(status, code, message, retry_after=retry_after)


class HubClient:
    """Requests to one hub's API, optionally with one bearer token.

    Thread-safe: holds no per-request state. ``opener`` is the tests' seam."""

    def __init__(
        self,
        base: str,
        token: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        self.base = base
        self.token = token
        self.timeout = timeout
        self._opener = opener or build_opener()

    def url(self, path: str, query: Mapping[str, str] | None = None) -> str:
        if not path.startswith("/") or "?" in path or "#" in path or "\\" in path:
            raise ValueError("Internal error: API path must come from api_path()")
        text = self.base + API_PREFIX + path
        if query:
            text += "?" + urlencode([(k, v) for k, v in query.items() if v is not None])
        return text

    @contextmanager
    def open(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        json_body: Any = None,
        data: bytes | IO[bytes] | None = None,
        length: int | None = None,
        content_type: str | None = None,
        headers: Mapping[str, str] | None = None,
        authenticated: bool = True,
    ) -> Iterator[HTTPResponse]:
        """One request; yields the open response for 2xx, else HubError.

        Only the headers named here are sent: Accept, the content type and
        length of the body, the bearer, and the caller's fixed extras (the
        upload chunk's digest). Nothing from the local page is forwarded."""
        sent: dict[str, str] = {"Accept": "application/json"}
        body: bytes | IO[bytes] | None = data
        if json_body is not None:
            body = json.dumps(json_body, allow_nan=False).encode("utf-8")
            sent["Content-Type"] = "application/json"
        elif content_type is not None:
            sent["Content-Type"] = content_type
        if body is not None:
            sent["Content-Length"] = str(len(body) if isinstance(body, bytes) else length)
            if not isinstance(body, bytes) and length is None:
                raise ValueError("Internal error: a streamed body needs its length")
        if authenticated:
            if not self.token:
                raise HubError(401, "unauthenticated", "Sign in to the hub first")
            sent["Authorization"] = f"Bearer {self.token}"
        for name, value in (headers or {}).items():
            sent[name] = value
        request = urllib.request.Request(
            self.url(path, query), data=body, headers=sent, method=method
        )
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            with exc:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if 300 <= exc.code < 400:
                    raise HubError(
                        502,
                        "hub_redirect",
                        "The hub answered with a redirect, which is not followed: check the "
                        "hub address (credentials are never sent to another address)",
                    ) from None
                try:
                    detail = exc.read(MAX_ERROR_BYTES)
                except OSError:
                    detail = b""
            raise _error_from(exc.code, detail, retry_after) from None
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, ssl.SSLCertVerificationError):
                text = f"its certificate could not be verified ({reason.verify_message})"
            else:
                text = str(reason)
            raise HubError(
                502, "hub_unreachable", f"The hub at {self.base} did not answer: {text}"
            ) from None
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise HubError(
                502, "hub_unreachable", f"The hub at {self.base} did not answer: {exc}"
            ) from None
        with response:
            yield response

    def json(self, method: str, path: str, **kwargs: Any) -> Any:
        """A request whose answer is JSON (an empty 204 is None)."""
        with self.open(method, path, **kwargs) as response:
            try:
                body = response.read(MAX_JSON_BYTES + 1)
            except (OSError, TimeoutError) as exc:
                raise HubError(
                    502, "hub_unreachable", f"The hub's answer was cut off: {exc}"
                ) from None
        if len(body) > MAX_JSON_BYTES:
            raise HubError(502, "hub_bad_response", "The hub's answer is too large")
        if not body:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise HubError(502, "hub_bad_response", "The hub's answer is not JSON") from None

    def download(
        self, path: str, destination: Path, *, max_bytes: int, expected_sha256: str | None = None
    ) -> tuple[int, str]:
        """Stream a file to ``destination`` (created, never replaced) and
        return ``(size, sha256)``. Too many bytes or a different digest
        removes the partial file and raises HubError."""
        digest = hashlib.sha256()
        size = 0
        try:
            with self.open("GET", path) as response, destination.open("xb") as out:
                declared = response.headers.get("Content-Length")
                if declared is not None and declared.isdigit() and int(declared) > max_bytes:
                    raise HubError(413, "too_large", "The release is larger than allowed")
                while True:
                    try:
                        block = response.read(1 << 20)
                    except (OSError, TimeoutError) as exc:
                        raise HubError(
                            502, "hub_unreachable", f"The download was cut off: {exc}"
                        ) from None
                    if not block:
                        break
                    size += len(block)
                    if size > max_bytes:
                        raise HubError(413, "too_large", "The release is larger than allowed")
                    digest.update(block)
                    out.write(block)
                out.flush()
            sha = digest.hexdigest()
            if expected_sha256 is not None and sha != expected_sha256:
                raise HubError(
                    409,
                    "hash_mismatch",
                    "The downloaded release does not match its published SHA-256; nothing "
                    "was installed",
                )
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return size, sha


def probe_hub(
    base: str, *, timeout: float = 10.0, opener: urllib.request.OpenerDirector | None = None
) -> dict[str, Any]:
    """The hub's public ``/config``, checked to be an alhazen hub speaking
    API version 1; HubError otherwise. Sends no credential."""
    answer = HubClient(base, None, timeout=timeout, opener=opener).json(
        "GET", "/config", authenticated=False
    )
    if not isinstance(answer, dict) or answer.get("role") != "server":
        raise HubError(502, "not_a_hub", "That address does not answer as an alhazen hub")
    if answer.get("api_version") != 1:
        raise HubError(
            502, "unsupported_hub", "That hub speaks another API version than this alhazen"
        )
    return answer
