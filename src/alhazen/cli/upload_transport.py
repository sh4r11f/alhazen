"""Copy saved sessions to an archive: the transports and their settings.

The workspace's Upload action (`workspace_upload.py`) copies session folders
from a data folder on the rig to an archive on a remote host (or to a folder
on this computer) into ``<base>/<experiment>/<the run's own path>``. This
module holds the two decisions that action must not spread around:

- HOW bytes get there (`Transport`): rsync over SSH through a connection the
  operator opened once (`SshRsync`), or a plain copy into a directory on this
  computer (`LocalCopy`, also what the tests verify against). Both obey one
  contract: a file already at the destination is never deleted or
  rewritten; a copy that stops halfway is resumed or redone, never left under
  its final name; and what is there afterwards is checked against the local
  file by content, not by name.
- WHERE the settings live (`UploadSettings`, ``upload.json`` in the
  workspace's state directory, never in a repository): the login user,
  remote host and remote base path belong to the computer and the operator,
  so the code ships no default for any of them; they are typed in on the
  dashboard's upload settings.

Passwords and second factors never pass through here. `SshRsync` runs ssh
with ``BatchMode=yes`` and ``ControlMaster=no``: it only rides a master
connection the operator opened in a terminal (`SshRsync.open_command` says
the exact line), so the dashboard can never sit waiting at a prompt nobody
sees. Without a control path it relies on keys or Kerberos instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, field_validator

from alhazen.data.atomic import replace_atomically

SETTINGS_FILE = "upload.json"
DEFAULT_CONTROL_PATH = "~/.ssh/cm-%r@%h:%p"
# How long a dashboard command waits for ssh before calling the connection
# unusable (a master that went away makes ssh fail at once with BatchMode).
SSH_TIMEOUT_S = 30
# Where an interrupted rsync keeps the part it had, inside each destination
# folder, so the next upload resumes it instead of starting over; and where
# LocalCopy writes before the rename.
PARTIAL_DIR = ".alhazen-partial"
# rsync 3.1 added --info=progress2, which the progress readout uses.
RSYNC_MINIMUM = (3, 1)

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}")
# A remote path the transport will pass through a shell: letters, digits and
# the punctuation of ordinary paths only, and absolute.
_REMOTE_PATH = re.compile(r"/[A-Za-z0-9._/@+-]*")


class UploadError(ValueError):
    """An upload that cannot go ahead, with a reason a person can act on (the
    dashboard answers it 400, like any refused request)."""


class UploadSettings(BaseModel):
    """This computer's archive settings (``upload.json``). Empty means not
    set: an SSH upload needs the user, host and base path typed in first."""

    model_config = ConfigDict(extra="forbid")

    transport: Literal["ssh", "local"] = "ssh"
    # What the page calls the archive ("Upload to <label>").
    label: str = "archive"
    # The account the SSH connection logs in as.
    user: str = ""
    host: str = ""
    # The folder on the host each experiment's folder is made in.
    base_path: str = ""
    # The master connection's socket; empty: no master (keys or Kerberos).
    control_path: str = DEFAULT_CONTROL_PATH
    # The ssh program, for a computer where it is not on PATH as "ssh".
    ssh_command: str = "ssh"
    # The directory a "local" upload copies into.
    local_path: str = ""

    @field_validator("label")
    @classmethod
    def _label(cls, value: str) -> str:
        value = value.strip() or "archive"
        if not _LABEL.fullmatch(value):
            raise ValueError("The archive's name may hold letters, digits, spaces, '.', '_', '-'")
        return value

    @field_validator("user")
    @classmethod
    def _user(cls, value: str) -> str:
        value = value.strip()
        if value and not _NAME.fullmatch(value):
            raise ValueError("The login user may hold only letters, digits, '.', '_' and '-'")
        return value

    @field_validator("host")
    @classmethod
    def _host(cls, value: str) -> str:
        value = value.strip()
        if value and not _HOST.fullmatch(value):
            raise ValueError("The host must be a host name, such as archive.example.org")
        return value

    @field_validator("base_path")
    @classmethod
    def _base(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        value = value.rstrip("/") or "/"
        if not _REMOTE_PATH.fullmatch(value) or ".." in PurePosixPath(value).parts:
            raise ValueError(
                "The remote base path must be absolute, without '..' or spaces, such as "
                "/path/to/remote/data"
            )
        return value

    @field_validator("control_path", "ssh_command", "local_path")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


def load_settings(directory: Path) -> UploadSettings:
    """The workspace's settings, or the defaults when none were saved yet."""
    path = directory / SETTINGS_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return UploadSettings()
    try:
        return UploadSettings.model_validate(json.loads(text))
    except ValueError as exc:
        raise UploadError(f"{path} cannot be read: {exc}") from exc


def save_settings(directory: Path, fields: object) -> UploadSettings:
    """Validate and save the settings; nothing is written when invalid."""
    settings = UploadSettings.model_validate(fields)
    replace_atomically(directory / SETTINGS_FILE, settings.model_dump_json(indent=2))
    return settings


def experiment_folder(name: str) -> str:
    """The archive folder an experiment's sessions go in: its name exactly,
    refused rather than rewritten when it could not be one folder."""
    if not _NAME.fullmatch(name) or name in {".", ".."}:
        raise UploadError(f"{name!r} cannot name a folder in the archive")
    return name


# -- what an upload moves -----------------------------------------------------


@dataclass(frozen=True)
class Item:
    """One session folder: where it is here, and its path under the
    experiment's archive folder (the run id, posix)."""

    source: Path
    relative: str


@dataclass(frozen=True)
class FilePlan:
    """One file of a planned upload: ``new`` (not at the destination),
    ``present`` (there and the same by size and time — content is checked
    after the copy) or ``conflict`` (there and different: never replaced)."""

    item: str
    path: str
    size: int
    state: Literal["new", "present", "conflict"]


@dataclass
class Progress:
    """How far a copy has come, as the transport can tell."""

    bytes_done: int = 0
    bytes_total: int = 0
    file: str = ""


def local_files(item: Item) -> list[tuple[str, int]]:
    """Every regular file of a session folder, relative and posix, with its
    size. Symlinks are not followed and not copied: a session writes none,
    and one would let an upload reach outside the folder."""
    found = []
    for path in sorted(item.source.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        found.append((path.relative_to(item.source).as_posix(), path.stat().st_size))
    return found


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


class Cancelled(UploadError):
    """The operator stopped the upload; what was copied stays and resumes."""


class Transport(Protocol):
    """How session folders reach the archive. Every method may raise
    UploadError; none ever deletes or overwrites a file at the destination."""

    def destination(self, folder: str) -> str:
        """Where ``folder`` (an experiment's archive folder) is, for people."""

    def check(self) -> dict[str, object]:
        """Can an upload start now? ``{ok, message, command?}``; never raises."""

    def plan(self, folder: str, items: list[Item]) -> list[FilePlan]:
        """What an upload would do, file by file, without copying anything."""

    def copy(
        self,
        folder: str,
        items: list[Item],
        progress: Callable[[Progress], None],
        cancelled: threading.Event,
    ) -> None:
        """Copy every file not yet at the destination; leave the rest."""

    def verify(self, folder: str, items: list[Item]) -> list[tuple[str, str]]:
        """Compare by content: ``(item, path)`` of every file that is
        missing or different at the destination (empty: all verified)."""


# -- rsync over SSH ---------------------------------------------------------------


def _version(text: str) -> tuple[int, ...] | None:
    match = re.search(r"version\s+(\d+)\.(\d+)", text)
    return (int(match.group(1)), int(match.group(2))) if match else None


@dataclass
class SshRsync:
    """rsync over an SSH master connection the operator opened (log in once).

    Copying runs ``rsync -rt --relative --ignore-existing --partial-dir``:
    recursive, with times, the run's path kept under the experiment folder,
    a file already there never touched, an interrupted file resumed from its
    partial copy. Never ``--delete``; permissions and owners are not sent,
    so files take the group folder's own. Verifying runs the same transfer as
    a dry run with ``--checksum``: any file it would still send differs in
    content, or is missing.
    """

    settings: UploadSettings
    rsync: str = "rsync"
    run: Callable[..., subprocess.CompletedProcess[str]] = field(default=subprocess.run)
    popen: Callable[..., subprocess.Popen[str]] = field(default=subprocess.Popen)

    @property
    def login(self) -> str:
        missing = [
            what
            for what, value in (("login user", self.settings.user), ("host", self.settings.host))
            if not value
        ]
        if missing:
            raise UploadError(f"Set the {' and '.join(missing)} in the upload settings first")
        return f"{self.settings.user}@{self.settings.host}"

    def _ssh_options(self) -> list[str]:
        options = ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_TIMEOUT_S}"]
        if self.settings.control_path:
            options += ["-o", "ControlMaster=no", "-o", f"ControlPath={self.settings.control_path}"]
        return options

    def open_command(self) -> str:
        """The line the operator runs in a terminal to open the connection:
        it asks for the password and any second factor there, once, and then stays up in
        the background for 12 hours of uploads."""
        parts = [self.settings.ssh_command or "ssh", "-fN"]
        if self.settings.control_path:
            parts += [
                "-o",
                "ControlMaster=yes",
                "-o",
                "ControlPersist=12h",
                "-o",
                f"ControlPath={self.settings.control_path}",
            ]
        parts.append(self.login)
        return shlex.join(parts)

    def _remote(self, folder: str) -> str:
        if not self.settings.base_path:
            raise UploadError("Set the remote base path in the upload settings first")
        return f"{self.settings.base_path}/{experiment_folder(folder)}"

    def destination(self, folder: str) -> str:
        return f"{self.login}:{self._remote(folder)}"

    def _ready(self) -> None:
        # The login user, host and remote base path are all set (each raises).
        self.login  # noqa: B018
        self._remote("x")
        if shutil.which(self.settings.ssh_command or "ssh") is None:
            raise UploadError(
                f"{self.settings.ssh_command or 'ssh'} was not found on this computer"
            )
        if shutil.which(self.rsync) is None:
            raise UploadError(
                "rsync was not found on this computer. Install rsync 3.1 or newer "
                "(macOS: brew install rsync; Linux: your package manager; Windows: run the "
                "dashboard inside WSL, or use another computer that can reach the data)"
            )
        answer = self.run([self.rsync, "--version"], capture_output=True, text=True, timeout=10)
        version = _version(answer.stdout)
        if version is None or version < RSYNC_MINIMUM:
            raise UploadError(
                f"rsync {'.'.join(map(str, version or ())) or '?'} is too old: "
                "the upload needs rsync 3.1 or newer"
            )

    def _ssh(
        self, *remote: str, timeout: float = SSH_TIMEOUT_S
    ) -> subprocess.CompletedProcess[str]:
        command = [
            self.settings.ssh_command or "ssh",
            *self._ssh_options(),
            self.login,
            shlex.join(remote),
        ]
        try:
            return self.run(command, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise UploadError(f"{self.settings.host} did not answer within {timeout:g} s") from exc

    def check(self) -> dict[str, object]:
        try:
            self._ready()
        except UploadError as exc:
            return {"ok": False, "message": str(exc)}
        if self.settings.control_path:
            master = self.run(
                [
                    self.settings.ssh_command or "ssh",
                    "-O",
                    "check",
                    "-o",
                    f"ControlPath={self.settings.control_path}",
                    self.login,
                ],
                capture_output=True,
                text=True,
                timeout=SSH_TIMEOUT_S,
            )
            if master.returncode != 0:
                return {
                    "ok": False,
                    "message": (
                        f"No open connection to {self.settings.host}. Run this in a terminal "
                        "on this computer, log in there (password and any second factor), "
                        "then check again:"
                    ),
                    "command": self.open_command(),
                }
        try:
            answer = self._ssh("test", "-d", self.settings.base_path)
        except UploadError as exc:
            return {"ok": False, "message": str(exc)}
        if answer.returncode == 255:
            return {
                "ok": False,
                "message": f"ssh to {self.login} failed: {answer.stderr.strip()}",
                "command": self.open_command(),
            }
        if answer.returncode != 0:
            return {
                "ok": False,
                "message": f"{self.settings.base_path} is not a folder on {self.settings.host}",
            }
        return {"ok": True, "message": f"Connected to {self.settings.host} as {self.settings.user}"}

    def _sources(self, items: list[Item]) -> list[str]:
        # --relative keeps the path after "/./": the run id, so the folder
        # lands at <experiment>/<run id> with its parents made as needed.
        sources = []
        for item in items:
            root = item.source
            for _ in PurePosixPath(item.relative).parts:
                root = root.parent
            sources.append(f"{root.as_posix()}/./{item.relative}")
        return sources

    def _rsync(self, folder: str, items: list[Item], *flags: str) -> list[str]:
        rsh = shlex.join([self.settings.ssh_command or "ssh", *self._ssh_options()])
        return [
            self.rsync,
            "-rt",
            "--relative",
            "--partial-dir",
            PARTIAL_DIR,
            "--rsh",
            rsh,
            *flags,
            *self._sources(items),
            f"{self.destination(folder)}/",
        ]

    def _make_folder(self, folder: str) -> None:
        answer = self._ssh("mkdir", "-p", "--", self._remote(folder))
        if answer.returncode != 0:
            raise UploadError(
                f"Could not make {self._remote(folder)} on {self.settings.host}: "
                f"{answer.stderr.strip() or answer.returncode}"
            )

    def _itemized(self, folder: str, items: list[Item], *flags: str) -> list[tuple[str, str, int]]:
        """A dry run's itemized changes: ``(flags, path, size)`` per file."""
        self._ready()
        answer = self.run(
            self._rsync(folder, items, "--dry-run", "-ii", "--out-format", "%i|%l|%n", *flags),
            capture_output=True,
            text=True,
            timeout=600,
        )
        # 23: some files could not be read at all, which is an error here too.
        if answer.returncode != 0:
            raise UploadError(_rsync_failure(answer.returncode, answer.stderr))
        lines = []
        for line in answer.stdout.splitlines():
            parts = line.split("|", 2)
            if len(parts) != 3 or len(parts[0]) < 2 or parts[0][1] != "f":
                continue  # directories, and anything that is not a file line
            lines.append((parts[0], parts[2], int(parts[1]) if parts[1].isdigit() else 0))
        return lines

    def _split(self, items: list[Item], path: str) -> tuple[str, str]:
        for item in items:
            if path.startswith(item.relative + "/"):
                return item.relative, path[len(item.relative) + 1 :]
        raise UploadError(f"rsync named a file outside the chosen sessions: {path}")

    def plan(self, folder: str, items: list[Item]) -> list[FilePlan]:
        planned = []
        for flags, path, size in self._itemized(folder, items):
            item, name = self._split(items, path)
            # "<" is a file sent to a remote host, ">" one received (a
            # local copy); "+++" after the type: it is not there at all.
            if flags[0] in "<>" and flags[2:].startswith("+"):
                state = "new"
            elif flags[0] in "<>ch":
                state = "conflict"
            else:
                state = "present"
            planned.append(FilePlan(item, name, size, state))  # type: ignore[arg-type]
        return planned

    def copy(
        self,
        folder: str,
        items: list[Item],
        progress: Callable[[Progress], None],
        cancelled: threading.Event,
    ) -> None:
        self._ready()
        self._make_folder(folder)
        total = sum(size for item in items for _, size in local_files(item))
        process = self.popen(
            self._rsync(
                folder, items, "--ignore-existing", "--info=progress2", "--no-inc-recursive"
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stop = threading.Thread(target=_stop_when, args=(cancelled, process), daemon=True)
        stop.start()
        assert process.stdout is not None
        buffer = ""
        while chunk := process.stdout.read(256):
            buffer += chunk
            *done, buffer = re.split(r"[\r\n]", buffer)
            for line in done:
                match = re.match(r"\s*([\d,]+)\s+\d+%", line)
                if match:
                    progress(Progress(int(match.group(1).replace(",", "")), total))
        stderr = process.stderr.read() if process.stderr else ""
        code = process.wait()
        if cancelled.is_set():
            raise Cancelled("Upload stopped; the next upload resumes where this one stopped")
        if code != 0:
            raise UploadError(_rsync_failure(code, stderr))
        progress(Progress(total, total))

    def verify(self, folder: str, items: list[Item]) -> list[tuple[str, str]]:
        return [
            self._split(items, path)
            for flags, path, _ in self._itemized(folder, items, "--checksum")
            if flags[0] in "<>ch"
        ]


def _stop_when(cancelled: threading.Event, process: subprocess.Popen[str]) -> None:
    while process.poll() is None:
        if cancelled.wait(0.2):
            process.terminate()
            return


_RSYNC_CODES = {
    5: "the remote side refused (check the login user and that the connection is open)",
    10: "the connection failed",
    11: "a file could not be read or written",
    12: "the connection broke while copying",
    23: "some files could not be transferred",
    24: "some source files vanished while copying",
    30: "the connection timed out",
    255: "ssh failed: the connection is not open, or was refused",
}


def _rsync_failure(code: int, stderr: str) -> str:
    reason = _RSYNC_CODES.get(code, f"rsync exited with code {code}")
    tail = stderr.strip().splitlines()[-3:]
    return f"Upload failed: {reason}" + (f" ({' / '.join(tail)})" if tail else "")


# -- a directory on this computer -------------------------------------------------


@dataclass
class LocalCopy:
    """Copy into a directory on this computer: a mounted share, a backup
    disk — and the transport the tests and the sandbox verify against. Same
    rules as `SshRsync`, by hand: a file is written beside its final name
    and renamed when whole; a file already there is left alone; verification
    compares SHA-256 of both sides."""

    root: Path

    @classmethod
    def from_settings(cls, settings: UploadSettings) -> LocalCopy:
        if not settings.local_path:
            raise UploadError("Set the local folder to copy into in the upload settings")
        return cls(Path(settings.local_path).expanduser())

    def _folder(self, folder: str) -> Path:
        return self.root / experiment_folder(folder)

    def destination(self, folder: str) -> str:
        return str(self._folder(folder))

    def check(self) -> dict[str, object]:
        if not self.root.is_dir():
            return {"ok": False, "message": f"{self.root} is not a folder on this computer"}
        if not os.access(self.root, os.W_OK):
            return {"ok": False, "message": f"{self.root} cannot be written"}
        return {"ok": True, "message": f"Copying into {self.root}"}

    def _pairs(self, folder: str, items: list[Item]) -> Iterable[tuple[Item, str, int, Path]]:
        if not self.root.is_dir():
            raise UploadError(f"{self.root} is not a folder on this computer")
        base = self._folder(folder)
        for item in items:
            for name, size in local_files(item):
                yield item, name, size, base / item.relative / name

    def plan(self, folder: str, items: list[Item]) -> list[FilePlan]:
        planned = []
        for item, name, size, target in self._pairs(folder, items):
            if not target.exists():
                state = "new"
            elif target.stat().st_size == size and int(target.stat().st_mtime) == int(
                (item.source / name).stat().st_mtime
            ):
                state = "present"
            else:
                state = "conflict"
            planned.append(FilePlan(item.relative, name, size, state))  # type: ignore[arg-type]
        return planned

    def copy(
        self,
        folder: str,
        items: list[Item],
        progress: Callable[[Progress], None],
        cancelled: threading.Event,
    ) -> None:
        pairs = list(self._pairs(folder, items))
        total = sum(size for _, _, size, _ in pairs)
        done = 0
        for item, name, size, target in pairs:
            if cancelled.is_set():
                raise Cancelled("Upload stopped; the next upload resumes where this one stopped")
            progress(Progress(done, total, f"{item.relative}/{name}"))
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                partial = target.parent / PARTIAL_DIR / target.name
                partial.parent.mkdir(exist_ok=True)
                shutil.copy2(item.source / name, partial)
                os.replace(partial, target)
                with_partial = partial.parent
                if not any(with_partial.iterdir()):
                    with_partial.rmdir()
            done += size
        progress(Progress(total, total))

    def verify(self, folder: str, items: list[Item]) -> list[tuple[str, str]]:
        bad = []
        for item, name, _, target in self._pairs(folder, items):
            if not target.is_file() or sha256_file(target) != sha256_file(item.source / name):
                bad.append((item.relative, name))
        return bad


def transport_for(settings: UploadSettings) -> SshRsync | LocalCopy:
    """The transport the settings choose."""
    if settings.transport == "local":
        return LocalCopy.from_settings(settings)
    return SshRsync(settings)
