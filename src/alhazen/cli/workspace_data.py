"""The experiment workspace's Data view, server side: read saved sessions.

The Data view (`assets/workspace_data.js`) lets a person pick one of a
project's data folders, browse the runs in it, read one run's records and
load its CSV tables. Everything here READS files; nothing imports the
experiment's code and nothing is ever written into a data folder.

Where the data is. A project does not say where its data goes — its rigs do
(`data_root`), and a rehearsal (test, simulate) writes to the sibling
``<data_root>-rehearsal`` (`alhazen.modes.rehearsal.rehearsal_root`). So the
folders offered are computed from the project's rigs, the experiment's own
and the shared ones its alhazen ships, merged the way a launch merges them
(`rig_mapping`). The browser never names a folder: it sends back the id of
one this module computed, and the id is looked up again on every request.

What the browser may name. Run ids are paths relative to the data folder
(``v0.1.0/sub-01/ses-001/run-01_task-x``); file names are relative to the run
folder. Each is checked twice before it is used: its SHAPE (a run id must
look like a folder `find_runs` lists; a record must be one of the known
names) and its LOCATION (`path_inside`, which resolves symlinks, so a link
pointing out of the folder is refused like ``..`` is).

Numbers. CSV cells are sent as the exact text of the file, never parsed
here: a CSV has no types, the text is the truth, and the page decides per
column whether it sorts as numbers (`assets/workspace_data.js`,
`numericColumn`). Parsing on the server would guess once for everyone and
lose "1.50" vs "1.5".

Extension point (not implemented): experiment figures. An experiment will
later be able to declare its own analysis figures — a function of a run
folder (or of several) that returns a figure — and the Data view will list
them in a card of their own. When that lands, it belongs in a new route here
that RUNS in the project's interpreter (like `workspace._read_schema`), since
this module never imports experiment code; see docs/workspace.md §Data.
"""

from __future__ import annotations

import csv
import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from alhazen.cli.workspace import MEDIA_TYPES, Workspace, _shared_rigs, path_inside
from alhazen.config.rigs import SHARED_PREFIX, file_rig_name, rig_mapping
from alhazen.data import naming
from alhazen.data.paths import RunFolder, find_runs
from alhazen.errors import AlhazenError
from alhazen.modes.rehearsal import rehearsal_root

# At most this many table rows go to the browser per request, pooled runs
# together. A frames CSV has one row per display frame (61 564 for one
# 20-minute rehearsal); past this the JSON would take seconds to build and
# the page seconds to parse. The answer says when the cap cut it (`capped`),
# with the file's full row count, so a plot is never silently partial.
MAX_TABLE_ROWS = 50_000
# How much of a text record is sent: all of a small file, the END of the log
# (the last lines are the ones that say how a session ended).
MAX_TEXT_BYTES = 256 * 1024
LOG_TAIL_BYTES = 64 * 1024
# The files one run folder lists at most; a run folder holds a dozen.
MAX_FILES = 2000
# How long a link to a saved monitor page stays valid (see `page_ticket`).
TICKET_TTL_S = 120.0
MAX_TICKETS = 64

# The text records the viewer shows, in the order it offers them. Nothing
# else is read as text: the name is the allowlist.
TEXT_RECORDS = (
    "session.json",
    "config_snapshot.yaml",
    "rig.yaml",
    "rig-source.yaml",
    "params.yaml",
    "report.yaml",
    "manifest.yaml",
    "session.log",
)
# The tables a run folder holds: `<base>_<kind>.csv` (data.paths.SessionPaths).
TABLE_KINDS = ("trials", "events", "frames", "paradigm")
# The saved live monitor page, as alhazen.live_monitor.runtime.SAVED_PAGE
# names it since 2.0, then its pre-2.0 name. Spelled here rather than
# imported: importing the monitor's runtime would pull its server and
# multiprocessing machinery into the dashboard for one string. A test pins
# the first to the runtime's constant.
SAVED_PAGES = ("figures/live_monitor.html", "figures/dashboard.html")
# Images a run's figures/ folder may hold. SVG too: it is served only with a
# sandboxing Content-Security-Policy (dashboard.DATA_FILE_CSP), so a script
# inside one never runs in the workspace's origin.
IMAGE_TYPES = {
    **{suffix: kind for suffix, kind in MEDIA_TYPES.items() if kind.startswith("image/")},
    ".svg": "image/svg+xml",
}
# The C loader when PyYAML was built with it: listing a large folder of
# pre-2.0 runs parses one config_snapshot.yaml per run.
_YAML_LOADER: Any = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _root_id(path: Path) -> str:
    """A data folder's id: stable for the same folder, meaningless on its own
    (the browser cannot turn it back into a path; only this module can)."""
    return hashlib.sha256(str(path).encode()).hexdigest()[:16]


@dataclass
class DataRoot:
    """One folder a project's data lands in, and who writes there."""

    id: str
    path: Path
    kind: str  # "real" or "rehearsal"
    rigs: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": str(self.path),
            "name": self.path.name,
            "kind": self.kind,
            "rigs": self.rigs,
        }


def data_roots(described: dict[str, Any]) -> tuple[list[DataRoot], list[DataRoot], list[str]]:
    """A project's data folders, from its rigs: ``(existing, missing, problems)``.

    ``described`` is `Workspace.describe`'s record: its ``rigs`` are the Rig
    menu's entries (the experiment's own, then the shared ones its alhazen
    ships). Each rig's merged ``data_root`` — relative to the project folder
    when relative, since a launch runs there — is a real root, and its
    rehearsal sibling a rehearsal root. Rigs sharing a folder are listed
    together on it. A rig that cannot be read, or names no ``data_root``, is
    a problem, said by name, never skipped quietly: the folder it would have
    added is exactly what the reader may be looking for.
    """
    project_dir = Path(described["path"])
    shared = _shared_rigs(described)
    found: dict[tuple[Path, str], DataRoot] = {}
    problems: list[str] = []
    if described.get("rigs_note"):
        problems.append(described["rigs_note"])
    # The experiment's own rigs are named `<experiment>/<rig>`, the way the
    # Rig menu and `--rig` name them, so a folder's list of writers reads the
    # same as the menu. The slug is the pyproject's [project] name (describe()
    # reads it); a record without one falls back to the registry's name.
    owner = described.get("slug") or described.get("name")
    for entry in described.get("rigs", []):
        if entry.get("shadowed"):
            # A shared rig hidden by the experiment's own rig of the same name
            # is not in the Rig menu, so it is not listed as a writer either:
            # the dashboard cannot launch it, and naming it here would offer a
            # rig the page nowhere else shows.
            continue
        label = (
            f"{SHARED_PREFIX}{entry['name']}"
            if entry["source"] == "alhazen"
            else f"{owner}/{entry['name']}"
        )
        if entry.get("error"):
            problems.append(f"Rig {label} cannot be read: {entry['error']}")
            continue
        rig_file = Path(entry["path"])
        if not rig_file.is_absolute():
            rig_file = project_dir / rig_file
        try:
            values = rig_mapping(rig_file, shared=shared).values
        except (AlhazenError, OSError) as exc:
            problems.append(f"Rig {label} cannot be read: {exc}")
            continue
        raw = values.get("data_root")
        if not isinstance(raw, str) or not raw.strip():
            problems.append(f"Rig {label} names no data_root, so it has no data folder")
            continue
        real = Path(raw).expanduser()
        if not real.is_absolute():
            real = project_dir / real
        # Resolved so two spellings of one folder ("data", "./data") are one
        # entry, and so the id is the folder's, not the spelling's.
        real = real.resolve()
        for path, kind in ((real, "real"), (rehearsal_root(real), "rehearsal")):
            root = found.setdefault((path, kind), DataRoot(_root_id(path), path, kind))
            if label not in root.rigs:
                root.rigs.append(label)
    roots = sorted(found.values(), key=lambda r: (str(r.path), r.kind))
    existing = [root for root in roots if root.path.is_dir()]
    missing = [root for root in roots if not root.path.is_dir()]
    return existing, missing, problems


def _run_folder(root: Path, run_id: str) -> Path:
    """The run folder ``run_id`` names under ``root``, or ValueError.

    The shape is checked first — ``[v<version>/]sub-<ID>/ses-<NNN>/run-<NN>…``,
    exactly the levels `find_runs` accepts — so only something that could be
    a listed run is ever looked at; then its location (`path_inside`, which
    resolves symlinks). ``..``, an absolute path and a backslash all fail the
    shape; a symlink out of the folder fails the location.
    """
    parts = run_id.split("/")
    shaped = (
        len(parts) in (3, 4)
        and (len(parts) == 3 or naming.parse_version_dirname(parts[0]) is not None)
        and parts[-3].startswith("sub-")
        and len(parts[-3]) > len("sub-")
        and parts[-2].startswith("ses-")
        and parts[-2][4:].isascii()
        and parts[-2][4:].isdigit()
        and naming.parse_run_dirname(parts[-1]) is not None
        and "\\" not in run_id
    )
    if not shaped:
        raise ValueError(f"Not a run folder name: {run_id!r}")
    folder = path_inside(root, run_id)
    if not folder.is_dir():
        raise FileNotFoundError(f"The run {run_id} is no longer in {root}")
    return folder


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def _read_yaml(path: Path) -> dict[str, Any]:
    value = yaml.load(path.read_text(encoding="utf-8"), Loader=_YAML_LOADER)
    if not isinstance(value, dict):
        raise ValueError("expected a YAML mapping")
    return value


def _table_file(folder: Path, kind: str, card: dict[str, Any] | None) -> Path | None:
    """The run's ``<kind>`` CSV: the name session.json records when it has
    one, else the one ``*_<kind>.csv`` in the folder. Two candidates are an
    error rather than a guess."""
    named = ((card or {}).get("files") or {}).get(kind)
    if isinstance(named, str):
        path = path_inside(folder, named)
        return path if path.is_file() else None
    candidates = sorted(folder.glob(f"*_{kind}.csv"))
    if len(candidates) > 1:
        names = ", ".join(p.name for p in candidates)
        raise ValueError(f"{folder.name} holds several {kind} tables ({names}); cannot choose")
    return candidates[0] if candidates else None


def _count_lines(path: Path) -> int:
    """Data rows in a CSV by counting line ends, without parsing it: the
    listing must stay quick on a folder of hundreds of runs. A quoted cell
    holding a line break would make this over-count, which is why the
    listing marks such a count as approximate (``trials_counted: "lines"``)."""
    count = 0
    last = b"\n"
    with path.open("rb") as stream:
        while block := stream.read(1 << 20):
            count += block.count(b"\n")
            last = block[-1:]
    if last != b"\n":
        count += 1  # a last line with no line end is still a row
    return max(0, count - 1)  # the header


def _summary(root: Path, folder: Path, found: RunFolder) -> dict[str, Any]:
    """One row of the run browser: what the path says, then what the run's
    small records say. Only small files are read (session.json, report.yaml,
    and for a pre-2.0 run its config_snapshot.yaml); a record that cannot be
    read is named in the row's ``problems``, and the row is still listed."""
    row: dict[str, Any] = {
        "id": folder.relative_to(root).as_posix(),
        "version": found.experiment_version,
        "layout": "pre-2.0" if found.experiment_version is None else "2.0",
        "subject": found.subject,
        "initials": None,
        "session": found.session,
        "run": found.run,
        "task": found.task,
        "mode": None,
        "date": None,
        "rig": None,
        "trials": None,
        "trials_counted": None,
        "problems": [],
    }
    card = None
    if (folder / "session.json").is_file():
        try:
            card = _read_json(folder / "session.json")
        except (OSError, ValueError) as exc:
            row["problems"].append(f"session.json cannot be read: {exc}")
    if card is not None:
        subject = card.get("subject") or {}
        row["initials"] = subject.get("initials") if isinstance(subject, dict) else None
        row["mode"] = card.get("mode")
        row["task"] = card.get("task") or row["task"]
        stamp = card.get("date")
        if isinstance(stamp, str) and len(stamp) == 8 and stamp.isdigit():
            row["date"] = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"
        rig = card.get("rig") or {}
        if isinstance(rig, dict):
            row["rig"] = rig.get("name") or (
                file_rig_name(Path(rig["file"])) if rig.get("file") else None
            )
    elif (folder / "config_snapshot.yaml").is_file():
        # A pre-2.0 run (or a 2.0 run whose card is missing): what the
        # snapshot says. It has no mode; the folder's kind says real or
        # rehearsal.
        try:
            snapshot = _read_yaml(folder / "config_snapshot.yaml")
            # The snapshot keeps `sources` inside its `config` section
            # (config/snapshot.py), beside `provenance`.
            sources = (snapshot.get("config") or {}).get("sources") or {}
            if isinstance(sources.get("rig"), str):
                row["rig"] = file_rig_name(Path(sources["rig"]))
            created = (snapshot.get("provenance") or {}).get("created")
            if isinstance(created, str):
                row["date"] = created[:10]
        except (OSError, ValueError, yaml.YAMLError, AttributeError) as exc:
            row["problems"].append(f"config_snapshot.yaml cannot be read: {exc}")
    else:
        row["problems"].append("no session.json or config_snapshot.yaml: the run did not start")
    # The trial count: from report.yaml when the run wrote one (exact), else
    # by counting the trials file's lines (approximate, see _count_lines).
    report = folder / "report.yaml"
    if report.is_file():
        try:
            rows = (_read_yaml(report).get("trials") or {}).get("n_rows")
            if isinstance(rows, int):
                row["trials"], row["trials_counted"] = rows, "report"
        except (OSError, ValueError, yaml.YAMLError, AttributeError) as exc:
            row["problems"].append(f"report.yaml cannot be read: {exc}")
    if row["trials"] is None:
        try:
            trials = _table_file(folder, "trials", card)
            if trials is not None:
                row["trials"], row["trials_counted"] = _count_lines(trials), "lines"
        except (OSError, ValueError) as exc:
            row["problems"].append(str(exc))
    return row


class DataView:
    """The Data view's reads, over one workspace's registered projects.

    Holds no copy of anything on disk: every request recomputes the
    project's data folders from its rigs, so a rig edited or a folder
    deleted since the last request is seen at once. The one state kept is
    the short-lived links to saved monitor pages (`page_ticket`).
    """

    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self._tickets: dict[str, tuple[Path, float]] = {}
        self._ticket_lock = threading.Lock()

    # -- data folders --------------------------------------------------

    def roots(self, project_id: str) -> dict[str, Any]:
        existing, missing, problems = data_roots(self.workspace.describe(project_id))
        return {
            "roots": [root.as_json() for root in existing],
            "missing": [root.as_json() for root in missing],
            "problems": problems,
        }

    def _root(self, project_id: str, root_id: str) -> DataRoot:
        existing, missing, _ = data_roots(self.workspace.describe(project_id))
        for root in existing:
            if root.id == root_id:
                return root
        for root in missing:
            if root.id == root_id:
                raise FileNotFoundError(f"The data folder {root.path} no longer exists")
        raise FileNotFoundError(
            "Unknown data folder: it is not one this project's rigs write to (a rig may "
            "have changed); pick a folder again"
        )

    # -- runs ------------------------------------------------------------

    def runs(self, project_id: str, root_id: str) -> dict[str, Any]:
        """Every run in one data folder, newest version first, as rows."""
        root = self._root(project_id, root_id)
        top = root.path.resolve()
        rows, problems = [], []
        for found in find_runs(root.path):
            if not found.path.resolve().is_relative_to(top):
                problems.append(
                    f"{found.path.relative_to(root.path).as_posix()} is skipped: it links to a "
                    "folder outside this data folder"
                )
                continue
            rows.append(_summary(root.path, found.path, found))
        return {"root": root.as_json(), "runs": rows, "problems": problems}

    def run(self, project_id: str, root_id: str, run_id: str) -> dict[str, Any]:
        """One run's detail: its row, its card, its files, what can be viewed."""
        root = self._root(project_id, root_id)
        folder = _run_folder(root.path, run_id)
        top = folder.resolve()
        # What the path says, as find_runs would say it; _run_folder has
        # already held the id to find_runs' shape, so no walk of the folder.
        parts = run_id.split("/")
        found = RunFolder(
            path=root.path / run_id,
            experiment_version=naming.parse_version_dirname(parts[0]) if len(parts) == 4 else None,
            subject=parts[-3].removeprefix("sub-"),
            session=int(parts[-2][4:]),
            run=naming.parse_run_dirname(parts[-1]) or 0,
            task=naming.parse_run_task(parts[-1]),
        )
        row = _summary(root.path, found.path, found)
        card, card_error = None, None
        if (folder / "session.json").is_file():
            try:
                card = _read_json(folder / "session.json")
            except (OSError, ValueError) as exc:
                card_error = f"session.json cannot be read: {exc}"
        files: list[dict[str, Any]] = []
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or not path.resolve().is_relative_to(top):
                continue
            files.append({"name": path.relative_to(folder).as_posix(), "size": path.stat().st_size})
            if len(files) >= MAX_FILES:
                break
        names: set[str] = {entry["name"] for entry in files}
        tables = []
        problems = list(row["problems"])
        for kind in TABLE_KINDS:
            try:
                table = _table_file(folder, kind, card)
            except (OSError, ValueError) as exc:
                problems.append(str(exc))
                continue
            if table is not None:
                tables.append({"kind": kind, "name": table.relative_to(top).as_posix()})
        return {
            **row,
            "problems": problems,
            "path": str(folder),
            "card": card,
            "card_error": card_error,
            "files": files,
            "files_capped": len(files) >= MAX_FILES,
            "texts": [name for name in TEXT_RECORDS if name in names],
            "tables": tables,
            "images": [
                name
                for name in sorted(names)
                if name.startswith("figures/") and Path(name).suffix.lower() in IMAGE_TYPES
            ],
            "page": next((name for name in SAVED_PAGES if name in names), None),
        }

    def text(self, project_id: str, root_id: str, run_id: str, name: str) -> dict[str, Any]:
        """One of the run's text records (TEXT_RECORDS), whole when small; the
        log's tail; any other file's head, marked ``truncated``."""
        if name not in TEXT_RECORDS:
            raise ValueError(f"{name!r} is not a record the viewer shows")
        folder = _run_folder(self._root(project_id, root_id).path, run_id)
        path = path_inside(folder, name)
        size = path.stat().st_size
        limit = LOG_TAIL_BYTES if name == "session.log" else MAX_TEXT_BYTES
        with path.open("rb") as stream:
            if name == "session.log":
                stream.seek(max(0, size - limit))
            data = stream.read(limit)
        return {
            "name": name,
            "size": size,
            "truncated": size > limit,
            "tail": name == "session.log",
            "text": data.decode("utf-8", errors="replace"),
        }

    # -- tables ------------------------------------------------------------

    def table(self, project_id: str, root_id: str, run_ids: list[str], kind: str) -> dict[str, Any]:
        """One table kind of one or more runs, pooled, as text cells.

        With several runs, three columns are added in front — the run
        folder's id, its subject and session — so pooled rows stay
        distinguishable; a CSV column of the same name keeps its own and
        the added one is called ``<name> (folder)``. Columns are the union
        of the runs', in first-seen order, and a run without one gets ''.
        At most MAX_TABLE_ROWS rows are sent; ``total`` counts them all.
        A run with no such table, or a file that cannot be parsed, fails
        the whole request with the run and the line named: a pooled table
        silently missing one run would plot as if it were complete.
        """
        if kind not in TABLE_KINDS:
            raise ValueError(f"Unknown table {kind!r}; choose one of {', '.join(TABLE_KINDS)}")
        if not run_ids:
            raise ValueError("Choose at least one run")
        if len(set(run_ids)) != len(run_ids):
            raise ValueError("A run was chosen twice")
        root = self._root(project_id, root_id)
        pooled = len(run_ids) > 1
        sources: list[tuple[str, list[str], list[list[str]], int, Path]] = []
        columns: list[str] = []
        total = 0
        problems: list[str] = []
        for run_id in run_ids:
            folder = _run_folder(root.path, run_id)
            card = None
            if (folder / "session.json").is_file():
                try:
                    card = _read_json(folder / "session.json")
                except (OSError, ValueError):
                    card = None  # said in the run's own detail; the glob finds the file
            path = _table_file(folder, kind, card)
            if path is None:
                raise FileNotFoundError(f"The run {run_id} has no {kind} table")
            header, rows, count = _read_csv(path, MAX_TABLE_ROWS - total, run_id, problems)
            total += count
            for name in header:
                if name not in columns:
                    columns.append(name)
            sources.append((run_id, header, rows, count, path))
        added: list[str] = []
        if pooled:
            added = [
                f"{name} (folder)" if name in columns else name
                for name in ("run", "subject", "session")
            ]
        out: list[list[str]] = []
        for run_id, header, rows, _, _ in sources:
            where = {name: index for index, name in enumerate(header)}
            parts = run_id.split("/")
            extra = [run_id, parts[-3].removeprefix("sub-"), str(int(parts[-2][4:]))]
            for cells in rows:
                values = [cells[where[c]] if c in where else "" for c in columns]
                out.append(extra + values if pooled else values)
        return {
            "kind": kind,
            "columns": added + columns,
            "added": added,
            "rows": out,
            "total": total,
            "capped": total > len(out),
            "limit": MAX_TABLE_ROWS,
            "files": [
                # `loaded` < `rows` for a file the cap cut, down to 0 for a
                # run that came after the cap in a pool: said per file, so a
                # pooled plot is never read as covering every run.
                {"run": run_id, "name": path.name, "rows": count, "loaded": len(rows)}
                for run_id, _, rows, count, path in sources
            ],
            "problems": problems,
        }

    # -- files and saved pages -----------------------------------------------

    def file(self, project_id: str, root_id: str, run_id: str, name: str) -> tuple[Path, str]:
        """An image under the run's figures/, with its content type."""
        folder = _run_folder(self._root(project_id, root_id).path, run_id)
        path = path_inside(folder, name)
        kind = IMAGE_TYPES.get(path.suffix.lower())
        if kind is None or not name.startswith("figures/"):
            raise ValueError("Only images under a run's figures/ folder are served")
        if not path.is_file():
            raise FileNotFoundError(f"{name} is not in the run {run_id}")
        return path, kind

    def page_ticket(self, project_id: str, root_id: str, run_id: str, name: str) -> dict[str, Any]:
        """A short-lived link to the run's saved live monitor page.

        The page is a self-contained HTML file with inline script, so it
        cannot run under the workspace's CSP, and it reads sessionStorage, so
        it cannot run in a sandboxed (opaque) origin either. It is therefore
        served same-origin under its own strict CSP (no network, no frames;
        dashboard.SAVED_PAGE_CSP) — and reached through a random ticket
        rather than the API token, because a URL opened in a tab is visible
        to the page's script and kept in the browser's history, and the
        token must be in neither. A ticket names one file and lapses after
        TICKET_TTL_S.
        """
        if name not in SAVED_PAGES:
            raise ValueError(f"{name!r} is not a saved monitor page")
        folder = _run_folder(self._root(project_id, root_id).path, run_id)
        path = path_inside(folder, name)
        if not path.is_file():
            raise FileNotFoundError(f"{name} is not in the run {run_id}")
        ticket = secrets.token_urlsafe(24)
        now = time.monotonic()
        with self._ticket_lock:
            self._tickets = {k: v for k, v in self._tickets.items() if v[1] > now}
            if len(self._tickets) >= MAX_TICKETS:
                oldest = min(self._tickets, key=lambda k: self._tickets[k][1])
                del self._tickets[oldest]
            self._tickets[ticket] = (path, now + TICKET_TTL_S)
        return {"url": f"/data-page/{ticket}"}

    def page(self, ticket: str) -> Path:
        """The file a ticket was issued for, while it is valid."""
        with self._ticket_lock:
            entry = self._tickets.get(ticket)
        if entry is None or entry[1] <= time.monotonic():
            raise FileNotFoundError(
                "This link has expired; open the monitor page again from the Data view"
            )
        return entry[0]

    # -- the routes' one entry point ---------------------------------------------

    def get(self, route: str, query: dict[str, list[str]]) -> dict[str, Any]:
        """Answer ``GET /api/data/<route>``; `dashboard.Handler` checked the token."""

        def arg(name: str) -> str:
            return query.get(name, [""])[0]

        if route == "roots":
            return self.roots(arg("project"))
        if route == "runs":
            return self.runs(arg("project"), arg("root"))
        if route == "run":
            return self.run(arg("project"), arg("root"), arg("run"))
        if route == "text":
            return self.text(arg("project"), arg("root"), arg("run"), arg("name"))
        if route == "table":
            # Several runs as one comma-separated parameter: run ids never
            # hold a comma (naming writes none), and a repeated parameter is
            # refused by the handler.
            runs = [r for r in arg("runs").split(",") if r]
            return self.table(arg("project"), arg("root"), runs, arg("kind"))
        if route == "page":
            return self.page_ticket(arg("project"), arg("root"), arg("run"), arg("name"))
        raise FileNotFoundError(f"No data route {route!r}")


def _read_csv(
    path: Path, room: int, run_id: str, problems: list[str]
) -> tuple[list[str], list[list[str]], int]:
    """A CSV's header, up to ``room`` rows of text cells, and its row count.

    Read with the csv module, so quoted cells with commas and line breaks
    (the trials file stores JSON in some) come out whole. A row with more or
    fewer cells than the header is a damaged file; it is padded or cut to
    the header and named in ``problems`` (the first few), so the reader sees
    it. A file the csv module cannot read at all raises, naming the run and
    the line.
    """
    rows: list[list[str]] = []
    count = 0
    ragged = 0
    line = 0
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.reader(stream)
            header = next(reader, None)
            if header is None:
                raise ValueError(f"{run_id}: {path.name} is empty (no header line)")
            width = len(header)
            for cells in reader:
                line = reader.line_num
                if not cells:
                    continue  # a blank line, e.g. a trailing one
                count += 1
                if len(cells) != width:
                    ragged += 1
                    if ragged <= 3:
                        problems.append(
                            f"{run_id}: {path.name} line {reader.line_num} has {len(cells)} "
                            f"cells, the header {width}"
                        )
                    cells = (cells + [""] * width)[:width]
                if len(rows) < room:
                    rows.append(cells)
    except csv.Error as exc:
        raise ValueError(
            f"{run_id}: {path.name} cannot be parsed after line {line}: {exc}"
        ) from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"{run_id}: {path.name} is not UTF-8 text: {exc}") from exc
    if ragged > 3:
        problems.append(f"{run_id}: {path.name} has {ragged} damaged rows in all")
    return header, rows, count
