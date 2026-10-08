"""The workspace's Upload action, server side: sessions to the archive.

Routes (dashboard.py, all behind the API token):

  GET  /api/upload/settings          this computer's settings and defaults
  POST /api/upload/settings          save them (validated; nothing on error)
  GET  /api/upload/check             can an upload start now (the connection)
  GET  /api/upload/launch-session    the session folder a launch wrote
  GET  /api/upload/receipts          one session's receipts, newest first
  POST /api/upload/preview           a dry run: per session, what would copy
  POST /api/upload/start             start the upload (one at a time)
  GET  /api/upload/job               the current or last upload's progress
  POST /api/upload/cancel            stop it; what was copied stays

What may be uploaded is chosen the way the Data view chooses what to read:
the browser names a data folder by the id `data_roots` computed and runs by
their ids, both looked up again here (`_run_folder`). A session goes to
``<base>/<experiment>/<run id>``: the experiment's [project] name as its
folder, the run's own ``v<version>/sub-<ID>/ses-<NNN>/run-<NN>_task-<task>``
path inside it. A rehearsal data folder goes to ``<experiment>-rehearsal``,
as on the rig, so practice runs never land among real ones.

Only whole sessions go by default: a session folder without its manifest
never finished teardown (the run is still going, or was killed) and is
skipped unless the request includes incomplete sessions. The session the
active run is writing is never uploaded.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alhazen.cli.upload_receipts import latest, receipts, write_receipt
from alhazen.cli.upload_transport import (
    Cancelled,
    FilePlan,
    Item,
    Progress,
    Transport,
    UploadError,
    UploadSettings,
    load_settings,
    local_files,
    save_settings,
    sha256_file,
    transport_for,
)
from alhazen.cli.workspace import Workspace, now
from alhazen.cli.workspace_data import DataRoot, DataView, _run_folder, data_roots
from alhazen.cli.workspace_manage import _console_run_folder
from alhazen.data.manifest import verify_manifest
from alhazen.errors import AlhazenError
from alhazen.version import __version__

# At most this many sessions in one upload: a batch is a click, not a
# migration, and its preview has to stay readable.
MAX_SESSIONS = 500


@dataclass
class Group:
    """The sessions of one upload from one data folder."""

    root: DataRoot
    folder: str  # the experiment's archive folder
    items: list[Item]


class Uploads:
    """Uploads over one workspace: settings, previews and one job at a time."""

    def __init__(self, workspace: Workspace, data: DataView, transport: Transport | None = None):
        self.workspace = workspace
        self.data = data
        # A transport given here replaces the one the settings choose: the
        # tests' seam (a fake that fails on cue).
        self._transport = transport
        self._lock = threading.Lock()
        self._job: dict[str, Any] | None = None
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    # -- settings ----------------------------------------------------------

    def settings(self) -> UploadSettings:
        return load_settings(self.workspace.directory)

    def transport(self) -> Transport:
        if self._transport is not None:
            return self._transport
        return transport_for(self.settings())

    def describe_settings(self) -> dict[str, Any]:
        settings = self.settings()
        return {
            "settings": settings.model_dump(),
            "defaults": UploadSettings().model_dump(),
            "open_command": (
                transport_for(settings).open_command()  # type: ignore[union-attr]
                if settings.transport == "ssh" and settings.user
                else None
            ),
        }

    def check(self) -> dict[str, Any]:
        try:
            return dict(self.transport().check())
        except UploadError as exc:
            return {"ok": False, "message": str(exc)}

    # -- what to upload --------------------------------------------------------

    def _folder_name(self, described: dict[str, Any], root: DataRoot) -> str:
        name = described.get("slug") or described.get("name") or ""
        return f"{name}-rehearsal" if root.kind == "rehearsal" else name

    def _active_folder(self, project_id: str, roots: list[DataRoot]) -> str | None:
        with self.workspace.lock:
            active = self.workspace.active
            run = self.workspace.runs.get(active) if active else None
        if not run or run["project"] != project_id:
            return None
        console = self.workspace.directory / "runs" / run["id"] / "console.log"
        return _console_run_folder(console, roots, run["mode"])

    def groups(self, project_id: str, selection: Any) -> tuple[list[Group], list[str]]:
        """The sessions a request names, by data folder, and the reasons any
        named session is left out. ``selection`` is a list of
        ``{"root": id, "runs": [ids]}`` or ``{"root": id, "all": true}``."""
        if not isinstance(selection, list) or not selection:
            raise ValueError("Choose at least one session to upload")
        described = self.workspace.describe(project_id)
        existing, _, _ = data_roots(described)
        active = self._active_folder(project_id, existing)
        groups: list[Group] = []
        skipped: list[str] = []
        count = 0
        for entry in selection:
            if not isinstance(entry, dict) or not isinstance(entry.get("root"), str):
                raise ValueError("Each selection names a data folder")
            root = self.data._root(project_id, entry["root"])
            run_ids: list[str]
            if entry.get("all") is True:
                run_ids = [row["id"] for row in self.data.runs(project_id, root.id)["runs"]]
            else:
                named = entry.get("runs")
                if not isinstance(named, list) or not all(isinstance(r, str) for r in named):
                    raise ValueError("Each selection lists run ids")
                run_ids = named
                if len(set(run_ids)) != len(run_ids):
                    raise ValueError("A session was chosen twice")
            items = []
            for run_id in run_ids:
                folder = _run_folder(root.path, run_id)
                if active and str(folder.resolve()) == active:
                    skipped.append(f"{run_id}: the active run is still writing it")
                    continue
                items.append(Item(folder, run_id))
            count += len(items)
            if items:
                groups.append(Group(root, self._folder_name(described, root), items))
        if count > MAX_SESSIONS:
            raise ValueError(f"At most {MAX_SESSIONS} sessions per upload; choose fewer")
        if not groups:
            raise ValueError("Nothing to upload: " + ("; ".join(skipped) or "no sessions chosen"))
        return groups, skipped

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
                try:
                    where: str | None = self.transport().destination(
                        self._folder_name(described, root)
                    )
                    where = f"{where}/{run_id}"
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

    # -- preview ----------------------------------------------------------------

    def preview(self, project_id: str, selection: Any) -> dict[str, Any]:
        """A dry run: what an upload of ``selection`` would copy, run by run."""
        groups, skipped = self.groups(project_id, selection)
        transport = self.transport()
        out = []
        for group in groups:
            planned = transport.plan(group.folder, group.items)
            out.append(
                {
                    "root": group.root.id,
                    "root_kind": group.root.kind,
                    "destination": transport.destination(group.folder),
                    "sessions": [
                        _session_preview(group.root, item, planned) for item in group.items
                    ],
                }
            )
        return {"groups": out, "skipped": skipped}

    # -- the job ------------------------------------------------------------------

    def job(self) -> dict[str, Any]:
        with self._lock:
            return {"job": dict(self._job) if self._job else None}

    def start(self, project_id: str, selection: Any, include_incomplete: bool) -> dict[str, Any]:
        groups, skipped = self.groups(project_id, selection)
        transport = self.transport()
        with self._lock:
            if self._job and self._job["phase"] in ("starting", "copying", "verifying"):
                raise ValueError("An upload is already running; wait for it or stop it")
            for group in groups:
                kept = []
                for item in group.items:
                    if include_incomplete or (item.source / "manifest.yaml").is_file():
                        kept.append(item)
                    else:
                        skipped.append(
                            f"{item.relative}: no manifest (the session never finished); "
                            "include incomplete sessions to upload it anyway"
                        )
                group.items = kept
            groups = [g for g in groups if g.items]
            if not groups:
                raise ValueError("Nothing to upload: " + "; ".join(skipped))
            self._cancel = threading.Event()
            self._job = {
                "id": uuid.uuid4().hex,
                "project": project_id,
                "phase": "starting",
                "started": now(),
                "finished": None,
                "skipped": skipped,
                "groups": [
                    {
                        "root": g.root.id,
                        "destination": transport.destination(g.folder),
                        "runs": [i.relative for i in g.items],
                    }
                    for g in groups
                ],
                "progress": {"bytes_done": 0, "bytes_total": 0, "file": ""},
                "results": {},
                "error": None,
            }
            job_id = self._job["id"]
            self._thread = threading.Thread(
                target=self._run, args=(job_id, transport, groups), daemon=True
            )
            self._thread.start()
            return {"job": dict(self._job)}

    def cancel(self) -> None:
        with self._lock:
            if not self._job or self._job["phase"] not in ("starting", "copying", "verifying"):
                raise ValueError("No upload is running")
        self._cancel.set()

    def _set(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            if self._job and self._job["id"] == job_id:
                self._job.update(fields)

    def _run(self, job_id: str, transport: Transport, groups: list[Group]) -> None:
        total = sum(size for g in groups for i in g.items for _, size in local_files(i))
        done_before = 0
        results: dict[str, dict[str, Any]] = {}
        phase, error = "done", None
        for group in groups:
            started = now()
            destination = transport.destination(group.folder)
            try:
                planned = transport.plan(group.folder, group.items)
                self._set(job_id, phase="copying")

                def report(p: Progress, base: int = done_before) -> None:
                    self._set(
                        job_id,
                        progress={
                            "bytes_done": base + p.bytes_done,
                            "bytes_total": total,
                            "file": p.file,
                        },
                    )

                transport.copy(group.folder, group.items, report, self._cancel)
                self._set(job_id, phase="verifying")
                bad = transport.verify(group.folder, group.items)
            except Cancelled as exc:
                phase, error = "cancelled", str(exc)
                self._receipts(group, destination, started, None, "cancelled", str(exc), results)
                break
            except (UploadError, OSError, ValueError, AlhazenError) as exc:
                phase, error = "failed", str(exc)
                self._receipts(group, destination, started, None, "failed", str(exc), results)
                break
            done_before += sum(size for i in group.items for _, size in local_files(i))
            self._receipts(group, destination, started, (planned, bad), None, None, results)
            self._set(job_id, results=dict(results))
        self._set(job_id, phase=phase, error=error, finished=now(), results=dict(results))

    def _receipts(
        self,
        group: Group,
        destination: str,
        started: str,
        outcome: tuple[list[FilePlan], list[tuple[str, str]]] | None,
        status: str | None,
        error: str | None,
        results: dict[str, dict[str, Any]],
    ) -> None:
        """Write one receipt per session of ``group`` and note it in ``results``."""
        settings = self.settings()
        for item in group.items:
            files = []
            for name, size in local_files(item):
                files.append(
                    {"path": name, "size": size, "sha256": sha256_file(item.source / name)}
                )
            manifest = item.source / "manifest.yaml"
            local_check = (
                verify_manifest(item.source, manifest)
                if manifest.is_file()
                else ["no manifest.yaml"]
            )
            receipt: dict[str, Any] = {
                "started": started,
                "finished": now(),
                "destination": {
                    "transport": settings.transport if self._transport is None else "custom",
                    "path": f"{destination}/{item.relative}",
                },
                "files": files,
                "local_manifest_problems": local_check,
                "alhazen": __version__,
            }
            if outcome is None:
                receipt.update(status=status, error=error, verified=False)
            else:
                planned, bad = outcome
                mine = [p for p in planned if p.item == item.relative]
                differing = [name for run, name in bad if run == item.relative]
                new = {p.path for p in mine if p.state == "new"}
                # A file this upload had to copy and that is not there whole
                # is missing; one that was there before and differs in
                # content is a conflict, left as it was.
                missing = [n for n in differing if n in new]
                conflicts = [n for n in differing if n not in new]
                if conflicts:
                    state = "conflict"
                elif missing:
                    state = "incomplete"
                else:
                    state = "verified"
                receipt.update(
                    status=state,
                    verified=state == "verified",
                    verify_method="content (rsync --checksum dry run, or SHA-256 for a "
                    "local folder)",
                    copied=[p.path for p in mine if p.state == "new"],
                    already_there=[p.path for p in mine if p.state != "new"],
                    conflicts=conflicts,
                    missing=missing,
                )
            path = write_receipt(group.root.path, item.relative, receipt)
            results[f"{group.root.id}:{item.relative}"] = {
                "status": receipt["status"],
                "receipt": path.name,
            }

    # -- routes -----------------------------------------------------------------

    def get(self, route: str, query: dict[str, list[str]]) -> dict[str, Any]:
        def arg(name: str) -> str:
            return query.get(name, [""])[0]

        if route == "settings":
            return self.describe_settings()
        if route == "check":
            return self.check()
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
        if route == "preview":
            return self.preview(_project(body), body.get("selection"))
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


def _session_preview(root: DataRoot, item: Item, planned: list[FilePlan]) -> dict[str, Any]:
    mine = [p for p in planned if p.item == item.relative]
    count = {
        state: sum(1 for p in mine if p.state == state) for state in ("new", "present", "conflict")
    }
    return {
        "run": item.relative,
        "complete": (item.source / "manifest.yaml").is_file(),
        "files": len(mine),
        "bytes": sum(p.size for p in mine),
        "new": count["new"],
        "new_bytes": sum(p.size for p in mine if p.state == "new"),
        "present": count["present"],
        "conflict": count["conflict"],
        "conflicts": [p.path for p in mine if p.state == "conflict"],
        "upload": latest(root.path, item.relative),
    }
