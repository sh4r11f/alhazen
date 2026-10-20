"""This machine's rig: the one rig a dashboard workspace runs every session on.

A dashboard workspace is one machine. Its experiments are registered there and
run there, so the rig a session runs on is not a choice to make at each launch:
it is the machine the dashboard is running on. The workspace keeps that machine
as ONE complete rig file in its own state folder,

    <state>/machine-rig/rig-<name>.yaml      the machine (no ``extends``)
    <state>/machine-rig/rig-<name>_gamma.yaml, rig-<name>.reward.yaml
                                             what Measure rig and
                                             ``alhazen calibrate`` keep beside it
    <state>/machine-rig/machine.json         where it came from, and each save
    <state>/machine-rig/history/             every earlier version of the file

and a session runs on that rig, changed for the session only where the Run
page changed it. The rig a session ran is written into its run folder, named
like the machine's (so PsychoPy's monitor registration, a measured gamma and
a reward calibration are found exactly as they are for the machine's own
file), and launched as ``--rig <that file>``.

Why one flat file rather than ``extends``: an experiment's rig that extends a
shared one is two files merged, and which settings a session got depended on
which experiment's folder it was launched from. A machine's rig is the
machine, the same for every experiment run on it; what differs per experiment
is chosen per session and recorded with it.

**What an experiment adds.** The shared rigs leave out four things that are
each experiment's, not the machine's (docs/rigs.md §2): which of the task's
events go out on which sync line, which events flash the photodiode patch,
the EDF file name and a tighter calibration limit. Event names are refused in
the machine's file — a machine rig naming an event one task never declares
would refuse every other experiment's sessions — and a session's events are
carried over from the last session of the same experiment and task on this
machine (`prefill`), else taken from the experiment's own rig of this
machine's name, else left unassigned and shown so.

Everything here reads and writes the workspace's state folder only; nothing
here touches an experiment's folder or imports a device.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from alhazen.config.gamma import GAMMA_FILENAME_SUFFIX
from alhazen.config.loader import read_mapping, validate_rig
from alhazen.config.reward_calibration import REWARD_CALIBRATION_SUFFIX
from alhazen.config.rigs import FILE_PREFIX, NAME_PATTERN, MergedRig, rig_mapping
from alhazen.data.atomic import replace_atomically
from alhazen.errors import ConfigError

FOLDER = "machine-rig"
META_FILE = "machine.json"
HISTORY = "history"
MAX_RIG_BYTES = 256 * 1024
# How many saves machine.json lists; history/ keeps every earlier file.
MAX_SAVES = 200

# The settings each experiment adds to a machine (docs/rigs.md §2 "What an
# experiment adds"), carried from one session to the next of the same
# experiment and task on this machine. The first two name the task's events.
EVENT_LINES = ("devices", "sync", "event_lines")
PHOTODIODE_EVENTS = ("display", "photodiode", "events")
EVENT_PATHS = (EVENT_LINES, PHOTODIODE_EVENTS)
CARRIED_PATHS = (
    EVENT_LINES,
    PHOTODIODE_EVENTS,
    ("devices", "eyetracker", "edf_host_filename"),
    ("devices", "eyetracker", "accuracy_max_deg"),
)
# What a session may not change: whether real data may be collected is the
# machine's (docs/rigs.md §5), decided once in its file, never at a launch.
MACHINE_ONLY = (("real_data",),)

NOT_SET_UP = (
    "This machine has no rig yet. Open the Run page (or an experiment's General page) and set "
    "up this machine's rig once, or run `alhazen rig init` in a terminal."
)


class MachineRigError(ValueError):
    """A refusal about this machine's rig, worded for the person at it."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dotted(path: tuple[str, ...]) -> str:
    return ".".join(path)


def get_path(values: Any, path: tuple[str, ...]) -> tuple[bool, Any]:
    """``(found, value)`` at ``path`` in nested mappings."""
    current = values
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return False, None
        current = current[key]
    return True, current


def set_path(values: dict[str, Any], path: tuple[str, ...], value: Any) -> bool:
    """Set ``path`` to ``value`` where its parent section exists; False (and
    nothing set) when it does not — an event line is no use on a machine with
    no sync device, and creating one would invent hardware."""
    parent: Any = values
    for key in path[:-1]:
        if not isinstance(parent, dict) or not isinstance(parent.get(key), dict):
            return False
        parent = parent[key]
    parent[path[-1]] = value
    return True


def strip_events(values: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """``values`` with no event names in it, and the names taken out:
    ``{"event_lines": {...}, "photodiode_events": [...]}`` (only what was there)."""
    clean = copy.deepcopy(values)
    removed: dict[str, Any] = {}
    for path, key, kind in (
        (EVENT_LINES, "event_lines", dict),
        (PHOTODIODE_EVENTS, "photodiode_events", list),
    ):
        found, names = get_path(clean, path)
        if found and names:
            removed[key] = kind(names)
            # Taken out rather than emptied: absent is the model's own
            # default (none), and reads the same as a rig that never had any.
            _found, parent = get_path(clean, path[:-1])
            del parent[path[-1]]
    return clean, removed


def event_names(values: Any) -> list[str]:
    """Every event name a rig's values name, sorted."""
    names: set[str] = set()
    found, lines = get_path(values, EVENT_LINES)
    if found and isinstance(lines, dict):
        names.update(str(k) for k in lines)
    found, events = get_path(values, PHOTODIODE_EVENTS)
    if found and isinstance(events, list):
        names.update(str(e) for e in events)
    return sorted(names)


def rig_changes(machine: Any, session: Any, prefix: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Every setting the session's rig has differently from the machine's, as
    ``{"path", "machine", "session"}``, a missing side marked by ``absent``.
    Sections are compared key by key; a list or a value compares whole."""
    changes: list[dict[str, Any]] = []
    if isinstance(machine, dict) and isinstance(session, dict):
        for key in list(machine) + [k for k in session if k not in machine]:
            path = (*prefix, str(key))
            if key not in session:
                changes.append({"path": dotted(path), "machine": machine[key], "absent": "session"})
            elif key not in machine:
                changes.append({"path": dotted(path), "session": session[key], "absent": "machine"})
            else:
                changes.extend(rig_changes(machine[key], session[key], path))
        return changes
    if machine != session:
        changes.append({"path": dotted(prefix), "machine": machine, "session": session})
    return changes


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_name(name: Any) -> str:
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name.strip()):
        raise MachineRigError(
            "A machine's name is letters, digits, '.', '_' or '-', starting with a letter or "
            "digit (it becomes the rig's file name, rig-<name>.yaml)"
        )
    return name.strip()


def dump(values: dict[str, Any], header: str) -> str:
    """A rig's values as YAML, under a comment header."""
    lines = [f"# {line}".rstrip() if line else "#" for line in header.strip().splitlines()]
    body = yaml.safe_dump(values, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return "\n".join(lines) + "\n\n" + body


def validate(values: dict[str, Any], path: Path) -> None:
    """A complete rig, checked the way a launch checks one; ConfigError if not."""
    validate_rig(MergedRig(values, None, None), path)


@dataclass(frozen=True)
class Machine:
    """This machine's rig as read: its name, its file and its settings."""

    name: str
    path: Path
    text: str
    values: dict[str, Any]

    @property
    def sha256(self) -> str:
        return sha256_text(self.text)


class MachineRig:
    """The workspace's machine rig; see the module."""

    def __init__(self, state_dir: Path):
        self.folder = Path(state_dir) / FOLDER

    # -- reading ----------------------------------------------------------------

    def file(self) -> Path | None:
        """The machine's rig file, or None before it is set up. Two rig files
        in the folder are refused rather than one picked: whichever were
        picked, sessions would silently run on the other machine's numbers."""
        if not self.folder.is_dir():
            return None
        files = sorted(
            p
            for p in self.folder.iterdir()
            if p.is_file()
            and p.name.startswith(FILE_PREFIX)
            and p.suffix in {".yaml", ".yml"}
            and not p.name.endswith(GAMMA_FILENAME_SUFFIX)
            and not p.name.endswith(REWARD_CALIBRATION_SUFFIX)
        )
        if len(files) > 1:
            raise MachineRigError(
                f"{self.folder} holds {len(files)} rig files ({', '.join(p.name for p in files)}); "
                "a machine has one rig. Move the one that is not this machine's out of the folder"
            )
        return files[0] if files else None

    def meta(self) -> dict[str, Any]:
        path = self.folder / META_FILE
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def read(self) -> Machine | None:
        path = self.file()
        if path is None:
            return None
        text = path.read_text(encoding="utf-8-sig")
        return Machine(path.stem.removeprefix(FILE_PREFIX), path, text, read_mapping(path))

    def require(self) -> Machine:
        machine = self.read()
        if machine is None:
            raise MachineRigError(NOT_SET_UP)
        return machine

    def status(self) -> dict[str, Any]:
        """What the pages show about this machine's rig."""
        try:
            machine = self.read()
        except (MachineRigError, ConfigError) as exc:
            return {"configured": False, "error": str(exc), "folder": str(self.folder)}
        if machine is None:
            return {"configured": False, "error": None, "folder": str(self.folder)}
        problem = None
        try:
            validate(machine.values, machine.path)
        except ConfigError as exc:
            problem = str(exc)
        meta = self.meta()
        return {
            "configured": True,
            "error": None,
            "name": machine.name,
            "file": str(machine.path),
            "folder": str(self.folder),
            "text": machine.text,
            "sha256": machine.sha256,
            "values": machine.values,
            "problem": problem,
            "created": meta.get("created"),
            "template": meta.get("template"),
            "saves": (meta.get("saves") or [])[-5:],
        }

    # -- writing ----------------------------------------------------------------

    def _write(self, name: str, text: str, entry: dict[str, Any]) -> Path:
        """Write the machine's file atomically, keeping the one it replaces in
        history/, and note the save in machine.json."""
        self.folder.mkdir(parents=True, exist_ok=True)
        target = self.folder / f"{FILE_PREFIX}{name}.yaml"
        old = self.file()
        if old is not None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            (self.folder / HISTORY).mkdir(exist_ok=True)
            shutil.copy2(old, self.folder / HISTORY / f"{old.stem}.{stamp}{old.suffix}")
        replace_atomically(target, text)
        meta = self.meta()
        saves = list(meta.get("saves") or [])
        saves.append({**entry, "at": now(), "sha256": sha256_text(text)})
        meta["saves"] = saves[-MAX_SAVES:]
        meta.setdefault("created", now())
        meta["name"] = name
        replace_atomically(self.folder / META_FILE, json.dumps(meta, indent=2, ensure_ascii=False))
        return target

    def setup(
        self,
        name: str,
        source: Path,
        *,
        shared: dict[str, Path] | None,
        template: dict[str, Any],
    ) -> dict[str, Any]:
        """Set up this machine's rig from ``source``, a shared rig or one of a
        registered experiment's, written out complete (``extends`` merged).

        A source that extends nothing and names no event is copied byte for
        byte, comments and all; any other is written as its merged settings
        under a header saying what it was made from. Event names are taken
        out (they are each experiment's) and returned, so the caller can say
        so. Refused when the machine already has a rig: this is set up once,
        and edited after (`save_text`)."""
        name = check_name(name)
        if self.file() is not None:
            raise MachineRigError(
                "This machine already has a rig; edit it on an experiment's General page "
                "(This machine's rig) instead of setting it up again"
            )
        merged = rig_mapping(Path(source), shared=shared)
        values, removed = strip_events(merged.values)
        target = self.folder / f"{FILE_PREFIX}{name}.yaml"
        validate(values, target)
        if merged.base is None and not removed:
            text = Path(source).read_text(encoding="utf-8-sig")
        else:
            made = [f"This machine's rig, '{name}': one complete file, made {now()[:10]} from"]
            made.append(f"{template.get('label') or source} ({source})")
            if merged.base is not None:
                made.append(f"merged over alhazen's shared rig '{merged.extends}' ({merged.base}).")
            if removed:
                made.append(
                    "Event names were left out: which of a task's events go out on which line is "
                    "chosen per session on the Run page."
                )
            text = dump(values, "\n".join(made))
        meta_template = {
            **template,
            "file": str(source),
            "sha256": hashlib.sha256(Path(source).read_bytes()).hexdigest(),
        }
        if merged.base is not None:
            meta_template["extends"] = merged.extends
        self._write(name, text, {"by": "setup", "template": meta_template.get("label")})
        meta = self.meta()
        meta["template"] = meta_template
        if removed:
            meta["left_out"] = removed
        replace_atomically(self.folder / META_FILE, json.dumps(meta, indent=2, ensure_ascii=False))
        return {**self.status(), "left_out": removed}

    def _check_text(self, text: str, path: Path) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise MachineRigError("The rig is empty")
        if len(text.encode("utf-8")) > MAX_RIG_BYTES:
            raise MachineRigError("A rig file must be at most 256 KiB")
        try:
            values = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise MachineRigError(f"invalid YAML: {exc}") from exc
        if not isinstance(values, dict):
            raise MachineRigError("A rig file is a YAML mapping (key: value lines)")
        if "extends" in values:
            raise MachineRigError(
                "This machine's rig is one complete file and extends nothing: write out the "
                "settings it would take from the shared rig"
            )
        names = event_names(values)
        if names:
            raise MachineRigError(
                f"This machine's rig names events ({', '.join(names)}). Event names are each "
                "experiment's: choose which go out on which line on the Run page, per session. "
                "Leave event_lines and the photodiode's events empty here"
            )
        try:
            validate(values, path)
        except ConfigError as exc:
            raise MachineRigError(str(exc)) from exc
        return values

    def check_text(self, text: str) -> dict[str, Any]:
        machine = self.require()
        values = self._check_text(text, machine.path)
        return {"ok": True, "changes": rig_changes(machine.values, values)}

    def save_text(self, text: str, expected_sha: str | None, *, by: str) -> dict[str, Any]:
        """Replace the machine's file with ``text`` (the General page's editor),
        refused if the file changed since it was read (``expected_sha``)."""
        machine = self.require()
        if expected_sha is not None and expected_sha != machine.sha256:
            raise MachineRigError(
                "This machine's rig changed since it was opened (another page saved it); "
                "reopen it and make the change again"
            )
        values = self._check_text(text, machine.path)
        changes = rig_changes(machine.values, values)
        self._write(machine.name, text, {"by": by, "changes": [c["path"] for c in changes]})
        return self.status()

    def save_values(
        self, values: dict[str, Any], expected_sha: str | None, *, by: str
    ) -> dict[str, Any]:
        """Save a session's rig settings as this machine's (the Run page's
        "Save to this machine's rig"). Event names are left out: they stay the
        session's. The file is written from the settings, so its comments
        go; the previous file is kept in history/."""
        machine = self.require()
        if expected_sha is not None and expected_sha != machine.sha256:
            raise MachineRigError(
                "This machine's rig changed since this page read it; reload the page and make "
                "the change again"
            )
        if not isinstance(values, dict):
            raise MachineRigError("The rig settings must be a mapping")
        clean, _removed = strip_events(values)
        if "extends" in clean:
            raise MachineRigError("This machine's rig extends nothing")
        try:
            validate(clean, machine.path)
        except ConfigError as exc:
            raise MachineRigError(str(exc)) from exc
        changes = rig_changes(machine.values, clean)
        if not changes:
            return {**self.status(), "saved": []}
        header = (
            f"This machine's rig, '{machine.name}'. Saved {now()[:10]} from the Run page "
            f"(changed: {', '.join(c['path'] for c in changes)}); earlier versions are in "
            f"{HISTORY}/ beside this file."
        )
        self._write(
            machine.name, dump(clean, header), {"by": by, "changes": [c["path"] for c in changes]}
        )
        return {**self.status(), "saved": [c["path"] for c in changes]}

    # -- a session's rig ----------------------------------------------------------

    def companions(self, machine: Machine) -> list[Path]:
        """The files kept beside the machine's rig and read with it: a
        measured gamma and a reward calibration, where they exist."""
        stem = machine.path.stem
        found = [
            self.folder / f"{stem}{GAMMA_FILENAME_SUFFIX}",
            self.folder / f"{stem}{REWARD_CALIBRATION_SUFFIX}",
        ]
        return [p for p in found if p.is_file()]


def session_header(machine: Machine, origin: dict[str, Any]) -> str:
    changes = origin.get("changes") or []
    lines = [
        f"The rig this session ran on: this machine's rig '{machine.name}' ({machine.path},",
        f"sha256 {machine.sha256[:12]}),",
    ]
    if changes:
        lines.append("changed for this session only:")
        for change in changes:
            if change.get("absent") == "session":
                lines.append(f"  {change['path']}: removed (machine: {change.get('machine')!r})")
            elif change.get("absent") == "machine":
                lines.append(f"  {change['path']}: {change.get('session')!r} (machine: not set)")
            else:
                lines.append(
                    f"  {change['path']}: {change.get('session')!r} "
                    f"(machine: {change.get('machine')!r})"
                )
    else:
        lines.append("unchanged.")
    events = origin.get("events") or {}
    source = (events.get("source") or {}).get("label")
    if source:
        lines.append(f"Event lines: {source}.")
    return "\n".join(lines)


def write_session_rig(
    folder: Path, machine: Machine, values: dict[str, Any], origin: dict[str, Any], companions
) -> Path:
    """Write the rig a session runs into ``folder`` as ``rig-<machine>.yaml``,
    with the machine's gamma and reward calibration beside it under the same
    stem, and return its path. An unchanged rig is the machine's file byte
    for byte; a changed one is its settings under a header listing what
    changed."""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / machine.path.name
    if values == machine.values:
        target.write_text(machine.text, encoding="utf-8")
    else:
        target.write_text(dump(values, session_header(machine, origin)), encoding="utf-8")
    for companion in companions:
        shutil.copy2(companion, folder / companion.name)
    return target
