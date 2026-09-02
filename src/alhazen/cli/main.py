"""The alhazen CLI: the commands an experimenter actually types.

    alhazen new <name>        scaffold an experiment package
    alhazen run --task ...    run one session of an installed task
    alhazen validate --rig    is this config file well-formed?
    alhazen check-rig --rig   is this rig actually wired? (before the subject)
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

from alhazen.config.loader import load_rig
from alhazen.errors import AlhazenError, ConfigError
from alhazen.modes import Mode, flag_refusal
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
    run.add_argument("--list", action="store_true", help="list installed tasks and exit")
    add_mode_arguments(run)
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

    monitor = sub.add_parser("monitor", help="register this rig's monitor with PsychoPy")
    monitor_sub = monitor.add_subparsers(dest="monitor_command")
    monitor_register = monitor_sub.add_parser(
        "register", help="write the rig's monitor into PsychoPy's monitor database"
    )
    monitor_register.add_argument("--rig", required=True, help="path to a rig YAML file")
    monitor_show = monitor_sub.add_parser(
        "show", help="compare a rig's monitor with what PsychoPy has stored"
    )
    monitor_show.add_argument("--rig", required=True, help="path to a rig YAML file")
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
        print("  python run.py --mode simulate --rig configs/rig-lab.yaml --headless")
        print("\nthen, on a machine with a screen:")
        print("  python run.py --mode demo --rig configs/rig-mac.yaml")
        return 0

    if args.command == "run":
        return _run_session(args)

    if args.command == "calibrate":
        return _calibrate(args, parser)

    if args.command == "monitor":
        return _monitor(args, parser)

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
    parser.add_argument("--rig", default=None, help="path to a rig YAML file")
    parser.add_argument("--params", default=None, help="path to the task's params YAML")
    parser.add_argument("--sub", default=None, help="subject id (prompted if omitted)")
    parser.add_argument("--ses", type=int, default=None, help="session number (prompted)")
    parser.add_argument(
        "--run", type=int, default=None, help="run number (default: the next free one)"
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
    dashboard_group = parser.add_mutually_exclusive_group()
    dashboard_group.add_argument(
        "--dashboard", action="store_true", default=None, help="enable the live dashboard"
    )
    dashboard_group.add_argument(
        "--no-dashboard", action="store_false", dest="dashboard", help="disable the dashboard"
    )
    parser.set_defaults(dashboard=None)
    parser.add_argument(
        "--no-dashboard-browser",
        action="store_true",
        help="serve the dashboard without opening a browser window",
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

    ``params_hook`` is supplied only by an experiment's own ``run.py``
    (``alhazen.cli.modes.run_experiment``); ``alhazen run`` passes None and
    is unaffected. See that function for why the seam exists.
    """
    from alhazen.cli.tasks import installed_tasks, load_task_class

    mode = Mode(args.mode)

    # Refused before anything loads: a flag the mode cannot honour is a
    # usage error, and finding that out after the rig opened a window is
    # the wrong moment.
    refusal = flag_refusal(mode, headless=args.headless, mouse=args.mouse)
    if refusal is not None:
        print(f"CANNOT RUN: {refusal}", file=sys.stderr)
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

    from alhazen.config.loader import load_rig

    try:
        rig = load_rig(args.rig)
    except ConfigError as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return 1

    if mode is Mode.MEASURE:
        return _measure_rig(args, rig)

    from alhazen.config.loader import load_model

    try:
        if task_class is None:
            task_class = load_task_class(args.task)
        params = (
            load_model(args.params, task_class.params_model)
            if args.params
            else task_class.params_model()
        )
        if params_hook is not None:
            # Re-validated through the task's own model, so a hook that
            # returns something the task cannot express fails here — with
            # the config still on screen — rather than mid-session. Same
            # rule a training stage's overrides follow.
            try:
                params = task_class.params_model.model_validate(params_hook(params, args))
            except ValidationError as e:
                raise ConfigError(
                    f"the params hook for {task_class.__name__} returned something "
                    f"{task_class.params_model.__name__} cannot accept:\n{e}"
                ) from e
    except ConfigError as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return 1

    if mode is Mode.DEMO:
        return _demo_task(args, rig, task_class(params), params)
    if mode is Mode.MOVIE:
        return _movie_task(args, rig, task_class(params), params)
    return _trial_session(args, rig, task_class(params), params, mode)


def _measure_rig(args: argparse.Namespace, rig: Any) -> int:
    """Measure the rig and write the report beside its data."""
    from alhazen.modes.measure import run_measurements

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
    print(report.render())
    written = report.save(_measurement_path(args.rig))
    print(f"written: {written}")
    # Non-zero when something disagrees with the config, so this is usable in
    # a pre-session script that must not carry on past a bad rig.
    return 0 if report.ok else 1


def _measurement_path(rig_path: str) -> Path:
    """Where one measurement run is written: beside the rig config it measured.

    Beside the CONFIG rather than beside the data, because these describe the
    machine and not any subject — they stay relevant across the reinstall that
    eventually clears the data, and they belong with the file whose claims
    they are checking.

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

    # Prompted rather than required, because an experimenter at a rig types
    # this with an animal already waiting and should not have to remember the
    # flag names. A simulated session has nobody to prompt, so it names itself.
    if mode is Mode.SIMULATE:
        subject = args.sub if args.sub is not None else "sim"
        session = args.ses if args.ses is not None else 1
    else:
        # ...but only where a person can answer. With stdin not a terminal —
        # nohup, CI, a batch script — input() blocks forever or dies in a raw
        # EOFError after the rig config has already loaded, so the missing
        # flags are refused up front instead.
        missing = [f for f, v in (("--sub", args.sub), ("--ses", args.ses)) if v is None]
        if missing and not (sys.stdin and sys.stdin.isatty()):
            print(
                f"{' and '.join(missing)} required: stdin is not a terminal, so "
                f"{mode.value} mode cannot prompt for them",
                file=sys.stderr,
            )
            return 2
        subject = args.sub if args.sub is not None else input("subject id: ").strip()
        session = args.ses if args.ses is not None else int(input("session number: ").strip())

    curriculum = None
    if args.curriculum:
        from alhazen.config.loader import load_model
        from alhazen.training import Curriculum

        curriculum = load_model(args.curriculum, Curriculum)

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
            dashboard=args.dashboard,
            open_dashboard=False if args.no_dashboard_browser else None,
            headless=args.headless,
            mouse=args.mouse,
            instructions=getattr(args, "instructions", None),
            sources={"rig": str(args.rig), "task": str(args.params or "<defaults>")},
        )
    except ConfigError as e:
        print(f"CANNOT RUN: {e}", file=sys.stderr)
        return 1

    # Printed BEFORE the session starts, and it lists every trial count the
    # mode turned down and the directory the data is going to. A mode that
    # quietly redesigns the experiment would put numbers in the snapshot that
    # are not the numbers that ran, and the snapshot is the record.
    print(built.describe())
    # The task's own name, not args.task: an experiment's run.py has no
    # --task flag, because it already knows which experiment it is.
    print(f"running {task.name}: sub-{subject} ses-{session:03d} run-{built.run:02d}")
    built.runner.run()
    print(f"session complete — data under {built.data_root.resolve()}")
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
    from alhazen.errors import DisplayError

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

        rig = load_rig(args.rig)
        if args.monitor_command == "register":
            # The gamma alhazen measured belongs on the monitor too: once it
            # is there, PsychoPy applies it to every window opened against
            # this monitor — including ones opened by other scripts on this
            # machine, which know nothing about alhazen's own gamma file.
            fit = load_gamma(args.rig)
            gamma = fit["gamma"] if fit else None
            notes = f"{registry.NOTES_PREFIX} {get_version()} from {Path(args.rig).name}"
            written = registry.register(rig.monitor, gamma=gamma, notes=notes)
            print(f"registered {rig.monitor.name!r} with psychopy")
            print(f"  {registry.lookup(rig.monitor.name).summary()}")
            if gamma is None:
                print(
                    f"  no measured gamma yet — {gamma_path(args.rig).name} does not exist "
                    f"(alhazen calibrate gamma --rig {args.rig} --measurements <csv>)"
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


if __name__ == "__main__":
    raise SystemExit(main())
