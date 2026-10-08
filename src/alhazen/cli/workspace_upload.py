"""The workspace's Upload action, server side: an experiment's data to the archive.

Routes (dashboard.py, all behind the API token):

  GET  /api/upload/settings          this computer's settings
  POST /api/upload/settings          save them (validated; nothing on error)
  GET  /api/upload/check             can an upload start now
  GET  /api/upload/login             the SFTP connection's login state
  POST /api/upload/login             connect / trust / answer / disconnect
  GET  /api/upload/launch-session    the session folder a launch wrote
  GET  /api/upload/receipts          one session's receipts, newest first
  POST /api/upload/preview           a quick dry run: what would copy
  POST /api/upload/start             start the upload (one at a time)
  GET  /api/upload/job               the current or last upload's progress
  POST /api/upload/cancel            stop it; what was copied stays

WHAT goes. Everything in the experiment's data folder: the session folders
and every other file saved there (participants.tsv, experiment.sqlite3,
calibrations, logs, the receipts of earlier uploads), plus the workspace's
people registry — a consistent snapshot of ``people.sqlite3`` and its CSV
copies for this experiment — under ``people/``. Choosing sessions (the Run
page's card, the checked rows on History or Data) uploads those sessions
and every non-session file of their data folder; "all" uploads the whole
folder. SQLite files are uploaded as snapshots (the backup API), never
copied while something may be writing them.

WHERE. ``<base>/<experiment>/<path inside the data folder>``, the
experiment's [project] name as its folder; a rehearsal data folder goes to
``<experiment>-rehearsal``, never among the real sessions; the registry to
``<experiment>/people/``.

THE RULES. Nothing at the destination is ever deleted or replaced. A file
already there with the same content is left. Inside a session folder a
different file there is a *conflict*: reported, left alone, and nothing is
added (the session folder must stay exactly what its manifest lists). Any
other file that changed here is uploaded beside the old one as a new
version, ``name.<UTC stamp>.ext``, unless an earlier version already holds
the same content. Every file is checked by SHA-256 at the destination.

Only whole sessions go by default: a session folder without its manifest
never finished teardown and is uploaded only when asked for; the session
the active run is writing never is.
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from alhazen.cli.upload_receipts import latest, receipts, write_receipt
from alhazen.cli.upload_sftp import SftpSession, SftpTransport
from alhazen.cli.upload_transport import (
    Cancelled,
    LocalCopy,
    Progress,
    Put,
    RsyncSsh,
    Transport,
    UploadError,
    UploadSettings,
    is_version_of,
    load_settings,
    local_tree,
    save_settings,
    sha256_file,
    versioned,
)
from alhazen.cli.workspace import Workspace, now
from alhazen.cli.workspace_data import DataRoot, DataView, _run_folder, data_roots
from alhazen.cli.workspace_manage import _console_run_folder
from alhazen.data.manifest import verify_manifest
from alhazen.data.paths import find_runs
from alhazen.errors import AlhazenError
from alhazen.version import __version__

# At most this many sessions in one upload: a batch is a click, not a
# migration, and its preview has to stay readable.
MAX_SESSIONS = 500
# The receipt "item" of a data folder's files outside any session folder,
# and of the people registry.
SHARED = "_shared"
PEOPLE = "_people"
ACTIVE_PHASES = ("starting", "copying", "verifying")


@dataclass
class Entry:
    """One file to upload: here, and its path in the archive folder."""

    local: Path
    remote: str
    size: int
    item: str  # a run id, SHARED or PEOPLE
    session: bool  # inside a session folder: never versioned
    sha256: str = ""
    target: str | None = None  # the name it ends up under there
    state: str = ""  # new | present | version | conflict


@dataclass
class Group:
    """What goes to one archive folder from one place."""

    folder: str
    kind: str  # real | rehearsal | people
    root: DataRoot | None
    entries: list[Entry] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


class Uploads:
    """Uploads over one workspace: settings, the SFTP connection, previews and
    one job at a time."""

    def __init__(self, workspace: Workspace, data: DataView, transport: Transport | None = None):
        self.workspace = workspace
        self.data = data
        # A transport given here replaces the one the settings choose: the
        # tests' seam (a fake that fails on cue).
        self._transport = transport
        # The dashboard's one SSH connection (the SFTP transport), held for
        # its lifetime once the operator has logged in.
        self.sftp = SftpSession(workspace.directory)
        self._lock = threading.Lock()
        self._job: dict[str, Any] | None = None
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    def close(self) -> None:
        self.sftp.close()

    # -- settings and the connection --------------------------------------------

    def settings(self) -> UploadSettings:
        return load_settings(self.workspace.directory)

    def transport(self) -> Transport:
        if self._transport is not None:
            return self._transport
        settings = self.settings()
        if settings.transport == "local":
            return LocalCopy.from_settings(settings)
        if settings.transport == "rsync":
            return RsyncSsh(settings)
        return SftpTransport(self.sftp, settings)

    def describe_settings(self) -> dict[str, Any]:
        settings = self.settings()
        command = None
        if settings.transport == "rsync":
            try:
                command = RsyncSsh(settings).open_command()
            except UploadError:
                command = None
        return {
            "settings": settings.model_dump(),
            "defaults": UploadSettings().model_dump(),
            "open_command": command,
            "login": self.sftp.snapshot() if settings.transport == "sftp" else None,
        }

    def check(self) -> dict[str, Any]:
        try:
            return dict(self.transport().check())
        except UploadError as exc:
            return {"ok": False, "message": str(exc)}

    def login(self, body: dict[str, Any]) -> dict[str, Any]:
        action = body.get("action")
        if action == "connect":
            settings = self.settings()
            if settings.transport != "sftp":
                raise ValueError("Connect is for the SFTP transport; the settings choose another")
            return self.sftp.connect(settings)
        if action == "trust":
            return self.sftp.trust(str(body.get("fingerprint", "")))
        if action == "answer":
            return self.sftp.answer(body.get("answers"))
        if action == "disconnect":
            return self.sftp.disconnect()
        raise ValueError("Unknown login action")

    # -- what to upload ------------------------------------------------------------

    def _slug(self, described: dict[str, Any]) -> str:
        return str(described.get("slug") or described.get("name") or "")

    def _active_folder(self, project_id: str, roots: list[DataRoot]) -> str | None:
        with self.workspace.lock:
            active = self.workspace.active
            run = self.workspace.runs.get(active) if active else None
        if not run or run["project"] != project_id:
            return None
        console = self.workspace.directory / "runs" / run["id"] / "console.log"
        return _console_run_folder(console, roots, run["mode"])

    def groups(
        self, project_id: str, selection: Any, include_incomplete: bool, stage: Path
    ) -> list[Group]:
        """The files a request names, by archive folder. ``selection`` is a
        list of ``{"root": id, "runs": [ids]}`` or ``{"root": id, "all":
        true}``; SQLite snapshots are written under ``stage``."""
        if not isinstance(selection, list) or not selection:
            raise ValueError("Choose at least one session to upload")
        described = self.workspace.describe(project_id)
        slug = self._slug(described)
        existing, _, _ = data_roots(described)
        active = self._active_folder(project_id, existing)
        groups: list[Group] = []
        sessions = 0
        for entry in selection:
            if not isinstance(entry, dict) or not isinstance(entry.get("root"), str):
                raise ValueError("Each selection names a data folder")
            root = self.data._root(project_id, entry["root"])
            whole = entry.get("all") is True
            chosen: list[str] = []
            if not whole:
                named = entry.get("runs")
                if not isinstance(named, list) or not all(isinstance(r, str) for r in named):
                    raise ValueError("Each selection lists run ids")
                if len(set(named)) != len(named):
                    raise ValueError("A session was chosen twice")
                for run_id in named:
                    _run_folder(root.path, run_id)  # its shape, and that it exists
                chosen = named
            folder = f"{slug}-rehearsal" if root.kind == "rehearsal" else slug
            group = Group(folder, root.kind, root)
            runs = {
                f.path.relative_to(root.path).as_posix(): f.path
                for f in find_runs(root.path)
                if f.path.resolve().is_relative_to(root.path.resolve())
            }
            wanted = set(runs) if whole else set(chosen)
            sessions += len(wanted)
            for rel, path, size in local_tree(root.path):
                run_id = next((r for r in runs if rel.startswith(r + "/")), None)
                if run_id is None:
                    group.entries.append(_entry(path, rel, size, SHARED, False, stage))
                    continue
                if run_id not in wanted:
                    continue
                folder_path = runs[run_id]
                if active and str(folder_path.resolve()) == active:
                    _note(group, f"{run_id}: the active run is still writing it")
                    continue
                if not include_incomplete and not (folder_path / "manifest.yaml").is_file():
                    _note(
                        group,
                        f"{run_id}: no manifest (the session never finished); include "
                        "incomplete sessions to upload it anyway",
                    )
                    continue
                group.entries.append(Entry(path, rel, size, run_id, True))
            groups.append(group)
        if sessions > MAX_SESSIONS:
            raise ValueError(f"At most {MAX_SESSIONS} sessions per upload; choose fewer")
        people = self._people_group(project_id, slug, stage)
        if people is not None:
            groups.append(people)
        return groups

    def _people_group(self, project_id: str, slug: str, stage: Path) -> Group | None:
        """The people registry: a snapshot of the database and this
        experiment's CSV copies (plus the experimenters list), to
        ``<experiment>/people/``."""
        registry = self.workspace.people
        if registry is None:
            return None
        group = Group(slug, "people", None)
        if registry.path.is_file():
            group.entries.append(
                _entry(registry.path, "people/people.sqlite3", 0, PEOPLE, False, stage)
            )
        csv_files = [registry.csv_path("experimenters", None)]
        mine = registry.csv_path("subjects", project_id).parent
        if mine.is_dir():
            csv_files += sorted(p for p in mine.iterdir() if p.is_file() and p.suffix == ".csv")
        for path in csv_files:
            if path.is_file():
                rel = path.relative_to(registry.directory).as_posix()
                group.entries.append(
                    Entry(path, f"people/{rel}", path.stat().st_size, PEOPLE, False)
                )
        return group if group.entries else None

    # -- the plan -------------------------------------------------------------------

    def _classify(self, transport: Transport, group: Group, *, by_content: bool) -> None:
        """Decide what each entry needs (see the module's rules). Without
        ``by_content`` (the preview) only names and sizes are compared."""
        listing = transport.listing(group.folder)
        if by_content:
            for e in group.entries:
                e.sha256 = e.sha256 or sha256_file(e.local)
            ask: list[str] = []
            for e in group.entries:
                if e.remote in listing:
                    ask.append(e.remote)
                    if not e.session:
                        ask += [n for n in listing if is_version_of(n, e.remote)]
            remote = transport.checksums(group.folder, sorted(set(ask))) if ask else {}
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for e in group.entries:
            if e.remote not in listing:
                e.state, e.target = "new", e.remote
            elif not by_content:
                same = listing[e.remote] == e.size
                e.state = "present" if same else ("conflict" if e.session else "version")
                e.target = e.remote if same else None
            elif remote.get(e.remote) == e.sha256:
                e.state, e.target = "present", e.remote
            elif e.session:
                e.state, e.target = "conflict", None
            else:
                match = next(
                    (
                        n
                        for n in listing
                        if is_version_of(n, e.remote) and remote.get(n) == e.sha256
                    ),
                    None,
                )
                if match:
                    e.state, e.target = "present", match
                else:
                    e.state, e.target = "version", versioned(e.remote, stamp)

    def preview(self, project_id: str, selection: Any, include_incomplete: bool) -> dict[str, Any]:
        """A quick dry run: names and sizes only (content is compared when
        the upload runs)."""
        transport = self.transport()
        with tempfile.TemporaryDirectory(prefix="alhazen-upload-") as stage:
            groups = self.groups(project_id, selection, include_incomplete, Path(stage))
            out = []
            for group in groups:
                self._classify(transport, group, by_content=False)
                out.append(
                    {
                        "kind": group.kind,
                        "root": group.root.id if group.root else None,
                        "destination": transport.destination(group.folder),
                        "items": _items(group),
                        "skipped": group.skipped,
                    }
                )
        return {"groups": out}

    # -- the job -----------------------------------------------------------------------

    def job(self) -> dict[str, Any]:
        with self._lock:
            return {"job": dict(self._job) if self._job else None}

    def start(self, project_id: str, selection: Any, include_incomplete: bool) -> dict[str, Any]:
        transport = self.transport()
        stage = tempfile.TemporaryDirectory(prefix="alhazen-upload-")
        try:
            groups = self.groups(project_id, selection, include_incomplete, Path(stage.name))
            if not any(g.entries for g in groups):
                raise ValueError(
                    "Nothing to upload: " + "; ".join(s for g in groups for s in g.skipped)
                    or "no files"
                )
            with self._lock:
                if self._job and self._job["phase"] in ACTIVE_PHASES:
                    raise ValueError("An upload is already running; wait for it or stop it")
                self._cancel = threading.Event()
                self._job = {
                    "id": uuid.uuid4().hex,
                    "project": project_id,
                    "phase": "starting",
                    "started": now(),
                    "finished": None,
                    "skipped": [s for g in groups for s in g.skipped],
                    "groups": [
                        {
                            "root": g.root.id if g.root else None,
                            "kind": g.kind,
                            "destination": transport.destination(g.folder),
                            "runs": sorted({e.item for e in g.entries}),
                        }
                        for g in groups
                    ],
                    "progress": {"bytes_done": 0, "bytes_total": 0, "file": ""},
                    "results": {},
                    "error": None,
                }
                job_id = self._job["id"]
                self._thread = threading.Thread(
                    target=self._run, args=(job_id, transport, groups, stage), daemon=True
                )
                self._thread.start()
                return {"job": dict(self._job)}
        except BaseException:
            stage.cleanup()
            raise

    def cancel(self) -> None:
        with self._lock:
            if not self._job or self._job["phase"] not in ACTIVE_PHASES:
                raise ValueError("No upload is running")
        self._cancel.set()

    def _set(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            if self._job and self._job["id"] == job_id:
                self._job.update(fields)

    def _run(
        self,
        job_id: str,
        transport: Transport,
        groups: list[Group],
        stage: tempfile.TemporaryDirectory[str],
    ) -> None:
        results: dict[str, dict[str, Any]] = {}
        phase, error = "done", None
        try:
            for group in groups:
                if not group.entries:
                    continue
                started = now()
                destination = transport.destination(group.folder)
                try:
                    self._classify(transport, group, by_content=True)
                    puts = [
                        Put(e.local, e.target, e.size, e.sha256)
                        for e in group.entries
                        if e.state in ("new", "version") and e.target
                    ]
                    total = sum(p.size for p in puts)
                    self._set(
                        job_id,
                        phase="copying",
                        progress={"bytes_done": 0, "bytes_total": total, "file": ""},
                    )

                    def report(p: Progress) -> None:
                        self._set(
                            job_id,
                            progress={
                                "bytes_done": p.bytes_done,
                                "bytes_total": p.bytes_total,
                                "file": p.file,
                            },
                        )

                    written = transport.put(group.folder, puts, report, self._cancel)
                    self._set(job_id, phase="verifying")
                    missing = self._verify(transport, group, written)
                except Cancelled as exc:
                    phase, error = "cancelled", str(exc)
                    self._receipts(group, destination, started, "cancelled", str(exc), [], results)
                    break
                except (UploadError, OSError, ValueError, AlhazenError, sqlite3.Error) as exc:
                    phase, error = "failed", str(exc)
                    self._receipts(group, destination, started, "failed", str(exc), [], results)
                    break
                self._receipts(group, destination, started, None, None, missing, results)
                self._set(job_id, results=dict(results))
        finally:
            stage.cleanup()
            self._set(job_id, phase=phase, error=error, finished=now(), results=dict(results))

    def _verify(self, transport: Transport, group: Group, written: dict[str, str]) -> list[str]:
        """Targets that are not there with the local content. A file the put
        checked before naming it, or one the plan already compared, is not
        asked for again."""
        ask = [
            e.target
            for e in group.entries
            if e.target and e.state in ("new", "version") and e.target not in written
        ]
        found = transport.checksums(group.folder, ask) if ask else {}
        missing = []
        for e in group.entries:
            if not e.target or e.state not in ("new", "version"):
                continue
            digest = written.get(e.target) or found.get(e.target)
            if digest != e.sha256:
                missing.append(e.target)
        return missing

    def _receipts(
        self,
        group: Group,
        destination: str,
        started: str,
        status: str | None,
        error: str | None,
        missing: list[str],
        results: dict[str, dict[str, Any]],
    ) -> None:
        """One receipt per session, one for the folder's other files and one
        for the registry, each noted in ``results``."""
        settings = self.settings()
        by_item: dict[str, list[Entry]] = {}
        for e in group.entries:
            by_item.setdefault(e.item, []).append(e)
        for item, entries in by_item.items():
            files = []
            for e in entries:
                e.sha256 = e.sha256 or sha256_file(e.local)
                files.append(
                    {
                        "path": e.remote,
                        "size": e.size,
                        "sha256": e.sha256,
                        "state": e.state or None,
                        "stored_as": e.target,
                    }
                )
            receipt: dict[str, Any] = {
                "started": started,
                "finished": now(),
                "destination": {
                    "transport": settings.transport if self._transport is None else "custom",
                    "path": destination if item in (SHARED, PEOPLE) else f"{destination}/{item}",
                },
                "files": files,
                "alhazen": __version__,
            }
            if group.root is not None and item not in (SHARED, PEOPLE):
                folder = group.root.path / item
                manifest = folder / "manifest.yaml"
                receipt["local_manifest_problems"] = (
                    verify_manifest(folder, manifest)
                    if manifest.is_file()
                    else ["no manifest.yaml"]
                )
            if status is not None:
                receipt.update(status=status, error=error, verified=False)
            else:
                conflicts = [e.remote for e in entries if e.state == "conflict"]
                lost = [e.target for e in entries if e.target in missing]
                state = "conflict" if conflicts else "incomplete" if lost else "verified"
                receipt.update(
                    status=state,
                    verified=state == "verified",
                    verify_method="SHA-256 at the destination",
                    copied=[e.remote for e in entries if e.state == "new"],
                    new_versions={e.remote: e.target for e in entries if e.state == "version"},
                    already_there=[e.remote for e in entries if e.state == "present"],
                    conflicts=conflicts,
                    missing=lost,
                )
            where = group.root.path if group.root is not None else self.workspace.directory
            path = write_receipt(where, item, receipt)
            key = f"{group.root.id if group.root else 'people'}:{item}"
            results[key] = {"status": receipt["status"], "receipt": path.name}

    # -- the Run page -------------------------------------------------------------------

    def launch_session(self, project_id: str, launch_id: str) -> dict[str, Any]:
        """The session folder a launch from this workspace wrote, with its
        upload state; ``{"session": None}`` when its console names none."""
        with self.workspace.lock:
            run = self.workspace.runs.get(launch_id)
        if run is None or run["project"] != project_id:
            raise FileNotFoundError("Unknown launch")
        described = self.workspace.describe(project_id)
        existing, _, _ = data_roots(described)
        console = self.workspace.directory / "runs" / launch_id / "console.log"
        folder = _console_run_folder(console, existing, run["mode"])
        if folder is None:
            return {"session": None}
        for root in existing:
            top = root.path.resolve()
            if Path(folder).is_relative_to(top):
                run_id = Path(folder).relative_to(top).as_posix()
                slug = self._slug(described)
                name = f"{slug}-rehearsal" if root.kind == "rehearsal" else slug
                try:
                    where: str | None = f"{self.transport().destination(name)}/{run_id}"
                    unset = None
                except UploadError as exc:
                    where, unset = None, str(exc)
                return {
                    "session": {
                        # Where an upload would put it, from the settings now;
                        # None with the reason when they are not complete.
                        "destination": where,
                        "destination_problem": unset,
                        "root": root.id,
                        "root_kind": root.kind,
                        "run": run_id,
                        "complete": (Path(folder) / "manifest.yaml").is_file(),
                        "upload": latest(root.path, run_id),
                    }
                }
        return {"session": None}

    def receipts(self, project_id: str, root_id: str, run_id: str) -> dict[str, Any]:
        root = self.data._root(project_id, root_id)
        _run_folder(root.path, run_id)
        return {"receipts": receipts(root.path, run_id)}

    # -- routes -----------------------------------------------------------------------

    def get(self, route: str, query: dict[str, list[str]]) -> dict[str, Any]:
        def arg(name: str) -> str:
            return query.get(name, [""])[0]

        if route == "settings":
            return self.describe_settings()
        if route == "check":
            return self.check()
        if route == "login":
            return self.sftp.snapshot()
        if route == "job":
            return self.job()
        if route == "launch-session":
            return self.launch_session(arg("project"), arg("launch"))
        if route == "receipts":
            return self.receipts(arg("project"), arg("root"), arg("run"))
        raise FileNotFoundError(f"No upload route {route!r}")

    def post(self, route: str, body: dict[str, Any]) -> dict[str, Any]:
        if route == "settings":
            save_settings(self.workspace.directory, body.get("settings"))
            return self.describe_settings()
        if route == "login":
            return self.login(body)
        if route == "preview":
            return self.preview(
                _project(body), body.get("selection"), body.get("include_incomplete") is True
            )
        if route == "start":
            return self.start(
                _project(body), body.get("selection"), body.get("include_incomplete") is True
            )
        if route == "cancel":
            self.cancel()
            return {"ok": True}
        raise FileNotFoundError(f"No upload route {route!r}")


def _project(body: dict[str, Any]) -> str:
    value = body.get("project")
    if not isinstance(value, str):
        raise ValueError("Name the experiment")
    return value


def _note(group: Group, reason: str) -> None:
    if reason not in group.skipped:
        group.skipped.append(reason)


def _entry(path: Path, rel: str, size: int, item: str, session: bool, stage: Path) -> Entry:
    """An entry; a SQLite database is replaced by a consistent snapshot."""
    if PurePosixPath(rel).suffix in (".sqlite3", ".sqlite", ".db"):
        copy = stage / uuid.uuid4().hex / PurePosixPath(rel).name
        copy.parent.mkdir(parents=True)
        source = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            target = sqlite3.connect(copy)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        return Entry(copy, rel, copy.stat().st_size, item, session)
    return Entry(path, rel, size, item, session)


def _items(group: Group) -> list[dict[str, Any]]:
    """The preview's rows: one per session, then the other files, then the
    registry."""
    by_item: dict[str, list[Entry]] = {}
    for e in group.entries:
        by_item.setdefault(e.item, []).append(e)
    rows = []
    for item, entries in sorted(by_item.items(), key=lambda kv: (kv[0] in (SHARED, PEOPLE), kv[0])):
        count = {
            s: sum(1 for e in entries if e.state == s)
            for s in ("new", "present", "version", "conflict")
        }
        rows.append(
            {
                "item": item,
                "kind": "people" if item == PEOPLE else "shared" if item == SHARED else "session",
                "files": len(entries),
                "bytes": sum(e.size for e in entries),
                "new_bytes": sum(e.size for e in entries if e.state in ("new", "version")),
                **count,
                "conflicts": [e.remote for e in entries if e.state == "conflict"],
                "versions": [e.remote for e in entries if e.state == "version"],
                "complete": True
                if item in (SHARED, PEOPLE) or group.root is None
                else (group.root.path / item / "manifest.yaml").is_file(),
                "upload": latest(group.root.path, item)
                if group.root is not None and item not in (SHARED, PEOPLE)
                else None,
            }
        )
    return rows
