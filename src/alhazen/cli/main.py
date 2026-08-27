"""The alhazen CLI: the commands an experimenter actually types.

    alhazen new <name>        scaffold an experiment package
    alhazen run --task ...    run one session of an installed task
    alhazen validate --rig    is this config file well-formed?
    alhazen check-rig --rig   is this rig actually wired? (before the subject)
    alhazen calibrate ...     verify the monitor's geometry and gamma
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
from pathlib import Path

from alhazen.config.loader import load_rig
from alhazen.errors import AlhazenError, ConfigError
from alhazen.session.checks import check_rig, format_result
from alhazen.version import get_version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alhazen", description="Vision science experiments.")
    parser.add_argument("--version", action="version", version=f"alhazen {get_version()}")
    sub = parser.add_subparsers(dest="command")

    validate = sub.add_parser("validate", help="validate a config file")
    validate.add_argument("--rig", required=True, help="path to a rig YAML file")

    new = sub.add_parser("new", help="scaffold a new experiment package")
    new.add_argument("name", help="package name, e.g. saccade_bias")
    new.add_argument("--into", default=".", help="where to create it (default: here)")
    new.add_argument("--force", action="store_true", help="write into a non-empty directory")

    run = sub.add_parser("run", help="run one session of an installed task")
    run.add_argument("--task", default=None, help="the task's registered name")
    run.add_argument("--rig", default=None, help="path to a rig YAML file")
    run.add_argument("--params", default=None, help="path to the task's params YAML")
    run.add_argument("--sub", default=None, help="subject id (prompted if omitted)")
    run.add_argument("--ses", type=int, default=None, help="session number (prompted)")
    run.add_argument(
        "--run", type=int, default=None, help="run number (default: the next free one)"
    )
    run.add_argument("--seed", type=int, default=None, help="session seed")
    run.add_argument("--windowed", action="store_true", help="bordered window, for dev")
    dashboard_group = run.add_mutually_exclusive_group()
    dashboard_group.add_argument(
        "--dashboard", action="store_true", default=None, help="enable the live dashboard"
    )
    dashboard_group.add_argument(
        "--no-dashboard", action="store_false", dest="dashboard", help="disable the dashboard"
    )
    run.set_defaults(dashboard=None)
    run.add_argument(
        "--no-dashboard-browser",
        action="store_true",
        help="serve the dashboard without opening a browser window",
    )
    run.add_argument("--curriculum", default=None, help="path to a curriculum YAML")
    run.add_argument("--list", action="store_true", help="list installed tasks and exit")

    calibrate = sub.add_parser("calibrate", help="check a monitor's geometry and gamma")
    calibrate_sub = calibrate.add_subparsers(dest="calibration")
    ruler = calibrate_sub.add_parser(
        "ruler", help="what a known angular size should measure on the panel"
    )
    ruler.add_argument("--rig", required=True)
    ruler.add_argument("--dva", type=float, default=10.0, help="the bar's size in degrees")
    ruler.add_argument(
        "--windowed", action="store_true", help="bordered window rather than fullscreen"
    )
    gamma = calibrate_sub.add_parser("gamma", help="fit a gamma curve from photometer measurements")
    gamma.add_argument("--rig", required=True)
    gamma.add_argument(
        "--measurements", required=True, help="CSV with 'level' and 'luminance' columns"
    )

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
    check.add_argument("--rig", required=True, help="path to a rig YAML file")
    check.add_argument(
        "--pulse",
        action="store_true",
        help="also fire one real reward pulse and one pulse per mapped sync line",
    )

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "validate":
        try:
            rig = load_rig(args.rig)
        except ConfigError as e:
            print(f"INVALID: {e}", file=sys.stderr)
            return 1
        print(
            f"OK: {args.rig} — {rig.display.backend} display, "
            f"{rig.monitor.width_px}x{rig.monitor.height_px}@{rig.monitor.refresh_rate_hz:g}Hz, "
            f"data_root={rig.data_root}"
        )
        return 0
    if args.command == "new":
        from alhazen._scaffold import scaffold

        try:
            root = scaffold(args.name, Path(args.into), force=args.force)
        except ConfigError as e:
            print(f"CANNOT SCAFFOLD: {e}", file=sys.stderr)
            return 1
        print(f"created {root}")
        print("\nnext:")
        print(f"  cd {root}")
        print('  pip install -e ".[dev]"')
        print("  pytest")
        print("  python run.py --rig configs/rig-sim.yaml --sub s01 --ses 1")
        return 0

    if args.command == "run":
        return _run_session(args)

    if args.command == "calibrate":
        return _calibrate(args, parser)

    if args.command == "report":
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

    if args.command == "check-rig":
        try:
            rig = load_rig(args.rig)
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
        return 0 if all(r.ok for r in results) else 1
    return 0


def _run_session(args: argparse.Namespace) -> int:
    """Resolve an installed task, load its configs, and run one session."""
    from alhazen.cli.tasks import installed_tasks, load_task_class

    if args.list:
        tasks = installed_tasks()
        if not tasks:
            print("no tasks installed — install an experiment package first")
            return 0
        for name, point in sorted(tasks.items()):
            print(f"{name}\t{point.value}")
        return 0

    missing = [name for name in ("task", "rig") if getattr(args, name) is None]
    if missing:
        print(
            f"alhazen run needs --{' and --'.join(missing)} (or --list to see what is installed)",
            file=sys.stderr,
        )
        return 2

    from alhazen.config.loader import load_model, load_rig
    from alhazen.session.builder import build_session

    try:
        task_class = load_task_class(args.task)
        rig = load_rig(args.rig)
        params = (
            load_model(args.params, task_class.params_model)
            if args.params
            else task_class.params_model()
        )
    except ConfigError as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return 1

    # Prompted rather than required, because an experimenter at a rig types
    # this command with an animal already waiting and should not have to
    # remember the flag names.
    subject = args.sub if args.sub is not None else input("subject id: ").strip()
    session = args.ses if args.ses is not None else int(input("session number: ").strip())
    run_number = args.run if args.run is not None else _next_run(rig.data_root, subject, session)

    curriculum = None
    if args.curriculum:
        from alhazen.training import Curriculum

        curriculum = load_model(args.curriculum, Curriculum)

    runner = build_session(
        rig=rig,
        subject=subject,
        session=session,
        run=run_number,
        task=task_class(params),
        curriculum=curriculum,
        seed=args.seed,
        iti=getattr(params, "iti", None),
        windowed=args.windowed,
        dashboard=args.dashboard,
        open_dashboard=False if args.no_dashboard_browser else None,
        sources={"rig": str(args.rig), "task": str(args.params or "<defaults>")},
    )
    print(f"running {args.task}: sub-{subject} ses-{session:03d} run-{run_number:02d}")
    runner.run()
    print(f"session complete — data under {rig.data_root.resolve()}")
    return 0


def _next_run(data_root: Path, subject: str, session: int) -> int:
    """First unused run number for this subject and session.

    The run directories ARE the record, so they are what is counted — a
    counter file that disagreed with them is what would eventually overwrite
    a session's data.
    """
    session_dir = Path(data_root) / f"sub-{subject}" / f"ses-{session:03d}"
    if not session_dir.exists():
        return 1
    taken = []
    for path in session_dir.glob("run-*"):
        try:
            taken.append(int(path.name.split("_")[0].split("-")[1]))
        except (IndexError, ValueError):
            continue
    return max(taken, default=0) + 1


def _calibrate(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """The monitor checks: geometry by tape measure, gamma by photometer."""
    from alhazen.cli import calibrate as calibration
    from alhazen.config.loader import load_rig

    if args.calibration is None:
        parser.parse_args(["calibrate", "--help"])
        return 2
    try:
        rig = load_rig(args.rig)
        if args.calibration == "ruler":
            # Draws the bar on a real display and blocks until a key is
            # pressed; on a simulated one there is nothing to hold a tape
            # against, so the report is the whole answer.
            print(calibration.draw_ruler(rig, args.dva, windowed=args.windowed))
            return 0
        levels, luminances = calibration.read_measurements(args.measurements)
        fit = calibration.fit_gamma(levels, luminances)
        written = calibration.write_gamma(args.rig, fit)
    except ConfigError as e:
        print(f"CANNOT CALIBRATE: {e}", file=sys.stderr)
        return 1
    print(
        f"gamma {fit['gamma']:.3f} from {fit['n_measurements']} measurements "
        f"(residual {fit['residual_rms']:.4f})"
    )
    print(f"written: {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
