"""Copy files to an archive: the transports, the settings, the naming rules.

The workspace's Upload action (`workspace_upload.py`) copies an experiment's
data folder — its session folders and everything else saved there — to an
archive into ``<base>/<experiment>/<the path inside the data folder>``. It
decides WHAT goes WHERE; this module hides HOW bytes get there and the rules
every way must keep, behind four operations (`Transport`):

- ``listing``: every file already under an experiment's archive folder, with
  its size;
- ``checksums``: the SHA-256 of files there, computed there when the host
  can, else by reading them back;
- ``put``: copy files to names that do not exist yet, resuming a copy that
  stopped halfway, checking its content before it takes its final name, and
  never replacing a file that is there;
- ``destination`` and ``check``: where it goes and whether it can go now.

Three transports keep that contract: SFTP in pure Python (`upload_sftp`,
the default on every platform), rsync over an SSH master connection where
rsync exists (`RsyncSsh`, faster for large first uploads), and a folder on
this computer (`LocalCopy`, also what the tests verify against). None ever
deletes or rewrites a file at the destination; the only things a transport
removes there are its own partial copies, in ``.alhazen-partial`` folders.

The settings (`UploadSettings`, ``upload.json`` in the workspace's state
directory, never in a repository) belong to the computer and the operator:
the code ships no host, user or path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from alhazen.data.atomic import replace_atomically

SETTINGS_FILE = "upload.json"
DEFAULT_CONTROL_PATH = "~/.ssh/cm-%r@%h:%p"
# How long one SSH request may take before the connection counts as gone.
SSH_TIMEOUT_S = 30
# Where a copy is written until it is whole and checked, beside its final
# name; an interrupted copy is resumed from here. Never part of a listing.
PARTIAL_DIR = ".alhazen-partial"
# rsync 3.1 added --info=progress2, which the progress readout uses.
RSYNC_MINIMUM = (3, 1)
BLOCK = 256 * 1024

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}")
# A remote path that may pass through a shell: ordinary path characters only.
_REMOTE_PATH = re.compile(r"/[A-Za-z0-9._/@+-]*")
_STAMP = re.compile(r"\d{8}T\d{6}Z")
_HEX = re.compile(r"[0-9a-f]{64}")


class UploadError(ValueError):
    """An upload that cannot go ahead, with a reason a person can act on (the
    dashboard answers it 400, like any refused request)."""


class Cancelled(UploadError):
    """The operator stopped the upload; what was copied stays and resumes."""


class UploadSettings(BaseModel):
    """This computer's archive settings (``upload.json``). Empty means not
    set: the code ships no destination."""

    model_config = ConfigDict(extra="forbid")

    # sftp: pure Python, every platform (the default). rsync: rsync over an
    # SSH master connection, where both exist. local: a folder here.
    transport: Literal["sftp", "rsync", "local"] = "sftp"
    # What the page calls the archive ("Upload to <label>").
    label: str = "archive"
    user: str = ""
    host: str = ""
    port: int = Field(default=22, ge=1, le=65535)
    # The folder on the host each experiment's folder is made in.
    base_path: str = ""
    # rsync only: the master connection's socket (empty: keys or Kerberos),
    # and the ssh program.
    control_path: str = DEFAULT_CONTROL_PATH
    ssh_command: str = "ssh"
    # local only: the directory to copy into.
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

    def remote_folder(self, folder: str) -> str:
        """``<base path>/<folder>`` on the host; refused while unset."""
        missing = [
            what
            for what, value in (
                ("login user", self.user),
                ("remote host", self.host),
                ("remote base path", self.base_path),
            )
            if not value
        ]
        if missing:
            raise UploadError(f"Set the {', '.join(missing)} in the upload settings first")
        return f"{self.base_path}/{experiment_folder(folder)}"

    def login(self) -> str:
        where = f"{self.user}@{self.host}"
        return where if self.port == 22 else f"{where}:{self.port}"


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
    """The archive folder an experiment's files go in: its name exactly,
    refused rather than rewritten when it could not be one folder."""
    if not _NAME.fullmatch(name) or name in {".", ".."}:
        raise UploadError(f"{name!r} cannot name a folder in the archive")
    return name


def check_relative(path: str) -> str:
    """A path inside an archive folder: posix, relative, no '..', no part
    that is a partial-copy folder. Refused otherwise."""
    parts = PurePosixPath(path).parts
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(p in ("", ".", "..", PARTIAL_DIR) for p in parts)
    ):
        raise UploadError(f"{path!r} is not a path inside the archive folder")
    return path


# -- versions -------------------------------------------------------------------


def versioned(path: str, stamp: str) -> str:
    """The name a changed file is kept under beside the older copy:
    ``participants.tsv`` -> ``participants.20261008T174512Z.tsv``."""
    if not _STAMP.fullmatch(stamp):
        raise ValueError(f"Not a UTC stamp: {stamp!r}")
    pure = PurePosixPath(path)
    return str(pure.with_name(f"{pure.stem}.{stamp}{pure.suffix}"))


def is_version_of(candidate: str, path: str) -> bool:
    """Is ``candidate`` a versioned copy of ``path`` (same folder)?"""
    pure, other = PurePosixPath(path), PurePosixPath(candidate)
    if pure.parent != other.parent:
        return False
    pattern = re.escape(pure.stem) + r"\.\d{8}T\d{6}Z" + re.escape(pure.suffix)
    return re.fullmatch(pattern, other.name) is not None


# -- what a put moves -----------------------------------------------------------------


@dataclass(frozen=True)
class Put:
    """One file to copy: from ``local`` to ``remote`` (relative to the
    experiment's archive folder), with the size and SHA-256 it must have."""

    local: Path
    remote: str
    size: int
    sha256: str


@dataclass
class Progress:
    """How far a copy has come."""

    bytes_done: int = 0
    bytes_total: int = 0
    file: str = ""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


class Transport(Protocol):
    """How files reach the archive. Every method may raise UploadError; none
    ever deletes or overwrites a file at the destination."""

    def destination(self, folder: str) -> str:
        """Where ``folder`` (an experiment's archive folder) is, for people."""

    def check(self) -> dict[str, object]:
        """Can an upload start now? ``{ok, message, ...}``; never raises."""

    def listing(self, folder: str) -> dict[str, int]:
        """Every file under ``folder``, relative posix path -> size; empty
        when the folder does not exist. Partial copies are not listed."""

    def checksums(self, folder: str, paths: list[str]) -> dict[str, str]:
        """SHA-256 of each of ``paths`` that exists under ``folder``."""

    def put(
        self,
        folder: str,
        puts: list[Put],
        progress: Callable[[Progress], None],
        cancelled: threading.Event,
    ) -> dict[str, str]:
        """Copy each put to a name that does not exist yet; a name that
        exists is left alone. Returns the SHA-256 checked at the
        destination for each file it wrote (verification may skip those)."""


def parse_sha256(output: str, names: list[str]) -> dict[str, str]:
    """``sha256sum``/``shasum -a 256`` output for ``names`` (in that order):
    one line per file, the hash first; an escaped name starts with '\\'.
    Lines are matched to names by order, so odd characters in a name never
    confuse the parse; any mismatch yields nothing for the rest."""
    found: dict[str, str] = {}
    lines = [line for line in output.splitlines() if line.strip()]
    if len(lines) != len(names):
        return found
    for name, line in zip(names, lines, strict=True):
        digest = line.lstrip("\\")[:64]
        if not _HEX.fullmatch(digest):
            return {}
        found[name] = digest
    return found


# -- a directory on this computer ------------------------------------------------


@dataclass
class LocalCopy:
    """Copy into a directory on this computer: a mounted share, a backup
    disk — and the transport the tests verify the upload's rules against."""

    root: Path

    @classmethod
    def from_settings(cls, settings: UploadSettings) -> LocalCopy:
        if not settings.local_path:
            raise UploadError("Set the local folder to copy into in the upload settings")
        return cls(Path(settings.local_path).expanduser())

    def _folder(self, folder: str) -> Path:
        if not self.root.is_dir():
            raise UploadError(f"{self.root} is not a folder on this computer")
        return self.root / experiment_folder(folder)

    def destination(self, folder: str) -> str:
        return str(self.root / experiment_folder(folder))

    def check(self) -> dict[str, object]:
        if not self.root.is_dir():
            return {"ok": False, "message": f"{self.root} is not a folder on this computer"}
        if not os.access(self.root, os.W_OK):
            return {"ok": False, "message": f"{self.root} cannot be written"}
        return {"ok": True, "message": f"Copying into {self.root}"}

    def listing(self, folder: str) -> dict[str, int]:
        base = self._folder(folder)
        found: dict[str, int] = {}
        if not base.is_dir():
            return found
        for directory, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d != PARTIAL_DIR]
            for name in files:
                path = Path(directory) / name
                if not path.is_symlink():
                    found[path.relative_to(base).as_posix()] = path.stat().st_size
        return found

    def checksums(self, folder: str, paths: list[str]) -> dict[str, str]:
        base = self._folder(folder)
        return {p: sha256_file(base / p) for p in paths if (base / check_relative(p)).is_file()}

    def put(
        self,
        folder: str,
        puts: list[Put],
        progress: Callable[[Progress], None],
        cancelled: threading.Event,
    ) -> dict[str, str]:
        base = self._folder(folder)
        total = sum(p.size for p in puts)
        done = 0
        written: dict[str, str] = {}
        for item in puts:
            if cancelled.is_set():
                raise Cancelled("Upload stopped; the next upload resumes where this one stopped")
            progress(Progress(done, total, item.remote))
            target = base / check_relative(item.remote)
            if not target.exists():
                partial = target.parent / PARTIAL_DIR / target.name
                partial.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item.local, partial)
                if sha256_file(partial) != item.sha256:
                    partial.unlink()
                    raise UploadError(f"{item.remote} changed while it was being copied")
                if not target.exists():  # never replace a file that appeared meanwhile
                    os.rename(partial, target)
                    written[item.remote] = item.sha256
                if partial.exists():
                    partial.unlink()
                with_partial = partial.parent
                if with_partial.is_dir() and not any(with_partial.iterdir()):
                    with_partial.rmdir()
            done += item.size
        progress(Progress(total, total))
        return written


# -- rsync over an SSH master connection (optional) -------------------------------------


def _version(text: str) -> tuple[int, ...] | None:
    match = re.search(r"version\s+(\d+)\.(\d+)", text)
    return (int(match.group(1)), int(match.group(2))) if match else None


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


@dataclass
class RsyncSsh:
    """rsync over an SSH master connection the operator opened in a terminal
    (log in once there; the dashboard then rides it with ``BatchMode=yes``).
    An optional fast path for computers that have rsync 3.1+ and OpenSSH
    master connections; the SFTP transport needs neither.

    Copies are ``rsync -rt --ignore-existing --partial-dir``: nothing there
    is ever rewritten or deleted, an interrupted file resumes from its
    partial copy, permissions and owners are not sent. Checksums are
    computed on the host (``sha256sum``, else ``shasum -a 256``)."""

    settings: UploadSettings
    rsync: str = "rsync"
    run: Callable[..., subprocess.CompletedProcess[str]] = field(default=subprocess.run)
    popen: Callable[..., subprocess.Popen[str]] = field(default=subprocess.Popen)

    def _login(self) -> str:
        self.settings.remote_folder("x")  # user, host, base path all set
        return f"{self.settings.user}@{self.settings.host}"

    def _ssh_options(self) -> list[str]:
        options = ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_TIMEOUT_S}"]
        if self.settings.port != 22:
            options += ["-p", str(self.settings.port)]
        if self.settings.control_path:
            options += ["-o", "ControlMaster=no", "-o", f"ControlPath={self.settings.control_path}"]
        return options

    def open_command(self) -> str:
        """The line the operator runs in a terminal to open the connection."""
        parts = [self.settings.ssh_command or "ssh", "-fN"]
        if self.settings.port != 22:
            parts += ["-p", str(self.settings.port)]
        if self.settings.control_path:
            parts += [
                "-o",
                "ControlMaster=yes",
                "-o",
                "ControlPersist=12h",
                "-o",
                f"ControlPath={self.settings.control_path}",
            ]
        parts.append(self._login())
        return shlex.join(parts)

    def destination(self, folder: str) -> str:
        return f"{self._login()}:{self.settings.remote_folder(folder)}"

    def _ready(self) -> None:
        self._login()
        if shutil.which(self.settings.ssh_command or "ssh") is None:
            ssh = self.settings.ssh_command or "ssh"
            raise UploadError(f"{ssh} was not found on this computer")
        if shutil.which(self.rsync) is None:
            raise UploadError(
                "rsync was not found on this computer: choose the SFTP transport in the upload "
                "settings (it needs nothing installed), or install rsync 3.1 or newer"
            )
        answer = self.run([self.rsync, "--version"], capture_output=True, text=True, timeout=10)
        version = _version(answer.stdout)
        if version is None or version < RSYNC_MINIMUM:
            raise UploadError(
                f"rsync {'.'.join(map(str, version or ())) or '?'} is too old: the rsync "
                "transport needs rsync 3.1 or newer (or choose SFTP)"
            )

    def _ssh(
        self, *remote: str, timeout: float = SSH_TIMEOUT_S
    ) -> subprocess.CompletedProcess[str]:
        command = [
            self.settings.ssh_command or "ssh",
            *self._ssh_options(),
            self._login(),
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
                    self._login(),
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
                "message": f"ssh to {self._login()} failed: {answer.stderr.strip()}",
                "command": self.open_command(),
            }
        if answer.returncode != 0:
            return {
                "ok": False,
                "message": f"{self.settings.base_path} is not a folder on {self.settings.host}",
            }
        return {"ok": True, "message": f"Connected to {self.settings.host} as {self.settings.user}"}

    def _rsh(self) -> str:
        return shlex.join([self.settings.ssh_command or "ssh", *self._ssh_options()])

    def listing(self, folder: str) -> dict[str, int]:
        self._ready()
        answer = self.run(
            [self.rsync, "-r", "--list-only", "--rsh", self._rsh(), f"{self.destination(folder)}/"],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if answer.returncode == 23 and "No such file" in answer.stderr:
            return {}  # the experiment's folder is not there yet
        if answer.returncode != 0:
            raise UploadError(_rsync_failure(answer.returncode, answer.stderr))
        found: dict[str, int] = {}
        for line in answer.stdout.splitlines():
            match = re.match(r"(-)\S+\s+([\d,]+)\s+\S+\s+\S+\s+(.+)$", line)
            if not match:
                continue  # directories, links, the summary
            name = match.group(3)
            if PARTIAL_DIR in PurePosixPath(name).parts:
                continue
            found[name] = int(match.group(2).replace(",", ""))
        return found

    def checksums(self, folder: str, paths: list[str]) -> dict[str, str]:
        remote = self.settings.remote_folder(folder)
        found: dict[str, str] = {}
        for start in range(0, len(paths), 100):
            chunk = [check_relative(p) for p in paths[start : start + 100]]
            for tool in (["sha256sum", "--"], ["shasum", "-a", "256", "--"]):
                answer = self._ssh(
                    "sh",
                    "-c",
                    f"cd {shlex.quote(remote)} && " + shlex.join([*tool, *chunk]),
                    timeout=3600,
                )
                parsed = parse_sha256(answer.stdout, chunk) if answer.returncode == 0 else {}
                if parsed:
                    found.update(parsed)
                    break
            else:
                raise UploadError(
                    f"{self.settings.host} cannot compute SHA-256 (no sha256sum or shasum): "
                    "use the SFTP transport, which can verify by reading files back"
                )
        return found

    def _make_parents(self, folder: str, puts: list[Put]) -> None:
        remote = self.settings.remote_folder(folder)
        parents = sorted({str(PurePosixPath(remote, p.remote).parent) for p in puts})
        answer = self._ssh("mkdir", "-p", "--", *parents)
        if answer.returncode != 0:
            raise UploadError(
                f"Could not make folders on {self.settings.host}: "
                f"{answer.stderr.strip() or answer.returncode}"
            )

    def put(
        self,
        folder: str,
        puts: list[Put],
        progress: Callable[[Progress], None],
        cancelled: threading.Event,
    ) -> dict[str, str]:
        self._ready()
        if not puts:
            return {}
        self._make_parents(folder, puts)
        total = sum(p.size for p in puts)
        # One rsync per local root whose files keep their relative names
        # (--files-from); a file uploaded under another name (a version, a
        # database snapshot) is staged under that name in a temporary folder.
        groups: dict[Path, list[str]] = {}
        with tempfile.TemporaryDirectory(prefix="alhazen-upload-") as stage:
            for item in puts:
                rel = check_relative(item.remote)
                root = item.local
                for _ in PurePosixPath(rel).parts:
                    root = root.parent
                if (root / rel) == item.local:
                    groups.setdefault(root, []).append(rel)
                else:
                    staged = Path(stage) / rel
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item.local, staged)
                    groups.setdefault(Path(stage), []).append(rel)
            done = 0
            for root, names in groups.items():
                listed = Path(stage) / ".files-from"
                listed.write_text("\n".join(names) + "\n", encoding="utf-8")
                part = sum(p.size for p in puts if p.remote in names)
                self._copy(folder, root, listed, done, total, progress, cancelled)
                done += part
        progress(Progress(total, total))
        return {}

    def _copy(
        self,
        folder: str,
        root: Path,
        listed: Path,
        before: int,
        total: int,
        progress: Callable[[Progress], None],
        cancelled: threading.Event,
    ) -> None:
        process = self.popen(
            [
                self.rsync,
                "-rt",
                "--ignore-existing",
                "--partial-dir",
                PARTIAL_DIR,
                "--info=progress2",
                "--files-from",
                str(listed),
                "--rsh",
                self._rsh(),
                f"{root.as_posix()}/",
                f"{self.destination(folder)}/",
            ],
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
            *lines, buffer = re.split(r"[\r\n]", buffer)
            for line in lines:
                match = re.match(r"\s*([\d,]+)\s+\d+%", line)
                if match:
                    progress(Progress(before + int(match.group(1).replace(",", "")), total))
        stderr = process.stderr.read() if process.stderr else ""
        code = process.wait()
        if cancelled.is_set():
            raise Cancelled("Upload stopped; the next upload resumes where this one stopped")
        if code != 0:
            raise UploadError(_rsync_failure(code, stderr))


def _stop_when(cancelled: threading.Event, process: subprocess.Popen[str]) -> None:
    while process.poll() is None:
        if cancelled.wait(0.2):
            process.terminate()
            return


def local_tree(root: Path, skip: Iterable[str] = ()) -> list[tuple[str, Path, int]]:
    """Every regular file under ``root``: ``(relative posix, path, size)``,
    sorted. Symlinks are not followed or copied (one could reach outside the
    folder); folders named in ``skip`` at the top are left out."""
    skipped = set(skip)
    found = []
    for directory, dirs, files in os.walk(root):
        here = Path(directory)
        if here == root:
            dirs[:] = [d for d in dirs if d not in skipped]
        dirs[:] = [d for d in dirs if d != PARTIAL_DIR and not (here / d).is_symlink()]
        for name in files:
            path = here / name
            if path.is_symlink() or not path.is_file():
                continue
            found.append((path.relative_to(root).as_posix(), path, path.stat().st_size))
    return sorted(found)
