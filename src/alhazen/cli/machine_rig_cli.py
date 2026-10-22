"""``alhazen rig init``, ``rig list`` and ``rig show``: this machine's rig configs from a
terminal.

The experiment workspace (``alhazen dashboard``) runs every session on one of this
machine's rig configs, kept in its state folder (alhazen.cli.machine_rig). The General
page adds and edits them; these commands do the same from a terminal — to set a machine up
before anyone opens the page, or from a setup script — and show what is there.

    alhazen rig init --name lab --from lab                # alhazen's shared lab
    alhazen rig init --name booth --from configs/rig-booth.yaml --project PATH
    alhazen rig list
    alhazen rig show [--config NAME]

``--from`` takes anything ``--rig`` takes: a shared rig's name, an
experiment's rig by name (looked for under ``--project``, else the current
folder) or a path. The rig is written out complete, any ``extends`` merged;
event names are left out (they are chosen per session). ``--state-dir`` is
the dashboard's own (default ``~/.alhazen/dashboard``).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from alhazen.cli.machine_rig import MachineRigError, MachineRigs
from alhazen.config.experiment import experiment_title
from alhazen.config.rigs import resolve_rig
from alhazen.errors import ConfigError


def default_state() -> Path:
    """The dashboard's own state folder. Looked up when a rig command runs, not at import:
    every ``alhazen`` command imports this module, and on a machine whose home folder cannot
    be determined (no HOME / USERPROFILE) an import-time ``Path.home()`` made every command,
    ``alhazen validate`` included, fail before it started."""
    return Path.home() / ".alhazen" / "dashboard"


def add_parser(sub: Any) -> None:
    parser = sub.add_parser("rig", help="add or show this machine's rig configs for the dashboard")
    actions = parser.add_subparsers(dest="rig_action", required=True)
    init = actions.add_parser(
        "init", help="add a rig config to this machine, from a shared or an experiment's rig"
    )
    init.add_argument("--name", required=True, help="the config's name, e.g. lab or eyelink")
    init.add_argument(
        "--default", action="store_true", help="make it the default (the first config always is)"
    )
    init.add_argument(
        "--from",
        dest="source",
        required=True,
        help="the rig it starts from: a name (lab, alhazen/lab, amodal-averaging/lab) or a path",
    )
    init.add_argument(
        "--project",
        default=None,
        metavar="PATH",
        help="the experiment whose rigs --from may name (default: the current folder)",
    )
    init.add_argument("--state-dir", default=None, help="the dashboard's state folder")
    listed = actions.add_parser("list", help="list this machine's rig configs")
    listed.add_argument("--state-dir", default=None, help="the dashboard's state folder")
    show = actions.add_parser("show", help="print one of this machine's rig configs")
    show.add_argument("--config", default=None, help="its name (default: the default config)")
    show.add_argument("--state-dir", default=None, help="the dashboard's state folder")


def run(args: argparse.Namespace) -> int:
    machine = MachineRigs(Path(args.state_dir).expanduser() if args.state_dir else default_state())
    if args.rig_action in ("show", "list"):
        status = machine.status()
        if status.get("error") or not status["configs"]:
            print(status.get("error") or "This machine has no rig config yet: `alhazen rig init`.")
            return 1
        if args.rig_action == "list":
            for config in status["configs"]:
                mark = "*" if config["default"] else " "
                problem = f"  INVALID: {config['problem']}" if config.get("problem") else ""
                print(f"{mark} {config['name']}  ({config['file']}){problem}")
            return 0
        try:
            config = next(
                c
                for c in status["configs"]
                if (args.config and args.config in (c["id"], c["name"]))
                or (not args.config and c["default"])
            )
        except StopIteration:
            print(f"This machine has no rig config {args.config!r} (`alhazen rig list`)")
            return 1
        print(f"# this machine's rig config: {config['name']} ({config['file']})")
        if config.get("problem"):
            print(f"# INVALID: {config['problem']}")
        text = config.get("text") or ""
        print(text, end="" if text.endswith("\n") else "\n")
        return 0
    root = Path(args.project).expanduser().resolve() if args.project else Path.cwd()
    source = args.source
    # A relative path is read from the current folder, as --rig reads one;
    # failing that, from the experiment folder --project names.
    if not Path(source).is_file() and (root / source).is_file():
        source = str(root / source)
    try:
        ref = resolve_rig(source, root)
        label = ref.qualified(experiment_title(root).slug)
        status = machine.setup(
            args.name,
            ref.path,
            shared=None,
            template={
                "kind": "shared" if ref.source == "alhazen" else "experiment",
                "label": label,
            },
            by="alhazen rig init",
        )
        if args.default:
            machine.set_default(status["id"])
    except (MachineRigError, ConfigError) as exc:
        print(f"CANNOT ADD: {exc}")
        return 1
    is_default = args.default or status["default"]
    print(
        f"added this machine's rig config {status['name']}: {status['file']} (from {label})"
        + (", the default" if is_default else "")
    )
    left_out = status.get("left_out") or {}
    if left_out:
        names = sorted({*left_out.get("event_lines", {}), *left_out.get("photodiode_events", [])})
        print(
            f"left out the event names {', '.join(names)}: they are chosen per session, on the "
            "dashboard's Run page"
        )
    return 0
