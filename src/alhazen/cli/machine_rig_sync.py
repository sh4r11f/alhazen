"""Syncing this machine's rig configs with the platform (docs/design/machine-rig-configs.md §4).

The rig's own files are what runs, and work offline; the platform keeps a copy per machine
that the person can edit there. A sync reads the machine's record from the platform, decides
per config what to do — send a change made here, take (or refuse) one made there, report a
conflict when both changed — and sends this machine's side in one request. The platform's
rules (rigsync.py) are compare-and-set on each config's revision, so neither side ever
overwrites a change it has not seen.

This machine validates: a platform edit is taken only when it passes the checks a launch
makes (machine_rig.MachineRigs._check_text); otherwise it is refused with the reason, once
per revision, and the version here keeps running.

A sync runs in the background after every change here (``soon``), every few minutes while
the dashboard runs, when a page reads the configs and it is due, and on demand. A platform
that cannot be reached, one without rig configs (an alhazen before 3.6) or no connection at
all leaves everything local; ``state()`` says which.
"""

from __future__ import annotations

import json
import platform as platform_module
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from alhazen.cli import machine_rig as mr
from alhazen.config.gamma import GAMMA_FILENAME_SUFFIX
from alhazen.config.reward_calibration import REWARD_CALIBRATION_SUFFIX
from alhazen.errors import ConfigError
from alhazen.experiment_platform import rigsync
from alhazen.experiment_platform.library import LibraryError, RigsUnsupported

# Connection = () -> (source, client) or None when no library is connected.
Connection = Callable[[], Any]
DUE_S = 60
INTERVAL_S = 300


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def rig_sync_view(config: dict[str, Any], sync: dict[str, Any], engine: Any) -> dict[str, Any]:
    """One config's sync state as the pages show it (contract §3 ``Config.sync``)."""
    view = {
        "state": "not-synced",
        "platform_revision": sync.get("platform_revision"),
        "rejected": sync.get("rejected"),
        "conflict": sync.get("conflict"),
    }
    if engine is None or not engine.connected():
        return view
    if sync.get("conflict"):
        view["state"] = "conflict"
    elif config.get("sha256") and config.get("sha256") != sync.get("sha256"):
        view["state"] = "local"
    elif sync.get("rejected"):
        view["state"] = "rejected"
    elif sync.get("platform_revision"):
        view["state"] = "synced"
    return view


class RigSync:
    """The sync of one workspace's rig configs. ``connection`` gives the library's
    connection and a client for it (None: not connected); ``templates`` and ``schema``
    give what the platform's editor needs."""

    def __init__(
        self,
        rigs: mr.MachineRigs,
        connection: Connection,
        *,
        templates: Callable[[], list[dict[str, Any]]] = list,
        schema: Callable[[], dict[str, Any]] | None = None,
        version: str = "",
    ) -> None:
        self.rigs = rigs
        self._connection = connection
        self._templates = templates
        self._schema = schema
        self._version = version
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state: dict[str, Any] = {
            "state": "not-connected",
            "at": None,
            "error": None,
            "platform": None,
            "connected": False,
        }
        self._tried: float | None = None
        self._again = False
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._timer: threading.Thread | None = None
        self._label: str | None = None

    # ------------------------------------------------------------------ status
    def state(self) -> dict[str, Any]:
        with self._state_lock:
            state = dict(self._state)
        if not state["connected"]:
            try:
                connected = self._connection() is not None
            except LibraryError as error:
                return {**state, "state": "error", "error": str(error)}
            state["connected"] = connected
            if connected and state["state"] == "not-connected":
                state["state"] = "not-synced"
        return state

    def connected(self) -> bool:
        return bool(self.state()["connected"])

    def label(self) -> str:
        return self._label or platform_module.node() or "this machine"

    def _set(self, **fields: Any) -> None:
        with self._state_lock:
            self._state.update(fields)

    # ------------------------------------------------------------------ scheduling
    def soon(self) -> None:
        """Sync in the background now (again after the running one, if one runs)."""
        with self._state_lock:
            if self._worker is not None and self._worker.is_alive():
                self._again = True
                return

            def work() -> None:
                while True:
                    self.sync()
                    with self._state_lock:
                        if not self._again:
                            return
                        self._again = False

            self._worker = threading.Thread(target=work, daemon=True, name="rig-sync")
            self._worker.start()

    def due(self) -> None:
        """Sync in the background when the last attempt is older than a minute."""
        if self._tried is None or time.monotonic() - self._tried > DUE_S:
            self.soon()

    def start(self, interval: float = INTERVAL_S) -> None:
        """Sync every ``interval`` seconds until ``close``."""

        def loop() -> None:
            while not self._stop.wait(interval):
                self.sync()

        self._timer = threading.Thread(target=loop, daemon=True, name="rig-sync-timer")
        self._timer.start()
        self.soon()

    def close(self) -> None:
        self._stop.set()

    def join(self, timeout: float = 10) -> None:
        """Wait for a background sync to finish (tests, and shutting down)."""
        worker = self._worker
        if worker is not None:
            worker.join(timeout)

    # ------------------------------------------------------------------ the sync
    def sync(self) -> dict[str, Any]:
        """One sync now; never raises for a platform that cannot be reached or refuses (the
        reason goes into ``state()``). Returns ``state()``."""
        with self._lock:
            self._tried = time.monotonic()
            try:
                found = self._connection()
            except LibraryError as error:
                self._set(state="error", error=str(error), connected=False, at=_now())
                return self.state()
            if found is None:
                self._set(state="not-connected", error=None, connected=False, platform=None)
                return self.state()
            source, client = found
            self._label = getattr(source, "machine", None) or platform_module.node() or None
            self._set(connected=True, platform=getattr(source, "label", None))
            try:
                for _round in range(2):
                    if not self._round(client):
                        break
            except RigsUnsupported as error:
                self._set(state="unsupported", error=str(error), at=_now())
            except LibraryError as error:
                self._set(state="offline", error=str(error), at=_now())
            except (OSError, ValueError, rigsync.RigSyncError) as error:
                self._set(
                    state="error", error=f"The rig configs could not be synced: {error}", at=_now()
                )
            else:
                self._set(state="synced", error=None, at=_now())
            return self.state()

    def _round(self, client: Any) -> bool:
        """Read, decide, send. True when a write was refused as stale, so a second round
        should report the conflict at once."""
        mid = self.rigs.machine_id()
        record = rigsync.normalize(client.rig_record(mid), mid)
        index = self.rigs.index()
        platform_configs = record["configs"]
        push: dict[str, Any] = {
            "label": self.label(),
            "alhazen": self._version,
            "writes": [],
            "acks": [],
            "conflicts": [],
            "deletes": [],
            "running": {},
        }
        texts: dict[str, str] = {}
        statuses = {c["id"]: c for c in self.rigs.status()["configs"]}
        for cid, entry in list(index["configs"].items()):
            status = statuses.get(cid)
            if status is None or status.get("text") is None:
                continue  # its file is gone; the page shows why
            sync = entry.get("sync") or {}
            p = platform_configs.get(cid)
            self._decide(cid, status, sync, p, push, texts, index)
        for cid, p in platform_configs.items():
            if cid in index["configs"]:
                continue
            gone = (index.get("deleted") or {}).get(cid)
            if gone is not None:
                push["deletes"].append({"id": cid, "base": int(gone.get("platform_revision") or 0)})
            elif p["deleted"]:
                push["acks"].append({"id": cid, "revision": p["revision"], "ok": True})
            else:
                self._create_here(cid, p, push)
        # Deletions here of configs the platform no longer has are settled.
        for cid in list(index.get("deleted") or {}):
            if cid not in platform_configs:
                self._forget_deleted(cid)
        # The running version of every config, after what was taken above.
        for config in self.rigs.status()["configs"]:
            if config.get("text") is None:
                continue
            push["running"][config["id"]] = {
                "name": config["name"],
                "text": config["text"],
                "problem": config.get("problem"),
                "calibration": self._calibration(config),
            }
        self._default(record, push)
        templates_sha = self._extras(record, push)
        answer = client.rig_sync(mid, push)
        after = rigsync.normalize(answer.get("record"), mid)
        results = answer.get("results") or {}
        stale = False
        for write in push["writes"]:
            result = results.get(write["id"]) or {}
            if result.get("ok"):
                self.rigs.update_sync(
                    write["id"],
                    platform_revision=result["revision"],
                    sha256=write["sha256"],
                    rejected=None,
                    conflict=None,
                )
            else:
                stale = True
        for delete in push["deletes"]:
            if (results.get(delete["id"]) or {}).get("ok"):
                self._forget_deleted(delete["id"])
        with self.rigs.lock:
            data = self.rigs.index()
            data["sync"] = {
                **(data.get("sync") or {}),
                "platform_default_rev": after["defaultRev"],
                "templates_sha": templates_sha or (data.get("sync") or {}).get("templates_sha"),
                "at": _now(),
            }
            self.rigs._save_index(data)
        return stale

    def _decide(
        self,
        cid: str,
        status: dict[str, Any],
        sync: dict[str, Any],
        p: dict[str, Any] | None,
        push: dict[str, Any],
        texts: dict[str, str],
        index: dict[str, Any],
    ) -> None:
        text, sha = status["text"], status["sha256"]
        write = {"id": cid, "name": status["name"], "text": text, "sha256": sha}
        if p is None:
            push["writes"].append({**write, "base": 0})
            return
        agreed_sha = sync.get("sha256")
        agreed_rev = sync.get("platform_revision")
        if agreed_rev is None and p["sha256"] == sha and not p["deleted"]:
            # Never synced, but the same text: agree on it.
            self.rigs.update_sync(cid, platform_revision=p["revision"], sha256=sha)
            agreed_sha, agreed_rev = sha, p["revision"]
        local_changed = (
            sha != agreed_sha or status["name"] != p["name"] and agreed_rev == p["revision"]
        )
        platform_changed = p["revision"] != agreed_rev
        if p["deleted"]:
            if local_changed or index.get("default") == cid:
                # A deletion asked there of a config changed here (or the default): this
                # machine's version stays, written back over the deletion.
                push["writes"].append({**write, "base": p["revision"]})
            else:
                self._delete_here(cid, sha, p, push)
            return
        if not platform_changed and not local_changed:
            # The same version on both sides, but the platform still shows it as waiting
            # or in conflict (an ack lost, a conflict settled here): say it is taken.
            if (p["state"] != "synced" or p["conflict"]) and p["sha256"] == sha:
                push["acks"].append({"id": cid, "revision": p["revision"], "ok": True})
            return
        if not platform_changed:
            push["writes"].append({**write, "base": p["revision"]})
            return
        if not local_changed or p["basedOn"] == sha:
            self._take(cid, status, p, push)
            return
        rejected = sync.get("rejected") or {}
        if p["state"] == "rejected" and rejected.get("revision") == p["revision"]:
            # The platform's latest is an edit this machine refused; the change made here
            # since replaces it.
            push["writes"].append({**write, "base": p["revision"]})
            return
        if p["sha256"] == sha:
            push["acks"].append({"id": cid, "revision": p["revision"], "ok": True})
            self.rigs.update_sync(cid, platform_revision=p["revision"], sha256=sha, conflict=None)
            return
        push["conflicts"].append({"id": cid, "text": text})
        self.rigs.update_sync(
            cid,
            conflict={
                "platform_text": p["text"],
                "platform_sha256": p["sha256"],
                "platform_revision": p["revision"],
                "platform_name": p["name"],
                "at": _now(),
                "by": p["updatedBy"],
            },
        )

    def _take(
        self, cid: str, status: dict[str, Any], p: dict[str, Any], push: dict[str, Any]
    ) -> None:
        """A platform edit: taken when it passes a launch's checks, else refused once."""
        sync = (self.rigs.index()["configs"].get(cid) or {}).get("sync") or {}
        if (sync.get("rejected") or {}).get("revision") == p["revision"]:
            return  # refused already; the platform knows
        if p["sha256"] == status["sha256"] and p["name"] == status["name"]:
            push["acks"].append({"id": cid, "revision": p["revision"], "ok": True})
            self.rigs.update_sync(
                cid,
                platform_revision=p["revision"],
                sha256=status["sha256"],
                rejected=None,
                conflict=None,
            )
            return
        try:
            machine = self.rigs.require(cid)
            self.rigs._check_text(p["text"], machine.path)
            if p["name"] != status["name"]:
                self.rigs.rename(cid, p["name"], status["sha256"], by="platform")
            if p["sha256"] != status["sha256"]:
                self.rigs.save_text(cid, p["text"], status["sha256"], by="platform")
        except (mr.MachineRigError, ConfigError, OSError) as error:
            reason = str(error)
            push["acks"].append(
                {"id": cid, "revision": p["revision"], "ok": False, "reason": reason}
            )
            self.rigs.update_sync(
                cid,
                rejected={
                    "revision": p["revision"],
                    "reason": reason,
                    "text": p["text"],
                    "at": _now(),
                },
            )
            return
        push["acks"].append({"id": cid, "revision": p["revision"], "ok": True})
        self.rigs.update_sync(
            cid, platform_revision=p["revision"], sha256=p["sha256"], rejected=None, conflict=None
        )

    def _create_here(self, cid: str, p: dict[str, Any], push: dict[str, Any]) -> None:
        """A config made on the platform: created here when it passes a launch's checks."""
        try:
            rigsync.check_config_id(cid)
            self.rigs.create(
                p["name"], p["text"], origin={"kind": "platform"}, by="platform", cid=cid
            )
        except (mr.MachineRigError, ConfigError, OSError, rigsync.RigSyncError) as error:
            push["acks"].append(
                {"id": cid, "revision": p["revision"], "ok": False, "reason": str(error)}
            )
            return
        push["acks"].append({"id": cid, "revision": p["revision"], "ok": True})
        self.rigs.update_sync(cid, platform_revision=p["revision"], sha256=p["sha256"])

    def _delete_here(self, cid: str, sha: str, p: dict[str, Any], push: dict[str, Any]) -> None:
        try:
            self.rigs.delete(cid, sha, by="platform")
        except (mr.MachineRigError, OSError) as error:
            push["acks"].append(
                {"id": cid, "revision": p["revision"], "ok": False, "reason": str(error)}
            )
            return
        push["acks"].append({"id": cid, "revision": p["revision"], "ok": True})

    def _calibration(self, config: dict[str, Any]) -> dict[str, Any]:
        path = config.get("file")
        out: dict[str, Any] = {"gamma": None, "reward": None}
        if not path:
            return out
        from pathlib import Path

        rig = Path(path)
        gamma = rig.parent / f"{rig.stem}{GAMMA_FILENAME_SUFFIX}"
        reward = rig.parent / f"{rig.stem}{REWARD_CALIBRATION_SUFFIX}"
        if gamma.is_file():
            out["gamma"] = {"at": mr._mtime(gamma)}
        if reward.is_file():
            out["reward"] = {"at": mr._mtime(reward)}
        return out

    def _default(self, record: dict[str, Any], push: dict[str, Any]) -> None:
        index = self.rigs.index()
        seen = int((index.get("sync") or {}).get("platform_default_rev") or 0)
        theirs = record.get("default")
        if (
            record["defaultRev"] != seen
            and record.get("defaultBy") == "platform"
            and isinstance(theirs, str)
            and theirs in index["configs"]
            and theirs != index.get("default")
        ):
            self.rigs.set_default(theirs)
            index = self.rigs.index()
        push["default"] = index.get("default")
        push["defaultRevSeen"] = record["defaultRev"]

    def _extras(self, record: dict[str, Any], push: dict[str, Any]) -> str | None:
        """The rig schema and the templates, sent when the platform's differ; returns the
        templates' sha when they were sent."""
        if self._schema is not None:
            text = json.dumps(self._schema(), sort_keys=True)
            sha = rigsync.sha256_text(text)
            if record.get("rigSchemaSha") != sha:
                push["rigSchema"], push["rigSchemaSha"] = text, sha
        try:
            templates = json.dumps(self._templates(), sort_keys=True)
        except (OSError, ValueError, ConfigError):
            return None
        sha = rigsync.sha256_text(templates)
        known = (self.rigs.index().get("sync") or {}).get("templates_sha")
        if known != sha or record.get("templates") is None:
            push["templates"] = templates
            return sha
        return None

    # ------------------------------------------------------------------ the page's actions
    def deleted_here(self, removed: dict[str, Any]) -> None:
        """A config deleted on this machine: remember to delete it on the platform."""
        sync = removed.get("sync") or {}
        if not sync.get("platform_revision"):
            return
        with self.rigs.lock:
            data = self.rigs.index()
            data.setdefault("deleted", {})[removed["id"]] = {
                "platform_revision": sync["platform_revision"],
                "name": removed.get("name"),
            }
            self.rigs._save_index(data)

    def _forget_deleted(self, cid: str) -> None:
        with self.rigs.lock:
            data = self.rigs.index()
            if cid in (data.get("deleted") or {}):
                del data["deleted"][cid]
                self.rigs._save_index(data)

    def resolve(self, cid: str, keep: Any) -> None:
        """Settle a conflict here: keep this machine's version (sent over the platform's) or
        the platform's (validated and written here). Then sync."""
        if keep not in ("machine", "platform"):
            raise ValueError('Keep "machine" or "platform"')
        entry = self.rigs.index()["configs"].get(cid)
        if entry is None:
            raise ValueError("This machine has no such rig config")
        conflict = (entry.get("sync") or {}).get("conflict")
        if not conflict:
            raise ValueError("This rig config has no conflict to settle")
        machine = self.rigs.require(cid)
        if keep == "machine":
            # Agree with the platform's revision as it was, so this machine's version is
            # sent over it (compare-and-set: if the platform moved again, it is a new conflict).
            self.rigs.update_sync(
                cid,
                platform_revision=conflict["platform_revision"],
                sha256="settled-here",
                conflict=None,
            )
        else:
            text = conflict["platform_text"]
            try:
                self.rigs._check_text(text, machine.path)
            except mr.MachineRigError as error:
                raise ValueError(f"The platform's version cannot run here: {error}") from None
            name = conflict.get("platform_name") or machine.name
            if name != machine.name:
                self.rigs.rename(cid, name, machine.sha256, by="platform")
            self.rigs.save_text(cid, text, machine.sha256, by="platform")
            self.rigs.update_sync(
                cid,
                platform_revision=conflict["platform_revision"],
                sha256=rigsync.sha256_text(text),
                conflict=None,
                rejected=None,
            )
        self.sync()
