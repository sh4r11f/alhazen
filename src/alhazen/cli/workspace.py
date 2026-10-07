"""Local experiment registry and supervised subprocesses for the launcher.

Discovery reads files, never imports experiment code into the web server.
Each launch uses the experiment's own entry point and interpreter, with a
private params snapshot and media directory. Session data still belongs to
its rig: the launcher must not redefine where real/rehearsal data goes.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import re
import shlex
import signal
import sqlite3
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Literal

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib

import yaml
from pydantic import BaseModel, ConfigDict, Field

from alhazen.cli.people import PeopleError, PeopleRegistry
from alhazen.cli.workspace_schema import TASK_NAME_KEY
from alhazen.config.calibration_images import NAME_PATTERN
from alhazen.config.experiment import experiment_stimuli, experiment_title
from alhazen.config.loader import validate_rig
from alhazen.config.models import RigConfig, normalize_initials, with_calibration_target
from alhazen.config.rigs import (
    SHARED_PREFIX,
    MergedRig,
    RigRef,
    collecting_rigs,
    file_rig_name,
    list_rigs,
    rig_extends,
    rig_mapping,
)
from alhazen.data.atomic import replace_atomically
from alhazen.data.participants import check_participant
from alhazen.errors import AlhazenError, ConfigError
from alhazen.modes import Mode, flag_refusal, real_data_refusal
from alhazen.modes.rehearsal import rehearsal_root

log = logging.getLogger(__name__)

MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}
ACTIVE = {"running", "stopping"}
# How long a stopped run gets to tear down before it is killed. Teardown is
# real work — the recorder writes trials.csv, the manifest hashes every file,
# an EyeLink transfers its EDF over the link — and on a rig it routinely
# outlasts the ten seconds this used to be. Killing early is exactly what
# loses that data, so the grace is generous; a run that has still not exited
# is then killed, and its record says so (`_force_kill`).
STOP_GRACE_S = 30


# How long a project's interpreter gets to answer a one-off question (import
# alhazen, print a schema). A cold conda env importing numpy and pydantic takes
# a few seconds; one that hangs this long is not going to answer.
CHILD_TIMEOUT_S = 20
# What `add()` asks the project's interpreter, as a single -c program: which
# alhazen it can import, which Python it is, which shared rigs that alhazen
# ships (name and absolute file), and which PsychoPy it has. JSON on the last
# line so the answer survives anything the import itself prints.
#
# The shared rigs are asked of the PROJECT's alhazen, not read from the
# workspace's own: the two may be different versions, and `--rig alhazen/lab`
# in a launched run means the lab the project's alhazen ships. An alhazen from
# before shared rigs has no module to list them, which is "none", not a
# failure; a module that is there but fails to import is a broken
# installation, and raises like any other import here.
#
# PsychoPy is looked for, never imported: importing it takes seconds and
# starts things (its preferences, audio and window libraries), and all the
# page needs is whether a launch that opens a window can find it. find_spec
# locates the package without running it; its version comes from the
# installed distribution's metadata (the distribution and the import are both
# named psychopy, so no look-alike can answer). null: not importable here.
# "unknown": a psychopy package with no installed metadata (a bare source
# tree on the path) — present, but of no known version. The probe uses
# importlib itself rather than alhazen.version because it runs under the
# project's alhazen, which may be older than any helper this one has.
INTERPRETER_PROBE = """\
import importlib.util, json, sys
from importlib.metadata import PackageNotFoundError, version
import alhazen
if importlib.util.find_spec("alhazen.config.rigs") is None:
    shared = []
else:
    from alhazen.config.rigs import shared_rig_files
    shared = [{"name": n, "path": str(p.resolve())} for n, p in shared_rig_files().items()]
psychopy = None
if importlib.util.find_spec("psychopy") is not None:
    try:
        psychopy = version("psychopy")
    except PackageNotFoundError:
        psychopy = "unknown"
calibration = None
if importlib.util.find_spec("alhazen.config.calibration_images") is not None:
    from alhazen.config.calibration_images import IMAGE_DIR, image_names
    from alhazen.config.models import CalibrationTargetConfig
    calibration = {"dir": str(IMAGE_DIR.resolve()), "images": list(image_names()),
                   "defaults": CalibrationTargetConfig().model_dump(mode="json")}
capabilities = None
if importlib.util.find_spec("alhazen.cli.capabilities") is not None:
    from alhazen.cli.capabilities import CAPABILITIES
    capabilities = sorted(CAPABILITIES)
measurements = None
if importlib.util.find_spec("alhazen.modes.measure_jobs") is not None:
    try:
        from alhazen.modes.measure_jobs import catalog, installed_jobs
        measurements = {"jobs": catalog(installed_jobs())}
    except Exception as error:
        measurements = {"error": f"{type(error).__name__}: {error}"}
print(json.dumps({"alhazen": alhazen.__version__, "python": sys.version, "shared_rigs": shared,
                  "psychopy": psychopy, "calibration_targets": calibration,
                  "measurements": measurements, "capabilities": capabilities}))
"""
# Said wherever a project registered before the probe asked for shared rigs
# is missing them: its record has no list, which is not the same as an empty
# one, and re-registering is what fills it in.
REGISTER_AGAIN = (
    "This project was registered before the workspace listed alhazen's shared rigs, so "
    "none are offered for it: open Project settings and save, to register it again"
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_record(path: Path, kind: type) -> Any:
    """Read one of the workspace's JSON files, naming it when it cannot be read.

    A corrupt projects.json or run.json used to surface as a raw
    JSONDecodeError ("Expecting value: line 1 column 1") with no file name in
    it — and a workspace has one registry plus a run.json per run to choose
    from. The person at the rig needs the path to fix or move.
    """
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError both are
        raise ValueError(
            f"Cannot read {path}: {exc}. Fix or move the file, then start the dashboard again."
        ) from exc
    if not isinstance(value, kind):
        raise ValueError(f"Cannot read {path}: expected a JSON {kind.__name__}")
    return value


def _child_env(project: dict[str, Any]) -> dict[str, str]:
    """The environment a project's interpreter runs in: its own paths, then ours.

    The project's ``src/`` and root come first so a src-layout checkout works
    without an editable install. The launcher's own checkout is deliberately
    NOT added. From an installed wheel that directory is the launcher's whole
    site-packages, and putting it first on the path of the *project's*
    interpreter — possibly another env or another Python version — shadowed
    the project's pinned alhazen and every package beside it (a numpy built
    for another interpreter fails to import). The project's interpreter must
    have alhazen installed itself; ``probe_interpreter`` checks that when the
    project is registered, which is the moment the message can still be acted on.
    """
    env = os.environ.copy()
    root = Path(project["path"])
    inherited = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        [str(root / "src"), str(root)] + ([inherited] if inherited else [])
    )
    return env


def probe_interpreter(python: str, project_path: str) -> dict[str, Any]:
    """Which alhazen and which Python does this interpreter have, which
    shared rigs does that alhazen ship, and which PsychoPy is installed
    beside it? Refuse if it has no alhazen; a missing PsychoPy is recorded,
    not refused (a project may only ever simulate or record movies).

    Run in the same environment a launch gets, so what is checked is what a
    run would import. Every failure is a ValueError that names the
    interpreter and says what to install, because the alternative is a
    registration that looks fine and a launch that dies on ``import alhazen``.
    """
    env = _child_env({"path": project_path})
    try:
        result = subprocess.run(
            [python, "-c", INTERPRETER_PROBE],
            cwd=project_path,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=CHILD_TIMEOUT_S,
        )
    except OSError as exc:
        raise ValueError(f"Cannot run the Python interpreter {python}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            f"The Python interpreter {python} did not answer within {CHILD_TIMEOUT_S} s; "
            "check that it starts from a terminal"
        ) from exc
    if result.returncode:
        raise ValueError(
            f"The Python interpreter {python} cannot import alhazen: "
            f"{result.stderr.strip()[-2000:]}\n"
            "Install alhazen-vision in that environment (pip install alhazen-vision), "
            "or choose the interpreter that has it in Project settings."
        )
    lines = result.stdout.strip().splitlines()
    unexpected = f"Unexpected reply from {python} while checking alhazen: {result.stdout[-500:]!r}"
    try:
        report = json.loads(lines[-1])
        shared = [{"name": str(r["name"]), "path": str(r["path"])} for r in report["shared_rigs"]]
        return {
            "alhazen_version": report["alhazen"],
            "python_version": report["python"],
            # Name and absolute file of each shared rig the project's alhazen
            # ships. Kept in the project record: the page lists them, a launch
            # checks against them, and the workspace merges a rig that
            # `extends` one over exactly that file (_shared_rigs).
            "shared_rigs": shared,
            # The interpreter's PsychoPy version, or None when it has none.
            # The page warns before a launch that would open a PsychoPy
            # window without it (workspace.js). A record without the key was
            # registered before the probe asked, which is "unknown" there, not
            # "not installed".
            "psychopy_version": None if report["psychopy"] is None else str(report["psychopy"]),
            # The calibration-target choice the project's alhazen offers: its
            # pictures' folder and names, and the setting's defaults; None for
            # an alhazen from before the choice (and for a record registered
            # before the probe asked), which the page then does not offer.
            "calibration_targets": _calibration_offer(report.get("calibration_targets")),
            # What the project's command line can be asked to record
            # (alhazen.cli.capabilities): [] for an alhazen from before the
            # list, which records no experimenter in its session folders.
            "capabilities": _capabilities(report.get("capabilities")),
            # The measurements its alhazen and installed packages offer for
            # Measure rig, in run order; None for an alhazen from before they
            # were selectable (the page then shows the fixed list it runs).
            # A provider that failed to load is recorded, not fatal: the
            # project can still run everything else.
            **_measurement_offer(report.get("measurements")),
        }
    except (IndexError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(unexpected) from exc


def _capabilities(reported: Any) -> list[str]:
    """The probe's capability names, checked to be plain names."""
    if reported is None:
        return []
    if not isinstance(reported, list) or not all(
        isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,40}", name) for name in reported
    ):
        raise ValueError(f"unexpected capabilities {reported!r}")
    return sorted(reported)


def records_experimenter(project: dict[str, Any]) -> bool | None:
    """Whether the project's alhazen records an experimenter in its session
    folders: True, False, or None when its registration predates the
    question (Project settings → save asks again)."""
    capabilities = project.get("capabilities")
    if capabilities is None:
        return None
    return "experimenter" in capabilities


def _calibration_offer(reported: Any) -> dict[str, Any] | None:
    """The probe's calibration-target answer, checked: a folder, picture
    names that can never be paths, and the defaults; None when the project's
    alhazen has no such choice. Anything else is an unexpected reply."""
    if reported is None:
        return None
    names = [str(name) for name in reported["images"]]
    if not all(NAME_PATTERN.fullmatch(name) for name in names):
        raise ValueError(f"unexpected calibration picture names {names!r}")
    return {
        "dir": str(reported["dir"]),
        "images": names,
        "defaults": dict(reported["defaults"]),
    }


# What a measurement key may be (alhazen.modes.measure_jobs.KEY_CHARS): it
# becomes a command-line value, so nothing else from a reply gets through.
MEASUREMENT_KEY = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+")
# A selection longer than this is not a selection anyone made by hand.
MAX_MEASUREMENTS = 64


def _measurement_offer(reported: Any) -> dict[str, Any]:
    """The probe's measurement catalog, checked: plain keys, groups and
    titles as text, prerequisites that name listed keys. ``measurements`` is
    None for an alhazen without selectable measurements; a provider that
    could not load is kept as ``measurements_error`` beside an empty list."""
    if reported is None:
        return {"measurements": None, "measurements_error": None}
    if "error" in reported:
        return {"measurements": [], "measurements_error": str(reported["error"])}
    jobs: list[dict[str, Any]] = []
    for entry in reported["jobs"]:
        key = str(entry["key"])
        if not MEASUREMENT_KEY.fullmatch(key):
            raise ValueError(f"unexpected measurement key {key!r}")
        jobs.append(
            {
                "key": key,
                "group": str(entry["group"]),
                "title": str(entry["title"]),
                "description": str(entry.get("description", "")),
                "order": int(entry["order"]),
                "needs": [str(n) for n in entry.get("needs", [])],
                "requires": [str(r) for r in entry.get("requires", [])],
                "provider": str(entry.get("provider", "alhazen")),
                "inputs": [str(i) for i in entry.get("inputs", [])],
                # "required" / "optional" / "none": whether the measurement is
                # of a person or animal in the chair (an older catalog: none).
                "subject": str(entry.get("subject", "none")),
            }
        )
    keys = {job["key"] for job in jobs}
    for job in jobs:
        if set(job["requires"]) - keys:
            raise ValueError(f"measurement {job['key']} requires one that is not listed")
    return {"measurements": jobs, "measurements_error": None}


def check_measurements(project: dict[str, Any], mode: str, selected: list[str] | None) -> None:
    """Refuse a measurement selection the project's run.py would refuse,
    before a run record exists — the server's check, whatever the page sent.

    Only Measure rig takes measurements. A project whose alhazen offers them
    must name at least one: an empty selection is not "everything". Each
    must be one its alhazen listed, once, with its prerequisites selected
    too. A project whose alhazen predates them takes none.
    """
    offered = project.get("measurements")
    if mode != Mode.MEASURE.value:
        if selected:
            raise ValueError("Only Measure rig takes a selection of measurements")
        return
    if offered is None:
        if selected:
            raise ValueError(
                f"{project['name']}'s alhazen runs Measure rig's fixed list and cannot choose "
                "measurements; update its alhazen, then open Project settings and save"
            )
        return
    if not selected:
        raise ValueError("Choose at least one measurement for Measure rig")
    if len(selected) > MAX_MEASUREMENTS:
        raise ValueError("Too many measurements selected")
    by_key = {job["key"]: job for job in offered}
    seen: set[str] = set()
    for key in selected:
        if key in seen:
            raise ValueError(f"The measurement {key} is selected twice")
        if key not in by_key:
            raise ValueError(f"{key} is not a measurement {project['name']}'s alhazen offers")
        seen.add(key)
    for key in selected:
        missing = [need for need in by_key[key]["requires"] if need not in seen]
        if missing:
            titles = ", ".join(by_key[need]["title"] for need in missing)
            raise ValueError(f"{by_key[key]['title']} needs {titles} in the same run")


def measurement_subject(project: dict[str, Any], request: Launch) -> str | None:
    """The subject ID a Measure rig launch passes on (--sub), or None.

    Only measurements of the person or animal in the chair take one (the
    tracker's calibration and accuracy, a vergence check): those are refused
    without it, since a gaze measurement nobody can attribute is not one.
    Every other measurement is of the machine and takes no subject, even
    when the form holds one."""
    if request.mode != Mode.MEASURE.value or not request.measurements:
        return None
    by_key = {job["key"]: job for job in project.get("measurements") or []}
    needs = [
        by_key[k]["title"]
        for k in request.measurements
        if by_key.get(k, {}).get("subject") == "required"
    ]
    wants = needs or [
        by_key[k]["title"]
        for k in request.measurements
        if by_key.get(k, {}).get("subject") == "optional"
    ]
    subject = request.subject.strip()
    if needs and not subject:
        raise ValueError(f"{', '.join(needs)} measure the subject in the chair: give a subject ID")
    return subject if wants and subject else None


def measurement_status(run_dir: Path) -> dict[str, Any] | None:
    """A Measure rig run's queue, as its child last wrote it; None when there
    is none (yet), or the file is not one this can read."""
    path = run_dir / "measure-status.json"
    try:
        if not path.is_file() or path.stat().st_size > 1_000_000:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def calibration_picture(project: dict[str, Any], name: str) -> Path:
    """The file of one of a project's calibration pictures, for the page's
    preview: only a name the project's alhazen listed when it was registered,
    from the folder it named — never a path the request spells out."""
    offer = project.get("calibration_targets")
    if not offer:
        raise FileNotFoundError(f"{project['name']}'s alhazen ships no calibration pictures")
    if not NAME_PATTERN.fullmatch(name) or name not in offer["images"]:
        raise FileNotFoundError(f"{name!r} is not one of the calibration pictures")
    folder = Path(offer["dir"])
    target = path_inside(folder, f"{name}.png")
    if not target.is_file():
        raise FileNotFoundError(
            f"calibration picture {name!r} is missing from {folder}; the project's alhazen "
            "has been reinstalled or moved — open Project settings and save, to register it again"
        )
    return target


class CalibrationChoice(BaseModel):
    """The calibration target a launch asks for, over the rig's own setting:
    run.py's --calibration-target, --calibration-images, --calibration-motion.
    Each None leaves the rig's choice."""

    model_config = ConfigDict(extra="forbid")
    appearance: Literal["standard", "images", "random_images"] | None = None
    images: list[str] | None = None
    motion: Literal["still", "pulse"] | None = None


def _calibration_arguments(choice: CalibrationChoice | None) -> list[str]:
    """The run.py flags for a launch's calibration-target choice."""
    if choice is None:
        return []
    args: list[str] = []
    if choice.appearance is not None:
        args += ["--calibration-target", choice.appearance]
    if choice.images is not None:
        args += ["--calibration-images", ",".join(choice.images)]
    if choice.motion is not None:
        args += ["--calibration-motion", choice.motion]
    return args


def _shared_rigs(project: dict[str, Any]) -> dict[str, Path] | None:
    """The shared rigs the project's alhazen ships, name to file, as its
    registration recorded them; None for a project registered before the
    record had them (REGISTER_AGAIN), which is not the same as having none."""
    recorded = project.get("shared_rigs")
    if recorded is None:
        return None
    return {entry["name"]: Path(entry["path"]) for entry in recorded}


def _read_schema(project: dict[str, Any], task: str | None = None) -> dict[str, Any]:
    """One read of a task's parameter schema, in the project's own interpreter:
    the task named, or run.py's only or default one."""
    try:
        result = subprocess.run(
            [
                project["python"],
                "-m",
                "alhazen.cli.workspace_schema",
                str(Path(project["path"]) / "run.py"),
                *([task] if task is not None else []),
            ],
            cwd=project["path"],
            env=_child_env(project),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=CHILD_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Reading the task's parameter choices timed out") from exc
    if result.returncode:
        raise ValueError(f"Cannot read task parameter choices: {result.stderr[-2000:]}")
    return json.loads(result.stdout)


def path_inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Path must stay inside its project or run directory")
    return path


def parse_parameters(text: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid parameter YAML: {exc}") from exc
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ValueError("Parameters must be a YAML mapping with string keys")
    # Reject YAML-specific objects (dates, sets, cycles) before handing to the browser.
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            "Parameters must contain finite numbers and JSON-compatible values"
        ) from exc
    return value


TASK_TABLE_SHAPE = (
    "run.py's tasks= must name a module-level dict literal, "
    'TASKS = {"name": (TaskClass, "configs/params.yaml"), ...}, passed as '
    "run_experiment(tasks=TASKS), for the workspace to list the tasks without importing "
    "the experiment"
)


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    return getattr(func, "attr", None)


def _module_assignment(tree: ast.Module, name: str) -> ast.expr | None:
    """The value bound to `name` at run.py's top level, or None."""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                return node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            return node.value
    return None


def _params_path(tree: ast.Module, node: ast.expr) -> str | None:
    """A task table entry's params file, relative to the project, when the
    file alone says what it is; otherwise None.

    Two forms are read. A string, as written: relative to the directory the
    session is started from, which the launcher sets to the project. And the
    form amodal-averaging's run.py uses for its own defaults, ``HERE /
    "configs" / "task.yaml"``: a chain of ``/`` whose left end is a
    module-level name bound from ``__file__`` (``HERE = Path(__file__).parent``)
    and whose other operands are strings — relative to run.py's folder, which
    is the project, and so found wherever the command is typed. A left end
    bound to anything else (a data root, a home directory) is not the
    project's folder and is not guessed at.
    """
    parts: list[str] = []
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        if not (isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)):
            return None
        parts.insert(0, node.right.value)
        node = node.left
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and not parts:
        text = node.value
    elif isinstance(node, ast.Name) and parts:
        bound = _module_assignment(tree, node.id)
        if bound is None or not any(
            isinstance(inner, ast.Name) and inner.id == "__file__" for inner in ast.walk(bound)
        ):
            return None
        text = "/".join(parts)
    else:
        return None
    # Posix form on every OS, like the rigs and presets it is matched against.
    # `Path.as_posix()` alone would not do it: on macOS and Linux a backslash
    # is a filename character, so a table written on Windows as
    # "configs\\x.yaml" would keep its backslash there and never match the
    # preset menu.
    return PureWindowsPath(text).as_posix()


def project_tasks(root: Path) -> dict[str, Any]:
    """The tasks a project's run.py declares, read from the file without running it.

    An experiment that ships several tasks writes them as a module-level dict
    literal and hands it to alhazen: ``TASKS = {"mt-tuning": (MTTuningTask,
    "configs/task-tuning.yaml"), ...}`` then ``run_experiment(tasks=TASKS,
    default_task=...)``. Reading the literal — the way rigs and scripts are
    found, by looking at files — is what lets the page list the tasks and each
    one's params file without starting the project's interpreter on every
    poll. A ``tasks=`` this cannot read is reported in ``error`` with what
    run.py must look like, never shown as "one task": a launch of such a
    project is refused with the same words (`Workspace._task_for`).

    Returns ``{"tasks": [{"name", "params"}, ...], "default": name, "error":
    None}``; a run.py declaring one task (``task_class=``) gives an empty
    list and no default. ``default`` is the task the Task menu opens on:
    run.py's ``default_task=``, else the first declared. ``default_task=`` is
    deprecated in run_experiment (alhazen 2.5; gone in 3.0, when every
    command names its task), but while a run.py still passes it, it is still
    read and checked here, because it is still that experiment's own answer
    to which task to offer first. It only preselects: every launch sends the
    task chosen in the menu as ``--task`` (`Workspace._task_for`), so no
    session started here relies on run.py's default.

    ``params`` is the task's params file relative to the project, in posix
    form: a string as written, or run.py's own-folder form ``HERE /
    "configs" / "x.yaml"`` (`_params_path`). It is None when the table gives
    none or gives it as an expression the file alone cannot evaluate.
    """
    empty: dict[str, Any] = {"tasks": [], "default": None, "error": None}
    run_py = root / "run.py"
    if not run_py.is_file():
        return empty
    try:
        tree = ast.parse(run_py.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError) as exc:
        return {**empty, "error": f"run.py cannot be read for its tasks: {exc}"}
    call = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _called_name(node) == "run_experiment"
        ),
        None,
    )
    keywords = {kw.arg: kw.value for kw in call.keywords if kw.arg} if call is not None else {}
    table_node = keywords.get("tasks")
    if table_node is None:
        return empty
    if not isinstance(table_node, ast.Name):
        return {**empty, "error": TASK_TABLE_SHAPE}
    table = _module_assignment(tree, table_node.id)
    if not isinstance(table, ast.Dict):
        return {**empty, "error": TASK_TABLE_SHAPE}
    tasks: list[dict[str, Any]] = []
    for key, value in zip(table.keys, table.values, strict=True):
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            return {**empty, "error": TASK_TABLE_SHAPE}
        params = None
        if isinstance(value, ast.Tuple | ast.List) and len(value.elts) >= 2:
            params = _params_path(tree, value.elts[1])
        tasks.append({"name": key.value, "params": params})
    if not tasks:
        return {**empty, "error": "run.py's tasks= table is empty; name at least one task"}
    names = [task["name"] for task in tasks]
    default_node = keywords.get("default_task")
    default = names[0]
    if default_node is not None:
        if isinstance(default_node, ast.Name):
            default_node = _module_assignment(tree, default_node.id)
        if not (isinstance(default_node, ast.Constant) and isinstance(default_node.value, str)):
            return {
                **empty,
                "error": "run.py's default_task= must be a string literal, or a module-level "
                "name bound to one",
            }
        default = default_node.value
        if default not in names:
            return {
                **empty,
                "error": f"run.py's default_task {default!r} is not one of its tasks: "
                f"{', '.join(names)}",
            }
    return {"tasks": tasks, "default": default, "error": None}


PARAMETERS_SHAPE = (
    "run.py's PARAMETERS must be a module-level dict literal naming each entry of the Task "
    'parameters menu: {"Main": ("task-name", HERE / "configs" / "task.yaml"), ...}, or '
    '{"Main": "configs/task.yaml"} for a run.py with one task; None for a path means the '
    "task's own defaults"
)


def _short_parameter_name(path: str) -> str:
    """A parameter file's name without configs/, its task- or params- prefix
    and its .yaml/.yml ending: configs/task-pilot.yaml is "pilot". A file in
    a subfolder keeps the subfolder (configs/presets/task-x.yaml is
    "presets/x")."""
    inside = path.removeprefix("configs/")
    folder, _, file = inside.rpartition("/")
    file = re.sub(r"\.ya?ml$", "", file)
    file = re.sub(r"^(task|params)-", "", file)
    return f"{folder}/{file}" if folder else file


def _derived_parameter_sets(configs: list[str], declared: dict[str, Any]) -> list[dict[str, Any]]:
    """The Task parameters menu for a run.py that declares no PARAMETERS.

    Nothing is guessed. With a task table, each task is offered on its own
    file (by the task's name; on no file when its entry names none, or names
    one the project lacks), and every other file is offered once per task as
    "<task> · <file>", because the file alone does not say which task it is
    for. Without one, every file is offered by its short name.
    """
    shorts = [_short_parameter_name(path) for path in configs]
    labels = {
        path: (path if shorts.count(short) > 1 else short)
        for path, short in zip(configs, shorts, strict=True)
    }
    tasks = declared["tasks"]
    if not tasks:
        return [{"label": labels[path], "task": None, "params": path} for path in configs]
    own = {task["params"] for task in tasks if task["params"] in configs}
    sets: list[dict[str, Any]] = []
    for task in tasks:
        entry: dict[str, Any] = {
            "label": task["name"],
            "task": task["name"],
            "params": task["params"] if task["params"] in configs else None,
        }
        if task["params"] and task["params"] not in configs:
            entry["missing"] = task["params"]
        sets.append(entry)
    for path in configs:
        if path in own:
            continue
        for task in tasks:
            sets.append(
                {"label": f"{task['name']} · {labels[path]}", "task": task["name"], "params": path}
            )
    return sets


def project_parameter_sets(
    root: Path, configs: list[str], declared: dict[str, Any]
) -> dict[str, Any]:
    """The Task parameters menu: every parameter set the page offers, each
    with the task it runs, read from run.py without running it.

    An experiment names its menu in run.py, beside TASKS, as a module-level
    dict literal ``PARAMETERS = {"Main": ("amodal-averaging", HERE / "configs"
    / "task.yaml"), "Main (less trials)": ("amodal-averaging", HERE /
    "configs" / "task-light.yaml"), ...}``: the label shown, the task it runs
    (one of TASKS) and its parameter file, in either of TASKS' path forms
    (`_params_path`), or None for the task's own defaults. A run.py with one
    task writes the path alone. Choosing an entry chooses its task, which is
    why the page has no separate Task menu: the file and the task cannot be
    paired wrongly.

    Returns ``{"sets": [{"label", "task", "params"}, ...], "default": label,
    "error": None}``. ``default`` is the entry the menu opens on: the one
    pairing the default task with that task's own file, else the first entry
    for the default task, else (no table) task.yaml's, else the first. When
    run.py has no PARAMETERS the sets are derived from TASKS and configs/
    (`_derived_parameter_sets`); when it has one that cannot be read the
    derived sets are offered and ``error`` says what to fix, so a typo there
    never stops a session from being launched.
    """
    sets = _derived_parameter_sets(configs, declared)
    error = None
    run_py = root / "run.py"
    if not declared["error"] and run_py.is_file():
        try:
            tree = ast.parse(run_py.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            tree = None  # project_tasks reports it already
        node = _module_assignment(tree, "PARAMETERS") if tree is not None else None
        if tree is not None and node is not None:
            try:
                sets = _declared_parameter_sets(tree, node, configs, declared)
            except ValueError as exc:
                error = str(exc)
    return {"sets": sets, "default": _default_parameter_set(sets, declared), "error": error}


def _declared_parameter_sets(
    tree: ast.Module, node: ast.expr, configs: list[str], declared: dict[str, Any]
) -> list[dict[str, Any]]:
    """run.py's PARAMETERS, checked: every label a non-empty string literal
    given once, every task one of TASKS, every path one of the project's
    parameter files. Raises ValueError naming the first entry that is not."""
    if not isinstance(node, ast.Dict) or not node.keys:
        raise ValueError(PARAMETERS_SHAPE)
    names = [task["name"] for task in declared["tasks"]]
    sets: list[dict[str, Any]] = []
    for key, value in zip(node.keys, node.values, strict=True):
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value.strip()):
            raise ValueError(PARAMETERS_SHAPE)
        label = key.value.strip()
        if any(entry["label"] == label for entry in sets):
            raise ValueError(f"run.py's PARAMETERS names {label!r} twice")
        task: str | None = None
        if names:
            if not (isinstance(value, ast.Tuple | ast.List) and len(value.elts) == 2):
                raise ValueError(
                    f"run.py's PARAMETERS[{label!r}] must be (task, params file); "
                    f"{PARAMETERS_SHAPE}"
                )
            first, value = value.elts
            if not (isinstance(first, ast.Constant) and first.value in names):
                raise ValueError(
                    f"run.py's PARAMETERS[{label!r}] names a task that is not one of TASKS "
                    f"({', '.join(names)})"
                )
            task = str(first.value)
        if isinstance(value, ast.Constant) and value.value is None:
            params = None
        else:
            params = _params_path(tree, value)
            if params is None:
                raise ValueError(
                    f"run.py's PARAMETERS[{label!r}] gives its file in a form that cannot be "
                    f"read without running run.py; {PARAMETERS_SHAPE}"
                )
            if params not in configs:
                raise ValueError(
                    f"run.py's PARAMETERS[{label!r}] names {params}, which is not one of the "
                    f"project's parameter files (configs/task*.yaml, configs/params*.yaml)"
                )
        sets.append({"label": label, "task": task, "params": params})
    return sets


def _default_parameter_set(sets: list[dict[str, Any]], declared: dict[str, Any]) -> str | None:
    """The label the Task parameters menu opens on (see project_parameter_sets)."""
    if not sets:
        return None
    if declared["tasks"]:
        default = declared["default"]
        own = next((t["params"] for t in declared["tasks"] if t["name"] == default), None)
        for entry in sets:
            if entry["task"] == default and entry["params"] == own:
                return str(entry["label"])
        for entry in sets:
            if entry["task"] == default:
                return str(entry["label"])
        return str(sets[0]["label"])
    for entry in sets:
        if entry["params"] and entry["params"].endswith("/task.yaml"):
            return str(entry["label"])
    return str(sets[0]["label"])


# The id of the Preview images action an experiment gets by declaring its
# stimuli: alhazen's own command rather than a module of the experiment's, so
# it can never be mistaken for one (theirs are "<package>.preview").
STIMULUS_PREVIEW = "alhazen.preview"


def script_actions(root: Path) -> list[dict[str, Any]]:
    """Recognise a runnable preview module by its literal argparse flags.

    A preview.py without a CLI (kde-vergence's viewer helper, for example)
    is not an image generator. Requiring --out and a __main__ guard avoids
    offering a button which runs successfully but produces nothing.

    Only preview.py is looked for. A movie.py with a command line used to
    get a **Movie script** button beside the Record movies mode, and the two
    wrote the same clips by different routes. Movies are the mode's alone
    now: it records the task's ``movie_clips``, with the form's own
    controls for scale, sheet and clip selection.

    An experiment that declares its stimuli (``[tool.alhazen] stimuli``, read
    from its pyproject.toml without importing anything) gets **Preview
    images** from alhazen instead: ``alhazen preview``, which draws every
    declared stimulus with no task and no parameter file, because neither
    changes what an experiment shows. That button replaces the one its own
    preview.py would have had. The declaration is the experiment's explicit
    statement of what to draw, and two buttons of one name would leave the
    reader guessing which draws what. A declaration that cannot be used still
    gets the button, carrying the reason as ``error``, which a launch raises:
    a missing button would read as "nothing declared" when the truth is
    "declared wrongly".
    """
    declaration = experiment_stimuli(root)
    declared = declaration.target is not None or declaration.error is not None
    actions: list[dict[str, Any]] = []
    if declared:
        actions.append(
            {
                "id": STIMULUS_PREVIEW,
                "label": "Preview images",
                "module": "alhazen",
                "params_flag": None,
                "rig_flag": True,
                "flags": ["--out", "--project", "--rig"],
                # The page hides the Task menu for it: whichever task is
                # chosen, the same images are drawn.
                "task_free": True,
                "error": declaration.error,
            }
        )
    # The experiment's own preview.py, unless the declaration above replaced it.
    sources = [] if declared else sorted((root / "src").glob("*/preview.py"))
    for source in sources:
        text = source.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            # No button for a script that cannot parse — but said out loud,
            # or the missing button reads as "not a generator" when the truth
            # is "broken". The others are still offered.
            log.warning(
                "%s is not offered as a script: it does not parse (line %s: %s)",
                source,
                exc.lineno,
                exc.msg,
            )
            continue
        flags = {
            arg.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
            for arg in node.args
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        }
        if "--out" not in flags or "__main__" not in text:
            continue
        module = f"{source.parent.name}.{source.stem}"
        params_flag = next((f for f in ("--params", "--task-config") if f in flags), None)
        actions.append(
            {
                "id": module,
                "label": "Preview images",
                "module": module,
                "params_flag": params_flag,
                "rig_flag": "--rig" in flags,
                "flags": sorted(flags),
                # The experiment's own script may read the task's parameter
                # file (--params / --task-config), so the Task menu, which
                # picks that file, stays.
                "task_free": False,
                "error": None,
            }
        )
    return actions


class Launch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: str
    mode: str
    rig: str
    parameters_yaml: str | None = None
    parameters: dict[str, Any] | None = None
    subject: str = ""
    # The subject's initials, beside the subject ID: required, like it, for
    # run and test (`_launch_initials`), and sent to run.py as --initials,
    # which records them and never puts them in a file name.
    initials: str = ""
    session: int = Field(default=1, ge=1)
    # The session seed to pass as --seed, or None to pass none, so the
    # session draws a fresh one and records it (session.log, the snapshot):
    # what the command line does when no seed is typed. It used to default to
    # 0 and always be sent, so every session started here had the same trial
    # order and jitters. A typed seed is still sent, to repeat a session.
    seed: int | None = Field(default=None, ge=0)
    trials: int = Field(default=1, ge=1)
    headless: bool = False
    mouse: bool = False
    windowed: bool = False
    scale: float = Field(default=0.5, gt=0, le=1)
    sheet: bool = False
    columns: int | None = Field(default=None, ge=1)
    clips: list[str] = Field(default_factory=list)
    # Free-form arguments for the entry point, split like a shell line and
    # appended after the launcher's own flags — for a mode as much as for a
    # standalone script. An experiment that ships several tasks reads its own
    # `--task <name>` from argv before run_experiment sees the rest, and
    # cannot be launched without it; `--curriculum <path>` and `--run <n>` are
    # the runner's flags the form has no control for. What the form does
    # control may not be contradicted here (`_extra_arguments`).
    extra_args: str = ""
    # Which of the experiment's tasks to run, for a run.py that declares
    # several (`run_experiment(tasks=...)`, read by `project_tasks`); None
    # means the task the Task menu opens on, which is then sent as --task
    # like any other choice (`Workspace._task_for`). A project declaring one
    # task takes no task here — the launcher names that one itself
    # (`Workspace._one_task`) — and neither does a standalone script.
    task: str | None = None
    # The Task parameters menu entry the launch was made from, by its label
    # (`project_parameter_sets`), for the history and run.json: the run's
    # task and folder keep the task's own name, so changing a label never
    # renames anything already recorded. None from a client that sends no
    # label, and for a launch that takes no parameters.
    parameter_set: str | None = None
    # The calibration target for this run, when the page's choice differs
    # from the rig's (CalibrationChoice); None runs the rig's own.
    calibration_target: CalibrationChoice | None = None
    # The measurements Measure rig runs, by key (check_measurements); None
    # for every other mode, and for a project whose alhazen predates them.
    measurements: list[str] | None = None
    # The subject and the experimenter by their people-registry record ids
    # (cli/people.py), as the Run page selects them. The server resolves the
    # subject's ID and initials from the record — `subject` and `initials`
    # above must then be empty or the same — and records a snapshot of both
    # records with the run (`Workspace._identity`). None: a launch that
    # names no record, as every client before the registry did (typed
    # subject and initials, no experimenter).
    subject_record: str | None = None
    experimenter: str | None = None


# Every flag `_mode_command` can emit, whichever mode. An extra argument
# naming one of these is refused: the form's controls are what the run record
# and its history show, and a `--seed 5` typed behind a seed field saying 0
# would make the record lie about the run. The runner's other flags — `--run`,
# `--curriculum`, `--live-monitor`, measure's `--skip` — are not the form's and
# pass through. `--task` is the Task menu's for a project whose run.py declares
# several tasks (`project_tasks`) and is reserved for those launches only; an
# experiment that reads its own `--task` from argv still gets it from here.
# TestCommandContract pins this set to what `_mode_command` actually emits, so
# a flag added there without joining it fails a test.
MODE_FLAGS = frozenset(
    {
        "--mode",
        "--rig",
        "--params",
        "--seed",
        "--no-live-monitor-browser",
        # The same flag as alhazen spelled it before 1.9: emitted only for a
        # project whose interpreter runs an alhazen that old (no_browser_flag).
        # alhazen 2.0 itself no longer accepts it, but the child is not this
        # alhazen, so the workspace keeps speaking its language.
        "--no-dashboard-browser",
        "--ses",
        "--sub",
        "--initials",
        "--trials-per-condition",
        "--headless",
        "--mouse",
        "--windowed",
        "--out",
        "--scale",
        "--sheet",
        "--columns",
        "--clip",
        "--screenshots",
        "--calibration-target",
        "--calibration-images",
        "--calibration-motion",
        # Sent for a registry experimenter, to a project whose alhazen
        # records one (records_experimenter).
        "--experimenter",
        "--experimenter-id",
        "--measure",
        "--measure-status",
    }
)
# The same for a standalone preview module: the flags `_script_command`
# passes it, which are what makes the run reproducible from its directory.
SCRIPT_FLAGS = frozenset({"--out", "--rig", "--params", "--task-config"})


def _extra_arguments(text: str, reserved: frozenset[str]) -> list[str]:
    """The free-form arguments as argv, with any flag the launcher owns refused by name.

    A reserved flag is caught in both spellings, `--seed 5` and `--seed=5`,
    and the refusal names it, because "not allowed" alone sends the person
    at the screen back to guess which of their tokens it meant. Quoting
    errors are named too: shlex's own "No closing quotation" does not say
    which field it is talking about.
    """
    try:
        extra = shlex.split(text)
    except ValueError as exc:
        raise ValueError(f"Cannot read the extra arguments ({exc}); check their quoting") from exc
    for token in extra:
        flag = token.split("=", 1)[0]
        if flag in reserved:
            raise ValueError(
                f"{flag} is set from the dashboard controls; change it there rather than in "
                "the extra arguments"
            )
    return extra


def _alhazen_release(alhazen_version: str | None) -> tuple[int, int]:
    """The (MAJOR, MINOR) of the alhazen a project's interpreter runs, as
    registration recorded it. A version this cannot read means the record is
    not one registration wrote, so it is refused rather than guessed: either
    guess would speak to the child in a language it may not know."""
    match = re.match(r"(\d+)\.(\d+)", alhazen_version or "")
    if match is None:
        raise ValueError(
            f"Cannot tell which alhazen this project runs ({alhazen_version!r}); "
            "remove the project and register it again"
        )
    return int(match.group(1)), int(match.group(2))


# The first alhazen whose run_experiment(task_class=...) takes --task, with
# the one task's name as its only choice. A project on it or later is sent
# --task for its one task, as a project with a Task menu always is; one on an
# older alhazen is not, because its run.py would refuse the flag.
ONE_TASK_FLAG_SINCE = (2, 5)


def no_browser_flag(alhazen_version: str | None) -> str:
    """The flag that keeps a launched session from opening its own browser
    tab (the page embeds the monitor instead), spelled the way the project's
    alhazen understands it. It is `--no-live-monitor-browser` since 1.9; an
    alhazen before 1.9 knows only `--no-dashboard-browser`. 1.9 and 1.10
    accept both, and a 2.0 child refuses the old spelling, so only a child
    older than 1.9 is given it. Registration records the version, so a launch
    never dies on argparse in the child's console.
    """
    recent = _alhazen_release(alhazen_version) >= (1, 9)
    return "--no-live-monitor-browser" if recent else "--no-dashboard-browser"


def _as_the_project_reads_it(merged: MergedRig, alhazen_version: str | None) -> MergedRig:
    """A project's rig settings as the project's own alhazen will read them,
    for the workspace's check before a launch.

    The check validates with the workspace's loader, which is alhazen 2.0's,
    and 2.0 refuses a rig's `dashboard:` section: 1.9 renamed it to
    `live_monitor:`. But the child reads the rig with the project's alhazen.
    Before 1.9 that alhazen knows only `dashboard:`, and 1.9 and 1.10 still
    read it, so for a project on any of them the section is moved to its
    new name before checking — otherwise this workspace would refuse to launch
    rigs their own sessions run correctly. Only the check sees the moved
    section; the child is handed the file as written, and validates it itself.
    A project on 2.0 or later gets the settings unchanged, and 2.0's refusal,
    which names the new key. So does a rig that has both sections: every
    alhazen refuses that one, each in its own words.
    """
    values = merged.values
    if "dashboard" not in values or "live_monitor" in values:
        return merged
    if _alhazen_release(alhazen_version) >= (2, 0):
        return merged
    moved = {key: value for key, value in values.items() if key != "dashboard"}
    moved["live_monitor"] = values["dashboard"]
    return merged._replace(values=moved)


def _launch_initials(request: Launch, mode: Mode) -> str | None:
    """The subject's initials a launch passes on, as run.py records them, or
    None when it passes none.

    Only for the modes that run trials, which are the ones the page shows the
    field for; any other mode ignores what the form holds. Required for run
    and test, the modes that name a real subject — the same rule the command
    line applies, refused here first so the refusal comes before anything is
    written — and held to the same rule, in the same words
    (config.models.normalize_initials), whenever given.
    """
    if not mode.runs_trials:
        return None
    text = request.initials.strip()
    if not text:
        if mode in {Mode.RUN, Mode.TEST}:
            raise ValueError("Subject initials are required for run and test modes")
        return None
    return normalize_initials(text)


def _run_identity(request: Launch) -> dict[str, Any]:
    """Who a launch is for, for its run record and the history ("sub-01 ·
    HD"): the subject, session and initials of a mode that runs trials, and
    nothing for any other launch. Called once the command has been built, so
    the initials have already passed their check."""
    if request.mode not in {m.value for m in Mode}:
        return {}
    mode = Mode(request.mode)
    if not mode.runs_trials:
        return {}
    return {
        "subject": request.subject.strip() or None,
        "session": request.session,
        "initials": _launch_initials(request, mode),
    }


# The line a session that runs trials prints before trial one, naming the seed
# it runs with (cli/main.py _seed_line): "seed: 2718281828 (drawn for this
# run; ...)". An empty seed field sends no --seed, so the session draws its
# own, and this line is how the workspace learns which, as it learns the live
# monitor's address from the same console. A child whose alhazen predates the
# line prints none, and the history then says "new".
SEED_LINE = re.compile(r"^seed: ([0-9]+)\b", re.MULTILINE)
# How much of the start of a console is searched for it. The line comes
# before trial one, after a few lines of summary, so it is in the first few
# kilobytes; the cap keeps a long console that never printed it from being
# read whole on every poll while its run is active.
SEED_SEARCH_BYTES = 65536
# The modes whose session draws a seed when it is given none, and prints it.
# Demo and movie take 0 when given none (the command line's own default) and
# measure takes none, so none of the three prints the line.
SEED_DRAWING_MODES = frozenset({Mode.RUN.value, Mode.TEST.value, Mode.SIMULATE.value})


def console_seed(console: Path) -> int | None:
    """The seed a launched session printed on its console (``SEED_LINE``), or
    None when it has printed none: not yet, or never (an alhazen from before
    the line, or a console that is not there)."""
    if not console.is_file():
        return None
    with console.open("rb") as stream:
        head = stream.read(SEED_SEARCH_BYTES).decode("utf-8", errors="replace")
    match = SEED_LINE.search(head)
    return int(match.group(1)) if match else None


def seed_argument(command: list[str]) -> int | None:
    """The seed a launch's command line passed with ``--seed N``, or None when
    it passed none.

    What a run record keeps as its ``seed`` at launch, read off the command
    rather than the request so the record says what the child was actually
    told. Also how a record written before the workspace kept the field is
    read: the form then always sent a seed, 0 unless one was typed, so the
    history can show every one of those sessions ran with the same seed.
    """
    # Each token beside the one after it; the last token has no value, which
    # is what strict=False lets the shorter list end on.
    for flag, value in zip(command, command[1:], strict=False):
        if flag == "--seed" and re.fullmatch(r"[0-9]+", value):
            return int(value)
    return None


def _mode_command(
    mode: Mode,
    request: Launch,
    root: Path,
    rig: str,
    run_dir: Path,
    no_browser: str = "--no-live-monitor-browser",
    task: str | None = None,
    reserved: frozenset[str] = MODE_FLAGS,
    measure_subject: str | None = None,
    experimenter: dict[str, Any] | None = None,
) -> list[str]:
    """run.py's arguments for one of the six modes: the launcher's flags, then the extras.

    Refusals come first, before anything is written to the run directory,
    in the words the person at the screen needs. TestCommandContract parses
    the launcher's part of the result with the runner's own parser, so a
    flag renamed there fails here rather than in a child's console; the
    extras ride behind it, untouched, for the experiment's run.py to read.
    """
    refusal = flag_refusal(
        mode,
        headless=request.headless,
        mouse=request.mouse,
        calibration=bool(_calibration_arguments(request.calibration_target)),
    )
    if refusal:
        raise ValueError(refusal)
    has_parameters = request.parameters is not None or request.parameters_yaml is not None
    if mode is Mode.MEASURE and has_parameters:
        raise ValueError("Measure rig does not use task parameters")
    extra = _extra_arguments(request.extra_args, reserved)
    if mode in {Mode.RUN, Mode.TEST} and not request.subject.strip():
        raise ValueError("A subject ID is required for run and test modes")
    initials = _launch_initials(request, mode)
    output = run_dir / "media"
    command = [str(root / "run.py"), "--mode", mode.value]
    # The task right after the mode, where run_experiment's own --task reads
    # it: the Task menu's, or a one-task project's own name; none only for a
    # project on an alhazen too old to take it, or one whose extra arguments
    # name it (`Workspace._one_task`).
    if task is not None:
        command += ["--task", task]
    command += ["--rig", rig]
    # No seed typed, no --seed: the session draws its own and records it,
    # and prints it on the console, where the history reads it (console_seed).
    if request.seed is not None:
        command += ["--seed", str(request.seed)]
    command += [no_browser]
    if has_parameters:
        command += ["--params", str(run_dir / "params.yaml")]
    if mode.runs_trials:
        command += ["--ses", str(request.session)]
        if request.subject.strip():
            command += ["--sub", request.subject.strip()]
        if initials is not None:
            command += ["--initials", initials]
        # Who runs it — the registry record's name and id — for a session
        # that records it in session.json; None for any other launch.
        if experimenter is not None:
            command += ["--experimenter", experimenter["name"]]
            command += ["--experimenter-id", experimenter["record_id"]]
    if mode in {Mode.TEST, Mode.SIMULATE}:
        command += ["--trials-per-condition", str(request.trials)]
    for flag in ("headless", "mouse", "windowed"):
        if getattr(request, flag):
            command += ["--" + flag]
    if mode is Mode.MOVIE:
        command += ["--out", str(output), "--scale", str(request.scale)]
        if request.sheet:
            command += ["--sheet", str(output / "all-clips.mp4")]
        if request.columns:
            command += ["--columns", str(request.columns)]
        for clip in request.clips:
            command += ["--clip", clip]
    if mode is Mode.DEMO:
        command += ["--screenshots", str(output)]
    if mode is Mode.MEASURE and request.measurements:
        for key in request.measurements:
            command += ["--measure", key]
        command += ["--measure-status", str(run_dir / "measure-status.json")]
        if measure_subject is not None:
            command += ["--sub", measure_subject]
    command += _calibration_arguments(request.calibration_target)
    # Last, after every flag of the launcher's own, where a typed command
    # puts them: run.py takes what is its own (a run.py that reads its own
    # `--task` before run_experiment does) and hands the rest on.
    return command + extra


def _script_command(request: Launch, root: Path, rig_path: Path, run_dir: Path) -> list[str]:
    """A standalone preview module's arguments, from the flags it declares.

    The rig, parameters and output directory are the launcher's to set (they
    are what makes the run reproducible from its directory), so the free-form
    arguments may not name them (SCRIPT_FLAGS).
    """
    action = next((s for s in script_actions(root) if s["id"] == request.mode), None)
    if action is None:
        raise ValueError("Unknown experiment mode or script")
    if action["id"] == STIMULUS_PREVIEW:
        return _stimulus_preview_command(action, request, root, rig_path, run_dir)
    command = ["-m", action["module"], "--out", str(run_dir / "media")]
    if action["rig_flag"]:
        command += ["--rig", str(rig_path)]
    if request.parameters is not None or request.parameters_yaml is not None:
        if not action["params_flag"]:
            raise ValueError("This script has no parameter-file option; use its own arguments")
        command += [action["params_flag"], str(run_dir / "params.yaml")]
    return command + _extra_arguments(request.extra_args, SCRIPT_FLAGS)


def _stimulus_preview_command(
    action: dict[str, Any], request: Launch, root: Path, rig_path: Path, run_dir: Path
) -> list[str]:
    """``alhazen preview`` for an experiment that declares its stimuli, in
    the project's own interpreter: every declared stimulus, at the chosen
    rig's scale, into the run's media folder.

    The project's own alhazen must have the command (the first release after
    2.8.0). An older one refuses it in the run's console, as ``invalid
    choice: 'preview'``. The workspace does not decide from the version it
    recorded at registration, which goes stale whenever the project
    upgrades and would refuse a development checkout that has the command.
    """
    if action["error"] is not None:
        raise ValueError(action["error"])
    if request.parameters is not None or request.parameters_yaml is not None:
        raise ValueError(
            "Preview images draws every stimulus the experiment declares, whatever the task "
            "and its parameters, so it takes no parameter file"
        )
    command = [
        "-m",
        "alhazen",
        "preview",
        "--project",
        str(root),
        "--rig",
        str(rig_path),
        "--out",
        str(run_dir / "media"),
    ]
    return command + _extra_arguments(request.extra_args, SCRIPT_FLAGS | {"--project"})


def _describe_rigs(root: Path, shared: dict[str, Path] | None) -> list[dict[str, Any]]:
    """The Rig menu's entries: the experiment's own rigs, then the shared rigs
    its alhazen ships, each ``{name, source, path, shadowed, extends}``.

    Found by the same ``list_rigs`` the command line uses, so the menu offers
    exactly the names ``--rig`` takes, in the same order. An experiment rig's
    ``path`` is relative to the project, in posix form (see ``describe``); a
    file that resolves outside the project — a symlink out of it — is not
    offered, since a launch refuses it (``path_inside``). A shared rig's
    ``path`` is the absolute file the probe recorded. ``extends`` is the
    shared rig an experiment rig builds on; a file that cannot be read for it
    carries ``error`` instead, which the page shows, and a launch of it fails
    with the same words.
    """
    entries: list[dict[str, Any]] = []
    for ref in list_rigs(root, shared=shared or {}):
        entry: dict[str, Any] = {
            "name": ref.name,
            "source": ref.source,
            "shadowed": ref.shadowed,
            "extends": None,
        }
        if ref.source == "alhazen":
            entry["path"] = str(ref.path)
        else:
            if not ref.path.resolve().is_relative_to(root.resolve()):
                continue
            entry["path"] = ref.path.relative_to(root).as_posix()
            try:
                entry["extends"] = rig_extends(ref.path)
            except ConfigError as exc:
                entry["error"] = str(exc)
        entries.append(entry)
    return entries


def _real_data_instead(root: Path, shared: dict[str, Path] | None) -> list[str]:
    """What to choose instead of a Run launch refused on a development rig,
    in the page's words: the lines ``real_data_refusal`` puts between its
    first sentence and the deliberate exception.

    The rigs offered are the Rig menu's that collect real data
    (``config.rigs.collecting_rigs``, over the project's shared rigs), by the
    qualified names the menu shows; a rig file that cannot be read is named
    as left out. Called only for a refused launch.
    """
    slug = experiment_title(root).slug
    collecting, unreadable = collecting_rigs(root, shared=shared or {})
    if collecting:
        names = ", ".join(ref.qualified(slug) for ref in collecting)
        line = (
            f"To record a subject, choose a rig that collects real data in the Rig menu: {names}."
        )
    else:
        line = (
            "To record a subject, choose its machine's rig; no rig in the Rig menu collects "
            "real data."
        )
    if unreadable:
        files = ", ".join(str(ref.path) for ref in unreadable)
        line += f" Not considered, because they cannot be read: {files}."
    return [line, "To try the session on this machine, choose Test session or Simulate."]


# launch.json's shape (Workspace.start); bumped only on an incompatible change.
LAUNCH_SCHEMA_VERSION = 1


def _experimenter_destination(
    project: dict[str, Any], request: Launch, identity: dict[str, Any]
) -> str | None:
    """Where a launch's experimenter is recorded: in the session folder's
    session.json when the session records one (a mode that runs trials, on
    an alhazen that takes --experimenter), else only in the workspace's own
    run records ("workspace"); None when no experimenter was chosen."""
    if identity.get("experimenter") is None:
        return None
    is_session = request.mode in {m.value for m in Mode} and Mode(request.mode).runs_trials
    if is_session and records_experimenter(project):
        return "session.json"
    return "workspace"


def _experiment_version(root: Path) -> dict[str, Any]:
    """``{version, version_error}`` from the experiment's pyproject.toml, read
    as text (never by importing the experiment)."""
    path = root / "pyproject.toml"
    if not path.is_file():
        return {"version": None, "version_error": "no pyproject.toml"}
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return {"version": None, "version_error": f"cannot read {path}: {exc}"}
    version = (document.get("project") or {}).get("version")
    if not isinstance(version, str) or not version:
        return {"version": None, "version_error": "pyproject.toml gives no [project] version"}
    return {"version": version, "version_error": None}


def _merged_rig_text(launched: str, merged: MergedRig) -> str:
    """A run folder's rig.yaml for a rig that extends a shared one: the merged
    settings as YAML, headed by where each half came from. What ran is then
    readable from the run folder alone, without the shared file of whichever
    alhazen was installed that day."""
    header = (
        f"# The rig this run was launched with ({launched}): the experiment's file,\n"
        f"# merged over alhazen's shared rig '{merged.extends}' ({merged.base}).\n"
        "# The experiment's file as written is rig-source.yaml, beside this one.\n"
    )
    return header + yaml.safe_dump(merged.values, sort_keys=False, allow_unicode=True)


# Fields of a project record that are the workspace's own, not the probe's:
# kept when the experiment is registered again (`Workspace.add`).
MANAGED_FIELDS = ("meta", "archived", "registered")


class Workspace:
    def __init__(self, directory: Path):
        self.directory = directory.expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "runs").mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.process: subprocess.Popen | None = None
        self.worker: threading.Thread | None = None
        self.active: str | None = None
        registry = self.directory / "projects.json"
        self.projects: list[dict[str, Any]] = (
            _load_record(registry, list) if registry.exists() else []
        )
        self.runs: dict[str, dict[str, Any]] = {}
        for path in sorted((self.directory / "runs").glob("*/run.json")):
            run = _load_record(path, dict)
            try:
                if "seed" not in run:
                    # A record from before the workspace kept the seed: its
                    # command says which one the form sent (seed_argument).
                    run["seed"] = seed_argument(run["command"])
                if run["status"] in ACTIVE:
                    run.update(status="interrupted", finished=now())
                    # Its session may have printed the seed it drew before
                    # the server went away; _finish never ran to keep it.
                    self._keep_drawn_seed(run)
                    replace_atomically(path, json.dumps(run, indent=2))
                self.runs[run["id"]] = run
            except KeyError as exc:
                raise ValueError(f"Cannot read {path}: the record has no {exc} field") from exc
        # Task schemas by project id, with the (run.py mtime, interpreter) they
        # were read under. Reading one imports the task in a child interpreter,
        # seconds each time, and the UI asks on every project switch; editing
        # run.py or choosing another interpreter can change the choices, so
        # either invalidates. The lock makes concurrent requests read once.
        self._schemas: dict[tuple[str, str | None], tuple[tuple[int, str], dict[str, Any]]] = {}
        self._schema_lock = threading.Lock()
        # The people registry (cli/people.py): the subjects and experimenters
        # the General page manages and the Run page selects. A registry this
        # alhazen cannot read leaves the rest of the workspace working and
        # says why wherever a record would be used (`people_error`); the
        # file is never moved or replaced to make it readable.
        self.people: PeopleRegistry | None = None
        self.people_error: str | None = None
        try:
            self.people = PeopleRegistry(self.directory)
        except (PeopleError, sqlite3.Error, OSError) as exc:
            self.people_error = f"The people registry cannot be opened: {exc}"
            log.error("%s", self.people_error)
        else:
            # CSV copies a crash or a full disk left behind the database.
            self.people.export_csv(raise_errors=False)

    def _save_projects(self) -> None:
        replace_atomically(self.directory / "projects.json", json.dumps(self.projects, indent=2))

    def project(self, key: str) -> dict[str, Any]:
        for project in self.projects:
            if project["id"] == key:
                return project
        raise ValueError("Experiment is not registered")

    def add(self, path: str, python: str = "") -> dict[str, Any]:
        root = Path(path).expanduser().resolve()
        if not (root / "run.py").is_file():
            raise ValueError(f"No run.py in {root}. Choose an Alhazen experiment folder.")
        if python:
            interpreter = Path(python).expanduser().absolute()
            if not interpreter.is_file():
                raise ValueError(f"Python interpreter does not exist: {interpreter}")
        else:
            candidates = [root / ".venv/bin/python", root / ".venv/Scripts/python.exe"]
            interpreter = next((p for p in candidates if p.is_file()), Path(sys.executable))
        project = {
            "id": hashlib.sha256(str(root).encode()).hexdigest()[:16],
            "name": root.name,
            "path": str(root),
            "python": str(interpreter),
            # Refuses here, with the reason, rather than at the first launch:
            # the wrong interpreter is a registration mistake, and this is the
            # moment the person who made it is looking at the screen.
            **probe_interpreter(str(interpreter), str(root)),
        }
        with self.lock:
            if self.active and self.runs[self.active]["project"] == project["id"]:
                raise ValueError(
                    "Wait for this experiment's run to finish before changing its interpreter"
                )
            # What the workspace keeps about the experiment beyond its
            # registration (its General page's notes, whether it is
            # archived) survives registering it again.
            previous = next((p for p in self.projects if p["id"] == project["id"]), None)
            for kept in MANAGED_FIELDS:
                if previous is not None and kept in previous:
                    project[kept] = previous[kept]
            if previous is None:
                project["registered"] = now()
            self.projects = [p for p in self.projects if p["id"] != project["id"]] + [project]
            self._save_projects()
        return self.describe(project["id"])

    def register(self, path: str, python: str = "") -> dict[str, Any]:
        """Register a new experiment from the Experiments page. A folder that
        is registered already is refused, naming it, rather than silently
        re-registered: its settings are where its interpreter changes."""
        root = Path(path).expanduser().resolve()
        key = hashlib.sha256(str(root).encode()).hexdigest()[:16]
        existing = next((p for p in self.projects if p["id"] == key), None)
        if existing is not None:
            raise ValueError(
                f"{root} is already registered as {existing['name']}; open it to change its "
                "interpreter"
            )
        return self.add(path, python)

    def update_meta(self, key: str, fields: Any) -> dict[str, Any]:
        """The experiment's own notes on the General page: a description and
        free notes, plain text. Written to projects.json (atomically); the
        experiment's folder is never touched."""
        if not isinstance(fields, dict) or set(fields) - {"description", "notes"}:
            raise ValueError("Only the description and notes can be changed here")
        clean: dict[str, str] = {}
        for name, value in fields.items():
            if not isinstance(value, str) or len(value) > 4000:
                raise ValueError(f"The {name} must be text of at most 4000 characters")
            if any(ord(ch) < 32 and ch not in "\n\t" for ch in value):
                raise ValueError(f"The {name} may not contain control characters")
            clean[name] = value.strip()
        with self.lock:
            project = self.project(key)
            project["meta"] = {**project.get("meta", {}), **clean}
            self._save_projects()
        return self.describe(key)

    def set_archived(self, key: str, archived: Any) -> dict[str, Any]:
        """Archive an experiment: kept registered, with its records and runs,
        but listed apart on the Experiments page and left out of the sidebar."""
        if not isinstance(archived, bool):
            raise ValueError("archived must be true or false")
        with self.lock:
            project = self.project(key)
            if archived and self.active and self.runs[self.active]["project"] == key:
                raise ValueError("Wait for this experiment's run to finish before archiving it")
            project["archived"] = archived
            self._save_projects()
        return self.describe(key)

    def remove(self, key: str) -> None:
        with self.lock:
            self.project(key)
            if self.active and self.runs[self.active]["project"] == key:
                raise ValueError("Stop this experiment's run before removing it")
            self.projects = [p for p in self.projects if p["id"] != key]
            self._save_projects()
        with self._schema_lock:
            # Every task's cached schema goes with the project.
            for cached in [entry for entry in self._schemas if entry[0] == key]:
                del self._schemas[cached]

    def describe(self, key: str) -> dict[str, Any]:
        project = self.project(key)
        root = Path(project["path"])
        params = []
        for path in sorted((root / "configs").rglob("*")):
            if path.suffix not in {".yaml", ".yml"} or not path.resolve().is_relative_to(root):
                continue
            # Posix form on every OS: these strings go into the registry, the
            # browser and run.json, and a record written on a Windows rig must
            # read the same elsewhere. Path accepts '/' back on Windows.
            if path.stem.startswith(("task", "params")):
                params.append(path.relative_to(root).as_posix())
        declared = project_tasks(root)
        menu = project_parameter_sets(root, params, declared)
        shared = _shared_rigs(project)
        # The experiment's names, read from its pyproject.toml on every
        # describe (like its rigs and tasks) so an edit shows on the next
        # poll. `name` stays the folder's name the registry recorded: run
        # records made before titles existed carry it, and keep working.
        naming = experiment_title(root)
        return {
            **project,
            # Its display name ([tool.alhazen] title), else its slug.
            "title": naming.title,
            # Its short name ([project] name, else the folder's): what rig
            # names are qualified with, amodal-averaging/lab.
            "slug": naming.slug,
            # Why a declared title (or the pyproject) could not be used; the
            # page shows it under the heading. None when nothing is wrong.
            "title_error": naming.error,
            "rigs": _describe_rigs(root, shared),
            # Why the Rig menu offers no shared rigs, when that is because the
            # registration predates them; None otherwise.
            "rigs_note": REGISTER_AGAIN if shared is None else None,
            "configs": params,
            "scripts": script_actions(root),
            "tasks": declared["tasks"],
            "default_task": declared["default"],
            "tasks_error": declared["error"],
            # The Task parameters menu, each entry with the task it runs
            # (project_parameter_sets): choosing one chooses both.
            "parameter_sets": menu["sets"],
            "default_parameter_set": menu["default"],
            "parameter_sets_error": menu["error"],
            "available": (root / "run.py").is_file(),
            # The experiment's version, the protocol its data is filed under
            # (pyproject.toml's [project] version), or None with the reason.
            **_experiment_version(root),
            # Whether its alhazen records the experimenter in session.json
            # (records_experimenter): True, False, or None when unknown.
            "records_experimenter": records_experimenter(project),
            "meta": project.get("meta", {}),
            "archived": bool(project.get("archived", False)),
        }

    def config(self, key: str, path: str) -> dict[str, Any]:
        root = Path(self.project(key)["path"])
        target = path_inside(root, path)
        if target.suffix not in {".yaml", ".yml"}:
            raise ValueError("Choose a YAML config")
        content = target.read_text(encoding="utf-8")
        return {"text": content, "values": parse_parameters(content)}

    def schema(self, key: str, task: str | None = None) -> dict[str, Any]:
        """The parameter schema of one of the project's tasks — `task`, or its
        only or default one — cached per task until run.py or the interpreter
        changes."""
        project = self.project(key)
        run_py = Path(project["path"]) / "run.py"
        try:
            stamp = (run_py.stat().st_mtime_ns, project["python"])
        except OSError as exc:
            raise ValueError(
                f"Cannot read task parameter choices: no run.py in {project['path']} ({exc})"
            ) from exc
        # Held across the read on purpose: a second request for the same
        # project arriving meanwhile waits for this answer instead of starting
        # its own interpreter. Serialising two different projects' reads is
        # the price, and it is smaller than two interpreters at once.
        with self._schema_lock:
            cached = self._schemas.get((key, task))
            if cached is not None and cached[0] == stamp:
                return cached[1]
            schema = _read_schema(project, task)
            self._schemas[(key, task)] = (stamp, schema)
            return schema

    def state(self) -> dict[str, Any]:
        with self.lock:
            return {
                "projects": [self.describe(p["id"]) for p in self.projects],
                "runs": [
                    self._listed(r)
                    for r in sorted(self.runs.values(), key=lambda r: r["started"], reverse=True)
                ],
                "active": self.active,
                "directory": str(self.directory),
            }

    def _keep_drawn_seed(self, run: dict[str, Any]) -> None:
        """Fill in ``run["seed"]`` with the seed its session drew for itself,
        once its console has said which (``console_seed``).

        Only for a launch that passed no seed to a mode that draws one; a
        seed the launch passed is already the record's, and a mode that draws
        none prints none. Left None when the console has not said it yet, or
        never will (an alhazen from before the console line): the page shows
        that as "new"."""
        if run.get("seed") is None and run.get("mode") in SEED_DRAWING_MODES:
            run["seed"] = console_seed(self.directory / "runs" / run["id"] / "console.log")

    def _listed(self, run: dict[str, Any]) -> dict[str, Any]:
        """A run as the page is sent it: a copy of its record, and for the
        active run, the seed its session drew as soon as its console says it.
        The record itself keeps it once the run has ended (``_finish``), so a
        finished run's console is never read again for it."""
        shown = dict(run)
        if run["id"] == self.active:
            self._keep_drawn_seed(shown)
        return shown

    def _command(
        self, request: Launch, run_dir: Path, experimenter: dict[str, Any] | None = None
    ) -> list[str]:
        """The child's argv: run.py in one of the six modes, or a standalone script.

        What both share — the project's interpreter, the rig checked for
        existence and validity — is settled here; the two argument lists have
        nothing else in common and are built apart.
        """
        project = self.project(request.project)
        root = Path(project["path"])
        ref, shared = self._launch_rig(project, request.rig)
        # Validated here, with the workspace's own loader, so a rig that
        # cannot run is refused before a run directory exists — merged over
        # the PROJECT's shared rig when it extends one, not the workspace's,
        # and read as the project's alhazen reads it (_as_the_project_reads_it).
        merged = rig_mapping(ref.path, shared=shared)
        checked = validate_rig(
            _as_the_project_reads_it(merged, project.get("alhazen_version")), ref.path
        )
        # Run mode on a development rig (`real_data: false` — the laptop the
        # Rig menu opens on, the mac, the lab rehearsal), refused here with
        # the words the session itself would use, before a run record or
        # anything else exists, and with the menu's names for the rigs that
        # do collect: the child would refuse it too, but only after the
        # workspace had filed a failed run for it (docs/rigs.md §5).
        if request.mode == Mode.RUN.value:
            refusal = real_data_refusal(
                Mode.RUN, checked, ref, instead=lambda: _real_data_instead(root, shared)
            )
            if refusal is not None:
                raise ValueError(refusal)
        self._check_registered_initials(root, request, checked)
        self._check_calibration_choice(project, request, checked)
        check_measurements(project, request.mode, request.measurements)
        task = self._task_for(project, request)
        base = [project["python"], "-u"]
        if request.mode in {m.value for m in Mode}:
            # With a task chosen from the menu, `--task` is the form's too and
            # may not be contradicted from the extra arguments.
            reserved = MODE_FLAGS | {"--task"} if task is not None else MODE_FLAGS
            # Every session names its task: the menu's for a project with
            # several, else the one task's own name where the project's
            # alhazen takes it (None: an older alhazen, or a --task the
            # person typed in the extras, which then names it instead).
            if task is None:
                task = self._one_task(project, request)
            # The child resolves a shared rig by name, the way `--rig
            # alhazen/lab` typed at its terminal would, which also records in
            # its snapshot that the rig was alhazen's (sources["rig_source"]).
            # An experiment rig goes as its file, as it always has.
            rig = ref.spec if ref.source == "alhazen" else str(ref.path)
            return base + _mode_command(
                Mode(request.mode),
                request,
                root,
                rig,
                run_dir,
                no_browser_flag(project.get("alhazen_version")),
                task=task,
                reserved=reserved,
                measure_subject=measurement_subject(project, request),
                experimenter=experimenter,
            )
        # A standalone script reads --rig with its own argparse and hands it
        # to load_rig, which takes a file: a shared rig goes as the file the
        # probe recorded.
        return base + _script_command(request, root, ref.path, run_dir)

    @staticmethod
    def _check_registered_initials(root: Path, request: Launch, rig: RigConfig) -> None:
        """Refuse a run or test launch whose subject the data folder's
        participants.tsv records with other initials — the session's own
        check (data.participants.check_participant), made here first so the
        refusal comes before a run record exists. The folder is the one the
        session will write to: the rig's data_root for run, its rehearsal
        sibling for test, relative to the experiment's folder (where a launch
        runs). Reads only."""
        if request.mode not in {Mode.RUN.value, Mode.TEST.value}:
            return
        subject, initials = request.subject.strip(), request.initials.strip()
        if not subject or not initials:
            return
        data_root = Path(rig.data_root).expanduser()
        if not data_root.is_absolute():
            data_root = root / data_root
        if request.mode != Mode.RUN.value:
            data_root = rehearsal_root(data_root)
        try:
            check_participant(data_root, subject, normalize_initials(initials))
        except AlhazenError as exc:
            raise ValueError(str(exc)) from exc

    def used_subjects(self, project_id: str) -> set[str]:
        """The people-registry subject records a launch of this experiment
        has named: their ID and recorded initials are then fixed."""
        with self.lock:
            return {
                run["identity"]["subject"]["record_id"]
                for run in self.runs.values()
                if run.get("project") == project_id
                and isinstance(run.get("identity"), dict)
                and isinstance(run["identity"].get("subject"), dict)
                and run["identity"]["subject"].get("record_id")
            }

    def _identity(self, project: dict[str, Any], request: Launch) -> tuple[Launch, dict[str, Any]]:
        """Who a launch is for and who runs it: the request with the
        subject's ID and initials taken from its registry record, and the
        snapshot the run keeps (launch.json, run.json ``identity``).

        A request that names no record is a typed one (every client before
        the registry, and the command line's equivalent): its snapshot
        records the typed subject with no record, and no experimenter —
        "not recorded", never a guess. Refusals say what to choose.
        """
        is_mode = request.mode in {m.value for m in Mode}
        mode = Mode(request.mode) if is_mode else None
        names_people = mode is not None and (mode.runs_trials or mode is Mode.MEASURE)
        snapshot: dict[str, Any] = {"subject": None, "experimenter": None, "source": "typed"}
        if request.subject_record is None and request.experimenter is None:
            if names_people and request.subject.strip():
                snapshot["subject"] = {
                    "record_id": None,
                    "id": request.subject.strip(),
                    "initials": request.initials.strip().upper() or None,
                }
            return request, snapshot
        if not names_people:
            raise ValueError(
                "Only a session (run, test, simulate) or Measure rig takes a subject or an "
                "experimenter; clear them for this launch"
            )
        assert mode is not None
        if self.people is None:
            raise ValueError(self.people_error or "The people registry is not available")
        named_subject = mode in {Mode.RUN, Mode.TEST}
        if named_subject and request.subject_record is not None and request.experimenter is None:
            raise ValueError("Choose the experimenter who runs this session")
        taken = self.people.launch_identity(
            project["id"],
            request.subject_record,
            request.experimenter,
            need_initials=named_subject,
        )
        snapshot.update(taken, source="registry")
        update: dict[str, Any] = {}
        subject = taken["subject"]
        if subject is not None:
            typed, typed_initials = request.subject.strip(), request.initials.strip().upper()
            if typed and typed != subject["id"]:
                raise ValueError(
                    f"The subject ID typed ({typed}) is not the selected subject's "
                    f"(sub-{subject['id']}); choose one or the other"
                )
            if typed_initials and subject["initials"] and typed_initials != subject["initials"]:
                raise ValueError(
                    f"The initials typed ({typed_initials}) are not sub-{subject['id']}'s"
                )
            update = {"subject": subject["id"], "initials": subject["initials"] or ""}
        elif request.subject.strip():
            # A typed subject beside a registry experimenter: kept as typed.
            snapshot["subject"] = {
                "record_id": None,
                "id": request.subject.strip(),
                "initials": request.initials.strip().upper() or None,
            }
        elif named_subject:
            raise ValueError("Choose the subject for this session")
        return request.model_copy(update=update), snapshot

    def _check_calibration_choice(
        self, project: dict[str, Any], request: Launch, rig: RigConfig
    ) -> None:
        """Refuse a calibration-target choice the launch cannot honour, before
        anything is written: a project whose alhazen has no such choice (its
        run.py would refuse the flags), a picture its alhazen does not ship,
        or a choice the rig itself refuses (no tracker that draws a target, a
        bad combination) — checked with the same rule run.py applies
        (with_calibration_target). The mode's own refusal (run and test only,
        not with Mouse as gaze) is _mode_command's."""
        choice = request.calibration_target
        if not _calibration_arguments(choice):
            return
        assert choice is not None
        offer = project.get("calibration_targets")
        if not offer:
            raise ValueError(
                f"{project['name']}'s alhazen ({project.get('alhazen_version')}) has no "
                "calibration-target choice, or was registered before the workspace asked: "
                "update its alhazen, then open Project settings and save, to register it again"
            )
        unknown = [name for name in choice.images or [] if name not in offer["images"]]
        if unknown:
            raise ValueError(
                f"{', '.join(unknown)}: not among {project['name']}'s calibration pictures"
            )
        if request.mode not in {m.value for m in Mode}:
            raise ValueError(
                "A calibration-target choice is for run and test, which calibrate the rig's eye "
                "tracker; a script never calibrates"
            )
        if request.mode not in {Mode.RUN.value, Mode.TEST.value}:
            return  # refused by _mode_command, in the mode's own words
        try:
            with_calibration_target(
                rig, appearance=choice.appearance, images=choice.images, motion=choice.motion
            )
        except ConfigError as exc:
            raise ValueError(str(exc)) from exc

    def _launch_rig(
        self, project: dict[str, Any], spec: str
    ) -> tuple[RigRef, dict[str, Path] | None]:
        """The rig a launch (or the rig summary) names, and the project's
        shared rigs to merge an ``extends`` over.

        ``spec`` is what the Rig menu sends: ``alhazen/<name>`` for a shared
        rig, the project-relative path for one of the experiment's own —
        which must stay inside the project (``path_inside``), as every path a
        request carries must. Each refusal says what to do.
        """
        shared = _shared_rigs(project)
        if spec.startswith(SHARED_PREFIX):
            if shared is None:
                raise ValueError(REGISTER_AGAIN)
            name = spec.removeprefix(SHARED_PREFIX)
            if name not in shared:
                raise ValueError(
                    f"{project['name']}'s alhazen ({project.get('alhazen_version')}) ships no "
                    f"shared rig {name!r}; its shared rigs are {', '.join(shared) or 'none'}"
                )
            if not shared[name].is_file():
                raise ValueError(
                    f"The shared rig {name!r} was recorded at {shared[name]}, which no longer "
                    "exists — the project's alhazen has been reinstalled or moved. Open Project "
                    "settings and save, to register it again"
                )
            return RigRef(name, shared[name], "alhazen"), shared
        path = path_inside(Path(project["path"]), spec)
        if not spec or not path.is_file():
            raise ValueError("Choose an existing rig YAML file")
        if shared is None and rig_extends(path) is not None:
            # Without the recorded list there is no shared file to merge it
            # over, and "extends a rig that is not shared" would blame the file.
            raise ValueError(REGISTER_AGAIN)
        return RigRef(file_rig_name(path), path, "experiment"), shared

    def rig(self, key: str, spec: str) -> dict[str, Any]:
        """The rig ``spec`` names, as the page summarises it under the menu:
        its name, whose it is, the shared rig it extends, and its settings —
        merged when it extends one, so the summary (live monitor on or off,
        the monitor's size) describes the rig that would run, not the half of
        it one file holds. Merged by the workspace's own loader, over the file
        the project's probe recorded; not validated (a launch does that)."""
        ref, shared = self._launch_rig(self.project(key), spec)
        merged = rig_mapping(ref.path, shared=shared)
        return {
            "name": ref.name,
            "source": ref.source,
            "extends": merged.extends,
            "values": merged.values,
        }

    def _one_task(self, project: dict[str, Any], request: Launch) -> str | None:
        """The name to send as ``--task`` in a mode launch of a project whose
        run.py declares one task (``run_experiment(task_class=...)``), so that
        its sessions name their task as every other session does — a command
        without one is deprecated since alhazen 2.5 and refused in 3.0.

        None, and nothing sent, when the launch is not of that kind, when the
        project's alhazen predates the flag (`ONE_TASK_FLAG_SINCE`; its run.py
        would refuse it), or when the extra arguments already carry a
        ``--task`` — an experiment that reads its own, as attention-clamp's
        run.py does, is named there, and a second ``--task`` would contradict
        it. The name comes from the task's parameter schema, read in the
        project's interpreter (`schema`, which caches it and which the page
        has usually asked for already): run.py holds only the class, so the
        file alone cannot say it. A name that cannot be read refuses the
        launch, saying how to name the task by hand, rather than starting a
        session that names none.

        Asked from `_command`, after the rig is checked, so a launch with two
        problems is refused for the one it always was. The price: when the
        schema is not cached (run.py changed since the page last asked), the
        read happens under `start`'s lock, and the page's polls wait the
        seconds it takes.
        """
        if request.mode not in {m.value for m in Mode}:
            return None
        declared = project_tasks(Path(project["path"]))
        # A table, or one that cannot be read: the Task menu's business
        # (`_task_for`), whose refusal of an unreadable table must be the one
        # the person sees, not a schema error from here.
        if declared["tasks"] or declared["error"]:
            return None
        if _alhazen_release(project.get("alhazen_version")) < ONE_TASK_FLAG_SINCE:
            return None
        typed = _extra_arguments(request.extra_args, frozenset())
        if any(token.split("=", 1)[0] == "--task" for token in typed):
            return None
        how = "type --task <its name> in the extra arguments to name it yourself"
        try:
            schema = self.schema(project["id"])
        except ValueError as exc:
            raise ValueError(
                f"Every session names its task, and {project['name']}'s one task cannot be "
                f"named for --task because its parameter schema cannot be read ({exc}); {how}"
            ) from exc
        name = schema.get(TASK_NAME_KEY)
        if not isinstance(name, str):
            raise ValueError(
                f"Every session names its task, and {project['name']}'s alhazen did not say "
                f"its one task's name, though registration recorded alhazen "
                f"{project.get('alhazen_version')}; open Project settings and save, to "
                f"register it again, or {how}"
            )
        return name

    def _task_for(self, project: dict[str, Any], request: Launch) -> str | None:
        """The Task menu's task for this launch: the one asked for, else the
        one the menu opens on (run.py's ``default_task=``, else the first
        declared), for an experiment that declares several — sent as
        ``--task`` either way, so the session never falls back on run.py's
        own default. None for one that declares one (named by `_one_task`
        instead) and for a standalone script (which has no task). Each
        refusal says what to change, before anything is written."""
        declared = project_tasks(Path(project["path"]))
        if declared["error"]:
            raise ValueError(declared["error"])
        names = [task["name"] for task in declared["tasks"]]
        is_mode = request.mode in {m.value for m in Mode}
        if not names or not is_mode:
            if request.task:
                what = (
                    "A standalone script"
                    if not is_mode
                    else f"{project['name']}'s run.py, which declares one task,"
                )
                raise ValueError(
                    f"{what} takes no task; clear the task, or declare several with "
                    "run_experiment(tasks=...)"
                )
            return None
        task = request.task or declared["default"]
        if task not in names:
            raise ValueError(
                f"{project['name']} declares no task {task!r}; choose one of {', '.join(names)}"
            )
        return task

    def _check_parameter_set(
        self, project: dict[str, Any], request: Launch, task: str | None
    ) -> None:
        """Refuse a launch whose Task parameters label is not one of the
        project's, or whose task is not the one that entry runs, so the label
        the history shows cannot disagree with the session that ran. Checked
        before anything is written."""
        if request.parameter_set is None:
            return
        sets = self.describe(project["id"])["parameter_sets"]
        entry = next((e for e in sets if e["label"] == request.parameter_set), None)
        if entry is None:
            raise ValueError(
                f"{project['name']} has no Task parameters entry {request.parameter_set!r}; "
                f"choose one of {', '.join(e['label'] for e in sets)}"
            )
        is_mode = request.mode in {m.value for m in Mode}
        if entry["task"] is not None and is_mode and entry["task"] != task:
            raise ValueError(
                f"Task parameters {request.parameter_set!r} run the task {entry['task']}, "
                f"but the launch names {task}; choose the entry again"
            )

    def start(self, request: Launch) -> dict[str, Any]:
        with self.lock:
            if self.active:
                raise ValueError(
                    "Another run is active. Finish or stop it before starting a new one."
                )
            key = uuid.uuid4().hex
            run_dir = self.directory / "runs" / key
            # Who it is for and who runs it, from the people registry when the
            # page selected records: resolved first, so every check after
            # this one sees the subject the record names.
            request, identity = self._identity(self.project(request.project), request)
            recorded_in = _experimenter_destination(
                self.project(request.project), request, identity
            )
            command = self._command(
                request,
                run_dir,
                experimenter=identity["experimenter"] if recorded_in == "session.json" else None,
            )
            if request.parameters is not None and request.parameters_yaml is not None:
                raise ValueError("Supply either parameter fields or YAML, not both")
            values = (
                parse_parameters(request.parameters_yaml)
                if request.parameters_yaml is not None
                else request.parameters
            )
            text = yaml.safe_dump(values, sort_keys=False) if values is not None else None
            if text is not None:
                parse_parameters(text)
            project = self.project(request.project)
            task = self._task_for(project, request)
            self._check_parameter_set(project, request, task)
            ref, shared = self._launch_rig(project, request.rig)
            merged = rig_mapping(ref.path, shared=shared)
            (run_dir / "media").mkdir(parents=True)
            started = now()
            # The launch as it was decided, written once and never again
            # (mode "x"): run.json changes as the run goes on, this does not,
            # so a record renamed on the General page later never rewrites
            # who a past session was for or who ran it.
            launch = {
                "schema_version": LAUNCH_SCHEMA_VERSION,
                "run": key,
                "project": {"id": project["id"], "name": project["name"], "path": project["path"]},
                "mode": request.mode,
                "task": task,
                "parameter_set": request.parameter_set,
                "rig": Path(request.rig).as_posix(),
                "rig_name": ref.name,
                "started": started,
                "command": command,
                "identity": identity,
                "experimenter_recorded_in": recorded_in,
            }
            with (run_dir / "launch.json").open("x", encoding="utf-8") as stream:
                json.dump(launch, stream, indent=2, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            if text is not None:
                (run_dir / "params.yaml").write_text(text, encoding="utf-8")
            # A copy of the rig, for the record; the run itself reads the rig
            # where it is, so relative paths in it keep their usual
            # experiment-working-directory meaning. A whole file is copied as
            # it is, comments and all. A file that extends a shared rig is
            # only half of what ran, so rig.yaml is the merged rig instead,
            # and the file as written is kept beside it as rig-source.yaml.
            if merged.base is None:
                (run_dir / "rig.yaml").write_bytes(ref.path.read_bytes())
            else:
                (run_dir / "rig.yaml").write_text(
                    _merged_rig_text(request.rig, merged), encoding="utf-8"
                )
                (run_dir / "rig-source.yaml").write_bytes(ref.path.read_bytes())
            run = {
                "id": key,
                "project": project["id"],
                "name": project["name"],
                "mode": request.mode,
                # The task run, for the history and anyone reading run.json;
                # None for a project with one task and for a script.
                "task": task,
                # The Task parameters entry it was launched from, by its
                # label; None when the client named none.
                "parameter_set": request.parameter_set,
                # What was launched: a project-relative path, stored as posix
                # whatever the client typed so run.json reads the same on
                # every OS, or `alhazen/<name>` for a shared rig. The rig
                # itself was resolved above.
                "rig": Path(request.rig).as_posix(),
                # Which rig that is, for the history (which shows the name,
                # not the file) and for anyone reading run.json.
                "rig_name": ref.name,
                "rig_source": ref.source,
                "started": started,
                "finished": None,
                "status": "running",
                "returncode": None,
                "command": command,
                "cwd": project["path"],
                "directory": str(run_dir),
                # subject, session and initials, for a mode that runs trials.
                **_run_identity(request),
                # The launch's snapshot of the subject and experimenter records
                # (launch.json holds the same, never rewritten), and where the
                # session itself records the experimenter: "session.json", or
                # "workspace" when its alhazen predates the flag (only here
                # and in launch.json), None when no experimenter was chosen.
                "identity": identity,
                "experimenter_recorded_in": recorded_in,
                # The seed the launch passed, None when it passed none: the
                # session then draws its own, and _keep_drawn_seed fills it in
                # from the console. Read off the command, which is what the
                # child was actually told.
                "seed": seed_argument(command),
                # The measurements a Measure rig run was asked for, in run
                # order as the page listed them; None for any other launch.
                "measurements": list(request.measurements) if request.measurements else None,
            }
            self.runs[key] = run
            self._save_run(run)
            env = _child_env(project)
            env["PYTHONUNBUFFERED"] = "1"
            try:
                with (run_dir / "console.log").open("wb") as log:
                    self.process = subprocess.Popen(
                        command,
                        cwd=project["path"],
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=os.name != "nt",
                        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                        if os.name == "nt"
                        else 0,
                    )
            except OSError as exc:
                run.update(status="failed", finished=now(), error=str(exc))
                self._save_run(run)
                return dict(run)
            self.active = key
            self.worker = threading.Thread(
                target=self._finish, args=(key, self.process), daemon=True
            )
            self.worker.start()
            return dict(run)

    def _save_run(self, run: dict[str, Any]) -> None:
        replace_atomically(
            self.directory / "runs" / run["id"] / "run.json", json.dumps(run, indent=2)
        )

    def _finish(self, key: str, process: subprocess.Popen) -> None:
        code = process.wait()
        with self.lock:
            run = self.runs[key]
            if run["status"] == "stopping":
                # "cancelled" is the clean ending: the interrupt reached the run
                # and its teardown finished on its own. A run that had to be
                # killed has no such guarantee — no trials file, no manifest —
                # and its history must not read as if it had.
                status = "killed" if run.get("stopped") == "forced" else "cancelled"
            else:
                status = "completed" if code == 0 else "failed"
            run.update(status=status, returncode=code, finished=now())
            # The seed the session drew, kept in the record now that its
            # console is complete, so the history shows it without reading
            # the console again.
            self._keep_drawn_seed(run)
            self._save_run(run)
            self.active = None
            self.process = None

    def stop(self, key: str) -> None:
        with self.lock:
            if key != self.active or self.process is None:
                raise ValueError("This run is no longer active")
            process = self.process
            self.runs[key]["status"] = "stopping"
            self._save_run(self.runs[key])
            self._signal(process, force=False)
        threading.Thread(target=self._kill_later, args=(key, process), daemon=True).start()

    @staticmethod
    def _signal(process: subprocess.Popen, *, force: bool) -> None:
        if process.poll() is not None:
            return
        try:
            # sys.platform rather than os.name: mypy narrows on it, so the
            # branch for the other platform is not checked against this one's
            # stubs (Windows has no os.killpg or SIGKILL; POSIX no CTRL_BREAK).
            if sys.platform == "win32":
                if force:
                    process.kill()
                else:
                    # The child runs in its own group (CREATE_NEW_PROCESS_GROUP),
                    # so the break reaches it alone; its run.py turns the break
                    # into KeyboardInterrupt (alhazen.cli.console_break).
                    process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(process.pid, signal.SIGKILL if force else signal.SIGINT)
        except ProcessLookupError:
            # The run exited between poll() and the signal. There is nothing
            # left to interrupt, and _finish is already recording how it ended.
            pass

    def _kill_later(self, key: str, process: subprocess.Popen) -> None:
        try:
            process.wait(timeout=STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            self._force_kill(key, process)

    def _force_kill(self, key: str, process: subprocess.Popen) -> None:
        """Kill a run that outlived the grace — and say so wherever its history is read."""
        with self.lock:
            if process.poll() is not None:
                # It exited on its own at the last moment: nothing was forced,
                # and _finish will record the clean ending.
                return
            run = self.runs[key]
            run["stopped"] = "forced"
            self._save_run(run)
        # Written before the kill so that it is the log's last line: a killed
        # child writes nothing more, and _finish (which waits for the exit) runs
        # after this, so whoever reads the tail finds the verdict at the end.
        with (self.directory / "runs" / key / "console.log").open("ab") as log:
            log.write(
                f"\n[workspace] Run killed after {STOP_GRACE_S} s without exiting; "
                "its data may be incomplete because teardown did not finish.\n".encode()
            )
        self._signal(process, force=True)

    def close(self) -> None:
        with self.lock:
            if self.active:
                self.stop(self.active)
        if self.worker:
            # Long enough for the grace and the kill: leaving earlier would
            # strand the run as "stopping" for the next start to call interrupted.
            self.worker.join(timeout=STOP_GRACE_S + 5)

    def detail(self, key: str) -> dict[str, Any]:
        with self.lock:
            if key not in self.runs:
                raise ValueError("Unknown run")
            # As the history lists it, with an active run's drawn seed.
            run = self._listed(self.runs[key])
        directory = self.directory / "runs" / key
        log = directory / "console.log"
        tail = ""
        if log.exists():
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 65536))
                tail = stream.read(65536).decode("utf-8", errors="replace")
        artifacts = []
        root = directory / "media"
        for path in sorted(root.rglob("*")):
            if (
                path.suffix.lower() in MEDIA_TYPES
                and path.is_file()
                and path.resolve().is_relative_to(root)
            ):
                stat = path.stat()
                artifacts.append(
                    {
                        # Posix, like every relative path the workspace hands out.
                        "path": path.relative_to(root).as_posix(),
                        "size": stat.st_size,
                        "modified": stat.st_mtime_ns,
                        "type": MEDIA_TYPES[path.suffix.lower()],
                    }
                )
                if len(artifacts) >= 500:
                    break
        # The existing session monitor retains its own authentication and pause
        # policy; offer its URL rather than reimplementing those controls.
        urls = re.findall(r"http://127\.0\.0\.1:\d+/\?token=[A-Za-z0-9_-]+", tail)
        return {
            **run,
            "log": tail,
            "artifacts": artifacts,
            "monitor": urls[-1] if urls else None,
            "measurement": measurement_status(directory),
        }
