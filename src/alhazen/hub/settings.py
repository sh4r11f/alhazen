"""Hub service configuration: one TOML file, kept outside every repository.

The file names where the database and the artifact archive are, which
origins may use the browser API, and the service's limits. Real hosts,
paths and credentials live only in the operator's private copy; the
repository ships a template with placeholders (docs/hub/server.md).

Standard library only, so the rig CLI can read and check a configuration
without the optional ``[hub]`` dependencies installed.

A database URL that carries a password is a secret: `HubSettings` never
shows it in ``repr`` or in errors, and ``url_env`` lets the URL come from an
environment variable so the file itself can be shared with no secret in it.
"""

from __future__ import annotations

import dataclasses
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10 (tomli is a core dependency there)
    import tomli as tomllib

MIB = 1024 * 1024
GIB = 1024 * MIB

# The transfer protocol's chunk ceiling (api-contract.md). A configuration
# may lower it, never raise it: clients are written against 8 MiB.
CHUNK_CEILING = 8 * MIB


class SettingsError(ValueError):
    """The configuration file is missing, malformed or unsafe."""


@dataclass(frozen=True)
class HubLimits:
    """Server-side caps (accepted review gate M4). Each refusal names its limit."""

    max_package_bytes: int = 256 * MIB
    max_package_expanded_bytes: int = 1 * GIB
    max_package_files: int = 10_000
    max_experiments_per_owner: int = 100
    max_versions_per_experiment: int = 100
    max_session_bytes: int = 20 * GIB
    max_session_files: int = 10_000
    max_chunk_bytes: int = CHUNK_CEILING
    # Reserved (staging) plus stored (sessions and package versions) bytes
    # one owner may hold.
    user_quota_bytes: int = 50 * GIB
    max_staging_sessions: int = 3
    # An upload untouched this long expires: its partial bytes are removed
    # and its reservation released. Committed sessions are never affected.
    staging_retention_seconds: int = 7 * 24 * 3600
    # How long one completion attempt owns a session's seal before a retry
    # (or startup reconciliation) may take it over.
    seal_lease_seconds: int = 30 * 60
    # Uploads (chunks, packages, seals) running at once in ONE server
    # process. The pilot runs exactly one process (serve() pins workers=1),
    # so this is the service's transfer bound; see docs/hub/server.md.
    max_concurrent_transfers: int = 4
    max_concurrent_exports: int = 2
    max_json_bytes: int = 1 * MIB
    max_manifest_json_bytes: int = 8 * MIB
    # Derived trial index budgets; raw files are kept whole regardless.
    max_indexed_rows: int = 100_000
    max_indexed_columns: int = 256
    max_csv_row_bytes: int = 1 * MIB
    max_csv_cell_bytes: int = 64 * 1024
    max_indexed_file_bytes: int = 512 * MIB
    # Request-body admission (auth-review finding 2). One owner may run at
    # most this many body-carrying uploads (chunks, packages) at once, so no
    # single account can hold every transfer slot.
    max_transfers_per_owner: int = 2
    # A request over that share waits up to this long for one of its own
    # slots (a rig's back-to-back retries, two rigs of one account) before
    # 429 owner_transfer_limit. Waiting never holds a shared slot.
    owner_transfer_wait_seconds: int = 10
    # A request body must keep arriving: at most this long between two
    # received parts, else 408 and the slot is released.
    body_idle_seconds: int = 30
    # And it must finish within base + expected bytes / floor rate, where the
    # expected size is the declared Content-Length (or the route's limit).
    # Defaults: an 8 MiB chunk gets 60 s + 128 s, a 256 MiB package 60 s +
    # about 68 min, a 1 MiB JSON body 60 s + 16 s.
    body_base_seconds: int = 60
    body_min_bytes_per_second: int = 64 * 1024

    def public(self) -> dict[str, int]:
        """The limits a client may need, for GET /config."""
        return {
            "max_package_bytes": self.max_package_bytes,
            "max_package_expanded_bytes": self.max_package_expanded_bytes,
            "max_package_files": self.max_package_files,
            "max_session_bytes": self.max_session_bytes,
            "max_session_files": self.max_session_files,
            "max_chunk_bytes": self.max_chunk_bytes,
            "user_quota_bytes": self.user_quota_bytes,
            "max_staging_sessions": self.max_staging_sessions,
            "page_limit_max": 100,
        }


@dataclass(frozen=True)
class AuthPolicy:
    """Session lifetimes, password rules and sign-in admission (gate M1)."""

    browser_idle_seconds: int = 3600
    browser_max_seconds: int = 12 * 3600
    token_seconds: int = 12 * 3600
    failure_window_seconds: int = 15 * 60
    failures_per_account: int = 5
    failures_per_address: int = 20
    # Password hashes (login, token, register) the whole service starts per
    # minute, and how many may run at once: Argon2 at 64 MiB each.
    hashes_per_minute: int = 60
    max_concurrent_hashes: int = 2
    registrations_per_address_per_hour: int = 20
    min_password_length: int = 12
    max_password_length: int = 1024
    invite_days: int = 14
    # Ended (expired or revoked) sign-in sessions are deleted this long after
    # they ended by the hourly housekeeping (auth-review finding 8).
    session_retention_seconds: int = 7 * 24 * 3600


@dataclass(frozen=True)
class HubSettings:
    """Everything the service needs to start, validated once."""

    database_url: str = field(repr=False)
    artifact_root: Path
    public_origin: str
    extra_origins: tuple[str, ...] = ()
    # Passed to uvicorn: client addresses for throttling come from
    # X-Forwarded-For only for these proxy addresses ("" trusts none).
    forwarded_allow_ips: str = ""
    min_free_bytes: int = 1 * GIB
    limits: HubLimits = field(default_factory=HubLimits)
    auth: AuthPolicy = field(default_factory=AuthPolicy)

    def __post_init__(self) -> None:
        _check_database_url(self.database_url)
        if not self.artifact_root.is_absolute():
            raise SettingsError("storage.artifact_root must be an absolute path")
        for origin in self.allowed_origins:
            _check_origin(origin)
        if self.limits.max_chunk_bytes > CHUNK_CEILING or self.limits.max_chunk_bytes < 1:
            raise SettingsError(f"limits.max_chunk_bytes must be between 1 and {CHUNK_CEILING}")
        for name, value in dataclasses.asdict(self.limits).items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise SettingsError(f"limits.{name} must be a positive integer")
        for name, value in dataclasses.asdict(self.auth).items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise SettingsError(f"auth.{name} must be a positive integer")

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        return (self.public_origin, *self.extra_origins)

    @property
    def secure_cookies(self) -> bool:
        return self.public_origin.startswith("https://")

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def redacted_database_url(self) -> str:
        """The database URL with any password replaced, for logs and errors."""
        return _redact(self.database_url)

    @classmethod
    def for_development(
        cls,
        root: Path,
        *,
        public_origin: str = "http://127.0.0.1:8750",
        **overrides: Any,
    ) -> HubSettings:
        """Local single-process development: SQLite and artifacts under ``root``.

        Never for a deployment: SQLite is for one process on a local disk
        (docs/hub/server.md), and the deployed pilot uses PostgreSQL.
        """
        root = root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        return cls(
            database_url=f"sqlite:///{(root / 'hub.sqlite3').as_posix()}",
            artifact_root=root / "artifacts",
            public_origin=public_origin,
            **overrides,
        )


def load_settings(config_path: Path, environ: Mapping[str, str] | None = None) -> HubSettings:
    """Read and validate a hub TOML configuration.

    Relative paths are resolved against the file's own folder. Unknown keys
    are refused, so a misspelt limit fails here instead of silently keeping
    its default.
    """
    environ = os.environ if environ is None else environ
    path = Path(config_path).expanduser()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SettingsError(f"hub configuration not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"hub configuration is not valid TOML: {exc}") from None
    _only(raw, {"server", "database", "storage", "limits", "auth"}, "top level")
    server = _table(raw, "server")
    database = _table(raw, "database")
    storage = _table(raw, "storage")
    _only(server, {"public_origin", "extra_origins", "forwarded_allow_ips"}, "[server]")
    _only(database, {"url", "url_env"}, "[database]")
    _only(storage, {"artifact_root", "min_free_bytes"}, "[storage]")

    if "url" in database and "url_env" in database:
        raise SettingsError("[database] takes url or url_env, not both")
    if "url_env" in database:
        name = _string(database, "url_env")
        url = environ.get(name, "")
        if not url:
            raise SettingsError(f"environment variable {name} (database.url_env) is not set")
    else:
        url = _string(database, "url")
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        relative = url[len("sqlite:///") :]
        if relative and not Path(relative).is_absolute():
            url = f"sqlite:///{(path.parent / relative).resolve().as_posix()}"

    root = Path(_string(storage, "artifact_root")).expanduser()
    if not root.is_absolute():
        root = (path.parent / root).resolve()

    extra = server.get("extra_origins", [])
    if not isinstance(extra, list) or not all(isinstance(o, str) for o in extra):
        raise SettingsError("server.extra_origins must be a list of origin strings")

    kwargs: dict[str, Any] = {
        "database_url": url,
        "artifact_root": root,
        "public_origin": _string(server, "public_origin"),
        "extra_origins": tuple(extra),
        "forwarded_allow_ips": str(server.get("forwarded_allow_ips", "")),
    }
    if "min_free_bytes" in storage:
        kwargs["min_free_bytes"] = storage["min_free_bytes"]
    kwargs["limits"] = _dataclass_from(HubLimits, _table(raw, "limits"), "[limits]")
    kwargs["auth"] = _dataclass_from(AuthPolicy, _table(raw, "auth"), "[auth]")
    try:
        return HubSettings(**kwargs)
    except SettingsError:
        raise
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"invalid hub configuration: {exc}") from None


def _dataclass_from(kind: Any, values: dict[str, Any], where: str) -> Any:
    names = {f.name for f in dataclasses.fields(kind)}
    _only(values, names, where)
    return kind(**values)


def _table(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise SettingsError(f"[{name}] must be a table")
    return value


def _only(values: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise SettingsError(f"unknown key(s) in {where}: {', '.join(unknown)}")


def _string(values: dict[str, Any], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"{key} is required and must be a non-empty string")
    return value.strip()


_ORIGIN = re.compile(r"^https?://[A-Za-z0-9.\-\[\]:]+$")


def _check_origin(origin: str) -> None:
    if not _ORIGIN.match(origin) or origin.endswith("/"):
        raise SettingsError(
            f"origin {origin!r} must be scheme://host[:port] with no path or trailing slash"
        )
    parts = urlsplit(origin)
    if parts.scheme == "http" and parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise SettingsError(f"origin {origin!r} uses plain http on a non-loopback host; use https")


def _check_database_url(url: str) -> None:
    scheme = url.split(":", 1)[0]
    if scheme not in ("sqlite", "postgresql", "postgresql+psycopg"):
        raise SettingsError(
            f"database URL scheme {scheme!r} is not supported (sqlite for local development, "
            "postgresql for a deployment)"
        )
    if scheme == "sqlite" and (":memory:" in url or url in ("sqlite://", "sqlite:///")):
        raise SettingsError("an in-memory SQLite database cannot back the hub; give a file path")


def _redact(url: str) -> str:
    return re.sub(r"(://[^:/@]+):[^@]*@", r"\1:***@", url)
