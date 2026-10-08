"""The SFTP transport: one SSH connection, opened from the dashboard.

Pure Python (paramiko), so it works the same on Windows, macOS and Linux
with nothing else installed. The operator types the host, the login user
and the remote base path in the upload settings, presses Connect, and logs
in on the page: the host's key on first contact (shown by fingerprint and
trusted only when they say so), then whatever the host asks — a password, a
second factor, a choice of push or code — relayed prompt by prompt
(keyboard-interactive). Keys from an SSH agent or ``~/.ssh`` are tried first
and silently. The connection is then held, with keepalives, for as long as
the dashboard runs; every upload reuses it, so the login is asked once.

Copies go over SFTP on that connection (`SftpTransport.put`): written to a
partial file beside the final name, resumed from its size after an
interruption, checked by SHA-256 on the host, and only then renamed to the
final name — with SFTP's plain rename, which refuses to replace an existing
file. Checksums are computed on the host (``sha256sum``, else ``shasum``)
over the same connection; a host that runs no commands (an SFTP-only
account) is verified by reading the files back.

Host keys: ``~/.ssh/known_hosts`` is read, never written; a key the
operator trusts here is kept in the workspace's own ``upload_known_hosts``.
A key that differs from the one on record refuses the connection.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import shlex
import socket
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

import paramiko

from alhazen.cli.upload_transport import (
    BLOCK,
    PARTIAL_DIR,
    SSH_TIMEOUT_S,
    Cancelled,
    Progress,
    Put,
    UploadError,
    UploadSettings,
    check_relative,
    parse_sha256,
)

KNOWN_HOSTS_FILE = "upload_known_hosts"
KEEPALIVE_S = 30
# How long the login waits for the operator at a prompt or a host key.
OPERATOR_TIMEOUT_S = 600
# How long one page request waits for the login to need the operator again
# (or to finish) before answering "still working" (the page then polls).
REQUEST_WAIT_S = 20
DEFAULT_KEY_FILES = ("id_ed25519", "id_ecdsa", "id_rsa")


def fingerprint(key: paramiko.PKey) -> str:
    """A host key's SHA256 fingerprint, as OpenSSH prints it."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


class SftpSession:
    """The dashboard's one SSH connection and its login conversation.

    States: ``disconnected``, ``connecting`` (the host is being reached or
    is checking an answer), ``hostkey`` (an unknown host key waits for
    Trust), ``prompt`` (the host asked something), ``connected``,
    ``failed``. `snapshot` is what the page shows; `connect`, `trust` and
    `answer` move it on and return the next snapshot once the login needs
    the operator again or has finished (or after REQUEST_WAIT_S).
    """

    def __init__(
        self,
        workspace_dir: Path,
        *,
        user_known_hosts: Path | None = None,
        key_files: list[Path] | None = None,
        use_agent: bool = True,
        dial: Callable[..., socket.socket] = socket.create_connection,
    ):
        self.known_hosts_file = workspace_dir / KNOWN_HOSTS_FILE
        ssh_dir = Path.home() / ".ssh"
        self.user_known_hosts = (
            user_known_hosts if user_known_hosts is not None else ssh_dir / "known_hosts"
        )
        self.key_files = (
            key_files if key_files is not None else [ssh_dir / n for n in DEFAULT_KEY_FILES]
        )
        self.use_agent = use_agent
        self.dial = dial
        self._cond = threading.Condition()
        self._version = 0
        self._state: dict[str, Any] = {"state": "disconnected"}
        self._transport: paramiko.Transport | None = None
        self._target: tuple[str, str, int] | None = None
        self._answers: list[str] | None = None
        self._trusted: str | None = None
        self._abandon = False

    # -- what the page sees -------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._cond:
            self._notice_lost()
            return dict(self._state)

    def _set(self, **state: Any) -> None:
        """Replace the state (caller holds the lock) and wake every waiter."""
        target = self._target
        self._state = {
            **state,
            **({"user": target[0], "host": target[1], "port": target[2]} if target else {}),
        }
        self._version += 1
        self._cond.notify_all()

    def _notice_lost(self) -> None:
        if self._state.get("state") == "connected" and not (
            self._transport and self._transport.is_active()
        ):
            self._set(state="disconnected", message="The connection was lost; connect again")

    def _wait(self, version: int) -> dict[str, Any]:
        deadline = time.monotonic() + REQUEST_WAIT_S
        while self._version == version and self._state.get("state") == "connecting":
            left = deadline - time.monotonic()
            if left <= 0:
                break
            self._cond.wait(left)
        return dict(self._state)

    # -- what the page does ---------------------------------------------------

    def connect(self, settings: UploadSettings) -> dict[str, Any]:
        settings.remote_folder("x")  # user, host and base path are set
        target = (settings.user, settings.host, settings.port)
        with self._cond:
            self._notice_lost()
            state = self._state.get("state")
            if self._target == target and state in ("connected", "connecting", "hostkey", "prompt"):
                return dict(self._state)
            self._close_locked()
            self._target = target
            self._abandon = False
            self._answers = None
            self._trusted = None
            self._set(state="connecting", message=f"Reaching {settings.host}…")
            version = self._version
            threading.Thread(target=self._login, args=(target,), daemon=True).start()
            return self._wait(version)

    def trust(self, offered: str) -> dict[str, Any]:
        with self._cond:
            if self._state.get("state") != "hostkey":
                raise UploadError("No host key is waiting to be trusted")
            if offered != self._state.get("fingerprint"):
                raise UploadError("That is not the fingerprint the host presented")
            self._trusted = offered
            self._set(state="connecting", message="Logging in…")
            return self._wait(self._version)

    def answer(self, answers: Any) -> dict[str, Any]:
        with self._cond:
            if self._state.get("state") != "prompt":
                raise UploadError("The host is not waiting for an answer")
            prompts = self._state.get("prompts") or []
            if (
                not isinstance(answers, list)
                or len(answers) != len(prompts)
                or not all(isinstance(a, str) for a in answers)
            ):
                raise UploadError(f"Answer each of the {len(prompts)} questions")
            self._answers = answers
            self._set(state="connecting", message="Checking…")
            return self._wait(self._version)

    def disconnect(self) -> dict[str, Any]:
        with self._cond:
            self._close_locked()
            self._set(state="disconnected", message="Disconnected")
            return dict(self._state)

    def close(self) -> None:
        with self._cond:
            self._close_locked()

    def _close_locked(self) -> None:
        self._abandon = True
        self._cond.notify_all()
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def transport(self) -> paramiko.Transport:
        """The live connection, or UploadError saying what to do."""
        with self._cond:
            self._notice_lost()
            if self._state.get("state") != "connected" or self._transport is None:
                raise UploadError(
                    "Not connected to the remote host: press Connect and log in first"
                )
            return self._transport

    # -- the login (its own thread) ----------------------------------------------

    def _operator(self, waiting: Callable[[], bool]) -> None:
        """Wait (lock held) until ``waiting()`` is false; UploadError when the
        operator took too long or the login was abandoned."""
        deadline = time.monotonic() + OPERATOR_TIMEOUT_S
        while waiting():
            if self._abandon:
                raise UploadError("The login was cancelled")
            left = deadline - time.monotonic()
            if left <= 0:
                raise UploadError("The login waited too long for an answer; connect again")
            self._cond.wait(left)

    def _login(self, target: tuple[str, str, int]) -> None:
        user, host, port = target
        transport = None
        try:
            sock = self.dial((host, port), timeout=SSH_TIMEOUT_S)
            transport = paramiko.Transport(sock)
            transport.start_client(timeout=SSH_TIMEOUT_S)
            self._check_host_key(transport, host, port)
            self._authenticate(transport, user)
            transport.set_keepalive(KEEPALIVE_S)
            with self._cond:
                if self._abandon or self._target != target:
                    transport.close()
                    return
                self._transport = transport
                self._set(
                    state="connected",
                    message=f"Connected to {host} as {user}; held until the dashboard stops",
                )
        except (UploadError, paramiko.SSHException, OSError, EOFError) as exc:
            if transport is not None:
                transport.close()
            with self._cond:
                if self._target == target and not self._abandon:
                    reason = str(exc) or type(exc).__name__
                    if isinstance(exc, paramiko.AuthenticationException):
                        reason = f"The host refused the login ({reason})"
                    self._set(state="failed", message=reason)

    def _host_keys(self) -> tuple[paramiko.HostKeys, paramiko.HostKeys]:
        theirs, ours = paramiko.HostKeys(), paramiko.HostKeys()
        for keys, path in ((theirs, self.user_known_hosts), (ours, self.known_hosts_file)):
            if path.is_file():
                try:
                    keys.load(str(path))
                except (OSError, paramiko.SSHException) as exc:
                    raise UploadError(f"{path} cannot be read: {exc}") from exc
        return theirs, ours

    def _check_host_key(self, transport: paramiko.Transport, host: str, port: int) -> None:
        key = transport.get_remote_server_key()
        name = host if port == 22 else f"[{host}]:{port}"
        print_ = fingerprint(key)
        theirs, ours = self._host_keys()
        for keys in (theirs, ours):
            known = keys.lookup(name)
            if known is None or key.get_name() not in known:
                continue
            if known[key.get_name()] == key:
                return
            raise UploadError(
                f"The host key of {host} has CHANGED ({print_}) and does not match the one on "
                "record. This can mean someone is intercepting the connection; ask whoever runs "
                "the host before trusting it. Nothing was sent."
            )
        with self._cond:
            self._set(
                state="hostkey",
                fingerprint=print_,
                key_type=key.get_name(),
                message=f"First connection to {host}: check the fingerprint before trusting it",
            )
            self._operator(lambda: self._trusted is None)
            self._trusted = None
        ours.add(name, key.get_name(), key)
        self.known_hosts_file.parent.mkdir(parents=True, exist_ok=True)
        ours.save(str(self.known_hosts_file))

    def _interactive(
        self, title: str, instructions: str, prompts: list[tuple[str, bool]]
    ) -> list[str]:
        """paramiko's keyboard-interactive handler: relay the host's questions
        to the page and wait for the operator's answers."""
        if not prompts:
            return []
        with self._cond:
            self._answers = None
            self._set(
                state="prompt",
                name=title,
                instructions=instructions,
                prompts=[{"text": text, "echo": bool(echo)} for text, echo in prompts],
                message="The host asks:",
            )
            self._operator(lambda: self._answers is None)
            answers, self._answers = self._answers, None
        assert answers is not None
        return answers

    def _keys(self, notes: list[str]) -> list[paramiko.PKey]:
        """Keys to try first: the agent's, then the usual files. One that
        cannot be used (an encrypted file: there is no one to ask for its
        passphrase) is named in ``notes``, said if the whole login fails."""
        keys: list[paramiko.PKey] = []
        if self.use_agent:
            try:
                keys.extend(paramiko.Agent().get_keys())
            except (paramiko.SSHException, OSError) as exc:
                notes.append(f"SSH agent: {exc}")
        for path in self.key_files:
            if not path.is_file():
                continue
            try:
                keys.append(paramiko.PKey.from_path(path))
            except (paramiko.SSHException, OSError, ValueError) as exc:
                notes.append(f"{path.name} not used: {exc}")
        return keys

    def _authenticate(self, transport: paramiko.Transport, user: str) -> None:
        try:
            transport.auth_none(user)
            return
        except paramiko.BadAuthenticationType as exc:
            methods = list(exc.allowed_types)
        notes: list[str] = []
        if "publickey" in methods:
            for key in self._keys(notes):
                try:
                    methods = transport.auth_publickey(user, key) or methods
                except paramiko.AuthenticationException as exc:
                    notes.append(f"{key.get_name()} key refused: {exc}")
                    continue
                if transport.is_authenticated():
                    return
                break  # a partial success: the next method follows
        if "keyboard-interactive" in methods and not transport.is_authenticated():
            methods = transport.auth_interactive(user, self._interactive) or methods
            if transport.is_authenticated():
                return
        if "password" in methods and not transport.is_authenticated():
            (password,) = self._interactive("", "", [("Password:", False)])
            transport.auth_password(user, password)
        if not transport.is_authenticated():
            raise paramiko.AuthenticationException(
                f"no login method the host offers ({', '.join(methods)}) succeeded"
                + (f"; {'; '.join(notes)}" if notes else "")
            )


class SftpTransport:
    """The upload's operations over the session's connection (see module)."""

    def __init__(self, session: SftpSession, settings: UploadSettings):
        self.session = session
        self.settings = settings
        self._exec_ok: bool | None = None

    def _remote(self, folder: str) -> str:
        return self.settings.remote_folder(folder)

    def destination(self, folder: str) -> str:
        return f"{self.settings.login()}:{self._remote(folder)}"

    def check(self) -> dict[str, object]:
        try:
            self.settings.remote_folder("x")
        except UploadError as exc:
            return {"ok": False, "message": str(exc)}
        state = self.session.snapshot()
        if state.get("state") != "connected":
            return {
                "ok": False,
                "login": True,
                "message": state.get("message") or f"Not connected to {self.settings.host}",
            }
        try:
            with self._sftp() as sftp:
                if not stat.S_ISDIR(sftp.stat(self.settings.base_path).st_mode or 0):
                    raise UploadError(f"{self.settings.base_path} is not a folder")
        except (OSError, UploadError) as exc:
            return {
                "ok": False,
                "message": f"{self.settings.base_path} on {self.settings.host}: {exc}",
            }
        return {"ok": True, "message": state.get("message", "Connected")}

    def _sftp(self) -> paramiko.SFTPClient:
        client = self.session.transport().open_sftp_client()
        if client is None:
            raise UploadError("The host did not open an SFTP channel")
        return client

    # -- listing -----------------------------------------------------------

    def listing(self, folder: str) -> dict[str, int]:
        remote = self._remote(folder)
        found: dict[str, int] = {}
        with self._sftp() as sftp:
            try:
                sftp.stat(remote)
            except FileNotFoundError:
                return found
            pending = [""]
            while pending:
                rel = pending.pop()
                for entry in sftp.listdir_attr(f"{remote}/{rel}" if rel else remote):
                    path = f"{rel}/{entry.filename}" if rel else entry.filename
                    mode = entry.st_mode or 0
                    if stat.S_ISDIR(mode):
                        if entry.filename != PARTIAL_DIR:
                            pending.append(path)
                    elif stat.S_ISREG(mode):
                        found[path] = entry.st_size or 0
        return found

    # -- checksums ------------------------------------------------------------

    def _exec(self, command: str) -> tuple[int, str]:
        channel = self.session.transport().open_session(timeout=SSH_TIMEOUT_S)
        try:
            channel.exec_command(command)
            out = b""
            while True:
                block = channel.recv(65536)
                if not block:
                    break
                out += block
            return channel.recv_exit_status(), out.decode("utf-8", errors="replace")
        finally:
            channel.close()

    def _sha_on_host(self, paths: list[str]) -> dict[str, str]:
        if self._exec_ok is False:
            return {}
        found: dict[str, str] = {}
        for start in range(0, len(paths), 100):
            chunk = paths[start : start + 100]
            parsed: dict[str, str] = {}
            for tool in ("sha256sum --", "shasum -a 256 --"):
                try:
                    code, out = self._exec(f"{tool} {' '.join(shlex.quote(p) for p in chunk)}")
                except paramiko.SSHException:
                    self._exec_ok = False  # the host runs no commands (SFTP only)
                    return found
                parsed = parse_sha256(out, chunk) if code == 0 else {}
                if parsed:
                    break
            if not parsed:
                self._exec_ok = False
                return found
            self._exec_ok = True
            found.update(parsed)
        return found

    def _sha_by_reading(self, sftp: paramiko.SFTPClient, path: str) -> str:
        digest = hashlib.sha256()
        with sftp.open(path, "rb") as stream:
            stream.prefetch()
            while block := stream.read(1 << 20):
                digest.update(block)
        return digest.hexdigest()

    def _sha(self, sftp: paramiko.SFTPClient, paths: list[str]) -> dict[str, str]:
        found = self._sha_on_host(paths)
        for path in paths:
            if path not in found:
                found[path] = self._sha_by_reading(sftp, path)
        return found

    def checksums(self, folder: str, paths: list[str]) -> dict[str, str]:
        remote = self._remote(folder)
        absolute = {f"{remote}/{check_relative(p)}": p for p in paths}
        with self._sftp() as sftp:
            present = [path for path in absolute if _is_file(sftp, path)]
            return {absolute[a]: digest for a, digest in self._sha(sftp, present).items()}

    # -- copying ------------------------------------------------------------------

    def _makedirs(self, sftp: paramiko.SFTPClient, path: str, made: set[str]) -> None:
        parts = PurePosixPath(path).parts
        for i in range(2, len(parts) + 1):
            here = str(PurePosixPath(*parts[:i]))
            if here in made:
                continue
            try:
                sftp.stat(here)
            except FileNotFoundError:
                sftp.mkdir(here)
            made.add(here)

    def put(
        self,
        folder: str,
        puts: list[Put],
        progress: Callable[[Progress], None],
        cancelled: threading.Event,
    ) -> dict[str, str]:
        remote = self._remote(folder)
        total = sum(p.size for p in puts)
        done = 0
        written: dict[str, str] = {}
        made: set[str] = set()
        with self._sftp() as sftp:
            for item in puts:
                target = f"{remote}/{check_relative(item.remote)}"
                parent = str(PurePosixPath(target).parent)
                self._makedirs(sftp, parent, made)
                if _exists(sftp, target):
                    done += item.size
                    continue  # never replaced
                partial_dir = f"{parent}/{PARTIAL_DIR}"
                self._makedirs(sftp, partial_dir, made)
                partial = f"{partial_dir}/{PurePosixPath(target).name}"
                for attempt in (1, 2):
                    offset = _size(sftp, partial)
                    if offset is None or offset > item.size or attempt == 2:
                        offset = 0
                    self._send(sftp, item, partial, offset, done, total, progress, cancelled)
                    if self._sha(sftp, [partial]).get(partial) == item.sha256:
                        break
                    if attempt == 2:
                        sftp.remove(partial)  # our own partial copy, never a final file
                        raise UploadError(
                            f"{item.remote} arrived different from the local file twice"
                        )
                if not _exists(sftp, target):
                    try:
                        sftp.rename(partial, target)  # SFTP rename never replaces a file
                        written[item.remote] = item.sha256
                    except OSError:
                        # Refused because a file took the name meanwhile: it
                        # stays, and verification compares it. Anything else
                        # is a failure of the upload.
                        if not _exists(sftp, target):
                            raise
                if _exists(sftp, partial):
                    sftp.remove(partial)
                # Other partial copies may still be in it; then it stays for them.
                with contextlib.suppress(OSError):
                    sftp.rmdir(partial_dir)
                    made.discard(partial_dir)
                done += item.size
        progress(Progress(total, total))
        return written

    def _send(
        self,
        sftp: paramiko.SFTPClient,
        item: Put,
        partial: str,
        offset: int,
        before: int,
        total: int,
        progress: Callable[[Progress], None],
        cancelled: threading.Event,
    ) -> None:
        mode = "r+b" if offset else "wb"
        with item.local.open("rb") as source, sftp.open(partial, mode) as sink:
            sink.set_pipelined(True)
            if offset:  # "wb" already started an empty file
                sink.seek(offset)
                source.seek(offset)
            sent = offset
            while block := source.read(BLOCK):
                if cancelled.is_set():
                    raise Cancelled("Upload stopped; the next upload resumes where it stopped")
                sink.write(block)
                sent += len(block)
                progress(Progress(before + sent, total, item.remote))


def _size(sftp: paramiko.SFTPClient, path: str) -> int | None:
    try:
        return sftp.stat(path).st_size
    except FileNotFoundError:
        return None


def _exists(sftp: paramiko.SFTPClient, path: str) -> bool:
    return _size(sftp, path) is not None


def _is_file(sftp: paramiko.SFTPClient, path: str) -> bool:
    try:
        return stat.S_ISREG(sftp.stat(path).st_mode or 0)
    except FileNotFoundError:
        return False
