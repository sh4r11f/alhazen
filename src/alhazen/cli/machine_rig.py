"""This machine's rig configs: the rigs a dashboard workspace runs its sessions on.

A dashboard workspace is one machine. Its experiments are registered there and
run there, so the rigs a session can run on are this machine's own: one or
more named **rig configs** (an EyeLink set-up, a keyboard-only one, the monkey
DAQ), one of them the **default**, kept as complete rig files in the
workspace's state folder (docs/design/machine-rig-configs.md §2):

    <state>/machine-rig/rig-<name>.yaml      3.5.0's single machine rig, left
                                             where it was: the first config
    <state>/machine-rig/configs.json         the index: ids, names, default,
                                             sync state with the platform
    <state>/machine-rig/configs/<name>/rig-<name>.yaml
                                             every other config, each with
    .../rig-<name>_gamma.yaml, rig-<name>.reward.yaml
                                             what Measure rig and
                                             ``alhazen calibrate`` keep beside it
    .../config.json (machine.json for the first), history/
                                             each save, every earlier version
    <state>/machine-rig/trash/               deleted configs, moved, not destroyed

and a session runs on the config chosen on the Run page, changed for the
session only where the Run page changed it. The rig a session ran is written
into its run folder, named like the config's (so PsychoPy's monitor
registration, a measured gamma and a reward calibration are found exactly as
they are for the config's own file), and launched as ``--rig <that file>``.

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
import os
import secrets
import shutil
import threading
from dataclasses import dataclass, field
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
CONFIG_META = "config.json"
INDEX = "configs.json"
CONFIGS = "configs"
TRASH = "trash"
HISTORY = "history"
LEGACY = "."
INDEX_SCHEMA = 1
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
    "This machine has no rig config yet. Add one on an experiment's General page (This "
    "machine's rig configs) or run `alhazen rig init` in a terminal."
)


class MachineRigError(ValueError):
    """A refusal about this machine's rig, worded for the person at it."""


class MachineRigStale(MachineRigError):
    """A save against a version of a config that has changed since it was read (the
    dashboard answers 409)."""


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
    if (
        not isinstance(name, str)
        or len(name.strip()) > 64
        or not NAME_PATTERN.fullmatch(name.strip())
    ):
        raise MachineRigError(
            "A rig config's name is letters, digits, '.', '_' or '-', starting with a letter or "
            "digit, at most 64 characters (it becomes the rig's file name, rig-<name>.yaml)"
        )
    return name.strip()


def new_id(prefix: str, nbytes: int) -> str:
    return prefix + secrets.token_hex(nbytes)


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
    """One of this machine's rig configs as read: its id, its name, its file and its
    settings. (Called Machine since 3.5.0, when a machine had one rig.)"""

    name: str
    path: Path
    text: str
    values: dict[str, Any]
    id: str = ""
    legacy: bool = False

    @property
    def sha256(self) -> str:
        return sha256_text(self.text)


@dataclass
class Config:
    """A config as the index knows it: where its file is, before it is read."""

    id: str
    name: str
    folder: Path
    legacy: bool
    entry: dict[str, Any] = field(default_factory=dict)

    @property
    def file(self) -> Path:
        return self.folder / f"{FILE_PREFIX}{self.name}.yaml"

    @property
    def meta_file(self) -> Path:
        return self.folder / (META_FILE if self.legacy else CONFIG_META)


def _rig_files(folder: Path) -> list[Path]:
    """The rig files directly in ``folder`` (not the gamma or reward files beside them)."""
    if not folder.is_dir():
        return []
    return sorted(
        p
        for p in folder.iterdir()
        if p.is_file()
        and p.name.startswith(FILE_PREFIX)
        and p.suffix in {".yaml", ".yml"}
        and not p.name.endswith(GAMMA_FILENAME_SUFFIX)
        and not p.name.endswith(REWARD_CALIBRATION_SUFFIX)
    )


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_atomically(path, json.dumps(data, indent=2, ensure_ascii=False))


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


class MachineRigs:
    """This machine's rig configs; see the module. Every change holds ``lock``, so the
    pages and the background sync with the platform never interleave writes."""

    def __init__(self, state_dir: Path):
        self.folder = Path(state_dir) / FOLDER
        self.lock = threading.RLock()

    # -- the index ----------------------------------------------------------------

    def _legacy_file(self) -> Path | None:
        """3.5.0's machine rig, the first config, at the folder's top. Two rig files there
        are refused rather than one picked, as 3.5.0 refused them."""
        files = _rig_files(self.folder)
        if len(files) > 1:
            raise MachineRigError(
                f"{self.folder} holds {len(files)} rig files ({', '.join(p.name for p in files)}); "
                "the folder's top holds one (the first config). Move the one that is not this "
                "machine's out of the folder"
            )
        return files[0] if files else None

    def index(self) -> dict[str, Any]:
        """configs.json, made or brought up to date on the way: a 3.5.0 machine rig becomes
        the first config (``migrated``), and a rig file a 3.5.0 dashboard set up at the top
        since is adopted. Written only when something changed."""
        with self.lock:
            path = self.folder / INDEX
            data = _read_json(path)
            changed = False
            if not data:
                data = {"schema": INDEX_SCHEMA, "configs": {}, "default": None, "default_rev": 0}
            data.setdefault("configs", {})
            data.setdefault("default", None)
            data.setdefault("default_rev", 0)
            data.setdefault("deleted", {})
            data.setdefault("sync", {})
            if not data.get("machine_id"):
                data["machine_id"] = new_id("mc_", 8)
                changed = True
            legacy = self._legacy_file()
            has_legacy = any(e.get("folder") == LEGACY for e in data["configs"].values())
            if legacy is not None and not has_legacy:
                cid = new_id("c", 5)
                name = legacy.stem.removeprefix(FILE_PREFIX)
                meta = _read_json(self.folder / META_FILE)
                data["configs"][cid] = {
                    "name": name,
                    "folder": LEGACY,
                    "created": meta.get("created") or now(),
                    "origin": {
                        "kind": "3.5.0" if not data["configs"] else "older-dashboard",
                        "template": meta.get("template"),
                    },
                    "revision": len(meta.get("saves") or []) or 1,
                }
                if not data.get("default") or data["default"] not in data["configs"]:
                    data["default"] = cid
                if len(data["configs"]) == 1:
                    data["migrated"] = {"from": "3.5.0", "config": cid, "at": now()}
                changed = True
            elif legacy is not None:
                # 3.5.0 never renames, but a person may: follow the file's name.
                for entry in data["configs"].values():
                    if entry.get("folder") == LEGACY:
                        name = legacy.stem.removeprefix(FILE_PREFIX)
                        if entry.get("name") != name:
                            entry["name"] = name
                            changed = True
            if data["default"] not in data["configs"]:
                data["default"] = next(iter(data["configs"]), None)
                changed = changed or data["default"] is not None
            if changed:
                # Written at once, so the machine's id is the same from the first read on.
                _write_json(path, data)
            return data

    def _save_index(self, data: dict[str, Any]) -> None:
        _write_json(self.folder / INDEX, data)

    def machine_id(self) -> str:
        return str(self.index()["machine_id"])

    def _config(self, cid: str, entry: dict[str, Any]) -> Config:
        legacy = entry.get("folder") == LEGACY
        folder = self.folder if legacy else self.folder / CONFIGS / str(entry.get("name"))
        return Config(cid, str(entry.get("name")), folder, legacy, entry)

    def configs(self) -> list[Config]:
        """Every config, the default first, then by name."""
        data = self.index()
        out = [self._config(cid, e) for cid, e in data["configs"].items()]
        return sorted(out, key=lambda c: (c.id != data["default"], c.name.lower()))

    def default_id(self) -> str | None:
        return self.index().get("default")

    def find(self, ref: str | None = None) -> Config:
        """A config by id or name; None or "" is the default."""
        data = self.index()
        if not data["configs"]:
            raise MachineRigError(NOT_SET_UP)
        if not ref:
            ref = data["default"]
        if ref in data["configs"]:
            return self._config(str(ref), data["configs"][ref])
        for cid, entry in data["configs"].items():
            if entry.get("name") == ref:
                return self._config(cid, entry)
        names = ", ".join(sorted(str(e.get("name")) for e in data["configs"].values()))
        raise MachineRigError(f"This machine has no rig config {ref!r} (it has: {names})")

    # -- reading ------------------------------------------------------------------

    def read(self, ref: str | None = None) -> Machine | None:
        """A config's file and settings; None before the machine has any config."""
        if not self.index()["configs"]:
            return None
        config = self.find(ref)
        return self._read(config)

    def _read(self, config: Config) -> Machine:
        path = config.file
        if not path.is_file():
            raise MachineRigError(
                f"The rig config {config.name!r} has no file ({path}); it was moved or deleted "
                "outside the dashboard. Delete the config, or put the file back"
            )
        text = path.read_text(encoding="utf-8-sig")
        return Machine(config.name, path, text, read_mapping(path), config.id, config.legacy)

    def require(self, ref: str | None = None) -> Machine:
        machine = self.read(ref)
        if machine is None:
            raise MachineRigError(NOT_SET_UP)
        return machine

    def config_status(self, config: Config, default: str | None) -> dict[str, Any]:
        """What the pages show about one config."""
        meta = _read_json(config.meta_file)
        base = {
            "id": config.id,
            "name": config.name,
            "default": config.id == default,
            "legacy": config.legacy,
            "folder": str(config.folder),
            "file": str(config.file),
            "created": config.entry.get("created"),
            "origin": config.entry.get("origin"),
            "revision": config.entry.get("revision", 0),
            "saves": (meta.get("saves") or [])[-5:],
        }
        try:
            machine = self._read(config)
        except (MachineRigError, ConfigError, OSError) as exc:
            return {**base, "text": None, "sha256": None, "values": None, "problem": str(exc)}
        problem = None
        try:
            validate(machine.values, machine.path)
            names = event_names(machine.values)
            if names:
                problem = f"names events ({', '.join(names)}); event lines are chosen per session"
            elif "extends" in machine.values:
                problem = "extends a shared rig; a machine's rig config is one complete file"
        except ConfigError as exc:
            problem = str(exc)
        gamma = machine.path.parent / f"{machine.path.stem}{GAMMA_FILENAME_SUFFIX}"
        return {
            **base,
            "text": machine.text,
            "sha256": machine.sha256,
            "values": machine.values,
            "problem": problem,
            "gamma": ({"file": str(gamma), "at": _mtime(gamma)} if gamma.is_file() else None),
        }

    def _signature(self) -> tuple[Any, ...]:
        """What the configs' state depends on: the index and every config file, by
        modification time and size."""
        stamps: list[Any] = []
        for path in [self.folder / INDEX, *(c.file for c in self.configs())]:
            try:
                st = path.stat()
                stamps.append((str(path), st.st_mtime_ns, st.st_size))
            except OSError:
                stamps.append((str(path), None, None))
        return tuple(stamps)

    def choices(self) -> dict[str, Any]:
        """The configs as the Run page's chooser lists them (id, name, default, problem),
        read again only when a file changed: every page poll describes every project."""
        signature = self._signature()
        cached = getattr(self, "_choices", None)
        if cached is not None and cached[0] == signature:
            return copy.deepcopy(cached[1])
        status = self.status()
        if status.get("error"):
            raise MachineRigError(status["error"])
        value = {
            "default": status["default"],
            "configs": [
                {
                    "id": c["id"],
                    "name": c["name"],
                    "default": c["default"],
                    "problem": c["problem"],
                    "sha256": c.get("sha256"),
                }
                for c in status["configs"]
            ],
        }
        self._choices = (signature, value)
        return copy.deepcopy(value)

    def status(self) -> dict[str, Any]:
        """Every config, for the pages (docs/design/machine-rig-configs.md §3 ``Rigs``,
        without the sync line, which the workspace adds)."""
        try:
            data = self.index()
        except (MachineRigError, OSError) as exc:
            return {
                "machine": None,
                "default": None,
                "configs": [],
                "error": str(exc),
                "folder": str(self.folder),
                "migrated": None,
            }
        default = data.get("default")
        configs = [self.config_status(c, default) for c in self.configs()]
        return {
            "machine": {"id": data["machine_id"]},
            "default": default,
            "configs": configs,
            "error": None,
            "folder": str(self.folder),
            "migrated": data.get("migrated"),
        }

    # -- writing ------------------------------------------------------------------

    def _write(self, config: Config, text: str, entry: dict[str, Any]) -> Path:
        """Write a config's file atomically, keeping the one it replaces in its history/,
        and note the save in its meta file and the index."""
        config.folder.mkdir(parents=True, exist_ok=True)
        target = config.file
        if target.is_file():
            (config.folder / HISTORY).mkdir(exist_ok=True)
            shutil.copy2(
                target, config.folder / HISTORY / f"{target.stem}.{_stamp()}{target.suffix}"
            )
        replace_atomically(target, text)
        meta = _read_json(config.meta_file)
        saves = list(meta.get("saves") or [])
        saves.append({**entry, "at": now(), "sha256": sha256_text(text)})
        meta["saves"] = saves[-MAX_SAVES:]
        meta.setdefault("created", now())
        meta["name"] = config.name
        _write_json(config.meta_file, meta)
        data = self.index()
        if config.id in data["configs"]:
            data["configs"][config.id]["revision"] = (
                int(data["configs"][config.id].get("revision", 0)) + 1
            )
            self._save_index(data)
        return target

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
                "A machine's rig config is one complete file and extends nothing: write out the "
                "settings it would take from the shared rig"
            )
        names = event_names(values)
        if names:
            raise MachineRigError(
                f"This rig config names events ({', '.join(names)}). Event names are each "
                "experiment's: choose which go out on which line on the Run page, per session. "
                "Leave event_lines and the photodiode's events empty here"
            )
        try:
            validate(values, path)
        except ConfigError as exc:
            raise MachineRigError(str(exc)) from exc
        return values

    def _unique(self, name: str, but: str | None = None) -> None:
        for cid, entry in self.index()["configs"].items():
            if cid != but and entry.get("name") == name:
                raise MachineRigError(f"This machine already has a rig config named {name!r}")

    def _expect(self, machine: Machine, expected_sha: str | None) -> None:
        if expected_sha is not None and expected_sha != machine.sha256:
            raise MachineRigStale(
                f"The rig config {machine.name!r} changed since it was opened (another page or "
                "the platform saved it); reopen it and make the change again"
            )

    def create(
        self,
        name: Any,
        text: str,
        *,
        origin: dict[str, Any],
        by: str,
        cid: str | None = None,
        make_default: bool = False,
    ) -> Config:
        """A new config with ``text`` (checked as a launch checks a rig), in
        configs/<name>/. ``cid`` keeps an id the platform chose."""
        with self.lock:
            name = check_name(name)
            self._unique(name)
            data = self.index()
            cid = cid or new_id("c", 5)
            if cid in data["configs"]:
                raise MachineRigError("This machine already has a rig config with that id")
            folder = self.folder / CONFIGS / name
            if folder.exists():
                raise MachineRigError(
                    f"{folder} already exists; move it out of the way or choose another name"
                )
            self._check_text(text, folder / f"{FILE_PREFIX}{name}.yaml")
            entry = {
                "name": name,
                "folder": f"{CONFIGS}/{name}",
                "created": now(),
                "origin": origin,
                "revision": 0,
            }
            config = self._config(cid, entry)
            data["configs"][cid] = entry
            if make_default or not data.get("default"):
                data["default"] = cid
                data["default_rev"] = int(data.get("default_rev", 0)) + 1
            self._save_index(data)
            self._write(config, text, {"by": by, "created": True})
            return config

    def setup(
        self,
        name: Any,
        source: Path,
        *,
        shared: dict[str, Path] | None,
        template: dict[str, Any],
        by: str = "setup",
    ) -> dict[str, Any]:
        """A new config from ``source``, a shared rig or one of a registered experiment's,
        written out complete (``extends`` merged).

        A source that extends nothing and names no event is copied byte for byte, comments
        and all; any other is written as its merged settings under a header saying what it
        was made from. Event names are taken out (they are each experiment's) and returned
        as ``left_out``, so the caller can say so."""
        text, removed, meta_template = template_text(name, source, shared=shared, template=template)
        config = self.create(
            name, text, origin={"kind": "template", "template": meta_template}, by=by
        )
        status = self.config_status(config, self.default_id())
        return {**status, "left_out": removed}

    def check_text(self, ref: str | None, text: str) -> dict[str, Any]:
        machine = self.read(ref)
        path = machine.path if machine else self.folder / f"{FILE_PREFIX}check.yaml"
        values = self._check_text(text, path)
        return {"ok": True, "changes": rig_changes(machine.values, values) if machine else []}

    def save_text(
        self, ref: str | None, text: str, expected_sha: str | None, *, by: str
    ) -> dict[str, Any]:
        """Replace a config's file with ``text`` (the General page's YAML editor, or a
        platform edit), refused if the file changed since it was read (``expected_sha``)."""
        with self.lock:
            config = self.find(ref)
            machine = self._read(config)
            self._expect(machine, expected_sha)
            values = self._check_text(text, machine.path)
            changes = rig_changes(machine.values, values)
            self._write(config, text, {"by": by, "changes": [c["path"] for c in changes]})
            return {"id": config.id, "saved": [c["path"] for c in changes]}

    def save_values(
        self, ref: str | None, values: dict[str, Any], expected_sha: str | None, *, by: str
    ) -> dict[str, Any]:
        """Save settings as a config's (the Run page's "Save to <config>", the General page's
        form). Event names are left out: they stay the session's. The file is written from
        the settings, so its comments go; the previous file is kept in history/."""
        with self.lock:
            config = self.find(ref)
            machine = self._read(config)
            self._expect(machine, expected_sha)
            if not isinstance(values, dict):
                raise MachineRigError("The rig settings must be a mapping")
            clean, _removed = strip_events(values)
            if "extends" in clean:
                raise MachineRigError("A machine's rig config extends nothing")
            try:
                validate(clean, machine.path)
            except ConfigError as exc:
                raise MachineRigError(str(exc)) from exc
            changes = rig_changes(machine.values, clean)
            if not changes:
                return {"id": config.id, "saved": []}
            header = (
                f"This machine's rig config '{machine.name}'. Saved {now()[:10]} from the "
                f"{by} (changed: {', '.join(c['path'] for c in changes)}); earlier versions are "
                f"in {HISTORY}/ beside this file."
            )
            self._write(
                config, dump(clean, header), {"by": by, "changes": [c["path"] for c in changes]}
            )
            return {"id": config.id, "saved": [c["path"] for c in changes]}

    def rename(self, ref: str | None, name: Any, expected_sha: str | None, *, by: str) -> Config:
        """Rename a config: its file and the gamma and reward files beside it take the new
        name (PsychoPy's monitor and a session's copy are named like it); its history keeps
        the old names. The first config stays where a 3.5.0 dashboard looks for it."""
        with self.lock:
            config = self.find(ref)
            machine = self._read(config)
            self._expect(machine, expected_sha)
            name = check_name(name)
            if name == config.name:
                return config
            self._unique(name, but=config.id)
            old_stem = f"{FILE_PREFIX}{config.name}"
            new_stem = f"{FILE_PREFIX}{name}"
            if config.legacy:
                folder = self.folder
            else:
                folder = self.folder / CONFIGS / name
                if folder.exists():
                    raise MachineRigError(f"{folder} already exists; choose another name")
                os.replace(config.folder, folder)
            for suffix in (".yaml", GAMMA_FILENAME_SUFFIX, REWARD_CALIBRATION_SUFFIX):
                source = folder / f"{old_stem}{suffix}"
                if source.is_file():
                    os.replace(source, folder / f"{new_stem}{suffix}")
            data = self.index()
            entry = data["configs"][config.id]
            entry["name"] = name
            if not config.legacy:
                entry["folder"] = f"{CONFIGS}/{name}"
            entry["revision"] = int(entry.get("revision", 0)) + 1
            self._save_index(data)
            renamed = self._config(config.id, entry)
            meta = _read_json(renamed.meta_file)
            saves = list(meta.get("saves") or [])
            saves.append(
                {"by": by, "at": now(), "renamed": [config.name, name], "sha256": machine.sha256}
            )
            meta["saves"] = saves[-MAX_SAVES:]
            meta["name"] = name
            _write_json(renamed.meta_file, meta)
            return renamed

    def set_default(self, ref: str, *, count: bool = True) -> Config:
        with self.lock:
            config = self.find(ref)
            data = self.index()
            if data.get("default") != config.id:
                data["default"] = config.id
                if count:
                    data["default_rev"] = int(data.get("default_rev", 0)) + 1
                self._save_index(data)
            return config

    def delete(self, ref: str, expected_sha: str | None, *, by: str) -> dict[str, Any]:
        """Move a config's folder (for the first config: its files) to trash/; refused for
        the default config. Returns the index entry it had."""
        with self.lock:
            config = self.find(ref)
            data = self.index()
            if data.get("default") == config.id:
                raise MachineRigError(
                    f"{config.name!r} is this machine's default rig config: make another config "
                    "the default first"
                )
            if config.file.is_file():
                self._expect(self._read(config), expected_sha)
            target = self.folder / TRASH / f"{config.name}.{_stamp()}"
            target.mkdir(parents=True)
            if config.legacy:
                stem = f"{FILE_PREFIX}{config.name}"
                for suffix in (".yaml", GAMMA_FILENAME_SUFFIX, REWARD_CALIBRATION_SUFFIX):
                    source = self.folder / f"{stem}{suffix}"
                    if source.is_file():
                        os.replace(source, target / source.name)
                for extra in (META_FILE, HISTORY):
                    if (self.folder / extra).exists():
                        os.replace(self.folder / extra, target / extra)
            elif config.folder.exists():
                os.replace(config.folder, target / CONFIGS)
            entry = data["configs"].pop(config.id)
            _write_json(
                target / "deleted.json", {"id": config.id, **entry, "deleted": now(), "by": by}
            )
            self._save_index(data)
            return {"id": config.id, **entry, "trash": str(target)}

    def history(self, ref: str) -> dict[str, Any]:
        config = self.find(ref)
        meta = _read_json(config.meta_file)
        folder = config.folder / HISTORY
        versions = []
        if folder.is_dir():
            for p in sorted(folder.iterdir(), reverse=True):
                if p.is_file() and p.suffix in {".yaml", ".yml"}:
                    versions.append({"file": p.name, "at": _mtime(p), "size": p.stat().st_size})
        return {
            "id": config.id,
            "saves": list(reversed(meta.get("saves") or [])),
            "versions": versions,
        }

    def version(self, ref: str, file: str) -> str:
        config = self.find(ref)
        folder = (config.folder / HISTORY).resolve()
        if not isinstance(file, str) or "/" in file or "\\" in file or file.startswith("."):
            raise MachineRigError("Name one file of the config's history")
        path = (folder / file).resolve()
        if path.parent != folder or not path.is_file():
            raise MachineRigError(f"The history of {config.name!r} has no {file}")
        return path.read_text(encoding="utf-8-sig")

    def update_sync(self, cid: str, **fields: Any) -> None:
        """Merge ``fields`` into one config's sync state in the index (None removes a field)."""
        with self.lock:
            data = self.index()
            if cid not in data["configs"]:
                return
            sync = dict(data["configs"][cid].get("sync") or {})
            for key, value in fields.items():
                if value is None:
                    sync.pop(key, None)
                else:
                    sync[key] = value
            data["configs"][cid]["sync"] = sync
            self._save_index(data)

    # -- a session's rig ----------------------------------------------------------------

    def companions(self, machine: Machine) -> list[Path]:
        """The files kept beside a config's rig and read with it: a measured gamma and a
        reward calibration, where they exist."""
        stem = machine.path.stem
        folder = machine.path.parent
        found = [
            folder / f"{stem}{GAMMA_FILENAME_SUFFIX}",
            folder / f"{stem}{REWARD_CALIBRATION_SUFFIX}",
        ]
        return [p for p in found if p.is_file()]


def _mtime(path: Path) -> str | None:
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds")


def template_text(
    name: Any, source: Path, *, shared: dict[str, Path] | None, template: dict[str, Any]
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """The text a new config starts with from a template (a shared rig or an experiment's),
    the event names left out of it, and what to record about the template."""
    name = check_name(name)
    merged = rig_mapping(Path(source), shared=shared)
    values, removed = strip_events(merged.values)
    if merged.base is None and not removed:
        text = Path(source).read_text(encoding="utf-8-sig")
    else:
        made = [f"This machine's rig config '{name}': one complete file, made {now()[:10]} from"]
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
    return text, removed, meta_template


def session_header(machine: Machine, origin: dict[str, Any]) -> str:
    changes = origin.get("changes") or []
    lines = [
        f"The rig this session ran on: this machine's rig config '{machine.name}' ({machine.path},",
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
