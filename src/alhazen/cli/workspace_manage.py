"""The experiment workspace's management pages, server side: the
Experiments page's registration, an experiment's General page (its notes,
its people, its rig files) and its History page.

Each page is a rendering of state this module reads and writes through the
owners of that state; it keeps none of its own:

- registration and the experiment's notes: `Workspace` (projects.json);
- subjects and experimenters: `PeopleRegistry` (cli/people.py);
- rig files: the experiment's own ``configs/rig-<name>.yaml``, validated with
  the same loader a launch uses. Shared rigs are read, never written;
- history: the workspace's launch records (runs/<id>/run.json, launch.json)
  and the session folders in the experiment's data folders (read through
  `workspace_data`), joined where a launch's console names its folder.

Every path a request carries is a name this module looks up again, never a
path it opens as given: a rig by its project-relative path checked with
`path_inside`, a launch by its id, a session by (data folder id, run id) as
the Data view checks them.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

from alhazen.cli.people import Conflict, PeopleRegistry
from alhazen.cli.workspace import (
    Workspace,
    _as_the_project_reads_it,
    _shared_rigs,
    path_inside,
)
from alhazen.cli.workspace_data import (
    SAVED_PAGES,
    DataView,
    _run_folder,
    data_roots,
)
from alhazen.config.loader import validate_rig
from alhazen.config.rigs import FILE_PREFIX, NAME_PATTERN, file_rig_name, rig_mapping
from alhazen.data.atomic import replace_atomically
from alhazen.errors import AlhazenError

# A workspace launch's own files the History page reads as text, by name.
LAUNCH_TEXTS = (
    "console.log",
    "launch.json",
    "run.json",
    "params.yaml",
    "rig.yaml",
    "rig-source.yaml",
)
LAUNCH_TEXT_BYTES = 256 * 1024
MAX_RIG_BYTES = 256 * 1024
# How far into a launch's console the History page looks for the lines that
# name the session folder it opened.
CONSOLE_HEAD_BYTES = 64 * 1024
# The console lines a session prints before trial 1 (cli/main.py), which
# name the folder it writes.
RUNNING_LINE = re.compile(r"^running (\S+): sub-(\S+) ses-([0-9]+) run-([0-9]+)\s*$", re.M)
FILED_LINE = re.compile(r"filed under (v[^/\s]+)/")
DATA_LINE = re.compile(r"^session complete — data under (.+)$", re.M)


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{what} must be text")
    return value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Management:
    """The management pages' reads and writes over one workspace."""

    def __init__(self, workspace: Workspace, data: DataView):
        self.workspace = workspace
        self.data = data

    # -- people ----------------------------------------------------------------

    def _people(self) -> PeopleRegistry:
        if self.workspace.people is None:
            raise ValueError(self.workspace.people_error or "The people registry is not open")
        return self.workspace.people

    def people(self, project_id: str) -> dict[str, Any]:
        """Everything the General page and the Run page's selectors show:
        the experiment's subjects (with whether a launch has used each), all
        experimenters and which are this experiment's, and the state of the
        CSV copies. ``error`` when the registry cannot be opened."""
        self.workspace.project(project_id)
        if self.workspace.people is None:
            return {
                "error": self.workspace.people_error,
                "subjects": [],
                "experimenters": [],
                "assigned": [],
                "export": None,
                "files": None,
            }
        registry = self.workspace.people
        used = self.workspace.used_subjects(project_id)
        subjects = [{**s, "used": s["id"] in used} for s in registry.subjects(project_id)]
        return {
            "error": None,
            "subjects": subjects,
            "experimenters": registry.experimenters(),
            "assigned": registry.assignments(project_id),
            "export": registry.export_status().as_json(),
            "files": {
                "database": str(registry.path),
                "subjects": str(registry.csv_path("subjects", project_id)),
                "experimenters": str(registry.csv_path("experimenters", None)),
                "assigned": str(registry.csv_dir / project_id / "experimenters.csv"),
                "backups": str(registry.backup_dir),
            },
            "records_experimenter": self.workspace.describe(project_id)["records_experimenter"],
        }

    def _sources(self, project_id: str) -> list[tuple[Path, str]]:
        existing, _missing, _problems = data_roots(self.workspace.describe(project_id))
        return [(root.path, root.kind) for root in existing]

    def people_action(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        registry = self._people()
        project_id = _text(body.get("project", ""), "The experiment")
        self.workspace.project(project_id)
        record: Any = None
        if action == "subject-add":
            record = registry.add_subject(project_id, body.get("fields"))
        elif action == "subject-update":
            used = _text(body.get("id"), "The subject") in self.workspace.used_subjects(project_id)
            record = registry.update_subject(
                project_id, body["id"], body.get("revision"), body.get("fields"), used=used
            )
        elif action == "subject-status":
            record = registry.set_subject_status(
                project_id,
                _text(body.get("id"), "The subject"),
                body.get("revision"),
                body.get("status"),
            )
        elif action == "experimenter-add":
            record = registry.add_experimenter(body.get("fields"))
            if body.get("assign", True):
                registry.assign(project_id, record["id"])
        elif action == "experimenter-update":
            record = registry.update_experimenter(
                _text(body.get("id"), "The experimenter"),
                body.get("revision"),
                body.get("fields"),
            )
        elif action == "experimenter-status":
            record = registry.set_experimenter_status(
                _text(body.get("id"), "The experimenter"),
                body.get("revision"),
                body.get("status"),
            )
        elif action == "assign":
            registry.assign(project_id, _text(body.get("experimenter"), "The experimenter"))
        elif action == "unassign":
            registry.unassign(project_id, _text(body.get("experimenter"), "The experimenter"))
        elif action == "export":
            registry.export_csv()
        elif action == "participants-apply":
            record = registry.apply_participants_import(
                project_id, self._sources(project_id), _text(body.get("digest"), "The preview")
            )
        elif action == "csv-apply":
            kind = _text(body.get("kind"), "The list")
            record = registry.apply_csv_import(
                kind,
                project_id if kind == "subjects" else None,
                _text(body.get("digest"), "The preview"),
                self.workspace.used_subjects(project_id),
            )
        else:
            raise FileNotFoundError(f"No people action {action!r}")
        return {"record": record, "people": self.people(project_id)}

    def participants_plan(self, project_id: str) -> dict[str, Any]:
        self.workspace.project(project_id)
        return self._people().plan_participants_import(project_id, self._sources(project_id))

    def csv_plan(self, project_id: str, kind: str) -> dict[str, Any]:
        self.workspace.project(project_id)
        return self._people().plan_csv_import(kind, project_id if kind == "subjects" else None)

    # -- rigs ------------------------------------------------------------------------

    def rig_file(self, project_id: str, path: str) -> dict[str, Any]:
        """One rig file's text, for the General page's editor: an experiment
        rig (``configs/…``, editable, with its sha256 so a save can tell it
        changed meanwhile) or a shared one (read only)."""
        project = self.workspace.project(project_id)
        shared = _shared_rigs(project) or {}
        if path.startswith("alhazen/"):
            name = path.removeprefix("alhazen/")
            if name not in shared:
                raise ValueError(f"{project['name']}'s alhazen ships no shared rig {name!r}")
            target, editable = Path(shared[name]), False
        else:
            target, editable = self._own_rig(project, path), True
            if not target.is_file():
                raise FileNotFoundError(f"{path} does not exist")
        data = target.read_bytes()
        return {
            "path": path,
            "file": str(target),
            "editable": editable,
            "text": data.decode("utf-8-sig", errors="replace"),
            "sha256": _sha256(data),
        }

    @staticmethod
    def _own_rig(project: dict[str, Any], path: str) -> Path:
        root = Path(project["path"])
        target = path_inside(root, path)
        configs = (root / "configs").resolve()
        if not target.is_relative_to(configs):
            raise ValueError("An experiment's rigs are under its configs/ folder")
        if not (target.name.startswith(FILE_PREFIX) and target.suffix in {".yaml", ".yml"}):
            raise ValueError("A rig file is named rig-<name>.yaml")
        if target.name.endswith("_gamma.yaml"):
            raise ValueError("A gamma file is written by Measure rig, not edited here")
        return target

    def check_rig(self, project_id: str, text: str, path: str | None = None) -> dict[str, Any]:
        """Validate rig YAML as a launch would: parsed safely, merged over the
        shared rig it extends (the project's alhazen's), read as that alhazen
        reads it, validated. Nothing is written."""
        project = self.workspace.project(project_id)
        if len(text.encode("utf-8")) > MAX_RIG_BYTES:
            raise ValueError("A rig file must be at most 256 KiB")
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"Not valid YAML: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("A rig file is a YAML mapping (key: value lines)")
        shared = _shared_rigs(project)
        name = Path(path).name if path else "rig-new.yaml"
        with tempfile.TemporaryDirectory(prefix="alhazen-rig-") as scratch:
            candidate = Path(scratch) / name
            candidate.write_text(text, encoding="utf-8")
            try:
                merged = rig_mapping(candidate, shared=shared)
                validate_rig(
                    _as_the_project_reads_it(merged, project.get("alhazen_version")), candidate
                )
            except (AlhazenError, OSError) as exc:
                message = str(exc).replace(str(candidate), name)
                raise ValueError(message) from exc
        return {"extends": merged.extends, "values": merged.values, "valid": True}

    def save_rig(self, body: dict[str, Any]) -> dict[str, Any]:
        """Write an experiment rig: a new ``configs/rig-<name>.yaml`` (never
        over an existing file) or an existing one whose text is still the
        text the editor opened (``sha256``). Validated first, written
        atomically; shared rigs are never written."""
        project_id = _text(body.get("project"), "The experiment")
        project = self.workspace.project(project_id)
        text = _text(body.get("text"), "The rig")
        root = Path(project["path"])
        if body.get("path"):
            target = self._own_rig(project, _text(body["path"], "The rig's path"))
            if not target.is_file():
                raise FileNotFoundError(f"{body['path']} no longer exists")
            if _sha256(target.read_bytes()) != body.get("sha256"):
                raise Conflict(
                    f"{body['path']} changed on disk since it was opened; nothing was saved. "
                    "Open it again to see the current text."
                )
            self.check_rig(project_id, text, str(target))
            replace_atomically(target, text)
        else:
            name = _text(body.get("name"), "The rig's name").strip()
            if not NAME_PATTERN.fullmatch(name) or len(name) > 64:
                raise ValueError(
                    "A rig name is letters, digits, '.', '_' or '-', starting with a letter or "
                    "digit"
                )
            taken = (
                [
                    p
                    for p in (root / "configs").rglob(f"{FILE_PREFIX}*")
                    if file_rig_name(p) == name and p.suffix in {".yaml", ".yml"}
                ]
                if (root / "configs").is_dir()
                else []
            )
            if taken:
                raise ValueError(
                    f"The experiment already has a rig {name!r} "
                    f"({taken[0].relative_to(root).as_posix()}); open it to edit it"
                )
            target = root / "configs" / f"{FILE_PREFIX}{name}.yaml"
            self.check_rig(project_id, text, str(target))
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8", newline="") as stream:
                stream.write(text)
        relative = target.relative_to(root).as_posix()
        return {"path": relative, **self.rig_file(project_id, relative)}

    # -- history ------------------------------------------------------------------------

    def history(self, project_id: str) -> dict[str, Any]:
        """The experiment's previous sessions: the workspace's launches and
        the session folders in its data folders, each joined to the other
        where a launch's console names the folder it opened. Unknown values
        stay unknown (None) — a launch from before the workspace kept the
        experimenter, a session from before session.json said who ran it."""
        described = self.workspace.describe(project_id)
        existing, missing, problems = data_roots(described)
        sessions: list[dict[str, Any]] = []
        by_folder: dict[str, dict[str, Any]] = {}
        for root in existing:
            try:
                listed = self.data.runs(project_id, root.id)
            except (OSError, ValueError, AlhazenError) as exc:
                problems.append(f"{root.path} cannot be listed: {exc}")
                continue
            problems.extend(listed["problems"])
            for row in listed["runs"]:
                folder = root.path / row["id"]
                entry = {
                    **row,
                    "root": root.id,
                    "root_path": str(root.path),
                    "root_kind": root.kind,
                    "experimenter": _session_experimenter(folder),
                    "page": next((n for n in SAVED_PAGES if (folder / n).is_file()), None),
                    "has_log": any(folder.glob("*session.log")),
                    "launch": None,
                }
                sessions.append(entry)
                by_folder[str(folder.resolve())] = entry
        launches = []
        with self.workspace.lock:
            runs = [dict(r) for r in self.workspace.runs.values() if r["project"] == project_id]
        runs.sort(key=lambda r: r["started"], reverse=True)
        for run in runs:
            directory = self.workspace.directory / "runs" / run["id"]
            folder = _console_run_folder(directory / "console.log", existing, run["mode"])
            session = by_folder.get(folder) if folder else None
            if session is not None:
                session["launch"] = run["id"]
            identity = run.get("identity")
            launches.append(
                {
                    "id": run["id"],
                    "mode": run["mode"],
                    "task": run.get("task"),
                    "parameter_set": run.get("parameter_set"),
                    "rig": run.get("rig_name") or run.get("rig"),
                    "status": run["status"],
                    "started": run["started"],
                    "finished": run.get("finished"),
                    "subject": run.get("subject"),
                    "initials": run.get("initials"),
                    "session": run.get("session"),
                    "seed": run.get("seed"),
                    "identity": identity,
                    "experimenter": (identity or {}).get("experimenter"),
                    "experimenter_recorded_in": run.get("experimenter_recorded_in"),
                    "files": [name for name in LAUNCH_TEXTS if (directory / name).is_file()],
                    "session_folder": (
                        {"root": session["root"], "run": session["id"]} if session else None
                    ),
                    "active": run["id"] == self.workspace.active,
                }
            )
        return {
            "launches": launches,
            "sessions": sessions,
            "roots": [root.as_json() for root in existing],
            "missing": [root.as_json() for root in missing],
            "problems": problems,
        }

    def launch_text(self, project_id: str, run_id: str, name: str) -> dict[str, Any]:
        """One of a launch's own files as text (LAUNCH_TEXTS): whole when
        small, the console's tail otherwise. The page shows it as text."""
        if name not in LAUNCH_TEXTS:
            raise ValueError(f"{name!r} is not a launch record the History page shows")
        with self.workspace.lock:
            run = self.workspace.runs.get(run_id)
        if run is None or run["project"] != project_id:
            raise FileNotFoundError("Unknown launch")
        directory = self.workspace.directory / "runs" / run_id
        path = path_inside(directory, name)
        if not path.is_file():
            raise FileNotFoundError(f"This launch has no {name}")
        size = path.stat().st_size
        with path.open("rb") as stream:
            if name == "console.log":
                stream.seek(max(0, size - LAUNCH_TEXT_BYTES))
            data = stream.read(LAUNCH_TEXT_BYTES)
        return {
            "name": name,
            "size": size,
            "truncated": size > LAUNCH_TEXT_BYTES,
            "tail": name == "console.log",
            "text": data.decode("utf-8", errors="replace"),
        }

    def session_file(self, project_id: str, root_id: str, run_id: str, name: str) -> Path:
        """Any file of a session folder, for download: looked up by the data
        folder's id and the run's id as the Data view checks them, and kept
        inside the run folder (symlinks resolved)."""
        root = self.data._root(project_id, root_id)
        folder = _run_folder(root.path, run_id)
        target = path_inside(folder, name)
        if not target.is_file():
            raise FileNotFoundError(f"{name} is not in the run {run_id}")
        return target

    # -- the routes ------------------------------------------------------------------------

    def get(self, route: str, query: dict[str, list[str]]) -> dict[str, Any]:
        def arg(name: str) -> str:
            return query.get(name, [""])[0]

        if route == "people":
            return self.people(arg("project"))
        if route == "participants-plan":
            return self.participants_plan(arg("project"))
        if route == "csv-plan":
            return self.csv_plan(arg("project"), arg("kind"))
        if route == "rig-file":
            return self.rig_file(arg("project"), arg("path"))
        if route == "history":
            return self.history(arg("project"))
        if route == "launch-text":
            return self.launch_text(arg("project"), arg("run"), arg("name"))
        raise FileNotFoundError(f"No management route {route!r}")

    def post(self, route: str, body: dict[str, Any]) -> dict[str, Any]:
        workspace = self.workspace
        if route.startswith("people/"):
            return self.people_action(route.removeprefix("people/"), body)
        if route == "rig-check":
            return self.check_rig(
                _text(body.get("project"), "The experiment"),
                _text(body.get("text"), "The rig"),
                body.get("path") if isinstance(body.get("path"), str) else None,
            )
        if route == "rig-save":
            return self.save_rig(body)
        if route == "register":
            path = _text(body.get("path"), "The experiment's folder")
            python = _text(body.get("python", ""), "The interpreter")
            return workspace.register(path, python)
        if route == "meta":
            return workspace.update_meta(
                _text(body.get("project"), "The experiment"), body.get("fields")
            )
        if route == "archive":
            return workspace.set_archived(
                _text(body.get("project"), "The experiment"), body.get("archived")
            )
        raise FileNotFoundError(f"No management route {route!r}")


def _session_experimenter(folder: Path) -> dict[str, Any]:
    """Who ran a session, as its session.json says: ``{recorded, id, name}``
    — recorded False for a card from before the field (or no card)."""
    card = folder / "session.json"
    try:
        value = json.loads(card.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"recorded": False, "id": None, "name": None}
    if not isinstance(value, dict) or "experimenter" not in value:
        return {"recorded": False, "id": None, "name": None}
    who = value.get("experimenter")
    if not isinstance(who, dict):
        return {"recorded": True, "id": None, "name": None}
    return {"recorded": True, "id": who.get("id"), "name": who.get("name")}


def _console_run_folder(console: Path, roots: list[Any], mode: str | None) -> str | None:
    """The session folder a launch's console names (resolved), when it is
    under one of the experiment's data folders; None otherwise.

    A session prints which run it is before trial 1 (cli/main.py): ``running
    <task>: sub-<id> ses-<NNN> run-<NN>``, after ``filed under v<version>/``;
    and ``session complete — data under <folder>`` at the end. The folder is
    put together from those and must exist: in the folder the console names
    when it names one, else in the one data folder of the launch's kind (real
    for run, rehearsal otherwise) that holds it. Two candidates and no
    folder named is no answer rather than a guess; so is a console from
    before those lines, or one that never got that far.
    """
    if not console.is_file():
        return None
    with console.open("rb") as stream:
        head = stream.read(CONSOLE_HEAD_BYTES).decode("utf-8", errors="replace")
    running = RUNNING_LINE.search(head)
    if running is None:
        return None
    task, subject, session, run = running.groups()
    run_id = f"sub-{subject}/ses-{session}/run-{run}_task-{task}"
    version = FILED_LINE.search(head)
    if version:
        run_id = f"{version.group(1)}/{run_id}"
    named = DATA_LINE.search(head)
    if named is not None:
        candidates = [r for r in roots if str(r.path.resolve()) == named.group(1).strip()]
    else:
        kind = "real" if mode == "run" else "rehearsal"
        candidates = [r for r in roots if r.kind == kind]
    found = []
    for root in candidates:
        # Kept inside its data folder: the ID comes from a console's text.
        folder = (root.path / run_id).resolve()
        if folder.is_dir() and folder.is_relative_to(root.path.resolve()):
            found.append(str(folder))
    return found[0] if len(found) == 1 else None
