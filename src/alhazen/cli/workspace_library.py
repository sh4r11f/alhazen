"""The dashboard's library: every copy in its owner's library, downloaded on demand.

The page lists the copies a person keeps on an Experiment Platform host or in Mana
(``alhazen library connect`` stores where; experiment_platform.library holds the protocol
and the disk rules). A copy that is not on this machine shows as not downloaded; running it
needs a download first: fetch its current revision, write it to ``<copies>/<copy id>``,
build its environment from its lock file with uv, and register it like any experiment, so
its Run, Data and History pages are the usual ones.

Before anything is downloaded the disk is checked (experiment_platform.library.write_copy):
a folder already at the revision is used as it is, and a folder holding another revision
and session data is never overwritten unasked. Copies already on disk with data and a
built environment are registered when the library is refreshed, so their data shows.

The decision this module hides is how a download runs beside the page: one background job
per copy, its stage and outcome kept in memory for the page to poll, its uv output in
``<state>/library/logs/<id>.log``. The list itself is read in the background too: the first
time the page asks after a connection appears or changes (``library.json`` written while the
dashboard runs, as Mana's kit does when it joins an open dashboard), so the page never has to
know that a refresh is due, and a failure is kept for the page to show.

When the dashboard closes it tells the platform (:meth:`Library.report_closed`): which
machine, which alhazen, and the sessions run since it started. That is best effort with a
short deadline; a platform that cannot be reached never holds the shutdown up.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from alhazen.experiment_platform import library
from alhazen.experiment_platform.library import (
    CopyConflict,
    LibraryClient,
    LibraryError,
    LibrarySource,
)

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on the 3.10 CI job
    import tomli as tomllib

# A copy's environment build is the slow part (PsychoPy and its wheels); past this the
# job is reported failed and uv is stopped.
SYNC_TIMEOUT_S = 1800
# A failed background read of the list is tried again when the page next asks, at most
# this often (the Refresh button asks at once).
RETRY_S = 60
# How long a closing dashboard waits for the platform to take its report.
REPORT_TIMEOUT_S = 5.0
# The line a session ends its console with (session/runner.py, _log_session_end).
# The first parenthesis holds the counts (a FAILED line's exception text follows it).
SESSION_END = re.compile(r"session end: (\w+)[^\n(]*\(([^()\n]*)\)")
OUTCOME_COUNT = re.compile(r"([A-Za-z0-9_.-]+) (\d+)")
STAGES = ("checking", "fetching", "writing", "building", "registering")


def find_uv() -> str | None:
    """The uv executable: the one running this dashboard under ``uv run`` (it sets UV),
    else on PATH, else where its installer puts it."""
    candidates = [os.environ.get("UV"), shutil.which("uv")]
    home = Path.home()
    candidates += [
        str(home / ".local" / "bin" / "uv"),
        str(home / ".local" / "bin" / "uv.exe"),
        str(home / ".cargo" / "bin" / "uv"),
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return c
    return None


def declared_extras(root: Path, wanted: tuple[str, ...]) -> list[str]:
    """The extras in ``wanted`` that the experiment declares (an experiment without a
    psychopy extra syncs without it rather than failing)."""
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    declared = (project.get("project") or {}).get("optional-dependencies") or {}
    return [x for x in wanted if x in declared]


def default_sync(root: Path, extras: list[str], log: Path) -> None:
    """``uv sync --locked`` in the copy's folder, into its own ``.venv``."""
    uv = find_uv()
    if uv is None:
        raise LibraryError(
            "uv is needed to build the experiment's environment and was not found. Install it "
            "(https://docs.astral.sh/uv/) and download again."
        )
    command = [uv, "sync", "--locked", *[a for x in extras for a in ("--extra", x)]]
    env = {
        k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT")
    }
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as out:
        out.write(f"\n$ {' '.join(command[1:])}  (in {root})\n".encode())
        out.flush()
        try:
            done = subprocess.run(
                command,
                cwd=root,
                env=env,
                stdout=out,
                stderr=subprocess.STDOUT,
                timeout=SYNC_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise LibraryError(
                f"Building the environment took longer than {SYNC_TIMEOUT_S // 60} min"
            ) from None
    if done.returncode != 0:
        tail = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-6:]
        raise LibraryError("Building the environment failed: " + " / ".join(tail))


Sync = Callable[[Path, list[str], Path], None]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class Library:
    """The library of one dashboard workspace. ``client`` and ``sync`` are replaceable
    (tests pass fakes); by default they follow ``library.json`` and run uv."""

    def __init__(
        self,
        workspace: Any,
        *,
        client_factory: Callable[[LibrarySource], LibraryClient] = LibraryClient,
        sync: Sync = default_sync,
    ) -> None:
        self.workspace = workspace
        self.directory = Path(workspace.directory)
        self._client_factory = client_factory
        self._sync = sync
        self._lock = threading.RLock()
        self._forks: dict[str, dict[str, Any]] = {}
        self._fetched: float | None = None
        self._error: str | None = None
        self._jobs: dict[str, dict[str, Any]] = {}
        self._threads: list[threading.Thread] = []
        # Which connection the list belongs to, whether a background read is under way and
        # when the last one was tried; the run jobs every connection seen so far named.
        self._seen: str | None = None
        self._refreshing = 0
        self._tried: float | None = None
        self._run_jobs: dict[str, None] = {}

    # ------------------------------------------------------------------ reading
    def source(self) -> LibrarySource | None:
        return library.load_source(self.directory)

    def get(self) -> dict[str, Any]:
        """What the page shows: the connection, every copy with its disk state, jobs."""
        try:
            source = self.source()
        except LibraryError as error:
            return {"configured": True, "error": str(error), "items": []}
        if source is None:
            return {"configured": False, "items": []}
        if self._due(source):
            self._refresh_in_background()
        with self._lock:
            forks = dict(self._forks)
            jobs = {k: dict(v) for k, v in self._jobs.items()}
            fetched, problem = self._fetched, self._error
            refreshing = self._refreshing > 0
        registered = {str(Path(p["path"]).resolve()): p["id"] for p in self.workspace.projects}
        items = [
            self._item(source, fid, fork, registered, jobs.get(fid)) for fid, fork in forks.items()
        ]
        items.sort(key=lambda x: (-(x["updatedAt"] or 0), x["title"].lower()))
        return {
            "configured": True,
            "source": {
                "label": source.label,
                "origin": source.origin,
                "copies": str(source.copies_dir),
                "expires": source.expires,
            },
            "fetched": fetched,
            "error": problem,
            "refreshing": refreshing,
            "items": items,
        }

    def _due(self, source: LibrarySource) -> bool:
        """Note the connection the page sees; True when its list should be read now: a
        connection not read yet (new, or changed since: the old list is dropped), or a
        failed read older than RETRY_S."""
        with self._lock:
            for job in source.run_jobs:
                self._run_jobs.setdefault(job, None)
            if source.fingerprint != self._seen:
                if self._seen is not None:
                    self._forks, self._fetched, self._error = {}, None, None
                self._seen, self._tried = source.fingerprint, None
            if self._refreshing:
                return False
            if self._tried is None:
                return True
            return self._fetched is None and time.time() - self._tried > RETRY_S

    def _refresh_in_background(self) -> None:
        with self._lock:
            if self._refreshing:
                return
            # Counted before the thread starts, so the answer that started it says so.
            self._refreshing += 1
            self._tried = time.time()

        def work() -> None:
            # Any failure is kept in _error by refresh, for the page to show.
            with contextlib.suppress(Exception):
                self.refresh(counted=True)

        thread = threading.Thread(target=work, daemon=True, name="library-refresh")
        self._threads.append(thread)
        thread.start()

    def _item(
        self,
        source: LibrarySource,
        fork_id: str,
        fork: dict[str, Any],
        registered: dict[str, str],
        job: dict[str, Any] | None,
    ) -> dict[str, Any]:
        head = fork.get("head") if isinstance(fork.get("head"), dict) else None
        root = source.copy_dir(fork_id)
        disk = library.inspect_copy(root, head.get("id") if head else None)
        origin = fork.get("origin") or {}
        return {
            "id": fork_id,
            "title": str(fork.get("title") or fork_id),
            "from": None if origin.get("scratch") else origin.get("title"),
            "by": origin.get("publisherName") or None,
            "revision": head.get("id") if head else None,
            "downloadable": bool(head and head.get("path")),
            "updatedAt": fork.get("updatedAt"),
            "project": registered.get(str(root.resolve())),
            "disk": disk.as_json(),
            "job": job,
        }

    # ------------------------------------------------------------------ refreshing
    def refresh(self, *, counted: bool = False) -> dict[str, Any]:
        """Ask the platform for the copies again, then register copies already on disk
        that hold data and have an environment (so their data shows). Any failure is kept
        for the page (``error``) as well as raised."""
        if not counted:
            with self._lock:
                self._refreshing += 1
                self._tried = time.time()
        try:
            self._refresh()
        finally:
            with self._lock:
                self._refreshing -= 1
        return self.get()

    def _refresh(self) -> None:
        try:
            source = self.source()
            if source is None:
                raise LibraryError("No library is connected (alhazen library connect)")
            with self._lock:
                for job in source.run_jobs:
                    self._run_jobs.setdefault(job, None)
            forks = self._client_factory(source).forks()
        except LibraryError as error:
            with self._lock:
                self._error = str(error)
            raise
        except Exception as error:  # noqa: BLE001 - the page must hear of every failure
            message = f"The library could not be read: {error}"
            with self._lock:
                self._error = message
            raise LibraryError(message) from error
        self._accept(source, forks)

    def _accept(self, source: LibrarySource, forks: dict[str, dict[str, Any]]) -> None:
        """Keep ``forks`` as the list of ``source``, and register copies already on disk
        that hold data and have an environment."""
        with self._lock:
            self._forks, self._fetched, self._error = forks, time.time(), None
            self._seen = source.fingerprint
        registered = {str(Path(p["path"]).resolve()) for p in self.workspace.projects}
        for fork_id, fork in forks.items():
            root = source.copy_dir(fork_id)
            if str(root.resolve()) in registered:
                continue
            head = fork.get("head") if isinstance(fork.get("head"), dict) else {}
            disk = library.inspect_copy(root, (head or {}).get("id"))
            if disk.state != "absent" and disk.environment and disk.data.sessions:
                try:
                    self.workspace.add(str(root))
                except (ValueError, OSError) as error:
                    with self._lock:
                        self._jobs[fork_id] = {
                            "status": "failed",
                            "stage": "registering",
                            "message": f"Could not register the copy on disk: {error}",
                            "at": time.time(),
                        }

    # ------------------------------------------------------------------ connecting
    def connect(self, url: str, token: str, label: str | None = None) -> dict[str, Any]:
        """Connect to a library from the page: the platform's address and a machine token
        (or a local platform's ``#token=`` link). The connection is checked by listing the
        copies before it is saved, so a wrong token never replaces a working connection."""
        if not isinstance(url, str) or not url.strip():
            raise LibraryError("Give the platform's address")
        current = None
        try:
            current = self.source()
        except LibraryError:
            current = None  # an unreadable file is replaced by the new connection
        fields: dict[str, Any] = {"label": (label or "").strip()[:60] or "Library"}
        if current is not None:
            fields.update(copies=current.copies, extras=current.extras, machine=current.machine)
        source = LibrarySource.from_url(url, (token or "").strip() or None, **fields)
        forks = self._client_factory(source).forks()
        library.save_source(self.directory, source)
        self._accept(source, forks)
        with self._lock:
            self._tried = time.time()
        return self.get()

    def disconnect(self) -> dict[str, Any]:
        library.forget_source(self.directory)
        with self._lock:
            self._forks, self._fetched, self._error = {}, None, None
            self._seen, self._tried = None, None
        return self.get()

    # ------------------------------------------------------------------ closing
    def report_closed(
        self,
        *,
        started: str,
        sessions: list[dict[str, Any]],
        reason: str,
        timeout: float = REPORT_TIMEOUT_S,
    ) -> str:
        """Tell the connected platform this dashboard closed: the machine, its alhazen, when
        it ran, the sessions run meanwhile and the run jobs it served. Never raises and never
        waits much longer than ``timeout``; what happened is returned (empty when no library
        is connected), printed by the caller and appended to ``<state>/library/reports.log``."""
        from alhazen import __version__

        try:
            source = self.source()
        except LibraryError as error:
            return self._log_report(None, f"Not reported: {error}")
        if source is None:
            return ""  # nothing is connected, so there is no one to tell
        with self._lock:
            jobs = list(dict.fromkeys([*self._run_jobs, *source.run_jobs]))
        event = {
            "event": "dashboard-closed",
            "machine": source.machine or platform.node() or "this machine",
            "alhazen": __version__,
            "startedAt": started,
            "closedAt": _now(),
            "reason": reason,
            "jobs": jobs,
            "sessions": sessions,
        }
        outcome: list[str] = []

        def send() -> None:
            try:
                client = self._client_factory(source)
                if hasattr(client, "timeout"):
                    client.timeout = timeout
                client.report(event)
                n = len(sessions)
                outcome.append(
                    f"Told {source.label} the dashboard closed "
                    f"({n} session{'' if n == 1 else 's'} since it started)."
                )
            except Exception as error:  # noqa: BLE001 - a report must never stop a shutdown
                outcome.append(f"Could not tell {source.label} the dashboard closed: {error}")

        thread = threading.Thread(target=send, daemon=True, name="library-report")
        thread.start()
        thread.join(timeout + 1)
        message = (
            outcome[0]
            if outcome
            else f"{source.label} did not answer within {timeout:g} s; the report was not sent."
        )
        return self._log_report(event, message)

    def _log_report(self, event: dict[str, Any] | None, message: str) -> str:
        try:
            folder = self.directory / "library"
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / "reports.log").open("a", encoding="utf-8") as out:
                out.write(json.dumps({"at": _now(), "message": message, "event": event}) + "\n")
        except OSError:
            pass  # the message is still printed; the log is a convenience
        return message

    # ------------------------------------------------------------------ downloading
    def download(self, fork_id: str, replace: bool = False) -> dict[str, Any]:
        """Start (in the background) getting copy ``fork_id`` ready to run. Returns its job."""
        source = self.source()
        if source is None:
            raise LibraryError("No library is connected (alhazen library connect)")
        with self._lock:
            fork = self._forks.get(fork_id)
            if fork is None:
                raise LibraryError("That copy is not in the library (refresh the list)")
            running = self._jobs.get(fork_id)
            if running and running.get("status") == "running":
                raise LibraryError("That copy is already being downloaded")
            job = {
                "status": "running",
                "stage": "checking",
                "message": "",
                "at": time.time(),
                "replace": bool(replace),
            }
            self._jobs[fork_id] = job
        thread = threading.Thread(
            target=self._run,
            args=(source, fork_id, dict(fork), replace),
            daemon=True,
            name=f"library-{fork_id}",
        )
        self._threads.append(thread)
        thread.start()
        return dict(job)

    def _stage(self, fork_id: str, **fields: Any) -> None:
        with self._lock:
            self._jobs[fork_id] = {**self._jobs.get(fork_id, {}), **fields, "at": time.time()}

    def _run(
        self, source: LibrarySource, fork_id: str, fork: dict[str, Any], replace: bool
    ) -> None:
        try:
            result = self._prepare(source, fork_id, fork, replace)
        except CopyConflict as conflict:
            self._stage(
                fork_id, status="conflict", message=str(conflict), disk=conflict.state.as_json()
            )
        except (LibraryError, ValueError, OSError) as error:
            self._stage(fork_id, status="failed", message=str(error))
        except Exception as error:  # noqa: BLE001 - a background job must report every failure
            self._stage(fork_id, status="failed", message=f"Unexpected error: {error!r}")
            raise
        else:
            self._stage(fork_id, status="done", stage="done", message=result)

    def _prepare(
        self, source: LibrarySource, fork_id: str, fork: dict[str, Any], replace: bool
    ) -> str:
        head = fork.get("head") if isinstance(fork.get("head"), dict) else None
        if not head or not head.get("path"):
            raise LibraryError("This copy has no revision yet")
        root = source.copy_dir(fork_id)
        disk = library.inspect_copy(root, head["id"])
        action = "unchanged"
        if disk.state != "current":
            self._stage(fork_id, stage="fetching")
            archive = self._client_factory(source).archive(head["path"])
            self._stage(fork_id, stage="writing")
            root.parent.mkdir(parents=True, exist_ok=True)
            action = library.write_copy(
                root,
                archive,
                copy_id=fork_id,
                bundle_id=head["id"],
                title=str(fork.get("title") or fork_id),
                source=source.label,
                replace=replace,
            ).action
        if action in ("written", "updated") or library.environment_python(root) is None:
            self._stage(fork_id, stage="building")
            self._sync(
                root,
                declared_extras(root, source.extras),
                self.directory / "library" / "logs" / f"{fork_id}.log",
            )
        self._stage(fork_id, stage="registering")
        project = self.workspace.add(str(root))
        words = {
            "unchanged": "Already on this machine at this revision",
            "adopted": "The folder already held this revision",
            "written": "Downloaded",
            "updated": "Updated the experiment's files; the data beside them is unchanged",
        }[action]
        return f"{words}. Registered as {project.get('title') or project.get('name')}."

    def close(self, timeout: float = 1.0) -> None:
        for thread in self._threads:
            thread.join(timeout)


# ---------------------------------------------------------------------- the closing report
def session_summaries(workspace: Any, since: str) -> list[dict[str, Any]]:
    """The sessions launched from this dashboard since ``since`` (an ISO time), as a closing
    report lists them: experiment, the library copy it is (when it is one), task, mode,
    start, end, how it ended, and the outcome counts its console's last line gives."""
    try:
        start = _parse_time(since)
    except ValueError:
        return []
    with workspace.lock:
        runs = [dict(r) for r in workspace.runs.values()]
    out = []
    for run in sorted(runs, key=lambda r: str(r.get("started") or "")):
        try:
            if _parse_time(str(run.get("started"))) < start:
                continue
        except ValueError:
            continue
        cwd = Path(str(run.get("cwd") or ""))
        marker = library.read_marker(cwd) if run.get("cwd") else None
        status, outcomes = _session_end(
            Path(workspace.directory) / "runs" / str(run.get("id")) / "console.log"
        )
        out.append(
            {
                "experiment": run.get("name"),
                "copy": (marker or {}).get("id"),
                "revision": (marker or {}).get("bundle"),
                "task": run.get("task"),
                "mode": run.get("mode"),
                "startedAt": run.get("started"),
                "endedAt": run.get("finished"),
                "status": run.get("status"),
                "session": status,
                "outcomes": outcomes,
            }
        )
    return out


def _parse_time(text: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def _session_end(console: Path) -> tuple[str | None, dict[str, int]]:
    """How a session's console says it ended (complete, cancelled, FAILED) and its rows'
    outcome counts; (None, {}) for a console without that line."""
    try:
        with console.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 64 * 1024))
            tail = stream.read().decode("utf-8", errors="replace")
    except OSError:
        return None, {}
    found = list(SESSION_END.finditer(tail))
    if not found:
        return None, {}
    status, counts = found[-1].groups()
    return status, {name: int(n) for name, n in OUTCOME_COUNT.findall(counts)}


# ---------------------------------------------------------------------- the command line
def default_state_dir() -> Path:
    return Path.home() / ".alhazen" / "dashboard"


def add_parser(sub: Any) -> None:
    parser = sub.add_parser("library", help="connect a dashboard to your library on a platform")
    actions = parser.add_subparsers(dest="library_action", required=True)
    connect = actions.add_parser("connect", help="store where the dashboard reads your library")
    connect.add_argument(
        "url", help="the platform's address; a local platform's URL carries #token="
    )
    connect.add_argument(
        "--token",
        default=None,
        help="the machine token the platform's Machines page showed (a shell may keep it in its "
        "history; --token-file or $ALHAZEN_LIBRARY_TOKEN do not)",
    )
    connect.add_argument(
        "--token-file", default=None, help="file holding the token (else $ALHAZEN_LIBRARY_TOKEN)"
    )
    connect.add_argument(
        "--machine", default=None, help="this machine's name in reports (default: its host name)"
    )
    connect.add_argument("--label", default=None, help="the library's name on the page")
    connect.add_argument(
        "--copies", default=None, help="where downloads go (default: ~/alhazen-experiments)"
    )
    connect.add_argument(
        "--extras",
        default="psychopy",
        help="optional dependency groups to install when an experiment declares them, "
        "comma-separated (default: psychopy; empty for none)",
    )
    connect.add_argument("--state-dir", default=None, help="the dashboard's state folder")
    connect.add_argument(
        "--check", action="store_true", help="list the copies once to check the connection"
    )
    show = actions.add_parser("show", help="say which library the dashboard reads")
    show.add_argument("--state-dir", default=None)
    forget = actions.add_parser("disconnect", help="forget the library connection")
    forget.add_argument("--state-dir", default=None)


def run(args: argparse.Namespace) -> int:
    state = Path(args.state_dir).expanduser() if args.state_dir else default_state_dir()
    try:
        if args.library_action == "connect":
            token = args.token
            if args.token_file:
                token = Path(args.token_file).expanduser().read_text(encoding="utf-8").strip()
            token = token or os.environ.get("ALHAZEN_LIBRARY_TOKEN")
            fields: dict[str, Any] = {}
            if args.label:
                fields["label"] = args.label
            if args.machine:
                fields["machine"] = args.machine
            fields["extras"] = tuple(x.strip() for x in args.extras.split(",") if x.strip())
            if args.copies:
                fields["copies"] = Path(args.copies).expanduser().absolute()
            source = LibrarySource.from_url(args.url, token, **fields)
            if args.check:
                count = len(LibraryClient(source).forks())
                print(
                    f"{source.label} at {source.origin}: {count} cop{'y' if count == 1 else 'ies'}"
                )
            path = library.save_source(state, source)
            print(f"Connected: {source.label} at {source.origin} ({path})")
            print(f"Downloads go to {source.copies_dir}")
        elif args.library_action == "show":
            current = library.load_source(state)
            print(
                "No library connected"
                if current is None
                else f"{current.label} at {current.origin}; downloads go to {current.copies_dir}"
            )
        else:
            print("Disconnected" if library.forget_source(state) else "No library was connected")
    except (LibraryError, OSError) as error:
        print(f"LIBRARY: {error}", file=sys.stderr)
        return 1
    return 0
