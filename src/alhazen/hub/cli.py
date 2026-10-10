"""``alhazen hub``: the Experiment Hub from a terminal.

    alhazen hub connect URL           save this rig's hub (checks it answers)
    alhazen hub login [--username U]  sign in (password asked, never a flag)
    alhazen hub logout                sign out here and at the hub
    alhazen hub status                connection, operator, installs, uploads
    alhazen hub pack DIR --output F   preview, confirm and build a package
    alhazen hub push F --experiment ID   upload a built package (never publishes)
    alhazen hub install EXP VER --sha256 S --python PY --trust-code
    alhazen hub serve --config FILE   run the central hub service (hub extra)

The same state as ``alhazen dashboard`` (``--state-dir``, default
``~/.alhazen/dashboard``): a sign-in made here is the dashboard's, and the
other way round. Secrets are never accepted as arguments (shell history):
passwords come from getpass.

This module never imports ``alhazen.cli``; registering an install with the
workspace is done by the caller's ``install`` function (cli/main.py passes
``alhazen.cli.workspace_hub.cli_install``).
"""

from __future__ import annotations

import argparse
import getpass
import importlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from alhazen.hub.client import HubClient, HubError, api_path, canonical_base, probe_hub
from alhazen.hub.credentials import Connection, RigState, sign_in
from alhazen.hub.installation import TRUST_STATEMENT, InstallStore
from alhazen.hub.source import pack, suggest_metadata
from alhazen.hub.sync import Outbox, public_job

InstallFunction = Callable[..., dict[str, Any]]


def add_parser(sub: Any) -> None:
    """The ``hub`` subcommand and its subcommands, on the top-level parser."""
    hub = sub.add_parser("hub", help="the Experiment Hub: connect, sign in, install, upload")
    hub.add_argument("--state-dir", default=None, help="the dashboard's workspace directory")
    commands = hub.add_subparsers(dest="hub_command")
    connect = commands.add_parser("connect", help="save the hub this rig uses")
    connect.add_argument("url", help="the hub's https:// address")
    connect.add_argument(
        "--allow-http-loopback",
        action="store_true",
        help="allow http:// for a hub on this computer (local development only)",
    )
    login = commands.add_parser("login", help="sign in to the hub (password asked)")
    login.add_argument("--username", default=None)
    commands.add_parser("logout", help="sign out here and at the hub")
    commands.add_parser("status", help="connection, operator, installs and uploads")
    pack_cmd = commands.add_parser("pack", help="build a package from an experiment folder")
    pack_cmd.add_argument("project", help="the experiment folder (with run.py)")
    pack_cmd.add_argument("--output", required=True, help="the .zip to write")
    pack_cmd.add_argument("--license", default=None, help="the package's licence")
    pack_cmd.add_argument("--yes", action="store_true", help="do not ask to confirm the files")
    push = commands.add_parser("push", help="upload a package as a new private release")
    push.add_argument("bundle", help="a .zip built by `alhazen hub pack`")
    push.add_argument("--experiment", required=True, help="the hub experiment's id")
    install = commands.add_parser("install", help="install and register a hub release")
    install.add_argument("experiment_id")
    install.add_argument("version_id")
    install.add_argument("--sha256", required=True, help="the release's SHA-256, as reviewed")
    install.add_argument("--python", required=True, help="the interpreter to run it with")
    install.add_argument(
        "--trust-code", action="store_true", help="confirm you trust this release's code"
    )
    serve = commands.add_parser("serve", help="run the central hub service (needs the hub extra)")
    serve.add_argument("--config", required=True, help="the service's TOML settings file")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8750)


def state_directory(args: argparse.Namespace) -> Path:
    base = Path(args.state_dir) if args.state_dir else Path.home() / ".alhazen" / "dashboard"
    return base.expanduser().resolve()


def run(args: argparse.Namespace, *, install: InstallFunction | None = None) -> int:
    """Run one ``alhazen hub`` subcommand; the exit code."""
    command = getattr(args, "hub_command", None)
    if command is None:
        print("usage: alhazen hub {connect,login,logout,status,pack,push,install,serve}")
        return 2
    try:
        if command == "serve":
            return _serve(args)
        directory = state_directory(args)
        state = RigState(directory / "hub")
        if command == "connect":
            return _connect(args, state)
        if command == "login":
            return _login(args, state)
        if command == "logout":
            return _logout(state)
        if command == "status":
            return _status(directory, state)
        if command == "pack":
            return _pack(args, directory)
        if command == "push":
            return _push(args, state)
        if command == "install":
            if install is None:
                raise ValueError("install needs the workspace (run through `alhazen hub`)")
            return _install(args, directory, install)
    except HubError as exc:
        print(f"HUB ERROR ({exc.code}): {exc.message}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"CANNOT {command.upper()}: {exc}", file=sys.stderr)
        return 1
    raise ValueError(f"Unknown hub command {command}")  # argparse refuses others first


def _serve(args: argparse.Namespace) -> int:
    try:
        app = importlib.import_module("alhazen.hub.app")
    except ImportError as exc:
        print(
            f"The hub service is not installed ({exc}); install alhazen-vision[hub]",
            file=sys.stderr,
        )
        return 1
    app.serve(Path(args.config), args.host, args.port)
    return 0


def _client(state: RigState) -> HubClient:
    connection = state.connection()
    if connection is None:
        raise ValueError("No hub is configured; run `alhazen hub connect URL` first")
    credential = state.credential()
    return HubClient(connection.base, credential.token if credential else None)


def _connect(args: argparse.Namespace, state: RigState) -> int:
    base = canonical_base(args.url, allow_http_loopback=args.allow_http_loopback)
    probe_hub(base)
    previous = state.connection()
    state.save_connection(Connection(base, args.allow_http_loopback))
    print(f"Connected to {base}")
    if previous is not None and previous.base != base:
        print("The previous hub's sign-in was forgotten; sign in again.")
    return 0


def _login(args: argparse.Namespace, state: RigState) -> int:
    client = _client(state)
    username = args.username or input("Hub username: ").strip()
    password = getpass.getpass("Hub password: ")
    credential = sign_in(HubClient(client.base), username, password)
    state.save_credential(credential)
    print(f"Signed in as {credential.user['username']} at {credential.base}")
    return 0


def _logout(state: RigState) -> int:
    credential = state.credential()
    if credential is None:
        print("Not signed in.")
        return 0
    try:
        HubClient(credential.base, credential.token).json("POST", "/auth/logout")
        print("Signed out at the hub and on this rig.")
    except HubError as exc:
        print(f"Signed out on this rig; the hub could not confirm ({exc.message}).")
    state.clear_credential()
    return 0


def _status(directory: Path, state: RigState) -> int:
    public = state.public()
    credential = state.credential()
    installs = InstallStore(directory / "hub").records()
    jobs = Outbox(directory / "hub" / "outbox").visible(
        credential.base if credential else None, credential.user_id if credential else None
    )
    print(
        json.dumps(
            {
                **public,
                "installed": [
                    {k: r.get(k) for k in ("name", "version", "sha256", "status", "path")}
                    for r in installs
                ],
                "jobs": [public_job(j) for j in jobs],
            },
            indent=2,
        )
    )
    return 0


def _pack(args: argparse.Namespace, directory: Path) -> int:
    packages = importlib.import_module("alhazen.hub.packages")
    root = Path(args.project).expanduser().resolve()
    files = packages.suggest_files(root)
    metadata = suggest_metadata(root, files)
    if args.license:
        metadata["license"] = args.license
    print(f"{metadata['name']} {metadata['version']}: {len(files)} files")
    for name in files:
        print(f"  {name}")
    if not metadata.get("license"):
        raise ValueError("Give the package a licence with --license")
    if not args.yes and input("Include exactly these files? [y/N] ").strip().lower() != "y":
        print("Nothing built.")
        return 1
    info = pack(root, packages, Path(args.output), metadata, files)
    print(f"Built {args.output}: {info.size} bytes, sha256 {info.sha256}")
    return 0


def _push(args: argparse.Namespace, state: RigState) -> int:
    client = _client(state)
    if not client.token:
        raise ValueError("Sign in first: `alhazen hub login`")
    bundle = Path(args.bundle)
    with bundle.open("rb") as stream:
        answer = client.json(
            "POST",
            api_path("experiments", args.experiment, "versions"),
            data=stream,
            length=bundle.stat().st_size,
            content_type="application/zip",
        )
    version = answer.get("version", answer) if isinstance(answer, dict) else answer
    print(json.dumps(version, indent=2))
    print("Uploaded as a private release; publishing is a separate, explicit step.")
    return 0


def _install(args: argparse.Namespace, directory: Path, install: InstallFunction) -> int:
    print(TRUST_STATEMENT)
    if not args.trust_code:
        print("Nothing installed: confirm with --trust-code.", file=sys.stderr)
        return 1
    record = install(
        directory,
        experiment_id=args.experiment_id,
        version_id=args.version_id,
        sha256=args.sha256,
        python=args.python,
    )
    print(json.dumps(record, indent=2))
    return 0
