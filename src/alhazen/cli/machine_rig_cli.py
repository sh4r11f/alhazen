"""``alhazen rig init`` and ``alhazen rig show``: this machine's rig from a terminal.

The experiment workspace (``alhazen dashboard``) runs every session on one rig
per machine, kept in its state folder (alhazen.cli.machine_rig). It asks for
that rig the first time it is opened; these commands do the same from a
terminal — to set a machine up before anyone opens the page, or from a setup
script — and show what is there.

    alhazen rig init --name lab --from lab                # alhazen's shared lab
    alhazen rig init --name booth --from configs/rig-booth.yaml --project PATH
    alhazen rig show

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

from alhazen.cli.machine_rig import MachineRig, MachineRigError
from alhazen.config.experiment import experiment_title
from alhazen.config.rigs import resolve_rig
from alhazen.errors import ConfigError

DEFAULT_STATE = Path.home() / ".alhazen" / "dashboard"


def add_parser(sub: Any) -> None:
    parser = sub.add_parser("rig", help="set up or show this machine's rig for the dashboard")
    actions = parser.add_subparsers(dest="rig_action", required=True)
    init = actions.add_parser(
        "init", help="set up this machine's rig, once, from a shared or an experiment's rig"
    )
    init.add_argument("--name", required=True, help="the machine's name, e.g. lab")
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
    show = actions.add_parser("show", help="print this machine's rig")
    show.add_argument("--state-dir", default=None, help="the dashboard's state folder")


def run(args: argparse.Namespace) -> int:
    machine = MachineRig(Path(args.state_dir).expanduser() if args.state_dir else DEFAULT_STATE)
    if args.rig_action == "show":
        status = machine.status()
        if not status["configured"]:
            print(status.get("error") or "This machine has no rig yet: `alhazen rig init`.")
            return 1
        print(f"# this machine's rig: {status['name']} ({status['file']})")
        if status.get("problem"):
            print(f"# INVALID: {status['problem']}")
        print(status["text"], end="" if status["text"].endswith("\n") else "\n")
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
        )
    except (MachineRigError, ConfigError) as exc:
        print(f"CANNOT SET UP: {exc}")
        return 1
    print(f"this machine's rig is {status['name']}: {status['file']} (from {label})")
    left_out = status.get("left_out") or {}
    if left_out:
        names = sorted({*left_out.get("event_lines", {}), *left_out.get("photodiode_events", [])})
        print(
            f"left out the event names {', '.join(names)}: they are chosen per session, on the "
            "dashboard's Run page"
        )
    return 0
