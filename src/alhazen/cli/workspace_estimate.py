"""The Run page's duration estimate: asked of the project's own interpreter.

POST /api/estimate takes the launch form as it stands and answers how long
that launch would take (alhazen.modes.estimate says what is counted). The
answer comes from the project's own alhazen, through the same run.py a
launch starts, with ``--estimate-duration`` (alhazen.cli.duration): so the
rig, the task, the params and the mode are read exactly as the run would
read them, by the code that would run. Nothing is launched, no run record or
run folder is made, no subject is asked for; the edited params go to a
temporary file that is removed when the answer is in.

The secret this hides: how an estimate is asked and cached. A child
interpreter takes a few seconds, the page asks again on every edit, and the
same form is asked about repeatedly, so answers are kept by everything they
depend on — the interpreter and its alhazen, the project's source files'
modification times, the command, the params text and the rig as merged —
and a request whose answer is being computed waits for it instead of
starting a second child. A project whose alhazen cannot estimate (registered
without the ``duration-estimate`` capability) is answered "unavailable"
without a child.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from alhazen.cli.workspace import (
    MODE_FLAGS,
    CalibrationChoice,
    Launch,
    _calibration_arguments,
    _child_env,
    _extra_arguments,
    check_measurements,
    parse_parameters,
    script_actions,
)
from alhazen.config.rigs import rig_mapping
from alhazen.modes import Mode, flag_refusal

CAPABILITY = "duration-estimate"
FLAG = "--estimate-duration"
# Longer than a schema read: the child imports the experiment and builds its
# scheduler. One that has not answered in this long is not going to.
ESTIMATE_TIMEOUT_S = 60
# Answers kept, most recent last.
CACHE_SIZE = 64
# Directories a project's source stamp never looks in: they hold data,
# environments and tool caches, not the code an estimate runs.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "data",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "notebooks",
        "docs",
    }
)
SOURCE_SUFFIXES = frozenset({".py", ".yaml", ".yml", ".toml", ".json", ".md"})


class EstimateRequest(BaseModel):
    """The launch form's fields that decide how long a launch takes. The
    rest (who, the seed, movie output options) do not change it."""

    model_config = ConfigDict(extra="forbid")
    project: str
    mode: str
    rig: str
    task: str | None = None
    parameter_set: str | None = None
    parameters_yaml: str | None = None
    parameters: dict[str, Any] | None = None
    trials: int = Field(default=1, ge=1)
    headless: bool = False
    mouse: bool = False
    extra_args: str = ""
    calibration_target: CalibrationChoice | None = None
    measurements: list[str] | None = None


def _answer(status: str, headline: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"schema": 1, "status": status, "headline": headline, "reason": reason, **extra}


def source_stamp(root: Path) -> str:
    """The project's source as of now, cheaply: every source file's path,
    size and modification time, hashed. Any edit to run.py, the package or a
    config changes it, so a cached answer never outlives the code it came
    from."""
    digest = hashlib.sha256()
    for directory, names, files in os.walk(root):
        names[:] = sorted(n for n in names if n not in SKIP_DIRS and not n.startswith("."))
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix not in SOURCE_SUFFIXES:
                continue
            try:
                stat = path.stat()
                entry = f"{stat.st_size}\0{stat.st_mtime_ns}"
            except OSError as exc:
                # Gone or unreadable between the listing and the stat: that
                # is part of the source's state too, so it goes in the stamp
                # (and the next ask, when the file is back, differs).
                entry = f"unreadable: {type(exc).__name__}"
            digest.update(f"{path.relative_to(root)}\0{entry}\n".encode())
    return digest.hexdigest()


class DurationEstimator:
    """Answers POST /api/estimate for one workspace; see the module."""

    def __init__(self, workspace: Any) -> None:
        self.workspace = workspace
        self._cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._pending: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        # One child at a time per workspace: an estimate is a whole
        # interpreter, and the page's own requests are debounced already.
        self._child = threading.Semaphore(1)

    def estimate(self, request: EstimateRequest) -> dict[str, Any]:
        workspace = self.workspace
        project = workspace.project(request.project)
        modes = {m.value for m in Mode}
        if request.mode not in modes:
            action = next(
                (s for s in script_actions(Path(project["path"])) if s["id"] == request.mode), None
            )
            if action is None:
                raise ValueError("Unknown experiment mode or script")
            return _answer(
                "unknown",
                "No reliable estimate",
                f"{action.get('label') or request.mode} is a standalone script: how long it runs "
                "is up to the script, which declares no estimate.",
            )
        capabilities = project.get("capabilities")
        if not capabilities or CAPABILITY not in capabilities:
            why = (
                "was registered before the workspace asked what its alhazen can do; open "
                "Project settings and save, to register it again"
                if capabilities is None
                else f"runs alhazen {project.get('alhazen_version')}, which cannot estimate a "
                "session's duration; update its alhazen, then open Project settings and save"
            )
            return _answer(
                "unavailable",
                "Estimate unavailable for this environment",
                f"{project['name']} {why}.",
            )

        mode = Mode(request.mode)
        refusal = flag_refusal(
            mode,
            headless=request.headless,
            mouse=request.mouse,
            calibration=bool(_calibration_arguments(request.calibration_target)),
        )
        if refusal:
            return _answer("refused", "This launch would be refused", refusal)
        if mode is Mode.MEASURE and (request.parameters is not None or request.parameters_yaml):
            raise ValueError("Measure rig does not use task parameters")
        check_measurements(project, request.mode, request.measurements)
        launch = Launch(
            project=request.project,
            mode=request.mode,
            rig=request.rig,
            task=request.task,
            parameter_set=request.parameter_set,
            extra_args=request.extra_args,
        )
        task = workspace._task_for(project, launch)
        workspace._check_parameter_set(project, launch, task)
        reserved = MODE_FLAGS | {"--task", FLAG} if task is not None else MODE_FLAGS | {FLAG}
        if task is None:
            task = workspace._one_task(project, launch)
        extra = _extra_arguments(request.extra_args, reserved)
        ref, shared = workspace._launch_rig(project, request.rig)
        rig = ref.spec if ref.source == "alhazen" else str(ref.path)

        if request.parameters is not None and request.parameters_yaml is not None:
            raise ValueError("Supply either parameter fields or YAML, not both")
        values = (
            parse_parameters(request.parameters_yaml)
            if request.parameters_yaml is not None
            else request.parameters
        )
        text = yaml.safe_dump(values, sort_keys=False) if values is not None else None

        root = Path(project["path"])
        command = [str(root / "run.py"), "--mode", mode.value]
        if task is not None:
            command += ["--task", task]
        command += ["--rig", rig]
        if mode in {Mode.TEST, Mode.SIMULATE}:
            command += ["--trials-per-condition", str(request.trials)]
        if request.headless:
            command += ["--headless"]
        if request.mouse:
            command += ["--mouse"]
        for key in request.measurements or []:
            command += ["--measure", key]
        command += _calibration_arguments(request.calibration_target)
        command += extra

        merged = rig_mapping(ref.path, shared=shared)
        key = hashlib.sha256(
            json.dumps(
                {
                    "python": project["python"],
                    "alhazen": project.get("alhazen_version"),
                    "source": source_stamp(root),
                    "command": command,
                    "params": text,
                    "rig": merged.values,
                    "rig_extends": str(merged.extends),
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        form = (
            f"the parameters on the form ({request.parameter_set})"
            if request.parameter_set
            else "the parameters on the form"
        )
        return self._cached(key, lambda: self._ask(project, command, text, form))

    def _cached(self, key: str, compute: Any) -> dict[str, Any]:
        while True:
            with self._lock:
                if key in self._cache:
                    self._cache.move_to_end(key)
                    return {**self._cache[key], "cached": True, "key": key[:12]}
                event = self._pending.get(key)
                if event is None:
                    event = self._pending[key] = threading.Event()
                    break
            # Someone is computing this very answer: wait for it, then look
            # again (it may have failed, and then this request computes it).
            event.wait(ESTIMATE_TIMEOUT_S + 5)
        try:
            answer = compute()
            with self._lock:
                # Errors are not kept: a fix outside the inputs hashed here
                # (an installed package) must be picked up by the next ask.
                if answer.get("status") not in {"error"}:
                    self._cache[key] = answer
                    while len(self._cache) > CACHE_SIZE:
                        self._cache.popitem(last=False)
            return {**answer, "cached": False, "key": key[:12]}
        finally:
            with self._lock:
                self._pending.pop(key, None)
            event.set()

    def _ask(
        self,
        project: dict[str, Any],
        command: list[str],
        text: str | None,
        form: str = "the parameters on the form",
    ) -> dict[str, Any]:
        with self._child, tempfile.TemporaryDirectory(prefix="alhazen-estimate-") as scratch:
            argv = [project["python"], "-u", command[0], *command[1:]]
            if text is not None:
                params = Path(scratch) / "params.yaml"
                params.write_text(text, encoding="utf-8")
                argv += ["--params", str(params)]
            argv += [FLAG]
            try:
                result = subprocess.run(
                    argv,
                    cwd=project["path"],
                    env=_child_env(project),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=ESTIMATE_TIMEOUT_S,
                )
            except subprocess.TimeoutExpired:
                return _answer(
                    "error",
                    "No estimate",
                    f"The project's interpreter did not answer within {ESTIMATE_TIMEOUT_S} s",
                )
            except OSError as exc:
                return _answer("error", "No estimate", f"Cannot run {project['python']}: {exc}")
            params_path = str(Path(scratch) / "params.yaml")
        lines = result.stdout.strip().splitlines()
        try:
            answer = json.loads(lines[-1]) if lines else None
        except ValueError:
            answer = None
        if not isinstance(answer, dict):
            tail = (result.stderr or result.stdout).strip()[-1500:]
            answer = _answer(
                "error",
                "No estimate",
                f"The estimate failed (exit {result.returncode}): {tail or 'no output'}",
            )
        # The temporary file is gone by now: the page names what it held.
        return _renamed(answer, params_path, form)


def _renamed(value: Any, path: str, name: str) -> Any:
    """``value`` with every mention of the temporary params file replaced by
    what the page calls it."""
    if isinstance(value, str):
        return value.replace(path, name)
    if isinstance(value, list):
        return [_renamed(v, path, name) for v in value]
    if isinstance(value, dict):
        return {k: _renamed(v, path, name) for k, v in value.items()}
    return value
