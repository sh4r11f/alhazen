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
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from alhazen.config.loader import load_rig
from alhazen.data.atomic import replace_atomically
from alhazen.modes import Mode, flag_refusal

MEDIA = {
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


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Path must stay inside its project or run directory")
    return path


def mapping(text: str) -> dict[str, Any]:
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


def script_actions(root: Path) -> list[dict[str, Any]]:
    """Recognise runnable preview/movie modules by their literal argparse flags.

    A preview.py without a CLI (kde-vergence's viewer helper, for example)
    is not an image generator. Requiring --out and a __main__ guard avoids
    offering a button which runs successfully but produces nothing.
    """
    actions = []
    for source in sorted((root / "src").glob("*/*.py")):
        if source.stem not in {"preview", "movie"}:
            continue
        text = source.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError:
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
                "label": "Preview images" if source.stem == "preview" else "Movie script",
                "module": module,
                "params_flag": params_flag,
                "rig_flag": "--rig" in flags,
                "flags": sorted(flags),
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
    session: int = Field(default=1, ge=1)
    seed: int = Field(default=0, ge=0)
    trials: int = Field(default=1, ge=1)
    headless: bool = False
    mouse: bool = False
    windowed: bool = False
    scale: float = Field(default=0.5, gt=0, le=1)
    sheet: bool = False
    columns: int | None = Field(default=None, ge=1)
    clips: list[str] = Field(default_factory=list)
    script_args: str = ""


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
            json.loads(registry.read_text(encoding="utf-8")) if registry.exists() else []
        )
        self.runs: dict[str, dict[str, Any]] = {}
        for path in sorted((self.directory / "runs").glob("*/run.json")):
            run = json.loads(path.read_text(encoding="utf-8"))
            if run["status"] in ACTIVE:
                run.update(status="interrupted", finished=now())
                replace_atomically(path, json.dumps(run, indent=2))
            self.runs[run["id"]] = run

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
        }
        with self.lock:
            if self.active and self.runs[self.active]["project"] == project["id"]:
                raise ValueError(
                    "Wait for this experiment's run to finish before changing its interpreter"
                )
            self.projects = [p for p in self.projects if p["id"] != project["id"]] + [project]
            self._save_projects()
        return self.describe(project["id"])

    def remove(self, key: str) -> None:
        with self.lock:
            self.project(key)
            if self.active and self.runs[self.active]["project"] == key:
                raise ValueError("Stop this experiment's run before removing it")
            self.projects = [p for p in self.projects if p["id"] != key]
            self._save_projects()

    def describe(self, key: str) -> dict[str, Any]:
        project = self.project(key)
        root = Path(project["path"])
        rigs, params = [], []
        for path in sorted((root / "configs").rglob("*")):
            if path.suffix not in {".yaml", ".yml"} or not path.resolve().is_relative_to(root):
                continue
            relative = str(path.relative_to(root))
            if path.stem.startswith("rig"):
                rigs.append(relative)
            elif path.stem.startswith(("task", "params")):
                params.append(relative)
        return {
            **project,
            "rigs": rigs,
            "configs": params,
            "scripts": script_actions(root),
            "available": (root / "run.py").is_file(),
        }

    def config(self, key: str, path: str) -> dict[str, Any]:
        root = Path(self.project(key)["path"])
        target = inside(root, path)
        if target.suffix not in {".yaml", ".yml"}:
            raise ValueError("Choose a YAML config")
        content = target.read_text(encoding="utf-8")
        return {"text": content, "values": mapping(content)}

    def schema(self, key: str) -> dict[str, Any]:
        project = self.project(key)
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            [
                str(Path(__file__).resolve().parents[2]),
                str(Path(project["path"]) / "src"),
                project["path"],
                env.get("PYTHONPATH", ""),
            ]
        )
        try:
            result = subprocess.run(
                [
                    project["python"],
                    "-m",
                    "alhazen.cli.workspace_schema",
                    str(Path(project["path"]) / "run.py"),
                ],
                cwd=project["path"],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("Reading the task's parameter choices timed out") from exc
        if result.returncode:
            raise ValueError(f"Cannot read task parameter choices: {result.stderr[-2000:]}")
        return json.loads(result.stdout)

    def state(self) -> dict[str, Any]:
        with self.lock:
            return {
                "projects": [self.describe(p["id"]) for p in self.projects],
                "runs": [
                    dict(r)
                    for r in sorted(self.runs.values(), key=lambda r: r["started"], reverse=True)
                ],
                "active": self.active,
                "directory": str(self.directory),
            }

    def _command(self, request: Launch, run_dir: Path) -> list[str]:
        project = self.project(request.project)
        root = Path(project["path"])
        rig_path = inside(root, request.rig)
        if not request.rig or not rig_path.is_file():
            raise ValueError("Choose an existing rig YAML file")
        load_rig(rig_path)
        params = run_dir / "params.yaml"
        output = run_dir / "media"
        base = [project["python"], "-u"]
        if request.mode in {m.value for m in Mode}:
            mode = Mode(request.mode)
            refusal = flag_refusal(mode, headless=request.headless, mouse=request.mouse)
            if refusal:
                raise ValueError(refusal)
            if mode is Mode.MEASURE and (
                request.parameters is not None or request.parameters_yaml is not None
            ):
                raise ValueError("Measure rig does not use task parameters")
            if request.script_args.strip():
                raise ValueError("Extra script arguments are only used with standalone scripts")
            if mode in {Mode.RUN, Mode.TEST} and not request.subject.strip():
                raise ValueError("A subject ID is required for run and test modes")
            command = base + [
                str(root / "run.py"),
                "--mode",
                mode.value,
                "--rig",
                str(rig_path),
                "--seed",
                str(request.seed),
                "--no-dashboard-browser",
            ]
            if request.parameters is not None or request.parameters_yaml is not None:
                command += ["--params", str(params)]
            if mode.runs_trials:
                command += ["--ses", str(request.session)]
                if request.subject.strip():
                    command += ["--sub", request.subject.strip()]
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
            return command
        action = next((s for s in script_actions(root) if s["id"] == request.mode), None)
        if action is None:
            raise ValueError("Unknown experiment mode or script")
        command = base + ["-m", action["module"], "--out", str(output)]
        if action["rig_flag"]:
            command += ["--rig", str(rig_path)]
        if request.parameters is not None or request.parameters_yaml is not None:
            if not action["params_flag"]:
                raise ValueError("This script has no parameter-file option; use its own arguments")
            command += [action["params_flag"], str(params)]
        extra = shlex.split(request.script_args)
        reserved = {"--out", "--rig", "--params", "--task-config"}
        if any(token.split("=", 1)[0] in reserved for token in extra):
            raise ValueError("Set the rig, parameters and output through the dashboard controls")
        return command + extra

    def start(self, request: Launch) -> dict[str, Any]:
        with self.lock:
            if self.active:
                raise ValueError(
                    "Another run is active. Finish or stop it before starting a new one."
                )
            key = uuid.uuid4().hex
            run_dir = self.directory / "runs" / key
            command = self._command(request, run_dir)
            if request.parameters is not None and request.parameters_yaml is not None:
                raise ValueError("Supply either parameter fields or YAML, not both")
            values = (
                mapping(request.parameters_yaml)
                if request.parameters_yaml is not None
                else request.parameters
            )
            text = yaml.safe_dump(values, sort_keys=False) if values is not None else None
            if text is not None:
                mapping(text)
            project = self.project(request.project)
            (run_dir / "media").mkdir(parents=True)
            if text is not None:
                (run_dir / "params.yaml").write_text(text, encoding="utf-8")
            # Preserve the rig exactly as launched, without relocating it: relative
            # paths in a rig keep their usual experiment-working-directory meaning.
            (run_dir / "rig.yaml").write_bytes(
                inside(Path(project["path"]), request.rig).read_bytes()
            )
            run = {
                "id": key,
                "project": project["id"],
                "name": project["name"],
                "mode": request.mode,
                "rig": request.rig,
                "started": now(),
                "finished": None,
                "status": "running",
                "returncode": None,
                "command": command,
                "cwd": project["path"],
                "directory": str(run_dir),
            }
            self.runs[key] = run
            self._save_run(run)
            env = os.environ.copy()
            # The project may be a src-layout checkout without an editable install.
            # Keep THIS alhazen checkout first so the launcher and runner agree.
            env["PYTHONPATH"] = os.pathsep.join(
                [
                    str(Path(__file__).resolve().parents[2]),
                    str(Path(project["path"]) / "src"),
                    project["path"],
                    env.get("PYTHONPATH", ""),
                ]
            )
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
            run = dict(self.runs[key])
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
                path.suffix.lower() in MEDIA
                and path.is_file()
                and path.resolve().is_relative_to(root)
            ):
                stat = path.stat()
                artifacts.append(
                    {
                        "path": str(path.relative_to(root)),
                        "size": stat.st_size,
                        "modified": stat.st_mtime_ns,
                        "type": MEDIA[path.suffix.lower()],
                    }
                )
                if len(artifacts) >= 500:
                    break
        # The existing session monitor retains its own authentication and pause
        # policy; offer its URL rather than reimplementing those controls.
        urls = re.findall(r"http://127\.0\.0\.1:\d+/\?token=[A-Za-z0-9_-]+", tail)
        return {**run, "log": tail, "artifacts": artifacts, "monitor": urls[-1] if urls else None}
