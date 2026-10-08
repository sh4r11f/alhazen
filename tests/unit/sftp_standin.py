"""A stand-in SSH/SFTP server for the upload tests (paramiko, in-process).

Serves this computer's own filesystem over SFTP (remote paths are local
paths), logs a user in with keyboard-interactive in two rounds — a password,
then a second-factor code — as a host with a second factor does, and runs
commands (``sha256sum``) unless told not to, like an SFTP-only account.
SFTP rename refuses to replace an existing file, as OpenSSH's does.
Loopback only; for tests.
"""

from __future__ import annotations

import os
import socket
import subprocess
import threading

import paramiko
from paramiko import SFTPAttributes, SFTPHandle, SFTPServer, SFTPServerInterface
from paramiko.sftp import SFTP_FAILURE, SFTP_OK


class _Handle(SFTPHandle):
    def stat(self):  # noqa: D102
        try:
            return SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)


class FilesystemSFTP(SFTPServerInterface):
    """SFTP over the real filesystem, paths as given."""

    def list_folder(self, path):
        try:
            return [
                SFTPAttributes.from_stat(os.lstat(os.path.join(path, n)), filename=n)
                for n in os.listdir(path)
            ]
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    def stat(self, path):
        try:
            return SFTPAttributes.from_stat(os.stat(path))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    lstat = stat

    def open(self, path, flags, attr):
        try:
            fd = os.open(path, flags | getattr(os, "O_BINARY", 0), 0o644)
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
            os.remove(path)
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def rename(self, oldpath, newpath):
        if os.path.exists(newpath):
            return SFTP_FAILURE  # SFTP's rename never replaces a file
        try:
            os.rename(oldpath, newpath)
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def mkdir(self, path, attr):
        try:
            os.mkdir(path)
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def rmdir(self, path):
        try:
            os.rmdir(path)
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
            done = subprocess.run(command.decode(), shell=True, capture_output=True)
            channel.sendall(done.stdout)
            channel.send_exit_status(done.returncode)
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
