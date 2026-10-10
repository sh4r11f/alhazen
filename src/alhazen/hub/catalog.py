"""Experiments, immutable versions, publication and the per-user library.

Hides: who may see which experiment fields and which version (one rule,
`_visible_version`, used by download, documentation, library and session
upload), the publication snapshot (review gate M2), and the package checks
an uploaded version passes before it is stored.

Visibility:
- The owner sees their live private metadata and every version.
- Everyone else sees an experiment only while it is published, and then only
  the snapshot frozen at the explicit publish plus that one version. Editing
  metadata or uploading a newer version changes nothing public until the
  owner publishes again.
- A miss and a private object answer the same 404.
- Every version lookup is scoped to its experiment (parent-child check).
"""

from __future__ import annotations

import importlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, and_, delete, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from alhazen.hub import packages
from alhazen.hub.auth import Principal, audit, has_hidden_characters
from alhazen.hub.context import Hub, new_id
from alhazen.hub.errors import HubError, conflict, invalid, not_found, too_large
from alhazen.hub.quota import lock_owner, require_room
from alhazen.hub.schema import experiments, library, publications, users, versions
from alhazen.hub.timefmt import iso

log = logging.getLogger(__name__)

_TAG = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_TEXT_LIMITS = {"title": 120, "summary": 280, "description": 20_000, "license": 100}
METADATA_FIELDS = ("title", "summary", "description", "license", "citations", "tags")


# -- metadata validation -----------------------------------------------------


def parse_metadata(body: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    unknown = sorted(set(body) - set(METADATA_FIELDS))
    if unknown:
        raise invalid(f"unknown field(s): {', '.join(unknown)}", "unknown_field")
    out: dict[str, Any] = {}
    for name, limit in _TEXT_LIMITS.items():
        if name not in body:
            if not partial and name == "title":
                raise invalid("title is required")
            continue
        value = body[name]
        if not isinstance(value, str):
            raise invalid(f"{name} must be a string")
        value = value.strip()
        if len(value) > limit or _bad_text(value, multiline=name == "description"):
            raise invalid(f"{name} must be at most {limit} printable characters")
        if name == "title" and not value:
            raise invalid("title must not be empty")
        out[name] = value
    if "citations" in body:
        citations = body["citations"]
        if (
            not isinstance(citations, list)
            or len(citations) > 50
            or not all(isinstance(c, str) and 0 < len(c.strip()) <= 1000 for c in citations)
            or any(_bad_text(c, multiline=False) for c in citations)
        ):
            raise invalid("citations must be a list of at most 50 strings of 1-1000 characters")
        out["citations"] = [c.strip() for c in citations]
    if "tags" in body:
        tags = body["tags"]
        if (
            not isinstance(tags, list)
            or len(tags) > 20
            or not all(isinstance(t, str) and _TAG.match(t) for t in tags)
        ):
            raise invalid("tags must be at most 20 lowercase slugs of up to 32 characters")
        out["tags"] = list(dict.fromkeys(tags))
    if not partial:
        out.setdefault("summary", "")
        out.setdefault("description", "")
        out.setdefault("license", "")
        out.setdefault("citations", [])
        out.setdefault("tags", [])
    return out


def _bad_text(value: str, *, multiline: bool) -> bool:
    # The display-name rule (controls, bidi/format and other invisible
    # characters, separators, lone surrogates): public metadata is read by
    # people and must show what it says (auth-review finding 7).
    return has_hidden_characters(value, multiline=multiline)


# -- shapes ------------------------------------------------------------------


def _owner(conn: Connection, owner_id: str) -> dict[str, Any]:
    row = conn.execute(
        select(users.c.id, users.c.username, users.c.display_name).where(users.c.id == owner_id)
    ).one()
    return {"id": row.id, "username": row.username, "display_name": row.display_name}


def _published_id(conn: Connection, experiment_id: str) -> str | None:
    return conn.execute(
        select(publications.c.version_id).where(publications.c.experiment_id == experiment_id)
    ).scalar_one_or_none()


def private_view(conn: Connection, row: Any) -> dict[str, Any]:
    count, latest = _version_stats(conn, row.id)
    return {
        "id": row.id,
        "title": row.title,
        "summary": row.summary,
        "description": row.description,
        "owner": _owner(conn, row.owner_id),
        "license": row.license,
        "citations": json.loads(row.citations),
        "tags": json.loads(row.tags),
        "published_version_id": _published_id(conn, row.id),
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
        "package_name": row.package_name,
        "version_count": count,
        "latest_version": latest,
    }


def public_view(experiment_row: Any, pub: Any) -> dict[str, Any]:
    return {
        "id": experiment_row.id,
        "title": pub.title,
        "summary": pub.summary,
        "description": pub.description,
        "owner": {
            "id": experiment_row.owner_id,
            "username": pub.owner_username,
            "display_name": pub.owner_display_name,
        },
        "license": pub.license,
        "citations": json.loads(pub.citations),
        "tags": json.loads(pub.tags),
        "published_version_id": pub.version_id,
        "published_at": iso(pub.published_at),
        "created_at": iso(experiment_row.created_at),
        "package_name": experiment_row.package_name,
    }


def version_view(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "experiment_id": row.experiment_id,
        "version": row.version,
        "sha256": row.sha256,
        "size": int(row.size),
        "manifest": json.loads(row.manifest),
        "created_at": iso(row.created_at),
    }


def owner_version_view(row: Any) -> dict[str, Any]:
    """A version as its owner sees it: also whether it came from an accepted
    AI draft (schema 3). Visitors never see this."""
    view = version_view(row)
    if row.ai_draft_id is not None:
        # Also under manifest for the interface, in this response only: the
        # stored package manifest has no such field (its format refuses
        # unknown fields); the package carries docs/ai-provenance.json.
        view["manifest"] = {**view["manifest"], "ai_assisted": True}
    return {**view, "ai_assisted": row.ai_draft_id is not None, "ai_draft_id": row.ai_draft_id}


def _version_stats(conn: Connection, experiment_id: str) -> tuple[int, str | None]:
    count = conn.execute(
        select(func.count()).select_from(versions).where(versions.c.experiment_id == experiment_id)
    ).scalar_one()
    latest = conn.execute(
        select(versions.c.version)
        .where(versions.c.experiment_id == experiment_id)
        .order_by(versions.c.created_at.desc(), versions.c.id)
        .limit(1)
    ).scalar_one_or_none()
    return int(count), latest


# -- experiments -------------------------------------------------------------


def create_experiment(hub: Hub, principal: Principal, body: dict[str, Any]) -> dict[str, Any]:
    fields = parse_metadata(body, partial=False)
    now = hub.clock()
    with hub.db.transaction() as conn:
        experiment_id = insert_experiment(
            conn, principal.user_id, fields, now, hub.settings.limits.max_experiments_per_owner
        )
        row = conn.execute(select(experiments).where(experiments.c.id == experiment_id)).one()
        return {"experiment": private_view(conn, row)}


def insert_experiment(
    conn: Connection, owner_id: str, fields: dict[str, Any], now: int, max_experiments: int
) -> str:
    """Create one private experiment from parsed metadata inside the caller's
    transaction (the owner's experiment limit is checked under the owner
    lock); returns its id. Shared by POST /experiments and AI draft acceptance."""
    lock_owner(conn, owner_id)
    owned = conn.execute(
        select(func.count()).select_from(experiments).where(experiments.c.owner_id == owner_id)
    ).scalar_one()
    if owned >= max_experiments:
        raise too_large(
            f"You already own {owned} experiments, the limit for one account",
            "experiment_limit",
        )
    experiment_id = new_id()
    conn.execute(
        insert(experiments).values(
            id=experiment_id,
            owner_id=owner_id,
            title=fields["title"],
            summary=fields["summary"],
            description=fields["description"],
            license=fields["license"],
            citations=json.dumps(fields["citations"]),
            tags=json.dumps(fields["tags"]),
            package_name=None,
            created_at=now,
            updated_at=now,
        )
    )
    audit(conn, now, f"user:{owner_id}", "experiment.create", experiment_id, {})
    return experiment_id


def list_own(hub: Hub, principal: Principal, limit: int, offset: int) -> dict[str, Any]:
    with hub.db.transaction() as conn:
        rows = conn.execute(
            select(experiments)
            .where(experiments.c.owner_id == principal.user_id)
            .order_by(experiments.c.created_at.desc(), experiments.c.id)
            .limit(limit + 1)
            .offset(offset)
        ).all()
        items = [private_view(conn, row) for row in rows[:limit]]
    return {"items": items, "next_offset": offset + limit if len(rows) > limit else None}


def _owned(
    conn: Connection, principal: Principal | None, experiment_id: str, *, lock: bool = False
) -> Any:
    if principal is None:
        raise not_found("Experiment not found")
    query = select(experiments).where(
        experiments.c.id == experiment_id, experiments.c.owner_id == principal.user_id
    )
    if lock:
        query = query.with_for_update()
    row = conn.execute(query).first()
    if row is None:
        raise not_found("Experiment not found")
    return row


def get_experiment(hub: Hub, principal: Principal | None, experiment_id: str) -> dict[str, Any]:
    with hub.db.transaction() as conn:
        row = conn.execute(select(experiments).where(experiments.c.id == experiment_id)).first()
        if row is None:
            raise not_found("Experiment not found")
        pub = conn.execute(
            select(publications).where(publications.c.experiment_id == experiment_id)
        ).first()
        if principal is not None and row.owner_id == principal.user_id:
            all_versions = conn.execute(
                select(versions)
                .where(versions.c.experiment_id == experiment_id)
                .order_by(versions.c.created_at.desc(), versions.c.id)
            ).all()
            return {
                "experiment": private_view(conn, row),
                "versions": [owner_version_view(v) for v in all_versions],
                "publication": public_view(row, pub) if pub is not None else None,
                "can_edit": True,
            }
        if pub is None:
            raise not_found("Experiment not found")
        published = conn.execute(
            select(versions).where(
                versions.c.id == pub.version_id, versions.c.experiment_id == experiment_id
            )
        ).one()
        return {
            "experiment": public_view(row, pub),
            "versions": [version_view(published)],
            "can_edit": False,
        }


def patch_experiment(
    hub: Hub, principal: Principal, experiment_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    fields = parse_metadata(body, partial=True)
    if not fields:
        raise invalid("nothing to change")
    now = hub.clock()
    values: dict[str, Any] = {k: v for k, v in fields.items() if k not in ("citations", "tags")}
    if "citations" in fields:
        values["citations"] = json.dumps(fields["citations"])
    if "tags" in fields:
        values["tags"] = json.dumps(fields["tags"])
    values["updated_at"] = now
    with hub.db.transaction() as conn:
        _owned(conn, principal, experiment_id, lock=True)
        conn.execute(update(experiments).where(experiments.c.id == experiment_id).values(**values))
        audit(
            conn,
            now,
            f"user:{principal.user_id}",
            "experiment.edit",
            experiment_id,
            {"fields": sorted(fields)},
        )
        row = conn.execute(select(experiments).where(experiments.c.id == experiment_id)).one()
        return {"experiment": private_view(conn, row)}


# -- versions ----------------------------------------------------------------


def precheck_version_upload(hub: Hub, principal: Principal, experiment_id: str) -> None:
    """Cheap refusals before any bytes are received."""
    with hub.db.transaction() as conn:
        _owned(conn, principal, experiment_id)
        count = conn.execute(
            select(func.count())
            .select_from(versions)
            .where(versions.c.experiment_id == experiment_id)
        ).scalar_one()
        if count >= hub.settings.limits.max_versions_per_experiment:
            raise too_large("This experiment has reached its version limit", "version_limit")
        require_room(conn, principal.user_id, 1, hub.settings.limits.user_quota_bytes)


def accept_version(
    hub: Hub,
    principal: Principal,
    experiment_id: str,
    temp: Path,
    sha256: str,
    size: int,
    *,
    ai_draft_id: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Validate a received package and store it as a new immutable version.

    Returns (version, created). An identical re-upload of an existing version
    (same version string, same bytes) returns the stored one, so a client may
    retry after a lost response. ``temp`` is consumed or removed.

    Ordering: the file is installed before the row commits; if the commit
    fails the installed file is removed, so no row ever points at a missing
    file and a stray file is never served (only a row makes it reachable).
    """
    version_id = new_id()
    key = hub.store.release_key(experiment_id, version_id)
    installed = False
    staged: Path | None = None
    try:
        bundle = _stage(hub, temp, sha256)
        staged = bundle.path
        info = bundle.info
        if info.sha256 != sha256 or info.size != size:
            raise HubError(500, "internal", "Package digest disagreement; nothing was stored")
        manifest = info.manifest
        check_documentation(staged, manifest)
        name = str(manifest.get("name", ""))
        version = str(manifest.get("version", ""))
        now = hub.clock()
        with hub.db.transaction() as conn:
            lock_owner(conn, principal.user_id)
            row = _owned(conn, principal, experiment_id, lock=True)
            existing = conn.execute(
                select(versions).where(
                    versions.c.experiment_id == experiment_id, versions.c.version == version
                )
            ).first()
            if existing is not None:
                if existing.sha256 == info.sha256:
                    return version_view(existing), False
                raise conflict(
                    "version_exists",
                    f"Version {version} already exists with different content; versions are "
                    "immutable, so upload a new version number",
                )
            if row.package_name is not None and row.package_name != name:
                raise conflict(
                    "package_name_mismatch",
                    f"This experiment's packages are named {row.package_name!r}, not {name!r}",
                )
            count = conn.execute(
                select(func.count())
                .select_from(versions)
                .where(versions.c.experiment_id == experiment_id)
            ).scalar_one()
            if count >= hub.settings.limits.max_versions_per_experiment:
                raise too_large("This experiment has reached its version limit", "version_limit")
            require_room(conn, principal.user_id, info.size, hub.settings.limits.user_quota_bytes)
            hub.store.install_release(staged, key)
            installed = True
            conn.execute(
                insert(versions).values(
                    id=version_id,
                    experiment_id=experiment_id,
                    version=version,
                    sha256=info.sha256,
                    size=info.size,
                    manifest=json.dumps(manifest, sort_keys=True),
                    storage_key=key,
                    created_at=now,
                    ai_draft_id=ai_draft_id,
                )
            )
            if row.package_name is None:
                conn.execute(
                    update(experiments)
                    .where(experiments.c.id == experiment_id)
                    .values(package_name=name, updated_at=now)
                )
            audit(
                conn,
                now,
                f"user:{principal.user_id}",
                "version.upload",
                version_id,
                {"experiment": experiment_id, "version": version, "sha256": info.sha256},
            )
            stored = conn.execute(select(versions).where(versions.c.id == version_id)).one()
        return version_view(stored), True
    except BaseException as exc:
        if installed:
            hub.store.remove_release(key)
        if isinstance(exc, IntegrityError):
            raise conflict(
                "version_exists", "That version was uploaded concurrently; reload and retry"
            ) from None
        raise
    finally:
        temp.unlink(missing_ok=True)
        if staged is not None:
            staged.unlink(missing_ok=True)


def _stage(hub: Hub, temp: Path, sha256: str) -> packages.VerifiedBundle:
    """A private verified copy of the received upload: what gets stored is
    exactly the bytes that were checked (packages.stage_bundle), and they must
    be the bytes this request streamed (``sha256``)."""
    limits = hub.settings.limits
    try:
        return packages.stage_bundle(
            temp,
            hub.store.root / "tmp",
            expected_sha256=sha256,
            max_archive_bytes=limits.max_package_bytes,
            max_expanded_bytes=limits.max_package_expanded_bytes,
            max_files=limits.max_package_files,
        )
    except packages.PackageError as exc:
        message = str(exc).replace(str(temp), "<package>").replace(str(hub.store.root), "<archive>")
        raise HubError(422, "invalid_package", message) from None


def documentation_module() -> Any:
    """The documentation reader, or a 503 while this build lacks it.

    Imported on use: the module is developed alongside the service, and a
    hub without it refuses documented uploads rather than accepting them
    unchecked.
    """
    try:
        return importlib.import_module("alhazen.hub.documentation")
    except ImportError:
        raise HubError(
            503,
            "documentation_unavailable",
            "This hub cannot read experiment documentation in this build",
        ) from None


def check_documentation(temp: Path, manifest: dict[str, Any]) -> None:
    """422 invalid_documentation unless the package's documentation reads."""
    if not manifest.get("documentation"):
        return
    documentation = documentation_module()
    try:
        documentation.read_documentation(temp, manifest)
    except documentation.DocumentationError as exc:
        raise HubError(422, "invalid_documentation", str(exc)) from None


def _visible_version(
    conn: Connection, principal: Principal | None, experiment_id: str, version_id: str
) -> tuple[Any, Any]:
    """(experiment row, version row) the caller may download, or a 404."""
    viewer = principal.user_id if principal is not None else None
    return _visible_to(conn, viewer, experiment_id, version_id)


def _visible_to(
    conn: Connection, viewer_id: str | None, experiment_id: str, version_id: str
) -> tuple[Any, Any]:
    exp = conn.execute(select(experiments).where(experiments.c.id == experiment_id)).first()
    ver = conn.execute(
        select(versions).where(
            versions.c.id == version_id, versions.c.experiment_id == experiment_id
        )
    ).first()
    if exp is None or ver is None:
        raise not_found("Version not found")
    if viewer_id is not None and exp.owner_id == viewer_id:
        return exp, ver
    if _published_id(conn, experiment_id) == version_id:
        return exp, ver
    raise not_found("Version not found")


def readable_version(
    conn: Connection, user_id: str, experiment_id: str, version_id: str
) -> tuple[Any, Any]:
    """(experiment row, version row) if ``user_id`` may read that version
    (owner, or the published version), else 404: the download rule, for
    callers that hold a user id rather than a request principal."""
    return _visible_to(conn, user_id, experiment_id, version_id)


def version_download(
    hub: Hub, principal: Principal | None, experiment_id: str, version_id: str
) -> tuple[Path, str, str]:
    """(file, download name, sha256) for an authorized version download."""
    with hub.db.transaction() as conn:
        exp, ver = _visible_version(conn, principal, experiment_id, version_id)
    name = exp.package_name or "experiment"
    return hub.store.path(ver.storage_key), f"{name}-{ver.version}.zip", ver.sha256


def version_documentation(
    hub: Hub, principal: Principal | None, experiment_id: str, version_id: str
) -> dict[str, Any]:
    """The resolved documentation of a version, under the download's ACL."""
    with hub.db.transaction() as conn:
        _exp, ver = _visible_version(conn, principal, experiment_id, version_id)
    manifest = json.loads(ver.manifest)
    if not manifest.get("documentation"):
        return {"documentation": None}
    documentation = documentation_module()
    try:
        resolved = documentation.read_documentation(hub.store.path(ver.storage_key), manifest)
    except documentation.DocumentationError as exc:
        # It validated on upload; failing now means the stored file changed.
        log.error("version %s: stored documentation no longer validates: %s", version_id, exc)
        raise HubError(
            500, "documentation_unreadable", "This version's stored documentation is unreadable"
        ) from None
    return {"documentation": resolved}


# -- publication -------------------------------------------------------------


def publish(
    hub: Hub, principal: Principal, experiment_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    unknown = sorted(set(body) - {"version_id", "license_ack", "data_excluded_ack"})
    if unknown:
        raise invalid(f"unknown field(s): {', '.join(unknown)}", "unknown_field")
    if body.get("license_ack") is not True or body.get("data_excluded_ack") is not True:
        raise invalid(
            "Publishing needs license_ack and data_excluded_ack set to true: the licence applies "
            "to the public code and the package holds no participant data or private configuration",
            "acknowledgement_required",
        )
    version_id = body.get("version_id")
    if not isinstance(version_id, str):
        raise invalid("version_id is required")
    now = hub.clock()
    with hub.db.transaction() as conn:
        row = _owned(conn, principal, experiment_id, lock=True)
        ver = conn.execute(
            select(versions).where(
                versions.c.id == version_id, versions.c.experiment_id == experiment_id
            )
        ).first()
        if ver is None:
            raise not_found("Version not found")
        manifest_license = str(json.loads(ver.manifest).get("license", "")).strip()
        if not row.license:
            raise invalid("Set the experiment's licence before publishing", "license_required")
        if manifest_license != row.license:
            raise conflict(
                "license_mismatch",
                f"The experiment's licence ({row.license!r}) differs from the one this version's "
                f"package declares ({manifest_license!r}); make them agree before publishing",
            )
        owner = _owner(conn, row.owner_id)
        conn.execute(delete(publications).where(publications.c.experiment_id == experiment_id))
        conn.execute(
            insert(publications).values(
                experiment_id=experiment_id,
                version_id=version_id,
                title=row.title,
                summary=row.summary,
                description=row.description,
                license=row.license,
                citations=row.citations,
                tags=row.tags,
                owner_username=owner["username"],
                owner_display_name=owner["display_name"],
                published_at=now,
            )
        )
        audit(
            conn,
            now,
            f"user:{principal.user_id}",
            "experiment.publish",
            experiment_id,
            {"version": version_id, "license_ack": True, "data_excluded_ack": True},
        )
        return {"experiment": private_view(conn, row)}


def unpublish(hub: Hub, principal: Principal, experiment_id: str) -> dict[str, Any]:
    now = hub.clock()
    with hub.db.transaction() as conn:
        row = _owned(conn, principal, experiment_id, lock=True)
        removed = conn.execute(
            delete(publications).where(publications.c.experiment_id == experiment_id)
        ).rowcount
        if removed:
            audit(conn, now, f"user:{principal.user_id}", "experiment.unpublish", experiment_id, {})
        return {"experiment": private_view(conn, row)}


def catalog(hub: Hub, query: str, limit: int, offset: int) -> dict[str, Any]:
    if len(query) > 200:
        raise invalid("query is too long")
    with hub.db.transaction() as conn:
        stmt = (
            select(experiments, publications, versions)
            .select_from(
                publications.join(
                    experiments, experiments.c.id == publications.c.experiment_id
                ).join(
                    versions,
                    and_(
                        versions.c.id == publications.c.version_id,
                        versions.c.experiment_id == publications.c.experiment_id,
                    ),
                )
            )
            .order_by(publications.c.published_at.desc(), publications.c.experiment_id)
        )
        words = [w for w in query.lower().split() if w][:8]
        for word in words:
            pattern = "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            stmt = stmt.where(
                or_(
                    func.lower(publications.c.title).like(pattern, escape="\\"),
                    func.lower(publications.c.summary).like(pattern, escape="\\"),
                    func.lower(publications.c.tags).like(pattern, escape="\\"),
                    func.lower(publications.c.owner_username).like(pattern, escape="\\"),
                )
            )
        rows = conn.execute(stmt.limit(limit + 1).offset(offset)).all()
    items = []
    for row in rows[:limit]:
        exp = _Cols(row, experiments, "")
        pub = _Cols(row, publications, "")
        ver = _Cols(row, versions, "")
        items.append({"experiment": public_view(exp, pub), "version": version_view(ver)})
    return {"items": items, "next_offset": offset + limit if len(rows) > limit else None}


class _Cols:
    """Attribute access to one table's columns in a joined row."""

    def __init__(self, row: Any, table: Any, _prefix: str) -> None:
        self._row = row
        self._table = table

    def __getattr__(self, name: str) -> Any:
        return self._row._mapping[self._table.c[name]]


# -- library -----------------------------------------------------------------


def library_add(hub: Hub, principal: Principal, body: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(body) - {"experiment_id", "version_id"})
    if unknown:
        raise invalid(f"unknown field(s): {', '.join(unknown)}", "unknown_field")
    experiment_id, version_id = body.get("experiment_id"), body.get("version_id")
    if not isinstance(experiment_id, str) or not isinstance(version_id, str):
        raise invalid("experiment_id and version_id are required")
    now = hub.clock()
    with hub.db.transaction() as conn:
        exp, ver = _visible_version(conn, principal, experiment_id, version_id)
        view = _library_experiment(conn, principal, exp, None)
        conn.execute(
            delete(library).where(
                library.c.user_id == principal.user_id, library.c.experiment_id == experiment_id
            )
        )
        conn.execute(
            insert(library).values(
                user_id=principal.user_id,
                experiment_id=experiment_id,
                version_id=version_id,
                title=view["title"],
                added_at=now,
            )
        )
        return {
            "experiment": view,
            "version": version_view(ver),
            "added_at": iso(now),
            "available": True,
        }


def library_remove(hub: Hub, principal: Principal, experiment_id: str) -> bool:
    """Unpin an experiment from the caller's library; True if it was there.
    Removes only the caller's pin: the experiment and its versions are untouched."""
    with hub.db.transaction() as conn:
        removed = conn.execute(
            delete(library).where(
                library.c.user_id == principal.user_id, library.c.experiment_id == experiment_id
            )
        ).rowcount
    return bool(removed)


def library_list(hub: Hub, principal: Principal, limit: int, offset: int) -> dict[str, Any]:
    with hub.db.transaction() as conn:
        rows = conn.execute(
            select(library)
            .where(library.c.user_id == principal.user_id)
            .order_by(library.c.added_at.desc(), library.c.experiment_id)
            .limit(limit + 1)
            .offset(offset)
        ).all()
        items = []
        for item in rows[:limit]:
            exp = conn.execute(
                select(experiments).where(experiments.c.id == item.experiment_id)
            ).one()
            ver = conn.execute(
                select(versions).where(
                    versions.c.id == item.version_id, versions.c.experiment_id == item.experiment_id
                )
            ).one()
            owner = exp.owner_id == principal.user_id
            available = owner or _published_id(conn, exp.id) == item.version_id
            items.append(
                {
                    "experiment": _library_experiment(conn, principal, exp, item.title),
                    "version": version_view(ver),
                    "added_at": iso(item.added_at),
                    "available": available,
                }
            )
    return {"items": items, "next_offset": offset + limit if len(rows) > limit else None}


def _library_experiment(
    conn: Connection, principal: Principal, exp: Any, saved_title: str | None
) -> dict[str, Any]:
    if exp.owner_id == principal.user_id:
        return private_view(conn, exp)
    pub = conn.execute(select(publications).where(publications.c.experiment_id == exp.id)).first()
    if pub is not None:
        return public_view(exp, pub)
    # No longer public: only what the user saw when pinning it.
    return {"id": exp.id, "title": saved_title or "", "published_version_id": None}


def may_collect_with(
    conn: Connection, principal: Principal, experiment_id: str, version_id: str
) -> None:
    """A caller may upload data recorded with a version they own, that is
    published, or that they pinned in their library (an install that later
    stopped being public still produced their data)."""
    try:
        _visible_version(conn, principal, experiment_id, version_id)
        return
    except HubError:
        pinned = conn.execute(
            select(library.c.version_id).where(
                library.c.user_id == principal.user_id,
                library.c.experiment_id == experiment_id,
                library.c.version_id == version_id,
            )
        ).first()
        if pinned is None:
            raise not_found("Version not found") from None
