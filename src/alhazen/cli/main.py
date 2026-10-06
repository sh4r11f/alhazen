"""The alhazen CLI: the commands an experimenter actually types.

    alhazen new <name>        scaffold an experiment package
    alhazen run --task ...    run one session of an installed task
    alhazen validate --rig    is this config file well-formed?
    alhazen check-rig --rig   is this rig actually wired? (before the subject)
    alhazen rigs              which rigs --rig can name here, and whose each is
    alhazen sim-sorter        stand in for the real-time spike sorter (no rig)
    alhazen calibrate ...     verify the monitor's geometry and gamma
    alhazen monitor ...       tell PsychoPy about this rig's monitor
    alhazen report --run      what happened, and does the data check out?

Each command does one thing an experimenter needs, and each does it through
the same code a session would: ``check-rig`` constructs the real device
backends, ``run`` builds a real session, ``report`` reads a real run
directory. A tool whose "OK" comes from a parallel implementation is a tool
whose OK means nothing.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from alhazen.cli.console_break import interrupt_on_console_break
from alhazen.config.experiment import experiment_title
from alhazen.config.loader import load_rig
from alhazen.config.models import normalize_initials
from alhazen.config.rigs import (
    collecting_rigs,
    list_rigs,
    local_rig_file,
    resolve_rig,
    rig_extends,
)
from alhazen.errors import AlhazenError, ConfigError, DataError, DisplayError
from alhazen.modes import Mode, flag_refusal, real_data_refusal
from alhazen.session.checks import check_rig, format_result
from alhazen.testing.sorter import FAULTS
from alhazen.version import get_version

# What --rig takes, worded once for every subcommand that has one, so that no
# --help says "path" while the flag also takes a name.
RIG_HELP = (
    "the rig: a name, such as lab (the experiment's configs/rig-lab.yaml, else "
    "alhazen's shared one; alhazen/lab is always the shared one), or the path to a "
    "rig YAML file. `alhazen rigs` lists the names"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alhazen", description="Vision science experiments.")
    parser.add_argument("--version", action="version", version=f"alhazen {get_version()}")
    sub = parser.add_subparsers(dest="command")

    validate = sub.add_parser("validate", help="validate a config file")
    validate.add_argument("--rig", required=True, help=RIG_HELP)

    rigs = sub.add_parser("rigs", help="list the rigs --rig can name, and whose each one is")
    rigs.add_argument(
        "--project",
        default=None,
        metavar="PATH",
        help="the experiment's folder, holding configs/ (default: the current folder)",
    )

    preview = sub.add_parser(
        "preview",
        help="draw every stimulus the experiment declares ([tool.alhazen] stimuli) as PNGs",
    )
    preview.add_argument(
        "--project",
        default=None,
        metavar="PATH",
        help="the experiment's folder, holding its pyproject.toml (default: the current folder)",
    )
    preview.add_argument(
        "--rig", required=True, help=RIG_HELP + ". The images are drawn at its pixel scale"
    )
    preview.add_argument(
        "--out",
        required=True,
        metavar="FOLDER",
        help="where to write one PNG per stimulus and their index, README.md",
    )

    new = sub.add_parser("new", help="scaffold a new experiment package")
    new.add_argument("name", help="package name, e.g. saccade_bias")
    new.add_argument("--into", default=".", help="where to create it (default: here)")
    new.add_argument("--force", action="store_true", help="write into a non-empty directory")

    run = sub.add_parser("run", help="run one session of an installed task")
    run.add_argument("--task", default=None, help="the task's registered name")
    run.add_argument("--list", action="store_true", help="list installed tasks and exit")
    add_mode_arguments(run)
    dashboard = sub.add_parser("dashboard", help="open the experiment launcher in a browser")
    dashboard.add_argument(
        "--project", action="append", default=[], help="experiment folder to add"
    )
    dashboard.add_argument("--port", type=int, default=0, help="loopback port (default: automatic)")
    dashboard.add_argument("--state-dir", default=None, help="registry, logs and media directory")
    dashboard.add_argument(
        "--no-browser", action="store_true", help="print the URL without opening it"
    )
    calibrate = sub.add_parser("calibrate", help="check a monitor's geometry and gamma")
    calibrate_sub = calibrate.add_subparsers(dest="calibration")
    ruler = calibrate_sub.add_parser(
        "ruler", help="what a known angular size should measure on the panel"
    )
    ruler.add_argument("--rig", required=True, help=RIG_HELP)
    ruler.add_argument("--dva", type=float, default=10.0, help="the bar's size in degrees")
    ruler.add_argument(
        "--windowed", action="store_true", help="bordered window rather than fullscreen"
    )
    gamma = calibrate_sub.add_parser("gamma", help="fit a gamma curve from photometer measurements")
    gamma.add_argument("--rig", required=True, help=RIG_HELP)
    gamma.add_argument(
        "--measurements", required=True, help="CSV with 'level' and 'luminance' columns"
    )

    monitor = sub.add_parser("monitor", help="register this rig's monitor with PsychoPy")
    monitor_sub = monitor.add_subparsers(dest="monitor_command")
    monitor_register = monitor_sub.add_parser(
        "register", help="write the rig's monitor into PsychoPy's monitor database"
    )
    monitor_register.add_argument("--rig", required=True, help=RIG_HELP)
    monitor_show = monitor_sub.add_parser(
        "show", help="compare a rig's monitor with what PsychoPy has stored"
    )
    monitor_show.add_argument("--rig", required=True, help=RIG_HELP)
    monitor_sub.add_parser("list", help="every monitor PsychoPy knows on this machine")

    report = sub.add_parser("report", help="summarise a finished run, and align it to a recording")
    report.add_argument("--run", required=True, help="path to a run directory")
    report.add_argument(
        "--neural",
        default=None,
        help="path to the matching recording run (e.g. a SpikeGLX *_g0 directory)",
    )
    report.add_argument(
        "--analog-channel",
        type=int,
        default=0,
        help="which analog channel carries the photodiode (default: %(default)s)",
    )

    check = sub.add_parser("check-rig", help="smoke-test a rig's devices before a session")
    check.add_argument("--rig", required=True, help=RIG_HELP)
    check.add_argument(
        "--pulse",
        action="store_true",
        help="also fire one real reward pulse and one pulse per mapped sync line",
    )
    check.add_argument(
        "--record",
        default=None,
        metavar="PATH",
        help="write what each device did to PATH (JSON), with a readable summary "
        "beside it as PATH.txt; written whether the check passes or fails",
    )

    sorter = sub.add_parser(
        "sim-sorter",
        help="publish a simulated sorted-spike stream, to rehearse check-rig with no rig",
    )
    sorter.add_argument(
        "--address",
        default="tcp://127.0.0.1:5556",
        help="ZeroMQ endpoint to publish on (default: %(default)s)",
    )
    sorter.add_argument(
        "--units", type=int, default=4, help="how many units to publish (default: %(default)s)"
    )
    sorter.add_argument(
        "--firing-hz",
        type=float,
        default=20.0,
        help="mean firing rate per unit (default: %(default)s)",
    )
    sorter.add_argument(
        "--sample-rate-hz",
        type=float,
        default=30000.0,
        help="the acquisition sample rate this stream claims (default: %(default)s)",
    )
    sorter.add_argument(
        "--heartbeat-ms",
        type=float,
        default=200.0,
        help="how often to publish coverage; this is what sets the lag check-rig reports "
        "(default: %(default)s)",
    )
    sorter.add_argument(
        "--seed", type=int, default=0, help="spike-train seed (default: %(default)s)"
    )
    sorter.add_argument(
        "--seconds",
        type=float,
        default=None,
        help="stop after this long (default: run until interrupted)",
    )
    sorter.add_argument(
        "--fault",
        default="none",
        choices=list(FAULTS),
        help="publish a specific non-conformance instead, to rehearse the failure "
        "(default: %(default)s)",
    )

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    # The command line as this parser received it, for a session's run
    # folder to record (session.json's `command`). The program is written as
    # `alhazen`, the command a person types, whether this was started through
    # the console script or `python -m alhazen.cli.main`.
    args.invocation = ["alhazen", *(sys.argv[1:] if argv is None else argv)]
    # argparse has already refused any name not in the table (its `choices`
    # are the subparsers above), so the lookup cannot miss.
    return _COMMANDS[args.command](args, parser)


# One handler per subcommand. Each takes the parsed arguments and the
# top-level parser (the ones with sub-subcommands print their own --help
# through it) and returns the exit code.
Handler = Callable[[argparse.Namespace, argparse.ArgumentParser], int]


def _dashboard(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    from alhazen.cli.dashboard import serve

    try:
        return serve(args)
    except (OSError, ValueError) as exc:
        print(f"CANNOT OPEN DASHBOARD: {exc}", file=sys.stderr)
        return 1


def _experiment_root(task_class: Any = None) -> Callable[[], Path]:
    """Where a rig name is looked for among the experiment's own rigs — found
    only when a name needs it (``alhazen.config.rigs.ExperimentRoot``).

    With a task (``alhazen run --task``, an experiment's ``run.py``) it is the
    experiment the task's code belongs to, the folder holding its
    pyproject.toml (``alhazen.config.experiment``), wherever the command is
    typed: ``--rig lab`` from a terminal in another folder still finds that
    experiment's lab. With no task — validate, check-rig, calibrate, monitor,
    rigs, and measure mode from ``alhazen run`` — it is the current folder,
    which is where a relative ``--rig`` path is read from too. A task installed
    from a wheel has no folder of its own, and the current folder stands in.

    Lazy because finding the experiment reads its pyproject.toml, and a
    ``--rig`` given as a path must keep working for a task that has none.
    """

    def root() -> Path:
        if task_class is not None:
            from alhazen.config.experiment import find_experiment

            found = find_experiment(task_class).root
            if found is not None:
                return found
        return Path.cwd()

    return root


def _validate(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Is this rig file well-formed? Loads it exactly as a session would."""
    try:
        ref = resolve_rig(args.rig, _experiment_root())
        rig = load_rig(ref.path)
        # Loaded already, so this second read cannot fail on the file itself;
        # it is what lets the line say the file is not the whole rig.
        base = rig_extends(ref.path)
    except ConfigError as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return 1
    extends = f", extends alhazen/{base}" if base else ""
    print(
        f"OK: {ref.describe()}{extends} — {rig.display.backend} display, "
        f"{rig.monitor.width_px}x{rig.monitor.height_px}@{rig.monitor.refresh_rate_hz:g}Hz, "
        f"data_root={rig.data_root}"
    )
    return 0


def _preview(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Draw every stimulus the experiment declares into ``--out``
    (``alhazen.stimuli.preview``). No task and no parameter file: the
    stimuli are the experiment's, whichever task or configuration runs them.

    Prints each file written, the index last. A declaration that cannot be
    used, a rig that cannot be read or an output folder that would be left
    misleading exits 1 with the reason, and nothing written.
    """
    from alhazen.stimuli.preview import write_preview

    root = Path(args.project).expanduser() if args.project else Path.cwd()
    if not (root / "pyproject.toml").is_file():
        # Said here, where the flag can be named: without it the reason would
        # read "declares no stimuli", which sends the reader to the wrong file.
        print(
            f"CANNOT PREVIEW: {root} holds no pyproject.toml. Run this in the experiment's "
            "folder, or name that folder with --project",
            file=sys.stderr,
        )
        return 1
    try:
        written = write_preview(root, args.rig, Path(args.out))
    except ConfigError as e:
        print(f"CANNOT PREVIEW: {e}", file=sys.stderr)
        return 1
    for path in written:
        print(path)
    return 0


def _rigs(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Every rig ``--rig`` can name from an experiment's folder, and whose each is.

    One table: the experiment's own rigs first, then alhazen's shared ones,
    with the file each is read from and what a reader would otherwise have
    to open it to learn — that it extends a shared rig, or that a shared rig
    is hidden from ``--rig <name>`` by the experiment's own of that name.

    Exits 1 when a rig here would be refused by name — a file that cannot be
    read, or two of the experiment's files sharing a name — so a script that
    checks a checkout can stop on it; the table is printed either way.
    """
    root = Path(args.project).expanduser() if args.project else Path.cwd()
    if not root.is_dir():
        print(f"CANNOT LIST RIGS: {root} is not a folder", file=sys.stderr)
        return 1
    try:
        rigs = list_rigs(root)
    except ConfigError as e:
        print(f"CANNOT LIST RIGS: {e}", file=sys.stderr)
        return 1
    counts: dict[str, int] = {}
    for ref in rigs:
        if ref.source == "experiment":
            counts[ref.name] = counts.get(ref.name, 0) + 1

    # Each rig by its qualified name, the spelling that says whose it is —
    # <experiment>/<name> or alhazen/<name> — and which --rig takes as well
    # as the bare name (alhazen.config.rigs.resolve_rig). The experiment's
    # part is its slug, read from its pyproject.toml without importing it.
    title = experiment_title(root)
    if title.error:
        # The slug fell back to the folder's name; say why, or a reader who
        # typed the [project] name would not know why it is refused.
        print(f"note: {title.error}", file=sys.stderr)
    rows: list[tuple[str, str, str, str]] = []
    problems = 0
    for ref in rigs:
        notes = []
        if ref.source == "experiment":
            # Relative to the experiment, as it would be typed from there.
            shown = ref.path.relative_to(root).as_posix()
            try:
                base = rig_extends(ref.path)
            except ConfigError as e:
                problems += 1
                notes.append(f"UNREADABLE: {str(e).splitlines()[0]}")
            else:
                if base:
                    notes.append(f"extends alhazen/{base}")
            if counts[ref.name] > 1:
                problems += 1
                notes.append(f"DUPLICATE NAME: --rig {ref.name} is refused until one is renamed")
        else:
            shown = ref.path.name
            if ref.shadowed:
                notes.append(
                    f"shadowed by the experiment's {ref.name} (reach this one with "
                    f"--rig alhazen/{ref.name})"
                )
        rows.append((ref.qualified(title.slug), ref.source, shown, "; ".join(notes)))

    print(
        f"rigs for {root.resolve()} (experiment {title.slug}) — give --rig a NAME, with or "
        "without its owner, or the path to a rig file\n"
    )
    header = ("NAME", "SOURCE", "FILE", "NOTE")
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(3)]
    for row in [header, *rows]:
        cells = [cell.ljust(width) for cell, width in zip(row[:3], widths, strict=True)]
        print(("  " + "  ".join([*cells, row[3]])).rstrip())
    if not any(ref.source == "experiment" for ref in rigs):
        print(f"\n  (no rig-<name>.yaml under {root / 'configs'}: only alhazen's shared rigs)")
    shared = next((ref.path.parent for ref in rigs if ref.source == "alhazen"), None)
    if shared is not None:
        print(f"\nalhazen's shared rigs (alhazen {get_version()}) are in {shared}")
    return 1 if problems else 0


def _new(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Scaffold an experiment package, and say what to type next."""
    from alhazen._scaffold import scaffold, task_name

    try:
        root = scaffold(args.name, Path(args.into), force=args.force)
    except ConfigError as e:
        print(f"CANNOT SCAFFOLD: {e}", file=sys.stderr)
        return 1
    # Every session names its task, so the commands printed do too — the
    # same name the package's entry point registers.
    task = task_name(args.name)
    print(f"created {root}")
    print("\nnext:")
    print(f"  cd {root}")
    print('  pip install -e ".[dev]"')
    print("  pytest")
    print(f"  python run.py --task {task} --mode simulate --rig configs/rig-lab.yaml --headless")
    print("\nthen, on a machine with a screen:")
    print(f"  python run.py --task {task} --mode demo --rig configs/rig-mac.yaml")
    return 0


def _report(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Summarise a finished run, and align it to a recording when given one."""
    from alhazen.analysis.report import build_report

    try:
        session_report = build_report(args.run, args.neural, analog_channel=args.analog_channel)
    except AlhazenError as e:
        # A run that cannot be read at all is not a report with problems,
        # it is a path that is wrong.
        print(f"CANNOT READ RUN: {e}", file=sys.stderr)
        return 1
    print(session_report.render())
    written = session_report.save()
    print(f"written: {written}")
    # Non-zero when the manifest failed or an alignment was refused, so
    # this is usable in a pipeline that must not carry on past bad data.
    return 0 if session_report.ok else 1


def _check_rig(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Smoke-test a rig's devices, through the backends a session builds."""
    try:
        ref = resolve_rig(args.rig, _experiment_root())
        rig = load_rig(ref.path)
        results = check_rig(rig, pulse=args.pulse)
    except ConfigError as e:
        # A config no session could run (a bad file, a test-only backend)
        # is not a rig fault and has no per-check line to report under.
        print(f"INVALID: {e}", file=sys.stderr)
        return 1
    for result in results:
        print(format_result(result))
    # The display is the one component this can never check: verifying it
    # means opening a window, which is a session. Said out loud rather
    # than omitted, so nobody reads a clean run as "everything works".
    print("     display: untested (needs a real session)")
    if args.record:
        from alhazen.session.checkout import build_record

        # The file the rig was read from, not the name typed: a record is held
        # up against next month's, and "lab" may by then be another file.
        record, summary = build_record(ref.path, results, pulse=args.pulse).write(args.record)
        # After the lines, not instead of them, and unconditionally: the
        # failing checkout is the one whose evidence is worth keeping, so
        # the record is never skipped on a FAIL.
        print(f"record:  {record}")
        print(f"summary: {summary}")
    # Unchanged by the record: a checkout passes on the checks alone.
    return 0 if all(r.ok for r in results) else 1


def add_mode_arguments(parser: argparse.ArgumentParser) -> None:
    """The options every mode-aware entry point takes.

    Shared with ``alhazen.cli.modes.run_experiment`` so an experiment's own
    run.py offers exactly the flags this command does. Two entry points
    that drifted apart would mean a flag that works one way at the rig and
    another way in a script.
    """
    parser.add_argument(
        "--mode",
        default="run",
        choices=[m.value for m in Mode],
        help="; ".join(f"{m.value}: {m.summary}" for m in Mode) + " (default: run)",
    )
    parser.add_argument("--rig", default=None, help=RIG_HELP)
    parser.add_argument("--params", default=None, help="path to the task's params YAML")
    parser.add_argument("--sub", default=None, help="subject id (prompted if omitted)")
    parser.add_argument("--ses", type=int, default=None, help="session number (prompted)")
    parser.add_argument(
        "--run", type=int, default=None, help="run number (default: the next free one)"
    )
    parser.add_argument(
        "--initials",
        default=None,
        help="the subject's initials, 1-5 letters: recorded, never put in a file name "
        "(run and test: prompted if omitted)",
    )
    parser.add_argument("--seed", type=int, default=None, help="session seed")
    parser.add_argument("--windowed", action="store_true", help="bordered window, for dev")
    # The two flags that override the machine rather than the experiment.
    # Any rig takes them; only one mode each honours them (alhazen.modes.flag_refusal).
    parser.add_argument(
        "--headless",
        action="store_true",
        help="simulate mode: no window and no browser — for CI and ssh",
    )
    parser.add_argument(
        "--mouse",
        action="store_true",
        help="test mode: the mouse cursor as gaze, even on a rig with an eye tracker",
    )
    live_monitor_group = parser.add_mutually_exclusive_group()
    live_monitor_group.add_argument(
        "--live-monitor", action="store_true", default=None, help="enable the live monitor"
    )
    live_monitor_group.add_argument(
        "--no-live-monitor",
        action="store_false",
        dest="live_monitor",
        help="disable the live monitor",
    )
    parser.set_defaults(live_monitor=None)
    parser.add_argument(
        "--no-live-monitor-browser",
        action="store_true",
        help="serve the live monitor without opening a browser window",
    )
    parser.add_argument("--curriculum", default=None, help="path to a curriculum YAML")
    # test / simulate
    parser.add_argument(
        "--trials-per-condition",
        type=int,
        default=1,
        help="test and simulate modes: repetitions of each condition (default: %(default)s)",
    )
    # demo
    parser.add_argument(
        "--screenshots",
        default=None,
        help="demo mode: directory for screenshots (default: the working directory)",
    )
    # movie
    parser.add_argument(
        "--out",
        default="movies",
        help="movie mode: directory for the files (default: %(default)s)",
    )
    parser.add_argument(
        "--clip",
        action="append",
        default=[],
        metavar="NAME",
        help="movie mode: record only the clip with this name; repeatable (default: all)",
    )
    parser.add_argument(
        "--sheet",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="movie mode: one tiled movie of every clip, each labelled, instead of a "
        "file per clip (default path: <out>/all-clips.mp4)",
    )
    parser.add_argument(
        "--columns",
        type=int,
        default=None,
        help="movie mode: how many sheet columns (default: near-square)",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=1.0,
        help="movie mode: shrink each frame by this factor — 0.5 quarters the file "
        "of a full-resolution rig (default: %(default)s)",
    )
    # measure
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="MEASUREMENT",
        help="measure mode: a measurement to skip; repeatable",
    )
    parser.add_argument(
        "--presses",
        type=int,
        default=None,
        help="measure mode: how many keypresses to time (default: the mode's own)",
    )


def _run_session(
    args: argparse.Namespace,
    task_class: Any = None,
    params_hook: Callable[[Any, argparse.Namespace], Any] | None = None,
) -> int:
    """Dispatch one of the six modes.

    All six arrive through the same command because they are six ways of
    starting the same experiment, and an experimenter who has to remember a
    different command per mode will use one of them and forget the rest. What
    they share is the task and the rig; what differs is what happens next.

    The params come from the task itself unless the invocation says
    otherwise (``_load_params``, ``_apply_params_hook``), so ``alhazen run
    --task`` and an experiment's ``run.py`` start the same session.
    ``params_hook`` is ``run.py``'s own (``run_experiment``'s
    ``params_hook=``), and replaces the task's; ``alhazen run`` passes None,
    which leaves the task's in charge.
    """
    # Armed before anything else, so a stop that arrives while the rig or the
    # task is still loading already ends the session through teardown rather
    # than on the spot. The workspace's Stop run is a console break on Windows,
    # which Python would otherwise let kill the process with no `finally` run
    # at all (alhazen.cli.console_break); elsewhere this does nothing.
    interrupt_on_console_break()

    from alhazen.cli.tasks import installed_tasks, load_task_class

    mode = Mode(args.mode)

    # Refused before anything loads: a flag the mode cannot honour is a
    # usage error, and finding that out after the rig opened a window is
    # the wrong moment.
    refusal = flag_refusal(mode, headless=args.headless, mouse=args.mouse)
    if refusal is not None:
        print(f"CANNOT RUN: {refusal}", file=sys.stderr)
        return 2
    # Initials typed on the command line are held to their rule in every
    # mode, before anything loads: a typo is a usage error however little
    # the mode does with them.
    refused = _normalize_initials_flag(args)
    if refused is not None:
        print(refused, file=sys.stderr)
        return 2

    if getattr(args, "list", False):
        tasks = installed_tasks()
        if not tasks:
            print("no tasks installed — install an experiment package first")
            return 0
        for name, point in sorted(tasks.items()):
            print(f"{name}\t{point.value}")
        return 0

    # Measure mode asks nothing of the experiment — it is about the machine —
    # so it is the one mode that runs without a task installed.
    needed = ["rig"] if mode is Mode.MEASURE or task_class is not None else ["task", "rig"]
    missing = [name for name in needed if getattr(args, name) is None]
    if missing:
        print(
            f"alhazen run --mode {mode.value} needs --{' and --'.join(missing)}"
            f" (or --list to see what is installed)",
            file=sys.stderr,
        )
        return 2

    # The task is loaded before the rig because a rig NAME is looked up in the
    # experiment the task belongs to (_experiment_root). Measure mode runs
    # without a task, and looks in the current folder.
    try:
        if task_class is None and mode is not Mode.MEASURE:
            task_class = load_task_class(args.task)
        root = _experiment_root(task_class)
        args.rig_ref = resolve_rig(args.rig, root)
        rig = load_rig(args.rig_ref.path)
    except ConfigError as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return 1
    # From here on args.rig names the file the rig was read from, as
    # args.params does below for the params: the snapshot's `sources["rig"]`
    # has always been a path, so a name typed on the command line is recorded
    # beside it (`rig_name`, `rig_source`; _trial_session) and not in its place.
    args.rig = str(args.rig_ref.path)

    # Run mode on a development rig (`real_data: false`: the shared laptop
    # every run.py starts on, the mac, the lab rehearsal) is refused as soon
    # as the rig is read — before the params load, before anyone is asked for
    # a subject, before the params hook (which may load and save a subject's
    # state) and before build_session makes a folder, registers the subject or
    # connects a device. A forgotten --rig used to record a session with no
    # tracker into the real data root. Exit 2, like the flag refusal above: a
    # usage error. docs/rigs.md §5.
    refusal = real_data_refusal(
        mode, rig, args.rig_ref, instead=lambda: _real_data_instead(args, root)
    )
    if refusal is not None:
        print(f"CANNOT RUN: {refusal}", file=sys.stderr)
        return 2

    if mode is Mode.MEASURE:
        return _measure_rig(args, rig, root)

    try:
        # From here on args.params names the file the params actually came
        # from — the task's own when nobody named one — so the snapshot's
        # `sources`, the line printed before trial one and the params hook
        # all see the file that ran, not the flag that was typed.
        params, args.params = _load_params(task_class, args.params)
    except ConfigError as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return 1

    # Who is in the chair and which session this is, settled BEFORE the
    # params hook runs, because deriving params from exactly those is what a
    # hook is for. It used to run first, so a subject typed at the prompt
    # reached it as None, and a search state carried across sessions was
    # filed under `sub-None` with nothing saying so. The params file is still
    # loaded and checked before anyone is asked anything.
    if mode.runs_trials:
        refused = _settle_subject_and_session(args, mode)
        if refused is not None:
            print(refused, file=sys.stderr)
            return 2

    try:
        params = _apply_params_hook(task_class, params, args, params_hook)
    except ConfigError as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return 1

    if mode is Mode.DEMO:
        return _demo_task(args, rig, task_class(params), params)
    if mode is Mode.MOVIE:
        return _movie_task(args, rig, task_class(params), params)
    return _trial_session(args, rig, task_class(params), params, mode)


def _real_data_instead(args: argparse.Namespace, root: Callable[[], Path]) -> list[str]:
    """What to type instead of a run-mode command refused on a development
    rig, in the command line's words: the lines ``real_data_refusal`` puts
    between its first sentence and the deliberate exception.

    The rigs offered are the ones here that collect real data
    (``config.rigs.collecting_rigs``), spelled as ``--rig`` takes them: a
    rig file that cannot be read is named as left out, and a folder whose
    rigs cannot be listed at all says why, so the suggestion never quietly
    shrinks. Called only for a refused session.
    """
    lines: list[str] = []
    # Set by run_experiment when run.py's default stood in for --rig: the
    # common case, a forgotten --rig, and the first thing to know about it.
    if getattr(args, "rig_defaulted", False):
        lines.append("No --rig was given, so run.py started on its default rig.")
    try:
        collecting, unreadable = collecting_rigs(root())
    except ConfigError as e:
        lines.append(f"To record a subject, name the machine it sits at with --rig ({e}).")
    else:
        if collecting:
            options = _either([f"--rig {ref.name}" for ref in collecting])
            line = (
                f"To record a subject, name the machine it sits at: {options} "
                "(the rigs here that collect real data)."
            )
        else:
            line = (
                "To record a subject, name the machine it sits at with --rig; no rig here "
                "collects real data (`alhazen rigs` lists them)."
            )
        if unreadable:
            names = ", ".join(str(ref.path) for ref in unreadable)
            line += f" Not considered, because they cannot be read: {names}."
        lines.append(line)
    lines.append(
        "To try the session on this machine, use --mode test or --mode simulate; "
        "their data goes to the rehearsal root."
    )
    return lines


def _either(options: list[str]) -> str:
    """``a``, ``a or b``, ``a, b or c``: alternatives as a sentence says them."""
    if len(options) <= 2:
        return " or ".join(options)
    return f"{', '.join(options[:-1])} or {options[-1]}"


def _load_params(task_class: Any, named: str | None) -> tuple[Any, str | None]:
    """The params a session starts from, and the file they came from (None:
    the params model's own defaults).

    The first of these that applies:

    1. the file the invocation names — ``--params``, or for an experiment's
       ``run.py`` its ``default_params=``, which ``run_experiment`` installs
       as ``--params``'s default, so either one takes precedence over the
       task's;
    2. the file the task declares (``Task.default_params``). A declared file
       that is not there raises ``ConfigError`` naming it;
    3. the params model's defaults — only for a task that declares no file,
       which is what every task got before it could.

    ``alhazen run`` used to go straight from 1 to 3, so with no ``--params``
    it ran the model's defaults in place of the experiment's own file
    without a word — for one experiment, 432 trials of a 576-trial design.
    """
    from alhazen.config.loader import load_model
    from alhazen.task.task import default_params_path

    path = named
    if path is None:
        declared = default_params_path(task_class)
        path = str(declared) if declared is not None else None
    if path is None:
        return task_class.params_model(), None
    return load_model(path, task_class.params_model), path


def _normalize_initials_flag(args: argparse.Namespace) -> str | None:
    """``args.initials`` as they are recorded (uppercase), or why they cannot
    be — the rule's own words, for the caller to print.

    None (not given) stays None. Read with a default because a namespace
    built by code other than `add_mode_arguments` may not carry the flag.
    """
    given = getattr(args, "initials", None)
    if given is None:
        args.initials = None
        return None
    try:
        args.initials = normalize_initials(given)
    except ValueError as e:
        return f"INVALID: {e}"
    return None


def _settle_subject_and_session(args: argparse.Namespace, mode: Mode) -> str | None:
    """Fill in ``args.sub``, ``args.ses`` and ``args.initials`` for a session
    that runs trials, or return why they cannot be — a usage error for the
    caller to print.

    Simulate mode names its own subject: nobody is there to ask, and it
    needs no initials. ``run`` and ``test`` name a real subject, so they need
    all three — the initials are what catch a mistyped subject number against
    the registry (data/participants.py). A missing flag is prompted for — an
    experimenter at a rig types this with an animal already waiting and
    should not have to remember the flag names — but only where a person can
    answer. With stdin not a terminal (nohup, CI, a batch script) input()
    blocks forever or dies in a raw EOFError, so the missing flags are
    refused instead.

    Idempotent: flags already settled are left as they are, and nothing is
    asked twice.
    """
    refused = _normalize_initials_flag(args)
    if refused is not None:
        return refused
    if mode is Mode.SIMULATE:
        if args.sub is None:
            args.sub = "sim"
        if args.ses is None:
            args.ses = 1
        return None
    missing = [
        flag
        for flag, value in (("--sub", args.sub), ("--ses", args.ses), ("--initials", args.initials))
        if value is None
    ]
    if missing and not (sys.stdin and sys.stdin.isatty()):
        return (
            f"{' and '.join(missing)} required: stdin is not a terminal, so "
            f"{mode.value} mode cannot prompt for them"
        )
    if args.sub is None:
        args.sub = input("subject id: ").strip()
    if args.ses is None:
        args.ses = _ask_session_number()
    if args.initials is None:
        args.initials = _ask_initials()
    return None


def _ask_initials() -> str:
    """Prompt until the answer is initials by the rule (config.models
    INITIALS_RULE): 1 to 5 letters, recorded uppercase.

    Asked again on a bad answer, with the same ``INVALID:`` line the flag
    gets, for the same reason as the session number: the person who mistyped
    is right there, with a subject waiting.
    """
    while True:
        answer = input("subject initials: ")
        try:
            return normalize_initials(answer)
        except ValueError as e:
            print(f"INVALID: {e}", file=sys.stderr)


def _ask_session_number() -> int:
    """Prompt until the answer is a session number: a whole number, 1 or more.

    A typo used to end in a raw ValueError traceback, with the rig config
    loaded and an animal waiting. Asked again instead — the person who
    mistyped is right there — and each bad answer is named with the same
    ``INVALID:`` the CLI prints for every other bad input. Sessions start at
    1 because ``SessionInfo`` refuses anything lower; refusing it here saves
    the experimenter from meeting that as a validation error later.
    """
    while True:
        answer = input("session number: ").strip()
        try:
            session = int(answer)
        except ValueError:
            session = 0  # not a number at all; reported by the check below
        if session >= 1:
            return session
        print(
            f"INVALID: session number must be a whole number, 1 or more; got {answer!r}",
            file=sys.stderr,
        )


def _apply_params_hook(
    task_class: Any,
    params: Any,
    args: argparse.Namespace,
    run_py_hook: Callable[[Any, argparse.Namespace], Any] | None,
) -> Any:
    """The params after the experiment's params hook, re-validated; unchanged
    when there is none.

    ``run.py``'s hook (``run_experiment(params_hook=...)``), when given,
    replaces the task's own (``Task.params_hook``) — they are not chained,
    so a ``run.py`` written before tasks could declare one keeps doing
    exactly what it did. A task that declares none is not touched at all.

    What the hook returns is re-validated through the task's own model, so a
    hook that returns something the task cannot express fails here — with
    the config still on screen — rather than mid-session. Same rule a
    training stage's overrides follow. An exception the hook raises is its
    own, and is not caught.
    """
    from alhazen.task.task import declared_params_hook

    if run_py_hook is not None:
        hook, whose = run_py_hook, f"the params hook run.py passes for {task_class.__name__}"
    else:
        declared = declared_params_hook(task_class)
        if declared is None:
            return params
        hook, whose = declared, f"{task_class.__name__}.params_hook()"
    try:
        return task_class.params_model.model_validate(hook(params, args))
    except ValidationError as e:
        raise ConfigError(
            f"{whose} returned something {task_class.params_model.__name__} cannot accept:\n{e}"
        ) from e


def _params_line(args: argparse.Namespace, task: Any, params: Any) -> str:
    """Where this session's params came from, in one line before trial one.

    The snapshot records it (``sources``), but a snapshot is read after the
    session; the experimenter reads this before it, which is when a wrong
    file — or no file at all — can still be stopped.
    """
    if args.params:
        return f"params: {args.params}"
    return (
        f"params: the defaults of {type(params).__name__} — no --params given, and "
        f"{type(task).__name__} declares no default_params()"
    )


def _seed_line(seed: int, *, drawn: bool) -> str:
    """The session's seed in one line before trial one, and how to run with
    it again.

    A session given no --seed draws a fresh one, which the snapshot and
    session.log record; both are read after the session, and the console is
    what the experimenter reads before it — and what the experiment workspace
    reads to show the drawn seed in its history (cli/workspace.py
    ``SEED_LINE``: the line starts ``seed: <digits>``).
    """
    if drawn:
        return f"seed: {seed} (drawn for this run; --seed {seed} repeats it)"
    return f"seed: {seed} (as given with --seed)"


def _measure_rig(args: argparse.Namespace, rig: Any, root: Callable[[], Path]) -> int:
    """Measure the rig and write the report beside its config."""
    from alhazen.modes.measure import run_measurements

    # Where the report will go, settled BEFORE anything is measured: for a
    # shared rig that is the experiment's configs/ folder (local_rig_file),
    # and finding there is none after the experimenter has sat through every
    # measurement would waste all of them.
    try:
        beside = local_rig_file(args.rig_ref, root)
    except ConfigError as e:
        print(f"CANNOT MEASURE: {e}", file=sys.stderr)
        return 1
    extra = {} if args.presses is None else {"n_presses": args.presses}
    try:
        report = run_measurements(
            rig, str(args.rig), windowed=args.windowed, skip=tuple(args.skip), **extra
        )
    except ValueError as e:
        # A --skip naming a measurement that does not exist. Rejected rather
        # than ignored: an experimenter who thinks they skipped the tracker
        # and did not will sit through it wondering why.
        print(f"CANNOT MEASURE: {e}", file=sys.stderr)
        return 2
    except DisplayError as e:
        # The rig's window could not open (PsychoPy missing from this
        # interpreter, a framebuffer the wrong size...): nothing was
        # measured, and the message says why and what to do.
        print(f"CANNOT MEASURE: {e}", file=sys.stderr)
        return 1
    print(report.render())
    written = report.save(_measurement_path(beside))
    print(f"written: {written}")
    # Non-zero when something disagrees with the config, so this is usable in
    # a pre-session script that must not carry on past a bad rig.
    return 0 if report.ok else 1


def _measurement_path(rig_path: str | Path) -> Path:
    """Where one measurement run is written: beside the rig config it measured.

    Beside the CONFIG rather than beside the data, because these describe the
    machine and not any subject — they stay relevant across the reinstall that
    eventually clears the data, and they belong with the file whose claims
    they are checking. For one of alhazen's shared rigs, whose file is inside
    alhazen's installation, ``rig_path`` is where the experiment's own file of
    that name would be (``alhazen.config.rigs.local_rig_file``).

    Stamped with the time, and never overwritten: a rig that has drifted is
    only visible by comparing two of these, so the second one must not replace
    the first.
    """
    rig = Path(rig_path)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return rig.parent / "measurements" / f"{rig.stem}_{stamp}.json"


def _demo_task(args: argparse.Namespace, rig: Any, task: Any, params: Any) -> int:
    """Show the task's stimulus, with no trials and no data."""
    from alhazen.modes.demo import run_demo

    try:
        return run_demo(
            task.demo_views,
            rig=rig,
            params=params,
            controls=task.demo_controls,
            seed=args.seed if args.seed is not None else 0,
            windowed=args.windowed,
            screenshot_dir=args.screenshots,
        )
    except NotImplementedError as e:
        # The task declares no views. Its own message names the method to
        # implement, which is more useful than anything this layer could say.
        print(f"CANNOT DEMO: {e}", file=sys.stderr)
        return 2
    except DisplayError as e:
        # The window could not open: PsychoPy missing from this interpreter,
        # a framebuffer that is not the size the rig says, a monitor
        # registration that disagrees with it. Each message names the cause
        # and the fix; a traceback in front of it only buried it.
        print(f"CANNOT DEMO: {e}", file=sys.stderr)
        return 1


def _movie_task(args: argparse.Namespace, rig: Any, task: Any, params: Any) -> int:
    """Write the task's clips to files, with no window and no data."""
    from alhazen.modes.movie import DEFAULT_SHEET_NAME, run_movie
    from alhazen.task.task import Task

    # A task that never implemented the hook is told apart by identity, not by
    # catching NotImplementedError around the whole recording: an experiment's
    # own NotImplementedError, raised from a frames generator halfway into a
    # file, is the experiment's bug and must surface with its traceback — not
    # be misreported as "declares no movie clips" and exit 2.
    if type(task).movie_clips is Task.movie_clips:
        try:
            task.movie_clips(None)
        except NotImplementedError as e:
            # The default hook's own message names the method to implement,
            # which is more useful than anything this layer could say.
            print(f"CANNOT RECORD: {e}", file=sys.stderr)
            return 2

    # `--sheet` with no path means "the default file under --out"; argparse
    # stores that as the empty-string const, resolved here where --out is known.
    sheet = None
    if args.sheet is not None:
        sheet = args.sheet if args.sheet else str(Path(args.out) / DEFAULT_SHEET_NAME)

    try:
        return run_movie(
            task.movie_clips,
            rig=rig,
            params=params,
            out=args.out,
            clip_names=tuple(args.clip),
            sheet=sheet,
            columns=args.columns,
            scale=args.scale,
            seed=args.seed if args.seed is not None else 0,
        )
    except ConfigError as e:
        print(f"CANNOT RECORD: {e}", file=sys.stderr)
        return 1


def _trial_session(args: argparse.Namespace, rig: Any, task: Any, params: Any, mode: Mode) -> int:
    """run, test and simulate: the three modes that put trials on screen."""
    from alhazen.modes.session import build_mode_session

    # Settled already by _run_session, before the params hook ran; asked
    # again here only so this function stays correct on its own. It is
    # idempotent, so nobody is prompted twice.
    refused = _settle_subject_and_session(args, mode)
    if refused is not None:
        print(refused, file=sys.stderr)
        return 2
    subject, session = args.sub, args.ses

    curriculum = None
    if args.curriculum:
        from alhazen.config.loader import load_model
        from alhazen.training import Curriculum

        # Inside the same handling as every other config file: a misspelled
        # --curriculum path, or a curriculum that does not validate, used to
        # print a traceback where the rig and the params print INVALID.
        try:
            curriculum = load_model(args.curriculum, Curriculum)
        except ConfigError as e:
            print(f"INVALID: {e}", file=sys.stderr)
            return 1

    try:
        built = build_mode_session(
            mode,
            rig=rig,
            task=task,
            subject=subject,
            session=session,
            run=args.run,
            seed=args.seed,
            n_per_condition=args.trials_per_condition,
            windowed=args.windowed,
            curriculum=curriculum,
            live_monitor=args.live_monitor,
            open_live_monitor=False if args.no_live_monitor_browser else None,
            headless=args.headless,
            mouse=args.mouse,
            # run.py's own override (run_experiment's `instructions=`), or
            # None — `alhazen run` never sets it — in which case the session
            # builder shows what the task declares (Task.instructions).
            instructions=getattr(args, "instructions", None),
            # `rig` is the file, as it has always been; which rig that file is
            # — its name, and whether it was the experiment's own or one of
            # alhazen's shared rigs — goes beside it (alhazen.config.rigs).
            sources={
                "rig": str(args.rig),
                "rig_name": args.rig_ref.name,
                "rig_source": args.rig_ref.source,
                "task": str(args.params or "<defaults>"),
            },
            # Recorded and checked against the registry, never in a path.
            initials=args.initials,
            # How this session was started, for session.json and the
            # snapshot: set by `main` and `run_experiment` from the argv they
            # parsed. A namespace built some other way has none, and the run
            # records null rather than a guess.
            command=getattr(args, "invocation", None),
        )
    except (ConfigError, DataError, DisplayError) as e:
        # DataError: what is already on disk refuses the session — a used run
        # folder, an experiment database from before 2.0's schema. Each names
        # the file and what to do; a traceback would bury that. DisplayError:
        # the session's window could not open — PsychoPy missing from this
        # interpreter, a framebuffer that is not the size the rig says — and
        # its message, too, names the cause and the fix.
        print(f"CANNOT RUN: {e}", file=sys.stderr)
        return 1

    # Printed BEFORE the session starts, and it lists every trial count the
    # mode turned down and the directory the data is going to. A mode that
    # quietly redesigns the experiment would put numbers in the snapshot that
    # are not the numbers that ran, and the snapshot is the record.
    print(built.describe())
    print(_params_line(args, task, params))
    # The task's own name, the one its run folder is named after, not
    # args.task: that is the name the command used — an entry point's for
    # `alhazen run`, a task table's key for run.py — which need not be it.
    print(f"running {task.name}: sub-{subject} ses-{session:03d} run-{built.run:02d}")
    # The seed, drawn by the build when none was given. The experiment
    # workspace (`alhazen dashboard`) reads this line from a launched run's
    # console to show the seed a session drew in its history; that contract
    # is tested on both sides.
    print(_seed_line(built.runner.seed, drawn=args.seed is None))
    # The live monitor's address, on the console like everything else the
    # experimenter needs before trial one. The runner also logs it, but only
    # into the run's session.log — and with --no-live-monitor-browser nothing
    # opens it, so this line is the only place a terminal user sees it. The
    # experiment workspace (`alhazen dashboard`) reads the same line from a
    # launched run's console to embed the page; that contract is tested.
    if built.runner.live_monitor_url is not None:
        print(f"live monitor: {built.runner.live_monitor_url}")
    built.runner.run()
    print(f"session complete — data under {built.data_root.resolve()}")
    return 0


def _calibrate(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """The monitor checks: geometry by tape measure, gamma by photometer."""
    from alhazen.cli import calibrate as calibration
    from alhazen.config.loader import load_rig

    if args.calibration is None:
        parser.parse_args(["calibrate", "--help"])
        return 2
    try:
        root = _experiment_root()
        ref = resolve_rig(args.rig, root)
        rig = load_rig(ref.path)
        if args.calibration == "ruler":
            # Draws the bar on a real display and blocks until a key is
            # pressed; on a simulated one there is nothing to hold a tape
            # against, so the report is the whole answer.
            print(calibration.draw_ruler(rig, args.dva, windowed=args.windowed))
            return 0
        # Beside the rig's own file — or, for a shared rig, beside where the
        # experiment's file of that name would be: never inside alhazen's
        # installation, which a reinstall would take the fit away with.
        # Settled before the CSV is read, so a refusal costs nothing.
        beside = local_rig_file(ref, root)
        levels, luminances = calibration.read_measurements(args.measurements)
        fit = calibration.fit_gamma(levels, luminances)
        written = calibration.write_gamma(beside, fit)
    except (ConfigError, DisplayError) as e:
        # DisplayError: the ruler's window could not open (PsychoPy missing
        # from this interpreter, ...), said with its cause and fix.
        print(f"CANNOT CALIBRATE: {e}", file=sys.stderr)
        return 1
    print(
        f"gamma {fit['gamma']:.3f} from {fit['n_measurements']} measurements "
        f"(residual {fit['residual_rms']:.4f})"
    )
    print(f"written: {written}")
    return 0


def _monitor(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Tell PsychoPy about this rig's monitor, and say whether it still agrees.

    PsychoPy looks monitors up by name in a database of its own (Monitor
    Center's), which is where a window finds a stored calibration and where
    PsychoPy's own tools write one. A rig config that has never been
    registered is invisible to all of that; a registration that no longer
    matches its config is worse, because the two then describe the same panel
    differently. Hence three commands: write one, list them, compare one.
    """
    from alhazen.config.gamma import gamma_path, load_gamma
    from alhazen.config.loader import load_rig
    from alhazen.display import monitors as registry

    if args.monitor_command is None:
        parser.parse_args(["monitor", "--help"])
        return 2

    try:
        if args.monitor_command == "list":
            names = registry.registered_names()
            print(f"psychopy monitors on this machine ({registry.monitor_folder()}):")
            if not names:
                print("  none registered yet")
            for name in names:
                print(f"  {registry.lookup(name).summary()}")
            return 0

        root = _experiment_root()
        ref = resolve_rig(args.rig, root)
        rig = load_rig(ref.path)
        if args.monitor_command == "register":
            # The gamma alhazen measured belongs on the monitor too: once it
            # is there, PsychoPy applies it to every window opened against
            # this monitor — including ones opened by other scripts on this
            # machine, which know nothing about alhazen's own gamma file.
            # It is kept where `calibrate gamma` wrote it (local_rig_file).
            try:
                beside: Path | None = local_rig_file(ref, root)
            except ConfigError:
                # A shared rig with no experiment configs/ folder here: no
                # gamma can have been kept for it, which is the same answer
                # as a rig never measured, and is said below with the reason.
                beside = None
            fit = load_gamma(beside) if beside is not None else None
            gamma = fit["gamma"] if fit else None
            notes = f"{registry.NOTES_PREFIX} {get_version()} from {ref.path.name}"
            written = registry.register(rig.monitor, gamma=gamma, notes=notes)
            print(f"registered {rig.monitor.name!r} with psychopy")
            print(f"  {registry.lookup(rig.monitor.name).summary()}")
            if gamma is None and beside is not None:
                print(
                    f"  no measured gamma yet — {gamma_path(beside)} does not exist "
                    f"(alhazen calibrate gamma --rig {args.rig} --measurements <csv>)"
                )
            elif gamma is None:
                print(
                    "  no measured gamma — for a shared rig one is kept in an experiment's "
                    "configs/ folder, and there is none here; run this from the "
                    "experiment's folder to register the gamma measured there"
                )
            print(f"  written: {written}")
            if rig.display.backend != "psychopy":
                # Registered anyway (a rig config is often written before the
                # panel is switched on), but said out loud: no session run
                # from THIS config will ever open a psychopy window.
                print(
                    f"  note: this rig's display backend is '{rig.display.backend}', "
                    f"so its own sessions will not use the registration"
                )
            return 0

        # show
        registration = registry.lookup(rig.monitor.name)
        print(
            f"rig config: {rig.monitor.width_px}x{rig.monitor.height_px}, "
            f"{rig.monitor.width_cm:g} cm wide, {rig.monitor.distance_cm:g} cm away "
            f"(monitor name: {rig.monitor.name!r})"
        )
        print(f"psychopy:   {registration.summary()}")
        if not registration.registered:
            print(f"\nregister it with: alhazen monitor register --rig {args.rig}")
            return 1
        if registration.calibrated:
            print(f"            calibrated {registration.calibrated}")
        if registration.path:
            print(f"            {registration.path}")
        drift = registry.differences(rig.monitor, registration)
        if drift:
            print("\nMISMATCH — a session on this rig would refuse to open:")
            for difference in drift:
                print(f"  {difference}")
            print(
                "\nOne of them has been edited since the monitor was registered. Fix the "
                f"rig config if the panel has not changed, then:\n"
                f"  alhazen monitor register --rig {args.rig}"
            )
            return 1
        print("\nOK — the rig config and psychopy agree")
        return 0
    except ConfigError as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return 1
    except DisplayError as e:
        print(f"CANNOT REGISTER: {e}", file=sys.stderr)
        return 1


def _sim_sorter(args: argparse.Namespace) -> int:
    """Stand in for the real-time spike sorter, so check-rig can be rehearsed.

    The sorted-spike sorter is the one thing a rig check depends on that no
    repository here contains: it is a separate program on a separate machine.
    That makes ``FAIL spikes`` the one line an experimenter meets for the
    first time on the morning it matters. This command removes that excuse —
    run it in one terminal, point a rig config's ``sorted_stream`` address at
    it, and run the real ``alhazen check-rig --pulse`` in another.

    ``--fault`` publishes a named non-conformance instead, so the failures can
    be rehearsed too. Both of check-rig's failure messages, and the difference
    between them, are things worth having seen before.
    """
    from alhazen.testing.sorter import SortedSpikePublisher, SorterSim, describe_fault

    cfg = SorterSim(
        address=args.address,
        n_units=args.units,
        sample_rate_hz=args.sample_rate_hz,
        firing_hz=args.firing_hz,
        heartbeat_period_ms=args.heartbeat_ms,
        seed=args.seed,
        fault=args.fault,
    )
    publisher = SortedSpikePublisher(cfg)
    try:
        address = publisher.bind()
    except AlhazenError as e:
        print(f"CANNOT PUBLISH: {e}", file=sys.stderr)
        return 1

    print(f"simulated sorter publishing on {address}")
    print(
        f"  {cfg.n_units} unit(s) {list(cfg.unit_ids)} @ {cfg.sample_rate_hz:g} Hz, "
        f"{cfg.firing_hz:g} Hz each, seed {cfg.seed}"
    )
    print(f"  fault={cfg.fault}: {describe_fault(cfg.fault)}")
    # Said every time, not buried in the docs: these spikes are Poisson noise
    # with no receptive fields and no stimulus coupling. Somebody will
    # eventually point an analysis at this stream, and they should be told
    # here rather than discover it in a result.
    print("  NOT science: Poisson noise, no receptive fields, no stimulus coupling —")
    print("  it simulates the transport, not the brain. Nothing is written to disk.")
    print("\n  point a rig at it:")
    print("    devices: {spikes: {backend: sorted_stream, address: " + address + "}}")
    print("    alhazen check-rig --rig <yaml> --pulse")
    print("\n  Ctrl-C to stop.")
    try:
        publisher.run(duration_s=args.seconds)
    except KeyboardInterrupt:
        # An expected way to end a process whose job is to run until told to
        # stop; a traceback here would read as a fault and is not one.
        print("\nstopped")
    finally:
        publisher.close()
    return 0


# Looked up by name at call time for `run` and `sim-sorter`, whose handlers
# take other arguments; the rest are the functions themselves.
_COMMANDS: dict[str, Handler] = {
    "dashboard": _dashboard,
    "validate": _validate,
    "rigs": _rigs,
    "preview": _preview,
    "new": _new,
    "run": lambda args, parser: _run_session(args),
    "calibrate": _calibrate,
    "monitor": _monitor,
    "report": _report,
    "check-rig": _check_rig,
    "sim-sorter": lambda args, parser: _sim_sorter(args),
}


if __name__ == "__main__":
    raise SystemExit(main())
