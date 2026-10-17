"""The platform's rules, independent of any transport.

``PlatformService`` is the only place that decides who may read or change what; the HTTP
layer translates requests into these calls and errors into status codes. Every method
takes the acting ``Principal`` (or None for an anonymous visitor).

- The catalogue (listings, their versions, documentation and published bundles) is
  readable by anyone allowed to read the catalogue (``public_catalogue``, or any signed-in
  principal).
- A user's copies (forks), their revisions and documentation snapshots are visible to
  that user only; another user's fork id is reported missing, never forbidden.
- Publishing writes only under the acting principal's own id, with the name the host
  vouches for, never one taken from the request.

Every read-modify-write runs under the store lock, so concurrent requests (and a refresh
run from the command line) cannot interleave.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from alhazen.experiment_platform import bundles, catalog
from alhazen.experiment_platform.errors import Conflict, Forbidden, InvalidRequest, NotFound
from alhazen.experiment_platform.safety import check_id, is_id
from alhazen.experiment_platform.store import FileStore

SUBJECTS = ("human", "monkey")
MAX_MESSAGE = 500
MAX_TITLE = 200
MAX_SUMMARY = 4000


@dataclass(frozen=True)
class Principal:
    uid: str
    name: str


def _now() -> int:
    return int(time.time() * 1000)


def _slug(text: str) -> str:
    return (
        re.sub(r"[^a-z0-9]+", "-", str(text or "experiment").lower()).strip("-")[:40]
        or "experiment"
    )


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out or "0"


def listing_handle(publisher: str, exp: str, bundle_id: str) -> str:
    return f"L~{publisher}~{exp}~{bundle_id}"


def fork_handle(fork_id: str, bundle_id: str) -> str:
    return f"F~{fork_id}~{bundle_id}"


def _version_entry(listing: Mapping[str, Any], wanted: str) -> tuple[str, dict[str, Any] | None]:
    """(version key, entry) of a listing's history for a version key or a bundle id. A
    bundle id may have several versions (documentation refreshed); the latest is meant."""
    versions = listing.get("versions") or {}
    if isinstance(versions.get(wanted), dict):
        return wanted, versions[wanted]
    matches = [
        (k, v)
        for k, v in versions.items()
        if isinstance(v, dict) and (v.get("bundle") or k) == wanted
    ]
    if not matches:
        return wanted, None
    return max(matches, key=lambda kv: kv[1].get("at") or 0)


class PlatformService:
    """The platform over one FileStore."""

    def __init__(self, store: FileStore, *, public_catalogue: bool = False) -> None:
        self.store = store
        self.public_catalogue = public_catalogue

    # ------------------------------------------------------------------ access
    def _reader(self, principal: Principal | None) -> None:
        if principal is None and not self.public_catalogue:
            raise Forbidden("Sign in to see the catalogue")

    def _writer(self, principal: Principal | None) -> Principal:
        if principal is None:
            raise Forbidden("Sign in to keep copies of experiments")
        return principal

    # ------------------------------------------------------------------ catalogue
    def _present(self, publisher: str, exp: str, listing: Mapping[str, Any]) -> dict[str, Any]:
        out = dict(listing)
        bundle = dict(out.get("bundle") or {})
        if bundle.get("id"):
            bundle["path"] = listing_handle(publisher, exp, bundle["id"])
        out["bundle"] = bundle
        key = out.get("versionKey") or bundle.get("id")
        version = self.store.version(publisher, exp, key) if key else None
        out["docsStatus"] = (
            catalog.docs_status(out, (version or {}).get("docs"))
            if out.get("source", {}).get("kind") == "repo"
            else "partial"
        )
        return out

    def catalogue(self, principal: Principal | None) -> dict[str, dict[str, dict[str, Any]]]:
        self._reader(principal)
        return {
            pub: {exp: self._present(pub, exp, listing) for exp, listing in by.items()}
            for pub, by in self.store.listings().items()
        }

    def _listing(self, publisher: str, exp: str) -> dict[str, Any]:
        listing = self.store.listing(check_id(publisher, "Publisher"), check_id(exp, "Experiment"))
        if listing is None:
            raise NotFound("This experiment is not in the catalogue")
        return listing

    def docs(
        self, principal: Principal | None, publisher: str, exp: str, bundle_id: str | None = None
    ) -> dict[str, Any]:
        """The documentation record of the listing's current version, or of the version
        ``bundle_id`` names: a version key, or a bundle id (its latest version)."""
        self._reader(principal)
        listing = self._listing(publisher, exp)
        if bundle_id is None:
            key = listing.get("versionKey") or listing["bundle"]["id"]
            bid = listing["bundle"]["id"]
        else:
            wanted = check_id(bundle_id, "Version")
            key, entry = _version_entry(listing, wanted)
            if entry is None:
                raise NotFound("The listing has no such version")
            bid = entry.get("bundle") or key
        version = self.store.version(publisher, exp, key)
        docs = dict((version or {}).get("docs") or {})
        shown = dict((version or {}).get("listing") or listing)
        docs["bundleId"] = bid
        docs["docsStatus"] = (
            catalog.docs_status(shown, docs if version else None) if version else "missing"
        )
        if version is not None:
            docs["metadata"] = {"updatedAt": version.get("builtAt")}
        return docs

    # ------------------------------------------------------------------ bundles
    def _resolve_handle(
        self, principal: Principal | None, handle: str
    ) -> tuple[str | None, str, str]:
        """(store owner, bundle id, archive folder name) for a bundle handle."""
        parts = handle.split("~") if isinstance(handle, str) else []
        if len(parts) == 4 and parts[0] == "L" and all(is_id(p) for p in parts[1:]):
            self._reader(principal)
            _, pub, exp, bid = parts
            listing = self.store.listing(pub, exp)
            mine = principal is not None and principal.uid == pub
            known = listing is not None and (
                bid == (listing.get("bundle") or {}).get("id")
                or _version_entry(listing, bid)[1] is not None
            )
            if not (known or (mine and self.store.has_bundle(None, bid))):
                raise NotFound("No such version")
            return None, bid, exp
        if (
            len(parts) == 3
            and parts[0] == "F"
            and all(is_id(p) for p in parts[1:])
            and principal is not None
        ):
            _, fork_id, bid = parts
            fork = self.store.fork(principal.uid, fork_id)
            if fork is None or bid not in {
                r.get("bundle") for r in (fork.get("revisions") or {}).values()
            }:
                raise NotFound("No such revision")
            return principal.uid, bid, fork_id
        raise NotFound("No such bundle")

    def bundle_meta(self, principal: Principal | None, handle: str) -> dict[str, Any]:
        owner, bid, _ = self._resolve_handle(principal, handle)
        meta = self.store.bundle_meta(owner, bid)
        meta["complete"] = True
        return meta

    def bundle_chunk(self, principal: Principal | None, handle: str, index: int) -> str:
        owner, bid, _ = self._resolve_handle(principal, handle)
        return self.store.bundle_chunk(owner, bid, index)

    def bundle_archive(self, principal: Principal | None, handle: str) -> tuple[str, bytes]:
        owner, bid, folder = self._resolve_handle(principal, handle)
        return f"{folder}-{bid.rsplit('-', 1)[-1]}.zip", bundles.zip_entries(
            self.store.bundle(owner, bid).entries(), folder
        )

    def art(self, principal: Principal | None, name: str) -> bytes:
        self._reader(principal)
        return self.store.art(name)

    # ------------------------------------------------------------------ forks
    def _present_fork(self, fork_id: str, fork: Mapping[str, Any]) -> dict[str, Any]:
        out = dict(fork)
        head = out.get("head")
        if isinstance(head, dict) and head.get("id"):
            out["head"] = {**head, "path": fork_handle(fork_id, head["id"])}
        return out

    def forks(self, principal: Principal | None) -> dict[str, dict[str, Any]]:
        if principal is None:
            return {}
        return {
            fid: self._present_fork(fid, f) for fid, f in self.store.forks(principal.uid).items()
        }

    def _fork(self, principal: Principal, fork_id: str) -> dict[str, Any]:
        fork = self.store.fork(principal.uid, check_id(fork_id, "Copy"))
        if fork is None:
            raise NotFound("This copy is not in your experiments")
        return fork

    def fork(
        self, principal: Principal | None, publisher: str, exp: str
    ) -> tuple[str, dict[str, Any]]:
        """Copy a listing's current version into the principal's experiments, with the
        documentation of that version."""
        who = self._writer(principal)
        self._reader(principal)
        with self.store.lock():
            listing = self._listing(publisher, exp)
            bid = listing["bundle"]["id"]
            self.store.copy_bundle(None, who.uid, bid)
            # The snapshot is named by the catalogue version (files plus documentation), not
            # the files alone: a documentation-only refresh keeps the bundle and makes a new
            # version, and its snapshot must sit beside the earlier one, never replace it.
            # Its content comes only from the immutable version record, so two copies of
            # one version share one identical snapshot.
            docs_key = listing.get("versionKey") or bid
            version = self.store.version(publisher, exp, docs_key)
            if version is not None and version.get("docs"):
                self.store.put_fork_docs(
                    who.uid,
                    docs_key,
                    {
                        **version["docs"],
                        "source": {
                            "publisher": publisher,
                            "exp": exp,
                            "version": (version.get("listing") or {}).get("version", ""),
                        },
                    },
                )
            t = _now()
            fork_id = check_id(f"{_slug(exp)}-{_base36(t)}", "Copy")
            while self.store.fork(who.uid, fork_id) is not None:
                t += 1
                fork_id = f"{_slug(exp)}-{_base36(t)}"
            version_label = "v" + listing["version"] if listing.get("version") else ""
            record = {
                "title": listing.get("title", exp),
                "summary": listing.get("summary", ""),
                "art": listing.get("art") or "fixation",
                "artImage": listing.get("artImage"),
                "tags": listing.get("tags", []),
                "subjects": listing.get("subjects", []),
                "hardware": listing.get("hardware", {}),
                "original": listing.get("original"),
                "originStatus": listing.get("status"),
                "methodDiagram": listing.get("methodDiagram"),
                "methodDiagramBundle": bid if listing.get("methodDiagram") else None,
                "parameterSets": listing.get("parameterSets", []),
                "alhazen": listing.get("alhazen", ""),
                "origin": {
                    "publisherUid": publisher,
                    "publisherName": (listing.get("publisher") or {}).get("name", ""),
                    "exp": exp,
                    "title": listing.get("title", exp),
                    "bundle": bid,
                    "docsKey": docs_key,
                    "version": listing.get("version", ""),
                    "commit": str((listing.get("source") or {}).get("commit", ""))[:7],
                },
                "head": {"id": bid},
                "revisions": {
                    str(t): {
                        "bundle": bid,
                        "message": f"Forked from {listing.get('title', exp)}"
                        + (f" {version_label}" if version_label else ""),
                        "by": "user",
                        "at": t,
                    }
                },
                "createdAt": t,
                "updatedAt": t,
            }
            record = {k: v for k, v in record.items() if v is not None}
            self.store.put_fork(who.uid, fork_id, record)
        return fork_id, self._present_fork(fork_id, record)

    def fork_docs(self, principal: Principal | None, fork_id: str) -> dict[str, Any]:
        """The documentation of the version a copy was forked from (``docs``), or, for a
        copy started elsewhere, its README (``readme``). ``stale`` says the snapshot is
        missing although the copy came from a listing."""
        who = self._writer(principal)
        fork = self._fork(who, fork_id)
        origin = fork.get("origin") or {}
        bid = origin.get("bundle") if not origin.get("scratch") else None
        docs_key = (origin.get("docsKey") or bid) if bid else None
        docs = self.store.fork_docs(who.uid, docs_key) if docs_key else None
        if docs is not None:
            return {"docs": docs, "readme": None, "stale": False}
        readme = None
        head = (fork.get("head") or {}).get("id")
        if head:
            for entry in self.store.bundle(who.uid, head).entries():
                if entry[0] == "README.md" and not bundles.is_binary_entry(entry):
                    readme = entry[1]
        return {"docs": None, "readme": readme, "stale": bool(bid)}

    def upload_revision(
        self, principal: Principal | None, fork_id: str, archive: bytes, message: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """A new revision of a copy from a zip archive of its folder."""
        who = self._writer(principal)
        message = str(message or "").strip()
        if not message:
            raise InvalidRequest("Say in a few words what changed")
        if len(message) > MAX_MESSAGE:
            raise InvalidRequest(f"The message is longer than {MAX_MESSAGE} characters")
        blobs = bundles.blobs_from_zip(archive)
        if not any(path in ("run.py", "pyproject.toml") for path, _, _ in blobs):
            raise InvalidRequest(
                "The archive is not an alhazen experiment (no pyproject.toml or run.py at its top)"
            )
        entries, excluded = bundles.split_blobs(blobs)
        with self.store.lock():
            fork = self._fork(who, fork_id)
            built = bundles.build_bundle(entries, excluded, fork_id, {"fork": fork_id})
            previous = (fork.get("head") or {}).get("id")
            stat = self._stat(who.uid, previous, built)
            self.store.put_bundle(who.uid, built)
            t = max(_now(), int(fork.get("updatedAt") or 0) + 1)
            revision = {"bundle": built.id, "message": message, "by": "user", "at": t, "stat": stat}
            fork = dict(
                fork,
                head={"id": built.id},
                updatedAt=t,
                revisions={**(fork.get("revisions") or {}), str(t): revision},
            )
            self.store.put_fork(who.uid, fork_id, fork)
        return revision, self._present_fork(fork_id, fork)

    def _stat(self, uid: str, previous: str | None, built: bundles.Bundle) -> str:
        new = {e[0]: e[1] for e in built.entries()}
        old = {e[0]: e[1] for e in self.store.bundle(uid, previous).entries()} if previous else {}
        added = sorted(set(new) - set(old))
        removed = sorted(set(old) - set(new))
        changed = sorted(p for p in set(new) & set(old) if new[p] != old[p])
        lines = (
            [f" {p} (added)" for p in added]
            + [f" {p} (removed)" for p in removed]
            + [f" {p}" for p in changed]
        )
        lines.append(f" {len(changed)} changed, {len(added)} added, {len(removed)} removed")
        return "\n".join(lines[-200:])

    def checkout(
        self, principal: Principal, fork_id: str
    ) -> tuple[dict[str, Any], list[bundles.Entry]]:
        """A copy and the files of its current revision."""
        fork = self._fork(principal, fork_id)
        head = (fork.get("head") or {}).get("id")
        if not head:
            raise Conflict("This copy has no revision yet")
        return fork, self.store.bundle(principal.uid, head).entries()

    def publish(
        self, principal: Principal | None, fork_id: str, form: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Put a copy's current revision on the catalogue under the principal."""
        who = self._writer(principal)
        clean = self._publish_form(form)
        with self.store.lock():
            fork = self._fork(who, fork_id)
            head = (fork.get("head") or {}).get("id")
            if not head:
                raise Conflict("This copy has no revision to publish yet")
            self.store.copy_bundle(who.uid, None, head)
            meta = self.store.bundle_meta(None, head)
            t = _now()
            old = self.store.listing(who.uid, fork_id) or {}
            listing = {
                "id": fork_id,
                "title": clean["title"],
                "summary": clean["summary"],
                "art": fork.get("art") or "fixation",
                "artImage": fork.get("artImage"),
                "featured": False,
                "category": clean["category"],
                "collection": "own",
                "original": fork.get("original"),
                "tags": [
                    clean["category"],
                    *[x for x in fork.get("tags") or [] if x != clean["category"]],
                ],
                "subjects": clean["subjects"],
                "hardware": clean["hardware"],
                "platforms": list(catalog.PLATFORMS),
                "license": clean["license"],
                "publisher": {"uid": who.uid, "name": who.name},
                "source": {"kind": "fork", "from": fork.get("origin")},
                "version": clean["version"],
                "alhazen": fork.get("alhazen", ""),
                "parameterSets": fork.get("parameterSets", []),
                "bundle": {
                    "id": head,
                    "fileCount": meta["fileCount"],
                    "bytes": meta["bytes"],
                    "binaries": meta.get("binaries", 0),
                    "excluded": len(json.loads(meta.get("excluded") or "[]")),
                },
                "publishedAt": old.get("publishedAt") or t,
                "updatedAt": t,
                "versions": {
                    **(old.get("versions") or {}),
                    head: {"version": clean["version"], "at": t},
                },
            }
            listing = {k: v for k, v in listing.items() if v is not None}
            origin = fork.get("origin") or {}
            docs: dict[str, Any] = {}
            if origin.get("bundle") == head:
                snapshot = self.store.fork_docs(who.uid, origin.get("docsKey") or head)
                docs = {k: v for k, v in (snapshot or {}).items() if k != "source"}
            if not self.store.version(who.uid, fork_id, head):
                self.store.put_version(
                    who.uid,
                    fork_id,
                    head,
                    {"key": head, "bundleId": head, "listing": listing, "docs": docs, "builtAt": t},
                )
            self.store.put_listing(who.uid, fork_id, listing)
            fork = dict(
                fork,
                published={
                    "at": t,
                    "version": clean["version"],
                    "bundle": head,
                    "title": clean["title"],
                },
                updatedAt=t,
            )
            self.store.put_fork(who.uid, fork_id, fork)
        return self._present(who.uid, fork_id, listing)

    def _publish_form(self, form: Mapping[str, Any]) -> dict[str, Any]:
        def text(name: str, limit: int, required: bool = True) -> str:
            value = str(form.get(name) or "").strip()
            if required and not value:
                raise InvalidRequest(f"{name.capitalize()} is required")
            if len(value) > limit:
                raise InvalidRequest(f"{name.capitalize()} is longer than {limit} characters")
            return value

        category = text("category", 40)
        if not is_id(category):
            raise InvalidRequest("Category is not one of the catalogue's categories")
        version = text("version", 40)
        if not re.match(r"^[0-9A-Za-z][0-9A-Za-z.+_-]*$", version):
            raise InvalidRequest("Version may contain letters, digits and . + _ - only")
        subjects = [s for s in form.get("subjects") or [] if s in SUBJECTS]
        hardware_in = form.get("hardware") or {}
        if not isinstance(hardware_in, dict):
            raise InvalidRequest("Hardware must be a set of yes/no answers")
        hardware = {k: bool(hardware_in.get(k)) for k in catalog.HW_KEYS if k in hardware_in}
        return {
            "title": text("title", MAX_TITLE),
            "summary": text("summary", MAX_SUMMARY, required=False),
            "category": category,
            "version": version,
            "subjects": subjects,
            "hardware": hardware,
            "license": text("license", 200, required=False) or "All rights reserved",
        }

    def unpublish(self, principal: Principal | None, fork_id: str) -> None:
        who = self._writer(principal)
        with self.store.lock():
            fork = self._fork(who, fork_id)
            self.store.delete_listing(who.uid, fork_id)
            fork = dict(fork, updatedAt=_now())
            fork.pop("published", None)
            self.store.put_fork(who.uid, fork_id, fork)

    def remove_fork(self, principal: Principal | None, fork_id: str) -> dict[str, Any]:
        """Remove a copy from the principal's experiments: its record (revisions and job
        records with it), the private bundles of its revisions that no other copy of the
        principal names, and the docs snapshot of the version it was forked from when no
        other copy was forked from that version. A listing published from it stays until
        it is taken off: publishing copied that revision into the published store.
        Returns what was removed: ``{"bundles": [...], "docs": [...]}``."""
        who = self._writer(principal)
        with self.store.lock():
            self._fork(who, fork_id)
            self.store.delete_fork(who.uid, fork_id)
            kept_bundles: set[str] = set()
            kept_docs: set[str] = set()
            for other in self.store.forks(who.uid).values():
                kept_bundles.update(_fork_bundles(other))
                key = _fork_docs_key(other)
                if key:
                    kept_docs.add(key)
            # Private bundles and snapshots no remaining copy names: the removed copy's,
            # and any left behind by removals before this cleanup existed.
            bundles_gone = [b for b in self.store.private_bundles(who.uid) if b not in kept_bundles]
            docs_gone = [k for k in self.store.fork_docs_keys(who.uid) if k not in kept_docs]
            for bundle_id in bundles_gone:
                self.store.delete_private_bundle(who.uid, bundle_id)
            for key in docs_gone:
                self.store.delete_fork_docs(who.uid, key)
        return {"bundles": bundles_gone, "docs": docs_gone}


def _fork_bundles(fork: Mapping[str, Any]) -> set[str]:
    """Every bundle a copy names: its head and each revision's."""
    named = {
        str(r.get("bundle"))
        for r in (fork.get("revisions") or {}).values()
        if isinstance(r, dict) and r.get("bundle")
    }
    head = (fork.get("head") or {}).get("id") if isinstance(fork.get("head"), dict) else None
    if head:
        named.add(str(head))
    return named


def _fork_docs_key(fork: Mapping[str, Any]) -> str | None:
    """The docs snapshot a copy reads: the catalogue version it was forked from."""
    origin = fork.get("origin") or {}
    if origin.get("scratch"):
        return None
    key = origin.get("docsKey") or origin.get("bundle")
    return str(key) if key else None
