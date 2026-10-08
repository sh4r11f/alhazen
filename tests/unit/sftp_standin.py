"""A stand-in SSH/SFTP server for the upload tests (paramiko, in-process).

Serves this computer's own filesystem over SFTP, logs a user in with
keyboard-interactive in two rounds — a password, then a second-factor code — as a host with a second factor does, and runs
commands (``sha256sum``) unless told not to, like an SFTP-only account.
SFTP rename refuses to replace an existing file, as OpenSSH's does.
Loopback only; for tests.

Remote paths are POSIX, as on a real archive host. They name local paths on
the drive the temporary folder is on: ``remote_path(p)`` gives the remote
name of a local path and the server maps it back, so the same tests run on
Windows (where ``/Users/...`` is ``C:\\Users\\...``) as on Linux and macOS
(where the two are the same string). ``sha256sum`` is answered in-process,
in sha256sum's own format, so a host check does not depend on the tools this
computer happens to have.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import shlex
import socket
import subprocess
import tempfile
import threading
from pathlib import Path

import paramiko
from paramiko import SFTPAttributes, SFTPHandle, SFTPServer, SFTPServerInterface
from paramiko.sftp import SFTP_FAILURE, SFTP_OK

# The drive the tests' temporary folders are on: "/" on Linux and macOS.
_ANCHOR = Path(tempfile.gettempdir()).anchor


def remote_path(local: os.PathLike[str] | str) -> str:
    """The stand-in's remote (POSIX) name for a local path on ``_ANCHOR``."""
    parts = Path(local).resolve().parts
    return "/" + "/".join(parts[1:])


def local_path(remote: str) -> str:
    """The local path a remote (POSIX) path names."""
    return os.path.join(_ANCHOR, *[part for part in remote.split("/") if part])


def _sha256sum(command: str) -> tuple[bytes, int]:
    """``sha256sum -- <paths>`` over remote paths, answered here; anything
    else is run by the shell as before."""
    words = shlex.split(command)
    paths = [w for w in words[1:] if w != "--"]
    out, code = [], 0
    for path in paths:
        try:
            digest = hashlib.sha256(Path(local_path(path)).read_bytes()).hexdigest()
        except OSError:
            code = 1
            continue
        out.append(f"{digest}  {path}\n")
    return "".join(out).encode(), code


class _Handle(SFTPHandle):
    def stat(self):  # noqa: D102
        try:
            return SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)


class FilesystemSFTP(SFTPServerInterface):
    """SFTP over the real filesystem, paths as given."""

    def canonicalize(self, path):
        return posixpath.normpath(path if path.startswith("/") else "/" + path)

    def list_folder(self, path):
        path = local_path(path)
        try:
            return [
                SFTPAttributes.from_stat(os.lstat(os.path.join(path, n)), filename=n)
                for n in os.listdir(path)
            ]
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    def stat(self, path):
        try:
            return SFTPAttributes.from_stat(os.stat(local_path(path)))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    lstat = stat

    def open(self, path, flags, attr):
        try:
            fd = os.open(local_path(path), flags | getattr(os, "O_BINARY", 0), 0o644)
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        if flags & os.O_WRONLY:
            mode = "ab" if flags & os.O_APPEND else "wb"
        elif flags & os.O_RDWR:
            mode = "a+b" if flags & os.O_APPEND else "r+b"
        else:
            mode = "rb"
        handle = _Handle(flags)
        handle.filename = path
        handle.readfile = handle.writefile = os.fdopen(fd, mode)
        return handle

    def remove(self, path):
        try:
            os.remove(local_path(path))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def rename(self, oldpath, newpath):
        oldpath, newpath = local_path(oldpath), local_path(newpath)
        if os.path.exists(newpath):
            return SFTP_FAILURE  # SFTP's rename never replaces a file
        try:
            os.rename(oldpath, newpath)
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def mkdir(self, path, attr):
        try:
            os.mkdir(local_path(path))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def rmdir(self, path):
        try:
            os.rmdir(local_path(path))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def chattr(self, path, attr):
        return SFTP_OK


class _Server(paramiko.ServerInterface):
    def __init__(self, standin):
        self.standin = standin
        self.round = 0

    def check_channel_request(self, kind, chanid):
        return (
            paramiko.OPEN_SUCCEEDED
            if kind == "session"
            else (paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED)
        )

    def get_allowed_auths(self, username):
        return "keyboard-interactive"

    def check_auth_interactive(self, username, submethods):
        if username != self.standin.user:
            return paramiko.AUTH_FAILED
        self.round = 1
        return paramiko.InteractiveQuery("", "", ("Password: ", False))

    def check_auth_interactive_response(self, responses):
        if self.round == 1 and list(responses) == [self.standin.password]:
            self.round = 2
            return paramiko.InteractiveQuery(
                "Second factor",
                "Enter a passcode or 1 for a push.",
                ("Passcode or option (1-1): ", True),
            )
        if self.round == 2 and list(responses) == [self.standin.code]:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_exec_request(self, channel, command):
        if not self.standin.exec_ok:
            return False
        self.standin.commands.append(command.decode())

        def run():
            text = command.decode()
            if text.startswith("sha256sum"):
                stdout, code = _sha256sum(text)
            else:
                done = subprocess.run(text, shell=True, capture_output=True)
                stdout, code = done.stdout, done.returncode
            channel.sendall(stdout)
            channel.send_exit_status(code)
            channel.close()

        threading.Thread(target=run, daemon=True).start()
        return True


class StandIn:
    """The server: ``with StandIn() as server:`` listens on ``server.port``."""

    def __init__(
        self, *, user="alice", password="correct horse", code="246810", exec_ok=True, host_key=None
    ):
        self.user, self.password, self.code, self.exec_ok = user, password, code, exec_ok
        self.host_key = host_key or paramiko.ECDSAKey.generate()
        self.commands: list[str] = []
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._transports: list[paramiko.Transport] = []
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            transport = paramiko.Transport(conn)
            transport.add_server_key(self.host_key)
            transport.set_subsystem_handler("sftp", SFTPServer, FilesystemSFTP)
            transport.start_server(server=_Server(self))
            self._transports.append(transport)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop = True
        self._sock.close()
        for transport in self._transports:
            transport.close()
