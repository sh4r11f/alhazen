"""Installing a hub release on this rig: verified, versioned, trusted per digest.

Secret it hides: where releases are unpacked, how an install is recorded, and
the order that keeps unreviewed code from running —

1. the release the operator reviewed (experiment, version, SHA-256) must be
   the one the hub lists;
2. the download must hash to that SHA-256 and pass the shared package
   validation (``alhazen.hub.packages``; nothing is re-checked here);
3. it is unpacked into a NEW folder ``experiments/<name>/<version>-<sha12>``
   (an existing folder is never replaced) and its declared files are made
   read-only;
4. the trust acknowledgement is recorded for that exact digest;
5. only then may a caller probe an interpreter or register it with the
   workspace (:meth:`InstallStore.trusted_record` is the gate) — both of
   which import code from the folder.

What callers must NOT rely on: any safety property of the code itself. Hash
and archive checks prove the bytes are the published ones, not that they are
harmless; trusted code runs as the operator's OS user.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from alhazen.data.atomic import replace_atomically
from alhazen.hub.client import HubClient, HubError, api_path

INSTALLS_FILE = "installs.json"
# The package cap of the v1 contract (alhazen.hub.packages.inspect_bundle's
# default); the hub's own limit may be lower, never higher.
MAX_RELEASE_BYTES = 256 * 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}")
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}")
# Shown before the operator trusts a release, and recorded with the trust.
TRUST_STATEMENT = (
    "Installing runs this experiment's code on this computer as your user account. "
    "Trusted code can read and change your files, use your saved credentials and control "
    "connected devices; a virtual environment is not a sandbox. The checks prove the files "
    "are exactly the published release (SHA-256), not that the code is safe. Trust applies "
    "to this exact release only."
)


class PackageModule(Protocol):
    """What this module uses of ``alhazen.hub.packages`` (the shared contract)."""

    PackageError: type[Exception]

    def inspect_bundle(self, path: Path) -> Any: ...

    def compatibility_problems(
        self,
        manifest: Any,
        *,
        python_version: tuple[int, int] | None = None,
        alhazen_version: str | None = None,
        platform: str | None = None,
    ) -> list[str]: ...

    def extract_bundle(self, path: Path, destination: Path) -> Any: ...


class InstallError(ValueError):
    """An install refused, with the HTTP status and code the page shows."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check_identifier(value: Any, what: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise InstallError(400, "invalid_request", f"{what} must be a hub identifier")
    return value


def check_sha256(value: Any) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise InstallError(400, "invalid_request", "sha256 must be 64 lowercase hex digits")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def _summary(manifest: dict[str, Any]) -> dict[str, Any]:
    """The manifest fields a record keeps for the page, as plain data."""
    raw = manifest.get("hardware")
    hardware: dict[str, Any] = raw if isinstance(raw, dict) else {}
    return {
        "name": str(manifest.get("name", "")),
        "version": str(manifest.get("version", "")),
        "title": str(manifest.get("title", "")),
        "license": str(manifest.get("license", "")),
        "citations": [str(c) for c in manifest.get("citations", []) if isinstance(c, str)],
        "hardware": {k: bool(hardware.get(k)) for k in ("display", "eye_tracker", "reward")},
        "python_min": str(manifest.get("python_min", "")),
        "alhazen_min": str(manifest.get("alhazen_min", "")),
        "platforms": [str(p) for p in manifest.get("platforms", []) if isinstance(p, str)],
    }


class InstallStore:
    """Installed releases of one workspace, keyed by their ZIP's SHA-256.

    ``directory`` is the workspace's ``hub/`` folder; releases go under its
    ``experiments/`` and the records into ``installs.json``."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.root = directory / "experiments"
        self.downloads = directory / "downloads"
        self._lock = threading.RLock()
        # One install at a time: a second request waits rather than racing
        # the first into the same folder.
        self.install_lock = threading.Lock()

    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            path = self.directory / INSTALLS_FILE
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return []
            except (OSError, ValueError) as exc:
                raise ValueError(f"The install records cannot be read: {exc}") from exc
            if not isinstance(value, list):
                raise ValueError("The install records are not a list")
            return [r for r in value if isinstance(r, dict)]

    def record(self, sha256: str) -> dict[str, Any] | None:
        return next((r for r in self.records() if r.get("sha256") == sha256), None)

    def for_path(self, path: str) -> dict[str, Any] | None:
        """The release unpacked at ``path`` (a registered project's folder)."""
        try:
            resolved = str(Path(path).resolve())
        except OSError:
            return None
        return next((r for r in self.records() if r.get("path") == resolved), None)

    def _save(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            records = [r for r in self.records() if r.get("sha256") != record["sha256"]]
            records.append(record)
            self.directory.mkdir(parents=True, exist_ok=True)
            replace_atomically(self.directory / INSTALLS_FILE, json.dumps(records, indent=2))
        return record

    def update(self, sha256: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            record = self.record(sha256)
            if record is None:
                raise InstallError(404, "not_found", "That release is not installed here")
            record.update(fields)
            return self._save(record)

    def trusted_record(self, sha256: str) -> dict[str, Any]:
        """The record of an installed release whose code the operator trusted
        for exactly this digest, and whose folder is still there. The only
        way to the folder for anything that may import from it."""
        record = self.record(sha256)
        trust = (record or {}).get("trust") or {}
        if record is None or trust.get("sha256") != sha256 or record.get("sha256") != sha256:
            raise InstallError(409, "not_trusted", "This release's code has not been trusted")
        if not (Path(record["path"]) / record.get("entrypoint", "run.py")).is_file():
            raise InstallError(
                409, "install_missing", "The installed release's files are no longer there"
            )
        return record

    def verify(self, record: dict[str, Any]) -> list[str]:
        """Declared files of an install that are missing or changed since it
        was unpacked (by SHA-256). Files the experiment added (data, caches)
        are not its concern."""
        root = Path(record["path"])
        problems = []
        for entry in record.get("files", []):
            path = root / entry["path"]
            if not path.is_file() or path.is_symlink():
                problems.append(f"missing: {entry['path']}")
            elif path.stat().st_size != entry["size"] or sha256_file(path) != entry["sha256"]:
                problems.append(f"changed: {entry['path']}")
        return problems

    # -- install ------------------------------------------------------------------------

    def install(
        self,
        client: HubClient,
        packages: PackageModule,
        *,
        experiment_id: str,
        version_id: str,
        sha256: str,
        trust_code: Any,
        trusted_by: dict[str, Any],
        platform: str = sys.platform,
    ) -> dict[str, Any]:
        """Download, verify, unpack and trust one release (steps 1-4 of the
        module docstring). Idempotent: an already-installed digest is
        returned as it is, with no download."""
        check_identifier(experiment_id, "experiment_id")
        check_identifier(version_id, "version_id")
        check_sha256(sha256)
        if trust_code is not True:
            raise InstallError(
                400, "trust_required", "Confirm that you trust this release's code to install it"
            )
        with self.install_lock:
            existing = self.record(sha256)
            if existing is not None and (Path(existing["path"]) / "run.py").is_file():
                if existing.get("experiment_id") != experiment_id or (
                    existing.get("version_id") != version_id
                ):
                    raise InstallError(
                        409,
                        "conflict",
                        "This exact release is already installed under another hub record",
                    )
                return existing
            version = self._listed_version(client, experiment_id, version_id, sha256)
            declared = version.get("size")
            if not isinstance(declared, int) or declared <= 0 or declared > MAX_RELEASE_BYTES:
                raise InstallError(413, "too_large", "The hub lists no valid size for this release")
            self.downloads.mkdir(parents=True, exist_ok=True)
            bundle = self.downloads / f"{sha256}.{secrets.token_hex(4)}.zip"
            try:
                size, _ = client.download(
                    api_path("experiments", experiment_id, "versions", version_id, "download"),
                    bundle,
                    max_bytes=declared,
                    expected_sha256=sha256,
                )
                if size != declared:
                    raise InstallError(
                        409, "hash_mismatch", "The download's size differs from the hub's record"
                    )
                try:
                    info = packages.inspect_bundle(bundle)
                except packages.PackageError as exc:
                    raise InstallError(
                        422, "invalid_package", f"The release is not a valid package: {exc}"
                    ) from exc
                manifest = dict(info.manifest)
                self._check_matches(info, manifest, version, sha256, platform)
                destination = self.root / manifest["name"] / f"{manifest['version']}-{sha256[:12]}"
                if destination.exists():
                    raise InstallError(
                        409,
                        "conflict",
                        "A folder for this release already exists but is not recorded as an "
                        "install; it was left untouched",
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    packages.extract_bundle(bundle, destination)
                except packages.PackageError as exc:
                    raise InstallError(
                        422, "invalid_package", f"The release could not be unpacked: {exc}"
                    ) from exc
            finally:
                bundle.unlink(missing_ok=True)
            files = [
                {"path": str(f["path"]), "size": int(f["size"]), "sha256": str(f["sha256"])}
                for f in manifest.get("files", [])
            ]
            _make_read_only(destination, files)
            record = {
                "sha256": sha256,
                "experiment_id": experiment_id,
                "version_id": version_id,
                "base_url": client.base,
                "path": str(destination.resolve()),
                "entrypoint": str(manifest.get("entrypoint", "run.py")),
                **_summary(manifest),
                "files": files,
                "documentation": manifest.get("documentation"),
                "installed_at": now(),
                "installed_by": _public_user(trusted_by),
                "trust": {
                    "sha256": sha256,
                    "at": now(),
                    "by": _public_user(trusted_by),
                    "statement": TRUST_STATEMENT,
                },
                "status": "installed",
                "project_id": None,
                "python": None,
                "error": None,
            }
            return self._save(record)

    @staticmethod
    def _listed_version(
        client: HubClient, experiment_id: str, version_id: str, sha256: str
    ) -> dict[str, Any]:
        detail = client.json("GET", api_path("experiments", experiment_id))
        versions = detail.get("versions") if isinstance(detail, dict) else None
        if not isinstance(versions, list):
            raise HubError(502, "hub_bad_response", "The hub's experiment detail has no versions")
        version = next(
            (v for v in versions if isinstance(v, dict) and v.get("id") == version_id), None
        )
        if version is None or version.get("experiment_id", experiment_id) != experiment_id:
            raise InstallError(404, "not_found", "That release is not available to you")
        if version.get("sha256") != sha256:
            raise InstallError(
                409,
                "hash_mismatch",
                "The hub lists a different SHA-256 for this release than the one you reviewed; "
                "nothing was installed",
            )
        return version

    @staticmethod
    def _check_matches(
        info: Any,
        manifest: dict[str, Any],
        version: dict[str, Any],
        sha256: str,
        platform: str,
    ) -> None:
        if info.sha256 != sha256:
            raise InstallError(409, "hash_mismatch", "The package's digest differs")
        # inspect_bundle has validated the manifest (name a slug, version
        # MAJOR.MINOR.PATCH, entry point run.py), so both are path-safe here.
        name, number = manifest["name"], manifest["version"]
        raw = version.get("manifest")
        listed: dict[str, Any] = raw if isinstance(raw, dict) else {}
        if version.get("version", number) != number or listed.get("name", name) != name:
            raise InstallError(
                409, "conflict", "The package's name or version differs from the hub's record"
            )
        platforms = manifest.get("platforms")
        if isinstance(platforms, list) and platform not in platforms:
            raise InstallError(
                409,
                "unsupported_platform",
                f"This release supports {', '.join(map(str, platforms))}, not {platform}",
            )


def _public_user(user: dict[str, Any]) -> dict[str, str]:
    return {k: str(user.get(k, "")) for k in ("id", "username", "display_name")}


def _make_read_only(root: Path, files: list[dict[str, Any]]) -> None:
    """Declared files become read-only, so an edit in place is refused rather
    than silently changing what the release's digest names. Folders stay
    writable: an experiment may write data or caches beside its code."""
    mask = ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    for entry in files:
        path = root / entry["path"]
        os.chmod(path, stat.S_IMODE(path.stat().st_mode) & mask)
